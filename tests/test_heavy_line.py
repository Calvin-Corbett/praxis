"""The heavy job line: its own jobs are left out of "the machine is busy", a waiting job follows praxis.toml, a
landing check's jobs go first, and one agent cannot fill the line.

The other jobs in these tests are fakes: rows in the heavy table whose lock files the test keeps locked, so Praxis
sees live jobs that never use the CPU. The load is always a fake reader.
"""

from __future__ import annotations

import re
import sqlite3
import sys
import time
import uuid
from pathlib import Path
from types import SimpleNamespace

import pytest
from conftest import edit, folder, praxis, praxis_process

from agent_praxis import config, heavy, load
from agent_praxis import store as store_module
from agent_praxis.config import Config
from agent_praxis.errors import PraxisError
from agent_praxis.landing_queue import _try_lock, _unlock
from agent_praxis.load import Load
from agent_praxis.store import Store


@pytest.fixture(autouse=True)
def _a_plain_start(monkeypatch):
    """These tests may themselves run inside a landing's check, which sets both of these."""
    monkeypatch.delenv(heavy.CHECK_ENV, raising=False)
    monkeypatch.delenv("PRAXIS_AGENT", raising=False)
    yield
    load.use_reader(None)
    load.use_tree_reader(None)


class Line:
    """Fake heavy jobs in a project's line."""

    def __init__(self, project: Path):
        self.store = Store(project / ".git" / "praxis")
        self.handles: dict[int, object] = {}
        (self.store.dir / "heavy").mkdir(exist_ok=True)

    def add(self, agent: str, command: str = "pytest -q", running: bool = False, check: bool = False, pid=None) -> int:
        token = uuid.uuid4().hex
        handle = open(self.store.dir / "heavy" / f"{token}.lock", "a+b")  # noqa: SIM115 - held open on purpose: the lock lives as long as the handle
        assert _try_lock(handle)
        with self.store.write() as db:
            row = db.execute(
                "INSERT INTO heavy (agent, token, command, enqueued, started, pid) VALUES (?, ?, ?, ?, ?, ?)",
                (agent, token, command, time.time(), time.time() if running else None, pid),
            ).lastrowid
            if check:
                db.execute("UPDATE heavy SET priority = 1 WHERE id = ?", (row,))
        self.handles[row] = handle
        return row

    def end(self, row: int) -> None:
        """The job's runner is gone: its lock is free, its row is still there (as after a crash)."""
        handle = self.handles.pop(row)
        _unlock(handle)
        handle.close()

    def ids(self, rows) -> list[int]:
        return [r["id"] for r in rows]

    def close(self) -> None:
        for row in list(self.handles):
            self.end(row)
        self.store.close()


@pytest.fixture
def line(project):
    made = Line(project)
    yield made
    made.close()


def _set(project: Path, key: str, value) -> None:
    """Set one praxis.toml line, replacing it when the file has it."""
    toml = project / "praxis.toml"
    text, new = toml.read_text(encoding="utf-8"), f"{key} = {value}"
    if re.search(rf"(?m)^{key} = ", text):
        text = re.sub(rf"(?m)^{key} = .*$", lambda _m: new, text)
    else:
        text += f"\n{new}\n"
    toml.write_text(text, encoding="utf-8")


def _py(code: str) -> list[str]:
    return [sys.executable, "-c", code]


def _ctx(line: Line, **settings) -> SimpleNamespace:
    return SimpleNamespace(cfg=Config(**settings), store=line.store, say=lambda _text="": None)


def _may_join(line: Line, agent: str, **settings) -> int:
    """Ask whether one more ordinary job of *agent* may join the line (raises PraxisError when it is refused). The
    row this leaves has no runner, so it counts for nothing afterwards: the live jobs are the ones `line.add` made."""
    with line.store.write() as db:
        return heavy._join(_ctx(line, **settings), db, agent, uuid.uuid4().hex, "pytest", priority=False)


def _job(row: int, agent: str, running: bool = False, check: bool = False) -> dict:
    return {"id": row, "agent": agent, "started": 1.0 if running else None, "priority": int(check)}


