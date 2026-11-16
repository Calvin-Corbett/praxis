"""Which running programs work inside an agent's folder: a long test run is work, even when no file changes.

One look at the whole machine's process list serves every agent (`look`), then `inside` picks one folder's.

What is seen, and its limits:
- Windows: each process's command line, read with the Windows API (no extra package). A program counts when its
  command line names a path inside the folder. The current folder of another process is not read (Windows has no
  supported way to ask), so a program started from inside the folder with no path in its command line is missed.
  Processes of other users, and some protected system ones, cannot be read and are skipped.
- Linux: /proc, both the command line and the current folder.
- Elsewhere (macOS): `ps`, command lines only.
This process and the ones that started it (the shell running `praxis status`) never count.
"""

from __future__ import annotations

import os
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path


@dataclass
class Proc:
    pid: int
    command: str
    cwd: str = ""


def look() -> list[Proc]:
    """Every process this user can read, less this one and those that started it. [] when nothing can be read."""
    try:
        if os.name == "nt":
            found, parents = _windows()
        elif Path("/proc/self/cmdline").exists():
            found, parents = _proc()
        else:
            found, parents = _ps()
    except Exception:  # noqa: BLE001 - a heartbeat that cannot look must never break status
        return []
    mine = _ancestors(os.getpid(), parents)
    return [p for p in found if p.pid not in mine]


def _ancestors(pid: int, parents: dict[int, int]) -> set[int]:
    seen = set()
    while pid and pid not in seen:
        seen.add(pid)
        pid = parents.get(pid, 0)
    return seen


def _norm(text: str) -> str:
    text = text.replace("\\", "/")
    return text.casefold() if os.name == "nt" or sys.platform == "darwin" else text


def inside(procs: list[Proc], folder: Path | str) -> list[Proc]:
    """The processes that work inside *folder*: their command line names a path in it, or they run in it."""
    top = _norm(os.path.abspath(str(folder))).rstrip("/")
    if not top:
        return []
    tops = [top]
    if len(top) > 2 and top[1] == ":":  # Git Bash and MSYS programs write C:/work as /c/work
        tops.append("/" + top[0] + top[2:])
    found = []
    for proc in procs:
        cwd = _norm(proc.cwd).rstrip("/")
        command = _norm(proc.command)
        if cwd and (cwd == top or cwd.startswith(top + "/")) or any(_names_path_in(command, t) for t in tops):
            found.append(proc)
    return found


def _names_path_in(command: str, top: str) -> bool:
    """True when *top* appears in *command* as a whole path (followed by the end, a "/", a quote or a space), so
    the folder "game-praxis/bob" is not matched by "game-praxis/bobby"."""
    start = command.find(top)
    while start != -1:
        after = command[start + len(top):start + len(top) + 1]
        if after in ("", "/", '"', "'", " "):
            return True
        start = command.find(top, start + 1)
    return False


# Linux -----------------------------------------------------------------------------------------------------------

def _proc() -> tuple[list[Proc], dict[int, int]]:
    found, parents = [], {}
    for entry in os.scandir("/proc"):
        if not entry.name.isdigit():
            continue
        pid = int(entry.name)
        try:
            with open(f"/proc/{pid}/stat", "rb") as handle:
                stat = handle.read().decode(errors="replace")
            parents[pid] = int(stat[stat.rfind(")") + 2:].split()[1])
            with open(f"/proc/{pid}/cmdline", "rb") as handle:
                command = handle.read().replace(b"\0", b" ").decode(errors="replace").strip()
        except (OSError, ValueError, IndexError):
            continue
        try:
            cwd = os.readlink(f"/proc/{pid}/cwd")
        except OSError:
            cwd = ""
        if command or cwd:
            found.append(Proc(pid, command, cwd))
    return found, parents


# macOS and other systems with ps -----------------------------------------------------------------------------------

def _ps() -> tuple[list[Proc], dict[int, int]]:
    proc = subprocess.run(["ps", "-axww", "-o", "pid=,ppid=,command="], capture_output=True, text=True,
                          stdin=subprocess.DEVNULL, check=False, errors="replace")
    found, parents = [], {}
    for line in proc.stdout.splitlines():
        parts = line.split(None, 2)
        if len(parts) < 3 or not parts[0].isdigit() or not parts[1].isdigit():
            continue
        pid = int(parts[0])
        parents[pid] = int(parts[1])
        found.append(Proc(pid, parts[2]))
    return found, parents


