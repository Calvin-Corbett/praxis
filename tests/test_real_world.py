"""What a first real project, many agents at once and unusual setups turned up. One test per way it went wrong."""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path

import pytest
from conftest import edit, folder, git, praxis, praxis_process

from agent_praxis import enough
from agent_praxis.landing_queue import Ticket
from agent_praxis.store import Store


def _exclusive_code(project: Path) -> None:
    """The old rule, one agent per code file, kept behind exclusive_code = true."""
    cfg = project / "praxis.toml"
    cfg.write_text(
        cfg.read_text(encoding="utf-8").replace("exclusive_code = false", "exclusive_code = true"), encoding="utf-8"
    )
    git(project, "commit", "-qam", "exclusive_code = true")


def _set_check(project: Path, script: str) -> None:
    cfg = project / "praxis.toml"
    line = f"check = '\"{sys.executable}\" {script}'"  # single quotes: a TOML string with no escapes
    cfg.write_text(re.sub(r"(?m)^check = .*$", lambda _m: line, cfg.read_text()))
    git(project, "add", "-A")
    git(project, "commit", "-qm", f"check runs {script}")


def test_an_agent_cannot_undo_another_agents_landed_change(project):
    """With exclusive_code = true. The review's steps: bob edits a file alice holds (far from alice's line), is refused, then puts the file
    back to how it was when bob started. Rebased onto alice's landing, that second commit undoes alice's work."""
    _exclusive_code(project)
    lines = ["extends Node", "var speed = 10"] + [f"# {n}" for n in range(20)] + ["var jump = 1"]
    edit(project / "scripts" / "player.gd", "\n".join(lines) + "\n")
    git(project, "commit", "-qam", "a longer player script")
    start = git(project, "rev-parse", "main")
    praxis("start", "alice", "jump", "scripts/player.gd")
    praxis("start", "bob", "other", "scripts/enemy.gd")
    alice, bob = folder(project, "alice"), folder(project, "bob")
    edit(alice / "scripts" / "player.gd", "\n".join([lines[0], "var speed = 600", *lines[2:]]) + "\n")
    assert praxis("land", "alice")[0] == 0
    edit(bob / "scripts" / "player.gd", "\n".join([*lines[:-1], "var jump = 99"]) + "\n")
    assert praxis("land", "bob")[0] == 2  # refused: alice holds player.gd (and bob's edit is now a commit)
    git(bob, "checkout", "main", "--", "scripts/player.gd")
    git(bob, "commit", "-qam", "take the newest player.gd")
    git(bob, "checkout", start, "--", "scripts/player.gd")  # ...then the version from before alice's landing
    code, out = praxis("land", "bob")
    assert code == 2 and "held by alice" in out and "git checkout main -- <file>" in out, out
    assert "speed = 600" in git(project, "show", "main:scripts/player.gd"), "alice's work is still in main"


def test_taking_the_newest_version_of_a_held_file_lands(project):
    praxis("start", "alice", "jump", "scripts/player.gd")
    praxis("start", "bob", "other", "scripts/enemy.gd")
    alice, bob = folder(project, "alice"), folder(project, "bob")
    edit(alice / "scripts" / "player.gd", "extends Node\nvar speed = 600\n")
    edit(bob / "scripts" / "player.gd", "extends Node\nvar speed = 1\n")
    edit(bob / "scripts" / "enemy.gd", "extends Node\nvar hp = 4\n")
    git(bob, "commit", "-qam", "bob touched a file alice holds")
    assert praxis("land", "alice")[0] == 0
    assert praxis("land", "bob")[0] == 2  # refused: bob changed alice's file
    git(bob, "checkout", "main", "--", "scripts/player.gd")  # the advice
    code, out = praxis("land", "bob")
    assert code == 0, out
    assert "speed = 600" in git(project, "show", "main:scripts/player.gd")
    assert "hp = 4" in git(project, "show", "main:scripts/enemy.gd")
    assert "bob touched a file alice holds" in git(project, "log", "main", "--format=%B"), "messages are kept"


def test_done_from_inside_the_agents_own_folder_explains_and_changes_nothing(project, monkeypatch):
    praxis("start", "alice", "jump", "scripts/player.gd")
    wt = folder(project, "alice")
    edit(wt / "scripts" / "player.gd", "extends Node\nvar speed = 11\n")
    assert praxis("land", "alice")[0] == 0
    monkeypatch.chdir(wt)
    code, out = praxis("done", "alice")
    assert code == 1 and "You are inside alice's folder" in out and str(project) in out
    assert (wt / ".git").exists() and (wt / "scripts" / "player.gd").is_file()
    code, out = praxis("status")
    assert code == 0 and "You are in alice's folder" in out
    code, out = praxis("land", "alice", "--done")
    assert code == 0 and "praxis done alice" in out and wt.is_dir()
    monkeypatch.chdir(project)
    assert praxis("done", "alice")[0] == 0 and not wt.exists()


