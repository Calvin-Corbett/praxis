"""Praxis stays quick on a big project with many agents, and the speed-ups keep every answer the same."""

from __future__ import annotations

import os
import re
import subprocess
import sys
import time
from pathlib import Path

import pytest
from conftest import edit, folder, git, praxis

from agent_praxis import agents, gitops, landing


def _count_git(monkeypatch) -> list[tuple[list[str], str]]:
    calls: list[tuple[list[str], str]] = []
    real = gitops.git

    def counting(args, cwd, check=True, stdin=None):
        calls.append((list(args), str(cwd)))  # list.append is safe from several threads
        return real(args, cwd, check, stdin)

    monkeypatch.setattr(gitops, "git", counting)
    return calls


@pytest.mark.parametrize("count", [1, 4])
def test_status_asks_git_a_few_questions_per_agent(project, monkeypatch, count):
    for i in range(count):
        assert praxis("start", f"a{i}", f"task {i}", f"scripts/s{i}.gd")[0] == 0
    edit(folder(project, "a0") / "scripts" / "s0.gd", "extends Node\n")  # being edited
    wt1 = folder(project, f"a{count - 1}")
    edit(wt1 / "scripts" / "new.gd", "extends Node\n")
    git(wt1, "add", "scripts/new.gd")
    git(wt1, "commit", "-qm", "saved")  # a saved change, not landed
    calls = _count_git(monkeypatch)
    code, out = praxis("status")
    assert code == 0, out
    # Finding the project (2), every branch tip at once (1), then per agent: its folder and commit (1), its
    # changed files (1) and its unlanded commits (1).
    # Plus two for the author check: the main branch's recent authors, and the identity the agent folders use.
    assert len(calls) <= 5 + 3 * count, "\n".join(" ".join(a) for a, _ in calls)
    statuses = [args for args, _ in calls if "status" in args]
    assert len(statuses) == count and all(args[0] == "--no-optional-locks" for args in statuses), statuses
    assert "1 file(s) being edited" in out and "1 saved change(s) not landed yet" in out, out
    assert "last sign of work: just now" in out


def test_status_reads_a_folder_with_no_commit_yet_like_before(project):
    praxis("start", "alice", "jump", "scripts/player.gd")
    wt = folder(project, "alice")
    git(wt, "checkout", "-q", "--orphan", "fresh")  # HEAD has no commit: the one-call look cannot answer
    code, out = praxis("status")
    assert code == 0 and "alice (working)" in out and "could not read" not in out, out


def test_start_writes_files_with_parallel_workers_and_leaves_the_folder_clean(project, monkeypatch):
    for i in range(150):  # more files than git's threshold for parallel checkout (100)
        edit(project / "scripts" / "gen" / f"gen{i}.gd", f"extends Node\nvar n = {i}\n" * 20)
    git(project, "add", "-A")
    git(project, "commit", "-qm", "many scripts")
    calls = _count_git(monkeypatch)
    assert praxis("start", "alice", "jump", "scripts/player.gd")[0] == 0
    adds = [args for args, _ in calls if "worktree" in args and "add" in args]
    assert adds and adds[0][:2] == ["-c", "checkout.workers=0"], adds
    wt = folder(project, "alice")
    assert git(wt, "status", "--porcelain", "--untracked-files=all", "--ignored") == ""
    assert git(wt, "ls-files") == git(project, "ls-files")
    assert (wt / "scripts" / "gen" / "gen149.gd").read_text() == "extends Node\nvar n = 149\n" * 20


