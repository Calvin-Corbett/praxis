"""On Windows a bare `bash` or `sh` starting a project command means Git Bash, not WSL, and every command log names
the program that ran.

The rules are tested with a made-up Windows machine (a fake `which`, a fake set of files, os_name "nt"), so they
run on every system. The last tests run the real thing, on Windows machines that have Git for Windows.
"""

from __future__ import annotations

import ntpath
import os
import re
import sys
from pathlib import Path

import pytest
from conftest import edit, folder, praxis

from agent_praxis import config, enough, hooks, shells
from agent_praxis.config import Config

WSL = "C:\\Windows\\System32\\bash.exe"
GIT = "C:\\Program Files\\Git\\cmd\\git.exe"
GIT_BASH = "C:\\Program Files\\Git\\bin\\bash.exe"
GIT_SH = "C:\\Program Files\\Git\\usr\\bin\\sh.exe"
ENV = {"SystemRoot": "C:\\Windows", "ProgramFiles": "C:\\Program Files", "PATH": "C:\\Windows\\System32"}


def machine(on_path: dict[str, str], files: tuple[str, ...] = (), os_name: str = "nt") -> dict:
    """A made-up machine: *on_path* is what PATH finds for a name, *files* are the files that exist."""
    there = {ntpath.normcase(f) for f in (*files, *on_path.values())}

    def which(name, path=None):
        if ntpath.dirname(name):  # written with a folder: that file or nothing
            return name if ntpath.normcase(name) in there else None
        return on_path.get(ntpath.splitext(name)[0].lower())

    return {"which": which, "isfile": lambda p: ntpath.normcase(str(p)) in there, "os_name": os_name}


WITH_GIT = machine({"bash": WSL, "git": GIT}, (GIT_BASH, GIT_SH))


# The swap ---------------------------------------------------------------------------------------------------------


def test_a_shell_line_starting_with_bash_runs_git_bash_and_says_so():
    plan = shells.plan("bash tools/run.sh --fast && echo ok", ENV, "C:\\work", **WITH_GIT)
    assert plan.command == f'"{GIT_BASH}" tools/run.sh --fast && echo ok'
    assert WSL in plan.note and GIT_BASH in plan.note and "shell_fix = false" in plan.note
    assert "\n" not in plan.note  # one line
    assert plan.program.startswith(GIT_BASH)


def test_a_command_started_directly_is_swapped_even_when_path_would_pick_git_bash():
    # `praxis run --heavy bash run.sh` starts bash directly, and then Windows looks in System32 BEFORE the PATH.
    usr_bash = "C:\\Program Files\\Git\\usr\\bin\\bash.exe"
    found = machine({"bash": usr_bash, "git": GIT}, (WSL, GIT_BASH))
    plan = shells.plan(["bash", "run.sh", "a b"], ENV, "C:\\work", **found)
    assert plan.command == [GIT_BASH, "run.sh", "a b"] and plan.program == GIT_BASH
    assert WSL in plan.note
    # The same words as one shell line go through cmd.exe, which follows the PATH: Git's own bash already wins.
    line = shells.plan("bash run.sh", ENV, "C:\\work", **found)
    assert line.command == "bash run.sh" and line.note == "" and line.program.startswith(usr_bash)


@pytest.mark.parametrize(
    "line, rest",
    [
        ("bash", ""),
        ("  bash  x.sh", "  x.sh"),
        ("BASH.EXE x.sh", " x.sh"),
        ('"bash" x.sh', " x.sh"),
        ("bash&&echo hi", "&&echo hi"),
        ("bash.exe -c \"echo 'a b'\"", " -c \"echo 'a b'\""),
    ],
)
def test_however_the_bare_name_is_written_it_is_swapped(line, rest):
    assert shells.plan(line, ENV, "C:\\work", **WITH_GIT).command == f'"{GIT_BASH}"{rest}'


