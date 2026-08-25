---
description: Show every live Squirrel session on this account and who's burning the 5-hour limit
argument-hint: [wide]
---

Run this exact command with the Bash tool and show the user its output verbatim (as a code
block, so the columns line up):

`sh "${CLAUDE_PLUGIN_ROOT}/scripts/squirrel.sh" --show-sessions "${CLAUDE_SESSION_ID} $ARGUMENTS"`

The table sheds columns to fit the terminal rather than wrapping — ten columns need about 130
of them. `session` and `5h pts` are never dropped, since they are the question the table
answers: which session, and how much of the shared 5-hour window it has taken. When anything
is hidden the table says so and names the missing columns.

If the user wants everything regardless of width, pass `wide` (they may say "show me
everything" or "the full table") — the output will wrap, which is their choice to make.

Read the `5h pts` marks out when explaining the table: no mark means exact, `~` means
estimated (the window was shared, or part of it is a gap), `?` means nothing could be
attributed yet. Those marks stay on the figure at every width, so a number never reads as
more certain than it is.

Do not do anything else.
