"""Praxis must never lose, overwrite or wrongly land someone's work. One test per way it once could."""

from __future__ import annotations

import sys
from pathlib import Path

from conftest import edit, folder, git, praxis

from agent_praxis import config, gitops, instructions


def _saved_branches(project) -> list[str]:
    return [b.strip(" *") for b in git(project, "branch", "--list", "praxis-saved/*").splitlines()]


def test_work_committed_off_the_agents_branch_is_not_landed_wrong_or_deleted(project):
    praxis("start", "alice", "jump", "scripts/player.gd")
    wt = folder(project, "alice")
    git(wt, "switch", "-q", "--detach")
    edit(wt / "scripts" / "player.gd", "extends Node\nvar speed = 42\n")
    git(wt, "commit", "-qam", "work with no branch")
    before = git(project, "rev-parse", "main")

    code, out = praxis("land", "alice")
    assert code == 1 and "detached HEAD" in out and "git switch -C praxis/alice" in out
    assert git(project, "rev-parse", "main") == before, "nothing landed"

    code, out = praxis("done", "alice")
    assert code == 1 and "Nothing was removed" in out and wt.is_dir()

    code, out = praxis("abandon", "alice")
    assert code == 0, out
    kept = [b for b in _saved_branches(project) if b.endswith("-folder")]
    assert kept, out
    assert "speed = 42" in git(project, "show", f"{kept[0]}:scripts/player.gd")


def test_the_advice_for_a_detached_folder_works(project):
    praxis("start", "alice", "jump", "scripts/player.gd")
    wt = folder(project, "alice")
    git(wt, "switch", "-q", "--detach")
    edit(wt / "scripts" / "player.gd", "extends Node\nvar speed = 42\n")
    git(wt, "commit", "-qam", "work with no branch")
    git(wt, "switch", "-q", "-C", "praxis/alice")
    code, out = praxis("land", "alice", "--done")
    assert code == 0, out
    assert "speed = 42" in (project / "scripts" / "player.gd").read_text()


def test_landing_from_another_branch_never_moves_main_backwards(project):
    """The review's case: the folder moved to its own branch, the Praxis branch held older work, the main folder
    was on another branch, and another agent's landed commit vanished from main."""
    praxis("start", "alice", "jump", "scripts/player.gd")
    praxis("start", "bob", "enemies", "scripts/enemy.gd")
    alice = folder(project, "alice")
    edit(alice / "scripts" / "player.gd", "extends Node\nvar speed = 11\n")
    git(alice, "commit", "-qam", "older alice work")
    git(alice, "switch", "-q", "-c", "wip")
    edit(alice / "scripts" / "player.gd", "extends Node\nvar speed = 12\n")
    git(alice, "commit", "-qam", "newer alice work")
    edit(folder(project, "bob") / "scripts" / "enemy.gd", "extends Node\nvar hp = 30\n")
    assert praxis("land", "bob")[0] == 0
    git(project, "switch", "-q", "-c", "side")
    bob_landed = git(project, "rev-parse", "main")

    code, out = praxis("land", "alice")
    assert code == 1 and "on the branch wip" in out
    assert git(project, "rev-parse", "main") == bob_landed
    assert "hp = 30" in git(project, "show", "main:scripts/enemy.gd"), "bob's landed work is still in main"


def test_done_keeps_ignored_files_and_removes_only_tool_caches(project):
    (project / ".gitignore").write_text("*.local\n.godot/\n")
    git(project, "add", ".gitignore")
    git(project, "commit", "-qm", "ignore some files")
    praxis("start", "alice", "jump", "scripts/player.gd")
    wt = folder(project, "alice")
    (wt / "my.local").write_text("private notes")
    (wt / ".godot").mkdir()
    (wt / ".godot" / "cache.bin").write_bytes(b"rebuilt by the engine")

    code, out = praxis("done", "alice")
    assert code == 0, out
    assert not wt.exists()
    kept_root = project / ".git" / "praxis" / "kept"
    kept = list(kept_root.rglob("my.local"))
    assert kept and kept[0].read_text() == "private notes", out
    assert "They were not deleted" in out and ".godot" in out
    assert not list(kept_root.rglob("cache.bin")), "caches are not hoarded"