def test_sh_that_windows_cannot_find_runs_git_sh():
    plan = shells.plan("sh build.sh", ENV, "C:\\work", **machine({"git": GIT}, (GIT_BASH, GIT_SH)))
    assert plan.command == f'"{GIT_SH}" build.sh'  # Git\bin has no sh.exe here, so Git\usr\bin's is used
    assert "not a program Windows can find" in plan.note and GIT_SH in plan.note


@pytest.mark.parametrize(
    "line",
    [
        "C:\\Windows\\System32\\bash.exe run.sh",
        '"C:/Windows/System32/bash.exe" run.sh',
        ".\\bash run.sh",
        "tools/bash run.sh",
        "bashful run.sh",
        "bash-it run.sh",
        "python bash",
        "echo bash",
        "npm run bash",
    ],
)
def test_a_path_or_another_word_is_left_alone(line):
    plan = shells.plan(line, ENV, "C:\\work", **WITH_GIT)
    assert plan.command == line and plan.note == ""


def test_a_bash_that_is_not_the_wsl_launcher_is_left_alone():
    mine = "C:\\tools\\bash.exe"
    plan = shells.plan("bash run.sh", ENV, "C:\\work", **machine({"bash": mine, "git": GIT}, (GIT_BASH,)))
    assert plan.command == "bash run.sh" and plan.note == "" and plan.program.startswith(mine)


def test_without_git_bash_the_command_runs_as_written_with_a_clear_warning():
    plan = shells.plan("bash run.sh", ENV, "C:\\work", **machine({"bash": WSL}))
    assert plan.command == "bash run.sh"
    assert plan.note.startswith("Warning:") and WSL in plan.note and "cannot see Windows paths" in plan.note
    assert "\n" not in plan.note and plan.program.startswith(WSL)
    # Neither bash found nor Git: nothing to warn about, the shell reports it as it always did.
    nothing = shells.plan("bash run.sh", ENV, "C:\\work", **machine({}))
    assert nothing.command == "bash run.sh" and nothing.note == ""


def test_shell_fix_false_turns_it_off():
    plan = shells.plan("bash run.sh", ENV, "C:\\work", fix=False, **WITH_GIT)
    assert plan.command == "bash run.sh" and plan.note == ""
    assert plan.program.startswith(WSL)  # the log still names what really runs
    direct = shells.plan(["bash", "run.sh"], ENV, "C:\\work", fix=False, **machine({"git": GIT}, (WSL, GIT_BASH)))
    assert direct.command == ["bash", "run.sh"] and direct.note == "" and direct.program == WSL


def test_nothing_is_swapped_on_other_systems():
    linux = machine({"bash": "/usr/bin/bash", "git": "/usr/bin/git"}, os_name="posix")
    plan = shells.plan("bash run.sh", {"PATH": "/usr/bin"}, "/work", **linux)
    assert plan.command == "bash run.sh" and plan.note == ""
    assert plan.program == "/usr/bin/bash (started by the shell /bin/sh)"
    assert shells.plan(["bash", "run.sh"], {"PATH": "/usr/bin"}, "/work", **linux).program == "/usr/bin/bash"


def test_the_notes_never_look_like_a_problem_in_the_check_output():
    # A check's log is read back to count its problems; a note in its header must not be counted as one.
    for found in (WITH_GIT, machine({"bash": WSL}), machine({"git": GIT}, (GIT_BASH,))):
        plan = shells.plan("bash run.sh", ENV, "C:\\work", **found)
        assert plan.note and enough.problem_count(plan.header("bash run.sh").decode()) is None


# Finding Git's bash, and knowing the WSL launcher -----------------------------------------------------------------


