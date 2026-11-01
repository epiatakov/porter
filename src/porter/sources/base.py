"""Task source interface."""

from __future__ import annotations

from abc import ABC, abstractmethod

from ..models import Task


class TaskSource(ABC):
    """Lists tasks and reports escalations back to wherever tasks live."""

    name: str = "source"
    # True when list() hides escalated tasks, so a task listed again was cleared by a human.
    requeue_when_listed: bool = False

    @abstractmethod
    def list(self) -> list[Task]:
        """Return open tasks ready for an agent, most urgent first."""

    def get(self, task_id: str) -> Task | None:
        return next((t for t in self.list() if t.id == task_id), None)

    def escalate(self, task: Task, label: str, comment: str, dry_run: bool = False) -> list[str]:
        """Flag ``task`` for a human. Returns human-readable notes of what was (or would be) done.

        The default does nothing remote; the journal and handoff still record it.
        """
        return []

    def pr_link(self, task: Task) -> str:
        """Text placed in a PR body to link back to the task."""
        return f"Task: {task.url or task.id}"


def sort_tasks(tasks: list[Task]) -> list[Task]:
    def key(t: Task):
        num = int(t.id) if t.id.isdigit() else 0
        return (t.priority, num, t.id)

    return sorted(tasks, key=key)
