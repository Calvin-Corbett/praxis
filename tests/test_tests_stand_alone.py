"""The tests themselves: each one stands alone, so the suite can run across CPU cores (pytest -n auto).

Nothing here tests Praxis. It tests the walls conftest.py builds around every test: praxis commands run only inside
the test's own temporary folder (in this process and in every child process), the per-user Praxis folder and git's
global settings are the test's own, and nothing is left over in the environment from whoever started the tests.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import conftest
import pytest
from conftest import SRC, child_env, git, praxis, praxis_process

from agent_praxis import launcher

GUARD_CODE = 97  # tests/child_guard/sitecustomize.py ends a refused child with this


def _child(where: Path, *command: str, env: dict | None = None) -> subprocess.CompletedProcess:
    return subprocess.run(
        list(command), check=False, cwd=str(where), capture_output=True, text=True, env=env or child_env()
    )


def test_praxis_in_this_process_refuses_to_run_outside_the_tests_own_folder(project, tmp_path, monkeypatch):
    monkeypatch.chdir(SRC)  # the real Praxis source: never a place for a test's praxis command
    with pytest.raises(pytest.fail.Exception, match="outside this test's temporary folder"):
        praxis("status")
    with pytest.raises(pytest.fail.Exception, match="outside this test's temporary folder"):
        praxis_process(SRC, "status")
    monkeypatch.chdir(tmp_path.parent)  # pytest's temp area, where the other tests' folders are: not this test's
    with pytest.raises(pytest.fail.Exception, match="outside this test's temporary folder"):
        praxis("status")
    monkeypatch.chdir(project)
    assert praxis("status")[0] == 0


def test_a_child_process_refuses_to_run_praxis_outside_the_tests_own_folder(project, tmp_path):
    command = [sys.executable, "-m", "agent_praxis", "status"]
    for outside in (SRC, tmp_path.parent):
        done = _child(outside, *command)
        assert done.returncode == GUARD_CODE and "outside this test's temporary folder" in done.stderr, done
        assert "Praxis status" not in done.stdout, "the command never started"
    done = _child(project, *command)
    assert done.returncode == 0 and "Praxis status" in done.stdout, done


def test_the_guard_reaches_a_praxis_command_started_by_another_program(project):
    """A check command, a hook or a shell script that calls praxis gets the test's own environment, guard included."""
    inner = (
        f"import subprocess, sys; sys.exit(subprocess.run({[sys.executable, '-m', 'agent_praxis', 'status']!r}, "
        f"check=False, cwd={str(SRC)!r}).returncode)"
    )
    done = _child(project, sys.executable, "-c", inner, env=dict(os.environ))  # just what any child would inherit
    assert done.returncode == GUARD_CODE, done


def test_the_guard_leaves_every_other_program_alone(tmp_path):
    done = _child(SRC, sys.executable, "-c", "import agent_praxis, os; print(os.getcwd())")
    assert done.returncode == 0 and Path(done.stdout.strip()) == SRC, done


def test_the_per_user_praxis_folder_is_the_tests_own(tmp_path, monkeypatch):
    home = launcher.home_dir()
    assert tmp_path in home.parents, home
    monkeypatch.chdir(tmp_path)
    code, out = praxis("launcher")
    assert code == 0 and str(home) in out.replace("\n", ""), out
    assert (home / "installs.json").is_file()


def test_gits_global_settings_are_the_tests_own(project, tmp_path_factory):
    """Your own name, signing key and line-ending choice change no result, and no test can write to them."""
    ours = Path(os.environ["GIT_CONFIG_GLOBAL"])
    assert tmp_path_factory.getbasetemp() in ours.parents, ours
    shown = git(project, "config", "--global", "--list", "--show-origin")
    assert ours.name in shown and "signingkey" not in shown, shown
    assert git(project, "config", "--global", "--default", "nobody", "--get", "user.name") == "nobody"


def test_nothing_is_left_over_in_the_environment(tmp_path):
    ours = {"PRAXIS_HOME", "PRAXIS_TEST_AREA", "PRAXIS_TEST_SRC"}
    assert {name for name in os.environ if name.startswith("PRAXIS_")} <= ours
    assert not set(conftest._GIT_LEAKS) & set(os.environ)
    assert Path(os.environ["PRAXIS_TEST_AREA"]) == tmp_path.resolve()


@pytest.mark.parametrize(("cores", "workers"), [(16, 12), (32, 24), (6, 4), (4, 3), (2, 2), (1, 2), (None, 2)])
def test_n_auto_starts_a_worker_for_three_quarters_of_the_cores_and_never_one_for_each(cores, workers, monkeypatch):
    monkeypatch.setattr(os, "cpu_count", lambda: cores)
    assert conftest.pytest_xdist_auto_num_workers(SimpleNamespace()) == workers