def test_init_never_commits_your_unsaved_edits_to_agents_md(project):
    agents = project / "AGENTS.md"
    agents.write_text(agents.read_text() + "\nhalf-written private note\n")
    head = git(project, "rev-parse", "HEAD")
    code, out = praxis("init")
    assert code == 1 and "not committed yet, and committing would include them: AGENTS.md" in out
    assert "Nothing was changed" in out
    assert git(project, "rev-parse", "HEAD") == head
    assert "half-written private note" in agents.read_text(), "the edit is still there, uncommitted"
    assert "half-written" not in git(project, "show", "HEAD:AGENTS.md")


def test_init_runs_again_with_other_staged_work_and_leaves_it_staged(project):
    claude = project / "CLAUDE.md"
    claude.write_text("Just my notes\n")
    git(project, "commit", "-qm", "my notes", "--", "CLAUDE.md")
    edit(project / "scripts" / "enemy.gd", "extends Node\nvar hp = 9\n")
    git(project, "add", "scripts/enemy.gd")

    code, out = praxis("init")
    assert code == 0, out
    assert instructions.START in git(project, "show", "HEAD:CLAUDE.md"), "the rules were committed"
    assert "hp = 3" in git(project, "show", "HEAD:scripts/enemy.gd"), "the other staged work was not committed"
    assert git(project, "diff", "--cached", "--name-only") == "scripts/enemy.gd", "and it is still staged"


def test_init_runs_again_when_nothing_of_its_own_changed_but_other_work_is_staged(project):
    edit(project / "scripts" / "enemy.gd", "extends Node\nvar hp = 9\n")
    git(project, "add", "scripts/enemy.gd")
    head = git(project, "rev-parse", "HEAD")
    code, out = praxis("init")
    assert code == 0, out
    assert git(project, "rev-parse", "HEAD") == head, "nothing to commit, so no commit"
    assert git(project, "diff", "--cached", "--name-only") == "scripts/enemy.gd"


def test_init_leaves_an_ignored_claude_md_alone(tmp_path, monkeypatch):
    root = tmp_path / "game"
    root.mkdir()
    git(root, "init", "-q", "-b", "main")
    git(root, "config", "user.name", "Test")
    git(root, "config", "user.email", "test@example.com")
    (root / ".gitignore").write_text("CLAUDE.md\n")
    (root / "CLAUDE.md").write_text("private\n")
    git(root, "add", ".gitignore")
    git(root, "commit", "-qm", "first")
    monkeypatch.chdir(root)

    code, out = praxis("init")
    assert code == 0, out
    assert (root / "CLAUDE.md").read_text() == "private\n"
    assert "Left CLAUDE.md alone" in out
    assert git(root, "status", "--porcelain") == "", "nothing left half-staged"
    assert "AGENTS.md" in git(root, "ls-files")


def test_rules_keep_a_files_windows_line_endings(tmp_path):
    path = tmp_path / "AGENTS.md"
    path.write_bytes(b"# Mine\r\nLine two\r\n")
    assert instructions.write_into(path) == "added"
    data = path.read_bytes()
    assert data.startswith(b"# Mine\r\nLine two\r\n")
    assert b"\n" not in data.replace(b"\r\n", b""), "every line ending is still CRLF"
    assert instructions.write_into(path) == "unchanged"


def test_a_check_that_changes_files_does_not_land_untested_work(project):
    (project / "rewrite.py").write_text('open("scripts/enemy.gd", "a").write("# touched by the check\\n")\n')
    git(project, "add", "rewrite.py")
    git(project, "commit", "-qm", "a check that edits a file")
    cfg = config.load(project)
    cfg.check = f'"{sys.executable}" rewrite.py'
    (project / config.FILE_NAME).write_text(config.render(cfg))
    praxis("start", "alice", "jump", "scripts/player.gd")
    edit(folder(project, "alice") / "scripts" / "player.gd", "extends Node\nvar speed = 20\n")
    before = git(project, "rev-parse", "main")

    code, out = praxis("land", "alice")
    assert code == 2 and "files changed while the check ran" in out and "scripts/enemy.gd" in out
    assert git(project, "rev-parse", "main") == before


