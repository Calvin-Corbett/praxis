# Praxis

**Let several AI agents build your game at the same time, without breaking each other's work.**

You tell Claude Code, Codex, Cursor or any other AI coding agent what to build. That works well with one
agent. With two or three at once, things go wrong: one agent overwrites another's change, a level file gets
edited by two agents and ends up broken, and something that worked yesterday stops working.

Praxis is a small helper that sits between your agents and your project. It does five things:

1. **Each agent gets its own copy of the project to work in**, so they never type over each other.
2. **Each agent says which files it will change ("claims" them) before it starts.** If another agent
   already has those files, Praxis says no and names who has them.
3. **Files that cannot be combined** (scenes, levels, prefabs, 3D models, textures, sounds) are **locked**:
   only one agent at a time may hold one.
4. **Finished work goes through a line, one agent at a time.** Praxis puts the work on top of the newest
   version, runs your project's check (a build or test command), and only keeps the work if the check passes.
   If it fails, the agent gets the error text and its work is kept.
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

The package is called `agent-praxis`; the command is `praxis`.

---

## Five-minute walkthrough with two agents

Your game lives in a folder that uses git (if it does not yet: open a terminal in the folder and run
`git init`, then `git add -A` and `git commit -m "first version"`).

**1. Set the project up (once).** Open a terminal in your game folder and run:

```
praxis init
```

Praxis looks at the folder, recognises Unity, Unreal, Godot or a web game, and writes:

- `praxis.toml`: a short settings file. The important line is `check = "..."`: the command that proves the
  game still works. Praxis guesses one when it can. If it leaves it empty, ask an agent:
  *"Set the check line in praxis.toml to a command that builds or tests this project."*
- A section in `AGENTS.md` and `CLAUDE.md` that tells every agent the rules. Anything already in those
  files is kept, line endings included. If you have changed either file and not committed the change yet,
  Praxis stops and asks you to commit it first, so it never commits your own unfinished notes. A file your
  project tells git to ignore is left alone.

**2. Start two agents.** Each one gets a name, a task and the files it will touch:

```
praxis start claude-1 "make the player jump higher" scripts/player.gd
praxis start codex-1 "add a second level" scenes/level2.tscn art/level2
```

Praxis prints a folder for each agent (next to your project, in `<your game>-praxis/`). Open Claude Code in
the first folder and Codex in the second, and give each one its task.

**3. Watch.** Any time, run:

```
praxis status
```

and you see who is working on what, which asset files are locked, who is waiting to land, and anything that
needs you.

**4. The agents land their work.** When an agent finishes, it runs `praxis land <its name>` (the rules in
AGENTS.md tell it to). Praxis tests the work and adds it to the main version only if the check passes.
If two agents finish at once, the second simply waits its turn.

**5. Finish.** When a task is done and landed, the agent (or you) runs `praxis done <name>`. That frees its
files and removes its folder. Praxis refuses if anything has not landed yet, so nothing is lost. Files in the
folder that git ignores are not thrown away either: caches your tools rebuild by themselves (like `.godot/`,
`Library/` or `node_modules/`) are removed, and everything else is moved to `.git/praxis/kept/`, and Praxis
says so.

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
| `praxis claim <agent> <files...>` | Claim more files or folders. Asset files become locks. |
| `praxis unclaim <agent> <files...>` | Let go of files. |
| `praxis land <agent>` | Test the agent's work and add it to the main version. `--done` also finishes it. |
| `praxis status` | One page: who is doing what, locks, the line, what needs you, recent landings. |
| `praxis done <agent>` | Finish an agent whose work has landed. |
| `praxis abandon <agent>` | Stop an agent. Unlanded work is saved on a branch named `praxis-saved/...`. |
| `praxis blocked <agent> "<why>"` | An agent asks you for help; it is paused until you answer. |
| `praxis resume <agent>` | You answered; the agent may carry on. |
| `praxis release-stale [agent...]` | Free the files of agents that went quiet for a long time. |

