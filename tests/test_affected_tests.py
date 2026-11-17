"""tools/affected_tests.py picks the tests a change can affect, for Praxis's own quick check. It may pick too many,
never too few: whatever it does not know for sure runs the whole suite. The last tests here keep its map
(tools/affected_tests.json) from going stale without anyone noticing."""

from __future__ import annotations

import importlib.util
import shutil
import subprocess
import sys
from pathlib import Path

import pytest
from conftest import CHILD_GUARD, child_env

ROOT = Path(__file__).resolve().parents[1]


def _load(path: Path, name: str):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


tool = _load(ROOT / "tools" / "affected_tests.py", "affected_tests")
ALL = [tool.EVERYTHING]

# A small map, the way the tool writes it.
DATA = {
    "files": {
        "README.md": [],
        "LICENSE": [],
        "src/agent_praxis/agents.py": ["tests"],
        "src/agent_praxis/enough.py": ["tests/test_enough.py", "tests/test_real_world.py"],
        "src/agent_praxis/heavy.py": ["tests/test_autoheavy.py", "tests/test_heavy.py"],
        "tests/test_core_hooks.py": ["tests/test_after_done.py", "tests/test_core_hooks.py"],
        "tests/test_heavy.py": [],
    },
    "untraced": [],
}


def _write(root: Path, files: dict[str, str]) -> None:
    for name, text in files.items():
        (root / name).parent.mkdir(parents=True, exist_ok=True)
        (root / name).write_text(text, encoding="utf-8")


# Choosing ------------------------------------------------------------------------------------------------------------


def test_a_file_the_map_does_not_know_means_everything():
    picked, why = tool.choose(["src/agent_praxis/brand_new.py"], DATA)
    assert picked == ALL and "src/agent_praxis/brand_new.py is not in the map" in why
    assert tool.choose(["README.md", "docs/new-guide.md"], DATA)[0] == ALL, "one unknown file among known ones"
    assert tool.choose(["tests/test_brand_new.py"], DATA)[0] == ALL, "a new test file is unknown too"
    assert tool.choose(["README.MD"], DATA)[0] == ALL, "a different spelling is a different file"


@pytest.mark.parametrize(
    "name",
    [
        "tests/conftest.py",
        "conftest.py",
        "pyproject.toml",
        "src/agent_praxis/store.py",
        "src/agent_praxis/cli.py",
        "src/agent_praxis/__init__.py",
        "src/agent_praxis/__main__.py",
        "tools/affected_tests.py",
        "tools/affected_tests.json",
        "tests/child_guard/sitecustomize.py",
        "tests/child_guard/affected_watch.py",
    ],
)
def test_a_file_every_test_stands_on_means_everything_whatever_the_map_says(name):
    data = {"files": {**DATA["files"], name: []}, "untraced": []}  # even a map that says "no tests"
    picked, why = tool.choose(["README.md", name], data)
    assert picked == ALL and name in why


def test_a_source_file_runs_the_test_files_the_map_names():
    assert tool.choose(["src/agent_praxis/heavy.py"], DATA)[0] == ["tests/test_autoheavy.py", "tests/test_heavy.py"]
    both = tool.choose(["src/agent_praxis/heavy.py", "src/agent_praxis/enough.py", "README.md"], DATA)[0]
    assert both == [
        "tests/test_autoheavy.py",
        "tests/test_enough.py",
        "tests/test_heavy.py",
        "tests/test_real_world.py",
    ]
    assert tool.choose(["src/agent_praxis/heavy.py", "src/agent_praxis/agents.py"], DATA)[0] == ALL


def test_a_changed_test_file_always_runs_itself():
    assert tool.choose(["tests/test_heavy.py"], DATA)[0] == ["tests/test_heavy.py"], "even when the map forgot it"
    picked = tool.choose(["tests/test_core_hooks.py"], DATA)[0]
    assert picked == ["tests/test_after_done.py", "tests/test_core_hooks.py"], "and the test files that import it"


