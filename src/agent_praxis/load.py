"""How busy this machine is, and from that how many heavy jobs it can take (`heavy_jobs = "auto"`).

Only the Python standard library: on Windows the load comes from the system's own counters through ctypes, on
Linux from /proc, elsewhere from the load average. Tests replace the reader with a fake through `use_reader`.
"""

from __future__ import annotations

import os
import time
from collections.abc import Callable
from dataclasses import dataclass

TARGET_CPU = 75.0  # the total CPU percent "auto" aims for
LOW_MEMORY = 10.0  # below this much free memory (percent) "auto" allows one job at a time
SAMPLE_SECONDS = 0.25  # how long the CPU is watched to measure how busy it is


@dataclass
class Load:
    cpu: float | None  # percent of all cores busy right now, or None when it cannot be read
    memory_free: float | None  # percent of memory free, or None when it cannot be read
    cores: int


Reader = Callable[[], Load]


def cores() -> int:
    return max(1, os.cpu_count() or 1)


def default_max() -> int:
    """The most places "auto" may use when praxis.toml sets no heavy_max: half the CPU cores, at least 2."""
    return max(2, cores() // 2)


# Windows ---------------------------------------------------------------------------------------------------------


def _windows_times() -> tuple[int, int] | None:
    """(idle time, total time) of all cores so far, in 100-nanosecond ticks, or None."""
    import ctypes
    from ctypes import wintypes

    idle, kernel, user = (wintypes.FILETIME() for _ in range(3))
    try:
        ok = ctypes.windll.kernel32.GetSystemTimes(ctypes.byref(idle), ctypes.byref(kernel), ctypes.byref(user))
    except (AttributeError, OSError):
        return None
    if not ok:
        return None

    def ticks(t) -> int:
        return (t.dwHighDateTime << 32) | t.dwLowDateTime

    return ticks(idle), ticks(kernel) + ticks(user)  # kernel time already includes the idle time


def _windows_memory_free() -> float | None:
    import ctypes

    class Status(ctypes.Structure):
        _fields_ = [
            ("length", ctypes.c_ulong),
            ("load", ctypes.c_ulong),
            ("total", ctypes.c_ulonglong),
            ("avail", ctypes.c_ulonglong),
            ("page_total", ctypes.c_ulonglong),
            ("page_avail", ctypes.c_ulonglong),
            ("virt_total", ctypes.c_ulonglong),
            ("virt_avail", ctypes.c_ulonglong),
            ("ext", ctypes.c_ulonglong),
        ]

    status = Status()
    status.length = ctypes.sizeof(Status)
    try:
        if not ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(status)) or not status.total:
            return None
    except (AttributeError, OSError):
        return None
    return 100.0 * status.avail / status.total


def _windows() -> Load:
    first = _windows_times()
    cpu = None
    if first:
        time.sleep(SAMPLE_SECONDS)
        second = _windows_times()
        if second and second[1] > first[1]:
            cpu = 100.0 * (1 - (second[0] - first[0]) / (second[1] - first[1]))
    return Load(None if cpu is None else min(100.0, max(0.0, cpu)), _windows_memory_free(), cores())


# Linux and others -------------------------------------------------------------------------------------------------


def _meminfo_free() -> float | None:
    try:
        with open("/proc/meminfo", encoding="ascii") as handle:
            fields = {line.split(":")[0]: int(line.split(":")[1].split()[0]) for line in handle if ":" in line}
        return 100.0 * fields["MemAvailable"] / fields["MemTotal"]
    except (OSError, KeyError, ValueError, IndexError, ZeroDivisionError):
        return None


def _unix() -> Load:
    cpu = None
    try:
        cpu = min(100.0, 100.0 * os.getloadavg()[0] / cores())  # the 1 minute average: a slow, steady reading
    except (OSError, AttributeError):
        pass
    return Load(cpu, _meminfo_free(), cores())


def _real_reader() -> Load:
    return _windows() if os.name == "nt" else _unix()


_reader: Reader = _real_reader


def read() -> Load:
    """The machine's load right now."""
    return _reader()


def use_reader(reader: Reader | None) -> None:
    """Replace how the load is read (tests pass a fake); None puts the real reader back."""
    global _reader
    _reader = reader or _real_reader


# The limit ---------------------------------------------------------------------------------------------------------


def auto_limit(running: int, load: Load, heavy_max: int = 0) -> tuple[int, str]:
    """(how many heavy jobs may run at once, why) for *running* jobs now and the machine's *load*. Never below 1,
    never above *heavy_max* (0 means half the CPU cores, at least 2). Aims for about TARGET_CPU percent of the CPU:
    each running job is taken to cost its share of the CPU in use, and the headroom up to the target is split into
    that many more places (or fewer, when the machine is past the target)."""
    ceiling = heavy_max if heavy_max > 0 else max(2, load.cores // 2)
    if load.cpu is None:
        limit = min(ceiling, 2)
        return limit, f"auto: the machine's load could not be read, so {limit} (at most {ceiling})"
    seen = f"CPU {load.cpu:.0f}%" + ("" if load.memory_free is None else f", memory {load.memory_free:.0f}% free")
    if load.memory_free is not None and load.memory_free < LOW_MEMORY:
        return 1, f"auto: 1 because memory is nearly full ({seen}; at most {ceiling})"
    share = max(load.cpu / running, 100.0 / load.cores) if running else 2 * 100.0 / load.cores
    spare = (
        int((TARGET_CPU - load.cpu) // share) if load.cpu <= TARGET_CPU else -int((load.cpu - TARGET_CPU) // share + 1)
    )
    limit = max(1, min(ceiling, running + spare))
    note = "room to spare" if spare > 0 else "busy" if spare < 0 else "at the target"
    return (
        limit,
        f"auto: {limit} ({seen}, {running} running now, {note}; at most {ceiling}, aiming for {TARGET_CPU:.0f}% CPU)",
    )
