"""Tasks from a local TOML or JSON file (tests, demos, offline use).

TOML::

    [[task]]
    id = "demo-1"
    title = "Add a --version flag"
    body = "..."
    labels = ["p1"]

JSON: a list of the same objects, or ``{"task": [...]}``.
"""

from __future__ import annotations

import json
import tomllib
from pathlib import Path

from ..models import Task, priority_from_labels
from .base import TaskSource, sort_tasks


class LocalFile(TaskSource):
    name = "local"

    def __init__(self, path: Path):
        self.path = path

    def list(self) -> list[Task]:
        if not self.path.exists():
            raise FileNotFoundError(f"task file not found: {self.path}")
        text = self.path.read_text(encoding="utf-8")
        data = json.loads(text) if self.path.suffix == ".json" else tomllib.loads(text)
        items = data.get("task", data.get("tasks", [])) if isinstance(data, dict) else data
        tasks = []
        for i, item in enumerate(items):
            if item.get("done") or item.get("state", "open") != "open":
                continue
            labels = tuple(str(x) for x in item.get("labels", []))
            tasks.append(
                Task(
                    id=str(item.get("id", i + 1)),
                    title=str(item["title"]),
                    body=str(item.get("body", "")),
                    priority=int(item.get("priority", priority_from_labels(labels))),
                    labels=labels,
                    url=str(item.get("url", "")),
                    source=self.name,
                )
            )
        return sort_tasks(tasks)
