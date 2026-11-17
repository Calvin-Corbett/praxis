"""A failed full check is confirmed before anyone is named: no failure counts on one run.

The fake full check of test_fullcheck.py is used, with its plan file: a run told "fail" fails whatever the files say,
the way a whole suite does on a PC too busy for its time limits.
"""

from __future__ import annotations

import json
import random
from pathlib import Path
from types import SimpleNamespace

import pytest
from conftest import git, praxis
from test_fullcheck import is_running, land, landed_commit, places, plan, ran, rows, set_lines, use_full_check, wait_for

from agent_praxis import fullsearch, fullwords
from agent_praxis.config import Config, parse_toml, render

BREAK = "extends Node\n# SLOW_BREAK\n"  # the fake full check fails on it, every time; the quick check does not see it


@pytest.fixture(autouse=True)
def _no_full_check_left_running(tmp_path):
    yield
    project = tmp_path / "my game"
    if (project / ".git" / "praxis" / "praxis.db").exists():
        wait_for(lambda: not is_running(project), "the background full check to end")


def commits(log: Path) -> list[str]:
    """The commit of every run of the fake full check so far."""
    return [commit for _where, commit in ran(log)]


def needs_you(project: Path) -> str:
    """What `praxis status` shows before the list of agents: the NEEDS YOU part, or that nothing needs you."""
    return praxis("status")[1].split("WORKING NOW")[0]


def test_one_failed_run_that_passes_when_run_again_names_nobody(project, tmp_path):
    log = use_full_check(project, tmp_path)
    assert praxis("fullcheck")[0] == 0
    land(project, "ann")
    land(project, "bob")
    tip = git(project, "rev-parse", "main")
    plan(log, "fail")  # the next run fails though nothing is broken
    code, out = praxis("fullcheck")
    assert code == 0, out
    assert "The full check FAILED on main" in out and "One failed run is not proof" in out, out
    assert "Running it a second time on main" in out and "The full check passed on main" in out, out
    assert "It failed there once, then passed when Praxis ran it again on the same commit." in out, out
    assert "Nobody is named and nothing needs undoing" in out, out
    assert "git revert" not in out and "starts to fail" not in out and "FAILING" not in out, out
    assert commits(log)[1:] == [tip, tip], "the same commit, twice"
    row = rows(project)[-1]
    assert row["result"] == "passed" and row["culprit_commit"] is None and row["undo"] is None
    (failed_log,) = fullwords.facts(row)[tip]["failed"]
    assert failed_log != row["log"] and failed_log in out and row["log"] in out
    assert "ERROR: timed out waiting for a busy PC" in Path(failed_log).read_text()
    assert "ERROR" not in Path(row["log"]).read_text()

    status = praxis("status")[1]
    assert "Nothing needs you right now." in status and "NEEDS YOU" not in status, status
    told = status.split("FULL CHECK")[1].split("LANDING LINE")[0]
    assert "last passed just now" in told and "It failed there once, then passed when Praxis ran it again" in told
    assert f"The run that failed: {failed_log}" in told and f"The run that passed: {row['log']}" in told
    assert "The same files gave two answers, so the check itself is not steady" in told, told

    # the pass counts like any other: the next failure is searched from here, and a clean run says nothing more
    assert praxis("fullcheck")[0] == 0
    assert "It failed there" not in praxis("status")[1]


