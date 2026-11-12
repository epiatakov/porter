"""A scripted executor for tests and dry runs. It never calls a real agent.

Script steps (one per attempt; the last step repeats)::

    [executor.fake]
    script = [
      { outcome = "completed", summary = "...", write = { "notes.txt" = "hi" } },
    ]

Keys: ``outcome`` (completed|blocked|failed), ``summary``, ``blocker``, ``write``
(files relative to the workdir), ``events`` (list of strings), ``cost``
(e.g. ``{premium_requests = 1}``), ``kill`` (SIGKILL this process after the writes, to
test recovery; only when ``PORTER_FAKE_ALLOW_KILL=1``), ``sleep`` (seconds per event, to
test wall-time budgets).
"""

from __future__ import annotations

import os
import signal
import time
from collections.abc import Iterator
from typing import Any

from ..models import Task
from .base import Capabilities, ExecEvent, ExecResult, Executor, Run, RunContext

DEFAULT_STEP = {"outcome": "completed", "summary": "Fake executor made the requested change."}


class FakeExecutor(Executor):
    name = "fake"
    capabilities = Capabilities(
        resume=True,
        tools=True,
        structured_output=False,
        cost_reporting=True,
        model_per_run=True,
        notes="scripted",
    )
    safe_for_dry_run = True

    def __init__(self, script: list[dict[str, Any]] | None = None, **_: Any):
        self.script = list(script or [DEFAULT_STEP])
        self.started: list[RunContext] = []

    def _step(self, attempt: int) -> dict[str, Any]:
        return self.script[min(attempt - 1, len(self.script) - 1)]

    def start(self, task: Task, context: RunContext) -> Run:
        self.started.append(context)
        step = self._step(context.attempt)
        return Run(task=task, context=context, state={"step": step, "stopped": False})

    def events(self, run: Run) -> Iterator[ExecEvent]:
        step = run.state["step"]
        ctx = run.context
        yield ExecEvent("message", f"fake: starting attempt {ctx.attempt} for task {run.task.id}")
        for text in step.get("events", []):
            if run.state["stopped"]:
                return
            time.sleep(float(step.get("sleep", 0)))
            yield ExecEvent("message", str(text))
        # A dry run keeps fake edits inside the run dir so the checkout stays untouched.
        root = ctx.run_dir / "workdir" if ctx.dry_run else ctx.workdir
        for rel, content in step.get("write", {}).items():
            target = root / rel
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(str(content), encoding="utf-8")
            yield ExecEvent("tool", f"wrote {rel}", {"tool": "write", "path": rel})
        if step.get("kill") and os.environ.get("PORTER_FAKE_ALLOW_KILL") == "1":
            # Simulates the machine or process dying after unfinished edits: no cleanup,
            # no handoff. Guarded by an env var so a test runner never kills itself.
            os.kill(os.getpid(), signal.SIGKILL)
        if step.get("cost"):
            yield ExecEvent("usage", "usage", dict(step["cost"]))

    def stop(self, run: Run) -> None:
        run.state["stopped"] = True

    def result(self, run: Run) -> ExecResult:
        if run.state["stopped"]:
            return ExecResult("stopped", summary="stopped by porter")
        step = run.state["step"]
        return ExecResult(
            outcome=step.get("outcome", "completed"),
            summary=step.get("summary", DEFAULT_STEP["summary"]),
            blocker=step.get("blocker", ""),
            session_id=f"fake-session-{run.task.id}",
            cost={k: float(v) for k, v in step.get("cost", {}).items()},
        )

    def describe(self, context: RunContext) -> str:
        return f"fake executor, attempt {context.attempt}: {self._step(context.attempt)}"
