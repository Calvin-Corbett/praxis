"""Landing: put one agent's finished work into the main version, but only if the project still works.

Steps, all while holding this agent's turn in the landing line:
1. Save (commit) anything the agent changed but did not commit.
2. Refuse if the agent changed asset files another agent holds (code files are shared: see step 3).
3. Put the agent's work on top of the newest main version (rebase, or merge if praxis.toml says so). This is the
   clash check for shared code files: when git cannot combine two agents' edits, nothing lands and the agent is
   told which files clash, and whose landing they clash with. Then check again what landing would really change in
   main: it must not touch asset files another agent holds.
4. Run the project's check command in the agent's folder.
5. Only if it passes, and the folder still holds exactly the commit that was checked, move the main branch
   forward to the agent's work. Main only ever moves forward: it never loses a commit that already landed.
A failure at any step lands nothing and prints why. The agent's saved work is always kept (after step 3 it sits
on top of the newest main version).
"""

from __future__ import annotations

import locale
import os
import shutil
import subprocess
import time
from pathlib import Path

from agent_praxis import checkcache, enough, generated, gitops, hooks, landing_batch, paths, shells
from agent_praxis.agents import (
    JOINING_PREFIX,
    PARALLEL_CHECKOUT,
    Ctx,
    _foreign,
    _partial,
    _tree,
    agent_here,
    done,
    folder_state,
    need_agent,
    off_branch,
    refuse_links_git_walks_into,
    refuse_other_agents_folder,
)
from agent_praxis.errors import PraxisError
from agent_praxis.landing_queue import Ticket, landing_now, read_note

TAIL_LINES = 40
TAIL_BYTES = 256 * 1024  # how much of the end of a check's output is read back (the rest stays in the log)
KEEP_LOGS = 200
KEEP_LOG_BYTES = 1024 ** 3  # the check logs together stay under this (oldest go first), apart from the newest one
HEARTBEAT_SECONDS = 60  # while a check runs, say it is still running this often, so nobody thinks it hung
RETRIES_WHEN_MAIN_MOVES = 3
NEEDS_THE_HUMAN = 3  # a check's exit code saying: the problem is not this agent's doing, a person must look
MAIN_MOVED = "main moved"  # _advance's answer when another writer moved main just before Praxis could


def land(ctx: Ctx, name: str, finish: bool = False) -> bool:
    row = need_agent(ctx, name)
    name = row["name"]
    refuse_other_agents_folder(ctx, row, "land")
    if row["paused"]:
        raise PraxisError(
            f"{name} is paused, so it cannot land. Reason: {row['paused']}\n"
            f"It must wait for the human. The human lets it carry on with: praxis resume {name}"
        )
    folder = Path(row["worktree"])
    state = folder_state(ctx, row)
    if state == "foreign":
        raise _foreign(row)
    if state == "partial":
        raise _partial(row)
    if state == "missing":
        raise PraxisError(f"{name}'s folder {folder} is missing. Run `praxis abandon {name}` to keep what was saved.")
    if gitops.operation_in_progress(folder):
        raise PraxisError(
            f"{name}'s folder has a half-finished git rebase or merge. Finish it (fix the files, then "
            "`git rebase --continue`), or cancel it (`git rebase --abort`), then land again."
        )
    landing_batch.restore_folder(ctx, row, folder)  # a batch this agent headed was stopped: put its folder back
    problem = off_branch(row, folder, ctx.say)
    if problem:
        raise PraxisError(f"{problem}\nNothing was landed.")
    if landing_now(ctx.store, name):
        raise PraxisError(f"{name} is already landing (in another window or process). Wait for that to finish; "
                          "nothing was changed.")
    _refuse_embedded_projects(row, folder)
    refuse_links_git_walks_into(row, folder)  # before committing: git would save what a junction points to
    ctx.store.touch(name)
    try:
        if gitops.commit(folder, ctx.cfg.with_trailers(f"{row['task']} (by {name})", name, row["task"])):
            ctx.say("Saved your changes as a commit.")
    except gitops.GitError as exc:
        return _fail(ctx, row, "commit", "git would not save the changes as a commit",
                     "git refused to commit your changes, so nothing landed. This is often a pre-commit hook "
                     f"in the project that failed. What git said:\n{exc}\nFix that, then land again.")
    _refuse_pointer_commits(ctx, row, folder)
    main_ref = f"refs/heads/{ctx.cfg.main_branch}"
    if gitops.commits_ahead(ctx.repo.root, main_ref, f"refs/heads/{row['branch']}") == 0:
        ctx.say(f"{name} has nothing new to land: its folder matches the main version.")
        if finish:
            _finish(ctx, row)
        return True
    progress: dict = {}
    with Ticket(ctx.store, name, ctx.say) as ticket:
        landing_batch.recover(ctx)  # a stopped batch: what it landed is recorded, and its waiting agents are told
        ticket.verdict = ticket.verdict or _done_note(ctx, ticket)
        if ticket.verdict is not None:  # the agent ahead landed (or refused) this agent's work in its batch
            ok = landing_batch.apply_verdict(ctx, row, folder, ticket.verdict, finish)
        else:
            ctx.say(f"It is {name}'s turn to land.")
            try:
                ok = landing_batch.try_batch(ctx, row, folder, ticket, finish)  # None: no batch, the usual turn
                if ok is None:
                    ok = _turn(ctx, row, folder, progress, finish)
                elif ok and not finish:
                    ctx.say(f"When the task is finished: praxis done {name}")
            except BaseException:  # stopped half-way (Ctrl+C, a crash): undo the half-done rebase, keep the work
                _back_on_branch(row, folder)
                _record_if_it_landed(ctx, row, progress)
                raise
    if ok and finish:
        _finish(ctx, row)
    return ok


