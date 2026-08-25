#!/usr/bin/env python3
"""Render the README's screenshots — as SVG, from the REAL renderer.

Every image on the project page is produced by calling `render()` with a fixture status
payload and converting the escape sequences it emits into SVG text. Nothing here is drawn by
hand, so a picture on the README cannot claim a bar Squirrel does not actually produce: change
the renderer and the image changes with it (`DocsImagesTests` fails until they are regenerated).

    python3 dev/docs_images.py          # write docs/img/*.svg
    python3 dev/docs_images.py --check  # exit 1 if any file on disk is out of date

SVG rather than PNG on purpose: it needs no rasteriser, stays a few kB, renders crisply on a
retina screen, and diffs as text in review.
"""
from __future__ import annotations

import argparse
import importlib.util
import json
import os
import re
import sys
import tempfile
import time
from datetime import timezone
from pathlib import Path
from unittest import mock

REPO = Path(__file__).resolve().parent.parent
IMG_DIR = REPO / "docs" / "img"

# Isolation first, before the module under test is even imported. A screenshot built against
# the developer's own profile is not a screenshot of the product: the first attempt read the
# real shared store, found that today's water reminder had been acknowledged an hour ago, and
# quietly produced a "reminders" image with no reminder in it.
_ISOLATION = tempfile.TemporaryDirectory(prefix="docs-images-")
os.environ["CLAUDE_CONFIG_DIR"] = str(Path(_ISOLATION.name) / "config")
os.environ["SQUIRREL_SHARED_DIR"] = str(Path(_ISOLATION.name) / "shared")
os.environ["XDG_CACHE_HOME"] = str(Path(_ISOLATION.name) / "cache")
os.environ["SQUIRREL_NO_ALERTS"] = "1"      # the idle screenshots are hours-long waits

_spec = importlib.util.spec_from_file_location("sl", REPO / "plugin" / "scripts" / "statusline.py")
sl = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(sl)

NOW = 1750000000.0            # frozen: the images must not change with the clock

# ── the terminal these screenshots are "taken" in ────────────────────────────
CELL_W, CELL_H = 8.4, 20.0
PAD_X, PAD_Y = 14.0, 12.0
FONT = ("ui-monospace, SFMono-Regular, 'SF Mono', Menlo, Consolas, "
        "'DejaVu Sans Mono', monospace")
GROUND = "#12141a"            # the pane behind the bar
DEFAULT_FG = "#c8cdd6"
CAPTION_FG = "#7d8590"

ANSI16 = {
    30: "#3b4048", 31: "#e06c75", 32: "#98c379", 33: "#e5c07b", 34: "#61afef",
    35: "#c678dd", 36: "#56b6c2", 37: "#abb2bf",
    90: "#5c6370", 91: "#ff7b72", 92: "#b8e994", 93: "#f2cc60", 94: "#79c0ff",
    95: "#d2a8ff", 96: "#76e3ea", 97: "#ffffff",
}
ANSI_BG16 = {40: "#12141a", 41: "#b3261e", 42: "#2ea043", 43: "#c9a227",
             44: "#1f6feb", 45: "#8250df", 46: "#1f9c9c", 47: "#abb2bf"}

_SGR = re.compile(r"\033\[([0-9;]*)m")
_OSC = re.compile(r"\033\][^\033\007]*(?:\033\\|\007)")


class Pen:
    __slots__ = ("fg", "bg", "bold", "dim", "inverse")

    def __init__(self):
        self.reset()

    def reset(self):
        self.fg = self.bg = None
        self.bold = self.dim = self.inverse = False

    def copy(self):
        other = Pen()
        other.fg, other.bg = self.fg, self.bg
        other.bold, other.dim, other.inverse = self.bold, self.dim, self.inverse
        return other

    def apply(self, params: list[int]):
        i = 0
        while i < len(params):
            code = params[i]
            if code == 0:
                self.reset()
            elif code == 1:
                self.bold = True
            elif code == 2:
                self.dim = True
            elif code == 7:
                self.inverse = True
            elif code == 22:
                self.bold = self.dim = False
            elif code == 27:
                self.inverse = False
            elif code == 39:
                self.fg = None
            elif code == 49:
                self.bg = None
            elif code in ANSI16:
                self.fg = ANSI16[code]
            elif code in ANSI_BG16:
                self.bg = ANSI_BG16[code]
            elif code in (38, 48) and i + 1 < len(params):
                target = "fg" if code == 38 else "bg"
                if params[i + 1] == 2 and i + 4 < len(params):
                    setattr(self, target, "#%02x%02x%02x" % tuple(params[i + 2:i + 5]))
                    i += 4
                elif params[i + 1] == 5 and i + 2 < len(params):
                    setattr(self, target, _xterm256(params[i + 2]))
                    i += 2
            i += 1


