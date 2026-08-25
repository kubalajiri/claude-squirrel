---
description: Desktop notifications for a waiting session — the half of the alarm that reaches a monitor you are not looking at
argument-hint: [on|off|focused <on|off>|test]
---

Run this exact command with the Bash tool and show the user its output verbatim:

`sh "${CLAUDE_PLUGIN_ROOT}/scripts/squirrel.sh" --alerts "$ARGUMENTS"`

With no arguments it reports whether alerts are on, whether they fire while the terminal is
focused, and which phases carry an action. `test` sends one notification immediately, and says
which notifier it used — or that the machine has none.

To attach an action to a different phase, use `/squirrel-phases`:

- `set 300 notify=on` — notify five minutes into a wait (the shipped default)
- `set 120 notify="Claude is stuck"` — a phase with its own text
- `add 900 run="curl -d 'still waiting' ntfy.sh/your-topic"` — anything else; `run` needs
  `/squirrel-custom-commands on`
