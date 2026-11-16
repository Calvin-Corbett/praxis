"""How busy this machine is, and from that how many heavy jobs it can take (`heavy_jobs = "auto"`).

Only the Python standard library: on Windows the load comes from the system's own counters through ctypes, on
Linux from /proc, elsewhere from the load average. Tests replace the reader with a fake through `use_reader`.

The machine's reading includes the heavy jobs that are running, so each job's own share is measured too (treecpu)
and taken out before the limit is sized: a queue that counts its own jobs as "the machine is busy" can never grow.
Tests replace that measuring through `use_tree_reader`.
"""

from __future__ import annotations

import os
import time
from collections.abc import Callable
from dataclasses import dataclass

from agent_praxis import treecpu

TARGET_CPU = 75.0  # the total CPU percent "auto" aims for
LOW_MEMORY = 10.0  # below this much free memory (percent) "auto" allows one job at a time
SAMPLE_SECONDS = 0.25  # how long the CPU is watched to measure how busy it is
BIG_MACHINE_CORES = 8  # with this many cores or more, "auto" keeps at least 2 places (heavy_min's default)


@dataclass
class Load:
    cpu: float | None  # percent of all cores busy right now, or None when it cannot be read
    memory_free: float | None  # percent of memory free, or None when it cannot be read
    cores: int


Reader = Callable[[], Load]
TreeReader = Callable[[list[int]], "dict[int, float | None]"]  # job pids -> each one's percent of the whole CPU


def cores() -> int:
    return max(1, os.cpu_count() or 1)


def default_max() -> int:
    """The most places "auto" may use when praxis.toml sets no heavy_max: half the CPU cores, at least 2."""
    return max(2, cores() // 2)


def default_min(machine_cores: int) -> int:
    """The fewest places "auto" gives when praxis.toml sets no heavy_min: 2 on a big machine, else 1."""
    return 2 if machine_cores >= BIG_MACHINE_CORES else 1


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


_watch = treecpu.watch


def measure(pids: list[int]) -> tuple[Load, dict[int, float | None]]:
    """(the machine's load right now, {pid: the percent of the whole CPU that process and everything it started is
    using}) for the heavy jobs whose processes are *pids*, both over the same moment. A job that cannot be measured
    has None."""
    finish = _watch(pids, cores())
    reading = read()
    return reading, finish()


def use_tree_reader(reader: TreeReader | None) -> None:
    """Replace how the heavy jobs' own CPU share is measured (tests pass a fake); None puts the real one back."""
    global _watch
    _watch = treecpu.watch if reader is None else (lambda pids, _cores: lambda: reader(pids))


# The limit ---------------------------------------------------------------------------------------------------------


def auto_limit(jobs: list[float | None], load: Load, heavy_max: int = 0, heavy_min: int = 0) -> tuple[int, str]:
    """(how many heavy jobs may run at once, why) for the machine's *load*.

    *jobs* has one entry for each heavy job running now: the percent of the whole CPU that job's process tree is
    using, or None while that is not measured. The machine's reading includes those jobs, so their share is taken
    out first: what is left is the load of other programs, which no heavy job limit can change. The room between
    that and TARGET_CPU percent is split into places the size of one job: the average of the running jobs, where a
    job counts as at least one core and as exactly one core until it is measured; with no job running, two cores.

    Never above *heavy_max* (0 means half the CPU cores, at least 2) and never below *heavy_min* (0 means 2 on a
    machine with BIG_MACHINE_CORES cores or more, else 1), so a job that fills every core it is given does not
    hold the whole line at one place. Only nearly full memory brings it to 1 whatever heavy_min says."""
    ceiling = heavy_max if heavy_max > 0 else max(2, load.cores // 2)
    floor = max(1, min(ceiling, heavy_min if heavy_min > 0 else default_min(load.cores)))
    bounds = f"at most {ceiling}" if floor == 1 else f"at least {floor}, at most {ceiling}"
    if load.cpu is None:
        limit = max(floor, min(ceiling, 2))
        return limit, f"auto: the machine's load could not be read, so {limit} ({bounds})"
    core = 100.0 / load.cores
    own = min(load.cpu, sum(job for job in jobs if job is not None))
    other = load.cpu - own
    seen = _counted(other, own, jobs) + ("" if load.memory_free is None else f", memory {load.memory_free:.0f}% free")
    if load.memory_free is not None and load.memory_free < LOW_MEMORY:
        return 1, f"auto: 1 because memory is nearly full ({seen}; {bounds})"
    costs = [core if job is None else max(core, job) for job in jobs]
    typical = sum(costs) / len(costs) if costs else 2 * core
    fits = int((TARGET_CPU - other) // typical)
    limit = max(floor, min(ceiling, fits))
    note = "room to spare" if fits > len(jobs) else "busy" if fits < max(1, len(jobs)) else "at the target"
    if fits < floor and floor > 1:
        note += f", but never fewer than {floor} here (heavy_min)"
    return limit, f"auto: {limit} ({seen}; {note}; {bounds}, aiming for {TARGET_CPU:.0f}% CPU)"


def _counted(other: float, own: float, jobs: list[float | None]) -> str:
    """What the limit was sized from, in words: the other programs' load, and the heavy jobs' own."""
    if not jobs:
        return f"other programs {other:.0f}%, no heavy job running"
    text = f"other programs {other:.0f}%, {len(jobs)} heavy job{'s' if len(jobs) != 1 else ''} using {own:.0f}%"
    unknown = sum(1 for job in jobs if job is None)
    if unknown:
        text += f" ({unknown} not measured yet, counted as one core{' each' if unknown != 1 else ''})"
    return text
