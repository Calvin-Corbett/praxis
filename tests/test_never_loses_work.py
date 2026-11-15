"""Praxis must never lose, overwrite or wrongly land someone's work. One test per way it once could."""

from __future__ import annotations

import sys

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
    assert code == 1 and "AGENTS.md (has unsaved changes)" in out and "Nothing was changed" in out
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
