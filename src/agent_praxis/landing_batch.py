"""Batched landings: when several agents wait in the landing line, the one at the front lands the ready ones together.

The agent at the front (the head) takes the agents behind it that are ready, up to `batch_max` in all. In its own
folder, on a detached HEAD (its branch is never touched), it puts each agent's saved commits on top of the newest main
version in line order, so one candidate holds everyone's work. The check runs ONCE on that candidate. If it passes,
main moves forward to the candidate (every agent's commits, in line order) and each agent is told it landed. If it
fails, the batch is cut in half and each half is checked on top of the newest main (group testing): halves that pass
land, the one agent left alone in a failing group is refused with the check's output, and everyone else carries on.

Each waiting `praxis land` process is still its own: the head writes each agent's result into a small note next to its
ticket, and that agent's process prints it and exits with its own code. An agent whose work cannot join (its commits
clash with the batch, say) is simply left out and lands on its own turn, as it always did. Nothing lands that was not
checked as exactly what lands. A stopped head leaves a marker file, so the next landing finds out whether main moved,
records the landings that happened, and puts the head's folder back on its branch.
"""

from __future__ import annotations

import dataclasses
import json
import os
import time
from pathlib import Path

from agent_praxis import enough, generated, gitops, hooks
from agent_praxis.agents import Ctx, _tree, folder_state, off_branch
from agent_praxis.landing_queue import clear_note, read_note, ticket_alive, write_note

MARKERS = "batches"


class _Sink:
    """Collects what an agent in the batch would have been told, for that agent's own process to print."""

    def __init__(self) -> None:
        self.lines: list[str] = []

    def __call__(self, text="") -> None:
        self.lines.append(str(text))


@dataclasses.dataclass
class Member:
    row: object  # the agent's row in the database
    ctx: Ctx  # prints to the agent's own process (the head) or to its sink
    token: str
    tip: str  # the agent's saved work when the batch took it (never changes while the batch runs)
    sink: _Sink | None = None
    commit: str | None = None  # the agent's last commit in the candidate
    outcome: str | None = None  # "landed", "refused", "left-out" or None while undecided
    landed_at: str | None = None  # main's version right after the landing that held it

    @property
    def name(self) -> str:
        return self.row["name"]


class Abort(Exception):
    """Stop batching: nobody else is judged; the head's own result is already recorded."""


# --- markers -------------------------------------------------------------------------------------------------------


def _marker_path(ctx: Ctx, agent: str) -> Path:
    return ctx.store.dir / MARKERS / f"{agent}.json"


def _save_marker(ctx: Ctx, marker: dict) -> None:
    path = _marker_path(ctx, marker["agent"])
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix(f".tmp{os.getpid()}")
    temp.write_text(json.dumps(marker), encoding="utf-8")
    os.replace(temp, path)


def _drop_marker(ctx: Ctx, agent: str) -> None:
    try:
        _marker_path(ctx, agent).unlink()
    except OSError:
        pass


def _markers(ctx: Ctx) -> list[dict]:
    found = []
    for path in sorted((ctx.store.dir / MARKERS).glob("*.json")):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if isinstance(data, dict) and "agent" in data:
            found.append(data)
    return found


def restore_folder(ctx: Ctx, row, folder: Path) -> None:
    """A batch that was stopped part-way leaves its head's folder on a detached HEAD holding the batch's files. Put
    the folder back on its own branch (the branch itself was never moved). Only when no landing is still running."""
    marker = next((m for m in _markers(ctx) if m["agent"] == row["name"]), None)
    if marker is None or ticket_alive(ctx.store, marker["token"]):
        return
    if gitops.current_branch(folder) is None and gitops.operation_in_progress(folder) is None:
        proc = gitops.git(["checkout", "-q", row["branch"]], folder, check=False)
        if proc.returncode == 0:
            ctx.say(
                f"{row['name']}'s folder was left on a batch candidate by a landing that was stopped. Put it "
                f"back on {row['branch']}; your own saved work was not changed."
            )


