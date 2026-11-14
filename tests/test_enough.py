from __future__ import annotations

from conftest import edit, folder, praxis

from agent_praxis.enough import problem_count, should_pause, signature


def attempt(ok=False, problems=None, sig="s", kind="check", summary="the check failed"):
    return {"ok": ok, "problems": problems, "signature": sig, "kind": kind, "summary": summary}


def test_rule_pauses_only_when_nothing_gets_better():
    # newest first
    assert should_pause([attempt(problems=2)] * 3, 3)
    assert should_pause([attempt(problems=3), attempt(problems=2), attempt(problems=2)], 3), "more is not better"
    assert not should_pause([attempt(problems=1), attempt(problems=2), attempt(problems=3)], 3), "getting better"
    assert not should_pause([attempt(problems=2)] * 2, 3), "too few tries"
    assert not should_pause([attempt(problems=2), attempt(problems=2), attempt(ok=True), attempt(problems=2)], 3)
    same = [attempt(sig="a")] * 3
    assert should_pause(same, 3)
    assert not should_pause([attempt(sig="a"), attempt(sig="b"), attempt(sig="c")], 3), "different failures"


def test_problem_counts_and_signatures():
    assert problem_count("== 2 failed, 5 passed, 1 error in 3.2s ==") == 3
    assert problem_count("FAILED a::b\nFAILED a::c\n") == 2
    assert problem_count("all good") is None
    assert signature("took 1.23s at C:\\x\\y") == signature("took 9.87s at C:\\z\\w")


def test_repeated_identical_failures_pause_the_agent(project):
    praxis("start", "alice", "jump", "scripts/player.gd")
    wt = folder(project, "alice")
    for n in range(3):
        edit(wt / "scripts" / "player.gd", f"BROKEN try {n}\n")
        code, out = praxis("land", "alice")
        assert code == 2
    assert "ENOUGH: alice is now paused" in out
    code, out = praxis("status")
    assert "NEEDS YOU" in out and "alice is paused and waiting for you" in out
    assert "praxis resume alice" in out
    code, out = praxis("land", "alice")
    assert code == 1 and "is paused" in out
    assert praxis("resume", "alice")[0] == 0
    edit(wt / "scripts" / "player.gd", "extends Node\nvar speed = 40\n")
    assert praxis("land", "alice")[0] == 0


def test_progress_is_never_stopped(project):
    praxis("start", "alice", "jump", "scripts")
    wt = folder(project, "alice")
    edit(wt / "scripts" / "player.gd", "BROKEN\n")
    edit(wt / "scripts" / "enemy.gd", "BROKEN\n")
    edit(wt / "scripts" / "boss.gd", "BROKEN\n")
    for fixed in ("boss.gd", "enemy.gd"):  # 3 problems, then 2, then 1: getting better each time
        praxis("land", "alice")
        edit(wt / "scripts" / fixed, "extends Node\n")
    code, out = praxis("land", "alice")
    assert code == 2 and "ENOUGH" not in out
    assert "paused" not in praxis("status")[1].split("WORKING NOW")[1].split("\n")[1]


def test_blocked_pauses_and_tells_the_human(project):
    praxis("start", "alice", "jump", "scripts/player.gd")
    code, out = praxis("blocked", "alice", "Should the jump be higher or longer?")
    assert code == 0 and "paused" in out
    code, out = praxis("status")
    assert "It asked for help: Should the jump be higher or longer?" in out
    praxis("resume", "alice")
    assert "Nothing needs you right now." in praxis("status")[1]
