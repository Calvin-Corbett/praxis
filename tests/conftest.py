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

SRC = Path(os.environ.get("PRAXIS_TEST_SRC") or Path(__file__).resolve().parents[1] / "src")  # set it to test another copy
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


_TEMP_AREA: list[Path] = []  # pytest's temp area for this run, set by the fixture below


def refuse_outside_temp_area(where: Path | str, args) -> None:
    """The tests drive the real praxis command. A slip of the current folder once ran `praxis land` against the
    real Praxis project: no praxis command from a test may run anywhere but inside pytest's temp area."""
    try:
        Path(where).resolve().relative_to(_TEMP_AREA[0])
    except (ValueError, IndexError, OSError):
        pytest.fail(f"refused to run `praxis {' '.join(args)}` in {where}: that is outside pytest's temp area "
                    f"({_TEMP_AREA[0] if _TEMP_AREA else 'not known yet'}). Tests run praxis only in their own "
                    "temporary projects.", pytrace=False)


@pytest.fixture(autouse=True)
def _praxis_only_inside_the_temp_area(tmp_path_factory, monkeypatch):
    from agent_praxis import cli
    _TEMP_AREA[:] = [tmp_path_factory.getbasetemp().resolve()]
    real_run = cli.run

    def guarded_run(argv):
        refuse_outside_temp_area(Path.cwd(), argv)
        return real_run(argv)

    monkeypatch.setattr(cli, "run", guarded_run)  # main() calls run(): every in-process command passes here


def praxis_process(root: Path, *args: str, **kw) -> subprocess.Popen:
    refuse_outside_temp_area(root, args)
    env = dict(os.environ, PYTHONPATH=str(SRC))
    return subprocess.Popen([sys.executable, "-m", "agent_praxis", *args], cwd=str(root), env=env,
                            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, **kw)


def check_command() -> str:
    return f'"{sys.executable}" check.py'


@pytest.fixture
def project(tmp_path, monkeypatch) -> Path:
    root = tmp_path / "my game"
    (root / "scenes").mkdir(parents=True)
    (root / "art").mkdir()
    (root / "scripts").mkdir()
    (root / "project.godot").write_text("[application]\nconfig/name=\"Tiny\"\n")
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
    monkeypatch.delenv("PRAXIS_DEBUG", raising=False)
    code, out = praxis("init", "--check", check_command())
    assert code == 0, out
    return root


def folder(root: Path, agent: str) -> Path:
    return root.parent / f"{root.name}-praxis" / agent


def edit(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)
