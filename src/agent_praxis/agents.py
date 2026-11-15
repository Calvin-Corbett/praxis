"""Starting, claiming, finishing and abandoning: everything about who works on what."""

from __future__ import annotations

import re
import shutil
import sqlite3
import time
from dataclasses import dataclass
from pathlib import Path

from agent_praxis import config, gitops, paths
from agent_praxis.config import Config
from agent_praxis.errors import PraxisError
from agent_praxis.gitops import Repo
from agent_praxis.store import Store

_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,39}$")
BRANCH_PREFIX = "praxis/"
ARCHIVE_PREFIX = "praxis-saved/"
# The only ignored folders `done` deletes: caches an engine or tool rebuilds by itself, matched exactly. Engine
# caches count only at the top of an agent's folder, and only for the engine found there. Every other ignored
# file is moved to .git/praxis/kept instead, because "git ignores it" means "not shared", not "worthless".
ENGINE_CACHES = {
    "Unity": {"library", "temp", "obj"},
    "Unreal": {"intermediate", "deriveddatacache"},
    "Godot": {".godot", ".import"},
}
CACHES_ANYWHERE = {"node_modules", "__pycache__", ".pytest_cache", ".mypy_cache", ".ruff_cache"}


@dataclass
class Ctx:
    repo: Repo
    cfg: Config
    store: Store
    say: callable = print


def check_name(name: str) -> str:
    if not _NAME.match(name) or name.lower().endswith(".lock"):
        raise PraxisError(
            f"{name!r} cannot be an agent name. Use letters, numbers, '-', '_' or '.', "
            "up to 40 characters, for example claude-1."
        )
    return name.lower()


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
    """(wanted path, holder, held path, holder's task, is asset) for every clash with another agent's claim."""
    found = []
    for other in db.execute("SELECT c.*, a.task FROM claims c JOIN agents a ON a.name = c.agent WHERE c.agent != ?",
                            (agent,)).fetchall():
        for path in wanted:
            if paths.overlaps(path, other["path"]):
                found.append((path, other["agent"], other["path"], other["task"], other["is_asset"]))
    return found


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


def start(ctx: Ctx, name: str, task: str, wanted_raw: list[str]) -> Path:
    name = check_name(name)
    if not task.strip():
        raise PraxisError("Say what the agent will do, for example: praxis start claude-1 \"make the jump higher\"")
    if ctx.store.agent(name):
        row = ctx.store.agent(name)
        raise PraxisError(
            f"{name} is already working on \"{row['task']}\" in {row['worktree']}.\n"
            f"To add files: praxis claim {name} <files>. To finish first: praxis land {name}, then praxis done {name}."
        )
    main = ctx.cfg.main_branch
    if not gitops.rev(ctx.repo.root, f"refs/heads/{main}"):
        raise PraxisError(f"The main branch {main!r} does not exist. Fix main_branch in praxis.toml.")
    wanted = _dedupe([paths.normalize(p, _roots(ctx)) for p in wanted_raw])
    folder = ctx.cfg.worktree_dir(ctx.repo.root) / name
    branch = BRANCH_PREFIX + name
    if folder.exists() and any(folder.iterdir()):
        raise PraxisError(f"The folder {folder} already exists and is not empty. Move it away or pick another name.")
    now = time.time()
    try:
        with ctx.store.write() as db:  # claims first, so two agents racing for the same files cannot both get them
            lines = _conflicts(ctx, db, name, wanted)
            if lines:
                raise _refuse(name, lines)
            db.execute("INSERT INTO agents (name, task, branch, worktree, started, last_seen) "
                       "VALUES (?, ?, ?, ?, ?, ?)", (name, task.strip(), branch, str(folder), now, now))
            _insert_claims(db, ctx.cfg, name, wanted)
    except sqlite3.IntegrityError:  # another `praxis start` with the same name won the race
        raise PraxisError(f"{name} was started by someone else at the same moment. Pick another name.") from None
    try:
        folder.parent.mkdir(parents=True, exist_ok=True)
        if gitops.branch_exists(ctx.repo.root, branch):
            gitops.git(["worktree", "add", str(folder), branch], ctx.repo.root)
            ctx.say(f"Found earlier work on branch {branch}; {name} carries on from it.")
        else:
            gitops.git(["worktree", "add", "-b", branch, str(folder), main], ctx.repo.root)
    except BaseException:
        with ctx.store.write() as db:
            db.execute("DELETE FROM claims WHERE agent = ?", (name,))
            db.execute("DELETE FROM agents WHERE name = ?", (name,))
        raise
    ctx.say(f"{name} is ready to work on \"{task.strip()}\".")
    ctx.say(f"Its own folder is: {folder}")
    ctx.say("Work ONLY in that folder.")
    _say_claims(ctx, name, wanted)
    if not wanted:
        ctx.say(f"No files are claimed yet. Claim before editing: praxis claim {name} <files or folders>")
    return folder


