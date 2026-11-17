"""The look at the machine's process list: what it finds, what it leaves out, and that it never breaks a command.

The real looks here are at children that sleep. How long a look takes is not tested: a time on a shared machine
proves nothing. What is tested is the thing that makes it short on Windows (the reading thread goes in front of
ordinary programs for the length of the read, and is put back afterwards) and that the answer is the same.
"""

from __future__ import annotations

import os
import subprocess
import sys
import uuid

import pytest

from agent_praxis import processes
from agent_praxis.processes import Proc

windows_only = pytest.mark.skipif(os.name != "nt", reason="reads the Windows process list")


def _sleeper(*words: str) -> subprocess.Popen:
    """A child that sleeps, with *words* at the end of its command line."""
    return subprocess.Popen([sys.executable, "-c", "import time; time.sleep(120)", *words])


def _stop(*children: subprocess.Popen) -> None:
    for child in children:
        child.kill()
        child.wait()


def _thread_priority() -> int:
    import ctypes
    from ctypes import wintypes

    k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    k32.GetCurrentThread.restype = wintypes.HANDLE
    k32.GetThreadPriority.argtypes = [wintypes.HANDLE]
    return k32.GetThreadPriority(k32.GetCurrentThread())


def _front() -> int:
    """The priority a read in this test process goes to. pytest itself may have been started at any priority."""
    import ctypes
    from ctypes import wintypes

    k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    k32.GetCurrentProcess.restype = wintypes.HANDLE
    k32.GetPriorityClass.argtypes = [wintypes.HANDLE]
    k32.GetPriorityClass.restype = wintypes.DWORD
    level = processes.front_level(k32.GetPriorityClass(k32.GetCurrentProcess()))
    if level is None:
        pytest.skip("these tests were started above normal priority: their threads are in front already")
    return level


def _set_thread_priority(priority: int) -> None:
    import ctypes
    from ctypes import wintypes

    k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    k32.GetCurrentThread.restype = wintypes.HANDLE
    k32.SetThreadPriority.argtypes = [wintypes.HANDLE, ctypes.c_int]
    assert k32.SetThreadPriority(k32.GetCurrentThread(), priority)


# Which processes work inside a folder ---------------------------------------------------------------------------------


def test_a_command_line_that_names_a_path_in_the_folder_counts(tmp_path):
    folder = tmp_path / "game-praxis" / "bob"
    procs = [
        Proc(1, f'python -m pytest "{folder / "tests"}"'),
        Proc(2, f"node {folder}"),
        Proc(3, f"python {tmp_path / 'game-praxis' / 'bobby' / 'run.py'}"),  # another agent whose name starts alike
        Proc(4, "python -m http.server"),
    ]
    assert [p.pid for p in processes.inside(procs, folder)] == [1, 2]


def test_a_program_running_in_the_folder_counts_whatever_its_command_line_says(tmp_path):
    folder = tmp_path / "bob"
    procs = [Proc(1, "make", str(folder)), Proc(2, "make", str(folder / "src")), Proc(3, "make", str(tmp_path))]
    assert [p.pid for p in processes.inside(procs, folder)] == [1, 2]


@windows_only
def test_slashes_capitals_and_git_bash_spelling_all_name_the_same_folder():
    procs = [
        Proc(1, r"python C:\Work\Game-Praxis\Bob\run.py"),
        Proc(2, "python c:/work/game-praxis/bob/run.py"),
        Proc(3, "bash /c/work/game-praxis/bob/build.sh"),
        Proc(4, "bash /d/work/game-praxis/bob/build.sh"),
    ]
    assert [p.pid for p in processes.inside(procs, r"C:\work\game-praxis\bob")] == [1, 2, 3]


def test_no_process_and_no_folder_give_nothing(tmp_path):
    assert processes.inside([], tmp_path) == []


# One real look ------------------------------------------------------------------------------------------------------


def test_a_real_look_finds_a_child_by_its_command_line_and_never_this_process(tmp_path):
    mark = f"praxis-test-{uuid.uuid4().hex}"
    inside, outside = tmp_path / "bob", tmp_path / "bobby"
    children = [_sleeper(mark, str(inside / "run.py")), _sleeper(mark, str(outside / "run.py"))]
    try:
        seen = processes.look()
        mine = {p.pid: p for p in seen if mark in p.command}
        assert {child.pid for child in children} <= set(mine)
        assert os.getpid() not in {p.pid for p in seen}
        working = {p.pid for p in processes.inside(seen, inside)}
        assert children[0].pid in working and children[1].pid not in working
    finally:
        _stop(*children)


def test_a_look_that_fails_gives_nothing_and_never_an_error(monkeypatch):
    def broken():
        raise OSError("the process list cannot be read")

    for reader in ("_windows", "_proc", "_ps"):
        monkeypatch.setattr(processes, reader, broken)
    assert processes.look() == []


def test_going_in_front_runs_the_block_on_every_system():
    ran = []
    with processes.in_front():
        ran.append(1)
    assert ran == [1]


# Windows: the read goes in front of ordinary programs, and gives the same answer ------------------------------------


def test_which_priority_is_in_front_depends_on_how_the_process_was_started():
    assert processes.front_level(0x20) == 2  # an ordinary process: two steps up, "highest"
    assert processes.front_level(0x4000) == processes.front_level(0x40) == 15  # below normal, idle: "time critical"
    for already_in_front_or_unknown in (0x8000, 0x80, 0x100, 0, 12345):  # above normal, high, realtime, unreadable
        assert processes.front_level(already_in_front_or_unknown) is None


@windows_only
def test_inside_the_block_the_thread_is_in_front_and_afterwards_it_is_where_it_was():
    front, before = _front(), _thread_priority()
    with processes.in_front():
        assert _thread_priority() == front > before
        with processes.in_front():  # a read inside a read changes nothing
            assert _thread_priority() == front
        assert _thread_priority() == front
    assert _thread_priority() == before


