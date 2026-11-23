"""HeavyGate's pool: your other machines as more cpu lanes, over SSH.

    heavy pool                          each worker, asked now: reachable, its cpu lane, free RAM, what it can build
    heavy pool add NAME USER@HOST [--key FILE] [--port N] [--reserve-mb N] [--heavy CMD]
    heavy pool remove NAME
    heavy pool auto on|off              on: jobs of pool_kinds (Gradle) go to a ready worker instead of running here
    heavy cpu --pool[=NAME] -- ...      send this one job (to NAME, or to the worker with the most room)
    heavy cpu --no-pool -- ...          keep this one job here

A worker needs key-only SSH, python3 and HeavyGate 1.20 or newer, plus the job's tools (Java, and the Android SDK for
an Android build). Nothing else runs there: the job goes into the worker's own cpu lane, which queues it, runs it at
low priority and, under systemd, caps its RAM so the worker keeps pool_reserve_mb for its own services.

A job's trip: the project (its whole git checkout, or the folder when there is none) is copied to
~/.cache/heavygate-pool/<project> on the worker, sending only the files that changed since the last trip. The command
runs there with its output streamed here. Files it made that match pool_outputs (Gradle: build/outputs) come back and
are checked against their SHA256. Files git ignores are never sent (signing keys, keystores, config with secrets), nor
local.properties, which names this PC's SDK. Nothing goes back to the worker but the source. Standard library only."""
import fnmatch
import hashlib
import io
import json
import os
import posixpath
import re
import shlex
import shutil
import signal
import subprocess
import sys
import tarfile
import tempfile
import threading
import time

DEFAULTS = {
    'pool_auto': False,                         # jobs of pool_kinds go to a ready worker (heavy pool auto on|off)
    'pool_kinds': ['gradle'],                   # what auto mode sends: gradle, dotnet, cargo
    'pool_reserve_mb': 2048,                    # RAM a worker always keeps for its own services (per worker: --reserve-mb)
    'pool_expect_mb': {'gradle': 2500, 'dotnet': 1500, 'cargo': 2500},   # free RAM a worker needs to take a job
    'pool_outputs': {'gradle': ['build/outputs/**', 'build/install/**', 'build/libs/**'], 'dotnet': ['bin/**'], 'cargo': ['target/release/*', 'target/debug/*'],
                     'any': []},                # what comes back; 'any' is for a --pool job that is none of those
    # APKs that come back are signed again here with the key a local build uses (heavysign.py), so a worker's build
    # installs over yours. Off: a worker's APK keeps the worker's own debug key, and auto mode keeps release builds here.
    'pool_resign': True,
    'pool_java': '',                            # the JDK apksigner runs with; '' = JAVA_HOME or javac's JDK
    # Auto mode keeps a job here when a word of it contains one of these: publish uploads from this PC, and
    # install/connected/device tasks need the phone plugged in here. Without pool_resign, release, sign and bundle too.
    'pool_auto_skip': ['publish', 'install', 'connected', 'device'],
    'pool_ssh_timeout_s': 8,
    'pool_max_send_mb': 400,                    # a trip that would send more stays here
}
ROOT = '.cache/heavygate-pool'                  # under the worker's home
META = '.heavypool-meta.json'
MANIFEST = '.heavypool.json'
START = '.heavypool-start'
PIDFILE = '.heavypool-run.pid'
SUMS = '.heavypool-sha256.json'
OWN = {META, MANIFEST, START, PIDFILE, SUMS}
SKIP_DIRS = {'.git', '.gradle', 'build', 'node_modules', '.idea', '.vs', 'bin', 'obj', 'dist', 'out', 'target',
             '.venv', 'venv', '__pycache__', '.cxx', '.externalNativeBuild'}
SKIP_FILES = {'local.properties'}
# Never sent, even when git tracks them: signing keys and the files that point at them or hold secrets.
SECRETS = ('*.jks', '*.keystore', '*.p12', '*.pfx', '*.pem', '*.key', '*.snk', 'keystore.properties',
           'key.properties', 'signing.properties', '.env', '.env.*', 'secrets.json', 'credentials.json')
EXEC = {'gradlew', 'mvnw'}
KINDS = {'gradlew': 'gradle', 'gradlew.bat': 'gradle', 'gradlew.cmd': 'gradle', 'gradle': 'gradle',
         'gradle.bat': 'gradle', 'dotnet': 'dotnet', 'dotnet.exe': 'dotnet', 'cargo': 'cargo', 'cargo.exe': 'cargo'}
NO_WINDOW = 0x08000000 if os.name == 'nt' else 0
KEY_RE = re.compile(r'^[A-Za-z0-9._-]{1,120}$')


class PoolError(Exception):
    """Something went wrong before the job started on the worker: it can still run here."""


# the worker list ------------------------------------------------------------------------------------------------------
def pool_path(home):
    return os.path.join(home, 'pool.json')


def load(home):
    try:
        with open(pool_path(home), encoding='utf-8') as f:
            d = json.load(f)
    except (OSError, ValueError):
        d = {}
    if not isinstance(d.get('workers'), dict):
        d['workers'] = {}
    return d


def save(home, d):
    os.makedirs(home, exist_ok=True)
    tmp = pool_path(home) + '.tmp'
    with open(tmp, 'w', encoding='utf-8') as f:
        json.dump(d, f, indent=2)
    os.replace(tmp, pool_path(home))


def add(home, name, host, key=None, port=None, reserve_mb=None, heavy=None, os_name=None):
    if not KEY_RE.match(name or ''):
        raise PoolError(f'a worker name is letters, digits, dot, dash or underscore, not {name!r}')
    if not host or any(c.isspace() for c in host) or host.startswith('-'):
        raise PoolError(f'not a host: {host!r} (USER@HOST, or a Host from your ssh config)')
    d = load(home)
    w = {'host': host}
    if key:
        w['key'] = os.path.abspath(os.path.expanduser(key))
    if port:
        w['port'] = int(port)
    if reserve_mb is not None:
        w['reserve_mb'] = int(reserve_mb)
    if heavy:
        w['heavy'] = heavy
    if os_name:
        w['os'] = os_name
    d['workers'][name] = w
    save(home, d)
    return w


def install(w, s, folder):
    """Copies HeavyGate's modules into ~/HeavyGate on the worker, as a tar on ssh's stdin unpacked by its own python
    (a Windows worker has no git checkout of it). Returns its `--version` line."""
    names = sorted(f for f in os.listdir(folder) if re.match(r'^heavy\w*\.pyw?$', f))   # .pyw: the tray and rules window
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode='w', format=tarfile.PAX_FORMAT) as t:
        for f in names:
            t.add(os.path.join(folder, f), arcname=f)
    buf.seek(0)
    py = shlex.split(w.get('heavy') or (WIN_HEAVY if is_windows(w) else 'python3'))[0]
    code = ("import sys,tarfile;t=tarfile.open(fileobj=sys.stdin.buffer,mode='r|');"
            "t.extractall('HeavyGate',filter='data')")
    with tempfile.TemporaryFile() as f:
        f.write(buf.getvalue())
        f.seek(0)
        r = subprocess.run(ssh_base(w, s) + [remote_line(w, [py, '-c', code])], stdin=f, capture_output=True,
                           text=True, timeout=120, creationflags=NO_WINDOW)
    if r.returncode != 0:
        raise PoolError(f'copying HeavyGate there failed (exit {r.returncode}): {last_line(r.stderr) or last_line(r.stdout)}')
    code, out, err = run_ssh(w, ['--version'], s, timeout=60)
    if code != 0:
        raise PoolError(f'HeavyGate is there but did not start: {last_line(err) or last_line(out) or code}')
    return f'{len(names)} files; {last_line(out)}'


def remove(home, name):
    d = load(home)
    if d['workers'].pop(name, None) is None:
        return False
    save(home, d)
    return True


# SSH ------------------------------------------------------------------------------------------------------------------
def ssh_exe():
    alt = os.environ.get('HEAVYGATE_SSH')   # tests, or another client: parts split on |
    return alt.split('|') if alt else ['ssh']


def is_windows(w):
    return (w or {}).get('os') == 'windows'


WIN_HEAVY = 'python HeavyGate/heavygate.py'   # where `heavy pool install` puts it, relative to the worker's home


def heavy_argv(w):
    # relative to the worker's home, where ssh starts
    return shlex.split(w.get('heavy') or (WIN_HEAVY if is_windows(w) else '.local/bin/heavy'))


CMD_SPECIAL = set(' \t&|<>^()')


def cmd_quote(a):
    """One argument for a Windows worker, whose OpenSSH hands the command line to cmd.exe /c, and then to the program's
    own (Microsoft C runtime) parser. Inside double quotes cmd.exe leaves & | < > ^ ( ) alone; % and " can't be passed
    safely, so a job that needs them runs here."""
    if any(c in a for c in '%"\r\n'):
        raise PoolError(f"can't pass {a!r} to a Windows worker (cmd.exe would change it); it runs here")
    if a and not (set(a) & CMD_SPECIAL):
        return a
    tail = len(a) - len(a.rstrip('\\'))   # backslashes before the closing quote are doubled
    return '"' + a + '\\' * tail + '"'


