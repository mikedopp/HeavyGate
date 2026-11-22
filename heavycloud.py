"""HeavyGate's cloud lane: GitHub Actions as a third place to run a build, with the quota watched.

    heavy cloud                  plan, minutes used and left this month, charges, running and recent runs
    heavy cloud runs [-n 12]     recent runs across every repo that has a workflow
    heavy cloud advise ...       where a job should run, local or GitHub, and what it would cost
    heavy cloud dispatch ...     start a workflow on GitHub (refuses when the advice says keep it local)
    heavy cloud prune / keep     delete old artifacts now / how old they may get (cloud_keep_days, default 3)

Artifact storage is watched too: GitHub bills it in GB-hours against the plan's included GB for every hour of the month,
and a build that would upload into a used-up month stays local. A build brought home is deleted on GitHub once its
files are here and its SHA256SUMS match.

Everything is read with the `gh` command line (signed in as you); nothing but your own account is queried. Minutes come
from GitHub's billing usage report, which needs the `user` scope (gh auth refresh -h github.com -s user). A run's
own "billable" timing is not used: it reports 0 for private repos. Standard runners count Linux 1x, Windows 2x and
macOS 10x against the free minutes; the report lists raw rounded-up minutes per job (checked against real runs), so
the multiplier is applied here. Standard runners have no GPU: a GPU job is never sent.

The state lives in cloud.json next to runs.jsonl. Readers (status, the tray) only read that file; refresh() is the only
thing that calls gh, and it skips anything fetched recently. Python standard library only."""
import json
import math
import os
import re
import shlex
import shutil
import subprocess
import sys
import threading
import time
from datetime import datetime, timezone

NO_WINDOW = 0x08000000
MULT = {'linux': 1, 'windows': 2, 'macos': 10}
PLAN_MIN = {'free': 2000, 'pro': 3000, 'team': 3000, 'enterprise': 50000}   # GitHub's published included minutes
PLAN_GB = {'free': 0.5, 'pro': 1.0, 'team': 2.0, 'enterprise': 50.0}         # ...and artifact + package storage
GIB = 1024 ** 3   # the billing report's GB (checked: 432 MiB of artifacts bill 10.1 GB-hours a day)
ACTIVE = {'queued', 'in_progress', 'waiting', 'requested', 'pending'}
SCOPE_FIX = 'gh auth refresh -h github.com -s user'
DEFAULTS = {
    'cloud': True,                      # read GitHub Actions at all
    'cloud_quota_min': 0,               # monthly included minutes; 0 = by plan (free 2,000, pro 3,000)
    'cloud_reserve_pct': 20,            # kept back for releases: a job that would dip into it stays local
    'cloud_usage_s': 600,               # billing usage is refetched this often
    'cloud_runs_s': 300,                # every repo's recent runs
    'cloud_active_s': 20,               # a repo with a run in flight is rechecked this often
    'cloud_wait_s': 180,                # the local lane is this far behind: a job may go to the cloud
    'cloud_long_min': 10,               # a job that takes this long here is worth offloading while the quota is healthy
    'cloud_long_share': 0.25,           # ...if it costs no more than this share of the usable minutes
    'cloud_repos': [],                  # extra owner/repo names to watch
    'cloud_drop_root': '',              # folder of projects to scan for .github/workflows ('' = beside this tool)
    'cloud_auto': False,                # heavy auto / heavy cpu may send a mapped build to GitHub (heavy cloud auto on)
    'cloud_auto_day_min': 60,           # quota minutes auto mode may spend per day (local date)...
    'cloud_auto_month_min': 400,        # ...and per month; past either, builds stay here
    'cloud_est_min': 10,                # GitHub minutes assumed for a workflow with no past runs
    'cloud_poll_s': 15,                 # how often a build being waited on is checked
    'cloud_done_show_s': 120,           # how long the tray keeps a finished GitHub build (and its result) on the strip
    'cloud_max_min': 60,                # a build still running after this is cancelled
    'cloud_storage_gb': 0,              # artifact storage included all month; 0 = by plan (free 0.5 GB)
    'cloud_keep_days': 3,               # artifacts older than this are deleted from your repos; 0 = never
    'cloud_artifacts_s': 3600,          # the artifact list (and that clean-up) is read this often
    'cloud_delete_after': True,         # a build's artifact is deleted on GitHub once its files are here and checked
}


def utcnow(now=None):
    return datetime.fromtimestamp(time.time() if now is None else now, timezone.utc)


def month_of(now=None):
    d = utcnow(now)
    return d.year, d.month


def month_key(y, m):
    return f'{y:04d}-{m:02d}'


def prev_month(y, m):
    return (y - 1, 12) if m == 1 else (y, m - 1)


def parse_time(s):
    try:
        return datetime.fromisoformat(s.replace('Z', '+00:00')).timestamp()
    except (AttributeError, ValueError):
        return None


def counted(mins):
    """Minutes as the free allowance counts them: Linux 1x, Windows 2x, macOS 10x; anything else (larger runners) raw."""
    return sum(mins.get(k, 0) * MULT.get(k, 1) for k in mins)


def parse_usage(items):
    """The billing report's usageItems -> minutes per runner OS, per repo, storage and money. Pure."""
    mins = {'linux': 0.0, 'windows': 0.0, 'macos': 0.0, 'other': 0.0}
    by_repo, store_days, store_repos = {}, {}, set()
    net = gross = 0.0
    for i in items or []:
        if i.get('product') != 'actions':
            continue
        net += i.get('netAmount') or 0
        gross += i.get('grossAmount') or 0
        sku, q, repo = (i.get('sku') or '').lower(), i.get('quantity') or 0, i.get('repositoryName') or '?'
        if i.get('unitType') == 'Minutes':
            larger = bool(re.search(r'\d+-core|arm|gpu|larger', sku))
            key = 'other' if larger else 'linux' if 'linux' in sku else 'windows' if 'windows' in sku else \
                'macos' if 'macos' in sku else 'other'
            mins[key] += q
            by_repo[repo] = by_repo.get(repo, 0) + q * MULT.get(key, 1)
        elif 'storage' in sku:
            day = (i.get('date') or '')[:10]
            store_days[day] = store_days.get(day, 0) + q
            if q and repo != '?':
                store_repos.add(repo)
    last = store_days[max(store_days)] if store_days else 0.0
    return {'minutes': {k: round(v, 2) for k, v in mins.items()}, 'counted': round(counted(mins), 2),
            'by_repo': {k: round(v, 2) for k, v in sorted(by_repo.items(), key=lambda kv: -kv[1])},
            'storage_gb': round(last / 24, 3), 'storage_gbh': round(sum(store_days.values()), 2),
            'storage_repos': sorted(store_repos),
            'net_usd': round(net, 4), 'gross_usd': round(gross, 4)}


def remote_repo(git_config_text):
    """owner/repo from the origin url in a .git/config, or None."""
    m = re.search(r'\[remote "origin"\][^\[]*?url\s*=\s*(\S+)', git_config_text or '', re.S)
    if not m:
        return None
    m = re.search(r'github\.com[:/]+([^/\s]+)/([^/\s]+?)(?:\.git)?$', m.group(1))
    return f'{m.group(1)}/{m.group(2)}' if m else None