@pytest.mark.skipif(os.name != "nt", reason="only Windows refuses to remove a folder in use")
def test_done_refuses_while_another_program_is_using_the_folder(project):
    praxis("start", "alice", "jump", "scripts/player.gd")
    wt = folder(project, "alice")
    holder = subprocess.Popen(
        [sys.executable, "-c", "import time; print('ready', flush=True); time.sleep(60)"],
        cwd=str(wt),
        stdout=subprocess.PIPE,
        text=True,
    )
    try:
        assert holder.stdout.readline().strip() == "ready"  # it is running in the folder: no guessing how long
        code, out = praxis("done", "alice")
        assert code == 1 and "Another program is using" in out
        assert (wt / ".git").exists() and (wt / "scripts" / "player.gd").is_file(), "nothing was touched"
    finally:
        holder.kill()
        holder.communicate()  # waits, and closes its pipe
    assert praxis("done", "alice")[0] == 0


def test_an_emptied_folder_never_breaks_status_or_done(project):
    praxis("start", "alice", "jump", "scripts/player.gd")
    praxis("start", "bob", "other", "scripts/enemy.gd")
    wt = folder(project, "alice")
    edit(wt / "scripts" / "player.gd", "extends Node\nvar speed = 11\n")
    assert praxis("land", "alice")[0] == 0
    for item in wt.iterdir():  # what a removal that failed half-way leaves behind: an empty folder
        if item.is_dir():
            shutil.rmtree(item)
        else:
            item.unlink()
    code, out = praxis("status")
    assert code == 0 and "bob" in out and "its folder is gone" in out
    code, out = praxis("done", "alice")
    assert code == 0, out
    assert not wt.exists()


def test_abandon_never_commits_into_a_project_that_contains_the_agent_folder(tmp_path, monkeypatch):
    outer = tmp_path / "home"
    outer.mkdir()
    git(outer, "init", "-q", "-b", "master")
    git(outer, "config", "user.name", "Outer")
    git(outer, "config", "user.email", "outer@example.com")
    (outer / "notes.txt").write_text("v1\n")
    git(outer, "add", "-A")
    git(outer, "commit", "-qm", "outer")
    root = outer / "games" / "tiny"
    (root / "scripts").mkdir(parents=True)
    (root / "project.godot").write_text("[application]\n")
    (root / "scripts" / "player.gd").write_text("extends Node\n")
    git(root, "init", "-q", "-b", "main")
    git(root, "config", "user.name", "Test")
    git(root, "config", "user.email", "test@example.com")
    git(root, "add", "-A")
    git(root, "commit", "-qm", "first")
    monkeypatch.chdir(root)
    assert praxis("init")[0] == 0
    praxis("start", "alice", "jump", "scripts/player.gd")
    wt = folder(root, "alice")
    (outer / "notes.txt").write_text("my unsaved edit\n")
    (wt / ".git").unlink()  # an agent "re-initialised" its folder
    before = git(outer, "rev-parse", "HEAD")
    for command in ("land", "done", "abandon"):
        code, out = praxis(command, "alice")
        assert (
            code == 1 and "does not see it as a working folder of this project" in out and "worktree repair" in out
        ), (command, out)
    assert git(outer, "rev-parse", "HEAD") == before, "nothing was committed in the outer project"
    assert (outer / "notes.txt").read_text() == "my unsaved edit\n"
    assert praxis("status")[0] == 0


def _link(link: Path, target: Path) -> None:
    if os.name == "nt":
        subprocess.run(["cmd", "/c", "mklink", "/J", str(link), str(target)], check=True, capture_output=True)
    else:
        link.symlink_to(target, target_is_directory=True)


def test_done_never_deletes_through_a_link_in_a_cache(project, tmp_path):
    library = tmp_path / "my-library"
    library.mkdir()
    (library / "index.js").write_text("my own library, outside the project")
    (project / ".gitignore").write_text("node_modules/\n")
    git(project, "add", ".gitignore")
    git(project, "commit", "-qm", "ignore node_modules")
    praxis("start", "alice", "jump", "scripts/player.gd")
    wt = folder(project, "alice")
    (wt / "node_modules" / "real").mkdir(parents=True)
    (wt / "node_modules" / "real" / "a.js").write_text("cache")
    _link(wt / "node_modules" / "mylib", library)  # what `npm install file:../my-library` makes
    code, out = praxis("done", "alice")
    assert code == 0, out
    assert not wt.exists()
    assert (library / "index.js").read_text() == "my own library, outside the project"


