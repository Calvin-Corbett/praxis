"""The check result cache: the same tree that already passed the same command is not checked again."""

from __future__ import annotations

import sqlite3

from conftest import edit, folder, git, praxis

from agent_praxis import checkcache
from agent_praxis import store as store_module
from agent_praxis.store import Store


def _land(project, agent: str, text: str, name: str = "player") -> tuple[int, str]:
    edit(folder(project, agent) / "scripts" / f"{name}.gd", text)
    return praxis("land", agent)


def test_the_same_tree_is_not_checked_twice(project):
    praxis("start", "alice", "jump", "scripts/player.gd")
    code, out = _land(project, "alice", "extends Node\nvar speed = 11\n")
    assert code == 0 and "Running the check" in out and "Skipping the check" not in out
    code, out = _land(project, "alice", "extends Node\nvar speed = 12\n")
    assert code == 0 and "Running the check" in out
    # back to the exact files of the first landing: that tree already passed
    code, out = _land(project, "alice", "extends Node\nvar speed = 11\n")
    assert code == 0, out
    assert "Skipping the check: same tree already passed at " in out and "Running the check" not in out
    assert "(check passed, same tree already passed)" in out
    assert 'alice landed "jump" (check passed, same tree already passed)' in praxis("status")[1]


def test_a_different_command_never_reuses_a_pass(project):
    praxis("start", "alice", "jump", "scripts/player.gd")
    _land(project, "alice", "extends Node\nvar speed = 11\n")
    _land(project, "alice", "extends Node\nvar speed = 12\n")
    toml = project / "praxis.toml"
    toml.write_text(toml.read_text().replace("check.py", "check.py --other"))  # the command text now differs
    code, out = _land(project, "alice", "extends Node\nvar speed = 11\n")
    assert code == 0, out
    assert "Running the check" in out and "Skipping the check" not in out


def test_a_failure_is_never_remembered(project):
    praxis("start", "alice", "jump", "scripts/player.gd")
    for _ in range(2):
        code, out = _land(project, "alice", "BROKEN\n")
        assert code == 2 and "Running the check" in out and "Skipping the check" not in out
    store = Store(project / ".git" / "praxis")
    try:
        assert checkcache.count(store) == 0
    finally:
        store.close()


def test_a_skipped_check_still_lands_the_work(project):
    praxis("start", "alice", "jump", "scripts/player.gd")
    _land(project, "alice", "extends Node\nvar speed = 11\n")
    _land(project, "alice", "extends Node\nvar speed = 12\n")
    _land(project, "alice", "extends Node\nvar speed = 11\n")
    assert "speed = 11" in (project / "scripts" / "player.gd").read_text()
    assert git(project, "rev-parse", "main") == git(project, "rev-parse", "praxis/alice")


def test_only_the_newest_200_passes_are_kept(tmp_path):
    store = Store(tmp_path)
    try:
        for n in range(205):
            checkcache.record(store, f"tree{n}", "cmd")
        assert checkcache.count(store) == 200
        assert checkcache.passed_at(store, "tree0", "cmd") is None
        assert checkcache.passed_at(store, "tree204", "cmd") is not None
    finally:
        store.close()


def test_a_pass_is_keyed_on_the_tree_and_the_exact_command(tmp_path):
    store = Store(tmp_path)
    try:
        checkcache.record(store, "t1", "run tests")
        assert checkcache.passed_at(store, "t1", "run tests") is not None
        assert checkcache.passed_at(store, "t1", "run tests ") is None
        assert checkcache.passed_at(store, "t1", "run other") is None
        assert checkcache.passed_at(store, "t2", "run tests") is None
        checkcache.record(store, "t1", "run tests")  # again: one entry, not two
        assert checkcache.count(store) == 1
    finally:
        store.close()


def test_a_format_3_database_is_upgraded_and_gets_the_cache_table(tmp_path):
    db = sqlite3.connect(str(tmp_path / "praxis.db"))
    for script in (store_module.SCHEMA, store_module.UPGRADES[2], store_module.UPGRADES[3]):
        for statement in store_module._statements(script):
            db.execute(statement)
    db.execute("PRAGMA user_version = 3")
    db.commit()
    db.close()
    store = Store(tmp_path)
    try:
        assert store.row("PRAGMA user_version")[0] == store_module.FORMAT == 6
        assert checkcache.count(store) == 0
    finally:
        store.close()
