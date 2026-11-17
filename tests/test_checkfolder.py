"""The landing line can run its check in a folder of its own (praxis.toml `check_folder = "line"`).

That folder (`<agent folders>/_line`, a git worktree on no branch, kept between landings) holds exactly the files
being landed. So an edit in an agent's folder while the check runs cannot change what was tested: the verdict stands,
nobody pays for a second check, and the agent may keep working. A landing lands the work as it was saved when the
landing started; what is saved or edited in the agent's folder after that stays there for the next landing.
"""

from __future__ import annotations

import subprocess
import sys
import types
from pathlib import Path

from conftest import edit, folder, git, praxis, praxis_process
from test_batch import finish, land_behind_a_ghost, main_subjects, new_agent, runs, wait_for
from test_inputs import set_line
from test_stopped_landing import NUMBERED_CHECK, commit, landings

from agent_praxis import checkfolder, config, fullcheck
from agent_praxis.store import Store

# A short fake check. Each run writes one line: the name of the folder it runs in, whether the ignored file
# cache/warm.txt is there, and whether scripts/left.gd (a file git neither tracks nor ignores) is there. It waits for a
# gate file when it is given one, and fails when a script says BROKEN.
WHERE_CHECK = """\
import os, pathlib, sys, time
log, gate = sys.argv[1], (sys.argv[2] if len(sys.argv) > 2 else None)
with open(log, "a") as f:
    f.write(" ".join([os.path.basename(os.getcwd()), str(int(os.path.exists("cache/warm.txt"))),
                      str(int(os.path.exists("scripts/left.gd")))]) + "\\n")
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


def line_check(project: Path, tmp_path: Path, gate: Path | None = None, where: str | None = "line",
               script: str = WHERE_CHECK) -> Path:
    """Use the fake check above, and set check_folder to *where* (None: leave the setting out). Returns the log the
    check writes its runs to."""
    path, log = tmp_path / "where_check.py", tmp_path / "check-runs.log"
    path.write_text(script)
    command = f'"{sys.executable}" "{path}" "{log}"' + (f' "{gate}"' if gate else "")
    set_line(project, f"check = '{command}'")
    if where is not None:
        set_line(project, f'check_folder = "{where}"')
    return log


def ran_in(log: Path) -> list[str]:
    """The folder each run of the check ran in, in order."""
    return [line.split()[0] for line in log.read_text().splitlines()] if log.exists() else []


def own_folders(project: Path) -> list[str]:
    """The folders Praxis keeps for itself next to the agent folders."""
    return sorted(p.name for p in folder(project, "x").parent.glob("_*"))


def ignore(project: Path, *patterns: str) -> None:
    """The main version ignores *patterns* from now on."""
    (project / ".gitignore").write_text("\n".join(patterns) + "\n")
    git(project, "add", ".gitignore")
    git(project, "commit", "-q", "-m", "ignore " + " ".join(patterns))


def on_branch(project: Path, name: str) -> bool:
    return git(folder(project, name), "branch", "--show-current") == f"praxis/{name}"


# --- the setting -------------------------------------------------------------------------------------------------------


def test_it_is_off_unless_you_turn_it_on(project, tmp_path):
    log = line_check(project, tmp_path, where=None)
    new_agent(project, "alice")
    code, out = praxis("land", "alice")
    assert code == 0, out
    assert ran_in(log) == ["alice"], "the check ran in the agent's own folder, as it always did"
    assert own_folders(project) == [], "and no folder was made for the line"
    assert "check_folder" in (project / "praxis.toml").read_text(), "praxis init writes the setting with its note"


def test_check_folder_must_be_agent_or_line(project):
    set_line(project, 'check_folder = "elsewhere"')
    code, out = praxis("status")
    assert code == 1 and 'check_folder must be "agent"' in out and '"line"' in out and "'elsewhere'" in out, out
    set_line(project, "check_folder = true")
    code, out = praxis("status")
    assert code == 1 and "check_folder must be text in quotes" in out, out


# --- one folder, exactly the files being landed, kept warm ---------------------------------------------------------------


def test_the_check_runs_in_the_lines_one_folder_on_exactly_the_files_being_landed(project, tmp_path):
    ignore(project, "cache/")
    log = line_check(project, tmp_path)
    new_agent(project, "alice")
    new_agent(project, "bob")
    code, out = praxis("land", "alice")
    assert code == 0, out
    line = folder(project, "_line")
    assert "the landing line's own folder" in out and str(line) in out, "it says where the check runs"
    assert ran_in(log) == ["_line"]
    assert git(line, "branch", "--show-current") == "", "the line's folder is on no branch"
    assert git(line, "rev-parse", "HEAD") == git(project, "rev-parse", "main"), "it held exactly what landed"
    edit(line / "cache" / "warm.txt", "what a tool cached during the first check\n")  # git ignores it
    edit(line / "scripts" / "left.gd", "extends Node\n")  # git neither tracks nor ignores it
    edit(line / "scripts" / "player.gd", "left changed by the first check\n")  # a tracked file left changed
    code, out = praxis("land", "bob")
    assert code == 0, out
    assert log.read_text().splitlines() == ["_line 0 0", "_line 1 0"], "the cache is kept, the leftover is gone"
    assert (line / "cache" / "warm.txt").exists() and not (line / "scripts" / "left.gd").exists()
    assert git(line, "status", "--porcelain") == "", "only the files being landed, and what git ignores"
    assert (project / "scripts" / "player.gd").read_text() == "extends Node\nvar speed = 10\n"
    assert own_folders(project) == ["_line"], "one folder for the line, however many landings"
    assert on_branch(project, "alice") and on_branch(project, "bob")


def test_a_pass_remembered_from_before_is_found_and_the_folder_is_not_even_made(project, tmp_path):
    """The memory of passed checks does not depend on where a check ran: the same files and the same command."""
    log = line_check(project, tmp_path)
    new_agent(project, "alice")
    commit(folder(project, "alice"), "work of alice")
    code, out = praxis("check", "alice")  # earns the pass in a scratch folder of its own
    assert code == 0, out
    code, out = praxis("land", "alice")
    assert code == 0, out
    assert "same tree already passed" in out and runs(log) == 1, out
    assert own_folders(project) == ["_check-1"], "nothing had to run, so the line's folder was not made"


# --- the agent keeps working while its landing is checked ---------------------------------------------------------------


def test_an_edit_in_the_agents_folder_during_the_check_costs_no_second_check(project, tmp_path):
    gate = tmp_path / "go"
    log = line_check(project, tmp_path, gate=gate)
    new_agent(project, "alice")
    proc = praxis_process(project, "land", "alice")
    wait_for(lambda: runs(log) == 1, "the check to start")
    mine = folder(project, "alice")
    with open(mine / "project.godot", "a") as handle:
        handle.write("; edited\n")  # the agent kept working while its landing ran
    edit(mine / "scripts" / "next.gd", "extends Node\n")
    gate.write_text("go")
    codes, outs = finish([proc])
    assert codes == [0], outs
    assert "Landed: alice's work" in outs[0] and "files changed while the check ran" not in outs[0], outs[0]
    assert runs(log) == 1, "the verdict stands: no second check"
    assert (project / "scripts" / "alice.gd").exists()
    assert "; edited" not in (project / "project.godot").read_text(), "what was edited after the start did not land"
    assert not (project / "scripts" / "next.gd").exists()
    assert "; edited" in (mine / "project.godot").read_text() and (mine / "scripts" / "next.gd").exists()
    assert "2 file(s) changed in alice's folder after this landing started" in outs[0], outs[0]
    assert on_branch(project, "alice")
    code, out = praxis("land", "alice")  # the next landing saves and lands them
    assert code == 0, out
    assert "; edited" in (project / "project.godot").read_text() and (project / "scripts" / "next.gd").exists()
    assert ran_in(log) == ["_line", "_line"]


def test_an_edit_in_the_front_agents_folder_during_a_batchs_check_costs_nobody_a_second_check(project, tmp_path):
    """With the check in the agent's folder this refuses alice and makes bob pay for a second whole-project check
    (see test_stopped_landing). In the line's folder nothing she does can change what was tested."""
    gate = tmp_path / "go"
    log = line_check(project, tmp_path, gate=gate)
    new_agent(project, "alice")
    new_agent(project, "bob")
    procs, release = land_behind_a_ghost(project, ["alice", "bob"])
    release()
    wait_for(lambda: runs(log) == 1, "the batch's check to start")
    assert on_branch(project, "alice"), "the front agent's folder never leaves its branch"
    with open(folder(project, "alice") / "project.godot", "a") as handle:
        handle.write("; edited\n")
    gate.write_text("go")
    codes, outs = finish(procs)
    assert codes == [0, 0], outs
    assert "Landed: alice's work" in outs[0] and "Landed: bob's work" in outs[1], outs
    assert ran_in(log) == ["_line"], "one check for both, and it stands"
    assert (project / "scripts" / "alice.gd").exists() and (project / "scripts" / "bob.gd").exists()
    assert "; edited" not in (project / "project.godot").read_text()
    assert "; edited" in (folder(project, "alice") / "project.godot").read_text(), "her edit is still there"
    assert on_branch(project, "alice") and on_branch(project, "bob")


