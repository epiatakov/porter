"""Memory backends: recall context before a run, record lessons after notable ones.

``file`` keeps Markdown notes in ``.porter/memory/``. ``cairn`` shells out to
``cairn search --json`` (an optional external CLI); if it is missing or fails,
recall returns nothing and the run goes on.
"""

from __future__ import annotations

import json
import re
import time
from abc import ABC, abstractmethod
from dataclasses import dataclass
from pathlib import Path

from .models import slugify
from .proc import Shell


@dataclass
class MemoryItem:
    title: str
    text: str
    source: str = ""


class Memory(ABC):
    name = "memory"

    @abstractmethod
    def recall(self, query: str, limit: int = 5) -> list[MemoryItem]: ...

    def remember(self, title: str, text: str) -> Path | None:
        return None


class NoMemory(Memory):
    name = "none"

    def recall(self, query: str, limit: int = 5) -> list[MemoryItem]:
        return []


def _words(text: str) -> set[str]:
    return {w for w in re.findall(r"[a-z0-9]{3,}", text.lower())}


class FileMemory(Memory):
    name = "file"

    def __init__(self, directory: Path, clock=time.time):
        self.dir = directory
        self.clock = clock

    def recall(self, query: str, limit: int = 5) -> list[MemoryItem]:
        if not self.dir.exists():
            return []
        q = _words(query)
        scored = []
        for path in sorted(self.dir.glob("*.md")):
            text = path.read_text(encoding="utf-8", errors="replace")
            score = len(q & _words(text))
            if score:
                title = text.splitlines()[0].lstrip("# ").strip() if text else path.stem
                scored.append((score, path.name, MemoryItem(title, text.strip(), str(path))))
        scored.sort(key=lambda s: (-s[0], s[1]))
        return [item for _, _, item in scored[:limit]]

    def remember(self, title: str, text: str) -> Path:
        self.dir.mkdir(parents=True, exist_ok=True)
        stamp = time.strftime("%Y%m%d-%H%M%S", time.gmtime(self.clock()))
        path = self.dir / f"{stamp}-{slugify(title)}.md"
        path.write_text(f"# {title}\n\n{text.strip()}\n", encoding="utf-8")
        return path


class CairnMemory(Memory):
    """Recall through the ``cairn`` CLI. Writes go to the local file backend."""

    name = "cairn"

    def __init__(
        self,
        shell: Shell,
        fallback: FileMemory,
        binary: str = "cairn",
        args: list[str] | None = None,
        warn=None,
    ):
        self.shell = shell
        self.fallback = fallback
        self.binary = binary
        self.args = list(args or [])
        self.warn = warn or (lambda msg: None)

    def recall(self, query: str, limit: int = 5) -> list[MemoryItem]:
        res = self.shell.run([self.binary, "search", "--json", *self.args, query], timeout=30)
        if not res.ok:
            self.warn(f"cairn unavailable ({res.returncode}): {res.output[:200]}")
            return self.fallback.recall(query, limit)
        try:
            data = json.loads(res.stdout or "[]")
        except json.JSONDecodeError:
            self.warn("cairn returned non-JSON output; ignoring")
            return self.fallback.recall(query, limit)
        if isinstance(data, dict):
            data = data.get("results", data.get("items", []))
        items = []
        for hit in data if isinstance(data, list) else []:
            if not isinstance(hit, dict):
                continue
            title = str(hit.get("title") or hit.get("path") or hit.get("id") or "note")
            text = str(hit.get("snippet") or hit.get("text") or hit.get("content") or "")
            items.append(MemoryItem(title, text, str(hit.get("path", ""))))
        return items[:limit]

    def remember(self, title: str, text: str) -> Path:
        return self.fallback.remember(title, text)