def remote_repos(git_config_text):
    """{remote name: owner/repo} for every GitHub remote in a .git/config (origin and any other, e.g. a private fork
    kept as `mine` beside an upstream origin)."""
    out = {}
    for name, body in re.findall(r'\[remote "([^"]+)"\]([^\[]*)', git_config_text or ''):
        u = re.search(r'url\s*=\s*(\S+)', body)
        m = re.search(r'github\.com[:/]+([^/\s]+)/([^/\s]+?)(?:\.git)?$', u.group(1)) if u else None
        if m:
            out[name] = f'{m.group(1)}/{m.group(2)}'
    return out


def remote_for(top, repo):
    """The name of the checkout's remote that points at repo (origin first), or None."""
    try:
        with open(os.path.join(top, '.git', 'config'), encoding='utf-8', errors='replace') as f:
            rs = remote_repos(f.read())
    except OSError:
        return None
    for name in sorted(rs, key=lambda n: n != 'origin'):
        if rs[name].lower() == repo.lower():
            return name
    return None


def scan_workflows(root, owner):
    """{owner/repo: [{'file', 'name', 'dispatch'}]} for every project folder under root that has workflows and whose
    origin is one of the owner's repos. Upstream clones (someone else's origin) are skipped."""
    out = {}
    try:
        names = sorted(os.listdir(root))
    except OSError:
        return out
    for n in names:
        d = os.path.join(root, n)
        wf = os.path.join(d, '.github', 'workflows')
        if not os.path.isdir(wf):
            continue
        try:
            with open(os.path.join(d, '.git', 'config'), encoding='utf-8', errors='replace') as f:
                repo = remote_repo(f.read())
        except OSError:
            continue
        if not repo or repo.split('/')[0].lower() != (owner or '').lower():
            continue
        rows = []
        for fn in sorted(os.listdir(wf)):
            if not fn.lower().endswith(('.yml', '.yaml')):
                continue
            try:
                with open(os.path.join(wf, fn), encoding='utf-8', errors='replace') as f:
                    text = f.read()
            except OSError:
                continue
            m = re.search(r'^name:\s*["\']?(.+?)["\']?\s*$', text, re.M)
            rows.append({'file': fn, 'name': m.group(1) if m else fn,
                         'dispatch': bool(re.search(r'^\s*workflow_dispatch\b', text, re.M))})
        if rows:
            out[repo] = rows
    return out


def level_of(used, quota, reserve, net=0.0):
    """ok, watch (60% of the usable minutes gone), low (into the reserve), over (all gone, or being charged)."""
    if net > 0 or used >= quota:
        return 'over'
    if used >= quota - reserve:
        return 'low'
    return 'watch' if used >= 0.6 * (quota - reserve) else 'ok'


def summarize(state, cfg, now=None):
    """What status, the tray and the router read: the cached state turned into plain numbers. Pure and cheap."""
    now = time.time() if now is None else now
    st = (state or {}).get('state') or 'unknown'
    out = {'state': st, 'error': (state or {}).get('error'), 'fix': (state or {}).get('fix'),
           'age_s': int(now - state['fetched']) if state and state.get('fetched') else None,
           'login': (state or {}).get('login'), 'active': [], 'recent': []}
    for repo, r in sorted(((state or {}).get('repos') or {}).items()):
        for run in r.get('runs') or []:
            row = dict(run, repo=repo)
            if run.get('status') in ACTIVE:
                t0 = parse_time(run.get('startedAt') or run.get('createdAt'))
                row['for_s'] = max(int(now - t0), 0) if t0 else None
                out['active'].append(row)
            else:
                out['recent'].append(row)
    out['recent'].sort(key=lambda r: r.get('updatedAt') or '', reverse=True)
    use = (state or {}).get('usage')
    if not use:
        return out
    plan = ((state or {}).get('plan') or '').lower()
    quota = int(cfg.get('cloud_quota_min') or 0) or PLAN_MIN.get(plan) or 2000
    reserve = round(quota * cfg.get('cloud_reserve_pct', 20) / 100)
    used = use['counted']
    y, m = month_of(now)
    d = utcnow(now)
    days_in = (datetime(y + (m == 12), m % 12 + 1, 1, tzinfo=timezone.utc) - datetime(y, m, 1, tzinfo=timezone.utc)).days
    elapsed = max((now - datetime(y, m, 1, tzinfo=timezone.utc).timestamp()) / 86400, 1.0)
    out.update({'plan': plan or None, 'quota_min': quota, 'quota_assumed': not (plan in PLAN_MIN or cfg.get('cloud_quota_min')),
                'month': month_key(y, m), 'used_min': used, 'left_min': round(max(quota - used, 0), 1),
                'reserve_min': reserve, 'usable_min': round(max(quota - reserve - used, 0), 1),
                'pace_min': round(used / elapsed * days_in), 'days_left': days_in - d.day,
                'resets': (datetime(y + (m == 12), m % 12 + 1, 1)).strftime('%Y-%m-%d'),
                'minutes': use['minutes'], 'by_repo': use['by_repo'], 'storage_gb': use['storage_gb'],
                'net_usd': use['net_usd'], 'gross_usd': use['gross_usd'],
                'prev_used_min': (state.get('prev') or {}).get('counted'),
                'level': level_of(used, quota, reserve, use['net_usd'])})
    out['storage'] = storage_view(state, cfg, now, plan, use, days_in)
    return out


def storage_view(state, cfg, now, plan, use, days_in):
    """Artifact storage this month. GitHub bills it in GB-hours and includes the plan's GB for every hour of the month
    (free: 0.5 GB x 720 h = 360 GB-hours in a 30-day month). Past that, with no budget set, it refuses new uploads
    until the 1st (people report "Artifact storage quota has been hit"; a build's upload step then fails). The used
    figure is the billing report's, which trails by up to a day; what is stored now comes from the artifact list (or,
    before that is read, the report's last day). The projection assumes what is stored now stays to the month's end."""
    y, m = month_of(now)
    end = datetime(y + (m == 12), m % 12 + 1, 1, tzinfo=timezone.utc).timestamp()
    incl_gb = float(cfg.get('cloud_storage_gb') or 0) or PLAN_GB.get(plan) or 0.5
    incl = incl_gb * days_in * 24
    used = float(use.get('storage_gbh') or 0)
    arts = (state or {}).get('artifacts')
    if arts is not None:
        now_gb = sum(a.get('bytes') or 0 for rows in arts.values() for a in rows) / GIB
        count = sum(len(rows) for rows in arts.values())
    else:
        now_gb, count = float(use.get('storage_gb') or 0), None
    hours_left = max((end - now) / 3600, 0)
    proj = used + now_gb * hours_left
    runs_out = None
    if used < incl < proj and now_gb > 0:
        runs_out = datetime.fromtimestamp(now + (incl - used) / now_gb * 3600).strftime('%Y-%m-%d')
    return {'incl_gb': incl_gb, 'incl_gbh': round(incl, 1), 'used_gbh': round(used, 1), 'now_gb': round(now_gb, 3),
            'count': count, 'proj_gbh': round(proj, 1), 'runs_out': runs_out,
            'level': 'over' if used >= incl else 'low' if proj >= incl else 'ok',
            'keep_days': cfg.get('cloud_keep_days', 3), 'pruned': (state or {}).get('pruned_last'),
            'delete_after': bool(cfg.get('cloud_delete_after', True)),
            'by_repo': {r: round(sum(a.get('bytes') or 0 for a in rows) / 2 ** 20) for r, rows in (arts or {}).items()}}


