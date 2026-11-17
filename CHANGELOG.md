# Changelog

## Unreleased

### New
- **Your check can test only what a landing changes.** For every landing's check Praxis now sets `PRAXIS_BASE` (the
  commit of the main version your work was put on top of) and `PRAXIS_CHANGED_FILES` (the path of a text file
  listing the files the landing changes, one per line, from the top of the project). Your check command can read
  the list and run only the tests those files can affect. A batch's one check gets the files of all its agents, an
  early check gets them too, and the full check gets neither, because it tests everything. The list sits next to the
  check's log in `.git/praxis/logs/`. If your check runs in WSL on Windows, add
  `env = { WSLENV = "PRAXIS_CHANGED_FILES/p:PRAXIS_BASE" }` to `praxis.toml` so WSL sees them. Have such a check
  run everything when it meets a file it does not know, and keep `full_check` behind it.
- **Praxis's own quick check runs only the tests your change can affect.** `python tools/affected_tests.py --list
  <file of changed files>` (or the file names as arguments) prints what to hand to pytest: a few test files, nothing
  at all for a change no test can see (the README), or `tests` for the whole suite. It reads a map,
  `tools/affected_tests.json`, made by running the suite once while every test file's use of the project files is
  written down (`--rebuild`), child processes included. It runs the whole suite whenever it cannot be sure: a file
  the map does not know, no list, a damaged map, or a change to `conftest.py`, `pyproject.toml`, `store.py`,
  `cli.py`, the tool or the map. A changed test file always runs itself. If you add a source or test file, one test
  fails until the map knows it: `python tools/affected_tests.py --add` puts it in at once, the safe way (a new test
  file runs with every change, a new source file runs the whole suite), and `--rebuild` makes the map tight again.
  The whole suite stays the safety net: run it as the `full_check`.
- **The check can depend on what you change.** Add `checks = [{ paths = ["js/thermo.js", "css/**"], run = "npm run
  test:ui" }, ...]` to `praxis.toml`. For a landing Praxis runs the command of the FIRST entry whose `paths` match
  any file the landing changes, and your normal `check` when none does, so put the strictest entries first. Without
  `checks` nothing changes. The landing output and `praxis status` name the check that was used.
- **The next agent's check can start early.** With the new `speculate = true` in `praxis.toml` (off by default),
  while the agent at the front of the landing line runs its check, Praxis starts the check of the next waiting agent
  beside it, on "main + the front agent's work + its own work" in a scratch folder (`<project>-praxis/_early`). If
  the front agent lands, the next one finds its files already passed and lands without waiting for a check; if the
  early run is still going, its turn waits for it rather than running the same check twice. If the front agent does
  not land, Praxis stops the early run (only the process it started) and forgets it. Nothing ever lands from the
  scratch folder: a remembered pass in the check cache is the only thing handed over. One early check at a time, and
  only when a heavy job place is free. `praxis status` shows it, and it logs to `.git/praxis/logs/early-check-*.log`.
- **A full check that runs every so often, and finds the landing that broke it.** Quick checks are only safe with a
  net behind them. Set `full_check = "<the command that tests the whole project>"` in `praxis.toml`, and
  `full_check_every = N` to have Praxis start it in the background after every N landings that did not run that
  exact command themselves (`0`, the default: only when you ask). `praxis fullcheck` runs it now (`--background`
  returns at once). It runs on the main version in a scratch folder of its own (`<project>-praxis/_fullcheck`), never
  in your main folder and never holding up the landing line, takes a heavy job place, opens no window and logs to
  `.git/praxis/logs/full-check-*.log`. When it fails, Praxis searches the landings since the last pass the way
  `git bisect` does, one run per step, and `praxis status` shows under NEEDS YOU whose landing broke it, the commit,
  the log, and the exact `git revert` command. Praxis never undoes a landing by itself. If its process is cut short,
  the next landing notices and starts it again. This needs database format 5: Praxis upgrades your project's
  database the first time the new version opens it (one new table, `fullchecks`), and an older Praxis then refuses
  that project (update it, or use the launcher).
- **A check that already passed is not run twice.** If the exact tree Praxis is about to land already passed the same
  check command, you now see "Skipping the check: same tree already passed at <time>" and the landing goes on. A
  different command text never reuses a pass, a failure is never remembered, and the newest 200 passes are kept in
  the project database. This needs database format 4: Praxis upgrades your project's database the first time the
  new version opens it, and an older Praxis then refuses that project (update it, or use the launcher).
- **`heavy_jobs = "auto"`.** Praxis sizes the number of heavy jobs from the machine's load each time a job asks: more
  places while the CPU and memory are idle, fewer when it is busy, aiming for about 75 percent CPU, never below 1 and
  never above the new `heavy_max` (default: half the CPU cores, at least 2). A number still works as before.
  `praxis status` shows the current limit and why.
- **Waiting agents land together.** When two or more agents are waiting in the landing line, the agent at the front
  takes the others that are ready, puts their saved work on top of the newest version in line order and runs your
  check once for all of them, so nine agents waiting on a 30 minute check no longer wait about 5 hours. If the check
  passes, they all land, one commit each in order and each still marked by agent. If it fails, Praxis halves the
  batch and rechecks each half to find the work that broke it: the halves that pass land, the culprit is refused with
  the check's output, and the others carry on. Each `praxis land` still prints its own result and exits with its own
  code, and a refusal for a clash or for undoing another agent's work still names whose. With one agent waiting,
  nothing changes. If the front agent is stopped, nobody's work is lost and the next landing records what landed and
  puts its folder back. `praxis status` shows who is in a batch. The new `batch_max` setting in `praxis.toml`
  (default `4`) caps the batch; `1` turns batching off.
- **A heavy job can start another heavy job without freezing.** If the command you run with `praxis run --heavy`
  itself calls `praxis run --heavy`, the inner call used to hold a place while waiting for another, so two such jobs
  at the limit waited for each other forever. Now every heavy job's environment carries `PRAXIS_HEAVY_TOKEN`, and an
  inner call that sees it says "Already inside a heavy job" and runs its command directly, with the same log and exit
  code, taking no place of its own. `praxis status` still shows only the outer job. A leftover token from a job that
  has ended, or from another project, is ignored and you queue normally.
- **A queue for heavy jobs.** Run a whole test suite or a browser run with `praxis run --heavy <command...>` and it
  starts only when fewer than `heavy_jobs` (a new `praxis.toml` setting, default 2) heavy jobs are running on the
  project; otherwise it tells you who is in front of it and waits its turn. You get the job's own exit code, no
  window opens, and its output goes to a log under `.git/praxis/logs`. If a job's runner crashes, Praxis notices and
  frees its place. `praxis status` now lists the running and waiting heavy jobs with the agent and how long ago each
  started or joined. The agent rules gain one line: whole-suite tests and browser runs go through
  `praxis run --heavy`.
- **One stable launcher for every version.** Each Praxis version installs into its own folder, so a path you gave your
  agents stopped working after an update. Run `praxis launcher` once and you get `~/.praxis/bin/praxis` (and
  `praxis.cmd` on Windows), one fixed path that runs an installed Praxis. Use that path in your agents'
  instructions. The launcher only picks which Praxis to run: an older Praxis still refuses a project a newer one
  has used, so versions never mix.
- **The launcher never switches by itself.** After you install a new version, `praxis launcher` run with it only
  registers that install. The launcher keeps running the version it ran before, and the command tells you which
  version the launcher runs now, which is the newest, and the exact command that switches. Run
  `praxis launcher --switch` when you choose to: it points the launcher at the newest installed Praxis. The first
  `praxis launcher` on a machine still points the launcher at the Praxis you ran it with, and when the version the
  launcher runs is uninstalled, it runs the newest one that is left. Before, running `praxis launcher` with a new
  version repointed the launcher at once, in the middle of whatever your agents were doing.
- **A newer Praxis never takes a project over while older commands are still running in it.** Every Praxis command
  now keeps a small locked file in `.git/praxis/procs/` while it runs (its process number, the database format it
  knows, its version and the command); the operating system frees the lock when the process ends, so a crashed
  command is never counted. Before a newer Praxis upgrades your project's database it looks there, and (for a
  Praxis from before this change, which does not register) at the live places in the landing line and the live
  heavy jobs. While any older command is running, the database is left as it is: `praxis status` still shows the
  project and names the older commands, commands that only use what the older database has (`claim`, `start`,
  `done` and so on) work as usual, and a command that needs the new format (`praxis land`) changes nothing, exits
  with code 1 and says "Older Praxis commands are still running in this project (...). This newer Praxis will take
  over when they finish. Nothing was changed." Add `--upgrade-now` to any command to take over at once. Before, a
  routine `praxis status` through the launcher could upgrade the database under landings and heavy jobs that an
  older Praxis had been running for hours. The database format does not change for this.
- **A batch never takes a landing of an older Praxis.** Each `praxis land` now writes into its place in the landing
  line which batch messages it understands. The agent at the front only takes agents behind it whose `praxis land`
  understands the batch; a landing started by an older Praxis says nothing there, is left out, and lands on its own
  turn as it always did.
- **`after_done`** in `praxis.toml`: a command run in the main folder after `praxis done` finishes an agent (also after
  `praxis land --done`). Praxis sets `PRAXIS_AGENT` and `PRAXIS_TASK` for it. If it fails you get a warning, and the
  agent stays done.
- **A warning when your agents would commit as the wrong person.** If the git name and email an agent folder would
  commit with match none of the authors of the main branch's latest commits, `praxis start` and `praxis status` say so,
  naming both, and print the two `git config` lines that set the project's identity.

### Changed
- **On Windows, `bash` in your commands means Git Bash, not WSL.** A check, `setup`, `after_land`, `after_done` or
  `generated` command, or a `praxis run --heavy` command, that started with a bare `bash` or `sh` ran
  `C:\Windows\System32\bash.exe`. That is WSL (Linux inside Windows), which cannot see Windows paths: your script
  "was not found", the command ended with exit code 127, and landings were refused through no fault of the work.
  Praxis now starts Git for Windows' bash in its place when it finds one (next to the `git` on your PATH, or in
  `Program Files\Git`) and prints one line naming both programs. If you have no Git for Windows bash, the command
  runs as before and you get a warning that WSL's bash will run and cannot see Windows paths. A bash you name by its
  path, or one of your own that Windows would find first, is left alone, and nothing changes on macOS or Linux. To
  keep every command exactly as you wrote it, set the new `shell_fix = false` in `praxis.toml`.