def test_done_never_deletes_through_a_cache_that_is_itself_a_link(project, tmp_path):
    library = tmp_path / "shared-modules"
    library.mkdir()
    (library / "keep.js").write_text("someone's real files")
    (project / ".gitignore").write_text("node_modules\n")
    git(project, "add", ".gitignore")
    git(project, "commit", "-qm", "ignore node_modules")
    praxis("start", "alice", "jump", "scripts/player.gd")
    wt = folder(project, "alice")
    _link(wt / "node_modules", library)
    assert praxis("done", "alice")[0] == 0
    assert (library / "keep.js").read_text() == "someone's real files"


def test_land_from_another_agents_folder_is_refused(project, monkeypatch):
    praxis("start", "alice", "jump", "scripts/player.gd")
    praxis("start", "bob", "other", "scripts/enemy.gd")
    edit(folder(project, "bob") / "scripts" / "enemy.gd", "extends Node\nvar hp = 1 # half done\n")
    before = git(project, "rev-parse", "main")
    monkeypatch.chdir(folder(project, "alice"))
    code, out = praxis("land", "bob")
    assert code == 1 and "You are in alice's folder, but asked to land bob" in out
    assert git(project, "rev-parse", "main") == before
    assert git(folder(project, "bob"), "status", "--porcelain"), "bob's half-done edit was not even committed"


def test_a_git_project_inside_the_agent_folder_is_not_landed_as_a_broken_pointer(project):
    praxis("start", "alice", "lib", "vendor")
    wt = folder(project, "alice")
    lib = wt / "vendor" / "lib"
    lib.mkdir(parents=True)
    git(lib, "init", "-q")
    (lib / "a.txt").write_text("x")
    git(lib, "add", "-A")
    git(lib, "-c", "user.name=T", "-c", "user.email=t@e", "commit", "-qm", "inner")
    before = git(project, "rev-parse", "main")
    code, out = praxis("land", "alice")
    assert code == 1 and "separate git projects" in out and "vendor/lib" in out
    assert git(project, "rev-parse", "main") == before
    # committed by hand as a pointer: still refused, with the way out
    git(wt, "add", "vendor/lib")
    git(wt, "commit", "-qm", "pointer")
    code, out = praxis("land", "alice")
    assert code == 1 and "bare pointers" in out and "git rm --cached" in out
    assert git(project, "rev-parse", "main") == before
    code, out = praxis("done", "alice")  # never landed, so done refuses rather than lose the inner commit
    assert code == 1 and lib.is_dir()


def test_a_check_that_waits_for_a_key_does_not_hold_the_line(project):
    (project / "waits.py").write_text("import sys\nsys.stdin.read()\nprint('pressed a key')\n")
    _set_check(project, "waits.py")
    praxis("start", "alice", "jump", "scripts/player.gd")
    edit(folder(project, "alice") / "scripts" / "player.gd", "extends Node\nvar speed = 11\n")
    # Run it the way an AI tool does: as its own process, with an input that stays open and never sends a key.
    proc = praxis_process(project, "land", "alice", stdin=subprocess.PIPE)
    try:
        code = proc.wait(timeout=120)
    except subprocess.TimeoutExpired:
        proc.kill()
        pytest.fail("the landing waited for a key that never came")
    finally:
        proc.stdin.close()
    assert code == 0, proc.stdout.read()


def test_a_huge_check_output_is_logged_without_filling_memory(project):
    (project / "loud.py").write_text(
        "import sys\nfor i in range(200000): print('line', i, 'é')\nsys.exit(1)\n", encoding="utf-8"
    )
    _set_check(project, "loud.py")
    praxis("start", "alice", "jump", "scripts/player.gd")
    edit(folder(project, "alice") / "scripts" / "player.gd", "extends Node\nvar speed = 11\n")
    code, out = praxis("land", "alice")
    assert code == 2 and "line 199999 é" in out, out[-500:]
    log = next((project / ".git" / "praxis" / "logs").glob("alice-*.log")).read_text(encoding="utf-8")
    assert "line 0 é" in log and "(exit code 1)" in log


def _replace(project: Path, old: str, new: str) -> None:
    cfg = project / "praxis.toml"
    text = cfg.read_text()
    assert old in text
    cfg.write_text(text.replace(old, new))


def test_a_misspelled_setting_stops_praxis_instead_of_skipping_the_check(project):
    _replace(project, "\ncheck = ", "\nchek = ")
    code, out = praxis("status")
    assert code == 1 and "chek (did you mean check?)" in out


