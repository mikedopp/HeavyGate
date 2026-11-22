#!/usr/bin/env python3
"""HeavyGate runner probe: what a GitHub Actions runner is doing while it builds, for the HeavyGate tray at home.

GitHub gives no live CPU, RAM or disk for a hosted runner, and a running job's log can't be read until the job ends.
So this posts one commit status every 15 s (context "heavygate runner (<job>)", target_url = the run), and HeavyGate
reads the status history back:  cpu 87% ram 5.1/15.6G disk 41.2/145.0G cores 4 peak 100% 6.2G

Copied into a repo as .github/heavygate-probe.py by `heavy cloud probe <checkout>` (copy, never fork: change it in
HeavyGate, then copy again). Standard library only, Linux and Windows. It never fails the build: a status it can't post
is skipped, and it stops trying after a run of failures. Needs `permissions: statuses: write` and GITHUB_TOKEN in env.

  python heavygate-probe.py --start       start the sampler so it outlives the step, then return (use this one)
  python heavygate-probe.py               sample every 15 s until the job ends, in the foreground
  python heavygate-probe.py --final S     the last status: S is the job's status (success, failure, cancelled)

On a Windows runner every process a step starts is ended when the step ends (the step runs in a job object), so a
plain `Start-Process` sampler dies at once. --start leaves the job (CREATE_BREAKAWAY_FROM_JOB) or, when the job
doesn't allow that, has WMI start it; WMI's process gets its settings from a file in RUNNER_TEMP that it deletes.
"""
import json
import os
import shutil
import sys
import time
import urllib.request

PROBE_VERSION = '1.1.0'
EVERY_S = 15
MAX_HOURS = 6
ENV_KEYS = ('GITHUB_REPOSITORY', 'GITHUB_SHA', 'GITHUB_TOKEN', 'GITHUB_JOB', 'GITHUB_SERVER_URL', 'GITHUB_RUN_ID',
            'GITHUB_API_URL', 'GITHUB_WORKSPACE', 'RUNNER_TEMP')


def temp(name):
    return os.path.join(os.environ.get('RUNNER_TEMP') or '.', name)


def state_path():
    return temp('heavygate-probe.json')


def cpu_times():
    """(idle, total) CPU time since boot, in any unit."""
    if sys.platform == 'win32':
        import ctypes

        class FT(ctypes.Structure):
            _fields_ = [('lo', ctypes.c_uint32), ('hi', ctypes.c_uint32)]
        idle, kern, user = FT(), FT(), FT()
        ctypes.windll.kernel32.GetSystemTimes(ctypes.byref(idle), ctypes.byref(kern), ctypes.byref(user))
        v = lambda f: (f.hi << 32) | f.lo
        return v(idle), v(kern) + v(user)   # kernel time includes idle time
    with open('/proc/stat') as f:
        p = [int(x) for x in f.readline().split()[1:]]
    return p[3] + (p[4] if len(p) > 4 else 0), sum(p[:8])


def mem_gb():
    """(used, total) RAM in GB."""
    if sys.platform == 'win32':
        import ctypes

        class MS(ctypes.Structure):
            _fields_ = [('len', ctypes.c_uint32), ('load', ctypes.c_uint32), ('total', ctypes.c_uint64),
                        ('avail', ctypes.c_uint64), ('pt', ctypes.c_uint64), ('pa', ctypes.c_uint64),
                        ('vt', ctypes.c_uint64), ('va', ctypes.c_uint64), ('ve', ctypes.c_uint64)]
        m = MS(len=ctypes.sizeof(MS))
        ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(m))
        return (m.total - m.avail) / 2 ** 30, m.total / 2 ** 30
    info = {}
    with open('/proc/meminfo') as f:
        for line in f:
            k, v = line.split(':', 1)
            info[k] = int(v.split()[0]) * 1024
    return (info['MemTotal'] - info.get('MemAvailable', info.get('MemFree', 0))) / 2 ** 30, info['MemTotal'] / 2 ** 30


def disk_gb():
    """(used, total) GB of the drive the workspace is on."""
    u = shutil.disk_usage(os.environ.get('GITHUB_WORKSPACE') or '.')
    return u.used / 2 ** 30, u.total / 2 ** 30


def describe(s):
    """The status text HeavyGate parses (GitHub keeps 140 characters)."""
    return (f"cpu {s['cpu']:.0f}% ram {s['ram']:.1f}/{s['ram_total']:.1f}G disk {s['disk']:.1f}/{s['disk_total']:.1f}G "
            f"cores {s['cores']} peak {s['peak_cpu']:.0f}% {s['peak_ram']:.1f}G")[:140]


def post(state, description):
    env = os.environ
    repo, sha, token = env.get('GITHUB_REPOSITORY'), env.get('GITHUB_SHA'), env.get('GITHUB_TOKEN')
    if not (repo and sha and token):
        return False
    body = json.dumps({'state': state, 'description': description,
                       'context': f"heavygate runner ({env.get('GITHUB_JOB', 'job')})",
                       'target_url': f"{env.get('GITHUB_SERVER_URL', 'https://github.com')}/{repo}/actions/runs/"
                                     f"{env.get('GITHUB_RUN_ID', '')}"}).encode()
    req = urllib.request.Request(f"{env.get('GITHUB_API_URL', 'https://api.github.com')}/repos/{repo}/statuses/{sha}",
                                 data=body, method='POST',
                                 headers={'Authorization': f'Bearer {token}', 'Accept': 'application/vnd.github+json',
                                          'User-Agent': 'heavygate-probe'})
    try:
        with urllib.request.urlopen(req, timeout=15) as r:
            return 200 <= r.status < 300
    except Exception as e:
        print(f'heavygate probe: status not posted: {e}', flush=True)
        return False


