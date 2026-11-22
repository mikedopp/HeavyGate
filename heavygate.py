#!/usr/bin/env python3
"""HeavyGate: heavy jobs on this PC run one per lane, across every session and terminal.

    heavy gpu -n "GameDemo full" -- test.cmd full          3D windows, GPU checks, game QA, GPU renders
    heavy cpu -n "AppDemo apk" -- gradlew assembleRelease     builds, Blender CPU renders, big zips
    heavy auto -n "AppDemo" -- COMMAND                     let the rules pick the lane (heavy why, heavy rules)
    heavy status       per lane: what is running, what is waiting for RAM, what is queued; RAM/CPU/GPU now
                       (--json for other tools)
    heavy watch [-s 2] the same, live, redrawn every 2 s until Ctrl+C
    heavy tray         a text strip above the clock while anything runs or waits (hidden when idle), and a tray
                       icon: right-click either for every job and the commands (heavytray.pyw)
    heavy log [-n 20]  what ran: time queued, time waiting for RAM, run time, lowest free RAM, CPU and GPU peaks
    heavy free         stop idle Gradle and Kotlin daemons (never while a Gradle build runs)
    heavy guard        the RAM guard: ends a command run by a coding agent named in guard_agents that takes the PC past
                       ram_ceiling_pct of its RAM, lane or no lane (runs in the background; started by the tray, the
                       agent hook and at sign-in). heavy guard --check shows what it would end right now.
    heavy cloud        the cloud lane (GitHub Actions): minutes used and left this month, charges, running and recent
                       runs; heavy cloud advise -- COMMAND says whether a job should run here or on GitHub, and
                       heavy cloud dispatch REPO WORKFLOW starts one there (heavycloud.py). heavy cloud map ties a
                       build command to a workflow; with heavy cloud auto on, heavy auto sends it to GitHub when the
                       PC is short, follows the run and brings its files back into the project (--cloud / --no-cloud)
    heavy deps         what this tool depends on

Disk floor (1.16.0): a job does not start while the drive it runs on, or the temp/caches drive, has under
disk_floor_gb (15 GB) free, and it is ended if one of them falls under disk_stop_gb (3 GB): a full drive breaks git
and every app on it, not just the build. Exit 28 when a job was refused or ended for disk. heavy status shows the
drives; heavy log shows how much each job's drive went down while it ran.

RAM ceiling (1.13.0): physical RAM in use must never pass ram_ceiling_pct (92%). A job waits until it can start with
its usual need still under the ceiling, and never starts anyway past it. On Windows each job runs inside a Job Object
whose memory cap is what was left under the ceiling when it started: past that, its allocations fail and it stops,
instead of the PC going to 100%. While it runs, if RAM goes over the ceiling for ram_over_s seconds, or reaches
ram_kill_pct (95%) at all, the job is ended. The guard does the same for every command a coding agent started.

The gpu lane runs one job at a time and the cpu lane cpu_slots (2) at once; the others queue in arrival order. Before a job starts, the gate waits (up to
ram_wait_min minutes) until enough RAM is free, stopping idle Gradle daemons once if that helps. cpu jobs run at
below-normal priority on cpu_threads of the logical processors, so the desktop stays usable; everything they start
inherits that. --threads N gives one cpu job a different share. A gated job started inside another (build.cmd calling gradle, a suite calling a check) runs in its
parent's slot. HEAVYGATE=off skips the gate. Exit code: the command's own, or 75 when --no-wait finds the lane busy.

From Python: `with heavygate.hold('gpu', 'name') as slot: ...` (**slot.popen and slot.spawning() for Popen).
Python 3.9+ standard library. Windows and Linux (no tray on Linux: heavy watch is the live view); on anything else
it just runs the command.
"""
import argparse
import ctypes
import errno
import json
import os
import re
import shlex
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
import time
import urllib.request
from contextlib import contextmanager
from datetime import datetime

_HERE = os.path.dirname(os.path.abspath(__file__))
if _HERE not in sys.path:   # heavycloud.py sits beside this file; also when loaded through the old tools-path stub
    sys.path.append(_HERE)
import heavycloud  # noqa: E402

VERSION = '1.18.4'
LANES = ('gpu', 'cpu')
DEFAULT_HOME = os.path.join((os.environ.get('LOCALAPPDATA') or os.path.expanduser('~')) if os.name == 'nt' else
                            (os.environ.get('XDG_STATE_HOME') or os.path.expanduser('~/.local/state')), 'HeavyGate')


def home():
    """The state folder, read each time: HEAVYGATE_DIR can change after this module is imported (tests do)."""
    return os.environ.get('HEAVYGATE_DIR') or DEFAULT_HOME


HOME = home()   # the folder at import, kept for callers that read it
DEFAULTS = {
    'cpu_threads': 0,                           # logical processors a cpu job may use; 0 = three quarters of them
    'reserve_mb': 768,                          # RAM left over for the desktop once the job has what it needs
    'expect_mb': {'gpu': 3500, 'cpu': 2500},    # what a job of each lane usually takes (GameDemo ~3.5 GB, Gradle ~2.5)
    'ram_wait_min': 10,                         # then the job starts anyway, with a warning in the log
    'free_daemons': True,                       # stop idle Gradle/Kotlin daemons when RAM is short
    'free_ollama': True,                        # unload Ollama's models before a GPU job, and when RAM is short
    'ollama_url': 'http://127.0.0.1:11434',
    'poll_s': 3,
    'shutdown_idle_s': 180,                     # "shut down when done": both lanes empty this long (a follow-up job may queue)
    'shutdown_countdown_s': 60,                 # then a countdown on the strip, cancelled by clicking it
    'stall_min': 5,                             # a job this long with no real activity is flagged STALLED (never stopped)
    'stall_cpu_s': 2.0,                         # CPU seconds across the job's processes that count as real activity
    'stall_check_s': 15,
    'stall_stop_min': 20,                       # that long idle with a job waiting behind it: end it. 0 = never
    'cpu_slots': 2,                             # cpu jobs that may run at once (gpu always runs one)
    'foreground_wait_s': 90,                    # an agent's foreground build waits this long for a slot, then exits 75
    'ram_ceiling_pct': 92,                      # physical RAM in use never goes past this: jobs are capped and ended to keep it
    'ram_kill_pct': 95,                         # reached at all: the job (or the agent's command) is ended at once
    'ram_over_s': 3,                            # over the ceiling this long: ended
    'guard_agents': [],                         # your coding agents' process names: the guard may end their commands
    'gated_tools': [],                          # your own tools that take a lane themselves, so the hook leaves them be
    'guard_min_mb': 300,                        # an agent's shell command smaller than this is never the one ended
    'guard_big_mb': 2000,                       # anything else an agent started (an MCP server, a helper): only past this
    # Apps an agent app only opened for the user (Chrome from a link in the agent's window): never ended when they sit
    # directly under the agent. A browser a command started (headless Edge from a script) is still that command's.
    'guard_spare': ['chrome', 'msedge', 'firefox', 'brave', 'opera', 'vivaldi', 'explorer'],
    'disk_floor_gb': 15,                        # a job does not start with less free on its drive or the temp drive
    'disk_stop_gb': 3,                          # a running job is ended when one of those drives falls under this
    'disk_paths': [],                           # more folders whose drives are checked too (a cache on another drive)
}
DEFAULTS.update(heavycloud.DEFAULTS)            # the cloud lane's settings (GitHub Actions): see heavycloud.py
BUSY_EXIT = 75
DISK_EXIT = 28   # ENOSPC: refused or ended because a drive was too full
WIN = os.name == 'nt'
LINUX = sys.platform.startswith('linux')
SUPPORTED = WIN or LINUX
BELOW_NORMAL = 0x4000
CPU_LANE_NICE = 10
NO_WINDOW = 0x08000000


def settings():
    s = json.loads(json.dumps(DEFAULTS))
    try:
        with open(os.path.join(home(), 'settings.json'), encoding='utf-8') as f:
            for k, v in json.load(f).items():
                if isinstance(v, dict) and isinstance(s.get(k), dict):
                    s[k].update(v)
                else:
                    s[k] = v
    except (OSError, ValueError):
        pass
    return s


def now_iso():
    return datetime.now().isoformat(timespec='seconds')


def fmt_secs(s):
    s = int(round(s or 0))
    return f'{s}s' if s < 60 else f'{s // 60}m{s % 60:02d}s' if s < 3600 else f'{s // 3600}h{(s % 3600) // 60:02d}m'


# Win32 --------------------------------------------------------------------------------------------------------------
if LINUX:
    import fcntl

if WIN:
    import msvcrt
    from ctypes import wintypes

    k32 = ctypes.WinDLL('kernel32', use_last_error=True)
    k32.CreateFileW.restype = wintypes.HANDLE
    k32.CreateFileW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, ctypes.c_void_p, wintypes.DWORD,
                                wintypes.DWORD, wintypes.HANDLE]
    k32.OpenProcess.restype = wintypes.HANDLE
    k32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    k32.CloseHandle.argtypes = [wintypes.HANDLE]
    k32.GetCurrentProcess.restype = wintypes.HANDLE
    k32.GetProcessAffinityMask.argtypes = [wintypes.HANDLE, ctypes.POINTER(ctypes.c_size_t), ctypes.POINTER(ctypes.c_size_t)]
    k32.SetProcessAffinityMask.argtypes = [wintypes.HANDLE, ctypes.c_size_t]
    k32.QueryFullProcessImageNameW.argtypes = [wintypes.HANDLE, wintypes.DWORD, wintypes.LPWSTR, ctypes.POINTER(wintypes.DWORD)]
    k32.GetProcessTimes.argtypes = [wintypes.HANDLE] + [ctypes.POINTER(wintypes.FILETIME)] * 4
    k32.GetExitCodeProcess.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
    k32.K32GetProcessMemoryInfo.argtypes = [wintypes.HANDLE, ctypes.c_void_p, wintypes.DWORD]
    k32.K32EnumProcesses.argtypes = [ctypes.c_void_p, wintypes.DWORD, ctypes.POINTER(wintypes.DWORD)]
    INVALID_HANDLE = ctypes.c_void_p(-1).value

    class MEMORYSTATUSEX(ctypes.Structure):
        _fields_ = [('dwLength', wintypes.DWORD), ('dwMemoryLoad', wintypes.DWORD), ('ullTotalPhys', ctypes.c_ulonglong),
                    ('ullAvailPhys', ctypes.c_ulonglong), ('ullTotalPageFile', ctypes.c_ulonglong),
                    ('ullAvailPageFile', ctypes.c_ulonglong), ('ullTotalVirtual', ctypes.c_ulonglong),
                    ('ullAvailVirtual', ctypes.c_ulonglong), ('ullAvailExtendedVirtual', ctypes.c_ulonglong)]

    class PMC(ctypes.Structure):
        _fields_ = [('cb', wintypes.DWORD), ('PageFaultCount', wintypes.DWORD)] + \
                   [(n, ctypes.c_size_t) for n in ('PeakWorkingSetSize', 'WorkingSetSize', 'QuotaPeakPagedPoolUsage',
                                                    'QuotaPagedPoolUsage', 'QuotaPeakNonPagedPoolUsage',
                                                    'QuotaNonPagedPoolUsage', 'PagefileUsage', 'PeakPagefileUsage')]


def _open_excl(path, create_new=False):
    """Open path for writing so no one else can (readers still can). None when someone else has it, or when
    create_new and it exists. The OS drops the handle when the process dies, however it dies."""
    if not WIN:
        return _open_flock(path, create_new)
    h = k32.CreateFileW(path, 0xC0000000, 1, None, 1 if create_new else 4, 0x80, None)  # RW, share read; CREATE_NEW/OPEN_ALWAYS
    if h is None or h == INVALID_HANDLE:
        err = ctypes.get_last_error()
        if err in (5, 32, 33, 80):  # access denied (being deleted), sharing/lock violation, exists
            return None
        raise OSError(err, ctypes.FormatError(err), path)
    return msvcrt.open_osfhandle(h, os.O_RDWR)


def _flock(fd):
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        return True
    except OSError:
        return False


def _open_flock(path, create_new):
    """_open_excl on Linux: a flock, which the kernel drops with the process. Python's descriptors don't pass to
    children, so a job never keeps its gate's lock alive. A new file is locked under a temporary name first, so nobody
    can find it unlocked in the queue and take it for a dead waiter's."""
    if not create_new:
        fd = os.open(path, os.O_RDWR | os.O_CREAT, 0o644)
        if _flock(fd):
            return fd
        os.close(fd)
        return None
    tmp = path + '.new'
    try:
        fd = os.open(tmp, os.O_RDWR | os.O_CREAT | os.O_EXCL, 0o644)
    except FileExistsError:
        return None
    if not _flock(fd):
        os.close(fd)
        return None
    os.rename(tmp, path)
    return fd


def _remove_if_unheld(path):
    """Remove a ticket nobody holds. True when it is gone, False while its waiter still has it."""
    if WIN:
        try:
            os.remove(path)
        except FileNotFoundError:
            pass
        except OSError:
            return False
        return True
    try:
        fd = os.open(path, os.O_RDWR)
    except FileNotFoundError:
        return True
    try:
        if not _flock(fd):
            return False
        try:
            os.remove(path)
        except FileNotFoundError:
            pass
        return True
    finally:
        os.close(fd)


def _write(fd, data):
    b = json.dumps(data).encode('utf-8')
    os.lseek(fd, 0, 0)
    os.ftruncate(fd, 0)
    os.write(fd, b)


def _read_json(path):
    try:
        with open(path, encoding='utf-8') as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


if WIN:
    class _JOBBASIC(ctypes.Structure):
        _fields_ = [('PerProcessUserTimeLimit', ctypes.c_int64), ('PerJobUserTimeLimit', ctypes.c_int64),
                    ('LimitFlags', wintypes.DWORD), ('MinimumWorkingSetSize', ctypes.c_size_t),
                    ('MaximumWorkingSetSize', ctypes.c_size_t), ('ActiveProcessLimit', wintypes.DWORD),
                    ('Affinity', ctypes.c_size_t), ('PriorityClass', wintypes.DWORD), ('SchedulingClass', wintypes.DWORD)]

    class _JOBEXT(ctypes.Structure):
        _fields_ = [('Basic', _JOBBASIC), ('Io', ctypes.c_uint64 * 6), ('ProcessMemoryLimit', ctypes.c_size_t),
                    ('JobMemoryLimit', ctypes.c_size_t), ('PeakProcessMemoryUsed', ctypes.c_size_t),
                    ('PeakJobMemoryUsed', ctypes.c_size_t)]

    k32.CreateJobObjectW.restype = wintypes.HANDLE
    k32.CreateJobObjectW.argtypes = [ctypes.c_void_p, wintypes.LPCWSTR]
    k32.SetInformationJobObject.argtypes = [wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD]
    k32.QueryInformationJobObject.argtypes = [wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD, ctypes.c_void_p]
    k32.AssignProcessToJobObject.argtypes = [wintypes.HANDLE, wintypes.HANDLE]
    _ntdll = ctypes.WinDLL('ntdll')
    _ntdll.NtResumeProcess.argtypes = [wintypes.HANDLE]
    CREATE_SUSPENDED = 0x4


def job_with_cap(cap_mb):
    """A Windows Job Object whose processes, together, may commit no more than cap_mb. Anything started inside it is in
    it too, so a build tool's compilers and test hosts share the one cap. None when Windows refuses."""
    if not WIN:
        return None
    job = k32.CreateJobObjectW(None, None)
    if not job:
        return None
    info = _JOBEXT()
    info.Basic.LimitFlags = 0x200   # JOB_OBJECT_LIMIT_JOB_MEMORY
    info.JobMemoryLimit = int(cap_mb) * 2 ** 20
    if not k32.SetInformationJobObject(job, 9, ctypes.byref(info), ctypes.sizeof(info)):   # ExtendedLimitInformation
        k32.CloseHandle(job)
        return None
    return job


def job_peak_mb(job):
    info = _JOBEXT()
    if job and k32.QueryInformationJobObject(job, 9, ctypes.byref(info), ctypes.sizeof(info), None):
        return info.PeakJobMemoryUsed // 2 ** 20
    return None


def start_in_job(line, job, **popen):
    """Popen(line, shell=True) started suspended, put in job, then let go, so not one allocation happens outside it."""
    popen = dict(popen)
    popen['creationflags'] = popen.get('creationflags', 0) | CREATE_SUSPENDED
    p = subprocess.Popen(line, shell=True, **popen)
    ok = bool(k32.AssignProcessToJobObject(job, int(p._handle)))
    _ntdll.NtResumeProcess(int(p._handle))
    return p, ok


