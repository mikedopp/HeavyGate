"""HeavyGate taskbar readout: GPU, CPU and RAM use docked in the Windows taskbar, just left of the notification area.

    GPU 41%  |  CPU 63%  |  RAM 72%

Each value takes its lane's colour: green while a job runs on that lane, amber while one waits for RAM, red when it has
stalled; RAM turns amber when it is nearly gone. The small icon in front is the tray icon (GPU half | CPU half).
Hover for the jobs and when they should be done; click either button for the tray menu.

How it docks: a small child window of the taskbar, made the way TrafficMonitor does it. The task-button area
(ReBarWindow32 on the main taskbar, WorkerW on the others) is narrowed by the readout's width, the readout sits in the
gap, and every half second it checks Explorer hasn't laid the buttons back over it. Quitting gives the width back.
It draws its own pixels with per-pixel alpha (UpdateLayeredWindow), so the text sits on the taskbar's own glass.

It runs on its own thread with its own message loop. A child window of Explorer's taskbar shares Explorer's input queue,
so if this thread stalled, the taskbar would too; the tray's thread (menus, status reads, nvidia-smi) never touches
the readout's windows except through PostThreadMessage.

Windows 10's taskbar only. On a taskbar it can't find its parts in (Windows 11's XAML taskbar, a vertical one) it stays
away and says so in tray.log. Python standard library + ctypes.
"""
import ctypes
import threading
from ctypes import wintypes

u32 = ctypes.WinDLL('user32', use_last_error=True)
g32 = ctypes.WinDLL('gdi32')
k32 = ctypes.WinDLL('kernel32', use_last_error=True)
cc32 = ctypes.WinDLL('comctl32')

LRESULT = ctypes.c_ssize_t
WNDPROC = ctypes.WINFUNCTYPE(LRESULT, wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM)
WM_DESTROY, WM_TIMER, WM_QUIT, WM_APP = 0x0002, 0x0113, 0x0012, 0x8000
WM_MOUSEMOVE, WM_LBUTTONUP, WM_RBUTTONUP, WM_MOUSELEAVE = 0x0200, 0x0202, 0x0205, 0x02A3
WM_PAINT, WM_ERASEBKGND = 0x000F, 0x0014
WM_BAR_REFRESH, WM_BAR_SETTINGS = WM_APP + 20, WM_APP + 21
WS_CHILD, WS_VISIBLE, WS_CLIPSIBLINGS, WS_POPUP = 0x40000000, 0x10000000, 0x04000000, 0x80000000
WS_EX_LAYERED, WS_EX_TOPMOST, WS_EX_TOOLWINDOW, WS_EX_NOACTIVATE = 0x80000, 0x8, 0x80, 0x08000000
SWP_NOSIZE, SWP_NOMOVE, SWP_NOZORDER, SWP_NOACTIVATE = 0x1, 0x2, 0x4, 0x10
HWND_TOP = wintypes.HWND(0)
TICK_MS = 500
FONT_PT = 9   # the taskbar clock's size


class WNDCLASSEXW(ctypes.Structure):
    _fields_ = [('cbSize', wintypes.UINT), ('style', wintypes.UINT), ('lpfnWndProc', WNDPROC),
                ('cbClsExtra', ctypes.c_int), ('cbWndExtra', ctypes.c_int), ('hInstance', wintypes.HINSTANCE),
                ('hIcon', wintypes.HICON), ('hCursor', wintypes.HANDLE), ('hbrBackground', wintypes.HBRUSH),
                ('lpszMenuName', wintypes.LPCWSTR), ('lpszClassName', wintypes.LPCWSTR), ('hIconSm', wintypes.HICON)]


class BITMAPINFOHEADER(ctypes.Structure):
    _fields_ = [('biSize', wintypes.DWORD), ('biWidth', ctypes.c_long), ('biHeight', ctypes.c_long),
                ('biPlanes', wintypes.WORD), ('biBitCount', wintypes.WORD), ('biCompression', wintypes.DWORD),
                ('biSizeImage', wintypes.DWORD), ('biXPelsPerMeter', ctypes.c_long),
                ('biYPelsPerMeter', ctypes.c_long), ('biClrUsed', wintypes.DWORD), ('biClrImportant', wintypes.DWORD)]


class BLENDFUNCTION(ctypes.Structure):
    _fields_ = [('BlendOp', ctypes.c_ubyte), ('BlendFlags', ctypes.c_ubyte), ('SourceConstantAlpha', ctypes.c_ubyte),
                ('AlphaFormat', ctypes.c_ubyte)]


class SIZE(ctypes.Structure):
    _fields_ = [('cx', ctypes.c_long), ('cy', ctypes.c_long)]