def _order(*jobs: dict) -> list[int]:
    return [j["id"] for j in heavy.in_order(list(jobs))[1]]


# 1. Auto leaves its own jobs out ------------------------------------------------------------------------------------


def test_auto_measures_each_running_job_and_takes_it_out_of_the_machines_load(line):
    line.add("alice", running=True, pid=111)
    line.add("bob", running=True, pid=222)
    line.add("carol", pid=333)  # waiting: it uses nothing yet, so it is not measured
    asked = []
    load.use_reader(lambda: Load(cpu=70, memory_free=34, cores=16))
    load.use_tree_reader(lambda pids: asked.append(sorted(pids)) or {111: 24.0, 222: 24.0})
    limit, why = heavy.limit_now(_ctx(line, heavy_jobs="auto", heavy_max=3))
    assert asked == [[111, 222]]
    assert limit == 2 and "other programs 22%, 2 heavy jobs using 48%" in why, why


def test_one_job_filling_the_cpu_no_longer_holds_the_line_at_one(line):
    """The measured case: 16 threads, one heavy job at 100 percent, 27 waiting, and the limit stuck at 1."""
    line.add("alice", running=True, pid=111)
    load.use_reader(lambda: Load(cpu=100, memory_free=34, cores=16))
    load.use_tree_reader(lambda _pids: {111: 100.0})
    limit, why = heavy.limit_now(_ctx(line, heavy_jobs="auto", heavy_max=3))
    assert limit == 2 and "other programs 0%, 1 heavy job using 100%" in why, why
    # when it really is other programs that fill the machine, a small machine goes down to 1 as before
    load.use_reader(lambda: Load(cpu=100, memory_free=34, cores=4))
    load.use_tree_reader(lambda _pids: {111: 2.0})
    limit, why = heavy.limit_now(_ctx(line, heavy_jobs="auto"))
    assert limit == 1 and "other programs 98%" in why, why


def test_a_running_job_that_cannot_be_measured_counts_as_one_core(line):
    line.add("alice", running=True)  # no process number in its row
    line.add("bob", running=True, pid=222)  # its process cannot be read
    load.use_reader(lambda: Load(cpu=30, memory_free=60, cores=4))
    load.use_tree_reader(lambda _pids: {222: None})
    limit, why = heavy.limit_now(_ctx(line, heavy_jobs="auto", heavy_max=4))
    assert "2 heavy jobs using 0% (2 not measured yet, counted as one core each)" in why, why
    assert limit == 1  # other programs 30 percent, a job 25: one fits under 75


def test_status_says_what_was_counted(project, line):
    _set(project, "heavy_jobs", '"auto"')
    _set(project, "heavy_max", 3)
    line.add("alice", running=True, pid=111)
    line.add("bob", running=True, pid=222)
    load.use_reader(lambda: Load(cpu=70, memory_free=34, cores=16))
    load.use_tree_reader(lambda _pids: {111: 24.0, 222: 24.0})
    out = praxis("status")[1]
    assert "HEAVY JOBS (at most 2 run at once right now" in out, out
    assert "auto: 2 (other programs 22%, 2 heavy jobs using 48%, memory 34% free; " in out, out
    assert "at least 2, at most 3, aiming for 75% CPU)" in out, out


def test_a_new_job_is_measurable_from_the_start_by_its_runners_own_process(line):
    import os

    ctx = _ctx(line)
    with line.store.write() as db:
        row = heavy._join(ctx, db, "alice", "sometoken", "pytest -q", priority=False)
    assert line.store.row("SELECT pid FROM heavy WHERE id = ?", (row,))["pid"] == os.getpid()


# 2. The limit is read again while waiting -----------------------------------------------------------------------------


