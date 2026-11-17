"""Every command Praxis runs for the project is told where the project's main folder is: PRAXIS_ROOT.

A check that wants to run trusted code from the main folder, not the agent's own editable copy, cannot work the
folder out with git from inside the agent's folder (the agent can steer that). Praxis resolved the main folder itself
and sets PRAXIS_ROOT after it has copied the environment of whoever ran it, so nothing set beforehand counts.
"""

from __future__ import annotations

import dataclasses
import json
import sys
from pathlib import Path

import pytest
from conftest import edit, folder, git, praxis, praxis_process
from test_audit import audit_running
from test_batch import finish
from test_batch import new_agent as new_batch_agent
from test_core_hooks import _set
from test_early import early_running
from test_early import land_in_line as land_in_line_early
from test_inputs import set_line, use_checks, wait_for
from test_stopped_landing import commit

from agent_praxis import checks, hooks
from agent_praxis.agents import Ctx
from agent_praxis.config import load as load_config
from agent_praxis.gitops import Repo, find_repo
from agent_praxis.landing_queue import Ticket
from agent_praxis.store import Store

DECOY = "Z:/somebody/elses/folder"  # what is in the environment of whoever runs `praxis land`: it must never be seen

# A short fake check. Each run writes one line: the PRAXIS_ROOT it was given and the folder it runs in. It waits for
# a gate file named "all" when it is given a folder of gates, and fails when a script says *word*.
RECORD = """\
import json, os, pathlib, sys, time
log, word, gates = sys.argv[1], sys.argv[2], sys.argv[3]
told = {key: os.environ.get(key) for key in ("PRAXIS_ROOT", "PRAXIS_AGENT", "PRAXIS_TASK", "PRAXIS_CHECK",
                                             "PRAXIS_BASE", "PRAXIS_CHANGED_FILES")}
told["cwd"] = os.getcwd()
with open(log, "a", encoding="utf-8") as out:
    out.write(json.dumps(told) + "\\n")
if gates != "-":
    for _ in range(1200):
        if os.path.exists(os.path.join(gates, "all")):
            break
        time.sleep(0.05)
bad = [str(p) for p in pathlib.Path("scripts").glob("*.gd") if word in p.read_text()]
for name in bad:
    print("ERROR: broken script", name)
sys.exit(1 if bad else 0)
"""


@pytest.fixture(autouse=True)
def _a_root_in_the_environment_is_never_trusted(_each_test_alone, monkeypatch):
    monkeypatch.setenv("PRAXIS_ROOT", DECOY)


@pytest.fixture(autouse=True)
def _no_background_work_left_running(tmp_path):
    yield
    (tmp_path / "gates").mkdir(exist_ok=True)
    (tmp_path / "gates" / "all").write_text("")  # whatever still waits for a gate ends now
    project = tmp_path / "my game"
    if (project / ".git" / "praxis" / "praxis.db").exists():
        wait_for(lambda: not early_running(project) and not audit_running(project), "background work to end")


def command(tmp_path: Path, word: str = "BROKEN", gates: Path | None = None, name: str = "record.py") -> str:
    """The shell line of the fake check: it adds a line to told.jsonl in *tmp_path* on every run."""
    script = tmp_path / name
    script.write_text(RECORD)
    return f'"{sys.executable}" "{script.as_posix()}" "{(tmp_path / "told.jsonl").as_posix()}" {word} {gates or "-"}'


def record_with(project: Path, tmp_path: Path, setting: str = "check", **kw) -> None:
    _set(project, setting, f"'{command(tmp_path, **kw)}'")


def told(tmp_path: Path) -> list[dict]:
    log = tmp_path / "told.jsonl"
    return [json.loads(line) for line in log.read_text(encoding="utf-8").splitlines()] if log.exists() else []


