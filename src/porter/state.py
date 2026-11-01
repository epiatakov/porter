"""Durable work state in ``.porter/state.db`` (SQLite, stdlib only)."""

from __future__ import annotations

import json
import sqlite3
import time
from dataclasses import dataclass
from pathlib import Path

from .models import Status, Task, check_transition

SCHEMA = """
CREATE TABLE IF NOT EXISTS tasks (
    id               TEXT PRIMARY KEY,
    title            TEXT NOT NULL,
    body             TEXT NOT NULL DEFAULT '',
    priority         INTEGER NOT NULL DEFAULT 9,
    labels           TEXT NOT NULL DEFAULT '[]',
    url              TEXT NOT NULL DEFAULT '',
    source           TEXT NOT NULL DEFAULT '',
    status           TEXT NOT NULL DEFAULT 'queued',
    attempts         INTEGER NOT NULL DEFAULT 0,
    wall_seconds     REAL NOT NULL DEFAULT 0,
    premium_requests REAL NOT NULL DEFAULT 0,
    branch           TEXT NOT NULL DEFAULT '',
    pr_url           TEXT NOT NULL DEFAULT '',
    last_handoff     TEXT NOT NULL DEFAULT '',
    last_session     TEXT NOT NULL DEFAULT '',
    reason           TEXT NOT NULL DEFAULT '',
    created_at       REAL NOT NULL,
    updated_at       REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS runs (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    task_id     TEXT NOT NULL REFERENCES tasks(id),
    attempt     INTEGER NOT NULL,
    executor    TEXT NOT NULL,
    started_at  REAL NOT NULL,
    finished_at REAL,
    outcome     TEXT NOT NULL DEFAULT '',
    summary     TEXT NOT NULL DEFAULT '',
    session_id  TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS runs_task ON runs(task_id);
"""


@dataclass
class TaskRow:
    task: Task
    status: Status
    attempts: int
    wall_seconds: float
    premium_requests: float
    branch: str
    pr_url: str
    last_handoff: str
    last_session: str
    reason: str
    updated_at: float


@dataclass
class RunRow:
    id: int
    task_id: str
    attempt: int
    executor: str
    started_at: float
    finished_at: float | None
    outcome: str
    summary: str
    session_id: str


