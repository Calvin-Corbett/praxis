"""`praxis run --heavy <command...>`: a queue for jobs that eat the machine (whole test suites, browser runs).

At most `heavy_jobs` (praxis.toml, default 2) heavy jobs run at once for the project, whichever agent started them;
the rest wait in line and say so. Each runner keeps a small file locked with the operating system's file lock while
it lives, exactly like the landing line's tickets: when a runner dies (closed terminal, crash, power cut) the lock
is freed, and the next runner to look sees its row is dead and removes it, so a crashed job never holds a place.
Nobody's wait times out.

The order of the line (see `in_order`): a job started from inside a landing's check goes first, because a whole
batch of agents waits on that landing; then the other jobs, an agent with nothing running before an agent that
already has a job running. A running job is never stopped to make room. One agent may have only
`heavy_waiting_per_agent` jobs waiting, so it cannot fill the line.

Every waiting job decides by itself when it starts, with the rule of the Praxis that started it, and jobs of several
Praxis versions can wait in one line. The oldest ones start strictly in the order they joined. A job of this Praxis
therefore never starts before such a job that joined before it (see `rules`): if it did, the two would only race
for the next free place, and the line shown by `praxis status` would not be the order the jobs start in.
"""

from __future__ import annotations

import contextlib
import os
import subprocess
import time
import uuid
from collections import Counter
from pathlib import Path

from agent_praxis import agents, config, hooks, live, load, shells
from agent_praxis.errors import PraxisError
from agent_praxis.landing_queue import _try_lock, _unlock, held
from agent_praxis.store import Store

POLL_SECONDS = 0.5
TOKEN_ENV = "PRAXIS_HEAVY_TOKEN"  # set in a heavy job's environment: the job's own token, so a nested call knows
CHECK_ENV = "PRAXIS_CHECK"  # "1" in the environment of a landing's check command (landing._run_check)
LIVE_SETTINGS = ("heavy_jobs", "heavy_max", "heavy_min")  # read again from praxis.toml while a job waits

# The rule a waiting job starts by, which depends on the Praxis that started it (see rules):
JOINED = 0  # strictly in the order the jobs joined: every Praxis before database format 6
TURNS = 1  # a landing check's jobs first, then by agent in turns, whoever else waits: formats 6 and 7
PLACED = 2  # the same, and never before a JOINED job that joined earlier: this Praxis. Written into the job's file.


def _lock_file(store: Store, token: str) -> Path:
    return store.dir / "heavy" / f"{token}.lock"


def alive(store: Store, token: str) -> bool:
    """True while the runner that owns this row is still running (it keeps its lock file locked)."""
    return held(_lock_file(store, token))


def rows(store: Store) -> list:
    """Running and waiting heavy jobs, oldest first. Rows whose runner is gone are left out (and cleared by the
    next runner)."""
    return [r for r in store.rows("SELECT * FROM heavy ORDER BY id") if alive(store, r["token"])]


def is_check(job) -> bool:
    """True for a landing check's job. A project's database can stay at an older format while commands of an older
    Praxis are still running in it: its heavy table has no such column, and every job there is an ordinary one."""
    return "priority" in job.keys() and bool(job["priority"])  # noqa: SIM118 - a database row is not a dict


def rules(store: Store, jobs: list) -> dict:
    """{job id: JOINED, TURNS or PLACED} for *jobs*: the rule each one starts by. A job's lock file says it (PLACED),
    or says only the database format its Praxis knows, or says nothing at all (a Praxis from before the register)."""
    found = {}
    for job in jobs:
        facts = live.read_stamp(_lock_file(store, job["token"])) or {}
        if isinstance(facts.get("order"), int):
            found[job["id"]] = facts["order"]
        else:
            known = facts.get("format")
            found[job["id"]] = TURNS if isinstance(known, int) and known >= 6 else JOINED
    return found


