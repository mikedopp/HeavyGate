"""HeavyGate's split: one job made of many items (test classes, scenarios, frames) run as pieces on this PC and the
pool workers at the same time.

    heavy split cpu -n "app tests" --items A B C D -- python selftest.py {items}
    heavy split gpu -n "3D suite" --items-from scenarios.txt -- python tools/check.py --only {items}

The command names where the items go: a word that is exactly {items} becomes the piece's items as separate words; a
word with {items} inside gets them joined by commas (--only={items}); with neither, they go on the end. Each machine
gets one piece. How big each piece is comes from how fast each machine did the last run of the same job (seconds per
item, kept in split/<name>.json), so a slow laptop gets fewer items next time. This PC's piece runs in its own lane;
a worker's piece is a pool trip (the project is sent, run in the worker's lane, outputs brought back). A worker that
can't take its piece before it starts hands its items back to this PC.

Who can take a piece: this PC; a worker that answers and has the tools (heavypool.judge); for the gpu lane only a
Windows worker with a graphics card; for a Windows program (.exe .bat .cmd .ps1) only Windows workers. Standard
library only."""
import json
import os
import re
import subprocess
import sys
import threading
import time

PLACE = '{items}'
WIN_ONLY = re.compile(r'\.(exe|bat|cmd|ps1)$', re.I)


def fill(command, items):
    """The command with the piece's items put in (see the module doc)."""
    out, placed = [], False
    for t in command:
        if t == PLACE:
            out += list(items)
            placed = True
        elif PLACE in t:
            out.append(t.replace(PLACE, ','.join(items)))
            placed = True
        else:
            out.append(t)
    return out if placed else out + list(items)


def windows_only(command):
    head = command[0] if command else ''
    return bool(WIN_ONLY.search(head)) or head.lower() in ('pwsh', 'powershell', 'cmd')


def can_take(inf, lane, command, judge_can, desktop=False):
    """(yes, why not) for a worker, from its last answer."""
    if not inf or not inf.get('ok'):
        return False, (inf or {}).get('why') or 'no answer'
    if not judge_can:
        return False, 'missing tools'
    if lane == 'gpu':
        if not inf.get('windows'):
            return False, 'the gpu lane goes only to Windows workers'
        g = (inf.get('gpu') or '').lower()
        if not g or g.startswith('microsoft basic'):
            return False, 'no graphics card'
    if desktop and not inf.get('windows'):
        return False, '--desktop runs only on Windows workers'
    if windows_only(command) and not inf.get('windows'):
        return False, 'a Windows program'
    return True, ''


def history_path(home, name):
    safe = re.sub(r'[^\w.-]+', '_', name).strip('_') or 'split'
    return os.path.join(home, 'split', safe + '.json')


def load_rates(home, name):
    try:
        with open(history_path(home, name), encoding='utf-8') as f:
            return {k: float(v) for k, v in (json.load(f).get('rate') or {}).items() if float(v) > 0}
    except (OSError, ValueError, AttributeError):
        return {}


def save_rates(home, name, rates):
    p = history_path(home, name)
    os.makedirs(os.path.dirname(p), exist_ok=True)
    with open(p, 'w', encoding='utf-8') as f:
        json.dump({'rate': rates, 'updated': time.strftime('%Y-%m-%dT%H:%M:%S')}, f, indent=1)