def test_a_change_no_test_can_see_runs_no_tests():
    picked, why = tool.choose(["README.md", "LICENSE"], DATA)
    assert picked == [] and "0 test file(s)" in why


def test_no_list_of_changed_files_means_everything():
    assert tool.choose([], DATA)[0] == ALL
    assert tool.choose(["", "  ", "\n"], DATA)[0] == ALL


def test_names_are_read_the_way_git_and_windows_write_them():
    assert tool.choose(["src\\agent_praxis\\heavy.py\r", "./README.md"], DATA)[0] == [
        "tests/test_autoheavy.py",
        "tests/test_heavy.py",
    ]


def test_a_test_file_the_change_deleted_is_not_handed_to_pytest(tmp_path):
    _write(tmp_path, {"tests/test_autoheavy.py": ""})
    assert tool.choose(["src/agent_praxis/heavy.py", "tests/test_heavy.py"], DATA, tmp_path)[0] == [
        "tests/test_autoheavy.py"
    ]


def test_a_test_file_nobody_has_watched_yet_runs_with_every_change():
    data = {"files": {**DATA["files"], "tests/test_new.py": ["tests/test_new.py"]}, "untraced": ["tests/test_new.py"]}
    assert tool.choose(["README.md"], data)[0] == ["tests/test_new.py"]
    assert tool.choose(["src/agent_praxis/heavy.py"], data)[0] == [
        "tests/test_autoheavy.py",
        "tests/test_heavy.py",
        "tests/test_new.py",
    ]


@pytest.mark.parametrize(
    "data",
    [
        {},
        {"files": None},
        {"files": {"README.md": "tests/test_heavy.py"}},
        {"files": {"README.md": [3]}},
        {"files": {"README.md": []}, "untraced": 7},
    ],
)
def test_a_map_that_cannot_be_read_means_everything(data):
    picked, why = tool.choose(["README.md"], data)
    assert picked == ALL and "cannot be read" in why


# The command ---------------------------------------------------------------------------------------------------------


def _project(root: Path, files: dict | None = None) -> Path:
    tool.save(root / tool.MAP, DATA if files is None else {"files": files, "untraced": []})
    _write(root, {name: "" for name in ("tests/test_autoheavy.py", "tests/test_heavy.py", "tests/test_enough.py")})
    return root


def test_the_command_prints_what_pytest_should_run_and_says_why(tmp_path, capsys):
    root = _project(tmp_path)
    listed = tmp_path / "changed.txt"
    listed.write_text("src/agent_praxis/heavy.py\nREADME.md\n", encoding="utf-8")
    assert tool.main(["--root", str(root), "--list", str(listed)]) == 0
    out, err = capsys.readouterr()
    assert out.splitlines() == ["tests/test_autoheavy.py", "tests/test_heavy.py"]
    assert "2 test file(s)" in err
    assert tool.main(["--root", str(root), "README.md"]) == 0
    out, err = capsys.readouterr()
    assert out == "" and "no tests" in err
    assert tool.main(["--root", str(root), "src/agent_praxis/enough.py", "something/new.txt"]) == 0
    out, err = capsys.readouterr()
    assert out.splitlines() == ALL and "the whole suite" in err and "something/new.txt is not in the map" in err


def test_the_command_falls_back_to_everything_when_it_cannot_know(tmp_path, capsys):
    root = _project(tmp_path)
    assert tool.main(["--root", str(root)]) == 0, "no changed files given"
    assert capsys.readouterr().out.splitlines() == ALL
    assert tool.main(["--root", str(root), "--list", str(tmp_path / "no such list.txt")]) == 0
    assert capsys.readouterr().out.splitlines() == ALL
    (root / tool.MAP).write_text("{ not json", encoding="utf-8")
    assert tool.main(["--root", str(root), "README.md"]) == 0
    out, err = capsys.readouterr()
    assert out.splitlines() == ALL and "missing or damaged" in err
    (root / tool.MAP).unlink()
    assert tool.main(["--root", str(root), "README.md"]) == 0
    assert capsys.readouterr().out.splitlines() == ALL