- **Every command log names the program that ran.** The log of a check, `setup`, `after_land`, `after_done`,
  `generated` rebuild or heavy job now starts with the command as you wrote it and a `Program:` line with the full
  path of the file that was started (and the shell that started it), so you can see which `bash`, `python` or `npm`
  it really was.
- **Among installs of the same version, the one you switch from wins.** When two installs had the same version,
  `praxis launcher` could write a launcher that ran the older install. Now, when `praxis launcher --switch` picks
  the newest, the install you ran it from wins among equal versions, then the one registered most recently. A lower
  version still always loses to a higher one.
- The project database is now format 3 (a new table for the heavy jobs). Praxis upgrades it by itself the first time
  it runs; an older Praxis then refuses the project instead of guessing.
- **Praxis's own tests run across CPU cores.** `pip install ".[test]"` now also installs pytest-xdist, and
  `python -m pytest -n auto` runs the suite with a worker for three quarters of the CPU cores (12 on a PC with 16)
  instead of one test at a time. On a busy 16-core PC the suite, 33 minutes one test at a time on Windows, took
  about 4 minutes on Linux (WSL) with 12 workers; 8 workers took over 6 minutes, and 16 or 24 were no faster than
  12. Plain `python -m pytest` still
  works. To make that safe every test now stands alone: a praxis command runs only inside the test's own temporary
  folder (child processes included), `PRAXIS_HOME` and git's global settings are the test's own, and settings left
  in the environment by whoever started the tests (`PRAXIS_AGENT`, `GIT_DIR`) are cleared. The tests no longer sign
  their commits with your key or read your `~/.praxis`.
