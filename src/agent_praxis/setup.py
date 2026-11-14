"""`praxis init`: set a project up for several agents, without disturbing anything already there."""

from __future__ import annotations

from pathlib import Path

from agent_praxis import config, gitops, instructions
from agent_praxis.config import Config


def init(start: Path | str | None = None, check: str | None = None, commit: bool = True, say=print) -> Config:
    repo = gitops.find_repo(start)
    root = repo.root
    toml = root / config.FILE_NAME
    if toml.is_file():
        cfg = config.load(root)
        say(f"{config.FILE_NAME} already exists, so Praxis kept it as it is.")
    else:
        engines, assets, suggested = config.detect(root)
        branch = gitops.current_branch(root) or "main"
        cfg = Config(main_branch=branch, check=suggested if check is None else check, assets=assets, engines=engines)
        toml.write_text(config.render(cfg), encoding="utf-8", newline="\n")
        found = ", ".join(engines) if engines else "no particular game engine"
        say(f"Wrote {config.FILE_NAME}. Praxis found: {found}.")
        say(f"  Main version: branch {cfg.main_branch}")
        say(f"  Check command: {cfg.check or '(none yet: landings will not be tested)'}")
        say(f"  Asset file types locked one agent at a time: {len(cfg.assets)}")
    for name in instructions.FILES:
        result = instructions.write_into(root / name)
        words = {"created": "Created", "added": "Added the agent rules to the end of",
                 "updated": "Refreshed the agent rules in", "unchanged": "Agent rules already current in"}[result]
        say(f"{words} {name}.")
    repo.state_dir.mkdir(parents=True, exist_ok=True)
    if commit:
        _commit_setup(root, cfg, say)
    say("")
    say("Next: start each agent with its own name, task and files, for example:")
    say('  praxis start claude-1 "make the player jump higher" scripts/player.gd')
    say("Then tell the agent to read AGENTS.md (Claude Code reads CLAUDE.md by itself).")
    return cfg


def _commit_setup(root: Path, cfg: Config, say) -> None:
    files = [config.FILE_NAME, *instructions.FILES]
    on = gitops.current_branch(root)
    if on != cfg.main_branch:
        say(f"Did not commit the setup files: the main folder is on branch {on}, not {cfg.main_branch}.")
        say("Commit praxis.toml, AGENTS.md and CLAUDE.md to the main branch yourself, so every agent folder gets them.")
        return
    if gitops.commit(root, "Set up Praxis for several AI agents", paths=files):
        say("Committed praxis.toml, AGENTS.md and CLAUDE.md, so every agent's folder gets them.")