def _xterm256(n: int) -> str:
    if n < 16:
        return ANSI16.get(30 + n if n < 8 else 82 + n, DEFAULT_FG)
    if n < 232:
        n -= 16
        levels = (0, 95, 135, 175, 215, 255)
        return "#%02x%02x%02x" % (levels[n // 36], levels[(n // 6) % 6], levels[n % 6])
    grey = 8 + (n - 232) * 10
    return "#%02x%02x%02x" % (grey, grey, grey)


def cells(line: str) -> list[tuple[str, Pen]]:
    """One entry per DISPLAY COLUMN — a wide glyph occupies its cell and leaves the next one
    empty — so the SVG lines up column for column with what the terminal draws."""
    out: list[tuple[str, Pen]] = []
    pen, index = Pen(), 0
    line = _OSC.sub("", line)                      # a hyperlink is not visible ink
    while index < len(line):
        match = _SGR.match(line, index)
        if match:
            raw = match.group(1)
            pen = pen.copy()
            pen.apply([int(p or 0) for p in raw.split(";")] if raw else [0])
            index = match.end()
            continue
        ch = line[index]
        index += 1
        if ch == "\033":                           # any escape we do not model paints nothing
            continue
        width = sl._char_width(ch)
        if width == 0:
            continue
        out.append((ch, pen))
        for _ in range(width - 1):
            out.append(("", pen))
    return out


def _esc(text: str) -> str:
    return (text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;"))


def svg(lines: list[str], caption: str | None = None, columns: int | None = None) -> str:
    """One SVG per screenshot: the pane, the bar, and an optional caption underneath."""
    grids = [cells(line) for line in lines]
    columns = columns or max((len(g) for g in grids), default = 0)
    width = PAD_X * 2 + columns * CELL_W
    height = PAD_Y * 2 + len(grids) * CELL_H + (CELL_H if caption else 0)
    parts = [
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{width:.0f}" height="{height:.0f}" '
        f'viewBox="0 0 {width:.0f} {height:.0f}" font-family="{FONT}" font-size="14">',
        f'<rect width="{width:.0f}" height="{height:.0f}" rx="8" fill="{GROUND}"/>',
    ]
    for row, grid in enumerate(grids):
        y = PAD_Y + row * CELL_H
        # backgrounds first, merged into runs so a painted bar is one rect, not 150
        run_start, run_bg = 0, None
        def flush(end: int, colour: str | None):
            if colour and end > run_start:
                parts.append(f'<rect x="{PAD_X + run_start * CELL_W:.1f}" y="{y:.1f}" '
                             f'width="{(end - run_start) * CELL_W:.1f}" height="{CELL_H:.1f}" '
                             f'fill="{colour}"/>')
        for col, (_ch, pen) in enumerate(grid):
            bg = pen.fg if pen.inverse else pen.bg
            if bg != run_bg:
                flush(col, run_bg)
                run_start, run_bg = col, bg
        flush(len(grid), run_bg)
        # then the glyphs, one <text> per styled run
        col = 0
        while col < len(grid):
            pen = grid[col][1]
            end = col
            while end < len(grid) and _same(grid[end][1], pen):
                end += 1
            text = "".join(ch for ch, _ in grid[col:end])
            if text.strip():
                fill = (pen.bg or GROUND) if pen.inverse else (pen.fg or DEFAULT_FG)
                extra = ' font-weight="700"' if pen.bold else ""
                extra += ' opacity="0.62"' if pen.dim else ""
                parts.append(f'<text x="{PAD_X + col * CELL_W:.1f}" y="{y + 14.5:.1f}" '
                             f'fill="{fill}"{extra} xml:space="preserve">{_esc(text)}</text>')
            col = end
    if caption:
        parts.append(f'<text x="{PAD_X:.1f}" y="{PAD_Y + len(grids) * CELL_H + 15:.1f}" '
                     f'fill="{CAPTION_FG}" font-size="12.5">{_esc(caption)}</text>')
    parts.append("</svg>")
    return "\n".join(parts) + "\n"


def _same(a: Pen, b: Pen) -> bool:
    return (a.fg, a.bg, a.bold, a.dim, a.inverse) == (b.fg, b.bg, b.bold, b.dim, b.inverse)


# ── the scenarios ────────────────────────────────────────────────────────────
def status(**over) -> dict:
    base = {
        "session_id": "docs",
        "model": {"display_name": "Opus 5"},
        "workspace": {"repo": {"name": "my-app"}, "current_dir": "/w/my-app"},
        "context_window": {"context_window_size": 200000, "used_percentage": 61,
                           "current_usage": {"input_tokens": 118000, "output_tokens": 4000}},
        "cost": {"total_cost_usd": 4.12, "total_duration_ms": 5_400_000,
                 "total_lines_added": 431, "total_lines_removed": 96},
        "rate_limits": {"five_hour": {"used_percentage": 74,
                                      "resets_at": NOW + 46 * 60},
                        "seven_day": {"used_percentage": 38,
                                      "resets_at": NOW + 3 * 86400}},
    }
    base.update(over)
    return base


CACHE = {"fetched_at": NOW - 30, "ok": True, "error": None,
         "account": sl.account_hash("you@example.com"),
         "limits": {"session": {"label": "", "pct": 74.0, "resets_at": NOW + 46 * 60},
                    "weekly_all": {"label": "", "pct": 38.0, "resets_at": NOW + 3 * 86400},
                    # The scoped bucket's label comes from the API, not from us: it is whichever model
         # currently carries its own weekly cap (Fable, today). A screenshot showing a
         # different model would be inventing a limit nobody has.
         "scoped": [{"label": "Fable", "pct": 52.0, "resets_at": NOW + 3 * 86400}]}}


def render(status_json: dict, config: dict, width: int, branch="feat/checkout",
           dirty=True, state=None, now=NOW) -> list[str]:
    if state:
        sl.apply_state(state[0], status_json["session_id"], now - state[1])
    runs = [(f"⎇ {branch}" + ("*" if dirty else ""), sl.YELLOW if dirty else sl.GREEN)]
    with mock.patch.object(sl, "git_segment_runs", return_value=runs):
        return sl.render(status_json, CACHE, "you@example.com", config, now, timezone.utc,
                         width=width)


def base_config(**over) -> dict:
    config = sl.load_config()
    config.update(over)
    return config


def scenarios() -> dict[str, str]:
    """{filename: svg}. Each one is a real render."""
    out = {}
    cfg = base_config()

    out["bar.svg"] = svg(render(status(), cfg, 104, state=("working", 12)),
                         "context · limits · repo · branch · model · cost — two lines, one glance")

    out["idle.svg"] = svg(
        render(status(session_id="i1"), cfg, 104, state=("working", 4)) + [""]
        + render(status(session_id="i2"), cfg, 104, state=("idle", 40)) + [""]
        + render(status(session_id="i3"), cfg, 104, state=("idle", 1740)),
        "working · waiting 40s · waiting 29m — the whole bar escalates, so you cannot miss it")

    # The bar at five minutes, and the notification that goes with it. The toast's TEXT comes
    # from the same function the real notifier uses, so the picture cannot promise wording the
    # code does not produce; the box around it is an illustration of the OS notification.
    alert_status = status(session_id="a1")
    sl.apply_state("idle", "a1", NOW - 300)
    ctx = sl._ctx_for(alert_status, {}, False, "stdin", "you@example.com", cfg, NOW, timezone.utc, None)
    ctx["state"] = sl.read_state("a1", NOW)
    title, body = sl._alert_text({"after": 300, "notify": True}, ctx)
    box_w = max(len(title), len(body), 34) + 2
    toast = ["   ╭" + "─" * box_w + "╮",
             "   │ " + title.ljust(box_w - 2) + " │",
             "   │ " + body.ljust(box_w - 2) + " │",
             "   ╰" + "─" * box_w + "╯"]
    out["alert.svg"] = svg(
        render(status(session_id="a1"), cfg, 104, state=("idle", 300)) + [""] + toast,
        "five minutes in: a silent desktop notification, with the repo name so you know "
        "which terminal to go back to")

    # Three accounts, three nicknames, three colours — the multi-profile case, rendered by
    # the same code that renders anyone's bar.
    accounts = base_config(
        accountNames={"you@company.com": "Work", "you@example.com": "Personal",
                      "ops@example.com": "Ops"},
        accountColors={"you@company.com": "#4f9cf9", "you@example.com": "#2ea043",
                       "ops@example.com": "#c678dd"},
        layout=[["context", "session", "weekly"], ["account", "repo", "git", "model", "chip"]])
    rows = []
    for index, (email, repo, branch) in enumerate((
            ("you@company.com", "acme-billing", "fix/tax-rounding"),
            ("you@example.com", "my-app", "feat/checkout"),
            ("ops@example.com", "infra", "main"))):
        st = status(session_id=f"acct{index}",
                    workspace={"repo": {"name": repo}, "current_dir": f"/w/{repo}"})
        with mock.patch.object(sl, "git_segment_runs",
                               return_value=[(f"⎇ {branch}", sl.GREEN)]):
            rows += sl.render(st, CACHE, email, accounts, NOW, timezone.utc, width=96)[1:]
    out["accounts.svg"] = svg(rows, "one machine, three logins — each nicknamed and coloured, "
                                    "with its own limits and its own history")

    water = base_config(customSegments=[
        {"id": "water", "every": 1800, "ack": True, "shared": True, "line": 2,
         "text": "💧 drink water", "click": "ack"},
        {"id": "break", "every": 7200, "ack": True, "shared": True, "line": 2,
         "text": "🧘 take a break", "click": "ack"}])
    water["layout"] = [["context", "session", "weekly"],
                       ["account", "repo", "git", "model", "chip", "water", "break"]]
    out["reminders.svg"] = svg(render(status(session_id="w1"), water, 104, state=("working", 8)),
                               "every 30 minutes, on the bar you are already looking at — "
                               "click it (or say \"done\") and it goes quiet")

    out["narrow.svg"] = svg(
        render(status(session_id="n1"), cfg, 104, state=("working", 8)) + [""]
        + render(status(session_id="n2"), cfg, 80, state=("working", 8)) + [""]
        + render(status(session_id="n3"), cfg, 58, state=("working", 8)),
        "104 · 80 · 58 columns — bars shrink first, then the least important text steps aside",
        columns=104)
    return out


def sessions_table() -> list[str]:
    """`/squirrel-sessions` over a fixture of three sessions on one account.

    Anchored to the real clock (the command reads it) but built from fixed offsets, so the
    figures — points, coverage, cost, burn — come out the same on every run.
    """
    now = time.time()
    with tempfile.TemporaryDirectory(prefix="docs-shared-") as shared:
        with mock.patch.dict(os.environ, {"SQUIRREL_SHARED_DIR": shared}), \
             mock.patch.object(sl, "read_email", return_value="you@example.com"):
            account = sl.account_hash("you@example.com")
            # ONE account, so every session observes the SAME utilisation — that is what an
            # account-wide limit means, and a fixture where they disagree would be modelling
            # two accounts and producing nonsense (three sessions each "reaching" a different
            # percentage of one bucket).
            fixtures = {
                "s-api": ("api-gateway", 3.90, 210_000, (0.05, 1.00)),
                "s-web": ("my-app", 1.60, 96_000, (0.35, 1.00)),
                "s-doc": ("handbook", 0.20, 18_000, (0.70, 1.00)),
            }
            start, steps, span = now - 4 * 3600, 60, 4 * 3600
            for sid, (repo, cost, tokens, (begin, end)) in fixtures.items():
                samples = []
                for i in range(steps + 1):
                    frac = i / steps
                    # its own share of its own work, over the stretch it was actually running
                    own = 0.0 if frac <= begin else min(1.0, (frac - begin) / (end - begin))
                    samples.append([start + i * (span / steps), int(tokens * own),
                                    round(cost * own, 4), "Opus 5", account,
                                    round(74.0 * frac, 1), now + 46 * 60])
                sl.write_cache(sl.session_path(sid),
                               {"session_id": sid, "repo": repo, "model": "Opus 5",
                                "started": start, "samples": samples})
            table = sl.cmd_show_sessions("s-web", width=96)
    return [line for line in table.split("\n") if line.strip()][:5]


def build() -> dict[str, str]:
    files = scenarios()
    files["sessions.svg"] = svg(
        sessions_table(),
        "which of my sessions is eating the 5-hour limit — attributed, not guessed")
    return files


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true",
                        help="verify the committed images match the current renderer")
    args = parser.parse_args(argv)
    files = build()
    if args.check:
        stale = [name for name, body in files.items()
                 if not (IMG_DIR / name).exists() or (IMG_DIR / name).read_text(encoding="utf-8") != body]
        if stale:
            print("out of date: " + ", ".join(sorted(stale)))
            print("regenerate with: python3 dev/docs_images.py")
            return 1
        print(f"{len(files)} images current")
        return 0
    IMG_DIR.mkdir(parents=True, exist_ok=True)
    for name, body in files.items():
        (IMG_DIR / name).write_text(body, encoding="utf-8")
        print(f"wrote docs/img/{name}  ({len(body)} bytes)")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