def test_commits_saved_after_the_landing_started_are_kept_and_not_landed(project, tmp_path):
    gate = tmp_path / "go"
    log = line_check(project, tmp_path, gate=gate)
    new_agent(project, "alice")
    proc = praxis_process(project, "land", "alice")
    wait_for(lambda: runs(log) == 1, "the check to start")
    mine = folder(project, "alice")
    taken = git(mine, "rev-parse", "HEAD")
    edit(mine / "scripts" / "later.gd", "extends Node\n")
    later = commit(mine, "alice: saved while the landing was checked")
    gate.write_text("go")
    codes, outs = finish([proc])
    assert codes == [0], outs
    assert git(project, "rev-parse", "main") == taken, "main holds the work as it was when the landing started"
    assert not (project / "scripts" / "later.gd").exists(), "a commit no check tested is never landed"
    assert "1 commit(s) were saved in alice's folder after this landing started" in outs[0], outs[0]
    assert "alice: saved while the landing was checked" in outs[0] and "praxis land alice" in outs[0], outs[0]
    assert on_branch(project, "alice") and git(mine, "rev-parse", "HEAD") == later, "kept, on her own branch"
    assert git(mine, "rev-parse", "HEAD~1") == taken, "right on top of what landed"
    code, out = praxis("land", "alice")
    assert code == 0, out
    assert (project / "scripts" / "later.gd").exists() and runs(log) == 2
    assert main_subjects(project).count("work of alice (by alice)") == 1, "nothing landed twice"


