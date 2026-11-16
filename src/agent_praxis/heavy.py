"""`praxis run --heavy <command...>`: a queue for jobs that eat the machine (whole test suites, browser runs).

At most `heavy_jobs` (praxis.toml, default 2) heavy jobs run at once for the project, whichever agent started them;
the rest wait in line, first come first served, and say so. Each runner keeps a small file locked with the
operating system's file lock while it lives, exactly like the landing line's tickets: when a runner dies (closed
terminal, crash, power cut) the lock is freed, and the next runner to look sees its row is dead and removes it, so
a crashed job never holds a place. Nobody's wait times out.
"""

from __future__ import annotations

import os
import subprocess
import time
import uuid
from pathlib import Path

from agent_praxis import agents, hooks, load, shells
from agent_praxis.errors import PraxisError
from agent_praxis.landing_queue import _try_lock, _unlock, held
from agent_praxis.store import Store

POLL_SECONDS = 0.5
TOKEN_ENV = "PRAXIS_HEAVY_TOKEN"  # set in a heavy job's environment: the job's own token, so a nested call knows


def _lock_file(store: Store, token: str) -> Path:
    return store.dir / "heavy" / f"{token}.lock"


def alive(store: Store, token: str) -> bool:
    """True while the runner that owns this row is still running (it keeps its lock file locked)."""
    return held(_lock_file(store, token))


def rows(store: Store) -> list:
    """Running and waiting heavy jobs, oldest first. Rows whose runner is gone are left out (and cleared by the
    next runner)."""
    return [r for r in store.rows("SELECT * FROM heavy ORDER BY id") if alive(store, r["token"])]


def limit_now(ctx) -> tuple[int, str]:
    """(how many heavy jobs may run at once right now, and why). A number in praxis.toml is that number; "auto" is
    sized from the machine's load at this moment (see load.auto_limit)."""
    setting = ctx.cfg.heavy_jobs
    if setting != "auto":
        return max(1, int(setting)), f"heavy_jobs = {setting} in praxis.toml"
    running = sum(1 for r in rows(ctx.store) if r["started"])
    return load.auto_limit(running, load.read(), ctx.cfg.heavy_max)


def _forget_dead(store: Store, db, mine: int) -> None:
    for row in db.execute("SELECT * FROM heavy WHERE id != ?", (mine,)).fetchall():
        if not alive(store, row["token"]):
            db.execute("DELETE FROM heavy WHERE id = ?", (row["id"],))
            try:
                _lock_file(store, row["token"]).unlink()
            except OSError:
                pass


def _try_start(store: Store, mine: int, limit: int) -> tuple[bool, list]:
    """Start now if fewer than *limit* jobs run and nobody waited longer. Else (False, the jobs in front of us)."""
    with store.write() as db:
        _forget_dead(store, db, mine)
        live = db.execute("SELECT * FROM heavy ORDER BY id").fetchall()
        running = [r for r in live if r["started"]]
        before = [r for r in live if not r["started"] and r["id"] < mine]
        if len(running) < limit and not before:
            db.execute("UPDATE heavy SET started = ? WHERE id = ?", (time.time(), mine))
            return True, []
        return False, running + before


def _inside_heavy_job(store: Store) -> bool:
    """True when this process runs inside a heavy job of this project: the token in the environment names a row that
    exists here and whose runner is still alive and running. A stale or foreign token is not inside anything."""
    token = os.environ.get(TOKEN_ENV)
    if not token:
        return False
    rows = store.rows("SELECT * FROM heavy WHERE token = ?", (token,))
    return bool(rows) and bool(rows[0]["started"]) and alive(store, token)


def run(ctx, command: list[str], poll: float = POLL_SECONDS) -> int:
    """Wait for a free heavy place, run *command*, return its exit code."""
    if command and command[0] == "--":
        command = command[1:]
    if not command:
        raise PraxisError("Give the command to run, for example: praxis run --heavy pytest")
    store = ctx.store
    agent = os.environ.get("PRAXIS_AGENT") or agents.agent_here(ctx) or "(nobody)"
    text = command[0] if len(command) == 1 else subprocess.list2cmdline(command)
    if _inside_heavy_job(store):
        # Queueing here would hold a place while waiting for another: two such jobs at the limit would wait for each
        # other forever. The outer job already holds a place, so run directly and take none.
        ctx.say("Already inside a heavy job: running this command directly, without taking another place.")
        return _execute(ctx, command, text, agent, None, os.environ[TOKEN_ENV])
    token = uuid.uuid4().hex
    (store.dir / "heavy").mkdir(parents=True, exist_ok=True)
    handle = open(_lock_file(store, token), "a+b")  # noqa: SIM115 - locked BEFORE the row exists, so it never looks dead while alive
    mine = None
    try:
        if not _try_lock(handle):
            raise RuntimeError("could not lock a brand-new heavy job file")
        with store.write() as db:
            mine = db.execute(
                "INSERT INTO heavy (agent, token, command, enqueued) VALUES (?, ?, ?, ?)",
                (agent, token, text, time.time()),
            ).lastrowid
        told = None
        while True:
            limit, why = limit_now(ctx)  # read outside the database lock: measuring the load takes a moment
            go, ahead = _try_start(store, mine, limit)
            if go:
                break
            if told != len(ahead):
                told = len(ahead)
                ctx.say(
                    f"Waiting in line for a heavy job place: {len(ahead)} in front "
                    f"({', '.join(r['agent'] for r in ahead)}), at most {limit} run at once on this project "
                    f"({why}). "
                    "Praxis waits as long as it takes; this is normal."
                )
            time.sleep(poll)
        return _execute(ctx, command, text, agent, mine, token)
    finally:
        if mine is not None:
            with store.write() as db:
                db.execute("DELETE FROM heavy WHERE id = ?", (mine,))
        _unlock(handle)
        handle.close()
        try:
            _lock_file(store, token).unlink()
        except OSError:
            pass


def _execute(ctx, command: list[str], text: str, agent: str, mine: int | None, token: str) -> int:
    env = hooks.command_env(ctx.cfg)
    env[TOKEN_ENV] = token
    if agent != "(nobody)":
        env.setdefault("PRAXIS_AGENT", agent)
    log = hooks.new_log(ctx.store.dir / "logs", f"{agent.strip('()')}-heavy")
    ctx.say(f"Running the heavy job now: {text}\nIts output goes to {log}")
    # One word is a shell line, several are started directly: either way the log names the program that runs.
    plan = shells.prepare(ctx, command[0] if len(command) == 1 else command, env, os.getcwd())
    with open(log, "wb") as out:
        out.write(plan.header(text))
        out.flush()
        try:
            proc = subprocess.Popen(
                plan.command,
                shell=len(command) == 1,
                cwd=os.getcwd(),
                env=env,
                stdin=subprocess.DEVNULL,
                stdout=out,
                stderr=subprocess.STDOUT,
                creationflags=hooks.NO_WINDOW,
            )
        except OSError as exc:
            out.write(f"Could not start it: {exc}\n".encode())
            ctx.say(f"The heavy job could not start: {exc}")
            return 127
        if mine is not None:
            with ctx.store.write() as db:
                db.execute("UPDATE heavy SET pid = ? WHERE id = ?", (proc.pid, mine))
        try:
            code = proc.wait()
        except BaseException:  # stopped (Ctrl+C): stop the job too
            proc.kill()
            proc.wait()
            raise
        out.write(f"\n(exit code {code})\n".encode())
    ctx.say(f"The heavy job ended with exit code {code}. Its output: {log}")
    return code
