"""The project's own commands around Praxis's steps: `setup` in a new agent folder, `after_land` and `after_done` in the main folder,
and the `share` folders every agent folder links to instead of copying.

None of them has a time limit (a deliberate rule, like the check). None reads the keyboard or opens a window, and
their output goes to a log in .git/praxis/logs, with a short summary printed.
"""

from __future__ import annotations

import os
import subprocess
import time
from pathlib import Path

from agent_praxis import gitops, shells

NO_WINDOW = 0x08000000 if os.name == "nt" else 0  # CREATE_NO_WINDOW: a console program gets no window of its own
TAIL_LINES = 20


def command_env(cfg, **praxis_vars: str) -> dict[str, str]:
    """The environment for a project command: Praxis's own, then praxis.toml's env table, then PRAXIS_* values."""
    env = dict(os.environ)
    env.update(cfg.env)
    env.update(praxis_vars)
    env.setdefault("PYTHONIOENCODING", "utf-8")  # a Python command printing a non-English path must not crash
    return env


def new_log(logs: Path, stem: str) -> Path:
    logs.mkdir(parents=True, exist_ok=True)
    stamp = time.strftime("%Y%m%d-%H%M%S")
    log, n = logs / f"{stem}-{stamp}.log", 2
    while log.exists():
        log, n = logs / f"{stem}-{stamp}-{n}.log", n + 1
    return log


def run_logged(command: str, cwd: Path, env: dict[str, str], log: Path, ctx=None) -> int:
    """Run *command* (as a shell line) in *cwd* until it ends, its output going to *log*. Returns its exit code.
    The log starts with the program that ran (see shells). With *ctx* the project's shell_fix setting decides
    whether a bare `bash` is swapped on Windows, and a swap is said out loud; without it the swap is on and is
    written in the log only."""
    plan = shells.prepare(ctx, command, env, cwd) if ctx is not None else shells.plan(command, env, cwd)
    with open(log, "wb") as out:
        out.write(plan.header(command))
        out.flush()
        try:
            proc = subprocess.Popen(plan.command, shell=True, cwd=str(cwd), env=env, stdin=subprocess.DEVNULL,
                                    stdout=out, stderr=subprocess.STDOUT, creationflags=NO_WINDOW)
        except OSError as exc:
            out.write(f"Could not start it: {exc}\n".encode())
            return 127
        try:
            proc.wait()
        except BaseException:  # stopped (Ctrl+C): stop the command too
            proc.kill()
            proc.wait()
            raise
        out.write(f"\n(exit code {proc.returncode})\n".encode())
    return proc.returncode


def tail(log: Path, lines: int = TAIL_LINES) -> str:
    try:
        text = log.read_bytes()[-64 * 1024:].decode("utf-8", errors="replace")
    except OSError:
        return ""
    return "\n".join(text.rstrip().splitlines()[-lines:])


def run_setup(ctx, row) -> bool:
    """Run praxis.toml's setup command in the agent's folder. True when it passed (or there is none)."""
    command = ctx.cfg.setup.strip()
    if not command:
        return True
    name, folder = row["name"], Path(row["worktree"])
    log = new_log(ctx.store.dir / "logs", f"{name}-setup")
    ctx.say(f"Running the setup command in {name}'s folder: {command}")
    env = command_env(ctx.cfg, PRAXIS_AGENT=name, PRAXIS_TASK=row["task"])
    code = run_logged(command, folder, env, log, ctx)
    if code == 0:
        ctx.say(f"Setup finished. Its output: {log}")
        return True
    ctx.say(f"The setup command failed (exit code {code}), so {name}'s folder may not be ready to work in. The "
            f"folder is kept as it is. The end of its output:\n{tail(log)}\nFull output: {log}")
    ctx.say(f"Fix the problem, then run the setup again: praxis setup {name}")
    return False


def run_after_land(ctx, row, commit: str) -> None:
    """Run praxis.toml's after_land command in the main folder. Its failure is only a warning: the landing is
    done and stays done."""
    command = ctx.cfg.after_land.strip()
    if not command:
        return
    log = new_log(ctx.store.dir / "logs", f"{row['name']}-after-land")
    env = command_env(ctx.cfg, PRAXIS_AGENT=row["name"], PRAXIS_TASK=row["task"], PRAXIS_COMMIT=commit)
    try:
        code = run_logged(command, ctx.repo.root, env, log, ctx)
    except OSError as exc:
        ctx.say(f"Warning: the after_land command could not run ({exc}). The landing itself is done and stays done.")
        return
    if code == 0:
        ctx.say(f"Ran the after_land command: {command}")
        return
    ctx.say(f"Warning: the after_land command failed (exit code {code}). The landing itself is done and stays "
            f"done; only the command after it failed. The end of its output:\n{tail(log)}\nFull output: {log}")