def test_a_setting_of_the_wrong_kind_is_explained(project):
    _replace(project, "enough_after = 3", 'enough_after = "three"')
    code, out = praxis("status")
    assert code == 1 and "enough_after must be a whole number" in out
    _replace(project, 'enough_after = "three"', "enough_after = 3")
    _replace(project, "check_may_change = []", 'check_may_change = "ProjectSettings"')
    code, out = praxis("status")
    assert code == 1 and "check_may_change must be a list" in out


def test_praxis_toml_saved_with_a_bom_or_as_utf16_is_read(project):
    cfg = project / "praxis.toml"
    text = cfg.read_text()
    cfg.write_bytes(b"\xef\xbb\xbf" + text.encode("utf-8"))
    assert praxis("status")[0] == 0
    cfg.write_bytes(text.encode("utf-16"))
    assert praxis("status")[0] == 0


def test_a_backslash_in_the_check_line_is_explained(project):
    cfg = project / "praxis.toml"
    cfg.write_text(re.sub(r"(?m)^check = .*$", lambda _m: 'check = "tools\\build.bat"', cfg.read_text()))
    code, out = praxis("status")
    assert code == 1 and "forward" in out and "single quotes" in out


@pytest.mark.parametrize("name", ["con", "NUL", "com1", "lpt9.x", "a.", "a..b"])
def test_names_windows_or_git_cannot_use_are_refused_plainly(project, name):
    code, out = praxis("start", name, "task", "scripts/player.gd")
    assert code == 1 and "cannot be an agent name" in out


def test_a_tag_named_like_the_main_branch_does_not_break_start_or_land(project):
    git(project, "-c", "tag.gpgSign=false", "tag", "main", "HEAD")
    code, out = praxis("start", "alice", "jump", "scripts/player.gd")
    assert code == 0, out
    edit(folder(project, "alice") / "scripts" / "player.gd", "extends Node\nvar speed = 11\n")
    code, out = praxis("land", "alice", "--done")
    assert code == 0, out
    assert "speed = 11" in git(project, "show", "refs/heads/main:scripts/player.gd")


def test_init_will_not_take_over_a_bigger_project_around_a_game_folder(tmp_path, monkeypatch):
    home = tmp_path / "home"
    game = home / "Games" / "mygame"
    game.mkdir(parents=True)
    (game / "project.godot").write_text("[application]\n")
    git(home, "init", "-q", "-b", "master")
    monkeypatch.chdir(game)
    code, out = praxis("init")
    assert code == 1 and "looks like a project of its own" in out
    assert not (home / "praxis.toml").exists() and not (game / "praxis.toml").exists()


def test_init_warns_when_project_files_are_not_committed(tmp_path, monkeypatch):
    root = tmp_path / "new game"
    (root / "scripts").mkdir(parents=True)
    (root / "scripts" / "player.gd").write_text("extends Node\n")
    git(root, "init", "-q", "-b", "main")
    git(root, "config", "user.name", "Test")
    git(root, "config", "user.email", "test@example.com")
    monkeypatch.chdir(root)
    code, out = praxis("init")
    assert code == 0 and "not committed" in out and "scripts/player.gd" in out


def test_start_says_when_a_claimed_file_was_never_committed(project):
    (project / "scripts" / "boss.gd").write_text("extends Node\n")
    code, out = praxis("start", "alice", "boss", "scripts/boss.gd")
    assert code == 0 and "never committed" in out


def test_problems_in_the_main_folder_never_pause_an_agent():
    def row(kind):
        return {"ok": 0, "kind": kind, "problems": None, "signature": "same", "summary": kind}

    assert enough.should_pause([row("check")] * 3, 3)
    assert enough.should_pause([row("main-folder")] * 5, 3) is None


def test_done_abandon_and_land_wait_for_a_landing_in_progress(project):
    praxis("start", "alice", "jump", "scripts/player.gd")
    store = Store(project / ".git" / "praxis")
    try:
        with Ticket(store, "alice", lambda *_: None):
            for command in ("done", "abandon", "land"):
                code, out = praxis(command, "alice")
                assert code == 1 and "landing" in out, (command, out)
        assert folder(project, "alice").is_dir()
    finally:
        store.close()


def test_status_shows_a_stopped_landing_as_stopped(project):
    praxis("start", "alice", "jump", "scripts/player.gd")
    store = Store(project / ".git" / "praxis")
    try:
        with store.write() as db:
            db.execute(
                "INSERT INTO queue (agent, token, enqueued, started) VALUES ('alice', 'dead', ?, ?)",
                (time.time(), time.time()),
            )
    finally:
        store.close()
    code, out = praxis("status")
    assert code == 0 and "stopped part-way" in out and "is landing now" not in out


