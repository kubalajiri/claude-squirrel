---
description: Set any Squirrel threshold (colour thresholds, cache TTL, bar size, timeouts…)
argument-hint: <key> <number>
---

Run this exact command with the Bash tool and show the user its output verbatim:

`sh "${CLAUDE_PLUGIN_ROOT}/scripts/squirrel.sh" --set "$ARGUMENTS"`

If the user gave no arguments, the command lists every tunable with its current value — relay that list to the user. Do not do anything else.