- **`heavy_jobs = "auto"` no longer counts its own jobs.** In the previous version, auto counted its own jobs. It
  sized the limit from the machine's CPU reading, and that reading included the heavy jobs Praxis had already
  started. One job that kept the CPU busy read as "the machine is busy", so the limit stayed at 1 however many jobs
  waited. In real use that was 1 running and 27 waiting on a machine with 16 threads, where two at once had been
  fine. Praxis now measures the CPU each running heavy job uses (its process and everything that process started)
  and takes it out of the reading. The limit is sized from what other programs use and what one job costs.
  `praxis status` says what was counted, for example "other programs 22%, 2 heavy jobs using 48%". The new
  `heavy_min` setting is the fewest places auto gives: 2 by default on a machine with 8 or more cores, 1 on a
  smaller one, so a job that fills every core it gets no longer holds the whole line at one place. Set
  `heavy_min = 1` if you want the limit to fall to 1 as before. Nearly full memory still means 1.
- **A heavy job that is already waiting follows `praxis.toml`.** Each `praxis run --heavy` read `heavy_jobs` once,
  when it started, so raising the limit did nothing for the jobs already in line. A waiting job now reads
  `heavy_jobs`, `heavy_max` and `heavy_min` again from the main folder's `praxis.toml` whenever that file changes,
  and tells you. If the file has a mistake in it, the job tells you that and keeps the limit it had.