def _dedupe(items: list[str]) -> list[str]:
    seen, out = set(), []
    for item in items:
        if paths.key(item) not in seen:
            seen.add(paths.key(item))
            out.append(item)
    return out


def _say_claims(ctx: Ctx, name: str, added: list[str]) -> None:
    roots = _roots(ctx)
    for path in added:
        kind = "asset file, locked to" if paths.is_asset(path, ctx.cfg.assets) else "claimed by"
        new = "" if paths.exists_in(path, roots) else ", does not exist yet: fine for a new file, else check the name"
        ctx.say(f"  {path}  ({kind} {name}{new})")


def claim(ctx: Ctx, name: str, wanted_raw: list[str]) -> list[str]:
    row = need_agent(ctx, name)
    name = row["name"]
    if not wanted_raw:
        raise PraxisError(f"Say which files or folders to claim, for example: praxis claim {name} levels/level2.tscn")
    wanted = _dedupe([paths.normalize(p, _roots(ctx, row)) for p in wanted_raw])
    with ctx.store.write() as db:
        lines = _conflicts(ctx, db, name, wanted)
        if lines:
            raise _refuse(name, lines)
        added = _insert_claims(db, ctx.cfg, name, wanted)
        db.execute("UPDATE agents SET last_seen = ?, released = NULL WHERE name = ?", (time.time(), name))
    if added:
        ctx.say(f"{name} now holds:")
        _say_claims(ctx, name, added)
    else:
        ctx.say(f"{name} already held all of those.")
    return added


def unclaim(ctx: Ctx, name: str, wanted_raw: list[str]) -> None:
    row = need_agent(ctx, name)
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
    dirty = gitops.dirty_files(folder) if folder.is_dir() else []
    tips = [gitops.rev(ctx.repo.root, f"refs/heads/{row['branch']}")]
    if folder.is_dir():
        tips.append(gitops.rev(folder, "HEAD"))
    ahead = gitops.commits_not_in(ctx.repo.root, tips, [f"refs/heads/{ctx.cfg.main_branch}"])
    return ahead, dirty


def off_branch(row, folder: Path) -> str | None:
    """Why the agent's folder is not on its own branch, or None when it is. Praxis only lands, and only removes
    folders, from the agent's own branch, so work made anywhere else is never skipped or thrown away."""
    on = gitops.current_branch(folder)
    if on == row["branch"]:
        return None
    where = f"on the branch {on}" if on else "not on any branch (git calls this a 'detached HEAD')"
    tip = gitops.rev(folder, f"refs/heads/{row['branch']}")
    head = gitops.rev(folder, "HEAD")
    if tip and head and gitops.is_ancestor(folder, tip, head):
        fix = (f"Everything on {row['branch']} is also here, so in that folder run:\n"
               f"  git switch -C {row['branch']}\n"
               "That puts this work on the right branch without losing anything.")
    else:
        fix = (f"Its work there and its work on {row['branch']} have gone separate ways. This needs the human: "
               f"combine them on {row['branch']} (for example `git switch {row['branch']}` and then `git merge` the "
               "other work), or run `praxis abandon` to keep both on saved branches.")
    return f"{row['name']}'s folder {folder} is {where}, but its work must be on {row['branch']}.\n{fix}"


def _remove_folder(ctx: Ctx, row) -> None:
    """Remove an agent's folder whose work is all landed or saved. Never with untracked or changed files in it:
    callers check that first, and ignored files are moved out (or are rebuildable caches) before this runs."""
    folder = Path(row["worktree"])
    subs: list[str] = []
    if folder.exists():
        if folder.is_dir():
            subs = gitops.submodules(folder)
            stranded = [s for s in subs if gitops.commits_only_here(folder / s, ctx.repo.root / s)]
            if stranded:
                raise PraxisError(
                    f"These submodules in {row['name']}'s folder have commits that exist nowhere else: "
                    + ", ".join(stranded) + ". Removing the folder would lose them. Push them first (in each "
                    "submodule: `git push`), then run the same command again. Nothing was removed."
                )
            _refuse_unsaved_submodules(row, folder, subs)
            dest = paths.fresh_folder(ctx.repo.state_dir / "kept" / f"{row['name']}-{time.strftime('%Y%m%d-%H%M%S')}")
            _keep_ignored(ctx, row, folder, dest)
            for sub in subs:  # deinit below deletes a submodule's files, ignored ones included
                _keep_ignored(ctx, row, folder / sub, dest / sub, label=f"{row['name']}'s submodule {sub}")
            if subs:  # git will not remove a folder with checked-out submodules; their work was checked above
                gitops.git(["submodule", "deinit", "--all", "--force"], folder, check=False)
        # --force only gets past git's "contains submodules" refusal: the folder was checked clean above.
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
    gitops.git(["worktree", "prune"], ctx.repo.root, check=False)


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


