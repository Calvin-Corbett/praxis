"""Starting, claiming, finishing and abandoning: everything about who works on what."""

from __future__ import annotations

import os
import re
import shutil
import sqlite3
import stat
import sys
import threading
import time
from dataclasses import dataclass
from pathlib import Path

from agent_praxis import config, generated, gitops, paths, processes
from agent_praxis.config import Config
from agent_praxis.errors import PraxisError
from agent_praxis.gitops import GitError, Repo
from agent_praxis.store import Store

_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,39}$")
_RESERVED = re.compile(r"^(con|prn|aux|nul|com[0-9]|lpt[0-9])(\..*)?$")
ARCHIVE_PREFIX = "praxis-saved/"
# While a landing tries an agent's commits joined into one, its original commits are kept under this name.
JOINING_PREFIX = "refs/praxis/joining/"
# The only ignored folders `done` deletes: caches an engine or tool rebuilds by itself, matched exactly. Engine
# caches count only at the top of an agent's folder, and only for the engine found there. Every other ignored
# file is moved to .git/praxis/kept instead, because "git ignores it" means "not shared", not "worthless".
ENGINE_CACHES = {
    "Unity": {"library", "temp", "obj"},
    "Unreal": {"intermediate", "deriveddatacache"},
    "Godot": {".godot", ".import"},
}
CACHES_ANYWHERE = {"node_modules", "__pycache__", ".pytest_cache", ".mypy_cache", ".ruff_cache"}
# Write a new folder's files with several workers at once (git's parallel checkout: the same files, faster).
PARALLEL_CHECKOUT = ["-c", "checkout.workers=0"]


@dataclass
class Ctx:
    repo: Repo
    cfg: Config
    store: Store
    say: callable = print


def check_name(name: str) -> str:
    low = name.lower()
    # Names Windows keeps for devices (con, nul, com1...), and dot patterns git refuses in branch names.
    reserved = _RESERVED.match(low) or low.endswith((".", ".lock")) or ".." in low
    if not _NAME.match(name) or reserved:
        raise PraxisError(
            f"{name!r} cannot be an agent name. Use letters, numbers, '-', '_' or '.', "
            "up to 40 characters, for example claude-1 (Windows keeps names like con, nul and com1 for itself)."
        )
    return low


def folder_state(ctx: Ctx, row, is_worktree: bool | None = None) -> str:
    """'ok' when the agent's folder is this project's working folder for it, 'missing' when it is gone (or only
    an empty folder is left), 'partial' when it is what is left of a removal Praxis itself started, 'foreign' when
    something else is there. Git commands only ever run in 'ok'. *is_worktree* is a `gitops.is_worktree_of`
    answer for the folder, when the caller already has one."""
    folder = Path(row["worktree"])
    if not folder.exists():
        return "missing"
    if folder.is_dir() and (gitops.is_worktree_of(folder, ctx.repo.common) if is_worktree is None else is_worktree):
        return "ok"
    try:
        if folder.is_dir() and not any(folder.iterdir()):
            return "missing"
    except OSError:
        pass
    # No .git link and no record in git looks the same whether git stopped part-way through a removal or the link
    # was deleted some other way and a later `git worktree prune` dropped the record. Only the first is a removal
    # cut short, and only Praxis knows it started one: anything else may still hold unsaved work.
    marked = ctx.store.removing(row["name"])
    if (marked and gitops.same_path(marked["folder"], folder) and folder.is_dir()
            and not (folder / ".git").exists()
            and not any(gitops.same_path(e["worktree"], folder) for e in gitops.worktrees_at(ctx.repo.root))):
        return "partial"
    return "foreign"


def _partial(row) -> PraxisError:
    return PraxisError(
        f"{row['worktree']} is what is left of {row['name']}'s folder after a removal that stopped part-way "
        "(usually because a program had a file in it open). git no longer treats it as a working folder, so "
        f"Praxis will not run git there.\nEverything {row['name']} had saved is still on its branch "
        f"{row['branch']}. Close the program, look through what is left of the folder, delete the folder yourself "
        f"when you are sure you do not need anything in it, then run `praxis done {row['name']}` (or "
        f"`praxis abandon {row['name']}`) again. Nothing was changed."
    )


def _foreign(row) -> PraxisError:
    return PraxisError(
        f"{row['worktree']} should be {row['name']}'s Praxis folder, but git does not see it as a working folder "
        "of this project. Praxis will not run git there: git could act on some other project that contains it. "
        "Nothing was changed.\n"
        "- If you moved or renamed the main project folder (or the agents' folder), run this in the main folder "
        f'to reconnect it: git worktree repair "{row["worktree"]}"\n'
        "- If git says the folder is owned by someone else, see `git config --global --add safe.directory`.\n"
        f"- Otherwise its .git link was removed. Move the folder somewhere safe (it may still hold {row['name']}'s "
        f"files), then run `praxis abandon {row['name']}`: whatever it saved on its branch is kept."
    )


def _usable_folder(ctx: Ctx, row) -> Path | None:
    """The agent's folder when git may run in it, None when it is gone. Raises when something else is there.
    An empty folder left behind (a removal that failed half-way) is cleared so it stops getting in the way."""
    folder = Path(row["worktree"])
    state = folder_state(ctx, row)
    if state == "foreign":
        raise _foreign(row)
    if state == "partial":
        raise _partial(row)
    if state == "missing":
        if folder.is_dir():
            try:
                folder.rmdir()  # only ever succeeds on an empty folder
            except OSError:
                pass
        gitops.git(["worktree", "prune"], ctx.repo.root, check=False)
        return None
    return folder


def agent_here(ctx: Ctx) -> str | None:
    """The agent whose folder the current folder is in, if any."""
    try:
        here = Path.cwd().resolve()
    except OSError:
        return None
    for row in ctx.store.agents():
        try:
            here.relative_to(Path(row["worktree"]).resolve())
            return row["name"]
        except (ValueError, OSError):
            continue
    return None


def refuse_other_agents_folder(ctx: Ctx, row, verb: str) -> None:
    """Stop `praxis <verb> b` typed in agent a's folder: that is almost always a typo for a's own name."""
    here = agent_here(ctx)
    if here and here != row["name"]:
        raise PraxisError(
            f"You are in {here}'s folder, but asked to {verb} {row['name']}. Nothing was done.\n"
            f"If you are {here}, use your own name: praxis {verb} {here}\n"
            f"To {verb} {row['name']} on purpose, run the command from the main project folder ({ctx.repo.root})."
        )


