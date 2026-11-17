"""praxis.toml: the project's few settings, with defaults picked from what is in the folder."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path

from agent_praxis import paths
from agent_praxis.errors import PraxisError

FILE_NAME = "praxis.toml"
CONTROL = "\b\t\f\r\n\v"  # control characters a TOML backslash code can leave in a command

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
    after_done: str = ""
    commit_trailers: list[str] = field(default_factory=list)
    env: dict[str, str] = field(default_factory=dict)
    shell_fix: bool = True  # on Windows, a bare `bash` or `sh` starting a command runs Git Bash, not WSL
    setup: str = ""
    share: list[str] = field(default_factory=list)
    exclusive_code: bool = False
    heavy_jobs: int | str = 2  # a number, or "auto": sized from the machine's load each time a job asks
    heavy_max: int = 0  # the most places "auto" may use; 0 means half the CPU cores, at least 2
    heavy_min: int = 0  # the fewest places "auto" gives; 0 means 2 on a machine with 8 or more cores, else 1
    heavy_waiting_per_agent: int = 3  # the most heavy jobs one agent may have WAITING in line; 0 means no cap
    batch_max: int = 4
    generated: list[dict] = field(default_factory=list)
    checks: list[dict] = field(default_factory=list)
    full_check: str = ""  # the command that proves the WHOLE project works: the safety net behind `checks`
    full_check_every: int = 0  # run it after this many landings that did not run it themselves; 0: only on demand
    speculate: bool = False  # start the next waiting agent's check early, while the head's check runs
    audit_every: int = 20  # about one in this many passes used from memory is run again anyway; 0: never
    checks_together: bool = True  # several named checks of one landing run at once when heavy job places are free

    def check_for(self, changed: list[str]) -> tuple[str, str]:
        """(the check command, its name) for a landing that changes the files *changed*: the command of the FIRST
        `checks` entry with a pattern matching any changed file, else the default `check`."""
        for n, entry in enumerate(self.checks, 1):
            if any(paths.matches_from_top(path, entry["paths"]) for path in changed):
                return entry["run"], f"check {n} of `checks` ({', '.join(entry['paths'])})"
        return self.check, "default check"

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

# How many heavy jobs (`praxis run --heavy <command>`: whole test suites, browser runs) may run at once on this
# project, across all agents. The others wait in line. A number, or "auto": Praxis then sizes the limit from the
# machine's load each time a job asks (more places while the CPU and memory are idle, fewer under load, aiming
# for about 75 percent CPU). The heavy jobs' own CPU use is measured and left out of "the machine is busy". Never
# above heavy_max (default: half the CPU cores, at least 2) and never below heavy_min (default: 2 on a machine
# with 8 or more cores, else 1). A job already waiting picks up a change to these three without being restarted.
heavy_jobs = {_q(cfg.heavy_jobs) if isinstance(cfg.heavy_jobs, str) else cfg.heavy_jobs}
{_heavy_bounds(cfg)}
# The most heavy jobs one agent may have WAITING in line at once: one more is refused, with the list of the ones
# waiting. 0 means no cap. The heavy jobs of a landing's check are never refused, and they go first.
heavy_waiting_per_agent = {cfg.heavy_waiting_per_agent}

# Different checks for different changes. Praxis looks at the files a landing changes and runs the `run`
# command of the FIRST entry here whose `paths` match any changed file; otherwise it runs `check` above. So
# put the strictest entries first. Paths are read from the top of the project (* stays inside one folder,
# ** spans folders), for example
# checks = [{{ paths = ["js/thermo.js", "css/**"], run = "npm run test:ui" }}]
#
# Named checks. Give EVERY entry a `name` and the rule changes: every entry whose `paths` match a changed file
# runs (not only the first), and `check` above runs as well when no entry's `paths` match. A named entry may list
# `inputs`, the files its command reads (the same kind of patterns), and `env_inputs`, the environment variables
# it reads. Praxis then remembers a pass for exactly those files: while they, the command and those variables
# stay the same, that check is not run again, whatever else changes in the project. A named entry also runs when
# a landing changes one of its `inputs`. Without `inputs` an entry reads the whole project. For example
# checks = [{{ name = "ui", paths = ["css/**"], inputs = ["css/**", "js/ui/**"], run = "npm run test:ui" }}]
checks = [{_checks(cfg.checks)}]

# The audit behind `inputs`. About one in this many passes used from memory is run again anyway, in a scratch
# folder, after the landing that used it (never holding it up). If it fails there, the `inputs` list is probably
# missing files: `praxis status` says so under NEEDS YOU, and that check runs every time until you run
# `praxis trust <name>`. 0 turns the audit off.
audit_every = {cfg.audit_every}

# When a landing has several named checks to run, they run at the same time (in the same folder) while heavy job
# places (heavy_jobs above) are free: each one beyond the first takes a place. false: always one after another.
checks_together = {"true" if cfg.checks_together else "false"}

# When several agents wait in the landing line, the one at the front lands up to this many of them together: one
# check for the whole batch, and if it fails Praxis halves the batch to find the one that broke it. 1 turns
# batching off (one landing at a time).
batch_max = {cfg.batch_max}

# The full check: the command that proves the WHOLE project works (the complete test suite, say). It is the safety
# net that makes quick `checks` safe to rely on. `praxis fullcheck` runs it now, on the main version, in a scratch
# folder of its own, never in the main folder and never holding up landings. With full_check_every = N, Praxis
# also starts it in the background after every N landings that did not run this exact command themselves (0: only
# when you ask). If it fails, Praxis finds which landing broke it and `praxis status` says so under NEEDS YOU,
# with the exact `git revert` command. Praxis never undoes a landing by itself.
full_check = {_q(cfg.full_check)}
full_check_every = {cfg.full_check_every}

# Early checks. With true, while the agent at the front of the landing line runs its check, Praxis starts the
# check of the next waiting agent early, beside it, on "main + the front agent's work + its own work" in a scratch
# folder. If the front agent lands, the next one finds its files already passed and lands without waiting for a
# check; if not, the early run is stopped and forgotten. It uses more CPU to save waiting: one early check at a
# time, and only when a heavy job place (heavy_jobs above) is free.
speculate = {"true" if cfg.speculate else "false"}

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

# A command run in the main folder after `praxis done` has finished an agent. Praxis sets PRAXIS_AGENT and
# PRAXIS_TASK for it. If it fails, Praxis warns; the agent stays done.
after_done = {_q(cfg.after_done)}

# Lines added as git trailers to the commits Praxis writes itself (saving an agent's unsaved work, the
# leftovers of an abandoned agent, joined commits), for example ["Agent: {{agent}}"]. {{agent}} and {{task}}
# are filled in. Commits an agent makes itself are left as they are.
commit_trailers = [{", ".join(_q(p) for p in cfg.commit_trailers)}]

# Extra environment variables for the check, setup, after_land and after_done commands, for example
# env = {{ TEMP = "D:/tmp", TMP = "D:/tmp" }}
env = {_inline_table(cfg.env)}

# Windows only. A command above (or one given to `praxis run --heavy`) that starts with a bare `bash` or `sh`
# would start WSL's bash (Linux inside Windows), which cannot see Windows paths. With true, Praxis starts
# Git for Windows' bash instead when it finds one, and says so. false leaves the command exactly as written.
shell_fix = {"true" if cfg.shell_fix else "false"}
"""


