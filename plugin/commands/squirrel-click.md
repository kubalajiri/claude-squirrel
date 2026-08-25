---
description: Allow a reminder to be dismissed by clicking it — starts a local listener only when one is actually needed
argument-hint: <on|off>
---

Run this exact command with the Bash tool and show the user its output verbatim:

`sh "${CLAUDE_PLUGIN_ROOT}/scripts/squirrel.sh" --click "$ARGUMENTS"`

**Read the reply out — it tells the user whether their terminal can actually follow the
link.** Claude Code currently passes OSC-8 hyperlinks through to IDE terminals (VS Code,
Cursor) and not to standalone ones like iTerm2 or Konsole. On a terminal that cannot follow
them, no listener is started at all, deliberately: a socket nobody can click is pure cost.
`/squirrel-done` works everywhere and is the answer there.

**Say these three things before turning it on:**

1. **A local web server is involved.** It binds `127.0.0.1` on a random port, is reachable
   only from this machine, and needs an unguessable token in the URL. It starts only once a
   segment actually has `click=ack`, and exits by itself after `clickIdleSeconds` (30 min) of
   nothing clicking it.
2. **Clicking opens a browser tab.** A hyperlink is the only interactivity a terminal offers,
   and following one means the browser. The tab shows a small page saying what happened and
   closes itself where the browser allows. This cannot be engineered away — if that would
   annoy them, they should not enable it.
3. **It can only acknowledge or snooze.** The listener understands exactly those two verbs
   against segment ids already in their config. It cannot run commands.

To make a reminder clickable, it needs `ack=on` and a click action:

    /squirrel-custom set water click=ack
    /squirrel-custom set break click=snooze:15

`/squirrel-click off` stops new links being emitted; any running listener exits when it next
goes idle.

Do not do anything else.
