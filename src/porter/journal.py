"""Append-only journal: one JSON object per line in ``.porter/journal.jsonl``.

Kinds used by the core: ``decision``, ``state``, ``executor``, ``verify``, ``cost``,
``git``, ``escalation``, ``memory``, ``warning``.
"""

from __future__ import annotations

import json
import os
import time
from collections.abc import Iterator
from pathlib import Path
from typing import Any


class Journal:
    def __init__(self, path: Path, clock=time.time):
        path.parent.mkdir(parents=True, exist_ok=True)
        self.path = path
        self.clock = clock

    def append(
        self, kind: str, *, task: str | None = None, run: int | None = None, **data: Any
    ) -> dict:
        entry: dict[str, Any] = {"ts": round(self.clock(), 3), "kind": kind}
        if task is not None:
            entry["task"] = task
        if run is not None:
            entry["run"] = run
        entry.update(data)
        line = json.dumps(entry, ensure_ascii=False, default=str)
        # O_APPEND keeps concurrent writers from interleaving within a line.
        fd = os.open(self.path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o644)
        try:
            os.write(fd, (line + "\n").encode())
        finally:
            os.close(fd)
        return entry

    def read(self, task: str | None = None) -> Iterator[dict]:
        if not self.path.exists():
            return
        with self.path.open(encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    entry = json.loads(line)
                except json.JSONDecodeError:
                    continue  # a torn final line from a killed process
                if task is None or entry.get("task") == task:
                    yield entry