def _done_note(ctx: Ctx, ticket) -> dict | None:
    note = read_note(ctx.store, ticket.token)
    return note if note and note.get("done") else None


def _finish(ctx: Ctx, row) -> None:
    """`land --done`: finish the agent, unless the command was run from inside its own folder (which cannot be
    removed while a terminal is in it)."""
    if agent_here(ctx) == row["name"]:
        ctx.say("To finish, run this from the main project folder (a folder cannot be removed while you are in it):")
        ctx.say(f'  cd "{ctx.repo.root}"')
        ctx.say(f"  praxis done {row['name']}")
        return
    done(ctx, row["name"])


def _refuse_embedded_projects(row, folder: Path) -> None:
    nested = gitops.embedded_projects(folder)
    if nested:
        raise PraxisError(
            f"These folders in {row['name']}'s folder are separate git projects (each has its own .git): "
            + ", ".join(nested) + ".\ngit cannot save their files as part of this project: it would store only a "
            "pointer to commits that exist nowhere but this folder, and the main version would get an empty "
            "folder. Nothing was landed. Either delete the .git folder inside each one (so its files become part "
            "of this project), or move it out of the agent's folder. To keep it as a separate project on "
            "purpose, the human can add it as a git submodule."
        )


def _refuse_pointer_commits(ctx: Ctx, row, folder: Path) -> None:
    """A pointer to another git project's commit (made by `git add` on a folder that has its own .git) that is
    not a listed submodule: landing it would put an empty, broken folder into the main version."""
    main_ref = f"refs/heads/{ctx.cfg.main_branch}"
    # The pointers in HEAD that main does not also have (a pointer main has already landed is known), less the
    # ones .gitmodules lists. Comparing the two versions finds them without listing every file of both.
    loose = gitops.new_gitlinks(folder, main_ref, "HEAD")
    if loose:
        listed = gitops.listed_submodules(folder, "HEAD")
        loose = [p for p in loose if p not in listed]
    if loose:
        raise PraxisError(
            f"{row['name']}'s saved work stores these folders as bare pointers to other git projects: "
            + ", ".join(loose) + ". In the main version they would be empty, broken folders, so nothing was "
            "landed.\nIn the agent's folder, for each one: `git rm --cached <folder>`, then either delete the "
            ".git folder inside it (so its files become part of this project) or move it out, commit, and land "
            "again. To keep one as a separate project on purpose, the human can add it as a git submodule."
        )


def _record_if_it_landed(ctx: Ctx, row, progress: dict) -> None:
    """Stopped at the very end: if main already holds the checked work, say so and record it, so nobody is told
    a landing was undone when it happened."""
    checked = progress.get("checked")
    if not checked or progress.get("recorded"):
        return
    tip = gitops.rev(ctx.repo.root, f"refs/heads/{ctx.cfg.main_branch}")
    if tip and gitops.is_ancestor(ctx.repo.root, checked, tip):
        _record_landing(ctx, row, progress.get("summary", "stopped right after landing"), progress.get("log"), tip)
        ctx.say(f"Stopped, but {row['name']}'s work had already landed in {ctx.cfg.main_branch}.")


def _record_landing(ctx: Ctx, row, summary: str, log: str | None, tip: str) -> None:
    with ctx.store.write() as db:
        db.execute("INSERT INTO landings (agent, task, at, ok, kind, summary, log, commit_id) "
                   "VALUES (?, ?, ?, 1, 'landed', ?, ?, ?)",
                   (row["name"], row["task"], time.time(), summary, log, tip))


def _turn(ctx: Ctx, row, folder: Path, progress: dict, finish: bool = False) -> bool:
    """One agent's turn at the front of the line. With praxis.toml's `speculate`, the check of the next agent in
    line is started early while this agent's check runs (see early), and stopped again unless this work lands."""
    from agent_praxis import early

    line = early.Line(ctx, row)
    try:
        return _take_turn(ctx, row, folder, progress, finish, line)
    finally:
        line.stop()  # still here: the work it was built on did not land (after a landing it was let go, see keep)


