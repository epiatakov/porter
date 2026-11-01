"""Core data types: tasks and the task state machine."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from enum import StrEnum


class Status(StrEnum):
    QUEUED = "queued"
    RUNNING = "running"
    VERIFYING = "verifying"
    PR_OPEN = "pr_open"
    DONE = "done"
    NEEDS_HUMAN = "needs_human"
    FAILED = "failed"


# Allowed transitions. Anything else is a bug and raises.
TRANSITIONS: dict[Status, frozenset[Status]] = {
    Status.QUEUED: frozenset({Status.RUNNING, Status.NEEDS_HUMAN, Status.FAILED}),
    # RUNNING -> QUEUED happens only when recovering an interrupted run.
    Status.RUNNING: frozenset({Status.VERIFYING, Status.QUEUED, Status.NEEDS_HUMAN, Status.FAILED}),
    # VERIFYING -> RUNNING is the single verification-feedback retry.
    Status.VERIFYING: frozenset(
        {Status.RUNNING, Status.PR_OPEN, Status.QUEUED, Status.NEEDS_HUMAN, Status.FAILED}
    ),
    Status.PR_OPEN: frozenset({Status.DONE, Status.FAILED, Status.NEEDS_HUMAN}),
    Status.DONE: frozenset(),
    # A human (porter resume) puts a task back in the queue.
    Status.NEEDS_HUMAN: frozenset({Status.QUEUED}),
    Status.FAILED: frozenset({Status.QUEUED}),
}

ACTIVE = frozenset({Status.RUNNING, Status.VERIFYING})
TERMINAL = frozenset({Status.DONE, Status.FAILED})


class TransitionError(RuntimeError):
    pass


def check_transition(old: Status, new: Status) -> None:
    if new not in TRANSITIONS[old]:
        raise TransitionError(f"illegal task transition {old} -> {new}")


@dataclass(frozen=True)
class Task:
    """A unit of work from a task source."""

    id: str
    title: str
    body: str = ""
    priority: int = 9  # lower number = more urgent
    labels: tuple[str, ...] = field(default_factory=tuple)
    url: str = ""
    source: str = ""

    @property
    def slug(self) -> str:
        return slugify(self.title)


def slugify(text: str, max_len: int = 40) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")
    slug = slug[:max_len].rstrip("-")
    return slug or "task"


def priority_from_labels(labels: list[str] | tuple[str, ...], default: int = 9) -> int:
    """Map labels like ``p1`` / ``P2`` / ``priority:p0`` to an integer priority."""
    best = default
    for label in labels:
        m = re.fullmatch(r"(?:priority[:/ -])?p(\d)", label.strip().lower())
        if m:
            best = min(best, int(m.group(1)))
    return best
