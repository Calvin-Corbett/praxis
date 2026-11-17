"""A newer Praxis never takes a breaking step in a project while commands of an older Praxis are still running in it.

(The steps that only add to the database are taken at once: see test_formats.py.) The older commands here are fakes: the files a real one keeps (its line in the register, its landing-line ticket, its
heavy-job lock file), with their locks held by the test for as long as the "command" is meant to be running.
"""

from __future__ import annotations

import json
import os
import re
import sqlite3
import subprocess
import sys
import time
import types
import uuid
from pathlib import Path

import pytest
from conftest import edit, folder, git, praxis, praxis_process

from agent_praxis import cli, config, gitops, landing_batch, live, store
from agent_praxis.agents import Ctx
from agent_praxis.landing_queue import (
    ALONE,
    PROTOCOL,
    Ticket,
    clear_note,
    free_note,
    read_note,
    ticket_protocol,
    write_note,
)
from agent_praxis.store import Store

# The format of "the Praxis before this one" where it matters: the one before the newest breaking step.
BEHIND = max(step for step, kind in store.KIND.items() if kind == store.BREAKING) - 1
# The tables that step and the ones after it bring: what the database of that Praxis does not have.
NEWEST = {
    table
    for step in range(BEHIND + 1, store.FORMAT + 1)
    for table in re.findall(r"CREATE TABLE IF NOT EXISTS (\w+)", store.UPGRADES[step])
}


def state(project: Path) -> Path:
    return project / ".git" / "praxis"


def numbers(project: Path) -> tuple[int, int]:
    """(the database's format, its user_version: the oldest format whose Praxis can use it)."""
    conn = sqlite3.connect(state(project) / "praxis.db")
    try:
        return store.formats(conn)
    finally:
        conn.close()


def db_format(project: Path) -> int:
    return numbers(project)[0]


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


def _pid_of_a_process_that_ended() -> int:
    done = subprocess.Popen([sys.executable, "-c", "pass"])
    done.wait()
    return done.pid


def test_the_file_a_killed_command_left_is_removed_as_soon_as_it_is_seen(project):
    procs = state(project) / "procs"
    let_go(older_registered(project, pid=_pid_of_a_process_that_ended()))  # killed: its lock is free, its file stays
    left = next(iter(procs.glob("*.lock")))
    assert left.exists()
    assert live.running(state(project)) == [] and not left.exists(), "no waiting for the file to grow old"


def test_status_removes_the_file_a_killed_command_left(project):
    procs = state(project) / "procs"
    let_go(older_registered(project, pid=_pid_of_a_process_that_ended()))
    assert len(list(procs.glob("*.lock"))) == 1
    code, out = praxis("status")
    assert code == 0 and "OLDER PRAXIS" not in out, out
    assert list(procs.glob("*.lock")) == []


def test_the_file_of_a_command_that_is_just_starting_is_left_alone(project):
    """A starting command has made its file and not locked it yet: it must find it again."""
    procs = state(project) / "procs"
    procs.mkdir(parents=True, exist_ok=True)
    young = procs / f"{os.getpid()}-{uuid.uuid4().hex[:12]}.lock"
    starting = open(young, "a+b")  # noqa: SIM115 - what register() has done before it takes the lock
    try:
        assert live.running(state(project)) == [] and young.exists()
    finally:
        starting.close()
    if os.name != "nt":  # the process is alive and the file is not locked: only its age can say it was left behind
        long_ago = time.time() - 10 * live.SWEEP_AFTER_SECONDS
        os.utime(young, (long_ago, long_ago))
    assert live.running(state(project)) == [] and not young.exists()


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
        assert "in a way they cannot use. It will take over when they finish. Nothing was changed." in out
        assert "--upgrade-now" in out
        assert db_format(alice) == BEHIND
        assert git(alice, "log", "--format=%s", "main").count("alice") == 0, "nothing landed"
    finally:
        let_go(ghost)
    code, out = praxis("status")  # the older command has finished: this Praxis takes over
    assert code == 0 and "OLDER PRAXIS" not in out, out
    assert db_format(alice) == store.FORMAT and tables(alice) >= NEWEST