@pytest.mark.skipif(os.name != "nt", reason="Windows spellings")
def test_windows_spellings_of_a_held_file_are_the_same_file(project):
    _exclusive_code(project)
    praxis("start", "alice", "jump", "scripts/player.gd")
    praxis("start", "bob", "other", "scripts/enemy.gd")
    code, out = praxis("claim", "bob", "scripts/player.gd.")
    assert code == 1 and "alice holds" in out
    code, out = praxis("claim", "bob", "scripts/player.gd::$DATA")
    assert code == 1 and "':'" in out


@pytest.mark.skipif(os.name == "nt", reason="needs real symbolic links")
def test_a_project_that_commits_a_symlink_is_removed_without_forcing(project, tmp_path):
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "keep.txt").write_text("not the project's")
    (project / "docs").symlink_to(outside, target_is_directory=True)
    git(project, "add", "docs")
    git(project, "commit", "-qm", "a committed symlink")
    praxis("start", "alice", "jump", "scripts/player.gd")
    wt = folder(project, "alice")
    assert (wt / "docs").is_symlink()
    code, out = praxis("done", "alice")
    assert code == 0, out
    assert (outside / "keep.txt").read_text() == "not the project's"


def test_only_symlinks_and_junctions_count_as_links(tmp_path):
    from agent_praxis.agents import _is_link

    plain = tmp_path / "plain"
    plain.mkdir()
    (tmp_path / "file.txt").write_text("x")
    assert not _is_link(str(plain)) and not _is_link(str(tmp_path / "file.txt"))
    target = tmp_path / "target"
    target.mkdir()
    _link(tmp_path / "link", target)
    assert _is_link(str(tmp_path / "link"))


def test_check_output_with_a_stray_bad_byte_keeps_its_accents():
    from agent_praxis.landing import _decode

    assert _decode("Größe é ü".encode() + bytes([0xFF]) + b" fin").startswith("Größe é ü")
    if os.name == "nt" and "1252" in __import__("locale").getpreferredencoding(False):
        assert "�" not in _decode("café ".encode("cp1252") * 50)  # a Windows code page tool's output


@pytest.mark.skipif(os.name != "nt", reason="git walks into junctions only on Windows")
def test_a_junction_git_does_not_ignore_is_refused_before_anything_changes(project, tmp_path):
    outside = tmp_path / "outside"
    (outside / "node_modules" / "pkg").mkdir(parents=True)
    (outside / "foo.txt").write_text("real file")
    (outside / "node_modules" / "pkg" / "index.js").write_text("real package")
    (project / ".gitignore").write_text("node_modules/\n")
    git(project, "add", ".gitignore")
    git(project, "commit", "-qm", "ignore node_modules")
    praxis("start", "alice", "jump", "scripts/player.gd")
    wt = folder(project, "alice")
    _link(wt / "libs", outside)
    before = git(project, "rev-parse", "main")
    for command in ("land", "done", "abandon"):
        code, out = praxis(command, "alice")
        assert code == 1 and "junctions" in out and "rmdir" in out, (command, out)
    assert git(project, "rev-parse", "main") == before
    assert not git(wt, "log", "--oneline", "main..HEAD"), "nothing was committed through the junction"
    assert (outside / "node_modules" / "pkg" / "index.js").read_text() == "real package"
    assert (outside / "foo.txt").read_text() == "real file"
    os.rmdir(wt / "libs")  # the advice: removes only the link
    assert praxis("done", "alice")[0] == 0
    assert (outside / "foo.txt").read_text() == "real file"


@pytest.mark.skipif(os.name != "nt", reason="only Windows refuses to delete an open file")
def test_done_refuses_while_a_file_in_the_folder_is_open(project):
    praxis("start", "alice", "jump", "scripts/player.gd")
    wt = folder(project, "alice")
    target = wt / "scripts" / "enemy.gd"
    holder = subprocess.Popen(
        [sys.executable, "-c", f"f = open(r'{target}'); print('ready', flush=True); import time; time.sleep(60)"],
        stdout=subprocess.PIPE,
        text=True,
    )
    try:
        assert holder.stdout.readline().strip() == "ready"  # the file is open now, however busy the machine is
        code, out = praxis("done", "alice")
        assert code == 1 and "Another program is using" in out and "enemy.gd" in out, out
        assert (wt / ".git").exists() and target.is_file(), "nothing was touched"
    finally:
        holder.kill()
        holder.communicate()  # waits, and closes its pipe
    assert praxis("done", "alice")[0] == 0


