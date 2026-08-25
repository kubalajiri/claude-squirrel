---
description: View or surgically edit per-segment colours — foreground, background, bold, opacity, and the punctuation before a segment
argument-hint: [show | set <segment> fg=.. bg=.. bold=on|off opacity=0.0-1.0 sep=dot|bar|space|none force=on|off | clear <segment> | reset]
---

Run this exact command with the Bash tool and show the user its output verbatim:

`sh "${CLAUDE_PLUGIN_ROOT}/scripts/squirrel.sh" --style "$ARGUMENTS"`

With no arguments this lists every segment that carries a style and the fields available.

| Field | Values | Effect |
|---|---|---|
| `fg` | colour name, `#rrggbb`, or `auto` | foreground colour |
| `bg` | colour name, `#rrggbb`, or `auto` | background behind the segment |
| `bold` | `on` / `off` | bold text |
| `opacity` | `0.0`–`1.0`, or `auto` | fades the text toward the background |
| `sep` | `dot` `bar` `space` `none`, or `auto` | the punctuation printed *before* this segment |
| `force` | `on` / `off` | let `fg`/`opacity` override meaning-carrying colours too |

**The rule to explain when asked:** a warning always breaks through. `fg` and `opacity`
recolour anything decorative — the all-clear green, a clean branch's grey, the dimmed token
counts — but never a yellow or red one. A usage bar that turns red at 80%, the yellow
session-reset countdown and a dirty-branch marker keep their colours, so styling a segment can
never make it stop warning you. `force=on` opts out of that; only suggest it when the user
clearly wants total control.

`opacity` is a real alpha blend toward the idle phase banner's colour while one is painted.
When no banner is painted there is no knowable background, so it falls back to the terminal's
own dim attribute — and it does the same for named ANSI colours, whose actual RGB is the
terminal's choice rather than something we can blend against honestly.

While an idle phase banner or a bar rule is painting the whole line, its background is
applied last and covers a segment's own `bg` — the style is not broken, it is underneath.
Say so if a user reports their background vanishing during an idle alarm.

`sep` is what to reach for when a reordered layout punctuates oddly — a segment moved next to
a group it does not belong to keeps that group's separator until you override it.

- `set <segment> <field>=<value> ...` overlays only the fields given, leaving the segment's
  other fields (and every other segment) untouched. `auto` clears one field.
- `clear <segment>` drops that segment's whole style; `reset` drops all of them.

Every edit validates before writing: an unknown segment, an invalid colour, an out-of-range
opacity, or an unknown separator is refused and **nothing is written**. Never write the
`segmentStyles` config key by hand — always go through this command.

Do not do anything else.
