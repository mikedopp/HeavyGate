"""HeavyGate tray icon and text strip: the two lanes at a glance, light enough for a busy PC.

    pythonw heavytray.pyw              (or: heavy tray)
    python heavytray.pyw --selftest DIR   add the icon, read the lanes once, render the strip, write report.json and
                                          sample pictures, exit

The text strip sits above the clock while anything runs, waits or is queued, and fades out when both lanes are idle. It
names the job on each lane and how long it has been there, the next jobs in line, what a job waiting for RAM needs
against what is free, and the free RAM with its biggest users. Under that, three small graphs show CPU, GPU and RAM use
over the last two minutes, with each spike marked. It is a little see-through and turns solid while the mouse is
over it. Its two corner buttons send it behind other windows (click the strip to bring it back) or hide it until the
lanes are next idle; it also steps back on its own while a full-screen app is in front on its screen. Drag it anywhere
(the spot is kept). Clicking it opens nothing: the live view (heavy watch, a console window) opens only when you pick
"Open live view" in the tray menu. Right-click the strip or click the tray icon for that menu: live view, strip on/off,
on top, graphs, opacity.

The tray icon stays as the handle: its left half is the GPU lane and its right half the CPU lane (grey free, green
running, amber waiting for RAM, white dot = queued, red background = RAM nearly gone). Hover it for a summary;
click (either button) for every job and the commands, including "Open live view".

Every few seconds it reads the lane records (heavygate.peek, which never opens a lock, so it can't make a job miss
its turn) and the free RAM. While the strip shows, the graphs take a reading each second: GetSystemTimes for CPU,
NVML (nvml.dll from the NVIDIA driver) for GPU, GlobalMemoryStatusEx for RAM, each well under a millisecond. No
nvidia-smi, no PowerShell. Python standard library (tkinter for the strip) + ctypes; Windows only.
"""
import collections
import ctypes
import json
import os
import struct
import subprocess
import sys
import time
import tkinter as tk
import traceback
import zlib
from ctypes import wintypes

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import heavygate as hg  # noqa: E402
import heavybar  # noqa: E402
import heavycloud as hc  # noqa: E402

GATE = os.path.join(HERE, 'heavygate.py')
POLL_MS = 3000
LOW_RAM_EXTRA_MB = 256
COLORS = {'bg': (29, 35, 48), 'bg_low': (179, 38, 30), 'rim': (150, 160, 178), 'free': (88, 96, 112),
          'running': (48, 209, 88),
          'ram': (255, 159, 10), 'dot': (255, 255, 255)}

u32 = ctypes.WinDLL('user32', use_last_error=True)
g32 = ctypes.WinDLL('gdi32')
sh32 = ctypes.WinDLL('shell32')
k32 = ctypes.WinDLL('kernel32', use_last_error=True)

LRESULT = ctypes.c_ssize_t
WNDPROC = ctypes.WINFUNCTYPE(LRESULT, wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM)
WM_DESTROY, WM_CLOSE, WM_TIMER, WM_NULL, WM_ENDSESSION = 0x0002, 0x0010, 0x0113, 0x0000, 0x0016
WM_LBUTTONUP, WM_RBUTTONUP, WM_APP = 0x0202, 0x0205, 0x8000
WM_TRAY = WM_APP + 1
WM_BAR_CLICK = WM_APP + 2   # posted by the taskbar readout's thread: open the menu here
NIM_ADD, NIM_MODIFY, NIM_DELETE = 0, 1, 2
NIF_MESSAGE, NIF_ICON, NIF_TIP = 1, 2, 4
MF_STRING, MF_GRAYED, MF_SEPARATOR = 0x0, 0x1, 0x800
TPM_RIGHTBUTTON, TPM_RETURNCMD = 0x2, 0x100
CREATE_NEW_CONSOLE = 0x10
CMD_WATCH, CMD_LOG, CMD_FREE, CMD_DEPS, CMD_FOLDER, CMD_STRIP, CMD_STRIP_RESET, CMD_EXIT = 1, 2, 3, 4, 5, 6, 7, 9
MF_CHECKED = 0x8
CMD_STARTUP = 8
CMD_BEHIND, CMD_DISMISS, CMD_ONTOP, CMD_GRAPHS, CMD_POWER, CMD_RULES = 10, 11, 12, 13, 14, 15
CMD_SHOW_STRIP, CMD_SHOW_BAR, CMD_BAR_ALL = 16, 18, 17
MF_BYPOSITION = 0x400
CMD_OPACITY = 20   # + tenths: 26..30 = 60..100%
CMD_CLOUD_STRIP, CMD_CLOUD = 40, 41
CMD_LINK = 200   # + index into the menu's GitHub links (up to 60)
MF_POPUP = 0x10
STRIP = {'bg': '#1b1f27', 'name': '#e8ebf0', 'dim': '#8a93a3', 'run': '#30d158', 'wait': '#ffaa33', 'stop': '#ff5c5c',
         'border': '#3a4150', 'border_wait': '#ffaa33', 'border_low': '#e5484d', 'font': ('Consolas', 9),
         'small': ('Consolas', 8), 'panel': '#232a36', 'cpu': '#4da3ff', 'gpu': '#c58af9', 'ram': '#e8c547'}
PREFS = os.path.join(hg.home(), 'tray.json')
LOG_MAX = 256 * 1024


def log(msg):
    """One line in tray.log in the state folder: starts, exits and why, errors, shutdown steps. pythonw has no
    console, so without this a tray that stops leaves no trace."""
    try:
        p = os.path.join(hg.home(), 'tray.log')
        if os.path.exists(p) and os.path.getsize(p) > LOG_MAX:
            os.replace(p, p + '.old')
        with open(p, 'a', encoding='utf-8') as f:
            f.write(f'{hg.now_iso()} [{os.getpid()}] {msg}\n')
    except OSError:
        pass
GRAPH_SECONDS, GRAPH_H, SAMPLE_MS = 120, 34, 1000
FADE_IN_MS, FADE_OUT_MS, HOVER_MS = 220, 420, 120
REST_OPACITY = 0.88   # the strip at rest; solid while the mouse is over it


class WNDCLASSEXW(ctypes.Structure):
    _fields_ = [('cbSize', wintypes.UINT), ('style', wintypes.UINT), ('lpfnWndProc', WNDPROC),
                ('cbClsExtra', ctypes.c_int), ('cbWndExtra', ctypes.c_int), ('hInstance', wintypes.HINSTANCE),
                ('hIcon', wintypes.HICON), ('hCursor', wintypes.HANDLE), ('hbrBackground', wintypes.HBRUSH),
                ('lpszMenuName', wintypes.LPCWSTR), ('lpszClassName', wintypes.LPCWSTR), ('hIconSm', wintypes.HICON)]


class GUID(ctypes.Structure):
    _fields_ = [('Data1', wintypes.DWORD), ('Data2', wintypes.WORD), ('Data3', wintypes.WORD),
                ('Data4', ctypes.c_ubyte * 8)]


class NOTIFYICONDATAW(ctypes.Structure):
    _fields_ = [('cbSize', wintypes.DWORD), ('hWnd', wintypes.HWND), ('uID', wintypes.UINT),
                ('uFlags', wintypes.UINT), ('uCallbackMessage', wintypes.UINT), ('hIcon', wintypes.HICON),
                ('szTip', wintypes.WCHAR * 128), ('dwState', wintypes.DWORD), ('dwStateMask', wintypes.DWORD),
                ('szInfo', wintypes.WCHAR * 256), ('uVersion', wintypes.UINT), ('szInfoTitle', wintypes.WCHAR * 64),
                ('dwInfoFlags', wintypes.DWORD), ('guidItem', GUID), ('hBalloonIcon', wintypes.HICON)]


class BITMAPINFOHEADER(ctypes.Structure):
    _fields_ = [('biSize', wintypes.DWORD), ('biWidth', ctypes.c_long), ('biHeight', ctypes.c_long),
                ('biPlanes', wintypes.WORD), ('biBitCount', wintypes.WORD), ('biCompression', wintypes.DWORD),
                ('biSizeImage', wintypes.DWORD), ('biXPelsPerMeter', ctypes.c_long),
                ('biYPelsPerMeter', ctypes.c_long), ('biClrUsed', wintypes.DWORD), ('biClrImportant', wintypes.DWORD)]


class MONITORINFO(ctypes.Structure):
    _fields_ = [('cbSize', wintypes.DWORD), ('rcMonitor', wintypes.RECT), ('rcWork', wintypes.RECT),
                ('dwFlags', wintypes.DWORD)]


class HIGHCONTRASTW(ctypes.Structure):
    _fields_ = [('cbSize', wintypes.UINT), ('dwFlags', wintypes.DWORD), ('lpszDefaultScheme', wintypes.LPWSTR)]


class ICONINFO(ctypes.Structure):
    _fields_ = [('fIcon', wintypes.BOOL), ('xHotspot', wintypes.DWORD), ('yHotspot', wintypes.DWORD),
                ('hbmMask', wintypes.HBITMAP), ('hbmColor', wintypes.HBITMAP)]


u32.DefWindowProcW.argtypes = [wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM]
u32.DefWindowProcW.restype = LRESULT
u32.RegisterClassExW.argtypes = [ctypes.POINTER(WNDCLASSEXW)]
u32.CreateWindowExW.argtypes = [wintypes.DWORD, wintypes.LPCWSTR, wintypes.LPCWSTR, wintypes.DWORD, ctypes.c_int,
                                ctypes.c_int, ctypes.c_int, ctypes.c_int, wintypes.HWND, wintypes.HMENU,
                                wintypes.HINSTANCE, wintypes.LPVOID]
u32.CreateWindowExW.restype = wintypes.HWND
u32.SetTimer.argtypes = [wintypes.HWND, ctypes.c_size_t, wintypes.UINT, ctypes.c_void_p]
u32.CreatePopupMenu.restype = wintypes.HMENU
u32.AppendMenuW.argtypes = [wintypes.HMENU, wintypes.UINT, ctypes.c_size_t, wintypes.LPCWSTR]
u32.SetMenuDefaultItem.argtypes = [wintypes.HMENU, wintypes.UINT, wintypes.UINT]
u32.TrackPopupMenu.argtypes = [wintypes.HMENU, wintypes.UINT, ctypes.c_int, ctypes.c_int, ctypes.c_int,
                               wintypes.HWND, ctypes.c_void_p]