File names can be written with `/` or `\`, and upper/lower case does not matter. They are read from the
folder you are in, the way a terminal reads them (so from `scripts/`, `player.gd` means `scripts/player.gd`).

---

## When Praxis needs you

`praxis status` starts with **NEEDS YOU** when something is waiting on you:

- **An agent is paused.** Either it asked for help (`praxis blocked`), or its last few tries to land failed
  the same way without getting better. Praxis shows why. Look at the agent's work, decide what it should do,
  tell it, then run `praxis resume <name>`. An agent that is making progress (fewer problems each time) is
  never paused, however many tries it takes.
- **An agent went quiet.** If an agent still holds files but has shown no sign of work for a long time
  (24 hours by default, `stale_after_hours` in praxis.toml), Praxis shows it. If that agent has stopped for
  good, run `praxis release-stale <name>` so others can use its files. Its folder and work are kept.

---

## Troubleshooting

**"cannot claim these, because another agent is already working on them"**
Working as intended: two agents wanted the same file. Give one of them other work, or wait until the
first runs `praxis done`.

**"NOT landed: the check failed"**
The agent's change broke the check. Praxis printed the end of the check's output and kept the agent's work.
The agent should fix it and land again. The full output is saved; the message says where.

**"its changes clash with newer work"**
Another agent landed a change to the same lines first. The agent's work is safe. The message tells the agent
exactly what to do (combine the changes, then land again). For asset files, which cannot be combined, the
agent has to redo its change on the newest version. Claiming asset files first prevents this.

**"the main project folder has unsaved changes"**
Someone edited files directly in your main game folder, outside any agent folder. Save (commit) or undo those
changes, then land again. Tip: let only agents change files, each in its own folder.

**"files changed while the check ran"**
The agent's folder changed during the check, so the check may not have tested what would land, and nothing
landed. Usually an agent kept editing during a landing: land again once it stops. If your check itself
rewrites some files every time it runs, commit those files once or add them to `.gitignore`.

**"... is not on any branch (a 'detached HEAD')" or "is on the branch ..., but its work must be on praxis/..."**
The agent switched branches in its folder. Praxis will not land or remove that folder until its work is back
on its own branch, so nothing gets skipped. The message gives the exact command when there is a safe one.
`praxis abandon <name>` also keeps everything, on `praxis-saved/...` branches.

**"... is in the middle of a git rebase"**
Someone started a `git rebase` of the main branch in the main folder and it has not finished. Finish it
(`git rebase --continue`) or cancel it (`git rebase --abort`), then land again.

**"Could not remove the folder ... a program has files open in it"**
Close the editor, game engine or terminal that is open in that agent's folder, then run the command again.

**An agent's folder was deleted by hand**
Run `praxis abandon <name>`. Anything it had saved is kept on a `praxis-saved/...` branch.

**Waiting in line takes a long time**
That is normal: one landing at a time, and each runs the full check. Praxis never gives up waiting. If a
landing process was closed or crashed, the next one in line notices and carries on.

**Something else**
Run the command again with `PRAXIS_DEBUG=1` set to see the technical details, and show them to an agent.

---

## Where Praxis keeps things

- `praxis.toml`, `AGENTS.md`, `CLAUDE.md`: in your project, committed so every agent folder has them.
- Claims, locks, the landing line and the history: in `.git/praxis/`, never committed.
- Ignored files kept from removed agent folders: in `.git/praxis/kept/`. Delete them when you no longer need
  them.
- Agent folders: next to your project in `<project>-praxis/` (outside it, so game engines do not import
  copies of your project). Change this with `worktree_root` in praxis.toml.
- Each agent works on its own git branch, `praxis/<name>`.

Praxis never pushes anything to the internet, and never deletes work that has not landed. What it removes is
either already in the main version or a cache your tools rebuild, and it tells you when it does.

## License

MIT. Copyright (c) 2026 Calvin Corbett.
