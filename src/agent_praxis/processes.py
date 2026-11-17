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

How long the Windows read takes. It asks Windows about one process after another, three calls each, and needs
about a tenth of a second of CPU for every thousand processes. On a machine whose cores are all busy, a thread at
ordinary priority that makes those calls keeps waiting for a CPU in between (under the heaviest load one call in
ten took 30 ms or more), and the read took 2 to 25 seconds. So for the length of the read the thread goes in front
of ordinary programs (`in_front`, at the priority `front_level` picks) and is put back afterwards. The calls and
their order are the same, and so are the command lines.
Measured on a 16-core PC that other work kept at 100% CPU (2026-11-16), the old and the new read turn by turn,
each in a fresh Python, eight of each: with 1,200 to 1,500 processes 1.8 to 16.7 s before (half of them over 4.8 s)
and 0.16 to 0.21 s now; with 1,300 to 3,000 processes 6.6 to 24.9 s before (half over 12.8 s) and 0.17 to 0.37 s
now. Those times are `look()` as a whole. Once the thread is put back it is an ordinary thread again and can lose
the CPU at once: of 32 looks made one after another in one process (1,050 processes), the read in front took 0.15
to 0.39 s every time, and `look()` as a whole took that in 28 of them and 0.7, 0.8, 1.2 and 2.1 s in four, the
extra time coming after the read. Because the read is over in a moment, it also finds the short programs that used to
end while a slow read was still on its way to them. Reading from several threads at ordinary priority was
measured too, and is slower than one thread in front (4 threads 0.17 to 0.29 s, 8 threads 0.32 to 0.35 s, where
one thread in front took 0.08 s), so there are no threads here.
A Praxis that was itself started at a lowered priority ("below normal", "idle") needs a bigger step to be in
front (`front_level`). The read is then as short as anywhere, but whatever else that process does still waits
behind every ordinary program, the lines right after the read included: `look()` most often took 2.5 to 2.8 s
there on the same busy PC (0.2 s of it the read), where the step of an ordinary process gave 0.5 to 19 s from
"below normal" and 18 to 29 s from "idle".
"""

from __future__ import annotations

import contextlib
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
            with in_front():
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
_NORMAL_CLASS, _BELOW_NORMAL_CLASS, _IDLE_CLASS = 0x20, 0x4000, 0x40  # how a whole process was started
_HIGHEST, _TIME_CRITICAL = 2, 15  # THREAD_PRIORITY_HIGHEST and THREAD_PRIORITY_TIME_CRITICAL


def front_level(priority_class: int) -> int | None:
    """The thread priority that puts a thread in front of ordinary programs, for a process started with
    *priority_class*; None when its threads are in front already or the class is not known, and nothing is changed.

    Windows adds a thread's own step to its process's class. In an ordinary process two steps up (the most a step
    can be) is in front. A process started at "below normal" or "idle" (by Windows' task scheduler, by `start
    /low`, by a tool that keeps its agents out of the way) stays at or behind ordinary programs however many steps
    it takes: there only "time critical" is in front, the top of what a program without special rights may have.
    A process started above normal is in front as it is, and a "realtime" one is never touched."""
    if priority_class == _NORMAL_CLASS:
        return _HIGHEST
    if priority_class in (_BELOW_NORMAL_CLASS, _IDLE_CLASS):
        return _TIME_CRITICAL
    return None


@contextlib.contextmanager
def in_front():
    """Windows: for the length of the `with` block the calling thread goes in front of ordinary programs, and is
    put back where it was afterwards, also when the block fails. On other systems, and when Windows refuses, the
    block simply runs. Only for a short read of the process list (see the top of this file): never around work
    that keeps a CPU busy."""
    put_back = _go_in_front() if os.name == "nt" else None
    try:
        yield
    finally:
        if put_back:
            put_back()


def _go_in_front():
    """Raise the calling thread to its front_level. Gives the function that puts it back, or None when nothing
    was changed: the thread is already there or higher, or Windows would not say or would not do it."""
    try:
        import ctypes
        from ctypes import wintypes

        k32 = ctypes.WinDLL("kernel32", use_last_error=True)
        k32.GetCurrentProcess.restype = wintypes.HANDLE
        k32.GetPriorityClass.argtypes = [wintypes.HANDLE]
        k32.GetPriorityClass.restype = wintypes.DWORD
        k32.GetCurrentThread.restype = wintypes.HANDLE
        k32.GetThreadPriority.argtypes = [wintypes.HANDLE]
        k32.GetThreadPriority.restype = ctypes.c_int
        k32.SetThreadPriority.argtypes = [wintypes.HANDLE, ctypes.c_int]
        k32.SetThreadPriority.restype = wintypes.BOOL
        level = front_level(k32.GetPriorityClass(k32.GetCurrentProcess()))  # 0, no class, when it cannot be read
        me = k32.GetCurrentThread()  # a name for "this thread" that is only good inside this thread
        was = k32.GetThreadPriority(me)  # a very large number when it cannot be read: then nothing is changed
        if level is None or was >= level or not k32.SetThreadPriority(me, level):
            return None
    except Exception:  # noqa: BLE001 - going in front only saves time: the read happens either way
        return None
    return lambda: k32.SetThreadPriority(me, was)


def windows_parents() -> dict[int, int]:
    """{pid: its parent's pid} for every process on the machine, from one look at the Windows process list. {} when
    it cannot be read."""
    import ctypes
    from ctypes import wintypes

    class Entry(ctypes.Structure):  # PROCESSENTRY32W
        _fields_ = [("dwSize", wintypes.DWORD), ("cntUsage", wintypes.DWORD), ("th32ProcessID", wintypes.DWORD),
                    ("th32DefaultHeapID", ctypes.c_size_t), ("th32ModuleID", wintypes.DWORD),
                    ("cntThreads", wintypes.DWORD), ("th32ParentProcessID", wintypes.DWORD),
                    ("pcPriClassBase", ctypes.c_long), ("dwFlags", wintypes.DWORD),
                    ("szExeFile", ctypes.c_wchar * 260)]

    k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    k32.CreateToolhelp32Snapshot.restype = wintypes.HANDLE
    k32.CreateToolhelp32Snapshot.argtypes = [wintypes.DWORD, wintypes.DWORD]
    k32.Process32FirstW.argtypes = [wintypes.HANDLE, ctypes.POINTER(Entry)]
    k32.Process32NextW.argtypes = [wintypes.HANDLE, ctypes.POINTER(Entry)]
    k32.CloseHandle.argtypes = [wintypes.HANDLE]
    snap = k32.CreateToolhelp32Snapshot(_SNAPSHOT_PROCESSES, 0)
    if not snap or snap == wintypes.HANDLE(-1).value:
        return {}
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
    return pids


def _windows() -> tuple[list[Proc], dict[int, int]]:
    import ctypes
    from ctypes import wintypes

    class UnicodeString(ctypes.Structure):
        _fields_ = [("Length", wintypes.USHORT), ("MaximumLength", wintypes.USHORT), ("Buffer", ctypes.c_void_p)]

    k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    ntdll = ctypes.WinDLL("ntdll")
    k32.OpenProcess.restype = wintypes.HANDLE
    k32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    k32.CloseHandle.argtypes = [wintypes.HANDLE]
    ntdll.NtQueryInformationProcess.argtypes = [wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.ULONG,
                                                ctypes.POINTER(wintypes.ULONG)]
    ntdll.NtQueryInformationProcess.restype = ctypes.c_long

    pids = windows_parents()
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
