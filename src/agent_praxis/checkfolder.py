"""The landing line's own check folder (praxis.toml `check_folder = "line"`; the default is "agent").

By default a landing's check runs in the folder of the agent that is landing (for a batch: of the agent at the front
of the line). Three things follow from that. A file edited in that folder while the check runs spoils the verdict, for
that agent and, when the check reads the whole project, for every agent checked together with it. The agent cannot
work in its folder while its landing is checked. And whatever the project's tools cache while checking is spread over
every agent folder.

With `check_folder = "line"` the line has a folder of its own: `<agent folders>/_line`, a git worktree on no branch,
made on first use and kept, with the `share` folders linked and `setup` run in it once (the full check's scratch
folder is made the same way, by the same code: fullcheck._scratch). Before every check Praxis makes it hold exactly
the candidate: the candidate's files as git has them, nothing git tracks changed, and no file left over that git
neither tracks nor ignores. What git ignores stays, which is the point of keeping the folder: caches stay warm in one
place. A batch's candidate is built there and each smaller group of a batch that failed is checked there, one after
another.

One folder, one holder. Only the landing whose turn it is uses the folder, and it keeps
`.git/praxis/checkfolder/running.lock` locked while it does. A landing that finds the lock held (a landing that was
stopped is still ending) checks in the agent's folder, as without the setting. What a stopped landing left in the
folder is stale by definition, and the next check's first step clears it (see hold).

What a landing lands: the work as it was saved when the landing started (the commit the check ran on). The agent's
folder is not looked at again, so commits saved there later, and edits made there, stay where they are for the next
landing, and are named when the landing ends.

Where the two ways of checking can differ is written in the README ("Checking in the line's own folder"): the files
git ignores, the folder's path, and the branch the folder is on.
"""

from __future__ import annotations

import os
import stat
from pathlib import Path

from agent_praxis import fullcheck, gitops, paths
from agent_praxis.agents import Ctx, _is_link, _unlink, delete_without_following_links
from agent_praxis.errors import PraxisError
from agent_praxis.landing_queue import _unlock

NAME = "_line"  # an agent's name cannot start with "_", so no agent's folder ever has this name
SHOWN = 8  # how many file names a line of output lists before it says "and N more"


class NotReady(PraxisError):
    """The line's folder could not be made to hold the candidate, so no check ran in it."""


def wanted(cfg) -> bool:
    return cfg.check_folder == "line"


def lock_path(store) -> Path:
    return store.dir / "checkfolder" / "running.lock"


def _some(names: list[str]) -> str:
    return ", ".join(names[:SHOWN]) + (f" and {len(names) - SHOWN} more" if len(names) > SHOWN else "")


