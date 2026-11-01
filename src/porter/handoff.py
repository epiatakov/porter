"""Handoff notes: ``.porter/runs/<id>/handoff.md``.

Every run ends by writing one. The next run (or a human) starts by reading the last
one, so work resumes from what was actually done rather than from scratch.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

SECTIONS = ("Goal", "Done so far", "Next step", "Blockers")


@dataclass
class Handoff:
    task_id: str
    run_id: int
    goal: str
    done: list[str] = field(default_factory=list)
    next_step: str = ""
    blockers: list[str] = field(default_factory=list)

    def to_markdown(self) -> str:
        def bullets(items: list[str]) -> str:
            return "\n".join(f"- {i}" for i in items) if items else "- (none)"

        return (
            f"# Handoff: task {self.task_id}, run {self.run_id}\n\n"
            f"## Goal\n\n{self.goal.strip()}\n\n"
            f"## Done so far\n\n{bullets(self.done)}\n\n"
            f"## Next step\n\n{self.next_step.strip() or '(none)'}\n\n"
            f"## Blockers\n\n{bullets(self.blockers)}\n"
        )

    def write(self, run_dir: Path) -> Path:
        run_dir.mkdir(parents=True, exist_ok=True)
        path = run_dir / "handoff.md"
        tmp = path.with_suffix(".md.tmp")
        tmp.write_text(self.to_markdown(), encoding="utf-8")
        tmp.replace(path)  # atomic: a killed process never leaves half a handoff
        return path

    @classmethod
    def parse(cls, text: str) -> Handoff:
        head = re.search(r"^# Handoff: task (.+?), run (\d+)", text, re.M)
        parts: dict[str, str] = {}
        for m in re.finditer(r"^## (.+?)\n(.*?)(?=^## |\Z)", text, re.M | re.S):
            parts[m.group(1).strip()] = m.group(2).strip()

        def items(s: str) -> list[str]:
            out = [ln[2:].strip() for ln in s.splitlines() if ln.startswith("- ")]
            return [i for i in out if i != "(none)"]

        nxt = parts.get("Next step", "")
        return cls(
            task_id=head.group(1) if head else "",
            run_id=int(head.group(2)) if head else 0,
            goal=parts.get("Goal", ""),
            done=items(parts.get("Done so far", "")),
            next_step="" if nxt == "(none)" else nxt,
            blockers=items(parts.get("Blockers", "")),
        )

    @classmethod
    def load(cls, path: Path) -> Handoff | None:
        try:
            return cls.parse(path.read_text(encoding="utf-8"))
        except OSError:
            return None
