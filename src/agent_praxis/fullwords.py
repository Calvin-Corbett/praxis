"""What the runs of a full check showed, in plain words: for `praxis fullcheck`'s own output and `praxis status`.

Praxis knows two things about a run of the full check: its exit code and where its output is. It never reads test
output. So everything said here is built from the runs recorded in the full check's row (`steps`: one entry for
each commit it looked at, written by fullsearch.py) and says no more than those runs showed: how many times a
version failed, where the pass before it came from, and whether the same files ever gave two answers.
"""

from __future__ import annotations

import json

OWN_CHECK = "a landing's own check"  # the `why` of a pass recorded because a landing's own check was this command
# "It keeps happening": the same files gave two answers in at least ALARM of the last OF runs of the full check.
TWO_ANSWERS_OF = 3
TWO_ANSWERS_ALARM = 2


def short(commit: str | None) -> str:
    return (commit or "")[:10]


def facts(row) -> dict[str, dict]:
    """{commit: what this run found out there}, in the order it looked. Each entry has `ok` (True: it passed there,
    False: it failed there for good, None: Praxis could not tell), `log` (the run that passed, else the first that
    failed) and `failed` (the logs of every run that failed there, oldest first). A row written before failures
    were run a second time reads the same way: one failed run for each commit it called failing."""
    try:
        steps = json.loads(row["steps"] or "[]")
    except ValueError:
        steps = []
    found: dict[str, dict] = {}
    for step in steps if isinstance(steps, list) else []:
        if not isinstance(step, dict) or not step.get("commit"):
            continue
        failed = step.get("failed")
        if not isinstance(failed, list):
            failed = [step.get("log")] if step.get("ok") is False else []
        found[step["commit"]] = {**step, "failed": [log for log in failed if log]}
    tip = row["commit_id"]
    if row["result"] == "failed" and tip not in found:
        found[tip] = {"commit": tip, "ok": False, "log": row["log"], "failed": [row["log"]] if row["log"] else []}
    return found


def two_answers(row) -> list[dict]:
    """The commits where this run failed and then passed on the same files."""
    return [entry for entry in facts(row).values() if entry.get("ok") is True and entry["failed"]]


def times(count: int) -> str:
    """" once", " twice", " 3 times": how often, to go right after the verb (nothing when it is not known)."""
    return {0: "", 1: " once", 2: " twice"}.get(count, f" {count} times")


def _listed(logs: list[str]) -> str:
    return logs[0] if len(logs) == 1 else ", ".join(logs[:-1]) + " and " + logs[-1]


def _ago(when: float | None) -> str:
    from agent_praxis.status import ago

    return ago(when) if when else "earlier"


def two_answer_lines(row, passed_log: bool = False) -> list[str]:
    """What to say when the same files failed and then passed in this run; nothing when every version gave one
    answer. With *passed_log* the run that passed on the newest version is named too."""
    seen, lines = facts(row), []
    tip = seen.get(row["commit_id"])
    if row["result"] == "passed" and tip and tip["failed"]:
        count = times(len(tip["failed"]))
        if len(seen) == 1:  # no other version was looked at: the same commit was simply run again
            lines.append(
                f"It failed there{count}, then passed when Praxis ran it again on the same commit. Nobody is named "
                "and nothing needs undoing: one failed run is not proof that a landing broke anything."
            )
        else:
            lines.append(
                f"It failed there{count} first, so Praxis looked for the landing behind it and could not make the "
                "failure come back: the versions before it passed, and then this same commit passed too. Nobody is "
                "named and nothing needs undoing."
            )
        lines.append(f"The run{'s' if len(tip['failed']) > 1 else ''} that failed: {_listed(tip['failed'])}")
        if passed_log:
            lines.append(f"The run that passed: {row['log']}")
    for entry in seen.values():
        if entry["commit"] != row["commit_id"] and entry.get("ok") is True and entry["failed"]:
            lines.append(
                f"While looking for the landing, it failed{times(len(entry['failed']))} and then passed on the same "
                f"files at commit {short(entry['commit'])}. Failed: {_listed(entry['failed'])}. Passed: {entry['log']}"
            )
    if lines:
        lines.append(
            "The same files gave two answers, so the check itself is not steady: a test that sometimes fails by "
            "itself, or a PC too busy for the check's time limits."
        )
    return lines