def test_a_landing_is_named_only_after_two_failed_runs_on_it_and_a_pass_before_it(project, tmp_path):
    log = use_full_check(project, tmp_path)
    assert praxis("fullcheck")[0] == 0
    land(project, "ann")
    land(project, "bob")
    land(project, "cat", BREAK)
    land(project, "dan")
    tip, bob, cat = git(project, "rev-parse", "main"), landed_commit(project, "bob"), landed_commit(project, "cat")
    code, out = praxis("fullcheck")
    assert code == 2, out
    # the newest version twice; then bob's (passes: one run is enough) and cat's (fails, so it runs there again)
    assert commits(log)[1:] == [tip, tip, bob, cat, cat]
    assert "It FAILED again on main" in out and "It FAILED there again." in out, out
    assert (
        f'It starts to fail at cat\'s landing "work of cat" (commit {cat}): the full check failed twice on main as '
        "it was right after it, and passed on the version right before it." in out
    ), out
    row = rows(project)[-1]
    seen = fullwords.facts(row)
    assert row["culprit_agent"] == "cat" and len(seen[cat]["failed"]) == 2 and seen[bob]["ok"] is True
    assert seen[cat]["before"] == bob and seen[cat]["before_how"] == "search"
    assert f"That pass is from this same search: {seen[bob]['log']}" in out
    assert f"Output of the runs that failed there: {seen[cat]['failed'][0]} and {seen[cat]['failed'][1]}" in out
    assert f"  git revert --no-edit {cat}" in out


def test_in_the_search_a_version_that_fails_once_and_then_passes_is_good(project, tmp_path):
    log = use_full_check(project, tmp_path)
    assert praxis("fullcheck")[0] == 0
    land(project, "ann")
    land(project, "bob")
    land(project, "cat", BREAK)
    land(project, "dan")
    tip, bob, cat = git(project, "rev-parse", "main"), landed_commit(project, "bob"), landed_commit(project, "cat")
    plan(log, "ok", "ok", "fail")  # the third run, the search's first look at bob's version, fails for no reason
    code, out = praxis("fullcheck")
    assert code == 2, out
    assert commits(log)[1:] == [tip, tip, bob, bob, cat, cat]
    assert "It passed there this time, so Praxis takes this version for good." in out, out
    row = rows(project)[-1]
    assert row["culprit_agent"] == "cat" and row["culprit_commit"] == cat, "bob's landing did nothing and is not named"
    assert f"  git revert --no-edit {cat}" in out and bob not in out.split("git revert")[1]
    seen = fullwords.facts(row)
    assert seen[bob]["ok"] is True and len(seen[bob]["failed"]) == 1
    told = needs_you(project)
    assert f'It starts to fail at cat\'s landing "work of cat" (commit {cat})' in told, told
    assert f"it failed once and then passed on the same files at commit {bob[:10]}." in told, told
    assert seen[bob]["failed"][0] in told and "the check itself is not steady" in told
    # the pass the naming rests on was a second run: status says so
    again = f"That pass is from this same search, on a run made after it had failed once there: {seen[bob]['log']}"
    assert again in told, told


def test_failures_that_do_not_come_back_name_nobody(project, tmp_path):
    """The night this rule was written for: a busy PC failed the newest version, then an older one, and by the time
    the search reached the versions before them the PC was quiet and they passed."""
    log = use_full_check(project, tmp_path)
    assert praxis("fullcheck")[0] == 0
    names = ("ann", "bob", "cat", "dan", "eve")
    for name in names:
        land(project, name)  # nothing any of them lands is broken
    tip, at = git(project, "rev-parse", "main"), {name: landed_commit(project, name) for name in names}
    plan(log, "fail", "fail", "fail", "fail")  # the PC is busy for the first four runs
    code, out = praxis("fullcheck")
    assert code == 0, out
    # The newest twice (fails), bob's twice (fails), ann's (passes). A version passed after bob's last failed, so
    # bob's runs once more before anyone is named: it passes. The search goes on above it, cat's and dan's pass, and
    # the newest version, run once more for the same reason, passes too.
    assert commits(log)[1:] == [tip, tip, at["bob"], at["bob"], at["ann"], at["bob"], at["cat"], at["dan"], tip]
    assert f"A version passed after commit {at['bob'][:10]} last failed" in out, out
    assert "git revert" not in out and "starts to fail" not in out and "FAILING" not in out, out
    assert "could not make the failure come back" in out and "Nobody is named and nothing needs undoing" in out, out
    row = rows(project)[-1]
    assert row["result"] == "passed" and row["culprit_agent"] is None and row["undo"] is None
    seen = fullwords.facts(row)
    assert len(seen[tip]["failed"]) == 2 and len(seen[at["bob"]]["failed"]) == 2
    status = praxis("status")[1]
    assert "Nothing needs you right now." in status and "NEEDS YOU" not in status, status
    told = status.split("FULL CHECK")[1].split("LANDING LINE")[0]
    assert "It failed there twice first, so Praxis looked for the landing behind it and could not make the" in told
    assert f"The runs that failed: {seen[tip]['failed'][0]} and {seen[tip]['failed'][1]}" in told, told
    assert f"it failed twice and then passed on the same files at commit {at['bob'][:10]}" in told, told


