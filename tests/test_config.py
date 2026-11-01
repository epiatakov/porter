import pytest

from porter.config import ConfigError, load_config, parse_config


def test_defaults(tmp_path):
    cfg = parse_config({}, tmp_path)
    assert cfg.executor.kind == "fake"
    assert cfg.policy.open_pr and not cfg.policy.merge
    assert cfg.state_dir() == tmp_path.resolve() / ".porter"
    assert cfg.state_dir(dry_run=True).name == "dry-run"


def test_executor_section_options(tmp_path):
    raw = {
        "executor": {
            "kind": "copilot-cli",
            "copilot": {"model": "auto", "deny_tools": ["x"]},
            "fake": {"script": []},
        }
    }
    cfg = parse_config(raw, tmp_path)
    assert cfg.executor.options == {"model": "auto", "deny_tools": ["x"]}


@pytest.mark.parametrize(
    "raw",
    [
        {"policy": {"push_default_branch": True}},
        {"policy": {"merge": True}},
        {"budgets": {"attempts": 0}},
        {"nope": {}},
        {"verify": {"cmds": []}},
    ],
)
def test_rejects_unsafe_or_unknown(tmp_path, raw):
    with pytest.raises(ConfigError):
        parse_config(raw, tmp_path)


def test_missing_file(tmp_path):
    with pytest.raises(ConfigError, match="porter init"):
        load_config(cwd=tmp_path)


def test_repo_example_config_parses():
    from pathlib import Path

    root = Path(__file__).resolve().parents[1]
    for name in ("porter.toml", "examples/demo/porter.toml"):
        cfg = load_config(root / name)
        assert cfg.budgets.attempts >= 1