def _refuse_while_busy(ctx: Ctx, row, folder: Path | None, verb: str) -> FolderScan | None:
    """Before removing an agent's folder: it must not be landing, and nothing may be using the folder. Returns the
    look through the folder it took (on Windows), for the link check to reuse."""
    from agent_praxis.landing_queue import landing_now  # here, so the landing line can import this module
    if landing_now(ctx.store, row["name"]):
        raise PraxisError(
            f"{row['name']} is landing right now. Wait until that finishes, then run `praxis {verb} {row['name']}`. "
            "Nothing was changed."
        )
    if folder is None:
        return None
    try:
        Path.cwd().resolve().relative_to(folder.resolve())
        inside = True
    except (ValueError, OSError):
        inside = False
    if inside:
        raise PraxisError(
            f"You are inside {row['name']}'s folder, and a folder cannot be removed while you are in it. "
            f"Nothing was changed. Run it from the main project folder:\n"
            f'  cd "{ctx.repo.root}"\n  praxis {verb} {row["name"]}'
        )
    scan = scan_folder(folder) if os.name == "nt" else None  # one look serves this check and the link check
    busy = _folder_in_use(folder, scan=scan)  # Windows cannot remove a folder another program is using: ask first
    if busy is not None:
        raise PraxisError(
            f"Another program is using {row['name']}'s folder ({busy}): a terminal or file window open in it, an "
            "editor, or the game engine. Windows cannot remove a folder while it is in use. Close it, then run "
            f"`praxis {verb} {row['name']}` again. Nothing was changed."
        )
    return scan


IN_USE_LIMIT = 200_000  # a huge folder: stop asking after this many. A removal cut short is then "partial"


@dataclass
class FolderScan:
    """One walk through an agent's folder, never into .git and never through a link (Windows only)."""
    paths: list[str]      # every folder and file in walk order (folders before what is in them), up to the limit
    junctions: list[str]  # junctions to folders, relative to the folder, as `refuse_links_git_walks_into` wants


def _walk(top: str):
    """Like os.walk(top, topdown=True, followlinks=False), but with each name's os.DirEntry, whose details Windows
    hands over with the listing itself: asking whether an entry is a link then costs no extra call. Remove
    entries from the folder list in place to keep the walk out of them."""
    stack = [top]
    while stack:
        here = stack.pop()
        try:
            with os.scandir(here) as listing:
                entries = list(listing)
        except OSError:
            continue  # os.walk skips a folder it cannot list
        dirs, files = [], []
        for entry in entries:
            try:
                is_dir = entry.is_dir()
            except OSError:
                is_dir = False
            (dirs if is_dir else files).append(entry)
        yield here, dirs, files
        for entry in reversed(dirs):  # so the first folder is walked first, as os.walk does
            if not entry.is_symlink():
                stack.append(entry.path)


def scan_folder(folder: Path, limit: int | None = None) -> FolderScan:
    limit = IN_USE_LIMIT if limit is None else limit
    paths_seen: list[str] = []
    junctions: list[str] = []
    for here, dirs, files in _walk(str(folder)):
        keep = []
        for entry in dirs:
            if entry.name == ".git":
                continue
            if _entry_is_link(entry):  # never walk into it
                if _entry_reparse_tag(entry) == _JUNCTION:
                    junctions.append(Path(entry.path).relative_to(folder).as_posix())
                continue
            keep.append(entry)
        dirs[:] = keep
        if len(paths_seen) < limit:
            # Folders (a terminal's current folder) and files (an editor or engine holding one open) alike.
            paths_seen.append(here)
            paths_seen.extend(entry.path for entry in files)
            del paths_seen[limit:]
    return FolderScan(paths=paths_seen, junctions=junctions)


def _folder_in_use(folder: Path, limit: int | None = None, scan: FolderScan | None = None) -> str | None:
    """On Windows, the first folder or file inside *folder* that some program holds open (a terminal's current
    folder, a file open in an editor or the engine), or None. It only asks Windows for permission to delete each
    one; nothing is moved or changed. The asking is spread over a few threads (each answer is a wait on Windows),
    and the answer is still the first one in walk order."""
    if os.name != "nt":
        return None
    try:
        import ctypes
        from ctypes import wintypes
        k32 = ctypes.WinDLL("kernel32", use_last_error=True)
        k32.CreateFileW.restype = wintypes.HANDLE
        k32.CreateFileW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, wintypes.LPVOID,
                                    wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE]
        k32.CloseHandle.argtypes = [wintypes.HANDLE]
    except (OSError, AttributeError):
        return None
    invalid = wintypes.HANDLE(-1).value
    delete, share_all, open_existing, backup_semantics, open_reparse = 0x00010000, 0x7, 3, 0x02000000, 0x00200000
    limit = IN_USE_LIMIT if limit is None else limit
    paths_all = (scan or scan_folder(folder, limit)).paths[:limit]
    first = [len(paths_all)]  # the lowest busy position found so far
    lock = threading.Lock()

    def ask(start: int) -> None:
        for i in range(start, min(start + _IN_USE_BLOCK, len(paths_all))):
            if i > first[0]:
                return  # an earlier one is already busy
            handle = k32.CreateFileW(paths_all[i], delete, share_all, None, open_existing,
                                     backup_semantics | open_reparse, None)
            if handle == invalid or handle is None:
                if ctypes.get_last_error() == 32:  # ERROR_SHARING_VIOLATION: someone has it open
                    with lock:
                        first[0] = min(first[0], i)
                    return
            else:
                k32.CloseHandle(handle)

    starts = range(0, len(paths_all), _IN_USE_BLOCK)
    if len(starts) <= 1:
        for start in starts:
            ask(start)
    else:
        from concurrent.futures import ThreadPoolExecutor
        with ThreadPoolExecutor(max_workers=min(_IN_USE_THREADS, len(starts))) as pool:
            list(pool.map(ask, starts))  # blocks in order, so the earliest are asked first
    return paths_all[first[0]] if first[0] < len(paths_all) else None


_IN_USE_BLOCK = 256
_IN_USE_THREADS = 8


def need_agent(ctx: Ctx, name: str):
    row = ctx.store.agent(check_name(name))
    if row is None:
        known = ", ".join(a["name"] for a in ctx.store.agents()) or "none"
        raise PraxisError(
            f"There is no agent called {name} working right now (working now: {known}).\n"
            f'Start one with: praxis start {name} "<what it will do>" <files>'
        )
    return row


def _roots(ctx: Ctx, agent_row=None) -> list[Path]:
    roots = [ctx.repo.root]
    if agent_row is not None:
        roots.insert(0, Path(agent_row["worktree"]))
    roots += [Path(a["worktree"]) for a in ctx.store.agents()]
    return roots


def _conflicts(ctx: Ctx, db, agent: str, wanted: list[str]) -> list[tuple[str, str, str, str, int]]:
    """(wanted path, holder, held path, holder's task, is asset) for every overlap with another agent's claim."""
    found = []
    for other in db.execute("SELECT c.*, a.task FROM claims c JOIN agents a ON a.name = c.agent WHERE c.agent != ?",
                            (agent,)).fetchall():
        for path in wanted:
            if paths.overlaps(path, other["path"]):
                is_asset = int(bool(other["is_asset"]) or paths.is_asset(path, ctx.cfg.assets))
                found.append((path, other["agent"], other["path"], other["task"], is_asset))
    return found


def is_exclusive(cfg: Config, clash: tuple) -> bool:
    """True when this overlap must refuse: an asset file (never shared), or any file with exclusive_code on."""
    return cfg.exclusive_code or bool(clash[4])