def test_a_removal_cut_short_is_explained_not_called_foreign(project, monkeypatch):
    from agent_praxis import gitops

    praxis("start", "alice", "jump", "scripts/player.gd")
    wt = folder(project, "alice")
    real = gitops.git

    def git_stops_part_way(args, cwd, check=True, stdin=None):
        if args[:2] == ["worktree", "remove"]:  # git drops its record and the .git link, then a file is in use
            (wt / ".git").unlink()
            real(["worktree", "prune"], cwd, check=False)
            return subprocess.CompletedProcess(args, 1, "", "error: failed to delete: Permission denied")
        return real(args, cwd, check, stdin)

    monkeypatch.setattr(gitops, "git", git_stops_part_way)
    code, out = praxis("done", "alice")
    assert code == 1 and "Could not remove" in out
    monkeypatch.setattr(gitops, "git", real)
    code, out = praxis("status")
    assert code == 0 and "partly removed" in out and "look through" in out, out
    assert "no changes yet" not in out and "unknown" in out, out
    code, out = praxis("done", "alice")
    assert code == 1 and "stopped part-way" in out and "praxis/alice" in out
    assert (wt / "scripts" / "player.gd").is_file(), "Praxis deleted nothing itself"


def test_a_folder_that_lost_its_git_link_another_way_is_not_called_a_cut_short_removal(project):
    """An agent deletes its own .git file, then another agent's done runs `git worktree prune`: the folder looks
    exactly like a removal cut short, but Praxis never started removing it, and it may hold unsaved work."""
    praxis("start", "alice", "jump", "scripts/player.gd")
    praxis("start", "bob", "enemies", "scripts/enemy.gd")
    wt = folder(project, "alice")
    edit(wt / "scripts" / "player.gd", "extends Node\nvar speed = 99\n")  # not saved anywhere else
    (wt / ".git").unlink()
    assert praxis("done", "bob")[0] == 0  # runs git worktree prune
    code, out = praxis("status")
    assert code == 0 and "partly removed" not in out and "delete what is left" not in out, out
    assert "Move it somewhere safe" in out, out
    assert "no changes yet" not in out and "unknown" in out, out
    code, out = praxis("done", "alice")
    assert code == 1 and "Move the folder somewhere safe" in out and "stopped part-way" not in out, out
    assert "speed = 99" in (wt / "scripts" / "player.gd").read_text()


def test_status_does_not_claim_a_stopped_landing_landed_nothing(project):
    praxis("start", "alice", "jump", "scripts/player.gd")
    store = Store(project / ".git" / "praxis")
    try:
        with store.write() as db:
            db.execute(
                "INSERT INTO queue (agent, token, enqueued, started) VALUES ('alice', 'dead', ?, ?)",
                (time.time(), time.time()),
            )
    finally:
        store.close()
    code, out = praxis("status")
    assert code == 0 and "nothing landed from it" not in out and "may not have finished" in out, out
    assert "RECENT LANDINGS" in out and "main" in out


def _two_steps_that_clash_only_one_at_a_time(project) -> Path:
    """alice changes a line, then takes the newest version of it: replayed one commit at a time the first step
    clashes, joined into one it does not."""
    praxis("start", "alice", "jump", "scripts")
    wt = folder(project, "alice")
    edit(wt / "scripts" / "player.gd", "extends Node\nvar speed = 11\n")
    git(wt, "commit", "-qam", "faster")
    edit(project / "scripts" / "player.gd", "extends Node\nvar speed = 20\n")
    git(project, "commit", "-qam", "newer speed on main")
    edit(wt / "scripts" / "player.gd", "extends Node\nvar speed = 20\n")
    edit(wt / "scripts" / "jump.gd", "extends Node\nvar jump = 3\n")
    git(wt, "add", "-A")
    git(wt, "commit", "-qm", "take the newest speed, add jumping")
    return wt


def test_joining_commits_never_leaves_the_folder_off_its_branch(project, monkeypatch):
    """A landing killed during the joined try used to leave the folder detached, and land/done then said the
    work had gone separate ways. The joined try now runs on the agent's own branch the whole time."""
    from agent_praxis import gitops

    wt = _two_steps_that_clash_only_one_at_a_time(project)
    seen = []
    real = gitops.git

    def watch(args, cwd, check=True, stdin=None):
        if "rebase" in args and "--abort" not in args and Path(cwd) == wt:  # git itself detaches mid-rebase
            seen.append(real(["symbolic-ref", "--quiet", "--short", "HEAD"], cwd, check=False).stdout.strip())
        return real(args, cwd, check, stdin)

    monkeypatch.setattr(gitops, "git", watch)
    code, out = praxis("land", "alice")
    assert code == 0 and "together into one commit" in out, out
    assert seen and all(b == "praxis/alice" for b in seen), seen
    assert "jump = 3" in git(project, "show", "main:scripts/jump.gd")