def test_landing_does_not_break_a_rebase_of_main_in_the_main_folder(project):
    praxis("start", "alice", "jump", "scripts/player.gd")
    edit(folder(project, "alice") / "scripts" / "player.gd", "extends Node\nvar speed = 20\n")
    git(project, "branch", "side")
    git(project, "switch", "-q", "side")
    edit(project / "scripts" / "enemy.gd", "extends Node\nvar hp = 5\n")
    git(project, "commit", "-qam", "side change")
    git(project, "switch", "-q", "main")
    edit(project / "scripts" / "enemy.gd", "extends Node\nvar hp = 7\n")
    git(project, "commit", "-qam", "main change")
    assert gitops.git(["rebase", "side"], project, check=False).returncode != 0, "the rebase stops on a clash"
    before = git(project, "rev-parse", "refs/heads/main")

    code, out = praxis("land", "alice")
    assert code == 2 and "middle of a git rebase" in out
    assert git(project, "rev-parse", "refs/heads/main") == before
    assert gitops.git(["rebase", "--abort"], project, check=False).returncode == 0, "the human's rebase still works"
    assert git(project, "rev-parse", "HEAD") == before


def test_paths_are_read_from_the_folder_you_are_in(project, monkeypatch):
    monkeypatch.chdir(project / "scripts")
    code, out = praxis("start", "alice", "jump", "player.gd")
    assert code == 0 and "scripts/player.gd" in out, out


def test_a_path_written_from_the_top_still_works_from_a_subfolder(project, monkeypatch):
    praxis("start", "alice", "jump", "scripts/player.gd")
    monkeypatch.chdir(folder(project, "alice") / "scripts")
    code, out = praxis("claim", "alice", "scripts/enemy.gd")
    assert code == 0 and "  scripts/enemy.gd  (claimed by alice)" in out, out
    code, out = praxis("claim", "alice", "brand_new.gd")
    assert code == 0 and "scripts/brand_new.gd" in out and "does not exist yet" in out


def test_done_never_deletes_real_work_in_folders_with_cache_like_names(project):
    """The re-review's case: a name-based cache list deleted build/, saved/ and a settings file inside build/."""
    (project / ".gitignore").write_text("*.local\nbuild/\nsaved/\nout/\n.godot/\n")
    git(project, "add", ".gitignore")
    git(project, "commit", "-qm", "ignore some folders")
    praxis("start", "alice", "jump", "scripts/player.gd")
    wt = folder(project, "alice")
    for rel in ("build/release.zip", "saved/autosave.umap", "out/notes.txt", "scripts/build/notes.local"):
        edit(wt / rel, f"real work in {rel}")
    (wt / ".godot").mkdir()
    (wt / ".godot" / "cache.bin").write_bytes(b"rebuilt by the engine")

    code, out = praxis("done", "alice")
    assert code == 0, out
    kept_root = project / ".git" / "praxis" / "kept"
    for rel in ("build/release.zip", "saved/autosave.umap", "out/notes.txt", "scripts/build/notes.local"):
        found = list(kept_root.glob(f"*/{rel}"))
        assert found and found[0].read_text() == f"real work in {rel}", (rel, out)
    assert not list(kept_root.rglob("cache.bin")), "the Godot cache is the one thing removed"


