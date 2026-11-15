"""praxis.toml: the project's few settings, with defaults picked from what is in the folder."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path

from agent_praxis.errors import PraxisError

FILE_NAME = "praxis.toml"

# Files that git cannot merge in any game project: pictures, sound, video, 3D models, fonts, archives.
COMMON_ASSETS = [
    "*.png", "*.jpg", "*.jpeg", "*.gif", "*.bmp", "*.tga", "*.tif", "*.tiff", "*.psd", "*.exr", "*.hdr",
    "*.webp", "*.ico", "*.wav", "*.mp3", "*.ogg", "*.flac", "*.aif", "*.aiff", "*.mp4", "*.mov", "*.webm",
    "*.fbx", "*.obj", "*.blend", "*.glb", "*.gltf", "*.dae", "*.3ds", "*.max", "*.ma", "*.mb",
    "*.ttf", "*.otf", "*.zip", "*.7z", "*.rar", "*.pdf",
]
ENGINE_ASSETS = {
    # Unity scenes and prefabs are text, but two edits to one file almost never merge into something that works.
    "Unity": ["*.unity", "*.prefab", "*.asset", "*.mat", "*.anim", "*.controller", "*.overrideController",
              "*.physicMaterial", "*.lighting", "*.terrainlayer", "*.spriteatlas", "*.mask", "*.playable"],
    "Unreal": ["*.uasset", "*.umap", "*.upk", "*.udk"],
    "Godot": ["*.tscn", "*.scn", "*.tres", "*.res", "*.escn"],
    "Web game": ["*.wasm", "*.atlas"],
}


@dataclass
class Config:
    main_branch: str = "main"
    check: str = ""
    assets: list[str] = field(default_factory=lambda: list(COMMON_ASSETS))
    worktree_root: str = ""
    land_method: str = "rebase"
    enough_after: int = 3
    stale_after_hours: float = 24.0
    engines: list[str] = field(default_factory=list)
    check_may_change: list[str] = field(default_factory=list)

    def worktree_dir(self, project_root: Path) -> Path:
        if self.worktree_root:
            base = Path(self.worktree_root)
            return base if base.is_absolute() else (project_root / base).resolve()
        # Next to the project, never inside it: game engines scan every file in the project folder.
        return project_root.parent / f"{project_root.name}-praxis"


def detect(root: Path) -> tuple[list[str], list[str], str]:
    """(engines found, asset patterns, a suggested check command) for the folder at *root*."""
    engines: list[str] = []
    if (root / "Assets").is_dir() and (root / "ProjectSettings").is_dir():
        engines.append("Unity")
    if any(root.glob("*.uproject")):
        engines.append("Unreal")
    if (root / "project.godot").is_file():
        engines.append("Godot")
    if (root / "package.json").is_file() or (root / "index.html").is_file():
        engines.append("Web game")
    assets = list(COMMON_ASSETS)
    for engine in engines:
        assets += ENGINE_ASSETS[engine]
    return engines, assets, _suggest_check(root)


def _suggest_check(root: Path) -> str:
    package = root / "package.json"
    if package.is_file():
        try:
            scripts = json.loads(package.read_text(encoding="utf-8")).get("scripts", {})
        except (OSError, ValueError, AttributeError):
            scripts = {}
        for name in ("test", "build"):
            if name in scripts:
                return f"npm run {name}"
    if (root / "pyproject.toml").is_file() or (root / "tests").is_dir():
        return "python -m pytest -q"
    if (root / "Cargo.toml").is_file():
        return "cargo test"
    return ""


def _q(text: str) -> str:
    return json.dumps(text)  # a JSON string is a valid TOML basic string


def render(cfg: Config) -> str:
    found = ", ".join(cfg.engines) if cfg.engines else "no game engine"
    assets = "".join(f"  {_q(a)},\n" for a in cfg.assets)
    return f"""# Praxis settings for this project. Praxis found: {found}.
# Lines starting with # are notes. Change a value, save, and Praxis uses it next time.

# The branch that holds the finished, working version of the project.
main_branch = {_q(cfg.main_branch)}

# The command that proves the project still works (a build or test command).
# Praxis runs it before every landing and lands only if it succeeds.
# Leave it empty ("") and Praxis lands without a check.
check = {_q(cfg.check)}

# Files the check itself rewrites every time it runs (for example "ProjectSettings/*.asset").
# Normally a landing stops if any committed file changes while the check runs, because then the
# check may not have tested what would land. Files listed here are put back to their committed
# version after every check instead (a copy is kept in .git/praxis/kept), and the landing goes ahead.
# Paths are read from the top of the project: * stays inside one folder, ** spans folders.
check_may_change = [{", ".join(_q(p) for p in cfg.check_may_change)}]

# Files git cannot merge (scenes, levels, models, textures, sounds).
# Only one agent at a time may hold one of these.
assets = [
{assets}]

# Where each agent's own folder is made. Empty means next to the project, in "<project>-praxis".
worktree_root = {_q(cfg.worktree_root)}

# How an agent's work is put on top of the latest version: "rebase" (tidy history) or "merge".
land_method = {_q(cfg.land_method)}

# Pause an agent after this many landing tries in a row fail without getting better.
enough_after = {cfg.enough_after}