# Windows ---------------------------------------------------------------------------------------------------------

_QUERY_LIMITED, _COMMAND_LINE_INFO, _SNAPSHOT_PROCESSES = 0x1000, 60, 0x2


def _windows() -> tuple[list[Proc], dict[int, int]]:
    import ctypes
    from ctypes import wintypes

    class Entry(ctypes.Structure):  # PROCESSENTRY32W
        _fields_ = [("dwSize", wintypes.DWORD), ("cntUsage", wintypes.DWORD), ("th32ProcessID", wintypes.DWORD),
                    ("th32DefaultHeapID", ctypes.c_size_t), ("th32ModuleID", wintypes.DWORD),
                    ("cntThreads", wintypes.DWORD), ("th32ParentProcessID", wintypes.DWORD),
                    ("pcPriClassBase", ctypes.c_long), ("dwFlags", wintypes.DWORD),
                    ("szExeFile", ctypes.c_wchar * 260)]

    class UnicodeString(ctypes.Structure):
        _fields_ = [("Length", wintypes.USHORT), ("MaximumLength", wintypes.USHORT), ("Buffer", ctypes.c_void_p)]

    k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    ntdll = ctypes.WinDLL("ntdll")
    k32.CreateToolhelp32Snapshot.restype = wintypes.HANDLE
    k32.CreateToolhelp32Snapshot.argtypes = [wintypes.DWORD, wintypes.DWORD]
    k32.Process32FirstW.argtypes = [wintypes.HANDLE, ctypes.POINTER(Entry)]
    k32.Process32NextW.argtypes = [wintypes.HANDLE, ctypes.POINTER(Entry)]
    k32.OpenProcess.restype = wintypes.HANDLE
    k32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    k32.CloseHandle.argtypes = [wintypes.HANDLE]
    ntdll.NtQueryInformationProcess.argtypes = [wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.ULONG,
                                                ctypes.POINTER(wintypes.ULONG)]
    ntdll.NtQueryInformationProcess.restype = ctypes.c_long

    snap = k32.CreateToolhelp32Snapshot(_SNAPSHOT_PROCESSES, 0)
    if not snap or snap == wintypes.HANDLE(-1).value:
        return [], {}
    pids: dict[int, int] = {}
    try:
        entry = Entry()
        entry.dwSize = ctypes.sizeof(Entry)
        more = k32.Process32FirstW(snap, ctypes.byref(entry))
        while more:
            pids[int(entry.th32ProcessID)] = int(entry.th32ParentProcessID)
            more = k32.Process32NextW(snap, ctypes.byref(entry))
    finally:
        k32.CloseHandle(snap)
    found = []
    buf = ctypes.create_string_buffer(8192)
    for pid in pids:
        if pid in (0, 4):  # the idle and System processes
            continue
        handle = k32.OpenProcess(_QUERY_LIMITED, False, pid)
        if not handle:
            continue
        try:
            command = _command_line(ntdll, handle, buf, UnicodeString)
        finally:
            k32.CloseHandle(handle)
        if command:
            found.append(Proc(pid, command))
    return found, pids


def _command_line(ntdll, handle, buf, text_type) -> str:
    """The process's command line (ProcessCommandLineInformation, Windows 8.1 and newer): a UNICODE_STRING whose
    text follows it in the same buffer."""
    import ctypes
    from ctypes import wintypes

    size = wintypes.ULONG(0)
    status = ntdll.NtQueryInformationProcess(handle, _COMMAND_LINE_INFO, buf, len(buf), ctypes.byref(size))
    if status != 0 and size.value > len(buf):  # a very long command line: ask again with room for it
        buf = ctypes.create_string_buffer(size.value)
        status = ntdll.NtQueryInformationProcess(handle, _COMMAND_LINE_INFO, buf, len(buf), ctypes.byref(size))
    if status != 0:
        return ""
    text = text_type.from_buffer(buf)
    if not text.Buffer or not text.Length:
        return ""
    return ctypes.wstring_at(text.Buffer, text.Length // 2)