class Own:
    """The line's folder, held by this process until give_back."""

    def __init__(self, ctx: Ctx, row, handle):
        self.ctx, self.row, self._handle = ctx, row, handle
        self.path = fullcheck.scratch_folder(ctx, NAME)
        self._said = False

    def hold(self, commit: str) -> Path:
        """Make the folder hold exactly the files of *commit* and nothing else that git does not ignore; returns the
        folder. Whatever was there before (a check that was killed, a half-done git step, files a check wrote) is
        stale: only this process uses the folder."""
        try:
            folder = fullcheck._scratch(self.ctx, commit, NAME)  # made on first use; `share` linked, `setup` run once
            fullcheck._put_files(folder, commit)
            _clear_new_files(folder)
        except (PraxisError, OSError) as exc:
            raise NotReady(str(exc)) from exc
        # git commands Praxis runs there are this landing's: the project's git hooks see the same agent as before.
        gitops.agent_folder(folder, self.row["name"], self.row["task"])
        if not self._said:
            self._said = True
            self.ctx.say(
                f"The check runs in the landing line's own folder ({folder}), on exactly the files being landed "
                '(check_folder = "line"). What is saved or changed in an agent\'s folder from now on is not part of '
                "this landing, and cannot spoil its check."
            )
        return folder

    def put_back_check_rewrites(self) -> None:
        """Put the files the check is allowed to rewrite (check_may_change) back to their committed version. Nobody
        edits in this folder, so no copy is kept (in an agent's folder one is: see landing)."""
        cfg = self.ctx.cfg
        allowed = cfg.check_may_change + [g["path"] for g in cfg.generated]
        if not allowed:
            return
        changed = [p for p in gitops.dirty_files(self.path, untracked=False) if paths.matches_from_top(p, allowed)]
        if changed:
            gitops.git(["restore", "--worktree", "--source=HEAD", "--", *changed], self.path)
            self.ctx.say("Put back files the check rewrote (check_may_change in praxis.toml): " + _some(changed) + ".")

    def changed_by_check(self, checked: str) -> str | None:
        """None when the folder still holds exactly the candidate *checked* after its checks; else what changed, in
        plain words. The rule is the one for an agent's folder: a check that rewrites files git tracks may not have
        tested what would land. Here nobody else can have changed them."""
        folder = self.path
        if gitops.rev(folder, "HEAD") != checked:
            return (
                "The check made a commit (or moved to another one) in the landing line's own folder while it ran, so "
                "it did not test what would land. Nothing landed. A check must not commit; fix the check command, "
                "then land again."
            )
        entries = gitops.status_entries(folder)
        tracked = [path for state, path in entries if state != "??"]
        if tracked:
            return (
                "These files changed in the landing line's own folder while the check ran:\n"
                + "\n".join(f"  - {p}" for p in tracked[:20])
                + "\nOnly the check works in that folder, so the check itself rewrote them, and it may not have "
                "tested what would land. Nothing landed.\nIf the check rewrites them every time it runs, add them to "
                'check_may_change in praxis.toml (for example check_may_change = ["' + tracked[0] + '"]), then land '
                "again."
            )
        new = [path for _state, path in entries]
        if new:
            self.ctx.say(
                "Note: the check created new files that git does not ignore: " + _some(new) + ". They were not "
                "landed, and Praxis removes them from its folder before the next check. If they are build output, "
                "add them to .gitignore: what git ignores is kept there between checks."
            )
        return None

    def not_as_taken(self, folder: Path, checked: str) -> str | None:
        """Main moved while the check ran, so the work must go on top of the newer main and be checked again. That is
        done in the agent's folder, and would take in whatever was saved or edited there since. None when the folder
        is still exactly as the landing took it; else why this landing stops here, in plain words."""
        name = self.row["name"]
        if gitops.rev(folder, "HEAD") == checked and not gitops.dirty_files(folder, untracked=False):
            return None
        return (
            f"The main version changed while the check ran (someone committed to it outside Praxis), so the work "
            f"must be put on top of the newer version and checked again. {name}'s folder has changed since this "
            "landing started, and a landing only lands the work as it was when it started, so Praxis did not take "
            f"the newer changes in. Nothing landed, and nothing in the folder was touched.\nLand again: praxis land "
            f"{name}"
        )

    def say_what_stays(self, folder: Path, taken: str | None) -> None:
        """After a landing: name what was saved or changed in the agent's *folder* since the landing took *taken*.
        None of it landed (no check tested it) and all of it is still there."""
        ctx, name, branch = self.ctx, self.row["name"], self.row["branch"]
        if not taken or gitops.current_branch(folder) != branch:
            return
        tip = gitops.rev(folder, "HEAD")
        if tip and tip != taken:
            if gitops.is_ancestor(folder, taken, tip):
                later = gitops.git(["log", "--reverse", "--format=%h %s", f"{taken}..{tip}"], folder, check=False)
                lines = later.stdout.strip().splitlines()
                ctx.say(
                    f"{len(lines)} commit(s) were saved in {name}'s folder after this landing started. No check "
                    f"tested them, so they did not land. They are kept on {branch}:\n"
                    + "\n".join(f"  - {line}" for line in lines[:20])
                    + f"\nLand them when they are ready: praxis land {name}"
                )
            else:
                ctx.say(
                    f"{branch} no longer holds the commits that landed: its commits were rewritten after this "
                    f"landing started. What landed is the work as it was saved then (commit {taken[:10]}), which is "
                    f"what the check tested. Your newer version is kept in {name}'s folder; `praxis land {name}` "
                    "puts it on top of what landed, and tells you if the two clash."
                )
        changed = gitops.dirty_files(folder)
        if changed:
            ctx.say(
                f"{len(changed)} file(s) changed in {name}'s folder after this landing started are not part of it: "
                + _some(changed)
                + f". They are still there; the next `praxis land {name}` saves and lands them."
            )

    def does_not_count(self, head: str, names: list[str], files: list[str]) -> str:
        """For the agent at the front of a batch whose own work is already settled: the check of the others rewrote
        files git tracks, so it does not count for them."""
        return (
            f"The check of {', '.join(names)} rewrote files git tracks in the landing line's own folder, so that "
            "check does not count: they land on their own turn."
            + "".join(f"\n{line}" for line in files)
            + f"\n{head}'s own result stands. If the check rewrites these files every time it runs, add them to "
            "check_may_change in praxis.toml."
        )


