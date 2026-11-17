"""Named checks with `inputs`: every matching one runs, and a pass is remembered for the files that check reads."""

from __future__ import annotations

import sys
import time
from pathlib import Path

import pytest
from conftest import edit, folder, git, praxis, praxis_process

from agent_praxis import checks, config
from agent_praxis.config import Config
from agent_praxis.errors import PraxisError
from agent_praxis.landing_queue import Ticket
from agent_praxis.store import Store

# A short fake check for one named check. It notes its name when it starts and when it ends, waits until a gate file
# with its name (or "all") exists when it is given a folder of gates, and fails when one of its files says BROKEN.
AREA_CHECK = """\
import os, pathlib, sys, time
log, name, gates, files = sys.argv[1], sys.argv[2], sys.argv[3], sys.argv[4:]
with open(log, "a") as f:
    f.write("start|" + name + "\\n")
for _ in range(1200):
    if gates == "-" or os.path.exists(os.path.join(gates, name)) or os.path.exists(os.path.join(gates, "all")):
        break
    time.sleep(0.05)
bad = [p for p in files if os.path.exists(p) and "BROKEN" in pathlib.Path(p).read_text()]
for p in bad:
    print("ERROR: broken script", p)
with open(log, "a") as f:
    f.write("end|" + name + "\\n")
sys.exit(1 if bad else 0)
"""


def set_line(project: Path, line: str) -> None:
    """Put `name = value` into praxis.toml, in place of the line that sets that name now (if there is one)."""
    toml = project / "praxis.toml"
    key = line.split("=")[0].strip()
    kept = [ln for ln in toml.read_text().splitlines() if not ln.startswith(key + " =")]
    toml.write_text("\n".join([*kept, line]) + "\n")


def named_check(
    tmp_path: Path,
    name: str,
    reads: list[str],
    paths: list[str] | None = None,
    inputs: list[str] | None = None,
    env_inputs: list[str] | None = None,
    gates: Path | None = None,
) -> str:
    """One entry for praxis.toml's `checks`: a check called *name* that really reads the files *reads* (it fails when
    one of them says BROKEN), is chosen by *paths* and says it reads *inputs* (both default to *reads*)."""
    script = tmp_path / "area_check.py"
    script.write_text(AREA_CHECK)
    gate = f'"{gates}"' if gates else "-"
    run = f'"{sys.executable}" "{script}" "{tmp_path / "check-runs.log"}" {name} {gate} ' + " ".join(reads)

    def listed(values: list[str]) -> str:
        return "[" + ", ".join(f'"{v}"' for v in values) + "]"

    parts = [f'name = "{name}"', f"paths = {listed(paths or reads)}"]
    if inputs is None or inputs:
        parts.append(f"inputs = {listed(reads if inputs is None else inputs)}")
    if env_inputs:
        parts.append(f"env_inputs = {listed(env_inputs)}")
    return "{ " + ", ".join([*parts, f"run = '{run}'"]) + " }"


def use_checks(project: Path, *entries: str, **settings: str) -> None:
    """Set praxis.toml's `checks` (and other settings). The audit is off unless a test turns it on, so that no test
    leaves a background process behind by accident."""
    settings.setdefault("audit_every", "0")
    set_line(project, "checks = [" + ", ".join(entries) + "]")
    for name, value in settings.items():
        set_line(project, f"{name} = {value}")


def runs(tmp_path: Path, what: str = "start") -> list[str]:
    """The names of the fake checks that have started (or, with what="end", ended) so far, in order."""
    log = tmp_path / "check-runs.log"
    if not log.exists():
        return []
    return [line.split("|", 1)[1] for line in log.read_text().splitlines() if line.startswith(what + "|")]


def new_agent(project: Path, name: str, file: str | None = None, text: str = "extends Node\n") -> None:
    file = file or f"scripts/{name}.gd"
    code, out = praxis("start", name, f"work of {name}", file)
    assert code == 0, out
    edit(folder(project, name) / file, text)


def land(project: Path, name: str, file: str | None = None, text: str = "extends Node\n") -> str:
    new_agent(project, name, file, text)
    code, out = praxis("land", name)
    assert code == 0, out
    return out


def wait_for(condition, what: str, seconds: float = 120):
    end = time.time() + seconds
    while time.time() < end:
        found = condition()
        if found:
            return found
        time.sleep(0.1)
    raise AssertionError(f"timed out waiting for {what}")


