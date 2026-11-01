"""Executors: adapters that drive a coding agent."""

from .base import (
    Capabilities,
    ExecEvent,
    ExecResult,
    Executor,
    Run,
    RunContext,
    parse_status_line,
)

__all__ = [
    "Capabilities",
    "ExecEvent",
    "ExecResult",
    "Executor",
    "Run",
    "RunContext",
    "parse_status_line",
]