def _take_turn(ctx: Ctx, row, folder: Path, progress: dict, finish: bool, line) -> bool:
    main, root = ctx.cfg.main_branch, ctx.repo.root
    main_ref, branch_ref = f"refs/heads/{main}", f"refs/heads/{row['branch']}"
    for _ in range(RETRIES_WHEN_MAIN_MOVES):
        line.stop()  # a new try builds on a newer main: an early check built on the last try is of no use
        base = gitops.rev(root, main_ref)
        landing_changes: list[str] = []
        # Early, cheap refusal: files the agent changed that now differ from main and that someone else holds.
        early = set(gitops.changed_files(root, base, branch_ref)) & set(gitops.changed_between(root, base, branch_ref))
        clashes = _held_by_others(ctx, row, sorted(early))
        if not clashes:
            problem = _integrate(ctx, row, folder)
            if problem is not None:  # a clash, already recorded and explained
                return problem
            # What landing would really change in main, now that the work sits on top of it. This is the one
            # that counts: it also catches an agent putting a file back to an older version.
            landing_changes = gitops.changed_between(folder, base, "HEAD")
            clashes = _held_by_others(ctx, row, landing_changes, note=True)
            undone = [] if clashes or ctx.cfg.exclusive_code else _undoes_landed_work(ctx, row, folder, base,
                                                                                       landing_changes)
            if undone:
                return _fail(ctx, row, "undo", "it would undo work that landed after it started",
                             "Landing this would put these files back to an older version, undoing newer work "
                             "in main:\n" + "\n".join(undone)
                             + "\nTake the newest main version of each: in your folder run\n"
                             f"  git checkout {main} -- <file>\n"
                             "then make your own change to it again, and land again. If the older version is "
                             "really wanted, ask the human.")
        if clashes:
            return _fail(ctx, row, "claims", "it changed files another agent holds",
                         "These files are held by another agent, so this work cannot land:\n" + "\n".join(clashes)
                         + "\nPut each of them back to the newest main version: in your folder run\n"
                         f"  git checkout {main} -- <file>\n"
                         "then land again (or ask the human to sort it out). Landing never changes files "
                         "another agent holds.")
        checked = gitops.rev(folder, "HEAD")
        log = None
        command, which = ctx.cfg.check_for(landing_changes)
        command = command.strip()
        named = f" ({which})" if ctx.cfg.checks else ""  # without `checks` it is just "the check", as before
        tree = gitops.git(["rev-parse", "HEAD^{tree}"], folder, check=False).stdout.strip()
        cached = checkcache.passed_at(ctx.store, tree, command) if command and tree else None
        if cached is None and command and tree:
            cached = line.wait_for(tree, command)  # its check may have been started early, and still be running
        if cached is not None:
            ctx.say(f"Skipping the check{named}: same tree already passed at "
                    f"{time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(cached))} with the same command: {command}")
        elif command:
            line.start(checked, [row["name"]])  # with `speculate`: the next agent's check starts beside this one
            ctx.say(f"Running the check{named}: {command}")
            code, output, log = _run_check(ctx, row, folder, command)
            _put_back_check_rewrites(ctx, row, folder)  # pass or fail, so they are never saved as the agent's work
            if code == NEEDS_THE_HUMAN:
                return _needs_the_human(ctx, row, output, log)
            if code != 0:
                count = enough.problem_count(output)
                tail = "\n".join(output.rstrip().splitlines()[-TAIL_LINES:])
                return _fail(ctx, row, "check", "the check failed" + (f" ({count} problem(s))" if count else ""),
                             f"The check failed, so nothing landed. The end of its output:\n{tail}\n"
                             f"Full output: {log}\nFix the problem in your folder, then land again.",
                             problems=count, log=log, sig_text=output)
            moved = _changed_during_check(ctx, row, folder, checked)
            if moved:
                return _fail(ctx, row, "changed-during-check", "files changed while the check ran", moved)
            if tree:
                checkcache.record(ctx.store, tree, command)  # only a pass is remembered, never a failure
        if gitops.rev(root, main_ref) != base:
            ctx.say("The main version changed while the check ran. Doing it again on top of the newest version.")
            continue
        summary = "no check is set, so nothing was tested"
        if command:
            summary = ("check passed" + (", same tree already passed" if cached is not None else "")
                       + (f" ({which})" if named else ""))
        progress.update(checked=checked, summary=summary, log=log)
        advanced = _advance(ctx, row, base, checked, bool(command))
        if advanced == MAIN_MOVED:
            progress.clear()
            ctx.say("The main version changed just as Praxis was moving it forward. Doing it again on top of the "
                    "newest version.")
            continue
        if advanced is not True:
            progress.clear()
            return _fail(ctx, row, "main-folder", "the main version could not be moved forward safely", advanced)
        tip = gitops.rev(root, main_ref)
        _record_landing(ctx, row, summary, log, tip)
        progress["recorded"] = True
        ctx.say(f"Landed: {row['name']}'s work on \"{row['task']}\" is now in {main} ({summary}).")
        generated.rebuild_after_landing(ctx, row, base, tip)  # still this agent's turn: nobody else moves main
        if gitops.rev(root, main_ref) == tip:
            line.keep()  # main is exactly what the early check was built on: let it finish
        hooks.run_after_land(ctx, row, gitops.rev(root, main_ref) or tip)
        _full_check_after(ctx, command, tip)
        if not finish:
            ctx.say(f"When the task is finished: praxis done {row['name']}")
        return True
    return _fail(ctx, row, "busy-main", "the main version kept changing during the check",
                 f"The main version changed {RETRIES_WHEN_MAIN_MOVES} times while Praxis was checking. "
                 "Someone is committing to it directly, outside Praxis. Land again when that stops.")


def _full_check_after(ctx: Ctx, used: str, tip: str) -> None:
    """The safety net behind quick checks: after a landing, start praxis.toml's full_check in the background when
    it is due (see fullcheck). *used* is the check command this landing ran, *tip* the commit it landed."""
    if ctx.cfg.full_check.strip():
        from agent_praxis import fullcheck

        fullcheck.after_landing(ctx, used, tip)