def mem_mb():
    """(available, total, committed, commit limit) in MB."""
    if LINUX:
        m = {}
        with open('/proc/meminfo', encoding='ascii') as f:
            for line in f:
                k, _, v = line.partition(':')
                m[k] = int(v.split()[0]) // 1024
        return m['MemAvailable'], m['MemTotal'], m['Committed_AS'], m['CommitLimit']
    if not WIN:
        return 0, 0, 0, 0
    m = MEMORYSTATUSEX()
    m.dwLength = ctypes.sizeof(m)
    k32.GlobalMemoryStatusEx(ctypes.byref(m))
    mb = 2 ** 20
    return (m.ullAvailPhys // mb, m.ullTotalPhys // mb, (m.ullTotalPageFile - m.ullAvailPageFile) // mb,
            m.ullTotalPageFile // mb)


def ram_load(ceiling_pct):
    """(percent of physical RAM in use, MB that can still be used before ceiling_pct, MB kept free by the ceiling)."""
    avail, total, _, _ = mem_mb()
    if not total:
        return 0.0, 1 << 30, 0
    keep = int(total * (100 - float(ceiling_pct)) / 100)
    return 100.0 * (total - avail) / total, avail - keep, keep


def _ft(t):
    return (t.dwHighDateTime << 32) | t.dwLowDateTime


class CpuMeter:
    """Whole-machine CPU use between calls, in percent."""
    def __init__(self):
        self.last = self._times()

    @staticmethod
    def _times():
        if LINUX:
            with open('/proc/stat', encoding='ascii') as f:
                v = [int(x) for x in f.readline().split()[1:9]]   # user nice system idle iowait irq softirq steal
            return v[3] + v[4], sum(v)
        if not WIN:
            return 0, 0
        i, k, u = wintypes.FILETIME(), wintypes.FILETIME(), wintypes.FILETIME()
        k32.GetSystemTimes(ctypes.byref(i), ctypes.byref(k), ctypes.byref(u))
        return _ft(i), _ft(k) + _ft(u)   # kernel time includes idle

    def percent(self):
        cur = self._times()
        di, dt = cur[0] - self.last[0], cur[1] - self.last[1]
        self.last = cur
        return round(100 * (1 - di / dt)) if dt > 0 else 0


def processes():
    """[(pid, exe name, working set MB)] for every process this user can see."""
    if LINUX:
        out = []
        for pid in _proc_pids():
            try:
                with open(f'/proc/{pid}/comm', encoding='utf-8', errors='replace') as f:
                    name = f.read().strip()
                out.append((pid, name, _proc_rss_mb(pid)))
            except (OSError, ValueError, IndexError):
                continue   # it ended while we looked
        return out
    if not WIN:
        return []
    arr = (wintypes.DWORD * 8192)()
    got = wintypes.DWORD()
    k32.K32EnumProcesses(arr, ctypes.sizeof(arr), ctypes.byref(got))
    out = []
    for pid in arr[:got.value // 4]:
        h = k32.OpenProcess(0x1000, False, pid)   # PROCESS_QUERY_LIMITED_INFORMATION
        if not h:
            continue
        try:
            buf, n = ctypes.create_unicode_buffer(1024), wintypes.DWORD(1024)
            name = os.path.basename(buf.value) if k32.QueryFullProcessImageNameW(h, 0, buf, ctypes.byref(n)) else '?'
            pmc = PMC()
            pmc.cb = ctypes.sizeof(pmc)
            ws = pmc.WorkingSetSize // 2 ** 20 if k32.K32GetProcessMemoryInfo(h, ctypes.byref(pmc), pmc.cb) else 0
            out.append((pid, name, ws))
        finally:
            k32.CloseHandle(h)
    return out


def _proc_pids():
    return [int(d) for d in os.listdir('/proc') if d.isdigit()]


def _proc_rss_mb(pid):
    with open(f'/proc/{pid}/statm', encoding='ascii') as f:
        return int(f.read().split()[1]) * os.sysconf('SC_PAGE_SIZE') // 2 ** 20


def _proc_stat(pid):
    """The fields of /proc/pid/stat after the name (which may hold spaces): [0] state, [11] utime, [12] stime,
    [19] start time. None when the process is gone."""
    try:
        with open(f'/proc/{pid}/stat', encoding='utf-8', errors='replace') as f:
            return f.read().rpartition(')')[2].split()
    except OSError:
        return None


def _boot_time():
    with open('/proc/stat', encoding='ascii') as f:
        return next(int(x.split()[1]) for x in f if x.startswith('btime'))


def top_memory(n=6):
    by = {}
    for _, name, ws in processes():
        k = name.lower().removesuffix('.exe')
        by[k] = by.get(k, 0) + ws
    return sorted(by.items(), key=lambda kv: -kv[1])[:n]


def top_text(n=6):
    return ', '.join(f'{k} {v / 1024:.1f} GB' for k, v in top_memory(n))


def cpu_seconds(pid):
    if LINUX:
        st = _proc_stat(pid)
        return (int(st[11]) + int(st[12])) / os.sysconf('SC_CLK_TCK') if st else None
    h = k32.OpenProcess(0x1000, False, pid)
    if not h:
        return None
    try:
        c, e, k, u = (wintypes.FILETIME() for _ in range(4))
        if not k32.GetProcessTimes(h, ctypes.byref(c), ctypes.byref(e), ctypes.byref(k), ctypes.byref(u)):
            return None
        return (_ft(k) + _ft(u)) / 1e7
    finally:
        k32.CloseHandle(h)


def gpu_now():
    """{'util', 'mem_mb', 'total_mb', 'temp'} from nvidia-smi, or None."""
    exe = shutil.which('nvidia-smi')
    if not exe:
        return None
    try:
        r = subprocess.run([exe, '--query-gpu=utilization.gpu,memory.used,memory.total,temperature.gpu',
                            '--format=csv,noheader,nounits'], capture_output=True, text=True, timeout=5,
                           creationflags=NO_WINDOW if WIN else 0)
        u, mu, mt, t = (float(x) for x in r.stdout.strip().splitlines()[0].split(','))
        return {'util': u, 'mem_mb': mu, 'total_mb': mt, 'temp': t}
    except (OSError, ValueError, IndexError, subprocess.SubprocessError):
        return None


_HARDWARE = None


def hardware_names():
    """{'cpu': 'AMD Ryzen 7 5700G with Radeon Graphics', 'gpu': 'NVIDIA GeForce RTX 3050'}, read once and kept.
    Either is None when it can't be read (no registry on this OS, no nvidia-smi)."""
    global _HARDWARE
    if _HARDWARE is not None:
        return _HARDWARE
    cpu = gpu = None
    if WIN:
        try:
            import winreg
            with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, r'HARDWARE\DESCRIPTION\System\CentralProcessor\0') as k:
                cpu = ' '.join(str(winreg.QueryValueEx(k, 'ProcessorNameString')[0]).split()) or None
        except OSError:
            pass
    elif LINUX:
        try:
            with open('/proc/cpuinfo', encoding='utf-8', errors='replace') as f:
                cpu = next((' '.join(x.split(':', 1)[1].split()) for x in f if x.startswith('model name')), None)
        except OSError:
            pass
    exe = shutil.which('nvidia-smi')
    if exe:
        try:
            r = subprocess.run([exe, '--query-gpu=name', '--format=csv,noheader'], capture_output=True, text=True,
                               timeout=5, creationflags=NO_WINDOW if WIN else 0)
            gpu = r.stdout.strip().splitlines()[0].strip() or None
        except (OSError, IndexError, subprocess.SubprocessError):
            pass
    if not gpu and WIN:   # no NVIDIA card: ask Windows, so AMD and Intel adapters get a name too
        names = display_adapters()
        real = [n for n in names if not n.lower().startswith(('microsoft basic', 'microsoft remote', 'microsoft hyper-v'))]
        gpu = (real or names or [None])[0]
    if not gpu and LINUX and shutil.which('lspci'):
        try:
            r = subprocess.run(['lspci'], capture_output=True, text=True, timeout=10)
            gpu = next((x.split(': ', 1)[1].strip() for x in r.stdout.splitlines()
                        if 'VGA compatible controller' in x or '3D controller' in x), None)
        except (OSError, IndexError, subprocess.SubprocessError):
            pass
    _HARDWARE = {'cpu': cpu, 'gpu': gpu}
    return _HARDWARE


def display_adapters():
    """Display adapter names from the registry (the display class key), with no PowerShell: on a fresh profile
    PowerShell took 20 s or more to start, and every `heavy status` paid it."""
    import winreg
    names = []
    cls = r'SYSTEM\CurrentControlSet\Control\Class\{4d36e968-e325-11ce-bfc1-08002be10318}'
    try:
        with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, cls) as k:
            for i in range(winreg.QueryInfoKey(k)[0]):
                sub = winreg.EnumKey(k, i)
                if not sub.isdigit():
                    continue
                try:
                    with winreg.OpenKey(k, sub) as d:
                        name = str(winreg.QueryValueEx(d, 'DriverDesc')[0]).strip()
                except OSError:
                    continue
                if name and name not in names:
                    names.append(name)
    except OSError:
        pass
    return names


def run_quiet(cmd, timeout):
    """(exit code, stdout text) of a helper command, never longer than timeout. Output goes to temp files, not pipes,
    and a timeout ends the whole tree: a PowerShell that stalled (an empty profile on a fresh runner) left a child
    holding the pipes, and `heavy status` waited on them forever. Exit code 124 on a timeout."""
    with tempfile.TemporaryFile() as fo:
        try:
            p = subprocess.Popen(cmd, stdin=subprocess.DEVNULL, stdout=fo, stderr=subprocess.DEVNULL,
                                 creationflags=NO_WINDOW if WIN else 0)
        except OSError:
            return 127, ''
        try:
            code = p.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            if WIN:
                subprocess.run(['taskkill', '/T', '/F', '/PID', str(p.pid)], capture_output=True, creationflags=NO_WINDOW)
            p.kill()
            try:
                p.wait(timeout=10)
            except subprocess.TimeoutExpired:
                pass
            code = 124
        fo.seek(0)
        return code, fo.read().decode('utf-8', 'replace')


