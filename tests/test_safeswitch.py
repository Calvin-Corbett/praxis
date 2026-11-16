"""A newer Praxis never takes a project over while commands of an older Praxis are still running in it.

The older commands here are fakes: the files a real one keeps (its line in the register, its landing-line ticket, its
heavy-job lock file), with their locks held by the test for as long as the "command" is meant to be running.
"""

from __future__ import annotations

import os
import re
import sqlite3
import sys
import time
import types
import uuid
from pathlib import Path

import pytest
from conftest import edit, folder, git, praxis, praxis_process

from agent_praxis import cli, config, gitops, landing_batch, live, store
from agent_praxis.agents import Ctx
from agent_praxis.landing_queue import PROTOCOL, Ticket, read_note, ticket_protocol
from agent_praxis.store import Store

BEHIND = store.FORMAT - 1  # the format of "the Praxis before this one"
# The tables the newest format brings: what the database of the Praxis before this one does not have.
NEWEST = set(re.findall(r"CREATE TABLE IF NOT EXISTS (\w+)", store.UPGRADES[store.FORMAT]))


def state(project: Path) -> Path:
    return project / ".git" / "praxis"


def db_format(project: Path) -> int:
    conn = sqlite3.connect(state(project) / "praxis.db")
    try:
        return int(conn.execute("PRAGMA user_version").fetchone()[0])
    finally:
        conn.close()


