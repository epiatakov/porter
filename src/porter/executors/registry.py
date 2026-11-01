"""Build executors from config. Plugins name a ``module:attr`` factory."""

from __future__ import annotations

from typing import Any

from ..config import ExecutorConfig
from ..plugins import load_object
from .base import Executor


def build_executor(cfg: ExecutorConfig) -> Executor:
    opts: dict[str, Any] = dict(cfg.options)
    if cfg.kind == "fake":
        from .fake import FakeExecutor

        return FakeExecutor(**opts)
    if cfg.kind == "copilot-cli":
        from .copilot import CopilotCliExecutor

        return CopilotCliExecutor(**opts)
    if ":" in cfg.kind:
        factory = load_object(cfg.kind)
        executor = factory(**opts)
        if not isinstance(executor, Executor):
            raise TypeError(f"{cfg.kind} did not return an Executor")
        return executor
    raise ValueError(
        f"unknown executor kind {cfg.kind!r} (built in: fake, copilot-cli; "
        "planned: codex, claude-code; or use module:attr)"
    )
