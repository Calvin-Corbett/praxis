"""Give a project folder that has never used git a git project of its own, without touching any file in it.

Only `praxis init` does this. It adds the .git folder, plus a .gitignore and a .gitattributes when the folder
has none, then saves everything else as the first commit. If anything goes wrong before that commit exists,
it takes away exactly what it added, so the folder is as it was.
"""

from __future__ import annotations

import os
import shutil
import stat
from pathlib import Path

from agent_praxis import config, gitops
from agent_praxis.errors import PraxisError

FIRST_COMMIT = "Project files, set up by Praxis"
BIG_FILE = 50 * 1024 * 1024        # git handles files this big badly; GitHub warns at 50 MB and refuses 100 MB
BIG_FOLDER_FILES = 10_000          # say "this can take a while" above this many files...
BIG_FOLDER_BYTES = 1024 ** 3       # ...or above this much data
SAVE_NAMES = {"save", "saves", "savegames", "savedata", "saved games", "savegame"}

ALWAYS_IGNORE = [
    "# Clutter the operating system and editors leave behind",
    "Thumbs.db", "ehthumbs.db", "Desktop.ini", ".DS_Store", "._*", "*~", "*.swp", "*.log",
    "__pycache__/", "*.pyc", ".vs/", ".idea/",
]
# Root-anchored (a leading /): only the engine's own folder at the top is ignored, never a folder of yours with
# the same name deeper down, such as Assets/Plugins/Build/.
ENGINE_IGNORE = {
    "Unity": ["/[Ll]ibrary/", "/[Tt]emp/", "/[Oo]bj/", "/[Bb]uild/", "/[Bb]uilds/", "/[Ll]ogs/",
              "/[Uu]ser[Ss]ettings/", "/[Mm]emoryCaptures/", "/*.csproj", "/*.sln", "/*.pidb", "/*.pdb",
              "/*.mdb", "/*.opendb", "/*.VC.db"],
    "Unreal": ["/Binaries/", "/Intermediate/", "/Saved/", "/DerivedDataCache/", "/Plugins/*/Binaries/",
               "/Plugins/*/Intermediate/", "/*.sln"],
    "Godot": ["/.godot/", "/.import/", "/export/"],
    # Not anchored: node_modules/ and dist/ appear at any depth in a JavaScript or Electron project.
    "Web game": ["node_modules/", "dist/", "logs/", ".ruff_cache/", "*.bak"],
}
ENGINE_WHY = {"Unity": "Unity rebuilds these itself", "Unreal": "Unreal rebuilds these itself (Saved/ holds logs, "
              "crash reports and local saves)", "Godot": "Godot rebuilds these itself (export/ holds exported "
              "builds)", "Web game": "npm and the build make these again"}

# Big binary files that git stores badly: kept in Git LFS when it is installed.
LFS_TYPES = ["psd", "png", "jpg", "jpeg", "tga", "tif", "tiff", "exr", "hdr", "bmp",
             "wav", "ogg", "mp3", "flac", "aif", "aiff",
             "fbx", "blend", "glb", "max", "ma", "mb",
             "mp4", "mov", "webm", "avi", "mkv",
             "uasset", "umap",
             "zip", "7z", "rar"]


def _any_case(ext: str) -> str:
    """*.png as a pattern that also matches HERO.PNG on every system (git matches case on Linux and macOS)."""
    return "*." + "".join(f"[{c.lower()}{c.upper()}]" if c.isalpha() else c for c in ext)


def gitignore_text(engines: list[str]) -> str:
    lines = ["# Written by Praxis when it set up git for this project. Git leaves out what is listed here.",
             "# Edit it freely. A line starting with / means only that folder at the top of the project.", ""]
    lines += ALWAYS_IGNORE
    for engine in engines:
        lines += ["", f"# {engine}: {ENGINE_WHY[engine]}", *ENGINE_IGNORE[engine]]
    return "\n".join(lines) + "\n"


def gitattributes_text() -> str:
    lines = ["# Written by Praxis: big binary files (pictures, sound, models, video, archives) are kept in Git LFS,",
             "# so the project history stays small. [pP][nN][gG] means png in any mix of capitals.", ""]
    lines += [f"{_any_case(ext)} filter=lfs diff=lfs merge=lfs -text" for ext in LFS_TYPES]
    return "\n".join(lines) + "\n"


