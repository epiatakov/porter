# Executors

An executor drives one coding agent through `start -> events -> stop -> result` and
declares its `capabilities`. See `docs/design.md` for the interface.

| Executor | `kind` | Status |
|---|---|---|
| Fake (scripted) | `fake` | Built; used by tests and dry runs |
| GitHub Copilot CLI | `copilot-cli` | Built; unit-tested against a fake process |
| Codex CLI | `codex` | Planned |
| Claude Code (headless) | `claude-code` | Planned |
| Your own | `module:attr` | Supported through `[plugins] paths` |

## GitHub Copilot CLI (`copilot-cli`)

### What was checked, and where

Checked on 2026-11-01 from public sources:

- `github/copilot-cli` repository: README, `LICENSE.md`, `container/README.md`,
  and `changelog.md` (latest entry seen: 1.0.105, dated 2026-10-27).
- `github/copilot-sdk` repository: README, `docs/getting-started.md`,
  `docs/auth/README.md`, `LICENSE`.

Could **not** be read from this environment: the docs.github.com Copilot CLI pages
(including the command reference) and the npm package page. Facts below come from
the repositories above. Items marked *unverified* were not confirmed in any source.

| Topic | Finding | Confidence |
|---|---|---|
| Non-interactive mode | `-p` / `--prompt "<text>"` runs one prompt and exits; piped stdin also works | Verified (changelog) |
| Tool permissions | `--allow-all-tools`, `--allow-all-paths`, `--allow-all-urls`; `--allow-tool` / `--deny-tool` with patterns; `--allow-all` / `--yolo` | Flags verified; **pattern syntax unverified** |
| Model | `--model <name>`; `auto` is accepted | Verified (changelog) |
| Reasoning effort | `--reasoning-effort <none\|minimal\|low\|medium\|high\|xhigh\|max>`; supported levels depend on the model | Verified (`copilot --help`, CLI 1.0.108, 2026-11-12) |
| Output format | `--output-format json` emits JSONL in prompt mode | Flag verified; **event schema unverified** |
| Stats output | An exit summary with usage is printed; `--silent` suppresses stats | Partly verified (changelog summaries) |
| Resume | `--resume[=<id or name>]`, `--continue`, `--session-id=<id>` exist | Verified; **use together with `-p` unverified** |
| Structured output | No schema-constrained final answer for the top-level agent found | Not found |
| Usage / cost | One premium request per prompt (times the model multiplier); `--usage-output-file` writes JSON usage | First verified (README); second **schema unverified** |
| Working dir | Runs in the cwd; `-C <dir>`; `--add-dir <dir>`; `--trust-folder` for prompt mode | Verified (changelog) |
| Logs | `--log-dir`, `--log-level` | Verified (changelog, container README) |
| Auth | `COPILOT_GITHUB_TOKEN`, then `GH_TOKEN`, then `GITHUB_TOKEN`; or `copilot` `/login`; fine-grained PAT needs the "Copilot Requests" permission | Verified (README, container README) |
| Exit codes | Non-zero on failure is assumed | **Unverified** |
| License | "GitHub Copilot CLI License": proprietary; install and run allowed; redistribution only unmodified and as part of a larger product; no modification | Verified (`LICENSE.md`) |
| Copilot SDK | MIT; generally available; Node/TS, Python, Go, .NET, Rust, Java; talks JSON-RPC to the CLI in server mode | Verified (SDK README, LICENSE) |

porter invokes `copilot` as a separate program that the user installs and licenses.
It does not bundle or redistribute it.

### Declared capabilities

| Capability | Value | Reason |
|---|---|---|
| `resume` | `false` | Resume flags exist, but combining them with `-p` is unverified. porter resumes from the handoff text instead. |
| `tools` | `true` | The agent edits files and runs commands under the `--allow-*` flags. |
| `structured_output` | `false` | No top-level schema-constrained output found. porter parses the `PORTER-STATUS:` line. |
| `cost_reporting` | `false` | The usage summary format is unverified. With a premium-request cap set, porter counts one request per run and marks it `estimated` in the journal. |
| `model_per_run` | `true` | `--model` and `--reasoning-effort` are set on every invocation, so each `[[stage]]` can use its own. |