@pytest.mark.parametrize(
    "git",
    [
        "C:\\Program Files\\Git\\cmd\\git.exe",
        "C:\\Program Files\\Git\\bin\\git.exe",
        "C:\\Program Files\\Git\\mingw64\\bin\\git.EXE",
        "D:/apps/PortableGit/cmd/git.exe",
    ],
)
def test_git_bash_is_found_next_to_the_git_on_path(git):
    root = ntpath.dirname(ntpath.dirname(git)).replace("\\mingw64", "")
    want = ntpath.join(root, "bin", "bash.exe")
    found = machine({"git": git}, (want, ntpath.join(root, "usr", "bin", "bash.exe")))
    assert shells.git_shell("bash", ENV, which=found["which"], isfile=found["isfile"]) == want


def test_git_bash_is_found_where_git_installs_itself_when_git_is_not_on_path():
    for env, want in (
        (ENV, GIT_BASH),
        ({"PROGRAMFILES(X86)": "C:\\Program Files (x86)"}, "C:\\Program Files (x86)\\Git\\usr\\bin\\bash.exe"),
        (
            {"LOCALAPPDATA": "C:\\Users\\me\\AppData\\Local"},
            "C:\\Users\\me\\AppData\\Local\\Programs\\Git\\bin\\bash.exe",
        ),
    ):
        found = machine({}, (want,))
        assert shells.git_shell("bash", env, which=found["which"], isfile=found["isfile"]) == want
    none = machine({"git": "C:\\Windows\\System32\\git.exe"}, (WSL,))
    assert shells.git_shell("bash", ENV, which=none["which"], isfile=none["isfile"]) is None


@pytest.mark.parametrize(
    "path, launcher",
    [
        (WSL, True),
        ("c:/windows/SYSTEM32/bash.EXE", True),
        ("C:\\Windows\\Sysnative\\bash.exe", True),
        ("C:\\Users\\me\\AppData\\Local\\Microsoft\\WindowsApps\\bash.exe", True),
        (GIT_BASH, False),
        ("C:\\Program Files\\Git\\usr\\bin\\bash.exe", False),
        ("C:\\tools\\System32\\bash.exe", False),
    ],
)
def test_the_wsl_launcher_is_known_by_where_it_lives(path, launcher):
    assert shells.is_wsl_launcher(path, {"SYSTEMROOT": "C:\\WINDOWS"}) is launcher  # Windows hands names in capitals


# The log header ---------------------------------------------------------------------------------------------------


def test_the_header_names_the_command_the_program_and_the_note():
    plan = shells.plan("bash run.sh", ENV, "C:\\work", **WITH_GIT)
    lines = plan.header("bash run.sh").decode().split("\n")
    assert lines[0] == "$ bash run.sh"  # the command as you wrote it
    assert lines[1].startswith(f"Program: {GIT_BASH} (started by the shell ")
    assert lines[2] == plan.note and lines[3:] == ["", ""]
    quiet = shells.plan("python -m pytest", ENV, "C:\\work", **machine({"python": "C:\\Py\\python.exe"}))
    assert quiet.header("python -m pytest").decode().split("\n")[1:] == [
        f"Program: C:\\Py\\python.exe (started by the shell {shells._shell('nt')})",
        "",
        "",
    ]


def test_a_first_word_praxis_cannot_place_is_left_to_the_shell_and_said_so():
    plan = shells.plan("cd tools && build.bat", ENV, "C:\\work", **machine({}))
    assert plan.command == "cd tools && build.bat"
    assert plan.program.startswith("the shell ") and '"cd"' in plan.program
    direct = shells.plan(["nosuchprogram", "x"], ENV, "C:\\work", **machine({}))
    assert '"nosuchprogram"' in direct.program and "could not find" in direct.program


def test_a_program_written_with_a_folder_is_read_from_the_commands_folder():
    here = machine({}, ("C:\\work\\tools\\build.exe", "C:\\Py\\python.exe"))
    assert shells.plan("tools\\build.exe all", ENV, "C:\\work", **here).program.startswith("C:\\work\\tools\\build.exe")
    assert shells.plan('"C:\\Py\\python.exe" -m pytest', ENV, "C:\\work", **here).program.startswith(
        "C:\\Py\\python.exe"
    )