def fixed(waiting: list, rule: dict | None) -> set:
    """The ids of the waiting jobs whose place in the line is fixed by when they joined: the JOINED ones. Every job
    before one of them starts before it, and every job after it starts after it.

    Not while a TURNS job waits as well: it takes no notice of a JOINED job and may be waiting for a job of this
    Praxis that joined after one, so holding that job back for the JOINED one could leave all three waiting for
    each other. Then the order is the one TURNS jobs go by, and a JOINED job only races the others for a place."""
    rule = rule or {}
    if any(rule.get(r["id"], PLACED) == TURNS for r in waiting):
        return set()
    return {r["id"] for r in waiting if rule.get(r["id"], PLACED) == JOINED}


def in_order(jobs: list, rule: dict | None = None) -> tuple[list, list]:
    """(the running jobs, the waiting jobs in the order they will start) for *jobs*, oldest first. *rule* is what
    rules() says about them; without it every job is taken for one of this Praxis.

    A landing check's jobs come first, in the order they joined. Then the others take turns by agent: each agent's
    jobs are numbered from the ones it has running (not counting a landing check's), so an agent with nothing
    running comes before an agent with one job running, which comes before an agent with two. Equal turns go by
    arrival. A job of an older Praxis that starts strictly in the order the jobs joined (see fixed) keeps exactly
    that place: the jobs that joined before it are put in order among themselves, then it, then those after it."""
    running = [r for r in jobs if r["started"]]
    waiting = [r for r in jobs if not r["started"]]
    turn = Counter(r["agent"] for r in running if not is_check(r))
    stay = fixed(waiting, rule)
    ordered, checks, later = [], [], []

    def close() -> None:
        later.sort(key=lambda item: item[:2])
        ordered.extend(checks + [r for _turn, _id, r in later])
        checks.clear()
        later.clear()

    for r in waiting:
        if r["id"] in stay:
            close()
            ordered.append(r)
        elif is_check(r):
            checks.append(r)
            continue
        else:
            later.append((turn[r["agent"]], r["id"], r))
        turn[r["agent"]] += 1
    close()
    return running, ordered


def _in_front(jobs: list, mine: int, rule: dict | None = None) -> list:
    """The jobs job *mine* waits for: every running one, and the waiting ones that start before it."""
    running, waiting = in_order(jobs, rule)
    ids = [r["id"] for r in waiting]
    return running + (waiting[: ids.index(mine)] if mine in ids else waiting)


def limit_now(ctx) -> tuple[int, str]:
    """(how many heavy jobs may run at once right now, and why). A number in praxis.toml is that number; "auto" is
    sized from the machine's load at this moment, with the running heavy jobs' own share of it measured and left
    out (see load.auto_limit)."""
    setting = ctx.cfg.heavy_jobs
    if setting != "auto":
        return max(1, int(setting)), f"heavy_jobs = {setting} in praxis.toml"
    pids = [r["pid"] for r in rows(ctx.store) if r["started"]]
    reading, shares = load.measure([pid for pid in pids if pid])
    return load.auto_limit([shares.get(pid) for pid in pids], reading, ctx.cfg.heavy_max, ctx.cfg.heavy_min)


def _forget_dead(store: Store, db, mine: int) -> None:
    for row in db.execute("SELECT * FROM heavy WHERE id != ?", (mine,)).fetchall():
        if not alive(store, row["token"]):
            db.execute("DELETE FROM heavy WHERE id = ?", (row["id"],))
            try:
                _lock_file(store, row["token"]).unlink()
            except OSError:
                pass


def _try_start(store: Store, mine: int, limit: int) -> tuple[bool, list]:
    """Start now if fewer than *limit* jobs run and no waiting job is in front. Else (False, the jobs in front)."""
    with store.write() as db:
        _forget_dead(store, db, mine)
        jobs = db.execute("SELECT * FROM heavy ORDER BY id").fetchall()
        ahead = _in_front(jobs, mine, rules(store, jobs))
        if len(ahead) < limit and all(r["started"] for r in ahead):
            db.execute("UPDATE heavy SET started = ? WHERE id = ?", (time.time(), mine))
            return True, []
        return False, ahead


