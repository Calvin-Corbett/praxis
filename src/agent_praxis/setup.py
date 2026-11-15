"""`praxis init`: set a project up for several agents, without disturbing anything already there."""

from __future__ import annotations

from pathlib import Path

from agent_praxis import config, gitops, instructions
from agent_praxis.config import Config
from agent_praxis.errors import PraxisError


def _uncommitted(root: Path, name: str) -> str | None:
    """'changed' or 'new' when *name* has edits git has not committed (Praxis would commit them too)."""
    raw = gitops.git(["status", "--porcelain=v1", "-z", "--", name], root).stdout
    for item in raw.split("\0"):
        if len(item) >= 4:
            return "new" if item.startswith("??") else "changed"
    return None


def init(start: Path | str | None = None, check: str | None = None, commit: bool = True, say=print) -> Config:
    here = Path(start or Path.cwd()).resolve()
    repo = gitops.find_repo(here)
    root = repo.root
    toml = root / config.FILE_NAME
    if not gitops.same_path(here, root) and not toml.is_file():
        engines, _a, check_here = config.detect(here)
        mine = engines or (["a project with its own build or tests"] if check_here else [])
        theirs_engines, _a, check_there = config.detect(root)
        theirs = theirs_engines or check_there
        if mine and not theirs:  # a project folder inside some bigger git project (a home folder kept in git, say)
            raise PraxisError(
                f"This folder ({here}) looks like a project of its own ({', '.join(mine)}), but it has no git of "
                f"its own: it is part of the git project at {root}. Praxis works on a whole git project, so it "
                "would set up all of that one. Nothing was changed.\n"
                f"- If this folder belongs in that project (one project with several parts): run praxis init in "
                f"{root}. Agents then get the whole project.\n"
                f"- If it should not be in that project at all (say, a home folder kept in git): give it its own "
                f'git first (cd "{here}", git init, git add -A, git commit -m "Project files"), then praxis init.'
            )
        say(f"Setting up the whole git project at {root} (the folder that holds .git).")
    ignored = {name for name in (config.FILE_NAME, *instructions.FILES) if gitops.is_ignored(root, name)}
    state = {name: _uncommitted(root, name) for name in instructions.FILES if name not in ignored}
    yours = [name for name, s in state.items() if s == "new"]  # your own file, never committed: never commit it
    if commit:  # never commit someone's own unsaved edits along with the setup files
        busy = [name for name, s in state.items() if s == "changed"]
        if busy:
            raise PraxisError(
                "Praxis adds its rules to AGENTS.md and CLAUDE.md and commits them. These have changes you have not "
                "committed yet, and committing would include them: " + ", ".join(busy) + ".\n"
                "Commit your changes to them first (or put them back the way they were), then run `praxis init` "
                "again. Nothing was changed. (Or run `praxis init --no-commit` and commit the files yourself.)"
            )
    if toml.is_file():
        cfg = config.load(root)
        say(f"{config.FILE_NAME} already exists, so Praxis kept it as it is.")
    else:
        engines, assets, suggested = config.detect(root)
        branch = _main_branch(root)
        cfg = Config(main_branch=branch, check=suggested if check is None else check, assets=assets, engines=engines)
        toml.write_text(config.render(cfg), encoding="utf-8", newline="\n")
        found = ", ".join(engines) if engines else "no particular game engine"
        say(f"Wrote {config.FILE_NAME}. Praxis found: {found}.")
        say(f"  Main version: branch {cfg.main_branch}")
        usual = [b for b in ("main", "master") if b != branch and gitops.branch_exists(root, b)]
        if usual and branch not in ("main", "master", "trunk", "develop"):
            say(f"  NOTE: you are on the branch {branch}, so Praxis made it the main version. If the finished version "
                f"of your project lives on {usual[0]}, change main_branch in {config.FILE_NAME} to \"{usual[0]}\".")
        say(f"  Check command: {cfg.check or '(none yet: landings will not be tested)'}")
        say(f"  Asset file types locked one agent at a time: {len(cfg.assets)}")
    for name in instructions.FILES:
        if name in ignored:
            say(f"Left {name} alone: this project tells git to ignore it, so agent folders would never get it.")
            continue
        result = instructions.write_into(root / name)
        words = {"created": "Created", "added": "Added the agent rules to the end of",
                 "updated": "Refreshed the agent rules in", "unchanged": "Agent rules already current in"}[result]
        say(f"{words} {name}.")
    repo.state_dir.mkdir(parents=True, exist_ok=True)
    if set(instructions.FILES) <= ignored:
        say("WARNING: both AGENTS.md and CLAUDE.md are ignored by git, so agents will not see the Praxis rules. "
            "Remove them from .gitignore and run `praxis init` again.")
    if commit:
        _commit_setup(root, cfg, say, ignored | set(yours))
        if yours:
            say("")
            say(f"{' and '.join(yours)} {'was' if len(yours) == 1 else 'were'} already here but not in git, so Praxis "
                "added its rules without committing your file. Agent folders only get it once it is committed:")
            say(f"  git add {' '.join(yours)}")
            say('  git commit -m "Agent rules"')
    setup_files = {config.FILE_NAME, *instructions.FILES}
    loose = [p for p in gitops.dirty_files(root) if p not in setup_files]
    if loose:
        say("")
        say(f"WARNING: {len(loose)} file(s) in this project are not committed (for example: {', '.join(loose[:5])}).")
        say("Agent folders are made from what is committed, so agents will not see them. Commit them first:")
        say("  git add -A")
        say('  git commit -m "Project files"')
    say("")
    say("Next: start each agent with its own name, task and files, for example:")
    say('  praxis start claude-1 "make the player jump higher" scripts/player.gd')
    say("Then tell the agent to read AGENTS.md (Claude Code reads CLAUDE.md by itself).")
    return cfg


def _main_branch(root: Path) -> str:
    """The branch the main folder is on; when it is on none (a 'detached HEAD'), the usual main branch name
    that exists, else "main"."""
    on = gitops.current_branch(root)
    if on:
        return on
    for name in ("main", "master", "trunk"):
        if gitops.branch_exists(root, name):
            return name
    return "main"


def _commit_setup(root: Path, cfg: Config, say, ignored: set[str]) -> None:
    files = [name for name in (config.FILE_NAME, *instructions.FILES) if name not in ignored]
    if not files:
        return
    on = gitops.current_branch(root)
    if on != cfg.main_branch:
        where = f"on branch {on}" if on else "not on any branch (a 'detached HEAD')"
        say(f"Did not commit the setup files: the main folder is {where}, not on {cfg.main_branch}.")
        say("Commit praxis.toml, AGENTS.md and CLAUDE.md to the main branch yourself, so every agent folder gets them.")
        return
    if gitops.commit(root, "Set up Praxis for several AI agents", paths=files):
        say(f"Committed {', '.join(files)}, so every agent's folder gets them.")
