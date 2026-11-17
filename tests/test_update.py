"""`praxis update`: the launcher is switched to this Praxis when that takes nothing from the commands at work.

The older installs are the fake Pythons of test_launcher.py, and the older commands the fakes of test_safeswitch.py.
"""

from __future__ import annotations

import json
import os
import sqlite3
import sys
import threading
import time
from pathlib import Path

import pytest
from conftest import praxis
from test_formats import _a_step_later
from test_launcher import _fake_python, _launcher_from, _registry
from test_safeswitch import downgrade, let_go, numbers, older_registered, state

from agent_praxis import launcher, store, update


@pytest.fixture
def home() -> Path:
    return Path(os.environ["PRAXIS_HOME"])  # conftest points it inside the test's own folder


@pytest.fixture
def older_launcher(home, tmp_path, monkeypatch) -> Path:
    """A launcher that runs an older installed Praxis. Returns that Praxis's (fake) Python."""
    older = _fake_python(tmp_path / "older install", "OLDER")
    _launcher_from(monkeypatch, older, "0.3.0")
    assert launcher._same(launcher.runs_now(home), str(older))
    return older


@pytest.fixture
def used(project) -> Path:
    """A project a Praxis command has run in: it has its database, and Praxis knows of it."""
    code, out = praxis("status")
    assert code == 0, out
    return project


def _notes(home: Path) -> list[dict]:
    return [json.loads(path.read_text(encoding="utf-8")) for path in sorted((home / "projects").glob("*.json"))]


def _runs_this_praxis(home: Path) -> bool:
    return launcher._same(launcher.runs_now(home) or "", sys.executable)


# --- which projects Praxis knows -------------------------------------------------------------------------------------


def test_a_command_in_a_project_leaves_one_note_of_it_in_the_per_user_folder(project, home):
    for _again in range(2):
        code, out = praxis("status")
        assert code == 0, out
    notes = _notes(home)
    assert len(notes) == 1 and os.path.samefile(notes[0]["root"], project)
    assert os.path.samefile(notes[0]["state"], state(project))
    assert update.known_projects(home) == [(notes[0]["root"], Path(notes[0]["state"]))]


def test_a_project_that_is_gone_is_forgotten(project, home):
    praxis("status")
    gone = home / "projects" / "0000000000000000.json"
    gone.write_text(
        json.dumps(
            {"root": str(project.parent / "deleted"), "state": str(project.parent / "deleted" / ".git" / "praxis")}
        )
    )
    (home / "projects" / "broken.json").write_text("{not json")
    assert len(update.known_projects(home)) == 1 and not gone.exists()


def test_a_note_that_cannot_be_written_stops_nothing(project, home, monkeypatch):
    (home / "projects").mkdir(parents=True, exist_ok=True)
    for note in (home / "projects").glob("*.json"):
        note.unlink()

    def refuse(*_args, **_kw):
        raise PermissionError("the folder cannot be written")

    monkeypatch.setattr(launcher, "_write_atomic", refuse)
    code, out = praxis("status")
    assert code == 0 and _notes(home) == [], out


# --- switching ---------------------------------------------------------------------------------------------------------


def test_the_first_update_makes_the_launcher_and_says_there_is_nothing_to_wait_for(home, tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)  # not in any project
    code, out = praxis("update")
    assert code == 0, out
    assert "Praxis knows of no project it is used in yet" in out and _runs_this_praxis(home)


def test_update_switches_at_once_when_this_praxis_only_adds_and_older_commands_carry_on(used, home, older_launcher):
    project = used
    downgrade(project, 6)  # what the older Praxis left
    lander = older_registered(project, "praxis land ghost", fmt=6, pid=4242)
    try:
        code, out = praxis("update")
        assert code == 0, out
        assert "safe now: this Praxis only adds to its database (format 6 to" in out
        assert "carry on beside this one: praxis land ghost (pid 4242, Praxis 0.3.0)" in out
        assert f"Switched: it ran {older_launcher} before." in out and _runs_this_praxis(home)
        assert numbers(project) == (6, 6), "looking at a project changes nothing in it"
    finally:
        let_go(lander)
    code, out = praxis("update")
    assert code == 0 and "The launcher runs this Praxis already." in out, out


def test_update_says_when_a_project_needs_nothing(used, home, older_launcher):
    code, out = praxis("update")
    assert code == 0 and f"safe now: its database (format {store.FORMAT}) needs nothing from this Praxis." in out, out
    assert _runs_this_praxis(home)


def test_update_waits_for_the_older_commands_a_breaking_step_would_cut_off(used, home, older_launcher, monkeypatch):
    project = used
    lander = older_registered(project, "praxis land ghost", fmt=store.FORMAT, pid=4242)
    _a_step_later(monkeypatch, store.BREAKING, "all_changed")
    try:
        code, out = praxis("update")
        assert code == 1, out
        assert "not yet. This Praxis changes its database in a way an older Praxis cannot use" in out
        assert "Waiting for: praxis land ghost (pid 4242, Praxis 0.3.0)" in out
        assert "Not switched: the launcher was left as it is, and nothing was changed." in out and "--when-safe" in out
        assert launcher._same(launcher.runs_now(home), str(older_launcher)), "the launcher still runs the older one"

        said: list[str] = []
        ended: dict = {}
        waiting = threading.Thread(
            target=lambda: ended.setdefault("code", update.run(said.append, when_safe=True, poll=0.05)), daemon=True
        )
        waiting.start()
        deadline = time.time() + 60
        while time.time() < deadline and not any("Waiting for those commands to finish" in line for line in said):
            time.sleep(0.05)
        time.sleep(0.5)
        assert waiting.is_alive() and "code" not in ended, "it waits, in the foreground, for as long as it takes"
        assert launcher._same(launcher.runs_now(home), str(older_launcher))
    finally:
        let_go(lander)
    waiting.join(timeout=120)
    assert ended.get("code") == 0, said
    assert any("no command of an older Praxis is running there" in line for line in said), said
    assert any(line.startswith("Switched: it ran") for line in said) and _runs_this_praxis(home)


def test_update_from_an_install_that_is_not_the_newest_only_registers_and_says_which_one_to_use(
    home, tmp_path, monkeypatch
):
    monkeypatch.chdir(tmp_path)
    newer = _fake_python(tmp_path / "newer install", "NEWER")
    _registry(home, ("99.0.0", newer))
    _launcher_from(monkeypatch, newer, "99.0.0")
    code, out = praxis("update")
    assert code == 1, out
    assert "Not switched" in out and "is newer than this one" in out
    assert f"{newer}" in out and "-m agent_praxis update" in out
    assert launcher._same(launcher.runs_now(home), str(newer)), "the launcher was left on the newer one"
    assert sys.executable in [item["python"] for item in json.loads((home / "installs.json").read_text())]


def test_a_project_only_a_newer_praxis_can_use_is_named_and_does_not_hold_the_switch(used, home, older_launcher):
    project = used
    conn = sqlite3.connect(state(project) / "praxis.db")
    conn.execute(f"PRAGMA user_version = {store.FORMAT + 1}")
    conn.commit()
    conn.close()
    code, out = praxis("update")
    assert code == 0, out
    assert (
        f"this Praxis cannot use it: a newer Praxis changed its database in a way only Praxis from format {store.FORMAT + 1} on can use"
        in out
    )
    assert _runs_this_praxis(home)
