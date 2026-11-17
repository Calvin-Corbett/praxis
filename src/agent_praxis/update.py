"""`praxis update`: make the launcher run this Praxis, once that takes nothing from the commands already at work.

Installing a new Praxis changes nothing by itself: the launcher (launcher.py) keeps running the version it ran.
`praxis update`, run with the new version, registers it, looks at every project Praxis is known to be used in, and
says for each one what this Praxis's first command would do to its database (store.py):

- only add to it, or nothing at all: safe at any moment, and the commands of the older Praxis carry on beside it;
- change it in a way an older Praxis cannot use (a breaking step): safe only when no command of an older Praxis is
  running there. Until then this Praxis would hold that step back, and its `praxis land` would be refused.

When every project is safe it points the launcher at this Praxis. When one is not, it names the commands it waits
for and changes nothing; with --when-safe it stays in the foreground, waits for them to finish, and switches then.

Praxis knows a project from the moment one of its commands has run there: each leaves one small file in the
per-user folder (`~/.praxis/projects/`), which holds the project's path and nothing else.
"""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import sys
import time
from pathlib import Path
from typing import NamedTuple

from agent_praxis import __version__, launcher, live, store

PROJECTS = "projects"
POLL_SECONDS = 2.0
SAFE, WAITS, OUT_OF_REACH = "safe", "waits", "out of reach"


def remember(root: Path, state_dir: Path, home: Path | None = None) -> None:
    """Note in the per-user folder that Praxis is used in the project at *root*. Written once per project, and a
    problem writing it stops nothing."""
    try:
        name = hashlib.sha256(os.path.normcase(str(root)).encode("utf-8")).hexdigest()[:16]
        path = (home or launcher.home_dir()) / PROJECTS / f"{name}.json"
        if not path.exists():
            launcher._write_atomic(path, json.dumps({"root": str(root), "state": str(state_dir)}) + "\n")
    except OSError:
        pass


def known_projects(home: Path) -> list[tuple[str, Path]]:
    """(the project's folder, where its Praxis state is) for every remembered project that still has a Praxis
    database, by folder name. The note of a project that is gone is removed."""
    found = {}
    try:
        notes = sorted((home / PROJECTS).glob("*.json"))
    except OSError:
        notes = []
    for note in notes:
        try:
            data = json.loads(note.read_text(encoding="utf-8"))
            root, state = str(data["root"]), Path(data["state"])
        except (OSError, ValueError, KeyError, TypeError):
            continue
        if (state / "praxis.db").is_file():
            found[os.path.normcase(root)] = (root, state)
        else:
            try:
                note.unlink()
            except OSError:
                pass
    return [found[key] for key in sorted(found)]


class Look(NamedTuple):
    """What switching to this Praxis means for one project."""

    root: str
    kind: str  # SAFE, WAITS or OUT_OF_REACH
    words: str  # one sentence for a person
    waits_for: list[str]  # with WAITS: the commands of an older Praxis that must finish first
    beside: list[str]  # with SAFE: the commands of an older Praxis that carry on beside this one


def look(root: str, state_dir: Path) -> Look:
    """Read (never change) the project's database and say what this Praxis's first command would do there."""
    conn = sqlite3.connect((state_dir / "praxis.db").as_uri() + "?mode=ro", uri=True, timeout=60)
    conn.row_factory = sqlite3.Row
    try:
        level, version = store.formats(conn)
        if version > store.FORMAT:
            words = (
                f"this Praxis cannot use it: a newer Praxis changed its database in a way only Praxis from "
                f"format {version} on can use (this one knows format {store.FORMAT}). Use that newer Praxis "
                "there."
            )
            return Look(root, OUT_OF_REACH, words, [], [])
        steps = range(max(level, 1) + 1, store.FORMAT + 1)
        breaking = [step for step in steps if store.KIND[step] == store.BREAKING]
        if breaking:
            waits_for = [one.words for one in live.older(state_dir, conn, max(breaking))]
            change = (
                f"this Praxis changes its database in a way an older Praxis cannot use (format {level} to "
                f"{store.FORMAT})"
            )
            if waits_for:
                return Look(
                    root,
                    WAITS,
                    f"not yet. {change[0].upper()}{change[1:]}, and commands of an older Praxis "
                    "are still running there.",
                    waits_for,
                    [],
                )
            return Look(root, SAFE, f"safe now: {change}, and no command of an older Praxis is running there.", [], [])
        beside = [one.words for one in live.older(state_dir, conn, store.FORMAT)]
        if level >= store.FORMAT:
            words = f"safe now: its database (format {level}) needs nothing from this Praxis."
        else:
            words = f"safe now: this Praxis only adds to its database (format {level} to {store.FORMAT})."
        return Look(root, SAFE, words, [], beside)
    finally:
        conn.close()


