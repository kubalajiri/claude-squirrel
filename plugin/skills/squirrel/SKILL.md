---
name: squirrel
description: Set up, configure, explain or troubleshoot the Squirrel status line — the two-line Claude Code bar showing context, 5-hour and weekly usage limits, git branch, cost, an activity chip, and cross-session usage attribution. Use when the user asks to install or set up the status line, change their nickname or colour, hide or show segments (git, cost, tokens, PR, branch), shorten a long branch name, adjust the idle alarm or the full-width idle background, change colour thresholds or any other Squirrel setting, asks what the status line means or what Squirrel can do, reports that the bar looks wrong, is too wide, too noisy, or is not updating, or asks which of several open Claude Code sessions is consuming their usage limit.
allowed-tools: Bash(sh "${CLAUDE_PLUGIN_ROOT}/scripts/squirrel.sh" *)
---

# Squirrel status line

Squirrel is a two-line Claude Code status line: context usage, 5-hour and weekly rate
limits, git state, cost, and an activity chip that tells you when Claude is working, idle,
or needs you. Everything about it is configured through one bundled script — never by
hand-editing the user's files.

## 1. How to act

Always change settings by running the bundled script, never by hand-editing the user's
config file or `settings.json`:

```
sh "${CLAUDE_PLUGIN_ROOT}/scripts/squirrel.sh" <subcommand> <args>
```

