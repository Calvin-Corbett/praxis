# Changelog

## 0.5.0

This release adds batch landings, reusable check results and a shared limit for heavy jobs.
The landing queue can check changes in its own worktree, so an agent can keep editing while its saved work is tested.

### New
- **The landing line can check in a folder of its own, so you can keep working while your landing is checked.**
  Until now a landing's check ran in the landing agent's folder (a batch's in the folder of the agent at the front).
  One edit there during the check refused the landing, and when the check reads the whole project every other
  agent in the batch paid for it again. Set `check_folder = "line"` in `praxis.toml` and the check runs in
  `<project>-praxis/_line` instead: one git worktree on no branch for the whole project, kept between landings,
  made to hold exactly the files being landed before each check (what git ignores stays, so your tools' caches
  stay warm in one place). A batch is put together there and each smaller group of a failed batch is checked
  there. An edit in any agent's folder can no longer change what was tested, so the verdict stands. A landing
  lands your work as it was saved when the landing started: commits you save and files you change in your folder
  after that are named when the landing ends, stay on your branch, and land with your next `praxis land`. The
  front agent's folder never leaves its branch, so a batch stopped part-way leaves nothing there to put right, and
  what a killed check left in the line's folder is cleared before the next check.
  The default stays `check_folder = "agent"`, which is exactly the old behaviour, because the line's folder holds
  only the committed files, the `share` folders and what `setup` makes there: a check that needs packages or build
  output an agent put into its own folder by hand fails in it. `praxis check <agent>` runs your check in a folder
  made the same way, so it tells you beforehand. The README ("Checking in the line's own folder") lists every way
  the two can differ. No change to the database.
- **`praxis update`: one command to move to a new Praxis version while agents work.** After installing a new
  version, run `praxis update` with it. It registers the version with the launcher, looks at every project Praxis
  is used in, tells you for each one what the new version will do to its database, and points the launcher at the
  new version when that is safe everywhere: when it only adds to the databases (the usual case, and then the older
  commands carry on beside it), or when no command of an older Praxis is running where it must change more. If
  commands of an older Praxis have to finish first, it names them, changes nothing and exits with code 1;
  `praxis update --when-safe` waits for them in the foreground and switches the moment they have finished. Praxis
  now leaves one small file with the project's path in `~/.praxis/projects/` when a command runs in a project, so
  that `praxis update` knows where to look. `praxis launcher` and `praxis launcher --switch` work as before.
- **A test that proves an update under load.** `tests/test_mixed_versions.py` installs the last released Praxis
  (built from this project's git history) and the new code side by side, puts the older one's landings and heavy
  jobs in flight in a real project (foreground and background, running and waiting), switches to the new code with
  `praxis update` in the middle, lets both versions start more work, and passes only if every heavy job ends with
  its own exit code, every landing of both versions lands, no more heavy jobs ever ran at once than `heavy_jobs`
  allows, and no command printed a refusal. It takes minutes: `pytest -m "not mixed"` leaves it out, and it skips
  itself in a copy of the project without git history. If you change Praxis, the top of that file says how to move
  its baseline at each release.
- **Your check is told where the project's main folder is: `PRAXIS_ROOT`.** Every check Praxis runs (a landing's,
  in the agent's folder or in the line's own folder; a batch's and each step that looks for the culprit; an early
  check; `praxis check`; the full check and its search for the landing it starts to fail at; an audit) now gets
  `PRAXIS_ROOT`, the full path of the main folder Praxis itself found, never the scratch folder or the agent's
  folder the check runs in. A check that wants to run trusted code from the main version can use it instead of
  working the folder out with git from inside the agent's folder, which the agent can steer (`GIT_DIR`, a rewritten
  `.git` file). Praxis sets it after it has copied the environment it was started with, so a `PRAXIS_ROOT` that was
  already there, or one in your `env`, is replaced and never trusted. `setup`, `after_land`, `after_done`, a
  `generated` rebuild and `praxis run --heavy` are told it too. It is not part of what a check remembers: moving the
  project folder keeps every remembered pass, unless a check lists `PRAXIS_ROOT` in its `env_inputs`. There is no
  new setting, and a check that never reads it behaves as before. If your check runs in WSL on Windows, add
  `PRAXIS_ROOT/p` to `WSLENV`. `PRAXIS_AGENT`, `PRAXIS_TASK`, `PRAXIS_CHECK`, `PRAXIS_BASE` and
  `PRAXIS_CHANGED_FILES` were already set after the copy; a new test says so for each of them.
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
- **A passed check is remembered for the files that check reads.** Until now a pass was remembered for the whole
  project tree, so with many agents landing, every landing changed the tree and paid for the whole check again. Now
  a `checks` entry in `praxis.toml` may have a `name`, `inputs = [patterns]` (the files its command reads) and
  `env_inputs = [names]` (the environment variables it reads). Give every entry a name and the rule changes: EVERY
  entry whose `paths` match a file your landing changes runs (not only the first), every entry that lists a changed
  file among its `inputs` runs too, and your default `check` runs as well when no entry's `paths` match. A named
  check's pass is remembered for exactly the files it reads (git's content ids of the files matching `inputs`, the
  command text and those variables), so while they stay the same you see "Skipping check ui: it passed at <time> on
  exactly the files it reads", whatever else landed. A failure is never remembered. Several checks of one landing
  run one after another, and at the same time while heavy job places are free (`checks_together = false` turns that
  off). Without any `name`, nothing changes: first match only, and the check cache as before.
  **What a landing guarantees changes when you declare `inputs`:** from "the exact files that land passed the
  check" to "every check that reads a file in the landing has passed on exactly those file contents", where "reads"
  is what you wrote. The audit below and the periodic full check are the net, so keep both on.
