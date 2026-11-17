"""The audit behind `inputs`: a pass taken from memory is run again now and then, and a wrong memory is shown."""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest
from conftest import folder, praxis
from test_inputs import land, named_check, remembered, runs, use_checks, wait_for

from agent_praxis import checkaudit
from agent_praxis import store as store_module
from agent_praxis.store import Store


def trust_row(project: Path, name: str) -> dict | None:
    store = Store(project / ".git" / "praxis")
    try:
        row = store.row("SELECT * FROM checktrust WHERE name = ?", (name,))
        return None if row is None else dict(row)
    finally:
        store.close()


def audit_running(project: Path) -> bool:
    store = Store(project / ".git" / "praxis")
    try:
        return checkaudit.running(store)
    finally:
        store.close()


def wait_audited(project: Path, name: str, result: str, audits: int = 1) -> dict:
    """Wait until the named check's newest audit has ended with *result* (its *audits*-th audit) and no audit runs."""

    def done():
        row = trust_row(project, name)
        logs = list((project / ".git" / "praxis" / "logs").glob("audit-background-*.log"))
        ok = row and row["audit_result"] == result and len(logs) >= audits and not audit_running(project)
        return row if ok else None

    return wait_for(done, f"audit {audits} of check {name} to end with {result}")


@pytest.fixture(autouse=True)
def _no_audit_left_running(tmp_path):
    yield
    project = tmp_path / "my game"
    if (project / ".git" / "praxis" / "praxis.db").exists():
        wait_for(lambda: not audit_running(project), "the background audit to end")


def _incomplete(project: Path, tmp_path: Path, every: int = 1) -> None:
    """A check that really reads both scripts, but says it reads only player.gd."""
    use_checks(
        project,
        named_check(
            tmp_path, "scripts", ["scripts/player.gd", "scripts/enemy.gd"], paths=["**"], inputs=["scripts/player.gd"]
        ),
        audit_every=str(every),
    )


def test_an_incomplete_inputs_list_is_found_shown_and_not_trusted_until_you_say_so(project, tmp_path):
    _incomplete(project, tmp_path)
    land(project, "alice", "scripts/player.gd", "extends Node\nvar speed = 11\n")
    out = land(project, "bob", "scripts/enemy.gd", "BROKEN\n")  # the check reads it, its `inputs` do not say so
    assert "Skipping check scripts: " in out and "Landed: bob's work" in out, out
    assert "The audit (audit_every = 1): check scripts passed from memory" in out, out
    row = wait_audited(project, "scripts", "failed")
    assert "ERROR: broken script scripts/enemy.gd" in Path(row["broken_log"]).read_text()
    assert row["broken_agent"] == "bob" and runs(tmp_path) == ["scripts", "scripts"]
    assert folder(project, "_audit").is_dir(), "it ran in a scratch folder of its own"
    assert remembered(project) == {}, "its remembered passes were dropped"
    status = praxis("status")[1]
    assert "NEEDS YOU" in status, status
    assert "check scripts passed from memory but fails when run: its inputs list is probably missing files" in status
    assert row["broken_log"] in status and "praxis trust scripts" in status and "bob's landing" in status
    assert "NOT remembered: its audit failed" in status

    out = land(project, "carol", "scripts/enemy.gd", "extends Node\nvar hp = 4\n")  # the fix
    assert "Running check scripts: " in out, out
    out = land(project, "dave", "notes/a.txt")
    assert "Running check scripts: " in out and "Skipping" not in out, "not trusted: it runs every time"
    assert remembered(project) == {} and "The audit" not in out

    code, out = praxis("trust", "scripts")
    assert code == 0 and "Check scripts is trusted again" in out, out
    status = praxis("status")[1]
    assert "Nothing needs you right now." in status and "passed from memory but fails" not in status
    out = land(project, "erin", "notes/b.txt")
    assert "Running check scripts: " in out and remembered(project) == {"scripts": 1}, "memory starts from none"
    out = land(project, "frank", "notes/c.txt")
    assert "Skipping check scripts: " in out and "The audit (audit_every = 1)" in out, out
    wait_audited(project, "scripts", "passed", audits=2)
    assert "Nothing needs you right now." in praxis("status")[1]


