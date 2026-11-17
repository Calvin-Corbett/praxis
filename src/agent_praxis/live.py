"""Who is running Praxis in a project right now, so a newer Praxis never takes the project over under older ones.

Every Praxis command that opens a project's database keeps one small file in `<git folder>/praxis/procs/` locked with
the operating system's file lock for as long as it runs (the trick the landing line and the heavy jobs use). When the
process ends, however it ends, the lock is gone: a dead process is noticed without trusting a process number. The file
says who holds it: process number, the database format its code knows, its Praxis version and the command.

Only the first byte of such a file is locked (Windows lets nobody else read a locked byte), so what the file says
starts at the second byte. Landing-line tickets and heavy-job lock files carry the same kind of line. A Praxis from
before this register wrote those two empty, which is how its commands are told apart: a live ticket or heavy job with
nothing written in it belongs to an older Praxis.
"""

from __future__ import annotations

import json
import os
import time
import uuid
from pathlib import Path

if os.name == "nt":
    import msvcrt

    def try_lock(handle) -> bool:
        handle.seek(0)
        try:
            msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            return True
        except OSError:
            return False

    def unlock(handle) -> None:
        handle.seek(0)
        try:
            msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
        except OSError:
            pass
else:
    import fcntl

    def try_lock(handle) -> bool:
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            return True
        except OSError:
            return False

    def unlock(handle) -> None:
        fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


PROCS = "procs"
SWEEP_AFTER_SECONDS = 60  # an unlocked register file this old was left by a process that died (see _left_behind)
STARTING_SECONDS = 10  # a lock file this young, with no row yet, may belong to a command that is just starting
SHOWN = 12  # the refusal names at most this many commands
CROWDED = 200  # with more files than this in the register, a starting command first clears out the dead ones


def held(path: Path) -> bool:
    """True while some running process keeps the file at *path* locked."""
    if not path.exists():
        return False
    try:
        with open(path, "a+b") as handle:
            if try_lock(handle):
                unlock(handle)
                return False
            return True
    except OSError:
        return True  # cannot even open it: someone is using it


def stamp(handle, **facts) -> None:
    """Write who holds this lock file into it. Call it on a brand-new file BEFORE locking it, so a file that is locked
    always says who holds it. The first byte is only the one that gets locked."""
    handle.write(b"\n" + json.dumps(facts).encode("utf-8"))
    handle.flush()


def read_stamp(path: Path) -> dict | None:
    """What stamp() wrote into the lock file at *path*, or None when nothing (readable) is written there."""
    try:
        with open(path, "rb") as handle:
            handle.seek(1)  # the first byte is locked while its owner lives
            data = json.loads(handle.read().decode("utf-8"))
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def register(state_dir: Path, fmt: int, version: str, command: str):
    """Say that this process is working in the project until unregister() (or until it dies). Returns the handle to
    give to unregister()."""
    folder = state_dir / PROCS
    folder.mkdir(parents=True, exist_ok=True)
    if len(os.listdir(folder)) > CROWDED:  # processes that were killed leave their file: do not let them pile up
        running(state_dir)
    path = folder / f"{os.getpid()}-{uuid.uuid4().hex[:12]}.lock"
    handle = open(path, "a+b")  # noqa: SIM115 - held open, and locked, for as long as the command runs
    try:
        stamp(handle, pid=os.getpid(), format=fmt, version=version, command=command, started=time.time())
        for _ in range(300):  # another Praxis looking at the register holds the lock for an instant while it looks
            if try_lock(handle):
                return path, handle
            time.sleep(0.01)
    except BaseException:
        handle.close()
        raise
    handle.close()
    raise RuntimeError(f"could not lock this command's own register file {path}")


def unregister(registration) -> None:
    path, handle = registration
    try:
        unlock(handle)
        handle.close()
    except (OSError, ValueError):
        pass
    try:
        path.unlink()
    except OSError:
        pass


def _left_behind(path: Path) -> bool:
    """True when the unlocked register file at *path* belongs to no process any more, so it can be removed now.

    A command that is just starting has made its file and not locked it yet, and its file must stay. On Windows a
    file another process still has open cannot be removed, so trying is safe. Elsewhere the file's name gives the
    process number: a process that is gone left it. Only when that cannot be told does the file's age decide."""
    if os.name == "nt":
        return True
    try:
        os.kill(int(path.name.split("-")[0]), 0)
    except ProcessLookupError:
        return True
    except (OSError, ValueError, OverflowError):
        pass
    try:
        return time.time() - path.stat().st_mtime > SWEEP_AFTER_SECONDS
    except OSError:
        return False


def running(state_dir: Path, skip: Path | None = None) -> list[dict]:
    """What every live registered Praxis command wrote about itself, oldest first. Files left by dead processes
    (a killed one never removes its own) are removed whenever they are seen here."""
    found = []
    try:
        names = sorted(os.listdir(state_dir / PROCS))
    except OSError:
        return []
    for name in names:
        path = state_dir / PROCS / name
        if not name.endswith(".lock") or path == skip:
            continue
        if held(path):
            # Whoever wrote no (readable) stamp is counted as knowing no format at all: the careful reading.
            found.append(read_stamp(path) or {"command": "a Praxis command that did not say what it is"})
            continue
        if _left_behind(path):
            try:
                path.unlink()
            except OSError:
                pass
    return sorted(found, key=lambda facts: _number(facts.get("started")))


def _number(value) -> int:
    return int(value) if isinstance(value, (int, float)) else 0


