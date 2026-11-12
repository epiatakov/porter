"""Git and pull-request operations, through ``git`` and ``gh``.

Hard rules, independent of config: porter only commits and pushes branches under
its own prefix, never the default branch, and never merges.

Each task gets its own linked worktree under ``<state dir>/worktrees/``. porter
never switches branches, stages, or commits in the developer's checkout: the
executor, verification, change detection, commit, and push all run in the task's
worktree.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path

from .models import Task
from .proc import CommandError, Shell


class PolicyError(RuntimeError):
    pass


class WorktreeConflict(RuntimeError):
    """porter cannot safely use or create the task's worktree; nothing was changed.

    ``recovery`` is a concrete instruction for the human.
    """

    def __init__(self, message: str, recovery: str):
        super().__init__(message)
        self.recovery = recovery


@dataclass
class PullRequest:
    title: str
    body: str
    branch: str
    base: str
    url: str = ""
    commands: list[str] = field(default_factory=list)


@dataclass
class Worktree:
    path: Path
    branch: str
    note: str


def worktree_dirname(branch: str, prefix: str) -> str:
    """``porter/7-add-greeting`` -> ``7-add-greeting`` (a single safe path component)."""
    name = branch[len(prefix) :] if branch.startswith(prefix) else branch
    return re.sub(r"[^A-Za-z0-9._-]+", "-", name).strip(".-") or "task"


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
        worktrees_dir: Path | None = None,
    ):
        self.shell = shell
        self.workdir = workdir  # the developer's checkout; porter never changes it
        self.default_branch = default_branch
        self.remote = remote
        self.prefix = prefix
        self.github_repo = github_repo
        self.state_dir_name = state_dir_name
        self.dry_run = dry_run
        self.worktrees_dir = worktrees_dir or workdir / state_dir_name / "worktrees"

    def branch_name(self, task: Task) -> str:
        return f"{self.prefix}{task.id}-{task.slug}"

    def worktree_path(self, branch: str) -> Path:
        return self.worktrees_dir / worktree_dirname(branch, self.prefix)

    def _guard(self, branch: str) -> None:
        if branch == self.default_branch or not branch.startswith(self.prefix):
            raise PolicyError(f"refusing to write to branch {branch!r}")

    def _git(self, *args: str, cwd: Path | None = None, check: bool = True):
        return self.shell.run(["git", *args], cwd=cwd or self.workdir, check=check)

    def _repo_args(self) -> list[str]:
        return ["--repo", self.github_repo] if self.github_repo else []

    def current_branch(self, cwd: Path | None = None) -> str:
        return self._git("rev-parse", "--abbrev-ref", "HEAD", cwd=cwd).stdout.strip()

    # -- worktrees -------------------------------------------------------------

    def worktrees(self) -> list[dict[str, str]]:
        """``git worktree list --porcelain`` as dicts with ``path`` and ``branch``."""
        out = self._git("worktree", "list", "--porcelain").stdout
        entries: list[dict[str, str]] = []
        for line in out.splitlines():
            key, _, value = line.partition(" ")
            if key == "worktree":
                entries.append({"path": value, "branch": ""})
            elif entries and key == "branch":
                entries[-1]["branch"] = value.removeprefix("refs/heads/")
            elif entries and key in ("prunable", "locked", "detached", "bare"):
                entries[-1][key] = value or "yes"
        return entries

    def _common_dir(self, cwd: Path) -> str:
        res = self._git("rev-parse", "--path-format=absolute", "--git-common-dir", cwd=cwd)
        return str(Path(res.stdout.strip()).resolve())

    def prepare_worktree(self, branch: str) -> Worktree:
        """Create or reuse porter's worktree for ``branch``. Never touches the developer checkout.

        A new branch starts from the default branch. An existing porter worktree is
        reused as-is, with any unfinished edits. Anything ambiguous raises
        :class:`WorktreeConflict` and leaves every file where it is.
        """
        self._guard(branch)
        path = self.worktree_path(branch)
        if self.dry_run:
            cmd = f"git worktree add -b {branch} {path} {self.default_branch}"
            return Worktree(path, branch, f"would run: {cmd}")
        here = self.workdir.resolve()
        entries = self.worktrees()
        holder = next((e for e in entries if e["branch"] == branch), None)
        at_path = next((e for e in entries if _same(e["path"], path)), None)
        if holder is not None and _same(holder["path"], path):
            self._check_owned(path, branch)
            return Worktree(path, branch, f"reusing worktree {path}")
        if holder is not None and _same(holder["path"], here):
            raise WorktreeConflict(
                f"branch {branch} is checked out in the developer checkout {here}",
                f"commit or stash your work there and switch that checkout to another branch "
                f"(for example `git switch {self.default_branch}`); porter will then continue "
                f"the branch in its own worktree {path}",
            )
        if holder is not None:
            where = holder["path"]
            raise WorktreeConflict(
                f"branch {branch} is checked out in {where}, which is not porter's worktree",
                f"save any work in {where}, then remove it with `git worktree remove {where}` "
                "(or `git worktree prune` if that directory is gone)",
            )
        if at_path is not None or path.exists():
            raise WorktreeConflict(
                f"{path} already exists but is not porter's worktree for {branch}",
                f"inspect {path} and move it aside (or `git worktree prune` if it is a stale "
                "entry); porter will then create a fresh worktree there",
            )
        path.parent.mkdir(parents=True, exist_ok=True)
        exists = self._git("rev-parse", "--verify", "--quiet", f"refs/heads/{branch}", check=False)
        if exists.ok:
            self._git("worktree", "add", str(path), branch)
            return Worktree(path, branch, f"added worktree {path} for existing {branch}")
        self._git("worktree", "add", "-b", branch, str(path), self.default_branch)
        return Worktree(path, branch, f"created {branch} from {self.default_branch} in {path}")

    def _check_owned(self, path: Path, branch: str) -> None:
        """The registered worktree at ``path`` must be this repository, on ``branch``."""
        problem = ""
        if not path.is_dir():
            problem = "its directory is missing"
        else:
            try:
                if self._common_dir(path) != self._common_dir(self.workdir):
                    problem = "it belongs to a different repository"
                elif self.current_branch(path) != branch:
                    problem = f"it is on {self.current_branch(path)!r}"
            except CommandError as exc:
                problem = f"git cannot read it ({_first_line(str(exc))})"
        if problem:
            raise WorktreeConflict(
                f"worktree {path} is registered for {branch}, but {problem}",
                f"inspect {path}, save anything you need, then run `git worktree prune` "
                f"(and move {path} aside if it still exists)",
            )

    def remove_worktree(self, branch: str) -> str:
        """Remove porter's worktree for a finished branch if it is clean. Returns a note."""
        path = self.worktree_path(branch)
        if self.dry_run:
            return f"would run: git worktree remove {path}"
        entries = self.worktrees()
        if not any(_same(e["path"], path) and e["branch"] == branch for e in entries):
            return ""
        self._check_owned(path, branch)
        # No --force: git refuses if there are uncommitted or untracked files, so
        # nothing unfinished is ever deleted.
        self._git("worktree", "remove", str(path))
        return f"removed worktree {path}"

    # -- changes, commit, PR -----------------------------------------------------

    def has_changes(self, worktree: Path) -> bool:
        if self.dry_run:
            return True
        status = self._git("status", "--porcelain", cwd=worktree)
        state = self.state_dir_name.rstrip("/") + "/"
        return any(ln[3:] and not ln[3:].startswith(state) for ln in status.stdout.splitlines())

    def commits_ahead(self, branch: str) -> int:
        res = self._git("rev-list", "--count", f"{self.default_branch}..{branch}", check=False)
        return int(res.stdout.strip() or 0) if res.ok else 0

    def commit_push_pr(
        self, task: Task, branch: str, title: str, body: str, worktree: Path
    ) -> PullRequest:
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
            f"cd {worktree}",
            f"git add -A && git reset -q -- {self.state_dir_name}",
            f"git commit -m {title!r}",
            f"git push -u {self.remote} {branch}",
            " ".join(create[:3]) + f" --base {self.default_branch} --head {branch} ...",
        ]
        if self.dry_run:
            pr.url = f"(dry-run) would open PR {branch} -> {self.default_branch}"
            return pr
        if _same(worktree, self.workdir):
            raise PolicyError("refusing to commit in the developer checkout")
        current = self.current_branch(worktree)
        if current != branch:
            raise PolicyError(f"worktree {worktree} is on {current!r}, expected {branch!r}")
        # Staging happens only inside the task's worktree.
        self._git("add", "-A", cwd=worktree)
        # Never commit porter's own state, even if it is not in .gitignore.
        self._git("reset", "-q", "--", self.state_dir_name, cwd=worktree, check=False)
        if not self._git("diff", "--cached", "--quiet", cwd=worktree, check=False).ok:
            self._git("commit", "-m", commit_msg, cwd=worktree)
        self._git("push", "-u", self.remote, branch, cwd=worktree)
        res = self.shell.run(create, cwd=worktree, input=body)
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


def _same(a: str | Path, b: str | Path) -> bool:
    return Path(a).resolve() == Path(b).resolve()


def _first_line(text: str) -> str:
    return text.strip().splitlines()[0] if text.strip() else ""