def _split(cfg: Config, clashes: list[tuple]) -> tuple[list[tuple], list[tuple]]:
    """(overlaps that refuse, overlaps on shared code files that are only noted)."""
    refusing = [c for c in clashes if is_exclusive(cfg, c)]
    return refusing, [c for c in clashes if not is_exclusive(cfg, c)]


def _say_shared(ctx: Ctx, shared: list[tuple]) -> None:
    """Tell the agent who else is working on the code files it just claimed."""
    by_path: dict[str, list[str]] = {}
    for path, holder, _held, _task, _asset in shared:
        names = by_path.setdefault(path, [])
        if holder not in names:
            names.append(holder)
    for path, names in by_path.items():
        ctx.say(f"  {path} is also being edited by {' and '.join(names)}: your changes are checked against "
                "theirs when you land.")


def _refuse(agent: str, clashes: list[tuple[str, str, str, str, int]]) -> PraxisError:
    lines = []
    for path, holder, held, task, is_asset in clashes:
        what = "the asset file " if is_asset else ""
        lines.append(f"  - {path}: {holder} holds {what}{held} (working on: \"{task}\")")
    holders = sorted({c[1] for c in clashes})
    return PraxisError(
        f"{agent} cannot claim these, because another agent is already working on them:\n"
        + "\n".join(lines)
        + "\nDo not edit them. Pick other work, or wait until "
        + " and ".join(f"`praxis done {h}`" for h in holders)
        + " has been run."
    )


def _insert_claims(db, cfg: Config, agent: str, wanted: list[str]) -> list[str]:
    added = []
    for path in wanted:
        cur = db.execute("INSERT OR IGNORE INTO claims (agent, path, key, is_asset, since) VALUES (?, ?, ?, ?, ?)",
                         (agent, path, paths.key(path), int(paths.is_asset(path, cfg.assets)), time.time()))
        if cur.rowcount:
            added.append(path)
    return added


def _refuse_unknown_branch(ctx: Ctx, name: str, task: str, branch: str, wanted_raw: list[str]) -> None:
    """A branch with the agent's name that Praxis did not make for it may be someone else's work: never take it
    over silently."""
    made_for = ctx.store.row("SELECT agent FROM branches WHERE branch = ?", (branch,))
    if made_for is not None and made_for["agent"] == name:
        return
    files = "".join(f" {p}" for p in wanted_raw)
    raise PraxisError(
        f"A branch named {branch} already exists, and Praxis did not make it for {name}. Praxis will not take over "
        "a branch it did not make, in case it holds someone else's work. Nothing was changed.\n"
        f"Either pick another name for the agent, or, to carry on from that branch on purpose:\n"
        f"  praxis start {name} \"{task.strip()}\"{files} --reuse-branch"
    )


def start(ctx: Ctx, name: str, task: str, wanted_raw: list[str], reuse_branch: bool = False) -> Path:
    name = check_name(name)
    if not task.strip():
        raise PraxisError("Say what the agent will do, for example: praxis start claude-1 \"make the jump higher\"")
    if ctx.store.agent(name):
        row = ctx.store.agent(name)
        if folder_state(ctx, row) != "ok":
            raise PraxisError(
                f"{name} is still listed as working on \"{row['task']}\", but its folder {row['worktree']} is gone "
                f"or damaged.\nRun `praxis abandon {name}` first (anything it saved is kept on a branch), then start "
                "it again."
            )
        raise PraxisError(
            f"{name} is already working on \"{row['task']}\" in {row['worktree']}.\n"
            f"To add files: praxis claim {name} <files>. To finish first: praxis land {name}, then praxis done {name}."
        )
    main = ctx.cfg.main_branch
    if not gitops.rev(ctx.repo.root, f"refs/heads/{main}"):
        raise PraxisError(
            f"The main branch {main!r} does not exist yet. If this project has never been committed, commit its "
            "files first (git add -A, then git commit -m \"Project files\"); else fix main_branch in praxis.toml."
        )
    wanted = _dedupe([paths.normalize(p, _roots(ctx)) for p in wanted_raw])
    generated.refuse_claims(ctx.cfg, wanted)
    folder = ctx.cfg.worktree_dir(ctx.repo.root) / name
    branch = ctx.cfg.branch_prefix + name
    if gitops.branch_exists(ctx.repo.root, branch) and not reuse_branch:
        _refuse_unknown_branch(ctx, name, task, branch, wanted_raw)
    if folder.parent.exists() and not folder.parent.is_dir():
        raise PraxisError(f"Praxis makes agent folders in {folder.parent}, but a file has that name. Move or rename "
                          "that file (or set worktree_root in praxis.toml), then start again.")
    if folder.exists() and (not folder.is_dir() or any(folder.iterdir())):
        raise PraxisError(f"The folder {folder} already exists and is not empty. Move it away or pick another name.")
    now = time.time()
    try:
        with ctx.store.write() as db:  # claims first, so two agents racing for the same files cannot both get them
            lines, shared = _split(ctx.cfg, _conflicts(ctx, db, name, wanted))
            if lines:
                raise _refuse(name, lines)
            db.execute("INSERT INTO agents (name, task, branch, worktree, started, last_seen) "
                       "VALUES (?, ?, ?, ?, ?, ?)", (name, task.strip(), branch, str(folder), now, now))
            db.execute("INSERT OR REPLACE INTO branches (branch, agent, since) VALUES (?, ?, ?)", (branch, name, now))
            _insert_claims(db, ctx.cfg, name, wanted)
    except sqlite3.IntegrityError:  # another `praxis start` with the same name won the race
        raise PraxisError(f"{name} was started by someone else at the same moment. Pick another name.") from None
    made_branch = False
    gitops.agent_folder(folder, name, task.strip())
    try:
        folder.parent.mkdir(parents=True, exist_ok=True)
        if gitops.branch_exists(ctx.repo.root, branch):
            gitops.git([*PARALLEL_CHECKOUT, "worktree", "add", str(folder), branch], ctx.repo.root)
            ctx.say(f"Found earlier work on branch {branch}; {name} carries on from it.")
        else:
            made_branch = True
            gitops.git([*PARALLEL_CHECKOUT, "worktree", "add", "-b", branch, str(folder), f"refs/heads/{main}"],
                       ctx.repo.root)
    except BaseException as exc:
        with ctx.store.write() as db:
            db.execute("DELETE FROM claims WHERE agent = ?", (name,))
            db.execute("DELETE FROM agents WHERE name = ?", (name,))
            if made_branch:
                db.execute("DELETE FROM branches WHERE branch = ?", (branch,))
        if folder.is_dir() and not gitops.is_worktree_of(folder, ctx.repo.common):
            shutil.rmtree(folder, ignore_errors=True)  # a half-made checkout of committed files only
        gitops.git(["worktree", "prune"], ctx.repo.root, check=False)
        if made_branch and gitops.branch_exists(ctx.repo.root, branch) and not gitops.commits_not_in(
                ctx.repo.root, [branch], [f"refs/heads/{main}"]):
            gitops.git(["branch", "-D", branch], ctx.repo.root, check=False)  # it was made just now and holds nothing
        if isinstance(exc, GitError) and "too long" in str(exc).lower():
            raise PraxisError(
                f"Some file paths in this project are too long for Windows inside {folder}. Let git use long paths:\n"
                "  git config --global core.longpaths true\n"
                f"then start {name} again (or set a shorter worktree_root in praxis.toml). Nothing was kept."
            ) from exc
        raise
    if ctx.cfg.share:
        from agent_praxis import hooks
        hooks.link_shares(ctx, folder)
    ctx.say(f"{name} is ready to work on \"{task.strip()}\".")
    ctx.say(f"Its own folder is: {folder}")
    ctx.say("Work ONLY in that folder.")
    ctx.say(f"(The main project folder, for `praxis done` at the end, is: {ctx.repo.root})")
    _say_claims(ctx, name, wanted, folder)
    _say_shared(ctx, shared)
    from agent_praxis import identity
    for line in identity.warnings(ctx, {name: folder}):
        ctx.say(line)
    if not wanted:
        ctx.say(f"No files are claimed yet. Claim before editing: praxis claim {name} <files or folders>")
    waiting = gitops.dirty_files(ctx.repo.root)
    if waiting:
        ctx.say(f"Note: the main folder has {len(waiting)} file(s) that are not committed ({', '.join(waiting[:5])}"
                f"{' ...' if len(waiting) > 5 else ''}). Agent folders only get committed work, so {name} does not "
                "have them.")
    return folder


