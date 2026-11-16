"""The check result cache: a tree that already passed a check command is not checked again.

A pass is remembered by the git tree hash of the candidate (the exact files that were checked) and the exact text of
the check command, so a different command never reuses it. Only passes are stored, never failures. The newest KEEP
entries stay. It lives in the project's Praxis database (`checkcache` table, database format 4).
"""

from __future__ import annotations

import time

from agent_praxis.store import Store

KEEP = 200


def passed_at(store: Store, tree: str, command: str) -> float | None:
    """When this tree last passed exactly this command, or None."""
    row = store.row("SELECT passed_at FROM checkcache WHERE tree = ? AND command = ?", (tree, command))
    return None if row is None else float(row["passed_at"])


def record(store: Store, tree: str, command: str) -> None:
    """Remember a pass (call this only for a check that succeeded), and forget all but the newest KEEP."""
    with store.write() as db:
        db.execute("DELETE FROM checkcache WHERE tree = ? AND command = ?", (tree, command))
        db.execute("INSERT INTO checkcache (tree, command, passed_at) VALUES (?, ?, ?)", (tree, command, time.time()))
        db.execute(
            "DELETE FROM checkcache WHERE id NOT IN (SELECT id FROM checkcache ORDER BY id DESC LIMIT ?)", (KEEP,)
        )


def count(store: Store) -> int:
    return int(store.row("SELECT COUNT(*) AS n FROM checkcache")["n"])
