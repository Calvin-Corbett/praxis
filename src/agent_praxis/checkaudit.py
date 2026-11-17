"""The audit: the net that makes an incomplete `inputs` list visible.

A named check with `inputs` is skipped when it already passed on the same files it reads (checks.py). That is only as
true as the `inputs` list: if the check really reads a file the list leaves out, a pass can be taken from memory for a
version the check would fail on. So about one in `audit_every` passes taken from memory (the first one, then every
N-th) is run again anyway, on exactly the version that landed with it, in a scratch folder of its own
(`<agent folders>/_audit`, the way the full check runs: a git worktree on no branch, a heavy job place, a process of
its own). It starts after the landing and never holds it up.

If the check fails there, `praxis status` says so under NEEDS YOU, with the log. That check's remembered passes are
dropped, and it is neither remembered nor skipped again (it simply runs every time) until `praxis trust <name>`.
Praxis never undoes the landing that used the pass.

The count and the verdict live in the project database (`checktrust`, one row for each named check). A running audit
keeps `.git/praxis/audit/running.lock` locked: one audit at a time, and one that could not start stays due.
"""

from __future__ import annotations

import dataclasses
import os
import sys
import time
from pathlib import Path

from agent_praxis import checks, gitops, heavy, hooks
from agent_praxis.agents import Ctx
from agent_praxis.errors import PraxisError
from agent_praxis.landing_queue import _unlock, held

SCRATCH = "_audit"  # an agent's name cannot start with "_", so no agent's folder ever has this name
WHO = {"name": "audit", "task": "a check that passed from memory, run again"}  # what the command sees as PRAXIS_AGENT
NEEDS_THE_HUMAN = 3  # the command's exit code for "a tool or service I need is down": no verdict on the check


def _lock_path(store) -> Path:
    return store.dir / "audit" / "running.lock"


def running(store) -> bool:
    """True while some process is running an audit for this project (it keeps the lock file locked)."""
    return held(_lock_path(store))


# --- after a landing -------------------------------------------------------------------------------------------------


def after_landing(ctx: Ctx, who: str, base: str, commit: str, used: list) -> None:
    """*who*'s landing moved main from *base* to *commit*, and the named checks *used* were skipped for it because
    of a remembered pass. Count that, and start the audit in the background for the checks whose turn has come.
    Never fails the landing."""
    try:
        due = _count(ctx.store, [check.name for check in used], ctx.cfg.audit_every)
        if not due or running(ctx.store):
            return  # an audit that is still running: these stay due, and the next pass from memory starts theirs
        log = start_background(ctx, who, base, commit, due)
    except Exception as exc:  # noqa: BLE001 - the landing is done and stays done, whatever happens to the net
        ctx.say(
            f"Warning: Praxis could not start the audit of a check that passed from memory ({exc}). The landing "
            "itself is done and stays done."
        )
        return
    ctx.say(
        f"The audit (audit_every = {ctx.cfg.audit_every}): {_listed(due)} passed from memory, and Praxis now "
        f"runs {'it' if len(due) == 1 else 'them'} again in the background, in a scratch folder, to make sure. "
        f"`praxis status` says so if it fails. Its output: {log}"
    )


def _listed(names: list[str]) -> str:
    return ("check " if len(names) == 1 else "checks ") + ", ".join(names)


def _count(store, names: list[str], every: int) -> list[str]:
    """Add one pass taken from memory to each named check's count. Returns the checks whose audit is due: the first
    such pass, then one in every *every* (0: never)."""
    due = []
    with store.write() as db:
        for name in names:
            db.execute("INSERT OR IGNORE INTO checktrust (name) VALUES (?)", (name,))
            db.execute("UPDATE checktrust SET reuses = reuses + 1 WHERE name = ?", (name,))
            row = db.execute("SELECT reuses, audited FROM checktrust WHERE name = ?", (name,)).fetchone()
            if every > 0 and (row["audited"] is None or row["reuses"] - row["audited"] >= every):
                due.append(name)
    return due


def start_background(ctx: Ctx, who: str, base: str, commit: str, names: list[str]) -> Path:
    """Start the audit in a process of its own (no window, nothing tying it to this one), from the main folder so
    it never keeps an agent's folder in use. Returns the log its output goes to."""
    from agent_praxis import background

    log = hooks.new_log(ctx.store.dir / "logs", "audit-background")
    args = [
        sys.executable,
        "-m",
        "agent_praxis",
        "audit-check",
        "--base",
        base,
        "--commit",
        commit,
        "--who",
        who,
        "--names",
        ",".join(names),
    ]
    with open(log, "wb") as out:
        background._spawn(args, out, cwd=ctx.repo.root)
    return log


# --- the audit itself (a process of its own) -------------------------------------------------------------------------


def run_child(ctx: Ctx, base: str, commit: str, who: str, names: list[str]) -> int:
    """The audit's own process. Always ends with 0: its verdict goes into the database, for `praxis status`."""
    from agent_praxis import fullcheck

    os.environ.pop(heavy.TOKEN_ENV, None)  # started by a landing inside a heavy job: this run outlives that job
    handle = fullcheck.take_lock(_lock_path(ctx.store))
    if handle is None:
        ctx.say("Another audit is still running, so this one is not started. These checks stay due.")
        return 0
    try:
        for name in names:
            _audit(ctx, base, commit, who, name)
        return 0
    finally:
        _unlock(handle)
        handle.close()


