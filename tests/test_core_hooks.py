"""Hooks a bigger project needs around Praxis: background landings, commands before and after, signed and marked
commits, branch names, a check that can say "not the agent's doing", races on main, the database format, shared
folders and the landing line in status."""

from __future__ import annotations

import json
import os
import re
import sqlite3
import sys
from pathlib import Path

import pytest
from conftest import edit, folder, git, praxis

from agent_praxis import gitops

RECORD_PY = """\
import json, os, sys
with open(sys.argv[1], "a", encoding="utf-8") as out:
    out.write(json.dumps({"cwd": os.getcwd(), **{k: os.environ.get(k) for k in sys.argv[2:]}}) + "\\n")
"""


def _set(project: Path, key: str, value: str) -> None:
    """Set one praxis.toml line (*value* is already written as TOML), replacing it when the file has it."""
    cfg = project / "praxis.toml"
    text, line = cfg.read_text(encoding="utf-8"), f"{key} = {value}"
    if re.search(rf"(?m)^{key} = ", text):
        text = re.sub(rf"(?m)^{key} = .*$", lambda _m: line, text)
    else:
        text += f"\n{line}\n"
    cfg.write_text(text, encoding="utf-8")


def _script(tmp_path: Path, name: str, body: str, *args: str) -> str:
    """A TOML literal string running a Python script kept outside the project."""
    path = tmp_path / name
    path.write_text(body, encoding="utf-8")
    words = " ".join(f'"{a}"' for a in args)
    return f"'\"{sys.executable}\" \"{path.as_posix()}\" {words}'"


def _records(path: Path) -> list[dict]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()] if path.exists() else []


def _exit_with(tmp_path: Path, code: int) -> str:
    return _script(tmp_path, f"exit{code}.py", f"import sys\nprint('the tool it needs is down')\nsys.exit({code})\n")


def _sh(path: Path, body: str) -> None:
    path.write_bytes(("#!/bin/sh\n" + body).encode())
    path.chmod(0o755)


# 1. Background landings -----------------------------------------------------------------------------------------

def test_a_background_landing_returns_at_once_and_wait_reports_how_it_ended(project, tmp_path):
    go = tmp_path / "go"
    _set(project, "check", _script(tmp_path, "waitgo.py", "import pathlib, sys, time\n"
                                   "while not pathlib.Path(sys.argv[1]).exists():\n    time.sleep(0.1)\n",
                                   go.as_posix()))
    praxis("start", "alice", "jump", "scripts/player.gd")
    edit(folder(project, "alice") / "scripts" / "player.gd", "extends Node\nvar speed = 12\n")
    code, out = praxis("land", "alice", "--background")
    assert code == 0 and "praxis wait alice" in out, out
    from agent_praxis import background
    _code, status = praxis("status")
    assert "landing now" in status, status  # still running: its check waits for the go file
    assert "speed = 12" not in (project / "scripts" / "player.gd").read_text()
    go.write_text("go")
    code, out = praxis("wait", "alice")
    assert code == 0 and "Landed" in out and "exit code 0" in out, out
    assert "speed = 12" in (project / "scripts" / "player.gd").read_text()
    again = praxis("wait", "alice")  # repeatable: the same result
    assert again[0] == 0 and "Landed" in again[1], again
    assert background.paths  # the land log is the agent's own
    assert (project / ".git" / "praxis" / "background" / "alice.log").is_file()


def test_wait_with_no_background_landing_reports_the_last_landing(project):
    praxis("start", "alice", "jump", "scripts/player.gd")
    code, out = praxis("wait", "alice")
    assert code == 1 and "nothing to wait for" in out, out
    edit(folder(project, "alice") / "scripts" / "player.gd", "extends Node\nvar speed = 12\n")
    assert praxis("land", "alice")[0] == 0
    code, out = praxis("wait", "alice")
    assert code == 0 and "last landing" in out and "landed" in out, out


@pytest.mark.skipif(os.name != "nt", reason="Windows process flags")
def test_the_background_landing_leaves_the_callers_job_or_says_it_could_not(monkeypatch):
    from agent_praxis import background
    calls = []

    def fake_popen(args, creationflags=0, **kw):
        calls.append(creationflags)
        if creationflags & background._BREAKAWAY:
            raise PermissionError(5, "Access is denied")
        return "started"

    monkeypatch.setattr(background.subprocess, "Popen", fake_popen)
    proc, independent = background._spawn(["x"], None)
    assert proc == "started" and independent is False
    assert calls[0] & background._BREAKAWAY and calls[0] & background._NO_WINDOW
    assert not calls[1] & background._BREAKAWAY and calls[1] & background._NO_WINDOW
    assert not any(c & 0x8 for c in calls)  # never DETACHED_PROCESS: its children would open windows


