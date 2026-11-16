"""Shared code files, generated files, an honest heartbeat, a safer release-stale and plainer claim wording."""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path

import pytest
from conftest import edit, folder, git, praxis

from agent_praxis.store import Store

PLAYER = "".join(f"line {n}\n" for n in range(1, 21))  # long enough for two edits far apart to combine

GEN_PY = """\
import pathlib, sys
if pathlib.Path("fail.flag").exists():
    print("the generator broke on purpose")
    sys.exit(1)
parts = [pathlib.Path(p).read_text() for p in ("scripts/player.gd", "scripts/enemy.gd")]
pathlib.Path("scripts/all.txt").write_text("".join(parts))
print("made scripts/all.txt")
"""


@pytest.fixture(autouse=True)
def _no_signing(project):
    git(project, "config", "commit.gpgsign", "false")  # this machine's own git settings must not matter here
    return project


def set_setting(project, name: str, value: str, commit: bool = True) -> None:
    """Set one praxis.toml line (replacing it when init wrote one), and commit it in the main folder."""
    path = project / "praxis.toml"
    text = path.read_text(encoding="utf-8")
    line = f"{name} = {value}"
    pattern = re.compile(rf"^{name} = .*$", re.MULTILINE)
    text = pattern.sub(lambda _m: line, text) if pattern.search(text) else text.rstrip("\n") + f"\n{line}\n"
    path.write_text(text, encoding="utf-8")
    if commit:
        git(project, "add", "praxis.toml")
        git(project, "commit", "-qm", f"set {name}")


def commit_in_main(project, rel: str, text: str) -> None:
    edit(project / rel, text)
    git(project, "add", rel)
    git(project, "commit", "-qm", f"add {rel}")


def _age(project, agent: str, hours: float) -> None:
    store = Store(project / ".git" / "praxis")
    with store.write() as db:
        db.execute("UPDATE agents SET last_seen = ? WHERE name = ?", (time.time() - hours * 3600, agent))
    store.close()


# 1. Shared code files ---------------------------------------------------------------------------------------------

def test_two_agents_may_claim_one_code_file_but_never_one_asset(project):
    assert praxis("start", "engine", "physics", "scripts/player.gd", "scenes/level1.tscn")[0] == 0
    code, out = praxis("start", "ui", "menus", "scripts/player.gd")
    assert code == 0, out
    assert "also being edited by engine: your changes are checked against theirs when you land" in out
    assert folder(project, "ui").is_dir()
    code, out = praxis("claim", "ui", "scripts")  # a folder holding a file engine works on: shared too
    assert code == 0 and "also being edited by engine" in out, out
    code, out = praxis("status")
    assert "SHARED CODE FILES" in out and "scripts/player.gd: engine, ui" in out, out
    # Asset files stay one agent at a time, claimed directly or through their folder.
    code, out = praxis("claim", "ui", "scenes/level1.tscn")
    assert code == 1 and "engine holds the asset file scenes/level1.tscn" in out, out
    code, out = praxis("claim", "ui", "scenes")
    assert code == 1 and "engine holds" in out, out


def test_exclusive_code_true_keeps_one_agent_per_code_file(project):
    set_setting(project, "exclusive_code", "true")
    assert praxis("start", "engine", "physics", "scripts/player.gd")[0] == 0
    code, out = praxis("start", "ui", "menus", "scripts/player.gd")
    assert code == 1 and "engine holds scripts/player.gd" in out, out
    assert praxis("start", "ui", "menus", "scripts/enemy.gd")[0] == 0
    edit(folder(project, "ui") / "scripts" / "player.gd", "extends Node\nvar speed = 99\n")
    code, out = praxis("land", "ui")
    assert code == 2 and "scripts/player.gd (held by engine)" in out, out


def test_edits_to_one_shared_file_that_do_not_clash_both_land(project):
    commit_in_main(project, "scripts/player.gd", PLAYER)
    praxis("start", "engine", "physics", "scripts/player.gd")
    praxis("start", "ui", "menus", "scripts/player.gd")
    edit(folder(project, "engine") / "scripts" / "player.gd", PLAYER.replace("line 2\n", "line 2 engine\n"))
    edit(folder(project, "ui") / "scripts" / "player.gd", PLAYER.replace("line 18\n", "line 18 ui\n"))
    code, out = praxis("land", "engine")
    assert code == 0, out
    code, out = praxis("land", "ui")  # engine still holds the file: that no longer stops ui
    assert code == 0, out
    landed = git(project, "show", "main:scripts/player.gd")
    assert "line 2 engine" in landed and "line 18 ui" in landed


