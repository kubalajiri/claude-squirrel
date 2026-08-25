#!/usr/bin/env bash
# Squirrel smoke test: exercises every statusline.py subcommand, the activity-state hooks,
# and the responsive renderer against a throwaway CLAUDE_CONFIG_DIR profile.
#
# No network: usageFetch is turned off before anything renders, so a run can never reach
# the rate-limited usage endpoint. Isolation: everything lives under a mktemp -d, cleaned
# up on exit; the real profile (~/.claude or $CLAUDE_CONFIG_DIR from the caller's shell,
# whichever /root/.claude/squirrel resolves to) is snapshotted before and after and must
# be byte-for-byte unchanged.
#
# This is an assertion-driven test, not a demo: every step below either matches an
# expected substring/value or the script exits 1 with a clear message.
#
# Exit code: 0 only if every assertion passed, non-zero otherwise (verified directly, not
# through a pipe — see below). The script also prints a final "RESULT: PASS" / "RESULT: FAIL
# (exit N)" line no matter which check (if any) fails, so a caller who scrapes captured output
# instead of checking $? still gets an unambiguous answer.
#
# IMPORTANT — checking the result: run this directly (`dev/smoke_install.sh; echo $?`, or
# capture it with `out="$(dev/smoke_install.sh 2>&1)"; echo $?`, both of which propagate the
# real exit code correctly) or check the final "RESULT:" line. Do NOT pipe its invocation into
# another command (`dev/smoke_install.sh | tail`, `| tee`, `| cat`, …) and then read `$?` —
# that reads the exit status of the *last command in the pipe*, not of this script, and will
# silently report 0 even when a "SMOKE FAIL" line is sitting right there in the output (this is
# exactly how a stale assertion below survived past this gate more than once).
#
# Usage: dev/smoke_install.sh
set -euo pipefail

# ROOT is the *published* tree (plugin/); DEV is this directory, which never ships.
DEV="$(cd "$(dirname "$0")" && pwd)"
ROOT="$(cd "$DEV/../plugin" && pwd)"
cd "$ROOT"

fail() {
    echo "SMOKE FAIL: $*" >&2
    exit 1
}

# ── real-profile snapshot (before touching anything) ─────────────────────────
# The fingerprint and the reasoning behind what it does and does not compare live in
# dev/profile_snapshot.py, which is a separate script precisely so its teeth can be
# tested against a fixture (ProfileSnapshotTests) instead of by planting faults in the
# developer's real profile.
REAL_SQUIRREL_DIR="/root/.claude/squirrel"
profile_snapshot() {
    python3 "$DEV/profile_snapshot.py" "$1"
}
real_before="$(profile_snapshot "$REAL_SQUIRREL_DIR" 2>&1 || true)"

# macOS's BSD mktemp wants a template; GNU's does not need one. Try the bare form, fall
# back to a templated one, so this gate runs unchanged on a colleague's Mac.
TMP="$(mktemp -d 2>/dev/null || mktemp -d -t squirrel-smoke)"
# One combined EXIT trap (bash keeps only the last trap registered per signal): clean up the
# throwaway profile, then print an unmissable final result line reflecting whatever exit status
# is pending — whether the script reached its natural end, or `fail()` called `exit 1` from
# anywhere above. `$?` is captured first, before `rm -rf` (or anything else) can change it.
cleanup_and_report() {
    local rc=$?
    rm -rf "$TMP" 2>/dev/null || true
    if [ "$rc" -eq 0 ]; then
        echo "RESULT: PASS"
    else
        echo "RESULT: FAIL (exit $rc)"
    fi
}
trap cleanup_and_report EXIT

export CLAUDE_CONFIG_DIR="$TMP/config"
mkdir -p "$CLAUDE_CONFIG_DIR"
printf '{"oauthAccount":{"emailAddress":"you@example.com"}}' >"$CLAUDE_CONFIG_DIR/.claude.json"

# The cross-session shared store (~/.squirrel by default) deliberately ignores
# CLAUDE_CONFIG_DIR/HOME so every profile finds the same one — which means it needs its own
# override here too, or this "isolated" smoke test would write real session files into the
# machine's actual home directory the moment it renders a status line with a session_id.
export SQUIRREL_SHARED_DIR="$TMP/shared"

SQUIRREL_DIR="$CLAUDE_CONFIG_DIR/squirrel"
CONFIG_JSON="$SQUIRREL_DIR/config.json"
SETTINGS_JSON="$CLAUDE_CONFIG_DIR/settings.json"
RUN_SH="$SQUIRREL_DIR/run.sh"

s() { python3 "$ROOT/scripts/statusline.py" "$@"; }

