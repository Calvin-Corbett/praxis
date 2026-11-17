"""A landing's check is told what the landing changes: PRAXIS_BASE is the main version the work sits on, and
PRAXIS_CHANGED_FILES names a text file listing the changed files. A check can then test only what a change can
affect. The full check, which tests everything, is told neither."""

from __future__ import annotations

import os
from pathlib import Path

from conftest import edit, folder, git, praxis
from test_batch import finish, land_behind_a_ghost, new_agent
from test_core_hooks import _records, _script, _set

from agent_praxis import landing

# A check that writes down what Praxis told it, then passes.
RECORD_CHANGES = """\
import json, os, sys
listed = os.environ.get("PRAXIS_CHANGED_FILES")
seen = {"base": os.environ.get("PRAXIS_BASE"), "listed": listed, "files": None, "raw": None}
if listed:
    with open(listed, "rb") as handle:
        seen["raw"] = handle.read().decode("utf-8")
    seen["files"] = seen["raw"].splitlines()
with open(sys.argv[1], "a", encoding="utf-8") as out:
    out.write(json.dumps(seen) + "\\n")
"""


def _recording_check(project: Path, tmp_path: Path, setting: str = "check") -> Path:
    rec = tmp_path / "told.jsonl"
    _set(project, setting, _script(tmp_path, "record_changes.py", RECORD_CHANGES, rec.as_posix()))
    return rec


def test_the_check_is_told_the_main_commit_and_the_files_the_landing_changes(project, tmp_path):
    rec = _recording_check(project, tmp_path)
    assert praxis("start", "alice", "jump", "scripts", "docs")[0] == 0
    wt = folder(project, "alice")
    edit(wt / "scripts" / "player.gd", "extends Node\nvar speed = 99\n")
    edit(wt / "docs" / "my notes.txt", "a new file, with a space in its name\n")
    (wt / "scripts" / "enemy.gd").unlink()
    before = git(project, "rev-parse", "main")
    code, out = praxis("land", "alice")
    assert code == 0, out
    (seen,) = _records(rec)
    assert seen["base"] == before, "the main version the work was put on top of"
    assert seen["files"] == ["docs/my notes.txt", "scripts/enemy.gd", "scripts/player.gd"], "a deleted file counts"
    assert seen["raw"].endswith("player.gd\n") and "\r" not in seen["raw"], "one name per line, the same everywhere"
    listed = Path(seen["listed"])
    assert listed.parent == project / ".git" / "praxis" / "logs" and listed.name.endswith(landing.CHANGED_LIST)
    assert listed.with_name(listed.name[: -len(landing.CHANGED_LIST)] + ".log").exists(), "next to the check's log"


def test_the_list_holds_only_this_landing_when_main_has_moved_on(project, tmp_path):
    rec = _recording_check(project, tmp_path)
    assert praxis("start", "alice", "jump", "scripts/player.gd")[0] == 0
    assert praxis("start", "bob", "enemies", "scripts/enemy.gd")[0] == 0
    edit(folder(project, "alice") / "scripts" / "player.gd", "extends Node\nvar speed = 99\n")
    edit(folder(project, "bob") / "scripts" / "enemy.gd", "extends Node\nvar hp = 9\n")
    assert praxis("land", "bob")[0] == 0
    after_bob = git(project, "rev-parse", "main")
    code, out = praxis("land", "alice")
    assert code == 0, out
    bobs, alices = _records(rec)
    assert bobs["files"] == ["scripts/enemy.gd"]
    assert alices["base"] == after_bob and alices["files"] == ["scripts/player.gd"], "bob's file is already in main"


def test_a_batch_is_checked_once_and_told_what_all_its_agents_change(project, tmp_path):
    rec = _recording_check(project, tmp_path)
    new_agent(project, "alice")
    new_agent(project, "bob")
    before = git(project, "rev-parse", "main")
    procs, release = land_behind_a_ghost(project, ["alice", "bob"])
    release()
    codes, outs = finish(procs)
    assert codes == [0, 0], outs
    (seen,) = _records(rec)
    assert seen["base"] == before and seen["files"] == ["scripts/alice.gd", "scripts/bob.gd"]


def test_the_full_check_is_told_neither_even_when_praxis_itself_was_started_with_them(project, tmp_path, monkeypatch):
    rec = _recording_check(project, tmp_path, "full_check")
    stale = tmp_path / "stale.txt"
    stale.write_text("scripts/player.gd\n")
    monkeypatch.setenv("PRAXIS_BASE", git(project, "rev-parse", "main"))
    monkeypatch.setenv("PRAXIS_CHANGED_FILES", str(stale))
    code, out = praxis("fullcheck")
    assert code == 0, out
    (seen,) = _records(rec)
    assert seen["base"] is None and seen["listed"] is None, "the full check tests everything: nothing narrows it"


def test_a_list_of_changed_files_goes_when_its_log_goes(tmp_path, monkeypatch):
    logs = tmp_path / "logs"
    logs.mkdir()
    for i in range(4):
        (logs / f"a-{i}.log").write_text("x")
        os.utime(logs / f"a-{i}.log", (1000 + i, 1000 + i))
        (logs / f"a-{i}{landing.CHANGED_LIST}").write_text("scripts/player.gd\n")
    monkeypatch.setattr(landing, "KEEP_LOGS", 2)
    landing._prune_logs(logs, keep=logs / "a-3.log")
    assert sorted(p.name for p in logs.iterdir()) == ["a-2.changed.txt", "a-2.log", "a-3.changed.txt", "a-3.log"]
