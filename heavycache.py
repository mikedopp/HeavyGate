"""HeavyGate's shared Gradle build cache: what one machine built, the others take instead of building it again.

One machine (a pool worker that is always on) runs a small HTTP build cache, `heavy cache serve`. It speaks Gradle's
HTTP build cache protocol (GET and PUT /cache/<key>), wants a password (HTTP Basic, user heavygate), keeps at most
cache_max_gb on disk and drops the least recently used entries past that.

It listens on that machine's loopback only (127.0.0.1), never on the LAN, so neither the password nor the outputs
cross the network in the clear: the other machines reach it through ssh, with the keys the pool already uses. On Linux
a systemd socket holds the port and starts the server on the first request; the server stops itself after
cache_idle_min without one, so nothing runs while nobody builds. A pool worker reaches it through its own systemd
socket on 127.0.0.1 that runs `ssh -W` for each connection; this PC through Relay, which the tray runs (one `ssh -W`
per connection, started by the first byte, so a bare connect costs nothing).

Every machine that builds gets one file, ~/.gradle/init.d/heavygate-cache.gradle, which turns the build cache on and
points it at 127.0.0.1, so every Gradle build reads it and adds to it. `heavy cache off` removes that file.

    heavy cache setup NAME     the server on pool worker NAME: socket, service, a new password
    heavy cache on | off       the init file (and the ssh link) on this PC and every pool worker
    heavy cache                entries, size, hits and misses, asleep or awake (asked over ssh: it doesn't wake it)

Standard library only."""
import base64
import hmac
import http.server
import json
import os
import re
import secrets
import shutil
import socket
import socketserver
import subprocess
import tempfile
import threading
import time
import urllib.error
import urllib.request

DEFAULTS = {
    'cache_url': '',          # this machine's client side: http://127.0.0.1:PORT/cache/ ('' = no cache set up)
    'cache_token': '',        # the password, set by heavy cache setup
    'cache_host': '',         # the pool worker that runs it
    'cache_port': 8815,
    'cache_dir': '',          # the server's store; '' = ~/.cache/heavygate-buildcache
    'cache_max_gb': 5,        # least recently used entries go past this
    'cache_entry_max_mb': 256,
    'cache_idle_min': 10,     # the server stops itself this long after its last request (0 = never); its socket stays
}
USER = 'heavygate'
INIT_NAME = 'heavygate-cache.gradle'
UNIT = 'heavygate-cache.service'
SOCKET_UNIT = 'heavygate-cache.socket'
TUNNEL_SOCKET = 'heavygate-cache-tunnel.socket'      # on a machine that reaches it over ssh
TUNNEL_UNIT = 'heavygate-cache-tunnel@.service'
STATS_FILE = '.stats.json'
KEY_RE = re.compile(r'^[0-9a-fA-F]{16,128}$')