- **A landing's check goes first in the heavy job line.** A landing's check, which a whole batch of agents can be
  waiting on, used to join at the back, behind short test jobs (place 14 in real use). Praxis now sets
  `PRAXIS_CHECK=1` for the check command, for a batch's check too. A `praxis run --heavy` started
  from inside it goes ahead of the waiting jobs that do not come from a check, behind other check jobs in the order
  they joined. It never stops a job that is already running. `praxis status` lists these jobs first and marks them
  "landing check". The full check and an early check get `PRAXIS_CHECK=1` as well; they hold a heavy job place of
  their own already, so a `praxis run --heavy` inside them runs straight away, as before.
- **One agent cannot fill the heavy job line.** With the new `heavy_waiting_per_agent` setting (default `3`, `0` for
  no cap), when an agent already has that many jobs waiting, one more `praxis run --heavy` from it is refused with
  exit code 1 and a message listing its waiting jobs. Nothing is run. A landing check's jobs are never refused. The
  order is fairer too: it was first come first served, and now an agent with nothing running goes before an agent
  that already has a job running, in turns by agent, with equal turns going by arrival.
- The project database is now format 6 (one new column, `priority`, on the heavy jobs table, for the landing check's
  jobs). Praxis upgrades it the first time the new version opens your project; an older Praxis then refuses that
  project (update it, or use the launcher).

## 0.4.0

Praxis is the coordination layer for AI agents: it runs no model, and lets several agents (Claude Code, Codex, Cursor
and others) work on one project at once without breaking each other's work.

### New
- **Code files can be shared.** Two agents may edit the same code file at once. Praxis checks their edits against each
  other when they land and refuses only a real clash, naming the files and whose landing they clash with. Asset files
  (images, sounds, models...) stay one agent at a time. `exclusive_code = true` brings back one agent per code file.
- **A landing can never quietly undo another agent's landed work.**
- **Generated files**: list a file, the command that rebuilds it and the files it is built from; Praxis rebuilds it
  after each landing that touched them, and agents never claim it.
- **No git yet? No problem.** `praxis init` sets git up for a folder that is not a git project.
- **Dependencies in agent folders**: `share = ["node_modules"]` links the main folder's copy into every agent folder;
  `setup` runs a command in each new agent folder (`praxis setup <agent>` runs it again).
- **`env`** settings for the check, setup and after-landing commands.
- **Background landings**: `praxis land <agent> --background`, then `praxis wait <agent>`, for tools that end long
  commands.
- **`after_land`**: a command run after every landing (push, notify...); its failure never undoes the landing.
- A check can exit 3 to say "a problem outside this agent's work": it never counts toward the enough pause.
- `branch_prefix` and `commit_trailers` settings. Praxis no longer silently takes over a branch it did not make.
- A heartbeat while the check runs, the place in the landing line in `status`, and an honest "running something now"
  for agents busy with long tests. `release-stale` refuses to free a busy agent's files.

### Faster
- `status` asks git about 6 times less (10 agents: 202 git calls down to 33).
- `start`, `done` and `abandon` are 2 to 4 times faster on big projects; ten landings at once finish in less than half
  the time.

### Safer
- Many fixes from real use and from reviews: folder removal never follows junctions or links, refuses while a program
  or file in the folder is in use, and handles half-removed folders; a retried rebase never leaves the folder detached;
  Windows reserved names and spellings; config typos get a "did you mean"; the project database has a format number, so
  an older Praxis refuses instead of guessing.
- The agent rules now say: stop only programs you started, by their process number, never by name.
