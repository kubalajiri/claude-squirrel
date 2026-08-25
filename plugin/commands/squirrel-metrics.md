---
description: List every metric a task or bar rule can use — with its live value, what it means, and the task vocabulary
argument-hint: (no arguments)
---

Run this exact command with the Bash tool and show the user its output verbatim:

`sh "${CLAUDE_PLUGIN_ROOT}/scripts/squirrel.sh" --show-metrics`

**Read this before writing any task or bar rule.** It prints the vocabulary of the version
that is actually installed, which is the only one that can accept or refuse a condition — do
not write a condition from memory of a metric name.

It lists every metric with its **live value right now**, a one-line meaning, the action verbs,
and every field a task takes.

A `?` means this invocation cannot know the value: either nothing has reported it yet, or the
metric is only knowable during a live render (`ctx.pct`, `cost.usd` and friends read the JSON
Claude Code pipes into the bar, which a command-line run does not have). A `?` is not a broken
metric — but **a condition over an unknown metric never fires**, `!=` included.

Do not do anything else.
