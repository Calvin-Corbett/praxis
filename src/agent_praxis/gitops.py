"""Small, careful wrappers around the git command line."""

from __future__ import annotations

import os
import subprocess
from dataclasses import dataclass
from pathlib import Path

from agent_praxis.errors import PraxisError


class GitError(PraxisError):
    def __init__(self, args: list[str], proc: subprocess.CompletedProcess):
        self.proc = proc
        detail = (proc.stderr or proc.stdout or "").strip()
        super().__init__(f"git {' '.join(args)} did not work:\n{detail}")


def git(args: list[str], cwd: Path | str, check: bool = True) -> subprocess.CompletedProcess:
    env = dict(os.environ)
    env.setdefault("GIT_TERMINAL_PROMPT", "0")
    env["GIT_EDITOR"] = "true"  # never open an editor and wait for a person
    env["LC_ALL"] = env.get("LC_ALL", "C")
    try:
        proc = subprocess.run(
            ["git", *args], check=False, cwd=str(cwd), capture_output=True, text=True,
            encoding="utf-8", errors="replace", env=env,
        )
    except FileNotFoundError as exc:
        raise PraxisError("Praxis needs git, and git is not installed (or not on the PATH).") from exc
    if check and proc.returncode != 0:
        raise GitError(args, proc)
    return proc


def out(args: list[str], cwd: Path | str) -> str:
    return git(args, cwd).stdout.strip()


@dataclass
class Repo:
    root: Path     # the main project folder (the main working tree)
    common: Path   # the git folder shared by every worktree

    @property
    def state_dir(self) -> Path:
        return self.common / "praxis"

    @property
    def name(self) -> str:
        return self.root.name


def find_repo(start: Path | str | None = None) -> Repo:
    here = Path(start or os.getcwd())
    proc = git(["rev-parse", "--git-common-dir"], here, check=False)
    if proc.returncode != 0:
        raise PraxisError(
            f"{here} is not inside a git project. Open your project folder first "
            "(if it has never used git, run `git init` there)."
        )
    common = Path(proc.stdout.strip())
    if not common.is_absolute():
        common = (here / common)
    common = common.resolve()
    root = None
    for entry in worktrees_at(here):
        root = entry["worktree"]
        break
    if root is None or entry.get("bare"):
        raise PraxisError("This git project has no main folder (it is a 'bare' repository). Praxis needs a normal project folder.")
    return Repo(root=Path(root).resolve(), common=common)


def worktrees_at(cwd: Path | str) -> list[dict]:
    """Every working tree of the project, the main folder first."""
    entries: list[dict] = []
    current: dict = {}
    for line in git(["worktree", "list", "--porcelain"], cwd).stdout.splitlines():
        if not line.strip():
            if current:
                entries.append(current)
            current = {}
            continue
        key, _, value = line.partition(" ")
        current[key] = value if value else True
    if current:
        entries.append(current)
    return entries


def branch_checked_out_at(cwd: Path | str, branch: str) -> Path | None:
    for entry in worktrees_at(cwd):
        if entry.get("branch") == f"refs/heads/{branch}":
            return Path(entry["worktree"])
    return None


def rev(cwd: Path | str, ref: str) -> str | None:
    proc = git(["rev-parse", "--verify", "--quiet", f"{ref}^{{commit}}"], cwd, check=False)
    return proc.stdout.strip() or None


def branch_exists(cwd: Path | str, branch: str) -> bool:
    return rev(cwd, f"refs/heads/{branch}") is not None


def current_branch(cwd: Path | str) -> str | None:
    proc = git(["symbolic-ref", "--quiet", "--short", "HEAD"], cwd, check=False)
    return proc.stdout.strip() or None


def dirty_files(cwd: Path | str, untracked: bool = True) -> list[str]:
    """Changed, new or deleted files that are not committed yet (ignored files do not count).
    With untracked=False, only files git already tracks."""
    mode = "--untracked-files=all" if untracked else "--untracked-files=no"
    raw = git(["status", "--porcelain=v1", "-z", mode], cwd).stdout
    parts = raw.split("\0")
    files: list[str] = []
    i = 0
    while i < len(parts):
        item = parts[i]
        i += 1
        if len(item) < 4:
            continue
        files.append(item[3:])
        if item[0] in "RC":  # a rename carries its old name next
            i += 1
    return files


