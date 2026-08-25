# Changelog

## 1.0.0

First release: a two-line Claude Code status line — how much context is left, how much of the
5-hour and weekly limits is gone and which of your sessions spent it, your git state, clickable
reminders, watcher tasks, and an alarm across the whole bar when Claude is waiting on you.

The three sections below record what was fixed and added while it was being built, before any
of it was published. They are kept because each one names a defect worth remembering, not
because anyone ever ran those versions.

### Usage numbers, staleness and contrast

**Fixed: the 5-hour and weekly numbers could be badly out of date.** A user comparing their bar
with Claude Code's own `/usage` panel found 51% against the panel's 69% — eighteen points low.
Squirrel preferred its own cached API figure over the fresher one Claude Code had sent in the
same payload, and all of their profiles were sitting on an HTTP 429 with no backoff, retrying
every sixty seconds indefinitely.

Claude Code's payload is now the primary source for both headline numbers, so they are always
current and need no network at all. The usage API is fetched only for the **per-model weekly
buckets**, every 15 minutes by default (`usageFetchSeconds`) instead of every minute. A refused
or disabled fetch now costs you the per-model line and nothing else. A 429 backs off properly,
honouring `Retry-After`, and the backoff is shared by every session in the profile.

`/squirrel-usage off` is now a real security simplification too: with the fetch disabled
Squirrel never reads your credentials file and never has a token in the process.

**Fixed: after switching accounts the bar showed the previous account's limits.** The usage
cache carried no account identity, so a mid-session `/login` left the old figures on screen —
as fact, unmarked — until the cache expired. It read as lag; it was a wrong number.

The cache is now stamped with the account it was fetched for. A cache from a different account
is discarded rather than displayed, and the mismatch triggers an immediate refetch instead of
waiting out the TTL. A stale figure that is genuinely yours still shows `~` exactly as before —
old-but-yours and belongs-to-someone-else are different things and are now treated differently.

**Fixed: the usage numbers went unreadable when the alarm banner painted.** Three faults with
one symptom — the phase text colour flattened the green/yellow/red thresholds, `white`
resolved to a standard white that many themes draw as grey, and dimmed text stayed dim over
the banner. On the amber phase that combination read as black. Now: threshold colours survive
`white` on a banner is bright white and dimmed text is no longer dimmed over it. Threshold
colours are still flattened there; see the README for why restoring them needs a change to
how a value's colour carries its meaning.

### Watcher tasks, reminders and cross-session attribution

**Watcher tasks.** Describe a condition and an action; Squirrel writes a job to a queue when
the condition holds, and a reader acts on it. Ask Claude for one — `/squirrel-task` — rather
than editing JSON.

```
/squirrel-metrics                       what you can write conditions on
/squirrel-task-actions notify on        nothing acts until you opt in
/squirrel-task add idle-check when='idle.secs > 1800' do=notify text='tldr status check' cooldown=1800
/squirrel-task test idle-check          would it fire right now, and if not, why not
```

**The activity chip stopped lying.** It used to read `‼ IDLE 1h42m ‼` while background agents
were working, because hook events cannot see them. Squirrel now reads the tail of the session
transcript — deterministically, no model — so an outstanding tool or agent call means the
session is not idle. Two new rule metrics come with it: `pending.tools` and `pending.agents`.

**Every nudge costs you a real request** on your own account. `nudgeMaxPerHour` (default 5)
is the machine-wide ceiling, `/squirrel-config` shows what you have used, and loop safety is
**bounded, not prevented** — see the README for exactly what that means.

### The first audited build

Squirrel is a two-line Claude Code status line: how much context you have left, how much of
your 5-hour and weekly limits you have burned and which of your sessions burned it, your git
state, and a chip that goes loud when Claude is waiting on you.

```
/plugin marketplace add kubalajiri/claude-squirrel
/plugin install squirrel@squirrel
/squirrel-setup
```

Two things to know before you install:

- **Clicking a reminder to acknowledge it works in IDE terminals only** — VS Code, Cursor,
  Windsurf. Standalone terminals do not currently follow the hyperlink, because of an upstream
  Claude Code issue (anthropics/claude-code#26356, closed as not planned). It may change in
  either direction. Everything else works everywhere; `/squirrel-config` says which of the
  three conditions is unmet if a click does nothing.
- **The click listener's lifecycle is untested on Windows.** The listener is off by default
  and only ever starts when you have asked for it, so this affects nobody who has not turned
  it on. macOS is untested as a whole — see the platform notes in the README.

### Security fixes in the run-up to 1.0

Two of these were found by an audit after the feature work was finished, and both had shipped.

- **Command injection in the installed launcher.** `run.sh` — which *is* the `statusLine`
  command, so it runs every few seconds — interpolated the plugin path into a double-quoted
  POSIX string, where `$(...)`, backticks and `${...}` all expand. A path containing a command
  substitution executed it silently on every render. Now single-quoted with the `'\''` idiom,
  verified inert against nineteen shapes and verified that legitimate paths containing
  apostrophes, `$`, parentheses, spaces, backticks and semicolons still launch.
- **The same class of bug on Windows, and it would have fired on an ordinary install.** The
  stale-path check used a parenthesised `if not exist "…" ( … )` block. cmd splits a block on
  the first `)` before it finishes resolving quotes, and `(` and `)` are legal in a Windows
  path — so the remainder of the line parses as a command, and the remainder comes from the
  path. `C:\Program Files (x86)\…` is the textbook case. Read as injection-capable rather than
  merely broken, by reasoning rather than execution: there was no cmd.exe available to run it
  on, and the shape is gone regardless, replaced by `goto`.
- Unbounded command output, listener records trusted on liveness alone, a shared listener lock,
  a token reachable by a foreign process, path traversal in two session-path builders, and a
  detached child that could hang for ever when a grandchild escaped its process group — all
  fixed, each with a regression test whose teeth were checked by mutation.

Twenty-one audit findings, all closed. 843 tests, an install smoke gate, and a clean-machine
install verified as a separate UNIX user.