def test_praxis_toml_is_read_again_only_when_it_changed(project, monkeypatch):
    said, loads, real = [], [], config.load
    monkeypatch.setattr(config, "load", lambda root: loads.append(root) or real(root))
    ctx = SimpleNamespace(cfg=real(project), repo=SimpleNamespace(root=project), say=said.append)
    seen = heavy._reread(ctx, None)
    assert len(loads) == 1 and said == []  # nothing differs from what the job started with
    assert heavy._reread(ctx, seen) == seen and len(loads) == 1  # the file did not change: it is not read
    _set(project, "heavy_jobs", '"auto"')
    _set(project, "heavy_max", 6)
    _set(project, "heavy_min", 3)
    newer = heavy._reread(ctx, seen)
    assert newer != seen and len(loads) == 2
    assert (ctx.cfg.heavy_jobs, ctx.cfg.heavy_max, ctx.cfg.heavy_min) == ("auto", 6, 3)
    assert 'heavy_jobs = "auto", heavy_max = 6, heavy_min = 3' in said[-1], said


def test_a_mistake_in_praxis_toml_keeps_the_limit_a_waiting_job_had(project):
    said = []
    ctx = SimpleNamespace(cfg=config.load(project), repo=SimpleNamespace(root=project), say=said.append)
    seen = heavy._reread(ctx, None)
    _set(project, "heavy_jobs", '"lots"')
    newer = heavy._reread(ctx, seen)
    assert ctx.cfg.heavy_jobs == 2 and "cannot be read" in said[-1] and "heavy_jobs must be" in said[-1], said
    assert heavy._reread(ctx, newer) == newer and len(said) == 1  # said once, not at every look
    _set(project, "heavy_jobs", 5)
    heavy._reread(ctx, newer)
    assert ctx.cfg.heavy_jobs == 5


def test_a_job_already_waiting_starts_when_praxis_toml_gives_more_places(project, line, monkeypatch):
    _set(project, "heavy_jobs", 1)
    line.add("alice", running=True)  # holds the only place for the whole test
    monkeypatch.setenv("PRAXIS_AGENT", "bob")
    proc = praxis_process(project, "run", "--heavy", *_py("print('got a place')"))
    try:
        deadline = time.time() + 60
        while time.time() < deadline and len(heavy.rows(line.store)) < 2:
            time.sleep(0.2)
        time.sleep(1)
        assert proc.poll() is None, "it must wait: the limit is 1 and alice's job runs"
        _set(project, "heavy_jobs", 2)
        out = proc.communicate(timeout=120)[0]
        assert proc.returncode == 0, out
        assert "praxis.toml changed while this job waited. It now goes by: heavy_jobs = 2" in out, out
        assert len(heavy.rows(line.store)) == 1  # alice's job never ended
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait(timeout=60)


# 3. Landing checks go first -------------------------------------------------------------------------------------------


def test_a_landing_checks_job_goes_ahead_of_the_waiting_jobs_but_behind_other_landing_checks():
    jobs = [_job(1, "alice", running=True), _job(2, "bob"), _job(3, "carol"), _job(4, "dan", check=True),
            _job(5, "erin", check=True)]  # fmt: skip
    running, waiting = heavy.in_order(jobs)
    assert [j["id"] for j in running] == [1] and [j["id"] for j in waiting] == [4, 5, 2, 3]


def test_a_landing_check_never_stops_a_running_job_and_starts_as_soon_as_a_place_is_free(line):
    running = line.add("alice", running=True)
    waiting = line.add("bob")
    check = line.add("carol", check=True)
    go, ahead = heavy._try_start(line.store, check, 1)
    assert not go and line.ids(ahead) == [running]  # it waits for the running job like everyone
    go, ahead = heavy._try_start(line.store, waiting, 1)
    assert not go and line.ids(ahead) == [running, check]  # and bob, who came first, is now behind it
    line.end(running)
    assert heavy._try_start(line.store, waiting, 1)[0] is False
    assert heavy._try_start(line.store, check, 1) == (True, [])
    assert [r["agent"] for r in heavy.rows(line.store) if r["started"]] == ["carol"]
    assert line.ids(heavy._try_start(line.store, waiting, 1)[1]) == [check]


