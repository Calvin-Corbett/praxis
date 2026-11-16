"""The stable launcher: one fixed per-user path that runs the newest installed Praxis."""

from __future__ import annotations

import json
import os
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest
from conftest import SRC, praxis

from agent_praxis import launcher, store


@pytest.fixture
def home(tmp_path, monkeypatch) -> Path:
    monkeypatch.setenv("PRAXIS_HOME", str(tmp_path / "praxis home"))
    monkeypatch.chdir(tmp_path)  # praxis commands run only inside pytest's temp area
    return tmp_path / "praxis home"


def _launcher_file(home: Path) -> Path:
    return home / "bin" / ("praxis.cmd" if os.name == "nt" else "praxis")


def _fake_python(folder: Path, word: str) -> Path:
    """A stand-in for an installed version's python that just says who it is and what it was asked."""
    folder.mkdir(parents=True, exist_ok=True)
    if os.name == "nt":
        path = folder / "python.cmd"
        path.write_text(f"@echo {word} %*\r\n@exit /b 7\r\n")
    else:
        path = folder / "python"
        path.write_text(f'#!/bin/sh\necho {word} "$@"\nexit 7\n')
        path.chmod(0o755)
    return path


def _run(home: Path, *args: str, **kw) -> subprocess.CompletedProcess:
    env = dict(os.environ, PYTHONPATH=str(SRC), PRAXIS_HOME=str(home))
    cmd = [str(_launcher_file(home)), *args]
    return subprocess.run(cmd, capture_output=True, text=True, env=env, **kw)


def _registry(home: Path, *items: tuple[str, Path]) -> None:
    home.mkdir(parents=True, exist_ok=True)
    (home / "installs.json").write_text(json.dumps([{"version": v, "python": str(p)} for v, p in items]))


def test_the_command_writes_the_launcher_at_the_fixed_per_user_path(home):
    code, out = praxis("launcher")
    assert code == 0, out
    assert _launcher_file(home).is_file() and str(_launcher_file(home)) in out
    assert (home / "bin" / "praxis").is_file()  # also the extensionless one, for Git Bash and Linux
    assert json.loads((home / "installs.json").read_text())[0]["python"] == sys.executable


def test_the_launcher_runs_this_praxis(home):
    praxis("launcher")
    done = _run(home, "--version")
    assert done.returncode == 0 and done.stdout.startswith("praxis "), done.stdout + done.stderr


def test_it_runs_the_newest_known_version_and_passes_arguments_and_exit_code_on(home, tmp_path):
    older = _fake_python(tmp_path / "old", "OLD")
    newer = _fake_python(tmp_path / "new", "NEW")
    _registry(home, ("0.9.0", older), ("9.10.0", newer), ("9.2.0", older))
    praxis("launcher")
    done = _run(home, "status", "two words")
    assert done.stdout.startswith("NEW -m agent_praxis status") and "two words" in done.stdout, done.stdout
    assert done.returncode == 7


def test_running_it_from_an_older_version_never_makes_the_launcher_older(home, tmp_path, monkeypatch):
    newer = _fake_python(tmp_path / "new", "NEW")
    _registry(home, ("99.0.0", newer))
    monkeypatch.setattr(launcher, "__version__", "0.1.0")
    praxis("launcher")
    assert _run(home).stdout.startswith("NEW")


def test_a_removed_version_is_skipped_and_the_next_newest_runs(home, tmp_path):
    kept = _fake_python(tmp_path / "kept", "KEPT")
    gone = _fake_python(tmp_path / "gone", "GONE")
    _registry(home, ("98.0.0", kept), ("99.0.0", gone))
    praxis("launcher")
    gone.unlink()  # that version is uninstalled later
    assert _run(home).stdout.startswith("KEPT")


def test_paths_with_spaces_work(home, tmp_path):
    fake = _fake_python(tmp_path / "a folder with spaces", "SPACED")
    _registry(home, ("99.0.0", fake))
    praxis("launcher")
    assert _run(home).stdout.startswith("SPACED")


def test_a_damaged_registry_is_ignored_and_rewritten(home):
    home.mkdir()
    (home / "installs.json").write_text("{not json")
    code, out = praxis("launcher")
    assert code == 0, out
    assert json.loads((home / "installs.json").read_text())[0]["python"] == sys.executable


def test_the_launcher_does_not_get_past_the_database_format_refusal(project, home):
    """An older Praxis, however it is started, still refuses a project a newer Praxis has used."""
    praxis("launcher")
    conn = sqlite3.connect(project / ".git" / "praxis" / "praxis.db")
    conn.execute(f"PRAGMA user_version = {store.FORMAT + 1}")
    conn.commit()
    conn.close()
    done = _run(home, "status", cwd=str(project))
    assert done.returncode == 1 and "newer Praxis" in done.stderr + done.stdout


def test_version_numbers_compare_as_numbers():
    assert launcher.version_key("0.10.0") > launcher.version_key("0.9.5") > launcher.version_key("0.4.0")
    assert launcher.version_key("1.0rc1") == (1, 0)
