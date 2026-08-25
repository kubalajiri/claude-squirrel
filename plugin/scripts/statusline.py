#!/usr/bin/env python3
"""Squirrel — a status line for Claude Code.

Line 1: context + 5-hour session limit + weekly limits (all models and per-model) with reset times.
Line 2: account · model · effort · flags │ cost · duration · lines changed · PR.

Usage:
  Claude Code pipes its status JSON to stdin  → prints the two lines
  statusline.py --fetch                       → refreshes the usage cache (spawned detached by the render path)
  statusline.py --state <event>               → records a session-activity event from a Claude Code hook
                                                 (reads the hook payload from stdin for the session id)

Config: `<config_dir>/squirrel/config.json`, where `<config_dir>` is `$CLAUDE_CONFIG_DIR` or
`~/.claude` — optional; missing or malformed → defaults, never an error. The plugin's packaged
defaults (`defaults/config.json`, next to this script) are always loaded first and merged under
whatever the user's config.json sets, so an absent or partial user config still renders sensibly.
See `load_config()`.
    {"accountColors": {"<full email or domain label>": "<color>", ...},    # account label colour
     "accountNames": {"<full email or domain label>": "<nickname>", ...}, # account label text override
     "repoColors": {"<repo name>": "<color>", ...},                       # repo segment colour override
     "defaultAccountColor": "<color>"}                                    # fallback account colour
  Lookup order for the account color: full email -> domain label -> defaultAccountColor -> white.
  <color> is an ANSI name (black red green yellow blue magenta cyan white gray, or bright<name>)
  or "#rrggbb"; an invalid value falls through to the next step in the lookup order.
  All per-profile state lives under `<config_dir>/squirrel/` — see data_dir(): the usage cache
  and fetch lock in `squirrel/cache/`, and per-session activity-state files directly in `squirrel/`.
  Naturally separate per CLAUDE_CONFIG_DIR profile, and untouched by plugin updates.
  Every user-facing numeric threshold (bar colours/width, cache/staleness TTLs, chip width, the
  activity-chip max age, the git call timeout, the git status cache TTL, the idle alarm delay,
  the branch-name limit, the idle-background delay) lives in one place, `TUNABLES`, and is
  settable via `/squirrel-set <key> <value>` (see `tunable()`). A few internal-only TTLs (lock
  max age, fetch timeout, no-token backoff) remain plain constants at the top of this file.
"""
from __future__ import annotations

import colorsys
import hashlib
import json
import os
import re
import stat
import shutil
import sys
import time
import unicodedata
from datetime import datetime, tzinfo
from pathlib import Path

# `subprocess` (git calls, the detached --fetch spawn) and `urllib.request`/`urllib.error` (the
# usage-API fetch, which pulls in http/ssl/email) are imported lazily, inside the functions that
# actually need them — never at module scope. Nothing on the render path uses either directly, so
# a render never pays for importing them; see the LazyImportGuardTests in test_statusline.py.

# ── constants ────────────────────────────────────────────────────────────────
# Every platform difference in this file keys off this one flag, so the Windows behaviour can be
# exercised from a POSIX test run by patching `statusline.IS_WINDOWS`. `os.name` is the right
# discriminator rather than `sys.platform`: it separates the "nt" family from everything POSIX
# (Linux, macOS, the BSDs) in exactly the places that matter here — path/launcher flavour,
# rename-over-open-file semantics, chmod, and the detach flags for a background child.
IS_WINDOWS = os.name == "nt"

USAGE_URL = "https://api.anthropic.com/api/oauth/usage"
LOCK_MAX_AGE_S = 30     # a fetch.lock older than this is considered dead
FETCH_TIMEOUT_S = 10
NO_TOKEN_RETRY_S = 600  # back off fetch attempts this long when the last one failed for lack of a token

FILLED, EMPTY = "▰", "▱"
SEP = " │ "
DOT = " · "

RESET = "\033[0m"
DIM = "\033[2m"
GREEN, YELLOW, RED, CYAN = "\033[32m", "\033[33m", "\033[31m", "\033[36m"
GRAY = "\033[90m"

STATE_MAX_AGE_S = 604800     # 7-day absolute backstop for a waiting state (idle / needs-you)
ACTIVE_STATES = ("working", "subagents")
STATE_STYLES = {
    "working":    ("⚙ working",   GREEN),    # ideal state → green
    "permission": ("◀ needs you", RED),
    "idle":       ("✓ idle",      YELLOW),   # mild warning → yellow
}   # "subagents" is formatted with its count at render time
ALARM_SEQ = "\033[1;97;41m"        # bold white on red — loud "come back" treatment
ALARM_LABELS = {"idle": "IDLE", "permission": "NEEDS YOU"}
ALARM_DEFAULT_STATES = ("idle", "permission")
IDLE_BG_STATES_DEFAULT = ("idle", "permission")
# The idle-painting ladder — how many phases, their colours, their timings — is data, shipped in
# defaults/config.json's `idlePhases`; the code carries no phase colours/timings of its own.
# This minimal one-phase ladder is used only if that file can't be read at all — see
# _shipped_phases()/load_phases().
FALLBACK_IDLE_PHASES = [{"after": 30, "bg": "#c9a227", "text": "white", "bold": True}]
ANSI_NAMES = {
    "black": 30, "red": 31, "green": 32, "yellow": 33,
    "blue": 34, "magenta": 35, "cyan": 36, "white": 37,
    "gray": 90, "grey": 90, "purple": 35,   # purple has no distinct ANSI code — aliased to magenta
}
PR_STATES = {
    "approved": "✓approved",
    "pending": "⏳pending",
    "changes_requested": "✗changes",
    "draft": "draft",
}

SEGMENT_KEYS = ("context", "tokens", "session", "weekly", "permodel", "resets", "account",
                "repo", "git", "branch", "ahead", "model", "effort", "flags",
                "sessionTokens", "cost", "duration", "lines", "pr", "agents", "chip",
                "share", "share-life", "sessions")

# The 5-hour rate-limit window, used to compute each session's *contribution* to the current
# window (not its lifetime total) for cross-session attribution — see live_sessions(). Fixed,
# not a tunable: it must track Claude Code's actual 5h limit window, not a display preference.
SESSION_WINDOW_S = 18000

# The shipped label for each segment that renders under a short textual tag, and the shipped
# default for `shareFormat` — both mirrored into defaults/config.json (the source of truth a
# user's overrides sit on top of) and verified against it by ShareConfigDefaultsTests, the same
# desync guard idlePhases/TUNABLES already get. `segment_label()`/`share_format()` fall back to
# these constants directly too, so a totally unreadable defaults/config.json still renders
# sensibly (mirrors _shipped_phases()'s FALLBACK_IDLE_PHASES).
#
# The mechanism is general — any segment can get an entry here — but today only `share` and
# `share-life` render under a configurable label; wiring another segment's fixed text (e.g. the
# "5h"/"week all" names) through segment_label() is a natural, separate follow-up.
SEGMENT_LABELS_DEFAULT = {"share": "mine", "share-life": "life"}
LABELABLE_SEGMENTS = tuple(SEGMENT_LABELS_DEFAULT)
SHARE_FORMATS = ("contribution", "share", "both")
SHARE_FORMAT_HELP = {
    "contribution": "this session's percentage points of the account's 5h utilisation, then "
                    "the account total, e.g. 'mine 19%/89%' (default)",
    "share": "this session's raw share of the window's cross-session consumption, e.g. 'mine 21%'",
    "both": "share, then contribution, e.g. 'mine 21% → 19%/89%'",
}

# Which delta drives cross-session attribution (session_contribution/share_segment/
# share_life_segment/sessions_segment/cmd_show_sessions): 'cost' (default) — the only figure
# that includes subagent usage, since Claude Code's status JSON does NOT fold subagent tokens
# into context_window.total_input_tokens/total_output_tokens while cost.total_cost_usd does.
# Ranking by tokens alone can show the session doing the most work as '0%' — the bug this
# constant's default fixes. 'tokens' keeps the pre-Task-11d, tokens-only behaviour for anyone
# who wants it. See live_sessions()/attribution_metric().
ATTRIBUTION_METRICS = ("cost", "tokens")
ATTRIBUTION_METRIC_HELP = {
    "cost": "dollar cost delta across the window/lifetime — includes subagent work, which "
            "token counts miss, so a subagent-heavy session is correctly attributed a large "
            "share even when its own token counters look small (default)",
    "tokens": "token delta only — the pre-11d behaviour; undercounts any session that ran "
              "subagents, since context_window totals exclude their usage",
}

# ── tunables ─────────────────────────────────────────────────────────────────
# Every user-configurable numeric threshold, in one place: key → (default, min, max, help).
# `defaults/config.json` mirrors these defaults exactly (verified by
# TunableTests.test_defaults_config_json_matches_tunables so the two can never desync); the
# handful of legacy constants tests/code still reference by name (ACTIVE_MAX_AGE_S,
# CHIP_WIDTH_DEFAULT) are derived aliases below, not separate values.
TUNABLES: dict[str, tuple[int, int, int, str]] = {
    "warnPercent":                (50, 0, 100, "percentage where bars turn yellow"),
    "dangerPercent":              (80, 0, 100, "percentage where bars turn red"),
    "cacheTtlSeconds":            (60, 5, 3600, "how often usage is re-fetched"),
    "staleAfterSeconds":          (600, 30, 86400, "when usage values are marked stale (~)"),
    "chipWidth":                  (0, 0, 40, "inner width of the activity chip (0 = natural: one space each side)"),
    "barCells":                   (10, 3, 40, "cells in each usage bar"),
    "activeMaxAgeSeconds":        (3600, 60, 604800, "when a stale 'working' chip disappears"),
    "gitTimeoutMs":               (500, 50, 5000, "timeout for each git call"),
    "stdinWaitSeconds":           (10, 0, 120, "how long a render waits for Claude Code's status JSON before giving up (0 = wait for ever)"),
    "clickIdleSeconds":           (1800, 30, 86400, "how long the click-to-acknowledge listener stays up with nothing clicking it"),
    "agentsLiveSeconds":          (60, 5, 3600, "how recently a background agent must have written to count as running"),
    "customTtlSeconds":           (60, 1, 86400, "how long a custom segment's command output is reused before refreshing"),
    "customOutputMaxBytes":       (65536, 256, 1048576, "most bytes a custom segment's command may print before it is cut off and killed"),
    "customValueMaxChars":        (512, 8, 4096, "most characters of a command's output that are cached and shown"),
    "nudgeMaxPerHour":            (5, 0, 200, "machine-wide ceiling on task nudges per hour, across every task"),
    "usageFetchSeconds":          (900, 30, 86400, "how often the per-model weekly buckets are fetched (the headline numbers need no fetch)"),
    "customTimeoutMs":            (2000, 50, 30000, "timeout for a custom segment's command"),
    "gitCacheSeconds":            (3, 0, 60, "how long a working dir's git status is reused before re-forking git (0 = never cache)"),
    "alarmAfterSeconds":          (120, 0, 604800, "waiting time before the loud chip alarm"),
    "branchMaxChars":             (20, 0, 200, "max characters of the git branch name (0 = no limit)"),
    "idleBackgroundAfterSeconds": (30, 0, 604800, "waiting time before the whole bar is tinted"),
    "resetWarnMinutes":           (30, 0, 1440, "minutes before a session reset when the countdown turns yellow"),
    "resetUrgentMinutes":         (15, 0, 1440, "minutes before a session reset when the countdown turns red"),
    "sessionSampleSeconds":       (60, 5, 3600, "minimum seconds between cross-session usage samples"),
    "sessionSampleKeepSeconds":   (21600, 60, 604800, "how long a session's usage samples are kept"),
    "sessionLiveSeconds":         (900, 30, 86400, "how recently a session must have sampled to count as live"),
    "burnWindowSeconds":          (600, 30, 86400, "span used to compute each session's tokens-per-minute burn rate"),
    "sessionsMax":                (4, 1, 20, "max sessions listed by the 'sessions' segment before collapsing to +N"),
    "shareWarnPercent":           (34, 0, 100, "this session's share of window consumption where its 5h-segment figure turns yellow"),
    "shareDangerPercent":         (67, 0, 100, "this session's share of window consumption where its 5h-segment figure turns red"),
    "paneReserveColumns":         (1, 0, 20, "columns left unused at the right edge of the pane"),
}

# Aliases kept for the few call sites / tests that reference these names directly; the values
# themselves live only in TUNABLES.
ACTIVE_MAX_AGE_S = TUNABLES["activeMaxAgeSeconds"][0]
CHIP_WIDTH_DEFAULT = TUNABLES["chipWidth"][0]


def tunable(config: dict | None, key: str) -> int:
    """A configurable numeric threshold: user value clamped to its bounds, else the default."""
    default, lo, hi, _ = TUNABLES[key]
    try:
        value = int((config or {}).get(key, default))
    except (TypeError, ValueError):
        return default
    return max(lo, min(hi, value))


_ANSI_RE = re.compile(r"\033\[[0-9;]*m")
# OSC sequences occupy no columns either, and unlike SGR they carry a payload — an OSC-8
# hyperlink embeds a whole URL. Counting one as visible text is not a rounding error: a link
# on a segment made visible_len() overstate the line by ~35 columns, so the fit search shed
# real segments and narrowed every bar to pay for width that was never on screen, and the
# banner painter padded a column short. Terminated by ST (ESC \) or BEL, both of which are
# in the wild.
_OSC_RE = re.compile(r"\033\][^\033\007]*(?:\033\\|\007)")
# Matches only a *plain* foreground-setting SGR sequence (standard 30-39, bright 90-97,
# truecolor 38;2;r;g;b, 256-colour 38;5;n) or a bare reset — never a compound sequence like
# ALARM_SEQ ("\033[1;97;41m", bold+fg+bg together), so the loud chip's own escape is never
# matched by this alone; apply_background() additionally excludes the chip's whole span by
# position, belt and braces. Used to make a forced phase `text` colour actually win over a
# segment's own foreground colour instead of merely being interleaved with it.
_FG_SGR_RE = re.compile(r"\033\[(?:3[0-9]|9[0-7]|38;5;\d{1,3}|38;2;\d{1,3};\d{1,3};\d{1,3}|0)m")

# ── responsive shedding: one priority scale ──────────────────────────────────
# The old LINE1_TIERS/LINE2_TIERS tables are gone. Everything a line can shed under width
# pressure — a whole segment, or one detail inside a segment — now carries a PRIORITY, and
# `_fit()` raises a pressure level until the line fits, shedding everything whose priority is
# at or below that level. Higher priority = kept longer. `None` = never shed (see Segment).
#
# The numbers below are not new policy: they are the two deleted tables, transcribed. Reading
# LINE1_TIERS top to bottom, line 1 used to shed, in order, bar cells 10→6→4→3 (the token
# counts going with the last of those), then the reset times together with the three
# attribution segments, then the bars entirely, then long labels ("week all" → "wk"). Line 2
# shed one segment per tier: pr, lines, duration, cost, flags, effort, session tokens, and
# finally repo+git together. GoldenRenderTests holds all of that to the byte.
#
# Narrowing the bars comes BEFORE dropping any text (the lowest priorities on the scale): each
# bar is pure decoration, so trading cells for columns keeps every number and label intact.
# This is a user-requested behaviour — see BarsShrinkBeforeTextIsDroppedTests.
BAR_CELL_STEPS = ((10, 6), (20, 4), (30, 3))
# The four details a segment can shed short of disappearing. Like a segment's own priority,
# each is overridable through `segmentPriority` — "keep the reset times even on a narrow
# terminal" is /squirrel-layout priority resets 95.
# `links` sheds LATE, and that is the second answer to this question, not the first. Shedding
# it first looked right on paper — an OSC-8 hyperlink costs the host ~60 columns of budget
# (see host_len) and the terminal none, so dropping it buys back more room than anything else
# and erases nothing from the screen. What it erases is the CLICK, and a reminder you cannot
# click is the feature not working: the user reported it within the hour, and their terminal
# made it worse by auto-linkifying the now-plain words into links that open nothing. So the
# ladder gives up the spend group, the effort and the token count — text nobody is reaching
# for — before it gives up an affordance the user actually uses.
DETAIL_PRIORITIES = {"tokens": 30, "resets": 40, "bars": 50, "abbrev": 60, "links": 75}


def detail_priority(config: dict | None, key: str) -> int | None:
    """A detail's shed priority, `segmentPriority` override first. None = never shed."""
    overrides = (config or {}).get("segmentPriority")
    raw = overrides.get(key) if isinstance(overrides, dict) else None
    if isinstance(raw, str) and raw.strip().lower() == "always":
        return None
    if isinstance(raw, bool) or not isinstance(raw, (int, float)):
        return DETAIL_PRIORITIES[key]
    return int(raw)


# Where a hyperlink sits on the shed scale depends on what the link is FOR, and the two cases
# pull in opposite directions:
#
#   an ACTION (click=ack on a reminder) — the click is the point of the segment. Shedding it
#     leaves a reminder the user cannot dismiss, and a terminal that auto-linkifies the now
#     plain words into links that open nothing. It is worth paying for in text, so it sheds
#     LATE (DETAIL_PRIORITIES["links"]).
#   a REFERENCE (link=https://... on an informational segment) — a convenience on text that
#     stands perfectly well alone. It costs the host ~60 columns (see host_len), so shedding
#     it FIRST is free width, and adding such a link must never cost the user visible content.
#     OscWidthAccountingTests holds exactly that property.
LINK_SHED_REFERENCE = 5


def link_levels(config: dict | None, ids: list[str], known: dict) -> dict:
    """{segment id: the level at which THAT segment's link is dropped} for one layout line.

    Links on the same line are staggered one level apart, in layout order, so a line with two
    of them sheds them one at a time. Without the stagger there is no level at which exactly
    one survives, and a pane with room for one clickable reminder shows neither — which is
    what happened to a bar carrying `drink water` and `take a break` side by side. The
    first-listed link is the last to go: layout order is an order the user already chose."""
    out: dict = {}
    rank = 0
    for seg_id in ids:
        seg = known.get(seg_id)
        if seg is None or not (getattr(seg, "click", None) or getattr(seg, "link", None)):
            continue
        level = link_shed_level(config, seg, segment_priority(config, seg_id))
        out[seg_id] = None if level is None else level - rank
        rank += 1
    return out


def link_shed_level(config: dict | None, seg, seg_priority: int | None) -> int | None:
    """The pressure level at which ONE segment's hyperlink is dropped — never later than the
    segment that carries it, since a link outliving its own segment is nonsense: the fit would
    drop the whole reminder to buy width for a hyperlink with nothing left to hang on.
    `None` = never shed (the detail is pinned and so is the segment)."""
    cap = detail_priority(config, "links") if getattr(seg, "click", None) else LINK_SHED_REFERENCE
    if cap is None:
        # Pinned (`--set-priority links always`). The segment guard below exists to stop the
        # ladder trading a whole segment for a hyperlink nobody asked to keep — which is
        # exactly what this user DID ask to keep, so it does not apply: the link outlives
        # every level, and the line sheds other segments until it fits with the URL counted.
        return None
    return cap if seg_priority is None else min(cap, seg_priority - 1)


def detail_levels(config: dict | None = None) -> tuple:
    """Every level at which a *detail* changes. `_fit()` adds the line's own segment
    priorities and walks the union; a level that changes nothing on a given line simply
    re-renders it identically (free — the producer cache holds every piece)."""
    return tuple(p for p, _ in BAR_CELL_STEPS) + tuple(
        p for p in (detail_priority(config, k) for k in DETAIL_PRIORITIES) if p is not None)


def _shed(level: int, priority: int | None) -> bool:
    return priority is not None and level >= priority


def detail_opts(level: int, config: dict | None = None) -> dict:
    """The render options in force at a given pressure level. `bar_cells` None = the
    configured `barCells`. `tokens`/`resets` also honour their own /squirrel-show switch."""
    cells = None
    for priority, value in BAR_CELL_STEPS:
        if level >= priority:
            cells = value
    return {
        "bars": not _shed(level, detail_priority(config, "bars")),
        "bar_cells": cells,
        "tokens": not _shed(level, detail_priority(config, "tokens")) and segment_enabled(config or {}, "tokens"),
        "resets": not _shed(level, detail_priority(config, "resets")) and segment_enabled(config or {}, "resets"),
        "abbrev": _shed(level, detail_priority(config, "abbrev")),
        "links": not _shed(level, detail_priority(config, "links")),
    }


# ── formatting ───────────────────────────────────────────────────────────────
def bar(pct: float, config: dict | None = None, cells: int | None = None) -> str:
    """`barCells`-wide bar, one cell per (100/cells) %; at least one cell when there is any usage.

    `cells` overrides the configured width for one call — used by the responsive ladder, which
    narrows the bars before it removes any text, since a bar is decoration and a number is not."""
    cells = tunable(config, "barCells") if cells is None else max(1, int(cells))
    filled = int(pct * cells / 100 + 0.5)
    if pct > 0:
        filled = max(1, filled)
    filled = max(0, min(cells, filled))
    return FILLED * filled + EMPTY * (cells - filled)


def pct_color(pct: float, config: dict | None = None) -> str:
    if pct >= tunable(config, "dangerPercent"):
        return RED
    if pct >= tunable(config, "warnPercent"):
        return YELLOW
    return GREEN


def share_pct_color(pct: float, config: dict | None = None) -> str:
    """Colour for this session's *share of a window's consumption* — a separate scale from
    pct_color()'s account-wide usage thresholds (`shareWarnPercent`/`shareDangerPercent`,
    default 34/67, vs `warnPercent`/`dangerPercent`, default 50/80), because it answers a
    different question: 'am I the problem?' rather than 'how full is the window?'. A session
    responsible for most of a window's burn reads red under this scale even while the
    window's total usage is still comfortably green under pct_color()."""
    if pct >= tunable(config, "shareDangerPercent"):
        return RED
    if pct >= tunable(config, "shareWarnPercent"):
        return YELLOW
    return GREEN


# ── boundary sanitiser ───────────────────────────────────────────────────────
# Untrusted text — a repo name, a branch name, anything Claude Code hands us or a custom
# segment's command prints — must not be able to drive the terminal. The rule is STRIP IN
# PLACE: remove the dangerous characters and sequences, keep every printable character around
# them, and never truncate. `repo␛[5A␛[10Dmalicious` becomes `repomalicious`, not `repo`,
# because silently swallowing the tail would hide the attack rather than defuse it.
#
# What goes, and why:
#   C0 controls (0x00-0x1F) and DEL   NUL truncates, LF breaks the two-line contract outright,
#                                     CR/BS move the cursor and overwrite, BEL rings, ESC opens
#                                     a sequence. Tab is the exception: it becomes a SPACE,
#                                     because deleting it changes the width unpredictably while
#                                     a tab stop is a width visible_len() cannot compute.
#   C1 controls (0x80-0x9F)           the 8-bit forms; 0x9B and 0x9D *are* CSI and OSC, so they
#                                     are consumed as sequences before the range strip, or
#                                     `repo\x9b31mred` would leave `repo31mred`.
#   CSI  ESC [ ... final              cursor movement, erase, alternate screen, mouse reporting.
#   OSC  ESC ] ... BEL | ESC \        window title, OSC-8 hyperlinks, OSC-52 clipboard writes.
#   Dangling ESC / ESC [ / ESC ]      an unterminated sequence still eats whatever follows it
#                                     in the terminal, so the remainder goes with it.
#   Bidi overrides and isolates       Trojan-Source display spoofing.
#
# What deliberately STAYS: zero-width joiners and non-joiners, the word joiner, soft hyphen
# and BOM (legitimate in Indic, Arabic and emoji sequences), combining marks, CJK, and RTL
# letters. Stripping those corrupts real names and breaks visible_len()'s width accounting.
#
# This is a scanner rather than a stack of regexes: ordering bugs between "strip OSC" and
# "strip CSI" are the classic way this goes wrong, and one left-to-right pass is both easier
# to reason about and linear in the input (the corpus carries a 700-character case for that).
_C0_ESC = "\033"
_C1_CSI, _C1_OSC = "\x9b", "\x9d"
_OSC_BEL = "\x07"
# 9 characters, not the 7 in the spec: LRE (U+202A) and RLE (U+202B) are the embedding pair
# that Trojan Source uses alongside the overrides, and leaving them in would be a known gap
# for no benefit. No corpus case exercises them either way.
BIDI_CONTROLS = "\u202a\u202b\u202c\u202d\u202e\u2066\u2067\u2068\u2069"
_CSI_PARAM_BYTES = set("0123456789;:<=>?")


def _skip_csi(text: str, i: int) -> int:
    """Index just past a CSI sequence starting at `i` (the byte after ESC [ or 0x9B).
    An unterminated sequence consumes the rest of the string."""
    j = i
    while j < len(text) and text[j] in _CSI_PARAM_BYTES:
        j += 1
    # Intermediate bytes 0x20-0x2F, then a final byte 0x40-0x7E.
    while j < len(text) and "\x20" <= text[j] <= "\x2f":
        j += 1
    if j < len(text) and "\x40" <= text[j] <= "\x7e":
        return j + 1
    return len(text)


def _skip_osc(text: str, i: int) -> int:
    r"""Index just past an OSC sequence starting at `i` (the byte after ESC ] or 0x9D).
    Terminated by BEL or by ST (ESC \); unterminated consumes the rest."""
    j = i
    while j < len(text):
        if text[j] == _OSC_BEL:
            return j + 1
        if text[j] == _C0_ESC and j + 1 < len(text) and text[j + 1] == "\\":
            return j + 2
        j += 1
    return len(text)


def sanitise(text):
    """Strip terminal control sequences and display-spoofing characters in place.

    Returns a str for any str input (possibly empty) and passes None straight through, so
    callers can keep using "no value" as distinct from "sanitised to nothing". Anything that
    is not a str is returned unchanged — numbers and booleans are ours, not the terminal's."""
    if not isinstance(text, str) or not text:
        return text
    out = []
    i, n = 0, len(text)
    while i < n:
        ch = text[i]
        if ch == _C0_ESC:
            nxt = text[i + 1] if i + 1 < n else ""
            if nxt == "[":
                i = _skip_csi(text, i + 2)
            elif nxt == "]":
                i = _skip_osc(text, i + 2)
            else:
                i += 1          # lone ESC: drop it, keep whatever followed
            continue
        if ch == _C1_CSI:
            i = _skip_csi(text, i + 1)
            continue
        if ch == _C1_OSC:
            i = _skip_osc(text, i + 1)
            continue
        if ch == "\t":
            out.append(" ")     # a predictable single column, never a tab stop
        elif ch < "\x20" or ch == "\x7f" or "\x80" <= ch <= "\x9f" or ch in BIDI_CONTROLS:
            pass                # dropped
        else:
            out.append(ch)
        i += 1
    return "".join(out)


# ── styled runs ──────────────────────────────────────────────────────────────
# A segment does not have "a colour". It colours PARTS of itself, and the parts mean different
# things: the 5h segment carries a contribution on the share scale and a total on the usage
# scale; the context segment dims only its token counts; the reset countdown tints
# independently of the bar beside it. So a producer returns a list of RUNS — (plain text, SGR
# prefix or None) — instead of a pre-coloured string.
#
# Two things depend on this shape:
#   * per-segment styles (`segmentStyles`) can then compose WITH a run's semantic colour
#     instead of fighting a finished string — see style_runs();
#   * the boundary sanitiser lands at _produce(), between producing and styling, where every
#     run's text is plain. Sanitising plain text is total; allow-listing escapes inside a
#     finished string is not.
# Every fragment on the bar goes through this shape — there is no escape hatch and no
# already-rendered exception. There used to be one (RAW, for the activity chip), which meant
# one fragment the boundary sanitiser could not reach at the seam; Task 17 rebuilt the chip as
# ordinary runs and retired it.
# The colours that mean "pay attention": everything pct_color()/share_pct_color()/
# countdown_color() reach for when a threshold is crossed, plus git's dirty marker. A user's
# own `fg`/`opacity` never overrides one of these, so no colour choice can stop the bar
# warning you. Every OTHER colour a segment picks — the all-clear green, the neutral grey of
# a clean branch, the dimmed token counts, an account's own colour — is decoration, and is
# the user's to change. See style_runs().
# NOTE (Task 29b): this is a HUE test standing in for an intent test, and it is wrong. Three
# unrelated producers return YELLOW — a crossed threshold, a dirty git tree, a user's own
# amber — and nothing here can tell them apart. That mistake reached a real bar once already.
# The fix is for a run to carry "this colour means a threshold was crossed" rather than for a
# reader to infer it, which also fixes a latent Task 14 bug: styling your branch yellow makes
# segmentStyles treat it as a warning and silently refuses your `fg`.
ALERT_SGRS = (YELLOW, RED)
REVERSE = "\033[7m"
# One rule for text on a painted banner, because the user's screenshot was three faults
# wearing one symptom: threshold colours flattened by the phase `text` override, `white`
# resolving to standard `37` (a mid-grey on many themes) where the loud chip has always used
# bright `97`, and `DIM` still on the token counts. Bold-standard-white plus dim on bright
# amber is what they called black, and they were right.
#
# The rule: on a painted ground, text stays READABLE and keeps MEANING something.
#   * a threshold colour is never silenced — the Task 14 rule, one layer down;
#   * but red-on-red is not a fix, so an alert value is rendered INVERSE: the alert colour
#     becomes a small chip behind dark text. Legible on any banner colour including one close
#     to the alert hue, and it still says *red* rather than merely *not-white*;
#   * a forced white becomes bright white;
#   * dim is dropped, because it is dimming against a ground that is no longer there.
BANNER_BRIGHT = {"\033[1;37m": "\033[1;97m", "\033[37m": "\033[97m"}
# SGR parameters that are an ATTRIBUTE rather than a colour: bold, dim, italic, underline,
# reverse. A run wearing one of these (the dimmed token counts are `DIM`) is saying something
# about emphasis, not hue, so a user's colour composes WITH it instead of replacing it —
# "make this cyan" should not flatten the hierarchy the dim exists to create. See style_runs().
_ATTRIBUTE_PARAMS = {"1", "2", "3", "4", "7"}
_SGR_RE = re.compile(r"^\033\[([0-9;]*)m$")


def _is_attribute_only(sgr) -> bool:
    """True when an escape carries only emphasis attributes and no colour at all."""
    if not isinstance(sgr, str):
        return False
    match = _SGR_RE.match(sgr)
    if not match or not match.group(1):
        return False
    return all(part in _ATTRIBUTE_PARAMS for part in match.group(1).split(";"))


# Separator keywords a segment may request in place of the punctuation its group would give
# it. A closed set rather than a free string — originally because the boundary sanitiser had
# not landed yet. FOLLOW-ON: sanitise() now exists, so a free literal could be allowed by
# running it through sanitise() here and dropping the keyword lookup to a default. Left closed
# deliberately until someone asks for it; the constraint should not outlive its reason.
SEPARATORS = {"dot": DOT, "bar": SEP, "space": " ", "none": ""}
STYLE_FIELDS = ("fg", "bg", "bold", "opacity", "sep", "force")


def as_runs(produced) -> list:
    """Normalise a producer's return value to a list of runs. A bare string is sugar for one
    unstyled run; None or empty produces nothing."""
    if not produced:
        return []
    if isinstance(produced, str):
        return [(produced, None)]
    return [(text, sgr) for text, sgr in produced if text]


def join_runs(runs) -> str:
    """Render runs to a terminal string — the inverse of how producers build them."""
    return "".join(text if sgr is None else f"{sgr}{text}{RESET}"
                   for text, sgr in runs)


def _escape_rgb(sgr: str | None) -> tuple | None:
    """(r, g, b) for a truecolor escape, else None. Named/basic ANSI colours have no fixed
    RGB — what '\033[32m' actually looks like is the terminal's own palette choice — so they
    cannot be blended honestly and callers fall back to SGR dim instead of inventing values."""
    if not isinstance(sgr, str):
        return None
    m = re.search(r"38;2;(\d{1,3});(\d{1,3});(\d{1,3})m", sgr)
    return (int(m.group(1)), int(m.group(2)), int(m.group(3))) if m else None


def blend_escape(fg: str | None, toward: str | None, opacity: float) -> str | None:
    """Alpha-blend a truecolor foreground toward a background colour: the real thing an
    'opacity' means on a terminal that cannot composite. Returns None when either side is not
    truecolor-resolvable — the caller then uses SGR dim, which every terminal understands."""
    a = _escape_rgb(fg)
    b = _escape_rgb(toward) or _escape_rgb(color_code(toward) if isinstance(toward, str) else None)
    if a is None or b is None:
        return None
    o = max(0.0, min(1.0, opacity))
    r, g, bl = (round(x * o + y * (1 - o)) for x, y in zip(a, b))
    return f"\033[38;2;{r};{g};{bl}m"


def segment_style(config: dict | None, seg_id: str) -> dict:
    """The user's `segmentStyles` entry for one segment; {} when unset or malformed."""
    styles = (config or {}).get("segmentStyles")
    styles = styles if isinstance(styles, dict) else {}
    entry = styles.get(seg_id)
    return entry if isinstance(entry, dict) else {}


def style_runs(runs: list, style: dict, blend_toward: str | None = None) -> str:
    """Render runs, applying one segment's user style on top of each run's own meaning.

    The rule that matters: a warning always breaks through. A user's `fg` (and `opacity`)
    recolours anything decorative — the all-clear green, a clean branch's grey, the dimmed
    token counts — but never a run wearing an ALERT_SGRS colour, so a usage bar still turns
    red at `dangerPercent`, the reset countdown still turns yellow, and a dirty branch still
    shows yellow, whatever the user picked. A bar that stops warning you because someone
    chose a nice colour is a bar that lies. `force: true` opts out for people who want total
    control. `bg` and `bold` apply to every run, since neither can hide what a colour said.

    A run's own EMPHASIS survives too, composed with the user's colour rather than replaced
    by it: the dimmed `(84k/200k)` token counts take the user's hue and stay dim, so setting
    a segment's colour does not flatten the hierarchy the dim is there to create.
"""
    if not style:
        return join_runs(runs)
    fg = color_code(style.get("fg")) if style.get("fg") else None
    bg = bg_code(style.get("bg")) if style.get("bg") else None
    bold = bool(style.get("bold"))
    force = bool(style.get("force"))
    raw_opacity = style.get("opacity")
    opacity = float(raw_opacity) if isinstance(raw_opacity, (int, float)) and not isinstance(raw_opacity, bool) else 1.0
    out = []
    for text, sgr in runs:
        emphasis = sgr if _is_attribute_only(sgr) else None
        own = None if emphasis else sgr
        protected = own in ALERT_SGRS and not force
        colour = own if protected else (fg or own)
        if opacity < 1.0 and not protected:
            faded = blend_escape(colour, blend_toward, opacity)
            colour = faded if faded else (colour or "") + DIM
        prefix = (bg or "") + (colour or "") + (emphasis or "") + ("\033[1m" if bold else "")
        out.append(f"{prefix}{text}{RESET}" if prefix else text)
    return "".join(out)


def colored(text: str, color: str) -> str:
    return f"{color}{text}{RESET}"


def countdown_color(seconds_left: float, config: dict | None = None) -> str | None:
    """Colour for a session-reset countdown: red when imminent, yellow when near, None when far or disabled."""
    warn = tunable(config, "resetWarnMinutes") * 60
    if warn <= 0:
        return None
    urgent = min(tunable(config, "resetUrgentMinutes") * 60, warn)
    if seconds_left <= urgent:
        return RED
    if seconds_left <= warn:
        return YELLOW
    return None


def bg_code(value) -> str | None:
    """Background escape from '#rrggbb' or an ANSI colour name; None when invalid or disabled."""
    if not isinstance(value, str):
        return None
    v = value.strip().lower()
    if v in ("", "off", "none", "false"):
        return None
    if v.startswith("#") and len(v) == 7:
        try:
            r, g, b = (int(v[i:i + 2], 16) for i in (1, 3, 5))
        except ValueError:
            return None
        return f"\033[48;2;{r};{g};{b}m"
    bright = v.startswith("bright")
    code = ANSI_NAMES.get(v[6:] if bright else v)
    if code is None:
        return None
    code += 10                      # 30-series foreground → 40-series background
    if bright and code < 100:
        code += 60
    return f"\033[{code}m"


def _strip_foreground(text: str) -> str:
    """Remove plain foreground-setting SGR codes and resets (see _FG_SGR_RE) so a forced text
    colour actually replaces a segment's own colour instead of merely being interleaved with
    it. Bold/dim and other non-colour codes are left alone."""
    return _FG_SGR_RE.sub("", text)


def _banner_text(line: str, bg: str, lead: str) -> str:
    """Re-style one line for a painted ground: the forced colour wins everywhere, and dim goes.

    An earlier version drew threshold values as inverse chips so a red figure would not vanish
    into a red banner. It shipped and looked wrong on a real bar, for a reason worth keeping
    written down: **it decided what was a threshold by looking at the COLOUR.** `pct_color()`
    returns YELLOW because a limit was crossed; `git_segment()` returns YELLOW because the
    tree is dirty; a custom segment is amber because its owner likes amber. All three are the
    same escape sequence, so all three were inverted, and the bar read as though parts of it
    had been highlighted at random.

    A colour is not a meaning. Restoring the threshold treatment needs the run to CARRY its
    intent — see ALERT_SGRS' comment — and until it does, flattening is merely unhelpful
    whereas inverting the wrong things is actively confusing."""
    out: list = []
    for piece in re.split(r"(\033\[[0-9;]*m)", line):
        if piece and not piece.startswith("\033["):
            out.append(lead + piece)
    return "".join(out)


def banner_lines(config: dict | None, layout: list) -> set:
    """Which layout lines (0-based) the full-width banner paints — `bannerLines`.

    Default `"auto"`: every line EXCEPT one made up entirely of ACTION segments (a reminder
    you click or acknowledge). The user asked for exactly this and the reason is sound — an
    alarm ground is a warning about the session, and painting the row of things you are meant
    to reach for buries them in it. The actions line stays on its own colours, which is also
    what makes it read as a row of controls rather than more of the alarm.

    `"all"` paints everything (what it always did). A list of 1-based line numbers paints
    exactly those. Anything unparseable falls back to `"auto"` — a bad value must never turn
    the idle alarm off."""
    setting = (config or {}).get("bannerLines", "auto")
    if isinstance(setting, str) and setting.strip().lower() == "all":
        return set(range(len(layout)))
    if isinstance(setting, (list, tuple)):
        wanted = {int(n) - 1 for n in setting
                  if not isinstance(n, bool) and isinstance(n, (int, float))}
        return {i for i in wanted if 0 <= i < len(layout)}
    known = all_segments(config)
    out = set()
    for index, ids in enumerate(layout):
        present = [known[i] for i in ids if i in known]
        actions = [seg for seg in present if getattr(seg, "click", None)]
        if present and len(actions) == len(present):
            continue                     # an actions-only row: left unpainted
        out.add(index)
    return out


def apply_background(line: str, bg: str, width: int, force_text: str | None = None,
                      flat: bool = False, reserve: int = 1) -> str:
    """Tint a whole rendered line to (at most) `width - reserve` columns — short of the pane
    on purpose: a wide/ambiguous-width glyph can still be mismeasured by a column even with
    visible_len()'s East-Asian-Width awareness, and losing a blank column at the far right is
    invisible while a truncated chip is not. `reserve` is `paneReserveColumns` (default 1).

    Without `force_text`: `bg` is applied underneath the line and every per-segment foreground
    colour is left exactly as it renders on its own (re-asserting `bg` after each inner reset,
    as before). With `force_text`: it must actually win, so every per-segment foreground colour
    (and reset) is stripped from the line first — except inside the loud alarm chip (ALARM_SEQ),
    a self-contained inverse block that is left completely untouched wherever it falls, so its
    own styling always survives."""
    for standard, bright in BANNER_BRIGHT.items():
        if force_text:
            force_text = force_text.replace(standard, bright)
    lead = bg + (force_text or "")
    if flat and force_text:
        # The escape hatch. Someone who genuinely wants everything one colour can have it,
        # and it is off by default because it hides a threshold — the same shape as Task 14's
        # `force`, pointed the other way.
        # Flat means flat, but the contrast rule still applies: dim is dimming against a
        # ground that is no longer there.
        body = lead + _strip_foreground(line).replace(DIM, "")
        pad = max(0, (width - reserve) - host_len(line))
        return f"{body}{' ' * pad}{RESET}"
    if force_text:
        pre, sep, rest = line.partition(ALARM_SEQ)
        if sep:
            end = rest.find(RESET)
            end = len(rest) if end == -1 else end + len(RESET)
            chip, after = rest[:end], rest[end:]
            body = (_banner_text(pre, bg, lead) + sep + chip
                    + lead + _banner_text(after, bg, lead))
        else:
            body = _banner_text(line, bg, lead)
    else:
        body = lead + line.replace(RESET, RESET + lead)
    # host_len, not visible_len: padding to the visible width would push a line carrying a
    # link past the host's budget and have the tail truncated away again.
    pad = max(0, (width - reserve) - host_len(line))
    return f"{body}{' ' * pad}{RESET}"


def fmt_relative(seconds: float) -> str:
    """Countdown: 42m / 1h05m / 1d 3h."""
    total = max(0, int(seconds))
    days, rest = divmod(total, 86400)
    hours, rest = divmod(rest, 3600)
    minutes = rest // 60
    if days:
        return f"{days}d {hours}h"
    if hours:
        return f"{hours}h{minutes:02d}m"
    return f"{minutes}m"


def fmt_weekday_time(epoch: float, tz: tzinfo | None = None) -> str:
    """Absolute time like 'Thu 20:00'; tz=None means the machine's local time."""
    return datetime.fromtimestamp(epoch, tz).strftime("%a %H:%M")


def fmt_tokens(n: int) -> str:
    if n < 1_000:
        return str(n)
    if n < 1_000_000:
        return f"{n // 1_000}k"
    return f"{n / 1_000_000:.1f}M"


def fmt_duration(ms: float) -> str:
    return fmt_relative(ms / 1000)


def fmt_cost(usd: float) -> str:
    return f"${usd:.2f}"


def fmt_pr(pr: dict | None) -> str:
    """'PR #42 ✓approved' / 'MR #7' / '' when absent."""
    if not isinstance(pr, dict) or pr.get("number") is None:
        return ""
    kind = "MR" if pr.get("kind") == "mr" else "PR"
    state = PR_STATES.get(pr.get("review_state") or "", "")
    return f"{kind} #{pr['number']}" + (f" {state}" if state else "")


# ── paths & config ───────────────────────────────────────────────────────────
def config_dir() -> Path:
    return Path(os.environ.get("CLAUDE_CONFIG_DIR") or Path.home() / ".claude")


def claude_json_path() -> Path:
    """~/.claude.json, or $CLAUDE_CONFIG_DIR/.claude.json when that env var is set."""
    env = os.environ.get("CLAUDE_CONFIG_DIR")
    return Path(env) / ".claude.json" if env else Path.home() / ".claude.json"


def load_json(path: Path) -> dict:
    """Parse a JSON object file; {} on any problem (missing, unreadable, malformed, not an object)."""
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def data_dir() -> Path:
    """All Squirrel state for this profile: config, cache, activity state.

    Lives under the Claude config dir (not the plugin dir) so it survives plugin
    updates and is naturally separate per CLAUDE_CONFIG_DIR profile.
    """
    return config_dir() / "squirrel"


def config_path() -> Path:
    return data_dir() / "config.json"


def default_config_path() -> Path:
    """Packaged defaults shipped with the plugin (read-only)."""
    return Path(__file__).resolve().parent.parent / "defaults" / "config.json"


def cache_dir() -> Path:
    return data_dir() / "cache"


_PACKAGED_CACHE: dict = {}


def packaged_defaults() -> dict:
    """The plugin's own defaults/config.json, parsed once per (path, mtime).

    It is read several times on the render path — load_config(), merge_keys() and the idle
    ladder all want it — and it is a file that ships with the plugin and cannot change while
    a render is in flight. Keying on mtime_ns rather than caching blindly costs one stat()
    instead of a read-and-parse, and still picks up a plugin update or a test that rewrites
    the file. Callers get a copy: the cached maps and lists must never be mutated in place."""
    path = default_config_path()
    try:
        stamp = path.stat().st_mtime_ns
    except OSError:
        return {}
    cached = _PACKAGED_CACHE.get("data")
    if _PACKAGED_CACHE.get("key") != (str(path), stamp) or cached is None:
        cached = load_json(path)
        _PACKAGED_CACHE["key"] = (str(path), stamp)
        _PACKAGED_CACHE["data"] = cached
    return {k: (dict(v) if isinstance(v, dict) else list(v) if isinstance(v, list) else v)
            for k, v in cached.items()}


def merge_keys() -> tuple:
    """The config keys whose packaged default is a MAP, and which therefore merge per key
    rather than wholesale. Derived from defaults/config.json itself so it cannot desync;
    ConfigMergeSemanticsTests pins the resulting set, so adding a new map-valued setting is a
    deliberate decision about its merge semantics rather than an accident."""
    return tuple(k for k, v in packaged_defaults().items() if isinstance(v, dict))


# ── the three config layers ─────────────────────────────────────────────────
#
# shipped defaults  →  shared (<shared>/config.json)  →  per-profile.
#
# Per-profile keeps winning, so nothing an existing user has set changes behaviour. What
# changes is that there is now a level BELOW it for anything they want everywhere.
#
# The bug this exists to fix: the user runs three profiles, and only one has a config.json.
# Their layout, reminders, nicknames and colours therefore existed in one profile and not the
# others — while the ACK STATE for those same reminders is cross-profile and syncs perfectly
# ("clicked in one and now no session shows water warning"). The DEFINITION was per-profile
# and the STATE was shared. That split was the bug.
#
# The per-key merge is Task 25's, applied unchanged across three layers instead of two. There
# is deliberately no second merge: a map-valued setting merges per entry at every level, so a
# nickname set in the shared layer and one set per-profile are both present, with the profile
# winning on a collision.

CONFIG_LAYERS = ("shipped", "shared", "profile")


def shared_config_path() -> Path:
    """The cross-profile config, beside the cross-profile state that already syncs."""
    return shared_dir() / "config.json"


def _layer_path(layer: str) -> Path:
    if layer == "shared":
        return shared_config_path()
    if layer == "profile":
        return config_path()
    raise ValueError(f"not a writable config layer: {layer!r}")


def layer_config(layer: str) -> dict:
    """One layer's raw contents, unmerged. `shipped` is the packaged defaults."""
    if layer == "shipped":
        return packaged_defaults()
    return load_json(_layer_path(layer))


def user_config() -> dict:
    """Everything the USER has set, across every writable layer, profile winning — with the
    shipped defaults left out.

    "Is this set anywhere?" is a different question from "what is its value?", and answering
    it against config_path() alone gave the wrong answer for anything set in the shared layer
    — the same mistake, one layer along, that made a tunable's fallback dead code."""
    merged: dict = {}
    for layer in ("shared", "profile"):
        merged.update(layer_config(layer))
    return merged


def resolve_config() -> tuple[dict, dict]:
    """The effective config AND where every key came from.

    Returns `(config, provenance)`, where provenance maps each key to the tuple of layers
    that contributed to it, lowest first — so a wholesale-replaced key has exactly one layer
    (the winner) and a per-key-merged map can have several.

    Provenance is computed by the SAME walk that computes the value, not reconstructed
    afterwards, because the two must not be able to disagree. It exists because with three
    layers "why is this not what I set?" is otherwise unanswerable, and that question is how
    the user found the bug in the first place.

    It is also what makes "did the USER choose this?" answerable at all — see user_set_keys().
    Asking `key in config` cannot answer it: the shipped defaults are merged in, so every key
    is present. A tunable that asked the merged config that question had a dead fallback
    branch and went on ignoring the setting it existed to honour."""
    cfg: dict = {}
    provenance: dict = {}
    per_key = merge_keys()
    for layer in CONFIG_LAYERS:
        data = layer_config(layer)
        if not isinstance(data, dict):
            continue
        for key, value in data.items():
            if key in per_key and isinstance(value, dict) and isinstance(cfg.get(key), dict):
                cfg[key] = {**cfg[key], **value}
                provenance[key] = provenance.get(key, ()) + (layer,)
            else:
                cfg[key] = value
                provenance[key] = (layer,)          # a replacement erases the layers below it
    return cfg, provenance


def config_provenance() -> dict:
    """Which layer(s) supplied each key. See resolve_config()."""
    return resolve_config()[1]


def load_config() -> dict:
    """Packaged defaults, overridden by the shared config, overridden by this profile's —
    **per key** for the map-valued settings, wholesale for everything else.

    The per-key merge matters because several maps ship with meaningful contents. A user
    config saying `{"segments": {"share": true}}` used to REPLACE the whole shipped
    `{"share": false, "share-life": false, "sessions": false}`, so turning one attribution
    segment on silently turned the other two on as well — they lost their shipped `false` and
    fell back to segment_enabled()'s "unlisted means shown". Same trap for `segmentLabels`,
    where it had been papered over in the reader (SEGMENT_LABELS_DEFAULT) instead of here.
    This is the same per-entry merge Task 10 gave `idlePhases`, and it matches what
    save_config() does when writing, so a value round-trips through both.

    LIST-valued settings (`layout`, `idlePhases`, `alarmStates`, `idleBackgroundStates`) are
    deliberately still replaced wholesale: a user's list IS the whole list, and `--reset-*`
    depends on being able to write a shorter one."""
    return resolve_config()[0]


def segment_enabled(config: dict, key: str) -> bool:
    """User-level switch for a segment; defaults to shown. Independent of the responsive width tiers."""
    segs = config.get("segments")
    segs = segs if isinstance(segs, dict) else {}
    return bool(segs.get(key, True))


def branch_limit(config: dict) -> int:
    return tunable(config, "branchMaxChars")


def read_email() -> str | None:
    return (load_json(claude_json_path()).get("oauthAccount") or {}).get("emailAddress")


def read_token() -> str | None:
    return (load_json(config_dir() / ".credentials.json").get("claudeAiOauth") or {}).get("accessToken")


# ── config writing (slash commands) ─────────────────────────────────────────
#
# ONE reader and ONE writer of the user's config.json, with three named intents on top.
#
# Nine editing commands used to do their own read-modify-write against config_path():
# phases, layout, bar rules, styles, custom segments, priorities, labels, repo colours and
# click terminals. Nine copies of the same three lines is a maintenance cost on its own, but
# the reason it had to stop is that the file is about to stop being the only place config
# lives. A layered store can only be introduced in one writer; it cannot be introduced in
# nine, and an editor that reaches past the writer would keep landing per-profile whatever
# the layer table said.
#
# The three intents are genuinely different and MUST NOT be collapsed into one:
#
#   save_config    — merge. Right for ADDING a key: a map-valued patch is merged per key,
#                    so setting one nickname leaves the others alone.
#   replace_config — set the key to exactly this value. Right when the caller has already
#                    computed the whole value from stored_config(); the merge would then be
#                    actively wrong, since it would resurrect the entries the caller just
#                    removed (`{"segmentStyles": {}}` merged onto existing styles is a no-op,
#                    which is the exact opposite of what "reset" means).
#   forget_config  — remove keys outright, so the layer below shows through again. Neither
#                    of the other two can express this: merging cannot delete, and replacing
#                    with {} or [] leaves an empty value SHADOWING the shipped default rather
#                    than deferring to it.
#
# That last distinction is why this is not a mechanical substitution. Each editor was read
# for which of the three it actually means, and the removal paths have their own tests.


def stored_config(layer: str = "profile") -> dict:
    """One writable layer's own file, as written — NOT merged with the shipped defaults.

    Editors that compute a whole value (a list, or a map they are deleting from) must start
    from the layer they are about to WRITE, never from load_config(): starting from the merged
    config would silently copy every shipped default — and every value from the layer below —
    into the file they touch, freezing it there."""
    return layer_config(layer)


# Genuinely profile-local, named explicitly rather than by exclusion (the brief's rule):
# `usageFetch` is the gate on spending THIS profile's credentials on API calls, and the token
# it spends descends from config_dir(). Everything else — appearance, thresholds, reminders,
# and the identity-keyed maps (accountNames / accountColors / repoColors, where the user's
# expectation is plainly that their own nickname follows them) — defaults to shared.
PROFILE_ONLY_KEYS = frozenset({"usageFetch"})


# `--here` is a property of the INVOCATION, not of a key, and about thirty commands would
# otherwise have to thread it through by hand — thirty chances to forget one, each of which
# would land an edit in the wrong layer silently. main() sets it once, before dispatch.
_EDIT_HERE = False

# Which layers the current command actually wrote. Recorded at the single writer, so the
# "written to ..." line reports what HAPPENED rather than what the caller intended: a config
# edit that silently lands somewhere unexpected is its own bug, and a note derived from
# intent could not catch it.
_LAYERS_WRITTEN: list = []

LAYER_NOTES = {
    "shared": "shared — every profile on this machine",
    "profile": "this profile only",
}


def _reset_write_log(here: bool = False) -> None:
    global _EDIT_HERE, _LAYERS_WRITTEN
    _EDIT_HERE = here
    _LAYERS_WRITTEN = []


def written_layers() -> tuple:
    return tuple(dict.fromkeys(_LAYERS_WRITTEN))


def layer_note() -> str:
    """The line every editing command ends with, or "" when nothing was written."""
    layers = written_layers()
    if not layers:
        return ""
    return "\n" + "; ".join(f"→ written to {layer}: {LAYER_NOTES[layer]}" for layer in layers)


def split_here(arg: str) -> tuple:
    """Pull a `--here` token out of a command's argument string, returning (rest, here).

    Slash commands pass everything the user typed as ONE shell word ("$ARGUMENTS"), so a
    `--here` they type arrives inside the argument rather than as its own argv entry — it has
    to be found in both places. Only the exact `--here` counts: a bare `here` would silently
    eat a legitimate value (`/squirrel-nick here` is a perfectly good nickname)."""
    tokens = (arg or "").split()
    kept = [t for t in tokens if t != "--here"]
    return " ".join(kept), len(kept) != len(tokens)


def target_layer(key: str, here: bool | None = None) -> str:
    """Which layer an edit to `key` lands in.

    `--here` always wins, and a profile-local key is never shared. Otherwise: **the layer the
    key is already set in**, and shared when it is set nowhere.

    That last rule is the whole migration strategy, and it is chosen over "always shared"
    deliberately. Always-shared would mean a user with a populated per-profile config edits a
    setting, watches the write land in the shared layer, and sees NOTHING change — because
    their own profile still overrides it. Editing where the value already lives means existing
    users see exactly today's behaviour, while a bare profile's first edit lands shared, which
    is the case the bug was about. Nothing moves without being asked; `/squirrel-config share`
    is how you ask."""
    if here is None:
        here = _EDIT_HERE
    if here or key in PROFILE_ONLY_KEYS:
        return "profile"
    if key in stored_config("profile"):
        return "profile"
    return "shared"


def edit_layer(key: str, here: bool | None = None) -> tuple:
    """The layer an edit to `key` lands in, AND that layer's raw contents.

    Editors that compute a whole value must use this rather than target_layer() and
    stored_config() separately: the layer they READ and the layer they WRITE have to be the
    same one, and splitting the decision across two calls is how they would drift apart."""
    layer = target_layer(key, here)
    return layer, stored_config(layer)


def _write_config(cfg: dict, layer: str = "profile") -> dict:
    """The single point at which a config layer is written. Atomic, 0600 (see write_cache)."""
    write_cache(_layer_path(layer), cfg)
    _LAYERS_WRITTEN.append(layer)
    return cfg


def _by_layer(patch: dict, here: bool | None, layer: str | None) -> dict:
    """Group a patch by the layer each of its keys belongs in."""
    grouped: dict = {}
    for key, value in patch.items():
        grouped.setdefault(layer or target_layer(key, here), {})[key] = value
    return grouped


def save_config(patch: dict, here: bool | None = None, layer: str | None = None) -> dict:
    """Merge `patch` into the config and write it atomically (0600). Returns the merged config.

    Map-valued keys merge per entry; everything else is replaced. Use replace_config() when the
    patch is the whole intended value, and forget_config() to remove a key. Keys are routed to
    their layer individually — see target_layer() — so a patch touching two settings can write
    two files, which is why the return value is the resolved config rather than one file."""
    for dest, keys in _by_layer(patch, here, layer).items():
        cfg = stored_config(dest)
        for key, value in keys.items():
            if isinstance(value, dict) and isinstance(cfg.get(key), dict):
                cfg[key] = {**cfg[key], **value}
            else:
                cfg[key] = value
        _write_config(cfg, dest)
    return load_config()


def replace_config(patch: dict, here: bool | None = None, layer: str | None = None) -> dict:
    """Set each key to EXACTLY the given value — no per-key merge. Returns the resolved config."""
    for dest, keys in _by_layer(patch, here, layer).items():
        cfg = stored_config(dest)
        cfg.update(keys)
        _write_config(cfg, dest)
    return load_config()


def forget_config(*keys: str, here: bool | None = None) -> tuple:
    """Remove `keys` so whatever the layer below says governs again. Returns the layers actually
    changed, so the command can say what it cleared.

    Clears EVERY writable layer, not just the one an edit would target — unless `here`, which
    clears only this profile. "Reset" has to mean the setting stops being set: clearing one
    layer while the other still stood would leave the user looking at an unchanged status line
    having just been told it was reset, and with three layers that is the most confusing thing
    a reset can do. `--here` is there for the deliberate narrower version."""
    targets = ("profile",) if (_EDIT_HERE if here is None else here) else ("profile", "shared")
    changed = []
    for dest in targets:
        cfg = stored_config(dest)
        if not any(key in cfg for key in keys):
            continue
        for key in keys:
            cfg.pop(key, None)
        _write_config(cfg, dest)
        changed.append(dest)
    return tuple(changed)


def _current_email() -> str | None:
    return read_email()


def cmd_set_nick(name: str) -> str:
    name = (name or "").strip()
    if not name:
        return "usage: /squirrel-nick <nickname>"
    email = _current_email()
    if not email:
        return "no account found — log in with /login first"
    save_config({"accountNames": {email: name}})
    return f"nickname for {email} set to '{name}'"


def cmd_set_color(color: str) -> str:
    color = (color or "").strip()
    if not color:
        return "usage: /squirrel-color <color>   (name like cyan, brightblue, or #rrggbb)"
    if not color_code(color):
        return f"'{color}' is not a valid colour — use a name (red green yellow blue magenta cyan white gray, bright*) or #rrggbb"
    email = _current_email()
    if not email:
        return "no account found — log in with /login first"
    save_config({"accountColors": {email: color}})
    return f"colour for {email} set to {color}"


def cmd_set_default_color(arg: str) -> str:
    value = (arg or "").strip()
    if not value:
        return "usage: /squirrel-default-color <colour>"
    if not color_code(value):
        return f"'{value}' is not a valid colour — use a name (red green yellow blue magenta cyan white gray, bright*) or #rrggbb"
    save_config({"defaultAccountColor": value})
    return f"accounts with no colour of their own now render {value}"


def cmd_set_alarm(value: str) -> str:
    value = (value or "").strip().lower()
    if value in ("off", "0", "none"):
        save_config({"alarmAfterSeconds": 0})
        return "idle alarm turned off"
    try:
        seconds = int(value)
        if seconds < 0:
            raise ValueError
    except ValueError:
        return "usage: /squirrel-alarm <seconds|off>"
    save_config({"alarmAfterSeconds": seconds})
    return f"idle alarm set to {seconds}s ({fmt_relative(seconds)})"


def cmd_set_bg(arg: str) -> str:
    value = (arg or "").strip()
    if not value:
        return "usage: /squirrel-bg [urgent] <colour|off>   (e.g. #c9a227, yellow, off)"
    head, _, rest = value.partition(" ")
    if head.lower() == "lines":
        return _cmd_bg_lines(rest.strip())
    if head.lower() == "urgent":
        rest = rest.strip()
        if not rest:
            return "usage: /squirrel-bg urgent <colour|off>   (e.g. #b3261e, red, off)"
        if rest.lower() in ("off", "none"):
            save_config({"idleBackgroundUrgent": "off"})
            return "idle alarm bar turned off (falls back to the subtle tint)"
        if not bg_code(rest):
            return f"'{rest}' is not a valid colour — use a name (red green yellow blue magenta cyan white gray, bright*) or #rrggbb"
        save_config({"idleBackgroundUrgent": rest})
        return f"idle alarm bar colour set to {rest}"
    if value.lower() in ("off", "none"):
        save_config({"idleBackground": "off"})
        return "idle background turned off"
    if not bg_code(value):
        return f"'{value}' is not a valid colour — use a name (red green yellow blue magenta cyan white gray, bright*) or #rrggbb"
    save_config({"idleBackground": value})
    return f"idle background set to {value}"


def _cmd_bg_lines(value: str) -> str:
    """`/squirrel-bg lines <auto|all|1,2>` — which lines the tint covers."""
    if not value:
        return ("usage: /squirrel-bg lines <auto|all|<n>[,<n>...]>   "
                "auto = every line except a row that is only clickable reminders")
    lowered = value.lower()
    if lowered in ("auto", "all"):
        save_config({"bannerLines": lowered})
        return ("the tint covers every line" if lowered == "all" else
                "the tint covers every line except a row of clickable reminders")
    numbers = []
    for token in value.replace(",", " ").split():
        if not token.isdigit() or int(token) < 1:
            return f"'{token}' is not a line number — use auto, all, or 1-based numbers like '1,2'"
        numbers.append(int(token))
    if not numbers:
        return "usage: /squirrel-bg lines <auto|all|<n>[,<n>...]>"
    save_config({"bannerLines": sorted(set(numbers))})
    return f"the tint covers line{'s' if len(numbers) > 1 else ''} {', '.join(map(str, sorted(set(numbers))))}"


def cmd_alerts(arg: str = "") -> str:
    """`/squirrel-alerts [on|off|focused <on|off>|test]` — the off-screen half of the alarm."""
    parts = (arg or "").split()
    if not parts or parts[0].lower() in ("show", "status"):
        phases = [p for p in load_phases(load_config())
                  if any(p.get(f) for f in PHASE_ALERT_FIELDS)]
        config = load_config()
        lines = [f"desktop alerts : {'on' if phase_alerts_enabled(config) else 'off'}",
                 f"when focused   : {'notify anyway' if alert_when_focused(config) else 'stay quiet'}"]
        if phases:
            for phase in phases:
                what = []
                if phase.get("notify"):
                    what.append("notify" if phase["notify"] is True else f"notify: {phase['notify']}")
                if phase.get("run"):
                    what.append(f"run: {phase['run']}")
                lines.append(f"  after {phase['after']}s → {', '.join(what)}")
        else:
            lines.append("  no phase has an action yet — /squirrel-phases set 300 notify=on")
        lines.append("test it with /squirrel-alerts test")
        return "\n".join(lines)
    verb = parts[0].lower()
    if verb in ("on", "off"):
        save_config({"phaseAlerts": verb == "on"})
        return f"desktop alerts {verb}"
    if verb == "focused":
        value = (parts[1].lower() if len(parts) > 1 else "")
        if value not in ("on", "off"):
            return "usage: /squirrel-alerts focused <on|off>   (on = notify even when you are looking)"
        save_config({"alertWhenFocused": value == "on"})
        return ("notifications fire even when the terminal is focused" if value == "on"
                else "notifications stay quiet while the terminal is the focused window")
    if verb == "test":
        config = load_config()
        result = desktop_notify("\U0001f43f\ufe0f Squirrel", "This is what a waiting alarm looks like.",
                                hint="", config=config)
        return f"{result}\n(no hint passed, so the focus check was skipped for this test)"
    return "usage: /squirrel-alerts [on|off|focused <on|off>|test]"


def cmd_set_bg_after(arg: str) -> str:
    value = (arg or "").strip()
    try:
        seconds = int(value)
        if seconds < 0:
            raise ValueError
    except ValueError:
        return "usage: /squirrel-bg-after <seconds>   (0 disables)"
    save_config({"idleBackgroundAfterSeconds": seconds})
    return ("idle background disabled" if seconds == 0
            else f"the whole bar is tinted after {seconds}s of waiting")


def _phase_color_error(label: str, value: str) -> str:
    return (f"'{value}' is not a valid colour for {label} — use a name (red green yellow blue "
            f"magenta cyan white gray, bright*), #rrggbb, or 'off'")


def _parse_phase_fields(tokens: list[str]) -> dict | str:
    """Parse trailing bg=/text=/bold=/notify=/run= tokens for /squirrel-phases add|set. Returns a fields
    dict, or an error string on an unknown key, an invalid colour, or an invalid bold value —
    callers must check `isinstance(..., str)` and write nothing on error."""
    fields: dict = {}
    for tok in tokens:
        if "=" not in tok:
            return (f"unknown argument '{tok}' — expected bg=<colour|off>, text=<colour|off>, "
                    f"bold=<on|off>, notify=<on|off|text>, or run=<command>")
        key, _, value = tok.partition("=")
        key, value = key.strip().lower(), value.strip()
        if key == "bold":
            v = value.lower()
            if v in ("on", "true", "yes"):
                fields["bold"] = True
            elif v in ("off", "false", "no"):
                fields["bold"] = False
            else:
                return f"'{value}' is not a valid bold value — use on/true/yes or off/false/no"
            continue
        if key == "notify":
            v = value.lower()
            if v in ("on", "true", "yes"):
                fields["notify"] = True
            elif v in ("off", "false", "no"):
                fields["notify"] = False
            else:
                fields["notify"] = value          # custom body text
            continue
        if key == "run":
            fields["run"] = "" if value.lower() in ("off", "none") else value
            continue
        if key not in ("bg", "text"):
            return (f"unknown field '{key}' — use bg=.., text=.., bold=.., notify=<on|off|text>, "
                    f"or run=<command>")
        if value.lower() in ("off", "none"):
            fields[key] = "off"
        elif color_code(value):
            fields[key] = value
        else:
            return _phase_color_error(key, value)
    return fields


def save_phase_entry(entry: dict, here: bool | None = None) -> None:
    """Merge one phase entry into the user's config.json `idlePhases` — surgically: only the
    entry whose `after` matches `entry['after']` is touched, merged onto whatever the user
    already had there (so an earlier bg=/text= edit on that same phase survives a later,
    unrelated one — see PhaseCommandTests.test_repeated_edits_to_the_same_phase_do_not_clobber_
    each_other), or replaced with a `remove: true` marker when `entry['remove']` is set. Every
    other phase in the file is left byte-for-byte alone."""
    layer, stored = edit_layer("idlePhases", here)
    phases = stored.get("idlePhases")
    phases = list(phases) if isinstance(phases, list) else []
    after = entry["after"]
    existing = None
    kept = []
    for p in phases:
        if isinstance(p, dict) and p.get("after") == after:
            existing = p
        else:
            kept.append(p)
    if entry.get("remove"):
        kept.append({"after": after, "remove": True})
    else:
        merged = dict(existing) if isinstance(existing, dict) else {}
        merged.update(entry)
        kept.append(merged)
    # replace, not merge: `kept` IS the whole ladder, already computed above.
    replace_config({"idlePhases": kept}, layer=layer)


def _list_phases() -> str:
    phases = load_phases(load_config())
    if not phases:
        return "no idle phases configured"
    lines = ["idle phase ladder (after → bg / text / bold):"]
    for p in phases:
        lines.append(f"  {p['after']:>6}s   bg={p.get('bg', '(inherit)')}   "
                      f"text={p.get('text', '(inherit)')}   bold={p.get('bold', False)}")
    return "\n".join(lines)


def cmd_phases(arg: str) -> str:
    """Backs /squirrel-phases and the --show-phases/--add-phase/--set-phase/--remove-phase/
    --reset-phases dispatch flags. Subcommands: (no args) list; add <seconds> [bg=..] [text=..]
    [bold=..]; set <seconds> [bg=..] [text=..] [bold=..]; remove <seconds>; reset. Every edit
    is surgical — see save_phase_entry() — never a wholesale rewrite of idlePhases."""
    parts = (arg or "").split()
    if not parts:
        return _list_phases()
    sub, rest = parts[0].lower(), parts[1:]
    if sub == "reset":
        # forget, not replace: an empty list would SHADOW the shipped ladder with nothing,
        # where "reset to the shipped defaults" means the shipped ladder must show through.
        forget_config("idlePhases")
        return "idle phase ladder reset to the shipped defaults"
    if sub == "remove":
        if not rest:
            return "usage: /squirrel-phases remove <seconds>"
        try:
            seconds = int(rest[0])
        except ValueError:
            return f"'{rest[0]}' is not a number — usage: /squirrel-phases remove <seconds>"
        save_phase_entry({"after": seconds, "remove": True})
        return f"phase at {seconds}s removed"
    if sub in ("add", "set"):
        if not rest:
            return f"usage: /squirrel-phases {sub} <seconds> [bg=<colour|off>] [text=<colour|off>] [bold=<on|off>]"
        try:
            seconds = int(rest[0])
            if seconds < 0:
                raise ValueError
        except ValueError:
            return (f"'{rest[0]}' is not a valid number of seconds — "
                    f"usage: /squirrel-phases {sub} <seconds> [bg=<colour|off>] [text=<colour|off>] [bold=<on|off>]")
        fields = _parse_phase_fields(rest[1:])
        if isinstance(fields, str):
            return fields
        if not fields:
            return f"usage: /squirrel-phases {sub} <seconds> bg=<colour|off> [text=<colour|off>] [bold=<on|off>]"
        save_phase_entry({"after": seconds, **fields})
        verb = "added" if sub == "add" else "updated"
        return f"phase at {seconds}s {verb}: " + ", ".join(f"{k}={v}" for k, v in fields.items())
    return f"unknown subcommand '{sub}' — one of: add, set, remove, reset (no arguments to list)"


def cmd_show_phases(_arg: str = "") -> str:
    return cmd_phases("")


def cmd_add_phase(arg: str) -> str:
    return cmd_phases("add " + (arg or "").strip())


def cmd_set_phase(arg: str) -> str:
    return cmd_phases("set " + (arg or "").strip())


def cmd_remove_phase(arg: str) -> str:
    return cmd_phases("remove " + (arg or "").strip())


def cmd_reset_phases(_arg: str = "") -> str:
    return cmd_phases("reset")


STATE_SET_KEYS = {"alarm": "alarmStates", "background": "idleBackgroundStates"}
SETTABLE_STATES = ("idle", "permission", "working", "subagents")


def cmd_set_states(arg: str) -> str:
    parts = (arg or "").split()
    if len(parts) != 2:
        return ("usage: /squirrel-states <alarm|background> <states>   "
                f"(comma-separated from: {', '.join(SETTABLE_STATES)}; or 'reset')")
    which, raw = parts[0].lower(), parts[1].lower()
    key = STATE_SET_KEYS.get(which)
    if not key:
        return f"unknown target '{parts[0]}' — use 'alarm' or 'background'"
    if raw == "reset":
        default = list(ALARM_DEFAULT_STATES if which == "alarm" else IDLE_BG_STATES_DEFAULT)
        save_config({key: default})
        return f"{which} states reset to {', '.join(default)}"
    states = [s for s in (p.strip() for p in raw.split(",")) if s]
    unknown = [s for s in states if s not in SETTABLE_STATES]
    if unknown or not states:
        return f"unknown state(s) {', '.join(unknown) or '(none given)'} — choose from: {', '.join(SETTABLE_STATES)}"
    save_config({key: states})
    return f"{which} now triggers on: {', '.join(states)}"


def cmd_set_segment(arg: str) -> str:
    parts = (arg or "").split()
    if not parts:
        cfg = load_config()
        state = ", ".join(f"{k}={'on' if segment_enabled(cfg, k) else 'off'}" for k in SEGMENT_KEYS)
        return f"usage: /squirrel-show <segment> <on|off>\nsegments: {state}"
    if len(parts) != 2 or parts[1].lower() not in ("on", "off"):
        return "usage: /squirrel-show <segment> <on|off>"
    key, value = parts[0], parts[1].lower() == "on"
    if key not in SEGMENT_KEYS:
        return f"unknown segment '{key}' — one of: {', '.join(SEGMENT_KEYS)}"
    if key in PROTECTED_SEGMENTS and not value:
        return f"'{key}' cannot be hidden — it identifies the line"
    patch: dict = {"segments": {key: value}}
    if key in SEGMENTS:
        # Absence from the layout is what "off" now means; the `segments` map is kept in step
        # so a config written by an older version (or by hand) still reads the same way.
        layout = _layout_segment_ids()
        placed = any(key in line for line in layout)
        if value and not placed:
            _layout_place(layout, key, SEGMENTS)   # back at its shipped line/order position
            patch["layout"] = layout
        elif not value and placed:
            for line in layout:
                if key in line:
                    line.remove(key)
            patch["layout"] = layout
    save_config(patch)
    return f"segment '{key}' is now {'on' if value else 'off'}"


def _layout_segment_ids(config: dict | None = None) -> list[list[str]]:
    """The layout as *configured* — unknown ids dropped, but on/off ignored. This is what the
    edit commands operate on; resolve_layout() is what the renderer sees."""
    cfg = load_config() if config is None else config
    raw = cfg.get("layout")
    if not (isinstance(raw, list) and raw and all(isinstance(line, list) for line in raw)):
        return [list(line) for line in shipped_layout()]
    known = all_segments(cfg)
    return [[i for i in line if isinstance(i, str) and i in known] for line in raw]


def _layout_place(layout: list[list[str]], seg_id: str, known: dict | None = None) -> None:
    """Insert a segment at its shipped line/order position, creating lines as needed."""
    known = known if known is not None else all_segments(load_config())
    seg = known[seg_id]
    while len(layout) <= seg.line:
        layout.append([])
    target = layout[seg.line]
    at = len([i for i in target if i in known and known[i].order < seg.order])
    target.insert(at, seg_id)


def _layout_error(layout: list[list[str]]) -> str | None:
    """Refuse a layout that loses a protected segment or repeats one. Callers must check this
    BEFORE writing — a rejected edit leaves the config exactly as it was."""
    placed = [i for line in layout for i in line]
    for seg in SEGMENTS.values():           # only BUILT-INS can be protected
        if seg.protected and seg.id not in placed:
            return f"'{seg.id}' cannot be removed — it identifies the line; nothing was changed"
    dupes = sorted({i for i in placed if placed.count(i) > 1})
    if dupes:
        return f"segment(s) appear more than once: {', '.join(dupes)}; nothing was changed"
    return None


def _layout_line_number(raw: str, layout: list[list[str]], allow_new: bool = False) -> int | str:
    """User-facing line numbers are 1-based. Returns a 0-based index, or an error string."""
    try:
        n = int(raw)
    except (TypeError, ValueError):
        return f"'{raw}' is not a line number (lines are numbered from 1)"
    limit = len(layout) + 1 if allow_new else len(layout)
    if not 1 <= n <= limit:
        return f"line {n} is out of range — the layout has {len(layout)} line(s)"
    return n - 1


def _segment_state(cfg: dict, seg_id: str) -> str:
    seg = all_segments(cfg)[seg_id]
    priority = segment_priority(cfg, seg_id)
    marks = ["always" if priority is None else str(priority)]
    if seg.protected:
        marks.append("protected")
    if not seg.protected and not segment_enabled(cfg, seg_id):
        marks.append("off")
    return f"{seg_id}({', '.join(marks)})"


def cmd_show_layout(_arg: str = "") -> str:
    """The layout, line by line, with each segment's drop priority."""
    cfg = load_config()
    layout = _layout_segment_ids(cfg)
    lines = [f"layout — {len(layout)} line(s); higher priority survives a narrow pane longer:"]
    for n, ids in enumerate(layout, 1):
        body = " ".join(_segment_state(cfg, i) for i in ids) if ids else "(empty)"
        lines.append(f"  line {n}: {body}")
    unplaced = [i for i in all_segments(cfg) if i not in {j for line in layout for j in line}]
    lines.append("  unplaced : " + (" ".join(unplaced) if unplaced else "(none)"))
    details = " ".join(f"{k}({detail_priority(cfg, k) if detail_priority(cfg, k) is not None else 'always'})"
                       for k in DETAIL_PRIORITIES)
    lines.append(f"  details  : {details}   (shed inside a segment before it is dropped)")
    lines.append("edit with /squirrel-layout move|line|swap|priority|reset")
    return "\n".join(lines)


def cmd_set_priority(arg: str) -> str:
    parts = (arg or "").split()
    if len(parts) != 2:
        return "usage: /squirrel-layout priority <segment> <number|always>"
    seg_id, value = parts[0], parts[1].lower()
    if seg_id not in all_segments(load_config()) and seg_id not in DETAIL_PRIORITIES:
        return (f"unknown segment '{seg_id}' — one of: {', '.join(all_segments(load_config()))}"
                f" (or a shed-able detail: {', '.join(DETAIL_PRIORITIES)})")
    if value == "always":
        save_config({"segmentPriority": {seg_id: "always"}})
        return f"'{seg_id}' is now never dropped on a narrow pane"
    try:
        n = int(value)
    except ValueError:
        return f"'{value}' is not a number (or 'always')"
    save_config({"segmentPriority": {seg_id: n}})
    return f"'{seg_id}' priority is now {n} (higher survives longer)"


def cmd_layout(arg: str = "") -> str:
    """The whole layout in one subcommand — what /squirrel-layout runs. Every edit is surgical
    on the user's own layout and is validated before anything is written; a refused edit
    changes nothing."""
    parts = (arg or "").split()
    if not parts or parts[0].lower() in ("show", "list"):
        return cmd_show_layout()
    verb, rest = parts[0].lower(), parts[1:]
    if verb == "reset":
        # replace, not save_config(): its per-key dict merge would keep every existing
        # segmentPriority override — the one thing "reset" must discard.
        layer = target_layer("layout")
        replace_config({"layout": shipped_layout(), "segmentPriority": {}}, layer=layer)
        return "layout and priorities reset to the shipped arrangement"
    if verb == "priority":
        return cmd_set_priority(" ".join(rest))
    layout = _layout_segment_ids()
    if verb == "move":
        if not 2 <= len(rest) <= 3:
            return "usage: /squirrel-layout move <segment> <line> [position]"
        seg_id = rest[0]
        if seg_id not in all_segments(load_config()):
            return f"unknown segment '{seg_id}' — one of: {', '.join(all_segments(load_config()))}"
        line_no = _layout_line_number(rest[1], layout, allow_new=True)
        if isinstance(line_no, str):
            return line_no
        for line in layout:
            if seg_id in line:
                line.remove(seg_id)
        while len(layout) <= line_no:
            layout.append([])
        target = layout[line_no]
        if len(rest) == 3:
            try:
                pos = int(rest[2])
            except ValueError:
                return f"'{rest[2]}' is not a position (positions are numbered from 1)"
            if not 1 <= pos <= len(target) + 1:
                return f"position {pos} is out of range — line {line_no + 1} holds {len(target)} segment(s)"
            target.insert(pos - 1, seg_id)
        else:
            target.append(seg_id)
    elif verb == "line":
        if not rest:
            return "usage: /squirrel-layout line <n> <segment> [<segment> ...]"
        line_no = _layout_line_number(rest[0], layout, allow_new=True)
        if isinstance(line_no, str):
            return line_no
        unknown = [i for i in rest[1:] if i not in all_segments(load_config())]
        if unknown:
            return f"unknown segment(s): {', '.join(unknown)}; nothing was changed"
        while len(layout) <= line_no:
            layout.append([])
        new_ids = list(rest[1:])
        for n, line in enumerate(layout):        # a segment lives on exactly one line
            if n != line_no:
                layout[n] = [i for i in line if i not in new_ids]
        layout[line_no] = new_ids
    elif verb == "swap":
        if len(rest) != 2:
            return "usage: /squirrel-layout swap <line> <line>"
        a = _layout_line_number(rest[0], layout)
        if isinstance(a, str):
            return a
        b = _layout_line_number(rest[1], layout)
        if isinstance(b, str):
            return b
        layout[a], layout[b] = layout[b], layout[a]
    else:
        return ("usage: /squirrel-layout [show | move <segment> <line> [position] | "
                "line <n> <segment>... | swap <line> <line> | priority <segment> <n|always> | reset]")
    error = _layout_error(layout)
    if error:
        return error
    save_config({"layout": layout})
    return cmd_show_layout()


def _parse_style_fields(tokens: list[str]) -> dict | str:
    """Parse `fg=.. bg=.. bold=.. opacity=.. sep=.. force=..` for /squirrel-style set.
    Returns a fields dict (a value of None means "clear this field"), or an error string —
    callers must check `isinstance(..., str)` and write nothing on error."""
    fields: dict = {}
    for tok in tokens:
        if "=" not in tok:
            return (f"unknown argument '{tok}' — expected "
                    + ", ".join(f"{f}=.." for f in STYLE_FIELDS))
        key, _, value = tok.partition("=")
        key, value = key.strip().lower(), value.strip()
        if key not in STYLE_FIELDS:
            return f"unknown field '{key}' — one of: {', '.join(STYLE_FIELDS)}"
        low = value.lower()
        if key in ("bold", "force"):
            if low in ("on", "true", "yes"):
                fields[key] = True
            elif low in ("off", "false", "no"):
                fields[key] = False
            else:
                return f"'{value}' is not a valid {key} value — use on/true/yes or off/false/no"
        elif key in ("fg", "bg"):
            if low in ("off", "none", "auto"):
                fields[key] = None
            elif (color_code(value) if key == "fg" else bg_code(value)):
                fields[key] = value
            else:
                return (f"'{value}' is not a valid colour for {key} — a name (red, brightcyan, "
                        f"gray, …), '#rrggbb', or 'auto' to clear it")
        elif key == "opacity":
            if low in ("off", "none", "auto"):
                fields[key] = None
                continue
            try:
                num = float(value)
            except ValueError:
                return f"'{value}' is not a number — opacity is 0.0 (invisible) to 1.0 (full)"
            if not 0.0 <= num <= 1.0:
                return f"opacity {value} is out of range — 0.0 (invisible) to 1.0 (full)"
            fields[key] = num
        elif key == "sep":
            if low in ("auto", "off"):
                fields[key] = None
            elif low in SEPARATORS:
                fields[key] = low
            else:
                return (f"'{value}' is not a separator — one of: "
                        f"{', '.join(SEPARATORS)}, or 'auto' to follow the layout")
    return fields


def _style_summary(seg_id: str, entry: dict) -> str:
    bits = [f"{k}={sanitise(str(entry[k]))}" for k in STYLE_FIELDS
            if k in entry and entry[k] is not None]
    return f"{seg_id}: {' '.join(bits) if bits else '(no style)'}"


def cmd_show_styles(_arg: str = "") -> str:
    """Every segment that carries a style, plus what can be set."""
    styles = load_config().get("segmentStyles")
    styles = styles if isinstance(styles, dict) else {}
    lines = ["segment styles:"]
    configured = [(k, v) for k, v in styles.items() if isinstance(v, dict) and v]
    lines += [f"  {_style_summary(k, v)}" for k, v in configured] or ["  (none — every segment renders with its shipped colours)"]
    lines.append("")
    lines.append("fields: fg=<colour|auto>  bg=<colour|auto>  bold=<on|off>  "
                 "opacity=<0.0-1.0|auto>  sep=<" + "|".join(SEPARATORS) + "|auto>  force=<on|off>")
    lines.append("  fg/opacity apply only to the parts of a segment with no meaning-carrying")
    lines.append("  colour of their own, so a style can never hide a threshold warning;")
    lines.append("  force=on overrides that. sep replaces the punctuation before the segment.")
    lines.append("edit with /squirrel-style set <segment> <field>=<value> ...")
    return "\n".join(lines)


def cmd_style(arg: str = "") -> str:
    """Per-segment styling in one subcommand — what /squirrel-style runs. Every edit overlays
    only the fields given onto that one segment, and validates before anything is written."""
    parts = (arg or "").split()
    if not parts or parts[0].lower() in ("show", "list"):
        return cmd_show_styles()
    verb, rest = parts[0].lower(), parts[1:]
    if verb == "reset":
        # replace with an empty map, not forget: `segmentStyles` has no shipped contents to
        # fall back to, and this preserves the byte-for-byte behaviour the fixture pins.
        layer = target_layer("segmentStyles")
        replace_config({"segmentStyles": {}}, layer=layer)
        return "every segment style cleared — the shipped colours are back"
    if verb not in ("set", "clear"):
        return ("usage: /squirrel-style [show | set <segment> <field>=<value> ... | "
                "clear <segment> | reset]")
    if not rest:
        return f"usage: /squirrel-style {verb} <segment>" + (" <field>=<value> ..." if verb == "set" else "")
    seg_id = rest[0]
    if seg_id not in all_segments(load_config()):
        return f"unknown segment '{seg_id}' — one of: {', '.join(all_segments(load_config()))}"
    layer, stored = edit_layer("segmentStyles")
    styles = dict(stored.get("segmentStyles") or {})
    if verb == "clear":
        styles.pop(seg_id, None)
        # replace: a merge would put the entry straight back.
        replace_config({"segmentStyles": styles}, layer=layer)
        return f"style cleared for '{seg_id}'"
    fields = _parse_style_fields(rest[1:])
    if isinstance(fields, str):
        return fields + "; nothing was changed"
    if not fields:
        return f"usage: /squirrel-style set {seg_id} <field>=<value> ...   ({', '.join(STYLE_FIELDS)})"
    # Overlay onto the segment's EXISTING entry, computed here rather than handed to
    # save_config(): its merge is one level deep, so passing {seg_id: fields} would replace
    # the whole entry and silently drop the fields the user is not touching — the same trap
    # load_config() had for the maps themselves. Having computed it, we replace.
    entry = dict(styles.get(seg_id) or {})
    for key, value in fields.items():
        if value is None:
            entry.pop(key, None)
        else:
            entry[key] = value
    if entry:
        styles[seg_id] = entry
    else:
        styles.pop(seg_id, None)
    replace_config({"segmentStyles": styles}, layer=layer)
    return _style_summary(seg_id, entry)


RULE_FIELDS = ("bg", "text", "bold", "priority")


_RULE_FIELD_TOKEN_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_]*=")


def _split_rule_args(tokens: list[str]) -> tuple:
    """Separate the condition from the trailing field assignments.

    A token counts as a field when it starts with an identifier followed by '=' — which is
    true of `bg=red` and of a misspelt `sparkle=on` (so the user gets "unknown field
    'sparkle'" rather than having it silently absorbed into the condition and being told the
    rule paints nothing), but never of the condition's own '==' or '!=', whose left-hand side
    is empty or '!'."""
    for i, tok in enumerate(tokens):
        if _RULE_FIELD_TOKEN_RE.match(tok):
            return " ".join(tokens[:i]).strip(), tokens[i:]
    return " ".join(tokens).strip(), []


def _parse_rule_fields(tokens: list[str]) -> dict | str:
    """Parse bg=/text=/bold=/priority= for the rule commands. Returns a fields dict (None
    means clear), or an error string — callers must write nothing on error."""
    fields: dict = {}
    for tok in tokens:
        key, _, value = tok.partition("=")
        key, value = key.strip().lower(), value.strip()
        low = value.lower()
        if key == "bold":
            if low in ("on", "true", "yes"):
                fields["bold"] = True
            elif low in ("off", "false", "no"):
                fields["bold"] = False
            else:
                return f"'{value}' is not a valid bold value — use on/true/yes or off/false/no"
        elif key == "priority":
            if low in ("auto", "off", "none"):
                fields["priority"] = None
                continue
            try:
                fields["priority"] = int(value)
            except ValueError:
                return f"'{value}' is not a whole number — priority decides which of two matching rules wins"
        elif key in ("bg", "text"):
            if low in ("off", "none", "auto"):
                fields[key] = None
            elif (bg_code(value) if key == "bg" else color_code(value)):
                fields[key] = value
            else:
                return f"'{value}' is not a valid colour for {key}"
        else:
            return f"unknown field '{key}' — one of: {', '.join(RULE_FIELDS)}"
    return fields


def _user_rules() -> list:
    raw = stored_config(target_layer("barRules")).get("barRules")
    return [r for r in raw if isinstance(r, dict)] if isinstance(raw, list) else []


def _save_user_rules(rules: list, here: bool | None = None) -> str:
    # `rules` is the whole list the caller computed from _user_rules(), so: replace.
    layer = target_layer("barRules", here)
    replace_config({"barRules": rules}, layer=layer)
    return layer


def _rule_line(index: int, rule: dict) -> str:
    # Echoing config back is still rendering it. Found by the all-surfaces guard, not by
    # inspection: this was the THIRD output path outside the seam, after the chip and
    # --show-sessions. A user's own config is a lower bar than the cross-profile store, but
    # a listing that can emit a hyperlink is a listing that can be used to mislead.
    bits = [f"{k}={sanitise(str(rule[k]))}" for k in RULE_FIELDS if rule.get(k) is not None]
    tag = "ladder" if rule.get("source") == "phase" else "yours "
    body = f"  {index:>2}. [{tag}] when {sanitise(str(rule.get('when')))!r}"
    if bits:
        body += "   " + " ".join(bits)
    if rule.get("error"):
        body += f"\n        !! IGNORED — {sanitise(str(rule['error']))}"
    return body


# Metrics that only exist while Claude Code is piping a status line — they come off the
# per-render status JSON, not from anything a command can read. `--show-rules` can evaluate
# everything else, and says so rather than guessing about these.
LIVE_ONLY_METRICS = ("ctx.pct", "cost.usd", "duration.min", "lines.added", "lines.removed",
                     "tokens.session", "model.name", "effort.level", "share.pct")


def _show_rules_ctx(cfg: dict) -> dict:
    """The best context a command-line invocation can build: the usage cache for the limits,
    the working directory for git and the repo name, and this profile's real state files."""
    cache_file, _lock = cache_paths()
    cache = load_json(cache_file)
    now = time.time()
    status = {"workspace": {"current_dir": os.getcwd()}}
    limits, stale, source = pick_limits(status, cache, now, cfg)
    return _ctx_for(status, limits, stale, source, read_email(), cfg, now, None, None)


def cmd_show_rules(_arg: str = "") -> str:
    """Every bar rule in effect, in evaluation order, marking the one winning right now —
    "why is my bar purple?" should be answerable by looking, not by reasoning about
    precedence."""
    cfg = load_config()
    rules = bar_rules(cfg)
    ctx = _show_rules_ctx(cfg)
    unknown = 0
    states: list = []
    for rule in rules:
        parsed = parse_condition(rule.get("when"))
        if rule.get("error") or isinstance(parsed, str):
            states.append(None)
        elif parsed[0] in LIVE_ONLY_METRICS:
            states.append("?")
            unknown += 1
        else:
            states.append("yes" if condition_holds(ctx, rule["when"]) else "no")
    winner = None
    best = None
    for index, (rule, state) in enumerate(zip(rules, states)):
        if state != "yes":
            continue
        key = (_rule_priority(rule), index)
        if best is None or key >= best:
            winner, best = index, key
    lines = ["bar rules — later rules win, higher priority wins over that:"]
    for i, rule in enumerate(rules):
        line = _rule_line(i + 1, rule)
        if i == winner:
            line += "   <== WINNING NOW"
        elif states[i] == "yes":
            line += "   (matches)"
        elif states[i] == "?":
            line += "   (needs the live status line to tell)"
        lines.append(line)
    if not rules:
        lines.append("  (none)")
    elif winner is None and not unknown:
        lines.append("  nothing matches right now — the bar is unpainted")
    if unknown:
        lines.append(f"  {unknown} rule(s) read values that only exist while the bar is "
                     f"rendering, so they could not be checked here — the winner may differ")
    broken = [r for r in rules if r.get("error")]
    if broken:
        lines.append(f"  {len(broken)} rule(s) ignored — fix the message above; the bar still renders")
    lines.append("")
    lines.append("metrics: " + ", ".join(sorted(METRICS)))
    lines.append("condition: <metric> <op> <value>  (ops: " + " ".join(RULE_OPS) +
                 ") or a bare <metric> for true/false")
    lines.append("edit with /squirrel-rules add|set|remove|reset")
    return "\n".join(lines)


def cmd_rules(arg: str = "") -> str:
    """Conditional bar painting in one subcommand — what /squirrel-rules runs. Rules are the
    user's own data, never fetched and never evaluated as code (see parse_condition())."""
    parts = (arg or "").split()
    if not parts or parts[0].lower() in ("show", "list"):
        return cmd_show_rules()
    verb, rest = parts[0].lower(), parts[1:]
    if verb == "reset":
        _save_user_rules([])
        return "your bar rules are cleared — only the idle ladder remains"
    if verb not in ("add", "set", "remove"):
        return ("usage: /squirrel-rules [show | add <condition> bg=.. text=.. [bold=on|off] "
                "[priority=<n>] | set <condition> <field>=<value> ... | remove <condition> | reset]")
    condition, field_tokens = _split_rule_args(rest)
    if not condition:
        return f"usage: /squirrel-rules {verb} <condition> ..."
    parsed = parse_condition(condition)
    if isinstance(parsed, str):
        return parsed + "; nothing was changed"
    rules = _user_rules()
    index = next((i for i, r in enumerate(rules) if r.get("when") == condition), None)
    if verb == "remove":
        if index is None:
            ladder = any(r.get("when") == condition and r.get("source") == "phase"
                         for r in bar_rules(load_config()))
            hint = ("that is a rule from the idle ladder — edit it with "
                    f"/squirrel-phases remove {condition.split('>')[-1].strip()}"
                    if ladder else "run /squirrel-rules to see your rules")
            return f"no rule of your own matches {condition!r} — {hint}; nothing was changed"
        rules.pop(index)
        _save_user_rules(rules)
        return f"removed the rule for {condition!r}"
    fields = _parse_rule_fields(field_tokens)
    if isinstance(fields, str):
        return fields + "; nothing was changed"
    if verb == "set" and index is None:
        return f"no rule of your own matches {condition!r} — use 'add' to create it; nothing was changed"
    entry = dict(rules[index]) if index is not None else {"when": condition}
    for key, value in fields.items():
        if value is None:
            entry.pop(key, None)
        else:
            entry[key] = value
    if not (entry.get("bg") or entry.get("text")):
        return "a rule needs bg, text, or both — it has to paint something; nothing was changed"
    if index is None:
        rules.append(entry)
    else:
        rules[index] = entry
    _save_user_rules(rules)
    return cmd_show_rules()


_CUSTOM_ID_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_-]{0,31}$")
# These three take the REST of the line as their value, because text, commands and conditions
# all contain spaces and a status-line command gets one quoted argument to work with. Put one
# of them last; use a second `set` for another.
CUSTOM_REST_FIELDS = ("text", "command", "when")


def _parse_custom_fields(raw: str) -> dict | str:
    """Parse `field=value ...` for the custom-segment commands. A rest-field swallows the
    remainder of the line. Returns a fields dict (None = clear), or an error string."""
    fields: dict = {}
    tokens = (raw or "").split()
    i = 0
    while i < len(tokens):
        tok = tokens[i]
        if "=" not in tok:
            return f"'{tok}' is not a field — expected <field>=<value>, one of: {', '.join(CUSTOM_FIELDS[1:])}"
        key, _, value = tok.partition("=")
        key = key.strip().lower()
        if key in CUSTOM_REST_FIELDS:
            rest = " ".join([value] + tokens[i + 1:]).strip()
            if rest.lower() in ("off", "none", "auto", ""):
                fields[key] = None
            elif key == "when":
                parsed = parse_condition(rest)
                if isinstance(parsed, str):
                    return parsed
                fields[key] = rest
            else:
                fields[key] = rest
            break
        value = value.strip()
        low = value.lower()
        if key in ("ack", "shared"):
            if low in ("on", "true", "yes"):
                fields[key] = True
            elif low in ("off", "false", "no"):
                fields[key] = False
            else:
                return f"'{value}' is not a valid {key} value — use on/true/yes or off/false/no"
        elif key == "every":
            if low in ("off", "none", "auto"):
                fields[key] = None
            else:
                seconds = _duration_seconds(value)
                if seconds is None:
                    return f"'{value}' is not a duration — try 30m, 2h, 90s, or a number of seconds"
                fields[key] = seconds
        elif key == "at":
            if low in ("off", "none", "auto"):
                fields[key] = None
            elif _parse_at(value):
                fields[key] = value
            else:
                return f"'{value}' is not a time of day — use HH:MM, 24-hour"
        elif key == "line":
            if low in ("off", "none", "auto"):
                fields[key] = None
            else:
                try:
                    n = int(value)
                except ValueError:
                    return f"'{value}' is not a line number (lines are numbered from 1)"
                if n < 1:
                    return f"line {n} is out of range — lines are numbered from 1"
                fields[key] = n
        elif key == "click":
            if low in ("off", "none", "auto"):
                fields[key] = None
            elif low == "ack" or (low.startswith("snooze:") and low[7:].isdigit()):
                fields[key] = low
            else:
                return (f"'{value}' is not a click action — use ack, or snooze:<minutes>")
        elif key == "link":
            if low in ("off", "none", "auto"):
                fields[key] = None
            elif safe_link(value):
                fields[key] = safe_link(value)
            else:
                return f"'{value}' is not a usable link — http:// or https:// only, no spaces"
        else:
            return f"unknown field '{key}' — one of: {', '.join(CUSTOM_FIELDS[1:])}"
        i += 1
    return fields


def _duration_seconds(value: str):
    """'30m' / '2h' / '90s' / '1800' -> seconds; None when it is not a duration."""
    text = value.strip().lower()
    if not text:
        return None
    unit, digits = 1, text
    if text[-1] in "smhd":
        unit = {"s": 1, "m": 60, "h": 3600, "d": 86400}[text[-1]]
        digits = text[:-1]
    try:
        amount = float(digits)
    except ValueError:
        return None
    return int(amount * unit) if amount > 0 else None


def _fmt_duration_short(seconds) -> str:
    seconds = int(seconds)
    for unit, size in (("d", 86400), ("h", 3600), ("m", 60)):
        if seconds % size == 0 and seconds >= size:
            return f"{seconds // size}{unit}"
    return f"{seconds}s"


def _custom_status_line(cfg: dict, spec: dict, ctx: dict) -> str:
    bits = []
    if spec.get("command"):
        bits.append(f"command={sanitise(str(spec['command']))!r}")
    elif spec.get("text"):
        bits.append(f"text={sanitise(str(spec['text']))!r}")
    for key in ("every", "at", "when", "line", "link", "click"):
        if spec.get(key) is not None:
            value = _fmt_duration_short(spec[key]) if key == "every" else spec[key]
            bits.append(f"{key}={sanitise(str(value))}")
    for key in ("ack", "shared"):
        if spec.get(key):
            bits.append(key)
    state = []
    if spec.get("command") and not custom_commands_enabled(cfg):
        state.append("BLOCKED: command segments are off — /squirrel-custom-commands on")
    else:
        state.append("showing" if custom_due(ctx, spec) else "acknowledged, back later")
        if spec.get("command"):
            entry = load_json(custom_cache_path()).get(spec["id"]) or {}
            if entry.get("error"):
                state.append(f"last run: {entry['error']}")
    return f"  {spec['id']}: {' '.join(bits)}\n      {' · '.join(state)}"


def _custom_ctx(cfg: dict) -> dict:
    return _ctx_for({}, {}, False, "stdin", read_email(), cfg, time.time(), None, None)


def cmd_show_custom(_arg: str = "") -> str:
    """Every custom segment, what it is, and whether it is showing right now."""
    cfg = load_config()
    specs = custom_specs(cfg)
    ctx = _custom_ctx(cfg)
    lines = ["custom segments:"]
    lines += [_custom_status_line(cfg, spec, ctx) for spec in specs] or ["  (none)"]
    lines.append("")
    lines.append("fields: text=<rest of line> | command=<rest of line>   every=<30m|2h|90s>  "
                 "at=<HH:MM>  ack=<on|off>  shared=<on|off>")
    lines.append("        when=<condition, rest of line>  line=<n>  link=<https://...>")
    lines.append("a reminder is just: every=<how often> ack=on   (add shared=on to clear it "
                 "in every session at once)")
    lines.append("acknowledge with /squirrel-done <id>, or /squirrel-done all")
    return "\n".join(lines)


def cmd_custom(arg: str = "") -> str:
    """Custom segments in one subcommand — what /squirrel-custom runs."""
    parts = (arg or "").split()
    if not parts or parts[0].lower() in ("show", "list"):
        return cmd_show_custom()
    verb, rest = parts[0].lower(), parts[1:]
    if verb == "reset":
        # replace with an empty list, not forget: there are no shipped custom segments, and
        # this keeps the observable file identical to what it was before the refactor.
        layer = target_layer("customSegments")
        replace_config({"customSegments": []}, layer=layer)
        return "every custom segment removed"
    if verb not in ("add", "set", "remove"):
        return ("usage: /squirrel-custom [show | add <id> <field>=<value> ... | "
                "set <id> <field>=<value> ... | remove <id> | reset]")
    if not rest:
        return f"usage: /squirrel-custom {verb} <id>" + ("" if verb == "remove" else " <field>=<value> ...")
    seg_id = rest[0]
    if not _CUSTOM_ID_RE.match(seg_id):
        return (f"'{seg_id}' is not a usable id — letters, digits, dash and underscore, "
                "starting with a letter; nothing was changed")
    if seg_id in SEGMENTS:
        return f"'{seg_id}' is already a built-in segment — pick another id; nothing was changed"
    layer, stored = edit_layer("customSegments")
    specs = [c for c in (stored.get("customSegments") or []) if isinstance(c, dict)]
    index = next((i for i, c in enumerate(specs) if c.get("id") == seg_id), None)
    if verb == "remove":
        if index is None:
            return f"no custom segment called '{seg_id}'; nothing was changed"
        specs.pop(index)
        # replace: merging a list is not a thing, but more to the point `specs` is the whole
        # list with the removed entry already gone.
        replace_config({"customSegments": specs}, layer=layer)
        return f"removed custom segment '{seg_id}'"
    fields = _parse_custom_fields(" ".join(rest[1:]))
    if isinstance(fields, str):
        return fields + "; nothing was changed"
    if verb == "set" and index is None:
        return f"no custom segment called '{seg_id}' — use 'add' to create it; nothing was changed"
    if not fields:
        return f"usage: /squirrel-custom {verb} {seg_id} <field>=<value> ...   ({', '.join(CUSTOM_FIELDS[1:])})"
    entry = dict(specs[index]) if index is not None else {"id": seg_id}
    for key, value in fields.items():
        if value is None:
            entry.pop(key, None)
        else:
            entry[key] = value
    if not (entry.get("text") or entry.get("command")):
        return "a custom segment needs text= or command= — it has to show something; nothing was changed"
    if entry.get("text") and entry.get("command"):
        return "use text= or command=, not both; nothing was changed"
    if index is None:
        specs.append(entry)
    else:
        specs[index] = entry
    replace_config({"customSegments": specs}, layer=layer)
    return cmd_show_custom()


def cmd_enable_custom_commands(arg: str) -> str:
    value = (arg or "").strip().lower()
    if value in ("on", "true", "yes"):
        save_config({"customCommands": True})
        return ("custom segment commands are ON — Squirrel will now run the `command` of any "
                "custom segment in your config. Only your own config can define them, but "
                "note that anything able to edit your config can therefore run a command.")
    if value in ("off", "false", "no"):
        save_config({"customCommands": False})
        return "custom segment commands are OFF — `text` segments still work"
    return "usage: /squirrel-custom-commands <on|off>"


def cmd_ack(arg: str) -> str:
    """Acknowledge a due custom segment — /squirrel-done. `all` clears every one of them."""
    target = (arg or "").strip()
    if not target:
        return "usage: /squirrel-done <id|all>"
    cfg = load_config()
    ackable = [c for c in custom_specs(cfg) if c.get("ack")]
    if target.lower() == "all":
        chosen = ackable
        if not chosen:
            return "nothing to acknowledge — no custom segment has ack=on"
    else:
        spec = next((c for c in custom_specs(cfg) if c["id"] == target), None)
        if spec is None:
            return f"no custom segment called '{target}'"
        if not spec.get("ack"):
            return (f"'{target}' cannot be acknowledged — give it ack=on first: "
                    f"/squirrel-custom set {target} ack=on")
        chosen = [spec]
    now = time.time()
    for spec in chosen:
        shared = bool(spec.get("shared"))
        path = custom_state_path(shared)
        state = load_json(path)
        state = state if isinstance(state, dict) else {}
        state[spec["id"]] = {**(state.get(spec["id"]) if isinstance(state.get(spec["id"]), dict) else {}),
                             "acked_at": now}
        write_cache(path, state)
    names = ", ".join(c["id"] for c in chosen)
    when = [c for c in chosen if _positive_seconds(c.get("every"))]
    back = (" — back in " + _fmt_duration_short(min(_positive_seconds(c["every"]) for c in when))
            if when else "")
    return f"acknowledged: {names}{back}"


def cmd_snooze(arg: str) -> str:
    """Push a due segment out by N minutes without acknowledging its full interval."""
    parts = (arg or "").split()
    if len(parts) != 2:
        return "usage: /squirrel-snooze <id> <minutes>"
    seg_id, raw = parts
    cfg = load_config()
    spec = next((c for c in custom_specs(cfg) if c["id"] == seg_id), None)
    if spec is None:
        return f"no custom segment called '{seg_id}'"
    if not spec.get("ack"):
        return f"'{seg_id}' cannot be snoozed — it has no ack=on"
    minutes = _duration_seconds(raw if raw[-1:] in "smhd" else raw + "m")
    if minutes is None:
        return f"'{raw}' is not a number of minutes"
    every = _positive_seconds(spec.get("every"))
    if not every:
        return f"'{seg_id}' has no every=<interval> to snooze against"
    # Acknowledge "in the past" so the normal interval brings it back in `minutes`.
    path = custom_state_path(bool(spec.get("shared")))
    state = load_json(path)
    state = state if isinstance(state, dict) else {}
    state[seg_id] = {"acked_at": time.time() - every + minutes}
    write_cache(path, state)
    return f"snoozed '{seg_id}' for {_fmt_duration_short(minutes)}"


def cmd_enable_click(arg: str) -> str:
    """/squirrel-click — the second of the two switches a click needs."""
    value = (arg or "").strip().lower()
    cfg = load_config()
    if value in ("on", "true", "yes"):
        save_config({"clickToAck": True})
        term = os.environ.get("TERM_PROGRAM") or "(unknown)"
        if click_capable_terminal(cfg):
            return (f"click-to-acknowledge is ON, and your terminal ({term}) can follow the "
                    "link. A listener starts only once a segment has click=ack, binds "
                    "127.0.0.1 on a random port, and exits by itself when idle. Clicking "
                    "opens a browser tab that says what happened — that is how hyperlinks "
                    "work and cannot be avoided.")
        return (f"click-to-acknowledge is ON, but your terminal ({term}) will NOT follow the "
                "link: Claude Code currently passes OSC-8 hyperlinks to IDE terminals "
                "(VS Code, Cursor) and not to standalone ones. No listener will be started "
                "here, deliberately — a socket nobody can click is pure cost. Use "
                "/squirrel-done instead, or /squirrel-click-terminals any to override.")
    if value in ("off", "false", "no"):
        save_config({"clickToAck": False})
        return "click-to-acknowledge is OFF — any running listener exits when it next idles"
    return "usage: /squirrel-click <on|off>"


def cmd_set_click_terminals(arg: str) -> str:
    """Override which TERM_PROGRAMs are treated as able to follow a status-line hyperlink."""
    value = (arg or "").strip().lower()
    if not value:
        return (f"usage: /squirrel-click-terminals <name,name|any|reset>   "
                f"(default: {', '.join(CLICK_CAPABLE_TERM_PROGRAMS)})")
    if value == "reset":
        # forget: the shipped list must show through again, which an empty list would not do.
        forget_config("clickTerminals")
        return f"click terminals reset to the shipped list: {', '.join(CLICK_CAPABLE_TERM_PROGRAMS)}"
    names = [n.strip() for n in value.replace(",", " ").split() if n.strip()]
    save_config({"clickTerminals": names})
    if "any" in names:
        return ("click links will now be emitted in EVERY terminal. If yours does not follow "
                "them, a listener will run with nothing able to reach it.")
    return "click terminals set to: " + ", ".join(names)


def cmd_set_branch_max(value: str) -> str:
    value = (value or "").strip().lower()
    if value in ("off", "0", "none"):
        save_config({"branchMaxChars": 0})
        return "branch name truncation is off (no limit)"
    try:
        n = int(value)
        if n < 0:
            raise ValueError
    except ValueError:
        return "usage: /squirrel-branch <chars|off>"
    save_config({"branchMaxChars": n})
    return f"branch name limit set to {n} chars"


def cmd_set_reset_warn(arg: str) -> str:
    parts = (arg or "").split()
    if len(parts) == 1 and parts[0].lower() in ("off", "none", "0"):
        save_config({"resetWarnMinutes": 0})
        return "session-reset countdown warnings turned off"
    if len(parts) != 2:
        return "usage: /squirrel-reset-warn <warn minutes> <urgent minutes>   (or 'off')"
    try:
        warn, urgent = int(parts[0]), int(parts[1])
    except ValueError:
        return "usage: /squirrel-reset-warn <warn minutes> <urgent minutes>   (whole minutes, or 'off')"
    if warn < 0 or urgent < 0:
        return "minutes cannot be negative"
    if urgent > warn:
        return f"the urgent threshold ({urgent}) cannot be greater than the warning threshold ({warn})"
    save_config({"resetWarnMinutes": warn, "resetUrgentMinutes": urgent})
    return (f"the session countdown turns yellow at {warn} min and red at {urgent} min before the reset")


def cmd_set_usage(value: str) -> str:
    value = (value or "").strip().lower()
    if value in ("on", "true", "yes"):
        save_config({"usageFetch": True})
        return "per-model usage fetch is ON (reads your Claude token, sends it only to api.anthropic.com)"
    if value in ("off", "false", "no"):
        save_config({"usageFetch": False})
        return "per-model usage fetch is OFF (5h/weekly still shown from Claude Code's own data)"
    return "usage: /squirrel-usage <on|off>"


def cmd_set_tunable(arg: str) -> str:
    """Backs `/squirrel-set <key> <number>`; no arguments lists every tunable with its current value."""
    parts = (arg or "").split()
    if not parts:
        cfg = load_config()
        rows = [f"  {k:<28} {tunable(cfg, k):>7}   {TUNABLES[k][3]}" for k in TUNABLES]
        return "usage: /squirrel-set <key> <number>\n" + "\n".join(rows)
    if len(parts) != 2:
        return "usage: /squirrel-set <key> <number>"
    key, raw = parts[0], parts[1]
    if key not in TUNABLES:
        return f"unknown setting '{key}' — one of: {', '.join(TUNABLES)}"
    default, lo, hi, help_text = TUNABLES[key]
    try:
        value = int(raw)
    except ValueError:
        return f"'{raw}' is not a number — usage: /squirrel-set {key} <number>"
    if not lo <= value <= hi:
        return f"{key} must be between {lo} and {hi}"
    save_config({key: value})
    return f"{key} set to {value} ({help_text})"


def settings_path() -> Path:
    return config_dir() / "settings.json"


def launcher_path() -> Path:
    """Stable path the user's statusLine setting points at; rewritten to track plugin updates.

    Windows gets a `.cmd`: cmd.exe is the one interpreter guaranteed to be present, and a
    `.cmd` is directly executable by CreateProcess, so the statusLine command needs no shell
    at all. POSIX keeps the `/bin/sh` shim.
    """
    return data_dir() / ("run.cmd" if IS_WINDOWS else "run.sh")


LAUNCHER_TEMPLATE = """#!/bin/sh
# Written by the Squirrel plugin. Points at the currently installed plugin version.
#
# Claude Code installs each plugin version into its own directory, so this path goes stale the
# moment the plugin updates. Normally refresh_launcher() has already rewritten this file — but
# that needs one invocation carrying $CLAUDE_PLUGIN_ROOT (a hook or a slash command), and a
# user who has disabled hooks and not run a command yet will arrive here with a dead path.
# Say so, in the bar, where they are already looking. Claude Code renders our STDOUT, so the
# message goes there: stderr plus a non-zero exit is a blank bar, the silence we are fixing.
if [ ! -f '{script}' ]; then
    echo "squirrel: the plugin moved (updated?) — run /squirrel-setup to repair the status line"
    exit 0
fi
exec '{python}' '{script}' "$@"
"""

# `%*` forwards the arguments. `%` is doubled by _cmd_quote so a path containing one cannot be
# taken for a variable reference; delayed expansion stays off so `!` in a path is literal too.
#
# The stale-path check uses `goto` rather than a parenthesised `if ... ( ... )` block: cmd
# splits a block on `)` before it finishes thinking about quotes, and `(` and `)` are legal in
# a Windows path. No block, no hazard.
LAUNCHER_TEMPLATE_CMD = """@echo off
rem Written by the Squirrel plugin. Points at the currently installed plugin version.
rem See LAUNCHER_TEMPLATE for why the stale-path message goes to stdout and exits 0.
setlocal EnableExtensions DisableDelayedExpansion
if exist "{script}" goto squirrel_run
echo squirrel: the plugin moved - run /squirrel-setup to repair the status line
exit /b 0
:squirrel_run
"{python}" "{script}" %*
"""


def _shell_squote(value: str) -> str:
    r"""Quote a value for a POSIX shell as a SINGLE-quoted string, `'\''` idiom and all.

    Audit X1, a HIGH that shipped: the previous version escaped `\\` and `"` and interpolated
    into a DOUBLE-quoted string — where `$(...)`, backticks and `${...}` all still expand.
    `run.sh` *is* the statusLine command, so an injected payload ran every few seconds,
    silently, for as long as the bar was installed. Demonstrated:

        --setup '/tmp/x$(touch /tmp/WB_PROOF)y'   ->  the file appeared on the next render

    Not remotely reachable (Claude Code supplies the plugin root), but a user can type it into
    `/squirrel-setup`, and "the input is usually trusted" is not a property to rest a
    render-path command on. Flagged as minor in the Task 4 review; only the quote half was
    ever fixed, and it took a second interpolation site to put it back in front of anyone.

    Nothing expands inside single quotes. The one character that needs handling is `'`
    itself: end the string, emit an escaped quote, start a new one."""
    return value.replace("'", "'\\''")


def _cmd_quote(value: str) -> str:
    """Escape a value for interpolation inside a double-quoted cmd.exe batch argument.

    Batch has no escape for `"` inside a quoted string, so a path containing one cannot be
    represented; Windows forbids `"` in paths anyway, and we drop it rather than emit a
    broken script. `%` is doubled so it is not read as a variable reference.
    """
    return value.replace('"', "").replace("%", "%%")


def resolve_interpreter() -> str:
    """Absolute path of a Python 3 to bake into the installed launcher.

    Resolved ONCE, at `/squirrel-setup` time, and written into the launcher — never guessed at
    runtime. `sys.executable` is the answer whenever it is available: the interpreter currently
    running `--setup` is by definition one that works on this machine, which no PATH lookup can
    guarantee. On Windows in particular `python3` on PATH is usually the Microsoft Store App
    Execution Alias — a zero-byte stub that prints "Python was not found" and exits 9009 — so a
    PATH fallback must skip empty files.
    """
    exe = sys.executable
    if exe and Path(exe).is_file():
        return str(Path(exe).resolve())
    for name in ("python3", "python", "py"):            # last resort: frozen/embedded hosts
        found = shutil.which(name)
        if not found:
            continue
        try:
            if Path(found).stat().st_size == 0:         # Windows Store alias stub
                continue
        except OSError:
            continue
        return str(Path(found).resolve())
    return "python3"


def atomic_replace(tmp: Path, path: Path, attempts: int = 5, delay: float = 0.01) -> None:
    """`os.replace(tmp, path)`, retrying briefly on Windows.

    POSIX rename over an open file is atomic and always succeeds. Windows raises
    PermissionError (WinError 5) while ANY other process holds the destination open — and with
    several Claude sessions re-reading the cache and state files every few seconds, that
    collision is routine rather than theoretical. Verified on native Windows: see
    CrossPlatformTests. Retries are capped at ~40ms total so the render path is never delayed;
    the final attempt is allowed to raise so callers see a genuine failure.
    """
    if not IS_WINDOWS:
        os.replace(tmp, path)
        return
    for remaining in range(attempts - 1, -1, -1):
        try:
            os.replace(tmp, path)
            return
        except PermissionError:
            if not remaining:
                raise
            time.sleep(delay)


def chmod_private(path: Path, mode: int) -> None:
    """Owner-only permissions where the OS has them; a documented no-op on Windows.

    Windows honours only the read-only bit here, and setting it would break the very
    tmp+rename this is called before. Access control on Windows is the per-user profile
    directory's own ACL, which already excludes other users — see the README security note.
    """
    if IS_WINDOWS:
        return
    os.chmod(path, mode)


def _owned_roots() -> list:
    """The directory trees Squirrel itself creates and is therefore entitled to re-permission:
    this profile's data dir and the cross-profile shared store. Deliberately NOT their
    parents — `~/.claude` and the user's home are theirs, and tightening those underneath
    them would be a surprise, not a fix."""
    roots = []
    for getter in (data_dir, shared_dir):
        try:
            roots.append(getter().resolve())
        except OSError:
            pass
    return roots


def _is_ours(path: Path) -> bool:
    try:
        resolved = path.resolve()
    except OSError:
        return False
    return any(resolved == root or root in resolved.parents for root in _owned_roots())


def mkdir_private(directory: Path) -> None:
    """Create a directory tree owner-only (0700), a documented no-op on Windows (Task 18: an
    ACL-based filesystem has no equivalent, and the only bit os.chmod can set there is
    read-only, which would break our own atomic writes).

    Two separate failures to avoid. First, `Path.mkdir(parents=True, mode=0o700)` applies the
    mode to the LAST component only — every parent it creates gets the default 0777 & ~umask,
    which is how `~/.squirrel` came to sit at 0755 while the files inside it were correctly
    0600. So each missing ancestor is created individually at 0700.

    Second, mkdir does nothing at all to a directory that already exists, so a store created
    at 0755 by an older version would stay 0755 for ever. Any existing directory that is OURS
    (see _owned_roots()) and wider than 0700 is therefore tightened. Directories that are not
    ours are never touched, whatever their mode.

    0700 has no group or other bits, so umask cannot loosen what we create."""
    missing = []
    probe = directory
    while not probe.exists():
        missing.append(probe)
        if probe.parent == probe:
            break
        probe = probe.parent
    for path in reversed(missing):
        try:
            path.mkdir(mode=0o700)
        except FileExistsError:
            pass        # raced with another session; it exists, which is all we needed
    if IS_WINDOWS:
        return
    probe = directory
    for _ in range(8):          # bounded: our trees are two deep, this is belt and braces
        if not _is_ours(probe):
            break
        try:
            if probe.is_dir() and not probe.is_symlink():
                mode = stat.S_IMODE(probe.stat().st_mode)
                if mode & 0o077:
                    os.chmod(probe, 0o700)
        except OSError:
            pass                # never let a permissions repair break the status line
        if probe.parent == probe:
            break
        probe = probe.parent


def write_launcher(plugin_root: str) -> Path:
    """(Re)write the launcher shim to run the given plugin root's status line. Idempotent."""
    path = launcher_path()
    script = str(Path(plugin_root) / "scripts" / "statusline.py")
    python = resolve_interpreter()
    if IS_WINDOWS:
        body = LAUNCHER_TEMPLATE_CMD.format(python=_cmd_quote(python), script=_cmd_quote(script))
    else:
        body = LAUNCHER_TEMPLATE.format(python=_shell_squote(python), script=_shell_squote(script))
    path.parent.mkdir(parents=True, exist_ok=True)
    if not path.exists() or path.read_text(encoding="utf-8") != body:
        tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
        tmp.write_text(body, encoding="utf-8")
        chmod_private(tmp, 0o700)
        atomic_replace(tmp, path)
    return path


def refresh_launcher() -> None:
    """Re-point the launcher at the plugin root of *this* invocation, if there is one.

    Why here and not on the render path: the render is reached THROUGH the launcher, so the
    code running there is by definition whatever the launcher already points at — it cannot
    see that a newer version exists. Only an invocation Claude Code starts from the plugin
    directory itself carries $CLAUDE_PLUGIN_ROOT, and those are the activity hooks and the
    slash commands. So the recovery lives in both of those, and the render pays nothing.

    write_launcher() is idempotent: in the steady state this is one read and no write.
    """
    root = os.environ.get("CLAUDE_PLUGIN_ROOT")
    if not root:
        return
    # Deliberately not conditional on the launcher already existing: a settings.json that
    # points at a launcher someone has deleted is exactly the broken bar this repairs.
    try:
        write_launcher(root)
    except Exception:
        pass            # a stale path is bad; breaking a hook or a command over it is worse


def cmd_setup(plugin_root: str) -> str:
    """Install the status line into the user's settings.json and write the launcher.

    Writes through an existing symlink to its real target (never replaces the link with a
    plain file), and preserves the destination's existing permission mode — defaulting to
    0600 only when the file doesn't exist yet.
    """
    launcher = write_launcher(plugin_root)
    path = settings_path()
    if path.is_symlink():
        path = path.resolve()
    if path.exists():
        try:
            raw = path.read_text(encoding="utf-8")
        except OSError as exc:
            return f"could not read {path} — {exc.strerror or exc.__class__.__name__}."
        try:
            settings = json.loads(raw)
        except ValueError:
            return (f"could not read {path} — it is not valid JSON. Fix it, then run /squirrel-setup again.")
        if not isinstance(settings, dict):
            return f"could not read {path} — expected a JSON object."
        mode = path.stat().st_mode & 0o777
    else:
        settings = {}
        mode = 0o600
    desired = {"type": "command", "command": str(launcher), "refreshInterval": 5}
    if settings.get("statusLine") == desired:
        return f"status line installed already (config: {config_path()})"
    settings["statusLine"] = desired
    tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp.write_text(json.dumps(settings, indent=2) + "\n", encoding="utf-8")
    chmod_private(tmp, mode)
    atomic_replace(tmp, path)
    return (f"status line installed → {path}\n"
            f"config: {config_path()}\n"
            f"try /squirrel-nick <name> to label this account")


def _rules_summary(cfg: dict) -> str:
    """One line for --show-config: how many rules, and how many are being ignored. A rule that
    does not parse must never be silently dropped — the user has to be able to find out."""
    rules = bar_rules(cfg)
    mine = [r for r in rules if r.get("source") != "phase"]
    broken = [r for r in rules if r.get("error")]
    text = f"{len(rules)} in effect ({len(mine)} yours, {len(rules) - len(mine)} from the idle ladder)"
    return text + (f" — {len(broken)} IGNORED, see /squirrel-rules" if broken else "")


def _tasks_summary(cfg: dict) -> str:
    """One line for --show-config. A budget the user cannot see is not a budget, and a
    watcher that has gone quiet because the ceiling tripped must be able to say so."""
    specs = task_specs(cfg)
    broken = [sp for sp in specs if sp.get("error")]
    if not specs:
        return "none configured"
    budget = nudge_budget(time.time(), cfg)
    text = f"{len(specs)} task{'' if len(specs) == 1 else 's'}"
    if broken:
        text += f" — {len(broken)} IGNORED, see /squirrel-tasks"
    text += f"; nudges {budget['used']}/{budget['ceiling']} used this hour"
    if budget.get("tripped_at") and time.time() - float(budget["tripped_at"]) < 3600:
        text += " (ceiling reached — nudges paused)"
    return text


def _click_summary(cfg: dict) -> str:
    """One line for --show-config. When a colleague reports "clicking does nothing", the
    reason should be one command away rather than a debugging session — so this states the
    terminal it detected and which of the three conditions is unmet."""
    if not click_enabled(cfg):
        return "off (/squirrel-click on)"
    term = os.environ.get("TERM_PROGRAM") or "(none)"
    if not click_capable_terminal(cfg):
        return (f"on, but TERM_PROGRAM={term} cannot follow a status-line link — no listener "
                f"is started here; use /squirrel-done, or /squirrel-click-terminals any")
    clickable = [c["id"] for c in custom_specs(cfg) if c.get("click") and c.get("ack")]
    if not clickable:
        return f"on, terminal {term} can follow links — but no segment has click= yet"
    live = read_click_listener(time.time(), cfg)
    if live:
        return f"on, terminal {term}, clickable: {', '.join(clickable)} — listening on 127.0.0.1:{live['port']}"
    # Be exact about WHEN. A listener starts on the next render in which a clickable segment
    # is actually showing — not simply the next render. With every reminder acknowledged
    # that can be hours away, and "starts on the next render" would be a confident wrong
    # statement of the kind this project keeps producing.
    ctx = _custom_ctx(cfg)
    due = [c["id"] for c in custom_specs(cfg)
           if c.get("click") and c.get("ack") and custom_due(ctx, c)]
    when = ("one starts on the next render" if due
            else "one starts when a clickable segment is next showing — all of them are "
                 "acknowledged right now")
    return f"on, terminal {term}, clickable: {', '.join(clickable)} — no listener; {when}"


def _provenance_lines() -> list:
    """Every setting the user has set, and the layer it came from.

    This is the answer to "why is this not what I set?", which with three layers is otherwise
    unanswerable — and that question is exactly how the user found the bug this layering
    exists to fix. Shipped-only keys are left out deliberately: they are not something anyone
    set, and listing fifty of them would bury the handful that are.

    A value set in the shared layer and then overridden per-profile is called out as
    SHADOWING, because that is the case where the user is looking at a setting they made and
    a status line that ignores it."""
    provenance = config_provenance()
    shared = layer_config("shared")
    rows = []
    for key in sorted(provenance):
        layers = provenance[key]
        if not set(layers) - {"shipped"}:
            continue
        where = "+".join(layers)
        if layers[-1] == "profile" and key in shared and "shared" not in layers:
            where += ", shadowing shared"
        rows.append(f"  {sanitise(key)} [{where}]")
    if not rows:
        return ["  (nothing set — every value is a shipped default)"]
    return rows


def _layer_lines() -> list:
    lines = [f"  shipped  : {default_config_path()}"]
    for layer in ("shared", "profile"):
        path = _layer_path(layer)
        count = len(layer_config(layer))
        state = f"{count} setting(s)" if count else "not used"
        lines.append(f"  {layer:9}: {path}  ({state})")
    return lines


def _usage_fetch_summary(config: dict) -> str:
    """'on · fetched 3m ago · ok', and — when it is not ok — the reason.

    `per-model ?` on the bar has exactly four causes, and until this existed the user could
    not tell them apart: the fetch is off, no token was found, the last fetch failed, or it
    simply has not run yet. The first real macOS install hit one of them and the answer was
    a shrug. Nothing here prints or logs the token itself; it reports only whether one was
    found."""
    if not config.get("usageFetch", True):
        return "off (/squirrel-usage on) — the 5h and weekly numbers still come from Claude Code"
    cache = load_json(cache_paths()[0])
    parts = ["on"]
    try:
        has_token = bool(read_token())
    except Exception:
        has_token = False
    if not has_token:
        parts.append("NO TOKEN in " + str(config_dir() / ".credentials.json"))
        if sys.platform == "darwin":
            # Measured, not guessed: Claude Code on macOS keeps its OAuth credentials in the
            # login Keychain rather than a file, so there is nothing for the fetch to read.
            parts.append("macOS keeps the token in the Keychain — the per-model line cannot "
                         "be fetched here; the 5h and weekly numbers are unaffected")
        return " · ".join(parts)
    fetched = cache.get("fetched_at")
    if not fetched:
        parts.append("has not run yet (it starts in the background on the next render)")
        return " · ".join(parts)
    parts.append(f"fetched {fmt_relative(max(0.0, time.time() - float(fetched)))} ago")
    if cache.get("ok"):
        scoped = ((cache.get("limits") or {}).get("scoped")) or []
        parts.append(f"ok, {len(scoped)} per-model bucket{'s' if len(scoped) != 1 else ''}")
    else:
        parts.append(f"FAILED: {sanitise(str(cache.get('error') or 'unknown'))[:120]}")
    return " · ".join(parts)


def cmd_show_config() -> str:
    cfg = load_config()
    email = _current_email() or "(not logged in)"
    names = cfg.get("accountNames") or {}
    colors = cfg.get("accountColors") or {}
    alarm = tunable(cfg, "alarmAfterSeconds")
    phases = load_phases(cfg)
    phase_summary = ", ".join(f"{p['after']}s→{p.get('bg') or 'off'}" for p in phases) if phases else "none"
    hidden = [k for k in SEGMENT_KEYS if not segment_enabled(cfg, k)]
    non_default = [f"{k}={tunable(cfg, k)}" for k in TUNABLES if tunable(cfg, k) != TUNABLES[k][0]]
    lines = [
        "config      : shipped defaults → shared → this profile (the last one set wins)",
        *_layer_lines(),
        "set by you  :",
        *_provenance_lines(),
        "",
        f"account     : {email}",
        f"nickname    : {sanitise(str(names.get(email, '(none — /squirrel-nick <name>)')))}",
        f"colour      : {sanitise(str(colors.get(email, cfg.get('defaultAccountColor', 'white'))))}",
        f"idle alarm  : {'off' if not alarm else str(alarm) + 's'}",
        f"idle phases : {phase_summary}",
        f"usage fetch : {_usage_fetch_summary(cfg)}",
        f"hidden      : {', '.join(hidden) if hidden else 'all shown'}",
        f"layout      : {' | '.join(' '.join(line) or '(empty)' for line in _layout_segment_ids(cfg))}",
        f"bar rules   : {_rules_summary(cfg)}",
        f"click-to-ack: {_click_summary(cfg)}",
        f"tasks       : {_tasks_summary(cfg)}",
        f"branch max  : {branch_limit(cfg)} chars",
        f"tunables    : {', '.join(non_default) if non_default else 'all defaults'}",
    ]
    return "\n".join(lines)


def cmd_config_share() -> str:
    """Promote this profile's settings to the shared layer, so the other profiles get them.

    Nothing moves without being asked, and this is the asking. It COPIES rather than moves:
    the per-profile file is left exactly as it was, so the profile you run it in cannot change
    behaviour at all. That is what makes it safe to run — and safe not to. The user has one
    populated profile and two bare ones; the failure mode to avoid is a promotion that
    reshuffles the profile that was already working.

    Maps merge per key into whatever the shared layer already has, matching how they resolve.
    Profile-local keys are never promoted. Every key is reported with what happened to it,
    because a config edit that silently lands somewhere unexpected is its own bug — and this
    one touches every setting at once."""
    mine = stored_config("profile")
    if not mine:
        return ("this profile has no config of its own — there is nothing to share.\n"
                "settings you make from here will go to the shared layer anyway.")
    shared = stored_config("shared")
    per_key = merge_keys()
    promoted, merged, already, skipped = [], [], [], []
    patch: dict = {}
    for key in sorted(mine):
        value = mine[key]
        if key in PROFILE_ONLY_KEYS:
            skipped.append(key)
        elif key not in shared:
            promoted.append(key)
            patch[key] = value
        elif shared[key] == value:
            already.append(key)
        elif key in per_key and isinstance(value, dict) and isinstance(shared.get(key), dict):
            merged.append(key)
            patch[key] = value
        else:
            # A differing scalar or list. The shared layer already carries a deliberate
            # value; overwriting it from one profile would be this command reaching into the
            # other profiles' settings, which is not what "share mine" asks for.
            already.append(key)
    if patch:
        save_config(patch, layer="shared")
    lines = ["shared what this profile has set — your own config is unchanged:"]
    for label, keys in (("promoted", promoted), ("merged into shared", merged),
                        ("already in shared, left alone", already),
                        ("profile-local, never shared", skipped)):
        if keys:
            lines.append(f"  {label}: {', '.join(sanitise(k) for k in keys)}")
    lines.append("")
    lines.append("your per-profile values still win HERE — that is deliberate, and /squirrel-config")
    lines.append("shows which layer each one comes from. the other profiles now use the shared ones.")
    return "\n".join(lines)


def cmd_config(arg: str = "") -> str:
    verb = (arg or "").strip().lower()
    if verb in ("", "show", "list"):
        return cmd_show_config()
    if verb == "share":
        return cmd_config_share()
    return "usage: /squirrel-config [show | share]"


# Argument-hint text and one-line description for --help-text, keyed by bare flag. The *set*
# of flags cmd_help() lists always comes from CONFIG_COMMANDS and NON_CONFIG_COMMANDS below —
# the same structures main() actually dispatches against — never from a separate hand-written
# list, so a subcommand can't go missing from --help-text (or a retired one linger in it). This
# dict only supplies presentation text for flags that structure already names.
COMMAND_HELP: dict[str, tuple[str, str]] = {
    "--config": ("[show | share]", "show the config and which layer each value comes from; "
                 "`share` promotes this profile's settings to the shared layer"),
    "--set-nick": ("<name>", "label the account you are logged in as"),
    "--set-color": ("<colour>", "set that account's status-line colour"),
    "--set-alarm": ("<seconds|off>", "delay before the loud waiting-alarm chip"),
    "--set-usage": ("<on|off>", "toggle usageFetch — the per-model weekly usage fetch"),
    "--show-segment": ("<segment> <on|off>", "show or hide one line segment"),
    "--set-branch-max": ("<n>", "truncate the git branch name to n chars (0 = no limit)"),
    "--set-reset-warn": ("<warn minutes> <urgent minutes>|off", "when the session-reset countdown turns yellow/red"),
    "--set-bg": ("[urgent|lines] <colour|off|auto|all|1,2>", "full-width tint colour shown while a session waits "
                 "(prefix with `urgent` for the alarm-phase bar colour)"),
    "--set-bg-after": ("<seconds>", "seconds of waiting before that tint appears"),
    "--alerts": ("[on|off|focused <on|off>|test]", "desktop notifications for a waiting session — "
                 "the half of the alarm that reaches a monitor you are not looking at"),
    "--phases": ("[add|set|remove|reset] <seconds> [bg=<colour|off>] [text=<colour|off>] [bold=<on|off>]",
                 "the idle phase ladder, in one subcommand (what /squirrel-phases runs)"),
    "--show-phases": ("", "list the idle phase ladder (idlePhases) currently in effect"),
    "--add-phase": ("<seconds> bg=<colour|off> [text=<colour|off>] [bold=<on|off>]", "add a phase to the idle ladder"),
    "--set-phase": ("<seconds> [bg=<colour|off>] [text=<colour|off>] [bold=<on|off>]",
                     "surgically overlay one field of one phase, leaving the rest untouched"),
    "--remove-phase": ("<seconds>", "drop one phase from the ladder"),
    "--reset-phases": ("", "restore the shipped idlePhases ladder, discarding overrides"),
    "--set": ("<key> <number>", "any TUNABLES threshold below"),
    "--set-repo-color": ("<colour|off>", "colour used for the current repository"),
    "--set-default-color": ("<colour>", "fallback colour for accounts with no colour of their own"),
    "--set-states": ("<alarm|background> <states>", "which activity states trigger the alarm or the idle tint"),
    "--set-label": ("<segment> <text|reset>", "override a segment's display label "
                    f"(currently: {', '.join(LABELABLE_SEGMENTS)})"),
    "--set-share-format": (f"<{'|'.join(SHARE_FORMATS)}>", "how the 'share' segment renders — "
                           "raw share, this session's contribution to the account limit, or both"),
    "--set-session-contribution": ("<on|off>", "whether the 5h segment folds in this "
                                   "session's contribution as '<contribution>/<total>%'"),
    "--set-attribution-metric": (f"<{'|'.join(ATTRIBUTION_METRICS)}>", "which delta drives "
                                 "cross-session attribution — dollar cost (default, includes "
                                 "subagent work) or tokens"),
    "--set-sessions-show-model": ("<on|off>", "whether --show-sessions includes the per-model "
                                  "mix column"),
    "--layout": ("[show | move <segment> <line> [position] | line <n> <segment>... | "
                 "swap <line> <line> | priority <segment> <n|always> | reset]",
                 "the whole layout in one subcommand (what /squirrel-layout runs)"),
    "--show-layout": ("", "print the layout line by line with each segment's drop priority"),
    "--set-priority": ("<segment|detail> <n|always>", "how hard a segment (or one of the "
                       f"shed-able details {', '.join(DETAIL_PRIORITIES)}) clings to a narrow "
                       "pane — higher survives longer, 'always' is never dropped"),
    "--style": ("[show | set <segment> <field>=<value> ... | clear <segment> | reset]",
                "per-segment colours (what /squirrel-style runs): fg, bg, bold, opacity, "
                "sep, force"),
    "--show-styles": ("", "print every segment style in effect and the fields available"),
    "--rules": ("[show | add <condition> bg=.. text=.. | set <condition> <field>=<value> ... | "
                "remove <condition> | reset]",
                "paint the whole bar when a condition holds (what /squirrel-rules runs)"),
    "--show-rules": ("", "print every bar rule in effect, and any that do not parse"),
    "--show-metrics": ("", "print every metric, its live value and meaning, plus the task vocabulary"),
    "--task": ("<add|set|remove|test> <id> [field=value ...]", "create, edit, remove or dry-run one watcher task"),
    "--tasks": ("", "print every task, its queue state, and the nudge budget"),
    "--task-actions": ("<verb> <on|off>", "opt one task action in or out (all off by default)"),
    "--custom": ("[show | add <id> <field>=<value> ... | set <id> ... | remove <id> | reset]",
                 "custom segments (what /squirrel-custom runs) — text, commands and reminders"),
    "--show-custom": ("", "list every custom segment and whether it is showing right now"),
    "--custom-commands": ("<on|off>", "allow custom segments to RUN their `command` "
                          "(off by default)"),
    "--ack": ("<id|all>", "acknowledge a due reminder (what /squirrel-done runs)"),
    "--snooze": ("<id> <minutes>", "push a due reminder out without clearing its full interval"),
    "--click": ("<on|off>", "allow a reminder to be dismissed by clicking it (off by "
                "default; starts a loopback listener only when one is actually needed)"),
    "--click-terminals": ("<name,name|any|reset>", "which TERM_PROGRAMs are treated as able "
                          "to follow a status-line hyperlink"),
    "--show-config": ("", "print the current configuration"),
    "--setup": ("<plugin root>", "install the status line into settings.json"),
    "--show-sessions": ("[session id] [wide]", "table of every live session on this account, "
                        "ranked by its share of the 5h window; sheds columns to fit a narrow "
                        "terminal, `wide` prints them all"),
}

# Subcommands main() dispatches outside the CONFIG_COMMANDS table (setup/introspection, not
# config-setters). Kept as one small tuple — not a second flag list — so cmd_help() still
# enumerates every dispatched subcommand from source structures alone.
NON_CONFIG_COMMANDS = ("--show-config", "--setup", "--show-sessions")


def cmd_help() -> str:
    """Compact reference for every subcommand, segment key, and tunable — what `--help-text`
    and the `squirrel` skill both point Claude at. The subcommand list is built by walking
    CONFIG_COMMANDS and NON_CONFIG_COMMANDS (the structures main() actually dispatches
    against), and the segment/tunable lists are pulled live from SEGMENT_KEYS/TUNABLES, so none
    of the three can drift out of date with the code."""
    commands = [(f"{flag} {COMMAND_HELP[flag][0]}".rstrip(), COMMAND_HELP[flag][1])
                for flag in (*CONFIG_COMMANDS, *NON_CONFIG_COMMANDS)]
    lines = ["squirrel status line — subcommands:"]
    lines += [f"  {cmd:<34} {desc}" for cmd, desc in commands]
    lines.append("")
    lines.append("segments (--show-segment <segment> on|off): " + ", ".join(SEGMENT_KEYS))
    lines.append(f"  protected — cannot be hidden: {', '.join(PROTECTED_SEGMENTS)}")
    lines.append("")
    lines.append("tunables (--set <key> <number>):")
    lines += [f"  {k:<28} default {default:<6} range {lo}-{hi}   {help_text}"
              for k, (default, lo, hi, help_text) in TUNABLES.items()]
    lines.append("")
    lines.append(f"config file: {config_path()}")
    return "\n".join(lines)


# ── account ──────────────────────────────────────────────────────────────────
def account_label(email: str | None, config: dict | None = None) -> str:
    """Configured nickname (accountNames: full email → nickname, then domain → nickname) if set,
    else the full email address; '?' when unknown."""
    if not email or "@" not in email:
        return "?"
    names = (config or {}).get("accountNames")
    names = names if isinstance(names, dict) else {}
    return names.get(email) or names.get(account_domain(email)) or email


def account_domain(email: str | None) -> str:
    """'you@example.com' → 'example'; '?' when unknown. Used only for colour fallback."""
    if not email or "@" not in email:
        return "?"
    return email.split("@", 1)[1].split(".", 1)[0] or "?"


def color_code(value) -> str | None:
    """'#rrggbb' or an ANSI name (red, brightcyan, gray, …) → escape sequence; None if invalid."""
    if not isinstance(value, str):
        return None
    v = value.strip().lower()
    if v.startswith("#") and len(v) == 7:
        try:
            r, g, b = (int(v[i:i + 2], 16) for i in (1, 3, 5))
        except ValueError:
            return None
        return f"\033[38;2;{r};{g};{b}m"
    bright = v.startswith("bright")
    code = ANSI_NAMES.get(v[6:] if bright else v)
    if code is None:
        return None
    if bright and code < 90:
        code += 60
    return f"\033[{code}m"


def bold_code(escape: str | None) -> str | None:
    """Insert a bold ('1;') SGR parameter into a plain colour escape from color_code(), e.g.
    '\\033[37m' -> '\\033[1;37m', '\\033[38;2;r;g;bm' -> '\\033[1;38;2;r;g;bm'; None (or
    anything not already an escape) passes through unchanged."""
    if not escape or not escape.startswith("\033["):
        return escape
    return f"\033[1;{escape[2:]}"


def resolve_color(email: str | None, config: dict) -> str:
    """Lookup order: full email → domain → defaultAccountColor → white."""
    colors = config.get("accountColors")
    colors = colors if isinstance(colors, dict) else {}
    for key in (email, account_domain(email) if email else None):
        seq = color_code(colors.get(key)) if key else None
        if seq:
            return seq
    return color_code(config.get("defaultAccountColor")) or color_code("white")


# ── repo ────────────────────────────────────────────────────────────────────
def repo_name(status: dict) -> str | None:
    """Repository/project name from the status JSON: workspace.repo.name, else a dir basename;
    None if unknown. Sanitised here at INGEST as well as at the render seam, so every later
    decision — width accounting, colour hashing — is made on the characters that will actually
    be shown rather than on something the terminal would eat."""
    ws = status.get("workspace") or {}
    repo = ws.get("repo") or {}
    name = sanitise(repo.get("name"))
    if name:
        return name
    for key in ("git_worktree", "current_dir", "project_dir"):
        path = ws.get(key)
        if path:
            # Sanitise BEFORE taking the basename, not after: an escape sequence can contain
            # a '/', so a hostile path would otherwise be split on the attacker's slash and
            # the wrong component chosen — "/x/demo<OSC-8 http://evil.com>" yields "evil.com"
            # if you sanitise second. Order is the whole bug.
            return sanitise(Path(sanitise(path)).name) or None
    return None


def hash_color(name: str) -> str:
    """Deterministic truecolor for a name: same name → same colour, spread across the hue circle,
    fixed saturation/value so every result is legible on a dark background."""
    digest = int(hashlib.md5(name.encode("utf-8")).hexdigest(), 16)
    hue = (digest % 360) / 360.0
    r, g, b = colorsys.hsv_to_rgb(hue, 0.55, 0.95)
    return f"\033[38;2;{int(r * 255)};{int(g * 255)};{int(b * 255)}m"


def repo_color(name: str, config: dict) -> str:
    """`repoColors` override for this repo if configured, else the hashed colour."""
    overrides = config.get("repoColors")
    overrides = overrides if isinstance(overrides, dict) else {}
    return color_code(overrides.get(name)) or hash_color(name)


def current_repo_name() -> str | None:
    """Repository name for the working directory: the git toplevel's basename, else the cwd's."""
    try:
        cwd = os.getcwd()
    except OSError:
        return None
    top = _git(cwd, ["rev-parse", "--show-toplevel"], tunable(load_config(), "gitTimeoutMs"))
    return Path(top).name if top else (Path(cwd).name or None)


def cmd_set_repo_color(arg: str) -> str:
    value = (arg or "").strip()
    if not value:
        return "usage: /squirrel-repo-color <colour|off>"
    repo = current_repo_name()
    if not repo:
        return "no repository here — run this inside a project directory"
    if value.lower() in ("off", "auto", "none"):
        layer, stored = edit_layer("repoColors")
        colors = dict(stored.get("repoColors") or {})
        colors.pop(repo, None)
        # replace: save_config() would merge the entry we just removed back in.
        replace_config({"repoColors": colors}, layer=layer)
        return f"'{repo}' is back to its automatic colour"
    if not color_code(value):
        return f"'{value}' is not a valid colour — use a name (red green yellow blue magenta cyan white gray, bright*) or #rrggbb"
    save_config({"repoColors": {repo: value}})
    return f"colour for '{repo}' set to {value}"


# ── git ──────────────────────────────────────────────────────────────────────
def _git(cwd: str, args: list[str], timeout_ms: int | None = None) -> str | None:
    """Run a quick git command in `cwd`; stdout on success, None on any failure (incl. timeout).

    Imports `subprocess` lazily — this is the only place in the render path that would otherwise
    pay for it, and when the git segment is disabled or `cwd` is never a repo, nothing here ever
    runs."""
    import subprocess
    timeout_ms = TUNABLES["gitTimeoutMs"][0] if timeout_ms is None else timeout_ms
    try:
        r = subprocess.run(["git", "-C", cwd, *args],
                           capture_output=True, text=True, timeout=timeout_ms / 1000)
    except (OSError, subprocess.SubprocessError):
        return None
    return r.stdout.strip() if r.returncode == 0 else None


_BRANCH_AB_RE = re.compile(r"\+(\d+)\s+-(\d+)")


def _parse_git_status_v2(output: str) -> dict:
    """Parse `git status --porcelain=v2 --branch` into {"branch": str|None, "dirty": bool,
    "ahead": int} — one fork instead of the three this used to take (rev-parse --abbrev-ref HEAD,
    status --porcelain, rev-list --count @{u}..HEAD).

    `branch` reproduces exactly what `git rev-parse --abbrev-ref HEAD` used to return, so the
    rendered branch name is unchanged: None when there is no branch.oid yet — an unborn branch,
    where the old rev-parse call itself used to fail — and the literal string 'HEAD' for a
    detached HEAD (`branch.head` reads '(detached)' in v2 output, but plain rev-parse there
    returns 'HEAD'). `dirty` is True when any non-header line is present (changed/renamed/
    unmerged/untracked entries all sort after the '#' header block, exactly what a non-empty
    `git status --porcelain` used to signal). `ahead` comes from `branch.ab +N -M` (present only
    with an upstream) and is 0 without one, matching `rev-list --count @{u}..HEAD` failing there.
    """
    head, unborn, ahead, dirty = None, False, 0, False
    for line in output.splitlines():
        if line.startswith("# branch.oid "):
            if line[len("# branch.oid "):].strip() == "(initial)":
                unborn = True
        elif line.startswith("# branch.head "):
            # Sanitised at INGEST, not just at the render seam: a branch name is attacker-
            # influenceable (anyone who can push a branch), and everything downstream —
            # branchMaxChars truncation above all — must count the characters that will
            # actually be shown. Truncating first and sanitising later would also mean the
            # cut could land inside an escape sequence, leaving the dangling-fragment rules
            # to save us by luck rather than by design.
            head = sanitise(line[len("# branch.head "):].strip())
        elif line.startswith("# branch.ab "):
            m = _BRANCH_AB_RE.match(line[len("# branch.ab "):].strip())
            if m:
                ahead = int(m.group(1))
        elif not line.startswith("#"):
            dirty = True
    if unborn or not head:
        return {"branch": None, "dirty": dirty, "ahead": ahead}
    return {"branch": "HEAD" if head == "(detached)" else head, "dirty": dirty, "ahead": ahead}


def git_cache_path() -> Path:
    return cache_dir() / "git.json"


def _git_status(cwd: str, config: dict, now: float) -> dict | None:
    """Parsed git status for `cwd` (see _parse_git_status_v2) — None when `cwd` isn't a repo (or
    the call failed/timed out). Cached per working directory for `gitCacheSeconds` so repeated
    renders inside one refresh window (Squirrel's own, and the fact that several sessions may
    share one profile) don't each re-fork git; a cache past its TTL is never served — the TTL is
    checked on every read, so a stale entry can't outlive it. `gitCacheSeconds <= 0` disables the
    cache outright (always re-forks, never reads or writes the cache file). Best-effort: a
    missing/unreadable/unwritable cache file just means calling git every time, never a crash."""
    ttl = tunable(config, "gitCacheSeconds")
    path = git_cache_path()
    cache = load_json(path) if ttl > 0 else {}
    entry = cache.get(cwd) if isinstance(cache, dict) else None
    if ttl > 0 and isinstance(entry, dict) and (now - float(entry.get("ts") or 0)) < ttl:
        return entry.get("result")
    timeout_ms = tunable(config, "gitTimeoutMs")
    raw = _git(cwd, ["status", "--porcelain=v2", "--branch"], timeout_ms)
    result = _parse_git_status_v2(raw) if raw is not None else None
    if ttl > 0:
        cache = cache if isinstance(cache, dict) else {}
        cache[cwd] = {"ts": now, "result": result}
        try:
            write_cache(path, cache)
        except OSError:
            pass   # caching is a best-effort speedup — never let it break the git segment
    return result


def git_segment_runs(status: dict, config: dict | None = None, now: float | None = None) -> list:
    """'⎇ branch* ↑3' — branch (truncated per branchMaxChars), dirty marker, unpushed count — for
    the working dir; None when not a git repo. When `branch` is switched off, the branch name is
    dropped but the dirty/ahead markers still show (bare '⎇ * ↑2'); if there is nothing to report
    (clean, not ahead) the whole segment is omitted rather than rendering a bare '⎇'."""

    ws = status.get("workspace") or {}
    cwd = ws.get("current_dir") or ws.get("git_worktree") or ws.get("project_dir")
    if not cwd:
        return []
    config = config or {}
    now = time.time() if now is None else now
    info = _git_status(cwd, config, now)
    branch = info.get("branch") if info else None
    if not branch:
        return []
    dirty = bool(info.get("dirty"))
    ahead = int(info.get("ahead") or 0)
    if not segment_enabled(config, "ahead"):
        ahead = 0
    if not segment_enabled(config, "branch"):
        if not dirty and ahead <= 0:
            return []
        text = "\u2387" + (" *" if dirty else "") + (f" \u2191{ahead}" if ahead > 0 else "")
        return [(text, YELLOW if dirty else GRAY)]
    limit = branch_limit(config)
    if limit <= 0 or len(branch) <= limit:
        shown = branch
    elif limit <= 1:
        shown = branch[:limit]
    else:
        shown = branch[: limit - 1] + "\u2026"
    text = f"\u2387 {shown}" + ("*" if dirty else "") + (f" \u2191{ahead}" if ahead > 0 else "")
    return [(text, YELLOW if dirty else GRAY)]


def git_segment(status: dict, config: dict | None = None, now: float | None = None) -> str | None:
    """String form of git_segment_runs(); None (not "") when there is nothing to report."""
    return join_runs(git_segment_runs(status, config, now)) or None


# ── usage: normalize & cache ─────────────────────────────────────────────────
def parse_iso(value) -> float | None:
    """ISO-8601 string → epoch seconds; None when missing or invalid."""
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


def _bucket(pct, resets_at, label: str = "") -> dict:
    return {"label": label, "pct": float(pct or 0), "resets_at": resets_at}


def normalize_usage(api: dict) -> dict:
    """API payload → {"session": bucket|None, "weekly_all": bucket|None, "scoped": [bucket, …]}.

    Prefers the `limits[]` array (kind = session / weekly_all / weekly_scoped; scoped entries are
    labelled by scope.model.display_name, so new per-model buckets show up automatically).
    Falls back to the legacy top-level five_hour / seven_day / seven_day_<model> objects.
    """
    out = {"session": None, "weekly_all": None, "scoped": []}
    limits = api.get("limits")
    if isinstance(limits, list):
        for lim in limits:
            if not isinstance(lim, dict):
                continue
            b = _bucket(lim.get("percent"), parse_iso(lim.get("resets_at")))
            kind = lim.get("kind")
            if kind == "session":
                out["session"] = b
            elif kind == "weekly_all":
                out["weekly_all"] = b
            elif kind == "weekly_scoped":
                scope = lim.get("scope") or {}
                model = scope.get("model") or {}
                b["label"] = model.get("display_name") or model.get("id") or scope.get("surface") or "scoped"
                out["scoped"].append(b)
        return out
    for key, raw in api.items():
        if not isinstance(raw, dict) or "utilization" not in raw:
            continue
        b = _bucket(raw.get("utilization"), parse_iso(raw.get("resets_at")))
        if key == "five_hour":
            out["session"] = b
        elif key == "seven_day":
            out["weekly_all"] = b
        elif key.startswith("seven_day_"):
            b["label"] = key[len("seven_day_"):].capitalize()
            out["scoped"].append(b)
    return out


def cache_paths() -> tuple[Path, Path]:
    d = cache_dir()
    return d / "usage.json", d / "fetch.lock"


def user_set_keys() -> set:
    """Keys the USER actually wrote, as opposed to keys present because we merged a default.

    load_config() returns the shipped defaults merged with the user's files, so `key in config`
    is true of everything and cannot answer "did they choose this?". The first version of
    usage_fetch_ttl() asked exactly that question of the merged config, so its fallback was
    dead code and the setting it existed to honour was still being ignored — verified against
    the user's real profile, not inferred.

    Expressed in terms of provenance rather than by reading a file, so it answers the question
    across ALL the layers a user can write to. Reading config_path() alone would have said "no"
    about a value the user had deliberately set in the shared layer — the same class of wrong
    answer, one layer along."""
    return {key for key, layers in config_provenance().items() if set(layers) - {"shipped"}}


def usage_fetch_ttl(config: dict | None) -> int:
    """How often the per-model buckets may be fetched.

    `usageFetchSeconds` when the user has set it. Otherwise an explicitly set
    `cacheTtlSeconds` still governs: it WAS this cadence until the two were separated, and
    someone who deliberately set it would otherwise find their choice quietly stopped
    mattering, with nothing to tell them. Only when neither is set does the shipped default
    apply. The presence test reads the user's own file — see user_set_keys()."""
    chosen = user_set_keys()
    if "usageFetchSeconds" not in chosen and "cacheTtlSeconds" in chosen:
        return tunable(config, "cacheTtlSeconds")
    return tunable(config, "usageFetchSeconds")


USAGE_BACKOFF_BASE_S = 120
USAGE_BACKOFF_MAX_S = 3600


def backoff_until(cache: dict) -> float:
    """When a refused endpoint may be asked again.

    All three of the user's profiles were sitting on `http 429`, retrying every 60 seconds
    for ever, with nothing in the code mentioning 429 at all. `Retry-After` wins when the
    server sent one — it is the server telling us the answer — otherwise exponential from
    the last attempt with a ceiling. Recorded in the CACHE, so every session in the profile
    backs off together instead of each discovering the refusal for itself."""
    explicit = cache.get("retry_after")
    if isinstance(explicit, (int, float)) and explicit > 0:
        return float(explicit)
    tries = max(1, int(cache.get("refusals") or 1))
    wait = min(USAGE_BACKOFF_MAX_S, USAGE_BACKOFF_BASE_S * (2 ** (tries - 1)))
    return float(cache.get("fetched_at") or 0) + wait


def needs_fetch(cache: dict, now: float, lock: Path, config: dict | None = None,
                 email: str | None = None) -> bool:
    """True when the cache is older than `cacheTtlSeconds` and no fetch is already in flight.

    Backs off to NO_TOKEN_RETRY_S when the last attempt failed for lack of a token, so a
    logged-out profile doesn't spawn a python child every `cacheTtlSeconds` forever.
    """
    if int(cache.get("refusals") or 0) > 0 and now < backoff_until(cache):
        return False        # refused, and the endpoint is not ours to hammer
    ttl = (NO_TOKEN_RETRY_S if cache.get("error") == "no token"
           else usage_fetch_ttl(config))
    # An account change is known the moment it happens, so waiting out the TTL would leave an
    # honest blank on screen for up to a minute for no reason. Not a mismatch when the current
    # account is unknowable — that is not evidence of a change.
    fresh_enough = now - float(cache.get("fetched_at") or 0) < ttl
    if fresh_enough and cache_is_this_account(cache, email) is not False:
        return False
    try:
        if now - lock.stat().st_mtime < LOCK_MAX_AGE_S:
            return False
    except OSError:
        pass
    return True


def touch_private(path: Path) -> None:
    """Create or refresh a lock file, owner-only.

    `Path.touch()` defaults to 0o666 & ~umask, i.e. 0644 on a normal box — so every lock
    this plugin wrote sat world-readable next to the 0600 files it guards. They are empty,
    so nothing leaked, but it contradicted the permissions discipline the README states
    twice, and it was three separate call sites making the same mistake. One helper now, so
    the next lock cannot get it wrong."""
    mkdir_private(path.parent)
    if path.is_symlink():
        # Never follow one: a symlink planted at a lock path would have this process
        # create-or-touch whatever it pointed at, with our permissions.
        return
    path.touch()
    chmod_private(path, 0o600)


def write_cache(path: Path, data: dict) -> None:
    """Atomic write (tmp + rename) with owner-only permissions, into an owner-only directory."""
    mkdir_private(path.parent)
    tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    # Created 0600 rather than written-then-chmod'd: the old order left a readable window
    # between the two calls, which for the token file is the whole secret.
    payload = json.dumps(data).encode("utf-8")
    if IS_WINDOWS:
        tmp.write_bytes(payload)
    else:
        handle = os.open(tmp, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        try:
            os.write(handle, payload)
        finally:
            os.close(handle)
    chmod_private(tmp, 0o600)
    atomic_replace(tmp, path)


# Win32 process-creation flags, spelled out rather than read off `subprocess` with a `getattr`
# default: that default would be 0, which silently means "do not detach" — the exact bug this is
# meant to prevent, and invisible until a console window flashes on a user's screen. They are
# fixed Win32 API constants (processthreadsapi.h) and cannot drift.
DETACHED_PROCESS = 0x00000008
CREATE_NO_WINDOW = 0x08000000


def detach_kwargs(subprocess_mod=None) -> dict:
    """Popen keyword arguments that fully detach a child, per platform.

    POSIX: `start_new_session=True` (setsid), so the child outlives this render and is not
    killed by a signal sent to the foreground process group. That argument exists on Windows
    too but is a no-op there, and the child would inherit the console.

    Windows: DETACHED_PROCESS severs the console, and CREATE_NO_WINDOW suppresses the new one
    that would otherwise be allocated. Both matter: without them a console window flashes on
    screen every time the usage cache expires — several times an hour, on top of a status bar
    that redraws every 5 seconds.

    `subprocess_mod` is accepted and ignored; it keeps the call site self-documenting about
    which module the flags belong to without making the values depend on the running platform's
    build of it.
    """
    if IS_WINDOWS:
        return {"creationflags": DETACHED_PROCESS | CREATE_NO_WINDOW}
    return {"start_new_session": True}


def spawn_fetch(lock: Path) -> None:
    """Touch the lock and start `statusline.py --fetch` detached; never waits for it."""
    import subprocess
    touch_private(lock)
    subprocess.Popen(
        [sys.executable, os.path.abspath(__file__), "--fetch"],
        stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        close_fds=True, **detach_kwargs(subprocess),
    )


# ── activity state ────────────────────────────────────────────────────────────
def state_paths(session_id) -> Path | None:
    """Per-session activity-state file under the per-profile data dir, or None for an id
    that has no business being a filename.

    Audit #7 residual: the first pass at that finding validated `session_path()` and left
    this one alone, which is worse than validating neither — a guard that covers one of two
    path builders reads as if it covers both. Demonstrated end to end through the real
    `--state` hook, writing outside the data dir. `None` rather than an exception because
    every caller here is on the hook path, which must stay silent."""
    sid = safe_session_id(session_id)
    return data_dir() / f"state-{sid}.json" if sid else None


def read_state(session_id: str | None, now: float | None = None) -> dict | None:
    """Current activity state for a session, or None when unknown/stale."""
    if not session_id:
        return None
    now = time.time() if now is None else now
    path = state_paths(session_id)
    data = load_json(path) if path else {}
    if not data or (now - float(data.get("ts") or 0) > STATE_MAX_AGE_S):
        return None
    return data


def apply_state(event: str, session_id: str, now: float) -> dict:
    """Advance the state machine for one hook event; write (or delete) the state file; return the new state."""
    path = state_paths(session_id)
    if path is None:
        # Nothing is written and nothing is raised: the caller is a hook, and the state
        # machine's answer for an id we refuse to name a file after is simply "no state".
        return {"state": event, "subagents": 0, "ts": now}
    cur = load_json(path)
    subagents = int(cur.get("subagents") or 0)
    if event == "end":
        try:
            path.unlink()
        except OSError:
            pass
        return {"state": "end", "subagents": 0, "ts": now}
    if event == "working":
        state, subagents = "working", 0
    elif event == "subagent-start":
        subagents += 1
        state = "subagents"
    elif event == "subagent-stop":
        subagents = max(0, subagents - 1)
        state = "subagents" if subagents else "working"
    elif event == "activity":
        state = "subagents" if subagents else "working"
    elif event == "permission":
        state = "permission"
    elif event == "idle":
        state, subagents = "idle", 0
    else:
        state = event
    data = {"state": state, "subagents": subagents, "ts": now}
    write_cache(path, data)   # atomic, 0600
    return data


# ── cross-session usage attribution ───────────────────────────────────────────
# A cross-profile store (immune to CLAUDE_CONFIG_DIR/HOME overrides) so every session on the
# account — regardless of which profile/alias launched it — can see the others' usage and work
# out which of them is consuming the shared 5-hour limit. Reused later by other cross-session
# features (e.g. reminders). Contains only session ids, repo names, timestamps, token counts
# and costs — never prompts, file paths, or other content.
def _real_home() -> Path:
    """The OS user's real home, immune to a HOME/CLAUDE_CONFIG_DIR override.

    POSIX: the passwd database, which the user's `claude-<profile>` aliases cannot override the
    way they override $HOME — that is the whole point of this store, since a water reminder or a
    session sample is not per-profile.

    Windows has no `pwd` module (verified: the import raises and this fallback is the branch
    actually taken, giving C:\\Users\\<user>\\.squirrel), so it goes through the documented
    profile variables. `Path.home()` is last and is GUARDED: on Windows with USERPROFILE,
    HOMEDRIVE and HOMEPATH all unset it raises RuntimeError rather than returning anything, and
    this function is on the render path via record_session_sample() — an exception there costs
    the whole attribution feature. A temp directory is a poor store but a working one.
    """
    try:
        import pwd
        return Path(pwd.getpwuid(os.getuid()).pw_dir)
    except Exception:                       # non-POSIX, or no passwd entry for this uid
        pass
    explicit = (os.environ.get("USERPROFILE")
                or (os.environ.get("HOMEDRIVE", "") + os.environ.get("HOMEPATH", "")))
    if explicit:
        return Path(explicit)
    try:
        return Path.home()
    except (RuntimeError, OSError):
        import tempfile
        return Path(tempfile.gettempdir())


def shared_dir() -> Path:
    """Cross-profile state: the same directory for every Claude config dir and every alias."""
    return Path(os.environ.get("SQUIRREL_SHARED_DIR") or _real_home() / ".squirrel")


def account_hash(email: str | None) -> str:
    """Short stable id for an account, so sessions of different accounts (separate 5-hour
    limits) are never pooled together; 'anon' when no email is known."""
    if not email:
        return "anon"
    return hashlib.sha256(email.encode("utf-8")).hexdigest()[:16]


_SAFE_SESSION_ID = re.compile(r"^[A-Za-z0-9._-]{1,128}$")


def safe_session_id(session_id) -> str | None:
    """A session id fit to be a filename, or None.

    Audit #7: this value goes straight into a path in the shared store, and traversal was
    demonstrated. No attacker-controlled route to it is known — Claude Code generates it —
    but "no known route" is not a property, and validating costs nothing. Validated rather
    than hashed so that the stores already on disk keep working; hashing would orphan every
    existing record."""
    if not isinstance(session_id, str) or not _SAFE_SESSION_ID.match(session_id):
        return None
    if session_id in (".", "..") or session_id.startswith("."):
        return None
    return session_id


def session_path(session_id: str, email: str | None = None) -> Path:
    """One flat file per session: `<shared>/sessions/<session_id>.json`.

    Task 12: previously this lived under a per-account subdirectory
    (`sessions/<account_hash(email)>/<id>.json`), keyed by whichever account was current at
    write time. That breaks the moment a session outlives an account switch — Claude Code lets
    you `/login` to a different account mid-session (and immediately charges the window that
    was already open to the new account), so the SAME session needs to keep writing to the
    SAME file across that switch; which account owns which slice of its history is recorded
    per-sample instead (see record_session_sample()'s `account` field and _parse_sample()).
    `email` is accepted only for backward compatibility with existing call sites — it no
    longer affects the path. Older per-account-directory files are never deleted or moved;
    _session_files()/_load_session_records() still read them in place."""
    safe = safe_session_id(session_id)
    if safe is None:
        raise ValueError("unusable session id")
    return shared_dir() / "sessions" / f"{safe}.json"


def _parse_sample(raw) -> list | None:
    """A raw sample entry -> [ts, cumulative_tokens, cumulative_cost, model, account,
    account_utilisation_pct], padding whatever trailing elements an older format didn't carry
    with None — never 0 (see the Task 12 brief's storage section: a missing value means
    unknown, not zero). None when the entry isn't a valid sample at all (wrong type/length).

    Every generation of the sample format is accepted in the same file:
      3 elements — pre-Task-11e:  [ts, tokens, cost]
      4 elements — Task 11e:      [..., model]
      5 elements — pre-Task-12:   [..., account]
      6 elements — Task 12:       [..., account_utilisation_pct]
      7 elements — the stale-observer fix: [..., account_resets_at]
    This is the single point where every generation is reconciled — record_session_sample(),
    _load_session_records(), and the attribution walker all route every raw entry through
    here.

    `account_resets_at` is when the window that `account_utilisation_pct` describes ends. It
    is what makes a stale reading detectable: a sample written AFTER its own resets_at is
    reporting a window that had already rolled. See _observation_series()."""
    if not isinstance(raw, (list, tuple)) or len(raw) not in (3, 4, 5, 6, 7):
        return None
    ts, tokens, cost = raw[0], raw[1], raw[2]
    model = raw[3] if len(raw) >= 4 else None
    account = raw[4] if len(raw) >= 5 else None
    pct = raw[5] if len(raw) >= 6 else None
    resets_at = raw[6] if len(raw) >= 7 else None
    return [ts, tokens, cost, model, account, pct, resets_at]


def record_session_sample(status: dict, email: str | None, now: float | None = None,
                           config: dict | None = None, account_pct: float | None = None,
                           account_resets_at: float | None = None) -> None:
    """Best-effort: append one [timestamp, cumulative_tokens, cumulative_cost_usd,
    model_display_name, account, account_utilisation_pct] sample to this session's file in the
    shared store, rate-limited to at most one per `sessionSampleSeconds` and pruned to
    `sessionSampleKeepSeconds`. A no-op when the status JSON carries no session_id. Wrapped so
    writing here can never slow or break the render — no exception escapes this function.
    `config` lets the render path pass its already-loaded config instead of this function
    re-reading + re-merging both config.json files itself; omitted (e.g. from a hook or test
    calling this directly), it loads its own, as before.

    `account` is account_hash(email) — the same stable, privacy-preserving id session_path()
    used to key its directory on before Task 12 — never the raw email. This also makes older
    per-account-directory data attribute correctly without recovering any email:
    _load_session_records() backfills exactly this same hash (the directory's own name) onto
    samples from that layout that predate the per-sample field.

    `account_resets_at` is when the window `account_pct` describes ends — stored so a reading
    can later be shown to be stale (see _observation_series()). None when unknown.

    `account_pct` is this account's *session* (5-hour) bucket's `used_percentage` at the
    moment of this write — the ground truth Task 12 attributes against — passed in by the
    caller (main(), which already computed it via pick_limits() for the '5h' segment itself)
    rather than re-fetched here; None when it isn't known (usage fetch off, no data yet, or
    the cache is stale). Never invented, never defaulted to 0.

    The model is `status["model"]["display_name"]` — the model active on THIS write, not
    necessarily the one active for the whole interval since the previous sample (a mid-interval
    switch is attributed to the newer model when the per-model breakdown is computed — see
    live_sessions()). A sample recorded before a given field existed reads back with that field
    None via _parse_sample() — never dropped, never crashes."""
    try:
        session_id = safe_session_id(status.get("session_id"))
        if not session_id:
            return
        now = time.time() if now is None else now
        config = load_config() if config is None else config
        interval = tunable(config, "sessionSampleSeconds")
        keep = tunable(config, "sessionSampleKeepSeconds")
        path = session_path(session_id)
        existing = load_json(path)
        raw_samples = existing.get("samples")
        samples = ([s for s in (_parse_sample(r) for r in raw_samples) if s is not None]
                   if isinstance(raw_samples, list) else [])
        if samples and (now - samples[-1][0]) < interval:
            return          # sampled too recently — nothing to do
        ctxw = status.get("context_window") or {}
        tokens = int(ctxw.get("total_input_tokens") or 0) + int(ctxw.get("total_output_tokens") or 0)
        cost = float((status.get("cost") or {}).get("total_cost_usd") or 0.0)
        # Sanitised at INGEST, exactly like repo_name() beside it. This was the hole: the
        # model name went into the shared store raw, and the store is read by EVERY profile
        # on the machine — so one session opened in a maliciously named project could put an
        # escape sequence into another profile's /squirrel-sessions output.
        model = sanitise((status.get("model") or {}).get("display_name"))
        account = account_hash(email)
        samples.append([now, tokens, cost, model, account, account_pct, account_resets_at])
        cutoff = now - keep
        samples = [s for s in samples if s[0] >= cutoff]
        write_cache(path, {
            "session_id": session_id,
            "repo": repo_name(status),
            "model": model,
            "started": existing.get("started") or samples[0][0],
            "samples": samples,
        })
        prune_session_store(now, config)
    except Exception:
        pass    # sampling must never disrupt the status line


def _model_breakdown(samples: list[list], include_first: bool) -> dict[str, dict[str, float]]:
    """Splits the total usage represented by `samples` (sorted [ts, tokens, cost, model]) by
    model -> {"tokens": n, "cost": c}, attributing each interval between consecutive samples to
    the model recorded on the LATER sample of the pair — the model active when Squirrel
    observed that chunk of usage. This is a documented approximation: a model switch mid-
    interval is attributed entirely to the newer model, since Squirrel only samples at most
    once every `sessionSampleSeconds`. A sample with no recorded model (padded from a
    pre-Task-11e 3-element entry) is grouped under the literal key 'unknown'.

    `include_first=True` also attributes samples[0]'s own cumulative value — usage from before
    Squirrel's first sample of this session — to samples[0]'s model; used for the *lifetime*
    breakdown, where that opening amount has to land somewhere and telescopes correctly back to
    the plain lifetime total. `include_first=False` only sums deltas between consecutive
    samples, excluding the first one's own absolute value; used for the *window* breakdown,
    which is already defined as newest-minus-baseline, not newest-minus-zero — the baseline
    sample marks where the window starts, it doesn't itself count as window usage."""
    if not samples:
        return {}
    pairs = samples
    if include_first:
        zero = [samples[0][0], 0, 0.0, samples[0][3]]
        pairs = [zero] + samples
    out: dict[str, dict[str, float]] = {}
    for prev, curr in zip(pairs, pairs[1:]):
        tokens_delta = max(0, curr[1] - prev[1])
        cost_delta = max(0.0, curr[2] - prev[2])
        if not tokens_delta and not cost_delta:
            continue    # a no-op interval contributes nothing — never invent an 'unknown' entry for it
        model = curr[3] or "unknown"
        bucket = out.setdefault(model, {"tokens": 0, "cost": 0.0})
        bucket["tokens"] += tokens_delta
        bucket["cost"] += cost_delta
    return out


def _model_mix_label(by_model: dict, metric: str, max_shown: int = 2) -> str:
    """'Opus 5 82% · Sonnet 5 18%' — one session's own usage split by model, as a plain ratio
    of that session's own total (per `metric`, 'cost' or 'tokens') — never weighted against the
    account's shared limit, since Squirrel has no way to know how the API weighs one model
    against another there. At most `max_shown` models are named, ranked by their share,
    descending; the rest collapse to a trailing '+N'. '' when there's nothing to show (no
    breakdown, or its total is zero)."""
    entries = [(model, vals["cost"] if metric == "cost" else vals["tokens"])
               for model, vals in by_model.items()]
    total = sum(v for _, v in entries)
    if not total:
        return ""
    entries.sort(key=lambda e: e[1], reverse=True)
    shown = entries[:max_shown]
    label = " · ".join(f"{model} {v / total * 100:.0f}%" for model, v in shown)
    extra = len(entries) - len(shown)
    if extra > 0:
        label += f" +{extra}"
    return label


def prune_session_store(now: float, config: dict | None = None) -> int:
    """Delete session records older than the keep window. Returns how many went.

    Audit #14: nothing ever removed them, and every render reads the whole directory —
    2000 files was measured at eighteen times the render cost. Records are already pruned
    sample-by-sample inside a file; this prunes the files themselves. Cheap because it only
    runs from the sampling path, which is already rate-limited to once a minute."""
    keep = tunable(config, "sessionSampleKeepSeconds")
    removed = 0
    for path in _session_files():
        try:
            if now - path.stat().st_mtime > keep:
                path.unlink(missing_ok=True)
                removed += 1
        except OSError:
            continue
    return removed


def _session_files() -> list[Path]:
    """Every session file Squirrel can find: the flat `sessions/<id>.json` layout (Task 12)
    plus every file under the older `sessions/<account_hash>/<id>.json` layout — never
    migrated, never deleted, just read alongside the new ones (see session_path())."""
    base = shared_dir() / "sessions"
    try:
        entries = list(base.iterdir())
    except OSError:
        return []
    files = [p for p in entries if p.is_file() and p.suffix == ".json"]
    for d in entries:
        if not d.is_dir():
            continue
        try:
            files.extend(p for p in d.iterdir() if p.is_file() and p.suffix == ".json")
        except OSError:
            pass
    return files


def _load_session_records() -> dict[str, dict]:
    """Every session, from every file, merged by session_id ->
    {"repo": str|None, "samples": [[ts, tokens, cost, model, account, pct], ...] sorted}.

    A legacy per-account-directory file (see session_path()) never recorded `account` on the
    sample itself — the directory it lived in WAS the account (account_hash(email), the exact
    same hash record_session_sample() writes today) — so every sample read from one is
    backfilled with that directory name here, never left as an unrecoverable unknown. A session
    that was already split across two such directories (a pre-Task-12 account switch) is
    reunited into one series, each half still correctly tagged with its own account. Nothing
    is ever written back; this is a read-only merge done fresh on every call."""
    out: dict[str, dict] = {}
    for f in _session_files():
        data = load_json(f)
        raw_samples = data.get("samples")
        if not isinstance(raw_samples, list):
            continue
        legacy_account = f.parent.name if f.parent.name != "sessions" else None
        parsed = []
        for r in raw_samples:
            s = _parse_sample(r)
            if s is None:
                continue
            if s[4] is None and legacy_account is not None:
                s[4] = legacy_account
            parsed.append(s)
        if not parsed:
            continue
        sid = data.get("session_id") or f.stem
        entry = out.setdefault(sid, {"repo": None, "samples": []})
        entry["repo"] = entry["repo"] or data.get("repo")
        entry["samples"].extend(parsed)
    for entry in out.values():
        entry["samples"].sort(key=lambda s: s[0])
    return out


# An account's utilisation has ONE true value at any moment, so readings taken within a few
# seconds of each other are the same observation seen by different sessions. When they
# disagree by more than a rounding step, one of them is stale and — absent a resets_at to say
# which — we cannot tell which.
OBS_CLUSTER_S = 30.0        # readings this close together are one observation
OBS_CONFLICT_PP = 1.0       # a larger spread within a cluster is contradiction, not rounding
# Grace before a reading is called stale. The test is intra-sample — the sample's own
# timestamp against the resets_at recorded beside it, both written by one process in one
# instant — so no clock is compared with any other clock and cross-profile skew cannot reach
# it. The margin is for the honest boundary case: a render that lands in the same second the
# window rolls, where the reading is a moment late rather than hours stale. Generous on
# purpose. What this fix exists to discard was three HOURS past its window.
OBS_STALE_GRACE_S = 120.0


def _observation_series(sessions: dict[str, list], account: str) -> list[tuple]:
    """Every trustworthy (ts, account_utilisation_pct) reading tagged with `account`, across
    every session's samples, sorted by ts. This is the ground-truth timeline the attribution
    walker steps through — built from ALL sessions at once, since two concurrently live
    sessions each sample the same account and are not two independent timelines.

    They are, however, not automatically the same timeline either, and assuming they were is
    what produced `183/3%` on the user's bar. Five sessions wrote readings for one account;
    one of them reported a frozen 45.0 every minute for three hours while the other four
    reported the true 3.0.

    Where that stale reading came from matters, because it is not where you would look: NOT
    our usage cache. Three of the four profiles on that machine had no usage cache at all, so
    the percentage came from Claude Code's OWN `rate_limits` block in the status JSON, and
    that block had gone stale inside a long-lived session — it was still describing the
    pre-reset window. Nothing in our fetch path was wrong and no amount of fetching more
    often would have helped. Do not go looking there. Merged blindly, a flat 3%% becomes a 3->45->3->45 sawtooth, and the walker
    credits every up-leg as +42 points of fresh consumption. Measured on the real store:
    182 attributed points against a 7%% account reading; 6 with that one observer removed.

    Two filters, because they cover different data:

      1. A reading written after its own `resets_at` describes a window that had already
         rolled. That is stale BY DEFINITION, not by heuristic, so it is dropped outright.

      2. Samples predating the resets_at field cannot be dated. Where undatable readings of
         the same moment contradict each other, neither can be shown to be the true one, so
         the whole cluster is dropped and that stretch becomes an observation GAP — reported
         as unknown rather than summed as movement. Unknown is not zero, and it is certainly
         not 42.

    Readings that agree are all kept, exactly as before, so the single-profile case — most
    people — is untouched."""
    raw: list[tuple[float, float, float | None]] = []
    for samples in sessions.values():
        for s in samples:
            ts, _tokens, _cost, _model, acc, pct = s[:6]
            resets_at = s[6] if len(s) > 6 else None
            if acc == account and pct is not None:
                raw.append((float(ts), float(pct), resets_at))
    raw.sort(key=lambda r: r[0])

    # (1) datable and expired -> stale. See OBS_STALE_GRACE_S for why this compares a sample
    # against its own recorded resets_at and never against the local clock.
    fresh = [r for r in raw
             if not (r[2] is not None and float(r[2]) + OBS_STALE_GRACE_S <= r[0])]

    # (2) undatable and contradictory -> a gap
    points: dict[float, tuple] = {}
    i = 0
    while i < len(fresh):
        j = i
        while j + 1 < len(fresh) and fresh[j + 1][0] - fresh[i][0] <= OBS_CLUSTER_S:
            j += 1
        cluster = fresh[i:j + 1]
        pcts = [c[1] for c in cluster]
        if max(pcts) - min(pcts) > OBS_CONFLICT_PP:
            pass                # contradiction we cannot resolve: observe nothing here
        else:
            for ts, pct, r in cluster:
                points.setdefault(ts, (pct, r))
        i = j + 1
    return [(ts, v[0], v[1]) for ts, v in sorted(points.items())]


def current_window_start(sessions: dict[str, list], account: str, now: float) -> float:
    """The start of the window the account is CURRENTLY in.

    Attribution used to clip at a rolling `now - SESSION_WINDOW_S`, which reaches back past
    the last reset into the previous window — and is why coverage read `4h59m/5h` for a
    window only 3h36m old. The real boundary is `resets_at - SESSION_WINDOW_S`, taken from
    the freshest reading that carries one. Samples predating that field fall back to the
    rolling five hours, which is what they have always had."""
    latest = None
    for samples in sessions.values():
        for s in samples:
            if len(s) > 6 and s[4] == account and s[6] is not None:
                ts = float(s[0])
                if latest is None or ts > latest[0]:
                    latest = (ts, float(s[6]))
    # This one DOES compare against the local clock — there is nothing else to compare a
    # "window still open?" question to — so it gets the same grace as the staleness test.
    if latest is not None and latest[1] + OBS_STALE_GRACE_S > now:
        return latest[1] - SESSION_WINDOW_S
    # No usable resets_at — every sample predates the field, or the newest one names a window
    # that has itself rolled. The boundary can still be INFERRED: the last time this account's
    # utilisation fell is the last time its window rolled, and that is where the current one
    # began. Measured on the user's real store this recovers the aligned start exactly, which
    # matters because the alternative — a rolling five hours — reaches back across that reset
    # and banks the previous window's consumption into this one's total.
    series = _observation_series(sessions, account)
    last_roll = None
    for (_t0, p0, _r0), (t1, p1, _r1) in zip(series, series[1:]):
        if p1 < p0:
            last_roll = t1
    rolling = now - SESSION_WINDOW_S
    if last_roll is not None and last_roll > rolling:
        return last_roll
    return rolling


def _session_account_series(samples: list[list], account: str) -> list[tuple[float, float, float]]:
    """One session's own (ts, cumulative_tokens, cumulative_cost) samples restricted to the
    stretches it was tagged with `account` — used both to gate whether the session was even
    present for a given interval and, when it was, to measure its own growth in it."""
    return [(s[0], s[1], s[2]) for s in samples if s[4] == account]


def walk_utilisation_attribution(sessions: dict[str, list], account: str) -> list[dict]:
    """THE interval walker (Task 12) — a pure function over per-session sample series, with no
    filesystem access, so it is directly testable. Steps through `account`'s own observed
    utilisation timeline (_observation_series()) and, for each interval between two consecutive
    observations, decides who it belongs to:

      exactly one session active in the interval  -> the whole delta, marked "exact"
      several sessions active                      -> split by their own cost deltas in the
                                                        interval (falling back to an even split
                                                        when none of them show a cost delta),
                                                        marked "estimated"
      no session active at all                      -> "unattributed" (the delta happened, but
                                                        nothing Squirrel tracks claims it — e.g.
                                                        usage from the web UI)
      utilisation dropped (delta < 0)                -> "reset" (the 5-hour window rolling, or
                                                        an account switch that jumps this
                                                        account's own reading down) — NEVER a
                                                        negative contribution; the post-reset
                                                        reading becomes the new baseline simply
                                                        by being the start of the next interval.

    "Active in an interval (t0, t1]" means the session wrote a sample tagged `account` inside
    it AND its cumulative tokens or cost grew relative to the last such sample at or before t0
    (or, lacking one, relative to its own first in-interval sample — so a brand-new session's
    very first reading is a baseline, not an instant spike). A session present but unchanged
    (idle) is never counted as active and receives nothing.

    Cost is used only to SPLIT an already-known delta among several concurrently active
    sessions — never as the measurement itself (see the Task 12 brief's rule 3).

    Returns one record per interval: {"start", "end", "delta" (>= 0), "kind", "shares": {sid:
    percentage_points}}."""
    obs = _observation_series(sessions, account)
    per_session = {sid: _session_account_series(samples, account) for sid, samples in sessions.items()}
    out: list[dict] = []
    # Utilisation cannot FALL inside a window — it only climbs until the window rolls. So the
    # baseline is the highest reading seen since the last roll, not merely the previous one,
    # and only consumption above that high-water mark is new. This is what makes "the part
    # cannot exceed the whole" true by construction rather than by hoping every reading is
    # sane: without it, one dip-and-recovery (a stale observer, a duplicated old reading)
    # is counted as a second helping of the same consumption. `183/3%` was ~300 of them.
    if not obs:
        return out
    prev_ts, peak, window_id = obs[0][0], obs[0][1], obs[0][2]
    for t1, p1, r1 in obs[1:]:
        t0 = prev_ts
        rolled = (r1 is not None and window_id is not None and float(r1) > float(window_id))
        if rolled or (p1 < peak and r1 is None and window_id is None):
            # A genuine window roll: dated by a later resets_at, or — for samples too old to
            # carry one — inferred from the drop, exactly as before. Never a negative
            # contribution; the post-roll reading is simply the next baseline.
            out.append({"start": t0, "end": t1, "delta": 0.0, "kind": "reset", "shares": {}})
            prev_ts, peak, window_id = t1, p1, r1 if r1 is not None else window_id
            continue
        if p1 < peak:
            # A fall inside a window that has NOT rolled. The reading is wrong, not the
            # world: skip it entirely rather than let its recovery read as fresh usage.
            continue
        delta = p1 - peak
        peak = p1
        prev_ts = t1
        active: dict[str, float] = {}
        for sid, series in per_session.items():
            in_interval = [s for s in series if t0 < s[0] <= t1]
            if not in_interval:
                continue
            prior = [s for s in series if s[0] <= t0]
            baseline = prior[-1] if prior else in_interval[0]
            current = in_interval[-1]
            if current[1] > baseline[1] or current[2] > baseline[2]:
                active[sid] = current[2] - baseline[2]     # cost delta, may be 0 — used for splitting
            elif current[1] < baseline[1] or current[2] < baseline[2]:
                # The session's own counters went BACKWARDS: it restarted (resumed after a crash,
                # a machine restart, or a replaced process) and its totals began again from near
                # zero. That is not idleness — work happened, we simply cannot size it. Count the
                # session as active with an unknown weight (0.0), so it takes part in the split
                # instead of being silently dropped; the even-split fallback covers the case where
                # no active session has a usable weight.
                active[sid] = None      # active, but its weight is unknowable this interval
        if not active:
            out.append({"start": t0, "end": t1, "delta": delta, "kind": "unattributed", "shares": {}})
        elif len(active) == 1:
            sid = next(iter(active))
            out.append({"start": t0, "end": t1, "delta": delta, "kind": "exact", "shares": {sid: delta}})
        else:
            # A restarted session (weight None) cannot be sized, so no weighting of this
            # interval would be honest: fall back to an even split among everyone active.
            unknown_weight = any(v is None for v in active.values())
            total_cost = 0.0 if unknown_weight else sum(v for v in active.values() if v and v > 0)
            if total_cost > 0:
                shares = {sid: delta * (max(0.0, v or 0.0) / total_cost) for sid, v in active.items()}
            else:
                even = delta / len(active)
                shares = {sid: even for sid in active}
            out.append({"start": t0, "end": t1, "delta": delta, "kind": "estimated", "shares": shares})
    return out


def account_window_attribution(sessions: dict[str, list], account: str, window_start: float,
                                now: float) -> dict:
    """Aggregates walk_utilisation_attribution() over the current window [window_start, now]
    for one account -> {"sessions": {sid: {"points", "kind", "coverage_seconds"}},
    "total_points", "first_observation", "last_observation"}.

    Per session:
      "points"  — summed attributed percentage points within the window (0 when none).
      "kind"    — "exact" when every contributing interval was exact AND this session's own
                  relevant span (from `max(window_start, its own first sample)` through its
                  last sample, clipped to `now`) is fully bracketed by real observations of
                  `account`'s utilisation (see the gap check below — "exact" means the account
                  was continuously observed across that whole span, nothing weaker);
                  "estimated" when some contributing interval was a split, OR there's a gap
                  in that observation coverage (a genuine partial figure — the true total may
                  be higher); "unknown" when nothing at all could be attributed AND a gap
                  explains why (Task 12 rule 4: unknown is shown as unknown, never as a
                  confident zero).
      "coverage_seconds" — how much of this session's own presence in the window overlaps a
                  span where `account`'s utilisation was actually observed (bounded by the
                  window itself) — e.g. a session that has only been open 40 minutes of a
                  5-hour window can have at most 40 minutes of coverage. The same figure that
                  drives the gap check above, not a separate approximation.

    Gap check: `account`'s utilisation only needs to have been observed continuously — by ANY
    session, not necessarily this one — across `[max(window_start, this session's own first
    sample), min(this session's last sample, now)]`; the earliest and latest of every session's
    utilisation readings for this account (`first_observation`/`last_observation`, module-wide)
    must bracket that whole span, or there is a stretch this session's own activity fell into
    that nobody was watching. This deliberately does NOT require that every interval WITHIN
    that span resolved to a single active session — an interval where several sessions were
    active gets split (kind "estimated" already covers that), and one where NO session could be
    identified as active is simply unclaimed by anyone (the account's own utilisation was still
    genuinely observed throughout; nobody just happened to be credited for that slice — see the
    design note's §4, which flags this as a distinct, deliberately out-of-scope ambiguity from
    a true observation gap)."""
    intervals = walk_utilisation_attribution(sessions, account)
    obs_ts = sorted({p for iv in intervals for p in (iv["start"], iv["end"])})
    first_obs = obs_ts[0] if obs_ts else None
    last_obs = obs_ts[-1] if obs_ts else None

    totals: dict[str, dict] = {}
    for iv in intervals:
        if iv["end"] <= window_start:
            continue
        for sid, pp in iv["shares"].items():
            entry = totals.setdefault(sid, {"points": 0.0, "estimated": False})
            entry["points"] += pp
            if iv["kind"] == "estimated":
                entry["estimated"] = True

    out_sessions: dict[str, dict] = {}
    for sid, samples in sessions.items():
        series = _session_account_series(samples, account)
        if not series:
            continue
        session_start = max(series[0][0], window_start)
        session_end = min(series[-1][0], now)
        if first_obs is None or last_obs is None:
            coverage = 0.0
        else:
            lo = max(session_start, first_obs, window_start)
            hi = min(session_end, last_obs, now)
            coverage = max(0.0, hi - lo)
        span = max(0.0, min(session_end, now) - max(session_start, window_start))
        has_gap = coverage < span
        info = totals.get(sid, {"points": 0.0, "estimated": False})
        in_window = any(window_start <= t <= now for t, _tok, _cost in series)
        if not in_window:
            # Present in the history but not in THIS window. Nothing is known about its
            # share of the current window, and "unknown" is not "0pp" — that distinction is
            # the whole reason this attribution reports a kind at all.
            out_sessions[sid] = {"points": 0.0, "kind": "unknown", "coverage_seconds": 0.0}
            continue
        if info["points"] == 0.0 and has_gap and not info["estimated"]:
            kind = "unknown"
        elif info["estimated"] or has_gap:
            kind = "estimated"
        else:
            kind = "exact"
        out_sessions[sid] = {"points": info["points"], "kind": kind, "coverage_seconds": coverage}
    total_points = sum(v["points"] for v in totals.values())
    return {"sessions": out_sessions, "total_points": total_points,
            "first_observation": first_obs, "last_observation": last_obs}


def live_sessions(email: str | None, now: float, config: dict | None = None) -> list[dict]:
    """Every session whose newest sample is within `sessionLiveSeconds` AND whose newest sample
    is tagged with this account (or carries no account tag at all — permissive for older data
    that predates the per-sample field, per the Task 12 brief's backward-compatibility rule: a
    missing tag is never treated as "not mine"). A session that has since moved on to a
    DIFFERENT account is excluded — it is that account's to list now, not this one's.

    Each entry carries: session_id, repo, lifetime_tokens/lifetime_cost (the newest sample's
    cumulative values — session-wide, not account-scoped: Claude Code's own cost/token
    counters are a property of the session, regardless of which account is currently logged
    in), lifetime_by_model/window_by_model ({model: {"tokens", "cost"}} — see _model_breakdown();
    'unknown' for samples with no recorded model), window_tokens/window_cost (the *delta* since
    `now - SESSION_WINDOW_S` — not the lifetime total, so an old session's early tokens no
    longer count against the current 5-hour window), burn_per_min/burn_cost_per_min (tokens-
    and dollars-per-minute over the last `burnWindowSeconds`), partial (True when the session is
    younger than the window, so window_tokens/window_cost are measured from the session's first
    sample rather than a full window's worth — a lower bound), age (seconds since the newest
    sample), and account (the account tag on the newest sample, for display).

    Task 12 additionally attaches the GROUND-TRUTH attribution for the current window,
    computed once per call via account_window_attribution() and merged in per session:
    attributed_points, attribution_kind ("exact"/"estimated"/"unknown"), coverage_seconds. See
    that function's docstring for exactly what each means. window_tokens/window_cost/
    window_by_model/burn_*/lifetime_* are UNCHANGED by Task 12 — they remain the session's own
    cost/token proxy figures, still shown as secondary, informational columns (see
    cmd_show_sessions()); only the window PERCENTAGE/SHARE figures now come from
    attributed_points instead of a cost-or-tokens ratio.
    """
    config = config or {}
    live_seconds = tunable(config, "sessionLiveSeconds")
    burn_window = tunable(config, "burnWindowSeconds")
    account = account_hash(email)
    records = _load_session_records()
    # The window the account is in NOW, not a rolling five hours — see current_window_start().
    window_start = current_window_start({sid: r["samples"] for sid, r in records.items()},
                                        account, now)
    attribution = account_window_attribution(
        {sid: r["samples"] for sid, r in records.items()}, account, window_start, now)
    attr_sessions = attribution["sessions"]
    out: list[dict] = []
    for sid, rec in records.items():
        samples = rec["samples"]
        if not samples:
            continue
        newest = samples[-1]
        if now - newest[0] > live_seconds:
            continue
        newest_account = newest[4]
        if newest_account is not None and newest_account != account:
            continue    # moved on to a different account — not ours to list any more
        lifetime_tokens, lifetime_cost = newest[1], newest[2]
        cutoff = window_start
        baseline_idx = None
        for i, s in enumerate(samples):
            if s[0] <= cutoff:
                baseline_idx = i
            else:
                break
        partial = False
        if baseline_idx is None:
            baseline_idx = 0
            partial = samples[0][0] > cutoff
        baseline = samples[baseline_idx]
        window_tokens = max(0, lifetime_tokens - baseline[1])
        window_cost = max(0.0, lifetime_cost - baseline[2])
        window_by_model = _model_breakdown(samples[baseline_idx:], include_first=False)
        lifetime_by_model = _model_breakdown(samples, include_first=True)
        burn_cutoff = now - burn_window
        burn_baseline = None
        for s in samples:
            if s[0] <= burn_cutoff:
                burn_baseline = s
            else:
                break
        if burn_baseline is None:
            burn_baseline = samples[0]
        elapsed_min = (newest[0] - burn_baseline[0]) / 60.0
        # A counter that goes backwards means the session's own totals restarted (a resumed or
        # replaced session), not negative work — report no burn rather than a negative rate.
        token_delta = max(0.0, lifetime_tokens - burn_baseline[1])
        cost_delta = max(0.0, lifetime_cost - burn_baseline[2])
        burn_per_min = (token_delta / elapsed_min) if elapsed_min > 0 else 0.0
        burn_cost_per_min = (cost_delta / elapsed_min) if elapsed_min > 0 else 0.0
        attr = attr_sessions.get(sid, {"points": 0.0, "kind": "unknown", "coverage_seconds": 0.0})
        out.append({
            "session_id": sid,
            "repo": rec.get("repo"),
            "account": newest_account,
            "attributed_points": attr["points"],
            "attribution_kind": attr["kind"],
            "coverage_seconds": attr["coverage_seconds"],
            "lifetime_tokens": lifetime_tokens,
            "lifetime_cost": lifetime_cost,
            "lifetime_by_model": lifetime_by_model,
            "window_tokens": window_tokens,
            "window_cost": window_cost,
            "window_by_model": window_by_model,
            "burn_per_min": burn_per_min,
            "burn_cost_per_min": burn_cost_per_min,
            "partial": partial,
            "age": now - newest[0],
        })
    return out


def attribution_metric(config: dict | None) -> str:
    """The configured `attributionMetric` ('cost' default — the only figure that includes
    subagent work, see live_sessions()/ATTRIBUTION_METRIC_HELP — or 'tokens' for the pre-11d,
    tokens-only behaviour); an unset or invalid value always falls back to 'cost', never an
    error or a silently-wrong metric."""
    value = (config or {}).get("attributionMetric")
    return value if value in ATTRIBUTION_METRICS else "cost"


def _window_metric(s: dict, metric: str) -> float:
    """This session's window delta under the configured attribution metric."""
    return s["window_cost"] if metric == "cost" else s["window_tokens"]


def _lifetime_metric(s: dict, metric: str) -> float:
    """This session's lifetime cumulative value under the configured attribution metric."""
    return s["lifetime_cost"] if metric == "cost" else s["lifetime_tokens"]


def _session_label(session: dict, used: set) -> str:
    """The repo name when known, else the first 4 characters of the session id; collisions get
    a trailing digit so every label in one listing is unique."""
    base = session.get("repo") or (str(session.get("session_id") or "")[:4] or "????")
    label = base
    n = 2
    while label in used:
        label = f"{base}{n}"
        n += 1
    used.add(label)
    return label


def segment_label(config: dict | None, key: str) -> str:
    """Display label for one segment: a `segmentLabels` override for `key` if set and
    non-blank, else the shipped default for `key`, else `key` itself. A partial override
    (only one of `share`/`share-life` set) never loses the other's default — each key is
    resolved independently, not by treating `segmentLabels` as an all-or-nothing map."""
    labels = (config or {}).get("segmentLabels")
    labels = labels if isinstance(labels, dict) else {}
    override = labels.get(key)
    if isinstance(override, str) and override:
        return override
    return SEGMENT_LABELS_DEFAULT.get(key, key)


def share_format(config: dict | None) -> str:
    """The configured `shareFormat` ('contribution' default, 'share', or 'both'); an unset or
    invalid value always falls back to 'contribution', never an error or blank format."""
    value = (config or {}).get("shareFormat")
    return value if value in SHARE_FORMATS else "contribution"


def cmd_set_label(arg: str) -> str:
    parts = (arg or "").split(None, 1)
    if not parts:
        return (f"usage: /squirrel-label <segment> <text|reset>   "
                f"labelable segments: {', '.join(LABELABLE_SEGMENTS)}")
    key = parts[0]
    if key not in LABELABLE_SEGMENTS:
        return f"'{key}' has no configurable label — one of: {', '.join(LABELABLE_SEGMENTS)}"
    text = parts[1].strip() if len(parts) > 1 else ""
    if not text:
        return f"usage: /squirrel-label {key} <text|reset>"
    if text.lower() == "reset":
        layer, stored = edit_layer("segmentLabels")
        labels = dict(stored.get("segmentLabels") or {})
        labels.pop(key, None)
        # replace: save_config() would merge the label we just removed back in.
        replace_config({"segmentLabels": labels}, layer=layer)
        return f"label for '{key}' reset to '{SEGMENT_LABELS_DEFAULT.get(key, key)}'"
    save_config({"segmentLabels": {key: text}})
    return f"label for '{key}' set to '{text}'"


def cmd_set_share_format(arg: str) -> str:
    value = (arg or "").strip().lower()
    if value not in SHARE_FORMATS:
        return f"usage: /squirrel-share-format <{'|'.join(SHARE_FORMATS)}>"
    save_config({"shareFormat": value})
    return f"share format set to '{value}' ({SHARE_FORMAT_HELP[value]})"


def cmd_set_attribution_metric(arg: str) -> str:
    value = (arg or "").strip().lower()
    if value not in ATTRIBUTION_METRICS:
        return f"usage: /squirrel-attribution-metric <{'|'.join(ATTRIBUTION_METRICS)}>"
    save_config({"attributionMetric": value})
    return f"attribution metric set to '{value}' ({ATTRIBUTION_METRIC_HELP[value]})"


def sessions_show_model(config: dict | None) -> bool:
    """Whether `--show-sessions` includes the per-model mix column — on by default;
    `/squirrel-sessions-show-model off` drops it (e.g. for a narrower table, or for anyone who
    finds it noise)."""
    return bool((config or {}).get("sessionsShowModel", True))


def cmd_set_sessions_show_model(value: str) -> str:
    value = (value or "").strip().lower()
    if value in ("on", "true", "yes"):
        save_config({"sessionsShowModel": True})
        return "--show-sessions will include the per-model mix column"
    if value in ("off", "false", "no"):
        save_config({"sessionsShowModel": False})
        return "--show-sessions will no longer include the per-model mix column"
    return "usage: /squirrel-sessions-show-model <on|off>"


def session_contribution_enabled(config: dict | None) -> bool:
    """Whether the `session` (5h) segment may fold in this session's contribution as a
    composite — on by default; `/squirrel-session-contribution off` always renders the plain
    account total instead, regardless of what attribution data is available."""
    return bool((config or {}).get("sessionContribution", True))


def cmd_set_session_contribution(value: str) -> str:
    value = (value or "").strip().lower()
    if value in ("on", "true", "yes"):
        save_config({"sessionContribution": True})
        return "the 5h segment will fold in this session's contribution when the data supports it"
    if value in ("off", "false", "no"):
        save_config({"sessionContribution": False})
        return "the 5h segment now always shows the plain account total"
    return "usage: /squirrel-session-contribution <on|off>"


def session_contribution(status: dict, email: str | None, now: float,
                          config: dict | None = None) -> dict | None:
    """This session's slice of the current 5-hour window's GROUND-TRUTH utilisation —
    {"points": percentage points, "share_pct": this session's share of every live session's
    attributed points, "partial": bool} — or None when there is nothing meaningful to show:
    this session isn't (yet) live, it is the *only* live session (trivially 100% of whatever is
    attributed — a '90/90%' composite would just repeat the account total), or its own
    attribution is entirely "unknown" (see live_sessions()/account_window_attribution()) — a
    made-up composite is never shown in place of an honest "we don't know yet".

    `points` IS the contribution figure directly — Task 12 replaced the old `fraction *
    account_utilisation` estimate with a real, summed percentage-point delta read straight off
    the account's own utilisation history (see walk_utilisation_attribution()), so there is no
    multiplication left to do. `partial` is True whenever this session's own kind is anything
    other of "exact" (i.e. "estimated" — some of its interval(s) were split among concurrent
    sessions, or part of its own window has a coverage gap) — the caller marks the figure with
    '~' exactly as it always has for a not-fully-known number."""
    config = config or {}
    sid = status.get("session_id")
    if not sid:
        return None
    sessions = live_sessions(email, now, config)
    if len(sessions) < 2:
        return None
    mine = next((s for s in sessions if s["session_id"] == sid), None)
    if mine is None:
        return None
    if mine.get("attribution_kind") == "unknown":
        return None
    total_points = sum(s.get("attributed_points", 0.0) for s in sessions)
    share_pct = (mine.get("attributed_points", 0.0) / total_points * 100) if total_points else 0.0
    return {"points": mine.get("attributed_points", 0.0), "share_pct": share_pct,
            "partial": mine.get("attribution_kind") != "exact"}


def share_segment_runs(status: dict, email: str | None, now: float, config: dict | None = None,
                   account_pct: float | None = None) -> list:
    """This session's contribution to the shared 5-hour window — default `shareFormat`
    ('contribution'): 'mine 19%/89%', this session's attributed percentage points of the
    account's 5h utilisation, then the account total (`account_pct`, shown only as the
    denominator now — see session_contribution()). Labelled via `segmentLabels` (default
    'mine' — see segment_label()) precisely so it can never be mistaken for the account-wide
    '5h' segment, which is what actually consumes the shared limit.

    `share_text` ('mine 21%') is this session's share of every live session's attributed
    points — a relative "am I the problem?" figure among sessions, independent of the
    account's overall utilisation; coloured on that same independent scale
    (share_pct_color(), shareWarnPercent/shareDangerPercent — this segment had never been
    coloured at all before Task 12; now it matches the composite's own convention exactly:
    the contribution/share number on the share scale, the account total on the usage scale
    (pct_color())). Renders '{label} ?' outright — plain, uncoloured, degrading sensibly —
    when this session's own window attribution is entirely unknown (see session_contribution())
    — Task 12's rule 4: unknown is shown as unknown, never as a confident wrong number.

    Falls back to the plain share ('mine 21%') whenever `account_pct` is None — no usage
    data, a stale cache with no value, or the caller simply not supplying one. `shareFormat ==
    "share"` always renders the plain share, regardless of `account_pct`. `shareFormat ==
    "both"` shows the share and the contribution together ('mine 21% → 19%/89%'), falling back
    the same way.

    None when the session is unknown or not (yet) live."""

    config = config or {}
    sid = status.get("session_id")
    if not sid:
        return []
    sessions = live_sessions(email, now, config)
    mine = next((s for s in sessions if s["session_id"] == sid), None)
    if mine is None:
        return []
    label = segment_label(config, "share")
    if mine.get("attribution_kind") == "unknown":
        return [(f"{label} ?", None)]
    total_points = sum(s.get("attributed_points", 0.0) for s in sessions)
    share_pct = (mine.get("attributed_points", 0.0) / total_points * 100) if total_points else 0.0
    contribution_pp = mine.get("attributed_points", 0.0)
    mark = "~" if mine.get("attribution_kind") != "exact" else ""
    share_color = share_pct_color(share_pct, config)
    fmt = share_format(config)
    runs = [(f"{label} ", None)]
    if fmt == "share" or account_pct is None:
        runs.append((f"{mark}{share_pct:.0f}%", share_color))
        return runs
    if fmt == "both":
        runs.append((f"{mark}{share_pct:.0f}%", share_color))
        runs.append((" \u2192 ", None))
    # 'pp', matching the composite and the /squirrel-sessions table — see the note in
    # session_usage_segment_runs(): two percentages of the same window separated by a slash
    # read as a fraction, and the unit is the only thing that stops them.
    runs.append((f"{mark}{contribution_pp:.0f}pp", share_color))
    runs.append(("/", None))
    runs.append((f"{account_pct:.0f}%", pct_color(account_pct, config)))
    return runs


def share_segment(status: dict, email: str | None, now: float, config: dict | None = None,
                   account_pct: float | None = None) -> str | None:
    """String form of share_segment_runs(); None (not "") when nothing renders — the
    convention every caller of this segment already relies on."""
    return join_runs(share_segment_runs(status, email, now, config, account_pct)) or None


def share_life_segment(status: dict, email: str | None, now: float, config: dict | None = None) -> str | None:
    """'life 22%' — this session's share of all live sessions' lifetime totals, labelled via
    `segmentLabels` (default 'life' — see segment_label()). None when the session is unknown
    or not (yet) live. Driven by attribution_metric() — 'cost' by default (see
    session_contribution()'s docstring)."""
    config = config or {}
    sid = status.get("session_id")
    if not sid:
        return None
    sessions = live_sessions(email, now, config)
    mine = next((s for s in sessions if s["session_id"] == sid), None)
    if mine is None:
        return None
    metric = attribution_metric(config)
    total = sum(_lifetime_metric(s, metric) for s in sessions)
    pct = (_lifetime_metric(mine, metric) / total * 100) if total else 0.0
    label = segment_label(config, "share-life")
    return f"{label} {pct:.0f}%"


def sessions_segment(status: dict, email: str | None, now: float, config: dict | None = None) -> str | None:
    """'A 52% · B 31% · ▸C 12% · D 5%' — every live session ranked by its GROUND-TRUTH
    attributed share of the current window, descending; the current session (if any) marked
    '▸'. At most `sessionsMax` entries, then '+N'. None when there are no live sessions.

    A session whose own attribution is entirely unknown (see live_sessions()) shows '?' in
    place of a fabricated '0%' — Task 12's rule 4, marked '~' when its own figure is anything
    less than fully exact.

    WHICH percentage follows `shareFormat`, the same setting and the same vocabulary the
    `share` segment uses, and for the same reason the user gave when they caught this: a bar
    reading 'mine ~43%/77% │ ▸beacon ~76% · sandbox ~24%' shows the same session as
    both 43 and 76, because those two percentages had different denominators — points of the
    ACCOUNT's window on the left, share of the LIVE SESSIONS' total on the right. Two
    denominators, one '%' sign, side by side on one line.

      'contribution' (default) — attributed percentage points of the account's window, the
                                 same unit as 'mine 43%/77%' and the /squirrel-sessions table
      'share'                  — the ratio among live sessions (what this always did)
      'both'                   — 'beacon 43% (76%)', the points with the ratio after it"""
    config = config or {}
    sessions = live_sessions(email, now, config)
    if not sessions:
        return None
    sid = status.get("session_id")
    total_points = sum(s.get("attributed_points", 0.0) for s in sessions)
    sessions = sorted(sessions, key=lambda s: s.get("attributed_points", 0.0), reverse=True)
    maxn = tunable(config, "sessionsMax")
    used: set = set()
    parts = []
    shown = sessions[:maxn]
    for s in shown:
        label = _session_label(s, used)
        marker = "▸" if sid and s["session_id"] == sid else ""
        if s.get("attribution_kind") == "unknown":
            parts.append(f"{marker}{label} ?")
            continue
        points = s.get("attributed_points", 0.0)
        ratio = (points / total_points * 100) if total_points else 0.0
        mark = "~" if s.get("attribution_kind") != "exact" else ""
        fmt = share_format(config)
        if fmt == "share":
            figure = f"{mark}{ratio:.0f}%"
        elif fmt == "both":
            figure = f"{mark}{points:.0f}pp ({ratio:.0f}%)"
        else:
            figure = f"{mark}{points:.0f}pp"
        parts.append(f"{marker}{label} {figure}")
    extra = len(sessions) - len(shown)
    if extra > 0:
        parts.append(f"+{extra}")
    return DOT.join(parts)


# Which columns of the /squirrel-sessions table survive a narrow terminal, worst first. This
# is the bar's own idiom — shed by priority — applied to a table, and the order answers the
# question the table exists for: "which session should I stop?" That needs the name, its share
# of the window, and whether it is still burning. Everything else is context.
#
# `session` and `5h pts` are never dropped: the name and the ranking figure ARE the table.
# `5h pts` carries its own uncertainty marks ('~' estimated, '?' unknown), so losing the
# `coverage` column costs precision but never lets a figure read as more certain than it is —
# which is the line that matters here.
SESSION_COLUMN_DROP_ORDER = ("life tok", "life cost", "5h tok", "model mix", "life %",
                             "5h cost", "coverage", "age", "burn")


def _wrap_note(text: str, width: int) -> list:
    """Wrap a trailing note to the table's budget. The notes under this table are the part a
    user actually reads to understand the numbers, and a fix for a table that wraps is not
    much of a fix if its own explanation still does."""
    import textwrap
    return textwrap.wrap(text, max(20, width), break_long_words=False,
                         break_on_hyphens=False) or [""]


def _session_table_width(width: int | None = None) -> int:
    return width if isinstance(width, int) and width > 0 else terminal_width()


def _fit_session_columns(order: list, cells: list, width: int | None = None) -> list:
    """The widest prefix of `order` that fits, dropping by SESSION_COLUMN_DROP_ORDER."""
    budget = _session_table_width(width)
    keep = list(order)

    def total(columns):
        return sum(max(len(h), *(len(c[h]) for c in cells)) for h in columns) + 2 * (len(columns) - 1)

    for column in SESSION_COLUMN_DROP_ORDER:
        if total(keep) <= budget:
            break
        if column in keep:
            keep.remove(column)
    return keep


def cmd_show_sessions(current_session_id: str | None = None,
                       width: int | None = None) -> str:
    """The full cross-session table for /squirrel-sessions: every live session on this
    account, sorted by its GROUND-TRUTH attributed utilisation points (Task 12), with
    coverage, cost/tokens (secondary columns, still driven by `attributionMetric` for the
    lifetime figures — see below), model mix, burn rate, and age — this is what a user reads
    to decide which session to stop. `current_session_id` (passed via ${CLAUDE_SESSION_ID}
    substitution when the Claude Code version supports it) marks that row with '▸'; without
    it, the table is still shown, just with nothing marked.

    Task 12: the '5h pts' column and the sort order are the account's own utilisation, summed
    per session via account_window_attribution() — 'pp' (percentage points) with a trailing
    mark: none for "exact" (this session was the only one active for every interval it
    contributed to), '~' for "estimated" (split among concurrent sessions in at least one
    interval, or a coverage gap alongside some real signal), or '?' for "unknown" (nothing
    could be attributed at all — shown as unknown, never as a confident zero). 'coverage'
    shows how much of the window Squirrel can actually vouch for, for this session (e.g.
    '93m/5h' — see account_window_attribution()'s docstring). The window's own cost/tokens
    stay visible as secondary, informational columns (they no longer drive the ranking).

    The 'life cost'/'life %'/'life tok' columns are UNCHANGED by Task 12 — there is no ground
    truth for a *lifetime* percentage (utilisation resets every 5 hours), so they remain the
    pre-Task-12 cost/token ratio, still following `attribution_metric()` (cost by default,
    because Claude Code's status JSON does not fold subagent usage into
    context_window.total_input_tokens/total_output_tokens the way it does into
    cost.total_cost_usd).

    Task 11e: when `sessions_show_model()` is on (default), a 'model mix' column shows each
    session's own window usage split by model (e.g. 'Opus 5 82% · Sonnet 5 18%') via
    _model_mix_label() — a ratio of that session's own total, informational only. This is NOT
    the same as the exact, API-reported per-model weekly buckets the `permodel` segment shows
    (§ README "How the numbers are derived")."""
    email = _current_email()
    now = time.time()
    config = load_config()
    sessions = live_sessions(email, now, config)
    if not sessions:
        return ("no live Squirrel sessions" + (f" for {email}" if email else "")
                + " — a session appears here once it's been open a minute or so")
    metric = attribution_metric(config)
    show_model = sessions_show_model(config)
    total_life = sum(_lifetime_metric(s, metric) for s in sessions)
    sessions = sorted(sessions, key=lambda s: s.get("attributed_points", 0.0), reverse=True)
    used: set = set()
    cells: list = []
    any_estimated = any(s.get("attribution_kind") == "estimated" for s in sessions)
    any_unknown = any(s.get("attribution_kind") == "unknown" for s in sessions)
    window_span = f"{int(SESSION_WINDOW_S // 3600)}h"   # '5h' — the brief's own example ('93m/5h')
    for s in sessions:
        label = _session_label(s, used)
        marker = "▸" if current_session_id and s["session_id"] == current_session_id else ""
        kind = s.get("attribution_kind", "unknown")
        if kind == "unknown":
            pts_text = "?"
        else:
            pts_text = f"{'~' if kind == 'estimated' else ''}{s.get('attributed_points', 0.0):.0f}pp"
        life_pct = (_lifetime_metric(s, metric) / total_life * 100) if total_life else 0.0
        # Sanitised at assembly as well as at ingest. Records written by an older version
        # are still in the shared store and are not re-written; a table built from them must
        # be safe today, not once every machine has rotated its samples.
        cells.append({
            "session": sanitise(f"{marker}{label}"),
            "5h pts": sanitise(pts_text),
            "coverage": sanitise(f"{fmt_relative(s.get('coverage_seconds', 0.0))}/{window_span}"),
            "5h cost": fmt_cost(s["window_cost"]) + ("~" if s.get("partial") else ""),
            "5h tok": fmt_tokens(s["window_tokens"]),
            "life cost": fmt_cost(s["lifetime_cost"]),
            "life %": f"{life_pct:.0f}%",
            "life tok": fmt_tokens(s["lifetime_tokens"]),
            "model mix": sanitise(_model_mix_label(s.get("window_by_model") or {}, metric) or "-"),
            "burn": f"{fmt_cost(s['burn_cost_per_min'])}/min ({fmt_tokens(int(s['burn_per_min']))}tok/min)",
            "age": fmt_relative(s["age"]),
        })
    order = ["session", "5h pts", "coverage", "5h cost", "5h tok", "life cost", "life %",
             "life tok"] + (["model mix"] if show_model else []) + ["burn", "age"]
    keep = _fit_session_columns(order, cells, width)
    hidden = [h for h in order if h not in keep]
    widths = [max(len(h), *(len(c[h]) for c in cells)) for h in keep]

    def _row(values):
        return "  ".join(v.ljust(w) for v, w in zip(values, widths)).rstrip()

    budget = _session_table_width(width)
    lines = [_row(keep), _row(["-" * w for w in widths])]
    lines += [_row([c[h] for h in keep]) for c in cells]
    notes: list = []
    if hidden:
        notes.append(f"narrowed to fit {budget} columns — hiding {', '.join(hidden)}. "
                     "Widen the window, or run /squirrel-sessions wide for the whole table.")
    if current_session_id:
        notes.append("▸ = this session")
    if any(s.get("partial") for s in sessions) and "5h cost" in keep:
        notes.append("~ (cost/tokens columns) = partial (session younger than the 5-hour window)")
    notes.append("5h pts: this session's share of the account's own 5-hour utilisation, read "
                 "straight from Claude's usage data — 'exact' (no mark) when it was the only "
                 "session active, '~' (estimated) when it shared the window with others or "
                 "part of its coverage is a gap, '?' (unknown) when nothing could be attributed "
                 "at all yet (e.g. before Squirrel started watching this account)")
    if any_estimated:
        notes.append("~ (5h pts) = estimated — split across concurrent sessions by cost, or a partial coverage gap")
    if any_unknown:
        notes.append("? (5h pts) = unknown — no utilisation data covers this session's activity yet")
    if "life %" in keep:
        notes.append(f"life % ranked by {'cost' if metric == 'cost' else 'tokens'} "
                     f"(attributionMetric={metric}; change with /squirrel-attribution-metric) — "
                     "there is no ground truth for a lifetime percentage, since utilisation resets "
                     "every 5 hours")
    notes.append("note: token counts exclude subagent usage; cost includes it — a session can "
                 "show few tokens but a large cost when it runs a lot of subagent work")
    for note in notes:
        lines.append("")
        lines += _wrap_note(note, budget)
    return "\n".join(lines)


# ── usage: fetch (runs in the detached `--fetch` process) ────────────────────
# urllib (and the http/ssl/email machinery it pulls in) is imported lazily, only by the functions
# below, and only when they actually run — which is always inside the detached `--fetch` child,
# never on the render path. `_OPENER` is a module-level cache of the built opener (constructed
# once per process, on first use) rather than a plain constant, precisely so nothing above forces
# the import at module-load time; see _no_redirect_handler_class()/_get_opener() and the
# LazyImportGuardTests in test_statusline.py.
_OPENER = None


def _no_redirect_handler_class():
    """The urllib.request.HTTPRedirectHandler subclass that refuses redirects (urllib forwards
    the Authorization header to the redirect target otherwise). A fresh class each call — this
    is only ever called once per process (from _get_opener()) plus directly by
    FetchTests.test_no_redirect_handler_refuses_redirects, which needs urllib imported to
    exercise it without depending on _get_opener()'s caching."""
    import urllib.error
    import urllib.request

    class _NoRedirect(urllib.request.HTTPRedirectHandler):
        def redirect_request(self, req, fp, code, msg, headers, newurl):
            raise urllib.error.HTTPError(req.full_url, code, "redirect refused", headers, fp)

    return _NoRedirect


def _get_opener():
    """The module-wide opener that refuses redirects, built lazily on first use."""
    global _OPENER
    if _OPENER is None:
        import urllib.request
        _OPENER = urllib.request.build_opener(_no_redirect_handler_class())
    return _OPENER


def fetch_usage(token: str, opener=None) -> dict:
    """GET the OAuth usage endpoint Claude Code's /usage screen uses."""
    import urllib.request
    if opener is None:
        opener = _get_opener().open
    req = urllib.request.Request(USAGE_URL, headers={
        "Authorization": f"Bearer {token}",
        "anthropic-beta": "oauth-2025-04-20",
        "Accept": "application/json",
        "User-Agent": "claude-statusline/1.0",
    })
    with opener(req, timeout=FETCH_TIMEOUT_S) as resp:
        return json.loads(resp.read().decode("utf-8"))


def run_fetch(now: float | None = None) -> dict:
    """Refresh usage.json. Keeps the previous `limits` on failure so stale values can still render."""
    import urllib.error
    now = time.time() if now is None else now
    if not load_config().get("usageFetch", True):
        return {"fetched_at": now, "ok": False, "error": "usage fetch disabled", "limits": None}
    cache_file, lock = cache_paths()
    previous = load_json(cache_file)
    # Stamped with the account these figures belong to. HASHED, not the raw email: the
    # comparison only ever needs equality, every other store here keeps a hash for the same
    # reason, and the README promises no address is written to disk. See pick_limits().
    entry = {"fetched_at": now, "ok": False, "error": None, "limits": previous.get("limits"),
             "account": account_hash(read_email())}
    token = read_token()
    if not token:
        entry["error"] = "no token"
    else:
        try:
            entry["limits"] = normalize_usage(fetch_usage(token))
            entry["ok"] = True
        except urllib.error.HTTPError as exc:
            entry["error"] = f"http {exc.code}"
            if exc.code == 429:
                entry["refusals"] = int(previous.get("refusals") or 0) + 1
                retry = (exc.headers or {}).get("Retry-After") if hasattr(exc, "headers") else None
                try:
                    entry["retry_after"] = now + float(retry) if retry else None
                except (TypeError, ValueError):
                    entry["retry_after"] = None
        except Exception as exc:  # URLError, timeout, bad JSON — the fetcher must never crash
            entry["error"] = type(exc).__name__
    write_cache(cache_file, entry)
    try:
        lock.unlink()
    except OSError:
        pass
    return entry


# ── render ───────────────────────────────────────────────────────────────────
def _char_width(ch: str) -> int:
    """Display columns for one character: 0 for a combining mark, 2 for East-Asian Wide/
    Fullwidth, else 1 — including 'Ambiguous' width glyphs (│ · ▰ ⚙ ✓ …), which most terminals
    render as a single column, matching Squirrel's own glyph set."""
    if unicodedata.combining(ch):
        return 0
    return 2 if unicodedata.east_asian_width(ch) in ("W", "F") else 1


def visible_len(text: str) -> int:
    """Displayed width — colour escapes AND OSC sequences stripped, then summed by display
    column per character (see _char_width) rather than a flat character count.

    OSC matters as much as SGR here: an OSC-8 hyperlink carries its whole URL inside the
    escape, so a segment with a `link` used to read ~35 columns wider than it renders."""
    return sum(_char_width(ch) for ch in _OSC_RE.sub("", _ANSI_RE.sub("", text)))


def pane_reserve(config: dict | None = None) -> int:
    """Columns held back at the right edge. One by default — enough that a glyph mismeasured
    by a single column costs a blank rather than an ellipsis. It is a tunable because only the
    user can see whether their host leaves the last column alone: if a line still ends in `…`
    with nothing missing from it, the pane is narrower than COLUMNS says and this is the dial
    that says so."""
    return tunable(config, "paneReserveColumns")


def host_len(text: str) -> int:
    """The width CLAUDE CODE measures for this line — which is not the width the terminal
    draws (visible_len).

    Claude Code truncates each status line with Ink's `wrap="truncate"`, measured by
    `Bun.stringWidth(text, {ambiguousIsNarrow: true})`. That agrees with visible_len() glyph
    for glyph — bars, box-drawing, the ambiguous-width set, the emoji in a reminder — with ONE
    exception: it strips SGR colour but NOT OSC, so the whole URL inside an OSC-8 hyperlink is
    counted as if it were text on screen. A clickable reminder carries a 43-character token,
    so one link reads ~86 columns wider to the host than it draws: the line is silently
    truncated mid-word and an ellipsis replaces the tail (the bug the user photographed on
    four panes).

    So both measures are needed and neither is wrong: visible_len() for what the user sees,
    host_len() for the budget that decides what survives.

    ESC itself occupies nothing in either measure; what is left after the SGR sequences and
    the escape characters go is exactly what the host counts."""
    return sum(_char_width(ch) for ch in _ANSI_RE.sub("", text).replace("\033", ""))


def terminal_width(default: int = 80) -> int:
    """Pane width: COLUMNS (Claude Code sets it per refresh), else the OS, else `default`."""
    try:
        return int(os.environ["COLUMNS"])
    except (KeyError, ValueError):
        pass
    try:
        return shutil.get_terminal_size((default, 20)).columns
    except OSError:
        return default


def context_segment_runs(ctx: dict, opts: dict, config: dict | None = None) -> list:
    """'ctx ▰▰▰▰▱▱▱▱▱▱ 42% (84k/200k)' as styled runs — bars/tokens per opts. The bar and
    percentage carry the usage-threshold colour; the token counts are dimmed separately."""
    pct = float(ctx.get("used_percentage") or 0)
    body = f"{bar(pct, config, opts.get('bar_cells'))} {pct:.0f}%" if opts["bars"] else f"{pct:.0f}%"
    runs = [("ctx ", None), (body, pct_color(pct, config))]
    if opts["tokens"]:
        usage = ctx.get("current_usage") or {}
        used = sum(int(usage.get(k) or 0)
                   for k in ("input_tokens", "cache_creation_input_tokens", "cache_read_input_tokens"))
        size = int(ctx.get("context_window_size") or 0)
        if size:
            runs.append((f" ({fmt_tokens(used)}/{fmt_tokens(size)})", DIM))
    return runs


def context_segment(ctx: dict, opts: dict, config: dict | None = None) -> str:
    """String form of context_segment_runs(), for callers that want the finished fragment."""
    return join_runs(context_segment_runs(ctx, opts, config))


def usage_segment_runs(name: str, bucket: dict | None, stale: bool, opts: dict,
                       config: dict | None = None) -> list:
    """'5h ▰▰▱▱▱▱▱▱▱▱ 17%' as styled runs — bar per opts, '~17%' when stale, 'n/a' when missing."""
    if bucket is None:
        return [(f"{name} n/a", None)]
    pct = bucket["pct"]
    mark = "~" if stale else ""
    body = f"{bar(pct, config, opts.get('bar_cells'))} {mark}{pct:.0f}%" if opts["bars"] else f"{mark}{pct:.0f}%"
    return [(f"{name} ", None), (body, pct_color(pct, config))]


def usage_segment(name: str, bucket: dict | None, stale: bool, opts: dict, config: dict | None = None) -> str:
    """String form of usage_segment_runs()."""
    return join_runs(usage_segment_runs(name, bucket, stale, opts, config))


def session_usage_segment_runs(name: str, bucket: dict | None, stale: bool, opts: dict, status: dict,
                           email: str | None, now: float, config: dict | None = None) -> list:
    """The account-wide 5-hour bucket, like usage_segment(), but — when the data genuinely
    supports it — folds in this session's own contribution as a composite
    '<contribution>/<total>%' instead of the plain '<total>%', e.g.
    '5h ▰▰▰▰▰▰▰▰▰▱ 19/90%': the account has used 90% of the window, and this session is
    responsible for 19 of those points.

    Task 12: `<contribution>` is this session's own GROUND-TRUTH attributed percentage points
    (session_contribution()'s `points`) — read straight off the account's own utilisation
    history, not `fraction * total_pct` any more; there is no multiplication left to do, since
    the attributed figure already IS expressed in the same percentage-point units as `<total>`.

    `<total>` keeps the existing usage-threshold colour (pct_color(), warnPercent/
    dangerPercent — unchanged). `<contribution>` is coloured on its OWN scale — this
    session's share of every live session's attributed points (share_pct_color(),
    shareWarnPercent/shareDangerPercent) — because it answers a different question than
    `<total>` does ('am I the problem?', not 'how full is the window?'): a session
    responsible for most of a window's burn should read red even while the window's total
    usage is still comfortably green.

    Controlled by `sessionContribution` (default on — session_contribution_enabled()). Falls
    back to the plain '<total>%' — exactly what this segment has always rendered — whenever
    the composite's inputs aren't all genuinely known: `sessionContribution` off, the bucket
    itself missing ('n/a'), this session not (yet) live, no other live session to attribute
    against, or this session's own attribution is entirely unknown (session_contribution()
    returns None in all those cases — Task 12's rule 4: no attributed data means no composite,
    never a guessed or invented figure). The stale marker (`~`) behaves exactly as before on
    the plain path; on the composite path it (and this session's own 'partial' flag — "not
    fully exact", see session_contribution()) marks the contribution figure, since both
    reflect the freshness/completeness of what's being attributed, while the account total is
    always a complete, independently-fresh reading of its own staleness.
    """
    if bucket is None:
        return [(f"{name} n/a", None)]
    config = config or {}
    total_pct = bucket["pct"]
    contribution = (session_contribution(status, email, now, config)
                    if session_contribution_enabled(config) else None)
    if contribution is None:
        mark = "~" if stale else ""
        body = (f"{bar(total_pct, config, opts.get('bar_cells'))} {mark}{total_pct:.0f}%"
                if opts["bars"] else f"{mark}{total_pct:.0f}%")
        return [(f"{name} ", None), (body, pct_color(total_pct, config))]
    contribution_pp, share_pct, partial = (contribution["points"], contribution["share_pct"],
                                            contribution["partial"])
    mark = "~" if (stale or partial) else ""
    runs = [(f"{name} ", None)]
    if opts["bars"]:
        # The bar always reflects "how full is the window?" — the TOTAL's colour, exactly as
        # in the plain path above. Only the two numbers get their own (possibly different)
        # colours; the bar must never be left uncoloured just because a composite is showing.
        runs.append((bar(total_pct, config, opts.get("bar_cells")), pct_color(total_pct, config)))
        runs.append((" ", None))
    # '~1pp/5%', not '~1/5%'. The unit is what was confusing: both numbers are percentages of
    # the same window, so '1/5' reads as the fraction one-fifth when it means "one of the five
    # points used is mine". The author of this plugin misread his own bar, which is as clear a
    # verdict as a format ever gets. 'pp' (percentage points) is the unit the /squirrel-sessions
    # table already prints, so the bar and the table now say the same word for the same thing.
    runs.append((f"{mark}{contribution_pp:.0f}pp", share_pct_color(share_pct, config)))
    runs.append(("/", None))
    runs.append((f"{total_pct:.0f}%", pct_color(total_pct, config)))
    return runs


def session_usage_segment(name: str, bucket: dict | None, stale: bool, opts: dict, status: dict,
                           email: str | None, now: float, config: dict | None = None) -> str:
    """String form of session_usage_segment_runs()."""
    return join_runs(session_usage_segment_runs(name, bucket, stale, opts, status, email, now, config))


def stdin_bucket(raw) -> dict | None:
    """Claude Code's rate_limits.{five_hour,seven_day} → bucket. resets_at is normally epoch seconds;
    an ISO string is tolerated (parsed) so a payload change can't collapse the line to the error state."""
    if not isinstance(raw, dict):
        return None
    resets = raw.get("resets_at")
    if isinstance(resets, str):
        resets = parse_iso(resets)
    return _bucket(raw.get("used_percentage"), resets)


def _bucket_expiry(bucket) -> float | None:
    return float(bucket["resets_at"]) if bucket and bucket.get("resets_at") else None


def _fresher_bucket(from_stdin, from_api, now: float, cache: dict):
    """The more trustworthy of the two readings of one bucket, or None when neither is known.

    Freshness, not identity. A stdin bucket whose own window has already passed is describing
    a window that has rolled — the Task 28 failure — and loses to an API bucket whose window
    is still open. Otherwise stdin wins, because it arrived with this render and the API
    cache did not."""
    if from_stdin is None:
        return from_api
    if from_api is None:
        return from_stdin
    stdin_open = (_bucket_expiry(from_stdin) or 0) > now
    api_open = (_bucket_expiry(from_api) or 0) > now
    if not stdin_open and api_open:
        return from_api
    return from_stdin


def cache_is_this_account(cache: dict, email: str | None) -> bool | None:
    """Does this cache belong to the account we are rendering for? None when unknowable.

    Three answers, and the three-ness is the point:

      True  — stamped with our account. Trust it; if it is old, that is STALE and the bar
              marks it `~`, which is honest: an old number of ours.
      False — stamped with a different account, or not stamped at all. Not stale, WRONG. The
              user reported this as "large lags" after `/login`: the bar kept showing the
              previous account's limits as fact until the TTL ran out. An unstamped cache is
              unknown provenance and gets the same answer — it self-heals in one fetch, and
              nobody sees another account's numbers in the meantime.
      None  — we cannot tell who we are (not logged in, credentials unreadable). Unknown is
              not "different": discarding a good cache because we could not read a file would
              invent a reason to show less. Same rule as `nudged`.
    """
    if not email:
        return None
    stamp = cache.get("account")
    if not isinstance(stamp, str) or not stamp:
        return False
    return stamp == account_hash(email)


def pick_limits(status: dict, cache: dict, now: float, config: dict | None = None,
                 email: str | None = None) -> tuple[dict, bool, str]:
    """→ (limits, stale, source): merges the API cache with stdin rate_limits, per bucket.

    Each of session / weekly_all / scoped takes the API's value when the API provided it, else
    falls back to stdin `rate_limits`. `source` is "api" only when the API contributed at least
    one bucket (so an unrecognised/empty payload shape degrades to the stdin baseline instead of
    to worse-than-baseline `n/a`); staleness only applies when `source == "api"`.
    """
    api = cache.get("limits")
    api = api if isinstance(api, dict) else {}
    if cache_is_this_account(cache, email) is False:
        # Deliberately dropped rather than marked. The fall-back below is the session's own
        # `rate_limits`, which Claude Code supplies for the account currently logged in — so
        # a mismatch degrades to current-account data, never to a foreign number and never
        # to a needlessly blank bar.
        api = {}
    rl = status.get("rate_limits") or {}
    # STDIN IS PRIMARY for the two headline buckets. Claude Code hands them to us free on
    # every render, for the account logged in now; the API is a cache that can be minutes
    # old, refused, or from before a /login. The old `api or stdin` preferred it by identity
    # and showed a cached 51% while 69% sat in the same payload.
    #
    # But not "stdin always wins" either: Task 28 established that `rate_limits` can go stale
    # inside a long-lived session, which is what manufactured the `183/3%` sawtooth. Neither
    # source is authoritative BY IDENTITY — the fresher one is, per bucket, and unknown never
    # beats known.
    session = _fresher_bucket(stdin_bucket(rl.get("five_hour")), api.get("session"), now, cache)
    weekly_all = _fresher_bucket(stdin_bucket(rl.get("seven_day")), api.get("weekly_all"), now, cache)
    # The per-model buckets are the ONLY thing the API is still needed for — and the reason
    # this plugin exists. stdin has no equivalent.
    scoped = api.get("scoped") or []
    source = "api" if scoped else "stdin"
    stale = bool(scoped) and ((not cache.get("ok"))
                              or (now - float(cache.get("fetched_at") or 0)
                                  > tunable(config, "staleAfterSeconds")))
    return {"session": session, "weekly_all": weekly_all, "scoped": scoped}, stale, source


def _chip_inner(text: str, width: int) -> str:
    """Pad chip text: width <= 0 (the shipped default) → natural — exactly one space each side;
    a positive width centres the text to that inner width instead, so chips render as
    equal-size blocks (the old default). Never truncates."""
    if width <= 0:
        return f" {text} "
    return text.center(width) if len(text) < width else text


_UNREAD = object()      # "caller passed nothing", distinct from "there is no state file"


def state_segment_runs(status: dict, now: float, config: dict | None = None,
                        data=_UNREAD) -> list:
    """Coloured activity-state chip for line 2, as styled runs. A waiting state (idle / needs-you) that has lasted
    longer than `alarmAfterSeconds` renders as a loud alarm with the elapsed time. None when no fresh state."""
    # `data is _UNREAD` — not `data is None` — because None is a legitimate answer meaning
    # "no state file", and treating it as "not provided" would read the file a second time.
    data = read_state(status.get("session_id"), now) if data is _UNREAD else data
    if not data:
        return []
    # The hook events cannot see a background agent still working — see refine_state(). This
    # is why the bar used to read `IDLE 1h42m` with agents running.
    scan = scan_transcript(transcript_path(status))
    state = refine_state(data.get("state"), scan)
    age = now - float(data.get("ts") or now)
    subagents = int(data.get("subagents") or 0)
    if scan and state != data.get("state"):
        # Refined. Both the age and the count now come from the transcript: the hook's
        # timestamp belongs to the event this just overruled, and judging the new state by it
        # would age an active session out on the staleness check below within minutes.
        age = max(0.0, now - float(scan.get("observed") or now))
        subagents = scan["pending_agents"] or subagents
    if state in ACTIVE_STATES and age > tunable(config, "activeMaxAgeSeconds"):
        return []
    config = config or {}
    alarm_after = tunable(config, "alarmAfterSeconds")
    alarm_states = config.get("alarmStates") or list(ALARM_DEFAULT_STATES)
    chip_width = tunable(config, "chipWidth")
    if alarm_after > 0 and state in alarm_states and age >= alarm_after:
        # The chip is the one fragment that reaches the bar already rendered (it carries its
        # own alarm/background sequences), so the seam in _produce() cannot sanitise it — see
        # seg_chip(). Its only externally-influenced text is the state name, which arrives via
        # the --state hook argument, so it is sanitised right here at its source.
        label = ALARM_LABELS.get(state, str(state).upper())
        inner = f"‼ {label} {fmt_relative(age)} ‼"
        return [(f"[{_chip_inner(inner, chip_width)}]", ALARM_SEQ)]
    if state == "subagents":
        n = subagents
        inner = f"⧗ {n} subagent{'' if n == 1 else 's'}"
        return [(f"[{_chip_inner(inner, chip_width)}]", CYAN)]
    style = STATE_STYLES.get(state)
    if not style:
        return []
    text, color = style
    return [(f"[{_chip_inner(text, chip_width)}]", color)]


def state_segment(status: dict, now: float, config: dict | None = None,
                   data=_UNREAD) -> str | None:
    """String form of state_segment_runs()."""
    return join_runs(state_segment_runs(status, now, config, data)) or None


def _shipped_phases() -> list[dict]:
    """The packaged idlePhases ladder from defaults/config.json — the source of truth for
    colours and timings. Falls back to FALLBACK_IDLE_PHASES only if that file is unreadable."""
    packaged = packaged_defaults().get("idlePhases")
    return packaged if isinstance(packaged, list) else FALLBACK_IDLE_PHASES


def _phase_after(value) -> int | float | None:
    """Normalize a phase's `after` field to a non-negative number; None when invalid."""
    if isinstance(value, bool) or not isinstance(value, (int, float)) or value < 0:
        return None
    return int(value) if float(value).is_integer() else value


def _apply_phase_overlay(baseline: dict, match_after, entry: dict) -> None:
    """Overlay `entry`'s named fields onto the phase currently keyed `match_after` (adding a new
    phase if none exists there), moving it to `entry['after']` when that differs from
    `match_after`; `entry['remove']` drops the phase instead of reinserting it. Every field the
    entry doesn't mention is left exactly as it was — this is what makes a --set-phase/
    --add-phase/--remove-phase edit surgical rather than a wholesale rewrite."""
    current = baseline.pop(match_after, None)
    if entry.get("remove"):
        return
    merged = dict(current) if current else {}
    for key, value in entry.items():
        if key != "remove":
            merged[key] = value
    after = merged.get("after", match_after)
    merged["after"] = after
    baseline[after] = merged


def _legacy_seconds(config: dict, key: str) -> int | None:
    """Parse a legacy seconds value from `config[key]`; None when absent or not a valid number."""
    if key not in config:
        return None
    try:
        return int(config[key])
    except (TypeError, ValueError):
        return None


def _legacy_phase_overlays(config: dict, order: list) -> list[tuple]:
    """idleBackgroundAfterSeconds/alarmAfterSeconds are long-standing tunables — always present,
    defaulting to the same 30s/120s the shipped ladder already uses — that keep driving the
    *timing* of the ladder's first two slots exactly as they did before the ladder existed, so
    /squirrel-bg-after and /squirrel-alarm keep working unchanged. idleBackground/
    idleBackgroundUrgent (plain colour keys, no longer shipped, present only when a config still
    sets them) drive those same two slots' `bg` when set. Anything from the third slot on lives
    purely in `idlePhases` data, untouched by any tunable."""
    overlays = []
    if order:
        entry = {"after": tunable(config, "idleBackgroundAfterSeconds")}
        if "idleBackground" in config:
            entry["bg"] = config["idleBackground"]
        overlays.append((order[0], entry))
    if len(order) > 1:
        alarm_after = tunable(config, "alarmAfterSeconds")
        if alarm_after <= 0:
            overlays.append((order[1], {"remove": True}))
        else:
            entry = {"after": alarm_after}
            if "idleBackgroundUrgent" in config:
                entry["bg"] = config["idleBackgroundUrgent"]
            overlays.append((order[1], entry))
    return overlays


def load_phases(config: dict | None = None) -> list[dict]:
    """The effective idle-phase ladder: the shipped defaults, deep-merged per-phase (keyed by
    `after`) with the caller's `idlePhases` overlay, then with the legacy idleBackground(...)/
    idleBackgroundUrgent keys (see _legacy_phase_overlays()). A user phase overlays only the
    fields it names — unmentioned fields/phases keep the shipped value; `remove: true` drops a
    phase (and blocks a legacy overlay from resurrecting it); a new `after` adds a phase.
    Invalid entries (not a dict, missing/negative `after`) are skipped, never fatal. Sorted
    ascending by `after`."""
    config = config or {}
    baseline: dict = {}
    order: list = []
    for p in _shipped_phases():
        after = _phase_after(p.get("after")) if isinstance(p, dict) else None
        if after is not None:
            baseline[after] = {**p, "after": after}
            order.append(after)
    removed: set = set()
    overlay_list = config.get("idlePhases")
    if isinstance(overlay_list, list):
        for entry in overlay_list:
            if not isinstance(entry, dict):
                continue
            after = _phase_after(entry.get("after"))
            if after is None:
                continue
            if entry.get("remove"):
                removed.add(after)
            _apply_phase_overlay(baseline, after, {**entry, "after": after})
    for match_after, entry in _legacy_phase_overlays(config, order):
        if match_after in removed:
            continue
        _apply_phase_overlay(baseline, match_after, entry)
    return sorted(baseline.values(), key=lambda ph: ph["after"])


# ── phase alerts: reaching a user who is not looking at this monitor ─────────
#
# The bar can only shout at a screen someone is looking at. The user runs three monitors, so
# the loudest possible status line is still invisible half the time — and they do not want a
# sound. So a phase in the ladder may carry an ACTION as well as a colour: the same data, the
# same delays, one more field.
#
#     {"after": 300, "notify": true}                    a desktop notification at 5 minutes
#     {"after": 900, "run": "curl -d ... ntfy.sh/..."}  anything else, at 15
#
# Three rules, each of which is a bug this design is avoiding:
#   * ONCE per waiting episode. The dedupe key is (this session, the timestamp the wait began,
#     the phase) — so a notification fires when a phase is entered and never again for that
#     wait, however many times the bar re-renders (every few seconds, all day).
#   * NEVER on the render path. The alert is spawned detached, exactly like the usage fetch.
#     A notifier that hangs must not hang the bar.
#   * `run` is a user command, so it obeys the same switch custom segments do
#     (`customCommands`). A config that can run commands is a config worth being explicit about.
PHASE_ALERT_FIELDS = ("notify", "run")


def phase_alert_path(session_id: str | None) -> Path:
    safe = re.sub(r"[^A-Za-z0-9_-]", "-", str(session_id or "unknown"))[:64] or "unknown"
    return shared_dir() / "alerts" / f"{safe}.json"


def phase_alerts_enabled(config: dict | None) -> bool:
    """`phaseAlerts`, with an env kill-switch on top.

    SQUIRREL_NO_ALERTS exists because a render is not always a render: the test suite and the
    README's image generator both call render() on sessions that have been "waiting" for
    hours, and without this every full test run would fire real desktop notifications at
    whoever is at the keyboard. Config alone could not cover it — those renders deliberately
    use the SHIPPED config, notification phase and all."""
    if os.environ.get("SQUIRREL_NO_ALERTS"):
        return False
    return bool((config or {}).get("phaseAlerts", True))


def alert_when_focused(config: dict | None) -> bool:
    """Whether to notify even when the terminal is the focused window. Default False: if you
    are already looking at it, the bar has done its job — see foreground_window_title()."""
    return bool((config or {}).get("alertWhenFocused", False))


def _phase_alert_fired(record: dict, episode: float, after: int) -> bool:
    if not isinstance(record, dict):
        return False
    # Exact equality, not a tolerance: the episode IS the timestamp the wait began, written
    # and read back as the same float. A one-second tolerance here silently treated "they came
    # back and left again" as the same wait, which is precisely the case that must notify.
    if float(record.get("episode") or 0) != episode:
        return False
    return after in [int(x) for x in (record.get("fired") or []) if isinstance(x, (int, float))]


def maybe_fire_phase_alert(ctx: dict) -> None:
    """Fire the winning phase's action, at most once per waiting episode. Best-effort in every
    sense: nothing here may raise, block, or delay the bar."""
    config = ctx["config"]
    if not phase_alerts_enabled(config):
        return
    age = _metric_idle_secs(ctx)            # None unless genuinely waiting in a painted state
    if age is None:
        return
    phase = phase_for(age, config)
    if not phase or not any(phase.get(field) for field in PHASE_ALERT_FIELDS):
        return
    session_id = ctx["status"].get("session_id")
    state = _ctx_state(ctx) or {}
    episode = float(state.get("ts") or 0)
    if not episode:
        return
    path = phase_alert_path(session_id)
    record = load_json(path)
    after = int(phase["after"])
    if _phase_alert_fired(record, episode, after):
        return
    same_episode = float(record.get("episode") or 0) == episode
    fired = ([int(x) for x in (record.get("fired") or []) if isinstance(x, (int, float))]
             if same_episode else [])
    # Written BEFORE the spawn: a notifier that fails is one missed notification, while a
    # record written after a crash would re-fire on every render for the rest of the wait.
    write_cache(path, {"episode": episode, "fired": sorted(set(fired + [after])), "at": ctx["now"]})
    _spawn_phase_alert(phase, ctx)


def _alert_text(phase: dict, ctx: dict) -> tuple:
    """(title, body) for the built-in notification. Says what is waiting and where, because a
    toast with no repo in it is useless to someone running four sessions."""
    age = _metric_idle_secs(ctx) or 0
    repo = repo_name(ctx["status"]) or ""
    state = refine_state((_ctx_state(ctx) or {}).get("state"), _ctx_transcript(ctx))
    what = "needs your permission" if state == "permission" else "has been waiting"
    body = f"Claude {what} {fmt_relative(age)}"
    return (f"\U0001f43f\ufe0f {repo}" if repo else "\U0001f43f\ufe0f Squirrel", body)


def _spawn_phase_alert(phase: dict, ctx: dict) -> None:
    import subprocess
    quiet = dict(stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        if phase.get("notify"):
            title, body = _alert_text(phase, ctx)
            if isinstance(phase.get("notify"), str):
                body = sanitise(phase["notify"])
            ws = ctx["status"].get("workspace") or {}
            hint = repo_name(ctx["status"]) or Path(ws.get("current_dir") or ".").name
            subprocess.Popen(
                [sys.executable, os.path.abspath(__file__), "--notify", title, body, hint],
                **quiet, **detach_kwargs(subprocess))
        command = phase.get("run")
        if command and isinstance(command, str) and custom_commands_enabled(ctx["config"]):
            subprocess.Popen(command, shell=True, **quiet, **detach_kwargs(subprocess))
    except Exception:
        pass            # an alert that cannot start must never disturb the render


def foreground_window_title(timeout: float = 3.0) -> str | None:
    """The title of the window that currently has focus, or None when we cannot tell.

    Focus is a PROXY for "you are looking at it", not a measurement: with three monitors the
    focused window can be on a screen you are not looking at. So None — and every platform we
    have no probe for — means NOTIFY, never suppress. The cost of a redundant toast is a
    glance; the cost of a suppressed one is the thing this feature exists to prevent.

    Runs only inside the detached notifier, never on the render path: the Windows probe costs
    about half a second."""
    import subprocess
    try:
        if _is_wsl():
            script = ('Add-Type @"\nusing System;using System.Runtime.InteropServices;'
                      'using System.Text;\npublic class W {\n'
                      ' [DllImport("user32.dll")] public static extern IntPtr GetForegroundWindow();\n'
                      ' [DllImport("user32.dll")] public static extern int GetWindowText(IntPtr h, StringBuilder s, int n);\n'
                      '}\n"@\n$h=[W]::GetForegroundWindow(); $sb=New-Object System.Text.StringBuilder 512\n'
                      '[void][W]::GetWindowText($h,$sb,512); $sb.ToString()')
            out = subprocess.run(_powershell_argv(script), capture_output=True, text=True,
                                 timeout=timeout)
            return (out.stdout or "").strip() or None
        if sys.platform == "darwin":
            out = subprocess.run(
                ["osascript", "-e",
                 'tell application "System Events" to get name of first process whose frontmost is true'],
                capture_output=True, text=True, timeout=timeout)
            return (out.stdout or "").strip() or None
        if shutil.which("xdotool"):
            out = subprocess.run(["xdotool", "getactivewindow", "getwindowname"],
                                 capture_output=True, text=True, timeout=timeout)
            return (out.stdout or "").strip() or None
    except Exception:
        return None
    return None


def _is_wsl() -> bool:
    try:
        return "microsoft" in Path("/proc/version").read_text().lower()
    except OSError:
        return False


def _powershell_argv(script: str) -> list:
    """powershell.exe with the script passed as -EncodedCommand: base64 UTF-16LE, which is the
    one form that survives every layer of quoting between here and Windows."""
    import base64
    encoded = base64.b64encode(script.encode("utf-16-le")).decode("ascii")
    exe = shutil.which("powershell.exe") or "/mnt/c/Windows/System32/WindowsPowerShell/v1.0/powershell.exe"
    return [exe, "-NoProfile", "-NonInteractive", "-EncodedCommand", encoded]


def desktop_notify(title: str, body: str, hint: str = "", config: dict | None = None) -> str:
    """Show one desktop notification. Returns a short report (what was used, or why nothing
    was) — `/squirrel-alerts test` prints it, which is how a user finds out whether their
    machine can do this at all before relying on it."""
    config = config or {}
    title, body = sanitise(str(title))[:120], sanitise(str(body))[:400]
    if not alert_when_focused(config) and hint:
        focused = foreground_window_title()
        if focused and hint.lower() in focused.lower():
            return f"suppressed: you are looking at it ({focused})"
    import subprocess
    if _is_wsl() or sys.platform.startswith("win"):
        script = _TOAST_PS.format(title=_ps_quote(title), body=_ps_quote(body))
        try:
            out = subprocess.run(_powershell_argv(script), capture_output=True, text=True,
                                 timeout=30)
            if out.returncode == 0:
                return "sent: Windows toast"
            return f"powershell failed: {(out.stderr or '').strip()[:200]}"
        except Exception as exc:
            return f"powershell failed: {type(exc).__name__}"
    for argv, label in (([shutil.which("notify-send"), title, body], "notify-send"),
                        ([shutil.which("terminal-notifier"), "-title", title, "-message", body],
                         "terminal-notifier")):
        if not argv[0]:
            continue
        try:
            out = subprocess.run(argv, capture_output=True, text=True, timeout=15)
            # notify-send EXITS 0 with a GDBus error on its stderr when no notification daemon
            # is running — which is exactly the case on this developer's WSL box. Trusting the
            # exit status alone would have reported success for a notification nobody saw.
            if out.returncode == 0 and not (out.stderr or "").strip():
                return f"sent: {label}"
        except Exception:
            continue
    if sys.platform == "darwin":
        try:
            subprocess.run(["osascript", "-e",
                            f'display notification {_as_applescript(body)} with title {_as_applescript(title)}'],
                           capture_output=True, timeout=15)
            return "sent: osascript"
        except Exception:
            pass
    return "no notifier found on this machine (tried powershell/notify-send/terminal-notifier/osascript)"


_TOAST_PS = (
    '[Windows.UI.Notifications.ToastNotificationManager, Windows.UI.Notifications, '
    'ContentType=WindowsRuntime] > $null\n'
    '[Windows.Data.Xml.Dom.XmlDocument, Windows.Data.Xml.Dom.XmlDocument, '
    'ContentType=WindowsRuntime] > $null\n'
    '$AppId = \'{{1AC14E77-02E7-4E5D-B744-2EB1AE5198B7}}\\WindowsPowerShell\\v1.0\\powershell.exe\'\n'
    '$doc = New-Object Windows.Data.Xml.Dom.XmlDocument\n'
    '$doc.LoadXml(\'<toast><visual><binding template="ToastGeneric"><text>{title}</text>'
    '<text>{body}</text></binding></visual></toast>\')\n'
    '$toast = New-Object Windows.UI.Notifications.ToastNotification $doc\n'
    '[Windows.UI.Notifications.ToastNotificationManager]::CreateToastNotifier($AppId).Show($toast)')


def _ps_quote(text: str) -> str:
    """Safe inside the single-quoted PowerShell string AND inside the toast's XML."""
    return (text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
                .replace("'", "&apos;").replace('"', "&quot;"))


def _as_applescript(text: str) -> str:
    return '"' + text.replace("\\", "\\\\").replace('"', '\\"') + '"'


def phase_for(age: float, config: dict | None = None) -> dict | None:
    """The winning phase for a wait of `age` seconds: the last (highest-`after`) phase whose
    `after` has strictly elapsed; None when the wait hasn't reached the first phase yet."""
    winner = None
    for phase in load_phases(config):
        if age > phase["after"]:
            winner = phase
        else:
            break
    return winner


def idle_background(status: dict, now: float, config: dict | None = None) -> tuple:
    """(background escape, text escape) for the whole bar, from the winning BAR RULE — the
    idle-phase ladder plus whatever the user has added (see bar_rules()); (None, None) when
    nothing should be painted.

    The name is historical: this used to be only about idle time, and the idle ladder is
    still the shipped default, but the mechanism is now general — a rule can paint the bar on
    any published metric (`weekly.pct >= 90`, `git.dirty`, `cost.usd > 5`). A rule's `bold`
    makes its `text` colour bold via bold_code(); with no `text` colour resolved, `bold` has
    nothing to apply to. `idleBackground` explicitly off/invalid, or
    idleBackgroundAfterSeconds <= 0, still disable the idle ladder outright — but not a user's
    own rules, which they added on purpose. Only states in `idleBackgroundStates` are ever
    painted by the ladder and ACTIVE_STATES never are; that gating lives in the `idle.secs`
    metric, which is None (not 0) whenever the session is not waiting in a painted state."""
    config = config or {}
    ctx = _ctx_for(status, {}, False, "stdin", None, config, now, None, None)
    return rule_colors(winning_rule(ctx, config))


# ── bar-rule conditions: metrics and a deliberately tiny grammar ─────────────
# Rules are DATA from the user's own config, and they are never evaluated as code. There is
# no eval(), no exec(), no lambda from a string. A condition is exactly one of:
#
#     <metric> <op> <literal>        e.g.  "weekly.pct >= 80"   "git.branch == main"
#     <metric>                       bare truthiness, e.g.  "git.dirty"
#
# with <op> one of >= > <= < == != . Anything else — an unknown metric, a missing operand, a
# third clause, an operator we do not implement — is a parse error: the rule is IGNORED and
# reported by --show-rules, never applied and never fatal. A status line that crashes on a
# typo is worse than one that quietly does nothing.
#
# A metric whose value is unknown (None — no data yet, segment off, not a git repo) makes
# EVERY comparison false, `!=` included. "Unknown" is not "different from"; treating it as a
# match would paint the bar on missing data, which is the failure mode this whole project has
# been bitten by three times.
RULE_OPS = {
    ">=": lambda a, b: a >= b,
    "<=": lambda a, b: a <= b,
    "==": lambda a, b: a == b,
    "!=": lambda a, b: a != b,
    ">": lambda a, b: a > b,
    "<": lambda a, b: a < b,
}
# Longest first, so ">=" is never mis-split as ">".
_RULE_OP_RE = re.compile(r"\s*(" + "|".join(re.escape(o) for o in
                                            sorted(RULE_OPS, key=len, reverse=True)) + r")\s*")


def _metric_git(ctx: dict, field: str):
    ws = ctx["status"].get("workspace") or {}
    cwd = ws.get("current_dir") or ws.get("git_worktree") or ws.get("project_dir")
    if not cwd:
        return None
    info = _git_status(cwd, ctx["config"], ctx["now"])
    return info.get(field) if info else None


def _metric_bucket(ctx: dict, name: str):
    bucket = ctx["limits"].get(name)
    return bucket["pct"] if bucket else None


# ── activity read from the session transcript ────────────────────────────────
# Only the tail is ever read. The largest transcript on the developer's machine was 78 MB
# and the render runs every few seconds; reading one whole would be a per-render tax measured
# in seconds. 256 KB is far more than enough: a tool call that is still outstanding is by
# definition the last thing written, because nothing else is appended while we wait for it.
TRANSCRIPT_TAIL_BYTES = 256 * 1024
AGENT_TOOL_NAMES = ("Task", "Agent")
# The mark a nudge leaves. Causality is not inferable — nothing records WHY a session woke —
# but a nudge arrives as text and the receiving session's transcript records that text, so a
# recognisable suffix makes it a fact the receiver can read about itself. Short on purpose:
# the user sees this in their own session, so it is a suffix and never a banner. It must
# survive sanitise() unchanged or it could never be read back.
NUDGE_MARKER = "[squirrel]"
_TRANSCRIPT_CACHE: dict = {}


def transcript_path(status: dict):
    """This session's transcript, or None.

    `transcript_path` in the status JSON when Claude Code supplies it — always preferred, it
    is the only non-guess. Otherwise derive it the way Claude Code lays the directory out:
    `~/.claude/projects/<cwd with separators flattened>/<session id>.jsonl`. That layout is
    undocumented internals, exactly like the tasks directory, so every caller treats a miss
    as "no information" and falls back to the hook-derived state."""
    direct = status.get("transcript_path")
    if isinstance(direct, str) and direct:
        return Path(direct)
    sid = safe_session_id(status.get("session_id"))
    if not sid:
        return None
    ws = status.get("workspace") or {}
    cwd = ws.get("current_dir") or ws.get("project_dir")
    if not cwd:
        return None
    slug = re.sub(r"[^A-Za-z0-9]", "-", str(cwd))
    return config_dir() / "projects" / slug / f"{sid}.jsonl"


def _scan_transcript_uncached(path: Path) -> dict:
    """Walk the tail of one transcript and report what the session is actually doing.

    Deterministic, no model. An assistant that spoke last with nothing outstanding is waiting
    for the user; a `tool_use` with no matching `tool_result` means something is still
    running; anything else is mid-turn.

    Sidechain entries — a subagent's own turns, written into the same file — are skipped.
    They are not this session talking to the user and must not decide what the bar says; the
    main chain's own `Task` call stays outstanding for as long as the subagent runs, which is
    the signal we actually want.

    Verified against the real format before it was written, by truncating a live transcript
    immediately after a genuine call: after an `Agent` tool_use this reports waiting-tools
    with one agent, after a `Skill` tool_use waiting-tools with one tool, and four live
    sessions at rest all reported waiting-you. 1.0-2.5 ms regardless of file size."""
    size = path.stat().st_size
    with open(path, "rb") as handle:
        if size > TRANSCRIPT_TAIL_BYTES:
            handle.seek(size - TRANSCRIPT_TAIL_BYTES)
            handle.readline()       # a seek lands mid-line by definition; drop the fragment
        blob = handle.read(TRANSCRIPT_TAIL_BYTES + 1)
    results: set = set()
    uses: list = []
    last_role, last_spoke, last_user_text = None, False, ""
    for raw in blob.split(b"\n"):
        if not raw.strip():
            continue
        try:
            entry = json.loads(raw.decode("utf-8", "replace"))
        except ValueError:
            continue                # a truncated or malformed line is simply not evidence
        if not isinstance(entry, dict) or entry.get("isSidechain"):
            continue
        kind = entry.get("type")
        if kind not in ("assistant", "user"):
            continue
        message = entry.get("message")
        content = (message or {}).get("content")
        spoke = False
        for part in content if isinstance(content, list) else ():
            if not isinstance(part, dict):
                continue
            ptype = part.get("type")
            if ptype == "tool_result" and part.get("tool_use_id"):
                results.add(part["tool_use_id"])
            elif ptype == "tool_use" and part.get("id"):
                uses.append((part["id"], part.get("name") or ""))
            elif ptype == "text" and str(part.get("text") or "").strip():
                spoke = True
                if kind == "user":
                    last_user_text = str(part.get("text"))
        last_role, last_spoke = kind, spoke
    outstanding = [name for ident, name in uses if ident not in results]
    if outstanding:
        state = "waiting-tools"
    elif last_role == "assistant" and last_spoke:
        state = "waiting-you"
    else:
        state = "working"
    return {"state": state, "pending_tools": len(outstanding),
            "pending_agents": sum(1 for n in outstanding if n in AGENT_TOOL_NAMES),
            # Only the MOST RECENT user turn counts: an old nudge must not silence a watcher
            # for ever, because the user has spoken since.
            "nudged": NUDGE_MARKER in last_user_text,
            # When the transcript overrules the hooks, the hook's timestamp is the wrong
            # clock to judge the result by — this is the evidence's own age.
            "observed": path.stat().st_mtime}


def scan_transcript(path):
    """scan_transcript() with the result memoised on (path, mtime_ns, size). None when there
    is nothing to read — which every caller must treat as "no information", never as a
    finding. The memo is per-process: one render calls this a few times, and re-reading a
    256 KB tail costs ~1.5 ms, so a cross-render disk cache would cost about what it saves."""
    if path is None:
        return None
    path = Path(path)
    try:
        stat = path.stat()
        key = (str(path), stat.st_mtime_ns, stat.st_size)
        hit = _TRANSCRIPT_CACHE.get(key)
        if hit is None:
            hit = _scan_transcript_uncached(path)
            _TRANSCRIPT_CACHE.clear()       # one session per process; never grows
            _TRANSCRIPT_CACHE[key] = hit
        return hit
    except Exception:
        # Deliberately everything. This reads a file whose format is Claude Code's internal
        # business and may change without notice; the contract here is that the bar keeps
        # rendering exactly as it did before this existed, whatever comes back.
        return None


def _ctx_transcript(ctx: dict):
    """This render's transcript scan, read at most once."""
    if "transcript" not in ctx:
        try:
            ctx["transcript"] = scan_transcript(transcript_path(ctx["status"]))
        except Exception:
            ctx["transcript"] = None
    return ctx["transcript"]


def refine_state(state, scan) -> str:
    """Sharpen a hook-derived state with what the transcript actually shows.

    Deliberately ONE-DIRECTIONAL. An outstanding tool call can contradict a hook that claimed
    the session was idle — that is the user's complaint, a background agent firing its spawn
    event and then `Stop`, leaving the bar reading `IDLE 1h42m` while agents work. Nothing
    here may invent idleness the hooks did not report: if the transcript is missing, stale or
    unreadable, the answer is exactly what it was before this existed."""
    if not scan:
        return state
    if scan["state"] == "waiting-tools" and state not in ACTIVE_STATES:
        return "subagents" if scan["pending_agents"] else "working"
    return state


def _ctx_state(ctx: dict):
    """This session's activity-state record, read once per render. state_segment() and the
    idle metrics used to read the same file separately; this keeps it to one read."""
    if "state" not in ctx:
        ctx["state"] = read_state(ctx["status"].get("session_id"), ctx["now"])
    return ctx["state"]


def _metric_idle_secs(ctx: dict):
    """Seconds this session has been WAITING in a state the user paints, else None. None (not
    0) when the session is working or the state is not painted, so `idle.secs > 30` is simply
    false rather than accidentally true at 0."""
    config = ctx["config"]
    data = _ctx_state(ctx)
    if not data:
        return None
    # The SAME refinement the chip and the `state` metric get. Without it this read the raw
    # hook state, so a session with agents running still reported idle seconds and the idle
    # ladder still painted the banner — the very complaint the chip fix was for, surviving
    # one layer down. It also makes the flagship recipe a single clause: "idle for 30
    # minutes" then genuinely means "and not waiting on tools", with no boolean grammar.
    state = refine_state(data.get("state"), _ctx_transcript(ctx))
    states = config.get("idleBackgroundStates") or list(IDLE_BG_STATES_DEFAULT)
    if state in ACTIVE_STATES or state not in states:
        return None
    return ctx["now"] - float(data.get("ts") or ctx["now"])


def _metric_state(ctx: dict):
    """The hook-derived state, sharpened by the transcript when the two disagree — and when
    there is no hook state at all, the transcript's own verdict, which is strictly more than
    the nothing this used to return."""
    data = _ctx_state(ctx)
    scan = _ctx_transcript(ctx)
    if not data:
        return scan["state"] if scan else None
    return refine_state(data.get("state"), scan)


def _metric_pending(ctx: dict, field: str):
    scan = _ctx_transcript(ctx)
    return scan[field] if scan else None


def _metric_cost(ctx: dict, field: str, scale: float = 1.0):
    return float((ctx["status"].get("cost") or {}).get(field) or 0) * scale


def _metric_session_tokens(ctx: dict):
    ctxw = ctx["status"].get("context_window") or {}
    return int(ctxw.get("total_input_tokens") or 0) + int(ctxw.get("total_output_tokens") or 0)


def _metric_share_pct(ctx: dict):
    contribution = session_contribution(ctx["status"], ctx["email"], ctx["now"], ctx["config"])
    return contribution["share_pct"] if contribution else None


# Every metric a rule may name. Values are computed LAZILY and memoised per render, so naming
# a metric no rule uses costs nothing and naming `git.dirty` twice still forks git at most
# once (the same per-cwd cache the git segment relies on — see GitSubprocessCountGuardTests).
METRICS = {
    "ctx.pct":        lambda c: float((c["status"].get("context_window") or {}).get("used_percentage") or 0),
    "session.pct":    lambda c: _metric_bucket(c, "session"),
    "weekly.pct":     lambda c: _metric_bucket(c, "weekly_all"),
    "share.pct":      _metric_share_pct,
    "cost.usd":       lambda c: _metric_cost(c, "total_cost_usd"),
    "duration.min":   lambda c: _metric_cost(c, "total_duration_ms", 1 / 60000),
    "lines.added":    lambda c: int((c["status"].get("cost") or {}).get("total_lines_added") or 0),
    "lines.removed":  lambda c: int((c["status"].get("cost") or {}).get("total_lines_removed") or 0),
    "tokens.session": _metric_session_tokens,
    "git.dirty":      lambda c: _metric_git(c, "dirty"),
    "git.ahead":      lambda c: _metric_git(c, "ahead"),
    "git.branch":     lambda c: _metric_git(c, "branch"),
    "repo.name":      lambda c: repo_name(c["status"]),
    "model.name":     lambda c: (c["status"].get("model") or {}).get("display_name"),
    "effort.level":   lambda c: (c["status"].get("effort") or {}).get("level"),
    "idle.secs":      _metric_idle_secs,
    "state":          _metric_state,
    "pending.tools":  lambda c: _metric_pending(c, "pending_tools"),
    "pending.agents": lambda c: _metric_pending(c, "pending_agents"),
    "nudged":         lambda c: _metric_pending(c, "nudged"),
}


def metric_value(ctx: dict, name: str):
    """One metric's value for this render, memoised. None means "not known"."""
    cache = ctx.setdefault("metrics", {})
    if name not in cache:
        try:
            cache[name] = METRICS[name](ctx)
        except Exception:
            cache[name] = None      # a metric must never be able to break the bar
    return cache[name]


def parse_condition(text) -> tuple | str:
    """'<metric> <op> <literal>' or bare '<metric>' → (metric, op_or_None, literal_or_None);
    an error string when it does not parse. Callers must check `isinstance(..., str)`."""
    if not isinstance(text, str) or not text.strip():
        return "empty condition"
    raw = text.strip()
    match = _RULE_OP_RE.search(raw)
    if not match:
        if raw in METRICS:
            return (raw, None, None)
        return f"unknown metric '{raw}' — one of: {', '.join(sorted(METRICS))}"
    metric = raw[:match.start()].strip()
    literal = raw[match.end():].strip()
    if metric not in METRICS:
        return f"unknown metric '{metric}' — one of: {', '.join(sorted(METRICS))}"
    if not literal:
        return f"'{raw}' is missing a value after '{match.group(1)}'"
    if _RULE_OP_RE.search(literal):
        return f"'{raw}' has more than one comparison — a condition is '<metric> <op> <value>'"
    return (metric, match.group(1), literal)


def _coerce(value, literal: str):
    """Line up a metric value and a literal for comparison; None when they cannot be compared."""
    low = literal.strip().lower()
    if isinstance(value, bool):
        if low in ("true", "yes", "on", "1"):
            return value, True
        if low in ("false", "no", "off", "0"):
            return value, False
        return None
    if isinstance(value, (int, float)):
        try:
            return float(value), float(literal)
        except ValueError:
            return None
    return str(value).lower(), low


def condition_holds(ctx: dict, condition: str) -> bool:
    """Evaluate one parsed-on-the-fly condition. False for anything unknown or malformed —
    never an exception, never a match on missing data."""
    parsed = parse_condition(condition)
    if isinstance(parsed, str):
        return False
    metric, op, literal = parsed
    value = metric_value(ctx, metric)
    if value is None:
        return False
    if op is None:
        return bool(value)
    pair = _coerce(value, literal)
    if pair is None:
        return False
    try:
        return bool(RULE_OPS[op](pair[0], pair[1]))
    except TypeError:
        return False




# ── bar rules: one mechanism for painting the whole bar ──────────────────────
def _phase_rules(config: dict) -> list[dict]:
    """The idle-phase ladder expressed as bar rules, so there is ONE painting mechanism
    rather than two. A phase is just a rule whose condition is on `idle.secs`, and
    phase_for()'s "last phase whose `after` has strictly elapsed" is exactly the rule
    engine's "last matching rule wins" over an ascending ladder.

    /squirrel-phases, /squirrel-bg and /squirrel-bg-after keep editing the ladder; this only
    changes how it is evaluated, not where it is stored."""
    return [{"when": f"idle.secs > {ph['after']}", "bg": ph.get("bg"), "text": ph.get("text"),
             "bold": ph.get("bold"), "force": ph.get("force"), "source": "phase"}
            for ph in load_phases(config)]


def _phases_disabled(config: dict) -> bool:
    """The two long-standing "paint nothing at all" switches. They gate the PHASE-derived
    rules only: someone who turns the idle tint off and then writes a bar rule of their own
    means the rule to fire."""
    if "idleBackground" in config and not bg_code(config["idleBackground"]):
        return True
    legacy_after = _legacy_seconds(config, "idleBackgroundAfterSeconds")
    return legacy_after is not None and legacy_after <= 0


def bar_rules(config: dict | None = None) -> list[dict]:
    """Every rule in effect, in evaluation order: the idle ladder first, then the user's own
    `barRules`. Each entry carries `when`, the colours, an optional numeric `priority`, and —
    for a rule that does not parse — an `error` string, so --show-rules can tell the user what
    is wrong with their config instead of silently ignoring it."""
    config = config or {}
    rules = [] if _phases_disabled(config) else _phase_rules(config)
    raw = config.get("barRules")
    if isinstance(raw, list):
        for entry in raw:
            if not isinstance(entry, dict):
                rules.append({"when": repr(entry), "source": "user",
                              "error": "not a rule object"})
                continue
            rule = {"when": entry.get("when"), "bg": entry.get("bg"), "text": entry.get("text"),
                    "bold": entry.get("bold"), "priority": entry.get("priority"),
                    "source": "user"}
            parsed = parse_condition(entry.get("when"))
            if isinstance(parsed, str):
                rule["error"] = parsed
            elif not (entry.get("bg") or entry.get("text")):
                rule["error"] = "rule paints nothing — needs bg, text, or both"
            elif entry.get("bg") and not bg_code(entry["bg"]):
                rule["error"] = f"'{entry['bg']}' is not a valid bg colour"
            elif entry.get("text") and not color_code(entry["text"]):
                rule["error"] = f"'{entry['text']}' is not a valid text colour"
            rules.append(rule)
    return rules


def _rule_priority(rule: dict) -> float:
    value = rule.get("priority")
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return 0.0
    return float(value)


def winning_rule(ctx: dict, config: dict | None = None) -> dict | None:
    """The rule that paints this render, or None. Highest `priority` wins; ties go to the LAST
    matching rule in order, which is what makes an ascending idle ladder behave as it always
    has. A rule carrying an `error` is never evaluated."""
    winner, best = None, None
    for index, rule in enumerate(bar_rules(config if config is not None else ctx["config"])):
        if rule.get("error"):
            continue
        if not condition_holds(ctx, rule.get("when")):
            continue
        key = (_rule_priority(rule), index)
        if best is None or key >= best:
            winner, best = rule, key
    return winner


def rule_colors(rule: dict | None) -> tuple:
    """(background escape, text escape, force) for a winning rule; (None, None, False) for none.

    `force` is the escape hatch, and it is deliberately Task 14's word: there, a user's `fg`
    tints only what has no semantic colour unless they opt in with `force`. Here it is the
    same opt-in for the same reason — the flat look is available to anyone who wants it, and
    it is off by default because it hides a threshold."""
    if not rule:
        return None, None, False
    text = color_code(rule.get("text"))
    if text and rule.get("bold"):
        text = bold_code(text)
    return bg_code(rule.get("bg")), text, bool(rule.get("force"))


# ── segment registry ─────────────────────────────────────────────────────────
# One entry per renderable segment: what it produces, where it ships, and how hard it clings
# to the pane. Adding a segment is adding an entry — no render function to edit, no tier table
# to extend. `layout` in the config decides what actually renders and in what order; this
# registry only supplies the shipped arrangement and the per-segment metadata.
#
#   line/order  the shipped position — which line, and rank within it. Only used to build the
#               shipped layout (and to re-insert a segment that /squirrel-show turns back on).
#   priority    survival rank under width pressure; higher is kept longer, None = never
#               dropped (what `--set-priority <seg> always` writes). Overridable per segment
#               via the `segmentPriority` config map.
#   protected   cannot be removed from the layout or switched off at all (account, model).
#   group       segments sharing a group are joined by DOT; a group boundary (or group None)
#               is joined by SEP. This is what reproduces line 2's
#               "account · repo · git · model │ tok · $ · dur │ chip" punctuation.
#   details     the detail_opts() keys the producer reads. Also the per-render cache key, so a
#               producer whose output cannot change with the pressure level (every line-2
#               segment, notably git) is invoked exactly once per render however many fit
#               attempts _fit() makes.
class Segment:
    """A registry entry. Deliberately a plain __slots__ class rather than a NamedTuple or a
    dataclass: importing `typing` costs ~2ms on every one of these renders (one per session
    every few seconds), and this file's whole budget is ~35ms — see LazyImportGuardTests for
    the same reasoning applied to urllib."""
    __slots__ = ("id", "line", "order", "render", "priority", "protected", "group",
                 "details", "link", "click")

    def __init__(self, id: str, line: int, order: int, render, priority=None,
                 protected: bool = False, group=None, details: tuple = (), link=None,
                 click=None):
        self.id = id
        self.line = line
        self.order = order
        self.render = render
        self.priority = priority
        self.protected = protected
        self.group = group
        self.details = details
        self.link = link
        self.click = click

    def __repr__(self) -> str:
        return f"Segment({self.id!r}, line={self.line}, order={self.order}, priority={self.priority})"


def _ctx_for(status: dict, limits: dict, stale: bool, source: str, email: str | None,
             config: dict, now: float, tz: tzinfo | None, chip: str | None) -> dict:
    """Everything every producer may need, built once per render. `opts` is swapped in per
    pressure level by _render_line(); `cache` memoises producer output across fit attempts."""
    return {"status": status, "limits": limits, "stale": stale, "source": source,
            "email": email, "config": config, "now": now, "tz": tz, "chip_runs": chip,
            "opts": detail_opts(0, config), "cache": {}, "banner_bg": None}


# ── producers: line 1 ────────────────────────────────────────────────────────
# Every producer returns styled RUNS (see as_runs/join_runs): plain text plus the SGR that
# gives that piece its meaning. Returning a finished, pre-coloured string would put escapes
# where the sanitiser and the per-segment styles both need plain text.
def seg_context(ctx: dict) -> list:
    return context_segment_runs(ctx["status"].get("context_window") or {}, ctx["opts"], ctx["config"])


def seg_session(ctx: dict) -> list:
    opts, config, now = ctx["opts"], ctx["config"], ctx["now"]
    session = ctx["limits"].get("session")
    # NOT ctx["stale"]: since Task 29d this number comes from the payload Claude Code sent
    # with this very render, so it cannot be stale. `stale` now describes the API cache, and
    # the API cache only supplies the per-model buckets — marking a figure that arrived
    # milliseconds ago as old would be its own small lie.
    runs = session_usage_segment_runs("5h", session, False, opts, ctx["status"],
                                      ctx["email"], now, config)
    if opts["resets"] and session and session.get("resets_at"):
        left = session["resets_at"] - now
        runs.append((" \u21bb ", None))
        runs.append((fmt_relative(left), countdown_color(left, config)))
    return runs


def seg_weekly(ctx: dict) -> list:
    opts, config = ctx["opts"], ctx["config"]
    week = ctx["limits"].get("weekly_all")
    runs = usage_segment_runs("wk" if opts["abbrev"] else "week all", week, False, opts, config)
    if segment_enabled(config, "permodel"):
        for b in ctx["limits"].get("scoped") or []:
            runs.append((DOT, None))
            runs += usage_segment_runs(b["label"][:1] if opts["abbrev"] else b["label"],
                                       b, ctx["stale"], opts, config)
        if ctx["source"] == "stdin":
            runs.append((DOT + "per-model ?", None))
    if opts["resets"] and week and week.get("resets_at"):
        runs.append((f" \u21bb {fmt_weekday_time(week['resets_at'], ctx['tz'])}", None))
    return runs


def seg_share(ctx: dict) -> list:
    session = ctx["limits"].get("session")
    return share_segment_runs(ctx["status"], ctx["email"], ctx["now"], ctx["config"],
                              session["pct"] if session else None)


def seg_share_life(ctx: dict) -> list:
    return as_runs(share_life_segment(ctx["status"], ctx["email"], ctx["now"], ctx["config"]))


def seg_sessions(ctx: dict) -> list:
    return as_runs(sessions_segment(ctx["status"], ctx["email"], ctx["now"], ctx["config"]))


# ── producers: line 2 ────────────────────────────────────────────────────────
def seg_account(ctx: dict) -> list:
    return [(account_label(ctx["email"], ctx["config"]), resolve_color(ctx["email"], ctx["config"]))]


def seg_repo(ctx: dict) -> list:
    repo = repo_name(ctx["status"])
    return [(repo, repo_color(repo, ctx["config"]))] if repo else []


def seg_git(ctx: dict) -> list:
    return git_segment_runs(ctx["status"], ctx["config"])


def seg_model(ctx: dict) -> list:
    return as_runs((ctx["status"].get("model") or {}).get("display_name"))


def seg_effort(ctx: dict) -> list:
    return as_runs((ctx["status"].get("effort") or {}).get("level"))


def seg_flags(ctx: dict) -> list:
    status = ctx["status"]
    flags = []
    if (status.get("thinking") or {}).get("enabled"):
        flags.append("thinking")
    if status.get("fast_mode"):
        flags.append("fast")
    return as_runs(DOT.join(flags))


def seg_session_tokens(ctx: dict) -> list:
    ctxw = ctx["status"].get("context_window") or {}
    total = int(ctxw.get("total_input_tokens") or 0) + int(ctxw.get("total_output_tokens") or 0)
    return as_runs(f"tok {fmt_tokens(total)}" if total else None)


def seg_cost(ctx: dict) -> list:
    return [(f"${float((ctx['status'].get('cost') or {}).get('total_cost_usd') or 0):.2f}", None)]


def seg_duration(ctx: dict) -> list:
    return [(fmt_duration(float((ctx["status"].get("cost") or {}).get("total_duration_ms") or 0)), None)]


def seg_lines(ctx: dict) -> list:
    cost = ctx["status"].get("cost") or {}
    return [(f"+{int(cost.get('total_lines_added') or 0)}/\u2212{int(cost.get('total_lines_removed') or 0)}", None)]


def seg_pr(ctx: dict) -> list:
    return as_runs(fmt_pr(ctx["status"].get("pr")))


def seg_chip(ctx: dict) -> list:
    """The activity chip. Task 17 turned this into ordinary runs like every other producer —
    it used to arrive already rendered and be marked RAW so the seam could not touch it,
    which left one fragment the boundary sanitiser had to handle at its source. There is no
    longer an exception: the chip's text is plain here and is sanitised and styled with
    everything else."""
    return ctx.get("chip_runs") or []


# ── background agents ────────────────────────────────────────────────────────
# Claude Code's hooks cannot see a BACKGROUND agent while it works: PostToolUse fires once at
# spawn, then Stop, so the bar reads "idle" with three agents running. That is what this
# segment is for — and it is the reason it reads a path nobody documents:
#
#     <tmpdir>/claude-<uid>/<project-slug>/<session-id>/tasks/
#
# EVERY assumption in that path is undocumented and may change without notice, so the whole
# thing degrades to "no segment" rather than guessing: unknown session, missing directory,
# unreadable directory, unexpected shape — all render nothing at all. It never errors, and it
# never reports a number it cannot stand behind.
#
# What counts as an agent is the part a first pass gets wrong. That directory holds two very
# different things:
#
#     agent-id.output -> .../subagents/agent-<id>.jsonl     a SYMLINK: a real agent
#     b4nhdbqzi.output                                       a regular file: a tool result
#
# Counting "`.output` files touched in the last minute" — the obvious reading, and what this
# task was originally sketched as — counts your own Bash output as a running agent. Observed
# on a real session: three symlinks (all finished hours ago) and three regular files (all
# written seconds ago) would have reported three live agents when the true answer was zero.
# So: symlinks only, and liveness comes from the TARGET's mtime, because the link's own mtime
# is when the agent started, not when it last did anything.
#
# Cost when nothing is running, which is almost always: one isdir() to find the directory and
# one scandir() of it. `entry.is_symlink()` is answered from the directory entry itself, so a
# directory of plain tool outputs costs no extra syscalls at all — we only stat() symlinks,
# and there are none.
def _tmp_roots() -> list:
    """Candidate `<tmpdir>/claude-*` directories, cheapest guess first. Plain strings and
    os.path rather than Path objects: this runs on every render, and constructing Path
    objects to throw away costs more than the two stat() calls it takes to answer."""
    tmp = os.environ.get("SQUIRREL_TMPDIR") or os.environ.get("TMPDIR") or "/tmp"
    try:
        preferred = os.path.join(tmp, f"claude-{os.getuid()}")
        if os.path.isdir(preferred):
            return [preferred]                          # the layout observed in practice
    except (AttributeError, OSError):
        pass                                            # getuid() is POSIX-only
    try:
        return [os.path.join(tmp, n) for n in os.listdir(tmp)
                if n.startswith("claude-") and os.path.isdir(os.path.join(tmp, n))]
    except OSError:
        return []


def agents_tasks_dir(session_id, cwd):
    """The tasks directory for this session as a path string, or None. Derived, not searched:
    the project slug is the working directory with its separators replaced, so this is two
    stat() calls rather than a walk."""
    if not session_id or not cwd:
        return None
    slug = str(cwd).replace(os.sep, "-").replace("/", "-")
    for root in _tmp_roots():
        candidate = os.path.join(root, slug, str(session_id), "tasks")
        try:
            if os.path.isdir(candidate):
                return candidate
        except OSError:
            continue
    return None


def live_agent_count(session_id, cwd, now: float, config: dict | None = None) -> int:
    """How many background agents have written in the last `agentsLiveSeconds`. 0 for
    anything we cannot determine — the segment then renders nothing, which is the honest
    answer for "we do not know" as much as for "none"."""
    directory = agents_tasks_dir(session_id, cwd)
    if directory is None:
        return 0
    window = tunable(config, "agentsLiveSeconds")
    live = 0
    try:
        with os.scandir(directory) as entries:
            for entry in entries:
                # is_symlink() comes from the directory entry, so plain tool outputs cost
                # nothing; only real agent links are followed.
                if not entry.name.endswith(".output") or not entry.is_symlink():
                    continue
                try:
                    if now - os.stat(entry.path).st_mtime <= window:
                        live += 1
                except OSError:
                    continue        # a dangling link is a finished agent, not an error
    except OSError:
        return 0
    return live


def seg_agents(ctx: dict) -> list:
    status = ctx["status"]
    ws = status.get("workspace") or {}
    cwd = ws.get("current_dir") or ws.get("project_dir")
    count = live_agent_count(status.get("session_id"), cwd, ctx["now"], ctx["config"])
    if count <= 0:
        return []
    return [(f"⧗ {count} agent{'' if count == 1 else 's'}", CYAN)]


_BARS = ("bars", "bar_cells")
SEGMENTS: dict[str, Segment] = {s.id: s for s in (
    # line 1 — context and limits. The three that carry numbers are never dropped: on a pane
    # too narrow even for them the line simply overflows, exactly as the old tier table's
    # last row did.
    Segment("context",       0, 10, seg_context,       details=(*_BARS, "tokens")),
    Segment("session",       0, 20, seg_session,       details=(*_BARS, "resets")),
    Segment("weekly",        0, 30, seg_weekly,        details=(*_BARS, "resets", "abbrev")),
    Segment("share",         0, 40, seg_share,         priority=40),
    Segment("share-life",    0, 50, seg_share_life,    priority=40),
    Segment("sessions",      0, 60, seg_sessions,      priority=40),
    # line 2 — identity, then the session's own numbers, then the activity chip.
    Segment("account",       1, 10, seg_account,       protected=True, group="id"),
    Segment("repo",          1, 20, seg_repo,          priority=80, group="id"),
    Segment("git",           1, 30, seg_git,           priority=80, group="id"),
    Segment("model",         1, 40, seg_model,         protected=True, group="id"),
    Segment("effort",        1, 50, seg_effort,        priority=60, group="id"),
    Segment("flags",         1, 60, seg_flags,         priority=50, group="id"),
    Segment("sessionTokens", 1, 70, seg_session_tokens, priority=70, group="spend"),
    Segment("cost",          1, 80, seg_cost,          priority=40, group="spend"),
    Segment("duration",      1, 90, seg_duration,      priority=30, group="spend"),
    Segment("lines",         1, 100, seg_lines,        priority=20, group="spend"),
    Segment("pr",            1, 110, seg_pr,           priority=10, group="spend"),
    Segment("agents",        1, 115, seg_agents,       priority=45, group="spend"),
    Segment("chip",          1, 120, seg_chip),
)}
# Derived, so the two can never disagree: /squirrel-show refuses to hide these.
PROTECTED_SEGMENTS = tuple(s.id for s in SEGMENTS.values() if s.protected)





# ── click to acknowledge: an on-demand loopback listener ─────────────────────
# Claude Code exposes no click, hover or mouse event for the status line. The only
# interactivity a terminal offers is an OSC-8 hyperlink, and a hyperlink can only open a URL.
# So "click the water reminder to dismiss it" requires something local to be listening.
#
# THE RULE THAT GOVERNS THIS ENTIRE SECTION, in the user's words: "it should run only when
# needed. We should not neccessarily create local listener servers for users which dont need
# that." Nothing is bound at install, on first render, or by any default. Three separate
# conditions must ALL hold before a socket exists:
#
#   1. the feature is switched on          (clickToAck, default false)
#   2. some segment actually declares      (click=ack / click=snooze:<mins>)
#   3. the terminal can follow the link    (see click_capable_terminal)
#
# Condition 3 is not in the original brief and is the sharpest form of the rule. Claude Code
# currently passes OSC-8 through to IDE terminals (VS Code, Cursor) but NOT to standalone
# emulators — a regression between 2.0.76 and 2.1.44, upstream issue #26356, closed as "not
# planned". On Konsole or iTerm2 the link is simply not clickable, so binding a socket there
# would be a server that can never receive a request. "Only when needed" has to mean "only
# where it can work", or the rule is satisfied on paper and broken in fact.
#
# WHAT THE LISTENER CAN DO, exhaustively: acknowledge or snooze a segment id that already
# exists in the user's own config. It never runs a command. It never takes a path, a filename
# or a duration from the request. It never writes anything but acknowledgement state. That
# fixed verb set is the property that keeps a loopback HTTP server from being a remote code
# execution hole, and it must not be widened casually — a `click=command:...` would undo the
# entire security argument.
CLICK_ACTIONS = ("ack", "snooze")
# Terminals where Claude Code is known to pass OSC-8 through to the emulator. Deliberately an
# allow-list: a terminal wrongly included means a socket nobody can use, which is the failure
# this is here to prevent.
CLICK_CAPABLE_TERM_PROGRAMS = ("vscode", "cursor", "windsurf")


def click_enabled(config: dict | None = None) -> bool:
    return bool((config or {}).get("clickToAck", False))


def click_capable_terminal(config: dict | None = None) -> bool:
    """Whether a hyperlink in the status line can actually be followed here.

    `clickTerminals` overrides the allow-list for anyone who knows their terminal works (or
    knows ours is wrong); `clickTerminals: ["any"]` disables the check entirely."""
    allowed = (config or {}).get("clickTerminals")
    allowed = [str(a).lower() for a in allowed] if isinstance(allowed, list) and allowed \
        else list(CLICK_CAPABLE_TERM_PROGRAMS)
    if "any" in allowed:
        return True
    return (os.environ.get("TERM_PROGRAM") or "").lower() in allowed


def click_state_path() -> Path:
    """The listener record. In the SHARED store, not the per-profile one.

    Audit #2: this used to descend from config_dir(), so "one listener per machine" was
    false — it was one per profile, and the user runs three, so three live sockets. The
    acknowledgement state it exists to write is already cross-profile (shared_dir()), so the
    listener that writes it has to be too, or the claim in the docs is simply untrue."""
    return shared_dir() / "click-listener.json"


def click_lock_path() -> Path:
    return shared_dir() / "click-listener.lock"


def _proc_start_ticks(pid: int):
    """The process's own start time, so a REUSED pid cannot inherit a dead listener's record.

    Linux only. Elsewhere this returns None and callers treat that as a match, which means
    the guard is absent rather than degraded — see _pid_owns_port for the full statement of
    what is and is not checked off Linux."""
    try:
        with open(f"/proc/{pid}/stat", "rb") as handle:
            fields = handle.read().rsplit(b")", 1)[1].split()
        return int(fields[19])          # starttime, field 22 of proc(5)
    except (OSError, IndexError, ValueError):
        return None


def _pid_alive(pid) -> bool:
    """Whether a process id is live.

    `os.kill(pid, 0)` is the POSIX idiom and it is actively DANGEROUS on Windows, which is
    what CI found on its first run: there, signal 0 is CTRL_C_EVENT, so asking "is this pid
    alive?" sends Ctrl-C to our own console group. The test that checks a dead owner's lock
    can be taken over interrupted the test runner itself. On a user's machine the same call
    would have Ctrl-C'd whatever shares the console.

    So Windows gets the API that answers the question it was actually asked: open the process
    and read its exit code (259 = STILL_ACTIVE)."""
    if isinstance(pid, bool) or not isinstance(pid, int) or pid <= 0:
        return False
    if os.name == "nt":
        try:
            import ctypes
            PROCESS_QUERY_LIMITED_INFORMATION, STILL_ACTIVE = 0x1000, 259
            kernel32 = ctypes.windll.kernel32
            handle = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
            if not handle:
                return False            # gone, or not ours to look at
            code = ctypes.c_ulong()
            ok = kernel32.GetExitCodeProcess(handle, ctypes.byref(code))
            kernel32.CloseHandle(handle)
            return bool(ok) and code.value == STILL_ACTIVE
        except Exception:
            return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True         # exists, owned by someone else
    except OSError:
        return False
    return True


def _pid_owns_port(pid: int, port: int) -> bool:
    """Whether `pid` actually holds a listening socket on `port`.

    Audit #4: liveness alone let the plugin hand its token to a process it did not start —
    the listener dies, something unrelated takes the port, a live (or reused) pid keeps the
    record looking valid, and a real click delivers the token to a stranger. Reading the
    socket's inode out of /proc/net/tcp and matching it against the pid's own descriptors
    binds pid to port.

    What this proves, exactly: that the recorded pid is the process holding the port. It does
    NOT prove that process is our listener. Someone who already owns the port AND can write
    the shared state file can still get themselves named — that is a different attacker with
    a different capability, and this guard does not claim to stop them. What it closes is the
    accidental case the finding was about: our listener dies, something unrelated binds the
    port, and a live-or-reused pid keeps the stale record looking valid.

    **Linux only, and there is no fallback.** Off Linux this returns True — and so does the
    start-time check (_proc_start_ticks returns None, which callers read as "matches"), so on
    macOS and Windows BOTH of audit #4's guards are no-ops and liveness alone is what remains.
    An earlier version of this docstring said it "falls back to the other guards"; there are
    no other guards, and saying so was worse than saying nothing. macOS is a first-class
    Claude Code platform, so this is stated in the README's security section too."""
    if not sys.platform.startswith("linux"):
        return True
    inodes = set()
    for table in ("/proc/net/tcp", "/proc/net/tcp6"):
        try:
            with open(table, "r", encoding="utf-8", errors="replace") as handle:
                next(handle, None)
                for line in handle:
                    parts = line.split()
                    if len(parts) < 10 or parts[3] != "0A":      # 0A = LISTEN
                        continue
                    if int(parts[1].rsplit(":", 1)[1], 16) == port:
                        inodes.add(parts[9])
        except OSError:
            return True         # cannot check — do not fail closed on a read error
    if not inodes:
        return False            # nothing is listening there at all
    try:
        for name in os.listdir(f"/proc/{pid}/fd"):
            try:
                target = os.readlink(f"/proc/{pid}/fd/{name}")
            except OSError:
                continue
            if target.startswith("socket:[") and target[8:-1] in inodes:
                return True
    except OSError:
        return True             # cannot read the pid's descriptors (permissions) — allow
    return False


def _discard_click_state() -> None:
    try:
        click_state_path().unlink(missing_ok=True)
    except OSError:
        pass


def read_click_listener(now: float, config: dict | None = None) -> dict | None:
    """The live listener's {port, token}, or None.

    Four things must hold, and each closes a finding: the record is well-formed (#15 — a
    JSON `true` is not a port), its process is alive, that process started when the record
    says it did (#4 — pid reuse), and it actually owns the port (#4 — port takeover). A
    record failing any of them is deleted rather than ignored, so it stops claiming a port
    that may since belong to something else."""
    data = load_json(click_state_path())
    if not isinstance(data, dict):
        return None
    port, token, pid = data.get("port"), data.get("token"), data.get("pid")
    if isinstance(port, bool) or not isinstance(port, int) or not 0 < port < 65536:
        _discard_click_state()
        return None
    if not isinstance(token, str) or not token:
        _discard_click_state()
        return None
    try:
        started = float(data.get("started") or 0)      # #9: a string here killed the bar
    except (TypeError, ValueError):
        _discard_click_state()
        return None
    idle = tunable(config, "clickIdleSeconds")
    # #5: the listener refreshes `started` while it lives, so this bound tracks its own exit
    # rule instead of expiring a busy listener out from under itself and spawning a rival.
    fresh = now - started <= idle + LOCK_MAX_AGE_S
    ticks = data.get("start_ticks")
    same_process = ticks is None or _proc_start_ticks(pid) == ticks
    if not (_pid_alive(pid) and fresh and same_process and _pid_owns_port(pid, port)):
        _discard_click_state()
        return None
    return {"port": port, "token": token, "pid": pid}


def click_url(ctx: dict, seg_id: str, action: str) -> str | None:
    """The acknowledge URL for one segment, or None when no listener is live yet.

    Reads the listener state once per render. When nothing is listening it spawns one,
    detached, and returns None — so the first render after enabling shows the segment without
    a link and the next one has it. The render never waits for a socket."""
    config = ctx["config"]
    if not (click_enabled(config) and click_capable_terminal(config)):
        return None
    if "click_listener" not in ctx:
        ctx["click_listener"] = read_click_listener(ctx["now"], config)
        if ctx["click_listener"] is None:
            spawn_click_listener(ctx["now"])
    live = ctx["click_listener"]
    if not live:
        return None
    verb = "snooze" if str(action).startswith("snooze") else "ack"
    minutes = ""
    if verb == "snooze":
        _, _, raw = str(action).partition(":")
        minutes = f"/{int(raw)}" if raw.strip().isdigit() else ""
    return safe_link(f"http://127.0.0.1:{live['port']}/{live['token']}/{verb}/{seg_id}{minutes}")


def spawn_click_listener(now: float) -> None:
    """Start the listener detached, at most one at a time per machine."""
    try:
        lock = click_lock_path()
        try:
            # #11: a stray non-file at the lock path used to kill the feature for ever.
            if lock.is_dir() or lock.is_symlink():
                return
            if now - lock.stat().st_mtime < LOCK_MAX_AGE_S:
                return
        except OSError:
            pass
        touch_private(lock)
        import subprocess
        subprocess.Popen(
            [sys.executable, os.path.abspath(__file__), "--click-listener"],
            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            **detach_kwargs(subprocess))
    except Exception:
        pass        # a listener that cannot start must never disturb the render



def _take_click_lock(now: float):
    """Claim the right to be THE listener on this machine, atomically.

    O_CREAT|O_EXCL so two starters cannot both win. A lock whose recorded pid is gone, or
    which is older than an idle window with no live owner, is stale and may be taken over —
    otherwise one crash would disable the feature permanently."""
    path = click_lock_path()
    mkdir_private(path.parent)
    for attempt in (1, 2):
        try:
            handle = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
            os.write(handle, str(os.getpid()).encode())
            os.close(handle)
            return path
        except FileExistsError:
            try:
                owner = int(path.read_text().strip() or 0)
            except (OSError, ValueError):
                owner = 0
            if owner and owner != os.getpid() and _pid_alive(owner):
                return None             # someone else is genuinely holding it
            if attempt == 1:
                try:
                    path.unlink(missing_ok=True)    # stale: the owner is gone
                except OSError:
                    return None
        except OSError:
            return None
    return None


def _release_click_lock(path) -> None:
    try:
        if path is not None and int(Path(path).read_text().strip() or 0) == os.getpid():
            Path(path).unlink(missing_ok=True)
    except (OSError, ValueError):
        pass


def _click_page(title: str, detail: str) -> bytes:
    """A tiny self-contained page. No external anything, and the segment id — which is
    user-authored text — is HTML-escaped as well as sanitised.

    Styled to match the bar rather than the browser: the same near-black ground and amber
    (#c9a227) the status line uses, a monospace face, and the ▰▱ cell motif. The rule along
    the bottom drains over the same 1.2s the auto-close waits, so the tab disappearing is
    something the page told you was coming rather than something that just happened."""
    import html
    return (
        "<!doctype html><meta charset=utf-8><title>Squirrel</title>"
        "<style>"
        "*{box-sizing:border-box}"
        "body{margin:0;min-height:100vh;display:flex;flex-direction:column;align-items:center;"
        "justify-content:center;gap:26px;padding:40px;position:relative;background:#0d0e10;"
        "color:#e8e6e1;font:16px ui-monospace,SFMono-Regular,'SF Mono',Menlo,Consolas,monospace}"
        ".mark{display:flex;align-items:center;gap:9px;color:#6d6a63;font-size:12px;"
        "letter-spacing:.14em;text-transform:uppercase}"
        ".row{display:flex;align-items:center;gap:14px}"
        "h1{margin:0;font-size:38px;font-weight:600;letter-spacing:-.015em;color:#c9a227;line-height:1}"
        "p{margin:0;font-size:15px;color:#a3a099}"
        ".cells{display:flex;gap:4px}"
        ".cells i{width:22px;height:6px;border-radius:1px;background:#26282c}"
        ".cells i.on{background:#c9a227}"
        ".cells i.dim{background:#c9a227;opacity:.55}"
        ".drain{position:absolute;left:0;right:0;bottom:0;height:3px;background:#26282c}"
        ".drain span{display:block;height:100%;background:#c9a227;transform-origin:left;"
        "animation:d 1.2s linear forwards}"
        "@keyframes d{from{transform:scaleX(1)}to{transform:scaleX(0)}}"
        "small{position:absolute;bottom:16px;font-size:11px;color:#55524c;letter-spacing:.08em}"
        "</style>"
        "<div class=mark>"
        "<svg width=15 height=15 viewBox='0 0 24 24' fill=none stroke=currentColor stroke-width=1.6 "
        "stroke-linecap=round stroke-linejoin=round aria-hidden=true>"
        "<path d='M9 3.5c3 0 5 2 5 4.6 0 2-1.4 3-1.4 3H16c2.2 0 4 1.9 4 4.2S18.2 19.5 16 19.5H8.5'/>"
        "<path d='M8.5 19.5c-2.5 0-4.5-2-4.5-4.6 0-2.4 1.6-3.7 3.4-4.2'/></svg>squirrel</div>"
        "<div class=row>"
        "<svg width=28 height=28 viewBox='0 0 24 24' fill=none stroke='#c9a227' stroke-width=2.2 "
        "stroke-linecap=round stroke-linejoin=round aria-hidden=true><path d='M4.5 12.5l5 5 10-11'/></svg>"
        f"<h1>{html.escape(title)}</h1></div>"
        f"<p>{html.escape(detail)}</p>"
        "<div class=cells><i class=on></i><i class=dim></i><i></i><i></i><i></i><i></i></div>"
        "<div class=drain><span></span></div><small>closing&hellip;</small>"
        "<script>setTimeout(function(){window.close()},1200)</script>"
    ).encode("utf-8")


def _click_apply(verb: str, seg_id: str, minutes: str) -> tuple:
    """Perform one click. The ONLY two things this can do — see the section header.

    Audit #6: authorisation is re-checked HERE, on every request, not merely when the URL
    was minted. `/squirrel-click off` used to mean "off in up to thirty minutes" — the socket
    outlived the setting that permitted it, and an already-rendered URL kept working. The
    config is already loaded per request, so honouring it costs nothing. The same goes for a
    segment whose `click=` has since been removed: no click action, no click."""
    config = load_config()
    if not click_enabled(config):
        return "Clicking is switched off", "run /squirrel-click on to re-enable it"
    spec = next((c for c in custom_specs(config) if c["id"] == seg_id), None)
    if spec is None or not spec.get("ack"):
        return "Nothing to do", f"{seg_id} is not a dismissible segment"
    if not spec.get("click"):
        return "Nothing to do", f"{seg_id} is no longer clickable"
    if verb == "snooze" and minutes:
        cmd_snooze(f"{seg_id} {minutes}")
        return "Snoozed", f"{seg_id} — back in {minutes} minutes"
    cmd_ack(seg_id)
    every = _positive_seconds(spec.get("every"))
    back = f" — back in {_fmt_duration_short(every)}" if every else ""
    return "Acknowledged", f"{seg_id}{back}"


def _click_handler_class(state: dict):
    """The request handler. Everything it refuses, it refuses with a bare 404 and no hint."""
    import secrets
    from http.server import BaseHTTPRequestHandler

    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.0"        # no keep-alive: one request, one connection
        server_version = "Squirrel"
        sys_version = ""
        # Bounds a slow or silent client. This belongs on the CONNECTION, not on the
        # listening socket: putting a timeout on the listener makes accept() non-blocking and
        # killed the detached child outright on Windows, where the state file appeared and
        # the process was gone a moment later.
        timeout = 10

        def log_message(self, *_args):
            """Silence. The URL contains the token, so logging a request line would write the
            only secret in this design to a place it was never meant to be."""

        def _deny(self):
            self.send_response(404)
            self.send_header("Content-Length", "0")
            self.end_headers()

        def do_GET(self):                                    # noqa: N802
            # Audit #10: `last_seen` is bumped only AFTER the token checks out. Bumping it
            # first let any local process keep a listener alive for ever by poking the port,
            # which is also what turned #5 into an unbounded accumulation.
            # DNS rebinding: a page on the open web can resolve its own hostname to 127.0.0.1
            # and issue requests here. The browser sends that hostname in Host, so requiring
            # a literal loopback Host closes it. No CORS headers are ever sent, so a
            # cross-origin page cannot read the response either way.
            host = (self.headers.get("Host") or "").strip()
            if host not in (f"127.0.0.1:{state['port']}", f"localhost:{state['port']}"):
                return self._deny()
            parts = [p for p in self.path.split("?")[0].split("/") if p]
            if len(parts) < 3 or not secrets.compare_digest(parts[0], state["token"]):
                return self._deny()          # wrong or missing token: 404, no hints, no list
            verb, seg_id = parts[1], parts[2]
            minutes = parts[3] if len(parts) > 3 and parts[3].isdigit() else ""
            if verb not in CLICK_ACTIONS:
                return self._deny()
            state["last_seen"] = time.time()        # authenticated: this one counts
            try:
                title, detail = _click_apply(verb, sanitise(seg_id), minutes)
            except Exception:
                title, detail = "Something went wrong", "nothing was changed"
            body = _click_page(title, detail)
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)

        def do_POST(self):                                   # noqa: N802
            self._deny()

        do_PUT = do_DELETE = do_PATCH = do_HEAD = do_OPTIONS = do_POST

        def handle_one_request(self):
            """A malformed request closes the connection instead of raising — the listener
            must never wedge on someone probing the port."""
            try:
                super().handle_one_request()
            except Exception:
                self.close_connection = True

    return Handler


def run_click_listener(now: float | None = None, serve: bool = True) -> dict:
    """The detached click-to-acknowledge listener. Loopback only, ephemeral port, one
    unguessable token per start, and it exits by itself once nothing has clicked for
    `clickIdleSeconds`."""
    import secrets
    import socketserver
    now = time.time() if now is None else now
    config = load_config()
    # Audit #3: the lock guarded SPAWNING only, so a second listener would bind anyway and
    # overwrite the record — orphaning the first, which kept serving its old token for the
    # rest of its idle window, referenced by nothing. Taken here, before bind, with O_EXCL
    # so it is the socket owner that holds it and not whoever happened to start last.
    holder = _take_click_lock(now)
    if holder is None:
        return {"port": 0, "token": ""}
    # 128 bits, not 256. The token is the only barrier on loopback, and 128 bits is far past
    # any local brute force against a listener that lives half an hour — while every character
    # of it is charged against the status line's width by Claude Code (see host_len), and 21
    # columns is the difference between a reminder that can be clicked on a busy bar and one
    # that cannot.
    token = secrets.token_urlsafe(16)
    state = {"token": token, "last_seen": now, "port": 0}

    class Server(socketserver.TCPServer):
        allow_reuse_address = False          # never inherit a port someone else is on
        timeout = 1.0                        # so the idle check actually runs
        def handle_timeout(self):
            pass                             # nothing arrived within `timeout`; loop again
        def handle_error(self, *_a):         # a bad client is not an event worth reporting
            pass

    # 127.0.0.1 always, whatever any config might say. A status-line acknowledgement has no
    # business being reachable from the network.
    server = Server(("127.0.0.1", 0), _click_handler_class(state), bind_and_activate=False)
    server.server_bind()
    server.server_activate()
    state["port"] = server.server_address[1]

    def publish(stamp: float) -> None:
        write_cache(click_state_path(), {"port": state["port"], "token": token,
                                         "pid": os.getpid(), "started": stamp,
                                         "start_ticks": _proc_start_ticks(os.getpid())})

    publish(now)
    if not serve:
        server.server_close()
        _release_click_lock(holder)
        return {"port": state["port"], "token": token}
    idle = tunable(config, "clickIdleSeconds")
    try:
        published = now
        while time.time() - state["last_seen"] < idle:
            server.handle_request()          # returns after `timeout` with nothing pending
            # Audit #5: `started` was written once and never refreshed while the exit loop
            # watched `last_seen`, so a busy listener outlived its own record — a render
            # would delete it as stale and spawn a replacement beside it, every idle window,
            # for ever. Republishing keeps the record's life and the process's life the same
            # fact.
            if time.time() - published > LOCK_MAX_AGE_S:
                published = time.time()
                publish(published)
    finally:
        _release_click_lock(holder)
        server.server_close()
        try:
            if read_click_listener(time.time(), config) and \
                    load_json(click_state_path()).get("pid") == os.getpid():
                click_state_path().unlink(missing_ok=True)
        except OSError:
            pass
    return {"port": state["port"], "token": token}


# ── custom segments ──────────────────────────────────────────────────────────
# One mechanism, configured differently — there is no "reminder" type in this file. A
# reminder is a custom segment with `every` and `ack`; a CI indicator is one with `command`;
# a branch warning is one with `when`. The user's own framing, and the reason it is built
# this way: "is this really a reminder type or better customSegment where customSegments
# would get more options to ACT as reminders, so we dont hardcode it?"
#
#   id        the segment id — usable anywhere a built-in id is (layout, style, priority)
#   text      static text, OR
#   command   a shell command whose stdout is the text (never run on the render path —
#             see custom_command_value())
#   every     seconds; with `ack` it is how long after acknowledging before it returns,
#             without it, how often a `command` is refreshed
#   at        "HH:MM" wall clock, as an alternative to `every`
#   ack       the segment can be dismissed with /squirrel-done; it comes back when next due
#   shared    keep the acknowledgement in the cross-profile store, so dismissing it once
#             clears it in every session and every alias
#   when      a Task 15 condition — the same grammar, never a second one
#   line      which layout line it joins when it has not been placed explicitly (1-based)
CUSTOM_FIELDS = ("id", "text", "command", "every", "at", "ack", "shared", "when", "line",
                 "link", "click")


def custom_specs(config: dict | None = None) -> list[dict]:
    """The user's `customSegments`, with entries that could never work dropped: not an
    object, no id, or an id that would shadow a built-in segment."""
    raw = (config or {}).get("customSegments")
    if not isinstance(raw, list):
        return []
    out, seen = [], set()
    for spec in raw:
        if not isinstance(spec, dict):
            continue
        sid = spec.get("id")
        if not isinstance(sid, str) or not sid or sid in SEGMENTS or sid in seen:
            continue
        seen.add(sid)
        out.append(spec)
    return out


# Line 2, not line 1. A custom segment with no `line:` of its own used to land at the end of
# the FIRST line — the line that is nothing but numbers: context, the 5-hour window, the weekly
# buckets. A reminder there reads as another metric. Line 2 is where the identity and the
# activity chip live, which is what a reminder belongs next to, and it is where the first Mac
# install put "drink water" before the user pointed out it looked wrong.
CUSTOM_DEFAULT_LINE = 2


def _custom_line(spec: dict) -> int:
    raw = spec.get("line")
    if isinstance(raw, bool) or not isinstance(raw, (int, float)) or raw < 1:
        return CUSTOM_DEFAULT_LINE - 1
    return int(raw) - 1


def _custom_producer(spec: dict):
    def produce(ctx: dict) -> list:
        if spec.get("when") and not condition_holds(ctx, spec["when"]):
            return []
        if not custom_due(ctx, spec):
            return []
        text = custom_text(ctx, spec)
        return [(text, None)] if text else []
    return produce


def custom_segments(config: dict | None = None) -> dict:
    """The user's custom segments as registry entries, so everything that already works for
    a built-in — layout, styles, priorities, /squirrel-show, the sanitiser at the seam —
    works for them with no special cases anywhere."""
    out: dict = {}
    for index, spec in enumerate(custom_specs(config)):
        # A reminder you configured to interrupt you outranks the spend readouts: when the
        # pane runs out of room the bar gives up "$4.12 · 12m · +431/−96" before it gives up
        # "drink water". A plain informational custom segment keeps the old low priority.
        # Interactive segments are ranked in the order the user listed them, one step apart
        # (55, 54, 53 …, never below 41 so they always outrank the spend group). The order is
        # what the user already expressed by writing them down; the STEPS are what make two
        # reminders shed one at a time instead of together — including their links, which cost
        # ~64 columns each. With both at one priority, a pane with room for one clickable
        # reminder showed neither, because the only levels available dropped both at once.
        interactive = bool(spec.get("ack") or spec.get("click"))
        rank = max(41, 55 - index) if interactive else 20
        out[spec["id"]] = Segment(spec["id"], _custom_line(spec), 1000 + index,
                                  _custom_producer(spec), priority=rank,
                                  link=safe_link(spec.get("link")),
                                  click=spec.get("click") if spec.get("ack") else None)
    return out


def all_segments(config: dict | None = None) -> dict:
    """Built-in segments plus the user's custom ones. Custom ids can never shadow a built-in
    (custom_specs() drops those), so the merge order is safe either way."""
    customs = custom_segments(config)
    return {**SEGMENTS, **customs} if customs else SEGMENTS



def _positive_seconds(value):
    """A positive number of seconds, or None. Booleans are not numbers here."""
    if isinstance(value, bool) or not isinstance(value, (int, float)) or value <= 0:
        return None
    return float(value)


def _parse_at(value):
    """'HH:MM' -> (hour, minute), or None when absent or malformed."""
    if not isinstance(value, str):
        return None
    parts = value.strip().split(":")
    if len(parts) != 2:
        return None
    try:
        hour, minute = int(parts[0]), int(parts[1])
    except ValueError:
        return None
    return (hour, minute) if 0 <= hour <= 23 and 0 <= minute <= 59 else None


def _at_epoch_today(now: float, at: tuple, tz: tzinfo | None) -> float:
    """The epoch time of HH:MM on the day `now` falls in, in the render's timezone."""
    moment = datetime.fromtimestamp(now, tz) if tz else datetime.fromtimestamp(now)
    return moment.replace(hour=at[0], minute=at[1], second=0, microsecond=0).timestamp()


def safe_link(url) -> str | None:
    """A URL fit to emit as an OSC-8 hyperlink, or None.

    Only http and https: a status line that can be made to render `file://` or, worse, a
    shell-adjacent scheme is a footgun, and no other scheme has a use here. The URL is
    sanitised as well as scheme-checked, because an OSC-8 payload containing its own
    terminator would let a crafted link close our sequence and start something else."""
    if not isinstance(url, str):
        return None
    cleaned = url.strip()
    if not cleaned or any(c.isspace() for c in cleaned):
        return None
    # Rejected, not repaired: if sanitising would CHANGE the URL then it carried control
    # characters, and quietly emitting a different link than the one written is worse than
    # emitting none. Everywhere else we strip in place; a destination is not text.
    if sanitise(cleaned) != cleaned:
        return None
    return cleaned if cleaned.lower().startswith(("http://", "https://")) else None


def custom_state_path(shared: bool) -> Path:
    """Where a custom segment's acknowledgement lives. `shared: true` puts it in the
    cross-profile store, so acknowledging once clears it in every session AND every alias —
    the user runs claude-work and claude-personal with different CLAUDE_CONFIG_DIRs, and a
    water reminder is not per-account. Propagation needs no IPC: every bar re-reads this on
    its next refresh, so all sessions clear within a few seconds."""
    return (shared_dir() if shared else data_dir()) / "custom-state.json"


def _custom_state(ctx: dict, shared: bool) -> dict:
    """Both stores are read at most once per render and memoised on the context."""
    cache = ctx.setdefault("custom_state", {})
    if shared not in cache:
        data = load_json(custom_state_path(shared))
        cache[shared] = data if isinstance(data, dict) else {}
    return cache[shared]


def custom_ack_time(ctx: dict, spec: dict):
    entry = _custom_state(ctx, bool(spec.get("shared"))).get(spec["id"])
    if not isinstance(entry, dict):
        return None
    value = entry.get("acked_at")
    return float(value) if isinstance(value, (int, float)) and not isinstance(value, bool) else None


def custom_due(ctx: dict, spec: dict) -> bool:
    """Whether a scheduled custom segment should be showing right now.

    `at` is a schedule: hidden before that time of day, shown after it. `every` is a re-fire
    interval, but ONLY for a segment that can be acknowledged — without `ack` it is the
    refresh interval of a `command` and has no bearing on visibility.

    A segment that has never been acknowledged is due. That is deliberate: an outstanding
    reminder is outstanding, it needs no first-seen bookkeeping written from the render path,
    and adding one shows it immediately, which is how the user finds out it works."""
    at = _parse_at(spec.get("at"))
    every = _positive_seconds(spec.get("every"))
    acked = custom_ack_time(ctx, spec)
    if at is not None:
        due_at = _at_epoch_today(ctx["now"], at, ctx.get("tz"))
        if ctx["now"] < due_at:
            return False
        return not (spec.get("ack") and acked is not None and acked >= due_at)
    if not (spec.get("ack") and every):
        return True
    return acked is None or (ctx["now"] - acked) >= every


def custom_text(ctx: dict, spec: dict) -> str:
    """The segment's text: static, or the cached output of its command."""
    if spec.get("command"):
        return custom_command_value(ctx, spec)
    text = spec.get("text")
    return text if isinstance(text, str) else ""



# ── custom segment commands: never on the render path ────────────────────────
# A user will eventually write a command that hangs. The bar must not. So a command is NEVER
# run while rendering: the render reads a cached value and, if that value is stale, spawns a
# detached child to refresh it for NEXT time — exactly the shape the usage fetch has used
# since Task 4, lock file and all. The very first render of a new command therefore shows
# nothing, and the one after it shows the value. That is the correct trade: a blank segment
# for a few seconds beats a status line that stops redrawing.
def custom_cache_path() -> Path:
    return data_dir() / "custom-cache.json"


def custom_lock_path(seg_id: str) -> Path:
    return cache_dir() / f"custom-{hashlib.sha256(seg_id.encode()).hexdigest()[:16]}.lock"


def custom_commands_enabled(config: dict | None = None) -> bool:
    """Running a command from the config file is OFF unless deliberately switched on.

    The config is the user's own file, but it is also a file this plugin's own slash commands
    write — and Claude runs those while reading untrusted material. A single gate the user
    turns on once, rather than execution being reachable by any path that can write config,
    is a cheap and proportionate brake. It is NOT a complete answer: anything that can run
    /squirrel-custom can also run the enable command. It raises the bar and makes the
    capability visible, and that is what it claims to do."""
    return bool((config or {}).get("customCommands", False))


def custom_command_ttl(config: dict | None, spec: dict) -> float:
    return _positive_seconds(spec.get("every")) or float(tunable(config, "customTtlSeconds"))


def custom_command_value(ctx: dict, spec: dict) -> str:
    """The cached stdout of this segment's command; "" when there is nothing cached yet.
    Spawns a refresh when the value is stale, and never waits for it."""
    config = ctx["config"]
    if not custom_commands_enabled(config):
        return ""
    cache = ctx.setdefault("custom_cache", None)
    if cache is None:
        cache = load_json(custom_cache_path())
        cache = cache if isinstance(cache, dict) else {}
        ctx["custom_cache"] = cache
    entry = cache.get(spec["id"])
    entry = entry if isinstance(entry, dict) else {}
    value = entry.get("value")
    value = value if isinstance(value, str) else ""
    # Capped on READ as well as on write. The write-side cap only applies to values this
    # version produced; a cache written by an older build (or by hand) still holds a huge
    # string, and every render pays for it until the next refresh happens to overwrite it.
    value = value[:tunable(config, "customValueMaxChars")]
    ttl = custom_command_ttl(config, spec)
    if ctx["now"] - float(entry.get("at") or 0) >= ttl:
        spawn_custom_refresh(spec["id"], ctx["now"], ttl)
    return value


def spawn_custom_refresh(seg_id: str, now: float, ttl: float) -> None:
    """Start `--run-custom <id>` detached, at most one at a time per segment."""
    try:
        lock = custom_lock_path(seg_id)
        try:
            if now - lock.stat().st_mtime < max(ttl, LOCK_MAX_AGE_S):
                return          # a refresh is already in flight (or too recent to retry)
        except OSError:
            pass
        touch_private(lock)
        import subprocess
        subprocess.Popen(
            [sys.executable, os.path.abspath(__file__), "--run-custom", seg_id],
            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            **detach_kwargs(subprocess))
    except Exception:
        pass        # a refresh that cannot start must never disturb the render


def _run_command(command: str, timeout_ms: int, max_bytes: int = 65536) -> tuple:
    """Run one shell command with a hard timeout AND a hard output cap.
    Returns (stdout, returncode, error).

    Both bounds matter, and the byte cap is not paranoia. `communicate()` buffers everything
    the child writes: a command whose endpoint has a bad day and returns a 20MB body cost
    58MB of resident memory and wrote a 20MB cache file that every later render then re-read
    — measured, no attacker involved. The cap stops reading, kills the process group and
    reports truncation instead.

    The timeout is a watchdog rather than `communicate(timeout=)` because a bounded read has
    to be the thing doing the reading. But killing the process group is NOT enough to end
    that read: a grandchild that has `setsid`'d out of the group survives the kill and keeps
    the pipe's write end open, so the read blocks for ever. Measured: the child was still
    sitting in `anon_pipe_read` at t=12s with a 1s timeout, three of them accumulated in 70
    seconds, and the cache was never written — strictly worse than the `communicate(timeout=)`
    it replaced, and exactly the leak this function exists to prevent.

    So the read happens on a daemon thread and we join it with a deadline. If it is still
    blocked when the deadline passes, it is abandoned: this runs in the short-lived detached
    `--run-custom` child, which exits immediately afterwards, and the fd goes with it. The
    thread must never be joined without a timeout for the same reason it exists.

    The child is a process-group leader so the whole group is killed, not just the shell."""
    import signal
    import subprocess
    import threading
    kwargs = {"stdout": subprocess.PIPE, "stderr": subprocess.DEVNULL,
              "stdin": subprocess.DEVNULL, "shell": True}
    if IS_WINDOWS:
        kwargs["creationflags"] = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
    else:
        kwargs["start_new_session"] = True
    try:
        proc = subprocess.Popen(command, **kwargs)
    except Exception as exc:
        return "", 1, f"{type(exc).__name__}: {exc}"

    killed = {"timeout": False}

    def kill_group():
        try:
            if IS_WINDOWS:
                proc.kill()
            else:
                os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
        except (OSError, ProcessLookupError):
            pass

    def on_timeout():
        killed["timeout"] = True
        kill_group()

    deadline = max(0.05, timeout_ms / 1000)
    watchdog = threading.Timer(deadline, on_timeout)
    watchdog.daemon = True
    watchdog.start()

    read = {"raw": b""}

    def read_bounded():
        try:
            # cap + 1 so we can tell "exactly at the cap" from "more than the cap"
            read["raw"] = proc.stdout.read(max(1, max_bytes) + 1) or b""
        except Exception:
            pass

    reader = threading.Thread(target=read_bounded, daemon=True)
    reader.start()
    # A short grace beyond the deadline so the normal case — the kill lands, the last writer
    # closes, the read returns short — is reported as a timeout with whatever it managed to
    # read, rather than as an abandoned read.
    reader.join(deadline + 0.5)
    watchdog.cancel()
    stuck = reader.is_alive()
    if stuck:
        killed["timeout"] = True
        kill_group()            # again: something outlived the first one
    raw = read["raw"]
    truncated = len(raw) > max_bytes
    if truncated:
        kill_group()            # stop it producing more; we are not going to read it
    if not stuck:
        # Only safe once the reader is done with it. A stuck reader still holds this fd, and
        # closing it under another thread is how you get a read against a recycled
        # descriptor. The abandoned thread and its fd die with this process.
        try:
            proc.stdout.close()
        except Exception:
            pass
    try:
        proc.wait(timeout=1)
    except Exception:
        kill_group()
    text = raw[:max_bytes].decode("utf-8", "replace")
    if killed["timeout"]:
        return "", 1, "timed out"
    if truncated:
        return text, proc.returncode or 0, f"output over {max_bytes} bytes — truncated"
    return text, proc.returncode, None


def run_custom(seg_id: str, now: float | None = None) -> dict:
    """Run one custom segment's command and cache its first line of stdout. Runs in the
    detached child, never in the render. Failure is recorded, not raised: the segment shows
    its previous value (or nothing) and /squirrel-custom reports the error."""
    now = time.time() if now is None else now
    config = load_config()
    spec = next((c for c in custom_specs(config) if c["id"] == seg_id), None)
    entry: dict = {"at": now}
    if spec is None or not spec.get("command"):
        entry["error"] = "no such command segment"
    elif not custom_commands_enabled(config):
        entry["error"] = "command segments are switched off"
    else:
        stdout, code, error = _run_command(spec["command"], tunable(config, "customTimeoutMs"),
                                           tunable(config, "customOutputMaxBytes"))
        out = stdout.strip().splitlines()
        # Capped independently of the read: a segment shows one short line, so anything
        # longer is never going to be displayed and has no business being written to a cache
        # that every render re-reads.
        limit = tunable(config, "customValueMaxChars")
        entry["value"] = sanitise(out[0])[:limit] if out else ""
        if error:
            entry["error"] = error
        elif code != 0:
            entry["error"] = f"exit {code}"
    cache = load_json(custom_cache_path())
    cache = cache if isinstance(cache, dict) else {}
    previous = cache.get(seg_id)
    if entry.get("error") and not entry.get("value") and isinstance(previous, dict):
        # A failed or empty refresh keeps whatever was last known good, so a flaky command
        # blanks the segment rather than the bar flickering between a value and nothing.
        if isinstance(previous.get("value"), str) and previous["value"]:
            entry["value"] = previous["value"]
    cache[seg_id] = entry
    write_cache(custom_cache_path(), cache)
    return entry


# ── tasks: squirrel writes jobs, it never acts ───────────────────────────────
# The architecture the user asked for, and the better one: "the agent will continuously
# watch the queue, so we will write to the queue and he will act". Squirrel evaluates a
# condition and writes a JOB. What can be done with a job is the reader's business, not
# ours — which is why the verb set is a vocabulary here rather than a dispatch table.
#
# Evaluation happens during the render because that is the only place the metrics exist,
# and it is free: they are already computed for the bar rules. Nothing that could block
# happens there. Writing a job is one small atomic write, exactly like a session sample,
# and the reader is spawned detached like --fetch.
TASK_VERBS = ("notify", "run", "ack", "snooze")
TASKS_MAX = 32
TASK_COOLDOWN_MIN = 60          # a watcher with no brake is the mistake that costs money
TASK_COOLDOWN_DEFAULT = 1800


def queue_dir() -> Path:
    return shared_dir() / "queue"


def task_record_path(task_id: str) -> Path:
    """One file per task: its current job and when it last fired. Per-task rather than one
    queue file so four live sessions never contend on the same inode."""
    safe = re.sub(r"[^A-Za-z0-9_-]", "-", str(task_id))[:64] or "task"
    return queue_dir() / f"{safe}.json"


def task_lock_path(task_id: str) -> Path:
    return task_record_path(task_id).with_suffix(".lock")


def task_specs(config: dict | None) -> list[dict]:
    """Every configured task, validated, with `error` set on the ones that are unusable.

    Never silently dropped: a task that does not parse has to be findable, or the user is
    left with a watcher that simply never fires and no way to learn why — the same reason
    --show-rules reports its ignored rules."""
    raw = (config or {}).get("tasks")
    out: list[dict] = []
    for entry in (raw if isinstance(raw, list) else [])[:TASKS_MAX]:
        if not isinstance(entry, dict):
            continue
        spec = {"id": str(entry.get("id") or "").strip(), "when": entry.get("when"),
                "do": str(entry.get("do") or "").strip(), "text": entry.get("text"),
                "target": str(entry.get("target") or "idle").strip(),
                "warn": max(0, int(_positive_seconds(entry.get("warn")) or 0)),
                "cancel": bool(entry.get("cancel", True)), "error": None}
        cooldown = int(_positive_seconds(entry.get("cooldown")) or 0)
        spec["cooldown"] = max(TASK_COOLDOWN_MIN, cooldown or TASK_COOLDOWN_DEFAULT)
        if not spec["id"]:
            spec["error"] = "a task needs an id"
        elif not spec["when"]:
            spec["error"] = f"task '{spec['id']}' has no condition"
        elif spec["do"] not in TASK_VERBS:
            spec["error"] = (f"task '{spec['id']}': '{spec['do'] or '(none)'}' is not an action"
                             f" — one of: {', '.join(TASK_VERBS)}")
        else:
            parsed = parse_condition(spec["when"])
            if isinstance(parsed, str):
                spec["error"] = f"task '{spec['id']}': {parsed}"
        out.append(spec)
    return out


def task_action_allowed(config: dict | None, verb: str) -> bool:
    """Per-action opt-in, checked on the WRITING side. A job squirrel is not permitted to
    create is never created — never written and then declined by a reader, because a job on
    disk is a thing that exists and something else may one day read it."""
    allowed = (config or {}).get("taskActions")
    return bool(isinstance(allowed, dict) and allowed.get(verb))


def job_is_actionable(job: dict, now: float) -> bool:
    return bool(job) and job.get("state") == "pending" and now >= float(job.get("due") or 0)


def _take_task_lock(task_id: str):
    """Same discipline as the click lock: O_CREAT|O_EXCL, and a lock whose owner is gone is
    stale rather than permanent. With four live sessions rendering every few seconds, this is
    the only thing between one job and four."""
    path = task_lock_path(task_id)
    try:
        mkdir_private(path.parent)
    except OSError:
        return None
    for attempt in (1, 2):
        try:
            handle = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
            os.write(handle, str(os.getpid()).encode())
            os.close(handle)
            return path
        except FileExistsError:
            try:
                owner = int(path.read_text().strip() or 0)
            except (OSError, ValueError):
                owner = 0
            if owner and owner != os.getpid() and _pid_alive(owner):
                return None
            if attempt == 1:
                try:
                    path.unlink(missing_ok=True)
                except OSError:
                    return None
        except OSError:
            return None
    return None


def _release_task_lock(path) -> None:
    try:
        Path(path).unlink(missing_ok=True)
    except OSError:
        pass


METRIC_HELP = {
    "ctx.pct":        "percent of the context window used",
    "session.pct":    "percent of the 5-hour limit used, account-wide",
    "weekly.pct":     "percent of the weekly all-models limit used",
    "share.pct":      "this session's share of the live sessions' attributed usage",
    "cost.usd":       "this session's cost so far, in dollars",
    "duration.min":   "this session's wall-clock duration, in minutes",
    "lines.added":    "lines added by this session",
    "lines.removed":  "lines removed by this session",
    "tokens.session": "tokens this session has consumed",
    "git.dirty":      "true when the working tree has uncommitted changes",
    "git.ahead":      "commits ahead of the upstream branch",
    "git.branch":     "current branch name",
    "repo.name":      "repository or project directory name",
    "model.name":     "model display name, e.g. 'Claude Opus 5'",
    "effort.level":   "reasoning effort level when the model reports one",
    "idle.secs":      "seconds spent waiting in a painted state; unknown while working",
    "state":          "working / idle / permission / subagents / waiting-you / waiting-tools",
    "pending.tools":  "outstanding tool calls, read from the transcript",
    "pending.agents": "outstanding agent/Task calls, read from the transcript",
    "nudged":         "true when this session's most recent turn was a squirrel nudge",
}


def cmd_show_metrics(_arg: str = "") -> str:
    """Every metric, its live value and what it means — plus the task vocabulary.

    Exists so Claude can DISCOVER what it may write rather than recall it. A skill that lists
    metric names is a snapshot of whatever version it was written against; this is the version
    actually running, which is the only one that can refuse a condition.

    Same honesty as --show-rules: a metric only knowable during a live render prints `?`
    rather than a plausible number this invocation cannot actually know."""
    cfg = load_config()
    ctx = _show_rules_ctx(cfg)
    lines = ["metrics — usable in any task `when` or bar rule condition:"]
    for name in sorted(METRICS):
        if name in LIVE_ONLY_METRICS:
            shown = "?"
        else:
            value = metric_value(ctx, name)
            shown = "?" if value is None else str(value)
        lines.append(f"  {name:<16} {shown:<28} {METRIC_HELP.get(name, '')}")
    lines += [
        "",
        "  '?' means this invocation cannot know the value — either nothing has reported it",
        "  yet, or it is only knowable during a live render. A condition over an unknown",
        "  metric NEVER fires, `!=` included.",
        "",
        "condition: <metric> <op> <value>   (ops: >= <= == != > <), or a bare <metric>",
        f"actions:   {', '.join(TASK_VERBS)}   (each needs its own opt-in: --task-actions <verb> on)",
        "task fields:",
        "  id        a name you choose; also the queue file's name",
        "  when      the condition above",
        "  do        one of the actions above",
        "  text      the exact message a `notify` sends — squirrel never composes one",
        "  target    idle (any idle session), or a session name from `claude agents --json`",
        f"  cooldown  seconds between firings (minimum {TASK_COOLDOWN_MIN}, default {TASK_COOLDOWN_DEFAULT})",
        "  warn      seconds of visible countdown before it fires (0 = fire at once)",
        "  cancel    whether that countdown can be clicked to cancel this firing",
        "",
        "check a task before trusting it:  /squirrel-task test <id>",
    ]
    return "\n".join(lines)


TASK_FIELDS = ("when", "do", "text", "target", "cooldown", "warn", "cancel")


def _task_field_args(arg: str) -> tuple:
    """`key=value` pairs, where a value runs GREEDILY to the next recognised `key=`.

    The first version stopped at the first space, so `when=weekly.pct > 85` stored
    `weekly.pct` and silently dropped the comparison — and then accepted, listed and
    dry-ran the result as a perfectly valid but DIFFERENT task. A bare metric is
    truthy-tested, so that task plausibly fires constantly, on the surface Claude drives
    unattended, for an action that costs money.

    Quoting is not the answer: this arrives already inside a quoted `$ARGUMENTS`, nesting
    quotes is a trap anyone will fall into repeatedly, and EVERY real condition contains a
    space. So the natural unquoted form is the one that works, and quoting still works too.

    A token is only a new field if its key is one we recognise, so `text=a=b c=d` keeps its
    equals signs instead of inventing a field called `c`."""
    import shlex
    try:
        tokens = shlex.split(arg)
    except ValueError:
        return None, "unbalanced quotes"
    fields: dict = {}
    rest: list = []
    current = None
    for token in tokens:
        key, sep, value = token.partition("=")
        if sep and key.strip() in TASK_FIELDS:
            current = key.strip()
            fields[current] = value
        elif current is not None:
            fields[current] = f"{fields[current]} {token}".strip()
        else:
            rest.append(token)
    return (rest, fields)


def cmd_task(arg: str = "") -> str:
    """`/squirrel-task add|set|remove|test <id> [field=value ...]` — surgical, validated,
    and nothing written on error, the same discipline as /squirrel-layout.

    `test` is the one that matters most: Claude cannot see the bar, so without a dry run it
    writes a task, cannot observe it, and tells the user it works."""
    parts, fields = _task_field_args(arg)
    if parts is None:
        return f"{fields}; nothing was changed"
    action = (parts[0] if parts else "").lower()
    task_id = parts[1] if len(parts) > 1 else ""
    if action not in ("add", "set", "remove", "test"):
        return ("usage: /squirrel-task add|set|remove|test <id> [when=..] [do=..] [text=..] "
                "[target=..] [cooldown=..] [warn=..]; nothing was changed")
    if not task_id:
        return f"usage: /squirrel-task {action} <id> ...; nothing was changed"

    config = load_config()
    tasks = [dict(t) for t in (config.get("tasks") or []) if isinstance(t, dict)]
    index = next((i for i, t in enumerate(tasks) if str(t.get("id")) == task_id), None)

    if action == "test":
        return _task_dry_run(config, task_id)
    if action == "remove":
        if index is None:
            return f"no task '{task_id}'; nothing was changed"
        tasks.pop(index)
        save_config(dict(config, tasks=tasks))
        return f"removed task '{task_id}'"
    if action == "add" and index is not None:
        return f"task '{task_id}' already exists — use `set`; nothing was changed"
    if action == "set" and index is None:
        return f"no task '{task_id}'; nothing was changed"
    if action == "add" and len(tasks) >= TASKS_MAX:
        return f"too many tasks (limit {TASKS_MAX}); nothing was changed"

    entry = dict(tasks[index]) if index is not None else {"id": task_id}
    for key in ("when", "do", "text", "target"):
        if key in fields:
            entry[key] = fields[key]
    for key in ("cooldown", "warn"):
        if key in fields:
            try:
                entry[key] = int(fields[key])
            except ValueError:
                return f"'{fields[key]}' is not a number of seconds; nothing was changed"
    if "cancel" in fields:
        entry["cancel"] = fields["cancel"].lower() in ("on", "true", "yes", "1")

    candidate = [dict(spec) for spec in tasks]
    if index is None:
        candidate.append(entry)
    else:
        candidate[index] = entry
    checked = next((sp for sp in task_specs({"tasks": candidate}) if sp["id"] == task_id), None)
    if checked is None:
        return f"could not read task '{task_id}'; nothing was changed"
    if checked.get("error"):
        return f"{checked['error']}; nothing was changed"
    if not task_action_allowed(config, checked["do"]):
        # Refused rather than created dormant: a task that silently never acts is worse than
        # one that was never made, because nothing tells the user which it is.
        return (f"'{checked['do']}' is not enabled, so this task would never act — "
                f"run `/squirrel-task-actions {checked['do']} on` first "
                f"(--task-actions {checked['do']} on); nothing was changed")
    save_config(dict(config, tasks=candidate))
    return (f"task '{task_id}': when {checked['when']!r} -> {checked['do']}"
            f" (cooldown {checked['cooldown']}s"
            + (f", warn {checked['warn']}s" if checked["warn"] else "") + ")")


def _task_dry_run(config: dict, task_id: str) -> str:
    """Would this task fire right now, and if not, which clause failed and what was the
    metric's actual value?"""
    spec = next((sp for sp in task_specs(config) if sp["id"] == task_id), None)
    if spec is None:
        return f"no task '{task_id}'"
    if spec.get("error"):
        return f"task '{task_id}' is not usable: {spec['error']}"
    ctx = _show_rules_ctx(config)
    parsed = parse_condition(spec["when"])
    metric = parsed[0]
    live_only = metric in LIVE_ONLY_METRICS
    value = None if live_only else metric_value(ctx, metric)
    shown = "?" if value is None else repr(value)
    lines = [f"task '{task_id}': when {spec['when']!r} -> {spec['do']}"]
    if live_only:
        lines.append(f"  {metric} = ? — only knowable during a live render, so this check")
        lines.append("  cannot say. It is not a broken task; run the bar and watch it.")
        return "\n".join(lines)
    if value is None:
        lines.append(f"  {metric} = ? (unknown) — WOULD NOT FIRE. An unknown metric never")
        lines.append("  matches, `!=` included.")
        return "\n".join(lines)
    holds = condition_holds(ctx, spec["when"])
    lines.append(f"  {metric} = {shown}")
    if holds:
        room = nudge_budget(time.time(), config)["room"] if spec["do"] == "notify" else 1
        if room <= 0:
            lines.append("  the condition HOLDS, but the nudge ceiling is reached — no job")
            lines.append("  would be created. See /squirrel-config.")
        else:
            lines.append(f"  the condition HOLDS — WOULD FIRE (then quiet for {spec['cooldown']}s)")
    else:
        lines.append(f"  the condition does not hold — WOULD NOT FIRE")
    return "\n".join(lines)


def cmd_tasks(_arg: str = "") -> str:
    """Every task, its condition, its queue state, and the nudge budget — the listing the
    user reads when a watcher did not do what they expected, so it says plainly when it
    cannot know something."""
    cfg = load_config()
    specs = task_specs(cfg)
    if not specs:
        return ("no tasks configured. /squirrel-task add <id> when='<condition>' do=notify "
                "text='...'  —  /squirrel-metrics lists what you can write conditions on.")
    budget = nudge_budget(time.time(), cfg)
    lines = ["tasks:"]
    for spec in specs:
        if spec.get("error"):
            lines.append(f"  {spec['id'] or '(no id)':<16} IGNORED — {spec['error']}")
            continue
        record = load_json(task_record_path(spec["id"]))
        record = record if isinstance(record, dict) else {}
        job = record.get("job") or {}
        last = record.get("last_fired")
        state = job.get("state") or "—"
        when_last = fmt_relative(time.time() - float(last)) + " ago" if last else "never"
        lines.append(f"  {spec['id']:<16} when {spec['when']!r} -> {spec['do']}")
        lines.append(f"  {'':<16} queue: {state}   last fired: {when_last}"
                     f"   cooldown: {spec['cooldown']}s")
    lines += ["", f"nudges: {budget['used']}/{budget['ceiling']} used this hour"
                  + ("  (ceiling reached — nudges paused)" if budget["room"] <= 0 else ""),
              "check one before trusting it: /squirrel-task test <id>"]
    return "\n".join(lines)


def cmd_task_actions(arg: str = "") -> str:
    """Per-action opt-in. Off by default because a task that reaches outside the bar is not
    something to enable by accident."""
    parts = (arg or "").split()
    if len(parts) != 2 or parts[1].lower() not in ("on", "off"):
        enabled = [v for v in TASK_VERBS if task_action_allowed(load_config(), v)]
        return (f"task actions enabled: {', '.join(enabled) or 'none'}\n"
                f"usage: --task-actions <{'|'.join(TASK_VERBS)}> <on|off>")
    verb, setting = parts[0], parts[1].lower() == "on"
    if verb not in TASK_VERBS:
        return f"'{verb}' is not an action — one of: {', '.join(TASK_VERBS)}; nothing was changed"
    config = load_config()
    actions = dict(config.get("taskActions") or {})
    actions[verb] = setting
    save_config(dict(config, taskActions=actions))
    return f"task action '{verb}' is {'ON' if setting else 'OFF'}"


def nudge_ledger_path() -> Path:
    """Deliberately a sibling of the queue, not inside it. Everything in queue/ is a task
    record that a reader globs and interprets; a ledger sitting among them is one refactor
    away from being read as a job."""
    return shared_dir() / "queue-budget.json"


def nudge_budget(now: float, config: dict | None = None) -> dict:
    """How many nudges have been created in the last hour, against the ceiling.

    A budget the user cannot observe is not a budget, so this is what --show-config and
    --show-tasks read; `tripped_at` is kept so "why did my watcher go quiet?" has an answer
    on the bar rather than in a debugging session."""
    ceiling = tunable(config, "nudgeMaxPerHour")
    data = load_json(nudge_ledger_path())
    data = data if isinstance(data, dict) else {}
    stamps = [t for t in (data.get("at") or []) if isinstance(t, (int, float)) and now - t < 3600]
    return {"used": len(stamps), "ceiling": ceiling, "at": stamps,
            "tripped_at": data.get("tripped_at"),
            "room": max(0, ceiling - len(stamps))}


def _charge_nudge(now: float, config: dict | None, tripped: bool = False) -> None:
    try:
        budget = nudge_budget(now, config)
        stamps = budget["at"] if tripped else budget["at"] + [now]
        write_cache(nudge_ledger_path(),
                    {"at": stamps[-400:],
                     "tripped_at": now if tripped else budget.get("tripped_at")})
    except Exception:
        pass


def evaluate_tasks(ctx: dict) -> list[dict]:
    """Evaluate every task against this render's metrics and queue the ones that are due.

    Returns the jobs written (for tests and --show-tasks); never raises, never blocks, and
    does nothing at all when no tasks are configured."""
    jobs: list[dict] = []
    try:
        config = ctx.get("config") or {}
        specs = [sp for sp in task_specs(config) if not sp.get("error")]
        if not specs:
            return jobs
        now = ctx["now"]
        for spec in specs:
            if not task_action_allowed(config, spec["do"]):
                continue        # not opted in: never create the job
            if not condition_holds(ctx, spec["when"]):
                continue
            if spec["do"] == "notify" and _ctx_transcript(ctx) \
                    and _ctx_transcript(ctx).get("nudged"):
                # This session's most recent user turn was itself a nudge, so nudging back is
                # the ping-pong. Precision where it is available; the ceiling below is what
                # covers the cases where it is not. Note the explicit transcript check —
                # UNKNOWN is not "was nudged", and a missing transcript must not silence
                # every watcher on the machine.
                continue
            if spec["do"] == "notify" and nudge_budget(now, config)["room"] <= 0:
                # The blast-radius backstop. Cooldowns stop the loops we anticipated; this
                # bounds the ones we did not, whatever caused them — causality is not
                # observable from here (nothing records WHY a session woke), so the honest
                # move is to bound the damage rather than guess at the cause. Recorded, and
                # reported by --show-config, because a watcher that has gone quiet needs to
                # be able to say why.
                _charge_nudge(now, config, tripped=True)
                continue
            job = _queue_job(spec, ctx, now)
            if job and spec["do"] == "notify":
                _charge_nudge(now, config)
            if job:
                jobs.append(job)
        if jobs:
            spawn_queue_reader()    # a no-op today; the reader is the next piece
    except Exception:
        pass        # a task must never be able to break the bar
    return jobs


def _queue_job(spec: dict, ctx: dict, now: float) -> dict | None:
    """Write one job under the task's lock, honouring its cooldown. None when it did not fire."""
    lock = _take_task_lock(spec["id"])
    if lock is None:
        return None             # another session is deciding this same task right now
    try:
        path = task_record_path(spec["id"])
        record = load_json(path)
        record = record if isinstance(record, dict) else {}
        existing = record.get("job")
        if existing and existing.get("state") in ("pending", "claimed"):
            return None         # already queued; a task has at most one job outstanding
        last = float(record.get("last_fired") or 0)
        if last and now - last < spec["cooldown"]:
            return None
        job = {
            "job": f"{spec['id']}-{int(now)}-{os.getpid()}",
            "task": spec["id"], "do": spec["do"],
            "text": sanitise(spec.get("text")) or "", "target": spec["target"],
            "session": ctx["status"].get("session_id"),
            "account": account_hash(ctx.get("email")),
            "created": now, "due": now + spec["warn"], "state": "pending",
            "cancellable": spec["cancel"], "fired_at": None, "claimed_by": None,
        }
        record = {"task": spec["id"], "last_fired": record.get("last_fired"), "job": job}
        write_cache(path, record)
        return job
    except Exception:
        return None
    finally:
        _release_task_lock(lock)


def _job_record(job_id: str):
    """(path, record) for the task holding this job, or (None, None)."""
    task = str(job_id).rsplit("-", 2)[0]
    path = task_record_path(task)
    record = load_json(path)
    if isinstance(record, dict) and (record.get("job") or {}).get("job") == job_id:
        return path, record
    return None, None


def claim_job(job_id: str, now: float) -> bool:
    """Claim a job for action. True for exactly one caller.

    A claim records the claiming pid, so a reader that dies mid-job leaves the job runnable
    rather than stuck: the next reader sees a claim whose owner is gone and takes it over.
    That is the same reasoning as the click lock — one crash must not disable a feature."""
    lock = _take_task_lock(str(job_id).rsplit("-", 2)[0])
    if lock is None:
        return False
    try:
        path, record = _job_record(job_id)
        if not record:
            return False
        job = record["job"]
        if not job_is_actionable(job, now):
            if job.get("state") != "claimed":
                return False
            owner = job.get("claimed_by")
            if owner and _pid_alive(int(owner)):
                return False        # genuinely being worked on
        job["state"] = "claimed"
        job["claimed_by"] = os.getpid()
        write_cache(path, record)
        return True
    except Exception:
        return False
    finally:
        _release_task_lock(lock)


def mark_job(job_id: str, state: str, now: float) -> None:
    """Record a job's outcome. `done` and `cancelled` are both terminal; only `done` starts
    the cooldown, so cancelling a firing delays nothing and the task lives on unchanged."""
    lock = _take_task_lock(str(job_id).rsplit("-", 2)[0])
    if lock is None:
        return
    try:
        path, record = _job_record(job_id)
        if not record:
            return
        job = record["job"]
        job["state"] = state
        job["claimed_by"] = None if state == "pending" else job.get("claimed_by")
        job["fired_at"] = now if state == "done" else job.get("fired_at")
        if state in ("done", "cancelled", "refused"):
            # A cancellation starts the cooldown too, and that is the whole point of the
            # button. Without it the condition still holds a second later, the task
            # re-queues immediately, and "cancel" means nothing — the user dismisses a
            # nudge and it reappears. Cancel suppresses THIS firing; the task itself is
            # untouched and fires again once its cooldown has passed.
            record["last_fired"] = now
        record["job"] = None if state in ("done", "cancelled", "refused") else job
        record["last_job"] = job            # kept for --show-tasks; one entry, not a log
        write_cache(path, record)
    except Exception:
        pass
    finally:
        _release_task_lock(lock)


def list_agent_sessions() -> list[dict]:
    """Live interactive Claude Code sessions on THIS machine, from `claude agents --json`.

    The enumeration source is a safety property, not a convenience. Verified from inside a
    subagent rather than reasoned about: run as a subagent whose own pid was 2814901, this
    returned two entries and neither was mine. What it returned was the PARENT — pid 22551,
    the interactive session hosting that subagent — with status "busy", busy precisely
    because the subagent was running.

    So a subagent has no entry here to filter out. It is only ever visible as its host's
    status, which the idle rule then excludes as well. Two independent reasons a nudge cannot
    reach one.

    NEVER change this to `ListAgents`. That tool DOES see subagents, and "use the tool that
    lists agents" is exactly the tidy-looking refactor that would silently start messaging
    them. The `kind == "interactive"` filter stays as belt and braces in case a future
    version of the CLI grows other kinds.

    Runs a subprocess, so it belongs to the detached reader and must never be called from a
    render."""
    try:
        out, code, error = _run_command("claude agents --json", 5000, 256 * 1024)
        if code != 0 or error:
            return []
        data = json.loads(out or "null")
        if not isinstance(data, list):
            return []
        return [row for row in data
                if isinstance(row, dict) and row.get("kind") == "interactive"]
    except Exception:
        return []           # no CLI, no permission, a changed shape: reach nobody


def resolve_notify_targets(job: dict, sessions: list[dict]) -> list[dict]:
    """Which live sessions this job may be delivered to, decided at ACTION time.

    Resolved here rather than when the job was written because a session can exit in between,
    and silently picking a different recipient is worse than doing nothing. Four rules, all of
    which subtract and none of which add:

      * never the session that created the job — a watcher waking itself is a loop whose only
        brake is a cooldown;
      * only `status == "idle"` — interrupting a working session is noise that arrives as a
        turn and costs tokens;
      * a named target that has vanished, or is busy, means NO delivery, never a substitute;
      * nothing is ever broadcast to a session outside this machine, because the enumeration
        is local by construction.
    """
    origin = job.get("session")
    named = (job.get("target") or "idle").strip()
    live = [s for s in sessions
            if s.get("sessionId") != origin and s.get("name") != origin]
    idle = [s for s in live if s.get("status") == "idle"]
    if named in ("", "idle", "any"):
        return idle
    if named == "self":
        return []           # deliberately unreachable: see rule 1
    return [s for s in idle if s.get("name") == named or s.get("sessionId") == named]


def send_notify(target: dict, text: str) -> bool:
    """Deliver one job's text to one live session. True when it was accepted.

    The text is the configured string and nothing else — squirrel never composes a word of
    it — and it arrives labelled as a cross-session message, not as if the user typed it.
    Isolated in its own function because it is the ONLY place the plugin reaches out of its
    own process to something that is not a file."""
    import shlex        # detached-child only; the render never imports it
    name = str(target.get("name") or "").strip()
    if not name or not text:
        return False
    # The marker rides with the text so the receiving session's transcript records it — that
    # is the whole protocol. See NUDGE_MARKER.
    body = f"{text} {NUDGE_MARKER}"
    prompt = f"Send this message to the session named {name}, verbatim, and then stop: {body}"
    out, code, error = _run_command(
        f"claude -p {shlex.quote(prompt)} --model claude-haiku-4-5-20251001", 120_000, 64 * 1024)
    return code == 0 and not error


def drain_queue(now: float | None = None) -> int:
    """Action every job that is due. Returns how many were actioned.

    Runs only in the detached `--drain-queue` child. A job it cannot action is left exactly
    as it was, so a transient failure is a retry rather than a lost nudge; a job whose verb it
    does not recognise is REFUSED and recorded, never guessed at."""
    now = time.time() if now is None else now
    actioned = 0
    try:
        records = sorted(queue_dir().glob("*.json"))
    except OSError:
        return 0
    sessions = None
    for path in records:
        try:
            record = load_json(path)
            job = (record or {}).get("job")
            if not job_is_actionable(job, now):
                continue
            if not claim_job(job["job"], now):
                continue        # another reader has it
            verb = job.get("do")
            if verb not in TASK_VERBS:
                mark_job(job["job"], "refused", now)
                continue
            if verb != "notify":
                # The other verbs are the reader's to grow into. Refused rather than silently
                # dropped, so `/squirrel-tasks` can say why nothing happened.
                mark_job(job["job"], "refused", now)
                continue
            if sessions is None:
                sessions = list_agent_sessions()
            targets = resolve_notify_targets(job, sessions)
            if not targets:
                mark_job(job["job"], "done", now)   # nobody to tell: finished, not pending
                continue
            sent = any(send_notify(target, job.get("text") or "") for target in targets)
            if sent:
                actioned += 1
                mark_job(job["job"], "done", now)
            else:
                mark_job(job["job"], "pending", now)     # retryable
        except Exception:
            continue        # one bad job must never stop the queue draining
    return actioned


def spawn_queue_reader() -> None:
    """Start the on-demand reader, detached, at most one at a time. Zero cost when nothing
    is queued, because nothing is spawned when nothing is queued."""
    try:
        import subprocess
        subprocess.Popen(
            [sys.executable, os.path.abspath(__file__), "--drain-queue"],
            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            **detach_kwargs(subprocess))
    except Exception:
        pass        # a reader that cannot start must never disturb the render


def shipped_layout() -> list[list[str]]:
    """The arrangement the plugin ships, read straight off the registry. Mirrored into
    defaults/config.json (and held equal to it by LayoutDefaultsTests)."""
    by_line: dict[int, list[Segment]] = {}
    for seg in SEGMENTS.values():
        by_line.setdefault(seg.line, []).append(seg)
    return [[s.id for s in sorted(by_line[n], key=lambda s: s.order)] for n in sorted(by_line)]


def segment_priority(config: dict | None, seg_id: str) -> int | None:
    """The segment's drop priority, `segmentPriority` override first. None = never dropped."""
    seg = all_segments(config).get(seg_id)
    if seg is None:
        return None
    overrides = (config or {}).get("segmentPriority")
    raw = overrides.get(seg_id) if isinstance(overrides, dict) else None
    if isinstance(raw, str) and raw.strip().lower() == "always":
        return None
    if isinstance(raw, bool) or not isinstance(raw, (int, float)):
        return seg.priority
    return int(raw)


def resolve_layout(config: dict | None = None) -> list[list[str]]:
    """The layout actually rendered: the user's `layout` (or the shipped one), with unknown
    ids skipped — a plugin downgrade must never break someone's bar — and segments switched
    off via the legacy `segments` map folded out. Protected segments are re-inserted at their
    shipped position if a hand-edited config lost them."""
    config = config or {}
    known = all_segments(config)
    raw = config.get("layout")
    if not (isinstance(raw, list) and raw and all(isinstance(line, list) for line in raw)):
        raw = shipped_layout()
    lines = [[i for i in line
              if isinstance(i, str) and i in known
              and (known[i].protected or segment_enabled(config, i))]
             for line in raw]
    placed = {i for line in lines for i in line}
    # Protected segments are reinstated if a hand-edited layout lost them; a CUSTOM segment
    # is placed automatically the first time it appears, because "I added a reminder and
    # nothing happened" is a worse default than "it turned up where I said". Turning it off
    # still works the usual way, through /squirrel-show.
    for seg in known.values():
        if seg.id in placed:
            continue
        auto = seg.protected or (seg.id not in SEGMENTS and segment_enabled(config, seg.id))
        if not auto:
            continue
        while len(lines) <= seg.line:
            lines.append([])
        target = lines[seg.line]
        at = len([i for i in target if known[i].order < seg.order])
        target.insert(at, seg.id)
    return lines


def _produce(ctx: dict, seg: Segment) -> str | None:
    """Render one segment, memoised for this render. The cache key carries only the detail
    options the producer actually reads, so a producer that cannot vary with the pressure
    level runs once no matter how many widths _fit() tries — this is what keeps the render
    path at one git fork instead of the 27 the tier search used to cause."""
    key = (seg.id, tuple(ctx["opts"][k] for k in seg.details))
    cache = ctx["cache"]
    if key not in cache:
        # ── the boundary seam ──────────────────────────────────────────────
        # Every run's text is PLAIN here, which is what makes sanitising it total: strip the
        # dangerous characters from text carrying no escapes of its own, THEN style it. Since
        # Task 17 there is no exception — the activity chip, the last fragment that arrived
        # pre-rendered, is ordinary runs like everything else.
        runs = [(sanitise(text), sgr) for text, sgr in as_runs(seg.render(ctx))]
        runs = [r for r in runs if r[0]]        # a run sanitised down to nothing renders nothing
        rendered = style_runs(runs, segment_style(ctx["config"], seg.id), ctx.get("banner_bg"))
        # OSC-8 goes on LAST, after the sanitiser has run: it is the one escape sequence we
        # emit around untrusted text on purpose, so it must be added where the sanitiser can
        # no longer strip it — and its URL is scheme-checked and sanitised by safe_link().
        # Terminals without OSC-8 support ignore it and show the text unchanged.
        # An explicit `link` wins; otherwise a `click` action generates one, but only when a
        # listener is already live (click_url returns None and spawns one for next time).
        link = (safe_link(seg.link) if seg.link else
                click_url(ctx, seg.id, seg.click) if seg.click else None) if rendered else None
        if link:
            rendered = f"\033]8;;{link}\033\\{rendered}\033]8;;\033\\"
        cache[key] = rendered
    return cache[key]


def _render_line(ctx: dict, ids: list[str], level: int) -> str:
    """One layout line at one pressure level: produce, drop what the level sheds, and join —
    DOT within a group, SEP across groups."""
    ctx["opts"] = detail_opts(level, ctx["config"])
    out, prev_group = "", None
    known = all_segments(ctx["config"])
    links = link_levels(ctx["config"], ids, known)
    for seg_id in ids:
        seg = known[seg_id]
        priority = segment_priority(ctx["config"], seg_id)
        if priority is not None and priority <= level:
            continue
        text = _produce(ctx, seg)
        if not text:
            continue
        shed_at = links.get(seg_id)
        if shed_at is not None and level >= shed_at:
            # The link, not the segment: the text stays exactly as it was, only its OSC-8
            # wrapper goes. Stripping it here rather than in _produce() keeps the producer
            # cache keyed on what the producer actually reads.
            text = _OSC_RE.sub("", text)
        if out:
            # A segment may override the punctuation its group would give it — this is what
            # makes a freely reordered layout punctuate sensibly instead of inheriting a
            # separator from whichever group it happens to belong to.
            override = segment_style(ctx["config"], seg_id).get("sep")
            if override in SEPARATORS:
                out += SEPARATORS[override]
            else:
                out += DOT if (seg.group is not None and seg.group == prev_group) else SEP
        out += text
        prev_group = seg.group
    return out


def _fit(ctx: dict, ids: list[str], width: int) -> str:
    """Raise the pressure level until the line fits; if nothing fits, return the most reduced
    one and let it overflow (what the old tier table's last row did)."""
    priorities = [segment_priority(ctx["config"], i) for i in ids]
    levels = {p for p in priorities if p is not None}
    levels.update(detail_levels(ctx["config"]))
    # A link is shed one step before its own segment, so those steps are levels of their own —
    # without them the search would jump straight past the level that drops the link and take
    # the segment instead.
    levels.update(p for p in link_levels(ctx["config"], ids, all_segments(ctx["config"])).values()
                  if p is not None and p >= 0)
    line = _render_line(ctx, ids, 0)
    for level in sorted(levels):
        if host_len(line) <= width:
            return line
        line = _render_line(ctx, ids, level)
    return line


def render(status: dict, cache: dict, email: str | None, config: dict, now: float,
           tz: tzinfo | None = None, width: int | None = None) -> list[str]:
    """One string per layout line — two by default, but the layout decides. width=None → full
    detail (unconstrained); an int → shed detail per segment priority until each line fits."""
    limits, stale, source = pick_limits(status, cache, now, config, email)
    ctx = _ctx_for(status, limits, stale, source, email, config, now, tz, None)
    # The chip and the idle metrics both want this session's activity record. Read it once
    # into the context and hand it to both, instead of each opening the file for itself.
    ctx["state"] = read_state(status.get("session_id"), now)
    ctx["chip_runs"] = state_segment_runs(status, now, config, ctx["state"])
    # Evaluated before the lines are built, and against the render's OWN ctx rather than a
    # synthetic one, for two reasons: `opacity` in a segment style is a real alpha blend
    # toward whatever is behind the text (the banner, when one is painted), and a bar rule
    # must be able to read every published metric — a rule on `weekly.pct` would never fire
    # if the rule engine were handed a context with no limits in it.
    bg, text, flat = rule_colors(winning_rule(ctx, config))
    ctx["banner_bg"] = bg or None
    layout = resolve_layout(config)
    if width is None:
        lines = [_render_line(ctx, ids, 0) for ids in layout]
    else:
        # Fit to the same budget the banner painter pads to (width - paneReserveColumns), so a
        # painted line can never exceed its own pad target by the columns the margin reserves.
        budget = max(1, width - pane_reserve(config))
        lines = [_fit(ctx, ids, budget) for ids in layout]
    if bg or text:
        pad_width = width if width is not None else terminal_width()
        painted = banner_lines(config, layout)
        lines = [apply_background(line, bg or "", pad_width, text, flat, pane_reserve(config))
                 if index in painted else line
                 for index, line in enumerate(lines)]
    # Last, after the bar is built, so a task can never delay or alter what is displayed.
    # This is the only place the metrics exist, which is why evaluation lives on the render
    # at all; it returns on its first line when no tasks are configured, and everything it
    # might actually DO happens in a detached child. See evaluate_tasks().
    try:
        maybe_fire_phase_alert(ctx)
    except Exception:
        pass            # an alert must never be able to break the bar it is alerting about
    try:
        evaluate_tasks(ctx)
    except Exception:
        pass        # belt as well as braces: evaluate_tasks guards itself, but the bar must
                    # not depend on it continuing to, and this is the boundary to say so at
    return lines


# Every key in defaults/config.json must be settable by a command (guarded by a test).
CONFIGURABLE_KEYS = (
    "accountNames", "accountColors", "repoColors", "defaultAccountColor",
    "segments", "usageFetch", "idlePhases", "idleBackgroundStates", "alarmStates",
    "segmentLabels", "shareFormat", "sessionContribution", "attributionMetric", "sessionsShowModel",
    "layout", "segmentPriority", "segmentStyles", "barRules",
    "customSegments", "customCommands", "clickToAck", "clickTerminals",
    "phaseAlerts", "alertWhenFocused", "bannerLines",
    *TUNABLES,
)

# ── entry point ──────────────────────────────────────────────────────────────
CONFIG_COMMANDS = {
    "--set-nick": cmd_set_nick,
    "--set-color": cmd_set_color,
    "--set-alarm": cmd_set_alarm,
    "--set-bg": cmd_set_bg,
    "--set-bg-after": cmd_set_bg_after,
    "--alerts": cmd_alerts,
    "--phases": cmd_phases,
    "--show-phases": cmd_show_phases,
    "--add-phase": cmd_add_phase,
    "--set-phase": cmd_set_phase,
    "--remove-phase": cmd_remove_phase,
    "--reset-phases": cmd_reset_phases,
    "--set-usage": cmd_set_usage,
    "--show-segment": cmd_set_segment,
    "--set-branch-max": cmd_set_branch_max,
    "--set-reset-warn": cmd_set_reset_warn,
    "--set": cmd_set_tunable,
    "--set-repo-color": cmd_set_repo_color,
    "--set-default-color": cmd_set_default_color,
    "--set-states": cmd_set_states,
    "--set-label": cmd_set_label,
    "--set-share-format": cmd_set_share_format,
    "--set-session-contribution": cmd_set_session_contribution,
    "--set-attribution-metric": cmd_set_attribution_metric,
    "--set-sessions-show-model": cmd_set_sessions_show_model,
    "--layout": cmd_layout,
    "--show-layout": cmd_show_layout,
    "--set-priority": cmd_set_priority,
    "--rules": cmd_rules,
    "--show-rules": cmd_show_rules,
    "--show-metrics": cmd_show_metrics,
    "--task": cmd_task,
    "--tasks": cmd_tasks,
    "--task-actions": cmd_task_actions,
    "--custom": cmd_custom,
    "--show-custom": cmd_show_custom,
    "--custom-commands": cmd_enable_custom_commands,
    "--click": cmd_enable_click,
    "--click-terminals": cmd_set_click_terminals,
    "--ack": cmd_ack,
    "--snooze": cmd_snooze,
    "--config": cmd_config,
    "--style": cmd_style,
    "--show-styles": cmd_show_styles,
}


def use_utf8_io() -> None:
    """Force UTF-8 on this process's stdio, whatever the console code page says.

    Windows Python picks the console code page (cp1252 on this machine) for stdout, and every
    surface this tool has is full of characters cp1252 cannot encode — the bar glyphs, the
    branch arrow, box-drawing separators, and the `→` in the /squirrel-setup confirmation.
    Without this, `--setup` dies with UnicodeEncodeError before it can tell the user anything.
    Found exactly that way on native Windows; see the Task 18 report.

    Applied to stdin too: the status JSON and hook payloads Claude Code pipes in are UTF-8, and
    a cp1252 read mangles any non-ASCII repo, branch or model name in them.

    errors="replace" on the way out, because a status line that prints a `?` in place of one
    glyph is still a status line, while one that raises is a blank bar.
    """
    for stream, kwargs in ((sys.stdin, {"errors": "replace"}),
                           (sys.stdout, {"errors": "replace"}),
                           (sys.stderr, {"errors": "replace"})):
        try:
            stream.reconfigure(encoding="utf-8", **kwargs)
        except (AttributeError, ValueError, OSError):
            pass          # a redirected/replaced stream that cannot be reconfigured; carry on


# Every flag main() dispatches. Anything else beginning with "--" is a mistake, and the one
# thing it must NOT do is fall through to the render path — that blocks on stdin, so a
# mistyped flag left the terminal apparently frozen with no message. Every doc and skill
# recipe invites people to run these by hand, so a near-miss like `--config` for
# `--show-config` is the expected kind of error, not an exotic one.
INTERNAL_FLAGS = ("--fetch", "--state", "--setup", "--run-custom", "--click-listener",
                  "--drain-queue", "--notify", "--help-text")

# Not commands: modifiers that may accompany any editing command. main() strips them before
# dispatch, but they still have to be KNOWN, or the unknown-flag guard refuses them first.
MODIFIER_FLAGS = ("--here",)


def known_flags() -> tuple:
    return tuple(sorted(set(CONFIG_COMMANDS) | set(NON_CONFIG_COMMANDS)
                        | set(INTERNAL_FLAGS) | set(MODIFIER_FLAGS)))


def unknown_flag_message(flag: str) -> str:
    """An error that names the closest real flag, because that is almost always the fix."""
    import difflib
    close = difflib.get_close_matches(flag, known_flags(), n=1, cutoff=0.5)
    lines = [f"unknown option '{flag}'"]
    if close:
        lines.append(f"did you mean '{close[0]}'?")
    lines.append("run --help-text for every option, or see the /squirrel-* commands")
    return "\n".join(lines)


def emit(text: str) -> None:
    """Write one command's output, sanitised.

    THE chokepoint. Every /squirrel-* command's output goes through here, so a listing
    cannot leak an escape whatever it echoes back — config values, store records, error
    messages, or a field nobody has added yet. Sanitising per field is what failed three
    times: the chip, then --show-sessions, then --show-rules and --show-config, each found
    only when someone thought to look at that particular surface.

    Applied PER LINE, not to the whole string: a listing's newlines are ours — they are its
    layout — and sanitise() strips newlines by design. Running it over the joined text
    collapsed the whole sessions table onto one 1287-character line, which the smoke gate
    caught. Splitting first keeps our structure and removes everything a value smuggled in.

    A newline smuggled INSIDE a value would already have split the line before reaching
    here, so values are also sanitised where they are assembled; this is the backstop that
    does not need anyone to remember the next field.

    Safe to apply wholesale because no COMMAND emits colour of its own; the bar's render
    path is separate and sanitises per run, before styling, in _produce()."""
    sys.stdout.write("\n".join(sanitise(line) for line in text.split("\n")) + "\n")


def emit_err(text: str) -> None:
    """The stderr twin of emit(). Same sanitising, same per-line rule.

    Audit N4: a FIFTH surface outside emit() was `main()` writing unknown_flag_message()
    straight to stderr, echoing argv back verbatim — live SGR and OSC-8 included. Barely
    exploitable, since the argv is the user's own typing, but the guard next door asserts a
    STRUCTURAL property ("no output path leaks an escape") and one raw writer disproves it.
    Every stderr message in main() goes through here so the property holds by construction
    rather than by each of them happening to be a literal today."""
    sys.stderr.write("\n".join(sanitise(line) for line in text.split("\n")) + "\n")


def config_for_stdin() -> dict:
    """Config read before stdin, only for the stdin backstop's own timeout."""
    try:
        return load_config()
    except Exception:
        return {}


def _stdin_has_data_within(seconds: float) -> bool:
    """True when stdin has something to read (or we cannot tell and must just read).

    The backstop for a status line that would otherwise block for ever. Claude Code always
    writes the status JSON, but if it ever did not, it keeps spawning a new render every few
    seconds and each one would sit blocked — hundreds of stuck processes from one upstream
    bug. A render that prints nothing is recoverable; one that hangs is not.

    POSIX only, deliberately. select() cannot watch a pipe on Windows, and the portable
    alternative — a watchdog thread calling os._exit — can fire mid-render and truncate real
    output, which is a worse failure than the one it guards. Windows still gets the isatty
    check below, which covers the case that actually bit: a person running this by hand."""
    if IS_WINDOWS or seconds <= 0:
        return True
    try:
        if sys.stdin.isatty():
            return True         # handled with a proper message just below
        import select
        ready, _, _ = select.select([sys.stdin], [], [], seconds)
        return bool(ready)
    except Exception:
        return True             # not a selectable stream — read it the ordinary way


def main(argv: list[str]) -> int:
    # Before any dispatch: every subcommand writes non-ASCII somewhere, not just the renderer.
    use_utf8_io()
    # `--here` is a modifier on any editing command, not a command: pull it out before the
    # unknown-flag check and before dispatch, so it never reaches a handler as an argument and
    # never terminates the word-collection loop below.
    here = "--here" in argv
    argv = [arg for arg in argv if arg != "--here"]
    _reset_write_log(here)
    # Refuse an unrecognised option before anything else can consume it. Only a bare
    # invocation — no flags at all — is allowed to reach the render path below.
    for arg in argv:
        if arg.startswith("--") and arg not in known_flags():
            emit_err(unknown_flag_message(arg))
            return 2
    if "--fetch" in argv:
        run_fetch()
        return 0
    if "--state" in argv:
        try:
            event = argv[argv.index("--state") + 1]
            hook = json.loads(sys.stdin.read() or "{}")
            sid = hook.get("session_id")
            refresh_launcher()
            if sid:
                apply_state(event, sid, time.time())
        except Exception:
            pass          # a hook must never disrupt Claude Code
        return 0
    if "--click-listener" in argv:
        # The detached click-to-acknowledge listener. Never reached from the render path.
        run_click_listener()
        return 0
    if "--notify" in argv:
        # The detached notifier: shows one desktop notification and exits. Spawned by
        # maybe_fire_phase_alert(), never run on the render path — the focus probe alone costs
        # about half a second on Windows.
        i = argv.index("--notify")
        rest = argv[i + 1:i + 4]
        title = rest[0] if rest else "Squirrel"
        body = rest[1] if len(rest) > 1 else ""
        hint = rest[2] if len(rest) > 2 else ""
        try:
            desktop_notify(title, body, hint, load_config())
        except Exception:
            pass
        return 0
    if "--drain-queue" in argv:
        # The on-demand queue reader, detached and short-lived: it drains what is due and
        # exits, so nothing is running when nothing is queued. Squirrel writes jobs; this
        # acts on them, and it is deliberately swappable — a parked reader that watches the
        # directory is a documented alternative, not a second implementation here.
        drain_queue()
        return 0
    if "--run-custom" in argv:
        # The detached child that refreshes one custom segment's command output. Never
        # reached from the render path — see custom_command_value()/spawn_custom_refresh().
        i = argv.index("--run-custom")
        if len(argv) > i + 1:
            run_custom(argv[i + 1])
        return 0
    if "--setup" in argv:
        i = argv.index("--setup")
        root = argv[i + 1] if len(argv) > i + 1 else str(Path(__file__).resolve().parent.parent)
        emit(cmd_setup(root))
        return 0
    # Every remaining flag is a slash command, which Claude Code runs with
    # $CLAUDE_PLUGIN_ROOT set. That makes it the other place — besides the hooks above —
    # where a launcher left stale by a plugin update gets repaired. See refresh_launcher().
    # The flag test is what keeps it off the render path: a bare invocation, and only a bare
    # invocation, renders (see the argv check at the top of main), and it must pay nothing.
    if any(arg.startswith("--") for arg in argv):
        refresh_launcher()
    for flag, handler in CONFIG_COMMANDS.items():
        if flag in argv:
            i = argv.index(flag)
            # Every remaining word up to the next flag, joined. `--task-actions notify on`
            # and `--task add x when=...` are the natural first attempts, and taking only
            # argv[i+1] answered both with a bare `usage:` while the quoted form worked.
            # Stopping at the next `--` keeps a following flag from being eaten as an
            # argument rather than dispatched.
            words: list = []
            for token in argv[i + 1:]:
                if token.startswith("--"):
                    break
                words.append(token)
            # layer_note() is appended HERE rather than in each of the forty handlers: it
            # reports the layers _write_config() actually wrote, so it cannot drift from what
            # happened, and no handler can forget it. A read-only command writes nothing and
            # gets no note.
            # `--here` is accepted both as its own argv entry and inside the single
            # "$ARGUMENTS" word the slash commands pass — see split_here().
            command_arg, arg_here = split_here(" ".join(words))
            _reset_write_log(here or arg_here)
            emit(handler(command_arg) + layer_note())
            return 0
    if "--show-config" in argv:
        emit(cmd_show_config())
        return 0
    if "--show-sessions" in argv:
        i = argv.index("--show-sessions")
        raw = argv[i + 1] if len(argv) > i + 1 and argv[i + 1] else ""
        # One argv slot, two possible things in it: a session id to mark, and/or the word
        # `wide` to print every column regardless of the terminal. Both are optional and the
        # order does not matter, because a user typing "/squirrel-sessions wide" should not
        # have to know it is the same slot the session id uses.
        tokens = [t for t in raw.split() if t]
        wide = any(t.lower() in ("wide", "full") for t in tokens)
        rest = [t for t in tokens if t.lower() not in ("wide", "full")]
        sid = rest[0] if rest else None
        emit(cmd_show_sessions(sid, width=10_000 if wide else None))
        return 0
    if "--help-text" in argv:
        emit(cmd_help())
        return 0
    # The render path reads the status JSON from stdin and blocks until it arrives. Claude
    # Code always supplies it; a person running this by hand does not, and would otherwise
    # sit at a frozen terminal. A tty on stdin means nobody is piping anything.
    if not _stdin_has_data_within(tunable(config_for_stdin(), "stdinWaitSeconds")):
        emit_err("squirrel: no status JSON arrived on stdin; nothing to render")
        return 2
    try:
        if sys.stdin.isatty():
            emit_err(
                "squirrel: this renders the status line from the JSON Claude Code pipes in.\n"
                "Nothing is piped, so there is nothing to render.\n"
                "Try --help-text for the options, or --show-config to see your settings.")
            return 2
    except (AttributeError, ValueError, OSError):
        pass            # not a real stream (a test double, a closed fd) — carry on and read
    try:
        raw = sys.stdin.read()
        if not raw.strip():
            return 0
        status = json.loads(raw)
        now = time.time()
        cache_file, lock = cache_paths()
        cache = load_json(cache_file)
        config = load_config()
        email = read_email()
        if config.get("usageFetch", True) and needs_fetch(cache, now, lock, config, email):
            spawn_fetch(lock)
        lines = render(status, cache, email, config, now, width=terminal_width())
        sys.stdout.write("\n".join(lines) + "\n")
        # account_pct: the SAME '5h' bucket the line just rendered — see pick_limits() — read
        # again here (cheap, pure) rather than threading a return value through render(), so
        # record_session_sample() attributes against the exact number the user is looking at,
        # never a second, independently-derived one. None when that bucket itself is None.
        limits, _stale, _source = pick_limits(status, cache, now, config, email)
        session_bucket = limits.get("session")
        account_pct = session_bucket["pct"] if session_bucket else None
        # resets_at rides along with the percentage it belongs to. A reading is only
        # trustworthy while its own window is still open, and this is what lets a later
        # reader tell a current observation from one a stale session keeps repeating —
        # see _observation_series(). Recorded from the SAME bucket, never re-derived.
        account_resets_at = session_bucket.get("resets_at") if session_bucket else None
        record_session_sample(status, email, now, config, account_pct,
                              account_resets_at)   # best-effort; never delays or breaks output
    except Exception as exc:  # a broken status line must stay visible, never silent
        sys.stdout.write(f"statusline error: {type(exc).__name__}: {exc}\n")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
