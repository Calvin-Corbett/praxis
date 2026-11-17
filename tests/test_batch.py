"""Batched landings: agents waiting in the landing line land together, with one check for all of them."""

from __future__ import annotations

import sys
import time
from pathlib import Path

from conftest import edit, folder, git, praxis, praxis_process

from agent_praxis.landing_queue import Ticket
from agent_praxis.store import Store

# A short fake check: logs that it ran, optionally waits for a gate file, and fails when a script says BROKEN.
FAKE_CHECK = """\
import os, pathlib, sys, time
log, gate = sys.argv[1], (sys.argv[2] if len(sys.argv) > 2 else None)
with open(log, "a") as f:
    f.write("run\\n")
if gate:
    for _ in range(600):
        if os.path.exists(gate):
            break
        time.sleep(0.1)
bad = [str(p) for p in pathlib.Path("scripts").glob("*.gd") if "BROKEN" in p.read_text()]
for name in bad:
    print("ERROR: broken script", name)
sys.exit(1 if bad else 0)
"""


def use_check(project: Path, tmp_path: Path, gate: Path | None = None, batch_max: int | None = None) -> Path:
    script, log = tmp_path / "fake_check.py", tmp_path / "check-runs.log"
    script.write_text(FAKE_CHECK)
    command = f'"{sys.executable}" "{script}" "{log}"' + (f' "{gate}"' if gate else "")
    lines = [
        f"check = '{command}'" if line.startswith("check =") else line
        for line in (project / "praxis.toml").read_text().splitlines()
    ]
    if batch_max is not None:
        lines = [f"batch_max = {batch_max}" if line.startswith("batch_max =") else line for line in lines]
    (project / "praxis.toml").write_text("\n".join(lines) + "\n")
    return log


def runs(log: Path) -> int:
    return len(log.read_text().splitlines()) if log.exists() else 0


def new_agent(project: Path, name: str, text: str = "extends Node\n") -> None:
    code, out = praxis("start", name, f"work of {name}", f"scripts/{name}.gd")
    assert code == 0, out
    edit(folder(project, name) / "scripts" / f"{name}.gd", text)


def queue_names(project: Path) -> list[str]:
    store = Store(project / ".git" / "praxis")
    try:
        return [r["agent"] for r in store.rows("SELECT agent FROM queue ORDER BY id")]
    finally:
        store.close()


def wait_for(condition, what: str, seconds: float = 60):
    end = time.time() + seconds
    while time.time() < end:
        found = condition()
        if found:
            return found
        time.sleep(0.1)
    raise AssertionError(f"timed out waiting for {what}")


def land_behind_a_ghost(project: Path, names: list[str]):
    """Start `praxis land` for each name while a ghost holds the front of the line, so they all wait together. Returns
    the processes and a function that lets the ghost go."""
    store = Store(project / ".git" / "praxis")
    ghost = Ticket(store, "ghost", say=lambda _t: None)
    ghost.__enter__()
    procs = []
    for name in names:
        procs.append(praxis_process(project, "land", name))
        wait_for(lambda n=name: n in queue_names(project), f"{name} in the line")

    def release() -> None:
        ghost.__exit__(None, None, None)
        store.close()

    return procs, release


def finish(procs) -> tuple[list[int], list[str]]:
    outs = [p.communicate(timeout=180)[0] for p in procs]
    return [p.returncode for p in procs], outs


def main_subjects(project: Path) -> list[str]:
    return git(project, "log", "--format=%s", "--reverse", "main").splitlines()


def read_until(proc, text: str) -> str:
    """What *proc* prints, read line by line up to and including the first line holding *text*."""
    seen = ""
    for line in proc.stdout:
        seen += line
        if text in line:
            return seen
    raise AssertionError(f"{text!r} was never printed:\n{seen}")