def _size(n: float) -> str:
    for unit in ("bytes", "KB", "MB", "GB"):
        if n < 1024 or unit == "GB":
            return f"{n:.0f} {unit}" if unit == "bytes" else f"{n:.1f} {unit}"
        n /= 1024
    return f"{n:.1f} GB"


def _sizes(root: Path, names: list[str]) -> list[tuple[int, str]]:
    found = []
    for name in names:
        try:
            found.append((os.stat(root / name).st_size, name))
        except OSError:
            found.append((0, name))
    return found


def _z(text: str) -> list[str]:
    return [p for p in text.split("\0") if p]


def refuse_reason(here: Path) -> str | None:
    """Why Praxis must not make *here* a git project of its own, or None when it may."""
    if here.parent == here:
        return f"{here} is the top of a whole drive. Run praxis init in your project's own folder instead."
    try:
        if gitops.same_path(here, Path.home()):
            return ("this is your home folder: Praxis would put everything in it into git. Run praxis init in "
                    "your project's own folder instead.")
    except RuntimeError:
        pass
    for folder in (here, *here.parents):
        if (folder / ".git").exists():  # git could not use it (damaged, or not trusted): never make a second one
            return (f"{folder / '.git'} is already there, but git cannot use it as a project. Praxis will not "
                    "make a new git project over or inside it. Run `git status` there to see what git says.")
    for folder in list(here.parents)[:4]:
        engines, _a, _c = config.detect(folder)
        if engines and engines != ["Web game"] and not config.detect(here)[0]:
            return (f"this folder looks like a part of the {engines[0]} project at {folder}. Run praxis init in "
                    f"{folder} instead, so agents get the whole project.")
    return None


def _git_lfs_works(here: Path) -> bool:
    try:
        return gitops.git(["lfs", "version"], here, check=False).returncode == 0
    except PraxisError:
        return False


def _remove_tree(path: Path) -> None:
    def _writable(func, name, _info):  # git marks its object files read-only on Windows
        os.chmod(name, stat.S_IWRITE)
        func(name)
    shutil.rmtree(path, onerror=_writable)


def create(here: Path, commit: bool = True, say=print) -> None:
    """Make *here* a git project and (with commit) save its files as the first commit."""
    if shutil.which("git") is None:
        raise PraxisError(f"{here} is not a git project yet, and Praxis needs git to set it up, but git is not "
                          "installed (or not on the PATH). Install it from https://git-scm.com/downloads, open a "
                          "new terminal, and run praxis init again. Nothing was changed.")
    why = refuse_reason(here)
    if why:
        raise PraxisError(f"{here} is not inside a git project, and Praxis did not set one up: {why} "
                          "Nothing was changed.")
    say(f"{here} does not use git yet, so Praxis is setting git up for it.")
    say("Praxis never changes, moves or deletes your files: it only adds the .git folder (where git keeps its "
        "copies), and a .gitignore and .gitattributes if they are not there yet.")
    say("A backup copy of the folder is still wise the first time.")
    made: list[Path] = []
    try:
        _create(here, commit, say, made)
    except BaseException:
        for path in reversed(made):  # only what Praxis itself just added
            try:
                _remove_tree(path) if path.is_dir() else path.unlink()
            except OSError:
                say(f"Could not take away {path}; Praxis added it, so you can delete it.")
        if made:
            say("Praxis took away what it had added, so the folder is as it was.")
        raise