# The setting ------------------------------------------------------------------------------------------------------


def _set(project: Path, key: str, value: str) -> None:
    cfg = project / "praxis.toml"
    text, line = cfg.read_text(encoding="utf-8"), f"{key} = {value}"
    if re.search(rf"(?m)^{key} = ", text):
        text = re.sub(rf"(?m)^{key} = .*$", lambda _m: line, text)
    else:
        text += f"\n{line}\n"
    cfg.write_text(text, encoding="utf-8")


def test_shell_fix_is_a_setting_that_is_on_unless_you_turn_it_off(project):
    assert Config().shell_fix is True and config.load(project).shell_fix is True
    assert "shell_fix = true" in (project / "praxis.toml").read_text()  # praxis init writes it with its note
    _set(project, "shell_fix", "false")
    assert config.load(project).shell_fix is False
    assert config.parse_toml(config.render(Config(shell_fix=False)))["shell_fix"] is False
    _set(project, "shell_fix", '"no"')
    code, out = praxis("status")
    assert code == 1 and "shell_fix must be true or false" in out, out


# All the places that run a project command use it ---------------------------------------------------------------------


def _python_says(word: str) -> str:
    return f'"{sys.executable}" -c "print(\'{word}\')"'


@pytest.fixture
def swapped(monkeypatch):
    """Every plan made while this is on swaps the command for one that prints RAN-INSTEAD, as the Windows rule does
    for a bare bash. Gives the list of (command, fix) each place asked with."""
    asked = []

    def fake(command, env, cwd, fix=True, **_kw):
        asked.append((command, fix))
        other = (
            _python_says("RAN-INSTEAD") if isinstance(command, str) else [sys.executable, "-c", "print('RAN-INSTEAD')"]
        )
        return shells.Plan(other, "THE-PROGRAM", "NOTE-SAID")

    monkeypatch.setattr(shells, "plan", fake)
    return asked


def _log(project: Path, pattern: str) -> str:
    return max((project / ".git" / "praxis" / "logs").glob(pattern), key=lambda p: p.stat().st_mtime).read_text()


def test_the_check_runs_the_swapped_command_and_its_log_names_the_program(project, swapped):
    _set(project, "check", '"bash check.sh"')
    praxis("start", "alice", "jump", "scripts/player.gd")
    edit(folder(project, "alice") / "scripts" / "player.gd", "extends Node\nvar speed = 12\n")
    code, out = praxis("land", "alice")
    assert code == 0 and "NOTE-SAID" in out, out
    assert ("bash check.sh", True) in swapped
    log = _log(project, "alice-*.log")
    assert log.startswith("$ bash check.sh\nProgram: THE-PROGRAM\nNOTE-SAID\n\n") and "RAN-INSTEAD" in log


def test_setup_after_land_and_after_done_run_the_swapped_command(project, swapped):
    for key in ("setup", "after_land", "after_done"):
        _set(project, key, f'"bash {key}.sh"')
    code, out = praxis("start", "alice", "jump", "scripts/player.gd")
    assert code == 0 and "NOTE-SAID" in out, out
    edit(folder(project, "alice") / "scripts" / "player.gd", "extends Node\nvar speed = 12\n")
    code, out = praxis("land", "alice", "--done")
    assert code == 0 and out.count("NOTE-SAID") >= 3, out  # the check, after_land and after_done each said it
    for key, stem in (
        ("setup", "alice-setup-*"),
        ("after_land", "alice-after-land-*"),
        ("after_done", "alice-after-done-*"),
    ):
        assert (f"bash {key}.sh", True) in swapped
        log = _log(project, stem + ".log")
        assert log.startswith(f"$ bash {key}.sh\nProgram: THE-PROGRAM\nNOTE-SAID\n\n") and "RAN-INSTEAD" in log, log