def test_a_long_check_says_it_is_still_running(project, monkeypatch, tmp_path):
    monkeypatch.setattr(landing, "HEARTBEAT_SECONDS", 0.3)
    slow = tmp_path / "slow check.py"
    slow.write_text(
        "import time\nprint('compiling level 1', flush=True)\nprint('', flush=True)\ntime.sleep(2)\nprint('done')\n"
    )
    cfg = project / "praxis.toml"
    line = f'check = \'"{sys.executable}" "{slow}"\''
    cfg.write_text(re.sub(r"(?m)^check = .*$", lambda _m: line, cfg.read_text()))
    praxis("start", "alice", "jump", "scripts/player.gd")
    edit(folder(project, "alice") / "scripts" / "player.gd", "extends Node\nvar speed = 11\n")
    code, out = praxis("land", "alice")
    assert code == 0, out
    assert "Check still running (0 min). Output so far: " in out and ".log" in out, out
    assert "Its last line: compiling level 1" in out, out
    assert out.index("Check still running") < out.index("Landed:"), out


def test_last_line_skips_blank_lines_and_progress_redraws(tmp_path):
    log = tmp_path / "a.log"
    log.write_bytes(b"$ check\n\nstep 1\r\nprogress 10%\rprogress 90%\r\n\n  \n")
    assert landing._last_line(log) == "progress 90%"
    log.write_bytes(b"")
    assert landing._last_line(log) == ""
    assert landing._last_line(tmp_path / "missing.log") == ""


def test_check_logs_are_capped_by_total_size_oldest_first(tmp_path, monkeypatch):
    logs = tmp_path / "logs"
    logs.mkdir()
    made = []
    for i in range(6):
        path = logs / f"a-{i}.log"
        path.write_bytes(b"x" * 100)
        os.utime(path, (1000 + i, 1000 + i))
        made.append(path)
    monkeypatch.setattr(landing, "KEEP_LOG_BYTES", 350)
    landing._prune_logs(logs, keep=made[-1])
    assert [p.name for p in sorted(logs.glob("*.log"))] == ["a-3.log", "a-4.log", "a-5.log"]
    # The log of the landing just run is never deleted, even when it alone is over the cap.
    big = logs / "b-big.log"
    big.write_bytes(b"y" * 1000)
    os.utime(big, (900, 900))  # even when its clock says it is the oldest
    landing._prune_logs(logs, keep=big)
    assert [p.name for p in logs.glob("*.log")] == ["b-big.log"]


def test_check_logs_are_still_capped_by_count(tmp_path, monkeypatch):
    logs = tmp_path / "logs"
    logs.mkdir()
    for i in range(5):
        path = logs / f"a-{i}.log"
        path.write_text("x")
        os.utime(path, (1000 + i, 1000 + i))
    monkeypatch.setattr(landing, "KEEP_LOGS", 2)
    landing._prune_logs(logs, keep=logs / "a-4.log")
    assert sorted(p.name for p in logs.glob("*.log")) == ["a-3.log", "a-4.log"]


def _repo(path: Path) -> Path:
    path.mkdir()
    git(path, "init", "-q", "-b", "main")
    git(path, "config", "user.name", "Test")
    git(path, "config", "user.email", "test@example.com")
    return path


def test_new_pointers_are_found_by_comparing_the_two_versions(tmp_path):
    """The one comparison that replaced listing every file of both versions gives the same answer."""
    repo = _repo(tmp_path / "r")
    one, two = "1" * 40, "2" * 40
    (repo / "a.txt").write_text("a")
    (repo / "d").mkdir()
    (repo / "d" / "x.txt").write_text("x")
    git(repo, "add", "-A")
    git(repo, "update-index", "--add", "--cacheinfo", f"160000,{one},kept")
    git(repo, "update-index", "--add", "--cacheinfo", f"160000,{one},same")
    git(repo, "commit", "-qm", "base")
    git(repo, "branch", "base")
    git(repo, "update-index", "--cacheinfo", f"160000,{two},kept")  # a pointer main has: moved on
    git(repo, "update-index", "--add", "--cacheinfo", f"160000,{two},sub/new")  # brand new
    git(repo, "rm", "-q", "--cached", "a.txt")
    git(repo, "update-index", "--add", "--cacheinfo", f"160000,{two},a.txt")  # a file became a pointer
    git(repo, "rm", "-q", "--cached", "d/x.txt")
    git(repo, "update-index", "--add", "--cacheinfo", f"160000,{two},d")  # a folder became a pointer
    git(repo, "commit", "-qm", "pointers")
    before_way = [p for p in gitops.gitlinks(repo, "HEAD") if p not in set(gitops.gitlinks(repo, "base"))]
    assert before_way == ["a.txt", "d", "sub/new"]
    assert gitops.new_gitlinks(repo, "base", "HEAD") == before_way