def _report(looks: list[Look]) -> list[str]:
    out = []
    for one in looks:
        out.append(f"  - {one.root}: {one.words}")
        if one.waits_for:
            out.append(f"      Waiting for: {live.listed(one.waits_for)}")
        if one.beside:
            out.append(
                f"      Commands of an older Praxis are at work there and carry on beside this one: "
                f"{live.listed(one.beside)}"
            )
    return out


def _projects(home: Path) -> list[tuple[str, Path]]:
    """The remembered projects, and the one this command was run in when it is not among them yet."""
    from agent_praxis import gitops
    from agent_praxis.errors import PraxisError

    found = known_projects(home)
    try:
        repo = gitops.find_repo()
    except (PraxisError, OSError):
        return found
    here = (str(repo.root), repo.state_dir)
    known = {os.path.normcase(root) for root, _state in found}
    if os.path.normcase(here[0]) not in known and (repo.state_dir / "praxis.db").is_file():
        remember(repo.root, repo.state_dir, home)
        found.append(here)
    return found


def run(say, when_safe: bool = False, home: Path | None = None, poll: float = POLL_SECONDS) -> int:
    """Register this Praxis and point the launcher at it when that is safe in every known project. Returns 0 when
    the launcher runs this Praxis afterwards, 1 when it was left as it was (the words say why)."""
    home = home or launcher.home_dir()
    newest = launcher.known_installs(home)[0]
    if not launcher._same(newest["python"], sys.executable):
        launcher.write_launcher(say, home)  # registers this install and says which one is the newest
        say("")
        say(
            f"Not switched: `praxis update` moves the launcher to the Praxis it is run with, and Praxis "
            f"{newest['version']} at {newest['python']} is newer than this one. Run it with that one:"
        )
        program = newest["python"]
        say("  " + (f'"{program}"' if " " in program else program) + " -m agent_praxis update")
        return 1
    say(f"This is Praxis {__version__} (database format {store.FORMAT}) at {sys.executable}")
    before = launcher.runs_now(home)
    if before and launcher._same(before, sys.executable):
        say("The launcher runs this Praxis already.")
    elif before:
        say(f"The launcher runs {before} now.")
    told = None
    while True:
        looks = [look(root, state) for root, state in _projects(home)]
        waiting = [one for one in looks if one.kind == WAITS]
        report = _report(looks)
        if report != told:
            told = report
            if looks:
                say(f"Projects Praxis is used in ({len(looks)}), and what switching to this Praxis means for each:")
                for line in report:
                    say(line)
            else:
                say(
                    "Praxis knows of no project it is used in yet (it learns of one when a command runs there), so "
                    "there is nothing to wait for."
                )
        if not waiting:
            break
        if not when_safe:
            say("")
            say(
                "Not switched: the launcher was left as it is, and nothing was changed. Switching now would get "
                'this Praxis\'s `praxis land` refused in the project(s) marked "not yet" until those commands have '
                "finished. Run `praxis update` again then, or run `praxis update --when-safe`: it waits for them "
                "here and switches the moment they have finished."
            )
            return 1
        if told is report:  # said only when the picture changed
            say(
                "Waiting for those commands to finish (as long as it takes; Ctrl+C stops the waiting and changes "
                "nothing) ..."
            )
        time.sleep(poll)
    say("")
    launcher.write_launcher(say, home, switch=True)
    return 0
