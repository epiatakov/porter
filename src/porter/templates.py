"""Starter files written by ``porter init``."""

from __future__ import annotations


def config_template(source: str = "local", executor: str = "fake") -> str:
    if source == "github":
        tasks = (
            '[tasks]\nsource = "github"\nlabel = "agent-ready"   # issues with this label are '
            "picked; p0..p3 labels set priority\nlimit = 50\n"
        )
    else:
        tasks = '[tasks]\nsource = "local"\nfile = "tasks.toml"\n'
    if executor == "copilot-cli":
        ex = (
            '[executor]\nkind = "copilot-cli"\n\n[executor.copilot]\nbinary = "copilot"\n'
            'model = ""              # empty = CLI default\nallow_all_tools = true\n'
            "deny_tools = []\n"
            'output_format = "text"  # "json" = JSONL (schema not documented; experimental)\n'
        )
        verify = '[verify]\ncommands = ["ruff check .", "pytest -q"]\ntimeout = 900\n'
    else:
        ex = (
            '[executor]\nkind = "fake"   # scripted stand-in; switch to "copilot-cli" for real '
            'work\n\n[executor.fake]\nscript = [\n  { outcome = "completed", summary = "Added '
            'the requested note.", write = { "PORTER_DEMO.md" = "hello from porter\\n" } },\n]\n'
        )
        verify = (
            "[verify]\n# {python} expands to the interpreter running porter.\n"
            'commands = ["{python} -c \\"print(\'checks ok\')\\""]\ntimeout = 900\n'
        )
    return f"""# porter configuration. See docs/design.md and docs/deployment.md.

[repo]
path = "."                # repository porter works in, relative to this file
github = ""               # "owner/name"; empty = let `gh` infer from the git remote
default_branch = "main"
remote = "origin"
branch_prefix = "porter/"

{tasks}
{ex}
{verify}
[policy]
open_pr = true
push_default_branch = false   # must stay false
merge = false                 # must stay false
escalation_label = "needs-human"
verify_feedback_rounds = 1

[budgets]
wall_seconds = 1800
attempts = 3
premium_requests = 0          # 0 = no cap

[memory]
backend = "file"              # "file" | "cairn" | "none"
limit = 5
"""


def tasks_template() -> str:
    return """# Local task list for porter (source = "local").

[[task]]
id = "demo-1"
title = "Add a hello note"
body = "Create PORTER_DEMO.md with a short greeting."
labels = ["p1"]
"""
