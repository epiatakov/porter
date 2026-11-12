"""The core loop: pick a task, drive an executor, verify, open a PR, or escalate.

One call to :meth:`Porter.run_once` handles at most one task. All decisions go to
the journal; every run ends with a handoff note so the next run can resume.
"""

from __future__ import annotations

import contextlib
import os
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

from .config import Config
from .executors.base import ExecResult, Executor, RunContext
from .executors.fake import FakeExecutor
from .executors.registry import build_executor
from .gitops import GitOps, PolicyError, PullRequest, WorktreeConflict
from .handoff import Handoff
from .journal import Journal
from .memory import CairnMemory, FileMemory, Memory, NoMemory
from .models import ACTIVE, Status, Task
from .plugins import add_plugin_paths, load_object
from .proc import CommandError, Shell
from .prompt import build_prompt
from .sources import GitHubIssues, LocalFile, TaskSource
from .state import StateStore, TaskRow
from .verify import VerifyReport, expand, run_checks


class PorterError(RuntimeError):
    pass


class BusyError(PorterError):
    pass


@dataclass
class Outcome:
    task_id: str
    status: Status
    reason: str = ""
    pr: PullRequest | None = None
    run_ids: list[int] = field(default_factory=list)


def build_source(cfg: Config, shell: Shell) -> TaskSource:
    kind = cfg.tasks.source
    if kind == "local":
        return LocalFile(cfg.root / cfg.tasks.file)
    if kind == "github":
        return GitHubIssues(
            shell,
            repo=cfg.repo.github,
            label=cfg.tasks.label,
            limit=cfg.tasks.limit,
            skip_labels=(cfg.policy.escalation_label,),
        )
    if ":" in kind:
        return load_object(kind)(**cfg.tasks.options)
    raise PorterError(f"unknown task source {kind!r} (built in: local, github; or module:attr)")


def build_memory(cfg: Config, shell: Shell, state_dir: Path, warn) -> Memory:
    files = FileMemory(state_dir / "memory")
    if cfg.memory.backend == "file":
        return files
    if cfg.memory.backend == "cairn":
        return CairnMemory(shell, files, cfg.memory.cairn_binary, cfg.memory.cairn_args, warn)
    if cfg.memory.backend == "none":
        return NoMemory()
    raise PorterError(f"unknown memory backend {cfg.memory.backend!r}")


