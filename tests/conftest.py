"""A real, small game-like git project for every test, with a space in its path on purpose."""

from __future__ import annotations

import contextlib
import io
import os
import subprocess
import sys
from pathlib import Path

import pytest

from agent_praxis.cli import main

SRC = Path(
    os.environ.get("PRAXIS_TEST_SRC") or Path(__file__).resolve().parents[1] / "src"
)  # set it to test another copy
# The check fails when any script contains the word BROKEN, and prints one line per broken file.
CHECK_PY = """\
import pathlib, sys
bad = [str(p) for p in pathlib.Path("scripts").glob("*.gd") if "BROKEN" in p.read_text()]
for name in bad:
    print("ERROR: broken script", name)
print(f"{len(bad)} failed, 3 passed")
sys.exit(1 if bad else 0)
"""


def git(cwd, *args) -> str:
    proc = subprocess.run(["git", *args], check=False, cwd=str(cwd), capture_output=True, text=True)
    assert proc.returncode == 0, proc.stderr
    return proc.stdout.strip()


def praxis(*args: str) -> tuple[int, str]:
    """Run the praxis command in this process; returns (exit code, everything it printed)."""
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf), contextlib.redirect_stderr(buf):
        code = main(list(args))
    return code, buf.getvalue()


CHILD_GUARD = Path(__file__).resolve().parent / "child_guard"  # its sitecustomize.py guards child processes
AREA_ENV = "PRAXIS_TEST_AREA"  # the running test's own temporary folder, for child_guard/sitecustomize.py
_AREA: list[Path] = []  # the running test's own temporary folder, set by the fixture below
# Settings git and Praxis read from the environment. One left over from whoever started the tests (a landing's
# check runs with PRAXIS_AGENT set, a git hook with GIT_DIR) would point a test at a real agent or a real project.
_GIT_LEAKS = (
    "GIT_DIR",
    "GIT_WORK_TREE",
    "GIT_INDEX_FILE",
    "GIT_COMMON_DIR",
    "GIT_OBJECT_DIRECTORY",
    "GIT_PREFIX",
    "GIT_NAMESPACE",
    "GIT_CONFIG",
    "GIT_CONFIG_COUNT",
    "GIT_CONFIG_PARAMETERS",
    "GIT_AUTHOR_NAME",
    "GIT_AUTHOR_EMAIL",
    "GIT_AUTHOR_DATE",
    "GIT_COMMITTER_NAME",
    "GIT_COMMITTER_EMAIL",
    "GIT_COMMITTER_DATE",
)


