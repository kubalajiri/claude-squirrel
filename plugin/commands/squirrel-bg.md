---
description: Set the full-width background colour shown when a session sits idle, the alarm-phase bar colour, or which lines it covers
argument-hint: [urgent|lines] <colour|off|auto|all|1,2>
---

Run this exact command with the Bash tool and show the user its output verbatim:

`sh "${CLAUDE_PLUGIN_ROOT}/scripts/squirrel.sh" --set-bg "$ARGUMENTS"`

`/squirrel-bg <colour|off>` sets the warn-phase banner colour shown once a session has been
waiting past `idleBackgroundAfterSeconds` (a clearly visible yellow, `#c9a227`, by default —
bold white text) — a hazard-banner-style warning, not a subtle tint. `/squirrel-bg urgent
<colour|off>` sets the alarm-phase bar colour that takes over once the wait also passes
`alarmAfterSeconds` — the same trigger as the loud chip — painting the whole bar in that
colour (strong red, `#b3261e`, by default) with bold white text. These two phases are really
the first two entries of the full idle-phase ladder (see `/squirrel-phases` for any phase
beyond these two, or to change `text`/`bold` per phase — e.g. `--set-phase 30 text=black
bold=off`). `urgent off` paints no background at the alarm phase (the loud chip and its text
colour still apply) rather than falling back to the warn-phase colour; `/squirrel-bg off`
disables all painting outright.

Do not do anything else.