def remote_line(w, args):
    return ' '.join(cmd_quote(x) for x in args) if is_windows(w) else shlex.join(args)


# Windows' sshd drops a login when several start at once (MaxStartups: "banner exchange ... connection abort"), and
# under load it can refuse a good key once ("Permission denied (publickey)"). Both happen before the remote command
# starts, so trying again is safe; one dropped login no longer marks a worker unreachable or fails a trip.
LOGIN_FAILS = ('banner exchange', 'kex_exchange_identification', 'connection reset', 'connection closed by',
               'connection abort', 'permission denied (publickey')
LOGIN_TRIES = 3


def login_failed(code, err):
    """ssh's own failure (255) while logging in, not the remote command's."""
    e = (err or '').lower()
    return code == 255 and any(m in e for m in LOGIN_FAILS)


def only_login_lines(text):
    """Nothing came back but ssh's own login complaint: the remote command never started."""
    lines = [x.strip() for x in (text or '').splitlines() if x.strip()]
    return bool(lines) and all(any(m in x.lower() for m in LOGIN_FAILS) or x.lower().startswith(('ssh:', 'ssh_'))
                               for x in lines)


def login_wait(attempt):
    time.sleep(0.7 * (attempt + 1) + (os.getpid() % 7) / 10)   # spread so parallel callers don't retry in step


_SECURED = set()


def key_too_open(icacls_out, me, path=''):
    """The accounts icacls lists on a key file that Windows' OpenSSH would refuse it for: anyone but its owner
    (`me`, DOMAIN\\user), SYSTEM and Administrators. An account deleted from Windows shows as a bare SID and counts."""
    bad = []
    for line in (icacls_out or '').splitlines():
        if path and line.lower().startswith(path.lower()):   # the first line starts with the file's path
            line = line[len(path):]
        if ':(' not in line:
            continue
        who = line.split(':(')[0].strip()
        if who.lower() == me.lower() or who.upper().endswith(('\\SYSTEM', '\\ADMINISTRATORS')):
            continue
        bad.append(who)
    return bad


def secure_key(path):
    """Windows' own ssh (System32\\OpenSSH, the one the tray finds) ignores a key file other accounts can read: "bad
    permissions ... This private key will be ignored", then "Permission denied (publickey)", so the worker shows as
    unreachable. Git's ssh doesn't check, so it works from a Git shell and fails from the tray. A key file inherits its
    folder's permissions, and a profile carried over from an older Windows install often holds a deleted account's
    SID there. Once per key per process: if anyone else is listed, the file keeps only its owner and SYSTEM."""
    if os.name != 'nt' or not path or path in _SECURED or not os.path.isfile(path):
        return
    _SECURED.add(path)
    me = f"{os.environ.get('USERDOMAIN') or os.environ.get('COMPUTERNAME', '')}\\{os.environ.get('USERNAME', '')}"
    def icacls(*args):
        return subprocess.run(['icacls', path, *args], capture_output=True, text=True, timeout=15,
                              creationflags=NO_WINDOW).stdout
    try:
        bad = key_too_open(icacls(), me, path)
        if not bad:
            return
        icacls('/grant:r', f'{me}:F', '*S-1-5-18:F')    # owner and SYSTEM first, so the file is never unreadable
        icacls('/inheritance:r')                        # drops what came from the folder
        for who in key_too_open(icacls(), me, path):    # and what was granted on the file itself
            icacls('/remove', ('*' + who) if who.upper().startswith('S-1-') else who)
        left = key_too_open(icacls(), me, path)
        print(f'heavy pool: {os.path.basename(path)} could be read by {", ".join(bad)}, and Windows\' ssh refuses a '
              f'key like that; ' + (f'it now has only {me} and SYSTEM' if not left else
                                    f'could not remove {", ".join(left)}: fix it with icacls'), file=sys.stderr)
    except (OSError, subprocess.TimeoutExpired):
        pass


def ssh_base(w, s):
    t = int(s.get('pool_ssh_timeout_s', 8))
    cmd = ssh_exe() + ['-o', 'BatchMode=yes', '-o', f'ConnectTimeout={t}', '-o', 'ServerAliveInterval=15',
                       '-o', 'ServerAliveCountMax=4']
    if w.get('key'):
        secure_key(w['key'])
        cmd += ['-i', w['key']]
    if w.get('port'):
        cmd += ['-p', str(w['port'])]
    return cmd + [w['host']]


def ssh_cmd(w, remote, s):
    return ssh_base(w, s) + [remote_line(w, heavy_argv(w) + list(remote))]


def detect_os(w, s):
    """'windows' or 'posix' from the worker's own shell (`ver` in cmd.exe names Windows; elsewhere it is not a command), or raises."""
    try:
        r = subprocess.run(ssh_base(w, s) + ['ver'], capture_output=True, text=True,
                           timeout=int(s.get('pool_ssh_timeout_s', 8)) + 20, creationflags=NO_WINDOW,
                           stdin=subprocess.DEVNULL)
    except (OSError, subprocess.TimeoutExpired) as e:
        raise PoolError(f'could not reach it: {e}')
    if r.returncode == 255:
        raise PoolError('unreachable: ' + (last_line(r.stderr) or 'ssh failed'))
    out = r.stdout + r.stderr
    if 'Microsoft Windows' in out:
        return 'windows'
    return 'posix'


def run_ssh(w, remote, s, timeout=60, stdin=None):
    """(exit, stdout, stderr). 255 is ssh's own failure (unreachable, refused key); 124 a timeout. A dropped login is
    tried again (LOGIN_TRIES)."""
    for attempt in range(LOGIN_TRIES):
        code, out, err = _run_ssh_once(w, remote, s, timeout, stdin)
        if not login_failed(code, err) or attempt == LOGIN_TRIES - 1:
            return code, out, err
        if stdin is not None:
            try:
                stdin.seek(0)
            except (AttributeError, OSError, ValueError):
                return code, out, err
        login_wait(attempt)
    return code, out, err


def _run_ssh_once(w, remote, s, timeout, stdin):
    with tempfile.TemporaryFile() as out, tempfile.TemporaryFile() as err:
        try:
            p = subprocess.Popen(ssh_cmd(w, remote, s), stdin=stdin if stdin is not None else subprocess.DEVNULL,
                                 stdout=out, stderr=err, creationflags=NO_WINDOW)
        except OSError as e:
            return 255, '', f'could not start ssh: {e}'
        try:
            code = p.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            p.kill()
            p.wait()
            code = 124
        out.seek(0)
        err.seek(0)
        return (code, out.read().decode('utf-8', 'replace'), err.read().decode('utf-8', 'replace')
                + (f'\ntimed out after {timeout} s' if code == 124 else ''))


def last_line(text):
    lines = [x.strip() for x in (text or '').splitlines() if x.strip()]
    return lines[-1] if lines else ''


def info(w, s):
    """What the worker says about itself (worker_info), or {'ok': False, 'why': ...}."""
    code, out, err = run_ssh(w, ['pool-worker', 'info'], s, timeout=int(s.get('pool_ssh_timeout_s', 8)) + 30)
    if code == 255:
        return {'ok': False, 'why': 'unreachable: ' + (last_line(err) or 'ssh failed')}
    if code == 124:
        return {'ok': False, 'why': 'no answer in time'}
    if code != 0:
        e = last_line(err) or last_line(out)
        if 'invalid choice' in e or 'not found' in e or 'No such file' in e:
            return {'ok': False, 'why': f'no pool support there (needs HeavyGate 1.20+ at {" ".join(heavy_argv(w))}): {e}'}
        return {'ok': False, 'why': f'exit {code}: {e}'}
    try:
        r = json.loads(last_line(out))
    except ValueError:
        return {'ok': False, 'why': 'answered with something that is not JSON: ' + last_line(out)[:120]}
    r['ok'] = True
    return r


def check(home, s, names=None):
    """Asks every worker (or names) at once; keeps the answers in pool.json for status. {name: info}."""
    d = load(home)
    ws = {n: w for n, w in d['workers'].items() if names is None or n in names}
    found = {}

    def one(n, w):
        found[n] = info(w, s)

    threads = [threading.Thread(target=one, args=(n, w), daemon=True) for n, w in ws.items()]
    for t in threads:
        t.start()
    for t in threads:
        t.join(int(s.get('pool_ssh_timeout_s', 8)) + 45)
    d = load(home)   # again: another heavy may have written meanwhile
    for n, r in found.items():
        if n in d['workers']:
            d['workers'][n]['info'] = dict(r, checked=time.strftime('%Y-%m-%dT%H:%M:%S'), checked_ts=time.time())
    save(home, d)
    return {n: found.get(n, {'ok': False, 'why': 'no answer in time'}) for n in ws}


def room_mb(w, inf, s):
    reserve = int(w.get('reserve_mb', s['pool_reserve_mb']))
    return int(inf.get('mem_free_mb') or 0) - reserve


