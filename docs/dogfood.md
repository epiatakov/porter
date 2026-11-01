# Dogfood: porter works on porter

The first real user of porter is this repository. porter picks open issues labeled
`agent-ready` in `epiatakov/porter`, drives Copilot CLI on a `porter/<issue>-<slug>`
branch, runs `ruff` and `pytest`, and opens a PR to `main`. A human reviews and
merges.

## Setup

1. Install: `python -m pip install -e ".[dev]"`.
2. Log in: `gh auth login` and Copilot CLI (`copilot`, then `/login`, or set
   `COPILOT_GITHUB_TOKEN`). See `docs/executors.md`.
3. Labels (once): `agent-ready`, `needs-human`, `p1`, `p2`, `p3`.
   porter creates `needs-human` itself on first escalation.
4. Use the root `porter.toml` (GitHub source, Copilot executor, budgets of 30
   minutes, 3 attempts, and 5 premium requests per task).
5. File issues with the "Agent-ready task" template
   (`.github/ISSUE_TEMPLATE/agent-ready.yml`).

Run:

```bash
porter tasks
porter run --once --dry-run   # prints the copilot command and the PR it would open
porter run --once
porter status
```

Protect `main` on GitHub (require PR + review) as a second guard.

## Example issues

These are written out here as examples. They have **not** been created as real
issues.

### 1. `porter log --cost` summary (p2)

**Goal:** `porter log --cost` prints total wall time and premium requests per task,
from the journal's `cost` entries.

**Acceptance criteria:**
- [ ] New `--cost` flag on `porter log`; works with `--task` and `--dry-run`.
- [ ] Shows `estimated` totals separately from reported ones.
- [ ] Test in `tests/test_cli.py` using a journal written by a dry run.
- [ ] `ruff check .` and `pytest -q` pass.

**Pointers:** `src/porter/cli.py` (`cmd_log`), `src/porter/journal.py`.

### 2. Validate `[tasks]` keys for built-in sources (p3)

**Goal:** an unknown key in `[tasks]` (for example `lable = "x"`) is a config error
for the built-in `local` and `github` sources, while plugin sources still receive
extra keys.

**Acceptance criteria:**
- [ ] `parse_config` raises `ConfigError` naming the unknown key for built-in sources.
- [ ] Plugin sources (`module:attr`) still get extra keys as constructor arguments.
- [ ] Tests in `tests/test_config.py` for both cases.

**Pointers:** `src/porter/config.py`, `src/porter/runner.py` (`build_source`).

### 3. Show the last handoff in `porter status --task` (p2)

**Goal:** `porter status --task <id>` prints the task's row plus the full text of
its last handoff and the list of its runs (id, outcome, duration).

**Acceptance criteria:**
- [ ] New `--task` option on `porter status`.
- [ ] Unknown task id exits with code 2 and a clear message.
- [ ] Test covers a task with two runs (use the fake executor's verification
      feedback path).

**Pointers:** `src/porter/cli.py` (`cmd_status`), `src/porter/state.py` (`runs`).
