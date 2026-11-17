"""Praxis's memory: one SQLite file in the project's shared git folder, safe for many processes at once.

It lives in `<git folder>/praxis/praxis.db`, so every worktree sees the same claims, locks and queue, and
none of it is ever committed. Writes take SQLite's write lock up front (BEGIN IMMEDIATE), so two agents
claiming the same file at the same moment cannot both win.

Every Store registers its process as at work in the project (live.py) before it reads the database, and a database is
never brought up to a newer format while commands of an older Praxis are still running in the project.
"""

from __future__ import annotations

import os
import re
import sqlite3
import time
from contextlib import contextmanager
from pathlib import Path

from agent_praxis import __version__, live
from agent_praxis.errors import PraxisError

# The database's format number (kept in SQLite's user_version). Raise it whenever the tables change, and add the
# step that brings an older database up to it to UPGRADES. A database from before format numbers reads as 0.
FORMAT = 7

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
    3: """
CREATE TABLE IF NOT EXISTS heavy (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    agent TEXT NOT NULL,
    token TEXT NOT NULL,
    command TEXT NOT NULL,
    enqueued REAL NOT NULL,
    started REAL,
    pid INTEGER
);
""",
    4: """
CREATE TABLE IF NOT EXISTS checkcache (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    tree TEXT NOT NULL,
    command TEXT NOT NULL,
    passed_at REAL NOT NULL,
    UNIQUE (tree, command)
);
""",
    5: """
CREATE TABLE IF NOT EXISTS fullchecks (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    commit_id TEXT NOT NULL,
    command TEXT NOT NULL,
    why TEXT NOT NULL,
    landing_id INTEGER NOT NULL,
    pid INTEGER,
    started REAL NOT NULL,
    ended REAL,
    result TEXT,
    log TEXT,
    good_commit TEXT,
    culprit_agent TEXT,
    culprit_task TEXT,
    culprit_commit TEXT,
    undo TEXT,
    note TEXT,
    steps TEXT
);
""",
    # A heavy job started from inside a landing's check goes ahead of the waiting jobs that were not (heavy.py).
    6: """
ALTER TABLE heavy ADD COLUMN priority INTEGER NOT NULL DEFAULT 0;
""",
    # Named checks (checks.py): a pass remembered for the files the check reads, and whether its memory is trusted.
    7: """
CREATE TABLE IF NOT EXISTS checkpasses (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,
    key TEXT NOT NULL,
    passed_at REAL NOT NULL,
    log TEXT,
    UNIQUE (name, key)
);
CREATE TABLE IF NOT EXISTS checktrust (
    name TEXT PRIMARY KEY,
    reuses INTEGER NOT NULL DEFAULT 0,
    audited INTEGER,
    audit_at REAL,
    audit_result TEXT,
    audit_log TEXT,
    broken_at REAL,
    broken_log TEXT,
    broken_commit TEXT,
    broken_agent TEXT
);
""",
}


def _statements(script: str) -> list[str]:
    return [part.strip() for part in script.split(";") if part.strip()]


def _apply(db, statement: str) -> None:
    """Run one upgrade statement. Adding a column the table already has is not a mistake: the step ran before."""
    try:
        db.execute(statement)
    except sqlite3.OperationalError as exc:
        if "duplicate column name" not in str(exc):
            raise


def added_in(table: str) -> int:
    """The format that brought *table*: 1 for the first tables, else the number of the upgrade step that makes it."""
    for number, script in UPGRADES.items():
        if table in re.findall(r"CREATE TABLE IF NOT EXISTS (\w+)", script):
            return number
    return 1


