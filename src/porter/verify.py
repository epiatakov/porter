"""Verification: run configured commands after the executor finishes."""

from __future__ import annotations

import shlex
import sys
from dataclasses import dataclass, field
from pathlib import Path

from .proc import Shell


@dataclass
class CheckResult:
    command: str
    returncode: int
    output: str

    @property
    def ok(self) -> bool:
        return self.returncode == 0


@dataclass
class VerifyReport:
    results: list[CheckResult] = field(default_factory=list)

    @property
    def passed(self) -> bool:
        return all(r.ok for r in self.results)

    def failures(self) -> list[CheckResult]:
        return [r for r in self.results if not r.ok]

    def feedback(self, max_chars: int = 4000) -> str:
        parts = [
            f"$ {r.command}\n(exit {r.returncode})\n{r.output[-max_chars:]}"
            for r in self.failures()
        ]
        return "\n\n".join(parts)


def expand(command: str) -> str:
    """``{python}`` expands to the interpreter running porter (portable demo configs)."""
    return command.replace("{python}", shlex.quote(sys.executable))


def run_checks(shell: Shell, commands: list[str], cwd: Path, timeout: float) -> VerifyReport:
    report = VerifyReport()
    for cmd in commands:
        res = shell.run(expand(cmd), cwd=cwd, timeout=timeout)
        report.results.append(CheckResult(cmd, res.returncode, res.output))
        if not res.ok:
            break  # later checks are usually noise once one fails
    return report