def judge(w, inf, s, kind, need_sdk, expect):
    """(can take it at all, can start it now, why not). 'Now' means a free cpu slot, nobody queued and the RAM."""
    if not inf.get('ok'):
        return False, False, inf.get('why') or 'not checked'
    if kind and kind not in inf.get('kinds', []):
        return False, False, f"can't build {kind} (" + {'gradle': 'no Java', 'dotnet': 'no dotnet',
                                                         'cargo': 'no cargo'}.get(kind, 'missing tools') + ')'
    if need_sdk and not inf.get('android_sdk'):
        return False, False, 'no Android SDK (ANDROID_HOME or ~/android-sdk)'
    if float(inf.get('disk_free_gb') or 0) < float(inf.get('disk_floor_gb') or 0):
        return False, False, f"only {inf['disk_free_gb']:.1f} GB free there (its floor is {inf['disk_floor_gb']:g} GB)"
    cpu = inf.get('cpu') or {}
    if cpu.get('running', 0) >= cpu.get('slots', 1) or cpu.get('queued', 0):
        return True, False, f"its cpu lane is busy ({cpu.get('running', 0)} running, {cpu.get('queued', 0)} queued)"
    room = room_mb(w, inf, s)
    if room < expect:
        return True, False, (f"{inf.get('mem_free_mb', 0) / 1024:.1f} GB free, keeps "
                             f"{int(w.get('reserve_mb', s['pool_reserve_mb'])) / 1024:.1f} GB, the job needs "
                             f"{expect / 1024:.1f} GB")
    return True, True, ''


def pick(home, s, kind, need_sdk, expect, want=None):
    """(name, worker, info, {name: why not}). Auto mode (want None) takes only a worker that can start it now, the one
    with the most room. --pool (want '') or --pool=NAME (want NAME) also takes a busy worker: the job queues there."""
    d = load(home)
    if want and want not in d['workers']:
        raise PoolError(f"no worker named {want} (heavy pool lists them; heavy pool add NAME USER@HOST)")
    names = [want] if want else list(d['workers'])
    infos = check(home, s, names)
    best, why = None, {}
    for n in names:
        w, inf = d['workers'][n], infos[n]
        can, now, reason = judge(w, inf, s, kind, need_sdk, expect)
        if not can or (want is None and not now):
            why[n] = reason
            continue
        score = (now, room_mb(w, inf, s))
        if best is None or score > best[0]:
            best = (score, n)
        if not now:
            why[n] = reason
    if best is None:
        return None, None, None, why
    n = best[1]
    return n, d['workers'][n], infos[n], why


# what runs there ------------------------------------------------------------------------------------------------------
def tokens_of(command):
    """The job's words. One string (as an agent's hook passes it) is split POSIX-style with backslashes made slashes."""
    command = list(command)
    if len(command) == 1:
        return shlex.split(command[0].replace('\\', '/'))
    return command


def strip_wrappers(tokens):
    t = list(tokens)
    while t and t[0].lower() in ('cmd', 'cmd.exe', '/c', 'call'):
        t = t[1:]
    return t


OUT_FLAGS = {'dotnet': ('-o', '--output'), 'cargo': ('--target-dir', '--artifact-dir')}


def out_patterns(tokens, kind, cwd, root):
    """(patterns, outside): what to bring back from an output folder the command names (dotnet -o DIR, cargo
    --target-dir DIR), as patterns from the project's root, and the named folders outside the project, which a trip
    can't bring back (so auto mode keeps that job here)."""
    t, dirs = strip_wrappers(tokens), []
    for i, x in enumerate(t):
        for f in OUT_FLAGS.get(kind) or ():
            if x == f and i + 1 < len(t):
                dirs.append((f, t[i + 1]))
            elif x.startswith(f + '=') and f.startswith('--'):
                dirs.append((f, x.split('=', 1)[1]))
    pats, outside = [], []
    for f, d in dirs:
        full = os.path.normpath(os.path.join(cwd, d))
        rel = os.path.relpath(full, root).replace(os.sep, '/') if os.path.splitdrive(full)[0].lower() == \
            os.path.splitdrive(root)[0].lower() else '..'
        if rel == '..' or rel.startswith('../'):
            outside.append(d)
        elif f == '--target-dir':   # cargo's own layout: only the finished programs, not the incremental files
            pats += [f'{rel}/release/*', f'{rel}/debug/*']
        else:
            pats.append(f'{rel}/**' if rel != '.' else '**')
    return pats, outside


def kind_of(tokens):
    t = strip_wrappers(tokens)
    return KINDS.get(posixpath.basename(t[0].replace('\\', '/')).lower()) if t else None


def for_linux(tokens, kind=None):
    """The same command as a Linux worker runs it: gradlew.bat -> ./gradlew (and --no-daemon, so no daemon sits on the
    worker's RAM afterwards), dotnet.exe -> dotnet."""
    t = strip_wrappers(tokens)
    if not t:
        return t
    head = t[0].replace('\\', '/')
    base = posixpath.basename(head).lower()
    if base.startswith('gradlew'):
        d = posixpath.dirname(head)
        t[0] = f'{d}/gradlew' if d and d != '.' else './gradlew'
    elif base in KINDS and '.' in base:   # dotnet.exe, gradle.bat: the Linux tool has no extension
        t[0] = base.rsplit('.', 1)[0]
    if (kind or kind_of(tokens)) == 'gradle' and not {'--no-daemon', '--daemon'} & set(t):
        t.append('--no-daemon')
    return t


def for_worker(tokens, kind=None, windows=False):
    """The command as the worker runs it. A Windows worker keeps gradlew.bat (and gets --no-daemon too); its program
    path gets backslashes, which CreateProcess needs for a relative path."""
    if not windows:
        return for_linux(tokens, kind)
    t = strip_wrappers(tokens)
    if not t:
        return t
    t[0] = t[0].replace('/', '\\')
    if (kind or kind_of(tokens)) == 'gradle' and not {'--no-daemon', '--daemon'} & set(t):
        t.append('--no-daemon')
    return t


def is_android(folder):
    """An Android Gradle build: com.android in a build script here or one level down (app/)."""
    for d in [folder] + [os.path.join(folder, x) for x in _listdir(folder) if os.path.isdir(os.path.join(folder, x))]:
        for f in ('build.gradle', 'build.gradle.kts', 'settings.gradle', 'settings.gradle.kts'):
            try:
                with open(os.path.join(d, f), encoding='utf-8', errors='replace') as fh:
                    if 'com.android' in fh.read():
                        return True
            except OSError:
                pass
    return False


def _listdir(d):
    try:
        return sorted(os.listdir(d))
    except OSError:
        return []


# the project's files --------------------------------------------------------------------------------------------------
def project_root(cwd):
    """The git checkout the job runs in (so ../shared files come along), else the folder itself."""
    try:
        r = subprocess.run(['git', '-C', cwd, 'rev-parse', '--show-toplevel'], capture_output=True, text=True,
                           timeout=20, creationflags=NO_WINDOW)
        if r.returncode == 0 and r.stdout.strip():
            return os.path.normpath(r.stdout.strip())
    except (OSError, subprocess.TimeoutExpired):
        pass
    return os.path.normpath(cwd)


def project_key(root):
    base = re.sub(r'[^A-Za-z0-9._-]', '_', os.path.basename(root.rstrip('\\/')) or 'project')[:60]
    return f"{base}-{hashlib.sha1(os.path.normcase(os.path.abspath(root)).encode('utf-8')).hexdigest()[:8]}"


def script(rel):
    """A shell script the worker runs: sent executable, with LF line endings."""
    return rel.split('/')[-1] in EXEC or rel.endswith('.sh')


def skipped(rel, dirs=True):
    parts = rel.split('/')
    return (parts[-1] in SKIP_FILES or parts[-1] in OWN or secret(parts[-1])
            or (dirs and any(p in SKIP_DIRS for p in parts[:-1])))


def secret(name):
    return any(fnmatch.fnmatch(name.lower(), pat) for pat in SECRETS)


def auto_skip(tokens, s, kind=None):
    """The word that keeps this job here in auto mode, or None. For dotnet, "publish" is a build (dotnet publish makes
    the program), so it may go; an upload there is "push" (dotnet nuget push), which stays."""
    words = [w.lower() for w in s.get('pool_auto_skip') or []]
    if (kind or kind_of(tokens)) == 'dotnet':
        words = [w for w in words if w != 'publish'] + ['push']
    if not s.get('pool_resign', True):   # nothing signs it again here: a release would carry the worker's key
        words += ['release', 'sign', 'bundle']
    for t in strip_wrappers(tokens)[1:]:
        low = t.lower().replace('installdist', '')   # :core:installDist unpacks a program into build/: no device
        for w in words:
            if w and w in low:
                return t
    return None


