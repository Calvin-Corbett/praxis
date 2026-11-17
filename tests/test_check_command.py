"""`praxis check <agent>`: the checks of an agent's saved work, run before the landing line, and reused by it."""

from __future__ import annotations

from pathlib import Path

from conftest import edit, folder, git, praxis, praxis_process
from test_inputs import land, named_check, new_agent, runs, set_line, use_checks, wait_for

from agent_praxis import instructions
from agent_praxis.store import Store


def save(project: Path, name: str) -> str:
    """Commit what the agent has in its folder (what an agent does before `praxis check`). Returns the commit."""
    where = folder(project, name)
    git(where, "add", "-A")
    git(where, "commit", "-q", "-m", f"work of {name}")
    return git(where, "rev-parse", "HEAD")


def scripts_check(project: Path, tmp_path: Path, **more) -> None:
    use_checks(project, named_check(tmp_path, "scripts", ["scripts/player.gd", "scripts/enemy.gd"], **more))


def test_a_pass_earned_before_the_line_is_reused_by_the_landing_even_after_main_moved(project, tmp_path):
    scripts_check(project, tmp_path)
    new_agent(project, "alice", "scripts/player.gd", "extends Node\nvar speed = 11\n")
    saved, main = save(project, "alice"), git(project, "rev-parse", "main")
    code, out = praxis("check", "alice")
    assert code == 0, out
    assert "Checking alice's saved work on top of the newest main" in out and "Running check scripts: " in out
    assert "  - scripts: passed. Its output: " in out and "All passed, and Praxis remembers it." in out
    assert "reads the whole project" not in out and runs(tmp_path) == ["scripts"]
    # nothing moved: not main, not alice's branch, not alice's folder
    assert git(project, "rev-parse", "main") == main and git(project, "rev-parse", "praxis/alice") == saved
    assert git(folder(project, "alice"), "rev-parse", "HEAD") == saved
    assert git(folder(project, "alice"), "status", "--porcelain") == ""
    assert git(folder(project, "alice"), "branch", "--show-current") == "praxis/alice"
    assert "speed = 10" in (project / "scripts" / "player.gd").read_text(), "nothing landed"
    assert "RECENT LANDINGS: none yet" in praxis("status")[1]

    land(project, "bob", "notes/todo.txt", "the main version moves on\n")
    code, out = praxis("land", "alice")
    assert code == 0, out
    assert "Skipping check scripts: it passed at " in out and "Running check" not in out, out
    assert "check passed (scripts from memory)" in out and runs(tmp_path) == ["scripts"]
    assert "speed = 11" in (project / "scripts" / "player.gd").read_text()


def test_the_one_whole_project_check_is_reused_only_while_main_stays_the_same(project):
    new_agent(project, "alice", "scripts/player.gd", "extends Node\nvar speed = 11\n")
    save(project, "alice")
    code, out = praxis("check", "alice")
    assert code == 0 and "Running the check: " in out and "  - the check: passed." in out, out
    assert "Note: the check reads the whole project" in out
    code, out = praxis("check", "alice")
    assert code == 0 and "  - the check: reused, it had already passed at " in out and "Running" not in out, out
    code, out = praxis("land", "alice")
    assert code == 0 and "Skipping the check: same tree already passed at " in out, out

    new_agent(project, "carol", "scripts/enemy.gd", "extends Node\nvar hp = 4\n")
    save(project, "carol")
    assert praxis("check", "carol")[0] == 0
    land(project, "dave", "notes/todo.txt")  # main moves: carol's whole tree is another one now
    code, out = praxis("land", "carol")
    assert code == 0 and "Running the check: " in out and "Skipping" not in out, out


