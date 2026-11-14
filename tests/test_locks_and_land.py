from __future__ import annotations

from conftest import edit, folder, git, praxis

from agent_praxis import gitops


def test_asset_files_are_locks_shown_in_status(project):
    assert praxis("start", "alice", "level layout", "scenes/level1.tscn")[0] == 0
    code, out = praxis("status")
    assert "LOCKED ASSET FILES" in out and "scenes/level1.tscn  held by alice" in out
    code, out = praxis("start", "bob", "also the level", "scenes")
    assert code == 1 and "the asset file scenes/level1.tscn" in out


def test_landing_refuses_an_asset_another_agent_holds(project):
    praxis("start", "alice", "level layout", "scenes/level1.tscn")
    praxis("start", "bob", "enemies", "scripts/enemy.gd")
    bob = folder(project, "bob")
    (bob / "scenes" / "level1.tscn").write_bytes(b"bob changed the level\x00")
    code, out = praxis("land", "bob")
    assert code == 2
    assert "scenes/level1.tscn (held by alice)" in out
    assert (bob / "scenes" / "level1.tscn").read_bytes() == b"bob changed the level\x00", "work kept"
    assert gitops.commits_ahead(project, "main", "praxis/bob") == 1
    assert (project / "scenes" / "level1.tscn").read_bytes().startswith(b"[gd_scene"), "main untouched"


def test_unclaimed_asset_lands_with_a_note(project):
    praxis("start", "bob", "new hero art", "scripts/enemy.gd")
    (folder(project, "bob") / "art" / "hero.png").write_bytes(b"new art")
    code, out = praxis("land", "bob")
    assert code == 0, out
    assert "changed without being claimed" in out
    assert (project / "art" / "hero.png").read_bytes() == b"new art"


def test_successful_landing_updates_the_main_folder(project):
    praxis("start", "alice", "jump higher", "scripts/player.gd")
    edit(folder(project, "alice") / "scripts" / "player.gd", "extends Node\nvar speed = 20\n")
    code, out = praxis("land", "alice")
    assert code == 0, out
    assert "Landed" in out and "check passed" in out
    assert "speed = 20" in (project / "scripts" / "player.gd").read_text()
    code, out = praxis("status")
    assert 'alice landed "jump higher" (check passed)' in out


def test_failing_check_keeps_the_work_and_reports_the_failure(project):
    praxis("start", "alice", "jump higher", "scripts/player.gd")
    wt = folder(project, "alice")
    edit(wt / "scripts" / "player.gd", "BROKEN\n")
    before = git(project, "rev-parse", "main")
    code, out = praxis("land", "alice")
    assert code == 2
    assert "NOT landed: the check failed (1 problem(s))" in out
    assert "ERROR: broken script" in out, "the agent sees the check's own output"
    assert git(project, "rev-parse", "main") == before
    assert (wt / "scripts" / "player.gd").read_text() == "BROKEN\n"
    assert gitops.commits_ahead(project, "main", "praxis/alice") == 1
    assert "BROKEN" not in (project / "scripts" / "player.gd").read_text()
    code, out = praxis("status")
    assert "alice could NOT land" in out
    # fixing it and landing again works
    edit(wt / "scripts" / "player.gd", "extends Node\nvar speed = 30\n")
    assert praxis("land", "alice")[0] == 0


def test_second_agent_lands_on_top_of_the_first(project):
    praxis("start", "alice", "jump", "scripts/player.gd")
    praxis("start", "bob", "enemies", "scripts/enemy.gd")
    edit(folder(project, "alice") / "scripts" / "player.gd", "extends Node\nvar speed = 11\n")
    edit(folder(project, "bob") / "scripts" / "enemy.gd", "extends Node\nvar hp = 5\n")
    assert praxis("land", "alice")[0] == 0
    code, out = praxis("land", "bob")
    assert code == 0, out
    assert "speed = 11" in (project / "scripts" / "player.gd").read_text()
    assert "hp = 5" in (project / "scripts" / "enemy.gd").read_text()


def test_clash_in_an_unclaimed_file_keeps_work_and_names_the_file(project):
    praxis("start", "alice", "readme a")
    praxis("start", "bob", "readme b")
    edit(folder(project, "alice") / "notes.txt", "alice\n")
    edit(folder(project, "bob") / "notes.txt", "bob\n")
    assert praxis("land", "alice")[0] == 0
    code, out = praxis("land", "bob")
    assert code == 2
    assert "clash" in out and "notes.txt" in out
    bob = folder(project, "bob")
    assert gitops.operation_in_progress(bob) is None, "the half-done rebase was undone"
    assert (bob / "notes.txt").read_text() == "bob\n"


def test_nothing_to_land(project):
    praxis("start", "alice", "jump", "scripts/player.gd")
    code, out = praxis("land", "alice")
    assert code == 0 and "nothing new to land" in out


def test_merge_method_also_lands(project):
    toml = project / "praxis.toml"
    toml.write_text(toml.read_text().replace('land_method = "rebase"', 'land_method = "merge"'))
    praxis("start", "alice", "jump", "scripts/player.gd")
    praxis("start", "bob", "enemies", "scripts/enemy.gd")
    edit(folder(project, "alice") / "scripts" / "player.gd", "extends Node\nvar speed = 12\n")
    edit(folder(project, "bob") / "scripts" / "enemy.gd", "extends Node\nvar hp = 6\n")
    assert praxis("land", "alice")[0] == 0
    assert praxis("land", "bob")[0] == 0
    assert "hp = 6" in (project / "scripts" / "enemy.gd").read_text()
    assert "speed = 12" in (project / "scripts" / "player.gd").read_text()
