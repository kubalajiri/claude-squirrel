---
description: View or surgically edit the bar rules — paint the whole status line when a condition holds (usage high, repo dirty, session idle, …)
argument-hint: [show | add <condition> bg=.. text=.. [bold=on|off] [priority=<n>] | set <condition> <field>=<value> ... | remove <condition> | reset]
---

Run this exact command with the Bash tool and show the user its output verbatim:

`sh "${CLAUDE_PLUGIN_ROOT}/scripts/squirrel.sh" --rules "$ARGUMENTS"`

With no arguments this lists every rule in evaluation order — the shipped idle ladder first,
then the user's own — marking the one that is **winning right now** (`<== WINNING NOW`), along
with the metrics available and any rule that does not parse. That listing is the answer to
"why is my bar this colour?"; read it out rather than reasoning about precedence.

A **condition** is exactly `<metric> <op> <value>`, or a bare `<metric>` for true/false:

    weekly.pct >= 80        git.dirty        cost.usd > 5        git.branch == main

Operators are `>=` `<=` `==` `!=` `>` `<`. Run the command with no arguments to get the live
list of metrics — they cover context and usage percentages, cost, duration, lines, tokens,
git (dirty/ahead/branch), repo, model, effort, idle seconds, and activity state.

Nothing here is ever evaluated as code. A condition that does not parse — an unknown metric,
a missing value, two comparisons — is **reported and ignored**, and the bar keeps rendering.
`/squirrel-rules` and `/squirrel-config` both say how many rules are being ignored and why.

**A metric with no value never matches**, not even with `!=`. "Unknown" is not "different
from": a rule must not paint the bar because data is missing.

- `add <condition> bg=.. text=.. [bold=on|off] [priority=<n>]` — a rule must paint something,
  so `bg`, `text`, or both are required.
- `set <condition> <field>=<value> ...` — overlays only the fields given onto an existing rule.
- `remove <condition>` — drops one of the user's own rules (the idle ladder is edited with
  `/squirrel-phases`, not here).
- `reset` — clears the user's rules; the idle ladder remains.

**Which rule wins:** the last matching rule, unless one carries a higher `priority`. The idle
phase ladder ships as the first rules, so a user rule added later wins a tie with it by
default — mention that if they ask why their rule beats the idle tint.

Every edit validates before writing; a refused edit changes nothing. Never write the
`barRules` config key by hand — always go through this command.

Do not do anything else.