# the server -----------------------------------------------------------------------------------------------------------
class Store:
    """Entries as files named by their key, an LRU by mtime (a hit touches it), a size cap."""

    def __init__(self, folder, max_bytes):
        self.dir, self.max = folder, int(max_bytes)
        os.makedirs(folder, exist_ok=True)
        self.lock = threading.Lock()
        self.stats = {'hits': 0, 'misses': 0, 'stored': 0, 'stored_bytes': 0, 'served_bytes': 0, 'evicted': 0,
                      'refused': 0, 'since': time.time()}
        self.stats.update(read_counters(folder))   # the counters outlive the server, which stops when idle
        self.stats['starts'] = self.stats.get('starts', 0) + 1
        self.total = sum(os.path.getsize(os.path.join(folder, f)) for f in os.listdir(folder)
                         if KEY_RE.match(f))
        self.last_used, self.open = time.time(), 0   # the last cache request; connections open now
        self.saved = None

    def used(self):
        self.last_used = time.time()

    def save(self):
        """The counters to STATS_FILE when they changed, for the next start and for host_summary."""
        with self.lock:
            now = dict(self.stats)
        if now == self.saved:
            return
        tmp = os.path.join(self.dir, STATS_FILE + '.tmp')
        try:
            with open(tmp, 'w') as f:
                json.dump(now, f)
            os.replace(tmp, os.path.join(self.dir, STATS_FILE))
            self.saved = now
        except OSError:
            pass

    def path(self, key):
        return os.path.join(self.dir, key.lower())

    def get(self, key):
        p = self.path(key)
        try:
            with open(p, 'rb') as f:
                data = f.read()
            os.utime(p)
        except OSError:
            with self.lock:
                self.stats['misses'] += 1
            return None
        with self.lock:
            self.stats['hits'] += 1
            self.stats['served_bytes'] += len(data)
        return data

    def put(self, key, stream, length):
        fd, tmp = tempfile.mkstemp(dir=self.dir, prefix='.put-')
        try:
            with os.fdopen(fd, 'wb') as f:
                left = length
                while left > 0:
                    b = stream.read(min(left, 1 << 20))
                    if not b:
                        raise OSError('the upload ended early')
                    f.write(b)
                    left -= len(b)
            dest = self.path(key)
            with self.lock:
                old = os.path.getsize(dest) if os.path.exists(dest) else 0
                os.replace(tmp, dest)
                self.total += length - old
                self.stats['stored'] += 1
                self.stats['stored_bytes'] += length
                self.trim()
        except BaseException:
            try:
                os.remove(tmp)
            except OSError:
                pass
            raise

    def trim(self):
        """Past the cap: drop the least recently used entries down to 90% of it. Called holding the lock."""
        if self.total <= self.max:
            return
        files = []
        for f in os.listdir(self.dir):
            if KEY_RE.match(f):
                try:
                    st = os.stat(os.path.join(self.dir, f))
                    files.append((st.st_mtime, st.st_size, f))
                except OSError:
                    pass
        for _, size, f in sorted(files):
            if self.total <= self.max * 0.9:
                break
            try:
                os.remove(os.path.join(self.dir, f))
                self.total -= size
                self.stats['evicted'] += 1
            except OSError:
                pass

    def summary(self):
        with self.lock:
            n = sum(1 for f in os.listdir(self.dir) if KEY_RE.match(f))
            return dict(self.stats, entries=n, bytes=self.total, max_bytes=self.max)


def make_handler(store, token, entry_max):
    want = 'Basic ' + base64.b64encode(f'{USER}:{token}'.encode()).decode()

    class Handler(http.server.BaseHTTPRequestHandler):
        protocol_version = 'HTTP/1.1'
        server_version = 'HeavyGateCache'
        timeout = 60   # a kept-alive connection nobody uses closes, so the server can go idle

        def setup(self):
            super().setup()
            with store.lock:
                store.open += 1

        def finish(self):
            try:
                super().finish()
            finally:
                with store.lock:
                    store.open -= 1

        def log_message(self, *args):   # quiet: systemd's journal gets errors only
            pass

        def reply(self, code, body=b'', ctype='application/octet-stream', head=False):
            self.send_response(code)
            if code == 401:
                self.send_header('WWW-Authenticate', 'Basic realm="HeavyGate build cache"')
            self.send_header('Content-Type', ctype)
            self.send_header('Content-Length', str(len(body)))
            self.end_headers()
            if body and not head:
                self.wfile.write(body)

        def allowed(self, drain=False):
            got = self.headers.get('Authorization') or ''
            if token and hmac.compare_digest(got.encode(), want.encode()):
                return True
            if drain:
                self.drain()
            self.reply(401, b'a password is needed', 'text/plain')
            return False

        def drain(self):
            """Read and drop an upload we won't keep, so the client hears the answer instead of a broken pipe. Past
            the entry limit the connection is closed instead."""
            try:
                left = int(self.headers.get('Content-Length') or 0)
            except ValueError:
                left = 0
            if left > entry_max or self.headers.get('Expect', '').lower() == '100-continue':
                self.close_connection = True
                return
            while left > 0:
                b = self.rfile.read(min(left, 1 << 20))
                if not b:
                    break
                left -= len(b)

        def key(self):
            m = re.match(r'^/cache/([^/?]+)$', self.path)
            return m.group(1) if m and KEY_RE.match(m.group(1)) else None

        def handle_expect_100(self):   # don't take a body we won't keep: ask for the password first
            if not self.allowed():
                return False
            return super().handle_expect_100()

        def do_GET(self, head=False):
            if not self.allowed():
                return
            if self.path == '/stats':
                return self.reply(200, json.dumps(store.summary()).encode(), 'application/json', head)
            k = self.key()
            if not k:
                return self.reply(404, b'', head=head)
            store.used()
            data = store.get(k)
            if data is None:
                return self.reply(404, b'', head=head)
            self.reply(200, data, head=head)

        def do_HEAD(self):
            self.do_GET(head=True)

        def do_PUT(self):
            if not self.allowed(drain=True):
                return
            store.used()
            k = self.key()
            try:
                length = int(self.headers.get('Content-Length') or -1)
            except ValueError:
                length = -1
            if not k or length < 0:
                if length > 0:
                    self.drain()
                else:
                    self.close_connection = True
                return self.reply(400, b'PUT /cache/<hex key> with a Content-Length', 'text/plain')
            if length > entry_max:
                with store.lock:
                    store.stats['refused'] += 1
                left = length   # read it and drop it, so the client hears 413 instead of a broken pipe
                while left > 0:
                    b = self.rfile.read(min(left, 1 << 20))
                    if not b:
                        break
                    left -= len(b)
                return self.reply(413, b'too big for this cache (cache_entry_max_mb)', 'text/plain')
            try:
                store.put(k, self.rfile, length)
            except OSError as e:
                self.close_connection = True
                return self.reply(500, str(e).encode(), 'text/plain')
            self.reply(200)

    return Handler