# run_expect <expected-substring> -- <argv for s...>: run, echo, and require the substring.
run_expect() {
    local expect="$1"
    shift
    local out
    out="$(s "$@")"
    echo "$out"
    case "$out" in
    *"$expect"*) ;;
    *) fail "'s $*' expected output containing '$expect', got: $out" ;;
    esac
}

cfg_get() { # cfg_get <python-expr on `cfg` dict> — reads the RESOLVED config
    # Resolved through the product's own load_config(), not by opening one layer's file.
    # Every call site below asks "did this setting persist?", and since config became layered
    # that question is no longer answerable from a single file: a setting persists perfectly
    # well in the shared layer. Reading squirrel/config.json directly turned every one of
    # these into a test of WHICH FILE was written rather than of whether the setting took
    # effect — which is the weaker question, and the wrong one.
    # eval() here only ever runs the fixed, developer-authored expressions passed at the
    # call sites below (never external/user input); builtins are stripped as a second guard.
    python3 -c "
import sys
sys.path.insert(0, sys.argv[1])
import statusline as sl
print(eval(sys.argv[2], {'__builtins__': {}}, {'cfg': sl.load_config()}))
" "$ROOT/scripts" "$1"
}

layer_get() { # layer_get <shared|profile> <python-expr on `cfg`> — ONE layer's own file
    python3 -c "
import sys
sys.path.insert(0, sys.argv[1])
import statusline as sl
print(eval(sys.argv[3], {'__builtins__': {}}, {'cfg': sl.stored_config(sys.argv[2])}))
" "$ROOT/scripts" "$1" "$2"
}

strip_ansi() { sed -E 's/\x1b\[[0-9;]*m//g'; }

echo "== setup =="
run_expect "status line installed" --setup "$ROOT"
[ -x "$RUN_SH" ] || fail "$RUN_SH is not executable after --setup"
settings_cmd="$(python3 -c "import json; print(json.load(open('$SETTINGS_JSON'))['statusLine']['command'])")"
[ "$settings_cmd" = "$RUN_SH" ] || fail "settings.json statusLine.command is '$settings_cmd', expected '$RUN_SH'"
run_expect "installed already" --setup "$ROOT"

echo "== config commands (usage off first — no network) =="
run_expect "OFF" --set-usage off
[ "$(cfg_get 'cfg["usageFetch"]')" = "False" ] || fail "usageFetch did not persist as false"

run_expect "nickname for you@example.com set to 'demo'" --set-nick demo
[ "$(cfg_get 'cfg["accountNames"]["you@example.com"]')" = "demo" ] || fail "nickname did not persist"

echo "== the shared config layer =="
# The bug this layer exists to fix, at install level: the user runs three profiles, only one
# had a config.json, so their nicknames and colours existed in one profile and not the others
# — while the ACK STATE for the same reminders was cross-profile and synced perfectly.
# A unit test can assert this; only the smoke test proves it across two real CLAUDE_CONFIG_DIRs
# in separate processes, which is the shape the user actually runs.
[ "$(layer_get shared 'cfg["accountNames"]["you@example.com"]')" = "demo" ] \
    || fail "the nickname did not land in the shared layer"
[ "$(layer_get profile 'cfg.get("accountNames")')" = "None" ] \
    || fail "the nickname was written per-profile as well as shared"
SECOND_PROFILE="$TMP/profile2"
[ "$(CLAUDE_CONFIG_DIR="$SECOND_PROFILE" cfg_get 'cfg["accountNames"]["you@example.com"]')" = "demo" ] \
    || fail "a second profile with no config of its own did not see the shared nickname"
# ...and --here is how a profile deliberately diverges. Uses branchMaxChars rather than the
# nickname because --set-nick needs an account, and the second profile deliberately has
# nothing in it at all — which is the point of the check above.
# Deliberately NOT 15: the later `--set-branch-max 15` gate must still be able to prove it
# changed something, and a value that was already there would make it pass vacuously.
s --set-branch-max 21 >/dev/null
[ "$(CLAUDE_CONFIG_DIR="$SECOND_PROFILE" cfg_get 'cfg["branchMaxChars"]')" = "21" ] \
    || fail "the second profile did not inherit the shared branchMaxChars"
CLAUDE_CONFIG_DIR="$SECOND_PROFILE" s --set-branch-max 9 --here >/dev/null
[ "$(CLAUDE_CONFIG_DIR="$SECOND_PROFILE" cfg_get 'cfg["branchMaxChars"]')" = "9" ] \
    || fail "--here did not override the shared value in the second profile"
[ "$(cfg_get 'cfg["branchMaxChars"]')" = "21" ] \
    || fail "--here in one profile changed another profile's value"
