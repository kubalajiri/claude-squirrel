---
description: View or surgically edit the idle-phase ladder (the full-width background's colours and timings)
argument-hint: [add|set|remove|reset] <seconds> [bg=..] [text=..] [bold=..]
---

Run this exact command with the Bash tool and show the user its output verbatim:

`sh "${CLAUDE_PLUGIN_ROOT}/scripts/squirrel.sh" --phases "$ARGUMENTS"`

With no arguments, this lists the current ladder. `add <seconds> bg=<colour|off>
[text=<colour|off>] [bold=on|off]` adds a new phase; `set <seconds> [bg=..] [text=..]
[bold=..]` overlays only the field(s) given onto the phase at that `after` value, leaving
everything else about it (and every other phase) untouched; `remove <seconds>` drops one
phase; `reset` discards all overrides and restores the shipped ladder. `bold` makes the
`text` colour bold (default `false`; both shipped phases ship `true`).

Never construct or write `idlePhases` JSON by hand — always go through this command (or
`--show-phases`/`--add-phase`/`--set-phase`/`--remove-phase`/`--reset-phases`), which edit one
phase at a time and never touch the others.

Do not do anything else.
