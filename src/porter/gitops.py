"""Git and pull-request operations, through ``git`` and ``gh``.

Hard rules, independent of config: porter only commits and pushes branches under
its own prefix, never the default branch, and never merges.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path

from .models import Task
from .proc import CommandError, Shell


class PolicyError(RuntimeError):
    pass


@dataclass
class PullRequest:
    title: str
    body: str
    branch: str
    base: str
    url: str = ""
    commands: list[str] = field(default_factory=list)


class GitOps:
    def __init__(
        self,
        shell: Shell,
        workdir: Path,
        default_branch: str = "main",
        remote: str = "origin",
        prefix: str = "porter/",
        github_repo: str = "",
        state_dir_name: str = ".porter",
        dry_run: bool = False,
    ):
        self.shell = shell
        self.workdir = workdir
        self.default_branch = default_branch
        self.remote = remote
        self.prefix = prefix
        self.github_repo = github_repo
        self.state_dir_name = state_dir_name
        self.dry_run = dry_run

    def branch_name(self, task: Task) -> str:
        return f"{self.prefix}{task.id}-{task.slug}"

    def _guard(self, branch: str) -> None:
        if branch == self.default_branch or not branch.startswith(self.prefix):
            raise PolicyError(f"refusing to write to branch {branch!r}")

    def _git(self, *args: str, check: bool = True):
        return self.shell.run(["git", *args], cwd=self.workdir, check=check)

    def _repo_args(self) -> list[str]:
        return ["--repo", self.github_repo] if self.github_repo else []

    def current_branch(self) -> str:
        return self._git("rev-parse", "--abbrev-ref", "HEAD").stdout.strip()

    def prepare_branch(self, branch: str) -> str:
        """Check out ``branch``, creating it from the default branch if needed."""
        self._guard(branch)
        if self.dry_run:
            return f"would run: git checkout -b {branch} {self.default_branch}"
        exists = self._git("rev-parse", "--verify", "--quiet", f"refs/heads/{branch}", check=False)
        if exists.ok:
            self._git("checkout", branch)
            return f"checked out existing {branch}"
        self._git("checkout", "-b", branch, self.default_branch)
        return f"created {branch} from {self.default_branch}"

    def has_changes(self) -> bool:
        if self.dry_run:
            return True
        status = self._git("status", "--porcelain")
        state = self.state_dir_name.rstrip("/") + "/"
        return any(ln[3:] and not ln[3:].startswith(state) for ln in status.stdout.splitlines())

    def commits_ahead(self, branch: str) -> int:
        res = self._git("rev-list", "--count", f"{self.default_branch}..{branch}", check=False)
        return int(res.stdout.strip() or 0) if res.ok else 0

    def commit_push_pr(self, task: Task, branch: str, title: str, body: str) -> PullRequest:
        self._guard(branch)
        pr = PullRequest(title=title, body=body, branch=branch, base=self.default_branch)
        commit_msg = f"{title}\n\nporter task {task.id}"
        create = [
            "gh",
            "pr",
            "create",
            *self._repo_args(),
            "--base",
            self.default_branch,
            "--head",
            branch,
            "--title",
            title,
            "--body-file",
            "-",
        ]
        pr.commands = [
            f"git add -A && git reset -q -- {self.state_dir_name}",
            f"git commit -m {title!r}",
            f"git push -u {self.remote} {branch}",
            " ".join(create[:3]) + f" --base {self.default_branch} --head {branch} ...",
        ]
        if self.dry_run:
            pr.url = f"(dry-run) would open PR {branch} -> {self.default_branch}"
            return pr
        if self.current_branch() != branch:
            raise PolicyError(f"working tree is on {self.current_branch()!r}, expected {branch!r}")
        self._git("add", "-A")
        # Never commit porter's own state, even if it is not in .gitignore.
        self._git("reset", "-q", "--", self.state_dir_name, check=False)
        if not self._git("diff", "--cached", "--quiet", check=False).ok:
            self._git("commit", "-m", commit_msg)
        self._git("push", "-u", self.remote, branch)
        res = self.shell.run(create, cwd=self.workdir, input=body)
        if not res.ok:
            raise CommandError(res)
        pr.url = res.stdout.strip().splitlines()[-1] if res.stdout.strip() else ""
        return pr

    def pr_state(self, branch: str) -> str:
        """Return MERGED, CLOSED, OPEN, or '' if unknown."""
        res = self.shell.run(
            ["gh", "pr", "view", branch, *self._repo_args(), "--json", "state,url"],
            cwd=self.workdir,
        )
        if not res.ok:
            return ""
        try:
            return str(json.loads(res.stdout).get("state", ""))
        except json.JSONDecodeError:
            return ""