def test_two_agents_land_together_with_one_check(project, tmp_path):
    gate = tmp_path / "gate"
    log = use_check(project, tmp_path, gate=gate)
    new_agent(project, "alice")
    new_agent(project, "bob")
    procs, release = land_behind_a_ghost(project, ["alice", "bob"])
    release()
    # The check waits for the gate, so bob is told while the batch is still being checked.
    told = read_until(procs[1], "taken into a batch")
    gate.write_text("go")
    told += procs[1].stdout.read()
    codes, outs = finish(procs)
    outs[1] = told + outs[1]
    assert codes == [0, 0], outs
    assert runs(log) == 1, "one check for the whole batch"
    assert "together" in outs[0] and "alice, bob" in outs[0]
    assert "taken into a batch" in outs[1]
    assert "Landed: alice's work" in outs[0] and "Landed: bob's work" in outs[1]
    assert "Landed: bob" not in outs[0], "each process prints only its own result"
    subjects = main_subjects(project)
    assert [s for s in subjects if "(by " in s] == ["work of alice (by alice)", "work of bob (by bob)"]
    assert (project / "scripts" / "alice.gd").exists() and (project / "scripts" / "bob.gd").exists()
    for name in ("alice", "bob"):  # each folder is level with the main version afterwards
        assert git(folder(project, name), "rev-parse", "HEAD") == git(project, "rev-parse", "main")
        assert git(folder(project, name), "branch", "--show-current") == f"praxis/{name}"
    assert queue_names(project) == []


def test_the_culprit_is_found_by_halving_and_the_rest_land(project, tmp_path):
    log = use_check(project, tmp_path)
    new_agent(project, "alice")
    new_agent(project, "bob", "BROKEN\n")
    new_agent(project, "carol")
    procs, release = land_behind_a_ghost(project, ["alice", "bob", "carol"])
    release()
    codes, outs = finish(procs)
    assert codes == [0, 2, 0], outs
    assert "broken script" in outs[1] and "NOT landed" in outs[1]
    assert "broken script" not in outs[0] + outs[2]
    assert runs(log) == 4, "whole batch, alice alone, then bob alone and carol alone (the rest is the known failure)"
    assert (project / "scripts" / "alice.gd").exists() and (project / "scripts" / "carol.gd").exists()
    assert not (project / "scripts" / "bob.gd").exists()
    assert git(folder(project, "bob"), "log", "--format=%s", "-1") == "work of bob (by bob)", "bob's work is kept"
    landings = Store(project / ".git" / "praxis").rows("SELECT agent, ok FROM landings")
    assert sorted((r["agent"], r["ok"]) for r in landings) == [("alice", 1), ("bob", 0), ("carol", 1)]


def test_one_agent_waiting_lands_exactly_as_before(project, tmp_path):
    log = use_check(project, tmp_path)
    new_agent(project, "alice")
    code, out = praxis("land", "alice")
    assert code == 0, out
    assert runs(log) == 1
    assert "together" not in out and "batch" not in out
    assert "Landed: alice's work" in out


def test_batch_max_one_turns_batching_off(project, tmp_path):
    log = use_check(project, tmp_path, batch_max=1)
    new_agent(project, "alice")
    new_agent(project, "bob")
    procs, release = land_behind_a_ghost(project, ["alice", "bob"])
    release()
    codes, outs = finish(procs)
    assert codes == [0, 0], outs
    assert runs(log) == 2
    assert not any("together" in out for out in outs)


def test_batch_max_caps_the_batch(project, tmp_path):
    log = use_check(project, tmp_path, batch_max=2)
    for name in ("alice", "bob", "carol"):
        new_agent(project, name)
    procs, release = land_behind_a_ghost(project, ["alice", "bob", "carol"])
    release()
    codes, outs = finish(procs)
    assert codes == [0, 0, 0], outs
    assert runs(log) == 2, "alice and bob together, then carol"
    # bob is told about the batch before its result, even when the batch is over within one look at the line.
    assert "taken into a batch: alice, bob " in outs[1], outs[1]
    assert outs[1].index("taken into a batch") < outs[1].index("Landed: bob's work"), outs[1]
    assert "together with alice, bob" in outs[1] and "together" not in outs[2]


def test_a_batch_member_that_clashes_is_left_out_and_lands_on_its_own_turn(project, tmp_path):
    log = use_check(project, tmp_path)
    new_agent(project, "alice")
    praxis("start", "bob", "same file", "scripts/alice.gd")
    praxis("start", "carol", "other", "scripts/carol.gd")
    edit(
        folder(project, "bob") / "scripts" / "alice.gd", "extends Node\nvar x = 2\n"
    )  # bob and alice both add the file
    edit(folder(project, "carol") / "scripts" / "carol.gd", "extends Node\n")
    procs, release = land_behind_a_ghost(project, ["alice", "bob", "carol"])
    release()
    codes, outs = finish(procs)
    assert codes == [0, 2, 0], outs
    assert "clash" in outs[1] and "alice" in outs[1], "the refusal still names whose work it clashes with"
    assert runs(log) == 1, "alice and carol checked together; bob never reached a check"


