"""What a failing full check proves, and which landing it points at.

One failed run proves nothing about a landing: a PC too busy for the check's time limits fails it too, and so does
a test that sometimes fails by itself. Praxis knows a run's exit code and its log and nothing else, so the only way
to tell is to run it again. With `full_check_confirm = true` (the default):

- A pass counts on one run. A failure counts only when the same commit has failed twice.
- When the newest main version fails, the command runs a second time on that commit. If it passes then, the row
  records "failed once, then passed", nobody is named and no undo command is made.
- The search goes by the same rule: a version that fails is run again at once, and one that then passes is taken
  for good.
- A landing is named only when its version failed twice, the version right before it passed, and no run of this
  search passed after its last failure. If one did (the failures may all be older than the passes: the PC was busy
  and then was not), that version is run once more first. If it passes now it is good after all and the search goes
  on above it, and when in the end the newest version itself passes, the failure did not come back: nobody is named.

With `full_check_confirm = false` every failed run counts at once and nothing is run a second time.

What each run showed is kept in the full check's row as it goes (`steps`, JSON: one entry per commit, see
fullwords.facts), so a run that is cut short keeps it, a later run does not repeat it, and `praxis status` can say
exactly what was shown. Failures recorded since the last pass by earlier runs count: a commit that failed once
before needs one more failed run, not two.
"""

from __future__ import annotations

import json

from agent_praxis import checkcache, gitops
from agent_praxis.agents import Ctx, _tree
from agent_praxis.fullwords import facts, short, times

NEEDS_THE_HUMAN = 3  # the command's exit code for "a tool or service I need is down": no verdict on the project
OUTSIDE = (
    f"exit code {NEEDS_THE_HUMAN}, which means a problem outside the project's files (a tool or service it needs is "
    "down), so it says nothing about the landings"
)


