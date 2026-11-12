import pytest

from porter.handoff import Handoff
from porter.journal import Journal
from porter.models import Status, Task, TransitionError
from porter.state import StateStore


def test_state_roundtrip(tmp_path):
    store = StateStore(tmp_path / "state.db")
    task = Task("1", "Title", "body", 2, ("p2",), "u", "local")
    row = store.upsert_task(task)
    assert row.status == Status.QUEUED and row.task == task
    store.set_status("1", Status.RUNNING)
    # Refreshing metadata never resets status.
    store.upsert_task(Task("1", "New title"))
    assert store.get("1").status == Status.RUNNING
    assert store.get("1").task.title == "New title"
    with pytest.raises(TransitionError):
        store.set_status("1", Status.DONE)


def test_runs_count_attempts(tmp_path):
    store = StateStore(tmp_path / "state.db")
    store.upsert_task(Task("1", "t"))
    r1 = store.start_run("1", "fake")
    r2 = store.start_run("1", "fake")
    assert (r1.attempt, r2.attempt) == (1, 2)
    assert store.get("1").attempts == 2
    store.finish_run(r1.id, "completed", "ok", "s1")
    assert [r.id for r in store.open_runs("1")] == [r2.id]


def test_state_survives_reopen(tmp_path):
    store = StateStore(tmp_path / "state.db")
    store.upsert_task(Task("1", "t"))
    store.set_status("1", Status.RUNNING)
    store.close()
    assert StateStore(tmp_path / "state.db").get("1").status == Status.RUNNING


def test_state_db_from_before_stages_gets_stage_column(tmp_path):
    import sqlite3

    from porter.state import SCHEMA

    old = SCHEMA.replace("    stage            TEXT NOT NULL DEFAULT '',\n", "")
    assert old != SCHEMA
    db = sqlite3.connect(tmp_path / "state.db")
    db.executescript(old)
    db.execute("INSERT INTO tasks (id, title, created_at, updated_at) VALUES ('1', 't', 0, 0)")
    db.commit()
    db.close()
    store = StateStore(tmp_path / "state.db")
    assert store.get("1").stage == ""
    store.update("1", stage="build")
    assert store.get("1").stage == "build"


def test_journal_append_and_filter(tmp_path):
    j = Journal(tmp_path / "j.jsonl")
    j.append("decision", task="1", action="pick")
    j.append("state", task="2", **{"from": "queued", "to": "running"})
    with (tmp_path / "j.jsonl").open("a") as fh:
        fh.write('{"torn": ')  # a killed writer
    assert [e["kind"] for e in j.read()] == ["decision", "state"]
    assert [e["task"] for e in j.read(task="2")] == ["2"]


def test_handoff_roundtrip(tmp_path):
    h = Handoff("7", 3, "Ship it", ["a", "b"], "do c", ["needs token"])
    path = h.write(tmp_path / "runs" / "3")
    assert path.name == "handoff.md"
    assert Handoff.load(path) == h
    empty = Handoff.parse(Handoff("7", 4, "g").to_markdown())
    assert empty.done == [] and empty.blockers == [] and empty.next_step == ""