- **An audit that shows you an incomplete `inputs` list.** With `audit_every = N` (default 20, `0` = off), the first
  pass a check takes from memory and then about one in N is run again anyway, on exactly the version that landed,
  in a scratch folder (`<project>-praxis/_audit`), in the background, never holding up the landing. If it fails,
  `praxis status` shows under NEEDS YOU "check <name> passed from memory but fails when run: its inputs list is
  probably missing files" with the log, drops that check's remembered passes, and runs it every time until you have
  fixed its `inputs` and run the new `praxis trust <name>`. Praxis never undoes the landing.
- **`praxis check <agent>`: earn your pass before the landing line.** It builds "the newest main version + the
  agent's saved work" the way a landing would, in a scratch folder (`<project>-praxis/_check-1`...), runs the checks
  that landing would run, remembers the passes, and prints which checks passed, were reused or failed (exit code 0
  or 2). It never moves main, never changes the agent's branch or folder, is not a landing try, and holds one heavy
  job place. A later `praxis land` finds the passes while the files each check reads are unchanged, and lands in
  seconds. The agent rules gain one line (run `praxis init` again to refresh `AGENTS.md` and `CLAUDE.md`): when your
  work is saved, run `praxis check <you>` before `praxis land`.
- **Batches use the same memory.** A batch whose named checks all have a remembered pass for the files they read
  lands without running anything, and when a batch fails, the smaller groups run again only the checks whose files
  differ from what already passed, usually just the one that failed.
  All of this needs database format 7: Praxis upgrades your project's database the first time the new version opens
  it (two new tables, `checkpasses` and `checktrust`), and an older Praxis then refuses that project (update it, or
  use the launcher).
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

### Fixed
- Concurrent test checks now publish each result in its own file. On Windows, two checks appending to the same
  test log could overwrite part of a result and cause a spurious JSON decoding failure.
- **A failed full check is confirmed before anyone is named.** Until now one failed run was taken as proof. On a PC
  too busy for a test suite's time limits the full check failed on the newest version, the search then failed on an
  older version for the same reason, passed on the one before it once the PC was quiet, and `praxis status` named
  the landing in between as the one that broke it, with a `git revert` command. Nothing was broken. Now a pass
  counts on one run and a failure only when the same commit fails a second time. If the second run passes, Praxis
  records that it failed once and then passed, names nobody and prints no undo command: `praxis fullcheck` ends with
  exit code 0 and `praxis status` says it under FULL CHECK with both outputs (a notice under NEEDS YOU comes only
  when the same files gave two answers in 2 of the last 3 runs). The search goes by the same rule, and a landing is
  named only when the full check failed twice on the version right after it, passed on the version right before
  it, and nothing in the search passed after that last failure; if something did, that version runs once more, and
  when the failure does not come back nobody is named and status says so. Status now says what the runs showed and
  no more: "It starts to fail at <agent>'s landing" with how many times it failed there, every failed run's output,
  and which pass the version before it rests on, where it used to say "<agent>'s landing broke it". A failure costs
  about twice the runs it did (the README, "The full check", has the numbers), and every run now takes a heavy job
  place of its own and gives it back when it ends, so other heavy jobs get their turn between two runs where the
  whole search used to hold one place. `full_check_confirm = false` in `praxis.toml` turns the confirming off: every
  failed run counts at once, as before. No change to the database: what each run showed is kept in the `steps`
  column the full check's rows already had, and rows written by an older Praxis read as one failed run each.