class Porter:
    def __init__(
        self,
        cfg: Config,
        *,
        shell: Shell | None = None,
        source: TaskSource | None = None,
        executor: Executor | None = None,
        memory: Memory | None = None,
        dry_run: bool = False,
        out: Callable[[str], None] = print,
        clock: Callable[[], float] = time.monotonic,
    ):
        add_plugin_paths(cfg.plugin_paths)
        self.cfg = cfg
        self.dry_run = dry_run
        self.out = out
        self.clock = clock
        self.shell = shell or Shell()
        self.state_dir = cfg.state_dir(dry_run)
        self.state_dir.mkdir(parents=True, exist_ok=True)
        self.store = StateStore(self.state_dir / "state.db")
        self.journal = Journal(self.state_dir / "journal.jsonl")
        self.runs_dir = self.state_dir / "runs"
        self.source = source or build_source(cfg, self.shell)
        executor = executor or build_executor(cfg.executor)
        # A dry run never launches a real agent: it shows the command and uses the fake.
        self.preview: Executor | None = None
        if dry_run and not executor.safe_for_dry_run:
            self.preview, executor = executor, FakeExecutor()
        self.executor = executor
        self.memory = memory or build_memory(cfg, self.shell, self.state_dir, self._warn)
        # Where the current task's files live: its worktree on a real run.
        self.task_dir = cfg.workdir
        self.git = GitOps(
            self.shell,
            cfg.workdir,
            default_branch=cfg.repo.default_branch,
            remote=cfg.repo.remote,
            prefix=cfg.repo.branch_prefix,
            github_repo=cfg.repo.github,
            state_dir_name=cfg.state_dir_name,
            dry_run=dry_run,
            worktrees_dir=cfg.worktrees_dir,
        )

    # -- public API ------------------------------------------------------------

    def run_once(self, task_id: str | None = None) -> Outcome | None:
        with self._lock():
            self.recover_interrupted()
            self.reconcile_prs()
            listed = self.sync_tasks()
            row = self._pick(task_id, listed)
            if row is None:
                self.out("no queued tasks")
                self.journal.append("decision", action="idle", reason="no queued tasks")
                return None
            self.journal.append(
                "decision",
                task=row.task.id,
                action="pick",
                priority=row.task.priority,
                dry_run=self.dry_run,
            )
            return self._work(row)

    def resume(self, task_id: str) -> Outcome:
        """Put an escalated or failed task back in the queue with a fresh budget and run it."""
        with self._lock():
            self.recover_interrupted()
            row = self.store.get(task_id)
            if row is None:
                self.sync_tasks()
                row = self.store.get(task_id)
            if row is None:
                raise PorterError(f"unknown task {task_id}")
            if row.status in (Status.NEEDS_HUMAN, Status.FAILED):
                self._requeue(row, why="porter resume")
            elif row.status != Status.QUEUED:
                raise PorterError(f"task {task_id} is {row.status}; nothing to resume")
            row = self.store.get(task_id)
            assert row is not None
            return self._work(row)

    def sync_tasks(self) -> set[str] | None:
        """Pull tasks from the source into state. Returns listed ids, or None on error."""
        try:
            tasks = self.source.list()
        except Exception as exc:
            self._warn(f"task source {self.source.name} failed: {exc}")
            return None
        for task in tasks:
            row = self.store.upsert_task(task)
            if row.status == Status.NEEDS_HUMAN and self.source.requeue_when_listed:
                # The source hides escalated tasks (e.g. the needs-human label); seeing the
                # task again means a human cleared it.
                self._requeue(row, why="escalation cleared at the source")
        return {t.id for t in tasks}

    def _requeue(self, row: TaskRow, why: str) -> None:
        self._transition(row.task, Status.QUEUED, reason="")
        # Budgets apply per escalation cycle; run history stays in the runs table.
        self.store.update(row.task.id, attempts=0, wall_seconds=0, premium_requests=0)
        self.journal.append(
            "decision", task=row.task.id, action="requeue", why=why, previous=str(row.status)
        )

    def recover_interrupted(self) -> list[str]:
        """Requeue tasks left active by a killed process. Caller must hold the lock."""
        recovered = []
        for row in self.store.list(set(ACTIVE)):
            task = row.task
            prev = Handoff.load(Path(row.last_handoff)) if row.last_handoff else None
            open_runs = self.store.open_runs(task.id)
            for run in open_runs:
                self.store.finish_run(run.id, "interrupted", "process stopped without cleanup")
            run_id = open_runs[-1].id if open_runs else 0
            where = f" in worktree {self.git.worktree_path(row.branch)}" if row.branch else ""
            handoff = Handoff(
                task_id=task.id,
                run_id=run_id,
                goal=_goal(task),
                done=[
                    *(prev.done if prev else []),
                    f"run {run_id} was interrupted while {row.status} (no clean shutdown)",
                ],
                next_step=(
                    f"resume on branch {row.branch or '(not created)'}{where}: inspect what "
                    "the interrupted run changed, then finish the task"
                ),
            )
            path = handoff.write(self.runs_dir / str(run_id))
            self._transition(task, Status.QUEUED, last_handoff=str(path))
            self.journal.append(
                "decision", task=task.id, run=run_id, action="recover", reason="interrupted run"
            )
            self.out(f"recovered interrupted run {run_id} of task {task.id}")
            recovered.append(task.id)
        return recovered

    def reconcile_prs(self) -> None:
        if self.dry_run:
            return
        for row in self.store.list({Status.PR_OPEN}):
            state = self.git.pr_state(row.branch)
            if state == "MERGED":
                self._transition(row.task, Status.DONE)
                self._cleanup_worktree(row)
            elif state == "CLOSED":
                self._transition(row.task, Status.FAILED, reason="PR closed without merge")

    def _cleanup_worktree(self, row: TaskRow) -> None:
        """After a merge, drop the task's worktree if it holds nothing unfinished."""
        try:
            note = self.git.remove_worktree(row.branch)
        except (CommandError, WorktreeConflict) as exc:
            path = self.git.worktree_path(row.branch)
            self._warn(f"kept worktree {path} of task {row.task.id}: {_one_line(str(exc))}")
            return
        if note:
            self.journal.append("git", task=row.task.id, action="remove_worktree", note=note)

    # -- the work loop ---------------------------------------------------------

    def _work(self, row: TaskRow) -> Outcome:
        task = row.task
        outcome = Outcome(task.id, row.status)
        self.out(f"task {task.id}: {row.status} (p{task.priority}) {task.title}")
        reason = self._budget_exceeded(row)
        if reason:
            return self._escalate(
                task,
                reason,
                f"raise the budget or split the task, then `porter resume {task.id}`",
                outcome,
            )

        branch = row.branch or self.git.branch_name(task)
        try:
            tree = self.git.prepare_worktree(branch)
        except WorktreeConflict as exc:
            return self._escalate(
                task,
                f"could not use a worktree for {branch}: {exc}",
                f"{exc.recovery}, then `porter resume {task.id}`",
                outcome,
            )
        except (CommandError, PolicyError) as exc:
            return self._escalate(
                task,
                f"could not prepare worktree for {branch}: {_one_line(str(exc))}",
                f"fix the repository state, then `porter resume {task.id}`",
                outcome,
            )
        self.store.update(task.id, branch=branch)
        self.journal.append(
            "git", task=task.id, action="prepare_worktree", path=str(tree.path), note=tree.note
        )
        # A dry run creates no worktree: the fake executor writes into the run dir and
        # checks run in the configured checkout, which nothing modifies.
        self.task_dir = self.cfg.workdir if self.dry_run else tree.path

        memory = self.memory.recall(f"{task.title}\n{task.body}", self.cfg.memory.limit)
        self.journal.append(
            "memory", task=task.id, backend=self.memory.name, recalled=[m.title for m in memory]
        )

        feedback = ""
        rounds = 0
        while True:
            row = self._row(task.id)
            reason = self._budget_exceeded(row)
            if reason:
                return self._escalate(task, reason, "raise the budget or split the task", outcome)
            prev = Handoff.load(Path(row.last_handoff)) if row.last_handoff else None
            run = self.store.start_run(task.id, self.executor.name)
            outcome.run_ids.append(run.id)
            self._transition(task, Status.RUNNING, run=run.id)
            remaining = self.cfg.budgets.wall_seconds - row.wall_seconds
            ctx = RunContext(
                run_id=run.id,
                attempt=run.attempt,
                workdir=self.task_dir,
                run_dir=self.runs_dir / str(run.id),
                prompt=build_prompt(
                    task,
                    handoff=prev,
                    memory=memory,
                    feedback=feedback,
                    verify_commands=[expand(c) for c in self.cfg.verify.commands],
                ),
                deadline=self.clock() + max(remaining, 0),
                resume_session=row.last_session if self.executor.capabilities.resume else "",
                dry_run=self.dry_run,
            )
            ctx.run_dir.mkdir(parents=True, exist_ok=True)
            (ctx.run_dir / "prompt.md").write_text(ctx.prompt, encoding="utf-8")
            if self.preview is not None:
                self.out(f"dry-run: would launch {self.preview.describe(ctx)}")

            started = self.clock()
            result = self._execute(task, ctx)
            self._account(task.id, run.id, self.clock() - started, result)
            self.store.finish_run(run.id, result.outcome, result.summary, result.session_id)
            if result.session_id:
                self.store.update(task.id, last_session=result.session_id)
            done = [*(prev.done if prev else [])]
            done.append(
                f"run {run.id} ({self.executor.name}): {result.outcome}: "
                f"{_one_line(result.summary)}"
            )

            if result.outcome != "completed":
                reason, nxt = _executor_reason(result, task.id)
                self._handoff(task, run.id, done, nxt, [reason])
                return self._escalate(task, reason, nxt, outcome, run.id)

            self._transition(task, Status.VERIFYING, run=run.id)
            report = self._verify(task, run.id)
            if not report.passed:
                failed = report.failures()[0].command
                done.append(f"verification failed: `{failed}`")
                budget = self._budget_exceeded(self._row(task.id))
                if rounds < self.cfg.policy.verify_feedback_rounds and not budget:
                    rounds += 1
                    feedback = report.feedback()
                    self._handoff(task, run.id, done, f"fix the failure in `{failed}`", [])
                    self.journal.append(
                        "decision", task=task.id, run=run.id, action="verify_feedback", round=rounds
                    )
                    continue
                reason = (
                    f"verification failed (`{failed}`) and {budget}"
                    if budget
                    else f"verification still failing after {rounds} feedback round(s): `{failed}`"
                )
                nxt = (
                    f"look at the failing check in {self._where(task)}, fix or clarify the "
                    "task, then resume"
                )
                self._handoff(task, run.id, done, nxt, [reason])
                return self._escalate(task, reason, nxt, outcome, run.id)

            checks = ", ".join(f"`{c}`" for c in self.cfg.verify.commands) or "(no commands)"
            done.append(f"verification passed: {checks}")
            return self._finish(task, run.id, result, report, done, outcome)

    def _finish(
        self,
        task: Task,
        run_id: int,
        result: ExecResult,
        report: VerifyReport,
        done: list[str],
        outcome: Outcome,
    ) -> Outcome:
        branch = self._row(task.id).branch
        try:
            changed = self.git.has_changes(self.task_dir) or self.git.commits_ahead(branch) > 0
        except CommandError:
            changed = False
        if not changed:
            reason = "agent reported done but the branch has no changes"
            nxt = "check whether the task is already done or needs clarification"
            self._handoff(task, run_id, done, nxt, [reason])
            return self._escalate(task, reason, nxt, outcome, run_id)
        if not self.cfg.policy.open_pr:
            reason = "verified, but policy.open_pr is false"
            nxt = f"review {self._where(task)} and open a PR by hand"
            self._handoff(task, run_id, done, nxt, [])
            return self._escalate(task, reason, nxt, outcome, run_id)

        title = task.title
        body = self._pr_body(task, run_id, result, report)
        try:
            pr = self.git.commit_push_pr(task, branch, title, body, self.task_dir)
        except (CommandError, PolicyError) as exc:
            reason = f"could not push or open the PR: {_one_line(str(exc))}"
            nxt = (
                f"push {branch} from {self._where(task)} and open the PR by hand, or fix "
                "credentials and resume"
            )
            self._handoff(task, run_id, done, nxt, [reason])
            return self._escalate(task, reason, nxt, outcome, run_id)
        self.journal.append(
            "git",
            task=task.id,
            run=run_id,
            action="open_pr",
            url=pr.url,
            branch=branch,
            commands=pr.commands,
            dry_run=self.dry_run,
        )
        done.append(f"opened PR: {pr.url}")
        self._handoff(task, run_id, done, "a human reviews and merges the PR", [])
        self._transition(task, Status.PR_OPEN, run=run_id, pr_url=pr.url)
        outcome.pr = pr
        outcome.status = Status.PR_OPEN
        self._print_pr(pr)
        if self.dry_run:
            self.out("dry-run: treating the PR as merged")
            self._transition(task, Status.DONE, run=run_id)
            outcome.status = Status.DONE
        return outcome

    def _execute(self, task: Task, ctx: RunContext) -> ExecResult:
        ex = self.executor
        try:
            run = ex.start(task, ctx)
        except Exception as exc:
            return ExecResult("failed", summary=f"could not start {ex.name}: {exc}")
        try:
            for ev in ex.events(run):
                self.journal.append(
                    "executor",
                    task=task.id,
                    run=ctx.run_id,
                    event=ev.kind,
                    text=ev.text[:500],
                    data={k: v for k, v in ev.data.items() if k != "raw"},
                )
                if ctx.deadline is not None and self.clock() > ctx.deadline:
                    ex.stop(run)
                    self.journal.append(
                        "decision",
                        task=task.id,
                        run=ctx.run_id,
                        action="stop",
                        reason="wall-time budget",
                    )
                    break
            return ex.result(run)
        except Exception as exc:
            with contextlib.suppress(Exception):
                ex.stop(run)
            return ExecResult("failed", summary=f"{ex.name} error: {exc}")

    def _verify(self, task: Task, run_id: int) -> VerifyReport:
        report = run_checks(
            self.shell, self.cfg.verify.commands, self.task_dir, self.cfg.verify.timeout
        )
        for r in report.results:
            self.journal.append(
                "verify",
                task=task.id,
                run=run_id,
                command=r.command,
                returncode=r.returncode,
                output=r.output[-1000:],
            )
            self.out(f"verify {'ok  ' if r.ok else 'FAIL'} {r.command}")
        return report

    def _account(self, task_id: str, run_id: int, seconds: float, result: ExecResult) -> None:
        premium = result.cost.get("premium_requests", 0.0)
        estimated = False
        if not self.executor.capabilities.cost_reporting and self.cfg.budgets.premium_requests:
            # Without reported usage, count one request per prompt (a lower bound).
            premium, estimated = 1.0, True
        self.store.add_usage(task_id, wall_seconds=seconds, premium_requests=premium)
        self.journal.append(
            "cost",
            task=task_id,
            run=run_id,
            wall_seconds=round(seconds, 3),
            premium_requests=premium,
            estimated=estimated,
            reported=result.cost,
        )

    # -- escalation, handoff, state helpers -----------------------------------

    def _escalate(
        self, task: Task, reason: str, next_step: str, outcome: Outcome, run_id: int | None = None
    ) -> Outcome:
        self._transition(task, Status.NEEDS_HUMAN, run=run_id, reason=reason)
        where = (
            f" Handoff: `.porter/runs/{run_id}/handoff.md` on the porter host." if run_id else ""
        )
        tree = self._worktree_of(task)
        if tree is not None:
            where += f" Unfinished work stays in the worktree `{self._rel(tree)}`."

        comment = (
            f"porter stopped working on this task.\n\n**Reason:** {reason}\n\n"
            f"**Next step:** {next_step}.{where}\n\n"
            f"To retry, remove the `{self.cfg.policy.escalation_label}` label or run "
            f"`porter resume {task.id}`."
        )
        notes = self.source.escalate(task, self.cfg.policy.escalation_label, comment, self.dry_run)
        self.journal.append(
            "escalation", task=task.id, run=run_id, reason=reason, next_step=next_step, notes=notes
        )
        # Escalations are worth remembering; routine successful runs are not.
        self.memory.remember(
            f"Escalation: {task.title}",
            f"Task {task.id}.\n\nReason: {reason}\n\nNext step: {next_step}",
        )
        self.out(f"needs human: {reason}")
        for n in notes:
            self.out(f"  {n}")
        outcome.status = Status.NEEDS_HUMAN
        outcome.reason = reason
        return outcome

    def _handoff(
        self, task: Task, run_id: int, done: list[str], next_step: str, blockers: list[str]
    ) -> Path:
        h = Handoff(task.id, run_id, _goal(task), done[-20:], next_step, blockers)
        path = h.write(self.runs_dir / str(run_id))
        self.store.update(task.id, last_handoff=str(path))
        self.journal.append(
            "decision",
            task=task.id,
            run=run_id,
            action="handoff",
            path=str(path),
            next_step=next_step,
        )
        return path

    def _transition(self, task: Task, new: Status, run: int | None = None, **fields) -> None:
        old = self._row(task.id).status
        self.store.set_status(task.id, new, **fields)
        self.journal.append("state", task=task.id, run=run, **{"from": str(old), "to": str(new)})
        self.out(f"task {task.id}: {old} -> {new}")

    def _worktree_of(self, task: Task) -> Path | None:
        """The task's existing worktree, if porter made one (never in a dry run)."""
        row = self.store.get(task.id)
        if self.dry_run or row is None or not row.branch:
            return None
        path = self.git.worktree_path(row.branch)
        return path if path.is_dir() else None

    def _where(self, task: Task) -> str:
        tree = self._worktree_of(task)
        branch = self._row(task.id).branch
        return f"worktree {tree} (branch {branch})" if tree else f"branch {branch}"

    def _rel(self, path: Path) -> str:
        try:
            return str(path.relative_to(self.cfg.workdir))
        except ValueError:
            return str(path)

    def _row(self, task_id: str) -> TaskRow:
        row = self.store.get(task_id)
        if row is None:
            raise PorterError(f"unknown task {task_id}")
        return row

    def _pick(self, task_id: str | None, listed: set[str] | None) -> TaskRow | None:
        if task_id:
            row = self.store.get(task_id)
            if row is None:
                raise PorterError(f"unknown task {task_id}")
            if row.status != Status.QUEUED:
                raise PorterError(f"task {task_id} is {row.status}; use `porter resume {task_id}`")
            return row
        queued = self.store.list({Status.QUEUED})
        # Finish work already in flight (e.g. a recovered interrupted run) before new work.
        started = [r for r in queued if self.store.runs(r.task.id)]
        if started:
            return started[0]
        for row in queued:
            # Only start tasks the source still lists (closed issues drop out).
            if listed is None or row.task.id in listed:
                return row
        return None

    def _budget_exceeded(self, row: TaskRow) -> str:
        b = self.cfg.budgets
        if row.attempts >= b.attempts:
            return f"attempt budget used ({row.attempts}/{b.attempts})"
        if row.wall_seconds >= b.wall_seconds:
            return f"wall-time budget used ({row.wall_seconds:.0f}s/{b.wall_seconds}s)"
        if b.premium_requests and row.premium_requests >= b.premium_requests:
            return f"premium-request budget used ({row.premium_requests:g}/{b.premium_requests:g})"
        return ""

    def _pr_body(self, task: Task, run_id: int, result: ExecResult, report: VerifyReport) -> str:
        checks = "\n".join(f"- [x] `{r.command}` passed" for r in report.results)
        summary = result.summary.strip()
        summary = summary.replace("PORTER-STATUS: done", "").strip()
        row = self._row(task.id)
        return (
            f"## Summary\n\n{task.title}\n\n{summary[:3000]}\n\n{self.source.pr_link(task)}\n\n"
            "## Checklist\n\n"
            "- [x] Agent reported the task done\n"
            f"{checks + chr(10) if checks else ''}"
            f"- [x] Work is on `{row.branch}`; the default branch was not touched\n"
            "- [ ] Reviewed by a human\n"
            "- [ ] Merged by a human (porter never merges)\n\n"
            f"<sub>Opened by porter · task {task.id} · run {run_id} · "
            f"{row.attempts} attempt(s) · executor {self.executor.name}</sub>\n"
        )

    def _print_pr(self, pr: PullRequest) -> None:
        verb = "would open" if self.dry_run else "opened"
        self.out(f"\n--- PR {verb}: {pr.branch} -> {pr.base} ---")
        if self.dry_run:
            for c in pr.commands:
                self.out(f"$ {c}")
        self.out(f"title: {pr.title}\n\n{pr.body}")
        if not self.dry_run:
            self.out(f"url: {pr.url}")
        self.out("---")

    def _warn(self, msg: str) -> None:
        self.journal.append("warning", message=msg)
        self.out(f"warning: {msg}")

    @contextlib.contextmanager
    def _lock(self):
        path = self.state_dir / "lock"
        try:
            fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        except FileExistsError:
            pid = _read_pid(path)
            if pid and _alive(pid):
                raise BusyError(f"another porter process (pid {pid}) holds {path}") from None
            path.unlink(missing_ok=True)  # stale lock from a killed process
            self.journal.append("warning", message=f"removed stale lock from pid {pid}")
            fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        os.write(fd, str(os.getpid()).encode())
        os.close(fd)
        try:
            yield
        finally:
            path.unlink(missing_ok=True)

    def close(self) -> None:
        self.store.close()


def _goal(task: Task) -> str:
    return f"{task.title}" + (f" ({task.url})" if task.url else f" (task {task.id})")


def _one_line(text: str, n: int = 200) -> str:
    line = " ".join(text.split())
    return line if len(line) <= n else line[: n - 1] + "…"


def _executor_reason(result: ExecResult, task_id: str) -> tuple[str, str]:
    if result.outcome == "blocked":
        return (
            f"agent is blocked: {result.blocker}",
            f"resolve the blocker, then `porter resume {task_id}`",
        )
    if result.outcome == "stopped":
        return (
            f"executor stopped: {_one_line(result.summary)}",
            f"raise budgets.wall_seconds or split the task, then `porter resume {task_id}`",
        )
    return (
        f"executor failed: {_one_line(result.summary)}",
        "check the executor install and auth, then resume",
    )


def _read_pid(path: Path) -> int:
    try:
        return int(path.read_text().strip() or 0)
    except (OSError, ValueError):
        return 0


def _alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True
