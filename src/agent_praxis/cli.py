"""The `praxis` command."""

from __future__ import annotations

import argparse
import os
import sys
import traceback

from agent_praxis import __version__, agents, config, gitops
from agent_praxis.agents import Ctx
from agent_praxis.errors import PraxisError
from agent_praxis.store import Store

HELP = """Praxis lets several AI agents work on one git project without breaking each other's work.

Everyday commands:
  praxis init                                   set this project up (once)
  praxis start <agent> "<task>" <files...>      give an agent its own folder and claim its files
  praxis claim <agent> <files...>               claim more files or folders (asset files get locked)
  praxis land <agent>                           test the agent's work and put it into the main version
  praxis land <agent> --background              the same, carrying on after this command returns
  praxis wait <agent>                           follow a background landing to its end
  praxis status                                 see who is doing what, and what needs you
  praxis done <agent>                           finish: release its files and remove its folder

When things go wrong:
  praxis blocked <agent> "<what it needs>"      an agent asks the human for help (pauses it)
  praxis resume <agent>                         the human lets a paused agent carry on
  praxis abandon <agent>                        stop an agent; unlanded work is saved on a branch
  praxis release-stale [agent...]               free the files of agents that went quiet
  praxis unclaim <agent> <files...>             let go of files
  praxis setup <agent>                          run praxis.toml's setup command in the agent's folder again

Exit codes: 0 done, 1 refused (the message says why; nothing was changed), 2 not landed or a step failed (the
message says why; work is kept), 3 Praxis hit a problem it did not expect, 130 stopped with Ctrl+C.
"""


def _parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="praxis", description=HELP, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--version", action="version", version=f"praxis {__version__}")
    sub = p.add_subparsers(dest="command", metavar="command")

    s = sub.add_parser("init", help="set this project up for Praxis")
    s.add_argument("--check", help="the command that proves the project works, e.g. \"npm test\"")
    s.add_argument("--no-commit", action="store_true", help="write the setup files but do not commit them")

    s = sub.add_parser("start", help="start an agent on a task")
    s.add_argument("agent")
    s.add_argument("task")
    s.add_argument("files", nargs="*")
    s.add_argument("--reuse-branch", action="store_true",
                   help="carry on from an existing branch with the agent's name that Praxis did not make")

    for name, text in (("claim", "claim more files or folders"), ("unclaim", "let go of files")):
        s = sub.add_parser(name, help=text)
        s.add_argument("agent")
        s.add_argument("files", nargs="+")

    s = sub.add_parser("land", help="test an agent's work and land it")
    s.add_argument("agent")
    s.add_argument("--done", action="store_true", help="also finish the agent after it lands")
    s.add_argument("--background", action="store_true",
                   help="return once the landing has its place in line; follow it with `praxis wait <agent>`")
    s.add_argument("--in-background", action="store_true", help=argparse.SUPPRESS)  # the background process itself

    s = sub.add_parser("wait", help="follow an agent's background landing to its end")
    s.add_argument("agent")

    s = sub.add_parser("setup", help="run praxis.toml's setup command in an agent's folder again")
    s.add_argument("agent")

    sub.add_parser("status", help="show who is doing what")

    for name, text in (("done", "finish an agent whose work has landed"),
                       ("abandon", "stop an agent, saving unlanded work"),
                       ("resume", "let a paused agent carry on")):
        s = sub.add_parser(name, help=text)
        s.add_argument("agent")

    s = sub.add_parser("blocked", help="pause an agent that needs the human")
    s.add_argument("agent")
    s.add_argument("reason", nargs="?", default="")

    s = sub.add_parser("release-stale", help="free the files of agents that went quiet")
    s.add_argument("agents", nargs="*")
    return p


def _ctx() -> tuple[Ctx, Store]:
    repo = gitops.find_repo()
    cfg = config.load(repo.root)
    store = Store(repo.state_dir)
    try:
        for row in store.agents():  # git hooks in an agent's folder can tell which agent is committing
            gitops.agent_folder(row["worktree"], row["name"], row["task"])
    except BaseException:
        store.close()
        raise
    return Ctx(repo=repo, cfg=cfg, store=store, say=_say), store