def only_the_main_folder(seen: list[dict], project: Path) -> None:
    assert seen, "the check never ran"
    for one in seen:
        assert one["PRAXIS_ROOT"] and Path(one["PRAXIS_ROOT"]) == project.resolve(), one
        assert Path(one["cwd"]).resolve() != project.resolve(), f"the check ran in the main folder: {one}"


def land_behind_a_ghost(project: Path, names: list[str]):
    """`praxis land` for each name while a ghost holds the front of the line, so they wait together. Returns the
    processes and the function that lets the ghost go. (test_batch has one too; this waits longer, for a busy PC.)"""
    store = Store(project / ".git" / "praxis")
    ghost = Ticket(store, "ghost", say=lambda _t: None)
    ghost.__enter__()
    procs = []
    for name in names:
        procs.append(praxis_process(project, "land", name))
        wait_for(lambda n=name: store.rows("SELECT 1 FROM queue WHERE agent = ?", (n,)), f"{name} in the line", 400)

    def release() -> None:
        ghost.__exit__(None, None, None)
        store.close()

    return procs, release


def new_agent(project: Path, name: str, text: str = "extends Node\n") -> None:
    code, out = praxis("start", name, f"work of {name}", f"scripts/{name}.gd")
    assert code == 0, out
    edit(folder(project, name) / "scripts" / f"{name}.gd", text)


def land(name: str) -> str:
    code, out = praxis("land", name)
    assert code == 0, out
    return out


# --- a landing's check -------------------------------------------------------------------------------------------------


def test_a_landings_check_in_the_agents_folder_is_told_the_main_folder_not_what_was_in_the_environment(
    project, tmp_path
):
    record_with(project, tmp_path)
    new_agent(project, "alice")
    land("alice")
    (seen,) = told(tmp_path)
    only_the_main_folder([seen], project)
    assert Path(seen["cwd"]).resolve() == folder(project, "alice").resolve(), "it ran in the agent's own folder"
    assert seen["PRAXIS_ROOT"] != DECOY


def test_a_landings_check_in_the_lines_own_folder_is_told_the_main_folder_not_that_folder(project, tmp_path):
    record_with(project, tmp_path)
    set_line(project, 'check_folder = "line"')
    new_agent(project, "alice")
    land("alice")
    (seen,) = told(tmp_path)
    only_the_main_folder([seen], project)
    assert Path(seen["cwd"]).name == "_line", "it ran in the line's folder"


def test_what_praxis_toml_sets_in_env_cannot_change_it_either(project, tmp_path):
    record_with(project, tmp_path)
    _set(project, "env", '{ PRAXIS_ROOT = "Y:/in the config", praxis_root = "Y:/in lower case" }')
    new_agent(project, "alice")
    land("alice")
    only_the_main_folder(told(tmp_path), project)


def test_the_check_is_told_the_main_folder_whichever_folder_praxis_was_started_in(project, tmp_path, monkeypatch):
    """`praxis land` run from inside an agent's folder: the main folder is still the one Praxis found."""
    record_with(project, tmp_path)
    new_agent(project, "alice")
    monkeypatch.chdir(folder(project, "alice"))
    land("alice")
    only_the_main_folder(told(tmp_path), project)


# --- the other names Praxis sets ---------------------------------------------------------------------------------------


def test_the_names_praxis_sets_are_set_after_the_copy_so_nothing_set_beforehand_counts(project, tmp_path, monkeypatch):
    record_with(project, tmp_path)
    new_agent(project, "alice")
    before = git(project, "rev-parse", "main")
    for name, value in (("PRAXIS_AGENT", "mallory"), ("PRAXIS_TASK", "somebody else's task"), ("PRAXIS_CHECK", "0"),
                        ("PRAXIS_BASE", "0" * 40), ("PRAXIS_CHANGED_FILES", "Z:/not/the/list.txt")):
        monkeypatch.setenv(name, value)
    land("alice")
    (seen,) = told(tmp_path)
    assert seen["PRAXIS_AGENT"] == "alice" and seen["PRAXIS_TASK"] == "work of alice", seen
    assert seen["PRAXIS_CHECK"] == "1" and seen["PRAXIS_BASE"] == before, seen
    assert Path(seen["PRAXIS_CHANGED_FILES"]).name.endswith(".changed.txt"), seen