def test_status_shows_no_section_whose_table_the_older_database_lacks(alice):
    """Every section of the status page that reads a table of the newest format is left out, not an error."""
    toml = alice / "praxis.toml"
    lines = [line for line in toml.read_text().splitlines() if not line.startswith(("full_check =", "heavy_jobs ="))]
    lines += ["full_check = 'echo full'", 'heavy_jobs = "auto"']
    toml.write_text("\n".join(lines) + "\n")
    downgrade(alice, 2)
    ghost = older_registered(alice, fmt=2)
    try:
        code, out = praxis("status")
        assert code == 0 and "OLDER PRAXIS COMMANDS ARE STILL RUNNING" in out and "alice" in out, out
        assert "FULL CHECK" not in out and "HEAVY JOBS" not in out and "CHECK CACHE" not in out
        assert db_format(alice) == 2
    finally:
        let_go(ghost)
    code, out = praxis("status")
    assert code == 0 and "FULL CHECK" in out and "HEAVY JOBS" in out and db_format(alice) == store.FORMAT, out


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
def test_upgrade_now_takes_over_under_older_heavy_jobs(alice, args):
    downgrade(alice)
    ghost = older_registered(alice, "praxis run --heavy pytest -q")
    old = older_heavy_job(alice, "bob", "npm test", pid=777)
    try:
        code, out = praxis(*args)
        assert code == 0, out
        assert "Took this project over (--upgrade-now)" in out and "praxis run --heavy pytest -q" in out
        assert "praxis run --heavy npm test" in out and "heavy jobs finish normally" in out
        assert db_format(alice) == store.FORMAT and tables(alice) >= NEWEST
    finally:
        let_go(ghost)
        let_go(old)


def _older_lander(project: Path, kind: str):
    """A landing of an older Praxis: one that registered, or one from before the register (its ticket is all)."""
    if kind == "registered":
        return older_registered(project, "praxis land ghost", pid=4242), "praxis land ghost (pid 4242, Praxis 0.3.0)"
    return older_landing(project, "bob")[0], "praxis land bob (an older Praxis, pid not known)"


@pytest.mark.parametrize("kind", ["registered", "from before the register"])
@pytest.mark.parametrize("args", [("status", "--upgrade-now"), ("--upgrade-now", "land", "alice")])
def test_upgrade_now_refuses_while_an_older_landing_is_running(alice, kind, args):
    downgrade(alice)
    ghost, named = _older_lander(alice, kind)
    heavy = older_heavy_job(alice, "carol", "pytest -q", pid=777)
    try:
        code, out = praxis(*args)
        assert code == 1, out
        assert "Did not take this project over (--upgrade-now)" in out and named in out
        assert "stop them and land them again with this Praxis afterwards" in out and "Nothing was changed." in out
        assert "--even-with-older-landings" in out
        assert "praxis run --heavy pytest -q" not in out, "the heavy job is not what stops it"
        assert db_format(alice) == BEHIND and not NEWEST & tables(alice)
        assert git(alice, "log", "--format=%s", "main").count("alice") == 0, "nothing landed"
        left = [live.read_stamp(path)["pid"] for path in (state(alice) / "procs").glob("*.lock")]
        assert left == ([4242] if kind == "registered" else []), "the refused command left the register clean"
    finally:
        let_go(ghost)
        let_go(heavy)


def test_upgrade_now_refuses_under_any_older_command_that_is_not_a_heavy_job(alice):
    downgrade(alice)
    ghost = older_registered(alice, "praxis fullcheck --in-background", pid=31)
    try:
        code, out = praxis("status", "--upgrade-now")
        assert code == 1 and "Other commands of an older Praxis are still running" in out, out
        assert "praxis fullcheck --in-background (pid 31" in out and "wait for them to end" in out
        assert db_format(alice) == BEHIND
    finally:
        let_go(ghost)


@pytest.mark.parametrize("args", [("status", "--upgrade-now", "--even-with-older-landings"),
                                  ("--even-with-older-landings", "--upgrade-now", "status")])