def test_a_heavy_job_started_inside_a_landing_check_runs_before_the_jobs_already_waiting(project, line, monkeypatch):
    _set(project, "heavy_jobs", 1)
    line.add("alice")
    line.add("bob")  # two jobs wait (fakes: they never take the free place)
    monkeypatch.setenv("PRAXIS_AGENT", "carol")
    monkeypatch.setenv(heavy.CHECK_ENV, "1")
    code, out = praxis("run", "--heavy", *_py("print('checked')"))
    assert code == 0 and "Running the heavy job now" in out and "Waiting in line" not in out, out
    assert [r["agent"] for r in heavy.rows(line.store)] == ["alice", "bob"]  # still waiting, untouched


def test_a_waiting_landing_check_says_why_it_is_in_front(project, line, monkeypatch):
    _set(project, "heavy_jobs", 1)
    holder = line.add("alice", running=True)
    line.add("bob")
    monkeypatch.setenv("PRAXIS_AGENT", "carol")
    monkeypatch.setenv(heavy.CHECK_ENV, "1")
    proc = praxis_process(project, "run", "--heavy", *_py("print('checked')"))
    try:
        deadline = time.time() + 60
        while time.time() < deadline and len(heavy.rows(line.store)) < 3:
            time.sleep(0.2)
        time.sleep(1)
        assert proc.poll() is None, "a landing check's job never stops a running job"
        status = praxis("status")[1]
        listed = [ln.strip() for ln in status.splitlines() if ln.strip().startswith(("- running:", "- waiting:"))]
        assert [ln.split(",")[0] for ln in listed] == [
            "- running: alice",
            "- waiting: carol (landing check",
            "- waiting: bob",
        ], status
        line.end(holder)
        out = proc.communicate(timeout=120)[0]
        assert proc.returncode == 0, out
        assert "1 in front (alice)" in out and "This job belongs to a landing's check" in out, out
        assert [r["agent"] for r in heavy.rows(line.store)] == ["bob"]
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait(timeout=60)


def test_status_lists_landing_checks_first_and_says_so(project, line):
    line.add("alice", "pytest tests/a", running=True)
    line.add("bob", "pytest tests/b")
    line.add("carol", "pytest tests/c", check=True)
    line.add("dan", "pytest tests/d", running=True, check=True)
    line.add("alice", "pytest tests/a2")
    out = praxis("status")[1]
    listed = [ln.strip() for ln in out.splitlines() if ln.strip().startswith(("- running:", "- waiting:"))]
    assert listed == [
        "- running: dan (landing check), started just now: pytest tests/d",
        "- running: alice, started just now: pytest tests/a",
        "- waiting: carol (landing check, goes first), joined just now: pytest tests/c",
        "- waiting: bob, joined just now: pytest tests/b",
        "- waiting: alice, joined just now: pytest tests/a2",
    ], out
    assert "waiting jobs start from the top" in out


RECORD_CHECK = "import os, sys\nopen(sys.argv[1], 'a').write(os.environ.get('PRAXIS_CHECK', 'not set') + '\\n')\n"


def test_praxis_tells_the_check_command_that_it_is_a_landing_check(project, tmp_path):
    script, seen = tmp_path / "record.py", tmp_path / "seen.txt"
    script.write_text(RECORD_CHECK, encoding="utf-8")
    _set(project, "check", f'\'"{sys.executable}" "{script.as_posix()}" "{seen.as_posix()}"\'')
    praxis("start", "alice", "jump", "scripts/player.gd")
    edit(folder(project, "alice") / "scripts" / "player.gd", "extends Node\nvar speed = 12\n")
    code, out = praxis("land", "alice")
    assert code == 0, out
    assert seen.read_text().split() == ["1"]


def test_praxis_tells_the_full_check_command_too(project, tmp_path):
    script, seen = tmp_path / "record.py", tmp_path / "seen.txt"
    script.write_text(RECORD_CHECK, encoding="utf-8")
    _set(project, "full_check", f'\'"{sys.executable}" "{script.as_posix()}" "{seen.as_posix()}"\'')
    code, out = praxis("fullcheck")
    assert code == 0, out
    assert seen.read_text().split() == ["1"]


# 4. One agent cannot fill the line --------------------------------------------------------------------------------------


