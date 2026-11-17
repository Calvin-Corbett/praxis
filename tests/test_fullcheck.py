"""The full check: `praxis fullcheck`, the run after every N landings, and finding the landing that broke it."""

from __future__ import annotations

import shlex
import sqlite3
import subprocess
import sys
import time
from pathlib import Path

import pytest
from conftest import check_command, edit, folder, git, praxis, praxis_process

from agent_praxis import fullcheck, heavy
from agent_praxis import store as store_module
from agent_praxis.config import Config, parse_toml, render
from agent_praxis.landing_queue import Ticket
from agent_praxis.store import Store

# A short fake full check. It notes where it ran and on which commit, optionally waits for a gate file, and fails when
# a script says SLOW_BREAK: a break the quick check (which only looks for the word BROKEN) does not see.
FULL_CHECK = """\
import os, pathlib, sys, time
log, gate = sys.argv[1], (sys.argv[2] if len(sys.argv) > 2 else None)
with open(log, "a") as f:
    f.write(os.getcwd() + "|" + os.environ.get("PRAXIS_COMMIT", "") + "\\n")
if gate:
    for _ in range(1200):
        if os.path.exists(gate):
            break
        time.sleep(0.05)
if os.path.exists(log + ".down"):
    print("the test database is down")
    sys.exit(3)
bad = sorted(str(p) for p in pathlib.Path("scripts").glob("*.gd") if "SLOW_BREAK" in p.read_text())
for name in bad:
    print("ERROR: the whole suite fails on", name)
sys.exit(1 if bad else 0)
"""


def set_lines(project: Path, **values: str) -> None:
    """Replace `name = ...` lines of praxis.toml (each value is written as given)."""
    lines = (project / "praxis.toml").read_text().splitlines()
    for name, value in values.items():
        assert any(line.startswith(f"{name} =") for line in lines), name
        lines = [f"{name} = {value}" if line.startswith(f"{name} =") else line for line in lines]
    (project / "praxis.toml").write_text("\n".join(lines) + "\n")


def use_full_check(project: Path, tmp_path: Path, every: int = 0, gate: Path | None = None) -> Path:
    """Set full_check to the fake one; returns the file it adds a line to on every run."""
    script, log = tmp_path / "full_check.py", tmp_path / "full-runs.log"
    script.write_text(FULL_CHECK)
    command = f'"{sys.executable}" "{script}" "{log}"' + (f' "{gate}"' if gate else "")
    set_lines(project, full_check=f"'{command}'", full_check_every=str(every))
    return log


def ran(log: Path) -> list[tuple[str, str]]:
    """(the folder, the commit) of every run of the fake full check so far."""
    if not log.exists():
        return []
    return [tuple(line.rsplit("|", 1)) for line in log.read_text().splitlines()]


def rows(project: Path) -> list[dict]:
    store = Store(project / ".git" / "praxis")
    try:
        return [dict(r) for r in store.rows("SELECT * FROM fullchecks ORDER BY id")]
    finally:
        store.close()


def is_running(project: Path) -> bool:
    store = Store(project / ".git" / "praxis")
    try:
        return fullcheck.running(store)
    finally:
        store.close()


def wait_for(condition, what: str, seconds: float = 120):
    end = time.time() + seconds
    while time.time() < end:
        found = condition()
        if found:
            return found
        time.sleep(0.1)
    raise AssertionError(f"timed out waiting for {what}")


def wait_finished(project: Path, count: int) -> list[dict]:
    """Wait until *count* runs are recorded, none is unfinished and no full check process is left."""

    def done():
        found = rows(project)
        ok = len(found) >= count and all(r["result"] for r in found) and not is_running(project)
        return found if ok else None

    return wait_for(done, f"{count} finished full check run(s)")


def land(project: Path, name: str, text: str = "extends Node\n") -> str:
    """Start an agent, give it one new script and land it (the quick check passes). Returns what landing printed."""
    code, out = praxis("start", name, f"work of {name}", f"scripts/{name}.gd")
    assert code == 0, out
    edit(folder(project, name) / "scripts" / f"{name}.gd", text)
    code, out = praxis("land", name)
    assert code == 0, out
    return out


