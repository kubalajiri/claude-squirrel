---
description: Acknowledge a due reminder so it goes away until it is next due
argument-hint: <id|all>
---

Run this exact command with the Bash tool and show the user its output verbatim:

`sh "${CLAUDE_PLUGIN_ROOT}/scripts/squirrel.sh" --ack "$ARGUMENTS"`

Use this when the user says they have done the thing — "drank it", "had a break", "done" —
not only when they type an id. `all` clears every dismissible segment at once.

A segment can only be acknowledged if it has `ack=on`; the reply names the command to add it
if not. Where the segment has `shared=on`, one acknowledgement clears it in every session and
every account alias within a few seconds.

To postpone rather than clear, use `/squirrel-snooze <id> <minutes>`.

Do not do anything else.