def take(ctx: Ctx, row) -> Own | None:
    """The line's folder for the landing whose turn it is, or None: the checks then run in the agent's own folder
    (praxis.toml does not ask for it, or another process still holds it)."""
    if not wanted(ctx.cfg):
        return None
    handle = fullcheck.take_lock(lock_path(ctx.store))
    if handle is None:
        ctx.say(
            f"Note: the landing line's own folder ({fullcheck.scratch_folder(ctx, NAME)}) is in use by another "
            "process (a landing that was stopped may still be ending). This landing's check runs in "
            f"{row['name']}'s own folder instead, as it does without check_folder = \"line\": do not change that "
            "folder while it runs."
        )
        return None
    return Own(ctx, row, handle)


def give_back(own: Own | None) -> None:
    """The landing's turn is over: let go of the folder. It stays as it is, for the next landing to make its own."""
    if own is None or own._handle is None:
        return
    handle, own._handle = own._handle, None
    _unlock(handle)
    handle.close()


def refuse(ctx: Ctx, row, problem: NotReady, settled: bool = False) -> bool:
    """Nothing lands, because the line's folder could not be made ready: say so, and record it as a failure that is
    not the agent's doing. With *settled* (the agent at the front of a batch whose own work already landed, or was
    refused, in an earlier group) it is only said: its own result stands."""
    from agent_praxis import landing as L

    detail = (
        'With check_folder = "line" the check runs in a folder the landing line keeps for itself '
        f"({fullcheck.scratch_folder(ctx, NAME)}), and Praxis could not make that folder ready for the files being "
        f"landed:\n{problem}\n"
    )
    fix = (
        "This needs the human: fix what is said above, then land again. To land meanwhile the way it was done "
        'before, set check_folder = "agent" in praxis.toml.'
    )
    if settled:
        ctx.say(f"{detail}So the agents still waiting in this batch land on their own turn. {fix}")
        return False
    return L._fail(
        ctx,
        row,
        "needs-human",
        "the landing line's own folder could not be made ready",
        f"{detail}Nothing landed, and this does not count against the agent's tries. {fix}",
        sig_text=str(problem),
    )


def _clear_new_files(folder: Path) -> None:
    """Remove every file in *folder* that git neither tracks nor ignores: what an earlier check left there would be
    read by this one as if it were part of the project. Ignored files (caches, installed packages, build output)
    stay. A link on the way to such a file is removed as a link, and nothing is ever deleted through one."""
    top = str(folder)
    for state, rel in gitops.status_entries(folder):
        if state != "??":
            continue
        path, through_link = top, False
        for part in rel.rstrip("/").split("/"):
            path = os.path.join(path, part)
            if _is_link(path):
                _unlink(path)  # the link itself, never what it points to
                through_link = True
                break
        if through_link:
            continue
        if not os.path.lexists(path):
            continue  # it was behind a link that is gone now
        if os.path.isdir(path):  # a git project of its own that a check made: git names only its folder
            delete_without_following_links(Path(path))
        else:
            try:
                os.unlink(path)
            except PermissionError:  # a read-only file
                os.chmod(path, stat.S_IWRITE)
                os.unlink(path)
        parent = os.path.dirname(path)
        while parent != top:  # folders that held nothing else go too (a folder with ignored files in it stays)
            try:
                os.rmdir(parent)
            except OSError:
                break
            parent = os.path.dirname(parent)
