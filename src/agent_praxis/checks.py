"""The checks of one candidate: which ones run, which passes are remembered, and running the rest.

Without names in praxis.toml's `checks`, a candidate has ONE check: the first entry whose `paths` match a changed file,
else the default `check`. Its pass is remembered for the whole tree and the command (checkcache.py), as it always was.

With names, EVERY entry whose `paths` match a changed file runs, and so does every entry that lists a changed file among
its `inputs` (it reads that file); the default `check` runs as well when no entry's `paths` match. A named check's pass
is remembered under a key made from what the check reads, not from the whole project:

- the git blob ids (with path and mode) of every file in the candidate that matches its `inputs`, asked from git with
  `ls-tree`, never by reading a file; without `inputs` it reads everything, and the key holds the candidate's tree;
- the exact text of its command;
- the value of every environment variable it lists in `env_inputs`.

So a pass earned on one candidate counts for any later candidate whose files that check reads are the same, whatever
else changed. Only passes are remembered, never failures; the newest KEEP for each check stay (`checkpasses` table).
A check whose memory an audit found wrong (checkaudit.py) is not remembered or reused until `praxis trust <name>`.

Several checks of one candidate run one after another. With `checks_together` (the default), a check beyond the first
runs at the same time when a heavy job place is free right now: it takes that place for as long as it runs.
"""

from __future__ import annotations

import dataclasses
import hashlib
import os
import threading
import time
from pathlib import Path

from agent_praxis import checkcache, gitops, heavy, hooks, paths
from agent_praxis.agents import Ctx, _tree

KEEP = 200  # the newest remembered passes kept for each named check
JOIN_SECONDS = 0.2


@dataclasses.dataclass(frozen=True)
class Check:
    name: str  # "" for the one check of a project without named checks, and for the default check
    command: str
    shown: str  # what the output calls it: "the check", "check ui", "the default check"
    tag: str = ""  # what a landing's summary calls it ("" when there is nothing to tell apart)
    inputs: tuple[str, ...] | None = None  # the patterns of the files it reads; None: the whole project
    env_inputs: tuple[str, ...] = ()
    alone: bool = False  # True for the one check of a project without named checks

    @property
    def label(self) -> str:
        """Its short name in a list of checks."""
        return self.name or ("the check" if self.alone else "the default check")


def named(cfg) -> bool:
    """True when praxis.toml's `checks` entries have names: then every matching entry runs."""
    return any("name" in entry for entry in cfg.checks)


def _of(entry: dict) -> Check:
    inputs = tuple(entry["inputs"]) if "inputs" in entry else None
    return Check(
        entry["name"],
        entry["run"].strip(),
        f"check {entry['name']}",
        entry["name"],
        inputs,
        tuple(entry.get("env_inputs", ())),
    )


def by_name(cfg, name: str) -> Check | None:
    """The named check called *name* (upper and lower case do not matter), or None."""
    for entry in cfg.checks:
        if entry.get("name", "").casefold() == name.casefold() and "name" in entry:
            return _of(entry)
    return None


def names(cfg) -> list[str]:
    return [entry["name"] for entry in cfg.checks if "name" in entry]


def plan(cfg, changed: list[str]) -> list[Check]:
    """The checks a candidate that changes the files *changed* must pass, in the order they run. Empty when no check
    is set for them."""
    if not named(cfg):
        command, which = cfg.check_for(changed)
        command = command.strip()
        if not command:
            return []
        tag = which if cfg.checks else ""  # without `checks` it is just "the check", as before
        return [Check("", command, "the check" + (f" ({tag})" if tag else ""), tag, alone=True)]
    picked, chosen_by_paths = [], False
    for entry in cfg.checks:
        by_paths = any(paths.matches_from_top(path, entry["paths"]) for path in changed)
        reads = any(paths.matches_from_top(path, entry.get("inputs", [])) for path in changed)
        chosen_by_paths = chosen_by_paths or by_paths
        if by_paths or reads:
            picked.append(_of(entry))
    default = cfg.check.strip()
    if not chosen_by_paths and default:  # nobody's `paths` cover these files: the default check answers for them
        picked.append(Check("", default, "the default check", "default check"))
    return picked


