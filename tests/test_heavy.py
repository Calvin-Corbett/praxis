"""`praxis run --heavy`: the machine-wide queue for heavy jobs."""

from __future__ import annotations

import sys
import time
import uuid

from conftest import praxis, praxis_process

from agent_praxis import heavy
from agent_praxis.landing_queue import _try_lock, _unlock
from agent_praxis.store import Store


def _set_limit(project, number: int) -> None:
    toml = project / "praxis.toml"
    toml.write_text(
        "\n".join(
            f"heavy_jobs = {number}" if line.startswith("heavy_jobs") else line
            for line in toml.read_text().splitlines()
        )
        + "\n"
    )


def _py(code: str) -> list[str]:
    return [sys.executable, "-c", code]


def test_a_heavy_job_runs_and_its_exit_code_comes_back(project):
    code, out = praxis("run", "--heavy", *_py("print('hello heavy')"))
    assert code == 0 and "Running the heavy job now" in out, out
    code, out = praxis("run", "--heavy", "--", *_py("import sys; sys.exit(7)"))
    assert code == 7 and "exit code 7" in out, out
    logs = sorted((project / ".git" / "praxis" / "logs").glob("*-heavy-*.log"))
    assert len(logs) == 2 and any("hello heavy" in log.read_text() for log in logs)


def test_the_job_gets_the_agent_environment_and_the_queue_is_empty_afterwards(project, monkeypatch, tmp_path):
    monkeypatch.setenv("PRAXIS_AGENT", "heavy-1")
    seen = tmp_path / "seen.txt"
    praxis("run", "--heavy", *_py(f"import os; open(r'{seen}', 'w').write(os.environ['PRAXIS_AGENT'])"))
    assert seen.read_text() == "heavy-1"
    store = Store(project / ".git" / "praxis")
    try:
        assert store.rows("SELECT * FROM heavy") == []
    finally:
        store.close()


def test_a_command_that_cannot_start_returns_127(project):
    code, out = praxis("run", "--heavy", "definitely-not-a-program-xyz")
    assert code != 0, out  # run through the shell (one word): the shell reports it
    code, out = praxis("run", "--heavy", "definitely-not-a-program-xyz", "arg")
    assert code == 127 and "could not start" in out, out


def test_nothing_to_run_is_refused(project):
    code, out = praxis("run", "--heavy")
    assert code == 1 and "Give the command" in out, out


def test_run_needs_the_heavy_flag(project):
    proc = praxis_process(project, "run", "echo", "hi")
    out = proc.communicate(timeout=60)[0]
    assert proc.returncode == 2 and "--heavy" in out, out


def test_heavy_jobs_must_be_a_positive_whole_number(project):
    _set_limit(project, 0)
    code, out = praxis("status")
    assert code == 1 and "heavy_jobs must be 1 or more" in out, out


def test_a_third_job_waits_while_two_run_and_says_so(project, tmp_path):
    _set_limit(project, 2)
    stop = tmp_path / "stop"
    sleeper = _py(f"import os, time\nwhile not os.path.exists(r'{stop}'): time.sleep(0.1)")
    first = praxis_process(project, "run", "--heavy", *sleeper)
    second = praxis_process(project, "run", "--heavy", *sleeper)
    third = None
    try:
        store = Store(project / ".git" / "praxis")
        try:
            deadline = time.time() + 60
            while time.time() < deadline and len(heavy.rows(store)) < 2:
                time.sleep(0.2)
            assert len([r for r in heavy.rows(store) if r["started"]]) == 2
            third = praxis_process(project, "run", "--heavy", *_py("print('third ran')"))
            deadline = time.time() + 60
            while time.time() < deadline and len(heavy.rows(store)) < 3:
                time.sleep(0.2)
            rows = heavy.rows(store)
            assert [bool(r["started"]) for r in rows] == [True, True, False]
            _code, status = praxis("status")
            assert "HEAVY JOBS" in status and status.count("running:") == 2 and "waiting:" in status, status
            assert third.poll() is None  # still waiting in line
        finally:
            store.close()
        stop.write_text("go")
        outs = [p.communicate(timeout=120)[0] for p in (first, second, third)]
        assert [p.returncode for p in (first, second, third)] == [0, 0, 0], outs
        assert "Waiting in line for a heavy job place" in outs[2], outs
        assert "Waiting in line" not in outs[0] and "Waiting in line" not in outs[1]
    finally:
        stop.write_text("go")
        for p in (first, second, third):
            if p is not None and p.poll() is None:
                p.wait(timeout=60)