class Server(socketserver.ThreadingMixIn, http.server.HTTPServer):
    daemon_threads = True
    allow_reuse_address = True


def store_dir(s):
    return s.get('cache_dir') or os.path.join(os.path.expanduser('~'), '.cache', 'heavygate-buildcache')


def read_counters(folder):
    try:
        with open(os.path.join(folder, STATS_FILE)) as f:
            d = json.load(f)
        return {k: v for k, v in d.items() if isinstance(v, (int, float))}
    except (OSError, ValueError, AttributeError):
        return {}


def systemd_socket():
    """The listening socket systemd handed us (socket activation), or None."""
    if os.environ.get('LISTEN_PID') == str(os.getpid()) and int(os.environ.get('LISTEN_FDS') or 0) >= 1:
        return socket.socket(fileno=3)
    return None


def make_server(s, host='127.0.0.1', sock=None):
    """Loopback only unless told otherwise: other machines come in over ssh. sock: an already listening socket."""
    if not s.get('cache_token'):
        raise SystemExit('heavy cache serve: no cache_token in the settings (heavy cache setup NAME makes one)')
    store = Store(store_dir(s), float(s.get('cache_max_gb') or 5) * 1024 ** 3)
    handler = make_handler(store, s['cache_token'], int(s.get('cache_entry_max_mb') or 256) * 1024 * 1024)
    if sock is None:
        port = s.get('cache_port')
        srv = Server((host, 8815 if port in (None, '') else int(port)), handler)   # 0: any free port
    else:
        srv = Server(sock.getsockname()[:2], handler, bind_and_activate=False)
        srv.socket.close()
        srv.socket = sock
        srv.server_address = sock.getsockname()[:2]
    srv.store = store
    return srv


def run(srv, idle_s, every=5.0):
    """Serves until idle_s pass with no cache request and no connection open (0 = forever), saving the counters as
    it goes. Returns 'idle' or 'stopped'."""
    store, why, done = srv.store, ['stopped'], threading.Event()

    def watch():
        while not done.wait(every):
            store.save()
            if idle_s and store.open == 0 and time.time() - store.last_used > idle_s:
                why[0] = 'idle'
                srv.shutdown()
                return
    threading.Thread(target=watch, daemon=True).start()
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        done.set()
        store.save()
        srv.server_close()
    return why[0]