class TRACKMOUSEEVENT(ctypes.Structure):
    _fields_ = [('cbSize', wintypes.DWORD), ('dwFlags', wintypes.DWORD), ('hwndTrack', wintypes.HWND),
                ('dwHoverTime', wintypes.DWORD)]


class TOOLINFOW(ctypes.Structure):
    _fields_ = [('cbSize', wintypes.UINT), ('uFlags', wintypes.UINT), ('hwnd', wintypes.HWND),
                ('uId', ctypes.c_size_t), ('rect', wintypes.RECT), ('hinst', wintypes.HINSTANCE),
                ('lpszText', wintypes.LPWSTR), ('lParam', wintypes.LPARAM), ('lpReserved', ctypes.c_void_p)]


class PAINTSTRUCT(ctypes.Structure):
    _fields_ = [('hdc', wintypes.HDC), ('fErase', wintypes.BOOL), ('rcPaint', wintypes.RECT),
                ('fRestore', wintypes.BOOL), ('fIncUpdate', wintypes.BOOL), ('rgbReserved', ctypes.c_byte * 32)]


class INITCOMMONCONTROLSEX(ctypes.Structure):
    _fields_ = [('dwSize', wintypes.DWORD), ('dwICC', wintypes.DWORD)]


class HIGHCONTRASTW(ctypes.Structure):
    _fields_ = [('cbSize', wintypes.UINT), ('dwFlags', wintypes.DWORD), ('lpszDefaultScheme', wintypes.LPWSTR)]


u32.DefWindowProcW.argtypes = [wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM]
u32.DefWindowProcW.restype = LRESULT
u32.RegisterClassExW.argtypes = [ctypes.POINTER(WNDCLASSEXW)]
u32.CreateWindowExW.argtypes = [wintypes.DWORD, wintypes.LPCWSTR, wintypes.LPCWSTR, wintypes.DWORD, ctypes.c_int,
                                ctypes.c_int, ctypes.c_int, ctypes.c_int, wintypes.HWND, wintypes.HMENU,
                                wintypes.HINSTANCE, wintypes.LPVOID]
u32.CreateWindowExW.restype = wintypes.HWND
u32.DestroyWindow.argtypes = [wintypes.HWND]
u32.IsWindow.argtypes = [wintypes.HWND]
u32.GetParent.argtypes = [wintypes.HWND]
u32.GetParent.restype = wintypes.HWND
u32.FindWindowW.argtypes = [wintypes.LPCWSTR, wintypes.LPCWSTR]
u32.FindWindowW.restype = wintypes.HWND
u32.FindWindowExW.argtypes = [wintypes.HWND, wintypes.HWND, wintypes.LPCWSTR, wintypes.LPCWSTR]
u32.FindWindowExW.restype = wintypes.HWND
u32.GetWindowRect.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.RECT)]
u32.GetClientRect.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.RECT)]
u32.MapWindowPoints.argtypes = [wintypes.HWND, wintypes.HWND, ctypes.c_void_p, wintypes.UINT]
u32.SetWindowPos.argtypes = [wintypes.HWND, wintypes.HWND, ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int,
                             wintypes.UINT]
u32.SetTimer.argtypes = [wintypes.HWND, ctypes.c_size_t, wintypes.UINT, ctypes.c_void_p]
u32.SetTimer.restype = ctypes.c_size_t
u32.KillTimer.argtypes = [wintypes.HWND, ctypes.c_size_t]
u32.GetMessageW.argtypes = [ctypes.POINTER(wintypes.MSG), wintypes.HWND, wintypes.UINT, wintypes.UINT]
u32.TranslateMessage.argtypes = [ctypes.POINTER(wintypes.MSG)]
u32.DispatchMessageW.argtypes = [ctypes.POINTER(wintypes.MSG)]
u32.DispatchMessageW.restype = LRESULT
u32.PostThreadMessageW.argtypes = [wintypes.DWORD, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM]
u32.PostMessageW.argtypes = [wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM]
u32.SendMessageW.argtypes = [wintypes.HWND, wintypes.UINT, wintypes.WPARAM, ctypes.c_void_p]
u32.SendMessageW.restype = LRESULT
u32.GetDC.argtypes = [wintypes.HWND]
u32.GetDC.restype = wintypes.HDC
u32.ReleaseDC.argtypes = [wintypes.HWND, wintypes.HDC]
u32.UpdateLayeredWindow.argtypes = [wintypes.HWND, wintypes.HDC, ctypes.c_void_p, ctypes.POINTER(SIZE), wintypes.HDC,
                                    ctypes.POINTER(wintypes.POINT), wintypes.DWORD, ctypes.POINTER(BLENDFUNCTION),
                                    wintypes.DWORD]