u32.DestroyMenu.argtypes = [wintypes.HMENU]
u32.GetMenuItemCount.argtypes = [wintypes.HMENU]
u32.CheckMenuRadioItem.argtypes = [wintypes.HMENU, wintypes.UINT, wintypes.UINT, wintypes.UINT, wintypes.UINT]
u32.PostMessageW.argtypes = [wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM]
u32.CreateIconIndirect.argtypes = [ctypes.POINTER(ICONINFO)]
u32.CreateIconIndirect.restype = wintypes.HICON
u32.DestroyIcon.argtypes = [wintypes.HICON]
u32.DestroyWindow.argtypes = [wintypes.HWND]
u32.GetParent.argtypes = [wintypes.HWND]
u32.GetParent.restype = wintypes.HWND
u32.SetForegroundWindow.argtypes = [wintypes.HWND]
u32.GetCursorPos.argtypes = [ctypes.POINTER(wintypes.POINT)]
u32.GetMessageW.argtypes = [ctypes.POINTER(wintypes.MSG), wintypes.HWND, wintypes.UINT, wintypes.UINT]
u32.TranslateMessage.argtypes = [ctypes.POINTER(wintypes.MSG)]
u32.DispatchMessageW.argtypes = [ctypes.POINTER(wintypes.MSG)]
u32.DispatchMessageW.restype = LRESULT
u32.GetWindowLongPtrW.argtypes = [wintypes.HWND, ctypes.c_int]
u32.GetWindowLongPtrW.restype = ctypes.c_ssize_t
u32.SetWindowLongPtrW.argtypes = [wintypes.HWND, ctypes.c_int, ctypes.c_ssize_t]
u32.SetWindowLongPtrW.restype = ctypes.c_ssize_t
u32.SystemParametersInfoW.argtypes = [wintypes.UINT, wintypes.UINT, ctypes.c_void_p, wintypes.UINT]
u32.MonitorFromPoint.argtypes = [wintypes.POINT, wintypes.DWORD]
u32.MonitorFromPoint.restype = wintypes.HANDLE
u32.MonitorFromWindow.argtypes = [wintypes.HWND, wintypes.DWORD]
u32.MonitorFromWindow.restype = wintypes.HANDLE
u32.GetMonitorInfoW.argtypes = [wintypes.HANDLE, ctypes.POINTER(MONITORINFO)]
u32.GetForegroundWindow.restype = wintypes.HWND
u32.GetWindowRect.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.RECT)]
u32.GetClassNameW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
u32.SetWindowPos.argtypes = [wintypes.HWND, wintypes.HWND, ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int,
                             wintypes.UINT]
u32.PrintWindow.argtypes = [wintypes.HWND, wintypes.HDC, wintypes.UINT]
u32.GetDC.argtypes = [wintypes.HWND]
u32.GetDC.restype = wintypes.HDC
u32.ReleaseDC.argtypes = [wintypes.HWND, wintypes.HDC]
g32.CreateCompatibleDC.argtypes = [wintypes.HDC]
g32.CreateCompatibleDC.restype = wintypes.HDC
g32.SelectObject.argtypes = [wintypes.HDC, wintypes.HANDLE]
g32.SelectObject.restype = wintypes.HANDLE
g32.DeleteDC.argtypes = [wintypes.HDC]
u32.RegisterWindowMessageW.argtypes = [wintypes.LPCWSTR]
u32.RegisterWindowMessageW.restype = wintypes.UINT
g32.CreateDIBSection.argtypes = [wintypes.HDC, ctypes.c_void_p, wintypes.UINT, ctypes.POINTER(ctypes.c_void_p),
                                 wintypes.HANDLE, wintypes.DWORD]
g32.CreateDIBSection.restype = wintypes.HBITMAP
g32.CreateBitmap.argtypes = [ctypes.c_int, ctypes.c_int, wintypes.UINT, wintypes.UINT, ctypes.c_void_p]
g32.CreateBitmap.restype = wintypes.HBITMAP
g32.DeleteObject.argtypes = [wintypes.HANDLE]
sh32.Shell_NotifyIconW.argtypes = [wintypes.DWORD, ctypes.POINTER(NOTIFYICONDATAW)]
k32.CreateMutexW.argtypes = [ctypes.c_void_p, wintypes.BOOL, wintypes.LPCWSTR]
k32.CreateMutexW.restype = wintypes.HANDLE
k32.GetModuleHandleW.restype = wintypes.HMODULE


# what the lanes are doing --------------------------------------------------------------------------------------------
def read_state():
    s = hg.settings()
    free, total = hg.mem_mb()[:2]
    lanes = hg.lane_rows()   # every slot: a second cpu job running at once is its own row
    eta = hg.estimate(lanes)   # adds usual_s / left_s to each job from the run log
    try:
        cloud = hg.cloud_summary()   # reads cloud.json only; the refresher thread is what calls gh
    except Exception as e:
        cloud = {'state': 'error', 'error': repr(e)[:120], 'active': [], 'recent': []}
    try:
        mine = hg.cloud_mine()   # builds HeavyGate sent to GitHub: shown at once, without waiting for a gh refresh
    except Exception:
        mine = []
    if mine:
        cloud = dict(cloud or {'state': 'ok'}, state='ok')
        seen = {a.get('repo') for a in mine}
        cloud['active'] = mine + [a for a in (cloud.get('active') or []) if a.get('repo') not in seen]
    try:
        disk = hg.disk_state(lanes)
    except Exception:
        disk = None
    return {'free_mb': free, 'total_mb': total, 'lanes': lanes, 'eta': eta, 'power': None, 'ollama': hg.ollama_seen(),
            'low_ram': free < int(s['reserve_mb']) + LOW_RAM_EXTRA_MB, 'ram_wait_min': s['ram_wait_min'],
            'cloud': cloud, 'disk': disk, 'cloud_mine': bool(mine)}


def disk_low(st):
    """[(drive, free GB)] under the floor, from read_state's disk entry."""
    dk = st.get('disk') or {}
    return [(d, g) for d, g in (dk.get('free_gb') or {}).items() if g < dk.get('floor_gb', 0)]


def github_links(st, recent=8):
    """[(menu text, url) or None for a separator]: builds on GitHub now, recent runs, each mapped repo's Actions page,
    the billing page. From what the tray already holds; no gh call."""
    cl = (st or {}).get('cloud') or {}
    out = []

    def run_url(r):
        if r.get('url'):
            return r['url']
        return f"https://github.com/{r['repo']}/actions/runs/{r['databaseId']}" if r.get('databaseId') else None

    for a in cl.get('active') or []:
        u = run_url(a)
        if u:
            took = hg.fmt_secs(a['for_s']) if a.get('for_s') is not None else a.get('status', '')
            out.append((f"\u25ba {_cloud_label(a)}  ({took}, running)", u))
    done = [r for r in cl.get('recent') or [] if run_url(r)][:recent]
    if done:
        if out:
            out.append(None)
        for r in done:
            when = (r.get('updatedAt') or r.get('createdAt') or '')[5:16].replace('T', ' ')
            out.append((f"{r.get('conclusion') or r.get('status') or ''}  {_cloud_label(r)}  {when}", run_url(r)))
    try:
        repos = sorted({j['repo'] for j in hc.read_jobs(hg.home()) if j.get('repo')})
    except Exception:
        repos = []
    if repos:
        out.append(None)
        out += [(f"Actions page: {r.split('/')[-1]}", f'https://github.com/{r}/actions') for r in repos]
    out += [None, ('GitHub minutes and billing', 'https://github.com/settings/billing/usage')]
    return out


def _cloud_label(a):
    """'MyApp Android (phone + TV)'; the repo name once when the job's own name already starts with it."""
    repo, name = a['repo'].split('/')[-1], (a.get('workflowName') or '').strip()
    return name if name.lower().startswith(repo.lower()) else f'{repo} {name}'.strip()


def cloud_rows(cl, alone=False):
    """The strip's CLOUD rows: each GitHub run in flight (two at most), else one quiet line with the minutes left.
    alone: the strip is up only for the cloud, so an unreadable or idle cloud adds nothing."""
    if not cl:
        return []
    if cl.get('state') != 'ok':
        return [] if alone else [[('CLOUD ', 'dim'), (hc.short_line(cl).replace('cloud: ', ''), 'dim')]]
    warn = 'wait' if cl.get('level') in ('low', 'over') else 'dim'
    left = (f" · {hc.fmt_min(cl['left_min'])} min left", warn) if cl.get('left_min') is not None else ('', 'dim')
    act = cl.get('active') or []
    if not act:
        return [] if alone else [[('CLOUD ', 'dim'), ('idle', 'dim'), left]]
    rows = []
    for i, a in enumerate(act[:2]):
        took = hg.fmt_secs(a['for_s']) if a.get('for_s') is not None else a.get('status', '')
        st = a.get('status')
        going = st in ('in_progress', 'downloading')
        mark, tag = (('✓ ', 'run') if st in ('built', 'success') else ('✗ ', 'stop') if a.get('ended_ts') else
                     ('► ', 'run') if going else ('■ ', 'wait'))
        url = a.get('url') or (f"https://github.com/{a['repo']}/actions/runs/{a['databaseId']}"
                               if a.get('repo') and a.get('databaseId') else None)
        state = ('  built' if st in ('built', 'success') else f'  {st}') if a.get('ended_ts') else \
            ('  downloading' if st == 'downloading' else '')
        rows.append([('CLOUD ', 'dim'), (mark, tag), (short(_cloud_label(a), 34), 'name', url), (f'  {took}', 'dim'),
                     (state, tag)] + ([left] if i == 0 else []))
        rows += runner_rows(a)
    if len(act) > 2:
        rows.append([(f'      +{len(act) - 2} more on GitHub', 'dim')])
    return rows


SPARK = '▁▂▃▄▅▆▇█'


def spark(vals, top):
    """A sparkline of vals on a 0..top scale (the last readings, oldest first)."""
    if not vals or not top:
        return ''
    return ''.join(SPARK[max(0, min(7, int(float(v) / top * 8)))] for v in vals)


def runner_rows(a, now=None):
    """The rows under a GitHub run: the runner's CPU, RAM and disk with sparklines (from the probe in the workflow),
    or why there are none yet."""
    r = a.get('runner') or {}
    n = r.get('now')
    if a.get('ended_ts'):
        out = []
        if n:   # the last reading: the run's peaks
            out.append([('      peak ', 'dim'), (f"CPU {n.get('peak_cpu') or n['cpu']:.0f}%", 'name'),
                        (f"  RAM {n.get('peak_ram') or n['ram']:.1f} of {n['ram_total']:.0f} GB", 'name'),
                        (f"  disk {n['disk']:.0f} of {n['disk_total']:.0f} GB", 'dim')])
        files = a.get('files') or []
        if files and a.get('dest'):
            out.append([('      in ', 'dim'), (short(a['dest'], 40), 'name'), (': ' + ', '.join(files[:2])
                        + (f' +{len(files) - 2}' if len(files) > 2 else ''), 'dim')])
        return out
    if a.get('status') not in ('in_progress', 'downloading'):
        return []
    if not n:
        quiet = (a.get('for_s') or 0) > 90
        return [[('      runner: ', 'dim'), ('no readings (heavy cloud probe adds them)' if quiet else 'starting…', 'dim')]]
    now = time.time() if now is None else now
    age = f" · {hg.fmt_secs(max(0, now - n['t']))} ago" if n.get('t') else ''
    ram_full, disk_full = n['ram'] / max(n['ram_total'], 0.1), n['disk'] / max(n['disk_total'], 0.1)
    return [[('      CPU ', 'dim'), (f"{n['cpu']:3.0f}% ", 'name'), (spark(r.get('cpu_hist'), 100), 'run'),
             ('  RAM ', 'dim'), (f"{n['ram']:.1f}/{n['ram_total']:.0f} GB ", 'wait' if ram_full > 0.9 else 'name'),
             (spark(r.get('ram_hist'), n['ram_total']), 'wait' if ram_full > 0.9 else 'run')],
            [('      disk ', 'dim'), (f"{n['disk']:.0f}/{n['disk_total']:.0f} GB", 'wait' if disk_full > 0.9 else 'name'),
             (f" · {n['cores']} cores{age}", 'dim')]]


def lane_look(L):
    """(state, queued) for the icon: state is free, running or ram."""
    j = L['job']
    state = 'free' if not j else 'running' if j['state'] == 'running' else 'ram'
    return state, bool(L['queue'])


def short(text, n):
    return text if len(text) <= n else text[:n - 1] + '…'


def done_text(st):
    """'done ~23:40' for the tooltip, or '' when nothing is known."""
    e = st.get('eta')
    return f" · done ~{e['done_at']}" if e and e['done_in_s'] and busy(st) else ''