def test_a_check_allowed_to_rewrite_a_file_lands_the_checked_commit(project):
    (project / "rewrite.py").write_text('open("scripts/enemy.gd", "a").write("# stamp\\n")\n')
    git(project, "add", "rewrite.py")
    git(project, "commit", "-qm", "a check that stamps a file")
    cfg = config.load(project)
    cfg.check = f'"{sys.executable}" rewrite.py'
    cfg.check_may_change = ["scripts/enemy.gd"]
    (project / config.FILE_NAME).write_text(config.render(cfg))
    assert config.load(project).check_may_change == ["scripts/enemy.gd"], "the setting round-trips"
    praxis("start", "alice", "jump", "scripts/player.gd")
    wt = folder(project, "alice")
    edit(wt / "scripts" / "player.gd", "extends Node\nvar speed = 20\n")

    code, out = praxis("land", "alice")
    assert code == 0, out
    assert "speed = 20" in git(project, "show", "main:scripts/player.gd")
    assert "# stamp" not in git(project, "show", "main:scripts/enemy.gd"), "the check's rewrite did not land"
    assert git(wt, "status", "--porcelain") == "", "and the folder is clean again"


def test_the_advice_for_a_rewriting_check_is_the_setting_that_fixes_it(project):
    (project / "rewrite.py").write_text('open("scripts/enemy.gd", "a").write("# stamp\\n")\n')
    git(project, "add", "rewrite.py")
    git(project, "commit", "-qm", "a check that stamps a file")
    cfg = config.load(project)
    cfg.check = f'"{sys.executable}" rewrite.py'
    (project / config.FILE_NAME).write_text(config.render(cfg))
    praxis("start", "alice", "jump", "scripts/player.gd")
    edit(folder(project, "alice") / "scripts" / "player.gd", "extends Node\nvar speed = 20\n")
    code, out = praxis("land", "alice")
    assert code == 2 and 'check_may_change = ["scripts/enemy.gd"]' in out


def test_done_and_abandon_work_with_a_submodule(project, tmp_path):
    lib = tmp_path / "addon"
    lib.mkdir()
    git(lib, "init", "-q", "-b", "main")
    git(lib, "config", "user.name", "Test")
    git(lib, "config", "user.email", "test@example.com")
    (lib / "addon.gd").write_text("extends Node\n")
    git(lib, "add", "-A")
    git(lib, "commit", "-qm", "addon")
    git(project, "-c", "protocol.file.allow=always", "submodule", "add", "-q", str(lib), "addons/thing")
    git(project, "commit", "-qm", "add an addon")
    for agent in ("alice", "bob"):
        praxis("start", agent, "work", f"scripts/{'player' if agent == 'alice' else 'enemy'}.gd")
        git(folder(project, agent), "-c", "protocol.file.allow=always", "submodule", "update", "-q", "--init")
        assert (folder(project, agent) / "addons" / "thing" / "addon.gd").is_file()
    code, out = praxis("done", "alice")
    assert code == 0, out
    assert not folder(project, "alice").exists()
    code, out = praxis("abandon", "bob")
    assert code == 0, out
    assert not folder(project, "bob").exists()


def test_done_keeps_a_submodule_commit_that_exists_nowhere_else(project, tmp_path):
    lib = tmp_path / "addon"
    lib.mkdir()
    git(lib, "init", "-q", "-b", "main")
    git(lib, "config", "user.name", "Test")
    git(lib, "config", "user.email", "test@example.com")
    (lib / "addon.gd").write_text("extends Node\n")
    git(lib, "add", "-A")
    git(lib, "commit", "-qm", "addon")
    git(project, "-c", "protocol.file.allow=always", "submodule", "add", "-q", str(lib), "addons/thing")
    git(project, "commit", "-qm", "add an addon")
    praxis("start", "alice", "addon work", "addons")
    wt = folder(project, "alice")
    git(wt, "-c", "protocol.file.allow=always", "submodule", "update", "-q", "--init")
    sub = wt / "addons" / "thing"
    git(sub, "config", "user.name", "Test")
    git(sub, "config", "user.email", "test@example.com")
    (sub / "addon.gd").write_text("extends Node\nvar new = 1\n")
    git(sub, "commit", "-qam", "only in this folder")
    assert praxis("land", "alice")[0] == 0, "the new submodule commit is recorded in main"

    code, out = praxis("done", "alice")
    assert code == 1 and "exist nowhere else: addons/thing" in out and "Nothing was removed" in out
    assert sub.is_dir()