[ "$(layer_get shared 'cfg["branchMaxChars"]')" = "21" ] \
    || fail "--here wrote to the shared layer"
rm -rf "$SECOND_PROFILE"

run_expect "colour for you@example.com set to cyan" --set-color cyan
[ "$(cfg_get 'cfg["accountColors"]["you@example.com"]')" = "cyan" ] || fail "account colour did not persist"

run_expect "accounts with no colour of their own now render magenta" --set-default-color magenta
[ "$(cfg_get 'cfg["defaultAccountColor"]')" = "magenta" ] || fail "defaultAccountColor did not persist"

run_expect "set to magenta" --set-repo-color magenta
# The repo key is the name of the enclosing git repository, not of the directory we happen to
# be standing in — since Task 27 those differ, because the published tree is `plugin/` inside
# the repo. Derive it, never hardcode it, or this gate only passes in a checkout that happens
# to be called "claude-squirrel" (it would fail in any worktree, release tarball or fork).
REPO_KEY="$(basename "$(git -C "$ROOT" rev-parse --show-toplevel 2>/dev/null || echo "$ROOT")")"
[ "$(cfg_get "cfg[\"repoColors\"][\"$REPO_KEY\"]")" = "magenta" ] || fail "repoColors did not persist for '$REPO_KEY'"

run_expect "idle alarm set to 60s" --set-alarm 60
[ "$(cfg_get 'cfg["alarmAfterSeconds"]')" = "60" ] || fail "alarmAfterSeconds did not persist"

run_expect "idle background set to #112233" --set-bg "#112233"
[ "$(cfg_get 'cfg["idleBackground"]')" = "#112233" ] || fail "idleBackground did not persist"

run_expect "tinted after 45s" --set-bg-after 45
[ "$(cfg_get 'cfg["idleBackgroundAfterSeconds"]')" = "45" ] || fail "idleBackgroundAfterSeconds did not persist"

run_expect "branch name limit set to 15 chars" --set-branch-max 15
[ "$(cfg_get 'cfg["branchMaxChars"]')" = "15" ] || fail "branchMaxChars did not persist"

run_expect "segment 'context' is now off" --show-segment "context off"
[ "$(cfg_get 'cfg["segments"]["context"]')" = "False" ] || fail "segments.context did not persist"

echo "== layout =="
run_expect "line 1:" --layout ""
run_expect "line 3: git" --layout "move git 3"
[ "$(cfg_get '",".join(cfg["layout"][2])')" = "git" ] || fail "layout move did not persist"
run_expect "'git' priority is now 5" --layout "priority git 5"
[ "$(cfg_get 'cfg["segmentPriority"]["git"]')" = "5" ] || fail "segmentPriority did not persist"
run_expect "nothing was changed" --layout "line 2 model cost"
run_expect "unknown segment 'banana'" --layout "move banana 1"
run_expect "out of range" --layout "move git 9"
run_expect "layout and priorities reset" --layout "reset"
[ "$(cfg_get 'cfg["layout"][1][0]')" = "account" ] || fail "layout reset did not restore the shipped arrangement"
[ "$(cfg_get 'cfg["segmentPriority"]')" = "{}" ] || fail "layout reset did not clear priorities"

echo "== custom segments (reminders are a configuration of them) =="
run_expect "water: text='drink water' every=30m ack shared" --custom "add water every=30m ack=on shared=on text=drink water"
[ "$(cfg_get 'cfg["customSegments"][0]["every"]')" = "1800" ] || fail "customSegments did not persist"
run_expect "showing" --show-custom
run_expect "back in 30m" --ack "water"
run_expect "acknowledged, back later" --show-custom
run_expect "snoozed 'water' for 5m" --snooze "water 5"
run_expect "already a built-in" --custom "add cost text=nope"
run_expect "not a duration" --custom "add w every=soon text=x"
run_expect "needs text= or command=" --custom "add w every=1m"
run_expect "BLOCKED" --custom "add ci command=echo GREEN"
run_expect "custom segment commands are ON" --custom-commands on
run_expect "custom segment commands are OFF" --custom-commands off
run_expect "removed custom segment 'ci'" --custom "remove ci"
run_expect "every custom segment removed" --custom "reset"
[ "$(cfg_get 'cfg["customSegments"]')" = "[]" ] || fail "custom reset did not clear customSegments"

echo "== bar rules =="
# The ladder is shown as rules, and still driven by the legacy tunables set above
# (--set-bg-after 45, --set-alarm 60, --set-bg #112233) — proof the two are one mechanism.
run_expect "[ladder] when 'idle.secs > 45'   bg=#112233" --rules ""
run_expect "[ladder] when 'idle.secs > 60'" --rules ""
run_expect "[yours ] when 'weekly.pct >= 80'" --rules "add weekly.pct >= 80 bg=#7c3aed text=white bold=on"
[ "$(cfg_get 'cfg["barRules"][0]["bg"]')" = "#7c3aed" ] || fail "barRules did not persist"
run_expect "text=cyan" --rules "add git.branch == main text=cyan"
run_expect "unknown metric 'wibble'" --rules "add wibble >= 3 bg=red"
run_expect "unknown field 'sparkle'" --rules "add ctx.pct >= 90 sparkle=on bg=red"
run_expect "needs bg, text, or both" --rules "add ctx.pct >= 90"
run_expect "removed the rule" --rules "remove git.branch == main"
# `repo.name` in the rules PREVIEW falls back to the basename of the working directory
# (there is no status JSON to read a repo out of), which is $ROOT — the published tree,
# not the repository. Derive it so this keeps matching wherever the tree is checked out.
CWD_NAME="$(basename "$ROOT")"
run_expect "WINNING NOW" --rules "add repo.name == $CWD_NAME bg=#7c3aed"
run_expect "/squirrel-phases remove" --rules "remove idle.secs > 45"
run_expect "your bar rules are cleared" --rules "reset"
[ "$(cfg_get 'cfg["barRules"]')" = "[]" ] || fail "rule reset did not clear barRules"

echo "== segment styles =="
run_expect "(none" --style ""
run_expect "git: fg=#ff8800 bold=True" --style "set git fg=#ff8800 bold=on"
[ "$(cfg_get 'cfg["segmentStyles"]["git"]["fg"]')" = "#ff8800" ] || fail "segmentStyles did not persist"
run_expect "git: fg=#ff8800 bold=True opacity=0.5" --style "set git opacity=0.5"
run_expect "not a valid colour" --style "set git fg=banana"
run_expect "out of range" --style "set git opacity=5"
run_expect "unknown segment 'banana'" --style "set banana fg=red"
run_expect "style cleared for 'git'" --style "clear git"
run_expect "every segment style cleared" --style "reset"
[ "$(cfg_get 'cfg["segmentStyles"]')" = "{}" ] || fail "style reset did not clear segmentStyles"

run_expect "warnPercent set to 40" --set "warnPercent 40"
[ "$(cfg_get 'cfg["warnPercent"]')" = "40" ] || fail "warnPercent did not persist"

run_expect "alarm now triggers on: idle, working" --set-states "alarm idle,working"
[ "$(cfg_get '",".join(cfg["alarmStates"])')" = "idle,working" ] || fail "alarmStates did not persist"
run_expect "alarm states reset to idle, permission" --set-states "alarm reset"

run_expect "background now triggers on: permission, working" --set-states "background permission,working"
[ "$(cfg_get '",".join(cfg["idleBackgroundStates"])')" = "permission,working" ] || fail "idleBackgroundStates did not persist"
run_expect "background states reset to idle, permission" --set-states "background reset"

echo "== show-config / help-text =="
show_config_out="$(s --show-config)"
echo "$show_config_out"
for needle in "nickname    : demo" "colour      : cyan" "idle alarm  : 60s" \
    "usage fetch : off" "hidden      : context" "branch max  : 15 chars" "warnPercent=40"; do
    case "$show_config_out" in
    *"$needle"*) ;;
    *) fail "--show-config missing expected text: $needle" ;;
    esac