def test_the_front_agent_of_a_batch_can_save_more_work_while_the_batch_is_checked(project, tmp_path):
    gate = tmp_path / "go"
    log = line_check(project, tmp_path, gate=gate)
    new_agent(project, "alice")
    new_agent(project, "bob")
    procs, release = land_behind_a_ghost(project, ["alice", "bob"])
    release()
    wait_for(lambda: runs(log) == 1, "the batch's check to start")
    mine = folder(project, "alice")
    edit(mine / "scripts" / "later.gd", "extends Node\n")
    later = commit(mine, "alice: saved while the batch was checked")
    edit(mine / "scripts" / "unsaved.gd", "extends Node\n")
    gate.write_text("go")
    codes, outs = finish(procs)
    assert codes == [0, 0], outs
    assert runs(log) == 1
    assert (project / "scripts" / "alice.gd").exists() and (project / "scripts" / "bob.gd").exists()
    assert not (project / "scripts" / "later.gd").exists() and not (project / "scripts" / "unsaved.gd").exists()
    assert "1 commit(s) were saved in alice's folder after this landing started" in outs[0], outs[0]
    assert on_branch(project, "alice") and git(mine, "rev-parse", "HEAD") == later
    assert (mine / "scripts" / "unsaved.gd").exists()
    assert [(r["ok"], r["kind"]) for r in landings(project, "alice")] == [(1, "landed")]
    code, out = praxis("land", "alice")
    assert code == 0, out
    assert (project / "scripts" / "later.gd").exists() and (project / "scripts" / "unsaved.gd").exists()
    for name in ("alice", "bob"):  # what the batch landed is in main once
        assert len(git(project, "log", "--format=%s", "main", "--", f"scripts/{name}.gd").splitlines()) == 1, name
    assert runs(log) == 2


