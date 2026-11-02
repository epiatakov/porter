"""Per-task worktrees: porter never changes the developer's checkout.

These tests use a real Git repository, the real CLI entry point, the FakeExecutor,
and a fake ``gh`` on PATH.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

from porter.cli import main
from porter.models import Status
from porter.runner import Porter

from .conftest import FakeGhShell, git

BRANCH = "porter/7-add-greeting-file"
RUN_7 = ("run", "--once", "--task", "7")
WRITE = '[{ outcome = "completed", summary = "Wrote it.", write = { "GREETING.md" = "hi" } }]'


def quiet(*_):
    pass


def tree_of(cfg) -> Path:
    return cfg.worktrees_dir / "7-add-greeting-file"


def developer_at_work(repo: Path) -> None:
    """A developer on a feature branch with staged, unstaged, and untracked work."""
    (repo / "notes.txt").write_text("notes\n")
    git(repo, "add", "notes.txt")
    git(repo, "commit", "-q", "-m", "notes")
    git(repo, "switch", "-q", "-c", "feature/dev")
    (repo / "feature.txt").write_text("feature work\n")
    git(repo, "add", "feature.txt")
    git(repo, "commit", "-q", "-m", "feature")
    (repo / "README.md").write_text("test repo\nstaged edit\n")
    git(repo, "add", "README.md")  # staged
    (repo / "notes.txt").write_text("notes\nunstaged edit\n")  # unstaged
    (repo / "draft.md").write_text("my draft, not for porter\n")  # untracked


def snapshot(repo: Path) -> dict:
    return {
        "branch": git(repo, "rev-parse", "--abbrev-ref", "HEAD"),
        "head": git(repo, "rev-parse", "HEAD"),
        "stage": git(repo, "ls-files", "--stage"),
        "staged": git(repo, "diff", "--cached"),
        "unstaged": git(repo, "diff"),
        "status": git(repo, "status", "--porcelain", "--untracked-files=all"),
        "files": {
            p.relative_to(repo).as_posix(): p.read_text()
            for p in sorted(repo.rglob("*"))
            if p.is_file() and ".git" not in p.parts and ".porter" not in p.parts
        },
    }


def run_cli(cfg, *args: str) -> int:
    return main(["--config", str(cfg.source_path), *args])


def committed(repo: Path) -> list[str]:
    return git(repo, "diff", "--name-only", f"main..{BRANCH}").splitlines()


def test_task_runs_in_worktree_and_leaves_developer_checkout_alone(
    repo, make_config, fake_gh_bin, tmp_path
):
    seen = tmp_path / "verify-saw.txt"
    verify = (
        f"['pwd > {seen}', 'test -f GREETING.md', 'test ! -e draft.md', "
        "'test ! -e feature.txt', 'grep -qx \"test repo\" README.md', "
        "'test \"$(cat notes.txt)\" = notes']"
    )
    cfg = make_config(repo, script=WRITE, verify=verify)
    developer_at_work(repo)
    before = snapshot(repo)

    assert run_cli(cfg, "run", "--once", "--task", "7") == 0

    assert snapshot(repo) == before  # branch, index, tracked edits, untracked files
    tree = tree_of(cfg)
    # Verification ran in the task's worktree and saw the task's files, not the developer's.
    assert Path(seen.read_text().strip()).resolve() == tree.resolve()
    assert git(tree, "rev-parse", "--abbrev-ref", "HEAD") == BRANCH
    # The task commit holds only the intended change, on top of the configured base.
    assert committed(repo) == ["GREETING.md"]
    assert git(repo, "merge-base", "main", BRANCH) == git(repo, "rev-parse", "main")
    assert git(repo, "ls-remote", "origin", BRANCH)
    calls = fake_gh_bin.read_text()
    assert f"pr create --base main --head {BRANCH}" in calls
    assert "pr merge" not in calls
    # State stays in the shared location, not in the worktree.
    assert (cfg.state_dir() / "state.db").exists()
    assert not (tree / ".porter").exists()


def test_interrupted_run_keeps_unfinished_edits_and_resumes_in_same_worktree(
    repo, make_config, fake_gh_bin, capsys
):
    script = (
        '[{ outcome = "completed", kill = true, write = { "part.txt" = "half done" } }, '
        + WRITE[1:]
    )
    verify = "['test -f part.txt', 'test -f GREETING.md']"
    cfg = make_config(repo, script=script, verify=verify)
    developer_at_work(repo)
    before = snapshot(repo)

    proc = subprocess.run(
        [sys.executable, "-m", "porter.cli", "--config", str(cfg.source_path), *RUN_7],
        cwd=repo,
        capture_output=True,
        text=True,
        timeout=60,
        env={**os.environ, "PORTER_FAKE_ALLOW_KILL": "1"},
    )
    assert proc.returncode == -9, proc.stderr
    tree = tree_of(cfg)
    # The unfinished edit is in porter's worktree, available for inspection.
    assert (tree / "part.txt").read_text() == "half done"
    assert snapshot(repo) == before
    capsys.readouterr()
    assert run_cli(cfg, "status") == 0
    assert f"worktree: {tree}" in capsys.readouterr().out

    assert run_cli(cfg, "run", "--once") == 0  # the interrupted task goes first

    assert snapshot(repo) == before
    assert sorted(committed(repo)) == ["GREETING.md", "part.txt"]
    porter = Porter(cfg, shell=FakeGhShell(), out=quiet)
    notes = [e.get("note", "") for e in porter.journal.read(task="7") if e["kind"] == "git"]
    assert any(n.startswith("created") for n in notes)
    assert any(n.startswith("reusing worktree") for n in notes)
    assert [r.outcome for r in porter.store.runs("7")] == ["interrupted", "completed"]
    prompt = (cfg.state_dir() / "runs" / "2" / "prompt.md").read_text()
    assert str(tree) in prompt  # the recovery handoff points at the worktree


def test_branch_checked_out_in_developer_checkout_is_reported(repo, make_config, fake_gh_bin):
    cfg = make_config(repo, script=WRITE)
    git(repo, "switch", "-q", "-c", BRANCH)
    (repo / "wip.txt").write_text("developer's own work on the porter branch\n")
    (repo / "README.md").write_text("test repo\nstaged\n")
    git(repo, "add", "README.md")
    before = snapshot(repo)

    assert run_cli(cfg, "run", "--once", "--task", "7") == 3

    assert snapshot(repo) == before
    assert not tree_of(cfg).exists()
    porter = Porter(cfg, shell=FakeGhShell(), out=quiet)
    row = porter.store.get("7")
    assert row.status == Status.NEEDS_HUMAN
    assert "checked out in the developer checkout" in row.reason
    (esc,) = [e for e in porter.journal.read(task="7") if e["kind"] == "escalation"]
    assert "git switch main" in esc["next_step"] and "porter resume 7" in esc["next_step"]
    porter.close()

    # Following the instruction: the developer moves off the branch and resumes.
    git(repo, "commit", "-q", "-m", "developer commit on the branch")
    git(repo, "switch", "-q", "main")
    assert run_cli(cfg, "resume", "7") == 0
    assert (tree_of(cfg) / "wip.txt").exists() is False  # untracked files stay with the developer
    assert (repo / "wip.txt").exists()
    show = git(repo, "show", "--name-only", "--format=", BRANCH).splitlines()
    assert show == ["GREETING.md"]


def test_foreign_worktree_for_branch_is_reported(repo, make_config, tmp_path):
    cfg = make_config(repo, script=WRITE)
    other = tmp_path / "elsewhere"
    git(repo, "worktree", "add", "-q", "-b", BRANCH, str(other), "main")
    (other / "theirs.txt").write_text("keep me\n")

    outcome = Porter(cfg, shell=FakeGhShell(), out=quiet).run_once(task_id="7")

    assert outcome.status == Status.NEEDS_HUMAN
    assert "not porter's worktree" in outcome.reason
    assert (other / "theirs.txt").read_text() == "keep me\n"
    assert not tree_of(cfg).exists()


def test_stray_directory_at_worktree_path_is_reported(repo, make_config):
    cfg = make_config(repo, script=WRITE)
    stray = tree_of(cfg)
    stray.mkdir(parents=True)
    (stray / "file.txt").write_text("not porter's\n")

    outcome = Porter(cfg, shell=FakeGhShell(), out=quiet).run_once(task_id="7")

    assert outcome.status == Status.NEEDS_HUMAN
    assert "is not porter's worktree" in outcome.reason
    assert (stray / "file.txt").read_text() == "not porter's\n"


def test_retry_reuses_edits_and_rejects_replaced_worktree(repo, make_config):
    script = (
        '[{ outcome = "blocked", blocker = "unclear", write = { "part.txt" = "half" } }, '
        + WRITE[1:]
    )
    cfg = make_config(repo, script=script, verify="['test -f part.txt']")
    porter = Porter(cfg, shell=FakeGhShell(), out=quiet)
    assert porter.run_once(task_id="7").status == Status.NEEDS_HUMAN
    tree = tree_of(cfg)
    assert (tree / "part.txt").exists()

    # Someone replaced porter's worktree with an unrelated repository on the same branch.
    saved = repo.parent / "saved-tree"
    shutil.move(tree, saved)
    git(tree.parent, "init", "-q", "-b", BRANCH, tree.name)
    (tree / "unrelated.txt").write_text("x\n")
    outcome = porter.resume("7")
    assert outcome.status == Status.NEEDS_HUMAN
    assert "different repository" in outcome.reason
    assert (tree / "unrelated.txt").exists()

    # Put the real worktree back: the retry continues with the unfinished edit.
    shutil.rmtree(tree)
    shutil.move(saved, tree)
    outcome = porter.resume("7")
    assert outcome.status == Status.PR_OPEN, outcome.reason
    assert sorted(committed(repo)) == ["GREETING.md", "part.txt"]


def test_dry_run_creates_no_worktree_and_changes_nothing(repo, make_config, fake_gh_bin):
    cfg = make_config(repo, script=WRITE)
    developer_at_work(repo)
    before = snapshot(repo)
    worktrees_before = git(repo, "worktree", "list", "--porcelain")
    branches_before = git(repo, "branch", "--list")

    assert run_cli(cfg, "run", "--once", "--task", "7", "--dry-run") == 0

    assert snapshot(repo) == before
    assert git(repo, "worktree", "list", "--porcelain") == worktrees_before
    assert git(repo, "branch", "--list") == branches_before
    assert not cfg.worktrees_dir.exists()
    assert fake_gh_bin.read_text() == ""  # no PR, issue, or label calls
    assert (cfg.state_dir(True) / "runs" / "1" / "handoff.md").exists()
    assert not (cfg.state_dir() / "state.db").exists()
