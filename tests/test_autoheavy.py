"""heavy_jobs = "auto": the limit follows the machine's load."""

from __future__ import annotations

import sys
from types import SimpleNamespace

import pytest
from conftest import praxis

from agent_praxis import config, heavy, load
from agent_praxis.config import Config
from agent_praxis.errors import PraxisError
from agent_praxis.load import Load
from agent_praxis.store import Store


@pytest.fixture(autouse=True)
def _real_reader_afterwards():
    yield
    load.use_reader(None)


def _set(project, line: str, name: str = "heavy_jobs") -> None:
    toml = project / "praxis.toml"
    kept = [ln for ln in toml.read_text().splitlines() if not ln.startswith(name + " =")]
    toml.write_text("\n".join([*kept, line]) + "\n")


# The sizing ---------------------------------------------------------------------------------------------------------


def test_an_idle_machine_gets_all_the_places_it_is_allowed():
    limit, why = load.auto_limit(0, Load(cpu=3, memory_free=70, cores=16))
    assert limit == 5 and "auto: 5" in why and "at most 8" in why  # 75% of 16 cores, a job taking about two


def test_a_busy_machine_gets_fewer_places_but_never_fewer_than_one():
    assert load.auto_limit(3, Load(cpu=95, memory_free=50, cores=16))[0] < 3  # fewer than are running now
    assert load.auto_limit(1, Load(cpu=95, memory_free=50, cores=16))[0] == 1
    assert load.auto_limit(0, Load(cpu=100, memory_free=50, cores=4))[0] == 1


def test_the_limit_follows_the_load_it_sees():
    quiet = load.auto_limit(2, Load(cpu=30, memory_free=60, cores=32), heavy_max=16)[0]
    loaded = load.auto_limit(2, Load(cpu=70, memory_free=60, cores=32), heavy_max=16)[0]
    swamped = load.auto_limit(2, Load(cpu=90, memory_free=60, cores=32), heavy_max=16)[0]
    assert quiet > loaded >= swamped >= 1


def test_the_target_is_about_75_percent_cpu():
    # 4 jobs use 40 percent, so one costs 10: room for 3 more to reach 70 (the next would pass 75)
    assert load.auto_limit(4, Load(cpu=40, memory_free=60, cores=64), heavy_max=32)[0] == 7


def test_nearly_full_memory_allows_one_job():
    limit, why = load.auto_limit(0, Load(cpu=5, memory_free=4, cores=16))
    assert limit == 1 and "memory" in why


def test_heavy_max_caps_it_and_the_default_is_half_the_cores_at_least_2():
    assert load.auto_limit(0, Load(cpu=1, memory_free=80, cores=64), heavy_max=3)[0] == 3
    assert "at most 2" in load.auto_limit(0, Load(cpu=1, memory_free=80, cores=2))[1]
    assert "at most 2" in load.auto_limit(0, Load(cpu=1, memory_free=80, cores=1))[1]
    assert load.default_max() >= 2


def test_an_unreadable_load_falls_back_to_a_small_safe_limit():
    limit, why = load.auto_limit(0, Load(cpu=None, memory_free=None, cores=16))
    assert limit == 2 and "could not be read" in why


def test_the_real_reader_reads_something_sensible():
    reading = load.read()
    assert reading.cores >= 1
    assert reading.cpu is None or 0 <= reading.cpu <= 100
    assert reading.memory_free is None or 0 <= reading.memory_free <= 100


# praxis.toml --------------------------------------------------------------------------------------------------------


def test_auto_and_numbers_are_both_valid(project):
    _set(project, 'heavy_jobs = "auto"')
    assert config.load(project).heavy_jobs == "auto"
    _set(project, 'heavy_jobs = "AUTO"')
    assert config.load(project).heavy_jobs == "auto"
    _set(project, "heavy_jobs = 3")
    assert config.load(project).heavy_jobs == 3


@pytest.mark.parametrize("line", ['heavy_jobs = "lots"', "heavy_jobs = 0", "heavy_jobs = true"])
def test_other_values_are_refused(project, line):
    _set(project, line)
    with pytest.raises(PraxisError):
        config.load(project)


def test_heavy_max_is_read_and_must_not_be_negative(project):
    _set(project, "heavy_max = 3", "heavy_max")
    assert config.load(project).heavy_max == 3
    _set(project, "heavy_max = -1", "heavy_max")
    with pytest.raises(PraxisError):
        config.load(project)


def test_the_written_file_documents_auto():
    text = config.render(Config(heavy_jobs="auto", heavy_max=4))
    assert 'heavy_jobs = "auto"' in text and "heavy_max = 4" in text
    parsed = config.parse_toml(text)
    assert parsed["heavy_jobs"] == "auto" and parsed["heavy_max"] == 4


# At work ------------------------------------------------------------------------------------------------------------


def test_limit_now_asks_the_machine_every_time(project):
    store = Store(project / ".git" / "praxis")
    try:
        ctx = SimpleNamespace(cfg=Config(heavy_jobs="auto", heavy_max=8), store=store)
        load.use_reader(lambda: Load(cpu=5, memory_free=80, cores=32))
        assert heavy.limit_now(ctx)[0] == 8  # 75% of 32 cores holds more than the cap
        load.use_reader(lambda: Load(cpu=92, memory_free=80, cores=32))
        assert heavy.limit_now(ctx)[0] == 1
        ctx.cfg.heavy_jobs = 3
        limit, why = heavy.limit_now(ctx)
        assert limit == 3 and "heavy_jobs = 3" in why
    finally:
        store.close()


def test_a_heavy_job_runs_under_auto_on_a_quiet_machine(project):
    _set(project, 'heavy_jobs = "auto"')
    load.use_reader(lambda: Load(cpu=2, memory_free=80, cores=8))
    code, out = praxis("run", "--heavy", sys.executable, "-c", "print(1)")
    assert code == 0 and "Running the heavy job now" in out, out


def test_a_busy_machine_still_lets_the_first_job_run(project):
    _set(project, 'heavy_jobs = "auto"')
    load.use_reader(lambda: Load(cpu=99, memory_free=3, cores=8))  # never below 1: nobody starves
    code, out = praxis("run", "--heavy", sys.executable, "-c", "print(1)")
    assert code == 0, out


def test_status_shows_the_current_limit_and_why(project):
    _set(project, 'heavy_jobs = "auto"')
    load.use_reader(lambda: Load(cpu=10, memory_free=60, cores=8))
    out = praxis("status")[1]
    assert "HEAVY JOBS (at most 2 run at once right now" in out
    assert "auto: 2 (CPU 10%, memory 60% free" in out
    load.use_reader(lambda: Load(cpu=97, memory_free=60, cores=8))
    assert "at most 1 run at once right now" in praxis("status")[1]


def test_a_number_still_shows_as_before(project):
    _set(project, "heavy_jobs = 2")
    assert "HEAVY JOBS" not in praxis("status")[1]  # nothing running, nothing to say