def recover(ctx: Ctx) -> None:
    """At the start of every turn: settle batches whose head stopped. If main moved to a batch's candidate, record the
    landings that happened (and tell the agents still waiting in it); drop markers nobody needs."""
    if not (ctx.store.dir / MARKERS).exists():
        return
    for marker in _markers(ctx):
        if not ticket_alive(ctx.store, marker["token"]):
            _settle(ctx, marker)


def _settle(ctx: Ctx, marker: dict) -> None:
    main_ref = f"refs/heads/{ctx.cfg.main_branch}"
    landing = marker.get("landing")
    if landing and not marker.get("recorded"):
        tip = gitops.rev(ctx.repo.root, main_ref)
        if tip and gitops.is_ancestor(ctx.repo.root, landing["tip"], tip):
            for entry in landing["members"]:
                known = ctx.store.row(
                    "SELECT 1 FROM landings WHERE agent = ? AND ok = 1 AND commit_id = ?",
                    (entry["agent"], entry["commit"]),
                )
                if known is None:
                    with ctx.store.write() as db:
                        db.execute(
                            "INSERT INTO landings (agent, task, at, ok, kind, summary, log, commit_id) "
                            "VALUES (?, ?, ?, 1, 'landed', ?, ?, ?)",
                            (
                                entry["agent"],
                                entry["task"],
                                time.time(),
                                entry["summary"] + " (recorded after the landing was stopped)",
                                entry.get("log"),
                                entry["commit"],
                            ),
                        )
                if entry.get("token") and ticket_alive(ctx.store, entry["token"]) and entry["agent"] != marker["agent"]:
                    write_note(
                        ctx.store,
                        entry["token"],
                        _landed_note(
                            ctx, entry["agent"], entry["task"], entry["tip"], landing["tip"], entry["summary"]
                        ),
                    )
        marker["recorded"] = True
        marker["landing"] = None
        _save_marker(ctx, marker)
    for entry in marker.get("members", []):  # agents still waiting, who were never told: they carry on by themselves
        note = read_note(ctx.store, entry["token"]) if entry["agent"] != marker["agent"] else None
        if note and not note.get("done"):
            clear_note(ctx.store, entry["token"])
    folder = Path(marker["folder"])
    if not folder.exists() or gitops.current_branch(folder) == marker["branch"]:
        _drop_marker(ctx, marker["agent"])


# --- who can join --------------------------------------------------------------------------------------------------


def _followers(ctx: Ctx, row, ticket) -> list[Member]:
    main_ref = f"refs/heads/{ctx.cfg.main_branch}"
    found: list[Member] = []
    for q in ctx.store.rows("SELECT * FROM queue WHERE id > ? ORDER BY id", (ticket.id,)):
        if len(found) >= ctx.cfg.batch_max - 1:
            break
        if q["agent"] == row["name"] or any(m.name == q["agent"] for m in found):
            continue
        if not ticket_alive(ctx.store, q["token"]) or read_note(ctx.store, q["token"]) is not None:
            continue
        other = ctx.store.agent(q["agent"])
        if other is None or other["paused"]:
            continue
        folder = Path(other["worktree"])
        if (
            folder_state(ctx, other) != "ok"
            or gitops.operation_in_progress(folder)
            or off_branch(other, folder) is not None
        ):
            continue
        branch_ref = f"refs/heads/{other['branch']}"
        tip = gitops.rev(ctx.repo.root, branch_ref)
        if not tip or gitops.commits_ahead(ctx.repo.root, main_ref, branch_ref) == 0:
            continue
        fork = gitops.git(["merge-base", tip, main_ref], ctx.repo.root, check=False).stdout.strip()
        merges = gitops.git(
            ["rev-list", "--merges", "-n1", f"{fork}..{tip}"], ctx.repo.root, check=False
        ).stdout.strip()
        if not fork or merges:
            continue  # a merge commit cannot be replayed one commit at a time: it lands on its own turn
        sink = _Sink()
        found.append(Member(other, dataclasses.replace(ctx, say=sink), q["token"], tip, sink))
    return found


# --- the batch -----------------------------------------------------------------------------------------------------


