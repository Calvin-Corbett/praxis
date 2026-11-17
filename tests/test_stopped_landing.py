"""A landing that is stopped part-way never loses anyone's work and never lands it twice.

Found by a scale test with 37 agents: an AI tool's time limit kills `praxis land` during the check. The agent at the
front of a batch is then left in a folder on no branch (the batch's candidate), where it carries on working; and an
agent waiting in the batch is never told that its work landed.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

from conftest import edit, folder, git, praxis
from test_batch import finish, land_behind_a_ghost, new_agent, queue_names, runs, use_check, wait_for
from test_inputs import named_check, use_checks
from test_inputs import runs as named_runs

from agent_praxis.landing_queue import Ticket, clear_dead, note_file, write_note
from agent_praxis.store import Store


def a_stopped_head(project: Path, tmp_path: Path) -> tuple[Path, Path]:
    """zed lands first (so main has moved since alice and bob started), then alice heads a batch with bob and her
    `praxis land` is killed during the batch's check. bob lands on his own turn. Returns alice's folder, which is
    left on no branch holding the batch's candidate, and the log the check writes its runs to."""
    gate = tmp_path / "go"
    log = use_check(project, tmp_path, gate=gate)
    for name in ("zed", "alice", "bob"):
        new_agent(project, name)
    top = (folder(project, "bob") / "check.py").read_text().splitlines()  # bob also changes a file everyone has
    edit(folder(project, "bob") / "check.py", "\n".join([top[0] + "  # bob", *top[1:]]) + "\n")
    gate.write_text("go")
    code, out = praxis("land", "zed")
    assert code == 0, out
    gate.unlink()
    procs, release = land_behind_a_ghost(project, ["alice", "bob"])
    release()
    wait_for(lambda: runs(log) == 2, "the batch's check to start")
    procs[0].kill()  # what a tool's time limit does
    procs[0].communicate(timeout=30)
    gate.write_text("go")
    codes, outs = finish(procs[1:])
    assert codes == [0], outs
    mine = folder(project, "alice")
    assert git(mine, "branch", "--show-current") == "", "alice's folder is on no branch"
    subjects = git(mine, "log", "--format=%s", "-2").splitlines()
    assert subjects == ["work of bob (by bob)", "work of alice (by alice)"], "it holds the batch's candidate"
    return mine, log


def commit(where: Path, message: str) -> str:
    git(where, "add", "-A")
    git(where, "commit", "-q", "-m", message)
    return git(where, "rev-parse", "HEAD")


def landings(project: Path, agent: str) -> list:
    store = Store(project / ".git" / "praxis")
    try:
        return store.rows("SELECT * FROM landings WHERE agent = ? AND ok = 1", (agent,))
    finally:
        store.close()


def test_work_saved_after_a_stopped_landing_is_carried_onto_the_branch_and_lands(project, tmp_path):
    mine, _log = a_stopped_head(project, tmp_path)
    edit(mine / "scripts" / "after_kill.gd", "extends Node\n# made after the landing was killed\n")
    commit(mine, "alice: more work after the kill")
    edit(mine / "scripts" / "second.gd", "extends Node\n")
    commit(mine, "alice: and more")
    edit(mine / "scripts" / "alice.gd", "extends Node\nvar unsaved = 1\n")  # an edit that is not saved yet
    edit(mine / "scripts" / "brand_new.gd", "extends Node\n")  # and a file git does not know yet
    code, out = praxis("land", "alice")
    assert code == 0, out
    assert "2 commit(s) were saved in it after that" in out and "Praxis put them on praxis/alice" in out, out
    assert "alice: more work after the kill" in out and "alice: and more" in out, "it says exactly which commits"
    assert git(mine, "branch", "--show-current") == "praxis/alice"
    in_main = git(project, "log", "--format=%s", "main").splitlines()
    assert "alice: more work after the kill" in in_main and "alice: and more" in in_main
    assert in_main.index("alice: more work after the kill") > in_main.index("alice: and more"), "in their order"
    assert in_main.count("work of bob (by bob)") == 1, "bob's work is not landed a second time as alice's"
    for name in ("after_kill.gd", "second.gd", "brand_new.gd"):
        assert (project / "scripts" / name).exists() and (mine / "scripts" / name).exists(), name
    assert "var unsaved = 1" in (project / "scripts" / "alice.gd").read_text(), "the unsaved edit landed too"
    assert git(project, "for-each-ref", "refs/praxis/saved/") == "", "nothing needed setting aside"


