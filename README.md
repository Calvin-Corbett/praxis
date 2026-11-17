# Praxis

**Let several AI agents build your game at the same time, without breaking each other's work.**

You tell Claude Code, Codex, Cursor or any other AI coding agent what to build. That works well with one
agent. With two or three at once, things go wrong: one agent overwrites another's change, a level file gets
edited by two agents and ends up broken, and something that worked yesterday stops working.

Praxis is the coordination layer for AI agents. It runs no AI model of its own, and it is not a harness: it has
no tools, skills or prompts. It sits above the agents you already use (Claude Code, Codex, Cursor...) and keeps
them from breaking each other's work. It does five things:

1. **Each agent gets its own copy of the project to work in**, so they never type over each other.
2. **Each agent says which files it will change ("claims" them) before it starts**, so everyone can see who
   is working on what. Code (text) files can be shared: two agents may edit the same file at once, and Praxis
   tells each one who else is working on it.
3. **Files that cannot be combined** (scenes, levels, prefabs, 3D models, textures, sounds) are **locked**:
   only one agent at a time may hold one. Praxis says no to a second agent and names who has it.
4. **Finished work goes through a line, one agent at a time.** Praxis puts the work on top of the newest
   version, runs your project's check (a build or test command), and only keeps the work if the check passes.
   If two agents changed the same lines of a shared file, the second to land is told which files clash and
   whose landing they clash with, and fixes it. If anything fails, the agent gets the error text and its work
   is kept.
5. **When an agent keeps failing the same way, Praxis pauses it** and tells you, in plain words, what it needs.

You never have to read code to use it. `praxis status` tells you what is going on.

You need: **git** (Praxis builds on it) and **Python 3.10 or newer**. It works on Windows, macOS and Linux.

---

## Install

The easiest way is `pipx`, which installs command-line tools in their own little space:

```
pipx install git+https://github.com/Calvin-Corbett/praxis.git
```

(If you have the Praxis folder on your computer, you can install from it directly instead:
`pipx install C:\path\to\praxis` or `pip install C:\path\to\praxis`.)

No pipx? Plain pip works too:

```
python -m pip install git+https://github.com/Calvin-Corbett/praxis.git
```

Check it worked:

```
praxis --version
```

If the terminal says `praxis` is not found, the install worked but its folder is not on your PATH yet. With
pipx, run `pipx ensurepath`, then close the terminal and open a new one. With pip, the warning it printed names
the folder to add. Until then, `python -m agent_praxis` works everywhere `praxis` does
(for example `python -m agent_praxis status`).

The package is called `agent-praxis`; the command is `praxis`.

**One path that survives updates.** Each Praxis version installs into its own folder, so the path of `praxis` changes
when you update. Run `praxis launcher` once: it writes `~/.praxis/bin/praxis` (and `praxis.cmd` on Windows; set
`PRAXIS_HOME` to use another folder than `~/.praxis`). That launcher runs the Praxis you made it with, so you can put
its full path in your agents' instructions and leave it.

**The launcher never switches by itself.** After you install a new version, run `praxis update` once with it:
it registers the new install, looks at your projects, and points the launcher at the new version when no command
at work would be refused (see "Updating Praxis while agents work"; agents in the middle of a landing or a heavy job
carry on). The two steps also exist on their own. `praxis launcher` only registers the install: the launcher keeps
running the version it ran before, and the command tells you which version the launcher runs now and which is the
newest. `praxis launcher --switch` switches without looking at any project. Either way the launcher is pointed
at the newest installed Praxis; among installs of the same version, the one you ran the command from wins. If the
version the launcher runs is uninstalled, the launcher runs the newest one that is left. The launcher only chooses
which Praxis runs; see "Updating Praxis while agents work" for what happens in a project while two versions are at
work in it.

---

## Five-minute walkthrough with two agents