def test_the_full_check_is_told_the_root_but_not_the_changed_files(project, tmp_path):
    record_with(project, tmp_path, "full_check")
    code, out = praxis("fullcheck")
    assert code == 0, out
    only_the_main_folder(told(tmp_path), project)
    assert told(tmp_path)[0]["PRAXIS_BASE"] is None


# --- a batch ---------------------------------------------------------------------------------------------------------


@pytest.mark.parametrize("where", ["agent", "line"])
def test_a_batchs_combined_check_and_every_step_that_finds_the_culprit_are_told_the_main_folder(
    project, tmp_path, where
):
    record_with(project, tmp_path)
    set_line(project, f'check_folder = "{where}"')
    new_batch_agent(project, "alice")
    new_batch_agent(project, "bob", "BROKEN\n")
    new_batch_agent(project, "carol")
    procs, release = land_behind_a_ghost(project, ["alice", "bob", "carol"])
    release()
    codes, outs = finish(procs)
    assert codes == [0, 2, 0], outs
    seen = told(tmp_path)
    assert len(seen) == 4, "the whole batch, alice alone, then bob alone and carol alone"
    only_the_main_folder(seen, project)
    assert {Path(one["cwd"]).name for one in seen} == ({"alice"} if where == "agent" else {"_line"}), seen


# --- praxis check, early checks, the full check, the audit ---------------------------------------------------------------


def test_praxis_check_runs_in_a_scratch_folder_and_is_told_the_main_folder(project, tmp_path):
    record_with(project, tmp_path)
    new_agent(project, "alice")
    commit(folder(project, "alice"), "work of alice")  # `praxis check` tests what is saved
    code, out = praxis("check", "alice")
    assert code == 0, out
    (seen,) = told(tmp_path)
    only_the_main_folder([seen], project)
    assert Path(seen["cwd"]).name.startswith("_check"), seen


def test_an_early_check_is_told_the_main_folder(project, tmp_path):
    gates = tmp_path / "gates"
    gates.mkdir()
    record_with(project, tmp_path, gates=gates)
    set_line(project, "speculate = true")
    set_line(project, "batch_max = 1")
    new_agent(project, "ann")
    new_agent(project, "bob")
    procs, release = land_in_line_early(project, ["ann", "bob"])
    release()
    wait_for(lambda: len(told(tmp_path)) == 2, "ann's check and bob's early check to be running at once")
    assert sorted(Path(one["cwd"]).name for one in told(tmp_path)) == ["_early", "ann"]
    (gates / "all").write_text("")
    outs = [p.communicate(timeout=180)[0] for p in procs]
    assert [p.returncode for p in procs] == [0, 0], outs
    only_the_main_folder(told(tmp_path), project)


def test_the_full_checks_scratch_folder_setup_and_the_search_for_the_culprit_are_told_the_main_folder(
    project, tmp_path
):
    setup_log = tmp_path / "setup.jsonl"
    setup = tmp_path / "setup_record.py"
    setup.write_text(
        "import os, sys\nopen(sys.argv[1], 'a').write(os.environ.get('PRAXIS_ROOT', '') + '\\n')\n"
    )
    _set(project, "setup", f"'\"{sys.executable}\" \"{setup.as_posix()}\" \"{setup_log.as_posix()}\"'")
    record_with(project, tmp_path, "full_check", word="SLOW_BREAK")
    assert praxis("fullcheck")[0] == 0  # the last pass the search starts from
    for name, text in (("ann", "extends Node\n"), ("bob", "extends Node\n# SLOW_BREAK\n"), ("cat", "extends Node\n")):
        new_agent(project, name, text)
        land(name)
    before = len(told(tmp_path))
    code, out = praxis("fullcheck")
    assert code == 2 and "starts to fail at bob's landing" in out, out
    seen = told(tmp_path)
    assert len(seen) - before >= 4, "the newest version twice, then the search"
    only_the_main_folder(seen, project)
    assert {Path(one["cwd"]).name for one in seen} == {"_fullcheck"}
    roots = setup_log.read_text(encoding="utf-8").splitlines()
    assert roots and all(Path(root) == project.resolve() for root in roots), roots


