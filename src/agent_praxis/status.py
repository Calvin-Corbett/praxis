"""`praxis status`: one plain page saying who is doing what, and what (if anything) needs the human."""

from __future__ import annotations

import time
from pathlib import Path

from agent_praxis import generated, identity, landing_batch, live, paths
from agent_praxis.agents import Ctx, agent_here, folder_state, look_at_agents, stale_agents
from agent_praxis.landing_queue import batch_of, stopped, ticket_alive


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
    out += _older_lines(store)

    looks = look_at_agents(ctx, agents)  # every agent's folder read once, in a few threads
    paused = [a for a in agents if a["paused"]]
    stale = stale_agents(ctx, now, {n: look.activity for n, look in looks.items() if look.error is None})
    broken = _full_check_needs_you(ctx, now) + _audit_needs_you(ctx, now)
    broken += _stopped_landings(ctx, agents, looks)
    if paused or stale or broken:
        out.append("NEEDS YOU")
        out += broken
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
    together = {a: batch_of(store, a) for a in places}  # agents landing in a batch: checked once, together
    for a, names in together.items():
        if len(names) > 1:
            places[a] += f"; in a batch with {', '.join(n for n in names if n != a)} (checked together)"
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
            if where == "missing" and look.error is None and not look.ahead:
                # Nothing unlanded: what `praxis done` cut short (or a folder removed by hand) leaves just this.
                out.append(f"      its folder is gone and everything it saved has landed. Finish with: praxis done "
                           f"{a['name']}")
            elif where == "missing":
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
            landed = landing_batch.landed_already(ctx, a, look.tip) if ahead and not look.off else None
            if landed is not None:  # a batch landed copies of its commits after its own `praxis land` had ended
                work.append(f"its saved work landed {when(landed['at'])} (in a batch); `praxis land {a['name']}` "
                            "levels its folder")
            elif ahead:
                work.append(f"{ahead} saved change(s) not landed yet")
            if where != "ok":  # Praxis does not look inside a folder git does not see as this agent's
                work.append("unknown what is in its folder" if where != "missing" else "its folder is gone")
            if dirty:
                work.append(f"{len(dirty)} file(s) being edited")
            landed = store.row("SELECT 1 FROM landings WHERE agent = ? AND ok = 1 AND at >= ?", (a["name"], a["started"]))
            idle = "nothing new since its last landing" if landed else "no changes yet"
            out.append("      work: " + ("; ".join(work) if work else idle))
            if look.running:
                out.append(f"      running something now ({look.running} process{'es' if look.running != 1 else ''}"
                           " working in its folder)")
            out.append(f"      last sign of work: {ago(look.activity, now)}")
        except Exception:  # noqa: BLE001 - a damaged folder must not hide the rest of the page
            out.append("      work: (could not read its folder)")
    out.append("")

    # Agent folders share the project's git settings, so one folder answers for all (keeps status to few git calls).
    sample = next((a["worktree"] for a in agents if Path(a["worktree"]).is_dir()), None)
    for warning in identity.warnings(ctx, {a["name"]: sample for a in agents} if sample else {}):
        out += warning.splitlines() + [""]

    out += _shared_files(cfg, claims)
    out += _generated_lines(ctx)

    locks = [c for c in claims if c["is_asset"]]
    out.append("LOCKED ASSET FILES (only the holder may change these)" if locks else "LOCKED ASSET FILES: none")
    for c in locks:
        out.append(f"  - {c['path']}  held by {c['agent']} since {ago(c['since'], now)}")
    out.append("")

    out += _checks_lines(ctx)
    out += _full_check_lines(ctx, now)
    out += _early_lines(ctx, now)
    out += _heavy_lines(ctx, now)

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
        if len(together.get(q["agent"], [])) > 1 and alive[q["id"]]:
            out[-1] += f" - in a batch with {', '.join(t for t in together[q['agent']] if t != q['agent'])}"
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


