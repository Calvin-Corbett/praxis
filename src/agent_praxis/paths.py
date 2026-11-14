"""Project paths: one spelling for every path, whichever system or slash direction it came from.

Paths are stored relative to the project folder with forward slashes. They are compared without regard to
upper/lower case, because Windows and macOS treat "Levels/One.tscn" and "levels/one.tscn" as the same file.
"." means the whole project.
"""

from __future__ import annotations

import fnmatch
import re
from pathlib import Path, PurePosixPath

from agent_praxis.errors import PraxisError

_DRIVE = re.compile(r"^[A-Za-z]:[\\/]")


def _is_absolute(text: str) -> bool:
    return bool(_DRIVE.match(text)) or text.startswith(("/", "\\\\"))


def normalize(path: str, roots: list[Path]) -> str:
    """Turn what a person or agent typed into a project path, or explain why it is not one."""
    text = path.strip().strip('"').strip("'")
    if not text:
        raise PraxisError("An empty file name was given.")
    if _is_absolute(text):
        target = Path(text).resolve()
        for root in roots:
            try:
                rel = target.relative_to(root.resolve())
            except ValueError:
                continue
            text = rel.as_posix()
            break
        else:
            raise PraxisError(f"{path} is outside your project folder, so Praxis cannot claim it.")
    parts: list[str] = []
    for piece in text.replace("\\", "/").split("/"):
        if piece in ("", "."):
            continue
        if piece == "..":
            if not parts:
                raise PraxisError(f"{path} points outside your project folder.")
            parts.pop()
            continue
        parts.append(piece)
    return "/".join(parts) if parts else "."


def key(path: str) -> str:
    return path.casefold()


def covers(claim: str, path: str) -> bool:
    """True when *claim* (a file or folder) includes *path*."""
    c, p = key(claim), key(path)
    return c == "." or c == p or p.startswith(c + "/")


def overlaps(a: str, b: str) -> bool:
    return covers(a, b) or covers(b, a)


def is_asset(path: str, patterns: list[str]) -> bool:
    """True when *path* is a file type that git cannot merge (it matches one of the asset patterns)."""
    full = key(path)
    name = PurePosixPath(full).name
    for pattern in patterns:
        pat = key(pattern.replace("\\", "/"))
        if fnmatch.fnmatchcase(full, pat) or fnmatch.fnmatchcase(name, pat):
            return True
    return False