def test_a_landing_killed_during_the_joined_try_leaves_the_folder_on_its_branch(project, monkeypatch):
    """A kill skips every cleanup step. Simulate one: the joined rebase stops, and nothing puts anything back."""
    from agent_praxis import gitops, landing

    class Killed(BaseException):
        pass

    wt = _two_steps_that_clash_only_one_at_a_time(project)
    edit(wt / "scripts" / "player.gd", "extends Node\nvar speed = 33\n")  # the joined rebase clashes too
    git(wt, "commit", "-qam", "even faster")
    original = git(wt, "rev-parse", "HEAD")
    real, rebases = gitops.git, []

    def killed_in_the_joined_rebase(args, cwd, check=True, stdin=None):
        proc = real(args, cwd, check, stdin)
        if "rebase" in args and "--abort" not in args and Path(cwd) == wt:
            rebases.append(args)
            if len(rebases) == 2:
                raise Killed
        return proc

    cleanups = landing._back_on_branch, landing._record_if_it_landed
    monkeypatch.setattr(gitops, "git", killed_in_the_joined_rebase)
    monkeypatch.setattr(landing, "_back_on_branch", lambda *_a: None)
    monkeypatch.setattr(landing, "_record_if_it_landed", lambda *_a: None)
    with pytest.raises(Killed):
        praxis("land", "alice")
    # Not monkeypatch.undo(): that would also undo the project fixture's chdir.
    monkeypatch.setattr(gitops, "git", real)
    monkeypatch.setattr(landing, "_back_on_branch", cleanups[0])
    monkeypatch.setattr(landing, "_record_if_it_landed", cleanups[1])
    assert git(project, "rev-parse", "refs/praxis/joining/alice") == original, "the original commits are kept"
    git(wt, "rebase", "--abort")  # what land says to do with a half-finished rebase
    assert git(wt, "symbolic-ref", "--short", "HEAD") == "praxis/alice"
    assert git(wt, "rev-parse", "HEAD^{tree}") == git(project, "rev-parse", f"{original}^{{tree}}")
    code, out = praxis("land", "alice")
    assert code != 0
    assert "separate ways" not in out and "detached" not in out and "clash" in out, out


def test_a_joined_try_that_still_clashes_puts_the_original_commits_back(project):
    wt = _two_steps_that_clash_only_one_at_a_time(project)
    edit(wt / "scripts" / "player.gd", "extends Node\nvar speed = 33\n")  # now the end result clashes too
    git(wt, "commit", "-qam", "even faster")
    before = git(wt, "rev-parse", "HEAD")
    code, out = praxis("land", "alice")
    assert code != 0 and "clash" in out, out
    assert git(wt, "rev-parse", "HEAD") == before and git(wt, "symbolic-ref", "--short", "HEAD") == "praxis/alice"
    assert git(project, "for-each-ref", "refs/praxis/") == "", "the safety copy is cleared once it is not needed"


def test_a_detached_folder_with_the_same_files_as_its_branch_is_put_back_not_called_separate(project):
    """What a landing killed in the middle of the old joined try left: detached on a commit beside the branch
    whose files are exactly the branch's."""
    praxis("start", "alice", "jump", "scripts/player.gd")
    wt = folder(project, "alice")
    edit(wt / "scripts" / "player.gd", "extends Node\nvar speed = 42\n")
    git(wt, "commit", "-qam", "one")
    edit(wt / "scripts" / "player.gd", "extends Node\nvar speed = 43\n")
    git(wt, "commit", "-qam", "two")
    fork = git(wt, "merge-base", "HEAD", "main")
    joined = git(wt, "commit-tree", "HEAD^{tree}", "-p", fork, "-m", "joined")
    git(wt, "checkout", "-q", "--detach", joined)
    code, out = praxis("land", "alice")
    assert code == 0 and "separate ways" not in out and "same files" in out, out
    assert "speed = 43" in (project / "scripts" / "player.gd").read_text()
    assert git(wt, "symbolic-ref", "--short", "HEAD") == "praxis/alice"


