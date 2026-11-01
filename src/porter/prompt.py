"""Builds the instruction text given to an executor for one run."""

from __future__ import annotations

from .handoff import Handoff
from .memory import MemoryItem
from .models import Task

RULES = """\
## Rules
- Work only in this repository, on the current branch. Do not switch branches, push,
  open pull requests, or merge: porter does that after verification.
- Keep the change small and focused on this task. Add or update tests for it.
- Before finishing, run the verification commands listed below and fix failures.
- If you are blocked (missing access, unclear requirement, a needed change outside
  this task, or a fix that would need a workaround nobody asked for), stop and say so.
  Do not invent a new workaround.
- End your final message with exactly one status line:
  `PORTER-STATUS: done` or `PORTER-STATUS: blocked: <one-line reason>`"""


def build_prompt(
    task: Task,
    *,
    handoff: Handoff | None,
    memory: list[MemoryItem],
    feedback: str,
    verify_commands: list[str],
    max_memory_chars: int = 1500,
) -> str:
    parts = [f"# Task {task.id}: {task.title}"]
    if task.url:
        parts.append(f"Source: {task.url}")
    if task.body.strip():
        parts.append(task.body.strip())
    if handoff:
        parts.append(
            "## Previous handoff (resume from here)\n\n"
            + "\n".join(f"- done: {d}" for d in handoff.done)
            + (f"\n- next step: {handoff.next_step}" if handoff.next_step else "")
            + "".join(f"\n- blocker: {b}" for b in handoff.blockers)
        )
    if memory:
        notes = "\n\n".join(f"### {m.title}\n{m.text[:max_memory_chars]}" for m in memory)
        parts.append("## Relevant notes from memory\n\n" + notes)
    if feedback:
        parts.append(
            "## Verification failed on your previous attempt\n\n"
            "Fix these failures; do not weaken or skip the checks.\n\n```\n" + feedback + "\n```"
        )
    if verify_commands:
        parts.append(
            "## Verification commands\n\n" + "\n".join(f"- `{c}`" for c in verify_commands)
        )
    parts.append(RULES)
    return "\n\n".join(parts) + "\n"
