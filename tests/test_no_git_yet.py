"""`praxis init` in a folder that has never used git: it sets git up itself and never touches a file."""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

import pytest
from conftest import folder, git, praxis

from agent_praxis import newgit


@pytest.fixture(autouse=True)
def plain_git(tmp_path_factory, monkeypatch):
    """A global git config of our own (never the real one), with the Git for Windows default autocrlf=true."""
    cfg = tmp_path_factory.mktemp("globalcfg") / "gitconfig"
    cfg.write_text("[core]\n\tautocrlf = true\n")
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(cfg))
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
    monkeypatch.setattr(newgit, "_git_lfs_works", lambda here: False)  # the LFS test below turns it back on


def make(root: Path, files: dict[str, bytes | str]) -> Path:
    for name, data in files.items():
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data if isinstance(data, bytes) else data.encode())
    return root


def snapshot(root: Path) -> dict[str, tuple[bytes, int]]:
    found = {}
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d != ".git"]
        for name in filenames:
            path = Path(dirpath) / name
            found[path.relative_to(root).as_posix()] = (path.read_bytes(), path.stat().st_mtime_ns)
    return found


GODOT = {
    "project.godot": "[application]\n",
    "scenes/level1.tscn": b"[gd_scene]\r\n\x00\x01",
    "scripts/player.gd": "extends Node\n",
    "art/Hero.PNG": b"\x89PNG fake",
    ".godot/imported/hero.ctex": b"cache" * 50,
    "addons/tool/Build/keep.gd": "# a folder of yours named Build is never ignored\n",
    "saves/slot1.sav": b"player save",
    "notes.log": "a log\n",
}


def test_plain_godot_folder_becomes_a_git_project_without_touching_a_file(tmp_path, monkeypatch):
    root = make(tmp_path / "My Game", GODOT)
    before = snapshot(root)
    monkeypatch.chdir(root)
    code, out = praxis("init")
    assert code == 0, out
    after = snapshot(root)
    for name, data in before.items():
        assert after[name] == data, f"{name} was changed"
    assert set(after) - set(before) == {".gitignore", "praxis.toml", "AGENTS.md", "CLAUDE.md"}
    ignore = (root / ".gitignore").read_text()
    assert "/.godot/" in ignore and "Thumbs.db" in ignore and "*.log" in ignore
    assert git(root, "log", "--format=%s") == "Set up Praxis for several AI agents\n" + newgit.FIRST_COMMIT
    saved = git(root, "ls-files").splitlines()
    assert "scenes/level1.tscn" in saved and "addons/tool/Build/keep.gd" in saved and "saves/slot1.sav" in saved
    assert not any(p.startswith(".godot/") for p in saved), "the engine cache was committed"
    assert "notes.log" not in saved
    assert git(root, "status", "--porcelain") == ""
    assert git(root, "config", "core.autocrlf") == "false"
    for text in (
        "Created: .git, .gitignore.",
        "Saved 7 files",
        "Git leaves out: .godot/",
        "backup",
        "Git LFS is not installed",
        "saves/ looks like player saves",
        "Line endings are kept",
    ):
        assert text in out, out


def test_line_endings_stay_byte_exact_in_the_commit_and_in_agent_folders(tmp_path, monkeypatch):
    root = make(
        tmp_path / "web game",
        {
            "package.json": json.dumps({"name": "g"}),
            "src/crlf.js": b"let a = 1;\r\nlet b = 2;\r\n",
            "src/lf.js": b"let a = 1;\nlet b = 2;\n",
            "src/mixed.txt": b"one\r\ntwo\nthree\r\n",
        },
    )
    monkeypatch.chdir(root)
    code, out = praxis("init", "--check", "")
    assert code == 0, out
    assert not (root / ".gitattributes").exists()
    for name in ("src/crlf.js", "src/lf.js", "src/mixed.txt"):
        blob = subprocess.run(["git", "show", f"HEAD:{name}"], cwd=root, capture_output=True, check=True).stdout
        assert blob == (root / name).read_bytes(), f"{name} changed in the commit"
    code, out = praxis("start", "a1", "edit things", "src/lf.js")
    assert code == 0, out
    agent = folder(root, "a1")
    for name in ("src/crlf.js", "src/lf.js", "src/mixed.txt"):
        assert (agent / name).read_bytes() == (root / name).read_bytes(), f"{name} differs in the agent folder"


def test_web_ignores_match_nested_folders_and_only_committed_big_files_are_warned(tmp_path, monkeypatch):
    monkeypatch.setattr(newgit, "BIG_FILE", 1000)
    root = make(
        tmp_path / "game",
        {
            "package.json": "{}",
            "node_modules/electron/dist/electron.exe": b"x" * 5000,
            "app/node_modules/x/index.js": "x",
            "app/dist/out.js": "x",
            "assets/music.ogg": b"m" * 3000,
            "main.js": "x",
        },
    )
    monkeypatch.chdir(root)
    code, out = praxis("init", "--check", "")
    assert code == 0, out
    assert git(root, "ls-files", "app", "node_modules") == ""
    warning = out.split("WARNING:", 1)[1].split("Saved", 1)[0]
    assert "assets/music.ogg" in warning and "electron.exe" not in warning