def test_a_real_clash_is_refused_naming_whose_landing_and_the_folder_is_left_clean(project):
    commit_in_main(project, "scripts/player.gd", PLAYER)
    praxis("start", "engine", "physics", "scripts/player.gd")
    praxis("start", "ui", "menus", "scripts/player.gd")
    edit(folder(project, "engine") / "scripts" / "player.gd", PLAYER.replace("line 5\n", "line 5 engine\n"))
    edit(folder(project, "ui") / "scripts" / "player.gd", PLAYER.replace("line 5\n", "line 5 ui\n"))
    assert praxis("land", "engine")[0] == 0
    code, out = praxis("land", "ui")
    assert code == 2, out
    assert "scripts/player.gd (clashes with engine's landing)" in out, out
    assert "Update your folder (rebase it on the newest main version)" in out
    ui = folder(project, "ui")
    assert git(ui, "rev-parse", "--abbrev-ref", "HEAD") == "praxis/ui"
    assert git(ui, "status", "--porcelain") == "", "the folder is left clean"
    assert not (ui / ".git").is_file() or "rebase" not in git(ui, "status")
    assert "line 5 ui" in git(ui, "show", "HEAD:scripts/player.gd"), "ui's work is kept, committed"
    assert "line 5 ui" in (ui / "scripts" / "player.gd").read_text()
    # The agent fixes it the way the message says, and lands.
    rebase = subprocess.run(["git", "rebase", "main"], check=False, cwd=str(ui), capture_output=True, text=True)
    assert rebase.returncode != 0
    edit(ui / "scripts" / "player.gd", PLAYER.replace("line 5\n", "line 5 engine and ui\n"))
    git(ui, "add", "scripts/player.gd")
    subprocess.run(["git", "-c", "core.editor=true", "rebase", "--continue"], cwd=str(ui), check=True,
                   capture_output=True, env=dict(os.environ, GIT_EDITOR="true"))
    code, out = praxis("land", "ui")
    assert code == 0, out
    assert "line 5 engine and ui" in git(project, "show", "main:scripts/player.gd")


def test_putting_a_shared_file_back_to_an_older_version_is_refused(project):
    """An agent that rewrites a shared file from an old copy would silently throw away another agent's landing."""
    commit_in_main(project, "scripts/player.gd", PLAYER)
    start = git(project, "rev-parse", "main")
    praxis("start", "engine", "physics", "scripts/player.gd")
    praxis("start", "ui", "menus", "scripts/player.gd")
    engine, ui = folder(project, "engine"), folder(project, "ui")
    edit(engine / "scripts" / "player.gd", PLAYER.replace("line 3\n", "line 3 engine\n"))
    assert praxis("land", "engine")[0] == 0
    edit(ui / "notes.txt", "ui's own work\n")
    praxis("claim", "ui", "notes.txt")
    git(ui, "add", "-A")
    git(ui, "commit", "-qm", "ui's work")
    git(ui, "rebase", "main")
    git(ui, "checkout", start, "--", "scripts/player.gd")  # an old copy of the whole file
    code, out = praxis("land", "ui")
    assert code == 2, out
    assert "scripts/player.gd (undoing engine's landing)" in out and "git checkout main -- <file>" in out, out
    assert "line 3 engine" in git(project, "show", "main:scripts/player.gd"), "engine's work is still in main"


# 2. Generated files -----------------------------------------------------------------------------------------------

def _set_up_generator(project) -> None:
    commit_in_main(project, "tools/gen.py", GEN_PY)
    command = json.dumps(f'"{sys.executable}" tools/gen.py')
    set_setting(project, "generated",
                f'[{{ path = "scripts/all.txt", command = {command}, '
                f'from = ["scripts/player.gd", "scripts/enemy.gd"] }}]')


