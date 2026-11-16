"""after_done: a command run in the main folder after `praxis done`; its failure is only a warning."""

from __future__ import annotations

from pathlib import Path

from conftest import edit, folder, git, praxis
from test_core_hooks import RECORD_PY, _exit_with, _records, _script, _set

from agent_praxis import gitops


def _landed(project: Path, name: str = "alice") -> None:
    praxis("start", name, "jump", "scripts/player.gd")
    edit(folder(project, name) / "scripts" / "player.gd", "extends Node\nvar speed = 12\n")
    code, out = praxis("land", name)
    assert code == 0, out


def test_after_done_runs_in_the_main_folder_once_the_agent_is_done(project, tmp_path):
    rec = tmp_path / "done.jsonl"
    _set(
        project, "after_done", _script(tmp_path, "record.py", RECORD_PY, rec.as_posix(), "PRAXIS_AGENT", "PRAXIS_TASK")
    )
    _landed(project)
    assert not _records(rec)  # landing is not done
    code, out = praxis("done", "alice")
    assert code == 0 and "Ran the after_done command" in out, out
    (seen,) = _records(rec)
    assert seen["PRAXIS_AGENT"] == "alice" and seen["PRAXIS_TASK"] == "jump"
    assert gitops.same_path(seen["cwd"], project)
    assert not folder(project, "alice").exists()


def test_a_failing_after_done_is_only_a_warning(project, tmp_path):
    _set(project, "after_done", _exit_with(tmp_path, 3))
    _landed(project)
    code, out = praxis("done", "alice")
    assert code == 0 and "Warning: the after_done command failed (exit code 3)" in out and "stays done" in out, out
    assert "WORKING NOW: nobody" in praxis("status")[1]


def test_a_command_that_cannot_start_is_only_a_warning(project):
    _set(project, "after_done", "'nothing-like-this-exists-anywhere'")
    _landed(project)
    code, out = praxis("done", "alice")
    assert code == 0 and "Warning: the after_done command" in out, out


def test_after_done_does_not_run_when_done_is_refused(project, tmp_path):
    rec = tmp_path / "done.jsonl"
    _set(project, "after_done", _script(tmp_path, "record.py", RECORD_PY, rec.as_posix(), "PRAXIS_AGENT"))
    praxis("start", "alice", "jump", "scripts/player.gd")
    edit(folder(project, "alice") / "scripts" / "player.gd", "extends Node\nvar speed = 99\n")  # not landed
    code, out = praxis("done", "alice")
    assert code == 1, out
    assert not _records(rec)


def test_after_done_runs_after_land_done_too(project, tmp_path):
    rec = tmp_path / "done.jsonl"
    _set(project, "after_done", _script(tmp_path, "record.py", RECORD_PY, rec.as_posix(), "PRAXIS_AGENT"))
    praxis("start", "alice", "jump", "scripts/player.gd")
    edit(folder(project, "alice") / "scripts" / "player.gd", "extends Node\nvar speed = 12\n")
    code, out = praxis("land", "alice", "--done")
    assert code == 0, out
    assert [r["PRAXIS_AGENT"] for r in _records(rec)] == ["alice"]
    assert "speed = 12" in git(project, "show", "main:scripts/player.gd")


def test_the_setting_is_written_into_new_praxis_toml_files(project):
    assert "after_done" in (project / "praxis.toml").read_text(encoding="utf-8")