def test_a_folder_left_by_a_stopped_landing_is_put_back_when_nothing_was_done_in_it(project, tmp_path):
    mine, _log = a_stopped_head(project, tmp_path)
    code, status = praxis("status")
    assert code == 0, status
    needs = status.split("WORKING NOW")[0]
    assert "alice's landing was stopped part-way" in needs and "left on no branch" in needs, status
    assert "praxis land alice" in needs, "the one command that puts it right"
    code, out = praxis("land", "alice")
    assert code == 0, out
    assert "Put it back on praxis/alice; your own saved work was not changed." in out
    assert "commit(s) were saved in it" not in out
    assert git(mine, "branch", "--show-current") == "praxis/alice"
    assert (project / "scripts" / "alice.gd").exists()
    code, status = praxis("status")
    assert "Nothing needs you right now." in status, status


def test_done_puts_a_stopped_folder_back_the_same_way_before_it_answers(project, tmp_path):
    mine, _log = a_stopped_head(project, tmp_path)
    edit(mine / "scripts" / "after_kill.gd", "extends Node\n")
    commit(mine, "alice: more work after the kill")
    code, out = praxis("done", "alice")
    assert code == 1, out
    assert "Praxis put them on praxis/alice" in out and "alice: more work after the kill" in out, out
    assert "git switch -C" not in out, "no advice that would put the batch's commits on her branch"
    assert "still has work that is not in the main version" in out and "praxis land alice" in out, out
    assert git(mine, "branch", "--show-current") == "praxis/alice"
    assert git(mine, "log", "--format=%s", "-2").splitlines() == [
        "alice: more work after the kill",
        "work of alice (by alice)",
    ]


def test_work_that_cannot_be_carried_is_kept_under_a_name_and_the_folder_is_left_alone(project, tmp_path):
    mine, _log = a_stopped_head(project, tmp_path)
    # bob's file is in the folder (the candidate holds it) but not on alice's branch: a change to it cannot go there.
    edit(mine / "scripts" / "bob.gd", "extends Node\nvar changed_by_alice = 1\n")
    made = commit(mine, "alice: a change to a file her branch does not have")
    edit(mine / "scripts" / "alice.gd", "extends Node\nvar unsaved = 1\n")
    before = git(project, "rev-parse", "main")
    for _again in range(2):  # the same answer every time, and one name for the same commits
        code, out = praxis("land", "alice")
        assert code == 1, out
        assert "Praxis could not put them on praxis/alice by itself" in out and "Nothing was landed." in out, out
        assert "alice: a change to a file her branch does not have" in out
        assert "git switch praxis/alice" in out and f"git cherry-pick {made[:10]}" in out, "how to get it back"
    kept = git(project, "for-each-ref", "--format=%(refname) %(objectname)", "refs/praxis/saved/").splitlines()
    assert len(kept) == 1 and kept[0].startswith("refs/praxis/saved/alice-") and kept[0].endswith(made), kept
    assert kept[0].split()[0] in out, "the name is said"
    assert git(mine, "rev-parse", "HEAD") == made and git(mine, "branch", "--show-current") == "", "left alone"
    assert "changed_by_alice" in (mine / "scripts" / "bob.gd").read_text()
    assert "var unsaved = 1" in (mine / "scripts" / "alice.gd").read_text(), "the unsaved edit is still there"
    assert git(project, "rev-parse", "main") == before, "nothing landed"
    # What Praxis said to do gets the work onto her branch (she settles the clash git reports her own way).
    git(mine, "stash")
    git(mine, "switch", "-q", "praxis/alice")
    picked = subprocess.run(
        ["git", "cherry-pick", made[:10]], check=False, cwd=str(mine), capture_output=True, text=True
    )
    assert picked.returncode != 0, "it clashes, as Praxis said"
    git(mine, "add", "-A")
    git(mine, "-c", "core.editor=true", "cherry-pick", "--continue")
    git(mine, "stash", "pop")
    assert git(mine, "log", "--format=%s", "-1") == "alice: a change to a file her branch does not have"
    assert "changed_by_alice" in (mine / "scripts" / "bob.gd").read_text()
    assert "var unsaved = 1" in (mine / "scripts" / "alice.gd").read_text()


