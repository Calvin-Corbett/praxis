"""Which tests does a change need? Give it the changed files; it prints what to hand to pytest.

    python tools/affected_tests.py FILE [FILE ...]     the changed files, as arguments
    python tools/affected_tests.py --list FILE         the changed files, one per line in a text file
    python tools/affected_tests.py --missing           source and test files the map does not know yet
    python tools/affected_tests.py --add               put those in the map now, the safe way (nothing is run)
    python tools/affected_tests.py --rebuild           run the whole suite once, watched, and write the map again
                                                       (pytest options may follow, for example: --rebuild -n 8)

It prints one line per thing pytest should run: `tests` (the whole suite), or test files, or nothing at all when no
test can be affected (a change to the README). The reason goes to the error output. It always errs on the side of
running more:

- a changed file the map does not know means the whole suite;
- so does a change to a file every test stands on: conftest.py, pyproject.toml, store.py, cli.py, the package's
  __init__.py and __main__.py, tests/child_guard, this tool and its map;
- so does an empty list of changed files, and a map that cannot be read;
- a changed test file always runs itself.

The map (affected_tests.json, next to this file) says for every other project file which test files can be
affected by it. `--rebuild` makes it by running the suite once while tests/child_guard/affected_watch.py writes
down, for each test file, the project files whose functions were called (in the test's own process and in every
Python it started) and the project files that were opened. On top of that comes what the source text says: the
project modules a test file imports, and for each module the modules whose constants and classes it uses (a
constant is read, not called, so watching cannot see it). A source file no test was seen using is sent to the
whole suite. The map holds what happened on the system that made it, so make it where the check runs.

The map only chooses the quick check. The whole suite stays the safety net behind it (`full_check` in praxis.toml).
Only the Python standard library is used here; `--rebuild` also needs pytest.
"""

from __future__ import annotations

import argparse
import ast
import importlib.util
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

EVERYTHING = "tests"  # what pytest is handed for the whole suite; in the map, a file that can affect every test
ROOT = Path(__file__).resolve().parents[1]
MAP = "tools/affected_tests.json"
PACKAGE = "agent_praxis"
SOURCE = "src"

# A change to one of these always means the whole suite, whatever the map says.
ALWAYS = (
    "pyproject.toml",
    f"{SOURCE}/{PACKAGE}/__init__.py",
    f"{SOURCE}/{PACKAGE}/__main__.py",
    f"{SOURCE}/{PACKAGE}/cli.py",
    f"{SOURCE}/{PACKAGE}/store.py",
    "tools/affected_tests.py",
    MAP,
)
ALWAYS_NAMED = ("conftest.py",)  # in any folder
ALWAYS_UNDER = ("tests/child_guard/",)

CODE_FOLDERS = (SOURCE, "tests", "tools")  # where every .py file must be in the map (see missing)
NOT_PROJECT_FILES = {".git", "__pycache__", ".pytest_cache", ".ruff_cache", ".venv", "build", "dist", "node_modules"}

# The same three names as in tests/child_guard/affected_watch.py, which does the watching.
OUT_ENV, ROOT_ENV, NOW_ENV = "AFFECTED_TESTS_WATCH", "AFFECTED_TESTS_ROOT", "AFFECTED_TESTS_NOW"

ABOUT = (
    "Which test files each project file can affect: `tests` means all of them, an empty list none. `untraced` "
    "lists test files added since the suite was last watched; they run with every change. Written by "
    "`python tools/affected_tests.py --rebuild` (or `--add` for new files): do not edit it by hand."
)


# choosing the tests -------------------------------------------------------------------------------------------------


def clean(name: str) -> str:
    """A changed file's name the way the map spells it: from the top of the project, with forward slashes."""
    name = name.strip().replace("\\", "/")
    while name.startswith("./"):
        name = name[2:]
    return name


def is_test_file(name: str) -> bool:
    return name.startswith("tests/") and name.endswith(".py") and name.rsplit("/", 1)[-1].startswith("test_")


def always_everything(name: str) -> bool:
    return name in ALWAYS or name.rsplit("/", 1)[-1] in ALWAYS_NAMED or name.startswith(ALWAYS_UNDER)


