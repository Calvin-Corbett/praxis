"""A heavy job's own share of the CPU: its process, the processes it started, and theirs.

No test here keeps a CPU busy: the arithmetic is tested on made-up numbers, and the one real look is at a child
that sleeps.
"""

from __future__ import annotations

import os
import subprocess
import sys

import pytest

from agent_praxis import load, treecpu
from agent_praxis.load import Load


@pytest.fixture(autouse=True)
def _real_readers_afterwards():
    yield
    load.use_reader(None)
    load.use_tree_reader(None)


# The arithmetic -----------------------------------------------------------------------------------------------------


def test_the_share_is_the_cpu_time_the_tree_used_out_of_all_the_cores():
    first = {10: 5.0, 11: 1.0}
    second = {10: 6.0, 11: 2.0}  # two CPU seconds in one second, on 16 cores
    assert treecpu.share(first, second, 1.0, 16) == pytest.approx(12.5)
    assert treecpu.share(first, second, 0.5, 16) == pytest.approx(25.0)


def test_a_process_that_started_between_the_two_looks_counts_in_full():
    assert treecpu.share({10: 5.0}, {10: 5.0, 12: 0.4}, 1.0, 4) == pytest.approx(10.0)


def test_a_process_that_ended_or_a_number_given_to_a_new_process_never_counts_below_zero():
    assert treecpu.share({10: 5.0, 11: 9.0}, {10: 5.5}, 1.0, 1) == pytest.approx(50.0)  # 11 ended
    assert treecpu.share({10: 5.0}, {10: 0.1}, 1.0, 1) == 0.0  # the same number, a new process


def test_the_share_is_never_more_than_the_whole_machine_and_none_when_the_tree_could_not_be_read():
    assert treecpu.share({10: 0.0}, {10: 99.0}, 1.0, 4) == 100.0
    assert treecpu.share({}, {10: 1.0}, 1.0, 4) is None  # not seen at the first look
    assert treecpu.share({10: 1.0}, {}, 1.0, 4) is None  # gone at the second
    assert treecpu.share(None, None, 1.0, 4) is None
    assert treecpu.share({10: 1.0}, {10: 2.0}, 0.0, 4) is None


def test_a_tree_is_the_job_its_children_and_their_children():
    parents = {1: 0, 10: 1, 11: 10, 12: 11, 13: 10, 20: 1, 21: 20}
    used = {pid: float(pid) for pid in parents}
    found = treecpu.trees(
        [10, 20, 99], treecpu.by_parent(parents), lambda pid: (0.0, used[pid]) if pid in used else None
    )
    assert found == {10: {10: 10.0, 11: 11.0, 12: 12.0, 13: 13.0}, 20: {20: 20.0, 21: 21.0}, 99: {}}


def test_a_child_created_before_its_parent_is_somebody_elses():
    """Windows keeps only the parent's number. When the real parent ended and its number was given to a new
    process, the old "child" is older than that new process: it is not part of the job."""
    parents = {10: 1, 11: 10, 12: 10}
    made = {10: 500.0, 11: 600.0, 12: 100.0}  # 12 was made long before 10
    found = treecpu.trees([10], treecpu.by_parent(parents), lambda pid: (made[pid], 1.0))
    assert found == {10: {10: 1.0, 11: 1.0}}


def test_a_loop_in_the_process_list_does_not_hang():
    found = treecpu.trees([10], {10: [11], 11: [10]}, lambda pid: (0.0, 1.0))
    assert found == {10: {10: 1.0, 11: 1.0}}


def test_nothing_to_measure_takes_no_time_and_gives_nothing():
    assert treecpu.watch([], 8)() == {}
    assert treecpu.watch([0, None], 8)() == {}  # a job that has no process yet


# One real look ------------------------------------------------------------------------------------------------------


def test_a_real_child_is_found_under_this_process_and_measured():
    child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])  # asleep: uses no CPU
    try:
        finish = treecpu.watch([os.getpid(), 4_000_000_000], load.cores())
        if os.name == "nt" or os.path.exists("/proc/self/stat"):
            assert child.pid in treecpu._look([os.getpid()])[os.getpid()]
        shares = finish()
        mine = shares[os.getpid()]
        assert mine is None or 0.0 <= mine <= 100.0
        assert shares[4_000_000_000] is None  # no such process: not measured, and no error
        if os.name == "nt" or os.path.exists("/proc/self/stat"):
            assert mine is not None
    finally:
        child.kill()
        child.wait()


# With the machine's reading ------------------------------------------------------------------------------------------


def test_measure_gives_the_machine_and_each_jobs_share_and_tests_can_fake_both():
    asked = []
    load.use_reader(lambda: Load(cpu=70, memory_free=34, cores=16))
    load.use_tree_reader(lambda pids: asked.append(list(pids)) or {111: 24.0, 222: None})
    reading, shares = load.measure([111, 222])
    assert reading.cpu == 70 and shares == {111: 24.0, 222: None} and asked == [[111, 222]]
    load.use_tree_reader(None)
    assert load.measure([])[1] == {}
