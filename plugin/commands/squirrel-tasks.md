---
description: List every watcher task — its condition, its queue state, when it last fired, and how much of the nudge budget is left
argument-hint: (no arguments)
---

Run this exact command with the Bash tool and show the user its output verbatim:

`sh "${CLAUDE_PLUGIN_ROOT}/scripts/squirrel.sh" --tasks`

This is what the user reads when a watcher did not do what they expected, so read it out
rather than reasoning about it. It shows, per task: the condition, the action, the queue state
(`pending`, or `—` when nothing is queued), when it last fired, and its cooldown. Any task
that does not parse is listed as `IGNORED` with the reason — never silently dropped.

The last line is the **nudge budget**: how many nudges have been created this hour against
`nudgeMaxPerHour`. When the ceiling is reached no nudge job is created at all, and that is
the answer to "why has my watcher gone quiet?".

To check whether one task would fire right now, use `/squirrel-task test <id>`.

Do not do anything else.