def test_init_adds_rules_to_your_own_uncommitted_claude_md_without_committing_it(tmp_path, monkeypatch):
    root = tmp_path / "game"
    root.mkdir()
    git(root, "init", "-q", "-b", "main")
    git(root, "config", "user.name", "Test")
    git(root, "config", "user.email", "test@example.com")
    (root / "game.txt").write_text("a game\n")
    git(root, "add", "-A")
    git(root, "commit", "-qm", "first")
    (root / "CLAUDE.md").write_text("# Notes Claude Code wrote\n")
    monkeypatch.chdir(root)

    code, out = praxis("init")
    assert code == 0, out
    text = (root / "CLAUDE.md").read_text()
    assert text.startswith("# Notes Claude Code wrote") and instructions.START in text
    assert "CLAUDE.md" not in git(root, "ls-files"), "your own file was not committed"
    assert "git add CLAUDE.md" in out and "undo" not in out
    assert "AGENTS.md" in git(root, "ls-files")


def test_done_explains_a_half_finished_rebase_instead_of_wrong_advice(project):
    praxis("start", "alice", "jump", "scripts/player.gd")
    wt = folder(project, "alice")
    edit(wt / "scripts" / "player.gd", "extends Node\nvar speed = 1\n")
    git(wt, "commit", "-qam", "alice")
    edit(project / "scripts" / "player.gd", "extends Node\nvar speed = 2\n")
    git(project, "commit", "-qam", "main moved")
    assert gitops.git(["rebase", "main"], wt, check=False).returncode != 0
    code, out = praxis("done", "alice")
    assert code == 1 and "half-finished git rebase" in out and "git switch" not in out
    assert wt.is_dir()


def _addon(project, tmp_path, tag: bool = False):
    """A small git project used as a submodule at addons/thing (optionally pinned to a tag on no branch)."""
    lib = tmp_path / "addon"
    lib.mkdir()
    git(lib, "init", "-q", "-b", "main")
    git(lib, "config", "user.name", "Test")
    git(lib, "config", "user.email", "test@example.com")
    (lib / "addon.gd").write_text("extends Node\n")
    git(lib, "add", "-A")
    git(lib, "commit", "-qm", "addon")
    if tag:
        git(lib, "switch", "-q", "--detach")
        (lib / "addon.gd").write_text("extends Node\nvar released = 1\n")
        git(lib, "commit", "-qam", "release")
        git(lib, "-c", "tag.gpgSign=false", "tag", "v1")
        git(lib, "switch", "-q", "main")
    git(project, "-c", "protocol.file.allow=always", "submodule", "add", "-q", str(lib), "addons/thing")
    if tag:
        git(project / "addons" / "thing", "checkout", "-q", "v1")
        git(project, "add", "addons/thing")
    git(project, "commit", "-qm", "add an addon")


def _start_with_addon(project, agent: str) -> Path:
    praxis("start", agent, "work", "scripts/player.gd")
    wt = folder(project, agent)
    git(wt, "-c", "protocol.file.allow=always", "submodule", "update", "-q", "--init")
    return wt


def test_abandon_never_deletes_uncommitted_edits_inside_a_submodule(project, tmp_path):
    _addon(project, tmp_path)
    wt = _start_with_addon(project, "alice")
    (wt / "addons" / "thing" / "addon.gd").write_text("extends Node\nvar my_edit = 1\n")
    for command in ("abandon", "done"):
        code, out = praxis(command, "alice")
        assert code == 1 and "not committed inside them: addons/thing" in out, (command, out)
        assert "my_edit" in (wt / "addons" / "thing" / "addon.gd").read_text()


def test_ignored_files_inside_a_submodule_are_kept(project, tmp_path):
    _addon(project, tmp_path)
    wt = _start_with_addon(project, "alice")
    sub = wt / "addons" / "thing"
    exclude = gitops.out(["rev-parse", "--git-path", "info/exclude"], sub)
    exclude_path = Path(exclude) if Path(exclude).is_absolute() else sub / exclude
    exclude_path.parent.mkdir(parents=True, exist_ok=True)
    exclude_path.write_text("*.local\n")
    (sub / "my.local").write_text("my addon settings")
    code, out = praxis("done", "alice")
    assert code == 0, out
    kept = list((project / ".git" / "praxis" / "kept").glob("*/addons/thing/my.local"))
    assert kept and kept[0].read_text() == "my addon settings", out