def test_a_failing_check_lands_nothing_and_is_not_a_landing_try(project, tmp_path):
    scripts_check(project, tmp_path)
    new_agent(project, "alice", "scripts/player.gd", "BROKEN\n")
    save(project, "alice")
    for _try in range(4):  # more often than landing tries are allowed to fail the same way
        code, out = praxis("check", "alice")
        assert code == 2 and "  - scripts: FAILED (exit code 1). Its output: " in out, out
        assert "ERROR: broken script scripts/player.gd" in out and "does not count as a landing try" in out
    status = praxis("status")[1]
    assert "RECENT LANDINGS: none yet" in status and "paused" not in status
    assert len(runs(tmp_path)) == 4, "a failure is never remembered"
    edit(folder(project, "alice") / "scripts" / "player.gd", "extends Node\nvar speed = 12\n")
    save(project, "alice")
    assert praxis("check", "alice")[0] == 0
    code, out = praxis("land", "alice")
    assert code == 0 and "Skipping check scripts: " in out, out


def test_only_saved_work_is_checked(project, tmp_path):
    scripts_check(project, tmp_path)
    new_agent(project, "alice", "scripts/player.gd", "extends Node\nvar speed = 11\n")
    code, out = praxis("check", "alice")
    assert code == 0 and "no saved work" in out and "nothing to check" in out, out
    assert "1 file(s) in alice's folder are not saved (committed) yet" in out and runs(tmp_path) == []
    save(project, "alice")
    edit(folder(project, "alice") / "scripts" / "enemy.gd", "BROKEN\n")  # not saved: not part of the check
    code, out = praxis("check", "alice")
    assert code == 0 and "1 file(s) in alice's folder are not saved (committed) yet" in out, out
    assert "BROKEN" in (folder(project, "alice") / "scripts" / "enemy.gd").read_text(), "the folder is left alone"


def test_work_that_clashes_with_the_newest_main_is_not_checked(project, tmp_path):
    scripts_check(project, tmp_path)
    new_agent(project, "alice", "scripts/player.gd", "extends Node\nvar speed = 11\n")
    save(project, "alice")
    land(project, "bob", "scripts/player.gd", "extends Node\nvar speed = 99\n")
    before = len(runs(tmp_path))
    code, out = praxis("check", "alice")
    assert code == 2 and "NOT checked" in out and "  - scripts/player.gd" in out, out
    assert "git rebase main" in out and len(runs(tmp_path)) == before
    scratch = folder(project, "_check-1")
    assert not list(Path(git(scratch, "rev-parse", "--absolute-git-dir")).glob("rebase-*")), "no half-done rebase"


def test_it_holds_a_heavy_job_place_and_runs_in_a_scratch_folder(project, tmp_path):
    gates = tmp_path / "gates"
    gates.mkdir()
    scripts_check(project, tmp_path, gates=gates)
    new_agent(project, "alice", "scripts/player.gd", "extends Node\nvar speed = 11\n")
    save(project, "alice")
    proc = praxis_process(project, "check", "alice")
    try:
        wait_for(lambda: runs(tmp_path), "the check to start")
        store = Store(project / ".git" / "praxis")
        try:
            jobs = [dict(r) for r in store.rows("SELECT agent, command FROM heavy WHERE started IS NOT NULL")]
        finally:
            store.close()
        assert jobs == [{"agent": "alice (praxis check)", "command": "praxis check alice"}]
        assert folder(project, "_check-1").is_dir()
        assert "alice (praxis check)" in praxis("status")[1]
    finally:
        (gates / "all").write_text("")
    out = proc.communicate(timeout=180)[0]
    assert proc.returncode == 0 and "All passed" in out, out
    assert f"scratch folder {folder(project, '_check-1')}" in out


def test_with_land_method_merge_the_pass_is_found_too(project, tmp_path):
    scripts_check(project, tmp_path)
    set_line(project, 'land_method = "merge"')
    new_agent(project, "alice", "scripts/player.gd", "extends Node\nvar speed = 11\n")
    save(project, "alice")
    land(project, "bob", "notes/todo.txt")
    code, out = praxis("check", "alice")
    assert code == 0 and "  - scripts: passed." in out, out
    code, out = praxis("land", "alice")
    assert code == 0 and "Skipping check scripts: " in out and runs(tmp_path) == ["scripts"], out


def test_an_agent_that_is_not_working_cannot_be_checked(project):
    code, out = praxis("check", "nobody")
    assert code == 1 and "There is no agent called nobody" in out, out


def test_the_agent_rules_say_to_check_before_landing():
    assert "run `praxis check <your-name>` before `praxis land`" in instructions.BODY