def _needs_the_human(ctx: Ctx, row, output: str, log: str) -> bool:
    tail = "\n".join(output.rstrip().splitlines()[-TAIL_LINES:])
    return _fail(ctx, row, "needs-human", "the check reported a problem outside this agent's work",
                 f"The check ended with exit code {NEEDS_THE_HUMAN}, which means it found a problem that is not "
                 "this agent's doing (for example a tool or service it needs is broken), so nothing landed. This "
                 "does not count against the agent's tries.\nThis needs the human: fix what the check reports, then "
                 f"land again. The end of its output:\n{tail}\nFull output: {log}",
                 log=log, sig_text=output)


UNDO_LOOKBACK = 30  # how many of a file's older versions in main are compared with what would land


def _undoes_landed_work(ctx: Ctx, row, folder: Path, base: str, changed: list[str], ref: str = "HEAD") -> list[str]:
    """Shared code files this landing would put back exactly to an older main version, throwing away what other
    landings changed in them since (an agent that rewrote a file from an old copy). One line per file."""
    landed = ctx.store.rows("SELECT agent, commit_id FROM landings WHERE ok = 1 AND commit_id IS NOT NULL "
                            "ORDER BY id DESC LIMIT 50")
    lines = []
    for path in changed[:200]:
        if paths.is_asset(path, ctx.cfg.assets):
            continue  # asset files are one agent at a time: the claims check covers them
        new = gitops.git(["rev-parse", "--verify", "--quiet", f"{ref}:{path}"], folder, check=False).stdout.strip()
        if not new:
            continue  # deleted: not a step back to an older version
        versions = _versions_in_main(folder, base, path)  # [(commit, blob)], newest first
        older = [i for i, (_c, blob) in enumerate(versions) if blob == new]
        if not older or older[0] == 0:
            continue
        newer_commit = versions[0][0]  # the newest change to it in main, which landing would throw away
        agent = _landing_holding(folder, newer_commit, landed)
        if agent == row["name"]:
            continue  # its own earlier landing: taking that back is its own business
        whose = f"{agent}'s landing" if agent else "newer work in main"
        lines.append(f"  - {path} (undoing {whose})")
    return lines


def _versions_in_main(folder: Path, base: str, path: str) -> list[tuple[str, str]]:
    """(commit, the file's content id after it) for the newest UNDO_LOOKBACK commits in main that changed *path*."""
    raw = gitops.git(["log", f"-n{UNDO_LOOKBACK}", "--no-renames", "--no-abbrev", "--raw", "--format=%x01%H", base,
                      "--", path], folder, check=False).stdout
    found = []
    for chunk in raw.split("\x01")[1:]:
        lines = chunk.strip().splitlines()
        if not lines:
            continue
        for line in lines[1:]:
            fields = line.split("\t", 1)[0].split()
            if line.startswith(":") and len(fields) >= 4:
                found.append((lines[0].strip(), fields[3]))
                break
    return found


def _held_by_others(ctx: Ctx, row, changed: list[str], note: bool = False) -> list[str]:
    """Changed files another agent holds one at a time: asset files, or every file with exclusive_code on. Code
    files are shared, so another agent working on one never stops a landing; only a real clash does."""
    others = [c for c in ctx.store.claims() if c["agent"] != row["name"]]
    mine = ctx.store.claims(row["name"])
    lines, loose_assets = [], []
    for path in changed:
        exclusive = ctx.cfg.exclusive_code or paths.is_asset(path, ctx.cfg.assets)
        holders = [c for c in others if paths.covers(c["path"], path) and (exclusive or c["is_asset"])]
        if holders:
            lines.append(f"  - {path} (held by {holders[0]['agent']})")
        elif paths.is_asset(path, ctx.cfg.assets) and not any(paths.covers(c["path"], path) for c in mine):
            loose_assets.append(path)
    if note and loose_assets and not lines:
        ctx.say("Note: these asset files were changed without being claimed first: " + ", ".join(loose_assets))
        ctx.say("Nobody else holds them, so landing goes ahead. Next time claim asset files before editing them.")
    return lines


def _integrate(ctx: Ctx, row, folder: Path):
    """Put the agent's work on top of main. Returns None when that worked, else the result of a recorded failure."""
    main = ctx.cfg.main_branch
    if ctx.cfg.land_method == "merge":
        args = [*gitops.identity_args(folder), "merge", "--no-edit", "-q", f"refs/heads/{main}"]
    else:
        args = [*gitops.identity_args(folder), "rebase", "-q", f"refs/heads/{main}"]
    before = gitops.rev(folder, "HEAD")
    fork = gitops.git(["merge-base", "HEAD", f"refs/heads/{main}"], folder, check=False).stdout.strip()
    proc = _past_generated_clashes(ctx, folder, gitops.git(args, folder, check=False))
    if proc.returncode == 0:
        return None
    clashing = gitops.conflicted_files(folder)
    gitops.abort_operation(folder)
    if clashing and ctx.cfg.land_method == "rebase":
        retried = _rebase_as_one(ctx, row, folder)
        if retried is None:
            return None
        clashing = retried or clashing  # what still clashes once the commits are joined, when that was tried
    lost = _put_folder_back(row, folder, before)
    if lost:
        return _fail(ctx, row, "git", "the folder could not be put back as it was", lost)
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
    whose = _who_landed(ctx, folder, fork, clashing)
    return _fail(ctx, row, "conflict", f"its changes clash with newer work in {len(clashing)} file(s)",
                 "Your changes clash with work that landed while you were working, in:\n"
                 + "\n".join(f"  - {p}" + (f" (clashes with {whose[p]})" if p in whose else "") for p in clashing)
                 + "\nNothing landed. Update your folder (rebase it on the newest main version), fix the clashes, "
                 "then land again.\n" + advice,
                 problems=len(clashing), sig_text="\n".join(sorted(clashing)))


