"""Early checks for the landing line (`speculate = true`): the next agent's check starts while the head's runs."""

from __future__ import annotations

import sys
import time
from pathlib import Path

import pytest
from conftest import edit, folder, git, praxis, praxis_process

from agent_praxis import checkcache, early
from agent_praxis.landing_queue import Ticket
from agent_praxis.store import Store

# A short fake check. It notes the folder it runs in when it starts and when it ends, waits until a gate file named
# after that folder (or "all") exists, and fails when a script says BROKEN.
GATED_CHECK = """\
import os, pathlib, sys, time
log, gates = sys.argv[1], sys.argv[2]
here = os.path.basename(os.getcwd())
with open(log, "a") as f:
    f.write("start|" + here + "\\n")
for _ in range(1200):
    if os.path.exists(os.path.join(gates, here)) or os.path.exists(os.path.join(gates, "all")):
        break
    time.sleep(0.05)
bad = [str(p) for p in pathlib.Path("scripts").glob("*.gd") if "BROKEN" in p.read_text()]
for name in bad:
    print("ERROR: broken script", name)
with open(log, "a") as f:
    f.write("end|" + here + "\\n")
sys.exit(1 if bad else 0)
"""


def set_lines(project: Path, **values: str) -> None:
    lines = (project / "praxis.toml").read_text().splitlines()
    for name, value in values.items():
        assert any(line.startswith(f"{name} =") for line in lines), name
        lines = [f"{name} = {value}" if line.startswith(f"{name} =") else line for line in lines]
    (project / "praxis.toml").write_text("\n".join(lines) + "\n")


def use_gated_check(project: Path, tmp_path: Path, speculate: bool = True, batch_max: int = 1) -> tuple[Path, Path]:
    """(the file the fake check writes its starts and ends to, the folder its gate files go in)."""
    script, log, gates = tmp_path / "gated_check.py", tmp_path / "check-runs.log", tmp_path / "gates"
    script.write_text(GATED_CHECK)
    gates.mkdir()
    command = f'"{sys.executable}" "{script}" "{log}" "{gates}"'
    set_lines(project, check=f"'{command}'", speculate="true" if speculate else "false", batch_max=str(batch_max))
    return log, gates


def seen(log: Path, what: str) -> list[str]:
    """The folders the fake check has started (what="start") or ended (what="end") in so far, in order."""
    if not log.exists():
        return []
    return [line.split("|", 1)[1] for line in log.read_text().splitlines() if line.startswith(what + "|")]


def new_agent(project: Path, name: str, text: str = "extends Node\n") -> None:
    code, out = praxis("start", name, f"work of {name}", f"scripts/{name}.gd")
    assert code == 0, out
    edit(folder(project, name) / "scripts" / f"{name}.gd", text)


def wait_for(condition, what: str, seconds: float = 120):
    end = time.time() + seconds
    while time.time() < end:
        found = condition()
        if found:
            return found
        time.sleep(0.1)
    raise AssertionError(f"timed out waiting for {what}")


def land_in_line(project: Path, names: list[str]):
    """Start `praxis land` for each name behind a ghost at the front of the line, so they wait in this order. Returns
    the processes and the function that lets the ghost go."""
    store = Store(project / ".git" / "praxis")
    ghost = Ticket(store, "ghost", say=lambda _t: None)
    ghost.__enter__()
    procs = []
    for name in names:
        procs.append(praxis_process(project, "land", name))
        wait_for(lambda n=name: store.rows("SELECT 1 FROM queue WHERE agent = ?", (n,)), f"{name} in the line")

    def release() -> None:
        ghost.__exit__(None, None, None)
        store.close()

    return procs, release


def early_running(project: Path) -> dict | None:
    store = Store(project / ".git" / "praxis")
    try:
        return early.active(store)
    finally:
        store.close()


def passes_remembered(project: Path) -> int:
    store = Store(project / ".git" / "praxis")
    try:
        return checkcache.count(store)
    finally:
        store.close()


def early_logs(project: Path) -> str:
    return "\n".join(p.read_text() for p in (project / ".git" / "praxis" / "logs").glob("early-check-*.log"))


@pytest.fixture(autouse=True)
def _no_early_check_left_running(tmp_path):
    yield
    (tmp_path / "gates").mkdir(exist_ok=True)
    (tmp_path / "gates" / "all").write_text("")  # whatever still waits for a gate ends now
    project = tmp_path / "my game"
    if (project / ".git" / "praxis" / "praxis.db").exists():
        wait_for(lambda: not early_running(project), "the early check to end")


def test_it_is_off_unless_you_turn_it_on(project, tmp_path):
    log, gates = use_gated_check(project, tmp_path, speculate=False)
    (gates / "all").write_text("")
    new_agent(project, "ann")
    new_agent(project, "bob")
    procs, release = land_in_line(project, ["ann", "bob"])
    release()
    outs = [p.communicate(timeout=180)[0] for p in procs]
    assert [p.returncode for p in procs] == [0, 0], outs
    assert seen(log, "start") == ["ann", "bob"] and "early" not in outs[0] + outs[1]
    assert not (project / ".git" / "praxis" / "early").exists()
    assert not folder(project, "_early").exists()


