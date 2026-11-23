"""HeavyGate: free RAM for a build. A small window listing your apps by memory; tick the ones to close.

    pythonw heavyapps.pyw            (or the strip's "free RAM..." link, or "Free RAM..." in the tray menu)
    python heavyapps.pyw --selftest DIR   draw it with sample apps, save window.png, close nothing

Windows itself, HeavyGate, Python, SSH and the jobs running in the lanes are never listed (heavyapps.KEEP). Ticked
apps are asked to close as if you clicked their X; one still running after 8 s is ended.
"""
import os
import sys
import threading
import tkinter as tk

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import heavyapps  # noqa: E402
import heavygate as hg  # noqa: E402

BG, PANEL, FG, DIM, ACCENT, WARN = '#15191f', '#1d232b', '#e6e9ee', '#8b95a3', '#5fb3f0', '#e9be77'
FONT, SMALL, BOLD = ('Segoe UI', 10), ('Segoe UI', 9), ('Segoe UI Semibold', 10)


class Window:
    def __init__(self, root, sample=None):
        self.root, self.sample = root, sample
        root.title('HeavyGate: free RAM')
        root.configure(bg=BG)
        root.minsize(420, 260)
        head = tk.Frame(root, bg=BG)
        head.pack(fill='x', padx=14, pady=(12, 4))
        self.ram = tk.Label(head, text='', bg=BG, fg=FG, font=BOLD, anchor='w')
        self.ram.pack(fill='x')
        tk.Label(head, text='Tick apps to close. Each is asked to close first; one still running after 8 s is ended '
                 '(unsaved work in it is lost). Windows, HeavyGate and running builds are never listed.',
                 bg=BG, fg=DIM, font=SMALL, wraplength=440, justify='left', anchor='w').pack(fill='x', pady=(2, 0))
        self.list = tk.Frame(root, bg=PANEL, padx=8, pady=6)
        self.list.pack(fill='both', expand=True, padx=14, pady=8)
        foot = tk.Frame(root, bg=BG)
        foot.pack(fill='x', padx=14, pady=(0, 12))
        self.status = tk.Label(foot, text='', bg=BG, fg=DIM, font=SMALL, anchor='w', justify='left', wraplength=440)
        self.status.pack(fill='x', pady=(0, 6))
        row = tk.Frame(foot, bg=BG)
        row.pack(fill='x')
        self.close_btn = self.button(row, 'Close ticked', self.close, primary=True)
        self.button(row, 'Tick all', self.tick_all)
        self.button(row, 'Refresh', self.refresh)
        self.button(row, 'Done', root.destroy, side='right')
        self.vars, self.busy = {}, False
        root.bind('<Escape>', lambda e: root.destroy())
        self.refresh()

    def button(self, parent, text, fn, primary=False, side='left'):
        b = tk.Button(parent, text=text, command=fn, font=FONT, relief='flat', bd=0, padx=12, pady=4,
                      bg=ACCENT if primary else PANEL, fg='#0b1220' if primary else FG, activebackground=FG,
                      activeforeground=BG, cursor='hand2', highlightthickness=1, highlightbackground='#2c3440')
        b.pack(side=side, padx=(0, 6) if side == 'left' else (6, 0))
        return b

    def rows(self):
        return self.sample if self.sample is not None else heavyapps.apps()

    def refresh(self):
        avail, total, _, _ = hg.mem_mb()
        self.ram.configure(text=f'RAM {avail / 1024:.1f} GB free of {total / 1024:.1f} GB' if total else 'RAM')
        ticked = {n for n, v in self.vars.items() if v.get()}
        for w in self.list.winfo_children():
            w.destroy()
        self.vars = {}
        apps = self.rows()
        if not apps:
            tk.Label(self.list, text='Nothing of yours to close here.', bg=PANEL, fg=DIM, font=FONT).pack(anchor='w')
        for a in apps[:14]:
            v = tk.BooleanVar(value=a['name'] in ticked)
            self.vars[a['name']] = v
            line = tk.Frame(self.list, bg=PANEL)
            line.pack(fill='x', pady=1)
            tk.Checkbutton(line, variable=v, bg=PANEL, activebackground=PANEL, selectcolor=BG, fg=FG, bd=0,
                           highlightthickness=0, cursor='hand2').pack(side='left')
            tk.Label(line, text=a['name'], bg=PANEL, fg=FG, font=FONT, anchor='w').pack(side='left')
            extra = f"{len(a['pids'])} processes" if len(a['pids']) > 1 else ''
            if not a['windowed']:
                extra = (extra + ' · ' if extra else '') + 'no window'
            tk.Label(line, text=f"{a['mb'] / 1024:.1f} GB", bg=PANEL, fg=FG, font=BOLD, width=7,
                     anchor='e').pack(side='right')
            if extra:
                tk.Label(line, text=extra, bg=PANEL, fg=DIM, font=SMALL).pack(side='right', padx=8)
            for w in line.winfo_children()[1:]:
                w.bind('<Button-1>', lambda e, v=v: v.set(not v.get()))

    def tick_all(self):
        on = not all(v.get() for v in self.vars.values())
        for v in self.vars.values():
            v.set(on)

    def close(self):
        names = [n for n, v in self.vars.items() if v.get()]
        if self.busy or not names:
            if not names:
                self.status.configure(text='Tick an app first.', fg=WARN)
            return
        self.busy = True
        self.close_btn.configure(state='disabled', text='Closing...')
        self.status.configure(text='Asking ' + ', '.join(names) + ' to close...', fg=DIM)
        lines = []

        def work():
            if self.sample is None:
                heavyapps.close(names, say=lines.append)
            self.root.after(0, done)

        def done():
            self.busy = False
            self.close_btn.configure(state='normal', text='Close ticked')
            self.status.configure(text='\n'.join(lines[-6:]) or 'Done.', fg=FG)
            self.refresh()
        threading.Thread(target=work, daemon=True).start()


