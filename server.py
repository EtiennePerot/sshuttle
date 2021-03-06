import re, struct, socket, select, traceback, time
if not globals().get('skip_imports'):
    import ssnet, helpers, hostwatch
    import compat.ssubprocess as ssubprocess
    from ssnet import SockWrapper, Handler, Proxy, Mux, MuxWrapper
    from helpers import *


def _ipmatch(ipstr):
    if ipstr == 'default':
        ipstr = '0.0.0.0/0'
    m = re.match(r'^(\d+(\.\d+(\.\d+(\.\d+)?)?)?)(?:/(\d+))?$', ipstr)
    if m:
        g = m.groups()
        ips = g[0]
        width = int(g[4] or 32)
        if g[1] == None:
            ips += '.0.0.0'
            width = min(width, 8)
        elif g[2] == None:
            ips += '.0.0'
            width = min(width, 16)
        elif g[3] == None:
            ips += '.0'
            width = min(width, 24)
        return (struct.unpack('!I', socket.inet_aton(ips))[0], width)


def _ipstr(ip, width):
    if width >= 32:
        return ip
    else:
        return "%s/%d" % (ip, width)


def _maskbits(netmask):
    if not netmask:
        return 32
    for i in range(32):
        if netmask[0] & _shl(1, i):
            return 32-i
    return 0


def _shl(n, bits):
    return n * int(2**bits)


def _list_routes():
    argv = ['netstat', '-rn']
    p = ssubprocess.Popen(argv, stdout=ssubprocess.PIPE)
    routes = []
    for line in p.stdout:
        cols = re.split(r'\s+', line)
        ipw = _ipmatch(cols[0])
        if not ipw:
            continue  # some lines won't be parseable; never mind
        maskw = _ipmatch(cols[2])  # linux only
        mask = _maskbits(maskw)   # returns 32 if maskw is null
        width = min(ipw[1], mask)
        ip = ipw[0] & _shl(_shl(1, width) - 1, 32-width)
        routes.append((socket.inet_ntoa(struct.pack('!I', ip)), width))
    rv = p.wait()
    if rv != 0:
        log('WARNING: %r returned %d\n' % (argv, rv))
        log('WARNING: That prevents --auto-nets from working.\n')
    return routes


def list_routes():
    for (ip,width) in _list_routes():
        if not ip.startswith('0.') and not ip.startswith('127.'):
            yield (ip,width)


def _exc_dump():
    exc_info = sys.exc_info()
    return ''.join(traceback.format_exception(*exc_info))


def start_hostwatch(seed_hosts):
    s1,s2 = socket.socketpair()
    pid = os.fork()
    if not pid:
        # child
        rv = 99
        try:
            try:
                s2.close()
                os.dup2(s1.fileno(), 1)
                os.dup2(s1.fileno(), 0)
                s1.close()
                rv = hostwatch.hw_main(seed_hosts) or 0
            except Exception, e:
                log('%s\n' % _exc_dump())
                rv = 98
        finally:
            os._exit(rv)
    s1.close()
    return pid,s2


class Hostwatch:
    def __init__(self):
        self.pid = 0
        self.sock = None


class DnsProxy(Handler):
    def __init__(self, mux, chan, request):
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        Handler.__init__(self, [sock])
        self.timeout = time.time()+30
        self.mux = mux
        self.chan = chan
        self.tries = 0
        self.peer = None
        self.request = request
        self.sock = sock
        self.sock.setsockopt(socket.SOL_IP, socket.IP_TTL, 42)
        self.try_send()

    def try_send(self):
        if self.tries >= 3:
            return
        self.tries += 1
        self.peer = resolvconf_random_nameserver()
        self.sock.connect((self.peer, 53))
        debug2('DNS: sending to %r\n' % self.peer)
        try:
            self.sock.send(self.request)
        except socket.error, e:
            if e.args[0] in ssnet.NET_ERRS:
                # might have been spurious; try again.
                # Note: these errors sometimes are reported by recv(),
                # and sometimes by send().  We have to catch both.
                debug2('DNS send to %r: %s\n' % (self.peer, e))
                self.try_send()
                return
            else:
                log('DNS send to %r: %s\n' % (self.peer, e))
                return

    def callback(self):
        try:
            data = self.sock.recv(4096)
        except socket.error, e:
            if e.args[0] in ssnet.NET_ERRS:
                # might have been spurious; try again.
                # Note: these errors sometimes are reported by recv(),
                # and sometimes by send().  We have to catch both.
                debug2('DNS recv from %r: %s\n' % (self.peer, e))
                self.try_send()
                return
            else:
                log('DNS recv from %r: %s\n' % (self.peer, e))
                return
        debug2('DNS response: %d bytes\n' % len(data))
        self.mux.send(self.chan, ssnet.CMD_DNS_RESPONSE, data)
        self.ok = False

_udp_sockets = {} # Maps port numbers to udp_socket instances
class udp_socket(Handler):
    def __init__(self, mux, chan, source_port, forwarding=False):
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        Handler.__init__(self, [self.sock])
        try:
            self.sock.bind(('', source_port))
        except socket.error:
            for source_port in xrange(3000, 65000):
                try:
                    self.sock.bind(('', source_port))
                except:
                    pass
        self.source_port = source_port
        _udp_sockets[self.source_port] = self
        self.channels = {}
        self.mux = mux
        self.chan = chan
        self.forwarding = forwarding
        self.default_key = struct.pack('!H4sH', self.source_port, '\x00\x00\x00\x00', 0)
        debug1('UDP socket listening on port %d\n' % self.source_port)
    def addchannel(self, channel, key):
        self.channels[key] = channel
    def removechannel(self, key):
        del self.channels[key]
        if not self.channels:
            self.close()
    def sendto(self, data, destination):
        self.sock.sendto(data, destination)
    def callback(self):
        try:
            data, source = self.sock.recvfrom(131072)
            if source in self.channels:
                self.channels[source].callback(source, data)
            else:
                self.mux.send(self.chan, ssnet.CMD_UDP_IN, self.default_key + struct.pack('!4sH', socket.inet_aton(source[0]), source[1]) + data)
        except socket.error:
            self.close()
    def close(self):
        if not self.forwarding:
            self.ok = False
            try:
                self.sock.close()
            except:
                pass