def list_files(root):
    """Relative POSIX paths to send. In a checkout: what git tracks plus new files it doesn't ignore (build folders
    among the new ones skipped); elsewhere every file outside the usual build and cache folders."""
    try:
        def git(*extra):
            r = subprocess.run(['git', '-C', root, 'ls-files', '-z', *extra], capture_output=True, timeout=60,
                               creationflags=NO_WINDOW)
            if r.returncode:
                raise OSError(r.stderr.decode('utf-8', 'replace'))
            return [x for x in r.stdout.decode('utf-8', 'replace').split('\0') if x]
        tracked = git('--cached')
        others = git('--others', '--exclude-standard')
        files = [f for f in tracked if not skipped(f, dirs=False)] + [f for f in others if not skipped(f)]
    except (OSError, subprocess.TimeoutExpired):
        files = []
        for d, subdirs, fs in os.walk(root):
            subdirs[:] = [x for x in subdirs if x not in SKIP_DIRS]
            for f in fs:
                rel = os.path.relpath(os.path.join(d, f), root).replace(os.sep, '/')
                if not skipped(rel):
                    files.append(rel)
    return sorted({f for f in files if os.path.isfile(os.path.join(root, *f.split('/')))})


SIGNING = ('keystore.properties', 'key.properties', 'signing.properties', '*.jks', '*.keystore', 'local.properties')
NOT_INPUTS = ('*.apk', '*.aab', '*.zip', '*.exe', '*.msi', '*.log', '*.tmp', '*.bak', '*.pyc')


def unsent_inputs(root, gradle_dir):
    """Files git ignores (so a trip never sends them) that the build reads: one the Gradle scripts name (keys.js copied
    into the assets), anything ignored inside a src folder (a Capacitor app's synced web files and config.xml), and an
    ignored module folder with its own build script (capacitor-cordova-android-plugins). A worker would build without
    them. Signing files don't count: the APK is signed again here. [names]"""
    try:
        r = subprocess.run(['git', '-C', root, 'ls-files', '-z', '--others', '--ignored', '--exclude-standard',
                            '--directory'], capture_output=True, timeout=60, creationflags=NO_WINDOW)
    except (OSError, subprocess.TimeoutExpired):
        return []
    if r.returncode:
        return []
    names, inside = set(), set()
    grel = os.path.relpath(gradle_dir, root).replace(os.sep, '/')
    prefix = '' if grel == '.' else grel.rstrip('/') + '/'
    listed = [x for x in r.stdout.decode('utf-8', 'replace').split('\0') if x]
    folders = [x for x in listed if x.endswith('/')]
    if folders:   # newer git (2.5x) also lists a folder that only holds ignored files: keep what a pattern ignores
        try:
            c = subprocess.run(['git', '-C', root, 'check-ignore', '-z', '--stdin'], input='\0'.join(folders).encode(),
                               capture_output=True, timeout=60, creationflags=NO_WINDOW)
            if c.returncode in (0, 1):
                kept = {x.rstrip('/') for x in c.stdout.decode('utf-8', 'replace').split('\0') if x}
                listed = [x for x in listed if not x.endswith('/') or x.rstrip('/') in kept]
        except (OSError, subprocess.TimeoutExpired):
            pass
    for rel in listed:
        folder = rel.endswith('/')
        parts = rel.rstrip('/').split('/')
        name = parts[-1]
        if any(p in SKIP_DIRS for p in parts[:None if folder else -1]) \
                or any(fnmatch.fnmatch(name.lower(), p) for p in SIGNING + NOT_INPUTS):
            continue
        if rel.startswith(prefix):
            sub = rel[len(prefix):].rstrip('/')
            if folder and 'src' in sub.split('/')[:-1]:
                continue   # newer git names an ignored folder under src: its files are listed one by one below
            if 'src' in sub.split('/')[:-1] or (folder and any(
                    os.path.isfile(os.path.join(root, *parts, f)) for f in ('build.gradle', 'build.gradle.kts'))):
                inside.add(sub + ('/' if folder else ''))
                continue
        if not folder:
            names.add(name)
    try:   # --directory hides ignored files inside a folder git doesn't track yet: list the src folders file by file
        r = subprocess.run(['git', '-C', root, 'ls-files', '-z', '--others', '--ignored', '--exclude-standard', '--',
                            prefix or '.'] + [f':(exclude,glob)**/{d}/**' for d in ('build', '.gradle', '.cxx',
                                                                                     'node_modules', '.idea')],
                           capture_output=True, timeout=60, creationflags=NO_WINDOW)
        for rel in (r.stdout.decode('utf-8', 'replace').split('\0') if r.returncode == 0 else []):
            parts = rel.split('/')
            if rel.startswith(prefix) and 'src' in rel[len(prefix):].split('/')[:-1] \
                    and not any(p in SKIP_DIRS for p in parts[:-1]) \
                    and not any(fnmatch.fnmatch(parts[-1].lower(), p) for p in SIGNING + NOT_INPUTS):
                inside.add(rel[len(prefix):])
    except (OSError, subprocess.TimeoutExpired):
        pass
    if not names and not inside:
        return []
    try:   # an ignored copy of a tracked file (assets/generator.js made from extension/generator.js) is an output
        t = subprocess.run(['git', '-C', root, 'ls-files', '-z'], capture_output=True, timeout=60, creationflags=NO_WINDOW)
        tracked = {x.split('/')[-1] for x in t.stdout.decode('utf-8', 'replace').split('\0') if x}
        names -= tracked
        inside = {x for x in inside if x.endswith('/') or x.split('/')[-1] not in tracked}
    except (OSError, subprocess.TimeoutExpired):
        pass
    text = ''
    for d in [gradle_dir] + [os.path.join(gradle_dir, x) for x in _listdir(gradle_dir)
                             if os.path.isdir(os.path.join(gradle_dir, x)) and x not in SKIP_DIRS]:
        for f in ('build.gradle', 'build.gradle.kts', 'settings.gradle', 'settings.gradle.kts', 'gradle.properties'):
            try:
                with open(os.path.join(d, f), encoding='utf-8', errors='replace') as fh:
                    text += fh.read() + '\n'
            except OSError:
                pass
    return sorted(inside) + sorted(n for n in names if re.search(r'(?<![\w.-])' + re.escape(n) + r'(?![\w.-])', text))


def manifest(root, files):
    out = {}
    for rel in files:
        try:
            st = os.stat(os.path.join(root, *rel.split('/')))
        except OSError:
            continue
        out[rel] = [st.st_size, st.st_mtime_ns]
    return out


def safe_parts(rel):
    """A path from the other side, as parts under the project folder, or None when it would leave it."""
    rel = (rel or '').replace('\\', '/')
    if rel.startswith('/') or re.match(r'^[A-Za-z]:', rel):
        return None
    parts = [p for p in rel.split('/') if p not in ('', '.')]
    if not parts or any(p == '..' for p in parts):
        return None
    return parts


def matches(rel, patterns):
    """rel matches a pattern at its start or after any folder: build/outputs/** matches app/build/outputs/apk/x.apk."""
    parts = rel.split('/')
    for pat in patterns:
        pat = pat.replace('\\', '/').replace('**', '*')
        for i in range(len(parts)):
            if fnmatch.fnmatchcase('/'.join(parts[i:]), pat):
                return True
    return False


def _sha256(path):
    h = hashlib.sha256()
    with open(path, 'rb') as f:
        for b in iter(lambda: f.read(1 << 20), b''):
            h.update(b)
    return h.hexdigest()


def _json_member(name, obj):
    data = json.dumps(obj).encode('utf-8')
    ti = tarfile.TarInfo(name)
    ti.size, ti.mode, ti.mtime = len(data), 0o644, int(time.time())
    return ti, io.BytesIO(data)


# the trip, this side --------------------------------------------------------------------------------------------------
def extra_files(root, paths):
    """--send: files and folders of the project a trip sends although git ignores them (a built dist folder, a test
    copy of the app). Relative to the project (or absolute inside it); a folder goes whole. PoolError for one outside
    the project or missing."""
    out = []
    for p in paths or []:
        full = os.path.abspath(p if os.path.isabs(p) else os.path.join(root, p))
        rel = os.path.relpath(full, root).replace(os.sep, '/')
        if rel.startswith('..') or os.path.isabs(rel):
            raise PoolError(f'--send {p}: not inside the project ({root}); a trip sends only the project')
        if os.path.isfile(full):
            out.append(rel)
        elif os.path.isdir(full):
            for d, subdirs, fs in os.walk(full):
                for f in fs:
                    out.append(os.path.relpath(os.path.join(d, f), root).replace(os.sep, '/'))
        else:
            raise PoolError(f'--send {p}: no such file or folder')
    return out