def signature(checks: list[Check]) -> str:
    """One text that names exactly these checks and commands (for one unnamed check: its command)."""
    if len(checks) == 1 and not checks[0].name:
        return checks[0].command
    return "\n".join(f"{c.name or 'default'}: {c.command}" for c in checks)


def describe(checks: list[Check]) -> str:
    """The checks in a few words, for a line of output."""
    if len(checks) == 1 and checks[0].alone:
        return checks[0].command
    return ", ".join(c.label for c in checks)


# --- what a check reads --------------------------------------------------------------------------------------------


def _env_value(env: dict[str, str], name: str) -> str | None:
    if name in env:
        return env[name]
    if os.name == "nt":  # Windows does not tell upper from lower case in a variable's name
        for key, value in env.items():
            if key.upper() == name.upper():
                return value
    return None


class Keys:
    """The memory key of each named check for one candidate commit. git lists the candidate's files once."""

    def __init__(self, folder: Path | str, commit: str, env: dict[str, str]):
        self.folder, self.commit, self.env = folder, commit, env
        self.tree = _tree(Path(folder), commit)
        self._files: list[tuple[str, str]] | None = None  # (path, git's line for it: mode, type, blob id, path)

    def _listing(self) -> list[tuple[str, str]]:
        if self._files is None:
            raw = gitops.git(["ls-tree", "-r", "-z", "--full-tree", self.commit], self.folder).stdout
            self._files = [(line.partition("\t")[2], line) for line in raw.split("\0") if line]
        return self._files

    def of(self, check: Check) -> str | None:
        """The key a pass of *check* on this candidate is remembered under (None for an unnamed check, which is
        remembered by tree and command, and when the candidate cannot be read)."""
        if not check.name or not self.tree:
            return None
        digest = hashlib.sha256(b"praxis named check 1\0")
        digest.update(check.command.encode("utf-8", "replace") + b"\0")
        for name in sorted(set(check.env_inputs)):
            value = _env_value(self.env, name)
            digest.update(f"{name}\0{'unset' if value is None else 'set ' + value}\0".encode("utf-8", "replace"))
        if check.inputs is None:
            digest.update(f"tree {self.tree}\0".encode())
        else:
            patterns = [paths._glob(pattern) for pattern in check.inputs]
            for path, line in self._listing():
                folded = paths.key(path)
                if any(pattern.fullmatch(folded) for pattern in patterns):
                    digest.update(line.encode("utf-8", "replace") + b"\0")
        return digest.hexdigest()


# --- the memory ----------------------------------------------------------------------------------------------------


def trusted(store, name: str) -> bool:
    """False while an audit has found this check's memory wrong and nobody has run `praxis trust <name>`."""
    row = store.row("SELECT broken_at FROM checktrust WHERE name = ?", (name,))
    return row is None or row["broken_at"] is None


def passed_at(store, check: Check, keys: Keys) -> float | None:
    """When *check* last passed on what it reads in this candidate, or None."""
    if not check.name:
        return checkcache.passed_at(store, keys.tree, check.command) if keys.tree else None
    key = keys.of(check)
    if key is None or not trusted(store, check.name):
        return None
    row = store.row("SELECT passed_at FROM checkpasses WHERE name = ? AND key = ?", (check.name, key))
    return None if row is None else float(row["passed_at"])


def remember(store, check: Check, keys: Keys, log: str | None = None) -> None:
    """Remember a pass (call this only for a check that succeeded on this candidate)."""
    if not check.name:
        if keys.tree:
            checkcache.record(store, keys.tree, check.command)
        return
    key = keys.of(check)
    if key is None:
        return
    with store.write() as db:
        broken = db.execute("SELECT broken_at FROM checktrust WHERE name = ?", (check.name,)).fetchone()
        if broken is not None and broken["broken_at"] is not None:
            return  # its memory is not trusted: it runs every time until `praxis trust`
        db.execute("DELETE FROM checkpasses WHERE name = ? AND key = ?", (check.name, key))
        db.execute(
            "INSERT INTO checkpasses (name, key, passed_at, log) VALUES (?, ?, ?, ?)",
            (check.name, key, time.time(), log),
        )
        db.execute(
            "DELETE FROM checkpasses WHERE name = ? AND id NOT IN "
            "(SELECT id FROM checkpasses WHERE name = ? ORDER BY id DESC LIMIT ?)",
            (check.name, check.name, KEEP),
        )


