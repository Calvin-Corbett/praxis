"""Which program a project command starts, and on Windows a bare `bash` or `sh` that means Git Bash, not WSL.

Windows keeps a bash.exe of its own in System32. It starts WSL (Linux inside Windows), which cannot see a Windows
path such as C:\\work\\run.sh: the script "is not found" and the command ends with exit code 127. So when a project
command's first word is a bare `bash` or `sh` and Windows would start that launcher (or nothing at all), Praxis
starts Git for Windows' own instead, when there is one, and says so. `shell_fix = false` in praxis.toml turns the
swap off.

Every place Praxis runs a project command (the check, setup, after_land, after_done, a `generated` rebuild and
`praxis run --heavy`) asks `prepare` here first, and writes the answer's `header` at the top of the command's log,
so each log names the program that ran.

Everything that looks at the machine (`which`, `isfile`, the system's name) can be handed in, and Windows paths are
read with `ntpath`, so the tests run the Windows rules on any system.
"""

from __future__ import annotations

import ntpath
import os
import posixpath
import re
import shutil
import sys
from dataclasses import dataclass

SHELL_NAMES = frozenset({"bash", "sh", "bash.exe", "sh.exe"})
# A shell line's first word: in double quotes, or up to the first space or shell sign.
_FIRST = re.compile(r'\s*(?:"(?P<quoted>[^"]*)"|(?P<plain>[^\s&|<>;()"]+))')


@dataclass(frozen=True)
class Plan:
    """How one project command will be started."""

    command: str | list[str]  # what to hand to Popen: the command, its first word swapped when the fix applied
    program: str  # the log's "Program:" line: the file that runs, and the shell that starts it
    note: str = ""  # one line for the person: what was swapped and why, or the warning. "" when nothing to say

    def header(self, text: str) -> bytes:
        """The top of the command's log: the command as it was written, the program that runs, any note."""
        lines = [f"$ {text}", f"Program: {self.program}"] + ([self.note] if self.note else [])
        return ("\n".join(lines) + "\n\n").encode()


def prepare(ctx, command: str | list[str], env: dict[str, str], cwd) -> Plan:
    """The plan for *command* under the project's settings, saying its note (if any) to the person."""
    found = plan(command, env, cwd, fix=ctx.cfg.shell_fix)
    if found.note:
        ctx.say(found.note)
    return found


def plan(
    command: str | list[str],
    env: dict[str, str],
    cwd,
    fix: bool = True,
    *,
    which=shutil.which,
    os_name: str | None = None,
    isfile=os.path.isfile,
) -> Plan:
    """How to start *command*: a text is a shell line (Popen with shell=True), a list is started directly."""
    os_name = os.name if os_name is None else os_name
    direct = not isinstance(command, str)
    if direct:
        word, rest = (command[0] if command else ""), None
    else:
        match = _FIRST.match(command)
        word = (match.group("quoted") or match.group("plain") or "") if match else ""
        rest = command[match.end() :] if match else command
    found = _would_run(word, env, str(cwd), direct, os_name, which, isfile) if word else None
    note = ""
    if fix and os_name == "nt" and word.lower() in SHELL_NAMES and (found is None or is_wsl_launcher(found, env)):
        name = ntpath.splitext(word)[0].lower()
        ours = git_shell(name, env, which=which, isfile=isfile)
        if ours:
            why = (
                f"would start WSL's {found} (Linux inside Windows, which cannot see Windows paths)"
                if found
                else "is not a program Windows can find here"
            )
            note = (
                f'On Windows a bare "{word}" {why}, so Praxis runs Git for Windows\' {name} instead: {ours} '
                "(shell_fix = false in praxis.toml turns this off)."
            )
            command = [ours, *command[1:]] if direct else f'"{ours}"{rest}'
            found = ours
        elif found:
            note = (
                f'Warning: "{word}" in this command is WSL\'s {found} (Linux inside Windows). It cannot see '
                "Windows paths, so a script named by one is not found (exit code 127). Praxis looked for Git for "
                f"Windows' {name} to run instead and found none: install Git for Windows, or write the full "
                f"path of the {name} you want in the command."
            )
    if direct:
        program = found or f'"{word}" (Praxis could not find its file before starting it)'
    else:
        shell = _shell(os_name)
        program = (
            f"{found} (started by the shell {shell})"
            if found
            else f'the shell {shell} (it finds "{word}" itself: Praxis could not tell the file beforehand)'
        )
    return Plan(command, program, note)