def test_a_heavy_job_runs_the_swapped_command_both_ways(project, swapped):
    code, out = praxis("run", "--heavy", "bash", "job.sh")  # several words: started directly
    assert code == 0 and "NOTE-SAID" in out, out
    assert (["bash", "job.sh"], True) in swapped
    log = _log(project, "*-heavy-*.log")
    assert log.startswith("$ bash job.sh\nProgram: THE-PROGRAM\nNOTE-SAID\n\n") and "RAN-INSTEAD" in log
    code, out = praxis("run", "--heavy", "bash job.sh")  # one word: a shell line
    assert code == 0 and ("bash job.sh", True) in swapped, out


def test_shell_fix_false_reaches_every_place(project, swapped):
    _set(project, "shell_fix", "false")
    _set(project, "setup", '"bash setup.sh"')
    _set(project, "check", '"bash check.sh"')
    praxis("start", "alice", "jump", "scripts/player.gd")
    edit(folder(project, "alice") / "scripts" / "player.gd", "extends Node\nvar speed = 12\n")
    praxis("land", "alice")
    praxis("run", "--heavy", "bash", "job.sh")
    assert [fix for _command, fix in swapped] == [False, False, False] and len(swapped) == 3, swapped


def test_every_log_names_the_program_that_really_ran(project, tmp_path):
    code, out = praxis("run", "--heavy", sys.executable, "-c", "print('hello')")
    assert code == 0, out
    lines = _log(project, "*-heavy-*.log").splitlines()
    assert lines[0].startswith("$ ") and lines[1].startswith("Program: ")
    assert os.path.samefile(lines[1][len("Program: ") :], sys.executable)
    log = tmp_path / "plain.log"
    assert hooks.run_logged(_python_says("hi"), tmp_path, dict(os.environ), log) == 0
    second = log.read_text().splitlines()[1]
    assert second.startswith("Program: ") and " (started by the shell " in second
    assert os.path.samefile(second[len("Program: ") : second.index(" (started by the shell ")], sys.executable)


# The real thing, on a Windows machine with Git for Windows --------------------------------------------------------

_REAL_GIT_BASH = shells.git_shell("bash", dict(os.environ)) if os.name == "nt" else None
real_windows = pytest.mark.skipif(not _REAL_GIT_BASH, reason="needs Windows with Git for Windows")


def _wsl_first(monkeypatch) -> dict[str, str]:
    """This process's environment with System32 first on PATH, as on a normal Windows machine."""
    system32 = os.path.join(os.environ.get("SystemRoot", "C:\\Windows"), "System32")
    if not os.path.isfile(os.path.join(system32, "bash.exe")):
        pytest.skip("this Windows has no WSL launcher in System32")
    monkeypatch.setenv("PATH", system32 + os.pathsep + os.environ["PATH"])
    return dict(os.environ)


@real_windows
def test_real_a_script_named_by_a_windows_path_runs_in_git_bash(project, tmp_path, monkeypatch):
    env = _wsl_first(monkeypatch)
    script = tmp_path / "say it.sh"
    script.write_bytes(b'echo "kernel: $(uname -s)"\nexit 7\n')
    log = tmp_path / "line.log"
    code = hooks.run_logged(f'bash "{script}"', tmp_path, env, log)  # a shell line, as a check or setup is
    text = log.read_text()
    assert code == 7 and re.search(r"kernel: (MINGW|MSYS)", text), text  # WSL would say Linux, or exit 127
    assert f"Program: {_REAL_GIT_BASH}" in text
    code, out = praxis("run", "--heavy", "bash", str(script))  # started directly, as `praxis run --heavy bash x.sh` is
    assert code == 7 and "Git for Windows' bash instead" in out, out
    assert re.search(r"kernel: (MINGW|MSYS)", _log(project, "*-heavy-*.log"))