def test_an_audit_of_a_remembered_pass_is_told_the_main_folder(project, tmp_path):
    script = tmp_path / "named.py"
    script.write_text(RECORD)
    run = f'"{sys.executable}" "{script.as_posix()}" "{(tmp_path / "told.jsonl").as_posix()}" BROKEN -'
    use_checks(project, '{ name = "core", paths = ["**"], inputs = ["scripts/player.gd"], run = \'' + run + "' }",
               audit_every="1")
    new_agent(project, "alice")
    land("alice")  # the check runs
    new_agent(project, "bob")
    out = land("bob")  # the pass is taken from memory and audited in the background
    assert "Skipping check core: " in out and "The audit (audit_every = 1)" in out, out
    wait_for(lambda: len(told(tmp_path)) == 2 and not audit_running(project), "the audit to end")
    seen = told(tmp_path)
    only_the_main_folder(seen, project)
    assert [Path(one["cwd"]).name for one in seen] == ["alice", "_audit"]


# --- named checks, and the commands that are not checks ---------------------------------------------------------------


def test_every_named_check_that_runs_beside_another_is_told_the_main_folder(project, tmp_path):
    script = tmp_path / "named.py"
    script.write_text(RECORD)
    log = (tmp_path / "told.jsonl").as_posix()
    entries = [
        '{ name = "one", paths = ["**"], inputs = ["scripts/player.gd"], run = \'"' + sys.executable
        + '" "' + script.as_posix() + '" "' + log + '" BROKEN -\' }',
        '{ name = "two", paths = ["**"], inputs = ["scripts/enemy.gd"], run = \'"' + sys.executable
        + '" "' + script.as_posix() + '" "' + log + '" BROKEN -\' }',
    ]
    use_checks(project, *entries, checks_together="true")
    new_agent(project, "alice")
    land("alice")
    seen = told(tmp_path)
    assert len(seen) == 2
    only_the_main_folder(seen, project)


def test_setup_after_land_after_done_and_a_heavy_job_are_told_it_too(project, tmp_path):
    script = tmp_path / "any.py"
    script.write_text("import os, sys\nopen(sys.argv[1], 'a').write(sys.argv[2] + ' ' + os.environ['PRAXIS_ROOT'] + '\\n')\n")
    log = tmp_path / "any.log"
    for key in ("setup", "after_land", "after_done"):
        _set(project, key, f"'\"{sys.executable}\" \"{script.as_posix()}\" \"{log.as_posix()}\" {key}'")
    new_agent(project, "alice")
    land("alice")
    code, out = praxis("done", "alice")
    assert code == 0, out
    code, out = praxis("run", "--heavy", sys.executable, str(script), str(log), "heavy")
    assert code == 0, out
    lines = log.read_text(encoding="utf-8").splitlines()
    assert [line.split(" ", 1)[0] for line in lines] == ["setup", "after_land", "after_done", "heavy"], lines
    assert all(Path(line.split(" ", 1)[1]) == project.resolve() for line in lines), lines


# --- what a check remembers --------------------------------------------------------------------------------------------


def _ctx(project: Path, root: Path) -> Ctx:
    """The project's context, as if its main folder were at *root* (the folder is not moved: only what is told)."""
    repo = find_repo(project)
    cfg = load_config(repo.root)
    store = Store(repo.state_dir)
    return Ctx(repo=Repo(root=root, common=repo.common), cfg=cfg, store=store, say=lambda _t: None)


