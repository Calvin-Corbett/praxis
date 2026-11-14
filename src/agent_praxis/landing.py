"""Landing: put one agent's finished work into the main version, but only if the project still works.

Steps, all while holding this agent's turn in the landing line:
1. Save (commit) anything the agent changed but did not commit.
2. Refuse if the agent changed files another agent holds.
3. Put the agent's work on top of the newest main version (rebase, or merge if praxis.toml says so).
4. Run the project's check command in the agent's folder.
5. Only if it passes, move the main branch forward to the agent's work.
A failure at any step leaves the agent's work exactly where it was and prints why.
"""

from __future__ import annotations

import os
import subprocess
import time
from pathlib import Path

from agent_praxis import enough, gitops, paths
from agent_praxis.agents import Ctx, done, need_agent
from agent_praxis.errors import PraxisError
from agent_praxis.landing_queue import Ticket

TAIL_LINES = 40
RETRIES_WHEN_MAIN_MOVES = 3


def land(ctx: Ctx, name: str, finish: bool = False) -> bool:
    row = need_agent(ctx, name)
    name = row["name"]
    if row["paused"]:
        raise PraxisError(
            f"{name} is paused, so it cannot land. Reason: {row['paused']}\n"
            f"It must wait for the human. The human lets it carry on with: praxis resume {name}"
        )
    folder = Path(row["worktree"])
    if not folder.is_dir():
        raise PraxisError(f"{name}'s folder {folder} is missing. Run `praxis abandon {name}` to keep what was saved.")
    if gitops.operation_in_progress(folder):
        raise PraxisError(
            f"{name}'s folder has a half-finished git rebase or merge. Finish it (fix the files, then "
            "`git rebase --continue`), or cancel it (`git rebase --abort`), then land again."
        )
    ctx.store.touch(name)
    if gitops.commit(folder, f"{row['task']} (by {name})"):
        ctx.say("Saved your changes as a commit.")
    if gitops.commits_ahead(ctx.repo.root, ctx.cfg.main_branch, row["branch"]) == 0:
        ctx.say(f"{name} has nothing new to land: its folder matches the main version.")
        if finish:
            done(ctx, name)
        return True
    with Ticket(ctx.store, name, ctx.say):
        ctx.say(f"It is {name}'s turn to land.")
        try:
            ok = _turn(ctx, row, folder)
        except BaseException:  # stopped half-way (Ctrl+C, a crash): undo the half-done rebase, keep the work
            gitops.abort_operation(folder)
            raise
    if ok and finish:
        done(ctx, name)
    return ok


def _turn(ctx: Ctx, row, folder: Path) -> bool:
    main, root = ctx.cfg.main_branch, ctx.repo.root
    for _ in range(RETRIES_WHEN_MAIN_MOVES):
        base = gitops.rev(root, f"refs/heads/{main}")
        clashes = _held_by_others(ctx, row, base)
        if clashes:
            return _fail(ctx, row, "claims", "it changed files another agent holds",
                         "These files are held by another agent, so this work cannot land:\n" + "\n".join(clashes)
                         + "\nUndo your changes to those files (or ask the human to sort it out), then land again.")
        problem = _integrate(ctx, row, folder)
        if problem is not None:  # a clash, already recorded and explained
            return problem
        log = None
        if ctx.cfg.check.strip():
            ctx.say(f"Running the check: {ctx.cfg.check}")
            passed, output, log = _run_check(ctx, row, folder)
            if not passed:
                count = enough.problem_count(output)
                tail = "\n".join(output.rstrip().splitlines()[-TAIL_LINES:])
                return _fail(ctx, row, "check", "the check failed" + (f" ({count} problem(s))" if count else ""),
                             f"The check failed, so nothing landed. The end of its output:\n{tail}\n"
                             f"Full output: {log}\nFix the problem in your folder, then land again.",
                             problems=count, log=log, sig_text=output)
        if gitops.rev(root, f"refs/heads/{main}") != base:
            ctx.say("The main version changed while the check ran. Doing it again on top of the newest version.")
            continue
        advanced = _advance(ctx, row, base)
        if advanced is not True:
            return _fail(ctx, row, "main-folder", "the main project folder had clashing unsaved changes", advanced)
        tip = gitops.rev(root, f"refs/heads/{main}")
        checked = "check passed" if ctx.cfg.check.strip() else "no check is set, so nothing was tested"
        with ctx.store.write() as db:
            db.execute("INSERT INTO landings (agent, task, at, ok, kind, summary, log, commit_id) "
                       "VALUES (?, ?, ?, 1, 'landed', ?, ?, ?)",
                       (row["name"], row["task"], time.time(), checked, log, tip))
        ctx.say(f"Landed: {row['name']}'s work on \"{row['task']}\" is now in {main} ({checked}).")
        ctx.say(f"When the task is finished: praxis done {row['name']}")
        return True
    return _fail(ctx, row, "busy-main", "the main version kept changing during the check",
                 f"The main version changed {RETRIES_WHEN_MAIN_MOVES} times while Praxis was checking. "
                 "Someone is committing to it directly, outside Praxis. Land again when that stops.")


