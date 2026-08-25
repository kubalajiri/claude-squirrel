#!/usr/bin/env python3
"""Fingerprint a Squirrel profile directory, for the smoke test's "did we touch the user's
real installation?" guard.

This exists because the unit suite once created `state-wtest*.json` files in the developer's
live profile, and on a colleague's machine would have written into their installation. The
guard compares this fingerprint before and after a smoke run.

What it compares, and why only that:

  * the SET of entry names, always — a file appearing that was not there is the original
    defect, and is what must never slip through;
  * the mode of every entry;
  * the content of everything EXCEPT the two things the user's own running status line
    rewrites while the test runs — `cache/` (its usage cache, every few seconds) and
    `state-*.json` (its activity state, on every hook).

Comparing those two made the gate fail about one run in five for reasons that had nothing to
do with the test, and a gate that fails at random trains you to re-run until green, which is
exactly how a real failure gets waved through.

Kept as its own script rather than inlined in the shell so that it can be tested: see
ProfileSnapshotTests, which exercises every tooth against a temporary fixture. The teeth used
to be checked by planting faults in the real profile, which was both risky and impossible to
run in CI.

    python3 dev/profile_snapshot.py <directory>
"""
import hashlib
import os
import stat
import sys
from pathlib import Path

LIVE_PREFIX, LIVE_SUFFIX, LIVE_DIR = "state-", ".json", "cache"


def is_live(name: str) -> bool:
    """True for entries the user's own status line rewrites while the test runs."""
    return name == LIVE_DIR or (name.startswith(LIVE_PREFIX) and name.endswith(LIVE_SUFFIX))


def snapshot(root) -> str:
    root = Path(root)
    if not root.exists():
        return "(absent)"
    rows = []
    for entry in sorted(root.iterdir(), key=lambda p: p.name):
        mode = oct(stat.S_IMODE(entry.lstat().st_mode))
        if is_live(entry.name):
            kind = "dir" if entry.is_dir() else "file"
            rows.append(f"{entry.name}\t{kind}\t{mode}\t(live: content not compared)")
        elif entry.is_dir():
            inner = sorted(p.name for p in entry.iterdir())
            rows.append(f"{entry.name}\tdir\t{mode}\t{inner}")
        else:
            digest = hashlib.sha256(entry.read_bytes()).hexdigest()[:16]
            rows.append(f"{entry.name}\tfile\t{mode}\t{entry.stat().st_size}\t{digest}")
    return "\n".join(rows)


if __name__ == "__main__":
    print(snapshot(sys.argv[1] if len(sys.argv) > 1 else os.getcwd()))