def forget(db, name: str) -> None:
    """Drop every remembered pass of the named check (inside the caller's write)."""
    db.execute("DELETE FROM checkpasses WHERE name = ?", (name,))


def remembered(store) -> dict[str, int]:
    """{check name: how many passes are remembered for it}."""
    return {
        row["name"]: int(row["n"]) for row in store.rows("SELECT name, COUNT(*) AS n FROM checkpasses GROUP BY name")
    }


# --- one candidate's checks ----------------------------------------------------------------------------------------


@dataclasses.dataclass
class Step:
    check: Check
    how: str = "waiting"  # then "reused", "passed", "failed" or "not run"
    at: float | None = None  # when the pass that was reused had been earned
    code: int | None = None
    output: str = ""
    log: str | None = None

    def failure(self) -> tuple[str, str]:
        """(a short and a sentence-start way of saying this check failed)."""
        if self.check.alone:
            return "the check failed", "The check failed"
        return f"{self.check.shown} failed", f"{self.check.shown[0].upper()}{self.check.shown[1:]} failed"


@dataclasses.dataclass
class Outcome:
    steps: list[Step]
    moved: str | None = None  # files changed while the checks ran (in plain words): no pass was remembered

    @property
    def failed(self) -> Step | None:
        return next((step for step in self.steps if step.how == "failed"), None)

    @property
    def ok(self) -> bool:
        return self.moved is None and all(step.how in ("reused", "passed") for step in self.steps)

    @property
    def log(self) -> str | None:
        """The log to keep with the result: the failing check's, else the first check's that ran."""
        bad = self.failed
        if bad is not None:
            return bad.log
        return next((step.log for step in self.steps if step.how == "passed"), None)

    def from_memory(self) -> list[Check]:
        """The named checks with `inputs` whose pass was taken from memory: the ones an audit may run again."""
        return [s.check for s in self.steps if s.how == "reused" and s.check.name and s.check.inputs is not None]

    def summary(self) -> str:
        """The result of a candidate that passed, for the landing's record."""
        if not self.steps:
            return "no check is set, so nothing was tested"
        if len(self.steps) == 1 and self.steps[0].check.alone:
            step = self.steps[0]
            return (
                "check passed"
                + (", same tree already passed" if step.how == "reused" else "")
                + (f" ({step.check.tag})" if step.check.tag else "")
            )
        parts = [s.check.tag + (" from memory" if s.how == "reused" else "") for s in self.steps]
        return f"check passed ({', '.join(parts)})"

    def used(self, cfg) -> str:
        """The full check's command when this candidate passed it on its whole tree, else the one command used."""
        full = cfg.full_check.strip()
        whole = [s.check.command for s in self.steps if s.check.inputs is None]
        if full and full in whole:
            return full
        return self.steps[0].check.command if len(self.steps) == 1 and self.steps[0].check.alone else ""


def look(ctx: Ctx, folder: Path, checked: str, checks: list[Check]) -> Outcome:
    """What is already known about the candidate *checked*: every check with a remembered pass is "reused"."""
    outcome = Outcome([Step(check) for check in checks])
    _recall(ctx.store, Keys(folder, checked, hooks.command_env(ctx.cfg)), outcome.steps)
    return outcome


def _recall(store, keys: Keys, steps: list[Step]) -> bool:
    """Mark the waiting steps whose check has a remembered pass. True when no step is left to run."""
    for step in steps:
        if step.how == "waiting":
            step.at = passed_at(store, step.check, keys)
            if step.at is not None:
                step.how = "reused"
    return all(step.how != "waiting" for step in steps)


def _stamp(at: float) -> str:
    return time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(at))


def _skip_text(step: Step, batch: list[str] | None) -> str:
    check = step.check
    if check.alone and batch is not None:
        return "Skipping the check: the same files already passed the same command."
    if check.name and check.inputs is not None:
        return (
            f"Skipping {check.shown}: it passed at {_stamp(step.at)} on exactly the files it reads (its `inputs`), "
            f"with the same command: {check.command}"
        )
    return (
        f"Skipping {check.shown}: same tree already passed at {_stamp(step.at)} with the same command: {check.command}"
    )