# 2. after_land, 11. env ------------------------------------------------------------------------------------------

def test_after_land_runs_in_the_main_folder_with_the_landing_in_its_environment(project, tmp_path):
    rec = tmp_path / "after.jsonl"
    _set(project, "after_land", _script(tmp_path, "record.py", RECORD_PY, rec.as_posix(), "PRAXIS_AGENT",
                                        "PRAXIS_TASK", "PRAXIS_COMMIT", "MY_VAR"))
    _set(project, "env", '{ MY_VAR = "hello" }')
    praxis("start", "alice", "jump", "scripts/player.gd")
    edit(folder(project, "alice") / "scripts" / "player.gd", "extends Node\nvar speed = 12\n")
    code, out = praxis("land", "alice")
    assert code == 0, out
    (seen,) = _records(rec)
    assert seen["PRAXIS_AGENT"] == "alice" and seen["PRAXIS_TASK"] == "jump" and seen["MY_VAR"] == "hello"
    assert seen["PRAXIS_COMMIT"] == git(project, "rev-parse", "main")
    assert gitops.same_path(seen["cwd"], project)


def test_a_failing_after_land_is_a_warning_and_the_landing_stays(project, tmp_path):
    _set(project, "after_land", _exit_with(tmp_path, 1))
    praxis("start", "alice", "jump", "scripts/player.gd")
    edit(folder(project, "alice") / "scripts" / "player.gd", "extends Node\nvar speed = 12\n")
    code, out = praxis("land", "alice")
    assert code == 0 and "Warning: the after_land command failed" in out and "stays done" in out, out
    assert "speed = 12" in git(project, "show", "main:scripts/player.gd")


def test_env_reaches_the_check(project, tmp_path):
    _set(project, "check", _script(tmp_path, "needvar.py", "import os, sys\n"
                                   "sys.exit(0 if os.environ.get('MY_VAR') == 'hello' else 1)\n"))
    _set(project, "env", '{ MY_VAR = "hello" }')
    praxis("start", "alice", "jump", "scripts/player.gd")
    edit(folder(project, "alice") / "scripts" / "player.gd", "extends Node\nvar speed = 12\n")
    code, out = praxis("land", "alice")
    assert code == 0, out


def test_env_must_be_a_table_of_text(project):
    _set(project, "env", '{ MY_VAR = 3 }')
    code, out = praxis("status")
    assert code == 1 and "env must be" in out, out


# 3. A check that says "not the agent's doing" ---------------------------------------------------------------------

def test_check_exit_3_needs_the_human_and_never_pauses_the_agent(project, tmp_path):
    _set(project, "check", _exit_with(tmp_path, 3))
    praxis("start", "alice", "jump", "scripts/player.gd")
    edit(folder(project, "alice") / "scripts" / "player.gd", "extends Node\nvar speed = 12\n")
    for _ in range(4):
        code, out = praxis("land", "alice")
        assert code == 2 and "outside this agent's work" in out and "needs the human" in out, out
        assert "ENOUGH" not in out
    from agent_praxis.store import Store
    store = Store(project / ".git" / "praxis")
    try:
        assert store.agent("alice")["paused"] is None
        assert {r["kind"] for r in store.landings("alice")} == {"needs-human"}
    finally:
        store.close()


# 4. Signed joined commits, 7. trailers on the joined commit --------------------------------------------------------

