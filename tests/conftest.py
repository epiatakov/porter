from __future__ import annotations

import json
import subprocess
import textwrap
from pathlib import Path

import pytest

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


def write_config(root: Path, *, script: str, verify: str = '["true"]', extra: str = "") -> Path:
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
