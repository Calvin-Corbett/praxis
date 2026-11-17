"""Putting an agent's folder back on its own branch without dropping anything that was made in it.

A landing that is stopped part-way (a tool's time limit kills `praxis land` during the check) can leave the agent's
folder on no branch, holding a batch's candidate: the newest main version with the batch's work on top. The agent does
not know. It carries on in that folder: it saves another commit, or edits a file. Going straight back to the branch
then drops that commit (it is on no branch, and its files leave the folder with it).

So a folder is only ever put back like this:

1. Find the commits in the folder that are on neither the agent's branch nor main, and that Praxis did not put there
   itself (the batch's candidates, and copies of the batch's own commits). Those are the agent's own new commits.
2. None: switch to the branch. git carries unsaved edits and new files across, and refuses (changing nothing) when
   it would have to overwrite one.
3. Some: put copies of them on top of the branch, in order, without touching the folder (git works each one out
   from the files it stores). Only when every one fits does the branch take them and the folder switch to it, in
   one git step that changes nothing when it cannot finish.
4. When that cannot be done, nothing is changed at all: the folder stays exactly as it is, the commits get a name of
   their own (refs/praxis/saved/<agent>-<time>) so nothing can remove them, and the caller says how to carry on.
"""

from __future__ import annotations

import dataclasses
import time
from pathlib import Path

from agent_praxis import gitops

SAVED_PREFIX = "refs/praxis/saved/"


@dataclasses.dataclass
class Result:
    ok: bool  # the folder is on the agent's own branch now
    moved: bool = False  # this call switched it there
    carried: list[str] = dataclasses.field(default_factory=list)  # "<short id> <subject>" of each commit carried
    left: list[str] = dataclasses.field(default_factory=list)  # the same, for commits that could not be carried
    ids: list[str] = dataclasses.field(default_factory=list)  # their ids, oldest first
    saved: str | None = None  # the name the folder's commits were kept under, when it was left as it is
    why: str = ""  # why it could not be put back: git's own words, or ours


def new_commits(folder: Path, branch_ref: str, main_ref: str, known: list[str], copies_of: list[str]) -> list[str]:
    """The commits in *folder* (oldest first) that are on neither the agent's branch nor main and that Praxis did not
    make: not one of *known* (a stopped batch's candidates) or below one, and not a copy of a commit of *copies_of*
    (the saved work the batch took from each of its agents)."""
    copies_of = [t for t in copies_of if t and gitops.rev(folder, t)]
    known = [k for k in known if k and gitops.rev(folder, k)] + copies_of  # the batch's agents' commits are theirs
    found = gitops.git(["rev-list", "--reverse", "HEAD", "--not", branch_ref, main_ref, *known], folder).stdout.split()
    for tip in copies_of:
        if not found:
            break
        # Of the commits only the folder has, the ones that are not the same change as a commit only *tip* has.
        proc = gitops.git(["rev-list", "--cherry-pick", "--right-only", f"{tip}...HEAD"], folder, check=False)
        if proc.returncode == 0:
            own = set(proc.stdout.split())
            found = [c for c in found if c in own]
    return found


def _described(folder: Path, commits: list[str]) -> list[str]:
    return [gitops.git(["log", "-1", "--format=%h %s", c], folder, check=False).stdout.strip() or c for c in commits]


def _copy_onto(folder: Path, onto: str, commit: str) -> str | None:
    """*commit*'s change on top of *onto*, as a new commit made beside the folder (no file in it changes). *onto*
    itself when the change is already there; None when it does not fit (or this git cannot work it out)."""
    parents = gitops.git(["rev-list", "--parents", "-n1", commit], folder, check=False).stdout.split()
    if len(parents) != 2:
        return None  # a merge, or a commit with nothing before it: not a change that can be replayed by itself
    merged = gitops.git(["merge-tree", "--write-tree", f"--merge-base={parents[1]}", onto, commit], folder, check=False)
    tree = merged.stdout.split("\n", 1)[0].strip()
    if merged.returncode != 0 or not tree:
        return None
    if tree == gitops.out(["rev-parse", f"{onto}^{{tree}}"], folder):
        return onto
    name, email, date, message = gitops.git(
        ["log", "-1", "--format=%an%x00%ae%x00%aI%x00%B", commit], folder
    ).stdout.split("\0", 3)
    made = gitops.git(
        [*gitops.identity_args(folder), "commit-tree", *gitops.sign_args(folder), tree, "-p", onto],
        folder,
        check=False,
        stdin=message,
        env={"GIT_AUTHOR_NAME": name, "GIT_AUTHOR_EMAIL": email, "GIT_AUTHOR_DATE": date},
    )
    return made.stdout.strip() if made.returncode == 0 and made.stdout.strip() else None


