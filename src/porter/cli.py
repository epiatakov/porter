"""Command line: porter init | tasks | run | resume | status | log."""

from __future__ import annotations

import argparse
import json
import shutil
import sys
import time
from pathlib import Path

from . import __version__
from .config import DEFAULT_CONFIG_NAME, Config, ConfigError, load_config
from .journal import Journal
from .runner import Porter, PorterError, build_source
from .state import StateStore
from .templates import config_template, tasks_template


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="porter", description="Autonomous work runner for coding agents."
    )
    parser.add_argument("--config", help=f"config file (default ./{DEFAULT_CONFIG_NAME})")
    parser.add_argument("--version", action="version", version=f"porter {__version__}")
    sub = parser.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("init", help="write a starter porter.toml in the current directory")
    p.add_argument("--source", choices=["local", "github"], default="local")
    p.add_argument("--executor", choices=["fake", "copilot-cli"], default="fake")
    p.add_argument("--force", action="store_true", help="overwrite existing files")

    sub.add_parser("tasks", help="list tasks from the source with their porter status")

    p = sub.add_parser("run", help="work on the next task")
    p.add_argument("--once", action="store_true", help="handle one task and exit")
    p.add_argument("--task", help="work on this task id instead of the next in the queue")
    p.add_argument(
        "--dry-run",
        action="store_true",
        help="no git, PR, or issue changes; real agents are not launched",
    )

    p = sub.add_parser("resume", help="requeue an escalated task with a fresh budget and run it")
    p.add_argument("task")
    p.add_argument("--dry-run", action="store_true")

    p = sub.add_parser("status", help="show task states")
    p.add_argument("--dry-run", action="store_true", help="show dry-run state")

    p = sub.add_parser("log", help="print the journal")
    p.add_argument("--task")
    p.add_argument("-n", type=int, default=50, help="last N entries (0 = all)")
    p.add_argument("--json", action="store_true", help="raw JSON lines")
    p.add_argument("--dry-run", action="store_true", help="show dry-run journal")

    args = parser.parse_args(argv)
    try:
        if args.cmd == "init":
            return cmd_init(args)
        cfg = load_config(args.config)
        return {
            "tasks": cmd_tasks,
            "run": cmd_run,
            "resume": cmd_resume,
            "status": cmd_status,
            "log": cmd_log,
        }[args.cmd](cfg, args)
    except (ConfigError, PorterError, FileNotFoundError) as exc:
        print(f"porter: {exc}", file=sys.stderr)
        return 2


def cmd_init(args) -> int:
    cwd = Path.cwd()
    written = []
    cfg_path = Path(args.config) if args.config else cwd / DEFAULT_CONFIG_NAME
    files = {cfg_path: config_template(args.source, args.executor)}
    if args.source == "local":
        files[cfg_path.parent / "tasks.toml"] = tasks_template()
    for path, text in files.items():
        if path.exists() and not args.force:
            print(f"exists, skipped: {path}")
            continue
        path.write_text(text, encoding="utf-8")
        written.append(path)
    gitignore = cfg_path.parent / ".gitignore"
    lines = gitignore.read_text().splitlines() if gitignore.exists() else []
    if ".porter/" not in lines:
        with gitignore.open("a", encoding="utf-8") as fh:
            fh.write(("\n" if lines and lines[-1] else "") + ".porter/\n")
        written.append(gitignore)
    for p in written:
        print(f"wrote {p}")
    print("next: porter tasks && porter run --once --dry-run")
    return 0


def cmd_tasks(cfg: Config, args) -> int:
    from .proc import Shell

    tasks = build_source(cfg, Shell()).list()
    store = _store(cfg, False)
    for t in tasks:
        row = store.get(t.id) if store else None
        status = row.status if row else "new"
        print(f"{t.id:>8}  p{t.priority}  {status:<11}  {t.title}")
    if not tasks:
        print("no open tasks")
    return 0


def cmd_run(cfg: Config, args) -> int:
    if not args.once:
        print("porter: continuous mode is planned; use `porter run --once`", file=sys.stderr)
        return 2
    if args.dry_run:
        # Each dry run starts from a clean preview state so it can be repeated.
        shutil.rmtree(cfg.state_dir(dry_run=True), ignore_errors=True)
    porter = Porter(cfg, dry_run=args.dry_run, out=_say)
    try:
        outcome = porter.run_once(args.task)
    finally:
        porter.close()
    return _exit(outcome)


def cmd_resume(cfg: Config, args) -> int:
    porter = Porter(cfg, dry_run=args.dry_run, out=_say)
    try:
        outcome = porter.resume(args.task)
    finally:
        porter.close()
    return _exit(outcome)


def _say(text: str) -> None:
    """Print progress; a closed stdout (e.g. piped into `head`) must not abort a run."""
    try:
        print(text, flush=True)
    except BrokenPipeError:
        pass


def _exit(outcome) -> int:
    if outcome is None:
        return 0
    _say(f"result: task {outcome.task_id} is {outcome.status}")
    return 0 if outcome.status in ("pr_open", "done") else 3


def cmd_status(cfg: Config, args) -> int:
    store = _store(cfg, args.dry_run)
    rows = store.list() if store else []
    if not rows:
        print("no tasks in state yet")
        return 0
    print(f"{'task':>8}  {'status':<11} {'tries':>5} {'wall':>6}  branch / PR / reason")
    for r in rows:
        extra = r.pr_url or r.reason or r.branch
        print(f"{r.task.id:>8}  {r.status:<11} {r.attempts:>5} {r.wall_seconds:>5.0f}s  {extra}")
        if r.last_handoff:
            print(f"{'':>8}  handoff: {r.last_handoff}")
    return 0


def cmd_log(cfg: Config, args) -> int:
    journal = Journal(cfg.state_dir(args.dry_run) / "journal.jsonl")
    entries = list(journal.read(task=args.task))
    if args.n:
        entries = entries[-args.n :]
    for e in entries:
        if args.json:
            print(json.dumps(e, ensure_ascii=False))
            continue
        ts = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(e.get("ts", 0)))
        rest = {k: v for k, v in e.items() if k not in ("ts", "kind", "task", "run")}
        where = f"task {e['task']}" if "task" in e else ""
        if "run" in e and e["run"] is not None:
            where += f" run {e['run']}"
        print(f"{ts}  {e['kind']:<10} {where:<16} {json.dumps(rest, ensure_ascii=False)[:200]}")
    return 0


def _store(cfg: Config, dry_run: bool) -> StateStore | None:
    path = cfg.state_dir(dry_run) / "state.db"
    return StateStore(path) if path.exists() else None


if __name__ == "__main__":
    sys.exit(main())
