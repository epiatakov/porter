"""The executor interface: start -> events -> stop -> result.

Executors differ. Each one declares what it can do in ``capabilities`` and the core
reads that instead of assuming. For example, the core only passes a resume session
when ``capabilities.resume`` is true, and only enforces a premium-request cap from
reported numbers when ``capabilities.cost_reporting`` is true.
"""

from __future__ import annotations

import re
from abc import ABC, abstractmethod
from collections.abc import Iterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

from ..models import Task

Outcome = Literal["completed", "blocked", "failed", "stopped"]


@dataclass(frozen=True)
class Capabilities:
    resume: bool = False  # can continue a previous agent session by id
    tools: bool = False  # agent can edit files / run commands itself
    structured_output: bool = False  # final answer can be constrained to a schema
    cost_reporting: bool = False  # reports usage (e.g. premium requests) per run
    notes: str = ""  # caveats, e.g. what was not verified

    def as_dict(self) -> dict[str, Any]:
        return {
            "resume": self.resume,
            "tools": self.tools,
            "structured_output": self.structured_output,
            "cost_reporting": self.cost_reporting,
        }


@dataclass
class RunContext:
    run_id: int
    attempt: int
    workdir: Path
    run_dir: Path
    prompt: str
    deadline: float | None = None  # monotonic seconds
    resume_session: str = ""  # only set when the executor declares resume
    dry_run: bool = False


@dataclass
class ExecEvent:
    kind: str  # "message" | "tool" | "log" | "error" | "usage"
    text: str = ""
    data: dict[str, Any] = field(default_factory=dict)


@dataclass
class ExecResult:
    outcome: Outcome
    summary: str = ""
    blocker: str = ""
    session_id: str = ""
    cost: dict[str, float] = field(default_factory=dict)  # e.g. {"premium_requests": 1}


@dataclass
class Run:
    task: Task
    context: RunContext
    handle: Any = None  # executor-private (process, iterator, ...)
    state: dict[str, Any] = field(default_factory=dict)


class Executor(ABC):
    name: str = "executor"
    capabilities: Capabilities = Capabilities()
    # True only for executors with no side effects outside the run dir (the fake).
    safe_for_dry_run: bool = False

    @abstractmethod
    def start(self, task: Task, context: RunContext) -> Run: ...

    @abstractmethod
    def events(self, run: Run) -> Iterator[ExecEvent]:
        """Yield events until the agent finishes. The core may call ``stop`` between events."""

    @abstractmethod
    def stop(self, run: Run) -> None: ...

    @abstractmethod
    def result(self, run: Run) -> ExecResult: ...

    def describe(self, context: RunContext) -> str:
        """Human-readable preview of what ``start`` would launch (used by --dry-run)."""
        return f"{self.name} executor"


STATUS_RE = re.compile(r"^\s*PORTER-STATUS:\s*(done|blocked)\s*(?::\s*(.*))?$", re.I | re.M)


def parse_status_line(text: str) -> tuple[str, str] | None:
    """Find the last ``PORTER-STATUS: done`` / ``PORTER-STATUS: blocked: <reason>`` line."""
    matches = list(STATUS_RE.finditer(text))
    if not matches:
        return None
    m = matches[-1]
    return m.group(1).lower(), (m.group(2) or "").strip()
