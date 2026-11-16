"""The full check: the safety net behind quick, chosen-by-path checks.

praxis.toml's `full_check` is the command that proves the WHOLE project works (the complete test suite, say).
`praxis fullcheck` runs it now, and `full_check_every = N` starts it in the background after every N landings that did
not run that exact command themselves. It always runs on the main version in a scratch folder of its own
(`<agent folders>/_fullcheck`, a git worktree on no branch), never in the main folder and never inside the landing
line, so landings carry on while it runs. It takes a heavy job place like any other heavy job.

When it fails, Praxis looks for the landing that broke it. The landings since the last pass are searched the way
`git bisect` searches commits: check the middle one, keep the half that holds the break, again, each step one more
run in the scratch folder. The answer is recorded and `praxis status` shows it under NEEDS YOU: whose landing, the
commit, the log, and the exact `git revert` command. Praxis never undoes a landing itself.

Every run is a row in the project database (`fullchecks`, database format 5). A running full check keeps
`.git/praxis/fullcheck/running.lock` locked. A run whose process died leaves its row without a result and the lock
free: the next landing sees that, marks the row interrupted and starts the full check again.
"""

from __future__ import annotations

import dataclasses
import json
import os
import sys
import time
from pathlib import Path

from agent_praxis import checkcache, gitops, heavy, hooks
from agent_praxis.agents import PARALLEL_CHECKOUT, Ctx, _tree
from agent_praxis.errors import PraxisError
from agent_praxis.landing_queue import _try_lock, _unlock, held

SCRATCH = "_fullcheck"  # an agent's name cannot start with "_", so no agent's folder ever has this name
WHO = {"name": "full-check", "task": "the full check of the main version"}  # what the command sees as PRAXIS_AGENT
HEAVY_NAME = "(full check)"
ASKED = "you asked for it (praxis fullcheck)"
POLL_SECONDS = 0.5
NEEDS_THE_HUMAN = 3  # the command's exit code for "a tool or service I need is down": no verdict on the project
JUDGED = ("passed", "failed")  # results that say something about the project (the others: the run did not finish)
KEEP = 500  # the newest runs kept in the database


def command(cfg) -> str:
    return cfg.full_check.strip()


def short(commit: str | None) -> str:
    return (commit or "")[:10]


def scratch_folder(ctx: Ctx, name: str = SCRATCH) -> Path:
    return ctx.cfg.worktree_dir(ctx.repo.root) / name


def _lock_path(store) -> Path:
    return store.dir / "fullcheck" / "running.lock"


def running(store) -> bool:
    """True while some process is running the full check for this project (it keeps the lock file locked)."""
    return held(_lock_path(store))


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
            _new_run(db, tip, cmd, "a landing's own check", covered, "passed", "the landing's check was this command")
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
    """Run the full check on the main version now. 0 when it passed, 2 when it did not. With *auto* (the background
    process): give way to a run already going, and run again while landings that are due keep arriving."""
    store, cmd = ctx.store, command(ctx.cfg)
    path = _lock_path(store)
    path.parent.mkdir(parents=True, exist_ok=True)
    while True:
        handle = open(path, "a+b")  # noqa: SIM115 - held open, and locked, for the whole run
        if _try_lock(handle):
            break
        handle.close()
        if auto:
            ctx.say("A full check is already running, so this one is not needed.")
            return 0
        code = _follow(ctx, cmd)
        if code is not None:
            return code
    try:
        _close_interrupted(store)
        with heavy.place(ctx, HEAVY_NAME, cmd) as (_mine, token):
            while True:
                code, result = _once(ctx, cmd, why, token)
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