def choose(changed, data, root: Path | None = None) -> tuple[list[str], str]:
    """(what pytest should run, why): [EVERYTHING] is the whole suite, [] means no test is needed. With *root*, a
    test file that is no longer there (the change deleted it) is left out."""
    names = sorted({clean(name) for name in changed} - {""})
    if not names:
        return [EVERYTHING], "there is no list of changed files"
    try:
        files, untraced = data["files"], list(data.get("untraced", []))
        picked = set(untraced)
        for name in names:
            if always_everything(name):
                return [EVERYTHING], f"{name} can change the result of any test"
            if name not in files:
                return [EVERYTHING], f"{name} is not in the map"
            if not isinstance(files[name], list) or not all(isinstance(test, str) for test in files[name]):
                raise TypeError(f"the entry for {name} is not a list of file names")
            picked.update(files[name])
            if is_test_file(name):
                picked.add(name)
    except (KeyError, TypeError, AttributeError) as exc:
        return [EVERYTHING], f"the map cannot be read ({exc!r})"
    if EVERYTHING in picked:
        return [EVERYTHING], "the map sends this change to every test"
    if root is not None:
        picked = {test for test in picked if (root / test).is_file()}
    return sorted(picked), f"{len(names)} changed file(s) can affect {len(picked)} test file(s)"


def load(path: Path) -> dict | None:
    """The map, or None when it is not there or is not what this tool wrote."""
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) and isinstance(data.get("files"), dict) else None


def save(path: Path, data: dict) -> None:
    """One line per project file, in order, so two agents' changes to the map seldom touch the same line."""
    files = sorted(data["files"].items())
    lines = ["{", f' "about": {json.dumps(ABOUT)},', ' "files": {']
    lines += [
        f"  {json.dumps(name)}: {json.dumps(sorted(tests))}{',' * (n < len(files))}"
        for n, (name, tests) in enumerate(files, 1)
    ]
    lines += [" },", f' "untraced": {json.dumps(sorted(data.get("untraced", [])))}', "}"]
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8", newline="\n")


# keeping the map up to date -----------------------------------------------------------------------------------------


def code_files(root: Path) -> list[str]:
    """Every .py file under src, tests and tools, from the top of the project."""
    found = []
    for folder in CODE_FOLDERS:
        for path in sorted((root / folder).rglob("*.py")):
            if not NOT_PROJECT_FILES & set(path.relative_to(root).parts):
                found.append(path.relative_to(root).as_posix())
    return found


def missing(root: Path, data: dict | None) -> list[str]:
    """Source and test files the map does not know. Until one is added, any change to it runs the whole suite."""
    files = data["files"] if data else {}
    return [name for name in code_files(root) if not always_everything(name) and name not in files]


def add_missing(root: Path, data: dict | None) -> tuple[dict, list[str]]:
    """The map with every unknown source and test file added the safe way, and with files that are gone taken out.
    A new test file runs with every change, and any other new file runs the whole suite, until the next rebuild."""
    data = {"files": dict(data["files"]) if data else {}, "untraced": list(data.get("untraced", [])) if data else []}
    added = missing(root, data)
    for name in added:
        data["files"][name] = [name] if is_test_file(name) else [EVERYTHING]
        if is_test_file(name):
            data["untraced"].append(name)
    here = {name for name in data["files"] if (root / name).is_file()} | {EVERYTHING}
    data["files"] = {name: [t for t in tests if t in here] for name, tests in data["files"].items() if name in here}
    data["untraced"] = sorted({test for test in data["untraced"] if test in here})
    return data, added


# what the source text says ------------------------------------------------------------------------------------------


def _module_file(root: Path, module: str, beside: str = "") -> str | None:
    """The project file that `import module` loads, or None: a module of the package, or (for a test file) a
    file in the same folder such as conftest.py or another test file."""
    parts = module.split(".")
    if parts[0] == PACKAGE:
        for name in ("/".join([SOURCE, *parts]) + ".py", "/".join([SOURCE, *parts, "__init__.py"])):
            if (root / name).is_file():
                return name
    elif beside and len(parts) == 1 and (root / beside / f"{module}.py").is_file():
        return f"{beside}/{module}.py"
    return None


def _plain_functions(tree: ast.Module) -> set[str]:
    """Names a module's top level defines with `def`. Calling one runs that module's code, and the watching sees
    it; every other top-level name (a constant, a class) can be used without a call into the module."""
    found, blocks = set(), [tree.body]
    while blocks:
        for node in blocks.pop():
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                found.add(node.name)
            elif isinstance(node, (ast.If, ast.Try, ast.With)):
                blocks += [getattr(node, part, []) for part in ("body", "orelse", "finalbody")]
                blocks += [handler.body for handler in getattr(node, "handlers", [])]
    return found


