import json

import pytest

from porter.models import Task
from porter.proc import CommandError, RecordingShell, Result
from porter.sources import GitHubIssues, LocalFile


def test_local_toml_sorted_by_priority(tmp_path):
    p = tmp_path / "t.toml"
    p.write_text(
        '[[task]]\nid="a"\ntitle="A"\nlabels=["p3"]\n'
        '[[task]]\nid="b"\ntitle="B"\nlabels=["p1"]\n'
        '[[task]]\nid="c"\ntitle="C"\ndone=true\n'
    )
    assert [t.id for t in LocalFile(p).list()] == ["b", "a"]


def test_local_json(tmp_path):
    p = tmp_path / "t.json"
    p.write_text(json.dumps([{"id": 1, "title": "One", "priority": 0}]))
    (task,) = LocalFile(p).list()
    assert task.id == "1" and task.priority == 0 and task.source == "local"


ISSUES = [
    {"number": 12, "title": "Later", "body": "", "labels": [{"name": "agent-ready"}], "url": "u12"},
    {"number": 5, "title": "Soon", "body": "b", "labels": [{"name": "p1"}], "url": "u5"},
    {"number": 9, "title": "Stuck", "body": "", "labels": [{"name": "needs-human"}], "url": ""},
]


def test_github_issues_list():
    shell = RecordingShell({"gh issue list": Result([], 0, json.dumps(ISSUES))})
    src = GitHubIssues(shell, repo="acme/widgets")
    tasks = src.list()
    assert [t.id for t in tasks] == ["5", "12"]  # p1 first; needs-human skipped
    cmd = shell.commands()[0]
    assert "--label agent-ready" in cmd and "--repo acme/widgets" in cmd
    assert "--json number,title,body,labels,url" in cmd


def test_github_issues_error():
    shell = RecordingShell({"gh issue list": Result([], 4, "", "auth required")})
    with pytest.raises(CommandError):
        GitHubIssues(shell).list()


def test_github_escalate_labels_and_comments():
    shell = RecordingShell()
    src = GitHubIssues(shell)
    notes = src.escalate(Task("5", "Soon"), "needs-human", "reason")
    cmds = shell.commands()
    assert any(c.startswith("gh issue edit 5 --add-label needs-human") for c in cmds)
    assert any(c.startswith("gh issue comment 5 --body reason") for c in cmds)
    assert all(n.startswith("ok") for n in notes)
    dry = RecordingShell()
    GitHubIssues(dry).escalate(Task("5", "Soon"), "needs-human", "r", dry_run=True)
    assert dry.calls == []
    assert src.pr_link(Task("5", "x", url="u5")) == "Closes #5 (u5)"
