"""The stable launcher: one fixed per-user path that runs an installed Praxis, and never switches by itself."""

from __future__ import annotations

import json
import os
import sqlite3
import subprocess
import sys
import time
from pathlib import Path

import pytest
from conftest import child_env, praxis

from agent_praxis import launcher, store


@pytest.fixture
def home(tmp_path, monkeypatch) -> Path:
    monkeypatch.chdir(tmp_path)  # praxis commands run only inside the test's own temporary folder
    return Path(os.environ["PRAXIS_HOME"])  # conftest points it inside tmp_path for every test


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
    cmd = [str(_launcher_file(home)), *args]
    return subprocess.run(cmd, check=False, capture_output=True, text=True, env=child_env(PRAXIS_HOME=str(home)), **kw)


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


def _launcher_from(monkeypatch, python: Path, version: str, *args: str) -> str:
    """Run `praxis launcher` as the install at *python* (of *version*) would; returns what it printed."""
    with monkeypatch.context() as patch:
        patch.setattr(sys, "executable", str(python))
        patch.setattr(launcher, "__version__", version)
        code, out = praxis("launcher", *args)
    assert code == 0, out
    return out


def test_a_newly_installed_version_registers_but_does_not_take_the_launcher(home, tmp_path, monkeypatch):
    old = _fake_python(tmp_path / "old", "OLD")
    new = _fake_python(tmp_path / "new", "NEW")
    _launcher_from(monkeypatch, old, "0.4.0")
    assert _run(home).stdout.startswith("OLD")
    out = _launcher_from(monkeypatch, new, "0.5.0")  # the new version is installed and registers itself
    assert _run(home, "status").stdout.startswith("OLD -m agent_praxis status"), "the launcher was left as it was"
    assert f"It runs Praxis 0.4.0 ({old}) now, and was left on it." in out
    assert f"is Praxis 0.5.0 ({new})" in out
    command = next(line for line in out.splitlines() if line.endswith("-m agent_praxis launcher --switch"))
    assert command.strip().strip('"').startswith(str(new)), "the exact command to switch, run with the newest"
    assert [i["python"] for i in json.loads((home / "installs.json").read_text())] == [str(old), str(new)]


def test_switch_points_the_launcher_at_the_newest(home, tmp_path, monkeypatch):
    old = _fake_python(tmp_path / "old", "OLD")
    new = _fake_python(tmp_path / "new", "NEW")
    _launcher_from(monkeypatch, old, "0.4.0")
    _launcher_from(monkeypatch, new, "0.5.0")
    out = _launcher_from(monkeypatch, new, "0.5.0", "--switch")
    assert _run(home).stdout.startswith("NEW")
    assert f"Switched: it ran {old} before." in out and f"It runs Praxis 0.5.0 ({new}) now: the newest" in out
    assert "--switch" not in out.split("After installing")[0], "nothing left to switch to"
    out = _launcher_from(monkeypatch, old, "0.4.0", "--switch")  # asked from the older install: still the newest
    assert _run(home).stdout.startswith("NEW") and "Switched" not in out


def test_the_first_launcher_runs_the_praxis_that_made_it_even_when_a_newer_one_is_known(home, tmp_path, monkeypatch):
    here = _fake_python(tmp_path / "here", "HERE")
    newer = _fake_python(tmp_path / "newer", "NEWER")
    _registry(home, ("9.0.0", newer))
    out = _launcher_from(monkeypatch, here, "0.4.0")
    assert _run(home).stdout.startswith("HERE")
    assert str(newer) in out and "-m agent_praxis launcher --switch" in out


def test_when_the_kept_version_is_uninstalled_the_newest_one_left_runs(home, tmp_path, monkeypatch):
    kept = _fake_python(tmp_path / "kept", "KEPT")
    low = _fake_python(tmp_path / "low", "LOW")
    high = _fake_python(tmp_path / "high", "HIGH")
    _launcher_from(monkeypatch, kept, "0.4.0")
    _launcher_from(monkeypatch, low, "0.5.0")
    _launcher_from(monkeypatch, high, "0.6.0")
    assert _run(home).stdout.startswith("KEPT")
    kept.unlink()  # the version the launcher was left on is uninstalled
    assert _run(home).stdout.startswith("HIGH")
    out = _launcher_from(monkeypatch, low, "0.5.0")  # and the launcher now stays on what it runs
    assert _run(home).stdout.startswith("HIGH") and f"It runs Praxis 0.6.0 ({high}) now: the newest" in out


