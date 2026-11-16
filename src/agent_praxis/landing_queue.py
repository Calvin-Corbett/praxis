"""The landing line: first come, first served, one landing at a time, across any number of processes.

Each agent waiting to land holds a ticket: a numbered row in the database plus a small file that its process
keeps locked with the operating system's file lock. The lowest-numbered ticket whose owner is still running
lands; everyone else waits. If a process dies (closed terminal, crash, power cut), the operating system frees
its file lock, and the next process in line sees that and removes the dead ticket. Nobody's wait ever times out.
"""

from __future__ import annotations

import json
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
NOTE_HEARTBEAT_SECONDS = 60  # a waiting batch member says it is still waiting this often


def note_file(store: Store, token: str) -> Path:
    """The small file where the agent at the head of the line tells the agent holding *token* that they land together
    (and, at the end, how it went). Only the head writes it; only its owner and Praxis's recovery read it."""
    return store.dir / "queue" / f"{token}.note"


def read_note(store: Store, token: str) -> dict | None:
    try:
        data = json.loads(note_file(store, token).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def write_note(store: Store, token: str, data: dict) -> None:
    """Replace the note in one step, so a reader never sees half of it."""
    path = note_file(store, token)
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix(f".tmp{os.getpid()}")
    temp.write_text(json.dumps(data), encoding="utf-8")
    for attempt in range(50):  # a reader may have the old file open for an instant (Windows refuses the swap then)
        try:
            os.replace(temp, path)
            return
        except OSError:
            if attempt == 49:
                raise
            time.sleep(0.05)


def clear_note(store: Store, token: str) -> None:
    try:
        note_file(store, token).unlink()
    except OSError:
        pass


def batch_of(store: Store, agent: str) -> list[str]:
    """The agents landing together with *agent* right now (including itself), or [] when it is not in a batch."""
    try:  # the head of a batch keeps a marker file
        marker = json.loads((store.dir / "batches" / f"{agent}.json").read_text(encoding="utf-8"))
        if ticket_alive(store, marker["token"]):
            return [m["agent"] for m in marker["members"]]
    except (OSError, ValueError, KeyError, TypeError):
        pass
    for row in store.rows("SELECT token FROM queue WHERE agent = ? ORDER BY id", (agent,)):
        note = read_note(store, row["token"])
        if note and not note.get("done") and note.get("members") and ticket_alive(store, row["token"]):
            return list(note["members"])
    return []


def ticket_alive(store: Store, token: str) -> bool:
    """True while the process that holds this ticket is still running (it keeps the ticket's file locked)."""
    return held(store.dir / "queue" / f"{token}.lock")


def held(path: Path) -> bool:
    """True while some running process keeps the file at *path* locked."""
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
        self.verdict: dict | None = None  # set when the agent ahead landed (or refused) this agent's work for it

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
        told = told_batch = None
        last_batch_word = time.monotonic()
        while True:
            note = read_note(self.store, self.token)
            if note and note.get("done"):  # the agent at the head of the line took this one into its batch and is done
                self.verdict = note
                return
            if note and note.get("members") and told_batch != note["members"]:
                told_batch = note["members"]
                last_batch_word = time.monotonic()
                self.say("Your work was taken into a batch: " + ", ".join(told_batch) + " are checked together, once, "
                         "and each lands in line order. Praxis tells you how it went for you.")
            elif note and note.get("members") and time.monotonic() - last_batch_word >= NOTE_HEARTBEAT_SECONDS:
                last_batch_word = time.monotonic()
                self.say("Still waiting for the batch's check (" + ", ".join(note["members"]) + ").")
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
                clear_note(self.store, row["token"])
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
            clear_note(self.store, self.token)
