"""`praxis check <agent>`: earn the checks' passes before the landing line.

It builds what a landing of the agent's saved work would build right now, "the newest main version + the agent's saved
commits", in a scratch folder of Praxis's own (`<agent folders>/_check-1`, `_check-2`...: git worktrees on no branch,
made on first use and kept, with the `share` folders linked and `setup` run once, as for the full check). It never
moves main, never changes the agent's branch and never touches the agent's folder, so the agent can keep working
while it runs. It then runs the checks that landing would run (checks.py) and remembers the ones that pass.

A later `praxis land` finds those passes: a named check with `inputs` is skipped while the files it reads are the
same, whatever else landed in between; a check that reads the whole project is skipped only when nothing else
landed. Nothing here is a landing try: a failure is not recorded against the agent.

It holds one heavy job place from start to end and waits in that line like any heavy job, so at most `heavy_jobs`
of these run at once, and no more scratch folders than that are ever made.
"""

from __future__ import annotations

import contextlib
import dataclasses
from pathlib import Path

from agent_praxis import checks, gitops, heavy
from agent_praxis.agents import Ctx, folder_state, need_agent, refuse_other_agents_folder
from agent_praxis.errors import PraxisError
from agent_praxis.landing_queue import _unlock

SCRATCH = "_check"  # an agent's name cannot start with "_", so no agent's folder ever has this name
NEEDS_THE_HUMAN = 3
TAIL_LINES = 40


@contextlib.contextmanager
def _slot(ctx: Ctx):
    """A scratch folder nobody else is using right now: gives its name, and keeps it locked meanwhile."""
    from agent_praxis import fullcheck

    number = 1
    while True:
        handle = fullcheck.take_lock(ctx.store.dir / "check" / f"slot-{number}.lock", tries=1)
        if handle is not None:
            break
        number += 1
    try:
        yield f"{SCRATCH}-{number}"
    finally:
        _unlock(handle)
        handle.close()


def run(ctx: Ctx, name: str) -> int:
    """0 when every check of the agent's saved work passed (now, or from memory), 2 when not."""
    row = need_agent(ctx, name)
    name = row["name"]
    refuse_other_agents_folder(ctx, row, "check")
    root, main = ctx.repo.root, ctx.cfg.main_branch
    main_ref, branch_ref = f"refs/heads/{main}", f"refs/heads/{row['branch']}"
    if gitops.rev(root, branch_ref) is None:
        raise PraxisError(f"{name}'s branch {row['branch']} does not exist, so there is no saved work to check.")
    unsaved = _unsaved(ctx, row)
    if gitops.commits_ahead(root, main_ref, branch_ref) == 0:
        ctx.say(f"{name} has no saved work that is not in {main} already, so there is nothing to check.{unsaved}")
        return 0
    ctx.store.touch(name)
    with heavy.place(ctx, f"{name} (praxis check)", f"praxis check {name}") as (_mine, token), _slot(ctx) as scratch:
        return _check(ctx, row, token, scratch, unsaved)


def _unsaved(ctx: Ctx, row) -> str:
    """A sentence about files in the agent's folder that are not committed yet ("" when there are none)."""
    if folder_state(ctx, row) != "ok":
        return ""
    count = len(gitops.dirty_files(Path(row["worktree"])))
    if not count:
        return ""
    return (
        f"\nNote: {count} file(s) in {row['name']}'s folder are not saved (committed) yet, so they are not part "
        f"of this check. `praxis land {row['name']}` saves them and checks them."
    )


def _check(ctx: Ctx, row, token: str | None, scratch: str, unsaved: str) -> int:
    from agent_praxis import fullcheck

    name, main = row["name"], ctx.cfg.main_branch
    # Read now that the place is ours: the newest main, and the work the agent has saved by this moment.
    base = gitops.rev(ctx.repo.root, f"refs/heads/{main}")
    tip = gitops.rev(ctx.repo.root, f"refs/heads/{row['branch']}")
    folder = fullcheck._scratch(ctx, base, scratch)
    fullcheck._put_files(folder, base)
    ctx.say(
        f"Checking {name}'s saved work on top of the newest {main} (commit {base[:10]}), in Praxis's scratch "
        f"folder {folder}. Nothing lands, and {name}'s folder and branch are not touched.{unsaved}"
    )
    clash = _build(ctx, folder, base, tip)
    if clash is not None:
        ctx.say(
            f"NOT checked: {name}'s saved work does not fit on top of the newest {main}"
            + (": it clashes in\n" + "\n".join(f"  - {p}" for p in clash) if clash else ".")
            + f"\nUpdate your folder first (in it, run `git {ctx.cfg.land_method} {main}` and fix the files "
            "listed), then check again. `praxis land` would be refused the same way."
        )
        return 2
    checked = gitops.rev(folder, "HEAD")
    changed = gitops.changed_between(folder, base, "HEAD")
    plan = checks.plan(ctx.cfg, changed) if checked != base else []
    if not plan:
        ctx.say(
            f"Nothing to run: {'no check is set for these files' if changed else f'{main} already holds this work'}."
        )
        return 0
    if token:  # a `praxis run --heavy` inside a check runs in this command's place
        ctx = dataclasses.replace(ctx, cfg=dataclasses.replace(ctx.cfg, env={**ctx.cfg.env, heavy.TOKEN_ENV: token}))
    outcome = checks.run(ctx, row, folder, checked, plan, base=base, changed=changed)
    return _report(ctx, row, outcome)