def test_a_launcher_program_the_list_of_installs_forgot_is_still_kept(home, tmp_path, monkeypatch):
    old = _fake_python(tmp_path / "old", "OLD")
    new = _fake_python(tmp_path / "new", "NEW")
    _launcher_from(monkeypatch, old, "0.4.0")
    (home / "installs.json").write_text("{not json")
    out = _launcher_from(monkeypatch, new, "0.5.0")
    assert _run(home).stdout.startswith("OLD")
    assert f"It runs {old} now, and was left on it." in out


def test_it_runs_the_newest_known_version_and_passes_arguments_and_exit_code_on(home, tmp_path):
    older = _fake_python(tmp_path / "old", "OLD")
    newer = _fake_python(tmp_path / "new", "NEW")
    _registry(home, ("0.9.0", older), ("9.10.0", newer), ("9.2.0", older))
    praxis("launcher", "--switch")
    done = _run(home, "status", "two words")
    assert done.stdout.startswith("NEW -m agent_praxis status") and "two words" in done.stdout, done.stdout
    assert done.returncode == 7


def test_running_it_from_an_older_version_never_makes_the_launcher_older(home, tmp_path, monkeypatch):
    newer = _fake_python(tmp_path / "new", "NEW")
    _launcher_from(monkeypatch, newer, "99.0.0")
    monkeypatch.setattr(launcher, "__version__", "0.1.0")
    praxis("launcher")
    assert _run(home).stdout.startswith("NEW")
    praxis("launcher", "--switch")
    assert _run(home).stdout.startswith("NEW")


def _order(home: Path) -> list[str]:
    return [i["python"] for i in launcher.known_installs(home, here_python=str(home / "none"), here_version="0.0.1")]


def test_equal_versions_the_one_you_run_it_from_goes_first(home, tmp_path, monkeypatch):
    first = _fake_python(tmp_path / "first", "FIRST")
    second = _fake_python(tmp_path / "second", "SECOND")
    _registry(home, ("0.4.0", first))
    monkeypatch.setattr(launcher, "__version__", "0.4.0")
    monkeypatch.setattr(sys, "executable", str(second))
    praxis("launcher")
    assert _run(home).stdout.startswith("SECOND")
    assert [i["python"] for i in json.loads((home / "installs.json").read_text())] == [str(first), str(second)]
    monkeypatch.setattr(sys, "executable", str(first))  # now run from the first install: it is the most recent
    praxis("launcher")
    assert _run(home).stdout.startswith("SECOND"), "registering alone never switches"
    praxis("launcher", "--switch")
    assert _run(home).stdout.startswith("FIRST")
    assert [i["python"] for i in json.loads((home / "installs.json").read_text())] == [str(second), str(first)]


def test_equal_versions_the_most_recently_registered_wins(home, tmp_path):
    a, b, c = (_fake_python(tmp_path / n, n) for n in "abc")
    _registry(home, ("0.4.0", a), ("0.4.0", b), ("0.4.0", c))
    assert _order(home) == [str(c), str(b), str(a)]


def test_a_lower_version_loses_to_a_higher_one_whatever_the_order(home, tmp_path):
    low = _fake_python(tmp_path / "low", "LOW")
    high = _fake_python(tmp_path / "high", "HIGH")
    _registry(home, ("0.10.0", high), ("0.9.0", low))  # the lower one registered last
    assert _order(home) == [str(high), str(low)]
    _registry(home, ("0.9.0", low), ("0.10.0", high))
    assert _order(home) == [str(high), str(low)]


def test_a_removed_version_is_skipped_and_the_next_newest_runs(home, tmp_path):
    kept = _fake_python(tmp_path / "kept", "KEPT")
    gone = _fake_python(tmp_path / "gone", "GONE")
    _registry(home, ("98.0.0", kept), ("99.0.0", gone))
    praxis("launcher", "--switch")
    gone.unlink()  # that version is uninstalled later
    assert _run(home).stdout.startswith("KEPT")


def test_paths_with_spaces_work(home, tmp_path):
    fake = _fake_python(tmp_path / "a folder with spaces", "SPACED")
    _registry(home, ("99.0.0", fake))
    out = praxis("launcher")[1]
    assert f'"{fake}" -m agent_praxis launcher --switch' in out, "a path with a space is quoted in the command"
    praxis("launcher", "--switch")
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


def _windows_launcher_of_an_older_praxis(pythons: list[str]) -> bytes:
    """praxis.cmd as every Praxis wrote it before the exit code moved onto the line that runs Praxis."""
    lines = ["@echo off", f"rem {launcher.ABOUT}"]
    for n, py in enumerate(pythons, 1):
        lines += [f'if not exist "{py}" goto next{n}', f'"{py}" -m agent_praxis %*', "exit /b %errorlevel%", f":next{n}"]
    lines += ["echo Praxis: no installed version was found. 1>&2", "exit /b 1"]
    return ("\r\n".join(lines) + "\r\n").encode()