def test_work_rewritten_during_the_check_lands_as_it_was_when_the_landing_started(project, tmp_path):
    gate = tmp_path / "go"
    log = line_check(project, tmp_path, gate=gate)
    new_agent(project, "alice")
    proc = praxis_process(project, "land", "alice")
    wait_for(lambda: runs(log) == 1, "the check to start")
    mine = folder(project, "alice")
    taken = git(mine, "rev-parse", "HEAD")
    edit(mine / "scripts" / "alice.gd", "extends Node\nvar rewritten = 1\n")
    git(mine, "commit", "-q", "-a", "--amend", "-m", "work of alice, rewritten")
    gate.write_text("go")
    codes, outs = finish([proc])
    assert codes == [0], outs
    assert git(project, "rev-parse", "main") == taken
    assert (project / "scripts" / "alice.gd").read_text() == "extends Node\n", "what the check tested is what landed"
    assert "no longer holds the commits that landed" in outs[0], outs[0]
    assert "var rewritten = 1" in (mine / "scripts" / "alice.gd").read_text() and on_branch(project, "alice")


def _the_agent_starts_a_merge_of_its_own(project: Path) -> Path:
    """alice has a merge under way in her folder that stopped on a clash (her own doing, not Praxis's)."""
    mine = folder(project, "alice")
    git(mine, "branch", "side", "HEAD~1")
    git(mine, "worktree", "add", "-q", str(mine.parent / "side"), "side")
    edit(mine.parent / "side" / "scripts" / "alice.gd", "another version\n")
    commit(mine.parent / "side", "the other side")
    merged = subprocess.run(["git", "merge", "side"], cwd=mine, capture_output=True, text=True, check=False)
    assert merged.returncode != 0, merged.stdout
    return mine


def test_a_landing_stopped_during_its_check_leaves_a_git_step_of_the_agents_own_alone(project, tmp_path, monkeypatch):
    from agent_praxis import gitops, landing

    line_check(project, tmp_path)
    new_agent(project, "alice")

    def stopped(*_a, **_k):  # the check has started; the agent works on; then the landing is stopped (Ctrl+C)
        _the_agent_starts_a_merge_of_its_own(project)
        raise KeyboardInterrupt

    monkeypatch.setattr(landing, "_run_check", stopped)
    assert praxis("land", "alice")[0] == 130
    mine = folder(project, "alice")
    assert gitops.operation_in_progress(mine) == "merge", "her half-done merge is still hers to finish"
    assert "<<<<<<<" in (mine / "scripts" / "alice.gd").read_text() and on_branch(project, "alice")
    assert not (project / "scripts" / "alice.gd").exists(), "and nothing landed"

# --- batches -----------------------------------------------------------------------------------------------------------


