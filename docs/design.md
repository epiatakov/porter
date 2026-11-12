# Design

## Architecture

porter is an open core with pluggable executors and task sources.

```
            +------------------- core (porter) --------------------+
 task  ---> | queue & pick -> run loop -> verify -> git/PR         |
 source     |       |            |           |          |          |
 (local,    |   state.db     executor    commands    gh / git      |
  GitHub,   |  (SQLite)     interface   (tests,lint)               |
  plugin)   |       \            |           /                     |
            |        +--- journal.jsonl + runs/<id>/handoff.md ---+|
            |        policy & budgets  ·  memory (file | cairn)     |
            +------------------------------|------------------------+
                                           v
                     executor adapters: fake · copilot-cli · (codex, claude-code planned)
```

The core owns:

- **Task queue and pick.** Sources list tasks; the core upserts them into state and
  picks the most urgent queued task. Work already in flight (an interrupted run)
  goes first.
- **Work state and handoff.** `.porter/state.db` holds tasks and runs. Every run
  ends with `.porter/runs/<id>/handoff.md`.
- **Per-task worktrees.** Each task branch lives in its own linked worktree,
  `.porter/worktrees/<id>-<slug>/`, created from the default branch. The executor,
  verification, change detection, commit, and push run there; state, journal,
  handoffs, and memory stay in `.porter/`. The developer's checkout is never
  switched, staged, or committed.
- **Autonomy policy and budgets.** From `[policy]` and `[budgets]`.
- **Verification.** Configured commands run after the executor says it is done.
- **Journal.** `.porter/journal.jsonl`, append-only, one JSON object per decision,
  state change, executor event, verification result, git action, escalation, and
  cost entry.

Executors only drive an agent. Task sources only list tasks and report
escalations. Neither touches state directly.

## Executor interface and capability model

```python
class Executor:
    name: str
    capabilities: Capabilities   # resume, tools, structured_output, cost_reporting
    def start(self, task, context) -> Run
    def events(self, run) -> Iterator[ExecEvent]
    def stop(self, run) -> None
    def result(self, run) -> ExecResult   # completed | blocked | failed | stopped
```

`RunContext` carries the prompt, the workdir, the run dir, a deadline, and (only
when `capabilities.resume` is true) the previous session id.

The core reads the capabilities instead of assuming:

| Capability | If true | If false |
|---|---|---|
| `resume` | core passes the last session id | core resumes from the handoff text in the prompt |
| `tools` | agent edits files itself | (no such executor yet) |
| `structured_output` | reserved for schema-constrained results | core parses a `PORTER-STATUS:` line |
| `cost_reporting` | premium-request cap uses reported numbers | cap counts 1 per run, flagged `estimated` in the journal |
| `model_per_run` | a `[[stage]]` model and effort go to the run | stages run on the configured model; porter logs a warning |

The agent ends its final message with `PORTER-STATUS: done` or
`PORTER-STATUS: blocked: <reason>`. This is plain text on purpose: it works with
every executor, including ones without structured output.

## Stages

By default one task is one agent session. With `[[stage]]` tables, porter runs a task
as an ordered list of stages. Each stage is a fresh session with its own model,
reasoning effort and instructions. A strong model can plan and review while a cheaper
one writes the code, and no stage drags the full transcript of the one before it.

```toml
[[stage]]
name = "plan"
model = "<strong model>"
effort = "high"
instructions = "Read the code and write the plan into the handoff. Do not change code."

[[stage]]
name = "build"
model = "<cheaper model>"
verify = true
instructions = "Implement the plan from the handoff and make the checks pass."

[[stage]]
name = "review"
model = "<strong model>"
effort = "high"
instructions = "Review the diff against the plan. Fix real defects only."
```

- The next stage starts from the handoff of the stage before it, never from its
  transcript.
- `verify = true` runs the verify commands after that stage, with the usual feedback
  round. The last stage always verifies, because the PR depends on it.
- `blocked` or `failed` in any stage escalates as usual. `porter resume` continues
  the stage where the task stopped, not the first stage.
- The attempt budget applies per stage. The wall-time and premium-request budgets
  stay per task.
- The journal records a `stage` decision per run with the model and effort, so cost
  per stage can be read back later.

## Task state machine