def test_the_joined_commit_is_signed_when_the_project_signs_commits(project, tmp_path):
    praxis("start", "alice", "jump", "scripts")
    wt = folder(project, "alice")
    edit(wt / "scripts" / "player.gd", "extends Node\nvar speed = 11\n")
    git(wt, "commit", "-qam", "faster")
    fork = git(project, "rev-parse", "main")
    edit(project / "scripts" / "player.gd", "extends Node\nvar speed = 20\n")
    git(project, "commit", "-qam", "newer speed on main")
    edit(wt / "scripts" / "player.gd", "extends Node\nvar speed = 20\n")
    edit(wt / "scripts" / "jump.gd", "extends Node\nvar jump = 3\n")
    git(wt, "add", "-A")
    git(wt, "commit", "-qm", "take the newest speed, add jumping")
    signed = tmp_path / "signed.txt"
    fake = tmp_path / "fake-gpg.sh"
    _sh(fake, f'cat >> "{signed.as_posix()}"\necho "=====" >> "{signed.as_posix()}"\n'
              "printf '\\n[GNUPG:] SIG_CREATED D 1 8 00 0 0 FAKE\\n' >&2\n"
              "printf -- '-----BEGIN PGP SIGNATURE-----\\n\\nfake\\n-----END PGP SIGNATURE-----\\n'\n")
    git(project, "config", "gpg.format", "openpgp")  # this machine's own setting may be ssh
    git(project, "config", "gpg.program", fake.as_posix())
    git(project, "config", "commit.gpgsign", "true")
    _set(project, "commit_trailers", '["Agent: {agent}"]')
    code, out = praxis("land", "alice")
    assert code == 0 and "together into one commit" in out, out
    payloads = signed.read_text(encoding="utf-8").split("=====")
    assert any(f"parent {fork}" in p and "(by alice)" in p for p in payloads), payloads  # the commit-tree one
    assert "Agent: alice" in git(project, "log", "-1", "--format=%B", "main")


# 5. branch_prefix, and a branch Praxis did not make -----------------------------------------------------------------

def test_branch_prefix_names_the_agents_branches(project):
    _set(project, "branch_prefix", '"team/agents/"')
    code, out = praxis("start", "alice", "jump", "scripts/player.gd")
    assert code == 0, out
    assert git(project, "branch", "--list", "team/agents/alice")
    assert not git(project, "branch", "--list", "praxis/alice")
    edit(folder(project, "alice") / "scripts" / "player.gd", "extends Node\nvar speed = 12\n")
    assert praxis("land", "alice")[0] == 0
    assert praxis("done", "alice")[0] == 0
    assert not git(project, "branch", "--list", "team/agents/alice")


@pytest.mark.parametrize("prefix", ['"team"', '"a..b/"', '"has space/"', '".hidden/"', '"x.lock/"'])
def test_a_branch_prefix_git_cannot_use_is_refused(project, prefix):
    _set(project, "branch_prefix", prefix)
    code, out = praxis("status")
    assert code == 1 and "cannot start a git branch name" in out, out


def test_start_refuses_a_branch_praxis_did_not_make_unless_told_to_reuse_it(project):
    git(project, "branch", "praxis/alice")
    code, out = praxis("start", "alice", "jump", "scripts/player.gd")
    assert code == 1 and "praxis/alice" in out and "--reuse-branch" in out, out
    assert not folder(project, "alice").exists()
    code, out = praxis("status")
    assert "nobody" in out, out
    code, out = praxis("start", "alice", "jump", "scripts/player.gd", "--reuse-branch")
    assert code == 0 and "carries on from it" in out, out


def test_abandon_says_how_to_carry_on_with_reuse_branch(project):
    praxis("start", "alice", "jump", "scripts/player.gd")
    edit(folder(project, "alice") / "scripts" / "player.gd", "extends Node\nvar speed = 12\n")
    code, out = praxis("abandon", "alice")
    assert code == 0 and "--reuse-branch" in out, out
    saved = re.search(r"git branch -m (\S+) praxis/alice", out).group(1)
    git(project, "branch", "-m", saved, "praxis/alice")
    code, out = praxis("start", "alice", "jump", "scripts/player.gd", "--reuse-branch")
    assert code == 0, out
    assert "speed = 12" in (folder(project, "alice") / "scripts" / "player.gd").read_text()


# 6. The final fast-forward loses a race ----------------------------------------------------------------------------

def test_main_moving_just_before_the_fast_forward_goes_round_again(project, monkeypatch):
    praxis("start", "alice", "jump", "scripts/player.gd")
    edit(folder(project, "alice") / "scripts" / "player.gd", "extends Node\nvar speed = 12\n")
    real, fired = gitops.git, []

    def racing(args, cwd, check=True, stdin=None):
        if "--ff-only" in args and not fired:  # another writer commits to main at this very moment
            fired.append(True)
            edit(project / "notes.txt", "someone else\n")
            git(project, "add", "notes.txt")
            git(project, "commit", "-qm", "a commit outside Praxis")
        return real(args, cwd, check, stdin)

    monkeypatch.setattr(gitops, "git", racing)
    code, out = praxis("land", "alice")
    assert fired and code == 0 and "Doing it again" in out, out
    assert "speed = 12" in git(project, "show", "main:scripts/player.gd")
    assert git(project, "show", "main:notes.txt") == "someone else"


# 7. Trailers on the commits Praxis writes ------------------------------------------------------------------------------