def test_a_batch_is_built_checked_and_halved_in_the_lines_folder(project, tmp_path):
    log = line_check(project, tmp_path)
    set_line(project, "batch_max = 4")
    new_agent(project, "alice")
    new_agent(project, "bob", "BROKEN\n")
    new_agent(project, "carol")
    procs, release = land_behind_a_ghost(project, ["alice", "bob", "carol"])
    release()
    codes, outs = finish(procs)
    assert codes == [0, 2, 0], outs
    assert "broken script" in outs[1] and "this is the work that broke the check" in outs[1], outs[1]
    where = ran_in(log)
    assert len(where) >= 3 and set(where) == {"_line"}, "the whole batch and every smaller group, one folder"
    assert (project / "scripts" / "alice.gd").exists() and (project / "scripts" / "carol.gd").exists()
    assert not (project / "scripts" / "bob.gd").exists()
    assert own_folders(project) == ["_line"]
    for name in ("alice", "bob", "carol"):
        assert on_branch(project, name), name
    assert git(folder(project, "carol"), "rev-parse", "HEAD") == git(project, "rev-parse", "main")
    git(project, "merge-base", "--is-ancestor", git(folder(project, "alice"), "rev-parse", "HEAD"), "main")
    assert not list((project / ".git" / "praxis" / "batches").glob("*.json")), "no marker is left"


# --- stopped part-way --------------------------------------------------------------------------------------------------


def test_a_landing_killed_during_its_check_leaves_the_folder_usable(project, tmp_path):
    gate = tmp_path / "go"
    log = line_check(project, tmp_path, gate=gate)
    new_agent(project, "alice")
    new_agent(project, "bob")
    edit(folder(project, "bob") / "project.godot", "bob's version\n")  # for a half-done git step, below
    proc = praxis_process(project, "land", "alice")
    wait_for(lambda: runs(log) == 1, "the check to start")
    proc.kill()  # what a tool's time limit does
    proc.communicate(timeout=30)
    line = folder(project, "_line")
    # Everything a check that was cut short can leave behind, and a git step that was cut short too.
    edit(line / "scripts" / "player.gd", "BROKEN\n")
    edit(line / "scripts" / "junk.gd", "BROKEN\n")
    edit(line / "project.godot", "the line's own half-written version\n")
    commit(folder(project, "bob"), "work of bob")
    git(line, "add", "project.godot")
    git(line, "-c", "user.name=T", "-c", "user.email=t@example.com", "commit", "-q", "-m", "debris")
    merged = subprocess.run(["git", "merge", "praxis/bob"], cwd=line, capture_output=True, text=True, check=False)
    assert merged.returncode != 0 and (line / "project.godot").read_text().count("<<<<<<<") == 1, merged.stdout
    lock = Path(git(line, "rev-parse", "--path-format=absolute", "--git-path", "index.lock"))
    lock.write_text("")
    gate.write_text("go")
    assert on_branch(project, "alice"), "the agent's folder was never moved"
    code, out = praxis("land", "alice")
    assert code == 0, out
    assert "Landed: alice's work" in out and ran_in(log) == ["_line", "_line"], out
    assert git(line, "status", "--porcelain") == "" and not (line / "scripts" / "junk.gd").exists()
    assert git(line, "rev-parse", "HEAD") == git(project, "rev-parse", "main")
    assert "BROKEN" not in (project / "scripts" / "player.gd").read_text()
    assert main_subjects(project)[-1] == "work of alice (by alice)", "and nothing of the debris landed"
    code, out = praxis("land", "bob")
    assert code == 0, out


