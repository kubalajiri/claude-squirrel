---
description: Create, edit, remove or dry-run a watcher task — a condition that queues an action when it holds
argument-hint: <add|set|remove|test> <id> [when='<condition>'] [do=notify] [text='...'] [target=idle] [cooldown=<s>] [warn=<s>]
---

Run this exact command with the Bash tool and show the user its output verbatim:

`sh "${CLAUDE_PLUGIN_ROOT}/scripts/squirrel.sh" --task "$ARGUMENTS"`

## How to build a watcher, in order

1. **Read the vocabulary first** — `/squirrel-metrics`. Never write a condition from memory of
   a metric name; the installed version is the only one that can accept or refuse it.
2. **Enable the action** if it is not already — `/squirrel-task-actions notify on`. A task
   whose action is off is refused, not created dormant.
3. **Create it** — `add <id> when='<condition>' do=<verb> text='...' cooldown=<seconds>`.
4. **Dry-run it** — `/squirrel-task test <id>`. You cannot see the user's bar, so this is how
   you check your own work before telling them it works. It reports whether the condition
   holds right now, and if not, the metric's actual value.
5. **Show the user what you made**, in plain words: what it watches, what it will do, how
   often at most, and how to stop it. A watcher that acts on their machine is not something
   to create silently.

## The fields

| Field | Meaning |
|---|---|
| `when` | `<metric> <op> <value>`, or a bare `<metric>`. Same grammar as `/squirrel-rules`. |
| `do` | `notify`, `run`, `ack` or `snooze`. Each needs its own opt-in. |
| `text` | the exact message a `notify` sends — squirrel never composes one of its own |
| `target` | `idle` (any idle session) or a session name from `claude agents --json` |
| `cooldown` | seconds between firings. Minimum 60, default 1800. A `0` is silently raised. |
| `warn` | seconds of visible countdown before it fires (`0` fires at once) |
| `cancel` | whether that countdown can be clicked to dismiss this firing |

## Things worth telling the user

- **Dismissing a nudge buys them the cooldown, not a second.** Cancelling suppresses that
  firing and starts the cooldown; the task itself is untouched and fires again after it.
- **A nudge costs a real request** on their own account, capped by `nudgeMaxPerHour`.
- **Unknown never fires.** A condition over a metric with no value does not match, `!=`
  included.
- Squirrel only ever *queues* a job; a reader acts on it. Nothing acts from the render.

## Examples, none of which are the obvious one

    add spend    when='cost.usd > 20'        do=notify text='this session has passed $20' cooldown=3600
    add dirty    when='git.dirty'            do=notify text='uncommitted work still open'  cooldown=7200
    add weekly   when='weekly.pct >= 85'     do=notify text='weekly limit nearly gone'     cooldown=3600
    add stalled  when='pending.agents > 0'   do=notify text='agents still running'         cooldown=1800 warn=300

Every edit validates before writing, and a refused edit changes nothing. Never write the
`tasks` config key by hand — always go through this command.

Do not do anything else.
