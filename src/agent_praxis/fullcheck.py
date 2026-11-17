"""The full check: the safety net behind quick, chosen-by-path checks.

praxis.toml's `full_check` is the command that proves the WHOLE project works (the complete test suite, say).
`praxis fullcheck` runs it now, and `full_check_every = N` starts it in the background after every N landings that did
not run that exact command themselves. It always runs on the main version in a scratch folder of its own
(`<agent folders>/_fullcheck`, a git worktree on no branch), never in the main folder and never inside the landing
line, so landings carry on while it runs.

One failed run is not proof (a busy PC fails it too), so a failure counts only when the same commit fails a second
time (`full_check_confirm`, on by default). Then Praxis looks for the landing behind it: the landings since the last
pass are searched the way `git bisect` searches commits, by the same rule (fullsearch.py). What the runs showed is
recorded and `praxis status` says exactly that (fullwords.py): under NEEDS YOU the landing it starts to fail at, the
commit, the logs and the exact `git revert` command, or that it failed and then passed and nobody is named. Praxis
never undoes a landing itself. Every run of the command takes a heavy job place of its own and gives it back when
it ends, so the other heavy jobs get their turn between two runs.

Every run is a row in the project database (`fullchecks`, database format 5). A running full check keeps
`.git/praxis/fullcheck/running.lock` locked. A run whose process died leaves its row without a result and the lock
free: the next landing sees that, marks the row interrupted and starts the full check again.
"""

from __future__ import annotations

import dataclasses
import os
import sys
import time
from pathlib import Path

from agent_praxis import fullsearch, gitops, heavy, hooks
from agent_praxis.agents import PARALLEL_CHECKOUT, Ctx
from agent_praxis.errors import PraxisError
from agent_praxis.fullwords import OWN_CHECK, describe, short
from agent_praxis.landing_queue import _try_lock, _unlock, held

SCRATCH = "_fullcheck"  # an agent's name cannot start with "_", so no agent's folder ever has this name
WHO = {"name": "full-check", "task": "the full check of the main version"}  # what the command sees as PRAXIS_AGENT
HEAVY_NAME = "(full check)"
ASKED = "you asked for it (praxis fullcheck)"
POLL_SECONDS = 0.5
JUDGED = ("passed", "failed")  # results that say something about the project (the others: the run did not finish)
KEEP = 500  # the newest runs kept in the database


def command(cfg) -> str:
    return cfg.full_check.strip()


def scratch_folder(ctx: Ctx, name: str = SCRATCH) -> Path:
    return ctx.cfg.worktree_dir(ctx.repo.root) / name


def _lock_path(store) -> Path:
    return store.dir / "fullcheck" / "running.lock"


def running(store) -> bool:
    """True while some process is running the full check for this project (it keeps the lock file locked)."""
    return held(_lock_path(store))


def take_lock(path: Path, tries: int = 5):
    """Open the file at *path* and lock it for this process: the open file, or None when another process keeps it
    locked. A process that only looks whether it is locked (`held`) locks it for an instant too, so a refusal is
    tried again a few times before it counts."""
    path.parent.mkdir(parents=True, exist_ok=True)
    for attempt in range(tries):
        handle = open(path, "a+b")  # noqa: SIM115 - the caller keeps it open, and locked, for its whole run
        if _try_lock(handle):
            return handle
        handle.close()
        if attempt + 1 < tries:
            time.sleep(0.1)
    return None


def newest(store, cmd: str, results: tuple[str, ...] | None = None):
    """The newest run of exactly this command, or the newest with one of *results*. None when there is none."""
    if results is None:
        return store.row("SELECT * FROM fullchecks WHERE command = ? ORDER BY id DESC LIMIT 1", (cmd,))
    for row in store.rows("SELECT * FROM fullchecks WHERE command = ? ORDER BY id DESC LIMIT 200", (cmd,)):
        if row["result"] in results:
            return row
    return None


def in_progress(store):
    """The run going on right now, or None."""
    row = store.row("SELECT * FROM fullchecks WHERE result IS NULL ORDER BY id DESC LIMIT 1")
    return row if row is not None and running(store) else None


def _newest_landing(store) -> int:
    return int(store.row("SELECT COALESCE(MAX(id), 0) AS n FROM landings")["n"])


def landings_since(store, cmd: str) -> int:
    """How many landings no full check has judged yet: those after the newest run that passed or failed."""
    mark = 0
    for result in JUDGED:
        row = store.row(
            "SELECT COALESCE(MAX(landing_id), 0) AS n FROM fullchecks WHERE command = ? AND result = ?", (cmd, result)
        )
        mark = max(mark, int(row["n"]))
    row = store.row("SELECT COUNT(*) AS n FROM landings WHERE ok = 1 AND kind = 'landed' AND id > ?", (mark,))
    return int(row["n"])


