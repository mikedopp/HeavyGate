"""The apps using memory on this machine, and closing the ones you pick, to make room for a build.

Used by the strip's "free RAM..." link and the tray menu (heavyapps.pyw, a small window) and by `heavy apps`
(text, for checking a pool worker from another machine). Only the signed-in user's own apps in this desktop session
are listed. Never listed, so never closed: Windows itself (the shell, services, security, drivers), HeavyGate, Python,
SSH, and every process of a job running in a lane (the build).

Closing asks first: each window of the app gets WM_CLOSE (as if you clicked its X). An app still running after
`wait_s` (it hid in the tray, or it is stuck) is ended with its child processes. Unsaved work in an app that is ended
is lost; the window says so.
"""
import ctypes
import os
import sys
import time

import heavygate as hg

WIN = os.name == 'nt'

# Never listed: Windows and its shell, security, drivers' helpers, the things HeavyGate itself runs on, and helpers that
# live and die with another app (WebView2, crash reporters). Names in lower case without .exe.
KEEP = {
    'system', 'idle', 'registry', 'memory compression', 'secure system', 'smss', 'csrss', 'wininit', 'winlogon',
    'services', 'lsass', 'lsaiso', 'svchost', 'fontdrvhost', 'dwm', 'explorer', 'sihost', 'ctfmon', 'taskhostw',
    'runtimebroker', 'searchhost', 'searchindexer', 'searchapp', 'searchui', 'startmenuexperiencehost',
    'shellexperiencehost', 'shellhost', 'textinputhost', 'applicationframehost', 'lockapp', 'logonui', 'userinit',
    'securityhealthsystray', 'securityhealthservice', 'msmpeng', 'nissrv', 'mpdefendercoreservice', 'smartscreen',
    'mpcmdrun', 'audiodg', 'conhost', 'openconsole', 'dllhost', 'wmiprvse', 'spoolsv', 'dashost', 'wudfhost',
    'useroobebroker', 'backgroundtaskhost', 'systemsettingsbroker', 'gamebar', 'gamebarftserver', 'crossdevicehost',
    'widgetservice', 'microsoftsecurityapp', 'securityhealthhost', 'phoneexperiencehost', 'crossdeviceresume',
    'crossdeviceservice', 'msedgewebview2', 'crashpad_handler', 'werfault', 'taskmgr', 'sshd', 'sshd-session',
    'ssh-agent', 'ssh', 'python', 'pythonw', 'py', 'pyw', 'cmd', 'powershell', 'pwsh', 'windowsterminal',
    'nvcontainer', 'nvdisplay.container', 'nvidia web helper', 'amdrsserv', 'amdrssrcext', 'atieclxx', 'atiesrxx',
    'radeonsoftware', 'igfxem', 'igfxcuiservice', 'rtkauduservice64', 'wlanext', 'unsecapp', 'vmcompute', 'vmmem',
    'vmmemwsl', 'wslservice', 'wslhost', 'git', 'dotnet', 'java', 'javaw', 'node', 'gradle', 'cargo', 'rustc',
    'msbuild', 'vbcscompiler', 'cl', 'link',
}


def session_of(pid):
    if not WIN:
        return 0
    s = ctypes.c_ulong()
    return s.value if hg.k32.ProcessIdToSessionId(pid, ctypes.byref(s)) else None


def lane_pids():
    """The jobs running in the lanes here and everything they started: never offered for closing."""
    roots = set()
    for lane in hg.LANES:
        try:
            for r in hg.peeks(lane):
                if r.get('pid'):
                    roots.add(int(r['pid']))
        except Exception:
            pass
    keep = set(roots)
    for pid in roots:
        keep.update(hg._descendants(pid))
    return keep


def windowed_pids():
    """Processes with a visible top-level window (the app you see), Windows only."""
    if not WIN:
        return set()
    u = ctypes.WinDLL('user32')
    from ctypes import wintypes
    found = set()
    proc = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)

    def each(hwnd, _):
        if u.IsWindowVisible(hwnd) and u.GetWindowTextLengthW(hwnd) > 0 and not u.GetWindow(hwnd, 4):   # GW_OWNER
            pid = wintypes.DWORD()
            u.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
            found.add(pid.value)
        return True
    u.EnumWindows(proc(each), 0)
    return found


