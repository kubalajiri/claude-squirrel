#!/bin/sh
# Squirrel — portable entry point for the slash commands, the skill and the activity hooks.
#
# Why this exists: plugin files (hooks/hooks.json, commands/*.md, skills/squirrel/SKILL.md) ship
# as-is and Claude Code offers no per-OS conditionality in them — no OS matcher, no variable that
# expands to a Python interpreter. So the interpreter cannot be baked into those files at install
# time the way it is baked into the status-line launcher (see resolve_interpreter() in
# statusline.py). It has to be found here, at run time, by the one interpreter-agnostic thing
# every supported platform has: /bin/sh.
#
# On Windows that shell is Git Bash, which Claude Code already requires as a documented
# prerequisite (https://code.claude.com/docs/en/setup.md), so this adds no new dependency.
#
# The search must VERIFY, not merely resolve. On a default Windows install `python3` on PATH is
# the Microsoft Store "App Execution Alias" — a zero-byte stub that prints "Python was not found"
# and exits 9009. `command -v python3` finds it happily. Actually running `-c` is the only way to
# tell it apart from a real interpreter, so that is what we do.
set -u

# Locate statusline.py. Preferred: the plugin root Claude Code told us about. Fallback: relative
# to this wrapper, which always sits next to statusline.py — this covers the reported cases where
# ${CLAUDE_PLUGIN_ROOT} is not injected into a hook's environment (claude-code#27145, #66557).
STATUSLINE=""
if [ -n "${CLAUDE_PLUGIN_ROOT:-}" ] && [ -f "${CLAUDE_PLUGIN_ROOT}/scripts/statusline.py" ]; then
    STATUSLINE="${CLAUDE_PLUGIN_ROOT}/scripts/statusline.py"
elif [ -f "$(dirname "$0")/statusline.py" ]; then
    STATUSLINE="$(dirname "$0")/statusline.py"
else
    echo "squirrel: cannot find statusline.py next to $0 or under \$CLAUDE_PLUGIN_ROOT" >&2
    exit 1
fi

PY=""
PY_VERSION_FLAG=""
CHECK='import sys; sys.exit(0 if sys.version_info[0] == 3 else 1)'
for candidate in python3 python py; do
    command -v "$candidate" >/dev/null 2>&1 || continue
    if [ "$candidate" = py ]; then
        # The Windows `py` launcher needs an explicit version selector.
        if py -3 -c "$CHECK" >/dev/null 2>&1; then
            PY=py; PY_VERSION_FLAG=-3; break
        fi
    elif "$candidate" -c "$CHECK" >/dev/null 2>&1; then
        PY=$candidate; break
    fi
done

if [ -z "$PY" ]; then
    echo "squirrel: no working Python 3 found on PATH (tried python3, python, py -3)." >&2
    echo "squirrel: install Python 3 and make sure it runs from this shell, then try again." >&2
    exit 1
fi

if [ -n "$PY_VERSION_FLAG" ]; then
    exec "$PY" "$PY_VERSION_FLAG" "$STATUSLINE" "$@"
fi
exec "$PY" "$STATUSLINE" "$@"
