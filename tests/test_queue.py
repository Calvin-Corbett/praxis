from __future__ import annotations

import sys
import time

from conftest import edit, folder, git, praxis, praxis_process
from test_batch import read_until, wait_for

from agent_praxis.store import Store

# A check that records when it started and ended, so overlapping landings would show. It holds the landing line
# until the gate file is there (a minute at most), so the test decides how long it takes, not the machine's speed.
SLOW_CHECK = """\
import os, sys, time
log, gate = sys.argv[1], sys.argv[2]
with open(log, "a") as f:
    f.write(f"start {os.environ['PRAXIS_AGENT']} {time.time()}\\n")
for _ in range(600):
    if os.path.exists(gate):
        break
    time.sleep(0.1)
with open(log, "a") as f:
    f.write(f"end {os.environ['PRAXIS_AGENT']} {time.time()}\\n")
"""


def test_two_processes_land_one_at_a_time(project, tmp_path):
    log, gate = tmp_path / "check-times.log", tmp_path / "gate"
    script = tmp_path / "slow_check.py"
    script.write_text(SLOW_CHECK)
    toml = project / "praxis.toml"
    text = toml.read_text().splitlines()
    check = f"check = '\"{sys.executable}\" \"{script}\" \"{log}\" \"{gate}\"'"
    text = [check if line.startswith("check =") else line for line in text]
    text = ["batch_max = 1" if line.startswith("batch_max =") else line for line in text]  # one at a time is the point
    toml.write_text("\n".join(text) + "\n")

    praxis("start", "alice", "jump", "scripts/player.gd")
    praxis("start", "bob", "enemies", "scripts/enemy.gd")
    edit(folder(project, "alice") / "scripts" / "player.gd", "extends Node\nvar speed = 11\n")
    edit(folder(project, "bob") / "scripts" / "enemy.gd", "extends Node\nvar hp = 5\n")

    alice = praxis_process(project, "land", "alice")
    wait_for(lambda: log.exists() and "start" in log.read_text(), "alice's check to start")
    bob = praxis_process(project, "land", "bob")
    waited = read_until(bob, "Waiting in line")  # alice's check holds the line until the gate opens: bob must wait
    gate.write_text("go")
    outputs = [alice.communicate(timeout=120)[0], waited + bob.stdout.read()]
    bob.wait(timeout=120)
    assert [alice.returncode, bob.returncode] == [0, 0], outputs

    events = [line.split() for line in log.read_text().splitlines()]
    assert [e[0] for e in events] == ["start", "end", "start", "end"], events
    assert events[0][1] == events[1][1] and events[2][1] == events[3][1], "one landing finished before the next began"
    assert float(events[2][2]) >= float(events[1][2])

    assert "speed = 11" in (project / "scripts" / "player.gd").read_text()
    assert "hp = 5" in (project / "scripts" / "enemy.gd").read_text()
    assert int(git(project, "rev-list", "--count", "main")) == 4  # first version, setup, alice, bob


def test_a_dead_ticket_does_not_block_the_line(project):
    store = Store(project / ".git" / "praxis")
    with store.write() as db:  # a ticket whose process is gone (its lock file does not exist)
        db.execute("INSERT INTO queue (agent, token, enqueued) VALUES ('ghost', 'deadbeef', ?)", (time.time(),))
    store.close()
    praxis("start", "alice", "jump", "scripts/player.gd")
    edit(folder(project, "alice") / "scripts" / "player.gd", "extends Node\nvar speed = 11\n")
    code, out = praxis("land", "alice")
    assert code == 0, out
    store = Store(project / ".git" / "praxis")
    assert store.rows("SELECT * FROM queue") == []
    store.close()


def test_a_live_ticket_holds_the_line_until_it_leaves(project):
    from agent_praxis.landing_queue import Ticket

    store = Store(project / ".git" / "praxis")
    seen = []
    first = Ticket(store, "first", say=seen.append)
    first.__enter__()
    import threading

    entered, leave = threading.Event(), threading.Event()

    def wait_turn():  # SQLite connections belong to one thread, so this one opens its own
        other = Store(project / ".git" / "praxis")
        with Ticket(other, "second", say=seen.append, poll=0.05):
            entered.set()
            leave.wait(10)
        other.close()

    t = threading.Thread(target=wait_turn)
    t.start()
    time.sleep(0.5)
    assert not entered.is_set(), "the second waits while the first is landing"
    first.__exit__(None, None, None)
    assert entered.wait(10)
    leave.set()
    t.join(timeout=10)
    assert any("Waiting in line to land: 1 ahead (first)" in s for s in seen)
    store.close()
