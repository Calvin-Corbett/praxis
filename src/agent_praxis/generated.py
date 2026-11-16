"""Generated files (praxis.toml `generated`): files a command makes from other files.

Nobody claims or edits one. After a landing whose change touched any of its `from` files, Praxis runs its command in
the main folder, inside the same turn in the landing line, and commits the new file onto main as
"Rebuild <path> (after <agent>'s landing)". If the command fails, the landing stays: the file is marked out of date
(status shows it, with the log) and the next landing tries again.
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path

from agent_praxis import gitops, hooks, paths
from agent_praxis.errors import PraxisError

STATE_FILE = "generated.json"  # {path: {"why": ..., "log": ..., "at": ...}} for each generated file out of date


def entry_for(cfg, path: str) -> dict | None:
    for item in cfg.generated:
        if paths.key(item["path"]) == paths.key(path):
            return item
    return None


def refuse_claims(cfg, wanted: list[str]) -> None:
    """Nobody may claim a generated file: Praxis rebuilds it after every landing that changes what it is made of."""
    found = [entry_for(cfg, path) for path in wanted]
    found = [item for item in found if item is not None]
    if not found:
        return
    lines = [f"  - {g['path']} is made by `{g['command']}` from {', '.join(g['from'])}" for g in found]
    raise PraxisError(
        "These files are generated, so nobody claims or edits them:\n" + "\n".join(lines)
        + "\nAfter each landing that changes the files they are made from, Praxis runs the command in the main "
        "folder and commits the new version itself. Claim and edit the files they are made from instead. "
        "Nothing was claimed."
    )


def out_of_date(store) -> dict[str, dict]:
    try:
        data = json.loads((store.dir / STATE_FILE).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _save(store, data: dict) -> None:
    path = store.dir / STATE_FILE
    if not data:
        try:
            path.unlink()
        except OSError:
            pass
        return
    temp = path.with_suffix(".tmp")
    temp.write_text(json.dumps(data, indent=1), encoding="utf-8")
    os.replace(temp, path)


def rebuild_after_landing(ctx, row, base: str, tip: str) -> None:
    """Run in the landing turn, right after main moved from *base* to *tip*. Rebuilds every generated file whose
    `from` files that landing changed, and every one an earlier rebuild left out of date."""
    if not ctx.cfg.generated:
        return
    pending = out_of_date(ctx.store)
    changed = gitops.changed_between(ctx.repo.root, base, tip)
    wanted = [g for g in ctx.cfg.generated
              if g["path"] in pending or any(paths.covers(src, c) for src in g["from"] for c in changed)]
    if not wanted:
        return
    main = ctx.cfg.main_branch
    where = gitops.branch_checked_out_at(ctx.repo.root, main)
    for item in wanted:
        why, log = _rebuild(ctx, row, item, where)
        if why is None:
            pending.pop(item["path"], None)
        else:
            pending[item["path"]] = {"why": why, "log": log, "at": time.time()}
            ctx.say(f"Warning: {item['path']} could not be rebuilt: {why}. The landing itself is done and stays "
                    "done. The next landing tries the rebuild again." + (f" Its output: {log}" if log else ""))
    _save(ctx.store, pending)


def _rebuild(ctx, row, item: dict, where: Path | None) -> tuple[str | None, str | None]:
    """(None, log) when the file is rebuilt (and committed if it changed), else (why not, log or None)."""
    path, main = item["path"], ctx.cfg.main_branch
    if where is None:
        return (f"the branch {main} is not checked out in any folder, so there is no main folder to run "
                f"`{item['command']}` in"), None
    if path in gitops.dirty_files(where):
        return (f"the main folder ({where}) has unsaved changes to {path}, and Praxis will not overwrite them. "
                "Commit or undo them"), None
    ctx.say(f"Rebuilding {path}: {item['command']}")
    log = hooks.new_log(ctx.store.dir / "logs", f"{row['name']}-rebuild")
    env = hooks.command_env(ctx.cfg, PRAXIS_AGENT=row["name"], PRAXIS_TASK=row["task"])
    try:
        code = hooks.run_logged(item["command"], where, env, log, ctx)
    except OSError as exc:
        return f"the command could not run ({exc})", str(log)
    if code != 0:
        return f"`{item['command']}` failed (exit code {code})", str(log)
    message = ctx.cfg.with_trailers(f"Rebuild {path} (after {row['name']}'s landing)", row["name"], row["task"])
    try:
        made = gitops.commit(where, message, paths=[path])
    except gitops.GitError as exc:
        return f"git would not commit the rebuilt file ({str(exc).strip()})", str(log)
    ctx.say(f"Rebuilt {path} and committed it to {main}." if made else f"Rebuilt {path}: it did not change.")
    return None, str(log)