def test_one_landing_since_the_last_pass_says_where_the_pass_before_it_is_from(project, tmp_path):
    log = use_full_check(project, tmp_path)
    assert praxis("fullcheck")[0] == 0
    land(project, "bob", BREAK)
    tip = git(project, "rev-parse", "main")
    code, out = praxis("fullcheck")
    assert code == 2 and commits(log)[1:] == [tip, tip], out
    assert f'It starts to fail at bob\'s landing "work of bob" (commit {tip}): the full check failed twice' in out, out
    assert "That pass is the full check's last pass (just now); that version was not run again now." in out, out
    assert "Output of the run" not in out, "the failed runs of the newest version are named once, in the first line"


def test_a_landing_named_on_two_failed_runs_is_cleared_when_the_same_commit_passes_later(project, tmp_path):
    """What two failed runs cannot rule out: something outside the files that fails both. The pass before the only
    landing is the last pass, which is not run again, so that landing is named. Status says which pass it rests on,
    and the next run that passes on the same commit takes the name back."""
    log = use_full_check(project, tmp_path)
    assert praxis("fullcheck")[0] == 0
    land(project, "bob")  # nothing is broken
    tip = git(project, "rev-parse", "main")
    plan(log, "fail", "fail")
    code, out = praxis("fullcheck")
    assert code == 2 and f'It starts to fail at bob\'s landing "work of bob" (commit {tip})' in out, out
    assert "That pass is the full check's last pass (just now); that version was not run again now." in out, out
    code, out = praxis("fullcheck")
    assert code == 0 and commits(log)[1:] == [tip, tip, tip], out
    assert "It failed there twice, then passed when Praxis ran it again on the same commit." in out, out
    assert "Nobody is named and nothing needs undoing" in out and "git revert" not in out, out
    status = praxis("status")[1]
    assert "Nothing needs you right now." in status and "starts to fail" not in status and "git revert" not in status


def test_a_second_run_that_cannot_judge_blames_nobody(project, tmp_path):
    log = use_full_check(project, tmp_path)
    assert praxis("fullcheck")[0] == 0
    land(project, "bob")
    plan(log, "fail", "down")
    code, out = praxis("fullcheck")
    assert code == 2 and "could not judge main" in out and "exit code 3" in out, out
    assert "it failed first (" in out and "when Praxis ran it a second time it ended with exit code 3" in out, out
    assert "git revert" not in out and "starts to fail" not in out
    row = rows(project)[-1]
    assert row["result"] == "unknown" and row["culprit_commit"] is None
    assert "could not judge main" in needs_you(project)
    # the failure it saw is not lost: one more failed run on that commit is the second
    plan(log, "fail")
    code, out = praxis("fullcheck")
    assert code == 2 and len(commits(log)) == 4, out
    assert "It failed on this commit in an earlier full check too, so this is not a one-off." in out, out


