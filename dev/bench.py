#!/usr/bin/env python3
"""Benchmark helper for statusline.py's render path.

Not part of the test suite — wall-clock numbers are too machine-dependent and flaky for CI
assertions (see test_statusline.py's structural regression guards for the assertable version
of "the render path stays fast"). This is a by-hand tool for a maintainer to see the actual
effect of a change on their own machine, using the same method as the Task 11b baseline: median
of N full `statusline.py` invocations (each fed a representative status JSON on stdin, as a
subprocess so process-startup/import costs are included, same as Claude Code actually pays them),
next to reference numbers for bare interpreter startup and the two import families most likely to
dominate that cost (urllib, which the render path must never import — see the `--fetch` docstring
in statusline.py — and json+subprocess, which it does need).

Isolation: runs against a throwaway CLAUDE_CONFIG_DIR (mktemp, cleaned up on exit), never the
caller's real ~/.claude — this is safe to run on a real machine without touching a live profile.
The usage cache is pre-seeded with a fresh `fetched_at` so the benchmarked renders never trigger
a detached --fetch child (which would add fork noise and isn't part of what's being measured).
`workspace.current_dir` points at this repo itself, so the git segment's real subprocess/caching
behaviour is exercised, not mocked away.

Usage: python3 dev/bench.py [N]   (default N=15)

CAUTION — this number tracks machine load, and it has already sent someone chasing a ghost.
It measures N full process invocations, so most of it is interpreter start-up: it drifted
from ~50 ms to ~58 ms across three commits that changed the render path not at all, purely
because the machine was busy. To answer "did my change make the render slower?", import the
module and time `render()` in-process over ~60 iterations instead — that comparison put the
same commits at 0.24 ms against 0.26 ms. Use this script for the shape of the whole
invocation, never for a regression verdict.
"""
from __future__ import annotations

import json
import shutil
import statistics
import subprocess
import sys
import tempfile
import time
from pathlib import Path

PLUGIN_ROOT = Path(__file__).resolve().parent.parent / "plugin"
SCRIPT = PLUGIN_ROOT / "scripts" / "statusline.py"


def make_status(now: float) -> dict:
    return {
        "session_id": "bench-session",
        "model": {"id": "claude-fable-5[1m]", "display_name": "Fable 5"},
        "context_window": {
            "context_window_size": 200_000, "used_percentage": 42,
            "current_usage": {"input_tokens": 80_000, "output_tokens": 1_200,
                              "cache_creation_input_tokens": 2_000, "cache_read_input_tokens": 2_000},
        },
        "cost": {"total_cost_usd": 1.23, "total_duration_ms": 7_980_000,
                 "total_lines_added": 312, "total_lines_removed": 48},
        "effort": {"level": "xhigh"},
        "thinking": {"enabled": True},
        "fast_mode": False,
        "rate_limits": {
            "five_hour": {"used_percentage": 30, "resets_at": now + 3600},
            "seven_day": {"used_percentage": 9, "resets_at": now + 400_000},
        },
        "workspace": {"current_dir": str(PLUGIN_ROOT.parent), "repo": {"name": "claude-squirrel"}},
    }


def timed_run(argv: list[str], stdin_text: str = "", env: dict | None = None) -> float:
    """Wall-clock milliseconds for one subprocess run."""
    start = time.perf_counter()
    subprocess.run(argv, input=stdin_text, capture_output=True, text=True, timeout=10, env=env)
    return (time.perf_counter() - start) * 1000


def median_ms(fn, n: int) -> float:
    return statistics.median(fn() for _ in range(n))


def main() -> int:
    n = int(sys.argv[1]) if len(sys.argv) > 1 else 15

    tmp = tempfile.mkdtemp(prefix="squirrel-bench-")
    try:
        config_dir = Path(tmp) / "config"
        squirrel_dir = config_dir / "squirrel"
        cache_dir = squirrel_dir / "cache"
        cache_dir.mkdir(parents=True)
        now = time.time()
        # Pre-seed a fresh usage cache so no render in the benchmark loop spawns a detached
        # --fetch child (that fork cost isn't what this benchmark measures).
        (cache_dir / "usage.json").write_text(json.dumps({
            "fetched_at": now, "ok": True, "error": None,
            "limits": {"session": None, "weekly_all": None, "scoped": []},
        }))

        import os
        env = dict(os.environ)
        env["CLAUDE_CONFIG_DIR"] = str(config_dir)
        env["SQUIRREL_SHARED_DIR"] = str(Path(tmp) / "shared")
        env.pop("COLUMNS", None)

        payload = json.dumps(make_status(now))

        rows = [
            ("full render", lambda: timed_run([sys.executable, str(SCRIPT)], payload, env)),
            ("bare python3 -c pass", lambda: timed_run([sys.executable, "-c", "pass"])),
            ("import urllib.request", lambda: timed_run([sys.executable, "-c", "import urllib.request"])),
            ("import json,subprocess", lambda: timed_run([sys.executable, "-c", "import json,subprocess"])),
        ]

        print(f"median of {n} runs (ms):")
        results = {}
        for label, fn in rows:
            m = median_ms(fn, n)
            results[label] = m
            print(f"  {label:<26} {m:6.1f}")
        return 0
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    sys.exit(main())