- **A landing that was stopped no longer drops work you save afterwards.** Before this, work could be dropped. When
  the `praxis land` of the agent at the front of a batch was killed during the check (an AI tool's time limit does
  that), its folder was left on no branch, holding the batch's files. If the agent then saved another commit there,
  its next `praxis land` said "Put it back on praxis/<agent>; your own saved work was not changed", landed, exited
  0, and that commit was on no branch and its files were gone from the folder: only git's reflog still had it. Now,
  before Praxis puts a folder back on its branch, it looks for commits made in it that are on neither the agent's
  branch nor main and are not the batch's own. It puts copies of them on the branch, in order, tells you which, and
  only then switches the folder; unsaved edits and new files stay in the folder as they are. When the commits do not
  fit on the branch, or git would have to overwrite an unsaved edit, Praxis changes nothing, keeps the commits under
  a name of their own (`refs/praxis/saved/<agent>-<time>`), exits 1 and prints the commands that bring the work
  back. The same rule holds wherever a folder is put back: a commit made in the front agent's folder while a batch's
  check runs used to be dropped the same way, and is now carried onto its branch; and levelling a folder after a
  batch no longer writes over a new file in it that git did not know yet.
- **Work a batch landed is not landed a second time.** When an agent's waiting `praxis land` ended while the batch
  that held its work was being checked, the batch still landed the work, but `praxis status` went on saying "N saved
  change(s) not landed yet", the agent's next `praxis land` took a turn in the line and recorded the same work as a
  second landing, and the note written for the ended process was never removed. Now every landing a batch records
  says which saved work it took. `praxis status` shows that work as landed, and `praxis land` says "already landed
  at <time> (in a batch)", levels the folder and exits 0 without taking a turn or recording anything. Places in the
  line and notes left by landings whose process has ended are cleared by the next landing.
- **`praxis status` tells you when a stopped landing left something to put right.** Under NEEDS YOU it lists an
  agent whose landing was stopped part-way, or whose folder is not on its own branch, with the one command that puts
  it right: `praxis land <agent>`.
- **A waiting agent is always told it was taken into a batch.** With a quick check the whole batch could be over
  between two looks at the line, and the waiting `praxis land` printed its result without the "Your work was taken
  into a batch: ..." line. It now always prints that line, with the names, before the result.
- **A second `praxis land` for an agent whose landing is still running says so.** While a landing runs a batch, its
  agent's folder is on no branch on purpose; a second `praxis land` used to answer with advice to switch the folder
  back by hand. It now says the agent is already landing, and changes nothing.
- **One agent's edit during a batch's check costs the others less.** A batch's check runs in the folder of the agent
  at the front. One edit there while it ran refused that agent and threw the passed check away for everybody else in
  the batch. Praxis still never lands files a check may not have tested, so a check that reads the whole project runs
  again for the others. But a named check that lists the files it reads (`inputs`) now keeps its pass when none of
  the files that changed is one it reads: the others land on their own turn with "Skipping check <name>: it passed
  ...", without waiting for it again. The same holds for a single landing that is refused for files changed during
  its checks: the checks that cannot have read them are not run again.
- **An agent whose work a batch already landed is no longer told it did not land.** When a batch was split to find
  the work that broke its check, and a file changed in the front agent's folder during a later check of other
  agents' work, the front agent got "NOT landed: files changed while the check ran", exit code 2 and a failed try
  on its record, although its own work had landed minutes before. Its result now stands, and it is told that the
  later check does not count for the agents it was checking.
- **On a busy Windows PC, `praxis status` no longer waits many seconds for the process list.** To show "N processes
  working in its folder" for each agent, Praxis reads the command line of every process on the machine, one after
  another. That needs about a tenth of a second of CPU for every thousand processes, but with every core busy the
  read took 2 to 25 seconds: at ordinary priority the reading thread kept waiting for a CPU between its calls. Now
  that thread goes in front of ordinary programs for the length of the read and is put back right after. Measured
  on a 16-core PC at 100% CPU, the old and the new read turn by turn: with 1,200 to 1,500 processes running, 1.8 to
  16.7 seconds before and 0.16 to 0.21 seconds now; with 1,300 to 3,000 processes, 6.6 to 24.9 seconds before and
  0.17 to 0.37 seconds now. The read makes the same calls in the same order and gives the same command lines.
  Because it is over in a moment, it also finds short programs that used to end while a slow read was still on its
  way to them. `praxis release-stale` uses the same read, and the two looks `heavy_jobs = "auto"` takes at each
  running heavy job's processes go in front the same way. No setting of your PC is changed and nothing is
  installed. A Praxis that was itself started at a lowered priority ("below normal", as Windows' task scheduler
  starts its tasks, or "idle") takes a bigger step for the read, because two steps up from there is still behind
  ordinary programs; everything else such a Praxis does stays as slow as the priority it was given.