def send(w, s, root, key, extra=None):
    """Copies what changed since the worker's last copy (and the --send files). {'files', 'deleted', 'bytes', 'secs',
    'total'}."""
    t0 = time.time()
    more = extra_files(root, extra)   # before anything is asked of the worker: a bad path runs the job here
    code, out, err = run_ssh(w, ['pool-worker', 'manifest', key], s, timeout=60)
    if code != 0:
        raise PoolError(f"could not read the worker's copy (exit {code}): {last_line(err) or last_line(out)}")
    try:
        remote = json.loads(last_line(out) or '{}')
    except ValueError:
        remote = {}
    files = sorted(set(list_files(root)) | set(more))
    local = manifest(root, files)
    changed = [r for r, v in local.items() if remote.get(r) != v]
    gone = [r for r in remote if r not in local]
    size = sum(local[r][0] for r in changed)
    asked = set(more)   # --send files were named on purpose: the cap is for a project that grew without anyone noticing
    project = sum(local[r][0] for r in changed if r not in asked)
    if project > float(s['pool_max_send_mb']) * 1024 * 1024:
        raise PoolError(f'{project / 1048576:.0f} MB of the project to send, over pool_max_send_mb '
                        f'({s["pool_max_send_mb"]}; --send files not counted)')
    for attempt in range(LOGIN_TRIES):
        with tempfile.TemporaryFile() as rout, tempfile.TemporaryFile() as rerr:
            p = subprocess.Popen(ssh_cmd(w, ['pool-worker', 'recv', key], s), stdin=subprocess.PIPE, stdout=rout,
                                 stderr=rerr, creationflags=NO_WINDOW)
            try:
                with tarfile.open(fileobj=p.stdin, mode='w|', format=tarfile.PAX_FORMAT) as t:
                    t.addfile(*_json_member(META, {'delete': gone, 'manifest': local}))
                    for rel in changed:
                        path = os.path.join(root, *rel.split('/'))
                        ti = tarfile.TarInfo(rel)
                        try:
                            st = os.stat(path)
                            ti.mtime = int(st.st_mtime)
                            if script(rel) and not is_windows(w):   # a Windows checkout gives gradlew CRLF: "#!/bin/sh\r"
                                with open(path, 'rb') as f:
                                    data = f.read().replace(b'\r\n', b'\n')
                                ti.size, ti.mode = len(data), 0o755
                                t.addfile(ti, io.BytesIO(data))
                            else:
                                with open(path, 'rb') as f:
                                    ti.size, ti.mode = st.st_size, 0o644
                                    t.addfile(ti, f)
                        except OSError:
                            continue   # gone or locked since the listing: the next trip sends it
                p.stdin.close()
            except (BrokenPipeError, OSError):
                pass
            try:
                code = p.wait(timeout=600)
            except subprocess.TimeoutExpired:
                p.kill()
                code = 124
            rout.seek(0)
            rerr.seek(0)
            o, e = rout.read().decode('utf-8', 'replace'), rerr.read().decode('utf-8', 'replace')
        if not login_failed(code, e) or attempt == LOGIN_TRIES - 1:
            break
        login_wait(attempt)
    if code != 0:
        raise PoolError(f'copying the project failed (exit {code}): {last_line(e) or last_line(o)}')
    return {'files': len(changed), 'deleted': len(gone), 'bytes': size, 'secs': round(time.time() - t0, 1),
            'total': len(local)}


def back_path(home, key):
    return os.path.join(home, 'pool-back', key + '.json')


def bring_back(w, s, root, key, patterns, home, always=()):
    """The worker's files matching patterns that are missing here or changed there since they last came back, written
    under root after their SHA256 checks out. A clean here with the worker up to date still gets its APK back.
    always: patterns of a folder the command itself named (dotnet publish -o dist\\X), whose files come back whenever
    they differ from here, even with old times (publish copies files out of an up-to-date bin, keeping their times).
    {'files': [rel], 'bytes', 'bad': [rel], 'error'}."""
    patterns = list(patterns) + [p for p in always if p not in patterns]
    code, out, err = run_ssh(w, ['pool-worker', 'sums', key, *patterns], s, timeout=300)
    if code != 0:
        return {'files': [], 'bytes': 0, 'bad': [], 'error': f'exit {code}: ' + (last_line(err) or last_line(out))}
    try:
        sums = json.loads(last_line(out) or '{}')
    except ValueError:
        return {'files': [], 'bytes': 0, 'bad': [], 'error': 'the list of files was not JSON'}
    sums = {rel: (v if isinstance(v, list) else [v, True]) for rel, v in sums.items()}
    made = {rel for rel, (_, new) in sums.items() if new}
    sums = {rel: sha for rel, (sha, _) in sums.items()}
    had, renamed = read_back(home, key)

    def here(rel):   # present here, or signed and renamed here (app-release-unsigned.apk -> app-release.apk)
        return any(os.path.isfile(os.path.join(root, *r.split('/'))) for r in (rel, renamed.get(rel)) if r)

    # what this run made; what came back before and changed there or went missing here. A file the worker has from an
    # older build that never came back (a debug APK from another trip) stays there.
    need = [rel for rel, sha in sums.items() if safe_parts(rel) and matches(rel, patterns)
            and ((rel in made or rel in had or (always and matches(rel, always)))
                 and (had.get(rel) != sha or not here(rel)))]
    got, bad, total, error = [], [], 0, None
    if need:
        for attempt in range(LOGIN_TRIES):
            with tempfile.TemporaryFile() as rerr:
                p = subprocess.Popen(ssh_cmd(w, ['pool-worker', 'pack', key], s), stdin=subprocess.PIPE,
                                     stdout=subprocess.PIPE, stderr=rerr, creationflags=NO_WINDOW)
                try:
                    p.stdin.write(json.dumps(need).encode('utf-8'))
                    p.stdin.close()
                    with tarfile.open(fileobj=p.stdout, mode='r|') as t:
                        for m in t:
                            if m.name == SUMS:
                                continue
                            parts = safe_parts(m.name)
                            rel = '/'.join(parts) if parts else ''
                            if not m.isfile() or not parts or rel not in sums:
                                continue
                            dest = os.path.join(root, *parts)
                            os.makedirs(os.path.dirname(dest), exist_ok=True)
                            h = hashlib.sha256()
                            part = dest + '.heavypool-part'
                            with t.extractfile(m) as src, open(part, 'wb') as fo:
                                for b in iter(lambda: src.read(1 << 20), b''):
                                    h.update(b)
                                    fo.write(b)
                            if sums[rel] != h.hexdigest():
                                os.remove(part)
                                bad.append(rel)
                                continue
                            os.replace(part, dest)
                            os.utime(dest, (m.mtime, m.mtime))
                            got.append(rel)
                            had[rel] = sums[rel]
                            total += m.size
                except (tarfile.TarError, OSError, ValueError) as e:
                    error = f'{type(e).__name__}: {e}'
                p.stdout.close()
                code = p.wait()
                rerr.seek(0)
                etext = rerr.read().decode('utf-8', 'replace')
                if code != 0 and not error:
                    error = f'exit {code}: ' + last_line(etext)
            if got or not login_failed(code, etext) or attempt == LOGIN_TRIES - 1:
                break
            error = None
            login_wait(attempt)
    write_back(home, key, had, renamed)
    return {'files': got, 'bytes': total, 'bad': bad, 'error': error}


def read_back(home, key):
    """What came back from this project's copy before: ({rel: worker SHA256}, {rel: the name it was signed to here})."""
    try:
        with open(back_path(home, key), encoding='utf-8') as f:
            d = json.load(f)
    except (OSError, ValueError):
        d = {}
    renamed = d.pop('~renamed', {}) if isinstance(d.get('~renamed'), dict) else {}
    return d, renamed


def write_back(home, key, had, renamed):
    path = back_path(home, key)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, 'w', encoding='utf-8') as f:
        json.dump(dict(had, **{'~renamed': renamed}), f)


WIN_ABS = re.compile(r'^(?:[A-Za-z]:[\\/]|\\\\)')


def _absolute(v):
    return bool(WIN_ABS.match(v)) or (os.name != 'nt' and v.startswith('/'))


def localize(tokens, root, cwd):
    """The command with paths into the project made relative to the folder it runs in, as a worker's copy has them
    (a script calls C:\\App\\android\\gradlew.bat -p C:\\App\\android). A path outside the project can't be
    there: PoolError, and it runs here."""
    out = []
    for i, t in enumerate(tokens):
        m = re.match(r'^(-[^=\s]+=)(.+)$', t)
        prefix, val = (m.group(1), m.group(2)) if m else ('', t)
        if not _absolute(val):
            out.append(t)
            continue
        full = os.path.normpath(val)
        try:
            inside = os.path.relpath(full, root)
            rel = os.path.relpath(full, cwd).replace(os.sep, '/')
        except ValueError:   # another drive
            inside = '..'
        if inside == '..' or inside.startswith('..' + os.sep) or os.path.isabs(inside):
            if i == 0:   # a tool of this PC (C:/Program Files/.../python.exe): the worker has its own
                out.append(re.split(r'[\\/]', val)[-1])
                continue
            raise PoolError(f'the command names {val}, outside the project, which a worker has no copy of')
        if i == 0 and not rel.startswith('.'):
            rel = './' + rel
        out.append(prefix + rel)
    return out


def gradle_dir(tokens, cwd):
    """The Gradle build's folder: -p / --project-dir, else where it runs, else beside the gradlew it calls."""
    t = strip_wrappers(tokens)
    for i, x in enumerate(t):
        if x in ('-p', '--project-dir') and i + 1 < len(t):
            return os.path.normpath(os.path.join(cwd, t[i + 1]))
        if x.startswith('--project-dir='):
            return os.path.normpath(os.path.join(cwd, x.split('=', 1)[1]))
    has = lambda d: any(os.path.isfile(os.path.join(d, f)) for f in ('settings.gradle', 'settings.gradle.kts',
                                                                       'build.gradle', 'build.gradle.kts'))
    if has(cwd) or not t:
        return cwd
    near = os.path.dirname(os.path.normpath(os.path.join(cwd, t[0])))
    return near if has(near) else cwd