def _stopped_landings(ctx, agents, looks) -> list[str]:
    """What a landing that was stopped part-way (a tool's time limit killed `praxis land`) left to put right: a
    folder that is not on its own branch, or a place in the line nobody holds. One command does it."""
    out = []
    for a in agents:
        look = looks[a["name"]]
        if look.landing or look.error is not None:  # a running landing has the folder on no branch on purpose
            continue
        cut = stopped(ctx.store, a["name"]) or landing_batch.stopped(ctx, a["name"])
        if not cut and not look.off:
            continue
        what = "landing was stopped part-way" if cut else "folder is not on its own branch"
        if cut and look.off:
            what += f", and its folder was left on {look.off} (it belongs on {a['branch']})"
        elif look.off:
            what += f" {a['branch']} (it is on {look.off})"
        out.append(f"  - {a['name']}'s {what}. Nothing is lost.")
        out.append(f"      To put it right, run: praxis land {a['name']}")
        out.append("      (it puts the folder back with everything saved in it, lands what has not landed, and says "
                   "so when the work already landed)")
    return out


def _older_lines(store) -> list[str]:
    """The commands of an older Praxis running in the project, and what they can and cannot do: said for as long as
    one of them lives."""
    out = []
    if store.held_back:
        out.append(f"OLDER PRAXIS COMMANDS ARE STILL RUNNING HERE: {live.listed(store.older)}.")
        out.append("  This newer Praxis changes the project's database in a way they cannot use, so it left the "
                   "database as it is and shows what it holds. It takes over when they finish.")
        out.append("  Until then a command that needs the newer database (`praxis land`) says so and changes nothing: "
                   "run it again when they have finished.")
        return [*out, ""]
    older = store.older_now()
    fine = [one.words for one in older if one.format >= store.usable_from]
    lost = [one.words for one in older if one.format < store.usable_from]
    if fine:
        out.append(f"OLDER PRAXIS VERSIONS ARE AT WORK HERE: {live.listed(fine)}.")
        out.append("  They carry on as usual, and so does every new command of those versions: this project's "
                   f"database (format {store.level}) can be used by every Praxis from format {store.usable_from} on.")
        out.append("  They do not use what a newer Praxis added to it, and a landing of theirs is checked on its own, "
                   "never in one batch with a landing of this Praxis. To move the launcher to the newest Praxis: "
                   "praxis update")
        out.append("")
    if lost:
        out.append(f"OLDER PRAXIS COMMANDS ARE STILL RUNNING HERE: {live.listed(lost)}.")
        out.append("  They started before the project's database was changed in a way their Praxis cannot use: "
                   f"only Praxis from format {store.usable_from} on can use it now. A heavy job among them runs to "
                   "its end and ends with its own exit code.")
        out.append("  A landing among them can fail: when its check starts a `praxis` command with that older "
                   "Praxis, the database refuses it. Its work is kept: land it again with this Praxis.")
        out.append("  Any new command of that older Praxis is refused and changes nothing (it says: update Praxis).")
        out.append("")
    return out


def _checks_lines(ctx) -> list[str]:
    from agent_praxis import checkcache

    out = []
    if any("name" in entry for entry in ctx.cfg.checks):
        out += _named_checks_lines(ctx)
    elif ctx.cfg.checks:
        out.append("CHECKS (a landing runs the first of these whose paths match a file it changes; else the default)")
        for n, entry in enumerate(ctx.cfg.checks, 1):
            out.append(f"  {n}. {', '.join(entry['paths'])}: {entry['run']}")
        out.append(f"  default: {ctx.cfg.check or '(none)'}")
    kept = checkcache.count(ctx.store) if ctx.store.has("checkcache") else 0
    if kept:
        out.append(f"{'  ' if out else 'CHECK CACHE: '}{kept} passed check(s) remembered: the same tree is not "
                   "checked twice with the same command")
    return [*out, ""] if out else []


