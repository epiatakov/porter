"""End-to-end core loop tests with the FakeExecutor."""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

from porter.handoff import Handoff
from porter.models import Status, Task
from porter.runner import BusyError, Porter

from .conftest import FakeGhShell, git, states

ALL_STATES = ["queued", "running", "verifying", "pr_open", "done"]
WRITE = '[{ outcome = "completed", summary = "Wrote it.", write = { "GREETING.md" = "hi" } }]'


def quiet(*_):
    pass


def test_dry_run_walks_every_state(tmp_path, make_config):
    cfg = make_config(tmp_path, script=WRITE)
    porter = Porter(cfg, dry_run=True, out=quiet)
    outcome = porter.run_once()
    assert outcome.task_id == "3"  # p1 before p2
    assert outcome.status == Status.DONE
    assert states(porter, "3") == ALL_STATES
    handoff = Handoff.load(cfg.state_dir(True) / "runs" / "1" / "handoff.md")
    assert handoff.goal.startswith("Urgent fix")
    assert any("opened PR" in d for d in handoff.done)
    assert "Closes" not in outcome.pr.body and "Task: 3" in outcome.pr.body
    assert not (tmp_path / "GREETING.md").exists()  # dry run leaves the checkout alone
    assert not (cfg.state_dir() / "state.db").exists()  # and real state untouched
    kinds = {e["kind"] for e in porter.journal.read()}
    assert {"decision", "state", "executor", "verify", "cost", "git"} <= kinds


def test_real_run_opens_pr_on_branch(repo, make_config):
    cfg = make_config(repo, script=WRITE)
    main_before = git(repo, "rev-parse", "main")
    remote_main_before = git(repo, "ls-remote", "origin", "refs/heads/main")
    shell = FakeGhShell()
    porter = Porter(cfg, shell=shell, out=quiet)
    outcome = porter.run_once(task_id="7")
    assert outcome.status == Status.PR_OPEN, outcome.reason
    assert outcome.pr.url == "https://github.com/acme/widgets/pull/7"
    branch = "porter/7-add-greeting-file"
    # The developer checkout stays on its branch; the work happened in porter's worktree.
    assert git(repo, "rev-parse", "--abbrev-ref", "HEAD") == "main"
    tree = cfg.worktrees_dir / "7-add-greeting-file"
    assert git(tree, "rev-parse", "--abbrev-ref", "HEAD") == branch
    assert not (repo / "GREETING.md").exists()
    # The branch reached the remote; main did not move.
    assert git(repo, "ls-remote", "origin", branch)
    assert git(repo, "rev-parse", "main") == main_before
    assert git(repo, "ls-remote", "origin", "refs/heads/main") == remote_main_before
    assert "GREETING.md" in git(repo, "show", "--name-only", "--format=", branch)
    assert ".porter" not in git(repo, "show", "--name-only", "--format=", branch)
    (create,) = [c for c in shell.gh_calls if c[1:3] == ["pr", "create"]]
    assert create[create.index("--base") + 1] == "main"
    assert create[create.index("--head") + 1] == branch
    body = shell.gh_inputs[shell.gh_calls.index(create)]
    assert "## Checklist" in body and "Task: 7" in body
    assert not any(c[1:3] == ["pr", "merge"] for c in shell.gh_calls)

    # Next run reconciles the PR: merged -> done.
    shell.pr_state = "MERGED"
    porter.run_once()  # also picks task 3
    assert porter.store.get("7").status == Status.DONE
    # A merged task's clean worktree is removed; the branch stays.
    assert not tree.exists()
    assert git(repo, "rev-parse", "--verify", branch)


def test_verify_failure_feeds_back_once_then_passes(repo, make_config):
    # Attempt 1 writes nothing (check fails); attempt 2 writes the file (check passes).
    script = '[{ outcome = "completed" }, { outcome = "completed", write = { "ok.flag" = "1" } }]'
    cfg = make_config(repo, script=script, verify='["test -f ok.flag"]')
    porter = Porter(cfg, shell=FakeGhShell(), out=quiet)
    outcome = porter.run_once(task_id="7")
    assert outcome.status == Status.PR_OPEN
    assert states(porter, "7") == [
        "queued",
        "running",
        "verifying",
        "running",
        "verifying",
        "pr_open",
    ]
    second_prompt = (cfg.state_dir() / "runs" / "2" / "prompt.md").read_text()
    assert "Verification failed on your previous attempt" in second_prompt
    assert "test -f ok.flag" in second_prompt
    assert porter.store.get("7").attempts == 2