def _say(text: str = "") -> None:
    print(text, flush=True)


def run(argv: list[str]) -> int:
    args = _parser().parse_args(argv)
    if not args.command:
        print(HELP)
        return 0
    gitops.forget_cached_answers()  # nothing a git call answered outlives one command
    # Each command imports only what it needs, so a quick one (status, claim) starts quickly.
    if args.command == "init":
        from agent_praxis import setup
        setup.init(check=args.check, commit=not args.no_commit, say=_say)
        return 0
    ctx, store = _ctx()
    try:
        cmd = args.command
        if cmd == "start":
            agents.start(ctx, args.agent, args.task, args.files, reuse_branch=args.reuse_branch)
            return _setup(ctx, args.agent)
        elif cmd == "claim":
            agents.claim(ctx, args.agent, args.files)
        elif cmd == "unclaim":
            agents.unclaim(ctx, args.agent, args.files)
        elif cmd == "land":
            return _land(ctx, args)
        elif cmd == "wait":
            from agent_praxis import background
            return background.wait(ctx, args.agent)
        elif cmd == "setup":
            return _setup(ctx, args.agent, again=True)
        elif cmd == "status":
            from agent_praxis import status
            sys.stdout.write(status.render(ctx))
        elif cmd == "done":
            agents.done(ctx, args.agent)
        elif cmd == "abandon":
            agents.abandon(ctx, args.agent)
        elif cmd == "blocked":
            agents.blocked(ctx, args.agent, args.reason)
        elif cmd == "resume":
            agents.resume(ctx, args.agent)
        elif cmd == "release-stale":
            agents.release_stale(ctx, args.agents)
        return 0
    finally:
        store.close()


def _land(ctx: Ctx, args) -> int:
    from agent_praxis import background, landing

    if args.background and not args.in_background:
        return background.start(ctx, args.agent, finish=args.done)
    if args.in_background:
        name = agents.check_name(args.agent)
        return background.run_child(ctx, name, lambda: _guarded(
            lambda: 0 if landing.land(ctx, args.agent, finish=args.done) else 2))
    return 0 if landing.land(ctx, args.agent, finish=args.done) else 2


def _setup(ctx: Ctx, name: str, again: bool = False) -> int:
    """Run praxis.toml's setup command in the agent's folder: 0 when it passed (or there is none), else 2."""
    from agent_praxis import hooks

    row = agents.need_agent(ctx, name)
    if again:
        agents.refuse_other_agents_folder(ctx, row, "set up")
        if agents.folder_state(ctx, row) != "ok":
            raise PraxisError(f"{row['name']}'s folder {row['worktree']} is missing or damaged, so its setup cannot "
                              "run. Run `praxis status` to see what to do.")
        if not ctx.cfg.setup.strip():
            ctx.say("No setup command is set in praxis.toml (setup = \"...\"), so there is nothing to run.")
            return 0
    return 0 if hooks.run_setup(ctx, row) else 2


def main(argv: list[str] | None = None) -> int:
    for stream in (sys.stdout, sys.stderr):
        try:  # UTF-8, so a folder named "My Gäme" prints the same in Git Bash, a pipe or an AI tool as on screen
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            pass
    return _guarded(lambda: run(sys.argv[1:] if argv is None else argv))


def _guarded(fn) -> int:
    """Run *fn* (which returns an exit code), turning any problem into a plain message and Praxis's exit code."""
    try:
        return fn()
    except PraxisError as exc:
        print(f"Praxis: {exc}", file=sys.stderr, flush=True)
        return 1
    except KeyboardInterrupt:
        print("Praxis: stopped. Any half-finished step was undone and no work was lost. "
              "Run `praxis status` to see where things are.", file=sys.stderr)
        return 130
    except Exception as exc:  # noqa: BLE001 - last resort: still say something a person can act on
        print(f"Praxis hit a problem it did not expect: {exc}", file=sys.stderr)
        if os.environ.get("PRAXIS_DEBUG"):
            traceback.print_exc()
        else:
            print("Run the same command again with PRAXIS_DEBUG=1 set to see the details.", file=sys.stderr)
        return 3