def test_it_runs_as_a_script_with_nothing_but_python(tmp_path):
    proc = subprocess.run(
        [sys.executable, "-I", str(ROOT / "tools" / "affected_tests.py"), "tools/a file nobody knows.txt"],
        check=False,
        cwd=str(tmp_path),
        capture_output=True,
        text=True,
    )
    assert proc.returncode == 0 and proc.stdout.splitlines() == ALL, proc.stderr


# Keeping the map up to date ------------------------------------------------------------------------------------------


def test_the_map_knows_every_source_and_test_file():
    """The staleness guard. A file the map does not know runs the whole suite on every change to it, so the quick
    check quietly stops being quick: this fails instead, and says what to do."""
    data = tool.load(ROOT / tool.MAP)
    assert data is not None, f"{tool.MAP} is missing or damaged: run `python tools/affected_tests.py --rebuild`"
    unknown = tool.missing(ROOT, data)
    assert not unknown, (
        f"{tool.MAP} does not know: {', '.join(unknown)}.\n"
        "Add them now with `python tools/affected_tests.py --add` (quick and safe: a new test file then runs with "
        "every change, a new source file runs the whole suite), and commit the map. For a tight map again, run "
        "`python tools/affected_tests.py --rebuild` where the check runs (it runs the whole suite once)."
    )


def test_every_test_file_in_the_map_runs_itself_and_the_map_names_only_test_files():
    data = tool.load(ROOT / tool.MAP)
    assert data is not None
    for name, tests in data["files"].items():
        assert isinstance(tests, list), name
        assert all(test == tool.EVERYTHING or tool.is_test_file(test) for test in tests), name
        if tool.is_test_file(name):
            assert name in tests or tool.EVERYTHING in tests, name
        assert not tool.always_everything(name), f"{name} always runs everything: it does not belong in the map"


def test_missing_lists_new_source_and_test_files_and_add_puts_them_in_the_safe_way(tmp_path, capsys):
    root = _project(tmp_path)
    _write(
        root,
        {
            "src/agent_praxis/brand_new.py": "",
            "src/agent_praxis/cli.py": "",  # always everything: never asked for in the map
            "tests/test_brand_new.py": "",
            "tests/conftest.py": "",
            "tests/helpers.py": "",
            "tests/__pycache__/test_old.py": "",
            "tools/thing.py": "",
            "notes.md": "",  # not code: an unknown document runs everything, but the guard does not ask for it
            "src/agent_praxis/heavy.py": "",  # these two are in the map already
            "README.md": "",
        },
    )
    unknown = [
        "src/agent_praxis/brand_new.py",
        "tests/helpers.py",
        "tests/test_autoheavy.py",
        "tests/test_brand_new.py",
        "tests/test_enough.py",
        "tools/thing.py",
    ]
    assert tool.missing(root, tool.load(root / tool.MAP)) == unknown
    assert tool.main(["--root", str(root), "--missing"]) == 0
    assert capsys.readouterr().out.splitlines() == unknown
    assert tool.main(["--root", str(root), "--add"]) == 0
    data = tool.load(root / tool.MAP)
    assert tool.missing(root, data) == []
    assert data["files"]["src/agent_praxis/brand_new.py"] == ALL and data["files"]["tests/helpers.py"] == ALL
    assert data["files"]["tests/test_brand_new.py"] == ["tests/test_brand_new.py"]
    assert data["untraced"] == ["tests/test_autoheavy.py", "tests/test_brand_new.py", "tests/test_enough.py"]
    assert tool.choose(["src/agent_praxis/heavy.py"], data, root)[0] == [
        "tests/test_autoheavy.py",
        "tests/test_brand_new.py",
        "tests/test_enough.py",
        "tests/test_heavy.py",
    ], "the new test files run with every change until the suite is watched again"
    # Files that are gone leave the map: LICENSE and the rest were never in this folder.
    assert "LICENSE" not in data["files"] and "src/agent_praxis/agents.py" not in data["files"]
    assert data["files"]["README.md"] == [] and data["files"]["tests/test_heavy.py"] == []


