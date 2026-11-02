# porter

An autonomous work runner for coding agents.

porter picks the next task, drives a coding agent you already pay for (GitHub
Copilot CLI first; Codex and Claude Code planned), checks the result with your own
tests and linters, opens a pull request, and keeps durable work state so it can
pick up again after a stop.

## Why

- **Uses tools you already pay for.** porter does not ship a model. It drives an
  agent CLI you already have, through a small executor interface.
- **State survives stops.** Tasks, runs, and attempts live in SQLite. Every run
  writes a handoff note (goal, done so far, next step, blockers). A killed run is
  found on the next start and resumed from its handoff.
- **Explicit autonomy rules.** Opening PRs is allowed. Pushing to the default branch
  and merging are not. Each task has budgets (wall time, attempts, optional
  premium-request cap). When a task is blocked or over budget, porter labels it
  `needs-human`, leaves one short comment, and stops working on that task.
- **Executor-agnostic.** Executors declare what they can do (resume, tools,
  structured output, cost reporting). The core reads those declarations and does
  not assume that all agents work the same way.

## 60-second quickstart

Needs Python 3.11+ and nothing else (no runtime dependencies).

```bash
git clone https://github.com/epiatakov/porter && cd porter
python -m pip install -e .
porter --config examples/demo/porter.toml run --once --dry-run
```

That uses a local task file and the scripted `FakeExecutor`. It walks one task
through `queued -> running -> verifying -> pr_open -> done`, writes the handoff and
the journal under `examples/demo/.porter/dry-run/`, and prints the PR it would open.
A dry run makes no git, PR, or issue changes and never launches a real agent.

In your own repository:

```bash
porter init                     # porter.toml + tasks.toml (local tasks, fake executor)
porter tasks                    # what would be worked on
porter run --once --dry-run     # preview one task end to end
porter status && porter log     # state and journal
```

For real work on GitHub issues with Copilot CLI (needs `gh` and `copilot`, both
logged in):

```bash
porter init --source github --executor copilot-cli --force
porter run --once               # one issue labeled agent-ready -> one PR
```

## Where porter works

porter never switches branches, stages, or commits in your checkout. Each task
gets its own Git worktree on its own branch, so you can keep working in the same
repository while porter runs:

- A new task branch (`porter/<id>-<slug>`) starts from `repo.default_branch` in
  `.porter/worktrees/<id>-<slug>/`. The agent, the verification commands, change
  detection, the commit, and the push all run there. Your branch, index, edits,
  and untracked files are left as they are.
- State stays shared: `.porter/state.db`, the journal, handoffs, and memory.
- A retry or a restart after a crash reuses the same branch and worktree,
  including unfinished edits.
- Unfinished work stays in the worktree for inspection. `porter status` prints its
  path; `cd` there and use `git status` / `git diff` as usual. After the PR is
  merged, porter removes the worktree if it is clean (git refuses otherwise).
- porter stops with `needs-human` and a specific instruction, and changes nothing,
  if the task branch is checked out in your checkout or in a worktree it does not
  own, or if something else is in its worktree's place. For example: switch your
  checkout off `porter/7-...`, then run `porter resume 7`.
- A dry run creates no worktree or branch and stages nothing.

Verification runs inside the worktree. If your tests import an installed copy of
the project (for example an editable install), point them at the working
directory, e.g. pytest's `pythonpath = ["src"]`, so they test the task's code.

## Commands

| Command | What it does |
|---|---|
| `porter init` | Write a starter `porter.toml` (and `tasks.toml` for local tasks). |
| `porter tasks` | List tasks from the source with their porter status. |
| `porter run --once [--task ID] [--dry-run]` | Handle one task: run, verify, open PR or escalate. |
| `porter resume <task>` | Requeue an escalated task with a fresh budget and run it. |
| `porter status` | Show task states, attempts, PRs, last handoff, and the task worktree. |
| `porter log [--task ID]` | Print the journal. |

`--config PATH` overrides `./porter.toml` for every command.

## Design principles

1. **The core owns the work; executors own the agent.** Queue, state, policy,
   verification, and the journal live in the core. An executor only starts, streams,
   stops, and reports.
2. **Declare capabilities, never assume them.** An executor that cannot report
   cost is not trusted to enforce a cost cap; the core counts conservatively.
3. **Verify with the project's own checks.** The agent saying "done" is not
   enough. Configured commands must pass.
4. **Stop and escalate when blocked.** No improvised workarounds. One short comment
   with the reason and the next step, then move on.
5. **Everything is resumable and auditable.** SQLite state, a handoff per run, and
   an append-only JSONL journal of every decision and cost.
6. **No org-specific anything in the core.** Hosts, repos, labels, commands, and
   credentials come from config or plugins. See [docs/deployment.md](docs/deployment.md).
7. **Stdlib only at runtime.** `tomllib`, `sqlite3`, `subprocess`.

## Status

porter is early (v0.1). Honest state of things:

| Area | Status |
|---|---|
| Config, SQLite state, handoffs, journal | Built |
| Task sources: local file, GitHub issues (`gh`) | Built |
| Autonomy policy, budgets, `needs-human` escalation | Built |
| Verification with one feedback round | Built |
| Git branch / commit / push / `gh pr create`, in a per-task worktree | Built |
| File memory backend; optional `cairn` recall | Built (cairn: basic) |
| `FakeExecutor` | Built |
| `CopilotCliExecutor` | Built; unit-tested against a fake process; real smoke test is manual |
| Codex and Claude Code executors | Planned |
| Follow-up loop for CI failures and review comments | Planned |
| Scheduler / daemon (`porter run` without `--once`) | Planned |
| Worktree isolation per task | Built |

See [ROADMAP.md](ROADMAP.md), [docs/design.md](docs/design.md),
[docs/executors.md](docs/executors.md), and [docs/dogfood.md](docs/dogfood.md).

## License

MIT. See [LICENSE](LICENSE) and [CLEAN_ROOM.md](CLEAN_ROOM.md).
