"""A warning for the case where an agent's commits would be signed by someone other than the project's usual author.

An agent folder commits with the git identity it finds (user.name and user.email). On a machine with a different
global identity than the one the project's history uses, every agent commit would carry the wrong author.
"""

from __future__ import annotations

from pathlib import Path

from agent_praxis import gitops

RECENT = 10  # how many of the main branch's latest commits tell who the project's author is


def identity_in(folder: Path | str) -> tuple[str, str]:
    """(name, email) a commit made in *folder* would carry; Praxis's own stand-in where git has none."""
    proc = gitops.git(["config", "--get-regexp", r"^user\.(name|email)$"], folder, check=False)
    found = {}
    for line in proc.stdout.splitlines():
        key, _sep, value = line.partition(" ")
        found[key.lower()] = value.strip()
    return found.get("user.name") or "Praxis", found.get("user.email") or "praxis@localhost"


def recent_authors(ctx) -> list[tuple[str, str]]:
    """The distinct (name, email) of the main branch's latest commits, newest first."""
    proc = gitops.git(
        ["log", f"-n{RECENT}", "--format=%an%x1f%ae", f"refs/heads/{ctx.cfg.main_branch}", "--"],
        ctx.repo.root,
        check=False,
    )
    seen: dict[str, tuple[str, str]] = {}
    for line in proc.stdout.splitlines():
        name, _sep, email = line.partition("\x1f")
        seen.setdefault(email.strip().lower(), (name.strip(), email.strip()))
    return list(seen.values())


def warnings(ctx, folders: dict[str, Path | str]) -> list[str]:
    """One warning per distinct identity (naming the agents it affects) when the identity the agent *folders*
    would commit with matches none of the recent authors of the main branch. Empty when all is as expected."""
    authors = recent_authors(ctx)
    if not authors:
        return []
    known = {email.lower() for _name, email in authors}
    by_identity: dict[tuple[str, str], list[str]] = {}
    asked: dict[str, tuple[str, str]] = {}  # one question per folder, however many agents it stands for
    for agent, folder in folders.items():
        if not Path(folder).is_dir():
            continue
        ours = asked[str(folder)] if str(folder) in asked else identity_in(folder)
        asked[str(folder)] = ours
        if ours[1].lower() not in known:
            by_identity.setdefault(ours, []).append(agent)
    lines = []
    newest_name, newest_email = authors[0]
    for (name, email), agents in by_identity.items():
        who = ", ".join(agents)
        lines.append(
            f'Warning: {who} would commit as "{name} <{email}>", but the recent commits on {ctx.cfg.main_branch} are '
            f'by "{newest_name} <{newest_email}>". To set the project\'s identity (for every agent folder), run in '
            f"the main folder {ctx.repo.root}:\n"
            f'  git config user.name "{newest_name}"\n'
            f'  git config user.email "{newest_email}"'
        )
    return lines