def serve(s, say=print):
    sock = systemd_socket()
    srv = make_server(s, sock=sock)
    idle_s = float(s.get('cache_idle_min') or 0) * 60 if sock else 0   # without a socket to wake it, it stays up
    say(f"HeavyGate build cache on {srv.server_address[0]}:{srv.server_address[1]}"
        f"{' (from its systemd socket)' if sock else ''}, {store_dir(s)}, up to {s.get('cache_max_gb')} GB"
        + (f', stops after {idle_s / 60:g} min idle' if idle_s else ''))
    why = run(srv, idle_s)
    say('stopped: idle; the socket starts it again on the next request' if why == 'idle' else 'stopped')
    return 0


def host_summary(s, need_unit=True):
    """On the server, without waking it: the counters it saved, the store as it is, and whether it is awake.
    None when this machine doesn't serve a cache."""
    if not s.get('cache_token') or (need_unit and not os.path.exists(unit_path(SOCKET_UNIT))):
        return None
    d = store_dir(s)
    st = {'hits': 0, 'misses': 0, 'stored': 0, 'since': time.time()}
    st.update(read_counters(d))
    n = size = 0
    try:
        for e in os.scandir(d):
            if KEY_RE.match(e.name):
                n += 1
                size += e.stat().st_size
    except OSError:
        pass
    awake = None
    if shutil.which('systemctl'):
        awake = _run(['systemctl', '--user', 'is-active', UNIT], timeout=5)[1] == 'active'
    return dict(st, entries=n, bytes=size, max_bytes=float(s.get('cache_max_gb') or 5) * 1024 ** 3, awake=awake)


# the server's systemd unit and firewall (run on the server, over ssh, by heavy cache setup) ------------------------
def unit_path(name=UNIT):
    return os.path.join(os.environ.get('XDG_CONFIG_HOME') or os.path.expanduser('~/.config'), 'systemd', 'user', name)


def _q(p):
    return '"' + p.replace('\\', '\\\\').replace('"', '\\"') + '"'


def unit_text(python, script, version):
    """The service: started by its socket, never at boot, and it ends itself when idle (exit 0: not restarted)."""
    return ('[Unit]\n'
            f'Description=HeavyGate {version}: shared Gradle build cache (started by {SOCKET_UNIT})\n'
            f'Requires={SOCKET_UNIT}\n'
            f'After={SOCKET_UNIT}\n\n'
            '[Service]\n'
            f'ExecStart={_q(python)} {_q(script)} cache serve\n'
            'Restart=on-failure\n'
            'RestartSec=5\n'
            'Nice=10\n')


def socket_text(port, version):
    return ('[Unit]\n'
            f'Description=HeavyGate {version}: the build cache port, loopback only (ssh brings the other machines)\n\n'
            '[Socket]\n'
            f'ListenStream=127.0.0.1:{int(port)}\n\n'
            '[Install]\n'
            'WantedBy=sockets.target\n')


def tunnel_argv(ssh, target, port):
    """One connection to the cache through ssh: stdin and stdout become the server's 127.0.0.1:PORT."""
    return list(ssh) + ['-q', '-o', 'BatchMode=yes', '-o', 'ConnectTimeout=8', '-o', 'ServerAliveInterval=15',
                        '-o', 'ServerAliveCountMax=3', '-W', f'127.0.0.1:{int(port)}'] + list(target)


def tunnel_texts(ssh_exe, target, port, version):
    """A worker's link: a socket on its own 127.0.0.1:PORT, and one ssh -W per connection (nothing runs between)."""
    sock = ('[Unit]\n'
            f'Description=HeavyGate {version}: the shared build cache on {target[-1]}, through ssh\n\n'
            '[Socket]\n'
            f'ListenStream=127.0.0.1:{int(port)}\n'
            'Accept=yes\n\n'
            '[Install]\n'
            'WantedBy=sockets.target\n')
    unit = ('[Unit]\n'
            f'Description=HeavyGate {version}: one connection to the build cache on {target[-1]}\n\n'
            '[Service]\n'
            'ExecStart=' + ' '.join(_q(x) for x in tunnel_argv([ssh_exe], target, port)) + '\n'
            'StandardInput=socket\n'
            'StandardOutput=socket\n'
            'StandardError=journal\n')
    return sock, unit