def test_verify_failure_escalates_after_feedback(tmp_path, make_config):
    cfg = make_config(tmp_path, script='[{ outcome = "completed" }]', verify='["false"]')
    porter = Porter(cfg, dry_run=True, out=quiet)
    outcome = porter.run_once(task_id="7")
    assert outcome.status == Status.NEEDS_HUMAN
    assert "verification still failing" in outcome.reason
    assert porter.store.get("7").attempts == 2  # original + one feedback round
    assert any(e["kind"] == "escalation" for e in porter.journal.read(task="7"))


def test_blocked_escalates_to_needs_human_with_label_and_comment(repo, make_config):
    from porter.sources import GitHubIssues

    script = '[{ outcome = "blocked", blocker = "needs a deploy token" }]'
    cfg = make_config(repo, script=script)
    shell = FakeGhShell()
    porter = Porter(cfg, shell=shell, source=GitHubIssues(shell), out=quiet)
    # The fake gh lists no issues, so seed the task into state directly.
    porter.store.upsert_task(Task("7", "Add greeting file"))
    outcome = porter.run_once(task_id="7")
    assert outcome.status == Status.NEEDS_HUMAN
    assert outcome.reason == "agent is blocked: needs a deploy token"
    edit = [c for c in shell.gh_calls if c[1:3] == ["issue", "edit"]]
    comment = [c for c in shell.gh_calls if c[1:3] == ["issue", "comment"]]
    assert edit and edit[0][edit[0].index("--add-label") + 1] == "needs-human"
    assert len(comment) == 1
    text = comment[0][comment[0].index("--body") + 1]
    assert "needs a deploy token" in text and "Next step" in text and "porter resume 7" in text
    handoff = Handoff.load(Path(porter.store.get("7").last_handoff))
    assert handoff.blockers == ["agent is blocked: needs a deploy token"]
    # Escalations are captured to memory (routine runs are not).
    assert list((cfg.state_dir() / "memory").glob("*.md"))
    # A stopped task is not picked again until a human resumes it.
    assert porter.run_once() is None or porter.store.get("7").status == Status.NEEDS_HUMAN


def test_attempt_budget_escalates(tmp_path, make_config):
    cfg = make_config(
        tmp_path,
        script='[{ outcome = "completed" }]',
        verify='["false"]',
        extra="[policy]\nverify_feedback_rounds = 5\n",
    )
    cfg.budgets.attempts = 2
    porter = Porter(cfg, dry_run=True, out=quiet)
    outcome = porter.run_once(task_id="7")
    assert outcome.status == Status.NEEDS_HUMAN
    assert porter.store.get("7").attempts == 2
    assert "attempt budget" in outcome.reason or "verification" in outcome.reason


def test_wall_time_budget_stops_executor(tmp_path, make_config):
    script = '[{ outcome = "completed", events = ["a", "b", "c"], sleep = 0.2 }]'
    cfg = make_config(tmp_path, script=script)
    cfg.budgets.wall_seconds = 0.3
    porter = Porter(cfg, dry_run=True, out=quiet)
    outcome = porter.run_once(task_id="7")
    assert outcome.status == Status.NEEDS_HUMAN
    assert "executor stopped" in outcome.reason


def test_premium_request_cap(tmp_path, make_config):
    script = '[{ outcome = "completed", cost = { premium_requests = 3 } }]'
    cfg = make_config(tmp_path, script=script, verify='["false"]')
    cfg.budgets.premium_requests = 2
    porter = Porter(cfg, dry_run=True, out=quiet)
    outcome = porter.run_once(task_id="7")
    assert outcome.status == Status.NEEDS_HUMAN
    assert "premium-request budget" in outcome.reason
    costs = [e for e in porter.journal.read(task="7") if e["kind"] == "cost"]
    assert costs[0]["premium_requests"] == 3 and costs[0]["estimated"] is False