def selftest(out):
    os.makedirs(out, exist_ok=True)
    root = tk.Tk()
    sample = [{'name': 'chrome', 'mb': 2900, 'pids': list(range(14)), 'windowed': True},
              {'name': 'Spotify', 'mb': 1210, 'pids': [1, 2, 3], 'windowed': True},
              {'name': 'Teams', 'mb': 640, 'pids': [4, 5], 'windowed': False},
              {'name': 'OneDrive', 'mb': 120, 'pids': [6], 'windowed': False}]
    w = Window(root, sample)
    root.geometry('480x400+40+40')
    root.update()
    root.after(400, lambda: (save_png(root, os.path.join(out, 'window.png')), root.destroy()))
    root.mainloop()
    print(os.path.join(out, 'window.png'))
    return 0


def save_png(root, path):
    try:
        from PIL import ImageGrab
        x, y = root.winfo_rootx(), root.winfo_rooty()
        ImageGrab.grab((x, y, x + root.winfo_width(), y + root.winfo_height())).save(path)
    except Exception as e:
        print('no picture:', e)


def main():
    if '--selftest' in sys.argv:
        return selftest(sys.argv[sys.argv.index('--selftest') + 1])
    if hg.WIN:
        try:   # one window at a time: a second click brings nothing new up twice
            import ctypes
            ctypes.windll.kernel32.CreateMutexW(None, False, 'Local\\HeavyGateApps')
            if ctypes.windll.kernel32.GetLastError() == 183:
                return 0
            ctypes.windll.shcore.SetProcessDpiAwareness(1)
        except Exception:
            pass
    root = tk.Tk()
    Window(root)
    root.update_idletasks()
    w, h = max(480, root.winfo_reqwidth()), root.winfo_reqheight()
    root.geometry(f'{w}x{h}+{root.winfo_screenwidth() - w - 40}+{root.winfo_screenheight() - h - 120}')
    root.attributes('-topmost', True)
    root.after(800, lambda: root.attributes('-topmost', False))
    root.mainloop()
    return 0


if __name__ == '__main__':
    sys.exit(main())