def tables(project: Path) -> set[str]:
    conn = sqlite3.connect(state(project) / "praxis.db")
    try:
        return {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
    finally:
        conn.close()


def downgrade(project: Path, fmt: int = BEHIND) -> None:
    """Make the project's database what a Praxis that knows only format *fmt* left behind."""
    conn = sqlite3.connect(state(project) / "praxis.db")
    for number in range(store.FORMAT, fmt, -1):
        for table in re.findall(r"CREATE TABLE IF NOT EXISTS (\w+)", store.UPGRADES[number]):
            conn.execute(f"DROP TABLE {table}")
    conn.execute(f"PRAGMA user_version = {fmt}")
    conn.commit()
    conn.close()


def _locked(path: Path, **facts):
    path.parent.mkdir(parents=True, exist_ok=True)
    handle = open(path, "a+b")  # noqa: SIM115 - held open on purpose: the lock lives as long as the handle
    if facts:
        live.stamp(handle, **facts)
    assert live.try_lock(handle)
    return handle


def let_go(handle) -> None:
    live.unlock(handle)
    handle.close()


def older_registered(project: Path, command: str = "praxis land ghost", fmt: int = BEHIND, pid: int = 4242):
    """A running command of a Praxis that has the register but knows an older database format."""
    return _locked(
        state(project) / "procs" / f"{pid}-{uuid.uuid4().hex[:12]}.lock",
        pid=pid,
        format=fmt,
        version="0.3.0",
        command=command,
        started=time.time(),
    )


def _insert(project: Path, sql: str, args: tuple) -> None:
    conn = sqlite3.connect(state(project) / "praxis.db")
    conn.execute(sql, args)
    conn.commit()
    conn.close()


def older_landing(project: Path, agent: str, **facts):
    """A `praxis land` waiting in line. With no *facts* its ticket file is empty: a Praxis from before the register."""
    token = uuid.uuid4().hex
    handle = _locked(state(project) / "queue" / f"{token}.lock", **facts)
    _insert(project, "INSERT INTO queue (agent, token, enqueued) VALUES (?, ?, ?)", (agent, token, time.time()))
    return handle, token


def older_heavy_job(project: Path, agent: str, command: str, pid: int):
    token = uuid.uuid4().hex
    handle = _locked(state(project) / "heavy" / f"{token}.lock")
    _insert(
        project,
        "INSERT INTO heavy (agent, token, command, enqueued, started, pid) VALUES (?, ?, ?, ?, ?, ?)",
        (agent, token, command, time.time(), time.time(), pid),
    )
    return handle


@pytest.fixture
def alice(project) -> Path:
    code, out = praxis("start", "alice", "work of alice", "scripts/alice.gd")
    assert code == 0, out
    edit(folder(project, "alice") / "scripts" / "alice.gd", "extends Node\n")
    return project


# --- the register ---------------------------------------------------------------------------------------------------


def test_an_open_store_is_in_the_register_and_leaves_nothing_behind(project):
    opened = Store(state(project), command="praxis try this")
    try:
        files = list((state(project) / "procs").glob("*.lock"))
        assert len(files) == 1 and live.held(files[0])
        facts = live.read_stamp(files[0])
        assert facts["pid"] == os.getpid() and facts["format"] == store.FORMAT
        assert facts["command"] == "praxis try this" and isinstance(facts["version"], str)
        assert [f["command"] for f in live.running(state(project))] == ["praxis try this"]
    finally:
        opened.close()
    assert list((state(project) / "procs").glob("*.lock")) == []


def test_a_real_command_registers_what_it_is_doing(project, tmp_path):
    gate = tmp_path / "gate"
    job = f"import os, time\nwhile not os.path.exists(r'{gate}'): time.sleep(0.05)"
    proc = praxis_process(project, "run", "--heavy", sys.executable, "-c", job)
    try:
        end = time.time() + 120
        seen = heavy_files = []
        while time.time() < end and not (seen and heavy_files):
            seen = [f for f in live.running(state(project)) if f["command"].startswith("praxis run --heavy")]
            heavy_files = list((state(project) / "heavy").glob("*.lock"))
            time.sleep(0.1)
        assert seen and seen[0]["format"] == store.FORMAT and isinstance(seen[0]["pid"], int)
        assert len(heavy_files) == 1, "the job's file is there"
        assert live.read_stamp(heavy_files[0])["pid"] == seen[0]["pid"], "and says whose it is"
    finally:
        gate.write_text("go")
        out = proc.communicate(timeout=120)[0]
    assert proc.returncode == 0, out
    assert live.running(state(project)) == []


def test_files_of_killed_commands_do_not_pile_up(project, monkeypatch):
    procs = state(project) / "procs"
    long_ago = time.time() - 10 * live.SWEEP_AFTER_SECONDS
    for n in range(5):
        let_go(older_registered(project, pid=1000 + n))
    for left in procs.glob("*.lock"):
        os.utime(left, (long_ago, long_ago))
    monkeypatch.setattr(live, "CROWDED", 3)
    code, out = praxis("status")
    assert code == 0, out
    assert list(procs.glob("*.lock")) == []


def test_a_dead_process_is_not_running_and_its_file_is_swept_later(project):
    procs = state(project) / "procs"
    dead = older_registered(project)
    let_go(dead)  # the process died: the operating system freed its lock, the file is still there
    left = next(iter(procs.glob("*.lock")))
    assert live.running(state(project)) == [] and left.exists(), "a young file may belong to a command just starting"
    long_ago = time.time() - 10 * live.SWEEP_AFTER_SECONDS
    os.utime(left, (long_ago, long_ago))
    assert live.running(state(project)) == [] and not left.exists()


# --- no upgrade under live older commands ---------------------------------------------------------------------------


def test_a_registered_older_command_keeps_the_database_as_it_is(alice):
    downgrade(alice)
    ghost = older_registered(alice, "praxis land ghost", pid=4242)
    try:
        code, out = praxis("status")
        assert code == 0, out
        assert "OLDER PRAXIS COMMANDS ARE STILL RUNNING" in out and "praxis land ghost (pid 4242, Praxis 0.3.0)" in out
        assert "alice" in out, "status still answers from the database as it is"
        assert db_format(alice) == BEHIND and not NEWEST & tables(alice)

        code, out = praxis("land", "alice")
        assert code == 1, out
        assert "Older Praxis commands are still running in this project (praxis land ghost (pid 4242" in out
        assert "This newer Praxis will take over when they finish. Nothing was changed." in out
        assert "--upgrade-now" in out
        assert db_format(alice) == BEHIND
        assert git(alice, "log", "--format=%s", "main").count("alice") == 0, "nothing landed"
    finally:
        let_go(ghost)
    code, out = praxis("status")  # the older command has finished: this Praxis takes over
    assert code == 0 and "OLDER PRAXIS" not in out, out
    assert db_format(alice) == store.FORMAT and tables(alice) >= NEWEST


def test_an_older_landing_waiting_in_line_keeps_the_database_as_it_is(alice):
    """A Praxis from before the register: all it keeps is its locked, empty ticket file."""
    downgrade(alice)
    ghost, _token = older_landing(alice, "bob")
    try:
        code, out = praxis("land", "alice")
        assert code == 1 and "praxis land bob (an older Praxis, pid not known)" in out, out
        assert "Nothing was changed." in out and db_format(alice) == BEHIND
        code, out = praxis("status")
        assert code == 0 and "praxis land bob" in out and db_format(alice) == BEHIND, out
    finally:
        let_go(ghost)
    code, out = praxis("status")
    assert code == 0 and db_format(alice) == store.FORMAT, out


def test_an_older_heavy_job_keeps_the_database_as_it_is(alice):
    downgrade(alice)
    ghost = older_heavy_job(alice, "bob", "pytest -q", pid=777)
    try:
        code, out = praxis("land", "alice")
        assert code == 1, out
        assert "praxis run --heavy pytest -q (an older Praxis, by bob, its job is pid 777)" in out
        assert db_format(alice) == BEHIND
    finally:
        let_go(ghost)
    code, out = praxis("status")
    assert code == 0 and db_format(alice) == store.FORMAT, out


@pytest.mark.parametrize("args", [("status", "--upgrade-now"), ("--upgrade-now", "status")])
def test_upgrade_now_takes_over_anyway(alice, args):
    downgrade(alice)
    ghost = older_registered(alice)
    try:
        code, out = praxis(*args)
        assert code == 0, out
        assert "Took this project over (--upgrade-now)" in out and "praxis land ghost" in out
        assert db_format(alice) == store.FORMAT and tables(alice) >= NEWEST
    finally:
        let_go(ghost)


def test_commands_that_only_use_the_older_tables_carry_on_without_upgrading(alice):
    downgrade(alice, 3)  # no check cache yet: only landing uses it
    ghost = older_registered(alice, fmt=3)
    try:
        for args in (
            ("claim", "alice", "scripts/player.gd"),
            ("start", "carol", "work of carol", "scripts/carol.gd"),
            ("run", "--heavy", sys.executable, "-c", "pass"),
            ("blocked", "alice", "a question"),
            ("resume", "alice"),
            ("done", "carol"),
        ):
            code, out = praxis(*args)
            assert code == 0, (args, out)
            assert db_format(alice) == 3, args
        assert "checkcache" not in tables(alice)
    finally:
        let_go(ghost)


def test_a_command_whose_table_is_missing_waits_and_the_rest_carry_on(alice):
    downgrade(alice, 2)  # from before the heavy jobs
    ghost = older_registered(alice, fmt=2)
    try:
        code, out = praxis("run", "--heavy", sys.executable, "-c", "pass")
        assert code == 1 and "Older Praxis commands are still running" in out and "Nothing was changed." in out, out
        code, out = praxis("status")
        assert code == 0 and "HEAVY JOBS" not in out and "alice" in out, out
        code, out = praxis("claim", "alice", "scripts/enemy.gd")
        assert code == 0, out
        assert db_format(alice) == 2 and "heavy" not in tables(alice)
    finally:
        let_go(ghost)


def test_a_command_let_through_by_mistake_says_why_it_stopped(alice, monkeypatch):
    downgrade(alice, 1)
    monkeypatch.setitem(cli.USES, "wait", ())  # wrong on purpose: wait reads the background table (format 2)
    ghost = older_registered(alice, fmt=1)
    try:
        code, out = praxis("wait", "alice")
        assert code == 1, out
        assert "Older Praxis commands are still running" in out and "run it again when they have finished" in out
        assert db_format(alice) == 1
    finally:
        let_go(ghost)


def test_dead_older_commands_hold_nothing_back(alice):
    downgrade(alice)
    let_go(older_registered(alice))
    let_go(older_landing(alice, "bob")[0])
    let_go(older_heavy_job(alice, "bob", "pytest", pid=777))
    code, out = praxis("status")
    assert code == 0 and "OLDER PRAXIS" not in out, out
    assert db_format(alice) == store.FORMAT


def test_commands_of_this_praxis_hold_nothing_back(alice):
    downgrade(alice)
    same = older_registered(alice, "praxis land carol", fmt=store.FORMAT)
    mine, _token = older_landing(alice, "carol", pid=4242, format=store.FORMAT, version="9.9.9", protocol=PROTOCOL)
    try:
        code, out = praxis("status")
        assert code == 0 and "OLDER PRAXIS" not in out, out
        assert db_format(alice) == store.FORMAT
    finally:
        let_go(same)
        let_go(mine)


def test_a_lock_file_with_no_row_yet_counts_as_an_older_command_that_is_starting(alice):
    """An older `praxis land` locks its ticket file before it writes its row: in between, the file is all there is."""
    downgrade(alice)
    young = state(alice) / "queue" / f"{uuid.uuid4().hex}.lock"
    young.parent.mkdir(parents=True, exist_ok=True)
    young.write_bytes(b"")
    code, out = praxis("status")
    assert code == 0 and "just starting" in out and db_format(alice) == BEHIND, out
    long_ago = time.time() - 10 * live.STARTING_SECONDS
    os.utime(young, (long_ago, long_ago))  # a leftover, not a start
    code, out = praxis("status")
    assert code == 0 and db_format(alice) == store.FORMAT, out


def test_a_brand_new_project_is_set_up_whoever_is_running(tmp_path):
    ghost = _locked(tmp_path / "procs" / "1-ghost.lock", pid=1, format=1, version="0.1.0", command="praxis status")
    try:
        opened = Store(tmp_path)
        try:
            assert opened.format == store.FORMAT and not opened.held_back
        finally:
            opened.close()
    finally:
        let_go(ghost)


def test_an_older_praxis_still_refuses_a_newer_database_and_leaves_the_register_clean(project):
    conn = sqlite3.connect(state(project) / "praxis.db")
    conn.execute(f"PRAGMA user_version = {store.FORMAT + 1}")
    conn.commit()
    conn.close()
    code, out = praxis("status")
    assert code == 1 and "newer Praxis" in out, out
    assert list((state(project) / "procs").glob("*.lock")) == []


# --- batching never takes an older lander ---------------------------------------------------------------------------


def _ctx(project: Path, opened: Store) -> Ctx:
    repo = gitops.find_repo()
    return Ctx(repo=repo, cfg=config.load(repo.root), store=opened, say=lambda _text="": None)


def _saved_work(project: Path, name: str) -> None:
    code, out = praxis("start", name, f"work of {name}", f"scripts/{name}.gd")
    assert code == 0, out
    edit(folder(project, name) / "scripts" / f"{name}.gd", "extends Node\n")
    git(folder(project, name), "add", "-A")
    git(folder(project, name), "commit", "-q", "-m", f"work of {name}")


def test_a_ticket_says_which_protocol_its_lander_understands(project):
    opened = Store(state(project))
    try:
        with Ticket(opened, "alice", say=lambda _t: None) as ticket:
            assert ticket_protocol(opened, ticket.token) == PROTOCOL
            assert live.read_stamp(opened.dir / "queue" / f"{ticket.token}.lock")["pid"] == os.getpid()
        handle, token = older_landing(project, "bob")
        assert ticket_protocol(opened, token) == 0, "a ticket that says nothing is an older lander"
        let_go(handle)
    finally:
        opened.close()


def test_the_batch_head_takes_only_followers_that_understand_the_batch(project):
    _saved_work(project, "alice")
    _saved_work(project, "bob")
    _saved_work(project, "carol")
    _saved_work(project, "dave")
    opened = Store(state(project))
    held = [
        older_landing(project, "bob"),  # from before the number
        older_landing(project, "carol", pid=1, format=store.FORMAT, version="x", protocol=PROTOCOL - 1),
        older_landing(project, "dave", pid=2, format=store.FORMAT, version="x", protocol=PROTOCOL),
    ]
    try:
        ctx = _ctx(project, opened)
        head = types.SimpleNamespace(id=0)
        members = landing_batch._followers(ctx, opened.agent("alice"), head)
        assert [m.name for m in members] == ["dave"], "bob and carol land on their own turn"
    finally:
        for handle, _token in held:
            let_go(handle)
        opened.close()


def test_a_landing_leaves_an_older_lander_behind_it_alone(project):
    """The whole thing: alice lands with this Praxis while an older Praxis's landing of bob waits behind her."""
    _saved_work(project, "alice")
    _saved_work(project, "bob")
    opened = Store(state(project))
    ghost = Ticket(opened, "ghost", say=lambda _t: None)
    ghost.__enter__()  # holds the front of the line, so alice and bob both wait
    proc = praxis_process(project, "land", "alice")
    bob = None
    try:
        end = time.time() + 120
        while time.time() < end and not opened.rows("SELECT 1 FROM queue WHERE agent = 'alice'"):
            time.sleep(0.1)
        assert opened.rows("SELECT 1 FROM queue WHERE agent = 'alice'"), "alice never joined the line"
        bob, token = older_landing(project, "bob")
    finally:
        ghost.__exit__(None, None, None)
    try:
        out = proc.communicate(timeout=180)[0]
        assert proc.returncode == 0, out
        assert "Landed: alice's work" in out and "together" not in out, out
        assert read_note(opened, token) is None, "nothing was written for the older lander to misread"
        assert (project / "scripts" / "alice.gd").exists() and not (project / "scripts" / "bob.gd").exists()
    finally:
        if bob is not None:
            let_go(bob)
        opened.close()