def apps(min_mb=40):
    """[{name, mb, pids, windowed}] biggest first: the user's own apps in this session, by program name."""
    # the desktop's session; over ssh (no desktop) the signed-in user's console session, so `heavy apps` there lists
    # what is on the laptop's screen
    me = session_of(os.getpid()) if not WIN or hg.has_desktop() else ctypes.windll.kernel32.WTSGetActiveConsoleSessionId()
    keep = lane_pids() | {os.getpid(), os.getppid()}
    shown = windowed_pids()
    by = {}
    for pid, exe, mb in hg.processes():
        name = exe.lower().removesuffix('.exe')
        if pid in keep or name in KEEP or name.startswith('heavy') or name in ('?', ''):
            continue
        if WIN and session_of(pid) != me:   # services and other people's sessions
            continue
        a = by.setdefault(name, {'name': exe.removesuffix('.exe').removesuffix('.EXE'), 'mb': 0, 'pids': [],
                                 'windowed': False})
        a['mb'] += mb
        a['pids'].append(pid)
        a['windowed'] = a['windowed'] or pid in shown
    return sorted((a for a in by.values() if a['mb'] >= min_mb), key=lambda a: -a['mb'])


def _close_windows(pids):
    """WM_CLOSE to every top-level window of these processes: what clicking each window's X does."""
    if not WIN:
        return 0
    u = ctypes.WinDLL('user32')
    from ctypes import wintypes
    pids, sent = set(pids), [0]
    proc = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)

    def each(hwnd, _):
        pid = wintypes.DWORD()
        u.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        if pid.value in pids and u.IsWindowVisible(hwnd):
            u.PostMessageW(hwnd, 0x0010, 0, 0)   # WM_CLOSE
            sent[0] += 1
        return True
    u.EnumWindows(proc(each), 0)
    return sent[0]


def close(names, wait_s=8.0, say=print):
    """Closes the apps with these names (as apps() gives them): asks their windows, then ends what is left after
    wait_s. Returns {name: 'closed' | 'ended' | 'could not end it (...)' | 'not running'}; RAM freed is said."""
    want = {n.lower() for n in names}
    listed = {a['name'].lower(): a for a in apps(min_mb=0)}
    targets = {n: listed[n] for n in want if n in listed}
    out = {n: 'not running' for n in want if n not in listed}
    if not targets:
        return out
    before = hg.ram_load(100)[0] if hasattr(hg, 'ram_load') else None
    for n, a in targets.items():
        w = _close_windows(a['pids'])
        say(f"{a['name']}: asked {w} window{'s' if w != 1 else ''} to close" if w else
            f"{a['name']}: no window to ask, ending it")
    deadline = time.time() + wait_s
    while time.time() < deadline and any(hg.pid_alive(p) for a in targets.values() for p in a['pids']):
        time.sleep(0.4)
    for n, a in targets.items():
        alive = [p for p in a['pids'] if hg.pid_alive(p)]
        if not alive:
            out[n] = 'closed'
            continue
        ok = all(hg.end_pid_tree(p) for p in alive)
        out[n] = 'ended' if ok else 'could not end it (it may run as administrator)'
    for n, a in targets.items():
        say(f"{a['name']}: {out[n]} ({a['mb'] / 1024:.1f} GB)")
    if before is not None:
        time.sleep(1.0)
        say(f'RAM in use: {before:.0f}% before, {hg.ram_load(100)[0]:.0f}% now')
    return out


def text(min_mb=40):
    rows = apps(min_mb)
    if not rows:
        return 'No apps of yours to close here (Windows, HeavyGate and the lanes\' jobs are never listed).'
    w = max(len(a['name']) for a in rows)
    return '\n'.join(f"{a['name']:<{w}}  {a['mb'] / 1024:5.1f} GB  {len(a['pids']):>3} process"
                     f"{'es' if len(a['pids']) != 1 else '  '}{'  window' if a['windowed'] else ''}" for a in rows)


if __name__ == '__main__':
    print(text())
    sys.exit(0)