def tooltip(st):
    """The hover text. Windows cuts it at 127 characters, so names are shortened."""
    lines = [f"HeavyGate: {st['free_mb'] / 1024:.1f} GB RAM free" + (' (low)' if st['low_ram'] else '') + done_text(st)]
    low = disk_low(st)
    if low:
        lines.append('DISK LOW: ' + ', '.join(f'{d[:2]} {g:.0f} GB' for d, g in low))
    for L in st['lanes']:
        j, q = L['job'], L['queue']
        if not j:
            text = 'free' + (f', {len(q)} queued' if q else '')
        else:
            what = 'waits for RAM' if j['state'] == 'waiting for RAM' else hg.fmt_secs(j['for_s'])
            text = f"{short(j['name'], 26)} {what}" + (f' +{len(q)} queued' if q else '')
        lines.append(f"{L['lane'].upper()}: {text}")
    if st.get('ollama'):
        lines.append(f"Ollama: {sum(m['mb'] for m in st['ollama']) / 1024:.1f} GB loaded")
    if st.get('cloud'):
        lines.append('CLOUD: ' + hc.short_line(st['cloud']).replace('cloud: ', ''))
    return '\n'.join(lines)[:127]


def menu_lines(st):
    """The read-only part of the menu: every running, waiting and queued job, in full, with its estimate."""
    out = [f"RAM {st['free_mb'] / 1024:.1f} of {st['total_mb'] / 1024:.1f} GB free"
           + ('  (low)' if st['low_ram'] else '')]
    dk = st.get('disk') or {}
    if dk.get('free_gb'):
        low = {d for d, _ in disk_low(st)}
        out.append('Disk free: ' + ', '.join(f"{d[:2] if len(d) == 3 else d} {g:,.0f} GB" + (' (LOW: jobs there wait)'
                                                                                              if d in low else '')
                                             for d, g in dk['free_gb'].items())
                   + f"  ·  jobs need {dk['floor_gb']:g} GB to start")
    if st.get('ollama'):
        out.append('Ollama: ' + hg.ollama_text(st['ollama']))
    if st.get('eta'):
        out.append(hg.eta_line(st['eta'], busy(st)))
    for L in st['lanes']:
        j, q = L['job'], L['queue']
        out.append(None)
        out.append(f"{L['lane'].upper()} lane: " + ('free' if not j and not q else
                   ('running' if j and j['state'] == 'running' else 'waiting for RAM' if j else 'nothing running')
                   + (f', {len(q)} queued' if q else '')))
        if j:
            took = hg.fmt_secs(j['for_s']) if j['for_s'] is not None else '?'
            note = f"  ·  {hg.eta_note(j)}" if 'usual_s' in j else ''
            if 'need_mb' in j:
                out.append(f"     waiting for RAM: {j['name']}  {took}  (needs {j['need_mb'] / 1024:.1f} GB, "
                           f"{j['free_mb'] / 1024:.1f} GB free){note}")
            else:
                out.append(f"     {j['state']}: {j['name']}  {took}{note}")
            out.append(f"        {j['cwd']}")
        for i, qj in enumerate(q, 1):
            note = f"  ·  {hg.eta_note(qj)}" if 'usual_s' in qj else ''
            out.append(f"     queued #{i}: {qj['name']}  {hg.fmt_secs(qj['for_s'])}{note}")
    if st.get('cloud'):
        out.append(None)
        out += [x.strip() for x in hc.status_lines(st['cloud'])]
    return out


def busy(st):
    """Something to show: a lane job running or queued, or a build HeavyGate sent to GitHub."""
    return any(L['job'] or L['queue'] for L in st['lanes']) or bool(st.get('cloud_mine'))


def power_rows(p):
    """The strip's "shut down when done" line. p is App.power: {'phase': armed | idle | countdown, 'secs'}."""
    if not p:
        return []
    if p['phase'] == 'armed':
        return [[('Shut down when done: on', 'name'), ('  (tray menu to cancel)', 'dim')]]
    when = hg.fmt_secs(max(p['secs'], 0))
    if p['phase'] == 'idle':
        return [[(f'Shutting down in {when}', 'wait'), (' unless a job starts · click to cancel', 'dim')]]
    return [[(f'SHUTTING DOWN in {when}', 'stop'), (' · click here to cancel', 'name')]]


def strip_lines(st, biggest=None):
    """The strip's rows as [(text, tag)] runs; [] when nothing is running, waiting or queued (the strip hides) and
    no shutdown is counting down. Tags are colours: name, dim, run, wait, stop."""
    p = st.get('power')
    if not busy(st):   # GitHub runs alone bring the strip up only when the user chose that (tray.json cloud_strip)
        return (cloud_rows(st.get('cloud'), alone=True) if st.get('cloud_alone') else []) + (
            power_rows(p) if p and p['phase'] != 'armed' else [])
    rows = []
    for L in st['lanes']:
        j, q, lane = L['job'], L['queue'], L['lane'].upper()
        if j:
            waiting = j['state'] != 'running'
            took = hg.fmt_secs(j['for_s']) if j['for_s'] is not None else '?'
            left = ''
            if j.get('usual_s') is not None:
                left = (f" · ~{hg.fmt_eta(j['left_s'])} left" if j.get('left_s') else ' · over its usual time'
                        ) if not waiting else f" · usually {hg.fmt_eta(j['usual_s'])}"
            stalled = j.get('stalled_s') is not None
            rows.append([(f'{lane} ', 'dim'), ('■ ' if waiting or stalled else '► ', 'stop' if stalled else 'wait' if waiting else 'run'),
                         (short(j['name'], 34), 'name'), (f'  {took}', 'dim'),
                         (f" · STALLED {hg.fmt_eta(j['stalled_s'])}", 'stop') if stalled else (left, 'dim')])
            if 'need_mb' in j:
                rows.append([(f"      waiting for RAM: needs {j['need_mb'] / 1024:.1f} GB, "
                              f"{j['free_mb'] / 1024:.1f} free", 'wait')])
        else:
            rows.append([(f'{lane}   ', 'dim'), ('starting' if q else 'free', 'dim')])
        if q:
            names = ', '.join(short(x['name'], 24) for x in q[:2]) + (f' +{len(q) - 2} more' if len(q) > 2 else '')
            rows.append([('      next: ', 'dim'), (names, 'dim')])
    e = st.get('eta')
    if e and e['done_in_s']:
        rows.append([('All done around ', 'dim'), (e['done_at'], 'name'), (f" (~{hg.fmt_eta(e['done_in_s'])})", 'dim')]
                    + ([(f" + {len(e['unknown'])} with no past runs", 'dim')] if e['unknown'] else []))
    rows += cloud_rows(st.get('cloud'))
    foot = f"RAM {st['free_mb'] / 1024:.1f} GB free"
    if biggest:
        foot += ' · biggest: ' + ', '.join(f'{n} {mb / 1024:.1f} GB' for n, mb in biggest[:2])
    rows.append([(foot, 'wait' if st['low_ram'] else 'dim')])
    low = disk_low(st)
    if low:
        rows.append([('DISK LOW: ' + ', '.join(f'{d[:2]} {g:.1f} GB free' for d, g in low)
                      + f" (jobs need {st['disk']['floor_gb']:g} GB to start)", 'stop')])
    return rows + power_rows(p)


def load_prefs():
    try:
        with open(PREFS, encoding='utf-8') as f:
            p = json.load(f)
        return p if isinstance(p, dict) else {}
    except (OSError, ValueError):
        return {}


def save_prefs(p):
    try:
        os.makedirs(os.path.dirname(PREFS), exist_ok=True)
        with open(PREFS, 'w', encoding='utf-8') as f:
            json.dump(p, f, indent=2)
    except OSError:
        pass


def work_area():
    r = wintypes.RECT()
    u32.SystemParametersInfoW(0x30, 0, ctypes.byref(r), 0)   # SPI_GETWORKAREA: the main screen less the taskbar
    return r


def motion_on():
    """False when Windows animations are off (Settings > Accessibility > Visual effects): no fades then."""
    on = wintypes.BOOL(True)
    u32.SystemParametersInfoW(0x1042, 0, ctypes.byref(on), 0)   # SPI_GETCLIENTAREAANIMATION
    return bool(on.value)


def solid_wanted():
    """True when Windows has transparency effects off or a high-contrast theme on: the strip is then fully opaque."""
    hc = HIGHCONTRASTW(cbSize=ctypes.sizeof(HIGHCONTRASTW))
    if u32.SystemParametersInfoW(0x42, hc.cbSize, ctypes.byref(hc), 0) and hc.dwFlags & 1:   # HCF_HIGHCONTRASTON
        return True
    try:
        import winreg
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER,
                            r'Software\Microsoft\Windows\CurrentVersion\Themes\Personalize') as k:
            return winreg.QueryValueEx(k, 'EnableTransparency')[0] == 0
    except OSError:
        return False


def fullscreen_in_front(near=None):
    """True when the window in front fills its whole screen (a game, a video, a presentation) and, given `near` (a
    window handle), that screen is the one `near` is on. The desktop and the taskbar don't count."""
    h = u32.GetForegroundWindow()
    if not h:
        return False
    cls = ctypes.create_unicode_buffer(64)
    u32.GetClassNameW(h, cls, 64)
    if cls.value in ('Progman', 'WorkerW', 'Shell_TrayWnd', 'Shell_SecondaryTrayWnd'):
        return False
    mon = u32.MonitorFromWindow(h, 2)   # MONITOR_DEFAULTTONEAREST
    if near and u32.MonitorFromWindow(near, 2) != mon:
        return False
    mi, r = MONITORINFO(cbSize=ctypes.sizeof(MONITORINFO)), wintypes.RECT()
    if not u32.GetMonitorInfoW(mon, ctypes.byref(mi)) or not u32.GetWindowRect(h, ctypes.byref(r)):
        return False
    m = mi.rcMonitor
    return r.left <= m.left and r.top <= m.top and r.right >= m.right and r.bottom >= m.bottom


NVML_RETRY_S = 30


class Meters:
    """CPU, GPU and RAM use for the graphs, as (time, cpu %, gpu % or None, ram used %) rows for the last
    GRAPH_SECONDS. GetSystemTimes, NVML (nvml.dll ships with the NVIDIA driver; no nvidia-smi process) and
    GlobalMemoryStatusEx: each read takes well under a millisecond. Without NVML the GPU graph says n/a."""
    def __init__(self):
        self.cpu, self.hist, self.nvml, self.dev = hg.CpuMeter(), collections.deque(), None, None
        self.retry_at = None   # when to reconnect after the driver dropped our handle
        self.connect()

    def connect(self):
        self.nvml = self.dev = None
        try:
            lib, dev = ctypes.CDLL('nvml.dll'), ctypes.c_void_p()
            if lib.nvmlInit_v2() == 0 and lib.nvmlDeviceGetHandleByIndex_v2(0, ctypes.byref(dev)) == 0:
                self.nvml, self.dev = lib, dev
        except (OSError, AttributeError):
            pass

    def gpu(self):
        """GPU use in percent, or None. A device handle can go stale under us, and using one is an access violation
        (ctypes raises it as OSError), so a bad read drops the handle and tries a new one in NVML_RETRY_S."""
        if not self.nvml:
            if self.retry_at is None or time.time() < self.retry_at:
                return None
            self.retry_at = None
            self.connect()
            if not self.nvml:
                self.retry_at = time.time() + NVML_RETRY_S
                return None
        u = (ctypes.c_uint * 2)()   # nvmlUtilization_t: gpu %, memory controller %
        try:
            return int(u[0]) if self.nvml.nvmlDeviceGetUtilizationRates(self.dev, u) == 0 else None
        except OSError as e:
            log(f'GPU reading failed, reconnecting in {NVML_RETRY_S} s: {e!r}')
            self.nvml = self.dev = None
            self.retry_at = time.time() + NVML_RETRY_S
            return None

    def sample(self, now=None):
        now = time.time() if now is None else now
        free, total = hg.mem_mb()[:2]
        row = (now, self.cpu.percent(), self.gpu(), round(100 * (1 - free / total)) if total else 0)
        self.hist.append(row)
        while self.hist and self.hist[0][0] < now - GRAPH_SECONDS - 5:
            self.hist.popleft()
        return row

    def close(self):
        if self.nvml:
            try:
                self.nvml.nvmlShutdown()
            except OSError:
                pass
            self.nvml = None