def _past_generated_clashes(ctx: Ctx, folder: Path, proc):
    """A rebase that stopped only on generated files: take main's version of each (Praxis rebuilds them after the
    landing anyway, from the sources) and carry on. Returns the last git step's result."""
    taken: list[str] = []
    while proc.returncode != 0 and ctx.cfg.generated and gitops.operation_in_progress(folder) == "rebase":
        clashing = gitops.conflicted_files(folder)
        if not clashing or not all(generated.entry_for(ctx.cfg, p) for p in clashing):
            break
        # In a rebase, "ours" is the newest main version the agent's commits are being put on.
        if gitops.git(["checkout", "--ours", "--", *clashing], folder, check=False).returncode != 0:
            break
        gitops.git(["add", "--", *clashing], folder)
        taken += [p for p in clashing if p not in taken]
        empty = gitops.git(["diff", "--cached", "--quiet"], folder, check=False).returncode == 0
        proc = gitops.git([*gitops.identity_args(folder), "rebase", "--skip" if empty else "--continue"], folder,
                          check=False)
    if taken and proc.returncode == 0:
        ctx.say("Took the newest main version of the generated file(s) " + ", ".join(taken) + ": Praxis rebuilds "
                "them from their sources after this landing.")
    return proc


def _put_folder_back(row, folder: Path, before: str | None) -> str | None:
    """After a refused rebase or merge: the folder must be on the agent's own branch, with no half-done git step,
    holding the same files it held before (the same commit, or the original commits an earlier joined try that
    was cut short had put aside). None when it is; else what is wrong, in plain words."""
    _back_on_branch(row, folder)
    head = gitops.rev(folder, "HEAD")
    same = head == before or (head is not None and before is not None and _tree(folder, head) == _tree(folder, before))
    if gitops.operation_in_progress(folder) is None and gitops.current_branch(folder) == row["branch"] and same:
        return None
    return (f"git could not combine your work with the newest main version, and then Praxis could not put "
            f"{row['name']}'s folder back exactly as it was. Your saved work is still in git (commit "
            f"{before or 'unknown'}); nothing was deleted.\nThis needs the human: in {folder} run `git status`, "
            f"then `git rebase --abort` if a rebase is shown, and `git switch {row['branch']}`.")


def _who_landed(ctx: Ctx, folder: Path, fork: str, clashing: list[str]) -> dict[str, str]:
    """{file: whose landing it clashes with} for each clashing file Praxis can tell: the agent of the last
    landing (since this agent's work forked from main) that changed the file."""
    main_ref = f"refs/heads/{ctx.cfg.main_branch}"
    if not fork:
        return {}
    landed = ctx.store.rows("SELECT agent, commit_id FROM landings WHERE ok = 1 AND commit_id IS NOT NULL "
                            "ORDER BY id DESC LIMIT 50")
    whose = {}
    for path in clashing[:20]:
        last = gitops.git(["log", "-1", "--format=%H %s", f"{fork}..{main_ref}", "--", path], folder,
                          check=False).stdout.strip()
        if not last:
            continue
        commit, _, subject = last.partition(" ")
        if subject.startswith("Rebuild ") and generated.entry_for(ctx.cfg, path):
            whose[path] = "Praxis's own rebuild of this generated file"
            continue
        agent = _landing_holding(folder, commit, landed)
        if agent:
            whose[path] = f"{agent}'s landing"
    return whose


def _landing_holding(folder: Path, commit: str, landed: list) -> str | None:
    """The agent whose landing brought *commit* into main: the oldest landing whose tip holds it. *landed* is
    newest first."""
    found = None
    for item in landed:
        if gitops.is_ancestor(folder, commit, item["commit_id"]):
            found = item["agent"]
        else:
            break  # main only moves forward: older landings cannot hold it either
    return found