def test_a_pass_remembered_with_one_root_is_reused_with_another(project, tmp_path):
    """Moving the project folder must not discard what was remembered: the root is in no key unless listed."""
    use_checks(
        project,
        '{ name = "core", paths = ["**"], inputs = ["scripts/player.gd"], run = "python -c pass" }',
        '{ name = "listed", paths = ["**"], inputs = ["scripts/player.gd"], env_inputs = ["PRAXIS_ROOT"], '
        'run = "python -c pass" }',
    )
    new_agent(project, "alice")
    commit = git(project, "rev-parse", "main")
    here = _ctx(project, project)
    there = _ctx(project, tmp_path / "somewhere else")
    try:
        plan = checks.plan(here.cfg, ["scripts/player.gd"])
        core, listed = (c for c in plan if c.name in ("core", "listed"))
        keys_here = checks.Keys(project, commit, hooks.command_env(here.cfg, here.repo.root))
        keys_there = checks.Keys(project, commit, hooks.command_env(there.cfg, there.repo.root))
        assert keys_here.of(core) == keys_there.of(core), "not in the key unless the project lists it"
        assert keys_here.of(listed) != keys_there.of(listed), "a project that lists PRAXIS_ROOT gets what it asked for"
        checks.remember(here.store, core, keys_here)
        assert checks.passed_at(there.store, core, keys_there) is not None, "remembered with one root, found with another"
        assert checks.look(there, project, commit, [core]).ok, "the whole way through look(), as a landing asks"
        checks.remember(here.store, listed, keys_here)
        assert checks.passed_at(there.store, listed, keys_there) is None
    finally:
        here.store.close()
        there.store.close()


def test_the_root_a_key_is_made_with_is_never_the_one_in_the_environment(project, monkeypatch):
    use_checks(
        project, '{ name = "listed", paths = ["**"], inputs = ["scripts/player.gd"], env_inputs = ["PRAXIS_ROOT"], '
        'run = "python -c pass" }',
    )
    ctx = _ctx(project, project)
    try:
        (check,) = (c for c in checks.plan(ctx.cfg, ["scripts/player.gd"]) if c.name == "listed")
        commit = git(project, "rev-parse", "main")
        key = checks.Keys(project, commit, hooks.command_env(ctx.cfg, ctx.repo.root)).of(check)
        monkeypatch.setenv("PRAXIS_ROOT", "Z:/another decoy")
        assert checks.Keys(project, commit, hooks.command_env(ctx.cfg, ctx.repo.root)).of(check) == key
    finally:
        ctx.store.close()


def test_command_env_sets_it_last(project):
    ctx = _ctx(project, project)
    try:
        cfg = dataclasses.replace(ctx.cfg, env={"PRAXIS_ROOT": "Y:/config", "praxis_root": "Y:/lower"})
        env = hooks.command_env(cfg, ctx.repo.root, PRAXIS_ROOT="Y:/a caller", PRAXIS_AGENT="a")
        assert Path(env["PRAXIS_ROOT"]) == project.resolve() and env["PRAXIS_AGENT"] == "a"
        assert [key for key in env if key.upper() == "PRAXIS_ROOT" and sys.platform == "win32"] in ([], ["PRAXIS_ROOT"])
    finally:
        ctx.store.close()


def test_a_project_that_never_reads_it_sees_nothing_else_change(project, tmp_path):
    """The record of what a check was told, without PRAXIS_ROOT, is the same as before it existed."""
    record_with(project, tmp_path)
    new_agent(project, "alice")
    land("alice")
    (seen,) = told(tmp_path)
    assert {key for key, value in seen.items() if value is not None} == {
        "PRAXIS_ROOT", "PRAXIS_AGENT", "PRAXIS_TASK", "PRAXIS_CHECK", "PRAXIS_BASE", "PRAXIS_CHANGED_FILES", "cwd"
    }
