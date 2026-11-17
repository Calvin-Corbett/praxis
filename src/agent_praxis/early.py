"""Early checks for the landing line (praxis.toml `speculate = true`, off by default).

While the agent at the front of the line (the head) runs its check, the next agent only waits. With `speculate` on,
the head starts that agent's check early, in a process of its own, on "the newest main version + the head's work +
the next agent's work", built in a scratch folder (`<agent folders>/_early`, a git worktree on no branch). If the head
lands, the next agent's turn builds exactly the same files and finds that they already passed: the remembered pass
(the check cache: same tree, same command; for a named check, the files it reads: see checks) is the ONLY hand-off.
Nothing is ever landed from the scratch folder, and only a pass is remembered. If the head's work does not land, the
head stops the early run (the process it started, by its process id) and nothing is remembered.

It is bounded. One early check at a time; only for the first agent waiting behind the head that is not in the head's
batch; and only when a heavy job place is free right now (it takes one and never waits for one).
"""

from __future__ import annotations

import dataclasses
import json
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

from agent_praxis import checkcache, gitops, heavy, hooks
from agent_praxis.agents import Ctx, _tree, folder_state
from agent_praxis.landing_queue import _unlock, held, read_note, ticket_alive

SCRATCH = "_early"  # an agent's name cannot start with "_", so no agent's folder ever has this name
POLL_SECONDS = 0.3
STILL_WAITING_SECONDS = 60


def _lock_path(store) -> Path:
    return store.dir / "early" / "running.lock"


def _state_path(store) -> Path:
    return store.dir / "early" / "state.json"


def _write_state(store, data: dict) -> None:
    """Replace the state file in one step. It is only a note for the agent whose check runs early (so it can wait
    for it): if it cannot be written, that agent simply runs its own check, so this never fails the run."""
    path = _state_path(store)
    temp = path.with_suffix(f".tmp{os.getpid()}")
    try:
        temp.write_text(json.dumps(data), encoding="utf-8")
    except OSError:
        return
    for _attempt in range(40):  # a reader may have the old file open for an instant (Windows refuses the swap then)
        try:
            os.replace(temp, path)
            return
        except OSError:
            time.sleep(0.05)
    try:
        temp.unlink()
    except OSError:
        pass


def active(store) -> dict | None:
    """The early check running right now ({"agent", "tree", "command", "started", ...}), or None. The process that
    runs it keeps the lock file locked, and writes what it is checking only once it holds the lock."""
    if not held(_lock_path(store)):
        return None
    try:
        data = json.loads(_state_path(store).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) and data.get("agent") else None


# --- the head's side -------------------------------------------------------------------------------------------------