def try_batch(ctx: Ctx, row, folder: Path, ticket, finish: bool = False) -> bool | None:
    """The head's turn, when agents behind it are ready: land them together. Returns the head's own result (True
    landed, False not landed), or None when no batch was made, or the head was left out: then the normal turn runs."""
    cfg = ctx.cfg
    if cfg.batch_max < 2 or cfg.land_method != "rebase" or gitops.dirty_files(folder, untracked=False):
        return None
    followers = _followers(ctx, row, ticket)
    if not followers:
        return None
    failed = _vet_head(ctx, row, folder)
    if failed is not None:
        return failed
    head = Member(row, ctx, ticket.token, gitops.rev(ctx.repo.root, f"refs/heads/{row['branch']}") or "")
    members = [head, *followers]
    marker = {
        "agent": row["name"],
        "token": ticket.token,
        "folder": str(folder),
        "branch": row["branch"],
        "members": [{"agent": m.name, "token": m.token, "tip": m.tip} for m in members],
        "landing": None,
        "recorded": False,
    }
    _save_marker(ctx, marker)
    names = [m.name for m in members]
    for m in followers:
        write_note(ctx.store, m.token, {"members": names})
    ctx.say(
        f"{len(members)} agents are ready to land: {', '.join(names)}. Praxis lands them together: one check "
        "for all of them, and it only looks for the culprit if that check fails."
    )
    run = _Run(ctx, head, folder, marker, finish)
    try:
        run.process(members)
        for m in followers:
            if m.outcome is None:  # left out: it lands on its own turn
                clear_note(ctx.store, m.token)
    except Abort:
        for m in followers:
            if m.outcome is None:
                clear_note(ctx.store, m.token)
        run.close()
        return False if head.outcome is None else head.outcome == "landed"
    except (
        BaseException
    ):  # stopped (Ctrl+C, a crash): put the folder back, record what happened, keep every agent's work
        run.close(quiet=True)
        _settle(ctx, marker)
        raise
    run.close()
    if head.outcome is None:
        return None
    return head.outcome == "landed"


def _vet_head(ctx: Ctx, row, folder: Path):
    """What a single landing does before its check: clashes with held files, putting the work on top of main, the
    undo check. None when the head's work is ready for the batch, else the recorded failure (False)."""
    from agent_praxis import landing as L

    main = ctx.cfg.main_branch
    main_ref, branch_ref = f"refs/heads/{main}", f"refs/heads/{row['branch']}"
    root = ctx.repo.root
    base = gitops.rev(root, main_ref)
    early = set(gitops.changed_files(root, base, branch_ref)) & set(gitops.changed_between(root, base, branch_ref))
    clashes = L._held_by_others(ctx, row, sorted(early))
    if not clashes:
        problem = L._integrate(ctx, row, folder)
        if problem is not None:
            return problem
        changes = gitops.changed_between(folder, base, "HEAD")
        clashes = L._held_by_others(ctx, row, changes, note=True)
        undone = [] if clashes or ctx.cfg.exclusive_code else L._undoes_landed_work(ctx, row, folder, base, changes)
        if undone:
            return _fail_undo(ctx, row, undone)
    if clashes:
        return _fail_claims(ctx, row, clashes)
    return None


def _fail_undo(ctx: Ctx, row, undone: list[str]) -> bool:
    from agent_praxis import landing as L

    main = ctx.cfg.main_branch
    return L._fail(
        ctx,
        row,
        "undo",
        "it would undo work that landed after it started",
        "Landing this would put these files back to an older version, undoing newer work in main:\n"
        + "\n".join(undone)
        + "\nTake the newest main version of each: in your folder run\n"
        f"  git checkout {main} -- <file>\n"
        "then make your own change to it again, and land again. If the older version is really wanted, "
        "ask the human.",
    )


def _fail_claims(ctx: Ctx, row, clashes: list[str]) -> bool:
    from agent_praxis import landing as L

    main = ctx.cfg.main_branch
    return L._fail(
        ctx,
        row,
        "claims",
        "it changed files another agent holds",
        "These files are held by another agent, so this work cannot land:\n"
        + "\n".join(clashes)
        + "\nPut each of them back to the newest main version: in your folder run\n"
        f"  git checkout {main} -- <file>\n"
        "then land again (or ask the human to sort it out). Landing never changes files another agent "
        "holds.",
    )