def decide(cfg, summ, job):
    """Where a job should run. Pure: job = {'gpu', 'os' (linux|windows|macos), 'est_min' (minutes on GitHub),
    'local_min' (minutes here, optional), 'need_mb', 'free_mb' (None = unknown), 'reserve_mb', 'wait_s' (how far behind
    the local lane is), 'disk_low' (why this PC's disk is too full to build on, or None), 'uploads' (it brings files back
    as an artifact; default True)}. Returns {'where': local|cloud, 'rule', 'reason', 'cost_min', 'left_after'}. Local
    is free and skips the upload, so it wins unless a rule says otherwise."""
    cost = math.ceil(job.get('est_min') or 0) * MULT.get(job.get('os', 'linux'), 1)

    def out(where, rule, why):
        left = summ.get('left_min')
        return {'where': where, 'rule': rule, 'reason': why, 'cost_min': cost,
                'left_after': None if left is None else round(left - (cost if where == 'cloud' else 0), 1)}

    if job.get('gpu'):
        return out('local', 'gpu', 'GitHub\'s standard runners have no GPU, so it stays in the gpu lane.')
    if summ.get('state') != 'ok':
        return out('local', 'unavailable', f"The cloud lane isn't readable ({summ.get('state')}"
                   + (f": {summ.get('fix')}" if summ.get('fix') else '') + ').')
    if summ.get('net_usd', 0) > 0:
        return out('local', 'charged', f"GitHub already charged ${summ['net_usd']:.2f} this month: nothing more goes there.")
    st = summ.get('storage') or {}
    if job.get('uploads', True) and st.get('level') == 'over':
        return out('local', 'storage', f"This month's artifact storage is used up ({fmt_min(st['used_gbh'])} of "
                   f"{fmt_min(st['incl_gbh'])} GB-hours): GitHub may refuse the upload until {summ.get('resets')}.")
    if cost > summ.get('usable_min', 0):
        return out('local', 'quota', f"It would count {cost} min and only {fmt_min(summ.get('usable_min', 0))} are free "
                   f"above the {fmt_min(summ.get('reserve_min') or 0)}-minute reserve.")
    if job.get('disk_low'):
        return out('cloud', 'disk', job['disk_low'])
    free, need = job.get('free_mb'), (job.get('need_mb') or 0) + (job.get('reserve_mb') or 0)
    if free is not None and need and free < need:
        return out('cloud', 'ram', f"This PC has {free / 1024:.1f} GB free and the job needs {need / 1024:.1f} GB.")
    if (job.get('wait_s') or 0) >= cfg.get('cloud_wait_s', 180):
        return out('cloud', 'queue', f"The local lane is about {round(job['wait_s'] / 60)} min behind.")
    long_here = job.get('local_min') or 0
    if long_here >= cfg.get('cloud_long_min', 10) and cost <= summ.get('usable_min', 0) * cfg.get('cloud_long_share', 0.25):
        return out('cloud', 'long', f"It takes about {round(long_here)} min here and the quota is healthy "
                   f"({fmt_min(summ.get('usable_min', 0))} usable min left).")
    return out('local', 'free', 'RAM is fine and the lane is clear. Local is free and skips the upload.')


def gh_command():
    """The gh command as a list: HEAVYGATE_GH (parts joined with |, for tests), else gh on the PATH, else None."""
    env = os.environ.get('HEAVYGATE_GH')
    if env:
        return env.split('|')
    p = shutil.which('gh')
    return [p] if p else None


def classify(text):
    t = (text or '').lower()
    if 'needs the "user" scope' in t or 'needs the \\"user\\" scope' in t or 'user scope' in t:
        return 'no_scope'
    if 'gh auth login' in t or 'not logged in' in t or 'authentication' in t or 'http 401' in t:
        return 'no_auth'
    if 'rate limit' in t:
        return 'rate'
    return 'error'