def _run_text(check: Check, batch: list[str] | None) -> str:
    if batch is None:
        return f"Running {check.shown}: {check.command}"
    what = "the check" if check.alone else check.shown
    return f"Running {what} once for {len(batch)} agent(s) ({', '.join(batch)}): {check.command}"


def run(
    ctx: Ctx,
    row,
    folder: Path,
    checked: str,
    checks: list[Check],
    *,
    base: str | None = None,
    changed: list[str] | None = None,
    wait_for=None,
    before_run=None,
    batch: list[str] | None = None,
    own=None,
) -> Outcome:
    """Bring the candidate *checked* (the commit *folder* holds) through *checks*: a check with a remembered pass is
    skipped, and says so; the others run in *folder*, and those that pass are remembered. When files git tracks
    changed while they ran (`moved` then says what), only a pass that cannot have read them is (see _read_none_of).
    A failure stops the checks not yet started.

    With *own*, the landing line's own folder (see checkfolder), the checks that have to run do so there instead: it
    is first made to hold exactly the candidate, and nobody but the checks can change a file in it meanwhile.

    *base* is the main version the candidate is built on and *changed* the files it changes from there: every
    check command is told both (see _run_one). *wait_for(tree, signature, passed)* is asked first when something is left to run (an early check of
    these very files may still be running); *before_run()* is called right before the first check starts; *batch*
    names the agents checked together, for the words."""
    from agent_praxis import landing as L

    outcome = Outcome([Step(check) for check in checks])
    steps = outcome.steps
    if not steps:
        return outcome
    keys = Keys(folder, checked, hooks.command_env(ctx.cfg))
    if not _recall(ctx.store, keys, steps) and wait_for is not None and keys.tree:
        wait_for(keys.tree, signature(checks), lambda: time.time() if _recall(ctx.store, keys, steps) else None)
    for step in steps:
        if step.how == "reused":
            ctx.say(_skip_text(step, batch))
    todo = [step for step in steps if step.how == "waiting"]
    if not todo:
        return outcome
    spot = folder if own is None else own.hold(checked)  # where the checks run
    if before_run is not None:
        before_run()
    _run_all(ctx, row, spot, todo, batch, (base, changed))
    if own is None:
        L._put_back_check_rewrites(ctx, row, folder)  # pass or fail, so they are never saved as the agent's work
    else:
        own.put_back_check_rewrites()
    passed = [step for step in todo if step.how == "passed"]
    if passed:
        if own is None:
            outcome.moved = L._changed_during_check(ctx, row, folder, checked)
        else:
            outcome.moved = own.changed_by_check(checked)
        if outcome.moved is not None:
            passed = _read_none_of(passed, _touched(spot, checked))
        for step in passed:
            remember(ctx.store, step.check, keys, step.log)  # only a pass is remembered, never a failure
    return outcome


def _touched(folder: Path, checked: str) -> list[str] | None:
    """Every file in *folder* that is not as the candidate *checked* has it: changed, deleted, saved in a new commit
    on top, or new and unknown to git. None when git cannot say."""
    try:
        return sorted({*gitops.changed_between(folder, checked, "HEAD"), *gitops.dirty_files(folder)})
    except gitops.GitError:
        return None


def _read_none_of(passed: list[Step], touched: list[str] | None) -> list[Step]:
    """Files changed in the folder while the checks ran, so a pass is not known to be a pass of the candidate, and
    nothing lands from it. One kind of pass still stands: a named check that says which files it reads (`inputs`),
    when none of the files that changed is one of them. It read exactly the candidate's files. Remembering it means
    the agents checked together with the one whose folder changed do not pay for that check again: on their own
    turn it is found by the files it reads, as any remembered pass is."""
    if touched is None:
        return []
    return [
        step
        for step in passed
        if step.check.name
        and step.check.inputs is not None
        and not any(paths.matches_from_top(path, list(step.check.inputs)) for path in touched)
    ]


