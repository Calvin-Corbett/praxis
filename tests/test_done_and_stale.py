from __future__ import annotations

import time

from conftest import edit, folder, git, praxis

from agent_praxis.store import Store


def test_done_refuses_while_work_has_not_landed(project):
    praxis("start", "alice", "jump", "scripts/player.gd")
    wt = folder(project, "alice")
    edit(wt / "scripts" / "player.gd", "extends Node\nvar speed = 99\n")
    code, out = praxis("done", "alice")
    assert code == 1 and "not saved" in out and "Nothing was removed" in out
    git(wt, "commit", "-qam", "saved")
    code, out = praxis("done", "alice")
    assert code == 1 and "1 saved change(s) that have not landed" in out
    assert wt.is_dir() and "speed = 99" in (wt / "scripts" / "player.gd").read_text()
    assert praxis("land", "alice", "--done")[0] == 0
    assert not wt.exists()
    assert "praxis/alice" not in git(project, "branch", "--list")
    assert "speed = 99" in (project / "scripts" / "player.gd").read_text()
    assert praxis("start", "bob", "player too", "scripts/player.gd")[0] == 0, "claims were released"


def test_abandon_saves_unlanded_work_on_a_branch(project):
    praxis("start", "alice", "jump", "scripts/player.gd", "scenes/level1.tscn")
    wt = folder(project, "alice")
    edit(wt / "scripts" / "player.gd", "extends Node\nvar speed = 77\n")
    (wt / "new_level.txt").write_text("unsaved idea\n")
    code, out = praxis("abandon", "alice")
    assert code == 0, out
    assert "NOT deleted" in out
    saved = [b.strip(" *") for b in git(project, "branch", "--list", "praxis-saved/*").splitlines()]
    assert len(saved) == 1 and saved[0].startswith("praxis-saved/alice-")
    assert "speed = 77" in git(project, "show", f"{saved[0]}:scripts/player.gd")
    assert git(project, "show", f"{saved[0]}:new_level.txt") == "unsaved idea"
    assert not wt.exists()
    assert praxis("start", "bob", "level", "scenes/level1.tscn")[0] == 0, "locks were released"


def test_abandon_with_no_work_saves_nothing(project):
    praxis("start", "alice", "jump", "scripts/player.gd")
    code, out = praxis("abandon", "alice")
    assert code == 0 and "nothing needed saving" in out
    assert git(project, "branch", "--list", "praxis-saved/*") == ""


def _age(project, agent, hours):
    store = Store(project / ".git" / "praxis")
    with store.write() as db:
        db.execute("UPDATE agents SET last_seen = ? WHERE name = ?", (time.time() - hours * 3600, agent))
    store.close()


def test_stale_claims_are_shown_and_released_on_request_only(project):
    praxis("start", "alice", "jump", "scripts/player.gd", "scenes/level1.tscn")
    praxis("start", "bob", "enemies", "scripts/enemy.gd")
    _age(project, "alice", 30)
    code, out = praxis("status")
    assert "alice has shown no sign of work for 30 hours" in out
    assert "praxis release-stale alice" in out
    assert "bob has shown" not in out
    code, out = praxis("start", "carol", "jump", "scenes/level1.tscn")
    assert code == 1, "stale claims still protect files until released"
    code, out = praxis("release-stale")
    assert code == 0 and "Released alice's claims (scenes/level1.tscn, scripts/player.gd)" in out
    assert "bob" not in out.split("Released")[1].split("\n")[0]
    assert folder(project, "alice").is_dir(), "the folder and its work are kept"
    assert praxis("start", "carol", "jump", "scenes/level1.tscn")[0] == 0
    assert "its claims were released" in praxis("status")[1]


def test_recent_file_edits_count_as_activity(project):
    praxis("start", "alice", "jump", "scripts/player.gd")
    _age(project, "alice", 30)
    edit(folder(project, "alice") / "scripts" / "player.gd", "extends Node\n# still working\n")
    assert "no sign of work" not in praxis("status")[1]