class Line:
    """What the head holds on to: the early check it started for the next agent in line (at most one)."""

    def __init__(self, ctx: Ctx, row):
        self.ctx, self.row = ctx, row
        self.proc: subprocess.Popen | None = None

    def start(self, candidate: str, taken: list[str]) -> None:
        """The head is about to check *candidate* (its own work on top of main, with the agents *taken* in it). If
        somebody else waits behind it, start that agent's check early. Returns at once."""
        ctx = self.ctx
        if not ctx.cfg.speculate or self.proc is not None:
            return
        try:
            mine = ctx.store.row("SELECT MIN(id) AS n FROM queue WHERE agent = ?", (self.row["name"],))["n"]
            if mine is None:
                return
            behind = ctx.store.rows("SELECT agent FROM queue WHERE id > ? ORDER BY id", (mine,))
            if not any(q["agent"] not in taken for q in behind):
                return
            from agent_praxis import background

            log = hooks.new_log(ctx.store.dir / "logs", "early-check")
            args = [sys.executable, "-m", "agent_praxis", "early-check", "--candidate", candidate]
            args += ["--after", str(mine), "--taken", ",".join(taken)]
            with open(log, "wb") as out:
                self.proc, _independent = background._spawn(args, out, cwd=ctx.repo.root)
        except Exception as exc:  # noqa: BLE001 - an early check is only a head start: the landing never depends on it
            ctx.say(f"Note: the next agent's check could not be started early ({exc}). The landing carries on.")
            return
        ctx.say(f"Starting the next agent's check early, beside this one (speculate = true). Its output: {log}")

    def wait_for(self, tree: str, command: str, passed=None) -> float | None:
        """On this agent's own turn: when the check of exactly these files was started early and still runs, wait
        for it. Returns when the files passed, or None (then the turn runs the check itself)."""
        if not self.ctx.cfg.speculate:
            return None
        return wait_for(self.ctx, self.row, tree, command, passed=passed)

    def keep(self) -> None:
        """The work the early check was built on has landed: let it finish. Its pass is found in the check cache."""
        self.proc = None

    def stop(self) -> None:
        """The work the early check was built on did not land: stop the process this head started (and, with it, the
        check it runs). Only that process, by its id; a run that was kept, or already ended, is left alone."""
        proc, self.proc = self.proc, None
        if proc is None or proc.poll() is not None:
            return
        try:
            if os.name == "nt":
                proc.kill()  # the check is in a Windows job that ends with this process (see landing._CheckEndsWithMe)
            else:
                os.killpg(proc.pid, signal.SIGTERM)  # it leads a process group of its own: itself and what it started
        except OSError:
            pass
        try:
            proc.wait(timeout=30)
        except subprocess.TimeoutExpired:
            pass
        self.ctx.say("Stopped the early check of the next agent: the work it was built on did not land.")


# --- the next agent's side -------------------------------------------------------------------------------------------


def wait_for(ctx: Ctx, row, tree: str, command: str, poll: float = POLL_SECONDS, passed=None) -> float | None:
    """On an agent's own turn, when its files have no remembered pass yet: if an early check of exactly these files
    and this command is still running, wait for it rather than running the same check twice. Returns when the files
    passed (from the check cache), or None: then the agent's turn runs the check itself. *passed* answers that
    question another way (several named checks: *command* then names them all, see checks.signature)."""
    if passed is None:

        def passed() -> float | None:
            return checkcache.passed_at(ctx.store, tree, command)

    state = active(ctx.store)
    same = state and state.get("agent") == row["name"] and state.get("tree") == tree and state.get("command") == command
    if not same:
        return passed()  # it may have ended, and passed, a moment ago
    ctx.say(
        f"{row['name']}'s check was started early, while the landing before it was being checked. Waiting for that "
        "run to end rather than running the same check twice."
    )
    began = last = time.monotonic()
    while held(_lock_path(ctx.store)):
        time.sleep(poll)
        if time.monotonic() - last >= STILL_WAITING_SECONDS:
            last = time.monotonic()
            ctx.say(f"The early check is still running ({int((last - began) // 60)} min).")
    found = passed()
    if found is None:
        ctx.say("The early run did not pass, so the check runs here now and shows its result.")
    return found


# --- the early check itself (a process of its own) -------------------------------------------------------------------


def _next_in_line(ctx: Ctx, after: int, taken: list[str]):
    """(the agent's row, its saved work, where that work left main) for the first agent waiting behind the head that
    is not in the head's batch. None when there is none, or when that agent is not ready. Reads only."""
    root, main_ref = ctx.repo.root, f"refs/heads/{ctx.cfg.main_branch}"
    for q in ctx.store.rows("SELECT * FROM queue WHERE id > ? ORDER BY id", (after,)):
        note = read_note(ctx.store, q["token"])
        if q["agent"] in taken or not ticket_alive(ctx.store, q["token"]) or (note and note.get("done")):
            continue
        row = ctx.store.agent(q["agent"])
        if row is None or row["paused"]:
            return None
        folder = Path(row["worktree"])
        if folder_state(ctx, row) != "ok" or gitops.operation_in_progress(folder):
            return None
        if gitops.current_branch(folder) != row["branch"]:
            return None
        tip = gitops.rev(root, f"refs/heads/{row['branch']}")
        if not tip or gitops.commits_ahead(root, main_ref, tip) == 0:
            return None
        fork = gitops.git(["merge-base", tip, main_ref], root, check=False).stdout.strip()
        merges = gitops.git(["rev-list", "--merges", "-n1", f"{fork}..{tip}"], root, check=False).stdout.strip()
        if not fork or merges:
            return None  # a merge commit cannot be replayed one commit at a time
        return row, tip, fork
    return None