u32.TrackMouseEvent.argtypes = [ctypes.POINTER(TRACKMOUSEEVENT)]
u32.BeginPaint.argtypes = [wintypes.HWND, ctypes.POINTER(PAINTSTRUCT)]
u32.BeginPaint.restype = wintypes.HDC
u32.EndPaint.argtypes = [wintypes.HWND, ctypes.POINTER(PAINTSTRUCT)]
u32.GetWindowLongPtrW.argtypes = [wintypes.HWND, ctypes.c_int]
u32.GetWindowLongPtrW.restype = ctypes.c_ssize_t
u32.SetWindowLongPtrW.argtypes = [wintypes.HWND, ctypes.c_int, ctypes.c_ssize_t]
u32.SetWindowLongPtrW.restype = ctypes.c_ssize_t
u32.InvalidateRect.argtypes = [wintypes.HWND, ctypes.c_void_p, wintypes.BOOL]
u32.EnumWindows.argtypes = [ctypes.c_void_p, wintypes.LPARAM]
u32.GetClassNameW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
u32.SystemParametersInfoW.argtypes = [wintypes.UINT, wintypes.UINT, ctypes.c_void_p, wintypes.UINT]
u32.GetSysColor.argtypes = [ctypes.c_int]
u32.GetSysColor.restype = wintypes.DWORD
g32.CreateCompatibleDC.argtypes = [wintypes.HDC]
g32.CreateCompatibleDC.restype = wintypes.HDC
g32.DeleteDC.argtypes = [wintypes.HDC]
g32.SelectObject.argtypes = [wintypes.HDC, wintypes.HANDLE]
g32.SelectObject.restype = wintypes.HANDLE
g32.DeleteObject.argtypes = [wintypes.HANDLE]
g32.CreateDIBSection.argtypes = [wintypes.HDC, ctypes.c_void_p, wintypes.UINT, ctypes.POINTER(ctypes.c_void_p),
                                 wintypes.HANDLE, wintypes.DWORD]
g32.CreateDIBSection.restype = wintypes.HBITMAP
g32.CreateFontW.argtypes = [ctypes.c_int] * 5 + [wintypes.DWORD] * 8 + [wintypes.LPCWSTR]
g32.CreateFontW.restype = wintypes.HANDLE
g32.SetTextColor.argtypes = [wintypes.HDC, wintypes.DWORD]
g32.SetBkMode.argtypes = [wintypes.HDC, ctypes.c_int]
g32.TextOutW.argtypes = [wintypes.HDC, ctypes.c_int, ctypes.c_int, wintypes.LPCWSTR, ctypes.c_int]
g32.GetTextExtentPoint32W.argtypes = [wintypes.HDC, wintypes.LPCWSTR, ctypes.c_int, ctypes.POINTER(SIZE)]
g32.GetDeviceCaps.argtypes = [wintypes.HDC, ctypes.c_int]
g32.StretchDIBits.argtypes = [wintypes.HDC] + [ctypes.c_int] * 8 + [ctypes.c_void_p, ctypes.c_void_p, wintypes.UINT,
                                                                     wintypes.DWORD]
u32.LoadCursorW.argtypes = [wintypes.HINSTANCE, ctypes.c_void_p]
u32.LoadCursorW.restype = wintypes.HANDLE
k32.GetModuleHandleW.restype = wintypes.HMODULE
k32.GetCurrentThreadId.restype = wintypes.DWORD

# colours as (r, g, b). 'dark' is the usual Windows 10 taskbar; 'light' is the taskbar with the light Windows theme
THEMES = {
    'dark': {'label': (178, 184, 194), 'value': (243, 245, 248), 'run': (64, 214, 102), 'wait': (255, 176, 64),
             'stop': (255, 104, 104), 'rule': (255, 255, 255), 'hover': (255, 255, 255), 'plate': (32, 32, 32)},
    'light': {'label': (78, 84, 94), 'value': (18, 20, 24), 'run': (0, 132, 48), 'wait': (178, 102, 0),
              'stop': (196, 36, 36), 'rule': (0, 0, 0), 'hover': (0, 0, 0), 'plate': (226, 226, 226)},
}
STATE_COLOUR = {'free': 'value', 'running': 'run', 'ram': 'wait', 'stalled': 'stop'}


def taskbar_theme():
    """'hc' with a high-contrast theme on, else 'light' when Windows draws a light taskbar, else 'dark'."""
    hc = HIGHCONTRASTW(cbSize=ctypes.sizeof(HIGHCONTRASTW))
    if u32.SystemParametersInfoW(0x42, hc.cbSize, ctypes.byref(hc), 0) and hc.dwFlags & 1:
        return 'hc'
    try:
        import winreg
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER,
                            r'Software\Microsoft\Windows\CurrentVersion\Themes\Personalize') as k:
            return 'light' if winreg.QueryValueEx(k, 'SystemUsesLightTheme')[0] == 1 else 'dark'
    except OSError:
        return 'dark'


