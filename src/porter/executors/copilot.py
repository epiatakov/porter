"""Drives the GitHub Copilot CLI (``copilot``) in non-interactive prompt mode.

Only behaviour verified from public sources is relied on; see docs/executors.md.
Verified: ``-p/--prompt`` runs one prompt non-interactively; ``--allow-all-tools``,
``--allow-tool``/``--deny-tool``, ``--model``, ``--output-format json`` (JSONL) and
``--log-dir`` exist; auth comes from the environment (``COPILOT_GITHUB_TOKEN``,
``GH_TOKEN``, ``GITHUB_TOKEN``) or a prior ``copilot`` login. Not verified: exit
codes, the JSONL event schema, resume combined with ``-p``, the format of the usage
summary. Capabilities below reflect that.
"""

from __future__ import annotations

import json
import os
import queue
import subprocess
import threading
import time
from collections.abc import Iterator
from typing import Any

from ..models import Task
from .base import (
    Capabilities,
    ExecEvent,
    ExecResult,
    Executor,
    Run,
    RunContext,
    parse_status_line,
)

_EOF = object()


class CopilotCliExecutor(Executor):
    name = "copilot-cli"
    capabilities = Capabilities(
        resume=False,  # --resume/--session-id exist, but not verified together with -p
        tools=True,  # agentic: edits files and runs commands under --allow-* flags
        structured_output=False,  # no schema-constrained final answer found
        cost_reporting=False,  # an exit summary exists; its format is not verified
        notes="see docs/executors.md for what was verified",
    )

    def __init__(
        self,
        binary: str | list[str] = "copilot",
        model: str = "",
        allow_all_tools: bool = True,
        allow_tools: list[str] | None = None,
        deny_tools: list[str] | None = None,
        output_format: str = "text",  # "text" | "json" (JSONL; schema unverified)
        extra_args: list[str] | None = None,
        env: dict[str, str] | None = None,
        stop_grace_seconds: float = 10.0,
        **_: Any,
    ):
        if output_format not in ("text", "json"):
            raise ValueError("executor.copilot.output_format must be 'text' or 'json'")
        self.binary = [binary] if isinstance(binary, str) else list(binary)
        self.model = model
        self.allow_all_tools = allow_all_tools
        self.allow_tools = list(allow_tools or [])
        self.deny_tools = list(deny_tools or [])
        self.output_format = output_format
        self.extra_args = list(extra_args or [])
        self.env = dict(env or {})
        self.stop_grace_seconds = stop_grace_seconds

    # -- command ---------------------------------------------------------------

    def command(self, context: RunContext) -> list[str]:
        cmd = [*self.binary, "-p", context.prompt]
        if self.model:
            cmd += ["--model", self.model]
        if self.allow_all_tools:
            cmd.append("--allow-all-tools")
        for tool in self.allow_tools:
            cmd += ["--allow-tool", tool]
        for tool in self.deny_tools:
            cmd += ["--deny-tool", tool]
        if self.output_format == "json":
            cmd += ["--output-format", "json"]
        cmd += ["--log-dir", str(context.run_dir / "copilot-logs")]
        cmd += self.extra_args
        return cmd

    def describe(self, context: RunContext) -> str:
        cmd = self.command(context)
        shown = [f"<prompt: {len(a)} chars>" if a == context.prompt else a for a in cmd]
        return f"(cwd {context.workdir}) " + " ".join(shown)

    # -- lifecycle -------------------------------------------------------------

    def start(self, task: Task, context: RunContext) -> Run:
        context.run_dir.mkdir(parents=True, exist_ok=True)
        env = {**os.environ, "NO_COLOR": "1", **self.env}
        proc = subprocess.Popen(
            self.command(context),
            cwd=context.workdir,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            bufsize=1,
            env=env,
        )
        lines: queue.Queue = queue.Queue()

        def pump() -> None:
            assert proc.stdout is not None
            for line in proc.stdout:
                lines.put(line.rstrip("\n"))
            lines.put(_EOF)

        threading.Thread(target=pump, daemon=True).start()
        return Run(
            task=task,
            context=context,
            handle=proc,
            state={"lines": lines, "text": [], "stopped": False, "timed_out": False},
        )

    def events(self, run: Run) -> Iterator[ExecEvent]:
        lines: queue.Queue = run.state["lines"]
        transcript = (run.context.run_dir / "copilot-output.log").open("w", encoding="utf-8")
        try:
            while True:
                deadline = run.context.deadline
                if deadline is not None and time.monotonic() > deadline:
                    run.state["timed_out"] = True
                    self.stop(run)
                    yield ExecEvent("error", "wall-time budget reached; stopped copilot")
                    return
                try:
                    item = lines.get(timeout=0.5)
                except queue.Empty:
                    continue
                if item is _EOF:
                    break
                transcript.write(item + "\n")
                transcript.flush()
                yield self._event(run, item)
        finally:
            transcript.close()
            run.handle.wait()

    def _event(self, run: Run, line: str) -> ExecEvent:
        if self.output_format == "json":
            try:
                obj = json.loads(line)
            except json.JSONDecodeError:
                obj = None
            if isinstance(obj, dict):
                text = _first_text(obj)
                if text:
                    run.state["text"].append(text)
                kind = str(obj.get("type") or obj.get("event") or "json")
                return ExecEvent("message", text, {"type": kind, "raw": obj})
        run.state["text"].append(line)
        return ExecEvent("log", line)

    def stop(self, run: Run) -> None:
        proc: subprocess.Popen = run.handle
        run.state["stopped"] = True
        if proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(timeout=self.stop_grace_seconds)
            except subprocess.TimeoutExpired:
                proc.kill()

    def result(self, run: Run) -> ExecResult:
        proc: subprocess.Popen = run.handle
        rc = proc.wait()
        text = "\n".join(run.state["text"])
        status = parse_status_line(text)
        tail = _tail(text)
        if run.state["stopped"]:
            reason = "wall-time budget reached" if run.state["timed_out"] else "stopped by porter"
            return ExecResult("stopped", summary=reason)
        if status and status[0] == "blocked":
            return ExecResult(
                "blocked", summary=tail, blocker=status[1] or "agent reported blocked"
            )
        if rc != 0:
            return ExecResult("failed", summary=f"copilot exited with code {rc}\n{tail}")
        summary = tail if status else tail + "\n(no PORTER-STATUS line; verification decides)"
        return ExecResult("completed", summary=summary)


def _first_text(obj: dict[str, Any]) -> str:
    """Best-effort text extraction from a JSONL event whose schema is not documented."""
    for key in ("content", "text", "message", "deltaContent"):
        val = obj.get(key)
        if isinstance(val, str):
            return val
    data = obj.get("data")
    if isinstance(data, dict):
        return _first_text(data)
    return ""


def _tail(text: str, lines: int = 15, chars: int = 2000) -> str:
    return "\n".join(text.strip().splitlines()[-lines:])[-chars:]