def test_commit_trailers_mark_only_the_commits_praxis_writes(project):
    _set(project, "commit_trailers", '["Agent: {agent}", "Task: {task}"]')
    praxis("start", "alice", "jump higher", "scripts/player.gd")
    wt = folder(project, "alice")
    edit(wt / "scripts" / "player.gd", "extends Node\nvar speed = 11\n")
    git(wt, "commit", "-qam", "the agent's own commit")
    edit(wt / "scripts" / "player.gd", "extends Node\nvar speed = 12\n")  # left for Praxis to save
    assert praxis("land", "alice")[0] == 0
    saved, own = git(project, "log", "-2", "--format=%B%x00", "main").split("\0")[:2]
    assert "Agent: alice\nTask: jump higher" in saved, saved
    assert "Agent:" not in own, own
    praxis("start", "bob", "enemies", "scripts/enemy.gd")
    edit(folder(project, "bob") / "scripts" / "enemy.gd", "extends Node\nvar hp = 9\n")
    code, out = praxis("abandon", "bob")
    assert code == 0, out
    branch = re.search(r"kept on the branch (\S+?)\.", out).group(1)
    assert "Agent: bob\nTask: enemies" in git(project, "log", "-1", "--format=%B", branch)


def test_a_trailer_with_an_unknown_placeholder_is_refused(project):
    _set(project, "commit_trailers", '["Owner: {owner}"]')
    code, out = praxis("status")
    assert code == 1 and "can only use {agent} and {task}" in out, out


# 8. PRAXIS_AGENT for git hooks -------------------------------------------------------------------------------------

def test_git_hooks_see_which_agent_praxis_commits_for(project, tmp_path):
    seen = tmp_path / "hook.txt"
    _sh(project / ".git" / "hooks" / "pre-commit", f'echo "$PRAXIS_AGENT|$PRAXIS_TASK" >> "{seen.as_posix()}"\n')
    praxis("start", "alice", "jump", "scripts/player.gd")
    edit(folder(project, "alice") / "scripts" / "player.gd", "extends Node\nvar speed = 12\n")
    assert praxis("land", "alice")[0] == 0
    assert "alice|jump" in seen.read_text().splitlines()


# 9. The database format -------------------------------------------------------------------------------------------

def test_a_database_from_a_newer_praxis_is_refused(project):
    db = sqlite3.connect(str(project / ".git" / "praxis" / "praxis.db"))
    db.execute("PRAGMA user_version = 99")
    db.commit()
    db.close()
    code, out = praxis("status")
    assert code == 1 and "newer Praxis (format 99)" in out and "update praxis" in out.lower(), out


def test_an_older_database_is_upgraded_in_place(tmp_path):
    from agent_praxis import store as store_module
    db = sqlite3.connect(str(tmp_path / "praxis.db"))
    db.executescript(store_module.SCHEMA)  # what a Praxis before format numbers made
    db.execute("INSERT INTO agents (name, task, branch, worktree, started, last_seen) "
               "VALUES ('alice', 'jump', 'praxis/alice', 'x', 1, 1)")
    db.commit()
    db.close()
    store = store_module.Store(tmp_path)
    try:
        assert store.row("PRAGMA user_version")[0] == store_module.FORMAT == 7
        assert store.agent("alice")["task"] == "jump"
        assert store.rows("SELECT * FROM branches") == []
        assert store.rows("SELECT * FROM background") == []
        assert store.rows("SELECT * FROM heavy") == []
    finally:
        store.close()


# 12. setup ----------------------------------------------------------------------------------------------------------

def test_setup_runs_in_the_new_folder_and_can_be_run_again(project, tmp_path):
    rec = tmp_path / "setup.jsonl"
    _set(project, "setup", _script(tmp_path, "record.py", RECORD_PY, rec.as_posix(), "PRAXIS_AGENT", "PRAXIS_TASK",
                                   "MY_VAR"))
    _set(project, "env", '{ MY_VAR = "hello" }')
    code, out = praxis("start", "alice", "jump", "scripts/player.gd")
    assert code == 0 and "Setup finished" in out, out
    (seen,) = _records(rec)
    assert gitops.same_path(seen["cwd"], folder(project, "alice"))
    assert (seen["PRAXIS_AGENT"], seen["PRAXIS_TASK"], seen["MY_VAR"]) == ("alice", "jump", "hello")
    assert praxis("setup", "alice")[0] == 0
    assert len(_records(rec)) == 2