def _dedupe(items: list[str]) -> list[str]:
    seen, out = set(), []
    for item in items:
        if paths.key(item) not in seen:
            seen.add(paths.key(item))
            out.append(item)
    return out


def _say_claims(ctx: Ctx, name: str, added: list[str], folder: Path) -> None:
    main = f"refs/heads/{ctx.cfg.main_branch}"
    for path in added:
        kind = "asset file, locked to" if paths.is_asset(path, ctx.cfg.assets) else "claimed by"
        if path == "." or paths.exists_in(path, [folder]):
            new = ""
        elif gitops.git(["cat-file", "-e", f"{main}:{path}"], ctx.repo.root, check=False).returncode == 0:
            new = f", landed after {name} started: it arrives in {name}'s folder with its next landing"
        elif paths.exists_in(path, [ctx.repo.root]):
            new = (f", only in the main folder and never committed, so {name}'s folder does not have it: "
                   "commit it in the main folder first")
        else:
            new = ", does not exist yet: fine for a new file"
            near = _similar_path(path, folder)
            if near:
                new += f" (did you mean {near}?)"
        ctx.say(f"  {path}  ({kind} {name}{new})")


def _similar_path(path: str, folder: Path) -> str | None:
    """An existing file or folder whose path is very close to *path* (a likely typo), or None. Only the names in
    the folder that would hold *path* are compared, so this stays quick in a huge project."""
    import difflib  # only here: it is slow to import

    parent, _, leaf = path.rpartition("/")
    where = folder / parent if parent else folder
    try:
        names = [entry.name for entry in os.scandir(where) if entry.name != ".git"]
    except OSError:
        return None
    close = difflib.get_close_matches(leaf, names, n=1, cutoff=0.85)
    if not close or close[0].casefold() == leaf.casefold():
        return None
    return f"{parent}/{close[0]}" if parent else close[0]


def claim(ctx: Ctx, name: str, wanted_raw: list[str]) -> list[str]:
    row = need_agent(ctx, name)
    name = row["name"]
    refuse_other_agents_folder(ctx, row, "claim")
    if not wanted_raw:
        raise PraxisError(f"Say which files or folders to claim, for example: praxis claim {name} levels/level2.tscn")
    wanted = _dedupe([paths.normalize(p, _roots(ctx, row)) for p in wanted_raw])
    generated.refuse_claims(ctx.cfg, wanted)
    with ctx.store.write() as db:
        lines, shared = _split(ctx.cfg, _conflicts(ctx, db, name, wanted))
        if lines:
            raise _refuse(name, lines)
        added = _insert_claims(db, ctx.cfg, name, wanted)
        db.execute("UPDATE agents SET last_seen = ?, released = NULL WHERE name = ?", (time.time(), name))
    if added:
        ctx.say(f"{name} now holds:")
        _say_claims(ctx, name, added, Path(row["worktree"]))
    else:
        ctx.say(f"{name} already held all of those.")
    _say_shared(ctx, shared)
    return added


def unclaim(ctx: Ctx, name: str, wanted_raw: list[str]) -> None:
    row = need_agent(ctx, name)
    refuse_other_agents_folder(ctx, row, "unclaim")
    wanted = [paths.key(paths.normalize(p, _roots(ctx, row))) for p in wanted_raw]
    released, missing = [], []
    with ctx.store.write() as db:
        for k in wanted:
            gone = db.execute("DELETE FROM claims WHERE agent = ? AND key = ?", (row["name"], k)).rowcount
            (released if gone else missing).append(k)
    if released:
        ctx.say(f"{row['name']} let go of: {', '.join(released)}")
    if missing:
        ctx.say(f"{row['name']} did not hold: {', '.join(missing)} (nothing to let go of there)")


def unlanded(ctx: Ctx, row) -> tuple[int, list[str]]:
    """(commits not yet on the main branch, files changed but not saved) for an agent. Commits count whether they
    are on the agent's branch or only in its folder (made on another branch, or with no branch at all)."""
    folder = Path(row["worktree"])
    is_worktree, head = _probe(ctx, folder)
    usable = folder_state(ctx, row, is_worktree) == "ok"  # never run git in a folder that is not this agent's
    dirty = gitops.dirty_files(folder) if usable else []
    tips = [gitops.rev(ctx.repo.root, f"refs/heads/{row['branch']}")]
    if usable:
        tips.append(head)
    ahead = gitops.commits_not_in(ctx.repo.root, tips, [f"refs/heads/{ctx.cfg.main_branch}"])
    return ahead, dirty


def _probe(ctx: Ctx, folder: Path) -> tuple[bool | None, str | None]:
    """(is_worktree_of answer, commit checked out there) in one git call; (None, None) when folder_state would not
    ask git at all."""
    if not folder.is_dir():
        return None, None
    return gitops.worktree_probe(folder, ctx.repo.common, want_head=True)


@dataclass
class Look:
    """What `praxis status` shows about one agent's folder, read in one go."""
    is_worktree: bool | None
    ahead: int = 0
    dirty: list = None
    activity: float = 0.0
    error: BaseException | None = None  # reading past the folder check failed
    running: int = 0  # programs working inside its folder right now (a test run, a build)
    landing: bool = False  # a landing (and its check) for it is running or waiting in line


def busy(ctx: Ctx, rows: list, procs: list | None = None) -> dict[str, tuple[int, bool]]:
    """{agent: (programs running inside its folder, a landing running or in line)}, from ONE look at the
    process list for all agents (*procs*, when the caller already took it)."""
    from agent_praxis import background  # here: background imports this module
    from agent_praxis.landing_queue import landing_now
    procs = processes.look() if procs is None and rows else (procs or [])
    return {row["name"]: (len(processes.inside(procs, row["worktree"])),
                          landing_now(ctx.store, row["name"]) or background.running(ctx, row["name"]))
            for row in rows}