# Making the map ------------------------------------------------------------------------------------------------------

CHILD_TEST = """\
import subprocess
import sys
from pathlib import Path

TOP = Path(__file__).parents[1]


def _run(*args):
    proc = subprocess.run([sys.executable, *args], check=False, capture_output=True, text=True)
    return proc.stdout.strip() or proc.stderr


def test_child_processes_and_a_file_it_reads():
    assert _run("-m", "agent_praxis") == "hello"
    assert _run(str(TOP / "tools" / "say.py")) == "said"
    assert "one test" in (TOP / "NOTES.md").read_text()
"""

FAKE_PROJECT = {
    "README.md": "A tiny project.\n",
    "NOTES.md": "read by one test\n",
    "tools/say.py": "print('said')\n",
    "src/agent_praxis/__init__.py": "",
    "src/agent_praxis/__main__.py": "from agent_praxis import gamma\n\nprint(gamma.hello())\n",
    "src/agent_praxis/alpha.py": "from agent_praxis import beta\n\n\ndef double(n):\n    return n * beta.FACTOR\n",
    "src/agent_praxis/beta.py": "FACTOR = 2\n\n\ndef unused():\n    return 0\n",
    "src/agent_praxis/gamma.py": "def hello():\n    return 'hello'\n",
    "src/agent_praxis/delta.py": "def never_called():\n    return 1\n",
    "src/agent_praxis/epsilon.py": "from agent_praxis.beta import unused\n\n\ndef calls_it():\n    return unused()\n",
    "tests/test_alpha.py": (
        "from agent_praxis import alpha\n\n\ndef test_double():\n    assert alpha.double(2) == 4\n"
    ),
    "tests/test_child.py": CHILD_TEST,
    "tests/test_epsilon.py": (
        "from agent_praxis import epsilon\n\n\ndef test_calls_it():\n    assert epsilon.calls_it() == 0\n"
    ),
}


def test_the_map_is_built_from_what_was_seen_and_what_the_source_says(tmp_path):
    _write(tmp_path, FAKE_PROJECT)
    used = {
        "tests/test_alpha.py": {"src/agent_praxis/alpha.py", "tests/test_alpha.py"},
        "tests/test_child.py": {"src/agent_praxis/gamma.py", "NOTES.md", "a file that is not in the project"},
        "tests/test_epsilon.py": {"src/agent_praxis/epsilon.py", "src/agent_praxis/beta.py"},
    }
    files = tool.build(tmp_path, used)["files"]
    assert files.pop("tools/say.py") == ALL, "code nobody was seen using, outside the package too"
    assert files == {
        "NOTES.md": ["tests/test_child.py"],
        "README.md": [],  # a document no test opened
        "src/agent_praxis/alpha.py": ["tests/test_alpha.py"],
        # alpha reads beta's constant, which no watching can see: the source text says so. epsilon calls it.
        "src/agent_praxis/beta.py": ["tests/test_alpha.py", "tests/test_epsilon.py"],
        "src/agent_praxis/delta.py": ALL,  # code nobody was seen using is never trusted to affect nothing
        "src/agent_praxis/epsilon.py": ["tests/test_epsilon.py"],
        "src/agent_praxis/gamma.py": ["tests/test_child.py"],
        "tests/test_alpha.py": ["tests/test_alpha.py"],
        "tests/test_child.py": ["tests/test_child.py"],
        "tests/test_epsilon.py": ["tests/test_epsilon.py"],
    }


def test_a_file_every_test_uses_is_written_as_everything(tmp_path):
    _write(tmp_path, FAKE_PROJECT)
    used = {name: {"src/agent_praxis/gamma.py"} for name in FAKE_PROJECT if name.startswith("tests/")}
    assert tool.build(tmp_path, used)["files"]["src/agent_praxis/gamma.py"] == ALL