done
# The idle-painting ladder is an open-ended list of phases, not a fixed pair of fields, so its
# rendering is free to evolve (it already has, from a flat "idle bg" line to a phase-summary
# list). Assert on the *values* that must appear — the colour and threshold just set — rather
# than a hardcoded label and column spacing for whatever line currently shows them.
case "$show_config_out" in
*"45s"*"#112233"*) ;;
*) fail "--show-config does not show the configured idle threshold (45s) and colour (#112233) together" ;;
esac

help_out="$(s --help-text)"
# `... | head -5` under `set -o pipefail` is a latent flake: head exits after five lines,
# echo takes SIGPIPE, and the pipeline reports 141 — intermittently, depending on whether the
# output fit the pipe buffer first. It cost one FAIL(141) run here. sed reads to EOF, so the
# writer never sees a closed pipe.
printf '%s\n' "$help_out" | sed -n '1,5p' 
echo "  ... ($(echo "$help_out" | wc -l) lines total)"
case "$help_out" in
"squirrel status line — subcommands:"*) ;;
*) fail "--help-text missing its header" ;;
esac
help_flags=(
    "--set-nick <name>" "--set-color <colour>" "--set-alarm <seconds|off>" "--set-usage <on|off | keychain on|off>"
    "--show-segment <segment> <on|off>" "--set-branch-max <n>" "--set-bg [urgent|lines] <colour|off|auto|all|1,2>" "--set-bg-after <seconds>"
    "--set <key> <number>" "--set-repo-color <colour|off>" "--set-default-color <colour>"
    "--set-states <alarm|background> <states>" "--show-config" "--setup <plugin root>" "--show-sessions"
    "--show-layout" "--set-priority <segment|detail> <n|always>" "--show-styles" "--show-rules"
    "--show-custom" "--ack <id|all>" "--snooze <id> <minutes>" "--custom-commands <on|off>"
)
for flag in "${help_flags[@]}"; do
    case "$help_out" in
    *"$flag"*) ;;
    *) fail "--help-text does not document: $flag" ;;
    esac
