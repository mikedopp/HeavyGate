# HeavyGate

**Air traffic control for your workstation.** One command in front of your builds, test suites, renders and deploys, so
they take turns instead of taking down the machine.

![The text strip while jobs wait for RAM](docs/strip.png)

```
heavy cpu -n "App release build" -- ./gradlew assembleRelease
heavy gpu -n "Game full suite"   -- ./test.sh full
```

No daemon, no container, no config, no dependencies. One Python file, on Windows and Linux.

## Why anyone cares

Your computer has no idea that the three things you just started all want the same 8 GB.

It used to take one person to overload a workstation. Now it takes one person with four terminals, two AI coding agents
and a CI script, all of them deciding on their own to run the full test suite right now. HeavyGate exists because of a
real night: two 3D test runs, a phone build and a Blender render started at the same time on a 16 GB PC and ran it out
of memory. Nothing was wrong with any of those jobs. They just had no way to know about each other.

CI servers solved this years ago with runners and queues. Your own machine still has nothing, and a full CI system is
absurd for one box. HeavyGate is the missing middle:

- **One job per lane.** Heavy GPU work and heavy CPU work each get a lane, so a build and a 3D test can overlap but two
  3D tests can't. It holds across every terminal, script and agent, because the lanes live in the OS, not in a shell.
- **It checks the RAM before it starts.** A job that has its lane still waits until enough memory is really free, and
  clears idle Gradle and Kotlin daemons if that helps.
- **RAM never goes past the ceiling (92%).** Every job runs under a hard memory cap set to what is left below the
  ceiling. A job is ended if the PC goes over 92% for 3 seconds, or reaches 95%. `heavy guard` does the same for any
  command a coding agent runs outside the lanes. List your agents' process names in `guard_agents` in `settings.json`
  (for example `["myagent.exe"]`); the list is empty until you do, so the guard ends nothing on its own.
- **A fair line.** First come, first served. A polling monitor can never cost a job its turn.
- **It can't jam.** Locks belong to the kernel. Kill a job, crash it, pull the plug: the lane frees itself. There are no
  stale lock files to delete at 2 am.
- **A stuck job gets noticed.** Alive but doing nothing for 5 minutes shows as STALLED, and `heavy stop` clears the lane. After 20 minutes with another job waiting, it is ended for you.
- **Your desktop stays usable.** CPU-lane jobs run at low priority on three quarters of the cores.
- **It learns how long things take.** Past runs give each job an estimate, so you get "All done around 23:40".
- **It keeps receipts.** `heavy log` shows what every job cost: time in line, time waiting for RAM, run time, lowest
  free RAM, CPU and GPU peaks.

## Where it fits

Put `heavy` in front of anything that hurts when two run at once. The exit code passes straight through, so scripts and
CI steps behave as they did before.

**Builds**

```
heavy cpu -n "Android release" -- ./gradlew assembleRelease
heavy cpu -n "Server publish"  -- dotnet publish -c Release
heavy cpu -n "Docs archive" --expect 6000 -- tar czf site.tgz public/
```

**QA and test suites**

```
heavy gpu -n "Browser 3D checks" -- npx playwright test
heavy gpu -n "Blender render"    -- blender -b scene.blend -a
heavy cpu -n "Unit tier"         -- pytest -n 8
```

**Deployments**

A release script can build and test through the lanes, then ship. If a gated script calls `heavy` again, the inner call
runs in its parent's slot, so it can't deadlock on itself.

```sh
#!/bin/sh
set -e
heavy cpu -n "Release build" -- ./build.sh
heavy gpu -n "Release QA"    -- ./qa.sh --full
./deploy.sh                  # light work stays outside the lanes
```

Skip a run when the machine is busy instead of queueing (a nightly job, say):

```sh
heavy cpu --no-wait -n "Nightly" -- ./nightly.sh
[ $? -eq 75 ] && echo "lane busy, skipped"
```

**AI coding agents**

Add one line to your agent instructions: *run builds and test suites through `heavy`.* Every parallel session then
queues in one line, and each agent gets the normal output and exit code back. `--no-wait` and `heavy status --json` let
an agent decide what to do while it waits.

**Your own tools**

```python
import subprocess, heavygate

with heavygate.hold('gpu', 'Nightly render') as slot:
    with slot.spawning():
        p = subprocess.Popen(['blender', '-b', 'scene.blend', '-a'], **slot.popen)
    slot.exit = p.wait()
```

Anything that polls the lanes uses `heavygate.peek()` / `peek_queue()` or `heavy status --json --peek`, which never
touch a lock.

## How it works

```mermaid
flowchart LR
    A[terminal] --> Q
    B[agent session] --> Q
    C[release script] --> Q
    Q[queue, in order] --> L{lane free?}
    L -- yes --> R{enough free RAM?}
    L -- no --> Q
    R -- yes --> J[run the job]
    R -- "no: free idle daemons, wait up to 10 min" --> J
    J --> H[log the cost, free the lane]
```

| Lane | For | How it runs |
| --- | --- | --- |
| `gpu` | 3D window checks, game QA, headless browser 3D, GPU renders | normal priority |
| `cpu` | Compiles, Gradle/Android builds, `dotnet publish`, CPU renders, big archives | low priority, three quarters of the cores |

The gpu lane runs one job and the cpu lane two (`cpu_slots`); the rest queue in arrival order. Before a job starts, the gate waits for enough free RAM: 3.5
GB for a gpu job and 2.5 GB for a cpu job, plus a 768 MB reserve. Change it per job with `--expect <MB>`. The job also waits until it fits under the RAM
ceiling (92% by default). After 10 minutes it starts if at least 1.5 GB is left under the ceiling, and records a warning.