def test_a_right_memory_passes_its_audit_and_only_one_pass_in_n_is_audited(project, tmp_path):
    use_checks(project, named_check(tmp_path, "scripts", ["scripts/player.gd"], paths=["**"]), audit_every="2")
    land(project, "alice", "scripts/player.gd", "extends Node\nvar speed = 11\n")
    out = land(project, "bob", "notes/a.txt")
    assert "Skipping check scripts: " in out and "The audit (audit_every = 2)" in out, "the first pass from memory"
    row = wait_audited(project, "scripts", "passed")
    assert row["broken_at"] is None and remembered(project) == {"scripts": 1}
    assert runs(tmp_path) == ["scripts", "scripts"], "the landing's own run, and the audit"
    out = land(project, "carol", "notes/b.txt")
    assert "Skipping check scripts: " in out and "The audit" not in out, "the second is not audited"
    out = land(project, "dave", "notes/c.txt")
    assert "Skipping check scripts: " in out and "The audit (audit_every = 2)" in out, "the third is"
    wait_audited(project, "scripts", "passed", audits=2)
    assert "Nothing needs you right now." in praxis("status")[1]


def test_audit_every_zero_turns_it_off(project, tmp_path):
    _incomplete(project, tmp_path, every=0)
    land(project, "alice", "scripts/player.gd", "extends Node\nvar speed = 11\n")
    out = land(project, "bob", "scripts/enemy.gd", "BROKEN\n")
    assert "Skipping check scripts: " in out and "The audit" not in out, out
    row = trust_row(project, "scripts")
    assert row["reuses"] == 1 and row["audited"] is None and row["audit_result"] is None
    assert not folder(project, "_audit").exists() and runs(tmp_path) == ["scripts"]
    assert "audit: off (audit_every = 0)" in praxis("status")[1]


def test_the_first_pass_from_memory_is_audited_and_then_one_in_n(tmp_path):
    store = Store(tmp_path)
    try:
        due = []
        for _n in range(7):
            found = checkaudit._count(store, ["ui"], 3)
            due.append(bool(found))
            if found:  # what the audit's own process does when it starts
                with store.write() as db:
                    db.execute("UPDATE checktrust SET audited = reuses WHERE name = 'ui'")
        assert due == [True, False, False, True, False, False, True]
        assert checkaudit._count(store, ["other"], 0) == [], "0 never audits"
    finally:
        store.close()


def test_trust_names_the_checks_it_knows(project, tmp_path):
    use_checks(project, named_check(tmp_path, "scripts", ["scripts/player.gd"]))
    code, out = praxis("trust", "nope")
    assert code == 1 and "no check named nope (named checks: scripts)" in out, out
    code, out = praxis("trust", "scripts")
    assert code == 0 and "trusted already" in out, out


def test_a_database_from_before_named_checks_is_upgraded_and_gets_their_tables(tmp_path):
    step = store_module.added_in("checkpasses")  # the format that brought the named checks' tables
    assert step == store_module.added_in("checktrust") and step > 5
    db = sqlite3.connect(str(tmp_path / "praxis.db"))
    for script in (store_module.SCHEMA, *(store_module.UPGRADES[n] for n in range(2, step))):
        for statement in store_module._statements(script):
            db.execute(statement)
    db.execute(f"PRAGMA user_version = {step - 1}")
    db.commit()
    db.close()
    store = Store(tmp_path)
    try:
        assert store.row("PRAGMA user_version")[0] == store_module.FORMAT
        assert store.has("checkpasses") and store.has("checktrust")
        assert store.rows("SELECT * FROM checkpasses") == [] and checkaudit.broken(store) == []
    finally:
        store.close()