@windows_only
@pytest.mark.parametrize("started", [0x4000, 0x40], ids=["below normal", "idle"])
def test_a_praxis_started_at_lowered_priority_reads_in_front_too(started):
    """Two steps up from "below normal" or "idle" is still not in front of an ordinary program: measured on a busy
    PC, the read then took seconds again (up to 19 from "below normal", 18 to 29 from "idle"). There the thread goes
    to "time critical" for the read. This test process is lowered for the few lines that look, then put back."""
    import ctypes
    from ctypes import wintypes

    k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    k32.GetCurrentProcess.restype = wintypes.HANDLE
    k32.GetPriorityClass.argtypes = [wintypes.HANDLE]
    k32.GetPriorityClass.restype = wintypes.DWORD
    k32.SetPriorityClass.argtypes = [wintypes.HANDLE, wintypes.DWORD]
    this_process = k32.GetCurrentProcess()
    was = k32.GetPriorityClass(this_process)
    assert was and k32.SetPriorityClass(this_process, started)
    try:
        seen = [_thread_priority()]
        with processes.in_front():
            seen.append(_thread_priority())
        seen.append(_thread_priority())
    finally:
        assert k32.SetPriorityClass(this_process, was)
    assert seen[1] == 15 and seen[0] == seen[2] < 15, seen


@windows_only
def test_a_thread_set_lower_is_put_back_lower_and_a_thread_already_higher_is_left_alone():
    before = _thread_priority()
    try:
        front = _front()
        _set_thread_priority(-2)  # THREAD_PRIORITY_LOWEST
        with processes.in_front():
            assert _thread_priority() == front
        assert _thread_priority() == -2
        _set_thread_priority(15)  # THREAD_PRIORITY_TIME_CRITICAL: never brought down
        with processes.in_front():
            assert _thread_priority() == 15
        assert _thread_priority() == 15
    finally:
        _set_thread_priority(before)


@windows_only
def test_the_thread_is_put_back_when_the_block_fails():
    before = _thread_priority()
    with pytest.raises(RuntimeError), processes.in_front():
        raise RuntimeError("the read broke")
    assert _thread_priority() == before


@windows_only
def test_look_reads_the_windows_list_in_front_and_puts_the_thread_back(monkeypatch):
    front, before = _front(), _thread_priority()
    during = []

    def reader():
        during.append(_thread_priority())
        return [Proc(os.getpid(), "this process"), Proc(4_000_000_001, "another")], {os.getpid(): 0}

    monkeypatch.setattr(processes, "_windows", reader)
    assert [p.pid for p in processes.look()] == [4_000_000_001]
    assert during == [front] and _thread_priority() == before

    def broken():
        during.append(_thread_priority())
        raise OSError("the process list cannot be read")

    monkeypatch.setattr(processes, "_windows", broken)
    assert processes.look() == []
    assert during[-1] == front and _thread_priority() == before


@windows_only
def test_a_process_that_ended_or_cannot_be_opened_is_left_out_and_the_rest_are_read(monkeypatch):
    """Between the look at the list and the look at each process, programs end. The ended child here is still in
    the list the reader is given, exactly as when it ends in between."""
    mark = f"praxis-test-{uuid.uuid4().hex}"
    alive = _sleeper(mark)
    ended = subprocess.Popen([sys.executable, "-c", "pass", mark])
    ended.wait()  # this test still holds it, so Windows keeps its number and has no command line for it
    try:
        listed = {0: 0, 4: 0, ended.pid: os.getpid(), 4_000_000_000: 4, alive.pid: os.getpid()}
        monkeypatch.setattr(processes, "windows_parents", lambda: dict(listed))
        found, parents = processes._windows()
        assert [(p.pid, mark in p.command) for p in found] == [(alive.pid, True)]
        assert parents == listed
    finally:
        _stop(alive)


@windows_only
def test_an_empty_list_gives_nothing(monkeypatch):
    monkeypatch.setattr(processes, "windows_parents", dict)
    assert processes._windows() == ([], {})
    assert processes.look() == []


@windows_only
def test_the_read_in_front_gives_the_same_command_lines_as_the_plain_read(monkeypatch):
    mark = f"praxis-test-{uuid.uuid4().hex}"
    children = [_sleeper(mark, f"child-{n}", "a path/with spaces/run.py") for n in range(3)]
    try:
        after = {p.pid: p.command for p in processes.look() if mark in p.command}  # the whole machine, in front
        whole = processes.windows_parents()
        # The read as it always was, at ordinary priority, of these children only: the whole list read that way
        # takes many seconds on a busy machine. (A venv's python.exe starts the real one a moment later, so only
        # the children themselves are compared.)
        monkeypatch.setattr(processes, "windows_parents", lambda: {child.pid: whole[child.pid] for child in children})
        plain, parents = processes._windows()
        before = {p.pid: p.command for p in plain}
        assert sorted(before) == sorted(child.pid for child in children)
        for n, child in enumerate(children):
            assert before[child.pid] == after[child.pid] and f"child-{n}" in after[child.pid]
            assert after[child.pid].endswith('"a path/with spaces/run.py"')
            assert parents[child.pid] == os.getpid()
    finally:
        _stop(*children)


@windows_only
def test_a_command_line_longer_than_the_first_buffer_is_read_whole():
    long_word = "x" * 9000  # past the 8192 bytes asked for first
    child = _sleeper(long_word)
    try:
        commands = [p.command for p in processes.look() if p.pid == child.pid]
        assert len(commands) == 1 and commands[0].endswith(long_word)
    finally:
        _stop(child)
