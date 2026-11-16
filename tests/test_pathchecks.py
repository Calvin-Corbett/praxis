"""`checks` in praxis.toml: the check a landing runs is chosen by the files it changes."""

from __future__ import annotations

import sys

import pytest
from conftest import edit, folder, praxis

from agent_praxis import config
from agent_praxis.config import Config
from agent_praxis.errors import PraxisError


def _says(text: str) -> str:
    """A check command (for praxis.toml, in single quotes) that prints *text* and succeeds."""
    return f'"{sys.executable}" -c "print(0)" {text}'  # the log's first line echoes the command, text included


def _add_to_toml(project, line: str) -> None:
    toml = project / "praxis.toml"
    key = line.split("=")[0].strip()
    kept = [ln for ln in toml.read_text().splitlines() if not ln.startswith(key + " =")]
    toml.write_text("\n".join([*kept, line]) + "\n")


def _checks_line(*entries: tuple[list[str], str]) -> str:
    items = ", ".join(
        "{ paths = [" + ", ".join(f'"{p}"' for p in paths) + "], run = '" + run + "' }" for paths, run in entries
    )
    return f"checks = [{items}]"


# The choice itself --------------------------------------------------------------------------------------------------


def _cfg() -> Config:
    return Config(
        check="default",
        checks=[{"paths": ["js/thermo.js", "css/**"], "run": "strict"}, {"paths": ["js/*.js"], "run": "medium"}],
    )


def test_the_first_matching_entry_wins():
    cfg = _cfg()
    assert cfg.check_for(["js/thermo.js"])[0] == "strict"
    assert cfg.check_for(["js/other.js", "js/thermo.js"])[0] == "strict", "order of the files does not matter"
    assert cfg.check_for(["js/other.js"])[0] == "medium"


def test_a_double_star_spans_folders_and_a_single_star_does_not():
    cfg = _cfg()
    assert cfg.check_for(["css/a/b/site.css"])[0] == "strict"
    assert cfg.check_for(["js/sub/x.js"])[0] == "default"


def test_nothing_matching_runs_the_default_check():
    command, name = _cfg().check_for(["README.md"])
    assert command == "default" and name == "default check"


def test_without_checks_it_is_always_the_default():
    assert Config(check="default").check_for(["a.txt"]) == ("default", "default check")


def test_the_chosen_entry_is_named():
    assert "check 1 of `checks`" in _cfg().check_for(["css/x.css"])[1]
    assert "check 2" in _cfg().check_for(["js/a.js"])[1]


# Reading praxis.toml ------------------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "line, words",
    [
        ('checks = [{ paths = ["a"] }]', "needs run"),
        ('checks = [{ run = "x" }]', "needs paths"),
        ('checks = [{ paths = [], run = "x" }]', "needs paths"),
        ('checks = [{ paths = ["a"], run = "x", extra = 1 }]', "does not know: extra"),
        ('checks = ["a"]', "must be a table"),
        ('checks = [{ paths = ["../a"], run = "x" }]', "needs paths"),
        ('checks = "x"', "must be a list of tables"),
    ],
)
def test_a_mistaken_checks_entry_is_refused_in_plain_words(project, line, words):
    _add_to_toml(project, line)
    with pytest.raises(PraxisError) as err:
        config.load(project)
    assert words in str(err.value) or "list of tables" in str(err.value)


def test_checks_are_read_and_paths_are_cleaned(project):
    _add_to_toml(project, 'checks = [{ paths = ["js\\\\thermo.js", "./css/**"], run = "  run it  " }]')
    cfg = config.load(project)
    assert cfg.checks == [{"paths": ["js/thermo.js", "css/**"], "run": "run it"}]


def test_a_new_project_file_has_the_checks_documented():
    text = config.render(Config())
    assert "FIRST entry" in text and "checks = []" in text
    assert config.parse_toml(text)["checks"] == []


# Landing ------------------------------------------------------------------------------------------------------------


def test_a_landing_runs_the_check_its_files_choose_and_names_it(project):
    _add_to_toml(project, _checks_line((["scripts/enemy.gd"], _says("STRICT-CHECK-RAN"))))
    praxis("start", "alice", "enemy", "scripts/enemy.gd")
    edit(folder(project, "alice") / "scripts" / "enemy.gd", "extends Node\nvar hp = 9\n")
    code, out = praxis("land", "alice")
    assert code == 0, out
    assert "Running the check (check 1 of `checks` (scripts/enemy.gd))" in out
    assert "check passed (check 1 of `checks` (scripts/enemy.gd))" in out
    assert "alice landed" in praxis("status")[1] and "check 1 of `checks`" in praxis("status")[1]
    log = sorted((project / ".git" / "praxis" / "logs").glob("alice-*.log"))[-1].read_text()
    assert "STRICT-CHECK-RAN" in log


def test_other_files_get_the_default_check(project):
    _add_to_toml(project, _checks_line((["scripts/enemy.gd"], _says("STRICT-CHECK-RAN"))))
    praxis("start", "bob", "player", "scripts/player.gd")
    edit(folder(project, "bob") / "scripts" / "player.gd", "extends Node\nvar speed = 11\n")
    code, out = praxis("land", "bob")
    assert code == 0, out
    assert "Running the check (default check)" in out
    assert "check passed (default check)" in out


def test_a_failing_entry_stops_the_landing(project):
    failing = f'"{sys.executable}" -c "import sys; sys.exit(1)" STRICT-SAID-NO'
    _add_to_toml(project, _checks_line((["scripts/**"], failing)))
    praxis("start", "alice", "enemy", "scripts/enemy.gd")
    edit(folder(project, "alice") / "scripts" / "enemy.gd", "extends Node\nvar hp = 9\n")
    code, out = praxis("land", "alice")
    assert code == 2 and "NOT landed: the check failed" in out and "STRICT-SAID-NO" in out


def test_without_checks_the_output_is_unchanged(project):
    praxis("start", "alice", "jump", "scripts/player.gd")
    edit(folder(project, "alice") / "scripts" / "player.gd", "extends Node\nvar speed = 12\n")
    code, out = praxis("land", "alice")
    assert code == 0 and "Running the check: " in out and "(check passed)" in out
    assert "default check" not in out and "same tree" not in out


def test_status_lists_the_checks(project):
    _add_to_toml(project, _checks_line((["css/**"], "npm run ui")))
    out = praxis("status")[1]
    assert "CHECKS" in out and "1. css/**: npm run ui" in out and "default: " in out