def _run(cmd, timeout=30):
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        return r.returncode, (r.stdout + r.stderr).strip()
    except (OSError, subprocess.TimeoutExpired) as e:
        return 127, str(e)


def lan_networks():
    """This machine's IPv4 networks (a home LAN's /24, say), from ip -o -4 addr."""
    import ipaddress
    code, out = _run(['ip', '-o', '-4', 'addr', 'show', 'scope', 'global'])
    nets = []
    for m in re.finditer(r'inet (\d+\.\d+\.\d+\.\d+/\d+)', out if code == 0 else ''):
        iface = ipaddress.ip_interface(m.group(1))
        if iface.ip.is_private:
            nets.append((str(iface.ip), str(iface.network)))
    return nets


def host_install(cfg, python, script, version, set_setting):
    """On the server: settings, the socket and the service, and any old LAN firewall rule for the port taken away
    (it listens on loopback now). {'ok', 'steps': [...], 'port'}."""
    steps = []
    port = int(cfg.get('port') or 8815)
    for k, v in (('cache_token', cfg['token']), ('cache_port', port), ('cache_max_gb', cfg.get('max_gb') or 5),
                 ('cache_idle_min', cfg.get('idle_min', 10))):
        set_setting(k, v)
    os.makedirs(os.path.dirname(unit_path()), exist_ok=True)
    for name, text in ((UNIT, unit_text(python, script, version)), (SOCKET_UNIT, socket_text(port, version))):
        with open(unit_path(name), 'w') as f:
            f.write(text)
        steps.append(f'wrote {unit_path(name)}')
    ok = True
    _run(['systemctl', '--user', 'disable', UNIT])   # 1.24.0 started the service at boot; the socket does now
    for cmd in (['systemctl', '--user', 'daemon-reload'], ['systemctl', '--user', 'stop', UNIT],
                ['systemctl', '--user', 'enable', SOCKET_UNIT], ['systemctl', '--user', 'restart', SOCKET_UNIT]):
        code, out = _run(cmd)
        steps.append(f"{' '.join(cmd[1:])}: {'ok' if code == 0 else out or code}")
        ok = ok and code == 0
    steps += close_lan_port(port)
    code, out = _run(['systemctl', '--user', 'is-active', SOCKET_UNIT])
    steps.append(f'socket: {out} (the service starts on the first request)')
    return {'ok': ok and out == 'active', 'steps': steps, 'port': port}


def close_lan_port(port):
    """Takes away the ufw rules 1.24.0 opened for the port on each LAN (by their comment), if sudo needs no password."""
    if not shutil.which('ufw'):
        return []
    if _run(['sudo', '-n', 'true'])[0] != 0:
        return [f'sudo needs a password: check `sudo ufw status` for an old rule for port {port} and delete it']
    code, out = _run(['sudo', '-n', 'ufw', 'status', 'numbered'])
    nums = [int(m.group(1)) for m in re.finditer(r'^\[\s*(\d+)\]\s+%d/tcp\b.*HeavyGate build cache' % port, out, re.M)]
    for n in sorted(nums, reverse=True):   # highest first: numbers shift as rules go
        _run(['sudo', '-n', 'ufw', '--force', 'delete', str(n)])
    return [f'ufw: took away {len(nums)} LAN rule(s) for port {port}' if nums else f'ufw: no LAN rule for port {port}']


def host_remove():
    steps = []
    for cmd in (['systemctl', '--user', 'disable', '--now', SOCKET_UNIT],
                ['systemctl', '--user', 'disable', '--now', UNIT]):
        code, out = _run(cmd)
        steps.append(f"{' '.join(cmd[1:])}: {'ok' if code == 0 else out or code}")
    for name in (UNIT, SOCKET_UNIT):
        try:
            os.remove(unit_path(name))
            steps.append(f'removed {unit_path(name)}')
        except OSError:
            pass
    _run(['systemctl', '--user', 'daemon-reload'])
    return {'ok': True, 'steps': steps}