class Cloud:
    """Reads and refreshes the cached state. home = HeavyGate's state folder."""

    def __init__(self, home, cfg=None, drop_root=None, now=None):
        self.home, self.cfg = home, dict(DEFAULTS, **(cfg or {}))
        self.drop_root = drop_root or self.cfg.get('cloud_drop_root') or \
            os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        self.now = now or time.time
        self.path = os.path.join(home, 'cloud.json')
        self.lock = threading.Lock()

    def load(self):
        try:
            with open(self.path, encoding='utf-8') as f:
                return json.load(f)
        except (OSError, ValueError):
            return {}

    def save(self, state):
        os.makedirs(self.home, exist_ok=True)
        tmp = f'{self.path}.{os.getpid()}.{threading.get_ident()}.tmp'
        with open(tmp, 'w', encoding='utf-8') as f:
            json.dump(state, f, indent=1)
        os.replace(tmp, self.path)

    def summary(self):
        s = summarize(self.load(), self.cfg, self.now())
        sp = spent(self.home, self.now())
        s['auto'] = {'on': bool(self.cfg.get('cloud_auto')), 'day_min': round(sp['day'], 1),
                     'day_cap': self.cfg['cloud_auto_day_min'], 'month_min': round(sp['month'], 1),
                     'month_cap': self.cfg['cloud_auto_month_min'], 'maps': len(read_jobs(self.home))}
        return s

    def gh(self, args, timeout=30):
        """(stdout, None) or (None, kind): kind is no_gh, no_auth, no_scope, rate or error."""
        cmd = gh_command()
        if not cmd:
            return None, 'no_gh', 'gh is not installed'
        env = dict(os.environ, GH_PROMPT_DISABLED='1', NO_COLOR='1', GH_NO_UPDATE_NOTIFIER='1')
        try:
            p = subprocess.run(cmd + list(args), capture_output=True, text=True, timeout=timeout, env=env,
                               encoding='utf-8', errors='replace',
                               creationflags=NO_WINDOW if os.name == 'nt' else 0)
        except (OSError, subprocess.TimeoutExpired) as e:
            return None, 'error', str(e)[:200]
        if p.returncode != 0:
            msg = (p.stderr or p.stdout or '').strip()
            return None, classify(msg), msg[:300]
        return p.stdout, None, ''

    def gh_json(self, args, timeout=30):
        out, kind, msg = self.gh(args, timeout)
        if kind:
            return None, kind, msg
        try:
            return json.loads(out), None, ''
        except ValueError:
            return None, 'error', 'gh printed something that is not JSON'

    def refresh(self, force=False):
        """Fetch what is stale (everything with force) and save. Returns the new state; never raises."""
        if not self.cfg.get('cloud'):
            return self.load()
        with self.lock:
            try:
                return self._refresh(force)
            except Exception as e:   # a monitor must not die on one bad answer
                s = self.load()
                s.update({'state': 'error', 'error': f'{type(e).__name__}: {e}'[:200], 'fetched': self.now()})
                self.save(s)
                return s

    def _refresh(self, force):
        now = self.now()
        s = self.load()
        c = self.cfg
        if s.get('state') and s.get('state') != 'ok' and not force and now - s.get('fetched', 0) < 120:
            return s   # a failure (often one a person must fix): look again in a couple of minutes, not every 5 s
        if force or now - s.get('usage_fetched', 0) >= c['cloud_usage_s'] or s.get('state') != 'ok':
            who, kind, msg = self.gh_json(['api', 'user'])
            if kind:
                s.update({'state': kind, 'error': msg, 'fix': SCOPE_FIX if kind == 'no_scope' else
                          'gh auth login' if kind == 'no_auth' else None, 'fetched': now})
                self.save(s)
                return s
            login = who.get('login')
            plan = ((who.get('plan') or {}).get('name')) or s.get('plan')
            y, m = month_of(now)
            use, kind, msg = self.gh_json(['api', f'users/{login}/settings/billing/usage?year={y}&month={m}'])
            if kind:
                s.update({'state': kind, 'error': msg, 'login': login, 'plan': plan, 'fetched': now,
                          'fix': SCOPE_FIX if kind in ('no_scope', 'error') and '404' in msg else None})
                if '404' in msg:
                    s['state'] = 'no_scope'
                self.save(s)
                return s
            s.update({'state': 'ok', 'error': None, 'fix': None, 'login': login, 'plan': plan,
                      'usage': parse_usage(use.get('usageItems')), 'usage_fetched': now, 'month': month_key(y, m)})
            py, pm = prev_month(y, m)
            if force or s.get('prev_month') != month_key(py, pm):
                prev, kind2, _ = self.gh_json(['api', f'users/{login}/settings/billing/usage?year={py}&month={pm}'])
                if not kind2:
                    s['prev'] = parse_usage(prev.get('usageItems'))
                    s['prev_month'] = month_key(py, pm)
            fetched = True
        else:
            fetched = False
        if s.get('state') == 'ok':
            fetched = self._runs(s, force, now) or fetched
            fetched = self._artifacts(s, force, now) or fetched
        if fetched:   # nothing was due: leave the file and its time alone (the tray asks every few seconds)
            s['fetched'] = now
            self.save(s)
        return s

    def _runs(self, s, force, now):
        """Rechecks the repos that are due. True when anything was read."""
        c, did = self.cfg, False
        if force or now - s.get('workflows_fetched', 0) >= 86400 or 'workflows' not in s:
            s['workflows'] = scan_workflows(self.drop_root, s.get('login'))
            s['workflows_fetched'] = now
            did = True
        repos = self.watched(s)
        s.setdefault('repos', {})
        for repo in sorted(repos):
            cur = s['repos'].setdefault(repo, {})
            active = any(r.get('status') in ACTIVE for r in cur.get('runs') or [])
            if not force and now - cur.get('checked', 0) < (c['cloud_active_s'] if active else c['cloud_runs_s']):
                continue
            runs, kind, msg = self.gh_json(['run', 'list', '-R', repo, '--limit', '6', '--json',
                                            'databaseId,status,conclusion,workflowName,displayTitle,createdAt,'
                                            'updatedAt,startedAt,event,headBranch'])
            cur['checked'] = now
            did = True
            if kind:
                cur['error'] = msg[:120]
                if kind == 'rate':
                    break
                continue
            cur['error'] = None
            cur['runs'] = runs
        for repo in list(s['repos']):
            if repo not in repos:
                del s['repos'][repo]
                did = True
        return did

    def watched(self, s):
        """Every repo the lane looks at: yours with workflows here, cloud_repos, mapped builds, and any repo the billing
        report charged minutes or storage to."""
        repos = set(s.get('workflows') or {}) | set(self.cfg.get('cloud_repos') or []) | \
            {j['repo'] for j in read_jobs(self.home)}
        for blob in (s.get('usage'), s.get('prev')):
            for r in (blob or {}).get('by_repo', {}):
                if '/' not in r and s.get('login'):
                    repos.add(f"{s['login']}/{r}")
        for r in (s.get('usage') or {}).get('storage_repos') or []:
            if s.get('login'):
                repos.add(f"{s['login']}/{r}")
        return repos

    def list_artifacts(self, repo):
        """([{'id', 'name', 'bytes', 'created', 'expires', 'run_id'}], kind, msg) for the artifacts not yet expired."""
        rows, page = [], 1
        while page <= 10:
            data, kind, msg = self.gh_json(['api', f'repos/{repo}/actions/artifacts?per_page=100&page={page}'])
            if kind:
                return None, kind, msg
            got = data.get('artifacts') or []
            for a in got:
                if not a.get('expired'):
                    rows.append({'id': a.get('id'), 'name': a.get('name'), 'bytes': a.get('size_in_bytes') or 0,
                                 'created': a.get('created_at'), 'expires': a.get('expires_at'),
                                 'run_id': (a.get('workflow_run') or {}).get('id')})
            if len(got) < 100:
                break
            page += 1
        return rows, None, ''

    def delete_artifact(self, repo, art_id):
        _, kind, msg = self.gh(['api', '-X', 'DELETE', f'repos/{repo}/actions/artifacts/{art_id}'])
        return kind is None, msg

    def sweep(self, s, days, dry_run=False, now=None):
        """Lists every watched repo's artifacts into s['artifacts'] and deletes those older than days (None or 0 = keep
        all). Returns the rows deleted (or that would be, with dry_run), each with its repo and age."""
        now = self.now() if now is None else now
        arts, gone, old = {}, [], s.get('artifacts') or {}
        for repo in sorted(self.watched(s)):
            rows, kind, msg = self.list_artifacts(repo)
            if kind:
                if repo in old:
                    arts[repo] = old[repo]   # unread this time: keep what was known
                if kind == 'rate':
                    break
                continue
            keep = []
            for a in rows:
                t = parse_time(a.get('created') or '')
                age = (now - t) / 86400 if t else 0
                if days and age >= days:
                    row = dict(a, repo=repo, age_days=round(age, 1))
                    if dry_run:
                        gone.append(row)
                        keep.append(a)
                        continue
                    ok, why = self.delete_artifact(repo, a['id'])
                    if ok:
                        gone.append(row)
                        continue
                    a = dict(a, delete_error=why[:120])
                keep.append(a)
            if keep:
                arts[repo] = keep
        s['artifacts'] = arts
        if gone and not dry_run:
            s['pruned_last'] = {'time': datetime.fromtimestamp(now).isoformat(timespec='seconds'), 'count': len(gone),
                                'mb': round(sum(g['bytes'] for g in gone) / 2 ** 20), 'days': round(days, 2)}
            for g in gone:
                log_cloud(self.home, {'time': s['pruned_last']['time'], 'action': 'prune', 'repo': g['repo'],
                                      'artifact': g['name'], 'id': g['id'], 'mb': round(g['bytes'] / 2 ** 20, 1),
                                      'age_days': g['age_days'], 'keep_days': round(days, 2)})
        return gone

    def _artifacts(self, s, force, now):
        """The artifact list, and the clean-up past cloud_keep_days, once every cloud_artifacts_s."""
        if not force and now - s.get('artifacts_fetched', 0) < self.cfg['cloud_artifacts_s']:
            return False
        self.sweep(s, float(self.cfg.get('cloud_keep_days') or 0), now=now)
        s['artifacts_fetched'] = now
        return True

    def prune(self, days, dry_run=False):
        """heavy cloud prune: the sweep now, with any age limit (0 = every artifact)."""
        with self.lock:
            s = self.load()
            gone = self.sweep(s, days if days else 1e-9, dry_run)
            s['artifacts_fetched'] = self.now()
            self.save(s)
            return gone, s

    def delete_run_artifacts(self, repo, run_id, name=None):
        """Deletes a finished run's artifacts (only the one called name, when given). Returns (deleted, failed)."""
        data, kind, _ = self.gh_json(['api', f'repos/{repo}/actions/runs/{run_id}/artifacts'])
        if kind:
            return 0, 1
        n = bad = 0
        for a in data.get('artifacts') or []:
            if name and a.get('name') != name:
                continue
            ok, _ = self.delete_artifact(repo, a.get('id'))
            n, bad = n + ok, bad + (not ok)
        return n, bad

    def rescan_workflows(self):
        """Reads the projects' workflow files again now (normally once a day), e.g. when a new one was just added."""
        with self.lock:
            s = self.load()
            s['workflows'] = scan_workflows(self.drop_root, s.get('login'))
            s['workflows_fetched'] = self.now()
            self.save(s)
            return s

    def dispatch(self, repo, workflow, ref=None):
        """gh workflow run. Returns (ok, message)."""
        args = ['workflow', 'run', workflow, '-R', repo] + (['--ref', ref] if ref else [])
        out, kind, msg = self.gh(args, timeout=60)
        return (kind is None), (out.strip() if kind is None else msg)


