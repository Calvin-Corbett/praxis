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

from agent_praxis import live
from agent_praxis.live import held
from agent_praxis.live import try_lock as _try_lock
from agent_praxis.live import unlock as _unlock
from agent_praxis.store import Store

# What this Praxis's `praxis land` understands of the landing line, written into its ticket file. Raise it when a
# waiting lander must understand something new (1: the notes a batch's head writes). A ticket that says nothing is a
# lander from before the number, and is never taken into a batch.
PROTOCOL = 1

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


def clear_dead(store: Store) -> None:
    """Remove what landings whose process has ended left behind: their places in the line, their ticket files, and
    notes written for them that nobody will read. A ticket's file is locked before its place in the line exists, so
    one that is not locked belongs to a process that is gone."""
    for row in store.rows("SELECT id, token FROM queue ORDER BY id"):
        if ticket_alive(store, row["token"]):
            continue
        with store.write() as db:
            db.execute("DELETE FROM queue WHERE id = ?", (row["id"],))
        try:
            (store.dir / "queue" / f"{row['token']}.lock").unlink()
        except OSError:
            pass
        clear_note(store, row["token"])
    for note in (store.dir / "queue").glob("*.note"):
        if not ticket_alive(store, note.stem):
            clear_note(store, note.stem)


def stopped(store: Store, agent: str) -> bool:
    """True when *agent* has a place in the landing line whose `praxis land` has ended, and no running one."""
    tokens = [row["token"] for row in store.rows("SELECT token FROM queue WHERE agent = ?", (agent,))]
    return bool(tokens) and not any(ticket_alive(store, token) for token in tokens)


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


def ticket_protocol(store: Store, token: str) -> int:
    """The landing-line protocol the lander holding this ticket understands; 0 for a lander that does not say."""
    facts = live.read_stamp(store.dir / "queue" / f"{token}.lock") or {}
    return facts["protocol"] if isinstance(facts.get("protocol"), int) else 0


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
        self.told_batch: list[str] | None = None  # the batch this agent was last told it is in

    def _file(self, token: str) -> Path:
        return self.dir / f"{token}.lock"

    def __enter__(self) -> Ticket:  # noqa: PYI034 - typing.Self needs Python 3.11
        self.dir.mkdir(parents=True, exist_ok=True)
        # Lock the file BEFORE the row exists, so nobody can ever see this ticket while it looks dead.
        self.handle = open(self._file(self.token), "a+b")
        self.store.stamp(self.handle, protocol=PROTOCOL)
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

    def tell_batch(self, note: dict | None) -> bool:
        """Say, once for each batch, that this agent's work was taken into it. Also when the first thing this agent
        sees of the batch is its result: a quick check can be over between two looks at the line."""
        members = note.get("members") if note else None
        if not members or members == self.told_batch:
            return False
        self.told_batch = members
        self.say("Your work was taken into a batch: " + ", ".join(members) + " are checked together, once, "
                 "and each lands in line order. Praxis tells you how it went for you.")
        return True

    def _wait(self) -> None:
        told = None
        last_batch_word = time.monotonic()
        while True:
            note = read_note(self.store, self.token)
            if note and note.get("done"):  # the agent at the head of the line took this one into its batch and is done
                self.tell_batch(note)
                self.verdict = note
                return
            if self.tell_batch(note):
                last_batch_word = time.monotonic()
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
