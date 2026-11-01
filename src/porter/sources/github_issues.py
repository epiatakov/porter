"""Tasks from GitHub issues, through the GitHub CLI (``gh``).

Auth is whatever ``gh`` is already logged in with (``gh auth login`` or ``GH_TOKEN``).
Porter never stores or reads tokens itself.
"""

from __future__ import annotations

import json

from ..models import Task, priority_from_labels
from ..proc import CommandError, Shell
from .base import TaskSource, sort_tasks

FIELDS = "number,title,body,labels,url"


class GitHubIssues(TaskSource):
    name = "github"
    requeue_when_listed = True  # list() skips issues carrying the escalation label

    def __init__(
        self,
        shell: Shell,
        repo: str = "",
        label: str = "agent-ready",
        limit: int = 50,
        skip_labels: tuple[str, ...] = ("needs-human",),
    ):
        self.shell = shell
        self.repo = repo
        self.label = label
        self.limit = limit
        self.skip_labels = skip_labels

    def _repo_args(self) -> list[str]:
        return ["--repo", self.repo] if self.repo else []

    def list(self) -> list[Task]:
        args = [
            "gh",
            "issue",
            "list",
            *self._repo_args(),
            "--label",
            self.label,
            "--state",
            "open",
            "--limit",
            str(self.limit),
            "--json",
            FIELDS,
        ]
        res = self.shell.run(args)
        if not res.ok:
            raise CommandError(res)
        tasks = []
        for issue in json.loads(res.stdout or "[]"):
            labels = tuple(lbl["name"] for lbl in issue.get("labels", []))
            if any(s in labels for s in self.skip_labels):
                continue
            tasks.append(
                Task(
                    id=str(issue["number"]),
                    title=issue["title"],
                    body=issue.get("body") or "",
                    priority=priority_from_labels(labels),
                    labels=labels,
                    url=issue.get("url", ""),
                    source=self.name,
                )
            )
        return sort_tasks(tasks)

    def escalate(self, task: Task, label: str, comment: str, dry_run: bool = False) -> list[str]:
        cmds = [
            [
                "gh",
                "label",
                "create",
                label,
                *self._repo_args(),
                "--force",
                "--color",
                "D93F0B",
                "--description",
                "porter stopped; a human is needed",
            ],
            ["gh", "issue", "edit", task.id, *self._repo_args(), "--add-label", label],
            ["gh", "issue", "comment", task.id, *self._repo_args(), "--body", comment],
        ]
        notes = []
        for cmd in cmds:
            if dry_run:
                notes.append("would run: " + " ".join(cmd[:4]) + " ...")
                continue
            res = self.shell.run(cmd)
            notes.append(("ok: " if res.ok else "failed: ") + " ".join(cmd[:4]))
        return notes

    def pr_link(self, task: Task) -> str:
        return f"Closes #{task.id}" + (f" ({task.url})" if task.url else "")