class Search:
    """The runs of one full check: the newest main version first, then the landings since the last pass."""

    def __init__(self, run, last_pass_row):
        """*run* is fullcheck's run (its `check(commit)` runs the command there); *last_pass_row* gives the newest
        row that passed, or None, each time it is called."""
        self.run, self.ctx, self.cmd, self.tip, self.last_pass_row = run, run.ctx, run.cmd, run.tip, last_pass_row
        self.root = run.ctx.repo.root
        self.need = 2 if run.ctx.cfg.full_check_confirm else 1  # failed runs before a commit counts as failing
        self.seen: dict[str, dict] = {}  # commit -> what the runs showed there (see fullwords.facts)
        self.earlier: dict[str, list[str]] = {}  # commit -> logs of runs that failed there in earlier full checks
        self.clock = 0  # how many times the command has run in this full check
        self.last_pass = 0  # the clock at the newest run that passed
        self.failed_at: dict[str, int] = {}  # commit -> the clock at its newest run that failed
        self._learn()

    def steps(self) -> str:
        return json.dumps(list(self.seen.values()))

    def _save(self) -> None:
        with self.ctx.store.write() as db:  # as it goes: a run that is interrupted keeps what it found out
            db.execute("UPDATE fullchecks SET steps = ? WHERE id = ?", (self.steps(), self.run.id))

    def _learn(self) -> None:
        """Failed runs recorded since the last pass by earlier full checks. A pass there since wipes them."""
        passed = self.last_pass_row()
        rows = self.ctx.store.rows(
            "SELECT * FROM fullchecks WHERE command = ? AND id > ? AND id != ? ORDER BY id",
            (self.cmd, passed["id"] if passed is not None else 0, self.run.id),
        )
        for row in rows:
            for commit, entry in facts(row).items():
                if entry.get("ok") is True:
                    self.earlier.pop(commit, None)
                    continue
                known = self.earlier.setdefault(commit, [])
                known += [log for log in entry["failed"] if log not in known]

    def _remember_pass(self, commit: str) -> None:
        tree = _tree(self.root, commit)
        if tree:
            checkcache.record(self.ctx.store, tree, self.cmd)  # the same files and command: a landing reuses it too

    def judge(self, point: dict, run_it: bool = False) -> bool | None:
        """True when the full check passes on this point, False when it fails there for good (it has failed
        `need` times), None when it could not tell. With *run_it* the command runs at least once whatever is known
        already: for the newest version, and for a version Praxis is about to name."""
        ctx, commit, newest = self.ctx, point["commit"], point["commit"] == self.tip
        start = {"commit": commit, "ok": None, "log": None, "failed": list(self.earlier.get(commit, []))}
        entry = self.seen.setdefault(commit, start)
        failed = entry["failed"]
        pad, where = ("", f"on {ctx.cfg.main_branch} (commit {short(commit)})") if newest else ("    ", "there")
        if not run_it:
            whose = f"{point['agent']}'s landing" if point["agent"] else "the newest version"
            tree = _tree(self.root, commit)
            if tree and checkcache.passed_at(ctx.store, tree, self.cmd) is not None:
                ctx.say(f"  After {whose} (commit {short(commit)}): the same files already passed this command.")
                entry.update(ok=True, log=None, failed=[])
                self._save()
                return True
            if len(failed) >= self.need:
                ctx.say(
                    f"  After {whose} (commit {short(commit)}): it already failed there{times(len(failed))} in "
                    "earlier runs."
                )
                entry.update(ok=False, log=failed[0])
                self._save()
                return False
            ctx.say(f"  Checking {ctx.cfg.main_branch} as it was after {whose} (commit {short(commit)}).")
        while True:
            self.clock += 1
            code, _output, log = self.run.check(commit)
            if code == NEEDS_THE_HUMAN:
                entry.update(ok=None, log=log)
                self._save()
                return None
            if code == 0:
                self._remember_pass(commit)
                self.last_pass = self.clock
                entry.update(ok=True, log=log)
                self._save()
                if not newest:  # what the newest version's pass means is said once, when the full check ends
                    took = " this time, so Praxis takes this version for good" if failed else ""
                    ctx.say(f"{pad}It passed {where}{took}. Its output: {log}")
                return True
            again = commit in self.failed_at  # it failed on this commit before, in this very full check
            failed.append(log)
            self.failed_at[commit] = self.clock
            subject = "It" if again or not newest else "The full check"
            spot = where if not again else (f"again {where}" if newest else f"{where} again")
            ctx.say(f"{pad}{subject} FAILED {spot}. Its output: {log}")
            if len(failed) >= self.need:
                entry.update(ok=False, log=failed[0])
                self._save()
                if len(failed) > 1 and not again:
                    ctx.say(f"{pad}It failed on this commit in an earlier full check too, so this is not a one-off.")
                return False
            entry.update(ok=None, log=log)
            self._save()
            ctx.say(
                f"{pad}One failed run is not proof that a landing broke anything: a busy PC or a test that sometimes "
                f"fails by itself fails it too. Running it a second time {where}."
            )

    def unknown_note(self) -> str:
        """Why the newest version could not be judged (its last run ended with NEEDS_THE_HUMAN)."""
        if self.tip not in self.failed_at:  # its first run in this full check
            return f"it ended with {OUTSIDE}"
        failed = self.seen[self.tip]["failed"][-1]
        return f"it failed first ({failed}), and when Praxis ran it a second time it ended with {OUTSIDE}"

    # finding the landing ------------------------------------------------------------------------------------------

    def _points(self, good: str, after_landing_id: int) -> list[dict]:
        """The main version after each landing since the last pass, oldest first, ending with the version that
        failed. Each is a commit the search may check."""
        points, seen = [], {good}
        rows = self.ctx.store.rows(
            "SELECT agent, task, commit_id FROM landings WHERE ok = 1 AND commit_id IS NOT NULL AND id > ? ORDER BY id",
            (after_landing_id,),
        )
        for row in rows:
            commit = row["commit_id"]
            if commit in seen:
                continue
            seen.add(commit)
            if gitops.is_ancestor(self.root, good, commit) and gitops.is_ancestor(self.root, commit, self.tip):
                points.append({"agent": row["agent"], "task": row["task"], "commit": commit})
        if not points or points[-1]["commit"] != self.tip:  # main holds commits no landing brought (see describe)
            points.append({"agent": None, "task": None, "commit": self.tip})
        return points

    def find(self) -> dict | None:
        """Called when the newest version fails for good. The columns of the failed row that say what Praxis found:
        the landing it points at, or why it cannot tell. None when the failure did not come back: in the end the
        newest version itself passed."""
        ctx, tip, passed = self.ctx, self.tip, self.last_pass_row()
        if passed is None:
            return {
                "note": "The full check has not passed on this project before, so there is no good version to "
                "compare with, and Praxis cannot tell which landing is behind it."
            }
        good = passed["commit_id"]
        if good == tip:
            return {
                "good_commit": good,
                "note": "This same commit passed the full check before, so no landing is behind it: the full check "
                "itself gives different answers on the same files (or something outside the files changed).",
            }
        if not gitops.is_ancestor(self.root, good, tip):
            return {
                "good_commit": good,
                "note": f"The commit that last passed ({short(good)}) is no longer part of "
                f"{ctx.cfg.main_branch}, so Praxis cannot tell which landing is behind it.",
            }
        points = self._points(good, passed["landing_id"])
        if len(points) > 1:
            ctx.say(
                f"Looking for the landing behind it: {len(points)} landings since the last pass "
                f"(commit {short(good)}). Praxis checks the middle one, then the half that holds the break."
            )
        bad = {len(points) - 1}  # the points that fail for good; the lowest one above `low` is where it starts
        low = -1  # points[low] passes (-1: the last pass itself)
        while True:
            high = min(n for n in bad if n > low)
            if high - low > 1:
                middle = (low + high) // 2
                verdict = self.judge(points[middle])
            else:
                middle = high
                if self.need < 2 or self.last_pass <= self.failed_at.get(points[high]["commit"], 0):
                    break  # nothing passed since this version last failed: what was shown still stands
                ctx.say(
                    f"  A version passed after commit {short(points[high]['commit'])} last failed, so before "
                    "Praxis points at it, it runs there once more."
                )
                verdict = self.judge(points[high], run_it=True)
                if verdict is False:
                    break
            if verdict is None:
                first = points[low]["commit"] if low >= 0 else good
                return {
                    "good_commit": good,
                    "note": f"Praxis could not finish finding the landing behind it: a run ended with exit "
                    f"code {NEEDS_THE_HUMAN} (a tool or service the full check needs is down). It is one of the "
                    f"{high - low} landing(s) after commit {short(first)}.",
                }
            if verdict:
                bad.discard(middle)
                low = middle
                if not any(n > low for n in bad):
                    return None  # the newest version passed: the failure did not come back
            else:
                bad.add(middle)
        culprit = points[high]
        before = points[high - 1]["commit"] if high > 0 else good
        how = "last pass" if high == 0 else ("search" if self.seen[before]["log"] else "memory")
        self.seen[culprit["commit"]].update(before=before, before_how=how)
        self._save()
        return {
            "good_commit": good,
            "culprit_agent": culprit["agent"],
            "culprit_task": culprit["task"],
            "culprit_commit": culprit["commit"],
            "culprit_log": self.seen[culprit["commit"]]["log"],
            "undo": _undo_command(ctx, _past_rebuilds(ctx, before, culprit["commit"]), culprit["commit"]),
        }