def test_only_a_plain_function_is_left_to_the_watching(tmp_path):
    _write(
        tmp_path,
        {
            "src/agent_praxis/__init__.py": "",
            "src/agent_praxis/errors.py": "class PraxisError(Exception):\n    pass\n",
            "src/agent_praxis/gitops.py": "LIMIT = 3\n\n\ndef git():\n    return 0\n",
            "src/agent_praxis/hooks.py": "def run():\n    return 0\n",
            "src/agent_praxis/user.py": (
                "from agent_praxis import gitops, hooks\n"
                "from agent_praxis import hooks as H\n"
                "from agent_praxis.errors import PraxisError\n"
                "from agent_praxis.gitops import git\n\n\n"
                "def go(patch):\n"
                "    patch(H, 'run', None)\n"  # the module itself is handed on
                "    return gitops.git() + git() + hooks.run()\n"
            ),
            "src/agent_praxis/caller.py": (
                "from agent_praxis import gitops, hooks\n\n\ndef go():\n    return gitops.git()\n"
            ),
        },
    )
    sources = tool.Sources(tmp_path)
    assert sources.uses("src/agent_praxis/user.py") == (
        {"src/agent_praxis/errors.py", "src/agent_praxis/gitops.py", "src/agent_praxis/hooks.py"},
        {"src/agent_praxis/errors.py", "src/agent_praxis/hooks.py"},  # a class, and a module handed on as a value
    )
    assert sources.uses("src/agent_praxis/caller.py")[1] == set(), "calling plain functions is seen by the watching"
    assert sources.uses("src/agent_praxis/no such file.py") == (set(), set())


def test_the_tool_and_the_watching_agree_on_their_names():
    watch = _load(CHILD_GUARD / "affected_watch.py", "affected_watch_for_test")
    assert (tool.OUT_ENV, tool.ROOT_ENV, tool.NOW_ENV) == (watch.OUT_ENV, watch.ROOT_ENV, watch.NOW_ENV)
    assert not any(name.startswith("PRAXIS_") for name in (tool.OUT_ENV, tool.ROOT_ENV, tool.NOW_ENV)), (
        "conftest.py clears every PRAXIS_ setting for each test: the watching would never reach a child process"
    )


def test_rebuild_watches_a_real_run_child_processes_and_opened_files_included(tmp_path):
    root = tmp_path / "proj"
    _write(root, FAKE_PROJECT)
    shutil.copytree(CHILD_GUARD, root / "tests" / "child_guard", ignore=shutil.ignore_patterns("__pycache__"))
    command = [sys.executable, str(ROOT / "tools" / "affected_tests.py"), "--root", str(root), "--rebuild", "-q"]
    proc = subprocess.run(command, check=False, cwd=str(root), capture_output=True, text=True, env=child_env())
    assert proc.returncode == 0, proc.stdout + proc.stderr
    files = tool.load(root / tool.MAP)["files"]
    assert files["src/agent_praxis/alpha.py"] == ["tests/test_alpha.py"], "a call in the test's own process"
    assert files["src/agent_praxis/beta.py"] == ["tests/test_alpha.py", "tests/test_epsilon.py"]
    assert files["src/agent_praxis/gamma.py"] == ["tests/test_child.py"], "a call in a process the test started"
    assert files["NOTES.md"] == ["tests/test_child.py"], "a file the test opened"
    assert files["tools/say.py"] == ["tests/test_child.py"], "a script a child Python was told to run"
    assert files["README.md"] == [] and files["src/agent_praxis/delta.py"] == ALL
    assert tool.missing(root, tool.load(root / tool.MAP)) == []
    # A failing suite never writes a map: a test that stops early is not seen using everything it uses.
    (root / "tests" / "test_alpha.py").write_text("def test_fails():\n    assert False\n", encoding="utf-8")
    before = (root / tool.MAP).read_text(encoding="utf-8")
    proc = subprocess.run(command, check=False, cwd=str(root), capture_output=True, text=True, env=child_env())
    assert proc.returncode != 0 and "left as it was" in proc.stderr
    assert (root / tool.MAP).read_text(encoding="utf-8") == before