def _once(ctx: Ctx, cmd: str, why: str, token: str | None) -> tuple[int, str]:
    """One full check of the main version as it is now: (exit code, the result recorded)."""
    store, main = ctx.store, ctx.cfg.main_branch
    covered = _newest_landing(store)  # read BEFORE the commit: a landing in between is then simply judged again
    tip = gitops.rev(ctx.repo.root, f"refs/heads/{main}")
    if not tip:
        raise PraxisError(f"The main branch {main!r} does not exist, so there is nothing to check.")
    with store.write() as db:
        run_id = _new_run(db, tip, cmd, why, covered)
    one = _Run(ctx, cmd, token, run_id, tip)
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
        code = hooks.run_logged(setup, folder, env, log)
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
    """One full check of one main version, and the search for the landing that broke it when it fails."""

    def __init__(self, ctx: Ctx, cmd: str, token: str | None, run_id: int, tip: str):
        self.ctx, self.cmd, self.token, self.id, self.tip = ctx, cmd, token, run_id, tip
        self.root = ctx.repo.root
        self.scratch: Path | None = None
        self.bad: dict[str, str | None] = {}  # commit -> the log of the run that failed on it
        self.steps: list[dict] = []  # what the search found out, kept in the row as it goes
        self.runs = 0

    def check(self, commit: str) -> tuple[int, str, str]:
        """Put the files of *commit* into the scratch folder and run the command there: (exit code, output, log)."""
        from agent_praxis import landing as L

        _put_files(self.scratch, commit)
        extra = {"PRAXIS_COMMIT": commit}
        if self.token:
            extra[heavy.TOKEN_ENV] = self.token  # a `praxis run --heavy` inside the command runs in this job's place
        cfg = dataclasses.replace(self.ctx.cfg, env={**self.ctx.cfg.env, **extra})
        self.runs += 1
        code, output, log = L._run_check(dataclasses.replace(self.ctx, cfg=cfg), WHO, self.scratch, self.cmd)
        with self.ctx.store.write() as db:
            db.execute("UPDATE fullchecks SET log = COALESCE(log, ?) WHERE id = ?", (log, self.id))
        return code, output, log

    def go(self) -> tuple[int, str]:
        ctx, main, tip = self.ctx, self.ctx.cfg.main_branch, self.tip
        self.scratch = _scratch(ctx, tip)
        ctx.say(f"Running the full check on {main} (commit {short(tip)}): {self.cmd}")
        ctx.say(f"It runs in a scratch folder of its own ({self.scratch}). Landings carry on meanwhile.")
        code, _output, log = self.check(tip)
        if code == 0:
            self._remember_pass(tip)
            _finish(ctx.store, self.id, "passed", log=log)
            ctx.say(f"The full check passed on {main} (commit {short(tip)}). Its output: {log}")
            return 0, "passed"
        if code == NEEDS_THE_HUMAN:
            note = (
                f"it ended with exit code {NEEDS_THE_HUMAN}, which means a problem outside the project's files "
                "(a tool or service it needs is down), so it says nothing about the landings"
            )
            _finish(ctx.store, self.id, "unknown", log=log, note=note)
            self._report()
            return 2, "unknown"
        self.bad[tip] = log
        ctx.say(f"The full check FAILED on {main} (commit {short(tip)}). Its output: {log}")
        found = self._find_culprit()
        _finish(ctx.store, self.id, "failed", log=log, steps=json.dumps(self.steps), **found)
        self._report()
        return 2, "failed"

    def _report(self) -> None:
        row = self.ctx.store.row("SELECT * FROM fullchecks WHERE id = ?", (self.id,))
        for line in describe(self.ctx, row):
            self.ctx.say(line)

    def _remember_pass(self, commit: str) -> None:
        tree = _tree(self.root, commit)
        if tree:
            checkcache.record(self.ctx.store, tree, self.cmd)  # the same files and command: a landing reuses it too

    # finding the landing that broke it ---------------------------------------------------------------------------

    def _points(self, good: str, after_landing_id: int) -> list[dict]:
        """The main version after each landing since the last pass, oldest first, ending with the version that
        failed. Each is a commit the search may check."""
        points, seen = [], {good}
        rows = self.ctx.store.rows(
            "SELECT agent, task, commit_id FROM landings WHERE ok = 1 AND commit_id IS NOT NULL AND id > ? ORDER BY id",
            (after_landing_id,),
        )
        for row in rows:
            commit = row["commit_id"]
            if commit in seen:
                continue
            seen.add(commit)
            if gitops.is_ancestor(self.root, good, commit) and gitops.is_ancestor(self.root, commit, self.tip):
                points.append({"agent": row["agent"], "task": row["task"], "commit": commit})
        if not points or points[-1]["commit"] != self.tip:  # main holds commits no landing brought (see describe)
            points.append({"agent": None, "task": None, "commit": self.tip})
        return points

    def _learn_earlier_failures(self, since_run: int) -> None:
        """Runs since the last pass that already failed on a commit: no need to run those commits again."""
        rows = self.ctx.store.rows(
            "SELECT commit_id, result, log, steps FROM fullchecks WHERE command = ? AND id > ? AND id != ?",
            (self.cmd, since_run, self.id),
        )
        for row in rows:
            if row["result"] == "failed":
                self.bad.setdefault(row["commit_id"], row["log"])
            try:
                steps = json.loads(row["steps"] or "[]")
            except ValueError:
                continue
            for step in steps if isinstance(steps, list) else []:
                if isinstance(step, dict) and step.get("ok") is False and step.get("commit"):
                    self.bad.setdefault(step["commit"], step.get("log"))

    def _find_culprit(self) -> dict:
        """The columns of the failed row that say what broke it: the landing, or why Praxis cannot tell."""
        ctx, tip = self.ctx, self.tip
        passed = newest(ctx.store, self.cmd, ("passed",))
        if passed is None:
            return {
                "note": "The full check has not passed on this project before, so there is no good version to "
                "compare with, and Praxis cannot tell which landing broke it."
            }
        good = passed["commit_id"]
        if good == tip:
            return {
                "good_commit": good,
                "note": "This same commit passed the full check before, so no landing broke it: the full check "
                "itself gives different answers on the same files (or something outside the files changed).",
            }
        if not gitops.is_ancestor(self.root, good, tip):
            return {
                "good_commit": good,
                "note": f"The commit that last passed ({short(good)}) is no longer part of "
                f"{ctx.cfg.main_branch}, so Praxis cannot tell which landing broke it.",
            }
        points = self._points(good, passed["landing_id"])
        self._learn_earlier_failures(passed["id"])
        if len(points) > 1:
            ctx.say(
                f"Looking for the landing that broke it: {len(points)} landings since the last pass "
                f"(commit {short(good)}). Praxis checks the middle one, then the half that holds the break."
            )
        low, high = -1, len(points) - 1  # points[low] passes (low -1: the last pass itself), points[high] fails
        while high - low > 1:
            middle = (low + high) // 2
            verdict = self._verdict(points[middle])
            if verdict is None:
                return {
                    "good_commit": good,
                    "note": f"Praxis could not finish finding the landing that broke it: a run ended with exit "
                    f"code {NEEDS_THE_HUMAN} (a tool or service the full check needs is down). It is one of the "
                    f"{high - low} landing(s) after commit {short(points[low]['commit'] if low >= 0 else good)}.",
                }
            if verdict:
                low = middle
            else:
                high = middle
        culprit = points[high]
        before = points[high - 1]["commit"] if high > 0 else good
        return {
            "good_commit": good,
            "culprit_agent": culprit["agent"],
            "culprit_task": culprit["task"],
            "culprit_commit": culprit["commit"],
            "culprit_log": self.bad.get(culprit["commit"]) or self.bad.get(tip),
            "undo": _undo_command(ctx, _past_rebuilds(ctx, before, culprit["commit"]), culprit["commit"]),
        }

    def _verdict(self, point: dict) -> bool | None:
        """True when the full check passes on this point, False when it fails, None when it could not tell."""
        ctx, commit = self.ctx, point["commit"]
        whose = f"{point['agent']}'s landing" if point["agent"] else "the newest version"
        if commit in self.bad:
            ctx.say(f"  After {whose} (commit {short(commit)}): it already failed there in an earlier run.")
            return False
        tree = _tree(self.root, commit)
        if tree and checkcache.passed_at(ctx.store, tree, self.cmd) is not None:
            ctx.say(f"  After {whose} (commit {short(commit)}): the same files already passed this command.")
            self._step(commit, True, None)
            return True
        ctx.say(f"  Checking {ctx.cfg.main_branch} as it was after {whose} (commit {short(commit)}).")
        code, _output, log = self.check(commit)
        if code == NEEDS_THE_HUMAN:
            return None
        if code == 0:
            self._remember_pass(commit)
        else:
            self.bad[commit] = log
        ctx.say(f"    It {'passed' if code == 0 else 'FAILED'} there. Its output: {log}")
        self._step(commit, code == 0, log)
        return code == 0

    def _step(self, commit: str, ok: bool, log: str | None) -> None:
        self.steps.append({"commit": commit, "ok": ok, "log": log})
        with self.ctx.store.write() as db:  # as it goes: a run that is interrupted keeps what it found out
            db.execute("UPDATE fullchecks SET steps = ? WHERE id = ?", (json.dumps(self.steps), self.id))


