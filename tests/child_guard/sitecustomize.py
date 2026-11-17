"""Stops a test's child process from running a praxis command outside that test's own temporary folder.

conftest.py puts this folder on PYTHONPATH for every test, so every Python a test starts (directly, through a
shell, through a check command, through praxis itself) loads this file first. It does nothing unless the process
is `python -m agent_praxis ...` (or Praxis starting itself again in the background, which names its own code
instead: background._spawn); then it ends the process at once when the current folder is not inside the folder
named by PRAXIS_TEST_AREA. conftest.py does the same for commands run inside the test process.
"""

import itertools
import os
import sys

EXIT_CODE = 97  # no praxis command ends with this code, so a test that sees it knows the guard spoke


def _is_praxis_command(args: list[str]) -> bool:
    if any(a == "-m" and b == "agent_praxis" for a, b in itertools.pairwise(args)):
        return True
    return any(a == "-c" and "from agent_praxis.cli import main" in b for a, b in itertools.pairwise(args))


def _words(args: list[str]) -> str:
    """The praxis command in *args*, as a person would type it."""
    if "agent_praxis" in args:
        return " ".join(args[args.index("agent_praxis") + 1 :])
    return " ".join(args[args.index("-c") + 3 :])  # after the code and the folder it is told (background._spawn)


def _inside(here: str, top: str) -> bool:
    here, top = (os.path.normcase(os.path.realpath(p)) for p in (here, top))
    return here == top or here.startswith(top.rstrip("\\/") + os.sep)


def _guard() -> None:
    area = os.environ.get("PRAXIS_TEST_AREA")
    args = list(getattr(sys, "orig_argv", []))
    if not area or not _is_praxis_command(args):
        return
    try:
        here = os.getcwd()
    except OSError:
        here = "(a folder that no longer exists)"
    if os.path.isdir(here) and _inside(here, area):
        return
    sys.stderr.write(
        f"refused to run `praxis {_words(args)}` in {here}: that is outside "
        f"this test's temporary folder ({area}). Tests run praxis only in their own temporary projects.\n"
    )
    sys.stderr.flush()
    os._exit(EXIT_CODE)


def _watch() -> None:
    """`python tools/affected_tests.py --rebuild` asks every Python a test starts to write down the project files
    it uses (see affected_watch.py next to this file). Without that request nothing is loaded."""
    if os.environ.get("AFFECTED_TESTS_WATCH"):
        import affected_watch

        affected_watch.install()


_guard()
_watch()