def blend(fg, bg, a):
    """fg over bg at opacity a, as #rrggbb: tkinter has no alpha, so the graphs' see-through fill is mixed here."""
    f, b = (tuple(int(c[i:i + 2], 16) for i in (1, 3, 5)) for c in (fg, bg))
    return '#' + ''.join(f'{round(x * a + y * (1 - a)):02x}' for x, y in zip(f, b))


def graph_points(hist, idx, now, x0, w, top, h):
    """Canvas points for one series: newest at the right edge, GRAPH_SECONDS across, 0-100 % bottom to top."""
    return [(x0 + w * (1 - (now - r[0]) / GRAPH_SECONDS), top + h - h * min(max(r[idx], 0), 100) / 100)
            for r in hist if r[idx] is not None and now - r[0] <= GRAPH_SECONDS]


def draw_graphs(cv, hist, width, now=None):
    """Three small graphs side by side: CPU, GPU, RAM. Each has its current value, a see-through area, and the
    highest point of the last two minutes marked with a dot and its value, so a spike stays visible."""
    cv.delete('all')
    now = now if now is not None else (hist[-1][0] if hist else time.time())
    gap, label_h = 8, 12
    pw = (width - 2 * gap) / 3
    last = hist[-1] if hist else None
    for k, (key, idx) in enumerate((('cpu', 1), ('gpu', 2), ('ram', 3))):
        x0, color = k * (pw + gap), STRIP[key]
        cv.create_rectangle(x0, label_h, x0 + pw, GRAPH_H, fill=STRIP['panel'], outline='')
        pts = graph_points(hist, idx, now, x0, pw, label_h + 1, GRAPH_H - label_h - 2)
        cur = last[idx] if last else None
        cv.create_text(x0, 0, anchor='nw', font=STRIP['small'], fill=color,
                       text=f"{key.upper()} {cur}%" if cur is not None else f"{key.upper()} n/a")
        if len(pts) >= 2:
            cv.create_polygon(*pts, pts[-1][0], GRAPH_H, pts[0][0], GRAPH_H, fill=blend(color, STRIP['panel'], 0.32),
                              outline='')
            cv.create_line(*pts, fill=blend(color, STRIP['panel'], 0.9), width=1)
            peak = min(pts, key=lambda p: p[1])
            peak_v = max(r[idx] for r in hist if r[idx] is not None and now - r[0] <= GRAPH_SECONDS)
            cv.create_oval(peak[0] - 2, peak[1] - 2, peak[0] + 2, peak[1] + 2, fill=color, outline='')
            cv.create_text(x0 + pw, 0, anchor='ne', font=STRIP['small'], fill=STRIP['dim'], text=f'peak {peak_v}')


class Strip:
    """The text strip: a small window that never takes focus, placed by its bottom-right corner, so it grows up and to
    the left as rows come and go. It fades in and out, rests a little see-through and turns solid under the mouse.
    It stays on top unless you send it behind (until you click it or the lanes go idle), switch "on top" off, or a
    full-screen app is in front on its screen."""
    def __init__(self, root, prefs, on_menu):
        self.prefs, self.on_menu = prefs, on_menu
        self.win = tk.Toplevel(root)
        self.win.withdraw()
        self.win.overrideredirect(True)
        self.frame = tk.Frame(self.win, bg=STRIP['border'], padx=1, pady=1)
        self.frame.pack()
        self.body = tk.Frame(self.frame, bg=STRIP['bg'])
        self.body.pack()
        self.text = tk.Text(self.body, bg=STRIP['bg'], fg=STRIP['name'], font=STRIP['font'], bd=0, padx=10, pady=6,
                            highlightthickness=0, cursor='arrow', takefocus=0, wrap='none', height=1, width=10)
        self.text.grid(row=0, column=0, sticky='nw')
        for tag in ('name', 'dim', 'run', 'wait', 'stop'):
            self.text.tag_configure(tag, foreground=STRIP[tag])
        self.text.tag_configure('link', underline=True)   # a GitHub run: a click opens it in the browser
        self.text.tag_bind('link', '<Enter>', lambda e: self.text.configure(cursor='hand2'))
        self.text.tag_bind('link', '<Leave>', lambda e: self.text.configure(cursor='arrow'))
        self.urls = {}
        self.buttons = tk.Frame(self.body, bg=STRIP['bg'])
        self.buttons.grid(row=0, column=1, sticky='ne', padx=(0, 4), pady=(2, 0))
        for glyph, fn in (('▾', self.send_behind), ('×', self.dismiss)):   # behind other windows / hide until idle
            b = tk.Label(self.buttons, text=glyph, bg=STRIP['bg'], fg=STRIP['dim'], font=('Segoe UI Symbol', 9),
                         cursor='hand2', padx=3, pady=0)
            b.pack(side='left')
            b.bind('<ButtonPress-1>', lambda e: 'break')
            b.bind('<ButtonRelease-1>', lambda e, fn=fn: self.button(fn))
            b.bind('<Enter>', lambda e, b=b: b.configure(fg=STRIP['name']))
            b.bind('<Leave>', lambda e, b=b: b.configure(fg=STRIP['dim']))
        self.canvas = tk.Canvas(self.body, height=GRAPH_H, width=240, bg=STRIP['bg'], highlightthickness=0, bd=0)
        self.canvas.grid(row=1, column=0, columnspan=2, sticky='w', padx=10, pady=(0, 8))
        # bound on the window only: every widget inside it carries the window's tag, so these see all of its clicks
        self.win.bind('<ButtonPress-1>', self.press)
        self.win.bind('<B1-Motion>', self.drag)
        self.win.bind('<ButtonRelease-1>', self.release)
        self.win.bind('<ButtonRelease-3>', lambda e: self.on_menu())
        self.win.bind('<Enter>', lambda e: self.hover(True))
        self.win.bind('<Leave>', lambda e: self.win.after(60, self.check_hover))
        self.rows, self.shown, self.styled, self.grab = None, False, False, None
        self.behind = self.dismissed = self.fullscreen = self.hovered = self.counting = False
        self.on_cancel = None   # set by the app: cancels "shut down when done"
        self.top_now, self.alpha, self.fade_id, self.graph_w = None, 0.0, None, None
        self.animate, self.alpha_override = motion_on(), None   # the selftest pins both
        self.anchor = self.saved_anchor()
        self.apply_z()

    @property
    def hwnd(self):
        return int(self.win.wm_frame(), 16)

    @property
    def graphs_on(self):
        return self.prefs.get('graphs', True)

    def rest_alpha(self):
        if solid_wanted():
            return 1.0
        try:
            return min(max(float(self.prefs.get('strip_opacity', REST_OPACITY)), 0.3), 1.0)
        except (TypeError, ValueError):
            return REST_OPACITY

    def target_alpha(self):
        return 1.0 if self.hovered else self.rest_alpha()

    def set_alpha(self, a):
        self.alpha = a
        self.win.attributes('-alpha', self.alpha_override if self.alpha_override is not None else a)

    def fade_to(self, target, ms, then=None):
        """Ease the window's opacity to target over ms (at once when Windows animations are off), then call then."""
        if self.fade_id:
            self.win.after_cancel(self.fade_id)
            self.fade_id = None
        if not self.animate or ms <= 0 or abs(self.alpha - target) < 0.01:
            self.set_alpha(target)
            if then:
                then()
            return
        start, a0 = time.perf_counter(), self.alpha

        def step():
            t = min(1.0, (time.perf_counter() - start) * 1000 / ms)
            self.set_alpha(a0 + (target - a0) * (1 - (1 - t) ** 3))   # ease out
            if t < 1:
                self.fade_id = self.win.after(15, step)
            else:
                self.fade_id = None
                if then:
                    then()
        step()

    def hover(self, on):
        if on != self.hovered:
            self.hovered = on
            if self.shown:
                self.fade_to(self.target_alpha(), HOVER_MS)

    def check_hover(self):
        """Leave also fires when the pointer moves between widgets inside the strip, so look where it really is."""
        x, y = self.win.winfo_pointerxy()
        wx, wy = self.win.winfo_rootx(), self.win.winfo_rooty()
        self.hover(wx <= x < wx + self.win.winfo_width() and wy <= y < wy + self.win.winfo_height())

    def apply_z(self, force=False):
        top = self.prefs.get('strip_on_top', True) and not self.behind and not self.fullscreen
        if force or top != self.top_now:
            self.top_now = top
            self.win.attributes('-topmost', top)
            if not top:   # under every other window, without taking focus
                u32.SetWindowPos(self.hwnd, wintypes.HWND(1), 0, 0, 0, 0, 0x13)   # HWND_BOTTOM; NOSIZE|NOMOVE|NOACTIVATE

    def send_behind(self):
        self.behind = True
        self.apply_z(force=True)

    def bring_front(self):
        self.behind = False
        self.apply_z(force=True)

    def dismiss(self):
        """Hide until both lanes are next idle; the next busy spell shows it again."""
        self.dismissed = True
        self.hide()

    def button(self, fn):
        self.grab = None
        fn()
        return 'break'

    def saved_anchor(self):
        a = self.prefs.get('strip_anchor')
        if isinstance(a, list) and len(a) == 2:
            if u32.MonitorFromPoint(wintypes.POINT(int(a[0]) - 8, int(a[1]) - 8), 0):   # still on a screen
                return int(a[0]), int(a[1])
        wa = work_area()
        return wa.right - 12, wa.bottom - 12

    def reset_position(self):
        self.prefs.pop('strip_anchor', None)
        save_prefs(self.prefs)
        self.anchor = self.saved_anchor()
        self.place()

    def no_activate(self):
        """Clicks must not pull focus from whatever you are typing in, and the strip gets no taskbar button."""
        if self.styled:
            return
        hwnd = u32.GetParent(self.hwnd) or self.hwnd
        for h in {self.hwnd, hwnd}:
            ex = u32.GetWindowLongPtrW(h, -20)   # GWL_EXSTYLE
            u32.SetWindowLongPtrW(h, -20, ex | 0x08000000 | 0x80)   # WS_EX_NOACTIVATE | WS_EX_TOOLWINDOW
        self.styled = True

    def hide(self):
        if self.shown:
            self.shown = False
            self.fade_to(0.0, FADE_OUT_MS, then=self.withdraw)

    def withdraw(self):
        if not self.shown:
            self.win.withdraw()

    def show(self, st, biggest=None, enabled=True, hist=None):
        if not busy(st):   # a quiet spell ends "sent behind" and "hidden until idle"
            self.dismissed = False
            if self.behind:
                self.bring_front()
        p = st.get('power')
        self.counting = bool(p and p['phase'] in ('idle', 'countdown'))   # a shutdown on its way always shows
        rows = strip_lines(st, biggest) if (enabled and not self.dismissed) or self.counting else []
        if not rows:
            self.hide()
            self.rows = rows
            return
        if self.counting and self.behind:
            self.bring_front()
        stalled = any(L['job'] and L['job'].get('stalled_s') is not None for L in st['lanes'])
        border = STRIP['border_low'] if st['low_ram'] or stalled or (p and p['phase'] == 'countdown') else STRIP['border_wait'] if any(
            L['job'] and L['job']['state'] != 'running' for L in st['lanes']) else STRIP['border']
        self.frame.configure(bg=border)
        if rows != self.rows:
            self.rows = rows
            t = self.text
            t.configure(state='normal')
            t.delete('1.0', 'end')
            self.urls = {}
            for i, row in enumerate(rows):
                for text, tag, *url in row:
                    if url and url[0]:
                        key = f'url{len(self.urls)}'
                        self.urls[key] = url[0]
                        t.insert('end', text, (tag, 'link', key))
                    else:
                        t.insert('end', text, tag)
                if i < len(rows) - 1:
                    t.insert('end', '\n')
            t.configure(state='disabled', height=len(rows), width=max(sum(len(x[0]) for x in r) for r in rows))
        if self.graphs_on:
            self.canvas.grid()
        else:
            self.canvas.grid_remove()
        self.draw(hist)
        if not self.shown:
            self.shown = True
            if self.win.state() == 'withdrawn':
                self.set_alpha(0.0)
                self.win.deiconify()
            self.place()
            self.no_activate()
            self.apply_z(force=True)
            self.fade_to(self.target_alpha(), FADE_IN_MS)
        self.place()

    def draw(self, hist):
        """Redraw the graphs, as wide as the text above them."""
        if not self.graphs_on or hist is None:
            return
        self.win.update_idletasks()
        w = max(240, self.text.winfo_reqwidth() + self.buttons.winfo_reqwidth() + 4 - 20)
        if w != self.graph_w:
            self.graph_w = w
            self.canvas.configure(width=w)
        draw_graphs(self.canvas, hist, w)

    def place(self):
        self.win.update_idletasks()
        w, h = self.win.winfo_reqwidth(), self.win.winfo_reqheight()
        self.win.geometry(f'+{self.anchor[0] - w}+{self.anchor[1] - h}')

    def press(self, e):
        self.grab = (e.x_root, e.y_root, self.anchor, False)

    def drag(self, e):
        if not self.grab:
            return
        x0, y0, (ax, ay), moved = self.grab
        if moved or abs(e.x_root - x0) + abs(e.y_root - y0) > 4:
            self.grab = (x0, y0, (ax, ay), True)
            self.anchor = (ax + e.x_root - x0, ay + e.y_root - y0)
            self.place()

    def release(self, e):
        if self.grab and self.grab[3]:
            self.prefs['strip_anchor'] = list(self.anchor)
            save_prefs(self.prefs)
        elif self.grab and self.counting and self.on_cancel:   # a click during a shutdown countdown cancels it
            self.on_cancel()
        elif self.grab and self.open_link(e):   # a GitHub run's name: opens it in the browser
            pass
        elif self.grab and self.behind:   # a click on a strip that was sent behind brings it back
            self.bring_front()
        self.grab = None

    def link_at(self, e):
        """The URL under a click on the strip's text, or None."""
        if e.widget is not self.text:
            return None
        for name in self.text.tag_names(f'@{e.x},{e.y}'):
            if name in self.urls:
                return self.urls[name]
        return None

    def open_link(self, e):
        url = self.link_at(e)
        if url and url.startswith('https://github.com/'):   # only links the tray built itself
            os.startfile(url)
            return True
        return False

    def grab_bits(self):
        """One PrintWindow of this window only, never the screen: (printed, width, height, BGRA bytes)."""
        hwnd = int(self.win.wm_frame(), 16)
        w, h = self.win.winfo_width(), self.win.winfo_height()
        screen = u32.GetDC(None)
        dc = g32.CreateCompatibleDC(screen)
        bmi = BITMAPINFOHEADER(biSize=ctypes.sizeof(BITMAPINFOHEADER), biWidth=w, biHeight=-h, biPlanes=1,
                               biBitCount=32, biCompression=0)
        bits = ctypes.c_void_p()
        bmp = g32.CreateDIBSection(screen, ctypes.byref(bmi), 0, ctypes.byref(bits), None, 0)
        old = g32.SelectObject(dc, bmp)
        ok = u32.PrintWindow(hwnd, dc, 2)   # PW_RENDERFULLCONTENT
        data = bytearray(ctypes.string_at(bits, w * h * 4))
        for i in range(3, len(data), 4):
            data[i] = 255
        g32.SelectObject(dc, old)
        g32.DeleteObject(bmp)
        g32.DeleteDC(dc)
        u32.ReleaseDC(None, screen)
        return bool(ok), w, h, bytes(data)

    def capture(self, path):
        """Save what the strip window itself draws. Returns (printed, width, height, bright pixels, graph pixels): an
        unpainted capture is all black, and graph pixels are the exact colours of the graph lines (text edges are
        ClearType-tinted, so a colour range would count them). Right after a resize the window can be half repainted,
        more so on a busy GPU, so it waits for two identical, non-black captures in a row (up to about 3 s)."""
        last = None
        for _ in range(40):
            self.win.update()
            time.sleep(0.05)
            self.win.update()
            ok, w, h, data = self.grab_bits()
            if last == (w, h, data) and any(data[i] for i in range(0, len(data), 4)):
                break
            last = (w, h, data)
        write_png(path, w, data, h)
        bright = sum(1 for i in range(0, len(data), 4) if data[i] + data[i + 1] + data[i + 2] > 300)
        lines = {bytes(int(c[k:k + 2], 16) for k in (5, 3, 1))   # the graph lines' exact colours, as BGR
                 for c in (blend(STRIP[n], STRIP['panel'], 0.9) for n in ('cpu', 'gpu', 'ram'))}
        graph = sum(1 for i in range(0, len(data), 4) if data[i:i + 3] in lines)
        return ok, w, h, bright, graph


