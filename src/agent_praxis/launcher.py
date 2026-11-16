"""One stable launcher at a fixed per-user path, so an agent's instructions never carry a version-specific path.

Each Praxis version installs into its own folder, so the `praxis` program of one version lives somewhere that changes
with every install. `praxis launcher` writes `~/.praxis/bin/praxis` (and `praxis.cmd` on Windows), one fixed path that
runs an installed Praxis. Every version that runs `praxis launcher` is remembered in `~/.praxis/installs.json`.

The launcher never changes which Praxis it runs by itself. The first `praxis launcher` points it at the Praxis that
ran the command. After that, `praxis launcher` from another install only registers that install and says which
Praxis the launcher runs now and which is the newest; `praxis launcher --switch` points it at the newest. So
installing a new version, and registering it, takes nothing over while agents are at work with the one before. The
launcher file lists the other installs after the chosen one, newest first: when the chosen one is uninstalled, the
next one that still exists runs.

The launcher only chooses which Praxis to run. An older Praxis that finds a project's database from a newer one still
refuses every command, and a newer Praxis leaves a project's database alone while commands of an older one are still
running in it (store.py), so the launcher cannot make versions mix.
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
    ranked = sorted(enumerate(registered), key=lambda pair: (version_key(pair[1]["version"]), pair[0]), reverse=True)
    return [item for _, item in ranked]


def _same(a: str, b: str) -> bool:
    """True when two spellings name the same program (the sh launcher writes forward slashes on Windows too)."""
    return os.path.normcase(a).replace("\\", "/") == os.path.normcase(b).replace("\\", "/")


def runs_now(home: Path) -> str | None:
    """The program the launcher at *home* runs now: the first one its file lists that still exists, which is exactly
    what the launcher itself does. None when there is no launcher yet, or nothing it lists is still installed.

    Read from the launcher file itself, so it is right whichever Praxis version wrote that file.
    """
    bin_dir = home / "bin"
    shapes = [(bin_dir / "praxis", r"if \[ -x '(.+)' \]; then exec ")]
    if os.name == "nt":  # praxis.cmd first: it spells the paths the way Windows does
        shapes.insert(0, (bin_dir / "praxis.cmd", r'if not exist "(.+)" goto next\d+$'))
    for file, shape in shapes:
        try:
            text = file.read_text(encoding="utf-8")
        except (OSError, ValueError):
            continue
        for line in text.splitlines():
            found = re.match(shape, line.strip())
            if found and Path(found.group(1)).is_file():
                return str(Path(found.group(1)))
        return None
    return None


ABOUT = "Written by `praxis launcher`: runs the first Praxis below that is still installed. Do not edit."


def _sh(pythons: list[str]) -> str:
    lines = ["#!/bin/sh", f"# {ABOUT}"]
    for py in pythons:
        quoted = "'" + py.replace("\\", "/").replace("'", "'''") + "'"
        lines.append(f'if [ -x {quoted} ]; then exec {quoted} -m agent_praxis "$@"; fi')
    lines.append('echo "Praxis: no installed version was found. Install Praxis again, then run: praxis launcher" >&2')
    lines.append("exit 1")
    return "\n".join(lines) + "\n"


def _cmd(pythons: list[str]) -> str:
    lines = ["@echo off", f"rem {ABOUT}"]
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


def _named(install: dict) -> str:
    return f"Praxis {install['version']} ({install['python']})" if install["version"] else f"{install['python']}"


def write_launcher(say, home: Path | None = None, switch: bool = False) -> list[Path]:
    """Register this install and (re)write the launcher; returns the files written. Safe to run from any version at
    any time. The launcher keeps running the Praxis it runs now unless *switch* (then: the newest installed), and a
    launcher made for the first time runs the Praxis this command was run with."""
    home = home or home_dir()
    before = runs_now(home)  # read before anything is written
    registered = _registered(home)  # kept in registration order, so the next run still knows who came last
    _write_atomic(home / REGISTRY, json.dumps(registered, indent=2) + "\n")
    installs = known_installs(home)
    newest = installs[0]
    if switch:
        chosen = newest
    else:
        keep = before or sys.executable
        # A program the launcher runs that the list of installs does not know is still kept: its version is unknown.
        chosen = next((i for i in installs if _same(i["python"], keep)), {"version": "", "python": keep})
    pythons = [chosen["python"], *[i["python"] for i in installs if not _same(i["python"], chosen["python"])]]
    bin_dir = home / "bin"
    written = [bin_dir / "praxis"]
    _write_atomic(written[0], _sh(pythons), executable=True)
    if os.name == "nt":  # praxis.cmd for cmd and PowerShell; the extensionless one for Git Bash
        written.append(bin_dir / "praxis.cmd")
        _write_atomic(written[1], _cmd(pythons), newline="")
    say(f"The launcher is at {written[-1] if os.name == 'nt' else written[0]}")
    count = f"{len(installs)} installed version{'s' if len(installs) != 1 else ''} it knows about"
    if before and not _same(before, chosen["python"]):
        say(f"Switched: it ran {before} before.")
    if _same(chosen["python"], newest["python"]):
        say(f"It runs {_named(chosen)} now: the newest of the {count}.")
    else:
        program = newest["python"]
        say(f"It runs {_named(chosen)} now, and was left on it.")
        say(f"The newest of the {count} is {_named(newest)}. To make the launcher run the newest:")
        say("  " + (f'"{program}"' if " " in program else program) + " -m agent_praxis launcher --switch")
        say(
            "Switch when you choose to. Commands that are already running keep the Praxis they started with, and a "
            "newer Praxis leaves a project's database as it is until the older commands in that project have finished."
        )
    say(
        "Put the launcher's folder on your PATH, or use its full path in your agents' instructions, and it keeps "
        "working when Praxis is updated."
    )
    say(
        "After installing a new Praxis version, run `praxis launcher` once with it (that only registers it), then "
        "`praxis launcher --switch` when the launcher should run it."
    )
    return written