class Sources:
    """The project's Python files as text: what each imports, and whose constants and classes it uses."""

    def __init__(self, root: Path) -> None:
        self.root = root
        self._trees: dict[str, ast.Module | None] = {}
        self._functions: dict[str, set[str]] = {}

    def tree(self, name: str) -> ast.Module | None:
        if name not in self._trees:
            try:
                self._trees[name] = ast.parse((self.root / name).read_text(encoding="utf-8"))
            except (OSError, ValueError, SyntaxError):
                self._trees[name] = None
        return self._trees[name]

    def functions(self, name: str) -> set[str]:
        if name not in self._functions:
            tree = self.tree(name)
            self._functions[name] = _plain_functions(tree) if tree else set()
        return self._functions[name]

    def uses(self, name: str) -> tuple[set[str], set[str]]:
        """(every project file *name* imports, those it uses more of than plain functions)."""
        tree = self.tree(name)
        if tree is None:
            return set(), set()
        beside = name.rsplit("/", 1)[0] if name.startswith("tests/") else ""
        imported, leans, aliases = set(), set(), {}
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    target = _module_file(self.root, alias.name, beside)
                    if target:
                        imported.add(target)
                        leans.add(target)  # `import a.b` then `a.b.NAME`: not followed, so counted in full
            elif isinstance(node, ast.ImportFrom):
                module = ((PACKAGE + "." if node.level else "") + (node.module or "")).rstrip(".")
                target = _module_file(self.root, module, beside)
                for alias in node.names:
                    inner = _module_file(self.root, f"{module}.{alias.name}", beside)
                    if inner:  # `from agent_praxis import heavy`: a module, known here by this name
                        imported.add(inner)
                        aliases[alias.asname or alias.name] = inner
                    elif target:  # `from agent_praxis.heavy import place`: a name out of a module
                        imported.add(target)
                        if alias.name not in self.functions(target):
                            leans.add(target)
        plain = set()  # the uses of a module's name that only reach for one of its plain functions
        for node in ast.walk(tree):
            reaches = isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name)
            if reaches and node.value.id in aliases and node.attr in self.functions(aliases[node.value.id]):
                plain.add(id(node.value))
        for node in ast.walk(tree):
            if isinstance(node, ast.Name) and node.id in aliases and id(node) not in plain:
                leans.add(aliases[node.id])  # a constant, a class, or the module itself handed on
        return imported, leans


# making the map -----------------------------------------------------------------------------------------------------


def project_files(root: Path) -> list[str]:
    """Every file of the project, from its top: what git tracks or would track, or without git every file that
    is not in a cache or build folder."""
    if (root / ".git").exists():
        proc = subprocess.run(
            ["git", "ls-files", "-z", "--cached", "--others", "--exclude-standard"],
            check=False,
            cwd=str(root),
            capture_output=True,
            text=True,
        )
        if proc.returncode == 0:
            return sorted(name for name in proc.stdout.split("\0") if name and (root / name).is_file())
    found = []
    for folder, folders, names in os.walk(root):
        folders[:] = [f for f in folders if f not in NOT_PROJECT_FILES and not f.endswith(".egg-info")]
        found += [(Path(folder) / name).relative_to(root).as_posix() for name in names if not name.endswith(".pyc")]
    return sorted(found)


def build(root: Path, used: dict[str, set[str]]) -> dict:
    """The map, from what the watching saw (*used*: test file -> project files) and what the source text says."""
    known = project_files(root)
    sources = Sources(root)
    tests = [name for name in known if is_test_file(name)]
    follows = {}  # a file -> the files a change to which can reach it without any call being seen
    for name in known:
        if name.endswith(".py"):
            imported, leans = sources.uses(name)
            follows[name] = imported if name.startswith("tests/") else leans
    needs = {}
    for test in tests:
        seen, todo = set(), [test, *used.get(test, ())]
        while todo:
            name = todo.pop()
            if name not in seen:
                seen.add(name)
                todo += follows.get(name, ())
        needs[test] = seen
    files = {}
    for name in known:
        if always_everything(name):
            continue
        users = [test for test in tests if name in needs[test]]
        code = name.endswith(".py") or name.startswith((SOURCE + "/", "tests/"))
        if (code and not users) or (len(tests) > 1 and len(users) == len(tests)):
            users = [EVERYTHING]  # nobody was seen using it: never trust that; or everybody uses it
        files[name] = users
    return {"files": files, "untraced": []}


