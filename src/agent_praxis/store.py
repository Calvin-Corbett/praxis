"""Praxis's memory: one SQLite file in the project's shared git folder, safe for many processes at once.

It lives in `<git folder>/praxis/praxis.db`, so every worktree sees the same claims, locks and queue, and
none of it is ever committed. Writes take SQLite's write lock up front (BEGIN IMMEDIATE), so two agents
claiming the same file at the same moment cannot both win.

Every Store registers its process as at work in the project (live.py) before it reads the database.

Several Praxis versions can work in one project at the same time, because a database says two things about itself
(see FORMAT below): what it holds, and the oldest Praxis that can still use it. A step that only adds to the database
is taken at once and leaves every older Praxis working. Only a step an older Praxis cannot live with waits until no
command of such a Praxis is running in the project any more.
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

# Two numbers describe a project's database.
#
# Its FORMAT says which of the steps below it has had, so which tables and columns it holds. It is kept in the `meta`
# table, where no Praxis from before that table looks.
#
# Its "usable from" number is the oldest format whose Praxis can still use it safely. It is kept in SQLite's
# user_version. Every Praxis released before the `meta` table (format 6 and older) knows only that number: it takes
# it for the database's format and refuses a database where it is higher than the format it knows. That can never be
# changed in them. So user_version is raised only by a BREAKING step and stays where it is through every ADDITIVE
# one, and such an older Praxis, whether it is running already or started afterwards, keeps working in a database
# that a newer Praxis has added to. A Praxis that knows `meta` goes by the same rule: it works in every database
# whose "usable from" is not above its own FORMAT, using only what it knows.
#
# To change the tables: raise FORMAT, add the step to UPGRADES, and say in KIND which kind it is.
FORMAT = 8
USABLE_FROM = 6  # what a brand-new database starts at: the newest format a released Praxis from before `meta` knows
META = 8  # the step that brought the `meta` table: a database below it keeps its format in user_version alone

# ADDITIVE: the step only adds a table, or a column that has a default, so the code of every older format (from the
# database's "usable from" on) keeps working without knowing about it. It is taken at once, whoever is running.
# BREAKING: anything else. An older Praxis cannot use the database afterwards, so the step waits until no command of
# such a Praxis is running in the project (_upgrade), and it raises "usable from" to its own number.
ADDITIVE, BREAKING = "additive", "breaking"
KIND = {
    2: BREAKING,  # 2 to 4 are from before the two numbers. Nobody has run a Praxis that old beside a newer one,
    3: BREAKING,  # so they are treated as they always were: taken only when no older command is running.
    4: BREAKING,
    5: ADDITIVE,  # a new table (fullchecks)
    6: ADDITIVE,  # a new column with a default (heavy.priority)
    7: ADDITIVE,  # two new tables (checkpasses, checktrust)
    8: ADDITIVE,  # a new table (meta)
}

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
    # Where a database says what it holds (see FORMAT): one row, key 'format'.
    8: """