Praxis builds on git. If your game folder does not use git yet, `praxis init` sets it up for you (see
[Your game is not in git yet](#your-game-is-not-in-git-yet)). If it already uses git, commit everything you want
agents to see before you start: each agent's folder is made from what is committed, so a file you never
committed is not in it. `praxis init` warns you if it finds any. A `.gitignore` that leaves out the engine's
caches (Unity `Library/`, Godot `.godot/`, `node_modules/`...) keeps those out of the main version too.

**1. Set the project up (once).** Open a terminal in your game folder and run:

```
praxis init
```

Praxis looks at the folder, recognises Unity, Unreal, Godot or a web game, and writes:

- `praxis.toml`: a short settings file. The important line is `check = "..."`: the command that proves the
  game still works. Praxis guesses one when it can. If it leaves it empty, ask an agent:
  *"Set the check line in praxis.toml to a command that builds or tests this project."*
- A section in `AGENTS.md` and `CLAUDE.md` that tells every agent the rules. Anything already in those
  files is kept, line endings included. Praxis never commits your own unfinished notes: if one of those
  files has changes you have not committed, it stops and asks you to commit them first. If one is yours and
  was never committed (Claude Code's `/init` leaves one like that), Praxis adds its rules to it and prints
  the one command to commit it. A file your project tells git to ignore is left alone.

**2. Start two agents.** Each one gets a name, a task and the files it will touch:

```
praxis start claude-1 "make the player jump higher" scripts/player.gd
praxis start codex-1 "add a second level" scenes/level2.tscn art/level2
```

Praxis prints a folder for each agent (next to your project, in `<your game>-praxis/`). Open Claude Code in
the first folder and Codex in the second, and give each one its task.

An agent's folder holds only what is committed, so things git ignores are not in it: installed packages
(`node_modules/`), and engine caches such as Unity's `Library/` or Godot's `.godot/`. If your check needs
them, the agent installs them in its own folder first (`npm install`), or the engine rebuilds them on the
first check, which can take a while for a big Unity project. The agent rules say so.

**3. Watch.** Any time, run:

```
praxis status
```

and you see who is working on what, which asset files are locked, who is waiting to land, and anything that
needs you.

**4. The agents land their work.** When an agent finishes, it runs `praxis land <its name>` (the rules in
AGENTS.md tell it to). Praxis tests the work and adds it to the main version only if the check passes.
If two agents finish at once, the second simply waits its turn. A landing can take minutes; the rules tell
agents to give it a long time limit and never stop it part-way. While a long check runs, Praxis says about once a
minute that it is still running, with the check's last line of output and where its full output is, so nobody
mistakes it for a hang. The check never gets a keyboard, so a build script that ends with `pause` simply carries
on. Praxis keeps the newest 200 check logs, up to 1 GB together (the oldest go first).

**5. Finish.** When a task is done and landed, the agent (or you) runs `praxis done <name>` from the main
project folder (a folder cannot be removed while a terminal is in it; Praxis says so if you try). That frees
its files and removes its folder. Praxis refuses if anything has not landed yet, so nothing is lost. Files in the
folder that git ignores are not thrown away either. Only caches that rebuild by themselves are removed: the
engine's own cache folders at the top of the agent's folder (Godot `.godot/`, Unity `Library/`, `Temp/` and
`obj/`, Unreal `Intermediate/` and `DerivedDataCache/`) and any `node_modules/` or `__pycache__/`.
Everything else is moved to `.git/praxis/kept/`, and Praxis says so. A link inside the folder (for example one
`npm link` or `npm install file:...` made to a library somewhere else on your computer) is removed as a link:
what it points to is never touched.

**6. Put the work on GitHub (if you use it).** Praxis never pushes. Landed work is in your main folder's
branch; push it from there as usual (`git push`) whenever you like.

---

## Your game is not in git yet

Just run `praxis init` in the game's folder. When the folder is not part of any git project, Praxis sets git up
first, and it never changes, moves or deletes any of your files. It only adds:

- the `.git` folder, where git keeps its copies;
- a `.gitignore`, only if there is none, that leaves out what the engine rebuilds by itself (Unity `Library/`,
  `Temp/`, `Builds/`...; Unreal `Binaries/`, `Intermediate/`, `Saved/`, `DerivedDataCache/`; Godot `.godot/`;
  `node_modules/` and `dist/`) and clutter such as `Thumbs.db`, `.DS_Store` and `*.log`. If you write your own
  `.gitignore` first, Praxis keeps it exactly as it is;
- with [Git LFS](https://git-lfs.com) installed, a `.gitattributes` (only if there is none) that keeps big
  binary files (pictures, sound, models, video, `.uasset`, archives) in LFS. Without LFS they go into plain git:
  Praxis says so and lists any file over 50 MB that would be saved.

Line endings are kept exactly as they are: the new git project is set never to convert CRLF or LF, even where
Git for Windows would normally do so. Then Praxis saves all the files as the first commit ("Project files, set
up by Praxis") and carries on with the normal setup. It prints what it created, how many files it saved and
their size, what git leaves out, and the biggest files saved. Check that list (`git status --ignored` shows what
is left out) before agents start. A big folder (thousands of files, gigabytes) can take several minutes; leave
the window open. A backup copy of the folder is wise the first time. If anything goes wrong before the first
commit, Praxis takes away what it added, so the folder is as it was.

Praxis does not do this for a folder inside another git project (it stops and explains), for your home folder
or a whole drive, or for a folder that is part of a bigger game folder (it names the folder to run it in).

---

## Telling your agents to use Praxis

`praxis init` already wrote the rules into `AGENTS.md` (read by Codex, Cursor and most agents) and
`CLAUDE.md` (read by Claude Code). To be sure, start each agent with a message like this:

> You are **claude-1**. Read AGENTS.md and follow the Praxis rules in it exactly.
> Your task: make the player jump higher. Work only in the folder Praxis gave you.
> When you are finished, run `praxis land claude-1`. If you get stuck, run
> `praxis blocked claude-1 "<what you need>"` and stop.

Use a different name for each agent (`claude-1`, `claude-2`, `codex-1`...).

**Claude Code:** open it in the agent's folder (`cd` into the folder Praxis printed, then `claude`).
**Codex:** same, `codex` in the agent's folder. **Cursor:** open the agent's folder as the project.

---

## The commands

| Command | What it does |
|---|---|
| `praxis init` | Set the project up. Safe to run again. |
| `praxis start <agent> "<task>" <files...>` | Give an agent its own folder and claim its files. |
| `praxis claim <agent> <files...>` | Claim more files or folders. Code files can be shared; asset files become locks. |
| `praxis unclaim <agent> <files...>` | Let go of files. |
| `praxis check <agent>` | Run the checks of the agent's saved work now, on top of the newest main version, without landing it. `praxis land` reuses the passes. |
| `praxis land <agent>` | Test the agent's work and add it to the main version. `--done` also finishes it. |
| `praxis land <agent> --background` | The same, but returns as soon as the landing has its place in line. |
| `praxis wait <agent>` | Follow a background landing to its end: its output, result and exit code. |
| `praxis run --heavy <command...>` | Run a heavy job (a whole test suite, a browser run) only when fewer than `heavy_jobs` are running; otherwise wait in line. |
| `praxis fullcheck` | Run `full_check` (the test of the whole project) on the main version now, in a scratch folder. `--background` returns at once. |
| `praxis status` | One page: who is doing what, locks, the line, what needs you, recent landings. |
| `praxis done <agent>` | Finish an agent whose work has landed. |
| `praxis abandon <agent>` | Stop an agent. Unlanded work is saved on a branch named `praxis-saved/...`. |
| `praxis blocked <agent> "<why>"` | An agent asks you for help; it is paused until you answer. |
| `praxis resume <agent>` | You answered; the agent may carry on. |
| `praxis release-stale [agent...]` | Free the files of agents that went quiet for a long time (`--force`: even one that may still be working). |
| `praxis setup <agent>` | Run praxis.toml's `setup` command in the agent's folder again. |
| `praxis trust <check>` | Remember a named check's passes again, after its audit failed (see "Named checks"). |
| `praxis launcher` | Write `~/.praxis/bin/praxis`, a launcher at a fixed path, and register this Praxis with it. It keeps running the version it runs now; `--switch` points it at the newest installed Praxis. |
| `praxis update` | After installing a new Praxis: register it, look at every project Praxis is used in, and point the launcher at it when no command at work would be refused. `--when-safe` waits for that moment. |

Every command that works in a project also takes `--upgrade-now` and `--even-with-older-landings` (see "Several
versions of Praxis").

`praxis start` refuses a branch with the agent's name that Praxis did not make (it may be someone's work). Add
`--reuse-branch` to carry on from it on purpose.

File names can be written with `/` or `\`, and upper/lower case does not matter. They are read from the
folder you are in, the way a terminal reads them (so from `scripts/`, `player.gd` means `scripts/player.gd`).

### Batched landings

When two or more agents are waiting in the landing line, the agent at the front takes the others that are ready
and lands them together, so nine waiting agents do not wait nine checks one after another. Praxis puts each
agent's saved work on top of the newest version in line order, runs your check **once** for all of them, and if
it passes lands them all: each agent's commits stay its own, in line order, still marked by agent. Every waiting
`praxis land` still prints only its own result and exits with its own code (0, 1, 2 or 3 as above), and "taken
into a batch", with the names, always shows in its output before the result.

If the check fails, Praxis cuts the batch in half and checks each half on top of the newest version, again and
again, until it finds the work that broke it. The halves that pass land; the work that failed is refused with the
check's own output, and the others carry on. Nothing lands that was not checked as exactly what lands. An agent
whose work cannot join a batch (its changes clash with another agent's in the same batch) is left out and lands on
its own turn, so a refusal for a clash or for undoing another agent's work still names whose landing it is. With one
agent waiting, landing works exactly as it did before. `praxis status` shows who is in a batch. Turn it off, or cap
it, with `batch_max`.

If a `praxis land` in a batch is stopped part-way (an AI tool's time limit kills it), nobody's work is lost and
none is landed twice. When it was the agent at the front, the next landing records whatever did land and the line
carries on; that agent's folder is left on no branch, holding the batch's files, and its next `praxis land` puts it
back on its own branch together with every commit saved in it since (it says which) and every unsaved edit. When
it was an agent waiting in the batch, the batch still lands its work, and its next `praxis land` says "already
landed at <time> (in a batch)", levels its folder and exits 0 without taking a turn. Until then `praxis status`
lists the agent under NEEDS YOU with that one command. (With `check_folder = "line"` a batch is put together and
checked in the landing line's own folder, never in an agent's, so the front agent's folder stays on its branch: see
"Checking in the line's own folder" below.)

A batch uses the same memory of passed checks as a single landing. With named checks (see "Named checks" below), a
batch whose checks all have a remembered pass for the files they read in the batch lands without running anything.
When a batch fails, the checks that passed for it are remembered: for each smaller group Praxis runs again only
the checks whose files differ from what already passed, which is usually just the one that failed.

### Landing in the background

Many AI tools end a command after about 10 minutes, and a full check can take longer. Run
`praxis land <agent> --background`: it returns as soon as the landing has its place in the landing line and
says so. The landing itself carries on in a process of its own, with no window, that keeps going when the
command (or the tool's shell) that started it ends. Its output goes to the agent's land log,
`.git/praxis/background/<agent>.log`.

`praxis wait <agent>` waits (as long as it takes) until that landing ends, then prints its output and ends with
the same exit code `praxis land` would have. Run it again as often as you like: it reports the same result.
When no landing is running, it reports how the agent's last landing ended.

The check of a background landing runs in the same environment as the check of a landing in the foreground: the
one your command had. Praxis puts nothing of its own install into it, so a `praxis` your check starts (for
example `praxis run --heavy ...`) is the one your PATH or the launcher names at that moment, also after you have
updated Praxis while the landing was running.

### Heavy jobs

Whole test suites and browser runs can swamp the machine when several agents start them at once. Run them with
`praxis run --heavy <command...>` (for example `praxis run --heavy pytest`). Praxis starts the command only when
fewer than `heavy_jobs` heavy jobs (`praxis.toml`, default 2) are already running for this project, whichever agent
started them. Otherwise it says who is in front of it and waits its turn, with no time
limit. The job runs in your current folder with your environment (`PRAXIS_AGENT` and the rest), opens no window,
and its output goes to a log in `.git/praxis/logs/`; `praxis run --heavy` ends with the job's own exit code.
If a runner crashes or its terminal is closed, Praxis notices it is gone and frees its place. `praxis status` lists
the running and waiting heavy jobs with the agent and how long ago each started or joined.

**The order of the line.** A heavy job started from inside a landing's check goes first, because a whole batch of
agents may be waiting on that landing. Praxis sets `PRAXIS_CHECK=1` for the check command (a batch's check too),
and a `praxis run --heavy` that sees it joins ahead of the waiting jobs that do not come from a
check, behind other check jobs in the order they joined. It never stops a job that is already running. The other
jobs take turns by agent: an agent with nothing running goes before an agent that already has a job running, and
equal turns go by arrival. `praxis status` lists the waiting jobs in the order they will start and marks a check's
jobs "landing check". (The full check, an early check, `praxis check`, an audit and a named check that runs beside
another one get `PRAXIS_CHECK=1` as well. They already hold a heavy job place of their own, so a
`praxis run --heavy` inside them runs straight away.)

**One agent cannot fill the line.** An agent may have `heavy_waiting_per_agent` jobs (default 3) waiting at once.
One more `praxis run --heavy` from it is refused with exit code 1 and a message that lists the ones waiting; nothing
is run. Wait until one of them has started, or put the work into fewer, larger jobs. A landing check's jobs are never
refused and do not count, and neither do jobs with no agent's name on them (started in the main folder, or the full
check).

**A waiting job follows `praxis.toml`.** Change `heavy_jobs`, `heavy_max` or `heavy_min` in the main folder's
`praxis.toml` and the jobs already waiting go by the new values at their next look, and say so. You do not have to
stop and restart them.

A heavy job may itself call `praxis run --heavy` (a test script that starts a browser run, say). Praxis gives every
heavy job's environment a `PRAXIS_HEAVY_TOKEN`; when `praxis run --heavy` sees one that names a heavy job still
running in this project, it says it is already inside a heavy job and runs the command straight away, with the same
log, no window and the command's own exit code, and without taking a place. Otherwise two such jobs at the limit
would each hold a place while waiting for another and never finish. A leftover token (the outer job is gone, or it
belongs to another project) is ignored and the call waits in line as usual.

### The full check

Quick checks make a landing fast because they do not test everything every time (`checks` picks a check by the
files that changed, a check may test only what `PRAXIS_CHANGED_FILES` says the landing changes, the check cache
skips a tree that already passed, a named check with `inputs` is skipped while the files it reads are the same, a
batch is checked once). The full check is
the safety net behind them: one command that tests the WHOLE project, run on the main version every so often. Set
it in `praxis.toml`:

```
full_check = "npm run test:all"
full_check_every = 5
```

`praxis fullcheck` runs it now and ends with exit code 0 when it passed and 2 when it did not. With
`full_check_every = 5` Praxis also starts it by itself, in the background, after every 5 landings that did not run
that exact command as their own check (`0`, the default: only when you ask). A landing whose own check is the
same command counts as a pass, so nothing is tested twice.

It never runs in your main folder and never holds up the landing line. Praxis keeps a scratch folder of its own next
to the agent folders (`<project>-praxis/_fullcheck`), puts the files of the main version there and runs the command
in it, while agents keep landing. The folder holds only what is committed; the `share` folders are linked into it and
`setup` runs in it once, as for a new agent folder, and it is kept between runs so your tools' caches are kept too.
Every run of the command takes a heavy job place like any `praxis run --heavy` job (so it waits its turn when
`heavy_jobs` of them are already running) and gives the place back when it ends, so when the full check has to run
more than once, the other heavy jobs get their turn in between. It opens no window and writes to
`.git/praxis/logs/full-check-*.log`. The command gets `PRAXIS_COMMIT`, the commit it is testing.
`praxis fullcheck --background` starts it and returns at once; `praxis status` shows how it goes.

**One failed run is not proof.** A whole test suite also fails on a PC that is too busy for its time limits, and
when one test sometimes fails by itself. Praxis knows a run's exit code and where its output is, nothing more (it
never reads test output), so the only way it can tell is to run again. The rule:

- A pass counts on one run. A failure counts only when the same commit has failed twice.
- When the newest main version fails, the command runs a second time on the same commit. If it passes then, Praxis
  records "failed once, then passed", names nobody and prints no undo command. `praxis fullcheck` ends with exit
  code 0, and `praxis status` says what happened under FULL CHECK, with the output of the run that failed and of the
  run that passed. That is information, not an alarm. It becomes a notice under NEEDS YOU only when it keeps
  happening: when the same files gave two answers in 2 of the last 3 runs of the full check.
- When it fails both times, Praxis looks for the landing behind it. It takes the landings since the last pass and
  searches them the way `git bisect` searches commits: test the main version as it was after the middle landing,
  keep the half that holds the break, and again (a batch is searched agent by agent). The same rule holds at every
  step: a version that fails is run again at once, and one that then passes is taken for good.
- A landing is named only when the full check failed twice on the main version right after it, passed on the
  version right before it, and nothing in this search passed after that last failure. If something did (the
  failures may all be older than the passes: the PC was busy, and then it was not), the version about to be named
  runs once more. If it fails again, it is named. If it passes now, it is good after all and the search goes on
  above it; and when in the end the newest version itself passes, the failure did not come back: nobody is named,
  and status says that Praxis could not make it come back.

`praxis status` then shows under NEEDS YOU what the runs showed and no more: how many times the newest version
failed, the landing it starts to fail at, the commit, how many times it failed there and the output of each of those
runs, where the pass of the version before it comes from (this same search, a remembered pass of the same files, or
the full check's last pass, which is not run again), and the exact `git revert` command that undoes that landing.
If a version failed and then passed during the search, it says that too, because then the check is not steady and
you should read the outputs before you undo anything. **Praxis never undoes a landing by itself.** You run that
command in the main folder, or have an agent land a fix; then `praxis fullcheck` checks again, and the notice goes
away when it passes.

**What a failure costs in runs.** A pass costs one run, as before. A failure that passes when run again costs two.
A real break costs two runs on the newest version, then the search: one run for every version that passes, two for
every version that fails, and sometimes one more of the version about to be named. That is about twice what it was
(before: one run, then one for each step of the search). With 5 landings since the last pass it is 5 or 6 runs
where it was 3 or 4 (with a full check of 20 minutes, about two hours of background runs, each waiting its turn in
the heavy job line); with one landing it is 2 where it was 1. A check that is not steady costs more, because every
failure that then passes was one extra run. `full_check_confirm = false` in
`praxis.toml` turns all of this off: every failed run counts at once and nothing runs a second time, which is
cheaper and will sometimes name a landing that broke nothing.

Good to know:

- Until the full check has passed once, there is no good version to compare with, so a failure is reported
  without a landing. Run `praxis fullcheck` once when you set it up.
- If the full check's process is cut short (a closed terminal, a crash, a restart), nothing is left half-done: the
  next landing notices and starts it again, and what its search had already found out is not run again. Failed runs
  recorded since the last pass count: a commit that failed once in an earlier full check needs one more failed run,
  not two.
- Exit code 3 from the command means "a tool or service I need is down", as for the normal check: Praxis reports
  that it could not judge the main version and blames no landing, also when that happens on a second run.
- What two failed runs cannot rule out: something outside the project's files that stays broken (a full disk, a
  port in use, a PC that stays busy for hours). Then every version fails twice, and the search ends at the oldest
  landing since the last pass. Status says which pass the version before it rests on, and when that is "the full
  check's last pass", that version was not run again: check out that commit and run the command yourself before
  you undo anything.
- A break that only shows sometimes (it fails, then passes on the same files) is taken for a one-off each time.
  The notice under NEEDS YOU for a check that keeps giving two answers is how you see it.

### Early checks for the line

Off by default. With `speculate = true` in `praxis.toml`, the agent behind the one that is landing does not just
wait: while the front agent's check runs, Praxis starts the next agent's check early, beside it. It builds "the
newest main version + the front agent's work + the next agent's work" in a scratch folder of its own
(`<project>-praxis/_early`) and runs the next agent's check there.

- If the front agent lands, the next agent's turn builds exactly the same files, sees that they already passed
  ("Skipping the check: same tree already passed"), and lands without waiting for a check. If the early run is
  still going, its turn waits for it instead of running the same check twice.
- If the front agent does not land, Praxis stops the early run (only the process it started for it) and forgets
  it. The next agent's check then runs on its own turn, as always.

Nothing ever lands from the scratch folder. The only thing an early check leaves behind is a remembered pass in the
check cache, for exactly the files and the command it ran, and a failure is never remembered. It is bounded: one
early check at a time, only for the first waiting agent that the front agent's batch did not take (`batch_max` was
reached, or batching is off), and only when a heavy job place is free at that moment (it takes one, and never waits
for one). If that agent's work clashes with the work that is landing, there is nothing to check early: it is told
about the clash on its own turn, as always. It trades CPU for waiting time, so leave it off on a busy machine.
`praxis status` shows a running early check, and its output goes to `.git/praxis/logs/early-check-*.log`.
With named checks (below), what an early check hands over is one remembered pass for each check that passed.

### Checking in the line's own folder

Off by default. Normally a landing's check runs in the folder of the agent that is landing, and a batch's check in
the folder of the agent at the front of the line. So nothing in that folder may change while the check runs: one
edit there and the check may not have tested what would land, the landing is refused ("files changed while the check
ran"), and when the check reads the whole project every other agent in the batch pays for it again on its own turn.

With `check_folder = "line"` in `praxis.toml` the landing line checks in a folder of its own,
`<project>-praxis/_line`: a git worktree on no branch, made at the first landing that needs it and kept. Before each
check Praxis makes it hold exactly the files being landed: the candidate's files as git has them, and nothing left
over from the check before except what git ignores. Then:

- **An edit in an agent's folder cannot spoil a check.** The verdict stands, for one landing and for a batch, and
  nobody pays for a check twice.
- **You can keep working while your landing is checked.** A landing lands your work as it was saved when the
  landing started. Commits you save in your folder after that, and files you change there, are not looked at again:
  no check tested them, so they do not land. They stay in your folder, on your branch; the landing names them when
  it ends, and your next `praxis land` lands them.
- **What your tools cache stays warm in one place.** Build output, compiled files, test caches (whatever git
  ignores) stay in that one folder from landing to landing, and git rewrites only the files that differ from the
  landing before, so a tool that goes by file times rebuilds only those.
- **A batch is put together there too**, and when it fails each smaller group is checked there, one after another.
  The front agent's folder never leaves its branch, so a `praxis land` that is stopped part-way leaves nothing in
  an agent's folder to put right.

It is one extra folder for the project, however many agents land: a copy of the committed files, plus whatever your
`setup` installs and your check builds in it.

**It is off unless you set it, because the two ways of checking are not the same for every project.** They agree
on the files git tracks (exactly the candidate), the command, what the command is told (`PRAXIS_AGENT`,
`PRAXIS_TASK`, `PRAXIS_CHECK`, `PRAXIS_BASE`, `PRAXIS_CHANGED_FILES` and your `env`), and the memory of passed
checks: a pass is remembered by the files and the command, never by the folder, so a pass earned one way counts
the other way too. They can differ in four things:

- **Files git ignores.** The line's folder holds what is committed, the `share` folders (linked, as in an agent
  folder) and what `setup` makes in it. `setup` runs there once, when the folder is made, and again when you change
  the `setup` line in `praxis.toml`. Packages an agent installed by hand in its own folder, or build output it left
  there, are not in the line's folder: a check that needs them fails there. And when a landing changes what must be
  installed (a new dependency), nothing installs it there by itself: let the check command install what it needs
  (`npm ci && npm test`), or `share` the folder, or delete the small file
  `.git/praxis/fullcheck/_line-setup.txt`: `setup` then runs in the folder again before the next check.
- **What earlier checks left behind.** In an agent's folder a check finds, in the ignored folders, what that agent's
  own earlier work left. In the line's folder it finds what the checks of the landings before it left, which is what
  makes it fast. A check that trusts a stale cache (the compiled copy of a source file that has been deleted) can
  give a different answer there.
- **The folder's path and branch.** The check's working folder is `<project>-praxis/_line`, not
  `<project>-praxis/<agent>`, and it is on no branch: `git branch --show-current` prints nothing there (as in a
  batch's check before), while `git diff --name-only $PRAXIS_BASE HEAD` gives the same names as always. A check
  that finds the agent's folder from `PRAXIS_AGENT`, or needs the branch name, sees the difference.
- **Files the check rewrites.** A check that rewrites files git tracks is refused either way, unless they are
  listed in `check_may_change`. In the line's folder no copy of them is kept in `.git/praxis/kept`, because nobody
  edits there.

**Try it before you turn it on.** `praxis check <agent>` runs your check in a scratch folder made the same way
(only committed files, `share` linked, `setup` run once). If it passes there on work that lands today, it passes in
the line's folder. If it fails there for a missing package or file, put that right in `setup`, `share` or the check
command first.

Good to know:

- One landing at a time uses the folder, the one whose turn it is, and it keeps
  `.git/praxis/checkfolder/running.lock` locked meanwhile. Early checks, `praxis check`, the audit and the full
  check each have a scratch folder of their own, so none of them ever writes into the line's folder or waits for it.
  If the lock is ever held by another process, the landing says so and checks in the agent's folder, as without the
  setting.
- If a landing is killed during its check, the next landing first clears what it left in the folder: changed
  files, new files, a half-done git step. Nothing in an agent's folder is touched for that: a rebase or merge the
  agent has under way there stays its own.
- If the folder cannot be made ready (your `setup` fails in it), nothing lands, it does not count against the
  agent, and the message says what to fix.
- If the main version moves while the check runs (someone commits to it outside Praxis), the work has to go on top
  of the newer version and be checked again, and that step happens in the agent's folder. When that folder has
  changed since the landing started, the landing stops there and says so, rather than land work it did not start
  with; land again.

### Named checks: a pass is remembered for the files the check reads

With many agents landing, the project's files change with every landing, so "these exact files already passed" is
almost never true for the next agent, and every landing pays for the whole check again. Named checks fix that: you
split the check into parts, and tell Praxis which files each part reads.

```
checks = [
  { name = "ui",     paths = ["css/**", "js/ui/**"], inputs = ["css/**", "js/ui/**", "package.json"], run = "npm run test:ui" },
  { name = "server", paths = ["server/**"], inputs = ["server/**", "shared/**"], env_inputs = ["NODE_ENV"], run = "npm run test:server" },
]
audit_every = 20
```

The rules, plainly:

- **Give every entry a `name`, or none.** Without names nothing changes: only the FIRST entry whose `paths` match
  runs. With names, **every** entry whose `paths` match a file the landing changes runs, and so does every entry
  that lists a changed file among its `inputs` (it reads that file). When no entry's `paths` match, your default
  `check` runs as well, as it always did for files no entry covers.
- **`inputs`** are the files the command reads, written like `paths`. Praxis remembers a pass of that check for
  exactly those files: it asks git for the content ids of every file matching `inputs` in what is about to land
  (it never reads the files), and adds the command's text and the values of the environment variables named in
  **`env_inputs`**. The next time the same check is due and all of that is the same, it is skipped:
  "Skipping check ui: it passed at <time> on exactly the files it reads". Whatever else changed in the project does
  not matter. Without `inputs` a named check reads the whole project, and its pass counts only for the exact same
  files, like the check cache.
- A failure is never remembered. The newest 200 passes of each check are kept.
- Several checks of one landing run one after another. While heavy job places are free (`heavy_jobs`), a check
  beyond the first runs at the same time and holds a place while it does, so the limit is never passed. They run
  in the same folder: if yours cannot share it, set `checks_together = false`.
- The landing output and `praxis status` name each check, and say which passes came from memory, for example
  "check passed (ui from memory, server)".

**What a landing guarantees changes when you declare `inputs`.** Without them: the exact files that land passed the
check. With them: **every check that reads a file in the landing has passed on exactly those file contents**, where
"reads" means what you wrote in `inputs`. If a check reads a file its `inputs` leave out, a pass can be taken from
memory for a version that check would fail on. Two things are the net behind that, so keep both on:

- **The audit** (`audit_every`, default 20; `0` turns it off). The first pass a check takes from memory, and then
  about one in 20, is run again anyway: on exactly the version that landed, in a scratch folder of Praxis's own
  (`<project>-praxis/_audit`), in the background, holding a heavy job place, never holding up the landing that
  used the pass. If it fails there, `praxis status` shows under NEEDS YOU "check ui passed from memory but fails
  when run: its inputs list is probably missing files", with the log. That check's remembered passes are dropped
  and it runs every time, with nothing remembered, until you have added the missing files to its `inputs` and run
  `praxis trust ui`. Praxis never undoes the landing that used the pass.
- **The periodic full check** (`full_check`, above), which tests the whole project whatever the `inputs` say.

### Checking before the landing line

`praxis check <agent>` runs the checks of an agent's saved (committed) work without landing it and without waiting
in the landing line. It builds what a landing would build right now, the newest main version with the agent's
commits on top, in a scratch folder of Praxis's own (`<project>-praxis/_check-1`, `_check-2`...), runs the checks
that landing would run, and remembers the ones that pass. It prints which checks passed, were reused from memory,
or failed, and ends with exit code 0 when all passed and 2 when not.

It never moves the main version, never changes the agent's branch, and never touches the agent's folder, so the
agent can keep working while it runs. A failure is not a landing try: nothing is recorded against the agent. It
holds one heavy job place while it runs (and waits for one like any heavy job), so no more of them run at once
than `heavy_jobs`, and no more scratch folders than that are made. The scratch folders hold only what is committed;
`share` folders are linked into them and `setup` runs in each once, as for the full check.

A later `praxis land` finds those passes. With `inputs`, a check is skipped as long as the files it reads are the
same, however many other agents landed in between, so the landing takes seconds. A check without `inputs` reads
the whole project: its pass is found only if nothing else landed meanwhile. The agent rules tell every agent to
run `praxis check` when its work is saved, before `praxis land`.

### Exit codes

The check command tells Praxis what happened by its exit code:

| Check exit code | Meaning | What Praxis does |
|---|---|---|
| 0 | the project works | lands the work |
| 3 | a problem that is not the agent's doing (a tool or service it needs is down) | does not land, says it needs the human, and never counts it towards pausing the agent |
| anything else | the agent's change broke something | does not land; the agent fixes it and lands again |

Praxis's own exit codes: **0** done; **1** refused, and the message says why (nothing was changed);
**2** not landed, or a step failed (`praxis start` whose setup command failed), and the message says why (work
is always kept); **3** Praxis hit a problem it did not expect; **130** stopped with Ctrl+C.

---

## Settings for bigger projects

All of these live in `praxis.toml`, and `praxis init` writes each one with a note. Praxis checks every setting's
name and form, and stops with a plain message if one is wrong.

**`exclusive_code`** (default `false`): code (text) files are shared. Several agents may claim and edit the
same one; `praxis start` and `praxis claim` say who else is working on it, and `praxis status` lists every shared
file with everyone working on it. At landing, putting the work on top of the newest main version is the clash
check: if git cannot combine two agents' edits, nothing lands, and the agent is told which files clash and (when
Praxis can tell) whose landing they clash with. Its folder is left clean, on its own branch, with every commit.
Asset files are never shared. `exclusive_code = true` makes every file one agent at a time again.

**`generated`**: files a command makes from other files, for example
`generated = [{ path = "js/thermodata.js", command = "node tools/gen.cjs", from = ["js/thermo.js", "js/data.js"] }]`.
Nobody may claim a generated file. After each landing whose change touched one of its `from` files, Praxis runs
the command in the main folder (with `env`, no keyboard, no window, no time limit), still inside that landing's
turn in the line, and if the file changed commits it onto main as "Rebuild js/thermodata.js (after claude-1's
landing)". If the command fails, the landing stays: `praxis status` shows the file as out of date with the log,
and the next landing tries again. Praxis never overwrites unsaved changes to the file in the main folder.

**`batch_max`** (default `4`): the most agents the agent at the front of the landing line lands together, with one
check (see "Batched landings" above). `1` turns batching off: one landing at a time, as before. Batching is for
`land_method = "rebase"`; with `"merge"` every landing is on its own. Must be 1 or more.

**`heavy_jobs`** (default `2`): how many `praxis run --heavy` jobs may run at once on this project (see
"Heavy jobs" above). The rest wait in line. A whole number, 1 or more, or `"auto"`: Praxis then works the limit out
from the machine's load each time a job asks. Idle CPU and memory give more places, a busy machine gives fewer,
aiming for about 75 percent CPU in total; the limit is never below 1 and never above **`heavy_max`** (default: half
the CPU cores, at least 2). Jobs already running are never stopped when the limit drops: it only decides whether the
next one may start. `praxis status` shows the current limit and the load it came from. Load is read with the
Python standard library only (Windows counters through ctypes, `/proc` and the load average elsewhere); if it cannot
be read, `"auto"` allows 2.

The heavy jobs' own work is not counted as "the machine is busy". Praxis measures the CPU each running heavy job
uses (its process and every process that one started) and takes it out of the machine's reading. What is left is
the load of other programs. The room between that and 75 percent is split into places the size of one job: the
average of the running jobs, where a job counts as at least one core, and as exactly one core until it has been
measured (two cores when no job is running). `praxis status` says what was counted, for example
`auto: 2 (other programs 22%, 2 heavy jobs using 48%, memory 34% free; ...)`. A process whose parent has already
ended is not found under its job, so a job that starts programs and lets go of them looks lighter than it is.

**`heavy_min`** (default: `2` on a machine with 8 or more CPU cores, `1` on a smaller one): the fewest places
`"auto"` gives. A job that uses every core it can get (a parallel build or test run) always fills the CPU, so sized
by CPU alone the limit would stay at 1; `heavy_min` keeps a big machine at 2 or more. Set `heavy_min = 1` to let the
limit fall to 1 again. Nearly full memory (under 10 percent free) allows 1 whatever `heavy_min` says. It cannot be
more than `heavy_max`.

**`heavy_waiting_per_agent`** (default `3`): the most heavy jobs one agent may have waiting in line at once (see
"Heavy jobs" above). `0` means no cap. Running jobs do not count, only waiting ones.

**`checks`**: a different check for different changes, for example
`checks = [{ paths = ["js/thermo.js", "css/**"], run = "npm run test:ui" }, { paths = ["js/**"], run = "npm run test:js" }]`.
For a landing Praxis looks at the files the landing changes and runs the `run` command of the FIRST entry with a
`paths` pattern matching any of them; if none matches it runs `check`. So the order is the strictness: put the
entries that must always win first. Patterns are read from the top of the project like the other path settings
(`*` stays inside one folder, `**` spans folders). Without `checks`, nothing changes. The landing output and
`praxis status` name the check that was used, for example "check passed (check 1 of `checks` (css/**))".
That first-match rule holds as long as no entry has a `name`. Give every entry a `name` and every matching entry
runs, each with a memory of its own for the files it reads (`inputs`, `env_inputs`): see "Named checks" above.

**`audit_every`** (default `20`): with named checks that list `inputs`, the first pass a check takes from memory and
then about one in this many is run again in the background to make sure the `inputs` list is complete (see "Named
checks" above). `0` turns the audit off; then only the full check stands behind `inputs`.

**`checks_together`** (default `true`): when one landing has several named checks to run, the ones beyond the first
run at the same time, in the same folder, while heavy job places are free. `false` runs them one after another.

**What a landing changes, for the check.** A check command can also decide for itself, file by file. For every
landing's check (`check` or every `checks` entry that runs, a batch's check, an early check, `praxis check`, and
an audit, which is told what the landing it audits changed) Praxis sets `PRAXIS_BASE`, the
commit of the main version the work was put on top of, and `PRAXIS_CHANGED_FILES`, the path of a text file listing
the files the landing changes compared with that commit: one per line, from the top of the project, with forward
slashes, in UTF-8. A deleted file is listed too, and a batch's check gets the files of all its agents together.
So a check can run only the tests those files can affect, for example
`pytest $(python tools/affected_tests.py --list "$PRAXIS_CHANGED_FILES")` with a tool of your own that knows your
project. The list sits next to the check's log in `.git/praxis/logs/` and goes when that log goes;
`git diff --name-only $PRAXIS_BASE HEAD` in the folder being checked gives the same names. The full check gets
neither variable, because it tests everything. Two rules keep such a check safe: when it meets a file it knows
nothing about, or gets no list, it runs everything; and `full_check` is set behind it. On Windows a check that runs
in WSL sees only the variables you name in `WSLENV`: add `env = { WSLENV = "PRAXIS_CHANGED_FILES/p:PRAXIS_BASE" }`
(the `/p` turns the Windows path into a WSL one).

**`full_check`** and **`full_check_every`** (default `""` and `0`): the command that tests the whole project, and
after how many landings Praxis runs it on the main version by itself (see "The full check" above). `0` means only
when you run `praxis fullcheck`. `full_check_every` above 0 with an empty `full_check` is refused, so you never
believe in a safety net that is not there.

**`full_check_confirm`** (default `true`): one failed run of the full check is not taken as proof. A failure counts
only when the same commit fails a second time, and a landing is named only when the full check failed twice on its
version and passed on the version before it (see "The full check" above, which also says what a failure then costs
in runs). `false`: every failed run counts at once and nothing runs a second time, as it was before this setting.

**`speculate`** (default `false`): start the next waiting agent's check early, while the check of the agent at the
front of the landing line runs (see "Early checks for the line" above). One at a time, and only when a heavy job
place is free.

**`check_folder`** (default `"agent"`): where a landing's check runs. `"agent"`: in the folder of the agent that is
landing (for a batch, of the agent at the front of the line), where nothing may change while the check runs.
`"line"`: in a folder the landing line keeps for itself, on exactly the files being landed, so an edit in an
agent's folder never costs anyone a second check and agents keep working while their landing is checked (see
"Checking in the line's own folder" above). It needs a check that passes in a folder holding only the committed
files, the `share` folders and what `setup` makes: `praxis check <agent>` shows you whether yours does.

**The check cache.** When the exact files that would land (the git tree of the candidate) already passed the same
check command, Praxis does not run the check again and says "Skipping the check: same tree already passed at
<time>". The command text must be identical: change the command in praxis.toml and nothing is reused. A failure
is never remembered, and the newest 200 passes are kept in the project database (database format 4). A check
that depends on something outside the files (the date, a web service) can pass today and fail tomorrow on the
same tree; keep such a check out of what you rely on, or change its command text to start fresh. This is the
memory of the default `check` and of `checks` entries without names: with no `inputs` anywhere in praxis.toml,
nothing about it changed. A named check has a memory of its own, keyed by the files it reads (see "Named checks"
above; database format 7).

**`branch_prefix`** (default `"praxis/"`): the start of every agent's branch name. It must be the start of a
valid git branch name and end with `/`, for example `"agents/"`.

**`setup`**: a command run once in each new agent folder right after `praxis start` makes it, for example
`"npm install"`. It runs in the agent's folder with `PRAXIS_AGENT` and `PRAXIS_TASK` set, no keyboard, no window
and no time limit; its output goes to a log in `.git/praxis/logs/` and Praxis prints a short summary. If it
fails, the folder stays, `praxis start` says so (exit code 2), and `praxis setup <agent>` runs it again.

**`share`**: folders git ignores in the main folder (for example `["desktop/node_modules"]`) that each new agent
folder links to instead of getting a copy: a junction on Windows, a symbolic link elsewhere. **All agents then
share one copy**: a check or setup command that writes into it changes it for every agent and for the main
folder. A share that git tracks, does not ignore, or that is not a folder in the main folder is skipped with a
warning. `praxis done` and `praxis abandon` remove these links as links; what they point to is never touched.

**`after_land`**: a command run in the main folder after each landing has moved the main branch and been
recorded, for example to rebuild or restart something. Praxis sets `PRAXIS_AGENT`, `PRAXIS_TASK` and
`PRAXIS_COMMIT` (the new main commit) for it. If it fails, Praxis prints a warning: the landing is done and
stays done.

**`after_done`**: a command run in the main folder after `praxis done` has finished an agent (also when
`praxis land --done` does), for example to clean up something the agent started. Praxis sets `PRAXIS_AGENT` and
`PRAXIS_TASK` for it. If it fails, Praxis prints a warning: the agent stays done.

**`commit_trailers`**: lines added as git trailers to every commit Praxis writes itself (saving an agent's
unsaved work when it lands, an abandoned agent's leftovers, and the joined commit when a landing joins an agent's
commits into one). `{agent}` and `{task}` are filled in, for example `["Agent: {agent}", "Task: {task}"]`.
Commits an agent makes itself are left as they are.

**`env`**: extra environment variables for the check, `setup`, `after_land` and `after_done`, for example
`env = { TEMP = "D:/tmp", TMP = "D:/tmp" }`. Values are text.

**`shell_fix`** (default `true`, Windows only): what a bare `bash` or `sh` at the start of a command means.
Windows keeps a `bash.exe` of its own in `C:\Windows\System32` that starts WSL (Linux inside Windows). WSL cannot
see a Windows path such as `C:\game\tools\test.sh`, so `bash tools/test.sh` ends with exit code 127 ("No such file
or directory") although the file is there. So when the check, `setup`, `after_land`, `after_done`, a `generated`
command or a `praxis run --heavy` command starts with `bash` or `sh` written as the name alone, and Windows would
start that launcher (or would find no such program), Praxis starts Git for Windows' own instead: `bin\bash.exe`
(or `usr\bin\bash.exe`) in the Git folder, found from the `git` on your PATH and then in `Program Files\Git`. It
prints one line naming both programs. `praxis run --heavy bash test.sh` needs this even when Git's bash comes
first on your PATH, because a program started directly is looked for in System32 before the PATH. If you have no
Git for Windows bash, the command runs as you wrote it and Praxis warns you that WSL's bash will run and cannot see
Windows paths. A bash you name by its path, a bash of your own that Windows would find first, and every command on
macOS and Linux are left alone. `shell_fix = false` leaves every command exactly as you wrote it.

**The program that ran.** Every command log in `.git/praxis/logs/` (the check, `setup`, `after_land`,
`after_done`, a `generated` rebuild, a heavy job) starts with the command as you wrote it, then a `Program:` line
with the full path of the file that was started, and the shell that started it when the command is a shell line.
When the first word is not a file Praxis can find beforehand (a shell's own command such as `cd`), the line says
so and names the shell.

**Signed commits.** If the project signs its commits (`commit.gpgsign = true` in git's settings), the commits
Praxis writes are signed too, with the key and program git is set up to use.

**Who the commits are by.** An agent folder commits with the git name and email it finds. When those match none of the
authors of the main branch's latest commits, `praxis start` and `praxis status` print a warning naming both
identities and the two lines that set the project's (`git config user.name ...`, `git config user.email ...`, run in
the main folder; they apply to every agent folder).

**Git hooks.** Every git command Praxis runs inside an agent's folder (and the check) has `PRAXIS_AGENT` and
`PRAXIS_TASK` set, so a project's git hooks can tell an agent's commit, and which agent, from a person's.

### Updating Praxis while agents work

You can install a newer Praxis and use it while agents are landing and heavy jobs are running. Nothing has to
stop first, and nothing is refused:

- **What the older Praxis is doing carries on.** A landing, a background landing, a heavy job that is running or
  waiting: each runs to its end with the Praxis it started with and ends with its own result. Commands the older
  Praxis starts afterwards work too: a check that calls `praxis run --heavy` by the old version's path, or an agent
  whose instructions still name the old version.
- **The newer Praxis works at once.** Its first command in a project adds what it needs to the project's database
  (new tables, new columns) and takes nothing away. It does not wait for the older commands, and they do not notice.
- **The versions do not check each other's work.** A landing takes into its batch only landings of its own
  version. A landing of another version, older or newer, lands on its own turn, in line order.
- **`praxis status` tells you while both are at work.** Its top lines name every command of an older Praxis that
  is still running:

  ```
  OLDER PRAXIS VERSIONS ARE AT WORK HERE: praxis land alice (pid 4242, Praxis 0.4.0); ...
    They carry on as usual, and so does every new command of those versions: this project's database (format 8)
    can be used by every Praxis from format 6 on.
  ```

What an older Praxis does not get is what the newer one added: it does not use the newer tables (the passes
remembered per named check, for one), so its landings run their checks as that version always did.

**Leave new settings for later.** Every Praxis refuses a `praxis.toml` that has a setting it does not know (it
could be a typing mistake). So while an older Praxis is still in use in a project, do not add settings to its
`praxis.toml` that only the newer one knows. `praxis init` of a newer Praxis writes all of its settings: a project
set up with it is for that Praxis and newer ones.

**Which older versions this covers.** A project's database says which Praxis can still use it: "usable from
format N" in the status lines above. A project started with this Praxis is usable from format 6. A project that
an older Praxis started keeps the number it has, so the Praxis that made it keeps working in it. A Praxis older
than that number refuses every command ("This project was used by a newer Praxis") and changes nothing: that
refusal is built into the versions already released and cannot be taken out of them, so update that Praxis.

**How to update.** Install the new version (it goes into a folder of its own, next to the old one). Then run
this once, with the new version:

```
praxis update
```

It registers the new version with the launcher, looks at every project Praxis is used in, and says for each one
what the new version's first command will do there:

```
Projects Praxis is used in (2), and what switching to this Praxis means for each:
  - C:\games\tiny: safe now: this Praxis only adds to its database (format 6 to 8).
      Commands of an older Praxis are at work there and carry on beside this one: praxis land alice (pid 4242, ...)
  - C:\games\other: safe now: its database (format 8) needs nothing from this Praxis.
Switched: it ran C:\...\old\python.exe before.
```

When every project is safe, which is the usual case, it points the launcher at the new version and exits with
code 0. From then on every `praxis` an agent starts through the launcher is the new one, and what the older one
is still doing carries on. Agents do not have to be idle, and you do not have to wait for a quiet moment.

Only when the new version must change a project's database in a way the older one cannot use (a breaking step,
see below) is there something to wait for. Then `praxis update` names the commands of the older Praxis that are
still running there, leaves the launcher as it is and exits with code 1: switching at that moment would get the new
version's `praxis land` refused in that project until those commands have finished. Run it again when they have,
or run `praxis update --when-safe`: it stays in the foreground, waits for them (as long as it takes; Ctrl+C stops
the waiting and changes nothing), and switches the moment the last one has finished.

Praxis knows a project from the moment one of its commands has run there: each leaves one small file with the
project's path in `~/.praxis/projects/`. A project where only a Praxis from before `praxis update` has run is not
known yet; run `praxis update` from inside that project and it is looked at too.

### Several versions of Praxis

This is how the section above is kept true. A project's database (`.git/praxis/praxis.db`) carries two numbers:

- its **format**: which steps it has had, so which tables and columns it holds;
- **usable from**: the oldest format whose Praxis can still use it safely.

Every change to the database is a numbered step, and every step is one of two kinds. An **additive** step adds a
table, or a column that has a default. A Praxis that does not know about it carries on unharmed, so the step is
taken at once, whoever is running, and "usable from" stays where it is. Every step since format 5 has been of this
kind. A **breaking** step is anything else: a Praxis from before it cannot use the database afterwards, and the
step raises "usable from" to its own number.

A Praxis works in every project whose "usable from" is not above the format it knows itself, using only what it
knows; what a newer Praxis added it leaves alone. The versions released before these two numbers know one number
only, and refuse a database where it is above their own format. That number is the one "usable from" is kept in,
which is why they keep working in a database a newer Praxis has added to.

**A breaking step is never taken while older commands are still running.** A landing or a heavy job can run for
hours. So before a newer Praxis takes a breaking step, it looks for commands of a Praxis older than that step that
are still running in the project:

- Every Praxis command keeps a small locked file in `.git/praxis/procs/` while it runs, saying its process number,
  the database format it knows, its version and the command. The operating system frees the lock when the process
  ends, however it ends, so a command that crashed is never counted as running. A command that was killed leaves
  its file behind; the next `praxis status` removes it.
- A Praxis from before this register is found by what it does keep locked: its place in the landing line and its
  heavy jobs.

While any of them is running, the newer Praxis leaves the database without that step. `praxis status` still shows
the project (with a line at the top naming the older commands), and commands that only use what the database
already has (`claim`, `start`, `done`, `blocked` and so on) work as usual. A command that needs the step
(`praxis land`, for one) changes nothing, exits with code 1 and says:

```
Older Praxis commands are still running in this project (praxis land alice (pid 4242, Praxis 0.4.0); ...), and
this newer Praxis changes the project's database in a way they cannot use. It will take over when they finish.
Nothing was changed.
```

Run it again when they have finished; the first command after that takes the step.

To take a breaking step at once, add `--upgrade-now` to any command (for example `praxis status --upgrade-now`).
What it does depends on what the older Praxis is still running:

- **Only heavy jobs:** it takes over. A heavy job that is running or waiting carries on and ends with its own exit
  code.
- **A landing (`praxis land`), or any other command:** it refuses, exits with code 1 and changes nothing. The check
  of an older landing starts `praxis` commands with the older Praxis, the database would refuse them, the check
  would fail, and every agent in that landing's batch would be told its work failed. The message names the
  landings: wait for them, or stop them and land them again with the newer Praxis afterwards. If you want to take
  over regardless, add `--even-with-older-landings` as well; the work of a landing that then fails is kept, and you
  land it again.

After such a takeover `praxis status` keeps naming the older commands that are still running, and says what they
can still do, until the last one has ended.

No Praxis version takes a landing of another version into its batch (see "Batched landings"). Each `praxis land`
writes into its place in the line which batch messages it understands, and a head of the line takes only places
that say what it says itself. A Praxis from before that rule cannot be changed any more, so a newer `praxis land`
also marks its place in a way that Praxis has always left alone.

Heavy jobs of several versions wait in one line too, and each waiting job decides by itself when it starts, by
the rule of the Praxis that started it. A Praxis before format 6 starts its jobs strictly in the order they
joined. So this Praxis never starts a job before such an older job that joined first, not even a landing check's
job: it puts its own jobs in order around the older one (those that joined before it, then it, then the rest),
`praxis status` lists the line in exactly that order, and a landing check's job that waits behind an older job
says so instead of claiming it goes first. The limit (`heavy_jobs`) holds for all of them together.

**If you change Praxis itself.** Every step in `store.py` says whether it is additive or breaking, and a test
checks that a step called additive only adds a table or a column with a default. Keep steps additive whenever you
can: a breaking step is the only thing that makes an update wait. `tests/test_mixed_versions.py` is the gate: it
builds the last released Praxis from git, puts its landings and heavy jobs in flight in a real project, switches
to the new code with `praxis update` in the middle, and passes only if every job ends with its own exit code,
every landing of both versions lands, the heavy limit is never exceeded and no command printed a refusal. It
takes minutes, so it is marked: `pytest -m "not mixed"` leaves it out, plain `pytest` runs it, and it skips
itself where there is no git history to build the older version from. At each release, set `BASELINE` in that
file to the released commit (the top of the file says how).

---

## When Praxis needs you

`praxis status` starts with **NEEDS YOU** when something is waiting on you:

- **An agent is paused.** Either it asked for help (`praxis blocked`), or its last few tries to land failed
  the same way without getting better. Praxis shows why. Look at the agent's work, decide what it should do,
  tell it, then run `praxis resume <name>`. An agent that is making progress (fewer problems each time) is
  never paused, however many tries it takes.
- **The full check is failing.** The test of the whole project (`full_check`) failed twice on the main version.
  Praxis shows the landing it starts to fail at, the commit, how many times it failed there and the output of each
  of those runs, where the pass of the version before it comes from, and the exact `git revert` command that undoes
  that landing. Praxis does not undo it for you: run the command in the main folder, or have an agent land a fix,
  then run `praxis fullcheck`. A full check that failed once and passed when it was run again is not here: it is told
  under FULL CHECK, and nobody is named.
- **The full check does not give one answer on the same files.** In 2 of its last 3 runs it failed and then passed
  on the same commit. No landing is named and nothing needs undoing, but find out why: a test that sometimes fails
  by itself, or a PC too busy for the check's time limits. Praxis shows the output of the runs that failed. The
  notice goes away after two runs in a row that give one answer each.
- **A check passed from memory but fails when run.** The audit ran a named check again that a landing had skipped
  because of a remembered pass, and it failed: its `inputs` list is probably missing files the check reads. Praxis
  shows the check, the landing that used the pass and the log, has dropped that check's remembered passes, and runs
  it every time from now on. Add the missing files to its `inputs` in praxis.toml, then run `praxis trust <check>`.
  If that landing broke the project, have an agent land a fix: Praxis never undoes a landing by itself.
- **A landing was stopped part-way.** An agent's `praxis land` ended before it finished (a tool's time limit, a
  closed window), or its folder is not on its own branch. Nothing is lost. Run `praxis land <name>`: it puts the
  folder back with everything saved in it, lands what has not landed, and says so when the work already landed.
- **An agent went quiet.** If an agent still holds files but has shown no sign of work for a long time
  (24 hours by default, `stale_after_hours` in praxis.toml), Praxis shows it. If that agent has stopped for
  good, run `praxis release-stale <name>` so others can use its files. Its folder and work are kept.
  Signs of work are: a Praxis command, a saved change, a file being edited, a landing running or waiting, and a
  program running inside its folder (a long test run: status says "running something now"). Praxis looks at the
  programs once per `praxis status`. On Windows it reads each program's command line, so a program counts when
  its command line names a path inside the folder (the shell an AI tool runs commands in usually does); it cannot
  see another program's current folder. On Linux it reads both from /proc; on macOS, command lines from `ps`.
  `release-stale` refuses an agent with programs running in its folder, edits not saved yet, or a landing in
  line, and says which; add `--force` when you are sure it has stopped.

---

## Troubleshooting

**"cannot claim these, because another agent is already working on them"**
Working as intended: two agents wanted the same asset file (or any file, with `exclusive_code = true`). Give
one of them other work, or wait until the first runs `praxis done`.

**"These files are generated, so nobody claims or edits them"**
The file is listed under `generated` in praxis.toml: Praxis rebuilds it after landings. Claim and edit the files
it is made from instead.

**"NOT landed: the check failed"**
The agent's change broke the check. Praxis printed the end of the check's output and kept the agent's work.
The agent should fix it and land again. The full output is saved; the message says where.

**A `bash` command ends with exit code 127 on Windows, or says "No such file or directory" for a file that is there**
The command started WSL's bash (`C:\Windows\System32\bash.exe`), which is Linux inside Windows and cannot see
Windows paths. The `Program:` line at the top of the command's log names the program that ran. Praxis swaps a
bare `bash` or `sh` at the start of a command for Git for Windows' bash by itself (see `shell_fix` above), so
this is left only when you have no Git for Windows (install it), when `shell_fix = false` is set in praxis.toml,
or when the `bash` is not the command's first word (inside a `.bat` file, or after `cd tools &&`): write the full
path there, for example `"C:/Program Files/Git/bin/bash.exe" tools/test.sh`.

**"the check reported a problem outside this agent's work"**
The check ended with exit code 3: it found a problem the agent did not cause (a tool or service it needs is
broken). Nothing landed and the agent's tries are not counted against it. Fix what the check reports, then land
again.

**"A branch named ... already exists, and Praxis did not make it"**
`praxis start` found a branch with the agent's name that Praxis did not make for it. It may hold someone's work,
so Praxis leaves it alone. Pick another name, or add `--reuse-branch` to carry on from that branch on purpose
(this is also how to pick up work `praxis abandon` saved).

**"This project was used by a newer Praxis"**
A newer Praxis has changed this project's database in a way this older one cannot use, or this Praxis is one of
the versions that refuse every database a newer Praxis has made. Nothing was changed. Update Praxis, or use the
newer one for this project. See "Updating Praxis while agents work".

**"Older Praxis commands are still running in this project"**
You ran a newer Praxis that must change the project's database in a way an older one cannot use, while commands
of an older one (the message lists them) are still at work in the project. That is rare: most updates only add to
the database and wait for nobody. The newer Praxis changed nothing and takes over when they finish: run your
command again then. If a listed command
is stuck or no longer wanted, stop it; if you must take over right now, add `--upgrade-now` to your command (it
goes ahead when only heavy jobs of the older Praxis are left). See "Several versions of Praxis".

**"Did not take this project over (--upgrade-now)"**
A landing of an older Praxis is still running in the project (the message names it), and taking over under it
would make its check fail. Nothing was changed. Wait for it (`praxis status` shows the landing line), or stop it
and land that agent again with the newer Praxis afterwards. See "Several versions of Praxis".

**"its changes clash with newer work"**
Another agent landed a change to the same lines first; the message names whose landing when Praxis can tell.
The agent's work is safe and its folder is clean, on its own branch. The message tells the agent exactly what to
do (rebase on the newest main version, combine the changes, then land again). For asset files, which cannot be combined, the
agent has to redo its change on the newest version. Claiming asset files first prevents this.

**"the main project folder has unsaved changes"**
Someone edited files directly in your main game folder, outside any agent folder. Save (commit) or undo those
changes, then land again. Tip: let only agents change files, each in its own folder.

**"files changed while the check ran"**
The agent's folder changed during the check, so the check may not have tested what would land, and nothing
landed. Usually an agent kept editing during a landing: land again once it stops. If your check itself
rewrites some files every time it runs (some Unity and Godot builds do), list them in praxis.toml, for
example `check_may_change = ["ProjectSettings/*.asset"]`. Paths are read from the top of the project: `*` stays
inside one folder and `**` spans folders, so `*.cfg` means only .cfg files at the top. After every check Praxis
puts those files back the way they were committed (keeping a copy in `.git/praxis/kept/`, in case an agent had
edited one), and lands. The message names the files and gives the exact line.
In a batch the check runs in the folder of the agent at the front, so only that agent is refused; the others land
on their own turn. A check that reads the whole project runs again for them, because a pass with edited files in
the folder is not known to be a pass of what would land. A named check that lists what it reads (`inputs`) keeps
its pass when none of the edited files is one it reads, so the others do not wait for it a second time.

**"submodules ... have commits that exist nowhere else"** or **"... changes that are not committed inside them"**
An agent changed something inside a submodule (an addon or plugin kept in its own git project) and did not
commit or push it there. Removing the folder would lose it, so Praxis stops; it cannot save changes inside a
submodule for you. Commit and push them from inside that submodule (or undo them), then run the same command
again.

**"... is not on any branch (a 'detached HEAD')" or "is on the branch ..., but its work must be on praxis/..."**
The agent switched branches in its folder. Praxis will not land or remove that folder until its work is back
on its own branch, so nothing gets skipped. The message gives the exact command when there is a safe one.
`praxis abandon <name>` also keeps everything, on `praxis-saved/...` branches.

**"...'s folder was left on a batch candidate by a landing that was stopped"**
The agent was at the front of a batch and its `praxis land` was killed during the check, so its folder stayed on
no branch, holding the batch's files. Praxis put it back on the agent's own branch. If the agent saved commits in
the folder in the meantime, Praxis put copies of them on the branch first, in order, and lists them; unsaved edits
and new files stay in the folder. Nothing to do.

**"...'s folder ... was left on no branch by a landing that was stopped" and nothing landed (exit code 1)**
The same, but Praxis could not put the folder back by itself, so it changed nothing: the folder is exactly as it
was. Either the commits saved in it since clash with what the agent's branch holds (the message lists them, names
the `refs/praxis/saved/<agent>-<time>` they are also kept under, and gives the `git switch` and `git cherry-pick`
lines that bring them onto the branch), or git would have to overwrite an unsaved edit (save it as a commit right
there, as the message says, and land again: Praxis then carries that commit onto the branch). When the work is
safely landed you can remove the kept name with `git update-ref -d refs/praxis/saved/<agent>-<time>`.

**"...'s saved work ... already landed at ... (in a batch)"**
Not a problem. The agent's earlier `praxis land` ended while it waited in a batch, and the batch landed its work
anyway. This landing levelled the agent's folder and landed nothing a second time.

**"... is in the middle of a git rebase"**
Someone started a `git rebase` of the main branch in the main folder and it has not finished. Finish it
(`git rebase --continue`) or cancel it (`git rebase --abort`), then land again.

**"Another program is using ...'s folder" or "You are inside ...'s folder"**
A folder cannot be removed while something is open in it. Close the editor, game engine, file window or
terminal that is open in that agent's folder (or `cd` to the main project folder), then run the command
again. Praxis checks this before it touches anything, so nothing was changed.

**"These files are held by another agent, so this work cannot land"**
The agent changed a file another agent is working on. Landing never changes a file someone else holds, and
that includes putting it back to an older version. In the agent's folder, `git checkout <main branch> --
<file>` takes the newest version of that file; then land again.

**"You are in ...'s folder, but asked to ... another agent"**
A command was typed with another agent's name from inside one agent's folder, which is almost always a typo.
Use your own name, or run it from the main project folder if you really meant the other agent.

**"... are separate git projects (each has its own .git)"**
An agent cloned or created a git project inside its folder. git cannot store that as part of your project
(it would land as an empty, broken folder), so Praxis stops. Delete the `.git` folder inside it so its files
become part of your project, or move it out. To keep it as a separate project on purpose, add it as a git
submodule yourself.

**"praxis.toml has setting(s) Praxis does not know" or "... must be ..."**
A setting's name is misspelled or its value has the wrong form. Praxis stops rather than guess, so a typo in
`check` can never mean "land without testing". The message names the setting and the form it needs. For a
Windows path in the check line, use forward slashes (`tools/build.bat`) or single quotes
(`check = 'tools\build.bat'`): inside double quotes a backslash starts a code.

**"... looks like a project of its own, but it has no git of its own"**
You ran `praxis init` in a game folder that is part of a bigger git project. Praxis works on a whole git
project, so it stops and asks. If the game belongs in that project, run `praxis init` at the top of it (the
folder with `.git`). If it does not (say, a home folder kept in git), give the game folder its own git first.

**An agent's folder was deleted by hand**
`praxis status` shows a PROBLEM line for it. Run `praxis abandon <name>`. Anything it had saved is kept on a
`praxis-saved/...` branch, and the message says how to carry on from it.

**Waiting in line takes a long time**
That is normal: landings go through the line, and each batch of waiting agents runs the full check once (see
"Batched landings" above; `batch_max = 1` makes it one landing, and one check, at a time). Praxis never gives up waiting. If a
landing process was closed or crashed, the next one in line notices and carries on (`praxis status` shows a
stopped landing as stopped). On Windows, a check whose landing was stopped is stopped with it. Problems in the
main folder never count against an agent's tries, so they never get an agent paused.

**Something else**
Run the command again with `PRAXIS_DEBUG=1` set to see the technical details, and show them to an agent.

---

## Where Praxis keeps things

- `praxis.toml`, `AGENTS.md`, `CLAUDE.md`: in your project, committed so every agent folder has them.
- Claims, locks, the landing line and the history: in `.git/praxis/`, never committed.
- One small file for each Praxis command that is running right now: in `.git/praxis/procs/`. Praxis removes them.
- Ignored files kept from removed agent folders: in `.git/praxis/kept/`. Delete them when you no longer need
  them.
- Agent folders: next to your project in `<project>-praxis/` (outside it, so game engines do not import
  copies of your project). Change this with `worktree_root` in praxis.toml.
- Each agent works on its own git branch, `praxis/<name>` (`branch_prefix` in praxis.toml changes the start).
- Commits an agent saved in a folder a stopped landing had left on no branch, when Praxis could not put them on
  its branch by itself: under `refs/praxis/saved/<agent>-<time>`. Praxis never removes these.
- The early checks' scratch folder (with `speculate = true`): `<project>-praxis/_early`, on no branch.
- The landing line's own check folder (with `check_folder = "line"`): `<project>-praxis/_line`, on no branch, kept
  between landings. You may delete it when no landing is running; Praxis makes it again.
- The full check's scratch folder: `<project>-praxis/_fullcheck`, on no branch. Its runs are recorded in the
  database (format 5), and their output is in `.git/praxis/logs/full-check-*.log`.
- The check cache (the newest 200 passes) and the heavy job queue are in the same database; each job's output goes to `.git/praxis/logs/<agent>-heavy-<time>.log`.
- The scratch folders of `praxis check`: `<project>-praxis/_check-1`, `_check-2`... (as many as checks ran at once),
  and the audit's: `<project>-praxis/_audit`. All on no branch, kept between runs. The named checks' remembered passes
  (the newest 200 of each) and the audit's count and verdict are in the database (format 7: tables `checkpasses` and
  `checktrust`); an audit's output is in `.git/praxis/logs/audit-*.log`.
- Background landings write to `.git/praxis/background/<agent>.log`; setup, after_land and check output go to
  `.git/praxis/logs/`.

Praxis never pushes anything to the internet, and never deletes work that has not landed. What it removes is
either already in the main version or a cache your tools rebuild, and it tells you when it does.

## License

MIT. Copyright (c) 2026 Calvin Corbett.
