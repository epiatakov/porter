# Agent instructions for porter

This file is the canonical guide for any coding agent working in this repository.

## What this is

porter is an autonomous work runner for coding agents. It picks a task, drives an
agent CLI, verifies the result, and opens a PR. Read `docs/design.md` before
changing the core.

## Stack

- Python 3.11+, `src/` layout, package `porter` (distribution `porter-agent`).
- Runtime: standard library only (`tomllib`, `sqlite3`, `subprocess`, `argparse`).
  Do not add runtime dependencies.
- Dev: `pytest`, `ruff`.

## Commands

```bash
python -m pip install -e ".[dev]"
ruff check .
ruff format --check .
pytest -q
porter --config examples/demo/porter.toml run --once --dry-run
```

All four must pass before you finish.

## Layout

- `src/porter/runner.py` – the core loop (pick, run, verify, PR, escalate).
- `src/porter/state.py`, `journal.py`, `handoff.py` – durable state.
- `src/porter/executors/` – executor interface, `fake`, `copilot`.
- `src/porter/sources/` – task sources (`local_file`, `github_issues`).
- `src/porter/gitops.py`, `verify.py`, `memory.py`; `config.py` holds policy and budgets.
- `tests/` – pytest; `conftest.py` has git-repo and fake-`gh` fixtures.

## Conventions

- Small PRs with tests. One concern per PR. Add or update tests for every change.
- External commands go through `porter.proc.Shell` so tests can fake them.
- New executor features must be declared in `Capabilities`; the core must check
  the declaration instead of assuming.
- No hardcoded hosts, URLs, repos, labels, or credentials in the core. Put them in
  config or a plugin.
- Never push to the default branch, never merge. porter's own code must keep
  enforcing that.
- If you are blocked, stop and say why. Do not invent a workaround.
  End with `PORTER-STATUS: done` or `PORTER-STATUS: blocked: <reason>`.

## Clean-room rule

Write original code only. Use only public documentation and public patterns. Never
copy or reference code, documents, names, or file layouts from any employer or
other private source. See `CLEAN_ROOM.md`.