def test_it_needs_you_only_when_it_keeps_failing_and_then_passing(project, tmp_path):
    log = use_full_check(project, tmp_path)
    plan(log, "fail")
    assert praxis("fullcheck")[0] == 0  # failed, then passed: once is told under FULL CHECK and needs nobody
    assert "Nothing needs you right now." in needs_you(project)
    plan(log, "fail")
    assert praxis("fullcheck")[0] == 0  # and again
    told = needs_you(project)
    assert "NEEDS YOU" in told, told
    assert (
        "The full check does not give one answer on the same files: in 2 of its last 2 runs it failed and then "
        "passed on the same commit." in told
    ), told
    assert "No landing is named for those and nothing needs undoing." in told and "git revert" not in told
    failed = [fullwords.facts(row)[row["commit_id"]]["failed"][0] for row in rows(project)]
    assert len(failed) == 2 and all(name in told for name in failed), told
    assert praxis("fullcheck")[0] == 0  # one clean run: still 2 of the last 3
    assert "in 2 of its last 3 runs" in needs_you(project)
    assert praxis("fullcheck")[0] == 0  # two clean runs in a row: the notice goes
    assert "Nothing needs you right now." in needs_you(project)


def test_every_run_takes_a_heavy_job_place_of_its_own(project, tmp_path):
    log = use_full_check(project, tmp_path)
    assert praxis("fullcheck")[0] == 0
    land(project, "ann")
    land(project, "bob", BREAK)
    assert praxis("fullcheck")[0] == 2
    taken = places(log)
    assert len(taken) == len(commits(log)) == 5  # the first pass; the newest twice, ann's, the newest once more
    assert all(taken) and len(set(taken)) == len(taken), "one place per run, given back before the next run asks"


def test_full_check_confirm_false_counts_every_failed_run_at_once(project, tmp_path):
    parsed = parse_toml(render(Config()))
    assert parsed["full_check_confirm"] is True, "on unless you turn it off"
    assert parse_toml(render(Config(full_check_confirm=False)))["full_check_confirm"] is False
    log = use_full_check(project, tmp_path)
    set_lines(project, full_check_confirm="false")
    assert praxis("fullcheck")[0] == 0
    land(project, "ann")
    land(project, "bob", BREAK)
    land(project, "cat")
    land(project, "dan")
    tip, bob, ann = git(project, "rev-parse", "main"), landed_commit(project, "bob"), landed_commit(project, "ann")
    code, out = praxis("fullcheck")
    assert code == 2, out
    assert commits(log)[1:] == [tip, bob, ann], "one run for each verdict, as before"
    assert "): it failed there once. Its output: " in out and "second time" not in out, out
    assert f'at bob\'s landing "work of bob" (commit {bob}): the full check failed once on main' in out, out


def test_a_row_written_before_failures_were_confirmed_reads_as_one_failed_run_each():
    steps = [{"commit": "bob", "ok": False, "log": "bob.log"}, {"commit": "ann", "ok": True, "log": "ann.log"}]
    old = {"commit_id": "tip", "result": "failed", "log": "first.log", "steps": json.dumps(steps)}
    seen = fullwords.facts(old)
    assert seen["tip"]["failed"] == ["first.log"] and seen["bob"]["failed"] == ["bob.log"]
    assert seen["ann"]["failed"] == [] and fullwords.two_answers(old) == []


# --- the rule itself, on any answers the command can give -------------------------------------------------------------


class Answers:
    """Stands in for fullcheck's run: no project and no command, only what a run on each version answers."""

    def __init__(self, versions: int, first_bad: int, noise: float, rng: random.Random):
        self.points = [{"agent": f"agent{n}", "task": "work", "commit": f"c{n}"} for n in range(versions)]
        self.first_bad, self.noise, self.rng = first_bad, noise, rng
        self.cmd, self.id, self.tip, self.runs = "the full check", 1, f"c{versions - 1}", []
        cfg = SimpleNamespace(full_check_confirm=True, main_branch="main")
        store = SimpleNamespace(rows=lambda *_a: [])  # no earlier full checks
        self.ctx = SimpleNamespace(cfg=cfg, store=store, repo=SimpleNamespace(root=Path(".")), say=lambda _t: None)

    def check(self, commit: str) -> tuple[int, str, str]:
        """A version from the first bad one on always fails. A good one fails too, now and then (a busy PC)."""
        assert len(self.runs) < 500, "the search never ended"
        broken = int(commit[1:]) >= self.first_bad
        code = 1 if broken or self.rng.random() < self.noise else 0
        self.runs.append((commit, code))
        return code, "", f"run-{len(self.runs)}.log"