def test_resume_after_escalation(tmp_path, make_config):
    script = '[{ outcome = "blocked", blocker = "unclear" }, { outcome = "completed" }]'
    cfg = make_config(tmp_path, script=script)
    porter = Porter(cfg, dry_run=True, out=quiet)
    assert porter.run_once(task_id="7").status == Status.NEEDS_HUMAN
    outcome = porter.resume("7")
    assert outcome.status == Status.DONE
    prompt = (cfg.state_dir(True) / "runs" / "2" / "prompt.md").read_text()
    assert "Previous handoff" in prompt and "unclear" in prompt


def test_lock_blocks_concurrent_runs(tmp_path, make_config):
    cfg = make_config(tmp_path, script=WRITE)
    porter = Porter(cfg, dry_run=True, out=quiet)
    (porter.state_dir / "lock").write_text(str(os.getpid()))
    with pytest.raises(BusyError):
        porter.run_once()


def test_resume_after_killed_run(repo, make_config):
    """A run killed with SIGKILL mid-executor is recovered and resumed from its handoff."""
    script = '[{ outcome = "completed", kill = true }, ' + WRITE[1:]
    cfg = make_config(repo, script=script)
    proc = subprocess.run(
        [
            sys.executable,
            "-m",
            "porter.cli",
            "--config",
            str(cfg.source_path),
            "run",
            "--once",
            "--task",
            "7",
        ],
        cwd=repo,
        capture_output=True,
        text=True,
        timeout=60,
        env={**os.environ, "PORTER_FAKE_ALLOW_KILL": "1"},
    )
    assert proc.returncode == -9, proc.stderr

    porter = Porter(cfg, shell=FakeGhShell(), out=quiet)
    row = porter.store.get("7")
    assert row.status == Status.RUNNING  # left behind by the killed process
    assert porter.store.open_runs("7")
    assert (porter.state_dir / "lock").exists()  # stale lock from a dead pid

    outcome = porter.run_once()
    assert outcome.task_id == "7"  # the interrupted task is resumed first
    assert outcome.status == Status.PR_OPEN
    runs = porter.store.runs("7")
    assert [r.outcome for r in runs] == ["interrupted", "completed"]
    prompt = (porter.state_dir / "runs" / str(runs[1].id) / "prompt.md").read_text()
    assert "Previous handoff" in prompt and "interrupted" in prompt
    assert states(porter, "7") == ["queued", "running", "queued", "running", "verifying", "pr_open"]
    assert git(repo, "rev-parse", "--abbrev-ref", "HEAD") == "main"
    tree = cfg.worktrees_dir / "7-add-greeting-file"
    assert git(tree, "rev-parse", "--abbrev-ref", "HEAD") == "porter/7-add-greeting-file"


def test_no_changes_escalates(repo, make_config):
    cfg = make_config(repo, script='[{ outcome = "completed" }]')
    porter = Porter(cfg, shell=FakeGhShell(), out=quiet)
    outcome = porter.run_once(task_id="7")
    assert outcome.status == Status.NEEDS_HUMAN
    assert "no changes" in outcome.reason


def test_idle_when_queue_empty(tmp_path, make_config):
    cfg = make_config(tmp_path, script=WRITE)
    (tmp_path / "tasks.toml").write_text("")
    assert Porter(cfg, dry_run=True, out=quiet).run_once() is None


def test_source_failure_starts_no_new_task(tmp_path, make_config):
    cfg = make_config(tmp_path, script=WRITE)
    porter = Porter(cfg, dry_run=True, out=quiet)
    porter.sync_tasks()  # both tasks are stored as queued

    def broken():
        raise OSError("network down")

    porter.source.list = broken
    assert porter.run_once() is None
    assert porter.store.get("3").status == Status.QUEUED
    assert porter.store.get("7").status == Status.QUEUED