def run_child(ctx: Ctx, candidate: str, after: int, taken: list[str]) -> int:
    """The early check's own process. Always ends with 0: whatever happens here, the landing line carries on as it
    would without it, and the only thing it ever leaves behind is a remembered pass."""
    from agent_praxis import fullcheck

    store = ctx.store
    os.environ.pop(heavy.TOKEN_ENV, None)  # started inside a heavy job: this run takes its own place, or none
    handle = fullcheck.take_lock(_lock_path(store))
    if handle is None:
        ctx.say("Another early check is still running, so this one is not started.")
        return 0
    try:
        _write_state(store, {})
        found = _next_in_line(ctx, after, taken)
        if found is None:
            ctx.say("Nobody waiting behind the head is ready for an early check.")
            return 0
        _check_early(ctx, candidate, *found)
        return 0
    finally:
        _write_state(store, {})
        _unlock(handle)
        handle.close()


def _check_early(ctx: Ctx, candidate: str, row, tip: str, fork: str) -> None:
    from agent_praxis import checks, fullcheck

    store, name = ctx.store, row["name"]
    folder = fullcheck._scratch(ctx, candidate, SCRATCH)
    fullcheck._put_files(folder, candidate)
    args = [*gitops.identity_args(folder), "rebase", "-q", "--onto", candidate, fork, tip]
    if gitops.git(args, folder, check=False).returncode != 0:
        gitops.abort_operation(folder)
        ctx.say(
            f"{name}'s work does not fit on top of the work that is landing now, so its check is not started early."
        )
        return
    checked, tree = gitops.rev(folder, "HEAD"), _tree(folder, "HEAD")
    changed = gitops.changed_between(folder, candidate, "HEAD")  # on top of the work that is landing now
    plan = checks.plan(ctx.cfg, changed)  # the checks its own turn will choose
    command = checks.signature(plan)
    if not plan or not tree or checked == candidate:
        ctx.say(f"{name} has nothing to check early.")
        return
    if checks.look(ctx, folder, checked, plan).ok:
        ctx.say(f"{name}'s files on top of the work that is landing now already passed this command.")
        return
    with heavy.place(ctx, f"{name} (early check)", command, wait=False) as (mine, token):
        if mine is None:
            ctx.say("No heavy job place is free right now, so no check is started early.")
            return
        state = {"agent": name, "tree": tree, "command": command, "candidate": candidate}
        _write_state(store, {**state, "started": time.time(), "pid": os.getpid()})
        ctx.say(
            f"Running {name}'s check early, on top of the work that is landing now (commit {candidate[:10]}): "
            f"{checks.describe(plan)}"
        )
        cfg = dataclasses.replace(ctx.cfg, env={**ctx.cfg.env, heavy.TOKEN_ENV: token})
        inner = dataclasses.replace(ctx, cfg=cfg)
        # The only hand-off: a pass is remembered (same tree and command; a named check: the files it reads).
        outcome = checks.run(inner, row, folder, checked, plan, base=candidate, changed=changed)
        bad, log = outcome.failed, outcome.log
        if bad is not None:
            ctx.say(
                f"It did not pass (exit code {bad.code}). Nothing is remembered: {name}'s own turn runs the check. "
                f"Output: {log}"
            )
            return
        if outcome.moved:
            ctx.say("Files changed in the scratch folder while the check ran, so the pass is not remembered.")
            return
        ctx.say(f"It passed. When {name}'s turn builds the same files, the check is not run again. Output: {log}")