def test_an_existing_gitignore_is_kept_byte_for_byte(tmp_path, monkeypatch):
    mine = b"# mine\r\ncache/\r\n"
    root = make(
        tmp_path / "game",
        {
            "project.godot": "",
            ".gitignore": mine,
            "cache/big.bin": b"c" * 100,
            ".godot/x": "kept because my .gitignore does not list it",
        },
    )
    monkeypatch.chdir(root)
    code, out = praxis("init")
    assert code == 0, out
    assert (root / ".gitignore").read_bytes() == mine
    assert "Kept your own .gitignore" in out
    saved = git(root, "ls-files").splitlines()
    assert ".godot/x" in saved and "cache/big.bin" not in saved


def test_a_failure_before_the_first_commit_takes_away_only_what_praxis_added(tmp_path, monkeypatch):
    root = make(tmp_path / "game", GODOT)
    before = snapshot(root)
    monkeypatch.setattr(newgit.shutil, "disk_usage", lambda p: type("U", (), {"free": 10})())
    monkeypatch.chdir(root)
    code, out = praxis("init")
    assert code == 1 and "free" in out and "as it was" in out
    assert not (root / ".git").exists() and not (root / ".gitignore").exists()
    assert snapshot(root) == before


def test_a_project_folder_inside_another_git_project_is_still_refused(tmp_path, monkeypatch):
    outer = make(tmp_path / "home", {"notes.txt": "x"})
    git(outer, "init", "-q")
    git(outer, "add", "-A")
    git(outer, "-c", "user.name=T", "-c", "user.email=t@e", "commit", "-qm", "home")
    inner = make(outer / "games" / "tiny", {"project.godot": ""})
    monkeypatch.chdir(inner)
    code, out = praxis("init")
    assert code == 1 and "no git of its own" in out
    assert not (inner / ".git").exists()


def test_home_folder_and_a_folder_inside_a_game_are_refused(tmp_path, monkeypatch):
    game = make(tmp_path / "game", {"project.godot": "", "scripts/a.gd": "x"})
    monkeypatch.chdir(game / "scripts")
    code, out = praxis("init")
    assert code == 1 and "part of the Godot project" in out and not (game / "scripts" / ".git").exists()
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
    monkeypatch.chdir(tmp_path)
    code, out = praxis("init")
    assert code == 1 and "home folder" in out and not (tmp_path / ".git").exists()


def test_other_commands_say_to_run_praxis_init_and_create_nothing(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    for args in (("status",), ("start", "a", "task", "x"), ("land", "a")):
        code, out = praxis(*args)
        assert code == 1 and "not inside a git project" in out and "praxis init" in out, out
    assert not (tmp_path / ".git").exists()


def test_no_commit_sets_git_up_but_saves_nothing(tmp_path, monkeypatch):
    root = make(tmp_path / "game", {"project.godot": "", "a.gd": "x"})
    monkeypatch.chdir(root)
    code, out = praxis("init", "--no-commit")
    assert code == 0, out
    assert (root / ".git").is_dir() and "git add -A" in out
    assert (
        subprocess.run(["git", "rev-parse", "--verify", "HEAD"], cwd=root, capture_output=True, check=False).returncode
        != 0
    )


def _real_lfs() -> bool:
    try:
        return subprocess.run(["git", "lfs", "version"], capture_output=True, check=False).returncode == 0
    except OSError:
        return False


@pytest.mark.skipif(not _real_lfs(), reason="git-lfs is not installed")
def test_with_git_lfs_big_binary_types_are_kept_in_lfs(tmp_path, monkeypatch):
    monkeypatch.setattr(newgit, "_git_lfs_works", lambda here: True)  # undo the fixture: the real git-lfs runs
    root = make(
        tmp_path / "game", {"project.godot": "", "art/Hero.PNG": b"\x89PNG" + b"p" * 200, "scripts/a.gd": "x\r\n"}
    )
    before = snapshot(root)
    monkeypatch.chdir(root)
    code, out = praxis("init")
    assert code == 0, out
    attrs = (root / ".gitattributes").read_text()
    assert "filter=lfs" in attrs and "text=auto" not in attrs and "-text" in attrs
    assert "lfs" in git(root, "check-attr", "filter", "art/Hero.PNG")
    pointer = subprocess.run(
        ["git", "cat-file", "-p", "HEAD:art/Hero.PNG"], cwd=root, capture_output=True, check=True
    ).stdout
    assert pointer.startswith(b"version https://git-lfs"), "the picture was not stored in LFS"
    after = snapshot(root)
    assert all(after[name] == data for name, data in before.items())