def watch(root: Path, options: list[str]) -> tuple[int, dict[str, set[str]]]:
    """Run the suite with the watching on: (pytest's exit code, test file -> the project files it used)."""
    guard = root / "tests" / "child_guard"
    with tempfile.TemporaryDirectory(prefix="affected-tests-", ignore_cleanup_errors=True) as out:
        if Path(out).resolve().is_relative_to(root.resolve()):
            raise SystemExit(f"The temporary folder {out} is inside the project: set TMPDIR to a folder outside it.")
        env = dict(os.environ)
        env.update({OUT_ENV: out, ROOT_ENV: str(root)})
        env.pop(NOW_ENV, None)
        env["PYTHONPATH"] = os.pathsep.join(
            [str(guard), str(root / SOURCE), *filter(None, [os.environ.get("PYTHONPATH")])]
        )
        # The watching is loaded before pytest starts, so pytest cannot rewrite its asserts (it has none): no warning.
        quiet = ["-W", "ignore:Module already imported so cannot be rewritten"]
        command = [sys.executable, "-m", "pytest", "-q", "-p", "affected_watch", "-p", "no:cacheprovider", *quiet]
        command += options
        code = subprocess.run(command, check=False, cwd=str(root), env=env).returncode
        used: dict[str, set[str]] = {}
        for record in Path(out).glob("*.txt"):
            for line in record.read_text(encoding="utf-8", errors="replace").splitlines():
                parts = line.split("\t")
                if len(parts) == 3:
                    used.setdefault(parts[0], set()).add(parts[2])
    return code, used


def rebuild(root: Path, path: Path, options: list[str]) -> int:
    if not options and importlib.util.find_spec("xdist"):
        options = ["-n", "auto"]
    code, used = watch(root, options)
    if code != 0:
        print(
            f"The tests did not all pass (pytest ended with {code}), so the map is left as it was: a test that "
            "stops early is not seen using everything it uses.",
            file=sys.stderr,
        )
        return code or 1
    data = build(root, used)
    save(path, data)
    whole = sum(1 for tests in data["files"].values() if EVERYTHING in tests)
    print(f"Wrote {path}: {len(data['files'])} files, {whole} of them send a change to the whole suite.")
    return 0


# the command --------------------------------------------------------------------------------------------------------


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Which tests does a change need?",
        epilog=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("changed", nargs="*", help="the changed files")
    parser.add_argument("--list", metavar="FILE", help="a text file with the changed files, one per line")
    parser.add_argument("--root", type=Path, default=ROOT, help="the project's top folder (default: this one)")
    parser.add_argument("--missing", action="store_true", help="print source and test files the map does not know")
    parser.add_argument("--add", action="store_true", help="add those to the map the safe way, without running")
    parser.add_argument(
        "--rebuild",
        action="store_true",
        help="run the whole suite once and write the map again; it comes last, and what follows it is handed to pytest",
    )
    argv = list(sys.argv[1:] if argv is None else argv)
    options = []
    if "--rebuild" in argv:
        at = argv.index("--rebuild")
        argv, options = argv[: at + 1], argv[at + 1 :]
    args = parser.parse_args(argv)
    root = args.root.resolve()
    path = root / MAP
    if args.rebuild:
        return rebuild(root, path, options)
    data = load(path)
    if args.add:
        data, added = add_missing(root, data)
        save(path, data)
        print(f"Added {len(added)} file(s) to {path}" + (": " + ", ".join(added) if added else "."))
        return 0
    if args.missing:
        print("\n".join(missing(root, data)), end="\n" if missing(root, data) else "")
        return 0
    changed = list(args.changed)
    if args.list:
        try:
            changed += Path(args.list).read_text(encoding="utf-8").splitlines()
        except (OSError, ValueError) as exc:
            print(f"affected tests: the whole suite, because {args.list} cannot be read ({exc})", file=sys.stderr)
            print(EVERYTHING)
            return 0
    picked, why = ([EVERYTHING], f"{path} is missing or damaged") if data is None else choose(changed, data, root)
    what = "the whole suite" if picked == [EVERYTHING] else f"{len(picked)} test file(s)" if picked else "no tests"
    print(f"affected tests: {what}, because {why}", file=sys.stderr)
    print("\n".join(picked), end="\n" if picked else "")
    return 0


if __name__ == "__main__":
    sys.exit(main())