def test_upgrade_now_takes_over_under_an_older_landing_when_told_twice(alice, args):
    downgrade(alice)
    ghost, named = _older_lander(alice, "registered")
    try:
        code, out = praxis(*args)
        assert code == 0, out
        assert "Took this project over (--upgrade-now)" in out and named in out
        assert db_format(alice) == store.FORMAT and tables(alice) >= NEWEST
    finally:
        let_go(ghost)


def test_the_second_flag_alone_takes_nothing_over(alice):
    downgrade(alice)
    ghost, _named = _older_lander(alice, "registered")
    try:
        code, out = praxis("status", "--even-with-older-landings")
        assert code == 0 and "OLDER PRAXIS COMMANDS ARE STILL RUNNING" in out, out
        assert db_format(alice) == BEHIND
    finally:
        let_go(ghost)


def test_status_keeps_naming_older_commands_after_the_takeover(alice):
    """Taking over does not end the older commands: status says they are there until the last one has ended."""
    downgrade(alice)
    ghost = older_registered(alice, "praxis run --heavy pytest -q", pid=4242)
    old = older_heavy_job(alice, "bob", "npm test", pid=777)
    try:
        code, out = praxis("status", "--upgrade-now")
        assert code == 0 and db_format(alice) == store.FORMAT, out
        for _again in range(2):
            code, out = praxis("status")
            assert code == 0, out
            assert "OLDER PRAXIS COMMANDS ARE STILL RUNNING HERE" in out
            assert "praxis run --heavy pytest -q (pid 4242, Praxis 0.3.0)" in out
            assert "praxis run --heavy npm test (an older Praxis, by bob, its job is pid 777)" in out
            assert "changed in a way their Praxis cannot use" in out and "ends with its own exit code" in out
            assert f"only Praxis from format {BEHIND + 1} on can use it now" in out
            assert "It takes over when they finish" not in out, "it has taken over already"
    finally:
        let_go(ghost)
        let_go(old)
    code, out = praxis("status")
    assert code == 0 and "OLDER PRAXIS" not in out, out


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


def test_the_batch_head_takes_only_followers_of_its_own_praxis_version(project):
    for name in ("alice", "bob", "carol", "dave", "erin", "frank"):
        _saved_work(project, name)
    opened = Store(state(project))
    held = [
        older_landing(project, "bob"),  # from before the number
        older_landing(project, "carol", pid=1, format=store.FORMAT, version="x", protocol=PROTOCOL - 1),
        older_landing(project, "dave", pid=2, format=store.FORMAT, version="x", protocol=PROTOCOL),
        older_landing(project, "erin", pid=3, format=store.FORMAT + 1, version="y", protocol=PROTOCOL + 1),  # a newer one
    ]
    try:
        ctx = _ctx(project, opened)
        head = types.SimpleNamespace(id=0)
        assert [m.name for m in landing_batch._followers(ctx, opened.agent("alice"), head)] == ["dave"], (
            "an older lander and a newer one each land on their own turn"
        )
        frank = older_landing(project, "frank", pid=4, format=store.FORMAT, version="x", protocol=PROTOCOL)
        held.append(frank)
        write_note(opened, frank[1], dict(ALONE))  # what a real ticket of this Praxis starts with
        names = [m.name for m in landing_batch._followers(ctx, opened.agent("alice"), head)]
        assert names == ["dave", "frank"], "the note a ticket starts with does not keep its own version's head away"
        write_note(opened, frank[1], {"members": ["zed", "frank"]})
        names = [m.name for m in landing_batch._followers(ctx, opened.agent("alice"), head)]
        assert names == ["dave"], "a ticket another batch has taken is left alone"
    finally:
        for handle, _token in held:
            let_go(handle)
        opened.close()