done

echo "== launcher =="
[ -x "$RUN_SH" ] && echo "run.sh is executable"
# Task 18: the launcher must name a concrete interpreter, not the bare word `python3` — that
# name is a broken Microsoft Store stub on a default Windows install. Audit X1: the quoting is
# SINGLE, not double — nothing expands inside single quotes, and `run.sh` is the statusLine
# command, so a `$(...)` in the plugin path used to run on every render.
grep -q "^exec '/" "$RUN_SH" || fail "launcher does not exec a single-quoted absolute interpreter path"
if grep -q 'exec "' "$RUN_SH"; then
    fail "launcher interpolates into a double-quoted string (audit X1)"
fi
grep -qE '^exec "python3?"' "$RUN_SH" && fail "launcher baked a bare interpreter name instead of a resolved path"
echo "launcher interpreter: $(sed -n 's/^exec "\([^"]*\)".*/\1/p' "$RUN_SH")"

echo "== portable wrapper (what the hooks and slash commands run) =="
WRAPPER="$ROOT/scripts/squirrel.sh"
[ -x "$WRAPPER" ] || fail "scripts/squirrel.sh is missing or not executable"
wrapper_out="$(CLAUDE_PLUGIN_ROOT="$ROOT" sh "$WRAPPER" --help-text 2>&1)" \
    || fail "squirrel.sh failed to run the script"
printf '%s' "$wrapper_out" | grep -q 'subcommands' || fail "squirrel.sh did not reach statusline.py"
echo "squirrel.sh reached statusline.py via CLAUDE_PLUGIN_ROOT"
# and again with CLAUDE_PLUGIN_ROOT unset, the reported hook-env case (claude-code#27145)
wrapper_out2="$(env -u CLAUDE_PLUGIN_ROOT sh "$WRAPPER" --help-text 2>&1)" \
    || fail "squirrel.sh failed without CLAUDE_PLUGIN_ROOT"
printf '%s' "$wrapper_out2" | grep -q 'subcommands' || fail "squirrel.sh did not self-locate without CLAUDE_PLUGIN_ROOT"
echo "squirrel.sh self-located with CLAUDE_PLUGIN_ROOT unset"
# no shipped file may name an interpreter
if grep -rnE '(^|[^A-Za-z0-9_-])python3?[[:space:]]+["$]' "$ROOT/hooks" "$ROOT/commands" "$ROOT/skills" >/dev/null 2>&1; then
    grep -rnE '(^|[^A-Za-z0-9_-])python3?[[:space:]]+["$]' "$ROOT/hooks" "$ROOT/commands" "$ROOT/skills"
    fail "a shipped file still invokes an interpreter by name"
fi
echo "no shipped hook/command/skill file names an interpreter"
python3 -c "
import json
s = json.load(open('$SETTINGS_JSON'))
print('statusLine ->', s['statusLine']['command'])
"

echo "== state hooks + render =="
STATUS='{"session_id":"smoke","model":{"display_name":"Test Model"},"workspace":{"repo":{"name":"demo-repo"},"current_dir":"'"$ROOT"'"},"context_window":{"context_window_size":200000,"used_percentage":12,"current_usage":{"input_tokens":24000},"total_input_tokens":24000,"total_output_tokens":1000},"cost":{"total_cost_usd":0.5,"total_duration_ms":60000,"total_lines_added":1,"total_lines_removed":0},"rate_limits":{"five_hour":{"used_percentage":30,"resets_at":'"$(($(date +%s) + 3600))"'},"seven_day":{"used_percentage":9,"resets_at":'"$(($(date +%s) + 86400))"'}}}'

# chipWidth's shipped default (0) means "natural" padding: exactly one space on each side of
# the label, regardless of its length (see _chip_inner()) — not the old fixed-width centring
# these used to assume.
declare -A EXPECTED_CHIP=(
    [working]="[ ⚙ working ]"
    [subagent-start]="[ ⧗ 1 subagent ]"
    [permission]="[ ◀ needs you ]"
    [idle]="[ ✓ idle ]"
)
for state in working subagent-start permission idle; do
    printf '{"session_id":"smoke"}' | s --state "$state" >/dev/null
    line2="$(printf '%s' "$STATUS" | COLUMNS=200 "$RUN_SH" | sed -n 2p | strip_ansi)"
    echo "$line2"
    expected="${EXPECTED_CHIP[$state]}"
    case "$line2" in
    *"$expected") ;;
    *) fail "state '$state': line 2 does not end with expected chip '$expected' — got: $line2" ;;
    esac