**RAM ceiling.** On Windows each job runs inside a Job Object whose memory cap is what was left under the ceiling when it
started. Past that, the job's allocations fail, so a runaway compile stops itself instead of taking the PC to 100%. While
it runs, the job is ended if RAM stays over the ceiling for 3 seconds, or reaches 95%, whatever pushed it there (a job
under 300 MB is never blamed). `heavy guard` covers what never entered a lane. It ends the biggest command a coding agent
started (the shell its tool call opened, and everything under it), never the agent itself or anything you started
yourself. The tray, the agent hook and sign-in all start it. Its actions go to `guard.log`.

**Disk floor.** A full drive breaks git and every program writing to it, not just the build. So a job doesn't start
while the drive it runs in, the temp drive or the profile drive (where Gradle, NuGet and npm keep their caches) has under
`disk_floor_gb` free (15 GB). It's refused with exit 28 and a message naming the drive; from Python, `hold()` raises an
`OSError` with `errno.ENOSPC`. A running job is ended, also with exit 28, if one of those drives falls under
`disk_stop_gb` (3 GB), checked every 5 seconds. `disk_paths` adds more folders whose drives count. `heavy status` and the
tray show free space on each drive, and `heavy log` shows how far the job's drive went down while it ran (that includes
anything else writing there at the time). With the cloud lane, a mapped build goes to GitHub instead when the disk here
is under the floor.

## Install

You need Python 3.9 or newer. There's nothing else to install, and no admin or root rights are needed.

**Windows**

```powershell
git clone https://github.com/mikedopp/HeavyGate.git
cd HeavyGate
.\install.ps1
```

**Linux**

```sh
git clone https://github.com/mikedopp/HeavyGate.git
cd HeavyGate
./install.sh
```

Then open a new terminal and run `heavy status`. To remove it, delete the `heavy` launcher (`%LOCALAPPDATA%\HeavyGate\bin`
on Windows, `~/.local/bin` on Linux) and the state folder listed below.

## Commands

```
heavy gpu -n NAME -- COMMAND     run on the GPU lane (--expect MB, --no-wait)
heavy cpu -n NAME -- COMMAND     run on the CPU lane (also --threads N)
heavy auto -n NAME -- COMMAND    let the rules pick the lane and threads
heavy why -- COMMAND             where auto would send it, without running it
heavy hook -- COMMAND            what the agent hook would do to a command (no command: the hook itself)
heavy rules                      the rules window on Windows (--list prints them)
heavy status                     per lane: running, waiting for RAM, queued; hardware, RAM, CPU and GPU now
heavy status --json              the same, for other tools (--peek never touches a lock)
heavy watch                      the same, live
heavy log                        what each run cost
heavy stop LANE                  end the job holding a lane and everything it started
heavy free                       stop idle Gradle/Kotlin daemons (never during a build), unload Ollama's models
heavy deps                       everything it depends on and where it lives
heavy guard                      keep RAM under the ceiling: end the agent command that takes it past (background)
heavy guard --check              RAM now, every agent command with its size, and what the guard would end
heavy guard --at-sign-in on|off  start the guard at sign-in (Windows)
heavy service install|remove     Linux: the guard as a systemd user service (status: is it running; --dry-run)
heavy tray                       text strip above the clock, plus a tray icon (Windows)
heavy tray --at-sign-in on|off   start the tray at sign-in (Windows)
heavy shutdown-when-done         shut the PC down once both lanes are done; --off cancels (the tray on Windows,
                                 the guard on Linux)
heavy cloud                      the cloud lane: GitHub Actions minutes used and left, charges, runs, workflows
heavy cloud runs                 recent runs across your repos (--count N)
heavy cloud advise -- COMMAND    run it here or on GitHub, and what it would cost (--os, --est, -n NAME, --json)
heavy cloud dispatch REPO WF     start a workflow on GitHub; refused when the advice is to keep it here (--force, --ref,
                                 --wait follows it and brings its files back)
heavy cloud map REPO WF --match WORDS [--folder PART]   a build that may run as that workflow (no args: list them)
heavy cloud unmap N              remove a mapping
heavy cloud auto on|off          let heavy auto / heavy cpu send mapped builds to GitHub, within daily and monthly caps
heavy auto --cloud / --no-cloud  this one build: to GitHub on purpose, or always here
heavy cloud prune                delete artifacts older than cloud_keep_days now (--days N, 0 = all; --dry-run)
heavy cloud probe FOLDER         copy the runner probe into a checkout and print the workflow steps that start it
heavy cloud keep DAYS|off        how old an artifact may get on GitHub before heavy deletes it (default 3)
heavy pool                       your other machines, asked now: reachable, cpu lane, free RAM, what they can build
heavy pool add NAME USER@HOST    a worker (--key FILE, --port N, --reserve-mb N); remove NAME takes it out
heavy pool auto on|off           send Gradle jobs to a worker that can start them now, else run them here
heavy cpu --pool[=NAME] / --no-pool   this one job: to a worker (NAME, or the one with the most room), or always here
```

`--no-wait` exits with code 75 when the lane is busy, and a job refused or ended for disk space exits 28.
`HEAVYGATE=off` skips the gate entirely.

`heavy auto` picks the lane and threads from `rules.json` in the state folder, first match wins. A rule's `match` is whole
words anywhere in the command, and `folder` is any part of the working folder. A command no rule knows takes the CPU lane
at the usual share. An explicit `--threads` beats the rule. The rules window edits the same file and suggests rules from
what past runs used; it is Windows only for now, and `heavy rules --list` prints the rules anywhere.

