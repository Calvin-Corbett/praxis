from __future__ import annotations

import json

import pytest
from conftest import git, praxis

from agent_praxis import config, instructions
from agent_praxis.config import Config, _mini_toml, detect, render


def test_init_detects_godot_writes_rules_and_commits(project):
    cfg = config.load(project)
    assert "*.tscn" in cfg.assets and "*.png" in cfg.assets
    assert cfg.main_branch == "main"
    for name in ("AGENTS.md", "CLAUDE.md"):
        text = (project / name).read_text()
        assert text.count(instructions.START) == 1 and "praxis land" in text
    assert git(project, "status", "--porcelain") == "", "setup files were committed"
    assert "Set up Praxis" in git(project, "log", "-1", "--format=%s")


def test_init_keeps_existing_text_and_never_duplicates(project):
    agents = project / "AGENTS.md"
    agents.write_text("# My rules\nBe kind.\n\n" + agents.read_text() + "\n## After\nMore of mine.\n")
    praxis("init")
    praxis("init")
    text = agents.read_text()
    assert text.startswith("# My rules\nBe kind.")
    assert text.count(instructions.START) == 1 and text.count(instructions.END) == 1
    assert "More of mine." in text


def test_rules_are_appended_to_a_file_without_them(tmp_path):
    path = tmp_path / "CLAUDE.md"
    path.write_text("Existing notes")
    assert instructions.write_into(path) == "added"
    assert path.read_text().startswith("Existing notes\n\n" + instructions.START)
    assert instructions.write_into(path) == "unchanged"


@pytest.mark.parametrize("files,engine,pattern", [
    ({"Assets/x.txt": "", "ProjectSettings/p.txt": ""}, "Unity", "*.prefab"),
    ({"Game.uproject": "{}"}, "Unreal", "*.uasset"),
    ({"project.godot": ""}, "Godot", "*.tscn"),
    ({"package.json": json.dumps({"scripts": {"test": "vitest"}})}, "Web game", "*.png"),
])
def test_engine_detection(tmp_path, files, engine, pattern):
    for name, text in files.items():
        (tmp_path / name).parent.mkdir(parents=True, exist_ok=True)
        (tmp_path / name).write_text(text)
    engines, assets, check = detect(tmp_path)
    assert engine in engines and pattern in assets
    if engine == "Web game":
        assert check == "npm run test"


def test_config_round_trips_with_and_without_tomllib():
    cfg = Config(check='"C:\\Program Files\\py.exe" check.py # not a comment', assets=["*.png", "Assets/*.unity"])
    text = render(cfg)
    for data in (_mini_toml(text), config.parse_toml(text)):
        assert data["check"] == cfg.check
        assert data["assets"] == cfg.assets
        assert data["enough_after"] == 3 and data["stale_after_hours"] == 24


def test_project_settings_load_without_tomllib(project, monkeypatch):
    import sys

    monkeypatch.setitem(sys.modules, "tomllib", None)  # what Python 3.10 looks like
    cfg = config.load(project)
    assert cfg.main_branch == "main" and "*.tscn" in cfg.assets and "check.py" in cfg.check


def test_commands_explain_when_the_project_is_not_set_up(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    code, out = praxis("status")
    assert code == 1 and "not inside a git project" in out
    git(tmp_path, "init", "-q")
    code, out = praxis("status")
    assert code == 1 and "praxis init" in out