def _join(ctx, db, agent: str, token: str, text: str, priority: bool) -> int:
    """Add the job to the line (inside the caller's database write) and give its row id. Refused when *agent*
    already has heavy_waiting_per_agent jobs waiting: a landing check's job never is, and neither is a job with no
    agent's name on it (started in the main folder, or the full check)."""
    cap = ctx.cfg.heavy_waiting_per_agent
    if cap > 0 and not priority and not agent.startswith("("):
        mine = db.execute("SELECT * FROM heavy WHERE agent = ? AND started IS NULL ORDER BY id", (agent,)).fetchall()
        waiting = [r for r in mine if not is_check(r) and alive(ctx.store, r["token"])]
        if len(waiting) >= cap:
            from agent_praxis.status import ago

            listed = "\n".join(f"  - {r['command']} (joined {ago(r['enqueued'])})" for r in waiting)
            raise PraxisError(
                f"{agent} already has {len(waiting)} heavy job{'s' if len(waiting) != 1 else ''} waiting in line, "
                f"and one agent may have at most {cap} waiting (heavy_waiting_per_agent in praxis.toml), so this "
                f"one was not added and nothing was run. Waiting now:\n{listed}\n"
                "Wait until one of them has started, or put what you need into fewer, larger jobs. `praxis status` "
                "shows the whole line."
            )
    row = db.execute(
        "INSERT INTO heavy (agent, token, command, enqueued, pid) VALUES (?, ?, ?, ?, ?)",
        (agent, token, text, time.time(), os.getpid()),  # this runner's pid until the job has its own
    ).lastrowid
    if priority and any(column["name"] == "priority" for column in db.execute("PRAGMA table_info(heavy)")):
        db.execute("UPDATE heavy SET priority = 1 WHERE id = ?", (row,))
    return row


def _goes_first(jobs: list, rule: dict, mine: int) -> str:
    """What a landing check's waiting job is told about its place: the truth, also with jobs of an older Praxis in
    the line."""
    waiting = [r for r in jobs if not r["started"]]
    older = [r for r in waiting if rule.get(r["id"], PLACED) == JOINED]
    if not older:
        return "This job belongs to a landing's check, so it goes ahead of the waiting jobs that do not. "
    if not fixed(waiting, rule):  # jobs of three Praxis generations wait: nobody holds a place for anybody (fixed)
        return (
            "This job belongs to a landing's check, so it goes ahead of the waiting jobs of this Praxis that do "
            f"not. {len(older)} waiting job(s) of an older Praxis may still start before it: that Praxis starts its "
            "jobs in the order they joined, whatever else waits. "
        )
    before = len([r for r in older if r["id"] < mine])
    return (
        "This job belongs to a landing's check, so it goes ahead of the waiting jobs of this Praxis that do not. "
        + (
            f"{before} job(s) of an older Praxis joined the line before it and start first: that Praxis starts its "
            "jobs strictly in the order they joined. "
            if before
            else ""
        )
    )


def _reread(ctx, seen):
    """Bring the limits a waiting job goes by up to date with praxis.toml in the main folder, so a change reaches
    the jobs already in line. The file is read only when it changed: *seen* is what this returned last time."""
    path = ctx.repo.root / config.FILE_NAME
    try:
        info = path.stat()
    except OSError:
        return seen
    stamp = (info.st_mtime_ns, info.st_size)
    if stamp == seen:
        return seen
    try:
        fresh = config.load(ctx.repo.root)
    except PraxisError as exc:
        ctx.say(f"praxis.toml changed but cannot be read, so the heavy job limit stays as it was. {exc}")
        return stamp
    changed = [name for name in LIVE_SETTINGS if getattr(fresh, name) != getattr(ctx.cfg, name)]
    for name in changed:
        setattr(ctx.cfg, name, getattr(fresh, name))
    if changed:
        now = ", ".join(f"{name} = {getattr(fresh, name)!r}".replace("'", '"') for name in changed)
        ctx.say(f"praxis.toml changed while this job waited. It now goes by: {now}")
    return stamp


