"""The landing line: first come, first served, one landing at a time, across any number of processes.

Each agent waiting to land holds a ticket: a numbered row in the database plus a small file that its process
keeps locked with the operating system's file lock. The lowest-numbered ticket whose owner is still running
lands; everyone else waits. If a process dies (closed terminal, crash, power cut), the operating system frees
its file lock, and the next process in line sees that and removes the dead ticket. Nobody's wait ever times out.
"""

from __future__ import annotations

import os
import time
import uuid
from pathlib import Path

from agent_praxis.store import Store

if os.name == "nt":
    import msvcrt

    def _try_lock(handle) -> bool:
        handle.seek(0)
        try:
            msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            return True
        except OSError:
            return False

    def _unlock(handle) -> None:
        handle.seek(0)
        try:
            msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
        except OSError:
            pass
else:
    import fcntl

    def _try_lock(handle) -> bool:
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            return True
        except OSError:
            return False

    def _unlock(handle) -> None:
        fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


POLL_SECONDS = 0.3


def ticket_alive(store: Store, token: str) -> bool:
    """True while the process that holds this ticket is still running (it keeps the ticket's file locked)."""
    path = store.dir / "queue" / f"{token}.lock"
    if not path.exists():
        return False
    try:
        with open(path, "a+b") as handle:
            if _try_lock(handle):
                _unlock(handle)
                return False
            return True
    except OSError:
        return True  # cannot even open it: someone is using it


def landing_now(store: Store, agent: str) -> bool:
    """True when a running `praxis land` holds a place in the landing line for *agent*."""
    return any(ticket_alive(store, row["token"])
               for row in store.rows("SELECT token FROM queue WHERE agent = ?", (agent,)))


class Ticket:
    """`with Ticket(store, agent, say):` returns once it is this agent's turn to land."""

    def __init__(self, store: Store, agent: str, say=print, poll: float = POLL_SECONDS):
        self.store, self.agent, self.say, self.poll = store, agent, say, poll
        self.dir = store.dir / "queue"
        self.token = uuid.uuid4().hex
        self.handle = None
        self.id: int | None = None

    def _file(self, token: str) -> Path:
        return self.dir / f"{token}.lock"

    def __enter__(self) -> Ticket:  # noqa: PYI034 - typing.Self needs Python 3.11
        self.dir.mkdir(parents=True, exist_ok=True)
        # Lock the file BEFORE the row exists, so nobody can ever see this ticket while it looks dead.
        self.handle = open(self._file(self.token), "a+b")
        if not _try_lock(self.handle):
            raise RuntimeError("could not lock a brand-new queue file")
        with self.store.write() as db:
            self.id = db.execute("INSERT INTO queue (agent, token, enqueued) VALUES (?, ?, ?)",
                                 (self.agent, self.token, time.time())).lastrowid
        try:
            self._wait()
        except BaseException:
            self.__exit__(None, None, None)
            raise
        return self

    def _alive(self, token: str) -> bool:
        return ticket_alive(self.store, token)

    def _wait(self) -> None:
        told = None
        while True:
            ahead = []
            for row in self.store.rows("SELECT * FROM queue WHERE id < ? ORDER BY id", (self.id,)):
                if self._alive(row["token"]):
                    ahead.append(row["agent"])
                    continue
                with self.store.write() as db:
                    db.execute("DELETE FROM queue WHERE id = ?", (row["id"],))
                try:
                    self._file(row["token"]).unlink()
                except OSError:
                    pass
            if not ahead:
                with self.store.write() as db:
                    db.execute("UPDATE queue SET started = ? WHERE id = ?", (time.time(), self.id))
                return
            if told != len(ahead):
                told = len(ahead)
                self.say(f"Waiting in line to land: {len(ahead)} ahead ({', '.join(ahead)}). "
                         "Praxis waits as long as it takes; this is normal.")
            time.sleep(self.poll)

    def __exit__(self, *exc) -> None:
        if self.id is not None:
            with self.store.write() as db:
                db.execute("DELETE FROM queue WHERE id = ?", (self.id,))
            self.id = None
        if self.handle is not None:
            _unlock(self.handle)
            self.handle.close()
            self.handle = None
            try:
                self._file(self.token).unlink()
            except OSError:
                pass
