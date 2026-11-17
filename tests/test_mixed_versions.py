"""The gate for "Praxis can be updated while agents work": nothing is refused and nothing is lost.

One real project, two really installed Praxis versions, each in a virtual environment of its own: the BASELINE
(the last released one, taken from this project's git history) and this one (the source next to these tests). The
older one has landings and heavy jobs in flight, in the foreground and in the background, running and waiting. In
the middle of that the launcher is switched to this Praxis with `praxis update`, this Praxis starts landings and
heavy jobs of its own, and the older one starts new commands as well. The test passes only if every heavy job ends
with its own exit code, every landing of both versions lands, no more heavy jobs ever ran at once than
`heavy_jobs` allows, and no command of either version printed a refusal.

It takes minutes, so it is marked `mixed`: `pytest -m "not mixed"` leaves it out, plain `pytest` (the full check)
runs it. It needs the project's git history to build the BASELINE, so it also skips itself in a copy of the project
that has no `.git` (unless that version is installed in `~/.praxis-<BASELINE>`), and on a Python older than 3.11,
where the older Praxis would need a package the bare virtual environment does not have.

Bumping the BASELINE, at each release: set BASELINE below to the commit of the release that people have installed
(the newest one this Praxis must be able to take over from under load), and BASELINE_FORMAT to the database
format that release knows (FORMAT in its store.py). If this Praxis has taken a breaking step since that release
(store.KIND), this test is expected to change: say so in the CHANGELOG, because then an update waits for the older
commands instead of working beside them.
"""

from __future__ import annotations

import io
import json
import os
import re
import shutil
import sqlite3
import subprocess
import sys
import tarfile
import tempfile
import time
import uuid
from pathlib import Path

import pytest
from conftest import CHILD_GUARD, SRC, git
from test_safeswitch import an_older_head_leaves_this_praxis_alone, installed_praxis, numbers

from agent_praxis import launcher, store

BASELINE = "b7ddce6"  # the released Praxis this one must be able to take over from, under load
BASELINE_FORMAT = 6  # the database format that release knows

pytestmark = [
    pytest.mark.mixed,
    pytest.mark.skipif(sys.version_info < (3, 11), reason="the older Praxis needs tomli on a Python before 3.11"),
]

ROOT = SRC.parent
NO_WINDOW = 0x08000000 if os.name == "nt" else 0
PATIENCE = 900  # seconds any one step may take on a machine that is busy with other work

# What a refused or failed command prints. None of it may appear anywhere.
REFUSALS = (
    "This project was used by a newer Praxis",
    "Older Praxis commands are still running in this project",
    "Did not take this project over",
    "Praxis does not know",
    "Not switched",
    "hit a problem",
    "NOT landed",
    "Traceback",
)

JOB_PY = """\
import os, sys, time
out, name, code = sys.argv[1], sys.argv[2], int(sys.argv[3])
gate = sys.argv[4] if len(sys.argv) > 4 else ""
os.makedirs(os.path.join(out, "jobs"), exist_ok=True)
path = os.path.join(out, "jobs", name + ".txt")
def note(word):
    with open(path, "a", encoding="utf-8") as f:
        f.write(f"{word} {time.time()!r}\\n")
note("start")
while gate and not os.path.exists(gate):
    time.sleep(0.1)
time.sleep(1.0)
note("end")
sys.exit(code)
"""

# The landing check: one heavy job through whatever `praxis` the PATH gives at that moment (the launcher).
CHECK_PY = """\
import os, subprocess, sys
out, job = sys.argv[1], sys.argv[2]
name = "check-" + os.environ.get("PRAXIS_AGENT", "nobody") + "-" + str(os.getpid())
command = ["praxis", "run", "--heavy", sys.executable, job, out, name, "0"]
if os.name == "nt":
    done = subprocess.run(subprocess.list2cmdline(command), shell=True, capture_output=True, text=True)
else:
    done = subprocess.run(command, capture_output=True, text=True)
os.makedirs(os.path.join(out, "checks"), exist_ok=True)
with open(os.path.join(out, "checks", name + ".txt"), "w", encoding="utf-8") as f:
    f.write(f"exit {done.returncode}\\n{done.stdout}\\n{done.stderr}\\n")
print(done.stdout, done.stderr)
sys.exit(done.returncode)
"""