def _audit(ctx: Ctx, base: str, commit: str, who: str, name: str) -> None:
    """Run the named check the way that landing's turn would have run it, had nothing been remembered: on the
    version that landed, and told the same (the main version before it, and the files the landing changed)."""
    from agent_praxis import fullcheck
    from agent_praxis import landing as L

    store = ctx.store
    check = checks.by_name(ctx.cfg, name)
    if check is None or check.inputs is None:
        ctx.say(f"Check {name} is no longer a named check with `inputs` in praxis.toml: nothing to audit.")
        return
    if not checks.trusted(store, check.name):
        ctx.say(f"Check {name}'s memory is not trusted already (see `praxis status`): nothing to audit.")
        return
    with store.write() as db:  # counted from here: the next audit of this check is `audit_every` such passes away
        db.execute("UPDATE checktrust SET audited = reuses WHERE name = ?", (name,))
    with heavy.place(ctx, f"(audit of check {name})", check.command) as (_mine, token):
        folder = fullcheck._scratch(ctx, commit, SCRATCH)
        fullcheck._put_files(folder, commit)
        ctx.say(
            f"Check {name} passed from memory for {who}'s landing (commit {commit[:10]}). Running it again on "
            f"exactly that version, in {folder}: {check.command}"
        )
        cfg = dataclasses.replace(ctx.cfg, env={**ctx.cfg.env, "PRAXIS_COMMIT": commit, heavy.TOKEN_ENV: token})
        changed = gitops.changed_between(ctx.repo.root, base, commit)
        inner = dataclasses.replace(ctx, cfg=cfg)
        code, _output, log = L._run_check(inner, WHO, folder, check.command, base, changed)
    if code == 0:
        _verdict(store, name, "passed", log)
        ctx.say(f"It passed there too: check {name}'s memory was right. Its output: {log}")
    elif code == NEEDS_THE_HUMAN:
        _verdict(store, name, "unknown", log)
        ctx.say(
            f"It ended with exit code {NEEDS_THE_HUMAN} (a tool or service it needs is down), which says nothing "
            f"about check {name}'s memory. Its output: {log}"
        )
    else:
        with store.write() as db:
            db.execute(
                "UPDATE checktrust SET audit_at = ?, audit_result = 'failed', audit_log = ?, broken_at = ?, "
                "broken_log = ?, broken_commit = ?, broken_agent = ? WHERE name = ?",
                (time.time(), log, time.time(), log, commit, who, name),
            )
            checks.forget(db, name)
        for line in describe(store.row("SELECT * FROM checktrust WHERE name = ?", (name,))):
            ctx.say(line)


def _verdict(store, name: str, result: str, log: str) -> None:
    with store.write() as db:
        db.execute(
            "UPDATE checktrust SET audit_at = ?, audit_result = ?, audit_log = ? WHERE name = ?",
            (time.time(), result, log, name),
        )


# --- saying it, and `praxis trust` -----------------------------------------------------------------------------------


def broken(store) -> list:
    """The named checks whose memory an audit found wrong, and that nobody has trusted again yet."""
    return store.rows("SELECT * FROM checktrust WHERE broken_at IS NOT NULL ORDER BY broken_at")


def describe(row, found: str = "") -> list[str]:
    """A check whose audit failed, in plain words, one line per item: for the audit's output and `praxis status`."""
    name = row["name"]
    return [
        f"check {name} passed from memory but fails when run: its inputs list is probably missing files"
        + (f" (found {found})." if found else "."),
        (
            f"{row['broken_agent']}'s landing (commit {(row['broken_commit'] or '')[:10]}) skipped it because it "
            "had passed on the same files it says it reads. Run again on exactly that version, it fails. Its "
            f"output: {row['broken_log']}"
        ),
        (
            "Its remembered passes were dropped, and it now runs for every landing it is chosen for. Add the files "
            f"it reads to its `inputs` in praxis.toml (or fix what the log shows), then run: praxis trust {name}"
        ),
        "Praxis never undoes a landing by itself: if that landing broke the project, have an agent land a fix.",
    ]


def trust(ctx: Ctx, name: str) -> None:
    """`praxis trust <name>`: the check's passes are remembered and reused again, starting from none."""
    check = checks.by_name(ctx.cfg, name)
    if check is None:
        known = ", ".join(checks.names(ctx.cfg)) or "none"
        raise PraxisError(f"praxis.toml has no check named {name} (named checks: {known}). Nothing was changed.")
    row = ctx.store.row("SELECT broken_at FROM checktrust WHERE name = ?", (check.name,))
    if row is None or row["broken_at"] is None:
        ctx.say(f"Check {check.name} is trusted already: its passes are remembered. Nothing was changed.")
        return
    with ctx.store.write() as db:
        db.execute(
            "UPDATE checktrust SET broken_at = NULL, broken_log = NULL, broken_commit = NULL, "
            "broken_agent = NULL, reuses = 0, audited = NULL WHERE name = ?",
            (check.name,),
        )
        checks.forget(db, check.name)
    ctx.say(
        f"Check {check.name} is trusted again. Its passes are remembered from now on, starting from none, and "
        "the first one taken from memory is run again to make sure (the audit)."
    )