def test_the_front_agent_of_a_batch_killed_during_the_check_is_left_on_its_own_branch(project, tmp_path):
    """With the check in the agent's folder a stopped batch leaves that folder on no branch, holding the batch's
    files (see test_stopped_landing). Here the batch never was in it."""
    gate = tmp_path / "go"
    log = line_check(project, tmp_path, gate=gate)
    new_agent(project, "alice")
    new_agent(project, "bob")
    procs, release = land_behind_a_ghost(project, ["alice", "bob"])
    release()
    wait_for(lambda: runs(log) == 1, "the batch's check to start")
    procs[0].kill()
    procs[0].communicate(timeout=30)
    gate.write_text("go")
    codes, outs = finish(procs[1:])
    assert codes == [0], outs
    assert on_branch(project, "alice")
    assert git(folder(project, "alice"), "log", "--format=%s", "-1") == "work of alice (by alice)"
    code, out = praxis("land", "alice")
    assert code == 0, out
    assert "left on a batch candidate" not in out and "Landed: alice's work" in out, out
    assert set(ran_in(log)) == {"_line"}
    subjects = main_subjects(project)
    assert subjects.count("work of alice (by alice)") == 1 and subjects.count("work of bob (by bob)") == 1, subjects


# --- where the two ways of checking differ ----------------------------------------------------------------------------

# A check that needs a file git ignores: it passes only where local/tool.txt is.
NEEDS_TOOL = """\
import os, sys
if not os.path.exists("local/tool.txt"):
    print("ERROR: local/tool.txt is missing in", os.path.basename(os.getcwd()))
    sys.exit(1)
"""
MAKE_TOOL = "import os\nos.makedirs('local', exist_ok=True)\nopen('local/tool.txt', 'w').write('made by setup')\n"


def _needs_an_ignored_file(project: Path, tmp_path: Path, where: str) -> None:
    ignore(project, "local/")
    line_check(project, tmp_path, where=where, script=NEEDS_TOOL)


def test_a_check_that_needs_a_file_the_agent_made_by_hand_passes_only_in_the_agents_folder(project, tmp_path):
    """The one place the two cannot agree: the line's folder holds what is committed, what `share` links and what
    `setup` makes. A file git ignores that an agent put into its own folder by hand is not there."""
    _needs_an_ignored_file(project, tmp_path, "agent")
    new_agent(project, "alice")
    new_agent(project, "bob")
    for name in ("alice", "bob"):
        edit(folder(project, name) / "local" / "tool.txt", "installed by hand\n")
    code, out = praxis("land", "alice")
    assert code == 0, out
    set_line(project, 'check_folder = "line"')
    code, out = praxis("land", "bob")
    assert code == 2, out
    assert "NOT landed: the check failed" in out and "local/tool.txt is missing in _line" in out, out
    assert [(r["ok"], r["kind"]) for r in landings(project, "bob")] == []


def test_what_setup_makes_is_in_the_lines_folder_too(project, tmp_path):
    _needs_an_ignored_file(project, tmp_path, "line")
    script = tmp_path / "make_tool.py"
    script.write_text(MAKE_TOOL)
    set_line(project, f"setup = '\"{sys.executable}\" \"{script}\"'")
    new_agent(project, "alice")
    new_agent(project, "bob")
    code, out = praxis("land", "alice")
    assert code == 0, out
    assert "Running the setup command" in out and str(folder(project, "_line")) in out, out
    assert (folder(project, "_line") / "local" / "tool.txt").read_text() == "made by setup"
    code, out = praxis("land", "bob")
    assert code == 0, out
    assert "Running the setup command" not in out, "once, when the folder is made"


def test_a_shared_folder_is_linked_into_the_lines_folder_too(project, tmp_path):
    _needs_an_ignored_file(project, tmp_path, "line")
    edit(project / "local" / "tool.txt", "the one shared copy\n")
    set_line(project, 'share = ["local"]')
    new_agent(project, "alice")
    code, out = praxis("land", "alice")
    assert code == 0, out
    assert (folder(project, "_line") / "local" / "tool.txt").read_text() == "the one shared copy\n"
    assert (project / "local" / "tool.txt").exists(), "the shared folder itself is never cleared"


