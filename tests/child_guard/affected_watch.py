"""Watches which project files each test file uses, for `python tools/affected_tests.py --rebuild`.

It does nothing unless AFFECTED_TESTS_WATCH names a folder to write to. The rebuild loads it into pytest as a plugin
(`-p affected_watch`), and sitecustomize.py next to it loads it into every Python a test starts, so the praxis
commands a test runs as separate processes are watched too. Two things are written down, each once per test file:

- `call`: a function in a project file was called. Running a file's top level (an import) does not count, or every
  test would "use" every module that `agent_praxis.cli` imports; names a module only reads from another module are
  found by the rebuild from the source text instead.
- `read`: a project file was opened, other than by Python importing it or printing a traceback.

One line per fact goes to a file of this process's own in the folder: `test file<TAB>call or read<TAB>project file`.
The names start with AFFECTED_ and not PRAXIS_ because conftest.py clears every PRAXIS_ setting for each test.
"""

from __future__ import annotations

import os
import sys
import threading

OUT_ENV = "AFFECTED_TESTS_WATCH"  # the folder the records go to; it must not be inside the project
ROOT_ENV = "AFFECTED_TESTS_ROOT"  # the top folder of the project being watched
NOW_ENV = "AFFECTED_TESTS_NOW"  # the test file running now: pytest sets it per test, a child process inherits it

_A_FUNCTION = 0x0001  # CO_OPTIMIZED: set on a function's code, never on a module's top level or a class body
_IMPORTING = "<frozen importlib"  # Python reading a module in order to import it
_FOR_A_TRACEBACK = ("linecache.py", "tokenize.py")  # Python reading source lines to print a traceback
_UNKNOWN = object()
_state: dict = {"on": False, "root": "", "dir": "", "now": "", "out": None}
_names: dict = {}  # a path as Python gave it -> the same file from the top of the project, or None
_seen: set[tuple[str, str, str]] = set()
_lock = threading.Lock()


def _project_file(path) -> str | None:
    """*path* from the top of the project, with forward slashes, or None when it is not inside the project."""
    try:
        full = os.path.realpath(os.fsdecode(path))
    except (TypeError, ValueError, OSError):
        return None
    root = _state["root"]
    if not os.path.normcase(full).startswith(os.path.normcase(root) + os.sep):
        return None
    name = full[len(root) + 1 :].replace(os.sep, "/")
    return None if "__pycache__/" in name or name.startswith(".git/") else name


def _named(path) -> str | None:
    name = _names.get(path, _UNKNOWN)
    if name is _UNKNOWN:
        name = _project_file(path)
        if os.path.isabs(path):  # a path from the current folder means another file after a change of folder
            _names[path] = name
    return name


def _note(kind: str, name: str) -> None:
    now = _state["now"]
    key = (now, kind, name)
    if not now or key in _seen:
        return
    with _lock:
        if key in _seen:
            return
        _seen.add(key)
        try:
            if _state["out"] is None:
                mine = os.path.join(_state["dir"], f"{os.getpid()}.txt")
                _state["out"] = open(mine, "a", encoding="utf-8")  # noqa: SIM115 - kept open for the whole process
            _state["out"].write(f"{now}\t{kind}\t{name}\n")
            _state["out"].flush()  # at once: a test may stop this process without warning
        except (OSError, ValueError):
            _state["now"] = ""  # the folder has gone (the rebuild is over): stop, and never break what is watched


def _on_call(frame, event, _arg) -> None:
    if event == "call":
        code = frame.f_code
        if code.co_flags & _A_FUNCTION:
            name = _named(code.co_filename)
            if name is not None:
                _note("call", name)


def _on_audit(event, args) -> None:
    if event != "open" or not _state["now"] or not isinstance(args[0], (str, bytes)):
        return
    try:
        opener = sys._getframe(1).f_code.co_filename  # the Python file whose code opened it
    except ValueError:
        opener = ""  # Python itself, opening the script it was told to run: no Python code is running yet
    if opener.startswith(_IMPORTING) or opener.endswith(_FOR_A_TRACEBACK):
        return
    name = _named(args[0])
    if name is not None:
        _note("read", name)


def install() -> None:
    """Start watching this process, when the rebuild asked for it. Safe to call more than once."""
    out, root = os.environ.get(OUT_ENV), os.environ.get(ROOT_ENV)
    if not out or not root or _state["on"]:
        return
    _state.update(on=True, dir=out, root=os.path.realpath(root), now=os.environ.get(NOW_ENV, ""))
    if _project_file(os.path.join(out, "x")) is not None:
        return  # the records would be written inside the project, and writing one would itself be recorded
    sys.addaudithook(_on_audit)
    threading.setprofile(_on_call)
    sys.setprofile(_on_call)


def _switch(test_file: str) -> None:
    _state["now"] = test_file.replace("\\", "/")
    if test_file:
        os.environ[NOW_ENV] = _state["now"]  # a process the test starts says which test file it works for
    else:
        os.environ.pop(NOW_ENV, None)


# pytest's hooks, used when this file is loaded as a plugin ---------------------------------------------------------


def pytest_runtest_logstart(nodeid, location) -> None:
    install()
    if _state["on"]:
        _switch(location[0])


def pytest_runtest_logfinish(nodeid, location) -> None:
    if _state["on"]:
        _switch("")
