from porter.cli import main


def test_init_then_dry_run(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    assert main(["init"]) == 0
    assert (tmp_path / "porter.toml").exists() and (tmp_path / "tasks.toml").exists()
    assert ".porter/" in (tmp_path / ".gitignore").read_text()
    assert main(["tasks"]) == 0
    assert "demo-1" in capsys.readouterr().out

    assert main(["run", "--once", "--dry-run"]) == 0
    out = capsys.readouterr().out
    for s in (
        "queued -> running",
        "running -> verifying",
        "verifying -> pr_open",
        "pr_open -> done",
        "PR would open",
        "## Checklist",
    ):
        assert s in out
    assert (tmp_path / ".porter" / "dry-run" / "runs" / "1" / "handoff.md").exists()
    assert not (tmp_path / "PORTER_DEMO.md").exists()

    # Repeatable: each dry run starts fresh.
    assert main(["run", "--once", "--dry-run"]) == 0
    assert "pr_open -> done" in capsys.readouterr().out

    assert main(["status", "--dry-run"]) == 0
    assert "done" in capsys.readouterr().out
    assert main(["log", "--dry-run", "--task", "demo-1", "--json"]) == 0
    assert '"kind": "state"' in capsys.readouterr().out


def test_run_requires_once(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    main(["init"])
    assert main(["run"]) == 2


def test_init_github_copilot(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    assert main(["init", "--source", "github", "--executor", "copilot-cli"]) == 0
    text = (tmp_path / "porter.toml").read_text()
    assert 'kind = "copilot-cli"' in text and 'source = "github"' in text
    assert not (tmp_path / "tasks.toml").exists()


def test_missing_config(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    assert main(["status"]) == 2
    assert "porter init" in capsys.readouterr().err
