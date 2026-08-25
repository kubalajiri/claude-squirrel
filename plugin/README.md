# Squirrel

A Claude Code status line built for ADHD brains: context, usage limits, git state,
cross-session attribution, and an alarm you cannot miss when Claude is waiting on you.

You don't need to learn any of the commands below to use it. Just ask, in plain language —
*"hide the cost"*, *"the branch is too long"*, *"warn me sooner when it's waiting"* — and
Claude (via the bundled `squirrel` skill) runs the right command for you.

Two lines, always visible, that shrink gracefully as your terminal narrows:

```
ctx ▰▰▰▰▱▱▱▱▱▱ 42% (84k/200k) │ 5h ▰▰▰▰▰▰▰▰▰▱ 19pp/90% ↻ 2h31m │ week all ▰▱▱▱▱▱▱▱▱▱ 4% · Fable ▱▱▱▱▱▱▱▱▱▱ 0% ↻ Thu 20:00
demo · demo-repo · ⎇ main · Test Model │ tok 25k · $0.50 · 1m · +1/−0 │ [ ⚙ working ]
```

`19pp/90%` there reads: the account has used 90% of the 5-hour window, and *this session* is
responsible for 19 of those points — see [Multiple sessions](#multiple-sessions).

## Requirements

| Requirement | Why |
|---|---|
| Claude Code ≥ 2.1.153 | supplies `COLUMNS` to the status line process, used for the responsive layout |
| Python 3.10+ | runs the bundled script — standard library only, nothing to `pip install` |
| `git` on `PATH` | the branch / dirty / ahead segment; everything else works without it |

### Platforms

| Platform | Status |
|---|---|
| Linux | **supported** — developed and tested here; full test suite and install smoke test run on every change |
| WSL | **supported** — same as Linux |
| Windows (native) | **supported** — the test suite, a live render, the activity hooks, `/squirrel-setup` and the background usage fetch were each run against native Windows Python 3.13 on Windows 11 |
| macOS | **should work, untested** — see below |

**Desktop notifications** are per-platform and each is chosen at send time, first that works:
a **Windows toast** via `powershell.exe` (used on WSL too, and verified there), `notify-send`
on Linux **only when a notification daemon is actually running** — it exits 0 while printing a
`GDBus.Error` when there is none, so the exit status alone is not trusted — `terminal-notifier`
if installed, then `osascript` on macOS. `/squirrel-alerts test` sends one and tells you which
of them answered, or that the machine has none. The macOS path is written and unit-tested but
has not been run on a real Mac: see below.

**macOS** uses the same POSIX code paths as Linux everywhere Squirrel differs by platform
(file permissions, `pwd`-based home resolution, `os.replace`, the detached background child),
with no GNU-only command-line flag and no Linux-specific path. The one real difference is
`/proc`: two of the [click listener's](#clicking-a-reminder) staleness checks read it, and
on macOS they do not run — see that section for exactly what is and is not checked there.
Nothing else depends on it, and nothing else degrades. But nobody has run Squirrel on a Mac.
It is listed as untested rather than supported until someone has.

**Windows** notes, all of which Squirrel handles for you:

- The status line is installed as a `run.cmd` launcher rather than `run.sh`, with the full
  path of a working Python baked into it at `/squirrel-setup` time.
- `python3` is deliberately never invoked by name. On a default Windows install `python3` on
  `PATH` is the Microsoft Store *App Execution Alias* — a zero-byte stub that prints "Python
  was not found" and exits 9009. Squirrel resolves a real interpreter instead, and verifies it
  runs before using it.
- The slash commands and activity hooks run through `scripts/squirrel.sh`, so they need a
  POSIX shell. Git for Windows already provides one and is a
  [documented Claude Code prerequisite](https://code.claude.com/docs/en/setup.md) on Windows,
  so there is nothing extra to install. Without Git Bash the status line itself still renders;
  only the slash commands and the `[⚙ working]` chip would be unavailable.
- File permissions differ — see [Security](#security).

## Install

```
/plugin marketplace add kubalajiri/claude-squirrel
/plugin install squirrel@squirrel
/squirrel:squirrel-setup
```

**Run all three.** The first two install the plugin; the third writes the `statusLine` entry
into your settings, and it is not optional — Claude Code gives a plugin no way to install a
status line by itself, so without it you get the commands and the hooks and no bar. (Installed
commands are namespaced by the plugin: `/squirrel:squirrel-setup`, not `/squirrel-setup`.)

The activity hooks (the `[⚙ working]` / `[✓ idle]` chip) activate automatically the moment
the plugin is installed — no extra step. The bar itself appears within a few seconds of
running setup.

**Nothing on screen after setup?** The launcher runs `python3`, and macOS ships without it
until the Command Line Tools are installed — a launcher that cannot start renders nothing at
all. `python3 --version` should print 3.10 or later (`xcode-select --install` if it does not),
and `echo '{}' | ~/.claude/squirrel/run.sh` prints either two lines or the error behind the
blank bar.

## What the two lines show

**Line 1 — usage:**

| Segment | Shows |
|---|---|
| `context` | `ctx ▰▰▰▱▱ 42% (84k/200k)` — context-window used, as a bar and percentage |
| `tokens` | the `(used/window)` suffix on the context segment |
| `session` | the 5-hour rate-limit bucket, as a bar and percentage — `19pp/90%` when this session's own **contribution** to that usage is known, plain `90%` otherwise; see [Multiple sessions](#multiple-sessions) |
| `weekly` | the all-models weekly rate-limit bucket |
| `permodel` | per-model weekly buckets shown alongside the all-models one (needs `weekly` on) |
| `resets` | the `↻ <time>` reset countdown on the 5-hour and weekly buckets |
| `share` | *(off by default)* a standalone version of the same contribution figure — `mine 19pp/89%` — for anyone who wants it on its own line segment instead of (or as well as) folded into `session`. Configurable via `shareFormat` — see [Multiple sessions](#multiple-sessions) |
| `share-life` | *(off by default)* `life 22%` — this session's share of all live sessions' lifetime usage |
| `sessions` | *(off by default)* `A 52% · B 31% · ▸C 12% · D 5%` — every live session ranked by 5h-window share, current one marked `▸` — see [Multiple sessions](#multiple-sessions) |

A bar/percentage turns **yellow at `warnPercent`** (default 50%) and **red at
`dangerPercent`** (default 80%); below that it's green. A leading `~` on a percentage means
the value is stale — the cache is old, or the last background fetch attempt failed.

The 5-hour countdown itself also turns yellow at `resetWarnMinutes` (default 30) and red at
`resetUrgentMinutes` (default 15) as the reset approaches. That gives you a moment to
`/compact` just before the window resets, so the compaction spends capacity that would have
expired anyway and the fresh 5-hour window starts with a small context instead of a full one.

**Line 2 — identity, git, cost, activity:**

| Segment | Shows |
|---|---|
| `account` | account label — nickname if set, else email. Cannot be hidden. |
| `repo` | the current repo/project name, colour-hashed per repo |
| `git` | branch name, dirty marker (`*`), unpushed-commit count (`↑N`) |
| `branch` | just the branch-name text inside `git` (hiding it keeps the `*`/`↑N` markers) |
| `ahead` | just the `↑N` unpushed-commit marker inside `git` |
| `model` | the model's display name. Cannot be hidden. |
| `effort` | the effort level (e.g. high/medium/low), when the model reports one |
| `flags` | `thinking` / `fast` mode flags, when active |
| `sessionTokens` | total input+output tokens used this session |
| `cost` | total session cost in USD |
| `duration` | total session duration |
| `lines` | lines changed this session, `+added/−removed` |
| `pr` | the current PR/MR number and review state, when known |
| `agents` | `⧗ 2 agents` — background agents working right now, which the activity chip cannot see; renders nothing when none are running. See [Background agents](#background-agents) |
| `chip` | the activity chip — see below |

**Activity chip:**

| Chip | Colour | Meaning |
|---|---|---|
| `[⚙ working]` | green | Claude is actively working |
| `[✓ idle]` | yellow | the session is waiting, nothing pending |
| `[◀ needs you]` | red | Claude is waiting on a permission prompt or input |
| `[⧗ N subagents]` | cyan | N subagents are currently running |

If a waiting state lasts past the idle alarm threshold, the chip is replaced by a loud
bold white-on-red alarm — see [Idle alarm](#idle-alarm) below.

## Commands

Thirty-seven `/squirrel-*` commands. Type `/sq` in the command menu to filter down to just these.

| Command | Arguments | Effect |
|---|---|---|
| `/squirrel-setup` | — | installs the status line into `settings.json` and confirms the hooks are active |
| `/squirrel-help` | — | explains what the bar shows and the commands you're most likely to want |
| `/squirrel-config` | — | prints the current configuration and where it lives |
| `/squirrel-nick` | `<nickname>` | sets a short nickname for the account you're logged in as |
| `/squirrel-color` | `<colour\|#rrggbb>` | sets the status-line colour for the account you're logged in as |
| `/squirrel-default-color` | `<colour>` | sets the fallback colour for accounts with no colour of their own |
| `/squirrel-repo-color` | `<colour\|off>` | sets the colour used for the current repository |
| `/squirrel-show` | `<segment> <on\|off>` | shows or hides one status-line segment (`git`, `cost`, `tokens`, `pr`, …) |
| `/squirrel-custom` | `[show \| add \| set \| remove \| reset]` | your own segments: static text, a command's output, or a recurring reminder — see [Custom segments](#custom-segments) |
| `/squirrel-done` | `<id\|all>` | acknowledge a due reminder |
| `/squirrel-snooze` | `<id> <minutes>` | postpone a due reminder |
| `/squirrel-click` | `<on\|off>` | lets a reminder be dismissed by clicking it — see [Clicking a reminder](#clicking-a-reminder) |
| `/squirrel-click-terminals` | `<name,name\|any\|reset>` | overrides which terminals are treated as able to follow a status-line link |
| `/squirrel-custom-commands` | `<on\|off>` | allow custom segments to run their `command` (off by default) |
| `/squirrel-rules` | `[show \| add \| set \| remove \| reset]` | paints the whole bar when a condition holds — usage high, repo dirty, session idle — see [Bar rules](#bar-rules) |
| `/squirrel-metrics` | *(none)* | every metric a task or rule can use, with its live value and meaning — read this before writing a condition |
| `/squirrel-task` | `<add \| set \| remove \| test> <id> [field=value ...]` | create, edit, remove or **dry-run** one watcher task — see [Watcher tasks](#watcher-tasks-and-what-they-cost) |
| `/squirrel-tasks` | *(none)* | every task, its queue state, when it last fired, and the nudge budget |
| `/squirrel-task-actions` | `<verb> <on\|off>` | opt a task action in or out; everything is off until you do |
| `/squirrel-style` | `[show \| set \| clear \| reset]` | per-segment colours — fg, bg, bold, opacity, and the punctuation before a segment — see [Styling](#styling) |
| `/squirrel-layout` | `[show \| move \| line \| swap \| priority \| reset]` | views or surgically edits the layout — which segments sit on which line, in what order, and which survive a narrow pane — see [Layout](#layout) |
| `/squirrel-branch` | `<max characters>` | limits how many characters of the git branch name are shown (`0` = no limit) |
| `/squirrel-alarm` | `<seconds\|off>` | sets how long Claude may sit idle before the status line raises a loud alarm |
| `/squirrel-bg` | `[urgent] <colour\|off>` | sets the full-width warn-banner colour shown while a session sits idle; `urgent <colour\|off>` sets the alarm-bar colour |
| `/squirrel-alerts` | `[on\|off\|focused <on\|off>\|test]` | desktop notifications for a waiting session — the half of the alarm that reaches a monitor you are not looking at. `test` sends one now and says which notifier it used |
| `/squirrel-bg-after` | `<seconds>` | sets how many seconds of waiting before the warn banner appears |
| `/squirrel-phases` | `[add\|set\|remove\|reset] <seconds> [bg=..] [text=..]` | views or surgically edits the idle-phase ladder (any number of steps, not just the two above) |
| `/squirrel-states` | `<alarm\|background> <states>` | chooses which waiting states trigger the alarm or the background tint |
| `/squirrel-usage` | `<on\|off>` | turns the per-model weekly usage fetch on or off |
| `/squirrel-reset-warn` | `<warn minutes> <urgent minutes>\|off` | sets when the 5-hour session-reset countdown turns yellow/red |
| `/squirrel-set` | `<key> <number>` | sets any numeric threshold (colour thresholds, cache TTL, bar size, timeouts, …); run with no arguments to list every threshold and its current value |
| `/squirrel-sessions` | `[wide]` | prints a table of every live session on this account — which one is consuming the shared 5-hour limit — see [Multiple sessions](#multiple-sessions) |
| `/squirrel-label` | `<segment> <text\|reset>` | overrides the display label of `share` or `share-life` (e.g. rename `mine`); `reset` restores the shipped label |
| `/squirrel-share-format` | `<contribution\|share\|both>` | chooses how the standalone `share` segment renders — see [Multiple sessions](#multiple-sessions) |
| `/squirrel-session-contribution` | `<on\|off>` | turns the composite `<contribution>/<total>%` in the `session` (5h) segment on or off |
| `/squirrel-attribution-metric` | `<cost\|tokens>` | chooses which delta drives cross-session attribution — dollar cost (default) or tokens — see [Multiple sessions](#multiple-sessions) |
| `/squirrel-sessions-show-model` | `<on\|off>` | shows or hides the per-model mix column in `/squirrel-sessions` — see [Multiple sessions](#multiple-sessions) |

## Configuration

**The file is optional** — every setting below has a command that writes it for you; you never
need to edit it by hand.

### The three layers

Configuration resolves in three layers, and the last one that sets a value wins:

| Layer | Where | What it is for |
|---|---|---|
| shipped | the plugin's own `defaults/config.json` | the defaults; read-only |
| shared | `~/.squirrel/config.json` (or `$SQUIRREL_SHARED_DIR/config.json`) | settings you want in **every** profile |
| this profile | `~/.claude/squirrel/config.json` (or `$CLAUDE_CONFIG_DIR/squirrel/config.json`) | settings for **this** profile only |

If you run more than one Claude profile, the shared layer is the one you want. Without it your
layout, reminders, nicknames and colours live in whichever profile you happened to set them
in, while the *acknowledgements* for those same reminders have always been cross-profile —
so a reminder you dismissed in one profile stayed dismissed everywhere, but the reminder
itself only existed in one.

Map-valued settings merge **per entry** across all three layers, so a nickname in the shared
layer and one in a profile are both present, with the profile winning on a collision. Lists
(`layout`, `idlePhases`, `alarmStates`, `idleBackgroundStates`) are replaced whole by the
nearest layer that sets them — your list *is* the whole list.

### Where an edit goes

Every editing command writes to the **shared** layer, unless the setting is already set in
this profile — then it keeps being edited here, so nothing you have already configured moves
under you. **Every command tells you which layer it wrote to.**

- `--here` on any editing command writes to this profile instead, for a deliberate divergence.
- A `reset` clears the setting from **both** layers, so it genuinely stops being set;
  `--here` on a reset clears only this profile and leaves the shared value standing.
- `/squirrel-config` lists every setting you have made and the layer it came from. A value
  set in the shared layer and overridden here is reported as *shadowing shared* — that is the
  answer to "why is this not what I set?".
- `/squirrel-config share` copies this profile's settings down into the shared layer so your
  other profiles pick them up. It leaves this profile's own config exactly as it was, and it
  will not overwrite a value the shared layer already has, so it is safe to run and safe not
  to run.

`usageFetch` is deliberately never shared: it gates spending *this* profile's credentials on
API calls, and the token it spends belongs to this profile.

| Key | Type | Default | Meaning |
|---|---|---|---|
| `accountNames` | object (email → name) | `{}` | nickname shown instead of the account's email |
| `accountColors` | object (email → colour) | `{}` | status-line colour for one account |
| `repoColors` | object (repo → colour) | `{}` | status-line colour for one repository |
| `defaultAccountColor` | colour | `"white"` | fallback account colour when no override matches |
| `alarmAfterSeconds` | integer, seconds (0–604800) | `120` | waiting time before the loud alarm chip; `0` disables it |
| `alarmStates` | array of strings | `["idle","permission"]` | which activity states can trigger the alarm |
| `idlePhases` | array of `{after, bg, text, bold}` | 30s→`"#c9a227"`/white/bold, 120s→`"#b3261e"`/white/bold | the idle-painting ladder — any number of phases; see [Idle alarm](#idle-alarm) |
| `idleBackgroundAfterSeconds` | integer, seconds (0–604800) | `30` | **legacy**, folds into phase 1's timing; `0` disables all painting |
| `idleBackground` / `idleBackgroundUrgent` | colour or `"off"` | not shipped | **legacy**, fold into phases 1/2's colour when set |
| `idleBackgroundStates` | array of strings | `["idle","permission"]` | which activity states get the tint |
| `usageFetch` | boolean | `true` | fetches per-model weekly usage (reads the Claude OAuth token — see [Security](#security)) |
| `segments` | object (segment → boolean) | `{"share": false, "share-life": false, "sessions": false}` | shows/hides individual segments; `account` and `model` can't be hidden |
| `layout` | array of arrays of segment ids | the two shipped lines | which segments render, on which line, in what order — any number of lines; see [Layout](#layout) |
| `segmentPriority` | object (segment → integer or `"always"`) | `{}` | overrides how hard a segment clings to a narrow pane; higher survives longer, `"always"` is never dropped; see [Layout](#layout) |
| `segmentStyles` | object (segment → `{fg, bg, bold, opacity, sep, force}`) | `{}` | per-segment colours and punctuation; see [Styling](#styling) |
| `barRules` | array of `{when, bg, text, bold, priority}` | `[]` | paints the whole bar when a condition holds; see [Bar rules](#bar-rules) |
| `customSegments` | array of `{id, text\|command, every, at, ack, shared, when, line, link}` | `[]` | your own segments and reminders; see [Custom segments](#custom-segments) |
| `customCommands` | boolean | `false` | whether custom segments may run their `command` |
| `agentsLiveSeconds` | integer, seconds (5–3600) | `60` | how recently a background agent must have written to count as running |
| `clickToAck` | boolean | `false` | whether a reminder can be dismissed by clicking it; see [Clicking a reminder](#clicking-a-reminder) |
| `clickTerminals` | array of strings | `[]` (means `vscode`, `cursor`, `windsurf`) | which `TERM_PROGRAM`s are treated as able to follow a status-line link; `["any"]` skips the check |
| `stdinWaitSeconds` | integer, seconds (0–120) | `10` | how long a render waits for Claude Code's status JSON before giving up; `0` waits for ever. A backstop only — Claude Code always sends it |
| `clickIdleSeconds` | integer, seconds (30–86400) | `1800` | how long the click listener stays up with nothing clicking it |
| `customTtlSeconds` | integer, seconds (1–86400) | `60` | how long a command's output is reused before refreshing |
| `customTimeoutMs` | integer, milliseconds (50–30000) | `2000` | timeout for a custom segment's command |
| `customOutputMaxBytes` | integer, bytes (256–1048576) | `65536` | most a custom segment's command may print before it is cut off and killed |
| `customValueMaxChars` | integer (8–4096) | `512` | most characters of a command's output that are cached and shown |
| `nudgeMaxPerHour` | `5` | 0-200 | machine-wide ceiling on task nudges per hour, across every task and every target — the backstop that bounds a nudge loop nobody anticipated. Each nudge costs a turn on your own account, so this is the setting that caps what a misbehaving watcher can spend. `/squirrel-config` reports how much of it you have used this hour. |
| `usageFetchSeconds` | `900` | 30-86400 | how often the **per-model** weekly buckets are fetched. The `5h` and `week all` numbers need no fetch at all — Claude Code sends them with every render — so this cadence only affects the `Fable`-style per-model line, whose windows are seven days long. Four requests an hour per account at the default; raising it raises exactly that number. If you had previously set `cacheTtlSeconds` to control this, that value still governs until you set `usageFetchSeconds` — an explicit choice is not overridden by a new default. |
| `segmentLabels` | object (segment → text) | `{"share": "mine", "share-life": "life"}` | overrides a segment's display label; a key you don't set keeps its shipped default, so overriding one never erases the other |
| `shareFormat` | `"contribution"` \| `"share"` \| `"both"` | `"contribution"` | how the standalone `share` segment and the `sessions` list render their percentages — see [Multiple sessions](#multiple-sessions) |
| `sessionContribution` | boolean | `true` | whether the `session` (5h) segment folds in this session's contribution as `<contribution>/<total>%` when the data supports it |
| `attributionMetric` | `"cost"` \| `"tokens"` | `"cost"` | which delta drives cross-session attribution (`session`'s composite, `share`, `share-life`, `sessions`, `/squirrel-sessions`) — cost is the default because it's the only figure that includes subagent usage; see [Multiple sessions](#multiple-sessions) |
| `sessionsShowModel` | boolean | `true` | whether `/squirrel-sessions` includes the per-model mix column |
| `branchMaxChars` | integer (0–200) | `20` | truncates the displayed branch name; `0` = no limit |
| `warnPercent` | integer, % (0–100) | `50` | usage percentage where bars turn yellow |
| `dangerPercent` | integer, % (0–100) | `80` | usage percentage where bars turn red |
| `cacheTtlSeconds` | integer, seconds (5–3600) | `60` | how often usage is re-fetched |
| `staleAfterSeconds` | integer, seconds (30–86400) | `600` | age at which a usage value is marked stale (`~`) |
| `chipWidth` | integer (0–40) | `0` | inner width of the activity chip; `0` = natural (one space each side), positive centres to that width |
| `barCells` | integer (3–40) | `10` | number of cells in each usage bar |
| `activeMaxAgeSeconds` | integer, seconds (60–604800) | `3600` | age at which a stale "working" chip disappears |
| `gitTimeoutMs` | integer, milliseconds (50–5000) | `500` | timeout for each `git` call |
| `gitCacheSeconds` | integer, seconds (0–60) | `3` | how long a working directory's git status is reused before re-running `git`; `0` = never cache |
| `resetWarnMinutes` | integer, minutes (0–1440) | `30` | minutes before a session reset when the countdown turns yellow; `0` disables |
| `resetUrgentMinutes` | integer, minutes (0–1440) | `15` | minutes before a session reset when the countdown turns red; capped at `resetWarnMinutes` |
| `sessionSampleSeconds` | integer, seconds (5–3600) | `60` | minimum time between cross-session usage samples (see [Multiple sessions](#multiple-sessions)) |
| `sessionSampleKeepSeconds` | integer, seconds (60–604800) | `21600` | how long a session's usage samples are kept |
| `sessionLiveSeconds` | integer, seconds (30–86400) | `900` | how recently a session must have sampled to still count as live |
| `burnWindowSeconds` | integer, seconds (30–86400) | `600` | span used to compute a session's burn rate (`/squirrel-sessions` shows both $/min and tokens/min) |
| `sessionsMax` | integer (1–20) | `4` | max sessions the `sessions` segment lists before collapsing the rest to `+N` |
| `shareWarnPercent` | integer, % (0–100) | `34` | this session's share of window consumption where the `session` segment's contribution figure turns yellow |
| `shareDangerPercent` | integer, % (0–100) | `67` | this session's share of window consumption where the contribution figure turns red |
| `bannerLines` | `"auto"` \| `"all"` \| list of line numbers | `"auto"` | which layout lines the full-width idle tint covers. `auto` paints every line except one made up entirely of clickable reminders — an alarm ground is a warning about the session, and burying the row of things you are meant to reach for in it defeats both. Set with `/squirrel-bg lines <auto\|all\|1,2>`. |
| `phaseAlerts` | boolean | `true` | master switch for phase actions (the desktop notification and any `run=` command). Off means the ladder only paints. `/squirrel-alerts on\|off` |
| `alertWhenFocused` | boolean | `false` | whether a notification fires while the terminal is the focused window. Off — if you are looking at it, the bar has already told you. Focus is a proxy, not a measurement: when the platform gives no answer, Squirrel notifies. `/squirrel-alerts focused on\|off` |
| `paneReserveColumns` | integer, columns (0–20) | `1` | columns left unused at the right edge of the pane. Claude Code truncates a status line that its own measurement says is too wide, replacing the tail with `…`; one spare column absorbs a glyph mismeasured by one. Raise it if a line still ends in `…` with nothing actually missing from it — that means the pane is narrower than `COLUMNS` claims. |

**Colour values** (`accountColors`, `repoColors`, `defaultAccountColor`, `idlePhases[].bg`,
`idlePhases[].text`, and the legacy `idleBackground`/`idleBackgroundUrgent`) accept a named
colour — `red`, `green`, `yellow`, `blue`, `magenta`, `purple`, `cyan`, `white`, `gray`/`grey`
— optionally prefixed `bright` (e.g. `brightcyan`), or a `#rrggbb` hex triplet. A phase's `bg`
or `text` also accepts `off`, meaning that phase paints nothing for that field (it still fires
— see [Idle alarm](#idle-alarm)); `idleBackground` (legacy) accepts `off` to disable all
painting outright; `repoColors` also accepts `off`/`auto` to clear a repo's override.

## Layout

Nothing about the arrangement is hardcoded. `layout` is a list of lines, each a list of
segment ids, and Squirrel prints exactly what it describes — **one line, two, or four**.

```json
"layout": [
  ["context", "session", "weekly", "share", "share-life", "sessions"],
  ["account", "repo", "git", "model", "effort", "flags",
   "sessionTokens", "cost", "duration", "lines", "pr", "chip"]
]
```

You never need to write that by hand. `/squirrel-layout` edits it in place:

| Argument | Effect |
|---|---|
| *(none)* or `show` | prints the layout line by line, each segment with its drop priority, plus any registered segment not currently placed |
| `move <segment> <line> [position]` | moves one segment; a line number one past the end creates a new line, an omitted position appends |
| `line <n> <segment> <segment> …` | sets one whole line; the named segments are removed from wherever else they were |
| `swap <a> <b>` | exchanges two whole lines |
| `priority <segment> <n\|always>` | how hard that segment clings to a narrow pane |
| `reset` | restores the shipped layout and discards every priority override |

Lines and positions are numbered from 1. Every edit rewrites only the layout, never the rest
of your config, and is validated first: an unknown segment id, a line number out of range, or
an edit that would leave `account` or `model` unplaced is **refused and nothing is written**.
An unknown id already sitting in your config is skipped silently, so downgrading the plugin
can never break your bar.

A segment that is not in the layout does not render — that is all "off" means. `/squirrel-show
<segment> off` removes it and `on` puts it back where it shipped, so the two commands are two
views of the same thing.

### What gets dropped on a narrow pane

Every segment carries a **priority**. When a line does not fit, Squirrel first narrows the
usage bars (10 → 6 → 4 → 3 cells — bars are decoration, so trading cells for columns keeps
every number and label), then drops segments one at a time, lowest priority first, until the
line fits. Below that it sheds detail *within* segments: the `(84k/200k)` token counts, then
the `↻` reset times, then the bars entirely, then long labels (`week all` → `wk`).

| Priority | Segments |
|---|---|
| `always` | `account`, `model` (protected — cannot be dropped or switched off at all), `context`, `session`, `weekly`, `chip` |
| 80 | `repo`, `git` |
| 70 | `sessionTokens` |
| 60 | `effort` |
| 50 | `flags` |
| 40 | `cost`, `share`, `share-life`, `sessions` |
| 30 | `duration` |
| 20 | `lines` |
| 10 | `pr` |

The four **details** shed on the same scale, and are set the same way:

| Priority | Detail |
|---|---|
| 60 | `abbrev` — the long labels (`week all` → `wk`) |
| 50 | `bars` — the usage bars themselves |
| 40 | `resets` — the `↻` reset times |
| 30 | `tokens` — the `(84k/200k)` counts |

Change any of them with `/squirrel-layout priority <segment\|detail> <n\|always>`:

- keep the reset times even on a narrow terminal — `/squirrel-layout priority resets 95`
- never lose the branch — `/squirrel-layout priority git always`
- drop the git info first when it is tight — `/squirrel-layout priority git 5`

## Clicking a reminder

A reminder can be dismissed by clicking it instead of typing `/squirrel-done`. This is **off
by default and starts nothing until you ask for it**:

    /squirrel-click on
    /squirrel-custom set water click=ack        # or click=snooze:15

### Read this before enabling it

**It runs a small local web server.** That is the only way a click can do anything: Claude
Code exposes no click event, and the one piece of interactivity a terminal offers is a
hyperlink, which can only open a URL. Something local has to be listening for that URL.

**Nothing binds until all three of these are true** — the feature is on, a segment actually
declares a click action, and your terminal can follow the link. Miss any one and no socket
exists, no port is held, and no process runs. If you never enable it, this code never runs at
all.

**A fourth condition decides whether the link appears on a given render: room.** Claude Code
measures a status line's width with the URL of a hyperlink counted as visible text (see
`host_len()`), so one link costs about 64 columns of the line's budget. When the line does not
fit with it, the link is shed rather than your text — an action link last, after the spend
readouts, but shed all the same. On a narrow pane, or a busy second line, a reminder can
therefore be there and not be clickable. Widening the terminal, or moving reminders to a line
of their own (`/squirrel-layout move water 3`), is what buys the room back.

**Clicking opens a browser tab.** Following a hyperlink means the browser; there is no way
around it. The tab shows a small page saying what happened and closes itself where the
browser allows. If that would annoy you, don't enable this — `/squirrel-done` needs no tab.

**It works in VS Code and Cursor, and currently not in standalone terminals.** Claude Code
passes OSC-8 hyperlinks through to IDE terminals but not to emulators like iTerm2 or Konsole
([anthropics/claude-code#26356](https://github.com/anthropics/claude-code/issues/26356),
closed as not planned). Squirrel checks `TERM_PROGRAM` and, where a link cannot be followed,
emits none and starts no listener. `/squirrel-click-terminals` overrides that if the check is
wrong for you. This may change in either direction with a Claude Code release.

### What it can and cannot do

The listener understands exactly two verbs — acknowledge and snooze — against segment ids
that are already in your config. **It cannot run commands, read files, or take a path from a
request.** That fixed verb set is what keeps a local HTTP server from being a way into your
machine.

| | |
|---|---|
| Binds | `127.0.0.1` on a random port, never `0.0.0.0` — unreachable from the network |
| Authorises | a 256-bit token in the URL, new every start, stored only in a `0600` file |
| Refuses | any request without the exact token, any `Host` that is not loopback (defeats DNS rebinding), anything that is not a `GET` — all with a bare `404` and no hint |
| Sends | no CORS headers at all, so a web page cannot read a response even if it guessed |
| Exits | by itself after `clickIdleSeconds` (default 30 minutes) with nothing clicking |
| Runs | **one per machine**, not one per profile — its record and lock live in the [shared store](#multiple-sessions) alongside the acknowledgement state it writes, so your aliases share a single socket |

A stale listener record — its process gone, or older than the idle timeout — is replaced
rather than trusted, since a port it once held could since belong to something else. **On
Linux** two further checks apply: the recorded process must still be the one holding that
port, and its start time must match, so a recycled pid cannot inherit a dead listener's
record. Both read `/proc`, so **on macOS and Windows neither check runs** and the record is
trusted on process liveness alone — which is what shipped before those checks existed. The
window this leaves is narrow and accidental rather than adversarial: your listener would
have to die, something unrelated would have to take its exact port, and the pid would have
to still be live. It is stated here rather than buried because macOS is a first-class Claude
Code platform, and `/squirrel-click off` closes it completely on any platform.

Even on Linux, the check proves the recorded process holds the port — not that it is our
listener. Someone who already owns the port *and* can write your `0600` shared-store file
could still have themselves named; that is a different attacker with capabilities this guard
never claimed to take away.

**If clicking does nothing, run `/squirrel-config`.** The `click-to-ack` line names which of
the three conditions is unmet — feature off, terminal cannot follow links, or no segment has
`click=` yet — and, when a listener is up, the port it is on. If it reports the feature as ON
and the words still are not links, it is the fourth condition: the line had no room for the
URL that render. Widen the pane and look again.

One thing that is NOT Squirrel: in the VS Code terminal, an unlinked word offers to "open
file" when you hover it. That is VS Code's own fallback link provider, it applies to every
word on screen — `working`, a branch name, a repo — and it shows up exactly where a real link
is absent, which is why it is most noticeable on a reminder you expected to click.

Squirrel cannot suppress it: the offer is made by the editor about ordinary text, and the only
text we could change is the reminder's own words. Two things do work — enable click-to-ack so
the words carry a real link, or turn the fallback off in VS Code:

```jsonc
// settings.json
"terminal.integrated.enableFileLinks": "off"
```

`/squirrel-click off` stops new links; a running listener exits when it next goes idle.

## Background agents

Claude Code's hooks cannot see a **background** agent while it works — `PostToolUse` fires
once when it spawns and then the session goes quiet, so the activity chip reads `idle` while
three agents are busy. The `agents` segment fills that gap:

    … · $1.23 · 2h13m · ⧗ 2 agents · [ ⚙ working ]

It renders nothing at all when no agents are running, so it costs you no space in the normal
case.

**It reads a path nobody documents** — `<tmpdir>/claude-<uid>/<project>/<session>/tasks/` —
and every assumption in that path could change without notice. So it degrades to showing
nothing rather than guessing: unknown session, missing directory, unreadable directory,
unexpected shape all render nothing, and it never errors.

Two things live in that directory: symlinks that point at a real agent's transcript, and
ordinary files holding tool results. Only the symlinks are agents, and how recently an agent
*wrote* — not when it was spawned — decides whether it is still running. `agentsLiveSeconds`
(default 60) sets that window.

**Cost when nothing is running**, which is nearly always: one directory check and one scan. A
directory full of tool results costs no extra system calls at all, because only symlinks are
followed.

## Custom segments

Add your own segment with `/squirrel-custom`. It can be static text, the output of a command,
or something that comes back on a schedule:

| Field | Values | Effect |
|---|---|---|
| `text` | any text (**rest of the line**) | static text |
| `command` | a shell command (**rest of the line**) | its first line of output becomes the text |
| `every` | `30m` `2h` `90s`, or seconds | with `ack`: how long after dismissing before it returns. Without `ack`: how often a `command` is re-run |
| `at` | `HH:MM` | show from that time of day |
| `ack` | `on` / `off` | can be dismissed with `/squirrel-done` |
| `shared` | `on` / `off` | dismissal clears in **every session and every account alias** at once |
| `when` | a condition (**rest of the line**) | only show while it holds — the [bar rule](#bar-rules) grammar |
| `line` | `1`, `2`, … | which line it joins |
| `link` | `https://…` | clickable where the terminal supports OSC-8 hyperlinks |

`text=`, `command=` and `when=` take the rest of the line, so put one of them last.

A custom segment is a segment like any other: it works with `/squirrel-layout`,
`/squirrel-style`, `/squirrel-layout priority` and `/squirrel-show` exactly as the built-in
ones do, and its text goes through the same [sanitiser](#security).

### Reminders

There is no separate reminder feature — a reminder is a custom segment with `every` and `ack`:

    /squirrel-custom add water every=30m ack=on shared=on text=💧 drink water
    /squirrel-custom add break every=2h ack=on shared=on text=🧘 take a break
    /squirrel-done water        # or: /squirrel-done all
    /squirrel-snooze water 5

`shared=on` is what makes this work across the aliases: acknowledging once clears it in every
session and every account within a few seconds, because each bar re-reads the shared store on
its next refresh. Leave it off for something that really is per-project.

A reminder you have never acknowledged shows immediately — so you can see straight away that
it worked — and returns `every` after each acknowledgement.

### Commands

`command=` segments do nothing until you switch them on:

    /squirrel-custom-commands on

They are off by default on purpose. Squirrel's own slash commands write your config, and
Claude runs those while reading things it did not write, so *anything able to edit your config
could otherwise cause a command to run*. The switch is a deliberate brake, not a guarantee.

A command **never runs while the bar is drawing**. Its output is cached and refreshed by a
detached background process, exactly like the usage fetch, so a slow or hanging command costs
the status line nothing — the first render after you add one shows nothing, and the next shows
the value. A command that fails keeps its last good output and records the error, which
`/squirrel-custom` shows. Refresh interval is `every` (or `customTtlSeconds`); the command is
killed, with its whole process group, after `customTimeoutMs`.

## Bar rules

The full-width tint is not only about idle time. A **bar rule** paints the whole status line
whenever a condition holds:

    /squirrel-rules add weekly.pct >= 80 bg=#7c3aed text=white bold=on
    /squirrel-rules add git.dirty text=yellow
    /squirrel-rules add cost.usd > 5 bg=#b3261e text=white

A condition is exactly `<metric> <op> <value>`, or a bare `<metric>` for true/false.
Operators: `>=` `<=` `==` `!=` `>` `<`. Run `/squirrel-rules` with no arguments for the live
list of metrics — they cover context and usage percentages, cost, duration, lines, tokens, git
(`git.dirty`, `git.ahead`, `git.branch`), repo, model, effort, `idle.secs`, `state`, and the
two transcript-derived counts `pending.tools` / `pending.agents` (see
[What the chip knows](#what-the-chip-knows)).

| Argument | Effect |
|---|---|
| *(none)* or `show` | every rule in evaluation order, **which one is winning right now**, the metrics available, and any rule being ignored |
| `add <condition> bg=.. text=.. [bold=on\|off] [priority=<n>]` | adds a rule; it must paint something, so `bg`, `text`, or both are required |
| `set <condition> <field>=<value> …` | overlays only the fields given onto an existing rule |
| `remove <condition>` | drops one of your rules |
| `reset` | clears your rules; the idle ladder remains |

**The idle ladder ships as the default rules**, so there is one mechanism rather than two.
`/squirrel-rules` shows it as `[ladder]` entries, and `/squirrel-phases`, `/squirrel-bg` and
`/squirrel-bg-after` keep editing it exactly as before.

**Which rule wins:** the last matching rule, unless one carries a higher `priority`. Because
the ladder comes first, a rule you add later beats the idle tint by default.

**Nothing is ever evaluated as code.** There is no `eval` anywhere in the condition path — a
condition is parsed into `(metric, operator, value)` and compared, nothing more. A condition
that does not parse is reported and ignored, and the bar keeps rendering; `/squirrel-rules`
and `/squirrel-config` both tell you how many rules are being ignored and why. Rules come from
your own config file and are never fetched from anywhere.

**"Why is my bar purple?"** — run `/squirrel-rules`. The rule currently painting is marked
`<== WINNING NOW`, other matching rules are marked `(matches)`, and a rule whose metric only
exists while the bar is rendering (context percentage, cost, duration, lines, tokens, model,
effort) is marked as needing the live status line rather than guessed at — if any of those
are present, the listing says the real winner may differ instead of naming one confidently.

**A metric with no value never matches**, not even with `!=`. "Unknown" is not "different
from" — a rule must not paint your bar because data happens to be missing.

## Styling

Every segment can carry its own colours. Nothing is written by hand — `/squirrel-style` edits
one segment at a time:

    /squirrel-style set git fg=#ff8800 bold=on
    /squirrel-style set duration opacity=0.5
    /squirrel-style set cost sep=dot
    /squirrel-style clear git

| Field | Values | Effect |
|---|---|---|
| `fg` | colour name, `#rrggbb`, `auto` | foreground colour |
| `bg` | colour name, `#rrggbb`, `auto` | background behind the segment |
| `bold` | `on` / `off` | bold text |
| `opacity` | `0.0`–`1.0`, `auto` | fades the text toward the background |
| `sep` | `dot` `bar` `space` `none`, `auto` | the punctuation printed *before* this segment |
| `force` | `on` / `off` | let `fg`/`opacity` override meaning-carrying colours too |

`set` overlays only the fields you give it, so styling one field never resets the others, and
styling one segment never touches another. `auto` clears a single field. An unknown segment,
an invalid colour, an out-of-range opacity, or an unknown separator is refused and nothing is
written.

**A warning always breaks through.** `fg` and `opacity` recolour anything decorative — the
all-clear green, a clean branch's grey, the dimmed token counts, an account's own colour — but
never a yellow or red one. A usage bar still turns yellow at `warnPercent` and red at
`dangerPercent`, the session-reset countdown still turns yellow and red, and a dirty branch
still shows yellow, whatever you set. So `/squirrel-style set git fg=#ff8800` gives you an
orange branch that still goes yellow the moment it is dirty. `force=on` opts out if you really
want total control — but a bar that stops warning you is a bar that lies.

**Opacity** is a real alpha blend toward the idle phase banner's colour while one is painted
(see [Idle alarm](#idle-alarm)). With no banner painted there is no knowable background — a
terminal does not tell a program its background colour — so it falls back to the terminal's
own dim attribute. It does the same for named ANSI colours, whose actual RGB is the
terminal's palette choice rather than something that can be blended against honestly.

**A phase banner covers a segment background.** While the idle ladder (or any
[bar rule](#bar-rules)) is painting the whole line, its background is applied last and wins
over a segment's own `bg` — your style has not broken, it is underneath. Segment foreground
colours still show through.

**`sep`** is the fix for a reordered layout that punctuates oddly. Segments are grouped, and a
group is joined by `·` while a group boundary is joined by `│`; a segment moved next to a
group it does not belong to keeps its own group's separator until you override it:

    you@example.com │ $1.23 │ Fable 5      # cost moved next to the account
    /squirrel-style set cost sep=dot
    you@example.com · $1.23 │ Fable 5

## Multiple accounts

Config, cache, and activity state all live under the *active* `CLAUDE_CONFIG_DIR`. If you run
separate Claude Code profiles with different config directories, each one keeps its own
nicknames, colours, and usage data automatically — nothing to switch manually.

## Multiple sessions

Running several Claude Code sessions on one account? They all draw on the same shared
5-hour and weekly limits, but each session's own status JSON only knows its own totals.
Squirrel pools that knowledge in a small **shared store** — deliberately outside
`CLAUDE_CONFIG_DIR` and unaffected by `HOME` overrides, so every session on the account finds
the same store regardless of which config profile or shell alias launched it — at
`~/.squirrel/sessions/<session>.json` (one flat file per session, account-independent: a
session that `/login`s into a different account mid-conversation keeps writing to the same
file, since which account owns which slice of its history is recorded per-sample, not by
where the file lives; override the location with `SQUIRREL_SHARED_DIR`, mainly used by the
test suite). Older installs may still have files under a now-legacy
`sessions/<account-hash>/<session>.json` layout — Squirrel keeps reading those in place,
never migrating or deleting them.

**What's stored, and what isn't:** each file holds a session id, an optional repo name, and a
small ring of `[timestamp, cumulative_tokens, cumulative_cost, model, account,
account_utilisation_pct, account_resets_at]` samples (one at most every `sessionSampleSeconds`, pruned after
`sessionSampleKeepSeconds`) — enough to compute each session's ground-truth *attributed slice
of the current 5-hour window* (see "How the numbers are derived" below), not just its
lifetime total, and its usage split by model (a session can switch models mid-flight — see
"per-model mix" below). `account` is a short hash of the account email, never the email
itself. Samples written before one of these fields existed simply have that field as `null` —
nothing is migrated or backfilled, and nothing breaks. **Nothing else is ever written** — no
prompts, no file contents, no paths beyond a repo name and a model's display name. Writing a
sample is best-effort and cannot slow down or break the status line.

**Why cost, not tokens, for the lifetime figures:** `share-life` and the `life` columns are
computed from `cumulative_cost`, via `attributionMetric` (default `"cost"`, switch to
`"tokens"` with `/squirrel-attribution-metric`) — there's no ground-truth equivalent for a
*lifetime* percentage, since the window resets every 5 hours, so this stays a ratio. This
matters because Claude Code's status JSON does **not** fold subagent usage into
`context_window.total_input_tokens`/`total_output_tokens`, but it **does** fold it into
`cost.total_cost_usd`. A session that runs a lot of subagent work can therefore show a small
token count while still being the account's biggest lifetime consumer — ranking by tokens
alone would hide it. `/squirrel-sessions` always shows both a cost and a token column so the
token figures stay visible; only the `life %` ranking follows the configured metric. The
*window* (5h) figures have no such metric to configure — see below.

**The main view — folded into the `session` segment itself:** once a second session on the
account has been live for a moment, the `session` (5h) segment stops showing just the plain
account total and starts showing a composite:

```
5h ▰▰▰▰▰▰▰▰▰▱ 19pp/90%
```

Read as: the account has used 90% of the window, and *this session* is responsible for 19
of those points. That's the actionable figure — the *share of consumption* (what fraction of
this window's usage came from this session, 21% here) isn't, on its own, what you'd act on;
21% of a 90%-full window is roughly **19 percentage points of that 90%**, which is what
you'd actually stop to bring the account's usage down.

The two numbers are coloured on **independent scales**, because they answer different
questions. `<total>` (90 here) keeps the bar's own usage-threshold colour — `warnPercent`/
`dangerPercent`, same as always: "how full is the window?" `<contribution>` (19 here) is
coloured by this session's *share* of the consumption — `shareWarnPercent`/
`shareDangerPercent` (default 34/67) — because it answers "am I the problem?": a session
responsible for most of a window's burn reads red even while the window's total usage is
still comfortably green.

Only shown when the data genuinely supports it — turn it off entirely with
`/squirrel-session-contribution off`, or it falls back to the plain `<total>%` on its own
whenever any input isn't truly known: this session isn't (yet) live, it's the *only* live
session (trivially 100% of consumption — a composite would just repeat the total), or the
account's 5-hour utilisation itself is unknown. Never a guessed or partially-computed figure.

**The standalone view — three more segments** (all **off by default** — turn on with
`/squirrel-show <segment> on`), for anyone who wants the same numbers on their own line
segment instead of (or as well as) folded into `session`:

| Segment | Shows |
|---|---|
| `share` | this session's contribution again, on its own, labelled `mine` (not `5h`, so it can never be confused with the account-wide `session` segment on the same line) |
| `share-life` | this session's share of all live sessions' lifetime usage, labelled `life` |
| `sessions` | every live session ranked by its attributed contribution, e.g. `alpha 24pp · beta 8pp · ▸gamma 2pp · +2` |

`shareFormat` controls what the standalone `share` segment **and** the `sessions` list render.
They share the setting deliberately: one `%` sign on a line should mean one thing, and with
different denominators a bar could show the same session as both `mine 43pp/77%` and
`▸beacon 76%`.

| `shareFormat` | Renders | Meaning |
|---|---|---|
| `contribution` *(default)* | `mine 19pp/89%` · `alpha 24pp` | percentage points of the account's 5h utilisation, then (for `share`) the account total |
| `share` | `mine 21%` · `alpha 67%` | the raw share of this window's cross-session consumption |
| `both` | `mine 21% → 19pp/89%` · `alpha 24pp (67%)` | share and contribution together |

Same fallback rule as the composite: if the account's current 5h utilisation isn't known (no
usage data yet, `/squirrel-usage off` with no fallback data, or a cache with no value),
`contribution`/`both` fall back to the plain share (`mine 21%`) rather than showing a wrong
or blank number.

For the full picture — repo, window and lifetime **cost and tokens** and shares, burn rate
(`$/min`, with tokens/min alongside), a per-model mix, and age for every live session — run
**`/squirrel-sessions`**. That's the one to reach for when you genuinely can't tell which of
several open sessions to stop; it ranks by cost by default and prints a note explaining the
tokens-vs-cost discrepancy directly under the table. A session becomes "live" once it's
rendered its status line at least once and drops out `sessionLiveSeconds` after it goes quiet.

**Per-model mix:** a session can switch models mid-conversation, and different models cost
different amounts, so `/squirrel-sessions` also shows a `model mix` column, e.g.
`Opus 5 82% · Sonnet 5 18%` — that session's own cost (or tokens, matching `attributionMetric`)
split by the model that was active for each chunk of usage. Turn it off with
`/squirrel-sessions-show-model off` (`sessionsShowModel`). Two things worth being precise
about: it's **each session's own ratio**, not weighted against anything account-wide, and
a model switch that happens *between* two samples is attributed entirely to the newer model
(Squirrel only samples at most once every `sessionSampleSeconds`, so it can't see finer than
that) — an approximation, not an exact reconstruction of when the switch happened. A sample
with no recorded model (written before this feature existed) is grouped under `unknown` rather
than guessed at or dropped. This is a different, complementary number from the exact per-model
**weekly** buckets the `permodel` segment shows (§ [How the numbers are
derived](#how-the-numbers-are-derived)) — the mix column is about one session's own spend, the
`permodel` segment is the account-wide, API-reported truth for the week.

### Reading it on a narrow terminal

The table has ten columns and needs about 130 to show them all. Rather than wrapping into
unreadable ribbons it **sheds columns to fit**, worst-first: `life tok`, `life cost`,
`5h tok`, `model mix`, `life %`, `5h cost`, `coverage`, `age`, `burn`. `session` and
`5h pts` are never dropped — they are the question the table answers.

When anything is hidden it says so and names what is missing, so you are never reading a
table that is quietly incomplete. `/squirrel-sessions wide` prints every column regardless
and lets the terminal wrap, if that is what you want.

The `~` and `?` marks live on the `5h pts` figure itself, not in the `coverage` column, so
narrowing can cost you precision but can never make a number look more certain than it is.

## What the chip knows

The `[⚙ working]` / `[✓ idle]` chip is driven by Claude Code's hook events, and those events
cannot see everything. A background agent fires its spawn event and then the session fires
`Stop`, so the bar would read `‼ IDLE 1h42m ‼` while agents were still working — a real
complaint, and the numbers in that example are the ones that were actually on screen.

So Squirrel also reads the tail of your session transcript, which answers the question
deterministically rather than by inference — no model involved:

| What the transcript shows | State |
|---|---|
| a `tool_use` with no matching `tool_result` | something is still running (`⧗ N subagents` when those calls are agents, otherwise `⚙ working`) |
| the assistant spoke last, nothing outstanding | waiting for you |
| anything else | mid-turn |

Two counts are published as rule metrics: **`pending.tools`** and **`pending.agents`**. So
`when: "pending.agents > 0"` is a condition you can paint on.

This only ever contradicts the hooks in one direction: an outstanding call can prove a session
is *not* idle, but nothing here invents idleness the hooks did not report. If the transcript is
missing, unreadable, or in a shape Squirrel does not recognise, the chip is exactly what it was
before — that file is Claude Code's internal format, not a documented interface, so it is
treated as a bonus rather than a dependency.

**Cost**, since this runs on every render: only the last 256 KB is read, never the whole file,
and the result is remembered per `(path, mtime, size)`. Measured against the five largest
transcripts on the author's machine — the biggest 83 MB — the scan costs **0.8–2.5 ms** and
0.005 ms once memoised, against a render budget of about 50 ms. A tool call that is still
outstanding is by definition the last thing written, because nothing else is appended while
Squirrel waits for it, so the tail is all that is ever needed.

## Watcher tasks, and what they cost

A task is a condition plus an action. Squirrel evaluates the condition and writes a **job** to
a queue in the shared store; a reader picks the job up and acts on it. Squirrel itself never
acts — that split is deliberate, and it is why what a job can do is not limited by what
Squirrel knows how to do.

**Every nudge costs you a turn on your own account.** Delivering one runs a one-shot
`claude -p` on the cheapest model, so a watcher that fires ten times a day is ten small
requests billed to you. That is the whole reason for the ceilings below, and it is why
`notify` is off until you switch it on.

| Setting | Default | What it bounds |
|---|---|---|
| `taskActions` | `{}` — everything off | No action class runs until you opt into it by name. A task whose action is not enabled is refused at creation, not created and quietly ignored. |
| `cooldown` (per task) | 1800s, never less than 60 | The minimum gap between one task's firings. A cooldown of `0` is silently raised — a watcher with no brake is the mistake that costs money. |
| `nudgeMaxPerHour` | `5` | A machine-wide ceiling across **every** task and target. When it trips, no nudge job is created at all, and `/squirrel-config` says so. |

Worst case, with the defaults: five nudges an hour, each one small request on the cheapest
model. Raise `nudgeMaxPerHour` and you are raising exactly that number — nothing else changes.

### Why a nudge can't start a loop

A nudge wakes a session, and a woken session's state is precisely what watcher conditions
look at, so two carelessly written tasks could in principle nudge each other for ever. Three
things stop that, and they are deliberately different in kind:

1. **Cooldowns** stop a task repeating faster than you asked it to.
2. **The marker.** Every nudge arrives with a short `[squirrel]` suffix, which lands in the
   receiving session's transcript. A task refuses to send a nudge when *this* session's own
   most recent turn was itself a nudge — so a ping cannot be answered by a pong. Only the
   most recent turn counts: once you have replied, your watchers are live again.
3. **The ceiling**, which bounds the damage whatever the cause.

Be clear about what this is not: Squirrel **cannot observe causality**. Nothing records why a
session woke up. The marker makes one specific, common case visible; the ceiling covers
everything else. This is bounded, not prevented — which is why the ceiling is not optional.

## Reading the bar when it is painted

When the idle ladder paints the whole bar, the text on it follows one rule: **it stays
readable and it keeps meaning something.**

- **`white` means bright white** on a banner. Standard white renders as a mid-grey on many
  terminal themes, which on the amber phase reads as almost black.
- **Dimmed text is not dimmed on a banner** — it is dimming against a background that is no
  longer there, and bold grey-white plus dim on amber is unreadable.

Threshold colours are currently **flattened** on a painted banner along with everything else.
Keeping them would be better, and a version that did so shipped briefly and was withdrawn: it
worked out what counted as a threshold by looking at the colour, and a dirty git branch, a
custom segment's own amber and a crossed usage limit are all the same yellow. The result
highlighted unrelated parts of the bar at random. Restoring it needs each value to carry
whether its colour *means* something, rather than a reader guessing from the hue.

`force` on a phase (`/squirrel-phases set 120 force=on`) additionally strips per-segment
colours before painting, for a completely uniform bar.

## What a refused or disabled usage fetch costs you

**The `5h` and `week all` percentages do not depend on the network at all.** Claude Code sends
them with every render, for the account you are logged into right now, and Squirrel shows those.

The usage API is fetched for one thing: the **per-model weekly buckets** — the `Fable`-style
line. So if that fetch is rate-limited, fails, or you switch it off with `/squirrel-usage off`,
you lose the per-model line and nothing else. The headline numbers keep working.

That is a deliberate change. Previously a cached API figure won over the fresher one in the
same payload, and a user comparing their bar with Claude Code's own `/usage` panel found it
reading 51% against the panel's 69% — eighteen points low, in the safe-looking direction, on
the number they use to decide when to compact.

**Request arithmetic**, at the shipped `usageFetchSeconds: 900`: four requests an hour per
account, times however many accounts you use. Raising the setting raises exactly that number.
Weekly per-model windows move slowly, so a quarter-hour lag on that line is imperceptible.

**If the endpoint refuses (HTTP 429)** Squirrel backs off — honouring `Retry-After` when the
server sends one, otherwise doubling from two minutes up to an hour — and records it, so every
session in that profile backs off together rather than each discovering the refusal for itself.

**`/squirrel-usage off` is also a security simplification**: with the fetch disabled Squirrel
never reads your credentials file, never opens a socket, and never has a token in the process.

## How the numbers are derived

Squirrel deliberately never hardcodes a price, a per-token rate, or a model's "weight" against
any limit — anywhere, for any reason. Two reasons: Anthropic can change pricing or run
promotions at any time, and Squirrel has no way to know the exact formula the API itself uses
to weigh one model's usage against a shared limit. Baking in a guess would make the numbers
wrong the next time pricing changes, silently.

**The account-wide `5h`/`week all` percentages, and every per-model weekly bucket (`permodel`)
are ground truth** — they come directly from the usage API's own `used_percentage`, exactly as
reported. Squirrel never derives these from cost or tokens.

**Those figures are stamped with the account they were fetched for**, and a cache belonging to
a *different* account is discarded rather than shown. This matters if you `/login` mid-session,
which is a supported flow: before, the bar kept displaying the previous account's limits — as
fact, with no staleness mark — until the cache expired, which reads as lag and is worse than
lag. A mismatch also triggers an immediate refetch instead of waiting out `cacheTtlSeconds`.

The distinction is deliberate. A number that is old but **yours** is stale, and the bar marks
it `~`. A number belonging to **someone else** is not stale, it is wrong, so it is dropped and
the bar falls back to the figures Claude Code reports for the session you are in now. A cache
written before this existed carries no stamp, is treated as unknown provenance, and is
replaced by the next fetch. Only the account hash is stored, never the address.

**Cross-session attribution — the `session` segment's composite, `share`, `sessions`, and
`/squirrel-sessions` — is ground truth too, not a reconstruction.** Earlier versions estimated
one session's slice of the shared 5-hour window from its own cost/token delta, as a ratio
against every other live session's delta. That was a proxy, and it broke in ways users actually
hit: subagent work is invisible to a session's own token counters, and logging into a different
account mid-session instantly moves the *existing* conversation's cost onto the new account's
window — a jump no session-local delta could ever explain. Since Claude Code already computes
the account's real utilisation percentage, Squirrel now samples that percentage directly
(alongside its existing per-session sampling) and attributes its *delta* to whichever
session(s) were active during it:

| Situation over one interval between two utilisation readings | Attribution | Marked |
|---|---|---|
| exactly one session was active | the whole delta belongs to it | **exact** |
| several sessions were active | split by their cost deltas in that interval (cost is used only as a splitter here, never as the measurement) | **estimated** |
| several active, no cost signal to split by | split evenly | **estimated** |
| no observation covers the interval (before Squirrel's first sample of this account, or a gap) | not attributed at all | **unknown**, shown as `?` — never a confident `0` |

"Active" means a session wrote a sample in the interval *and* its own cumulative cost or tokens
actually grew — an open-but-idle session gets nothing. A window reset (the 5-hour limit rolling,
or an account switch) shows up as the utilisation reading dropping; a negative delta is always
treated as a reset, never as negative consumption. This design has three deliberate

**Two things keep the attributed figure honest, and both exist because it once was not.** A
user's bar read `5h ▰▰▱▱▱ 183/3%` — 183 attributed points against a 3% account total, which
is impossible: the part cannot exceed the whole.

- **A reading is only trusted while its own window is open.** Every sample records the
  `resets_at` of the window its percentage describes. Five sessions were writing readings for
  one account and one of them had frozen — its Claude Code process was still serving the
  pre-reset window's numbers, reporting 45% every minute for three hours while the others
  reported the true 3%. Merged into one timeline, a flat 3% became a 3→45→3→45 sawtooth, and
  every up-leg read as 42 points of fresh consumption. A reading written after its own
  `resets_at` has passed is now discarded. For samples written before that field existed,
  observers that contradict each other at the same moment are treated as an observation gap —
  `?`, never a guess at which one is right.
- **Utilisation cannot fall inside a window**, so the baseline is the highest reading since
  the last reset, not merely the previous one. Only consumption above that high-water mark
  counts as new. This is what makes "the attributed part can never exceed the account total"
  true by construction rather than by hoping every reading is sane.

Attribution is also scoped to the window the account is actually in — `resets_at` minus five
hours — rather than a rolling five hours, which reached back across the last reset and banked
the previous window's consumption into this one's total. Where no sample can date the
boundary, it is inferred from the last time utilisation fell, which is by definition the last
time the window rolled.

consequences, all improvements over the proxy it replaced: subagent work is included
automatically (it's inside the account's own utilisation, not reconstructed from any one
session's counters); a re-login jump lands on the account that actually absorbed it, because
Squirrel is reading that account's own utilisation; and nothing before Squirrel started
watching an account is ever invented — it reads as unknown coverage, not a misleadingly
confident zero.

Concretely, in the segments:
- The `session` (`5h`) segment's composite (`19/94%`) shows `19` only when this session has
  attributed data; a `~` marks it whenever any part of that figure is estimated rather than
  exact, and the composite disappears entirely (falling back to the plain `94%`) when this
  session's own attribution is entirely unknown.
- `share`/`mine`, `sessions`, and `/squirrel-sessions` show `?` for a session with no
  attribution at all, rather than a confident `0%`.
- `/squirrel-sessions` also shows a **coverage** column (e.g. `93m/5h`) — how much of the
  window Squirrel can actually vouch for, per session — so a `?` or `~` is never a mystery.
- **`share-life` and the `life` column are unchanged**, and stay cost/token-ratio based: there
  is no ground-truth equivalent for a *lifetime* percentage, since utilisation resets every 5
  hours. `attributionMetric` (default `"cost"`, switchable to `"tokens"`) still controls only
  these lifetime figures — it no longer has any effect on the window-based numbers above, which
  have no metric to switch since they read the account's own percentages directly.
- **The per-model mix is a different, complementary ratio**: one session's own cost (or tokens)
  split across the models it used, never a claim about how the account-wide limit weighs one
  model against another.

Because the window-based numbers are read directly from Claude's own percentages, and the
lifetime ratios cancel out any pricing change the same way they always did, none of this needs
updating when pricing changes, a promotion runs, or a new model ships.

## Idle alarm

The full-width background is a **phase ladder** (`idlePhases`) of any length you define — not
a fixed pair of colours baked into the code. Each phase is `{"after": <seconds>, "bg":
<colour|"off">, "text": <colour|"off">, "bold": <true|false>}`; the last phase whose `after`
has elapsed wins, and it paints only while the session is in one of `idleBackgroundStates`
(idle or needs-you, by default — never while working). `text` genuinely replaces every
segment's own colour on that line (not merely interleaved with it), and `bold` (default
`false`) makes it bold. Shipped with two steps, both bold:

1. **30 seconds** — the whole bar turns a clearly visible yellow (`#c9a227`) with every
   segment's own colour replaced by bold white text, so it reads at a glance rather than
   blending in.
2. **120 seconds** (the same threshold that triggers the loud chip) — the activity chip
   becomes a loud bold white-on-red alarm showing how long it's been waiting, e.g.
   `[ ‼ IDLE 2m ‼ ]`, and the bar switches to strong red (`#b3261e`), still with bold white
   text — unmissable across a room. The loud chip keeps its own inverse styling regardless of
   the ladder's `text`/`bold` values.

Manage the ladder with `/squirrel-phases` (or `--show-phases`/`--add-phase <seconds>
bg=<colour> [text=<colour>] [bold=on|off]`/`--set-phase <seconds> [bg=..] [text=..] [bold=..]`/
`--remove-phase <seconds>`/`--reset-phases`) — every edit is surgical: it touches exactly the
phase (and field) named, never rewriting the ladder wholesale, so an earlier customization is
never lost to a later, unrelated one. `bg`/`text: "off"` means that phase paints nothing for that field — it
still fires, it just doesn't override that field — which is different from omitting the field
(inherits whatever's already there) or `--remove-phase` (the step no longer exists at all).

The original two-command interface still works: `/squirrel-bg <colour>` and `/squirrel-bg-after
<seconds>` fold into the ladder's first phase, `/squirrel-bg urgent <colour>` into the second —
exactly as before the ladder existed. Turn the chip alarm off entirely with `/squirrel-alarm
off` (this also removes the second phase's painting, leaving just the first); `/squirrel-bg
off` disables all painting outright.

Waiting states keep counting for up to 7 days, so a session left open over a long weekend
still alarms correctly. A stale `[⚙ working]` chip — one left behind after a crash, with no
matching completion — disappears after an hour rather than showing a phantom state forever.

## Security

- Squirrel reads your Claude OAuth token from `<config dir>/.credentials.json` for one
  purpose only: calling `https://api.anthropic.com/api/oauth/usage` (the same endpoint the
  built-in `/usage` command uses) to fetch per-model weekly usage.
- The token is never written to disk, logged, or sent anywhere else.
- The usage cache stores only percentages and timestamps — never the token or any other
  response content.
- On **Linux, macOS and WSL**, every file Squirrel writes (config, cache, activity state, the
  [shared cross-session store](#multiple-sessions)) is created with permissions `0600` —
  owner read/write only, and every directory it creates for them with `0700`. A store left
  world-listable by an older version is repaired the next time Squirrel writes to it.
  Directories Squirrel did not create — `~/.claude` itself, your home directory — are never
  re-permissioned; only its own `squirrel/` and `~/.squirrel/` trees are.
- On **Windows there is no equivalent and Squirrel does not pretend otherwise**: it sets no
  permissions at all there. `0600` has no meaning in an ACL-based filesystem — the only bit
  Python's `os.chmod` can set on Windows is *read-only*, which protects nothing from other
  users and would break Squirrel's own atomic writes. What actually protects these files on
  Windows is the ACL on your user profile directory (`C:\Users\<you>\.claude\squirrel` and
  `C:\Users\<you>\.squirrel`), which already excludes other non-administrator users. If your
  profile directory has been re-permissioned to allow other users, these files inherit that.
- **Untrusted text cannot drive your terminal.** Repository names, branch names and anything
  else Squirrel is handed are sanitised before they are rendered: terminal escape sequences
  (CSI cursor movement and erase, OSC window-title, OSC-8 hyperlinks, OSC-52 clipboard
  writes, and the 8-bit forms of both), control characters, and Unicode bidi overrides used
  for display spoofing are all removed. The surrounding text is kept — a name is stripped in
  place, never truncated, so nothing is silently hidden from you. Newlines in particular can
  never survive: they would break the line-count contract. Legitimate Unicode — emoji, CJK,
  right-to-left scripts, combining marks, zero-width joiners — is preserved untouched. The
  36-case hostile-input corpus this is tested against lives in the repository, at
  `dev/hostile_strings.json`; it is deliberately **not** part of what is installed.
- **Squirrel runs `git` in your working directory**, which means git reads that repository's
  own `.git/config`. A repository you did not write can therefore configure git to execute a
  helper — `core.fsmonitor` is the usual example — and that is git's behaviour, not
  Squirrel's, and applies equally to any tool that shells out to git. If you open untrusted
  repositories, `git config --global safe.directory` and git's own protections are the place
  to address it; `/squirrel-show git off` stops Squirrel calling git at all.
- The shared cross-session store holds only session ids, repo names, timestamps, token
  counts, costs, and model display names — never prompts, file contents, or paths beyond a
  repo name.
- `git` is invoked read-only, with a 0.5-second timeout (`gitTimeoutMs`) on every call.
- There is no telemetry of any kind.
- `/squirrel-usage off` disables the token read entirely. The 5-hour and weekly-all usage
  numbers — which Claude Code provides directly — keep working either way; only the
  per-model breakdown requires the token.

## Uninstall

```
/plugin uninstall squirrel@squirrel
```

Then remove the `statusLine` block from `settings.json` and delete `~/.claude/squirrel/` and,
if you used the [multiple-sessions](#multiple-sessions) feature, `~/.squirrel/`. On Windows
those two are `%USERPROFILE%\.claude\squirrel\` and `%USERPROFILE%\.squirrel\`.

## Development

```bash
git clone https://github.com/kubalajiri/claude-squirrel
cd claude-squirrel
python3 -m unittest discover -s dev -t dev -p 'test_*.py'
bash dev/smoke_install.sh; echo $?
claude plugin validate --strict .
```

What you installed is the repository's `plugin/` directory and nothing else. The tests, the
golden render fixture, the install smoke gate and the hostile-input corpus live in `dev/`,
outside the published tree, so they are not on your disk — clone the repo to run them.

The unit suite runs on Windows too (`python -m unittest discover ...` from `dev\`); a
handful of assertions about POSIX file permissions and symlinks are skipped there, and the
Windows behaviour they cover is asserted directly in `CrossPlatformTests` instead. The install
smoke test is a POSIX shell script — run it under Git Bash or WSL. Run it **directly**, never
through a pipe: a pipeline reports the last command's exit status and would hide a failure.

## Licence

MIT — see [LICENSE](LICENSE).