These get revisited once the official reference can be checked. Flip a capability
only with a source and a test.

### What porter runs

```
copilot -p "<prompt>" [--model M] [--reasoning-effort E] --allow-all-tools
        [--allow-tool P]... [--deny-tool P]... [--output-format json]
        --log-dir .porter/runs/<id>/copilot-logs [extra_args...]
```

- A `[[stage]]` model or effort replaces the configured `model` / `effort` for that run.
- cwd is the task's worktree (see "Where porter works" in the README); stdin is
  closed; `NO_COLOR=1` is set.
- stdout and stderr go to `.porter/runs/<id>/copilot-output.log` and to the journal.
- The run is stopped (SIGTERM, then SIGKILL after a grace period) at the task's
  wall-time deadline.
- Outcome: `blocked` if the last `PORTER-STATUS:` line says so; `failed` on a
  non-zero exit; otherwise `completed`, and verification decides.

Config:

```toml
[executor]
kind = "copilot-cli"

[executor.copilot]
binary = "copilot"        # or a list, e.g. ["npx", "@github/copilot"]
model = ""                # empty = CLI default
effort = ""               # empty = CLI default; a [[stage]] can override both
allow_all_tools = true
allow_tools = []
deny_tools = []           # pattern syntax: check `copilot --help` first
output_format = "text"    # "json" is experimental: the event schema is undocumented
extra_args = []           # e.g. ["--log-level", "debug"]
env = {}                  # extra environment variables (never put tokens here)
```

### Tests

`tests/test_copilot.py` runs the adapter against a small fake `copilot` script (a
Python file) that prints recorded-style output, exits non-zero, emits JSONL, or
hangs. No Copilot auth is needed. A real Copilot run is not part of CI.

### Manual smoke test

Run this on a machine where Copilot CLI is installed and logged in, in a scratch
clone of a repository you own.

1. `copilot --version` and `copilot --help`. Write down the version and confirm
   `-p`, `--allow-all-tools`, `--deny-tool`, `--model`, `--output-format`,
   `--log-dir` exist. Note any differences here.
2. Plain prompt mode:
   `copilot -p "Reply with exactly: PORTER-STATUS: done" --allow-all-tools; echo "exit=$?"`.
   Expect the line in the output and `exit=0`. Note the usage summary format.
3. Failure exit code: run with an invalid `--model` and record the exit code.
4. JSONL: repeat step 2 with `--output-format json` and save the output. If the
   schema is stable, add the sample as a fixture and tighten `_first_text`.
5. Resume: `copilot -p "remember the word kiwi" --session-id <uuid>`, then
   `copilot -p "what word?" --resume=<uuid>`. If it works, set `resume = true` with
   a test and pass `context.resume_session`.
6. End to end: `porter init --source local --executor copilot-cli`, add a tiny task
   to `tasks.toml`, run `porter run --once --dry-run` (prints the command), then
   `porter run --once` with `[policy] open_pr = false` to stop before pushing.
   Check `.porter/runs/1/` (prompt, output log, handoff) and `porter log`.

Record results (date, CLI version, findings) in this file.

## Fake executor (`fake`)

Scripted steps, one per attempt (the last step repeats). Keys: `outcome`,
`summary`, `blocker`, `write` (files), `events`, `cost`, `sleep`, `kill` (SIGKILL
after the writes, leaving unfinished edits; only with `PORTER_FAKE_ALLOW_KILL=1`;
used by the killed-run tests). On a real run it writes into the task worktree; in a
dry run its file writes go to the run directory, not the checkout.

## Writing an executor

1. Subclass `porter.executors.Executor`, set `name` and honest `capabilities`.
2. `start()` launches the agent without blocking; `events()` yields while it runs and
   respects `context.deadline`; `stop()` must be safe to call twice; `result()`
   maps to `completed | blocked | failed | stopped`.
3. Test against a fake process. Do not require real credentials in CI.
4. Document what you verified here, with sources.