def is_wsl_launcher(path: str, env: dict[str, str]) -> bool:
    """True when *path* is one of the places Windows keeps its WSL launcher: its own System32 folder (also seen as
    Sysnative or SysWOW64), or the per-user WindowsApps folder of a WSL installed from the Store."""
    folder = ntpath.normcase(ntpath.normpath(ntpath.dirname(path)))
    root = ntpath.normcase(ntpath.normpath(_windows_folder(env)))
    return folder in {ntpath.join(root, name) for name in ("system32", "sysnative", "syswow64")} or folder.endswith(
        "\\microsoft\\windowsapps"
    )


def git_shell(name: str, env: dict[str, str], *, which=shutil.which, isfile=os.path.isfile) -> str | None:
    """Git for Windows' own bash.exe or sh.exe (*name* is "bash" or "sh"), or None when there is none. Looked for
    next to the git on PATH first (<git folder>/bin, then <git folder>/usr/bin), then where Git installs itself."""
    roots = []
    git = which("git", path=_get(env, "PATH"))
    if git:
        folder = ntpath.dirname(git)  # <git folder>\cmd, <git folder>\bin or <git folder>\mingw64\bin
        roots += [ntpath.dirname(folder), ntpath.dirname(ntpath.dirname(folder))]
    for var in ("ProgramFiles", "ProgramW6432", "ProgramFiles(x86)"):
        roots.append(ntpath.join(_get(env, var) or "C:\\Program Files", "Git"))
    if _get(env, "LocalAppData"):
        roots.append(ntpath.join(_get(env, "LocalAppData"), "Programs", "Git"))
    for root in roots:
        for sub in ("bin", "usr\\bin"):  # bin's one first: it sets up the Git Bash environment for the real one
            candidate = ntpath.join(root, sub, name + ".exe")
            if isfile(candidate) and not is_wsl_launcher(candidate, env):
                return candidate
    return None


def _would_run(word: str, env: dict[str, str], cwd: str, direct: bool, os_name: str, which, isfile) -> str | None:
    """The file the system starts for a command's first word, or None when that cannot be told beforehand."""
    path = ntpath if os_name == "nt" else posixpath
    if path.dirname(word):  # written with a folder: that file, read from the command's folder
        return which(word if path.isabs(word) else path.join(cwd, word), path=_get(env, "PATH"))
    if os_name != "nt":
        return which(word, path=_get(env, "PATH"))
    if not direct:  # cmd.exe looks in the command's folder, then along the command's PATH
        return which(word, path=ntpath.pathsep.join([cwd, _get(env, "PATH") or ""]))
    # Started directly, Windows looks in the folder of the running Python (in a virtual environment that is the
    # Python it was made from), the current folder and its own System32 and Windows folders BEFORE the PATH
    # (Praxis's PATH, not the command's), and adds only ".exe" to a name with no ending.
    name = word if ntpath.splitext(word)[1] else word + ".exe"
    windows = _windows_folder(env)
    python = ntpath.dirname(getattr(sys, "_base_executable", None) or sys.executable)
    for folder in (python, cwd, ntpath.join(windows, "System32"), windows):
        if isfile(ntpath.join(folder, name)):
            return ntpath.join(folder, name)
    return which(name, path=os.environ.get("PATH"))


def _shell(os_name: str) -> str:
    """The shell Python starts for a shell line (what subprocess does with shell=True)."""
    if os_name != "nt":
        return "/bin/sh"
    return os.environ.get("ComSpec") or ntpath.join(os.environ.get("SystemRoot", ""), "System32", "cmd.exe")


def _windows_folder(env: dict[str, str]) -> str:
    return _get(env, "SystemRoot") or _get(env, "windir") or "C:\\Windows"


def _get(env: dict[str, str], name: str) -> str | None:
    """An environment value by name, whatever its capitals: Windows reads PATH, Path and path as one name."""
    if name in env:
        return env[name]
    upper = name.upper()
    return next((value for key, value in env.items() if key.upper() == upper), None)