def _inside_heavy_job(store: Store) -> bool:
    """True when this process runs inside a heavy job of this project: the token in the environment names a row that
    exists here and whose runner is still alive and running. A stale or foreign token is not inside anything."""
    token = os.environ.get(TOKEN_ENV)
    if not token:
        return False
    rows = store.rows("SELECT * FROM heavy WHERE token = ?", (token,))
    return bool(rows) and bool(rows[0]["started"]) and alive(store, token)


@contextlib.contextmanager
def place(ctx, agent: str, text: str, poll: float = POLL_SECONDS, wait: bool = True):
    """Hold a heavy job place for as long as the `with` block runs, waiting in line for it first. Gives
    (the place's row id, its token). Inside a heavy job of this project it takes no place and gives (None, the outer
    job's token): queueing there would hold one place while waiting for another, and two such jobs at the limit
    would wait for each other forever. With wait=False it never waits: when no place is free right now it gives
    (None, None), and the caller does without. Started from inside a landing's check (CHECK_ENV), it is a landing
    check's job: it goes ahead of the waiting jobs that are not."""
    store = ctx.store
    priority = os.environ.get(CHECK_ENV) == "1"
    if _inside_heavy_job(store):
        ctx.say("Already inside a heavy job: running this command directly, without taking another place.")
        yield None, os.environ[TOKEN_ENV]
        return
    token = uuid.uuid4().hex
    (store.dir / "heavy").mkdir(parents=True, exist_ok=True)
    handle = open(_lock_file(store, token), "a+b")  # noqa: SIM115 - locked BEFORE the row exists, so it never looks dead while alive
    mine = None
    try:
        # A live heavy job with nothing written in its file belongs to an older Praxis (live.py); `order`: see rules.
        store.stamp(handle, order=PLACED)
        if not _try_lock(handle):
            raise RuntimeError("could not lock a brand-new heavy job file")
        with store.write() as db:
            mine = _join(ctx, db, agent, token, text, priority)
        told = seen = None
        while True:
            jobs = rows(store)
            rule = rules(store, jobs)
            ahead, limit = _in_front(jobs, mine, rule), None
            if all(r["started"] for r in ahead):  # first in line: only now does the limit decide anything
                limit, why = limit_now(ctx)  # read outside the database lock: measuring the load takes a moment
                go, ahead = _try_start(store, mine, limit)
                if go:
                    break
            if not wait:
                yield None, None
                return
            if told != len(ahead):
                told = len(ahead)
                if limit is None:
                    limit, why = limit_now(ctx)
                first = _goes_first(jobs, rule, mine) if priority else ""
                ctx.say(
                    f"Waiting in line for a heavy job place: {len(ahead)} in front "
                    f"({', '.join(r['agent'] for r in ahead)}), at most {limit} run at once on this project "
                    f"({why}). {first}Praxis waits as long as it takes; this is normal."
                )
            time.sleep(poll)
            seen = _reread(ctx, seen)
        yield mine, token
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


def run(ctx, command: list[str], poll: float = POLL_SECONDS) -> int:
    """Wait for a free heavy place, run *command*, return its exit code."""
    if command and command[0] == "--":
        command = command[1:]
    if not command:
        raise PraxisError("Give the command to run, for example: praxis run --heavy pytest")
    agent = os.environ.get("PRAXIS_AGENT") or agents.agent_here(ctx) or "(nobody)"
    text = command[0] if len(command) == 1 else subprocess.list2cmdline(command)
    with place(ctx, agent, text, poll) as (mine, token):
        return _execute(ctx, command, text, agent, mine, token)


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