CREATE TABLE IF NOT EXISTS meta (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
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


def usable_from(user_version: int) -> int:
    """The "usable from" number a database's user_version stands for. Any number up to USABLE_FROM is one (a Praxis
    from before `meta` wrote its format there), and so is the number of a breaking step. Anything else was written by
    a Praxis from before the two numbers for a step that is additive (7 was: it never left Praxis's own
    development), and stands for the newest real one below it."""
    real = [n for n in range(1, user_version + 1) if n <= USABLE_FROM or KIND.get(n) == BREAKING]
    return max(real, default=0)


def formats(db) -> tuple[int, int]:
    """(the format of the database behind the connection *db*, its user_version)."""
    version = int(db.execute("PRAGMA user_version").fetchone()[0])
    try:
        row = db.execute("SELECT value FROM meta WHERE key = 'format'").fetchone()
    except sqlite3.OperationalError as exc:
        if "no such table" not in str(exc):
            raise
        row = None  # from before `meta`: its user_version is its format
    try:
        return max(version, int(row[0])) if row else version, version
    except (TypeError, ValueError):
        return version, version


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
        self.format = 0  # the newest format this Store may use: the database's own, or FORMAT when that is newer
        self.level = 0  # the database's own format, which a newer Praxis may have taken past FORMAT
        self.usable_from = 0  # the oldest format whose Praxis can use the database
        self.older: list[str] = []  # live commands of an older Praxis that a breaking step met when it was due
        self.held_back = False  # True when the database was left at an older format because of them
        self.conn = None
        # Registered BEFORE the format is read: a newer Praxis that upgrades holds the whole database while it looks
        # at the register, so it either sees this process there or this process sees the newer format and refuses.
        self._registration = live.register(self.dir, FORMAT, __version__, command or "a Praxis command")
        try:
            self.conn = sqlite3.connect(str(self.dir / "praxis.db"), timeout=60, isolation_level=None)
            self.conn.row_factory = sqlite3.Row
            self.level, self.usable_from = self._retry(lambda: formats(self.conn))
            self._refuse_newer(self.usable_from)
            self.format = min(self.level, FORMAT)
            if self.level < FORMAT or self.usable_from != usable_from(self.usable_from):
                self._upgrade(upgrade_now, even_landings)
        except BaseException:
            self.close()
            raise

    @staticmethod
    def _refuse_newer(usable: int) -> None:
        if usable > FORMAT:
            raise PraxisError(
                f"This project was used by a newer Praxis (format {usable}), which changed the project's database "
                f"in a way an older Praxis cannot use; this Praxis knows format {FORMAT} and older. Update Praxis (or "
                "use the newer one) for this project. Nothing was changed."
            )

    def _upgrade(self, now: bool = False, even_landings: bool = False) -> None:
        """Bring a new or older database up to FORMAT, all in one transaction: another process upgrading at the same
        moment waits, then finds nothing left to do.

        An additive step is taken at once: every Praxis that could use the database before still can, and its
        user_version stays as it is. A breaking step is not taken while commands of a Praxis older than that step are
        still running in the project (unless *now*): they would carry on in a database they cannot use. Then the
        database is left at the step before it, and `older` says who they are. The transaction holds the whole
        database (readers too), so no command can read the old numbers while this one decides.

        Even with *now* a breaking step refuses, changing nothing, while a landing of such a Praxis runs
        (live.no_takeover says why), unless *even_landings*.
        """
        with self.write(exclusive=True) as db:
            level, version = formats(db)
            self._refuse_newer(version)
            usable = usable_from(version)
            new = not db.execute("SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'agents'").fetchone()
            if new:  # a brand-new database has nobody to take it from
                level, usable = 1, USABLE_FROM
            reached = level
            for statement in _statements(SCHEMA):
                _apply(db, statement)
            for step in range(max(level, 1) + 1, FORMAT + 1):
                if KIND[step] == BREAKING:
                    running = [] if new else live.older(self.dir, db, step, self._registration[0])
                    if running:
                        self.older = [found.words for found in running]
                        if not now:
                            self.held_back = True
                            break
                        refused = None if even_landings else live.no_takeover(running)
                        if refused:
                            raise PraxisError(refused)
                    usable = max(usable, step)
                for statement in _statements(UPGRADES[step]):
                    _apply(db, statement)
                reached = step
            if reached >= META:
                db.execute("INSERT OR REPLACE INTO meta (key, value) VALUES ('format', ?)", (str(max(reached, level)),))
            if usable != version:
                db.execute(f"PRAGMA user_version = {usable}")
            self.level, self.usable_from = max(reached, level), usable
            self.format = min(self.level, FORMAT)

    def older_now(self) -> list:
        """The commands of an older Praxis that are running in the project at this moment (see live.older)."""
        return live.older(self.dir, self.conn, FORMAT, self._registration[0])

    def stamp(self, handle, **more) -> None:
        """Write who holds a brand-new lock file (a landing-line ticket, a heavy job) into it, BEFORE it is locked. A
        live one with nothing written in it then belongs to a Praxis from before the register (live.py)."""
        live.stamp(handle, pid=os.getpid(), format=FORMAT, version=__version__, **more)

    def has(self, table: str) -> bool:
        """False only when *table* comes with a step the database has not had: a breaking step that waits for
        commands of an older Praxis to finish, or a step after it (see _upgrade)."""
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