def test_a_job_is_refused_when_its_agent_already_has_three_waiting(project, line, monkeypatch, tmp_path):
    _set(project, "heavy_jobs", 1)
    line.add("alice", running=True)
    for part in ("a", "b", "c"):
        line.add("bob", f"pytest tests/{part}")
    ran = tmp_path / "ran.txt"
    monkeypatch.setenv("PRAXIS_AGENT", "bob")
    code, out = praxis("run", "--heavy", *_py(f"open(r'{ran}', 'w').write('ran')"))
    assert code == 1, out
    assert "bob already has 3 heavy jobs waiting in line" in out and "heavy_waiting_per_agent" in out, out
    for part in ("a", "b", "c"):
        assert f"  - pytest tests/{part} (joined just now)" in out, out
    assert "nothing was run" in out and not ran.exists()
    assert len(heavy.rows(line.store)) == 4  # nothing was added to the line
    assert len(list((line.store.dir / "heavy").glob("*.lock"))) == 4  # and no file was left behind


def test_only_waiting_jobs_count_and_only_the_agents_own(line):
    for _n in range(3):
        line.add("bob", running=True)
    line.add("bob")
    line.add("bob")
    line.add("carol")
    assert _may_join(line, "bob")  # 3 running, 2 waiting: a third may wait
    line.add("bob")
    with pytest.raises(PraxisError, match="bob already has 3 heavy jobs waiting"):
        _may_join(line, "bob")
    assert _may_join(line, "carol")  # carol has 1 waiting: bob's line is not her problem


def test_a_waiting_job_whose_runner_is_gone_does_not_count(line):
    rows = [line.add("bob") for _n in range(3)]
    with pytest.raises(PraxisError):
        _may_join(line, "bob")
    line.end(rows[0])
    assert _may_join(line, "bob")


def test_a_landing_checks_job_is_never_refused_and_does_not_count(project, line, monkeypatch):
    _set(project, "heavy_jobs", 1)
    for _n in range(3):
        line.add("bob")
    monkeypatch.setenv("PRAXIS_AGENT", "bob")
    monkeypatch.setenv(heavy.CHECK_ENV, "1")
    code, out = praxis("run", "--heavy", *_py("print('checked')"))
    assert code == 0 and "Running the heavy job now" in out, out
    for _n in range(3):
        line.add("dan", check=True)  # three of dan's landing check jobs wait
    assert _may_join(line, "dan")  # they are not his ordinary jobs: one of those may still join


def test_the_cap_can_be_changed_or_turned_off_and_jobs_without_an_agent_are_not_capped(project, line):
    for _n in range(3):
        line.add("bob")
        line.add("(nobody)")
        line.add("(full check)")
    assert _may_join(line, "bob", heavy_waiting_per_agent=0)
    assert _may_join(line, "bob", heavy_waiting_per_agent=5)
    line.add("bob")
    line.add("bob")
    with pytest.raises(PraxisError, match="bob already has 5 heavy jobs waiting.*at most 5 waiting"):
        _may_join(line, "bob", heavy_waiting_per_agent=5)
    assert _may_join(line, "(nobody)")
    assert _may_join(line, "(full check)")
    assert config.load(project).heavy_waiting_per_agent == 3
    _set(project, "heavy_waiting_per_agent", 0)
    assert config.load(project).heavy_waiting_per_agent == 0
    _set(project, "heavy_waiting_per_agent", -1)
    with pytest.raises(PraxisError, match="heavy_waiting_per_agent must be 0"):
        config.load(project)
    text = config.render(Config(heavy_waiting_per_agent=5))
    assert config.parse_toml(text)["heavy_waiting_per_agent"] == 5


def test_an_agent_with_nothing_running_goes_before_an_agent_with_a_job_running():
    jobs = [_job(1, "alice", running=True), _job(2, "alice"), _job(3, "alice"), _job(4, "bob"), _job(5, "bob"),
            _job(6, "carol")]  # fmt: skip
    # bob and carol have nothing running: their first jobs go first, by arrival. Then everyone's next job, by
    # arrival: alice already has one running, so her first waiting job takes its turn with bob's second.
    assert _order(*jobs) == [4, 6, 2, 5, 3]