@pytest.hookimpl(optionalhook=True)  # pytest-xdist's hook: without xdist it is simply never called
def pytest_xdist_auto_num_workers(config) -> int:
    """How many workers `pytest -n auto` starts (needs pytest-xdist): three quarters of the CPU cores, at least 2,
    so 12 on a PC with 16. A worker is not a busy core. On a disk the tests mostly wait: every git commit and every
    database commit they make (thousands) waits for the disk, so eight workers kept only two to three cores busy.

    Measured on a 16-core PC that other agents kept busy (Linux in WSL, the whole suite, 2026-11-16). Temporary
    files on the disk: 8 workers 380 s, 12 workers 243 s and 244 s, 16 workers 276 s and 252 s, 24 workers 257 s
    with one test failing. Temporary files in memory (TMPDIR in /dev/shm), where nothing waits for a disk: 8 workers
    32 to 91 s over four runs, 12 workers 34 to 75 s over four, 16 workers 45 s and 51 s, 24 workers 61 s and 69 s;
    the spread is the other work on the machine. Above three quarters nothing gets faster either way. xdist's own
    `auto` would start a worker for every core. `-n 8` still means 8."""
    from agent_praxis import load

    return max(2, load.cores() * 3 // 4)


def child_env(**more: str) -> dict[str, str]:
    """The environment for a process a test starts itself: this copy of Praxis first, and the child guard."""
    return dict(os.environ, PYTHONPATH=os.pathsep.join([str(CHILD_GUARD), str(SRC)]), **more)


def refuse_outside_test_folder(where: Path | str, args) -> None:
    """The tests drive the real praxis command. A slip of the current folder once ran `praxis land` against the
    real Praxis project: no praxis command from a test may run anywhere but inside that test's own tmp_path."""
    try:
        Path(where).resolve().relative_to(_AREA[0])
    except (ValueError, IndexError, OSError):
        pytest.fail(
            f"refused to run `praxis {' '.join(args)}` in {where}: that is outside this test's temporary "
            f"folder ({_AREA[0] if _AREA else 'not known yet'}). Tests run praxis only in their own "
            "temporary projects.",
            pytrace=False,
        )


@pytest.fixture(scope="session")
def _git_settings_of_our_own(tmp_path_factory) -> Path:
    """A global git config for the tests alone, so the real one (a signing key, another name, autocrlf) changes no
    result and no test can write to it. One per pytest process: with pytest-xdist each worker has its own."""
    cfg = tmp_path_factory.mktemp("git-settings") / "gitconfig"
    cfg.write_text("[core]\n\tautocrlf = false\n[init]\n\tdefaultBranch = main\n")
    return cfg


@pytest.fixture(autouse=True)
def _each_test_alone(tmp_path, monkeypatch, _git_settings_of_our_own):
    """Nothing a test reads or writes is shared with another test, with a test running at the same moment in
    another worker, or with the person's real setup: praxis commands run only under this test's tmp_path, the
    per-user Praxis folder is inside it, and the environment carries no leftovers."""
    from agent_praxis import cli

    _AREA[:] = [tmp_path.resolve()]
    for name in list(os.environ):
        if (name.startswith("PRAXIS_") and name != "PRAXIS_TEST_SRC") or name in _GIT_LEAKS:
            monkeypatch.delenv(name)
    monkeypatch.setenv("PRAXIS_HOME", str(tmp_path / "praxis home"))  # never the real ~/.praxis
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(_git_settings_of_our_own))
    monkeypatch.setenv(AREA_ENV, str(_AREA[0]))
    monkeypatch.setenv(
        "PYTHONPATH",
        os.pathsep.join([str(CHILD_GUARD), *([os.environ["PYTHONPATH"]] if os.environ.get("PYTHONPATH") else [])]),
    )
    real_run = cli.run

    def guarded_run(argv):
        refuse_outside_test_folder(Path.cwd(), argv)
        return real_run(argv)

    monkeypatch.setattr(cli, "run", guarded_run)  # main() calls run(): every in-process command passes here


def praxis_process(root: Path, *args: str, **kw) -> subprocess.Popen:
    refuse_outside_test_folder(root, args)
    return subprocess.Popen(
        [sys.executable, "-m", "agent_praxis", *args],
        cwd=str(root),
        env=child_env(),
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        **kw,
    )


def check_command() -> str:
    return f'"{sys.executable}" check.py'


@pytest.fixture
def project(tmp_path, monkeypatch) -> Path:
    root = tmp_path / "my game"
    (root / "scenes").mkdir(parents=True)
    (root / "art").mkdir()
    (root / "scripts").mkdir()
    (root / "project.godot").write_text('[application]\nconfig/name="Tiny"\n')
    (root / "scenes" / "level1.tscn").write_bytes(b"[gd_scene format=3]\n\x00\x01binary-ish\n")
    (root / "art" / "hero.png").write_bytes(b"\x89PNG\r\n\x1a\n\x00\x00fake")
    (root / "scripts" / "player.gd").write_text("extends Node\nvar speed = 10\n")
    (root / "scripts" / "enemy.gd").write_text("extends Node\nvar hp = 3\n")
    (root / "check.py").write_text(CHECK_PY)
    git(root, "init", "-q", "-b", "main")
    git(root, "config", "user.name", "Test")
    git(root, "config", "user.email", "test@example.com")
    git(root, "config", "core.autocrlf", "false")
    git(root, "add", "-A")
    git(root, "commit", "-q", "-m", "first version")
    monkeypatch.chdir(root)
    code, out = praxis("init", "--check", check_command())
    assert code == 0, out
    return root


def folder(root: Path, agent: str) -> Path:
    return root.parent / f"{root.name}-praxis" / agent


def edit(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)