# --- two real installs -------------------------------------------------------------------------------------------------


def _python_of(venv: Path) -> Path:
    return venv / "Scripts" / "python.exe" if os.name == "nt" else venv / "bin" / "python"


def _install(venv: Path, package: Path) -> Path:
    """A real install: a virtual environment of its own with that agent_praxis in its site-packages."""
    subprocess.run(
        [sys.executable, "-m", "venv", "--without-pip", str(venv)],
        check=True,
        capture_output=True,
        creationflags=NO_WINDOW,
    )
    python = _python_of(venv)
    where = subprocess.run(
        [str(python), "-c", "import sysconfig; print(sysconfig.get_paths()['purelib'])"],
        check=True,
        capture_output=True,
        text=True,
        creationflags=NO_WINDOW,
    ).stdout.strip()
    shutil.copytree(package, Path(where) / "agent_praxis", ignore=shutil.ignore_patterns("__pycache__"))
    return python


def _baseline_source(into: Path) -> Path | None:
    """The BASELINE's agent_praxis folder, taken out of this project's git history. None when it is not there."""
    known = subprocess.run(
        ["git", "rev-parse", "--verify", "--quiet", f"{BASELINE}^{{commit}}"],
        cwd=str(ROOT),
        check=False,
        capture_output=True,
        creationflags=NO_WINDOW,
    )
    if known.returncode != 0:
        return None
    packed = subprocess.run(
        ["git", "archive", "--format=tar", BASELINE, "src/agent_praxis"],
        cwd=str(ROOT),
        check=True,
        capture_output=True,
        creationflags=NO_WINDOW,
    ).stdout
    with tarfile.open(fileobj=io.BytesIO(packed)) as tar:
        tar.extractall(into, **({"filter": "data"} if hasattr(tarfile, "data_filter") else {}))
    return into / "src" / "agent_praxis"


def _baseline_python() -> Path | None:
    """The Python of an installed BASELINE: built once from git and kept in the temporary folder for later runs (a
    commit's source never changes), or the one a person installed in ~/.praxis-<BASELINE>."""
    version = f"py{sys.version_info.major}{sys.version_info.minor}"
    kept = Path(tempfile.gettempdir()) / "praxis-mixed-versions" / f"{BASELINE}-{version}"
    if _python_of(kept).is_file() and (kept / "complete").is_file():
        return _python_of(kept)
    building = kept.with_name(f"{kept.name}.{uuid.uuid4().hex[:8]}")
    building.parent.mkdir(parents=True, exist_ok=True)
    source = _baseline_source(building / "source")
    if source is None:
        shutil.rmtree(building, ignore_errors=True)
        person = installed_praxis(BASELINE)
        return person if person.is_file() else None
    _install(building / "venv", source)
    try:
        os.replace(building / "venv", kept)  # in one step: another test process may be building it too
        (kept / "complete").write_text(BASELINE, encoding="utf-8")
    except OSError:
        pass  # the other one was first
    shutil.rmtree(building, ignore_errors=True)
    return _python_of(kept) if (kept / "complete").is_file() else None


@pytest.fixture(scope="module")
def older_python() -> Path:
    python = _baseline_python()
    if python is None:
        pytest.skip(
            f"this copy of the project has no git history to build Praxis {BASELINE} from, and none is "
            f"installed in ~/.praxis-{BASELINE}"
        )
    return python


@pytest.fixture(scope="module")
def this_python(tmp_path_factory) -> Path:
    return _install(tmp_path_factory.mktemp("this-praxis") / "venv", SRC / "agent_praxis")


# --- the scenario's helpers ---------------------------------------------------------------------------------------------