def remembered(project: Path) -> dict[str, int]:
    store = Store(project / ".git" / "praxis")
    try:
        return checks.remembered(store)
    finally:
        store.close()


# --- praxis.toml -----------------------------------------------------------------------------------------------------


def test_names_inputs_and_env_inputs_are_read_and_written_back(project):
    set_line(
        project,
        'checks = [{ name = "ui", paths = ["css\\\\**"], inputs = ["./css/**", "js/ui/**"], '
        'env_inputs = [" NODE_ENV "], run = " npm run test:ui " }]',
    )
    cfg = config.load(project)
    assert cfg.checks == [
        {
            "paths": ["css/**"],
            "run": "npm run test:ui",
            "name": "ui",
            "inputs": ["css/**", "js/ui/**"],
            "env_inputs": ["NODE_ENV"],
        }
    ]
    assert cfg.audit_every == 20 and cfg.checks_together is True
    again = config.parse_toml(config.render(Config(checks=cfg.checks, audit_every=7, checks_together=False)))
    assert again["checks"][0]["inputs"] == ["css/**", "js/ui/**"] and again["checks"][0]["name"] == "ui"
    assert again["audit_every"] == 7 and again["checks_together"] is False


@pytest.mark.parametrize(
    "line, words",
    [
        ('checks = [{ paths = ["a"], inputs = ["a"], run = "x" }]', "has inputs but no name"),
        ('checks = [{ paths = ["a"], env_inputs = ["A"], run = "x" }]', "has env_inputs but no name"),
        ('checks = [{ name = "a", paths = ["a"], inputs = [], run = "x" }]', "inputs of checks entry 1 must be"),
        ('checks = [{ name = "a", paths = ["a"], inputs = ["../x"], run = "x" }]', "inputs of checks entry 1 must be"),
        ('checks = [{ name = "a", paths = ["a"], env_inputs = ["A=B"], run = "x" }]', "env_inputs of checks entry 1"),
        ('checks = [{ name = "two words", paths = ["a"], run = "x" }]', "must be a short word"),
        ('checks = [{ name = "a", paths = ["a"], run = "x" }, { name = "A", paths = ["b"], run = "y" }]', "named A"),
        ('checks = [{ name = "a", paths = ["a"], run = "x" }, { paths = ["b"], run = "y" }]', "every entry a name"),
        ("audit_every = -1", "audit_every must be 0"),
    ],
)
def test_a_mistaken_named_entry_is_refused_in_plain_words(project, line, words):
    set_line(project, line)
    with pytest.raises(PraxisError) as err:
        config.load(project)
    assert words in str(err.value)


def test_a_new_project_file_explains_named_checks_and_the_audit():
    text = config.render(Config())
    assert "Named checks" in text and "`inputs`" in text and "audit_every = 20" in text
    assert "checks_together = true" in text and "praxis trust <name>" in text


# --- which checks a landing must pass --------------------------------------------------------------------------------


def _cfg() -> Config:
    return Config(
        check="default",
        checks=[
            {"name": "ui", "paths": ["css/**"], "inputs": ["css/**", "shared/**"], "run": "ui"},
            {"name": "js", "paths": ["js/**", "css/site.css"], "run": "js"},
        ],
    )


def test_every_named_entry_whose_paths_match_runs():
    assert [c.name for c in checks.plan(_cfg(), ["css/site.css"])] == ["ui", "js"]
    assert [c.name for c in checks.plan(_cfg(), ["js/a.js"])] == ["js"]


def test_an_entry_also_runs_when_the_landing_changes_a_file_it_reads_and_the_default_answers_for_unowned_files():
    plan = checks.plan(_cfg(), ["shared/util.js"])  # ui reads it; nobody's `paths` cover it
    assert [(c.name, c.command) for c in plan] == [("ui", "ui"), ("", "default")]
    assert [(c.name, c.command) for c in checks.plan(_cfg(), ["README.md"])] == [("", "default")]
    assert plan[0].inputs == ("css/**", "shared/**") and plan[1].inputs is None


def test_without_names_it_is_still_the_first_match_only():
    cfg = Config(check="default", checks=[{"paths": ["css/**"], "run": "strict"}, {"paths": ["**"], "run": "loose"}])
    plan = checks.plan(cfg, ["css/site.css"])
    assert [c.command for c in plan] == ["strict"] and plan[0].alone and plan[0].inputs is None
    assert checks.plan(Config(), ["a.txt"]) == []


