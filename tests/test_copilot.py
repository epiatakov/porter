"""CopilotCliExecutor against a fake ``copilot`` process (no auth needed)."""

from __future__ import annotations

import sys
import textwrap
import time
from pathlib import Path

import pytest

from porter.executors.base import RunContext, parse_status_line
from porter.executors.copilot import CopilotCliExecutor
from porter.models import Task

FAKE = textwrap.dedent(
    """
    import json, os, sys, time
    args = sys.argv[1:]
    mode = os.environ["FAKE_MODE"]
    open(os.environ["FAKE_ARGS"], "w").write(json.dumps(args))
    if mode == "done":
        print("Reading files...")
        print("Edited src/app.py")
        print("PORTER-STATUS: done")
    elif mode == "blocked":
        print("PORTER-STATUS: blocked: no access to the staging API")
    elif mode == "crash":
        print("error: not authenticated", file=sys.stderr)
        sys.exit(1)
    elif mode == "json":
        print(json.dumps({"type": "assistant.message", "data": {"content": "working"}}))
        print("not json")
        print(json.dumps({"type": "assistant.message", "content": "PORTER-STATUS: done"}))
    elif mode == "hang":
        print("thinking", flush=True)
        time.sleep(30)
    """
)


@pytest.fixture
def fake_copilot(tmp_path, monkeypatch):
    script = tmp_path / "fake_copilot.py"
    script.write_text(FAKE)
    args_file = tmp_path / "args.json"
    monkeypatch.setenv("FAKE_ARGS", str(args_file))

    def make(mode: str, **kw) -> tuple[CopilotCliExecutor, Path]:
        monkeypatch.setenv("FAKE_MODE", mode)
        return CopilotCliExecutor(binary=[sys.executable, str(script)], **kw), args_file

    return make


def ctx(tmp_path, deadline=None) -> RunContext:
    return RunContext(1, 1, tmp_path, tmp_path / "runs" / "1", "do the thing", deadline=deadline)


def drive(ex, context):
    run = ex.start(Task("1", "t"), context)
    events = list(ex.events(run))
    return events, ex.result(run)


def test_capabilities_are_conservative():
    caps = CopilotCliExecutor.capabilities
    assert caps.tools is True
    assert (caps.resume, caps.structured_output, caps.cost_reporting) == (False, False, False)


def test_command_flags(tmp_path):
    ex = CopilotCliExecutor(
        model="auto",
        allow_tools=["write"],
        deny_tools=["shell(rm)"],
        output_format="json",
        extra_args=["--log-level", "debug"],
    )
    cmd = ex.command(ctx(tmp_path))
    assert cmd[:3] == ["copilot", "-p", "do the thing"]
    for flag in ("--allow-all-tools", "--model", "--allow-tool", "--deny-tool", "--log-dir"):
        assert flag in cmd
    assert cmd[cmd.index("--output-format") + 1] == "json"
    assert cmd[-2:] == ["--log-level", "debug"]
    assert "<prompt: 12 chars>" in ex.describe(ctx(tmp_path))
    with pytest.raises(ValueError):
        CopilotCliExecutor(output_format="stream")


def test_model_and_effort_per_run(tmp_path):
    assert CopilotCliExecutor.capabilities.model_per_run is True
    ex = CopilotCliExecutor(model="base", effort="low")
    cmd = ex.command(ctx(tmp_path))
    assert cmd[cmd.index("--model") + 1] == "base"
    assert cmd[cmd.index("--reasoning-effort") + 1] == "low"
    stage = ctx(tmp_path)
    stage.model, stage.effort = "strong", "high"
    cmd = ex.command(stage)
    assert cmd[cmd.index("--model") + 1] == "strong"
    assert cmd[cmd.index("--reasoning-effort") + 1] == "high"
    assert "--reasoning-effort" not in CopilotCliExecutor().command(ctx(tmp_path))


def test_completed(fake_copilot, tmp_path):
    ex, args_file = fake_copilot("done")
    events, result = drive(ex, ctx(tmp_path))
    assert result.outcome == "completed"
    assert "Edited src/app.py" in result.summary
    assert [e.text for e in events][-1] == "PORTER-STATUS: done"
    assert "-p" in args_file.read_text()
    assert (tmp_path / "runs" / "1" / "copilot-output.log").read_text().count("\n") == 3


def test_blocked(fake_copilot, tmp_path):
    ex, _ = fake_copilot("blocked")
    _, result = drive(ex, ctx(tmp_path))
    assert result.outcome == "blocked"
    assert result.blocker == "no access to the staging API"


def test_nonzero_exit_is_failed(fake_copilot, tmp_path):
    ex, _ = fake_copilot("crash")
    _, result = drive(ex, ctx(tmp_path))
    assert result.outcome == "failed"
    assert "exited with code 1" in result.summary and "not authenticated" in result.summary


def test_json_lines(fake_copilot, tmp_path):
    ex, _ = fake_copilot("json", output_format="json")
    events, result = drive(ex, ctx(tmp_path))
    assert result.outcome == "completed"
    assert events[0].data["type"] == "assistant.message" and events[0].text == "working"
    assert events[1].kind == "log"


def test_deadline_stops_process(fake_copilot, tmp_path):
    ex, _ = fake_copilot("hang", stop_grace_seconds=2)
    t0 = time.monotonic()
    events, result = drive(ex, ctx(tmp_path, deadline=time.monotonic() + 1.0))
    assert time.monotonic() - t0 < 10
    assert result.outcome == "stopped"
    assert "wall-time" in result.summary
    assert events[-1].kind == "error"


def test_missing_binary_fails_cleanly(repo, make_config):
    from porter.runner import Porter

    cfg = make_config(repo, script="[]")
    ex = CopilotCliExecutor(binary="copilot-not-installed-here")
    porter = Porter(cfg, executor=ex, out=lambda *_: None)
    outcome = porter.run_once(task_id="7")
    assert outcome.status == "needs_human"
    assert "could not start" in outcome.reason


def test_dry_run_never_launches_real_agent(tmp_path, make_config):
    from porter.runner import Porter

    cfg = make_config(tmp_path, script="[]")
    lines = []
    ex = CopilotCliExecutor(binary="copilot-not-installed-here")
    porter = Porter(cfg, executor=ex, dry_run=True, out=lines.append)
    assert porter.run_once(task_id="7").status == "done"
    assert any("would launch" in ln and "copilot-not-installed-here -p" in ln for ln in lines)


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("x\nPORTER-STATUS: done\n", ("done", "")),
        ("PORTER-STATUS: done\nPORTER-STATUS: blocked: later wins", ("blocked", "later wins")),
        ("no status", None),
    ],
)
def test_parse_status_line(text, expected):
    assert parse_status_line(text) == expected