def _past_rebuilds(ctx: Ctx, before: str, commit: str) -> str:
    """Praxis's own "Rebuild <generated file>" commits right after the landing before this one belong to that
    landing's turn, not to this landing: the commit this landing really started from."""
    from agent_praxis import generated

    if not ctx.cfg.generated:
        return before
    log = gitops.git(["log", "--reverse", "--format=%H %s", f"{before}..{commit}"], ctx.repo.root, check=False)
    for line in log.stdout.splitlines():
        sha, _, subject = line.partition(" ")
        files = gitops.changed_between(ctx.repo.root, before, sha)
        if sha == commit or not subject.startswith("Rebuild "):
            break
        if not all(generated.entry_for(ctx.cfg, path) for path in files):
            break
        before = sha
    return before


def _undo_command(ctx: Ctx, before: str, commit: str) -> str:
    """The exact git command that undoes what main gained from *before* to *commit* (one landing), or "" when no
    single revert does it."""
    root = ctx.repo.root
    merges = gitops.git(["rev-list", "--merges", "-n1", f"{before}..{commit}"], root, check=False).stdout.strip()
    if not merges:
        if gitops.commits_not_in(root, [commit], [before]) == 1:
            return f"git revert --no-edit {commit}"
        return f"git revert --no-edit {before}..{commit}"
    parents = gitops.git(["rev-parse", f"{commit}^@"], root, check=False).stdout.split()
    if len(parents) == 2 and parents[1] == before:  # land_method = "merge": the landing is one merge on top of main
        return f"git revert --no-edit -m 2 {commit}"
    return ""


