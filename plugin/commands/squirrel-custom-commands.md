---
description: Allow (or forbid) custom segments to run their `command` — off by default
argument-hint: <on|off>
---

Run this exact command with the Bash tool and show the user its output verbatim:

`sh "${CLAUDE_PLUGIN_ROOT}/scripts/squirrel.sh" --custom-commands "$ARGUMENTS"`

Custom segments with a `command` do nothing until this is switched on. Static `text`
segments and reminders are unaffected and always work.

**Say what this means before turning it on.** With it on, Squirrel runs the `command` of any
custom segment in the user's config, in the background, on a timer. Only their own config can
define one — but that also means anything able to edit their config can cause a command to
run, and this plugin's own slash commands edit that config. It is a deliberate opt-in for
that reason. Do not turn it on speculatively; turn it on when the user has asked for a
command segment and understands what it does.

Do not do anything else.