def test_a_setup_that_fails_in_the_lines_folder_lands_nothing_and_is_not_the_agents_failure(project, tmp_path):
    line_check(project, tmp_path)
    script = tmp_path / "setup.py"
    script.write_text("import os, sys\nsys.exit(1 if os.path.basename(os.getcwd()) == '_line' else 0)\n")
    set_line(project, f"setup = '\"{sys.executable}\" \"{script}\"'")
    set_line(project, "enough_after = 2")
    new_agent(project, "alice")
    for _again in range(3):
        code, out = praxis("land", "alice")
        assert code == 2, out
        assert "NOT landed: the landing line's own folder could not be made ready" in out, out
        assert "The setup command failed" in out and "ENOUGH" not in out, out
    assert not (project / "scripts" / "alice.gd").exists() and on_branch(project, "alice")
    script.write_text("")  # the setup is put right: it runs again, because it never finished there
    code, out = praxis("land", "alice")
    assert code == 0, out
    assert "Running the setup command" in out, out


# A check that rewrites a file git tracks, every time it runs.
REWRITES = """\
import sys
with open(sys.argv[1], "a") as f:
    f.write("run\\n")
with open("project.godot", "a") as f:
    f.write("; stamped by the check\\n")
"""


def test_a_check_that_rewrites_a_tracked_file_is_refused_in_the_lines_folder_as_in_the_agents(project, tmp_path):
    log = line_check(project, tmp_path, script=REWRITES)
    new_agent(project, "alice")
    code, out = praxis("land", "alice")
    assert code == 2, out
    assert "NOT landed: files changed while the check ran" in out and "project.godot" in out, out
    assert "the check itself" in out and 'check_may_change = ["project.godot"]' in out, out
    assert "stamped" not in (folder(project, "alice") / "project.godot").read_text()
    set_line(project, 'check_may_change = ["project.godot"]')
    code, out = praxis("land", "alice")
    assert code == 0, out
    assert "Put back files the check rewrote" in out and runs(log) == 2, out
    assert "stamped" not in (project / "project.godot").read_text()
    assert not (project / ".git" / "praxis" / "kept").exists(), "nobody edits there, so no copy is kept"


# --- main moves under the check, and the folder is in use -----------------------------------------------------------------


def _main_moves_during_the_check(project: Path, tmp_path: Path, then) -> tuple[int, str, Path]:
    gate = tmp_path / "go"
    log = line_check(project, tmp_path, gate=gate)
    new_agent(project, "alice")
    proc = praxis_process(project, "land", "alice")
    wait_for(lambda: runs(log) == 1, "the check to start")
    edit(project / "notes.txt", "committed straight to main, outside Praxis\n")
    git(project, "add", "notes.txt")
    git(project, "commit", "-q", "-m", "someone commits to main directly")
    then()
    gate.write_text("go")
    codes, outs = finish([proc])
    return codes[0], outs[0], log


def test_when_main_moves_during_the_check_the_work_is_checked_again_on_the_newer_main(project, tmp_path):
    code, out, log = _main_moves_during_the_check(project, tmp_path, lambda: None)
    assert code == 0, out
    assert "The main version changed while the check ran" in out and ran_in(log) == ["_line", "_line"], out
    assert (project / "scripts" / "alice.gd").exists() and (project / "notes.txt").exists()


def test_when_main_moves_and_the_agent_kept_working_nothing_it_did_since_is_taken_in(project, tmp_path):
    def keep_working():
        edit(folder(project, "alice") / "scripts" / "later.gd", "extends Node\n")
        commit(folder(project, "alice"), "alice: saved while the landing was checked")

    code, out, log = _main_moves_during_the_check(project, tmp_path, keep_working)
    assert code == 2, out
    assert "NOT landed: the main version changed during the check" in out, out
    assert "praxis land alice" in out and runs(log) == 1, out
    assert not (project / "scripts" / "alice.gd").exists() and not (project / "scripts" / "later.gd").exists()
    assert git(folder(project, "alice"), "log", "--format=%s", "-1") == "alice: saved while the landing was checked"
    assert landings(project, "alice") == []
    code, out = praxis("land", "alice")
    assert code == 0, out
    assert (project / "scripts" / "later.gd").exists()