def _keep(folder: Path, agent: str) -> str | None:
    """Give the folder's commits a name of their own, so nothing can remove them. The same name again when an earlier
    try already kept exactly these."""
    earlier = gitops.git(
        ["for-each-ref", "--points-at", "HEAD", "--format=%(refname)", f"{SAVED_PREFIX}{agent}-*"], folder, check=False
    ).stdout.split()
    if earlier:
        return earlier[0]
    name = f"{SAVED_PREFIX}{agent}-{time.strftime('%Y%m%d-%H%M%S')}"
    return name if gitops.git(["update-ref", name, "HEAD"], folder, check=False).returncode == 0 else None


def put_back(folder: Path, agent: str, branch: str, main_ref: str, known: list[str], copies_of: list[str]) -> Result:
    """Put *folder* back on *branch* by the rule above. Only a folder on no branch is moved: one on another branch
    was put there by somebody on purpose (Result.ok is False, and nothing is said or changed)."""
    on = gitops.current_branch(folder)
    if on == branch:
        return Result(ok=True)
    if on is not None or gitops.operation_in_progress(folder):
        return Result(ok=False)
    branch_ref = f"refs/heads/{branch}"
    extra = new_commits(folder, branch_ref, main_ref, known, copies_of)
    if not extra:
        proc = gitops.git(["checkout", "-q", branch], folder, check=False)
        if proc.returncode == 0:
            return Result(ok=True, moved=True)
        return Result(ok=False, why=(proc.stderr or proc.stdout).strip())
    told = _described(folder, extra)
    tip = gitops.rev(folder, branch_ref)
    for commit in extra:
        tip = _copy_onto(folder, tip, commit) if tip else None
        if tip is None:
            return Result(ok=False, left=told, ids=extra, saved=_keep(folder, agent), why="clash")
    proc = gitops.git(["checkout", "-q", "-B", branch, tip], folder, check=False)  # all of it, or nothing changes
    if proc.returncode == 0:
        return Result(ok=True, moved=True, carried=told, ids=extra)
    return Result(ok=False, left=told, ids=extra, saved=_keep(folder, agent), why=(proc.stderr or proc.stdout).strip())


def back_if_nothing_is_left_behind(folder: Path, branch: str) -> bool:
    """For a caller that knows nothing about a stopped batch: switch a folder on no branch back to *branch* only when
    every commit in it is on some branch already, so the switch cannot drop one. Otherwise it stays as it is, and the
    agent's next landing puts it back by the rule above."""
    if gitops.current_branch(folder) == branch:
        return True
    proc = gitops.git(["rev-list", "-n1", "HEAD", "--not", "--branches"], folder, check=False)
    if proc.returncode != 0 or proc.stdout.strip():
        return False
    return gitops.git(["checkout", "-q", branch], folder, check=False).returncode == 0


def explain(result: Result, agent: str, branch: str, folder: Path, stopped: bool = True) -> str:
    """What to tell the agent when its folder could not be put back: what is where, and how to carry on. *stopped*:
    a landing that was stopped left it there (else the batch that has just ended did, and commits were saved in the
    folder while it ran)."""
    start = f"{agent}'s folder {folder} " + (
        "was left on no branch by a landing that was stopped" if stopped else "is still on no branch after its batch"
    )
    when = "after that" if stopped else "while the batch ran"
    kept = f" They are also kept under the name {result.saved}, which nothing removes." if result.saved else ""
    if result.why == "clash":
        return (
            f"{start}, and {len(result.left)} commit(s) were saved in it {when}:\n"
            + "\n".join(f"  - {line}" for line in result.left)
            + f"\nPraxis could not put them on {branch} by itself: they do not fit on top of what {branch} holds.\n"
            f"Nothing was changed and nothing is lost: the folder is exactly as it was, with those commits in it.{kept}\n"
            f"To carry on, in that folder run (after `git stash`, if you have unsaved edits there):\n"
            f"  git switch {branch}\n  git cherry-pick "
            + " ".join(c[:10] for c in result.ids)
            + "\nfix the files git names, run `git cherry-pick --continue` (then `git stash pop`), and land again."
        )
    more = (
        f", and {len(result.left)} commit(s) were saved in it {when}:\n"
        + "\n".join(f"  - {line}" for line in result.left)
        + "\n"
        if result.left
        else ". "
    )
    return (
        f"{start}{more}Praxis could not put it back on {branch}, because git would not switch it:\n{result.why}\n"
        f"Nothing was changed and nothing is lost: the folder is exactly as it was.{kept if result.left else ''}\n"
        "If git names unsaved changes or new files above, save them as a commit right where they are (in that "
        'folder: `git add -A`, then `git commit -m "<what you did>"`), then land again: Praxis puts your commits on '
        f"{branch} and the folder back on it."
    )
