import json

from porter.memory import CairnMemory, FileMemory
from porter.proc import RecordingShell, Result, Shell


def test_file_memory_recall(tmp_path):
    mem = FileMemory(tmp_path / "memory")
    assert mem.recall("anything") == []
    mem.remember("Flaky sqlite test", "Use tmp_path; never a shared db file.")
    mem.remember("Release notes", "Write them in CHANGELOG.")
    hits = mem.recall("fix the sqlite test")
    assert hits[0].title == "Flaky sqlite test"


def test_cairn_missing_binary_does_not_fail(tmp_path):
    warnings = []
    fallback = FileMemory(tmp_path / "m")
    fallback.remember("local note", "about widgets")
    mem = CairnMemory(Shell(), fallback, binary="cairn-definitely-missing", warn=warnings.append)
    hits = mem.recall("widgets")
    assert [h.title for h in hits] == ["local note"]
    assert warnings and "unavailable" in warnings[0]


def test_cairn_json(tmp_path):
    out = json.dumps([{"title": "Note A", "path": "a.md", "snippet": "use gh"}])
    shell = RecordingShell({"cairn search --json": Result([], 0, out)})
    mem = CairnMemory(shell, FileMemory(tmp_path))
    (hit,) = mem.recall("gh")
    assert (hit.title, hit.text, hit.source) == ("Note A", "use gh", "a.md")
    assert shell.calls[0][:3] == ["cairn", "search", "--json"]
