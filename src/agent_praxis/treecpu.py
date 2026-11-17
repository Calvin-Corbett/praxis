"""How much of the CPU one program and everything it started is using: a heavy job's own load.

`heavy_jobs = "auto"` must not read the heavy jobs' own work as "the machine is busy" (load.py), so each running
job is measured by itself: its process (the pid in the heavy table), that process's children, their children.
Only the Python standard library: on Windows the process list and each process's CPU time come from the system
through ctypes, on Linux from /proc, elsewhere from `ps`. On Windows each look goes in front of ordinary programs
for its length (processes.in_front): on a busy machine one look took up to 1.8 seconds without that, and status
makes two.

What is not seen: a process whose parent has already ended (only the parent's number is kept, and the chain from
the job to it is broken), and a program that started and ended between the two looks. Tests replace all of this
with a fake through load.use_tree_reader.
"""

from __future__ import annotations

import os
import subprocess
import time
from collections.abc import Callable
from pathlib import Path

from agent_praxis import processes

WINDOW_SECONDS = 0.25  # the least time between the two looks at each tree's CPU time

Shares = dict[int, "float | None"]  # a job's pid -> percent of the whole CPU its tree is using; None: not measured
Trees = dict[int, dict[int, float]]  # a job's pid -> {each pid in its tree: a number for the CPU it has used}
Times = Callable[[int], "tuple[float, float] | None"]  # pid -> (when it was created, CPU used), None: unreadable


def watch(roots: list[int], cores: int) -> Callable[[], Shares]:
    """Start measuring the process tree under each pid in *roots*. Returns the function that finishes: called a
    moment later (it waits until WINDOW_SECONDS have passed if they have not), it gives each root's share of the
    whole CPU, in percent of all *cores*, over that moment. A tree that cannot be read gives None."""
    roots = list(dict.fromkeys(int(pid) for pid in roots if pid))
    if not roots:
        return dict
    if os.name != "nt" and not Path("/proc/self/stat").exists():
        return lambda: _from_ps(roots, cores)  # `ps` already gives a recent share: one look is enough
    first = _look(roots)
    began = time.monotonic()

    def finish() -> Shares:
        left = WINDOW_SECONDS - (time.monotonic() - began)
        if left > 0:
            time.sleep(left)
        second = _look(roots)
        elapsed = time.monotonic() - began
        return {root: share(first.get(root), second.get(root), elapsed, cores) for root in roots}

    return finish


def share(first: dict[int, float] | None, second: dict[int, float] | None, elapsed: float, cores: int) -> float | None:
    """The percent of the whole CPU a tree used between two looks *elapsed* seconds apart (CPU seconds per pid).
    A process that is new in the second look started in between, so everything it used counts."""
    if not first or not second or elapsed <= 0:
        return None
    used = sum(max(0.0, seconds - first.get(pid, 0.0)) for pid, seconds in second.items())
    return min(100.0, 100.0 * used / (elapsed * max(1, cores)))


def trees(roots: list[int], children: dict[int, list[int]], times: Times) -> Trees:
    """Each root's tree: the root, its children, their children. A "child" created before its parent is not one:
    the parent's number was given to a new process after the real parent ended."""
    found: Trees = {}
    for root in roots:
        tree: dict[int, float] = {}
        todo = [(root, 0.0)]
        while todo:
            pid, parent_made = todo.pop()
            mine = None if pid in tree else times(pid)
            if mine is None or mine[0] < parent_made:
                continue
            tree[pid] = mine[1]
            todo += [(child, mine[0]) for child in children.get(pid, ())]
        found[root] = tree
    return found


def by_parent(parents: dict[int, int]) -> dict[int, list[int]]:
    children: dict[int, list[int]] = {}
    for pid, parent in parents.items():
        children.setdefault(parent, []).append(pid)
    return children


def _look(roots: list[int]) -> Trees:
    try:
        if os.name == "nt":
            with processes.in_front():  # on a busy machine this look takes seconds at ordinary priority
                return _windows(roots)
        return _proc(roots)
    except Exception:  # noqa: BLE001 - a load that cannot be measured must never stop a heavy job or status
        return {}


# Linux -----------------------------------------------------------------------------------------------------------


def _proc(roots: list[int]) -> Trees:
    tick = os.sysconf("SC_CLK_TCK")
    parents: dict[int, int] = {}
    used: dict[int, float] = {}
    for entry in os.scandir("/proc"):
        if not entry.name.isdigit():
            continue
        try:
            with open(f"/proc/{entry.name}/stat", "rb") as handle:
                stat = handle.read().decode(errors="replace")
            fields = stat[stat.rfind(")") + 2 :].split()  # after the name: state, parent, ..., user time, system time
            parents[int(entry.name)] = int(fields[1])
            used[int(entry.name)] = (int(fields[11]) + int(fields[12])) / tick
        except (OSError, ValueError, IndexError):
            continue
    # Linux hands a child whose parent ended to another process, so "created before its parent" proves nothing here.
    return trees(roots, by_parent(parents), lambda pid: (0.0, used[pid]) if pid in used else None)


# macOS and other systems with ps -----------------------------------------------------------------------------------


def _from_ps(roots: list[int], cores: int) -> Shares:
    try:
        proc = subprocess.run(
            ["ps", "-axo", "pid=,ppid=,pcpu="],
            capture_output=True,
            text=True,
            stdin=subprocess.DEVNULL,
            check=False,
            errors="replace",
        )
    except OSError:
        return dict.fromkeys(roots)
    parents: dict[int, int] = {}
    busy: dict[int, float] = {}  # percent of ONE core, as ps prints it
    for line in proc.stdout.splitlines():
        parts = line.split()
        try:
            parents[int(parts[0])], busy[int(parts[0])] = int(parts[1]), float(parts[2])
        except (ValueError, IndexError):
            continue
    found = trees(roots, by_parent(parents), lambda pid: (0.0, busy[pid]) if pid in busy else None)
    return {root: min(100.0, sum(tree.values()) / max(1, cores)) if tree else None for root, tree in found.items()}


# Windows ---------------------------------------------------------------------------------------------------------

_QUERY_LIMITED = 0x1000


def _windows(roots: list[int]) -> Trees:
    import ctypes
    from ctypes import wintypes

    k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    k32.OpenProcess.restype = wintypes.HANDLE
    k32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    k32.GetProcessTimes.argtypes = [wintypes.HANDLE] + [ctypes.POINTER(wintypes.FILETIME)] * 4
    k32.CloseHandle.argtypes = [wintypes.HANDLE]

    def ticks(stamp) -> int:
        return (stamp.dwHighDateTime << 32) | stamp.dwLowDateTime

    def times(pid: int) -> tuple[float, float] | None:
        handle = k32.OpenProcess(_QUERY_LIMITED, False, pid)
        if not handle:
            return None  # gone already, or another user's
        try:
            made, ended, kernel, user = (wintypes.FILETIME() for _ in range(4))
            if not k32.GetProcessTimes(handle, *(ctypes.byref(stamp) for stamp in (made, ended, kernel, user))):
                return None
        finally:
            k32.CloseHandle(handle)
        return float(ticks(made)), (ticks(kernel) + ticks(user)) / 1e7  # 100-nanosecond ticks to seconds

    return trees(roots, by_parent(processes.windows_parents()), times)