# --- saying it -------------------------------------------------------------------------------------------------------


def failing(ctx: Ctx):
    """The run that says the main version needs the human (the newest finished run, when it did not pass), or None."""
    cmd = command(ctx.cfg)
    if not cmd:
        return None
    last = newest(ctx.store, cmd, (*JUDGED, "unknown"))
    return last if last is not None and last["result"] != "passed" else None


def describe(ctx: Ctx, row, found: str = "") -> list[str]:
    """A finished run in plain words, one line per item: for the command's own output and for `praxis status`."""
    main, commit = ctx.cfg.main_branch, short(row["commit_id"])
    found = f", found {found}" if found else ""
    if row["result"] == "passed":
        return [f"The full check passed on {main} (commit {commit}). Its output: {row['log']}"]
    if row["result"] == "unknown":
        return [
            f"The full check could not judge {main} (commit {commit}{found}): {row['note']}.",
            f"Its output: {row['log']}" if row["log"] else "It wrote no output.",
            "Fix what it reports, then run it again with: praxis fullcheck",
        ]
    lines = [f"The full check is FAILING on {main} (commit {commit}{found}). Its output: {row['log']}"]
    culprit = row["culprit_commit"]
    if not culprit:
        lines.append(row["note"] or "Praxis could not tell which landing broke it.")
        lines.append("After a fix lands, check again with: praxis fullcheck")
        return lines
    if row["culprit_agent"]:
        lines.append(f'{row["culprit_agent"]}\'s landing "{row["culprit_task"]}" broke it (commit {culprit}).')
    else:
        lines.append(
            f"It first fails at commit {culprit}, which no recorded landing brought in (a commit made outside "
            "Praxis, or Praxis's own rebuild of a generated file)."
        )
    lines.append(
        f"The version right before it passes the full check. Output of the run that fails there: {row['note']}"
    )
    if row["undo"]:
        lines.append(
            f"Praxis never undoes a landing by itself. To undo this one, run in the main folder ({ctx.repo.root}):"
        )
        lines.append(f"  {row['undo']}")
        lines.append("Or have an agent land a fix. Then check again with: praxis fullcheck")
    else:
        lines.append(
            "Praxis never undoes a landing by itself, and no single `git revert` undoes this one (it holds merge "
            "commits). Have an agent land a fix, then check again with: praxis fullcheck"
        )
    return lines