LOOK_THREADS = 8


def look_at_agents(ctx: Ctx, rows: list) -> dict[str, Look]:
    """{agent: Look} for every agent, with as few git calls as possible: one for all branch tips, then per agent
    one for its folder (is it this project's, and its commit), one `git status` and one count, in a few threads.
    The answers are the ones folder_state, unlanded and last_activity give."""
    if not rows:
        return {}
    now = time.time()
    working = busy(ctx, rows)  # first, so the git commands below (run in agents' folders) are never counted
    agent_branches = "refs/heads/" + ctx.cfg.branch_prefix
    tips = gitops.branch_tips(ctx.repo.root, [agent_branches])
    main_ref = f"refs/heads/{ctx.cfg.main_branch}"

    def read(row) -> Look:
        folder = Path(row["worktree"])
        try:
            is_worktree, head = _probe(ctx, folder)
        except Exception as exc:  # noqa: BLE001 - status shows the rest of the page anyway
            return Look(is_worktree=None, error=exc)
        look = Look(is_worktree=is_worktree, dirty=[])
        look.running, look.landing = working[row["name"]]
        try:
            usable = folder.exists() and folder.is_dir() and bool(is_worktree)  # folder_state's "ok"
            branch_ref = f"refs/heads/{row['branch']}"
            if branch_ref.startswith(agent_branches):
                tip, stamp = tips.get(branch_ref, (None, None))
            else:
                tip = gitops.rev(ctx.repo.root, branch_ref)
                stamp = gitops.commit_time(ctx.repo.root, row["branch"]) if tip else None
            look.dirty = gitops.dirty_files(folder) if usable else []
            look.ahead = gitops.commits_not_in(ctx.repo.root, [tip, *([head] if usable else [])], [main_ref])
            newest = float(row["last_seen"])
            if tip and stamp:
                # The branch's own unlanded count: the same number as above unless the folder is on another commit.
                own = look.ahead if not usable or head == tip else gitops.commits_ahead(ctx.repo.root, main_ref,
                                                                                         branch_ref)
                if own:
                    newest = max(newest, stamp)
            for rel in look.dirty[:500]:
                try:
                    newest = max(newest, (folder / rel).stat().st_mtime)
                except OSError:
                    pass
            look.activity = now if look.running or look.landing else newest
        except Exception as exc:  # noqa: BLE001
            look.error = exc
        return look

    if len(rows) == 1:
        return {rows[0]["name"]: read(rows[0])}
    from concurrent.futures import ThreadPoolExecutor
    with ThreadPoolExecutor(max_workers=min(LOOK_THREADS, len(rows))) as pool:
        return {row["name"]: look for row, look in zip(rows, pool.map(read, rows))}


def off_branch(row, folder: Path, say=None) -> str | None:
    """Why the agent's folder is not on its own branch, or None when it is. Praxis only lands, and only removes
    folders, from the agent's own branch, so work made anywhere else is never skipped or thrown away."""
    on = gitops.current_branch(folder)
    if on == row["branch"]:
        return None
    where = f"on the branch {on}" if on else "not on any branch (git calls this a 'detached HEAD')"
    tip = gitops.rev(folder, f"refs/heads/{row['branch']}")
    head = gitops.rev(folder, "HEAD")
    # Exactly the branch's files, only on a commit beside it (what a landing killed part-way through used to
    # leave). Going back to the branch changes no file in the folder, and unsaved edits stay as they are.
    if (on is None and tip and head and _tree(folder, head) == _tree(folder, tip)
            and gitops.git(["checkout", "-q", row["branch"]], folder, check=False).returncode == 0):
        if say:
            say(f"{row['name']}'s folder was not on any branch, but it held the same files as {row['branch']} "
                f"(a landing that was stopped part-way can leave it like this). Put it back on {row['branch']}; "
                "no file changed.")
        return None
    if tip and head and gitops.is_ancestor(folder, tip, head):
        fix = (f"Everything on {row['branch']} is also here, so in that folder run:\n"
               f"  git switch -C {row['branch']}\n"
               "That puts this work on the right branch without losing anything.")
    else:
        fix = (f"Its work there and its work on {row['branch']} have gone separate ways. This needs the human: "
               f"combine them on {row['branch']} (for example `git switch {row['branch']}` and then `git merge` the "
               "other work), or run `praxis abandon` to keep both on saved branches.")
    return f"{row['name']}'s folder {folder} is {where}, but its work must be on {row['branch']}.\n{fix}"


def _tree(folder: Path, commit: str) -> str | None:
    proc = gitops.git(["rev-parse", "--verify", "--quiet", f"{commit}^{{tree}}"], folder, check=False)
    return proc.stdout.strip() or None


def _remove_folder(ctx: Ctx, row, links_checked: bool = False, subs: list[str] | None = None) -> None:
    """Remove an agent's folder whose work is all landed or saved. Never with untracked or changed files in it:
    callers check that first, and ignored files are moved out (or are rebuildable caches) before this runs.
    *links_checked*: the caller ran refuse_links_git_walks_into on this folder just before, with nothing changed
    since. *subs*: the folder's submodules, when the caller just asked and nothing changed since."""
    folder = Path(row["worktree"])
    state = folder_state(ctx, row)  # git only ever runs in a folder that is really this agent's
    if state == "foreign":
        raise _foreign(row)
    if state == "partial":
        raise _partial(row)
    if state == "ok":
        if not links_checked:
            refuse_links_git_walks_into(row, folder)  # before anything is moved or deleted
        subs = gitops.submodules(folder) if subs is None else subs
        stranded = [s for s in subs if gitops.commits_only_here(folder / s, ctx.repo.root / s)]
        if stranded:
            raise PraxisError(
                f"These submodules in {row['name']}'s folder have commits that exist nowhere else: "
                + ", ".join(stranded) + ". Removing the folder would lose them. Push them first (in each "
                "submodule: `git push`), then run the same command again. Nothing was removed."
            )
        _refuse_unsaved_submodules(row, folder, subs)
        ctx.store.mark_removing(row["name"], str(folder))  # from here on, a cut-short removal is known to be ours
        try:
            _take_folder_away(ctx, row, folder, subs)
        except BaseException:
            if folder.is_dir() and gitops.is_worktree_of(folder, ctx.repo.common):
                ctx.store.clear_removing(row["name"])  # nothing was removed: the folder is an ordinary one again
            raise
    elif folder.is_dir():  # only an empty folder is left
        try:
            folder.rmdir()
        except OSError:
            pass
    gitops.git(["worktree", "prune"], ctx.repo.root, check=False)
    ctx.store.clear_removing(row["name"])