def test_the_next_agents_check_runs_beside_the_heads_and_its_pass_is_reused(project, tmp_path):
    log, gates = use_gated_check(project, tmp_path)
    new_agent(project, "ann")
    new_agent(project, "bob")
    procs, release = land_in_line(project, ["ann", "bob"])
    release()
    wait_for(lambda: sorted(seen(log, "start")) == ["_early", "ann"], "both checks to be running at once")
    state = early_running(project)
    assert state and state["agent"] == "bob"
    status = praxis("status")[1]
    assert "EARLY CHECK: bob's check is running early" in status and "bob (early check)" in status
    (gates / "all").write_text("")
    outs = [p.communicate(timeout=180)[0] for p in procs]
    assert [p.returncode for p in procs] == [0, 0], outs
    assert "Starting the next agent's check early" in outs[0] and "Landed: ann's work" in outs[0]
    assert "Skipping the check: same tree already passed" in outs[1] and "Running the check" not in outs[1], outs[1]
    assert "Landed: bob's work" in outs[1]
    assert sorted(seen(log, "start")) == ["_early", "ann"], "bob's check ran once, early, and never in bob's folder"
    subjects = git(project, "log", "--format=%s", "main").splitlines()
    assert subjects[:2] == ["work of bob (by bob)", "work of ann (by ann)"]
    assert git(project, "rev-parse", "main") == git(folder(project, "bob"), "rev-parse", "HEAD")
    assert "It passed." in early_logs(project)


def test_when_the_head_fails_the_early_check_is_stopped_and_nothing_is_remembered(project, tmp_path):
    log, gates = use_gated_check(project, tmp_path)
    new_agent(project, "ann", "BROKEN\n")
    new_agent(project, "bob")
    procs, release = land_in_line(project, ["ann", "bob"])
    release()
    wait_for(lambda: sorted(seen(log, "start")) == ["_early", "ann"], "both checks to be running at once")
    (gates / "ann").write_text("")  # only the head's check may end: it fails
    out_ann = procs[0].communicate(timeout=180)[0]
    assert procs[0].returncode == 2 and "NOT landed: the check failed" in out_ann, out_ann
    assert "Stopped the early check of the next agent" in out_ann
    wait_for(lambda: not early_running(project), "the early check's process to be gone")
    (gates / "_early").write_text("")  # had its check outlived it, it would end now and say so
    wait_for(lambda: "bob" in seen(log, "start"), "bob's own check on its own turn")
    time.sleep(1.5)
    assert "_early" not in seen(log, "end"), "the early check was stopped, not left running"
    assert passes_remembered(project) == 0
    (gates / "bob").write_text("")
    out_bob = procs[1].communicate(timeout=180)[0]
    assert procs[1].returncode == 0 and "Running the check" in out_bob and "Skipping the check" not in out_bob, out_bob
    assert not (project / "scripts" / "ann.gd").exists() and (project / "scripts" / "bob.gd").exists()


def test_no_early_check_without_a_free_heavy_job_place(project, tmp_path):
    log, gates = use_gated_check(project, tmp_path)
    set_lines(project, heavy_jobs="1")
    stop = tmp_path / "stop-heavy"
    sleeper = f"import os, time\nwhile not os.path.exists(r'{stop}'): time.sleep(0.1)"
    busy = praxis_process(project, "run", "--heavy", sys.executable, "-c", sleeper)
    try:
        store = Store(project / ".git" / "praxis")
        wait_for(lambda: store.rows("SELECT 1 FROM heavy WHERE started IS NOT NULL"), "the heavy job to run")
        store.close()
        (gates / "all").write_text("")
        new_agent(project, "ann")
        new_agent(project, "bob")
        procs, release = land_in_line(project, ["ann", "bob"])
        release()
        outs = [p.communicate(timeout=180)[0] for p in procs]
        assert [p.returncode for p in procs] == [0, 0], outs
        wait_for(lambda: not early_running(project), "the early check's process to be gone")
        assert seen(log, "start") == ["ann", "bob"], "each check ran on its own turn"
    finally:
        stop.write_text("")
        busy.communicate(timeout=60)


def test_the_agent_a_full_batch_left_behind_is_checked_early(project, tmp_path):
    log, gates = use_gated_check(project, tmp_path, batch_max=2)
    for name in ("ann", "bob", "cat"):
        new_agent(project, name)
    procs, release = land_in_line(project, ["ann", "bob", "cat"])
    release()
    wait_for(lambda: sorted(seen(log, "start")) == ["_early", "ann"], "the batch's check and cat's early check")
    assert early_running(project)["agent"] == "cat"
    (gates / "all").write_text("")
    outs = [p.communicate(timeout=180)[0] for p in procs]
    assert [p.returncode for p in procs] == [0, 0, 0], outs
    assert "ann, bob" in outs[0] and "Skipping the check" in outs[2] and "Running the check" not in outs[2], outs[2]
    assert sorted(seen(log, "start")) == ["_early", "ann"], "two checks for three agents"
    for name in ("ann", "bob", "cat"):
        assert (project / "scripts" / f"{name}.gd").exists()


def test_speculate_must_be_true_or_false(project):
    set_lines(project, speculate='"yes"')
    code, out = praxis("status")
    assert code == 1 and "speculate must be true or false" in out, out
