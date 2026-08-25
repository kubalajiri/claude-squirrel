---
description: View or surgically edit the status-line layout — which segments sit on which line, in what order, and which survive a narrow pane
argument-hint: [show | move <segment> <line> [position] | line <n> <segment>... | swap <line> <line> | priority <segment> <n|always> | reset]
---

Run this exact command with the Bash tool and show the user its output verbatim:

`sh "${CLAUDE_PLUGIN_ROOT}/scripts/squirrel.sh" --layout "$ARGUMENTS"`

With no arguments this prints the layout line by line, each segment followed by its drop
priority — the rank that decides what survives when the pane gets narrow, higher surviving
longer, `always` never dropped — plus `protected` for the two segments that can never be
removed (`account`, `model`), `off` for a segment hidden with `/squirrel-show`, and the list
of registered segments that are not placed on any line.

Lines and positions are numbered from 1. The layout can have **any** number of lines: one,
two (the shipped arrangement), four — Claude Code prints whatever the status line emits.

- `move <segment> <line> [position]` — move one segment; a line number one past the end
  creates a new line, and an omitted position appends to the end of that line.
- `line <n> <segment> <segment> ...` — set one whole line at once; the named segments are
  removed from wherever else they were.
- `swap <a> <b>` — exchange two whole lines.
- `priority <segment> <n|always>` — how hard that segment clings to a narrow pane.
- `reset` — restore the shipped layout and discard every priority override.

Every edit is surgical: it rewrites only the layout, never the rest of the user's config, and
is validated before anything is written. An unknown segment id, a line number out of range, or
an edit that would leave `account` or `model` unplaced is refused with a message and **nothing
is written**. Never construct or write the `layout` / `segmentPriority` config keys by hand —
always go through this command.

Do not do anything else.