def test_a_submodule_pinned_to_a_tag_does_not_block_done(project, tmp_path):
    _addon(project, tmp_path, tag=True)
    _start_with_addon(project, "alice")
    code, out = praxis("done", "alice")
    assert code == 0, out


def test_check_may_change_reads_paths_from_the_top_and_keeps_a_copy(project):
    (project / "rewrite.py").write_text(
        'import pathlib\n'
        'pathlib.Path("imported.cfg").write_text("stamp\\n")\n'
        'pathlib.Path("scripts/level.cfg").write_text("the check wrote this\\n")\n'
    )
    (project / "imported.cfg").write_text("committed\n")
    (project / "scripts" / "level.cfg").write_text("real level settings\n")
    git(project, "add", "-A")
    git(project, "commit", "-qm", "a check that rewrites two cfg files")
    cfg = config.load(project)
    cfg.check = f'"{sys.executable}" rewrite.py'
    cfg.check_may_change = ["*.cfg"]  # the top-level one only, not scripts/level.cfg
    (project / config.FILE_NAME).write_text(config.render(cfg))
    praxis("start", "alice", "jump", "scripts/player.gd")
    edit(folder(project, "alice") / "scripts" / "player.gd", "extends Node\nvar speed = 20\n")

    code, out = praxis("land", "alice")
    assert code == 2 and "scripts/level.cfg" in out, "a file deeper down is not covered by *.cfg"
    assert "Put back files the check rewrote" in out and "imported.cfg" in out
    copies = list((project / ".git" / "praxis" / "kept").glob("alice-*-check/imported.cfg"))
    assert copies and copies[0].read_text() == "stamp\n"


def test_a_failed_check_never_leaves_its_rewrites_to_be_landed(project):
    (project / "rewrite.py").write_text(
        'import pathlib, sys\n'
        'pathlib.Path("imported.cfg").write_text("stamp\\n")\n'
        'sys.exit(1 if "BROKEN" in pathlib.Path("scripts/player.gd").read_text() else 0)\n'
    )
    (project / "imported.cfg").write_text("committed\n")
    git(project, "add", "-A")
    git(project, "commit", "-qm", "a check that stamps a file")
    cfg = config.load(project)
    cfg.check = f'"{sys.executable}" rewrite.py'
    cfg.check_may_change = ["imported.cfg"]
    (project / config.FILE_NAME).write_text(config.render(cfg))
    praxis("start", "alice", "jump", "scripts/player.gd")
    wt = folder(project, "alice")
    edit(wt / "scripts" / "player.gd", "BROKEN\n")
    assert praxis("land", "alice")[0] == 2
    edit(wt / "scripts" / "player.gd", "extends Node\nvar speed = 20\n")
    code, out = praxis("land", "alice")
    assert code == 0, out
    assert git(project, "show", "main:imported.cfg") == "committed", "the check's stamp never landed"


def test_patterns_from_the_top(tmp_path):
    from agent_praxis.paths import matches_from_top
    assert matches_from_top("imported.cfg", ["*.cfg"])
    assert not matches_from_top("scripts/level.cfg", ["*.cfg"])
    assert matches_from_top("scripts/level.cfg", ["**/*.cfg"])
    assert matches_from_top("ProjectSettings/Graphics.asset", ["ProjectSettings/*.asset"])
    assert not matches_from_top("Assets/ProjectSettings/x.asset", ["ProjectSettings/*.asset"])
    assert matches_from_top("Packages/packages-lock.json", ["packages/packages-lock.json"]), "case does not matter"


def test_unclaim_says_when_nothing_was_held(project):
    praxis("start", "alice", "jump", "scripts/player.gd")
    code, out = praxis("unclaim", "alice", "scripts/player.gd", "scripts/enemy.gd")
    assert code == 0
    assert "let go of: scripts/player.gd" in out and "did not hold: scripts/enemy.gd" in out
