"""The two numbers a project's database keeps: its format, and the oldest format whose Praxis can still use it.

A step that only adds to the database is taken at once, whoever is running, and leaves every older Praxis working: the
user_version, the one number a Praxis from before the `meta` table reads, stays where it is. Only a breaking step
raises it, and only that step waits for the commands of an older Praxis to finish.

The older commands are the fakes of test_safeswitch.py: the files a real one keeps, locked by the test.
"""

from __future__ import annotations

import re
import sqlite3
import sys
from pathlib import Path

import pytest
from conftest import edit, folder, git, praxis
from test_safeswitch import (
    downgrade,
    let_go,
    numbers,
    older_heavy_job,
    older_registered,
    state,
    tables,
)

from agent_praxis import store
from agent_praxis.store import Store

BEFORE_META = store.META - 1


@pytest.fixture
def alice(project) -> Path:
    code, out = praxis("start", "alice", "work of alice", "scripts/alice.gd")
    assert code == 0, out
    edit(folder(project, "alice") / "scripts" / "alice.gd", "extends Node\n")
    return project


def _sql(project: Path, *statements: str) -> None:
    conn = sqlite3.connect(state(project) / "praxis.db")
    for statement in statements:
        conn.execute(statement)
    conn.commit()
    conn.close()


def _a_step_later(monkeypatch, kind: str, table: str) -> int:
    """Make this Praxis one that knows one step more, of *kind*, bringing *table*. Returns the step's number."""
    step = store.FORMAT + 1
    monkeypatch.setitem(store.UPGRADES, step, f"CREATE TABLE IF NOT EXISTS {table} (x INTEGER);")
    monkeypatch.setitem(store.KIND, step, kind)
    monkeypatch.setattr(store, "FORMAT", step)
    return step


# --- the table of steps -------------------------------------------------------------------------------------------------


def test_every_step_says_whether_it_is_additive_or_breaking():
    assert set(store.KIND) == set(store.UPGRADES) == set(range(2, store.FORMAT + 1))
    assert set(store.KIND.values()) <= {store.ADDITIVE, store.BREAKING}
    assert store.KIND[5] == store.KIND[6] == store.ADDITIVE
    assert store.USABLE_FROM >= max(step for step, kind in store.KIND.items() if kind == store.BREAKING)


def test_a_step_called_additive_only_adds_what_older_code_can_ignore():
    """A new table, or a new column an INSERT that does not name it still fills: nothing else is additive."""
    table = re.compile(r"CREATE TABLE IF NOT EXISTS \w+ \(.*\)", re.DOTALL)
    column = re.compile(r"ALTER TABLE \w+ ADD COLUMN \w+ \w+(?P<rules>.*)", re.DOTALL)
    for step, kind in store.KIND.items():
        if kind != store.ADDITIVE:
            continue
        for statement in store._statements(store.UPGRADES[step]):
            added = column.fullmatch(statement)
            if added:
                rules = added["rules"].upper()
                assert "NOT NULL" not in rules or "DEFAULT" in rules, (step, statement)
                assert "PRIMARY KEY" not in rules and "UNIQUE" not in rules, (step, statement)
            else:
                assert table.fullmatch(statement), f"step {step} is called additive, but does: {statement}"


def test_user_version_stands_for_usable_from():
    assert [store.usable_from(n) for n in range(7)] == [0, 1, 2, 3, 4, 5, 6]
    assert store.usable_from(7) == 6, "7 was an additive step written into user_version before the two numbers"
    assert store.usable_from(store.FORMAT) == 6


# --- what a database says ---------------------------------------------------------------------------------------------


def test_a_brand_new_database_says_both_numbers(project):
    code, out = praxis("status")  # the first command that opens the project makes its database
    assert code == 0, out
    assert numbers(project) == (store.FORMAT, store.USABLE_FROM)
    assert "meta" in tables(project)
    opened = Store(state(project))
    try:
        assert (opened.format, opened.level, opened.usable_from) == (store.FORMAT, store.FORMAT, store.USABLE_FROM)
        assert not opened.held_back and opened.older == []
    finally:
        opened.close()