def _forget(ctx: Ctx, name: str) -> None:
    with ctx.store.write() as db:
        db.execute("DELETE FROM claims WHERE agent = ?", (name,))
        db.execute("DELETE FROM agents WHERE name = ?", (name,))
        db.execute("DELETE FROM queue WHERE agent = ?", (name,))


def done(ctx: Ctx, name: str) -> None:
    row = need_agent(ctx, name)
    folder = Path(row["worktree"])
    if folder.is_dir():
        if gitops.operation_in_progress(folder):
            raise PraxisError(
                f"{row['name']}'s folder has a half-finished git rebase or merge. Finish it (fix the files, then "
                "`git rebase --continue`), or cancel it (`git rebase --abort`), then run the same command again. "
                "Nothing was removed."
            )
        problem = off_branch(row, folder)
        if problem:
            raise PraxisError(f"{problem}\nNothing was removed.")
        _refuse_unsaved_submodules(row, folder)
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
    _remove_folder(ctx, row)
    gitops.git(["branch", "-D", row["branch"]], ctx.repo.root, check=False)
    _forget(ctx, row["name"])
    ctx.say(f"{row['name']} is done. Its claims and locks are released and its folder is removed.")


def abandon(ctx: Ctx, name: str) -> str | None:
    """Stop an agent. Unlanded work is never deleted: it is saved on a branch, and the branch is named."""
    row = need_agent(ctx, name)
    folder = Path(row["worktree"])
    root, main = ctx.repo.root, f"refs/heads/{ctx.cfg.main_branch}"
    stamp = time.strftime("%Y%m%d-%H%M%S")
    saved: list[str] = []
    if folder.is_dir():
        _refuse_unsaved_submodules(row, folder)  # before anything changes: those edits cannot be saved for you
        gitops.abort_operation(folder)
        if gitops.dirty_files(folder):
            gitops.commit(folder, f"praxis: unfinished work by {row['name']}, saved when it was abandoned")
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
    _forget(ctx, row["name"])
    ctx.say(f"{row['name']} has stopped. Its claims and locks are released and its folder is removed.")
    if saved:
        ctx.say(f"Its unfinished work ({ahead} saved change(s)) was NOT deleted. It is kept on the branch "
                f"{' and the branch '.join(saved)}.")
        ctx.say("To pick it up again, ask an agent to look at that branch.")
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
        if stamp and gitops.commits_ahead(ctx.repo.root, ctx.cfg.main_branch, row["branch"]):
            newest = max(newest, stamp)
    if folder.is_dir():
        for rel in gitops.dirty_files(folder)[:500]:
            try:
                newest = max(newest, (folder / rel).stat().st_mtime)
            except OSError:
                pass
    return newest


def stale_agents(ctx: Ctx, now: float | None = None) -> list[tuple[object, float]]:
    """(agent row, hours quiet) for agents that still hold claims but have shown no sign of work for too long."""
    now = now or time.time()
    found = []
    for row in ctx.store.agents():
        if not ctx.store.claims(row["name"]):
            continue
        quiet = (now - last_activity(ctx, row)) / 3600
        if quiet >= ctx.cfg.stale_after_hours:
            found.append((row, quiet))
    return found


def release_stale(ctx: Ctx, names: list[str]) -> list[str]:
    """Release the claims and locks of stale agents (all of them, or only *names*). Their folders and work stay."""
    wanted = {check_name(n) for n in names}
    released = []
    for row, quiet in stale_agents(ctx):
        if wanted and row["name"] not in wanted:
            continue
        held = [c["path"] for c in ctx.store.claims(row["name"])]
        with ctx.store.write() as db:
            db.execute("DELETE FROM claims WHERE agent = ?", (row["name"],))
            db.execute("UPDATE agents SET released = ? WHERE name = ?",
                       (f"its claims were released after {quiet:.0f} hours with no sign of work", row["name"]))
        released.append(row["name"])
        ctx.say(f"Released {row['name']}'s claims ({', '.join(held)}); it had been quiet for {quiet:.0f} hours.")
        ctx.say(f"  Its folder and any unfinished work are kept: {row['worktree']}")
    for name in wanted - set(released):
        ctx.say(f"{name} is not stale, so nothing of it was released.")
    if not released and not wanted:
        ctx.say("No agent is stale. Nothing was released.")
    return released