def test_a_failing_setup_keeps_the_folder_and_says_how_to_rerun_it(project, tmp_path):
    _set(project, "setup", _exit_with(tmp_path, 1))
    code, out = praxis("start", "alice", "jump", "scripts/player.gd")
    assert code == 2 and "setup command failed" in out and "praxis setup alice" in out, out
    assert "the tool it needs is down" in out
    assert (folder(project, "alice") / "scripts" / "player.gd").is_file()
    _set(project, "setup", '""')
    code, out = praxis("setup", "alice")
    assert code == 0 and "nothing to run" in out, out


# 13. share ----------------------------------------------------------------------------------------------------------

def _shared_packages(project: Path) -> Path:
    edit(project / ".gitignore", "node_modules/\n")
    git(project, "add", ".gitignore")
    git(project, "commit", "-qm", "ignore packages")
    package = project / "node_modules" / "pkg" / "index.js"
    edit(package, "module.exports = 1\n")
    _set(project, "share", '["node_modules"]')
    return package


def test_a_shared_link_is_never_the_agents_work_and_never_lands(project):
    """`node_modules/` in .gitignore matches only folders; on Linux and macOS the link is a symbolic link, which git
    sees as a file. The link must still be ignored, so it is never saved as the agent's work or landed on main."""
    _shared_packages(project)
    assert praxis("start", "alice", "jump", "scripts/player.gd")[0] == 0
    agent = folder(project, "alice")
    assert git(agent, "status", "--porcelain").strip() == ""
    edit(agent / "scripts" / "player.gd", "extends Node\nvar speed = 12\n")
    assert praxis("land", "alice")[0] == 0
    assert git(project, "ls-files", "node_modules").strip() == ""
    assert praxis("done", "alice")[0] == 0


def test_shared_folders_are_linked_and_survive_done_and_abandon(project):
    from agent_praxis import agents
    package = _shared_packages(project)
    code, out = praxis("start", "alice", "jump", "scripts/player.gd")
    assert code == 0 and "Shared from the main folder" in out, out
    link = folder(project, "alice") / "node_modules"
    assert agents._is_link(str(link)) and (link / "pkg" / "index.js").read_text() == "module.exports = 1\n"
    edit(folder(project, "alice") / "scripts" / "player.gd", "extends Node\nvar speed = 12\n")
    assert praxis("land", "alice")[0] == 0
    code, out = praxis("done", "alice")
    assert code == 0, out
    assert package.read_text() == "module.exports = 1\n"
    praxis("start", "bob", "enemies", "scripts/enemy.gd")
    edit(folder(project, "bob") / "scripts" / "enemy.gd", "extends Node\nvar hp = 9\n")
    code, out = praxis("abandon", "bob")
    assert code == 0, out
    assert package.read_text() == "module.exports = 1\n"
    assert not folder(project, "bob").exists()


def test_a_share_that_is_tracked_not_ignored_or_missing_is_skipped(project):
    _shared_packages(project)
    edit(project / "loose" / "a.txt", "not ignored\n")
    _set(project, "share", '["node_modules", "scripts", "loose", "missing"]')
    code, out = praxis("start", "alice", "jump", "scripts/player.gd")
    assert code == 0, out
    for name in ("scripts", "loose", "missing"):
        assert f"did not share {name}" in out, out
    assert (folder(project, "alice") / "node_modules" / "pkg").is_dir()
    assert not (folder(project, "alice") / "loose").exists()


# Status: places in the landing line ----------------------------------------------------------------------------------

def test_status_shows_each_waiting_agents_place_in_the_landing_line(project):
    from agent_praxis.landing_queue import _try_lock, _unlock
    from agent_praxis.store import Store
    praxis("start", "alice", "jump", "scripts/player.gd")
    praxis("start", "bob", "enemies", "scripts/enemy.gd")
    store = Store(project / ".git" / "praxis")
    queue = store.dir / "queue"
    queue.mkdir(exist_ok=True)
    handles = []
    try:
        for agent, token, started in (("alice", "t1", 1.0), ("bob", "t2", None)):
            handle = open(queue / f"{token}.lock", "a+b")  # noqa: SIM115
            assert _try_lock(handle)
            handles.append(handle)
            with store.write() as db:
                db.execute("INSERT INTO queue (agent, token, enqueued, started) VALUES (?, ?, 1, ?)",
                           (agent, token, started))
        _code, out = praxis("status")
        assert "landing: landing now" in out and "place 2 (1 ahead)" in out, out
    finally:
        for handle in handles:
            _unlock(handle)
            handle.close()
        store.close()