def test_status_shows_who_is_in_a_batch(project, tmp_path):
    gate = tmp_path / "go"
    log = use_check(project, tmp_path, gate=gate)
    new_agent(project, "alice")
    new_agent(project, "bob")
    procs, release = land_behind_a_ghost(project, ["alice", "bob"])
    release()
    wait_for(lambda: runs(log) == 1, "the check to start")
    code, out = praxis("status")
    assert code == 0, out
    assert "in a batch with bob" in out and "in a batch with alice" in out, out
    gate.write_text("go")
    codes, outs = finish(procs)
    assert codes == [0, 0], outs


def test_a_head_that_dies_mid_check_loses_nobody_and_the_line_recovers(project, tmp_path):
    gate = tmp_path / "go"
    log = use_check(project, tmp_path, gate=gate)
    new_agent(project, "alice")
    new_agent(project, "bob")
    procs, release = land_behind_a_ghost(project, ["alice", "bob"])
    release()
    wait_for(lambda: runs(log) == 1, "the check to start")
    procs[0].kill()  # the head dies in the middle of the batch's check
    procs[0].communicate(timeout=30)
    assert git(project, "log", "--format=%s", "-1", "main") != "work of alice (by alice)", "nothing landed"
    assert "work of alice (by alice)" in git(project, "log", "--format=%s", "praxis/alice"), "alice's work is saved"
    gate.write_text("go")  # the check can finish now
    codes, outs = finish(procs[1:])
    assert codes == [0], outs  # bob was waiting behind the dead head: he lands on his own turn
    assert (project / "scripts" / "bob.gd").exists()
    # alice's folder was left on the batch candidate; her next landing puts it back on her branch and lands her work
    code, out = praxis("land", "alice")
    assert code == 0, out
    assert (project / "scripts" / "alice.gd").exists()
    assert git(folder(project, "alice"), "branch", "--show-current") == "praxis/alice"
    assert queue_names(project) == []


def test_a_landing_stopped_right_after_main_moved_is_recorded_by_the_next_landing(project, tmp_path):
    """The batch moved main and the head was killed before it wrote the landings down: the next landing writes them
    down and tells the agent that was waiting."""
    import json

    from agent_praxis import landing_batch
    from agent_praxis.agents import Ctx
    from agent_praxis.cli import _ctx

    use_check(project, tmp_path)
    new_agent(project, "alice")
    new_agent(project, "bob")
    code, out = praxis("land", "alice")  # main now holds alice's work (as a stand-in for the batch's candidate)
    assert code == 0, out
    tip = git(project, "rev-parse", "main")
    store = Store(project / ".git" / "praxis")
    with store.write() as db:
        db.execute("DELETE FROM landings WHERE agent = 'alice'")
    ticket = Ticket(store, "bob", say=lambda _t: None)
    ticket.__enter__()
    marker_dir = project / ".git" / "praxis" / "batches"
    marker_dir.mkdir(exist_ok=True)
    (marker_dir / "alice.json").write_text(
        json.dumps(
            {
                "agent": "alice",
                "token": "deadbeef",
                "folder": str(folder(project, "alice")),
                "branch": "praxis/alice",
                "members": [
                    {"agent": "alice", "token": "deadbeef", "tip": tip},
                    {"agent": "bob", "token": ticket.token, "tip": git(folder(project, "bob"), "rev-parse", "HEAD")},
                ],
                "landing": {
                    "base": git(project, "rev-parse", "main~1"),
                    "tip": tip,
                    "members": [
                        {
                            "agent": "alice",
                            "task": "work of alice",
                            "token": "deadbeef",
                            "tip": tip,
                            "commit": tip,
                            "summary": "check passed, checked together with alice, bob",
                            "log": None,
                        }
                    ],
                },
                "recorded": False,
            }
        )
    )
    ctx, _store = _ctx()
    assert isinstance(ctx, Ctx)
    landing_batch.recover(ctx)
    rows = store.rows("SELECT agent, ok, commit_id FROM landings WHERE agent = 'alice'")
    assert [(r["agent"], r["ok"], r["commit_id"]) for r in rows] == [("alice", 1, tip)]
    assert not (marker_dir / "alice.json").exists() or json.loads((marker_dir / "alice.json").read_text())["recorded"]
    ticket.__exit__(None, None, None)
    store.close()