def test_an_unsaved_edit_git_would_overwrite_stops_the_put_back_and_survives(project, tmp_path):
    mine, _log = a_stopped_head(project, tmp_path)
    # check.py differs between the candidate (it holds bob's change to it) and alice's branch, and alice edits it.
    with open(mine / "check.py", "a") as handle:
        handle.write("# alice was here\n")
    head = git(mine, "rev-parse", "HEAD")
    code, out = praxis("land", "alice")
    assert code == 1, out
    assert "git would not switch it" in out and "check.py" in out and "Nothing was changed" in out, out
    assert "git add -A" in out and "then land again" in out, "what to do about it"
    assert git(mine, "rev-parse", "HEAD") == head and git(mine, "branch", "--show-current") == ""
    assert "# alice was here" in (mine / "check.py").read_text()
    commit(mine, "alice: a note in check.py")  # what Praxis said to do
    code, out = praxis("land", "alice")
    assert code == 0, out
    assert "1 commit(s) were saved in it after that" in out, out
    landed = (project / "check.py").read_text()
    assert "# alice was here" in landed and "# bob" in landed, "her edit and bob's change are both in main"
    assert git(mine, "branch", "--show-current") == "praxis/alice"


def test_a_commit_made_in_the_folder_while_the_batch_check_runs_is_not_dropped(project, tmp_path):
    gate = tmp_path / "go"
    log = use_check(project, tmp_path, gate=gate)
    new_agent(project, "alice")
    new_agent(project, "bob")
    procs, release = land_behind_a_ghost(project, ["alice", "bob"])
    release()
    wait_for(lambda: runs(log) == 1, "the batch's check to start")
    mine = folder(project, "alice")
    code, out = praxis("land", "alice")  # a second landing while the first runs: no advice to move the folder
    assert code == 1 and "alice is already landing" in out and "git switch" not in out, out
    code, status = praxis("status")
    assert "Nothing needs you right now." in status, "a running batch has the folder on no branch on purpose"
    edit(mine / "scripts" / "meanwhile.gd", "extends Node\n")  # the agent kept working while its landing ran
    commit(mine, "alice: saved while the check ran")
    gate.write_text("go")
    codes, outs = finish(procs)
    assert codes[0] == 2 and "A new commit was made in the folder while the check ran" in outs[0], outs
    assert "Praxis put them on praxis/alice" in outs[0] and "alice: saved while the check ran" in outs[0], outs[0]
    assert git(mine, "branch", "--show-current") == "praxis/alice"
    assert git(mine, "log", "--format=%s", "-1") == "alice: saved while the check ran", "it is on her branch"
    assert (mine / "scripts" / "meanwhile.gd").exists()
    assert codes[1] == 0, outs[1]
    code, out = praxis("land", "alice")
    assert code == 0, out
    assert (project / "scripts" / "meanwhile.gd").exists() and (project / "scripts" / "alice.gd").exists()


def a_member_whose_landing_ended(project: Path, tmp_path: Path) -> Path:
    """alice heads a batch with bob; bob's waiting `praxis land` is killed during the check; the batch lands both."""
    gate = tmp_path / "go"
    log = use_check(project, tmp_path, gate=gate)
    new_agent(project, "alice")
    new_agent(project, "bob")
    procs, release = land_behind_a_ghost(project, ["alice", "bob"])
    release()
    wait_for(lambda: runs(log) == 1, "the batch's check to start")
    procs[1].kill()
    procs[1].communicate(timeout=30)
    gate.write_text("go")
    codes, outs = finish(procs[:1])
    assert codes == [0], outs
    assert (project / "scripts" / "bob.gd").exists(), "the batch landed bob's work all the same"
    return log