class udp_channel:
    udp_channel_timeout = 600
    def __init__(self, mux, chan, handlers, source_port, destination, destination_port):
        self.original_key = struct.pack('!H4sH', source_port, destination, destination_port)
        self.timeout = time.time() + udp_channel.udp_channel_timeout
        self.source_port = source_port
        self.destination = socket.inet_ntoa(destination)
        self.destination_port = destination_port
        if self.source_port not in _udp_sockets:
            handlers.append(udp_socket(mux, chan, self.source_port))
        self.socket = _udp_sockets[self.source_port]
        self.socket.addchannel(self, (self.destination, self.destination_port))
        self.mux = mux
        self.chan = chan
        self.ok = True
        debug1('UDP channel opened from port %d to %s: %d\n' % (self.source_port, self.destination, self.destination_port))
    def send(self, data):
        try:
            self.socket.sendto(data, (self.destination, self.destination_port))
        except socket.error:
            self.close()
    def callback(self, source, data):
        try:
            self.mux.send(self.chan, ssnet.CMD_UDP_IN, self.original_key + struct.pack('!4sH', socket.inet_aton(source[0]), source[1]) + data)
        except socket.error:
            self.close()
    def close(self):
        self.ok = False
        self.socket.removechannel(self)
        try:
            self.sock.close()
        except:
            pass

def main():
    if helpers.verbose >= 1:
        helpers.logprefix = ' s: '
    else:
        helpers.logprefix = 'server: '
    debug1('latency control setting = %r\n' % latency_control)

    routes = list(list_routes())
    debug1('available routes:\n')
    for r in routes:
        debug1('  %s/%d\n' % r)

    # synchronization header
    sys.stdout.write('\0\0SSHUTTLE0001')
    sys.stdout.flush()

    handlers = []
    mux = Mux(socket.fromfd(sys.stdin.fileno(),
                            socket.AF_INET, socket.SOCK_STREAM),
              socket.fromfd(sys.stdout.fileno(),
                            socket.AF_INET, socket.SOCK_STREAM))
    handlers.append(mux)
    routepkt = ''
    for r in routes:
        routepkt += '%s,%d\n' % r
    mux.send(0, ssnet.CMD_ROUTES, routepkt)

    hw = Hostwatch()
    hw.leftover = ''

    def hostwatch_ready():
        assert(hw.pid)
        content = hw.sock.recv(4096)
        if content:
            lines = (hw.leftover + content).split('\n')
            if lines[-1]:
                # no terminating newline: entry isn't complete yet!
                hw.leftover = lines.pop()
                lines.append('')
            else:
                hw.leftover = ''
            mux.send(0, ssnet.CMD_HOST_LIST, '\n'.join(lines))
        else:
            raise Fatal('hostwatch process died')

    def got_host_req(data):
        if not hw.pid:
            (hw.pid,hw.sock) = start_hostwatch(data.strip().split())
            handlers.append(Handler(socks = [hw.sock],
                                    callback = hostwatch_ready))
    mux.got_host_req = got_host_req

    def new_channel(channel, data):
        (dstip,dstport) = data.split(',', 1)
        dstport = int(dstport)
        outwrap = ssnet.connect_dst(dstip,dstport)
        handlers.append(Proxy(MuxWrapper(mux, channel), outwrap))
    mux.new_channel = new_channel

    dnshandlers = {}
    def dns_req(channel, data):
        debug2('Incoming DNS request.\n')
        h = DnsProxy(mux, channel, data)
        handlers.append(h)
        dnshandlers[channel] = h
    mux.got_dns_req = dns_req

    udphandlers = {}
    def udp_req(channel, data):
        key = struct.unpack('!H4sH', data[:8])
        udp_content = data[8:]
        if key not in udphandlers:
            debug2('Opening UDP channel.\n')
            u = udp_channel(mux, channel, handlers, key[0], key[1], key[2])
            udphandlers[key] = u
        udphandlers[key].send(udp_content)
    def udp_fwd(channel, data):
        handlers.append(udp_socket(mux, channel, struct.unpack('!H', data)[0], forwarding=True))
    mux.udp_out = udp_req
    mux.udp_fwd = udp_fwd

    while mux.ok:
        if hw.pid:
            assert(hw.pid > 0)
            (rpid, rv) = os.waitpid(hw.pid, os.WNOHANG)
            if rpid:
                raise Fatal('hostwatch exited unexpectedly: code 0x%04x\n' % rv)

        ssnet.runonce(handlers, mux)
        if latency_control:
            mux.check_fullness()
        mux.callback()

        if dnshandlers:
            now = time.time()
            for channel,h in dnshandlers.items():
                if h.timeout < now or not h.ok:
                    del dnshandlers[channel]
                    h.ok = False

        if udphandlers:
            now = time.time()
            for key,u in udphandlers.items():
                if u.timeout < now or not u.ok:
                    del udphandlers[key]
                    u.close()
