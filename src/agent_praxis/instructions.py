"""The rules every agent reads, kept in a marked section of AGENTS.md and CLAUDE.md.

Praxis only ever changes the text between its own two marker lines. Everything else in those files is left
exactly as it was. Running `praxis init` again refreshes the section instead of adding a second copy.
"""

from __future__ import annotations

from pathlib import Path

START = "<!-- praxis:start -->"
END = "<!-- praxis:end -->"
FILES = ("AGENTS.md", "CLAUDE.md")

BODY = """## Working alongside other AI agents: use Praxis

Several AI agents work on this project at the same time. Praxis stops them from breaking each
other's work. Follow these rules exactly. (This section was written by `praxis init`; Praxis
only changes the text between the two praxis marker lines.)

1. Use one short name for yourself every time, for example `claude-1` or `codex-2`.
2. You work in your own folder (a git worktree), never in the main project folder.
   Usually the human has already run `praxis start` for you and opened you in that folder:
   then just carry on (`praxis status` shows whose folder you are in, and its files).
   If nobody started you, start yourself from the main project folder:
   `praxis start <your-name> "<what you are doing>" <files or folders you will change>`
   Praxis prints where your folder is. Go there, and work ONLY inside it.
3. Your folder holds only what is committed. Things git ignores (installed packages such
   as node_modules, engine caches such as Unity's Library) are not there: install or
   import them in your own folder (for example `npm install`) before you run the check.
4. To change more files later, claim them first:
   `praxis claim <your-name> <file or folder> ...`
   If Praxis says another agent holds a file, do not edit it. Pick other work, or wait.
5. Asset files (scenes, levels, prefabs, models, textures, sounds) cannot be merged.
   Never edit one you have not claimed. Claim it before you touch it.
6. When your change is finished, run `praxis land <your-name>`.
   Praxis saves (commits) your work, puts it on top of the newest version, runs the
   project check and lands it only if the check passes. It may wait in line for other
   agents first: waiting is normal and never fails. Landing can take many minutes, so run
   it with a long time limit (or in the background) and never stop it part-way.
7. If landing fails, read the failure text Praxis prints, fix the problem in your folder,
   and run `praxis land <your-name>` again. Your work is always kept.
8. When your task is finished and landed, finish from the main project folder (a folder
   cannot be removed while you are in it): `cd` to the main folder Praxis named, then run
   `praxis done <your-name>`.
9. If you are stuck (the same failure keeps coming back, or you need a decision from
   the human), STOP. Run `praxis blocked <your-name> "<what you need>"` and tell the human.
   Do not keep retrying the same thing.
10. Stay on the branch Praxis gave you (`praxis/<your-name>`): do not switch, create or delete
   branches. Never run `git push`, `git reset --hard`, `git clean`, or delete folders Praxis
   made. Praxis handles all of that.

Run `praxis status` at any time to see who is working on what."""


def block(nl: str = "\n") -> str:
    return f"{START}\n{BODY}\n{END}\n".replace("\n", nl)


def write_into(path: Path) -> str:
    """Add or refresh the Praxis section in *path*. Returns 'created', 'added', 'updated' or 'unchanged'.
    An existing file keeps its own line endings (Windows CRLF or LF)."""
    if not path.exists():
        path.write_text(block(), encoding="utf-8", newline="\n")
        return "created"
    with open(path, encoding="utf-8", newline="") as handle:  # newline="": see the file's real line endings
        text = handle.read()
    nl = "\r\n" if "\r\n" in text else "\n"
    begin, end = text.find(START), text.find(END)
    if begin != -1 and end > begin:
        tail = text[end + len(END):]
        if tail.startswith("\r\n"):
            tail = tail[2:]
        elif tail.startswith("\n"):
            tail = tail[1:]
        new = text[:begin] + block(nl) + tail
        if new == text:
            return "unchanged"
        path.write_text(new, encoding="utf-8", newline="")
        return "updated"
    sep = "" if not text or text.endswith(nl + nl) else (nl if text.endswith(nl) else nl + nl)
    path.write_text(text + sep + block(nl), encoding="utf-8", newline="")
    return "added"
