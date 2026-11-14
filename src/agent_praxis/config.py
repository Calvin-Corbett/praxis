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
    except ImportError:  # Python 3.10
        return _mini_toml(text)
    try:
        return tomllib.loads(text)
    except tomllib.TOMLDecodeError as exc:
        raise PraxisError(f"praxis.toml has a mistake in it: {exc}") from exc


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
    data = parse_toml(path.read_text(encoding="utf-8"))
    cfg = Config()
    for name in ("main_branch", "check", "worktree_root", "land_method"):
        if name in data:
            setattr(cfg, name, str(data[name]))
    if "assets" in data:
        cfg.assets = [str(a) for a in data["assets"]]
    if "enough_after" in data:
        cfg.enough_after = max(2, int(data["enough_after"]))
    if "stale_after_hours" in data:
        cfg.stale_after_hours = float(data["stale_after_hours"])
    if cfg.land_method not in ("rebase", "merge"):
        raise PraxisError('praxis.toml: land_method must be "rebase" or "merge".')
    return cfg