# --- the key a pass is remembered under ------------------------------------------------------------------------------


def _key(project: Path, check: checks.Check, env: dict | None = None) -> str:
    return checks.Keys(project, git(project, "rev-parse", "HEAD"), env or {}).of(check)


def _commit(project: Path, file: str, text: str) -> None:
    edit(project / file, text)
    git(project, "add", "-A")
    git(project, "commit", "-q", "-m", f"change {file}")


def test_the_key_follows_the_files_the_check_reads_and_nothing_else(project):
    ui = checks.Check("ui", "run ui", "check ui", "ui", inputs=("scripts/**",), env_inputs=("MODE",))
    first = _key(project, ui)
    _commit(project, "notes/todo.txt", "something else\n")
    assert _key(project, ui) == first, "a file it does not read changed"
    _commit(project, "scripts/player.gd", "extends Node\nvar speed = 99\n")
    second = _key(project, ui)
    assert second != first, "a file it reads changed"
    _commit(project, "scripts/player.gd", "extends Node\nvar speed = 10\n")
    assert _key(project, ui) == first, "the same contents again are the same key"
    git(project, "mv", "scripts/enemy.gd", "scripts/foe.gd")
    git(project, "commit", "-q", "-m", "rename")
    assert _key(project, ui) != first, "a renamed file is another file"


def test_the_key_follows_the_command_and_the_listed_environment_variables(project):
    ui = checks.Check("ui", "run ui", "check ui", "ui", inputs=("scripts/**",), env_inputs=("MODE",))
    base = _key(project, ui, {"MODE": "fast", "OTHER": "1"})
    assert _key(project, ui, {"MODE": "fast", "OTHER": "2"}) == base, "a variable it does not list"
    assert _key(project, ui, {"MODE": "slow"}) != base
    assert _key(project, ui, {}) != base and _key(project, ui, {"MODE": ""}) != _key(project, ui, {})
    other = checks.Check("ui", "run ui --more", "check ui", "ui", inputs=("scripts/**",), env_inputs=("MODE",))
    assert _key(project, other, {"MODE": "fast", "OTHER": "1"}) != base


def test_without_inputs_a_named_check_reads_the_whole_tree(project):
    whole = checks.Check("all", "run all", "check all", "all")
    first = _key(project, whole)
    _commit(project, "notes/todo.txt", "anything\n")
    assert _key(project, whole) != first


# --- landing ---------------------------------------------------------------------------------------------------------


def test_a_pass_is_reused_while_the_files_the_check_reads_stay_the_same(project, tmp_path):
    use_checks(project, named_check(tmp_path, "scripts", ["scripts/player.gd", "scripts/enemy.gd"], paths=["**"]))
    out = land(project, "alice", "scripts/player.gd", "extends Node\nvar speed = 11\n")
    assert "Running check scripts: " in out and "check passed (scripts)" in out, out
    out = land(project, "bob", "notes/todo.txt", "nothing the check reads\n")
    assert "Skipping check scripts: it passed at " in out and "on exactly the files it reads" in out, out
    assert "Running check" not in out and "check passed (scripts from memory)" in out
    assert runs(tmp_path) == ["scripts"], "the whole project changed, the files it reads did not"
    assert 'bob landed "work of bob" (check passed (scripts from memory))' in praxis("status")[1]
    out = land(project, "carol", "scripts/enemy.gd", "extends Node\nvar hp = 4\n")
    assert "Running check scripts: " in out and runs(tmp_path) == ["scripts", "scripts"], out


def test_every_matching_check_runs_and_each_is_named(project, tmp_path):
    use_checks(
        project,
        named_check(tmp_path, "player", ["scripts/player.gd"]),
        named_check(tmp_path, "all-scripts", ["scripts/player.gd", "scripts/enemy.gd"]),
        named_check(tmp_path, "notes", ["notes/todo.txt"]),
        checks_together="false",
    )
    out = land(project, "alice", "scripts/player.gd", "extends Node\nvar speed = 11\n")
    assert "Running check player: " in out and "Running check all-scripts: " in out, out
    assert runs(tmp_path) == ["player", "all-scripts"] and runs(tmp_path, "end") == ["player", "all-scripts"]
    assert "check passed (player, all-scripts)" in out and "notes" not in out.split("Landed:")[1]
    assert remembered(project) == {"player": 1, "all-scripts": 1}