class Refresher(threading.Thread):
    """Keeps the cache fresh from a long-lived process (the tray). refresh() skips anything fetched recently, so a
    short sleep here costs nothing; a failure is reported once through note() and never stops the loop."""

    def __init__(self, cloud, note=None, every=5.0):
        super().__init__(daemon=True, name='heavygate-cloud')
        self.cloud, self.note, self.every = cloud, note or (lambda *_: None), every
        self.stop = threading.Event()
        self.last_err = None

    def run(self):
        while not self.stop.is_set():
            try:
                s = self.cloud.refresh()
                err = s.get('error') if s.get('state') != 'ok' else None
                if err != self.last_err:
                    self.last_err = err
                    if err:
                        self.note(f"cloud lane: {s.get('state')}: {err}")
            except Exception as e:
                self.note(f'cloud lane refresher: {type(e).__name__}: {e}')
            self.stop.wait(self.every)


# builds mapped to workflows, their learned cost, and the round trip -----------------------------------------------------
JOB_KEYS = ('match', 'folder', 'repo', 'workflow', 'os', 'ref', 'artifact', 'dest', 'max_min')


def jobs_path(home):
    return os.path.join(home, 'cloud-jobs.json')


def runs_log_path(home):
    return os.path.join(home, 'cloud-runs.jsonl')


def clean_job(j):
    """A mapping with only the keys it may have, or None when it can't work (no match, repo or workflow)."""
    if not isinstance(j, dict):
        return None
    j = {k: str(j[k]).strip() for k in JOB_KEYS if j.get(k) not in (None, '')}
    if not (j.get('match') and re.match(r'^[\w.-]+/[\w.-]+$', j.get('repo', '')) and j.get('workflow')):
        return None
    j['os'] = j.get('os', 'linux').lower() if j.get('os', 'linux').lower() in MULT else 'linux'
    j.setdefault('dest', 'dist/cloud')
    try:
        if j.get('max_min') is not None:
            j['max_min'] = float(j['max_min']) if float(j['max_min']) > 0 else None
    except ValueError:
        j['max_min'] = None
    if j.get('max_min') is None:
        j.pop('max_min', None)
    return j


def read_jobs(home):
    try:
        with open(jobs_path(home), encoding='utf-8') as f:
            data = json.load(f)
    except (OSError, ValueError):
        return []
    return [c for c in map(clean_job, data.get('jobs') or []) if c]


def save_jobs(home, jobs):
    os.makedirs(home, exist_ok=True)
    tmp = f'{jobs_path(home)}.{os.getpid()}.tmp'
    with open(tmp, 'w', encoding='utf-8') as f:
        json.dump({'jobs': [c for c in map(clean_job, jobs) if c]}, f, indent=2)
    os.replace(tmp, jobs_path(home))


def find_job(jobs, command, cwd, matches):
    """(index, mapping) of the first mapping whose match and folder fit, or (None, None). matches is heavygate's
    rule_matches, so a mapping reads the same way as a lane rule: whole words anywhere, folder anywhere in the path."""
    for i, j in enumerate(jobs):
        if matches({'match': j['match'], 'folder': j.get('folder', '')}, command, cwd):
            return i, j
    return None, None


def read_runs_log(home):
    rows = []
    try:
        with open(runs_log_path(home), encoding='utf-8') as f:
            for line in f:
                try:
                    r = json.loads(line)
                except ValueError:
                    continue
                if isinstance(r, dict):
                    rows.append(r)
    except OSError:
        pass
    return rows


def log_run(home, rec):
    os.makedirs(home, exist_ok=True)
    with open(runs_log_path(home), 'a', encoding='utf-8') as f:
        f.write(json.dumps(rec) + '\n')


