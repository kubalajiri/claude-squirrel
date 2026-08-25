---
description: Turn a task action on or off — nothing a task can do outside the bar works until you enable it here
argument-hint: <notify|run|ack|snooze> <on|off>
---

Run this exact command with the Bash tool and show the user its output verbatim:

`sh "${CLAUDE_PLUGIN_ROOT}/scripts/squirrel.sh" --task-actions "$ARGUMENTS"`

Every action class is **off by default**. A task whose action is not enabled is refused when
you try to create it, rather than created and left silently doing nothing.

With no arguments this lists what is currently enabled.

**Explain the cost before enabling `notify`.** Delivering a nudge runs a one-shot `claude -p`
on the cheapest model, so every nudge is a real request billed to the user's own account.
`nudgeMaxPerHour` (default 5) is the machine-wide ceiling on that, across every task.

Do not do anything else.
