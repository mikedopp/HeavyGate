"""HeavyGate rules window: which lane a command goes to, so nobody has to know what counts as heavy.

    pythonw heavyrules.pyw                (or: heavy rules, or Rules in the tray menu)
    python heavyrules.pyw --selftest DIR  drive every control in a throwaway state folder, save pictures, write report.json

The top shows a command being routed: type one and it runs down the lane it would really take, past the same code
heavy auto uses. The lanes on the right are live. Below are the rules, first match wins: drag to reorder, click a
command to edit it, flip the lane, pick the threads. Under those is what happens to everything else, and rules worth
adding, learned from what past runs really used. Every change is saved at once and can be undone.

Keyboard: Ctrl+N add, Ctrl+Z undo, and with the list focused Up/Down select, Alt+Up/Down move, Enter edit, Space flip
the lane, +/- threads, Delete remove.

Python standard library only. Canvas draws no smooth edges, so every rounded shape and glow here is a small PNG made by
hand from a distance function, and the lines are layered to soften them. Windows only for now.
"""
import base64
import ctypes
import json
import math
import os
import struct
import sys
import time
import tkinter as tk
import traceback
import zlib
from ctypes import wintypes

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import heavygate as hg  # noqa: E402

TITLE = 'HeavyGate rules'
u32 = ctypes.windll.user32
k32 = ctypes.windll.kernel32
g32 = ctypes.windll.gdi32
u32.FindWindowW.restype = wintypes.HWND
u32.GetParent.restype = wintypes.HWND
u32.GetParent.argtypes = [wintypes.HWND]
u32.GetDC.restype = ctypes.c_void_p
u32.ReleaseDC.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
u32.PrintWindow.argtypes = [wintypes.HWND, ctypes.c_void_p, wintypes.UINT]
g32.CreateCompatibleDC.restype = ctypes.c_void_p
g32.CreateCompatibleDC.argtypes = [ctypes.c_void_p]
g32.CreateDIBSection.restype = ctypes.c_void_p
g32.CreateDIBSection.argtypes = [ctypes.c_void_p, ctypes.c_void_p, wintypes.UINT, ctypes.c_void_p, ctypes.c_void_p, wintypes.DWORD]
g32.SelectObject.restype = ctypes.c_void_p
g32.SelectObject.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
g32.DeleteObject.argtypes = [ctypes.c_void_p]
g32.DeleteDC.argtypes = [ctypes.c_void_p]

DARK = {'bg': '#12151b', 'panel': '#191d25', 'card': '#20252f', 'hover': '#282e3a', 'field': '#0e1116', 'line': '#343b49',
        'text': '#e8ebf0', 'dim': '#8f98a8', 'faint': '#5c6473', 'gpu': '#4cc2ff', 'cpu': '#b794ff', 'ok': '#30d158',
        'wait': '#ffaa33', 'bad': '#ff6b6b', 'focus': '#ffffff'}
CONTRAST = {'bg': '#000000', 'panel': '#000000', 'card': '#000000', 'hover': '#1a1a1a', 'field': '#000000', 'line': '#ffffff',
            'text': '#ffffff', 'dim': '#ffffff', 'faint': '#c0c0c0', 'gpu': '#00ffff', 'cpu': '#ffff00', 'ok': '#00ff00',
            'wait': '#ffff00', 'bad': '#ff8080', 'focus': '#00ffff'}
ICON = {'add': '\ue710', 'close': '\ue711', 'undo': '\ue7a7', 'grip': '\ue76f', 'bulb': '\uea80', 'up': '\ue70e', 'down': '\ue70d',
        'minus': '\ue738', 'copy': '\ue8c8', 'reset': '\ue72c'}


def high_contrast():
    class HC(ctypes.Structure):
        _fields_ = [('cbSize', wintypes.UINT), ('dwFlags', wintypes.DWORD), ('lpszDefaultScheme', wintypes.LPWSTR)]
    hc = HC(cbSize=ctypes.sizeof(HC))
    return bool(u32.SystemParametersInfoW(0x42, hc.cbSize, ctypes.byref(hc), 0) and hc.dwFlags & 1)


def motion_on():
    on = wintypes.BOOL(True)
    u32.SystemParametersInfoW(0x1042, 0, ctypes.byref(on), 0)   # SPI_GETCLIENTAREAANIMATION
    return bool(on.value)


C = CONTRAST if high_contrast() else DARK


def rgb(c):
    return tuple(int(c[i:i + 2], 16) for i in (1, 3, 5))


def mix(a, b, t):
    """Colour a moved t of the way to b. Tk has no alpha, so every soft colour here is mixed."""
    return '#' + ''.join(f'{round(x + (y - x) * t):02x}' for x, y in zip(rgb(a), rgb(b)))


def ease(t):
    return 1 - (1 - t) ** 3


def png(w, h, rows):
    def chunk(kind, data):
        return struct.pack('>I', len(data)) + kind + data + struct.pack('>I', zlib.crc32(kind + data))
    raw = b''.join(b'\0' + r for r in rows)
    return (b'\x89PNG\r\n\x1a\n' + chunk(b'IHDR', struct.pack('>IIBBBBB', w, h, 8, 6, 0, 0, 0))
            + chunk(b'IDAT', zlib.compress(raw, 6)) + chunk(b'IEND', b''))


