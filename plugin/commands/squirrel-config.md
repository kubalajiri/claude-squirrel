---
description: Show the Squirrel configuration and which layer each value comes from, or share this profile's settings with your other profiles
argument-hint: [show | share]
---

Run this exact command with the Bash tool and show the user its output verbatim:

`sh "${CLAUDE_PLUGIN_ROOT}/scripts/squirrel.sh" --config "$ARGUMENTS"`

Configuration resolves in three layers, the last one that sets a value winning:

| Layer | Where | What it is for |
|---|---|---|
| shipped | the plugin's own `defaults/config.json` | the defaults, read-only |
| shared | `~/.squirrel/config.json` | settings you want in **every** profile |
| this profile | `$CLAUDE_CONFIG_DIR/squirrel/config.json` | settings for **this** profile only |

With no arguments this lists every setting you have made and the layer it came from. That is
the answer to "why is this not what I set?" — a value set in the shared layer and overridden
here is reported as **shadowing shared**.

`share` copies this profile's settings down into the shared layer, so your other profiles pick
them up. It leaves this profile's own config exactly as it was, so running it here cannot
change anything you are looking at — it is safe to run and safe not to. It reports every
setting and what happened to it. Settings the shared layer already has are left alone rather
than overwritten, because those belong to your other profiles too.

**Where an edit goes.** Every editing command writes to the shared layer by default, unless
the setting is already set in this profile — in which case it keeps being edited here, so
nothing you have already configured moves under you. Add `--here` to any editing command to
write to this profile deliberately. Every command says which layer it wrote to.

A `reset` clears the setting from **both** layers, so it genuinely stops being set. Use
`--here` on a reset to clear only this profile and leave the shared value standing.

Do not do anything else.