@pytest.mark.parametrize("found", [4, 5, 6])
def test_an_older_database_gets_what_is_new_at_once_and_older_praxis_can_still_use_it(alice, found):
    """Older commands are running: a landing, and a heavy job of a Praxis from before the register. Nothing waits for
    them, and the number their Praxis reads stays what it was."""
    downgrade(alice, found)
    assert numbers(alice) == (found, found) and "meta" not in tables(alice)
    lander = older_registered(alice, "praxis land ghost", fmt=found, pid=4242)
    job = older_heavy_job(alice, "bob", "pytest -q", pid=777)
    try:
        code, out = praxis("land", "alice")
        assert code == 0 and "Landed: alice's work" in out, out
        assert "Older Praxis commands are still running" not in out and "Took this project over" not in out
        assert numbers(alice) == (store.FORMAT, found), "everything new is there, and user_version did not move"
        assert tables(alice) >= {"fullchecks", "checkpasses", "checktrust", "meta"}
        assert (alice / "scripts" / "alice.gd").exists()
    finally:
        let_go(lander)
        let_go(job)
    code, out = praxis("status")
    assert code == 0 and numbers(alice) == (store.FORMAT, found), "and it stays there when they have finished"


def test_status_names_older_versions_at_work_and_says_they_carry_on(alice):
    downgrade(alice, 6)
    lander = older_registered(alice, "praxis land ghost", fmt=6, pid=4242)
    job = older_registered(alice, "praxis run --heavy pytest -q", fmt=6, pid=777)
    try:
        for _again in range(2):
            code, out = praxis("status")
            assert code == 0, out
            assert "OLDER PRAXIS VERSIONS ARE AT WORK HERE: " in out
            assert "praxis land ghost (pid 4242, Praxis 0.3.0)" in out
            assert "praxis run --heavy pytest -q (pid 777, Praxis 0.3.0)" in out
            assert "They carry on as usual, and so does every new command of those versions" in out
            assert f"(format {store.FORMAT}) can be used by every Praxis from format 6 on" in out
            assert "never in one batch with a landing of this Praxis" in out
            assert "OLDER PRAXIS COMMANDS ARE STILL RUNNING" not in out, "nothing waits and nothing was taken over"
    finally:
        let_go(lander)
        let_go(job)
    code, out = praxis("status")
    assert code == 0 and "OLDER PRAXIS" not in out, out


def test_a_praxis_from_before_the_register_is_older_than_a_database_from_format_6_on(alice):
    """Its heavy job is still named, but under the commands that can no longer use the project."""
    job = older_heavy_job(alice, "bob", "pytest -q", pid=777)
    try:
        code, out = praxis("status")
        assert code == 0 and "OLDER PRAXIS COMMANDS ARE STILL RUNNING HERE: praxis run --heavy pytest -q" in out, out
        assert "runs to its end and ends with its own exit code" in out
    finally:
        let_go(job)


@pytest.mark.parametrize("with_meta", [False, True])
def test_a_user_version_of_7_is_put_back_to_6(alice, with_meta):
    """Format 7 (two more tables) was written into user_version by Praxis's own development, never by a release.
    It is an additive step: the database is usable from 6, and says so again."""
    if with_meta:  # this Praxis has been here; then that development Praxis came back and wrote its 7 again
        _sql(alice, "PRAGMA user_version = 7")
        assert numbers(alice) == (store.FORMAT, 7)
    else:
        downgrade(alice, 7)
        assert numbers(alice) == (7, 7) and {"checkpasses", "checktrust"} <= tables(alice)
    running = older_registered(alice, "praxis land ghost", fmt=7, pid=4242)
    try:
        code, out = praxis("status")
        assert code == 0 and "alice" in out, out
        assert numbers(alice) == (store.FORMAT, 6)
    finally:
        let_go(running)


# --- a database a newer Praxis has added to ---------------------------------------------------------------------------


def test_a_praxis_that_knows_less_works_in_a_database_a_newer_one_added_to(alice):
    later = store.FORMAT + 3
    _sql(
        alice,
        "CREATE TABLE from_a_newer_praxis (id INTEGER PRIMARY KEY, what TEXT)",
        "ALTER TABLE heavy ADD COLUMN shiny INTEGER NOT NULL DEFAULT 0",
        "ALTER TABLE queue ADD COLUMN lane TEXT",
        f"UPDATE meta SET value = '{later}' WHERE key = 'format'",
    )
    assert numbers(alice) == (later, store.USABLE_FROM)
    opened = Store(state(alice))
    try:
        assert (opened.format, opened.level) == (store.FORMAT, later), "it uses what it knows"
        assert opened.has("meta") and not opened.held_back
    finally:
        opened.close()
    for args in (
        ("status",),
        ("claim", "alice", "scripts/player.gd"),
        ("run", "--heavy", sys.executable, "-c", "pass"),
        ("land", "alice"),
        ("start", "carol", "work of carol", "scripts/carol.gd"),
        ("done", "carol"),
    ):
        code, out = praxis(*args)
        assert code == 0, (args, out)
        assert "newer Praxis" not in out and "OLDER PRAXIS" not in out, (args, out)
    assert numbers(alice) == (later, store.USABLE_FROM), "and it leaves the newer Praxis's numbers alone"
    assert "from_a_newer_praxis" in tables(alice) and (alice / "scripts" / "alice.gd").exists()


