#!/usr/bin/env python3
"""Regenerate dev/golden_render.json from the current renderer.

The golden fixture is the no-behaviour-change contract for the segment registry / layout
refactor (Task 13): it freezes what a user with no layout config sees, escapes and all.
Run this ONLY for a deliberate, reviewed rendering change, and review the JSON diff.
Re-baselined twice so far, both times after checking the diff contained ONLY the
intended substitution: Task 13 (the segment registry, zero cells moved) and Task 29d
(stdin became primary for the headline buckets, so 22 of 24 cells moved and every
change was the API's 17%/4% giving way to the payload's own 30%/9%).
"""
import json
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

import test_statusline as t   # noqa: E402  (sets up the isolated env below)

t.setUpModule()
try:
    data = t.golden_render_all()
finally:
    t.tearDownModule()

t.GOLDEN_PATH.write_text(json.dumps(data, indent=1, ensure_ascii=False) + "\n")
print(f"wrote {t.GOLDEN_PATH} ({len(data)} scenarios x {len(t.GOLDEN_WIDTHS)} widths)")