def active_dir(home):
    return os.path.join(home, 'pool-active')


PHASES = {'sending': 'sending the changes', 'running': 'building there', 'back': 'bringing the files back',
          'signing': 'signing the APKs here'}


def _note(path, rec, phase):
    """The trip as status, the tray and the strip see it: pool-active/<pid>.json, rewritten at each phase."""
    rec['phase'] = phase
    rec['phase_ts'] = time.time()
    try:
        tmp = path + '.tmp'
        with open(tmp, 'w', encoding='utf-8') as f:
            json.dump(rec, f)
        os.replace(tmp, path)
    except OSError:
        pass


def sender_name():
    """This machine's name, for the worker's strip: 'sent from MAIN-PC'."""
    import socket
    return re.sub(r'[^\w.-]+', '', os.environ.get('COMPUTERNAME') or socket.gethostname())[:40] or 'another machine'


def _job_ssh(w, remote, s, stream):
    """Starts the job's ssh. Its output goes on as before (to stream, or to this console); the first 4 KB are kept in
    `seen` so a login that was dropped before anything ran can be told from a job that failed."""
    seen, kept = [], [0]

    def keep(text):
        if kept[0] < 4096:
            seen.append(text)
            kept[0] += len(text)

    p = subprocess.Popen(ssh_cmd(w, remote, s), stdin=subprocess.DEVNULL, stdout=subprocess.PIPE if stream else None,
                         stderr=subprocess.STDOUT if stream else subprocess.PIPE,
                         creationflags=NO_WINDOW if stream else 0)
    p._hg_thread = None
    if not stream:   # streamed output is read (and kept) by the caller
        def pump():
            out = getattr(sys.stderr, 'buffer', None)
            for chunk in iter(lambda: p.stderr.read1(65536) if hasattr(p.stderr, 'read1') else p.stderr.read(4096), b''):
                keep(chunk.decode('utf-8', 'replace'))
                try:
                    (out.write(chunk), out.flush()) if out else (sys.stderr.write(chunk.decode('utf-8', 'replace')),
                                                                 sys.stderr.flush())
                except (OSError, ValueError):
                    pass
        p._hg_thread = threading.Thread(target=pump, daemon=True)
        p._hg_thread.start()
    return p, seen


def trip(home, s, wname, w, inf, cwd, tokens, kind, name, say, why='', lane='cpu', stream=None, extra=None, back=None,
         desktop=False):
    """Send, run, bring back. Raises PoolError only before the job started there (it can still run here); after that
    the result is the run record, its 'exit' the job's own exit code (255: the connection was lost). extra: more paths
    to send (--send); back: more patterns to bring back (--back), brought back even when the job failed (a failing
    check's screenshots are what you want to see)."""
    root = project_root(cwd)
    key = project_key(root)
    sub = os.path.relpath(cwd, root).replace(os.sep, '/')
    sub = '' if sub == '.' else sub
    if sub.startswith('..'):
        raise PoolError(f'{cwd} is not inside {root}')
    gdir = gradle_dir(tokens, cwd)
    cmd = for_worker(localize(tokens, root, cwd), kind, is_windows(w))
    if desktop and not is_windows(w):
        raise PoolError(f'--desktop needs a Windows worker ({wname} is not one)')
    if is_windows(w):
        remote_line(w, cmd)   # PoolError now, before anything is sent, for an argument cmd.exe would change
    note = os.path.join(active_dir(home), f'{os.getpid()}-{wname}.json')   # a split has several trips at once
    os.makedirs(active_dir(home), exist_ok=True)
    began = time.time()
    rec = {'name': name, 'lane': lane, 'pool': wname, 'host': w.get('host'), 'cwd': cwd,
           'project': os.path.basename(root.rstrip('\\/')), 'command': shlex.join(cmd)[:400], 'why': why,
           'queued': time.strftime('%Y-%m-%dT%H:%M:%S'), 'pid': os.getpid(), 'started_ts': began}
    _note(note, rec, 'sending')
    try:
        sent = send(w, s, root, key, extra)
    except BaseException:
        try:
            os.remove(note)
        except OSError:
            pass
        raise
    say(f"to {wname}: sent {sent['files']} changed of {sent['total']} files ({sent['bytes'] / 1048576:.1f} MB) in "
        f"{sent['secs']:g}s; running there: {shlex.join(cmd)}")
    cap = max(1024, room_mb(w, inf, s))
    started = time.time()
    rec.update({'started': time.strftime('%Y-%m-%dT%H:%M:%S'), 'sent_files': sent['files'],
                'sent_total': sent['total'], 'sent_mb': round(sent['bytes'] / 1048576, 1), 'send_s': sent['secs'],
                'ram_cap_mb': cap})
    _note(note, rec, 'running')
    remote = ['pool-worker', 'run', key, '--sub', sub, '-n', name, '--cap-mb', str(cap)] + \
        (['--lane', lane] if lane != 'cpu' else []) + (['--desktop'] if desktop else []) + \
        ['--from', sender_name(), '--', *cmd]
    try:
        p = None
        try:
            for attempt in range(LOGIN_TRIES):
                p, seen = _job_ssh(w, remote, s, stream)
                if stream:
                    for line in iter(p.stdout.readline, b''):
                        text = line.decode('utf-8', 'replace').rstrip('\r\n')
                        if sum(map(len, seen)) < 4096:
                            seen.append(text + '\n')
                        stream(text)
                code = p.wait()
                if p._hg_thread:
                    p._hg_thread.join(5)
                if code != 255 or attempt == LOGIN_TRIES - 1 or not only_login_lines(''.join(seen)):
                    break
                say(f'{wname} dropped the login ({last_line("".join(seen))}); trying again')
                login_wait(attempt)
        except KeyboardInterrupt:
            say(f'stopping it on {wname}')
            run_ssh(w, ['pool-worker', 'stop', key], s, timeout=30)
            if p:
                p.kill()
            code = 130
        rec['ran_s'] = round(time.time() - started, 1)
        rec['exit'] = code
        if code == 255:
            rec['warning'] = f'lost the connection to {wname}; the job may still be running there (heavy pool)'
            say(rec['warning'])
        elif code == 0 or (back and code != 130):
            named = [] if code else out_patterns(tokens, kind, cwd, root)[0]
            patterns = ([] if code else list((s.get('pool_outputs') or {}).get(kind or 'any') or [])) + named + \
                list(back or [])
            if patterns:
                _note(note, rec, 'back')
                b = bring_back(w, s, root, key, patterns, home, always=named)
                rec['back_files'] = len(b['files'])
                rec['back_mb'] = round(b['bytes'] / 1048576, 1)
                if b['bad'] or b['error']:
                    rec['warning'] = ('SHA256 did not match, not kept: ' + ', '.join(b['bad'][:5])) if b['bad'] else \
                        f"bringing the files back failed: {b['error']}"
                    rec['exit'] = rec['exit'] or 1
                    say(rec['warning'])
                elif b['files']:
                    shown = ', '.join(b['files'][:4]) + (f' and {len(b["files"]) - 4} more' if len(b['files']) > 4 else '')
                    say(f"brought back {len(b['files'])} files ({rec['back_mb']:g} MB, SHA256 checked): {shown}")
                    if kind == 'gradle' and s.get('pool_resign', True) and \
                            any(f.lower().endswith(('.apk', '.aab')) for f in b['files']):
                        import heavysign
                        _note(note, rec, 'signing')
                        done, failed = heavysign.sign_all(root, b['files'], gdir, s)
                        had, renamed = read_back(home, key)
                        for d in done:
                            out_rel = os.path.relpath(d['apk'], root).replace(os.sep, '/')
                            if d.get('rel') and out_rel != d['rel']:
                                renamed[d['rel']] = out_rel
                        write_back(home, key, had, renamed)
                        rec['signed'] = len(done)
                        if done:
                            rec['sign_cert'] = done[0]['cert']
                            say(heavysign.describe(done))
                        if failed:
                            rec['exit'] = 1
                            rec['warning'] = (f'{len(failed)} APK could not be signed here, renamed to .apk.worker-signed '
                                              f'so it is not installed by mistake: ' + '; '.join(
                                                  f'{r}: {why}' for r, why in failed[:3]))
                            say(rec['warning'])
                else:
                    say('nothing new to bring back (the job changed no files matching ' + ', '.join(patterns) + ')')
    finally:
        try:
            os.remove(note)
        except OSError:
            pass
    for k in ('started_ts', 'phase', 'phase_ts', 'sent_total'):
        rec.pop(k, None)
    return rec


