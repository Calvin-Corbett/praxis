from __future__ import annotations

from conftest import folder, git, praxis

from agent_praxis.paths import normalize, overlaps


def test_start_makes_folder_branch_and_claim(project):
    code, out = praxis("start", "alice", "make the jump higher", "scripts/player.gd")
    assert code == 0, out
    wt = folder(project, "alice")
    assert (wt / "scripts" / "player.gd").is_file()
    assert git(wt, "rev-parse", "--abbrev-ref", "HEAD") == "praxis/alice"
    assert "Work ONLY in that folder" in out
    code, out = praxis("status")
    assert "alice (working)" in out and "scripts/player.gd" in out


def test_overlapping_claims_are_refused_naming_the_holder(project):
    assert praxis("start", "alice", "rework the scripts", "scripts")[0] == 0
    code, out = praxis("start", "bob", "speed up enemies", "scripts/enemy.gd")
    assert code == 1
    assert "alice holds" in out and "scripts" in out and "rework the scripts" in out
    assert "praxis done alice" in out
    assert not folder(project, "bob").exists(), "a refused start must not leave a folder behind"
    code, out = praxis("status")
    assert "bob" not in out


def test_non_overlapping_claims_work_and_widening_is_checked(project):
    assert praxis("start", "alice", "jump", "scripts/player.gd")[0] == 0
    assert praxis("start", "bob", "enemies", "scripts/enemy.gd")[0] == 0
    code, out = praxis("claim", "bob", "scripts/player.gd")
    assert code == 1 and "alice holds" in out
    code, out = praxis("claim", "bob", "art")
    assert code == 0 and "art" in out
    code, out = praxis("claim", "alice", "art/hero.png")
    assert code == 1 and "bob holds" in out


def test_unclaim_frees_a_file(project):
    praxis("start", "alice", "jump", "scripts/player.gd")
    assert praxis("unclaim", "alice", "scripts/player.gd")[0] == 0
    assert praxis("start", "bob", "jump too", "scripts/player.gd")[0] == 0


def test_windows_style_paths_and_case_are_the_same_file(project):
    assert praxis("start", "alice", "level work", "scenes\\level1.tscn")[0] == 0
    code, out = praxis("start", "bob", "level too", "Scenes/LEVEL1.tscn")
    assert code == 1 and "alice holds" in out
    # an absolute path inside the agent's own folder, written with backslashes
    absolute = str(folder(project, "alice") / "scripts" / "player.gd").replace("/", "\\")
    code, out = praxis("claim", "alice", absolute)
    assert code == 0 and "scripts/player.gd" in out


def test_paths_outside_the_project_are_refused(project, tmp_path):
    praxis("start", "alice", "jump")
    code, out = praxis("claim", "alice", "../elsewhere.txt")
    assert code == 1 and "outside your project" in out
    code, out = praxis("claim", "alice", str(tmp_path / "other" / "x.txt"))
    assert code == 1 and "outside your project" in out


def test_normalize_and_overlap_rules(tmp_path):
    root = tmp_path / "game"
    assert normalize("./a\\b/../c/", [root]) == "a/c"
    assert normalize(".", [root]) == "."
    assert normalize(str(root / "Levels" / "One.tscn"), [root]) == "Levels/One.tscn"
    assert overlaps("levels", "Levels/one.tscn")
    assert overlaps(".", "anything")
    assert not overlaps("level", "levels/one.tscn")


def test_bad_names_and_duplicate_start(project):
    code, out = praxis("start", "bad name!", "x")
    assert code == 1 and "cannot be an agent name" in out
    praxis("start", "alice", "jump")
    code, out = praxis("start", "alice", "again")
    assert code == 1 and "already working" in out
    code, out = praxis("claim", "nobody", "x")
    assert code == 1 and "no agent called nobody" in out