class _Run:
    """One batch in progress, in the head's folder."""

    def __init__(self, ctx: Ctx, head: Member, folder: Path, marker: dict, finish: bool):
        self.ctx, self.head, self.folder, self.marker, self.finish = ctx, head, folder, marker, finish
        self.root = ctx.repo.root
        self.main_ref = f"refs/heads/{ctx.cfg.main_branch}"
        self.failed: dict[str, tuple[int, str, str]] = {}  # candidate tree -> (check exit code, output, log): it failed
        self.size = len(marker["members"])
        self.last_landed: str | None = None

    # building the candidate --------------------------------------------------------------------------------------

    def _build(self, group: list[Member], base: str) -> list[Member]:
        """Detach at *base* and put each agent's commits on top, in line order. Agents that cannot join are left out
        (they land on their own turn) or refused (a held file, undoing newer work: said as a single landing says it)."""
        from agent_praxis import landing as L

        folder = self.folder
        gitops.git(["checkout", "-q", "--detach", base], folder)
        built: list[Member] = []
        for m in group:
            if m.outcome is not None:
                continue
            cur = gitops.rev(folder, "HEAD")
            fork = gitops.git(["merge-base", m.tip, base], folder, check=False).stdout.strip()
            proc = (
                gitops.git(
                    [*gitops.identity_args(folder), "rebase", "-q", "--onto", cur, fork, m.tip], folder, check=False
                )
                if fork
                else None
            )
            if proc is not None:
                proc = L._past_generated_clashes(m.ctx, folder, proc)
            if proc is None or proc.returncode != 0:
                gitops.abort_operation(folder)
                gitops.git(["checkout", "-q", "--detach", cur], folder, check=False)
                self._leave_out(m)
                continue
            new = gitops.rev(folder, "HEAD")
            if new == cur:  # everything it did is already there
                self._leave_out(m)
                continue
            files = gitops.changed_between(folder, cur, new)
            clashes = L._held_by_others(m.ctx, m.row, files)
            undone = (
                []
                if clashes or self.ctx.cfg.exclusive_code
                else L._undoes_landed_work(m.ctx, m.row, folder, base, files, ref=new)
            )
            if clashes or undone:
                gitops.git(["checkout", "-q", "--detach", cur], folder, check=False)
                if clashes:
                    _fail_claims(m.ctx, m.row, clashes)
                else:
                    _fail_undo(m.ctx, m.row, undone)
                self._decide(m, "refused", ok=False)
                continue
            m.commit = new
            built.append(m)
        return built

    def _leave_out(self, m: Member) -> None:
        m.outcome = "left-out"
        if m is not self.head:
            clear_note(self.ctx.store, m.token)

    def _decide(self, m: Member, outcome: str, ok: bool, commit: str | None = None) -> None:
        """Record an agent's result; an agent behind the head is told through its note, and its process prints it."""
        m.outcome = outcome
        if m is self.head:
            return
        write_note(
            self.ctx.store,
            m.token,
            {"done": True, "ok": ok, "lines": m.sink.lines if m.sink else [], "commit": commit, "pinned": m.tip},
        )

    # the check ---------------------------------------------------------------------------------------------------

    def _run_check(self, built: list[Member], base: str) -> tuple[int, str, str | None]:
        from agent_praxis import checkcache
        from agent_praxis import landing as L

        ctx, folder = self.ctx, self.folder
        command = ctx.cfg.check_for(gitops.changed_between(folder, base, "HEAD"))[0].strip()
        if not command:
            return 0, "", None
        checked = gitops.rev(folder, "HEAD")
        tree = _tree(folder, "HEAD")
        if tree and checkcache.passed_at(ctx.store, tree, command) is not None:
            ctx.say("Skipping the check: the same files already passed the same command.")
            return 0, "", None
        ctx.say(f"Running the check once for {len(built)} agent(s) ({', '.join(m.name for m in built)}): {command}")
        code, output, log = L._run_check(ctx, self.head.row, folder, command)
        L._put_back_check_rewrites(ctx, self.head.row, folder)
        if code == 0:
            moved = L._changed_during_check(ctx, self.head.row, folder, checked)
            if moved:
                L._fail(ctx, self.head.row, "changed-during-check", "files changed while the check ran", moved)
                self.head.outcome = "refused"
                raise Abort
            if tree:
                checkcache.record(ctx.store, tree, command)  # only a pass is remembered
        return code, output, log

    # one group: check it, land it or cut it in half ---------------------------------------------------------------

    def process(self, group: list[Member]) -> None:
        from agent_praxis import landing as L

        ctx = self.ctx
        for _try in range(L.RETRIES_WHEN_MAIN_MOVES):
            base = gitops.rev(self.root, self.main_ref)
            built = self._build([m for m in group if m.outcome is None], base)
            if not built:
                return
            tree = _tree(self.folder, "HEAD")
            if tree in self.failed:  # the very files that already failed as a whole: no need to run it again
                code, output, log = self.failed[tree]
            else:
                code, output, log = self._run_check(built, base)
            if code == L.NEEDS_THE_HUMAN:
                L._needs_the_human(ctx, self.head.row, output, log)
                self.head.outcome = "refused"
                raise Abort
            if code != 0:
                self.failed[tree] = (code, output, log)
                if len(built) == 1:
                    self._refuse(built[0], output, log, alone=self.size == 1)
                    return
                ctx.say(
                    f"The check failed for the {len(built)} agents together. Checking them in two halves to find "
                    "which work broke it."
                )
                half = (len(built) + 1) // 2
                self.process(built[:half])
                self.process(built[half:])
                return
            if gitops.rev(self.root, self.main_ref) != base:
                ctx.say("The main version changed while the check ran. Doing it again on top of the newest version.")
                continue
            if self._land(built, base, log):
                return
            ctx.say(
                "The main version changed just as Praxis was moving it forward. Doing it again on top of the "
                "newest version."
            )
        L._fail(
            ctx,
            self.head.row,
            "busy-main",
            "the main version kept changing during the check",
            f"The main version changed {L.RETRIES_WHEN_MAIN_MOVES} times while Praxis was checking. Someone is "
            "committing to it directly, outside Praxis. Land again when that stops.",
        )
        self.head.outcome = "refused"
        raise Abort

    def _refuse(self, m: Member, output: str, log: str | None, alone: bool) -> None:
        from agent_praxis import landing as L

        count = enough.problem_count(output)
        tail = "\n".join(output.rstrip().splitlines()[-L.TAIL_LINES :])
        why = (
            ""
            if alone
            else (
                f"Praxis checked {self.size} agents' work together and then in smaller groups: this "
                f"is the work that broke the check.\n"
            )
        )
        L._fail(
            m.ctx,
            m.row,
            "check",
            "the check failed" + (f" ({count} problem(s))" if count else ""),
            f"The check failed, so nothing landed. {why}The end of its output:\n{tail}\nFull output: {log}\n"
            "Fix the problem in your folder, then land again.",
            problems=count,
            log=log,
            sig_text=output,
        )
        self._decide(m, "refused", ok=False)

    # landing a group that passed ---------------------------------------------------------------------------------

    def _land(self, built: list[Member], base: str, log: str | None) -> bool:
        """Move main to the candidate the check passed on. False when main moved just before (do it again)."""
        from agent_praxis import landing as L

        ctx = self.ctx
        tip = gitops.rev(self.folder, "HEAD")
        together = "" if len(built) == 1 else f", checked together with {', '.join(m.name for m in built)}"
        summary = (
            "check passed" if ctx.cfg.check.strip() or ctx.cfg.checks else "no check is set, so nothing was tested"
        ) + (together if len(built) > 1 else "")
        self.marker["landing"] = {
            "base": base,
            "tip": tip,
            "members": [
                {
                    "agent": m.name,
                    "task": m.row["task"],
                    "token": m.token,
                    "tip": m.tip,
                    "commit": m.commit,
                    "summary": summary,
                    "log": log,
                }
                for m in built
            ],
        }
        self.marker["recorded"] = False
        _save_marker(ctx, self.marker)
        advanced = L._advance(ctx, self.head.row, base, tip)
        if advanced == L.MAIN_MOVED:
            self.marker["landing"] = None
            _save_marker(ctx, self.marker)
            return False
        if advanced is not True:
            self.marker["landing"] = None
            _save_marker(ctx, self.marker)
            L._fail(ctx, self.head.row, "main-folder", "the main version could not be moved forward safely", advanced)
            self.head.outcome = "refused"
            raise Abort
        for m in built:
            L._record_landing(m.ctx, m.row, summary, log, m.commit)
            m.outcome, m.landed_at = "landed", tip
        self.marker["recorded"] = True
        _save_marker(ctx, self.marker)
        self.last_landed = tip
        main = ctx.cfg.main_branch
        for m in built:
            m.ctx.say(f'Landed: {m.name}\'s work on "{m.row["task"]}" is now in {main} ({summary}).')
        generated.rebuild_after_landing(ctx, built[0].row, base, tip)  # still the head's turn: nobody else moves main
        final = gitops.rev(self.root, self.main_ref) or tip
        for m in built:
            hooks.run_after_land(m.ctx, m.row, final)
        for m in built:
            self._decide(m, "landed", ok=True, commit=tip)
        return True

    # the end -----------------------------------------------------------------------------------------------------

    def close(self, quiet: bool = False) -> None:
        """Put the head's folder back on its branch (and level with main when its work landed). The marker goes
        once that is done; if it cannot be done, it stays, and the head's next landing does it."""
        ctx, folder, head = self.ctx, self.folder, self.head
        branch = head.row["branch"]
        if gitops.operation_in_progress(folder):
            gitops.abort_operation(folder)
        if gitops.current_branch(folder) != branch:
            proc = gitops.git(["checkout", "-q", branch], folder, check=False)
            if proc.returncode != 0:
                if not quiet:
                    ctx.say(
                        f"{head.name}'s folder could not be put back on {branch}: {(proc.stderr or '').strip()}\n"
                        "It will be put back by the next landing, or run `git switch " + branch + "` in it."
                    )
                return
        if head.outcome == "landed" and head.landed_at:
            catch_up(ctx, head.row, folder, head.tip, head.landed_at, quiet=quiet)
        if not self.marker.get("landing") or self.marker.get("recorded"):
            _drop_marker(ctx, head.name)