def _old_walk_answers(folder_path: Path, limit: int) -> tuple[list[str], list[str]]:
    """What the walks before the speed-up saw: _folder_in_use's paths and refuse_links_git_walks_into's junctions."""
    seen, found = [], []
    for here, dirs, files in os.walk(str(folder_path), topdown=True, followlinks=False):
        dirs[:] = [d for d in dirs if d != ".git" and not agents._is_link(os.path.join(here, d))]
        seen += [here, *(os.path.join(here, f) for f in files)]
    for here, dirs, _files in os.walk(str(folder_path), topdown=True, followlinks=False):
        if ".git" in dirs:
            dirs.remove(".git")
        for name in list(dirs):
            full = os.path.join(here, name)
            if agents._is_link(full):
                dirs.remove(name)
                if getattr(os.lstat(full), "st_reparse_tag", 0) == agents._JUNCTION:
                    found.append(Path(full).relative_to(folder_path).as_posix())
    return seen[:limit], found


@pytest.mark.skipif(os.name != "nt", reason="the folder walks that look for links run only on Windows")
def test_one_folder_walk_sees_exactly_what_the_old_walks_saw(tmp_path):
    top = tmp_path / "agent"
    outside = tmp_path / "outside"
    (outside / "deep").mkdir(parents=True)
    (outside / "deep" / "real.txt").write_text("real")
    for rel in ("a/b/c.txt", "a/d.txt", "e.txt", ".git/HEAD", "z/.git/config", "z/y.txt", "m/n/o/p.txt"):
        edit(top / rel, "x")
    gone = tmp_path / "gone"
    gone.mkdir()
    for link, target in (("a/lib", outside), ("m/dangling", gone), ("top-link", outside / "deep")):
        subprocess.run(["cmd", "/c", "mklink", "/J", str(top / link), str(target)], check=True, capture_output=True)
    gone.rmdir()  # a junction whose target is gone still counts as a folder, as it did for os.walk
    for limit in (3, 200_000):
        scan = agents.scan_folder(top, limit)
        old_paths, old_junctions = _old_walk_answers(top, limit)
        assert scan.paths == old_paths
        assert scan.junctions == old_junctions == ["top-link", "a/lib", "m/dangling"]
    assert (outside / "deep" / "real.txt").read_text() == "real"


@pytest.mark.skipif(os.name != "nt", reason="only Windows refuses to delete an open file")
def test_the_threaded_in_use_check_names_the_first_busy_file_in_walk_order(tmp_path, monkeypatch):
    top = tmp_path / "agent"
    for i in range(40):
        edit(top / f"f{i:02d}.txt", "x")
    monkeypatch.setattr(agents, "_IN_USE_BLOCK", 2)  # many small blocks, so several threads really share it
    scan = agents.scan_folder(top)
    later, earlier = scan.paths[30], scan.paths[7]
    with open(later), open(earlier):  # Python opens without letting others delete: exactly "in use"
        assert agents._folder_in_use(top) == earlier
        assert agents._folder_in_use(top, limit=5) is None  # past the limit Windows is not asked
    assert agents._folder_in_use(top) is None


def test_done_walks_the_folder_once_before_removing_it(project, monkeypatch):
    praxis("start", "alice", "jump", "scripts/player.gd")
    walks = []
    real = agents.scan_folder
    monkeypatch.setattr(agents, "scan_folder", lambda *a, **k: walks.append(a) or real(*a, **k))
    started = time.time()
    code, out = praxis("done", "alice")
    assert code == 0, out
    assert len(walks) == (1 if os.name == "nt" else 0), walks
    assert time.time() - started < 60