def landed_commit(project: Path, name: str) -> str:
    store = Store(project / ".git" / "praxis")
    try:
        return store.row("SELECT commit_id FROM landings WHERE agent = ? AND ok = 1", (name,))["commit_id"]
    finally:
        store.close()


@pytest.fixture(autouse=True)
def _no_full_check_left_running(tmp_path):
    yield
    project = tmp_path / "my game"
    if (project / ".git" / "praxis" / "praxis.db").exists():
        wait_for(lambda: not is_running(project), "the background full check to end")


# --- the settings ----------------------------------------------------------------------------------------------------


def test_the_settings_are_written_and_read_back():
    parsed = parse_toml(render(Config(full_check="npm run test:all", full_check_every=5)))
    assert parsed["full_check"] == "npm run test:all" and parsed["full_check_every"] == 5
    parsed = parse_toml(render(Config()))
    assert parsed["full_check"] == "" and parsed["full_check_every"] == 0


def test_every_n_without_a_full_check_is_refused(project):
    set_lines(project, full_check_every="3")
    code, out = praxis("status")
    assert code == 1 and "full_check_every is 3, but full_check is empty" in out, out
    set_lines(project, full_check_every="-1")
    code, out = praxis("status")
    assert code == 1 and "full_check_every must be 0" in out, out


def test_fullcheck_without_the_setting_says_how_to_set_it(project):
    code, out = praxis("fullcheck")
    assert code == 1 and "No full check is set" in out and "full_check =" in out, out
    assert "FULL CHECK" not in praxis("status")[1]


def test_a_format_4_database_is_upgraded_and_gets_the_full_check_table(tmp_path):
    db = sqlite3.connect(str(tmp_path / "praxis.db"))
    for script in (store_module.SCHEMA, *(store_module.UPGRADES[n] for n in (2, 3, 4))):
        for statement in store_module._statements(script):
            db.execute(statement)
    db.execute("PRAGMA user_version = 4")
    db.commit()
    db.close()
    store = Store(tmp_path)
    try:
        assert store.format == store.level == store_module.FORMAT
        assert store.rows("SELECT * FROM fullchecks") == []
    finally:
        store.close()


# --- praxis fullcheck ------------------------------------------------------------------------------------------------


def test_it_runs_on_main_in_a_scratch_folder_and_never_in_the_main_folder(project, tmp_path):
    log = use_full_check(project, tmp_path)
    before = git(project, "status", "--porcelain")
    tip = git(project, "rev-parse", "main")
    code, out = praxis("fullcheck")
    assert code == 0 and "The full check passed on main" in out, out
    scratch = folder(project, "_fullcheck")
    ((where, commit),) = ran(log)
    assert Path(where).resolve() == scratch.resolve() and commit == tip
    assert git(scratch, "rev-parse", "HEAD") == tip and git(scratch, "branch", "--show-current") == ""
    assert git(project, "status", "--porcelain") == before and git(project, "rev-parse", "main") == tip
    (row,) = rows(project)
    assert row["result"] == "passed" and row["commit_id"] == tip and Path(row["log"]).parent.name == "logs"
    status = praxis("status")[1]
    assert "FULL CHECK (only when you ask" in status and "last passed just now" in status
    assert "Nothing needs you right now." in status