def catch_up(ctx: Ctx, row, folder: Path, pinned: str, commit: str, quiet: bool = False) -> None:
    """After an agent's work landed as rebased copies: level its folder with the version that holds them, when that is
    safe (still on its branch, at the saved work the batch took, nothing unsaved). The landed copies hold every one of
    its changes, so nothing is lost; if it is not safe, the agent's next landing simply sets the old commits aside."""
    branch = row["branch"]
    if (
        gitops.current_branch(folder) != branch
        or gitops.rev(folder, f"refs/heads/{branch}") != pinned
        or gitops.dirty_files(folder, untracked=False)
    ):
        return
    if gitops.git(["reset", "-q", "--hard", commit], folder, check=False).returncode == 0 and not quiet:
        ctx.say(f"{row['name']}'s folder now matches the main version.")


# --- the other agents' side ----------------------------------------------------------------------------------------


def _landed_note(ctx: Ctx, agent: str, task: str, pinned: str, commit: str, summary: str) -> dict:
    main = ctx.cfg.main_branch
    return {
        "done": True,
        "ok": True,
        "pinned": pinned,
        "commit": commit,
        "lines": [
            f'Landed: {agent}\'s work on "{task}" is now in {main} ({summary}, recorded after the landing was stopped).'
        ],
    }


def apply_verdict(ctx: Ctx, row, folder: Path, note: dict, finish: bool) -> bool:
    """An agent behind the head, told how its work went: print it, level its folder if it landed, and give its result."""
    for line in note.get("lines", []):
        ctx.say(line)
    if note.get("ok"):
        if note.get("commit") and note.get("pinned"):
            catch_up(ctx, row, folder, note["pinned"], note["commit"])
        if not finish:
            ctx.say(f"When the task is finished: praxis done {row['name']}")
        return True
    return False
