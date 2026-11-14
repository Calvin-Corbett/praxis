"""The "enough" rule: stop an agent that keeps failing the same way, and ask the human.

This is not a clock and not a fixed number of tries. An agent that is getting somewhere (fewer problems
each time) is never stopped. Only when its last few landing tries in a row all failed and none of them got
better than the one before does Praxis pause it and say what it needs.
"""

from __future__ import annotations

import hashlib
import re

_PYTEST = re.compile(r"(\d+) (failed|errors?)\b")
_NODE = re.compile(r"^(?:FAILED|ERROR)\b", re.M)
_ERRORISH = re.compile(r"\b(error|failed|failure|exception)\b", re.I)
_NOISE = re.compile(r"0x[0-9a-f]+|\d+(\.\d+)?|[A-Za-z]:\\[^\s]*|/[^\s]*/", re.I)


def problem_count(text: str) -> int | None:
    """How many problems the check output reports, or None when it gives no number."""
    summary = [line for line in text.splitlines() if _PYTEST.search(line)]
    if summary:  # a test runner's closing line, e.g. "2 failed, 5 passed, 1 error in 3.2s"
        return sum(int(n) for n, _ in _PYTEST.findall(summary[-1]))
    nodes = len(_NODE.findall(text))
    if nodes:
        return nodes
    lines = [line for line in text.splitlines() if _ERRORISH.search(line)]
    return len(lines) or None


def signature(text: str) -> str:
    """A fingerprint of a failure that ignores numbers, timings and folder names, so a repeat looks the same."""
    lines = [line.strip() for line in text.splitlines() if line.strip()][-30:]
    cleaned = "\n".join(_NOISE.sub("#", line.lower()) for line in lines)
    return hashlib.sha1(cleaned.encode("utf-8", "replace")).hexdigest()[:12]


def no_better(previous, current) -> bool:
    """True when *current* failed and is not an improvement on *previous*."""
    if previous["problems"] is not None and current["problems"] is not None:
        return current["problems"] >= previous["problems"]
    return previous["signature"] == current["signature"] and previous["kind"] == current["kind"]


def should_pause(recent: list, limit: int) -> str | None:
    """*recent* is this agent's landing tries, newest first. Returns the plain reason to pause, or None."""
    run = []
    for attempt in recent:
        if attempt["ok"]:
            break
        run.append(attempt)
    if len(run) < limit:
        return None
    tries = list(reversed(run[:limit]))  # oldest first
    if not all(no_better(a, b) for a, b in zip(tries, tries[1:])):
        return None
    last = tries[-1]
    same = len({t["signature"] for t in tries}) == 1
    how = "the same way" if same else "without getting any better"
    return f"Its last {limit} tries to land all failed {how}. The latest: {last['summary']}."
