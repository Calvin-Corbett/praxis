# Changelog

## Unreleased

### New
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