# the client side: one init script per Gradle home -------------------------------------------------------------------
def gradle_home():
    return os.environ.get('GRADLE_USER_HOME') or os.path.join(os.path.expanduser('~'), '.gradle')


def init_path():
    return os.path.join(gradle_home(), 'init.d', INIT_NAME)


def init_script(url, token):
    g = lambda v: "'" + v.replace('\\', '\\\\').replace("'", "\\'") + "'"
    return (f'// HeavyGate shared build cache ({url}). `heavy cache off` removes this file.\n'
            '// Every Gradle build on this machine reads it and adds what it builds. One build without it:\n'
            '// HEAVYGATE_CACHE=off in the environment, or -Dheavygate.cache=off.\n'
            "if (System.getenv('HEAVYGATE_CACHE') != 'off' && System.getProperty('heavygate.cache') != 'off') {\n"
            'gradle.startParameter.buildCacheEnabled = true\n'
            'gradle.settingsEvaluated { settings ->\n'
            '    settings.buildCache {\n'
            '        remote(HttpBuildCache) {\n'
            f'            url = {g(url)}\n'
            '            allowInsecureProtocol = true\n'
            '            push = true\n'
            '            credentials {\n'
            f'                username = {g(USER)}\n'
            f'                password = {g(token)}\n'
            '            }\n'
            '        }\n'
            '    }\n'
            '}\n'
            '}\n')


def client_on(url, token):
    p = init_path()
    os.makedirs(os.path.dirname(p), exist_ok=True)
    fd = os.open(p, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, 'w', encoding='utf-8', newline='\n') as f:
        f.write(init_script(url, token))
    return p


def link_on(target, port, version):
    """On a Linux worker that is not the server: the ssh link as a systemd socket. {'ok', 'steps'}."""
    ssh = shutil.which('ssh')
    if not ssh or not shutil.which('systemctl'):
        return {'ok': False, 'steps': ['needs ssh and systemd (systemctl --user)']}
    code, out = _run([ssh, '-o', 'BatchMode=yes', '-o', 'ConnectTimeout=8'] + list(target) + ['true'], timeout=20)
    if code != 0:
        return {'ok': False, 'steps': [f"can't ssh to {target[-1]} with this machine's own key: {out or code}"]}
    sock, unit = tunnel_texts(ssh, target, port, version)
    os.makedirs(os.path.dirname(unit_path()), exist_ok=True)
    for name, text in ((TUNNEL_SOCKET, sock), (TUNNEL_UNIT, unit)):
        with open(unit_path(name), 'w') as f:
            f.write(text)
    steps, ok = [f'ssh to {target[-1]}: ok'], True
    for cmd in (['systemctl', '--user', 'daemon-reload'], ['systemctl', '--user', 'enable', TUNNEL_SOCKET],
                ['systemctl', '--user', 'restart', TUNNEL_SOCKET]):
        code, out = _run(cmd)
        steps.append(f"{' '.join(cmd[1:])}: {'ok' if code == 0 else out or code}")
        ok = ok and code == 0
    return {'ok': ok, 'steps': steps}


def link_off():
    if not os.path.exists(unit_path(TUNNEL_SOCKET)):
        return False
    _run(['systemctl', '--user', 'disable', '--now', TUNNEL_SOCKET])
    for name in (TUNNEL_SOCKET, TUNNEL_UNIT):
        try:
            os.remove(unit_path(name))
        except OSError:
            pass
    _run(['systemctl', '--user', 'daemon-reload'])
    return True


def client_off():
    link_off()
    try:
        os.remove(init_path())
        return True
    except FileNotFoundError:
        return False