done
printf '{"session_id":"smoke"}' | s --state end >/dev/null
line2_after_end="$(printf '%s' "$STATUS" | COLUMNS=200 "$RUN_SH" | sed -n 2p | strip_ansi)"
case "$line2_after_end" in
*"["*) fail "state 'end' should clear the activity chip, but line 2 still shows one: $line2_after_end" ;;
esac
echo "$line2_after_end (chip correctly cleared)"

echo "== no network: usage fetch never spawned =="
[ -e "$SQUIRREL_DIR/cache/usage.json" ] && fail "usage.json exists — a fetch ran despite usageFetch=off"
[ -e "$SQUIRREL_DIR/cache/fetch.lock" ] && fail "fetch.lock exists — a fetch was spawned despite usageFetch=off"
echo "no usage.json / fetch.lock under $SQUIRREL_DIR/cache — no fetch ran"

echo "== cross-session usage attribution (share / share-life / sessions / --show-sessions) =="
# The renders above (state hooks + render) already piped $STATUS — session_id "smoke" — through
# the real render path at least once, so record_session_sample() has already written this
# account's session into $SQUIRREL_SHARED_DIR; "smoke" is live by the time we get here.
run_expect "segment 'share' is now on" --show-segment "share on"
[ "$(cfg_get 'cfg["segments"]["share"]')" = "True" ] || fail "segments.share did not persist"
run_expect "segment 'share-life' is now on" --show-segment "share-life on"
[ "$(cfg_get 'cfg["segments"]["share-life"]')" = "True" ] || fail "segments.share-life did not persist"
run_expect "segment 'sessions' is now on" --show-segment "sessions on"
[ "$(cfg_get 'cfg["segments"]["sessions"]')" = "True" ] || fail "segments.sessions did not persist"

line1_attr="$(printf '%s' "$STATUS" | COLUMNS=200 "$RUN_SH" | sed -n 1p | strip_ansi)"
echo "$line1_attr"
# Task 11c: 'share' renders under the label 'mine', not a second, ambiguous '5h ...%'
# fragment — '5h' must appear exactly once on the line (the account-wide bucket only).
five_h_count="$(grep -o '5h' <<<"$line1_attr" | wc -l || true)"
[ "$five_h_count" -eq 1 ] || fail "'5h' should appear exactly once on line 1 (the account-wide bucket) — found $five_h_count — got: $line1_attr"
case "$line1_attr" in
*"mine "*"%"*) ;;
*) fail "'share' segment did not render under the 'mine' label (expected 'mine ...%' in line 1) — got: $line1_attr" ;;
esac
case "$line1_attr" in
*"life "*"%"*) ;;
*) fail "'share-life' segment did not render (expected 'life NN%' in line 1) — got: $line1_attr" ;;
esac
case "$line1_attr" in
*"▸"*) ;;
*) fail "'sessions' segment did not mark the current (only) live session with ▸ — got: $line1_attr" ;;
esac

echo "== 5h segment folds in this session's contribution once another session is live =="
# "smoke" was the only live session above, so session_contribution() correctly refused to
# show a composite there (trivially 100% — nothing to attribute against). Give the account a
# second live session directly in the shared store and confirm the 5h segment now shows
# '<contribution>/<total>%' instead of the plain '<total>%'.
python3 - <<'PYEOF'
import sys, time
sys.path.insert(0, "scripts")
import statusline as sl
sl.write_cache(sl.session_path("smoke-other", "you@example.com"),
               {"session_id": "smoke-other", "repo": "other-repo",
                # First sample is deliberately the legacy 3-element format (no model) — Task
                # 11e must tolerate a file mixing pre- and post-tracking samples. The second
                # sample carries a model, so its delta is attributed to "Opus 5".
                "samples": [[time.time() - 120, 0, 0.0], [time.time(), 500, 0.05, "Opus 5"]]})