def test_a_failure_names_the_landing_that_broke_it_and_how_to_undo_it(project, tmp_path):
    log = use_full_check(project, tmp_path)
    assert praxis("fullcheck")[0] == 0  # the last pass the search starts from
    land(project, "ann")
    land(project, "bob", "extends Node\n# SLOW_BREAK\n")  # the quick check does not see this
    land(project, "cat")
    land(project, "dan")
    tip, culprit = git(project, "rev-parse", "main"), landed_commit(project, "bob")
    code, out = praxis("fullcheck")
    assert code == 2, out
    assert (
        "The full check is FAILING on main" in out
        and f'bob\'s landing "work of bob" broke it (commit {culprit})' in out
    )
    assert f"  git revert --no-edit {culprit}" in out and "never undoes a landing by itself" in out
    # git bisect over four landings: the newest fails, then bob's (fails), then ann's (passes) = 3 runs
    assert [c for _w, c in ran(log)][1:] == [tip, culprit, landed_commit(project, "ann")]
    assert git(project, "rev-parse", "main") == tip, "nothing was undone"
    failed = rows(project)[-1]
    assert failed["result"] == "failed" and failed["culprit_agent"] == "bob" and failed["culprit_commit"] == culprit
    assert "ERROR: the whole suite fails on" in Path(failed["note"]).read_text()

    status = praxis("status")[1]
    needs = status.split("WORKING NOW")[0]
    assert "NEEDS YOU" in needs and 'bob\'s landing "work of bob" broke it' in needs
    assert failed["note"] in needs and f"git revert --no-edit {culprit}" in needs
    assert "FAILING since just now" in status

    # still failing: one more run of the newest version, and what the search found out is not run again
    code, out = praxis("fullcheck")
    assert code == 2 and "bob's landing" in out and "already failed there" in out, out
    assert len(ran(log)) == 5

    # the printed command is exact: run it as given, in the main folder, and the full check passes again
    undo = next(line.strip() for line in out.splitlines() if line.strip().startswith("git revert"))
    proc = subprocess.run(shlex.split(undo), check=False, cwd=str(project), capture_output=True, text=True)
    assert proc.returncode == 0, proc.stderr
    code, out = praxis("fullcheck")
    assert code == 0, out
    status = praxis("status")[1]
    assert "FAILING" not in status and "Nothing needs you right now." in status


def test_without_an_earlier_pass_it_says_it_cannot_tell_which_landing(project, tmp_path):
    use_full_check(project, tmp_path)
    land(project, "bob", "extends Node\n# SLOW_BREAK\n")
    code, out = praxis("fullcheck")
    assert code == 2 and "has not passed on this project before" in out and "git revert" not in out, out
    assert "has not passed on this project before" in praxis("status")[1].split("WORKING NOW")[0]


def test_exit_code_3_is_not_a_verdict_on_the_landings(project, tmp_path):
    log = use_full_check(project, tmp_path)
    Path(str(log) + ".down").write_text("")
    code, out = praxis("fullcheck")
    assert code == 2 and "could not judge main" in out and "exit code 3" in out, out
    assert "could not judge main" in praxis("status")[1].split("WORKING NOW")[0]
    Path(str(log) + ".down").unlink()
    assert praxis("fullcheck")[0] == 0
    assert "Nothing needs you right now." in praxis("status")[1]


def test_fullcheck_background_returns_at_once(project, tmp_path):
    gate = tmp_path / "gate"
    log = use_full_check(project, tmp_path, gate=gate)
    code, out = praxis("fullcheck", "--background")
    assert code == 0 and "Started the full check in the background" in out, out
    wait_for(lambda: ran(log), "the background full check to start its command")
    assert rows(project)[0]["result"] is None and is_running(project)
    code, out = praxis("fullcheck", "--background")
    assert code == 0 and "already running" in out, out
    asked = praxis_process(
        project, "fullcheck"
    )  # asked for while one runs: it follows that run, and runs nothing twice
    first = asked.stdout.readline()  # it says so before it waits
    assert "A full check is already running" in first, first
    gate.write_text("")
    out = asked.communicate(timeout=120)[0]
    assert asked.returncode == 0 and "The full check passed on main" in out, out
    (row,) = wait_finished(project, 1)
    assert row["result"] == "passed" and len(ran(log)) == 1


# --- after landings --------------------------------------------------------------------------------------------------


def test_every_n_landings_starts_it_in_the_background_as_a_heavy_job(project, tmp_path):
    gate = tmp_path / "gate"
    log = use_full_check(project, tmp_path, every=2, gate=gate)
    out = land(project, "ann")
    assert "full check" not in out and rows(project) == []
    out = land(project, "bob")  # the second landing: it is due, and the landing does not wait for it
    assert "Started the full check in the background: 2 landing(s) since the last one" in out, out
    wait_for(lambda: ran(log), "the background full check to start its command")
    (row,) = rows(project)
    assert row["result"] is None and row["commit_id"] == git(project, "rev-parse", "main")
    store = Store(project / ".git" / "praxis")
    try:
        jobs = heavy.rows(store)
    finally:
        store.close()
    assert [j["agent"] for j in jobs] == ["(full check)"] and jobs[0]["started"]
    status = praxis("status")[1]
    assert "running now on commit" in status and "running: (full check)" in status
    out = land(project, "cat")  # a landing while it runs: nothing is held up, and no second one starts
    assert "full check" not in out
    gate.write_text("")
    (row,) = wait_finished(project, 1)
    assert row["result"] == "passed" and "full_check_every = 2" in row["why"]
    assert Path(ran(log)[0][0]).name == "_fullcheck"
    logs = list((project / ".git" / "praxis" / "logs").glob("full-check-background-*.log"))
    assert len(logs) == 1 and "The full check passed on main" in logs[0].read_text()
    out = land(project, "dan")  # cat and dan: two landings since the run that covered ann and bob
    assert "Started the full check in the background" in out, out
    assert [r["result"] for r in wait_finished(project, 2)] == ["passed", "passed"]


