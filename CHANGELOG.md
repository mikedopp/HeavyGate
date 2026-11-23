# HeavyGate changelog

## 1.34.0 (2026-11-22)

- **Auto mode can send .NET and Rust builds** (`"pool_kinds": ["gradle", "dotnet", "cargo"]`), and they come back
  whole:
  - an output folder the command names (`dotnet publish -o dist\App-1.2-win-x64`, `cargo --target-dir`,
    `--artifact-dir`) is brought back too, even when its files keep old times (publish copies them out of an
    up-to-date `bin`);
  - a build that writes outside the project a trip sends (`-o ..\elsewhere`) stays here in auto mode, and `--pool`
    warns that the output stays on the worker.
- `pool_auto_skip` reads per kind: `dotnet publish` is a build and may go; `dotnet nuget push` and installs stay here.

## 1.33.0 (2026-11-22)

- **The strip lists what is using the memory.** Under the RAM line, up to four of your own apps over 150 MB, biggest
  first, each with its size and a **close** link: it asks that app's windows to close (as its X does) and ends it if
  it is still running 8 s later. The app leaves the list while it closes. It is the same list as the free-RAM window
  (never Windows, security, HeavyGate, Python, SSH, dev tools or a lane's job), replacing the old "biggest:" note,
  which named any process, WebView2 helpers included.

## 1.32.1 (2026-11-22)

- **A pool key Windows' own ssh refuses is fixed, not reported as an unreachable worker.** The tray runs
  `System32\OpenSSH\ssh.exe`, which ignores a private key that any other account can read ("bad permissions ... This
  private key will be ignored", then "Permission denied (publickey)"). Git's ssh doesn't check, so the same worker
  answered from a Git shell and showed as unreachable in the tray. A key file inherits its folder's permissions, and a
  profile carried over from an older Windows install can still list a deleted account there. The first time a
  process uses a pool key, HeavyGate checks it, and if anyone but you, SYSTEM or Administrators can read it, keeps only
  you and SYSTEM and says so.

## 1.32.0 (2026-11-22)

- **"free RAM..." on the strip** (and "Free RAM: close apps..." in the tray menu) opens a small window that lists your
  own apps by memory, with a checkbox each. "Close ticked" asks each app's windows to close, as clicking its X does,
  and ends whatever is still running 8 s later (unsaved work in it is lost; the window says so). It never lists
  Windows, its shell, services and security, HeavyGate, Python, SSH, dev tools, or any process of a job running in a
  lane. A pool worker shows the link on its own strip, so the person at that machine can make room for a build.
- **`heavy apps`** lists the same apps as text (`--json`, `--min-mb`); `heavy apps --close NAME...` closes them. Over
  ssh it lists the signed-in user's desktop session, so you can see from another machine what is using a worker's
  RAM.

## 1.31.0 (2026-11-22)

- **A dropped login is tried again.** Windows' SSH server drops a login when several start at once ("banner exchange
  ... connection abort") and under load can refuse a good key once ("Permission denied (publickey)"). One of those
  marked the laptop worker unreachable in the tray, or failed a trip. Every pool call (asking a worker, sending the
  project, the job, bringing files back) now tries a failed login up to 3 times. Only a failure before the remote
  command started is retried: a job that ran and failed is never run again, and a refused connection (machine off)
  still fails at once. Measured: 28 status asks at the laptop at once all answered (1 in 12 dropped before).
- **A worker's strip says who sent the job**: "why: sent from MAIN-PC by heavy pool, cpu lane" (`pool-worker run
  --from`). Workers need 1.31.0 for trips from a 1.31.0 machine.

## 1.30.0 (2026-11-22)

- **`heavy pool tray NAME`**: a Windows worker shows its own tray and strip, so a job sent there is seen on its
  screen like one on this PC. It starts in the signed-in user's desktop session (a one-off task, deleted after) and
  at every sign-in after (Startup shortcut). `heavy tray` run over ssh does the same instead of starting an icon
  nobody can see.
- `heavy pool install` copies the tray and rules windows (`heavy*.pyw`) too.

## 1.29.0 (2026-11-22)

- **`--desktop`** for pool trips and `heavy split`: on a Windows worker the job runs in the signed-in user's desktop
  session (a one-off scheduled task, no console window), where an app can open its window. WebView2 won't start in
  an ssh session. Output streams back, the job's exit code and the lane's RAM cap stay, the task is deleted after.
  Refused for a worker that isn't Windows (the job runs here).
- `--send` files don't count against `pool_max_send_mb` (they were named on purpose); the project still does.
- Measured: a 3D desktop app's quick checks PASS on the laptop worker in its desktop session (5 scenarios,
  12 shots, GTX 1650).

## 1.28.0 (2026-11-22)

- **`--send PATH` and `--back GLOB`** for pool trips and `heavy split`: send a file or folder of the project that git
  ignores (a built dist folder), and bring back the files the job made that match, even when it failed. A path
  outside the project keeps the job here.
- **3D checks on a Windows worker's graphics card**: headless Edge over ssh gets the real GPU (GTX 1650 through
  Direct3D 11 on the laptop). A 3D map app's screenshot check ran there and its shots came back SHA256-checked.
- A failed bring-back no longer turns a failing job's exit code into 1.

## 1.27.0 (2026-11-22)

- **A gpu lane job can go to a worker**: `heavy gpu --pool=NAME` (or `--pool` for the first worker with a graphics
  card) runs it in that Windows worker's gpu lane. It used to run here without a word. A worker without a graphics
  card says so and the job runs here; without `--pool` the gpu lane never leaves this PC.
- **Fix: with git 2.5x a Gradle build with ignored files under src stayed here.** Newer git also lists a folder that
  only holds ignored files (`app/src/main/`) as ignored; HeavyGate now keeps only what `git check-ignore` confirms, and
  takes the files of an ignored folder under src one by one, as it did with older git.
- The selftest runs on a Windows worker over ssh: thread counts come from the machine's share and the lane's
  processors, window and tray tests are skipped without a desktop, and the cloud tests skip without git.
- Measured on real machines: the selftest's 127 tests split across this PC, two Linux servers and a Windows laptop
  in 73 s, all passing (about 270 s on this PC alone).

## 1.26.0 (2026-11-22)

- **Windows workers.** `heavy pool add` asks a machine what it runs; a Windows PC gets HeavyGate copied into
  `~\HeavyGate` (`heavy pool install NAME` again after an update) and its jobs go through cmd.exe with every word
  quoted for it (a word with `%` or `"` keeps the job here). `gradlew.bat` stays as it is, no CRLF change, the RAM cap
  is the worker's own Job Object, and a job can go to the worker's gpu lane. `heavy pool` shows its OS and graphics card.
- **`heavy split`**: one job of many items as pieces on this PC and the workers at once, `{items}` where they go.
  Piece sizes follow each machine's speed on the last passing run; the gpu lane splits only to Windows workers with a
  graphics card. Measured: the selftest's 127 tests in 87-90 s on this PC and two servers, about 270 s here alone.
- **Fix: a job's own words with & | < > ^ ( ) went to cmd.exe unquoted on Windows**, so `heavy cpu -- python x.py
  "a&b"` ran `b` as a command. Such words are quoted now; a word that is only an operator (`&&`, `2>&1`) is left alone.
- The selftest runs inside a lane too (as a split piece does): it measures priority and processors from where it
  starts, and keeps its disk tests off a /tmp in RAM.

## 1.25.0 (2026-11-22)

- **The build cache no longer crosses the LAN in the clear.** In 1.24.0 it spoke plain HTTP on the LAN, so its password
  could be read by anything watching the network. Now it listens on its server's loopback only, the LAN firewall rule
  is taken away, and every other machine reaches it through ssh with the pool's keys: a systemd socket on each pool
  worker runs one `ssh -W` per connection, and the tray carries this PC's link the same way (`heavy cache tunnel`
  without the tray). Run `heavy cache setup NAME` and `heavy cache on` again to move a 1.24.0 cache over.
- **The cache runs only while builds use it.** A systemd socket on the server starts it on the first request; it stops
  itself after `cache_idle_min` (10) with no request. Its counters are saved, so they carry on across starts, and
  `heavy cache`, `heavy status` and the tray read them over ssh from the pool's answer, which doesn't wake it.
  Measured with each machine's own Gradle cache moved aside, so the hits came through the links: one server 49 s became
  19 s, this PC 33 s became 17 s. (1.24.0's figures may have come partly from each machine's own cache.)

## 1.24.0 (2026-11-22)

- **A shared Gradle build cache.** `heavy cache setup NAME` runs a Gradle HTTP build cache on a pool worker (a
  systemd user service, port open to the LAN only, a new password, least recently used entries dropped past 5 GB), and
  `heavy cache on` points every Gradle build on this PC and the pool workers at it with one init file. A clean build
  that another machine already ran takes the outputs from the cache: on one server 49 s became 19 s, on this PC 41 s became 22 s.
  `HEAVYGATE_CACHE=off` skips it for one build; `heavy cache off` / `remove` undo it.
- **The cache in status and the tray.** `heavy status` has a BUILD CACHE line (entries, size, hits and misses, whether
  this PC uses it), and the all-lanes view has a CACHE row, asked in the background with the pool workers.

## 1.23.0 (2026-11-22)

- **Two views on the strip: in use only, and all lanes.** "In use only" (the default) shows just what is running,
  waiting or queued and hides when all is idle. "All lanes" lists every lane, free or busy: the GPU lane, each CPU slot,
  each pool worker (ready or busy, free RAM, its lane, when it was last asked; asked again every minute in the
  background) and GitHub Actions (running, or idle with the minutes left). Switch with the link on the strip or "Show all
  lanes" in the strip settings.
- **Every job says why it runs where it does.** The rule that put it on its lane, why the pool or GitHub didn't take it
  (a skip word, an ignored input, no worker free and why), or why a worker did (auto mode, the most room). Shown as
  `why:` under each job in `heavy status` and on the strip.
- **App bundles go to the pool too.** An `.aab` a worker builds is signed again here with the key a local build uses,
  with the JDK's jarsigner (the worker's signature taken off first, the certificate checked with keytool), so
  `bundleRelease` left `pool_auto_skip` (it comes back with `pool_resign` off).
- **Bring-back fix.** A file the worker kept from an older build that never came back here (a debug APK from another
  trip) stays there, and an APK signed and renamed here (app-release-unsigned.apk to app-release.apk) is not fetched
  again. Before, both came back on the next trip and the second replaced the signed APK.

## 1.22.0 (2026-11-22)

- **The tray shows jobs out on the pool.** A trip holds no lane here, so the strip above the clock, the hover text and
  the menu showed nothing while a build ran on a worker. Now each one is a row: the job, the worker it went to, how long,
  and what it is doing (sending the changes, building there, bringing the files back, signing the APKs here), with what
  was sent and the RAM cap there. The menu also lists the workers. `heavy status` shows the same phase.
- **Build scripts reach the pool.** A script's Gradle step that goes through `heavy.cmd` can now go to a worker:
  absolute paths into the project (`C:\App\android\gradlew.bat -p C:\App\android`) become paths in the worker's
  copy (a path outside the project keeps the job here; a tool of this PC becomes the worker's own), the Gradle folder is
  found from `-p` or beside the gradlew it calls, and a Gradle step inside a job that already holds a lane may go out.
  The README shows the few lines a `.cmd` or `.ps1` script needs.
- **Outputs come back by hash.** A trip now asks the worker for the SHA256 of every output (`pool-worker sums`) and
  brings back what is missing here or changed since it last came back, instead of only what the job just wrote. A build
  folder cleaned here gets its APK back even when the worker had nothing to rebuild. Gradle outputs also cover
  `build/install/**` (`installDist`) and `build/libs/**`.
- **More ignored inputs keep a build here.** Besides files the Gradle scripts name, any git-ignored file inside a `src`
  folder and an ignored module folder with its own build script now keep an auto-mode build here: a Capacitor app's
  synced web files, `config.xml` and Cordova plugins, or a generated catalog in the assets, would be missing on the
  worker. `:core:installDist` no longer counts as an `install` job.

## 1.21.0 (2026-11-22)

- **APKs from a worker are signed here.** A worker never gets a signing key, so what it builds carries its own debug key
  (or none) and won't install over your app. Now every APK a trip brings back is signed again on this PC with the key a
  local build uses (`heavysign.py`): a release APK of a project with `keystore.properties` gets that key, everything
  else this PC's debug key. The signed APK's certificate is checked against the keystore's; one that can't be signed is
  renamed `.apk.worker-signed` and the job exits 1. So release builds may go to the pool now: `release` and `sign` left
  `pool_auto_skip` (they come back with `pool_resign` off).
- **A build that reads an ignored file stays here.** When the Gradle scripts name a file git ignores (an API key file
  copied into the assets), a trip can't send it and the worker would quietly build without it. Auto mode keeps that
  build here; `--pool` sends it with a warning. Ignored copies of tracked files and signing files don't count.

## 1.20.0 (2026-11-22)

- **The pool: your other machines as more cpu lanes, over SSH.** `heavy pool add NAME USER@HOST --key FILE` adds a
  worker (a Linux server, for now); `heavy pool` asks each one now and says whether it could take a job, or why not.
  `heavy cpu --pool[=NAME] -- gradlew assembleDebug` sends one job; `heavy pool auto on` sends every Gradle job to a
  worker that can start it now, and runs it here when none can. Nothing runs on a worker but SSH and HeavyGate.
- A trip copies the project's git checkout to the worker, sending only what changed and never ignored files or
  `local.properties`; runs the job in the worker's own cpu lane under a systemd `MemoryMax` that leaves the worker
  `pool_reserve_mb` (2 GB) for its own services; streams the output; and brings back what it made
  (`build/outputs/**` for Gradle), each file checked against its SHA256. `gradlew.bat` becomes `./gradlew
  --no-daemon`, and scripts get LF line endings and the executable bit on the way. Anything that fails before the job
  starts there runs it here instead.
- **Keys stay here.** Auto mode keeps release, bundle, publish, sign, install and device jobs on this PC
  (`pool_auto_skip`): release builds sign with this PC's keys, and installs need the phone plugged in here. Key files
  (`*.jks`, `*.keystore`, `*.p12`, `*.pfx`, `*.pem`, `*.key`, `keystore.properties`, `.env` ...) are never sent, even
  when git tracks them.
- `heavy status` lists the workers and jobs out on them; `heavy log` shows each trip. Pool runs don't count toward
  this PC's finish-time estimates.
- New self-tests: a whole trip against a stand-in `ssh` (sent, run in the worker's lane, outputs back, only changes
  sent the second time, deletions carried), a failed job, an unreachable worker, the worker choice, command translation
  and path safety.

## 1.19.1 (2026-11-21)

Found by running 1.19.0 on a real Linux server over SSH.

- **A tmpfs `/tmp` no longer blocks every job.** The disk floor counted `/tmp`, a 3.8 GB tmpfs, as a disk and refused
  each lane job (exit 28). RAM-backed mounts (tmpfs, ramfs, devtmpfs) are now left out; the RAM ceiling covers them.
- **`shutdown_command` setting.** Over SSH logind answers `challenge` for power-off, so `systemctl poweroff` would be
  refused. A server can now set the command it is allowed to run, e.g. `["sudo", "-n", "systemctl", "poweroff"]`.
  `heavy service status` and `heavy shutdown-when-done` show the command and warn when it would be refused.
- `install.sh` is executable in the public repo again.

## 1.19.0 (2026-11-21)

- **Linux server support.** `heavy service install` runs the RAM guard as a systemd user service
  (`~/.config/systemd/user/heavygate.service`): it starts now, comes back if it fails, and with
  `loginctl enable-linger` keeps watching with nobody signed in. `heavy service status` says whether it is installed,
  running and lingering; `remove` takes it away; `--dry-run` prints the unit and commands without changing anything.
  Nothing runs as root.
- **Shut down when done on Linux.** With no tray on a server, the guard watches the switch: once both lanes have been
  empty for `shutdown_idle_s` plus `shutdown_countdown_s` it runs `systemctl poweroff`, logging each step to the guard
  log. The switch goes off before the command runs, so a refused power-off (polkit) is logged once and never loops.
  `heavy shutdown-when-done` says when no guard is running to do it. The Windows tray now uses the same wait and
  countdown code (`power_step`).
- The self-test workflow runs on Ubuntu as well as Windows. New self-tests: the wait/countdown logic, the service unit,
  and a real guard on Linux running a stand-in shutdown command.

## 1.18.4 (2026-11-21)

- **Your agents and tools are settings now.** `guard_agents` (the coding agents whose commands the RAM guard may end)
  is empty by default; list your agents' process names in `settings.json`. `gated_tools` names your own tools that
  already take a lane, so the hook leaves them alone. `settings.example.json` lists every setting with its default.
- **The self-tests pass on a fresh GitHub Windows runner** (`selftest.yml`, started by hand). A GPU lane job keeps
  its caller's priority, and the runner starts every step below normal, so the test now compares against its own.
  Three tests waited a fixed time for a job to start, which a 2-core runner can miss; they now wait for it. The rules
  window's self-test makes its "all threads" tip on 2 cores too, and the RAM-cap test allows Windows' peak counter
  128 MB past a small cap.
- **`heavy status` can't hang on PowerShell any more.** The Gradle/Kotlin daemon scan and the GPU name lookup run
  PowerShell; on a fresh profile it can stall, and when its 30 s timeout hit, a child it left kept the output pipe
  open, so `status` (and the self-test calling it) waited for good. Helpers now write to temp files and a timeout
  ends their whole tree. The daemon scan skips PowerShell entirely when no Java process is running, and the GPU name
  comes from the registry instead of PowerShell, which took 20 s to start on a fresh profile.
- A stuck self-test run prints where every thread is, every 7 minutes.

## 1.18.3 (2026-11-16)

- **The RAM guard no longer ends your browser.** Chrome opened from a desktop agent app (a link in its window) runs
  as a child of that app, so the guard counted it as one of the agent's commands and, with RAM at 95% during a
  server build, ended a 7.5 GB and then an 8.0 GB Chrome on 2026-11-16 (Chrome "kept crashing"). A browser or Explorer
  sitting directly under an agent (`guard_spare`: chrome, msedge, firefox, brave, opera, vivaldi, explorer) is now
  never ended. A browser that a command started (headless Edge from a script) still belongs to that command.
  Self-test: `test_guard_spares_the_browser_an_agent_app_opened`.

## 1.18.2 (2026-11-15)

- **The runner probe works on Windows runners.** A Windows runner ends every process a step started when that step
  ends, so the probe started with `Start-Process` died at once and an 82-minute build showed no readings.
  The probe (1.1.0) now has `--start`: it leaves the step's job (`CREATE_BREAKAWAY_FROM_JOB`) or, if the job won't
  allow that, has WMI start it, passing the step's settings in a file it deletes. Linux starts it in its own session.
  One `bash` step now starts it on both, and `--final` prints why when there were no readings. `probe-test.yml` proves
  it on a Linux and a Windows runner (started by hand, about 4 quota minutes).

## 1.18.1 (2026-11-15)

- **A slow build can wait longer.** A mapping (or one dispatch) takes `--max-min N`: HeavyGate cancels that run only after
  N minutes instead of `cloud_max_min` (60). A Rust Windows release build is mapped at 190 with its workflow's
  `timeout-minutes` at 180: its first cold build was still compiling at 55 minutes on the 2-core runner a private
  repo gets.

## 1.18.0 (2026-11-15)

- **A GitHub build shows like a local one.** The strip stays up for the whole run and keeps the result for
  `cloud_done_show_s` (120 s) after it: built or failed, the peaks, and where the files went. The run's name on the strip
  is a link: a click opens its page on GitHub.
- **The runner's CPU, RAM and disk on the strip.** GitHub has no live numbers for its runners, and a running job's log
  can't be read until the job ends, so the new runner probe (`heavyprobe.py`, standard library, Linux and Windows) posts
  a commit status every 15 s and HeavyGate reads the history back: CPU and RAM with sparklines, disk, cores, and how old
  the reading is. `heavy cloud probe <checkout>` copies it into a repo (stamped with its version) and prints the
  workflow steps.

## 1.17.3 (2026-11-15)

- **GitHub links in the tray menu.** "GitHub links (open in the browser)": each build on GitHub now and the 8 most
  recent runs (each opens its run page), the Actions page of every repo mapped in `heavy cloud map`, and the billing
  page with the minutes used. Built from what the tray already holds (no gh call); it only opens `https://github.com/`
  addresses it built itself.
- **A plain `taskkill` ends the tray.** Windows' close message used to destroy the strip's widgets and leave the
  process running half-closed, logging an error every second. Closing any tray window now exits like Exit does, and a
  destroyed-window error ends it instead of repeating.

## 1.17.2 (2026-11-15)

- **A checkout's other remotes count.** The cloud lane only matched a project by its `origin` remote, so a private fork
  kept beside an upstream origin (`origin` = the upstream project, `mine` = your fork) was "No checkout
  found" and its builds could never go to GitHub. `find_checkout` and the push check now use whichever remote points
  at the repo (origin first) and compare against that remote's branch tip.
- The strip names a GitHub build once: "MyApp Windows cloud test", not "MyApp MyApp Windows…".

## 1.17.1 (2026-11-15)

- **A build HeavyGate sends to GitHub shows on the tray strip at once.** While `heavy cloud dispatch --wait` or auto
  mode waits on a run, it keeps a note in `cloud-active\<run id>.json` (status, start, its pid); the tray reads it every
  few seconds with no gh call, and the strip comes up for it as it does for a lane job ("CLOUD ► MyApp Android
  (phone + TV) 1m15s"), then hides when the files are home. Before, the strip came up only for local lane jobs, and
  GitHub runs alone needed the tray's `cloud_strip` option plus a refresh up to 5 min late, so a 4-minute Android
  build came and went unseen. A note whose process is gone is removed. Other GitHub runs (CI you didn't start through
  HeavyGate) still follow `cloud_strip`.
- **The tray shows the second cpu slot.** It read only the first slot's lock; it now uses `lane_rows()` like status.

## 1.17.0 (2026-11-15)

Builds stop being a chore. From the run log for 11-08..15: 591 gated jobs waited 739 min in all, 549 of it for the one
cpu slot; 228 light jobs (dotnet build/test, build.cmd, tar, 7z) used a median 75 MB of RAM yet waited 277 min.

- **Light jobs run ungated.** The starter rules now gate only Gradle, `dotnet publish`, `build-android.cmd`, cargo,
  Blender, `shot.py` and `desktop_check.py`. `dotnet build`, `dotnet test`, `msbuild`, `build.cmd`, `tar` and `7z` run
  straight away; the RAM guard still watches them like any agent command.
- **Two cpu jobs at once** (`cpu_slots`, default 2, at most 4; gpu stays at one). The second slot has its own lock
  (`cpu-2.lock`); `peek()`/`holder()` still answer for the lane, `peeks()`/`holders()` list every slot, and status and
  the tray show a second running job as its own CPU row.
- **Foreground agent builds wait instead of failing.** The hook adds `--wait-max 90` (`foreground_wait_s`) instead of
  `--no-wait`: a foreground build waits up to 90 s for a slot and only then exits 75. A foreground build mapped to the
  cloud lane runs here rather than exiting 75.
- **RAM ceiling 92%, ended at once at 95%** (were 85% / 90%). With Chrome holding 10-13 GB, 85% left builds 0-3 GB.
- Self-tests: existing queue tests pin `cpu_slots` to 1; new tests for two slots and `--wait-max`.

## 1.16.0 (2026-11-14)

- **GitHub artifact storage is watched.** GitHub bills storage in GB-hours and includes the plan's GB for every hour of
  the month (Free: 0.5 GB, about 360 GB-hours). With no budget set, it refuses new uploads once that is used up, so a
  build would run, spend its minutes and then fail at the upload. `heavy cloud` and status now show the GB-hours used,
  what is stored now (each repo's artifact list, read hourly), the projection to month end and the run-out date. When
  the month's storage is used up, mapped builds stay here (new rule `storage`). Today: 210 of 360 used, on pace for 222.
- **Old artifacts are deleted.** Anything older than `cloud_keep_days` (3) is deleted from your repos as the list is
  read, and each deletion goes to `cloud.log`. `heavy cloud prune [--days N] [--dry-run]` does it now, and
  `heavy cloud keep DAYS|off` changes the age.
- **A build brought home is deleted on GitHub** once its files are here and any SHA256SUMS file in it matches. On a
  mismatch the copy on GitHub is kept and the build exits 1 (`cloud_delete_after` turns the deletion off).
- **Disk floor.** A job doesn't start while the drive it runs in, the temp drive or the profile drive (build caches) has
  under `disk_floor_gb` (15 GB) free. It's ended if one falls under `disk_stop_gb` (3 GB) while it runs. Both exit 28,
  and `hold()` raises `OSError` ENOSPC. `disk_paths` adds drives. Status and the tray show free space per drive,
  and the log shows how far the job's drive went down. A mapped build goes to GitHub instead when the disk here is under
  the floor (new rule `disk`).
- Repos that the billing report charged storage to are watched even with no checkout here.
- The stand-in gh's "find the run by its commit" branch could never be reached, so that test passed without testing
  it. The branch now runs, and the test checks the build really came back from GitHub.

## 1.15.1 (2026-11-14)

- A repo you map is watched at once, and `heavy cloud map` rereads the projects' workflow files when it doesn't know
  the workflow yet. Before, a workflow added since the daily scan was missed, so a fresh mapping had no past runs to
  learn its GitHub time from.
- The folder a cloud build's files went to printed with mixed slashes.
- Proven for real on an Android app: `heavy auto --cloud` sent its Gradle build (core tests + release APK) to GitHub. The build
  took about 2 min and 2 quota minutes, and the APK and its checksum came back into `dist\cloud` with the sum matching.

## 1.15.0 (2026-11-14)

- **Builds mapped to GitHub workflows.** `heavy cloud map you/App android.yml --match assembleRelease --folder App` says
  which build command, in which project, a workflow can run instead. It matches the way a lane rule does: whole words
  anywhere in the command, and folder anywhere in the path. `heavy cloud map` lists the mappings with their expected
  GitHub time, and `heavy cloud unmap N` removes one. They live in `cloud-jobs.json`.
- **Learned GitHub cost.** Every build that ran on GitHub is logged in `cloud-runs.jsonl` with its real job minutes
  (each job rounded up, as GitHub bills). The next estimate is the median of the last five good runs, else of the
  workflow's recent runs on GitHub, else `cloud_est_min` (10).
- **Auto mode, opt-in (`heavy cloud auto on|off`).** `heavy auto` and `heavy cpu` check a mapped build before taking
  the lane. When the advice says GitHub, the build runs there instead. Caps: `cloud_auto_day_min` (60) and
  `cloud_auto_month_min` (400) quota minutes. It only builds what GitHub already has: the checkout's origin must be the
  mapped repo, with nothing uncommitted or untracked, and HEAD must be the branch's tip on GitHub. Otherwise the build
  runs here, and the job says why. Never sent: GPU lane jobs, a job inside another, `--no-cloud`, and
  `HEAVYGATE_CLOUD=off`. A foreground `--no-wait` call (the agent hook's) gets exit 75: rerun it in the background,
  where it waits for the build. `--cloud` sends one mapped build on purpose; the quota, charge and push checks still
  apply, and it is not counted against the caps.
- **The round trip.** It starts the workflow at that branch, finds the run (by URL, or by commit with an older gh),
  follows it every `cloud_poll_s` (15 s) and cancels it after `cloud_max_min` (60). It downloads the artifact into the
  checkout's `dist/cloud` (the mapping's `dest`, without the artifact's own folder), and exits like the build would
  have: 0 built, 1 failed on GitHub (with the `--log-failed` command; it is never quietly rebuilt here), 124 too
  slow, 130 stopped here (the GitHub run is cancelled too). If GitHub won't start it, the build runs here.
- `heavy log` lists cloud runs next to local ones, with their quota minutes. `heavy cloud` shows auto mode's spend
  against its caps. `heavy cloud advise -- COMMAND` shows the mapping, the learned time and whether GitHub would build
  exactly this checkout. `heavy cloud dispatch ... --wait` does the round trip by hand.
- git runs with prompts off, so a GitHub check can never hang on a sign-in window.

## 1.14.1 (2026-11-14)

- The cloud lane's refresher saved `cloud.json` and stamped it "read just now" every 5 seconds, even when nothing was due.
  It now writes only when it actually read something from GitHub, so the age in `heavy cloud` is the real one, and the
  tray no longer rewrites the file all day.

## 1.14.0 (2026-11-14)

- **A cloud lane: GitHub Actions, watched.** `heavy cloud` reads your plan and the billing usage report through `gh`, and
  shows the Actions minutes used this month against the quota, with Windows counted 2x and macOS 10x. It also shows the
  minutes left, the usable part above a 20% reserve kept for releases, the pace to month end and the reset date, what
  GitHub actually charged, the minutes per repo and runner, last month's total, and every run in flight with recent
  runs. It finds the repos with workflows in the projects folder (your own origins only) and marks the ones you can
  start by hand.
- **Where should a build run?** `heavy cloud advise -- COMMAND` picks here or GitHub. GPU work stays here. Nothing is
  sent when the lane can't be read, when anything has been charged, or when the job would dip into the reserve. A job
  goes to GitHub when the PC is short of RAM for it, when its lane is 3+ min behind, or when it is a 10+ min job here and
  cheap against the quota. It prints the cost in quota minutes and what is left after.
- **`heavy cloud dispatch REPO WORKFLOW`** starts a `workflow_dispatch` workflow, only when the advice says cloud
  (`--force` overrides), and logs each one to `cloud.log`.
- **Status and tray.** `heavy status` (and `--json`) has a CLOUD block. The tray tooltip and menu show it, and there is a
  "GitHub Actions: minutes and runs" item. The strip gets a CLOUD row while the lanes are busy. "Pop up for GitHub Actions
  runs too" is an opt-in that brings the strip up for a GitHub run alone. A background thread in the tray keeps
  `cloud.json` fresh: usage every 10 min, runs every 5 min, a run in flight every 20 s. It backs off 2 min after a
  failure such as a missing scope. Status readers never call GitHub.
- New file `heavycloud.py` (standard library only). New settings: `cloud`, `cloud_quota_min`, `cloud_reserve_pct`,
  `cloud_wait_s`, `cloud_long_min`, `cloud_long_share`, `cloud_usage_s`, `cloud_runs_s`, `cloud_active_s`,
  `cloud_repos`, `cloud_drop_root`. `HEAVYGATE_GH` swaps the gh command (the self-tests use a stand-in).

## 1.13.0 (2026-11-14)

- **A RAM ceiling: in-use RAM never goes past 85%.** Before this, the gate checked RAM only before a job started. A
  compile then filled the PC to 100% and sat there until Windows closed the hung apps. Now three things hold the line:
  - **Every job runs under a hard memory cap** (Windows Job Object). The cap is whatever was left under the ceiling when
    the job started. Past it, the job's allocations fail and the compiler stops with an out-of-memory error, instead of
    the whole PC running out.
  - **A job is ended if the PC goes over the ceiling while it runs,** whatever pushed it there: after 3 seconds over
    85%, or at once at 90%. Only a job holding 300 MB or more is blamed. It says why and suggests fewer parallel jobs
    (`cargo -j 2`, `dotnet -m:2`, `gradle --max-workers 2`).
  - **`heavy guard` covers commands that never entered a lane.** It runs in the background and checks RAM every second.
    Over the ceiling, it ends the biggest command a coding agent started (anything named in
    `guard_agents`): the shell a tool call opened, with everything under it. It never touches the agent itself, or
    anything you started yourself. Something an agent left running that isn't a shell, such as an MCP server, is ended
    only past 2 GB. What it did goes to `guard.log`, and `heavy status` shows the last action. `heavy guard --check`
    shows what it would end right now. The tray and the agent hook start it if it isn't running, and so does sign-in.
- **A job no longer starts anyway past the ceiling.** It used to start after 10 minutes of waiting for RAM, enough or
  not. Now it waits until its usual need fits under the ceiling. After 10 minutes it starts only if at least 1.5 GB
  (or its usual need, if that is smaller) is left under the ceiling, and its cap still applies.
- **More compilers go through the lanes:** the starter rules now include `cargo`, `dotnet test` and `msbuild`.
- Settings: `ram_ceiling_pct` (85), `ram_kill_pct` (90), `ram_over_s` (3), `guard_agents`, `guard_min_mb` (300),
  `guard_big_mb` (2000). The log has `ram_cap_mb`, `job_peak_mb` and `ram_peak_pct` for each run. `HEAVYGATE_GUARD=off`
  stops the hook and the tray from starting the guard.

## 1.12.2 (2026-11-11)

- **The tray menu leads with the choice.** Right under the version: "Display style (pick one):", then "Pop-up strip above
  the clock (default)" and "Taskbar readout: GPU | CPU | RAM", with a dot on the one in use. Click the other to switch.
  The chosen style's settings sit in one submenu beside it ("Pop-up strip settings" or "Taskbar readout settings").
- **Less to read in the menu.** The lanes are three short lines (GPU, CPU, free RAM); every job, its folder, the
  estimates and the CPU and GPU names moved to "Lanes in detail".

## 1.12.1 (2026-11-11)

- **The pop-up strip is the default again; the taskbar readout is a choice.** The tray menu now has two displays,
  "Display: pop-up strip above the clock" (the default, as before 1.12.0) and "Display: GPU, CPU and RAM in the
  taskbar". Pick one; the strip's own switches show while it's chosen, "Also in other screens' taskbars" while the
  readout is. A shutdown countdown still pops up the strip whichever display is chosen.

## 1.12.0 (2026-11-11)

- **GPU, CPU and RAM in the taskbar.** The tray now docks a small readout in the taskbar, just left of the
  notification area: `GPU 41% | CPU 63% | RAM 72%`. Each value takes its lane's colour: green while a job runs there,
  amber while one waits for RAM, red when it has stalled; RAM turns amber when it is nearly gone. The icon in front is
  the tray icon. Hover for the jobs and when they should be done, and click for the tray menu.
- It shows on every screen's taskbar; the menu has "Show GPU, CPU and RAM in the taskbar" and "Also in other screens'
  taskbars". The task buttons give up that much width while it shows and get it back when the tray exits.
- It runs on its own thread, so a busy tray can never stall the taskbar. Text is drawn with per-pixel alpha on the
  taskbar's own glass, follows the light or dark taskbar, and turns solid in high contrast.
- Windows 10's taskbar only. On Windows 11's taskbar, or a vertical one, it stays away and says why in `tray.log`.

## 1.11.0 (2026-11-06)

- **HeavyGate can see Ollama.** `status`, `status --json`, the tray and the rules window show the models it has loaded and
  how much is on the GPU, and `status` says when one shares the GPU with a job.
- **A GPU job clears the GPU first.** It unloads Ollama's models before it starts, and a CPU job does the same when RAM is
  short. A request in progress finishes first, and the next one reloads the model. `heavy free` unloads them too. Turn it
  off with `"free_ollama": false`.

## 1.10.0 (2026-11-06)

- **Agents use the lanes without being told.** `heavy hook` is a `PreToolUse` hook for the shell tools of a coding agent.
  A command a rule names goes through `heavy auto`, and everything around it stays as written. A foreground call gets
  `--no-wait`, so it can't sit in a queue until its timeout. It never approves anything, and if it fails the command runs
  as written. `heavy hook -- COMMAND` shows what it would do.
- **Starter rules now include `build.cmd` and `build-android.cmd`** on the CPU lane. A saved `rules.json` is not changed.
- `heavy auto -- ./gradlew build` now works on Windows: a `/` in the program's path no longer reaches cmd.exe.

## 1.9.0 (2026-11-06)

- **Nobody has to know what counts as heavy.** `heavy auto -n NAME -- COMMAND` picks the lane and threads from rules. The
  first match wins, a rule can apply to one folder, and a command no rule knows takes the CPU lane at the usual share.
  It starts with rules for Gradle, `dotnet publish` and `build`, tar, 7z, Blender and the 3D checks. A broken rules file
  falls back to those and never stops a job.
- **`heavy why -- COMMAND`** says where a command would go without running it.
- **The rules window** (`heavy rules`, or Rules in the tray menu, Windows only). Type a command and watch it take its lane,
  next to the live lanes. Drag to reorder, edit in place, flip the lane, pick threads, set the usual CPU share. Every change
  saves at once and can be undone, and all of it works from the keyboard. Elsewhere, `heavy rules --list` prints the rules.
- **Rules learned from past runs.** Three runs of 20 s or more that agree can suggest a command moves to the GPU lane,
  leaves it, or gets every thread. GPU readings cover the whole machine, so runs beside a GPU lane job don't count.

## 1.8.0 (2026-11-06)

- **`--threads N` gives one CPU lane job its own share of the processor.** `heavy cpu --threads 16 -n "Big publish" --
  ...` runs that job on 16 threads, and the next job goes back to `cpu_threads`. It stays at below-normal priority.
  `hold()` takes `threads=` too, so programs that hold a lane themselves can ask for it.
- `status` and `log` show how many threads each CPU job has.
- The flag is refused on the GPU lane, which has no limit. Inside a job already holding a lane, the parent's share stands
  and the job says so.

## 1.7.1 (2026-11-06)

- **Programs that hold a lane themselves are watched too.** The check moved into `hold()`, so test runners that call it
  directly get it with no changes. HeavyGate's own GPU readings don't count as activity.
- **A dead job can't block a queue.** After 20 minutes with no activity and another job waiting behind it, the job is
  ended and `heavy log` says so. With nobody waiting it is left alone. Inside a program, only the stuck child is ended,
  so the program carries on. `stall_stop_min` sets the time; 0 turns it off.

## 1.7.0 (2026-11-06)

- **A stuck job can't sit on a lane unnoticed.** A job that was alive but doing nothing used to hold its lane for ever
  while `status` said only "over its usual time". Now `heavy` watches the job's whole process tree. Five minutes with
  no real CPU and no process starting or ending, and the job shows as STALLED in `status`, `watch` and the strip, which
  turns red, with how long it has been idle and what is waiting behind it. The job's own output says so too, and `heavy
  log` records it. It flags and never stops a job: one waiting on a dialog or a slow download may be fine.
- **`heavy stop LANE`** ends the job holding a lane and everything it started, so the next one in line can go.
- **Estimates ignore failed runs and runs under a second.** A 6 s failure no longer makes a 10 minute build "usually 6s".

## 1.6.1 (2026-11-04)

- **The tray can't freeze any more.** A GPU reading through NVML could hit an access violation on a stale device handle.
  That exception skipped the line that schedules the next update, so first the graphs stopped, then the whole tray, and
  every click failed the same way, so even Exit was gone. Now a bad reading drops the handle and reconnects after 30 s,
  both update loops always reschedule themselves, and the menu opens even if its refresh fails.

## 1.6.0 (2026-11-04)

- **Linux.** The lanes, queue, RAM wait, estimates, `status`, `watch`, `log`, `free` and `deps` all run on Linux, on
  `flock` and `/proc`. The cpu lane runs at nice 10 on three quarters of the cores, and stopping `heavy` stops the whole
  job tree. State lives in `$XDG_STATE_HOME/HeavyGate`. The tray, strip and shutdown-when-done stay Windows only.
- **`cpu_threads` now defaults to three quarters of the machine** rather than a fixed 12, so it fits any PC. 16 threads
  still gives 12.
- **Hardware names:** `heavy status` and the tray menu show the CPU and GPU. Without `nvidia-smi` the GPU name comes
  from Windows or `lspci`.
- **`install.ps1` and `install.sh`** put `heavy` on the PATH.
- Library callers should pass `**slot.popen` to `Popen`; `slot.creationflags` still works on Windows.
- A MIT license, a rewritten README, and sample screenshots with generic job names.
- Python 3.9+ is the stated minimum (the code already needed it).

## 1.5.2 (2026-11-04)

- **Tray errors name their line.** When a tray message fails, `tray.log` now records the full traceback, not just
  the exception. Prompted by a tray that ran for 11 hours and then hit an access violation on every click, leaving
  its menu and Exit unreachable; a restart cleared it. The root cause is not yet found, so this is the diagnostic
  half of the fix.

## 1.5.1 (2026-11-03)

- **`heavy status --peek`** reads the lanes without taking a lock or removing dead queue tickets (it uses the same
  `peek()` the tray uses), so a monitor can poll it often without making a real job miss its turn. Plain `heavy status`
  is unchanged. A dashboard can poll `status --json --peek` every 10 s.

## 1.5.0 (2026-11-02)

- **When will it be done?** Every job now gets an estimate from its past runs: the median of the last five runs of
  the same job. Version numbers and run counters are left out of the name, so "Game 0.57.0 full suite" uses the
  earlier full suites and "nightly 6" uses the earlier nightly runs. If no name matches, it falls back to
  the same command in the same folder. Runs that failed within 5 s don't count.
  - The strip shows "~38m left" on each running job and an "All done around 23:40" line.
  - `heavy status` shows "~12m left (usually 1h10m)", "usually 3m" for queued jobs, and "All done in ~1h05m, around
    23:40". `--json` adds `usual_s`, `left_s`, `starts_in_s` and an `eta` block.
  - The tray menu shows the same per job, and the tooltip ends with "done ~23:40".
  - A job with no past runs says so, and the "all done" time counts only what is known.
- **Shut down the PC when all jobs are done.** Use "Shut down the PC when all jobs are done" in the tray menu, or
  `heavy shutdown-when-done`; `--off` cancels it.
  - Once both lanes have been empty for 3 minutes (in case a follow-up job queues), the strip counts down 60 s in red,
    then Windows shuts down.
  - Clicking the strip during the wait or the countdown cancels it, and a job arriving starts the wait over.
  - It uses `shutdown /s /t 0` without `/f`, so an app with unsaved work can still stop it and ask.
  - The switch turns itself off before shutting down, so it never carries over to the next start.
  - It only watches the lanes: work that doesn't run through HeavyGate isn't waited for.
  - `shutdown_idle_s` and `shutdown_countdown_s` in settings.json change the times.
- **Tray log.** The tray now writes `tray.log` in the state folder: when it starts, when it exits and why (Exit in the
  menu, closed by another program, Windows ending the session), and any error. The 1.4.1 tray stopped on its own
  sometime this evening and left no trace of why.
- Self-tests: 21. The new ones cover estimates matched across versions (a 2 s failure ignored), and the
  shutdown-when-done switch in status. The tray test runs the whole shutdown sequence in a throwaway folder with a
  stand-in command that only writes a file: wait, a job resetting it, countdown, cancel by click, then the command
  running and the switch going off.

## 1.4.1 (2026-11-02)

- **The live view opens only when you choose it in the tray.** Clicking the strip opens nothing now (when the strip
  was sent behind, a click still brings it back), and clicking the tray icon, left or right, opens the menu. The live
  view (`heavy watch`, a console window) opens only from the menu's "Open live view", and "Close live view" closes it.
  Before, a click on the strip or tray icon popped up the console window.

## 1.4.0 (2026-11-02)

- **Graphs on the strip.** Three small graphs under the jobs show CPU, GPU and RAM use over the last two minutes. Each
  has a see-through area, its current value, and the highest point marked with a dot and "peak N", so a spike stays
  visible after it passes. While the strip shows they read once a second: GetSystemTimes, NVML (`nvml.dll` from the
  NVIDIA driver, 0.2 ms, no nvidia-smi process) and GlobalMemoryStatusEx. Without NVML the GPU graph says n/a. "Show
  CPU, GPU and RAM graphs" in the menu turns them off.
- **It fades.** The strip fades in when a job starts and out when both lanes go idle. It rests at 88% opacity and
  turns solid while the mouse is over it; "Strip opacity" in the menu sets 60-100%. It stays solid when Windows has
  transparency effects off or a high-contrast theme, and it skips the fades when Windows animations are off.
- **Out of the way when you need the screen.**
  - ▾ in its corner sends it behind other windows; clicking it brings it back, and so does the next idle spell.
  - × hides it until both lanes are next idle.
  - "Keep strip on top" in the menu (saved) turns always-on-top off for good.
  - It steps behind on its own while a full-screen app (a game, a video) is in front on its screen.
- **The live view is no longer a stray Python window.** It is titled "HeavyGate 1.4.0 live view". Clicking the strip
  or the tray icon again closes it, and the menu says "Close live view" while it is open.
- Measured live: the strip showed for a real job at 88% opacity, on top, with no taskbar button, and was gone after the
  job. The tray process used 109 ms of CPU over 15 s while the strip was showing, and 54 MB of RAM (31 MB before,
  the rest is NVML).
- Self-tests: the tray test checks the fade in and out, solid on hover, send-behind, hide-until-idle, and that the
  graph lines are drawn (exact line colours, since ClearType tints text edges).

## 1.3.1 (2026-11-02)

- **Its own project,** with its own repository. The old path keeps a small stub so existing callers still work.
- Fixed: `heavytray.pyw --selftest` crashed printing its report to a cp1252 console whenever a real job was running
  (the strip's arrow character). The report is printed ASCII-escaped; `report.json` keeps the real text.
- Self-test: the status probe job runs 8 s instead of 3 s, so it is still running when the JSON status is read on a
  busy PC.

## 1.3.0 (2026-11-02)

- **Text strip.** The tray icon's colours alone didn't say what was happening, so `heavy tray` now also shows a small
  strip above the clock whenever anything runs, waits or is queued. It names the job on each lane and how long it
  has been there, the next jobs in line, what a job waiting for RAM needs against what is free, and the free RAM
  with its two biggest users. The border turns amber while a job waits for RAM and red when RAM is nearly gone. It
  hides when both lanes are idle.
  - It stays on top but never takes focus and has no taskbar button.
  - Drag it anywhere; the spot is kept in `tray.json` and dropped if that screen is gone.
  - Left-click opens the live view. Right-click opens the tray menu, which can switch the strip off or move it back
    above the clock.
  - Measured with the tray icon in the same process: 31 MB RAM, 0 ms CPU over 15 s idle, 16 ms over 15 s while
    shown.
- **Start at sign-in** (the user asked): `heavy tray --at-sign-in on|off`, or the "Start at sign-in" tick in the menu,
  adds or removes `HeavyGate tray.lnk` in the Startup folder (pythonw.exe "heavytray.pyw"). It is on for this
  account. Proven by launching the shortcut with no tray running: one tray started, and a second launch left one.
- Tray selftest renders the strip for a busy, short-on-RAM and idle example, saves what the window itself draws
  (PrintWindow on that window only), and fails on a blank capture. A window that is off-screen or fully transparent
  never paints, so the capture runs at 1% opacity.

## 1.2.0 (2026-11-02)

- **Tray icon** (`heavy tray`, `heavytray.pyw`): the left half is the GPU lane and the right half the CPU lane. Each
  half is grey when free, green while a job runs and amber while a job waits for RAM. A white dot means jobs are
  queued, and the background turns red when free RAM drops under the reserve plus 256 MB. Hovering gives a summary.
  Right-click lists every running, waiting and queued job, opens the live view, the run log or the dependencies,
  stops idle Gradle daemons, and holds Exit. Left-click opens the live view. One per sign-in, and it comes back
  after Explorer restarts. Measured: 19 MB RAM and no measurable CPU between polls (every 3 s). It does not start at
  sign-in on its own.
- **`peek()` / `peek_queue()`**: read a lane without opening its lock, for anything that polls. `holder()` takes the
  lock for a moment, which could make a real job miss its turn. A job now clears its record when it lets go, so a
  finished job inside a process that lives on (a test runner that embeds HeavyGate) no longer looks like it is still running.
- Self-tests: 18. The new ones cover peek during a job and after it, a dead holder's record, 3,000 lock grabs with
  no clash while peeks spin (the version that takes the lock fails this with 3 clashes), and the tray adding its
  icon, showing the job and removing the icon.

## 1.1.0 (2026-11-02)

- **Status shows what is running, waiting and queued.** Per lane: the job that is running, a job that has its lane
  but is waiting for RAM (with how much it needs and how much is free), and the jobs queued behind it, in order, each
  with how long it has been in that state. Before, a job waiting for RAM showed as "BUSY", which read as running.
- **`heavy watch`**: the same view, redrawn every 2 s (`-s` to change) until Ctrl+C.
- **`heavy status --json`**: the same as data, for other tools.
- **The log splits the wait**: "queued" is time behind other jobs, "for RAM" is time waiting for memory. Runs from
  1.0.0 kept one total and are marked with `*`.
- Self-tests: 14 (new: running and queued in status, a RAM wait in status and the log, the queue/RAM split, watch).

## 1.0.0 (2026-11-02)

- First version: gpu and cpu lanes, one job each across every session, FIFO queue, RAM admission, cpu jobs below
  normal priority on 12 threads, `status`, `log`, `free`, `deps`.