class Scene:
    def __init__(self, tmp_path: Path, older: Path, newer: Path):
        self.root = tmp_path / "proj"
        self.out = tmp_path / "out"
        self.home = Path(os.environ["PRAXIS_HOME"])
        self.older, self.newer = older, newer
        self.gate = tmp_path / "go"
        self.job = tmp_path / "job.py"
        self.check = tmp_path / "check.py"
        self.env = {k: v for k, v in os.environ.items() if k != "PYTHONPATH"}
        self.env["PYTHONPATH"] = str(CHILD_GUARD)  # the guard only: each install finds its own code by itself
        self.env["PATH"] = str(self.home / "bin") + os.pathsep + self.env["PATH"]
        self.procs: list[dict] = []
        self.n = 0

    @property
    def db(self) -> Path:
        return self.root / ".git" / "praxis" / "praxis.db"

    def folder(self, agent: str) -> Path:
        return self.root.parent / f"{self.root.name}-praxis" / agent

    def start(self, label: str, python: Path, *args: str, cwd: Path | None = None) -> dict:
        """Start `praxis <args>` of the install at *python*, its output going to files."""
        self.n += 1
        logs = self.out / "commands"
        logs.mkdir(parents=True, exist_ok=True)
        path = logs / f"{self.n:02d}-{label}.txt"
        handle = open(path, "wb")  # noqa: SIM115 - closed in wait()
        proc = subprocess.Popen(
            [str(python), "-m", "agent_praxis", *args],
            cwd=str(cwd or self.root),
            env=self.env,
            stdin=subprocess.DEVNULL,
            stdout=handle,
            stderr=subprocess.STDOUT,
            creationflags=NO_WINDOW,
        )
        record = {"label": label, "proc": proc, "path": path, "handle": handle, "code": None}
        self.procs.append(record)
        return record

    def wait(self, record: dict, timeout: float = PATIENCE) -> tuple[int, str]:
        try:
            record["code"] = record["proc"].wait(timeout=timeout)
        finally:
            record["handle"].close()
        return record["code"], self.said(record)

    def said(self, record: dict) -> str:
        return record["path"].read_text(encoding="utf-8", errors="replace")

    def run(self, label: str, python: Path, *args: str, cwd: Path | None = None) -> tuple[int, str]:
        return self.wait(self.start(label, python, *args, cwd=cwd))

    def heavy(self, label: str, python: Path, code: int, gated: bool = False) -> dict:
        args = ["run", "--heavy", sys.executable, str(self.job), str(self.out), label, str(code)]
        return self.start(label, python, *args, *([str(self.gate)] if gated else []))

    def rows(self, table: str) -> list[dict]:
        conn = sqlite3.connect(self.db.as_uri() + "?mode=ro", uri=True, timeout=60)
        conn.row_factory = sqlite3.Row
        try:
            return [dict(row) for row in conn.execute(f"SELECT * FROM {table} ORDER BY id")]
        except sqlite3.OperationalError:
            return []
        finally:
            conn.close()

    def until(self, what: str, done, timeout: float = PATIENCE) -> None:
        end = time.time() + timeout
        while time.time() < end:
            if done():
                return
            time.sleep(0.3)
        pytest.fail(f"gave up waiting for {what}.\n{self.everything()}", pytrace=False)

    def everything(self) -> str:
        """All that was printed, for a failure message."""
        parts = []
        for path in sorted(self.out.rglob("*.txt")) + sorted((self.root / ".git" / "praxis").rglob("*.log")):
            try:
                parts.append(f"--- {path.name}\n{path.read_text(encoding='utf-8', errors='replace')[-1500:]}")
            except OSError:
                pass
        return "\n".join(parts)

    def stop_what_is_left(self) -> None:
        """Only processes this test started: its own commands, and the background landings they started (their
        process numbers are in the project's database)."""
        for record in self.procs:
            if record["proc"].poll() is None:
                record["proc"].kill()
            if not record["handle"].closed:
                record["handle"].close()
        for row in self.rows("background"):
            if row.get("ended") is None and row.get("pid"):
                if os.name == "nt":
                    subprocess.run(
                        ["taskkill", "/PID", str(row["pid"]), "/T", "/F"],
                        check=False,
                        capture_output=True,
                        creationflags=NO_WINDOW,
                    )
                else:
                    try:
                        os.kill(int(row["pid"]), 9)
                    except OSError:
                        pass


def _most_at_once(jobs_dir: Path) -> tuple[int, int]:
    """(the most heavy jobs that ever ran at the same moment, how many ran in all), from what the jobs wrote."""
    spans = []
    for path in jobs_dir.glob("*.txt"):
        times = dict(line.split() for line in path.read_text(encoding="utf-8").splitlines())
        spans.append((float(times["start"]), float(times["end"])))
    edges = sorted(
        [(start, 1) for start, _end in spans] + [(end, -1) for _start, end in spans], key=lambda e: (e[0], e[1])
    )
    most = now = 0
    for _when, step in edges:
        now += step
        most = max(most, now)
    return most, len(spans)


