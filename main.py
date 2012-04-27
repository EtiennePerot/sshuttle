import sys, os, re
import helpers, options, client, server, firewall, hostwatch
import compat.ssubprocess as ssubprocess
from helpers import *


# list of:
# 1.2.3.4/5 or just 1.2.3.4
def parse_subnets(subnets_str):
    subnets = []
    for s in subnets_str:
        m = re.match(r'(\d+)(?:\.(\d+)\.(\d+)\.(\d+))?(?:/(\d+))?$', s)
        if not m:
            raise Fatal('%r is not a valid IP subnet format' % s)
        (a,b,c,d,width) = m.groups()
        (a,b,c,d) = (int(a or 0), int(b or 0), int(c or 0), int(d or 0))
        if width == None:
            width = 32
        else:
            width = int(width)
        if a > 255 or b > 255 or c > 255 or d > 255:
            raise Fatal('%d.%d.%d.%d has numbers > 255' % (a,b,c,d))
        if width > 32:
            raise Fatal('*/%d is greater than the maximum of 32' % width)
        subnets.append(('%d.%d.%d.%d' % (a,b,c,d), width))
    return subnets


# 1.2.3.4:567 or just 1.2.3.4 or just 567
def parse_ipport(s):
    s = str(s)
    m = re.match(r'(?:(\d+)\.(\d+)\.(\d+)\.(\d+))?(?::)?(?:(\d+))?$', s)
    if not m:
        raise Fatal('%r is not a valid IP:port format' % s)
    (a,b,c,d,port) = m.groups()
    (a,b,c,d,port) = (int(a or 0), int(b or 0), int(c or 0), int(d or 0),
                      int(port or 0))
    if a > 255 or b > 255 or c > 255 or d > 255:
        raise Fatal('%d.%d.%d.%d has numbers > 255' % (a,b,c,d))
    if port > 65535:
        raise Fatal('*:%d is greater than the maximum of 65535' % port)
    if a == None:
        a = b = c = d = 0
    return ('%d.%d.%d.%d' % (a,b,c,d), port)


optspec = """
sshuttle [-l [ip:]port] [-r [username@]sshserver[:port]] <subnets...>
sshuttle --server
sshuttle --firewall <port> <subnets...>
sshuttle --hostwatch
--
l,listen=          transproxy to this ip address and port number [127.0.0.1:0]
H,auto-hosts       scan for remote hostnames and update local /etc/hosts
N,auto-nets        automatically determine subnets to route
dns                capture local DNS requests and forward to the remote DNS server
u,udp              forward UDP as well
udp-forward=       Comma-separated list of UDP ports or port ranges to forward back to this machine
python=            path to python interpreter on the remote server
r,remote=          ssh hostname (and optional username) of remote sshuttle server
x,exclude=         exclude this subnet (can be used more than once)
v,verbose          increase debug message verbosity
e,ssh-cmd=         the command to use to connect to the remote [ssh]
seed-hosts=        with -H, use these hostnames for initial scan (comma-separated)
no-latency-control sacrifice latency to improve bandwidth benchmarks
wrap=              restart counting channel numbers after this number (for testing)
D,daemon           run in the background as a daemon
V,version          print sshuttle's version number
syslog             send log messages to syslog (default if you use --daemon)
pidfile=           pidfile name (only if using --daemon) [./sshuttle.pid]
server             (internal use only)
firewall           (internal use only)
hostwatch          (internal use only)
"""
o = options.Options(optspec)
(opt, flags, extra) = o.parse(sys.argv[2:])

if opt.version:
    import version
    print version.TAG
    sys.exit(0)
if opt.daemon:
    opt.syslog = 1
if opt.wrap:
    import ssnet
    ssnet.MAX_CHANNEL = int(opt.wrap)
helpers.verbose = opt.verbose

try:
    if opt.server:
        if len(extra) != 0:
            o.fatal('no arguments expected')
        server.latency_control = opt.latency_control
        sys.exit(server.main())
    elif opt.firewall:
        if len(extra) != 3:
            o.fatal('exactly three arguments expected')
        sys.exit(firewall.main(int(extra[0]), int(extra[1]), int(extra[2]), opt.syslog))
    elif opt.hostwatch:
        sys.exit(hostwatch.hw_main(extra))
    else:
        if len(extra) < 1 and not opt.auto_nets:
            o.fatal('at least one subnet (or -N) expected')
        includes = extra
        excludes = ['127.0.0.0/8']
        for k,v in flags:
            if k in ('-x','--exclude'):
                excludes.append(v)
        remotename = opt.remote
        if remotename == '' or remotename == '-':
            remotename = None
        if opt.seed_hosts and not opt.auto_hosts:
            o.fatal('--seed-hosts only works if you also use -H')
        if opt.seed_hosts:
            sh = re.split(r'[\s,]+', (opt.seed_hosts or "").strip())
        elif opt.auto_hosts:
            sh = []
        else:
            sh = None
        udp_forward = []
        if opt.udp_forward:
            opt.udp = True # Implicitly turn on
            for p in str(opt.udp_forward).split(','):
                if '-' in p:
                    try:
                        range_start, range_end = map(int, p.split('-', 2))
                        if range_start <= 0 or range_start >= 65535 or range_end <= 0 or range_end >= 65535:
                            o.fatal('UDP port values not in acceptable range: ' + p)
                        udp_forward += range(range_start, range_end)
                    except ValueError:
                        o.fatal('invalid UDP port range: ' + p)
                else:
                    try:
                        port = int(p)
                        if port <= 0 or port >= 65535:
                            o.fatal('UDP port value not in acceptable range: ' + p)
                        udp_forward.append(port)
                    except ValueError:
                        o.fatal('invalid port number: ' + p)
        sys.exit(client.main(parse_ipport(opt.listen or '0.0.0.0:0'),
                             opt.ssh_cmd,
                             remotename,
                             opt.python,
                             opt.latency_control,
                             opt.dns,
                             opt.udp,
                             udp_forward,
                             sh,
                             opt.auto_nets,
                             parse_subnets(includes),
                             parse_subnets(excludes),
                             opt.syslog, opt.daemon, opt.pidfile))
except FatalNeedsReboot, e:
    log('You must reboot before using sshuttle.\n')
    sys.exit(EXITCODE_NEEDS_REBOOT)
except Fatal, e:
    log('fatal: %s\n' % e)
    sys.exit(99)
except KeyboardInterrupt:
    log('\n')
    log('Keyboard interrupt: exiting.\n')
    sys.exit(1)