def _create(here: Path, commit: bool, say, made: list[Path]) -> None:
    gitops.git(["init", "-q", "-b", "main"], here, check=False)
    if not (here / ".git").exists():
        gitops.git(["init", "-q"], here)  # git older than 2.28 has no -b
        gitops.git(["symbolic-ref", "HEAD", "refs/heads/main"], here)
    made.append(here / ".git")
    # Keep every file's bytes exactly: Git for Windows usually sets core.autocrlf=true for everyone, which would
    # turn LF into CRLF in agent folders (and back on add). Tools that compare exact bytes then break.
    gitops.git(["config", "core.autocrlf", "false"], here)
    engines, _a, _c = config.detect(here)
    created = [".git"]
    if (here / ".gitignore").exists():
        say("Kept your own .gitignore as it is.")
    else:
        (here / ".gitignore").write_text(gitignore_text(engines), encoding="utf-8", newline="\n")
        made.append(here / ".gitignore")
        created.append(".gitignore")
    lfs = _git_lfs_works(here)
    if lfs:
        if (here / ".gitattributes").exists():
            say("Kept your own .gitattributes as it is.")
        else:
            (here / ".gitattributes").write_text(gitattributes_text(), encoding="utf-8", newline="\n")
            made.append(here / ".gitattributes")
            created.append(".gitattributes")
        gitops.git(["lfs", "install", "--local"], here)
    for name in gitops.embedded_projects(here):  # a folder with its own .git would be saved as a bare pointer
        with open(here / ".git" / "info" / "exclude", "a", encoding="utf-8", newline="\n") as fh:
            fh.write(f"/{name}/\n")
        say(f"WARNING: {name}/ is a git project of its own, so Praxis left it out (listed in .git/info/exclude). "
            "Agents will not get it.")
    say(f"Created: {', '.join(created)}.")
    say("Line endings are kept exactly as they are (this project's git never converts CRLF or LF).")
    say("Git leaves out: " + _ignored_summary(here))
    saves = [p.name for p in here.iterdir() if p.is_dir() and p.name.lower() in SAVE_NAMES
             and not gitops.is_ignored(here, p.name)]
    if saves:
        say(f"NOTE: {', '.join(n + '/' for n in saves)} {'looks' if len(saves) == 1 else 'look'} like player "
            "saves, and will be saved in git too. If they should not be, add them to .gitignore before you let "
            "agents work.")
    todo = _z(gitops.git(["ls-files", "-z", "--others", "--exclude-standard"], here).stdout)
    sizes = _sizes(here, todo)
    total = sum(s for s, _ in sizes)
    if not commit:
        say(f"Did not save the {len(todo)} files ({_size(total)}) as a first commit (--no-commit). "
            "Save them yourself: git add -A, then git commit -m \"Project files\".")
        made.clear()
        return
    free = shutil.disk_usage(here).free
    if total + 200 * 1024 * 1024 > free:
        raise PraxisError(f"Git keeps its own copy of every file, so the first save needs about {_size(total)} free, "
                          f"and the disk has only {_size(free)} free. Free some space and run praxis init again. "
                          "Nothing of yours was changed.")
    if len(todo) > BIG_FOLDER_FILES or total > BIG_FOLDER_BYTES:
        say(f"This is a big folder ({len(todo)} files, {_size(total)}): the first save can take several minutes. "
            "Leave the window open until it finishes.")
    else:
        say(f"Saving {len(todo)} files ({_size(total)}) as the first commit...")
    big = sorted((s, n) for s, n in sizes if s > BIG_FILE)[::-1]
    if not lfs:
        say("Git LFS is not installed, so big binary files (pictures, sound, models) are stored in plain git, "
            "which makes the project history grow fast. To use LFS instead: install it from https://git-lfs.com, "
            "run `git lfs install`, then delete the new .git folder and run praxis init again.")
        if big:
            say(f"WARNING: {len(big)} file(s) are over {_size(BIG_FILE)}. Git handles these badly, and GitHub "
                "refuses files over 100 MB. The biggest:")
            for s, n in big[:5]:
                say(f"  {_size(s):>10}  {n}")
    if not todo:
        say("The folder has no files to save yet, so there is no first commit.")
        made.clear()
        return
    gitops.git(["add", "-A"], here)
    gitops.git([*gitops.identity_args(here), "commit", "-q", "-m", FIRST_COMMIT], here)
    made.clear()  # the first commit exists: from here on .git holds your saved project and is never removed
    saved = _sizes(here, _z(gitops.git(["ls-files", "-z"], here).stdout))
    say(f"Saved {len(saved)} files ({_size(sum(s for s, _ in saved))}) as the first commit: \"{FIRST_COMMIT}\".")
    say("The biggest files saved:")
    for s, n in sorted(saved, reverse=True)[:5]:
        say(f"  {_size(s):>10}  {n}")
    say("Check that nothing is missing or left out by mistake: `git status --ignored` lists what git leaves out, "
        "and .gitignore says why.")
    say("")


def _ignored_summary(here: Path) -> str:
    top = sorted(gitops.ignored_entries(here), key=lambda e: (e.count("/") - e.endswith("/"), e.lower()))
    if not top:
        return "nothing."
    shown = ", ".join(top[:12]) + (f" and {len(top) - 12} more" if len(top) > 12 else "")
    return shown + " (they stay on your disk; git just does not save them)."
