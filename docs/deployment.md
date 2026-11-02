# Running a private installation in an organization

porter is designed so that an organization can run its own installation without
changing the core. Everything specific to the organization lives in its own config,
credentials, and plugin directory.

## Before you start: get approval

Running an autonomous agent against an organization's repositories is a policy
decision. Before installing:

- Get written approval from whoever owns engineering tooling and security policy.
- Confirm which agent products and models are approved, and on whose license.
- Confirm which repositories are in scope and what data may be sent to the agent.
- Agree who is on call for `needs-human` escalations.

## Layout

Keep the private installation separate from the public repository:

```
porter-org/                 # private repository or directory
  porter.toml               # the org's config
  plugins/                  # private plugins (Python modules)
    org_tasks.py            # e.g. a task source for the org's tracker
    org_executor.py         # e.g. an executor for an approved internal agent
```

Install porter itself from the public package or a pinned commit; do not fork the
core for org rules.

## Config

```toml
[repo]
path = "../service-repo"
github = "your-org/service-repo"   # or leave empty and let `gh` infer it
default_branch = "main"

[tasks]
source = "org_tasks:OrgTracker"    # module:attr from a plugin path
# Extra keys in [tasks] are passed to the plugin constructor:
project = "SERVICE"

[executor]
kind = "copilot-cli"               # or "org_executor:make_executor"

[plugins]
paths = ["plugins"]

[verify]
commands = ["make lint", "make test"]

[budgets]
wall_seconds = 1800
attempts = 2
premium_requests = 4
```

Every host name, URL, label, and command is a config value or lives in a plugin.
The core contains none.

## Credentials

porter never reads, stores, or logs tokens. Each tool authenticates itself:

- `gh` uses its own login or `GH_TOKEN`.
- Agent CLIs use their own login or environment variables (see `docs/executors.md`).
- Plugins should read credentials from the environment or the organization's secret
  store, never from `porter.toml`.

Use a dedicated service account or bot identity with the least permissions needed:
read issues, write branches, open PRs. It should not be able to push to protected
branches or merge. Enable branch protection on the default branch as a second line
of defense; porter's own guard is the first.

## Plugins

A plugin is a Python module on a configured plugin path:

- Task source: a class or factory returning a `porter.sources.TaskSource`
  (`list()`, optional `escalate()` and `pr_link()`).
- Executor: a factory returning a `porter.executors.Executor`, with honest
  `capabilities`.

Plugins are loaded by `module:attr` name from `[plugins] paths`. Keep them in the
private installation; do not upstream org-specific code.

## Operating

- Start with `porter run --once --dry-run`, then `porter run --once` by hand, then
  schedule it (cron or a CI schedule) once the escalation flow works.
- Keep `.porter/` on durable storage; it holds state, handoffs, the journal, and
  the per-task worktrees (`.porter/worktrees/`).
- Review `porter log` and the cost entries regularly.
