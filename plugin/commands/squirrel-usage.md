---
description: Turn the per-model weekly usage fetch on or off
argument-hint: <on|off | keychain on|off>
---

If the arguments are `keychain on`, FIRST tell the user (before running anything): macOS
will show a Keychain permission dialog during the command — click **Always Allow** so the
background fetches stay silent; a plain "Allow" means the next background fetch asks once
more. Then run the command below.

For any other arguments, run it straight away.

Run this exact command with the Bash tool and show the user its output verbatim:

`sh "${CLAUDE_PLUGIN_ROOT}/scripts/squirrel.sh" --set-usage "$ARGUMENTS"`

Do not do anything else.