def test_a_batch_member_whose_landing_ended_is_shown_as_landed_and_not_landed_twice(project, tmp_path):
    log = a_member_whose_landing_ended(project, tmp_path)
    assert len(landings(project, "bob")) == 1
    assert git(project, "rev-list", "--count", "main..praxis/bob") == "1", "bob's branch still holds the original"
    assert list((project / ".git" / "praxis" / "queue").glob("*.note")) == [], "no note is left for a dead process"
    code, status = praxis("status")
    assert code == 0, status
    needs = status.split("WORKING NOW")[0]
    assert "bob's landing was stopped part-way" in needs and "praxis land bob" in needs, status
    assert "saved change(s) not landed yet" not in status, status
    assert "its saved work landed" in status and "(in a batch)" in status, status
    # main moves on, then bob's tool tries again
    new_agent(project, "carol")
    code, out = praxis("land", "carol")
    assert code == 0, out
    checks = runs(log)
    code, out = praxis("land", "bob")
    assert code == 0, out
    assert "already landed at" in out and "(in a batch)" in out, out
    assert "turn to land" not in out and "Landed:" not in out, "no turn in the line was taken"
    assert runs(log) == checks, "no check ran"
    assert len(landings(project, "bob")) == 1, "one landing, not two"
    assert git(project, "rev-list", "--count", "main..praxis/bob") == "0", "its folder was levelled"
    assert git(folder(project, "bob"), "status", "--porcelain") == ""
    assert queue_names(project) == []
    code, status = praxis("status")
    assert "Nothing needs you right now." in status, status
    code, out = praxis("done", "bob")
    assert code == 0, out


def test_new_work_on_top_of_work_a_batch_landed_still_lands_once(project, tmp_path):
    a_member_whose_landing_ended(project, tmp_path)
    edit(folder(project, "bob") / "scripts" / "bob.gd", "extends Node\nvar more = 1\n")
    code, out = praxis("land", "bob")
    assert code == 0, out
    assert "already landed" not in out and "Landed: bob's work" in out, out
    assert "var more = 1" in (project / "scripts" / "bob.gd").read_text()
    assert git(project, "log", "--format=%s", "main").splitlines().count("work of bob (by bob)") == 2


def test_what_dead_landings_left_in_the_line_is_cleared(project):
    store = Store(project / ".git" / "praxis")
    try:
        alive = Ticket(store, "alice", say=lambda _t: None)
        alive.__enter__()
        with store.write() as db:
            db.execute("INSERT INTO queue (agent, token, enqueued) VALUES ('bob', 'deadbeef', 0)")
        (store.dir / "queue" / "deadbeef.lock").write_bytes(b"\n{}")
        write_note(store, "deadbeef", {"done": True, "ok": True, "lines": []})
        write_note(store, "nobody", {"members": ["x", "y"]})  # a note whose place in line is already gone
        write_note(store, alive.token, {"members": ["alice", "carol"]})
        clear_dead(store)
        assert [r["agent"] for r in store.rows("SELECT agent FROM queue")] == ["alice"]
        assert not note_file(store, "deadbeef").exists() and not note_file(store, "nobody").exists()
        assert not (store.dir / "queue" / "deadbeef.lock").exists()
        assert note_file(store, alive.token).exists(), "a running landing keeps its note"
        alive.__exit__(None, None, None)
    finally:
        store.close()


def test_a_waiting_agent_is_told_about_its_batch_even_when_the_result_is_the_first_thing_it_sees(project):
    """A quick check can be over between two looks at the line: the note already says how it went."""
    store = Store(project / ".git" / "praxis")
    said: list[str] = []
    try:
        ticket = Ticket(store, "bob", say=said.append)
        write_note(store, ticket.token, {"done": True, "ok": True, "lines": ["x"], "members": ["alice", "bob"]})
        ticket.__enter__()
        assert ticket.verdict is not None
        assert len(said) == 1 and said[0].startswith("Your work was taken into a batch: alice, bob "), said
        ticket.__exit__(None, None, None)
    finally:
        store.close()