def _set(path: Path, key: str, value: str) -> None:
    text, line = path.read_text(encoding="utf-8"), f"{key} = {value}"
    text = (
        re.sub(rf"(?m)^{key} = .*$", lambda _m: line, text)
        if re.search(rf"(?m)^{key} = ", text)
        else f"{text}\n{line}\n"
    )
    path.write_text(text, encoding="utf-8")


def _project(scene: Scene, agents: list[str]) -> None:
    """A project the OLDER Praxis set up (so its praxis.toml holds only settings that Praxis knows), with a launcher
    that runs the older Praxis, and an agent with saved work for every name."""
    scene.root.mkdir(parents=True)
    scene.out.mkdir()
    scene.job.write_text(JOB_PY, encoding="utf-8")
    scene.check.write_text(CHECK_PY, encoding="utf-8")
    for name in agents:
        (scene.root / f"{name}.txt").write_text(f"{name} original\n", encoding="utf-8")
    git(scene.root, "init", "-q", "-b", "main")
    git(scene.root, "config", "user.name", "Test")
    git(scene.root, "config", "user.email", "test@example.com")
    git(scene.root, "config", "core.autocrlf", "false")
    git(scene.root, "add", "-A")
    git(scene.root, "commit", "-q", "-m", "first version")
    code, out = scene.run("older-init", scene.older, "init")
    assert code == 0, out
    words = " ".join(f'"{Path(p).as_posix()}"' for p in (sys.executable, scene.check, scene.out, scene.job))
    _set(scene.root / "praxis.toml", "check", f"'{words}'")
    _set(scene.root / "praxis.toml", "heavy_jobs", "2")
    git(scene.root, "commit", "-q", "-am", "the check")
    code, out = scene.run("older-launcher", scene.older, "launcher")
    assert code == 0 and launcher._same(launcher.runs_now(scene.home) or "", str(scene.older)), out
    for name in agents:
        code, out = scene.run(f"older-start-{name}", scene.older, "start", name, f"work of {name}", f"{name}.txt")
        assert code == 0, out
        (scene.folder(name) / f"{name}.txt").write_text(f"{name} changed\n", encoding="utf-8")
        git(scene.folder(name), "commit", "-q", "-am", f"{name}: change")


# --- the gate ------------------------------------------------------------------------------------------------------------