@pytest.mark.skipif(os.name != "nt", reason="Windows checks wait for their job before starting")
def test_a_landing_stopped_before_its_job_is_ready_starts_no_project_command(project, monkeypatch):
    from agent_praxis import landing
    from agent_praxis.errors import PraxisError

    started = project.parent / "check-started"
    (project / "mark.py").write_text(f"from pathlib import Path\nPath({str(started)!r}).touch()\n")
    _set_check(project, "mark.py")
    praxis("start", "alice", "jump", "scripts/player.gd")
    edit(folder(project, "alice") / "scripts" / "player.gd", "extends Node\nvar speed = 11\n")
    before = git(project, "rev-parse", "main")

    def lose_parent_before_assignment(self, proc):
        # Losing the parent's pipe models its death at the precise point between process creation and assignment.
        if proc.stdin is not None:
            proc.stdin.close()
        proc.wait(timeout=30)
        raise PraxisError("Stopped before job assignment.")

    monkeypatch.setattr(landing._CheckEndsWithMe, "watch", lose_parent_before_assignment)
    code, out = praxis("land", "alice")
    assert code != 0 and "Stopped before job assignment" in out, out
    assert not started.exists()
    assert git(project, "rev-parse", "main") == before


@pytest.mark.skipif(os.name != "nt", reason="Windows exit codes use all 32 bits")
def test_a_check_keeps_its_full_windows_exit_code_in_the_log(project):
    (project / "exitcode.py").write_text("import sys\nsys.exit(-2147483645)\n")
    _set_check(project, "exitcode.py")
    praxis("start", "alice", "jump", "scripts/player.gd")
    edit(folder(project, "alice") / "scripts" / "player.gd", "extends Node\nvar speed = 11\n")
    code, out = praxis("land", "alice")
    assert code == 2, out
    logs = (project / ".git" / "praxis" / "logs").glob("alice-*.log")
    assert any("(exit code 2147483651)" in log.read_text() for log in logs)


def test_a_helper_the_check_leaves_running_survives_a_normal_landing(project):
    beat = project.parent / "helper-beat.txt"
    helper = f"import time, pathlib; time.sleep(4); pathlib.Path({str(beat)!r}).write_text('alive')"
    (project / "spawns.py").write_text(
        "import subprocess, sys\n"
        f"helper = {helper!r}\n"
        "flags = 0x00000008 if sys.platform == 'win32' else 0  # DETACHED_PROCESS\n"
        "subprocess.Popen([sys.executable, '-c', helper], creationflags=flags, start_new_session=sys.platform != 'win32')\n"
    )
    _set_check(project, "spawns.py")
    praxis("start", "alice", "jump", "scripts/player.gd")
    edit(folder(project, "alice") / "scripts" / "player.gd", "extends Node\nvar speed = 11\n")
    proc = praxis_process(project, "land", "alice")
    out, _ = proc.communicate(timeout=300)
    assert proc.returncode == 0, out
    for _ in range(40):
        if beat.exists():
            break
        time.sleep(0.5)
    assert beat.exists(), "the check's helper was stopped when Praxis finished"


@pytest.mark.skipif(os.name != "nt", reason="the check stops with a killed landing on Windows")
def test_a_check_stops_when_its_landing_is_killed(project):
    # The check adds to a file ten times a second for as long as it lives. No deadline to race against: however
    # busy the machine is, a check that stopped adds nothing more, and one that is still running keeps adding.
    beats = project.parent / "check-beats.txt"
    (project / "slow.py").write_text(
        f"import time\nfor _ in range(900):\n    open(r'{beats}', 'a').write('x')\n    time.sleep(0.1)\n"
    )
    _set_check(project, "slow.py")
    praxis("start", "alice", "jump", "scripts/player.gd")
    edit(folder(project, "alice") / "scripts" / "player.gd", "extends Node\nvar speed = 11\n")
    proc = praxis_process(project, "land", "alice")
    for _ in range(240):
        if beats.exists():
            break
        time.sleep(0.5)
    assert beats.exists(), "the check never started"
    subprocess.run(["taskkill", "/F", "/T", "/PID", str(proc.pid)], capture_output=True, check=False)
    proc.wait()
    proc.stdout.close()  # not communicate(): a check that lived on could hold the pipe open and be waited for
    size, quiet_since, give_up = beats.stat().st_size, time.time(), time.time() + 60
    while time.time() - quiet_since < 3 and time.time() < give_up:
        time.sleep(0.2)
        if beats.stat().st_size != size:
            size, quiet_since = beats.stat().st_size, time.time()
    assert time.time() - quiet_since >= 3, "the check kept running after its landing was killed"


def test_init_will_not_take_over_a_bigger_project_around_a_code_folder(tmp_path, monkeypatch):
    home = tmp_path / "home"
    app = home / "code" / "app"
    app.mkdir(parents=True)
    (app / "pyproject.toml").write_text("[project]\nname = 'app'\n")
    git(home, "init", "-q", "-b", "master")
    monkeypatch.chdir(app)
    code, out = praxis("init")
    assert code == 1 and "looks like a project of its own" in out
    assert not (home / "praxis.toml").exists()