def test_when_another_process_holds_the_lines_folder_the_check_runs_in_the_agents_folder(project, tmp_path):
    log = line_check(project, tmp_path)
    new_agent(project, "alice")
    store = Store(project / ".git" / "praxis")
    try:
        handle = fullcheck.take_lock(checkfolder.lock_path(store))
        assert handle is not None
        try:
            code, out = praxis("land", "alice")
        finally:
            handle.close()
    finally:
        store.close()
    assert code == 0, out
    assert "is in use by another process" in out and ran_in(log) == ["alice"], out
    assert own_folders(project) == []


def test_a_batch_whose_folder_cannot_be_made_ready_judges_nobody(project, tmp_path):
    line_check(project, tmp_path)
    script = tmp_path / "setup.py"
    script.write_text("import os, sys\nsys.exit(1 if os.path.basename(os.getcwd()) == '_line' else 0)\n")
    set_line(project, f"setup = '\"{sys.executable}\" \"{script}\"'")
    new_agent(project, "alice")
    new_agent(project, "bob")
    procs, release = land_behind_a_ghost(project, ["alice", "bob"])
    release()
    codes, outs = finish(procs)
    assert codes == [2, 2], outs
    for out in outs:
        assert "NOT landed: the landing line's own folder could not be made ready" in out, out
        assert "does not count against the agent's tries" in out and "ENOUGH" not in out, out
    assert on_branch(project, "alice") and on_branch(project, "bob")
    assert not (project / "scripts" / "alice.gd").exists() and not (project / "scripts" / "bob.gd").exists()
    script.write_text("")
    codes, outs = finish([praxis_process(project, "land", "alice"), praxis_process(project, "land", "bob")])
    assert codes == [0, 0], outs


def test_an_edit_while_a_later_group_of_the_batch_is_checked_changes_nothing(project, tmp_path):
    """The counterpart of the last test in test_stopped_landing: there the edit throws dave's check away."""
    script, log, gates = tmp_path / "numbered.py", tmp_path / "check-runs.log", tmp_path / "gates"
    script.write_text(NUMBERED_CHECK)
    gates.mkdir()
    set_line(project, f"check = '\"{sys.executable}\" \"{script}\" \"{log}\" \"{gates}\"'")
    set_line(project, "batch_max = 4")
    set_line(project, 'check_folder = "line"')
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
    assert "Landed: dave's work" in outs[3] and runs(log) == 4, "dave's check stands: it is not run again"
    assert "does not count" not in outs[0] and "NOT landed" not in outs[0], outs[0]
    assert "1 file(s) changed in alice's folder after this landing started" in outs[0], outs[0]
    assert "; edited" in (folder(project, "alice") / "project.godot").read_text()
    assert "; edited" not in (project / "project.godot").read_text()


def test_what_the_front_agent_is_told_when_its_own_result_already_stands(project):
    """Two things only the agent at the front of a batch can be told after its own work is settled."""
    said = []
    store = Store(project / ".git" / "praxis")
    try:
        ctx = types.SimpleNamespace(cfg=config.load(project), repo=types.SimpleNamespace(root=project), store=store,
                                    say=said.append)
        own = checkfolder.Own(ctx, {"name": "alice", "task": "t", "branch": "praxis/alice"}, None)
        text = own.does_not_count("alice", ["carol", "dave"], ["  - project.godot"])
        assert "The check of carol, dave rewrote files git tracks" in text and "  - project.godot" in text
        assert "alice's own result stands" in text and "check_may_change" in text
        assert checkfolder.refuse(ctx, {"name": "alice"}, checkfolder.NotReady("the setup failed"), settled=True) is False
        assert "the setup failed" in said[0] and "land on their own turn" in said[0] and "_line" in said[0]
        assert store.rows("SELECT * FROM landings") == [], "nothing is written against an agent whose result stands"
    finally:
        store.close()