def test_a_rewritten_windows_launcher_has_the_line_a_running_one_reads_on_at():
    """cmd.exe reads a batch file again when a command in it ends, from the byte it had reached. A launcher that is
    running a Praxis command while `praxis update` rewrites it must find, at that byte, the line it was going to
    read: end with the command's exit code."""
    old = _windows_launcher_of_an_older_praxis([r"C:\old\python.exe", r"C:\older\python.exe"])
    at = launcher._read_on_at(old)
    assert len(at) == 2 and all(old[n:].startswith(b"exit /b %errorlevel%\r\n") for n in at)
    assert all(old[:n].endswith(b" -m agent_praxis %*\r\n") for n in at), "right after the lines that run Praxis"

    newer = [r"C:\a folder with a much longer name than before\new\python.exe", r"C:\old\python.exe"]
    new = launcher._cmd(newer, at)
    for n in at:
        assert new[n:].startswith(b"exit /b %errorlevel%\r\n") and new[:n].endswith(b"\r\n"), new
    own = b" -m agent_praxis %* & call exit /b %%errorlevel%%\r\n"
    assert new.count(own) == 2, "its own lines hold the exit on the same line, and need no reading on"
    assert b"goto run\r\n" in new[: at[0]] and new.index(b":run\r\n") > at[-1], "and is never run from the top"

    assert launcher._read_on_at(new) == at, "the next rewrite keeps them: that command may still be running"
    shorter = launcher._cmd([r"C:\p.exe"], launcher._read_on_at(new))
    assert all(shorter[n:].startswith(b"exit /b %errorlevel%\r\n") for n in at)
    assert launcher._read_on_at(launcher._cmd(newer)) == [], "a launcher nobody could be running needs none"


def test_the_windows_launcher_still_says_which_praxis_it_runs_to_every_praxis_version():
    """An older Praxis finds the program the launcher runs by the `if not exist` lines: they stay as they were."""
    text = launcher._cmd([r"C:\new\python.exe", r"C:\old\python.exe"], [260, 400]).decode()
    lines = [line for line in text.splitlines() if line.startswith("if not exist")]
    assert lines == [r'if not exist "C:\new\python.exe" goto next1', r'if not exist "C:\old\python.exe" goto next2']


@pytest.mark.skipif(os.name != "nt", reason="only cmd.exe reads a batch file again after each command in it")
@pytest.mark.parametrize("written_by", ["an older Praxis", "this Praxis"])
def test_a_command_running_through_the_launcher_keeps_its_exit_code_when_the_launcher_is_rewritten(
    home, tmp_path, monkeypatch, written_by
):
    install = tmp_path / "an install"
    (install / "agent_praxis").mkdir(parents=True)
    (install / "agent_praxis" / "__init__.py").write_text("")
    started, gate = tmp_path / "started", tmp_path / "go"
    (install / "agent_praxis" / "__main__.py").write_text(  # a Praxis command that takes a while, then ends with 5
        "import os, sys, time\n"
        f"open({str(started)!r}, 'w').close()\n"
        f"while not os.path.exists({str(gate)!r}):\n    time.sleep(0.05)\n"
        "sys.exit(5)\n"
    )
    file = _launcher_file(home)
    file.parent.mkdir(parents=True)
    if written_by == "this Praxis":
        file.write_bytes(launcher._cmd([sys.executable]))
    else:
        file.write_bytes(_windows_launcher_of_an_older_praxis([sys.executable]))
    env = child_env(PRAXIS_HOME=str(home))
    env["PYTHONPATH"] = str(install) + os.pathsep + env["PYTHONPATH"]
    running = subprocess.Popen([str(file), "run", "--heavy", "tests"], cwd=str(tmp_path), env=env,
                               stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    try:
        deadline = time.time() + 120
        while time.time() < deadline and not started.exists():
            time.sleep(0.05)
        assert started.exists(), "the command never started"
        newer = _fake_python(tmp_path / ("a newer install in a folder with a longer name " + "x" * 30), "NEWER")
        _launcher_from(monkeypatch, newer, "99.0.0", "--switch")  # `praxis update` does this
        assert launcher._same(launcher.runs_now(home), str(newer))
    finally:
        gate.write_text("go")
    out = running.communicate(timeout=120)[0]
    assert running.returncode == 5, out
    assert "not recognized" not in out, out


def test_version_numbers_compare_as_numbers():
    assert launcher.version_key("0.10.0") > launcher.version_key("0.9.5") > launcher.version_key("0.4.0")
    assert launcher.version_key("1.0rc1") == (1, 0)