def save(s):
    try:
        with open(state_path(), 'w') as f:
            json.dump(s, f)
    except OSError:
        pass


def sample_forever():
    s = {'cores': os.cpu_count() or 0, 'peak_cpu': 0.0, 'peak_ram': 0.0, 'sum_cpu': 0.0, 'n': 0}
    last, fails, t_end = cpu_times(), 0, time.time() + MAX_HOURS * 3600
    while time.time() < t_end and fails < 8:
        time.sleep(EVERY_S)
        now = cpu_times()
        di, dt = now[0] - last[0], now[1] - last[1]
        last = now
        s['cpu'] = max(0.0, min(100.0, 100.0 * (1 - di / dt))) if dt > 0 else 0.0
        s['ram'], s['ram_total'] = mem_gb()
        s['disk'], s['disk_total'] = disk_gb()
        s['peak_cpu'], s['peak_ram'] = max(s['peak_cpu'], s['cpu']), max(s['peak_ram'], s['ram'])
        s['sum_cpu'] += s['cpu']
        s['n'] += 1
        save(s)
        fails = 0 if post('pending', describe(s)) else fails + 1


def start():
    """Start the sampler so it outlives this step; print how."""
    import subprocess
    me, log = os.path.abspath(__file__), temp('heavygate-probe.log')
    if sys.platform != 'win32':
        with open(log, 'ab') as out:
            p = subprocess.Popen([sys.executable, me], stdin=subprocess.DEVNULL, stdout=out, stderr=out,
                                 start_new_session=True)
        print(f'heavygate probe {PROBE_VERSION}: sampling in pid {p.pid}')
        return 0
    flags = 0x00000008 | 0x00000200   # DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP
    try:
        with open(log, 'ab') as out:
            p = subprocess.Popen([sys.executable, me], stdin=subprocess.DEVNULL, stdout=out, stderr=out,
                                 creationflags=flags | 0x01000000)   # CREATE_BREAKAWAY_FROM_JOB
        print(f'heavygate probe {PROBE_VERSION}: sampling in pid {p.pid} (left the step\'s job)')
        return 0
    except OSError as e:
        print(f'heavygate probe: the step\'s job does not allow leaving it ({e}); starting through WMI')
    envf = temp('heavygate-probe.env.json')
    with open(envf, 'w') as f:
        json.dump({k: os.environ[k] for k in ENV_KEYS if os.environ.get(k)}, f)
    cmd = f'"{sys.executable}" "{me}" --env "{envf}"'
    ps = (f"$r = Invoke-CimMethod -ClassName Win32_Process -MethodName Create -Arguments "
          f"@{{CommandLine='{cmd}'; CurrentDirectory='{os.getcwd()}'}}; "
          f"'WMI ReturnValue ' + $r.ReturnValue + ' pid ' + $r.ProcessId")
    r = subprocess.run(['powershell', '-NoProfile', '-NonInteractive', '-Command', ps],
                       capture_output=True, text=True, timeout=60)
    print(f'heavygate probe {PROBE_VERSION}: ' + (r.stdout.strip() or r.stderr.strip()))
    return 0


def from_env_file(path):
    """The WMI-started sampler: take the step's settings from the file, then delete it (it holds the token)."""
    try:
        with open(path) as f:
            os.environ.update(json.load(f))
    finally:
        try:
            os.remove(path)
        except OSError:
            pass
    log = temp('heavygate-probe.log')
    sys.stdout = sys.stderr = open(log, 'a', buffering=1)


def final(job_status):
    try:
        with open(state_path()) as f:
            s = json.load(f)
    except (OSError, ValueError):
        s = {}
    if not s.get('n'):
        print('heavygate probe: no readings were taken, so there is no last status. Its log:')
        try:
            with open(temp('heavygate-probe.log'), errors='replace') as f:
                print(''.join(f.readlines()[-20:]) or '(empty)')
        except OSError:
            print('(no log)')
        return
    s['cpu'] = s['sum_cpu'] / s['n']   # the last status shows the run's average CPU, and its peaks
    state = {'success': 'success', 'failure': 'failure'}.get(job_status, 'error')
    text = ('done ' + describe(s).replace('cpu ', 'avg cpu ', 1))[:140]
    print(f"heavygate probe: {s['n']} readings; " + text + ('' if post(state, text) else ' (not posted)'))


if __name__ == '__main__':
    if len(sys.argv) > 2 and sys.argv[1] == '--final':
        final(sys.argv[2])
    elif len(sys.argv) > 1 and sys.argv[1] == '--start':
        sys.exit(start())
    else:
        if len(sys.argv) > 2 and sys.argv[1] == '--env':
            from_env_file(sys.argv[2])
        sample_forever()
