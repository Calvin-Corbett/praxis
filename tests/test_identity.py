"""A warning when an agent folder would commit as someone other than the main branch's recent author."""

from __future__ import annotations

from conftest import git, praxis


def _other_identity(project) -> None:
    git(project, "config", "user.name", "Someone Else")
    git(project, "config", "user.email", "else@example.org")


def test_no_warning_when_the_identity_matches_the_projects_author(project):
    code, out = praxis("start", "alice", "jump", "scripts/player.gd")
    assert code == 0 and "would commit as" not in out, out
    assert "would commit as" not in praxis("status")[1]


def test_start_warns_naming_both_identities_and_how_to_set_it(project):
    _other_identity(project)
    code, out = praxis("start", "alice", "jump", "scripts/player.gd")
    assert code == 0, out
    assert 'alice would commit as "Someone Else <else@example.org>"' in out
    assert '"Test <test@example.com>"' in out
    assert 'git config user.name "Test"' in out and 'git config user.email "test@example.com"' in out
    assert str(project) in out


def test_status_warns_once_for_every_agent_with_that_identity(project):
    praxis("start", "alice", "jump", "scripts/player.gd")
    praxis("start", "bob", "hp", "scripts/enemy.gd")
    _other_identity(project)
    code, out = praxis("status")
    assert code == 0 and out.count("would commit as") == 1, out
    assert "alice, bob would commit as" in out and "test@example.com" in out


def test_no_warning_when_main_has_a_commit_by_this_identity(project):
    _other_identity(project)
    (project / "notes.txt").write_text("hi\n")
    git(project, "add", "-A")
    git(project, "commit", "-q", "-m", "by the new identity")
    code, out = praxis("start", "alice", "jump", "scripts/player.gd")
    assert code == 0 and "would commit as" not in out, out
