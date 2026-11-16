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
    branch_prefix: str = "praxis/"
    after_land: str = ""
    commit_trailers: list[str] = field(default_factory=list)
    env: dict[str, str] = field(default_factory=dict)
    setup: str = ""
    share: list[str] = field(default_factory=list)
    exclusive_code: bool = False
    generated: list[dict] = field(default_factory=list)

    def with_trailers(self, message: str, agent: str, task: str) -> str:
        """*message* with the commit_trailers lines added as git trailers (after a blank line), for a commit
        Praxis itself writes."""
        lines = [line.format(agent=agent, task=task) for line in self.commit_trailers]
        return message.rstrip("\n") + ("\n\n" + "\n".join(lines) if lines else "") + "\n"

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

# Code (text) files can be shared: several agents may claim and edit the same one, and Praxis refuses a
# landing only when the edits really clash (the same lines). true makes code files one agent at a time too.
exclusive_code = {"true" if cfg.exclusive_code else "false"}

# Files a command makes from other files. Nobody claims or edits them: after each landing that changes one
# of the `from` files, Praxis runs the command in the main folder and commits the new file, for example
# generated = [{{ path = "js/data.js", command = "node tools/gen.cjs", from = ["js/a.js", "js/b.js"] }}]
generated = [{_generated(cfg.generated)}]

# Where each agent's own folder is made. Empty means next to the project, in "<project>-praxis".
worktree_root = {_q(cfg.worktree_root)}

# How an agent's work is put on top of the latest version: "rebase" (tidy history) or "merge".
land_method = {_q(cfg.land_method)}

# Pause an agent after this many landing tries in a row fail without getting better.
enough_after = {cfg.enough_after}

# Show an agent's claims as stale after this many hours with no sign of work.
stale_after_hours = {cfg.stale_after_hours:g}

# The start of every agent's branch name: an agent called claude-1 works on "praxis/claude-1".
branch_prefix = {_q(cfg.branch_prefix)}

# A command Praxis runs in a new agent's folder right after `praxis start` makes it (for example
# "npm install"). If it fails the folder stays; run it again with `praxis setup <agent>`.
setup = {_q(cfg.setup)}

# Folders git ignores that every agent folder links to instead of getting its own copy (for example
# ["node_modules"]). All agents then share ONE copy: a check that writes into it changes it for everyone.
share = [{", ".join(_q(p) for p in cfg.share)}]

# A command run in the main folder after each landing (for example to restart a server). Praxis sets
# PRAXIS_AGENT, PRAXIS_TASK and PRAXIS_COMMIT for it. If it fails, Praxis warns; the landing stays.
after_land = {_q(cfg.after_land)}

# Lines added as git trailers to the commits Praxis writes itself (saving an agent's unsaved work, the
# leftovers of an abandoned agent, joined commits), for example ["Agent: {{agent}}"]. {{agent}} and {{task}}
# are filled in. Commits an agent makes itself are left as they are.
commit_trailers = [{", ".join(_q(p) for p in cfg.commit_trailers)}]