def test_jobs_take_turns_by_agent_and_equal_turns_go_by_arrival():
    assert _order(_job(1, "alice"), _job(2, "alice"), _job(3, "alice"), _job(4, "bob"), _job(5, "carol")) == [
        1,
        4,
        5,
        2,
        3,
    ]
    assert _order(_job(1, "alice"), _job(2, "bob"), _job(3, "carol")) == [1, 2, 3]  # one each: first come first
    two_running = [_job(1, "alice", running=True), _job(2, "alice", running=True), _job(3, "bob", running=True)]
    assert _order(*two_running, _job(4, "alice"), _job(5, "bob"), _job(6, "bob")) == [5, 4, 6]


def test_a_landing_check_running_under_an_agents_name_does_not_push_its_own_jobs_back():
    jobs = [_job(1, "alice", running=True, check=True), _job(2, "alice"), _job(3, "bob")]
    assert _order(*jobs) == [2, 3]


def test_a_real_job_goes_ahead_of_the_waiting_jobs_of_an_agent_that_has_one_running(project, line, monkeypatch):
    _set(project, "heavy_jobs", 2)
    line.add("bob", running=True)
    line.add("bob", "pytest tests/more")
    line.add("bob", "pytest tests/even-more")
    monkeypatch.setenv("PRAXIS_AGENT", "carol")
    code, out = praxis("run", "--heavy", *_py("print('carol ran')"))
    assert code == 0 and "Running the heavy job now" in out and "Waiting in line" not in out, out
    assert [bool(r["started"]) for r in heavy.rows(line.store)] == [True, False, False]  # bob's still wait


# The database -----------------------------------------------------------------------------------------------------------


def test_a_heavy_table_without_the_new_column_still_works_and_every_job_is_an_ordinary_one(project, line, monkeypatch):
    """A database can stay at an older format for a while (commands of an older Praxis still running in the
    project): the heavy table then has no priority column. The line must work as that format allows."""
    with line.store.write() as db:
        db.execute("DROP TABLE heavy")
        for statement in store_module._statements(store_module.UPGRADES[3]):  # the table as format 3 made it
            db.execute(statement)
    _set(project, "heavy_jobs", 2)
    line.add("alice", running=True)
    line.add("alice", "pytest tests/more")
    running, waiting = heavy.in_order(heavy.rows(line.store))
    assert len(running) == 1 and len(waiting) == 1 and not heavy.is_check(waiting[0])
    monkeypatch.setenv("PRAXIS_AGENT", "bob")
    monkeypatch.setenv(heavy.CHECK_ENV, "1")  # a landing check's job: here it can only be an ordinary one
    code, out = praxis("run", "--heavy", *_py("print('ran')"))
    assert code == 0 and "Running the heavy job now" in out, out  # bob has nothing running: he goes before alice
    status = praxis("status")[1]
    assert "- running: alice, started just now" in status and "- waiting: alice, joined just now" in status, status


def test_a_format_5_database_is_upgraded_and_its_jobs_are_ordinary_jobs(tmp_path):
    db = sqlite3.connect(str(tmp_path / "praxis.db"))
    for script in (store_module.SCHEMA, *(store_module.UPGRADES[n] for n in (2, 3, 4, 5))):
        for statement in store_module._statements(script):
            db.execute(statement)
    db.execute("INSERT INTO heavy (agent, token, command, enqueued) VALUES ('alice', 'tok', 'pytest', 1)")
    db.execute("PRAGMA user_version = 5")
    db.commit()
    db.close()
    store = Store(tmp_path)
    try:
        assert store.row("PRAGMA user_version")[0] == store_module.FORMAT == 6
        assert store.row("SELECT * FROM heavy")["priority"] == 0
        with store.write() as db:  # the step is safe on a database that already has the column
            for statement in store_module._statements(store_module.UPGRADES[6]):
                store_module._apply(db, statement)
            with pytest.raises(sqlite3.OperationalError):  # any other mistake is still a mistake
                store_module._apply(db, "ALTER TABLE no_such_table ADD COLUMN priority INTEGER")
    finally:
        store.close()