def test_this_praxis_takes_over_from_the_baseline_under_load_and_nothing_is_refused_or_lost(
    tmp_path, older_python, this_python
):
    scene = Scene(tmp_path, older_python, this_python)
    old, new = scene.older, scene.newer
    try:
        _project(scene, ["a1", "a2", "a3", "a4", "a5"])
        assert numbers(scene.root) == (BASELINE_FORMAT, BASELINE_FORMAT)

        # The older Praxis at work: two heavy jobs running (they hold both places until the gate opens), two
        # waiting, a landing in the foreground whose check waits for a heavy job place, and one in the background.
        expected = {"R1": 0, "R2": 7, "W1": 3, "W2": 0}
        jobs = {"R1": scene.heavy("R1", old, 0, gated=True), "R2": scene.heavy("R2", old, 7, gated=True)}
        scene.until("the two running heavy jobs", lambda: len([r for r in scene.rows("heavy") if r["started"]]) == 2)
        jobs["W1"] = scene.heavy("W1", old, 3)
        scene.until("the first waiting job", lambda: len(scene.rows("heavy")) == 3)
        jobs["W2"] = scene.heavy("W2", old, 0)
        scene.until("the second waiting job", lambda: len(scene.rows("heavy")) == 4)
        landings = {"a1": scene.start("older-land-a1", old, "land", "a1", cwd=scene.folder("a1"))}
        scene.until("a1's check to be waiting for a heavy job place", lambda: len(scene.rows("heavy")) >= 5)
        code, out = scene.run("older-land-a2-background", old, "land", "a2", "--background", cwd=scene.folder("a2"))
        assert code == 0 and "carries on in the background" in out, out

        # The switch, in the middle of all that.
        code, out = scene.run("this-update", new, "update")
        assert code == 0 and "Switched" in out and "only adds to its database" in out, out
        assert "carry on beside this one" in out and "land a1" in out, out
        assert launcher._same(launcher.runs_now(scene.home) or "", str(new)), "the launcher runs this Praxis now"
        assert numbers(scene.root) == (BASELINE_FORMAT, BASELINE_FORMAT), "looking changed nothing"

        code, out = scene.run("this-status", new, "status")
        assert code == 0 and "OLDER PRAXIS VERSIONS ARE AT WORK HERE" in out and "They carry on as usual" in out, out
        assert numbers(scene.root) == (store.FORMAT, BASELINE_FORMAT), "this Praxis added what it needs, at once"

        # This Praxis goes to work beside the older one ...
        expected.update({"N1": 5, "N2": 0})
        jobs["N1"] = scene.heavy("N1", new, 5)
        jobs["N2"] = scene.heavy("N2", new, 0)
        landings["a3"] = scene.start("this-land-a3", new, "land", "a3", cwd=scene.folder("a3"))
        scene.until("a3 in the landing line", lambda: "a3" in [q["agent"] for q in scene.rows("queue")])
        code, out = scene.run("this-land-a4-background", new, "land", "a4", "--background", cwd=scene.folder("a4"))
        assert code == 0 and "carries on in the background" in out, out

        # ... and the older one can still start whatever it likes in the database this one has added to.
        code, out = scene.run("older-status", old, "status")
        assert code == 0 and "a5" in out, out
        code, out = scene.run("older-claim", old, "claim", "a5", "more.txt", cwd=scene.folder("a5"))
        assert code == 0, out
        expected["O1"] = 9
        jobs["O1"] = scene.heavy("O1", old, 9)
        landings["a5"] = scene.start("older-land-a5", old, "land", "a5", cwd=scene.folder("a5"))
        scene.until("a5 in the landing line", lambda: "a5" in [q["agent"] for q in scene.rows("queue")])

        scene.gate.write_text("go", encoding="utf-8")  # the two running jobs may end: everything drains

        codes = {name: scene.wait(record)[0] for name, record in jobs.items()}
        assert codes == expected, f"every heavy job ends with its own exit code\n{scene.everything()}"
        for agent, record in landings.items():
            code, out = scene.wait(record)
            assert code == 0 and "Landed" in out, f"{agent} did not land:\n{out}\n{scene.everything()}"
        for agent in ("a2", "a4"):  # the background ones: followed to their end by this Praxis
            code, out = scene.run(f"this-wait-{agent}", new, "wait", agent)
            assert code == 0 and "Landed" in out, f"{agent} did not land:\n{out}\n{scene.everything()}"

        for agent in ("a1", "a2", "a3", "a4", "a5"):
            assert (scene.root / f"{agent}.txt").read_text(encoding="utf-8") == f"{agent} changed\n", agent
        most, ran = _most_at_once(scene.out / "jobs")
        assert most <= 2, f"{most} heavy jobs ran at once, and heavy_jobs is 2"
        assert ran >= len(expected) + 3, "the landing checks ran their heavy jobs too"
        everything = scene.everything()
        for words in REFUSALS:
            assert words not in everything, f"a command said {words!r}:\n{everything}"
        assert not [r for r in scene.procs if r["code"] == 1], "no command was refused"

        code, out = scene.run("this-status-at-the-end", new, "status")
        assert code == 0 and "OLDER PRAXIS" not in out and "LANDING LINE: empty" in out, out
        assert numbers(scene.root) == (store.FORMAT, BASELINE_FORMAT)
        records = [json.loads(p.read_text(encoding="utf-8")) for p in (scene.home / "projects").glob("*.json")]
        assert [Path(r["root"]) for r in records] == [scene.root], "and Praxis remembers the project for next time"
    finally:
        scene.gate.write_text("go", encoding="utf-8")
        scene.stop_what_is_left()


def test_the_baseline_leaves_a_landing_of_this_praxis_out_of_its_batch(project, older_python):
    """The same question test_safeswitch.py asks of the versions a person has installed, asked of the BASELINE."""
    an_older_head_leaves_this_praxis_alone(older_python, project)
