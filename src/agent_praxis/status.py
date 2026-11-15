"""`praxis status`: one plain page saying who is doing what, and what (if anything) needs the human."""

from __future__ import annotations

import time

from agent_praxis.agents import Ctx, agent_here, folder_state, look_at_agents, stale_agents
from agent_praxis.landing_queue import ticket_alive


def ago(stamp: float, now: float | None = None) -> str:
    seconds = max(0, (now or time.time()) - stamp)
    if seconds < 90:
        return "just now"
    if seconds < 3600 * 1.5:
        return f"{seconds / 60:.0f} minutes ago"
    if seconds < 86400 * 2:
        return f"{seconds / 3600:.0f} hours ago"
    return f"{seconds / 86400:.0f} days ago"


def when(stamp: float) -> str:
    return time.strftime("%b %d %H:%M", time.localtime(stamp))


def _places(line: list, alive: dict) -> dict[str, str]:
    """{agent: where it is in the landing line} for every agent with a running landing."""
    places, ahead = {}, 0
    for q in line:
        if not alive[q["id"]]:
            continue
        if q["started"]:
            places[q["agent"]] = "landing now"
        else:
            places[q["agent"]] = f"waiting in the landing line, place {ahead + 1} ({ahead} ahead)"
        ahead += 1
    return places


def render(ctx: Ctx) -> str:
    now = time.time()
    store, cfg = ctx.store, ctx.cfg
    agents = store.agents()
    claims = store.claims()
    out: list[str] = [f"Praxis status for {ctx.repo.name}  (main version: branch {cfg.main_branch})"]
    here = agent_here(ctx)
    if here:
        out.append(f"You are in {here}'s folder. The main project folder is {ctx.repo.root}")
    out.append("")

    looks = look_at_agents(ctx, agents)  # every agent's folder read once, in a few threads
    paused = [a for a in agents if a["paused"]]
    stale = stale_agents(ctx, now, {n: look.activity for n, look in looks.items() if look.error is None})
    if paused or stale:
        out.append("NEEDS YOU")
        for a in paused:
            out.append(f"  - {a['name']} is paused and waiting for you (since {ago(a['paused_at'], now)}).")
            out.append(f"      Why: {a['paused']}")
            out.append(f"      Look at its work in {a['worktree']}, decide what it should do,")
            out.append(f"      tell it, then run: praxis resume {a['name']}")
        for a, quiet in stale:
            out.append(f"  - {a['name']} has shown no sign of work for {quiet:.0f} hours but still holds files.")
            out.append(f"      If it has stopped for good, free its files with: praxis release-stale {a['name']}")
            out.append("      (its folder and unfinished work are kept)")
        out.append("")
    else:
        out.append("Nothing needs you right now.")
        out.append("")

    line = store.rows("SELECT * FROM queue ORDER BY id")
    alive = {q["id"]: ticket_alive(store, q["token"]) for q in line}
    places = _places(line, alive)
    out.append(f"WORKING NOW ({len(agents)} agent{'s' if len(agents) != 1 else ''})" if agents else "WORKING NOW: nobody")
    for a in agents:
        mine = [c for c in claims if c["agent"] == a["name"]]
        state = "paused" if a["paused"] else "working"
        out.append(f"  - {a['name']} ({state}): \"{a['task']}\"")
        out.append(f"      folder: {a['worktree']}")
        files = [c["path"] for c in mine if not c["is_asset"]]
        assets = [c["path"] for c in mine if c["is_asset"]]
        out.append("      holds: " + (", ".join(files) if files else "(no files)"))
        if assets:
            out.append("      asset locks: " + ", ".join(assets))
        if a["released"]:
            out.append(f"      note: {a['released']}")
        if a["name"] in places:
            out.append(f"      landing: {places[a['name']]}")
        try:
            look = looks[a["name"]]
            where = folder_state(ctx, a, look.is_worktree)
            if where == "missing":
                out.append(f"      PROBLEM: its folder is gone. To keep what it saved: praxis abandon {a['name']}")
            elif where == "partial":
                out.append("      PROBLEM: its folder was only partly removed (Praxis's removal stopped part-way, "
                           f"a program had a file open). Close it, look through what is left of {a['worktree']}, delete it yourself once you are "
                           f"sure nothing in it is needed, then: praxis done {a['name']}")
            elif where == "foreign":
                out.append("      PROBLEM: its folder is no longer a git folder of this project. Move it somewhere "
                           f"safe (it may still hold unsaved work), then: praxis abandon {a['name']}")
            if look.error is not None:
                raise look.error
            ahead, dirty = look.ahead, look.dirty
            work = []
            if ahead:
                work.append(f"{ahead} saved change(s) not landed yet")
            if where != "ok":  # Praxis does not look inside a folder git does not see as this agent's
                work.append("unknown what is in its folder" if where != "missing" else "its folder is gone")
            if dirty:
                work.append(f"{len(dirty)} file(s) being edited")
            landed = store.row("SELECT 1 FROM landings WHERE agent = ? AND ok = 1 AND at >= ?", (a["name"], a["started"]))
            idle = "nothing new since its last landing" if landed else "no changes yet"
            out.append("      work: " + ("; ".join(work) if work else idle))
            out.append(f"      last sign of work: {ago(look.activity, now)}")
        except Exception:  # noqa: BLE001 - a damaged folder must not hide the rest of the page
            out.append("      work: (could not read its folder)")
    out.append("")

    locks = [c for c in claims if c["is_asset"]]
    out.append("LOCKED ASSET FILES (only the holder may change these)" if locks else "LOCKED ASSET FILES: none")
    for c in locks:
        out.append(f"  - {c['path']}  held by {c['agent']} since {ago(c['since'], now)}")
    out.append("")

    out.append("LANDING LINE" if line else "LANDING LINE: empty")
    for n, q in enumerate(line, 1):
        if not alive[q["id"]]:
            out.append(f"  {n}. {q['agent']}: its landing stopped part-way (its process ended), so it may not have "
                       f"finished. Whether its work landed shows under RECENT LANDINGS below and on the branch "
                       f"{cfg.main_branch}. The next landing clears this line.")
        elif q["started"]:
            out.append(f"  {n}. {q['agent']} is landing now (started {ago(q['started'], now)})")
        else:
            out.append(f"  {n}. {q['agent']} is waiting (joined {ago(q['enqueued'], now)})")
    out.append("")

    recent = store.landings(limit=8)
    out.append("RECENT LANDINGS" if recent else "RECENT LANDINGS: none yet")
    for r in recent:
        if r["ok"]:
            out.append(f"  - {when(r['at'])}  {r['agent']} landed \"{r['task']}\" ({r['summary']})")
        else:
            out.append(f"  - {when(r['at'])}  {r['agent']} could NOT land \"{r['task']}\": {r['summary']}")
    if not cfg.check.strip():
        out.append("")
        out.append("Note: no check command is set in praxis.toml, so landings are not tested.")
    return "\n".join(out).rstrip() + "\n"