def _heavy_bounds(cfg: Config) -> str:
    return "".join(f"{name} = {getattr(cfg, name)}\n" for name in ("heavy_max", "heavy_min") if getattr(cfg, name))


def _checks(items: list[dict]) -> str:
    def strings(values: list[str]) -> str:
        return "[" + ", ".join(_q(v) for v in values) + "]"

    def one(c: dict) -> str:
        parts = [f"name = {_q(c['name'])}"] if "name" in c else []
        parts.append(f"paths = {strings(c['paths'])}")
        parts += [f"{key} = {strings(c[key])}" for key in ("inputs", "env_inputs") if key in c]
        return "{ " + ", ".join([*parts, f"run = {_q(c['run'])}"]) + " }"

    return ", ".join(one(c) for c in items)


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
        if name in ("generated", "checks") and raw not in ("[]", ""):
            raise PraxisError(f"praxis.toml: reading `{name}` needs Python 3.11 or newer (or the tomli package "
                              f"installed). Install one of those, or remove the {name} line.")
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
    if isinstance(cfg.heavy_jobs, str) and cfg.heavy_jobs.strip().lower() != "auto":
        raise PraxisError(f'praxis.toml: heavy_jobs must be a whole number (1 or more) or "auto", not '
                          f"{cfg.heavy_jobs!r}.")
    if isinstance(cfg.heavy_jobs, str):
        cfg.heavy_jobs = "auto"
    elif cfg.heavy_jobs < 1:
        raise PraxisError('praxis.toml: heavy_jobs must be 1 or more (or "auto").')
    if cfg.heavy_max < 0:
        raise PraxisError("praxis.toml: heavy_max must be 1 or more (or leave it out).")
    if cfg.heavy_min < 0:
        raise PraxisError("praxis.toml: heavy_min must be 1 or more (or leave it out).")
    if cfg.heavy_max and cfg.heavy_min > cfg.heavy_max:
        raise PraxisError(f"praxis.toml: heavy_min ({cfg.heavy_min}) cannot be more than heavy_max "
                          f"({cfg.heavy_max}). Lower heavy_min, or raise heavy_max.")
    if cfg.heavy_waiting_per_agent < 0:
        raise PraxisError("praxis.toml: heavy_waiting_per_agent must be 0 (no cap) or more.")
    if cfg.batch_max < 1:
        raise PraxisError("praxis.toml: batch_max must be 1 or more (1 turns batching off).")
    if cfg.full_check_every < 0:
        raise PraxisError("praxis.toml: full_check_every must be 0 (only when you ask) or more.")
    if cfg.audit_every < 0:
        raise PraxisError("praxis.toml: audit_every must be 0 (never audit a pass used from memory) or more.")
    if cfg.full_check_every and not cfg.full_check.strip():
        raise PraxisError(
            f"praxis.toml: full_check_every is {cfg.full_check_every}, but full_check is empty, so there is no "
            'full check to run. Set full_check = "<the command that tests the whole project>", or set '
            "full_check_every = 0."
        )
    if any(ch in cfg.full_check for ch in CONTROL):
        raise PraxisError(
            "praxis.toml: the full_check line holds a hidden control character (a backslash inside double quotes "
            "starts a code in TOML). Use forward slashes, or single quotes."
        )
    if cfg.land_method not in ("rebase", "merge"):
        raise PraxisError('praxis.toml: land_method must be "rebase" or "merge".')
    _check_branch_prefix(cfg.branch_prefix)
    _check_trailers(cfg.commit_trailers)
    cfg.generated = _check_generated(cfg.generated)
    cfg.checks = _check_checks(cfg.checks)
    if any(ch in cfg.check for ch in CONTROL):
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