PYEOF
line1_composite="$(printf '%s' "$STATUS" | COLUMNS=200 "$RUN_SH" | sed -n 1p | strip_ansi)"
echo "$line1_composite"
case "$line1_composite" in
*"5h "*"/"*"%"*) ;;
*) fail "5h segment did not fold in the contribution composite with a second live session present — got: $line1_composite" ;;
esac

# `wide`, because the table now sheds columns to fit the terminal and this assertion is
# about the table's CONTENT — the narrowing behaviour has its own section further down.
sessions_out="$(s --show-sessions "wide")"
echo "$sessions_out"
case "$sessions_out" in
*"5h cost"*"5h tok"*"life cost"*"model mix"*"burn"*"age"*) ;;
*) fail "--show-sessions is missing its table header — got: $sessions_out" ;;
esac
case "$sessions_out" in
*"demo-repo"*) ;;
*) fail "--show-sessions does not list the smoke session's repo (demo-repo) — got: $sessions_out" ;;
esac
# Task 11d: attribution ranks by cost by default, and the table explains why token counts
# and cost can diverge (subagent usage isn't reflected in the token counters).
case "$sessions_out" in
*"ranked by cost"*) ;;
*) fail "--show-sessions does not state that it ranks by cost by default — got: $sessions_out" ;;
esac
case "$sessions_out" in
*"subagent"*) ;;
*) fail "--show-sessions does not explain the tokens-vs-cost discrepancy (subagent usage) — got: $sessions_out" ;;
esac

echo "== attribution metric is configurable (Task 11d) =="
run_expect "attribution metric set to 'tokens'" --set-attribution-metric tokens
[ "$(cfg_get 'cfg["attributionMetric"]')" = "tokens" ] || fail "attributionMetric did not persist as 'tokens'"
tokens_ranked_out="$(s --show-sessions)"
case "$tokens_ranked_out" in
*"ranked by tokens"*) ;;
*) fail "--show-sessions did not switch its ranking note to tokens — got: $tokens_ranked_out" ;;
esac
run_expect "attribution metric set to 'cost'" --set-attribution-metric cost
[ "$(cfg_get 'cfg["attributionMetric"]')" = "cost" ] || fail "attributionMetric did not reset to 'cost'"

echo "== per-model mix column is configurable (Task 11e) =="
run_expect "will no longer include the per-model mix column" --set-sessions-show-model off
[ "$(cfg_get 'cfg["sessionsShowModel"]')" = "False" ] || fail "sessionsShowModel did not persist as off"
# `wide` here too: these assert on the model-mix COLUMN, which a narrow terminal sheds.
no_model_out="$(s --show-sessions "wide")"
case "$no_model_out" in
*"model mix"*) fail "--show-sessions still shows the model mix column after turning it off — got: $no_model_out" ;;
*) ;;
esac
run_expect "will include the per-model mix column" --set-sessions-show-model on
[ "$(cfg_get 'cfg["sessionsShowModel"]')" = "True" ] || fail "sessionsShowModel did not persist as on"
model_out="$(s --show-sessions "wide")"
case "$model_out" in
*"model mix"*"Opus 5"*) ;;
*) fail "--show-sessions does not show the per-model mix (Opus 5, from the mixed-format smoke-other session) — got: $model_out" ;;
esac

sessions_marked="$(s --show-sessions "smoke wide")"
echo "$sessions_marked"
case "$sessions_marked" in
*"this session"*) ;;
*) fail "--show-sessions <id> did not mark the matching session as current — got: $sessions_marked" ;;
esac

echo "== sessions table on a narrow terminal =="
narrow_sessions="$(COLUMNS=80 s --show-sessions smoke)"
while IFS= read -r line; do
    [ "${#line}" -le 80 ] || fail "sessions table line is ${#line} chars at COLUMNS=80: $line"
done <<<"$narrow_sessions"
case "$narrow_sessions" in
*"narrowed to fit 80 columns"*) ;;
*) fail "the sessions table did not say it had shed columns — got: $narrow_sessions" ;;
esac
case "$narrow_sessions" in
*"session"*"5h pts"*) ;;
*) fail "the sessions table dropped the columns it must never drop" ;;
esac
wide_sessions="$(COLUMNS=80 s --show-sessions "smoke wide")"
case "$wide_sessions" in
*"model mix"*) ;;
*) fail "--show-sessions wide did not print every column" ;;
esac
echo "  80 cols: sheds and says so; 'wide' overrides"