def test_nobody_claims_a_generated_file(project):
    _set_up_generator(project)
    code, out = praxis("start", "ui", "menus", "scripts/all.txt")
    assert code == 1 and "These files are generated, so nobody claims or edits them" in out, out
    assert "Praxis runs the command in the main folder" in out
    assert praxis("start", "ui", "menus", "scripts/player.gd")[0] == 0
    code, out = praxis("claim", "ui", "scripts/ALL.txt")
    assert code == 1 and "generated" in out


def test_a_landing_that_changes_a_source_rebuilds_the_generated_file(project):
    _set_up_generator(project)
    praxis("start", "engine", "physics", "scripts/player.gd")
    edit(folder(project, "engine") / "scripts" / "player.gd", "extends Node\nvar speed = 42\n")
    code, out = praxis("land", "engine")
    assert code == 0, out
    assert "Rebuilt scripts/all.txt and committed it to main" in out, out
    assert git(project, "log", "-1", "--format=%s", "main") == "Rebuild scripts/all.txt (after engine's landing)"
    assert "var speed = 42" in git(project, "show", "main:scripts/all.txt")
    assert git(project, "status", "--porcelain") == ""
    # A landing that does not touch the sources leaves it alone.
    praxis("start", "ui", "menus", "notes.txt")
    edit(folder(project, "ui") / "notes.txt", "hello\n")
    code, out = praxis("land", "ui")
    assert code == 0 and "Rebuild" not in out, out


def test_a_failed_rebuild_keeps_the_landing_shows_out_of_date_and_is_tried_again(project):
    _set_up_generator(project)
    (project / "fail.flag").write_text("x")
    praxis("start", "engine", "physics", "scripts/enemy.gd")
    edit(folder(project, "engine") / "scripts" / "enemy.gd", "extends Node\nvar hp = 9\n")
    code, out = praxis("land", "engine")
    assert code == 0, out
    assert "could not be rebuilt" in out and "The landing itself is done and stays done" in out, out
    assert "var hp = 9" in git(project, "show", "main:scripts/enemy.gd"), "the landing stays"
    code, out = praxis("status")
    assert "scripts/all.txt: OUT OF DATE" in out and "Output of the last try:" in out, out
    log = re.search(r"Output of the last try: (.+)", out).group(1).strip()
    assert "the generator broke on purpose" in Path(log).read_text(encoding="utf-8", errors="replace")
    (project / "fail.flag").unlink()
    praxis("start", "ui", "menus", "notes.txt")
    edit(folder(project, "ui") / "notes.txt", "hello\n")
    code, out = praxis("land", "ui")  # touches no source, but the file is out of date: tried again
    assert code == 0 and "Rebuilt scripts/all.txt" in out, out
    assert "var hp = 9" in git(project, "show", "main:scripts/all.txt")
    assert "OUT OF DATE" not in praxis("status")[1]


def test_a_clash_only_in_a_generated_file_is_settled_by_rebuilding_it(project):
    _set_up_generator(project)
    praxis("start", "engine", "physics", "scripts/player.gd")
    praxis("start", "ui", "menus", "scripts/enemy.gd")
    for agent, rel, text in (("engine", "scripts/player.gd", "extends Node\nvar speed = 42\n"),
                             ("ui", "scripts/enemy.gd", "extends Node\nvar hp = 9\n")):
        where = folder(project, agent)
        edit(where / rel, text)
        subprocess.run([sys.executable, "tools/gen.py"], cwd=str(where), check=True, capture_output=True)
        git(where, "add", "-A")
        git(where, "commit", "-qm", f"{agent}'s change, with the file made again")
    assert praxis("land", "engine")[0] == 0
    code, out = praxis("land", "ui")  # both made scripts/all.txt: that alone is not a real clash
    assert code == 0, out
    assert "Took the newest main version of the generated file(s) scripts/all.txt" in out, out
    made = git(project, "show", "main:scripts/all.txt")
    assert "var speed = 42" in made and "var hp = 9" in made
    assert git(folder(project, "ui"), "status", "--porcelain") == ""


@pytest.mark.parametrize("value, words", [
    ('[{ path = "a.txt", command = "x" }]', "needs from"),
    ('[{ path = "a.txt", from = ["b.txt"] }]', "needs command"),
    ('[{ path = "a.txt", command = "x", from = ["a.txt"] }]', "cannot be made from itself"),
    ('[{ path = "../a.txt", command = "x", from = ["b.txt"] }]', "needs path"),
    ('[{ path = "a.txt", command = "x", from = ["b.txt"], form = [] }]', "does not know: form"),
    ('"a.txt"', "generated must be a list of tables"),
])
def test_the_generated_setting_is_checked(project, value, words):
    set_setting(project, "generated", value, commit=False)
    code, out = praxis("status")
    assert code == 1 and words in out, out