def learned_est(home, state, repo, workflow, default=10):
    """(GitHub minutes before the OS multiplier, where the figure came from). The median of the last five good runs this
    tool followed; else of the workflow's recent good runs in the cached run list (start to finish, rounded up); else
    the default."""
    mine = [r['raw_min'] for r in read_runs_log(home) if r.get('repo') == repo and r.get('workflow') == workflow
            and r.get('conclusion') == 'success' and r.get('raw_min')][-5:]
    if mine:
        mine.sort()
        return mine[len(mine) // 2], f'median of {len(mine)} past run{"s" if len(mine) > 1 else ""}'
    name = next((w['name'] for w in ((state or {}).get('workflows') or {}).get(repo, []) if w['file'] == workflow),
                workflow)
    seen = []
    for r in (((state or {}).get('repos') or {}).get(repo) or {}).get('runs') or []:
        t0, t1 = parse_time(r.get('startedAt') or r.get('createdAt')), parse_time(r.get('updatedAt'))
        if r.get('conclusion') == 'success' and r.get('workflowName') in (name, workflow) and t0 and t1 and t1 > t0:
            seen.append(math.ceil((t1 - t0) / 60))
    if seen:
        seen = sorted(seen[:5])
        return seen[len(seen) // 2], f'from {len(seen)} recent run{"s" if len(seen) > 1 else ""} on GitHub'
    return default, 'a guess: no past runs'


def spent(home, now=None):
    """Quota minutes auto mode has spent: {'day' (local date), 'month' (UTC month, as GitHub bills)}."""
    now = time.time() if now is None else now
    day, (y, m) = time.strftime('%Y-%m-%d', time.localtime(now)), month_of(now)
    out = {'day': 0.0, 'month': 0.0}
    for r in read_runs_log(home):
        if not r.get('auto') or not r.get('cost_min'):
            continue
        t = parse_time(r.get('dispatched_utc') or '')
        if t is None:
            continue
        if time.strftime('%Y-%m-%d', time.localtime(t)) == day:
            out['day'] += r['cost_min']
        if month_of(t) == (y, m):
            out['month'] += r['cost_min']
    return out


def git(args, cwd, timeout=60):
    try:
        env = dict(os.environ, GIT_TERMINAL_PROMPT='0', GCM_INTERACTIVE='never')   # never wait on a sign-in prompt
        p = subprocess.run(['git', *args], cwd=cwd, capture_output=True, text=True, timeout=timeout, env=env,
                           encoding='utf-8', errors='replace', creationflags=NO_WINDOW if os.name == 'nt' else 0)
        return p.returncode, (p.stdout or '').strip(), (p.stderr or '').strip()
    except (OSError, subprocess.TimeoutExpired) as e:
        return 1, '', str(e)


def find_checkout(places, repo):
    """The folder of a checkout with a remote (origin or any other) that is repo: the first place itself (or a folder
    above it), else a project folder directly under the second. Reads .git/config only; None when there is none."""
    def origin(d):
        return repo.lower() if remote_for(d, repo) else ''
    here = os.path.abspath(places[0])
    while True:
        if origin(here) == repo.lower():
            return here
        up = os.path.dirname(here)
        if up == here:
            break
        here = up
    for root in places[1:]:
        try:
            for n in sorted(os.listdir(root)):
                if origin(os.path.join(root, n)) == repo.lower():
                    return os.path.join(root, n)
        except OSError:
            pass
    return None


def ready_to_build(cwd, repo, ref=None):
    """Would GitHub build exactly what is here? {'ok', 'reason', 'top', 'branch', 'sha'}. Needs: a git checkout whose
    origin is repo, nothing uncommitted or untracked, and HEAD already on GitHub as the branch's tip."""
    out = {'ok': False, 'reason': '', 'top': None, 'branch': None, 'sha': None}
    rc, top, _ = git(['rev-parse', '--show-toplevel'], cwd)
    if rc:
        out['reason'] = 'not a git checkout'
        return out
    out['top'] = os.path.normpath(top)
    remote_name = remote_for(top, repo)
    if not remote_name:
        try:
            with open(os.path.join(top, '.git', 'config'), encoding='utf-8', errors='replace') as f:
                origin = remote_repo(f.read())
        except OSError:
            origin = None
        out['reason'] = f"no remote of this checkout is {repo} (origin is {origin or 'not on GitHub'})"
        return out
    rc, dirty, _ = git(['status', '--porcelain'], top)
    if rc or dirty:
        n = len(dirty.splitlines())
        out['reason'] = f'{n} uncommitted or untracked file{"s" if n != 1 else ""}: GitHub would build something else'
        return out
    _, sha, _ = git(['rev-parse', 'HEAD'], top)
    branch = ref
    if not branch:
        rc, branch, _ = git(['rev-parse', '--abbrev-ref', 'HEAD'], top)
        if rc or branch == 'HEAD':
            out['reason'] = 'no branch checked out (detached HEAD)'
            return out
    rc, remote, err = git(['ls-remote', remote_name, f'refs/heads/{branch}'], top)
    if rc:
        out['reason'] = f"can't reach GitHub to compare ({err[:80]})"
        return out
    tip = remote.split()[0] if remote else None
    if tip != sha:
        out['reason'] = (f'{branch} on GitHub is at {tip[:7]}, here it is {sha[:7]}: push first' if tip
                         else f'{branch} is not on GitHub: push it first')
        return out
    out.update(ok=True, branch=branch, sha=sha, reason=f'{branch} at {sha[:7]} is on GitHub')
    return out


def job_cost(jobs, os_name):
    """(raw minutes, quota minutes) from a run's jobs: each job rounded up to a whole minute, as GitHub bills."""
    raw = 0
    for j in jobs or []:
        t0, t1 = parse_time(j.get('startedAt') or ''), parse_time(j.get('completedAt') or '')
        if t0 and t1 and t1 >= t0 and j.get('conclusion') != 'skipped':
            raw += max(math.ceil((t1 - t0) / 60), 1)
    return raw, raw * MULT.get(os_name, 1)


def place_files(src, dest):
    """Copies a downloaded artifact into dest, without the artifact's own folder when that is all there is. Returns
    the files placed, relative to dest."""
    root = src
    while True:
        entries = os.listdir(root)
        if len(entries) == 1 and os.path.isdir(os.path.join(root, entries[0])):
            root = os.path.join(root, entries[0])
        else:
            break
    placed = []
    for d, _, files in os.walk(root):
        for fn in files:
            rel = os.path.relpath(os.path.join(d, fn), root)
            target = os.path.join(dest, rel)
            os.makedirs(os.path.dirname(target), exist_ok=True)
            shutil.copy2(os.path.join(d, fn), target)
            placed.append(rel)
    return sorted(placed)


PROBE_RE = re.compile(r'cpu (?P<cpu>[\d.]+)% ram (?P<ram>[\d.]+)/(?P<ram_total>[\d.]+)G disk (?P<disk>[\d.]+)/'
                      r'(?P<disk_total>[\d.]+)G cores (?P<cores>\d+)(?: peak (?P<peak_cpu>[\d.]+)% (?P<peak_ram>[\d.]+)G)?')


def parse_probe(description):
    """One heavygate-probe status text -> {cpu, ram, ram_total, disk, disk_total, cores, peak_cpu, peak_ram, done}
    (numbers), or None when it isn't one."""
    m = PROBE_RE.search(description or '')
    if not m:
        return None
    out = {k: (float(v) if v is not None else None) for k, v in m.groupdict().items()}
    out['cores'] = int(out['cores'])
    out['done'] = (description or '').startswith('done ')
    return out


def runner_samples(cloud, repo, sha, run_id, limit=100):
    """The runner probe's readings for one run, oldest first: [{t, cpu, ram, ...}] from the commit's statuses (one gh
    call; GitHub lists the newest first). [] when the workflow has no probe or gh can't answer."""
    if not (repo and sha and run_id):
        return []
    rows, kind, _ = cloud.gh_json(['api', f'repos/{repo}/commits/{sha}/statuses?per_page={limit}'])
    out = []
    for r in rows or [] if not kind else []:
        if not str(r.get('context', '')).startswith('heavygate runner') or \
                not str(r.get('target_url', '')).endswith(f'/runs/{run_id}'):
            continue
        p = parse_probe(r.get('description'))
        if p:
            p['t'] = parse_time(r.get('created_at') or '')
            out.append(p)
    return sorted(out, key=lambda p: p['t'] or 0)


def runner_brief(samples, keep=24):
    """What the tray note carries: the newest reading, and the last `keep` CPU and RAM readings for a sparkline."""
    if not samples:
        return None
    last = dict(samples[-1])
    return {'now': last, 'cpu_hist': [round(s['cpu']) for s in samples[-keep:]],
            'ram_hist': [round(s['ram'], 1) for s in samples[-keep:]], 'n': len(samples)}


def round_trip(cloud, job, ready, name, say, auto=False, rule=None, est=None):
    """Start the mapped workflow at the commit that is here, follow it, bring its artifact home, log it. Returns the
    record; rec['exit'] is what the caller should exit with: 0 built, 1 failed on GitHub, 124 too slow (cancelled),
    130 stopped here (cancelled), 3 could not start (nothing ran on GitHub)."""
    c, repo, wf = cloud.cfg, job['repo'], job['workflow']
    max_min = float(job.get('max_min') or c['cloud_max_min'])   # a slow build's mapping can wait longer
    t0 = cloud.now()
    rec = {'name': name, 'repo': repo, 'workflow': wf, 'os': job['os'], 'branch': ready['branch'], 'sha': ready['sha'],
           'auto': bool(auto), 'rule': rule, 'est_min': est, 'dispatched_utc': utcnow(t0).strftime('%Y-%m-%dT%H:%M:%SZ'),
           'started': datetime.fromtimestamp(t0).isoformat(timespec='seconds'), 'run_id': None, 'url': None,
           'conclusion': None, 'exit': 3, 'raw_min': None, 'cost_min': None, 'files': [], 'dest': None}

    mark = {'path': None, 'runner': None, 'status': None}

    def note(status, ended=None):
        """cloud-active/<run>.json: what the tray shows while this build is on GitHub (no gh call needed)."""
        if not rec.get('run_id'):
            return
        d = os.path.join(cloud.home, 'cloud-active')
        os.makedirs(d, exist_ok=True)
        mark['path'] = os.path.join(d, f"{rec['run_id']}.json")
        tmp = mark['path'] + '.tmp'
        with open(tmp, 'w', encoding='utf-8') as f:
            json.dump({'repo': repo, 'workflowName': rec.get('name') or wf, 'status': status, 'url': rec.get('url'),
                       'sha': ready['sha'], 'os': job['os'], 'started_ts': t0, 'pid': os.getpid(),
                       'runner': mark['runner'], 'ended_ts': ended, 'dest': rec.get('dest'),
                       'files': rec.get('files') or []}, f)
        mark['status'] = status
        os.replace(tmp, mark['path'])

    def done(code, conclusion=None, **kw):
        rec.update(kw, exit=code, conclusion=conclusion, ended=datetime.fromtimestamp(cloud.now()).isoformat(timespec='seconds'),
                   ran_s=round(cloud.now() - t0))
        if mark['path']:   # the tray keeps the result up for a little while (cloud_done_show_s), then drops the note
            try:
                note({0: 'built'}.get(code, conclusion or 'failed'), ended=cloud.now())
            except OSError:
                pass
        try:
            log_run(cloud.home, rec)
        except OSError:
            pass
        return rec

    out, kind, msg = cloud.gh(['workflow', 'run', wf, '-R', repo, '--ref', ready['branch']], timeout=60)
    if kind:
        say(f'GitHub would not start {wf}: {msg[:200]}')
        return done(3, message=msg[:200])
    m = re.search(r'/actions/runs/(\d+)', out or '')
    run_id = int(m.group(1)) if m else None
    deadline = t0 + 90
    while run_id is None and cloud.now() < deadline:   # older gh prints no URL: find the run by its commit
        time.sleep(min(c['cloud_poll_s'], 5))
        runs, kind, _ = cloud.gh_json(['run', 'list', '-R', repo, '--workflow', wf, '--event', 'workflow_dispatch',
                                       '--limit', '10', '--json', 'databaseId,headSha,createdAt'])
        for r in runs or []:
            if r.get('headSha') == ready['sha'] and (parse_time(r.get('createdAt')) or 0) >= t0 - 60:
                run_id = r['databaseId']
                break
    if run_id is None:
        say(f'Started {wf} on {repo}, but its run never showed up. Look at: gh run list -R {repo}')
        return done(3, message='run not found')
    rec['run_id'], rec['url'] = run_id, f'https://github.com/{repo}/actions/runs/{run_id}'
    say(f"On GitHub: {rec['url']}")
    try:
        note('queued')
    except OSError:
        pass
    status, jobs, last = None, [], None
    try:
        while True:
            v, kind, msg = cloud.gh_json(['run', 'view', str(run_id), '-R', repo, '--json',
                                          'status,conclusion,jobs,createdAt,updatedAt'])
            if not kind:
                status, jobs = v.get('status'), v.get('jobs') or []
                if status != last:
                    last = status
                    try:
                        note('in_progress' if status != 'completed' else 'downloading')
                    except OSError:
                        pass
                    say(f'{repo} {wf}: {status}' + (f" ({fmt_secs(cloud.now() - t0)} so far)" if status != 'queued' else ''))
                if status == 'in_progress':   # the runner probe's CPU, RAM and disk (workflows that carry one)
                    brief = runner_brief(runner_samples(cloud, repo, ready['sha'], run_id))
                    if brief and brief != mark['runner']:
                        mark['runner'] = brief
                        try:
                            note(mark['status'] or 'in_progress')
                        except OSError:
                            pass
                if status == 'completed':
                    rec['conclusion'] = v.get('conclusion')
                    break
            if cloud.now() - t0 > max_min * 60:
                cloud.gh(['run', 'cancel', str(run_id), '-R', repo])
                say(f"Cancelled: still {status} after {max_min:g} min ({'max_min of this mapping' if job.get('max_min') else 'cloud_max_min'}).")
                raw, cost = job_cost(jobs, job['os'])
                return done(124, 'cancelled', raw_min=raw, cost_min=cost)
            time.sleep(c['cloud_poll_s'])
    except KeyboardInterrupt:
        cloud.gh(['run', 'cancel', str(run_id), '-R', repo])
        say('Stopped here: the GitHub run was cancelled too.')
        raw, cost = job_cost(jobs, job['os'])
        return done(130, 'cancelled', raw_min=raw, cost_min=cost)
    raw, cost = job_cost(jobs, job['os'])
    rec.update(raw_min=raw, cost_min=cost)
    if rec['conclusion'] != 'success':
        say(f"Failed on GitHub ({rec['conclusion']}) after {fmt_secs(cloud.now() - t0)}, {cost} quota min. "
            f"Why: gh run view {run_id} -R {repo} --log-failed")
        return done(1, rec['conclusion'])
    dest = os.path.normpath(os.path.join(ready['top'], job.get('dest') or 'dist/cloud'))
    tmp = os.path.join(cloud.home, 'cloud-download', str(run_id))
    shutil.rmtree(tmp, ignore_errors=True)
    os.makedirs(tmp, exist_ok=True)
    args = ['run', 'download', str(run_id), '-R', repo, '-D', tmp] + (['-n', job['artifact']] if job.get('artifact') else [])
    _, kind, msg = cloud.gh(args, timeout=600)
    if kind:
        say(f'Built, but the download failed: {msg[:200]}. Try: gh run download {run_id} -R {repo}')
        return done(1, 'success', message='download failed: ' + msg[:150])
    try:
        files = place_files(tmp, dest)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    if not files:
        say(f'Built, but the run had no files to bring back (no upload-artifact step?).')
        return done(0, 'success', files=files, dest=dest)
    sums = check_sums(dest, files)
    if sums['bad']:
        say(f"Built, but {', '.join(sums['bad'][:3])} did not match its checksum. The copy on GitHub is kept: "
            f"gh run download {run_id} -R {repo}")
        return done(1, 'success', files=files, dest=dest, sums=sums, message='checksum mismatch')
    say(f"Built on GitHub in {fmt_secs(cloud.now() - t0)} ({cost} quota min). In {dest}: " + ', '.join(files[:4])
        + (f' +{len(files) - 4} more' if len(files) > 4 else '')
        + (f" ({sums['ok']} checksum{'s' if sums['ok'] != 1 else ''} match)" if sums['ok'] else ''))
    deleted = None
    if c.get('cloud_delete_after', True):
        deleted, failed = cloud.delete_run_artifacts(repo, run_id, job.get('artifact'))
        if failed:
            say(f"Couldn't delete the copy on GitHub; it expires on its own (the workflow's retention-days), or "
                f"heavy cloud prune removes it.")
        elif deleted:
            say('Deleted the copy on GitHub, so it stops counting against the storage.')
    return done(0, 'success', files=files, dest=dest, sums=sums, deleted=deleted)


def check_sums(dest, files):
    """Checks every file a downloaded SHA256SUMS-style file lists ('<64 hex>  name' or '<hex> *name'), relative to that
    sums file. {'ok': matched, 'bad': [names that differ or are missing]}."""
    import hashlib
    ok, bad = 0, []
    for rel in files:
        if 'sha256' not in os.path.basename(rel).lower():
            continue
        try:
            with open(os.path.join(dest, rel), encoding='utf-8', errors='replace') as f:
                lines = f.read().splitlines()
        except OSError:
            continue
        for line in lines:
            m = re.match(r'^([0-9a-fA-F]{64})\s+\*?(.+?)\s*$', line)
            if not m:
                continue
            target = os.path.join(dest, os.path.dirname(rel), m.group(2))
            h = hashlib.sha256()
            try:
                with open(target, 'rb') as f:
                    for block in iter(lambda: f.read(1 << 20), b''):
                        h.update(block)
            except OSError:
                bad.append(m.group(2))
                continue
            if h.hexdigest().lower() == m.group(1).lower():
                ok += 1
            else:
                bad.append(m.group(2))
    return {'ok': ok, 'bad': bad}


def log_cloud(home, rec):
    """cloud.log: dispatches and deleted artifacts, one JSON object a line."""
    try:
        os.makedirs(home, exist_ok=True)
        with open(os.path.join(home, 'cloud.log'), 'a', encoding='utf-8') as f:
            f.write(json.dumps(rec) + '\n')
    except OSError:
        pass


# text -------------------------------------------------------------------------------------------------------------------
def fmt_min(m):
    return f'{round(m):,}'


def short_line(summ):
    """One line for the strip and the tooltip."""
    if summ.get('state') != 'ok':
        return 'cloud: ' + {'no_scope': 'needs the user scope', 'no_gh': 'gh not installed', 'no_auth': 'gh not signed in',
                            'unknown': 'not read yet', 'rate': 'rate limited'}.get(summ.get('state'), 'unreadable')
    n = len(summ['active'])
    st = (summ.get('storage') or {}).get('level')
    return (f'{n} running' if n else 'idle') + f" · {fmt_min(summ['left_min'])} min left" + \
        {'low': ' · storage low', 'over': ' · storage used up'}.get(st, '')


def status_lines(summ):
    """The block `heavy status` and `heavy cloud` print."""
    if summ.get('state') != 'ok':
        why = {'no_scope': 'GitHub says the sign-in is missing the "user" scope, which the billing report needs.',
               'no_gh': 'the gh command is not installed.', 'no_auth': 'gh is not signed in.',
               'unknown': 'nothing read yet: run heavy cloud (the tray reads it in the background).',
               'rate': 'GitHub rate limit reached; it retries on its own.'}.get(summ.get('state'),
                                                                               summ.get('error') or 'unreadable')
        out = [f'  CLOUD lane (GitHub Actions): not available: {why}']
        if summ.get('fix'):
            out.append(f"    Fix: {summ['fix']}")
        return out
    lvl = {'ok': '', 'watch': '  (60% of the usable minutes gone)', 'low': '  (into the reserve: only releases)',
           'over': '  (OUT OF MINUTES OR BEING CHARGED)'}[summ['level']]
    out = [f"  CLOUD lane (GitHub Actions, {summ['plan'] or 'unknown'} plan"
           + (', quota assumed' if summ['quota_assumed'] else '') + f"): {fmt_min(summ['used_min'])} of "
           f"{fmt_min(summ['quota_min'])} min used in {summ['month']}, {fmt_min(summ['left_min'])} left, "
           f"{fmt_min(summ['usable_min'])} usable above the {fmt_min(summ['reserve_min'])} reserve{lvl}",
           f"    On pace for {fmt_min(summ['pace_min'])} min by month end; resets {summ['resets']} "
           f"({summ['days_left']} days). Charged: ${summ['net_usd']:.2f} (list price ${summ['gross_usd']:.2f}, covered by "
           f"the free allowance)."]
    out += storage_lines(summ)
    m = summ['minutes']
    out.append(f"    Raw minutes: Linux {m['linux']:g}, Windows {m['windows']:g} (counts 2x), macOS {m['macos']:g}"
               + (f", larger runners {m['other']:g}" if m['other'] else '')
               + (f". Last month {fmt_min(summ['prev_used_min'])}." if summ.get('prev_used_min') is not None else ''))
    if summ['by_repo']:
        out.append('    By repo: ' + ', '.join(f'{r} {fmt_min(v)}' for r, v in list(summ['by_repo'].items())[:6]))
    for a in summ['active']:
        out.append(f"    RUNNING {a['repo']}: {a.get('workflowName')} ({a.get('status')}"
                   + (f", {fmt_secs(a['for_s'])}" if a.get('for_s') is not None else '') + f") {a.get('displayTitle', '')[:50]}")
    if not summ['active']:
        out.append('    Nothing running on GitHub now.')
    a = summ.get('auto')
    if a:
        out.append(f"    Auto: {'ON' if a['on'] else 'off'}, {a['maps']} build{'s' if a['maps'] != 1 else ''} mapped to "
                   f"workflows" + (f"; spent {fmt_min(a['day_min'])} of {fmt_min(a['day_cap'])} min today, "
                                   f"{fmt_min(a['month_min'])} of {fmt_min(a['month_cap'])} this month" if a['on'] else
                                   ' (heavy cloud auto on lets heavy auto send them)'))
    return out


def storage_lines(summ):
    st = summ.get('storage')
    if not st:
        return []
    now = (f"{st['now_gb']:.2f} GB stored now in {st['count']} artifact{'s' if st['count'] != 1 else ''}"
           if st['count'] is not None else f"about {st['now_gb']:.2f} GB stored (from the report)")
    out = [f"    Artifact storage: {fmt_min(st['used_gbh'])} of {fmt_min(st['incl_gbh'])} GB-hours used "
           f"({st['incl_gb']:g} GB included all month); {now}, on pace for {fmt_min(st['proj_gbh'])} by month end."]
    if st['level'] == 'over':
        out.append(f"    STORAGE USED UP: GitHub may refuse new uploads until {summ['resets']}, so mapped builds stay here. "
                   f"heavy cloud prune --days 0 clears what is left (the hours already used stay used).")
    elif st['runs_out']:
        out.append(f"    STORAGE RUNS OUT about {st['runs_out']} at this rate: then GitHub may refuse uploads until "
                   f"{summ['resets']}. heavy cloud prune deletes old artifacts now.")
    keep = st.get('keep_days') or 0
    p = st.get('pruned')
    out.append('    ' + (f"Artifacts older than {keep:g} days are deleted (cloud_keep_days)" if keep else
                         'Old artifacts are kept until GitHub expires them (cloud_keep_days 0)')
               + (f"; last clean-up {p['time'][:16].replace('T', ' ')} deleted {p['count']} ({p['mb']:,} MB)" if p else '')
               + ('. A build heavy brings home is deleted on GitHub at once.' if st.get('delete_after') else '.'))
    big = sorted(((r, mb) for r, mb in (st.get('by_repo') or {}).items() if mb), key=lambda x: -x[1])[:4]
    if big:
        out.append('    Stored by repo: ' + ', '.join(f'{r.split("/")[-1]} {mb:,} MB' for r, mb in big))
    return out


def fmt_secs(s):
    s = int(s or 0)
    return f'{s}s' if s < 90 else f'{round(s / 60)}m' if s < 5400 else f'{s // 3600}h{round(s % 3600 / 60):02d}m'


def runs_lines(summ, n=12):
    rows = summ['active'] + summ['recent']
    rows = sorted(rows, key=lambda r: r.get('createdAt') or '', reverse=True)[:n]
    if not rows:
        return ['  No runs read yet (heavy cloud refreshes, or the tray does in the background).']
    out = []
    for r in rows:
        t0, t1 = parse_time(r.get('startedAt') or r.get('createdAt')), parse_time(r.get('updatedAt'))
        took = fmt_secs(r['for_s']) if r.get('for_s') is not None else fmt_secs((t1 - t0) if t0 and t1 else 0)
        res = r.get('conclusion') or r.get('status')
        out.append(f"  {(r.get('createdAt') or '')[:16].replace('T', ' ')}  {r['repo']:30s} {str(r.get('workflowName'))[:22]:22s} "
                   f"{took:>6s}  {res:10s} {r.get('event', '')}")
    return out