`--threads N` gives one CPU lane job N logical processors instead of the `cpu_threads` setting. It still runs at
below-normal priority, a number above what the machine has means all of them, and the setting is untouched for the next
job. `status` and `log` show each job's count. GPU jobs have no limit, so the flag is refused there. Inside a job that
already holds a lane the parent's share stands, and the job says so.

**Agent hook.** `heavy hook` puts a coding agent's heavy commands on their lanes, so nobody has to tell it to. Add it as a
`PreToolUse` hook on the shell tools, in the agent's `settings.json`:

```json
{ "hooks": { "PreToolUse": [ { "matcher": "Bash|PowerShell", "hooks": [
  { "type": "command", "command": "python", "args": ["<path>/heavygate.py", "hook"], "timeout": 10 } ] } ] } }
```

It wraps only the command a rule names, and only when that command is the program being run, so `git commit -m "fix gradle"`
is left alone. The `cd`, pipes and redirects around it stay as written. A foreground call gets `--no-wait`, because it would
otherwise sit in a queue until its timeout: exit 75 tells the agent to run it again in the background, where it waits its
turn. Left alone: anything the hook can't read for certain (subshells, script blocks, heredocs), commands that are already
gated, quick questions like `--version` or `tar -tf`, and `.sh` scripts on Windows, which the lane's cmd.exe can't run. The
hook never approves anything, the agent's own permission checks still decide, and if it fails the command runs as written.
`HEAVYGATE=off` turns it off.

**Settings** go in `settings.json` in the state folder, and every one is optional:

```json
{ "cpu_threads": 12, "reserve_mb": 1024, "expect_mb": { "gpu": 6000, "cpu": 4000 }, "ram_wait_min": 20,
  "ram_ceiling_pct": 85, "ram_kill_pct": 90, "disk_floor_gb": 15, "disk_stop_gb": 3, "disk_paths": [],
  "guard_agents": ["myagent.exe"], "gated_tools": ["mytestrunner"] }
```

`guard_agents` names the coding agents whose commands the RAM guard may end; it is empty by default. `gated_tools`
names your own tools that already take a lane themselves, so the hook doesn't wrap them a second time.
`settings.example.json` has every setting with its default.

## Cloud lane (GitHub Actions)

GitHub Actions is a third place a build can run, off this machine. The cloud lane watches it with the `gh` command line,
signed in as you, and only reads your own account and repos:

- **Minutes.** The billing usage report gives the minutes used this month per runner OS and per repo. The free allowance
  counts Linux minutes once, Windows twice and macOS ten times; the report lists raw rounded-up minutes for each job, so the
  multiplier is applied here. The quota comes from your plan (Free 2,000 min, Pro 3,000), or `cloud_quota_min`. Status
  shows used, left, the usable part above a 20% reserve kept for releases, the pace to month end, the reset date, and what
  GitHub actually charged. Any charge at all turns the lane red.
- **Runs.** Every repo with a workflow under the projects folder (and any repo that used minutes) is checked for runs in
  flight. A run in flight is rechecked every 20 s, the rest every 5 min. Upstream clones, whose origin is someone else's,
  are left out.
- **Advice.** `heavy cloud advise -- COMMAND` says where a job should run. A GPU job stays here (standard runners have no
  GPU). Nothing goes to GitHub when the lane can't be read, when GitHub has charged anything this month, or when the job
  would dip into the reserve. Otherwise it goes when this PC is short of RAM for it, when its lane here is 3 min or more
  behind, or when it takes 10 min or more here and costs no more than a quarter of the usable minutes. Everything else
  runs here: local is free and skips the upload.
- **Dispatch.** `heavy cloud dispatch you/App build.yml` starts a workflow that has a `workflow_dispatch` trigger, only when
  the advice says cloud, unless `--force`. Each dispatch goes to `cloud.log`. `--wait` follows it and brings its files
  back, as below.