# 3. A heartbeat that does not lie --------------------------------------------------------------------------------

def _program_in(where) -> subprocess.Popen:
    return subprocess.Popen([sys.executable, "-c", "import time; time.sleep(300)", str(where / "tests")],
                            cwd=str(where), stdin=subprocess.DEVNULL)


def test_a_program_running_in_the_folder_counts_as_work(project):
    praxis("start", "engine", "physics", "scripts/player.gd")
    _age(project, "engine", 30)
    assert "engine has shown no sign of work" in praxis("status")[1]
    proc = _program_in(folder(project, "engine"))
    try:
        out = praxis("status")[1]
        assert "no sign of work" not in out, out
        # A venv's python.exe on Windows is a launcher that starts the real one: one or two processes.
        assert re.search(r"running something now \([12] process(es)? working in its folder\)", out), out
    finally:
        proc.kill()
        proc.wait()


# 4. Safer release-stale ------------------------------------------------------------------------------------------

def test_release_stale_refuses_an_agent_that_may_still_be_working_unless_forced(project):
    praxis("start", "engine", "physics", "scripts/player.gd")
    edited = folder(project, "engine") / "scripts" / "player.gd"
    edit(edited, "extends Node\n# half done\n")
    old = time.time() - 30 * 3600
    os.utime(edited, (old, old))
    _age(project, "engine", 30)
    code, out = praxis("release-stale", "engine")
    assert code == 1, out
    assert "engine: 1 file(s) in its folder have edits not saved yet (scripts/player.gd)" in out, out
    assert "praxis release-stale --force engine" in out
    assert "scripts/player.gd" in praxis("status")[1].split("holds:")[1].split("\n")[0], "claims are kept"
    proc = _program_in(folder(project, "engine"))
    try:
        code, out = praxis("release-stale", "engine")
        assert code == 1 and "program(s) are running inside its folder" in out, out
    finally:
        proc.kill()
        proc.wait()
    code, out = praxis("release-stale", "--force", "engine")
    assert code == 0 and "Released engine's claims (scripts/player.gd)" in out, out
    assert edited.read_text() == "extends Node\n# half done\n", "the work is kept"


def test_a_done_cut_short_after_the_folder_went_is_not_a_problem_and_done_again_finishes(project, monkeypatch):
    from agent_praxis import agents

    praxis("start", "engine", "physics", "scripts/player.gd")
    edit(folder(project, "engine") / "scripts" / "player.gd", "extends Node\nvar speed = 5\n")
    assert praxis("land", "engine")[0] == 0
    real = agents._forget

    def killed(*_a):
        raise KeyboardInterrupt  # what a closed terminal does right after the folder was removed

    monkeypatch.setattr(agents, "_forget", killed)
    assert praxis("done", "engine")[0] == 130
    monkeypatch.setattr(agents, "_forget", real)
    assert not folder(project, "engine").exists()
    out = praxis("status")[1]
    assert "PROBLEM" not in out and "praxis abandon engine" not in out, out
    assert "everything it saved has landed. Finish with: praxis done engine" in out, out
    code, out = praxis("done", "engine")
    assert code == 0, out
    assert "WORKING NOW: nobody" in praxis("status")[1]


def test_the_rules_say_to_stop_programs_only_by_their_own_pid():
    from agent_praxis import instructions
    assert "by their process number (PID)" in instructions.BODY and "taskkill /IM" in instructions.BODY


# 5. Claim wording ------------------------------------------------------------------------------------------------

def test_claiming_a_new_file_says_only_that_it_is_new_with_a_hint_only_for_a_likely_typo(project):
    praxis("start", "engine", "physics")
    code, out = praxis("claim", "engine", "scripts/brand_new_thing.gd")
    assert code == 0 and "does not exist yet: fine for a new file)" in out, out
    assert "check the name" not in out and "did you mean" not in out
    code, out = praxis("claim", "engine", "scripts/playr.gd")
    assert code == 0 and "did you mean scripts/player.gd?" in out, out