def test_a_failing_named_check_stops_the_landing_and_only_it_runs_again(project, tmp_path):
    use_checks(
        project,
        named_check(tmp_path, "player", ["scripts/player.gd"], paths=["scripts/**"]),
        named_check(tmp_path, "enemy", ["scripts/enemy.gd"], paths=["scripts/**"]),
        checks_together="false",
    )
    praxis("start", "alice", "both", "scripts")
    edit(folder(project, "alice") / "scripts" / "player.gd", "extends Node\nvar speed = 11\n")
    edit(folder(project, "alice") / "scripts" / "enemy.gd", "BROKEN\n")
    code, out = praxis("land", "alice")
    assert code == 2 and "NOT landed: check enemy failed" in out, out
    assert "Check enemy failed, so nothing landed." in out and "ERROR: broken script scripts/enemy.gd" in out
    assert remembered(project) == {"player": 1}, "the pass is remembered, the failure never is"
    code, out = praxis("land", "alice")  # the same work again: player is known, enemy is not
    assert code == 2 and "Skipping check player: " in out and "Running check enemy: " in out, out
    assert runs(tmp_path) == ["player", "enemy", "enemy"]
    edit(folder(project, "alice") / "scripts" / "enemy.gd", "extends Node\nvar hp = 5\n")
    code, out = praxis("land", "alice")
    assert code == 0 and "check passed (player from memory, enemy)" in out, out
    assert runs(tmp_path) == ["player", "enemy", "enemy", "enemy"]


def test_a_changed_environment_variable_it_lists_runs_the_check_again(project, tmp_path, monkeypatch):
    use_checks(project, named_check(tmp_path, "scripts", ["scripts/player.gd"], paths=["**"], env_inputs=["MODE"]))
    monkeypatch.setenv("MODE", "fast")
    land(project, "alice", "scripts/player.gd", "extends Node\nvar speed = 11\n")
    out = land(project, "bob", "notes/a.txt")
    assert "Skipping check scripts" in out and runs(tmp_path) == ["scripts"]
    monkeypatch.setenv("MODE", "slow")
    out = land(project, "carol", "notes/b.txt")
    assert "Running check scripts: " in out and runs(tmp_path) == ["scripts", "scripts"], out


def test_the_default_check_runs_too_when_no_paths_cover_the_change(project, tmp_path):
    use_checks(
        project, named_check(tmp_path, "player", ["scripts/player.gd"], inputs=["scripts/player.gd", "shared/**"])
    )
    out = land(project, "alice", "shared/util.gd")  # player reads it, but nobody's `paths` cover it
    assert "Running check player: " in out and "Running the default check: " in out, out
    assert "check passed (player, default check)" in out
    out = land(project, "bob", "scripts/player.gd", "extends Node\nvar speed = 12\n")
    assert "Running check player: " in out and "default check" not in out, out


def test_several_checks_run_at_the_same_time_when_a_heavy_job_place_is_free(project, tmp_path):
    gates = tmp_path / "gates"
    gates.mkdir()
    use_checks(
        project,
        named_check(tmp_path, "first", ["scripts/player.gd"], paths=["scripts/**"], gates=gates),
        named_check(tmp_path, "second", ["scripts/enemy.gd"], paths=["scripts/**"], gates=gates),
        named_check(tmp_path, "third", ["scripts/enemy.gd"], paths=["scripts/**"], gates=gates),
        heavy_jobs="1",
    )
    new_agent(project, "alice", "scripts/player.gd", "extends Node\nvar speed = 11\n")
    proc = praxis_process(project, "land", "alice")
    try:
        wait_for(lambda: len(runs(tmp_path)) == 2, "two checks running at once")
        time.sleep(1)
        assert sorted(runs(tmp_path)) == ["first", "second"], "one heavy job place: the third waits"
        store = Store(project / ".git" / "praxis")
        try:
            jobs = store.rows("SELECT agent FROM heavy WHERE started IS NOT NULL")
        finally:
            store.close()
        assert [j["agent"] for j in jobs] == ["alice (check second)"], "the check beside holds the place"
    finally:
        (gates / "all").write_text("")
    out = proc.communicate(timeout=180)[0]
    assert proc.returncode == 0 and "at the same time: a heavy job place is free" in out, out
    assert sorted(runs(tmp_path)) == ["first", "second", "third"]
    assert "check passed (first, second, third)" in out