- **Artifact storage.** The files a workflow uploads count against storage, not minutes. GitHub bills it in GB-hours
  and includes the plan's storage for every hour of the month: on Free, 0.5 GB × 720 h = 360 GB-hours in a 30-day month.
  Past that, with no budget set, it refuses new uploads until the 1st, so a build's upload step fails. Status shows the
  GB-hours used, what is stored now (read from each repo's artifact list every hour), the projection to month end, and
  the date it runs out at this rate. When it's used up, mapped builds stay here (rule `storage`). Artifacts older than
  `cloud_keep_days` (3) are deleted from your repos as they're listed, and each deletion goes to `cloud.log`.
  `heavy cloud prune` does it now. A build HeavyGate brings home is deleted on GitHub as soon as its files are here and
  any `SHA256SUMS` file it carries matches; on a mismatch the copy on GitHub is kept and the build exits 1. Also set
  `retention-days` on `upload-artifact` in your workflows: without it an artifact stays 90 days.

**Mapped builds and auto mode.** `heavy cloud map you/App android.yml --match assembleRelease --folder App` tells
HeavyGate that this build, in that project, can run as that workflow. It matches like a lane rule. `--os` sets the
runner OS (linux), `--artifact` the artifact to bring back (all of them), and `--dest` where its files go, under the
checkout (`dist/cloud`; keep it git-ignored). With `heavy cloud auto on`, `heavy auto` and `heavy cpu` (and so the agent
hook) check a mapped build before taking the lane. If the advice says GitHub, the workflow runs at the branch
that is checked out. HeavyGate follows the run, downloads its artifact into `dest`, logs the run with its real quota
minutes, and exits as the build would have. It only sends what GitHub already has: a clean checkout of the mapped repo
whose HEAD is the branch's tip on GitHub. Otherwise the build runs here and says why: uncommitted files, push first, and
so on. Auto mode stops at `cloud_auto_day_min` (60) and `cloud_auto_month_min` (400) quota minutes. The GitHub time is
learned from past runs. A build that fails on GitHub fails here too, with the command that shows its log; it isn't rebuilt
locally behind your back. `--no-cloud` keeps one build here. `--cloud` sends one there on purpose: the quota, charge and
push checks still apply, and it doesn't count against the caps. A foreground call with `--no-wait`, as the agent hook
makes, exits 75 instead of waiting, to be rerun in the background.

**Watching a GitHub build.** A build HeavyGate sent to GitHub keeps the strip up the whole time, the way a lane job
does. The run's name on the strip is a link to its page on GitHub. GitHub gives no live CPU, RAM or disk for its runners,
and a running job's log can't be read until the job ends. So a workflow can carry the runner probe:
`heavy cloud probe <checkout>` copies `heavyprobe.py` into `.github/heavygate-probe.py` and prints the three steps that
start it after checkout and post its last reading at the end. The workflow also needs `permissions: statuses: write`.
On the runner it posts a commit status every 15 s (`heavygate runner (<job>)`, standard library only, never fails the
build). The HeavyGate process waiting on the run reads those back and the strip shows the runner's CPU and RAM with
sparklines, its disk and core count, and how old the reading is. When the run ends the strip shows the result, the
peaks and where the files went for `cloud_done_show_s` (120 s). The probe is copied, never forked: change it here, then
run `heavy cloud probe` again in each repo.

What should not be mapped: anything that needs the GPU, files only this PC has, or signing keys that aren't GitHub
secrets. Also anything that has to produce the exact release file, since a runner signs with its own key.

The billing report needs the `user` scope: `gh auth refresh -h github.com -s user`. Without it, status says so and gives
that command. `heavy status` and the tray only read `cloud.json`; the tray's background thread is what calls GitHub.
On the strip, a CLOUD row shows runs in flight and the minutes left whenever the lanes are busy. "Pop up for GitHub
Actions runs too" (strip settings) brings the strip up for a GitHub run alone. Settings: `cloud` (on), `cloud_quota_min`,
`cloud_reserve_pct` (20), `cloud_wait_s` (180), `cloud_long_min` (10), `cloud_long_share` (0.25), `cloud_usage_s` (600),
`cloud_runs_s` (300), `cloud_active_s` (20), `cloud_repos`, `cloud_drop_root`, `cloud_auto` (off),
`cloud_auto_day_min` (60), `cloud_auto_month_min` (400), `cloud_est_min` (10), `cloud_poll_s` (15), `cloud_max_min` (60),
`cloud_storage_gb` (0 = by plan), `cloud_keep_days` (3; 0 = never), `cloud_artifacts_s` (3600), `cloud_delete_after` (on).

## Pool (your other machines)

A small server or a spare PC can take whole jobs off this one: a Gradle build there instead of here keeps its 2-3 GB off
your RAM. It is SSH and HeavyGate, nothing else: no agent, no daemon, no open port beyond SSH. A worker needs key-only
SSH from here, python3, HeavyGate 1.20 or newer (`./install.sh`), and the job's tools (Java and the Android SDK for
Android builds; `heavy pool` says what each worker has).

```sh
heavy pool add server1 you@192.0.2.20 --key ~/.ssh/worker_key   # asks it at once
heavy pool                                                      # every worker: ready or why not
cd MyApp/android
heavy cpu --pool -- gradlew assembleDebug                       # to the worker with the most room
heavy pool auto on                                              # every Gradle job goes out when a worker can take it
```

A trip:

1. **Pick.** Each worker is asked over SSH (`heavy pool-worker info`): its cpu lane, free RAM and tools. Auto mode
   takes only a worker that can start the job now (a free slot, and free RAM past what it keeps for itself,
   `pool_reserve_mb`, 2 GB); `--pool` also takes a busy one, where the job queues. No worker, or SSH fails: it runs here.
2. **Send.** The project's git checkout (or the folder, outside git) goes to `~/.cache/heavygate-pool/<project>` there.
   Only files that changed since the last trip are sent, and files deleted here are deleted there. Files git ignores
   (build folders, secrets) and `local.properties` are never sent, nor key files even when git tracks them (`*.jks`,
   `*.keystore`, `*.p12`, `*.pfx`, `*.pem`, `*.key`, `keystore.properties`, `.env` and the like). Scripts like `gradlew` arrive with LF line
   endings and the executable bit, whatever the checkout here has.
3. **Run.** The worker runs the job in its own cpu lane (queue, nice 10, its RAM guard) inside a `systemd-run` scope
   with `MemoryMax` set to its free RAM minus the reserve, so a runaway build fails instead of starving the worker's own
   services. Output streams here. `gradlew.bat` becomes `./gradlew --no-daemon`, so no Gradle daemon sits on the
   worker's RAM afterwards. Paths into the project (`C:\App\android\gradlew.bat -p C:\App\android`, as build scripts
   write them) become paths in the worker's copy; a path outside the project keeps the job here. Ctrl+C here stops it
   there.
4. **Bring back.** On success every file there that matches `pool_outputs` (Gradle: `build/outputs/**`,
   `build/install/**`, `build/libs/**`) and is missing here or changed since it last came back is brought back, each
   checked against its SHA256. So a build folder cleaned here still gets its APK back when the worker had nothing to
   rebuild.