def _named_checks_lines(ctx) -> list[str]:
    """The named checks: what chooses each one, what it reads, and how many of its passes are remembered."""
    from agent_praxis import checks

    known = ctx.store.has("checkpasses")  # False while the database is left at an older Praxis's format (store.py)
    kept = checks.remembered(ctx.store) if known else {}
    out = [("CHECKS (a landing runs every one of these whose paths match a file it changes, or that reads one; the "
            "default too when no paths match)")]
    for entry in ctx.cfg.checks:
        name, reads = entry["name"], entry.get("inputs")
        memory = f"{kept.get(name, 0)} pass(es) remembered"
        if known and not checks.trusted(ctx.store, name):
            memory = "NOT remembered: its audit failed (see NEEDS YOU above)"
        out.append(f"  - {name}: {entry['run']}")
        out.append(f"      paths: {', '.join(entry['paths'])}; reads: "
                   f"{', '.join(reads) if reads else 'the whole project'}; {memory}")
    out.append(f"  default: {ctx.cfg.check or '(none)'}")
    every = ctx.cfg.audit_every
    out.append("  audit: " + (f"the first pass taken from memory and then one in {every} is run again to make sure"
                              if every else "off (audit_every = 0)"))
    return out


def _audit_needs_you(ctx, now: float) -> list[str]:
    """Named checks whose pass from memory failed when it was run again: the `inputs` list needs a person."""
    from agent_praxis import checkaudit

    if not ctx.store.has("checktrust"):  # the database was left at an older Praxis's format (store.py)
        return []
    out = []
    for row in checkaudit.broken(ctx.store):
        lines = checkaudit.describe(row, found=ago(row["broken_at"], now))
        out += [f"  - {lines[0]}", *(f"      {line}" for line in lines[1:])]
    return out


def _full_check_needs_you(ctx, now: float) -> list[str]:
    """The full check's last word on the main version, when it is not "passed": what broke it and how to undo it."""
    from agent_praxis import fullcheck

    if not ctx.store.has("fullchecks"):  # the database was left at an older Praxis's format (store.py)
        return []
    row = fullcheck.failing(ctx)
    if row is None:
        return []
    lines = fullcheck.describe(ctx, row, found=ago(row["ended"] or row["started"], now))
    return [f"  - {lines[0]}", *(f"      {line}" for line in lines[1:])]


def _full_check_lines(ctx, now: float) -> list[str]:
    from agent_praxis import fullcheck

    cmd = fullcheck.command(ctx.cfg)
    if not cmd or not ctx.store.has("fullchecks"):
        return []
    every, store = ctx.cfg.full_check_every, ctx.store
    often = f"after every {every} landing(s)" if every else "only when you ask"
    out = [f"FULL CHECK ({often}; `praxis fullcheck` runs it now): {cmd}"]
    going, last = fullcheck.in_progress(store), fullcheck.newest(store, cmd)
    judged = fullcheck.newest(store, cmd, fullcheck.JUDGED)
    since = fullcheck.landings_since(store, cmd)
    if going is not None:
        out.append(f"  - running now on commit {fullcheck.short(going['commit_id'])} (started "
                   f"{ago(going['started'], now)}: {going['why']}). Its output is in {store.dir / 'logs'}")
    elif last is not None and last["result"] in (None, "interrupted"):
        out.append("  - the last run was interrupted (its process ended before it finished). The next landing starts "
                   "it again, or run: praxis fullcheck")
    if judged is None:
        out.append("  - it has not finished on this project yet")
    elif judged["result"] == "passed":
        out.append(f"  - last passed {ago(judged['ended'] or judged['started'], now)} on commit "
                   f"{fullcheck.short(judged['commit_id'])} ({judged['why']}); {since} landing(s) since")
    else:
        out.append(f"  - FAILING since {ago(judged['ended'] or judged['started'], now)} on commit "
                   f"{fullcheck.short(judged['commit_id'])} (see NEEDS YOU above); {since} landing(s) since")
    return [*out, ""]


