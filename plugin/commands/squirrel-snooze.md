---
description: Postpone a due reminder by a few minutes without clearing its full interval
argument-hint: <id> <minutes>
---

Run this exact command with the Bash tool and show the user its output verbatim:

`sh "${CLAUDE_PLUGIN_ROOT}/scripts/squirrel.sh" --snooze "$ARGUMENTS"`

Use this for "not now", "in ten minutes", "later" — as opposed to `/squirrel-done`, which
means the user has actually done it. The segment reappears after the given number of minutes
rather than after its full `every` interval.

Only works on a segment with both `ack=on` and `every=<interval>`.

Do not do anything else.