def _take_folder_away(ctx: Ctx, row, folder: Path, subs: list[str]) -> None:
    """The removal itself, once every check has passed: ignored files kept, submodules let go, folder removed."""
    dest = paths.fresh_folder(ctx.repo.state_dir / "kept" / f"{row['name']}-{time.strftime('%Y%m%d-%H%M%S')}")
    _unlink_shares(ctx, folder)  # first: they are links to the main folder's one copy, never this agent's files
    _keep_ignored(ctx, row, folder, dest)
    for sub in subs:  # deinit below deletes a submodule's files, ignored ones included
        _keep_ignored(ctx, row, folder / sub, dest / sub, label=f"{row['name']}'s submodule {sub}")
    if subs:  # git will not remove a folder with checked-out submodules; their work was checked above
        gitops.git(["submodule", "deinit", "--all", "--force"], folder, check=False)
    _unlink_links_inside(folder)  # git's own removal would step through a junction
    if subs:  # --force also skips git's own last look for changed files: take that look here instead
        late = [p for p in gitops.dirty_files(folder) if not any(paths.covers(s, p) for s in subs)]
        if late:
            raise PraxisError(f"Files changed in {row['name']}'s folder just now: {', '.join(late[:8])}. Is an "
                              "agent still working there? Nothing was removed. Run the same command again once "
                              "it has stopped.")
    # --force only gets past git's refusal about checked-out submodules, whose work was checked above.
    args = ["worktree", "remove", *(["--force"] if subs else []), str(folder)]
    proc = gitops.git(args, ctx.repo.root, check=False)
    if proc.returncode != 0 and folder.exists():
        said = (proc.stderr or proc.stdout or "").strip()
        if "locked" in said:
            why = (f"git has this folder marked as locked. If nothing is using it, run "
                   f"`git worktree unlock \"{folder}\"`, then the same command again.")
        elif any(w in said.lower() for w in ("permission", "access", "busy", "used by another")):
            why = ("A program (an editor, the game engine, a terminal) probably has files open in it. "
                   "Close it and run the same command again.")
        else:
            why = "Run the same command again after fixing what git says below."
        raise PraxisError(f"Could not remove the folder {folder}. {why}\ngit said: {said}")


def _unlink_shares(ctx: Ctx, folder: Path) -> None:
    """Remove the links `praxis start` made for praxis.toml's share folders, as links: what they point to (the
    main folder's copy) is never touched."""
    for rel in ctx.cfg.share:
        link = folder / rel.replace("\\", "/").strip("/")
        if _is_link(str(link)):
            try:
                _unlink(str(link))
            except OSError as exc:
                raise PraxisError(f"Could not remove the shared-folder link {link} ({exc}). Nothing it points to was "
                                  "touched. Close any program using it, then run the same command again.") from exc


def _refuse_unsaved_submodules(row, folder: Path, subs: list[str] | None = None) -> None:
    """Edits inside a submodule belong to the submodule's own git project: Praxis cannot save them for you."""
    subs = gitops.submodules(folder) if subs is None else subs
    unsaved = [s for s in subs if gitops.dirty_files(folder / s)]
    if unsaved:
        raise PraxisError(
            f"These submodules in {row['name']}'s folder have changes that are not committed inside them: "
            + ", ".join(unsaved) + ". Removing the folder would lose them, and Praxis cannot save changes "
            "inside a submodule for you. In each one, commit (and push) the changes, or undo them, then run "
            "the same command again. Nothing was removed."
        )


def _keep_ignored(ctx: Ctx, row, folder: Path, dest: Path, label: str | None = None) -> None:
    """Before a folder is removed: delete the caches tools rebuild, and move every other ignored file to *dest*,
    so nothing that might be someone's work disappears silently."""
    label = label or f"{row['name']}'s folder"
    engines, _assets, _check = config.detect(folder)
    top_caches = set().union(*(ENGINE_CACHES.get(e, set()) for e in engines)) if engines else set()
    rebuilt, kept = [], []
    for rel in gitops.ignored_entries(folder):
        clean = rel.rstrip("/")
        parts = clean.split("/")
        name = parts[-1].casefold()
        is_cache = (folder / clean).is_dir() and (
            (len(parts) == 1 and name in top_caches) or name in CACHES_ANYWHERE)
        (rebuilt if is_cache else kept).append(clean)
    if rebuilt:
        ctx.say("Removing caches that rebuild themselves: " + ", ".join(rebuilt[:8])
                + (f" and {len(rebuilt) - 8} more" if len(rebuilt) > 8 else ""))
        for rel in rebuilt:  # Praxis deletes these itself, so a link inside one never takes its target with it
            try:
                delete_without_following_links(folder / rel)
            except OSError as exc:
                raise PraxisError(
                    f"Could not delete the cache {rel} in {folder} ({exc}). A program probably has it open. Close "
                    "it and run the same command again. The folder was not removed."
                ) from exc
    if not kept:
        return
    for rel in kept:
        target = dest / rel
        try:
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.move(str(folder / rel), str(target))
        except OSError as exc:
            raise PraxisError(
                f"Could not move {rel} out of {folder} before removing the folder ({exc}). A program probably has "
                f"it open. Close it and run the same command again. The folder was not removed; anything already "
                f"moved is in {dest}"
            ) from exc
    more = f" and {len(kept) - 5} more" if len(kept) > 5 else ""
    ctx.say(f"{len(kept)} file(s) git ignores were in {label} ({', '.join(kept[:5])}{more}). "
            f"They were not deleted: they are kept in {dest}")
    ctx.say("Delete that folder when you no longer need them.")


_LINK_TAGS = {0xA000000C, 0xA0000003}  # Windows symbolic link, and mount point (a junction)
_JUNCTION, _NAME_SURROGATE = 0xA0000003, 0x20000000


def refuse_links_git_walks_into(row, folder: Path, scan: FolderScan | None = None) -> None:
    """On Windows, git treats a junction (what `mklink /J` and some tools make) as an ordinary folder: it would
    save the files it points to as part of the project, and removing the agent's folder would change or delete
    those real files. A junction git ignores is safe (Praxis moves it, as a link). Refuse before anything is
    committed, moved or deleted. *scan* is a look through the folder taken just before, to reuse."""
    if os.name != "nt":
        return  # elsewhere git stores a link as a link and never steps through it
    found = (scan or scan_folder(folder)).junctions
    if not found:
        return
    ignored = set(gitops.git(["check-ignore", "-z", "--stdin"], folder, check=False,
                             stdin="\0".join(found)).stdout.split("\0"))
    risky = [p for p in found if p not in ignored]
    if risky:
        raise PraxisError(
            f"{row['name']}'s folder has Windows junctions (links to folders elsewhere) that git does not ignore: "
            + ", ".join(risky[:8]) + ".\ngit would treat the files they point to as part of this project: it would "
            "save copies of them, and removing the folder would change or delete those real files. Nothing was "
            "changed.\nRemove each link itself (this does not touch what it points to), in the agent's folder:\n"
            + "\n".join(f'  rmdir "{folder / p}"' for p in risky[:8])
            + "\nor add it to .gitignore if it should stay. Then run the same command again."
        )