# Extra environment variables for the check, setup and after_land commands, for example
# env = {{ TEMP = "D:/tmp", TMP = "D:/tmp" }}
env = {_inline_table(cfg.env)}
"""


def _generated(items: list[dict]) -> str:
    return ", ".join("{ path = " + _q(g["path"]) + ", command = " + _q(g["command"]) + ", from = ["
                     + ", ".join(_q(f) for f in g["from"]) + "] }" for g in items)


def _inline_table(values: dict[str, str]) -> str:
    if not values:
        return "{}"
    return "{ " + ", ".join(f"{_key(k)} = {_q(v)}" for k, v in values.items()) + " }"


def _key(name: str) -> str:
    return name if re.fullmatch(r"[A-Za-z0-9_-]+", name) else _q(name)


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
    if raw.startswith("{") and raw.endswith("}"):  # an inline table of text values: { TEMP = "D:/tmp" }
        pairs = re.findall(rf"({_STRING}|[A-Za-z0-9_-]+)\s*=\s*({_STRING})", raw[1:-1])
        return {(_value(k, line_no) if k[0] in "\"'" else k): _value(v, line_no) for k, v in pairs}
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
        if name == "generated" and raw not in ("[]", ""):
            raise PraxisError("praxis.toml: reading `generated` needs Python 3.11 or newer (or the tomli package "
                              "installed). Install one of those, or remove the generated line.")
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
    _check_branch_prefix(cfg.branch_prefix)
    _check_trailers(cfg.commit_trailers)
    cfg.generated = _check_generated(cfg.generated)
    if any(ch in cfg.check for ch in "\b\t\f\r\n\v"):
        raise PraxisError(
            "praxis.toml: the check line holds a hidden control character. In TOML a backslash inside double quotes "
            r'starts a code (\b, \t), so check = "tools\build.bat" does not mean what it looks like. Use forward '
            "slashes (tools/build.bat), or single quotes: check = 'tools\\build.bat'"
        )
    return cfg


_BAD_REF = re.compile(r"[\x00-\x20\x7f~^:?*\[\\]|\.\.|@\{|//")


def _check_branch_prefix(prefix: str) -> None:
    """branch_prefix must be the start of a valid git branch name, ending with "/" (like "praxis/")."""
    parts = prefix[:-1].split("/") if prefix.endswith("/") else []
    bad = (not parts or _BAD_REF.search(prefix) or prefix.startswith("-")
           or any(not p or p.startswith(".") or p.endswith((".lock", ".")) or p == "@" for p in parts))
    if bad:
        raise PraxisError(
            f"praxis.toml: branch_prefix {prefix!r} cannot start a git branch name. Use letters, numbers, '-', '_' "
            "or '.', with folders split by \"/\", and end it with \"/\", for example branch_prefix = \"praxis/\"."
        )


def _check_trailers(lines: list[str]) -> None:
    import string

    for line in lines:
        try:
            names = {name for _text, name, _spec, _conv in string.Formatter().parse(line) if name is not None}
        except ValueError:
            names = {"?"}
        if names - {"agent", "task"} or "\n" in line:
            raise PraxisError(
                f"praxis.toml: the commit_trailers line {line!r} can only use {{agent}} and {{task}} (write {{{{ "
                "and }} for a brace itself), and must be one line, for example \"Agent: {agent}\"."
            )


def _check_generated(items: list) -> list[dict]:
    """Each generated entry must be { path = "...", command = "...", from = ["...", ...] }. Paths are kept with
    forward slashes, from the top of the project."""
    example = ('for example generated = [{ path = "js/data.js", command = "node tools/gen.cjs", '
               'from = ["js/a.js"] }]')
    seen, out = set(), []
    for n, item in enumerate(items, 1):
        if not isinstance(item, dict):
            raise PraxisError(f"praxis.toml: generated entry {n} must be a table in braces, {example}.")
        extra = sorted(set(item) - {"path", "command", "from"})
        if extra:
            raise PraxisError(f"praxis.toml: generated entry {n} has setting(s) Praxis does not know: "
                              + ", ".join(extra) + f". It reads path, command and from, {example}.")
        path, command, sources = item.get("path"), item.get("command"), item.get("from")
        if not isinstance(path, str) or not _clean(path):
            raise PraxisError(f"praxis.toml: generated entry {n} needs path, the file the command makes, {example}.")
        if not isinstance(command, str) or not command.strip():
            raise PraxisError(f"praxis.toml: generated entry {n} ({path}) needs command, the command that makes "
                              f"it, {example}.")
        if (not isinstance(sources, list) or not sources
                or not all(isinstance(s, str) and _clean(s) for s in sources)):
            raise PraxisError(f"praxis.toml: generated entry {n} ({path}) needs from, a list of the files it is "
                              f"made from, {example}.")
        path = _clean(path)
        sources = [_clean(s) for s in sources]
        if path.casefold() in {s.casefold() for s in sources}:
            raise PraxisError(f"praxis.toml: generated entry {n}: {path} cannot be made from itself.")
        if path.casefold() in seen:
            raise PraxisError(f"praxis.toml: {path} is listed twice under generated.")
        seen.add(path.casefold())
        out.append({"path": path, "command": command.strip(), "from": sources})
    return out


def _clean(path: str) -> str:
    """A project path from the top, with forward slashes; "" when it is empty or reaches outside the project."""
    parts = [p for p in path.strip().replace("\\", "/").split("/") if p not in ("", ".")]
    if not parts or ".." in parts or ":" in parts[0]:
        return ""
    return "/".join(parts)


# Every setting Praxis reads, with the kind of value it must be.
_KINDS = {"main_branch": str, "check": str, "check_may_change": list, "assets": list, "worktree_root": str,
          "land_method": str, "enough_after": int, "stale_after_hours": float, "branch_prefix": str,
          "setup": str, "share": list, "after_land": str, "commit_trailers": list, "env": dict,
          "exclusive_code": bool, "generated": "tables"}
_KIND_WORDS = {str: 'text in quotes, like "main"', list: 'a list in square brackets, like ["*.png", "*.wav"]',
               int: "a whole number, like 3", float: "a number, like 24",
               dict: 'a table of names and text values, like { TEMP = "D:/tmp" }',
               bool: "true or false", "tables": 'a list of tables in square brackets, like [{ path = "a.js", ... }]'}


def _typed(name: str, value):
    kind = _KINDS[name]
    if kind is str:
        ok = isinstance(value, str)
    elif kind is list:
        ok = isinstance(value, list) and all(isinstance(v, str) for v in value)
    elif kind is dict:
        ok = isinstance(value, dict) and all(isinstance(k, str) and isinstance(v, str) for k, v in value.items())
    elif kind is bool:
        ok = isinstance(value, bool)
    elif kind == "tables":
        ok = isinstance(value, list)
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
