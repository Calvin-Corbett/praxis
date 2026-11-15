"""Praxis's memory: one SQLite file in the project's shared git folder, safe for many processes at once.

It lives in `<git folder>/praxis/praxis.db`, so every worktree sees the same claims, locks and queue, and
none of it is ever committed. Writes take SQLite's write lock up front (BEGIN IMMEDIATE), so two agents
claiming the same file at the same moment cannot both win.
"""

from __future__ import annotations

import sqlite3
import time
from contextlib import contextmanager
from pathlib import Path

from agent_praxis.errors import PraxisError

# The database's format number (kept in SQLite's user_version). Raise it whenever the tables change, and add the
# step that brings an older database up to it to UPGRADES. A database from before format numbers reads as 0.
FORMAT = 2

SCHEMA = """
CREATE TABLE IF NOT EXISTS agents (
    name TEXT PRIMARY KEY,
    task TEXT NOT NULL,
    branch TEXT NOT NULL,
    worktree TEXT NOT NULL,
    started REAL NOT NULL,
    last_seen REAL NOT NULL,
    paused TEXT,
    paused_at REAL,
    released TEXT
);
CREATE TABLE IF NOT EXISTS claims (
    agent TEXT NOT NULL,
    path TEXT NOT NULL,
    key TEXT NOT NULL,
    is_asset INTEGER NOT NULL,
    since REAL NOT NULL,
    PRIMARY KEY (agent, key)
);
CREATE TABLE IF NOT EXISTS landings (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    agent TEXT NOT NULL,
    task TEXT,
    at REAL NOT NULL,
    ok INTEGER NOT NULL,
    kind TEXT NOT NULL,
    summary TEXT NOT NULL,
    problems INTEGER,
    signature TEXT,
    log TEXT,
    commit_id TEXT
);
CREATE TABLE IF NOT EXISTS queue (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    agent TEXT NOT NULL,
    token TEXT NOT NULL,
    enqueued REAL NOT NULL,
    started REAL
);
CREATE TABLE IF NOT EXISTS removing (
    agent TEXT PRIMARY KEY,
    folder TEXT NOT NULL,
    since REAL NOT NULL
);
"""

# Each step, by the format it brings a database up to. Every step must be safe on a database that already has it.
UPGRADES = {
    2: """
CREATE TABLE IF NOT EXISTS branches (
    branch TEXT PRIMARY KEY,
    agent TEXT NOT NULL,
    since REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS background (
    agent TEXT PRIMARY KEY,
    log TEXT NOT NULL,
    pid INTEGER,
    started REAL NOT NULL,
    ended REAL,
    exit_code INTEGER
);
""",
}


def _statements(script: str) -> list[str]:
    return [part.strip() for part in script.split(";") if part.strip()]


class Store:
    def __init__(self, state_dir: Path):
        self.dir = state_dir
        self.dir.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(str(self.dir / "praxis.db"), timeout=60, isolation_level=None)
        self.conn.row_factory = sqlite3.Row
        self._refuse_newer(self._format())
        if self._format() < FORMAT:
            self._upgrade()

    def _format(self) -> int:
        return int(self._retry(lambda: self.conn.execute("PRAGMA user_version").fetchone()[0]))

    @staticmethod
    def _refuse_newer(found: int) -> None:
        if found > FORMAT:
            raise PraxisError(
                f"This project was used by a newer Praxis (format {found}); this Praxis knows format {FORMAT} and "
                "older. Update Praxis (or use the newer one) for this project. Nothing was changed."
            )

    def _upgrade(self) -> None:
        """Bring a new or older database up to FORMAT, all in one transaction: another process upgrading at the same
        moment waits, then finds nothing left to do."""
        with self.write() as db:
            found = int(db.execute("PRAGMA user_version").fetchone()[0])
            self._refuse_newer(found)
            if found >= FORMAT:
                return
            steps = [SCHEMA] + [UPGRADES[n] for n in range(max(found, 1) + 1, FORMAT + 1)]
            for script in steps:
                for statement in _statements(script):
                    db.execute(statement)
            db.execute(f"PRAGMA user_version = {FORMAT}")

    def close(self) -> None:
        self.conn.close()

    @staticmethod
    def _retry(fn):
        # Another process holding the database for longer than the timeout is busy, not broken: keep waiting.
        while True:
            try:
                return fn()
            except sqlite3.OperationalError as exc:
                if "locked" not in str(exc) and "busy" not in str(exc):
                    raise
                time.sleep(0.2)

    @contextmanager
    def write(self):
        self._retry(lambda: self.conn.execute("BEGIN IMMEDIATE"))
        try:
            yield self.conn
        except BaseException:
            self.conn.execute("ROLLBACK")
            raise
        self._retry(lambda: self.conn.execute("COMMIT"))

    def rows(self, sql: str, args: tuple = ()) -> list[sqlite3.Row]:
        return self._retry(lambda: self.conn.execute(sql, args).fetchall())

    def row(self, sql: str, args: tuple = ()) -> sqlite3.Row | None:
        found = self.rows(sql, args)
        return found[0] if found else None

    # Common questions -----------------------------------------------------------------------------------------

    def agent(self, name: str) -> sqlite3.Row | None:
        return self.row("SELECT * FROM agents WHERE name = ?", (name,))

    def agents(self) -> list[sqlite3.Row]:
        return self.rows("SELECT * FROM agents ORDER BY started")

    def claims(self, agent: str | None = None) -> list[sqlite3.Row]:
        if agent is None:
            return self.rows("SELECT * FROM claims ORDER BY agent, path")
        return self.rows("SELECT * FROM claims WHERE agent = ? ORDER BY path", (agent,))

    def touch(self, name: str) -> None:
        with self.write() as db:
            db.execute("UPDATE agents SET last_seen = ? WHERE name = ?", (time.time(), name))

    def landings(self, agent: str | None = None, limit: int = 10) -> list[sqlite3.Row]:
        """Newest first."""
        if agent is None:
            return self.rows("SELECT * FROM landings ORDER BY id DESC LIMIT ?", (limit,))
        return self.rows("SELECT * FROM landings WHERE agent = ? ORDER BY id DESC LIMIT ?", (agent, limit))

    # Folders Praxis itself has started to remove ------------------------------------------------------------

    def mark_removing(self, agent: str, folder: str) -> None:
        """Written before Praxis starts removing an agent's folder, so a removal cut short can be told apart from a
        folder whose .git link went missing some other way (which may still hold unsaved work)."""
        with self.write() as db:
            db.execute("INSERT OR REPLACE INTO removing (agent, folder, since) VALUES (?, ?, ?)",
                       (agent, folder, time.time()))

    def clear_removing(self, agent: str) -> None:
        with self.write() as db:
            db.execute("DELETE FROM removing WHERE agent = ?", (agent,))

    def removing(self, agent: str) -> sqlite3.Row | None:
        return self.row("SELECT * FROM removing WHERE agent = ?", (agent,))