def _new_run(db, tip: str, cmd: str, why: str, covered: int, result: str | None = None, note: str | None = None) -> int:
    """Add a run's row (finished already when *result* is given) and forget all but the newest KEEP."""
    now = time.time()
    run_id = db.execute(
        "INSERT INTO fullchecks (commit_id, command, why, landing_id, pid, started, ended, result, note) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (tip, cmd, why, covered, os.getpid(), now, now if result else None, result, note),
    ).lastrowid
    db.execute("DELETE FROM fullchecks WHERE id NOT IN (SELECT id FROM fullchecks ORDER BY id DESC LIMIT ?)", (KEEP,))
    return run_id


def _close_interrupted(store) -> None:
    """A run with no result whose process is gone was interrupted: say so in its row. Checked inside a write, so a
    run that is just starting (it takes the lock first, then writes its row) is never mistaken for a dead one."""
    with store.write() as db:
        if running(store):
            return
        db.execute(
            "UPDATE fullchecks SET result = 'interrupted', ended = ?, note = ? WHERE result IS NULL",
            (time.time(), "its process ended before the full check finished"),
        )


def _finish(
    store,
    run_id: int,
    result: str,
    log: str | None = None,
    note: str | None = None,
    steps: str | None = None,
    good_commit: str | None = None,
    culprit_agent: str | None = None,
    culprit_task: str | None = None,
    culprit_commit: str | None = None,
    culprit_log: str | None = None,
    undo: str | None = None,
) -> None:
    """Write a run's result into its row. The note of a run with a culprit is the log of the run that fails there."""
    with store.write() as db:
        db.execute(
            "UPDATE fullchecks SET result = ?, ended = ?, log = COALESCE(?, log), note = ?, "
            "steps = COALESCE(?, steps), good_commit = ?, culprit_agent = ?, culprit_task = ?, culprit_commit = ?, "
            "undo = ? WHERE id = ?",
            (
                result,
                time.time(),
                log,
                culprit_log if culprit_commit else note,
                steps,
                good_commit,
                culprit_agent,
                culprit_task,
                culprit_commit,
                undo,
                run_id,
            ),
        )


# --- after each landing ----------------------------------------------------------------------------------------------


def after_landing(ctx: Ctx, used: str, tip: str) -> None:
    """Called at the end of a landing's turn. *used* is the check command that landing ran (or reused a pass of) and
    *tip* the commit it landed. Records a pass when that command was the full check itself; otherwise starts the full
    check in the background when it is due, or when the last one was interrupted."""
    if not command(ctx.cfg):
        return
    try:
        _after_landing(ctx, used.strip(), tip)
    except Exception as exc:  # noqa: BLE001 - the landing is done and stays done, whatever happens to the safety net
        ctx.say(
            f"Warning: Praxis could not start the full check ({exc}). The landing itself is done and stays done. "
            "Run it yourself with: praxis fullcheck"
        )


def _after_landing(ctx: Ctx, used: str, tip: str) -> None:
    store, cmd = ctx.store, command(ctx.cfg)
    if used == cmd:  # the files that landed passed the full check command: main is known good here
        covered = _newest_landing(store)
        with store.write() as db:
            _new_run(db, tip, cmd, OWN_CHECK, covered, "passed", "the landing's check was this command")
        return
    if running(store):
        return
    _close_interrupted(store)
    last = newest(store, cmd)
    every, since = ctx.cfg.full_check_every, landings_since(store, cmd)
    if last is not None and last["result"] == "interrupted":
        why = "the full check before it was interrupted"
    elif every > 0 and since >= every:
        why = f"{since} landing(s) since the last one (full_check_every = {every})"
    else:
        return
    log = start_background(ctx, why)
    ctx.say(f"Started the full check in the background: {why}. `praxis status` shows how it goes. Its output: {log}")


def start_background(ctx: Ctx, why: str) -> Path:
    """Start `praxis fullcheck` in a process of its own (no window, nothing tying it to this one), from the main
    folder so it never keeps an agent's folder in use. Returns the log its output goes to."""
    from agent_praxis import background

    log = hooks.new_log(ctx.store.dir / "logs", "full-check-background")
    args = [sys.executable, "-m", "agent_praxis", "fullcheck", "--in-background", "--why", why]
    with open(log, "wb") as out:
        background._spawn(args, out, cwd=ctx.repo.root)
    return log


# --- `praxis fullcheck` ----------------------------------------------------------------------------------------------


