# Changelog

## Unreleased

### New
- **The check can depend on what you change.** Add `checks = [{ paths = ["js/thermo.js", "css/**"], run = "npm run
  test:ui" }, ...]` to `praxis.toml`. For a landing Praxis runs the command of the FIRST entry whose `paths` match
  any file the landing changes, and your normal `check` when none does, so put the strictest entries first. Without
  `checks` nothing changes. The landing output and `praxis status` name the check that was used.
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
  `praxis.cmd` on Windows) that always runs the newest Praxis you have installed. Use that path in your agents'
  instructions. Run `praxis launcher` again after installing a new version. The launcher only picks which Praxis to
  run: an older Praxis still refuses a project a newer one has used, so versions never mix.
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
- **The launcher runs the install you ran it from.** When two installs had the same version, `praxis launcher` could
  write a launcher that ran the older install. Now, among equal versions, the install you ran `praxis launcher` from
  wins, then the one registered most recently. A lower version still always loses to a higher one.
- The project database is now format 3 (a new table for the heavy jobs). Praxis upgrades it by itself the first time
  it runs; an older Praxis then refuses the project instead of guessing.

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
