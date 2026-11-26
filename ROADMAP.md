# Roadmap

Kept short on purpose. Items move to the README status table when built.

Built since the first release, now in the README status table: a Git worktree per
task, stages with a model and effort per stage, and no new task when the task source
fails.

## Next

- **Codex adapter.** An executor for the Codex CLI's non-interactive mode, with
  capabilities set from its public docs (resume, structured output, usage) and the
  stage model and effort passed through where the CLI supports them.
- **Claude Code adapter.** An executor for Claude Code headless mode (`-p` with
  JSON / stream-JSON output), with resume by session id, the stage model and effort,
  and cost reporting where documented.
- **Follow-up loop for CI failures and review comments.** For tasks in `pr_open`,
  read failing checks and review comments through `gh`, feed them to the executor
  in the task's worktree on the same branch, and push a fix. The same budgets and
  escalation rules apply.
- **Scheduler / daemon.** `porter run` without `--once`: poll on an interval, with
  quiet hours and a per-day budget. Today, use cron or a CI schedule with
  `porter run --once`.
- **cairn memory backend hardening.** Pin the `cairn search --json` output contract,
  add a write path (capture lessons from escalations), timeouts, and a contract test.
- **Org deployment guide.** Expand `docs/deployment.md` with a worked example of a
  private plugin package (task source + executor) and a service-account setup.

## Proposals

Not scheduled. Open an issue to discuss them.

- **Finish mode without pull requests.** Some teams do not use pull requests. Today,
  with `[policy] open_pr = false`, a verified task escalates to `needs-human` as if
  it had failed. Proposal: a verified task keeps its change on its task branch and in
  its worktree, and waits in its own state for owner review. It gets no `needs-human`
  label and no escalation comment. The owner then accepts the work, and the task is
  done, or sends it back with feedback, and porter runs it again on the same branch.
  The rules "never push to the default branch, never merge" stay as they are.
- **Local status page.** A small read-only page, served with the standard library on
  localhost only. It lists the tasks that wait for the owner: `needs-human` tasks
  and, with the finish mode above, verified tasks that wait for review. For each
  task it shows the key sections of its last report: goal, done so far, next step,
  blockers, the verification result, and the worktree path. The page changes
  nothing; all actions stay in the CLI.

## Later

- Plugin discovery through package entry points.
- Cost reports from the journal (`porter log --cost`).