5. **Sign here.** Every APK that comes back is signed again on this PC with the key a local build would have used
   (`heavysign.py`), so it installs over your app like one built here. A worker never has a key: it signs with its own
   debug key, or not at all. A release APK of a project with `keystore.properties` beside its Gradle build gets that
   key (`storeFile` found relative to the build or `app/`); everything else gets this PC's debug key
   (`~/.android/debug.keystore`), which is what a local build uses for both. apksigner from the newest build-tools runs
   with the JDK of `JAVA_HOME` or `javac` (`pool_java`), passwords go through environment variables, and the signed
   APK's certificate is checked against the keystore's. An `app-release-unsigned.apk` becomes `app-release.apk`. An APK
   that can't be signed is renamed `.apk.worker-signed`, so it is never installed by mistake, and the job exits 1.
   An app bundle (`.aab`) is signed the same way with the JDK's `jarsigner` (the worker's signature taken off
   first, the certificate checked with `keytool -printcert -jarfile`). `python heavysign.py APK|AAB [GRADLE_DIR]` signs
   one by hand.

`heavy log` shows the trip (sent, ran, brought back), `heavy status` shows the workers and any job out on one.

**Build scripts.** Auto mode sees only what goes through `heavy`. A build script that calls `gradlew.bat` itself
reaches the pool once its Gradle step goes through `heavy.cmd` when it is installed, and runs as before where it isn't:

```bat
set "GRADLE_RUN=call"
where heavy.cmd >nul 2>&1 && set "GRADLE_RUN=call heavy.cmd cpu -n MyApp --"
%GRADLE_RUN% "%~dp0android\gradlew.bat" :app:assembleRelease || exit /b 1
```

```powershell
$gradle = @("$PSScriptRoot\android\gradlew.bat")
$gate = Get-Command heavy.cmd -ErrorAction SilentlyContinue
if ($gate) { $gradle = @($gate.Source, 'cpu', '-n', 'MyApp', '--', (Resolve-Path $gradle[0]).Path) }
& $gradle[0] @($gradle | Select-Object -Skip 1) :app:assembleRelease
```

A Gradle step inside a job that already holds a lane (a hook put the whole script on the cpu lane) may still go out.