# Show an agent's claims as stale after this many hours with no sign of work.
stale_after_hours = {cfg.stale_after_hours:g}
"""


def parse_toml(text: str) -> dict:
    """Read praxis.toml. Uses Python's own TOML reader when there is one (3.11+), else a small reader for
    the simple `key = value` lines Praxis writes (strings, numbers, true/false, lists of strings)."""
    try:
        import tomllib  # type: ignore[import-not-found]
    except ImportError:  # Python 3.10: the same reader, installed as tomli
        try:
            import tomli as tomllib  # type: ignore[import-not-found,no-redef]
        except ImportError:
            return _mini_toml(text)
    try:
        return tomllib.loads(text)
    except tomllib.TOMLDecodeError as exc:
        hint = ""
        if "\\" in str(exc) or "escape" in str(exc).lower():
            hint = ("\nA backslash inside double quotes starts a code in TOML. For Windows paths use forward slashes "
                    "(tools/build.bat), or single quotes: check = 'tools\\build.bat'")
        raise PraxisError(f"praxis.toml has a mistake in it: {exc}{hint}") from exc


_STRING = r'"(?:[^"\\]|\\.)*"|\'[^\']*\''


def _value(raw: str, line_no: int):
    raw = raw.strip()
    if re.fullmatch(_STRING, raw):
        return json.loads(raw) if raw.startswith('"') else raw[1:-1]
    if raw in ("true", "false"):
        return raw == "true"
    if raw.startswith("["):
        return [_value(item, line_no) for item in re.findall(_STRING, raw)]
    try:
        return float(raw) if "." in raw else int(raw)
    except ValueError:
        raise PraxisError(f"praxis.toml line {line_no}: cannot read the value {raw!r}.") from None


def _strip_comment(line: str) -> str:
    out, quote, escaped = [], None, False
    for ch in line:
        if quote:
            out.append(ch)
            if escaped:
                escaped = False
            elif ch == "\\" and quote == '"':
                escaped = True
            elif ch == quote:
                quote = None
        elif ch in "\"'":
            quote = ch
            out.append(ch)
        elif ch == "#":
            break
        else:
            out.append(ch)
    return "".join(out).strip()


def _mini_toml(text: str) -> dict:
    data: dict = {}
    pending_key, pending, start = None, "", 0
    for no, line in enumerate(text.splitlines(), 1):
        line = _strip_comment(line)
        if pending_key:
            pending += " " + line
            if "]" in line:
                data[pending_key] = _value(pending, start)
                pending_key = None
            continue
        if not line or line.startswith("["):
            continue
        name, eq, raw = line.partition("=")
        if not eq:
            raise PraxisError(f"praxis.toml line {no}: expected `name = value`.")
        name, raw = name.strip(), raw.strip()
        if raw.startswith("[") and "]" not in raw:
            pending_key, pending, start = name, raw, no
            continue
        data[name] = _value(raw, no)
    return data


def load(root: Path) -> Config:
    path = root / FILE_NAME
    if not path.is_file():
        raise PraxisError(
            f"This project is not set up for Praxis yet (there is no {FILE_NAME} in {root}).\n"
            "Run `praxis init` in the project folder first."
        )
    data = parse_toml(_read_text(path))
    unknown = sorted(set(data) - set(_KINDS))
    if unknown:  # a misspelled `chek = ...` must not mean "land without a check"
        import difflib  # only here: it is slow to import, and only this message needs it

        hints = []
        for name in unknown:
            close = difflib.get_close_matches(name, list(_KINDS), n=1)
            hints.append(f"{name}" + (f" (did you mean {close[0]}?)" if close else ""))
        raise PraxisError("praxis.toml has setting(s) Praxis does not know: " + ", ".join(hints)
                          + ".\nThe settings it reads are: " + ", ".join(_KINDS) + ". Fix the name, or delete the line.")
    cfg = Config()
    for name, value in data.items():
        setattr(cfg, name, _typed(name, value))
    cfg.enough_after = max(2, cfg.enough_after)
    if cfg.land_method not in ("rebase", "merge"):
        raise PraxisError('praxis.toml: land_method must be "rebase" or "merge".')
    if any(ch in cfg.check for ch in "\b\t\f\r\n\v"):
        raise PraxisError(
            "praxis.toml: the check line holds a hidden control character. In TOML a backslash inside double quotes "
            r'starts a code (\b, \t), so check = "tools\build.bat" does not mean what it looks like. Use forward '
            "slashes (tools/build.bat), or single quotes: check = 'tools\\build.bat'"
        )
    return cfg


# Every setting Praxis reads, with the kind of value it must be.
_KINDS = {"main_branch": str, "check": str, "check_may_change": list, "assets": list, "worktree_root": str,
          "land_method": str, "enough_after": int, "stale_after_hours": float}
_KIND_WORDS = {str: 'text in quotes, like "main"', list: 'a list in square brackets, like ["*.png", "*.wav"]',
               int: "a whole number, like 3", float: "a number, like 24"}


def _typed(name: str, value):
    kind = _KINDS[name]
    if kind is str:
        ok = isinstance(value, str)
    elif kind is list:
        ok = isinstance(value, list) and all(isinstance(v, str) for v in value)
    elif kind is int:
        ok = isinstance(value, int) and not isinstance(value, bool)
    else:
        ok = isinstance(value, (int, float)) and not isinstance(value, bool)
    if not ok:
        raise PraxisError(f"praxis.toml: {name} must be {_KIND_WORDS[kind]}, but it is {value!r}.")
    return float(value) if kind is float else value


def _read_text(path: Path) -> str:
    """praxis.toml as text, whatever an editor saved it as (UTF-8 with or without a BOM, or UTF-16, which
    Windows PowerShell's `>` writes)."""
    raw = path.read_bytes()
    if raw.startswith((b"\xff\xfe", b"\xfe\xff")):
        return raw.decode("utf-16")
    try:
        return raw.decode("utf-8-sig")
    except UnicodeDecodeError:
        raise PraxisError(f"{path} is not saved as UTF-8 text. Open it in a text editor and save it as UTF-8 "
                          "(in Notepad: File, Save As, Encoding: UTF-8).") from None
