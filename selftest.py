"""HeavyGate self-tests against a throwaway state folder (never the real lanes).

    python selftest.py
"""
import calendar
import errno
import http.server
import importlib.util
import json
import os
import shlex
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import unittest

WIN = os.name == 'nt'
windows_only = unittest.skipUnless(WIN, 'the tray, its shutdown and the sign-in shortcut are Windows only')
HERE = os.path.dirname(os.path.abspath(__file__))


def own_priority():
    """The priority this test runs at. A GPU lane job keeps its caller's: normal at a desktop, below normal on a
    GitHub runner, which starts every step that way."""
    if not WIN:
        return os.nice(0)
    import ctypes
    k = ctypes.windll.kernel32
    k.GetCurrentProcess.restype = ctypes.c_void_p
    k.GetPriorityClass.argtypes = [ctypes.c_void_p]
    return k.GetPriorityClass(k.GetCurrentProcess())


INHERITED = own_priority()
GATE = os.path.join(HERE, 'heavygate.py')
HOG = r'''
import sys, time
blocks = []
try:
    while len(blocks) * 100 < int(sys.argv[1]):
        blocks.append(bytearray(100 * 2 ** 20))
        time.sleep(float(sys.argv[2]))
except MemoryError:
    print('MemoryError at', len(blocks) * 100, flush=True)
    sys.exit(3)
time.sleep(float(sys.argv[3]))
print('hog finished', flush=True)
'''
JOB = r'''
import ctypes, json, os, sys, time
out, label, secs = sys.argv[1], sys.argv[2], float(sys.argv[3])
code = int(sys.argv[4]) if len(sys.argv) > 4 else 0
if os.name == 'nt':
    k = ctypes.windll.kernel32
    k.GetCurrentProcess.restype = ctypes.c_void_p
    h = k.GetCurrentProcess()
    mine, system = ctypes.c_size_t(), ctypes.c_size_t()
    k.GetProcessAffinityMask(ctypes.c_void_p(h), ctypes.byref(mine), ctypes.byref(system))
    k.GetPriorityClass.argtypes = [ctypes.c_void_p]
    priority, threads = k.GetPriorityClass(ctypes.c_void_p(h)), bin(mine.value).count('1')
else:
    priority, threads = os.nice(0), len(os.sched_getaffinity(0))
rec = {'start': time.time(), 'priority': priority, 'threads': threads, 'held': os.environ.get('HEAVYGATE_HELD')}
time.sleep(secs)
rec['end'] = time.time()
open(os.path.join(out, label + '.json'), 'w').write(json.dumps(rec))
sys.exit(code)
'''


def load_gate():
    spec = importlib.util.spec_from_file_location('heavygate_under_test', GATE)
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


class GateTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp(prefix='heavygate-test-')
        cls.state = os.path.join(cls.tmp, 'state')
        cls.out = os.path.join(cls.tmp, 'out')
        os.makedirs(cls.state)
        os.makedirs(cls.out)
        cls.job = os.path.join(cls.tmp, 'job.py')
        with open(cls.job, 'w') as f:
            f.write(JOB)
        cls.env = dict(os.environ, HEAVYGATE_DIR=cls.state, HEAVYGATE_GUARD='off')   # never a guard left running
        cls.env.pop('HEAVYGATE_HELD', None)
        cls.env.pop('HEAVYGATE', None)
        cls.settings({})

    @classmethod
    def settings(cls, extra):
        s = {'expect_mb': {'gpu': 0, 'cpu': 0}, 'reserve_mb': 0, 'free_daemons': False, 'poll_s': 0.2, 'cpu_threads': 12,
             'free_ollama': False, 'ollama_url': 'http://127.0.0.1:9',   # never the real one
             'ram_ceiling_pct': 100, 'ram_kill_pct': 101,   # the RAM ceiling has its own tests
             'cpu_slots': 1}   # one job at a time, so queue order is testable; two slots have their own test
        s.update(extra)
        with open(os.path.join(cls.state, 'settings.json'), 'w') as f:
            json.dump(s, f)

    def gate(self, lane, label, secs, *extra, code=0, name=None, capture=False):
        cmd = [sys.executable, GATE, lane, '-n', name or label, *extra, '--', sys.executable, self.job, self.out, label,
               str(secs), str(code)]
        if capture:
            return subprocess.Popen(cmd, env=self.env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
        return subprocess.Popen(cmd, env=self.env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

    def rec(self, label):
        with open(os.path.join(self.out, label + '.json')) as f:
            return json.load(f)

    def wait_all(self, *ps, timeout=60):
        return [p.wait(timeout=timeout) for p in ps]

    def test_same_lane_runs_one_at_a_time(self):
        a = self.gate('gpu', 'ser-a', 1.5)
        time.sleep(0.6)
        b = self.gate('gpu', 'ser-b', 0.2)
        self.assertEqual(self.wait_all(a, b), [0, 0])
        self.assertGreaterEqual(self.rec('ser-b')['start'], self.rec('ser-a')['end'] - 0.05)

    def test_gpu_and_cpu_lanes_overlap(self):
        a = self.gate('gpu', 'ov-a', 1.5)
        time.sleep(0.6)
        b = self.gate('cpu', 'ov-b', 0.2)
        self.assertEqual(self.wait_all(a, b), [0, 0])
        self.assertLess(self.rec('ov-b')['start'], self.rec('ov-a')['end'])

    def test_queue_is_first_come_first_served(self):
        a = self.gate('gpu', 'q-a', 2.0)
        time.sleep(0.6)
        b = self.gate('gpu', 'q-b', 0.5)
        time.sleep(0.6)
        c = self.gate('gpu', 'q-c', 0.2)
        self.assertEqual(self.wait_all(a, b, c), [0, 0, 0])
        self.assertGreaterEqual(self.rec('q-c')['start'], self.rec('q-b')['end'] - 0.05)

    def test_nested_job_runs_in_parents_slot(self):
        args = [sys.executable, GATE, 'gpu', '-n', 'inner', '--', sys.executable, self.job, self.out, 'nest-inner', '0.1']
        inner = subprocess.list2cmdline(args) if WIN else shlex.join(args)
        p = subprocess.Popen([sys.executable, GATE, 'gpu', '-n', 'outer', '--', inner], env=self.env,
                             stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
        self.assertEqual(p.wait(timeout=20), 0)
        self.assertEqual(self.rec('nest-inner')['held'], 'gpu')

    def test_killed_holder_frees_the_lane(self):
        a = self.gate('gpu', 'kill-a', 30)
        time.sleep(1.0)
        if WIN:
            subprocess.run(['taskkill', '/F', '/PID', str(a.pid)], capture_output=True)   # the gate, not its job
        else:
            a.kill()
            a.wait()
        t0 = time.time()
        b = self.gate('gpu', 'kill-b', 0.1)
        self.assertEqual(b.wait(timeout=20), 0)
        self.assertLess(time.time() - t0, 10)
        # the orphaned 30 s job keeps running without the gate; stop it so the folder can be removed
        if WIN:
            subprocess.run(['powershell', '-NoProfile', '-Command',
                            f"Get-CimInstance Win32_Process | ? {{ $_.CommandLine -like '*kill-a*' -and $_.CommandLine -like '*job.py*' }} | % {{ Stop-Process -Id $_.ProcessId -Force }}"],
                           capture_output=True)
        else:
            subprocess.run(['pkill', '-f', 'job.py .*kill-a'], capture_output=True)

    @unittest.skipIf(WIN, 'Linux: a stopped heavy takes its job with it')
    def test_terminated_gate_stops_its_job(self):
        a = self.gate('gpu', 'term-a', 30)
        time.sleep(1.0)
        a.terminate()
        self.assertEqual(a.wait(timeout=20), 130)
        self.assertEqual(subprocess.run(['pgrep', '-f', 'job.py .*term-a'], capture_output=True).returncode, 1)
        self.assertEqual(self.gate('gpu', 'term-b', 0.1).wait(timeout=20), 0)

    def test_exit_code_passes_through(self):
        self.assertEqual(self.gate('cpu', 'code', 0.1, code=3).wait(timeout=20), 3)

    def test_cpu_lane_lowers_priority_and_threads(self):
        self.assertEqual(self.gate('cpu', 'prio', 0.1).wait(timeout=20), 0)
        r = self.rec('prio')
        self.assertEqual(r['priority'], 0x4000 if WIN else 10)   # below normal, through the shell
        self.assertEqual(r['threads'], min(12, os.cpu_count()))
        self.assertEqual(self.gate('gpu', 'prio-gpu', 0.1).wait(timeout=20), 0)
        self.assertEqual(self.rec('prio-gpu')['priority'], INHERITED)   # the GPU lane leaves the caller's priority
        self.assertEqual(self.rec('prio-gpu')['threads'], os.cpu_count())

    def test_threads_sets_one_jobs_share_and_no_other(self):
        n = os.cpu_count()
        self.assertEqual(self.gate('cpu', 'thr-2', 0.1, '--threads', '2').wait(timeout=20), 0)
        self.assertEqual(self.rec('thr-2')['threads'], 2)
        self.assertEqual(self.rec('thr-2')['priority'], 0x4000 if WIN else 10)   # a bigger share is not a higher priority
        self.assertEqual(self.gate('cpu', 'thr-all', 0.1, '--threads', str(n + 8)).wait(timeout=20), 0)
        self.assertEqual(self.rec('thr-all')['threads'], n)
        self.assertEqual(self.gate('cpu', 'thr-next', 0.1).wait(timeout=20), 0)
        self.assertEqual(self.rec('thr-next')['threads'], min(12, n))

    def test_threads_shows_in_status_and_log(self):
        a = self.gate('cpu', 'thr-seen', 8, '--threads', '2')
        try:
            end = time.time() + 15
            while time.time() < end:
                r = subprocess.run([sys.executable, GATE, 'status'], env=self.env, capture_output=True, text=True, timeout=30)
                if 'thr-seen' in r.stdout:
                    break
                time.sleep(0.2)
            self.assertRegex(r.stdout, r'thr-seen.*2 threads')
        finally:
            subprocess.run([sys.executable, GATE, 'stop', 'cpu'], env=self.env, capture_output=True, timeout=30)
            a.wait(timeout=30)
        out = subprocess.run([sys.executable, GATE, 'log', '-n', '1'], env=self.env, capture_output=True, text=True).stdout
        self.assertRegex(out, r' 2  thr-seen')

    def test_threads_refused_where_it_means_nothing(self):
        for lane, label, value in (('gpu', 'thr-gpu', '4'), ('cpu', 'thr-zero', '0')):
            p = self.gate(lane, label, 0.1, '--threads', value, capture=True)
            out = p.communicate(timeout=20)[0]
            self.assertEqual(p.returncode, 2, out)
            self.assertIn('--threads', out)
            self.assertFalse(os.path.exists(os.path.join(self.out, label + '.json')))

    def test_threads_inside_a_held_lane_keeps_the_parents_share(self):
        args = [sys.executable, GATE, 'cpu', '-n', 'inner', '--threads', '4', '--', sys.executable, self.job, self.out,
                'thr-inner', '0.1']
        inner = subprocess.list2cmdline(args) if WIN else shlex.join(args)
        p = subprocess.Popen([sys.executable, GATE, 'cpu', '-n', 'outer', '--threads', '2', '--', inner], env=self.env,
                             stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
        out = p.communicate(timeout=20)[0]
        self.assertEqual(p.returncode, 0, out)
        self.assertEqual(self.rec('thr-inner')['threads'], 2)
        self.assertIn('does not apply', out)

    def rules(self, rules):
        with open(os.path.join(self.state, 'rules.json'), 'w') as f:
            json.dump({'rules': rules}, f)

    def auto(self, label, *extra):
        cmd = [sys.executable, GATE, 'auto', '-n', label, *extra, '--', sys.executable, self.job, self.out, label, '0.1']
        return subprocess.run(cmd, env=self.env, capture_output=True, text=True, timeout=30)

    def test_auto_follows_the_first_matching_rule(self):
        self.rules([{'match': 'job.py', 'lane': 'cpu', 'threads': 2}, {'match': 'job.py', 'lane': 'gpu'}])
        try:
            r = self.auto('auto-rule')
            self.assertEqual(r.returncode, 0, r.stderr)
            self.assertEqual(self.rec('auto-rule')['held'], 'cpu')
            self.assertEqual(self.rec('auto-rule')['threads'], 2)
            self.assertIn('rule 1', r.stderr)
            self.rules([{'match': 'job.py', 'lane': 'gpu'}])
            self.assertEqual(self.auto('auto-gpu').returncode, 0)
            self.assertEqual(self.rec('auto-gpu')['held'], 'gpu')
            self.assertEqual(self.rec('auto-gpu')['priority'], INHERITED)
        finally:
            os.remove(os.path.join(self.state, 'rules.json'))

    def test_auto_with_no_rule_takes_the_cpu_lane_at_the_usual_share(self):
        self.rules([])
        try:
            r = self.auto('auto-none')
            self.assertEqual(r.returncode, 0, r.stderr)
            self.assertEqual(self.rec('auto-none')['held'], 'cpu')
            self.assertEqual(self.rec('auto-none')['threads'], min(12, os.cpu_count()))
            self.assertIn('no rule', r.stderr)
        finally:
            os.remove(os.path.join(self.state, 'rules.json'))

    def test_a_threads_flag_beats_the_rule(self):
        self.rules([{'match': 'job.py', 'lane': 'cpu', 'threads': 4}])
        try:
            self.assertEqual(self.auto('auto-flag', '--threads', '2').returncode, 0)
            self.assertEqual(self.rec('auto-flag')['threads'], 2)
        finally:
            os.remove(os.path.join(self.state, 'rules.json'))

    def test_a_broken_rules_file_never_stops_a_job(self):
        with open(os.path.join(self.state, 'rules.json'), 'w') as f:
            f.write('{ not json')
        try:
            r = self.auto('auto-broken')
            self.assertEqual(r.returncode, 0, r.stderr)
            self.assertEqual(self.rec('auto-broken')['held'], 'cpu')
        finally:
            os.remove(os.path.join(self.state, 'rules.json'))

    def test_why_answers_without_running(self):
        self.rules([{'match': 'blender', 'lane': 'gpu'}])
        try:
            r = subprocess.run([sys.executable, GATE, 'why', '--', 'blender', '-b', 'scene.blend'], env=self.env,
                               capture_output=True, text=True, timeout=20)
            self.assertEqual(r.returncode, 0, r.stderr)
            self.assertIn('gpu lane', r.stdout)
        finally:
            os.remove(os.path.join(self.state, 'rules.json'))

    def test_rule_matching(self):
        hg = load_gate()
        self.assertFalse(hg.rule_matches({'match': '', 'folder': ''}, 'anything', r'F:\Code'))
        self.assertTrue(hg.rule_matches({'match': 'tar'}, 'tar czf x.tgz public'))
        self.assertTrue(hg.rule_matches({'match': 'tar'}, r'C:\tools\tar.exe x'))
        self.assertFalse(hg.rule_matches({'match': 'tar'}, 'start notepad'))
        self.assertFalse(hg.rule_matches({'match': 'gradle'}, 'gradle-wrapper-check'))
        self.assertTrue(hg.rule_matches({'match': 'BLENDER'}, 'blender.exe -b'))
        self.assertTrue(hg.rule_matches({'match': 'build.cmd', 'folder': 'harbor'}, 'build.cmd', r'F:\Code\Harbor'))
        self.assertFalse(hg.rule_matches({'match': 'build.cmd', 'folder': 'harbor'}, 'build.cmd', r'F:\Code\Other'))
        self.assertTrue(hg.rule_matches({'match': '', 'folder': 'Harbor'}, 'anything', r'F:\Code\Harbor'))

    def test_bad_rules_are_dropped(self):
        hg = load_gate()
        self.assertIsNone(hg.clean_rule({'match': 'x', 'lane': 'disk'}))
        self.assertIsNone(hg.clean_rule({'match': '', 'folder': ' ', 'lane': 'cpu'}))
        self.assertIsNone(hg.clean_rule({'match': 'x', 'lane': 'cpu', 'threads': -1}))
        self.assertIsNone(hg.clean_rule({'match': 'x', 'lane': 'gpu'})['threads'])
        self.assertEqual(hg.clean_rule({'match': 'x', 'lane': 'cpu', 'threads': 'all'})['threads'], 'all')
        self.assertIsNone(hg.clean_rule({'match': 'x', 'lane': 'gpu', 'threads': 8})['threads'])

    def test_set_setting_keeps_the_others(self):
        hg = load_gate()
        old = os.environ.get('HEAVYGATE_DIR')
        os.environ['HEAVYGATE_DIR'] = tempfile.mkdtemp(dir=self.tmp)
        try:
            with open(os.path.join(hg.home(), 'settings.json'), 'w') as f:
                json.dump({'reserve_mb': 1024}, f)
            hg.set_setting('cpu_threads', 6)
            self.assertEqual(hg.settings()['cpu_threads'], 6)
            self.assertEqual(hg.settings()['reserve_mb'], 1024)
            hg.set_setting('cpu_threads', None)
            self.assertEqual(hg.settings()['cpu_threads'], 0)
            self.assertEqual(hg.settings()['reserve_mb'], 1024)
        finally:
            os.environ['HEAVYGATE_DIR'] = old if old is not None else ''

    def test_suggestions_come_from_runs_that_agree(self):
        hg = load_gate()
        empty = {'rules': [], 'dismissed': []}

        def run(i, lane, gpu, tool='python tools/render.py -q', cpu=10, ran=60, cwd=r'F:\Code\App'):
            return {'lane': lane, 'command': tool, 'cwd': cwd, 'ran_s': ran, 'gpu_util_max': gpu, 'cpu_avg': cpu,
                    'started': f'2026-11-0{i}T10:00:00'}

        hot = [run(i, 'cpu', 85) for i in (1, 2, 3)]
        s = hg.suggestions(hot, empty)
        self.assertEqual((s[0]['match'], s[0]['lane']), ('render.py', 'gpu'))
        self.assertEqual(hg.suggestions(hot[:2], empty), [])                                   # two runs is not enough
        self.assertEqual(hg.suggestions([run(i, 'cpu', 85, ran=5) for i in (1, 2, 3)], empty), [])   # too short to say
        self.assertEqual(hg.suggestions(hot, {'rules': [{'match': 'render.py', 'lane': 'cpu'}], 'dismissed': []}), [])
        self.assertEqual(hg.suggestions(hot, {'rules': [], 'dismissed': [s[0]['key']]}), [])
        beside = hot + [run(i, 'gpu', 90, tool='blender', ran=120) for i in (1, 2, 3)]         # the GPU was someone else's
        self.assertEqual([x for x in hg.suggestions(beside, empty) if x['match'] == 'render.py'], [])
        idle = [run(i, 'gpu', 4, tool='pack.py') for i in (1, 2, 3)]
        self.assertEqual(hg.suggestions(idle, empty)[0]['lane'], 'cpu')
        plain = [run(i, 'cpu', 85, tool='build.cmd', cwd=r'F:\Code\Harbor') for i in (1, 2, 3)]
        self.assertEqual(hg.suggestions(plain, empty)[0]['folder'], 'Harbor')

    def fake_ollama(self, *names, stuck=()):
        """A small Ollama: lists what's loaded, and a keep_alive 0 request unloads it unless it's in stuck."""
        loaded, asked = list(names), []

        class Api(http.server.BaseHTTPRequestHandler):
            def reply(self, body):
                data = json.dumps(body).encode()
                self.send_response(200)
                self.send_header('Content-Length', str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def do_GET(self):
                self.reply({'models': [{'name': n, 'size': 2 << 30, 'size_vram': 1 << 30} for n in loaded]})

            def do_POST(self):
                body = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
                asked.append(body)
                if body['model'] not in stuck:
                    loaded.remove(body['model'])
                self.reply({'done_reason': 'unload'})

            def log_message(self, *a):
                pass
        server = http.server.ThreadingHTTPServer(('127.0.0.1', 0), Api)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        self.addCleanup(self.settings, {})
        return f'http://127.0.0.1:{server.server_port}', loaded, asked

    def test_gpu_job_clears_ollama_first_and_a_cpu_job_leaves_it(self):
        url, loaded, asked = self.fake_ollama('qwen3:8b', 'nomic-embed-text')
        self.settings({'free_ollama': True, 'ollama_url': url})
        self.assertEqual(self.gate('cpu', 'ol-cpu', 0.1).wait(timeout=30), 0)
        self.assertEqual(asked, [])
        p = self.gate('gpu', 'ol-gpu', 0.1, capture=True)
        out = p.communicate(timeout=30)[0]
        self.assertEqual(p.returncode, 0, out)
        self.assertEqual(sorted(a['model'] for a in asked), ['nomic-embed-text', 'qwen3:8b'])
        self.assertTrue(all(a['keep_alive'] == 0 and 'prompt' not in a for a in asked))
        self.assertEqual(loaded, [])
        self.assertIn('unloading Ollama', out)

    def test_cpu_job_clears_ollama_only_when_ram_is_short(self):
        url, loaded, asked = self.fake_ollama('qwen3:8b')
        self.settings({'free_ollama': True, 'ollama_url': url, 'expect_mb': {'gpu': 0, 'cpu': 10 ** 7}, 'ram_wait_min': 0.02})
        self.assertEqual(self.gate('cpu', 'ol-short', 0.1).wait(timeout=30), 0)
        self.assertEqual([a['model'] for a in asked], ['qwen3:8b'])

    def test_ollama_left_alone_when_turned_off_and_never_stops_a_job(self):
        url, loaded, asked = self.fake_ollama('qwen3:8b')
        self.settings({'free_ollama': False, 'ollama_url': url})
        self.assertEqual(self.gate('gpu', 'ol-off', 0.1).wait(timeout=30), 0)
        self.assertEqual(asked, [])
        self.settings({'free_ollama': True, 'ollama_url': 'http://127.0.0.1:9'})   # nothing listening
        self.assertEqual(self.gate('gpu', 'ol-down', 0.1).wait(timeout=30), 0)

    def test_ollama_in_status_and_heavy_free(self):
        url, loaded, asked = self.fake_ollama('qwen3:8b', stuck=('qwen3:8b',))
        self.settings({'ollama_url': url})

        def heavy(*args):
            return subprocess.run([sys.executable, GATE, *args], env=self.env, capture_output=True, text=True, timeout=60).stdout
        self.assertIn('Ollama: qwen3:8b 2.0 GB, 1.0 on the GPU', heavy('status'))
        self.assertEqual(json.loads(heavy('status', '--json'))['ollama'], [{'name': 'qwen3:8b', 'mb': 2048, 'gpu_mb': 1024}])
        self.assertIn('would unload', heavy('free', '--dry-run'))
        self.assertEqual(asked, [])
        self.assertIn('still in use', heavy('free'))   # a busy model is left to finish
        self.assertEqual(len(asked), 1)
        p = self.gate('gpu', 'ol-share', 3)
        time.sleep(1.2)
        self.assertIn('Sharing the GPU with ol-share', heavy('status'))
        self.wait_all(p)
        self.settings({})
        self.assertNotIn('Ollama:', heavy('status'))

    def test_hook_wraps_only_what_a_rule_names(self):
        hg = load_gate()
        rules = [dict(r) for r in hg.STARTER_RULES]
        gate = hg.gate_prefix('bash', False)

        def rewrite(line, shell='bash', **kw):
            return hg.hook_rewrite(line, shell, rules=rules, **kw)
        self.assertEqual(rewrite('./gradlew build 2>&1 | tail -5'), gate + './gradlew build 2>&1 | tail -5')
        self.assertEqual(rewrite('cd app && dotnet publish -c Release > log.txt'),
                         'cd app && ' + gate + 'dotnet publish -c Release > log.txt')
        self.assertEqual(rewrite('JAVA_HOME=/x ./gradlew build'), 'JAVA_HOME=/x ' + gate + './gradlew build')
        self.assertEqual(rewrite('python -u tools/shot.py --w 1'), gate + 'python -u tools/shot.py --w 1')
        self.assertEqual(rewrite('./gradlew build; dotnet publish'), gate + './gradlew build; ' + gate + 'dotnet publish')
        self.assertIn(' auto -- ', rewrite('cargo build', wait=True))
        for compiles in ('cargo test -p core', 'cargo build --release'):
            self.assertEqual(rewrite(compiles), gate + compiles)   # 1.13.0: compilers that filled the RAM
        for light in ('dotnet build', 'dotnet test tests/X.Tests', 'msbuild x.sln', 'tar -czf x.tgz src', '7z a x.7z d',
                      'cmd /c build.cmd'):
            self.assertIsNone(rewrite(light), light)   # 1.17.0: light jobs run ungated
        ps = hg.gate_prefix('powershell', False)
        self.assertEqual(rewrite('& "F:\\b\\blender.exe" -b x', 'powershell'), ps + '"F:\\b\\blender.exe" -b x')
        self.assertEqual(rewrite('$env:X="1"; .\\gradlew.bat build', 'powershell'), '$env:X="1"; ' + ps + '.\\gradlew.bat build')
        for left_alone in ('git commit -m "fix gradle"', 'grep gradle build.gradle', 'echo blender', 'ls tar',
                           'gradle --version', 'dotnet --info', 'tar -tf x.tar', '7z l x.7z', 'cargo --version',
                           'heavy cpu -- ./gradlew build', 'pw run && dotnet build', 'echo $(./gradlew build)',
                           'if true; then (./gradlew build); fi', 'cat <<EOF\ngradle\nEOF', 'echo "unclosed'):
            self.assertIsNone(rewrite(left_alone), left_alone)
        # run_quiet: a stalled helper whose child holds the output is ended on time, tree and all
        code = 'import subprocess, sys; subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"]); print("up", flush=True); import time; time.sleep(60)'
        t = time.time()
        rc, text = hg.run_quiet([sys.executable, '-c', code], 3)
        self.assertEqual((rc, text.strip()), (124, 'up'))
        self.assertLess(time.time() - t, 30)
        # gated_tools: your own tools that take a lane themselves are left alone
        self.assertIsNone(rewrite('cargo build', gated=hg.GATED | {'cargo'}))
        self.assertEqual(rewrite('cargo build', gated=hg.GATED), gate + 'cargo build')
        self.assertIsNone(hg.hook_rewrite('dotnet publish', rules=[{'match': '', 'folder': 'Site', 'lane': 'cpu'}], cwd=r'F:\Site'))
        only_here = [{'match': 'make', 'folder': 'Site', 'lane': 'cpu'}]
        self.assertIsNotNone(hg.hook_rewrite('make all', rules=only_here, cwd=r'F:\Site'))
        self.assertIsNone(hg.hook_rewrite('make all', rules=only_here, cwd=r'F:\Other'))

    def test_hook_speaks_json_and_never_fails(self):
        def hook(text, **env):
            return subprocess.run([sys.executable, GATE, 'hook'], input=text, capture_output=True, text=True, timeout=30,
                                  env={**self.env, **env})
        call = {'tool_name': 'Bash', 'cwd': self.tmp, 'tool_input': {'command': 'dotnet publish', 'description': 'x',
                                                                     'timeout': 5000, 'run_in_background': True}}
        out = json.loads(hook(json.dumps(call)).stdout)['hookSpecificOutput']
        self.assertEqual(out['hookEventName'], 'PreToolUse')
        self.assertNotIn('permissionDecision', out)   # the agent's own permission checks still decide
        new = out['updatedInput']
        self.assertEqual((new['description'], new['timeout'], new['run_in_background']), ('x', 5000, True))
        self.assertTrue(new['command'].endswith(' auto -- dotnet publish'))
        call['tool_input']['run_in_background'] = False
        self.assertIn('--wait-max', json.loads(hook(json.dumps(call)).stdout)['hookSpecificOutput']['updatedInput']['command'])
        for quiet in ('not json', '{}', json.dumps({'tool_name': 'Write', 'tool_input': {'command': 'dotnet build'}})):
            r = hook(quiet)
            self.assertEqual((r.returncode, r.stdout), (0, ''), quiet)
        self.assertEqual(hook(json.dumps(call), HEAVYGATE='off').stdout, '')

    def test_rewritten_command_really_runs_on_its_lane(self):
        hg = load_gate()
        work = tempfile.mkdtemp(dir=self.tmp)
        name = 'gradlew.bat' if os.name == 'nt' else 'gradlew'
        with open(os.path.join(work, name), 'w', newline='') as f:
            f.write('@echo ran %1\r\n@exit /b 7\r\n' if os.name == 'nt' else '#!/bin/sh\necho ran $1\nexit 7\n')
        os.chmod(os.path.join(work, name), 0o755)
        line = hg.hook_rewrite('./gradlew build', 'bash', True, work, [{'match': 'gradlew', 'folder': '', 'lane': 'cpu'}])
        shell = shutil.which('bash')
        if os.name == 'nt':   # never the WSL one in System32
            shell = os.path.join(os.environ.get('ProgramFiles', r'C:\Program Files'), 'Git', 'bin', 'bash.exe')
            if not os.path.exists(shell):
                self.skipTest('Git Bash is not here')
        r = subprocess.run([shell, '-c', line], cwd=work, env=self.env, capture_output=True, text=True, timeout=60)
        self.assertIn('ran build', r.stdout, r.stderr)
        self.assertEqual(r.returncode, 7, r.stderr)
        self.assertIn('cpu lane', r.stderr)

    @windows_only
    def test_rules_window_drives_every_control(self):
        out = tempfile.mkdtemp(dir=self.tmp)
        r = subprocess.run([sys.executable, os.path.join(HERE, 'heavyrules.pyw'), '--selftest', out], capture_output=True,
                           text=True, timeout=120)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        with open(os.path.join(out, 'report.json')) as f:
            self.assertEqual(json.load(f)['failed'], [])

    def test_two_cpu_slots_run_two_jobs_at_once(self):
        self.settings({'cpu_slots': 2})
        try:
            a = self.gate('cpu', 'two-a', 2.0)
            time.sleep(0.6)
            t0 = time.time()
            b = self.gate('cpu', 'two-b', 0.1, '--no-wait')
            self.assertEqual(b.wait(timeout=20), 0)   # the second slot was free: no exit 75
            self.assertLess(time.time() - t0, 1.9)    # and it did not wait for two-a
            self.assertEqual(a.wait(timeout=20), 0)
        finally:
            self.settings({})

    def test_wait_max_gives_up_with_75(self):
        a = self.gate('cpu', 'wm-a', 3.0)
        time.sleep(0.6)
        b = self.gate('cpu', 'wm-b', 0.1, '--wait-max', '0.5')
        self.assertEqual(b.wait(timeout=20), 75)
        self.assertEqual(a.wait(timeout=20), 0)

    def test_no_wait_when_busy_exits_75(self):
        a = self.gate('cpu', 'nw-a', 1.5)
        time.sleep(0.6)
        b = self.gate('cpu', 'nw-b', 0.1, '--no-wait')
        self.assertEqual(b.wait(timeout=20), 75)
        self.assertEqual(a.wait(timeout=20), 0)
        self.assertFalse(os.path.exists(os.path.join(self.out, 'nw-b.json')))

    def test_short_on_ram_starts_after_the_wait_with_a_warning(self):
        self.settings({'expect_mb': {'gpu': 10 ** 7, 'cpu': 0}, 'ram_wait_min': 0.02})
        try:
            p = self.gate('gpu', 'ram', 0.1, capture=True)
            out = p.communicate(timeout=30)[0]
            self.assertEqual(p.returncode, 0)
            self.assertIn('waiting for RAM', out)
            with open(os.path.join(self.state, 'runs.jsonl')) as f:
                rows = [json.loads(x) for x in f if x.strip()]
            self.assertTrue(any(r['name'] == 'ram' and r['warning'] for r in rows))
        finally:
            self.settings({})

    def status(self, *extra):
        s = subprocess.run([sys.executable, GATE, 'status', *extra], env=self.env, capture_output=True, text=True,
                           timeout=60)
        return json.loads(s.stdout) if '--json' in extra else s.stdout

    def lane(self, snap, lane):
        return next(L for L in snap['lanes'] if L['lane'] == lane)

    def test_status_estimates_finish_from_past_runs(self):
        with open(os.path.join(self.state, 'runs.jsonl'), 'a', encoding='utf-8') as f:
            for secs in (100, 140, 120, 2):   # the 2 s failure doesn't count
                f.write(json.dumps({'name': 'Estimate probe 0.9.0 full suite', 'lane': 'gpu', 'cwd': '', 'command': 'x',
                                    'ran_s': secs, 'exit': 0 if secs > 5 else 1}) + '\n')
        a = self.gate('gpu', 'eta-a', 6.0, name='Estimate probe 1.0.0 full suite')
        time.sleep(0.8)
        b = self.gate('gpu', 'eta-b', 0.1, name='Estimate probe 1.0.1 full suite')
        time.sleep(0.8)
        text, snap = self.status(), self.status('--json')
        self.assertEqual(self.wait_all(a, b), [0, 0])
        gpu = self.lane(snap, 'gpu')
        self.assertEqual((gpu['job']['usual_s'], gpu['job']['runs']), (120, 3))   # matched across versions
        self.assertTrue(110 <= gpu['job']['left_s'] <= 120)
        self.assertEqual(gpu['queue'][0]['usual_s'], 120)
        self.assertGreater(snap['eta']['done_in_s'], 200)
        self.assertRegex(text, r'Estimate probe 1\.0\.0 full suite .*~\dm left \(usually 2m\)')
        self.assertIn('All done in ~4m, around', text)

    @windows_only
    def test_shutdown_when_done_switch(self):
        def run(*extra):
            return subprocess.run([sys.executable, GATE, 'shutdown-when-done', '--no-tray', *extra], env=self.env,
                                  capture_output=True, text=True, timeout=60).stdout
        flag = os.path.join(self.state, 'shutdown.json')
        self.assertIn('Shut down when done: ON', run())
        self.assertTrue(os.path.exists(flag))
        self.assertIn('Shut down when done: ON', self.status())
        self.assertIsNotNone(self.status('--json')['shutdown_when_done'])
        self.assertIn('off', run('--off'))
        self.assertFalse(os.path.exists(flag))
        self.assertNotIn('Shut down when done', self.status())

    def test_status_shows_running_and_queued(self):
        a = self.gate('gpu', 'st-a', 8.0, name='Status probe job')
        time.sleep(0.8)
        b = self.gate('gpu', 'st-b', 0.1, name='Status queued job')
        time.sleep(0.8)
        text, snap = self.status(), self.status('--json')
        self.assertEqual(self.wait_all(a, b), [0, 0])
        self.assertIn('GPU lane: 1 running, 1 queued', text)
        self.assertRegex(text, r'running\s+Status probe job')
        self.assertRegex(text, r'queued #1\s+Status queued job')
        self.assertIn('CPU lane: free', text)
        gpu = self.lane(snap, 'gpu')
        self.assertEqual((gpu['job']['name'], gpu['job']['state']), ('Status probe job', 'running'))
        self.assertEqual([(q['name'], q['state']) for q in gpu['queue']], [('Status queued job', 'queued')])

    def test_status_says_when_a_job_waits_for_ram(self):
        self.settings({'expect_mb': {'gpu': 0, 'cpu': 10 ** 7}, 'ram_wait_min': 0.08})
        try:
            a = self.gate('cpu', 'ramst', 0.1, name='Hungry build')
            time.sleep(1.5)
            text, snap = self.status(), self.status('--json')
            self.assertEqual(a.wait(timeout=30), 0)
        finally:
            self.settings({})
        self.assertIn('CPU lane: 1 waiting for RAM', text)
        self.assertRegex(text, r'waiting for RAM\s+Hungry build .*needs \d+\.\d GB, \d+\.\d GB free')
        self.assertIn('Waiting for RAM: the job has its lane', text)
        job = self.lane(snap, 'cpu')['job']
        self.assertEqual(job['state'], 'waiting for RAM')
        self.assertGreater(job['need_mb'], job['free_mb'])
        with open(os.path.join(self.state, 'runs.jsonl')) as f:
            row = [json.loads(x) for x in f if x.strip() and 'Hungry build' in x][-1]
        self.assertGreater(row['ram_wait_s'], 3)   # the 5 s wait is counted as RAM wait, not queue wait
        self.assertLess(row['lane_wait_s'], 2)

    def test_log_splits_queue_wait_from_ram_wait(self):
        a = self.gate('cpu', 'lg-a', 1.5)
        time.sleep(0.6)
        b = self.gate('cpu', 'lg-b', 0.1)
        self.assertEqual(self.wait_all(a, b), [0, 0])
        with open(os.path.join(self.state, 'runs.jsonl')) as f:
            row = [json.loads(x) for x in f if x.strip() and '"lg-b"' in x][-1]
        self.assertGreater(row['lane_wait_s'], 0.5)
        self.assertLess(row['ram_wait_s'], 1)
        out = subprocess.run([sys.executable, GATE, 'log'], env=self.env, capture_output=True, text=True).stdout
        self.assertIn('for RAM', out.splitlines()[0])

    def test_watch_redraws_until_stopped(self):
        p = subprocess.Popen([sys.executable, GATE, 'watch', '-s', '0.5'], env=self.env, stdout=subprocess.PIPE,
                             stderr=subprocess.STDOUT, text=True)
        time.sleep(4)
        p.kill()
        out = p.communicate(timeout=20)[0]
        self.assertGreaterEqual(out.count('GPU lane:'), 2)
        self.assertIn('Ctrl+C to stop', out)

    def peek(self):
        code = ('import json, sys; sys.path.insert(0, %r); import heavygate as hg; '
                'print(json.dumps({l: [hg.peek(l), hg.peek_queue(l)] for l in hg.LANES}))' % HERE)
        return json.loads(subprocess.run([sys.executable, '-c', code], env=self.env, capture_output=True, text=True,
                                         timeout=30).stdout)

    def test_peek_sees_holder_and_queue_then_a_free_lane(self):
        a = self.gate('cpu', 'pk-a', 2.5, name='Peek holder')
        time.sleep(0.8)
        b = self.gate('cpu', 'pk-b', 0.1, name='Peek waiter')
        time.sleep(0.8)
        during = self.peek()
        self.assertEqual(self.wait_all(a, b), [0, 0])
        after = self.peek()
        self.assertEqual(during['cpu'][0]['name'], 'Peek holder')
        self.assertEqual([r['name'] for r in during['cpu'][1]], ['Peek waiter'])
        self.assertIsNone(during['gpu'][0])
        self.assertEqual(after['cpu'], [None, []])

    def test_peek_ignores_a_dead_holders_record(self):
        dead = subprocess.Popen([sys.executable, '-c', 'pass'])
        dead.wait()
        with open(os.path.join(self.state, 'gpu.lock'), 'w') as f:
            json.dump({'name': 'Crashed job', 'pid': dead.pid, 'queued_ts': time.time() - 5}, f)
        self.assertIsNone(self.peek()['gpu'][0])

    def test_peek_never_makes_a_job_wait(self):
        stop = os.path.join(self.tmp, 'stop-peeking')
        code = ('import os, sys; sys.path.insert(0, %r); import heavygate as hg\n'
                'while not os.path.exists(%r): hg.peek("gpu"); hg.peek_queue("gpu")' % (HERE, stop))
        with open(os.path.join(self.state, 'gpu.lock'), 'w') as f:
            f.write('{}')
        grab = ('import sys; sys.path.insert(0, %r); import os, heavygate as hg\n'
                'clash = 0\n'
                'for _ in range(3000):\n'
                '    fd = hg._open_excl(hg.lock_path("gpu"))\n'
                '    if fd is None: clash += 1\n'
                '    else: os.close(fd)\n'
                'print(clash)' % HERE)
        spinner = subprocess.Popen([sys.executable, '-c', code], env=self.env)
        try:
            time.sleep(0.5)
            clashes = subprocess.run([sys.executable, '-c', grab], env=self.env, capture_output=True, text=True,
                                     timeout=60).stdout.strip()
        finally:
            open(stop, 'w').close()
            spinner.wait(timeout=20)
        self.assertEqual(clashes, '0')   # a job taking the lane never finds it held by a monitor

    @windows_only
    def test_tray_shows_the_lanes(self):
        a = self.gate('gpu', 'tray-a', 3.0, name='Tray probe job')
        time.sleep(0.8)
        out = os.path.join(self.tmp, 'tray')
        p = subprocess.run([sys.executable, os.path.join(HERE, 'heavytray.pyw'), '--selftest', out], env=self.env,
                           capture_output=True, text=True, timeout=60)
        self.assertEqual(a.wait(timeout=20), 0)
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        with open(os.path.join(out, 'report.json'), encoding='utf-8') as f:
            r = json.load(f)
        self.assertTrue(r['added'] and r['removed'])
        self.assertIn('GPU: Tray probe job', r['tooltip'])
        self.assertIn('CPU: free', r['tooltip'])
        self.assertLessEqual(len(r['tooltip']), 127)
        self.assertTrue(any('running: Tray probe job' in (x or '') for x in r['menu']))
        self.assertEqual(r['look'][0], ['running', False])
        self.assertTrue(any('Tray probe job' in x for x in r['strip_now']))   # the strip names the real job
        s = r['strip']
        self.assertTrue(s['idle']['withdrawn'] and not s['idle']['shown'])   # hidden when both lanes are idle
        for k in ('busy', 'ram'):
            self.assertTrue(s[k]['shown'] and s[k]['printed'])
            self.assertGreater(s[k]['bright_px'], 200)   # it really drew text, not a black box
        self.assertIn('waiting for RAM: needs 4.2 GB, 3.2 free', s['ram']['lines'][1])
        self.assertIn('next: GameDemo race check, MapDemo shot', s['busy']['lines'][1])
        for k in ('busy', 'ram'):
            self.assertGreater(s[k]['graph_px'], 40)   # the CPU/GPU/RAM lines are drawn
        self.assertEqual(s['no-graphs']['graph_px'], 0)   # and gone when the graphs are switched off
        self.assertLess(s['no-graphs']['h'], s['busy']['h'])
        f = r['fade']
        self.assertLess(f['early'], f['faded_in'])   # it fades in rather than popping up
        self.assertAlmostEqual(f['faded_in'], r['rest_alpha'], places=2)
        self.assertEqual(f['hovered'], 1.0)   # solid under the mouse
        self.assertTrue(f['during_fade_out'] and f['withdrawn'] and f['after_fade_out'] == 0.0)
        z = r['z']
        self.assertTrue(z['topmost_at_start'] and z['topmost_after_idle'])
        self.assertFalse(z['topmost_when_behind'])   # "send behind" really drops it under other windows
        self.assertTrue(z['dismissed_stays_hidden'] and z['shown_after_idle'])
        p = r['power']   # shut down when done, run with a stand-in command in a throwaway folder
        self.assertEqual(p['idle']['phase'], 'idle')
        self.assertAlmostEqual(p['idle']['secs'], 15, delta=0.5)   # 10 s of empty lanes + a 5 s countdown
        self.assertTrue(p['busy_resets'])   # a job arriving starts the wait over
        self.assertEqual(p['countdown']['phase'], 'countdown')
        self.assertTrue(p['shown'] and p['bright_px'] > 100)   # the countdown shows even with the lanes idle
        self.assertIn('SHUTTING DOWN in', p['lines'][0])
        self.assertTrue(p['cancelled'])   # a click on the strip cancels it
        self.assertTrue(p['ran'] and p['off_after'])   # it ran the command, and the switch went off first
        self.assertIn('~1h06m left', s['busy']['lines'][0])
        self.assertIn('All done around 23:41', ' '.join(s['busy']['lines']))
        if os.path.exists(os.path.join(os.environ.get('SystemRoot', r'C:\Windows'), 'System32', 'nvml.dll')):
            self.assertIsInstance(r['gpu_reading'], int)   # GPU use read through NVML, no nvidia-smi
        tb = r['taskbar']   # the taskbar readout, drawn but never docked by the selftest
        self.assertTrue(tb['app_starts_it'] and tb['same_width_any_values'])
        self.assertTrue(tb['default_is_strip'])   # the pop-up strip is the default display
        self.assertTrue(tb['taskbar_choice'] and tb['strip_hidden_with_taskbar'])   # one display or the other
        self.assertTrue(tb['strip_back_with_strip'])
        for th in ('dark', 'light'):
            self.assertGreater(tb[th]['run_px'], 20)    # GPU value green: a job runs on the GPU lane
            self.assertGreater(tb[th]['wait_px'], 20)   # CPU value amber: its job waits for RAM
            self.assertGreater(tb[th]['label_px'], 20)
            self.assertGreaterEqual(tb[th]['min_alpha'], 1)   # every pixel takes clicks, none fall through
            self.assertGreater(tb[th]['hover_alpha'], tb[th]['rest_alpha'])
        for name in ('idle-16.png', 'gpu-running-queued_cpu-ram-64.png', 'taskbar-dark.png'):
            with open(os.path.join(out, name), 'rb') as f:
                self.assertEqual(f.read(8), b'\x89PNG\r\n\x1a\n')

    @windows_only
    def test_stale_gpu_handle_cannot_break_the_tray(self):
        code = ('import importlib.machinery, importlib.util, ctypes, json, os, sys, time\n'
                'sys.path.insert(0, %r)\n'
                'path = os.path.join(%r, "heavytray.pyw")\n'
                'ld = importlib.machinery.SourceFileLoader("heavytray", path)\n'
                'm = importlib.util.module_from_spec(importlib.util.spec_from_loader("heavytray", ld))\n'
                'ld.exec_module(m)\n'
                'me = m.Meters(); had = bool(me.nvml); out = {"nvml": had}\n'
                'if had:\n'
                '    me.dev = ctypes.c_void_p(0x29811D387C8)\n'   # the bad handle from a real tray freeze
                '    out["bad"] = me.gpu(); out["dropped"] = me.nvml is None\n'
                '    out["later"] = me.gpu()\n'
                '    me.retry_at = time.time() - 1\n'
                '    out["back"] = me.gpu(); out["again"] = me.nvml is not None\n'
                'me.sample(); out["rows"] = len(me.hist)\n'
                'print(json.dumps(out))' % (HERE, HERE))
        p = subprocess.run([sys.executable, '-c', code], env=self.env, capture_output=True, text=True, timeout=60)
        self.assertEqual(p.returncode, 0, p.stderr)
        r = json.loads(p.stdout.strip().splitlines()[-1])
        self.assertEqual(r['rows'], 1)
        if r['nvml']:
            self.assertIsNone(r['bad'])
            self.assertTrue(r['dropped'])
            self.assertIsNone(r['later'])   # no retry before its time
            self.assertTrue(r['again'])
            self.assertIsInstance(r['back'], int)   # and it reconnects by itself

    @windows_only
    def test_start_at_sign_in_adds_and_removes_the_shortcut(self):
        appdata = os.path.join(self.tmp, 'appdata')
        env = dict(self.env, APPDATA=appdata)
        code = ('import json, os, sys; sys.path.insert(0, %r); import heavygate as hg\n'
                'on = hg.set_startup(True); lnk = hg.startup_shortcut()\n'
                'cmd = os.popen("powershell -NoProfile -Command \\"(New-Object -ComObject WScript.Shell)'
                '.CreateShortcut(\'" + lnk + "\').Arguments\\"").read().strip()\n'
                'off = hg.set_startup(False)\n'
                'print(json.dumps({"on": on, "off": off, "lnk": lnk, "args": cmd}))' % HERE)
        r = json.loads(subprocess.run([sys.executable, '-c', code], env=env, capture_output=True, text=True,
                                      timeout=120).stdout)
        self.assertTrue(r['lnk'].startswith(appdata))
        self.assertTrue(r['on'])
        self.assertIn('heavytray.pyw', r['args'])
        self.assertFalse(r['off'])

    def test_status_peek_shows_the_lanes_without_disturbing_them(self):
        def peek():
            out = subprocess.run([sys.executable, GATE, 'status', '--json', '--peek'], env=self.env, capture_output=True,
                                 text=True, timeout=20)
            return {L['lane']: L for L in json.loads(out.stdout)['lanes']} if out.returncode == 0 else {}
        a = self.gate('cpu', 'peek-a', 10)
        end = time.time() + 15   # a slow runner can take seconds to start a job: wait until it really holds the lane
        while time.time() < end and ((peek().get('cpu') or {}).get('job') or {}).get('name') != 'peek-a':
            time.sleep(0.2)
        b = self.gate('cpu', 'peek-b', 0.2)
        end = time.time() + 15
        while time.time() < end and not (peek().get('cpu') or {}).get('queue'):
            time.sleep(0.2)
        for _ in range(3):
            out = subprocess.run([sys.executable, GATE, 'status', '--json', '--peek'], env=self.env, capture_output=True,
                                 text=True, timeout=20)
            self.assertEqual(out.returncode, 0, out.stderr)
            lanes = {L['lane']: L for L in json.loads(out.stdout)['lanes']}
            self.assertEqual(lanes['cpu']['job']['name'], 'peek-a')
            self.assertEqual([j['name'] for j in lanes['cpu']['queue']], ['peek-b'])
            self.assertIsNone(lanes['gpu']['job'])
        self.assertEqual(self.wait_all(a, b), [0, 0], 'polling must not make a job miss its turn')

    def test_default_cpu_share_is_three_quarters(self):
        self.settings({'cpu_threads': 0})
        try:
            self.assertEqual(self.gate('cpu', 'share', 0.1).wait(timeout=20), 0)
        finally:
            self.settings({})
        self.assertEqual(self.rec('share')['threads'], -(-3 * os.cpu_count() // 4))

    def test_quoted_arguments_reach_the_job(self):
        out = os.path.join(self.out, 'args.json')
        code = 'import json, sys; json.dump(sys.argv[1:], open(sys.argv[1], "w"))'
        p = subprocess.run([sys.executable, GATE, 'cpu', '-n', 'args', '--', sys.executable, '-c', code, out, 'two words'],
                           env=self.env, capture_output=True, text=True, timeout=30)
        self.assertEqual(p.returncode, 0, p.stderr)
        with open(out) as f:
            self.assertEqual(json.load(f), [out, 'two words'])

    def test_abandoned_ticket_is_cleared(self):
        d = os.path.join(self.state, 'queue', 'cpu')
        os.makedirs(d, exist_ok=True)
        with open(os.path.join(d, '00000000000000000001-1.ticket'), 'w') as f:
            f.write('{}')
        self.assertEqual(self.gate('cpu', 'stale', 0.1).wait(timeout=20), 0)

    FAST_STALL = {'stall_min': 0.02, 'stall_cpu_s': 0.5, 'stall_check_s': 0.2}   # flagged after about 1.2 s idle

    def test_idle_job_is_flagged_and_stop_frees_the_lane(self):
        self.settings(self.FAST_STALL)
        self.addCleanup(self.settings, {})
        p = self.gate('cpu', 'stuck', 120, capture=True)
        self.addCleanup(subprocess.run, [sys.executable, GATE, 'stop', 'cpu'], env=self.env, capture_output=True, timeout=30)
        job, end = None, time.time() + 20
        while time.time() < end:
            job = self.lane(self.status('--json'), 'cpu')['job']
            if job and job.get('stalled_s') is not None:
                break
            time.sleep(0.2)
        self.assertIsNotNone(job and job.get('stalled_s'), job)
        waiter = self.gate('cpu', 'behind stuck', 0.1)
        end = time.time() + 20
        while time.time() < end and not self.lane(self.status('--json'), 'cpu')['queue']:
            time.sleep(0.2)
        text = subprocess.run([sys.executable, GATE, 'status'], env=self.env, capture_output=True, text=True, timeout=30).stdout
        self.assertIn('STALLED on the cpu lane: stuck', text)
        self.assertIn('heavy stop cpu', text)
        self.assertIn('1 waiting behind it', text)
        s = subprocess.run([sys.executable, GATE, 'stop', 'cpu'], env=self.env, capture_output=True, text=True, timeout=30)
        self.assertEqual(s.returncode, 0, s.stdout + s.stderr)
        self.assertEqual(waiter.wait(timeout=30), 0)   # the job behind it got the lane
        p.wait(timeout=10)
        p.stdout.close()
        self.assertFalse(os.path.exists(os.path.join(self.out, 'stuck.json')))   # stopped, never finished

    def test_stuck_job_that_recovers_says_so_in_the_log(self):
        self.settings(self.FAST_STALL)
        self.addCleanup(self.settings, {})
        self.assertEqual(self.gate('cpu', 'slow start', 3).wait(timeout=30), 0)
        r = subprocess.run([sys.executable, GATE, 'log', '-n', '1'], env=self.env, capture_output=True, text=True, timeout=30)
        self.assertIn('slow start', r.stdout)
        self.assertIn('stalled', r.stdout)

    def test_busy_job_is_never_flagged(self):
        self.settings(self.FAST_STALL)
        self.addCleanup(self.settings, {})
        spin = os.path.join(self.tmp, 'spin.py')
        with open(spin, 'w') as f:
            f.write('import time\nend = time.time() + 8\nwhile time.time() < end:\n    pass\n')   # 2-core runners: room for 3 looks
        p = subprocess.Popen([sys.executable, GATE, 'cpu', '-n', 'spinning', '--', sys.executable, spin], env=self.env,
                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        seen = 0
        while p.poll() is None:
            job = self.lane(self.status('--json'), 'cpu')['job']
            if job and job['state'] == 'running':
                seen += 1
                self.assertNotIn('stalled_s', job)
            time.sleep(0.3)
        self.assertGreater(seen, 2)

    def test_stop_with_nothing_running_says_so(self):
        s = subprocess.run([sys.executable, GATE, 'stop', 'gpu'], env=self.env, capture_output=True, text=True, timeout=30)
        self.assertEqual(s.returncode, 1)
        self.assertIn('nothing is running', s.stdout)

    def test_usual_time_ignores_failed_and_instant_runs(self):
        with open(os.path.join(self.state, 'runs.jsonl'), 'a') as f:
            for ran, code in ((6, 1), (0.2, 0), (300, 0)):
                f.write(json.dumps({'name': 'Odd probe 1.0.0', 'lane': 'cpu', 'ran_s': ran, 'exit': code}) + '\n')
        code = "import heavygate as h; print(h.usual_secs('Odd probe 2.0.0'))"
        r = subprocess.run([sys.executable, '-c', code], cwd=os.path.dirname(os.path.abspath(GATE)), env=self.env,
                           capture_output=True, text=True, timeout=30)
        self.assertEqual(r.stdout.strip(), '(300, 1)', r.stderr)

    EMBEDDED = ("import subprocess, sys, heavygate as hg\n"
                "with hg.hold('cpu', 'embedded runner') as slot:\n"
                "    with slot.spawning():\n"
                "        p = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(120)'], **slot.popen)\n"
                "    slot.exit = p.wait()\n"
                "print('runner carried on')\n")

    def embedded(self):
        return subprocess.Popen([sys.executable, '-c', self.EMBEDDED], cwd=HERE, env=self.env, stdout=subprocess.PIPE,
                                stderr=subprocess.STDOUT, text=True)

    def wait_for_stall(self):
        job, end = None, time.time() + 20
        while time.time() < end:
            job = self.lane(self.status('--json'), 'cpu')['job']
            if job and job.get('stalled_s') is not None:
                return job
            time.sleep(0.2)
        self.fail(f'never flagged: {job}')

    def test_program_holding_a_lane_itself_is_watched_too(self):
        self.settings(self.FAST_STALL)
        self.addCleanup(self.settings, {})
        p = self.embedded()
        self.addCleanup(subprocess.run, [sys.executable, GATE, 'stop', 'cpu'], env=self.env, capture_output=True, timeout=30)
        self.assertEqual(self.wait_for_stall()['name'], 'embedded runner')
        subprocess.run([sys.executable, GATE, 'stop', 'cpu'], env=self.env, capture_output=True, timeout=30)
        p.wait(timeout=20)
        p.stdout.close()

    def test_dead_job_with_one_waiting_is_ended(self):
        self.settings(dict(self.FAST_STALL, stall_stop_min=0.05))
        self.addCleanup(self.settings, {})
        p = self.gate('cpu', 'dead job', 120)
        self.addCleanup(subprocess.run, [sys.executable, GATE, 'stop', 'cpu'], env=self.env, capture_output=True, timeout=30)
        self.wait_for_stall()
        waiter = self.gate('cpu', 'behind dead job', 0.1)
        self.assertEqual(waiter.wait(timeout=30), 0)
        p.wait(timeout=10)
        self.assertFalse(os.path.exists(os.path.join(self.out, 'dead job.json')))
        r = subprocess.run([sys.executable, GATE, 'log', '-n', '2'], env=self.env, capture_output=True, text=True, timeout=30)
        self.assertIn('ended to free the lane', r.stdout)

    def test_ending_a_stuck_child_leaves_the_program_holding_the_lane_running(self):
        self.settings(dict(self.FAST_STALL, stall_stop_min=0.05))
        self.addCleanup(self.settings, {})
        p = self.embedded()
        self.addCleanup(subprocess.run, [sys.executable, GATE, 'stop', 'cpu'], env=self.env, capture_output=True, timeout=30)
        self.wait_for_stall()
        waiter = self.gate('cpu', 'behind embedded', 0.1)
        self.assertEqual(waiter.wait(timeout=30), 0)
        out, _ = p.communicate(timeout=20)
        self.assertIn('runner carried on', out)

    def test_idle_job_with_nobody_waiting_is_left_alone(self):
        self.settings(dict(self.FAST_STALL, stall_stop_min=0.03))
        self.addCleanup(self.settings, {})
        self.assertEqual(self.gate('cpu', 'alone and idle', 4).wait(timeout=30), 0)
        self.assertTrue(os.path.exists(os.path.join(self.out, 'alone and idle.json')))

    @windows_only
    def test_tray_draws_a_stalled_job_in_red(self):
        out = os.path.join(self.tmp, 'tray-stall')
        p = subprocess.run([sys.executable, os.path.join(HERE, 'heavytray.pyw'), '--selftest', out], env=self.env,
                           capture_output=True, text=True, timeout=60)
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        with open(os.path.join(out, 'report.json'), encoding='utf-8') as f:
            s = json.load(f)['strip']['stalled']
        self.assertTrue(any('STALLED' in x for x in s['lines']), s['lines'])
        self.assertTrue(s['shown'])


class RamCeilingTests(unittest.TestCase):
    """1.13.0: RAM in use never passes ram_ceiling_pct. Real RAM is used only a little above what is in use now."""
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp(prefix='heavygate-ram-')
        cls.state = os.path.join(cls.tmp, 'state')
        os.makedirs(cls.state)
        cls.hog = os.path.join(cls.tmp, 'hog.py')
        with open(cls.hog, 'w') as f:
            f.write(HOG)
        cls.env = dict(os.environ, HEAVYGATE_DIR=cls.state, HEAVYGATE_GUARD='off')
        cls.env.pop('HEAVYGATE_HELD', None)
        cls.env.pop('HEAVYGATE', None)
        cls.hg = load_gate()

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def settings(self, **extra):
        s = {'expect_mb': {'gpu': 300, 'cpu': 300}, 'reserve_mb': 0, 'free_daemons': False, 'free_ollama': False,
             'ollama_url': 'http://127.0.0.1:9', 'poll_s': 0.2}
        s.update(extra)
        with open(os.path.join(self.state, 'settings.json'), 'w') as f:
            json.dump(s, f)

    def near_now(self, above):
        """A ceiling `above` percent over the RAM in use now, so a test never takes the PC anywhere near full."""
        return int(self.hg.ram_load(85)[0]) + above

    def log(self):
        with open(os.path.join(self.state, 'runs.jsonl'), encoding='utf-8') as f:
            return json.loads(f.readlines()[-1])

    @windows_only
    def test_job_object_caps_a_job_at_the_ceiling(self):
        c = self.near_now(6)
        self.settings(ram_ceiling_pct=c, ram_kill_pct=c + 30)
        r = subprocess.run([sys.executable, GATE, 'cpu', '-n', 'capped hog', '--', sys.executable, self.hog, '8000',
                            '0.03', '0'], env=self.env, capture_output=True, text=True, timeout=120)
        self.assertEqual(r.returncode, 3, r.stdout + r.stderr)
        self.assertIn('MemoryError', r.stdout)
        rec = self.log()
        self.assertTrue(rec['ram_cap_mb'])
        self.assertIn('hit its RAM cap', rec['warning'])
        # The job really stopped at the cap (MemoryError above). Windows' peak counter can read a little past a small
        # cap: a GitHub runner read 609 MB at the 512 MB floor, so allow 10% or 128 MB, whichever is more.
        self.assertLess(rec['job_peak_mb'], max(rec['ram_cap_mb'] * 1.1, rec['ram_cap_mb'] + 128))

    def test_over_the_ceiling_the_job_is_ended(self):
        """RamGuard.look with the RAM reading made up: over the ceiling for ram_over_s, the job's tree ends."""
        hg = self.hg
        s = dict(hg.DEFAULTS, ram_ceiling_pct=85, ram_kill_pct=90, ram_over_s=3, guard_min_mb=0)
        p = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)'])
        self.addCleanup(lambda: p.poll() is None and p.kill())
        slot = hg.Slot('cpu', 'test', nested=True)
        g = hg.RamGuard(os.getpid(), slot, 'test', lambda *_: None, s)
        g.watch(p)
        real = hg.ram_load
        self.addCleanup(setattr, hg, 'ram_load', real)
        hg.ram_load = lambda c: (80.0, 1000, 2000)
        self.assertIsNone(g.look(100.0, None))
        hg.ram_load = lambda c: (87.0, -300, 2000)
        since = g.look(100.0, None)
        self.assertEqual(since, 100.0)
        self.assertIsNone(p.poll(), 'ended before ram_over_s')
        g.look(103.5, since)
        self.assertIsNotNone(p.wait(timeout=10))
        self.assertIn('ended at 87% RAM', g.ended)
        p2 = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)'])
        self.addCleanup(lambda: p2.poll() is None and p2.kill())
        g2 = hg.RamGuard(os.getpid(), slot, 'test', lambda *_: None, s)
        g2.watch(p2)
        hg.ram_load = lambda c: (91.0, -900, 2000)
        g2.look(200.0, None)   # at ram_kill_pct: at once
        self.assertIsNotNone(p2.wait(timeout=10))

    def test_a_small_job_is_not_blamed(self):
        hg = self.hg
        s = dict(hg.DEFAULTS, guard_min_mb=10 ** 6)
        p = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(30)'])
        self.addCleanup(lambda: p.poll() is None and p.kill())
        g = hg.RamGuard(os.getpid(), hg.Slot('cpu', 't', nested=True), 't', lambda *_: None, s)
        g.watch(p)
        real = hg.ram_load
        self.addCleanup(setattr, hg, 'ram_load', real)
        hg.ram_load = lambda c: (95.0, -2000, 2000)
        g.look(1.0, None)
        time.sleep(0.5)
        self.assertIsNone(p.poll())
        self.assertIsNone(g.ended)

    def test_guard_groups_each_agent_command(self):
        hg = self.hg
        s = dict(hg.DEFAULTS, guard_min_mb=300, guard_big_mb=2000, guard_agents=['agentcli.exe', 'otheragent'])
        table = {   # pid: (parent, name, start)
            10: (1, 'agentcli', 100.0), 11: (10, 'agentcli', 101.0),
            20: (11, 'bash', 200.0), 21: (20, 'python', 201.0), 22: (21, 'cargo', 202.0), 23: (22, 'rustc', 203.0),
            30: (11, 'node', 150.0),                                   # an MCP server
            40: (1, 'explorer', 50.0), 41: (40, 'rustc', 300.0),       # the user's own build: never the guard's
            50: (11, 'pwsh', 400.0), 51: (50, 'dotnet', 401.0),
            60: (999, 'cmd', 500.0),                                   # parent gone
            70: (12, 'msbuild', 10.0), 12: (1, 'otheragent', 600.0),        # pid 12 is newer than its 'child': a reused id
        }
        ws = {22: 50, 23: 2600, 21: 20, 20: 5, 30: 900, 41: 5000, 51: 250, 70: 4000, 10: 3000, 11: 400}
        jobs = hg.agent_jobs(s, table, ws)
        by = {g['pid']: g for g in jobs}
        self.assertEqual(sorted(by), [20, 30, 50])
        self.assertEqual(by[20]['mb'], 2675)
        self.assertTrue(by[20]['shell'])
        self.assertEqual(by[20]['limit'], 300)
        self.assertEqual(by[30]['limit'], 2000)
        self.assertEqual(jobs[0]['pid'], 20)
        self.assertEqual(by[20]['procs'][0], ('rustc', 2600))

    def test_guard_spares_the_browser_an_agent_app_opened(self):
        """Chrome opened from a link in an agent app's window is a child of that app, but it is the user's browser
        (2026-11-16: the guard ended a 7.5 GB Chrome twice during a server build). A browser a command started stays
        that command's."""
        hg = self.hg
        s = dict(hg.DEFAULTS, guard_min_mb=300, guard_big_mb=2000, guard_agents=['agentcli'])
        table = {
            10: (1, 'agentcli', 100.0),
            80: (10, 'chrome', 700.0), 81: (80, 'chrome', 701.0), 82: (80, 'chrome-native-host', 702.0),
            90: (10, 'cmd', 800.0), 91: (90, 'python', 801.0), 92: (91, 'msedge', 802.0),   # headless Edge from a script
        }
        ws = {80: 3000, 81: 4500, 82: 10, 90: 5, 91: 100, 92: 2500}
        by = {g['pid']: g for g in hg.agent_jobs(s, table, ws)}
        self.assertEqual(sorted(by), [90])
        self.assertEqual(by[90]['mb'], 2605)

    def test_waits_rather_than_start_past_the_ceiling(self):
        hg = self.hg
        s = dict(hg.DEFAULTS, ram_ceiling_pct=85, ram_wait_min=0, reserve_mb=768, free_daemons=False, free_ollama=False)
        real = hg.mem_mb
        self.addCleanup(setattr, hg, 'mem_mb', real)
        hg.mem_mb = lambda: (3000, 16000, 0, 0)    # 2400 kept by the ceiling: 600 left, under a 2500 MB job
        said = []
        out = {}
        t = threading.Thread(target=lambda: out.update(r=hg._admit('cpu', 'big', 2500, s, said.append)), daemon=True)
        t.start()
        t.join(6)
        self.assertTrue(t.is_alive(), 'started anyway past the RAM ceiling')
        self.assertTrue(any('will not start anyway' in x for x in said), said)
        hg.mem_mb = lambda: (8000, 16000, 0, 0)
        t.join(10)
        self.assertFalse(t.is_alive())
        self.assertEqual(out['r'][0], 8000)

    @windows_only
    def test_guard_ends_an_agents_runaway_command(self):
        """The machine guard, for real: a stand-in agent (a copy of cmd.exe) starts a hog with no lane at all."""
        agent = os.path.join(self.tmp, 'testagent.exe')
        shutil.copy(os.path.join(os.environ.get('SystemRoot', r'C:\Windows'), 'System32', 'cmd.exe'), agent)
        c = self.near_now(6)
        self.settings(ram_ceiling_pct=c, ram_kill_pct=c + 4, guard_agents=['testagent.exe'], guard_big_mb=500)
        g = subprocess.Popen([sys.executable, GATE, 'guard'], env=self.env)
        self.addCleanup(lambda: g.poll() is None and g.kill())
        time.sleep(1.5)
        t0 = time.time()
        r = subprocess.run(f'"{agent}" /c ""{sys.executable}" "{self.hog}" 6000 0.05 30"', capture_output=True,
                           text=True, timeout=120)
        self.assertNotIn('hog finished', r.stdout)
        self.assertNotIn('MemoryError', r.stdout)
        self.assertNotEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertLess(time.time() - t0, 25)
        time.sleep(1.5)
        with open(os.path.join(self.state, 'guard.log'), encoding='utf-8') as f:
            text = f.read()
        self.assertIn('ENDED testagent command', text)


class DiskTests(unittest.TestCase):
    """1.16.0: a job never fills the drive it builds on. A floor in the billions of GB stands in for a full drive."""
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp(prefix='heavygate-disk-')
        cls.state = os.path.join(cls.tmp, 'state')
        os.makedirs(cls.state)
        cls.env = dict(os.environ, HEAVYGATE_DIR=cls.state, HEAVYGATE_GUARD='off', HEAVYGATE_CLOUD='off')
        cls.env.pop('HEAVYGATE_HELD', None)
        cls.env.pop('HEAVYGATE', None)
        cls.hg = load_gate()

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def settings(self, **extra):
        s = {'expect_mb': {'gpu': 1, 'cpu': 1}, 'reserve_mb': 0, 'free_daemons': False, 'free_ollama': False,
             'ollama_url': 'http://127.0.0.1:9', 'poll_s': 0.2, 'cloud': False, 'ram_ceiling_pct': 100,
             'ram_kill_pct': 101}
        s.update(extra)
        with open(os.path.join(self.state, 'settings.json'), 'w') as f:
            json.dump(s, f)

    def run_job(self, code, name='disk job'):
        return subprocess.run([sys.executable, GATE, 'cpu', '-n', name, '--', sys.executable, '-c', code],
                              cwd=self.tmp, env=self.env, capture_output=True, text=True, timeout=120)

    def log(self):
        with open(os.path.join(self.state, 'runs.jsonl'), encoding='utf-8') as f:
            return json.loads(f.readlines()[-1])

    def test_drives_a_job_writes_to(self):
        hg = self.hg
        if WIN:
            self.assertEqual(hg.drive_of(r'c:\some\project'), 'C:\\')
        drives = hg.watched_drives(self.tmp, dict(hg.DEFAULTS, disk_paths=[self.tmp]))
        self.assertEqual(drives[0], hg.drive_of(self.tmp))
        self.assertEqual(len(drives), len({os.path.normcase(d) for d in drives}))   # each drive once
        free = hg.disk_free(drives)
        self.assertTrue(all(g > 0 for g in free.values()))
        self.assertEqual(hg.disk_short({'F:\\': 9.2, 'C:\\': 55.0}, 15), [('F:\\', 9.2)])

    def test_a_job_does_not_start_under_the_floor(self):
        self.settings(disk_floor_gb=10 ** 9)
        r = self.run_job('print("RAN")')
        self.assertEqual(r.returncode, 28, r.stdout + r.stderr)
        self.assertNotIn('RAN', r.stdout)
        self.assertIn('NOT STARTED', r.stderr)
        rec = self.log()
        self.assertEqual(rec['exit'], 28)
        self.assertTrue(rec['warning'].startswith('not started:'), rec['warning'])
        st = subprocess.run([sys.executable, GATE, 'status'], env=self.env, capture_output=True, text=True, timeout=60)
        self.assertIn('UNDER THE FLOOR', st.stdout)
        with self.assertRaises(OSError) as e:   # from Python: hold() raises ENOSPC, the lane is free again
            with self.hg_hold():
                pass
        self.assertEqual(e.exception.errno, errno.ENOSPC)
        self.assertIsNone(self.hg.peek('cpu'))

    def hg_hold(self):
        old = os.environ.get('HEAVYGATE_DIR')
        os.environ['HEAVYGATE_DIR'] = self.state
        self.addCleanup(lambda: os.environ.__setitem__('HEAVYGATE_DIR', old) if old is not None
                        else os.environ.pop('HEAVYGATE_DIR', None))
        return self.hg.hold('cpu', 'in-process', say=lambda *_: None)

    def test_a_job_is_ended_when_its_drive_falls_under_the_stop_line(self):
        self.settings(disk_floor_gb=0, disk_stop_gb=10 ** 9)
        t0 = time.time()
        r = self.run_job('import time; print("STARTED", flush=True); time.sleep(60)')
        self.assertEqual(r.returncode, 28, r.stdout + r.stderr)
        self.assertIn('STARTED', r.stdout)
        self.assertLess(time.time() - t0, 40)
        self.assertIn('ENDED', r.stderr)
        self.assertIn('ended:', self.log()['warning'])

    def test_the_log_keeps_the_drive_and_its_free_space(self):
        self.settings(disk_floor_gb=0, disk_stop_gb=0)
        r = self.run_job('open("blob.bin", "wb").write(b"x" * 1024)')
        self.assertEqual(r.returncode, 0, r.stderr)
        rec = self.log()
        self.assertEqual(rec['disk_drive'], self.hg.drive_of(self.tmp))
        self.assertIn(rec['disk_drive'], rec['disk_free_start_gb'])
        self.assertIn(rec['disk_drive'], rec['disk_free_end_gb'])
        self.assertIsNotNone(rec['disk_used_gb'])
        st = subprocess.run([sys.executable, GATE, 'status'], env=self.env, capture_output=True, text=True, timeout=60)
        self.assertIn('Disk: ', st.stdout)
        self.assertIn('is ended under 0 GB', st.stdout)


FAKE_GH = r'''
import hashlib, json, os, re, sys, time
d = os.environ['FAKEGH_DIR']
a = sys.argv[1:]
with open(os.path.join(d, 'calls.txt'), 'a') as f:
    f.write(' '.join(a) + '\n')
def read(name, default):
    p = os.path.join(d, name)
    return json.load(open(p)) if os.path.exists(p) else default
def write(name, data):
    json.dump(data, open(os.path.join(d, name), 'w'))
mode = read('mode.json', 'ok')
path = next((x for x in a[1:] if x.startswith('repos/')), '') if a and a[0] == 'api' else ''
m_list = re.match(r'repos/([^/]+/[^/]+)/actions/artifacts\?', path)
m_one = re.match(r'repos/([^/]+/[^/]+)/actions/artifacts/(\d+)$', path)
m_run = re.match(r'repos/([^/]+/[^/]+)/actions/runs/(\d+)/artifacts$', path)
if a[:2] == ['api', 'user']:
    print(json.dumps({'login': 'you', 'plan': {'name': 'free'}}))
elif a[0] == 'api' and 'settings/billing/usage' in a[1]:
    if mode == 'noscope':
        sys.stderr.write('gh: Not Found (HTTP 404)\ngh: This API operation needs the "user" scope. To request it, '
                         'run:  gh auth refresh -h github.com -s user\n')
        sys.exit(1)
    print(json.dumps({'usageItems': read('usage.json', [])}))
elif m_one and '-X' in a and a[a.index('-X') + 1] == 'DELETE':
    arts = read('artifacts.json', {})
    arts[m_one.group(1)] = [x for x in arts.get(m_one.group(1), []) if str(x['id']) != m_one.group(2)]
    write('artifacts.json', arts)
elif m_list:
    print(json.dumps({'artifacts': read('artifacts.json', {}).get(m_list.group(1), [])}))
elif m_run:
    print(json.dumps({'artifacts': [x for x in read('artifacts.json', {}).get(m_run.group(1), [])
                                    if str(x['workflow_run']['id']) == m_run.group(2)]}))
elif a[:2] == ['run', 'list'] and '--workflow' in a:
    print(json.dumps([{'databaseId': 777, 'headSha': read('sha.json', ''), 'createdAt': read('now.json', '')}]))
elif a[:2] == ['run', 'list']:
    print(json.dumps(read('runs.json', {}).get(a[a.index('-R') + 1], [])))
elif a[:2] == ['workflow', 'run']:
    if mode == 'nostart':
        sys.stderr.write('HTTP 422: Workflow does not have workflow_dispatch trigger\n')
        sys.exit(1)
    repo = a[a.index('-R') + 1]
    arts = read('artifacts.json', {})   # the run's upload-artifact step, as GitHub would store it
    arts.setdefault(repo, []).append({'id': 9001, 'name': 'app-apk', 'size_in_bytes': 25 * 2 ** 20, 'expired': False,
                                      'created_at': time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime()),
                                      'workflow_run': {'id': 777}})
    write('artifacts.json', arts)
    print('Created workflow_dispatch event for ' + a[2])
    if mode != 'nourl':
        print('https://github.com/' + repo + '/actions/runs/777')
elif a[:2] == ['run', 'view']:
    n = read('views.json', 0) + 1
    json.dump(n, open(os.path.join(d, 'views.json'), 'w'))
    done = n >= 2
    print(json.dumps({'status': 'completed' if done else 'in_progress',
                      'conclusion': read('conclusion.json', 'success') if done else '',
                      'jobs': [{'name': 'apk', 'conclusion': 'success', 'startedAt': '2026-11-14T10:00:00Z',
                                'completedAt': '2026-11-14T10:02:30Z'}]}))
elif a[:2] == ['run', 'download']:
    out = a[a.index('-D') + 1]
    sub = out if '-n' in a else os.path.join(out, 'app-apk')
    os.makedirs(sub, exist_ok=True)
    open(os.path.join(sub, 'App-1.0-android-ci.apk'), 'wb').write(b'apk')
    h = hashlib.sha256(b'apk' if mode != 'badsum' else b'something else').hexdigest()
    open(os.path.join(sub, 'SHA256SUMS-1.0-ci.txt'), 'w').write(h + '  App-1.0-android-ci.apk\n')
elif a[:2] == ['run', 'cancel']:
    print('cancelled')
else:
    sys.exit(2)
'''


def iso(t):
    return time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime(t))


def usage_items():
    """Shaped like GitHub's billing usage report: raw rounded-up minutes per job, daily storage, money."""
    day = time.strftime('%Y-%m-%d', time.gmtime())
    base = {'product': 'actions', 'pricePerUnit': 0.006, 'netAmount': 0.0}
    return [dict(base, date=day + 'T01:00:00Z', sku='Actions Linux', quantity=5, unitType='Minutes', grossAmount=0.03,
                 repositoryName='App'),
            dict(base, date=day + 'T02:00:00Z', sku='Actions Windows', quantity=11, unitType='Minutes', grossAmount=0.11,
                 repositoryName='App'),
            dict(base, date=day + 'T03:00:00Z', sku='Actions Linux', quantity=4, unitType='Minutes', grossAmount=0.02,
                 repositoryName='Tests'),
            dict(base, date=day + 'T00:00:00Z', sku='Actions storage', quantity=12.0, unitType='GigabyteHours',
                 grossAmount=0.004, repositoryName='App'),
            dict(base, date='2000-01-01T00:00:00Z', sku='Actions storage', quantity=99.0, unitType='GigabyteHours',
                 grossAmount=0.03, repositoryName='App'),
            dict(base, product='packages', sku='Packages storage', quantity=50, unitType='GigabyteHours',
                 grossAmount=1.0, repositoryName='App')]


class CloudTests(unittest.TestCase):
    """The cloud lane (GitHub Actions) against a stand-in gh: nothing here reaches GitHub."""

    @classmethod
    def setUpClass(cls):
        cls.hc = importlib.util.module_from_spec(importlib.util.spec_from_file_location(
            'heavycloud_under_test', os.path.join(HERE, 'heavycloud.py')))
        cls.hc.__spec__.loader.exec_module(cls.hc)

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix='heavygate-cloud-')
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.state, self.fake, self.drop = (os.path.join(self.tmp, n) for n in ('state', 'gh', 'drop'))
        for d in (self.state, self.fake, self.drop):
            os.makedirs(d)
        gh = os.path.join(self.tmp, 'fakegh.py')
        with open(gh, 'w') as f:
            f.write(FAKE_GH)
        self.env = dict(os.environ, HEAVYGATE_DIR=self.state, HEAVYGATE_GUARD='off', FAKEGH_DIR=self.fake,
                        HEAVYGATE_GH=f'{sys.executable}|{gh}')
        self.env.pop('HEAVYGATE_HELD', None)
        self.env.pop('HEAVYGATE', None)
        self.put('usage.json', usage_items())
        now = time.time()
        self.put('runs.json', {'you/App': [
            {'databaseId': 2, 'status': 'in_progress', 'conclusion': '', 'workflowName': 'Build',
             'displayTitle': 'App 1.2.0', 'createdAt': iso(now - 95), 'startedAt': iso(now - 90),
             'updatedAt': iso(now - 5), 'event': 'push', 'headBranch': 'main'},
            {'databaseId': 1, 'status': 'completed', 'conclusion': 'success', 'workflowName': 'Build',
             'displayTitle': 'App 1.1.0', 'createdAt': iso(now - 9000), 'startedAt': iso(now - 9000),
             'updatedAt': iso(now - 8880), 'event': 'push', 'headBranch': 'main'}]})
        self.project('App', 'https://github.com/you/App.git',
                     {'build.yml': 'name: Build\non:\n  push:\n  workflow_dispatch:\njobs: {}\n'})
        self.project('Tests', 'git@github.com:you/Tests.git', {'tests.yml': 'name: tests\non: [push]\n'})
        self.project('Upstream', 'https://github.com/someone/Upstream', {'ci.yml': 'name: CI\non: [push]\n'})
        self.project('NoGit', None, {'ci.yml': 'name: CI\n'})
        with open(os.path.join(self.state, 'settings.json'), 'w') as f:   # a tiny RAM need: the advice is up to the test
            json.dump({'cloud_drop_root': self.drop, 'expect_mb': {'gpu': 1, 'cpu': 1}, 'reserve_mb': 0,
                       'free_ollama': False, 'ollama_url': 'http://127.0.0.1:9'}, f)

    def put(self, name, data):
        with open(os.path.join(self.fake, name), 'w') as f:
            json.dump(data, f)

    def read(self, name):
        with open(os.path.join(self.fake, name)) as f:
            return json.load(f)

    def project(self, name, origin, workflows):
        d = os.path.join(self.drop, name)
        os.makedirs(os.path.join(d, '.github', 'workflows'))
        if origin:
            os.makedirs(os.path.join(d, '.git'))
            with open(os.path.join(d, '.git', 'config'), 'w') as f:
                f.write(f'[core]\n\tbare = false\n[remote "origin"]\n\turl = {origin}\n\tfetch = +refs/heads/*\n')
        for fn, text in workflows.items():
            with open(os.path.join(d, '.github', 'workflows', fn), 'w') as f:
                f.write(text)

    def heavy(self, *args):
        return subprocess.run([sys.executable, GATE, *args], env=self.env, capture_output=True, text=True, timeout=60)

    def calls(self):
        try:
            with open(os.path.join(self.fake, 'calls.txt')) as f:
                return f.read().splitlines()
        except OSError:
            return []

    def test_usage_counts_windows_double_and_reads_the_latest_storage_day(self):
        u = self.hc.parse_usage(usage_items())
        self.assertEqual(u['minutes']['linux'], 9)
        self.assertEqual(u['minutes']['windows'], 11)
        self.assertEqual(u['counted'], 31)   # 9 + 2 x 11
        self.assertEqual(u['by_repo'], {'App': 27, 'Tests': 4})
        self.assertEqual(u['storage_gb'], 0.5)   # 12 GB-hours on the newest day, not the old 99
        self.assertEqual(u['storage_gbh'], 111)  # the month's total (the real report holds one month only)
        self.assertEqual(u['storage_repos'], ['App'])
        self.assertAlmostEqual(u['gross_usd'], 0.194, places=3)   # packages are not Actions
        self.assertEqual(u['net_usd'], 0)
        self.assertEqual(self.hc.parse_usage([dict(usage_items()[0], sku='Actions Linux 4-core')])['minutes']['other'], 5)

    def test_quota_levels(self):
        lv = self.hc.level_of
        self.assertEqual(lv(100, 2000, 400), 'ok')
        self.assertEqual(lv(1000, 2000, 400), 'watch')   # 60% of the 1,600 usable
        self.assertEqual(lv(1700, 2000, 400), 'low')     # into the reserve
        self.assertEqual(lv(2000, 2000, 400), 'over')
        self.assertEqual(lv(10, 2000, 400, net=0.5), 'over')   # any charge at all

    def test_summary_from_a_cached_state(self):
        st = {'state': 'ok', 'plan': 'free', 'fetched': time.time() - 30, 'usage': self.hc.parse_usage(usage_items()),
              'prev': {'counted': 133}, 'repos': {'you/App': {'runs': self.read('runs.json')['you/App']}}}
        s = self.hc.summarize(st, dict(self.hc.DEFAULTS))
        self.assertEqual((s['quota_min'], s['reserve_min'], s['used_min'], s['left_min'], s['usable_min']),
                         (2000, 400, 31, 1969, 1569))
        self.assertFalse(s['quota_assumed'])
        self.assertEqual(len(s['active']), 1)
        self.assertGreaterEqual(s['active'][0]['for_s'], 85)
        self.assertEqual(s['recent'][0]['displayTitle'], 'App 1.1.0')
        self.assertEqual(s['prev_used_min'], 133)
        p = self.hc.summarize(dict(st, plan='pro'), dict(self.hc.DEFAULTS))
        self.assertEqual(p['quota_min'], 3000)
        q = self.hc.summarize(dict(st, plan='mystery'), dict(self.hc.DEFAULTS))
        self.assertTrue(q['quota_assumed'])
        self.assertEqual(self.hc.summarize(st, dict(self.hc.DEFAULTS, cloud_quota_min=500))['quota_min'], 500)

    def test_router_rules_in_order(self):
        cfg = dict(self.hc.DEFAULTS)
        ok = {'state': 'ok', 'left_min': 1969, 'usable_min': 1569, 'reserve_min': 400, 'net_usd': 0}
        base = {'gpu': False, 'os': 'linux', 'est_min': 12, 'need_mb': 2500, 'free_mb': 8000, 'reserve_mb': 768,
                'wait_s': 0}

        def rule(summ=ok, **kw):
            return self.hc.decide(cfg, summ, dict(base, **kw))

        self.assertEqual(rule(gpu=True, free_mb=100)['rule'], 'gpu')   # no GPU on GitHub, starved or not
        self.assertEqual(rule({'state': 'no_scope', 'fix': 'x'}, free_mb=100)['where'], 'local')
        self.assertEqual(rule(dict(ok, net_usd=0.1), free_mb=100)['rule'], 'charged')
        self.assertEqual(rule(dict(ok, usable_min=20), os='windows', free_mb=100)['rule'], 'quota')   # 24 > 20
        r = rule(free_mb=2000)
        self.assertEqual((r['where'], r['rule'], r['cost_min'], r['left_after']), ('cloud', 'ram', 12, 1957))
        self.assertEqual(rule(wait_s=200)['rule'], 'queue')
        self.assertEqual(rule(local_min=15)['rule'], 'long')
        self.assertEqual(rule(local_min=15, est_min=500)['rule'], 'free')   # long, but too big a share of the quota
        r = rule()
        self.assertEqual((r['where'], r['rule'], r['left_after']), ('local', 'free', 1969))
        self.assertEqual(rule(est_min=2.1, os='macos')['cost_min'], 30)   # rounded up, then 10x
        full = dict(ok, resets='2026-12-01', storage={'level': 'over', 'used_gbh': 370, 'incl_gbh': 360})
        r = rule(full, free_mb=100)
        self.assertEqual((r['where'], r['rule']), ('local', 'storage'))   # GitHub may refuse the upload
        self.assertIn('until 2026-12-01', r['reason'])
        self.assertEqual(rule(full, free_mb=100, uploads=False)['rule'], 'ram')   # nothing to upload: no matter
        self.assertEqual(rule(dict(ok, storage={'level': 'low'}), free_mb=100)['rule'], 'ram')   # low is only a warning
        r = rule(disk_low='F:\\ has 9.0 GB free, under the 15 GB floor')
        self.assertEqual((r['where'], r['rule']), ('cloud', 'disk'))
        self.assertEqual(rule(dict(ok, usable_min=5), disk_low='full')['rule'], 'quota')   # never past the quota

    def test_storage_is_measured_in_gb_hours_and_projected(self):
        y, m = time.gmtime()[:2]
        days = calendar.monthrange(y, m)[1]
        use = dict(self.hc.parse_usage(usage_items()), storage_gbh=100.0)
        st = {'state': 'ok', 'plan': 'free', 'usage': use, 'artifacts': {'you/App': [{'bytes': 2 ** 30}]}}
        s = self.hc.summarize(st, dict(self.hc.DEFAULTS))['storage']
        self.assertEqual(s['incl_gbh'], round(0.5 * days * 24, 1))   # 0.5 GB for every hour of the month
        self.assertEqual((s['used_gbh'], s['now_gb'], s['count']), (100.0, 1.0, 1))
        end = calendar.timegm((y + (m == 12), m % 12 + 1, 1, 0, 0, 0))
        self.assertAlmostEqual(s['proj_gbh'], 100 + (end - time.time()) / 3600, delta=1)   # 1 GB until the 1st
        if 100 + (end - time.time()) / 3600 > s['incl_gbh']:
            self.assertEqual(s['level'], 'low')
            self.assertIsNotNone(s['runs_out'])
        self.assertEqual(self.hc.summarize(dict(st, usage=dict(use, storage_gbh=9999)), dict(self.hc.DEFAULTS))
                         ['storage']['level'], 'over')
        self.assertEqual(self.hc.summarize(dict(st, plan='pro'), dict(self.hc.DEFAULTS))['storage']['incl_gb'], 1.0)
        self.assertEqual(self.hc.summarize(st, dict(self.hc.DEFAULTS, cloud_storage_gb=4))['storage']['incl_gb'], 4)
        none = self.hc.summarize(dict(st, artifacts={}), dict(self.hc.DEFAULTS))['storage']
        self.assertEqual((none['now_gb'], none['level'], none['runs_out']), (0, 'ok', None))
        lines = '\n'.join(self.hc.storage_lines({'storage': s, 'resets': '2026-12-01'}))
        self.assertIn(f"100 of {self.hc.fmt_min(s['incl_gbh'])} GB-hours used", lines)
        self.assertIn('Stored by repo: App 1,024 MB', lines)

    def test_old_artifacts_are_deleted_and_the_rest_counted(self):
        now = time.time()
        self.put('artifacts.json', {'you/App': [
            {'id': 1, 'name': 'old', 'size_in_bytes': 50 * 2 ** 20, 'expired': False, 'created_at': iso(now - 5 * 86400),
             'workflow_run': {'id': 10}},
            {'id': 2, 'name': 'new', 'size_in_bytes': 20 * 2 ** 20, 'expired': False, 'created_at': iso(now - 3600),
             'workflow_run': {'id': 11}},
            {'id': 3, 'name': 'gone', 'size_in_bytes': 90 * 2 ** 20, 'expired': True, 'created_at': iso(now - 9e6),
             'workflow_run': {'id': 9}}]})
        r = self.heavy('cloud')
        self.assertIn('api -X DELETE repos/you/App/actions/artifacts/1', self.calls())
        self.assertNotIn('api -X DELETE repos/you/App/actions/artifacts/2', self.calls())
        self.assertEqual([x['id'] for x in self.read('artifacts.json')['you/App']], [2, 3])
        self.assertIn('0.02 GB stored now in 1 artifact,', r.stdout)   # the expired one no longer counts
        self.assertIn('last clean-up', r.stdout)
        with open(os.path.join(self.state, 'cloud.log')) as f:
            row = json.loads(f.read().splitlines()[-1])
        self.assertEqual((row['action'], row['repo'], row['artifact'], row['keep_days']), ('prune', 'you/App', 'old', 3))
        r = self.heavy('cloud', 'prune', '--days', '0', '--dry-run')
        self.assertIn('Would delete 1 (every artifact, 20 MB)', r.stdout)
        self.assertEqual(len(self.read('artifacts.json')['you/App']), 2)   # a dry run deletes nothing
        self.assertIn('Artifacts stay until GitHub expires them', self.heavy('cloud', 'keep', 'off').stdout)
        self.heavy('cloud', 'refresh')
        self.assertNotIn('api -X DELETE repos/you/App/actions/artifacts/2', self.calls())
        r = self.heavy('cloud', 'prune', '--days', '0')
        self.assertIn('Deleted 1 (every artifact', r.stdout)
        self.assertEqual(self.heavy('cloud', 'keep', '-2').returncode, 2)

    def test_workflows_found_only_in_your_own_repos(self):
        wf = self.hc.scan_workflows(self.drop, 'you')
        self.assertEqual(sorted(wf), ['you/App', 'you/Tests'])   # the upstream clone and the folder with no git are out
        self.assertEqual(wf['you/App'], [{'file': 'build.yml', 'name': 'Build', 'dispatch': True}])
        self.assertFalse(wf['you/Tests'][0]['dispatch'])
        self.assertEqual(self.hc.remote_repo('[remote "origin"]\n\turl = git@github.com:a/b.git\n'), 'a/b')
        self.assertIsNone(self.hc.remote_repo('[remote "origin"]\n\turl = https://gitlab.com/a/b\n'))

    def test_heavy_cloud_reads_github_and_status_never_calls_it(self):
        r = self.heavy('cloud')
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn('31 of 2,000 min used', r.stdout)
        self.assertIn('1,569 usable above the 400 reserve', r.stdout)
        self.assertIn('RUNNING you/App: Build (in_progress', r.stdout)
        self.assertIn('you/App: build.yml*', r.stdout)
        self.assertIn('you/Tests: tests.yml', r.stdout)
        self.assertNotIn('Upstream', r.stdout)
        n = len(self.calls())
        self.assertTrue(any(c.startswith('run list -R you/Tests') for c in self.calls()))
        s = self.heavy('status')
        self.assertIn('CLOUD lane (GitHub Actions, free plan): 31 of 2,000', s.stdout)
        j = json.loads(self.heavy('status', '--json').stdout)
        self.assertEqual(j['cloud']['used_min'], 31)
        self.assertEqual(len(self.calls()), n)   # status reads cloud.json only
        again = self.heavy('cloud')   # nothing stale yet: no new usage read
        self.assertEqual(again.returncode, 0)
        self.assertEqual(self.calls().count('api user'), 1)
        runs = self.heavy('cloud', 'runs', '--cached')
        self.assertIn('you/App', runs.stdout)
        self.assertIn('success', runs.stdout)

    def test_refresh_writes_only_when_something_was_due(self):
        clock = [time.time()]
        old = {k: os.environ.get(k) for k in ('HEAVYGATE_GH', 'FAKEGH_DIR')}
        os.environ.update(HEAVYGATE_GH=self.env['HEAVYGATE_GH'], FAKEGH_DIR=self.fake)
        self.addCleanup(lambda: [os.environ.pop(k, None) if v is None else os.environ.__setitem__(k, v)
                                 for k, v in old.items()])
        c = self.hc.Cloud(self.state, {'cloud_drop_root': self.drop}, now=lambda: clock[0])
        c.refresh()
        first, n = c.load()['fetched'], len(self.calls())
        mtime = os.path.getmtime(c.path)
        clock[0] += 10   # the run in flight is due again after 20 s, everything else later
        time.sleep(0.05)
        c.refresh()
        self.assertEqual((c.load()['fetched'], len(self.calls()), os.path.getmtime(c.path)), (first, n, mtime))
        clock[0] += 15
        c.refresh()
        new = self.calls()[n:]
        self.assertEqual(new, [x for x in new if x.startswith('run list -R you/App')])   # only the busy repo
        self.assertEqual(len(new), 1)
        self.assertEqual(c.load()['fetched'], clock[0])

    # the round trip: a real git checkout whose "GitHub" origin is a bare repo on disk ---------------------------------
    def make_repo(self, name='AppR'):
        bare = os.path.join(self.tmp, f'{name}-remote.git').replace('\\', '/')
        repo = os.path.join(self.drop, name)
        url = f'https://github.com/you/{name}.git'

        def g(*args, cwd=repo):
            subprocess.run(['git', *args], cwd=cwd, check=True, capture_output=True)
        subprocess.run(['git', 'init', '--bare', '-q', bare], check=True, capture_output=True)
        os.makedirs(os.path.join(repo, '.github', 'workflows'))
        g('init', '-q', '-b', 'main')
        g('config', 'user.name', 'test')
        g('config', 'user.email', 'test@example.invalid')
        g('config', 'core.autocrlf', 'false')
        g('remote', 'add', 'origin', url)
        g('config', f'url.{bare}.insteadOf', url)   # .git/config still says github.com; git really talks to bare
        with open(os.path.join(repo, '.github', 'workflows', 'android.yml'), 'w') as f:
            f.write('name: Android\non:\n  workflow_dispatch:\njobs: {}\n')
        with open(os.path.join(repo, '.gitignore'), 'w') as f:
            f.write('dist/\n')
        with open(os.path.join(repo, 'main.txt'), 'w') as f:
            f.write('one\n')
        g('add', '-A')
        g('commit', '-q', '-m', 'one')
        g('push', '-q', 'origin', 'main')
        sha = subprocess.run(['git', 'rev-parse', 'HEAD'], cwd=repo, capture_output=True, text=True).stdout.strip()
        self.put('sha.json', sha)
        self.put('now.json', iso(time.time()))
        self.repo, self.g = repo, g
        return repo

    def cloud_settings(self, **extra):
        s = {'cloud_drop_root': self.drop, 'expect_mb': {'gpu': 1, 'cpu': 1}, 'reserve_mb': 0, 'free_ollama': False,
             'ollama_url': 'http://127.0.0.1:9', 'cloud_poll_s': 0.2, 'free_daemons': False, 'poll_s': 0.2,
             'ram_ceiling_pct': 100, 'ram_kill_pct': 101}
        s.update(extra)
        with open(os.path.join(self.state, 'settings.json'), 'w') as f:
            json.dump(s, f)

    def build(self, *flags, env=None):
        """heavy auto on a build command that, run here, prints LOCAL BUILD."""
        return subprocess.run([sys.executable, GATE, 'auto', '-n', 'AppR build', *flags, '--', sys.executable, '-c',
                               'import sys; print("LOCAL BUILD", sys.argv[1:])', 'assembleRelease'],
                              cwd=self.repo, env=env or self.env, capture_output=True, text=True, timeout=120)

    def mapped(self, **settings):
        self.make_repo()
        self.cloud_settings(**settings)
        r = self.heavy('cloud', 'map', 'you/AppR', 'Android', '--match', 'assembleRelease', '--folder', 'AppR')
        self.assertIn('Mapped: assembleRelease in *AppR* -> you/AppR android.yml (linux), files to dist/cloud', r.stdout,
                      r.stdout + r.stderr)

    def cloud_log(self):
        return self.hc.read_runs_log(self.state)

    def test_a_workflow_added_after_the_daily_scan_is_found_and_watched(self):
        self.cloud_settings()
        self.heavy('cloud')   # the daily scan happens here, before the new project exists
        self.make_repo('Late')
        r = self.heavy('cloud', 'map', 'you/Late', 'Android', '--match', 'assembleRelease', '--folder', 'Late')
        self.assertIn('-> you/Late android.yml (linux)', r.stdout)   # found by name: the file was rescanned
        self.assertNotIn('Note:', r.stdout)
        self.heavy('cloud', 'refresh')
        self.assertTrue(any(c.startswith('run list -R you/Late') for c in self.calls()))

    def test_map_list_and_unmap(self):
        self.mapped()
        r = self.heavy('cloud', 'map', '--cached')
        self.assertIn('1. assembleRelease in *AppR* -> you/AppR android.yml on linux', r.stdout)
        self.assertIn('about 10 min (a guess: no past runs)', r.stdout)
        self.assertIn('auto mode is off', r.stdout)
        self.assertEqual(self.heavy('cloud', 'map', 'you/AppR', 'android.yml').returncode, 2)   # no --match
        r = self.heavy('cloud', 'unmap', '1')
        self.assertIn('Removed: assembleRelease', r.stdout)
        self.assertEqual(self.hc.read_jobs(self.state), [])

    def test_auto_off_builds_here(self):
        self.mapped(cloud_wait_s=0)   # the advice would say GitHub (the lane counts as behind)
        r = self.build()
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn('LOCAL BUILD', r.stdout)
        self.assertNotIn('workflow run', '\n'.join(self.calls()))

    def test_auto_sends_a_build_and_brings_the_apk_home(self):
        self.mapped(cloud_wait_s=0, cloud_auto=True)
        r = self.build()
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertNotIn('LOCAL BUILD', r.stdout)
        self.assertIn('to GitHub (queue)', r.stderr)
        self.assertIn('On GitHub: https://github.com/you/AppR/actions/runs/777', r.stderr)
        self.assertIn('workflow run android.yml -R you/AppR --ref main', self.calls())
        for f in ('App-1.0-android-ci.apk', 'SHA256SUMS-1.0-ci.txt'):   # the artifact's own folder is dropped
            self.assertTrue(os.path.exists(os.path.join(self.repo, 'dist', 'cloud', f)), f)
        rec = self.cloud_log()[-1]
        self.assertEqual((rec['exit'], rec['conclusion'], rec['raw_min'], rec['cost_min'], rec['auto'], rec['run_id']),
                         (0, 'success', 3, 3, True, 777))   # one 2m30s job: 3 minutes, Linux 1x
        self.assertEqual(self.hc.learned_est(self.state, {}, 'you/AppR', 'android.yml'), (3, 'median of 1 past run'))
        log = self.heavy('log').stdout
        self.assertIn('cloud', log)
        self.assertIn('3 quota min (linux, auto)  AppR build  -> you/AppR android.yml success  2 files to', log)
        st = self.heavy('cloud', '--cached').stdout
        self.assertIn('Auto: ON, 1 build mapped to workflows; spent 3 of 60 min today, 3 of 400 this month', st)
        self.assertEqual(rec['sums'], {'ok': 1, 'bad': []})   # the APK matched its SHA256SUMS line
        self.assertEqual(rec['deleted'], 1)
        self.assertIn('api -X DELETE repos/you/AppR/actions/artifacts/9001', self.calls())
        self.assertEqual(self.read('artifacts.json')['you/AppR'], [])   # no longer counting against the storage
        self.assertIn('1 checksum match', r.stderr)

    def test_a_bad_checksum_keeps_the_github_copy(self):
        self.mapped(cloud_wait_s=0, cloud_auto=True)
        self.put('mode.json', 'badsum')
        r = self.build()
        self.assertEqual(r.returncode, 1, r.stderr)
        self.assertIn('did not match its checksum', r.stderr)
        self.assertNotIn('LOCAL BUILD', r.stdout)
        self.assertFalse(any('DELETE' in c for c in self.calls()))
        self.assertEqual(self.cloud_log()[-1]['sums']['bad'], ['App-1.0-android-ci.apk'])

    def test_a_full_disk_here_sends_a_mapped_build_to_github(self):
        self.mapped(cloud_auto=True, disk_floor_gb=10 ** 9)   # every drive is "under the floor"
        r = self.build()
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn('to GitHub (disk)', r.stderr)
        self.assertNotIn('LOCAL BUILD', r.stdout)

    def test_cloud_flag_sends_one_build_with_auto_off(self):
        self.mapped()   # auto off, RAM fine, lane clear: only the flag sends it
        r = self.build('--cloud')
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertNotIn('LOCAL BUILD', r.stdout)
        self.assertIn('to GitHub (asked)', r.stderr)
        self.assertFalse(self.cloud_log()[-1]['auto'])   # not counted against auto mode's caps
        self.put('mode.json', 'ok')
        with open(os.path.join(self.repo, 'main.txt'), 'w') as f:
            f.write('dirty\n')
        r = self.build('--cloud')   # the push check still holds
        self.assertIn('LOCAL BUILD', r.stdout)

    def test_no_url_from_gh_finds_the_run_by_its_commit(self):
        self.mapped(cloud_wait_s=0, cloud_auto=True)
        self.put('mode.json', 'nourl')
        r = self.build()
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertTrue(any(c.startswith('run list -R you/AppR --workflow android.yml') for c in self.calls()))
        self.assertNotIn('LOCAL BUILD', r.stdout)   # found, followed and brought home, not a fall-back
        self.assertEqual(self.cloud_log()[-1]['run_id'], 777)

    def test_a_build_that_fails_on_github_fails_here_too(self):
        self.mapped(cloud_wait_s=0, cloud_auto=True)
        self.put('conclusion.json', 'failure')
        r = self.build()
        self.assertEqual(r.returncode, 1)
        self.assertNotIn('LOCAL BUILD', r.stdout)   # not quietly built again here
        self.assertIn('--log-failed', r.stderr)
        self.assertEqual(self.cloud_log()[-1]['conclusion'], 'failure')

    def test_github_refusing_to_start_falls_back_to_here(self):
        self.mapped(cloud_wait_s=0, cloud_auto=True)
        self.put('mode.json', 'nostart')
        r = self.build()
        self.assertEqual(r.returncode, 0)
        self.assertIn('LOCAL BUILD', r.stdout)
        self.assertIn('running it here instead', r.stderr)

    def test_only_what_is_on_github_is_built_there(self):
        self.mapped(cloud_wait_s=0, cloud_auto=True)
        with open(os.path.join(self.repo, 'main.txt'), 'w') as f:
            f.write('two\n')
        r = self.build()
        self.assertIn('LOCAL BUILD', r.stdout)
        self.assertIn('runs here (git): 1 uncommitted or untracked file', r.stderr)
        self.g('commit', '-qam', 'two')   # committed, not pushed
        r = self.build()
        self.assertIn('LOCAL BUILD', r.stdout)
        self.assertIn('push first', r.stderr)
        self.assertNotIn('workflow run', '\n'.join(self.calls()))

    def test_daily_cap_keeps_builds_here(self):
        self.mapped(cloud_wait_s=0, cloud_auto=True, cloud_auto_day_min=60)
        self.hc.log_run(self.state, {'repo': 'you/AppR', 'workflow': 'android.yml', 'auto': True, 'conclusion': 'success',
                                     'raw_min': 3, 'cost_min': 59, 'dispatched_utc': iso(time.time())})
        r = self.build()
        self.assertIn('LOCAL BUILD', r.stdout)
        self.assertIn('runs here (cap): auto mode has spent 59 of its 60 min today', r.stderr)

    def test_foreground_call_is_told_to_go_background_and_nested_jobs_stay(self):
        self.mapped(cloud_wait_s=0, cloud_auto=True)
        r = self.build('--no-wait')
        self.assertEqual(r.returncode, 75)
        self.assertIn('run it in the background', r.stderr)
        r = self.build(env=dict(self.env, HEAVYGATE_HELD='cpu'))
        self.assertIn('LOCAL BUILD', r.stdout)
        r = self.build('--no-cloud')
        self.assertIn('LOCAL BUILD', r.stdout)
        self.assertNotIn('workflow run', '\n'.join(self.calls()))

    def test_advise_shows_the_mapping_and_dispatch_wait_brings_files_home(self):
        self.mapped()
        r = subprocess.run([sys.executable, GATE, 'cloud', 'advise', '--', 'gradlew', 'assembleRelease'], cwd=self.repo,
                           env=self.env, capture_output=True, text=True, timeout=60)
        self.assertIn('Mapped to you/AppR android.yml on linux: about 10 min there (a guess: no past runs)', r.stdout)
        self.assertIn('Auto mode is off', r.stdout)
        r = self.heavy('cloud', 'dispatch', 'you/AppR', 'android.yml', '--wait', '--force')
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertTrue(os.path.exists(os.path.join(self.repo, 'dist', 'cloud', 'App-1.0-android-ci.apk')))
        self.assertFalse(self.cloud_log()[-1]['auto'])
        self.assertEqual(self.hc.spent(self.state)['day'], 0)   # by hand: not counted against auto mode's caps

    def test_missing_scope_says_how_to_fix_and_backs_off(self):
        self.put('mode.json', 'noscope')
        r = self.heavy('cloud')
        self.assertEqual(r.returncode, 1)
        self.assertIn('missing the "user" scope', r.stdout)
        self.assertIn('Fix: gh auth refresh -h github.com -s user', r.stdout)
        self.heavy('cloud')
        self.assertEqual(self.calls().count('api user'), 1)   # no retry every few seconds
        self.heavy('cloud', 'refresh')
        self.assertEqual(self.calls().count('api user'), 2)   # refresh always asks again
        a = json.loads(self.heavy('cloud', 'advise', '--cached', '--json', '--need-mb', '999999999').stdout)
        self.assertEqual((a['where'], a['rule']), ('local', 'unavailable'))

    def test_dispatch_follows_the_advice_unless_forced(self):
        self.heavy('cloud')
        r = self.heavy('cloud', 'dispatch', 'you/Tests', 'tests.yml', '--cached')
        self.assertEqual(r.returncode, 3)
        self.assertIn('no workflow_dispatch trigger', r.stdout)
        r = self.heavy('cloud', 'dispatch', 'you/Nope', 'x.yml', '--cached')
        self.assertEqual(r.returncode, 3)
        r = self.heavy('cloud', 'dispatch', 'you/App', 'build.yml', '--cached')   # RAM is fine, lane clear: stay here
        self.assertEqual(r.returncode, 3, r.stdout)
        self.assertIn('Not sent', r.stdout)
        self.assertFalse(any(c.startswith('workflow run') for c in self.calls()))
        r = self.heavy('cloud', 'dispatch', 'you/App', 'Build', '--cached', '--need-mb', '999999999', '--ref', 'main')
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn('Started build.yml on you/App at main', r.stdout)
        self.assertIn('workflow run build.yml -R you/App --ref main', self.calls())
        r = self.heavy('cloud', 'dispatch', 'you/App', 'build.yml', '--cached', '--force')
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        with open(os.path.join(self.state, 'cloud.log')) as f:
            log = [json.loads(x) for x in f]
        self.assertEqual([(x['advice'], x['forced']) for x in log], [('cloud', False), ('local', True)])

    def test_advise_keeps_gpu_commands_here(self):
        self.heavy('cloud')
        with open(os.path.join(self.state, 'rules.json'), 'w') as f:
            json.dump({'rules': [{'match': 'shot.py', 'folder': '', 'lane': 'gpu', 'threads': None}]}, f)
        a = json.loads(self.heavy('cloud', 'advise', '--cached', '--json', '--need-mb', '999999999', '--',
                                  'python', 'tools/shot.py').stdout)
        self.assertEqual(a['rule'], 'gpu')
        b = json.loads(self.heavy('cloud', 'advise', '--cached', '--json', '--need-mb', '999999999', '--',
                                  'gradlew', 'assembleRelease').stdout)
        self.assertEqual((b['where'], b['rule']), ('cloud', 'ram'))

    def test_runner_probe_text_round_trips(self):
        sys.path.insert(0, HERE)
        import heavyprobe
        s = {'cpu': 87.4, 'ram': 5.12, 'ram_total': 15.6, 'disk': 41.2, 'disk_total': 145.0, 'cores': 4,
             'peak_cpu': 100.0, 'peak_ram': 6.2}
        text = heavyprobe.describe(s)
        self.assertLessEqual(len(text), 140)
        p = self.hc.parse_probe(text)
        self.assertEqual((p['cpu'], p['ram'], p['ram_total'], p['disk'], p['cores'], p['peak_ram'], p['done']),
                         (87.0, 5.1, 15.6, 41.2, 4, 6.2, False))
        self.assertTrue(self.hc.parse_probe('done avg ' + text)['done'])
        self.assertIsNone(self.hc.parse_probe('Build passed'))
        b = self.hc.runner_brief([dict(p, t=1.0, cpu=c) for c in (10, 50, 90)])
        self.assertEqual((b['cpu_hist'], b['now']['cpu'], b['n']), ([10, 50, 90], 90, 3))

    @windows_only
    def test_tray_keeps_a_finished_cloud_build_and_shows_the_runner(self):
        d = os.path.join(self.state, 'cloud-active')
        os.makedirs(d)
        now = time.time()
        dead = 4194300   # no such process
        runner = {'now': {'cpu': 92.0, 'ram': 5.1, 'ram_total': 15.6, 'disk': 41.0, 'disk_total': 145.0, 'cores': 4,
                          'peak_cpu': 100.0, 'peak_ram': 6.2, 't': now - 12}, 'cpu_hist': [10, 60, 92],
                  'ram_hist': [2.0, 4.0, 5.1], 'n': 3}
        notes = {'1.json': {'repo': 'you/App', 'workflowName': 'App Cloud', 'status': 'in_progress', 'pid': os.getpid(),
                            'url': 'https://github.com/you/App/actions/runs/1', 'started_ts': now - 300, 'runner': runner},
                 '2.json': {'repo': 'you/Old', 'workflowName': 'Old', 'status': 'built', 'pid': dead,
                            'ended_ts': now - 10, 'started_ts': now - 400, 'dest': 'C:\\x\\dist\\cloud',
                            'files': ['a.apk'], 'url': 'https://github.com/you/Old/actions/runs/2', 'runner': runner},
                 '3.json': {'repo': 'you/Gone', 'status': 'built', 'pid': dead, 'ended_ts': now - 9999},
                 '4.json': {'repo': 'you/Dead', 'status': 'in_progress', 'pid': dead}}
        for n, body in notes.items():
            with open(os.path.join(d, n), 'w') as f:
                json.dump(body, f)
        code = ('import importlib.machinery, importlib.util, json, sys\n'
                'sys.path.insert(0, %r)\n'
                'l = importlib.machinery.SourceFileLoader("ht", %r)\n'
                's = importlib.util.spec_from_loader("ht", l)\n'
                'ht = importlib.util.module_from_spec(s)\n'
                'l.exec_module(ht)\n'
                'mine = ht.hg.cloud_mine()\n'
                'rows = ht.cloud_rows({"state": "ok", "active": mine})\n'
                'print(json.dumps({"repos": [m["repo"] for m in mine],\n'
                '                  "text": ["".join(x[0] for x in r) for r in rows],\n'
                '                  "links": [x[2] for r in rows for x in r if len(x) > 2 and x[2]]}))\n'
                ) % (HERE, os.path.join(HERE, 'heavytray.pyw'))
        r = subprocess.run([sys.executable, '-c', code], env=self.env, capture_output=True, text=True, timeout=60,
                           encoding='utf-8')
        self.assertEqual(r.returncode, 0, r.stderr)
        o = json.loads(r.stdout)
        self.assertEqual(o['repos'], ['you/App', 'you/Old'])   # finished 10 s ago stays; old and orphaned go
        self.assertEqual(sorted(os.listdir(d)), ['1.json', '2.json'])
        self.assertTrue(any(x.startswith('      CPU  92%') and 'RAM 5.1/16 GB' in x for x in o['text']), o['text'])
        self.assertTrue(any('4 cores' in x and 'ago' in x for x in o['text']), o['text'])
        self.assertTrue(any(x.startswith('CLOUD ✓ Old') and 'built' in x for x in o['text']), o['text'])
        self.assertTrue(any('peak CPU 100%' in x for x in o['text']), o['text'])
        self.assertEqual(o['links'], ['https://github.com/you/App/actions/runs/1', 'https://github.com/you/Old/actions/runs/2'])

    @windows_only
    def test_tray_strip_shows_github_runs(self):
        code = ('import importlib.machinery, importlib.util, json, sys\n'
                'sys.path.insert(0, %r)\n'
                'l = importlib.machinery.SourceFileLoader("ht", %r)\n'
                's = importlib.util.spec_from_loader("ht", l)\n'
                'ht = importlib.util.module_from_spec(s)\n'
                'l.exec_module(ht)\n'
                'st = ht.sample_states()\n'
                'cl = ht.hg.cloud_summary()\n'
                'busy, idle = st["busy"], st["idle"]\n'
                'busy["cloud"] = idle["cloud"] = cl\n'
                'text = lambda rows: ["".join(x[0] for x in r) for r in rows]\n'
                'out = {"busy": text(ht.strip_lines(busy)), "idle": text(ht.strip_lines(idle)),\n'
                '       "alone": text(ht.strip_lines(dict(idle, cloud_alone=True))),\n'
                '       "tip": ht.tooltip(busy), "menu": [x for x in ht.menu_lines(busy) if x]}\n'
                'cl["active"] = []\n'
                'out["alone_idle"] = text(ht.strip_lines(dict(idle, cloud_alone=True)))\n'
                'out["busy_idle"] = text(ht.strip_lines(busy))\n'
                'print(json.dumps(out))\n') % (HERE, os.path.join(HERE, 'heavytray.pyw'))
        self.heavy('cloud')
        r = subprocess.run([sys.executable, '-c', code], env=self.env, capture_output=True, text=True, timeout=60)
        self.assertEqual(r.returncode, 0, r.stderr)
        o = json.loads(r.stdout)
        self.assertTrue(any(x.startswith('CLOUD ► App Build') and '1,969 min left' in x for x in o['busy']), o['busy'])
        self.assertEqual(o['idle'], [])   # lanes idle: the strip stays hidden unless chosen
        self.assertTrue(o['alone'] and o['alone'][0].startswith('CLOUD ► App Build'))
        self.assertEqual(o['alone_idle'], [])
        self.assertIn('CLOUD idle · 1,969 min left', o['busy_idle'])
        self.assertIn('CLOUD: 1 running', o['tip'])
        self.assertTrue(any('31 of 2,000 min used' in x for x in o['menu']))


if __name__ == '__main__':
    import faulthandler
    faulthandler.dump_traceback_later(420, repeat=True)   # a stuck run says where it is stuck, every 7 min
    unittest.main(verbosity=2)