def test_a_database_only_a_newer_praxis_can_use_is_refused_and_left_alone(alice):
    beyond = store.FORMAT + 1
    _sql(alice, f"UPDATE meta SET value = '{beyond + 2}' WHERE key = 'format'", f"PRAGMA user_version = {beyond}")
    for args in (("status",), ("land", "alice"), ("status", "--upgrade-now", "--even-with-older-landings")):
        code, out = praxis(*args)
        assert code == 1 and f"newer Praxis (format {beyond})" in out and "Nothing was changed." in out, (args, out)
    assert numbers(alice) == (beyond + 2, beyond)
    assert git(alice, "log", "--format=%s", "main").count("alice") == 0, "nothing landed"


# --- a breaking step ---------------------------------------------------------------------------------------------------


def test_a_breaking_step_waits_for_older_commands_and_raises_usable_from(alice, monkeypatch):
    now = store.FORMAT
    lander = older_registered(alice, "praxis land ghost", fmt=now, pid=4242)
    try:
        with monkeypatch.context() as newer:
            step = _a_step_later(newer, store.BREAKING, "all_changed")
            code, out = praxis("land", "alice")
            assert code == 1 and "Older Praxis commands are still running in this project (praxis land ghost" in out
            assert "in a way they cannot use" in out and "Nothing was changed." in out, out
            code, out = praxis("status")
            assert code == 0 and "OLDER PRAXIS COMMANDS ARE STILL RUNNING HERE: praxis land ghost" in out, out
            code, out = praxis("claim", "alice", "scripts/player.gd")
            assert code == 0, out
            code, out = praxis("status", "--upgrade-now")
            assert code == 1 and "Did not take this project over (--upgrade-now)" in out, out
            assert numbers(alice) == (now, store.USABLE_FROM) and "all_changed" not in tables(alice)
    finally:
        let_go(lander)
    with monkeypatch.context() as newer:
        step = _a_step_later(newer, store.BREAKING, "all_changed")
        code, out = praxis("land", "alice")  # the older command has finished: the step is taken
        assert code == 0 and "Landed: alice's work" in out, out
        assert numbers(alice) == (step, step) and "all_changed" in tables(alice)
    code, out = praxis("status")  # this Praxis is now the older one, and the database is not for it any more
    assert code == 1 and f"newer Praxis (format {step})" in out, out


def test_the_additive_steps_before_a_breaking_one_do_not_wait_for_it(alice, monkeypatch):
    now = store.FORMAT
    running = older_registered(alice, "praxis run --heavy pytest", fmt=now, pid=4242)
    try:
        added = _a_step_later(monkeypatch, store.ADDITIVE, "something_more")
        _a_step_later(monkeypatch, store.BREAKING, "all_changed")
        code, out = praxis("status")
        assert code == 0 and "OLDER PRAXIS COMMANDS ARE STILL RUNNING HERE" in out, out
        assert numbers(alice) == (added, store.USABLE_FROM)
        assert "something_more" in tables(alice) and "all_changed" not in tables(alice)
        opened = Store(state(alice))
        try:
            assert opened.held_back and opened.format == added and opened.has("something_more")
        finally:
            opened.close()
    finally:
        let_go(running)


def test_a_command_of_this_format_does_not_hold_a_breaking_step_back(alice, monkeypatch):
    step = _a_step_later(monkeypatch, store.BREAKING, "all_changed")
    same = older_registered(alice, "praxis land carol", fmt=step, pid=4242)
    try:
        code, out = praxis("status")
        assert code == 0 and "OLDER PRAXIS" not in out, out
        assert numbers(alice) == (step, step)
    finally:
        let_go(same)