class Store:
    def __init__(self, state_dir: Path, upgrade_now: bool = False, command: str = "", even_landings: bool = False):
        """*command* is what this process is doing, in words for a person (live.py keeps it while the Store is open).
        *upgrade_now* brings the database up to FORMAT even while heavy jobs of an older Praxis are still running;
        while a landing (or anything else) of an older Praxis runs it refuses, unless *even_landings*."""
        self.dir = state_dir
        self.dir.mkdir(parents=True, exist_ok=True)
        self.format = 0  # the format the database is at, as far as this Store knows
        self.older: list[str] = []  # live commands of an older Praxis this Store found when it wanted to upgrade
        self.held_back = False  # True when the database was left at an older format because of them
        self.conn = None
        # Registered BEFORE the format is read: a newer Praxis that upgrades holds the whole database while it looks
        # at the register, so it either sees this process there or this process sees the newer format and refuses.
        self._registration = live.register(self.dir, FORMAT, __version__, command or "a Praxis command")
        try:
            self.conn = sqlite3.connect(str(self.dir / "praxis.db"), timeout=60, isolation_level=None)
            self.conn.row_factory = sqlite3.Row
            self.format = self._format()
            self._refuse_newer(self.format)
            if self.format < FORMAT:
                self._upgrade(upgrade_now, even_landings)
        except BaseException:
            self.close()
            raise

    def _format(self) -> int:
        return int(self._retry(lambda: self.conn.execute("PRAGMA user_version").fetchone()[0]))

    @staticmethod
    def _refuse_newer(found: int) -> None:
        if found > FORMAT:
            raise PraxisError(
                f"This project was used by a newer Praxis (format {found}); this Praxis knows format {FORMAT} and "
                "older. Update Praxis (or use the newer one) for this project. Nothing was changed."
            )

    def _upgrade(self, now: bool = False, even_landings: bool = False) -> None:
        """Bring a new or older database up to FORMAT, all in one transaction: another process upgrading at the same
        moment waits, then finds nothing left to do.

        Not while commands of an older Praxis are still running in the project (unless *now*): they checked the
        format when they started and would carry on in a database they do not know. Then the database is left as it
        is, and `older` says who they are. The transaction holds the whole database (readers too), so no command can
        read the old format while this one decides.

        Even with *now* it refuses, changing nothing, while a landing of an older Praxis runs (live.no_takeover says
        why), unless *even_landings*.
        """
        with self.write(exclusive=True) as db:
            found = int(db.execute("PRAGMA user_version").fetchone()[0])
            self._refuse_newer(found)
            self.format = found
            if found >= FORMAT:
                return
            in_use = db.execute("SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'agents'").fetchone()
            if in_use:  # a brand-new database has nobody to take it from
                running = live.older(self.dir, db, FORMAT, self._registration[0])
                self.older = [words for _kind, words in running]
                if self.older and not now:
                    self.held_back = True
                    return
                refused = None if even_landings else live.no_takeover(running)
                if refused:
                    raise PraxisError(refused)
            steps = [SCHEMA] + [UPGRADES[n] for n in range(max(found, 1) + 1, FORMAT + 1)]
            for script in steps:
                for statement in _statements(script):
                    _apply(db, statement)
            db.execute(f"PRAGMA user_version = {FORMAT}")
            self.format = FORMAT

    def older_now(self) -> list[str]:
        """The commands of an older Praxis that are running in the project at this moment (live.older_commands),
        whether or not the database was brought up to this Praxis's format under them."""
        if self.held_back:
            return self.older
        return live.older_commands(self.dir, self.conn, FORMAT, self._registration[0])

    def stamp(self, handle, **more) -> None:
        """Write who holds a brand-new lock file (a landing-line ticket, a heavy job) into it, BEFORE it is locked. A
        live one with nothing written in it then belongs to a Praxis from before the register (live.py)."""
        live.stamp(handle, pid=os.getpid(), format=FORMAT, version=__version__, **more)

    def has(self, table: str) -> bool:
        """False only when *table* comes with a format the database was not brought up to (see _upgrade)."""
        return self.format >= added_in(table)

    def need(self, tables=None) -> None:
        """Refuse, changing nothing, when the database was left at an older format and lacks one of *tables* (None:
        the caller may use every table). A command that only uses tables the database has carries on."""
        wanted = FORMAT if tables is None else max([1, *(added_in(table) for table in tables)])
        if self.format < wanted:
            raise PraxisError(live.refusal(self.older))

    def close(self) -> None:
        if self.conn is not None:
            self.conn.close()
            self.conn = None
        if self._registration is not None:
            live.unregister(self._registration)
            self._registration = None

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
    def write(self, exclusive: bool = False):
        self._retry(lambda: self.conn.execute("BEGIN EXCLUSIVE" if exclusive else "BEGIN IMMEDIATE"))
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
