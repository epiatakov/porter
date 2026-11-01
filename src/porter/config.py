"""Loading ``porter.toml``.

Everything deployment-specific (hosts, repos, labels, commands, credentials
handling) comes from here or from plugins. The core has no hardcoded org rules.
"""

from __future__ import annotations

import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

DEFAULT_CONFIG_NAME = "porter.toml"


class ConfigError(ValueError):
    pass


@dataclass
class RepoConfig:
    path: Path = Path(".")
    github: str = ""  # "owner/name"; empty lets `gh` infer from the git remote
    default_branch: str = "main"
    remote: str = "origin"
    branch_prefix: str = "porter/"


@dataclass
class TasksConfig:
    source: str = "local"  # "local" | "github" | "module:attr" plugin
    file: str = "tasks.toml"
    label: str = "agent-ready"
    limit: int = 50
    options: dict[str, Any] = field(default_factory=dict)


@dataclass
class ExecutorConfig:
    kind: str = "fake"  # "fake" | "copilot-cli" | "module:attr" plugin
    options: dict[str, Any] = field(default_factory=dict)


@dataclass
class VerifyConfig:
    commands: list[str] = field(default_factory=list)
    timeout: int = 900


@dataclass
class PolicyConfig:
    open_pr: bool = True
    push_default_branch: bool = False
    merge: bool = False
    escalation_label: str = "needs-human"
    verify_feedback_rounds: int = 1


@dataclass
class BudgetConfig:
    wall_seconds: int = 1800  # per task, summed over all runs
    attempts: int = 3  # executor runs per task
    premium_requests: float = 0  # 0 = no cap


@dataclass
class MemoryConfig:
    backend: str = "file"  # "file" | "cairn" | "none"
    limit: int = 5
    cairn_binary: str = "cairn"
    cairn_args: list[str] = field(default_factory=list)


@dataclass
class Config:
    root: Path
    repo: RepoConfig = field(default_factory=RepoConfig)
    tasks: TasksConfig = field(default_factory=TasksConfig)
    executor: ExecutorConfig = field(default_factory=ExecutorConfig)
    verify: VerifyConfig = field(default_factory=VerifyConfig)
    policy: PolicyConfig = field(default_factory=PolicyConfig)
    budgets: BudgetConfig = field(default_factory=BudgetConfig)
    memory: MemoryConfig = field(default_factory=MemoryConfig)
    state_dir_name: str = ".porter"
    plugin_paths: list[Path] = field(default_factory=list)
    source_path: Path | None = None

    @property
    def workdir(self) -> Path:
        return (self.root / self.repo.path).resolve()

    def state_dir(self, dry_run: bool = False) -> Path:
        base = self.workdir / self.state_dir_name
        return base / "dry-run" if dry_run else base


_SECTIONS = {
    "repo",
    "tasks",
    "executor",
    "verify",
    "policy",
    "budgets",
    "memory",
    "state",
    "plugins",
}


def _take(section: str, data: dict[str, Any], allowed: set[str]) -> dict[str, Any]:
    unknown = set(data) - allowed
    if unknown:
        raise ConfigError(f"[{section}] unknown keys: {', '.join(sorted(unknown))}")
    return data


def load_config(path: str | Path | None = None, cwd: Path | None = None) -> Config:
    """Load config from ``path`` or ``./porter.toml``. A missing default file is an error."""
    cwd = cwd or Path.cwd()
    cfg_path = Path(path) if path else cwd / DEFAULT_CONFIG_NAME
    if not cfg_path.is_absolute():
        cfg_path = cwd / cfg_path
    if not cfg_path.exists():
        raise ConfigError(f"config not found: {cfg_path} (run `porter init`)")
    with cfg_path.open("rb") as fh:
        try:
            raw = tomllib.load(fh)
        except tomllib.TOMLDecodeError as exc:
            raise ConfigError(f"{cfg_path}: {exc}") from exc
    cfg = parse_config(raw, root=cfg_path.parent)
    cfg.source_path = cfg_path
    return cfg


def parse_config(raw: dict[str, Any], root: Path) -> Config:
    unknown = set(raw) - _SECTIONS
    if unknown:
        raise ConfigError(f"unknown config sections: {', '.join(sorted(unknown))}")
    cfg = Config(root=root.resolve())

    repo = _take("repo", dict(raw.get("repo", {})), set(RepoConfig.__dataclass_fields__))
    if "path" in repo:
        repo["path"] = Path(repo["path"])
    cfg.repo = RepoConfig(**repo)

    tasks = dict(raw.get("tasks", {}))
    known = set(TasksConfig.__dataclass_fields__) - {"options"}
    cfg.tasks = TasksConfig(
        **{k: v for k, v in tasks.items() if k in known},
        options={k: v for k, v in tasks.items() if k not in known},
    )

    ex = dict(raw.get("executor", {}))
    kind = ex.pop("kind", "fake")
    # Executor-specific options live in [executor.<kind>] (e.g. [executor.copilot]).
    options: dict[str, Any] = {}
    for key, value in ex.items():
        if isinstance(value, dict):
            if _executor_key(kind) == key:
                options.update(value)
        else:
            options[key] = value
    cfg.executor = ExecutorConfig(kind=kind, options=options)

    verify = _take("verify", dict(raw.get("verify", {})), {"commands", "timeout"})
    cfg.verify = VerifyConfig(**verify)

    policy = _take("policy", dict(raw.get("policy", {})), set(PolicyConfig.__dataclass_fields__))
    cfg.policy = PolicyConfig(**policy)
    if cfg.policy.push_default_branch:
        raise ConfigError(
            "[policy] push_default_branch = true is not supported; porter only pushes "
            "its own branches"
        )
    if cfg.policy.merge:
        raise ConfigError("[policy] merge = true is not supported; merging stays with humans")

    budgets = _take("budgets", dict(raw.get("budgets", {})), set(BudgetConfig.__dataclass_fields__))
    cfg.budgets = BudgetConfig(**budgets)
    if cfg.budgets.attempts < 1:
        raise ConfigError("[budgets] attempts must be >= 1")

    memory = _take("memory", dict(raw.get("memory", {})), set(MemoryConfig.__dataclass_fields__))
    cfg.memory = MemoryConfig(**memory)

    state = _take("state", dict(raw.get("state", {})), {"dir"})
    cfg.state_dir_name = state.get("dir", ".porter")

    plugins = _take("plugins", dict(raw.get("plugins", {})), {"paths"})
    cfg.plugin_paths = [(cfg.root / p).resolve() for p in plugins.get("paths", [])]
    return cfg


def _executor_key(kind: str) -> str:
    """``copilot-cli`` -> ``copilot``; ``fake`` -> ``fake``; plugins use their full kind."""
    return {"copilot-cli": "copilot"}.get(kind, kind)