def _rebase_as_one(ctx: Ctx, row, folder: Path) -> list[str] | None:
    """A rebase replays an agent's commits one at a time, and a step in the middle can clash even when the end
    result does not (an agent that took the newest version of a file after an earlier edit to it). Try once more
    with the agent's unlanded commits joined into one. None when that worked. Otherwise the original commits are
    put back exactly, and the files that still clash are returned ([] when it was not tried). Nothing is lost
    either way: the joined commit has exactly the same files."""
    main_ref = f"refs/heads/{ctx.cfg.main_branch}"
    original = gitops.rev(folder, "HEAD")
    fork = gitops.out(["merge-base", "HEAD", main_ref], folder)
    if gitops.commits_not_in(folder, [original], [fork]) < 2:
        return []  # one commit: joining changes nothing
    # The joined commit is made beside the branch (commit-tree moves nothing), keeping every commit's message. The
    # folder never leaves the agent's branch: the original commits are kept under a name of their own first, then
    # the branch takes the joined commit (the same files, so nothing in the folder changes) and is rebased. If that
    # clashes, the branch goes back to the original commits. A landing killed at any point leaves the folder on
    # its own branch, holding either the original commits or the joined one, and the originals stay kept.
    messages = gitops.out(["log", "--reverse", "--format=%B", f"{fork}..{original}"], folder)
    message = ctx.cfg.with_trailers(f"{row['task']} (by {row['name']})\n\n{messages}", row["name"], row["task"])
    joined = gitops.git([*gitops.identity_args(folder), "commit-tree", *gitops.sign_args(folder),
                         f"{original}^{{tree}}", "-p", fork], folder, check=False, stdin=message)
    if joined.returncode != 0:
        return []
    keep = f"{JOINING_PREFIX}{row['name']}"
    if gitops.git(["update-ref", keep, original], folder, check=False).returncode != 0:
        return []
    gitops.git(["reset", "-q", "--soft", joined.stdout.strip()], folder)  # the same files as now
    proc = _past_generated_clashes(
        ctx, folder, gitops.git([*gitops.identity_args(folder), "rebase", "-q", main_ref], folder, check=False))
    if proc.returncode == 0:
        gitops.git(["update-ref", "-d", keep], folder, check=False)  # the joined work now holds them
        ctx.say("Put your saved changes together into one commit, so they fit on top of the newest version.")
        return None
    still = gitops.conflicted_files(folder)
    _back_on_branch(row, folder)
    return still


def _back_on_branch(row, folder: Path) -> None:
    """Undo a half-done rebase, make sure the folder is on the agent's own branch, and if a joining try was cut
    short, give the branch its original commits back. Each step leaves the same files in the folder."""
    gitops.abort_operation(folder)
    if gitops.current_branch(folder) != row["branch"]:
        gitops.git(["checkout", "-q", row["branch"]], folder, check=False)
    keep = f"{JOINING_PREFIX}{row['name']}"
    original = gitops.rev(folder, keep)
    if original is None:
        return
    tip = gitops.rev(folder, "HEAD")
    same_files = tip is not None and _tree(folder, tip) == _tree(folder, original)
    if same_files and tip != original and gitops.current_branch(folder) == row["branch"]:
        gitops.git(["reset", "-q", "--soft", original], folder)  # the joined commit back to the originals
        tip = gitops.rev(folder, "HEAD")
    if tip == original or not same_files:  # put back, or the joined work already sits on top of main
        gitops.git(["update-ref", "-d", keep], folder, check=False)


_KILL_ON_CLOSE, _BREAKAWAY_OK = 0x2000, 0x800


class _CheckEndsWithMe:
    """On Windows, put the check (and what it starts) in a job of its own that Windows ends if this process is
    killed (an AI tool's time limit), so an orphaned check never keeps changing the agent's folder while the next
    landing runs. When the check finishes normally, the job lets go: helpers it leaves running on purpose (a
    build daemon, an adb server) carry on. Only the check is in the job, never Praxis or its git steps, and a
    program the check starts may still leave the job (breakaway is allowed). Elsewhere a killed landing's check
    can outlive it; the next landing still refuses to land files that change while its own check runs."""

    def __init__(self):
        self.job = None
        self._limit = None

    def watch(self, proc: subprocess.Popen) -> None:
        if os.name != "nt":
            return
        try:
            import ctypes
            from ctypes import wintypes

            class Basic(ctypes.Structure):
                _fields_ = [("PerProcessUserTimeLimit", ctypes.c_int64), ("PerJobUserTimeLimit", ctypes.c_int64),
                            ("LimitFlags", wintypes.DWORD), ("MinimumWorkingSetSize", ctypes.c_size_t),
                            ("MaximumWorkingSetSize", ctypes.c_size_t), ("ActiveProcessLimit", wintypes.DWORD),
                            ("Affinity", ctypes.c_size_t), ("PriorityClass", wintypes.DWORD),
                            ("SchedulingClass", wintypes.DWORD)]

            class Extended(ctypes.Structure):
                _fields_ = [("BasicLimitInformation", Basic), ("IoInfo", ctypes.c_uint64 * 6),
                            ("ProcessMemoryLimit", ctypes.c_size_t), ("JobMemoryLimit", ctypes.c_size_t),
                            ("PeakProcessMemoryUsed", ctypes.c_size_t), ("PeakJobMemoryUsed", ctypes.c_size_t)]

            k32 = ctypes.WinDLL("kernel32", use_last_error=True)
            k32.CreateJobObjectW.restype = wintypes.HANDLE
            k32.CreateJobObjectW.argtypes = [wintypes.LPVOID, wintypes.LPCWSTR]
            k32.SetInformationJobObject.argtypes = [wintypes.HANDLE, ctypes.c_int, wintypes.LPVOID, wintypes.DWORD]
            k32.AssignProcessToJobObject.argtypes = [wintypes.HANDLE, wintypes.HANDLE]
            k32.CloseHandle.argtypes = [wintypes.HANDLE]
            job = k32.CreateJobObjectW(None, None)
            if not job:
                return
            info = Extended()

            def limit(flags: int) -> bool:
                info.BasicLimitInformation.LimitFlags = flags
                return bool(k32.SetInformationJobObject(job, 9, ctypes.byref(info), ctypes.sizeof(info)))

            if limit(_KILL_ON_CLOSE | _BREAKAWAY_OK) and k32.AssignProcessToJobObject(job, int(proc._handle)):
                self.job, self._limit, self._close = job, limit, k32.CloseHandle
            else:
                k32.CloseHandle(job)
        except (OSError, AttributeError, ValueError):
            return  # an older Windows: carry on without it

    def stop(self) -> None:
        """Stop the check and everything it started (closing the job while its limit is on ends them all)."""
        if self.job is not None:
            self._close(self.job)
            self.job = None

    def release(self) -> None:
        """The check is over: lift the limit and let go, so what it left running keeps running."""
        if self.job is not None:
            self._limit(_BREAKAWAY_OK)
            self._close(self.job)
            self.job = None