def command_line(ctx: Ctx, background: bool = False, child: bool = False, why: str = "") -> int:
    cmd = command(ctx.cfg)
    if not cmd:
        if child:
            return 0
        raise PraxisError(
            'No full check is set. Add full_check = "<the command that tests the whole project>" to praxis.toml '
            "(and full_check_every = N to run it after every N landings). Nothing was run."
        )
    if child:
        os.environ.pop(heavy.TOKEN_ENV, None)  # started by a landing inside a heavy job: this run outlives that job
        return run(ctx, why or ASKED, auto=True)
    if background:
        now = in_progress(ctx.store)
        if now is not None:
            ctx.say(f"A full check is already running (on commit {short(now['commit_id'])}). `praxis status` shows it.")
            return 0
        log = start_background(ctx, ASKED)
        ctx.say(f"Started the full check in the background. `praxis status` shows how it goes. Its output: {log}")
        return 0
    return run(ctx, ASKED)


def run(ctx: Ctx, why: str = ASKED, auto: bool = False) -> int:
    """Run the full check on the main version now. 0 when it passed (also when it failed first and passed when it
    was run again), 2 when it did not. With *auto* (the background process): give way to a run already going, and
    run again while landings that are due keep arriving."""
    store, cmd = ctx.store, command(ctx.cfg)
    while True:
        handle = take_lock(_lock_path(store))
        if handle is not None:
            break
        if auto:
            ctx.say("A full check is already running, so this one is not needed.")
            return 0
        code = _follow(ctx, cmd)
        if code is not None:
            return code
    try:
        _close_interrupted(store)
        while True:
            code, result = _once(ctx, cmd, why)
            every = ctx.cfg.full_check_every
            due = landings_since(store, cmd) if auto and every > 0 and result in JUDGED else 0
            if due < max(every, 1):
                return code
            why = f"{due} landing(s) arrived while the last one ran (full_check_every = {every})"
            ctx.say("")
            ctx.say(f"Running the full check again: {why}.")
    finally:
        _unlock(handle)
        handle.close()


def _follow(ctx: Ctx, cmd: str) -> int | None:
    """Another process is running the full check: wait for it, then give its result when it judged the newest main
    version. None when the caller should run its own (main moved on, or that run did not finish)."""
    now = in_progress(ctx.store)
    ctx.say(
        "A full check is already running"
        + (f" (on commit {short(now['commit_id'])}: {now['why']})" if now else "")
        + f". Waiting for it to end (Praxis waits as long as it takes). Its output is in {ctx.store.dir / 'logs'}"
    )
    while running(ctx.store):
        time.sleep(POLL_SECONDS)
    last = newest(ctx.store, cmd)
    tip = gitops.rev(ctx.repo.root, f"refs/heads/{ctx.cfg.main_branch}")
    if last is not None and last["result"] in JUDGED and last["commit_id"] == tip:
        for line in describe(ctx, last):
            ctx.say(line)
        return 0 if last["result"] == "passed" else 2
    return None


def _once(ctx: Ctx, cmd: str, why: str) -> tuple[int, str]:
    """One full check of the main version as it is now: (exit code, the result recorded)."""
    store, main = ctx.store, ctx.cfg.main_branch
    covered = _newest_landing(store)  # read BEFORE the commit: a landing in between is then simply judged again
    tip = gitops.rev(ctx.repo.root, f"refs/heads/{main}")
    if not tip:
        raise PraxisError(f"The main branch {main!r} does not exist, so there is nothing to check.")
    with store.write() as db:
        run_id = _new_run(db, tip, cmd, why, covered)
    one = _Run(ctx, cmd, run_id, tip)
    try:
        return one.go()
    except KeyboardInterrupt:
        _finish(store, run_id, "stopped", note="stopped by hand before it finished")
        raise
    except BaseException as exc:
        _finish(store, run_id, "unknown", note="Praxis could not run it: " + " ".join(str(exc).split())[:600])
        raise


def _scratch(ctx: Ctx, commit: str, name: str = SCRATCH) -> Path:
    """The scratch folder *name*, made on first use and kept (so what the project's tools cache in it is kept too).
    It is a git worktree on no branch: nothing in it is ever landed, and every run puts the files of the commit it
    checks there. `share` folders are linked and `setup` is run in it once, as for a new agent folder."""
    root, folder = ctx.repo.root, scratch_folder(ctx, name)
    ready = ctx.store.dir / "fullcheck" / f"{name}-setup.txt"
    if not (folder.is_dir() and gitops.is_worktree_of(folder, ctx.repo.common)):
        if folder.exists() and (not folder.is_dir() or any(folder.iterdir())):
            raise PraxisError(
                f"Praxis keeps a scratch folder of its own at {folder}, but something that is not that folder is "
                "there. Move or delete it, then try again. Nothing was run."
            )
        folder.parent.mkdir(parents=True, exist_ok=True)
        ready.parent.mkdir(parents=True, exist_ok=True)
        try:
            ready.unlink()
        except OSError:
            pass
        gitops.git([*PARALLEL_CHECKOUT, "worktree", "add", "--force", "--detach", str(folder), commit], root)
        if ctx.cfg.share:
            hooks.link_shares(ctx, folder)
    setup = ctx.cfg.setup.strip()
    try:
        done = ready.read_text(encoding="utf-8")
    except OSError:
        done = None
    if setup and done != setup:
        _put_files(folder, commit)
        log = hooks.new_log(ctx.store.dir / "logs", "full-check-setup")
        ctx.say(f"Running the setup command in Praxis's scratch folder {folder}: {setup}")
        env = hooks.command_env(ctx.cfg, PRAXIS_AGENT=WHO["name"], PRAXIS_TASK=WHO["task"])
        code = hooks.run_logged(setup, folder, env, log, ctx)
        if code != 0:
            raise PraxisError(
                f"The setup command failed in Praxis's scratch folder {folder} (exit code {code}), so the check "
                f"did not run there. The end of its output:\n{hooks.tail(log)}\nFull output: {log}"
            )
    if done != setup:
        ready.parent.mkdir(parents=True, exist_ok=True)
        ready.write_text(setup, encoding="utf-8")
    return folder