def test_checks_together_false_runs_them_one_after_another(project, tmp_path):
    use_checks(
        project,
        named_check(tmp_path, "first", ["scripts/player.gd"], paths=["scripts/**"]),
        named_check(tmp_path, "second", ["scripts/enemy.gd"], paths=["scripts/**"]),
        checks_together="false",
    )
    out = land(project, "alice", "scripts/player.gd", "extends Node\nvar speed = 11\n")
    log = (tmp_path / "check-runs.log").read_text().split()
    assert log == ["start|first", "end|first", "start|second", "end|second"] and "at the same time" not in out


def test_status_lists_the_named_checks_and_what_is_remembered(project, tmp_path):
    use_checks(
        project,
        named_check(tmp_path, "scripts", ["scripts/player.gd"], paths=["**"]),
        '{ name = "all", paths = ["docs/**"], run = "true" }',
        audit_every="20",
    )
    land(project, "alice", "scripts/player.gd", "extends Node\nvar speed = 11\n")
    out = praxis("status")[1]
    assert "CHECKS (a landing runs every one of these" in out, out
    assert "paths: **; reads: scripts/player.gd; 1 pass(es) remembered" in out
    assert "paths: docs/**; reads: the whole project; 0 pass(es) remembered" in out
    assert "audit: the first pass taken from memory and then one in 20" in out


# --- batches ---------------------------------------------------------------------------------------------------------


def _land_together(project: Path, names: list[str]) -> tuple[list[int], list[str]]:
    """`praxis land` for each name, all waiting behind a ghost first so that they land as one batch."""
    store = Store(project / ".git" / "praxis")
    ghost = Ticket(store, "ghost", say=lambda _t: None)
    ghost.__enter__()
    procs = []
    try:
        for name in names:
            procs.append(praxis_process(project, "land", name))
            wait_for(lambda n=name: store.rows("SELECT 1 FROM queue WHERE agent = ?", (n,)), f"{name} in the line")
    finally:
        ghost.__exit__(None, None, None)
        store.close()
    outs = [p.communicate(timeout=180)[0] for p in procs]
    return [p.returncode for p in procs], outs


def _area_checks(project: Path, tmp_path: Path, *names: str) -> None:
    use_checks(project, *(named_check(tmp_path, name, [f"scripts/{name}.gd"]) for name in names))


def test_a_batch_whose_checks_are_all_remembered_lands_without_running_them(project, tmp_path):
    _area_checks(project, tmp_path, "alice", "bob")
    new_agent(project, "alice")
    new_agent(project, "bob")
    for name in ("alice", "bob"):  # each earns its pass before the line
        git(folder(project, name), "add", "-A")
        git(folder(project, name), "commit", "-q", "-m", f"work of {name}")
        assert praxis("check", name)[0] == 0
    assert sorted(runs(tmp_path)) == ["alice", "bob"]
    codes, outs = _land_together(project, ["alice", "bob"])
    assert codes == [0, 0], outs
    assert "together" in outs[0] and "Skipping check alice: " in outs[0] and "Skipping check bob: " in outs[0]
    assert sorted(runs(tmp_path)) == ["alice", "bob"], "the batch ran nothing"
    assert "check passed (alice from memory, bob from memory), checked together with alice, bob" in outs[0]
    assert (project / "scripts" / "alice.gd").exists() and (project / "scripts" / "bob.gd").exists()


def test_a_batch_that_fails_runs_only_the_failed_check_again(project, tmp_path):
    _area_checks(project, tmp_path, "bob", "alice")
    set_line(project, "checks_together = false")
    new_agent(project, "alice", text="BROKEN\n")
    new_agent(project, "bob")
    codes, outs = _land_together(project, ["alice", "bob"])
    assert codes == [2, 0], outs
    assert "Check alice failed, so nothing landed." in outs[0] and "Landed: bob's work" in outs[1]
    assert "Skipping check bob: " in outs[0], "for bob alone, the pass the batch earned is taken from memory"
    assert runs(tmp_path).count("bob") == 1, "bob's check passed for the batch: it is not run again for bob alone"
    assert runs(tmp_path).count("alice") == 2, "once for the batch, once for alice alone"
    assert (project / "scripts" / "bob.gd").exists() and not (project / "scripts" / "alice.gd").exists()