def assign(items, machines, rates):
    """{machine: [items]}: each item goes where it would finish first, given seconds per item (a machine not seen yet
    counts as the median of those seen, or 1). Items keep their order within a piece; every machine gets at least one
    item while there are enough."""
    known = sorted(rates[m] for m in machines if m in rates)
    guess = known[len(known) // 2] if known else 1.0
    rate = {m: rates.get(m, guess) for m in machines}
    pieces = {m: [] for m in machines}
    for i, it in enumerate(items):
        if i < len(machines):   # one each first, fastest machines first
            m = sorted(machines, key=lambda x: rate[x])[i]
        else:
            m = min(machines, key=lambda x: ((len(pieces[x]) + 1) * rate[x], rate[x]))
        pieces[m].append(it)
    order = {it: i for i, it in enumerate(items)}
    return {m: sorted(v, key=order.get) for m, v in pieces.items() if v}


class Printer:
    """Lines from every piece, each with its machine in front, never torn in half."""

    def __init__(self, out=None):
        self.out, self.lock = out or sys.stdout, threading.Lock()

    def __call__(self, who, line):
        with self.lock:
            self.out.write(f'[{who}] {line}\n')
            self.out.flush()


def run_here(gate_script, lane, name, command, cwd, show):
    """This PC's piece, in its own lane (never sent on: --no-pool --no-cloud)."""
    argv = [sys.executable, gate_script, lane, '-n', name, '--no-pool', '--no-cloud', '--', *command]
    p = subprocess.Popen(argv, cwd=cwd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL)
    for line in iter(p.stdout.readline, b''):
        show(line.decode('utf-8', 'replace').rstrip('\r\n'))
    return p.wait()


def split(hg, lane, name, items, command, only=None, out=None, extra=None, back=None, desktop=False):
    """Runs the pieces; prints a table at the end. Returns the worst exit code (0 when every piece passed)."""
    import heavypool
    home, s = hg.home(), hg.settings()
    pr = Printer(out)
    items = [x for x in items if x]
    if not items:
        pr('split', 'no items')
        return 2
    d = heavypool.load(home)
    workers = {n: w for n, w in d['workers'].items() if only is None or n in only}
    machines, notes = [], {}
    if only is None or 'here' in only:
        machines.append('here')
    infos = heavypool.check(home, s, list(workers)) if workers else {}
    tokens = list(command)
    kind = heavypool.kind_of(tokens)
    for n, w in workers.items():
        can, _, why = heavypool.judge(w, infos.get(n) or {}, s, kind, False, 0)
        ok, why2 = can_take(infos.get(n), lane, tokens, can, desktop)
        if ok:
            machines.append(n)
        else:
            notes[n] = why2 if why2 != 'missing tools' else why
    for n, why in notes.items():
        pr('split', f'{n} sits this one out: {why}')
    if not machines:
        pr('split', 'nobody can take it')
        return 2
    rates = load_rates(home, name)
    pieces = assign(items, machines, rates)
    pr('split', f"{len(items)} items in {len(pieces)} pieces: "
       + ', '.join(f'{m} {len(v)}' for m, v in pieces.items()))
    cwd = os.getcwd()
    results, left = {}, []
    lock = threading.Lock()

    def one(m, its):
        t0 = time.time()
        show = (lambda line, m=m: pr(m, line))
        cmd = fill(tokens, its)
        label = f'{name} [{m}: {len(its)} of {len(items)}]'
        if m == 'here':
            code = run_here(os.path.abspath(hg.__file__), lane, label, cmd, cwd, show)
            res = {'exit': code}
        else:
            w = workers[m]
            try:
                rec = heavypool.trip(home, s, m, w, infos[m], cwd, cmd, kind, label, show, why=f'split: {name}',
                                     lane=lane, stream=show, extra=extra, back=back, desktop=desktop)
            except heavypool.PoolError as e:
                show(f'could not start there ({e}); its {len(its)} items go to this PC')
                with lock:
                    left.extend(its)
                return
            except Exception as e:   # a piece must never take the others down with it
                show(f'failed: {e!r}')
                rec = {'exit': 1}
            hg._log(rec)
            res = {'exit': rec.get('exit', 1), 'send_s': rec.get('send_s')}
        res.update(items=its, secs=round(time.time() - t0, 1))
        with lock:
            results[m] = res

    threads = [threading.Thread(target=one, args=(m, v), daemon=True) for m, v in pieces.items()]
    t0 = time.time()
    for t in threads:
        t.start()
    try:
        for t in threads:
            while t.is_alive():
                t.join(0.5)
    except KeyboardInterrupt:
        pr('split', 'stopping (the workers stop their pieces when ssh closes)')
        return 130
    if left:   # what a worker could not take runs here after this PC's own piece
        t1 = time.time()
        code = run_here(os.path.abspath(hg.__file__), lane, f'{name} [here: {len(left)} handed back]',
                        fill(tokens, left), cwd, lambda line: pr('here+', line))
        results['here+'] = {'exit': code, 'items': left, 'secs': round(time.time() - t1, 1)}
    wall = time.time() - t0
    for m, r in results.items():   # seconds per item, for the next run's piece sizes
        base = m.rstrip('+')
        if r['exit'] == 0 and r['items']:
            new = r['secs'] / len(r['items'])
            rates[base] = round(new if base not in rates else 0.5 * rates[base] + 0.5 * new, 3)
    if all(r['exit'] == 0 for r in results.values()):
        save_rates(home, name, rates)
    pr('split', f'done in {wall:.0f}s (the pieces together: {sum(r["secs"] for r in results.values()):.0f}s)')
    for m, r in results.items():
        pr('split', f"  {m:10s} {len(r['items']):4d} items  {r['secs']:7.1f}s  "
           + ('passed' if r['exit'] == 0 else f"FAILED (exit {r['exit']}): " + ' '.join(r['items'][:8])
              + (' ...' if len(r['items']) > 8 else '')))
    bad = [r['exit'] for r in results.values() if r['exit'] != 0]
    return bad[0] if bad else 0