class Art:
    """Smooth shapes as PNGs. Only the corners are worked out; the straight parts repeat, so a card costs a few hundred
    pixels of Python however wide it is."""
    def __init__(self):
        self.cache = {}

    def rrect(self, w, h, r, fill, stroke=None, sw=1.0, glow=0, glow_color=None):
        w, h, r, glow = int(w), int(h), min(r, min(w, h) / 2), int(glow)
        key = (w, h, r, fill, stroke, sw, glow, glow_color)
        if key in self.cache:
            return self.cache[key]
        W, H = w + 2 * glow, h + 2 * glow
        f = rgb(fill) if fill else None
        s = rgb(stroke) if stroke else None
        g = rgb(glow_color) if glow_color else (0, 0, 0)
        cx, cy, hx, hy = W / 2, H / 2, w / 2 - r, h / 2 - r

        def pixel(x, y):
            qx, qy = max(abs(x + .5 - cx) - hx, 0), max(abs(y + .5 - cy) - hy, 0)
            d = math.hypot(qx, qy) - r
            col, a = (0.0, 0.0, 0.0), 0.0
            if glow and d > -1:
                t = max(0.0, 1 - max(d, 0) / glow)
                ga = 0.42 * t * t
                col, a = g, ga
            cov = min(max(.5 - d, 0), 1)
            if f and cov:
                oa = cov + a * (1 - cov)
                col = tuple((p * cov + q * a * (1 - cov)) / oa for p, q in zip(f, col))
                a = oa
            if s:
                ring = min(max(.5 - abs(d + sw / 2) + sw / 2, 0), 1) * min(max(.5 - d, 0), 1)
                if ring:
                    oa = ring + a * (1 - ring)
                    col = tuple((p * ring + q * a * (1 - ring)) / oa for p, q in zip(s, col))
                    a = oa
            return bytes((round(col[0]), round(col[1]), round(col[2]), round(a * 255)))

        band = int(math.ceil(r)) + glow + 2
        band = min(band, W // 2, H // 2)

        def row(y):
            left = [pixel(x, y) for x in range(band)]
            mid = pixel(band, y) * (W - 2 * band)
            return b''.join(left) + mid + b''.join(reversed(left))

        top = [row(y) for y in range(band)]
        rows = top + [row(band)] * (H - 2 * band) + top[::-1]
        img = tk.PhotoImage(data=base64.b64encode(png(W, H, rows)))
        self.cache[key] = img
        return img


class Tween:
    """Everything that moves goes through one clock, which stops when nothing does."""
    def __init__(self, root):
        self.root, self.items, self.on, self.tickers = root, [], motion_on(), []
        self.running = False

    def run(self, secs, fn, done=None):
        if not self.on or secs <= 0:
            fn(1.0)
            if done:
                done()
            return
        self.items.append([time.perf_counter(), secs, fn, done])
        self.kick()

    def kick(self):
        if not self.running:
            self.running = True
            self.root.after(15, self.tick)

    def tick(self):
        now = time.perf_counter()
        for it in self.items[:]:
            t = min((now - it[0]) / it[1], 1.0)
            it[2](ease(t))
            if t >= 1:
                self.items.remove(it)
                if it[3]:
                    it[3]()
        alive = [f for f in self.tickers if f(now)]
        self.tickers = alive
        if self.items or self.tickers:
            self.root.after(15, self.tick)
        else:
            self.running = False


class Flow:
    """The top panel: a command enters on the left and takes its lane to the right."""
    def __init__(self, app, parent):
        self.app, self.S = app, app.S
        S = self.S
        self.h = int(186 * S)
        self.cv = tk.Canvas(parent, height=self.h, bg=C['bg'], highlightthickness=0, bd=0)
        self.cv.pack(fill='x')
        self.cmd = self.field('gradlew assembleRelease', 'A command, like gradlew assembleRelease')
        self.dir = self.field('', 'Folder it runs in (optional)')
        self.paths = {}
        self.parts = []
        self.spawned = {'gpu': 0.0, 'cpu': 0.0}
        self.lit = None
        self.lanes = {'gpu': None, 'cpu': None}
        self.w = 0
        self.last_key = None
        self.after_id = None
        self.dotimg = {k: app.art.rrect(6 * S, 6 * S, 3 * S, C[k], glow=int(5 * S), glow_color=C[k]) for k in ('gpu', 'cpu')}
        self.comet = {k: app.art.rrect(10 * S, 10 * S, 5 * S, mix(C[k], '#ffffff', .5), glow=int(9 * S), glow_color=C[k])
                      for k in ('gpu', 'cpu')}
        self.cv.bind('<Configure>', self.resized)

    def field(self, text, hint):
        e = tk.Entry(self.cv, bg=C['field'], fg=C['text'], insertbackground=C['text'], relief='flat', bd=0,
                     highlightthickness=0, font=self.app.mono, selectbackground=C['gpu'], selectforeground='#000000')
        e.hint, e.showing_hint = hint, False
        if text:
            e.insert(0, text)
        else:
            self.show_hint(e)
        e.bind('<FocusIn>', lambda _: self.hide_hint(e) or self.draw_fields())
        e.bind('<FocusOut>', lambda _: (self.show_hint(e) if not e.get() else None, self.draw_fields()))
        e.bind('<KeyRelease>', lambda _: self.changed())
        e.bind('<Return>', lambda _: self.send())
        return e

    def show_hint(self, e):
        e.delete(0, 'end')
        e.insert(0, e.hint)
        e.config(fg=C['faint'], font=self.app.small)
        e.showing_hint = True

    def hide_hint(self, e):
        if e.showing_hint:
            e.delete(0, 'end')
            e.config(fg=C['text'], font=self.app.mono)
            e.showing_hint = False

    def value(self, e):
        return '' if e.showing_hint else e.get().strip()

    def resized(self, ev):
        if ev.width != self.w:
            self.w = ev.width
            self.layout()

    def layout(self):
        S, cv, art = self.S, self.cv, self.app.art
        cv.delete('all')
        W, H = self.w, self.h
        pad = int(18 * S)
        self.left_w = lw = int(W * .46)
        self.fx, self.fy, self.fh = pad, int(38 * S), int(36 * S)
        self.fy2 = self.fy + self.fh + int(8 * S)
        self.fw = lw - pad
        cv.create_text(pad, int(20 * S), text='Try a command', anchor='w', fill=C['dim'], font=self.app.small)
        self.bg1 = cv.create_image(self.fx, self.fy, anchor='nw')
        self.bg2 = cv.create_image(self.fx, self.fy2, anchor='nw')
        ip = int(12 * S)
        cv.create_window(self.fx + ip, self.fy + self.fh // 2, window=self.cmd, anchor='w', width=self.fw - 2 * ip,
                         height=int(22 * S))
        cv.create_window(self.fx + ip, self.fy2 + self.fh // 2, window=self.dir, anchor='w', width=self.fw - 2 * ip,
                         height=int(22 * S))
        self.answer = cv.create_text(pad, self.fy2 + self.fh + int(16 * S), anchor='w', fill=C['text'], font=self.app.h2)
        self.because = cv.create_text(pad, self.fy2 + self.fh + int(36 * S), anchor='w', fill=C['dim'], font=self.app.small)
        cw, ch = int(W * .36), int(64 * S)
        self.cardx = W - pad - cw
        ys = {'gpu': int(24 * S), 'cpu': int(24 * S) + ch + int(14 * S)}
        start = (self.fx + self.fw, self.fy + self.fh // 2)
        self.cards = {}
        for lane, y in ys.items():
            end = (self.cardx, y + ch // 2)
            self.paths[lane] = self.curve(start, end)
            flat = [c for p in self.paths[lane] for c in p]
            lines = [cv.create_line(*flat, fill=mix(C['bg'], C['line'], a), width=wd, capstyle='round', joinstyle='round')
                     for wd, a in ((max(4 * S, 3), .28), (max(2.6 * S, 2), .6), (max(1.4 * S, 1), 1))]
            img = cv.create_image(self.cardx, y, anchor='nw', image=art.rrect(cw, ch, 12 * S, C['card'], C['line']))
            tx = self.cardx + int(32 * S)
            dot = cv.create_image(self.cardx + int(17 * S), y + ch // 2, image=art.rrect(9 * S, 9 * S, 4.5 * S, C['faint']))
            name = cv.create_text(tx, y + int(21 * S), anchor='w', text=f'{lane.upper()} lane', fill=C[lane], font=self.app.h3)
            info = cv.create_text(tx, y + int(43 * S), anchor='w', fill=C['dim'], font=self.app.small, text='free',
                                  width=cw - int(42 * S))
            self.cards[lane] = {'lines': lines, 'img': img, 'dot': dot, 'name': name, 'info': info, 'y': y, 'w': cw, 'h': ch}
        self.draw_fields()
        self.refresh_lanes()
        self.lit = self.last_key = None
        self.show(self.app.route_now(), animate=False)

    @staticmethod
    def curve(a, b, n=48):
        mx = (a[0] + b[0]) / 2
        pts = []
        for i in range(n + 1):
            t = i / n
            u = 1 - t
            pts.append((u ** 3 * a[0] + 3 * u * u * t * mx + 3 * u * t * t * mx + t ** 3 * b[0],
                        u ** 3 * a[1] + 3 * u * u * t * a[1] + 3 * u * t * t * b[1] + t ** 3 * b[1]))
        return pts

    def draw_fields(self):
        if not self.w:
            return
        art, S = self.app.art, self.S
        focus = self.cv.focus_get()
        for item, e in ((self.bg1, self.cmd), (self.bg2, self.dir)):
            on = focus is e
            self.cv.itemconfig(item, image=art.rrect(self.fw, self.fh, 10 * S, C['field'], C['gpu'] if on else C['line'],
                                                     sw=1.5 if on else 1.0))

    def changed(self):
        if self.after_id:
            self.cv.after_cancel(self.after_id)
        self.after_id = self.cv.after(140, lambda: self.show(self.app.route_now()))

    def send(self):
        self.last_key = None
        self.show(self.app.route_now())

    def show(self, r, animate=True):
        """r is what heavygate.route said, or None with nothing typed."""
        cv = self.cv
        if not r:
            self.lit = None
            cv.itemconfig(self.answer, text='Type a command to see where it goes', fill=C['dim'])
            cv.itemconfig(self.because, text='')
            self.paint_lines()
            self.app.rules.flash(None)
            return
        lane = r['lane']
        threads = r['threads'] or hg.usual_threads()
        if lane == 'gpu':
            text = 'GPU lane, normal priority'
        else:
            text = f'CPU lane, {threads} threads'
        why = (f"Rule {r['index'] + 1}: first match" if r['rule'] else 'No rule matched, so it takes the safe lane')
        cv.itemconfig(self.answer, text=text, fill=C[lane])
        cv.itemconfig(self.because, text=why)
        key = (lane, r['index'])
        if self.lit != lane or key != self.last_key:
            self.lit = lane
            self.paint_lines()
            if animate:
                self.fire(lane)
            self.app.rules.flash(r['index'], reveal=animate)
        self.last_key = key

    def paint_lines(self):
        for lane, c in self.cards.items():
            on = self.lit == lane
            base = C[lane] if on else C['line']
            for item, a in zip(c['lines'], (.22 if on else .28, .55 if on else .6, 1)):
                self.cv.itemconfig(item, fill=mix(C['bg'], base, a))
                if on:
                    self.cv.tag_raise(item)

    def fire(self, lane):
        """A bright dot runs down the lane; the card lights when it lands."""
        tw = self.app.tween
        if not tw.on:
            self.light(lane)
            return
        item = self.cv.create_image(*self.paths[lane][0], image=self.comet[lane])
        pts = self.paths[lane]
        trail = [self.cv.create_image(*pts[0], image=self.dotimg[lane]) for _ in range(3)]

        def step(t):
            for i, it in enumerate([item] + trail):
                k = max(t - i * .045, 0)
                x, y = pts[int(k * (len(pts) - 1))]
                self.cv.coords(it, x, y)

        def landed():
            self.cv.delete(item, *trail)
            self.light(lane)
        tw.run(.5, step, landed)

    def light(self, lane):
        c, art, S = self.cards[lane], self.app.art, self.S
        frames = [art.rrect(c['w'], c['h'], 12 * S, mix(C['card'], C[lane], a), mix(C['line'], C[lane], min(a * 3, 1)))
                  for a in (.0, .05, .1, .16, .22)]

        def step(t):
            k = 1 - abs(2 * t - 1)
            self.cv.itemconfig(c['img'], image=frames[round(k * (len(frames) - 1))])
        self.app.tween.run(.55, step)

    def refresh_lanes(self):
        """The cards are the real lanes, read without taking a lock, and a lane with a job shows slow traffic."""
        if not self.w:
            return
        art, S = self.app.art, self.S
        now_busy = False
        for lane, c in self.cards.items():
            h = hg.peek(lane)
            q = hg.peek_queue(lane)
            j = hg.job_view(h) if h else None
            self.lanes[lane] = j
            if j:
                now_busy = True
                state = C['ok'] if j['state'] == 'running' else C['wait']
                when = hg.fmt_secs(j['for_s']) if j['for_s'] is not None else ''
                text = f"{self.short(j['name'], 30)}  {when}"
            else:
                state, text = C['faint'], 'free'
            if q:
                text += f'  +{len(q)} waiting'
            held = sum(m['gpu_mb'] for m in hg.ollama_seen() or []) if lane == 'gpu' else 0
            if held:
                text += f'  · Ollama {held / 1024:.1f} GB'
            self.cv.itemconfig(c['info'], text=text)
            self.cv.itemconfig(c['dot'], image=art.rrect(9 * S, 9 * S, 4.5 * S, state, glow=int(5 * S) if j else 0, glow_color=state))
        if now_busy and self.app.tween.on and self.traffic not in self.app.tween.tickers:
            self.app.tween.tickers.append(self.traffic)
            self.app.tween.kick()

    @staticmethod
    def short(s, n):
        return s if len(s) <= n else s[:n - 1] + '\u2026'

    def traffic(self, now):
        for lane, j in self.lanes.items():
            if j and j['state'] == 'running' and now - self.spawned[lane] > 1.1:
                self.spawned[lane] = now
                self.parts.append([self.cv.create_image(*self.paths[lane][0], image=self.dotimg[lane]), lane, now])
        for p in self.parts[:]:
            t = (now - p[2]) / 2.4
            if t >= 1:
                self.cv.delete(p[0])
                self.parts.remove(p)
                continue
            pts = self.paths[p[1]]
            self.cv.coords(p[0], *pts[int(t * (len(pts) - 1))])
        return bool(self.parts) or any(j and j['state'] == 'running' for j in self.lanes.values())


class Rules:
    """The list. One canvas; rows are drawn from the data and found again by position, so a click needs no widget."""
    def __init__(self, app, parent):
        self.app, self.S = app, app.S
        S = self.S
        self.rowh, self.gap, self.pad = int(54 * S), int(6 * S), int(18 * S)
        wrap = tk.Frame(parent, bg=C['bg'])
        wrap.pack(fill='both', expand=True)
        self.cv = tk.Canvas(wrap, bg=C['bg'], highlightthickness=0, bd=0, takefocus=1)
        self.bar = tk.Canvas(wrap, width=int(10 * S), bg=C['bg'], highlightthickness=0, bd=0)
        self.bar.pack(side='right', fill='y')
        self.cv.pack(side='left', fill='both', expand=True)
        self.ys = {}        # rule id -> y now
        self.ids = []       # rule ids in order, kept with app.data rules
        self.next_id = 0
        self.w = self.h = 0
        self.hot = None
        self.sel = 0
        self.drag = None
        self.edit = None
        self.zones = []     # (x0, y0, x1, y1, kind, arg), newest on top
        self.flashed = None
        self.flash_t = 0
        self.total = 0
        self.thumb = None
        cv = self.cv
        cv.bind('<Configure>', self.resized)
        cv.bind('<Motion>', self.motion)
        cv.bind('<Leave>', lambda _: self.set_hot(None))
        cv.bind('<Button-1>', self.press)
        cv.bind('<B1-Motion>', self.dragged)
        cv.bind('<ButtonRelease-1>', self.release)
        cv.bind('<MouseWheel>', self.wheel)
        cv.bind('<FocusIn>', lambda _: self.draw())
        cv.bind('<FocusOut>', lambda _: self.draw())
        for key, fn in (('<Up>', lambda e: self.step(-1)), ('<Down>', lambda e: self.step(1)),
                        ('<Alt-Up>', lambda e: self.shift(-1)), ('<Alt-Down>', lambda e: self.shift(1)),
                        ('<Return>', lambda e: self.start_edit(self.sel)), ('<space>', lambda e: self.flip(self.sel)),
                        ('<plus>', lambda e: self.bump(self.sel, 1)), ('<equal>', lambda e: self.bump(self.sel, 1)),
                        ('<minus>', lambda e: self.bump(self.sel, -1)), ('<Delete>', lambda e: self.remove(self.sel))):
            cv.bind(key, fn)
        self.bar.bind('<Button-1>', self.bar_drag)
        self.bar.bind('<B1-Motion>', self.bar_drag)
        self.sync_ids()

    # data ------------------------------------------------------------------------------------------------------------
    def sync_ids(self):
        n = len(self.app.data['rules'])
        while len(self.ids) < n:
            self.ids.append(self.next_id)
            self.next_id += 1
        del self.ids[n:]

    def threads_options(self, rule):
        cores = os.cpu_count() or 1
        opts = [None] + sorted({max(cores // 4, 1), max(cores // 2, 1)}) + ['all']
        if isinstance(rule.get('threads'), int) and rule['threads'] not in opts:
            opts.insert(-1, rule['threads'])
        return opts

    # geometry --------------------------------------------------------------------------------------------------------
    def slot_y(self, i):
        return int(44 * self.S) + i * (self.rowh + self.gap)

    def resized(self, e):
        if (e.width, e.height) != (self.w, self.h):
            self.w, self.h = e.width, e.height
            self.finish_edit(True)
            self.draw()

    def text_w(self):
        return max(self.w - int(396 * self.S), int(120 * self.S))

    def draw(self):
        """Paints the whole list. A few dozen items; it takes well under a frame."""
        cv, art, S = self.cv, self.app.art, self.S
        if not self.w:
            return
        cv.delete('all')
        self.zones = []
        pad, W = self.pad, self.w - self.pad
        n = len(self.app.data['rules'])
        cv.create_text(pad, int(20 * S), text='Rules', anchor='w', fill=C['text'], font=self.app.h2)
        cv.create_text(pad + int(52 * S), int(21 * S), text='first match wins', anchor='w', fill=C['dim'], font=self.app.small)
        bw, bh = int(96 * S), int(28 * S)
        self.button(W - bw, int(6 * S), bw, bh, ICON['add'], 'Add rule', 'add', None)
        order = sorted(range(n), key=lambda i: self.drag is not None and self.ids[i] == self.drag['id'])
        for i in order:
            self.draw_row(i)
        y = self.slot_y(n) + int(2 * S)
        y = self.draw_else(y)
        y = self.draw_suggestions(y + int(16 * S))
        self.total = y + int(14 * S)
        cv.config(scrollregion=(0, 0, self.w, self.total))
        self.draw_bar()

    def zone(self, x, y, w, h, kind, arg=None):
        self.zones.append((x, y, x + w, y + h, kind, arg))

    def button(self, x, y, w, h, icon, text, kind, arg, primary=False):
        cv, art, S = self.cv, self.app.art, self.S
        hot = self.hot == (kind, arg)
        fill = C['gpu'] if primary else (C['hover'] if hot else C['card'])
        fg = '#06121a' if primary else C['text']
        cv.create_image(x, y, anchor='nw', image=art.rrect(w, h, 8 * S, fill, None if primary else C['line']))
        if icon:
            cv.create_text(x + int(16 * S), y + h // 2, text=icon, fill=fg, font=self.app.icons)
            cv.create_text(x + int(30 * S), y + h // 2, text=text, anchor='w', fill=fg, font=self.app.small)
        else:
            cv.create_text(x + w // 2, y + h // 2, text=text, fill=fg, font=self.app.small)
        self.zone(x, y, w, h, kind, arg)

    def draw_row(self, i):
        cv, art, S = self.cv, self.app.art, self.S
        r = self.app.data['rules'][i]
        rid = self.ids[i]
        y = self.ys.get(rid, self.slot_y(i))
        self.ys[rid] = y
        y = round(y)
        pad, W, h = self.pad, self.w - 2 * self.pad, self.rowh
        lifted = self.drag is not None and self.drag['id'] == rid
        hot = self.hot and self.hot[1] == i and self.hot[0] in ('row', 'text', 'grip', 'lane', 'seg', 'del')
        fill = C['hover'] if (hot or lifted) else C['card']
        if self.flashed == i:
            fill = mix(fill, C[r['lane']], self.flash_t * .22)
        focused = self.app.root.focus_get() is cv and self.sel == i
        stroke = C['focus'] if focused else (mix(C['line'], C[r['lane']], self.flash_t) if self.flashed == i else C['line'])
        if lifted:
            cv.create_image(pad - int(10 * S), y - int(10 * S), anchor='nw',
                            image=art.rrect(W, h, 12 * S, fill, C[r['lane']], glow=int(10 * S), glow_color='#000000'))
        else:
            cv.create_image(pad, y, anchor='nw', image=art.rrect(W, h, 12 * S, fill, stroke, sw=1.5 if focused else 1.0))
        my = y + h // 2
        cv.create_text(pad + int(16 * S), my, text=ICON['grip'], fill=C['dim'] if hot else C['faint'], font=self.app.icons)
        self.zone(pad, y, int(32 * S), h, 'grip', i)
        cv.create_text(pad + int(40 * S), my, text=str(i + 1), fill=C['faint'], font=self.app.small)
        tx = pad + int(56 * S)
        tw = self.text_w()
        cv.create_text(tx, y + int(19 * S), text=self.fit(r['match'] or 'any command', self.app.mono, tw), anchor='w',
                       fill=C['text'] if r['match'] else C['dim'], font=self.app.mono)
        sub = f"in {r['folder']}" if r['folder'] else 'in any folder'
        cv.create_text(tx, y + int(38 * S), text=self.fit(sub, self.app.small, tw), anchor='w',
                       fill=C['wait'] if r['folder'] else C['faint'], font=self.app.small)
        self.zone(tx - int(4 * S), y, tw + int(8 * S), h, 'text', i)
        # the lane switch
        sx = pad + W - int(286 * S)
        sw, sh = int(84 * S), int(28 * S)
        sy = my - sh // 2
        cv.create_image(sx, sy, anchor='nw', image=art.rrect(sw, sh, 8 * S, C['field'], C['line']))
        k = self.app.thumbs.get(rid, 0 if r['lane'] == 'gpu' else 1)
        kw = sw // 2 - int(2 * S)
        col = mix(C['gpu'], C['cpu'], k)
        cv.create_image(sx + int(2 * S) + k * (sw - kw - int(4 * S)), sy + int(2 * S), anchor='nw',
                        image=art.rrect(kw, sh - int(4 * S), 6 * S, mix(C['card'], col, .32), col))
        for lane, cx in (('gpu', sx + sw // 4), ('cpu', sx + 3 * sw // 4)):
            cv.create_text(cx, my, text=lane.upper(), fill=C['text'] if r['lane'] == lane else C['faint'], font=self.app.tiny)
        self.zone(sx, sy, sw, sh, 'lane', i)
        # threads
        ox = sx + sw + int(10 * S)
        ow = int(154 * S)
        if r['lane'] == 'gpu':
            cv.create_text(ox + ow // 2, my, text='no thread limit', fill=C['faint'], font=self.app.small)
        else:
            opts = self.threads_options(r)
            cv.create_image(ox, sy, anchor='nw', image=art.rrect(ow, sh, 8 * S, C['field'], C['line']))
            cell = (ow - int(4 * S)) / len(opts)
            pos = self.app.segs.get(rid, opts.index(r['threads']) if r['threads'] in opts else 0)
            cv.create_image(ox + int(2 * S) + pos * cell, sy + int(2 * S), anchor='nw',
                            image=art.rrect(cell, sh - int(4 * S), 6 * S, mix(C['card'], C['cpu'], .32), C['cpu']))
            for n, o in enumerate(opts):
                cx = ox + int(2 * S) + cell * (n + .5)
                label = 'usual' if o is None else 'all' if o == 'all' else str(o)
                cv.create_text(cx, my, text=label, fill=C['text'] if o == r['threads'] else C['dim'], font=self.app.tiny)
                self.zone(int(cx - cell / 2), sy, int(cell), sh, 'seg', (i, o))
        # delete
        dx = pad + W - int(24 * S)
        cv.create_text(dx, my, text=ICON['close'], font=self.app.icons,
                       fill=(C['bad'] if self.hot == ('del', i) else C['dim']) if hot else C['card'])
        self.zone(dx - int(14 * S), my - int(14 * S), int(28 * S), int(28 * S), 'del', i)
        self.zone(pad, y, W, h, 'row', i)

    def fit(self, text, font, width):
        if font.measure(text) <= width:
            return text
        while text and font.measure(text + '\u2026') > width:
            text = text[:-1]
        return text + '\u2026'

    def draw_else(self, y):
        """What every other command gets, and the one number that sets it."""
        cv, art, S = self.cv, self.app.art, self.S
        pad, W, h = self.pad, self.w - 2 * self.pad, int(46 * S)
        cv.create_image(pad, y, anchor='nw', image=art.rrect(W, h, 12 * S, C['panel'], C['line']))
        my = y + h // 2
        cv.create_text(pad + int(16 * S), my, text='Anything else', anchor='w', fill=C['text'], font=self.app.h3)
        cv.create_text(pad + int(120 * S), my, text='CPU lane, below normal priority', anchor='w', fill=C['dim'], font=self.app.small)
        n = hg.usual_threads()
        bx = pad + W - int(150 * S)
        b = int(26 * S)
        self.button(bx, my - b // 2, b, b, None, '\u2212', 'usual', -1)
        cv.create_text(bx + b + int(48 * S), my, text=f'{n} threads', fill=C['cpu'], font=self.app.h3)
        self.button(bx + b + int(96 * S), my - b // 2, b, b, None, '+', 'usual', 1)
        return y + h

    def draw_suggestions(self, y):
        cv, art, S = self.cv, self.app.art, self.S
        items = self.app.suggested
        if not items:
            return y
        pad, W = self.pad, self.w - 2 * self.pad
        cv.create_text(pad, y + int(10 * S), text='Learned from your runs', anchor='w', fill=C['text'], font=self.app.h3)
        y += int(26 * S)
        for s in items:
            h = int(62 * S)
            cv.create_image(pad, y, anchor='nw', image=art.rrect(W, h, 12 * S, mix(C['card'], C[s['lane']], .1),
                                                                 mix(C['line'], C[s['lane']], .55)))
            cv.create_text(pad + int(18 * S), y + h // 2, text=ICON['bulb'], fill=C[s['lane']], font=self.app.icons)
            cv.create_text(pad + int(38 * S), y + h // 2, text=s['text'], anchor='w', fill=C['text'], font=self.app.small,
                           width=W - int(240 * S))
            bx = pad + W - int(186 * S)
            self.button(bx, y + (h - int(28 * S)) // 2, int(84 * S), int(28 * S), None, 'Add rule', 'sug', s['key'], primary=True)
            self.button(bx + int(92 * S), y + (h - int(28 * S)) // 2, int(80 * S), int(28 * S), None, 'Not now', 'nosug', s['key'])
            y += h + self.gap
        return y

    def draw_bar(self):
        b, S = self.bar, self.S
        b.delete('all')
        if self.total <= self.h or not self.h:
            self.thumb = None
            return
        top, bottom = self.cv.yview()
        y0, y1 = top * self.h, bottom * self.h
        b.create_image(int(1 * S), y0 + 2, anchor='nw', image=self.app.art.rrect(6 * S, max(y1 - y0 - 4, 20 * S), 3 * S, C['line']))
        self.thumb = True

    # interaction -----------------------------------------------------------------------------------------------------
    def hit(self, e):
        x, y = self.cv.canvasx(e.x), self.cv.canvasy(e.y)
        for z in reversed(self.zones):
            if z[0] <= x <= z[2] and z[1] <= y <= z[3]:
                # zones are added widest last in a row, so look for the smallest one under the mouse first
                best = min((q for q in self.zones if q[0] <= x <= q[2] and q[1] <= y <= q[3]),
                           key=lambda q: (q[2] - q[0]) * (q[3] - q[1]))
                return best[4], best[5]
        return None

    def norm(self, h):
        if h and h[0] == 'seg':
            return 'seg', h[1][0]
        return h

    def set_hot(self, h):
        if h != self.hot:
            self.hot = h
            if not self.drag:
                self.draw()

    def motion(self, e):
        h = self.hit(e)
        self.set_hot(self.norm(h))
        kind = h[0] if h else None
        self.cv.config(cursor={'grip': 'fleur', 'text': 'xterm', 'lane': 'hand2', 'seg': 'hand2', 'del': 'hand2', 'add': 'hand2',
                               'usual': 'hand2', 'sug': 'hand2', 'nosug': 'hand2'}.get(kind, ''))

    def press(self, e):
        self.cv.focus_set()
        h = self.hit(e)
        if self.edit:
            self.finish_edit()
            h = self.hit(e)
        if not h:
            return
        kind, arg = h
        if kind == 'grip':
            self.sel = arg
            y = self.ys[self.ids[arg]]
            self.drag = {'id': self.ids[arg], 'off': self.cv.canvasy(e.y) - y, 'from': arg}
            self.draw()
        elif kind in ('row', 'text', 'lane', 'seg', 'del'):
            i = arg[0] if kind == 'seg' else arg
            self.sel = i
            if kind == 'text':
                self.start_edit(i)
            elif kind == 'lane':
                self.flip(i)
            elif kind == 'seg':
                self.pick(i, arg[1])
            elif kind == 'del':
                self.remove(i)
            else:
                self.draw()
        elif kind == 'add':
            self.add()
        elif kind == 'usual':
            self.app.set_usual(arg)
        elif kind == 'sug':
            self.app.accept(arg)
        elif kind == 'nosug':
            self.app.dismiss(arg)

    def dragged(self, e):
        d = self.drag
        if not d:
            return
        n = len(self.ids)
        y = self.cv.canvasy(e.y) - d['off']
        y = min(max(y, self.slot_y(0) - self.rowh / 3), self.slot_y(n - 1) + self.rowh / 3)
        self.ys[d['id']] = y
        at = min(max(round((y - self.slot_y(0)) / (self.rowh + self.gap)), 0), n - 1)
        i = self.ids.index(d['id'])
        if at != i:
            rules = self.app.data['rules']
            rules.insert(at, rules.pop(i))
            self.ids.insert(at, self.ids.pop(i))
            self.sel = at
            self.settle(skip=d['id'])
            self.app.reroute()
        self.draw()

    def release(self, e):
        d = self.drag
        if not d:
            return
        self.drag = None
        i = self.ids.index(d['id'])
        self.settle()
        if i != d['from']:
            self.app.save(f'Rule moved to number {i + 1}', undo=('move', i, d['from']))
        self.draw()

    def settle(self, skip=None):
        """Slides every row to its slot."""
        start = {rid: self.ys.get(rid, self.slot_y(i)) for i, rid in enumerate(self.ids)}
        goal = {rid: self.slot_y(i) for i, rid in enumerate(self.ids)}

        def step(t):
            for rid in self.ids:
                if rid != skip and rid in goal and rid in start:
                    self.ys[rid] = start[rid] + (goal[rid] - start[rid]) * t
            self.draw()
        self.app.tween.run(.16, step)

    def wheel(self, e):
        if self.total > self.h:
            self.cv.yview_scroll(int(-e.delta / 40), 'units')
            self.draw_bar()

    def bar_drag(self, e):
        if self.total > self.h:
            self.cv.yview_moveto(max(e.y / self.h - .1, 0))
            self.draw_bar()

    def step(self, d):
        n = len(self.ids)
        if n:
            self.sel = min(max(self.sel + d, 0), n - 1)
            self.reveal(self.sel)
            self.draw()
        return 'break'

    def reveal(self, i):
        if self.total > self.h:
            y = self.slot_y(i)
            top = self.cv.canvasy(0)
            if y < top or y + self.rowh > top + self.h:
                self.cv.yview_moveto(max(y - self.rowh, 0) / self.total)
                self.draw_bar()

    def shift(self, d):
        i, n = self.sel, len(self.ids)
        j = i + d
        if 0 <= i < n and 0 <= j < n:
            rules = self.app.data['rules']
            rules.insert(j, rules.pop(i))
            self.ids.insert(j, self.ids.pop(i))
            self.sel = j
            self.settle()
            self.app.save(f'Rule moved to number {j + 1}', undo=('move', j, i))
        return 'break'

    def flip(self, i):
        if not 0 <= i < len(self.ids):
            return 'break'
        r = self.app.data['rules'][i]
        was = dict(r)
        new = 'cpu' if r['lane'] == 'gpu' else 'gpu'
        rid = self.ids[i]
        a, b = (0, 1) if new == 'cpu' else (1, 0)
        r['lane'] = new
        if new == 'gpu':
            r['threads'] = None

        def step(t):
            self.app.thumbs[rid] = a + (b - a) * t
            self.draw()

        def done():
            self.app.thumbs.pop(rid, None)
            self.draw()
        self.app.tween.run(.16, step, done)
        self.app.save(f"{r['match'] or r['folder']} now goes to the {new.upper()} lane", undo=('set', i, was))
        return 'break'

    def pick(self, i, value):
        r = self.app.data['rules'][i]
        if r['threads'] == value:
            return
        was = dict(r)
        opts = self.threads_options(r)
        old = opts.index(r['threads']) if r['threads'] in opts else 0
        new = opts.index(value) if value in opts else 0
        rid = self.ids[i]
        r['threads'] = value

        def step(t):
            self.app.segs[rid] = old + (new - old) * t
            self.draw()

        def done():
            self.app.segs.pop(rid, None)
            self.draw()
        self.app.tween.run(.14, step, done)
        self.app.save('Threads changed', undo=('set', i, was))

    def bump(self, i, d):
        if 0 <= i < len(self.ids):
            r = self.app.data['rules'][i]
            if r['lane'] == 'cpu':
                opts = self.threads_options(r)
                k = opts.index(r['threads']) if r['threads'] in opts else 0
                self.pick(i, opts[min(max(k + d, 0), len(opts) - 1)])
        return 'break'

    def remove(self, i):
        if not 0 <= i < len(self.ids):
            return 'break'
        r = self.app.data['rules'].pop(i)
        rid = self.ids.pop(i)
        self.ys.pop(rid, None)
        self.sel = min(self.sel, max(len(self.ids) - 1, 0))
        self.hot = None
        self.settle()
        self.app.save(f"Removed {r['match'] or r['folder']}", undo=('add', i, r))
        return 'break'

    def add(self, rule=None):
        rules = self.app.data['rules']
        rules.insert(0, rule or {'match': '', 'folder': '', 'lane': 'cpu', 'threads': None})
        self.ids.insert(0, self.next_id)
        self.next_id += 1
        self.ys[self.ids[0]] = self.slot_y(0)
        self.sel = 0
        self.cv.yview_moveto(0)
        self.settle(skip=self.ids[0])
        self.app.reroute()
        self.draw()
        if not rule:
            self.start_edit(0, new=True)
        return 'break'

    def start_edit(self, i, new=False):
        if not 0 <= i < len(self.ids):
            return 'break'
        self.finish_edit()
        S, cv, r = self.S, self.cv, self.app.data['rules'][i]
        y = self.slot_y(i)
        tx, tw = self.pad + int(56 * S), self.text_w()
        boxes = []
        for text, font, off, hint in ((r['match'], self.app.mono, 18, 'command has'), (r['folder'], self.app.small, 38, 'in folder (optional)')):
            e = tk.Entry(cv, bg=C['field'], fg=C['text'], insertbackground=C['text'], relief='flat', bd=0, font=font,
                         highlightthickness=1, highlightbackground=C['line'], highlightcolor=C['gpu'],
                         selectbackground=C['gpu'], selectforeground='#000000')
            e.insert(0, text)
            win = cv.create_window(tx - int(3 * S), y + int(off * S), window=e, anchor='w', width=tw, height=int(20 * S if off == 18 else 17 * S))
            e.bind('<Return>', lambda _: self.finish_edit() or 'break')
            e.bind('<Escape>', lambda _: self.finish_edit(True) or 'break')
            boxes.append((e, win))
        self.edit = {'i': i, 'boxes': boxes, 'new': new, 'was': dict(r)}
        boxes[0][0].focus_set()
        boxes[0][0].select_range(0, 'end')
        return 'break'

    def finish_edit(self, cancel=False):
        ed = self.edit
        if not ed:
            return
        self.edit = None
        i = ed['i']
        match, folder = (b[0].get().strip() for b in ed['boxes'])
        for e, win in ed['boxes']:
            self.cv.delete(win)
            e.destroy()
        rules = self.app.data['rules']
        if cancel or not (match or folder):
            if ed['new'] or (not cancel and not ed['was'].get('match') and not ed['was'].get('folder')):
                rules.pop(i)
                self.ys.pop(self.ids.pop(i), None)
                self.settle()
            if not cancel and not ed['new']:
                self.app.say('A rule needs a command or a folder, so nothing changed')
            self.cv.focus_set()
            self.draw()
            return
        r = rules[i]
        changed = (r['match'], r['folder']) != (match, folder)
        r['match'], r['folder'] = match, folder
        self.cv.focus_set()
        if changed:
            self.app.save(f'Rule {i + 1} saved', undo=('del', i) if ed['new'] else ('set', i, ed['was']))
        self.draw()

    def flash(self, i, reveal=False):
        """Lights the rule that just won."""
        self.flashed = i
        if i is None:
            self.flash_t = 0
            self.draw()
            return
        if reveal and i is not None:
            self.reveal(i)

        def step(t):
            self.flash_t = 1 - abs(2 * t - 1) * .35 if t < .5 else 1 - t * .35 - .0
            self.flash_t = max(min(self.flash_t, 1), .35)
            self.draw()
        self.app.tween.run(.6, step)


class Footer:
    def __init__(self, app, parent):
        self.app, S = app, app.S
        self.h = int(38 * S)
        self.cv = tk.Canvas(parent, height=self.h, bg=C['panel'], highlightthickness=0, bd=0)
        self.cv.pack(fill='x', side='bottom')
        self.msg = ''
        self.undo = False
        self.timer = None
        self.cv.bind('<Configure>', lambda e: self.draw())
        self.cv.bind('<Button-1>', self.click)
        self.cv.bind('<Motion>', self.motion)
        self.zones = []

    def draw(self):
        cv, S = self.cv, self.app.S
        cv.delete('all')
        self.zones = []
        W = cv.winfo_width()
        my = self.h // 2
        pad = int(18 * S)
        x = pad
        if self.msg:
            t = cv.create_text(x, my, text=self.msg, anchor='w', fill=C['text'], font=self.app.small)
            x = cv.bbox(t)[2] + int(12 * S)
            if self.undo:
                t = cv.create_text(x, my, text='Undo', anchor='w', fill=C['gpu'], font=self.app.h3s)
                b = cv.bbox(t)
                self.zones.append((b[0] - 4, 0, b[2] + 4, self.h, 'undo'))
        else:
            cv.create_text(x, my, text='Run a job with', anchor='w', fill=C['dim'], font=self.app.small, tags='hint')
            x = cv.bbox('hint')[2] + int(8 * S)
            t = cv.create_text(x, my, text='heavy auto -- your command', anchor='w', fill=C['text'], font=self.app.monos)
            b = cv.bbox(t)
            self.zones.append((b[0] - 4, 0, b[2] + 4, self.h, 'copy'))
        right = W - pad
        t = cv.create_text(right, my, text=f'HeavyGate {hg.VERSION}', anchor='e', fill=C['faint'], font=self.app.small)
        right = cv.bbox(t)[0] - int(18 * S)
        t = cv.create_text(right, my, text='Starter rules', anchor='e', fill=C['dim'], font=self.app.small)
        b = cv.bbox(t)
        self.zones.append((b[0] - 4, 0, b[2] + 4, self.h, 'reset'))

    def hit(self, e):
        return next((z[4] for z in self.zones if z[0] <= e.x <= z[2]), None)

    def motion(self, e):
        self.cv.config(cursor='hand2' if self.hit(e) else '')

    def click(self, e):
        k = self.hit(e)
        if k == 'undo':
            self.app.undo()
        elif k == 'copy':
            self.app.root.clipboard_clear()
            self.app.root.clipboard_append('heavy auto -- ')
            self.say('Copied. Add your command after the two dashes')
        elif k == 'reset':
            self.app.starters()

    def say(self, msg, undo=False):
        self.msg, self.undo = msg, undo
        self.draw()
        if self.timer:
            self.cv.after_cancel(self.timer)
        self.timer = self.cv.after(7000 if undo else 3500, self.clear)

    def clear(self):
        self.msg, self.undo, self.timer = '', False, None
        self.draw()


class App:
    def __init__(self):
        try:
            ctypes.windll.shcore.SetProcessDpiAwareness(1)
        except (OSError, AttributeError):
            u32.SetProcessDPIAware()
        self.root = root = tk.Tk()
        root.withdraw()
        root.title(TITLE)
        root.configure(bg=C['bg'])
        self.S = S = max(root.winfo_fpixels('1i') / 96, 1)
        root.tk.call('tk', 'scaling', root.winfo_fpixels('1i') / 72)
        from tkinter import font as tkfont
        F = lambda fam, size, weight='normal': tkfont.Font(root=root, family=fam, size=size, weight=weight)
        self.small, self.tiny = F('Segoe UI', 9), F('Segoe UI Semibold', 8)
        self.h3, self.h3s, self.h2 = F('Segoe UI Semibold', 10), F('Segoe UI Semibold', 9), F('Segoe UI Semibold', 12)
        self.mono, self.monos, self.icons = F('Consolas', 11), F('Consolas', 9), F('Segoe MDL2 Assets', 10)
        self.art, self.tween = Art(), Tween(root)
        self.thumbs, self.segs, self.history = {}, {}, []
        self.data = hg.read_rules()
        self.suggested = hg.suggestions(data=self.data)
        self.stamp = self.file_stamp()
        icon = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'docs', 'icon.png')
        if os.path.exists(icon):
            self.icon = tk.PhotoImage(file=icon)
            root.iconphoto(True, self.icon)
        self.footer = Footer(self, root)
        self.rules = None
        self.flow = Flow(self, root)
        self.rules = Rules(self, root)
        root.bind('<Control-n>', lambda e: self.rules.add())
        root.bind('<Control-z>', lambda e: self.undo())
        root.protocol('WM_DELETE_WINDOW', self.close)
        w, h = int(860 * S), int(720 * S)
        root.geometry(f'{w}x{h}')
        root.minsize(int(720 * S), int(520 * S))
        root.after(30, self.pulse)
        self.place_saved()

    def show(self):
        self.root.deiconify()
        self.root.update_idletasks()
        self.dark_title()

    def place_saved(self):
        self.root.update_idletasks()
        sw, sh = self.root.winfo_screenwidth(), self.root.winfo_screenheight()
        w, h = int(860 * self.S), int(720 * self.S)
        self.root.geometry(f'+{max((sw - w) // 2, 0)}+{max((sh - h) // 3, 0)}')

    def dark_title(self):
        """The real title bar, in the dark mode Windows 10 and 11 offer apps."""
        try:
            hwnd = u32.GetParent(self.root.winfo_id()) or self.root.winfo_id()
            on = ctypes.c_int(1)
            ctypes.windll.dwmapi.DwmSetWindowAttribute(wintypes.HWND(hwnd), 20, ctypes.byref(on), ctypes.sizeof(on))
        except (OSError, AttributeError):
            pass

    def file_stamp(self):
        try:
            return os.stat(hg.rules_path()).st_mtime_ns
        except OSError:
            return None

    def route_now(self):
        cmd = self.flow.value(self.flow.cmd)
        if not cmd:
            return None
        return hg.route(cmd, self.flow.value(self.flow.dir), self.data['rules'])

    def pulse(self):
        """Live lanes every two seconds, and rules changed by something else are picked up."""
        try:
            self.flow.refresh_lanes()
            if self.file_stamp() != self.stamp and not self.rules.edit and not self.rules.drag:
                self.stamp = self.file_stamp()
                self.data = hg.read_rules()
                self.rules.sync_ids()
                self.rules.draw()
        except Exception:   # a bad read must not end the loop
            traceback.print_exc()
        self.root.after(2000, self.pulse)

    def save(self, msg, undo=None):
        hg.save_rules(self.data['rules'], self.data['dismissed'])
        self.stamp = self.file_stamp()
        if undo:
            self.history.append(undo)
        self.suggested = hg.suggestions(data=self.data)
        self.footer.say(msg, undo=bool(undo))
        self.rules.sync_ids()
        self.rules.draw()
        self.flow.last_key = None
        self.flow.show(self.route_now(), animate=False)

    def reroute(self):
        """The rule numbers moved, so the answer on top and the lit row follow."""
        self.flow.last_key = None
        self.flow.show(self.route_now(), animate=False)

    def say(self, msg):
        self.footer.say(msg)

    def undo(self):
        self.rules.finish_edit()
        if not self.history:
            return 'break'
        u = self.history.pop()
        rules, R = self.data['rules'], self.rules
        if u[0] == 'set':
            rules[u[1]] = u[2]
        elif u[0] == 'add':
            rules.insert(u[1], u[2])
            R.ids.insert(u[1], R.next_id)
            R.next_id += 1
        elif u[0] == 'del':
            rules.pop(u[1])
            R.ids.pop(u[1])
        elif u[0] == 'move':
            rules.insert(u[2], rules.pop(u[1]))
            R.ids.insert(u[2], R.ids.pop(u[1]))
        elif u[0] == 'all':
            self.data['rules'] = u[1]
            R.ids.clear()
            R.ys.clear()
            R.sync_ids()
        R.settle()
        self.save('Undone')
        return 'break'

    def set_usual(self, d):
        n = min(max(hg.usual_threads() + d, 1), os.cpu_count() or 1)
        hg.set_setting('cpu_threads', n)
        self.footer.say(f'CPU jobs now get {n} threads unless a rule says otherwise')
        self.rules.draw()
        self.flow.last_key = None
        self.flow.show(self.route_now(), animate=False)

    def accept(self, key):
        s = next((x for x in self.suggested if x['key'] == key), None)
        if s:
            self.rules.add({k: s[k] for k in ('match', 'folder', 'lane', 'threads')})
            self.save(f"Added {s['match']}", undo=('del', 0))

    def dismiss(self, key):
        self.data['dismissed'].append(key)
        self.save('It will not be suggested again')

    def starters(self):
        was = [dict(r) for r in self.data['rules']]
        self.data['rules'] = [dict(r) for r in hg.STARTER_RULES]
        self.rules.ids.clear()
        self.rules.ys.clear()
        self.rules.sync_ids()
        self.rules.sel = 0
        self.save('Back to the starter rules', undo=('all', was))

    def close(self):
        self.rules.finish_edit()
        self.root.destroy()

    def run(self):
        self.show()
        self.root.mainloop()


# proof --------------------------------------------------------------------------------------------------------------
def capture(root, path):
    """What this window itself draws, never the screen."""
    class BMI(ctypes.Structure):
        _fields_ = [('biSize', wintypes.DWORD), ('biWidth', wintypes.LONG), ('biHeight', wintypes.LONG), ('biPlanes', wintypes.WORD),
                    ('biBitCount', wintypes.WORD), ('biCompression', wintypes.DWORD), ('biSizeImage', wintypes.DWORD),
                    ('biXPelsPerMeter', wintypes.LONG), ('biYPelsPerMeter', wintypes.LONG), ('biClrUsed', wintypes.DWORD),
                    ('biClrImportant', wintypes.DWORD)]
    for _ in range(6):
        root.update()
        time.sleep(.03)
    hwnd = u32.GetParent(root.winfo_id()) or root.winfo_id()
    r = wintypes.RECT()
    u32.GetWindowRect(hwnd, ctypes.byref(r))
    w, h = r.right - r.left, r.bottom - r.top
    screen = u32.GetDC(None)
    dc = g32.CreateCompatibleDC(screen)
    bmi = BMI(biSize=ctypes.sizeof(BMI), biWidth=w, biHeight=-h, biPlanes=1, biBitCount=32)
    bits = ctypes.c_void_p()
    bmp = g32.CreateDIBSection(screen, ctypes.byref(bmi), 0, ctypes.byref(bits), None, 0)
    old = g32.SelectObject(dc, bmp)
    ok = u32.PrintWindow(hwnd, dc, 2)
    data = ctypes.string_at(bits, w * h * 4)
    g32.SelectObject(dc, old)
    g32.DeleteObject(bmp)
    g32.DeleteDC(dc)
    u32.ReleaseDC(None, screen)
    rows = []
    for y in range(h):
        line = bytearray(data[y * w * 4:(y + 1) * w * 4])
        line[0::4], line[2::4] = line[2::4], line[0::4]
        line[3::4] = b'\xff' * w
        rows.append(bytes(line))
    with open(path, 'wb') as f:
        f.write(png(w, h, rows))
    return bool(ok), w, h


def selftest(out):
    os.makedirs(out, exist_ok=True)
    os.environ['HEAVYGATE_DIR'] = os.path.join(out, 'state')
    os.makedirs(hg.home(), exist_ok=True)
    now = '2026-11-0%dT10:00:00'
    with open(os.path.join(hg.home(), 'runs.jsonl'), 'w') as f:
        for i in (1, 2, 3):
            f.write(json.dumps({'name': 'render', 'lane': 'cpu', 'command': 'python tools/render.py', 'cwd': r'F:\Demo',
                                'ran_s': 90, 'gpu_util_max': 85, 'cpu_avg': 20, 'started': now % i}) + '\n')
            f.write(json.dumps({'name': 'site', 'lane': 'cpu', 'command': 'pack.cmd', 'cwd': r'F:\Demo\Site', 'ran_s': 400,
                                'gpu_util_max': 5, 'cpu_avg': 72, 'started': (now % i).replace('T10', 'T12')}) + '\n')
    # pack.cmd's "all threads" tip needs a usual share under the whole machine. On 2 cores the default share is both,
    # so the suggestions part runs with half the threads; the setting goes before the share controls are tried.
    share = os.path.join(hg.home(), 'settings.json')
    with open(share, 'w') as f:
        json.dump({'cpu_threads': max(1, (os.cpu_count() or 2) // 2)}, f)
    app = App()
    root, R, Fl = app.root, app.rules, app.flow
    app.tween.on = False
    root.attributes('-alpha', 0.01)   # Tk paints nothing off screen, so it sits on screen but can't be seen
    root.geometry('+60+60')
    root.deiconify()
    root.update()
    app.dark_title()
    results = {}

    def check(name, ok):
        results[name] = bool(ok)

    def pump(n=3):
        for _ in range(n):
            root.update()
            time.sleep(.02)

    class Ev:
        def __init__(self, x, y):
            self.x, self.y = x, y

    def zone(kind, arg):
        z = next(z for z in R.zones if z[4] == kind and z[5] == arg)
        return Ev((z[0] + z[2]) / 2 - R.cv.canvasx(0), (z[1] + z[3]) / 2 - R.cv.canvasy(0))

    pump()
    capture(root, os.path.join(out, '1-start.png'))
    R.cv.yview_moveto(1)
    R.draw_bar()
    capture(root, os.path.join(out, '1b-bottom.png'))
    R.cv.yview_moveto(0)
    R.draw_bar()
    check('starter rules shown', len(app.data['rules']) == len(hg.STARTER_RULES))
    check('suggestions found', {s['match'] for s in app.suggested} >= {'render.py', 'pack.cmd'})
    check('try routes gradlew to the cpu lane', Fl.lit == 'cpu')

    Fl.hide_hint(Fl.cmd)
    Fl.cmd.delete(0, 'end')
    Fl.cmd.insert(0, 'blender -b scene.blend')
    Fl.send()
    pump()
    check('try routes blender to the gpu lane', Fl.lit == 'gpu' and R.flashed == 3)
    capture(root, os.path.join(out, '2-gpu.png'))

    R.press(zone('lane', 0))
    pump()
    check('lane flip saved', hg.read_rules()['rules'][0]['lane'] == 'gpu')
    app.undo()
    pump()
    check('undo restores it', hg.read_rules()['rules'][0]['lane'] == 'cpu')

    R.press(zone('seg', (0, 'all')))
    pump()
    check('threads saved', hg.read_rules()['rules'][0]['threads'] == 'all')

    R.add()
    pump()
    box, folder = R.edit['boxes'][0][0], R.edit['boxes'][1][0]
    box.insert(0, 'render.exe')
    folder.insert(0, 'Studio')
    pump()
    capture(root, os.path.join(out, '3-editing.png'))
    R.finish_edit()
    pump()
    check('new rule saved', hg.read_rules()['rules'][0] == {'match': 'render.exe', 'folder': 'Studio', 'lane': 'cpu', 'threads': None})

    R.add()
    R.finish_edit()
    pump()
    check('an empty new rule disappears', hg.read_rules()['rules'][0]['match'] == 'render.exe' and len(R.ids) == len(app.data['rules']))

    first = app.data['rules'][0]['match']
    g = zone('grip', 0)
    R.press(g)
    step = R.rowh + R.gap
    for k in range(1, 5):
        R.dragged(Ev(g.x, g.y + k * step * .55))
        pump(1)
    capture(root, os.path.join(out, '4-dragging.png'))
    R.dragged(Ev(g.x, g.y + 2 * step))
    R.release(Ev(g.x, g.y + 2 * step))
    pump()
    check('drag reorders and saves', hg.read_rules()['rules'][2]['match'] == first)

    R.sel = 1
    n = len(app.data['rules'])
    R.remove(1)
    pump()
    check('remove saved', len(hg.read_rules()['rules']) == n - 1)
    app.undo()
    pump()
    check('undo brings it back', len(hg.read_rules()['rules']) == n and len(R.ids) == n)

    key = app.suggested[0]['key']
    app.dismiss(key)
    pump()
    check('dismissed suggestion stays gone', key not in {s['key'] for s in hg.suggestions()})

    app.accept(app.suggested[0]['key'])
    pump()
    check('accepted suggestion becomes rule 1', hg.read_rules()['rules'][0]['match'] in ('render.py', 'pack.cmd'))

    os.remove(share)
    before = hg.usual_threads()
    app.set_usual(-1)
    check('usual share saved', hg.usual_threads() == before - 1)
    app.set_usual(1)

    R.cv.focus_set()
    R.sel = 0
    pump()
    R.step(1)
    check('keyboard selects', R.sel == 1)
    R.shift(1)
    check('keyboard moves', R.sel == 2)
    lane = app.data['rules'][2]['lane']
    R.flip(2)
    check('keyboard flips', app.data['rules'][2]['lane'] != lane)
    pump()
    capture(root, os.path.join(out, '5-keyboard.png'))

    app.starters()
    pump()
    check('starter rules restored', len(hg.read_rules()['rules']) == len(hg.STARTER_RULES))
    app.undo()
    pump()
    check('undo after starters', len(hg.read_rules()['rules']) != len(hg.STARTER_RULES))

    root.geometry(f'{int(720 * app.S)}x{int(520 * app.S)}')
    pump(6)
    capture(root, os.path.join(out, '6-small.png'))
    check('list scrolls when small', R.total > R.h)

    app.close()
    report = {'version': hg.VERSION, 'checks': results, 'failed': [k for k, v in results.items() if not v]}
    with open(os.path.join(out, 'report.json'), 'w') as f:
        json.dump(report, f, indent=2)
    print(json.dumps(report, indent=2))
    return 1 if report['failed'] else 0


def main(argv):
    if len(argv) > 2 and argv[1] == '--selftest':
        return selftest(argv[2])
    mutex = k32.CreateMutexW(None, False, 'HeavyGateRulesWindow')
    if mutex and k32.GetLastError() == 183:   # ERROR_ALREADY_EXISTS: a second launch brings the first one back
        hwnd = u32.FindWindowW(None, TITLE)
        if hwnd:
            u32.ShowWindow(hwnd, 9)
            u32.SetForegroundWindow(hwnd)
        return 0
    App().run()
    return 0


if __name__ == '__main__':
    sys.exit(main(sys.argv))