class Relay:
    """This PC's link (no systemd here): listens on 127.0.0.1:PORT and runs argv (ssh -W, see tunnel_argv) for each
    connection, started by its first byte, so a connect that sends nothing starts no ssh and wakes nothing."""

    def __init__(self, argv, port, note=lambda m: None, flags=0):
        self.argv, self.port, self.note, self.flags = list(argv), int(port), note, flags
        self.sock, self.stopped = None, threading.Event()
        self.started = self.failed = self.open = 0

    def start(self):
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        if hasattr(socket, 'SO_EXCLUSIVEADDRUSE'):
            s.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        try:
            s.bind(('127.0.0.1', self.port))   # OSError when something else holds the port
            s.listen(16)
        except OSError:
            s.close()
            raise
        self.sock = s
        threading.Thread(target=self.accept, daemon=True).start()
        return self

    def stop(self):
        self.stopped.set()
        if self.sock:
            try:
                self.sock.close()
            except OSError:
                pass

    def accept(self):
        while not self.stopped.is_set():
            try:
                c, _ = self.sock.accept()
            except OSError:
                return
            threading.Thread(target=self.one, args=(c,), daemon=True).start()

    def one(self, c):
        p = None
        try:
            c.settimeout(300)
            first = c.recv(65536)
            if not first:
                return
            try:
                p = subprocess.Popen(self.argv, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                     stderr=subprocess.DEVNULL, bufsize=0, creationflags=self.flags)
            except OSError as e:
                self.failed += 1
                self.note(f'build cache link: could not start ssh: {e}')
                return
            self.started += 1
            self.open += 1
            p.stdin.write(first)

            def up():
                try:
                    while True:
                        b = c.recv(65536)
                        if not b:
                            break
                        p.stdin.write(b)
                except (OSError, ValueError):
                    pass
                finally:
                    try:
                        p.stdin.close()
                    except OSError:
                        pass
            threading.Thread(target=up, daemon=True).start()
            while True:
                b = p.stdout.read(65536)
                if not b:
                    break
                c.sendall(b)
        except (OSError, ValueError):
            pass
        finally:
            if p is not None:
                self.open -= 1
                try:
                    p.kill()
                except OSError:
                    pass
                p.wait()
                for f in (p.stdin, p.stdout):
                    try:
                        f.close()
                    except OSError:
                        pass
            try:
                c.close()
            except OSError:
                pass


def client_state():
    """'on' (with the url), 'off', or 'other' (an init file that points somewhere else)."""
    try:
        with open(init_path(), encoding='utf-8') as f:
            m = re.search(r"url = '([^']+)'", f.read())
        return ('on', m.group(1)) if m else ('other', None)
    except OSError:
        return 'off', None


def stats(url, token, timeout=3):
    """The server's counters, or {'error': why}."""
    base = re.sub(r'/cache/?$', '', url.rstrip('/'))
    req = urllib.request.Request(base + '/stats', headers={
        'Authorization': 'Basic ' + base64.b64encode(f'{USER}:{token}'.encode()).decode()})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.loads(r.read().decode())
    except urllib.error.HTTPError as e:
        return {'error': f'HTTP {e.code}' + (' (wrong password)' if e.code == 401 else '')}
    except (OSError, ValueError) as e:
        return {'error': str(getattr(e, 'reason', e))}


def new_token():
    return secrets.token_urlsafe(24)


def fmt_bytes(n):
    n = float(n or 0)
    for unit in ('B', 'KB', 'MB', 'GB'):
        if n < 1024 or unit == 'GB':
            return f'{n:.0f} {unit}' if unit in ('B', 'KB') else f'{n:.1f} {unit}'
        n /= 1024


def stats_line(st):
    """'1,204 entries, 0.8 of 5.0 GB; 312 hits, 40 misses (89%) since 11-22 14:02' for status and the tray."""
    if st.get('error'):
        return f"unreachable ({st['error']})"
    looked = st['hits'] + st['misses']
    rate = f" ({100 * st['hits'] / looked:.0f}% hits)" if looked else ''
    awake = {True: 'awake, ', False: 'asleep, '}.get(st.get('awake'), '')
    return (f"{awake}{st['entries']:,} entries, {fmt_bytes(st['bytes'])} of {fmt_bytes(st['max_bytes'])}; "
            f"{st['hits']:,} hits, {st['misses']:,} misses{rate}, {st['stored']:,} stored since "
            f"{time.strftime('%m-%d %H:%M', time.localtime(st['since']))}")
