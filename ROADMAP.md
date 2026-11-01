# Roadmap

Kept short on purpose. Items move to the README status table when built.

## Next

- **Codex adapter.** An executor for the Codex CLI's non-interactive mode, with
  capabilities set from its public docs (resume, structured output, usage).
- **Claude Code adapter.** An executor for Claude Code headless mode (`-p` with
  JSON / stream-JSON output), with resume by session id and cost reporting where
  documented.
- **Follow-up loop for CI failures and review comments.** For tasks in `pr_open`,
  read failing checks and review comments through `gh`, feed them to the executor
  on the same branch, and push a fix. The same budgets and escalation rules apply.
- **Scheduler / daemon.** `porter run` without `--once`: poll on an interval, with
  quiet hours and a per-day budget. Today, use cron or a CI schedule with
  `porter run --once`.
- **cairn memory backend hardening.** Pin the `cairn search --json` output contract,
  add a write path (capture lessons from escalations), timeouts, and a contract test.
- **Org deployment guide.** Expand `docs/deployment.md` with a worked example of a
  private plugin package (task source + executor) and a service-account setup.

## Later

- Worktree per task, so the user's checkout is never touched.
- Plugin discovery through package entry points.
- Cost reports from the journal (`porter log --cost`).