class _InThread:
    """A check that runs in a thread of its own, so that several run at the same time. One that runs beside another
    holds the heavy job place taken for it. The thread only runs the command and waits for it: a place is taken and
    given back by the thread that made this (the project's database is used from that one thread only)."""

    def __init__(self, ctx: Ctx, row, folder: Path, step: Step, told: tuple, place=None, token: str | None = None):
        self.step, self.place, self.error = step, place, None
        if token:  # a `praxis run --heavy` inside the check runs in this check's place
            ctx = dataclasses.replace(
                ctx, cfg=dataclasses.replace(ctx.cfg, env={**ctx.cfg.env, heavy.TOKEN_ENV: token})
            )
        self.thread = threading.Thread(target=self._go, args=(ctx, row, folder, told), daemon=True)
        self.thread.start()

    def _go(self, ctx: Ctx, row, folder: Path, told: tuple) -> None:
        try:
            _run_one(ctx, row, folder, self.step, told)
        except BaseException as exc:  # noqa: BLE001 - handed to the thread that waits for this one
            self.error = exc

    def release(self) -> None:
        place, self.place = self.place, None
        if place is not None:
            place.__exit__(None, None, None)


def _run_one(ctx: Ctx, row, folder: Path, step: Step, told: tuple) -> None:
    """Run one check's command to its end. *told* is (the main version the candidate is built on, the files the
    candidate changes): the command is told both (PRAXIS_BASE and PRAXIS_CHANGED_FILES, see landing._run_check)."""
    from agent_praxis import landing as L

    step.code, step.output, step.log = L._run_check(ctx, row, folder, step.check.command, *told)
    step.how = "passed" if step.code == 0 else "failed"


def _start_beside(ctx: Ctx, row, folder: Path, step: Step, told: tuple) -> _InThread | None:
    """Start *step* beside the check about to run, when a heavy job place is free right now. None: it waits."""
    if os.environ.get(heavy.TOKEN_ENV):
        return None  # this command itself runs inside a heavy job: one check at a time
    place = heavy.place(ctx, f"{row['name']} ({step.check.shown})", step.check.command, wait=False)
    mine, token = place.__enter__()
    if mine is None:
        place.__exit__(None, None, None)
        return None
    try:
        return _InThread(ctx, row, folder, step, told, place, token)
    except BaseException:
        place.__exit__(None, None, None)
        raise


def _run_all(ctx: Ctx, row, folder: Path, todo: list[Step], batch: list[str] | None, told: tuple) -> None:
    """Run the steps of *todo*, in order: one at a time, and with `checks_together` also the following ones for
    which a heavy job place is free at that moment. After a failure nothing more is started."""
    waiting, beside = list(todo), []
    try:
        while waiting and not _any_failed(todo):
            step = waiting.pop(0)
            ctx.say(_run_text(step.check, batch))
            while waiting and ctx.cfg.checks_together:
                extra = _start_beside(ctx, row, folder, waiting[0], told)
                if extra is None:
                    break
                ctx.say(_run_text(waiting.pop(0).check, batch) + "  (at the same time: a heavy job place is free)")
                beside.append(extra)
            if not beside:
                _run_one(ctx, row, folder, step, told)  # the one check of most landings: run right here, as always
                continue
            beside.append(_InThread(ctx, row, folder, step, told))  # no place of its own: it is the landing's check
            while beside[-1].thread.is_alive():
                beside[-1].thread.join(JOIN_SECONDS)
                beside[:-1] = _ended(beside[:-1])  # a check that ended gives its place back at once
            beside = _ended(beside)
        while beside:
            beside[0].thread.join(JOIN_SECONDS)
            beside = _ended(beside)
    finally:
        for extra in beside:  # stopped part-way: the places are given back; the checks end with this process
            extra.release()
    for step in waiting:
        step.how = "not run"


def _any_failed(steps: list[Step]) -> bool:
    return any(step.how == "failed" for step in steps)


def _ended(running: list[_InThread]) -> list[_InThread]:
    """Give back the place of every check in *running* that has ended. Returns the ones still running."""
    left = []
    for one in running:
        if one.thread.is_alive():
            left.append(one)
            continue
        one.release()
        if one.error is not None:
            for other in running:
                other.release()
            raise one.error
    return left