# Gradle and Kotlin daemons ------------------------------------------------------------------------------------------
def java_processes():
    """[(pid, kind, working set MB)], kind = gradle-daemon | kotlin-daemon | gradle-client | other."""
    if LINUX:
        out = []
        for pid in _proc_pids():
            try:
                with open(f'/proc/{pid}/comm', encoding='utf-8', errors='replace') as f:
                    if f.read().strip() != 'java':
                        continue
                with open(f'/proc/{pid}/cmdline', encoding='utf-8', errors='replace') as f:
                    out.append((pid, _java_kind(f.read()), _proc_rss_mb(pid)))
            except (OSError, ValueError, IndexError):
                continue
        return out
    if not WIN:
        return []
    try:   # no Java running: no need to start PowerShell at all
        if not any(n in ('java', 'javaw') for _, n, _ in _proc_table().values()):
            return []
    except Exception:
        pass
    _, text = run_quiet(['powershell', '-NoProfile', '-NonInteractive', '-Command',
                         "Get-CimInstance Win32_Process -Filter \"Name='java.exe' or Name='javaw.exe'\" | "
                         "Select-Object ProcessId,CommandLine,WorkingSetSize | ConvertTo-Json -Compress"], 30)
    try:
        rows = json.loads(text or '[]')
    except ValueError:
        return []
    out = []
    for row in rows if isinstance(rows, list) else [rows]:
        out.append((int(row['ProcessId']), _java_kind(row.get('CommandLine') or ''),
                    int(row.get('WorkingSetSize') or 0) // 2 ** 20))
    return out


def _java_kind(cmd):
    return ('gradle-daemon' if 'GradleDaemon' in cmd else 'kotlin-daemon' if 'KotlinCompileDaemon' in cmd
            else 'gradle-client' if ('GradleWrapperMain' in cmd or 'GradleMain' in cmd) else 'other')


def idle_daemons(sample_s=3.0):
    """(idle daemons [(pid, kind, MB)], why not) -- none while a Gradle build (a client) is running."""
    js = java_processes()
    if any(k == 'gradle-client' for _, k, _ in js):
        return [], 'a Gradle build is running'
    ds = [j for j in js if j[1] in ('gradle-daemon', 'kotlin-daemon')]
    if not ds:
        return [], 'none running'
    before = {pid: cpu_seconds(pid) for pid, _, _ in ds}
    time.sleep(sample_s)
    idle = []
    for pid, kind, mb in ds:
        a, b = before.get(pid), cpu_seconds(pid)
        if a is not None and b is not None and b - a < 0.1:
            idle.append((pid, kind, mb))
    return idle, ('' if idle else 'all busy')


def free_daemons(say=print, dry_run=False):
    idle, why = idle_daemons()
    if not idle:
        say(f'[heavy] no idle Gradle/Kotlin daemons to stop ({why})')
        return []
    for pid, kind, mb in idle:
        say(f'[heavy] {"would stop" if dry_run else "stopping"} idle {kind} pid {pid} ({mb / 1024:.1f} GB)')
        if not dry_run:
            if WIN:
                subprocess.run(['taskkill', '/F', '/PID', str(pid)], capture_output=True, creationflags=NO_WINDOW)
            else:
                try:
                    os.kill(pid, signal.SIGTERM)
                except OSError:
                    pass
    return idle


# ollama: its models sit on the GPU and in RAM, and apps reach it over HTTP, where no lane can see them -------------------
_no_proxy = urllib.request.build_opener(urllib.request.ProxyHandler({}))
_ollama_seen = {'at': 0.0, 'models': None, 'looking': False}


def _ollama(path, body=None, timeout=0.5):
    req = urllib.request.Request(settings()['ollama_url'].rstrip('/') + path, headers={'Content-Type': 'application/json'},
                                 data=None if body is None else json.dumps(body).encode())
    with _no_proxy.open(req, timeout=timeout) as r:
        return json.loads(r.read() or b'{}')


def ollama_models():
    """What Ollama has loaded now: [{'name', 'mb', 'gpu_mb'}]. None when it isn't running, [] when nothing is loaded."""
    try:
        return [{'name': m.get('name') or m.get('model') or '?', 'mb': int(m.get('size') or 0) >> 20,
                 'gpu_mb': int(m.get('size_vram') or 0) >> 20} for m in _ollama('/api/ps').get('models') or []]
    except (OSError, ValueError, AttributeError, TypeError):
        return None


def ollama_seen(max_age=10):
    """ollama_models() for a window that can't stop to wait: the last answer, refreshed behind it."""
    seen = _ollama_seen
    if time.time() - seen['at'] >= max_age and not seen['looking']:
        seen['looking'] = True

        def look():
            seen.update(models=ollama_models(), at=time.time(), looking=False)
        threading.Thread(target=look, daemon=True).start()
    return seen['models']


def ollama_text(models):
    """'qwen3:8b 5.2 GB, 4.8 on the GPU', or '' when nothing is loaded."""
    return ', '.join(f"{m['name']} {m['mb'] / 1024:.1f} GB" + (f", {m['gpu_mb'] / 1024:.1f} on the GPU" if m['gpu_mb'] else '')
                     for m in models or [])


def free_ollama(say=print, dry_run=False):
    """Unloads what Ollama has loaded. A request in progress finishes first (Ollama unloads the model once it's done) and
    the next one reloads it, so an app only ever waits a few seconds. Returns the models it asked to go."""
    models = ollama_models()
    if not models:
        return []
    for m in models:
        say(f"[heavy] {'would unload' if dry_run else 'unloading'} Ollama's {m['name']} ({m['mb'] / 1024:.1f} GB)")
        if not dry_run:
            try:
                _ollama('/api/generate', {'model': m['name'], 'keep_alive': 0}, timeout=3)
            except (OSError, ValueError) as e:
                say(f"[heavy] couldn't unload {m['name']}: {e}")
    if dry_run:
        return models
    deadline = time.time() + 3
    left = ollama_models()
    while left and time.time() < deadline:
        time.sleep(0.2)
        left = ollama_models()
    if left:
        say(f"[heavy] still in use, unloads when its request ends: {ollama_text(left)}")
    return models


# lanes ----------------------------------------------------------------------------------------------------------------
def slots(lane):
    """How many jobs the lane runs at once: cpu_slots for cpu (1 to 4), always 1 for gpu."""
    if lane != 'cpu':
        return 1
    try:
        return max(1, min(int(settings().get('cpu_slots', 1)), 4))
    except (TypeError, ValueError):
        return 1


def lock_path(lane, i=0):
    """Slot i's lock. Slot 0 keeps the old name, so older tools still see the first job."""
    return os.path.join(home(), f'{lane}.lock' if i == 0 else f'{lane}-{i + 1}.lock')


def queue_dir(lane):
    return os.path.join(home(), 'queue', lane)


def _slot_holder(lane, i):
    p = lock_path(lane, i)
    if not SUPPORTED or not os.path.exists(p):
        return None
    fd = _open_excl(p)
    if fd is not None:
        os.close(fd)
        return None
    return _read_json(p) or {'name': '(starting)'}


def holders(lane):
    """The jobs holding the lane's slots, slot order."""
    return [h for h in (_slot_holder(lane, i) for i in range(max(slots(lane), 1 if lane == 'gpu' else 4))) if h]


def holder(lane):
    """A job holding the lane (the first busy slot's record), or None when no slot is busy."""
    h = holders(lane)
    return h[0] if h else None


def queue(lane):
    """Waiting jobs for the lane, oldest first: [(ticket path, record)]. A ticket nobody holds open belongs to a
    waiter that is gone, so it is removed."""
    d = queue_dir(lane)
    out = []
    for f in sorted(os.listdir(d)) if os.path.isdir(d) else []:
        p = os.path.join(d, f)
        if f.endswith('.ticket') and not _remove_if_unheld(p):   # False: its waiter still holds it open
            out.append((p, _read_json(p)))
    return out


def pid_alive(pid, since_ts=None):
    """Is process pid running? Given since_ts, also that it was already running then (not a reuse of its id)."""
    if not SUPPORTED or not pid:
        return False
    if LINUX:
        st = _proc_stat(int(pid))
        if not st or st[0] in 'ZX':
            return False
        return since_ts is None or _boot_time() + int(st[19]) / os.sysconf('SC_CLK_TCK') <= since_ts + 2
    h = k32.OpenProcess(0x1000, False, int(pid))   # PROCESS_QUERY_LIMITED_INFORMATION
    if not h:
        return ctypes.get_last_error() == 5   # access denied: it exists, it just isn't ours to look at
    try:
        code = wintypes.DWORD()
        if not k32.GetExitCodeProcess(h, ctypes.byref(code)) or code.value != 259:   # STILL_ACTIVE
            return False
        if since_ts is not None:
            c, e, k, u = (wintypes.FILETIME() for _ in range(4))
            if k32.GetProcessTimes(h, ctypes.byref(c), ctypes.byref(e), ctypes.byref(k), ctypes.byref(u)):
                if (_ft(c) - 116444736000000000) / 1e7 > since_ts + 2:
                    return False
        return True
    finally:
        k32.CloseHandle(h)


def peek(lane):
    """The lane's holder, read without opening its lock, for monitors that poll often (holder() takes the lock for a
    moment, which could make a real job miss its turn). A job clears the record when it lets go, and a crashed job's
    record names a process that is gone."""
    p = peeks(lane)
    return p[0] if p else None


def peeks(lane):
    """Every live job on the lane's slots, read without opening a lock (see peek)."""
    out = []
    for i in range(1 if lane == 'gpu' else 4):
        r = _read_json(lock_path(lane, i))
        if r.get('pid') and pid_alive(r['pid'], r.get('queued_ts')):
            out.append(r)
    return out


def peek_queue(lane):
    """The lane's waiting jobs, oldest first, read without removing anything."""
    d = queue_dir(lane)
    out = []
    for f in sorted(os.listdir(d)) if os.path.isdir(d) else []:
        if not f.endswith('.ticket'):
            continue
        r = _read_json(os.path.join(d, f))
        if r.get('pid') and pid_alive(r['pid'], r.get('queued_ts')):
            out.append(r)
    return out


def job_view(r, queued=False):
    """A lane holder's or waiter's record as status shows it: state is running, waiting for RAM or queued."""
    now = time.time()
    if queued:
        state, since = 'queued', r.get('queued_ts')
    elif r.get('started_ts'):
        state, since = 'running', r['started_ts']
    else:
        state, since = 'waiting for RAM', r.get('lane_ts') or r.get('queued_ts')
    j = {'name': r.get('name', '(starting)'), 'state': state, 'for_s': round(now - since) if since else None,
         'since_queued_s': round(now - r['queued_ts']) if r.get('queued_ts') else None, 'pid': r.get('pid'),
         'cwd': r.get('cwd', ''), 'command': r.get('command', ''), 'expect_mb': r.get('expect_mb'),
         'threads': r.get('threads')}
    if state == 'waiting for RAM' and r.get('ram'):
        j.update(need_mb=r['ram']['need_mb'], free_mb=r['ram']['free_mb'])
    if state == 'running' and r.get('idle_since_ts'):
        j['stalled_s'] = round(now - r['idle_since_ts'])
    return j


# how long jobs usually take -----------------------------------------------------------------------------------------
_HIST = {'key': None, 'rows': []}


def history():
    """Every finished run in runs.jsonl, read again only when the file changes."""
    p = os.path.join(home(), 'runs.jsonl')
    try:
        st = os.stat(p)
    except OSError:
        return []
    key = (p, st.st_mtime_ns, st.st_size)
    if _HIST['key'] != key:
        rows = []
        try:
            with open(p, encoding='utf-8') as f:
                for line in f:
                    try:
                        r = json.loads(line)
                    except ValueError:
                        continue
                    if isinstance(r, dict) and r.get('ran_s') is not None:
                        rows.append(r)
        except OSError:
            pass
        _HIST.update(key=key, rows=rows)
    return _HIST['rows']


def name_key(name):
    """A job's name without its version numbers and run counters, so "GameDemo 0.56.0 full suite" matches
    "GameDemo 0.55.0 full suite" and "nightly 3" matches "nightly 5"."""
    s = re.sub(r'\bv?\d+(?:\.\d+)+(?:-\w+)?', ' ', (name or '').lower())
    s = re.sub(r'(?<=[a-z])\d+\b', '', s)
    s = re.sub(r'\b\d+\b', ' ', s)
    return ' '.join(s.split())


def command_key(command, cwd):
    """The command with paths cut to their last part and every digit gone, plus the folder it ran in."""
    parts = [re.sub(r'\d+', '', re.split(r'[\\/]', t)[-1]) for t in (command or '').lower().replace('"', ' ').split()]
    return os.path.normcase(cwd or ''), ' '.join(p for p in parts if p)


def usual_secs(name, command='', cwd=None):
    """(median run time of the last five matching runs, how many matched), or (None, 0) with no history. Failed runs
    and runs under a second don't count: neither says how long the job takes. Matched by name first, then by command
    and folder."""
    rows = [r for r in history() if r.get('exit') in (0, None) and r['ran_s'] >= 1]
    kn = name_key(name)
    m = [r for r in rows if name_key(r.get('name')) == kn] if kn else []
    if not m and command:
        kc = command_key(command, cwd)
        m = [r for r in rows if command_key(r.get('command'), r.get('cwd')) == kc]
    if not m:
        return None, 0
    last = sorted(r['ran_s'] for r in m[-5:])
    return round(last[len(last) // 2]), len(m)


def estimate(lanes, now=None):
    """Adds usual_s, runs and left_s (running) or starts_in_s (queued) to each job, from past run times, and returns
    {'done_in_s', 'done_at', 'unknown'}: when both lanes should be empty, and the jobs with no history (then done_in_s
    counts only what is known). A job waiting for RAM counts its whole usual time; the RAM wait itself can't be known."""
    now = time.time() if now is None else now
    ends, unknown = [], []
    for L in lanes:
        t, jobs = 0, ([L['job']] if L.get('job') else []) + list(L.get('queue') or [])
        for j in jobs:
            usual, n = usual_secs(j.get('name'), j.get('command'), j.get('cwd'))
            j['usual_s'], j['runs'] = usual, n
            if j['state'] == 'queued':
                j['starts_in_s'] = t
            if usual is None:
                unknown.append(j.get('name'))
                continue
            left = max(usual - (j.get('for_s') or 0), 0) if j['state'] == 'running' else usual
            if j['state'] == 'running':
                j['left_s'] = left
            t += left
        L['done_in_s'] = t
        ends.append(t)
    done = max(ends or [0])
    return {'done_in_s': done, 'done_at': datetime.fromtimestamp(now + done).strftime('%H:%M'), 'unknown': unknown}


def fmt_eta(s):
    """Rounded for a guess: 40s, 12m, 1h05m."""
    s = int(round(s or 0))
    return f'{s}s' if s < 90 else f'{round(s / 60)}m' if s < 3600 else f'{s // 3600}h{round(s % 3600 / 60):02d}m'


def eta_note(j):
    """'~12m left (usually 1h10m)', 'over its usual 1h10m', 'usually 3m', 'no past runs' for one job, or STALLED."""
    if j.get('stalled_s') is not None:
        return f"STALLED: no activity for {fmt_eta(j['stalled_s'])}"
    u = j.get('usual_s')
    if u is None:
        return 'no past runs'
    if j['state'] == 'running':
        return f"~{fmt_eta(j['left_s'])} left (usually {fmt_eta(u)})" if j.get('left_s') else f'over its usual {fmt_eta(u)}'
    return f'usually {fmt_eta(u)}'


def eta_line(eta, busy_now=True):
    if not busy_now:
        return 'Both lanes are empty.'
    unknown = eta['unknown']
    if unknown and not eta['done_in_s']:
        return f"Done: unknown ({len(unknown)} job{'s' if len(unknown) > 1 else ''} with no past runs)"
    return (f"All done in ~{fmt_eta(eta['done_in_s'])}, around {eta['done_at']}"
            + (f" (+ {len(unknown)} job{'s' if len(unknown) > 1 else ''} with no past runs)" if unknown else ''))


# shut down when the lanes are done ------------------------------------------------------------------------------------
def power_path():
    return os.path.join(home(), 'shutdown.json')


def power_state():
    """The "shut down when done" switch: {'armed_at': iso time} while it is on, else None."""
    r = _read_json(power_path())
    return r if r.get('armed_at') else None


def set_power(on):
    """Arm or disarm "shut down when done". The tray does the watching and the shutting down."""
    p = power_path()
    if on:
        os.makedirs(home(), exist_ok=True)
        with open(p, 'w', encoding='utf-8') as f:
            json.dump({'armed_at': now_iso()}, f)
    else:
        try:
            os.remove(p)
        except FileNotFoundError:
            pass
    return power_state()


def shutdown_command():
    """Windows shutdown with no force: an app with unsaved work can still stop it and ask. HEAVYGATE_SHUTDOWN_CMD
    replaces it (the self-tests point it at a script that only writes a file)."""
    alt = os.environ.get('HEAVYGATE_SHUTDOWN_CMD')
    return alt.split('|') if alt else ['shutdown', '/s', '/t', '0']


def describe(h):
    if not h:
        return 'free'
    j = job_view(h)
    ram = f": needs {j['need_mb'] / 1024:.1f} GB, {j['free_mb'] / 1024:.1f} GB free" if 'need_mb' in j else ''
    return f"{j['name']} ({j['state']} {fmt_secs(j['for_s']) if j['for_s'] is not None else '?'}{ram}, pid {j['pid'] or '?'}, {j['cwd']})"


class Busy(Exception):
    def __init__(self, lane, h):
        super().__init__(f'{lane} lane busy: {describe(h)}')
        self.lane, self.holder = lane, h


class Slot:
    """A held lane. Pass **slot.popen to Popen and start the child inside spawning() so it, and everything it starts,
    gets the lane's priority and processor share (creationflags on Windows, a nice and affinity step on Linux)."""
    def __init__(self, lane, name, nested=False, threads=0):
        self.lane, self.name, self.nested = lane, name, nested
        self.creationflags = BELOW_NORMAL if (lane == 'cpu' and not nested and WIN) else 0
        self.affinity = 0
        if lane == 'cpu' and not nested and WIN and threads:
            mine, system = ctypes.c_size_t(), ctypes.c_size_t()
            k32.GetProcessAffinityMask(k32.GetCurrentProcess(), ctypes.byref(mine), ctypes.byref(system))
            mask, n, bit = 0, 0, 0
            while n < threads and (1 << bit) <= system.value:
                if system.value & (1 << bit):
                    mask, n = mask | (1 << bit), n + 1
                bit += 1
            self.affinity = mask if mask and mask != system.value else 0
        self.popen = {'creationflags': self.creationflags} if WIN else {}
        if lane == 'cpu' and not nested and LINUX:
            cpus = sorted(os.sched_getaffinity(0))
            keep = set(cpus[:threads]) if 0 < threads < len(cpus) else None

            def lower():   # in the child, before it runs the command; nice can't be undone, so never in this process
                os.nice(CPU_LANE_NICE)
                if keep:
                    os.sched_setaffinity(0, keep)
            self.popen = {'preexec_fn': lower}
        self.exit = None
        self.warning = None
        self.mark_idle = lambda since_ts: None   # hold() points this at the lane record, so status can show a stall
        self.watching = lambda proc: None        # and this at its Watchdog: watch this child, not the whole process
        self.cap_mb = self.peak_mb = None        # the Job Object's RAM cap and what the job really used
        self.disk_ended = False                  # its DiskGuard ended it: a drive fell under disk_stop_gb

    @contextmanager
    def spawning(self):
        if not self.affinity:
            yield
            return
        me = k32.GetCurrentProcess()
        mine, system = ctypes.c_size_t(), ctypes.c_size_t()
        k32.GetProcessAffinityMask(me, ctypes.byref(mine), ctypes.byref(system))
        k32.SetProcessAffinityMask(me, self.affinity)   # a child takes its parent's mask when it is created
        try:
            yield
        finally:
            k32.SetProcessAffinityMask(me, mine.value)


class Sampler(threading.Thread):
    """Lowest free RAM and the CPU/GPU peaks while a job runs."""
    def __init__(self):
        super().__init__(daemon=True)
        self.stop = threading.Event()
        self.min_free = mem_mb()[0]
        self.cpu, self.cpu_max, self.cpu_sum, self.n = CpuMeter(), 0, 0, 0
        self.gpu_util = self.gpu_mem = self.gpu_temp = None

    def run(self):
        tick = 0
        while not self.stop.wait(5):
            self.min_free = min(self.min_free, mem_mb()[0])
            c = self.cpu.percent()
            self.cpu_max, self.cpu_sum, self.n = max(self.cpu_max, c), self.cpu_sum + c, self.n + 1
            if tick % 3 == 0:
                g = gpu_now()
                if g:
                    self.gpu_util = max(self.gpu_util or 0, g['util'])
                    self.gpu_mem = max(self.gpu_mem or 0, g['mem_mb'])
                    self.gpu_temp = max(self.gpu_temp or 0, g['temp'])
            tick += 1

    def summary(self):
        return {'min_free_mb': self.min_free, 'cpu_max': self.cpu_max,
                'cpu_avg': round(self.cpu_sum / self.n) if self.n else None,
                'gpu_util_max': self.gpu_util, 'gpu_mem_max_mb': self.gpu_mem, 'gpu_temp_max': self.gpu_temp}


class Watchdog(threading.Thread):
    """Notices a job that is alive but doing nothing. A live process proves nothing: a script stuck waiting on a
    leftover background process, or on a window nobody will see, holds its lane for ever. Real activity is CPU spent
    across the job's process tree, or a process starting or ending in it. It starts with the process holding the lane,
    so a program that calls hold() itself is covered; heavy's own command line narrows it to the job (slot.watching).
    After stall_min it flags. After stall_stop_min, and only with another job waiting behind it, it ends the job: that
    long with nothing happening is dead, or waiting on a dialog nobody is answering, and it is costing someone else."""
    def __init__(self, root, slot, name, say, s):
        super().__init__(daemon=True)
        self.root, self.slot, self.name, self.say, self.proc = root, slot, name, say, None
        self.stall_s, self.cpu_s, self.check_s = float(s['stall_min']) * 60, float(s['stall_cpu_s']), float(s['stall_check_s'])
        self.stop_s = float(s['stall_stop_min']) * 60
        self.stop = threading.Event()
        self.seen, self.spent, self.active, self.flagged, self.worst, self.ended = {}, 0.0, time.time(), False, 0, False

    def watch(self, proc):
        self.proc, self.root, self.seen = proc, proc.pid, {}

    def end_job(self, now):
        self.ended = True
        self.say(f'[heavy] {self.name}: ending it. No activity for {fmt_secs(now - self.active)} with another job waiting '
                 f'on the {self.slot.lane} lane')
        end_job_tree(self.proc, self.root)

    def look(self, now):
        cpu = {p: cpu_seconds(p) or 0 for p in [self.root] + _descendants(self.root)}
        changed = set(cpu) != set(self.seen)
        self.spent += sum(max(v - self.seen.get(p, 0), 0) for p, v in cpu.items())
        self.seen = cpu
        if changed or self.spent >= self.cpu_s:
            self.spent = 0.0
            if self.flagged:
                self.worst = max(self.worst, now - self.active)
                self.flagged = False
                self.slot.mark_idle(None)
                self.say(f'[heavy] {self.name}: active again')
            self.active = now
        elif not self.flagged and now - self.active >= self.stall_s:
            self.flagged = True
            self.slot.mark_idle(self.active)
            self.say(f'[heavy] {self.name}: STALLED, no activity for {fmt_secs(now - self.active)} and still holding the '
                     f'{self.slot.lane} lane. If it is stuck: heavy stop {self.slot.lane}')
        elif self.flagged and not self.ended and self.stop_s and now - self.active >= self.stop_s and peek_queue(self.slot.lane):
            self.end_job(now)

    def run(self):
        while not self.stop.wait(self.check_s):
            try:
                self.look(time.time())
            except Exception:   # a bad reading must never touch the job
                pass

    def finish(self):
        self.stop.set()
        self.join(timeout=5)   # it writes to the lane record, which closes after this
        if self.flagged:
            self.worst = max(self.worst, time.time() - self.active)
        if self.worst:
            note = f"stalled {fmt_secs(self.worst)}{', ended to free the lane' if self.ended else ''}"
            self.slot.warning = f'{self.slot.warning}; {note}' if self.slot.warning else note


def end_job_tree(proc, root):
    """End a job: heavy's own child and everything under it, or, for hold() inside a program, what that program
    started, so the program itself carries on."""
    if proc:
        kill_tree(proc)
        return
    for pid in [p for p, parent in _parents().items() if parent == root]:
        end_pid_tree(pid)


def end_pid_tree(pid):
    """True when it is gone."""
    if WIN:
        r = subprocess.run(['taskkill', '/T', '/F', '/PID', str(pid)], capture_output=True, creationflags=NO_WINDOW)
        return r.returncode == 0 or not pid_alive(pid)
    for p in [pid] + _descendants(pid):
        try:
            os.kill(p, signal.SIGKILL)
        except OSError:
            pass
    return not pid_alive(pid)


def ws_mb():
    """{pid: working set MB} for every process this user can see."""
    return {pid: mb for pid, _, mb in processes()}


class RamGuard(threading.Thread):
    """Ends the job when the PC goes past the RAM ceiling: over ram_ceiling_pct for ram_over_s seconds, or at
    ram_kill_pct at all. A compile that fills the RAM takes the desktop, the agents and every open file with it; a
    killed compile only has to be run again (with fewer parallel jobs). Checks every second."""
    def __init__(self, root, slot, name, say, s):
        super().__init__(daemon=True)
        self.root, self.slot, self.name, self.say, self.proc = root, slot, name, say, None
        self.ceiling, self.kill = float(s['ram_ceiling_pct']), float(s['ram_kill_pct'])
        self.over_s, self.min_mb = float(s['ram_over_s']), int(s['guard_min_mb'])
        self.stop = threading.Event()
        self.ended = None
        self.peak_pct = 0.0

    def watch(self, proc):
        self.proc, self.root = proc, proc.pid

    def job_mb(self):
        ws = ws_mb()
        pids = [self.root] + _descendants(self.root)
        if not self.proc:   # hold() inside a program: the program itself is not the job
            pids = pids[1:]
        return sum(ws.get(p, 0) for p in pids)

    def look(self, now, over_since):
        pct = ram_load(self.ceiling)[0]
        self.peak_pct = max(self.peak_pct, pct)
        if pct < self.ceiling:
            return None
        over_since = over_since or now
        if pct >= self.kill or now - over_since >= self.over_s:
            mb = self.job_mb()
            if mb < self.min_mb:
                return over_since   # not this job: the guard looks for the agent command that is
            self.ended = f'ended at {pct:.0f}% RAM (ceiling {self.ceiling:.0f}%), the job held {mb / 1024:.1f} GB'
            self.say(f'[heavy] {self.name}: ENDED. RAM reached {pct:.0f}%, over the {self.ceiling:.0f}% ceiling; this job '
                     f'held {mb / 1024:.1f} GB. Run it again with fewer parallel jobs (cargo -j 2, dotnet -m:2, '
                     f'gradle --max-workers 2) or when more RAM is free. Biggest now: {top_text()}')
            end_job_tree(self.proc, self.root)
            self.stop.set()
        return over_since

    def run(self):
        over_since = None
        while not self.stop.wait(1.0):
            try:
                over_since = self.look(time.time(), over_since)
            except Exception:   # a bad reading must never touch the job
                pass

    def finish(self):
        self.stop.set()
        self.join(timeout=5)
        if self.ended:
            self.slot.warning = f'{self.slot.warning}; {self.ended}' if self.slot.warning else self.ended


# disk: a job never fills the drive it builds on ------------------------------------------------------------------------
def drive_of(path):
    """The root whose free space counts for a path: 'C:\\' on Windows, its mount point elsewhere."""
    p = os.path.abspath(path)
    if WIN:
        d = os.path.splitdrive(p)[0]
        return (d.upper() if len(d) == 2 else d) + '\\' if d else p
    while not os.path.ismount(p):
        up = os.path.dirname(p)
        if up == p:
            break
        p = up
    return p


def watched_drives(cwd=None, s=None):
    """The drives a job here writes to: its folder's, the temp folder's and the user profile's (where Gradle, NuGet and
    npm keep their caches), plus disk_paths. Each once, in that order."""
    s = s or settings()
    out = []
    for p in [cwd or os.getcwd(), tempfile.gettempdir(), os.path.expanduser('~'), *(s.get('disk_paths') or [])]:
        try:
            d = drive_of(p)
        except (OSError, ValueError, TypeError):
            continue
        if os.path.normcase(d) not in {os.path.normcase(x) for x in out}:
            out.append(d)
    return out


def disk_free_gb(drive):
    """Free GB (GiB) on a drive, or None when it can't be read."""
    try:
        return shutil.disk_usage(drive).free / 2 ** 30
    except OSError:
        return None


def disk_free(drives):
    """{drive: free GB} for the drives that can be read."""
    out = {}
    for d in drives:
        g = disk_free_gb(d)
        if g is not None:
            out[d] = round(g, 1)
    return out


def disk_short(free, floor):
    """[(drive, free GB)] of the drives under floor."""
    return [(d, g) for d, g in free.items() if g < floor]


def disk_text(short, floor):
    return ', '.join(f'{d} has {g:.1f} GB free' for d, g in short) + f', under the {floor:g} GB floor'


class DiskLow(OSError):
    """hold() refused a job: a drive it writes to has less than disk_floor_gb free. errno is ENOSPC."""
    def __init__(self, name, short, floor):
        super().__init__(errno.ENOSPC, f'{name}: not started: {disk_text(short, floor)}')
        self.short, self.floor = short, floor


class DiskGuard(threading.Thread):
    """Ends the job when a drive it writes to falls under disk_stop_gb, before it fills it: a full drive breaks git
    and every program writing there, not just the build. Checks every 5 s; keeps the lowest free space it saw."""
    def __init__(self, root, slot, name, say, s, drives):
        super().__init__(daemon=True)
        self.root, self.slot, self.name, self.say, self.proc = root, slot, name, say, None
        self.stop_gb, self.drives = float(s['disk_stop_gb']), drives
        self.stop = threading.Event()
        self.ended = None
        self.low = {}

    def watch(self, proc):
        self.proc, self.root = proc, proc.pid

    def look(self):
        free = disk_free(self.drives)
        for d, g in free.items():
            self.low[d] = min(self.low.get(d, g), g)
        short = disk_short(free, self.stop_gb)
        if short:
            self.ended = 'ended: ' + disk_text(short, self.stop_gb)
            self.slot.disk_ended = True
            self.say(f'[heavy] {self.name}: ENDED. {disk_text(short, self.stop_gb)}: the job was stopped before it filled '
                     f'the drive. Free space there (old dist folders, caches), then run it again.')
            end_job_tree(self.proc, self.root)
            self.stop.set()
        return short

    def run(self):
        while not self.stop.wait(5.0):
            try:
                self.look()
            except Exception:   # a bad reading must never touch the job
                pass

    def finish(self):
        self.stop.set()
        self.join(timeout=5)
        if self.ended:
            self.slot.warning = f'{self.slot.warning}; {self.ended}' if self.slot.warning else self.ended


def _recent_reserved(lane):
    """RAM the other lane's job claimed but may not have taken yet (it started under 90 s ago)."""
    total = 0
    for other in LANES:
        if other == lane:
            continue
        for h in holders(other):
            if h.get('started_ts') and time.time() - h['started_ts'] < 90:
                total += int(h.get('expect_mb') or 0)
    return total


def _admit(lane, name, expect, s, say, report=None):
    """Wait for RAM. Returns (free MB when started, warning or None). report(free, need) is called while short, so
    status can say what the job is waiting for."""
    keep = ram_load(s['ram_ceiling_pct'])[2]   # MB the ceiling keeps free
    need = expect + max(int(s['reserve_mb']), keep)
    deadline = time.time() + 60 * float(s['ram_wait_min'])
    use_daemons, use_ollama = s.get('free_daemons', True), s.get('free_ollama', True)
    tried_free, last = not (use_daemons or use_ollama), 0
    if lane == 'gpu' and use_ollama:   # RAM can look fine while a model fills the GPU, so a GPU job always clears it
        free_ollama(say)
    while True:
        avail = mem_mb()[0] - _recent_reserved(lane)
        if avail >= need:
            return avail, None
        if report:
            report(avail, need)
        if not tried_free:
            tried_free = True
            freed = free_daemons(say) if use_daemons else []
            if use_ollama and lane != 'gpu':
                freed = free_ollama(say) or freed
            if freed:
                time.sleep(2)
                continue
        room = avail - keep   # what the job could use before the ceiling
        if time.time() >= deadline and room >= min(expect, 1536):
            warn = (f'started with {avail} MB free (wanted {need}) after waiting {s["ram_wait_min"]} min for RAM; '
                    f'{room} MB left under the {s["ram_ceiling_pct"]}% ceiling')
            say(f'[heavy] {name}: {warn}. Biggest: {top_text()}')
            return avail, warn
        if time.time() - last >= 60:
            past = time.time() >= deadline
            say(f'[heavy] {name}: waiting for RAM: {avail} MB free, wants {need} MB'
                + (f' (only {room} MB left under the {s["ram_ceiling_pct"]}% ceiling, so it will not start anyway)'
                   if past else '') + f'. Biggest: {top_text()}')
            last = time.time()
        time.sleep(5)


def _log(rec):
    try:
        os.makedirs(home(), exist_ok=True)
        with open(os.path.join(home(), 'runs.jsonl'), 'a', encoding='utf-8') as f:
            f.write(json.dumps(rec) + '\n')
    except OSError:
        pass


def _say(s):
    print(s, file=sys.stderr, flush=True)


def check_threads(lane, threads):
    """threads is a share of the CPU lane; gpu jobs have no limit to change."""
    if threads is None:
        return
    if lane != 'cpu':
        raise ValueError(f'--threads is for the cpu lane; {lane} jobs already use every thread')
    if not isinstance(threads, int) or isinstance(threads, bool) or threads < 1:
        raise ValueError(f'--threads must be 1 or more, not {threads!r}')


@contextmanager
def hold(lane, name, expect_mb=None, say=None, wait=True, command='', cwd=None, threads=None, max_wait_s=None):
    """Hold a lane for the length of the with-block (queue, then wait for RAM). Re-entrant: inside a held lane, in
    this process or a child of it, every hold() passes straight through. threads overrides cpu_threads for this job;
    more than the machine has means all of them."""
    if lane not in LANES:
        raise ValueError(f'lane must be one of {", ".join(LANES)}, not {lane!r}')
    check_threads(lane, threads)
    say = say or _say
    held = os.environ.get('HEAVYGATE_HELD')
    if not SUPPORTED or os.environ.get('HEAVYGATE', '').lower() in ('off', '0', 'no', 'false'):
        yield Slot(lane, name, nested=True)
        return
    if held:
        if threads:
            say(f'[heavy] {name}: runs in the {held} job already holding a lane, so --threads {threads} does not apply')
        yield Slot(lane, name, nested=True)
        return
    s = settings()
    expect = int(expect_mb if expect_mb is not None else s['expect_mb'].get(lane, 2000))
    cores = os.cpu_count() or 1
    share = threads or int(s['cpu_threads']) or -(-3 * cores // 4)
    os.makedirs(queue_dir(lane), exist_ok=True)
    rec = {'name': name, 'lane': lane, 'pid': os.getpid(), 'cwd': cwd or os.getcwd(), 'command': (command or '')[:400],
           'queued': now_iso(), 'queued_ts': time.time(), 'expect_mb': expect,
           'threads': min(share, cores) if lane == 'cpu' else None}
    ticket = os.path.join(queue_dir(lane), f'{time.time_ns():020d}-{os.getpid()}.ticket')
    tfd = _open_excl(ticket, create_new=True)
    if tfd is None:
        raise OSError(f'could not make a queue ticket {ticket}')
    _write(tfd, rec)
    lfd, t0, last = None, time.time(), 0
    try:
        while True:
            q = queue(lane)
            pos = next((i for i, (p, _) in enumerate(q) if os.path.normcase(p) == os.path.normcase(ticket)), 0)
            n = slots(lane)
            if pos < n:   # among the first n in line: take any free slot
                for i in range(n):
                    lfd = _open_excl(lock_path(lane, i))
                    if lfd is not None:
                        rec['slot'] = i
                        break
                if lfd is not None:
                    break
            if not wait or (max_wait_s is not None and time.time() - t0 >= max_wait_s):
                raise Busy(lane, holder(lane))
            if time.time() - last >= 60:
                say(f'[heavy] {lane} lane busy: {describe(holder(lane))}; {name} is #{pos + 1} in line'
                    + (f' (waited {fmt_secs(time.time() - t0)})' if time.time() - t0 > 5 else ''))
                last = time.time()
            time.sleep(float(s['poll_s']))
    finally:
        os.close(tfd)
        try:
            os.remove(ticket)
        except OSError:
            pass
    rec['lane_ts'] = time.time()
    slot = Slot(lane, name, threads=share)
    sampler = dog = guard = disk = None
    old_env = os.environ.get('HEAVYGATE_HELD')
    drives = watched_drives(rec['cwd'], s)
    disk_start = {}

    def report(free, need):
        rec['ram'] = {'need_mb': need, 'free_mb': free}
        _write(lfd, rec)

    def mark_idle(since_ts):
        rec['idle_since_ts'] = since_ts
        _write(lfd, rec)

    slot.mark_idle = mark_idle
    try:
        _write(lfd, rec)
        floor = float(s['disk_floor_gb'])
        disk_start = disk_free(drives)
        short = disk_short(disk_start, floor)
        if short:
            say(f'[heavy] {name}: NOT STARTED. {disk_text(short, floor)} (disk_floor_gb). Free space there first: '
                f'old dist folders and build caches are the usual bulk.')
            _log({'name': name, 'lane': lane, 'cwd': rec['cwd'], 'command': rec['command'], 'queued': rec['queued'],
                  'started': now_iso(), 'lane_wait_s': round(rec['lane_ts'] - t0, 1), 'ram_wait_s': 0, 'ran_s': 0,
                  'exit': DISK_EXIT, 'warning': 'not started: ' + disk_text(short, floor), 'disk_free_start_gb': disk_start})
            raise DiskLow(name, short, floor)
        free_start, slot.warning = _admit(lane, name, expect, s, say, report)
        rec.pop('ram', None)
        rec.update(started=now_iso(), started_ts=time.time())
        _write(lfd, rec)
        if time.time() - t0 > 5:
            say(f'[heavy] {name}: starting on the {lane} lane after waiting {fmt_secs(time.time() - t0)}')
        os.environ['HEAVYGATE_HELD'] = lane
        sampler = Sampler()
        sampler.start()
        dog = Watchdog(os.getpid(), slot, name, say, s)
        guard = RamGuard(os.getpid(), slot, name, say, s)
        disk = DiskGuard(os.getpid(), slot, name, say, s, drives)
        disk_start = disk_free(drives) or disk_start   # again: the RAM wait may have been long

        def watching(proc):
            dog.watch(proc)
            guard.watch(proc)
            disk.watch(proc)
        slot.watching = watching
        dog.start()
        guard.start()
        disk.start()
        yield slot
    finally:
        if dog:
            dog.finish()
        if guard:
            guard.finish()
        if disk:
            disk.finish()
        if old_env is None:
            os.environ.pop('HEAVYGATE_HELD', None)
        else:
            os.environ['HEAVYGATE_HELD'] = old_env
        if sampler:
            sampler.stop.set()
            sampler.join(timeout=10)
            started = rec.get('started_ts') or time.time()
            disk_end = disk_free(drives)
            here = drives[0] if drives else None
            _log({'name': name, 'lane': lane, 'cwd': rec['cwd'], 'command': rec['command'], 'queued': rec['queued'],
                  'disk_drive': here, 'disk_free_start_gb': disk_start, 'disk_free_end_gb': disk_end,
                  'disk_used_gb': round(disk_start[here] - disk_end[here], 1)
                  if here in disk_start and here in disk_end else None,
                  'disk_low_gb': {d: round(g, 1) for d, g in disk.low.items()} if disk else None,
                  'started': rec.get('started'), 'waited_s': round(started - t0, 1),
                  'lane_wait_s': round(rec['lane_ts'] - t0, 1), 'ram_wait_s': round(started - rec['lane_ts'], 1),
                  'ran_s': round(time.time() - started, 1), 'exit': slot.exit, 'free_start_mb': free_start,
                  'threads': rec['threads'], 'warning': slot.warning, 'ram_cap_mb': slot.cap_mb,
                  'job_peak_mb': slot.peak_mb, 'ram_peak_pct': round(guard.peak_pct, 1) if guard else None,
                  **sampler.summary()})
        try:
            _write(lfd, {})   # so peek() sees the lane free even while this process lives on (a test runner that embeds HeavyGate)
        except OSError:
            pass
        os.close(lfd)


# rules: which lane a command belongs in -------------------------------------------------------------------------------
STARTER_RULES = [{'match': m, 'folder': '', 'lane': lane, 'threads': None} for m, lane in (
    # Only jobs that really take the machine. dotnet build/test, build.cmd, msbuild, tar and 7z run ungated since 1.17.0
    # (11-08..15: 228 of them used a median 75 MB yet waited 277 min in line); the RAM guard still watches them.
    ('gradlew', 'cpu'), ('gradle', 'cpu'), ('dotnet publish', 'cpu'),
    ('blender', 'gpu'), ('shot.py', 'gpu'), ('desktop_check.py', 'gpu'),
    ('build-android.cmd', 'cpu'), ('cargo', 'cpu'))]
WRAPPERS = {'python', 'python3', 'pythonw', 'py', 'node', 'cmd', 'pwsh', 'powershell', 'bash', 'sh', 'call', 'start'}
PLAIN_NAMES = {'build', 'test', 'tests', 'run', 'check', 'release', 'publish', 'main', 'selftest', 'gradlew'}


def rules_path():
    return os.path.join(home(), 'rules.json')


def clean_rule(r):
    """A rule as stored, or None when it can't be used. A bad rule is dropped, never allowed to stop a job."""
    if not isinstance(r, dict):
        return None
    match, folder = str(r.get('match') or '').strip(), str(r.get('folder') or '').strip()
    lane, threads = r.get('lane'), r.get('threads')
    if lane not in LANES or not (match or folder):
        return None
    if lane == 'gpu' or threads in (None, 0, ''):
        threads = None
    elif threads != 'all' and (not isinstance(threads, int) or isinstance(threads, bool) or threads < 1):
        return None
    return {'match': match, 'folder': folder, 'lane': lane, 'threads': threads}


def read_rules():
    """{'rules': [...], 'dismissed': [...]}. With no rules file, the starter rules."""
    try:
        with open(rules_path(), encoding='utf-8') as f:
            d = json.load(f)
        rules = [c for c in map(clean_rule, d.get('rules', [])) if c]
        return {'rules': rules, 'dismissed': [str(x) for x in d.get('dismissed', [])]}
    except (OSError, ValueError, AttributeError, TypeError):
        return {'rules': [dict(r) for r in STARTER_RULES], 'dismissed': []}


def _write_json(path, data):
    """Written beside the target and swapped in, so a job reading it never sees half a file."""
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = f'{path}.{os.getpid()}.tmp'
    with open(tmp, 'w', encoding='utf-8') as f:
        json.dump(data, f, indent=2)
    os.replace(tmp, path)


def save_rules(rules, dismissed=()):
    _write_json(rules_path(), {'rules': [c for c in map(clean_rule, rules) if c], 'dismissed': list(dismissed)})


def set_setting(key, value):
    """Changes one key of settings.json and keeps the rest. None removes it, back to the default."""
    p = os.path.join(home(), 'settings.json')
    try:
        with open(p, encoding='utf-8') as f:
            s = json.load(f)
    except (OSError, ValueError):
        s = {}
    if value is None:
        s.pop(key, None)
    else:
        s[key] = value
    _write_json(p, s)


def usual_threads():
    """How many logical processors a CPU lane job gets with no rule or flag."""
    cores = os.cpu_count() or 1
    return min(int(settings()['cpu_threads']) or -(-3 * cores // 4), cores)


def rule_matches(rule, command, cwd=''):
    """match is whole words anywhere in the command, so "tar" finds tar.exe but not start. folder is any part of the
    working folder. A rule with both needs both, and one with neither matches nothing."""
    if not (rule.get('match') or rule.get('folder')):
        return False
    if rule.get('match') and not re.search(r'(?<![\w-])' + re.escape(rule['match']) + r'(?![\w-])', command or '', re.I):
        return False
    return not rule.get('folder') or rule['folder'].lower().replace('/', '\\') in (cwd or '').lower().replace('/', '\\')


def route(command, cwd=None, rules=None):
    """Where a command goes: {'lane', 'threads' (None = the usual share), 'rule', 'index', 'why'}. The first rule that
    matches wins; a command no rule knows takes the CPU lane, which can't fight a 3D job for the GPU."""
    rules = read_rules()['rules'] if rules is None else rules
    cwd = os.getcwd() if cwd is None else cwd
    for i, r in enumerate(rules):
        if rule_matches(r, command, cwd):
            threads = (os.cpu_count() or 1) if r.get('threads') == 'all' else r.get('threads')
            what = ' in '.join(x for x in (r.get('match'), r.get('folder')) if x)
            return {'lane': r['lane'], 'threads': threads, 'rule': r, 'index': i,
                    'why': f'{r["lane"]} lane' + (f' on {threads} threads' if threads else '') + f' (rule {i + 1}: {what})'}
    return {'lane': 'cpu', 'threads': None, 'rule': None, 'index': None, 'why': 'cpu lane (no rule matched)'}


def command_tool(command):
    """The program or script a command line is really about: python tools/render.py -q -> render.py."""
    for t in re.findall(r'"[^"]+"|\S+', command or ''):
        t = t.strip('"')
        if t.startswith('-') or re.fullmatch(r'/[a-zA-Z]', t):
            continue
        base = re.split(r'[\\/]', t)[-1].lower()
        if base.endswith('.exe'):
            base = base[:-4]
        if base and base not in WRAPPERS:
            return base
    return ''


def _when(r):
    try:
        start = datetime.fromisoformat(r.get('started') or '').timestamp()
    except (TypeError, ValueError):
        return None
    return start, start + float(r.get('ran_s') or 0)


def suggestions(rows=None, data=None, limit=4):
    """Rules worth adding, from what past runs really used: [{'key', 'match', 'folder', 'lane', 'threads', 'text'}].
    GPU readings are for the whole machine, so a run beside a GPU lane job says nothing and is skipped. It takes three
    runs of 20 s or more that agree, and a command a rule already covers is left alone."""
    rows = history() if rows is None else rows
    data = read_rules() if data is None else data
    busy_gpu = [w for w in (_when(r) for r in rows if r.get('lane') == 'gpu') if w]
    groups = {}
    for r in rows:
        tool = command_tool(r.get('command'))
        if not tool or float(r.get('ran_s') or 0) < 20 or r.get('gpu_util_max') is None:
            continue
        folder = ''
        if os.path.splitext(tool)[0] in PLAIN_NAMES:   # build.cmd says nothing without its project
            folder = re.split(r'[\\/]', (r.get('cwd') or '').rstrip('\\/'))[-1]
        groups.setdefault((tool, folder, r.get('lane')), []).append(r)
    found = []
    for (tool, folder, lane), runs in groups.items():
        key = f'{tool}|{folder}|{lane}'
        last = runs[-5:]
        if len(last) < 3 or key in data['dismissed'] or any(rule_matches(x, last[-1].get('command'), last[-1].get('cwd'))
                                                            for x in data['rules']):
            continue
        name = tool + (f' in {folder}' if folder else '')
        total = sum(float(r['ran_s']) for r in last)
        if lane == 'cpu':
            alone = []
            for r in last:
                w = _when(r)
                if w and not any(a < w[1] and w[0] < b for a, b in busy_gpu):
                    alone.append(r)
            low = min((r['gpu_util_max'] for r in alone), default=0)
            if len(alone) >= 3 and low >= 60:
                found.append((total, {'key': key, 'match': tool, 'folder': folder, 'lane': 'gpu', 'threads': None,
                                      'text': f'{name} kept the GPU at {low:.0f}% or more in its last {len(alone)} runs. '
                                              'It belongs in the GPU lane.'}))
                continue
            limit_pct = 100 * usual_threads() / (os.cpu_count() or 1)
            avg = sorted(r.get('cpu_avg') or 0 for r in last)[len(last) // 2]
            if limit_pct < 100 and avg >= 0.85 * limit_pct:
                found.append((total, {'key': key, 'match': tool, 'folder': folder, 'lane': 'cpu', 'threads': 'all',
                                      'text': f'{name} sat near its CPU limit ({avg:.0f}% of the machine). '
                                              'All threads would let it finish sooner.'}))
        elif lane == 'gpu':
            high = max(r['gpu_util_max'] for r in last)
            if high < 15:
                found.append((total, {'key': key, 'match': tool, 'folder': folder, 'lane': 'cpu', 'threads': None,
                                      'text': f'{name} never took the GPU past {high:.0f}% in {len(last)} runs. '
                                              'It could leave the GPU lane free.'}))
    return [s for _, s in sorted(found, key=lambda x: -x[0])[:limit]]


# command line -------------------------------------------------------------------------------------------------------
def local_script(line, cwd):
    """'build.cmd x' -> '.\\build.cmd x' when the script is in cwd (cmd.exe may skip the current folder), and
    './gradlew x' -> '.\\gradlew x', because cmd.exe reads a / in the program's path as a switch."""
    m = re.match(r'"([^"]+)"|(\S+)', line) if WIN else None
    tok = m and (m.group(1) or m.group(2))
    if tok and '/' in tok and not tok.startswith('/') and '://' not in tok:
        line = line[:m.start()] + m.group(0).replace('/', '\\') + line[m.end():]
    first = line.split(' ', 1)[0].strip('"')
    if WIN and first.lower().endswith(('.cmd', '.bat', '.exe')) and os.sep not in first and '/' not in first \
            and os.path.isfile(os.path.join(cwd, first)):
        return '.\\' + line
    return line


def kill_tree(p):
    if WIN:
        subprocess.run(['taskkill', '/T', '/F', '/PID', str(p.pid)], capture_output=True, creationflags=NO_WINDOW)
        return
    tree = [p.pid] + _descendants(p.pid)   # the shell and whatever it started, found before the shell goes
    for sig, wait_s in ((signal.SIGTERM, 10), (signal.SIGKILL, 2)):
        for pid in tree:
            try:
                os.kill(pid, sig)
            except OSError:
                pass
        end = time.time() + wait_s
        while time.time() < end:
            p.poll()   # reaps the shell, so it stops counting as alive
            tree = [pid for pid in tree if pid_alive(pid)]
            if not tree:
                return
            time.sleep(0.1)


def _parents():
    """{pid: parent pid} for every process, minus HeavyGate's own GPU readings, which would look like job activity."""
    if not WIN:
        out = {}
        for pid in _proc_pids():
            st = _proc_stat(pid)
            try:
                with open(f'/proc/{pid}/comm', encoding='utf-8', errors='replace') as f:
                    name = f.read().strip()
            except OSError:
                continue
            if st and name != 'nvidia-smi':
                out[pid] = int(st[1])
        return out

    class Entry(ctypes.Structure):
        _fields_ = [('size', wintypes.DWORD), ('usage', wintypes.DWORD), ('pid', wintypes.DWORD),
                    ('heap', ctypes.c_size_t), ('module', wintypes.DWORD), ('threads', wintypes.DWORD),
                    ('parent', wintypes.DWORD), ('base', ctypes.c_long), ('flags', wintypes.DWORD),
                    ('exe', ctypes.c_wchar * 260)]
    snap_fn, first, nxt, close = k32.CreateToolhelp32Snapshot, k32.Process32FirstW, k32.Process32NextW, k32.CloseHandle
    snap_fn.restype = wintypes.HANDLE
    first.argtypes = nxt.argtypes = [wintypes.HANDLE, ctypes.POINTER(Entry)]
    snap = snap_fn(2, 0)   # TH32CS_SNAPPROCESS
    if snap in (None, wintypes.HANDLE(-1).value):
        return {}
    out, e = {}, Entry()
    e.size = ctypes.sizeof(Entry)
    try:
        ok = first(snap, ctypes.byref(e))
        while ok:
            if e.exe.lower() != 'nvidia-smi.exe':
                out[e.pid] = e.parent
            ok = nxt(snap, ctypes.byref(e))
    finally:
        close(wintypes.HANDLE(snap))
    return out


def _descendants(root):
    kids = {}
    for pid, parent in _parents().items():
        kids.setdefault(parent, []).append(pid)
    out, todo = [], [root]
    while todo:
        for pid in kids.get(todo.pop(), []):
            out.append(pid)
            todo.append(pid)
    return out


def _interrupt(*_):
    raise KeyboardInterrupt


def cmd_run(lane, a):
    command = list(a.command)
    if not command:
        print('heavy: nothing to run (heavy <lane> -n NAME -- command ...)', file=sys.stderr)
        return 2
    line = local_script(command[0] if len(command) == 1 else
                        subprocess.list2cmdline(command) if WIN else shlex.join(command), os.getcwd())
    if not WIN:   # a stopped heavy takes its job with it, as Ctrl+C does
        signal.signal(signal.SIGTERM, _interrupt)
        signal.signal(signal.SIGHUP, _interrupt)
    name = a.name or line[:60]
    threads = a.threads
    if lane == 'auto':
        r = route(line)
        lane = r['lane']
        if threads is None:
            threads = r['threads']
        print(f"[heavy] {name}: {r['why']}", file=sys.stderr, flush=True)
    code = cloud_first(lane, a, line, name)
    if code is not None:
        return code
    try:
        check_threads(lane, threads)
    except ValueError as e:
        print(f'heavy: {e}', file=sys.stderr)
        return 2
    try:
        with hold(lane, name, a.expect, wait=not a.no_wait, command=line, threads=threads,
                  max_wait_s=getattr(a, 'wait_max', None)) as slot:
            job = None
            if WIN and not slot.nested:
                s = settings()
                room = ram_load(s['ram_ceiling_pct'])[1]
                cap = max(room, 512)
                job = job_with_cap(cap)
                if job:
                    slot.cap_mb = cap
                    print(f'[heavy] {name}: RAM cap {cap / 1024:.1f} GB (what is left under the {s["ram_ceiling_pct"]}% '
                          f'ceiling); past it the job\'s allocations fail', file=sys.stderr, flush=True)
                else:
                    print(f'[heavy] {name}: could not make a RAM-capped job object; the RAM guard still ends it past '
                          f'{s["ram_ceiling_pct"]}%', file=sys.stderr, flush=True)
            with slot.spawning():
                if job:
                    p, inside = start_in_job(line, job, **slot.popen)
                    if not inside:
                        slot.cap_mb = None
                        print(f'[heavy] {name}: Windows would not put it in the capped job; the RAM guard still ends it '
                              f'past the ceiling', file=sys.stderr, flush=True)
                else:
                    p = subprocess.Popen(line, shell=True, **slot.popen)
            slot.watching(p)
            try:
                slot.exit = p.wait()
            except KeyboardInterrupt:
                kill_tree(p)
                slot.exit = 130
            if slot.disk_ended:
                slot.exit = DISK_EXIT
            if job:
                slot.peak_mb = job_peak_mb(job)
                if slot.cap_mb and slot.peak_mb and slot.peak_mb >= slot.cap_mb * 0.97:
                    note = f'hit its RAM cap of {slot.cap_mb / 1024:.1f} GB'
                    slot.warning = f'{slot.warning}; {note}' if slot.warning else note
                    print(f'[heavy] {name}: {note}. If it failed for memory, run it with fewer parallel jobs '
                          f'(cargo -j 2, dotnet -m:2, gradle --max-workers 2)', file=sys.stderr, flush=True)
                k32.CloseHandle(job)
            return slot.exit
    except Busy as b:
        print(f'heavy: {b}', file=sys.stderr)
        return BUSY_EXIT
    except DiskLow:   # hold() already said which drive and why
        return DISK_EXIT
    except KeyboardInterrupt:   # stopped while still in the queue or waiting for RAM
        return 130


def cloud_first(lane, a, line, name):
    """Auto mode: a CPU job mapped to a GitHub workflow may run there instead. Returns the exit code when it went to
    GitHub (or must be rerun in the background), None to run it here. Never for the GPU lane, a job inside another
    (it shares its parent's slot), --no-cloud or HEAVYGATE_CLOUD=off, and anything that goes wrong before GitHub has
    started the build falls back to running here."""
    if lane == 'gpu' or getattr(a, 'no_cloud', False) or os.environ.get('HEAVYGATE_HELD') \
            or os.environ.get('HEAVYGATE_CLOUD', '').lower() == 'off':
        return None
    s = settings()
    asked = getattr(a, 'cloud', False)
    if not (s.get('cloud') and (s.get('cloud_auto') or asked)):
        return None
    say = cloud_say(name)
    try:
        plan = cloud_plan(line, os.getcwd(), name, lane, s, caps=not asked, asked=asked)
    except Exception as e:
        say(f'cloud lane check failed ({type(e).__name__}: {e}); running it here')
        return None
    if plan is None:
        if asked:
            say('--cloud: no mapping matches this command (heavy cloud map), so it runs here')
        return None
    job = plan['job']
    if plan['where'] != 'cloud':
        say(f"mapped to {job['repo']} {job['workflow']} but runs here ({plan['rule']}): {plan['reason']}")
        return None
    if getattr(a, 'wait_max', None) is not None and not a.no_wait:   # a foreground agent call: build here, don't wait on GitHub
        say(f"mapped to {job['repo']} {job['workflow']} but runs here: a foreground call can't wait for GitHub")
        return None
    if a.no_wait:   # a foreground agent call would hit its timeout waiting for GitHub: same contract as a busy lane
        say(f"goes to GitHub ({job['repo']} {job['workflow']}, about {plan['est_min']:g} min): run it in the background, "
            f"where it waits for the build")
        return BUSY_EXIT
    say(f"to GitHub ({plan['rule']}): {plan['reason']} About {plan['cost_min']} quota min ({plan['est_src']}); "
        f"{plan['ready']['reason']}.")
    rec = heavycloud.round_trip(cloud(s), job, plan['ready'], name, say, auto=not asked, rule=plan['rule'],
                                est=plan['est_min'])
    if rec['exit'] == 3:
        say('nothing started on GitHub; running it here instead')
        return None
    return rec['exit']


def cmd_why(a):
    """heavy why -- COMMAND: where heavy auto would send it, without running it."""
    if not a.command:
        print('heavy: nothing to check (heavy why -- command ...)', file=sys.stderr)
        return 2
    line = subprocess.list2cmdline(a.command) if WIN else shlex.join(a.command)
    print(route(a.command[0] if len(a.command) == 1 else line)['why'])
    return 0


# the agent hook: a heavy command an agent runs goes through heavy auto without anyone asking ---------------------------
GATED = {'heavy', 'heavygate.py'}          # these take the lanes themselves; add your own in gated_tools
QUICK_ARGS = {'--version', '-v', '--help', '-h', '/?', '--stop', '--status'}
SEPARATORS = ('&&', '||', ';', '|', '\n', '\r')


def split_commands(line, shell='bash'):
    """[(start, end)] of each simple command in a shell line, or None for anything this can't read for certain (a
    subshell, a script block, a heredoc). Unread means left alone: a job that skips the lanes beats a broken command."""
    if re.search(r"""<<|\$\(|@'|@\"""", line):
        return None
    out, start, i, quote = [], 0, 0, None
    while i < len(line):
        c = line[i]
        if quote:
            if c == '\\' and quote == '"' and shell == 'bash':
                i += 1
            elif c == quote:
                quote = None
        elif c in '\'"':
            quote = c
        elif c == '\\' and shell == 'bash':
            i += 1
        elif c in '(){}`':
            return None
        else:
            sep = next((x for x in SEPARATORS if line.startswith(x, i)), None)
            lone = c == '&' and line[start:i].strip() and '>' not in line[i - 1:i + 2]   # not 2>&1, not PowerShell's call &
            if sep or lone:
                out.append((start, i))
                i += len(sep or c)
                start = i
                continue
        i += 1
    if quote:
        return None
    out.append((start, len(line)))
    return out


def _is_quick(tool, args):
    """Asking a tool something, not giving it work. This shouldn't wait behind an hour of Gradle."""
    if QUICK_ARGS & {x.lower() for x in args}:
        return True
    first = args[0].lower().lstrip('-') if args else ''
    return (tool == 'tar' and 't' in first and not set('cxru') & set(first)) or (tool == '7z' and first in ('l', 'i', 'h'))


def _names(head, rule, cwd):
    m = rule.get('match', '').lower()
    return bool(m) and re.match(re.escape(m) + r'(?![\w-])', head) is not None \
        and (not rule.get('folder') or rule_matches({'folder': rule['folder']}, '', cwd))


def heavy_at(line, start, end, rules, cwd, shell, gated=GATED):
    """Reads one simple command: (from, to), the span a wrapper replaces; 'gated' when it already runs on a lane; None
    when no rule names it. A rule has to name the program being run, not a word in its arguments, so
    git commit -m "fix gradle" is left alone."""
    toks = [(m.start() + start, m.group(0)) for m in re.finditer(r'''(?:"[^"]*"|'[^']*'|\S)+''', line[start:end])]
    i, wrap = 0, None
    while i < len(toks) and shell == 'bash' and re.match(r'[A-Za-z_]\w*=', toks[i][1]):
        i += 1
    while i < len(toks) and toks[i][1] == '&':
        wrap = toks[i][0] if wrap is None else wrap
        i += 1
    if i >= len(toks):
        return None
    program = toks[i][0]
    wrap = program if wrap is None else wrap
    base = name = ''
    while i < len(toks):
        t = toks[i][1].strip('\'"')
        base = re.split(r'[\\/]', t)[-1].lower()
        name = re.sub(r'\.(exe|cmd|bat)$', '', base)
        if not (t.startswith('-') or re.fullmatch(r'/[a-zA-Z]', t)) and name not in WRAPPERS:
            break
        i += 1
    else:
        return None
    if name in gated or base in gated:
        return 'gated'
    args = [t.strip('\'"') for _, t in toks[i + 1:]]
    args = [a for a in args if not re.match(r'\d?[<>]', a)]
    if _is_quick(name, args):
        return None
    if WIN and shell == 'bash' and base.endswith('.sh'):   # the lane runs jobs through cmd.exe, which can't run this
        return None
    heads = (' '.join([base] + args).lower(), ' '.join([name] + args).lower())
    return (wrap, program) if any(_names(h, r, cwd) for h in heads for r in rules) else None


def gate_prefix(shell, wait):
    """The start of a line that runs a command on its lane. Full paths, so it works with nothing on PATH."""
    py, gate = sys.executable, os.path.abspath(__file__)
    if WIN:
        py, gate = '"' + py.replace(os.sep, '/') + '"', '"' + gate.replace(os.sep, '/') + '"'
    else:
        py, gate = shlex.quote(py), shlex.quote(gate)
    return ('& ' if shell == 'powershell' else '') + f'{py} {gate} auto ' + (
        '' if wait else f"--wait-max {int(settings()['foreground_wait_s'])} ") + '-- '


def hook_rewrite(line, shell='bash', wait=False, cwd=None, rules=None, gated=None):
    """The same shell line with each heavy command put on its lane, or None when there's nothing to change. Only the
    heavy command is wrapped, so the shell still does the cd, the pipes and the redirects as written."""
    rules = read_rules()['rules'] if rules is None else rules
    if gated is None:
        gated = GATED | {re.sub(r'\.(exe|cmd|bat)$', '', g.lower()) for g in settings().get('gated_tools', [])}
    parts = split_commands(line, shell)
    if parts is None:
        return None
    found = [heavy_at(line, a, b, rules, cwd or '', shell, gated) for a, b in parts]
    if 'gated' in found or not any(found):
        return None
    prefix = gate_prefix(shell, wait)
    for hit in reversed([f for f in found if f]):
        line = line[:hit[0]] + prefix + line[hit[1]:]
    return line


def hook_reply(data):
    """The answer to a PreToolUse event, or None to leave the command alone."""
    shell = {'Bash': 'bash', 'PowerShell': 'powershell'}.get(data.get('tool_name'))
    tool_input = data.get('tool_input') or {}
    command = tool_input.get('command')
    if not shell or not isinstance(command, str) or os.environ.get('HEAVYGATE') == 'off':
        return None
    wait = bool(tool_input.get('run_in_background'))   # a foreground call must not sit in a queue until its timeout
    new = hook_rewrite(command, shell, wait, data.get('cwd') or os.getcwd())
    if new is None:
        return None
    note = 'HeavyGate put this heavy command on its lane.' + ('' if wait else (
        f' It waits up to {int(settings()["foreground_wait_s"])} s for a slot; exit {BUSY_EXIT} means both slots stayed busy: '
        f'run it again in the background and it will wait its turn.'))
    return {'hookSpecificOutput': {'hookEventName': 'PreToolUse', 'updatedInput': {**tool_input, 'command': new},
                                   'additionalContext': note}}


def cmd_hook(a):
    """heavy hook: a PreToolUse hook for coding agents, JSON in and out. With -- COMMAND, shows what it would do."""
    if a.command:
        line = a.command[0] if len(a.command) == 1 else ' '.join(a.command)
        print(hook_rewrite(line, a.shell, a.wait, os.getcwd()) or 'unchanged')
        return 0
    try:
        ensure_guard()
    except Exception as e:
        print(f'heavy hook: could not start the RAM guard: {e}', file=sys.stderr)
    try:   # a hook that fails must never cost anyone their command
        reply = hook_reply(json.load(sys.stdin))
        if reply:
            print(json.dumps(reply))
    except Exception as e:
        print(f'heavy hook: {e}', file=sys.stderr)
    return 0


def cmd_rules(a):
    """heavy rules: the rules window on Windows; the list as text with --list or anywhere else."""
    window = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'heavyrules.pyw')
    if WIN and not a.list:
        exe = os.path.join(os.path.dirname(sys.executable), 'pythonw.exe')
        subprocess.Popen([exe if os.path.exists(exe) else sys.executable, window])
        return 0
    data = read_rules()
    for i, r in enumerate(data['rules'], 1):
        what = ' in '.join(x for x in (r['match'], r['folder']) if x)
        print(f"{i:3d}  {r['lane']}  {str(r['threads'] or 'usual'):>5s}  {what}")
    print(f"     cpu  usual  anything else ({usual_threads()} threads)")
    for s in suggestions():
        print('  suggestion: ' + s['text'])
    return 0


def cmd_stop(a):
    """Ends the job holding a lane and everything it started, so the next job in line can go."""
    h = peek(a.lane)
    if not h or not h.get('pid'):
        print(f'heavy: nothing is running on the {a.lane} lane')
        return 1
    print(f"heavy: stopping {h.get('name', '?')} (pid {h['pid']}) on the {a.lane} lane")
    if WIN:
        subprocess.run(['taskkill', '/T', '/F', '/PID', str(h['pid'])], capture_output=True, creationflags=NO_WINDOW)
        started = h.get('started_ts') or time.time()   # the job can't write its own log line now
        _log({'name': h.get('name', '?'), 'lane': a.lane, 'cwd': h.get('cwd', ''), 'command': h.get('command', ''),
              'queued': h.get('queued'), 'started': h.get('started'), 'waited_s': round(started - h.get('queued_ts', started), 1),
              'lane_wait_s': round(h.get('lane_ts', started) - h.get('queued_ts', started), 1),
              'ram_wait_s': round(started - h.get('lane_ts', started), 1), 'ran_s': round(time.time() - started, 1),
              'exit': 137, 'threads': h.get('threads'), 'warning': 'stopped with heavy stop'})
    else:
        try:
            os.kill(h['pid'], signal.SIGTERM)   # heavy takes its job tree with it
        except OSError:
            pass
    end = time.time() + 15
    while time.time() < end:
        now = peek(a.lane)
        if not now or now.get('pid') != h['pid']:
            print('heavy: stopped')
            return 0
        time.sleep(0.2)
    print('heavy: it did not stop in 15 s', file=sys.stderr)
    return 1


def daemon_summary():
    """{'count', 'mb', 'building'} for running Gradle/Kotlin daemons, or None when there are none."""
    js = java_processes()
    ds = [j for j in js if j[1] in ('gradle-daemon', 'kotlin-daemon')]
    if not ds:
        return None
    return {'count': len(ds), 'mb': sum(m for *_, m in ds), 'building': any(k == 'gradle-client' for _, k, _ in js)}


def lane_rows(lockfree=True):
    """Per lane: the job running and the queue. A second cpu job running at once is its own row (with 'slot')."""
    lanes = []
    for lane in LANES:
        hs = peeks(lane) if lockfree else holders(lane)
        q = [job_view(r, queued=True) for r in peek_queue(lane)] if lockfree else             [job_view(r, queued=True) for _, r in queue(lane)]
        lanes.append({'lane': lane, 'job': job_view(hs[0]) if hs else None, 'queue': q})
        for i, h in enumerate(hs[1:], 2):
            lanes.append({'lane': lane, 'slot': i, 'job': job_view(h), 'queue': []})
    return lanes


def snapshot(meter=None, daemons=True, lockfree=False):
    """Everything status shows, as data: machine load, and per lane the job holding it and the jobs queued.
    lockfree=True reads the lanes with peek()/peek_queue(), which never take a lock or remove a ticket, so a monitor
    can poll it as often as it likes (a dashboard can do it every 10 s)."""
    if meter is None:
        meter = CpuMeter()
        time.sleep(0.5)
    avail, total, commit, limit = mem_mb()
    lanes = lane_rows(lockfree)
    eta = estimate(lanes)
    return {'version': VERSION, 'time': now_iso(), 'cpu': meter.percent(), 'gpu': gpu_now(),
            'hardware': hardware_names(),
            'ram': {'free_mb': avail, 'total_mb': total, 'committed_mb': commit, 'commit_limit_mb': limit},
            'lanes': lanes, 'eta': eta, 'shutdown_when_done': power_state(),
            'biggest': [{'name': k, 'mb': v} for k, v in top_memory(8)],
            'daemons': daemon_summary() if daemons else None, 'ollama': ollama_models(),
            'ram_wait_min': settings()['ram_wait_min'], 'guard': guard_state(), 'cloud': cloud_summary(),
            'disk': disk_state(lanes)}


def disk_state(lanes=()):
    """Free space on the drives jobs write to: the projects folder's, temp's, the profile's (build caches), disk_paths,
    and the folder of every job running or queued."""
    s = settings()
    drives = watched_drives(cloud(s).drop_root, s)
    for L in lanes:
        for j in ([L['job']] if L.get('job') else []) + list(L.get('queue') or []):
            if j.get('cwd'):
                d = drive_of(j['cwd'])
                if os.path.normcase(d) not in {os.path.normcase(x) for x in drives}:
                    drives.append(d)
    return {'floor_gb': s['disk_floor_gb'], 'stop_gb': s['disk_stop_gb'], 'free_gb': disk_free(drives)}


# the cloud lane: GitHub Actions (heavycloud.py) ------------------------------------------------------------------------
def cloud(s=None):
    return heavycloud.Cloud(home(), s or settings())


def cloud_mine():
    """GitHub builds HeavyGate started and is still waiting on, from cloud-active/*.json (no gh call), with the runner's
    CPU/RAM/disk when the workflow carries the probe. A finished one stays cloud_done_show_s so its result can be read;
    a note whose process is gone (and that never finished) is removed."""
    d = os.path.join(home(), 'cloud-active')
    out = []
    show_s = float(settings().get('cloud_done_show_s', 120))
    for f in sorted(os.listdir(d)) if os.path.isdir(d) else []:
        if not f.endswith('.json'):
            continue
        p = os.path.join(d, f)
        r = _read_json(p)
        ended = r.get('ended_ts')
        gone = (time.time() - float(ended) > show_s) if ended else (not r.get('pid') or not pid_alive(r['pid']))
        if gone:
            try:
                os.remove(p)
            except OSError:
                pass
            continue
        r['for_s'] = max(0, float(ended or time.time()) - float(r.get('started_ts') or time.time()))
        r['mine'] = True
        out.append(r)
    return out


def cloud_summary():
    """The cached cloud state as numbers, or None when the cloud lane is off. Reads cloud.json only: never calls gh."""
    s = settings()
    return cloud(s).summary() if s.get('cloud') else None


def cloud_plan(line, cwd, name, lane, s=None, caps=True, asked=False):
    """For a command mapped to a GitHub workflow (cloud-jobs.json): the advice, with the learned GitHub time, the auto
    mode caps (caps=True) and whether GitHub would build exactly what is here. None when no mapping matches. asked
    (--cloud): it goes to GitHub even with RAM and lane fine, but never past the quota, a charge or the push check.
    {'where', 'rule', 'reason', 'cost_min', 'left_after', 'job', 'est_min', 'est_src', 'inputs', 'ready'}."""
    s = s or settings()
    _, job = heavycloud.find_job(heavycloud.read_jobs(home()), line, cwd, rule_matches)
    if job is None:
        return None
    c = cloud(s)
    state = c.refresh()   # only what is stale; usually nothing
    summ = c.summary()
    est, src = heavycloud.learned_est(home(), state, job['repo'], job['workflow'], s['cloud_est_min'])
    lane = 'gpu' if lane == 'gpu' else 'cpu'
    u, _ = usual_secs(name, line, cwd)
    short = disk_short(disk_free(watched_drives(cwd, s)), float(s['disk_floor_gb']))
    inputs = {'gpu': lane == 'gpu', 'os': job['os'], 'est_min': est, 'local_min': u / 60 if u else None,
              'need_mb': s['expect_mb'][lane], 'free_mb': mem_mb()[0], 'reserve_mb': s['reserve_mb'],
              'wait_s': local_wait_s(lane),
              'disk_low': (disk_text(short, float(s['disk_floor_gb'])) + ': it would not start here.') if short else None}
    plan = dict(heavycloud.decide(s, summ, inputs), job=job, est_min=est, est_src=src, inputs=inputs, ready=None)
    if asked and plan['rule'] == 'free':
        plan.update(where='cloud', rule='asked', reason='asked for with --cloud.',
                    left_after=None if plan['left_after'] is None else round(plan['left_after'] - plan['cost_min'], 1))
    if plan['where'] == 'cloud' and caps:
        a = summ['auto']
        for spent_, cap, when in ((a['day_min'], a['day_cap'], 'today'), (a['month_min'], a['month_cap'], 'this month')):
            if spent_ + plan['cost_min'] > cap:
                plan.update(where='local', rule='cap', reason=f"auto mode has spent {heavycloud.fmt_min(spent_)} of its "
                            f"{heavycloud.fmt_min(cap)} min {when}, and this would take {plan['cost_min']} more.")
                break
    if plan['where'] == 'cloud':
        plan['ready'] = heavycloud.ready_to_build(cwd, job['repo'], job.get('ref'))
        if not plan['ready']['ok']:
            plan.update(where='local', rule='git', reason=plan['ready']['reason'] + '.')
    return plan


def cloud_say(name):
    return lambda m: print(f'[heavy] {name}: {m}', file=sys.stderr, flush=True)


def local_wait_s(lane, lanes=None):
    """How long until a new job on this lane would start: the known time left of what runs and waits there, plus 5 min
    for each job with no past runs."""
    if lanes is None:
        lanes = lane_rows()
    if sum(1 for L in lanes if L['lane'] == lane and L['job']) < slots(lane) and             not any(L['queue'] for L in lanes if L['lane'] == lane):
        return 0   # a slot is free
    estimate(lanes)
    for L in lanes:
        if L['lane'] == lane:
            jobs = ([L['job']] if L['job'] else []) + L['queue']
            return L.get('done_in_s', 0) + 300 * sum(1 for j in jobs if j.get('usual_s') is None)
    return 0


def guard_state():
    s = settings()
    last = [x for x in guard_last(20) if 'ENDED' in x or 'could NOT' in x]
    return {'ceiling_pct': s['ram_ceiling_pct'], 'kill_pct': s['ram_kill_pct'],
            'load_pct': round(ram_load(s['ram_ceiling_pct'])[0], 1), 'running': guard_running(),
            'last': last[-1] if last else None}


def status_text(snap):
    r, g = snap['ram'], snap['gpu']
    gtxt = f", GPU {g['util']:.0f}% {g['mem_mb'] / 1024:.1f} of {g['total_mb'] / 1024:.1f} GB {g['temp']:.0f} C" if g else ''
    out = [f"HeavyGate {snap['version']}: RAM {r['free_mb'] / 1024:.1f} of {r['total_mb'] / 1024:.1f} GB free, committed "
           f"{r['committed_mb'] / 1024:.1f} of {r['commit_limit_mb'] / 1024:.1f} GB, CPU {snap['cpu']}%{gtxt}"]
    hw = snap.get('hardware') or {}
    if hw.get('cpu') or hw.get('gpu'):
        out.append('  ' + ', '.join(f'{k.upper()} {v}' for k, v in (('cpu', hw.get('cpu')), ('gpu', hw.get('gpu'))) if v))
    short = False
    w = max([len(j['name']) for L in snap['lanes'] for j in ([L['job']] if L['job'] else []) + L['queue']] or [0])

    def row(label, j):
        extra = ''
        if 'need_mb' in j:
            extra = f"  needs {j['need_mb'] / 1024:.1f} GB, {j['free_mb'] / 1024:.1f} GB free"
        elif j['state'] == 'running' and j['since_queued_s'] and j['since_queued_s'] - (j['for_s'] or 0) > 5:
            extra = f"  (waited {fmt_secs(j['since_queued_s'] - j['for_s'])} first)"
        if j.get('threads'):
            extra += f"  {j['threads']} threads"
        if 'usual_s' in j:
            extra += f"  {eta_note(j)}"
        return (f"    {label:16s} {j['name']:{w}s} {fmt_secs(j['for_s']) if j['for_s'] is not None else '?':>6s}{extra}"
                f"  pid {j['pid'] or '?'}  {j['cwd']}")

    for L in snap['lanes']:
        j, q = L['job'], L['queue']
        out.append(f"  {L['lane'].upper()} lane: " + ('free' if not j and not q else
                   f"{'1 running' if j and j['state'] == 'running' else '1 waiting for RAM' if j else 'nothing running'}"
                   + (f", {len(q)} queued" if q else '')))
        if j:
            out.append(row(j['state'], j))
            short = short or j['state'] == 'waiting for RAM'
        for i, qj in enumerate(q, 1):
            out.append(row(f'queued #{i}', qj))
    for L in snap['lanes']:
        if L['job'] and L['job'].get('stalled_s') is not None:
            out.append(f"  STALLED on the {L['lane']} lane: {L['job']['name']} is alive but has done nothing for "
                       f"{fmt_secs(L['job']['stalled_s'])}"
                       + (f", and {len(L['queue'])} waiting behind it" if L['queue'] else '')
                       + f". If it is stuck: heavy stop {L['lane']}")
    if short:
        out.append(f"  Waiting for RAM: the job has its lane but not enough free memory. It starts when RAM frees up; "
                   f"after {snap['ram_wait_min']} min only if it can still start under the RAM ceiling.")
    if snap.get('eta'):
        busy_now = any(L['job'] or L['queue'] for L in snap['lanes'])
        out.append('  ' + eta_line(snap['eta'], busy_now) + (' Estimates are the median of past runs of the same job.'
                                                              if busy_now else ''))
    if snap.get('shutdown_when_done'):
        out.append(f"  Shut down when done: ON (since {snap['shutdown_when_done']['armed_at'][11:16]}). The PC shuts "
                   f"down once both lanes have been empty for {settings()['shutdown_idle_s'] // 60} min, after a "
                   f"countdown on the strip. Cancel: heavy shutdown-when-done --off, or click the strip.")
    g = snap.get('guard') or {}
    if g:
        out.append(f"  RAM ceiling {g['ceiling_pct']}%: now {g['load_pct']:.0f}%. Guard "
                   + ('running' if g['running'] else 'NOT RUNNING (heavy guard starts it)')
                   + (f". Last: {g['last']}" if g.get('last') else ''))
    dk = snap.get('disk')
    if dk and dk.get('free_gb'):
        out.append('  Disk: ' + ', '.join(f"{d} {g:,.1f} GB free" + (' (UNDER THE FLOOR: jobs there will not start)'
                                                                       if g < dk['floor_gb'] else '')
                                          for d, g in dk['free_gb'].items())
                   + f". A job needs {dk['floor_gb']:g} GB free on its drive and the temp drive to start, and is ended "
                     f"under {dk['stop_gb']:g} GB.")
    out.append('  Using the most RAM: ' + ', '.join(f"{b['name']} {b['mb'] / 1024:.1f} GB" for b in snap['biggest']))
    if snap.get('ollama'):
        gpu_job = next((L['job'] for L in snap['lanes'] if L['lane'] == 'gpu' and L['job']), None)
        out.append(f"  Ollama: {ollama_text(snap['ollama'])}" + (
            f". Sharing the GPU with {gpu_job['name']}" if gpu_job and any(m['gpu_mb'] for m in snap['ollama']) else
            ' (heavy free unloads it)'))
    d = snap['daemons']
    if d:
        out.append(f"  Gradle/Kotlin daemons: {d['count']} holding {d['mb'] / 1024:.1f} GB"
                   + (' (a build is running)' if d['building'] else ' (heavy free stops idle ones)'))
    if snap.get('cloud'):
        out += heavycloud.status_lines(snap['cloud'])
    return '\n'.join(out)


def cmd_status(a):
    snap = snapshot(lockfree=a.peek)
    print(json.dumps(snap, indent=2) if a.json else status_text(snap))
    return 0


def cmd_watch(a):
    """status, redrawn every few seconds until Ctrl+C."""
    if WIN:
        os.system('')   # turns on VT escape codes in a plain console window
        k32.SetConsoleTitleW(f'HeavyGate {VERSION} live view')
    meter, n, daemons = CpuMeter(), 0, None
    time.sleep(0.5)
    try:
        while True:
            snap = snapshot(meter, daemons=False)
            if n % 10 == 0:   # the daemon scan starts PowerShell, so not on every refresh
                daemons = daemon_summary()
            snap['daemons'] = daemons
            sys.stdout.write('\x1b[H\x1b[2J' + status_text(snap)
                             + f"\n\n  {snap['time'][11:]}  refreshes every {a.every:g}s, Ctrl+C to stop\n")
            sys.stdout.flush()
            n += 1
            time.sleep(a.every)
    except KeyboardInterrupt:
        print()
        return 0


def cmd_log(a):
    try:
        with open(os.path.join(home(), 'runs.jsonl'), encoding='utf-8') as f:
            rows = [json.loads(x) for x in f if x.strip()]
    except (OSError, ValueError):
        rows = []
    rows += [dict(r, lane='cloud') for r in heavycloud.read_runs_log(home())]   # builds that ran on GitHub
    rows.sort(key=lambda r: r.get('started') or r.get('queued') or '')
    if not rows:
        print('no runs yet')
        return 0
    print(f'{"started":19s} {"lane":5s}{"queued":>7s} {"for RAM":>7s} {"ran":>7s} {"exit":>4s} {"min free":>8s} '
          f'{"cpu max":>7s} {"gpu max":>7s} {"thr":>3s}  name')
    old = False
    for r in rows[-a.n:]:
        if r['lane'] == 'cloud':
            print(f'{(r.get("started") or "")[:19]:19s} cloud{"":>7s} {"":>7s} {fmt_secs(r.get("ran_s")):>7s} '
                  f'{str(r.get("exit")):>4s}  {r.get("cost_min") if r.get("cost_min") is not None else "?"} quota min'
                  f' ({r.get("os")}{", auto" if r.get("auto") else ""})  {r.get("name")}  -> {r.get("repo")} '
                  f'{r.get("workflow")} {r.get("conclusion") or ""}' + (f"  {len(r['files'])} files to {r['dest']}"
                                                                         if r.get('files') else ''))
            continue
        gm = f"{r['gpu_util_max']:.0f}%" if r.get('gpu_util_max') is not None else '-'
        if 'lane_wait_s' in r:
            qw, rw = fmt_secs(r['lane_wait_s']), fmt_secs(r['ram_wait_s'])
        else:   # 1.0.0 rows kept only the total wait
            qw, rw, old = fmt_secs(r.get('waited_s')) + '*', '*', True
        print(f'{(r.get("started") or r.get("queued") or "")[:19]:19s} {r["lane"]:4s} {qw:>7s} {rw:>7s} '
              f'{fmt_secs(r.get("ran_s")):>7s} {"-" if r.get("exit") is None else str(r["exit"]):>4s}{(r.get("min_free_mb") or 0) / 1024:>6.1f}GB '
              f'{str(r.get("cpu_max")) + "%":>7s} {gm:>7s} {r.get("threads") or "-":>3}  {r["name"]}'
              + (f'  {r["disk_drive"]} {-r["disk_used_gb"]:+.1f} GB' if r.get('disk_drive') and r.get('disk_used_gb')
                 and abs(r['disk_used_gb']) >= 0.1 else '')
              + ('  !' + r['warning'] if r.get('warning') else ''))
    if old:
        print('* run from HeavyGate 1.0.0, which kept queue time and RAM wait as one total')
    return 0


TRAY = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'heavytray.pyw')


def pythonw():
    exe = os.path.join(os.path.dirname(sys.executable), 'pythonw.exe')
    return exe if os.path.isfile(exe) else sys.executable


def startup_shortcut(guard=False):
    """The Startup-folder shortcut that starts the tray (asked for on 2026-11-02) or the RAM guard at sign-in."""
    return os.path.join(os.environ.get('APPDATA', ''), r'Microsoft\Windows\Start Menu\Programs\Startup',
                        'HeavyGate RAM guard.lnk' if guard else 'HeavyGate tray.lnk')


def set_startup(on, guard=False):
    """Add or remove the sign-in shortcut. Returns whether it exists afterwards."""
    lnk = startup_shortcut(guard)
    if not on:
        try:
            os.remove(lnk)
        except FileNotFoundError:
            pass
        return os.path.exists(lnk)
    os.makedirs(os.path.dirname(lnk), exist_ok=True)
    q = lambda s: "'" + s.replace("'", "''") + "'"
    if guard:
        args = '"' + os.path.abspath(__file__) + '" guard'
        desc = 'HeavyGate RAM guard: ends a coding agent command that takes RAM past the ceiling'
    else:
        args, desc = '"' + TRAY + '"', 'HeavyGate lanes: text strip and tray icon'
    ps = (f"$s = (New-Object -ComObject WScript.Shell).CreateShortcut({q(lnk)}); $s.TargetPath = {q(pythonw())}; "
          f"$s.Arguments = {q(args)}; $s.WorkingDirectory = {q(os.path.dirname(TRAY))}; "
          f"$s.Description = {q(desc)}; $s.Save()")
    run_quiet(['powershell', '-NoProfile', '-NonInteractive', '-Command', ps], 60)
    return os.path.exists(lnk)


def cmd_tray(a):
    """Start the tray icon without a console; a second one just exits (one per sign-in)."""
    if not WIN:
        print('heavy: the tray and strip are Windows only; use heavy watch for the live view', file=sys.stderr)
        return 2
    if a.at_sign_in:
        on = set_startup(a.at_sign_in == 'on')
        print(f'start at sign-in: {"on" if on else "off"} ({startup_shortcut()})')
        if a.at_sign_in == 'off':
            return 0
    subprocess.Popen([pythonw(), TRAY], close_fds=True,
                     creationflags=0x00000008 | 0x00000200 if WIN else 0)   # DETACHED_PROCESS, NEW_PROCESS_GROUP
    print('HeavyGate tray started (right-click it for the lanes; Exit is in its menu)')
    return 0


def tray_running():
    if not WIN:
        return False
    k32.OpenMutexW.restype = wintypes.HANDLE
    k32.OpenMutexW.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.LPCWSTR]
    h = k32.OpenMutexW(0x00100000, False, 'Local\\HeavyGateTray')   # SYNCHRONIZE
    if h:
        k32.CloseHandle(h)
    return bool(h)


def cmd_power(a):
    """heavy shutdown-when-done [--off]: arm or disarm shutting the PC down once the lanes are done."""
    if not WIN:
        print('heavy: shutdown-when-done runs in the tray, which is Windows only', file=sys.stderr)
        return 2
    if a.off:
        set_power(False)
        print('Shut down when done: off')
        return 0
    set_power(True)
    s = settings()
    lanes = lane_rows()
    busy_now = any(L['job'] or L['queue'] for L in lanes)
    print(f"Shut down when done: ON. Once both lanes have been empty for {s['shutdown_idle_s'] // 60} min, the strip "
          f"counts down {s['shutdown_countdown_s']} s and the PC shuts down (apps with unsaved work can still stop it).")
    print('  ' + eta_line(estimate(lanes), busy_now))
    print('  Cancel any time: heavy shutdown-when-done --off, the tray menu, or click the strip during the countdown.')
    if not a.no_tray and not tray_running():
        cmd_tray(argparse.Namespace(at_sign_in=None))
    return 0


SHELLS = {'bash', 'sh', 'dash', 'zsh', 'cmd', 'pwsh', 'powershell', 'conhost'}


def guard_lock_path():
    return os.path.join(home(), 'guard.lock')


def guard_log_path():
    return os.path.join(home(), 'guard.log')


def guard_running():
    """Someone holds guard.lock, which only a running guard does."""
    if not SUPPORTED:
        return False
    os.makedirs(home(), exist_ok=True)
    fd = _open_excl(guard_lock_path())
    if fd is None:
        return True
    os.close(fd)
    return False


def ensure_guard():
    """Start the RAM guard in the background if it isn't running. Cheap enough for every agent tool call."""
    off = ('off', '0', 'no', 'false')
    if not SUPPORTED or os.environ.get('HEAVYGATE', '').lower() in off or             os.environ.get('HEAVYGATE_GUARD', '').lower() in off or guard_running():
        return False
    me = os.path.abspath(__file__)
    if WIN:
        for flags in (0x00000008 | 0x00000200 | 0x01000000, 0x00000008 | 0x00000200):   # DETACHED, NEW_GROUP, BREAKAWAY
            try:   # out of the agent's job when allowed, so it outlives the session that started it
                subprocess.Popen([pythonw(), me, 'guard'], close_fds=True, cwd=os.path.dirname(me), creationflags=flags)
                break
            except OSError:
                continue
    else:
        subprocess.Popen([sys.executable, me, 'guard'], close_fds=True, start_new_session=True,
                         stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    return True


def _proc_table():
    """{pid: (parent pid, lower-case name without .exe, start time or None)}."""
    out = {}
    if LINUX:
        bt, tck = _boot_time(), os.sysconf('SC_CLK_TCK')
        for pid in _proc_pids():
            st = _proc_stat(pid)
            try:
                with open(f'/proc/{pid}/comm', encoding='utf-8', errors='replace') as f:
                    name = f.read().strip().lower()
            except OSError:
                continue
            if st:
                out[pid] = (int(st[1]), name, bt + int(st[19]) / tck)
        return out
    names = {pid: n for pid, n, _ in processes()}
    for pid, parent in _parents().items():
        start = None
        h = k32.OpenProcess(0x1000, False, pid)
        if h:
            c, e, k, u = (wintypes.FILETIME() for _ in range(4))
            if k32.GetProcessTimes(h, ctypes.byref(c), ctypes.byref(e), ctypes.byref(k), ctypes.byref(u)):
                start = (_ft(c) - 116444736000000000) / 1e7
            k32.CloseHandle(h)
        out[pid] = (parent, re.sub(r'\.exe$', '', names.get(pid, '?').lower()), start)
    return out


def agent_jobs(s=None, table=None, ws=None):
    """What coding agents are running right now, one entry per command: the process directly under the agent (the
    shell a tool call opened, or a helper such as an MCP server) and everything below it, biggest first. 'limit' is
    the size at which the guard may end it: guard_min_mb for a shell command, guard_big_mb for anything else."""
    s = s or settings()
    agents = {re.sub(r'\.exe$', '', a.lower()) for a in s['guard_agents']}
    spare = {re.sub(r'\.exe$', '', a.lower()) for a in s.get('guard_spare', [])}
    table = _proc_table() if table is None else table
    ws = ws_mb() if ws is None else ws
    me = os.getpid()
    groups = {}
    for pid, (parent, name, start) in table.items():
        if name in agents or pid == me:
            continue
        chain, cur, child_start = [pid], parent, start
        agent = None
        for _ in range(64):
            if cur not in table or cur == chain[-1]:
                break
            p_parent, p_name, p_start = table[cur]
            if p_start and child_start and p_start > child_start + 1:
                break   # that pid now belongs to a newer process: the real parent is gone
            if p_name in agents:
                agent = cur
                break
            chain.append(cur)
            cur, child_start = p_parent, p_start
        if agent is None:
            continue
        top = chain[-1]
        if table[top][1] in spare:
            continue   # the user's browser, opened from the agent's window: not an agent command
        g = groups.setdefault(top, {'pid': top, 'name': table[top][1], 'agent': table[agent][1], 'agent_pid': agent,
                                    'mb': 0, 'procs': {}})
        g['mb'] += ws.get(pid, 0)
        g['procs'][table[pid][1]] = g['procs'].get(table[pid][1], 0) + ws.get(pid, 0)
    out = []
    for g in groups.values():
        g['shell'] = g['name'] in SHELLS
        g['limit'] = int(s['guard_min_mb']) if g['shell'] else int(s['guard_big_mb'])
        g['procs'] = sorted(g['procs'].items(), key=lambda kv: -kv[1])[:4]
        out.append(g)
    return sorted(out, key=lambda g: -g['mb'])


def guard_note(text):
    try:
        os.makedirs(home(), exist_ok=True)
        with open(guard_log_path(), 'a', encoding='utf-8') as f:
            f.write(f'{now_iso()} {text}\n')
    except OSError:
        pass


def guard_last(n=1):
    try:
        with open(guard_log_path(), encoding='utf-8') as f:
            return [x.rstrip('\n') for x in f.readlines()[-n:]]
    except OSError:
        return []


def _job_text(g):
    return (f"{g['agent']} command {g['name']} (pid {g['pid']}, {g['mb'] / 1024:.1f} GB: "
            + ', '.join(f'{n} {mb / 1024:.1f}' for n, mb in g['procs']) + ')')


def guard_pass(s, pct, dry_run=False):
    """RAM is past the line: end the biggest agent command that is big enough to matter. Returns what it did."""
    jobs = [g for g in agent_jobs(s) if g['mb'] >= g['limit']]
    if not jobs:
        return None
    g = jobs[0]
    if dry_run:
        return 'would end ' + _job_text(g)
    gone = end_pid_tree(g['pid'])
    time.sleep(1.0)   # Windows hands the pages back over a moment
    after = ram_load(s['ram_ceiling_pct'])[0]
    text = (f"RAM {pct:.0f}% (ceiling {s['ram_ceiling_pct']}%): ENDED {_job_text(g)}; RAM now {after:.0f}%"
            if gone else f"RAM {pct:.0f}%: could NOT end {_job_text(g)} (access denied?)")
    guard_note(text)
    return text


def cmd_guard(a):
    """heavy guard: keep RAM under the ceiling, whoever's agent started the job. One per user (guard.lock)."""
    if not SUPPORTED:
        print('heavy: the RAM guard needs Windows or Linux', file=sys.stderr)
        return 2
    if a.check:
        s = settings()
        pct, room, _ = ram_load(s['ram_ceiling_pct'])
        print(f"RAM {pct:.0f}% in use, ceiling {s['ram_ceiling_pct']}% ({room / 1024:.1f} GB left under it), "
              f"ended at once at {s['ram_kill_pct']}%. Guard: {'running' if guard_running() else 'NOT RUNNING'}")
        for g in agent_jobs(s)[:8]:
            print(f"  {'may end' if g['mb'] >= g['limit'] else 'too small'}  {_job_text(g)}  (ends past "
                  f"{g['limit'] / 1024:.1f} GB)")
        for line in guard_last(5):
            print('  ' + line)
        return 0
    if a.at_sign_in:
        on = set_startup(a.at_sign_in == 'on', guard=True)
        print(f'RAM guard at sign-in: {"on" if on else "off"}')
        if a.at_sign_in == 'off':
            return 0
        ensure_guard()
        return 0
    os.makedirs(home(), exist_ok=True)
    lock = _open_excl(guard_lock_path())
    if lock is None:
        return 0   # one is running
    _write(lock, {'pid': os.getpid(), 'started': now_iso(), 'version': VERSION})
    guard_note(f'start: HeavyGate {VERSION} RAM guard, pid {os.getpid()}')
    s, read_at, over_since, quiet_until, said_none = settings(), time.time(), None, 0.0, 0.0
    try:
        while True:
            time.sleep(1.0)
            now = time.time()
            if now - read_at > 30:
                s, read_at = settings(), now
            pct = ram_load(s['ram_ceiling_pct'])[0]
            if pct < float(s['ram_ceiling_pct']):
                over_since = None
                continue
            over_since = over_since or now
            if now < quiet_until:
                continue   # just ended something: give Windows a moment to hand the RAM back
            if pct >= float(s['ram_kill_pct']) or now - over_since >= float(s['ram_over_s']):
                done = guard_pass(s, pct)
                if done:
                    quiet_until = now + 4
                elif now - said_none > 60:
                    guard_note(f"RAM {pct:.0f}% (ceiling {s['ram_ceiling_pct']}%): no agent command big enough to "
                               f"end. Biggest: {top_text()}")
                    said_none = now
    except KeyboardInterrupt:
        return 0
    finally:
        guard_note('stop')


def dependencies():
    """Where everything this tool uses lives."""
    if not WIN:
        return [
            ('Python', sys.executable, '3.9+ standard library only'),
            ('State folder', home(), 'gpu.lock / cpu.lock (flocked by the running job), queue/<lane>/*.ticket, runs.jsonl, settings.json'),
            ('Settings', os.path.join(home(), 'settings.json'), 'optional overrides of: ' + ', '.join(DEFAULTS)),
            ('Environment', 'HEAVYGATE=off', 'skip the gate; HEAVYGATE_GUARD=off: the hook and tray do not start the RAM guard; HEAVYGATE_HELD marks a job already inside a lane; HEAVYGATE_DIR moves the state folder'),
            ('/proc', '/proc', 'RAM, CPU use, process list, Gradle/Kotlin daemon command lines'),
            ('nvidia-smi', shutil.which('nvidia-smi') or '(not found)', 'GPU use, memory and temperature for status and the log'),
            ('Ollama', settings()['ollama_url'], 'optional: /api/ps for status, keep_alive 0 to unload (free_ollama setting)'),
            ('lspci', shutil.which('lspci') or '(not found)', 'GPU name when there is no nvidia-smi'),
            ('Run history', os.path.join(home(), 'runs.jsonl'), 'finish-time estimates: median of the last 5 runs of the same job'),
            ('Callers', 'the heavy script (or anything that runs python3 heavygate.py)', 'a test runner or build script can import heavygate and use hold()'),
        ]
    return [
        ('Python', sys.executable, '3.9+ standard library only'),
        ('Rules', rules_path(), 'which lane heavy auto picks, edited in heavyrules.pyw; starter rules with no file'),
        ('Ollama', settings()['ollama_url'], 'optional: /api/ps for status, keep_alive 0 to unload (free_ollama setting)'),
        ('State folder', home(), 'gpu.lock / cpu.lock (held open by the running job), queue\\<lane>\\*.ticket, runs.jsonl, settings.json'),
        ('Settings', os.path.join(home(), 'settings.json'), 'optional overrides of: ' + ', '.join(DEFAULTS)),
        ('Environment', 'HEAVYGATE=off', 'skip the gate; HEAVYGATE_HELD marks a job already inside a lane; HEAVYGATE_DIR moves the state folder'),
        ('nvidia-smi', shutil.which('nvidia-smi') or '(not found)', 'GPU use, memory and temperature for status and the log'),
        ('NVML', os.path.join(os.environ.get('SystemRoot', r'C:\Windows'), 'System32', 'nvml.dll'),
         "GPU use for the tray strip's graph, read in-process (ships with the NVIDIA driver)"),
        ('PowerShell', 'powershell.exe', 'Get-CimInstance Win32_Process: Java command lines to find Gradle/Kotlin daemons'),
        ('taskkill', 'taskkill.exe', 'stops idle daemons (heavy free) and a cancelled job\'s process tree'),
        ('Tray icon', TRAY, 'heavy tray; user32/shell32/gdi32 through ctypes (Shell_NotifyIconW); mutex Local\\HeavyGateTray'),
        ('Taskbar readout', os.path.join(os.path.dirname(os.path.abspath(__file__)), 'heavybar.py'),
         'GPU/CPU/RAM in the Windows 10 taskbar: a child window of Shell_TrayWnd / Shell_SecondaryTrayWnd that narrows '
         'ReBarWindow32 / WorkerW; "display": "taskbar" and "taskbar_every_screen" in tray.json'),
        ('Shutdown', ' '.join(shutdown_command()), 'shut down when done (heavy shutdown-when-done; switch file '
                     + power_path() + '); no /f, so an app with unsaved work can still stop it'),
        ('Run history', os.path.join(home(), 'runs.jsonl'), 'finish-time estimates: median of the last 5 runs of the same job'),
        ('Sign-in', startup_shortcut(), 'starts the tray at sign-in when present (heavy tray --at-sign-in on|off)'),
        ('RAM guard', guard_lock_path(), 'held by the running guard (heavy guard); its actions go to ' + guard_log_path()
         + '; sign-in shortcut ' + startup_shortcut(True)),
        ('Job Objects', 'kernel32 CreateJobObjectW / AssignProcessToJobObject, ntdll NtResumeProcess',
         "each job's RAM cap (JOB_OBJECT_LIMIT_JOB_MEMORY)"),
        ('gh', (heavycloud.gh_command() or ['(not found)'])[0],
         'cloud lane: GitHub CLI, signed in as you; the billing report needs the user scope (' + heavycloud.SCOPE_FIX + ')'),
        ('GitHub API', 'api.github.com: user, users/<you>/settings/billing/usage, run list, workflow run',
         'only your own account and repos; usage every cloud_usage_s, runs every cloud_runs_s (cloud_active_s while one runs)'),
        ('Cloud state', os.path.join(home(), 'cloud.json'), 'what the cloud lane last read; cloud.log lists dispatches'),
        ('Cloud builds', heavycloud.jobs_path(home()), 'builds mapped to workflows (heavy cloud map); each build that ran '
         'on GitHub goes to ' + heavycloud.runs_log_path(home()) + ', which also teaches the GitHub time'),
        ('git', shutil.which('git') or '(not found)', 'auto mode checks a checkout is clean and pushed (status, '
         'rev-parse, ls-remote origin) before GitHub builds it'),
        ('Workflows', cloud().drop_root, 'project folders scanned for .github/workflows whose origin is one of your repos'),
        ('Artifacts', 'api.github.com: repos/<repo>/actions/artifacts (list, DELETE)', 'artifact storage now and the '
         'clean-up past cloud_keep_days, every cloud_artifacts_s; deletions are listed in cloud.log'),
        ('Disk', ', '.join(watched_drives(cloud().drop_root)), 'free space (shutil.disk_usage) on the drive a job runs '
         'in, the temp drive and the profile drive (Gradle/NuGet/npm caches), plus disk_paths: disk_floor_gb to start, '
         'disk_stop_gb to keep running'),
        ('Callers', 'heavy.cmd (or any wrapper that runs python heavygate.py)', 'a test runner or build script can import heavygate and use hold()'),
    ]


PROBE_STEPS = """      # HeavyGate runner probe: CPU, RAM and disk for the tray at home (needs permissions: statuses: write)
      - name: HeavyGate runner probe
        continue-on-error: true
        shell: bash
        env:
          GITHUB_TOKEN: ${{ github.token }}
        run: $(command -v python3 || command -v python) .github/heavygate-probe.py --start
      # ... the build steps ...
      - name: HeavyGate runner probe (final)
        if: always()
        continue-on-error: true
        shell: bash
        env:
          GITHUB_TOKEN: ${{ github.token }}
          JOB_STATUS: ${{ job.status }}
        run: $(command -v python3 || command -v python) .github/heavygate-probe.py --final "$JOB_STATUS"
"""


def cloud_probe(folder):
    """heavy cloud probe FOLDER: copy heavyprobe.py into FOLDER/.github/heavygate-probe.py (stamped with its version)
    and show the workflow steps that start it."""
    import heavyprobe
    top = os.path.abspath(folder or '.')
    if not os.path.isdir(os.path.join(top, '.git')):
        print(f'{top} is not the top of a git checkout.')
        return 2
    dest = os.path.join(top, '.github', 'heavygate-probe.py')
    os.makedirs(os.path.dirname(dest), exist_ok=True)
    with open(os.path.join(_HERE, 'heavyprobe.py'), encoding='utf-8') as f:
        body = f.read()
    stamp = (f'# Copied from HeavyGate {VERSION} heavyprobe.py (probe {heavyprobe.PROBE_VERSION}); change it there, '
             f'then run heavy cloud probe again.\n')
    first, rest = body.split('\n', 1)   # the stamp goes under the #! line
    with open(dest, 'w', encoding='utf-8', newline='\n') as f:
        f.write(first + '\n' + stamp + rest)
    print(f'Copied the runner probe {heavyprobe.PROBE_VERSION} to {dest}.')
    print('Add to each mapped workflow: permissions: statuses: write, and these steps around the build:')
    print(PROBE_STEPS)
    return 0


def cmd_cloud(a):
    s = settings()
    if a.action == 'probe':
        return cloud_probe(a.repo)
    c = cloud(s)
    if a.action == 'refresh' or not (a.cached or a.action in ('auto', 'unmap', 'keep')):
        print('Reading GitHub ...', file=sys.stderr, flush=True)
        c.refresh(force=a.action == 'refresh')
    summ = c.summary()
    state = c.load()
    if a.action in ('status', 'refresh'):
        if a.json:
            print(json.dumps(dict(summ, workflows=state.get('workflows') or {}), indent=2))
            return 0
        print(f'HeavyGate {VERSION} cloud lane' + (f" (read {fmt_secs(summ['age_s'])} ago)" if summ.get('age_s') else ''))
        print('\n'.join(heavycloud.status_lines(summ)))
        wf = state.get('workflows') or {}
        if wf:
            print(f'  Workflows in {len(wf)} repos under {c.drop_root} (* = can be started with heavy cloud dispatch):')
            for repo, rows in wf.items():
                print(f'    {repo}: ' + ', '.join(f"{r['file']}{'*' if r['dispatch'] else ''}" for r in rows))
        if summ['state'] == 'ok':
            print('  Recent runs:')
            print('\n'.join(heavycloud.runs_lines(summ, 6)))
        return 0 if summ['state'] == 'ok' else 1
    if a.action == 'runs':
        if a.json:
            print(json.dumps(summ['active'] + summ['recent'], indent=2))
        else:
            print('\n'.join(heavycloud.runs_lines(summ, a.n)))
        return 0
    if a.action == 'auto':
        if a.repo not in ('on', 'off', None):
            print('heavy cloud auto on|off')
            return 2
        if a.repo:
            set_setting('cloud_auto', a.repo == 'on')
            s = settings()
        print(f"Auto mode is {'ON' if s.get('cloud_auto') else 'off'}. "
              + (f"heavy auto and heavy cpu send a mapped build to GitHub when the advice says so, up to "
                 f"{s['cloud_auto_day_min']} quota min a day and {s['cloud_auto_month_min']} a month "
                 f"(cloud_auto_day_min, cloud_auto_month_min). --no-cloud keeps one build here."
                 if s.get('cloud_auto') else 'Every build runs here. heavy cloud auto on to change that.'))
        return 0
    if a.action == 'keep':
        if a.repo is not None:
            try:
                days = 0.0 if a.repo == 'off' else float(a.repo)
                if days < 0:
                    raise ValueError
            except ValueError:
                print('heavy cloud keep DAYS|off: how many days an artifact may stay on GitHub before heavy deletes it')
                return 2
            set_setting('cloud_keep_days', days)
            s = settings()
        k = s.get('cloud_keep_days') or 0
        print(f"Artifacts older than {k:g} days are deleted from your repos (checked every "
              f"{fmt_secs(s['cloud_artifacts_s'])}; listed in cloud.log)." if k else
              'Artifacts stay until GitHub expires them (each workflow\'s retention-days). heavy cloud keep 3 to change that.')
        return 0
    if a.action == 'prune':
        days = (s.get('cloud_keep_days') or 0) if a.days is None else a.days
        if a.days is None and not days:
            print('cloud_keep_days is off: say how old with --days N (0 = every artifact).')
            return 2
        gone, state = c.prune(days, a.dry_run)
        verb = 'Would delete' if a.dry_run else 'Deleted'
        what = 'every artifact' if not days else f'artifacts at least {days:g} days old'
        if not gone:
            print(f'Nothing to delete: no {what} in {len(c.watched(state))} repos.')
        else:
            print(f"{verb} {len(gone)} ({what}, {round(sum(g['bytes'] for g in gone) / 2 ** 20):,} MB):")
            for g in gone:
                print(f"  {g['repo']}: {g['name']} {g['bytes'] / 2 ** 20:,.0f} MB, {g['age_days']:g} days old")
        print('\n'.join(heavycloud.storage_lines(cloud(s).summary())))
        return 0
    if a.action in ('map', 'unmap'):
        jobs = heavycloud.read_jobs(home())
        if a.action == 'unmap':
            try:
                i = int(a.repo) - 1
                gone = jobs.pop(i)
            except (TypeError, ValueError, IndexError):
                print(f'heavy cloud unmap N: N is the number heavy cloud map shows (1 to {len(jobs)})')
                return 2
            heavycloud.save_jobs(home(), jobs)
            print(f"Removed: {gone['match']} -> {gone['repo']} {gone['workflow']}")
            return 0
        if a.repo:
            if not (a.workflow and a.match):
                print('heavy cloud map REPO WORKFLOW --match WORDS [--folder PART] [--os linux] [--artifact NAME] '
                      '[--dest DIR] [--ref BRANCH]')
                return 2
            new = heavycloud.clean_job({'match': a.match, 'folder': a.folder, 'repo': a.repo, 'workflow': a.workflow,
                                        'os': a.os, 'ref': a.ref, 'artifact': a.artifact, 'dest': a.dest,
                                        'max_min': a.max_min})
            if not new:
                print('That mapping cannot work: it needs --match, an owner/repo and a workflow file.')
                return 2
            wf = (state.get('workflows') or {}).get(new['repo'])
            if not wf or not any(new['workflow'] in (r['file'], r['name']) for r in wf):
                state = c.rescan_workflows()   # a workflow added since the daily scan
                wf = (state.get('workflows') or {}).get(new['repo'])
            hit = next((r for r in wf or [] if new['workflow'] in (r['file'], r['name'])), None)
            if hit:
                new['workflow'] = hit['file']
                if not hit['dispatch']:
                    print(f"Note: {hit['file']} has no workflow_dispatch trigger yet, so it can't be started until it has.")
            elif wf is not None:
                print(f"Note: {new['repo']} has no workflow {new['workflow']!r} here yet (it has: "
                      + ', '.join(r['file'] for r in wf) + ').')
            jobs = [j for j in jobs if not (j['match'] == new['match'] and j.get('folder') == new.get('folder'))] + [new]
            heavycloud.save_jobs(home(), jobs)
            print(f"Mapped: {new['match']}" + (f" in *{new['folder']}*" if new.get('folder') else '')
                  + f" -> {new['repo']} {new['workflow']} ({new['os']}), files to {new['dest']}")
        jobs = heavycloud.read_jobs(home())
        if not jobs:
            print('No builds mapped to GitHub workflows. Add one: heavy cloud map you/App android.yml --match '
                  '"assembleRelease" --folder App')
            return 0
        print(f"Builds mapped to GitHub workflows ({heavycloud.jobs_path(home())}); auto mode is "
              f"{'ON' if s.get('cloud_auto') else 'off'}:")
        for i, j in enumerate(jobs, 1):
            est, src = heavycloud.learned_est(home(), state, j['repo'], j['workflow'], s['cloud_est_min'])
            print(f"  {i}. {j['match']}" + (f" in *{j['folder']}*" if j.get('folder') else '') + f" -> {j['repo']} "
                  f"{j['workflow']} on {j['os']}" + (f" at {j['ref']}" if j.get('ref') else '')
                  + f", files to {j['dest']}; about {est:g} min ({src})")
        return 0
    # advise and dispatch: where should this job run?
    cmd_line = ' '.join(a.command)
    gpu = a.gpu or bool(cmd_line and route(cmd_line)['lane'] == 'gpu')
    lane = 'gpu' if gpu else 'cpu'
    if a.action == 'advise' and cmd_line and not gpu:
        plan = cloud_plan(cmd_line, os.getcwd(), a.name or cmd_line[:60], lane, s, caps=bool(s.get('cloud_auto')))
        if plan:
            j = plan['job']
            if a.json:
                print(json.dumps(plan, indent=2, default=str))
                return 0
            print(f"{'Send to GitHub' if plan['where'] == 'cloud' else 'Run it here'} ({plan['rule']}): {plan['reason']}")
            print(f"  Mapped to {j['repo']} {j['workflow']} on {j['os']}: about {plan['est_min']:g} min there "
                  f"({plan['est_src']}), counting {plan['cost_min']} quota min"
                  + (f"; {heavycloud.fmt_min(plan['left_after'])} left after" if plan['left_after'] is not None else '') + '.')
            if plan['ready']:
                print(f"  GitHub would build: {plan['ready']['reason']}.")
            print(f"  Auto mode is {'ON: heavy auto would do this by itself' if s.get('cloud_auto') else 'off: heavy auto runs it here (heavy cloud auto on)'}.")
            return 0
    local_min = a.local_min
    if local_min is None and a.name:
        u, _ = usual_secs(a.name, cmd_line)
        local_min = u / 60 if u else None
    job = {'gpu': gpu, 'os': a.os, 'est_min': a.est, 'local_min': local_min,
           'need_mb': a.need_mb or s['expect_mb'][lane], 'free_mb': mem_mb()[0], 'reserve_mb': s['reserve_mb'],
           'wait_s': local_wait_s(lane)}
    d = heavycloud.decide(s, summ, job)
    if a.action == 'advise':
        if a.json:
            print(json.dumps(dict(d, job=job), indent=2))
            return 0
        print(f"{'Send to GitHub' if d['where'] == 'cloud' else 'Run it here'} ({d['rule']}): {d['reason']}")
        print(f"  On GitHub it would count {d['cost_min']} min ({a.est:g} min on {a.os}, x{heavycloud.MULT[a.os]})"
              + (f"; {heavycloud.fmt_min(d['left_after'])} min left after" if d['left_after'] is not None else '') + '.')
        print(f"  Here: {job['free_mb'] / 1024:.1f} GB free, needs {(job['need_mb'] + job['reserve_mb']) / 1024:.1f} GB, "
              f"{lane} lane about {fmt_eta(job['wait_s'])} behind"
              + (f", usually {fmt_eta(local_min * 60)} here" if local_min else '') + '.')
        return 0
    # dispatch
    wf = (state.get('workflows') or {}).get(a.repo)
    if not wf and not a.force:
        print(f"{a.repo} has no workflow under {c.drop_root} that heavy knows of. Known: "
              + (', '.join(state.get('workflows') or {}) or 'none (run heavy cloud first)') + '. --force sends it anyway.')
        return 3
    hit = next((r for r in wf or [] if a.workflow in (r['file'], r['name'])), None)
    if wf and not hit:
        print(f"{a.repo} has no workflow {a.workflow!r}. It has: " + ', '.join(r['file'] for r in wf))
        return 3
    if hit and not hit['dispatch']:
        print(f"{hit['file']} has no workflow_dispatch trigger, so it can't be started by hand. Add `workflow_dispatch:` "
              f"under `on:` in {a.repo}'s .github/workflows/{hit['file']} and push it.")
        return 3
    if d['where'] != 'cloud' and not a.force:
        print(f"Not sent: the advice is to run it here ({d['rule']}): {d['reason']} --force sends it anyway.")
        return 3
    if a.wait:   # the whole round trip: it must build exactly what is checked out here
        where = heavycloud.find_checkout([os.getcwd(), c.drop_root], a.repo)
        top = heavycloud.ready_to_build(where, a.repo, a.ref) if where else None
        if not top:
            print(f'No checkout of {a.repo} found here or under {c.drop_root}: --wait brings the files back into one.')
            return 3
        if not top['ok']:
            print(f"Not sent: {top['reason']} (in {top['top']}).")
            return 3
        mapped = next((j for j in heavycloud.read_jobs(home()) if j['repo'] == a.repo
                       and j['workflow'] == (hit['file'] if hit else a.workflow)), None)
        job = dict(mapped or {'dest': a.dest or 'dist/cloud', 'os': a.os}, repo=a.repo,
                   workflow=hit['file'] if hit else a.workflow)
        if a.artifact:
            job['artifact'] = a.artifact
        if a.max_min:
            job['max_min'] = a.max_min
        rec = heavycloud.round_trip(c, job, top, a.name or f"{a.repo} {job['workflow']}", cloud_say(a.repo),
                                    auto=False, rule='forced' if a.force else d['rule'], est=a.est)
        return rec['exit']
    ok, msg = c.dispatch(a.repo, hit['file'] if hit else a.workflow, a.ref)
    rec = {'time': now_iso(), 'repo': a.repo, 'workflow': hit['file'] if hit else a.workflow, 'ref': a.ref, 'ok': ok,
           'advice': d['where'], 'rule': d['rule'], 'forced': bool(a.force), 'cost_min': d['cost_min'], 'message': msg[:200]}
    try:
        with open(os.path.join(home(), 'cloud.log'), 'a', encoding='utf-8') as f:
            f.write(json.dumps(rec) + '\n')
    except OSError:
        pass
    if not ok:
        print(f'GitHub refused it: {msg}')
        return 1
    print(f"Started {rec['workflow']} on {a.repo}" + (f" at {a.ref}" if a.ref else '') + f". It should count about "
          f"{d['cost_min']} min. Follow it: heavy cloud runs, or gh run watch -R {a.repo}")
    return 0


def cmd_deps(a):
    for name, where, note in dependencies():
        print(f'{name:13s} {where}' + (f'\n{"":13s} {note}' if note else ''))
    return 0


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    if argv and argv[0] in LANES:
        argv = ['run'] + argv
    elif argv and argv[0] == 'auto':
        argv = ['run'] + argv
    command = []
    if '--' in argv:   # everything after the first -- is the command, untouched
        i = argv.index('--')
        argv, command = argv[:i], argv[i + 1:]
    ap = argparse.ArgumentParser(prog='heavy', description=__doc__.split('\n\n')[0])
    ap.add_argument('--version', action='version', version=f'HeavyGate {VERSION}')
    sub = ap.add_subparsers(dest='cmd')
    r = sub.add_parser('run', help='run a command on a lane')
    r.add_argument('lane', choices=LANES + ('auto',))
    r.add_argument('-n', '--name', help='what to call the job in status and the log')
    r.add_argument('--expect', type=int, help='MB of RAM the job needs (default per lane)')
    r.add_argument('--no-wait', action='store_true', help=f'exit {BUSY_EXIT} at once if the lane is busy')
    r.add_argument('--wait-max', type=float, help=f'wait at most this many seconds for a slot, then exit {BUSY_EXIT}')
    r.add_argument('--threads', type=int, help='logical processors this cpu job may use (default cpu_threads)')
    r.add_argument('--no-cloud', action='store_true', help='run it here even when auto mode would send it to GitHub')
    r.add_argument('--cloud', action='store_true', help='send this mapped build to GitHub (quota, charge and push '
                   'checks still apply; not counted against the auto caps)')
    st = sub.add_parser('status', help='what is running, waiting and queued on each lane; RAM/CPU/GPU now')
    st.add_argument('--json', action='store_true', help='the same as JSON, for other tools')
    st.add_argument('--peek', action='store_true', help='read the lanes without taking a lock or clearing dead tickets '
                    '(for monitors that poll)')
    w = sub.add_parser('watch', help='status, live: redrawn every few seconds')
    w.add_argument('-s', '--every', type=float, default=2.0, help='seconds between refreshes (default 2)')
    lg = sub.add_parser('log', help='recent runs')
    lg.add_argument('-n', type=int, default=20)
    sp = sub.add_parser('stop', help='end the job holding a lane and everything it started')
    sp.add_argument('lane', choices=LANES)
    fr = sub.add_parser('free', help='stop idle Gradle/Kotlin daemons, unload Ollama models')
    fr.add_argument('--dry-run', action='store_true')
    sub.add_parser('deps', help='dependencies')
    gd = sub.add_parser('guard', help="keep RAM under the ceiling: end the coding agent command that takes it past")
    gd.add_argument('--check', action='store_true', help='show RAM, the agents\' commands and what it would end')
    gd.add_argument('--at-sign-in', choices=['on', 'off'], help='start it at every sign-in (Startup-folder shortcut)')
    tr = sub.add_parser('tray', help='show the lanes as a text strip and tray icon (one per sign-in; background)')
    tr.add_argument('--at-sign-in', choices=['on', 'off'], help='start it at every sign-in (Startup-folder shortcut)')
    sub.add_parser('why', help='where heavy auto would send a command, without running it')
    hk = sub.add_parser('hook', help='agent hook: JSON in, heavy commands go through auto; with -- COMMAND, a dry run')
    hk.add_argument('--shell', choices=['bash', 'powershell'], default='bash', help='the shell of a dry run')
    hk.add_argument('--wait', action='store_true', help='dry run of a background call, which waits its turn')
    ru = sub.add_parser('rules', help='the rules that pick a lane: the window on Windows, text elsewhere')
    ru.add_argument('--list', action='store_true', help='print them instead of opening the window')
    cl = sub.add_parser('cloud', help='the cloud lane: GitHub Actions minutes, runs, and where a job should run',
                        description='status (default): plan, minutes used and left, charges, runs. runs: recent runs. '
                        'advise [-- COMMAND]: local or GitHub for a job, and the cost. dispatch REPO WORKFLOW: start a '
                        'workflow on GitHub (only when the advice says cloud, unless --force). refresh: read everything now. '
                        'prune [--days N] [--dry-run]: delete old artifacts from your repos now. keep DAYS|off: how old '
                        'an artifact may get before heavy deletes it. probe FOLDER: copy the runner probe into a checkout (the tray then '
                        'shows the runner CPU, RAM and disk while it builds).')
    cl.add_argument('action', nargs='?', default='status',
                    choices=['status', 'runs', 'advise', 'dispatch', 'refresh', 'map', 'unmap', 'auto', 'prune', 'keep',
                             'probe'])
    cl.add_argument('repo', nargs='?', help='dispatch/map: owner/repo; unmap: its number; auto: on or off; keep: days')
    cl.add_argument('--days', type=float, help='prune: delete artifacts at least this many days old (0 = all; '
                    'default cloud_keep_days)')
    cl.add_argument('--dry-run', action='store_true', help='prune: list what would go, delete nothing')
    cl.add_argument('workflow', nargs='?', help='dispatch/map: the workflow file (build.yml) or its name')
    cl.add_argument('--match', help='map: words in the build command, as in a lane rule (assembleRelease)')
    cl.add_argument('--folder', help='map: part of the folder the build runs in (the project name)')
    cl.add_argument('--artifact', help='map/dispatch: the artifact to bring back (default: all of them)')
    cl.add_argument('--dest', help='map/dispatch: where its files go, under the checkout (default dist/cloud)')
    cl.add_argument('--wait', action='store_true', help='dispatch: follow the run and bring its files back')
    cl.add_argument('--max-min', type=float, help='map/dispatch: cancel the run after this many minutes '
                    '(default cloud_max_min, 60); keep it above the workflow timeout-minutes')
    cl.add_argument('--ref', help='dispatch: branch or tag to run (default: the repo default branch)')
    cl.add_argument('--os', choices=list(heavycloud.MULT), default='linux', help='runner OS (default linux, counts 1x)')
    cl.add_argument('--est', type=float, default=10, help='minutes the job takes on GitHub (default 10)')
    cl.add_argument('--local-min', type=float, help='minutes it takes here (default: from past runs of -n NAME)')
    cl.add_argument('-n', '--name', help='the job name its past runs here were logged under')
    cl.add_argument('--gpu', action='store_true', help='the job needs a GPU (it then always stays here)')
    cl.add_argument('--need-mb', type=int, help='MB of RAM it needs here (default per lane)')
    cl.add_argument('--force', action='store_true', help='dispatch even when the advice is to keep it here')
    cl.add_argument('--cached', action='store_true', help="don't call gh: show what was last read")
    cl.add_argument('--json', action='store_true')
    cl.add_argument('--count', dest='n_runs', type=int, default=12, help='runs: how many (default 12)')
    pw = sub.add_parser('shutdown-when-done', help='shut the PC down once both lanes are done (the tray does it)')
    pw.add_argument('--off', action='store_true', help='cancel it')
    pw.add_argument('--no-tray', action='store_true', help="don't start the tray if it isn't running")
    a = ap.parse_args(argv)
    if a.cmd == 'shutdown-when-done':
        return cmd_power(a)
    if a.cmd == 'run':
        a.command = command
        return cmd_run(a.lane, a)
    if a.cmd == 'why':
        a.command = command
        return cmd_why(a)
    if a.cmd == 'hook':
        a.command = command
        return cmd_hook(a)
    if a.cmd == 'rules':
        return cmd_rules(a)
    if a.cmd == 'cloud':
        a.command, a.n = command, a.n_runs
        if a.action == 'dispatch' and not (a.repo and a.workflow):
            ap.error('heavy cloud dispatch needs REPO and WORKFLOW, for example: heavy cloud dispatch you/App build.yml')
        return cmd_cloud(a)
    if a.cmd == 'log':
        return cmd_log(a)
    if a.cmd == 'stop':
        return cmd_stop(a)
    if a.cmd == 'free':
        free_daemons(print, a.dry_run)
        free_ollama(print, a.dry_run)
        return 0
    if a.cmd == 'deps':
        return cmd_deps(a)
    if a.cmd == 'guard':
        return cmd_guard(a)
    if a.cmd == 'watch':
        return cmd_watch(a)
    if a.cmd == 'tray':
        return cmd_tray(a)
    if a.cmd is None:
        a.json = False
    if a.cmd == 'status' or a.cmd is None:
        return cmd_status(a)
    return 2


if __name__ == '__main__':
    sys.exit(main())