def _is_link(path: str) -> bool:
    """A symbolic link, or a Windows junction (as `npm link` and `npm install file:` make): something that points
    at a folder elsewhere, so deleting "inside" it deletes someone's real files. Other Windows reparse points
    (OneDrive files kept in the cloud, app launchers) are ordinary files and folders, and are not links."""
    try:
        info = os.lstat(path)
    except OSError:
        return False
    return _stat_is_link(info)


def _stat_is_link(info) -> bool:
    if stat.S_ISLNK(info.st_mode):
        return True
    tag = getattr(info, "st_reparse_tag", 0)
    return tag in _LINK_TAGS or bool(tag & _NAME_SURROGATE)  # the surrogate bit marks other kinds of link too


def _entry_is_link(entry: os.DirEntry) -> bool:
    """_is_link for a walk's entry. On Windows the answer comes with the folder listing (no call per entry);
    elsewhere a link is exactly a symbolic link, which the listing also says."""
    if os.name != "nt":
        return entry.is_symlink()
    try:
        info = entry.stat(follow_symlinks=False)
    except OSError:
        return False
    return _stat_is_link(info)


def _entry_reparse_tag(entry: os.DirEntry) -> int:
    try:
        return getattr(entry.stat(follow_symlinks=False), "st_reparse_tag", 0)
    except OSError:
        return 0


def _unlink(path: str) -> None:
    """Remove a link itself, never what it points to."""
    try:
        os.unlink(path)
    except OSError:
        os.rmdir(path)  # a Windows junction or folder link: rmdir removes only the link


def _writable(func, path, _exc) -> None:
    os.chmod(path, stat.S_IWRITE)  # read-only files (git objects, some packages) refuse deletion otherwise
    func(path)


def _unlink_links_inside(folder: Path) -> None:
    """Remove (as a link) every link in *folder* that git does not track, so deleting the folder can never reach
    files outside it. Links git tracks are left to git, which removes them as links. Removing an untracked link
    leaves git's own last check ("contains modified or untracked files") fully in force."""
    tracked = None  # what git tracks: asked only once a link is found
    for _here, dirs, files in _walk(str(folder)):
        dirs[:] = [entry for entry in dirs if entry.name != ".git"]
        for entry in list(dirs) + files:
            full = entry.path
            if _entry_is_link(entry):
                if entry in dirs:
                    dirs.remove(entry)  # never walk into it
                if tracked is None:
                    tracked = set(gitops.git(["ls-files", "-z"], folder).stdout.split("\0"))
                if Path(full).relative_to(folder).as_posix() in tracked:
                    continue
                try:
                    _unlink(full)
                except OSError as exc:
                    raise PraxisError(f"Could not remove the link {full} before removing the folder ({exc}). "
                                      "Nothing it points to was touched. Close any program using it, then run the "
                                      "same command again.") from exc


def delete_without_following_links(path: Path) -> None:
    """Delete a folder (or file) and everything in it, but delete links as links: never step through one."""
    top = str(path)
    if _is_link(top):
        _unlink(top)
        return
    if not path.is_dir():
        if path.exists():
            os.chmod(top, stat.S_IWRITE)
            os.unlink(top)
        return
    for _here, dirs, files in _walk(top):
        for entry in list(dirs):
            if _entry_is_link(entry):
                _unlink(entry.path)
                dirs.remove(entry)  # never walk into it
        for entry in files:
            if _entry_is_link(entry):
                _unlink(entry.path)
    retry = {"onexc": _writable} if sys.version_info >= (3, 12) else {"onerror": _writable}
    shutil.rmtree(top, **retry)  # no links are left inside


def _forget(ctx: Ctx, name: str) -> None:
    with ctx.store.write() as db:
        db.execute("DELETE FROM claims WHERE agent = ?", (name,))
        db.execute("DELETE FROM agents WHERE name = ?", (name,))
        db.execute("DELETE FROM queue WHERE agent = ?", (name,))
        db.execute("DELETE FROM removing WHERE agent = ?", (name,))
        db.execute("DELETE FROM branches WHERE agent = ?", (name,))


def done(ctx: Ctx, name: str) -> None:
    row = need_agent(ctx, name)
    refuse_other_agents_folder(ctx, row, "done")
    folder = _usable_folder(ctx, row)
    scan = _refuse_while_busy(ctx, row, folder, "done")
    subs = None
    if folder is not None:
        if gitops.operation_in_progress(folder):
            raise PraxisError(
                f"{row['name']}'s folder has a half-finished git rebase or merge. Finish it (fix the files, then "
                "`git rebase --continue`), or cancel it (`git rebase --abort`), then run the same command again. "
                "Nothing was removed."
            )
        problem = off_branch(row, folder, ctx.say)
        if problem:
            raise PraxisError(f"{problem}\nNothing was removed.")
        refuse_links_git_walks_into(row, folder, scan)  # first: git would count a junction's files as the agent's
        subs = gitops.submodules(folder)
        _refuse_unsaved_submodules(row, folder, subs)
    ahead, dirty = unlanded(ctx, row)
    if ahead or dirty:
        parts = []
        if ahead:
            parts.append(f"{ahead} saved change(s) that have not landed")
        if dirty:
            parts.append(f"{len(dirty)} file(s) changed but not saved: " + ", ".join(dirty[:8]))
        raise PraxisError(
            f"{row['name']} still has work that is not in the main version: " + "; ".join(parts) + ".\n"
            f"Nothing was removed. Land it first with `praxis land {row['name']}`, "
            f"or stop and keep a copy with `praxis abandon {row['name']}`."
        )
    # Only reads happened since the link and submodule checks above, so their answers still hold.
    _remove_folder(ctx, row, links_checked=folder is not None, subs=subs)
    gitops.git(["branch", "-D", row["branch"]], ctx.repo.root, check=False)
    gitops.git(["update-ref", "-d", f"{JOINING_PREFIX}{row['name']}"], ctx.repo.root, check=False)  # all landed
    _forget(ctx, row["name"])
    ctx.say(f"{row['name']} is done. Its claims and locks are released and its folder is removed.")
    from agent_praxis import hooks
    hooks.run_after_done(ctx, row)