def _early_lines(ctx, now: float) -> list[str]:
    from agent_praxis import early

    state = early.active(ctx.store)
    if not state:
        return []
    started = ago(state.get("started", now), now)
    line = (f"EARLY CHECK: {state['agent']}'s check is running early (started {started}), on top of the work that is "
            "landing now. Only a pass is remembered; nothing lands from it.")
    return [line, ""]


def _heavy_lines(ctx, now: float) -> list[str]:
    from agent_praxis import heavy

    if not ctx.store.has("heavy"):  # the database was left at an older Praxis's format (store.py)
        return []
    jobs = heavy.rows(ctx.store)
    if ctx.cfg.heavy_jobs == "auto":
        limit, why = heavy.limit_now(ctx)
        out = [f"HEAVY JOBS (at most {limit} run at once right now; `praxis run --heavy <command>`)", f"  {why}"]
    elif jobs:
        out = [f"HEAVY JOBS (at most {ctx.cfg.heavy_jobs} run at once; `praxis run --heavy <command>`)"]
    else:
        return []
    rule = heavy.rules(ctx.store, jobs)  # jobs of an older Praxis start by that Praxis's rule
    running, waiting = heavy.in_order(jobs, rule)  # the waiting jobs in the order they really start
    kept = heavy.fixed(waiting, rule)
    older = [j for j in waiting if rule.get(j["id"]) == heavy.JOINED]
    for j in sorted(running, key=lambda j: not heavy.is_check(j)):
        who = f"{j['agent']} (landing check)" if heavy.is_check(j) else j["agent"]
        out.append(f"  - running: {who}, started {ago(j['started'], now)}: {j['command']}")
    for j in waiting:
        who = j["agent"]
        if j["id"] in kept:
            who += " (an older Praxis: keeps the place it joined at)"
        elif rule.get(j["id"]) == heavy.JOINED:
            who += " (an older Praxis: starts when every job that joined before it has)"
        elif heavy.is_check(j):
            who += " (landing check, goes first)" if not older else " (landing check)"
        out.append(f"  - waiting: {who}, joined {ago(j['enqueued'], now)}: {j['command']}")
    if len(waiting) > 1:
        out.append("  (waiting jobs start from the top: a landing check's first, then an agent with nothing running "
                   "before one that has a job running)")
    if kept:
        out.append("  (a job of an older Praxis starts strictly in the order the jobs joined, so the jobs of this "
                   "Praxis are put in order around it: before it those that joined before it, after it the rest)")
    elif older:
        out.append("  (jobs of three Praxis versions are waiting, and each version starts its jobs by its own rule: "
                   "a job of the oldest one may start earlier than its place in this list)")
    return [*out, ""]


def _shared_files(cfg, claims) -> list[str]:
    """Code files (or folders) that more than one agent is working on, each with everyone working on it."""
    if cfg.exclusive_code:
        return []
    code = [c for c in claims if not c["is_asset"]]
    seen, lines = set(), []
    for c in code:
        if paths.key(c["path"]) in seen:
            continue
        seen.add(paths.key(c["path"]))
        names = []
        for other in code:
            if paths.overlaps(c["path"], other["path"]) and other["agent"] not in names:
                names.append(other["agent"])
        if len(names) > 1:
            lines.append(f"  - {c['path']}: {', '.join(names)}")
    if not lines:
        return []
    return ["SHARED CODE FILES (several agents edit these at once; a real clash is caught when each one lands)",
            *lines, ""]


def _generated_lines(ctx) -> list[str]:
    stale = generated.out_of_date(ctx.store)
    if not ctx.cfg.generated:
        return []
    out = ["GENERATED FILES (Praxis rebuilds these after landings; nobody edits them)"]
    for item in ctx.cfg.generated:
        info = stale.get(item["path"])
        if info is None:
            out.append(f"  - {item['path']}: up to date (made by `{item['command']}`)")
        else:
            out.append(f"  - {item['path']}: OUT OF DATE since {ago(info.get('at', 0))}: {info.get('why', '')}")
            if info.get("log"):
                out.append(f"      Output of the last try: {info['log']}")
            out.append("      The next landing tries again.")
    return [*out, ""]
