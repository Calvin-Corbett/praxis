"""`praxis land <agent> --background` and `praxis wait <agent>`.

A landing can take longer than a tool's shell lets one command run (often 10 minutes). With --background, Praxis
starts the landing in a process of its own that outlives the command that started it, waits only until the
landing has its place in the landing line, then returns. `praxis wait <agent>` follows it to the end and ends with
the same output and exit code a landing run in the foreground would have had.

The background process keeps a small file locked for as long as it runs (like a place in the landing line), so a
landing whose process was closed or crashed is told apart from one still running. Its output goes to the agent's
land log, .git/praxis/background/<agent>.log, and its exit code is kept in Praxis's database.
"""

from __future__ import annotations

import os
import subprocess
import sys
import time
from pathlib import Path

from agent_praxis.agents import Ctx, check_name, need_agent
from agent_praxis.errors import PraxisError
from agent_praxis.landing_queue import _try_lock, _unlock, held, landing_now

POLL_SECONDS = 0.3
# Windows process flags. DETACHED_PROCESS is left out on purpose: with it Windows ignores CREATE_NO_WINDOW, and then
# every console program the landing starts (git, the check) would open a window of its own. A hidden console of
# its own, a process group of its own and leaving the caller's job make the landing just as independent.
_NEW_PROCESS_GROUP, _NO_WINDOW, _BREAKAWAY = 0x200, 0x08000000, 0x01000000


def paths(ctx: Ctx, agent: str) -> tuple[Path, Path]:
    """(the agent's land log, the file its background landing keeps locked)."""
    where = ctx.store.dir / "background"
    return where / f"{agent}.log", where / f"{agent}.lock"


def running(ctx: Ctx, agent: str) -> bool:
    return held(paths(ctx, agent)[1])


def start(ctx: Ctx, name: str, finish: bool = False) -> int:
    """Start the landing in the background and return once it has its place in line (or has already ended)."""
    name = need_agent(ctx, name)["name"]
    if landing_now(ctx.store, name) or running(ctx, name):
        raise PraxisError(f"{name} is already landing. Follow it with: praxis wait {name}  (nothing was changed)")
    log, _lock = paths(ctx, name)
    log.parent.mkdir(parents=True, exist_ok=True)
    args = [sys.executable, "-m", "agent_praxis", "land", name, *(["--done"] if finish else []), "--in-background"]
    with open(log, "wb") as out:
        proc, independent = _spawn(args, out)
    while True:
        code = proc.poll()
        if code is not None:  # it ended before it needed a place in line (nothing to land, or refused at once)
            _echo(ctx, log, 0, final=True)
            return code
        if landing_now(ctx.store, name):
            break
        time.sleep(POLL_SECONDS)
    ctx.say(f"{name}'s landing has its place in the landing line and carries on in the background.")
    ctx.say(f"To follow it to the end (its result and exit code): praxis wait {name}")
    ctx.say(f"Its output so far: {log}")
    if not independent:
        ctx.say("Note: Windows would not let the landing leave this terminal's group of processes, so closing this "
                "terminal (or the tool running it) may stop the landing. If that happens, land again.")
    return 0


def _spawn(args: list[str], out) -> tuple[subprocess.Popen, bool]:
    """Start *args* with no keyboard, no window, and nothing tying it to this process. (process, fully independent)"""
    env = dict(os.environ, PYTHONUNBUFFERED="1")
    env.setdefault("PYTHONIOENCODING", "utf-8")
    here = str(Path(__file__).resolve().parents[1])  # the same Praxis as this one, wherever it was installed
    env["PYTHONPATH"] = here + (os.pathsep + env["PYTHONPATH"] if env.get("PYTHONPATH") else "")
    kw = {"cwd": os.getcwd(), "env": env, "stdin": subprocess.DEVNULL, "stdout": out, "stderr": subprocess.STDOUT}
    if os.name != "nt":
        return subprocess.Popen(args, start_new_session=True, **kw), True
    flags = _NEW_PROCESS_GROUP | _NO_WINDOW
    try:
        return subprocess.Popen(args, creationflags=flags | _BREAKAWAY, **kw), True
    except OSError:  # the job this terminal runs in does not allow leaving it
        return subprocess.Popen(args, creationflags=flags, **kw), False


def run_child(ctx: Ctx, name: str, land) -> int:
    """In the background process: hold the lock, run *land* (which returns an exit code), and keep the code."""
    log, lock = paths(ctx, name)
    lock.parent.mkdir(parents=True, exist_ok=True)
    handle = open(lock, "a+b")  # noqa: SIM115 - held open, and locked, for the whole landing
    try:
        if not _try_lock(handle):
            ctx.say(f"Another background landing of {name} is already running. Follow it with: praxis wait {name}")
            return 1
        with ctx.store.write() as db:
            db.execute("INSERT OR REPLACE INTO background (agent, log, pid, started, ended, exit_code) "
                       "VALUES (?, ?, ?, ?, NULL, NULL)", (name, str(log), os.getpid(), time.time()))
        code = land()
        with ctx.store.write() as db:
            db.execute("UPDATE background SET ended = ?, exit_code = ? WHERE agent = ?", (time.time(), code, name))
        return code
    finally:
        _unlock(handle)
        handle.close()


def wait(ctx: Ctx, name: str) -> int:
    """Follow the agent's background landing to its end; then (or when none is running) report how its last
    landing ended. Returns the exit code that landing had."""
    name = check_name(name)
    log, _lock = paths(ctx, name)
    shown = 0
    if running(ctx, name):
        ctx.say(f"Waiting for {name}'s landing to end (Praxis waits as long as it takes). Its output:")
        try:
            while running(ctx, name):
                shown = _echo(ctx, log, shown)
                time.sleep(POLL_SECONDS)
        except KeyboardInterrupt:
            ctx.say(f"Stopped waiting. The landing carries on in the background; follow it again with: "
                    f"praxis wait {name}")
            return 130
    record = ctx.store.row("SELECT * FROM background WHERE agent = ?", (name,))
    newest = ctx.store.landings(name, limit=1)
    since = (record["ended"] or record["started"]) if record else None
    if record and not (newest and newest[0]["at"] > since):
        _echo(ctx, log, shown, final=True)
        if record["exit_code"] is None:
            ctx.say(f"{name}'s background landing stopped before it ended (its process was closed or crashed). "
                    "Its work is kept. Run `praxis status` to see where things are, then land again.")
            return 1
        ctx.say(f"({name}'s landing ended with exit code {record['exit_code']}.)")
        return int(record["exit_code"])
    if newest:
        last = newest[0]
        stamp = time.strftime("%b %d %H:%M", time.localtime(last["at"]))
        ctx.say(f"No landing of {name} is running. Its last landing ({stamp}): "
                + ("landed" if last["ok"] else f"NOT landed: {last['summary']}") + ".")
        return 0 if last["ok"] else 2
    raise PraxisError(f"No landing of {name} is running, and it has not tried to land yet, so there is nothing to "
                      f"wait for. Start one with: praxis land {name} --background")


def _echo(ctx: Ctx, log: Path, start: int, final: bool = False) -> int:
    """Print the log's complete lines from byte *start* on (all of it when *final*); returns where it stopped."""
    try:
        with open(log, "rb") as handle:
            handle.seek(start)
            data = handle.read()
    except OSError:
        return start
    end = len(data) if final else data.rfind(b"\n") + 1
    if end > 0:
        text = data[:end].decode("utf-8", errors="replace").replace("\r\n", "\n").rstrip("\n")
        if text:
            ctx.say(text)
    return start + max(end, 0)
