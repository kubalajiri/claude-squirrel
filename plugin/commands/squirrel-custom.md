---
description: View or edit custom segments — your own text, a command's output, or a recurring reminder
argument-hint: [show | add <id> <field>=<value> ... | set <id> <field>=<value> ... | remove <id> | reset]
---

Run this exact command with the Bash tool and show the user its output verbatim:

`sh "${CLAUDE_PLUGIN_ROOT}/scripts/squirrel.sh" --custom "$ARGUMENTS"`

With no arguments this lists every custom segment, what it does, and whether it is showing
right now.

| Field | Values | Effect |
|---|---|---|
| `text` | any text (**takes the rest of the line**) | static text to show |
| `command` | a shell command (**rest of the line**) | its first line of output becomes the text |
| `every` | `30m` `2h` `90s` or seconds | with `ack`: how long after dismissing before it returns. Without `ack`: how often a `command` is re-run |
| `at` | `HH:MM` (24-hour) | show from that time of day |
| `ack` | `on` / `off` | can be dismissed with `/squirrel-done` |
| `shared` | `on` / `off` | keep the dismissal in the cross-profile store, so it clears in every session and every alias at once |
| `when` | a condition (**rest of the line**) | only show while it holds — same grammar as `/squirrel-rules` |
| `line` | `1`, `2`, … | which line it joins |
| `link` | `https://…` | make it clickable where the terminal supports OSC-8 |

`text=`, `command=` and `when=` swallow the rest of the line, so put one of them **last**;
use a second `set` for another. `add` needs `text=` or `command=` — never both.

**Reminders are just this.** There is no separate reminder feature:

    /squirrel-custom add water every=30m ack=on shared=on text=💧 drink water
    /squirrel-custom add break every=2h ack=on shared=on text=🧘 take a break

Dismiss with `/squirrel-done <id>` (or `all`), or push one out with
`/squirrel-snooze <id> <minutes>`. A reminder that has never been dismissed shows straight
away, which is how the user sees that it worked.

`command=` does nothing until commands are switched on with `/squirrel-custom-commands on`
— they are off by default. Tell the user that plainly if they add one; `--show-custom`
marks such segments `BLOCKED` with the command to run. A command never runs while the bar is
drawing: its output is cached and refreshed in the background, so the first render after
adding one shows nothing and the next shows the value.

Every edit validates before writing; a refused edit changes nothing. Never write the
`customSegments` config key by hand — always go through this command.

Do not do anything else.