- Show the command's own output back to the user verbatim — it is written for them.
- Run `--show-config` first when the request depends on current values (e.g. "what's my
  alarm set to?", "make it a bit more sensitive").
- Run `--help-text` if you are unsure of an exact key or subcommand name; it lists every
  subcommand, segment key, and tunable with its bounds, pulled live from the code so it can
  never drift out of date.
- Every setting has a subcommand — see §4. Never hand-edit the config file; there is no
  setting Squirrel can't reach through a command.

## 2. What the bar shows

**Line 1** — usage:

| Segment key | Shows |
|---|---|
| `context` | `ctx ▰▰▰▱▱ 42% (84k/200k)` — context-window used, as a bar and percentage |
| `tokens` | the `(used/window)` token-count suffix on the context segment |
| `session` | the 5-hour rate-limit bucket, as a bar and percentage — `19pp/90%` when this session's own **contribution** to the account's usage is known ('19 percentage points of the account's 90%'), plain `90%` otherwise; see §6 |
| `weekly` | the all-models weekly rate-limit bucket |
| `permodel` | per-model weekly buckets alongside the all-models one (needs `weekly` on) |
| `resets` | `↻ <time>` reset countdowns on the 5-hour and weekly segments |
| `share` | *(off by default)* a standalone version of the same contribution figure, labelled `mine` (not `5h`, so it never collides with the `session` segment) — `mine 19pp/89%` by default; see §6 |
| `share-life` | *(off by default)* this session's share of all live sessions' lifetime usage, labelled `life` — `life 22%` |
| `sessions` | *(off by default)* every live session ranked by attributed 5h-window share, current one marked `▸` — `A 52% · B 31% · ▸C 12% · D 5%` (or `?` for a session with no attribution yet) |

A bar/percentage turns yellow at `warnPercent` and red at `dangerPercent`. A leading `~`
on a percentage means the cached value is stale (old, or the last fetch attempt failed).
The 5-hour countdown itself also turns yellow at `resetWarnMinutes` and red at
`resetUrgentMinutes` before the reset (`0` disables); the weekly reset, shown as an
absolute time, is never coloured this way. `share`/`share-life`/`sessions` come from
**cross-session usage attribution** — see §6.

**Line 2** — identity, git, cost, activity:

| Segment key | Shows |
|---|---|
| `account` | account label — nickname if set, else email (cannot be hidden) |
| `repo` | the current repo/project name, colour-hashed per repo |
| `git` | branch name, dirty marker (`*`), unpushed-commit count (`↑N`) |
| `branch` | just the branch-name text within `git` (turning it off keeps the `*`/`↑N` markers) |
| `ahead` | just the `↑N` unpushed-commit marker within `git` |
| `model` | the model display name (cannot be hidden) |
| `effort` | the effort level (e.g. high/medium/low), when the model reports one |
| `flags` | `thinking` / `fast` mode flags, when active |
| `sessionTokens` | total input+output tokens used this session |
| `cost` | total session cost in USD |
| `duration` | total session duration |
| `lines` | lines changed this session, `+added/−removed` |
| `pr` | the current PR/MR number and review state, when known |
| `chip` | the activity chip — see below |

**Activity chip** (`[ ⚙ working ]` green, `[ ✓ idle ]` yellow, `[ ◀ needs you ]` red,
`[ ⧗ N subagents ]` cyan — one space padding each side by default, see `chipWidth` in §4): reflects
Claude's current activity state for this session. If a waiting state (idle or needs-you, by
default) lasts past `alarmAfterSeconds`, the chip switches to a loud bold white-on-red alarm
showing how long it's been waiting.

The full-width background is a **phase ladder** — `idlePhases` in `defaults/config.json` — of
any length, not a fixed pair. Each phase is `{"after": <seconds>, "bg": <colour|"off">, "text":
<colour|"off">, "bold": <true|false>}`; the last phase whose `after` has elapsed wins, and it
paints while the session is in one of `idleBackgroundStates` (never during
`working`/`subagents`). When a phase's `text` is set, it genuinely replaces every segment's
own foreground colour for that line (not merely interleaved with it) — the loud chip is the
one exception, keeping its own inverse styling regardless; `bold` (default `false`, but `true`
on both shipped phases) makes that `text` colour bold. Shipped: 30s → a clearly visible yellow
(`#c9a227`, bold white text — a hazard-banner warn), 120s (the same threshold that triggers
the loud chip) → strong red (`#b3261e`, bold white text) — unmissable across a room.

**Every setting has a subcommand — never write `idlePhases` by hand.** Run `--show-phases`
before any edit, then use exactly one surgical subcommand:

| Intent | Command | Effect |
|---|---|---|
| inherit the shipped value | leave the field out of `set`/`add` | shipped `bg`/`text` used |
| set a value | `--set-phase <seconds> bg=<colour>` | that colour |
| explicitly none | `--set-phase <seconds> bg=off` | phase still fires; no background painted (a `text` override may still apply) |
| drop the step | `--remove-phase <seconds>` | nothing happens at that time |
| add a new step | `--add-phase <seconds> bg=<colour> [text=<colour>] [bold=on\|off]` | a phase at that `after` |
| start over | `--reset-phases` | discards every override, back to the shipped ladder |

Never rewrite the whole ladder — `--set-phase`/`--add-phase`/`--remove-phase` touch exactly the
phase named and leave every other phase, and every other field of that phase, exactly as it
was. `/squirrel-phases` (and `--phases`) accepts the same subcommands in one call, e.g.
`/squirrel-phases add 600 bg=#7c3aed text=white`.

`idleBackground`/`idleBackgroundUrgent`/`idleBackgroundAfterSeconds` (`--set-bg`/`--set-bg
urgent`/`--set-bg-after`) are the original two-phase commands, kept working: they fold into
the ladder's first two slots exactly as before the ladder existed, so nothing written with
them stops working. Prefer the phase commands for anything beyond those first two steps.

## 3. Setup

`--setup "${CLAUDE_PLUGIN_ROOT}"` writes the status line into the user's `settings.json`
(with a 5-second refresh interval) and installs a small launcher shim so it keeps working
across plugin updates. The activity-state hooks activate automatically because the plugin
is installed — no separate step. After setup, suggest `/squirrel-nick <name>` to label the
account.

### 3a. The commands the user can type

Everything below can be driven two ways: the user asks you and you run the flag, or the user
types the slash command themselves. **When you tell someone how to change something, give
them the slash command** — a flag is a string they cannot type into Claude Code. Both are
listed so you can move between them.

| Slash command | Flag you run |
|---|---|
| `/squirrel-alarm` | `--set-alarm` |
| `/squirrel-attribution-metric` | `--set-attribution-metric` |
| `/squirrel-bg-after` | `--set-bg-after` |
| `/squirrel-bg` | `--set-bg` |
| `/squirrel-alerts` | `--alerts` |
| `/squirrel-branch` | `--set-branch-max` |
| `/squirrel-click-terminals` | `--click-terminals` |
| `/squirrel-click` | `--click` |
| `/squirrel-color` | `--set-color` |
| `/squirrel-config` | `--config` (`show` \| `share`; bare `--show-config` still works) |
| `/squirrel-custom-commands` | `--custom-commands` |
| `/squirrel-custom` | `--custom` |
| `/squirrel-default-color` | `--set-default-color` |
| `/squirrel-done` | `--ack` |
| `/squirrel-help` | `--help-text` |
| `/squirrel-label` | `--set-label` |
| `/squirrel-layout` | `--layout` |
| `/squirrel-nick` | `--set-nick` |
| `/squirrel-phases` | `--phases` |
| `/squirrel-repo-color` | `--set-repo-color` |
| `/squirrel-reset-warn` | `--set-reset-warn` |
| `/squirrel-rules` | `--rules` |
| `/squirrel-metrics` | `--show-metrics` |
| `/squirrel-task` | `--task` |
| `/squirrel-tasks` | `--tasks` |
| `/squirrel-task-actions` | `--task-actions` |
| `/squirrel-session-contribution` | `--set-session-contribution` |
| `/squirrel-sessions-show-model` | `--set-sessions-show-model` |
| `/squirrel-sessions` | `--show-sessions` |
| `/squirrel-set` | `--set` |
| `/squirrel-setup` | `--setup` |
| `/squirrel-share-format` | `--set-share-format` |
| `/squirrel-show` | `--show-segment` |
| `/squirrel-snooze` | `--snooze` |
| `/squirrel-states` | `--set-states` |
| `/squirrel-style` | `--style` |
| `/squirrel-usage` | `--set-usage` |

`/squirrel-help` prints this same reference live from the code.

## 4. Every setting

| Config key | Subcommand | Valid values | Default | Effect |
|---|---|---|---|---|
| `accountNames` | `--set-nick <name>` | any text (sets the name for the account you're currently logged into only) | `{}` | overrides the account label text |
| `accountColors` | `--set-color <colour>` | a colour name (`red green yellow blue magenta cyan white gray`, or `bright<name>`) or `#rrggbb` (current account only) | `{}` | overrides the account label colour |
| `repoColors` | `--set-repo-color <colour\|off>` | a colour name or `#rrggbb`, or `off`/`auto` (current repo only, resolved from the working directory) | `{}` | per-repo colour override |
| `defaultAccountColor` | `--set-default-color <colour>` | a colour name or `#rrggbb` | `"white"` | fallback account colour when no override matches |
| `segments.*` | `--show-segment <segment> <on\|off>` | one of the segment keys in §2; `account`/`model` can't be turned off | all on, except `share`/`share-life`/`sessions` (off) | shows/hides one segment |
| `layout` | `--layout <show\|move\|line\|swap\|priority\|reset>` (also `--show-layout`) | array of lines, each an array of segment ids from §2 — **any number of lines** | the two shipped lines | which segments render, on which line, in what order — see §4a |
| `segmentPriority.*` | `--set-priority <segment\|detail> <n\|always>` (also `--layout priority ...`) | an integer, or `always` (never dropped); keys are segment ids **or** the details `tokens`, `resets`, `bars`, `abbrev` | `{}` | how hard something clings to a narrow pane — higher survives longer; see §4a |
| `customSegments` | `--custom <add\|set\|remove\|reset> <id> <field>=<value> ...` (also `--show-custom`) | fields: `text`/`command`/`when` (each takes the REST of the line), `every` (`30m`/`2h`/`90s`), `at` (`HH:MM`), `ack`/`shared` (on/off), `line` (n), `link` (https) | `[]` | the user's own segments — and reminders; see §4d |
| `clickToAck` | `--click <on\|off>` | on/off | `false` | whether a reminder can be dismissed by clicking it — see §4f. FOUR conditions must hold for a link to appear: this on, the segment has `click=`, `TERM_PROGRAM` is vscode/cursor/windsurf, and the line has ~64 spare columns for the URL (Claude Code counts it as visible width). "Why can't I click the reminder?" is answered by `--show-config`, and if that says the feature is on, the answer is width — widen the pane or move reminders to their own line |
| `clickTerminals` | `--click-terminals <name,name\|any\|reset>` | terminal names, or `any` | `[]` (vscode, cursor, windsurf) | which `TERM_PROGRAM`s can follow a status-line link |
| `stdinWaitSeconds` | `--set stdinWaitSeconds <n>` | 0-120 | `10` | how long a render waits for the status JSON before giving up (`0` = for ever); a backstop, not something users normally touch |
| `clickIdleSeconds` | `--set clickIdleSeconds <n>` | 30-86400 | `1800` | how long the click listener stays up with nothing clicking it |
| `customCommands` | `--custom-commands <on\|off>` | on/off | `false` | whether a custom segment may RUN its `command` |
| `barRules` | `--rules <add\|set\|remove\|reset> <condition> <field>=<value> ...` (also `--show-rules`) | array of `{when, bg, text, bold, priority}`; condition is `<metric> <op> <value>` or a bare `<metric>` | `[]` | paints the whole bar when a condition holds — see §4c |
| `segmentStyles.*` | `--style set <segment> <field>=<value> ...` (also `--show-styles`) | fields: `fg`/`bg` (colour name, `#rrggbb`, `auto`), `bold` (on/off), `opacity` (0.0-1.0, `auto`), `sep` (`dot`/`bar`/`space`/`none`/`auto`), `force` (on/off) | `{}` | per-segment colours and the punctuation before a segment — see §4b |
| `segmentLabels.*` | `--set-label <segment> <text\|reset>` | any text for `share` or `share-life`; `reset` restores the shipped label | `{"share": "mine", "share-life": "life"}` | overrides one segment's display label — resolved per key, so overriding one never erases the other's default |
| `shareFormat` | `--set-share-format <contribution\|share\|both>` | one of the three values | `"contribution"` | how the standalone `share` segment AND the `sessions` list render their percentages (one denominator across the bar) — see §6 |
| `sessionContribution` | `--set-session-contribution <on\|off>` | `on`/`off` | `true` | whether the `session` (5h) segment folds in the composite `<contribution>/<total>%` |
| `attributionMetric` | `--set-attribution-metric <cost\|tokens>` | `cost`/`tokens` | `"cost"` | which delta drives cross-session attribution — see §6 |
| `sessionsShowModel` | `--set-sessions-show-model <on\|off>` | `on`/`off` | `true` | whether `--show-sessions` includes the per-model mix column — see §6 |
| `shareWarnPercent` | `--set shareWarnPercent <n>` | 0-100 | `34` | this session's share of window consumption where the 5h segment's contribution figure turns yellow |
| `shareDangerPercent` | `--set shareDangerPercent <n>` | 0-100 | `67` | this session's share of window consumption where the contribution figure turns red |
| `branchMaxChars` | `--set-branch-max <n\|off>` (also `--set branchMaxChars <n>`, 0-200) | integer ≥ 0, or `off`/`0` for no limit | `20` | truncates the displayed branch name |
| `alarmAfterSeconds` | `--set-alarm <seconds\|off>` (also `--set alarmAfterSeconds <n>`, 0-604800) | integer ≥ 0, or `off`/`0` to disable | `120` | waiting time before the loud alarm chip |
| `alarmStates` | `--set-states alarm <states\|reset>` | comma-separated from `idle, permission, working, subagents`; `reset` restores the default | `["idle","permission"]` | which activity states can trigger the alarm |
| `idlePhases` | `--show-phases` / `--add-phase` / `--set-phase` / `--remove-phase` / `--reset-phases` (or `--phases <subcommand>`) | array of `{after, bg, text, bold}`; see §2 | 30s amber/bold, 120s red/bold | the idle-painting ladder, any number of phases |
| `idleBackgroundAfterSeconds` | `--set-bg-after <seconds>` (also `--set idleBackgroundAfterSeconds <n>`, 0-604800) — legacy, folds into phase 1 | integer ≥ 0 (`0` disables all painting) | `30` | waiting time before phase 1 (`idleBackground`) appears |
| `idleBackground` / `idleBackgroundUrgent` | `--set-bg <colour\|off>` / `--set-bg urgent <colour\|off>` — legacy, fold into phases 1/2 | colour name or `#rrggbb`, or `off` | not shipped (phase 1/2 colours come from `idlePhases`) | override just the first two phases' colours |
| `idleBackgroundStates` | `--set-states background <states\|reset>` | comma-separated from `idle, permission, working, subagents`; `reset` restores the default | `["idle","permission"]` | which activity states get the tint |
| `usageFetch` | `--set-usage <on\|off>` | `on`/`off` | `true` | toggles the per-model weekly usage fetch (reads the Claude token) |
| `warnPercent` | `--set warnPercent <n>` | 0-100 | `50` | % where bars turn yellow |
| `dangerPercent` | `--set dangerPercent <n>` | 0-100 | `80` | % where bars turn red |
| `cacheTtlSeconds` | `--set cacheTtlSeconds <n>` | 5-3600 | `60` | how often usage is re-fetched |
| `staleAfterSeconds` | `--set staleAfterSeconds <n>` | 30-86400 | `600` | when usage is marked stale (`~`) |
| `chipWidth` | `--set chipWidth <n>` | 0-40 | `0` | inner width of the activity chip; `0` = natural (one space each side), positive centres to that width |
| `barCells` | `--set barCells <n>` | 3-40 | `10` | cells in each usage bar |
| `activeMaxAgeSeconds` | `--set activeMaxAgeSeconds <n>` | 60-604800 | `3600` | when a stale "working" chip disappears |
| `gitTimeoutMs` | `--set gitTimeoutMs <n>` | 50-5000 | `500` | timeout for each git call, in ms |
| `resetWarnMinutes` | `--set-reset-warn <warn> <urgent>` (also `--set resetWarnMinutes <n>`, 0-1440) | integer ≥ 0 (`0` disables) | `30` | minutes before a session reset when the countdown turns yellow |
| `resetUrgentMinutes` | `--set-reset-warn <warn> <urgent>` (also `--set resetUrgentMinutes <n>`, 0-1440) | integer ≥ 0, capped at `resetWarnMinutes` | `15` | minutes before a session reset when the countdown turns red |
| `sessionSampleSeconds` | `--set sessionSampleSeconds <n>` | 5-3600 | `60` | minimum time between cross-session usage samples — see §9 |
| `sessionSampleKeepSeconds` | `--set sessionSampleKeepSeconds <n>` | 60-604800 | `21600` | how long a session's usage samples are kept |
| `sessionLiveSeconds` | `--set sessionLiveSeconds <n>` | 30-86400 | `900` | how recently a session must have sampled to still count as live |
| `burnWindowSeconds` | `--set burnWindowSeconds <n>` | 30-86400 | `600` | span used to compute a session's tokens-per-minute burn rate |
| `gitCacheSeconds` | `--set gitCacheSeconds <n>` | 0-60 | `3` | how long a working dir's git status is reused before re-forking git (`0` = never cache) |
| `agentsLiveSeconds` | `--set agentsLiveSeconds <n>` | 5-3600 | `60` | how recently a background agent must have written to still count as running — see §4e |
| `customTtlSeconds` | `--set customTtlSeconds <n>` | 1-86400 | `60` | how long a custom segment's command output is reused before a background refresh |
| `customOutputMaxBytes` | `--set customOutputMaxBytes <n>` | 256-1048576 | `65536` | most a command may print before it is cut off and killed — a runaway endpoint otherwise costs memory on every render |
| `customValueMaxChars` | `--set customValueMaxChars <n>` | 8-4096 | `512` | most characters of a command's output cached and shown |
| `nudgeMaxPerHour` | `--set nudgeMaxPerHour <n>` | 0-200 | `5` | machine-wide ceiling on task nudges per hour across every task; when it trips, no nudge job is created and `--show-config` says so |
| `usageFetchSeconds` | `--set usageFetchSeconds <n>` | 30-86400 | `900` | cadence for the **per-model** weekly buckets only; the `5h`/`week all` numbers come from Claude Code's own payload every render and need no fetch |
| `customTimeoutMs` | `--set customTimeoutMs <n>` | 50-30000 | `2000` | timeout for a custom segment's command, after which its process group is killed |
| `sessionsMax` | `--set sessionsMax <n>` | 1-20 | `4` | max sessions the `sessions` segment lists before collapsing the rest to `+N` |
| `bannerLines` | `--set-bg lines <auto\|all\|1,2>` | `auto`, `all`, or 1-based line numbers | `"auto"` | which lines the idle tint covers; `auto` leaves a row of only clickable reminders unpainted |
| `phaseAlerts` | `--alerts <on\|off>` | boolean | `true` | master switch for phase actions: the desktop notification and any `run=` command |
| `alertWhenFocused` | `--alerts focused <on\|off>` | boolean | `false` | notify even when the terminal is the focused window; off = stay quiet, since the bar has already said it |
| `paneReserveColumns` | `--set paneReserveColumns <n>` | 0-20 | `1` | columns held back at the right edge. Raise it when a line ends in `…` although nothing is missing from it — the pane is narrower than `COLUMNS` says. |

Run `--show-config` to see the current values, or `--help-text` for this same reference
generated live from the code.

### 4a. Layout — lines, order, and what survives a narrow pane

Nothing about the arrangement is hardcoded. `layout` is a list of lines, each a list of
segment ids; Squirrel prints exactly what it describes — one line, two, or four. Placeable
segment ids are: `context session weekly share share-life sessions` (line 1 as shipped) and
`account repo git model effort flags sessionTokens cost duration lines pr chip` (line 2).
`tokens`, `resets`, `permodel`, `branch` and `ahead` are **not** placeable — they are details
*inside* another segment, switched with `--show-segment` and shed by priority.

**Never write `layout` or `segmentPriority` by hand.** Use `--layout`, which validates first
and edits only the layout:

- `--layout` / `--layout show` — the layout line by line, each segment with its priority,
  plus unplaced segments and the detail priorities
- `--layout "move <segment> <line> [position]"` — a line number one past the end creates a
  new line; an omitted position appends. Lines and positions count from 1.
- `--layout "line <n> <segment> <segment> ..."` — set one whole line
- `--layout "swap <a> <b>"` — exchange two lines
- `--layout "priority <segment|detail> <n|always>"`
- `--layout "reset"` — shipped layout, priorities discarded

An unknown segment id, an out-of-range line, or an edit that would leave `account` or `model`
unplaced is refused and **nothing is written** — report the refusal to the user, do not retry
with a guess.

Shipped priorities (higher survives longer; `always` = never dropped):
`account model context session weekly chip` = always · `repo git` 80 · `sessionTokens` 70 ·
`effort` 60 · `flags` 50 · `cost share share-life sessions` 40 · `duration` 30 · `lines` 20 ·
`pr` 10. Details: `abbrev` 60 · `bars` 50 · `resets` 40 · `tokens` 30. Under pressure Squirrel
first narrows the bars (10 → 6 → 4 → 3 cells), then sheds by priority, lowest first.

### 4b. Per-segment styles

`--style set <segment> <field>=<value> ...` overlays only the fields given; other fields of
that segment, and every other segment, are untouched. `auto` clears one field, `--style clear
<segment>` drops a segment's whole style, `--style reset` drops all of them. Validation
refuses an unknown segment, an invalid colour, an out-of-range opacity, or an unknown
separator, and writes nothing — report the refusal rather than retrying with a guess.

**State this whenever a user asks about colours:** a warning always breaks through. `fg` and
`opacity` recolour anything decorative (the all-clear green, a clean branch's grey, the dimmed
token counts, an account's colour) but never yellow or red. Usage bars still turn yellow at
`warnPercent` and red at `dangerPercent`, the session-reset countdown still turns yellow/red,
a dirty branch still shows yellow. So `--style set git fg=#ff8800` does give an orange branch —
one that still goes yellow when dirty. `force=on` overrides even that; only suggest it when
the user clearly wants total control, and say what they are giving up.

`opacity` alpha-blends toward the idle phase banner colour while one is painted, and falls
back to the terminal's dim attribute when no banner is painted or when the colour is a named
ANSI colour (whose real RGB is the terminal's choice).

**If a user says their segment background stopped working:** check whether a phase banner or
a bar rule is painting. The banner's background is applied last and covers a segment's own
`bg`; the style is intact, just underneath. Segment foreground colours still show through.

`sep` sets the punctuation *before* a segment — the fix when a reordered layout inherits the
wrong separator from its group.

### 4c. Bar rules — painting the whole bar on a condition

`--rules add <condition> bg=.. text=..` paints the entire status line while the condition
holds. A condition is exactly `<metric> <op> <value>` (ops `>=` `<=` `==` `!=` `>` `<`) or a
bare `<metric>` for true/false. Run `--show-rules` for the live metric list rather than
guessing a metric name — an unknown one is refused.

**Never write `barRules` by hand.** `add` needs `bg`, `text`, or both; `set` overlays fields
on an existing rule; `remove` drops one of the user's rules; `reset` clears them all. A
refused edit writes nothing.

Say these three things when a user asks how rules behave:
- The **idle ladder ships as the default rules** — one mechanism, not two. `/squirrel-phases`,
  `--set-bg` and `--set-bg-after` still edit it; `--show-rules` shows it as `[ladder]` rows.
- **Which rule wins:** the last matching rule, unless one has a higher `priority`. A rule the
  user adds beats the idle tint by default, because the ladder comes first. Do not reason
  this out for them — run `--show-rules`, which marks the winner `<== WINNING NOW`. Rules
  reading values that only exist while the bar renders are marked as uncheckable rather than
  guessed, and the listing warns that the real winner may differ.
- **`pending.tools` and `pending.agents`** come from the tail of the session transcript, not
  from the hook events, and are the honest answer to "is anything still running?". They are
  `None` — so no condition over them fires — whenever the transcript cannot be read. `state`
  is sharpened by the same reading: an outstanding call can prove a session is not idle, but
  the transcript is never allowed to invent idleness the hooks did not report. Useful
  conditions: `pending.agents > 0` (agents working), `state == waiting-you` (Claude has
  finished and is waiting on the human).
- **A metric with no value never matches, `!=` included.** Unknown is not "different from" —
  the bar must not paint because data is missing.

Nothing is evaluated as code: conditions are parsed into (metric, operator, value), never
executed. A condition that does not parse is reported by `--show-rules` and `--show-config`
and ignored; the bar keeps rendering.

### 4d. Custom segments — and reminders

One mechanism. A reminder is a custom segment with `every` and `ack`; there is no reminder
type to look for. The two the user asked for, verbatim:

    --custom "add water every=30m ack=on shared=on text=💧 drink water"
    --custom "add break every=2h ack=on shared=on text=🧘 take a break"

`text=`, `command=` and `when=` swallow the rest of the line — put one of them LAST, and use
a second `set` for another. `add` needs `text=` or `command=`, never both.

**When the user says they have done the thing** — "drank it", "had a break", "done" — run
`--ack <id>` (or `--ack all`). For "not now" / "in ten minutes", run `--snooze <id> <minutes>`
instead: snooze postpones, ack clears the full interval. Only segments with `ack=on` can be
either.

`shared=on` puts the acknowledgement in the cross-profile store, so it clears in every session
and every account alias at once. Suggest it for anything about the person (water, breaks,
posture) and leave it off for anything about one project.

**`command=` does nothing until `--custom-commands on`.** Say so plainly when a user adds one
— `--show-custom` marks it `BLOCKED`. Before enabling it, tell them what it means: Squirrel
will run that command in the background on a timer, and since this plugin's own commands edit
their config, anything able to edit the config could cause a command to run. Do not enable it
speculatively. A command never runs while the bar is drawing, so the first render after adding
one shows nothing and the next shows the value — that is expected, not a fault.

### 4f. Clicking a reminder

`--click on` plus `--custom set <id> click=ack` (or `click=snooze:<mins>`) makes a reminder
dismissible by clicking it. The segment needs `ack=on` first.

**Say all of this before enabling it, not after:**
- It runs a **small local web server** — the only way a click can do anything, since Claude
  Code has no click event and a hyperlink can only open a URL.
- **Nothing binds** until the feature is on AND a segment declares a click action AND the
  terminal can follow the link. Any one missing and no socket exists.
- **Clicking opens a browser tab.** Unavoidable; the tab says what happened and closes
  itself. If that would annoy them, do not enable it — `/squirrel-done` needs no tab.
- It works in **VS Code and Cursor**, currently not in standalone terminals (upstream
  regression, closed as not planned). `--click on` reports which case they are in; read the
  reply out rather than assuming it worked.

**If a user says clicking does nothing**, run `--show-config` and read the `click-to-ack`
line: it names the unmet condition rather than leaving you to guess.

**Switching it off is immediate** — `--click off`, or removing a segment's `click=`, revokes
URLs already on screen; the listener re-checks every request. And say plainly that the
listener's lifecycle is **untested on Windows**, unlike the rest of the plugin.

The listener can **only** acknowledge or snooze an id already in their config — it cannot run
a command. If a user asks for a click that does something else, the answer is no.

### 4g. Watcher tasks — the general mechanism

A **task** is a condition plus an action. When the condition holds, Squirrel writes a **job**
to a queue; a reader picks it up and acts. Squirrel itself never acts, and never on the render
path — which is why what a job can do is not limited by what Squirrel knows how to do.

**Tasks act; rules paint.** Do not be tempted to unify them. Bar rules resolve by priority
among all matches on every render; a task fires once, moves through a durable queue, and takes
a cooldown. The idle phase ladder already ships *as* bar rules, so "one painting path" is a
property this codebase currently holds, and folding painting into tasks would quietly break it
while looking like consolidation.

#### The procedure — follow it in this order

1. **Read the vocabulary.** `--show-metrics` prints every metric with its live value and
   meaning, the verbs, and every task field. Read it; do not recall a metric name. The
   installed version is the only one that can accept or refuse a condition.
2. **Pick the metrics** that express what the user actually means. "When I have been away a
   while" is `state == waiting-you and idle.secs > 1800`, not a timer.
3. **Write the condition** in the grammar: `<metric> <op> <value>`, or a bare `<metric>`.
   Operators `>=` `<=` `==` `!=` `>` `<`. **There is no `and`** — one comparison per task, and
   a second comparison is refused rather than half-read. Usually you do not need one: pick the
   metric that already means what you want. `idle.secs` is only set while the session is
   genuinely waiting on the *user* — a session waiting on tools or agents has no value at all
   — so `idle.secs > 1800` already means "idle for half an hour and not busy", and needs no
   second clause. If a request truly needs two conditions, say so plainly rather than
   inventing syntax.
4. **Enable the action** — `--task-actions notify on`. Off by default; a task whose action is
   disabled is refused rather than created dormant.
5. **Set a cooldown.** Minimum 60s, default 1800s. This is the brake; there is no other.
6. **Create it** — `--task add <id> when='...' do=... text='...' cooldown=...`.
7. **Dry-run it** — `--task test <id>`. **You cannot see the user's bar.** This is how you
   check your own work instead of asserting it: it says whether the condition holds right now
   and, if not, the metric's actual value.
8. **Show the user what you built**, in plain words: what it watches, what it will do, at most
   how often, and how to remove it. A watcher that acts on someone's machine is never created
   silently.

#### What to tell them about cost and safety

- **Every nudge is a real request on their account** — delivering one runs a one-shot
  `claude -p` on the cheapest model. `nudgeMaxPerHour` (default 5) caps that machine-wide.
- **Dismissing a nudge buys them the cooldown, not a second.** Cancelling suppresses that
  firing *and* starts the cooldown; the task itself is untouched.
- **Unknown never fires.** A condition over a metric with no value does not match, `!=`
  included.
- **Loop safety is bounded, not prevented.** Cooldowns stop the loops we anticipated; a
  `[squirrel]` marker on every nudge lets a task refuse to answer a nudge with a nudge; the
  ceiling bounds the rest. Nothing observes causality — say that plainly if asked.

#### Worked examples — read these for the mechanism, not to copy

| The user wants | The task |
|---|---|
| *"after 30 minutes idle, send a status check unless I cancel it"* | `add status when='idle.secs > 1800' do=notify text='tldr status check' cooldown=1800 warn=600 cancel=on` |
| a warning before the weekly limit bites | `add weekly when='weekly.pct >= 85' do=notify text='weekly limit nearly gone' cooldown=3600` |
| to stop leaving work uncommitted | `add dirty when='git.dirty' do=notify text='uncommitted work still open' cooldown=7200` |
| to know when a long agent run finishes | `add agents when='pending.agents > 0' do=notify text='agents still running' cooldown=1800 warn=300` |
| a spend alert on one session | `add spend when='cost.usd > 20' do=notify text='this session has passed $20' cooldown=3600` |

The first is the user's own case. The rest are there to show the mechanism is general: any
metric, any operator, any of the verbs. If a request needs a metric that does not exist, that
is a new `METRICS` entry in the code — never a second condition language, and never a
workaround in the text.

## 5. Recipes

Common requests, mapped to the exact command(s) to run:

- "why does it say idle when agents are running?" → that is what the `agents` segment is for; it shows `⧗ N agents` for BACKGROUND agents the hooks cannot see. If it shows nothing, either none are running or the internal path it reads is not there — it degrades silently by design, so do not promise it will always appear.
- "remind me to drink water every 30 minutes" → `--custom "add water every=30m ack=on shared=on text=💧 drink water"`
- "and a break every 2 hours" → `--custom "add break every=2h ack=on shared=on text=🧘 take a break"`
- "can I just click it?" → `--click on`, then `--custom set water click=ack`; read out whether their terminal can follow the link, and that clicking opens a tab
- "drank it" / "done" / "had my break" → `--ack water` (or `--ack all`)
- "not now, ask me in 10" → `--snooze water 10`
- "show me the branch's CI status" → `--custom "add ci every=60s command=<their command>"` then explain `--custom-commands on`
- "add a note that only shows on main" → `--custom "add note text=SHIPPING"` then `--custom "set note when=git.branch == main"`
- "stop reminding me about water" → `--custom "remove water"`
- "turn the bar purple when my weekly usage is high" → `--rules "add weekly.pct >= 80 bg=#7c3aed text=white bold=on"`
- "warn me when this session gets expensive" → `--rules "add cost.usd > 5 bg=#b3261e text=white"`
- "highlight when I have uncommitted work" → `--rules "add git.dirty text=yellow"`
- "only on the main branch" → `--rules "add git.branch == main text=cyan"`
- "why is my bar purple?" → `--show-rules` (shows every rule, which are the idle ladder, and any being ignored)
- "get rid of my rules" → `--rules reset` (the idle ladder stays; use `--reset-phases` for that)
- "make the branch orange and bold" → `--style set git fg=#ff8800 bold=on`
- "fade the less important bits" → `--style set duration opacity=0.5`, `--style set lines opacity=0.5`
- "the separator before the cost looks wrong" → `--style set cost sep=dot` (or `bar`/`space`/`none`)
- "give the account a coloured background" → `--style set account bg=#222222`
- "put my colours back" → `--style clear <segment>`, or `--style reset` for all of them
- "put the branch on its own line" → `--layout "move git 3"` (add `--layout "move repo 3"` if the project name should go with it)
- "I only want one line" → `--layout "line 1 account model context session weekly"` then `--layout "line 2"` to empty the second (or move the rest onto line 1 first, whatever they want kept)
- "swap my two lines round" → `--layout "swap 1 2"`
- "put the cost right after the account name" → `--layout "move cost 2 2"`
- "keep the reset times even on a narrow terminal" → `--layout "priority resets 95"`
- "drop the git info first when it is tight" → `--layout "priority git 5"`
- "never lose the PR status" → `--layout "priority pr always"`
- "put my layout back how it shipped" → `--layout "reset"`
- "too much information / too wide" → `--show-segment cost off`, `--show-segment lines off`, `--set-branch-max 12`
- "I keep missing when it needs me" → `--set-bg-after 15`, `--set-alarm 60`
- "the branch name is too long" → `--set-branch-max 12`
- "label this account" → `--set-nick <name>` (and `--set-color <colour>` for a distinct colour); this only labels the account the user is currently logged in as
- "stop reading my token / no per-model usage" → `--set-usage off`
- "make the warning colours kick in earlier" → `--set warnPercent 40`, `--set dangerPercent 70`
- "hide git entirely" → `--show-segment git off` (the repo/project name segment is separate — `--show-segment repo off` if that should go too)
- "give this project its own colour" → `--set-repo-color <colour>`
- "only alarm when it needs me, not when merely idle" → `--set-states alarm permission`
- "make the idle warning much more obvious" → `--set-bg urgent #ff0000`
- "remind me before my session limit resets so I can compact first" → `--set-reset-warn 30 15`
- "add a purple phase after 10 minutes" → `--add-phase 600 bg=#7c3aed text=white`
- "I prefer black text on the warning banner" → `--set-phase 30 text=black`
- "the banner text is too heavy / not bold enough" → `--set-phase 30 bold=off` (or `bold=on`); same for phase 120
- "make the chips all the same width again" → `--set chipWidth 13` (any positive number centres uniformly; `0` is natural)
- "no colour at the first step, just keep the text change" → `--set-phase 30 bg=off`
- "drop the middle step" → `--remove-phase 120`
- "put the idle ladder back how it was" → `--reset-phases`
- "the sessions table is unreadable / it's wrapping" → it already sheds columns to fit; if they want it all, `--show-sessions "<id> wide"` and warn them it will wrap
- "which session is eating my limit?" / "which of my sessions should I stop?" → `--show-sessions` (or `/squirrel-sessions`) — ranked by attributed 5h points (ground truth, see §6), coverage and cost/tokens shown alongside
- "a session shows `?` instead of a percentage" / "is that a bug?" → not a bug — `?` means Squirrel hasn't yet observed a real utilisation reading covering that session's activity (e.g. right after upgrading to this version, or before the account was ever watched), never a claim that it used nothing (§6). It resolves on its own as more samples accumulate
- "a session shows almost no tokens but a large 5h pts figure" / "is that a bug?" → not a bug — the figure comes from the account's own utilisation, which already includes subagent work that never touches a session's own token counters (§6)
- "which model was this session actually using?" / "did switching models change what it cost?" → `--show-sessions` / `/squirrel-sessions`, `model mix` column (§6); turn it off with `--set-sessions-show-model off` if it's noise
- "does the cost/share number account for the promo credit / new pricing?" → yes automatically — the window figures read the account's own percentages directly, and the lifetime ratios cancel out a pricing change the same way they always did, so neither needs a Squirrel update (see README "How the numbers are derived")
- "how much of the limit is THIS session using?" / "am I the one burning the window?" → nothing to do — the `session` (5h) segment already folds this in as `19pp/90%` once this session has real attributed data; point the user at it (§2/§6)
- "I don't want the 5h segment to do that composite thing" / "just show the plain percentage" → `--set-session-contribution off`
- "make the 'am I the problem' colour more/less sensitive" → `--set shareWarnPercent <n>`, `--set shareDangerPercent <n>`
- "show me each session's share as its own segment too" → `--show-segment share on` (add `--show-segment sessions on` for the full ranked list on the bar itself)
- "I want the raw percentage on the standalone share segment, not the contribution" → `--set-share-format share`
- "show me both numbers on the standalone share segment" → `--set-share-format both`
- "rename the mine/share label" → `--set-label share <text>` (`--set-label share reset` to restore `mine`)

## 6. Cross-session usage attribution

If the user runs several Claude Code sessions on one account, each session's own status JSON
only knows its own totals — the 5-hour and weekly limits are account-wide. Squirrel pools
usage across sessions in a small shared store (deliberately outside `CLAUDE_CONFIG_DIR`, so
every session on the account finds it regardless of profile or shell alias) at
`~/.squirrel/sessions/<session>.json` — one flat file per session, holding a session id, an
optional repo name, and a bounded ring of samples: `[timestamp, cumulative_tokens,
cumulative_cost, model, account, account_utilisation_pct, account_resets_at]`.
`account_resets_at` is when the window that percentage describes ends — it is what lets a
later reader tell a current reading from one a stale session keeps repeating, which is how a
frozen observer once turned a 3% account total into a reported 183 points. `account` and
`account_utilisation_pct` are per-sample (like `model`) because a session can `/login` to a
different account mid-conversation — Claude Code immediately charges the existing context to
the new account when that happens, and Squirrel needs to know which slice of a session's
history belongs to which account. A sample written before one of these fields existed just
has that field as `None` — never dropped, never crashed on, never treated as a `0`. (Older
installs may still have per-account-directory files from before this layout; Squirrel reads
those in place too, never migrating or deleting them.)

**Ground truth, not a reconstruction.** Squirrel used to estimate a session's slice of the
5-hour window from its own cost/token delta, as a ratio against every other live session's
delta. That broke in ways users actually hit: subagent work never touches a session's own
token counters, and a re-login mid-session moves real cost onto a new account's window with
no session-local delta to explain it. Since Claude Code already computes the account's real
utilisation percentage, Squirrel now samples *that* directly and attributes its delta to
whichever session(s) were active during it — exact when one session was active, split by cost
when several were (cost is used only to divide up an already-known delta here, never as the
measurement itself), and `?` (unknown) when no observation covers the interval at all — e.g.
before Squirrel started watching this account, never a confident `0`. Rule of thumb for the
user: **`?` means "we don't know yet," not "zero."** It resolves itself as Squirrel
accumulates more samples with a real utilisation reading attached.

**This only affects the *window* (5-hour) figures.** `share-life` and the `life` columns in
`--show-sessions` have no ground-truth equivalent — there's no such thing as a "lifetime
utilisation percentage" since the window resets every 5 hours — so they're unchanged: still a
ratio of `cost.total_cost_usd`/`context_window` tokens, still governed by `attributionMetric`
(`"cost"` default, `"tokens"` for the old behaviour, set with `--set-attribution-metric
<cost|tokens>` / `/squirrel-attribution-metric`). `attributionMetric` no longer has any effect
on the window-based numbers below — there's no metric to switch since they read the account's
own percentages directly, not a session-local proxy.

- **The `session` (5h) segment itself** (§2) is the main place this shows up: once this
  session has real attributed data, it stops rendering just the plain account total (`90%`)
  and starts rendering a composite, `19pp/90%` — the account has used 90% of the window, and
  *this* session's own attributed share of the account's own utilisation is 19 of those
  points, read straight off Claude's usage data (no multiplication or estimation left to do).
  `<total>` keeps the normal `warnPercent`/`dangerPercent` colour ('how full is the window?');
  `<contribution>` is coloured on its own scale, `shareWarnPercent`/`shareDangerPercent`
  (default 34/67 — 'am I the problem?', relative to every other live session's attributed
  points), so a session responsible for most of the burn reads red even while the window's
  total usage is still green. A `~` marks the contribution figure whenever it's estimated
  (shared with other sessions in some interval, or partially gapped) rather than exact.
  Controlled by `sessionContribution` (default on); falls back to the plain `<total>%`
  whenever the composite isn't genuinely known — this session not (yet) live, it being the
  *only* live session (trivially 100%, nothing to attribute against), or this session's own
  attribution being entirely unknown. Never a guessed or invented figure.
- `--show-sessions` / `/squirrel-sessions` — the full table: every live session (one that has
  rendered its status line within `sessionLiveSeconds`) with a **`5h pts`** column (this
  session's attributed percentage points — unmarked for exact, `~` for estimated, `?` for
  unknown), a **`coverage`** column (how much of the window Squirrel can actually vouch for
  this session, e.g. `93m/5h`), 5h-window cost and tokens (secondary, informational — no
  longer drive the ranking or a `%`), lifetime cost/tokens/share (still `attributionMetric`-
  driven, unchanged), burn rate (`$/min`, tokens/min alongside), and age. **Sorted by
  attributed points**, always — `attributionMetric` no longer affects this table's ranking.
  Trailing notes explain what `~`/`?` mean in the `5h pts` column, and (separately) which
  metric drives the `life %` column. It also shows a `model mix` column (e.g. `Opus 5 82% ·
  Sonnet 5 18%`) — that session's own cost (or tokens, matching `attributionMetric`) split by
  model — a ratio of the session's OWN usage, never weighted against the account limit; for
  the exact, API-reported per-model weekly usage, point the user at the `permodel` segment
  instead (§2), a different, complementary number. Toggle the column with
  `sessionsShowModel` / `--set-sessions-show-model <on|off>` / `/squirrel-sessions-show-model`.
- `share` / `share-life` / `sessions` segments (§2) put the same picture on their own line
  segments instead of (or as well as) folded into `session`; all three are off by default
  and turned on individually. `share` renders under the label `mine`, deliberately not `5h`
  — the `session` segment already renders a `5h ...%`/`5h N/M%` fragment for the
  account-wide bucket on the same line, and two segments both saying `5h` made it impossible
  to tell which was which. Its format is controlled the same way: `--set-share-format share`
  shows the plain 21% (this session's share of every live session's attributed points, not a
  cost ratio) instead of the contribution, `both` shows both; same fallback rule, plus `?`
  when this session's own attribution is unknown. `share` is coloured the same way as the
  `session` composite (contribution/share on the `shareWarnPercent`/`shareDangerPercent`
  scale, the account total on the usual `warnPercent`/`dangerPercent` scale) — it renders
  plain/uncoloured only for the `?` (unknown) case. `share-life` is untouched by any of this
  (see above — no ground truth for a lifetime percentage).
- Marking *this* row in `--show-sessions` needs the Claude Code host to substitute
  `${CLAUDE_SESSION_ID}` into the command (supported from Claude Code ≥ 2.1.9); on older
  versions the table still renders correctly, just without a marked row.
- **See the README's "How the numbers are derived" section** for the full attribution model
  (the exact/estimated/unknown table) — point the user there if they ask why Squirrel does it
  this way, or why a session that's clearly done heavy subagent work shows `?` right after
  upgrading (answer: Squirrel hasn't sampled a real utilisation reading for it yet — it needs
  a little wall-clock time after this version starts running before `?` resolves into a real
  number).

## 7. Troubleshooting

- **Bar not appearing**: run `--setup "${CLAUDE_PLUGIN_ROOT}"`, wait a few seconds (it
  refreshes every 5s), and confirm a working Python 3 is on the user's `PATH` — on Windows note that `python3` may be the Microsoft Store alias stub rather than a real interpreter (see the README's Windows notes).
- **Activity chip stuck**: the hooks that update it activate automatically once the plugin
  is installed; a waiting chip clears on the user's next prompt submission or the next time
  Claude acts (see limitations below).
- **Per-model usage missing**: run `--set-usage on`; it also needs the user to be logged
  into a Claude subscription account (the API token comes from Claude Code's own login).
- **Usage shows a `~`**: the cached value is stale — either it's older than
  `staleAfterSeconds` or the last background fetch attempt failed (no token, network, or
  HTTP error).
- **Everything looks plain / no colour**: the terminal may not support truecolor escape
  sequences.
- **No sessions show up in `--show-sessions` / the `sessions` segment**: a session only
  appears once its status line has rendered at least once and while it stays within
  `sessionLiveSeconds` of its last render; a brand-new or long-idle session won't be listed.

## 8. Limitations — state these honestly

- There is no keystroke/typing-presence detection in Claude Code. A waiting ("idle" /
  "needs you") state clears when the user submits their next prompt or Claude takes its
  next action — not the moment the user starts typing.
- The idle→working flip can lag by up to the status line's refresh interval (5 seconds,
  set by `--setup`), since the bar only redraws on that cadence.
- `--show-sessions`' current-session marker depends on the Claude Code host substituting
  `${CLAUDE_SESSION_ID}` (≥ 2.1.9); on older hosts the table renders fully, just unmarked.
- A session's window/lifetime figures are as fresh as its last sample (up to
  `sessionSampleSeconds` old) and as fresh as that *other* session's last status-line render
  — Squirrel cannot poll a session that isn't currently rendering.

## Untrusted text

Repo names, branch names and anything else Claude Code hands the status line are sanitised
before rendering: terminal escape sequences (CSI, OSC including OSC-8 hyperlinks and OSC-52
clipboard writes, and their 8-bit forms), control characters, and Unicode bidi overrides are
stripped. Text is stripped **in place**, never truncated, so a hostile name shows as its
harmless remainder rather than silently disappearing. Newlines can never survive — they would
break the line-count contract. Emoji, CJK, RTL scripts, combining marks and zero-width joiners
are preserved.

If a user asks why a repo or branch name looks different from what they expect, this is the
first thing to check: something in the name was a control character or an escape sequence.

## 9. Privacy

The only network call Squirrel makes is a read of the user's Claude Code OAuth token,
sent solely to `https://api.anthropic.com/api/oauth/usage` to fetch per-model weekly usage.
The token is never written to the cache, stdout, or logs. `--set-usage off` disables this
fetch entirely; the 5-hour and weekly-all bars keep working from Claude Code's own status
data either way.

The cross-session shared store (§6) is local-only — nothing in it is ever sent anywhere. It
holds only session ids, repo names, timestamps, token counts, costs, and utilisation
percentages; never prompts, file contents, or paths beyond a repo name. The per-sample
`account` field is a one-way hash of the account email (the same hash the old
per-account-directory layout used as a folder name), so the store itself never contains the
plain email address.