def test_levelling_a_folder_never_writes_over_a_new_file_git_does_not_know(project, tmp_path):
    gate = tmp_path / "go"
    log = use_check(project, tmp_path, gate=gate)
    new_agent(project, "alice")
    new_agent(project, "bob")
    procs, release = land_behind_a_ghost(project, ["alice", "bob"])
    release()
    wait_for(lambda: runs(log) == 1, "the batch's check to start")
    # While bob waits, a file appears in his folder that alice's work (landing in the same batch) also adds.
    edit(folder(project, "bob") / "scripts" / "alice.gd", "bob's own notes\n")
    gate.write_text("go")
    codes, outs = finish(procs)
    assert codes == [0, 0], outs
    assert (folder(project, "bob") / "scripts" / "alice.gd").read_text() == "bob's own notes\n", outs[1]
    assert "folder now matches the main version" not in outs[1]


# --- an edit in the front agent's folder while a batch's check runs there ---------------------------------------------


def _both_in_line_then_edit(project, started, edited: str, text: str):
    """alice heads a batch with bob; once its check has started (*started* says so), a file in alice's folder is
    edited. Returns the two processes; the caller lets the check finish."""
    new_agent(project, "alice")
    new_agent(project, "bob")
    procs, release = land_behind_a_ghost(project, ["alice", "bob"])
    release()
    wait_for(started, "the batch's check to start")
    with open(folder(project, "alice") / edited, "a") as handle:
        handle.write(text)  # the agent kept editing while its landing ran
    return procs


def test_an_edit_during_a_batch_check_refuses_that_agent_and_the_whole_project_check_runs_again(project, tmp_path):
    """A check that reads the whole project cannot be known to have tested the candidate: nothing is kept."""
    gate = tmp_path / "go"
    log = use_check(project, tmp_path, gate=gate)
    procs = _both_in_line_then_edit(project, lambda: runs(log) == 1, "project.godot", "; edited\n")
    gate.write_text("go")
    codes, outs = finish(procs)
    assert codes == [2, 0], outs
    assert "NOT landed: files changed while the check ran" in outs[0] and "project.godot" in outs[0], outs[0]
    assert "Landed: bob's work" in outs[1] and "files changed" not in outs[1], "only alice is refused"
    assert runs(log) == 2, "bob's work alone was never checked, so it is checked on his own turn"
    assert (project / "scripts" / "bob.gd").exists() and not (project / "scripts" / "alice.gd").exists()
    assert "; edited" in (folder(project, "alice") / "project.godot").read_text(), "her edit is still there"


def _a_check_for_each(project: Path, tmp_path: Path) -> Path:
    """Two named checks, one after the other: `alice` reads scripts/alice.gd, `bob` reads scripts/bob.gd. Each waits
    for a gate in the folder this returns."""
    gates = tmp_path / "gates"
    gates.mkdir()
    entries = [named_check(tmp_path, name, [f"scripts/{name}.gd"], gates=gates) for name in ("alice", "bob")]
    use_checks(project, *entries, checks_together="false")
    return gates


def test_an_edit_no_check_reads_costs_the_other_agents_no_second_check(project, tmp_path):
    """Named checks say what they read. An edit to a file none of them reads cannot have changed what they found, so
    their passes are kept for the files they read: bob lands on his own turn without his check running again."""
    gates = _a_check_for_each(project, tmp_path)
    procs = _both_in_line_then_edit(project, lambda: named_runs(tmp_path), "project.godot", "; edited\n")
    (gates / "all").write_text("go")
    codes, outs = finish(procs)
    assert codes == [2, 0], outs
    assert "NOT landed: files changed while the check ran" in outs[0], outs[0]
    assert "Skipping check bob: it passed at " in outs[1] and "Landed: bob's work" in outs[1], outs[1]
    assert sorted(named_runs(tmp_path)) == ["alice", "bob"], "each check ran once, for the batch"
    code, out = praxis("land", "alice")  # her edit is saved and lands; her own check read none of it either
    assert code == 0, out
    assert "Skipping check alice: it passed at " in out, out
    assert sorted(named_runs(tmp_path)) == ["alice", "bob"]
    assert "; edited" in (project / "project.godot").read_text()