def hc_colours():
    """High contrast: every colour from the system's own button text and face, drawn solid."""
    def rgb(i):
        c = u32.GetSysColor(i)
        return (c & 0xff, (c >> 8) & 0xff, (c >> 16) & 0xff)
    text, face = rgb(18), rgb(15)   # COLOR_BTNTEXT, COLOR_BTNFACE
    return {'label': text, 'value': text, 'run': text, 'wait': text, 'stop': text, 'rule': text, 'hover': text,
            'plate': face}


# drawing ------------------------------------------------------------------------------------------------------------
class Painter:
    """Lays the readout out once per DPI and draws it to premultiplied BGRA. GDI draws the text white on black into a
    32-bit DIB with greyscale anti-aliasing; the grey becomes each pixel's alpha, and the run's colour is laid on with
    it. That gives clean edges on any taskbar colour, glass included, which a colour key can't."""
    def __init__(self, height, dpi=96):
        self.h, self.dpi = height, dpi
        px = -(FONT_PT * dpi + 36) // 72
        self.f_label = g32.CreateFontW(px, 0, 0, 0, 400, 0, 0, 0, 1, 0, 0, 4, 0, 'Segoe UI')   # ANTIALIASED_QUALITY
        self.f_value = g32.CreateFontW(px, 0, 0, 0, 600, 0, 0, 0, 1, 0, 0, 4, 0, 'Segoe UI')
        self.icon = u32.GetSystemMetrics(49) or 16   # the tray's small-icon size
        self.pad, self.gap, self.sep = self.s(8), self.s(6), self.s(15)
        screen = u32.GetDC(None)
        dc = g32.CreateCompatibleDC(screen)
        u32.ReleaseDC(None, screen)

        def ext(font, text):
            g32.SelectObject(dc, font)
            sz = SIZE()
            g32.GetTextExtentPoint32W(dc, text, len(text), ctypes.byref(sz))
            return sz.cx, sz.cy
        self.label_w = {k: ext(self.f_label, k + ' ')[0] for k in ('GPU', 'CPU', 'RAM')}
        self.value_w, self.text_h = ext(self.f_value, '100%')
        g32.DeleteDC(dc)
        self.w = (self.pad + self.icon + self.gap + sum(self.label_w.values()) + 3 * self.value_w + 2 * self.sep
                  + self.pad)

    def s(self, n):
        return max(1, round(n * self.dpi / 96))

    def close(self):
        for f in (self.f_label, self.f_value):
            g32.DeleteObject(f)

    def layout(self, data):
        """[(x, font, text, colour key)] for the text, [x] for the thin rules between groups, and the icon's x."""
        runs, rules = [], []
        x = self.pad + self.icon + self.gap
        for i, key in enumerate(('gpu', 'cpu', 'ram')):
            label = key.upper()
            pct, state = data.get(key, (None, 'free'))
            runs.append((x, self.f_label, label, 'label'))
            x += self.label_w[label]
            text = f'{pct}%' if pct is not None else 'n/a'
            runs.append((x, self.f_value, text, STATE_COLOUR.get(state, 'value')))
            x += self.value_w
            if i < 2:
                rules.append(x + self.sep // 2)
                x += self.sep
        return runs, rules, self.pad

    def draw(self, data, theme='dark', hovered=False, icon_bgra=None):
        """Premultiplied top-down BGRA bytes, self.w by self.h. With theme 'hc' the plate is solid."""
        w, h = self.w, self.h
        c = hc_colours() if theme == 'hc' else THEMES.get(theme, THEMES['dark'])
        runs, rules, icon_x = self.layout(data)
        screen = u32.GetDC(None)
        dc = g32.CreateCompatibleDC(screen)
        u32.ReleaseDC(None, screen)
        bmi = BITMAPINFOHEADER(biSize=ctypes.sizeof(BITMAPINFOHEADER), biWidth=w, biHeight=-h, biPlanes=1,
                               biBitCount=32)
        bits = ctypes.c_void_p()
        bmp = g32.CreateDIBSection(dc, ctypes.byref(bmi), 0, ctypes.byref(bits), None, 0)
        old = g32.SelectObject(dc, bmp)
        g32.SetBkMode(dc, 1)   # TRANSPARENT
        g32.SetTextColor(dc, 0xFFFFFF)
        y = (h - self.text_h) // 2
        for x, font, text, _ in runs:
            g32.SelectObject(dc, font)
            g32.TextOutW(dc, x, y, text, len(text))
        mask = ctypes.string_at(bits, w * h * 4)
        g32.SelectObject(dc, old)
        g32.DeleteObject(bmp)
        g32.DeleteDC(dc)

        # which colour each column of text takes
        col = [None] * w
        for i, (x, _, _, key) in enumerate(runs):
            end = runs[i + 1][0] if i + 1 < len(runs) else w
            for xx in range(max(x, 0), min(end, w)):
                col[xx] = c[key]
        # the plate under it all: alpha 1 keeps the whole rectangle clickable; a soft lift while the mouse is over it
        if theme == 'hc':
            pr, pg, pb = c['plate']
            plate = (pb, pg, pr, 255)
        else:
            a = 30 if hovered else 1
            hr, hg_, hb = c['hover']
            plate = (hb * a // 255, hg_ * a // 255, hr * a // 255, a)
        out = bytearray(bytes(plate) * (w * h))
        pb_, pg_, pr_, pa = plate
        for i in range(0, w * h * 4, 4):
            a = mask[i + 2]   # greyscale text: r = g = b
            if a:
                r, g, b = col[(i // 4) % w] or c['value']
                inv = 255 - a
                out[i] = (b * a + pb_ * inv) // 255
                out[i + 1] = (g * a + pg_ * inv) // 255
                out[i + 2] = (r * a + pr_ * inv) // 255
                out[i + 3] = (255 * a + pa * inv) // 255
        # thin rules between the groups, 40% of the height
        rr, rg, rb = c['rule']
        ra = 255 if theme == 'hc' else 70
        top, bot = h * 3 // 10, h - h * 3 // 10
        for x in rules:
            for yy in range(top, bot):
                i = (yy * w + x) * 4
                out[i:i + 4] = bytes((rb * ra // 255, rg * ra // 255, rr * ra // 255, ra))
        # the lane icon, straight alpha in, premultiplied over the plate
        if icon_bgra:
            n = self.icon
            iy = (h - n) // 2
            for yy in range(n):
                for xx in range(n):
                    j = (yy * n + xx) * 4
                    b, g, r, a = icon_bgra[j:j + 4]
                    if not a:
                        continue
                    i = ((iy + yy) * w + icon_x + xx) * 4
                    inv = 255 - a
                    out[i] = (b * a + out[i] * inv) // 255
                    out[i + 1] = (g * a + out[i + 1] * inv) // 255
                    out[i + 2] = (r * a + out[i + 2] * inv) // 255
                    out[i + 3] = (255 * a + out[i + 3] * inv) // 255
        return bytes(out)


def flatten(bgra, rgb):
    """Premultiplied BGRA over a solid colour, as opaque BGRA: what it looks like on a plain taskbar."""
    out = bytearray(len(bgra))
    r0, g0, b0 = rgb
    for i in range(0, len(bgra), 4):
        inv = 255 - bgra[i + 3]
        out[i] = bgra[i] + b0 * inv // 255
        out[i + 1] = bgra[i + 1] + g0 * inv // 255
        out[i + 2] = bgra[i + 2] + r0 * inv // 255
        out[i + 3] = 255
    return bytes(out)


# finding the taskbars ------------------------------------------------------------------------------------------------
def rect_of(hwnd, parent=None):
    r = wintypes.RECT()
    u32.GetWindowRect(hwnd, ctypes.byref(r))
    if parent:
        u32.MapWindowPoints(None, parent, ctypes.byref(r), 2)
    return r


def taskbars(every=True):
    """The main taskbar first, then (with every) each other screen's."""
    out = []
    main = u32.FindWindowW('Shell_TrayWnd', None)
    if main:
        out.append(main)
    if every:
        found = []
        cls = ctypes.create_unicode_buffer(64)

        @ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
        def each(h, _):
            u32.GetClassNameW(h, cls, 64)
            if cls.value == 'Shell_SecondaryTrayWnd':
                found.append(h)
            return True
        u32.EnumWindows(each, 0)
        out += found
    return out


def parts(tb):
    """(task area, the window to its right or None, why not) for one taskbar. The main taskbar's buttons live in
    ReBarWindow32 with TrayNotifyWnd to its right; another screen's live in WorkerW with ClockButton (if shown)."""
    tbr = rect_of(tb)
    if tbr.bottom - tbr.top > tbr.right - tbr.left:
        return None, None, 'vertical taskbar'
    task = u32.FindWindowExW(tb, None, 'ReBarWindow32', None)
    stop = u32.FindWindowExW(tb, None, 'TrayNotifyWnd', None) if task else None
    if not task:
        task = u32.FindWindowExW(tb, None, 'WorkerW', None)
        stop = u32.FindWindowExW(tb, None, 'ClockButton', None) if task else None
    if not task:
        return None, None, 'no task-button area found (not the Windows 10 taskbar)'
    return task, stop, None


class Dock:
    """One readout window in one taskbar."""
    def __init__(self, tb, hwnd, task, stop):
        self.tb, self.hwnd, self.task, self.stop = tb, hwnd, task, stop
        self.x = self.y = self.h = None
        self.layered = True
        self.solid = None   # opaque pixels, only when per-pixel alpha was refused
        self.hovered = False


class Readout(threading.Thread):
    """The taskbar readout's own thread. The tray calls set() with what to show and stop() on exit; clicks go to
    notify_hwnd as notify_msg (posted, never sent, so the tray's thread can be busy)."""
    def __init__(self, notify_hwnd, notify_msg, every=True, log=print):
        super().__init__(name='HeavyGate taskbar readout', daemon=True)
        self.notify_hwnd, self.notify_msg, self.every, self.log = notify_hwnd, notify_msg, every, log
        self.data = {'gpu': (None, 'free'), 'cpu': (None, 'free'), 'ram': (None, 'free'), 'tip': 'HeavyGate',
                     'icon': None}
        self.docks, self.painter, self.theme, self.tid = {}, None, None, None
        self.ready = threading.Event()
        self.said = set()   # reasons already logged, so a taskbar it can't use is logged once
        self.tip_hwnd, self.tip_text = None, None
        self.ticks = 0
        self.frames = 0   # pictures pushed to the screen (the live check reads it)

    # called from the tray's thread -----------------------------------------------------------------------------------
    def set(self, data):
        self.data = data   # one reference swap: the readout thread reads whole dicts only
        if self.tid:
            u32.PostThreadMessageW(self.tid, WM_BAR_REFRESH, 0, 0)

    def set_every(self, every):
        self.every = every
        if self.tid:
            u32.PostThreadMessageW(self.tid, WM_BAR_SETTINGS, 0, 0)

    def stop(self, timeout=3):
        if self.tid:
            u32.PostThreadMessageW(self.tid, WM_QUIT, 0, 0)
        self.join(timeout)

    def where(self):
        """[(taskbar, readout rect, task area rect, rect of the window to its right)] in screen pixels, for checks."""
        out = []
        for d in list(self.docks.values()):
            if u32.IsWindow(d.hwnd):
                r, t = rect_of(d.hwnd), rect_of(d.task)
                s = rect_of(d.stop) if d.stop else None
                out.append({'taskbar': d.tb, 'readout': (r.left, r.top, r.right, r.bottom),
                            'task': (t.left, t.top, t.right, t.bottom),
                            'next': (s.left, s.top, s.right, s.bottom) if s else None, 'layered': d.layered})
        return out

    # the readout's own thread ----------------------------------------------------------------------------------------
    def run(self):
        self.tid = k32.GetCurrentThreadId()
        self.hinst = k32.GetModuleHandleW(None)
        self.proc = WNDPROC(self.wndproc)
        wc = WNDCLASSEXW(cbSize=ctypes.sizeof(WNDCLASSEXW), lpfnWndProc=self.proc, hInstance=self.hinst,
                         lpszClassName='HeavyGateTaskbarReadout', hCursor=u32.LoadCursorW(None, ctypes.c_void_p(32512)))
        u32.RegisterClassExW(ctypes.byref(wc))
        cc32.InitCommonControlsEx(ctypes.byref(INITCOMMONCONTROLSEX(ctypes.sizeof(INITCOMMONCONTROLSEX), 0x8)))
        timer = u32.SetTimer(None, 0, TICK_MS, None)
        msg = wintypes.MSG()
        self.ready.set()
        try:
            self.tick()
            while u32.GetMessageW(ctypes.byref(msg), None, 0, 0) > 0:
                try:
                    if msg.message == WM_TIMER and not msg.hWnd:
                        self.tick()
                    elif msg.message == WM_BAR_REFRESH:
                        self.paint_all()
                    elif msg.message == WM_BAR_SETTINGS:
                        self.tick()
                    else:
                        u32.TranslateMessage(ctypes.byref(msg))
                        u32.DispatchMessageW(ctypes.byref(msg))
                except Exception as e:   # one bad tick must not end the readout
                    self.log(f'taskbar readout: {e!r}')
        finally:
            u32.KillTimer(None, timer)
            self.undock_all()
            if self.tip_hwnd:
                u32.DestroyWindow(self.tip_hwnd)
            if self.painter:
                self.painter.close()

    def say(self, why):
        if why not in self.said:
            self.said.add(why)
            self.log(f'taskbar readout: {why}')

    def tick(self):
        """Every half second: find the taskbars (Explorer may have restarted), dock where missing, and keep the task
        buttons out from under the readout."""
        self.ticks += 1
        if self.ticks % 10 == 1:
            theme = taskbar_theme()
            if theme != self.theme:
                self.theme = theme
                self.paint_all()
        want = taskbars(self.every)
        for tb in list(self.docks):
            if tb not in want or not u32.IsWindow(self.docks[tb].hwnd):
                self.undock(self.docks.pop(tb))
        for tb in want:
            if tb not in self.docks:
                self.dock(tb)
            if tb in self.docks:
                self.fit(self.docks[tb])

    def dock(self, tb):
        task, stop, why = parts(tb)
        if why:
            self.say(why)
            return
        h = rect_of(tb).bottom - rect_of(tb).top
        if not self.painter or self.painter.h != h:
            if self.painter:
                self.painter.close()
            screen = u32.GetDC(None)
            dpi = g32.GetDeviceCaps(screen, 90) or 96   # LOGPIXELSY
            u32.ReleaseDC(None, screen)
            self.painter = Painter(h, dpi)
        p = self.painter
        hwnd = u32.CreateWindowExW(WS_EX_LAYERED, 'HeavyGateTaskbarReadout', 'HeavyGate',
                                   WS_CHILD | WS_VISIBLE | WS_CLIPSIBLINGS, 0, 0, p.w, h, tb, None, self.hinst, None)
        if not hwnd:
            self.say(f'could not create the readout window (error {ctypes.get_last_error()})')
            return
        d = Dock(tb, hwnd, task, stop)
        self.docks[tb] = d
        self.add_tip(hwnd)
        self.fit(d)
        self.paint(d)
        self.log(f'taskbar readout: docked in taskbar {tb:#x}, {p.w}x{h} px'
                 + ('' if d.layered else ' (solid: per-pixel alpha refused)'))

    def fit(self, d):
        """Narrow the task area so it ends where the readout starts, and keep the readout there and on top."""
        p = self.painter
        tbc = wintypes.RECT()
        u32.GetClientRect(d.tb, ctypes.byref(tbc))
        right = rect_of(d.stop, d.tb).left if d.stop and u32.IsWindow(d.stop) else tbc.right
        t = rect_of(d.task, d.tb)
        x, y, h = right - p.w, t.top, t.bottom - t.top
        if t.right > x:   # Explorer laid the buttons out to the full width again
            u32.SetWindowPos(d.task, None, 0, 0, max(x - t.left, 0), h, SWP_NOMOVE | SWP_NOZORDER | SWP_NOACTIVATE)
        if (x, y, h) != (d.x, d.y, d.h):
            d.x, d.y, d.h = x, y, h
            u32.SetWindowPos(d.hwnd, HWND_TOP, x, y, p.w, h, SWP_NOACTIVATE)
            self.paint(d)

    def undock(self, d):
        """Give the task area its width back and remove the readout."""
        try:
            if u32.IsWindow(d.task) and u32.IsWindow(d.tb):
                tbc = wintypes.RECT()
                u32.GetClientRect(d.tb, ctypes.byref(tbc))
                right = rect_of(d.stop, d.tb).left if d.stop and u32.IsWindow(d.stop) else tbc.right
                t = rect_of(d.task, d.tb)
                u32.SetWindowPos(d.task, None, 0, 0, right - t.left, t.bottom - t.top,
                                 SWP_NOMOVE | SWP_NOZORDER | SWP_NOACTIVATE)
        finally:
            if u32.IsWindow(d.hwnd):
                u32.DestroyWindow(d.hwnd)

    def undock_all(self):
        for tb in list(self.docks):
            self.undock(self.docks.pop(tb))

    def paint_all(self):
        for d in list(self.docks.values()):
            self.paint(d)
        self.update_tip()

    def paint(self, d):
        if not self.painter or d.h is None or not u32.IsWindow(d.hwnd):
            return
        data = self.data
        p = self.painter
        theme = self.theme or 'dark'
        px = p.draw(data, theme, d.hovered, data.get('icon'))
        if d.layered and self.push(d.hwnd, p.w, d.h, px):
            self.frames += 1
            return
        if d.layered:   # per-pixel alpha refused: draw it solid on the taskbar's usual colour from here on
            d.layered = False
            ex = u32.GetWindowLongPtrW(d.hwnd, -20)
            u32.SetWindowLongPtrW(d.hwnd, -20, ex & ~WS_EX_LAYERED)
            self.say('per-pixel alpha refused; drawing the readout solid')
        c = hc_colours() if theme == 'hc' else THEMES.get(theme, THEMES['dark'])
        d.solid = flatten(px, c['plate'])
        u32.InvalidateRect(d.hwnd, None, False)
        self.frames += 1

    @staticmethod
    def push(hwnd, w, h, px):
        screen = u32.GetDC(None)
        dc = g32.CreateCompatibleDC(screen)
        bmi = BITMAPINFOHEADER(biSize=ctypes.sizeof(BITMAPINFOHEADER), biWidth=w, biHeight=-h, biPlanes=1,
                               biBitCount=32)
        bits = ctypes.c_void_p()
        bmp = g32.CreateDIBSection(dc, ctypes.byref(bmi), 0, ctypes.byref(bits), None, 0)
        ctypes.memmove(bits, px, len(px))
        old = g32.SelectObject(dc, bmp)
        ok = u32.UpdateLayeredWindow(hwnd, screen, None, ctypes.byref(SIZE(w, h)), dc,
                                     ctypes.byref(wintypes.POINT(0, 0)), 0,
                                     ctypes.byref(BLENDFUNCTION(0, 0, 255, 1)), 2)   # AC_SRC_ALPHA, ULW_ALPHA
        g32.SelectObject(dc, old)
        g32.DeleteObject(bmp)
        g32.DeleteDC(dc)
        u32.ReleaseDC(None, screen)
        return bool(ok)

    # the hover text ---------------------------------------------------------------------------------------------------
    def add_tip(self, hwnd):
        if not self.tip_hwnd:
            self.tip_hwnd = u32.CreateWindowExW(WS_EX_TOPMOST, 'tooltips_class32', None, WS_POPUP | 0x1 | 0x2,
                                                0, 0, 0, 0, None, None, self.hinst, None)   # TTS_ALWAYSTIP|NOPREFIX
            if self.tip_hwnd:
                u32.SendMessageW(self.tip_hwnd, 0x0418, 0, ctypes.c_void_p(480))   # TTM_SETMAXTIPWIDTH: lines
        if self.tip_hwnd:
            self.tip_text = ctypes.create_unicode_buffer(self.data.get('tip') or 'HeavyGate', 512)
            ti = TOOLINFOW(cbSize=ctypes.sizeof(TOOLINFOW), uFlags=0x1 | 0x10, hwnd=hwnd, uId=hwnd,
                           lpszText=ctypes.cast(self.tip_text, wintypes.LPWSTR))   # TTF_IDISHWND | TTF_SUBCLASS
            u32.SendMessageW(self.tip_hwnd, 0x0432, 0, ctypes.addressof(ti))   # TTM_ADDTOOLW

    def update_tip(self):
        if not self.tip_hwnd:
            return
        text = self.data.get('tip') or 'HeavyGate'
        if self.tip_text is not None and self.tip_text.value == text:
            return
        self.tip_text = ctypes.create_unicode_buffer(text, max(512, len(text) + 1))
        for d in self.docks.values():
            ti = TOOLINFOW(cbSize=ctypes.sizeof(TOOLINFOW), uFlags=0x1 | 0x10, hwnd=d.hwnd, uId=d.hwnd,
                           lpszText=ctypes.cast(self.tip_text, wintypes.LPWSTR))
            u32.SendMessageW(self.tip_hwnd, 0x0439, 0, ctypes.addressof(ti))   # TTM_UPDATETIPTEXTW

    def dock_for(self, hwnd):
        for d in self.docks.values():
            if d.hwnd == hwnd:
                return d
        return None

    def wndproc(self, hwnd, msg, wp, lp):
        try:
            if msg == WM_MOUSEMOVE:
                d = self.dock_for(hwnd)
                if d and not d.hovered:
                    d.hovered = True
                    u32.TrackMouseEvent(ctypes.byref(TRACKMOUSEEVENT(ctypes.sizeof(TRACKMOUSEEVENT), 0x2, hwnd, 0)))
                    self.paint(d)
                return 0
            if msg == WM_MOUSELEAVE:
                d = self.dock_for(hwnd)
                if d:
                    d.hovered = False
                    self.paint(d)
                return 0
            if msg in (WM_LBUTTONUP, WM_RBUTTONUP):
                if self.notify_hwnd:
                    u32.PostMessageW(self.notify_hwnd, self.notify_msg, 0, 0)
                return 0
            if msg == WM_ERASEBKGND:
                return 1
            if msg == WM_PAINT:   # only when per-pixel alpha was refused
                ps = PAINTSTRUCT()
                hdc = u32.BeginPaint(hwnd, ctypes.byref(ps))
                d = self.dock_for(hwnd)
                if d and d.solid and self.painter:
                    w, h = self.painter.w, d.h
                    bmi = BITMAPINFOHEADER(biSize=ctypes.sizeof(BITMAPINFOHEADER), biWidth=w, biHeight=-h, biPlanes=1,
                                           biBitCount=32)
                    g32.StretchDIBits(hdc, 0, 0, w, h, 0, 0, w, h, d.solid, ctypes.byref(bmi), 0, 0x00CC0020)
                u32.EndPaint(hwnd, ctypes.byref(ps))
                return 0
        except Exception as e:
            self.log(f'taskbar readout message {msg:#x}: {e!r}')
        return u32.DefWindowProcW(hwnd, msg, wp, lp)