def abandon(ctx: Ctx, name: str) -> str | None:
    """Stop an agent. Unlanded work is never deleted: it is saved on a branch, and the branch is named."""
    row = need_agent(ctx, name)
    refuse_other_agents_folder(ctx, row, "abandon")
    folder = _usable_folder(ctx, row)  # never commit in a folder that is not really this agent's
    scan = _refuse_while_busy(ctx, row, folder, "abandon")
    root, main = ctx.repo.root, f"refs/heads/{ctx.cfg.main_branch}"
    stamp = time.strftime("%Y%m%d-%H%M%S")
    saved: list[str] = []
    if folder is not None:
        _refuse_unsaved_submodules(row, folder)  # before anything changes: those edits cannot be saved for you
        refuse_links_git_walks_into(row, folder, scan)  # before committing: git would save what a junction points to
        gitops.abort_operation(folder)
        if gitops.dirty_files(folder):
            message = f"praxis: unfinished work by {row['name']}, saved when it was abandoned"
            gitops.commit(folder, ctx.cfg.with_trailers(message, row["name"], row["task"]))
        head = gitops.rev(folder, "HEAD")
        tip = gitops.rev(root, f"refs/heads/{row['branch']}")
        if head and head != tip and gitops.commits_not_in(root, [head], [main, tip]):
            # Work made off the agent's branch: give it a branch of its own BEFORE the folder goes
            # (or reuse the one an earlier, interrupted abandon already made for exactly this work).
            earlier = gitops.out(["branch", "--list", f"{ARCHIVE_PREFIX}{row['name']}-*-folder",
                                  "--points-at", head, "--format=%(refname:short)"], root).splitlines()
            other = earlier[0] if earlier else f"{ARCHIVE_PREFIX}{row['name']}-{stamp}-folder"
            if not earlier:
                gitops.git(["branch", other, head], root)
            saved.append(other)
    ahead, _ = unlanded(ctx, row)
    branch_ahead = gitops.commits_not_in(root, [gitops.rev(root, f"refs/heads/{row['branch']}")], [main])
    _remove_folder(ctx, row)
    if branch_ahead:
        on_branch = f"{ARCHIVE_PREFIX}{row['name']}-{stamp}"
        gitops.git(["branch", "-m", row["branch"], on_branch], root)
        saved.insert(0, on_branch)
    else:
        gitops.git(["branch", "-D", row["branch"]], root, check=False)
    # A joined try's copy of the original commits holds the same files as what was just saved (or landed).
    gitops.git(["update-ref", "-d", f"{JOINING_PREFIX}{row['name']}"], root, check=False)
    _forget(ctx, row["name"])
    gone = "its folder was already gone" if folder is None else "its folder is removed"
    ctx.say(f"{row['name']} has stopped. Its claims and locks are released and {gone}.")
    if saved:
        ctx.say(f"Its unfinished work ({ahead} saved change(s)) was NOT deleted. It is kept on the branch "
                f"{' and the branch '.join(saved)}.")
        ctx.say(f"To carry on from it: git branch -m {saved[0]} {row['branch']}  (in the main folder), then "
                f"praxis start {row['name']} \"<task>\" <files> --reuse-branch  picks it up.")
    else:
        ctx.say("It had no unfinished work, so nothing needed saving.")
    return saved[0] if saved else None


def blocked(ctx: Ctx, name: str, reason: str) -> None:
    row = need_agent(ctx, name)
    words = reason.strip() or "it says it is stuck and needs help"
    with ctx.store.write() as db:
        db.execute("UPDATE agents SET paused = ?, paused_at = ?, last_seen = ? WHERE name = ?",
                   (f"It asked for help: {words}", time.time(), time.time(), row["name"]))
    ctx.say(f"{row['name']} is paused until the human answers. It should stop working now.")
    ctx.say(f"The human can let it carry on with: praxis resume {row['name']}")


def resume(ctx: Ctx, name: str) -> None:
    row = need_agent(ctx, name)
    with ctx.store.write() as db:
        db.execute("UPDATE agents SET paused = NULL, paused_at = NULL, last_seen = ? WHERE name = ?",
                   (time.time(), row["name"]))
    ctx.say(f"{row['name']} may carry on. Tell it what you decided.")


def last_activity(ctx: Ctx, row) -> float:
    """The newest sign of work: a Praxis command, a saved change on its branch, or a changed file in its folder."""
    newest = float(row["last_seen"])
    folder = Path(row["worktree"])
    if gitops.branch_exists(ctx.repo.root, row["branch"]):
        stamp = gitops.commit_time(ctx.repo.root, row["branch"])
        if stamp and gitops.commits_ahead(ctx.repo.root, f"refs/heads/{ctx.cfg.main_branch}", f"refs/heads/{row['branch']}"):
            newest = max(newest, stamp)
    if folder_state(ctx, row) == "ok":
        for rel in gitops.dirty_files(folder)[:500]:
            try:
                newest = max(newest, (folder / rel).stat().st_mtime)
            except OSError:
                pass
    return newest


def stale_agents(ctx: Ctx, now: float | None = None, activity: dict[str, float] | None = None,
                 rows: list | None = None) -> list[tuple[object, float]]:
    """(agent row, hours quiet) for agents that still hold claims but have shown no sign of work for too long.
    *activity*: last_activity answers the caller already has, by agent. *rows*: only these agents."""
    now = now or time.time()
    activity = activity or {}
    found = []
    for row in ctx.store.agents() if rows is None else rows:
        if not ctx.store.claims(row["name"]):
            continue
        newest = activity[row["name"]] if row["name"] in activity else last_activity(ctx, row)
        quiet = (now - newest) / 3600
        if quiet >= ctx.cfg.stale_after_hours:
            found.append((row, quiet))
    return found


def release_stale(ctx: Ctx, names: list[str], force: bool = False) -> list[str]:
    """Release the claims and locks of stale agents (all of them, or only *names*). Their folders and work stay.
    An agent with programs running in its folder, edits not saved yet, or a landing in line is not released unless
    *force*: it may only look quiet."""
    wanted = {check_name(n) for n in names}
    released, refused = [], {}
    rows = [row for row in ctx.store.agents() if not wanted or row["name"] in wanted]
    working = {} if force else busy(ctx, rows)
    for row, quiet in stale_agents(ctx, rows=rows):
        why = [] if force else _still_busy(ctx, row, *working[row["name"]])
        if why:
            refused[row["name"]] = "; ".join(why)
            continue
        held = [c["path"] for c in ctx.store.claims(row["name"])]
        with ctx.store.write() as db:
            db.execute("DELETE FROM claims WHERE agent = ?", (row["name"],))
            db.execute("UPDATE agents SET released = ? WHERE name = ?",
                       (f"its claims were released after {quiet:.0f} hours with no sign of work", row["name"]))
        released.append(row["name"])
        ctx.say(f"Released {row['name']}'s claims ({', '.join(held)}); it had been quiet for {quiet:.0f} hours.")
        ctx.say(f"  Its folder and any unfinished work are kept: {row['worktree']}")
    for name in wanted - set(released) - set(refused):
        ctx.say(f"{name} is not stale, so nothing of it was released.")
    if refused:
        raise PraxisError(
            "Not released, because these agents may still be working:\n"
            + "\n".join(f"  - {name}: {why}" for name, why in refused.items())
            + "\nTheir claims are kept. Check on them first. If you are sure they have stopped, release them "
            "anyway with: praxis release-stale --force " + " ".join(refused))
    if not released and not wanted:
        ctx.say("No agent is stale. Nothing was released.")
    return released


def _still_busy(ctx: Ctx, row, running: int, landing: bool) -> list[str]:
    """Plain reasons an agent that looks quiet may still be working, or []."""
    why = []
    if running:
        why.append(f"{running} program(s) are running inside its folder")
    if landing:
        why.append("a landing of its work is running or waiting in line")
    if folder_state(ctx, row) == "ok":
        dirty = gitops.dirty_files(Path(row["worktree"]))
        if dirty:
            why.append(f"{len(dirty)} file(s) in its folder have edits not saved yet ({', '.join(dirty[:5])})")
    return why