def test_an_edit_to_a_file_a_check_reads_is_never_taken_for_a_pass(project, tmp_path):
    gates = _a_check_for_each(project, tmp_path)
    started = lambda: named_runs(tmp_path)
    procs = _both_in_line_then_edit(project, started, "scripts/alice.gd", "# edited while the check ran\n")
    (gates / "all").write_text("go")
    codes, outs = finish(procs)
    assert codes == [2, 0], outs
    assert "Skipping check bob: it passed at " in outs[1], "bob's check read nothing that changed"
    git(folder(project, "alice"), "checkout", "--", "scripts/alice.gd")  # exactly the files the batch checked again
    code, out = praxis("land", "alice")
    assert code == 0, out
    assert "Running check alice: " in out and "Skipping check alice" not in out, "its pass was not remembered"
    assert sorted(named_runs(tmp_path)) == ["alice", "alice", "bob"]


NUMBERED_CHECK = """\
import os, pathlib, sys, time
log, gates = sys.argv[1], sys.argv[2]
with open(log, "a") as f:
    f.write("run\\n")
number = len(open(log).read().splitlines())
for _ in range(1200):
    if os.path.exists(os.path.join(gates, str(number))) or os.path.exists(os.path.join(gates, "all")):
        break
    time.sleep(0.05)
bad = [str(p) for p in pathlib.Path("scripts").glob("*.gd") if "BROKEN" in p.read_text()]
sys.exit(1 if bad else 0)
"""


def test_an_agent_that_landed_in_the_batch_stays_landed_when_a_later_check_in_its_folder_is_spoiled(project, tmp_path):
    """alice, bob, carol (broken) and dave land together. The batch fails, alice and bob land as a pair, carol is
    refused, and while dave's work is checked alone in alice's folder a file there is edited. That check does not
    count, so dave lands on his own turn; alice's landing is not turned into a failure."""
    script, log, gates = tmp_path / "numbered.py", tmp_path / "check-runs.log", tmp_path / "gates"
    script.write_text(NUMBERED_CHECK)
    gates.mkdir()
    command = f'"{sys.executable}" "{script}" "{log}" "{gates}"'
    toml = project / "praxis.toml"
    lines = [f"check = '{command}'" if ln.startswith("check =") else ln for ln in toml.read_text().splitlines()]
    toml.write_text("\n".join(ln for ln in lines if not ln.startswith("batch_max =")) + "\nbatch_max = 4\n")
    new_agent(project, "alice")
    new_agent(project, "bob")
    new_agent(project, "carol", "BROKEN\n")
    new_agent(project, "dave")
    for number in range(1, 4):  # the whole batch, alice + bob, carol alone (carol + dave: the known failure)
        (gates / str(number)).write_text("go")
    procs, release = land_behind_a_ghost(project, ["alice", "bob", "carol", "dave"])
    release()
    wait_for(lambda: runs(log) == 4, "dave's check, alone, to start")
    with open(folder(project, "alice") / "project.godot", "a") as handle:
        handle.write("; edited\n")
    (gates / "all").write_text("go")
    codes, outs = finish(procs)
    assert codes == [0, 0, 2, 0], outs
    assert "Landed: alice's work" in outs[0] and "NOT landed" not in outs[0], outs[0]
    assert "that check does not count" in outs[0] and "dave" in outs[0], "alice's process says what happened"
    assert "Landed: dave's work" in outs[3], outs[3]
    assert runs(log) == 5, "dave's work is checked again on his own turn"
    store = Store(project / ".git" / "praxis")
    try:
        rows = store.rows("SELECT ok, kind FROM landings WHERE agent = 'alice'")
    finally:
        store.close()
    assert [(r["ok"], r["kind"]) for r in rows] == [(1, "landed")], "no failure is written against alice"
    for name in ("alice", "bob", "dave"):
        assert (project / "scripts" / f"{name}.gd").exists()
    assert not (project / "scripts" / "carol.gd").exists()