def run_after_done(ctx, row) -> None:
    """Run praxis.toml's after_done command in the main folder. Its failure is only a warning: the agent is done
    and stays done."""
    command = ctx.cfg.after_done.strip()
    if not command:
        return
    log = new_log(ctx.store.dir / "logs", f"{row['name']}-after-done")
    env = command_env(ctx.cfg, PRAXIS_AGENT=row["name"], PRAXIS_TASK=row["task"])
    try:
        code = run_logged(command, ctx.repo.root, env, log, ctx)
    except OSError as exc:
        ctx.say(f"Warning: the after_done command could not run ({exc}). {row['name']} is done and stays done.")
        return
    if code == 0:
        ctx.say(f"Ran the after_done command: {command}")
        return
    ctx.say(f"Warning: the after_done command failed (exit code {code}). {row['name']} is done and stays done; "
            f"only the command after it failed. The end of its output:\n{tail(log)}\nFull output: {log}")


def share_problem(root: Path, rel: str) -> str | None:
    """Why the main folder's *rel* cannot be shared with agent folders, or None when it can."""
    source = root / rel
    if not source.is_dir():
        return f"{rel} is not a folder in the main folder"
    if gitops.git(["ls-files", "-z", "--", rel], root).stdout.strip("\0"):
        return f"{rel} holds files git tracks, so each agent needs its own copy"
    if not gitops.is_ignored(root, rel):
        return f"{rel} is not ignored by git, so git would save what is in it as the agent's work"
    return None


def link_shares(ctx, folder: Path) -> list[str]:
    """Link each `share` folder of the main folder into a new agent folder (a junction on Windows, a symbolic link
    elsewhere). One that cannot be shared is skipped with a warning. Returns what was linked."""
    linked = []
    for rel in ctx.cfg.share:
        rel = rel.replace("\\", "/").strip("/")
        problem = share_problem(ctx.repo.root, rel)
        target = folder / rel
        if problem is None and os.path.lexists(target):
            problem = f"{rel} already exists in the agent's folder"
        if problem:
            ctx.say(f"Warning: did not share {rel}: {problem}. Fix `share` in praxis.toml.")
            continue
        target.parent.mkdir(parents=True, exist_ok=True)
        try:
            make_link(ctx.repo.root / rel, target)
        except OSError as exc:
            ctx.say(f"Warning: could not link {rel} into the agent's folder ({exc}).")
            continue
        ignore_the_link(folder, rel)
        linked.append(rel)
    if linked:
        ctx.say("Shared from the main folder (one copy for every agent, linked, not copied): " + ", ".join(linked))
    return linked


def ignore_the_link(folder: Path, rel: str) -> None:
    """Make git ignore the link itself. A `.gitignore` line like `node_modules/` matches only folders, and to git a
    symbolic link (Linux, macOS) is a file, so without this git would save the link as the agent's work and land it.
    The exact path goes into the project's own exclude list (.git/info/exclude, never committed, shared by the main
    folder and every agent folder), where the main folder's real folder is ignored already anyway."""
    if gitops.is_ignored(folder, rel):
        return
    common = Path(gitops.out(["rev-parse", "--path-format=absolute", "--git-common-dir"], folder))
    exclude = common / "info" / "exclude"
    exclude.parent.mkdir(parents=True, exist_ok=True)
    line = "/" + rel
    old = exclude.read_text(encoding="utf-8") if exclude.is_file() else ""
    if line not in old.splitlines():
        with exclude.open("a", encoding="utf-8", newline="\n") as f:
            f.write(("" if not old or old.endswith("\n") else "\n") + f"# linked into agent folders by praxis share\n{line}\n")


def make_link(source: Path, target: Path) -> None:
    if os.name == "nt":
        import _winapi

        _winapi.CreateJunction(str(source), str(target))  # a junction needs no special Windows permission
    else:
        os.symlink(source, target, target_is_directory=True)