def test_github_label_removal_requeues(tmp_path, make_config):
    import json as _json

    from porter.proc import RecordingShell, Result
    from porter.sources import GitHubIssues

    cfg = make_config(tmp_path, script='[{ outcome = "blocked", blocker = "x" }]')
    issue = {"number": 7, "title": "Add greeting file", "body": "", "labels": [], "url": ""}
    listing = {"out": [issue]}
    shell = RecordingShell({"gh issue list": lambda *_: Result([], 0, _json.dumps(listing["out"]))})
    porter = Porter(cfg, shell=shell, source=GitHubIssues(shell), dry_run=True, out=quiet)
    assert porter.run_once().status == Status.NEEDS_HUMAN
    # While labeled needs-human the issue is not listed, so it stays parked.
    listing["out"] = []
    assert porter.run_once() is None
    # A human removed the label: the issue is listed again and is requeued with a fresh budget.
    listing["out"] = [issue]
    porter.sync_tasks()
    row = porter.store.get("7")
    assert row.status == Status.QUEUED and row.attempts == 0


STAGES = """
[[stage]]
name = "plan"
model = "strong"
effort = "high"
instructions = "Write a plan. Do not change code."

[[stage]]
name = "build"
model = "cheap"
verify = true

[[stage]]
name = "review"
model = "strong"
effort = "high"
"""


def test_stages_run_in_fresh_sessions_with_their_models(tmp_path, make_config):
    script = '[{ outcome = "completed" }, { outcome = "completed" }, ' + WRITE[1:]
    cfg = make_config(tmp_path, script=script, extra=STAGES)
    cfg.budgets.attempts = 1  # the attempt budget applies per stage
    porter = Porter(cfg, dry_run=True, out=quiet)
    outcome = porter.run_once(task_id="7")
    assert outcome.status == Status.DONE, outcome.reason
    assert len(outcome.run_ids) == 3
    seen = [(c.model, c.effort) for c in porter.executor.started]
    assert seen == [("strong", "high"), ("cheap", ""), ("strong", "high")]
    runs = cfg.state_dir(True) / "runs"
    plan, build, review = (runs / str(i) / "prompt.md" for i in outcome.run_ids)
    assert "## Stage 1 of 3: plan" in plan.read_text() and "Do not change code." in plan.read_text()
    assert "Stage 2 of 3: build" in build.read_text() and "stage plan done" in build.read_text()
    assert "It is the last one." in review.read_text()
    # build sets verify; the last stage always verifies; plan does not.
    verified = {e["run"] for e in porter.journal.read(task="7") if e["kind"] == "verify"}
    assert verified == set(outcome.run_ids[1:])


def test_resume_continues_the_parked_stage(tmp_path, make_config):
    script = (
        '[{ outcome = "completed" }, { outcome = "blocked", blocker = "no db" }, '
        '{ outcome = "completed" }, ' + WRITE[1:]
    )
    cfg = make_config(tmp_path, script=script, extra=STAGES)
    porter = Porter(cfg, dry_run=True, out=quiet)
    assert porter.run_once(task_id="7").status == Status.NEEDS_HUMAN
    assert porter.store.get("7").stage == "build"
    outcome = porter.resume("7")
    assert outcome.status == Status.DONE
    first = (cfg.state_dir(True) / "runs" / str(outcome.run_ids[0]) / "prompt.md").read_text()
    assert "Stage 2 of 3: build" in first and "no db" in first


def test_stage_model_needs_executor_support(tmp_path, make_config):
    from porter.executors.base import Capabilities
    from porter.executors.fake import FakeExecutor

    class NoModels(FakeExecutor):
        capabilities = Capabilities(tools=True)

    cfg = make_config(tmp_path, script=WRITE, extra=STAGES)
    ex = NoModels(script=[{"outcome": "completed", "write": {"GREETING.md": "hi"}}])
    porter = Porter(cfg, executor=ex, dry_run=True, out=quiet)
    assert porter.run_once(task_id="7").status == Status.DONE
    assert all(c.model == "" for c in ex.started)
    warnings = [e["message"] for e in porter.journal.read() if e["kind"] == "warning"]
    assert any("cannot switch model per run" in w for w in warnings)
