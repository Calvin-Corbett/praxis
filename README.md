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

**The launcher never switches by itself.** After you install a new version, run `praxis launcher` once with it. That
only registers the new install: the launcher keeps running the version it ran before, and the command tells you
which version the launcher runs now, which is the newest, and the exact command that switches. When you choose to
switch (best when no agent is in the middle of a landing or a heavy job), run that command:
`praxis launcher --switch` with the new version. It points the launcher at the newest installed Praxis; among
installs of the same version, the one you ran it from wins. If the version the launcher runs is uninstalled, the
launcher runs the newest one that is left. The launcher only chooses which Praxis runs; see "Several versions of
Praxis" for what keeps two versions from mixing inside one project.

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
| `praxis launcher` | Write `~/.praxis/bin/praxis`, a launcher at a fixed path, and register this Praxis with it. It keeps running the version it runs now; `--switch` points it at the newest installed Praxis. |

Every command that works in a project also takes `--upgrade-now` (see "Several versions of Praxis").

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
into a batch" shows in its output while it waits.

If the check fails, Praxis cuts the batch in half and checks each half on top of the newest version, again and
again, until it finds the work that broke it. The halves that pass land; the work that failed is refused with the
check's own output, and the others carry on. Nothing lands that was not checked as exactly what lands. An agent
whose work cannot join a batch (its changes clash with another agent's in the same batch) is left out and lands on
its own turn, so a refusal for a clash or for undoing another agent's work still names whose landing it is. With one
agent waiting, landing works exactly as it did before. If the agent at the front is stopped part-way, every agent's
work is still saved: the next landing records whatever did land, puts the stopped agent's folder back on its own
branch, and the line carries on. `praxis status` shows who is in a batch. Turn it off, or cap it, with `batch_max`.

### Landing in the background

Many AI tools end a command after about 10 minutes, and a full check can take longer. Run
`praxis land <agent> --background`: it returns as soon as the landing has its place in the landing line and
says so. The landing itself carries on in a process of its own, with no window, that keeps going when the
command (or the tool's shell) that started it ends. Its output goes to the agent's land log,
`.git/praxis/background/<agent>.log`.

`praxis wait <agent>` waits (as long as it takes) until that landing ends, then prints its output and ends with
the same exit code `praxis land` would have. Run it again as often as you like: it reports the same result.
When no landing is running, it reports how the agent's last landing ended.

### Heavy jobs

Whole test suites and browser runs can swamp the machine when several agents start them at once. Run them with
`praxis run --heavy <command...>` (for example `praxis run --heavy pytest`). Praxis starts the command only when
fewer than `heavy_jobs` heavy jobs (`praxis.toml`, default 2) are already running for this project, whichever agent
started them. Otherwise it says who is in front of it and waits its turn, first come first served, with no time
limit. The job runs in your current folder with your environment (`PRAXIS_AGENT` and the rest), opens no window,
and its output goes to a log in `.git/praxis/logs/`; `praxis run --heavy` ends with the job's own exit code.
If a runner crashes or its terminal is closed, Praxis notices it is gone and frees its place. `praxis status` lists
the running and waiting heavy jobs with the agent and how long ago each started or joined.

A heavy job may itself call `praxis run --heavy` (a test script that starts a browser run, say). Praxis gives every
heavy job's environment a `PRAXIS_HEAVY_TOKEN`; when `praxis run --heavy` sees one that names a heavy job still
running in this project, it says it is already inside a heavy job and runs the command straight away, with the same
log, no window and the command's own exit code, and without taking a place. Otherwise two such jobs at the limit
would each hold a place while waiting for another and never finish. A leftover token (the outer job is gone, or it
belongs to another project) is ignored and the call waits in line as usual.

### The full check

Quick checks make a landing fast because they do not test everything every time (`checks` picks a check by the
files that changed, the check cache skips a tree that already passed, a batch is checked once). The full check is
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
The full check takes a heavy job place like any `praxis run --heavy` job (so it waits its turn when `heavy_jobs` of
them are already running), opens no window, and writes to `.git/praxis/logs/full-check-*.log`. The command gets
`PRAXIS_COMMIT`, the commit it is testing. `praxis fullcheck --background` starts it and returns at once;
`praxis status` shows how it goes.

**When it fails, Praxis finds the landing that broke it.** It takes the landings since the last pass and searches
them the way `git bisect` searches commits: test the main version as it was after the middle landing, keep the half
that holds the break, and again, each step one more run in the scratch folder (a batch is searched agent by agent).
`praxis status` then shows under NEEDS YOU whose landing broke it, the commit, the log of the run that fails
there, and the exact `git revert` command that undoes that landing. **Praxis never undoes a landing by itself.**
You run that command in the main folder, or have an agent land a fix; then `praxis fullcheck` checks again, and the
notice goes away when it passes.

Good to know:

- Until the full check has passed once, there is no good version to compare with, so a failure is reported
  without a culprit. Run `praxis fullcheck` once when you set it up.
- If the full check's process is cut short (a closed terminal, a crash, a restart), nothing is left half-done: the
  next landing notices and starts it again, and what its search had already found out is not run again.
- Exit code 3 from the command means "a tool or service I need is down", as for the normal check: Praxis reports
  that it could not judge the main version and blames no landing.
- A full check that sometimes fails by itself will sometimes blame a landing wrongly. Keep it deterministic.

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

**`checks`**: a different check for different changes, for example
`checks = [{ paths = ["js/thermo.js", "css/**"], run = "npm run test:ui" }, { paths = ["js/**"], run = "npm run test:js" }]`.
For a landing Praxis looks at the files the landing changes and runs the `run` command of the FIRST entry with a
`paths` pattern matching any of them; if none matches it runs `check`. So the order is the strictness: put the
entries that must always win first. Patterns are read from the top of the project like the other path settings
(`*` stays inside one folder, `**` spans folders). Without `checks`, nothing changes. The landing output and
`praxis status` name the check that was used, for example "check passed (check 1 of `checks` (css/**))".

**`full_check`** and **`full_check_every`** (default `""` and `0`): the command that tests the whole project, and
after how many landings Praxis runs it on the main version by itself (see "The full check" above). `0` means only
when you run `praxis fullcheck`. `full_check_every` above 0 with an empty `full_check` is refused, so you never
believe in a safety net that is not there.

**The check cache.** When the exact files that would land (the git tree of the candidate) already passed the same
check command, Praxis does not run the check again and says "Skipping the check: same tree already passed at
<time>". The command text must be identical: change the command in praxis.toml and nothing is reused. A failure
is never remembered, and the newest 200 passes are kept in the project database (database format 4). A check
that depends on something outside the files (the date, a web service) can pass today and fail tomorrow on the
same tree; keep such a check out of what you rely on, or change its command text to start fresh.

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

### Several versions of Praxis

Praxis keeps a format number in its database (`.git/praxis/praxis.db`). A newer Praxis brings an older
database up to date by itself, in one step that cannot be left half-done. An older Praxis that finds a database
from a newer one refuses every command ("this project was used by a newer Praxis") instead of guessing, so
several Praxis versions can be installed side by side safely.

**A newer Praxis never takes a project over while older commands are still running in it.** A landing or a heavy
job can run for hours, and it checked the database's format when it started. So before a newer Praxis brings a
project's database up to date, it looks for commands of an older Praxis that are still running there:

- Every Praxis command keeps a small locked file in `.git/praxis/procs/` while it runs, saying its process number,
  the database format it knows, its version and the command. The operating system frees the lock when the process
  ends, however it ends, so a command that crashed is never counted as running.
- A Praxis from before this register is found by what it does keep locked: its place in the landing line and its
  heavy jobs.

While any of them is running, the newer Praxis leaves the database as it is. `praxis status` still shows the
project (with a line at the top naming the older commands), and commands that only use what the older database
already has (`claim`, `start`, `done`, `blocked` and so on) work as usual. A command that needs the new format
(`praxis land`, for one) changes nothing, exits with code 1 and says:

```
Older Praxis commands are still running in this project (praxis land alice (pid 4242, Praxis 0.4.0); ...).
This newer Praxis will take over when they finish. Nothing was changed.
```

Run it again when they have finished; the first command after that brings the database up to date. To take over
at once, add `--upgrade-now` to any command (for example `praxis status --upgrade-now`): the older commands may
then stop with an error, so use it only when you know they can be run again.

A landing with a newer Praxis also never takes a landing of an older Praxis into its batch (see "Batched
landings"): each `praxis land` writes into its place in the line which batch messages it understands, and one that
says nothing, or less than the batch needs, lands on its own turn.

---

## When Praxis needs you

`praxis status` starts with **NEEDS YOU** when something is waiting on you:

- **An agent is paused.** Either it asked for help (`praxis blocked`), or its last few tries to land failed
  the same way without getting better. Praxis shows why. Look at the agent's work, decide what it should do,
  tell it, then run `praxis resume <name>`. An agent that is making progress (fewer problems each time) is
  never paused, however many tries it takes.
- **The full check is failing.** The test of the whole project (`full_check`) failed on the main version. Praxis
  shows whose landing broke it, the commit, the log, and the exact `git revert` command that undoes it. Praxis does
  not undo it for you: run the command in the main folder, or have an agent land a fix, then run `praxis fullcheck`.
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
A newer Praxis has used this project, and this one does not know its database format. Update Praxis, or use the
newer one for this project.

**"Older Praxis commands are still running in this project"**
You ran a newer Praxis while commands of an older one (the message lists them) are still at work in the project.
The newer Praxis changed nothing and takes over when they finish: run your command again then. If a listed command
is stuck or no longer wanted, stop it; if you must take over right now, add `--upgrade-now` to your command (the
older commands may then stop with an error). See "Several versions of Praxis".

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

**"submodules ... have commits that exist nowhere else"** or **"... changes that are not committed inside them"**
An agent changed something inside a submodule (an addon or plugin kept in its own git project) and did not
commit or push it there. Removing the folder would lose it, so Praxis stops; it cannot save changes inside a
submodule for you. Commit and push them from inside that submodule (or undo them), then run the same command
again.

**"... is not on any branch (a 'detached HEAD')" or "is on the branch ..., but its work must be on praxis/..."**
The agent switched branches in its folder. Praxis will not land or remove that folder until its work is back
on its own branch, so nothing gets skipped. The message gives the exact command when there is a safe one.
`praxis abandon <name>` also keeps everything, on `praxis-saved/...` branches.

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
- The full check's scratch folder: `<project>-praxis/_fullcheck`, on no branch. Its runs are recorded in the
  database (format 5), and their output is in `.git/praxis/logs/full-check-*.log`.
- The check cache (the newest 200 passes) and the heavy job queue are in the same database; each job's output goes to `.git/praxis/logs/<agent>-heavy-<time>.log`.
- Background landings write to `.git/praxis/background/<agent>.log`; setup, after_land and check output go to
  `.git/praxis/logs/`.

Praxis never pushes anything to the internet, and never deletes work that has not landed. What it removes is
either already in the main version or a cache your tools rebuild, and it tells you when it does.

## License

MIT. Copyright (c) 2026 Calvin Corbett.