def test_a_landing_whose_own_check_is_the_full_check_counts_as_a_pass(project, tmp_path):
    log = use_full_check(project, tmp_path, every=1)
    command = parse_toml((project / "praxis.toml").read_text())["full_check"]
    set_lines(project, check=f"'{command}'")
    out = land(project, "ann")
    assert "Started the full check" not in out, out
    (row,) = rows(project)
    assert row["result"] == "passed" and row["why"] == "a landing's own check"
    assert row["commit_id"] == git(project, "rev-parse", "main")
    assert len(ran(log)) == 1 and Path(ran(log)[0][0]).name == "ann", "only the landing's own check ran"
    set_lines(project, check=f"'{check_command()}'")  # back to a quick check: now the landing is not a pass itself
    out = land(project, "bob")
    assert "Started the full check in the background: 1 landing(s) since the last one" in out, out
    assert [r["result"] for r in wait_finished(project, 2)] == ["passed", "passed"]


def test_a_batch_that_breaks_the_full_check_is_searched_agent_by_agent(project, tmp_path):
    use_full_check(project, tmp_path, every=2)
    assert praxis("fullcheck")[0] == 0
    for name, text in (("ann", "extends Node\n"), ("bob", "extends Node\n# SLOW_BREAK\n")):
        assert praxis("start", name, f"work of {name}", f"scripts/{name}.gd")[0] == 0
        edit(folder(project, name) / "scripts" / f"{name}.gd", text)
    store = Store(project / ".git" / "praxis")
    ghost = Ticket(store, "ghost", say=lambda _t: None)  # holds the front of the line so both wait, then land together
    ghost.__enter__()
    procs = []
    for name in ("ann", "bob"):
        procs.append(praxis_process(project, "land", name))
        wait_for(lambda n=name: store.rows("SELECT 1 FROM queue WHERE agent = ?", (n,)), f"{name} in the line")
    ghost.__exit__(None, None, None)
    store.close()
    outs = [p.communicate(timeout=180)[0] for p in procs]
    assert [p.returncode for p in procs] == [0, 0], outs
    assert "together" in outs[0] and "Started the full check in the background" in outs[0], outs[0]
    failed = wait_finished(project, 2)[-1]
    assert failed["result"] == "failed" and failed["culprit_agent"] == "bob"
    assert failed["culprit_commit"] == landed_commit(project, "bob") != landed_commit(project, "ann")
    assert 'bob\'s landing "work of bob" broke it' in praxis("status")[1].split("WORKING NOW")[0]


def test_an_interrupted_full_check_is_noticed_and_restarted_by_the_next_landing(project, tmp_path):
    gate = tmp_path / "gate"
    log = use_full_check(project, tmp_path, gate=gate)  # full_check_every = 0: even then a cut-short run is restarted
    proc = praxis_process(project, "fullcheck")
    wait_for(lambda: ran(log), "the full check to start its command")
    proc.kill()  # the process this test started, by its own handle
    proc.communicate(timeout=60)
    wait_for(lambda: not is_running(project), "the lock of the killed full check to be free")
    gate.write_text("")  # lets a command that outlived the killed runner end, and the next run pass at once
    assert rows(project)[0]["result"] is None
    assert "the last run was interrupted" in praxis("status")[1]
    out = land(project, "ann")
    assert "Started the full check in the background: the full check before it was interrupted" in out, out
    first, second = wait_finished(project, 2)
    assert first["result"] == "interrupted" and second["result"] == "passed"
    assert second["commit_id"] == git(project, "rev-parse", "main")
    out = land(project, "bob")  # it finished: with full_check_every = 0 nothing more starts by itself
    assert "full check" not in out