def running(home):
    """Jobs this PC has out on workers now, from pool-active/*.json (a note whose heavy is gone is removed)."""
    out = []
    d = active_dir(home)
    for f in _listdir(d):
        if not f.endswith('.json'):
            continue
        p = os.path.join(d, f)
        try:
            with open(p, encoding='utf-8') as fh:
                r = json.load(fh)
        except (OSError, ValueError):
            continue
        if not _alive(r.get('pid')):
            try:
                os.remove(p)
            except OSError:
                pass
            continue
        r['for_s'] = max(0, time.time() - float(r.get('started_ts') or time.time()))
        r['phase_s'] = max(0, time.time() - float(r.get('phase_ts') or r.get('started_ts') or time.time()))
        r['doing'] = PHASES.get(r.get('phase'), r.get('phase') or 'running')
        out.append(r)
    return out


def trip_text(r):
    """'AppDemo -> server1: building there 1m12s' for a job out on a worker."""
    m, sec = divmod(int(r['for_s']), 60)
    return f"{r['name']} -> {r['pool']}: {r.get('doing', 'running')}  {m}m{sec:02d}s"


def _alive(pid):
    if not pid:
        return False
    if os.name == 'nt':
        import ctypes
        h = ctypes.windll.kernel32.OpenProcess(0x1000, False, int(pid))
        if not h:
            return False
        code = ctypes.c_ulong()
        ctypes.windll.kernel32.GetExitCodeProcess(h, ctypes.byref(code))
        ctypes.windll.kernel32.CloseHandle(h)
        return code.value == 259
    try:
        os.kill(int(pid), 0)
        return True
    except (OSError, ValueError):
        return False


# status ---------------------------------------------------------------------------------------------------------------
def worker_text(n, w, inf, s, kind=None):
    if not inf:
        return f'{n}: not checked yet (heavy pool)'
    if not inf.get('ok'):
        return f"{n}: {inf.get('why')}"
    cpu = inf.get('cpu') or {}
    tools = list(inf.get('kinds') or [])
    if 'gradle' in tools and inf.get('android_sdk'):
        tools[tools.index('gradle')] = 'gradle (Android SDK)'
    _, now, why = judge(w, inf, s, kind, False, int(s['pool_expect_mb'].get(kind or 'gradle', 2500)))
    return (f"{n}: {'ready' if now else 'busy'}, cpu lane {cpu.get('running', 0)}/{cpu.get('slots', '?')}"
            + (f" +{cpu['queued']} queued" if cpu.get('queued') else '')
            + f", {inf.get('mem_free_mb', 0) / 1024:.1f} of {inf.get('mem_total_mb', 0) / 1024:.1f} GB free "
            f"(keeps {int(w.get('reserve_mb', s['pool_reserve_mb'])) / 1024:.1f}), {inf.get('cores', '?')} cores, "
            + (', '.join(tools) or 'no build tools')
            + (f", Windows, GPU {inf.get('gpu') or 'none'}" if inf.get('windows') else '') + ('' if now else f'. {why}'))


def summary(home, s):
    """For status: pool.json and the jobs out now. Never calls ssh. None with no workers."""
    d = load(home)
    if not d['workers']:
        return None
    return {'auto': bool(s.get('pool_auto')), 'kinds': s.get('pool_kinds'),
            'workers': {n: {'host': w.get('host'), 'reserve_mb': w.get('reserve_mb'), 'info': w.get('info')}
                        for n, w in d['workers'].items()},
            'running': running(home)}


def status_lines(summ, s):
    if not summ:
        return []
    how = ('auto: ' + ', '.join(summ['kinds'] or []) + ' jobs go to a ready worker') if summ['auto'] else \
        'auto off; heavy cpu --pool sends a job'
    out = [f'  POOL ({how}):']
    for n, w in summ['workers'].items():
        inf = w.get('info')
        age = ''
        if inf and inf.get('checked_ts'):
            m = (time.time() - inf['checked_ts']) / 60
            age = f'  [checked {m:.0f} min ago]' if m >= 1 else '  [checked just now]'
        mine = {'reserve_mb': w['reserve_mb']} if w.get('reserve_mb') is not None else {}
        out.append('    ' + worker_text(n, mine, inf, s) + age)
    for r in summ.get('running') or []:
        out.append(f"    out now: {trip_text(r)}  {r['cwd']}")
        if r.get('why'):
            out.append(f"      why: {r['why']}")
    return out


# the worker's side (heavy pool-worker ...) ----------------------------------------------------------------------------
def android_sdk():
    for c in (os.environ.get('ANDROID_HOME'), os.environ.get('ANDROID_SDK_ROOT'),
              os.path.expanduser('~/android-sdk'), os.path.expanduser('~/Android/Sdk')):
        if c and os.path.isdir(os.path.join(c, 'platforms')):
            return c
    return None


def scope_ok():
    """systemd can put a job in its own memory-capped scope (a user session with systemd --user)."""
    if not sys.platform.startswith('linux') or not shutil.which('systemd-run'):
        return False
    try:
        return subprocess.run(['systemd-run', '--user', '--scope', '--quiet', 'true'], capture_output=True,
                              timeout=15).returncode == 0
    except (OSError, subprocess.TimeoutExpired):
        return False


def pool_home():
    """Where a worker keeps the project copies: ~/.cache/heavygate-pool (HEAVYGATE_POOL_DIR moves it)."""
    return os.environ.get('HEAVYGATE_POOL_DIR') or os.path.join(os.path.expanduser('~'), *ROOT.split('/'))


def worker_dir(key):
    if not KEY_RE.match(key or '') or key in ('.', '..'):
        raise PoolError(f'not a project key: {key!r}')
    return os.path.join(pool_home(), key)


def worker_info(hg):
    s = hg.settings()
    avail, total = hg.mem_mb()[:2]
    rows = hg.lane_rows(True)
    cpu = [L for L in rows if L['lane'] == 'cpu']
    gpu = [L for L in rows if L['lane'] == 'gpu']
    os.makedirs(pool_home(), exist_ok=True)
    kinds = [k for k, tool in (('gradle', 'java'), ('dotnet', 'dotnet'), ('cargo', 'cargo'), ('node', 'node'))
             if shutil.which(tool)]
    try:
        load_avg = round(os.getloadavg()[0], 2)
    except (OSError, AttributeError):
        load_avg = None
    return {'version': hg.VERSION, 'os': sys.platform, 'cores': os.cpu_count(), 'mem_total_mb': total,
            'mem_free_mb': avail, 'load': load_avg,
            'cpu': {'slots': hg.slots('cpu'), 'running': sum(1 for L in cpu if L['job']),
                    'queued': sum(len(L['queue']) for L in cpu)},
            'gpu_lane': {'running': sum(1 for L in gpu if L['job']), 'queued': sum(len(L['queue']) for L in gpu)},
            'gpu': (hg.hardware_names() or {}).get('gpu'), 'windows': os.name == 'nt',
            'python': sys.version.split()[0],
            'kinds': kinds, 'java': shutil.which('java'), 'android_sdk': android_sdk(), 'scope': scope_ok(),
            'disk_free_gb': round(shutil.disk_usage(pool_home()).free / 1024 ** 3, 1),
            'disk_floor_gb': float(s['disk_floor_gb']),
            'cache': hg.heavycache.host_summary(s)}   # the build cache it serves, read without waking it (or None)


def worker_manifest(key):
    try:
        with open(os.path.join(worker_dir(key), MANIFEST), encoding='utf-8') as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def worker_recv(key, stream):
    """Reads the tar a trip sends: the meta member first (files to delete, the new manifest), then the changed files."""
    d = worker_dir(key)
    os.makedirs(d, exist_ok=True)
    meta, n, deleted = {}, 0, 0
    with tarfile.open(fileobj=stream, mode='r|') as t:
        for m in t:
            if m.name == META:
                meta = json.load(t.extractfile(m))
                for rel in meta.get('delete') or []:
                    parts = safe_parts(rel)
                    if parts:
                        try:
                            os.remove(os.path.join(d, *parts))
                            deleted += 1
                        except OSError:
                            pass
                continue
            parts = safe_parts(m.name)
            if not m.isfile() or not parts or parts[-1] in OWN:
                continue
            dest = os.path.join(d, *parts)
            if os.path.isdir(dest):
                shutil.rmtree(dest)
            os.makedirs(os.path.dirname(dest), exist_ok=True)
            part = dest + '.heavypool-part'
            with t.extractfile(m) as src, open(part, 'wb') as out:
                shutil.copyfileobj(src, out, 1 << 20)
            os.chmod(part, 0o755 if m.mode & 0o100 else 0o644)
            os.replace(part, dest)
            os.utime(dest, (m.mtime, m.mtime))
            n += 1
    with open(os.path.join(d, MANIFEST), 'w', encoding='utf-8') as f:
        json.dump(meta.get('manifest') or {}, f)
    return {'files': n, 'deleted': deleted}


DESKTOP_SPEC, DESKTOP_LOG, DESKTOP_EXIT = '.heavypool-desktop.json', '.heavypool-desktop.log', '.heavypool-desktop.exit'


def desktop_task(key):
    return 'HeavyGate-desktop-' + re.sub(r'[^\w.-]+', '_', key)[:80]