class StateStore:
    def __init__(self, path: Path, clock=time.time):
        path.parent.mkdir(parents=True, exist_ok=True)
        self.path = path
        self.clock = clock
        self.db = sqlite3.connect(path, isolation_level=None)  # autocommit; we use explicit txns
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.executescript(SCHEMA)

    def close(self) -> None:
        self.db.close()

    # -- tasks -----------------------------------------------------------------

    def upsert_task(self, task: Task) -> TaskRow:
        """Insert a new task as queued, or refresh its metadata (never its status)."""
        now = self.clock()
        self.db.execute(
            """
            INSERT INTO tasks (id, title, body, priority, labels, url, source,
                               status, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, 'queued', ?, ?)
            ON CONFLICT(id) DO UPDATE SET
                title=excluded.title, body=excluded.body, priority=excluded.priority,
                labels=excluded.labels, url=excluded.url, source=excluded.source
            """,
            (
                task.id,
                task.title,
                task.body,
                task.priority,
                json.dumps(list(task.labels)),
                task.url,
                task.source,
                now,
                now,
            ),
        )
        row = self.get(task.id)
        assert row is not None
        return row

    def get(self, task_id: str) -> TaskRow | None:
        r = self.db.execute("SELECT * FROM tasks WHERE id = ?", (task_id,)).fetchone()
        return _task_row(r) if r else None

    def list(self, statuses: set[Status] | None = None) -> list[TaskRow]:
        rows = [
            _task_row(r)
            for r in self.db.execute("SELECT * FROM tasks ORDER BY priority, created_at, id")
        ]
        return [r for r in rows if statuses is None or r.status in statuses]

    def set_status(self, task_id: str, new: Status, **fields) -> TaskRow:
        row = self.get(task_id)
        if row is None:
            raise KeyError(task_id)
        check_transition(row.status, new)
        self.update(task_id, status=new.value, **fields)
        updated = self.get(task_id)
        assert updated is not None
        return updated

    _UPDATABLE = frozenset(
        {
            "status",
            "attempts",
            "wall_seconds",
            "premium_requests",
            "branch",
            "pr_url",
            "last_handoff",
            "last_session",
            "reason",
        }
    )

    def update(self, task_id: str, **fields) -> None:
        bad = set(fields) - self._UPDATABLE
        if bad:
            raise ValueError(f"not updatable: {bad}")
        fields["updated_at"] = self.clock()
        cols = ", ".join(f"{k} = ?" for k in fields)
        self.db.execute(f"UPDATE tasks SET {cols} WHERE id = ?", (*fields.values(), task_id))

    def add_usage(self, task_id: str, wall_seconds: float = 0, premium_requests: float = 0) -> None:
        self.db.execute(
            "UPDATE tasks SET wall_seconds = wall_seconds + ?, "
            "premium_requests = premium_requests + ?, updated_at = ? WHERE id = ?",
            (wall_seconds, premium_requests, self.clock(), task_id),
        )

    # -- runs ------------------------------------------------------------------

    def start_run(self, task_id: str, executor: str) -> RunRow:
        """Record a new run and count the attempt atomically."""
        with self._txn():
            self.db.execute(
                "UPDATE tasks SET attempts = attempts + 1, updated_at = ? WHERE id = ?",
                (self.clock(), task_id),
            )
            # Run number over the task's whole history; ``tasks.attempts`` is the budget
            # counter and is reset when a human resumes the task.
            attempt = (
                self.db.execute(
                    "SELECT COUNT(*) FROM runs WHERE task_id = ?", (task_id,)
                ).fetchone()[0]
                + 1
            )
            cur = self.db.execute(
                "INSERT INTO runs (task_id, attempt, executor, started_at) VALUES (?, ?, ?, ?)",
                (task_id, attempt, executor, self.clock()),
            )
        run = self.get_run(int(cur.lastrowid))
        assert run is not None
        return run

    def finish_run(
        self, run_id: int, outcome: str, summary: str = "", session_id: str = ""
    ) -> None:
        self.db.execute(
            "UPDATE runs SET finished_at = ?, outcome = ?, summary = ?, session_id = ? "
            "WHERE id = ?",
            (self.clock(), outcome, summary, session_id, run_id),
        )

    def get_run(self, run_id: int) -> RunRow | None:
        r = self.db.execute("SELECT * FROM runs WHERE id = ?", (run_id,)).fetchone()
        return RunRow(**dict(r)) if r else None

    def runs(self, task_id: str) -> list[RunRow]:
        return [
            RunRow(**dict(r))
            for r in self.db.execute("SELECT * FROM runs WHERE task_id = ? ORDER BY id", (task_id,))
        ]

    def open_runs(self, task_id: str) -> list[RunRow]:
        return [r for r in self.runs(task_id) if r.finished_at is None]

    def _txn(self):
        db = self.db

        class _Txn:
            def __enter__(self):
                db.execute("BEGIN IMMEDIATE")

            def __exit__(self, exc_type, *_):
                db.execute("ROLLBACK" if exc_type else "COMMIT")

        return _Txn()


def _task_row(r: sqlite3.Row) -> TaskRow:
    task = Task(
        id=r["id"],
        title=r["title"],
        body=r["body"],
        priority=r["priority"],
        labels=tuple(json.loads(r["labels"])),
        url=r["url"],
        source=r["source"],
    )
    return TaskRow(
        task=task,
        status=Status(r["status"]),
        attempts=r["attempts"],
        wall_seconds=r["wall_seconds"],
        premium_requests=r["premium_requests"],
        branch=r["branch"],
        pr_url=r["pr_url"],
        last_handoff=r["last_handoff"],
        last_session=r["last_session"],
        reason=r["reason"],
        updated_at=r["updated_at"],
    )