def _check_checks(items: list) -> list[dict]:
    """Each checks entry must be { paths = ["...", ...], run = "..." }, and may have name, inputs and env_inputs
    (see checks.py). Patterns are kept with forward slashes. An entry keeps only the settings it was given."""
    example = 'for example checks = [{ paths = ["js/thermo.js", "css/**"], run = "npm run test:ui" }]'
    named = ('for example checks = [{ name = "ui", paths = ["css/**"], inputs = ["css/**", "js/ui/**"], '
             'run = "npm run test:ui" }]')
    out, seen = [], set()
    for n, item in enumerate(items, 1):
        if not isinstance(item, dict):
            raise PraxisError(f"praxis.toml: checks entry {n} must be a table in braces, {example}.")
        extra = sorted(set(item) - {"paths", "run", "name", "inputs", "env_inputs"})
        if extra:
            raise PraxisError(f"praxis.toml: checks entry {n} has setting(s) Praxis does not know: "
                              + ", ".join(extra) + f". It reads paths, run, name, inputs and env_inputs, {example}.")
        patterns, run = item.get("paths"), item.get("run")
        if (not isinstance(patterns, list) or not patterns
                or not all(isinstance(p, str) and _clean(p) for p in patterns)):
            raise PraxisError(f"praxis.toml: checks entry {n} needs paths, a list of the files or patterns that "
                              f"choose it, {example}.")
        if not isinstance(run, str) or not run.strip():
            raise PraxisError(f"praxis.toml: checks entry {n} needs run, the check command to use, {example}.")
        if any(ch in run for ch in CONTROL):
            raise PraxisError(f"praxis.toml: the run line of checks entry {n} holds a hidden control character "
                              "(a backslash inside double quotes starts a code in TOML). Use forward slashes, or "
                              "single quotes.")
        entry = {"paths": [_clean(p) for p in patterns], "run": run.strip()}
        if "name" in item:
            name = item["name"]
            if not isinstance(name, str) or not _CHECK_NAME.fullmatch(name):
                raise PraxisError(f"praxis.toml: the name of checks entry {n} must be a short word (letters, "
                                  f"numbers, '-', '_' or '.', starting with a letter or number), {named}.")
            if name.casefold() in seen:
                raise PraxisError(f"praxis.toml: two checks entries are named {name}. Each name is used once.")
            seen.add(name.casefold())
            entry["name"] = name
        for key, what in (("inputs", "the files or patterns its command reads"),
                          ("env_inputs", "the names of the environment variables its command reads")):
            if key not in item:
                continue
            values = item[key]
            clean = _clean if key == "inputs" else _env_name
            if not isinstance(values, list) or not values or not all(isinstance(v, str) and clean(v) for v in values):
                raise PraxisError(f"praxis.toml: {key} of checks entry {n} must be a list of {what}, {named}.")
            if "name" not in item:
                raise PraxisError(f"praxis.toml: checks entry {n} has {key} but no name. Praxis remembers a pass "
                                  f"by the check's name: give the entry one, {named}.")
            entry[key] = [clean(v) for v in values]
        out.append(entry)
    unnamed = [n for n, entry in enumerate(out, 1) if "name" not in entry]
    if seen and unnamed:
        raise PraxisError(
            "praxis.toml: some checks entries have a name and some do not (entry "
            + ", ".join(str(n) for n in unnamed) + " has none). With names, EVERY entry whose paths match runs; "
            "without, only the first one does. So give every entry a name, or none."
        )
    return out