def _build(ctx: Ctx, folder: Path, base: str, tip: str) -> list[str] | None:
    """Make *folder* hold what a landing would check: the agent's saved commits on top of *base* (or, with
    land_method = "merge", main merged into them). None when that worked, else the files that clash."""
    from agent_praxis import fullcheck
    from agent_praxis import landing as L

    who = gitops.identity_args(folder)
    if ctx.cfg.land_method == "merge":
        fullcheck._put_files(folder, tip)
        proc = gitops.git([*who, "merge", "--no-edit", "-q", base], folder, check=False)
    else:
        fork = gitops.git(["merge-base", tip, base], folder, check=False).stdout.strip()
        proc = L._past_generated_clashes(
            ctx, folder, gitops.git([*who, "rebase", "-q", "--onto", base, fork, tip], folder, check=False)
        )
        if proc.returncode != 0 and fork:  # as a landing does: once more with the commits joined into one
            gitops.abort_operation(folder)
            joined = gitops.git(
                [*who, "commit-tree", f"{tip}^{{tree}}", "-p", fork],
                folder,
                check=False,
                stdin="the agent's saved commits, joined for a check\n",
            )
            if joined.returncode == 0:
                proc = L._past_generated_clashes(
                    ctx,
                    folder,
                    gitops.git(
                        [*who, "rebase", "-q", "--onto", base, fork, joined.stdout.strip()], folder, check=False
                    ),
                )
    if proc.returncode == 0:
        return None
    clashing = gitops.conflicted_files(folder)
    gitops.abort_operation(folder)
    return clashing


def _report(ctx: Ctx, row, outcome) -> int:
    name, main = row["name"], ctx.cfg.main_branch
    ctx.say("")
    ctx.say(f"The checks of {name}'s saved work on top of the newest {main}:")
    for step in outcome.steps:
        label = step.check.label
        if step.how == "reused":
            ctx.say(f"  - {label}: reused, it had already passed at {checks._stamp(step.at)}")
        elif step.how == "passed":
            ctx.say(f"  - {label}: passed. Its output: {step.log}")
        elif step.how == "failed":
            ctx.say(f"  - {label}: FAILED (exit code {step.code}). Its output: {step.log}")
        else:
            ctx.say(f"  - {label}: not run, because another check failed")
    bad = outcome.failed
    if bad is not None:
        tail = "\n".join(bad.output.rstrip().splitlines()[-TAIL_LINES:])
        ctx.say(f"The end of the output of {bad.check.label}:\n{tail}")
        if bad.code == NEEDS_THE_HUMAN:
            ctx.say(
                f"Exit code {NEEDS_THE_HUMAN} means a problem that is not this agent's doing (a tool or service "
                "the check needs is down). This needs the human."
            )
        else:
            ctx.say(
                "Fix the problem in your folder, save (commit) it, then check again. Nothing was landed, and "
                "this does not count as a landing try."
            )
        return 2
    if outcome.moved:
        ctx.say(
            "Files that git tracks changed in the scratch folder while the checks ran, so no pass was "
            "remembered: `praxis land` runs them again. If a check rewrites files every time it runs, list them "
            "in check_may_change in praxis.toml."
        )
        return 2
    whole = [s.check.label for s in outcome.steps if s.check.inputs is None]
    ctx.say(
        f"All passed, and Praxis remembers it. `praxis land {name}` does not run a check again while the files "
        "that check reads are the same."
    )
    if whole:
        ctx.say(
            f"Note: {', '.join(whole)} reads the whole project (it has no `inputs` in praxis.toml), so its pass "
            f"counts only until something else lands in {main}."
        )
    return 0