def _put_files(folder: Path, commit: str) -> None:
    """Make the scratch folder hold exactly the files of *commit*. Only the process that holds the running lock
    uses the folder, so what a run that was killed left there is stale: a half-done git step is given up, and a git
    lock file is removed and the step tried once more."""
    gitops.abort_operation(folder)
    args = [*PARALLEL_CHECKOUT, "checkout", "-q", "-f", "--detach", commit]
    if gitops.git(args, folder, check=False).returncode == 0:
        return
    for lock in gitops.git_paths(folder, ["index.lock"]):
        try:
            lock.unlink()
        except OSError:
            pass
    gitops.git(args, folder)


class _Run:
    """One full check of one main version: the runs of the command, each in a heavy job place of its own. Which
    runs are made, and what they prove, is fullsearch's business."""

    def __init__(self, ctx: Ctx, cmd: str, run_id: int, tip: str):
        self.ctx, self.cmd, self.id, self.tip = ctx, cmd, run_id, tip
        self.scratch: Path | None = None

    def check(self, commit: str) -> tuple[int, str, str]:
        """Wait for a heavy job place, put the files of *commit* into the scratch folder and run the command there:
        (exit code, output, log). The place is given back when the command ends."""
        from agent_praxis import landing as L

        with heavy.place(self.ctx, HEAVY_NAME, self.cmd) as (_mine, token):
            if self.scratch is None:
                self.scratch = _scratch(self.ctx, self.tip)
            _put_files(self.scratch, commit)
            extra = {"PRAXIS_COMMIT": commit}
            if token:
                extra[heavy.TOKEN_ENV] = token  # a `praxis run --heavy` inside the command runs in this job's place
            cfg = dataclasses.replace(self.ctx.cfg, env={**self.ctx.cfg.env, **extra})
            code, output, log = L._run_check(dataclasses.replace(self.ctx, cfg=cfg), WHO, self.scratch, self.cmd)
        with self.ctx.store.write() as db:
            db.execute("UPDATE fullchecks SET log = COALESCE(log, ?) WHERE id = ?", (log, self.id))
        return code, output, log

    def go(self) -> tuple[int, str]:
        ctx, tip = self.ctx, self.tip
        ctx.say(f"Running the full check on {ctx.cfg.main_branch} (commit {short(tip)}): {self.cmd}")
        ctx.say(f"It runs in a scratch folder of its own ({scratch_folder(ctx)}). Landings carry on meanwhile.")
        search = fullsearch.Search(self, lambda: newest(ctx.store, self.cmd, ("passed",)))
        verdict = search.judge({"agent": None, "task": None, "commit": tip}, run_it=True)
        found: dict = {}
        if verdict is False:
            answer = search.find()
            verdict, found = (True, {}) if answer is None else (False, answer)  # None: the failure did not come back
        last_log = search.seen[tip]["log"]
        if verdict is None:
            result = "unknown"
            _finish(ctx.store, self.id, result, log=last_log, note=search.unknown_note(), steps=search.steps())
        elif verdict:
            result = "passed"
            _finish(ctx.store, self.id, result, log=last_log, steps=search.steps())
        else:
            result = "failed"  # its log stays the first run's, the one that failed
            _finish(ctx.store, self.id, result, steps=search.steps(), **found)
        row = ctx.store.row("SELECT * FROM fullchecks WHERE id = ?", (self.id,))
        for line in describe(ctx, row):
            ctx.say(line)
        return (0 if result == "passed" else 2), result


# --- saying it -------------------------------------------------------------------------------------------------------


def failing(ctx: Ctx):
    """The run that says the main version needs the human (the newest finished run, when it did not pass), or None."""
    cmd = command(ctx.cfg)
    if not cmd:
        return None
    last = newest(ctx.store, cmd, (*JUDGED, "unknown"))
    return last if last is not None and last["result"] != "passed" else None