_CHECK_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,39}")


def _env_name(name: str) -> str:
    """An environment variable's name, or "" when it cannot be one."""
    name = name.strip()
    return "" if not name or any(ch in name for ch in "= \t\0") else name


def _clean(path: str) -> str:
    """A project path from the top, with forward slashes; "" when it is empty or reaches outside the project."""
    parts = [p for p in path.strip().replace("\\", "/").split("/") if p not in ("", ".")]
    if not parts or ".." in parts or ":" in parts[0]:
        return ""
    return "/".join(parts)


# Every setting Praxis reads, with the kind of value it must be.
_KINDS = {"main_branch": str, "check": str, "check_may_change": list, "assets": list, "worktree_root": str,
          "land_method": str, "enough_after": int, "stale_after_hours": float, "branch_prefix": str, "shell_fix": bool,
          "setup": str, "share": list, "after_land": str, "after_done": str, "commit_trailers": list,
          "env": dict, "exclusive_code": bool, "heavy_jobs": "jobs", "heavy_max": int, "batch_max": int,
          "heavy_min": int, "heavy_waiting_per_agent": int,
          "generated": "tables", "checks": "tables", "full_check": str, "full_check_every": int,
          "speculate": bool, "audit_every": int, "checks_together": bool}
_KIND_WORDS = {str: 'text in quotes, like "main"', list: 'a list in square brackets, like ["*.png", "*.wav"]',
               int: "a whole number, like 3", float: "a number, like 24",
               dict: 'a table of names and text values, like { TEMP = "D:/tmp" }',
               bool: "true or false", "jobs": 'a whole number, like 2, or "auto"', "tables": 'a list of tables in square brackets, like [{ path = "a.js", ... }]'}


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
    elif kind == "jobs":
        ok = (isinstance(value, int) and not isinstance(value, bool)) or isinstance(value, str)
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