def test_waiting_jobs_start_in_order(project, tmp_path):
    _set_limit(project, 1)
    store = Store(project / ".git" / "praxis")
    token = uuid.uuid4().hex
    (store.dir / "heavy").mkdir(exist_ok=True)
    holder = open(store.dir / "heavy" / f"{token}.lock", "a+b")  # noqa: SIM115 - held open on purpose: the lock lives as long as the handle  # a live runner holding the only place
    assert _try_lock(holder)
    with store.write() as db:
        db.execute(
            "INSERT INTO heavy (agent, token, command, enqueued, started) VALUES ('other', ?, 'x', ?, ?)",
            (token, time.time(), time.time()),
        )
    order = tmp_path / "order.txt"
    procs = []
    try:
        for name in ("a", "b"):
            procs.append(praxis_process(project, "run", "--heavy", *_py(f"open(r'{order}', 'a').write('{name}')")))
            deadline = time.time() + 60
            while time.time() < deadline and len(heavy.rows(store)) < 2 + len(procs) - 1:
                time.sleep(0.2)
        time.sleep(1)
        assert not order.exists() and all(p.poll() is None for p in procs)
        _unlock(holder)  # the holder ends
        holder.close()
        for p in procs:
            assert p.communicate(timeout=120)[0] is not None
        assert order.read_text() == "ab"
    finally:
        for p in procs:
            if p.poll() is None:
                p.wait(timeout=60)
        store.close()


def test_a_crashed_job_does_not_hold_its_place(project):
    """A runner that died leaves a started row and no live lock: the next job takes over the place."""
    _set_limit(project, 1)
    store = Store(project / ".git" / "praxis")
    try:
        with store.write() as db:
            db.execute(
                "INSERT INTO heavy (agent, token, command, enqueued, started, pid) "
                "VALUES ('ghost', 'deadbeef', 'pytest', ?, ?, 999999)",
                (time.time(), time.time()),
            )
            db.execute(
                "INSERT INTO heavy (agent, token, command, enqueued) VALUES ('ghost2', 'deadbeef2', 'x', ?)",
                (time.time(),),
            )
        assert heavy.rows(store) == []  # status does not list them
    finally:
        store.close()
    code, out = praxis("run", "--heavy", *_py("print('took the place')"))
    assert code == 0 and "Waiting in line" not in out, out
    store = Store(project / ".git" / "praxis")
    try:
        assert store.rows("SELECT * FROM heavy") == []  # the dead rows were cleared too
    finally:
        store.close()


def test_a_runner_that_dies_while_running_is_noticed(project, tmp_path):
    """A live runner holds a place; once its lock is released (its process ended) the waiting job goes."""
    _set_limit(project, 1)
    store = Store(project / ".git" / "praxis")
    token = uuid.uuid4().hex
    (store.dir / "heavy").mkdir(exist_ok=True)
    holder = open(store.dir / "heavy" / f"{token}.lock", "a+b")  # noqa: SIM115 - held open on purpose: the lock lives as long as the handle
    assert _try_lock(holder)
    with store.write() as db:
        db.execute(
            "INSERT INTO heavy (agent, token, command, enqueued, started) VALUES ('crashy', ?, 'x', ?, ?)",
            (token, time.time(), time.time()),
        )
    proc = praxis_process(project, "run", "--heavy", *_py("print('after the crash')"))
    try:
        deadline = time.time() + 60
        while time.time() < deadline and len(heavy.rows(store)) < 2:
            time.sleep(0.2)
        time.sleep(1)
        assert proc.poll() is None, "it must wait while the other runner is alive"
        _unlock(holder)
        holder.close()  # the crashed runner's lock is freed by the operating system
        out = proc.communicate(timeout=120)[0]
        assert proc.returncode == 0 and "Waiting in line" in out and "after the crash" not in out.split("Waiting")[0]
    finally:
        if proc.poll() is None:
            proc.wait(timeout=60)
        store.close()


def test_status_lists_running_and_waiting_jobs_with_agent_and_age(project):
    store = Store(project / ".git" / "praxis")
    handles = []
    try:
        (store.dir / "heavy").mkdir(exist_ok=True)
        for agent, started in (("alice", time.time() - 7200), ("bob", None)):
            token = uuid.uuid4().hex
            handle = open(store.dir / "heavy" / f"{token}.lock", "a+b")  # noqa: SIM115 - held open on purpose: the lock lives as long as the handle
            assert _try_lock(handle)
            handles.append(handle)
            with store.write() as db:
                db.execute(
                    "INSERT INTO heavy (agent, token, command, enqueued, started) VALUES (?, ?, 'pytest -q', ?, ?)",
                    (agent, token, time.time() - 7300, started),
                )
        code, out = praxis("status")
        assert code == 0 and "HEAVY JOBS (at most 2 run at once" in out, out
        assert "running: alice, started 2 hours ago: pytest -q" in out, out
        assert "waiting: bob, joined 2 hours ago: pytest -q" in out, out
    finally:
        for handle in handles:
            _unlock(handle)
            handle.close()
        store.close()