**What stays here.** Auto mode keeps a job here when a word of it contains `publish`, `install`, `connected` or
`device` (`pool_auto_skip`: publishing uploads from this PC, and install and device tests need the phone plugged in
here; `installDist` doesn't count), and when the build reads a file git ignores, which a trip never sends,
so the worker would build without it: a file the Gradle scripts name (an API key file copied into the assets), any
ignored file inside a `src` folder (a Capacitor app's synced web files and `config.xml`, a generated catalog), or an
ignored module folder with its own build script (`capacitor-cordova-android-plugins`). `--pool` still sends such a
job, with a warning.
With `pool_resign` off, release, sign and bundle jobs stay here too.

**Why it went where it went.** Every job records its reason: the rule that put it on its lane (`cpu lane (rule 1:
gradlew)`), why the pool or GitHub didn't take it (`kept here: no worker could take it (server1: busy ...)`), or why a
worker did (`auto: gradle job, server1 had the most room (4.9 GB free past its reserve)`). `heavy status` prints it
under each job as `why:`, and so does the tray.
Settings: `pool_auto`, `pool_kinds` (`["gradle"]`; add `"dotnet"` and `"cargo"` to send those too), `pool_resign`, `pool_java`, `pool_auto_skip`, `pool_reserve_mb`, `pool_expect_mb`, `pool_outputs`,
`pool_ssh_timeout_s`, `pool_max_send_mb` (400: a bigger first trip stays here).

On the worker, give Gradle a size that fits: `~/.gradle/gradle.properties` with `org.gradle.jvmargs=-Xmx2g`,
`org.gradle.workers.max=2` and `kotlin.compiler.execution.strategy=in-process` suits a 4-core, 8 GB box.

### A Windows worker

A Windows PC (a laptop on the same network) can be a worker too, for what only Windows runs: .NET and WebView2 apps,
`.cmd` and `.ps1` build scripts, and with a graphics card, GPU-lane jobs such as 3D checks.

On the Windows PC, once, in an administrator PowerShell: install OpenSSH Server, allow only your key, open TCP 22 to
your LAN only, and install Python 3.10+. (A script that does all of it is easy to keep beside your key: OpenSSH from
the Windows optional feature, `PasswordAuthentication no`, the public key in
`C:\ProgramData\ssh\administrators_authorized_keys` for an administrator account, a firewall rule scoped to the LAN.)
Then, here:

```
heavy pool add laptop you@192.0.2.50 --key ~/.ssh/worker_key
```

`add` asks the machine what it runs (`ver` answers only in Windows' cmd.exe). For Windows it copies HeavyGate's
modules into `~\HeavyGate` there (`heavy pool install laptop` does it again after an update) and runs it as
`python HeavyGate/heavygate.py`. Differences from a Linux worker:

- OpenSSH on Windows hands the command to `cmd.exe /c`, so each word is quoted for it; a word with `%` or `"` can't
  be passed safely, and that job runs here instead.
- `gradlew.bat` stays `gradlew.bat`, files are sent as they are (no CRLF change), and the program's path gets
  backslashes.
- The RAM cap is the worker's own lane: a Job Object at its RAM ceiling, as on this PC.
- The build cache has no link to a Windows worker yet; its Gradle builds run without the shared cache.
- A worker with a graphics card takes gpu lane jobs too, only when asked: `heavy gpu --pool=laptop -- ...`
  (or `--pool` alone for the first worker that has one). Without `--pool` the gpu lane stays on this PC.
- The worker's tests that open windows or a tray icon are skipped over ssh: the session has no desktop.

### 3D checks on a worker's graphics card

A headless browser started over ssh on a Windows worker gets its real graphics card: Edge's WebGL there reported
`ANGLE (NVIDIA GeForce GTX 1650 ... Direct3D11)`, the same path as on this PC, although the ssh session has no desktop.
So screenshot checks of a web page can run on a worker's gpu lane:

```
heavy gpu --pool=laptop -n "shots" --back "checks/look/**" -- python tools/shot.py checks/look web pv="lat=...&lon=..."
```

- `--back GLOB` brings back the files the job made that match, **even when it failed** (a failing check's screenshots
  are the ones to look at). Again for more patterns. The job's own exit code stays.
- `--send PATH` also sends a file or folder of the project that git ignores (a built `dist\App-1.2.0` folder, test
  data). Again for more. A path outside the project keeps the job here; a trip only carries the project.
- Both work with `heavy split` too.
- Measured: two views of a 3D map app on the laptop, both settled with no errors, the screenshots identical to this PC's by
  eye; the first trip copied the whole project (26.5 MB, 125 s over the laptop's link), later ones only what changed.
  The laptop drew 33-52 fps where this PC draws 131-139, so checks that judge frame rate belong on this PC.
- **An app's own window needs `--desktop`.** An ssh session on Windows has no desktop, and WebView2 won't start
  there (the app runs, its page never comes). `--desktop` runs the job in the signed-in user's session instead,
  through a one-off scheduled task with no console window; its output streams back as usual, the exit code is the
  job's, the lane and RAM cap stay, and the task is deleted after. Someone has to be signed in on the worker. Measured:
  a 3D desktop app's quick window checks (a self-contained build sent with `--send`) PASS 5 scenarios
  and 12 shots on the laptop in 1m49s.
- On a laptop with two graphics chips an app's WebView2 gets the low-power one (the Radeon iGPU, 4-6 fps) unless
  it is started with `--force_high_performance_gpu` (then the GTX 1650, 13-15 fps). Headless Edge got the GTX 1650
  on its own.
- Files named with `--send` don't count against `pool_max_send_mb`; the project itself still does.
- **A Windows worker's own tray:** `heavy pool tray NAME` starts HeavyGate's tray in the signed-in session there
  (and at each sign-in), so a job sent to it shows above its clock like one here.

## Split (one job in pieces, on several machines at once)

A job made of many items (test classes, test scenarios, frames) can run as pieces on this PC and the workers at the
same time:

```
heavy split cpu -n "app tests" --items-from tests.txt -- python run_tests.py {items}
heavy split gpu -n "3D checks" --items s1 s2 s3 s4 -- python tools/check.py --only={items}
```

`{items}` on its own becomes the piece's items as separate words; inside a word they are joined with commas; without
it they go on the end. Each machine gets one piece: this PC's runs in its own lane, a worker's is a pool trip (the
project sent, run in that worker's lane, outputs brought back). Every line is printed with the machine in front, and
a table at the end gives each piece's items, time and result.

Who takes a piece: this PC and every worker that answers and has the tools. The gpu lane splits only to Windows
workers with a graphics card, and a Windows program (`.exe`, `.bat`, `.cmd`, `.ps1`) only to Windows workers. `--on
here,server1` picks the machines. A worker that can't take its piece before it starts hands its items back to this PC.

Piece sizes follow how fast each machine did the last passing run of the same `-n` name (seconds per item, in
`split/<name>.json`). Measured: HeavyGate's own 127 tests take about 270 s on this PC alone and 87-90 s split across
this PC and two small Linux servers, 73 s with a Windows laptop as the fourth machine (its pieces sized by the
speeds saved on the run before). The speeds are per machine, not per item, so a piece that happens to hold the
slowest items still finishes last.

## Build cache (shared by this PC and the pool)

A clean Gradle build on one machine reuses what another already built. `heavy cache setup server1` runs a small Gradle
build cache server on a pool worker that is always on, with a new password, entries in `~/.cache/heavygate-buildcache`,
the least recently used ones dropped past `cache_max_gb` (5), and nothing over `cache_entry_max_mb` (256) kept.
`heavy cache on` then points every Gradle build on this PC and on every pool worker at it, with one file in
`~/.gradle/init.d` (`heavygate-cache.gradle`, readable by you only). No project changes.

```
heavy cache setup server1  # the server, on a pool worker
heavy cache on             # this PC and every worker use it, through ssh
heavy cache                # entries, size, hits and misses, asleep or awake; whether this PC uses it
heavy cache off            # back to local-only builds
heavy cache remove         # off, and the server's socket and service removed
```

**Nothing crosses the network in the clear.** The server listens on its own machine's loopback (127.0.0.1) only; no
port is open to the LAN. Every other machine reaches it through ssh, with the keys the pool already uses, so the
password and the build outputs travel encrypted. Each machine's Gradle talks to `127.0.0.1:8815` on itself:

- a pool worker has a systemd socket there that runs one `ssh -W` to the server for each connection;
- this PC has the same link inside the tray (`heavy tray`), one ssh per connection, started by the first byte sent.
  Without the tray, builds here skip the cache (Gradle warns once and builds as before). `heavy cache tunnel` runs
  the link in a window instead.

**It runs only while builds use it.** On the server a systemd socket holds the port and starts the server on the first
request; the server stops itself after `cache_idle_min` (10) with no request, and the socket starts it again next
time. The links run nothing between connections. `heavy cache` and the tray read its counters over ssh from what it
saved, so looking at it doesn't wake it.

Gradle keeps its own build cache on each machine too, and asks it first: what a machine built itself comes from there,
and the shared cache serves what another machine built. Measured on a clean build of a small Android app (39 tasks)
with each machine's own cache moved aside, so every hit came from the shared one through the ssh links: on one server 49 s
became 19 s, on this PC 33 s became 17 s, 22 tasks from the cache each time (the server counted 25 hits per build;
this PC builds with JDK 17 and the workers with JDK 21). Each new connection from this PC costs about 0.4 s for the ssh
handshake, and waking the server about 0.2 s more.

To build one run without it, set `HEAVYGATE_CACHE=off` or pass `-Dheavygate.cache=off`. Gradle's own `--no-build-cache`
does not turn it off, because the init file switches the cache on.

Settings: `cache_url`, `cache_token`, `cache_host` (written by `setup`), `cache_port`, `cache_dir`, `cache_max_gb`,
`cache_entry_max_mb`, `cache_idle_min`.

## Linux server

A headless Linux box (a build server, a home server you reach over SSH) gets the lanes, `status`, `watch` and the
RAM guard with no desktop at all. Run the guard as a systemd user service, so it keeps watching with nobody signed in
and comes back if it ever fails:

```sh
./install.sh
heavy service install           # writes ~/.config/systemd/user/heavygate.service and starts it
loginctl enable-linger "$USER"   # keep your user's services running after you log out (may need sudo)
heavy service status            # installed, running, and whether linger is on
```

`heavy service install --dry-run` prints the unit and the commands without changing anything; `heavy service remove`
stops it and deletes the unit. Nothing runs as root.

The disk floor (`disk_floor_gb`) watches real disks only: a `/tmp` on tmpfs is RAM, which the RAM ceiling already
covers, so it is left out instead of refusing every job on a server with a small tmpfs.

**Shut down when done** works there too: `heavy shutdown-when-done` arms it, and the guard runs `systemctl poweroff`
once both lanes have been empty for `shutdown_idle_s` plus `shutdown_countdown_s`. Each step goes to the guard log
(`heavy guard --check`). Whether a user may power off is polkit's call: on most servers only an active local session
may, and `heavy service status` says which you have. Over SSH, either add a polkit rule for
`org.freedesktop.login1.power-off`, or set `"shutdown_command": ["sudo", "-n", "systemctl", "poweroff"]` in
`settings.json` with a matching sudoers line (`HEAVYGATE_SHUTDOWN_CMD`, parts split on `|`, overrides it for one shell).
If the command is refused, the guard logs why and the switch is already off, so it never loops.

## Windows and Linux

| | Windows | Linux |
| --- | --- | --- |
| Lanes, queue, RAM wait, estimates, log | yes | yes |
| `status`, `watch`, `free`, `deps` | yes | yes |
| CPU lane limits | below-normal priority, affinity | nice 10, affinity |
| CPU and GPU names | yes | yes (`lspci` for non-NVIDIA) |
| GPU use, memory, temperature | NVIDIA only | NVIDIA only |
| Tray icon, text strip, taskbar readout | yes (readout: Windows 10 taskbar) | no, use `heavy watch` |
| Shut down when done | yes (the tray) | yes (the guard; `systemctl poweroff`) |
| Guard runs | from sign-in (`heavy guard --at-sign-in on`) | with nobody signed in: `heavy service install` (systemd) |
| Pool: send jobs to workers | yes (OpenSSH client) | yes |
| Pool: be a worker | not yet | yes (systemd scope for the RAM cap) |
| State folder | `%LOCALAPPDATA%\HeavyGate` | `$XDG_STATE_HOME/HeavyGate` or `~/.local/state/HeavyGate` |

macOS isn't supported: there, `heavy` just runs the command with no gate.

The lanes are per user and per machine. It won't coordinate between two user accounts or across containers with separate
state folders. Between computers it hands over whole jobs (the pool), each worker keeping its own lanes.

## Finish-time estimates

Each job gets an estimate from `runs.jsonl`: the median of the last five good runs of the same job, with version
numbers and run counters left out of the name, so "App 1.2.0 build" learns from "App 1.1.9 build". The strip shows
"~38m left" per job and "All done around 23:40", and `heavy status` shows the same. A job with no past runs says so.

## Tray and strip (Windows)

`heavy tray` fades in a small strip above the clock whenever anything runs, waits or is queued, and fades it out when
both lanes are idle. It shows:

- the job on each lane and how long it has been there, and why it runs there
- a job out on a pool worker: which worker, how long, and what it is doing (sending, building there, bringing the files
  back, signing here)
- the next jobs in line
- what a job waiting for RAM needs, against what is free
- the free RAM and its biggest users
- small CPU, GPU and RAM graphs for the last two minutes, with each one's peak marked

![The strip with both lanes busy and a queue](docs/strip-busy.png)

**Two views.** "In use only" (the default) shows just what is running, waiting or queued, each with its reason, and
hides when all is idle. "All lanes" lists every lane, free or busy: the GPU lane, each CPU slot, each pool worker (ready
or busy, its free RAM and lane, when it was last asked) and GitHub Actions (running, or idle with the minutes left). It
stays up even when everything is idle, and asks the pool workers again every minute in the background. Switch with the
**all lanes** / **in use only** link on the strip, or "Show all lanes" in the strip settings; **×** in the all-lanes view
goes back to in use only.

![The all-lanes view: free lanes, a job out on a worker, an idle worker, GitHub](docs/strip-all.png)

It's slightly see-through and turns solid under the mouse. **▾** sends it behind other windows, **×** hides it until the
lanes are next idle, and it steps back by itself while a full-screen app is in front. Drag it anywhere and the spot is
kept. Right-click it, or click the tray icon, for the menu. It starts with the display style (this strip, or the taskbar
readout below) and that style's settings: pop up while jobs run, on top, graphs and opacity. Then come the lanes, with
every job in "Lanes in detail", the live view, start at sign-in and shutdown. Together they use about 54 MB of RAM.

**Shut down when done:** tick it in the tray menu (or run `heavy shutdown-when-done`). Once both lanes have been empty
for 3 minutes, the strip counts down 60 s in red and Windows shuts down. Click the strip to cancel. It doesn't force apps
closed, so unsaved work can still stop it, and it only waits for jobs that run through the lanes.

![The shutdown countdown](docs/strip-shutdown.png)

**Taskbar readout (optional):** instead of the pop-up strip, the tray can dock GPU, CPU and RAM use in the taskbar, just
left of the notification area, on every screen's taskbar. The tray menu starts with "Display style (pick one):":
click "Taskbar readout: GPU | CPU | RAM" to use it, or "Pop-up strip above the clock (default)" to go back. The dot
marks the one in use, and its settings are in the submenu just below. Each value takes its lane's colour: green while a job runs there, amber while one waits for RAM,
red when it has stalled. Hover it for the jobs and their finish time, and click it for the tray menu. "Show on every screen's
taskbar" (in "Taskbar readout settings") keeps it to the main taskbar when unticked. A shutdown countdown still pops up the strip. It narrows the task buttons by its width while it shows and gives the
width back on exit. It works with the Windows 10 taskbar; on Windows 11 or a vertical taskbar it stays away.

![The taskbar readout](docs/taskbar.png)

## Dependencies

`heavy deps` lists these with their real paths on your machine.

- **Python 3.9+**, standard library only. tkinter for the Windows strip.
- **State folder:** `gpu.lock` and `cpu.lock` (held by the running job), `queue/<lane>/*.ticket` (waiting jobs),
  `runs.jsonl` (history), and optional `settings.json`. On Windows also `tray.json` (strip and taskbar readout switches), `shutdown.json` and `tray.log`.
- **nvidia-smi** (optional): GPU use, memory and temperature. Without it, GPU figures are left out.
- **NVML** (optional, Windows): GPU use for the strip's graph, from `nvml.dll` in the NVIDIA driver.
- **PowerShell** (Windows): finds Gradle/Kotlin daemons, reads the GPU name without `nvidia-smi`, and writes the sign-in
  shortcut.
- **/proc** (Linux): RAM, CPU, processes and daemon command lines. **lspci** (optional): the GPU name without `nvidia-smi`.
- **gh** (optional): the GitHub CLI for the cloud lane, with the `user` scope for minutes. State in `cloud.json` and
  `cloud.log`. Without it the cloud lane says it can't be read and everything else works as before.
- **Split** (`heavysplit.py`): each job's machine speeds in `split/<name>.json` in the state folder.
- **Build cache** (optional): `heavycache.py`, on a pool worker's loopback behind a systemd socket; each machine
  that uses it has `~/.gradle/init.d/heavygate-cache.gradle` and reaches it over **ssh** (a systemd socket on
  a worker, the tray on this PC). See "Build cache" above.

## Tests

`python selftest.py` runs against a throwaway state folder, so it never touches your real lanes. The cloud lane tests
use a stand-in `gh` and never reach GitHub. The pool tests use a stand-in `ssh` that runs the worker's side on the same
machine, so a whole trip is tested with no server. The disk tests make a drive look full by raising the floor, so they never
fill one. The tray tests skip on Linux. The tray test briefly shows the strip and tray icon.

## Legal

**License.** MIT, see [`LICENSE`](LICENSE). Use it, change it and ship it, commercially too. Keep the copyright notice.

**No warranty.** HeavyGate is provided "as is". It runs your commands, and a few things it does on purpose have real
effects:

- `heavy free` and the RAM wait stop idle Gradle and Kotlin daemons belonging to your user. Turn that off with
  `"free_daemons": false`.
- **Ollama**: apps reach it over HTTP, so no lane sees its work, but a loaded model holds the GPU and RAM. `status`, the tray
  and the rules window show what's loaded. A GPU job unloads every model before it starts, a CPU job only when RAM is short,
  and so does `heavy free`. A request in progress finishes first, and the next request reloads the model, which costs that
  app a few seconds. Nothing stops an app loading a model during a GPU job; `status` says when they share the GPU. Turn it
  off with `"free_ollama": false`; `"ollama_url"` points somewhere else.
- Stopping `heavy` stops the job under it.
- Shut down when done powers off the PC, though it doesn't force apps closed.

The RAM ceiling keeps lane jobs and coding agents' commands from filling the RAM. Programs you start yourself are never
ended, so one of those can still fill it. The guard ends only what your own user can end, so commands an agent runs as
another account (a sandbox user) are logged as "could NOT end".

**Privacy.** Everything stays on your machine. HeavyGate has no telemetry, no accounts and makes no network calls.
`runs.jsonl` keeps job names, working folders and the first 400 characters of each command in plain text, so keep
secrets out of command lines, and read the file before sharing it. To clear the history, delete it.

**Third parties.** HeavyGate bundles no third-party code. NVIDIA, Windows, Linux, Gradle, Kotlin, Blender and other names
are trademarks of their owners and are used only to describe what it works with. This project isn't affiliated with or
endorsed by any of them.