def _held_by_others(ctx: Ctx, row, base: str) -> list[str]:
    changed = gitops.changed_files(ctx.repo.root, base, row["branch"])
    others = [c for c in ctx.store.claims() if c["agent"] != row["name"]]
    mine = ctx.store.claims(row["name"])
    lines, loose_assets = [], []
    for path in changed:
        holders = [c for c in others if paths.covers(c["path"], path)]
        if holders:
            lines.append(f"  - {path} (held by {holders[0]['agent']})")
        elif paths.is_asset(path, ctx.cfg.assets) and not any(paths.covers(c["path"], path) for c in mine):
            loose_assets.append(path)
    if loose_assets and not lines:
        ctx.say("Note: these asset files were changed without being claimed first: " + ", ".join(loose_assets))
        ctx.say("Nobody else holds them, so landing goes ahead. Next time claim asset files before editing them.")
    return lines


def _integrate(ctx: Ctx, row, folder: Path):
    """Put the agent's work on top of main. Returns None when that worked, else the result of a recorded failure."""
    main = ctx.cfg.main_branch
    if ctx.cfg.land_method == "merge":
        args = [*gitops.identity_args(folder), "merge", "--no-edit", "-q", main]
    else:
        args = [*gitops.identity_args(folder), "rebase", "-q", main]
    proc = gitops.git(args, folder, check=False)
    if proc.returncode == 0:
        return None
    clashing = gitops.conflicted_files(folder)
    gitops.abort_operation(folder)
    if not clashing:
        return _fail(ctx, row, "git", "git could not combine the work",
                     "git could not put your work on top of the newest main version:\n"
                     + (proc.stderr or proc.stdout).strip() + "\nYour work is kept, unchanged.")
    assets = [p for p in clashing if paths.is_asset(p, ctx.cfg.assets)]
    advice = (f"Your work is safe and unchanged. In your folder, run `git {ctx.cfg.land_method} {main}`, fix the "
              f"files listed above, finish with `git {ctx.cfg.land_method} --continue`"
              f"{'' if ctx.cfg.land_method == 'rebase' else ' (or commit)'}, then land again.")
    if assets:
        advice += ("\nAsset files cannot be merged: " + ", ".join(assets) + ". Take the newest version of each and "
                   "redo your change to it, and claim asset files before editing next time.")
    return _fail(ctx, row, "conflict", f"its changes clash with newer work in {len(clashing)} file(s)",
                 "Your changes clash with work that landed while you were working, in:\n"
                 + "\n".join(f"  - {p}" for p in clashing) + "\n" + advice,
                 problems=len(clashing), sig_text="\n".join(sorted(clashing)))


def _run_check(ctx: Ctx, row, folder: Path) -> tuple[bool, str, str]:
    env = dict(os.environ, PRAXIS_AGENT=row["name"])
    proc = subprocess.run(ctx.cfg.check, shell=True, cwd=str(folder), env=env,
                          stdout=subprocess.PIPE, stderr=subprocess.STDOUT)  # no time limit, on purpose
    output = proc.stdout.decode("utf-8", errors="replace")
    logs = ctx.store.dir / "logs"
    logs.mkdir(parents=True, exist_ok=True)
    log = logs / f"{row['name']}-{time.strftime('%Y%m%d-%H%M%S')}.log"
    log.write_text(f"$ {ctx.cfg.check}\n(exit code {proc.returncode})\n\n{output}", encoding="utf-8")
    return proc.returncode == 0, output, str(log)


def _advance(ctx: Ctx, row, base: str):
    """Move main to the agent's work. True when done, else the plain reason it could not."""
    main = ctx.cfg.main_branch
    target = gitops.rev(ctx.repo.root, f"refs/heads/{row['branch']}")
    where = gitops.branch_checked_out_at(ctx.repo.root, main)
    if where is None:
        gitops.git(["update-ref", f"refs/heads/{main}", target, base], ctx.repo.root)
        return True
    proc = gitops.git(["merge", "--ff-only", "-q", target], where, check=False)
    if proc.returncode == 0:
        return True
    return ("Your work passed, but it could not be put into the main project folder, because that folder has "
            f"unsaved changes to the same files ({where}). Praxis did not overwrite them.\n"
            f"git said: {(proc.stderr or proc.stdout).strip()}\n"
            "This needs the human: save (commit) or undo the changes made directly in the main folder, then land again.")


def _fail(ctx: Ctx, row, kind: str, summary: str, detail: str, problems: int | None = None,
          log: str | None = None, sig_text: str | None = None) -> bool:
    sig = enough.signature(f"{kind}\n{sig_text if sig_text is not None else detail}")
    with ctx.store.write() as db:
        db.execute("INSERT INTO landings (agent, task, at, ok, kind, summary, problems, signature, log) "
                   "VALUES (?, ?, ?, 0, ?, ?, ?, ?, ?)",
                   (row["name"], row["task"], time.time(), kind, summary, problems, sig, log))
    ctx.say(f"NOT landed: {summary}.")
    ctx.say(detail)
    ctx.say(f"{row['name']}'s work is kept in {row['worktree']}.")
    recent = ctx.store.rows("SELECT * FROM landings WHERE agent = ? AND at >= ? ORDER BY id DESC LIMIT 50",
                            (row["name"], row["started"]))
    reason = enough.should_pause(recent, ctx.cfg.enough_after)
    if reason:
        with ctx.store.write() as db:
            db.execute("UPDATE agents SET paused = ?, paused_at = ? WHERE name = ?", (reason, time.time(), row["name"]))
        ctx.say("")
        ctx.say(f"ENOUGH: {row['name']} is now paused. {reason}")
        ctx.say("Stop working on this and tell the human. Trying again the same way will not help.")
        ctx.say(f"The human lets it carry on with: praxis resume {row['name']}")
    return False