def _past_rebuilds(ctx: Ctx, before: str, commit: str) -> str:
    """Praxis's own "Rebuild <generated file>" commits right after the landing before this one belong to that
    landing's turn, not to this landing: the commit this landing really started from."""
    from agent_praxis import generated

    if not ctx.cfg.generated:
        return before
    log = gitops.git(["log", "--reverse", "--format=%H %s", f"{before}..{commit}"], ctx.repo.root, check=False)
    for line in log.stdout.splitlines():
        sha, _, subject = line.partition(" ")
        files = gitops.changed_between(ctx.repo.root, before, sha)
        if sha == commit or not subject.startswith("Rebuild "):
            break
        if not all(generated.entry_for(ctx.cfg, path) for path in files):
            break
        before = sha
    return before


def _undo_command(ctx: Ctx, before: str, commit: str) -> str:
    """The exact git command that undoes what main gained from *before* to *commit* (one landing), or "" when no
    single revert does it."""
    root = ctx.repo.root
    merges = gitops.git(["rev-list", "--merges", "-n1", f"{before}..{commit}"], root, check=False).stdout.strip()
    if not merges:
        if gitops.commits_not_in(root, [commit], [before]) == 1:
            return f"git revert --no-edit {commit}"
        return f"git revert --no-edit {before}..{commit}"
    parents = gitops.git(["rev-parse", f"{commit}^@"], root, check=False).stdout.split()
    if len(parents) == 2 and parents[1] == before:  # land_method = "merge": the landing is one merge on top of main
        return f"git revert --no-edit -m 2 {commit}"
    return ""