def _decode(data: bytes) -> str:
    text = data.decode("utf-8", errors="replace")
    bad = text.count("�")
    good = sum(1 for ch in text if ord(ch) > 127) - bad  # accented letters that did read as UTF-8
    if bad <= good:  # UTF-8 apart from a stray bad byte or two (or a line cut in half)
        return text
    # Mostly not UTF-8: Windows tools often print in the system's own code page.
    return data.decode(locale.getpreferredencoding(False) or "utf-8", errors="replace")


def _new_log(logs: Path, agent: str) -> Path:
    stamp = time.strftime("%Y%m%d-%H%M%S")
    log, n = logs / f"{agent}-{stamp}.log", 2
    while log.exists():
        log, n = logs / f"{agent}-{stamp}-{n}.log", n + 1
    return log


def _run_check(ctx: Ctx, row, folder: Path, command: str) -> tuple[int, str, str]:
    """(the check's exit code, the end of its output, its log)."""
    # PRAXIS_CHECK: a `praxis run --heavy` inside the check knows a landing waits on it, and goes first (heavy.py).
    env = hooks.command_env(ctx.cfg, PRAXIS_AGENT=row["name"], PRAXIS_TASK=row["task"], PRAXIS_CHECK="1")
    logs = ctx.store.dir / "logs"
    logs.mkdir(parents=True, exist_ok=True)
    log = _new_log(logs, row["name"])
    plan = shells.prepare(ctx, command, env, folder)  # on Windows a bare `bash` means Git Bash, not WSL
    with open(log, "wb") as out:
        out.write(plan.header(command))
        out.flush()
        # No time limit, on purpose. No keyboard either: a check that waits for a key (`pause`) would hold the
        # landing line forever. The output goes straight to the log, so a huge output never fills memory.
        guard = _CheckEndsWithMe()
        proc = subprocess.Popen(plan.command, shell=True, cwd=str(folder), env=env,
                                stdin=subprocess.DEVNULL, stdout=out, stderr=subprocess.STDOUT)
        try:
            guard.watch(proc)
            _wait_saying_so(ctx, proc, log)
        except BaseException:  # stopped (Ctrl+C): stop the check and what it started, then report it
            guard.stop()
            proc.kill()
            proc.wait()
            raise
        finally:
            guard.release()
        out.write(f"\n(exit code {proc.returncode})\n".encode())
    with open(log, "rb") as handle:
        size = log.stat().st_size
        handle.seek(max(0, size - TAIL_BYTES))
        data = handle.read()
        if size > TAIL_BYTES:  # started part-way through: drop the cut-off first line
            data = data[data.find(b"\n") + 1:]
        output = _decode(data)
    _prune_logs(logs, keep=log)
    return proc.returncode, output, str(log)