```
          +----------------- recover interrupted run ------------------+
          v                                                            |
 queued --> running --> verifying --> pr_open --> done                 |
   ^           |  ^         |             |                           |
   |           |  +-- one --+             +--> failed (PR closed)     |
   |           |   feedback round                                     |
   |           +---------------+-----------+--> needs_human           |
   |                                            (blocked, budget,     |
   +--------- porter resume / label removed ---  verify, git error)   |
                                                                      |
 running / verifying left behind by a killed process -----------------+
```

- `running -> verifying` when the executor reports `completed`.
- `running -> running` when a stage without `verify` completes and the next stage
  starts (see Stages).
- `verifying -> running` once (configurable) with the failing output as feedback,
  within budget.
- `verifying -> pr_open` after commit, push, and `gh pr create`.
- `pr_open -> done` when a later run sees the PR merged; `-> failed` when closed
  unmerged. A dry run marks the would-be PR as merged so it shows every state.
- Any step can go to `needs_human`. porter labels the issue, posts one comment with
  the reason and the next step, writes the blocker into the handoff, and stops
  working on that task.
- `needs_human -> queued` by `porter resume <id>` or, for GitHub issues, when a
  human removes the `needs-human` label. Budgets reset; the run history stays.

### Resume after a stop

`porter run` takes a lock file with its pid. On start, a lock from a dead pid is
stale: the previous process was killed. Any task still in `running` or `verifying`
gets its open run marked `interrupted`, a handoff saying so, and goes back to
`queued`. It is picked before new work, on the same branch and in the same
worktree (with whatever the killed run left there), with the handoff in its prompt.
The killed attempt still counts toward the budget.

Before reusing a worktree, porter checks with `git worktree list` that it is
registered for the task branch, belongs to the same repository, and is on that
branch. If the branch is checked out in the developer's checkout or in a worktree
porter did not create, or an unknown directory sits at the worktree path, the task
goes to `needs_human` with a specific recovery step and nothing is moved or
deleted. Unfinished worktrees are kept; a worktree is removed (without `--force`)
only after its PR is merged.

## Why SQLite

- In the standard library; no service to run; one file under `.porter/`.
- Transactions make "count an attempt and create a run" atomic, so a crash never
  leaves a run that was not counted.
- WAL mode tolerates a reader (`porter status`) while a run writes.
- Easy to inspect by hand (`sqlite3 .porter/state.db`).

The journal is a separate append-only JSONL file because it is a log, not state: it
is never updated, can be shipped elsewhere line by line, and a torn last line from
a killed process is simply skipped by readers.

## Memory

`recall()` runs before a run and adds a few relevant notes to the prompt.
`remember()` runs only on escalations. The `file` backend keeps Markdown notes in
`.porter/memory/`. The `cairn` backend calls `cairn search --json <query>`; if the
binary is missing or fails, porter journals a warning and falls back to the file
backend. Memory is never a code dependency.

## Safety rails that do not depend on config

- porter writes only to branches under `branch_prefix`; the default branch is refused
  in code (`GitOps._guard`).
- `policy.push_default_branch = true` and `policy.merge = true` are rejected at
  config load.
- `.porter/` is never committed.
- Commits are made only in porter's own worktree, never in the developer's
  checkout (`GitOps.commit_push_pr` refuses).
- A dry run replaces any non-fake executor with the fake and prints the command it
  would have run. It creates no worktree or branch and stages nothing.

## Lessons

These shaped the design and are worth keeping in mind when changing it.

**(a) A gate that forces a report or capture on every turn must skip trivial
turns, or agents learn to game it.** If every turn must produce a memory note or a
status report, agents produce filler to satisfy the gate, and the signal drowns.
porter captures memory only on escalations, and the handoff is written by the core
from facts (outcome, verification, PR), not demanded from the agent on every step.

**(b) An agent must stop and escalate when blocked, not invent a new workaround.**
Unrequested workarounds (skipping a test, stubbing a service, widening a
permission) look like progress and cost more later. The prompt tells the agent to
report `blocked`, and the core turns that into one clear `needs-human` comment with
a next step instead of retrying.

**(c) Verification is part of the loop, not an afterthought.** "Done" from the agent
is a claim. One feedback round with the real failure output fixes most slips; more
rounds mostly burn budget, so the default is one.
