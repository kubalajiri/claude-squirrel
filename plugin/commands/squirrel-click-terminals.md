---
description: Override which terminals are treated as able to follow a status-line hyperlink
argument-hint: <name,name|any|reset>
---

Run this exact command with the Bash tool and show the user its output verbatim:

`sh "${CLAUDE_PLUGIN_ROOT}/scripts/squirrel.sh" --click-terminals "$ARGUMENTS"`

Squirrel only emits a click link, and only starts a listener, where the link can actually be
followed — matched against `TERM_PROGRAM`. The shipped list is `vscode`, `cursor`,
`windsurf`, because Claude Code currently passes OSC-8 through to IDE terminals and not to
standalone ones.

Use this if that list is wrong for them: name their terminal, or `any` to skip the check
entirely. Warn them that `any` means a listener may run in a terminal that cannot reach it.
`reset` restores the shipped list.

Do not do anything else.