def _wait_saying_so(ctx: Ctx, proc: subprocess.Popen, log: Path) -> None:
    """Wait for the check (as long as it takes), saying about once a minute that it is still running, with the
    last line it printed: a long, silent check otherwise looks hung to a person or an AI tool."""
    began = time.monotonic()
    while True:
        try:
            proc.wait(timeout=HEARTBEAT_SECONDS)
            return
        except subprocess.TimeoutExpired:
            pass
        minutes = int((time.monotonic() - began) // 60)
        ctx.say(f"Check still running ({minutes} min). Output so far: {log}")
        last = _last_line(log)
        if last:
            ctx.say(f"  Its last line: {last}")


def _last_line(log: Path, limit: int = 200) -> str:
    """The last non-empty line in the log so far (cut to *limit* characters), or ""."""
    try:
        with open(log, "rb") as handle:
            size = handle.seek(0, os.SEEK_END)
            handle.seek(max(0, size - 8192))
            data = handle.read()
    except OSError:
        return ""
    for line in reversed(data.replace(b"\r", b"\n").split(b"\n")):  # a progress bar redraws its line with \r
        text = _decode(line).strip()
        if text:
            return text if len(text) <= limit else text[:limit] + "..."
    return ""


def _prune_logs(logs: Path, keep: Path | None = None) -> None:
    """Keep the newest KEEP_LOGS check logs, and keep them all together under KEEP_LOG_BYTES, oldest first to go.
    *keep* (the log of the landing just run) is never deleted, even when it alone is bigger."""
    found = []
    for path in logs.glob("*.log"):
        try:
            info = path.stat()
        except OSError:
            continue
        if keep is None or path.name != keep.name:  # the same folder: the name says which one it is
            found.append((info.st_mtime, info.st_size, path))
    found.sort(key=lambda item: item[0])
    total = sum(size for _m, size, _p in found)
    try:
        total += keep.stat().st_size if keep is not None else 0
    except OSError:
        pass
    extra = len(found) + (1 if keep is not None else 0) - KEEP_LOGS
    for _mtime, size, old in found:
        if extra <= 0 and total <= KEEP_LOG_BYTES:
            break
        try:
            old.unlink()
        except OSError:
            continue
        extra -= 1
        total -= size


def _changed_during_check(ctx: Ctx, row, folder: Path, checked: str) -> str | None:
    """None when the folder still holds exactly the commit that was checked; else what changed, in plain words.
    Landing a commit the check did not see would make "the check passed" untrue."""
    if gitops.rev(folder, "HEAD") != checked:
        return ("A new commit was made in the folder while the check ran, so the check did not test what would "
                "land. Nothing landed. Land again (and do not change the folder while a landing runs).")
    entries = gitops.status_entries(folder)  # one look: tracked changes, and new files the check made
    tracked = [path for state, path in entries if state != "??"]
    if tracked:
        return ("These files changed while the check ran, so the check may not have tested what would land:\n"
                + "\n".join(f"  - {p}" for p in tracked[:20])
                + "\nNothing landed. If an agent edited them, land again once it stops (and do not change the folder "
                "while a landing runs).\nIf the check itself rewrites them every time it runs, add them to "
                'check_may_change in praxis.toml (for example check_may_change = ["' + tracked[0] + '"]), then land '
                "again.")
    new = [path for _state, path in entries]
    if new:
        ctx.say("Note: the check created new files that git does not ignore. They were not landed: "
                + ", ".join(new[:8]) + (f" and {len(new) - 8} more" if len(new) > 8 else ""))
        ctx.say("If they are build output, add them to .gitignore, or the next `praxis land` will save them.")
    return None


def _put_back_check_rewrites(ctx: Ctx, row, folder: Path) -> None:
    """Put the files the check is allowed to rewrite (check_may_change) back to their committed version. A copy
    of each changed one is kept first, in case an agent had edited it during the check."""
    allowed = ctx.cfg.check_may_change + [g["path"] for g in ctx.cfg.generated]  # Praxis rebuilds those itself
    if not allowed:
        return
    changed = [p for p in gitops.dirty_files(folder, untracked=False) if paths.matches_from_top(p, allowed)]
    if not changed:
        return
    dest = paths.fresh_folder(ctx.store.dir / "kept" / f"{row['name']}-{time.strftime('%Y%m%d-%H%M%S')}-check")
    for rel in changed:
        src = folder / rel
        if src.is_file():
            (dest / rel).parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src, dest / rel)
    gitops.git(["restore", "--worktree", "--source=HEAD", "--", *changed], folder)
    ctx.say("Put back files the check rewrote (check_may_change in praxis.toml): " + ", ".join(changed[:8])
            + (f" and {len(changed) - 8} more" if len(changed) > 8 else "") + f". Copies as the check left them: {dest}")


def _advance(ctx: Ctx, row, base: str, target: str, checked: bool = True):
    """Move main forward to the checked commit. True when done, else the plain reason it could not."""
    main, root = ctx.cfg.main_branch, ctx.repo.root
    ready = "Your work passed the check" if checked else "Your work is ready (no check is set)"
    if not gitops.is_ancestor(root, base, target):
        return (f"The work is not built on top of the newest {main}, so moving {main} to it would drop commits "
                f"that already landed. Praxis did not move {main}. Land again.")
    entries = gitops.worktrees_at(root)  # one list of the folders for both questions below
    rebasing = gitops.branch_being_rebased(root, main, entries)
    if rebasing is not None:
        return (f"{main} is in the middle of a git rebase in {rebasing}. Moving it now would break that rebase. "
                "This needs the human: finish it (`git rebase --continue`) or cancel it (`git rebase --abort`), "
                "then land again.")
    where = gitops.branch_checked_out_at(root, main, entries)
    if where is None:
        moved = gitops.git(["update-ref", f"refs/heads/{main}", target, base], root, check=False)
        if moved.returncode == 0:
            return True
        if gitops.rev(root, f"refs/heads/{main}") != base:
            return MAIN_MOVED
        raise gitops.GitError(["update-ref", f"refs/heads/{main}", target, base], moved)
    proc = gitops.git([*PARALLEL_CHECKOUT, "merge", "--ff-only", "-q", target], where, check=False)
    if proc.returncode == 0:
        return True
    if gitops.rev(root, f"refs/heads/{main}") != base:  # another writer moved main just now: not a real problem
        return MAIN_MOVED
    said = (proc.stderr or proc.stdout).strip()
    clashing = set(gitops.dirty_files(where)) & set(gitops.changed_files(root, base, target))
    if clashing:
        return (f"{ready}, but it could not be put into the main project folder, because that folder has "
                f"unsaved changes to the same files ({where}): {', '.join(sorted(clashing)[:8])}. "
                "Praxis did not overwrite them.\n"
                "This needs the human: save (commit) or undo the changes made directly in the main folder, "
                "then land again.")
    return (f"{ready}, but git could not move the main project folder ({where}) forward. "
            f"Praxis changed nothing there.\ngit said: {said}\nThis needs the human.")


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
    if kind in enough.NOT_THE_AGENTS:  # the main folder or someone outside Praxis: never the agent's fault
        return False
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
