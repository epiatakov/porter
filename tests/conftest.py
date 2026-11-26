from __future__ import annotations

import json
import os
import shlex
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

import porter
from porter.config import load_config
from porter.proc import Result, Shell


class FakeGhShell(Shell):
    """Runs real commands (git, verify) but answers ``gh`` from canned responses."""

    def __init__(self, pr_url: str = "https://github.com/acme/widgets/pull/7", pr_state="OPEN"):
        self.gh_calls: list[list[str]] = []
        self.gh_inputs: list[str | None] = []
        self.pr_url = pr_url
        self.pr_state = pr_state

    def run(self, args, *, cwd=None, input=None, timeout=None, check=False):
        if isinstance(args, list) and args and args[0] == "gh":
            self.gh_calls.append(list(args))
            self.gh_inputs.append(input)
            if args[1:3] == ["pr", "create"]:
                return Result(args, 0, self.pr_url + "\n")
            if args[1:3] == ["pr", "view"]:
                return Result(args, 0, json.dumps({"state": self.pr_state, "url": self.pr_url}))
            return Result(args, 0, "")
        return super().run(args, cwd=cwd, input=input, timeout=timeout, check=check)


def child_env(**extra: str) -> dict[str, str]:
    """Environment for a child ``python -m porter.cli`` that imports the porter under test.

    Without it the child imports the installed porter, which can be another checkout (an
    editable install) when the tests run in a worktree.
    """
    src = str(Path(porter.__file__).resolve().parents[1])
    path = os.pathsep.join(p for p in (src, os.environ.get("PYTHONPATH")) if p)
    return {**os.environ, "PYTHONPATH": path, **extra}


def git(cwd: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=cwd, check=True, capture_output=True, text=True
    ).stdout.strip()


TASKS = """
[[task]]
id = "7"
title = "Add greeting file"
body = "Create GREETING.md."
labels = ["p2"]

[[task]]
id = "3"
title = "Urgent fix"
body = "Fix it."
labels = ["p1"]
"""


# Verify commands run through the shell: /bin/sh on POSIX, cmd.exe on Windows. Tests use
# only commands that both shells run the same way, so they need no POSIX tools on PATH.
PASS = "exit 0"
FAIL = "exit 1"


def py(code: str) -> str:
    """A verify command that runs Python ``code`` with the interpreter running porter.

    Keep ``code`` free of double quotes, ``$``, ``%`` and backslashes: sh or cmd.exe
    would change them.
    """
    return f'{{python}} -c "{code}"'


def check(expr: str) -> str:
    """A verify command that passes when the Python expression ``expr`` is true.

    ``expr`` can use ``Path`` and reads paths relative to the verify directory.
    """
    return py(f"import sys; from pathlib import Path; sys.exit(not ({expr}))")


def write_config(
    root: Path, *, script: str, verify: list[str] | None = None, extra: str = ""
) -> Path:
    # A JSON string list is also a TOML array.
    verify = json.dumps([PASS] if verify is None else verify)
    cfg = root / "porter.toml"
    cfg.write_text(
        textwrap.dedent(
            f"""
            [repo]
            default_branch = "main"

            [tasks]
            source = "local"
            file = "tasks.toml"

            [executor]
            kind = "fake"

            [executor.fake]
            script = {script}

            [verify]
            commands = {verify}
            timeout = 60

            [budgets]
            wall_seconds = 600
            attempts = 3
            """
        )
        + extra
    )
    if not (root / "tasks.toml").exists():
        (root / "tasks.toml").write_text(TASKS)
    return cfg


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    """A git repo on ``main`` with a bare ``origin``."""
    remote = tmp_path / "remote.git"
    work = tmp_path / "work"
    subprocess.run(["git", "init", "-q", "--bare", "-b", "main", str(remote)], check=True)
    work.mkdir()
    git(work, "init", "-q", "-b", "main")
    git(work, "config", "user.email", "porter@example.invalid")
    git(work, "config", "user.name", "porter test")
    (work / ".gitignore").write_text(".porter/\n")
    (work / "tasks.toml").write_text(TASKS)
    (work / "README.md").write_text("test repo\n")
    git(work, "add", "-A")
    git(work, "commit", "-q", "-m", "init")
    git(work, "remote", "add", "origin", str(remote))
    git(work, "push", "-q", "-u", "origin", "main")
    return work


@pytest.fixture
def make_config():
    def _make(root: Path, **kw):
        path = write_config(root, **kw)
        if (root / ".git").exists():
            git(root, "add", "-A")
            git(root, "commit", "-q", "-m", "config")
        return load_config(path)

    return _make


def states(porter, task_id: str) -> list[str]:
    """The ordered list of statuses a task went through, per the journal."""
    out = []
    for e in porter.journal.read(task=task_id):
        if e["kind"] == "state":
            if not out:
                out.append(e["from"])
            out.append(e["to"])
    return out


FAKE_GH = """\
# Fake `gh` for tests that go through the real CLI entry point.
import json
import os
import sys

args = sys.argv[1:]
with open(os.environ["FAKE_GH_LOG"], "a", encoding="utf-8") as log:
    log.write(" ".join(args) + "\\n")
if args[:2] == ["pr", "create"]:
    sys.stdin.read()
    print("https://github.com/acme/widgets/pull/7")
elif args[:2] == ["pr", "view"]:
    print(json.dumps({"state": "OPEN", "url": "https://github.com/acme/widgets/pull/7"}))
"""


@pytest.fixture
def fake_gh_bin(tmp_path: Path, monkeypatch) -> Path:
    """Send porter's ``gh`` calls to a canned fake. Returns the file that logs its calls.

    On POSIX a ``gh`` script goes first on PATH. On Windows, porter starts ``gh`` without
    a shell, and then Windows looks up only ``.exe`` files on PATH: no script or ``.cmd``
    file can stand in for ``gh.exe``. There the fixture changes ``Shell.run`` so that a
    ``gh`` command runs the same fake.
    """
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    fake = bin_dir / "fake_gh.py"
    fake.write_text(FAKE_GH, encoding="utf-8")
    log = tmp_path / "gh-calls.log"
    log.touch()
    monkeypatch.setenv("FAKE_GH_LOG", str(log))
    if os.name == "nt":
        real_run = Shell.run

        def run(self, args, **kw):
            if not isinstance(args, str) and args and args[0] == "gh":
                args = [sys.executable, str(fake), *args[1:]]
            return real_run(self, args, **kw)

        monkeypatch.setattr(Shell, "run", run)
    else:
        gh = bin_dir / "gh"
        python, script = shlex.quote(sys.executable), shlex.quote(str(fake))
        gh.write_text(f'#!/bin/sh\nexec {python} {script} "$@"\n')
        gh.chmod(0o755)
        monkeypatch.setenv("PATH", f"{bin_dir}{os.pathsep}{os.environ['PATH']}")
    return log