def identity_args(cwd: Path | str) -> list[str]:
    args: list[str] = []
    if not git(["config", "user.name"], cwd, check=False).stdout.strip():
        args += ["-c", "user.name=Praxis"]
    if not git(["config", "user.email"], cwd, check=False).stdout.strip():
        args += ["-c", "user.email=praxis@localhost"]
    return args


def commit(cwd: Path | str, message: str, paths: list[str] | None = None) -> bool:
    """Commit everything (or only *paths*). Returns False when there was nothing to commit."""
    if paths:
        git(["add", "--", *paths], cwd)
    else:
        git(["add", "-A"], cwd)
    staged = git(["diff", "--cached", "--quiet", *(["--", *paths] if paths else [])], cwd, check=False)
    has_head = rev(cwd, "HEAD") is not None
    if has_head and staged.returncode == 0:
        return False
    tail = ["--", *paths] if paths else []
    git([*identity_args(cwd), "commit", "-q", "-m", message, *tail], cwd)
    return True


def commits_ahead(cwd: Path | str, base: str, branch: str) -> int:
    return int(out(["rev-list", "--count", f"{base}..{branch}"], cwd) or 0)


def commits_not_in(cwd: Path | str, tips: list[str], base: list[str]) -> int:
    """How many commits reachable from any of *tips* are not reachable from any of *base*."""
    tips = [t for t in tips if t]
    if not tips:
        return 0
    return int(out(["rev-list", "--count", *tips, "--not", *[b for b in base if b]], cwd) or 0)


def is_ancestor(cwd: Path | str, older: str, newer: str) -> bool:
    return git(["merge-base", "--is-ancestor", older, newer], cwd, check=False).returncode == 0


def ignored_entries(cwd: Path | str) -> list[str]:
    """Ignored files and folders (a wholly ignored folder is one entry ending in '/')."""
    raw = git(["status", "--porcelain=v1", "-z", "--ignored"], cwd).stdout
    return [item[3:] for item in raw.split("\0") if item.startswith("!! ")]


def is_ignored(cwd: Path | str, path: str) -> bool:
    return git(["check-ignore", "-q", "--", path], cwd, check=False).returncode == 0


def branch_being_rebased(cwd: Path | str, branch: str) -> Path | None:
    """The folder where *branch* is in the middle of a rebase (git then shows that folder as 'detached')."""
    for entry in worktrees_at(cwd):
        folder = Path(entry["worktree"])
        if not folder.is_dir():
            continue
        for name in ("rebase-merge/head-name", "rebase-apply/head-name"):
            proc = git(["rev-parse", "--git-path", name], folder, check=False)
            path = Path(proc.stdout.strip())
            if not path.is_absolute():
                path = folder / path
            try:
                if path.read_text(encoding="utf-8").strip() == f"refs/heads/{branch}":
                    return folder
            except OSError:
                continue
    return None


def changed_files(cwd: Path | str, base: str, branch: str) -> list[str]:
    raw = git(["diff", "--name-only", "-z", f"{base}...{branch}"], cwd).stdout
    return [p for p in raw.split("\0") if p]


def conflicted_files(cwd: Path | str) -> list[str]:
    raw = git(["diff", "--name-only", "-z", "--diff-filter=U"], cwd, check=False).stdout
    return [p for p in raw.split("\0") if p]


def commit_time(cwd: Path | str, ref: str) -> float | None:
    proc = git(["log", "-1", "--format=%ct", ref], cwd, check=False)
    text = proc.stdout.strip()
    return float(text) if text.isdigit() else None


def operation_in_progress(cwd: Path | str) -> str | None:
    """'rebase' or 'merge' when one was left half-done in this folder."""
    for name, kind in (("rebase-merge", "rebase"), ("rebase-apply", "rebase"), ("MERGE_HEAD", "merge")):
        path = Path(out(["rev-parse", "--git-path", name], cwd))
        if not path.is_absolute():
            path = Path(cwd) / path
        if path.exists():
            return kind
    return None


def abort_operation(cwd: Path | str) -> None:
    kind = operation_in_progress(cwd)
    if kind:
        git([kind, "--abort"], cwd, check=False)