def _short(text: str, limit: int = 90) -> str:
    """One line, cut to a length a person still reads: a heavy job's command can be several long paths."""
    text = " ".join(str(text).split())
    return text if len(text) <= limit else text[: limit - 3].rstrip() + "..."


def _tables(db) -> set[str]:
    return {row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type = 'table'").fetchall()}


def _unstamped(state_dir: Path, db, table: str) -> tuple[list, int]:
    """(the rows of *table* whose owner is alive and wrote nothing into its lock file, how many lock files with no
    row and nothing written are brand new). Both mean a Praxis from before the register is at work."""
    folder = state_dir / table
    rows = db.execute(f"SELECT * FROM {table} ORDER BY id").fetchall()
    older = [
        row
        for row in rows
        if held(folder / f"{row['token']}.lock") and read_stamp(folder / f"{row['token']}.lock") is None
    ]
    known = {f"{row['token']}.lock" for row in rows}
    starting = 0
    try:
        names = os.listdir(folder)
    except OSError:
        names = []
    for name in names:
        # Never try the lock of a file with no row: its owner may be taking that lock at this very moment.
        path = folder / name
        if not name.endswith(".lock") or name in known or read_stamp(path) is not None:
            continue
        try:
            if time.time() - path.stat().st_mtime < STARTING_SECONDS:
                starting += 1
        except OSError:
            pass
    return older, starting


LAND, HEAVY, OTHER = "land", "heavy", "other"  # what an older command is, for the code that decides by it


def _kind(command: str) -> str:
    """Which command a registered command line is: its first word after `praxis` that is not an option."""
    words = [word for word in str(command).split()[1:] if not word.startswith("--")]
    if words[:1] == ["land"]:
        return LAND
    return HEAVY if words[:1] == ["run"] else OTHER


def older(state_dir: Path, db, fmt: int, skip: Path | None = None) -> list[tuple[str, str]]:
    """The live Praxis commands in this project whose Praxis knows a database format older than *fmt*, each as
    (LAND, HEAVY or OTHER, a few words for a person: what, and the process number when known).

    *db* is the open database (any format). Commands that registered say their format themselves. A Praxis from before
    the register is found by what it does keep locked: its place in the landing line and its heavy jobs.
    """
    found = []
    for facts in running(state_dir, skip):
        if _number(facts.get("format")) < fmt:
            words = _short(facts.get("command") or "a Praxis command")
            about = [f"pid {facts['pid']}"] if isinstance(facts.get("pid"), int) else []
            about += [f"Praxis {facts['version']}"] if isinstance(facts.get("version"), str) else []
            found.append((_kind(facts.get("command") or ""), words + (f" ({', '.join(about)})" if about else "")))
    tables = _tables(db)
    starting = 0
    if "queue" in tables:
        rows, n = _unstamped(state_dir, db, "queue")
        starting += n
        found += [(LAND, f"praxis land {row['agent']} (an older Praxis, pid not known)") for row in rows]
    if "heavy" in tables:
        rows, n = _unstamped(state_dir, db, "heavy")
        starting += n
        for row in rows:
            about = ["an older Praxis"]
            about += [f"by {row['agent']}"] if row["agent"] and row["agent"] != "(nobody)" else []
            about += [f"its job is pid {row['pid']}"] if row["pid"] else []
            found.append((HEAVY, f"{_short('praxis run --heavy ' + row['command'])} ({', '.join(about)})"))
    if starting:
        found.append((OTHER, f"{starting} landing or heavy job of an older Praxis that is just starting"))
    return found


def older_commands(state_dir: Path, db, fmt: int, skip: Path | None = None) -> list[str]:
    """older(), as the words alone. Empty when a newer Praxis may take over."""
    return [words for _kind, words in older(state_dir, db, fmt, skip)]


def listed(commands: list[str]) -> str:
    shown = commands[:SHOWN]
    more = len(commands) - len(shown)
    return "; ".join(shown) + (f"; and {more} more" if more else "")


def refusal(commands: list[str]) -> str:
    return (
        f"Older Praxis commands are still running in this project ({listed(commands)}). This newer Praxis will take "
        "over when they finish. Nothing was changed. (To take over now instead, run the same command with "
        "--upgrade-now: it goes ahead when all that is left of the older Praxis are heavy jobs, and those finish "
        "normally.)"
    )


def no_takeover(found: list[tuple[str, str]]) -> str | None:
    """Why --upgrade-now must not take the project over under the older commands *found* (see older), or None when it
    may: when all that is left of the older Praxis are heavy jobs, which run to their end whatever the database's
    format.

    A landing is different. Its check starts `praxis` commands with the older Praxis, the newer database refuses
    them, the check fails, and every agent in that landing's batch is told its work failed."""
    landings = [words for kind, words in found if kind == LAND]
    others = [words for kind, words in found if kind == OTHER]
    if not landings and not others:
        return None
    parts = []
    if landings:
        parts.append(
            f"Landings of an older Praxis are still running in this project ({listed(landings)}), and taking over "
            "under them makes their checks fail: wait for them, or stop them and land them again with this Praxis "
            "afterwards."
        )
    if others:
        parts.append(
            f"Other commands of an older Praxis are still running in this project ({listed(others)}): wait for "
            "them to end."
        )
    return (
        "Did not take this project over (--upgrade-now). " + " ".join(parts) + " Nothing was changed. (Heavy jobs of "
        "an older Praxis alone do not stop --upgrade-now. To take over regardless, add --even-with-older-landings.)"
    )