# icon pixels ---------------------------------------------------------------------------------------------------------
def _in_rrect(x, y, x0, y0, x1, y1, r):
    cx, cy = min(max(x, x0 + r), x1 - r), min(max(y, y0 + r), y1 - r)
    return (x - cx) ** 2 + (y - cy) ** 2 <= r * r


def render(size, gpu, cpu, low_ram, ss=4):
    """Top-down BGRA bytes for the icon. gpu and cpu are (state, queued) from lane_look()."""
    pills = [((0.13, 0.13, 0.45, 0.87), gpu), ((0.55, 0.13, 0.87, 0.87), cpu)]
    bg = COLORS['bg_low' if low_ram else 'bg']
    out = bytearray(size * size * 4)
    for py in range(size):
        for px in range(size):
            r = g = b = a = 0
            for sy in range(ss):
                for sx in range(ss):
                    x, y = (px + (sx + 0.5) / ss) / size, (py + (sy + 0.5) / ss) / size
                    if not _in_rrect(x, y, 0, 0, 1, 1, 0.22):
                        continue
                    # a light rim, so the idle icon still shows on a dark taskbar
                    c = bg if _in_rrect(x, y, 0.06, 0.06, 0.94, 0.94, 0.17) else COLORS['rim']
                    for (x0, y0, x1, y1), (state, queued) in pills:
                        if _in_rrect(x, y, x0, y0, x1, y1, (x1 - x0) / 2):
                            c = COLORS[state]
                            if queued and (x - (x0 + x1) / 2) ** 2 + (y - 0.70) ** 2 <= 0.085 ** 2:
                                c = COLORS['dot'] if state != 'free' else COLORS['ram']
                    r, g, b, a = r + c[0], g + c[1], b + c[2], a + 1
            i = (py * size + px) * 4
            if a:
                out[i:i + 4] = bytes((b // a, g // a, r // a, 255 * a // (ss * ss)))
    return bytes(out)


def make_icon(size, bgra):
    bmi = BITMAPINFOHEADER(biSize=ctypes.sizeof(BITMAPINFOHEADER), biWidth=size, biHeight=-size, biPlanes=1,
                           biBitCount=32, biCompression=0)
    bits = ctypes.c_void_p()
    color = g32.CreateDIBSection(None, ctypes.byref(bmi), 0, ctypes.byref(bits), None, 0)
    ctypes.memmove(bits, bgra, len(bgra))
    mask_bytes = (ctypes.c_ubyte * (((size + 15) // 16) * 2 * size))()
    mask = g32.CreateBitmap(size, size, 1, 1, mask_bytes)
    hicon = u32.CreateIconIndirect(ctypes.byref(ICONINFO(True, 0, 0, mask, color)))
    g32.DeleteObject(color)
    g32.DeleteObject(mask)
    return hicon


def write_png(path, size, bgra, height=None):
    """BGRA bytes (size wide, height or size tall) to a PNG."""
    height = height or size
    rows = b''.join(b'\0' + b''.join(bytes((bgra[i + 2], bgra[i + 1], bgra[i], bgra[i + 3]))
                                     for i in range(y * size * 4, (y + 1) * size * 4, 4)) for y in range(height))

    def chunk(kind, data):
        return struct.pack('>I', len(data)) + kind + data + struct.pack('>I', zlib.crc32(kind + data) & 0xffffffff)
    with open(path, 'wb') as f:
        f.write(b'\x89PNG\r\n\x1a\n' + chunk(b'IHDR', struct.pack('>IIBBBBB', size, height, 8, 6, 0, 0, 0))
                + chunk(b'IDAT', zlib.compress(rows, 9)) + chunk(b'IEND', b''))


# the tray icon -------------------------------------------------------------------------------------------------------
def console_python():
    exe = os.path.join(os.path.dirname(sys.executable), 'python.exe')
    return exe if os.path.isfile(exe) else sys.executable


class Tray:
    """The notification-area icon. Its window's messages arrive through tkinter's loop (same thread)."""
    def __init__(self, app):
        self.app = app
        self.size = u32.GetSystemMetrics(49) or 16   # SM_CXSMICON: the tray's icon size at this DPI
        self.icons, self.look, self.tip = {}, None, None
        self.hinst = k32.GetModuleHandleW(None)
        self.proc = WNDPROC(self.wndproc)   # kept on self so it isn't collected while Windows can call it
        wc = WNDCLASSEXW(cbSize=ctypes.sizeof(WNDCLASSEXW), lpfnWndProc=self.proc, hInstance=self.hinst,
                         lpszClassName='HeavyGateTray')
        u32.RegisterClassExW(ctypes.byref(wc))
        # a hidden top-level window rather than a message-only one: only top-level windows hear TaskbarCreated
        self.taskbar_created = u32.RegisterWindowMessageW('TaskbarCreated')   # before the window: it gets messages at once
        self.hwnd = u32.CreateWindowExW(0, 'HeavyGateTray', 'HeavyGate', 0, 0, 0, 0, 0, None, None, self.hinst, None)
        self.nid = NOTIFYICONDATAW(cbSize=ctypes.sizeof(NOTIFYICONDATAW), hWnd=self.hwnd, uID=1,
                                   uFlags=NIF_MESSAGE | NIF_ICON | NIF_TIP, uCallbackMessage=WM_TRAY)
        self.added = False

    def icon_for(self, look):
        if look not in self.icons:
            self.icons[look] = make_icon(self.size, render(self.size, look[0], look[1], look[2]))
        return self.icons[look]

    def update(self, st, err=None, force=False):
        if st:
            look, tip = (*(lane_look(L) for L in st['lanes']), st['low_ram']), tooltip(st)
        else:   # a half-written record or a vanished folder: say so, keep running
            look, tip = (('free', False), ('free', False), False), f'HeavyGate: cannot read lanes ({err})'[:127]
        if force or look != self.look or tip != self.tip:
            self.look, self.tip = look, tip
            self.nid.hIcon = self.icon_for(look)
            self.nid.szTip = tip
            self.added = bool(sh32.Shell_NotifyIconW(NIM_MODIFY if self.added and not force else NIM_ADD,
                                                     ctypes.byref(self.nid))) or self.added

    def remove(self):
        if self.added:
            sh32.Shell_NotifyIconW(NIM_DELETE, ctypes.byref(self.nid))
            self.added = False

    def close(self):
        self.remove()
        for h in self.icons.values():
            u32.DestroyIcon(h)
        self.icons.clear()
        u32.DestroyWindow(self.hwnd)

    def show_menu(self):
        """The menu, top to bottom: the display style (pick one, with that style's settings beside it), a short
        summary of the lanes with the full detail in a submenu, the commands, then sign-in, shutdown and Exit."""
        try:
            self.app.poll()
        except Exception as e:   # the menu holds Exit, so a failed refresh must not take it away
            log(f'menu refresh failed: {e!r}')
        st = self.app.state
        m = u32.CreatePopupMenu()
        u32.AppendMenuW(m, MF_STRING | MF_GRAYED, 0, f'HeavyGate v{hg.VERSION}')
        u32.AppendMenuW(m, MF_SEPARATOR, 0, None)

        # the display style: one or the other, marked with a dot
        bar = self.app.bar_on
        u32.AppendMenuW(m, MF_STRING | MF_GRAYED, 0, 'Display style (pick one):')
        first = u32.GetMenuItemCount(m)
        u32.AppendMenuW(m, MF_STRING, CMD_SHOW_STRIP, 'Pop-up strip above the clock  (default)')
        u32.AppendMenuW(m, MF_STRING, CMD_SHOW_BAR, 'Taskbar readout: GPU | CPU | RAM')
        u32.CheckMenuRadioItem(m, first, first + 1, first + (1 if bar else 0), MF_BYPOSITION)
        sub = u32.CreatePopupMenu()
        if bar:
            u32.AppendMenuW(sub, MF_STRING | (MF_CHECKED if self.app.bar_every else 0), CMD_BAR_ALL,
                            "Show on every screen's taskbar")
            u32.AppendMenuW(m, MF_STRING | MF_POPUP, sub, 'Taskbar readout settings')
        else:
            self.strip_items(sub)
            u32.AppendMenuW(m, MF_STRING | MF_POPUP, sub, 'Pop-up strip settings')
        u32.AppendMenuW(m, MF_SEPARATOR, 0, None)

        # the lanes: a short summary here, everything in "Lanes in detail"
        summary = tooltip(st).split('\n') if st else ['cannot read the lanes']
        summary = summary[1:] + [summary[0].replace('HeavyGate: ', '')] if st else summary
        for line in summary:
            u32.AppendMenuW(m, MF_STRING, 0, line.replace('&', '&&'))
        detail = u32.CreatePopupMenu()
        hw = hg.hardware_names()
        for name in (hw.get('cpu'), hw.get('gpu')):
            if name:
                u32.AppendMenuW(detail, MF_STRING | MF_GRAYED, 0, name.replace('&', '&&'))
        for line in menu_lines(st) if st else ['cannot read the lanes']:
            if line is None:
                u32.AppendMenuW(detail, MF_SEPARATOR, 0, None)
            else:
                u32.AppendMenuW(detail, MF_STRING, 0, line.replace('&', '&&'))
        u32.AppendMenuW(m, MF_STRING | MF_POPUP, detail, 'Lanes in detail')
        links = u32.CreatePopupMenu()
        self.app.links = []
        for item in github_links(st):
            if item is None:
                u32.AppendMenuW(links, MF_SEPARATOR, 0, None)
            elif len(self.app.links) < 60:
                u32.AppendMenuW(links, MF_STRING, CMD_LINK + len(self.app.links), item[0].replace('&', '&&'))
                self.app.links.append(item[1])
        u32.AppendMenuW(m, MF_STRING | MF_POPUP, links, 'GitHub links (open in the browser)')
        u32.AppendMenuW(m, MF_SEPARATOR, 0, None)

        for cid, text in ((CMD_WATCH, 'Close live view' if self.app.watch_open() else 'Open live view'),
                          (CMD_RULES, 'Rules'), (CMD_LOG, 'Show run log'),
                          (CMD_CLOUD, 'GitHub Actions: minutes and runs'),
                          (CMD_FREE, 'Stop idle Gradle daemons'), (CMD_DEPS, 'Dependencies'),
                          (CMD_FOLDER, 'Open state folder')):
            u32.AppendMenuW(m, MF_STRING, cid, text)
        u32.SetMenuDefaultItem(m, CMD_WATCH, 0)
        u32.AppendMenuW(m, MF_SEPARATOR, 0, None)
        u32.AppendMenuW(m, MF_STRING | (MF_CHECKED if os.path.exists(hg.startup_shortcut()) else 0), CMD_STARTUP,
                        'Start at sign-in')
        self.finish_menu(m, st)

    def strip_items(self, m):
        """The pop-up strip's own switches, in their submenu while the strip is the chosen display."""
        u32.AppendMenuW(m, MF_STRING | (MF_CHECKED if self.app.prefs.get('strip', True) else 0), CMD_STRIP,
                        'Pop up while jobs run')
        u32.AppendMenuW(m, MF_STRING | (MF_CHECKED if self.app.prefs.get('cloud_strip', False) else 0),
                        CMD_CLOUD_STRIP, 'Pop up for GitHub Actions runs too')
        s = self.app.strip
        if s.dismissed:
            u32.AppendMenuW(m, MF_STRING, CMD_DISMISS, 'Show the strip again')
        elif s.shown:
            u32.AppendMenuW(m, MF_STRING, CMD_BEHIND, 'Bring strip to front' if s.behind
                            else 'Send strip behind other windows')
            u32.AppendMenuW(m, MF_STRING, CMD_DISMISS, 'Hide strip until the lanes are idle')
        u32.AppendMenuW(m, MF_STRING | (MF_CHECKED if self.app.prefs.get('strip_on_top', True) else 0), CMD_ONTOP,
                        'Keep strip on top')
        u32.AppendMenuW(m, MF_STRING | (MF_CHECKED if s.graphs_on else 0), CMD_GRAPHS, 'Show CPU, GPU and RAM graphs')
        sub = u32.CreatePopupMenu()
        now = round(s.rest_alpha() * 10)
        for tenths in (10, 9, 8, 7, 6):
            u32.AppendMenuW(sub, MF_STRING | (MF_CHECKED if tenths == now else 0), CMD_OPACITY + tenths,
                            f'{tenths * 10}%' + ('  (solid: Windows transparency is off)'
                                                 if tenths == 10 and solid_wanted() else ''))
        u32.AppendMenuW(m, MF_STRING | MF_POPUP, sub, 'Strip opacity')
        u32.AppendMenuW(m, MF_STRING, CMD_STRIP_RESET, 'Move strip back above the clock')

    def finish_menu(self, m, st):
        u32.AppendMenuW(m, MF_SEPARATOR, 0, None)
        armed = bool(hg.power_state())
        u32.AppendMenuW(m, MF_STRING | (MF_CHECKED if armed else 0), CMD_POWER,
                        'Shut down the PC when all jobs are done' + (f"  (around {st['eta']['done_at']})"
                                                                     if st and st.get('eta') and st['eta']['done_in_s']
                                                                     and busy(st) else ''))
        u32.AppendMenuW(m, MF_SEPARATOR, 0, None)
        u32.AppendMenuW(m, MF_STRING, CMD_EXIT, 'Exit')
        pt = wintypes.POINT()
        u32.GetCursorPos(ctypes.byref(pt))
        u32.SetForegroundWindow(self.hwnd)   # so the menu closes when you click elsewhere
        cid = u32.TrackPopupMenu(m, TPM_RIGHTBUTTON | TPM_RETURNCMD, pt.x, pt.y, 0, self.hwnd, None)
        u32.PostMessageW(self.hwnd, WM_NULL, 0, 0)
        u32.DestroyMenu(m)
        self.app.command(cid)

    def wndproc(self, hwnd, msg, wp, lp):
        try:
            if msg == WM_TRAY:
                if lp in (WM_LBUTTONUP, WM_RBUTTONUP):   # either click: the menu; the live view only from its item
                    self.show_menu()
                return 0
            if msg == WM_BAR_CLICK:   # a click on the taskbar readout
                self.show_menu()
                return 0
            if msg == self.taskbar_created:   # Explorer restarted and lost every tray icon
                self.added = False
                self.update(self.app.state, force=True)
                return 0
            if msg == WM_ENDSESSION and wp:
                log('Windows is ending the session')
                self.remove()
                return 0
            if msg == WM_CLOSE:
                self.app.quit('WM_CLOSE (closed by another program)')
                return 0
        except Exception as e:
            log(f'tray message {msg:#x} failed: {e!r} | '
                + traceback.format_exc().strip().replace('\n', ' | '))
        return u32.DefWindowProcW(hwnd, msg, wp, lp)


# the app: tray icon + strip on one tkinter loop ----------------------------------------------------------------------
class App:
    def __init__(self):
        u32.SetProcessDPIAware()   # before tkinter starts, so the strip's text is sharp on a scaled screen
        self.root = tk.Tk()
        self.root.withdraw()
        self.prefs = load_prefs()
        self.tray = Tray(self)
        self.strip = Strip(self.root, self.prefs, self.tray.show_menu)
        self.strip.on_cancel = self.cancel_power
        self.meters = Meters()
        self.state, self.biggest, self.ticks, self.closed, self.watch = None, None, 0, False, None
        self.power, self.idle_since = None, None
        self.bar, self.bar_icons = None, {}   # the taskbar readout starts in run(), never in the selftest
        self.root.report_callback_exception = self.callback_error
        # A plain taskkill (WM_CLOSE) used to destroy the strip's widgets and leave the process running, logging a
        # TclError every second. Closing any of our windows now ends the tray the same way Exit does.
        for w in (self.root, self.strip.win):
            w.protocol('WM_DELETE_WINDOW', lambda: self.quit('closed by Windows (WM_CLOSE)'))

    def callback_error(self, kind, e, tb):
        log('error: ' + ''.join(traceback.format_exception(kind, e, tb)).strip().replace('\n', ' | '))
        if kind is tk.TclError and 'bad window path name' in str(e) and not self.closed:
            self.quit('its windows were destroyed')   # half-closed: exit instead of erroring forever

    @property
    def bar_on(self):
        """The chosen display: 'strip' (the pop-up strip above the clock, the default) or 'taskbar' (the readout)."""
        return self.prefs.get('display') == 'taskbar'

    @property
    def strip_on(self):
        return not self.bar_on and self.prefs.get('strip', True)

    @property
    def bar_every(self):
        return self.prefs.get('taskbar_every_screen', True)

    def sampling(self):
        """True while something shows live readings: the strip's graphs or the taskbar readout. Then sample() reads
        the meters every second; otherwise poll() takes one reading per poll."""
        return (self.strip.shown and self.strip.graphs_on) or bool(self.bar)

    def bar_start(self):
        if self.bar or not self.bar_on:
            return
        self.bar = heavybar.Readout(self.tray.hwnd, WM_BAR_CLICK, self.bar_every, log)
        self.bar.start()
        self.bar.ready.wait(3)
        self.bar_push()

    def bar_stop(self):
        if self.bar:
            self.bar.stop()
            self.bar = None

    def bar_push(self):
        """Hand the readout what to show: the newest CPU/GPU/RAM reading, each lane's state, the icon, the hover
        text. It repaints on its own thread."""
        if not self.bar:
            return
        row = self.meters.hist[-1] if self.meters.hist else (0, None, None, None)
        st = self.state
        states = {'gpu': 'free', 'cpu': 'free'}
        look = (('free', False), ('free', False), False)
        if st:
            for L in st['lanes']:
                j = L['job']
                states[L['lane']] = ('stalled' if j and j.get('stalled_s') is not None else lane_look(L)[0])
            look = (*(lane_look(L) for L in st['lanes']), st['low_ram'])
        if look not in self.bar_icons:
            self.bar_icons[look] = render(self.tray.size, *look)
        low = bool(st and st['low_ram'])
        tip = tooltip(st) if st else 'HeavyGate: cannot read the lanes'
        self.bar.set({'gpu': (row[2], states['gpu']), 'cpu': (row[1], states['cpu']),
                      'ram': (row[3], 'ram' if low else 'free'), 'icon': self.bar_icons[look],
                      'tip': tip + '\nClick for the HeavyGate menu'})

    def poll(self):
        try:
            st, err = read_state(), None
        except Exception as e:
            st, err = None, e
        if st:
            st['cloud_alone'] = bool(self.prefs.get('cloud_strip', False))
        self.state = st
        self.tray.update(st, err)
        if not self.sampling():   # while the graphs or the taskbar readout show, sample() reads every second instead
            self.meters.sample()
        if st:
            if self.strip_on and busy(st):
                if self.biggest is None or self.ticks % 3 == 0:   # every process's memory: not on every tick
                    self.biggest = hg.top_memory(3)
            else:
                self.biggest = None
            self.strip.fullscreen = self.strip.shown and fullscreen_in_front(self.strip.hwnd)
            self.strip.apply_z()
            self.power_tick()
            st['power'] = self.power
            self.strip.show(st, self.biggest, self.strip_on, self.meters.hist)
        self.bar_push()
        self.ticks += 1

    def power_tick(self, now=None):
        """Shut down when done: while the switch is on and both lanes have been empty for shutdown_idle_s, count down
        shutdown_countdown_s on the strip, then shut Windows down (no force, so unsaved work can still stop it). A job
        arriving at any point starts the wait over. Sets self.power to what the strip shows."""
        now = time.time() if now is None else now
        st = self.state
        if not hg.power_state() or not st:
            self.power, self.idle_since = None, None
            return
        was = self.idle_since
        phase, left, self.idle_since, go = hg.power_step(self.idle_since, busy(st), now, hg.settings())
        if phase == 'idle' and was is None:
            log('shutdown when done: lanes empty, waiting')
        if not go:
            self.power = {'phase': phase, 'secs': left}
        else:
            hg.set_power(False)   # off first, so a shutdown that an app blocks never comes back around
            self.power, self.idle_since = None, None
            cmd = hg.shutdown_command()
            log(f'shutdown when done: lanes empty, shutting down: {cmd}')
            try:
                subprocess.Popen(cmd, creationflags=hg.NO_WINDOW)
            except OSError as e:
                log(f'shutdown failed: {e!r}')

    def cancel_power(self):
        hg.set_power(False)
        log('shutdown when done: cancelled')
        self.power, self.idle_since = None, None
        if self.state:
            self.state['power'] = None
            self.strip.show(self.state, self.biggest, self.strip_on, self.meters.hist)

    def loop(self):
        if self.closed:
            return
        try:
            self.poll()
        finally:   # one bad reading must never stop the tray for good
            self.root.after(POLL_MS, self.loop)

    def sample(self):
        """One graph reading a second, only while the strip is showing."""
        if self.closed:
            return
        try:
            if self.sampling():
                self.meters.sample()
                if self.strip.shown and self.strip.graphs_on:
                    self.strip.draw(self.meters.hist)
                self.bar_push()
            if self.power and self.power['phase'] != 'armed' and self.state:   # the countdown moves every second
                self.power_tick()
                self.state['power'] = self.power
                self.strip.show(self.state, self.biggest, self.strip_on, self.meters.hist)
        finally:
            self.root.after(SAMPLE_MS, self.sample)

    def watch_open(self):
        return bool(self.watch and self.watch.poll() is None)

    def command(self, cid):
        if cid == CMD_WATCH:   # a second click closes the live view again
            if self.watch_open():
                self.watch.terminate()
                self.watch = None
            else:
                self.watch = self.open_console('watch')
        elif cid == CMD_BEHIND:
            self.strip.bring_front() if self.strip.behind else self.strip.send_behind()
        elif cid == CMD_DISMISS:
            if self.strip.dismissed:
                self.strip.dismissed = False
                self.poll()
            else:
                self.strip.dismiss()
        elif cid == CMD_ONTOP:
            self.prefs['strip_on_top'] = not self.prefs.get('strip_on_top', True)
            save_prefs(self.prefs)
            self.strip.apply_z(force=True)
        elif cid == CMD_GRAPHS:
            self.prefs['graphs'] = not self.strip.graphs_on
            save_prefs(self.prefs)
            self.poll()
        elif CMD_OPACITY + 6 <= cid <= CMD_OPACITY + 10:
            self.prefs['strip_opacity'] = (cid - CMD_OPACITY) / 10
            save_prefs(self.prefs)
            if self.strip.shown:
                self.strip.fade_to(self.strip.target_alpha(), HOVER_MS)
        elif cid == CMD_RULES:
            subprocess.Popen([sys.executable, os.path.join(os.path.dirname(os.path.abspath(__file__)), 'heavyrules.pyw')])
        elif cid == CMD_LOG:
            self.open_console('log', keep=True)
        elif cid == CMD_CLOUD:
            self.open_console('cloud', keep=True)
        elif CMD_LINK <= cid < CMD_LINK + len(getattr(self, 'links', [])):
            url = self.links[cid - CMD_LINK]
            if url.startswith('https://github.com/'):   # only links the tray built itself
                os.startfile(url)
        elif cid == CMD_CLOUD_STRIP:
            self.prefs['cloud_strip'] = not self.prefs.get('cloud_strip', False)
            save_prefs(self.prefs)
            self.poll()
        elif cid == CMD_FREE:
            self.open_console('free', keep=True)
        elif cid == CMD_DEPS:
            self.open_console('deps', keep=True)
        elif cid == CMD_FOLDER:
            os.makedirs(hg.home(), exist_ok=True)
            os.startfile(hg.home())
        elif cid == CMD_STRIP:
            self.prefs['strip'] = not self.prefs.get('strip', True)
            save_prefs(self.prefs)
            self.poll()
        elif cid == CMD_STRIP_RESET:
            self.strip.reset_position()
        elif cid in (CMD_SHOW_STRIP, CMD_SHOW_BAR):
            self.prefs['display'] = 'taskbar' if cid == CMD_SHOW_BAR else 'strip'
            self.prefs.pop('taskbar', None)   # 1.12.0's on/off switch, replaced by this choice
            save_prefs(self.prefs)
            self.bar_start() if self.bar_on else self.bar_stop()
            self.poll()   # shows or hides the strip to match
        elif cid == CMD_BAR_ALL:
            self.prefs['taskbar_every_screen'] = not self.bar_every
            save_prefs(self.prefs)
            if self.bar:
                self.bar.set_every(self.bar_every)
        elif cid == CMD_STARTUP:
            hg.set_startup(not os.path.exists(hg.startup_shortcut()))
        elif cid == CMD_POWER:
            if hg.power_state():
                self.cancel_power()
            else:
                hg.set_power(True)
                log('shutdown when done: on')
                self.poll()
        elif cid == CMD_EXIT:
            self.quit('Exit in the menu')

    @staticmethod
    def open_console(*args, keep=False):
        py = console_python()
        cmd = (['cmd', '/k', py, GATE, *args] if keep else [py, GATE, *args])
        return subprocess.Popen(cmd, creationflags=CREATE_NEW_CONSOLE, cwd=HERE)

    def quit(self, why='quit'):
        if self.closed:
            return
        self.closed = True
        log(f'exit: {why}')
        if self.watch_open():
            self.watch.terminate()
        self.bar_stop()   # gives the taskbar buttons their width back
        if getattr(self, 'cloud_thread', None):
            self.cloud_thread.stop.set()
        self.tray.close()
        self.meters.close()
        self.root.destroy()

    def run(self):
        if hg.settings().get('cloud'):   # keeps cloud.json fresh; never in the selftest, which must not call GitHub
            self.cloud_thread = hc.Refresher(hg.cloud(), note=log)
            self.cloud_thread.start()
        self.poll()
        self.tray.update(self.state, force=True)
        self.bar_start()
        self.root.after(POLL_MS, self.loop)
        self.root.after(SAMPLE_MS, self.sample)
        self.root.mainloop()


def sample_states():
    """Made-up lanes for the selftest pictures: busy, short on RAM, idle."""
    def job(name, state, for_s, **kw):
        return {'name': name, 'state': state, 'for_s': for_s, 'since_queued_s': for_s, 'pid': 1, 'cwd': '',
                'command': '', 'expect_mb': 0, **kw}
    base = {'total_mb': 16280, 'ram_wait_min': 10}
    return {
        'busy': dict(base, free_mb=3890, low_ram=False, lanes=[
            {'lane': 'gpu', 'job': job('GameDemo full', 'running', 252),
             'queue': [job('GameDemo race check', 'queued', 40), job('MapDemo shot', 'queued', 12)]},
            {'lane': 'cpu', 'job': job('AppDemo 0.4.1 build', 'running', 65), 'queue': []}]),
        'stalled': dict(base, free_mb=7400, low_ram=False, lanes=[
            {'lane': 'gpu', 'job': None, 'queue': []},
            {'lane': 'cpu', 'job': job('AppDemo 1.7.1 build', 'running', 660, stalled_s=610),
             'queue': [job('Voice render', 'queued', 450)]}]),
        'ram': dict(base, free_mb=3280, low_ram=False, lanes=[
            {'lane': 'gpu', 'job': job('MapDemo hub shot', 'waiting for RAM', 400, need_mb=4268, free_mb=3280),
             'queue': []},
            {'lane': 'cpu', 'job': job('AppDemo 0.1.1 build', 'waiting for RAM', 70, need_mb=3268, free_mb=3280),
             'queue': []}]),
        'idle': dict(base, free_mb=6100, low_ram=False, lanes=[{'lane': 'gpu', 'job': None, 'queue': []},
                                                                {'lane': 'cpu', 'job': None, 'queue': []}]),
    }


def demo_history(now=None):
    """Two minutes of made-up readings for the selftest pictures: a CPU spike, a GPU dip, RAM creeping up."""
    import math
    now = time.time() if now is None else now
    rows = []
    for i in range(GRAPH_SECONDS):
        cpu = 38 + 10 * math.sin(i / 7) + (55 if 70 <= i <= 73 else 0)
        gpu = 72 + 8 * math.sin(i / 11) - (45 if 30 <= i <= 38 else 0)
        rows.append((now - GRAPH_SECONDS + i + 1, round(min(cpu, 100)), round(gpu), round(70 + i * 0.06)))
    return collections.deque(rows)


def selftest(out):
    """Add the icon and read the lanes once; check the strip fades in and out, steps behind and hides until idle;
    render it with made-up lanes and graphs and save what it draws; take the icon away again. The strip is never
    more than 1% opaque on screen during this (a fully transparent window never paints, so it can't be 0)."""
    os.makedirs(out, exist_ok=True)
    app = App()
    s = app.strip
    app.prefs = {}   # never the real saved position or switches
    s.prefs = app.prefs
    s.alpha_override = 0.01

    def pump(secs):
        end = time.time() + secs
        while time.time() < end:
            app.root.update()
            time.sleep(0.01)
    app.poll()
    app.tray.update(app.state, force=True)
    t = app.tray
    report = {'version': hg.VERSION, 'icon_size': t.size, 'added': t.added, 'tooltip': t.tip,
              'menu': menu_lines(app.state) if app.state else None, 'look': t.look,
              'strip_now': [''.join(x[0] for x in r) for r in strip_lines(app.state)] if app.state else None,
              'gpu_reading': app.meters.gpu(), 'rest_alpha': s.rest_alpha(), 'strip': {}}
    states, biggest, hist = sample_states(), [('msedgewebview2', 4100), ('node', 1020)], demo_history()
    b = states['busy']   # what estimate() would add from past runs
    b['lanes'][0]['job'].update(usual_s=4226, runs=4, left_s=3974)
    for q in b['lanes'][0]['queue']:
        q.update(usual_s=220, runs=5)
    b['lanes'][1]['job'].update(usual_s=367, runs=4, left_s=302)
    b['eta'] = {'done_in_s': 4414, 'done_at': '23:41', 'unknown': []}

    # fade in, hover, fade out: off-screen, reading the opacity the strip asked for
    s.animate, s.anchor = True, (-4000, -4000)
    s.show(states['idle'])
    pump(FADE_OUT_MS / 1000 + 0.3)
    s.show(states['busy'], biggest, hist=hist)
    pump(0.03)
    early = s.alpha
    pump(FADE_IN_MS / 1000 + 0.3)
    faded_in = s.alpha
    s.hover(True)
    pump(HOVER_MS / 1000 + 0.2)
    hovered = s.alpha
    s.hover(False)
    pump(HOVER_MS / 1000 + 0.2)
    s.show(states['idle'])
    pump(0.05)
    still_there = s.win.state() != 'withdrawn'   # fading, not gone at once
    pump(FADE_OUT_MS / 1000 + 0.3)
    report['fade'] = {'early': early, 'faded_in': faded_in, 'hovered': hovered, 'during_fade_out': still_there,
                      'after_fade_out': s.alpha, 'withdrawn': s.win.state() == 'withdrawn'}

    # behind other windows, and hidden until the lanes are idle
    s.animate = False
    s.show(states['busy'], biggest, hist=hist)
    top_before = bool(int(s.win.attributes('-topmost')))
    s.send_behind()
    behind = bool(int(s.win.attributes('-topmost')))
    s.show(states['idle'])
    s.show(states['busy'], biggest, hist=hist)
    after_idle = bool(int(s.win.attributes('-topmost')))   # a quiet spell brings it back on top
    s.dismiss()
    s.show(states['busy'], biggest, hist=hist)
    dismissed_stays_hidden = not s.shown and s.win.state() == 'withdrawn'
    s.show(states['idle'])
    s.show(states['busy'], biggest, hist=hist)
    report['z'] = {'topmost_at_start': top_before, 'topmost_when_behind': behind, 'topmost_after_idle': after_idle,
                   'dismissed_stays_hidden': dismissed_stays_hidden, 'shown_after_idle': s.shown}

    # shut down when done: in a throwaway state folder, with a stand-in command that only writes a file, so this can
    # never shut the PC down
    import tempfile
    saved_env = {k: os.environ.get(k) for k in ('HEAVYGATE_DIR', 'HEAVYGATE_SHUTDOWN_CMD')}
    sandbox = tempfile.mkdtemp(prefix='hg-power-')
    marker = os.path.join(sandbox, 'shutdown-ran.txt')
    os.environ['HEAVYGATE_DIR'] = sandbox
    os.environ['HEAVYGATE_SHUTDOWN_CMD'] = '|'.join([sys.executable, '-c', f"open(r'{marker}', 'w').write('ok')"])
    with open(os.path.join(sandbox, 'settings.json'), 'w') as fh:
        json.dump({'shutdown_idle_s': 10, 'shutdown_countdown_s': 5}, fh)
    p = {}
    s.anchor = s.saved_anchor()
    try:
        t0 = 1000.0
        hg.set_power(True)
        app.state, app.idle_since = dict(states['idle']), None
        app.power_tick(t0)
        p['idle'] = dict(app.power)
        app.state = dict(states['busy'])
        app.power_tick(t0 + 3)
        p['busy_resets'] = app.idle_since is None and app.power['phase'] == 'armed'
        app.state = dict(states['idle'])
        app.power_tick(t0 + 5)
        app.power_tick(t0 + 5 + 12)
        p['countdown'] = dict(app.power)
        st = dict(app.state, power=app.power)
        s.show(st, biggest, hist=hist)
        p['lines'] = [''.join(x[0] for x in r) for r in strip_lines(st, biggest)]
        p['shown'] = s.shown
        p['printed'], _, _, p['bright_px'], _ = s.capture(os.path.join(out, 'strip-shutdown.png'))
        s.grab = (0, 0, s.anchor, False)   # a click on the strip during the countdown
        s.release(None)
        p['cancelled'] = hg.power_state() is None and app.power is None
        hg.set_power(True)   # on again, and this time through to the end
        app.idle_since = None
        app.power_tick(t0 + 100)
        app.power_tick(t0 + 100 + 16)
        deadline = time.time() + 15
        while not os.path.exists(marker) and time.time() < deadline:
            time.sleep(0.1)
        p['ran'] = os.path.exists(marker)
        p['off_after'] = hg.power_state() is None
    finally:
        for k, v in saved_env.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
    report['power'] = p
    app.state = None

    # the pictures, at the strip's usual spot
    for name, st in states.items():
        s.show(st, biggest, hist=hist)
        entry = {'lines': [''.join(x[0] for x in r) for r in strip_lines(st, biggest)], 'shown': s.shown,
                 'withdrawn': s.win.state() == 'withdrawn'}
        if s.shown:
            entry['printed'], entry['w'], entry['h'], entry['bright_px'], entry['graph_px'] = s.capture(
                os.path.join(out, f'strip-{name}.png'))
        report['strip'][name] = entry
    app.prefs['graphs'] = False
    s.show(states['busy'], biggest, hist=hist)
    report['strip']['no-graphs'] = dict(zip(('printed', 'w', 'h', 'bright_px', 'graph_px'),
                                            s.capture(os.path.join(out, 'strip-no-graphs.png'))))
    # the taskbar readout's pixels, drawn here: the selftest never docks it into the real taskbar
    bp = heavybar.Painter(40, 96)
    bar = {'w': bp.w, 'h': bp.h, 'app_starts_it': app.bar is None,
           'default_is_strip': not app.bar_on and app.strip_on}
    app.prefs['display'] = 'taskbar'   # the other choice: the strip stays away, the readout takes over
    bar['taskbar_choice'] = app.bar_on and not app.strip_on
    s.show(states['idle'])
    s.show(states['busy'], biggest, app.strip_on, hist)
    bar['strip_hidden_with_taskbar'] = not s.shown
    app.prefs.pop('display')
    s.show(states['busy'], biggest, app.strip_on, hist)
    bar['strip_back_with_strip'] = s.shown
    sample = {'gpu': (41, 'running'), 'cpu': (63, 'ram'), 'ram': (72, 'free')}
    icon = render(bp.icon, ('running', True), ('ram', False), False)
    for th, bg in (('dark', (16, 16, 16)), ('light', (238, 238, 238))):
        px = bp.draw(sample, th, False, icon)
        hov = bp.draw(sample, th, True, icon)
        flat = heavybar.flatten(px, bg)
        write_png(os.path.join(out, f'taskbar-{th}.png'), bp.w, flat, bp.h)
        c = heavybar.THEMES[th]

        def exact(key):
            want = bytes((c[key][2], c[key][1], c[key][0]))
            return sum(1 for i in range(0, len(flat), 4) if flat[i:i + 3] == want)
        bar[th] = {'run_px': exact('run'), 'wait_px': exact('wait'), 'value_px': exact('value'),
                   'label_px': exact('label'), 'min_alpha': min(px[3::4]), 'hover_alpha': hov[3],
                   'rest_alpha': px[3]}
    bar['same_width_any_values'] = bp.w == len(bp.draw({'gpu': (100, 'stalled'), 'cpu': (None, 'free'),
                                                        'ram': (5, 'ram')}, 'dark')) // 4 // bp.h
    bp.close()
    report['taskbar'] = bar
    app.tray.remove()
    report['removed'] = not t.added
    app.quit()
    samples = {'idle': (('free', False), ('free', False), False),
               'gpu-running': (('running', False), ('free', False), False),
               'gpu-running-queued_cpu-ram': (('running', True), ('ram', False), False),
               'both-running-low-ram': (('running', True), ('running', False), True)}
    for name, look in samples.items():
        for size in (16, 64):
            write_png(os.path.join(out, f'{name}-{size}.png'), size, render(size, *look))
    with open(os.path.join(out, 'report.json'), 'w', encoding='utf-8') as f:
        json.dump(report, f, indent=2, ensure_ascii=False)
    print(json.dumps(report, indent=2))   # the console may be cp1252; report.json keeps the real text
    st, f, z = report['strip'], report['fade'], report['z']
    ok = (report['added'] and report['removed'] and st['idle']['withdrawn']
          and all(st[k]['shown'] and st[k].get('bright_px', 0) > 200 and st[k].get('graph_px', 0) > 40
                  for k in ('busy', 'ram'))
          and st['no-graphs']['graph_px'] < 5
          and f['early'] < f['faded_in'] and abs(f['faded_in'] - report['rest_alpha']) < 0.02
          and f['hovered'] == 1.0 and f['during_fade_out'] and f['withdrawn'] and f['after_fade_out'] == 0.0
          and z['topmost_at_start'] and not z['topmost_when_behind'] and z['topmost_after_idle']
          and z['dismissed_stays_hidden'] and z['shown_after_idle'])
    tb = report['taskbar']
    ok = ok and tb['app_starts_it'] and tb['same_width_any_values'] and tb['default_is_strip'] and tb[
        'taskbar_choice'] and tb['strip_hidden_with_taskbar'] and tb['strip_back_with_strip'] and all(
        tb[th]['run_px'] > 20 and tb[th]['wait_px'] > 20 and tb[th]['value_px'] > 20 and tb[th]['label_px'] > 20
        and tb[th]['min_alpha'] >= 1 and tb[th]['hover_alpha'] > tb[th]['rest_alpha'] for th in ('dark', 'light'))
    pw = report['power']
    ok = ok and (pw['idle']['phase'] == 'idle' and abs(pw['idle']['secs'] - 15) < 0.5 and pw['busy_resets']
                 and pw['countdown']['phase'] == 'countdown' and pw['shown'] and pw['bright_px'] > 100
                 and pw['cancelled'] and pw['ran'] and pw['off_after'])
    return 0 if ok else 1


def main(argv):
    if len(argv) >= 2 and argv[0] == '--selftest':
        return selftest(argv[1])
    mutex = k32.CreateMutexW(None, False, 'Local\\HeavyGateTray')
    if ctypes.get_last_error() == 183:   # ERROR_ALREADY_EXISTS: one tray is enough
        return 0
    log(f'start: HeavyGate {hg.VERSION} tray, {sys.executable}')
    try:   # the RAM guard runs beside the tray, so a PC with the tray up is a PC with a RAM ceiling
        if hg.ensure_guard():
            log('started the RAM guard')
    except Exception:
        log('could not start the RAM guard: ' + traceback.format_exc().strip().replace('\n', ' | '))
    try:
        App().run()
    except BaseException as e:
        log('stopped by an error: ' + traceback.format_exc().strip().replace('\n', ' | '))
        if not isinstance(e, (SystemExit, KeyboardInterrupt)):
            raise
    else:
        log('exit: main loop ended')
    del mutex
    return 0


if __name__ == '__main__':
    sys.exit(main(sys.argv[1:]))