def desktop_run(hg, key, d, name, argv, cwd, env, start_s=45):
    """--desktop on a Windows worker: an ssh session has no desktop, and WebView2 (an app's window) won't start there.
    So the job runs in the signed-in user's own session through a one-off scheduled task (/IT), with no console window;
    its output is a log file this side streams back, and the task is deleted after. Needs someone signed in there."""
    for f in (DESKTOP_LOG, DESKTOP_EXIT):
        try:
            os.remove(os.path.join(d, f))
        except OSError:
            pass
    with open(os.path.join(d, DESKTOP_SPEC), 'w', encoding='utf-8') as f:
        json.dump({'argv': argv, 'cwd': cwd, 'env': env}, f)
    pyw = os.path.join(os.path.dirname(sys.executable), 'pythonw.exe')
    runner = pyw if os.path.isfile(pyw) else sys.executable
    tr = subprocess.list2cmdline([runner, os.path.abspath(hg.__file__), 'pool-worker', 'desktop', key])
    task = desktop_task(key)
    r = subprocess.run(['schtasks', '/create', '/tn', task, '/tr', tr, '/sc', 'once', '/st', '00:00', '/it', '/f'],
                       capture_output=True, text=True)
    if r.returncode:
        print(f'heavy pool: could not make the desktop task: {(r.stderr or r.stdout).strip()}', file=sys.stderr)
        return 2
    print(f"[heavy] {name}: in the desktop session of {os.environ.get('USERNAME', 'the user')} (a one-off task), so "
          f'windows can open', file=sys.stderr, flush=True)
    log, done = os.path.join(d, DESKTOP_LOG), os.path.join(d, DESKTOP_EXIT)
    try:
        r = subprocess.run(['schtasks', '/run', '/tn', task], capture_output=True, text=True)
        if r.returncode:
            print(f'heavy pool: the desktop task did not start: {(r.stderr or r.stdout).strip()}', file=sys.stderr)
            return 2
        t0, pos = time.time(), 0
        while True:
            if os.path.isfile(log):
                with open(log, 'rb') as f:
                    f.seek(pos)
                    chunk = f.read()
                if chunk:
                    pos += len(chunk)
                    sys.stdout.buffer.write(chunk)
                    sys.stdout.flush()
            elif time.time() - t0 > start_s:
                print(f'heavy pool: the desktop task never started in {start_s} s: is anyone signed in on '
                      f"{os.environ.get('COMPUTERNAME', 'the worker')}? (--desktop needs a signed-in session)",
                      file=sys.stderr)
                return 2
            if os.path.isfile(done):
                time.sleep(0.3)
                with open(log, 'rb') as f:   # what came after the last read
                    f.seek(pos)
                    sys.stdout.buffer.write(f.read())
                sys.stdout.flush()
                with open(done) as f:
                    return int(f.read().strip() or 1)
            time.sleep(0.5)
    except KeyboardInterrupt:
        worker_stop(key)
        return 130
    finally:
        subprocess.run(['schtasks', '/end', '/tn', task], capture_output=True)
        subprocess.run(['schtasks', '/delete', '/tn', task, '/f'], capture_output=True)


def desktop_runner(key):
    """The task's side of --desktop, in the user's session: runs the job with no console window, its output to the
    log, its pid where pool-worker stop finds it, its exit code last."""
    d = worker_dir(key)
    with open(os.path.join(d, DESKTOP_SPEC), encoding='utf-8') as f:
        spec = json.load(f)
    log = open(os.path.join(d, DESKTOP_LOG), 'wb', buffering=0)
    code = 1
    try:
        env = dict(spec['env'], PYTHONUNBUFFERED='1')
        p = subprocess.Popen(spec['argv'], cwd=spec['cwd'], env=env, stdout=log, stderr=subprocess.STDOUT,
                             stdin=subprocess.DEVNULL, creationflags=NO_WINDOW)
        with open(os.path.join(d, PIDFILE), 'w') as f:
            f.write(str(p.pid))
        code = p.wait()
    except Exception as e:
        log.write(f'heavy pool: the desktop task could not run it: {e}\n'.encode())
    finally:
        log.close()
        for f in (PIDFILE,):
            try:
                os.remove(os.path.join(d, f))
            except OSError:
                pass
        with open(os.path.join(d, DESKTOP_EXIT), 'w') as f:
            f.write(str(code))
    return code


def worker_run(hg, key, sub, name, cap_mb, cmd, lane='cpu', desktop=False, from_host=''):
    """Runs the command in the worker's own lane (cpu unless told), in the project copy, under a memory cap: a systemd
    scope on Linux; on Windows the lane's own Job Object (HeavyGate's RAM ceiling there). desktop: in the signed-in
    user's session (desktop_run)."""
    d = worker_dir(key)
    parts = safe_parts(sub) if sub else []
    if parts is None:
        print(f'heavy pool: not a folder in the project: {sub!r}', file=sys.stderr)
        return 2
    cwd = os.path.join(d, *parts)
    if not os.path.isdir(cwd):
        print(f'heavy pool: {cwd} is not there; was the project sent?', file=sys.stderr)
        return 2
    with open(os.path.join(d, START), 'w') as f:
        f.write(str(time.time()))
    env = dict(os.environ)
    env.pop('HEAVYGATE_HELD', None)
    if from_host:   # the worker's own status, strip and tray say who sent it
        env['HEAVYGATE_FROM'] = re.sub(r'[^\w.-]+', '', from_host)[:40]
    sdk = android_sdk()
    if sdk:
        env.setdefault('ANDROID_HOME', sdk)
        env.setdefault('ANDROID_SDK_ROOT', sdk)
    cmd = list(cmd)
    if cmd and cmd[0] in ('python', 'python3') and not shutil.which(cmd[0]):   # Ubuntu has python3 only, Windows python
        cmd[0] = sys.executable
    if lane not in ('cpu', 'gpu'):
        lane = 'cpu'
    argv = [sys.executable, os.path.abspath(hg.__file__), lane, '-n', name, '--no-pool', '--no-cloud', '--', *cmd]
    if os.name == 'nt':
        print(f"[heavy] {name}: on {os.environ.get('COMPUTERNAME', 'it')}, its {lane} lane, RAM capped by its own "
              f'HeavyGate (Job Object)', file=sys.stderr, flush=True)
    elif cap_mb and scope_ok():
        argv = ['systemd-run', '--user', '--scope', '--quiet', '-p', f'MemoryMax={int(cap_mb)}M',
                '-p', 'MemorySwapMax=0', '--'] + argv
        print(f'[heavy] {name}: on {os.uname().nodename}, RAM cap {int(cap_mb) / 1024:.1f} GB (systemd scope)',
              file=sys.stderr, flush=True)
    if desktop:
        if os.name != 'nt':
            print('heavy pool: --desktop is for Windows workers', file=sys.stderr)
            return 2
        return desktop_run(hg, key, d, name, argv, cwd, env)
    p = subprocess.Popen(argv, cwd=cwd, env=env, start_new_session=os.name != 'nt')
    pidfile = os.path.join(d, PIDFILE)
    with open(pidfile, 'w') as f:
        f.write(str(p.pid))
    try:
        return p.wait()
    except KeyboardInterrupt:
        _stop_pid(p.pid)
        return 130
    finally:
        try:
            os.remove(pidfile)
        except OSError:
            pass


def _stop_pid(pid):
    if os.name == 'nt':
        subprocess.run(['taskkill', '/T', '/F', '/PID', str(pid)], capture_output=True)
        return
    for sig, wait in ((signal.SIGTERM, 5), (signal.SIGKILL, 0)):
        try:
            os.killpg(pid, sig)
        except OSError:
            return
        t = time.time()
        while time.time() - t < wait:
            try:
                os.killpg(pid, 0)
            except OSError:
                return
            time.sleep(0.2)


def worker_stop(key):
    try:
        with open(os.path.join(worker_dir(key), PIDFILE)) as f:
            pid = int(f.read().strip())
    except (OSError, ValueError):
        return False
    _stop_pid(pid)
    return True


def worker_sums(key, patterns):
    """{rel: [SHA256, made by the last run]} of every file in the project copy matching patterns."""
    d = worker_dir(key)
    try:
        since = os.stat(os.path.join(d, START)).st_mtime - 2
    except OSError:
        since = float('inf')
    out = {}
    for base, subdirs, fs in os.walk(d):
        subdirs[:] = [x for x in subdirs if x not in ('.git', '.gradle', '.cxx')]
        for f in fs:
            path = os.path.join(base, f)
            rel = os.path.relpath(path, d).replace(os.sep, '/')
            if rel not in OWN and matches(rel, patterns):
                try:
                    out[rel] = [_sha256(path), os.stat(path).st_mtime >= since]
                except OSError:
                    pass
    return out


def worker_pack(key, wanted, stream):
    """Writes a tar of the files named in wanted (relative paths in the project copy)."""
    d = worker_dir(key)
    found = []
    for rel in wanted:
        parts = safe_parts(rel)
        if parts and os.path.isfile(os.path.join(d, *parts)):
            found.append(('/'.join(parts), os.path.join(d, *parts)))
    with tarfile.open(fileobj=stream, mode='w|', format=tarfile.PAX_FORMAT) as t:
        t.addfile(*_json_member(SUMS, {}))
        for rel, path in found:
            t.add(path, arcname=rel, recursive=False)
    return len(found)