def keeps_giving_two_answers(ctx, cmd: str) -> list[str]:
    """The notice for NEEDS YOU when the same files gave two answers in several of the last runs; else nothing."""
    rows = ctx.store.rows(
        "SELECT * FROM fullchecks WHERE command = ? AND result IN ('passed', 'failed') AND why != ? "
        "ORDER BY id DESC LIMIT ?",
        (cmd, OWN_CHECK, TWO_ANSWERS_OF),
    )
    unsteady = [(row, two_answers(row)) for row in rows]
    unsteady = [(row, entries) for row, entries in unsteady if entries]
    if len(unsteady) < TWO_ANSWERS_ALARM:
        return []
    runs = "; ".join(
        f"{_listed(entry['failed'])} (commit {short(entry['commit'])}, {_ago(row['ended'] or row['started'])})"
        for row, entries in unsteady
        for entry in entries
    )
    what = (
        f"The full check does not give one answer on the same files: in {len(unsteady)} of its last {len(rows)} "
        "runs it failed and then passed on the same commit."
    )
    why = (
        "No landing is named for those and nothing needs undoing. Find out why it does that: a test that sometimes "
        "fails by itself, or a PC too busy for the check's time limits (heavy_jobs in praxis.toml sets how many "
        "heavy jobs run at once)."
    )
    cost = (
        "Until then every such failure costs extra runs, and a break that only shows sometimes can pass as one "
        "of these."
    )
    gone = "This notice goes away after two runs in a row that give one answer each."
    return [what, why, cost, f"The runs that failed: {runs}", gone]


def _where_the_pass_is_from(ctx, row, there: dict, seen: dict) -> str:
    """One sentence on the pass of the version right before the one Praxis points at: which run showed it."""
    how, before = there.get("before_how"), there.get("before")
    if how == "search":
        run = seen.get(before, {})
        again = f", on a run made after it had failed{times(len(run['failed']))} there" if run.get("failed") else ""
        return f"That pass is from this same search{again}: {run.get('log')}"
    if how == "memory":
        return (
            "That pass is a remembered one: the same files passed this command before, and they were not run "
            "again now."
        )
    if how == "last pass":
        last = ctx.store.row(
            "SELECT ended, started FROM fullchecks WHERE command = ? AND commit_id = ? AND result = 'passed' "
            "ORDER BY id DESC LIMIT 1",
            (row["command"], before),
        )
        when = _ago(last["ended"] or last["started"]) if last is not None else "earlier"
        return f"That pass is the full check's last pass ({when}); that version was not run again now."
    return ""


def describe(ctx, row, found: str = "") -> list[str]:
    """A finished run in plain words, one line per item: for the command's own output and for `praxis status`."""
    main, commit = ctx.cfg.main_branch, short(row["commit_id"])
    found = f", found {found}" if found else ""
    if row["result"] == "passed":
        return [f"The full check passed on {main} (commit {commit}). Its output: {row['log']}", *two_answer_lines(row)]
    if row["result"] == "unknown":
        return [
            f"The full check could not judge {main} (commit {commit}{found}): {row['note']}.",
            f"Its output: {row['log']}" if row["log"] else "It wrote no output.",
            "Fix what it reports, then run it again with: praxis fullcheck",
        ]
    seen = facts(row)
    mine = seen.get(row["commit_id"], {}).get("failed", [])
    outputs = f"Its outputs: {_listed(mine)}" if len(mine) > 1 else f"Its output: {row['log']}"
    lines = [
        f"The full check is FAILING on {main} (commit {commit}{found}): it failed there{times(len(mine))}. {outputs}"
    ]
    culprit = row["culprit_commit"]
    if not culprit:
        lines.append(row["note"] or "Praxis could not tell which landing is behind it.")
        lines += two_answer_lines(row)
        lines.append("After a fix lands, check again with: praxis fullcheck")
        return lines
    there = seen.get(culprit, {})
    failed = there.get("failed", [])
    shown = (
        f"the full check failed{times(len(failed))} on {main} as it was right after it, and passed on the version "
        "right before it."
    )
    if row["culprit_agent"]:
        whose = f'{row["culprit_agent"]}\'s landing "{row["culprit_task"]}"'
        lines.append(f"It starts to fail at {whose} (commit {culprit}): {shown}")
    else:
        lines.append(
            f"It starts to fail at commit {culprit}, which no recorded landing brought in (a commit made outside "
            f"Praxis, or Praxis's own rebuild of a generated file): {shown}"
        )
    if culprit != row["commit_id"]:
        logs = failed or ([row["note"]] if row["note"] else [])
        if logs:
            lines.append(f"Output of the run{'s' if len(logs) > 1 else ''} that failed there: {_listed(logs)}")
    before = _where_the_pass_is_from(ctx, row, there, seen)
    if before:
        lines.append(before)
    lines += two_answer_lines(row)
    if row["undo"]:
        lines.append(
            f"Praxis never undoes a landing by itself. To undo this one, run in the main folder ({ctx.repo.root}):"
        )
        lines.append(f"  {row['undo']}")
        lines.append("Or have an agent land a fix. Then check again with: praxis fullcheck")
    else:
        lines.append(
            "Praxis never undoes a landing by itself, and no single `git revert` undoes this one (it holds merge "
            "commits). Have an agent land a fix, then check again with: praxis fullcheck"
        )
    return lines