### Changed
- **You can update Praxis while agents are landing and running heavy jobs.** Until now a newer Praxis either
  refused `praxis land` until every command of the older one had finished (even a heavy job that was only waiting
  in line), or, with `--upgrade-now`, took the project over and broke the older landings. Now a project's database
  carries two numbers: its format, and the oldest format whose Praxis can still use it ("usable from"). A step that
  only adds a table or a column is taken at once, whoever is running, and leaves "usable from" alone. So the newer
  Praxis works from its first command, and the older one keeps working beside it: what it is running, and what it
  starts afterwards. Only a step an older Praxis cannot live with waits for its commands to finish (that is where
  `--upgrade-now` still applies), and no step since format 5 has been one. `praxis status` names the older commands
  that are at work and says that they carry on. One thing still stops an older Praxis: a setting in `praxis.toml`
  that it does not know, so add new settings only when the older Praxis is out of use. See "Updating Praxis while
  agents work" in the README.
- **The project database is now format 8, and older versions can still use it.** The step adds one small table
  (`meta`) that holds the format. The number an already released Praxis reads (SQLite's `user_version`) now means
  "usable from" and stays at 6 for a new project, or at whatever your project has, so the Praxis you have installed
  today keeps working after a newer one has opened your project. A Praxis older than your project's "usable from"
  still refuses it ("This project was used by a newer Praxis"): that is built into the released versions and
  cannot be changed in them.
- **On Windows, switching the launcher no longer breaks a command that is running through it.** `cmd.exe` does
  not keep a batch file in memory: when a command in it ends, it opens the file again and reads on from the byte it
  had reached. So when the launcher (`praxis.cmd`) was rewritten while a Praxis command was running through it, for
  example a landing's check in the middle of `praxis run --heavy` at the moment you switched versions, that command
  ended with "'-m' is not recognized as an internal or external command" and exit code 9009, and the landing failed
  with "the check failed". The launcher now runs Praxis and passes its exit code on in one line, so `cmd.exe` never
  reads the file again once Praxis runs. And when Praxis rewrites a launcher an older version made, it puts the
  line those running commands will read on at exactly where they will look for it, so they too end with their own
  exit code. The mixed-version test found this.
- **Heavy jobs of two Praxis versions in one line start in the order `praxis status` shows.** A Praxis before
  format 6 starts its waiting jobs strictly in the order they joined, and a newer one put a landing check's job
  (and an agent with nothing running) in front. With both in one line, the newer job did not go in front: the two
  only raced for the next free place, the message "it goes ahead of the waiting jobs" was not true, and status
  listed an order the jobs did not start in. Now a job of this Praxis never starts before an older job that joined
  first, status lists the line in the order the jobs really start (an older job is marked, and keeps the place it
  joined at), and a landing check's job that waits behind an older job says how many of them start first.
- **No Praxis version takes a landing of another version into its batch.** A newer landing at the head of the line
  already left older landings alone. Now an older head leaves newer landings alone too: before, its one check for
  the batch could fail for a reason of its own version, and every newer landing in the batch was told its work
  failed. Each version's landings are checked by that version, on their own turn.
- **A background landing no longer makes the commands under it run its own Praxis version.** `praxis land
  --background` (and the full check, an early check and an audit, which Praxis also starts in the background) put
  the folder of its own install into `PYTHONPATH`. Your check inherited that, so every `praxis` the check started
  ran the lander's version, even through the launcher after you had switched it to a newer one. After an update
  those calls were refused ("this Praxis knows format 4") and the landing failed with "the check failed". Praxis now
  starts its background processes with its own Python by full path and leaves the environment exactly as your
  command had it: a `praxis` your check starts is the one your PATH or the launcher names at that moment.
- **`--upgrade-now` refuses while a landing of an older Praxis is still running.** Taking a project over under an
  older `praxis land` made that landing's check fail, and every agent in its batch was told its work failed (no
  work was lost, but all of them had to land again). Now `--upgrade-now` exits with code 1, changes nothing, names
  those landings and tells you to wait for them, or to stop them and land them again with the newer Praxis
  afterwards. It still takes over when all that is left of the older Praxis are heavy jobs: those carry on and end
  with their own exit code. To take over regardless, add the new `--even-with-older-landings`.
- **`praxis status` keeps naming the commands of an older Praxis after the takeover.** Once the database was
  brought up to date, the line about older commands disappeared although they were still running. Now status names
  them, and says what they can and cannot do, until the last one has ended.
- **The file a killed Praxis command leaves behind is removed as soon as it is seen.** A killed command cannot
  remove its file in `.git/praxis/procs/`, and Praxis used to clear such files only when a newer Praxis was
  deciding whether to take over, or once more than 200 had piled up. Now `praxis status` removes them every time.
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
  12. Most of that time the tests wait for the disk. If you run them in WSL, give them a temporary folder in memory
  (`TMPDIR=/dev/shm/praxis-tests python -m pytest -n auto`, after making that folder): the same suite then took
  between 34 and 75 seconds, depending on what else the machine was doing. Plain `python -m pytest` still
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
