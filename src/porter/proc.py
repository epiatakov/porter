"""A thin, injectable wrapper around subprocess so git/gh/verify calls are testable."""

from __future__ import annotations

import subprocess
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path


@dataclass
class Result:
    args: str | list[str]
    returncode: int
    stdout: str = ""
    stderr: str = ""

    @property
    def ok(self) -> bool:
        return self.returncode == 0

    @property
    def output(self) -> str:
        return (self.stdout + ("\n" if self.stdout and self.stderr else "") + self.stderr).strip()


class CommandError(RuntimeError):
    def __init__(self, result: Result):
        self.result = result
        cmd = result.args if isinstance(result.args, str) else " ".join(result.args)
        super().__init__(f"command failed ({result.returncode}): {cmd}\n{result.output[-2000:]}")


class Shell:
    """Runs commands. A string runs through the shell; a list runs directly."""

    def run(
        self,
        args: str | Sequence[str],
        *,
        cwd: Path | None = None,
        input: str | None = None,
        timeout: float | None = None,
        check: bool = False,
    ) -> Result:
        shell = isinstance(args, str)
        cmd: str | list[str] = args if shell else list(args)
        try:
            cp = subprocess.run(
                cmd,
                cwd=cwd,
                input=input,
                capture_output=True,
                text=True,
                timeout=timeout,
                shell=shell,
            )
            res = Result(cmd, cp.returncode, cp.stdout, cp.stderr)
        except FileNotFoundError as exc:
            res = Result(cmd, 127, "", f"not found: {exc.filename or cmd}")
        except subprocess.TimeoutExpired as exc:
            out = exc.stdout.decode() if isinstance(exc.stdout, bytes) else (exc.stdout or "")
            res = Result(cmd, 124, out, f"timed out after {timeout}s")
        if check and not res.ok:
            raise CommandError(res)
        return res


Handler = Callable[[list[str] | str, dict], Result | None]


class RecordingShell(Shell):
    """Test double: records every call and answers from registered handlers.

    ``responses`` maps a command prefix (e.g. ``"gh pr create"``) to a Result or a
    callable. Unmatched commands succeed with empty output.
    """

    def __init__(self, responses: dict[str, Result | Handler] | None = None):
        self.calls: list[str | list[str]] = []
        self.responses = dict(responses or {})

    def run(self, args, *, cwd=None, input=None, timeout=None, check=False) -> Result:
        self.calls.append(args if isinstance(args, str) else list(args))
        text = args if isinstance(args, str) else " ".join(args)
        res: Result | None = None
        for prefix in sorted(self.responses, key=len, reverse=True):
            if text.startswith(prefix):
                handler = self.responses[prefix]
                res = handler(args, {"cwd": cwd, "input": input}) if callable(handler) else handler
                break
        if res is None:
            res = Result(args, 0, "", "")
        if check and not res.ok:
            raise CommandError(res)
        return res

    def commands(self) -> list[str]:
        return [c if isinstance(c, str) else " ".join(c) for c in self.calls]