echo "== narrow width (50 columns) =="
narrow_lines="$(printf '%s' "$STATUS" | COLUMNS=50 "$RUN_SH" | strip_ansi)"
line_no=0
while IFS= read -r line; do
    line_no=$((line_no + 1))
    len=${#line}
    echo "line$line_no (len=$len): $line"
    [ "$len" -le 50 ] || fail "narrow render line $line_no is $len chars, exceeds requested width 50"
done <<<"$narrow_lines"
[ "$line_no" -eq 2 ] || fail "expected 2 rendered lines, got $line_no"
case "$narrow_lines" in
*"demo"*) ;; # nickname set earlier in this run — account segment always renders it or the email
*) fail "narrow render dropped the account segment entirely" ;;
esac

echo "== click to acknowledge (must open NO socket unless asked) =="
# The governing rule, checked end to end: count listening sockets before and after a render
# with a stock profile. Nothing may appear.
sockets_before="$(ss -ltn 2>/dev/null | wc -l || echo 0)"
printf '%s' "$STATUS" | COLUMNS=200 "$RUN_SH" >/dev/null
sockets_after="$(ss -ltn 2>/dev/null | wc -l || echo 0)"
[ "$sockets_before" = "$sockets_after" ] || fail "a render on a stock profile changed the listening-socket count ($sockets_before -> $sockets_after)"
[ ! -f "$SQUIRREL_DIR/click-listener.json" ] || fail "a stock render wrote a click-listener state file"
run_expect "click-to-acknowledge is ON" --click on
printf '%s' "$STATUS" | COLUMNS=200 "$RUN_SH" >/dev/null
[ ! -f "$SQUIRREL_DIR/click-listener.json" ] || fail "the feature alone, with nothing declaring click=, started a listener"
run_expect "not a click action" --custom "add ci every=1m ack=on click=command:ls text=x"
run_expect "click-to-acknowledge is OFF" --click off
run_expect "click terminals set to: ghostty" --click-terminals "ghostty"
run_expect "shipped list" --click-terminals reset
echo "  stock profile: no socket, no state file; command: refuses anything but ack/snooze"

echo "== background agents (degrades silently) =="
# No tasks directory exists for the smoke session, so the segment must render nothing and
# the bar must be otherwise unaffected. This is the common case and the one that must be free.
agents_out="$(printf '%s' "$STATUS" | COLUMNS=200 "$RUN_SH" | strip_ansi)"
case "$agents_out" in
*"agent"*) fail "the agents segment rendered with no tasks directory present: $agents_out" ;;
*) ;;
esac
echo "  no tasks dir -> no segment, no error"

echo "== hostile input (terminal-escape injection) =="
# A repo name carrying an OSC-8 hyperlink, an erase-line CSI and a newline, through the REAL
# launcher — the end-to-end proof that untrusted text cannot drive the terminal or break the
# line-count contract. Strip in place: "demo" survives, the sequences do not.
HOSTILE_STATUS='{"session_id":"smoke","model":{"display_name":"Test Model"},"workspace":{"repo":{"name":"demo\u001b]8;;http://evil.example\u0007\u001b[2K\ninjected"},"current_dir":"'"$ROOT"'"},"context_window":{"context_window_size":200000,"used_percentage":12},"cost":{"total_cost_usd":0.5,"total_duration_ms":60000}}'
hostile_out="$(printf '%s' "$HOSTILE_STATUS" | COLUMNS=200 "$RUN_SH")"
# $(...) strips the trailing newline, so count with a re-added one, not `wc -l` alone
hostile_lines="$(printf '%s\n' "$hostile_out" | wc -l)"
[ "$hostile_lines" -eq 2 ] || fail "hostile repo name changed the line count: got $hostile_lines lines, expected 2"
case "$hostile_out" in
*$'\033]'*) fail "an OSC sequence survived into the rendered line" ;;
*$'\033[2K'*) fail "an erase-line CSI survived into the rendered line" ;;
*) ;;
esac
case "$(printf '%s' "$hostile_out" | strip_ansi)" in
*"demoinjected"*) ;;
*) fail "hostile input was truncated instead of stripped in place — got: $(printf '%s' "$hostile_out" | strip_ansi)" ;;
esac
echo "  2 lines, no escapes survived, text stripped in place"

echo "== OK =="

# ── verify the real profile was never touched ────────────────────────────────
real_after="$(profile_snapshot "$REAL_SQUIRREL_DIR" 2>&1 || true)"
if [ "$real_before" != "$real_after" ]; then
    fail "real profile at $REAL_SQUIRREL_DIR changed during the smoke test$(printf '\nbefore:\n%s\nafter:\n%s' "$real_before" "$real_after")"
fi
echo "real profile untouched (names, modes, and content outside cache/): $REAL_SQUIRREL_DIR"
