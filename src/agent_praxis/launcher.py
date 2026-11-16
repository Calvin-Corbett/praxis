"""One stable launcher at a fixed per-user path, so an agent's instructions never carry a version-specific path.

Each Praxis version installs into its own folder, so the `praxis` program of one version lives somewhere that changes
with every install. `praxis launcher` writes `~/.praxis/bin/praxis` (and `praxis.cmd` on Windows) that runs the newest
installed version it knows about. Every version that runs `praxis launcher` is remembered in
`~/.praxis/installs.json`; running it from an older version never makes the launcher older.

The launcher only chooses which Praxis to run. An older Praxis that finds a project's database from a newer one still
refuses every command (store.py), so the launcher cannot make versions mix.
"""

from __future__ import annotations

import json
import os
import re
import sys
from pathlib import Path

from agent_praxis import __version__

REGISTRY = "installs.json"


def home_dir() -> Path:
    """The per-user Praxis folder: $PRAXIS_HOME, else ~/.praxis."""
    env = os.environ.get("PRAXIS_HOME")
    return Path(env) if env else Path.home() / ".praxis"


def version_key(version: str) -> tuple[int, ...]:
    """'0.10.1' -> (0, 10, 1), so 0.10 sorts after 0.9. Anything that is not a number is read as 0."""
    return tuple(int(m.group()) if (m := re.match(r"\d+", part)) else 0 for part in version.split("."))


def _load(path: Path) -> list[dict]:
    try:
        items = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    return (
        [
            i
            for i in items
            if isinstance(i, dict) and isinstance(i.get("version"), str) and isinstance(i.get("python"), str)
        ]
        if isinstance(items, list)
        else []
    )


def _write_atomic(path: Path, text: str, executable: bool = False, newline: str = "\n") -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(path.name + ".tmp")
    with open(temp, "w", encoding="utf-8", newline=newline) as handle:
        handle.write(text)
    if executable:
        temp.chmod(0o755)
    os.replace(temp, path)


def _registered(home: Path, here_python: str | None = None, here_version: str | None = None) -> list[dict]:
    """Every remembered install whose program still exists, oldest registration first.

    The install running now is the most recent of all: it goes last, wherever it was before.
    """
    here = {"version": here_version or __version__, "python": here_python or sys.executable}
    items = [i for i in _load(home / REGISTRY) if os.path.normcase(i["python"]) != os.path.normcase(here["python"])]
    seen, out = set(), []
    for item in [*items, here]:
        key = os.path.normcase(item["python"])
        if key in seen:
            out = [o for o in out if os.path.normcase(o["python"]) != key]  # a later registration replaces it
        seen.add(key)
        if Path(item["python"]).is_file():
            out.append({"version": item["version"], "python": item["python"]})
    return out


def known_installs(home: Path, here_python: str | None = None, here_version: str | None = None) -> list[dict]:
    """Every remembered install whose program still exists, best first.

    A higher version always wins. Among equal versions the most recently registered install wins, and the install
    running now counts as the most recent of all.
    """
    registered = _registered(home, here_python, here_version)
    return sorted(registered[::-1], key=lambda i: version_key(i["version"]), reverse=True)


def _sh(pythons: list[str]) -> str:
    lines = ["#!/bin/sh", "# Written by `praxis launcher`: runs the newest installed Praxis. Do not edit."]
    for py in pythons:
        quoted = "'" + py.replace("\\", "/").replace("'", "'''") + "'"
        lines.append(f'if [ -x {quoted} ]; then exec {quoted} -m agent_praxis "$@"; fi')
    lines.append('echo "Praxis: no installed version was found. Install Praxis again, then run: praxis launcher" >&2')
    lines.append("exit 1")
    return "\n".join(lines) + "\n"


def _cmd(pythons: list[str]) -> str:
    lines = ["@echo off", "rem Written by `praxis launcher`: runs the newest installed Praxis. Do not edit."]
    for n, py in enumerate(pythons, 1):
        lines += [
            f'if not exist "{py}" goto next{n}',
            f'"{py}" -m agent_praxis %*',
            "exit /b %errorlevel%",
            f":next{n}",
        ]
    lines += [
        "echo Praxis: no installed version was found. Install Praxis again, then run: praxis launcher 1>&2",
        "exit /b 1",
    ]
    return "\r\n".join(lines) + "\r\n"


def write_launcher(say, home: Path | None = None) -> list[Path]:
    """(Re)write the launcher; returns the files written. Safe to run from any version at any time."""
    home = home or home_dir()
    registered = _registered(home)  # kept in registration order, so the next run still knows who came last
    _write_atomic(home / REGISTRY, json.dumps(registered, indent=2) + "\n")
    installs = known_installs(home)
    pythons = [i["python"] for i in installs]
    bin_dir = home / "bin"
    written = [bin_dir / "praxis"]
    _write_atomic(written[0], _sh(pythons), executable=True)
    if os.name == "nt":  # praxis.cmd for cmd and PowerShell; the extensionless one for Git Bash
        written.append(bin_dir / "praxis.cmd")
        _write_atomic(written[1], _cmd(pythons), newline="")
    newest = installs[0]
    say(f"The launcher is at {written[-1] if os.name == 'nt' else written[0]}")
    say(
        f"It runs Praxis {newest['version']} (the newest of {len(installs)} installed version"
        f"{'s' if len(installs) != 1 else ''} it knows about). Put that folder on your PATH, or use the full path in "
        "your agents' instructions, and it keeps working when Praxis is updated."
    )
    say("After installing a new Praxis version, run `praxis launcher` once with it.")
    return written