def searched(monkeypatch, versions: int, first_bad: int, noise: float, seed: int):
    """(the run, what the search answered) when the newest of *versions* fails for good; None when it passed."""
    run = Answers(versions, first_bad, noise, random.Random(seed))
    monkeypatch.setattr(fullsearch, "_tree", lambda *_a: None)  # nothing is remembered from before
    monkeypatch.setattr(fullsearch.Search, "_save", lambda _self: None)
    monkeypatch.setattr(fullsearch.Search, "_points", lambda _self, _good, _after: run.points)
    monkeypatch.setattr(fullsearch.gitops, "is_ancestor", lambda *_a: True)
    monkeypatch.setattr(fullsearch, "_past_rebuilds", lambda _ctx, before, _commit: before)
    monkeypatch.setattr(fullsearch, "_undo_command", lambda _ctx, _before, commit: f"git revert {commit}")
    search = fullsearch.Search(run, lambda: {"id": 1, "commit_id": "good", "landing_id": 0})
    if search.judge({"agent": None, "task": None, "commit": run.tip}, run_it=True) is not False:
        return run, None
    return run, search.find()


def test_a_steady_break_is_found_wherever_it_is_and_costs_the_runs_the_readme_says(monkeypatch):
    for versions in range(1, 10):
        steps = (versions - 1).bit_length()  # how many versions a search of this many landings looks at, at most
        for first_bad in range(versions):
            run, found = searched(monkeypatch, versions, first_bad, noise=0.0, seed=0)
            assert found["culprit_commit"] == f"c{first_bad}", (versions, first_bad, run.runs)
            assert 2 <= len(run.runs) <= 2 + 2 * steps + 1, (versions, first_bad, run.runs)
    run, found = searched(monkeypatch, 5, 2, noise=0.0, seed=0)
    assert [commit for commit, _code in run.runs] == ["c4", "c4", "c1", "c2", "c2"]  # 5 runs; it was 3


def test_whatever_the_runs_answer_a_landing_is_named_only_on_two_failures_a_pass_before_and_none_after(monkeypatch):
    named = cleared = 0
    for seed in range(400):
        rng = random.Random(seed)
        versions = rng.randint(1, 9)
        first_bad = rng.randint(0, versions)  # == versions: nothing is broken, every failure is the busy PC
        run, found = searched(monkeypatch, versions, first_bad, noise=rng.choice([0.2, 0.5, 0.8]), seed=seed)
        commits_run = [commit for commit, _code in run.runs]
        if found is None:  # nobody is named: the newest version passed, on its first run or in the end
            assert run.runs[-1] == (run.tip, 0), (seed, run.runs)
            cleared += 1
            continue
        named += 1
        culprit = found["culprit_commit"]
        failures = [n for n, (commit, code) in enumerate(run.runs) if commit == culprit and code]
        passes = [n for n, (_commit, code) in enumerate(run.runs) if not code]
        assert len(failures) >= 2, (seed, run.runs)
        assert not passes or passes[-1] < failures[-1], f"seed {seed}: a version passed after its last failure"
        before = int(culprit[1:]) - 1
        if before >= 0:  # else the version before it is the last pass itself, which is not run again
            assert (f"c{before}", 0) in run.runs, f"seed {seed}: the version before it never passed"
        assert run.runs[commits_run.index(run.tip) + 1][0] == run.tip, "the newest version's second run comes first"
        if first_bad < versions:  # a real break is never passed over: nothing after it is ever named
            assert int(culprit[1:]) <= first_bad, (seed, run.runs)
    assert named > 20 and cleared > 20, (named, cleared)  # both outcomes were tried