def test_a_ticket_starts_with_a_note_that_keeps_an_older_head_away_and_gets_it_back(project):
    """A head from before PROTOCOL 2 takes no ticket that has a note. So every ticket of this Praxis has one from
    its first moment, and has it again whenever a batch of its own version lets it go."""
    opened = Store(state(project))
    try:
        with Ticket(opened, "alice", say=lambda _t: None) as ticket:
            assert read_note(opened, ticket.token) == ALONE
            write_note(opened, ticket.token, {"members": ["zed", "alice"]})  # a head of this Praxis took it ...
            free_note(opened, ticket.token)  # ... and left it out again
            assert read_note(opened, ticket.token) == ALONE
            token = ticket.token
        assert read_note(opened, token) is None, "nothing is left behind when the landing ends"
        handle, old = older_landing(project, "bob", pid=1, format=6, version="x", protocol=1)
        write_note(opened, old, {"members": ["zed", "bob"]})
        free_note(opened, old)
        assert read_note(opened, old) is None, "an older lander gets no note it does not expect"
        let_go(handle)
    finally:
        opened.close()


OLDER_FOLLOWERS = """\
import json, sys, types
from agent_praxis import cli, landing_batch
ctx, store = cli._ctx()
try:
    head = types.SimpleNamespace(id=0, token="the head's own")
    print(json.dumps([m.name for m in landing_batch._followers(ctx, store.agent(sys.argv[1]), head)]))
finally:
    store.close()
"""


def followers_an_older_praxis_would_take(python: Path | str, project: Path, head: str) -> list[str]:
    """Ask a really installed older Praxis (its own Python) which waiting landings its batch would take behind
    *head*: its own `_followers`, run in *project*."""
    env = {name: value for name, value in os.environ.items() if name != "PYTHONPATH"}  # its code, not this one's
    done = subprocess.run(
        [str(python), "-c", OLDER_FOLLOWERS, head], check=False, cwd=str(project), env=env, capture_output=True, text=True
    )
    assert done.returncode == 0, done.stdout + done.stderr
    return json.loads(done.stdout.strip().splitlines()[-1])


def an_older_head_leaves_this_praxis_alone(python: Path | str, project: Path) -> None:
    """With a landing of this Praxis (bob) and one of an older Praxis (carol) waiting behind alice: the older Praxis
    takes only carol into a batch. Without bob's note it would have taken bob too."""
    for name in ("alice", "bob", "carol"):
        _saved_work(project, name)
    # An older Praxis refuses a praxis.toml with a setting it does not know, and `praxis init` of this one writes
    # every setting there is. A project the older Praxis set up has none of the newer ones: only these are needed.
    (project / "praxis.toml").write_text('main_branch = "main"\ncheck = ""\n', encoding="utf-8")
    opened = Store(state(project))
    carol = None
    try:
        with Ticket(opened, "bob", say=lambda _t: None) as bob:
            carol, _token = older_landing(project, "carol", pid=1, format=4, version="x", protocol=1)
            assert followers_an_older_praxis_would_take(python, project, "alice") == ["carol"]
            clear_note(opened, bob.token)
            assert followers_an_older_praxis_would_take(python, project, "alice") == ["bob", "carol"], (
                "the note is what keeps it away"
            )
    finally:
        if carol is not None:
            let_go(carol)
        opened.close()


def installed_praxis(commit: str) -> Path:
    """The Python of an older Praxis installed on this machine in ~/.praxis-<commit>, as these tests' author has."""
    home = Path.home() / f".praxis-{commit}"
    return home / "Scripts" / "python.exe" if os.name == "nt" else home / "bin" / "python"


@pytest.mark.parametrize(("commit", "its_format"), [("ab8e599", 4), ("b7ddce6", 6)])
def test_a_really_installed_older_praxis_does_not_take_this_praxis_into_its_batch(project, commit, its_format):
    python = installed_praxis(commit)
    if not python.is_file():
        pytest.skip(f"no older Praxis is installed at {python.parent.parent}")
    code, out = praxis("status")
    assert code == 0, out
    if its_format < store.USABLE_FROM:  # that Praxis only works in a project it (or one like it) started
        downgrade(project, its_format)
        code, out = praxis("status")
        assert code == 0 and numbers(project) == (store.FORMAT, its_format), out
    an_older_head_leaves_this_praxis_alone(python, project)


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
