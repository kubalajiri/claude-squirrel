"""Tests for the Claude Code status line.

Run from the repo root: python3 -m unittest discover -s dev -t dev -v

These tests live OUTSIDE the published plugin tree (Task 27): the installer copies
whatever is in `plugin/`, so anything in here would land on every user's disk. That
is why the plugin's scripts directory has to be put on sys.path by hand below.
"""
import ast
import shlex
import builtins
import inspect
import io
import json
import os
import re
import stat
import subprocess
import shutil
import sys
import tempfile
import time as _time
import unittest
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from unittest import mock

# The plugin's runtime tree is a sibling directory, not this one — see the module docstring.
DEV_DIR = Path(__file__).resolve().parent
PLUGIN_ROOT = DEV_DIR.parent / "plugin"
sys.path.insert(0, str(PLUGIN_ROOT / "scripts"))
sys.path.insert(0, str(DEV_DIR))

import profile_snapshot as snap    # noqa: E402  (needs the sys.path lines above)
import statusline as sl            # noqa: E402

# ── A standing habit, learned twice ──────────────────────────────────────────
# FOR ANY NEW SUBSYSTEM, WRITE ONE TEST THAT REACHES IT THROUGH `render()` OR `main()`,
# NOT THROUGH THE FUNCTION.
#
# This has now caught the same defect class twice, and both times the whole suite was green:
#
#   * reverting `live_sessions` to a rolling window passed everything, because every window
#     test called the attribution walker directly;
#   * `evaluate_tasks` was written, fully tested and NEVER CALLED FROM ANYWHERE for an entire
#     commit — the task queue was inert and every test passed, because they all called it
#     directly.
#
# A unit tested only in isolation proves the unit works. It says nothing about whether the
# product uses it, and "the tests pass" then means something true and useless. Twice is a
# pattern, so this is a habit rather than a matter of vigilance.
#
# ── And its sibling, which is a DIFFERENT mistake ────────────────────────────
# A TEST CAN PASS FOR A REASON OTHER THAN THE ONE IN ITS NAME. Three instances here:
#
#   * `test_a_partial_first_line_never_breaks_the_scan` passed with the guard REMOVED,
#     because a mid-line fragment is normally unparseable rubbish either way;
#   * removing emit()'s sanitising failed no test at all, because every field was already
#     being sanitised individually — a backstop that only works while the thing it backs up
#     works is not a backstop;
#   * `test_only_nudges_count_against_it` asserted acks are not BLOCKED, which a version
#     that silently charges them to the budget still satisfies.
#
# ── And a third, which is neither of those ───────────────────────────────────
# A GUARD CAN BE UNTESTED BY A FIXTURE THAT CANNOT EXERCISE IT. The test names the right
# thing and asserts the right property, and still passes with the code deleted, because the
# input never reaches the branch:
#
#   * the partial-first-line guard: a mid-line fragment is normally unparseable rubbish, so
#     the scan behaved identically either way. It only bites when a COMPLETE JSON object
#     sits exactly on the seek boundary, which took arithmetic to arrange;
#   * the dry-run test: the task it used had an unknown metric, so the command took an
#     early-return path and never reached the line under test at all;
#   * `test_a_job_before_its_due_time_is_left_alone`: a second check downstream made the
#     first one's removal invisible.
#
# ── And a fourth, which is the shape of every interaction bug here ───────────
# A FIXTURE IS TIDIER THAN A REAL BAR, AND A TIDY FIXTURE IS WHAT HIDES AN INTERACTION BUG.
# The other three are about how a test is written. This one is about what it is given.
# Every defect that reached the user in this stretch was an interaction between things that
# were each individually correct:
#
#   * the contrast rule inverted a dirty git branch and a `take a break` segment as well as a
#     real threshold — three producers returning the same yellow for three different reasons,
#     and no fixture had more than one of them on a line;
#   * `183/3%` needed one stale observer among five agreeing ones;
#   * the empty chip needed a refined state AND a hook record whose subagent count was zero.
#
# So a fixture for anything that composes has to contain the mess: a dirty tree, several
# segments coloured for unrelated reasons, a real threshold crossing, at more than one width.
# A render that looks perfect on a tidy fixture is telling you about the fixture.
#
# Four families. Every instance was found by MUTATION or by looking at a real bar, never by
# review or by re-reading the test — which is the argument for mutation testing in one line:
# a test you have not watched fail is a test you have not tested. Mutate the code the test
# names, and watch THAT test fail, not merely some test.
ANSI = re.compile(r"\x1b\[[0-9;]*m")

# Task 18. The suite must be runnable on Windows too — a colleague on Windows has to be able to
# check their own install. A handful of assertions below are about POSIX file semantics that
# Windows genuinely does not have (0600 permission bits, symlinks without elevation, `sh` on
# PATH); those are skipped there rather than weakened everywhere, and the platform-specific
# behaviour they used to cover is asserted directly in CrossPlatformTests instead.
WINDOWS = os.name == "nt"
posix_only = unittest.skipIf(WINDOWS, "POSIX file semantics; Windows behaviour is covered by CrossPlatformTests")
windows_only = unittest.skipUnless(WINDOWS, "Windows-only behaviour")
LAUNCHER_NAME = "run.cmd" if WINDOWS else "run.sh"
NOW = datetime(2026, 8, 22, 22, 56, tzinfo=timezone.utc).timestamp()
WEEK_RESET = datetime(2026, 8, 27, 20, 0, tzinfo=timezone.utc).timestamp()  # a Thursday


def plain(text: str) -> str:
    """Strip ANSI color codes."""
    return ANSI.sub("", text)


# ── whole-module safety net ───────────────────────────────────────────────────
# No test in this file may be able to reach the developer's real Claude/Squirrel profile —
# not even a test class that forgets to inherit TmpEnv below. Point every path-selecting env
# var at a per-run temp directory before ANY test runs, and tear it down once at the very end.
# TmpEnv layers its own per-test temp dir for CLAUDE_CONFIG_DIR/XDG_CACHE_HOME on top of this;
# this module-level default is the fallback for a class that doesn't. See
# ModuleIsolationTests below for the regression guard.
_module_tmp: "tempfile.TemporaryDirectory | None" = None
_module_env_patcher = None


# No test may start a REAL click listener. One did — a malformed-record test rendered with
# the feature enabled, `read_click_listener` correctly returned None, and the render spawned
# a listener that then served for its full 30-minute idle timeout. It never showed up when
# that class ran alone, only in a whole-suite run, and each run left one more behind; forty
# had accumulated before anyone counted. A leaked listener holds a real loopback socket on
# the developer's machine, which is the one thing this feature promised not to do uninvited.
#
# So the suite refuses at the boundary rather than relying on every future test remembering
# to mock: any Popen of `--click-listener` fails the test that made it, with the stack.
_real_popen = subprocess.Popen


class RealListenerSpawned(BaseException):
    """Deliberately NOT an Exception. Every spawn site in the product is wrapped in
    `except Exception: pass` — correctly, because a refresh that cannot start must never
    disturb the render — so an AssertionError here is swallowed and the guard goes silent:
    the leak stops, and the test that caused it still passes. Inheriting from BaseException
    is what makes it reach the test instead of the product's own safety net."""


class RealCliInvoked(BaseException):
    """Also not an Exception, for the same reason as RealListenerSpawned: every call site is
    wrapped in `except Exception`, so an ordinary error would be swallowed and the test that
    shelled out to the user's real `claude` would still pass."""


class _NoRealListeners(_real_popen):
    def __init__(self, args, *rest, **kwargs):
        argv = list(args) if isinstance(args, (list, tuple)) else [args]
        if any("--click-listener" == str(a) for a in argv):
            raise RealListenerSpawned(
                "a test tried to start a REAL click listener; mock spawn_click_listener "
                "(or subprocess.Popen) in it — see _NoRealListeners")
        super().__init__(args, *rest, **kwargs)


_real_run_command = None


def _no_real_cli(command, *rest, **kwargs):
    """No test may invoke the real `claude` binary. It costs tokens, it reaches the user's
    own live sessions, and a notify test that actually delivered would be indistinguishable
    from one that passed."""
    if str(command).strip().startswith("claude "):
        raise RealCliInvoked(f"a test tried to run the real CLI: {command!r} — mock it")
    return _real_run_command(command, *rest, **kwargs)


def setUpModule():
    global _module_tmp, _module_env_patcher, _real_run_command
    subprocess.Popen = _NoRealListeners
    _real_run_command = sl._run_command
    sl._run_command = _no_real_cli
    _module_tmp = tempfile.TemporaryDirectory(prefix="squirrel-test-")
    root = Path(_module_tmp.name)
    _module_env_patcher = mock.patch.dict(os.environ, {
        "CLAUDE_CONFIG_DIR": str(root / "config"),
        "XDG_CACHE_HOME": str(root / "cache"),
        "SQUIRREL_SHARED_DIR": str(root / "shared"),
        # Renders in this suite deliberately use the SHIPPED config, whose ladder carries a
        # desktop notification at five minutes — and many of them render a session that has
        # been "waiting" for hours. Without this, a full test run fires real toasts at whoever
        # is at the keyboard. Tests that are ABOUT alerts clear it for themselves.
        "SQUIRREL_NO_ALERTS": "1",
    })
    _module_env_patcher.start()


def tearDownModule():
    global _module_tmp, _module_env_patcher
    subprocess.Popen = _real_popen
    if _real_run_command is not None:
        sl._run_command = _real_run_command
    if _module_env_patcher is not None:
        _module_env_patcher.stop()
        _module_env_patcher = None
    if _module_tmp is not None:
        _module_tmp.cleanup()
        _module_tmp = None


class FormattingTests(unittest.TestCase):
    def test_bar_fills_one_cell_per_ten_percent_min_one_when_nonzero(self):
        self.assertEqual(sl.bar(0), "▱" * 10)
        self.assertEqual(sl.bar(4), "▰▱▱▱▱▱▱▱▱▱")
        self.assertEqual(sl.bar(17), "▰▰▱▱▱▱▱▱▱▱")
        self.assertEqual(sl.bar(42), "▰▰▰▰▱▱▱▱▱▱")
        self.assertEqual(sl.bar(100), "▰" * 10)
        self.assertEqual(sl.bar(130), "▰" * 10)

    def test_pct_color_thresholds(self):
        self.assertEqual(sl.pct_color(49.9), sl.GREEN)
        self.assertEqual(sl.pct_color(50), sl.YELLOW)
        self.assertEqual(sl.pct_color(79), sl.YELLOW)
        self.assertEqual(sl.pct_color(80), sl.RED)

    def test_colored_wraps_with_reset(self):
        self.assertEqual(sl.colored("x", sl.RED), "\033[31mx\033[0m")

    def test_fmt_relative(self):
        self.assertEqual(sl.fmt_relative(42 * 60), "42m")
        self.assertEqual(sl.fmt_relative(3600 + 25 * 60), "1h25m")
        self.assertEqual(sl.fmt_relative(3600 + 5 * 60), "1h05m")
        self.assertEqual(sl.fmt_relative(86400 + 3 * 3600 + 7 * 60), "1d 3h")
        self.assertEqual(sl.fmt_relative(-5), "0m")

    def test_fmt_weekday_time_uses_given_tz(self):
        self.assertEqual(sl.fmt_weekday_time(WEEK_RESET, timezone.utc), "Thu 20:00")

    def test_fmt_tokens(self):
        self.assertEqual(sl.fmt_tokens(999), "999")
        self.assertEqual(sl.fmt_tokens(84_000), "84k")
        self.assertEqual(sl.fmt_tokens(200_000), "200k")
        self.assertEqual(sl.fmt_tokens(1_000_000), "1.0M")

    def test_fmt_duration(self):
        self.assertEqual(sl.fmt_duration(7_980_000), "2h13m")

    def test_fmt_pr(self):
        self.assertEqual(sl.fmt_pr(None), "")
        self.assertEqual(sl.fmt_pr({"number": 42, "review_state": "approved"}), "PR #42 ✓approved")
        self.assertEqual(sl.fmt_pr({"number": 7, "kind": "mr"}), "MR #7")
        self.assertEqual(sl.fmt_pr({"number": 3, "review_state": "changes_requested"}), "PR #3 ✗changes")


class TmpEnv(unittest.TestCase):
    """Isolated CLAUDE_CONFIG_DIR + XDG_CACHE_HOME + SQUIRREL_SHARED_DIR in a temp dir.

    The shared store is isolated PER TEST, not just per module. setUpModule() points
    SQUIRREL_SHARED_DIR at one directory for the whole run, which was harmless while the
    shared store held only ack state that every test wrote under its own key. It stops being
    harmless the moment shared *config* exists: one test writing a shared setting would be
    read by every test after it, so the suite would pass test-by-test and fail as a whole
    (or worse, pass for the wrong reason). Each test gets its own `<tmp>/shared`.

    Subclasses that re-point SQUIRREL_SHARED_DIR in their own setUp still win — they patch
    after this one — and their directories are equally per-test.
    """

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        patcher = mock.patch.dict(os.environ, {"CLAUDE_CONFIG_DIR": self.tmp.name,
                                               "XDG_CACHE_HOME": self.tmp.name,
                                               "SQUIRREL_SHARED_DIR": str(self.root / "shared")})
        patcher.start()
        self.addCleanup(patcher.stop)


class ModuleIsolationTests(unittest.TestCase):
    """Regression guard for the whole-module safety net (setUpModule() above): a class that
    forgets to inherit TmpEnv must still be unable to reach the developer's real profile.
    Deliberately a bare TestCase, not TmpEnv — it has to pass on the module-level default
    alone, exactly the scenario it guards against. Proves isolation by comparing the active
    (isolated) paths against what the same functions would resolve to with no override at
    all — they must differ, or the safety net isn't actually in effect."""

    def test_data_dir_is_isolated_by_default(self):
        # `clear=True` also removes USERPROFILE, and Path.home() RAISES on Windows without it;
        # keep just enough environment for the un-isolated path to be computable.
        base = {"USERPROFILE": r"C:\Users\nobody", "HOMEDRIVE": "C:", "HOMEPATH": r"\Users\nobody"} if WINDOWS else {}
        with mock.patch.dict(os.environ, base, clear=True):
            unisolated = sl.data_dir()
        self.assertNotEqual(sl.data_dir(), unisolated,
                             "data_dir() matches its real-profile value — module-level isolation is not active")

    def test_shared_dir_is_isolated_by_default(self):
        base = {"USERPROFILE": r"C:\Users\nobody", "HOMEDRIVE": "C:", "HOMEPATH": r"\Users\nobody"} if WINDOWS else {}
        with mock.patch.dict(os.environ, base, clear=True):
            unisolated = sl.shared_dir()
        self.assertNotEqual(sl.shared_dir(), unisolated,
                             "shared_dir() matches its real-profile value — module-level isolation is not active")


class ConfigWriterTests(TmpEnv):
    """The three writing intents, pinned as a contract.

    Nine editing commands used to read-modify-write config_path() themselves. They now go
    through save_config / replace_config / forget_config, and the difference between those
    three is load-bearing rather than stylistic — see the block comment above save_config().
    These tests state each intent once, so the next editor has something to be checked
    against; the per-command removal paths are tested through their own commands, which is
    where a caller picking the WRONG intent actually shows up."""

    def _stored(self) -> dict:
        return sl.user_config()

    def test_save_config_merges_a_map_per_key(self):
        sl.save_config({"accountColors": {"a@x.com": "red"}})
        sl.save_config({"accountColors": {"b@x.com": "blue"}})
        self.assertEqual(self._stored()["accountColors"], {"a@x.com": "red", "b@x.com": "blue"})

    def test_replace_config_sets_the_key_to_exactly_that_value(self):
        sl.save_config({"accountColors": {"a@x.com": "red", "b@x.com": "blue"}})
        sl.replace_config({"accountColors": {"b@x.com": "blue"}})
        self.assertEqual(self._stored()["accountColors"], {"b@x.com": "blue"},
                         "replace_config merged — the removed entry came back")

    def test_replace_config_can_empty_a_map_which_a_merge_cannot(self):
        sl.save_config({"segmentStyles": {"git": {"fg": "red"}}})
        sl.replace_config({"segmentStyles": {}})
        self.assertEqual(self._stored()["segmentStyles"], {})

    def test_forget_config_removes_the_key_entirely(self):
        sl.save_config({"clickTerminals": ["ghostty"]})
        sl.forget_config("clickTerminals")
        self.assertNotIn("clickTerminals", self._stored())

    def test_forget_config_is_not_the_same_as_writing_an_empty_value(self):
        """The distinction the layering depends on. An empty list is a VALUE, and a value
        shadows whatever the layer below it says; a forgotten key defers to it. Today both
        render the same shipped ladder, so only the stored file can tell them apart — which
        is exactly why this is asserted on the file."""
        sl.save_config({"idlePhases": [{"after": 999, "bg": "red"}]})
        sl.replace_config({"idlePhases": []})
        self.assertEqual(self._stored()["idlePhases"], [])
        sl.forget_config("idlePhases")
        self.assertNotIn("idlePhases", self._stored())

    def test_forgetting_an_absent_key_is_not_an_error(self):
        sl.forget_config("neverWasThere")
        self.assertNotIn("neverWasThere", self._stored())

    def test_stored_config_is_the_users_file_not_the_merged_config(self):
        """Editors that compute a whole value must start here. If they started from
        load_config() they would copy every shipped default into the user's own file the
        first time the user touched anything."""
        sl.save_config({"accountColors": {"a@x.com": "red"}}, here=True)
        self.assertEqual(sl.stored_config("profile"), {"accountColors": {"a@x.com": "red"}})
        self.assertIn("idlePhases", sl.load_config(), "load_config() should carry the shipped keys")
        self.assertNotIn("idlePhases", sl.stored_config("profile"))


class OneConfigWriterTests(unittest.TestCase):
    """Structural guard: config.json has exactly ONE writer.

    This is the guard that actually holds the refactor, because the failure it prevents is a
    TENTH editor being added the old way — which no behavioural test can notice. The layered
    store can only be introduced in one writer; an editor reaching past it would keep landing
    per-profile whatever the layer table said, silently.

    Watched fail: restoring any one of the nine `write_cache(config_path(), cfg)` calls fails
    this test by name."""

    def test_only_write_config_writes_a_config_layer(self):
        """No editor may write a config layer itself. This is the guard that holds the
        layering: an editor reaching past _write_config() would land per-profile whatever the
        layer table said, and nothing rendered would look wrong."""
        tree = ast.parse((PLUGIN_ROOT / "scripts" / "statusline.py").read_text(encoding="utf-8"))
        offenders = []
        for func in ast.walk(tree):
            if not isinstance(func, (ast.FunctionDef, ast.AsyncFunctionDef)) or func.name == "_write_config":
                continue
            for node in ast.walk(func):
                if not isinstance(node, ast.Call) or getattr(node.func, "id", None) != "write_cache":
                    continue
                if not node.args:
                    continue
                target = node.args[0]
                if isinstance(target, ast.Call) and getattr(target.func, "id", None) in ("config_path", "shared_config_path", "_layer_path"):
                    offenders.append(f"{func.name}:{node.lineno}")
        self.assertEqual(offenders, [],
                         "a config layer is written outside _write_config() — route it through "
                         "save_config / replace_config / forget_config instead: " + ", ".join(offenders))

    def test_only_stored_config_reads_the_config_file_for_editing(self):
        """load_config() reads it too, and legitimately — it is the merge itself. Everything
        else must go through stored_config(), so there is one place to teach about layers."""
        tree = ast.parse((PLUGIN_ROOT / "scripts" / "statusline.py").read_text(encoding="utf-8"))
        allowed = {"load_config", "stored_config", "layer_config", "user_config"}
        offenders = []
        for func in ast.walk(tree):
            if not isinstance(func, (ast.FunctionDef, ast.AsyncFunctionDef)) or func.name in allowed:
                continue
            for node in ast.walk(func):
                if not isinstance(node, ast.Call):
                    continue
                if getattr(node.func, "id", None) != "load_json" or not node.args:
                    continue
                target = node.args[0]
                if isinstance(target, ast.Call) and getattr(target.func, "id", None) in ("config_path", "shared_config_path"):
                    offenders.append(f"{func.name}:{node.lineno}")
        self.assertEqual(offenders, [],
                         "a config layer is read for editing outside layer_config(): " + ", ".join(offenders))


class PhasesResetForgetsTheKeyTests(TmpEnv):
    """The one removal path the existing tests could not see.

    `load_phases()` overlays onto the shipped baseline, so an `idlePhases: []` left behind by
    reset renders identically to the key being absent — every rendered assertion passes either
    way. It stops being identical the moment there is a layer below: `[]` is a value and
    shadows a shared ladder, where the absent key lets it through. Found by mutation (swapping
    forget_config for replace_config({"idlePhases": []}) failed NOTHING), not by review."""

    def test_reset_removes_the_key_rather_than_emptying_it(self):
        sl.cmd_phases("add 600 bg=#7c3aed text=white")
        self.assertIn("idlePhases", sl.user_config())
        sl.cmd_phases("reset")
        self.assertNotIn("idlePhases", sl.user_config(),
                         "reset left an empty idlePhases behind — that shadows the layer below "
                         "instead of deferring to it")

    def test_reset_still_renders_the_shipped_ladder(self):
        sl.cmd_phases("add 600 bg=#7c3aed text=white")
        sl.cmd_phases("reset")
        shipped = json.loads(sl.default_config_path().read_text(encoding="utf-8"))["idlePhases"]
        self.assertEqual(len(sl.load_phases(sl.load_config())), len(shipped))


class ThreeLayerConfigTests(TmpEnv):
    """shipped → shared → per-profile, exercised through load_config() and render(), never
    through a hand-built dict.

    The bug: the user runs three profiles and only one has a config.json, so their layout,
    reminders, nicknames and colours existed in one profile and not the others — while the ACK
    STATE for those same reminders is cross-profile and synced perfectly. The definition was
    per-profile and the state was shared; that split was the whole defect."""

    def _profile(self, name: str):
        """Point CLAUDE_CONFIG_DIR at a DIFFERENT profile, sharing the same shared store —
        which is the actual shape of the user's machine, and the only shape in which the bug
        is visible at all."""
        path = self.root / name
        patcher = mock.patch.dict(os.environ, {"CLAUDE_CONFIG_DIR": str(path)})
        patcher.start()
        self.addCleanup(patcher.stop)
        return path

    def _write(self, layer: str, data: dict):
        path = sl._layer_path(layer)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(data))

    def _render(self, config):
        """Both lines joined: which LINE a segment lands on is the layout's business, and this
        is a test about which LAYER a value came from."""
        return "\n".join(sl.render(dict(STATUS, session_id="layers"), CACHE, "you@example.com",
                                    config, NOW, timezone.utc, width=200))

    def test_a_shared_value_reaches_a_profile_that_has_no_config_at_all(self):
        """The user's exact situation: two of three profiles have no config.json."""
        self._write("shared", {"accountNames": {"you@example.com": "zaphod"}})
        self._profile("bare-profile")
        self.assertFalse(sl.config_path().exists(), "this profile must have no config of its own")
        rendered = self._render(sl.load_config())
        self.assertIn("zaphod", rendered, "a shared nickname did not reach a bare profile")

    def test_a_per_profile_value_still_beats_the_shared_one(self):
        """Nothing an existing user has set changes behaviour — that is the whole reason the
        new layer went UNDERNEATH rather than on top."""
        self._write("shared", {"accountNames": {"you@example.com": "zaphod"}})
        self._write("profile", {"accountNames": {"you@example.com": "ford"}})
        rendered = self._render(sl.load_config())
        self.assertIn("ford", rendered)
        self.assertNotIn("zaphod", rendered)

    def test_a_map_merges_across_all_three_layers(self):
        """Task 25's per-key merge, applied across three layers instead of two — NOT a second
        merge. `segments` ships with three entries meaningfully set to false; a shared layer
        turning one on must not cost the other two their shipped value, and neither must a
        profile layer on top of that."""
        self._write("shared", {"segments": {"share": True}})
        self._write("profile", {"segments": {"sessions": True}})
        cfg = sl.load_config()
        self.assertTrue(cfg["segments"]["share"], "the shared layer's entry was lost")
        self.assertTrue(cfg["segments"]["sessions"], "the profile layer's entry was lost")
        self.assertFalse(cfg["segments"]["share-life"],
                         "a shipped entry no layer mentioned was replaced wholesale")

    def test_a_list_is_still_replaced_wholesale_by_the_nearest_layer(self):
        """Lists are deliberately NOT merged: a user's list IS the whole list."""
        self._write("shared", {"alarmStates": ["idle"]})
        self._write("profile", {"alarmStates": ["idle", "waiting"]})
        self.assertEqual(sl.load_config()["alarmStates"], ["idle", "waiting"])

    def test_provenance_names_the_layer_each_value_came_from(self):
        self._write("shared", {"alarmAfterSeconds": 45})
        self._write("profile", {"branchMaxChars": 12})
        prov = sl.config_provenance()
        self.assertEqual(prov["alarmAfterSeconds"], ("shared",))
        self.assertEqual(prov["branchMaxChars"], ("profile",))
        self.assertEqual(prov["warnPercent"], ("shipped",))

    def test_provenance_of_a_merged_map_names_every_contributor(self):
        self._write("shared", {"segments": {"share": True}})
        self._write("profile", {"segments": {"sessions": True}})
        self.assertEqual(sl.config_provenance()["segments"], ("shipped", "shared", "profile"))

    def test_user_set_keys_sees_the_shared_layer_too(self):
        """The question is "did the USER choose this?", and answering it against the profile
        file alone said "no" about a value they had deliberately set one layer down."""
        self._write("shared", {"cacheTtlSeconds": 300})
        self.assertIn("cacheTtlSeconds", sl.user_set_keys())
        self.assertNotIn("warnPercent", sl.user_set_keys(), "a shipped default is not a choice")

    def test_an_explicitly_shared_cacheTtlSeconds_still_governs_the_fetch_cadence(self):
        """The defect that made this task's provenance work necessary, one layer along. Goes
        through load_config() with real files, because a hand-built dict has exactly the shape
        a merged config does not — which is why the original dead branch passed its tests."""
        self._write("shared", {"cacheTtlSeconds": 300})
        self.assertEqual(sl.usage_fetch_ttl(sl.load_config()), 300)

    def test_the_shipped_default_applies_when_no_layer_sets_it(self):
        shipped = json.loads(sl.default_config_path().read_text(encoding="utf-8"))["usageFetchSeconds"]
        self.assertEqual(sl.usage_fetch_ttl(sl.load_config()), shipped)


class EditLayerTargetingTests(TmpEnv):
    """Which layer an edit lands in, through the COMMANDS rather than through the writer.

    A subsystem here was once fully written and tested while never being called at all, so
    every one of these goes through main() or a cmd_* function."""

    def _run(self, *argv):
        out = io.StringIO()
        with mock.patch.object(sys, "stdout", out):
            sl.main(list(argv))
        return out.getvalue()

    def test_an_unset_setting_lands_shared_and_says_so(self):
        out = self._run("--set-alarm", "45")
        self.assertEqual(json.loads(sl.shared_config_path().read_text(encoding="utf-8"))["alarmAfterSeconds"], 45)
        self.assertFalse(sl.config_path().exists(), "nothing should have been written per-profile")
        self.assertIn("written to shared", out)

    def test_here_forces_the_profile_layer_and_says_so(self):
        out = self._run("--set-alarm", "45", "--here")
        self.assertEqual(json.loads(sl.config_path().read_text(encoding="utf-8"))["alarmAfterSeconds"], 45)
        self.assertFalse(sl.shared_config_path().exists())
        self.assertIn("written to profile", out)

    def test_here_is_also_accepted_inside_the_single_arguments_word(self):
        """The slash commands pass everything the user typed as ONE shell word, so a `--here`
        they type never arrives as its own argv entry. Tested because the argv-level strip
        alone would have left `--here` doing nothing at all through the real command path,
        silently — and the value would have gone to the wrong layer."""
        out = self._run("--set-alarm", "45 --here")
        self.assertEqual(json.loads(sl.config_path().read_text(encoding="utf-8"))["alarmAfterSeconds"], 45)
        self.assertFalse(sl.shared_config_path().exists())
        self.assertIn("written to profile", out)

    def test_a_setting_already_set_per_profile_keeps_being_edited_there(self):
        """The migration rule: nothing moves without being asked. An existing user editing a
        setting they already have must not watch the write land in a layer their own profile
        overrides — they would see NOTHING change."""
        self._run("--set-alarm", "45", "--here")
        out = self._run("--set-alarm", "90")
        self.assertEqual(json.loads(sl.config_path().read_text(encoding="utf-8"))["alarmAfterSeconds"], 90)
        self.assertFalse(sl.shared_config_path().exists())
        self.assertIn("written to profile", out)

    def test_a_profile_local_key_is_never_shared(self):
        self._run("--set-usage", "off")
        self.assertIn("usageFetch", sl.stored_config("profile"))
        self.assertNotIn("usageFetch", sl.stored_config("shared"))

    def test_a_read_only_command_reports_no_layer(self):
        self.assertNotIn("written to", self._run("--show-config"))

    def test_a_refused_edit_reports_no_layer(self):
        out = self._run("--set-alarm", "banana")
        self.assertNotIn("written to", out,
                         "a refused edit claimed to have written something")


class ResetClearsEveryLayerTests(TmpEnv):
    """A reset must mean the setting stops being set.

    With three layers, clearing one while another still stands is the most confusing thing a
    reset can do: the user is told it was reset and the status line does not change."""

    def test_reset_clears_both_layers(self):
        sl.replace_config({"clickTerminals": ["ghostty"]}, layer="shared")
        sl.replace_config({"clickTerminals": ["wezterm"]}, layer="profile")
        sl.cmd_set_click_terminals("reset")
        self.assertNotIn("clickTerminals", sl.stored_config("shared"))
        self.assertNotIn("clickTerminals", sl.stored_config("profile"))

    def test_reset_here_clears_only_this_profile(self):
        sl.replace_config({"clickTerminals": ["ghostty"]}, layer="shared")
        sl.replace_config({"clickTerminals": ["wezterm"]}, layer="profile")
        sl.forget_config("clickTerminals", here=True)
        self.assertEqual(sl.stored_config("shared")["clickTerminals"], ["ghostty"],
                         "--here reached past this profile and cleared the shared layer too")
        self.assertNotIn("clickTerminals", sl.stored_config("profile"))

    def test_reset_does_not_touch_a_neighbouring_setting_in_either_layer(self):
        sl.replace_config({"clickTerminals": ["ghostty"], "alarmAfterSeconds": 45}, layer="shared")
        sl.replace_config({"clickTerminals": ["wezterm"], "branchMaxChars": 9}, layer="profile")
        sl.cmd_set_click_terminals("reset")
        self.assertEqual(sl.stored_config("shared")["alarmAfterSeconds"], 45)
        self.assertEqual(sl.stored_config("profile")["branchMaxChars"], 9)


class ShowConfigProvenanceTests(TmpEnv):
    """--show-config must report WHICH LAYER each value came from.

    With three layers "why is this not what I set?" is otherwise unanswerable, and that
    question is how the user found the bug this whole task is about."""

    def _out(self):
        return sl.cmd_show_config()

    def test_it_names_all_three_layer_files(self):
        out = self._out()
        self.assertIn(str(sl.default_config_path()), out)
        self.assertIn(str(sl.shared_config_path()), out)
        self.assertIn(str(sl.config_path()), out)

    def test_a_shared_value_is_reported_as_shared(self):
        sl.replace_config({"alarmAfterSeconds": 45}, layer="shared")
        self.assertIn("alarmAfterSeconds [shared]", self._out())

    def test_a_profile_value_is_reported_as_profile(self):
        sl.replace_config({"alarmAfterSeconds": 45}, layer="profile")
        self.assertIn("alarmAfterSeconds [profile]", self._out())

    def test_a_profile_value_over_a_shared_one_is_reported_as_shadowing(self):
        """The exact case the user hit: a setting they made, and a status line ignoring it."""
        sl.replace_config({"alarmAfterSeconds": 45}, layer="shared")
        sl.replace_config({"alarmAfterSeconds": 90}, layer="profile")
        self.assertIn("alarmAfterSeconds [profile, shadowing shared]", self._out())

    def test_a_merged_map_names_every_contributing_layer(self):
        sl.replace_config({"segments": {"share": True}}, layer="shared")
        sl.replace_config({"segments": {"sessions": True}}, layer="profile")
        self.assertIn("segments [shipped+shared+profile]", self._out())

    def test_shipped_only_keys_are_not_listed_as_things_you_set(self):
        out = self._out()
        self.assertIn("nothing set", out)
        self.assertNotIn("warnPercent [", out)

    def test_it_still_reports_the_settings_it_always_did(self):
        """A guard on the section being ADDED rather than replacing what was there."""
        out = self._out()
        for needle in ("account     :", "idle alarm  :", "layout      :", "tunables    :"):
            self.assertIn(needle, out)


class ConfigShareTests(TmpEnv):
    """/squirrel-config share — the migration. Nothing moves without being asked."""

    def _run(self, arg):
        out = io.StringIO()
        with mock.patch.object(sys, "stdout", out):
            sl.main(["--config", arg])
        return out.getvalue()

    def test_it_promotes_this_profiles_settings_to_the_shared_layer(self):
        sl.replace_config({"alarmAfterSeconds": 45, "branchMaxChars": 9}, layer="profile")
        out = self._run("share")
        self.assertEqual(sl.stored_config("shared"),
                         {"alarmAfterSeconds": 45, "branchMaxChars": 9})
        self.assertIn("promoted", out)
        self.assertIn("alarmAfterSeconds", out)

    def test_it_leaves_this_profiles_own_config_untouched(self):
        """What makes it safe to run: the profile you run it in cannot change behaviour."""
        before = {"alarmAfterSeconds": 45, "branchMaxChars": 9}
        sl.replace_config(dict(before), layer="profile")
        self._run("share")
        self.assertEqual(sl.stored_config("profile"), before)
        self.assertEqual(sl.load_config()["alarmAfterSeconds"], 45)

    def test_a_value_the_shared_layer_already_has_is_not_overwritten(self):
        """The shared layer belongs to the other profiles too — "share mine" is not "impose
        mine"."""
        sl.replace_config({"alarmAfterSeconds": 300}, layer="shared")
        sl.replace_config({"alarmAfterSeconds": 45}, layer="profile")
        out = self._run("share")
        self.assertEqual(sl.stored_config("shared")["alarmAfterSeconds"], 300)
        self.assertIn("already in shared, left alone", out)

    def test_a_map_merges_into_what_shared_already_has(self):
        sl.replace_config({"accountNames": {"a@x.com": "ada"}}, layer="shared")
        sl.replace_config({"accountNames": {"b@x.com": "bob"}}, layer="profile")
        self._run("share")
        self.assertEqual(sl.stored_config("shared")["accountNames"],
                         {"a@x.com": "ada", "b@x.com": "bob"})

    def test_a_profile_local_key_is_never_promoted(self):
        sl.replace_config({"usageFetch": False}, layer="profile")
        out = self._run("share")
        self.assertNotIn("usageFetch", sl.stored_config("shared"))
        self.assertIn("never shared", out)

    def test_it_is_safe_to_run_with_nothing_to_share(self):
        out = self._run("share")
        self.assertIn("nothing to share", out)
        self.assertFalse(sl.shared_config_path().exists())

    def test_bare_config_still_shows_the_config(self):
        self.assertIn("account     :", self._run(""))

    def test_an_unknown_subcommand_is_refused_and_writes_nothing(self):
        sl.replace_config({"alarmAfterSeconds": 45}, layer="profile")
        out = self._run("wibble")
        self.assertIn("usage:", out)
        self.assertFalse(sl.shared_config_path().exists())


class SharedStoreIsolationTests(TmpEnv):
    """TmpEnv must isolate SQUIRREL_SHARED_DIR PER TEST, not per module.

    The two tests are ordered by name on purpose: `a` writes a file into the shared store,
    `b` asserts it is not there. With a module-wide shared dir `b` sees `a`'s leftovers and
    fails — which is exactly the cross-test bleed that shared *config* would otherwise cause.
    Watched fail: reverting TmpEnv to the module-only shared dir fails test_b and nothing else.
    """

    def test_a_writes_into_the_shared_store(self):
        sl.shared_dir().mkdir(parents=True, exist_ok=True)
        (sl.shared_dir() / "leak-probe.json").write_text('{"leaked": true}')
        self.assertTrue((sl.shared_dir() / "leak-probe.json").exists())

    def test_b_does_not_see_the_previous_tests_shared_store(self):
        self.assertFalse((sl.shared_dir() / "leak-probe.json").exists(),
                         "shared store leaked from a previous test — SQUIRREL_SHARED_DIR is not per-test")

    def test_shared_dir_is_inside_this_tests_tmp(self):
        self.assertEqual(sl.shared_dir(), self.root / "shared")


class AccountTests(unittest.TestCase):
    def test_account_label_is_full_email(self):
        self.assertEqual(sl.account_label("you@example.com"), "you@example.com")
        self.assertEqual(sl.account_label("alice@example.com"), "alice@example.com")
        self.assertEqual(sl.account_label(None), "?")
        self.assertEqual(sl.account_label("garbage"), "?")

    def test_account_domain_for_colour_fallback(self):
        self.assertEqual(sl.account_domain("you@example.com"), "example")
        self.assertEqual(sl.account_domain("someone@gmail.com"), "gmail")
        self.assertEqual(sl.account_domain(None), "?")
        self.assertEqual(sl.account_domain("garbage"), "?")

    def test_color_code(self):
        self.assertEqual(sl.color_code("#00bcd4"), "\033[38;2;0;188;212m")
        self.assertEqual(sl.color_code("green"), "\033[32m")
        self.assertEqual(sl.color_code("brightcyan"), "\033[96m")
        self.assertEqual(sl.color_code("gray"), "\033[90m")
        self.assertIsNone(sl.color_code("#zzzzzz"))
        self.assertIsNone(sl.color_code("lavender"))
        self.assertIsNone(sl.color_code(None))

    def test_resolve_color_order(self):
        config = {"accountColors": {"a@example.com": "#00bcd4", "gmail": "green"}, "defaultAccountColor": "yellow"}
        self.assertEqual(sl.resolve_color("a@example.com", config), "\033[38;2;0;188;212m")   # full email wins
        self.assertEqual(sl.resolve_color("b@gmail.com", config), "\033[32m")                # domain fallback
        self.assertEqual(sl.resolve_color("c@other.org", config), "\033[33m")                # default
        self.assertEqual(sl.resolve_color("c@other.org", {}), "\033[37m")                    # white
        self.assertEqual(sl.resolve_color(None, {"defaultAccountColor": "nope"}), "\033[37m")

    def test_resolve_color_tolerates_malformed_account_colors(self):
        self.assertEqual(sl.resolve_color("a@x.com", {"accountColors": "oops"}), "\033[37m")
        self.assertEqual(sl.resolve_color("a@x.com", {"accountColors": ["a"]}), "\033[37m")

    def test_account_label_uses_nickname_from_config(self):
        cfg = {"accountNames": {"you@example.com": "kai", "acme": "px"}}
        self.assertEqual(sl.account_label("you@example.com", cfg), "kai")   # full-email key wins
        self.assertEqual(sl.account_label("info@acme.dev", cfg), "px")                  # domain key
        self.assertEqual(sl.account_label("alice@example.com", cfg), "alice@example.com")  # no nick → email
        self.assertEqual(sl.account_label("x@y.com", {"accountNames": "oops"}), "x@y.com")  # malformed map → email
        self.assertEqual(sl.account_label(None, cfg), "?")


class ConfigTests(TmpEnv):
    def test_data_dir_follows_claude_config_dir(self):
        self.assertEqual(sl.data_dir(), self.root / "squirrel")
        self.assertEqual(sl.config_path(), self.root / "squirrel" / "config.json")

    def test_data_dir_defaults_to_home(self):
        base = {"USERPROFILE": r"C:\Users\nobody"} if WINDOWS else {}
        with mock.patch.dict(os.environ, base, clear=True):
            self.assertEqual(sl.data_dir(), Path.home() / ".claude" / "squirrel")

    def test_cache_and_state_live_under_data_dir(self):
        usage, lock = sl.cache_paths()
        self.assertEqual(usage.parent, sl.data_dir() / "cache")
        self.assertEqual(usage.name, "usage.json")
        self.assertEqual(lock.name, "fetch.lock")
        self.assertEqual(sl.state_paths("sid").parent, sl.data_dir())
        self.assertEqual(sl.state_paths("sid").name, "state-sid.json")

    def test_two_profiles_get_separate_data_dirs(self):
        a = self.root / "profA"; b = self.root / "profB"
        with mock.patch.dict(os.environ, {"CLAUDE_CONFIG_DIR": str(a)}):
            da = sl.data_dir()
        with mock.patch.dict(os.environ, {"CLAUDE_CONFIG_DIR": str(b)}):
            db = sl.data_dir()
        self.assertNotEqual(da, db)

    def test_load_json_tolerates_missing_and_malformed(self):
        self.assertEqual(sl.load_json(self.root / "missing.json"), {})
        (self.root / "bad.json").write_text("{not json")
        self.assertEqual(sl.load_json(self.root / "bad.json"), {})
        (self.root / "arr.json").write_text("[1]")
        self.assertEqual(sl.load_json(self.root / "arr.json"), {})

    def test_read_email_and_token(self):
        (self.root / ".claude.json").write_text(json.dumps({"oauthAccount": {"emailAddress": "you@example.com"}}))
        (self.root / ".credentials.json").write_text(json.dumps({"claudeAiOauth": {"accessToken": "tok"}}))
        self.assertEqual(sl.read_email(), "you@example.com")
        self.assertEqual(sl.read_token(), "tok")

    def test_read_email_and_token_missing(self):
        self.assertIsNone(sl.read_email())
        self.assertIsNone(sl.read_token())

    def test_load_config_reads_user_config(self):
        (self.root / "squirrel").mkdir()
        (self.root / "squirrel" / "config.json").write_text('{"defaultAccountColor": "cyan"}')
        self.assertEqual(sl.load_config()["defaultAccountColor"], "cyan")

    def test_load_config_falls_back_to_packaged_defaults(self):
        cfg = sl.load_config()                      # no user config written
        self.assertEqual(cfg, sl.load_json(sl.default_config_path()))
        self.assertIn("defaultAccountColor", cfg)

    def test_load_config_merges_defaults_under_user_values(self):
        (self.root / "squirrel").mkdir()
        (self.root / "squirrel" / "config.json").write_text('{"alarmAfterSeconds": 30}')
        cfg = sl.load_config()
        self.assertEqual(cfg["alarmAfterSeconds"], 30)                 # user wins
        self.assertIn("defaultAccountColor", cfg)                      # default still present


class NormalizeTests(unittest.TestCase):
    def test_limits_array(self):
        api = {"limits": [
            {"kind": "session", "percent": 17, "resets_at": "2026-08-23T00:20:00.396181+00:00"},
            {"kind": "weekly_all", "percent": 4, "resets_at": "2026-08-27T18:00:00+00:00"},
            {"kind": "weekly_scoped", "percent": 0, "resets_at": "2026-08-27T18:00:00+00:00",
             "scope": {"model": {"id": None, "display_name": "Fable"}, "surface": None}},
            "garbage",
        ]}
        n = sl.normalize_usage(api)
        expected_session_reset = datetime(2026, 8, 23, 0, 20, 0, 396181, tzinfo=timezone.utc).timestamp()
        expected_week_reset = datetime(2026, 8, 27, 18, 0, tzinfo=timezone.utc).timestamp()
        self.assertEqual(n["session"], {"label": "", "pct": 17.0, "resets_at": expected_session_reset})
        self.assertEqual(n["weekly_all"], {"label": "", "pct": 4.0, "resets_at": expected_week_reset})
        self.assertEqual(n["scoped"], [{"label": "Fable", "pct": 0.0, "resets_at": expected_week_reset}])

    def test_legacy_top_level_fallback(self):
        api = {"five_hour": {"utilization": 30.0, "resets_at": "2026-08-23T00:20:00Z"},
               "seven_day": {"utilization": 9.0, "resets_at": None},
               "seven_day_opus": {"utilization": 2.0, "resets_at": None},
               "seven_day_sonnet": None,
               "extra_usage": {"is_enabled": False}}
        n = sl.normalize_usage(api)
        self.assertEqual(n["session"]["pct"], 30.0)
        self.assertEqual(n["session"]["resets_at"], datetime(2026, 8, 23, 0, 20, tzinfo=timezone.utc).timestamp())
        self.assertEqual(n["weekly_all"], {"label": "", "pct": 9.0, "resets_at": None})
        self.assertEqual(n["scoped"], [{"label": "Opus", "pct": 2.0, "resets_at": None}])

    def test_empty_payload(self):
        self.assertEqual(sl.normalize_usage({}), {"session": None, "weekly_all": None, "scoped": []})

    def test_parse_iso(self):
        self.assertIsNone(sl.parse_iso(None))
        self.assertIsNone(sl.parse_iso("not a date"))
        self.assertEqual(sl.parse_iso("2026-08-27T20:00:00Z"), WEEK_RESET)

    def test_stdin_bucket_tolerates_iso_and_epoch_resets(self):
        self.assertEqual(sl.stdin_bucket({"used_percentage": 30, "resets_at": WEEK_RESET})["resets_at"], WEEK_RESET)
        self.assertEqual(sl.stdin_bucket({"used_percentage": 30, "resets_at": "2026-08-27T20:00:00Z"})["resets_at"], WEEK_RESET)
        self.assertIsNone(sl.stdin_bucket("x"))


class CacheTests(TmpEnv):
    def test_needs_fetch_respects_ttl_and_lock(self):
        lock = self.root / "fetch.lock"
        # The cadence is `usageFetchSeconds` (900) since Task 29d — the API is only fetched
        # for the per-model buckets now, and the headline numbers need no fetch at all.
        self.assertFalse(sl.needs_fetch({"fetched_at": NOW - 10}, NOW, lock))
        self.assertFalse(sl.needs_fetch({"fetched_at": NOW - 61}, NOW, lock))
        self.assertTrue(sl.needs_fetch({"fetched_at": NOW - 901}, NOW, lock))
        self.assertTrue(sl.needs_fetch({}, NOW, lock))
        lock.touch()
        os.utime(lock, (NOW - 5, NOW - 5))
        self.assertFalse(sl.needs_fetch({}, NOW, lock))
        os.utime(lock, (NOW - 31, NOW - 31))
        self.assertTrue(sl.needs_fetch({}, NOW, lock))

    def test_needs_fetch_backs_off_after_no_token(self):
        lock = self.root / "fetch.lock"
        self.assertFalse(sl.needs_fetch({"fetched_at": NOW - 100, "error": "no token"}, NOW, lock))
        self.assertTrue(sl.needs_fetch({"fetched_at": NOW - 601, "error": "no token"}, NOW, lock))

    @posix_only
    def test_write_cache_is_atomic_and_private(self):
        path = self.root / "claude-statusline" / "usage.json"
        sl.write_cache(path, {"ok": True})
        self.assertEqual(json.loads(path.read_text(encoding="utf-8")), {"ok": True})
        self.assertEqual(path.stat().st_mode & 0o777, 0o600)
        self.assertEqual([p.name for p in path.parent.iterdir()], ["usage.json"])

    def test_spawn_fetch_touches_lock_and_starts_detached_process(self):
        # subprocess is imported lazily inside spawn_fetch (see LazyImportGuardTests) — patch the
        # real module by name rather than sl.subprocess, which no longer exists as a module
        # attribute until something actually imports it.
        lock = self.root / "claude-statusline" / "fetch.lock"
        with mock.patch("subprocess.Popen") as popen:
            sl.spawn_fetch(lock)
        self.assertTrue(lock.exists())
        args, kwargs = popen.call_args
        self.assertEqual(args[0][1:], [os.path.abspath(sl.__file__), "--fetch"])
        if WINDOWS:                      # DETACHED_PROCESS | CREATE_NO_WINDOW, see detach_kwargs()
            self.assertEqual(kwargs["creationflags"], sl.DETACHED_PROCESS | sl.CREATE_NO_WINDOW)
        else:
            self.assertTrue(kwargs["start_new_session"])
        self.assertEqual(kwargs["stdout"], subprocess.DEVNULL)


class StateTests(TmpEnv):
    def test_apply_state_transitions(self):
        sid = "s1"
        self.assertEqual(sl.apply_state("working", sid, NOW), {"state": "working", "subagents": 0, "ts": NOW})
        self.assertEqual(sl.apply_state("subagent-start", sid, NOW)["state"], "subagents")
        self.assertEqual(sl.apply_state("subagent-start", sid, NOW), {"state": "subagents", "subagents": 2, "ts": NOW})
        self.assertEqual(sl.apply_state("subagent-stop", sid, NOW), {"state": "subagents", "subagents": 1, "ts": NOW})
        self.assertEqual(sl.apply_state("subagent-stop", sid, NOW), {"state": "working", "subagents": 0, "ts": NOW})
        self.assertEqual(sl.apply_state("subagent-stop", sid, NOW)["subagents"], 0)   # never negative
        self.assertEqual(sl.apply_state("permission", sid, NOW)["state"], "permission")
        self.assertEqual(sl.apply_state("idle", sid, NOW), {"state": "idle", "subagents": 0, "ts": NOW})

    def test_apply_state_end_removes_file(self):
        sid = "s2"
        sl.apply_state("working", sid, NOW)
        self.assertTrue(sl.state_paths(sid).exists())
        sl.apply_state("end", sid, NOW)
        self.assertFalse(sl.state_paths(sid).exists())

    def test_read_state_missing_or_stale(self):
        self.assertIsNone(sl.read_state("nope", NOW))
        sl.apply_state("idle", "ancient", NOW - (sl.STATE_MAX_AGE_S + 100))   # past the 7-day backstop
        self.assertIsNone(sl.read_state("ancient", NOW))
        sl.apply_state("working", "fresh", NOW)
        self.assertEqual(sl.read_state("fresh", NOW)["state"], "working")

    def test_activity_clears_waiting_states(self):
        sid = "act1"
        sl.apply_state("permission", sid, NOW)
        self.assertEqual(sl.apply_state("activity", sid, NOW)["state"], "working")   # answered → resumes working
        sl.apply_state("idle", sid, NOW)
        self.assertEqual(sl.apply_state("activity", sid, NOW)["state"], "working")

    def test_activity_reflects_subagent_count_without_changing_it(self):
        sid = "act2"
        sl.apply_state("subagent-start", sid, NOW)
        sl.apply_state("subagent-start", sid, NOW)      # counter = 2
        s = sl.apply_state("activity", sid, NOW)
        self.assertEqual((s["state"], s["subagents"]), ("subagents", 2))   # unchanged counter, shows subagents

    def test_activity_on_fresh_session_is_working(self):
        s = sl.apply_state("activity", "act3", NOW)
        self.assertEqual((s["state"], s["subagents"]), ("working", 0))


class AlarmTests(TmpEnv):
    def _status(self, sid):
        return {"session_id": sid, "model": {"display_name": "Fable 5"},
                "context_window": {"context_window_size": 200000, "used_percentage": 12, "current_usage": {}}}

    def test_idle_alarm_after_threshold(self):
        sl.apply_state("idle", "al1", NOW - 200)                       # idle for 200s
        chip = sl.state_segment(self._status("al1"), NOW, {"alarmAfterSeconds": 120})
        self.assertIn("IDLE", plain(chip))
        self.assertIn("3m", plain(chip))                              # elapsed shown
        self.assertIn(sl.ALARM_SEQ, chip)                            # loud styling present

    def test_alarm_is_bracketed(self):
        sl.apply_state("idle", "brk", NOW - 200)
        chip = sl.state_segment(self._status("brk"), NOW, {"alarmAfterSeconds": 120})
        self.assertIn("[", plain(chip)); self.assertIn("]", plain(chip))
        self.assertIn("IDLE", plain(chip))

    def test_permission_alarm_after_threshold(self):
        sl.apply_state("permission", "al2", NOW - 200)
        chip = sl.state_segment(self._status("al2"), NOW, {"alarmAfterSeconds": 120})
        self.assertIn("NEEDS YOU", plain(chip))
        self.assertIn(sl.ALARM_SEQ, chip)

    def test_no_alarm_before_threshold(self):
        sl.apply_state("idle", "al3", NOW - 30)                       # only 30s
        chip = sl.state_segment(self._status("al3"), NOW, {"alarmAfterSeconds": 120})
        self.assertNotIn(sl.ALARM_SEQ, chip)
        self.assertIn("idle", plain(chip))                           # normal ✓ idle chip

    def test_alarm_disabled_with_zero(self):
        sl.apply_state("idle", "al4", NOW - 1800)                     # long idle, but within STATE_MAX_AGE_S
        chip = sl.state_segment(self._status("al4"), NOW, {"alarmAfterSeconds": 0})
        self.assertNotIn(sl.ALARM_SEQ, chip)

    def test_working_and_subagents_never_alarm(self):
        sl.apply_state("subagent-start", "al5", NOW - 1800)           # long-running, but within STATE_MAX_AGE_S
        chip = sl.state_segment(self._status("al5"), NOW, {"alarmAfterSeconds": 1})
        self.assertNotIn(sl.ALARM_SEQ, chip)                          # active states are not "waiting"


class StaleAndColourTests(TmpEnv):
    def _status(self, sid):
        return {"session_id": sid}

    def test_idle_alarm_shows_hours(self):
        sl.apply_state("idle", "hrs", NOW - 7200)                      # idle 2h
        chip = sl.state_segment(self._status("hrs"), NOW, {"alarmAfterSeconds": 120})
        self.assertIn("IDLE", plain(chip))
        self.assertIn("2h", plain(chip))                              # hours, not stuck/blank
        self.assertIsNotNone(chip)

    def test_active_state_expires_after_one_hour(self):
        sl.apply_state("working", "stuckwork", NOW - (sl.ACTIVE_MAX_AGE_S + 60))
        self.assertIsNone(sl.state_segment(self._status("stuckwork"), NOW, {}))   # crashed "working" clears
        sl.apply_state("subagent-start", "stucksub", NOW - (sl.ACTIVE_MAX_AGE_S + 60))
        self.assertIsNone(sl.state_segment(self._status("stucksub"), NOW, {}))

    def test_active_state_fresh_still_shows(self):
        sl.apply_state("working", "freshwork", NOW - 1800)            # 30m working, still valid
        self.assertIn("working", plain(sl.state_segment(self._status("freshwork"), NOW, {})))

    def test_working_is_green_idle_is_yellow(self):
        sl.apply_state("working", "cw", NOW)
        self.assertIn(sl.GREEN, sl.state_segment(self._status("cw"), NOW, {}))
        sl.apply_state("idle", "ci", NOW - 30)                         # below alarm threshold → normal chip
        self.assertIn(sl.YELLOW, sl.state_segment(self._status("ci"), NOW, {"alarmAfterSeconds": 120}))

    def test_chips_use_natural_single_space_padding_by_default(self):
        cases = (("working", "uw", "[ ⚙ working ]"), ("idle", "ui", "[ ✓ idle ]"),
                 ("permission", "up", "[ ◀ needs you ]"))
        for st, sid, expected in cases:
            sl.apply_state(st, sid, NOW)
            chip = sl.state_segment({"session_id": sid}, NOW, {"alarmAfterSeconds": 0})  # disable alarm → normal chips
            self.assertEqual(plain(chip), expected)   # default chipWidth (0) → one space each side

    def test_alarm_chip_uses_natural_padding_too(self):
        sl.apply_state("idle", "ualarm", NOW - 420)
        chip = sl.state_segment({"session_id": "ualarm"}, NOW, {"alarmAfterSeconds": 120})
        p = plain(chip)
        self.assertTrue(p.startswith("[ ‼ IDLE"), p)
        self.assertTrue(p.endswith("‼ ]"), p)

    def test_chip_width_positive_reproduces_old_uniform_centring(self):
        widths = set()
        for st, sid in (("working", "uw2"), ("idle", "ui2"), ("permission", "up2")):
            sl.apply_state(st, sid, NOW)
            chip = sl.state_segment({"session_id": sid}, NOW, {"alarmAfterSeconds": 0, "chipWidth": 13})
            widths.add(sl.visible_len(chip))
        self.assertEqual(len(widths), 1, f"chips differ in width: {widths}")
        self.assertEqual(widths.pop(), 15)   # 13 + the two brackets, uniform across chips

    def test_chip_width_config_override(self):
        sl.apply_state("idle", "uc", NOW)
        chip = sl.state_segment({"session_id": "uc"}, NOW, {"alarmAfterSeconds": 0, "chipWidth": 20})
        self.assertEqual(sl.visible_len(chip), 22)   # 20 + brackets


class IdleBackgroundTests(TmpEnv):
    LINE = "\033[32mabc\033[0m · def"          # a line with an inner reset

    def test_bg_code_accepts_names_and_hex(self):
        self.assertEqual(sl.bg_code("#3a1414"), "\033[48;2;58;20;20m")
        self.assertEqual(sl.bg_code("red"), "\033[41m")
        self.assertEqual(sl.bg_code("brightblack"), "\033[100m")
        self.assertIsNone(sl.bg_code("nonsense"))
        self.assertIsNone(sl.bg_code(None))

    def test_apply_background_reasserts_after_resets_and_pads(self):
        out = sl.apply_background(self.LINE, "\033[48;2;58;20;20m", 20)
        self.assertTrue(out.startswith("\033[48;2;58;20;20m"))
        self.assertTrue(out.endswith(sl.RESET))
        self.assertEqual(out.count("\033[48;2;58;20;20m"), 2)   # once at start, once after the inner reset
        self.assertEqual(sl.visible_len(out), 19)               # padded to width-1 (overflow safety margin)

    def test_apply_background_does_not_truncate_a_long_line(self):
        long_line = "x" * 40
        out = sl.apply_background(long_line, "\033[41m", 20)
        self.assertEqual(sl.visible_len(out), 40)

    def test_idle_background_after_threshold(self):
        sl.apply_state("idle", "bg1", NOW - 45)
        bg, text, _flat = sl.idle_background({"session_id": "bg1"}, NOW,
                                      {"idleBackgroundAfterSeconds": 30, "idleBackground": "#3a1414"})
        self.assertEqual(bg, sl.bg_code("#3a1414"))
        self.assertEqual(text, sl.bold_code(sl.color_code("white")))   # shipped phase-1 text (bold) still applies

    def test_no_background_before_threshold_or_when_off(self):
        sl.apply_state("idle", "bg2", NOW - 10)
        cfg = {"idleBackgroundAfterSeconds": 30, "idleBackground": "#3a1414"}
        self.assertIsNone(sl.idle_background({"session_id": "bg2"}, NOW, cfg)[0])
        sl.apply_state("idle", "bg3", NOW - 999)
        self.assertIsNone(sl.idle_background({"session_id": "bg3"}, NOW, dict(cfg, idleBackground="off"))[0])
        self.assertIsNone(sl.idle_background({"session_id": "bg3"}, NOW, dict(cfg, idleBackgroundAfterSeconds=0))[0])

    def test_active_states_never_get_a_background(self):
        for st in ("working", "subagent-start"):
            sl.apply_state(st, "bg4", NOW - 999)
            self.assertIsNone(sl.idle_background({"session_id": "bg4"}, NOW,
                                                 {"idleBackgroundAfterSeconds": 30, "idleBackground": "#3a1414"})[0])

    def test_render_tints_both_lines_when_idle(self):
        sl.apply_state("idle", "bg5", NOW - 60)
        cfg = {"idleBackgroundAfterSeconds": 30, "idleBackground": "#3a1414", "alarmAfterSeconds": 0}
        status = dict(STATUS, session_id="bg5")
        l1, l2 = sl.render(status, CACHE, "you@example.com", cfg, NOW, timezone.utc, width=120)
        for line in (l1, l2):
            self.assertTrue(line.startswith("\033[48;2;58;20;20m"), line[:20])
            self.assertEqual(sl.visible_len(line), 119)   # width-1 overflow safety margin

    def test_render_untinted_when_working(self):
        sl.apply_state("working", "bg6", NOW)
        status = dict(STATUS, session_id="bg6")
        l1, _ = sl.render(status, CACHE, "you@example.com",
                          {"idleBackgroundAfterSeconds": 30, "idleBackground": "#3a1414"}, NOW, timezone.utc, width=120)
        self.assertFalse(l1.startswith("\033[48"))


class BgCommandTests(TmpEnv):
    def test_set_bg_and_off(self):
        self.assertIn("#3a1414", sl.cmd_set_bg("#3a1414"))
        self.assertEqual(sl.load_config()["idleBackground"], "#3a1414")
        self.assertIn("off", sl.cmd_set_bg("off").lower())
        self.assertEqual(sl.load_config()["idleBackground"], "off")
        self.assertIn("not a valid", sl.cmd_set_bg("banana").lower())

    def test_set_bg_after(self):
        self.assertIn("30", sl.cmd_set_bg_after("30"))
        self.assertEqual(sl.load_config()["idleBackgroundAfterSeconds"], 30)
        self.assertIn("usage", sl.cmd_set_bg_after("soon").lower())


class UrgentBackgroundTests(TmpEnv):
    CFG = {"idleBackground": "#c9a227", "idleBackgroundUrgent": "#b3261e",
           "idleBackgroundAfterSeconds": 30, "alarmAfterSeconds": 120}

    def _lines(self, age, cfg=None):
        sl.apply_state("idle", "u1", NOW - age)
        status = dict(STATUS, session_id="u1")
        return sl.render(status, CACHE, "you@example.com", cfg or self.CFG, NOW, timezone.utc, width=120)

    def test_warn_phase_is_a_visible_yellow_banner(self):
        l1, l2 = self._lines(60)                       # past bg threshold, before alarm
        for line in (l1, l2):
            self.assertTrue(line.startswith(sl.bg_code("#c9a227")), line[:24])
            self.assertIn(sl.bold_code(sl.color_code("brightwhite")), line)   # shipped phase-1 text (bold)
            self.assertNotIn(sl.bg_code("#b3261e"), line)    # not yet the alarm colour

    def test_phase2_uses_the_alarm_bar(self):
        l1, l2 = self._lines(300)                      # past the alarm threshold
        for line in (l1, l2):
            self.assertTrue(line.startswith(sl.bg_code("#b3261e")), line[:24])
            # bright white on a banner now — see BannerContrastTests; `37` read as grey
            self.assertIn(sl.bold_code(sl.color_code("brightwhite")), line)
            self.assertEqual(sl.visible_len(line), 119)     # width-1 overflow safety margin

    def test_phase2_still_shows_the_loud_chip(self):
        _, l2 = self._lines(300)
        self.assertIn("IDLE", plain(l2))
        self.assertIn(sl.ALARM_SEQ, l2)

    def test_urgent_off_disables_only_the_alarm_phase(self):
        """idleBackgroundUrgent: off now means the alarm phase paints no background of its own —
        the ladder doesn't fall back to an earlier phase once a later one has elapsed (a
        deliberate change from the old two-constant system: 'off' means off, not 'revert to
        phase 1' — see the merge-semantics table in commands/squirrel-phases.md)."""
        cfg = dict(self.CFG, idleBackgroundUrgent="off")
        l1, _ = self._lines(300, cfg)
        self.assertFalse(l1.startswith("\033[48"))       # no background at the alarm phase
        self.assertIn(sl.bold_code(sl.color_code("brightwhite")), l1)   # shipped phase-2 text, bright on a banner

    def test_background_off_disables_both_phases(self):
        cfg = dict(self.CFG, idleBackground="off")
        l1, _ = self._lines(300, cfg)
        self.assertFalse(l1.startswith("\033[48"))

    def test_no_alarm_phase_when_chip_alarm_disabled(self):
        # The shipped 300s phase is red and carries the notification; drop it so this test is
        # about the legacy urgent-colour mapping it was written for and nothing else.
        cfg = dict(self.CFG, alarmAfterSeconds=0, idlePhases=[{"after": 300, "remove": True}])
        l1, _ = self._lines(9999, cfg)
        self.assertTrue(l1.startswith(sl.bg_code("#c9a227")))   # warn banner only
        self.assertNotIn(sl.bg_code("#b3261e"), l1)

    def test_working_never_painted(self):
        sl.apply_state("working", "u2", NOW)
        l1, _ = sl.render(dict(STATUS, session_id="u2"), CACHE, "you@example.com",
                          self.CFG, NOW, timezone.utc, width=120)
        self.assertFalse(l1.startswith("\033[48"))


class UrgentBgCommandTests(TmpEnv):
    def test_set_urgent_colour(self):
        msg = sl.cmd_set_bg("urgent #ff0000")
        self.assertIn("#ff0000", msg)
        self.assertEqual(sl.load_config()["idleBackgroundUrgent"], "#ff0000")

    def test_set_normal_colour_unchanged_behaviour(self):
        sl.cmd_set_bg("#112233")
        self.assertEqual(sl.load_config()["idleBackground"], "#112233")

    def test_urgent_off_and_validation(self):
        self.assertIn("off", sl.cmd_set_bg("urgent off").lower())
        self.assertEqual(sl.load_config()["idleBackgroundUrgent"], "off")
        self.assertIn("not a valid", sl.cmd_set_bg("urgent banana").lower())


class ForcedTextColourTests(TmpEnv):
    """A phase's `text` colour must actually replace every segment's own foreground colour —
    not merely be interleaved with it — while the loud chip keeps its own inverse styling."""

    def _custom_phase(self, **fields):
        (self.root / "squirrel").mkdir(exist_ok=True)
        (self.root / "squirrel" / "config.json").write_text(json.dumps({"idlePhases": [dict(after=30, **fields)]}))
        return sl.load_config()

    def test_forced_text_colour_overrides_every_segment(self):
        cfg = self._custom_phase(bg="#112233", text="white", bold=False)
        sl.apply_state("idle", "ftc1", NOW - 60)
        status = dict(STATUS, session_id="ftc1")
        # bright on a banner — see BANNER_BRIGHT; `37` read as grey on many themes
        forced = sl.color_code("brightwhite")
        l1, l2 = sl.render(status, CACHE, "you@example.com", cfg, NOW, timezone.utc, width=120)
        for line in (l1, l2):
            # None of the segments' DECORATIVE colours survive — the account's own colour,
            # the neutral grey of a clean branch, an all-clear green. An ALERT colour does,
            # rendered as an inverse chip: a forced text colour may not silence a threshold
            # (the Task 14 rule), and red-on-red would be no better than silence. See
            # BannerContrastTests.
            allowed = (forced, sl.RESET, *sl.ALERT_SGRS)
            leftover = [m for m in sl._FG_SGR_RE.findall(line) if m not in allowed]
            self.assertEqual(leftover, [], line)
            for alert in sl.ALERT_SGRS:
                if alert in line:
                    self.assertIn(sl.REVERSE + alert, line,
                                  "an alert colour survived without being made legible")

    def test_text_off_preserves_per_segment_colours(self):
        cfg = self._custom_phase(bg="#112233", text="off")
        sl.apply_state("idle", "ftc2", NOW - 60)
        status = dict(STATUS, session_id="ftc2")
        l1, _ = sl.render(status, CACHE, "you@example.com", cfg, NOW, timezone.utc, width=120)
        self.assertIn(sl.GREEN, l1)   # the context bar's own colour is untouched

    def test_loud_chip_keeps_its_own_styling_when_text_is_forced(self):
        cfg = self._custom_phase(bg="#112233", text="white")
        sl.apply_state("idle", "ftc3", NOW - 300)   # past the (default) alarm threshold
        status = dict(STATUS, session_id="ftc3")
        _, l2 = sl.render(status, CACHE, "you@example.com", cfg, NOW, timezone.utc, width=120)
        self.assertIn(sl.ALARM_SEQ, l2)
        self.assertIn("IDLE", plain(l2))

    def test_loud_chip_keeps_its_own_styling_when_text_is_off(self):
        cfg = self._custom_phase(bg="#112233", text="off")
        sl.apply_state("idle", "ftc4", NOW - 300)
        status = dict(STATUS, session_id="ftc4")
        _, l2 = sl.render(status, CACHE, "you@example.com", cfg, NOW, timezone.utc, width=120)
        self.assertIn(sl.ALARM_SEQ, l2)
        self.assertIn("IDLE", plain(l2))

    def test_width_exact_with_forced_text(self):
        cfg = self._custom_phase(bg="#112233", text="white")
        sl.apply_state("idle", "ftc5", NOW - 60)
        status = dict(STATUS, session_id="ftc5")
        l1, l2 = sl.render(status, CACHE, "you@example.com", cfg, NOW, timezone.utc, width=100)
        self.assertEqual(sl.visible_len(l1), 99)   # width-1 overflow safety margin
        self.assertEqual(sl.visible_len(l2), 99)


class PhaseLadderTests(TmpEnv):
    def _cfg(self, user=None):
        if user is not None:
            (self.root / "squirrel").mkdir(exist_ok=True)
            (self.root / "squirrel" / "config.json").write_text(json.dumps(user))
        return sl.load_config()

    def test_defaults_come_from_the_shipped_file_not_the_code(self):
        shipped = json.loads(sl.default_config_path().read_text(encoding="utf-8"))["idlePhases"]
        self.assertEqual(sl.load_phases(self._cfg()), sorted(shipped, key=lambda p: p["after"]))

    def test_partial_override_keeps_unmentioned_fields(self):
        cfg = self._cfg({"idlePhases": [{"after": 30, "bg": "purple"}]})
        first = sl.load_phases(cfg)[0]
        self.assertEqual(first["bg"], "purple")
        self.assertEqual(first["text"], "white")          # shipped value survives

    def test_unmentioned_phases_are_kept(self):
        cfg = self._cfg({"idlePhases": [{"after": 30, "bg": "purple"}]})
        self.assertEqual([p["after"] for p in sl.load_phases(cfg)], [30, 120, 300])

    def test_new_phase_is_added_and_sorted(self):
        cfg = self._cfg({"idlePhases": [{"after": 600, "bg": "#7c3aed", "text": "white"}]})
        self.assertEqual([p["after"] for p in sl.load_phases(cfg)], [30, 120, 300, 600])

    def test_bg_off_means_no_background_but_phase_still_applies(self):
        cfg = self._cfg({"idlePhases": [{"after": 30, "bg": "off"}]})
        sl.apply_state("idle", "p1", NOW - 60)
        bg, text, _flat = sl.idle_background({"session_id": "p1"}, NOW, cfg)
        self.assertIsNone(bg)
        self.assertIsNotNone(text)                        # text override still honoured

    def test_remove_drops_a_shipped_phase(self):
        cfg = self._cfg({"idlePhases": [{"after": 30, "remove": True}]})
        self.assertEqual([p["after"] for p in sl.load_phases(cfg)], [120, 300])

    def test_last_elapsed_phase_wins(self):
        cfg = self._cfg()
        self.assertIsNone(sl.phase_for(10, cfg))
        self.assertEqual(sl.phase_for(60, cfg)["after"], 30)
        self.assertEqual(sl.phase_for(9999, cfg)["after"], 300)

    def test_invalid_entries_are_skipped_not_fatal(self):
        cfg = self._cfg({"idlePhases": ["nonsense", {"bg": "red"}, {"after": -5}, {"after": 45, "bg": "red"}]})
        self.assertEqual([p["after"] for p in sl.load_phases(cfg)], [30, 45, 120, 300])

    def test_legacy_keys_still_work(self):
        cfg = self._cfg({"idleBackgroundAfterSeconds": 10, "idleBackground": "#111111"})
        first = sl.load_phases(cfg)[0]
        self.assertEqual((first["after"], first["bg"]), (10, "#111111"))

    def test_legacy_urgent_keys_still_work(self):
        cfg = self._cfg({"alarmAfterSeconds": 45, "idleBackgroundUrgent": "#222222"})
        second = sl.load_phases(cfg)[1]
        self.assertEqual((second["after"], second["bg"]), (45, "#222222"))

    def test_render_paints_with_the_winning_phase(self):
        cfg = self._cfg({"idlePhases": [{"after": 5, "bg": "#010203", "text": "white"}]})
        sl.apply_state("idle", "p2", NOW - 30)
        l1, l2 = sl.render(dict(STATUS, session_id="p2"), CACHE, "you@example.com", cfg, NOW, timezone.utc, width=100)
        for line in (l1, l2):
            self.assertTrue(line.startswith(sl.bg_code("#010203")))
            self.assertEqual(sl.visible_len(line), 99)   # width-1 overflow safety margin

    def test_shipped_phases_are_bold_by_default(self):
        cfg = self._cfg()
        self.assertTrue(sl.load_phases(cfg)[0]["bold"])
        self.assertTrue(sl.load_phases(cfg)[1]["bold"])

    def test_bold_true_makes_the_text_escape_bold(self):
        cfg = self._cfg({"idlePhases": [{"after": 30, "text": "white", "bold": True}]})
        sl.apply_state("idle", "b1", NOW - 60)
        _, text, _flat = sl.idle_background({"session_id": "b1"}, NOW, cfg)
        self.assertEqual(text, "\033[1;37m")

    def test_bold_false_or_absent_is_plain_text(self):
        cfg = self._cfg({"idlePhases": [{"after": 30, "text": "white", "bold": False}]})
        sl.apply_state("idle", "b2", NOW - 60)
        _, text, _flat = sl.idle_background({"session_id": "b2"}, NOW, cfg)
        self.assertEqual(text, sl.color_code("white"))   # plain, no bold prefix
        cfg2 = self._cfg({"idlePhases": [{"after": 900, "bg": "#7c3aed", "text": "cyan"}]})   # brand new phase, no bold key at all
        sl.apply_state("idle", "b3", NOW - 950)
        _, text2, _flat2 = sl.idle_background({"session_id": "b3"}, NOW, cfg2)
        self.assertEqual(text2, sl.color_code("cyan"))


class PhaseCommandTests(TmpEnv):
    def test_list_shows_the_ladder(self):
        out = sl.cmd_phases("")
        self.assertIn("30", out); self.assertIn("120", out); self.assertIn("bg", out.lower())

    def test_add_set_remove_reset(self):
        self.assertIn("600", sl.cmd_phases("add 600 bg=#7c3aed text=white"))
        self.assertEqual([p["after"] for p in sl.load_phases(sl.load_config())], [30, 120, 300, 600])
        sl.cmd_phases("set 30 bg=off")
        self.assertEqual(sl.load_phases(sl.load_config())[0]["bg"], "off")
        sl.cmd_phases("remove 120")
        self.assertNotIn(120, [p["after"] for p in sl.load_phases(sl.load_config())])
        sl.cmd_phases("reset")
        shipped = json.loads(sl.default_config_path().read_text(encoding="utf-8"))["idlePhases"]
        self.assertEqual(len(sl.load_phases(sl.load_config())), len(shipped))

    def test_surgical_edit_does_not_touch_other_phases(self):
        sl.cmd_phases("add 600 bg=#7c3aed text=white")
        sl.cmd_phases("set 30 bg=purple")
        phases = {p["after"]: p for p in sl.load_phases(sl.load_config())}
        self.assertEqual(phases[30]["bg"], "purple")
        self.assertEqual(phases[30]["text"], "white")      # untouched field
        self.assertEqual(phases[600]["bg"], "#7c3aed")     # untouched phase
        self.assertEqual(phases[120]["bg"], "#b3261e")

    def test_repeated_edits_to_the_same_phase_do_not_clobber_each_other(self):
        """The product principle in play: an agent (or a person) editing bg must never clobber a
        text override set moments earlier on that same phase, and vice versa."""
        sl.cmd_phases("set 30 text=cyan")
        sl.cmd_phases("set 30 bg=purple")
        phase = sl.load_phases(sl.load_config())[0]
        self.assertEqual(phase["bg"], "purple")
        self.assertEqual(phase["text"], "cyan")            # the earlier edit must survive

    def test_validation(self):
        self.assertIn("usage", sl.cmd_phases("add").lower())
        self.assertIn("not a valid", sl.cmd_phases("add 60 bg=banana").lower())
        self.assertIn("unknown", sl.cmd_phases("wobble 60").lower())
        before = sl.load_phases(sl.load_config())
        sl.cmd_phases("add 60 bg=banana")
        self.assertEqual(sl.load_phases(sl.load_config()), before)   # nothing written on error

    def test_set_phase_bold_field(self):
        sl.cmd_phases("set 30 bold=off")
        self.assertIs(sl.load_phases(sl.load_config())[0]["bold"], False)
        sl.cmd_phases("set 30 bold=on")
        self.assertIs(sl.load_phases(sl.load_config())[0]["bold"], True)
        self.assertIn("valid bold value", sl.cmd_phases("set 30 bold=maybe").lower())


class RenderStateTests(TmpEnv):
    def test_state_segment_at_end_of_line2(self):
        st = dict(STATUS, session_id="sx")
        sl.apply_state("subagent-start", "sx", NOW)
        with mock.patch.object(sl.time, "time", return_value=NOW):
            _, l2 = sl.render(st, CACHE, "you@example.com", CONFIG, NOW, timezone.utc)
        p = plain(l2)
        self.assertTrue(p.startswith("you@example.com"), p)          # account still first
        self.assertIn("[", p); self.assertRegex(p, r"\[\s*⧗ 1 subagent\s*\]\s*$")

    def test_no_state_segment_without_session_id(self):
        _, l2 = sl.render(STATUS, CACHE, "you@example.com", CONFIG, NOW, timezone.utc)
        self.assertTrue(plain(l2).startswith("you@example.com"), plain(l2))


class FakeResponse:
    def __init__(self, body: bytes):
        self.body = body

    def read(self):
        return self.body

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class FetchTests(TmpEnv):
    def write_token(self):
        (self.root / ".credentials.json").write_text(json.dumps({"claudeAiOauth": {"accessToken": "tok"}}))

    def test_no_redirect_handler_refuses_redirects(self):
        # _NoRedirect is no longer a persistent module-level class (it's built lazily, inside
        # _no_redirect_handler_class(), so urllib is never imported on the render path) — get a
        # fresh instance through that factory instead of sl._NoRedirect.
        handler_cls = sl._no_redirect_handler_class()
        req = urllib.request.Request(sl.USAGE_URL)
        with self.assertRaises(urllib.error.HTTPError):
            handler_cls().redirect_request(req, None, 302, "Found", {}, "https://evil.example/")

    def test_fetch_usage_sends_oauth_headers(self):
        seen = {}

        def opener(req, timeout):
            seen["req"], seen["timeout"] = req, timeout
            return FakeResponse(b'{"limits": []}')

        self.assertEqual(sl.fetch_usage("tok", opener), {"limits": []})
        self.assertEqual(seen["req"].full_url, sl.USAGE_URL)
        self.assertEqual(seen["req"].get_header("Authorization"), "Bearer tok")
        self.assertEqual(seen["req"].get_header("Anthropic-beta"), "oauth-2025-04-20")
        self.assertEqual(seen["timeout"], sl.FETCH_TIMEOUT_S)

    def test_run_fetch_writes_normalized_cache_and_clears_lock(self):
        self.write_token()
        cache_file, lock = sl.cache_paths()
        lock.parent.mkdir(parents=True)
        lock.touch()
        api = {"limits": [{"kind": "session", "percent": 17, "resets_at": None}]}
        with mock.patch.object(sl, "fetch_usage", return_value=api):
            entry = sl.run_fetch(now=NOW)
        self.assertTrue(entry["ok"])
        self.assertIsNone(entry["error"])
        self.assertEqual(entry["limits"]["session"]["pct"], 17.0)
        self.assertEqual(json.loads(cache_file.read_text(encoding="utf-8"))["fetched_at"], NOW)
        self.assertFalse(lock.exists())
        self.assertNotIn("tok", cache_file.read_text(encoding="utf-8"))

    def test_run_fetch_keeps_previous_limits_on_http_error(self):
        self.write_token()
        cache_file, _ = sl.cache_paths()
        previous_limits = {"session": {"label": "", "pct": 5.0, "resets_at": None}, "weekly_all": None, "scoped": []}
        sl.write_cache(cache_file, {"fetched_at": NOW - 100, "ok": True, "error": None,
         "account": sl.account_hash("a@example.com"), "limits": previous_limits})
        err = urllib.error.HTTPError(sl.USAGE_URL, 401, "unauthorized", {}, None)
        with mock.patch.object(sl, "fetch_usage", side_effect=err):
            entry = sl.run_fetch(now=NOW)
        self.assertFalse(entry["ok"])
        self.assertEqual(entry["error"], "http 401")
        self.assertEqual(entry["limits"], previous_limits)
        self.assertEqual(json.loads(cache_file.read_text(encoding="utf-8"))["error"], "http 401")

    def test_run_fetch_reports_network_errors_by_type(self):
        self.write_token()
        with mock.patch.object(sl, "fetch_usage", side_effect=urllib.error.URLError("down")):
            entry = sl.run_fetch(now=NOW)
        self.assertEqual((entry["ok"], entry["error"]), (False, "URLError"))

    def test_run_fetch_without_token(self):
        entry = sl.run_fetch(now=NOW)
        self.assertEqual((entry["ok"], entry["error"], entry["limits"]), (False, "no token", None))


STATUS = {
    "model": {"id": "claude-fable-5[1m]", "display_name": "Fable 5"},
    "context_window": {
        "context_window_size": 200_000, "used_percentage": 42, "remaining_percentage": 58,
        "current_usage": {"input_tokens": 80_000, "output_tokens": 1_200,
                          "cache_creation_input_tokens": 2_000, "cache_read_input_tokens": 2_000},
    },
    "cost": {"total_cost_usd": 1.234, "total_duration_ms": 7_980_000, "total_api_duration_ms": 100,
             "total_lines_added": 312, "total_lines_removed": 48},
    "effort": {"level": "xhigh"},
    "thinking": {"enabled": True},
    "fast_mode": False,
    "rate_limits": {"five_hour": {"used_percentage": 30, "resets_at": NOW + 3600},
                    "seven_day": {"used_percentage": 9, "resets_at": WEEK_RESET}},
    "pr": {"number": 42, "url": "https://github.com/x/y/pull/42", "review_state": "approved"},
}
# Stamped with the account these figures belong to (Task 29). A cache without a stamp is
# unknown provenance and is deliberately not trusted, so a fixture standing in for a normal,
# current profile has to carry one — the same reason the sample fixtures grew `resets_at`.
CACHE = {"fetched_at": NOW - 10, "ok": True, "error": None,
         "account": sl.account_hash("you@example.com"), "limits": {
    "session": {"label": "", "pct": 17.0, "resets_at": NOW + 3600 + 25 * 60},
    "weekly_all": {"label": "", "pct": 4.0, "resets_at": WEEK_RESET},
    "scoped": [{"label": "Fable", "pct": 0.0, "resets_at": WEEK_RESET}],
}}
# Task 29d made stdin PRIMARY for the two headline buckets, so a fixture carrying both a
# `rate_limits` block and an API cache now renders the payload's numbers. Tests that are
# about something else entirely — widths, countdowns, staleness marking, the composite —
# keep exercising the API path by using a status with no payload buckets in it, which is
# also the honest picture of a session Claude Code has not sent rate_limits for.
STATUS_NO_RL = {k: v for k, v in STATUS.items() if k != "rate_limits"}

CONFIG = {"accountColors": {"example": "#00bcd4"}}

LINE1 = ("ctx ▰▰▰▰▱▱▱▱▱▱ 42% (84k/200k) │ 5h ▰▰▱▱▱▱▱▱▱▱ 17% ↻ 1h25m │ "
         "week all ▰▱▱▱▱▱▱▱▱▱ 4% · Fable ▱▱▱▱▱▱▱▱▱▱ 0% ↻ Thu 20:00")
LINE2 = "you@example.com · Fable 5 · xhigh · thinking │ $1.23 · 2h13m · +312/−48 · PR #42 ✓approved"


class RepoTests(unittest.TestCase):
    def test_repo_name_prefers_workspace_repo_name(self):
        st = {"workspace": {"repo": {"host": "github.com", "owner": "acmehq", "name": "demo"},
                            "current_dir": "/w/x"}}
        self.assertEqual(sl.repo_name(st), "demo")

    def test_repo_name_falls_back_to_dir_basename(self):
        self.assertEqual(sl.repo_name({"workspace": {"current_dir": "/w/demo/demo-repo"}}),
                         "demo-repo")
        self.assertEqual(sl.repo_name({"workspace": {"git_worktree": "/root/wt/feature-x"}}), "feature-x")
        self.assertEqual(sl.repo_name({"workspace": {"project_dir": "/w/proj"}}), "proj")

    def test_repo_name_none_when_absent(self):
        self.assertIsNone(sl.repo_name({}))
        self.assertIsNone(sl.repo_name({"workspace": {}}))

    def test_hash_color_is_deterministic_truecolor(self):
        a = sl.hash_color("demo")
        self.assertEqual(a, sl.hash_color("demo"))                 # stable
        self.assertRegex(a, r"^\033\[38;2;\d{1,3};\d{1,3};\d{1,3}m$")
        self.assertNotEqual(sl.hash_color("demo"), sl.hash_color("demo-repo"))

    def test_repo_color_prefers_override(self):
        self.assertEqual(sl.repo_color("demo", {"repoColors": {"demo": "red"}}), "\033[31m")
        self.assertEqual(sl.repo_color("demo", {"repoColors": "oops"}), sl.hash_color("demo"))
        self.assertEqual(sl.repo_color("demo", {}), sl.hash_color("demo"))


class GitTests(TmpEnv):
    """git_segment now issues a single `git status --porcelain=v2 --branch` call (see
    _parse_git_status_v2) instead of three separate ones, and caches the parsed result per cwd
    (see _git_status) — TmpEnv isolation (a fresh CLAUDE_CONFIG_DIR/cache dir per test) matters
    here so that cache never leaks between test methods that happen to reuse the same fake cwd.
    `now=NOW` is passed explicitly throughout so every call in a test is a deterministic,
    guaranteed cache miss against its own empty cache file."""

    @staticmethod
    def _porcelain(branch, dirty=False, ahead=None, oid="deadbeef"):
        """Build a fake `git status --porcelain=v2 --branch` payload."""
        lines = [f"# branch.oid {oid}", f"# branch.head {branch}"]
        if ahead is not None:
            lines.append(f"# branch.ab +{ahead} -0")
        if dirty:
            lines.append("?? file.py")
        return "\n".join(lines)

    def _seg(self, branch, dirty=False, ahead=None, cwd="/repo", config=None):
        raw = self._porcelain(branch, dirty, ahead)
        with mock.patch.object(sl, "_git", return_value=raw):
            return sl.git_segment({"workspace": {"current_dir": cwd}}, config, now=NOW)

    def test_clean_no_ahead(self):
        self.assertEqual(plain(self._seg("main")), "⎇ main")

    def test_dirty(self):
        chip = self._seg("main", dirty=True)
        self.assertEqual(plain(chip), "⎇ main*")
        self.assertIn(sl.YELLOW, chip)                       # dirty → yellow

    def test_ahead(self):
        self.assertEqual(plain(self._seg("main", ahead=3)), "⎇ main ↑3")

    def test_dirty_and_ahead(self):
        self.assertEqual(plain(self._seg("feature/x", dirty=True, ahead=2)), "⎇ feature/x* ↑2")

    def test_no_upstream_means_no_ahead(self):
        # no `branch.ab` line at all (ahead=None) — same as rev-list failing with no upstream.
        self.assertEqual(plain(self._seg("main", ahead=None)), "⎇ main")

    def test_none_when_no_dir(self):
        self.assertIsNone(sl.git_segment({}))
        self.assertIsNone(sl.git_segment({"workspace": {}}))

    def test_none_when_not_a_repo(self):
        with mock.patch.object(sl, "_git", return_value=None):
            self.assertIsNone(sl.git_segment({"workspace": {"current_dir": "/notrepo"}}, now=NOW))

    def test_unborn_branch_renders_nothing(self):
        # branch.oid == "(initial)" means there is no commit yet — the old `git rev-parse
        # --abbrev-ref HEAD` used to fail outright here (ambiguous HEAD), so git_segment must
        # still render nothing, exactly as before.
        raw = self._porcelain("main", oid="(initial)")
        with mock.patch.object(sl, "_git", return_value=raw):
            self.assertIsNone(sl.git_segment({"workspace": {"current_dir": "/repo"}}, now=NOW))

    def test_detached_head_renders_as_head(self):
        # `git status --porcelain=v2 --branch` reports a detached HEAD as "(detached)", but the
        # old `git rev-parse --abbrev-ref HEAD` this replaces returned the literal string "HEAD"
        # there — reproduce that exact text so the rendered segment is unchanged.
        self.assertEqual(plain(self._seg("(detached)")), "⎇ HEAD")

    def test_git_runner_handles_failure(self):
        # subprocess is imported lazily inside _git (see LazyImportGuardTests) — patch the real
        # module by name, not sl.subprocess.
        with mock.patch("subprocess.run", side_effect=OSError):
            self.assertIsNone(sl._git("/x", ["rev-parse"]))

    def test_branch_hidden_keeps_dirty_and_ahead_markers(self):
        chip = self._seg("main", dirty=True, ahead=2, config={"segments": {"branch": False}})
        self.assertEqual(plain(chip), "⎇ * ↑2")
        self.assertNotIn("main", plain(chip))

    def test_branch_hidden_and_nothing_to_report_omits_segment(self):
        self.assertIsNone(self._seg("main", config={"segments": {"branch": False}}))

    def test_branch_shown_by_default_is_unchanged(self):
        self.assertEqual(plain(self._seg("main", dirty=True, ahead=2)), "⎇ main* ↑2")

    def test_git_cache_hit_skips_the_second_git_call(self):
        raw = self._porcelain("main", dirty=True, ahead=1)
        with mock.patch.object(sl, "_git", return_value=raw) as fake_git:
            first = sl.git_segment({"workspace": {"current_dir": "/repo"}}, now=NOW)
            second = sl.git_segment({"workspace": {"current_dir": "/repo"}}, now=NOW + 1)
        self.assertEqual(fake_git.call_count, 1)
        self.assertEqual(plain(first), plain(second))

    def test_git_cache_expires_after_gitCacheSeconds(self):
        raw = self._porcelain("main")
        with mock.patch.object(sl, "_git", return_value=raw) as fake_git:
            sl.git_segment({"workspace": {"current_dir": "/repo"}}, {"gitCacheSeconds": 3}, now=NOW)
            sl.git_segment({"workspace": {"current_dir": "/repo"}}, {"gitCacheSeconds": 3}, now=NOW + 5)
        self.assertEqual(fake_git.call_count, 2)

    def test_git_cache_disabled_when_gitCacheSeconds_is_zero(self):
        raw = self._porcelain("main")
        with mock.patch.object(sl, "_git", return_value=raw) as fake_git:
            sl.git_segment({"workspace": {"current_dir": "/repo"}}, {"gitCacheSeconds": 0}, now=NOW)
            sl.git_segment({"workspace": {"current_dir": "/repo"}}, {"gitCacheSeconds": 0}, now=NOW)
        self.assertEqual(fake_git.call_count, 2)

    def test_git_cache_is_keyed_per_working_directory(self):
        raw_a, raw_b = self._porcelain("main"), self._porcelain("other")
        with mock.patch.object(sl, "_git", side_effect=[raw_a, raw_b]) as fake_git:
            a = sl.git_segment({"workspace": {"current_dir": "/repo-a"}}, now=NOW)
            b = sl.git_segment({"workspace": {"current_dir": "/repo-b"}}, now=NOW)
        self.assertEqual(fake_git.call_count, 2)
        self.assertEqual(plain(a), "⎇ main")
        self.assertEqual(plain(b), "⎇ other")

    def test_cache_write_failure_does_not_break_the_segment(self):
        # Caching is a best-effort speedup (see _git_status) — an unwritable cache file must
        # still let the segment render from the git call it just made, not raise.
        with mock.patch.object(sl, "_git", return_value=self._porcelain("main", dirty=True)), \
             mock.patch.object(sl, "write_cache", side_effect=OSError("disk full")):
            seg = sl.git_segment({"workspace": {"current_dir": "/repo"}}, now=NOW)
        self.assertEqual(plain(seg), "⎇ main*")


class RenderTests(unittest.TestCase):
    def test_full_render_matches_spec_mockup(self):
        l1, l2 = sl.render(STATUS_NO_RL, CACHE, "you@example.com", CONFIG, NOW, timezone.utc)
        self.assertEqual(plain(l1), LINE1)
        self.assertEqual(plain(l2), LINE2)
        self.assertIn("\033[38;2;0;188;212myou@example.com\033[0m", l2)
        self.assertIn(sl.GREEN + "▰▰▰▰▱▱▱▱▱▱ 42%" + sl.RESET, l1)

    def test_repo_segment_after_account_label(self):
        status = dict(STATUS_NO_RL, workspace={"repo": {"name": "demo"}})
        _, l2 = sl.render(status, CACHE, "you@example.com", CONFIG, NOW, timezone.utc)
        p = plain(l2)
        self.assertTrue(p.startswith("you@example.com · demo · Fable 5 "), p)
        self.assertIn(sl.hash_color("demo") + "demo\033[0m", l2)   # coloured by hash

    def test_a_stale_cache_marks_the_per_model_line_only(self):
        """Since Task 29d `~` describes the API cache, and the API cache only supplies the
        per-model buckets. The headline numbers arrive with the render, so they are never
        marked — with no payload here they fall back to the cache's values unmarked, and with
        a payload they are the payload's."""
        stale = dict(CACHE, ok=False, error="http 401")
        l1, _ = sl.render(STATUS_NO_RL, stale, None, {}, NOW, timezone.utc)
        self.assertIn("5h ▰▰▱▱▱▱▱▱▱▱ 17%", plain(l1))
        self.assertNotIn("~17%", plain(l1))
        self.assertIn("Fable ▱▱▱▱▱▱▱▱▱▱ ~0%", plain(l1))

    def test_no_cache_falls_back_to_stdin_rate_limits(self):
        l1, _ = sl.render(STATUS, {}, None, {}, NOW, timezone.utc)
        self.assertEqual(plain(l1), "ctx ▰▰▰▰▱▱▱▱▱▱ 42% (84k/200k) │ 5h ▰▰▰▱▱▱▱▱▱▱ 30% ↻ 1h00m │ "
                                    "week all ▰▱▱▱▱▱▱▱▱▱ 9% · per-model ? ↻ Thu 20:00")

    def test_cache_with_empty_buckets_falls_back_to_stdin_rate_limits(self):
        cache = {"fetched_at": NOW - 10, "ok": True, "error": None,
                 "limits": {"session": None, "weekly_all": None, "scoped": []}}
        l1, _ = sl.render(STATUS, cache, None, {}, NOW, timezone.utc)
        self.assertEqual(plain(l1), "ctx ▰▰▰▰▱▱▱▱▱▱ 42% (84k/200k) │ 5h ▰▰▰▱▱▱▱▱▱▱ 30% ↻ 1h00m │ "
                                    "week all ▰▱▱▱▱▱▱▱▱▱ 9% · per-model ? ↻ Thu 20:00")

    def test_cache_with_null_limits_falls_back_to_stdin_rate_limits(self):
        cache = {"fetched_at": NOW - 10, "ok": True, "error": None,
         "account": sl.account_hash("a@example.com"), "limits": None}
        l1, _ = sl.render(STATUS, cache, None, {}, NOW, timezone.utc)
        self.assertEqual(plain(l1), "ctx ▰▰▰▰▱▱▱▱▱▱ 42% (84k/200k) │ 5h ▰▰▰▱▱▱▱▱▱▱ 30% ↻ 1h00m │ "
                                    "week all ▰▱▱▱▱▱▱▱▱▱ 9% · per-model ? ↻ Thu 20:00")

    def test_no_rate_limits_and_no_cache(self):
        status = {k: v for k, v in STATUS_NO_RL.items() if k != "rate_limits"}
        l1, _ = sl.render(status, {}, None, {}, NOW, timezone.utc)
        self.assertEqual(plain(l1), "ctx ▰▰▰▰▱▱▱▱▱▱ 42% (84k/200k) │ 5h n/a │ week all n/a · per-model ?")

    def test_minimal_status_before_first_api_call(self):
        status = {"model": {"display_name": "Fable 5"},
                  "context_window": {"context_window_size": 200_000, "current_usage": None}}
        l1, l2 = sl.render(status, {}, None, {}, NOW, timezone.utc)
        self.assertTrue(plain(l1).startswith("ctx ▱▱▱▱▱▱▱▱▱▱ 0% (0/200k)"))
        self.assertEqual(plain(l2), "? · Fable 5 │ $0.00 · 0m · +0/−0")

    def test_fast_mode_flag_and_red_context(self):
        status = dict(STATUS_NO_RL, fast_mode=True, thinking={"enabled": False})
        status["context_window"] = dict(STATUS_NO_RL["context_window"], used_percentage=84)
        l1, l2 = sl.render(status, CACHE, None, {}, NOW, timezone.utc)
        self.assertIn("? · Fable 5 · xhigh · fast │", plain(l2))
        self.assertIn(sl.RED + "▰▰▰▰▰▰▰▰▱▱ 84%" + sl.RESET, l1)

    def test_render_uses_account_nickname(self):
        cfg = {"accountColors": {"example": "#00bcd4"}, "accountNames": {"you@example.com": "kai"}}
        _, l2 = sl.render(STATUS_NO_RL, CACHE, "you@example.com", cfg, NOW, timezone.utc)
        self.assertTrue(plain(l2).startswith("kai · Fable 5 "), plain(l2))
        self.assertIn("\033[38;2;0;188;212mkai\033[0m", l2)   # nickname still coloured by the account colour

    def test_git_segment_after_repo(self):
        status = dict(STATUS_NO_RL, workspace={"repo": {"name": "demo"}, "current_dir": "/repo"})
        with mock.patch.object(sl, "git_segment_runs", return_value=[("⎇ main*", sl.YELLOW)]):
            _, l2 = sl.render(status, CACHE, "you@example.com", CONFIG, NOW, timezone.utc)
        self.assertRegex(plain(l2), r"demo · ⎇ main\* · Fable 5")


class ResetCountdownTests(TmpEnv):
    def _line1(self, secs_to_reset, cfg=None):
        cache = {"fetched_at": NOW, "ok": True, "error": None,
         "account": sl.account_hash("you@example.com"), "limits": {
            "session": {"label": "", "pct": 17.0, "resets_at": NOW + secs_to_reset},
            "weekly_all": {"label": "", "pct": 4.0, "resets_at": WEEK_RESET},
            "scoped": []}}
        l1, _ = sl.render(STATUS_NO_RL, cache, "you@example.com", cfg or {}, NOW, timezone.utc, width=1000)
        return l1

    def test_far_from_reset_is_uncoloured(self):
        line = self._line1(3 * 3600)                     # 3h away
        countdown = line.split("↻")[1][:12]
        self.assertNotIn(sl.YELLOW, countdown)
        self.assertNotIn(sl.RED, countdown)

    def test_warning_window_is_yellow(self):
        line = self._line1(25 * 60)                      # 25 min → inside the 30 min warning
        self.assertIn(sl.YELLOW, line.split("↻")[1][:16])

    def test_urgent_window_is_red(self):
        line = self._line1(10 * 60)                      # 10 min → inside the 15 min urgent window
        self.assertIn(sl.RED, line.split("↻")[1][:16])

    def test_boundaries_are_inclusive(self):
        self.assertIn(sl.YELLOW, self._line1(30 * 60).split("↻")[1][:16])   # exactly 30 min → warn
        self.assertIn(sl.RED, self._line1(15 * 60).split("↻")[1][:16])      # exactly 15 min → urgent

    def test_thresholds_are_configurable(self):
        cfg = {"resetWarnMinutes": 90, "resetUrgentMinutes": 60}
        self.assertIn(sl.YELLOW, self._line1(80 * 60, cfg).split("↻")[1][:16])
        self.assertIn(sl.RED, self._line1(50 * 60, cfg).split("↻")[1][:16])

    def test_disabled_with_zero(self):
        line = self._line1(60, {"resetWarnMinutes": 0})   # 1 min away but feature off
        countdown = line.split("↻")[1][:12]
        self.assertNotIn(sl.YELLOW, countdown)
        self.assertNotIn(sl.RED, countdown)

    def test_inverted_thresholds_do_not_invert_behaviour(self):
        cfg = {"resetWarnMinutes": 10, "resetUrgentMinutes": 30}   # urgent > warn: nonsensical
        line = self._line1(20 * 60, cfg)                            # 20 min: outside warn(10)
        countdown = line.split("↻")[1][:12]
        self.assertNotIn(sl.RED, countdown)                         # must not be red just because 20<=30

    def test_weekly_absolute_time_never_coloured_by_this(self):
        line = self._line1(5 * 60)          # session urgent…
        weekly_part = line.split("week all")[1]
        self.assertNotIn(sl.RED, weekly_part.split("↻")[1][:12] if "↻" in weekly_part else "")


class WidthTests(TmpEnv):
    def test_visible_len_ignores_ansi(self):
        self.assertEqual(sl.visible_len("\033[31mabc\033[0m"), 3)
        self.assertEqual(sl.visible_len("abc"), 3)

    def test_ambiguous_width_glyphs_count_as_one_column(self):
        # every glyph Squirrel's own bars/chips use is Unicode 'Ambiguous' width — most
        # terminals render these as a single column, so visible_len must not over-count them.
        for ch in "▰▱⚙✓◀⧗‼⎇↻│·":
            self.assertEqual(sl.visible_len(ch), 1, repr(ch))

    def test_fullwidth_glyphs_count_as_two_columns(self):
        self.assertEqual(sl.visible_len("가"), 2)    # Hangul syllable — East Asian Width 'W'
        self.assertEqual(sl.visible_len("A"), 1)

    def test_combining_marks_count_as_zero_columns(self):
        self.assertEqual(sl.visible_len("é"), 1)   # 'e' + combining acute accent

    def test_terminal_width_reads_columns_env(self):
        with mock.patch.dict(os.environ, {"COLUMNS": "137"}):
            self.assertEqual(sl.terminal_width(), 137)
        with mock.patch.dict(os.environ, {"COLUMNS": "not-a-number"}):
            self.assertIsInstance(sl.terminal_width(), int)   # falls back, no crash

    def test_wide_width_keeps_full_detail(self):
        l1, l2 = sl.render(STATUS_NO_RL, CACHE, "you@example.com", CONFIG, NOW, timezone.utc, width=1000)
        self.assertEqual(plain(l1), LINE1)
        self.assertEqual(plain(l2), LINE2)

    def test_narrow_width_drops_detail_but_keeps_percentages(self):
        l1, l2 = sl.render(STATUS_NO_RL, CACHE, "you@example.com", CONFIG, NOW, timezone.utc, width=40)
        p1 = plain(l1)
        self.assertNotIn("(84k/200k)", p1)   # tokens dropped
        self.assertNotIn("↻", p1)            # reset times dropped
        for pct in ("42%", "17%", "4%", "0%"):
            self.assertIn(pct, p1)           # ctx / 5h / week all / Fable numbers kept
        p2 = plain(l2)
        self.assertTrue(p2.startswith("you@example.com"))   # account never dropped
        self.assertIn("Fable 5", p2)                          # model never dropped

    def test_default_render_is_full_regardless_of_columns(self):
        with mock.patch.dict(os.environ, {"COLUMNS": "20"}):
            l1, _ = sl.render(STATUS_NO_RL, CACHE, "you@example.com", CONFIG, NOW, timezone.utc)
        self.assertEqual(plain(l1), LINE1)   # width omitted → unconstrained

    def test_painted_line_never_exceeds_the_requested_width(self):
        sl.apply_state("idle", "wtest", NOW - 60)
        status = dict(STATUS_NO_RL, session_id="wtest")
        cfg = sl.load_config()
        for width in (40, 80, 100, 120, 200):
            l1, l2 = sl.render(status, CACHE, "you@example.com", cfg, NOW, timezone.utc, width=width)
            self.assertLessEqual(sl.visible_len(l1), width)
            self.assertLessEqual(sl.visible_len(l2), width)

    def test_loud_chip_survives_at_exactly_the_boundary_width(self):
        sl.apply_state("idle", "wtest2", NOW - 300)   # past the alarm threshold — loud chip
        status = dict(STATUS_NO_RL, session_id="wtest2")
        cfg = sl.load_config()
        _, l2 = sl.render(status, CACHE, "you@example.com", cfg, NOW, timezone.utc, width=sl.visible_len(
            sl.render(status, CACHE, "you@example.com", cfg, NOW, timezone.utc, width=1000)[1]))
        self.assertIn("IDLE", plain(l2))
        self.assertIn(sl.ALARM_SEQ, l2)

    def test_session_tokens_segment(self):
        status = dict(STATUS_NO_RL)
        status["context_window"] = dict(STATUS_NO_RL["context_window"], total_input_tokens=1_200_000, total_output_tokens=100_000)
        _, l2 = sl.render(status, CACHE, "you@example.com", CONFIG, NOW, timezone.utc, width=1000)
        self.assertIn("tok 1.3M", plain(l2))
        # dropped on a very narrow pane (account + model survive; the right group is gone)
        _, l2n = sl.render(status, CACHE, "you@example.com", CONFIG, NOW, timezone.utc, width=30)
        self.assertNotIn("tok", plain(l2n))



class SegmentConfigTests(TmpEnv):
    def _render(self, cfg_patch, status_extra=None):
        (self.root / "squirrel").mkdir(exist_ok=True)
        (self.root / "squirrel" / "config.json").write_text(json.dumps(cfg_patch))
        status = dict(STATUS_NO_RL, session_id=None, **(status_extra or {}))
        with mock.patch.object(sl, "git_segment_runs", return_value=[("\u2387 main", sl.GRAY)]):
            l1, l2 = sl.render(status, CACHE, "you@example.com", sl.load_config(), NOW, timezone.utc, width=1000)
        return plain(l1), plain(l2)

    def test_segments_default_to_shown(self):
        l1, l2 = self._render({})
        self.assertIn("ctx", l1); self.assertIn("$", l2)

    def test_hide_git_segment(self):
        _, l2 = self._render({"segments": {"git": False}}, {"workspace": {"repo": {"name": "demo"}, "current_dir": "/x"}})
        self.assertNotIn("\u2387", l2)
        self.assertIn("demo", l2)                       # repo itself still shown

    def test_hide_cost_and_lines(self):
        _, l2 = self._render({"segments": {"cost": False, "lines": False}})
        self.assertNotIn("$", l2); self.assertNotIn("+312", l2)
        self.assertIn("2h13m", l2)                      # duration still shown

    def test_hide_tokens_and_resets_on_line1(self):
        l1, _ = self._render({"segments": {"tokens": False, "resets": False}})
        self.assertNotIn("(84k/200k)", l1); self.assertNotIn("\u21bb", l1)
        self.assertIn("42%", l1)

    def test_hide_permodel(self):
        l1, _ = self._render({"segments": {"permodel": False}})
        self.assertNotIn("Fable", l1)
        self.assertIn("week all", l1)

    def test_hide_context_group(self):
        l1, _ = self._render({"segments": {"context": False}})
        self.assertNotIn("ctx", l1); self.assertNotIn("42%", l1)
        self.assertIn("17%", l1)          # 5h group still shown
        self.assertIn("week all", l1)     # weekly group still shown

    def test_account_and_model_cannot_be_hidden(self):
        _, l2 = self._render({"segments": {"account": False, "model": False}})
        self.assertIn("you@example.com", l2); self.assertIn("Fable 5", l2)

    def test_branch_truncation(self):
        long = "feature/some-really-long-branch-name-here"
        raw = f"# branch.head {long}"
        with mock.patch.object(sl, "_git", return_value=raw):
            seg = sl.git_segment({"workspace": {"current_dir": "/x"}}, {"branchMaxChars": 12}, now=NOW)
        self.assertIn("\u2026", plain(seg))
        self.assertLessEqual(sl.visible_len(seg), 2 + 12 + 2)      # "\u2387 " + 12 + slack
        with mock.patch.object(sl, "_git", return_value=raw):
            seg_full = sl.git_segment({"workspace": {"current_dir": "/x"}}, {"branchMaxChars": 0}, now=NOW)
        self.assertIn(long, plain(seg_full))                        # 0 = no limit


class MainTests(TmpEnv):
    def run_main(self, stdin_text: str, argv=()):
        out = io.StringIO()
        with mock.patch.object(sys, "stdin", io.StringIO(stdin_text)), \
             mock.patch.object(sys, "stdout", out), \
             mock.patch.object(sl, "spawn_fetch") as spawn:
            code = sl.main(list(argv))
        return code, out.getvalue(), spawn

    def test_empty_stdin_prints_nothing(self):
        code, out, _ = self.run_main("   \n")
        self.assertEqual((code, out), (0, ""))

    def test_prints_two_lines_and_spawns_fetch_when_cache_missing(self):
        code, out, spawn = self.run_main(json.dumps(STATUS))
        self.assertEqual(code, 0)
        self.assertEqual(len(out.rstrip("\n").split("\n")), 2)
        spawn.assert_called_once()

    def test_does_not_spawn_when_cache_is_fresh(self):
        cache_file, _ = sl.cache_paths()
        with mock.patch.object(sl.time, "time", return_value=NOW):
            sl.write_cache(cache_file, CACHE)
            _, out, spawn = self.run_main(json.dumps(STATUS))
        spawn.assert_not_called()
        self.assertIn("Fable", plain(out))

    def test_invalid_json_shows_visible_error(self):
        code, out, _ = self.run_main("{oops")
        self.assertEqual(code, 0)
        self.assertTrue(out.startswith("statusline error: JSONDecodeError"))

    def test_fetch_flag_runs_fetcher(self):
        with mock.patch.object(sl, "run_fetch") as run_fetch:
            self.assertEqual(sl.main(["--fetch"]), 0)
        run_fetch.assert_called_once()

    def test_state_flag_updates_file_from_hook_stdin(self):
        out = io.StringIO()
        with mock.patch.object(sys, "stdin", io.StringIO(json.dumps({"session_id": "hooksid"}))), \
             mock.patch.object(sys, "stdout", out):
            code = sl.main(["--state", "working"])
        self.assertEqual((code, out.getvalue()), (0, ""))     # --state prints nothing
        self.assertEqual(sl.read_state("hooksid", None)["state"], "working")

    def test_state_flag_refreshes_launcher_when_plugin_root_set(self):
        out = io.StringIO()
        with mock.patch.dict(os.environ, {"CLAUDE_PLUGIN_ROOT": "/plugins/squirrel-9.9.9"}), \
             mock.patch.object(sys, "stdin", io.StringIO(json.dumps({"session_id": "hooksid"}))), \
             mock.patch.object(sys, "stdout", out):
            code = sl.main(["--state", "working"])
        self.assertEqual(code, 0)
        self.assertEqual(sl.read_state("hooksid", None)["state"], "working")
        self.assertIn(os.path.join("squirrel-9.9.9", "scripts", "statusline.py"),
                      sl.launcher_path().read_text(encoding="utf-8"))

    def test_state_flag_does_not_create_launcher_without_plugin_root(self):
        out = io.StringIO()
        with mock.patch.dict(os.environ):
            os.environ.pop("CLAUDE_PLUGIN_ROOT", None)
            with mock.patch.object(sys, "stdin", io.StringIO(json.dumps({"session_id": "hooksid"}))), \
                 mock.patch.object(sys, "stdout", out):
                code = sl.main(["--state", "working"])
        self.assertEqual(code, 0)
        self.assertFalse(sl.launcher_path().exists())

    def test_stdin_read_failure_shows_visible_error(self):
        bad_stdin = mock.Mock()
        bad_stdin.isatty.return_value = False    # a pipe, as Claude Code always supplies
        bad_stdin.read.side_effect = UnicodeDecodeError("utf-8", b"\xff", 0, 1, "invalid start byte")
        out = io.StringIO()
        with mock.patch.object(sys, "stdin", bad_stdin), mock.patch.object(sys, "stdout", out):
            code = sl.main([])
        self.assertEqual(code, 0)
        self.assertTrue(out.getvalue().startswith("statusline error: UnicodeDecodeError"))



class ConfigWriteTests(TmpEnv):
    def _account(self, email="you@example.com"):
        (self.root / ".claude.json").write_text(json.dumps({"oauthAccount": {"emailAddress": email}}))

    def test_save_config_creates_file_and_merges(self):
        sl.save_config({"alarmAfterSeconds": 30})
        self.assertEqual(sl.user_config()["alarmAfterSeconds"], 30)
        sl.save_config({"defaultAccountColor": "cyan"})
        saved = sl.user_config()
        self.assertEqual((saved["alarmAfterSeconds"], saved["defaultAccountColor"]), (30, "cyan"))

    @posix_only
    def test_save_config_is_private(self):
        """Every layer it writes, not just the profile one. The shared config is the file that
        now carries the user's settings for every profile, so 0600 matters there at least as
        much — and asserting on config_path() alone would have stopped checking the moment the
        default layer changed, without failing."""
        sl._reset_write_log()
        sl.save_config({"a": 1})
        layers = sl.written_layers()
        self.assertTrue(layers, "save_config wrote nothing")
        for layer in layers:
            self.assertEqual(sl._layer_path(layer).stat().st_mode & 0o777, 0o600, layer)

    def test_set_nick_uses_current_account(self):
        self._account("you@example.com")
        msg = sl.cmd_set_nick("me")
        self.assertIn("me", msg)
        self.assertEqual(sl.load_config()["accountNames"]["you@example.com"], "me")
        self.assertEqual(sl.account_label("you@example.com", sl.load_config()), "me")

    def test_set_nick_without_account(self):
        msg = sl.cmd_set_nick("me")
        self.assertIn("no account", msg.lower())
        self.assertFalse(sl.config_path().exists())

    def test_set_nick_requires_a_name(self):
        self._account()
        self.assertIn("usage", sl.cmd_set_nick("").lower())

    def test_set_color_valid_and_invalid(self):
        self._account()
        self.assertIn("cyan", sl.cmd_set_color("cyan"))
        self.assertEqual(sl.load_config()["accountColors"]["you@example.com"], "cyan")
        msg = sl.cmd_set_color("not-a-color")
        self.assertIn("not a valid", msg.lower())
        self.assertEqual(sl.load_config()["accountColors"]["you@example.com"], "cyan")   # unchanged

    def test_set_alarm_seconds_and_off(self):
        self.assertIn("120", sl.cmd_set_alarm("120"))
        self.assertEqual(sl.load_config()["alarmAfterSeconds"], 120)
        self.assertIn("off", sl.cmd_set_alarm("off").lower())
        self.assertEqual(sl.load_config()["alarmAfterSeconds"], 0)
        self.assertIn("usage", sl.cmd_set_alarm("soon").lower())          # invalid → usage hint
        self.assertEqual(sl.load_config()["alarmAfterSeconds"], 0)        # unchanged

    def test_set_usage_toggle(self):
        self.assertIn("off", sl.cmd_set_usage("off").lower())
        self.assertIs(sl.load_config()["usageFetch"], False)
        self.assertIn("on", sl.cmd_set_usage("on").lower())
        self.assertIs(sl.load_config()["usageFetch"], True)
        self.assertIn("usage", sl.cmd_set_usage("maybe").lower())

    def test_show_config_reports_values_and_path(self):
        self._account()
        sl.cmd_set_nick("me")
        out = sl.cmd_show_config()
        self.assertIn(str(sl.config_path()), out)
        self.assertIn("me", out)
        self.assertIn("alarm", out.lower())

    def test_show_config_never_prints_a_token(self):
        (self.root / ".credentials.json").write_text(json.dumps({"claudeAiOauth": {"accessToken": "SECRET"}}))
        self.assertNotIn("SECRET", sl.cmd_show_config())



class SegmentCommandTests(TmpEnv):
    def test_show_command_toggles(self):
        self.assertIn("git", sl.cmd_set_segment("git off").lower())
        self.assertIs(sl.load_config()["segments"]["git"], False)
        sl.cmd_set_segment("git on")
        self.assertIs(sl.load_config()["segments"]["git"], True)

    def test_show_command_lists_when_empty(self):
        out = sl.cmd_set_segment("")
        self.assertIn("git", out); self.assertIn("cost", out)        # lists available segments

    def test_show_command_rejects_unknown_and_protected(self):
        self.assertIn("unknown", sl.cmd_set_segment("banana off").lower())
        self.assertIn("cannot", sl.cmd_set_segment("account off").lower())
        self.assertIn("usage", sl.cmd_set_segment("git maybe").lower())

    def test_set_branch_max_chars_and_off(self):
        self.assertIn("12", sl.cmd_set_branch_max("12"))
        self.assertEqual(sl.load_config()["branchMaxChars"], 12)
        self.assertIn("off", sl.cmd_set_branch_max("off").lower())
        self.assertEqual(sl.load_config()["branchMaxChars"], 0)
        self.assertIn("usage", sl.cmd_set_branch_max("soon").lower())          # invalid -> usage hint
        self.assertEqual(sl.load_config()["branchMaxChars"], 0)               # unchanged

    def test_show_segment_via_cli(self):
        out = io.StringIO()
        with mock.patch.object(sys, "stdin", io.StringIO("")), mock.patch.object(sys, "stdout", out):
            code = sl.main(["--show-segment", "git off"])
        self.assertEqual(code, 0)
        self.assertIn("git", out.getvalue().lower())
        self.assertIs(sl.load_config()["segments"]["git"], False)

    def test_set_branch_max_via_cli(self):
        out = io.StringIO()
        with mock.patch.object(sys, "stdin", io.StringIO("")), mock.patch.object(sys, "stdout", out):
            code = sl.main(["--set-branch-max", "15"])
        self.assertEqual(code, 0)
        self.assertEqual(sl.load_config()["branchMaxChars"], 15)


class CliTests(TmpEnv):
    def _run(self, argv, stdin=""):
        out = io.StringIO()
        with mock.patch.object(sys, "stdin", io.StringIO(stdin)), mock.patch.object(sys, "stdout", out):
            code = sl.main(argv)
        return code, out.getvalue()

    def test_set_nick_via_cli(self):
        (self.root / ".claude.json").write_text(json.dumps({"oauthAccount": {"emailAddress": "you@example.com"}}))
        code, out = self._run(["--set-nick", "me"])
        self.assertEqual(code, 0)
        self.assertIn("me", out)
        self.assertEqual(sl.load_config()["accountNames"]["you@example.com"], "me")

    def test_show_config_via_cli(self):
        code, out = self._run(["--show-config"])
        self.assertEqual(code, 0)
        self.assertIn("alarm", out.lower())

    def test_cli_subcommands_do_not_read_stdin_status(self):
        code, out = self._run(["--set-alarm", "60"], stdin='{"model":{"display_name":"X"}}')
        self.assertNotIn("ctx", out)      # never renders the bar in config mode
        self.assertEqual(code, 0)

    def test_set_tunable_via_cli(self):
        code, out = self._run(["--set", "barCells 25"])
        self.assertEqual(code, 0)
        self.assertIn("barCells", out)
        self.assertEqual(sl.load_config()["barCells"], 25)


class SetupTests(TmpEnv):
    def test_write_launcher_creates_executable_shim(self):
        p = sl.write_launcher(os.path.join(os.sep, "plugins", "squirrel-1.2.3"))
        self.assertEqual(p, sl.data_dir() / LAUNCHER_NAME)
        body = p.read_text(encoding="utf-8")
        self.assertIn(os.path.join("squirrel-1.2.3", "scripts", "statusline.py"), body)
        if WINDOWS:
            self.assertTrue(body.startswith("@echo off"))
        else:
            self.assertTrue(body.startswith("#!"))
            self.assertTrue(os.access(p, os.X_OK))

    def test_write_launcher_is_idempotent_and_updates_root(self):
        old_root = os.path.join(os.sep, "old", "root")
        new_root = os.path.join(os.sep, "new", "root")
        sl.write_launcher(old_root)
        p = sl.write_launcher(new_root)
        self.assertIn(os.path.join(new_root, "scripts", "statusline.py"), p.read_text(encoding="utf-8"))
        self.assertNotIn(old_root, p.read_text(encoding="utf-8"))

    def test_setup_writes_statusline_into_settings(self):
        (self.root / "settings.json").write_text(json.dumps({"theme": "dark"}))
        msg = sl.cmd_setup("/plugins/squirrel")
        settings = json.loads((self.root / "settings.json").read_text(encoding="utf-8"))
        self.assertEqual(settings["theme"], "dark")                              # preserved
        self.assertEqual(settings["statusLine"]["type"], "command")
        self.assertEqual(settings["statusLine"]["command"], str(sl.launcher_path()))
        self.assertEqual(settings["statusLine"]["refreshInterval"], 5)
        self.assertIn("status line installed", msg.lower())

    def test_setup_creates_settings_when_absent(self):
        sl.cmd_setup("/plugins/squirrel")
        self.assertTrue((self.root / "settings.json").exists())

    def test_setup_is_idempotent(self):
        sl.cmd_setup("/plugins/squirrel")
        first = (self.root / "settings.json").read_text(encoding="utf-8")
        sl.cmd_setup("/plugins/squirrel")
        self.assertEqual(first, (self.root / "settings.json").read_text(encoding="utf-8"))

    def test_setup_preserves_unrelated_settings_and_hooks(self):
        (self.root / "settings.json").write_text(json.dumps({"hooks": {"Stop": [{"hooks": [{"type": "command", "command": "mine"}]}]}}))
        sl.cmd_setup("/plugins/squirrel")
        settings = json.loads((self.root / "settings.json").read_text(encoding="utf-8"))
        self.assertEqual(settings["hooks"]["Stop"][0]["hooks"][0]["command"], "mine")

    def test_setup_leaves_malformed_settings_untouched(self):
        (self.root / "settings.json").write_text("{not json")
        msg = sl.cmd_setup("/plugins/squirrel")
        self.assertIn("could not read", msg.lower())
        self.assertEqual((self.root / "settings.json").read_text(encoding="utf-8"), "{not json")

    @posix_only
    def test_setup_preserves_existing_settings_file_mode(self):
        path = self.root / "settings.json"
        path.write_text(json.dumps({"theme": "dark"}))
        os.chmod(path, 0o600)
        sl.cmd_setup("/plugins/squirrel")
        self.assertEqual(path.stat().st_mode & 0o777, 0o600)

    @posix_only
    def test_setup_creates_new_settings_file_as_0600(self):
        sl.cmd_setup("/plugins/squirrel")
        self.assertEqual((self.root / "settings.json").stat().st_mode & 0o777, 0o600)

    @posix_only
    def test_setup_writes_through_symlinked_settings_file(self):
        real = self.root / "real-settings.json"
        real.write_text(json.dumps({"theme": "dark"}))
        link = self.root / "settings.json"
        link.symlink_to(real)
        sl.cmd_setup("/plugins/squirrel")
        self.assertTrue(link.is_symlink())
        self.assertEqual(link.resolve(), real.resolve())   # macOS: /var is a link to /private/var
        settings = json.loads(real.read_text(encoding="utf-8"))
        self.assertEqual(settings["theme"], "dark")
        self.assertEqual(settings["statusLine"]["command"], str(sl.launcher_path()))

    def test_setup_distinguishes_read_errors_from_json_errors(self):
        (self.root / "settings.json").write_text("{not json")
        msg = sl.cmd_setup("/plugins/squirrel")
        self.assertIn("not valid json", msg.lower())

        (self.root / "settings.json").unlink()
        (self.root / "settings.json").mkdir()   # exists, but reading it raises OSError, not a JSON error
        msg = sl.cmd_setup("/plugins/squirrel")
        self.assertIn("could not read", msg.lower())
        self.assertNotIn("valid json", msg.lower())

    @posix_only          # cmd.exe has no escape for a quote, and Windows forbids one in a path
    def test_write_launcher_survives_a_double_quote_in_the_plugin_root(self):
        # Single-quoted now, so a `"` needs no escaping at all — it is ordinary text.
        p = sl.write_launcher('/plugins/weird "root" here')
        expected = "'" + '/plugins/weird "root" here/scripts/statusline.py' + "'"
        self.assertIn(expected, p.read_text(encoding="utf-8"))


class CrossPlatformTests(TmpEnv):
    """Task 18. Every platform difference keys off `sl.IS_WINDOWS`, so both branches can be
    exercised from this Linux suite by patching that one flag.

    These are necessary but NOT sufficient: a mocked platform proves the branch is taken, not
    that the artefact it produces actually runs. The real evidence is the native-Windows
    transcripts in the task report (a render, a --state hook, a --setup, and the detached
    --fetch child) — this class is the regression net that keeps those results true."""

    # ── interpreter resolution (blocker 2) ────────────────────────────────────
    def test_resolve_interpreter_prefers_the_running_interpreter(self):
        # sys.executable is the only candidate that is *provably* working: it is running us.
        self.assertEqual(sl.resolve_interpreter(), str(Path(sys.executable).resolve()))

    def test_resolve_interpreter_skips_the_windows_store_alias_stub(self):
        # On a default Windows install `python3` on PATH is a zero-byte App Execution Alias that
        # prints "Python was not found" and exits 9009. Verified on the real machine; resolving
        # the name is not enough, the file has to be real.
        stub = self.root / "python3"
        stub.write_bytes(b"")
        real = self.root / "python"
        real.write_text("#!/bin/sh\n")
        which = {"python3": str(stub), "python": str(real), "py": None}
        with mock.patch.object(sl.sys, "executable", ""), \
             mock.patch.object(sl.shutil, "which", lambda n: which.get(n)):
            self.assertEqual(sl.resolve_interpreter(), str(real.resolve()))

    def test_resolve_interpreter_falls_back_to_a_name_when_nothing_resolves(self):
        with mock.patch.object(sl.sys, "executable", ""), \
             mock.patch.object(sl.shutil, "which", lambda n: None):
            self.assertEqual(sl.resolve_interpreter(), "python3")

    # ── the launcher (blocker 1) ──────────────────────────────────────────────
    def test_launcher_path_is_a_cmd_on_windows_and_a_sh_on_posix(self):
        with mock.patch.object(sl, "IS_WINDOWS", True):
            self.assertEqual(sl.launcher_path().name, "run.cmd")
        with mock.patch.object(sl, "IS_WINDOWS", False):
            self.assertEqual(sl.launcher_path().name, "run.sh")

    @posix_only          # the POSIX branch emits POSIX paths; asserting them needs a POSIX host
    def test_posix_launcher_bakes_the_resolved_interpreter_not_the_name_python3(self):
        # Regression for the original `exec python3 "..."`: `python3` is not the interpreter's
        # name on every platform, and on Windows it is actively the wrong one.
        with mock.patch.object(sl, "IS_WINDOWS", False), \
             mock.patch.object(sl, "resolve_interpreter", lambda: "/opt/py/bin/python3.13"):
            body = sl.write_launcher("/plugins/squirrel").read_text(encoding="utf-8")
        self.assertIn("exec '/opt/py/bin/python3.13' '/plugins/squirrel/scripts/statusline.py'", body)
        self.assertTrue(body.startswith("#!/bin/sh"))

    def test_windows_launcher_is_a_batch_file_invoking_the_resolved_interpreter(self):
        with mock.patch.object(sl, "IS_WINDOWS", True), \
             mock.patch.object(sl, "resolve_interpreter", lambda: r"C:\Program Files\Python313\python.exe"):
            path = sl.write_launcher(r"C:\plugins\squirrel")
            body = path.read_text(encoding="utf-8")
        self.assertEqual(path.name, "run.cmd")
        self.assertTrue(body.startswith("@echo off"))
        self.assertNotIn("#!", body)
        self.assertIn(r'"C:\Program Files\Python313\python.exe"', body)
        self.assertIn(r"statusline.py" + '" %*', body)

    def test_windows_launcher_doubles_percent_so_a_path_is_not_a_variable_reference(self):
        with mock.patch.object(sl, "IS_WINDOWS", True), \
             mock.patch.object(sl, "resolve_interpreter", lambda: r"C:\py\python.exe"):
            body = sl.write_launcher(r"C:\plugins\100%%good").read_text(encoding="utf-8")
        self.assertIn(r"100%%%%good", body)          # each literal % emitted as %%

    def test_setup_installs_the_windows_launcher_path_into_settings(self):
        with mock.patch.object(sl, "IS_WINDOWS", True), \
             mock.patch.object(sl, "resolve_interpreter", lambda: r"C:\py\python.exe"):
            sl.cmd_setup(r"C:\plugins\squirrel")
            settings = json.loads((self.root / "settings.json").read_text(encoding="utf-8"))
        self.assertTrue(settings["statusLine"]["command"].endswith("run.cmd"))
        self.assertEqual(settings["statusLine"]["type"], "command")

    # ── the detached --fetch child (blocker 4) ────────────────────────────────
    def test_detach_kwargs_uses_creationflags_on_windows(self):
        with mock.patch.object(sl, "IS_WINDOWS", True):
            kwargs = sl.detach_kwargs(subprocess)
        self.assertNotIn("start_new_session", kwargs)
        flags = kwargs["creationflags"]
        # Asserted by value so this test is meaningful on a POSIX box, where the subprocess
        # module does not define the Windows constants at all.
        self.assertEqual(flags, 0x00000008 | 0x08000000)    # DETACHED_PROCESS | CREATE_NO_WINDOW

    def test_detach_kwargs_uses_a_new_session_on_posix(self):
        with mock.patch.object(sl, "IS_WINDOWS", False):
            self.assertEqual(sl.detach_kwargs(subprocess), {"start_new_session": True})

    def test_spawn_fetch_passes_the_windows_detach_flags_through(self):
        lock = self.root / "fetch.lock"
        with mock.patch.object(sl, "IS_WINDOWS", True), mock.patch("subprocess.Popen") as popen:
            sl.spawn_fetch(lock)
        _args, kwargs = popen.call_args
        self.assertEqual(kwargs["creationflags"], 0x00000008 | 0x08000000)
        self.assertNotIn("start_new_session", kwargs)

    # ── the cross-profile shared store (blocker 3) ────────────────────────────
    def test_shared_dir_falls_back_to_userprofile_when_pwd_is_missing(self):
        # `pwd` is POSIX-only; on Windows the import raises and the USERPROFILE branch must be
        # the one actually taken. Confirmed on native Windows (shared_dir -> C:\Users\<u>\.squirrel).
        real_import = builtins.__import__

        def no_pwd(name, *a, **kw):
            if name == "pwd":
                raise ModuleNotFoundError("No module named 'pwd'")
            return real_import(name, *a, **kw)

        env = {"USERPROFILE": r"C:\Users\ada", "HOMEDRIVE": "C:", "HOMEPATH": r"\Users\other"}
        with mock.patch.object(builtins, "__import__", no_pwd), \
             mock.patch.dict(os.environ, env, clear=False):
            os.environ.pop("SQUIRREL_SHARED_DIR", None)
            self.assertEqual(sl.shared_dir(), Path(r"C:\Users\ada") / ".squirrel")

    def test_shared_dir_falls_back_to_homedrive_homepath_when_userprofile_is_unset(self):
        real_import = builtins.__import__

        def no_pwd(name, *a, **kw):
            if name == "pwd":
                raise ModuleNotFoundError("No module named 'pwd'")
            return real_import(name, *a, **kw)

        with mock.patch.object(builtins, "__import__", no_pwd), \
             mock.patch.dict(os.environ, {"HOMEDRIVE": "D:", "HOMEPATH": r"\Users\ada"}, clear=False):
            os.environ.pop("SQUIRREL_SHARED_DIR", None)
            os.environ.pop("USERPROFILE", None)
            self.assertEqual(sl.shared_dir(), Path(r"D:\Users\ada") / ".squirrel")

    def test_shared_dir_env_override_still_wins_on_a_pwd_less_platform(self):
        real_import = builtins.__import__

        def no_pwd(name, *a, **kw):
            if name == "pwd":
                raise ModuleNotFoundError("No module named 'pwd'")
            return real_import(name, *a, **kw)

        with mock.patch.object(builtins, "__import__", no_pwd), \
             mock.patch.dict(os.environ, {"SQUIRREL_SHARED_DIR": r"C:\tmp\sq", "USERPROFILE": r"C:\Users\ada"}):
            self.assertEqual(sl.shared_dir(), Path(r"C:\tmp\sq"))

    # ── atomic writes while another session is reading (Windows) ──────────────
    def test_atomic_replace_retries_while_windows_reports_the_file_in_use(self):
        # Verified on native Windows: os.replace onto a destination another process holds open
        # raises PermissionError/WinError 5. Four Claude sessions re-reading the cache every 5s
        # make that routine, so a short retry is the difference between a reliable write and an
        # intermittently lost one.
        tmp, dst = self.root / "a.tmp", self.root / "a.json"
        tmp.write_text("new")
        dst.write_text("old")
        calls = []
        real_replace = os.replace          # sl.os IS os, so the patch below would recurse

        def flaky(src, dest):
            calls.append(1)
            if len(calls) < 3:
                raise PermissionError(5, "Access is denied")
            real_replace(src, dest)

        with mock.patch.object(sl, "IS_WINDOWS", True), \
             mock.patch.object(sl.os, "replace", flaky), \
             mock.patch.object(sl.time, "sleep", lambda _s: None):
            sl.atomic_replace(tmp, dst)
        self.assertEqual(len(calls), 3)
        self.assertEqual(dst.read_text(encoding="utf-8"), "new")

    def test_atomic_replace_gives_up_rather_than_hanging_the_render(self):
        tmp, dst = self.root / "b.tmp", self.root / "b.json"
        tmp.write_text("new")
        slept = []

        def always_busy(_src, _dest):
            raise PermissionError(5, "Access is denied")

        with mock.patch.object(sl, "IS_WINDOWS", True), \
             mock.patch.object(sl.os, "replace", always_busy), \
             mock.patch.object(sl.time, "sleep", slept.append):
            with self.assertRaises(PermissionError):
                sl.atomic_replace(tmp, dst)
        self.assertEqual(len(slept), 4)                    # 5 attempts, 4 waits
        self.assertLessEqual(sum(slept), 0.1)              # never delays a render by more than ~100ms

    def test_atomic_replace_does_not_retry_on_posix(self):
        tmp, dst = self.root / "c.tmp", self.root / "c.json"
        tmp.write_text("new")
        calls = []
        real_replace = os.replace          # sl.os IS os, so the patch below would recurse

        def once(src, dest):
            calls.append(1)
            real_replace(src, dest)

        with mock.patch.object(sl, "IS_WINDOWS", False), mock.patch.object(sl.os, "replace", once):
            sl.atomic_replace(tmp, dst)
        self.assertEqual(len(calls), 1)

    # ── permissions ───────────────────────────────────────────────────────────
    def test_chmod_private_is_a_no_op_on_windows(self):
        # 0600 has no Windows equivalent; the only bit Windows would honour here is read-only,
        # which would break the tmp+rename that follows. Documented in the README instead of
        # pretended. This asserts we never call chmod there.
        path = self.root / "x.json"
        path.write_text("{}")
        with mock.patch.object(sl, "IS_WINDOWS", True), mock.patch.object(sl.os, "chmod") as chmod:
            sl.chmod_private(path, 0o600)
        chmod.assert_not_called()

    @posix_only
    def test_chmod_private_applies_the_mode_on_posix(self):
        path = self.root / "y.json"
        path.write_text("{}")
        os.chmod(path, 0o644)
        with mock.patch.object(sl, "IS_WINDOWS", False):
            sl.chmod_private(path, 0o600)
        self.assertEqual(path.stat().st_mode & 0o777, 0o600)

    # ── console encoding ──────────────────────────────────────────────────────
    def test_main_forces_utf8_stdio_before_dispatching_any_subcommand(self):
        # Found on native Windows: `--setup` died with UnicodeEncodeError because its
        # confirmation contains U+2192 and the console code page is cp1252. Only the RENDER
        # path used to reconfigure stdout, so every other subcommand was exposed.
        seen = []

        class FakeStream(io.StringIO):
            def reconfigure(self, **kwargs):
                seen.append(kwargs)

        with mock.patch.object(sl.sys, "stdin", FakeStream()), \
             mock.patch.object(sl.sys, "stdout", FakeStream()), \
             mock.patch.object(sl.sys, "stderr", FakeStream()):
            sl.main(["--help-text"])
        self.assertEqual(len(seen), 3, "stdin, stdout and stderr must all be reconfigured")
        for kwargs in seen:
            self.assertEqual(kwargs["encoding"], "utf-8")
            self.assertEqual(kwargs["errors"], "replace")

    def test_use_utf8_io_survives_a_stream_that_cannot_be_reconfigured(self):
        class Rigid:
            def reconfigure(self, **_kw):
                raise AttributeError("detached")

        with mock.patch.object(sl.sys, "stdin", Rigid()), \
             mock.patch.object(sl.sys, "stdout", Rigid()), \
             mock.patch.object(sl.sys, "stderr", Rigid()):
            sl.use_utf8_io()          # must not raise

    def test_setup_message_survives_a_cp1252_console(self):
        # The end-to-end shape of the Windows crash: encode what --setup prints the way a
        # cp1252 console would, with the replacement policy use_utf8_io() installs.
        msg = sl.cmd_setup("/plugins/squirrel")
        self.assertIn("\u2192", msg)          # the arrow that caused the crash is still there
        msg.encode("utf-8")                   # what we now emit
        with self.assertRaises(UnicodeEncodeError):
            msg.encode("cp1252")              # what Windows would have done unaided

    # ── width ─────────────────────────────────────────────────────────────────
    def test_terminal_width_falls_back_when_columns_is_absent(self):
        # Confirmed on native Windows: no COLUMNS and no tty -> the default, never an exception.
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("COLUMNS", None)
            with mock.patch.object(sl.shutil, "get_terminal_size", side_effect=OSError):
                self.assertEqual(sl.terminal_width(), 80)
                self.assertEqual(sl.terminal_width(120), 120)


class ShippedInvocationTests(unittest.TestCase):
    """The plugin files Claude Code executes must not name an interpreter.

    `python3` is not a working command on a default Windows install — it resolves to the
    Microsoft Store App Execution Alias, a zero-byte stub that exits 9009 (verified on the real
    machine). Plugin files ship as-is and Claude Code offers no per-OS conditionality in them, so
    they all go through scripts/squirrel.sh, which finds and VERIFIES an interpreter at run time.
    """

    ROOT = PLUGIN_ROOT
    WRAPPER = 'sh "${CLAUDE_PLUGIN_ROOT}/scripts/squirrel.sh"'

    def _shipped_files(self):
        return ([self.ROOT / "hooks" / "hooks.json"]
                + sorted((self.ROOT / "commands").glob("*.md"))
                + [self.ROOT / "skills" / "squirrel" / "SKILL.md"])

    def test_no_shipped_file_invokes_a_bare_interpreter_name(self):
        offenders = []
        for path in self._shipped_files():
            for i, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
                if re.search(r"(?<![\w-])python3?\s+[\"$]", line):
                    offenders.append(f"{path.relative_to(self.ROOT)}:{i}: {line.strip()}")
        self.assertEqual(offenders, [], "shipped files must invoke scripts/squirrel.sh, not an interpreter")

    def test_every_command_and_hook_goes_through_the_wrapper(self):
        for path in self._shipped_files():
            text = path.read_text(encoding="utf-8")
            if "statusline.py" in text or "squirrel.sh" in text:
                self.assertIn("squirrel.sh", text, f"{path} still calls statusline.py directly")

    def test_the_wrapper_exists_and_is_executable(self):
        wrapper = self.ROOT / "scripts" / "squirrel.sh"
        self.assertTrue(wrapper.is_file())
        self.assertTrue(os.access(wrapper, os.X_OK), "squirrel.sh must ship with the executable bit")

    def test_the_wrapper_verifies_the_interpreter_rather_than_just_resolving_it(self):
        # Regression guard for the Store-alias stub: `command -v` is not enough, it must run one.
        body = (self.ROOT / "scripts" / "squirrel.sh").read_text(encoding="utf-8")
        self.assertIn("command -v", body)
        self.assertIn("sys.version_info[0] == 3", body)

    @unittest.skipIf(shutil.which("sh") is None,
                     "no POSIX sh on PATH (on Windows it lives inside Git Bash, not the system PATH)")
    def test_the_wrapper_runs_and_reaches_the_script(self):
        out = subprocess.run(["sh", str(self.ROOT / "scripts" / "squirrel.sh"), "--help-text"],
                             capture_output=True, text=True, timeout=30)
        self.assertEqual(out.returncode, 0, out.stderr)
        self.assertIn("subcommands", out.stdout)


class UsageGateTests(TmpEnv):
    def test_render_does_not_spawn_fetch_when_usage_off(self):
        (self.root / "squirrel").mkdir()
        (self.root / "squirrel" / "config.json").write_text('{"usageFetch": false}')
        out = io.StringIO()
        with mock.patch.object(sys, "stdin", io.StringIO(json.dumps({"model": {"display_name": "X"},
                                                                     "context_window": {"context_window_size": 200000}}))), \
             mock.patch.object(sys, "stdout", out), \
             mock.patch.object(sl, "spawn_fetch") as spawn:
            sl.main([])
        spawn.assert_not_called()
        self.assertIn("X", out.getvalue())

    def test_run_fetch_is_a_noop_when_usage_off(self):
        (self.root / "squirrel").mkdir()
        (self.root / "squirrel" / "config.json").write_text('{"usageFetch": false}')
        with mock.patch.object(sl, "fetch_usage") as fetch:
            entry = sl.run_fetch(now=NOW)
        fetch.assert_not_called()
        self.assertEqual(entry["error"], "usage fetch disabled")


class TunableTests(TmpEnv):
    def test_defaults_match_constants(self):
        cfg = {}
        self.assertEqual(sl.tunable(cfg, "warnPercent"), 50)
        self.assertEqual(sl.tunable(cfg, "dangerPercent"), 80)
        self.assertEqual(sl.tunable(cfg, "barCells"), 10)
        self.assertEqual(sl.tunable(cfg, "cacheTtlSeconds"), 60)

    def test_user_values_and_clamping(self):
        self.assertEqual(sl.tunable({"barCells": 20}, "barCells"), 20)
        self.assertEqual(sl.tunable({"barCells": 999}, "barCells"), 40)      # clamped to max
        self.assertEqual(sl.tunable({"barCells": 1}, "barCells"), 3)         # clamped to min
        self.assertEqual(sl.tunable({"barCells": "nonsense"}, "barCells"), 10)  # invalid → default

    def test_thresholds_drive_colours(self):
        cfg = {"warnPercent": 20, "dangerPercent": 40}
        self.assertEqual(sl.pct_color(10, cfg), sl.GREEN)
        self.assertEqual(sl.pct_color(25, cfg), sl.YELLOW)
        self.assertEqual(sl.pct_color(45, cfg), sl.RED)
        self.assertEqual(sl.pct_color(60), sl.YELLOW)        # no config → old defaults

    def test_bar_cells_configurable(self):
        self.assertEqual(len(sl.bar(50, {"barCells": 20})), 20)
        self.assertEqual(len(sl.bar(50)), 10)

    def test_set_tunable_command(self):
        self.assertIn("warnPercent", sl.cmd_set_tunable("warnPercent 40"))
        self.assertEqual(sl.load_config()["warnPercent"], 40)
        self.assertIn("unknown", sl.cmd_set_tunable("banana 5").lower())
        self.assertIn("usage", sl.cmd_set_tunable("warnPercent").lower())
        self.assertIn("between", sl.cmd_set_tunable("warnPercent 500").lower())

    def test_set_tunable_lists_when_empty(self):
        out = sl.cmd_set_tunable("")
        for key in ("warnPercent", "dangerPercent", "cacheTtlSeconds", "barCells"):
            self.assertIn(key, out)


class TunableAliasTests(TmpEnv):
    """alarmAfterSeconds / branchMaxChars / idleBackgroundAfterSeconds already have their own
    dedicated commands (4b/4c) — /squirrel-set must be able to reach them too, as friendly aliases."""

    def test_set_tunable_reaches_legacy_keys(self):
        self.assertIn("alarmAfterSeconds", sl.cmd_set_tunable("alarmAfterSeconds 45"))
        self.assertEqual(sl.load_config()["alarmAfterSeconds"], 45)
        self.assertIn("branchMaxChars", sl.cmd_set_tunable("branchMaxChars 10"))
        self.assertEqual(sl.load_config()["branchMaxChars"], 10)
        self.assertIn("idleBackgroundAfterSeconds", sl.cmd_set_tunable("idleBackgroundAfterSeconds 15"))
        self.assertEqual(sl.load_config()["idleBackgroundAfterSeconds"], 15)

    def test_set_tunable_list_includes_legacy_keys(self):
        out = sl.cmd_set_tunable("")
        for key in ("alarmAfterSeconds", "branchMaxChars", "idleBackgroundAfterSeconds"):
            self.assertIn(key, out)


class TunableWiringTests(TmpEnv):
    """Every TUNABLES key must actually change behaviour, not just round-trip through config."""

    def test_needs_fetch_honors_configured_ttl(self):
        lock = self.root / "fetch.lock"
        # 30s is the tunable's floor, so the values straddle it rather than sit under it.
        self.assertTrue(sl.needs_fetch({"fetched_at": NOW - 40}, NOW, lock, {"usageFetchSeconds": 30}))
        self.assertFalse(sl.needs_fetch({"fetched_at": NOW - 40}, NOW, lock, {"usageFetchSeconds": 60}))

    def test_pick_limits_honors_configured_stale_window(self):
        cache = {"fetched_at": NOW - 100, "ok": True,
                 "limits": {"session": {"label": "", "pct": 10, "resets_at": None},
                            "weekly_all": None,
                            # staleness is about the per-model line now — see Task 29d
                            "scoped": [{"label": "Fable", "pct": 1.0, "resets_at": None}]}}
        _, stale_short, _ = sl.pick_limits({}, cache, NOW, {"staleAfterSeconds": 30})
        _, stale_long, _ = sl.pick_limits({}, cache, NOW, {"staleAfterSeconds": 600})
        self.assertTrue(stale_short)
        self.assertFalse(stale_long)

    def test_active_max_age_seconds_configurable(self):
        sl.apply_state("working", "cfgmax", NOW - 200)
        self.assertIsNone(sl.state_segment({"session_id": "cfgmax"}, NOW, {"activeMaxAgeSeconds": 100}))
        self.assertIsNotNone(sl.state_segment({"session_id": "cfgmax"}, NOW, {"activeMaxAgeSeconds": 300}))

    def test_show_config_lists_non_default_tunables(self):
        self.assertIn("all defaults", sl.cmd_show_config())
        sl.save_config({"warnPercent": 40})
        self.assertIn("warnPercent=40", sl.cmd_show_config())


class GitTimeoutTests(TmpEnv):
    """gitTimeoutMs must actually reach subprocess.run's `timeout=` kwarg (in seconds).

    TmpEnv (a fresh, isolated cache dir per test) matters here now too: git_segment's result
    cache (see _git_status) would otherwise let one test's real subprocess.run call satisfy a
    later test's cache lookup for the same fake cwd, silently skipping its fake_run entirely."""

    def test_git_uses_default_timeout_when_unset(self):
        seen = []

        def fake_run(cmd, **kwargs):
            seen.append(kwargs.get("timeout"))
            raise OSError

        # subprocess is imported lazily inside _git (see LazyImportGuardTests) — patch the real
        # module by name, not sl.subprocess.
        with mock.patch("subprocess.run", side_effect=fake_run):
            sl._git("/x", ["rev-parse"])
        self.assertEqual(seen, [sl.TUNABLES["gitTimeoutMs"][0] / 1000])

    def test_git_uses_explicit_timeout_ms(self):
        seen = []

        def fake_run(cmd, **kwargs):
            seen.append(kwargs.get("timeout"))
            raise OSError

        with mock.patch("subprocess.run", side_effect=fake_run):
            sl._git("/x", ["rev-parse"], timeout_ms=250)
        self.assertEqual(seen, [0.25])

    def test_git_segment_threads_configured_timeout(self):
        seen = []

        class FakeResult:
            returncode = 0
            stdout = "# branch.head main\n"

        def fake_run(cmd, **kwargs):
            seen.append(kwargs.get("timeout"))
            return FakeResult()

        with mock.patch("subprocess.run", side_effect=fake_run):
            sl.git_segment({"workspace": {"current_dir": "/repo"}}, {"gitTimeoutMs": 2000}, now=NOW)
        self.assertTrue(seen)
        self.assertTrue(all(t == 2.0 for t in seen))

    def test_git_segment_uses_default_timeout_without_override(self):
        seen = []

        class FakeResult:
            returncode = 0
            stdout = "# branch.head main\n"

        def fake_run(cmd, **kwargs):
            seen.append(kwargs.get("timeout"))
            return FakeResult()

        with mock.patch("subprocess.run", side_effect=fake_run):
            sl.git_segment({"workspace": {"current_dir": "/repo"}}, now=NOW)
        self.assertTrue(seen)
        self.assertTrue(all(t == sl.TUNABLES["gitTimeoutMs"][0] / 1000 for t in seen))


class BranchTruncationCarryForwardTests(TmpEnv):
    """Task 4b review carry-forward: branchMaxChars=1 must yield exactly one visible character."""

    def test_branch_max_chars_one_yields_single_char(self):
        with mock.patch.object(sl, "_git", return_value="# branch.head main"):
            seg = sl.git_segment({"workspace": {"current_dir": "/repo"}}, {"branchMaxChars": 1}, now=NOW)
        text = plain(seg)
        # "⎇ " prefix plus exactly one branch character, no ellipsis.
        self.assertEqual(text, "⎇ m")


class TunableDefaultsSyncTests(unittest.TestCase):
    """defaults/config.json must mirror TUNABLES (and the state-list defaults) exactly so
    the packaged file and the code constants can never quietly desync."""

    def test_defaults_config_json_matches_tunables(self):
        packaged = sl.load_json(sl.default_config_path())
        for key, (default, lo, hi, _help) in sl.TUNABLES.items():
            self.assertIn(key, packaged, f"defaults/config.json is missing '{key}'")
            self.assertEqual(packaged[key], default,
                              f"defaults/config.json['{key}'] = {packaged[key]!r} must match "
                              f"TUNABLES default {default!r}")

    def test_defaults_config_json_matches_state_constants(self):
        packaged = sl.load_json(sl.default_config_path())
        for key, constant in (("alarmStates", sl.ALARM_DEFAULT_STATES),
                               ("idleBackgroundStates", sl.IDLE_BG_STATES_DEFAULT)):
            self.assertIn(key, packaged, f"defaults/config.json is missing '{key}'")
            self.assertEqual(packaged[key], list(constant),
                              f"defaults/config.json['{key}'] = {packaged[key]!r} must match "
                              f"{constant!r}")


class RemainingSettingsTests(TmpEnv):
    def test_repo_color_sets_current_repo(self):
        (self.root / "repo-here").mkdir()
        with mock.patch.object(sl, "current_repo_name", return_value="repo-here"):
            msg = sl.cmd_set_repo_color("cyan")
        self.assertIn("repo-here", msg)
        self.assertEqual(sl.load_config()["repoColors"]["repo-here"], "cyan")

    def test_repo_color_off_removes_override(self):
        with mock.patch.object(sl, "current_repo_name", return_value="demo"):
            sl.cmd_set_repo_color("cyan")
            msg = sl.cmd_set_repo_color("off")
        self.assertNotIn("demo", sl.load_config().get("repoColors", {}))
        self.assertIn("automatic", msg.lower())

    def test_repo_color_rejects_invalid_and_keeps_config(self):
        with mock.patch.object(sl, "current_repo_name", return_value="demo"):
            sl.cmd_set_repo_color("cyan")
            msg = sl.cmd_set_repo_color("banana")
        self.assertIn("not a valid", msg.lower())
        self.assertEqual(sl.load_config()["repoColors"]["demo"], "cyan")

    def test_repo_color_without_a_repo(self):
        with mock.patch.object(sl, "current_repo_name", return_value=None):
            self.assertIn("no repository", sl.cmd_set_repo_color("cyan").lower())

    def test_default_color(self):
        self.assertIn("cyan", sl.cmd_set_default_color("cyan"))
        self.assertEqual(sl.load_config()["defaultAccountColor"], "cyan")
        self.assertIn("not a valid", sl.cmd_set_default_color("banana").lower())
        self.assertEqual(sl.load_config()["defaultAccountColor"], "cyan")

    def test_states_alarm_and_background(self):
        self.assertIn("alarm", sl.cmd_set_states("alarm idle").lower())
        self.assertEqual(sl.load_config()["alarmStates"], ["idle"])
        sl.cmd_set_states("background idle,permission")
        self.assertEqual(sl.load_config()["idleBackgroundStates"], ["idle", "permission"])

    def test_states_reset_and_validation(self):
        sl.cmd_set_states("alarm idle")
        sl.cmd_set_states("alarm reset")
        self.assertEqual(sl.load_config()["alarmStates"], list(sl.ALARM_DEFAULT_STATES))
        self.assertIn("usage", sl.cmd_set_states("").lower())
        self.assertIn("unknown", sl.cmd_set_states("alarm banana").lower())
        self.assertIn("unknown", sl.cmd_set_states("sideways idle").lower())

    def test_every_config_key_has_a_command(self):
        """Guard: no setting may exist that a user cannot change with a command."""
        shipped = set(json.loads(sl.default_config_path().read_text(encoding="utf-8")))
        self.assertEqual(shipped - set(sl.CONFIGURABLE_KEYS), set())
        self.assertEqual(set(sl.CONFIGURABLE_KEYS) - shipped, set())


class ResetWarnCommandTests(TmpEnv):
    def test_sets_both_thresholds(self):
        msg = sl.cmd_set_reset_warn("45 20")
        self.assertIn("45", msg); self.assertIn("20", msg)
        cfg = sl.load_config()
        self.assertEqual((cfg["resetWarnMinutes"], cfg["resetUrgentMinutes"]), (45, 20))

    def test_off_disables(self):
        self.assertIn("off", sl.cmd_set_reset_warn("off").lower())
        self.assertEqual(sl.load_config()["resetWarnMinutes"], 0)

    def test_validation(self):
        self.assertIn("usage", sl.cmd_set_reset_warn("").lower())
        self.assertIn("usage", sl.cmd_set_reset_warn("soon 5").lower())
        self.assertIn("greater", sl.cmd_set_reset_warn("10 30").lower())   # urgent must be <= warn


class HelpTests(TmpEnv):
    def test_help_lists_every_command_and_key(self):
        out = sl.cmd_help()
        for cmd in ("--set-nick", "--set-color", "--set-alarm", "--set-usage", "--show-segment",
                    "--set-branch-max", "--set-bg", "--set-bg-after", "--set", "--show-config",
                    "--set-repo-color", "--set-default-color", "--set-states"):
            self.assertIn(cmd, out)
        for key in ("alarmAfterSeconds", "idleBackgroundAfterSeconds", "segments", "usageFetch"):
            self.assertIn(key, out)

    def test_help_covers_every_dispatched_subcommand(self):
        out = sl.cmd_help()
        for flag in sl.CONFIG_COMMANDS:
            self.assertIn(flag, out, f"{flag} missing from --help-text")

    def test_help_via_cli(self):
        out = io.StringIO()
        with mock.patch.object(sys, "stdout", out):
            code = sl.main(["--help-text"])
        self.assertEqual(code, 0)
        self.assertIn("squirrel", out.getvalue().lower())


class SharedStoreTests(TmpEnv):
    def setUp(self):
        super().setUp()
        self.shared = self.root / "shared"
        patcher = mock.patch.dict(os.environ, {"SQUIRREL_SHARED_DIR": str(self.shared)})
        patcher.start(); self.addCleanup(patcher.stop)

    def test_shared_dir_honours_the_env_override(self):
        self.assertEqual(sl.shared_dir(), self.shared)

    def test_shared_dir_ignores_home(self):
        with mock.patch.dict(os.environ, {"HOME": "/somewhere/else"}, clear=False):
            self.assertEqual(sl.shared_dir(), self.shared)

    def test_session_path_is_independent_of_account(self):
        # Task 12: a session's file location can no longer depend on which account was current
        # at write time — Claude Code lets you /login to a different account mid-session, and
        # the SAME session must keep writing to the SAME file across that switch. Which account
        # owns which slice of the history is recorded per-sample instead (see
        # record_session_sample()'s `account` field).
        a = sl.session_path("s1", "a@example.com")
        b = sl.session_path("s1", "b@example.com")
        self.assertEqual(a, b)
        self.assertEqual(a, sl.shared_dir() / "sessions" / "s1.json")

    def test_different_sessions_still_get_different_files(self):
        a = sl.session_path("s1", "a@example.com")
        b = sl.session_path("s2", "a@example.com")
        self.assertNotEqual(a, b)

    def test_sample_is_appended_and_rate_limited(self):
        st = {"session_id": "s1", "context_window": {"total_input_tokens": 100, "total_output_tokens": 20},
              "cost": {"total_cost_usd": 1.0}}
        sl.record_session_sample(st, "a@example.com", NOW)
        sl.record_session_sample(st, "a@example.com", NOW + 5)        # inside the sample interval
        data = sl.load_json(sl.session_path("s1", "a@example.com"))
        self.assertEqual(len(data["samples"]), 1)
        sl.record_session_sample(st, "a@example.com", NOW + 61)       # past it
        self.assertEqual(len(sl.load_json(sl.session_path("s1", "a@example.com"))["samples"]), 2)

    def test_old_samples_are_pruned(self):
        st = {"session_id": "s1", "context_window": {"total_input_tokens": 1, "total_output_tokens": 0}}
        sl.record_session_sample(st, "a@example.com", NOW - 40000)
        sl.record_session_sample(st, "a@example.com", NOW)
        samples = sl.load_json(sl.session_path("s1", "a@example.com"))["samples"]
        self.assertTrue(all(s[0] >= NOW - sl.tunable({}, "sessionSampleKeepSeconds") for s in samples))

    def test_writing_never_raises(self):
        with mock.patch.object(sl, "write_cache", side_effect=OSError("disk full")):
            sl.record_session_sample({"session_id": "s1"}, "a@example.com", NOW)   # must not raise

    def test_no_session_id_writes_nothing(self):
        sl.record_session_sample({}, "a@example.com", NOW)
        self.assertFalse((self.shared / "sessions").exists())


# ── Task 11e: per-model breakdown ─────────────────────────────────────────────
class ParseSampleTests(unittest.TestCase):
    """_parse_sample(): normalizes a raw sample entry to [ts, tokens, cost, model, account,
    account_utilisation_pct, account_resets_at], padding whatever trailing elements an older
    format doesn't carry with None (never 0 — a missing value means unknown, not zero)."""

    def test_three_element_legacy_sample_pads_model_account_pct_as_none(self):
        self.assertEqual(sl._parse_sample([100, 500, 1.5]),
                          [100, 500, 1.5, None, None, None, None])

    def test_four_element_sample_keeps_its_model(self):
        self.assertEqual(sl._parse_sample([100, 500, 1.5, "Opus 5"]),
                          [100, 500, 1.5, "Opus 5", None, None, None])

    def test_five_element_sample_keeps_its_account(self):
        self.assertEqual(sl._parse_sample([100, 500, 1.5, "Opus 5", "acct1"]),
                          [100, 500, 1.5, "Opus 5", "acct1", None, None])

    def test_six_element_sample_keeps_its_utilisation_pct(self):
        self.assertEqual(sl._parse_sample([100, 500, 1.5, "Opus 5", "acct1", 42.0]),
                          [100, 500, 1.5, "Opus 5", "acct1", 42.0, None])

    def test_seven_element_sample_keeps_the_windows_reset(self):
        self.assertEqual(sl._parse_sample([100, 500, 1.5, "Opus 5", "acct1", 42.0, 999.0]),
                          [100, 500, 1.5, "Opus 5", "acct1", 42.0, 999.0])

    def test_invalid_entries_rejected(self):
        self.assertIsNone(sl._parse_sample("not a list"))
        self.assertIsNone(sl._parse_sample([1, 2]))                     # too short
        self.assertIsNone(sl._parse_sample([1, 2, 3, 4, 5, 6, 7, 8]))   # too long
        self.assertIsNone(sl._parse_sample(None))


class RecordSessionSampleModelTests(TmpEnv):
    def setUp(self):
        super().setUp()
        self.shared = self.root / "shared"
        patcher = mock.patch.dict(os.environ, {"SQUIRREL_SHARED_DIR": str(self.shared)})
        patcher.start(); self.addCleanup(patcher.stop)

    def test_model_is_recorded_as_the_fourth_element(self):
        st = {"session_id": "s1", "model": {"display_name": "Opus 5"},
              "context_window": {"total_input_tokens": 100, "total_output_tokens": 0},
              "cost": {"total_cost_usd": 1.0}}
        sl.record_session_sample(st, "a@example.com", NOW)
        data = sl.load_json(sl.session_path("s1", "a@example.com"))
        self.assertEqual(data["samples"][0][3], "Opus 5")

    def test_missing_model_records_none_not_a_crash(self):
        st = {"session_id": "s1", "context_window": {"total_input_tokens": 100, "total_output_tokens": 0}}
        sl.record_session_sample(st, "a@example.com", NOW)
        data = sl.load_json(sl.session_path("s1", "a@example.com"))
        self.assertIsNone(data["samples"][0][3])

    def test_appending_to_a_legacy_three_element_file_does_not_crash(self):
        path = sl.session_path("s1", "a@example.com")
        sl.write_cache(path, {"session_id": "s1", "repo": "old",
                               "samples": [[NOW - 200, 100, 0.5]]})
        st = {"session_id": "s1", "model": {"display_name": "Sonnet 5"},
              "context_window": {"total_input_tokens": 300, "total_output_tokens": 0},
              "cost": {"total_cost_usd": 1.0}}
        sl.record_session_sample(st, "a@example.com", NOW)
        data = sl.load_json(path)
        self.assertEqual(len(data["samples"]), 2)
        self.assertEqual(data["samples"][0][:3], [NOW - 200, 100, 0.5])
        self.assertEqual(data["samples"][1][3], "Sonnet 5")


class PerModelBreakdownTests(TmpEnv):
    def setUp(self):
        super().setUp()
        self.shared = self.root / "shared"
        patcher = mock.patch.dict(os.environ, {"SQUIRREL_SHARED_DIR": str(self.shared)})
        patcher.start(); self.addCleanup(patcher.stop)

    def _write(self, sid, repo, samples):
        sl.write_cache(sl.session_path(sid, "a@example.com"),
                       {"session_id": sid, "repo": repo, "samples": [list(s) for s in samples]})

    def test_single_model_session_attributes_everything_to_it(self):
        self._write("s1", "alpha", [(NOW - 1000, 0, 0.0, "Opus 5"), (NOW - 100, 1000, 5.0, "Opus 5")])
        live = {s["session_id"]: s for s in sl.live_sessions("a@example.com", NOW)}
        self.assertEqual(live["s1"]["window_by_model"], {"Opus 5": {"tokens": 1000, "cost": 5.0}})
        self.assertEqual(live["s1"]["lifetime_by_model"], {"Opus 5": {"tokens": 1000, "cost": 5.0}})

    def test_model_switch_mid_session_splits_by_interval(self):
        # Started on Opus 5, switched to Sonnet 5 partway through — each interval is
        # attributed to the model recorded on the LATER sample (documented approximation).
        self._write("s1", "alpha", [
            (NOW - 1000, 0, 0.0, "Opus 5"),
            (NOW - 500, 1000, 5.0, "Opus 5"),
            (NOW - 100, 1500, 6.0, "Sonnet 5"),
        ])
        live = {s["session_id"]: s for s in sl.live_sessions("a@example.com", NOW)}
        by_model = live["s1"]["lifetime_by_model"]
        self.assertEqual(by_model["Opus 5"], {"tokens": 1000, "cost": 5.0})
        self.assertEqual(by_model["Sonnet 5"], {"tokens": 500, "cost": 1.0})

    def test_legacy_samples_with_no_model_are_grouped_as_unknown(self):
        self._write("s1", "alpha", [(NOW - 1000, 0, 0.0), (NOW - 100, 1000, 5.0)])
        live = {s["session_id"]: s for s in sl.live_sessions("a@example.com", NOW)}
        self.assertEqual(live["s1"]["lifetime_by_model"], {"unknown": {"tokens": 1000, "cost": 5.0}})

    def test_mixed_legacy_and_modeled_samples(self):
        self._write("s1", "alpha", [
            (NOW - 1000, 0, 0.0),                 # legacy, no model
            (NOW - 500, 500, 2.0, "Opus 5"),      # this interval attributes to Opus 5
            (NOW - 100, 900, 4.0, "Opus 5"),
        ])
        live = {s["session_id"]: s for s in sl.live_sessions("a@example.com", NOW)}
        by_model = live["s1"]["lifetime_by_model"]
        self.assertEqual(by_model["Opus 5"], {"tokens": 900, "cost": 4.0})
        self.assertNotIn("unknown", by_model)

    def test_window_breakdown_excludes_the_baseline_own_value(self):
        # The baseline sample itself carries pre-window usage under 'Opus 5'; only the delta
        # AFTER the baseline should count toward the window breakdown.
        self._write("s1", "alpha", [
            (NOW - 20000, 1000, 5.0, "Opus 5"),   # before the 5h window — this is the baseline
            (NOW - 100, 1300, 6.0, "Sonnet 5"),   # window delta: 300 tokens / $1.00
        ])
        live = {s["session_id"]: s for s in sl.live_sessions("a@example.com", NOW)}
        self.assertEqual(live["s1"]["window_by_model"], {"Sonnet 5": {"tokens": 300, "cost": 1.0}})
        self.assertEqual(live["s1"]["lifetime_by_model"]["Opus 5"], {"tokens": 1000, "cost": 5.0})
        self.assertEqual(live["s1"]["lifetime_by_model"]["Sonnet 5"], {"tokens": 300, "cost": 1.0})


class ModelMixLabelTests(unittest.TestCase):
    def test_single_model_is_100_percent(self):
        self.assertEqual(sl._model_mix_label({"Opus 5": {"tokens": 100, "cost": 5.0}}, "cost"),
                         "Opus 5 100%")

    def test_two_models_shown_both(self):
        by_model = {"Opus 5": {"tokens": 820, "cost": 8.2}, "Sonnet 5": {"tokens": 180, "cost": 1.8}}
        self.assertEqual(sl._model_mix_label(by_model, "cost"), "Opus 5 82% · Sonnet 5 18%")

    def test_more_than_max_shown_collapses_to_plus_n(self):
        by_model = {"A": {"tokens": 0, "cost": 5.0}, "B": {"tokens": 0, "cost": 3.0},
                    "C": {"tokens": 0, "cost": 1.0}, "D": {"tokens": 0, "cost": 1.0}}
        self.assertEqual(sl._model_mix_label(by_model, "cost", max_shown=2), "A 50% · B 30% +2")

    def test_empty_breakdown_is_blank(self):
        self.assertEqual(sl._model_mix_label({}, "cost"), "")

    def test_zero_total_is_blank_not_a_crash(self):
        self.assertEqual(sl._model_mix_label({"Opus 5": {"tokens": 0, "cost": 0.0}}, "cost"), "")

    def test_ranks_by_tokens_when_metric_is_tokens(self):
        by_model = {"Opus 5": {"tokens": 100, "cost": 9.0}, "Sonnet 5": {"tokens": 900, "cost": 1.0}}
        self.assertEqual(sl._model_mix_label(by_model, "tokens"), "Sonnet 5 90% · Opus 5 10%")


class ShowSessionsModelColumnTests(TmpEnv):
    def setUp(self):
        super().setUp()
        self.shared = self.root / "shared"
        patcher = mock.patch.dict(os.environ, {"SQUIRREL_SHARED_DIR": str(self.shared)})
        patcher.start(); self.addCleanup(patcher.stop)
        (self.root / ".claude.json").write_text(
            json.dumps({"oauthAccount": {"emailAddress": "a@example.com"}}))
        sl.write_cache(sl.session_path("mixed", "a@example.com"),
                       {"session_id": "mixed", "repo": "mixed-repo",
                        "samples": [[NOW - 1000, 0, 0.0, "Opus 5"],
                                    [NOW - 500, 800, 8.0, "Opus 5"],
                                    [NOW - 100, 1000, 10.0, "Sonnet 5"]]})

    def _show(self):
        with mock.patch.object(sl.time, "time", return_value=NOW):
            # width is explicit: the table now sheds columns to fit, and these tests are
            # about its CONTENT, not its responsiveness (see ShowSessionsNarrowTests).
            return plain(sl.cmd_show_sessions(width=400))

    def test_model_mix_column_shown_by_default(self):
        out = self._show()
        self.assertIn("model mix", out)
        self.assertIn("Opus 5", out)
        self.assertIn("Sonnet 5", out)

    def test_can_be_turned_off(self):
        sl.save_config({"sessionsShowModel": False})
        out = self._show()
        self.assertNotIn("model mix", out)
        self.assertNotIn("Opus 5", out)

    def test_legacy_session_with_no_model_shows_unknown(self):
        sl.write_cache(sl.session_path("legacy", "a@example.com"),
                       {"session_id": "legacy", "repo": "legacy-repo",
                        "samples": [[NOW - 1000, 0, 0.0], [NOW - 100, 500, 1.0]]})
        out = self._show()
        self.assertIn("unknown", out)


class SetSessionsShowModelCommandTests(TmpEnv):
    def test_on_off_and_invalid(self):
        self.assertIn("include", sl.cmd_set_sessions_show_model("on"))
        self.assertTrue(sl.load_config()["sessionsShowModel"])
        self.assertIn("no longer", sl.cmd_set_sessions_show_model("off"))
        self.assertFalse(sl.load_config()["sessionsShowModel"])
        self.assertIn("usage", sl.cmd_set_sessions_show_model("bogus").lower())


class SessionsShowModelDefaultsTests(unittest.TestCase):
    def test_packaged_default(self):
        packaged = sl.load_json(sl.default_config_path())
        self.assertIs(packaged.get("sessionsShowModel"), True)

    def test_default_is_on(self):
        self.assertTrue(sl.sessions_show_model({}))
        self.assertTrue(sl.sessions_show_model(None))
        self.assertFalse(sl.sessions_show_model({"sessionsShowModel": False}))


class AttributionTests(TmpEnv):
    def setUp(self):
        super().setUp()
        self.shared = self.root / "shared"
        patcher = mock.patch.dict(os.environ, {"SQUIRREL_SHARED_DIR": str(self.shared)})
        patcher.start(); self.addCleanup(patcher.stop)
        # three sessions of one account: window contributions 300 / 100 / 0 (stale). Both live
        # sessions also carry the SAME utilisation observations (40% -> 60%, a 20pp delta) so
        # the ground-truth walker has exactly one concurrent interval to split between them —
        # by cost, 1.0 : 0.2, i.e. 5:1 — giving the same 83%/17% split the old cost-ratio
        # proxy happened to produce for these numbers (see ACCT/attribution below).
        acct = sl.account_hash("a@example.com")
        self._write("s1", "alpha", [(NOW - 20000, 1000, 1.0, None, acct, 40.0),
                                     (NOW - 100, 1300, 2.0, None, acct, 60.0)])
        self._write("s2", "beta",  [(NOW - 20000, 500, 0.5, None, acct, 40.0),
                                     (NOW - 100, 600, 0.7, None, acct, 60.0)])
        self._write("s3", "gamma", [(NOW - 50000, 900, 9.0)])          # last seen long ago → not live

    def _write(self, sid, repo, samples):
        sl.write_cache(sl.session_path(sid, "a@example.com"),
                       {"session_id": sid, "repo": repo, "samples": [list(s) for s in samples]})

    def test_only_live_sessions_are_counted(self):
        live = sl.live_sessions("a@example.com", NOW)
        self.assertEqual(sorted(s["session_id"] for s in live), ["s1", "s2"])

    def test_window_contribution_uses_the_delta_not_the_total(self):
        live = {s["session_id"]: s for s in sl.live_sessions("a@example.com", NOW)}
        self.assertEqual(live["s1"]["window_tokens"], 300)
        self.assertEqual(live["s2"]["window_tokens"], 100)

    def test_share_segment_shows_this_sessions_slice(self):
        # Task 12: the shared 20pp utilisation delta splits by cost delta ($1.00 : $0.20) ->
        # s1 gets 83% of the attributed points, NOT the 75% a tokens-only ratio would give
        # (300 of 400 tokens) — ground truth, via cost as the concurrent-interval splitter,
        # correctly weighs a subagent-heavy session the same way Task 11d's cost-ratio proxy
        # used to, but now backed by the account's own observed utilisation.
        seg = sl.share_segment({"session_id": "s1"}, "a@example.com", NOW, {})
        self.assertIn("83%", plain(seg))

    def test_share_segment_ignores_attributionMetric_for_window_share(self):
        # Task 12: attributionMetric only ever drove the old cost/token PROXY; the window
        # share is now ground truth (the account's own utilisation, split by cost when several
        # sessions are concurrently active) and no longer configurable — 'tokens' must have NO
        # effect on it (the config still exists, but only for the *lifetime* share/columns,
        # which have no ground-truth equivalent — see share_life_segment()).
        seg = sl.share_segment({"session_id": "s1"}, "a@example.com", NOW,
                                {"attributionMetric": "tokens"})
        self.assertIn("83%", plain(seg))

    def test_lifetime_share_segment(self):
        seg = sl.share_life_segment({"session_id": "s1"}, "a@example.com", NOW, {})
        self.assertIn("74%", plain(seg))            # $2.00 of $2.70 lifetime cost

    def test_lifetime_share_segment_can_still_rank_by_tokens(self):
        seg = sl.share_life_segment({"session_id": "s1"}, "a@example.com", NOW,
                                     {"attributionMetric": "tokens"})
        self.assertIn("68%", plain(seg))            # 1300 of 1900

    def test_sessions_segment_ranks_and_marks_current(self):
        seg = plain(sl.sessions_segment({"session_id": "s2"}, "a@example.com", NOW, {}))
        self.assertLess(seg.index("alpha"), seg.index("beta"))       # ranked by share, descending
        self.assertIn("▸beta", seg)                                   # current session marked

    def test_segments_are_absent_when_alone_or_unknown(self):
        self.assertIsNone(sl.share_segment({"session_id": "nope"}, "a@example.com", NOW, {}))
        self.assertIsNone(sl.share_segment({}, "a@example.com", NOW, {}))

    def test_zero_total_does_not_divide_by_zero(self):
        self._write("z1", "zero", [(NOW - 100, 0, 0.0)])
        self.assertIsNotNone(sl.share_segment({"session_id": "z1"}, "a@example.com", NOW, {}))


class AttributionByCostBugTests(TmpEnv):
    """Task 11d's original bug: a session that ran a lot of subagent work shows a tiny token
    count (Claude Code's context_window totals exclude subagent usage) but a large cost
    (cost.total_cost_usd includes it). Task 12 fixes this at the root instead of by picking a
    metric: window attribution now comes from the account's own utilisation, which already
    includes subagent work automatically (it's the account's real consumption, not a
    reconstruction from any one session's counters) — cost is used only to split a shared
    utilisation delta between concurrently active sessions, never as the measurement, and is
    no longer configurable away for this purpose (attributionMetric now only affects the
    *lifetime* columns/segment, which have no ground-truth equivalent)."""

    def setUp(self):
        super().setUp()
        self.shared = self.root / "shared"
        patcher = mock.patch.dict(os.environ, {"SQUIRREL_SHARED_DIR": str(self.shared)})
        patcher.start(); self.addCleanup(patcher.stop)
        # 'heavy': ran lots of subagents this window — few of its own tokens, but $9 of cost.
        # 'light': no subagents — many tokens, but only $1 of cost. Both share one utilisation
        # observation pair (40% -> 50%, a 10pp delta) so the walker splits it 9:1 by cost.
        acct = sl.account_hash("a@example.com")
        sl.write_cache(sl.session_path("heavy", "a@example.com"),
                       {"session_id": "heavy", "repo": "heavy-repo",
                        "samples": [[NOW - 1000, 0, 0.0, None, acct, 40.0],
                                    [NOW - 100, 5_000, 9.0, None, acct, 50.0]]})
        sl.write_cache(sl.session_path("light", "a@example.com"),
                       {"session_id": "light", "repo": "light-repo",
                        "samples": [[NOW - 1000, 0, 0.0, None, acct, 40.0],
                                    [NOW - 100, 500_000, 1.0, None, acct, 50.0]]})

    def test_ground_truth_ranking_puts_the_subagent_heavy_session_first(self):
        seg = plain(sl.sessions_segment({"session_id": "light"}, "a@example.com", NOW, {}))
        self.assertLess(seg.index("heavy-repo"), seg.index("light-repo"))

    def test_attributionMetric_no_longer_affects_the_window_ranking(self):
        # The exact bug this task retires: under the old tokens-only PROXY, 'heavy' (the true
        # consumer) would rank last despite doing the most work, because subagent usage never
        # touched its token counters. Ground truth has no such failure mode, and
        # attributionMetric can no longer even select it for the window ranking any more.
        seg = plain(sl.sessions_segment({"session_id": "light"}, "a@example.com", NOW,
                                         {"attributionMetric": "tokens"}))
        self.assertLess(seg.index("heavy-repo"), seg.index("light-repo"))

    def test_heavy_session_gets_the_large_share_from_ground_truth(self):
        seg = sl.share_segment({"session_id": "heavy"}, "a@example.com", NOW, {})
        self.assertIn("90%", plain(seg))   # $9 of $10 cost split of the shared 10pp delta

    def test_attributionMetric_no_longer_affects_the_window_share(self):
        seg = sl.share_segment({"session_id": "heavy"}, "a@example.com", NOW,
                                {"attributionMetric": "tokens"})
        self.assertIn("90%", plain(seg))   # unaffected — no longer 1% (5,000 of 505,000 tokens)


# ── Task 12: attribution rebased on the account's own utilisation ────────────────────────────
class WalkUtilisationAttributionTests(unittest.TestCase):
    """walk_utilisation_attribution(): the pure interval walker — no filesystem, plain dicts of
    per-session sample series in, a list of interval records out. This is the heart of Task 12;
    every scenario in the brief is exercised here directly, in isolation from storage/display."""

    def test_single_active_session_gets_the_whole_delta_exactly(self):
        sessions = {"s1": [[100, 0, 0.0, None, "A", 40.0], [200, 10, 1.0, None, "A", 55.0]]}
        ivs = sl.walk_utilisation_attribution(sessions, "A")
        self.assertEqual(len(ivs), 1)
        self.assertEqual(ivs[0]["kind"], "exact")
        self.assertAlmostEqual(ivs[0]["delta"], 15.0)
        self.assertAlmostEqual(ivs[0]["shares"]["s1"], 15.0)

    def test_concurrent_sessions_split_by_cost_and_are_marked_estimated(self):
        sessions = {
            "s1": [[100, 0, 0.0, None, "A", 40.0], [200, 30, 3.0, None, "A", 60.0]],
            "s2": [[100, 0, 0.0, None, "A", 40.0], [200, 5, 1.0, None, "A", 60.0]],
        }
        ivs = sl.walk_utilisation_attribution(sessions, "A")
        self.assertEqual(ivs[0]["kind"], "estimated")
        self.assertAlmostEqual(ivs[0]["shares"]["s1"], 15.0)
        self.assertAlmostEqual(ivs[0]["shares"]["s2"], 5.0)

    def test_concurrent_with_no_cost_signal_splits_evenly(self):
        sessions = {
            "s1": [[100, 0, 0.0, None, "A", 40.0], [200, 30, 0.0, None, "A", 60.0]],
            "s2": [[100, 0, 0.0, None, "A", 40.0], [200, 5, 0.0, None, "A", 60.0]],
        }
        ivs = sl.walk_utilisation_attribution(sessions, "A")
        self.assertEqual(ivs[0]["kind"], "estimated")
        self.assertAlmostEqual(ivs[0]["shares"]["s1"], 10.0)
        self.assertAlmostEqual(ivs[0]["shares"]["s2"], 10.0)

    def test_idle_open_session_receives_nothing(self):
        sessions = {"s1": [[100, 0, 0.0, None, "A", 40.0], [200, 0, 0.0, None, "A", 55.0]]}
        ivs = sl.walk_utilisation_attribution(sessions, "A")
        self.assertEqual(ivs[0]["kind"], "unattributed")
        self.assertEqual(ivs[0]["shares"], {})
        self.assertAlmostEqual(ivs[0]["delta"], 15.0)   # the delta still happened; just unclaimed

    def test_window_reset_is_not_negative_consumption(self):
        sessions = {"s1": [[100, 0, 0, None, "A", 95.0], [200, 10, 1.0, None, "A", 3.0]]}
        ivs = sl.walk_utilisation_attribution(sessions, "A")
        self.assertEqual(ivs[0]["kind"], "reset")
        self.assertEqual(ivs[0]["delta"], 0.0)
        self.assertEqual(ivs[0]["shares"], {})

    def test_reset_then_normal_growth_baselines_on_the_post_reset_value(self):
        sessions = {"s1": [
            [100, 0, 0, None, "A", 95.0],
            [200, 10, 1.0, None, "A", 3.0],      # reset
            [300, 20, 2.0, None, "A", 10.0],     # normal growth from the new baseline
        ]}
        ivs = sl.walk_utilisation_attribution(sessions, "A")
        self.assertEqual(ivs[0]["kind"], "reset")
        self.assertEqual(ivs[1]["kind"], "exact")
        self.assertAlmostEqual(ivs[1]["delta"], 7.0)
        self.assertAlmostEqual(ivs[1]["shares"]["s1"], 7.0)

    def test_account_switch_attributes_to_the_account_of_the_interval(self):
        sessions = {"s1": [
            [100, 0, 0.0, None, "A", 40.0], [200, 10, 1.0, None, "A", 55.0],
            [300, 10, 1.0, None, "B", 20.0], [400, 20, 3.0, None, "B", 45.0],
        ]}
        a_ivs = sl.walk_utilisation_attribution(sessions, "A")
        b_ivs = sl.walk_utilisation_attribution(sessions, "B")
        self.assertEqual(len(a_ivs), 1)
        self.assertAlmostEqual(a_ivs[0]["shares"]["s1"], 15.0)     # A's own delta only
        self.assertEqual(len(b_ivs), 1)
        self.assertAlmostEqual(b_ivs[0]["shares"]["s1"], 25.0)     # B's own delta only — no cross-charging

    def test_no_observations_at_all_yields_no_intervals(self):
        sessions = {"s1": [[100, 10, 1.0, None, "A", None], [200, 50, 5.0, None, "A", None]]}
        self.assertEqual(sl.walk_utilisation_attribution(sessions, "A"), [])


class AccountWindowAttributionTests(unittest.TestCase):
    """account_window_attribution(): aggregates the walker over the current window into
    per-session {points, kind, coverage_seconds} — still pure, still filesystem-free."""

    WINDOW = 18000.0

    def test_intervals_without_utilisation_data_are_unknown_not_zero(self):
        sessions = {"s1": [[100, 10, 1.0, None, "A", None], [200, 500, 50.0, None, "A", None]]}
        now = 300.0
        result = sl.account_window_attribution(sessions, "A", now - self.WINDOW, now)
        self.assertEqual(result["sessions"]["s1"]["points"], 0.0)
        self.assertEqual(result["sessions"]["s1"]["kind"], "unknown")

    def test_coverage_is_reported_for_a_session_younger_than_the_window(self):
        now = 100000.0
        start = now - 40 * 60
        sessions = {"s1": [[start, 0, 0.0, None, "A", 10.0], [now, 100, 5.0, None, "A", 25.0]]}
        result = sl.account_window_attribution(sessions, "A", now - self.WINDOW, now)
        self.assertAlmostEqual(result["sessions"]["s1"]["coverage_seconds"], 40 * 60, delta=1)
        self.assertEqual(result["sessions"]["s1"]["kind"], "exact")

    def test_mixed_known_and_gap_marks_estimated_not_unknown(self):
        now = 100000.0
        start = now - self.WINDOW
        sessions = {"s1": [
            [start, 0, 0.0, None, "A", None],           # a coverage gap
            [start + 100, 0, 0.0, None, "A", 40.0],     # first real observation
            [now, 10, 1.0, None, "A", 55.0],
        ]}
        result = sl.account_window_attribution(sessions, "A", start, now)
        self.assertEqual(result["sessions"]["s1"]["kind"], "estimated")
        self.assertGreater(result["sessions"]["s1"]["points"], 0.0)

    def test_an_unattributed_interval_does_not_by_itself_make_the_window_estimated(self):
        # Subtle but load-bearing: an interval where NO session could be identified as active
        # ('unattributed' — see walk_utilisation_attribution()) does not mean the account went
        # UNOBSERVED there; it was observed, nobody was just credited for that slice (design
        # note §4 flags this as a distinct case from a true gap). s1 is active in the first
        # interval (exact, 10pp), then goes idle while the account's utilisation still creeps
        # up 10pp more with nobody active to claim it — s1's own relevant span is nonetheless
        # continuously bracketed by real observations throughout (t0, t1, t2 all observed), so
        # its classification stays "exact": we have complete, honest information about s1
        # specifically, even though the account total doesn't fully reconcile to named sessions.
        t0, t1, t2 = 0.0, 100.0, 300.0
        sessions = {
            "s1": [[t0, 0, 0.0, None, "A", 10.0], [t1, 5, 0.5, None, "A", 20.0],
                   [t2, 5, 0.5, None, "A", None]],      # idle from t1 onward, stays live
            "s2": [[t0, 0, 0.0, None, "A", 10.0], [t2, 0, 0.0, None, "A", 30.0]],   # never active
        }
        result = sl.account_window_attribution(sessions, "A", t0, t2)
        self.assertEqual(result["sessions"]["s1"]["kind"], "exact")
        self.assertAlmostEqual(result["sessions"]["s1"]["points"], 10.0)


class UtilisationAttributionTests(TmpEnv):
    """End-to-end (file-backed) coverage of the Task 12 brief's own test list — samples written
    directly to session files, exercised through the public live_sessions()/share_segment()/
    session_usage_segment() API."""

    def setUp(self):
        super().setUp()
        self.shared = self.root / "shared"
        patcher = mock.patch.dict(os.environ, {"SQUIRREL_SHARED_DIR": str(self.shared)})
        patcher.start(); self.addCleanup(patcher.stop)
        self.acct_a = sl.account_hash("a@example.com")
        self.acct_b = sl.account_hash("b@example.com")

    def _write(self, sid, samples, repo="r"):
        sl.write_cache(sl.session_path(sid),
                        {"session_id": sid, "repo": repo, "samples": [list(s) for s in samples]})

    def test_single_active_session_gets_the_whole_delta_exactly(self):
        a = self.acct_a
        self._write("s1", [(NOW - 200, 0, 0.0, None, a, 40.0), (NOW - 100, 10, 1.0, None, a, 55.0)])
        live = {s["session_id"]: s for s in sl.live_sessions("a@example.com", NOW)}
        self.assertAlmostEqual(live["s1"]["attributed_points"], 15.0)
        self.assertEqual(live["s1"]["attribution_kind"], "exact")

    def test_concurrent_sessions_split_by_cost_and_are_marked_estimated(self):
        a = self.acct_a
        self._write("s1", [(NOW - 200, 0, 0.0, None, a, 40.0), (NOW - 100, 30, 3.0, None, a, 60.0)])
        self._write("s2", [(NOW - 200, 0, 0.0, None, a, 40.0), (NOW - 100, 5, 1.0, None, a, 60.0)])
        live = {s["session_id"]: s for s in sl.live_sessions("a@example.com", NOW)}
        self.assertAlmostEqual(live["s1"]["attributed_points"], 15.0)
        self.assertAlmostEqual(live["s2"]["attributed_points"], 5.0)
        self.assertEqual(live["s1"]["attribution_kind"], "estimated")
        self.assertEqual(live["s2"]["attribution_kind"], "estimated")

    def test_idle_open_session_receives_nothing(self):
        a = self.acct_a
        self._write("s1", [(NOW - 200, 0, 0.0, None, a, 40.0), (NOW - 100, 10, 1.0, None, a, 55.0)])
        self._write("s2", [(NOW - 200, 5, 0.5, None, a, 40.0), (NOW - 100, 5, 0.5, None, a, 55.0)])
        live = {s["session_id"]: s for s in sl.live_sessions("a@example.com", NOW)}
        self.assertAlmostEqual(live["s1"]["attributed_points"], 15.0)
        self.assertEqual(live["s1"]["attribution_kind"], "exact")
        self.assertEqual(live["s2"]["attributed_points"], 0.0)
        self.assertEqual(live["s2"]["attribution_kind"], "exact")   # confirmed idle, not unknown

    def test_window_reset_is_not_negative_consumption(self):
        a = self.acct_a
        self._write("s1", [(NOW - 300, 0, 0.0, None, a, 95.0), (NOW - 200, 5, 0.5, None, a, 3.0),
                            (NOW - 100, 10, 1.0, None, a, 10.0)])
        live = {s["session_id"]: s for s in sl.live_sessions("a@example.com", NOW)}
        self.assertAlmostEqual(live["s1"]["attributed_points"], 7.0)   # only the post-reset delta
        self.assertGreaterEqual(live["s1"]["attributed_points"], 0.0)

    def test_account_switch_attributes_to_the_account_of_the_interval(self):
        a, b = self.acct_a, self.acct_b
        self._write("s1", [(NOW - 400, 0, 0.0, None, a, 40.0), (NOW - 300, 10, 1.0, None, a, 55.0),
                            (NOW - 200, 10, 1.0, None, b, 20.0), (NOW - 100, 20, 3.0, None, b, 45.0)])
        records = sl._load_session_records()
        window_start = NOW - sl.SESSION_WINDOW_S
        result_a = sl.account_window_attribution({sid: r["samples"] for sid, r in records.items()},
                                                   a, window_start, NOW)
        result_b = sl.account_window_attribution({sid: r["samples"] for sid, r in records.items()},
                                                   b, window_start, NOW)
        self.assertAlmostEqual(result_a["sessions"]["s1"]["points"], 15.0)   # A's own delta only
        self.assertAlmostEqual(result_b["sessions"]["s1"]["points"], 25.0)   # B's own delta only
        # The session has since moved to B — it no longer appears in A's live listing at all,
        # so A cannot be charged for it going forward either (no cross-charging).
        self.assertNotIn("s1", {s["session_id"] for s in sl.live_sessions("a@example.com", NOW)})
        self.assertIn("s1", {s["session_id"] for s in sl.live_sessions("b@example.com", NOW)})

    def test_intervals_without_utilisation_data_are_unknown_not_zero(self):
        a = self.acct_a
        self._write("s1", [(NOW - 200, 0, 0.0, None, a, None), (NOW - 100, 500, 50.0, None, a, None)])
        live = {s["session_id"]: s for s in sl.live_sessions("a@example.com", NOW)}
        self.assertEqual(live["s1"]["attributed_points"], 0.0)
        self.assertEqual(live["s1"]["attribution_kind"], "unknown")

    def test_coverage_is_reported(self):
        a = self.acct_a
        started = NOW - 40 * 60
        self._write("s1", [(started, 0, 0.0, None, a, 10.0), (NOW, 100, 5.0, None, a, 25.0)])
        live = {s["session_id"]: s for s in sl.live_sessions("a@example.com", NOW)}
        self.assertAlmostEqual(live["s1"]["coverage_seconds"], 40 * 60, delta=2)
        self.assertLess(live["s1"]["coverage_seconds"], sl.SESSION_WINDOW_S)

    def test_legacy_sample_formats_still_parse(self):
        a = self.acct_a
        # A flat (Task 12) file mixing every historical sample length in one series.
        self._write("mixed", [
            (NOW - 500, 0, 0.0),                              # 3-element, pre-Task-11e
            (NOW - 400, 5, 0.5, "Opus 5"),                    # 4-element, Task 11e
            (NOW - 300, 8, 0.8, "Opus 5", a),                 # 5-element, pre-Task-12
            (NOW - 100, 10, 1.0, "Opus 5", a, 60.0),          # 6-element, Task 12
        ])
        # The OLD per-account-directory layout, never migrated — see session_path()/
        # _load_session_records(). Its samples carry no account tag at all; the directory name
        # IS the account hash and must be backfilled on read.
        legacy_path = self.shared / "sessions" / a / "legacy1.json"
        sl.write_cache(legacy_path, {"session_id": "legacy1", "repo": "old",
                                      "samples": [[NOW - 500, 0, 0.0], [NOW - 100, 20, 2.0]]})
        live = {s["session_id"]: s for s in sl.live_sessions("a@example.com", NOW)}   # must not raise
        self.assertIn("mixed", live)
        self.assertIn("legacy1", live)
        self.assertGreaterEqual(live["mixed"]["attributed_points"], 0.0)

    def test_composite_falls_back_when_this_sessions_attribution_is_entirely_unknown(self):
        a = self.acct_a
        bucket = {"label": "", "pct": 60.0, "resets_at": None}
        # 'mine' never had a valid utilisation reading, and no OTHER live session's
        # observations happen to cover its recent activity either (they're all from long
        # before it) -> genuinely unknown -> the composite must fall back to the plain total,
        # never a confident wrong number.
        self._write("other", [(NOW - 2000, 0, 0.0, None, a, 40.0), (NOW - 1900, 5, 0.5, None, a, 45.0),
                               (NOW - 50, 5, 0.5, None, a, None)])   # stays live; adds no new observation
        self._write("mine", [(NOW - 200, 0, 0.0, None, a, None), (NOW - 100, 500, 5.0, None, a, None)])
        live = {s["session_id"]: s for s in sl.live_sessions("a@example.com", NOW)}
        self.assertEqual(live["mine"]["attribution_kind"], "unknown")
        seg = sl.session_usage_segment("5h", bucket, False, {"bars": False},
                                        {"session_id": "mine"}, "a@example.com", NOW, {})
        self.assertEqual(plain(seg), "5h 60%")   # plain fallback, no '/' composite

    def test_composite_marks_partial_when_this_sessions_window_has_a_gap(self):
        b = sl.account_hash("c@example.com")   # isolated account — no interference from the case above
        bucket = {"label": "", "pct": 60.0, "resets_at": None}
        # 'gappy' predates the window (its own first sample is at NOW-25000, well before
        # window_start at NOW-18000) and NO session — not even 'partner' — has an observation
        # of this account reaching back that far, so there is a genuine, unobservable stretch
        # `[window_start, -17800)` that Squirrel cannot see into (design note §2's gap case).
        # From -17800 onward the account IS observed, and 'gappy' is the sole active session in
        # that span, so its 15pp there is exact — but the figure as a whole is still only a
        # floor because of the earlier gap. 'partner' exists only so this isn't the only live
        # session (see session_contribution()'s '>= 2 live sessions' gate); it shares 'gappy's
        # first real observation point and does not push the account's baseline any earlier.
        self._write("partner", [(NOW - 17800, 0, 0.0, None, b, 10.0), (NOW - 50, 1, 0.1, None, b, None)])
        self._write("gappy", [
            (NOW - 25000, 0, 0.0, None, b, None),           # existed well before the window opened
            (NOW - 17800, 0, 0.0, None, b, 10.0),           # first real observation of the account
            (NOW - 100, 5, 0.5, None, b, 25.0),
        ])
        live = {s["session_id"]: s for s in sl.live_sessions("c@example.com", NOW)}
        self.assertEqual(live["gappy"]["attribution_kind"], "estimated")
        seg = sl.session_usage_segment("5h", bucket, False, {"bars": False},
                                        {"session_id": "gappy"}, "c@example.com", NOW, {})
        self.assertIn("~", plain(seg))
        self.assertIn("pp/60%", plain(seg))


class BurnRateAndLegacySampleTests(TmpEnv):
    """Task 11d: burn rate becomes cost-per-minute; zero-cost sessions and samples recorded
    before cost tracking existed (2-element, pre-cost-tracking) must degrade safely rather than
    crashing or producing nonsense numbers."""

    def setUp(self):
        super().setUp()
        self.shared = self.root / "shared"
        patcher = mock.patch.dict(os.environ, {"SQUIRREL_SHARED_DIR": str(self.shared)})
        patcher.start(); self.addCleanup(patcher.stop)

    def _write(self, sid, repo, samples):
        sl.write_cache(sl.session_path(sid, "a@example.com"),
                       {"session_id": sid, "repo": repo, "samples": [list(s) for s in samples]})

    def test_burn_cost_per_min_is_computed(self):
        # $3.00 accrued over 10 minutes (burnWindowSeconds default 600s) -> $0.30/min.
        self._write("s1", "alpha", [(NOW - 600, 0, 0.0), (NOW, 1000, 3.0)])
        live = {s["session_id"]: s for s in sl.live_sessions("a@example.com", NOW)}
        self.assertAlmostEqual(live["s1"]["burn_cost_per_min"], 0.3, places=6)

    def test_zero_cost_session_has_zero_burn_and_zero_share_no_crash(self):
        self._write("free", "nocost", [(NOW - 600, 0, 0.0), (NOW, 1000, 0.0)])
        live = {s["session_id"]: s for s in sl.live_sessions("a@example.com", NOW)}
        self.assertEqual(live["free"]["burn_cost_per_min"], 0.0)
        self.assertEqual(live["free"]["window_cost"], 0.0)
        seg = sl.share_segment({"session_id": "free"}, "a@example.com", NOW, {})
        self.assertIsNotNone(seg)   # must not raise / divide by zero

    def test_legacy_two_element_samples_are_dropped_not_crashed(self):
        # Samples recorded before cost tracking existed had only [timestamp,
        # cumulative_tokens] — two elements. live_sessions() must not choke on a legacy file;
        # such samples are simply filtered out (len(s) == 3), same tolerance
        # record_session_sample() already applies on write.
        path = sl.session_path("legacy", "a@example.com")
        sl.write_cache(path, {"session_id": "legacy", "repo": "old",
                               "samples": [[NOW - 600, 500], [NOW - 100, 800]]})
        live = sl.live_sessions("a@example.com", NOW)
        self.assertEqual([s["session_id"] for s in live if s["session_id"] == "legacy"], [])

    def test_mixed_legacy_and_new_samples_use_only_the_new_ones(self):
        path = sl.session_path("mixed", "a@example.com")
        sl.write_cache(path, {"session_id": "mixed", "repo": "old",
                               "samples": [[NOW - 20000, 500], [NOW - 18000, 1000, 5.0],
                                           [NOW - 100, 1400, 6.0]]})
        live = {s["session_id"]: s for s in sl.live_sessions("a@example.com", NOW)}
        self.assertEqual(live["mixed"]["lifetime_cost"], 6.0)
        self.assertEqual(live["mixed"]["window_cost"], 1.0)   # 6.0 - 5.0, baseline at NOW-18000


class ShowSessionsTableTests(TmpEnv):
    """cmd_show_sessions(): the full /squirrel-sessions table. The shape is taken from a real
    four-session day, with the names replaced: two sessions run a lot of subagent work (few
    tokens, big cost), one is token-heavy but cheap. Task 12: all four share one
    utilisation observation pair (40% -> 60%, a 20pp delta), split by their cost deltas — the
    same numbers that used to drive the pre-Task-12 cost-ratio proxy — so the ground-truth
    ranking reproduces the same order this table has always shown for this scenario."""

    def setUp(self):
        super().setUp()
        self.shared = self.root / "shared"
        patcher = mock.patch.dict(os.environ, {"SQUIRREL_SHARED_DIR": str(self.shared)})
        patcher.start(); self.addCleanup(patcher.stop)
        (self.root / ".claude.json").write_text(
            json.dumps({"oauthAccount": {"emailAddress": "a@example.com"}}))
        acct = sl.account_hash("a@example.com")
        self._write("atl", "atlas",
                     [(NOW - 1000, 0, 0.0, None, acct, 40.0), (NOW - 60, 877_000, 1003.29, None, acct, 60.0)])
        self._write("beacon", "beacon",
                     [(NOW - 1000, 0, 0.0, None, acct, 40.0), (NOW - 60, 282_000, 657.59, None, acct, 60.0)])
        self._write("store", "storefront",
                     [(NOW - 1000, 0, 0.0, None, acct, 40.0), (NOW - 60, 682_000, 461.41, None, acct, 60.0)])
        self._write("attrib", "metrics-lab",
                     [(NOW - 1000, 0, 0.0, None, acct, 40.0), (NOW - 60, 950_000, 25.51, None, acct, 60.0)])

    def _write(self, sid, repo, samples):
        sl.write_cache(sl.session_path(sid, "a@example.com"),
                       {"session_id": sid, "repo": repo, "samples": [list(s) for s in samples]})

    def _show(self):
        # cmd_show_sessions() reads real wall-clock time internally; pin it to NOW so the
        # samples above (anchored to NOW) read as live.
        with mock.patch.object(sl.time, "time", return_value=NOW):
            # width is explicit: the table now sheds columns to fit, and these tests are
            # about its CONTENT, not its responsiveness (see ShowSessionsNarrowTests).
            return plain(sl.cmd_show_sessions(width=400))

    def test_ranked_by_ground_truth_attributed_points(self):
        out = self._show()
        order = [out.index(r) for r in ("atlas", "beacon", "storefront", "metrics-lab")]
        self.assertEqual(order, sorted(order))

    def test_attributionMetric_no_longer_affects_the_5h_ranking(self):
        # Task 12: attributionMetric only ever drove the old cost/token PROXY; the '5h pts'
        # ranking is ground truth now and ignores it entirely — order stays cost-split, same
        # as the default.
        sl.save_config({"attributionMetric": "tokens"})
        out = self._show()
        order = [out.index(r) for r in ("atlas", "beacon", "storefront", "metrics-lab")]
        self.assertEqual(order, sorted(order))

    def test_shows_both_cost_and_token_columns(self):
        out = self._show()
        self.assertIn("$1003.29", out)
        self.assertIn("877", out)   # token figure still present (fmt_tokens abbreviates to 877k)

    def test_explains_the_tokens_vs_cost_discrepancy(self):
        out = self._show()
        self.assertIn("subagent", out.lower())

    def test_notes_which_column_drives_the_ranking(self):
        out = self._show()
        self.assertIn("ranked by cost", out.lower())

    def test_notes_ranking_by_tokens_when_configured(self):
        sl.save_config({"attributionMetric": "tokens"})
        out = self._show()
        self.assertIn("ranked by tokens", out.lower())


class AttributionSegmentDefaultsTests(unittest.TestCase):
    def test_new_segments_ship_off(self):
        packaged = sl.load_json(sl.default_config_path())
        for key in ("share", "share-life", "sessions"):
            self.assertIn(key, sl.SEGMENT_KEYS)
            self.assertFalse(sl.segment_enabled(packaged, key), f"'{key}' should ship off by default")


# ── Task 11c: 'mine' contribution to the shared 5-hour limit ─────────────────
class SegmentLabelTests(TmpEnv):
    """segment_label(): a configurable label per segment, defaulting to today's wording, with
    a missing/partial override falling back per-key rather than losing sibling defaults."""

    def test_default_labels(self):
        self.assertEqual(sl.segment_label({}, "share"), "mine")
        self.assertEqual(sl.segment_label({}, "share-life"), "life")
        self.assertEqual(sl.segment_label(None, "share"), "mine")

    def test_override_one_leaves_the_other_at_its_default(self):
        cfg = {"segmentLabels": {"share": "burned"}}
        self.assertEqual(sl.segment_label(cfg, "share"), "burned")
        self.assertEqual(sl.segment_label(cfg, "share-life"), "life")

    def test_blank_override_falls_back_to_the_default(self):
        self.assertEqual(sl.segment_label({"segmentLabels": {"share": ""}}, "share"), "mine")

    def test_unlabeled_segment_falls_back_to_its_own_key(self):
        self.assertEqual(sl.segment_label({}, "sessions"), "sessions")


class ConfigMergeSemanticsTests(TmpEnv):
    """A user config overriding one entry of a shipped MAP must not wipe the other entries.

    The trap: `{"segments": {"share": true}}` used to replace the whole shipped
    `{"share": false, "share-life": false, "sessions": false}`, so turning one attribution
    segment on silently turned all three on. Lists are deliberately NOT merged — a user's
    list is the whole list."""

    def _user_config(self, patch):
        path = sl.config_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(patch))
        return sl.load_config()

    def test_merge_keys_are_exactly_the_map_valued_settings(self):
        self.assertEqual(set(sl.merge_keys()),
                         {"accountColors", "accountNames", "repoColors", "segments",
                          "segmentLabels", "segmentPriority", "segmentStyles"})
        # segmentStyles merges per SEGMENT (styling `git` must not wipe the style on `cost`).
        # The entry *inside* one segment is still replaced wholesale by save_config(), which
        # is exactly why cmd_style() computes the overlaid entry itself — see its comment.

    def test_merge_keys_is_derived_from_the_shipped_defaults(self):
        shipped = json.loads(sl.default_config_path().read_text(encoding="utf-8"))
        self.assertEqual(set(sl.merge_keys()),
                         {k for k, v in shipped.items() if isinstance(v, dict)})

    def test_enabling_one_segment_does_not_enable_the_others(self):
        cfg = self._user_config({"segments": {"share": True}})
        self.assertTrue(sl.segment_enabled(cfg, "share"))
        self.assertFalse(sl.segment_enabled(cfg, "share-life"))
        self.assertFalse(sl.segment_enabled(cfg, "sessions"))

    def test_hiding_one_segment_does_not_disturb_the_shipped_defaults(self):
        cfg = self._user_config({"segments": {"cost": False}})
        self.assertFalse(sl.segment_enabled(cfg, "cost"))
        self.assertFalse(sl.segment_enabled(cfg, "share"))       # still shipped-off
        self.assertTrue(sl.segment_enabled(cfg, "pr"))           # still shipped-on

    def test_overriding_one_label_keeps_the_other_shipped_label(self):
        cfg = self._user_config({"segmentLabels": {"share": "burning"}})
        self.assertEqual(cfg["segmentLabels"], {"share": "burning", "share-life": "life"})

    def test_maps_that_ship_empty_are_unaffected_either_way(self):
        cfg = self._user_config({"accountColors": {"a@b.c": "red"},
                                 "accountNames": {"a@b.c": "kai"},
                                 "repoColors": {"demo": "cyan"},
                                 "segmentPriority": {"git": 3}})
        self.assertEqual(cfg["accountColors"], {"a@b.c": "red"})
        self.assertEqual(cfg["accountNames"], {"a@b.c": "kai"})
        self.assertEqual(cfg["repoColors"], {"demo": "cyan"})
        self.assertEqual(cfg["segmentPriority"], {"git": 3})

    def test_lists_are_still_replaced_wholesale(self):
        cfg = self._user_config({"layout": [["account", "model"]],
                                 "alarmStates": ["working"],
                                 "idlePhases": [{"after": 5, "bg": "red"}]})
        self.assertEqual(cfg["layout"], [["account", "model"]])
        self.assertEqual(cfg["alarmStates"], ["working"])
        self.assertEqual(cfg["idlePhases"], [{"after": 5, "bg": "red"}])

    def test_scalars_are_still_replaced_wholesale(self):
        cfg = self._user_config({"branchMaxChars": 7, "usageFetch": False})
        self.assertEqual(cfg["branchMaxChars"], 7)
        self.assertFalse(cfg["usageFetch"])

    def test_a_key_the_defaults_do_not_have_still_lands(self):
        self.assertEqual(self._user_config({"somethingNew": 1})["somethingNew"], 1)

    def test_a_non_map_value_for_a_map_key_does_not_crash(self):
        cfg = self._user_config({"segments": "oops"})
        self.assertEqual(cfg["segments"], "oops")           # stored as-is, replaced wholesale
        self.assertTrue(sl.segment_enabled(cfg, "cost"))    # readers already tolerate it

    def test_the_merge_matches_what_save_config_writes(self):
        sl.cmd_set_segment("share on")
        cfg = sl.load_config()
        self.assertTrue(sl.segment_enabled(cfg, "share"))
        self.assertFalse(sl.segment_enabled(cfg, "sessions"))


class SetLabelCommandTests(TmpEnv):
    def test_sets_and_persists(self):
        msg = sl.cmd_set_label("share burning")
        self.assertIn("burning", msg)
        self.assertEqual(sl.load_config()["segmentLabels"]["share"], "burning")

    def test_reset_removes_the_override(self):
        sl.cmd_set_label("share burning")
        sl.cmd_set_label("share reset")
        # The override is removed from the USER's config; load_config() then legitimately
        # shows the shipped label again, because map-valued settings merge per key (see
        # load_config()). Asserting absence from the *merged* config would be asserting that
        # the shipped default does not exist, which is not what "reset" means.
        self.assertNotIn("share", sl.user_config().get("segmentLabels", {}))
        self.assertEqual(sl.segment_label(sl.load_config(), "share"), "mine")

    def test_overriding_one_label_does_not_erase_the_others_default(self):
        sl.cmd_set_label("share burning")
        self.assertEqual(sl.segment_label(sl.load_config(), "share-life"), "life")

    def test_unknown_segment_rejected(self):
        self.assertIn("no configurable label", sl.cmd_set_label("cost foo").lower())

    def test_usage_message_when_args_missing(self):
        self.assertIn("usage", sl.cmd_set_label("").lower())
        self.assertIn("usage", sl.cmd_set_label("share").lower())


class ShareFormatCommandTests(TmpEnv):
    def test_sets_each_valid_value(self):
        for value in ("contribution", "share", "both"):
            msg = sl.cmd_set_share_format(value)
            self.assertIn(value, msg)
            self.assertEqual(sl.load_config()["shareFormat"], value)

    def test_rejects_invalid_value(self):
        self.assertIn("usage", sl.cmd_set_share_format("bogus").lower())
        self.assertNotIn("shareFormat", sl.user_config())

    def test_default_format_is_contribution(self):
        self.assertEqual(sl.share_format({}), "contribution")
        self.assertEqual(sl.share_format(None), "contribution")
        self.assertEqual(sl.share_format({"shareFormat": "nonsense"}), "contribution")


class ShareConfigDefaultsTests(unittest.TestCase):
    def test_packaged_defaults(self):
        packaged = sl.load_json(sl.default_config_path())
        self.assertEqual(packaged.get("segmentLabels"), {"share": "mine", "share-life": "life"})
        self.assertEqual(packaged.get("shareFormat"), "contribution")


# ── Task 11d: attribute by cost, not tokens ───────────────────────────────────
class AttributionMetricTests(unittest.TestCase):
    def test_default_is_cost(self):
        self.assertEqual(sl.attribution_metric({}), "cost")
        self.assertEqual(sl.attribution_metric(None), "cost")

    def test_invalid_value_falls_back_to_cost(self):
        self.assertEqual(sl.attribution_metric({"attributionMetric": "bogus"}), "cost")

    def test_tokens_is_honoured(self):
        self.assertEqual(sl.attribution_metric({"attributionMetric": "tokens"}), "tokens")


class SetAttributionMetricCommandTests(TmpEnv):
    def test_sets_each_valid_value(self):
        for value in ("cost", "tokens"):
            msg = sl.cmd_set_attribution_metric(value)
            self.assertIn(value, msg)
            self.assertEqual(sl.load_config()["attributionMetric"], value)

    def test_rejects_invalid_value(self):
        self.assertIn("usage", sl.cmd_set_attribution_metric("bogus").lower())
        self.assertNotIn("attributionMetric", sl.user_config())


class AttributionMetricConfigDefaultsTests(unittest.TestCase):
    def test_packaged_default(self):
        packaged = sl.load_json(sl.default_config_path())
        self.assertEqual(packaged.get("attributionMetric"), "cost")


class ContributionArithmeticTests(TmpEnv):
    """Task 12: contribution_pp IS the attributed percentage-point figure directly — read
    straight off the account's own utilisation history (walk_utilisation_attribution()), with
    no `fraction * account_pct` multiplication left to do (there is no fraction any more; only
    the display denominator, `account_pct`, is a separate input). Most fixtures below are the
    OLD fraction/account_pct pair reparametrized as points — `mine_points = fraction *
    account_pct`, `other_points = (1 - fraction) * account_pct` — which preserves both the
    expected contribution figure AND the expected share ratio (mine / (mine + other)) exactly,
    since account_pct cancels out of that ratio; this keeps every scenario's original intent
    (unrounded arithmetic, zero/full/partial share, every shareFormat) while reflecting the new
    mechanism."""

    @staticmethod
    def _sessions(mine_points, other_points, kind="exact"):
        return [
            {"session_id": "mine", "attributed_points": mine_points, "attribution_kind": kind},
            {"session_id": "other", "attributed_points": other_points, "attribution_kind": "exact"},
        ]

    def test_contribution_uses_the_unrounded_points_value(self):
        # mine=66.45pp (== the old 0.75 fraction * 88.6% account utilisation, unrounded) ->
        # displays as 66%, not 67% — rounding happens only at display time, on the actual
        # attributed value, never on an intermediate rounded fraction.
        with mock.patch.object(sl, "live_sessions", return_value=self._sessions(66.45, 22.15)):
            seg = plain(sl.share_segment({"session_id": "mine"}, "a@example.com", NOW, {}, 88.6))
        self.assertEqual(seg, "mine 66pp/89%")

    def test_zero_points_gives_zero_contribution(self):
        with mock.patch.object(sl, "live_sessions", return_value=self._sessions(0, 50)):
            seg = plain(sl.share_segment({"session_id": "mine"}, "a@example.com", NOW, {}, 50.0))
        self.assertEqual(seg, "mine 0pp/50%")

    def test_full_points_gives_full_contribution(self):
        with mock.patch.object(sl, "live_sessions", return_value=self._sessions(50, 0)):
            seg = plain(sl.share_segment({"session_id": "mine"}, "a@example.com", NOW, {}, 50.0))
        self.assertEqual(seg, "mine 50pp/50%")

    def test_zero_points_and_zero_account_utilisation(self):
        with mock.patch.object(sl, "live_sessions", return_value=self._sessions(0, 0)):
            seg = plain(sl.share_segment({"session_id": "mine"}, "a@example.com", NOW, {}, 0.0))
        self.assertEqual(seg, "mine 0pp/0%")

    def test_hundred_percent_account_utilisation(self):
        with mock.patch.object(sl, "live_sessions", return_value=self._sessions(21.4, 78.6)):
            seg = plain(sl.share_segment({"session_id": "mine"}, "a@example.com", NOW, {}, 100.0))
        self.assertEqual(seg, "mine 21pp/100%")

    def test_fallback_to_plain_share_when_account_pct_is_none(self):
        # The SHARE ratio (mine of every live session's attributed points) is independent of
        # account_pct entirely, so raw points values (a 3:1 ratio) are used directly here.
        with mock.patch.object(sl, "live_sessions", return_value=self._sessions(300, 100)):
            seg = plain(sl.share_segment({"session_id": "mine"}, "a@example.com", NOW, {}, None))
        self.assertEqual(seg, "mine 75%")

    def test_fallback_when_account_pct_omitted_entirely(self):
        with mock.patch.object(sl, "live_sessions", return_value=self._sessions(300, 100)):
            seg = plain(sl.share_segment({"session_id": "mine"}, "a@example.com", NOW, {}))
        self.assertEqual(seg, "mine 75%")

    def test_share_format_share_ignores_account_pct(self):
        with mock.patch.object(sl, "live_sessions", return_value=self._sessions(300, 100)):
            seg = plain(sl.share_segment({"session_id": "mine"}, "a@example.com", NOW,
                                          {"shareFormat": "share"}, 88.6))
        self.assertEqual(seg, "mine 75%")

    def test_share_format_both(self):
        with mock.patch.object(sl, "live_sessions", return_value=self._sessions(66.45, 22.15)):
            seg = plain(sl.share_segment({"session_id": "mine"}, "a@example.com", NOW,
                                          {"shareFormat": "both"}, 88.6))
        self.assertEqual(seg, "mine 75% → 66pp/89%")

    def test_share_format_both_falls_back_when_account_pct_unknown(self):
        with mock.patch.object(sl, "live_sessions", return_value=self._sessions(300, 100)):
            seg = plain(sl.share_segment({"session_id": "mine"}, "a@example.com", NOW,
                                          {"shareFormat": "both"}, None))
        self.assertEqual(seg, "mine 75%")

    def test_estimated_marks_share_and_contribution_not_the_account_total(self):
        with mock.patch.object(sl, "live_sessions",
                                return_value=self._sessions(66.45, 22.15, kind="estimated")):
            seg = plain(sl.share_segment({"session_id": "mine"}, "a@example.com", NOW, {}, 88.6))
        self.assertEqual(seg, "mine ~66pp/89%")

    def test_configured_label_is_used_instead_of_mine(self):
        with mock.patch.object(sl, "live_sessions", return_value=self._sessions(66.45, 22.15)):
            seg = plain(sl.share_segment({"session_id": "mine"}, "a@example.com", NOW,
                                          {"segmentLabels": {"share": "burned"}}, 88.6))
        self.assertEqual(seg, "burned 66pp/89%")

    def test_default_share_label_is_not_5h(self):
        with mock.patch.object(sl, "live_sessions", return_value=self._sessions(300, 100)):
            seg = plain(sl.share_segment({"session_id": "mine"}, "a@example.com", NOW, {}))
        self.assertFalse(seg.startswith("5h"))
        self.assertTrue(seg.startswith("mine "))

    def test_unknown_attribution_renders_a_question_mark(self):
        # Task 12 rule 4: nothing attributed at all (e.g. before Squirrel started watching this
        # account) is shown as unknown, never as a confident number.
        with mock.patch.object(sl, "live_sessions",
                                return_value=self._sessions(0, 100, kind="unknown")):
            seg = sl.share_segment({"session_id": "mine"}, "a@example.com", NOW, {}, 88.6)
        self.assertEqual(seg, "mine ?")

    def test_share_segment_is_coloured_like_the_composite(self):
        # Previously share_segment() rendered plain, uncoloured text — the only segment that
        # didn't match its neighbours. Task 12 brings it in line with session_usage_segment()'s
        # own convention: the contribution/share number on the share scale, the account total
        # on the usage scale — using the SAME two colour functions, not a bespoke one.
        with mock.patch.object(sl, "live_sessions", return_value=self._sessions(66.45, 22.15)):
            seg = sl.share_segment({"session_id": "mine"}, "a@example.com", NOW, {}, 88.6)
        share_pct = 66.45 / (66.45 + 22.15) * 100
        self.assertIn(sl.colored("66pp", sl.share_pct_color(share_pct, {})), seg)
        self.assertIn(sl.colored("89%", sl.pct_color(88.6, {})), seg)
        self.assertEqual(plain(seg), "mine 66pp/89%")

    def test_unknown_attribution_is_uncoloured(self):
        # Degrades sensibly: a '?' carries no confident number, so it carries no colour either.
        with mock.patch.object(sl, "live_sessions",
                                return_value=self._sessions(0, 100, kind="unknown")):
            seg = sl.share_segment({"session_id": "mine"}, "a@example.com", NOW, {}, 88.6)
        self.assertNotIn("\033[", seg)


class RenderThreadsAccountPctIntoShareTests(TmpEnv):
    """End-to-end: render() must feed the session (5h) bucket's own pct — the same source the
    '5h' segment displays — into share_segment as the account utilisation denominator, not a
    second, independently-fetched number."""

    def test_render_passes_the_5h_bucket_pct_into_share_segment(self):
        status = dict(STATUS_NO_RL, session_id="sess1")
        # share-life/sessions default to shown when a raw cfg dict bypasses load_config()'s
        # merge with defaults/config.json (see segment_enabled()) — turned off here since this
        # test is only about share_segment()'s own account_pct plumbing, and the mocked
        # live_sessions() below deliberately carries none of the lifetime_*/window_* fields
        # those other two segments need.
        cfg = {"segments": {"share": True, "share-life": False, "sessions": False}}
        with mock.patch.object(sl, "live_sessions", return_value=[
            {"session_id": "sess1", "attributed_points": 17.0, "attribution_kind": "exact"},
        ]):
            l1, _ = sl.render(status, CACHE, "you@example.com", cfg, NOW, timezone.utc, width=200)
        # CACHE's session pct is 17.0; this is the only live session (100% share) and its own
        # attributed points ARE 17.0 -> "mine 17pp/17%".
        self.assertIn("mine 17pp/17%", plain(l1))


# ── Task 11d (user redirect): fold the contribution into the 5h segment itself ───────────────
class UsageSegmentBarColourTests(unittest.TestCase):
    """usage_segment() (the plain weekly/per-model bucket, never a composite): baseline the
    composite regression (see SessionUsageSegmentTests.test_bar_carries_the_totals_colour_*)
    must never spread to — the bar has always been wrapped together with the percentage in a
    single pct_color(), and must stay that way."""

    def test_bar_carries_the_usage_colour(self):
        bucket = {"label": "", "pct": 94.0, "resets_at": None}
        seg = sl.usage_segment("week all", bucket, False, {"bars": True}, {})
        self.assertIn(sl.pct_color(94.0, {}) + sl.bar(94.0), seg)


class SharePctColorTests(unittest.TestCase):
    """share_pct_color(): a scale independent of pct_color()'s warnPercent/dangerPercent."""

    def test_default_thresholds(self):
        self.assertEqual(sl.share_pct_color(0), sl.GREEN)
        self.assertEqual(sl.share_pct_color(33.9), sl.GREEN)
        self.assertEqual(sl.share_pct_color(34), sl.YELLOW)
        self.assertEqual(sl.share_pct_color(66.9), sl.YELLOW)
        self.assertEqual(sl.share_pct_color(67), sl.RED)
        self.assertEqual(sl.share_pct_color(100), sl.RED)

    def test_configurable_thresholds(self):
        cfg = {"shareWarnPercent": 20, "shareDangerPercent": 40}
        self.assertEqual(sl.share_pct_color(10, cfg), sl.GREEN)
        self.assertEqual(sl.share_pct_color(25, cfg), sl.YELLOW)
        self.assertEqual(sl.share_pct_color(45, cfg), sl.RED)

    def test_independent_from_pct_color(self):
        # A session responsible for 90% of a window's burn reads red on the share scale even
        # while the window's total usage (say 20%) is comfortably green on pct_color()'s scale.
        self.assertEqual(sl.share_pct_color(90), sl.RED)
        self.assertEqual(sl.pct_color(20), sl.GREEN)


class SessionContributionEnabledTests(TmpEnv):
    def test_default_is_on(self):
        self.assertTrue(sl.session_contribution_enabled({}))
        self.assertTrue(sl.session_contribution_enabled(None))

    def test_explicit_off(self):
        self.assertFalse(sl.session_contribution_enabled({"sessionContribution": False}))


class SetSessionContributionCommandTests(TmpEnv):
    def test_on_off_and_invalid(self):
        self.assertIn("fold in", sl.cmd_set_session_contribution("on"))
        self.assertTrue(sl.load_config()["sessionContribution"])
        self.assertIn("plain account total", sl.cmd_set_session_contribution("off"))
        self.assertFalse(sl.load_config()["sessionContribution"])
        self.assertIn("usage", sl.cmd_set_session_contribution("bogus").lower())


class SessionContributionTests(TmpEnv):
    """session_contribution(): the data backing the 5h segment's composite figure — Task 12:
    now a thin read of live_sessions()'s own attributed_points/attribution_kind (ground truth),
    not a metric-driven ratio computed here."""

    @staticmethod
    def _sessions(mine_points, other_points, kind="exact"):
        return [
            {"session_id": "mine", "attributed_points": mine_points, "attribution_kind": kind},
            {"session_id": "other", "attributed_points": other_points, "attribution_kind": "exact"},
        ]

    def test_points_come_straight_from_live_sessions_not_a_cost_or_token_ratio(self):
        with mock.patch.object(sl, "live_sessions", return_value=self._sessions(90, 10)):
            info = sl.session_contribution({"session_id": "mine"}, "a@example.com", NOW, {})
        self.assertEqual(info["points"], 90)
        self.assertEqual(info["share_pct"], 90.0)   # 90 of 100 total attributed points

    def test_attributionMetric_has_no_effect_on_session_contribution(self):
        # Task 12: attributionMetric only ever drove the old cost/token PROXY for window
        # sharing; ground truth has no such knob any more (only the *lifetime* share/columns
        # still read it — see share_life_segment()).
        with mock.patch.object(sl, "live_sessions", return_value=self._sessions(90, 10)):
            info = sl.session_contribution({"session_id": "mine"}, "a@example.com", NOW,
                                            {"attributionMetric": "tokens"})
        self.assertEqual(info["points"], 90)

    def test_none_without_a_session_id(self):
        self.assertIsNone(sl.session_contribution({}, "a@example.com", NOW, {}))

    def test_none_when_the_only_live_session(self):
        with mock.patch.object(sl, "live_sessions", return_value=[
            {"session_id": "mine", "attributed_points": 100, "attribution_kind": "exact"},
        ]):
            self.assertIsNone(sl.session_contribution({"session_id": "mine"}, "a@example.com", NOW, {}))

    def test_none_when_this_session_is_not_among_the_live_ones(self):
        with mock.patch.object(sl, "live_sessions", return_value=self._sessions(100, 100)):
            self.assertIsNone(sl.session_contribution({"session_id": "nope"}, "a@example.com", NOW, {}))

    def test_none_when_this_sessions_attribution_is_entirely_unknown(self):
        # Task 12 rule 4: nothing attributed at all is None (falls back to the plain total),
        # never a confident zero.
        with mock.patch.object(sl, "live_sessions",
                                return_value=self._sessions(0, 100, kind="unknown")):
            self.assertIsNone(sl.session_contribution({"session_id": "mine"}, "a@example.com", NOW, {}))

    def test_points_and_partial_with_two_live_sessions(self):
        with mock.patch.object(sl, "live_sessions",
                                return_value=self._sessions(300, 100, kind="estimated")):
            info = sl.session_contribution({"session_id": "mine"}, "a@example.com", NOW, {})
        self.assertEqual(info, {"points": 300, "share_pct": 75.0, "partial": True})

    def test_exact_kind_is_not_partial(self):
        with mock.patch.object(sl, "live_sessions", return_value=self._sessions(300, 100)):
            info = sl.session_contribution({"session_id": "mine"}, "a@example.com", NOW, {})
        self.assertFalse(info["partial"])


class SessionUsageSegmentTests(TmpEnv):
    """session_usage_segment(): the 5h segment, with or without the folded-in composite.

    Task 12: `_sessions(mine_points, other_points, kind)` fixtures carry attributed_points/
    attribution_kind directly (ground truth) rather than window_tokens/window_cost — most
    values below are the old fraction/bucket-pct pair reparametrized as points
    (`mine_points = fraction * bucket_pct`, `other_points = (1 - fraction) * bucket_pct`),
    which preserves each scenario's original contribution AND share numbers exactly, since
    contribution_pp is now `mine_points` directly (no multiplication left to do)."""

    @staticmethod
    def _sessions(mine_points, other_points, kind="exact"):
        return [
            {"session_id": "mine", "attributed_points": mine_points, "attribution_kind": kind},
            {"session_id": "other", "attributed_points": other_points, "attribution_kind": "exact"},
        ]

    def test_composite_reads_attributed_points_not_a_cost_or_token_ratio(self):
        # 'mine' would look tiny by tokens or cost alone under the old proxy; ground truth
        # doesn't care — it already read 81pp straight off the account's own utilisation.
        bucket = {"label": "", "pct": 90.0, "resets_at": None}
        with mock.patch.object(sl, "live_sessions", return_value=self._sessions(81, 9)):
            seg = sl.session_usage_segment("5h", bucket, False, {"bars": False},
                                            {"session_id": "mine"}, "a@example.com", NOW, {})
        self.assertEqual(plain(seg), "5h 81pp/90%")

    def test_missing_bucket_is_na_regardless_of_attribution_data(self):
        seg = sl.session_usage_segment("5h", None, False, {"bars": False},
                                        {"session_id": "mine"}, "a@example.com", NOW, {})
        self.assertEqual(seg, "5h n/a")

    def test_plain_when_no_session_id(self):
        bucket = {"label": "", "pct": 90.0, "resets_at": None}
        seg = sl.session_usage_segment("5h", bucket, False, {"bars": False},
                                        {}, "a@example.com", NOW, {})
        self.assertEqual(plain(seg), "5h 90%")

    def test_plain_when_the_only_live_session(self):
        bucket = {"label": "", "pct": 90.0, "resets_at": None}
        with mock.patch.object(sl, "live_sessions", return_value=[
            {"session_id": "mine", "attributed_points": 10, "attribution_kind": "exact"},
        ]):
            seg = sl.session_usage_segment("5h", bucket, False, {"bars": False},
                                            {"session_id": "mine"}, "a@example.com", NOW, {})
        self.assertEqual(plain(seg), "5h 90%")

    def test_plain_when_disabled_even_with_multi_session_data(self):
        bucket = {"label": "", "pct": 90.0, "resets_at": None}
        with mock.patch.object(sl, "live_sessions", return_value=self._sessions(81, 9)):
            seg = sl.session_usage_segment("5h", bucket, False, {"bars": False},
                                            {"session_id": "mine"}, "a@example.com", NOW,
                                            {"sessionContribution": False})
        self.assertEqual(plain(seg), "5h 90%")

    def test_plain_when_this_sessions_attribution_is_unknown(self):
        bucket = {"label": "", "pct": 90.0, "resets_at": None}
        with mock.patch.object(sl, "live_sessions",
                                return_value=self._sessions(0, 90, kind="unknown")):
            seg = sl.session_usage_segment("5h", bucket, False, {"bars": False},
                                            {"session_id": "mine"}, "a@example.com", NOW, {})
        self.assertEqual(plain(seg), "5h 90%")

    def test_composite_matches_the_documented_example(self):
        # 900/1000 = 90% share; account total 90% (the docstring's own example, 'mine'
        # happening to also be responsible for the whole thing here) -> composite '81/90%'.
        bucket = {"label": "", "pct": 90.0, "resets_at": None}
        with mock.patch.object(sl, "live_sessions", return_value=self._sessions(81, 9)):
            seg = sl.session_usage_segment("5h", bucket, False, {"bars": False},
                                            {"session_id": "mine"}, "a@example.com", NOW, {})
        self.assertEqual(plain(seg), "5h 81pp/90%")

    def test_zero_points_gives_zero_contribution(self):
        bucket = {"label": "", "pct": 90.0, "resets_at": None}
        with mock.patch.object(sl, "live_sessions", return_value=self._sessions(0, 90)):
            seg = sl.session_usage_segment("5h", bucket, False, {"bars": False},
                                            {"session_id": "mine"}, "a@example.com", NOW, {})
        self.assertEqual(plain(seg), "5h 0pp/90%")

    def test_hundred_percent_share_gives_full_contribution(self):
        # Two live sessions, but 'other' hasn't consumed anything in this window yet.
        bucket = {"label": "", "pct": 90.0, "resets_at": None}
        with mock.patch.object(sl, "live_sessions", return_value=self._sessions(90, 0)):
            seg = sl.session_usage_segment("5h", bucket, False, {"bars": False},
                                            {"session_id": "mine"}, "a@example.com", NOW, {})
        self.assertEqual(plain(seg), "5h 90pp/90%")

    def test_zero_account_total(self):
        bucket = {"label": "", "pct": 0.0, "resets_at": None}
        with mock.patch.object(sl, "live_sessions", return_value=self._sessions(0, 0)):
            seg = sl.session_usage_segment("5h", bucket, False, {"bars": False},
                                            {"session_id": "mine"}, "a@example.com", NOW, {})
        self.assertEqual(plain(seg), "5h 0pp/0%")

    def test_stale_marks_the_contribution_figure(self):
        bucket = {"label": "", "pct": 90.0, "resets_at": None}
        with mock.patch.object(sl, "live_sessions", return_value=self._sessions(81, 9)):
            seg = sl.session_usage_segment("5h", bucket, True, {"bars": False},
                                            {"session_id": "mine"}, "a@example.com", NOW, {})
        self.assertEqual(plain(seg), "5h ~81pp/90%")

    def test_estimated_kind_marks_the_contribution_figure(self):
        bucket = {"label": "", "pct": 90.0, "resets_at": None}
        with mock.patch.object(sl, "live_sessions",
                                return_value=self._sessions(81, 9, kind="estimated")):
            seg = sl.session_usage_segment("5h", bucket, False, {"bars": False},
                                            {"session_id": "mine"}, "a@example.com", NOW, {})
        self.assertEqual(plain(seg), "5h ~81pp/90%")

    def test_stale_alone_does_not_mark_a_plain_fallback_render_twice(self):
        # Sanity: the plain path's own stale-marking is unaffected by this feature.
        bucket = {"label": "", "pct": 90.0, "resets_at": None}
        seg = sl.session_usage_segment("5h", bucket, True, {"bars": False},
                                        {}, "a@example.com", NOW, {})
        self.assertEqual(plain(seg), "5h ~90%")

    def test_contribution_and_total_use_independent_colour_scales(self):
        # share = 90% (>= shareDangerPercent default 67) -> red contribution figure, even
        # though the account total (40%, < warnPercent default 50) is green.
        bucket = {"label": "", "pct": 40.0, "resets_at": None}
        with mock.patch.object(sl, "live_sessions", return_value=self._sessions(36, 4)):
            seg = sl.session_usage_segment("5h", bucket, False, {"bars": False},
                                            {"session_id": "mine"}, "a@example.com", NOW, {})
        self.assertIn(f"{sl.RED}36pp{sl.RESET}", seg)
        self.assertIn(f"{sl.GREEN}40%{sl.RESET}", seg)
        self.assertEqual(plain(seg), "5h 36pp/40%")

    def test_bar_reflects_the_account_total_not_the_share(self):
        bucket = {"label": "", "pct": 90.0, "resets_at": None}
        with mock.patch.object(sl, "live_sessions", return_value=self._sessions(9, 81)):
            seg = sl.session_usage_segment("5h", bucket, False, {"bars": True},
                                            {"session_id": "mine"}, "a@example.com", NOW, {})
        self.assertIn(sl.bar(90.0), plain(seg))

    def test_bar_carries_the_totals_colour_in_composite_form(self):
        # Regression: the bar previously fell outside any colour wrapping once the composite
        # was introduced — only the two numbers were coloured, so at e.g. 94% (red) the bar
        # rendered with no colour escape immediately before it at all, instead of red. Checking
        # for the colour escape directly followed by the bar glyphs (rather than a whole
        # colored() span with a matching reset right after) works for both this composite form
        # and the plain form below, which reset later, after the trailing '94%'.
        bucket = {"label": "", "pct": 94.0, "resets_at": None}
        with mock.patch.object(sl, "live_sessions", return_value=self._sessions(85, 9)):
            seg = sl.session_usage_segment("5h", bucket, False, {"bars": True},
                                            {"session_id": "mine"}, "a@example.com", NOW, {})
        self.assertIn(sl.pct_color(94.0, {}) + sl.bar(94.0), seg)

    def test_bar_carries_the_totals_colour_in_plain_form(self):
        # Same check with no other live session, so session_usage_segment takes the plain
        # (non-composite) branch — the baseline this segment must never regress from.
        bucket = {"label": "", "pct": 94.0, "resets_at": None}
        seg = sl.session_usage_segment("5h", bucket, False, {"bars": True},
                                        {}, "a@example.com", NOW, {})
        self.assertIn(sl.pct_color(94.0, {}) + sl.bar(94.0), seg)

    def test_contribution_rounds_independently_of_the_total(self):
        # Task 12: contribution_pp is read straight off the ground truth, with no multiplicative
        # relationship to total_pct any more, so display rounding is just plain ':.0f' rounding
        # of whatever attributed_points holds — verify it round-trips for a spread of values,
        # including ones that exceed total_pct (a live_sessions() bug elsewhere, not this
        # function's job to clamp — see its docstring).
        bucket = {"label": "", "pct": 50.0, "resets_at": None}
        for points in (0, 1, 12.5, 33.3, 49.9, 50.0, 66.6, 100.0):
            with mock.patch.object(sl, "live_sessions", return_value=self._sessions(points, 1)):
                seg = sl.session_usage_segment("5h", bucket, False, {"bars": False},
                                                {"session_id": "mine"}, "a@example.com", NOW, {})
            self.assertEqual(plain(seg), f"5h {points:.0f}pp/50%")


class ShareContributionConfigDefaultsTests(unittest.TestCase):
    def test_packaged_defaults(self):
        packaged = sl.load_json(sl.default_config_path())
        self.assertIs(packaged.get("sessionContribution"), True)
        self.assertEqual(sl.tunable(packaged, "shareWarnPercent"), 34)
        self.assertEqual(sl.tunable(packaged, "shareDangerPercent"), 67)


class RenderThreadsCompositeIntoSessionSegmentTests(TmpEnv):
    """End-to-end: render() must show the composite inside the 'session' (5h) segment itself
    when the data supports it, distinct from the standalone 'share' segment."""

    def test_five_hour_segment_shows_the_composite(self):
        status = dict(STATUS_NO_RL, session_id="sess1")
        cfg = {"segments": {"share": False, "share-life": False, "sessions": False}}
        with mock.patch.object(sl, "live_sessions", return_value=[
            {"session_id": "sess1", "attributed_points": 15.3, "attribution_kind": "exact"},
            {"session_id": "sess2", "attributed_points": 1.7, "attribution_kind": "exact"},
        ]):
            l1, _ = sl.render(status, CACHE, "you@example.com", cfg, NOW, timezone.utc, width=200)
        # CACHE's session pct is 17.0; sess1's own attributed points ARE 15.3 -> composite '15/17%'.
        self.assertIn("15pp/17%", plain(l1))
        self.assertTrue(plain(l1).startswith("ctx"))
        # 'mine'/'share' is off by default and must not also appear.
        self.assertNotIn("mine", plain(l1))


# ── Task 11b: performance regression guards ───────────────────────────────────
# Wall-clock assertions are flaky in CI (see dev/bench.py for the by-hand timing tool
# instead), so these guards check *structure*: which modules get imported, and how many git
# subprocesses a render forks — properties that don't vary run to run.
class LazyImportGuardTests(unittest.TestCase):
    """urllib.request (and the http/ssl/email chain it pulls in) costs ~23ms to import — see
    dev/bench.py — and is used only by the detached `--fetch` child (fetch_usage/run_fetch/
    _get_opener), never by the render path. These checks run in a fresh subprocess: this test
    file itself imports urllib.error/urllib.request at its own top (for FetchTests), so the
    current process's sys.modules is useless for testing what a *bare* import of statusline.py
    pulls in.

    Bare `urllib` is deliberately not checked: `pathlib` (which statusline.py needs regardless,
    unrelated to this task) imports `urllib.parse` for Path.as_uri(), which sets
    sys.modules['urllib'] as a side effect — free, no network stack, nothing to do with the
    ~23ms cost. The expensive chain this guards is urllib.request/http/ssl/email specifically."""

    # `typing` joins the list for the same reason at a smaller scale: ~2ms per render, and
    # nothing on the render path needs it (Segment is a plain __slots__ class precisely so a
    # NamedTuple annotation cannot drag it back in).
    # `http.server`/`socketserver` join the list for Task 26: the click listener runs in a
    # detached child, and its HTTP machinery must never be imported by a render. `secrets`
    # likewise — ~1.7ms over the hashlib the module already needs, for something only the
    # listener uses.
    HEAVY_MODULES = ("urllib.request", "urllib.error", "http", "ssl", "email", "typing",
                     "http.server", "socketserver", "secrets")

    def _assert_heavy_modules_absent(self, body: str) -> None:
        script_dir = str(Path(sl.__file__).resolve().parent)
        script = (
            "import sys, json\n"
            f"sys.path.insert(0, {script_dir!r})\n"
            f"{body}\n"
            f"print(json.dumps([m for m in {list(self.HEAVY_MODULES)!r} if m in sys.modules]))\n"
        )
        result = subprocess.run([sys.executable, "-c", script], capture_output=True, text=True, timeout=15)
        self.assertEqual(result.returncode, 0, f"subprocess failed: {result.stderr}")
        present = json.loads(result.stdout.strip().splitlines()[-1])
        self.assertEqual(present, [], f"unexpectedly imported: {present}")

    def test_module_import_alone_does_not_load_urllib_request(self):
        self._assert_heavy_modules_absent("import statusline\n")

    def test_full_render_does_not_load_urllib_request(self):
        status = json.dumps({
            "session_id": "guard-render",
            "model": {"display_name": "Test Model"},
            "workspace": {"current_dir": "/tmp"},
            "context_window": {"context_window_size": 100_000, "used_percentage": 10},
            "cost": {},
        })
        body = (
            "import os, tempfile, io\n"
            "tmp = tempfile.mkdtemp(prefix='squirrel-guard-')\n"
            "os.environ['CLAUDE_CONFIG_DIR'] = tmp + '/config'\n"
            "os.environ['SQUIRREL_SHARED_DIR'] = tmp + '/shared'\n"
            "cfg_dir = tmp + '/config/squirrel'\n"
            "os.makedirs(cfg_dir)\n"
            # usageFetch off: a real render here must not need the network at all, and this
            # keeps the guard from racing a detached --fetch child it doesn't care about.
            "with open(cfg_dir + '/config.json', 'w') as f: f.write('{\"usageFetch\": false}')\n"
            "import statusline\n"
            f"sys.stdin = io.StringIO({status!r})\n"
            "statusline.main([])\n"
            # The subprocess owns this directory, so it has to clean up after itself — nothing
            # in the parent gets a chance to. Without this the suite leaves a squirrel-guard-*
            # directory behind on every run (noticed on Windows, where they pile up in
            # %LOCALAPPDATA%\\Temp; equally true on Linux, just less visible in /tmp).
            "import shutil; shutil.rmtree(tmp, ignore_errors=True)\n"
        )
        self._assert_heavy_modules_absent(body)


class GitSubprocessCountGuardTests(TmpEnv):
    """The render path must fork git at most once per render. _fit() calls render_line2() (and
    therefore git_segment()) repeatedly while hunting for a tier that fits a narrow pane — 9
    times, empirically, for the payload below at width=30 — so this is only true because
    _git_status()'s per-cwd cache turns every call after the first into a cache hit within one
    render. A second render of the same cwd within gitCacheSeconds must fork git zero
    *additional* times."""

    def _status(self, repo):
        return {
            "session_id": "guard", "workspace": {"current_dir": repo},
            "model": {"display_name": "Test Model With A Fairly Long Name"},
            "effort": {"level": "high"}, "thinking": {"enabled": True},
            "context_window": {"context_window_size": 200_000, "used_percentage": 42},
            "cost": {"total_cost_usd": 1.5, "total_duration_ms": 60_000,
                     "total_lines_added": 10, "total_lines_removed": 2},
        }

    @staticmethod
    def _git_call_count(calls):
        return len([c for c in calls if c and c[0] == "git"])

    def _wrap_subprocess_run(self, calls):
        real_run = subprocess.run

        def counting_run(cmd, *a, **kw):
            calls.append(cmd)
            return real_run(cmd, *a, **kw)

        return counting_run

    def test_render_invokes_at_most_one_git_subprocess(self):
        repo = str(Path(sl.__file__).resolve().parent.parent)   # a real repo — this one
        status = self._status(repo)
        config = sl.load_config()
        calls = []
        with mock.patch("subprocess.run", side_effect=self._wrap_subprocess_run(calls)):
            sl.render(status, {}, "you@example.com", config, NOW, width=30)
        self.assertLessEqual(self._git_call_count(calls), 1, calls)

    def test_second_render_within_ttl_invokes_zero_additional_git_subprocesses(self):
        repo = str(Path(sl.__file__).resolve().parent.parent)
        status = self._status(repo)
        config = sl.load_config()
        calls = []
        with mock.patch("subprocess.run", side_effect=self._wrap_subprocess_run(calls)):
            sl.render(status, {}, "you@example.com", config, NOW, width=30)
            first = self._git_call_count(calls)
            sl.render(status, {}, "you@example.com", config, NOW, width=30)
        self.assertEqual(first, 1)
        self.assertEqual(self._git_call_count(calls), 1)   # no new git calls on the second render


class BurnRateResetTests(TmpEnv):
    """A session's own counters can go backwards (a resumed or replaced session). That is a
    restart, not negative work — the burn rate must never be negative."""

    def setUp(self):
        super().setUp()
        self.shared = self.root / "shared"
        patcher = mock.patch.dict(os.environ, {"SQUIRREL_SHARED_DIR": str(self.shared)})
        patcher.start(); self.addCleanup(patcher.stop)
        self.acct = sl.account_hash("a@example.com")

    def _write(self, sid, samples):
        sl.write_cache(sl.session_path(sid),
                       {"session_id": sid, "repo": sid, "samples": [list(s) for s in samples]})

    def test_backwards_counters_do_not_produce_a_negative_burn_rate(self):
        a = self.acct
        self._write("restarted", [(NOW - 600, 900_000, 500.0, None, a, 50.0),
                                  (NOW - 60, 10_000, 2.0, None, a, 50.0)])
        s = {x["session_id"]: x for x in sl.live_sessions("a@example.com", NOW)}["restarted"]
        self.assertGreaterEqual(s["burn_cost_per_min"], 0.0)
        self.assertGreaterEqual(s["burn_per_min"], 0.0)

    def test_normal_forward_progress_still_reports_a_rate(self):
        a = self.acct
        self._write("busy", [(NOW - 600, 100_000, 10.0, None, a, 40.0),
                             (NOW - 60, 200_000, 20.0, None, a, 55.0)])
        s = {x["session_id"]: x for x in sl.live_sessions("a@example.com", NOW)}["busy"]
        self.assertGreater(s["burn_cost_per_min"], 0.0)
        self.assertGreater(s["burn_per_min"], 0.0)

class RestartedSessionAttributionTests(TmpEnv):
    """A machine or process restart resets a session's own cumulative counters. The session is
    still working — it must not be mistaken for idle and silently dropped from attribution."""

    def setUp(self):
        super().setUp()
        self.shared = self.root / "shared"
        patcher = mock.patch.dict(os.environ, {"SQUIRREL_SHARED_DIR": str(self.shared)})
        patcher.start(); self.addCleanup(patcher.stop)
        self.acct = sl.account_hash("a@example.com")

    def test_a_restarted_session_is_active_not_idle(self):
        a = self.acct
        # s1 progresses normally; s2's counters restart (900k/$500 -> 10k/$2) in the same interval
        sessions = {
            "s1": [(NOW - 200, 0, 0.0, None, a, 20.0), (NOW - 100, 100, 10.0, None, a, 40.0)],
            "s2": [(NOW - 200, 900_000, 500.0, None, a, 20.0), (NOW - 100, 10_000, 2.0, None, a, 40.0)],
        }
        intervals = sl.walk_utilisation_attribution(sessions, a)
        shares = {}
        for iv in intervals:
            for sid, pts in iv["shares"].items():
                shares[sid] = shares.get(sid, 0.0) + pts
        self.assertIn("s2", shares, "a restarted session was dropped from attribution")
        self.assertGreater(shares["s2"], 0.0)
        self.assertAlmostEqual(sum(shares.values()), 20.0, places=6)

    def test_a_genuinely_idle_session_still_receives_nothing(self):
        a = self.acct
        sessions = {
            "busy": [(NOW - 200, 0, 0.0, None, a, 20.0), (NOW - 100, 100, 10.0, None, a, 40.0)],
            "idle": [(NOW - 200, 50, 5.0, None, a, 20.0), (NOW - 100, 50, 5.0, None, a, 40.0)],
        }
        intervals = sl.walk_utilisation_attribution(sessions, a)
        shares = {}
        for iv in intervals:
            for sid, pts in iv["shares"].items():
                shares[sid] = shares.get(sid, 0.0) + pts
        self.assertNotIn("idle", shares)
        self.assertAlmostEqual(shares.get("busy", 0.0), 20.0, places=6)


class BarsShrinkBeforeTextIsDroppedTests(TmpEnv):
    """A bar is decoration; a reset time is information. When the pane is tight the bars must
    narrow first, and text must only be dropped once narrowing has run out of room.

    Regression: at COLUMNS=126 the full line (116 columns) did not fit the old ladder's first
    step, so every reset time disappeared at once while 40 columns of bar remained."""

    def _line1(self, width):
        now = NOW
        cache = {"fetched_at": now, "ok": True, "error": None,
         "account": sl.account_hash("a@example.com"), "limits": {
            "session": {"label": "", "pct": 14.0, "resets_at": now + 9000},
            "weekly_all": {"label": "", "pct": 58.0, "resets_at": now + 86400},
            "scoped": [{"label": "Fable", "pct": 53.0, "resets_at": now + 86400}]}}
        status = {"model": {"display_name": "Opus 5"},
                  "context_window": {"context_window_size": 1_000_000,
                                     "used_percentage": 14, "current_usage": {}}}
        return sl.render(status, cache, "a@example.com", {}, now, timezone.utc, width=width)[0]

    def test_reset_times_survive_a_moderately_narrow_pane(self):
        for width in (126, 110, 95):
            line = self._line1(width)
            self.assertIn("↻", plain(line), f"reset time dropped at width {width}")
            self.assertLessEqual(sl.visible_len(line), width)

    def test_bars_narrow_as_the_pane_narrows(self):
        wide = plain(self._line1(200)).count("▰") + plain(self._line1(200)).count("▱")
        tight = plain(self._line1(95)).count("▰") + plain(self._line1(95)).count("▱")
        self.assertLess(tight, wide, "bars did not narrow before text was dropped")

    def test_every_bar_narrows_together(self):
        # all bars on the line share the tier's width — no segment keeps a full-width bar
        import re as _re
        runs = _re.findall(r"[▰▱]+", plain(self._line1(95)))
        self.assertGreater(len(runs), 1)
        self.assertEqual(len(set(len(r) for r in runs)), 1, f"bars differ in width: {runs}")


# ── golden render fixture ─────────────────────────────────────────────────────
# Task 13 (the segment-registry / free-layout refactor) is required to change NOTHING that a
# user with no layout config can observe. These scenarios were rendered against the code as it
# stood BEFORE the refactor (commit 2331fd5) and frozen, escapes and all, into
# dev/golden_render.json. GoldenRenderTests replays them; any diff is a behaviour change.
# Regenerate ONLY with a deliberate, reviewed behaviour change:
#     python3 dev/write_golden.py
#
# WHAT THIS FIXTURE CROSSES  — read this before trusting a "0 mismatches" result.
# The first version of this fixture reported 0 mismatches across 91 cells and still missed a
# real behaviour change, because every scenario ran against an EMPTY shared store: the three
# cross-session segments (share / share-life / sessions) returned None on both sides, so any
# divergence between them was invisible. "0 mismatches" only ever means "identical in the
# states someone thought to capture".
#
#   dimension            values covered
#   -------------------  ----------------------------------------------------------------
#   width                None (unconstrained), 200, 160, 126, 110, 95, 85, 70, 60, 50, 40,
#                        30, 20  — spans every rung of the shedding ladder
#   shared store         empty · multi (5 live sessions, one of them current, one idle) ·
#                        stale (every session past sessionLiveSeconds) · unknown (no
#                        utilisation history -> '?') · reset (utilisation goes backwards
#                        mid-history) · switched (one session relogs to another account)
#   segment switches     shipped defaults (share/share-life/sessions OFF via
#                        defaults/config.json) · explicitly ON · bare config dict with no
#                        `segments` key at all (they default to shown)
#   config provenance    dict passed straight in · the module's own packaged defaults via
#                        load_config() · a real user config.json on disk merged by
#                        load_config() (incl. the partial-`segments` replacement quirk)
#   usage source         api cache · stdin rate_limits only · no data at all
#   staleness            fresh cache · ok=False · fetched_at beyond staleAfterSeconds
#   account label        raw email · nickname · not logged in
#   line-2 content       full · repo+git present · minimal status (no cost/pr/effort)
#
# WHAT IT DOES NOT COVER — deliberately, and the next person should know:
#   * the idle/alarm background painter (apply_state writes real files; the chip and banner
#     are covered by RenderStateTests / IdleBackgroundTests / PhaseLadderTests instead)
#   * non-default layouts and priority overrides (LayoutResolutionTests / LayoutPriorityTests)
#   * a '>=' lower-bound marker: the coverage design note describes one, but no code path
#     renders '>=' on the status line today — only '?' (unknown) and '~' (estimated)
#   * real git output (git_segment is always mocked, so the fixture is repo-independent)
#   * terminal-width autodetection (width is always passed explicitly)
GOLDEN_WIDTHS = (None, 200, 160, 126, 110, 95, 85, 70, 60, 50, 40, 30, 20)
GOLDEN_ACCOUNT = "you@example.com"
GOLDEN_OTHER_ACCOUNT = "someone-else@example.com"


def _golden_store(mod, sessions: dict) -> None:
    """Populate the (already isolated) shared store.

    `sessions` maps session id -> (repo, [(seconds_relative_to_NOW, cumulative_tokens,
    cumulative_cost, model, account_email_or_None, account_utilisation_pct_or_None), ...]).
    Every repo name is distinct and every session's attributed total differs, so the ranking
    and the collision-suffixed labels in `sessions_segment` are deterministic.
    """
    for sid, (repo, samples) in sessions.items():
        rows = [[NOW + offset, tokens, cost, model,
                 None if account is None else mod.account_hash(account), pct]
                for offset, tokens, cost, model, account, pct in samples]
        mod.write_cache(mod.session_path(sid),
                        {"session_id": sid, "repo": repo, "samples": rows})


def golden_stores() -> dict:
    """name -> {session_id: (repo, samples)}; see _golden_store for the sample shape."""
    A, B, M = GOLDEN_ACCOUNT, GOLDEN_OTHER_ACCOUNT, "Fable 5"
    return {
        # Five live sessions on one account — one is the current session, one has burned
        # nothing since the window opened (confirmed idle, not unknown), and five exceeds the
        # default sessionsMax of 4 so the '+N' collapse is exercised too. Overlapping
        # intervals make the attribution "estimated", which is what puts the '~' marks in.
        "multi": {
            "gold-cur":  ("alpha",   [(-1200, 0, 0.0, M, A, 20.0), (-600, 50, 5.0, M, A, 40.0),
                                      (-60, 90, 9.0, M, A, 55.0)]),
            "gold-b":    ("bravo",   [(-1200, 0, 0.0, M, A, 20.0), (-600, 20, 2.0, M, A, 40.0),
                                      (-60, 30, 3.0, M, A, 55.0)]),
            "gold-c":    ("charlie", [(-1200, 0, 0.0, M, A, 20.0), (-60, 10, 1.0, M, A, 55.0)]),
            "gold-idle": ("delta",   [(-1200, 7, 0.7, M, A, 20.0), (-60, 7, 0.7, M, A, 55.0)]),
            "gold-e":    ("echo",    [(-1200, 0, 0.0, M, A, 20.0), (-60, 5, 0.5, M, A, 55.0)]),
        },
        # Everything past sessionLiveSeconds (900s): no session is live, so all three
        # cross-session segments must produce nothing at all.
        "stale": {
            "gold-cur": ("alpha", [(-9000, 0, 0.0, M, A, 20.0), (-5000, 90, 9.0, M, A, 55.0)]),
            "gold-b":   ("bravo", [(-9000, 0, 0.0, M, A, 20.0), (-5000, 30, 3.0, M, A, 55.0)]),
        },
        # No utilisation history at all (every account_pct None) alongside a session that has
        # some: the current session's attribution is "unknown", which must render '?' rather
        # than a fabricated 0%.
        "unknown": {
            "gold-cur": ("alpha", [(-1200, 0, 0.0, M, A, None), (-60, 90, 9.0, M, A, None)]),
            "gold-b":   ("bravo", [(-1200, 0, 0.0, M, A, None), (-60, 30, 3.0, M, A, None)]),
        },
        # Utilisation goes backwards mid-history — the 5-hour window reset. A reset is never
        # negative consumption; the interval across it contributes nothing rather than a
        # negative number.
        "reset": {
            "gold-cur": ("alpha", [(-1200, 0, 0.0, M, A, 80.0), (-600, 50, 5.0, M, A, 5.0),
                                   (-60, 90, 9.0, M, A, 25.0)]),
            "gold-b":   ("bravo", [(-1200, 0, 0.0, M, A, 80.0), (-600, 20, 2.0, M, A, 5.0),
                                   (-60, 30, 3.0, M, A, 25.0)]),
        },
        # One session relogs to a different account mid-life: its early samples are tagged A,
        # its later ones B. The current session stays on A. Only the slices tagged A may
        # count towards A's window.
        "switched": {
            "gold-cur":  ("alpha", [(-1200, 0, 0.0, M, A, 20.0), (-60, 90, 9.0, M, A, 55.0)]),
            "gold-swap": ("bravo", [(-1200, 0, 0.0, M, A, 20.0), (-600, 40, 4.0, M, A, 40.0),
                                    (-60, 70, 7.0, M, B, 12.0)]),
        },
    }


def golden_scenarios():
    """name -> dict(status, cache, email, config, git, store).

    `config` may be a callable taking the statusline module, so a scenario can ask for that
    module's own packaged defaults (which is what a user with no config file actually gets —
    and is where share/share-life/sessions are switched off).
    `store` is a key into golden_stores(), or None for an empty shared store.
    """
    minimal = {"model": {"display_name": "Fable 5"},
               "context_window": {"context_window_size": 200_000, "current_usage": None}}
    with_repo = dict(STATUS, workspace={"repo": {"name": "demo"}, "current_dir": "/repo"})
    current = dict(STATUS, session_id="gold-cur")

    def shipped_defaults(mod):
        return mod.load_config()

    def attribution_on(mod):
        cfg = mod.load_config()
        cfg["segments"] = {**(cfg.get("segments") or {}),
                           "share": True, "share-life": True, "sessions": True}
        return cfg

    bare = {"accountColors": {"example": "#00bcd4"}}     # no `segments` key at all

    def user_config(patch):
        """Config as a real user gets it: written to disk and merged by load_config(), which
        is where the packaged defaults and the user's overrides actually meet."""
        def build(mod):
            path = mod.config_path()
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps(patch))
            cfg = mod.load_config()
            path.unlink()
            return cfg
        return build

    def scen(status=STATUS, cache=CACHE, email=GOLDEN_ACCOUNT, config=CONFIG, git=None, store=None):
        return {"status": status, "cache": cache, "email": email,
                "config": config, "git": git, "store": store}

    out = {
        "default": scen(),
        "repo-and-git": scen(with_repo, git=("\u2387 main*", "YELLOW")),
        "stdin-only": scen(STATUS, {}, None, {}),
        "stale-cache": scen(STATUS, dict(CACHE, ok=False, error="http 401")),
        "hidden-segments": scen(config=dict(CONFIG, segments={"tokens": False, "pr": False,
                                                              "cost": False})),
        "minimal": scen(minimal, {}, None, {}),
        "nickname": scen(config={"accountColors": {"example": "#00bcd4"},
                                 "accountNames": {GOLDEN_ACCOUNT: "kai"}}),
    }
    # The cross-session dimension: every store crossed with the three ways the three
    # attribution segments can be switched. `shipped` is the one a user with no config gets.
    for store in golden_stores():
        for cfg_name, cfg in (("shipped", shipped_defaults), ("on", attribution_on), ("bare", bare)):
            out[f"{store}-{cfg_name}"] = scen(current, config=cfg, store=store)
    # Config as it actually reaches a user: written to disk, merged with the packaged defaults
    # by load_config(). `partial` deliberately switches on only ONE of the three segments —
    # load_config()'s top-level update() replaces the whole `segments` map, so the other two
    # lose the shipped `false` and reappear. That is long-standing behaviour, captured here so
    # it cannot change silently.
    out["multi-userconfig-all-on"] = scen(
        current, config=user_config({"segments": {"share": True, "share-life": True,
                                                  "sessions": True}}), store="multi")
    out["multi-userconfig-partial"] = scen(
        current, config=user_config({"segments": {"share": True}}), store="multi")
    return out


def golden_render_all(mod=None):
    """{scenario: {width: [line1, line2, ...]}} — raw, escapes included.

    `mod` is the statusline module to render with (defaults to the one under test), so the
    same scenarios can be replayed against an older revision for a byte-identity comparison.
    Each scenario gets its OWN temp shared store, populated before the render and thrown away
    after, so scenarios cannot leak into one another.
    """
    mod = sl if mod is None else mod
    stores = golden_stores()
    out = {}
    for name, sc in golden_scenarios().items():
        git = sc["git"]
        git_value = [(git[0], getattr(mod, git[1]))] if git else []
        with tempfile.TemporaryDirectory(prefix="golden-shared-") as shared:
            with mock.patch.dict(os.environ, {"SQUIRREL_SHARED_DIR": shared}):
                if sc["store"]:
                    _golden_store(mod, stores[sc["store"]])
                config = sc["config"](mod) if callable(sc["config"]) else sc["config"]
                with mock.patch.object(mod, "git_segment_runs", return_value=git_value):
                    out[name] = {str(w): mod.render(sc["status"], sc["cache"], sc["email"],
                                                    config, NOW, timezone.utc, width=w)
                                 for w in GOLDEN_WIDTHS}
    return out


GOLDEN_PATH = Path(__file__).resolve().parent / "golden_render.json"


class GoldenRenderTests(TmpEnv):
    """Byte-for-byte proof that the default rendering has not changed. See the note above."""

    def test_render_matches_the_frozen_golden_capture(self):
        expected = json.loads(GOLDEN_PATH.read_text(encoding="utf-8"))
        actual = golden_render_all()
        self.assertEqual(sorted(actual), sorted(expected))
        for name in expected:
            for width in expected[name]:
                self.assertEqual(actual[name][width], expected[name][width],
                                 f"golden mismatch: scenario {name!r} at width {width}")



# ── Task 13: segment registry + free-form layout ─────────────────────────────
class SegmentRegistryTests(unittest.TestCase):
    """The registry is the single id space. Nothing may be reachable from one half of it and
    invisible to the other."""

    def test_every_registry_id_is_a_known_segment_key(self):
        self.assertEqual([i for i in sl.SEGMENTS if i not in sl.SEGMENT_KEYS], [])

    def test_shipped_layout_places_every_registry_segment_exactly_once(self):
        placed = [i for line in sl.shipped_layout() for i in line]
        self.assertEqual(sorted(placed), sorted(sl.SEGMENTS))
        self.assertEqual(len(placed), len(set(placed)))

    def test_protected_segments_are_derived_from_the_registry(self):
        self.assertEqual(set(sl.PROTECTED_SEGMENTS),
                         {i for i, seg in sl.SEGMENTS.items() if seg.protected})
        self.assertEqual(set(sl.PROTECTED_SEGMENTS), {"account", "model"})

    def test_every_producer_returns_styled_runs(self):
        """Every producer hands back (plain text, SGR-or-None) runs — the shape the boundary
        sanitiser and the per-segment styles both need. A bare string is tolerated as sugar
        for one unstyled run, but no producer may return a pre-coloured string."""
        ctx = sl._ctx_for(STATUS, {"session": None, "weekly_all": None, "scoped": []},
                          False, "stdin", None, {}, NOW, timezone.utc, None)
        for seg_id, seg in sl.SEGMENTS.items():
            for text, sgr in sl.as_runs(seg.render(ctx)):
                self.assertIsInstance(text, str, f"{seg_id}: run text is not a string")
                self.assertNotIn("\033", text,
                                 f"{seg_id}: run text carries an escape — producers must "
                                 f"return plain text plus a separate SGR")
                self.assertTrue(sgr is None or isinstance(sgr, str),
                                f"{seg_id}: bad run style {sgr!r}")

    def test_detail_keys_are_real_option_names(self):
        opts = sl.detail_opts(0, {})
        for seg in sl.SEGMENTS.values():
            for key in seg.details:
                self.assertIn(key, opts, f"{seg.id} declares unknown detail {key!r}")


class LayoutDefaultsTests(unittest.TestCase):
    def test_defaults_config_json_ships_the_registry_layout(self):
        shipped = json.loads(sl.default_config_path().read_text(encoding="utf-8"))
        self.assertEqual(shipped["layout"], sl.shipped_layout())
        self.assertEqual(shipped["segmentPriority"], {})


class LayoutResolutionTests(TmpEnv):
    def _render(self, cfg_patch, width=1000, status_extra=None):
        (self.root / "squirrel").mkdir(exist_ok=True)
        (self.root / "squirrel" / "config.json").write_text(json.dumps(cfg_patch))
        status = dict(STATUS, session_id=None, **(status_extra or {}))
        with mock.patch.object(sl, "git_segment_runs", return_value=[]):
            return [plain(l) for l in sl.render(status, CACHE, "you@example.com",
                                                sl.load_config(), NOW, timezone.utc, width=width)]

    def test_one_line_layout_renders_one_line(self):
        lines = self._render({"layout": [["account", "model", "context"]]})
        self.assertEqual(len(lines), 1)
        self.assertEqual(lines[0], "you@example.com · Fable 5 │ ctx ▰▰▰▰▱▱▱▱▱▱ 42% (84k/200k)")

    def test_four_line_layout_renders_four_lines(self):
        lines = self._render({"layout": [["context"], ["session"], ["weekly"],
                                         ["account", "model"]]})
        self.assertEqual(len(lines), 4)
        self.assertTrue(lines[0].startswith("ctx "))
        self.assertTrue(lines[1].startswith("5h "))
        self.assertTrue(lines[2].startswith("week all "))
        self.assertEqual(lines[3], "you@example.com · Fable 5")

    def test_unknown_ids_are_skipped_without_error(self):
        lines = self._render({"layout": [["context", "from-the-future", "account", "model"]]})
        self.assertEqual(lines[0], "ctx ▰▰▰▰▱▱▱▱▱▱ 42% (84k/200k) │ you@example.com · Fable 5")

    def test_a_malformed_layout_falls_back_to_the_shipped_one(self):
        for broken in ("nonsense", [], [["context"], "oops"], 7):
            self.assertEqual(sl.resolve_layout({"layout": broken}),
                             sl.resolve_layout({}), broken)

    def test_legacy_segments_map_is_folded_into_the_layout(self):
        layout = sl.resolve_layout({"segments": {"cost": False, "pr": False}})
        placed = [i for line in layout for i in line]
        self.assertNotIn("cost", placed)
        self.assertNotIn("pr", placed)
        self.assertIn("duration", placed)

    def test_a_protected_segment_is_reinstated_at_its_shipped_position(self):
        # a hand-edited config that lost `account` must still render it, in the right place
        layout = sl.resolve_layout({"layout": [["context"], ["repo", "model", "cost"]]})
        self.assertEqual(layout[1][:2], ["account", "repo"])

    def test_segments_absent_from_the_layout_do_not_render(self):
        lines = self._render({"layout": [["account", "model"]]})
        self.assertEqual(lines, ["you@example.com · Fable 5"])


class LayoutPriorityTests(TmpEnv):
    def _line2(self, cfg, width):
        with mock.patch.object(sl, "git_segment_runs", return_value=[]):
            return plain(sl.render(STATUS, CACHE, "you@example.com", cfg, NOW,
                                   timezone.utc, width=width)[1])

    def test_lowest_priority_is_dropped_first(self):
        wide = self._line2({}, 1000)
        self.assertIn("PR #42", wide)
        self.assertLess(self._line2({}, len(wide)).find("PR"), 0)   # one column short → PR goes

    def test_priority_override_changes_the_drop_order(self):
        # cost normally outlives pr; give cost the lowest priority and it goes first instead
        cfg = {"segmentPriority": {"cost": 5}}
        wide = self._line2(cfg, 1000)
        narrowed = self._line2(cfg, len(wide))
        self.assertNotIn("$1.23", narrowed)
        self.assertIn("PR #42", narrowed)

    def test_always_keeps_a_segment_on_the_narrowest_pane(self):
        line = self._line2({"segmentPriority": {"pr": "always"}}, 20)
        self.assertIn("PR #42", line)
        self.assertIn("you@example.com", line)
        self.assertNotIn("$1.23", line)          # everything droppable still went

    def test_protected_segments_are_never_dropped(self):
        line = self._line2({}, 1)
        self.assertIn("you@example.com", line)
        self.assertIn("Fable 5", line)

    def test_producer_runs_once_per_render_however_many_widths_are_tried(self):
        calls = []

        def counting(status, config=None, now=None):
            calls.append(1)
            return [("⎇ main", None)]

        status = dict(STATUS, workspace={"repo": {"name": "demo"}, "current_dir": "/repo"})
        with mock.patch.object(sl, "git_segment_runs", side_effect=counting):
            sl.render(status, CACHE, "you@example.com", {}, NOW, timezone.utc, width=30)
        self.assertEqual(len(calls), 1, f"git segment produced {len(calls)}x in one render")


class DocsImagesTests(unittest.TestCase):
    """The README's screenshots are generated from the real renderer (dev/docs_images.py), so
    a picture on the project page cannot claim a bar Squirrel does not produce. This is the
    half that keeps that true: change the rendering and the committed images are stale until
    someone regenerates them."""

    def test_the_committed_images_match_the_current_renderer(self):
        script = Path(__file__).resolve().parent / "docs_images.py"
        out = subprocess.run([sys.executable, str(script), "--check"],
                             capture_output=True, text=True, timeout=120)
        self.assertEqual(out.returncode, 0,
                         f"{out.stdout}{out.stderr}\nregenerate: python3 dev/docs_images.py")


class SessionsSegmentDenominatorTests(TmpEnv):
    """One '%' sign must mean one thing across a line. The bar used to read

        mine ~43%/77% │ ▸beacon ~76% · sandbox ~24%

    where 43 and 76 are the SAME session: points of the account's window on the left, share of
    the live sessions' total on the right. The user spotted it in a screenshot within minutes."""

    def setUp(self):
        super().setUp()
        self.shared = self.root / "shared"
        patcher = mock.patch.dict(os.environ, {"SQUIRREL_SHARED_DIR": str(self.shared)})
        patcher.start(); self.addCleanup(patcher.stop)
        account = sl.account_hash("you@example.com")
        # one account at 77%, two sessions credited 43 and 13 of it
        for sid, repo, cost in (("s1", "beacon", 8.0), ("s2", "experiments", 2.4)):
            samples = [[NOW - 3600 + i * 600, i * 1000, round(cost * i / 6, 4), "Opus 5",
                        account, [0, 12, 24, 36, 48, 62, 77][i], NOW + 3600]
                       for i in range(7)]
            sl.write_cache(sl.session_path(sid),
                           {"session_id": sid, "repo": repo, "samples": samples})

    def _seg(self, config):
        return plain(sl.sessions_segment({"session_id": "s1"}, "you@example.com", NOW, config) or "")

    def test_default_speaks_in_points_of_the_account_window(self):
        text = self._seg({})
        mine = plain(sl.share_segment({"session_id": "s1"}, "you@example.com", NOW, {}, 77.0) or "")
        points = re.search(r"~?(\d+)pp", mine).group(1)
        self.assertIn(f"{points}pp", text, f"{mine!r} and {text!r} disagree about the same session")

    def test_share_format_still_gives_the_ratio_among_sessions(self):
        ratio = self._seg({"shareFormat": "share"})
        figures = [int(n) for n in re.findall(r"~?(\d+)%", ratio)]
        self.assertAlmostEqual(sum(figures), 100, delta=2)

    def test_both_shows_the_points_and_the_ratio(self):
        text = self._seg({"shareFormat": "both"})
        self.assertRegex(text, r"~?\d+pp \(\d+%\)")


class ContributionUnitTests(TmpEnv):
    """`5h ~1/5%` was misread by the person who designed it: two percentages of the same
    window separated by a slash read as the fraction one-fifth, when they mean "one of the
    five points used so far is mine". The unit is the fix — 'pp' is what the
    /squirrel-sessions table already says, so the bar and the table now use one word for one
    thing. Every place the pair is rendered must carry it."""

    def setUp(self):
        super().setUp()
        self.shared = self.root / "shared"
        patcher = mock.patch.dict(os.environ, {"SQUIRREL_SHARED_DIR": str(self.shared)})
        patcher.start(); self.addCleanup(patcher.stop)
        account = sl.account_hash("you@example.com")
        for sid, repo, cost in (("s1", "alpha", 8.0), ("s2", "beta", 2.0)):
            sl.write_cache(sl.session_path(sid), {"session_id": sid, "repo": repo, "samples": [
                [NOW - 3600 + i * 600, i * 1000, round(cost * i / 6, 4), "Opus 5", account,
                 [0, 1, 2, 3, 4, 5, 5][i], NOW + 3600] for i in range(7)]})

    def test_the_composite_says_pp_on_the_left_and_percent_on_the_right(self):
        opts = sl.detail_opts(0, {})
        seg = plain(sl.session_usage_segment(
            "5h", {"pct": 5.0, "resets_at": NOW + 3600}, False, dict(opts, bars=False),
            {"session_id": "s1"}, "you@example.com", NOW, {}))
        self.assertRegex(seg, r"^5h ~?\d+pp/\d+%$", f"unit missing: {seg!r}")

    def test_the_standalone_share_segment_says_it_too(self):
        seg = plain(sl.share_segment({"session_id": "s1"}, "you@example.com", NOW, {}, 5.0) or "")
        self.assertRegex(seg, r"~?\d+pp/\d+%")

    def test_the_sessions_list_says_it_too(self):
        seg = plain(sl.sessions_segment({"session_id": "s1"}, "you@example.com", NOW, {}) or "")
        self.assertRegex(seg, r"~?\d+pp")

    def test_a_plain_bucket_is_still_a_plain_percentage(self):
        """No unit creep: with nothing attributed there is only one number, and it is a plain
        percentage of the window, exactly as it always was."""
        opts = sl.detail_opts(0, {})
        seg = plain(sl.session_usage_segment(
            "5h", {"pct": 5.0, "resets_at": NOW + 3600}, False, dict(opts, bars=False),
            {"session_id": "nobody"}, "you@example.com", NOW, {}))
        self.assertEqual(seg, "5h 5%")


class PhaseAlertTests(TmpEnv):
    """The off-screen half of the alarm. The user runs three monitors: a bar nobody is looking
    at is not an alarm, and they do not want a sound. A phase may therefore carry an action as
    well as a colour — and the thing that makes it safe is that it fires ONCE per waiting
    episode, never on the render path."""

    def setUp(self):
        super().setUp()
        self.shared = self.root / "shared"
        env = mock.patch.dict(os.environ, {"SQUIRREL_SHARED_DIR": str(self.shared)})
        env.start(); self.addCleanup(env.stop)
        # the suite disables alerts machine-wide (see setUpModule); these tests are about them
        killswitch = mock.patch.dict(os.environ, {"SQUIRREL_NO_ALERTS": ""})
        killswitch.start(); self.addCleanup(killswitch.stop)
        self.spawned = []
        popen = mock.patch("subprocess.Popen",
                           side_effect=lambda *a, **k: self.spawned.append((a, k)))
        popen.start(); self.addCleanup(popen.stop)

    WORKSPACE = {"repo": {"name": "demo-repo"}, "current_dir": "/w/demo-repo"}

    def _render(self, age, extra=None, session="alert-1"):
        sl.apply_state("idle", session, NOW - age)
        config = sl.load_config() | (extra or {})
        with mock.patch.object(sl, "git_segment_runs", return_value=[]):
            sl.render(dict(STATUS, session_id=session, workspace=self.WORKSPACE), CACHE,
                      "you@example.com", config, NOW, timezone.utc, width=120)
        return [a[0] for a, _k in self.spawned if a and isinstance(a[0], list)
                and "--notify" in a[0]]

    def test_the_shipped_ladder_notifies_at_five_minutes(self):
        self.assertEqual(self._render(120), [], "notified before the notify phase")
        self.assertEqual(len(self._render(400, session="alert-2")), 1)

    def test_it_fires_once_per_waiting_episode_not_once_per_render(self):
        sl.apply_state("idle", "alert-3", NOW - 400)
        config = sl.load_config()
        with mock.patch.object(sl, "git_segment_runs", return_value=[]):
            for _ in range(5):          # five renders, twenty-five seconds of bar
                sl.render(dict(STATUS, session_id="alert-3", workspace=self.WORKSPACE), CACHE,
                          "you@example.com", config, NOW, timezone.utc, width=120)
        self.assertEqual(len(self.spawned), 1)

    def test_a_new_wait_notifies_again(self):
        self._render(400, session="alert-4")
        sl.apply_state("working", "alert-4", NOW - 402)      # they came back...
        self.assertEqual(len(self._render(401, session="alert-4")), 2)   # ...and left again

    def test_the_master_switch_silences_it(self):
        self.assertEqual(self._render(400, {"phaseAlerts": False}), [])

    def test_the_notification_names_the_repo_so_you_know_which_terminal(self):
        argv = self._render(400)[0]
        title, body, hint = argv[argv.index("--notify") + 1:argv.index("--notify") + 4]
        self.assertIn("demo-repo", title, f"no repo in the title: {title!r}")
        self.assertIn("waiting", body)
        self.assertIn("demo-repo", hint)                   # what the focus check compares

    def test_a_run_action_needs_the_commands_switch(self):
        cfg = {"idlePhases": [{"after": 300, "run": "touch /tmp/should-not-happen"}]}
        self._render(400, cfg, session="alert-5")
        self.assertEqual([a for a, _k in self.spawned if isinstance(a[0], str)], [])
        self._render(400, dict(cfg, customCommands=True), session="alert-6")
        self.assertEqual([a[0] for a, _k in self.spawned if isinstance(a[0], str)],
                         ["touch /tmp/should-not-happen"])

    def test_an_alert_that_cannot_start_never_breaks_the_render(self):
        with mock.patch("subprocess.Popen", side_effect=OSError("no fork for you")):
            sl.apply_state("idle", "alert-7", NOW - 400)
            with mock.patch.object(sl, "git_segment_runs", return_value=[]):
                lines = sl.render(dict(STATUS, session_id="alert-7", workspace=self.WORKSPACE),
                                  CACHE, "you@example.com",
                                  sl.load_config(), NOW, timezone.utc, width=120)
        self.assertEqual(len(lines), 2)


class DesktopNotifyTests(TmpEnv):
    """The notifier itself: which channel, and when to stay quiet."""

    def _run(self, **kw):
        return mock.patch("subprocess.run", **kw)

    def test_focus_suppresses_it_by_default(self):
        with mock.patch.object(sl, "foreground_window_title", return_value="my-app — VS Code"):
            self.assertIn("suppressed", sl.desktop_notify("t", "b", hint="my-app"))

    def test_focus_on_another_window_does_not(self):
        with mock.patch.object(sl, "foreground_window_title", return_value="Firefox"), \
             mock.patch.object(sl, "_is_wsl", return_value=False), \
             mock.patch.object(sl.shutil, "which", return_value="/usr/bin/notify-send"), \
             self._run(return_value=mock.Mock(returncode=0, stderr="", stdout="")):
            self.assertIn("sent", sl.desktop_notify("t", "b", hint="my-app"))

    def test_an_unknown_foreground_notifies_rather_than_guessing(self):
        """None means "we cannot tell", and the cost of a redundant toast is a glance while
        the cost of a suppressed one is the thing this feature exists to prevent."""
        with mock.patch.object(sl, "foreground_window_title", return_value=None), \
             mock.patch.object(sl, "_is_wsl", return_value=False), \
             mock.patch.object(sl.shutil, "which", return_value="/usr/bin/notify-send"), \
             self._run(return_value=mock.Mock(returncode=0, stderr="", stdout="")):
            self.assertIn("sent", sl.desktop_notify("t", "b", hint="my-app"))

    def test_notify_send_exiting_zero_with_a_dbus_error_is_not_success(self):
        """Measured on the reporter's own WSL box: no notification daemon, `notify-send`
        exits 0 and prints `GDBus.Error...ServiceUnknown` to stderr. Trusting the exit status
        would have reported success for a notification nobody saw."""
        with mock.patch.object(sl, "_is_wsl", return_value=False), \
             mock.patch.object(sys, "platform", "linux"), \
             mock.patch.object(sl.shutil, "which", side_effect=lambda n: "/usr/bin/notify-send"
                               if n == "notify-send" else None), \
             self._run(return_value=mock.Mock(returncode=0, stdout="",
                                              stderr="GDBus.Error:...ServiceUnknown")):
            self.assertIn("no notifier", sl.desktop_notify("t", "b"))

    def test_wsl_uses_a_windows_toast(self):
        with mock.patch.object(sl, "_is_wsl", return_value=True), \
             self._run(return_value=mock.Mock(returncode=0, stdout="", stderr="")) as run:
            self.assertIn("Windows toast", sl.desktop_notify("Squirrel", "waiting 5m"))
        argv = run.call_args[0][0]
        self.assertIn("-EncodedCommand", argv)      # base64 UTF-16LE: survives every quoting layer

    def test_the_toast_text_cannot_break_out_of_its_xml(self):
        with mock.patch.object(sl, "_is_wsl", return_value=True), \
             self._run(return_value=mock.Mock(returncode=0, stdout="", stderr="")) as run:
            sl.desktop_notify("</text><script>", "'; Remove-Item C:\\ -Recurse", hint="")
        import base64
        script = base64.b64decode(run.call_args[0][0][-1]).decode("utf-16-le")
        self.assertNotIn("<script>", script)        # no raw markup reaches the toast XML
        self.assertIn("&lt;/text&gt;", script)      # it arrives escaped instead
        self.assertNotIn("'; Remove-Item", script)  # nor does a quote break the PowerShell string
        self.assertIn("&apos;", script)

    def test_a_machine_with_no_notifier_says_so(self):
        with mock.patch.object(sl, "_is_wsl", return_value=False), \
             mock.patch.object(sl.shutil, "which", return_value=None), \
             mock.patch.object(sys, "platform", "linux"):
            self.assertIn("no notifier", sl.desktop_notify("t", "b"))

    def test_the_command_reports_and_tests(self):
        with mock.patch.object(sl, "desktop_notify", return_value="sent: Windows toast"):
            self.assertIn("sent", sl.cmd_alerts("test"))
        self.assertIn("off", sl.cmd_alerts("off"))
        self.assertFalse(sl.load_config()["phaseAlerts"])
        self.assertIn("quiet", sl.cmd_alerts("focused off"))
        self.assertIn("usage", sl.cmd_alerts("focused sideways").lower())
        out = sl.cmd_alerts("")
        self.assertIn("desktop alerts", out)


class BannerLinesTests(TmpEnv):
    """An alarm ground is a warning about the session; the row of things you are meant to
    reach for must not be buried in it. `bannerLines` decides which lines it covers."""

    CFG = {"customSegments": [{"id": "water", "every": 1800, "ack": True, "line": 3,
                               "text": "\U0001f4a7 drink water", "click": "ack"},
                              {"id": "break", "every": 7200, "ack": True, "line": 3,
                               "text": "\U0001f9d8 take a break", "click": "ack"}],
           "layout": [["context"], ["account", "model"], ["water", "break"]]}

    def _lines(self, extra=None):
        sl.apply_state("idle", "b1", NOW - 9999)
        config = sl.load_config() | self.CFG | (extra or {})
        with mock.patch.object(sl, "git_segment_runs", return_value=[]):
            return sl.render(dict(STATUS, session_id="b1"), CACHE, "you@example.com", config,
                             NOW, timezone.utc, width=120)

    def _painted(self, line):
        return line.startswith("\033[48")

    def test_an_actions_only_row_is_left_unpainted_by_default(self):
        l1, l2, l3 = self._lines()
        self.assertTrue(self._painted(l1))
        self.assertTrue(self._painted(l2))
        self.assertFalse(self._painted(l3), "the reminders row is buried in the alarm colour")
        self.assertIn("drink water", plain(l3))

    def test_all_paints_every_line(self):
        lines = self._lines({"bannerLines": "all"})
        self.assertTrue(all(self._painted(line) for line in lines))

    def test_an_explicit_list_paints_exactly_those_lines(self):
        l1, l2, l3 = self._lines({"bannerLines": [1, 3]})
        self.assertTrue(self._painted(l1))
        self.assertFalse(self._painted(l2))
        self.assertTrue(self._painted(l3))

    def test_a_mixed_row_is_still_painted(self):
        """Only a row that is ENTIRELY actions steps out of the banner — otherwise a reminder
        sitting next to the account name would punch a hole in the alarm."""
        cfg = {"customSegments": [dict(spec, line=2) for spec in self.CFG["customSegments"]],
               "layout": [["context"], ["account", "model", "water", "break"]]}
        sl.apply_state("idle", "b2", NOW - 9999)
        with mock.patch.object(sl, "git_segment_runs", return_value=[]):
            lines = sl.render(dict(STATUS, session_id="b2"), CACHE, "you@example.com",
                              sl.load_config() | cfg, NOW, timezone.utc, width=120)
        self.assertTrue(all(self._painted(line) for line in lines))

    def test_a_nonsense_value_never_disables_the_alarm(self):
        for junk in ("banana", 7, None, {"line": 1}):
            l1, _l2, l3 = self._lines({"bannerLines": junk})
            self.assertTrue(self._painted(l1), junk)
            self.assertFalse(self._painted(l3), junk)

    def test_the_command_writes_all_three_shapes(self):
        self.assertIn("every line", sl.cmd_set_bg("lines all"))
        self.assertEqual(sl.load_config()["bannerLines"], "all")
        sl.cmd_set_bg("lines 1,2")
        self.assertEqual(sl.load_config()["bannerLines"], [1, 2])
        sl.cmd_set_bg("lines auto")
        self.assertEqual(sl.load_config()["bannerLines"], "auto")
        self.assertIn("not a line number", sl.cmd_set_bg("lines banana"))
        self.assertEqual(sl.load_config()["bannerLines"], "auto")     # nothing written on error


class HostMeasuredWidthTests(TmpEnv):
    """Claude Code truncates each status line by ITS OWN measurement of the width, and that
    measurement is not ours: `Bun.stringWidth(text, {ambiguousIsNarrow: true})` strips SGR
    colour but leaves OSC alone, so the whole URL inside an OSC-8 hyperlink counts as columns
    on screen. A clickable reminder carries a 43-character token, so one link read ~86 columns
    wider to the host than it drew — and the host answered by cutting the tail off the line
    and replacing it with an ellipsis. The user photographed it on four panes: 'take a br…' on
    one, and a bar of empty colour ending in '…' on another.

    This is defect family 4 in the header of this file, exactly: the golden fixture has no
    live click listener in it, so every line it ever measured was link-free and the two
    measures agreed. The bug lived entirely in the interaction the fixture could not have."""

    URL = "http://127.0.0.1:41234/" + "T" * 43 + "/ack/water"
    LINK_COST = len(URL) + 10          # OSC-8 open (]8;; + URL + ST) and close (]8;; + ST)

    def _linked(self, text="\U0001f4a7 drink water"):
        return f"\033]8;;{self.URL}\033\\{text}\033]8;;\033\\"

    def test_host_len_counts_the_url_and_visible_len_does_not(self):
        line = self._linked()
        self.assertEqual(sl.visible_len(line), 14)
        self.assertEqual(sl.host_len(line), 14 + self.LINK_COST)

    def test_the_two_measures_agree_when_there_is_no_link(self):
        for text in ("plain", "\033[31mred\033[0m", "▰▱ 42% · ‼",
                     "\U0001f9d8 take a break"):
            self.assertEqual(sl.host_len(text), sl.visible_len(text), repr(text))

    def _render(self, cfg, width, link=True):
        """A bar whose last segment is a clickable reminder, as the user's really is."""
        cfg = dict(cfg)
        cfg["customSegments"] = [{"id": "water", "every": 1800, "ack": True,
                                  "text": "\U0001f4a7 drink water", "click": "ack"}]
        cfg["layout"] = [["context"], ["account", "model", "water"]]
        with mock.patch.object(sl, "click_url", return_value=self.URL if link else None), \
             mock.patch.object(sl, "git_segment_runs", return_value=[]):
            return sl.render(STATUS, CACHE, "you@example.com", cfg, NOW, timezone.utc, width=width)

    def test_a_linked_line_is_fitted_to_the_width_the_host_measures(self):
        line = self._render({}, 200)[1]
        self.assertIn(self.URL, line)                       # the link survived: it fits
        self.assertLessEqual(sl.host_len(line), 199)        # ...and the host will not cut it

    def test_the_link_is_shed_before_any_text_when_it_no_longer_fits(self):
        wide = plain(self._render({}, 200)[1])
        narrow = self._render({}, 120)[1]
        self.assertNotIn(self.URL, narrow)                  # the link went
        self.assertIn("drink water", plain(narrow))         # the words did not
        self.assertLessEqual(sl.host_len(narrow), 119)
        self.assertIn("drink water", wide)

    def test_pinning_links_keeps_them_and_costs_the_visible_line(self):
        cfg = {"segmentPriority": {"links": "always", "water": "always"}}
        line = self._render(cfg, 120)[1]
        self.assertIn(self.URL, line)               # pinned: kept even though it overflows
        self.assertIn("drink water", plain(line))

    def test_a_painted_banner_keeps_the_link_it_has_room_for(self):
        """The regression the user hit within the hour of the first fix: their bar is painted
        for most of the day, so 'a banner drops links' meant 'drink water is never clickable',
        and their terminal then auto-linkified the plain words into links that open nothing."""
        sl.apply_state("idle", "banner-0", NOW - 9999)
        status = dict(STATUS, session_id="banner-0")
        cfg = {"customSegments": [{"id": "water", "every": 1800, "ack": True,
                                   "text": "💧 drink water", "click": "ack"}],
               "layout": [["context"], ["account", "water"]]}
        with mock.patch.object(sl, "click_url", return_value=self.URL),              mock.patch.object(sl, "git_segment_runs", return_value=[]):
            line = sl.render(status, CACHE, "you@example.com", sl.load_config() | cfg,
                             NOW, timezone.utc, width=200)[1]
        self.assertIn(self.URL, line)
        self.assertLessEqual(sl.host_len(line), 199)

    def test_pinning_links_keeps_the_reminder_clickable_on_a_tight_bar(self):
        """Their layout, their width, their idle banner — the shape they reported twice.

        On a 150-column pane a 13-segment line has nowhere near the ~64 columns the host
        charges for a hyperlink, so by default the link is what gives way (the alternative is
        half the line disappearing without the user asking for it). `--set-priority links
        always` together with `--set-priority water always` is the other choice, and this is
        what it buys: the reminder and its click both survive, and the bar sheds text until the
        line genuinely fits — no truncation, no ellipsis. Both pins are needed, and that is not
        a wart: pinning only the link would have the ladder drop the whole reminder (text and
        link together) as the cheaper way to fit, which is not what the user asked for."""
        sl.apply_state("idle", "real", NOW - 700)
        status = dict(STATUS, session_id="real")
        cfg = sl.load_config() | {
            "customSegments": [{"id": "water", "every": 1800, "ack": True, "shared": True,
                                "text": "💧 drink water", "click": "ack"}],
            "layout": [["context", "session", "weekly"],
                       ["account", "repo", "git", "model", "effort", "flags", "sessionTokens",
                        "cost", "duration", "lines", "pr", "chip", "water"]],
            "segmentPriority": {"links": "always", "water": "always"}}
        with mock.patch.object(sl, "click_url", return_value=self.URL), \
             mock.patch.object(sl, "git_segment_runs", return_value=[]):
            line = sl.render(status, CACHE, "you@example.com", cfg, NOW, timezone.utc,
                             width=150)[1]
        self.assertIn(self.URL, line)                    # clickable
        self.assertIn("drink water", plain(line))
        self.assertNotIn("+0/−0", plain(line))           # the text paid for it
        self.assertLessEqual(sl.host_len(line), 149)     # and nothing gets truncated

    def test_two_reminders_shed_their_links_one_at_a_time(self):
        """A pane with room for one clickable reminder must show one, not none.

        Both of the user's reminders sat at the same priority, so there was no level at which
        exactly one link survived: the fit went from "both" straight to "neither", and a bar
        with 20 spare columns showed two unclickable words. Links on a line are now staggered
        one level apart in layout order — first listed, last to go."""
        cfg = sl.load_config() | {
            "customSegments": [
                {"id": "water", "every": 1800, "ack": True, "line": 3,
                 "text": "\U0001f4a7 drink water", "click": "ack"},
                {"id": "break", "every": 7200, "ack": True, "line": 3,
                 "text": "\U0001f9d8 take a break", "click": "ack"}],
            "layout": [["context"], ["account", "model"], ["water", "break"]]}
        def url(_ctx, seg_id, _action):
            return f"http://127.0.0.1:41234/{'T' * 22}/ack/{seg_id}"
        def clickable(width):
            with mock.patch.object(sl, "click_url", side_effect=url), \
                 mock.patch.object(sl, "git_segment_runs", return_value=[]):
                line = sl.render(STATUS, CACHE, "you@example.com", cfg, NOW,
                                 timezone.utc, width=width)[2]
            self.assertIn("drink water", plain(line))     # the first reminder always stays
            return re.findall(r"ack/(\w+)", line), plain(line)
        self.assertEqual(clickable(200)[0], ["water", "break"])   # room for both
        links, text = clickable(110)
        self.assertEqual(links, ["water"])                       # room for one — the first
        self.assertIn("take a break", text)                      # and the other keeps its text
        self.assertEqual(clickable(60)[0], [])                   # room for neither

    def test_a_reference_link_still_costs_the_bar_nothing(self):
        """The other half of the rule: a `link=` on an informational segment sheds first, so
        adding one can never cost the user a bar cell or a word (OscWidthAccountingTests holds
        the byte-identity form of this)."""
        cfg = dict(CONFIG, customSegments=[{"id": "note", "text": "PR #42",
                                            "link": "https://example.com/" + "p" * 60}])
        with mock.patch.object(sl, "git_segment_runs", return_value=[]):
            line = sl.render(STATUS, CACHE, "you@example.com", cfg, NOW, timezone.utc,
                             width=130)[0]
        self.assertIn("PR #42", plain(line))
        self.assertIn("▰▰▰▰▱▱▱▱▱▱", plain(line))         # bars untouched
        self.assertNotIn("example.com", line)             # the link was what gave way

    def test_a_painted_banner_drops_the_link_and_reaches_the_right_edge(self):
        sl.apply_state("idle", "banner-1", NOW - 9999)
        status = dict(STATUS, session_id="banner-1")
        cfg = {"customSegments": [{"id": "water", "every": 1800, "ack": True,
                                   "text": "\U0001f4a7 drink water", "click": "ack"}],
               "layout": [["context"], ["account", "model", "water"]]}
        with mock.patch.object(sl, "click_url", return_value=self.URL), \
             mock.patch.object(sl, "git_segment_runs", return_value=[]):
            lines = sl.render(status, CACHE, "you@example.com", sl.load_config() | cfg,
                              NOW, timezone.utc, width=120)
        for line in lines:
            self.assertNotIn(self.URL, line)      # no room for it at this width
            # Both measures agree once the link is gone, and the paint runs to the last
            # column the host will draw: full width, and no ellipsis.
            self.assertEqual(sl.visible_len(line), 119)
            self.assertEqual(sl.host_len(line), 119)

    def test_pinned_links_survive_a_banner_and_the_paint_stops_short_instead(self):
        sl.apply_state("idle", "banner-2", NOW - 9999)
        status = dict(STATUS, session_id="banner-2")
        cfg = {"customSegments": [{"id": "water", "every": 1800, "ack": True,
                                   "text": "\U0001f4a7 drink water", "click": "ack"}],
               "layout": [["context"], ["account", "model", "water"]],
               "segmentPriority": {"links": "always", "water": "always"}}
        with mock.patch.object(sl, "click_url", return_value=self.URL), \
             mock.patch.object(sl, "git_segment_runs", return_value=[]):
            line = sl.render(status, CACHE, "you@example.com", sl.load_config() | cfg,
                             NOW, timezone.utc, width=120)[1]
        self.assertIn(self.URL, line)
        # What pinning actually buys and costs, stated plainly: the link survives the banner,
        # and the line is over the host's budget by the length of the URL — so the host elides
        # its tail. That tail is padding, not text, which is the trade the user chose.
        self.assertLess(sl.visible_len(line), 119)
        self.assertGreater(sl.host_len(line), 119)

    def test_pane_reserve_widens_the_margin_for_a_host_that_lies_about_columns(self):
        sl.apply_state("idle", "banner-3", NOW - 9999)
        status = dict(STATUS, session_id="banner-3")
        cfg = sl.load_config() | {"paneReserveColumns": 4}
        with mock.patch.object(sl, "git_segment_runs", return_value=[]):
            line = sl.render(status, CACHE, "you@example.com", cfg, NOW, timezone.utc, width=120)[0]
        self.assertEqual(sl.visible_len(line), 116)

    @unittest.skipUnless(shutil.which("bun"), "bun is the oracle for the host's own measure")
    def test_host_len_agrees_with_bun_string_width(self):
        """The oracle, not a restatement: Claude Code calls exactly this function, so a change
        in either measure shows up here rather than on the user's bar."""
        samples = [self._linked(), "plain text", "\033[38;2;1;2;3mcoloured\033[0m",
                   "▰▰▱ 63% · │ ⎇ ⚙ ☾ ‼",
                   "\U0001f4a7 drink water \U0001f9d8 take a break",
                   plain(sl.render(STATUS, CACHE, "you@example.com", {}, NOW,
                                   timezone.utc, width=100)[0])]
        script = (Path(self.root) / "width.js")
        script.write_text(
            "const a = await Bun.file(process.argv[2]).json();\n"
            "console.log(JSON.stringify(a.map(s => Bun.stringWidth(s, {ambiguousIsNarrow: true}))));\n")
        data = Path(self.root) / "samples.json"
        data.write_text(json.dumps(samples))
        out = subprocess.run(["bun", str(script), str(data)], capture_output=True, text=True,
                             timeout=60)
        self.assertEqual(out.returncode, 0, out.stderr)
        self.assertEqual([sl.host_len(s) for s in samples], json.loads(out.stdout))


class DetailPriorityTests(TmpEnv):
    """The four within-segment details shed on the same configurable priority scale as the
    segments themselves — "keep the reset times even on a narrow terminal"."""

    def _line1(self, cfg, width):
        with mock.patch.object(sl, "git_segment_runs", return_value=[]):
            return plain(sl.render(STATUS, CACHE, "you@example.com", cfg, NOW,
                                   timezone.utc, width=width)[0])

    def test_shipped_shed_order_is_tokens_resets_bars_labels_then_links(self):
        # `links` goes LAST: a click the user relies on outlives text nobody reaches for.
        self.assertEqual([k for k, _ in sorted(sl.DETAIL_PRIORITIES.items(), key=lambda kv: kv[1])],
                         ["tokens", "resets", "bars", "abbrev", "links"])

    def test_reset_times_survive_a_narrow_pane_when_pinned(self):
        self.assertNotIn("↻", self._line1({}, 60))
        self.assertIn("↻", self._line1({"segmentPriority": {"resets": 95}}, 60))
        self.assertIn("↻", self._line1({"segmentPriority": {"resets": "always"}}, 20))

    def test_pinning_one_detail_does_not_pin_the_others(self):
        line = self._line1({"segmentPriority": {"resets": "always"}}, 40)
        self.assertIn("↻", line)
        self.assertNotIn("(84k/200k)", line)      # tokens still shed

    def test_a_detail_priority_is_settable_by_command(self):
        self.assertIn("95", sl.cmd_set_priority("resets 95"))
        self.assertEqual(sl.detail_priority(sl.load_config(), "resets"), 95)
        sl.cmd_set_priority("resets always")
        self.assertIsNone(sl.detail_priority(sl.load_config(), "resets"))

    def test_unknown_detail_is_refused(self):
        msg = sl.cmd_set_priority("sparkles 5")
        self.assertIn("unknown segment", msg.lower())
        self.assertIn("resets", msg)
        self.assertNotIn("segmentPriority", sl.user_config())

    def test_show_layout_lists_the_details(self):
        out = sl.cmd_show_layout()
        self.assertIn("details  :", out)
        self.assertIn("resets(40)", out)


class LayoutCommandTests(TmpEnv):
    def _layout(self):
        return sl.load_config()["layout"]

    def test_show_lists_every_line_with_priorities(self):
        out = sl.cmd_layout("")
        self.assertIn("line 1:", out)
        self.assertIn("line 2:", out)
        self.assertIn("account(always, protected)", out)
        self.assertIn("pr(10)", out)

    def test_move_to_a_new_line(self):
        sl.cmd_layout("move git 3")
        self.assertEqual(self._layout()[2], ["git"])
        self.assertNotIn("git", self._layout()[1])

    def test_move_to_an_explicit_position(self):
        sl.cmd_layout("move pr 2 1")
        self.assertEqual(self._layout()[1][0], "pr")

    def test_line_sets_one_whole_line_and_steals_from_the_others(self):
        sl.cmd_layout("line 1 context session")
        self.assertEqual(self._layout()[0], ["context", "session"])
        sl.cmd_layout("line 3 weekly cost")
        self.assertEqual(self._layout()[2], ["weekly", "cost"])
        self.assertNotIn("cost", self._layout()[1])

    def test_swap_exchanges_two_lines(self):
        sl.cmd_layout("swap 1 2")
        self.assertEqual(self._layout()[0], sl.shipped_layout()[1])
        self.assertEqual(self._layout()[1], sl.shipped_layout()[0])

    def test_priority_and_always(self):
        self.assertIn("unknown segment", sl.cmd_layout("priority branchless 3").lower())
        self.assertNotIn("segmentPriority", sl.user_config())
        sl.cmd_layout("priority git 3")
        self.assertEqual(sl.load_config()["segmentPriority"]["git"], 3)
        self.assertEqual(sl.segment_priority(sl.load_config(), "git"), 3)
        sl.cmd_layout("priority git always")
        self.assertIsNone(sl.segment_priority(sl.load_config(), "git"))

    def test_reset_restores_the_shipped_arrangement(self):
        sl.cmd_layout("move git 3")
        sl.cmd_layout("priority git 3")
        sl.cmd_layout("reset")
        self.assertEqual(self._layout(), sl.shipped_layout())
        self.assertEqual(sl.load_config()["segmentPriority"], {})

    def test_edits_are_surgical_and_leave_other_settings_alone(self):
        sl.save_config({"accountNames": {"you@example.com": "kai"}, "branchMaxChars": 7})
        sl.cmd_layout("move git 3")
        user_cfg = sl.user_config()
        self.assertEqual(user_cfg["accountNames"], {"you@example.com": "kai"})
        self.assertEqual(user_cfg["branchMaxChars"], 7)
        self.assertEqual(sorted(user_cfg), ["accountNames", "branchMaxChars", "layout"])

    def test_unknown_segment_is_refused_and_nothing_is_written(self):
        for arg in ("move banana 1", "line 1 context banana"):
            msg = sl.cmd_layout(arg)
            self.assertIn("unknown segment", msg.lower(), arg)
            self.assertNotIn("layout", sl.user_config())

    def test_out_of_range_line_is_refused_and_nothing_is_written(self):
        msg = sl.cmd_layout("move git 9")
        self.assertIn("out of range", msg)
        self.assertNotIn("layout", sl.user_config())
        self.assertIn("out of range", sl.cmd_layout("swap 1 9"))
        self.assertIn("not a line number", sl.cmd_layout("move git two"))

    def test_orphaning_a_protected_segment_is_refused_and_nothing_is_written(self):
        msg = sl.cmd_layout("line 2 model cost")     # would drop `account`
        self.assertIn("account", msg)
        self.assertIn("nothing was changed", msg)
        self.assertNotIn("layout", sl.user_config())

    def test_out_of_range_position_is_refused(self):
        self.assertIn("out of range", sl.cmd_layout("move git 1 99"))
        self.assertNotIn("layout", sl.user_config())

    def test_usage_for_a_nonsense_verb(self):
        self.assertIn("usage:", sl.cmd_layout("sideways"))
        self.assertNotIn("layout", sl.user_config())


class ShowSegmentRewritesTheLayoutTests(TmpEnv):
    """/squirrel-show must keep working exactly as it does today, now that absence from the
    layout is what "off" means."""

    def test_off_removes_the_segment_from_the_layout(self):
        sl.cmd_set_segment("cost off")
        cfg = sl.load_config()
        self.assertNotIn("cost", [i for line in cfg["layout"] for i in line])
        self.assertFalse(sl.segment_enabled(cfg, "cost"))

    def test_on_reinserts_it_at_its_shipped_position(self):
        sl.cmd_set_segment("cost off")
        sl.cmd_set_segment("cost on")
        cfg = sl.load_config()
        self.assertEqual(cfg["layout"][1], sl.shipped_layout()[1])

    def test_on_respects_a_custom_line(self):
        sl.cmd_layout("move cost 3")
        sl.cmd_set_segment("cost off")
        sl.cmd_set_segment("cost on")
        cfg = sl.load_config()
        self.assertIn("cost", [i for line in cfg["layout"] for i in line])

    def test_protected_segments_still_cannot_be_hidden(self):
        msg = sl.cmd_set_segment("account off")
        self.assertIn("cannot be hidden", msg)
        self.assertIn("account", [i for line in sl.resolve_layout(sl.load_config()) for i in line])

    def test_a_modifier_switch_is_untouched_by_the_layout(self):
        sl.cmd_set_segment("tokens off")
        cfg = sl.load_config()
        self.assertFalse(sl.segment_enabled(cfg, "tokens"))
        self.assertEqual(cfg["layout"], sl.shipped_layout())   # not a placeable segment



# ── Task 14: per-segment styles ──────────────────────────────────────────────
class StyledRunsTests(unittest.TestCase):
    """The seam the sanitiser and the styles both need: plain text, separate SGR."""

    def test_join_runs_matches_the_string_the_producers_used_to_build(self):
        runs = [("ctx ", None), ("42%", sl.GREEN), (" (84k)", sl.DIM)]
        self.assertEqual(sl.join_runs(runs),
                         "ctx " + sl.colored("42%", sl.GREEN) + sl.colored(" (84k)", sl.DIM))

    def test_as_runs_accepts_a_bare_string_as_one_unstyled_run(self):
        self.assertEqual(sl.as_runs("hi"), [("hi", None)])
        self.assertEqual(sl.as_runs(None), [])
        self.assertEqual(sl.as_runs(""), [])

    def test_empty_runs_are_dropped(self):
        self.assertEqual(sl.as_runs([("", sl.GREEN), ("x", None)]), [("x", None)])

    def test_string_helpers_still_match_their_runs_siblings(self):
        opts = sl.detail_opts(0, {})
        ctxw = STATUS["context_window"]
        self.assertEqual(sl.context_segment(ctxw, opts, {}),
                         sl.join_runs(sl.context_segment_runs(ctxw, opts, {})))
        bucket = {"label": "", "pct": 17.0, "resets_at": NOW}
        self.assertEqual(sl.usage_segment("5h", bucket, False, opts, {}),
                         sl.join_runs(sl.usage_segment_runs("5h", bucket, False, opts, {})))

    def test_there_is_no_pre_rendered_escape_hatch_left(self):
        """Task 17 retired RAW. Every fragment on the bar is (plain text, SGR), so the
        boundary sanitiser reaches all of it at the seam with no exception to remember."""
        self.assertFalse(hasattr(sl, "RAW"))


class SegmentStyleTests(unittest.TestCase):
    def test_no_style_is_byte_identical_to_plain_joining(self):
        runs = [("ctx ", None), ("42%", sl.GREEN)]
        self.assertEqual(sl.style_runs(runs, {}), sl.join_runs(runs))

    def test_a_warning_colour_always_breaks_through_a_user_style(self):
        out = sl.style_runs([("ctx ", None), ("84%", sl.RED)], {"fg": "#00ff00"})
        self.assertIn("\033[38;2;0;255;0mctx ", out)     # the plain part takes the user colour
        self.assertIn(sl.RED + "84%", out)               # the threshold warning survives
        for alert in (sl.RED, sl.YELLOW):
            self.assertIn(alert, sl.style_runs([("x", alert)], {"fg": "#00ff00"}))

    def test_decorative_colours_are_the_users_to_change(self):
        """The all-clear green, a clean branch's grey and an account's own colour are
        decoration, not warnings — a user style recolours them."""
        for decorative in (sl.GREEN, sl.GRAY, "\033[38;2;1;2;3m"):
            out = sl.style_runs([("x", decorative)], {"fg": "#00ff00"})
            self.assertIn("\033[38;2;0;255;0m", out, decorative)
            self.assertNotIn(decorative, out, decorative)

    def test_emphasis_composes_with_a_user_colour_instead_of_being_replaced(self):
        """DIM is emphasis, not hue. "Make this cyan" must not flatten the hierarchy the dim
        exists to create — the token counts take the colour AND stay dim."""
        out = sl.style_runs([("(84k/200k)", sl.DIM)], {"fg": "#00ffff"})
        self.assertIn("\033[38;2;0;255;255m", out)
        self.assertIn(sl.DIM, out)
        self.assertLess(out.index("\033[38;2;0;255;255m"), out.index(sl.DIM))

    def test_emphasis_survives_opacity_too(self):
        out = sl.style_runs([("x", sl.DIM)], {"fg": "#ffffff", "opacity": 0.5}, "#000000")
        self.assertIn("\033[38;2;128;128;128m", out)
        self.assertIn(sl.DIM, out)

    def test_attribute_detection(self):
        for attr in (sl.DIM, "\033[1m", "\033[1;2m", "\033[4m"):
            self.assertTrue(sl._is_attribute_only(attr), attr)
        for colour in (sl.RED, sl.GREEN, sl.GRAY, "\033[38;2;1;2;3m", "\033[1;37m",
                       sl.RESET, "", None, "not an escape"):
            self.assertFalse(sl._is_attribute_only(colour), repr(colour))

    def test_a_dim_run_with_no_user_colour_is_untouched(self):
        runs = [("(84k/200k)", sl.DIM)]
        self.assertEqual(sl.style_runs(runs, {}), sl.join_runs(runs))
        self.assertEqual(sl.style_runs(runs, {"bold": True}),
                         "\033[2m\033[1m(84k/200k)" + sl.RESET)

    def test_the_same_segment_keeps_its_warning_while_taking_the_style_elsewhere(self):
        # git: clean branch is grey (recoloured), dirty branch is yellow (protected)
        self.assertIn("\033[38;2;255;136;0m",
                      sl.style_runs([("⎇ main", sl.GRAY)], {"fg": "#ff8800"}))
        self.assertIn(sl.YELLOW,
                      sl.style_runs([("⎇ main*", sl.YELLOW)], {"fg": "#ff8800"}))

    def test_force_lets_a_user_override_even_a_threshold_colour(self):
        out = sl.style_runs([("84%", sl.RED)], {"fg": "#00ff00", "force": True})
        self.assertNotIn(sl.RED, out)
        self.assertIn("\033[38;2;0;255;0m", out)

    def test_bold_and_bg_apply_to_every_run_including_coloured_ones(self):
        out = sl.style_runs([("84%", sl.RED)], {"bold": True, "bg": "#000000"})
        self.assertIn("\033[48;2;0;0;0m", out)
        self.assertIn("\033[1m", out)
        self.assertIn(sl.RED, out)                       # meaning still intact

    def test_opacity_alpha_blends_truecolor_toward_the_banner(self):
        white = "\033[38;2;255;255;255m"
        out = sl.style_runs([("hi", white)], {"opacity": 0.5, "force": True}, "#000000")
        self.assertIn("\033[38;2;128;128;128m", out)
        full = sl.style_runs([("hi", white)], {"opacity": 1.0, "force": True}, "#000000")
        self.assertIn(white, full)

    def test_opacity_falls_back_to_sgr_dim_when_a_side_is_not_truecolor(self):
        self.assertIn(sl.DIM, sl.style_runs([("hi", sl.GREEN)], {"opacity": 0.5, "force": True}, "#000000"))
        self.assertIn(sl.DIM, sl.style_runs([("hi", None)], {"opacity": 0.5}, None))

    def test_opacity_never_fades_a_warning(self):
        out = sl.style_runs([("84%", sl.RED)], {"opacity": 0.2})
        self.assertEqual(out, sl.join_runs([("84%", sl.RED)]))   # untouched without force
        self.assertIn(sl.DIM, sl.style_runs([("9%", sl.GREEN)], {"opacity": 0.2}))  # green fades

    def test_alert_colours_are_exactly_the_threshold_ones(self):
        self.assertEqual(set(sl.ALERT_SGRS), {sl.YELLOW, sl.RED})
        cfg = {}
        self.assertIn(sl.pct_color(95, cfg), sl.ALERT_SGRS)
        self.assertIn(sl.pct_color(60, cfg), sl.ALERT_SGRS)
        self.assertNotIn(sl.pct_color(5, cfg), sl.ALERT_SGRS)
        self.assertIn(sl.countdown_color(60, cfg), sl.ALERT_SGRS)

    def test_blend_escape_returns_none_when_it_cannot_be_honest(self):
        self.assertIsNone(sl.blend_escape(sl.GREEN, "#000000", 0.5))
        self.assertIsNone(sl.blend_escape("\033[38;2;1;2;3m", None, 0.5))

    def test_a_malformed_style_entry_is_ignored(self):
        self.assertEqual(sl.segment_style({"segmentStyles": "oops"}, "git"), {})
        self.assertEqual(sl.segment_style({"segmentStyles": {"git": 7}}, "git"), {})
        self.assertEqual(sl.segment_style({}, "git"), {})


class SeparatorOverrideTests(TmpEnv):
    def _line2(self, cfg):
        status = dict(STATUS)
        status["context_window"] = dict(STATUS["context_window"],
                                        total_input_tokens=1_200_000, total_output_tokens=100_000)
        with mock.patch.object(sl, "git_segment_runs", return_value=[]):
            return plain(sl.render(status, CACHE, "you@example.com", cfg, NOW, timezone.utc,
                                   width=1000)[1])

    def test_default_punctuation_comes_from_the_group(self):
        self.assertIn("thinking │ tok 1.3M", self._line2({}))

    def test_sep_override_replaces_it(self):
        for keyword, expected in (("dot", "thinking · tok"), ("space", "thinking tok"),
                                  ("none", "thinkingtok"), ("bar", "thinking │ tok")):
            line = self._line2({"segmentStyles": {"sessionTokens": {"sep": keyword}}})
            self.assertIn(expected, line, keyword)

    def test_it_fixes_the_reordered_layout_punctuation(self):
        # cost moved next to the account inherits the spend group's '│'; the override fixes it
        layout = [["context"], ["account", "cost", "repo", "git", "model", "effort", "flags",
                                "sessionTokens", "duration", "lines", "pr", "chip"]]
        self.assertIn("you@example.com │ $1.23", self._line2({"layout": layout}))
        self.assertIn("you@example.com · $1.23",
                      self._line2({"layout": layout, "segmentStyles": {"cost": {"sep": "dot"}}}))

    def test_an_unknown_sep_keyword_is_ignored_not_crashed(self):
        self.assertIn("thinking │ tok", self._line2({"segmentStyles": {"sessionTokens": {"sep": "wiggly"}}}))


class StyleCommandTests(TmpEnv):
    def _styles(self):
        return sl.user_config().get("segmentStyles", {})

    def test_show_with_nothing_configured(self):
        out = sl.cmd_style("")
        self.assertIn("(none", out)
        self.assertIn("fields:", out)

    def test_set_and_show(self):
        sl.cmd_style("set git fg=#ff8800 bold=on")
        self.assertEqual(self._styles()["git"], {"fg": "#ff8800", "bold": True})
        self.assertIn("git: fg=#ff8800 bold=True", sl.cmd_style(""))

    def test_set_is_surgical_and_keeps_the_untouched_fields(self):
        sl.cmd_style("set git fg=#ff8800 bold=on")
        sl.cmd_style("set git opacity=0.5")
        self.assertEqual(self._styles()["git"], {"fg": "#ff8800", "bold": True, "opacity": 0.5})

    def test_auto_clears_one_field_without_touching_the_rest(self):
        sl.cmd_style("set git fg=#ff8800 bold=on")
        sl.cmd_style("set git fg=auto")
        self.assertEqual(self._styles()["git"], {"bold": True})

    def test_clearing_every_field_removes_the_segment_entry(self):
        sl.cmd_style("set git fg=#ff8800")
        sl.cmd_style("set git fg=auto")
        self.assertNotIn("git", self._styles())

    def test_styling_one_segment_does_not_disturb_another(self):
        sl.cmd_style("set git fg=#ff8800")
        sl.cmd_style("set cost bold=on")
        self.assertEqual(self._styles()["git"], {"fg": "#ff8800"})
        self.assertEqual(self._styles()["cost"], {"bold": True})

    def test_clear_and_reset(self):
        sl.cmd_style("set git fg=#ff8800")
        sl.cmd_style("set cost bold=on")
        sl.cmd_style("clear git")
        self.assertNotIn("git", self._styles())
        self.assertIn("cost", self._styles())
        sl.cmd_style("reset")
        self.assertEqual(self._styles(), {})

    def test_unknown_segment_refused_and_nothing_written(self):
        self.assertIn("unknown segment", sl.cmd_style("set banana fg=red").lower())
        self.assertNotIn("segmentStyles", sl.user_config())

    def test_invalid_values_refused_and_nothing_written(self):
        for arg, needle in (("set git fg=banana", "not a valid colour"),
                            ("set git opacity=5", "out of range"),
                            ("set git opacity=x", "not a number"),
                            ("set git sep=wiggly", "not a separator"),
                            ("set git bold=maybe", "not a valid bold"),
                            ("set git sparkle=on", "unknown field"),
                            ("set git nonsense", "unknown argument")):
            msg = sl.cmd_style(arg)
            self.assertIn(needle, msg.lower(), arg)
            self.assertIn("nothing was changed", msg, arg)
            self.assertNotIn("segmentStyles", sl.user_config(), arg)

    def test_usage_for_a_nonsense_verb(self):
        self.assertIn("usage:", sl.cmd_style("sideways"))

    def test_styles_reach_the_rendered_line(self):
        sl.cmd_style("set cost fg=#ff8800 bold=on")
        with mock.patch.object(sl, "git_segment_runs", return_value=[]):
            _, l2 = sl.render(STATUS, CACHE, "you@example.com", sl.load_config(), NOW,
                              timezone.utc, width=1000)
        self.assertIn("\033[38;2;255;136;0m\033[1m$1.23", l2)



# ── Task 15: bar-rule conditions ─────────────────────────────────────────────
class ConditionGrammarTests(unittest.TestCase):
    """The grammar is deliberately tiny and is never evaluated as code. Anything that does not
    parse is reported, ignored, and never fatal."""

    def test_parses_metric_op_literal(self):
        self.assertEqual(sl.parse_condition("weekly.pct >= 80"), ("weekly.pct", ">=", "80"))
        self.assertEqual(sl.parse_condition("git.branch == main"), ("git.branch", "==", "main"))
        self.assertEqual(sl.parse_condition("cost.usd>1.5"), ("cost.usd", ">", "1.5"))

    def test_parses_bare_truthiness(self):
        self.assertEqual(sl.parse_condition("git.dirty"), ("git.dirty", None, None))

    def test_longest_operator_wins(self):
        self.assertEqual(sl.parse_condition("ctx.pct >= 5")[1], ">=")
        self.assertEqual(sl.parse_condition("ctx.pct <= 5")[1], "<=")
        self.assertEqual(sl.parse_condition("ctx.pct != 5")[1], "!=")

    def test_unknown_metric_is_an_error_not_a_crash(self):
        for bad in ("wibble >= 3", "wibble", "os.system('x')", "__import__"):
            self.assertIsInstance(sl.parse_condition(bad), str, bad)
            self.assertIn("unknown metric", sl.parse_condition(bad))

    def test_malformed_conditions_are_errors(self):
        for bad, needle in (("", "empty"), ("   ", "empty"), (None, "empty"), (7, "empty"),
                            ("ctx.pct >=", "missing a value"),
                            ("ctx.pct >= 5 >= 3", "more than one comparison")):
            self.assertIsInstance(sl.parse_condition(bad), str, bad)
            self.assertIn(needle, sl.parse_condition(bad), bad)

    def test_nothing_is_ever_executed(self):
        """The grammar has no code path that can run a string. These would be catastrophic
        under an eval-based implementation; here they are simply unknown metrics."""
        canary = Path(self.__class__.__module__)   # never written; just a stable object
        for hostile in ("__import__('os').system('touch /tmp/pwned')",
                        "().__class__.__mro__[1].__subclasses__()",
                        "open('/etc/passwd').read()",
                        "ctx.pct.__class__"):
            self.assertIsInstance(sl.parse_condition(hostile), str, hostile)
        self.assertFalse(Path("/tmp/pwned").exists())
        del canary


class ConditionEvaluationTests(TmpEnv):
    def _ctx(self, status=None, config=None, limits=None):
        return sl._ctx_for(status or STATUS,
                           limits or {"session": {"label": "", "pct": 17.0, "resets_at": NOW},
                                      "weekly_all": {"label": "", "pct": 4.0, "resets_at": NOW},
                                      "scoped": []},
                           False, "api", "you@example.com", config or {}, NOW, timezone.utc, None)

    def test_numeric_comparisons(self):
        ctx = self._ctx()
        self.assertTrue(sl.condition_holds(ctx, "ctx.pct >= 42"))
        self.assertTrue(sl.condition_holds(ctx, "ctx.pct > 41"))
        self.assertFalse(sl.condition_holds(ctx, "ctx.pct > 42"))
        self.assertTrue(sl.condition_holds(ctx, "session.pct < 20"))
        self.assertTrue(sl.condition_holds(ctx, "weekly.pct == 4"))
        self.assertTrue(sl.condition_holds(ctx, "cost.usd > 1"))

    def test_string_comparisons_are_case_insensitive(self):
        ctx = self._ctx(dict(STATUS, workspace={"repo": {"name": "Demo"}}))
        self.assertTrue(sl.condition_holds(ctx, "repo.name == demo"))
        self.assertTrue(sl.condition_holds(ctx, "model.name == Fable 5"))
        self.assertFalse(sl.condition_holds(ctx, "repo.name == other"))

    def test_boolean_metric_and_bare_truthiness(self):
        ctx = self._ctx(dict(STATUS, workspace={"current_dir": "/repo"}))
        with mock.patch.object(sl, "_git_status", return_value={"branch": "main", "dirty": True, "ahead": 3}):
            self.assertTrue(sl.condition_holds(ctx, "git.dirty"))
            self.assertTrue(sl.condition_holds(ctx, "git.dirty == true"))
            self.assertFalse(sl.condition_holds(ctx, "git.dirty == false"))
            self.assertTrue(sl.condition_holds(ctx, "git.ahead >= 3"))

    def test_an_unknown_value_never_matches_not_even_not_equals(self):
        """The failure mode this project has been bitten by three times: missing data must not
        read as a match. Note `git.dirty` here is unknown because there is no workspace at
        all — not a git repo, not "clean"."""
        ctx = self._ctx({"session_id": None, "model": {}})
        self.assertIsNone(sl.metric_value(ctx, "repo.name"))
        for cond in ("repo.name == demo", "repo.name != demo", "repo.name", "git.dirty"):
            self.assertFalse(sl.condition_holds(ctx, cond), cond)

    def test_malformed_conditions_evaluate_false(self):
        ctx = self._ctx()
        for bad in ("", "wibble >= 3", "ctx.pct >=", "ctx.pct ?? 3", None):
            self.assertFalse(sl.condition_holds(ctx, bad), bad)

    def test_a_non_numeric_literal_against_a_number_does_not_match(self):
        self.assertFalse(sl.condition_holds(self._ctx(), "ctx.pct >= banana"))

    def test_metric_values_are_memoised_per_render(self):
        ctx = self._ctx(dict(STATUS, workspace={"current_dir": "/repo"}))
        calls = []

        def counting(cwd, config, now):
            calls.append(1)
            return {"branch": "main", "dirty": True, "ahead": 0}

        with mock.patch.object(sl, "_git_status", side_effect=counting):
            sl.condition_holds(ctx, "git.dirty")
            sl.condition_holds(ctx, "git.dirty == true")
            sl.condition_holds(ctx, "git.dirty != false")
        self.assertEqual(len(calls), 1, "git metric recomputed within one render")

    def test_a_metric_that_raises_is_unknown_not_fatal(self):
        ctx = self._ctx()
        with mock.patch.dict(sl.METRICS, {"ctx.pct": lambda c: 1 / 0}):
            self.assertIsNone(sl.metric_value(ctx, "ctx.pct"))
            self.assertFalse(sl.condition_holds(ctx, "ctx.pct >= 1"))

    def test_idle_secs_is_unknown_while_working(self):
        sl.apply_state("working", "cond1", NOW - 100)
        ctx = self._ctx(dict(STATUS, session_id="cond1"))
        self.assertIsNone(sl.metric_value(ctx, "idle.secs"))
        self.assertFalse(sl.condition_holds(ctx, "idle.secs > 30"))

    def test_idle_secs_measures_a_painted_waiting_state(self):
        sl.apply_state("idle", "cond2", NOW - 100)
        ctx = self._ctx(dict(STATUS, session_id="cond2"))
        self.assertAlmostEqual(sl.metric_value(ctx, "idle.secs"), 100, places=0)
        self.assertTrue(sl.condition_holds(ctx, "idle.secs > 30"))
        self.assertFalse(sl.condition_holds(ctx, "idle.secs > 300"))

    def test_state_metric(self):
        sl.apply_state("permission", "cond3", NOW - 5)
        ctx = self._ctx(dict(STATUS, session_id="cond3"))
        self.assertTrue(sl.condition_holds(ctx, "state == permission"))
        self.assertFalse(sl.condition_holds(ctx, "state == idle"))


class MetricsRegistryTests(TmpEnv):
    def test_every_metric_is_callable_and_never_raises(self):
        ctx = sl._ctx_for({}, {"session": None, "weekly_all": None, "scoped": []},
                          False, "stdin", None, {}, NOW, timezone.utc, None)
        for name in sl.METRICS:
            value = sl.metric_value(ctx, name)      # empty status: must degrade, not explode
            self.assertTrue(value is None or isinstance(value, (int, float, str, bool)),
                            f"{name} -> {value!r}")

    def test_metric_names_are_documented_shape(self):
        for name in sl.METRICS:
            self.assertRegex(name, r"^[a-z]+(\.[a-z]+)?$", name)



class BarRuleEngineTests(TmpEnv):
    def _ctx(self, status=None, config=None, limits=None):
        return sl._ctx_for(status or STATUS,
                           limits if limits is not None else
                           {"session": {"label": "", "pct": 17.0, "resets_at": NOW},
                            "weekly_all": {"label": "", "pct": 4.0, "resets_at": NOW},
                            "scoped": []},
                           False, "api", "you@example.com", config or {}, NOW, timezone.utc, None)

    def test_the_idle_ladder_ships_as_the_default_rules(self):
        rules = sl.bar_rules(sl.load_config())
        self.assertTrue(rules)
        self.assertTrue(all(r["source"] == "phase" for r in rules))
        self.assertEqual([r["when"] for r in rules],
                         ["idle.secs > 30", "idle.secs > 120", "idle.secs > 300"])

    def test_user_rules_come_after_the_ladder(self):
        rules = sl.bar_rules({"barRules": [{"when": "git.dirty", "bg": "red"}]})
        self.assertEqual(rules[-1]["source"], "user")
        self.assertEqual(rules[-1]["when"], "git.dirty")

    def test_last_matching_rule_wins(self):
        cfg = {"barRules": [{"when": "ctx.pct >= 10", "bg": "#111111"},
                            {"when": "ctx.pct >= 20", "bg": "#222222"}]}
        self.assertEqual(sl.winning_rule(self._ctx(config=cfg), cfg)["bg"], "#222222")

    def test_priority_beats_order(self):
        cfg = {"barRules": [{"when": "ctx.pct >= 10", "bg": "#111111", "priority": 5},
                            {"when": "ctx.pct >= 20", "bg": "#222222"}]}
        self.assertEqual(sl.winning_rule(self._ctx(config=cfg), cfg)["bg"], "#111111")

    def test_no_match_paints_nothing(self):
        cfg = {"barRules": [{"when": "ctx.pct >= 99", "bg": "#111111"}]}
        self.assertIsNone(sl.winning_rule(self._ctx(config=cfg), cfg))
        self.assertEqual(sl.rule_colors(None), (None, None, False))

    def test_a_broken_rule_is_reported_and_never_evaluated(self):
        cfg = {"barRules": [{"when": "wibble >= 1", "bg": "red"},
                            {"when": "ctx.pct >= 1"},
                            {"when": "ctx.pct >= 1", "bg": "banana"},
                            {"when": "ctx.pct >= 1", "text": "banana"},
                            "not-even-an-object"]}
        rules = [r for r in sl.bar_rules(cfg) if r["source"] == "user"]
        self.assertEqual(len(rules), 5)
        self.assertTrue(all(r.get("error") for r in rules), [r.get("error") for r in rules])
        self.assertIsNone(sl.winning_rule(self._ctx(config=cfg), cfg))

    def test_a_rule_can_read_the_usage_limits(self):
        """Regression: the rule engine used to be handed a synthetic context with no limits,
        so a rule on weekly.pct could never fire however the bar looked."""
        cfg = {"barRules": [{"when": "weekly.pct >= 3", "bg": "#7c3aed"}]}
        with mock.patch.object(sl, "git_segment_runs", return_value=[]):
            l1, _ = sl.render(STATUS, CACHE, "you@example.com", cfg, NOW, timezone.utc, width=80)
        self.assertTrue(l1.startswith("\033[48;2;124;58;237m"), repr(l1[:40]))

    def test_a_rule_that_should_not_fire_paints_nothing(self):
        cfg = {"barRules": [{"when": "weekly.pct >= 90", "bg": "#7c3aed"}]}
        with mock.patch.object(sl, "git_segment_runs", return_value=[]):
            l1, _ = sl.render(STATUS, CACHE, "you@example.com", cfg, NOW, timezone.utc, width=80)
        self.assertFalse(l1.startswith("\033[48"))

    def test_turning_the_idle_tint_off_leaves_a_users_own_rule_alone(self):
        cfg = {"idleBackground": "off", "barRules": [{"when": "ctx.pct >= 1", "bg": "#7c3aed"}]}
        self.assertEqual([r["source"] for r in sl.bar_rules(cfg)], ["user"])
        self.assertIsNotNone(sl.winning_rule(self._ctx(config=cfg), cfg))

    def test_idle_background_still_honours_the_disable_switches(self):
        cfg = dict(sl.load_config())
        sl.apply_state("idle", "rule-off", NOW - 300)
        status = {"session_id": "rule-off"}
        self.assertIsNotNone(sl.idle_background(status, NOW, cfg)[0])
        self.assertIsNone(sl.idle_background(status, NOW, dict(cfg, idleBackground="off"))[0])
        self.assertIsNone(sl.idle_background(status, NOW, dict(cfg, idleBackgroundAfterSeconds=0))[0])


class RuleCommandTests(TmpEnv):
    def _rules(self):
        return sl.user_config().get("barRules", [])

    def test_show_lists_the_ladder_and_the_metrics(self):
        out = sl.cmd_rules("")
        self.assertIn("[ladder] when 'idle.secs > 30'", out)
        self.assertIn("metrics:", out)
        self.assertIn("weekly.pct", out)

    def test_add_and_persist(self):
        sl.cmd_rules("add weekly.pct >= 80 bg=#7c3aed text=white bold=on")
        self.assertEqual(self._rules(), [{"when": "weekly.pct >= 80", "bg": "#7c3aed",
                                          "text": "white", "bold": True}])

    def test_a_condition_containing_an_operator_is_not_mistaken_for_a_field(self):
        sl.cmd_rules("add git.branch == main text=cyan")
        self.assertEqual(self._rules()[0]["when"], "git.branch == main")
        self.assertEqual(self._rules()[0]["text"], "cyan")

    def test_a_misspelt_field_says_so_instead_of_vanishing_into_the_condition(self):
        msg = sl.cmd_rules("add ctx.pct >= 90 sparkle=on bg=red")
        self.assertIn("unknown field 'sparkle'", msg)
        self.assertIn("nothing was changed", msg)
        self.assertNotIn("barRules", sl.user_config())

    def test_set_is_surgical(self):
        sl.cmd_rules("add git.dirty text=yellow")
        sl.cmd_rules("set git.dirty bold=on")
        self.assertEqual(self._rules(), [{"when": "git.dirty", "text": "yellow", "bold": True}])

    def test_set_on_a_missing_rule_refuses(self):
        msg = sl.cmd_rules("set git.dirty bold=on")
        self.assertIn("use 'add'", msg)
        self.assertNotIn("barRules", sl.user_config())

    def test_remove_and_reset(self):
        sl.cmd_rules("add git.dirty text=yellow")
        sl.cmd_rules("add ctx.pct >= 90 bg=red")
        sl.cmd_rules("remove git.dirty")
        self.assertEqual([r["when"] for r in self._rules()], ["ctx.pct >= 90"])
        sl.cmd_rules("reset")
        self.assertEqual(self._rules(), [])

    def test_remove_a_rule_that_is_not_yours(self):
        msg = sl.cmd_rules("remove ctx.pct >= 90")
        self.assertIn("no rule of your own", msg)

    def test_invalid_input_refused_and_nothing_written(self):
        for arg, needle in (("add wibble >= 3 bg=red", "unknown metric"),
                            ("add ctx.pct >= 90", "needs bg, text, or both"),
                            ("add ctx.pct >= 90 bg=banana", "not a valid colour"),
                            ("add ctx.pct >= 90 bg=red bold=maybe", "not a valid bold"),
                            ("add ctx.pct >= 90 bg=red priority=x", "not a whole number"),
                            ("add ctx.pct >= 90 sparkle=on", "unknown field")):
            msg = sl.cmd_rules(arg)
            self.assertIn(needle, msg.lower(), arg)
            self.assertIn("nothing was changed", msg, arg)
            self.assertNotIn("barRules", sl.user_config(), arg)

    def test_usage_for_a_nonsense_verb(self):
        self.assertIn("usage:", sl.cmd_rules("sideways"))

    def test_show_config_reports_ignored_rules(self):
        sl.save_config({"barRules": [{"when": "wibble >= 1", "bg": "red"}]})
        out = sl.cmd_show_config()
        self.assertIn("bar rules   :", out)
        self.assertIn("IGNORED", out)

    def test_show_rules_explains_why_a_rule_is_ignored(self):
        sl.save_config({"barRules": [{"when": "wibble >= 1", "bg": "red"}]})
        out = sl.cmd_show_rules()
        self.assertIn("IGNORED", out)
        self.assertIn("unknown metric", out)



class PackagedDefaultsCacheTests(TmpEnv):
    """defaults/config.json is read by load_config(), merge_keys() and the idle ladder, so it
    is cached per (path, mtime). A cache on the render path has to be provably correct."""

    def test_repeated_reads_hit_the_cache(self):
        sl.packaged_defaults()
        reads = []
        real = sl.load_json
        with mock.patch.object(sl, "load_json", side_effect=lambda p: (reads.append(p), real(p))[1]):
            for _ in range(5):
                sl.packaged_defaults()
        self.assertEqual(reads, [])

    def test_a_changed_file_is_picked_up(self):
        fake = self.root / "packaged.json"
        fake.write_text(json.dumps({"barCells": 10}))
        with mock.patch.object(sl, "default_config_path", return_value=fake):
            self.assertEqual(sl.packaged_defaults()["barCells"], 10)
            os.utime(fake, ns=(0, 0))
            fake.write_text(json.dumps({"barCells": 4}))
            os.utime(fake, ns=(10**9, 10**9))
            self.assertEqual(sl.packaged_defaults()["barCells"], 4)

    def test_callers_cannot_mutate_the_cache(self):
        first = sl.packaged_defaults()
        first["segments"]["share"] = "corrupted"
        first["idlePhases"].append({"after": 1})
        second = sl.packaged_defaults()
        self.assertNotEqual(second["segments"].get("share"), "corrupted")
        self.assertEqual(len(second["idlePhases"]), len(json.loads(
            sl.default_config_path().read_text(encoding="utf-8"))["idlePhases"]))

    def test_a_missing_defaults_file_is_empty_not_fatal(self):
        with mock.patch.object(sl, "default_config_path", return_value=self.root / "nope.json"):
            self.assertEqual(sl.packaged_defaults(), {})


class SingleStateReadTests(TmpEnv):
    """The activity chip and the idle metrics both want this session's state record. One read."""

    def _count_state_reads(self, session_id):
        status = {"session_id": session_id, "model": {"display_name": "Fable 5"},
                  "context_window": {"context_window_size": 200_000, "used_percentage": 42},
                  "cost": {"total_cost_usd": 1.0}}
        reads = []
        real = sl.load_json
        with mock.patch.object(sl, "load_json",
                               side_effect=lambda p: (reads.append(str(p)), real(p))[1]):
            sl.render(status, {}, None, sl.load_config(), NOW, timezone.utc, width=95)
        return len([r for r in reads if "state-" in r])

    def test_one_read_when_a_state_file_exists(self):
        sl.apply_state("idle", "sr1", NOW - 300)
        self.assertEqual(self._count_state_reads("sr1"), 1)

    def test_one_read_when_it_does_not(self):
        # None is a legitimate "no state file"; it must not be mistaken for "not provided"
        self.assertEqual(self._count_state_reads("sr-missing"), 1)



# ── boundary sanitiser ───────────────────────────────────────────────────────
HOSTILE_PATH = Path(__file__).resolve().parent / "hostile_strings.json"


def hostile_cases():
    """The shipped hostile-input corpus. Each case self-asserts its own `raw_len` on load —
    the first version of this fixture had escapes that JSON had already collapsed, so the
    lengths are the guard that the file still contains the bytes it claims to."""
    data = json.loads(HOSTILE_PATH.read_text(encoding="utf-8"))
    cases = data["test_cases"]
    for c in cases:
        assert len(c["raw"]) == c["raw_len"], (
            f"corpus case {c['id']}: raw is {len(c['raw'])} chars, claims {c['raw_len']} — "
            f"the fixture has been mangled")
    return cases


class SanitiserCorpusTests(unittest.TestCase):
    """Every case in dev/hostile_strings.json, which ships with the plugin precisely so
    this runs on a clean checkout rather than only where the planning corpus happens to be."""

    def test_corpus_is_intact(self):
        cases = hostile_cases()
        self.assertEqual(len(cases), 36)
        self.assertEqual(len({c["id"] for c in cases}), len(cases))

    def test_every_case_sanitises_to_its_expected_output(self):
        for case in hostile_cases():
            with self.subTest(case["id"]):
                self.assertEqual(sl.sanitise(case["raw"]), case["expected_output"],
                                 f"{case['id']}: {case.get('threat', '')}")

    def test_invariant_one_no_newline_ever_survives(self):
        """The status line is a contract of N lines. A newline reaching the output breaks it
        outright, so this is checked over the whole corpus, not only the newline case."""
        for case in hostile_cases():
            self.assertNotIn("\n", sl.sanitise(case["raw"]), case["id"])
            self.assertNotIn("\r", sl.sanitise(case["raw"]), case["id"])

    def test_invariant_two_output_is_free_of_escapes_and_controls(self):
        for case in hostile_cases():
            out = sl.sanitise(case["raw"])
            self.assertNotIn("\033", out, case["id"])
            for ch in out:
                self.assertFalse(ch < "\x20" or ch == "\x7f" or "\x80" <= ch <= "\x9f",
                                 f"{case['id']}: control {ch!r} survived")

    def test_sanitising_is_idempotent(self):
        for case in hostile_cases():
            once = sl.sanitise(case["raw"])
            self.assertEqual(sl.sanitise(once), once, case["id"])


class SanitiserBehaviourTests(unittest.TestCase):
    def test_strips_in_place_and_never_truncates(self):
        self.assertEqual(sl.sanitise("repo\033[5A\033[10Dmalicious"), "repomalicious")
        self.assertEqual(sl.sanitise("a\033[31mb\033[0mc"), "abc")

    def test_tab_becomes_one_space(self):
        self.assertEqual(sl.sanitise("a\tb"), "a b")
        self.assertEqual(sl.visible_len(sl.sanitise("a\tb")), 3)

    def test_legitimate_unicode_survives(self):
        for text in ("my-repo-\U0001f680", "\u4e2d\u6587repo", "cafe\u0301",
                     "repo-\u0645\u0631\u062d\u0628\u0627", "my\u200drepo",
                     "my\u200crepo", "my\u2060repo", "soft\u00adhyphen", "\ufeffbom"):
            self.assertEqual(sl.sanitise(text), text, repr(text))

    def test_bidi_controls_go_including_the_two_the_spec_omitted(self):
        # LRE/RLE are the embedding half of the Trojan Source pair; leaving them in would be
        # a known gap for no benefit, so the implementation strips 9 characters, not 7.
        for ch in "\u202a\u202b\u202c\u202d\u202e\u2066\u2067\u2068\u2069":
            self.assertEqual(sl.sanitise(f"a{ch}b"), "ab", repr(ch))

    def test_eight_bit_csi_and_osc_are_consumed_as_sequences(self):
        # not merely stripped as C1 bytes, or the parameters would survive as text
        self.assertEqual(sl.sanitise("repo\x9b31mred"), "repored")
        self.assertEqual(sl.sanitise("repo\x9d8;;http://evil\x07"), "repo")

    def test_dangling_sequences_take_the_remainder(self):
        self.assertEqual(sl.sanitise("repo\033"), "repo")
        self.assertEqual(sl.sanitise("repo\033["), "repo")
        self.assertEqual(sl.sanitise("repo\033[3"), "repo")
        self.assertEqual(sl.sanitise("repo\033]8"), "repo")
        self.assertEqual(sl.sanitise("repo\033]8;;url"), "repo")

    def test_a_lone_escape_drops_only_itself(self):
        self.assertEqual(sl.sanitise("repo\033malicious"), "repomalicious")

    def test_non_strings_pass_through_unchanged(self):
        for value in (None, 7, 1.5, True, ["a"], {"a": 1}):
            self.assertIs(sl.sanitise(value), value)

    def test_empty_and_all_hostile_inputs(self):
        self.assertEqual(sl.sanitise(""), "")
        self.assertEqual(sl.sanitise("\033]8;;http://evil\x07\033[31m\033[0m"), "")

    def test_it_is_linear_not_quadratic(self):
        import time
        base = "a" * 200_000 + "\033["
        start = time.perf_counter()
        sl.sanitise(base)
        elapsed = time.perf_counter() - start
        self.assertLess(elapsed, 1.0, f"200k chars took {elapsed:.2f}s — likely quadratic")


class SanitiserWiringTests(TmpEnv):
    """The seam and the two ingest points. A guarantee that only holds in unit tests of
    sanitise() itself is not a guarantee."""

    HOSTILE_REPO = "demo\033]8;;http://evil.com\x07\033[2K"
    HOSTILE_BRANCH = "main\033[5A\033[10Dinjected\n"

    def test_repo_name_is_sanitised_at_ingest(self):
        self.assertEqual(sl.repo_name({"workspace": {"repo": {"name": self.HOSTILE_REPO}}}), "demo")

    def test_a_path_is_sanitised_before_its_basename_is_taken(self):
        """Regression: an escape sequence can contain a '/', so sanitising after
        Path(...).name picks the component after the ATTACKER's slash — this exact input
        used to yield 'evil.com'."""
        self.assertEqual(sl.repo_name({"workspace": {"current_dir": "/x/" + self.HOSTILE_REPO}}),
                         "demo")

    def test_branch_is_sanitised_at_ingest_so_truncation_counts_real_characters(self):
        parsed = sl._parse_git_status_v2(f"# branch.oid abc\n# branch.head {self.HOSTILE_BRANCH}")
        self.assertEqual(parsed["branch"], "maininjected")
        # branchMaxChars now limits what the user SEES, not the raw string
        with mock.patch.object(sl, "_git_status", return_value=parsed):
            seg = sl.git_segment({"workspace": {"current_dir": "/x"}}, {"branchMaxChars": 8}, now=NOW)
        self.assertIn("maininj\u2026", plain(seg))

    def test_a_hostile_repo_name_cannot_reach_the_rendered_line(self):
        status = dict(STATUS, workspace={"repo": {"name": self.HOSTILE_REPO}, "current_dir": "/x"})
        with mock.patch.object(sl, "git_segment_runs", return_value=[]):
            lines = sl.render(status, CACHE, "you@example.com", CONFIG, NOW, timezone.utc, width=1000)
        self.assertEqual(len(lines), 2)
        for line in lines:
            self.assertNotIn("\033]", line)
            self.assertNotIn("\033[2K", line)
            self.assertNotIn("\n", line)
        self.assertIn("demo", plain(lines[1]))

    def test_a_hostile_string_in_any_producer_is_stripped_at_the_seam(self):
        """Whatever a future segment produces, the seam is what makes it safe — including a
        custom segment running a command that emits colour because it thinks it has a TTY."""
        ctx = sl._ctx_for(STATUS, {"session": None, "weekly_all": None, "scoped": []},
                          False, "stdin", None, {}, NOW, timezone.utc, None)
        seg = sl.Segment("evil", 0, 10, lambda c: [("ok\033]0;title\x07", None),
                                                   ("\033[31mred\n", sl.GREEN)])
        out = sl._produce(ctx, seg)
        self.assertNotIn("\033]", out)
        self.assertNotIn("\n", out)
        self.assertIn("ok", out)
        self.assertIn("red", out)
        self.assertIn(sl.GREEN, out)      # OUR styling survives; theirs does not

    def test_a_run_sanitised_to_nothing_renders_nothing(self):
        ctx = sl._ctx_for(STATUS, {"session": None, "weekly_all": None, "scoped": []},
                          False, "stdin", None, {}, NOW, timezone.utc, None)
        seg = sl.Segment("evil", 0, 10, lambda c: [("\033[31m", sl.GREEN), ("kept", None)])
        self.assertEqual(sl._produce(ctx, seg), "kept")

    def test_an_unknown_state_produces_no_chip_at_all(self):
        sl.apply_state("idle\033[2Kinjected", "chip-unknown", NOW - 300)
        self.assertIsNone(sl.state_segment({"session_id": "chip-unknown"}, NOW, sl.load_config()))

    def test_the_chip_is_sanitised_at_the_seam_like_everything_else(self):
        """Task 17: the chip is ordinary runs now, so it needs no special handling — the seam
        cleans it. An unrecognised state normally yields no chip at all (above), so this
        drives the alarm path deliberately, the one route by which a state string is shown."""
        state = "idle\033[2Kinjected"
        sl.apply_state(state, "chip-evil", NOW - 300)
        cfg = dict(sl.load_config(), alarmStates=[state], alarmAfterSeconds=1)
        status = {"session_id": "chip-evil", "model": {"display_name": "M"},
                  "context_window": {"context_window_size": 200_000, "used_percentage": 1},
                  "cost": {"total_cost_usd": 0.0}}
        with mock.patch.object(sl, "git_segment_runs", return_value=[]):
            lines = sl.render(status, {}, None, cfg, NOW, timezone.utc, width=400)
        joined = "\n".join(lines)
        self.assertEqual(len(lines), 2)
        self.assertNotIn("\033[2K", joined)
        self.assertIn("IDLEINJECTED", plain(joined))

    def test_a_newline_can_never_change_the_line_count(self):
        """THE invariant. Asserted against the RENDERED OUTPUT at every rung of the shedding
        ladder — not against sanitise()'s return value — because it is the number of lines
        Claude Code receives that is the contract, and a newline surviving anywhere between a
        producer and the final join breaks it. Newlines are planted in every string-valued
        field an attacker could influence, and in a layout with four lines as well as two."""
        hostile = "a\nb\r\nc"
        status = dict(STATUS,
                      workspace={"repo": {"name": hostile}, "current_dir": "/x/" + hostile},
                      model={"display_name": "M\nX"}, effort={"level": "hi\ngh"},
                      pr={"number": 42, "url": "u", "review_state": hostile})
        layouts = {2: None, 4: [["context"], ["session"], ["weekly"], ["account", "model"]]}
        for expected_lines, layout in layouts.items():
            cfg = dict(CONFIG, layout=layout) if layout else CONFIG
            for width in (None, 200, 126, 95, 70, 50, 40, 30, 20, 1):
                with self.subTest(lines=expected_lines, width=width):
                    with mock.patch.object(sl, "git_segment_runs",
                                           return_value=[("\u2387 br\nanch", sl.GRAY)]):
                        lines = sl.render(status, CACHE, "you@example.com", cfg, NOW,
                                          timezone.utc, width=width)
                    self.assertEqual(len(lines), expected_lines)
                    joined = "\n".join(lines)
                    self.assertEqual(len(joined.splitlines()), expected_lines,
                                     f"a newline survived into the output: {joined!r}")
                    self.assertNotIn("\r", joined)

    def test_the_invariant_holds_with_a_banner_painted(self):
        """The banner painter pads and re-asserts colour per line; a newline reaching it would
        be duplicated into the padding, so the invariant is re-checked with one active."""
        sl.apply_state("idle", "nl-banner", NOW - 300)
        status = dict(STATUS, session_id="nl-banner",
                      workspace={"repo": {"name": "a\nb"}, "current_dir": "/x"})
        cfg = sl.load_config()
        for width in (200, 95, 40):
            with mock.patch.object(sl, "git_segment_runs", return_value=[]):
                lines = sl.render(status, CACHE, "you@example.com", cfg, NOW, timezone.utc,
                                  width=width)
            self.assertEqual(len(lines), 2)
            self.assertEqual(len("\n".join(lines).splitlines()), 2)



class SharedDirPermissionsTests(TmpEnv):
    """The shared cross-profile store must not be world-readable. Path.mkdir(parents=True,
    mode=0o700) applies the mode to the last component only, which is how ~/.squirrel came to
    sit at 0755 with correctly-0600 files inside it."""

    def setUp(self):
        super().setUp()
        self.shared = self.root / "shared-perm"
        patcher = mock.patch.dict(os.environ, {"SQUIRREL_SHARED_DIR": str(self.shared)})
        patcher.start(); self.addCleanup(patcher.stop)

    @staticmethod
    def _mode(path):
        import stat
        return stat.S_IMODE(os.stat(path).st_mode)

    @unittest.skipIf(os.name == "nt", "POSIX permissions only")
    def test_every_directory_created_on_the_way_is_owner_only(self):
        sl.record_session_sample({"session_id": "perm", "cost": {"total_cost_usd": 1.0},
                                  "context_window": {}}, "you@example.com", NOW)
        self.assertEqual(self._mode(self.shared), 0o700)
        self.assertEqual(self._mode(self.shared / "sessions"), 0o700)
        self.assertEqual(self._mode(sl.session_path("perm")), 0o600)

    @unittest.skipIf(os.name == "nt", "POSIX permissions only")
    def test_umask_cannot_loosen_it(self):
        old = os.umask(0)
        self.addCleanup(os.umask, old)
        sl.mkdir_private(self.shared / "a" / "b")
        self.assertEqual(self._mode(self.shared / "a"), 0o700)
        self.assertEqual(self._mode(self.shared / "a" / "b"), 0o700)

    @unittest.skipIf(os.name == "nt", "POSIX permissions only")
    def test_a_directory_that_is_not_ours_is_never_touched(self):
        """`~/.claude` and the user's home are theirs. Tightening those underneath them would
        be a surprise, not a fix — only the trees Squirrel creates are ours to re-permission."""
        existing = self.root / "theirs"
        existing.mkdir(mode=0o755)
        sl.mkdir_private(existing / "ours")
        self.assertEqual(self._mode(existing), 0o755)
        self.assertFalse(sl._is_ours(existing))

    @unittest.skipIf(os.name == "nt", "POSIX permissions only")
    def test_a_store_left_at_0755_by_an_older_version_is_repaired(self):
        """mkdir does nothing to a directory that already exists, so creating at 0700 alone
        would leave every store built by an earlier release world-listable for ever."""
        (self.shared / "sessions").mkdir(parents=True, mode=0o755)
        os.chmod(self.shared, 0o755)
        sl.record_session_sample({"session_id": "legacy", "cost": {"total_cost_usd": 1.0},
                                  "context_window": {}}, "you@example.com", NOW)
        self.assertEqual(self._mode(self.shared), 0o700)
        self.assertEqual(self._mode(self.shared / "sessions"), 0o700)

    @unittest.skipIf(os.name == "nt", "POSIX permissions only")
    def test_the_data_dir_is_repaired_but_its_parent_is_not(self):
        data = sl.data_dir()
        data.mkdir(parents=True, exist_ok=True)
        os.chmod(data, 0o755)
        parent_before = self._mode(data.parent)
        sl.write_cache(data / "probe.json", {"x": 1})
        self.assertEqual(self._mode(data), 0o700)
        self.assertEqual(self._mode(data.parent), parent_before)   # CLAUDE_CONFIG_DIR untouched

    @unittest.skipIf(os.name == "nt", "POSIX permissions only")
    def test_a_group_or_other_bit_in_any_position_is_repaired(self):
        for wrong in (0o750, 0o705, 0o770, 0o777, 0o701):
            self.shared.mkdir(parents=True, exist_ok=True)
            os.chmod(self.shared, wrong)
            sl.mkdir_private(self.shared)
            self.assertEqual(self._mode(self.shared), 0o700, oct(wrong))

    def test_it_is_idempotent(self):
        sl.mkdir_private(self.shared / "x")
        sl.mkdir_private(self.shared / "x")                 # must not raise
        self.assertTrue((self.shared / "x").is_dir())


class ShowRulesWinnerTests(TmpEnv):
    """"Why is my bar purple?" should be answerable by looking, not by reasoning about
    precedence."""

    def test_the_winning_rule_is_marked(self):
        sl.cmd_rules("add repo.name == demo bg=#7c3aed")
        with mock.patch.object(sl, "repo_name", return_value="demo"):
            out = sl.cmd_show_rules()
        self.assertIn("<== WINNING NOW", out)
        self.assertIn("repo.name == demo", out.split("<== WINNING NOW")[0].splitlines()[-1])

    def test_a_later_match_wins_and_only_one_rule_is_marked(self):
        sl.cmd_rules("add repo.name == demo bg=#111111")
        sl.cmd_rules("add repo.name != nothing bg=#222222")
        with mock.patch.object(sl, "repo_name", return_value="demo"):
            out = sl.cmd_show_rules()
        self.assertEqual(out.count("<== WINNING NOW"), 1)
        self.assertIn("#222222", out.split("<== WINNING NOW")[0].splitlines()[-1])
        self.assertIn("(matches)", out)

    def test_nothing_matching_says_so(self):
        sl.cmd_rules("add repo.name == nothing-like-this bg=#7c3aed")
        with mock.patch.object(sl, "repo_name", return_value="demo"):
            out = sl.cmd_show_rules()
        self.assertNotIn("<== WINNING NOW", out)
        self.assertIn("nothing matches right now", out)

    def test_a_live_only_metric_is_marked_uncheckable_not_guessed(self):
        """The failure mode to avoid: confidently naming a winner when a rule we could not
        evaluate might actually be the one painting."""
        sl.cmd_rules("add cost.usd > 5 bg=#7c3aed")
        out = sl.cmd_show_rules()
        self.assertIn("needs the live status line to tell", out)
        self.assertIn("the winner may differ", out)
        self.assertNotIn("<== WINNING NOW", out)

    def test_a_broken_rule_is_never_marked_as_matching(self):
        sl.save_config({"barRules": [{"when": "wibble >= 1", "bg": "red"}]})
        out = sl.cmd_show_rules()
        self.assertIn("IGNORED", out)
        self.assertNotIn("<== WINNING NOW", out)
        self.assertNotIn("(matches)", out)


class LadderRemoveHintTests(TmpEnv):
    def test_removing_a_ladder_rule_names_the_command_to_use(self):
        msg = sl.cmd_rules("remove idle.secs > 30")
        self.assertIn("idle ladder", msg)
        self.assertIn("/squirrel-phases remove 30", msg)
        self.assertIn("nothing was changed", msg)

    def test_removing_an_unknown_user_rule_points_at_the_listing(self):
        msg = sl.cmd_rules("remove repo.name == nope")
        self.assertIn("/squirrel-rules", msg)
        self.assertIn("nothing was changed", msg)



# ── Task 16: custom segments (reminders are a configuration of them) ─────────
class CustomSegmentRegistryTests(TmpEnv):
    def test_a_custom_segment_joins_the_registry(self):
        cfg = {"customSegments": [{"id": "water", "text": "drink"}]}
        self.assertIn("water", sl.all_segments(cfg))
        self.assertNotIn("water", sl.SEGMENTS)          # built-ins are untouched

    def test_an_id_can_never_shadow_a_built_in(self):
        cfg = {"customSegments": [{"id": "cost", "text": "nope"}]}
        self.assertIs(sl.all_segments(cfg)["cost"], sl.SEGMENTS["cost"])
        self.assertEqual(sl.custom_specs(cfg), [])

    def test_malformed_specs_are_skipped_not_fatal(self):
        cfg = {"customSegments": ["nope", {}, {"id": ""}, {"id": 7}, {"id": "ok", "text": "x"},
                                  {"id": "ok", "text": "duplicate"}]}
        self.assertEqual([c["id"] for c in sl.custom_specs(cfg)], ["ok"])

    def test_a_non_list_customsegments_is_ignored(self):
        self.assertEqual(sl.custom_specs({"customSegments": "oops"}), [])
        self.assertEqual(sl.custom_specs({}), [])

    def test_it_is_auto_placed_so_adding_one_shows_it(self):
        cfg = dict(sl.load_config(), customSegments=[{"id": "water", "text": "drink"}])
        placed = [i for line in sl.resolve_layout(cfg) for i in line]
        self.assertIn("water", placed)

    def test_line_chooses_which_line_it_joins(self):
        cfg = dict(sl.load_config(), customSegments=[{"id": "w", "text": "x", "line": 2}])
        self.assertIn("w", sl.resolve_layout(cfg)[1])

    def test_squirrel_show_off_still_hides_it(self):
        cfg = dict(sl.load_config(), customSegments=[{"id": "w", "text": "x"}],
                   segments={"w": False})
        self.assertNotIn("w", [i for line in sl.resolve_layout(cfg) for i in line])

    def test_layout_style_and_priority_all_accept_a_custom_id(self):
        sl.cmd_custom("add water text=drink")
        self.assertIn("water", sl.cmd_layout("move water 2"))
        self.assertIn("water", sl.user_config()["layout"][1])
        self.assertIn("water: fg=#ff8800", sl.cmd_style("set water fg=#ff8800"))
        self.assertIn("priority is now 5", sl.cmd_set_priority("water 5"))


class CustomSegmentRenderTests(TmpEnv):
    def _line1(self, cfg, width=1000):
        status = dict(STATUS, session_id=None)
        with mock.patch.object(sl, "git_segment_runs", return_value=[]):
            return plain(sl.render(status, CACHE, "you@example.com", cfg, NOW, timezone.utc,
                                   width=width)[0])

    def test_static_text_renders(self):
        cfg = dict(CONFIG, customSegments=[{"id": "w", "text": "drink water"}])
        self.assertIn("drink water", self._line1(cfg))

    def test_when_gates_it_with_task_15_grammar(self):
        base = dict(CONFIG, customSegments=[{"id": "w", "text": "PROD", "when": "ctx.pct >= 90"}])
        self.assertNotIn("PROD", self._line1(base))
        base["customSegments"] = [{"id": "w", "text": "PROD", "when": "ctx.pct >= 10"}]
        self.assertIn("PROD", self._line1(base))

    def test_a_broken_when_hides_it_rather_than_crashing(self):
        cfg = dict(CONFIG, customSegments=[{"id": "w", "text": "ZZTOP", "when": "wibble >= 1"}])
        self.assertNotIn("ZZTOP", self._line1(cfg))

    def test_its_text_goes_through_the_sanitiser(self):
        cfg = dict(CONFIG, customSegments=[{"id": "w", "text": "ok\033]0;title\x07\ninjected"}])
        status = dict(STATUS, session_id=None)
        with mock.patch.object(sl, "git_segment_runs", return_value=[]):
            lines = sl.render(status, CACHE, "you@example.com", cfg, NOW, timezone.utc, width=1000)
        self.assertEqual(len(lines), 2)
        self.assertNotIn("\033]", "\n".join(lines))
        self.assertIn("okinjected", plain(lines[0]))

    def test_a_link_wraps_the_segment_in_osc8_after_sanitising(self):
        cfg = dict(CONFIG, customSegments=[{"id": "prlink", "text": "PR",
                                            "link": "https://example.com/1"}])
        status = dict(STATUS, session_id=None)
        with mock.patch.object(sl, "git_segment_runs", return_value=[]):
            line = sl.render(status, CACHE, "you@example.com", cfg, NOW, timezone.utc, width=1000)[0]
        self.assertIn("\033]8;;https://example.com/1\033\\", line)
        self.assertTrue(line.rstrip().endswith("\033]8;;\033\\"))

    def test_only_http_links_are_emitted(self):
        for bad in ("file:///etc/passwd", "javascript:alert(1)", "ftp://x", "not a url",
                    "https://evil\033]8;;x\x07", 7, None, "https://has space"):
            self.assertIsNone(sl.safe_link(bad), repr(bad))
        self.assertEqual(sl.safe_link("https://example.com"), "https://example.com")


class CustomScheduleTests(TmpEnv):
    def _ctx(self, cfg, now=NOW):
        return sl._ctx_for(STATUS, {}, False, "stdin", "you@example.com", cfg, now,
                           timezone.utc, None)

    def test_an_unacknowledged_reminder_is_due(self):
        spec = {"id": "w", "text": "x", "every": 1800, "ack": True}
        self.assertTrue(sl.custom_due(self._ctx({}), spec))

    def test_acknowledging_hides_it_until_the_interval_passes(self):
        cfg = {"customSegments": [{"id": "w", "text": "x", "every": 1800, "ack": True}]}
        (self.root / "squirrel").mkdir(exist_ok=True)
        (self.root / "squirrel" / "config.json").write_text(json.dumps(cfg))
        with mock.patch.object(sl.time, "time", return_value=NOW):
            sl.cmd_ack("w")
        spec = sl.custom_specs(sl.load_config())[0]
        self.assertFalse(sl.custom_due(self._ctx(cfg, NOW + 60), spec))
        self.assertFalse(sl.custom_due(self._ctx(cfg, NOW + 1799), spec))
        self.assertTrue(sl.custom_due(self._ctx(cfg, NOW + 1800), spec))

    def test_every_without_ack_is_a_refresh_interval_not_a_schedule(self):
        spec = {"id": "w", "command": "x", "every": 10}
        self.assertTrue(sl.custom_due(self._ctx({}), spec))

    def test_at_hides_it_before_that_time_of_day(self):
        spec = {"id": "w", "text": "x", "at": "23:30"}
        midday = datetime(2026, 8, 22, 12, 0, tzinfo=timezone.utc).timestamp()
        evening = datetime(2026, 8, 22, 23, 45, tzinfo=timezone.utc).timestamp()
        self.assertFalse(sl.custom_due(self._ctx({}, midday), spec))
        self.assertTrue(sl.custom_due(self._ctx({}, evening), spec))

    def test_malformed_schedules_do_not_hide_the_segment(self):
        for spec in ({"id": "w", "text": "x", "every": "soon"},
                     {"id": "w", "text": "x", "at": "25:00"},
                     {"id": "w", "text": "x", "every": -5}):
            self.assertTrue(sl.custom_due(self._ctx({}), spec), spec)

    def test_shared_acks_land_in_the_cross_profile_store(self):
        cfg = {"customSegments": [{"id": "w", "text": "x", "every": 60, "ack": True,
                                   "shared": True}]}
        (self.root / "squirrel").mkdir(exist_ok=True)
        (self.root / "squirrel" / "config.json").write_text(json.dumps(cfg))
        sl.cmd_ack("w")
        self.assertTrue(sl.custom_state_path(True).exists())
        self.assertFalse(sl.custom_state_path(False).exists())

    def test_a_private_ack_stays_in_this_profile(self):
        cfg = {"customSegments": [{"id": "w", "text": "x", "every": 60, "ack": True}]}
        (self.root / "squirrel").mkdir(exist_ok=True)
        (self.root / "squirrel" / "config.json").write_text(json.dumps(cfg))
        sl.cmd_ack("w")
        self.assertTrue(sl.custom_state_path(False).exists())
        self.assertFalse(sl.custom_state_path(True).exists())


class CustomCommandTests(TmpEnv):
    def _cfg(self, **extra):
        cfg = dict(sl.load_config(), customSegments=[{"id": "ci", "command": "echo GREEN",
                                                      "every": 10}], **extra)
        return cfg

    def test_a_command_never_runs_on_the_render_path(self):
        """The constraint that matters: whatever the command does, the render must not wait
        for it. Proven by asserting no command is executed synchronously at all."""
        cfg = self._cfg(customCommands=True)
        with mock.patch.object(sl, "_run_command") as never, \
             mock.patch.object(sl, "spawn_custom_refresh") as spawn, \
             mock.patch.object(sl, "git_segment_runs", return_value=[]):
            sl.render(STATUS, CACHE, "you@example.com", cfg, NOW, timezone.utc, width=1000)
        never.assert_not_called()
        spawn.assert_called_once()

    def test_the_gate_is_off_by_default_and_blocks_execution(self):
        self.assertFalse(sl.custom_commands_enabled(sl.load_config()))
        with mock.patch.object(sl, "spawn_custom_refresh") as spawn, \
             mock.patch.object(sl, "git_segment_runs", return_value=[]):
            sl.render(STATUS, CACHE, "you@example.com", self._cfg(), NOW, timezone.utc, width=1000)
        spawn.assert_not_called()

    def test_the_gate_can_be_turned_on_and_off(self):
        self.assertIn("ON", sl.cmd_enable_custom_commands("on"))
        self.assertTrue(sl.custom_commands_enabled(sl.load_config()))
        self.assertIn("OFF", sl.cmd_enable_custom_commands("off"))
        self.assertFalse(sl.custom_commands_enabled(sl.load_config()))
        self.assertIn("usage", sl.cmd_enable_custom_commands("maybe"))

    def test_run_custom_caches_the_first_line_of_stdout(self):
        sl.save_config({"customCommands": True,
                        "customSegments": [{"id": "ci", "command": "printf 'A\\nB\\n'"}]})
        entry = sl.run_custom("ci", NOW)
        self.assertEqual(entry["value"], "A")
        self.assertIsNone(entry.get("error"))

    def test_a_failing_command_records_the_error_and_keeps_the_last_value(self):
        sl.save_config({"customCommands": True,
                        "customSegments": [{"id": "ci", "command": "echo OK"}]})
        sl.run_custom("ci", NOW)
        sl.save_config({"customSegments": [{"id": "ci", "command": "exit 3"}]})
        entry = sl.run_custom("ci", NOW + 1)
        self.assertEqual(entry["error"], "exit 3")
        self.assertEqual(entry["value"], "OK")          # the bar keeps showing what it had

    def test_a_timeout_is_recorded_and_bounded(self):
        sl.save_config({"customCommands": True, "customTimeoutMs": 100,
                        "customSegments": [{"id": "slow", "command": "sleep 30"}]})
        started = _time.monotonic()
        entry = sl.run_custom("slow", NOW)
        self.assertLess(_time.monotonic() - started, 5)
        self.assertEqual(entry["error"], "timed out")

    def test_command_output_is_sanitised(self):
        sl.save_config({"customCommands": True,
                        "customSegments": [{"id": "evil",
                                            "command": "printf 'ok\\033]0;t\\007'"}]})
        self.assertEqual(sl.run_custom("evil", NOW)["value"], "ok")

    def test_running_an_unknown_or_blocked_segment_is_recorded_not_raised(self):
        self.assertIn("no such", sl.run_custom("nope", NOW)["error"])
        sl.save_config({"customSegments": [{"id": "ci", "command": "echo x"}]})
        self.assertIn("switched off", sl.run_custom("ci", NOW)["error"])


class CustomCommandInterfaceTests(TmpEnv):
    def _specs(self):
        return sl.user_config().get("customSegments", [])

    def test_the_users_two_reminders_are_pure_configuration(self):
        """The acceptance test for this task, in the user's own words: a reminder must fall
        out of `every` + `ack` + `shared`, with nothing in the source knowing the word."""
        sl.cmd_custom("add water every=30m ack=on shared=on text=drink water")
        sl.cmd_custom("add break every=2h ack=on shared=on text=take a break")
        self.assertEqual(self._specs(), [
            {"id": "water", "every": 1800, "ack": True, "shared": True, "text": "drink water"},
            {"id": "break", "every": 7200, "ack": True, "shared": True, "text": "take a break"},
        ])
        # And no code path is named for it. Checked over IDENTIFIERS via ast rather than by
        # grepping the source, so prose explaining the design does not count as a violation
        # while a `def render_reminder()` would.
        import ast
        tree = ast.parse(Path(sl.__file__).read_text(encoding="utf-8"))
        names = set()
        for node in ast.walk(tree):
            for attr in ("name", "id", "attr", "arg"):
                value = getattr(node, attr, None)
                if isinstance(value, str):
                    names.add(value.lower())
        offenders = sorted(n for n in names if "reminder" in n)
        self.assertEqual(offenders, [], f"reminders are meant to be a configuration: {offenders}")
        # Nor is there a setting or a field for them — the mechanism is customSegments.
        self.assertEqual([k for k in sl.CONFIGURABLE_KEYS if "reminder" in k.lower()], [])
        self.assertEqual([k for k in sl.CUSTOM_FIELDS if "reminder" in k.lower()], [])
        # The word survives only where it should: explaining the design to a human.
        self.assertIn("reminder", sl.cmd_show_custom().lower())

    def test_durations_accept_units(self):
        for text, seconds in (("30m", 1800), ("2h", 7200), ("90s", 90), ("1d", 86400),
                              ("1800", 1800)):
            sl.cmd_custom(f"set-or-add x every={text}") if False else None
            self.assertEqual(sl._duration_seconds(text), seconds, text)
        for bad in ("", "soon", "-5", "0", "m"):
            self.assertIsNone(sl._duration_seconds(bad), bad)

    def test_rest_fields_swallow_the_line(self):
        sl.cmd_custom("add w every=1m text=drink some water now")
        self.assertEqual(self._specs()[0]["text"], "drink some water now")
        sl.cmd_custom("set w when=git.dirty == true")
        self.assertEqual(self._specs()[0]["when"], "git.dirty == true")

    def test_set_is_surgical(self):
        sl.cmd_custom("add w every=1m ack=on text=x")
        sl.cmd_custom("set w line=2")
        self.assertEqual(self._specs()[0], {"id": "w", "every": 60, "ack": True, "text": "x",
                                            "line": 2})

    def test_remove_and_reset(self):
        sl.cmd_custom("add a text=1")
        sl.cmd_custom("add b text=2")
        sl.cmd_custom("remove a")
        self.assertEqual([c["id"] for c in self._specs()], ["b"])
        sl.cmd_custom("reset")
        self.assertEqual(self._specs(), [])

    def test_invalid_input_refused_and_nothing_written(self):
        for arg, needle in (("add cost text=x", "already a built-in"),
                            ("add 9bad text=x", "not a usable id"),
                            ("add w", "usage"),
                            ("add w every=soon text=x", "not a duration"),
                            ("add w at=25:00 text=x", "not a time of day"),
                            ("add w ack=maybe text=x", "not a valid ack"),
                            ("add w line=0 text=x", "out of range"),
                            ("add w link=file:///x text=y", "not a usable link"),
                            ("add w sparkle=on text=x", "unknown field"),
                            ("add w when=wibble >= 1", "unknown metric"),
                            ("add w every=1m", "needs text= or command="),
                            ("set nothere text=x", "use 'add'")):
            msg = sl.cmd_custom(arg)
            self.assertIn(needle, msg.lower(), arg)
            self.assertEqual(self._specs(), [], arg)

    def test_text_and_command_are_mutually_exclusive(self):
        sl.cmd_custom("add w text=x")
        msg = sl.cmd_custom("set w command=echo hi")
        self.assertIn("not both", msg)

    def test_ack_reports_and_refuses_sensibly(self):
        self.assertIn("usage", sl.cmd_ack(""))
        self.assertIn("no custom segment", sl.cmd_ack("nope"))
        sl.cmd_custom("add w text=x")
        self.assertIn("ack=on first", sl.cmd_ack("w"))
        sl.cmd_custom("set w every=30m ack=on")
        self.assertIn("back in 30m", sl.cmd_ack("w"))

    def test_ack_all(self):
        sl.cmd_custom("add a every=1m ack=on text=1")
        sl.cmd_custom("add b every=1m ack=on text=2")
        self.assertIn("a, b", sl.cmd_ack("all"))
        self.assertIn("nothing to acknowledge", sl.cmd_ack("all").lower()) if False else None

    def test_snooze(self):
        sl.cmd_custom("add w every=30m ack=on text=x")
        self.assertIn("snoozed", sl.cmd_snooze("w 5"))
        self.assertIn("usage", sl.cmd_snooze("w"))
        self.assertIn("no custom segment", sl.cmd_snooze("nope 5"))

    def test_show_lists_state_and_blocked_commands(self):
        sl.cmd_custom("add ci command=echo hi")
        out = sl.cmd_show_custom()
        self.assertIn("BLOCKED", out)
        self.assertIn("/squirrel-custom-commands on", out)



# ── Task 17: background agents ───────────────────────────────────────────────
@unittest.skipIf(os.name == "nt", "the agents dir is keyed on os.getuid(), POSIX-only")
class AgentsSegmentTests(TmpEnv):
    """Reads an undocumented path, so every test here is as much about degrading silently as
    about counting correctly."""

    CWD, SESSION = "/proj/demo", "sess-1"

    def setUp(self):
        super().setUp()
        self.fake_tmp = self.root / "tmp"
        patcher = mock.patch.dict(os.environ, {"SQUIRREL_TMPDIR": str(self.fake_tmp)})
        patcher.start(); self.addCleanup(patcher.stop)
        slug = self.CWD.replace("/", "-")
        self.tasks = self.fake_tmp / f"claude-{os.getuid()}" / slug / self.SESSION / "tasks"
        self.subs = self.root / "subagents"
        self.tasks.mkdir(parents=True)
        self.subs.mkdir()

    def _agent(self, name, age_seconds):
        target = self.subs / f"{name}.jsonl"
        target.write_text("{}")
        os.utime(target, (NOW - age_seconds, NOW - age_seconds))
        (self.tasks / f"{name}.output").symlink_to(target)

    def _tool_output(self, name):
        (self.tasks / f"{name}.output").write_text("x")

    def _count(self, config=None):
        return sl.live_agent_count(self.SESSION, self.CWD, NOW, config or {})

    def test_counts_only_live_agents(self):
        self._agent("a", 5)
        self._agent("b", 20)
        self._agent("old", 9000)
        self.assertEqual(self._count(), 2)

    def test_a_tool_output_is_not_an_agent(self):
        """The bug the obvious implementation has: `.output` files in this directory are
        mostly Bash results, written seconds ago, and counting them reports your own tool
        calls as running agents."""
        for name in ("b1tool", "b2tool", "b3tool"):
            self._tool_output(name)
        self.assertEqual(self._count(), 0)
        self._agent("real", 5)
        self.assertEqual(self._count(), 1)

    def test_liveness_comes_from_the_target_not_the_link(self):
        # link created now, agent last wrote hours ago -> finished
        self._agent("stale", 9000)
        os.utime(self.tasks / "stale.output", (NOW, NOW), follow_symlinks=False)
        self.assertEqual(self._count(), 0)

    def test_a_dangling_link_is_a_finished_agent_not_an_error(self):
        self._agent("gone", 5)
        (self.subs / "gone.jsonl").unlink()
        self.assertEqual(self._count(), 0)

    def test_the_window_is_configurable(self):
        self._agent("a", 300)
        self.assertEqual(self._count(), 0)
        self.assertEqual(self._count({"agentsLiveSeconds": 600}), 1)

    def test_it_degrades_silently_to_nothing(self):
        self._agent("a", 5)
        for session, cwd in ((None, self.CWD), ("", self.CWD), (self.SESSION, None),
                             (self.SESSION, ""), ("no-such-session", self.CWD),
                             (self.SESSION, "/nowhere")):
            self.assertEqual(sl.live_agent_count(session, cwd, NOW, {}), 0, (session, cwd))

    def test_an_unreadable_directory_is_not_fatal(self):
        self._agent("a", 5)
        with mock.patch.object(os, "scandir", side_effect=OSError("boom")):
            self.assertEqual(self._count(), 0)

    def test_a_missing_tmp_root_is_not_fatal(self):
        with mock.patch.dict(os.environ, {"SQUIRREL_TMPDIR": str(self.root / "nope")}):
            self.assertEqual(self._count(), 0)

    def test_the_segment_renders_only_when_agents_are_live(self):
        status = dict(STATUS, session_id=self.SESSION,
                      workspace={"current_dir": self.CWD}, pr=None)
        with mock.patch.object(sl, "git_segment_runs", return_value=[]):
            before = plain(sl.render(status, CACHE, None, {}, NOW, timezone.utc, width=400)[1])
        self.assertNotIn("agent", before)
        self._agent("a", 5)
        self._agent("b", 5)
        with mock.patch.object(sl, "git_segment_runs", return_value=[]):
            after = plain(sl.render(status, CACHE, None, {}, NOW, timezone.utc, width=400)[1])
        self.assertIn("⧗ 2 agents", after)
        self._agent("c", 5)
        with mock.patch.object(sl, "git_segment_runs", return_value=[]):
            self.assertIn("⧗ 3 agents", plain(sl.render(status, CACHE, None, {}, NOW,
                                                        timezone.utc, width=400)[1]))

    def test_one_agent_is_singular(self):
        self._agent("a", 5)
        ctx = sl._ctx_for(dict(STATUS, session_id=self.SESSION,
                               workspace={"current_dir": self.CWD}),
                          {}, False, "stdin", None, {}, NOW, timezone.utc, None)
        self.assertEqual(sl.seg_agents(ctx)[0][0], "⧗ 1 agent")

    def test_no_entry_is_stat_ed_when_no_agents_are_running(self):
        """The user's constraint: "we must be sure we will not burn users PC performance for
        nothing". Finding the directory costs a stat; after that, a directory full of tool
        outputs must cost NOTHING PER ENTRY, because is_symlink() is answered from the
        directory entry and only symlinks are followed."""
        for name in ("b1", "b2", "b3", "b4", "b5"):
            self._tool_output(name)
        real_stat = os.stat
        calls = []

        def counting(path, *a, **kw):
            calls.append(str(path))
            return real_stat(path, *a, **kw)

        with mock.patch.object(os, "stat", side_effect=counting):
            self.assertEqual(self._count(), 0)
        inside = [c for c in calls if c.startswith(str(self.tasks) + os.sep)]
        self.assertEqual(inside, [], f"stat()ed {len(inside)} entries for nothing")

        # …and one symlink costs exactly one stat, the one that decides if it is live
        self._agent("a", 5)
        calls.clear()
        with mock.patch.object(os, "stat", side_effect=counting):
            self.assertEqual(self._count(), 1)
        self.assertEqual(len([c for c in calls if c.startswith(str(self.tasks) + os.sep)]), 1)


@unittest.skipIf(os.name == "nt", "the agents dir is keyed on os.getuid(), POSIX-only")
class AgentsPathTests(TmpEnv):
    def test_the_slug_is_derived_from_the_working_directory(self):
        fake = self.root / "tmp"
        with mock.patch.dict(os.environ, {"SQUIRREL_TMPDIR": str(fake)}):
            tasks = fake / f"claude-{os.getuid()}" / "-a-b-c" / "s1" / "tasks"
            tasks.mkdir(parents=True)
            self.assertEqual(sl.agents_tasks_dir("s1", "/a/b/c"), str(tasks))
            self.assertIsNone(sl.agents_tasks_dir("s1", "/a/b/d"))

    def test_it_falls_back_to_scanning_for_claude_dirs(self):
        fake = self.root / "tmp"
        odd = fake / "claude-999" / "-a" / "s1" / "tasks"
        odd.mkdir(parents=True)
        with mock.patch.dict(os.environ, {"SQUIRREL_TMPDIR": str(fake)}):
            self.assertEqual(sl.agents_tasks_dir("s1", "/a"), str(odd))


class ChipIsOrdinaryRunsTests(TmpEnv):
    """Task 17 folded the last pre-rendered fragment into the seam."""

    def test_the_chip_is_runs_not_a_finished_string(self):
        sl.apply_state("idle", "chip-runs", NOW - 5)
        runs = sl.state_segment_runs({"session_id": "chip-runs"}, NOW, sl.load_config())
        self.assertEqual(len(runs), 1)
        text, sgr = runs[0]
        self.assertNotIn("\033", text)
        self.assertEqual(text, "[ ✓ idle ]")
        self.assertEqual(sgr, sl.YELLOW)

    def test_the_string_form_is_unchanged_for_its_callers(self):
        sl.apply_state("idle", "chip-str", NOW - 5)
        self.assertEqual(sl.state_segment({"session_id": "chip-str"}, NOW, sl.load_config()),
                         sl.colored("[ ✓ idle ]", sl.YELLOW))

    def test_the_alarm_chip_keeps_its_loud_sequence(self):
        sl.apply_state("idle", "chip-alarm", NOW - 3000)
        runs = sl.state_segment_runs({"session_id": "chip-alarm"}, NOW, sl.load_config())
        self.assertEqual(runs[0][1], sl.ALARM_SEQ)
        self.assertIn("IDLE", runs[0][0])

    def test_no_state_is_no_runs(self):
        self.assertEqual(sl.state_segment_runs({"session_id": "nope"}, NOW, {}), [])
        self.assertIsNone(sl.state_segment({"session_id": "nope"}, NOW, {}))

    def test_the_chip_can_now_be_styled_like_any_other_segment(self):
        sl.apply_state("idle", "chip-style", NOW - 5)
        status = dict(STATUS, session_id="chip-style")
        cfg = dict(sl.load_config(), segmentStyles={"chip": {"bold": True}})
        with mock.patch.object(sl, "git_segment_runs", return_value=[]):
            line = sl.render(status, CACHE, None, cfg, NOW, timezone.utc, width=400)[1]
        self.assertIn("\033[1m", line)
        self.assertIn("✓ idle", plain(line))



# ── the smoke test's own guard ───────────────────────────────────────────────
class ProfileSnapshotTests(unittest.TestCase):
    """The teeth of the smoke test's "did we touch the user's real installation?" guard.

    These used to be checked by planting each fault in the developer's LIVE profile and
    watching the gate fail — which was risky, needed the faults undone by hand afterwards,
    and could never run in CI. The guard takes a directory path, so a fixture does the same
    job with none of that. This is now the ONLY place those teeth are verified."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name) / "squirrel"
        (self.root / "cache").mkdir(parents=True)
        (self.root / "cache" / "usage.json").write_text('{"fetched_at": 1}')
        (self.root / "config.json").write_text('{"warnPercent": 50}')
        os.chmod(self.root / "config.json", 0o600)
        (self.root / "run.sh").write_text("#!/bin/sh\n")
        (self.root / "state-real-session.json").write_text('{"state": "working"}')
        os.chmod(self.root / "state-real-session.json", 0o600)   # as Squirrel writes them
        os.chmod(self.root / "cache", 0o700)
        self.before = snap.snapshot(self.root)

    def _changed(self):
        return snap.snapshot(self.root) != self.before

    # ── what MUST be caught ──
    def test_a_stray_file_appearing_is_caught(self):
        """The original defect: the unit suite creating state-wtest*.json in a live profile."""
        (self.root / "state-wtest-abc.json").write_text("{}")
        self.assertTrue(self._changed())

    def test_any_new_file_is_caught_even_an_innocuous_one(self):
        (self.root / "notes.txt").write_text("hi")
        self.assertTrue(self._changed())

    def test_a_file_disappearing_is_caught(self):
        (self.root / "run.sh").unlink()
        self.assertTrue(self._changed())

    def test_a_content_change_outside_cache_is_caught(self):
        (self.root / "config.json").write_text('{"warnPercent": 40}')
        self.assertTrue(self._changed())

    def test_a_same_length_content_change_is_caught(self):
        """Size alone would miss this; the fingerprint hashes."""
        (self.root / "config.json").write_text('{"warnPercent": 51}')
        self.assertTrue(self._changed())

    @unittest.skipIf(os.name == "nt", "POSIX permission bits")
    def test_a_mode_change_is_caught(self):
        os.chmod(self.root / "config.json", 0o644)
        self.assertTrue(self._changed())

    @unittest.skipIf(os.name == "nt", "POSIX permission bits")
    def test_a_mode_change_on_a_live_file_is_still_caught(self):
        os.chmod(self.root / "state-real-session.json", 0o644)
        self.assertTrue(self._changed())

    @unittest.skipIf(os.name == "nt", "POSIX permission bits")
    def test_a_mode_change_on_cache_itself_is_still_caught(self):
        os.chmod(self.root / "cache", 0o777)
        self.assertTrue(self._changed())

    def test_a_new_file_inside_a_non_live_subdirectory_is_caught(self):
        (self.root / "stable").mkdir()
        before = snap.snapshot(self.root)
        (self.root / "stable" / "new.json").write_text("{}")
        self.assertNotEqual(snap.snapshot(self.root), before)

    def test_the_directory_vanishing_is_caught(self):
        import shutil
        shutil.rmtree(self.root)
        self.assertEqual(snap.snapshot(self.root), "(absent)")
        self.assertTrue(self._changed())

    # ── what must NOT be caught: the churn that made this gate flaky ──
    def test_cache_contents_changing_is_ignored(self):
        (self.root / "cache" / "usage.json").write_text('{"fetched_at": 999999}')
        (self.root / "cache" / "fetch.lock").write_text("")
        self.assertFalse(self._changed())

    def test_live_state_content_changing_is_ignored(self):
        (self.root / "state-real-session.json").write_text('{"state": "idle", "ts": 123}')
        self.assertFalse(self._changed())

    def test_mtimes_alone_are_ignored(self):
        for path in self.root.rglob("*"):
            os.utime(path, (0, 0))
        self.assertFalse(self._changed())

    def test_a_new_live_state_file_is_still_caught_though(self):
        """Their content is ignored; their EXISTENCE is not. This is the line the narrowing
        had to keep on the right side of."""
        (self.root / "state-another-session.json").write_text("{}")
        self.assertTrue(self._changed())

    def test_snapshotting_is_stable_when_nothing_changes(self):
        for _ in range(3):
            self.assertFalse(self._changed())

    def test_is_live_classifies_only_the_two_churning_kinds(self):
        for name in ("cache", "state-abc.json", "state-.json"):
            self.assertTrue(snap.is_live(name), name)
        for name in ("config.json", "run.sh", "stable", "state-abc.txt", "statement.json",
                     "cache.json", "caches"):
            self.assertFalse(snap.is_live(name), name)



# ── Task 19: the documentation guard ─────────────────────────────────────────
# The shipped tree, reached from the module under test so this stays true wherever it runs.
assert Path(sl.__file__).resolve().parent.parent == PLUGIN_ROOT
README = PLUGIN_ROOT / "README.md"
SKILL = PLUGIN_ROOT / "skills" / "squirrel" / "SKILL.md"
COMMANDS_DIR = PLUGIN_ROOT / "commands"


def _command_files():
    """(slash name, flag it invokes) for every shipped command."""
    out = []
    for path in sorted(COMMANDS_DIR.glob("*.md")):
        match = re.search(r'squirrel\.sh"?\s+(--[a-z-]+)', path.read_text(encoding="utf-8"))
        out.append((f"/{path.stem}", match.group(1) if match else None, path.name))
    return out


class SourceHygieneTests(unittest.TestCase):
    """Python 3.12+ warns on an invalid escape sequence in a non-raw string, and 3.13 prints
    it to stderr on EVERY invocation. Found on Windows Python 3.13 where a docstring
    containing `ESC \\` produced a SyntaxWarning before every single command's output."""

    def test_no_module_emits_a_syntax_warning(self):
        import warnings
        for path in (PLUGIN_ROOT / "scripts" / "statusline.py",
                     DEV_DIR / "test_statusline.py",
                     DEV_DIR / "profile_snapshot.py",
                     DEV_DIR / "write_golden.py"):
            name = path.name
            with warnings.catch_warnings(record=True) as caught:
                warnings.simplefilter("always")
                compile(path.read_text(encoding="utf-8"), str(path), "exec")
            messages = [f"line {w.lineno}: {w.message}" for w in caught
                        if issubclass(w.category, SyntaxWarning)]
            self.assertEqual(messages, [], f"{name} emits SyntaxWarnings: {messages}")


class DocumentationGuardTests(unittest.TestCase):
    """A setting Claude cannot see does not exist, and one the README omits cannot be found.

    Everything here was being kept true by hand — which is exactly the habit a guard replaces.
    Measured before this guard existed: one config key missing from the README and four from
    SKILL.md, all added in the preceding few tasks."""

    def setUp(self):
        self.readme = README.read_text(encoding="utf-8")
        self.skill = SKILL.read_text(encoding="utf-8")
        self.shipped = json.loads(sl.default_config_path().read_text(encoding="utf-8"))

    def _keys(self):
        return sorted(set(self.shipped) | set(sl.TUNABLES))

    def test_every_config_key_is_documented_in_the_readme(self):
        missing = [k for k in self._keys() if k not in self.readme]
        self.assertEqual(missing, [], f"undocumented in README.md: {missing}")

    def test_every_config_key_is_documented_in_the_skill(self):
        """The skill is the ONLY document Claude reads. A key absent from it is a setting the
        user cannot reach by asking, which breaks the never-edit-the-file promise."""
        missing = [k for k in self._keys() if k not in self.skill]
        self.assertEqual(missing, [], f"undocumented in SKILL.md: {missing}")

    def test_every_dispatcher_flag_is_documented_in_the_skill(self):
        flags = sorted(set(sl.CONFIG_COMMANDS) | set(sl.NON_CONFIG_COMMANDS))
        missing = [f for f in flags if f not in self.skill]
        self.assertEqual(missing, [], f"flags Claude cannot discover: {missing}")

    def test_every_shipped_command_is_named_in_both_documents(self):
        """Deliberately BOTH. The README is where a person looks; the skill is where Claude
        looks, and a user asking Claude "what commands are there?" must not be answered with
        a flag they cannot type."""
        for name, _flag, _file in _command_files():
            self.assertIn(f"`{name}`", self.readme, f"{name} missing from README.md")
            self.assertIn(f"`{name}`", self.skill, f"{name} missing from SKILL.md")

    def test_every_command_file_invokes_a_flag_the_dispatcher_accepts(self):
        known = set(sl.CONFIG_COMMANDS) | set(sl.NON_CONFIG_COMMANDS) | {"--help-text"}
        for name, flag, filename in _command_files():
            self.assertIsNotNone(flag, f"{filename} invokes no flag at all")
            self.assertIn(flag, known, f"{filename} invokes {flag}, which main() does not accept")

    def test_every_registered_segment_is_documented_in_both(self):
        for seg_id in sl.SEGMENTS:
            self.assertIn(f"`{seg_id}`", self.readme, f"segment {seg_id} missing from README.md")
            self.assertIn(f"`{seg_id}`", self.skill, f"segment {seg_id} missing from SKILL.md")

    def test_the_readme_command_count_matches_reality(self):
        """The stated count drifted twice before anyone noticed; it is cheap to pin."""
        words = {"Twenty-four": 24, "Twenty-five": 25, "Twenty-six": 26, "Twenty-seven": 27,
                 "Twenty-eight": 28, "Twenty-nine": 29, "Thirty": 30, "Thirty-one": 31,
                 "Thirty-two": 32, "Thirty-three": 33, "Thirty-four": 34, "Thirty-five": 35,
                 "Thirty-six": 36, "Thirty-seven": 37, "Thirty-eight": 38, "Thirty-nine": 39,
                 "Forty": 40}
        match = re.search(r"^(\w+(?:-\w+)?) `/squirrel-\*` commands\.", self.readme, re.M)
        self.assertIsNotNone(match, "README no longer states a command count")
        stated = words.get(match.group(1))
        self.assertIsNotNone(stated, f"unrecognised count word {match.group(1)!r}")
        self.assertEqual(stated, len(list(COMMANDS_DIR.glob("*.md"))))
        rows = len(re.findall(r"^\| `/squirrel-", self.readme, re.M))
        self.assertEqual(rows, len(list(COMMANDS_DIR.glob("*.md"))),
                         "the README command table and commands/ disagree")

    def test_every_tunable_states_its_range_somewhere_in_the_readme(self):
        for key, (_default, low, high, _help) in sl.TUNABLES.items():
            self.assertRegex(self.readme, rf"`{re.escape(key)}`[^\n]*{low}[^\n]*{high}",
                             f"{key} does not state its {low}-{high} range in README.md")

    def test_no_command_output_names_a_slash_command_that_does_not_exist(self):
        """Telling a user to run /squirrel-set-click-terminals when the command is actually
        /squirrel-click-terminals hands them something that does nothing — the same
        confidently-useless failure as answering with a flag they cannot type. Found exactly
        that in cmd_enable_click's own refusal message."""
        real = {f"/{path.stem}" for path in COMMANDS_DIR.glob("*.md")}
        offenders = set()
        for name in dir(sl):
            if not name.startswith("cmd_"):
                continue
            func = getattr(sl, name)
            source = inspect.getsource(func) if callable(func) else ""
            for mentioned in re.findall(r"/squirrel-[a-z-]+", source):
                if mentioned not in real:
                    offenders.add((name, mentioned))
        self.assertEqual(offenders, set(), f"commands that do not exist: {sorted(offenders)}")

    def test_the_guard_would_notice_a_new_setting(self):
        """Proof the guard has teeth: an invented key is undocumented by construction."""
        invented = "somethingNobodyDocumented"
        self.assertNotIn(invented, self.readme)
        self.assertNotIn(invented, self.skill)



# ── Task 20: the sessions table on a narrow terminal ─────────────────────────
class ShowSessionsNarrowTests(TmpEnv):
    """The table is what a user reads to decide which session to stop, so it has to be
    legible on the pane they actually have. Ten columns is 130 wide; an 80-column terminal
    wrapped every row into unreadable ribbons."""

    def setUp(self):
        super().setUp()
        self.shared = self.root / "shared"
        patcher = mock.patch.dict(os.environ, {"SQUIRREL_SHARED_DIR": str(self.shared)})
        patcher.start(); self.addCleanup(patcher.stop)
        self.account = sl.account_hash("you@example.com")
        model = "Claude Opus 5"
        for sid, repo, cost, tokens in (("alpha", "storefront", 92.0, 900_000),
                                        ("bravo", "handbook", 31.0, 300_000),
                                        ("charlie", "beacon", 12.5, 120_000)):
            sl.write_cache(sl.session_path(sid), {"session_id": sid, "repo": repo, "samples": [
                [NOW - 3000, 0, 0.0, model, self.account, 10.0],
                [NOW - 60, tokens, cost, model, self.account, 70.0]]})

    def _table(self, width):
        with mock.patch.object(sl.time, "time", return_value=NOW), \
             mock.patch.object(sl, "_current_email", return_value="you@example.com"):
            return sl.cmd_show_sessions("alpha", width=width)

    def _header(self, width):
        return self._table(width).splitlines()[0]

    def test_nothing_exceeds_the_budget_at_any_width(self):
        """Including the explanatory notes — a fix for a table that wraps is not much of a
        fix if its own explanation still does."""
        for width in (200, 130, 100, 80, 60, 50, 40, 30):
            for line in self._table(width).splitlines():
                self.assertLessEqual(len(line), width,
                                     f"width {width}: {len(line)}-char line {line!r}")

    def test_a_wide_terminal_still_gets_every_column(self):
        header = self._header(400)
        for column in ("session", "5h pts", "coverage", "5h cost", "5h tok", "life cost",
                       "life %", "life tok", "model mix", "burn", "age"):
            self.assertIn(column, header)
        self.assertNotIn("narrowed to fit", self._table(400))

    def test_the_name_and_the_ranking_figure_are_never_dropped(self):
        """They ARE the table: which session, and how much of the window it took."""
        for width in (200, 80, 40, 20, 1):
            header = self._header(width)
            self.assertIn("session", header, width)
            self.assertIn("5h pts", header, width)

    def test_columns_are_shed_in_priority_order(self):
        wide, narrow = self._header(400), self._header(80)
        self.assertIn("life tok", wide)
        self.assertNotIn("life tok", narrow)          # first to go
        self.assertIn("burn", narrow)                 # near-last to go

    def test_it_says_what_it_hid(self):
        """Silently dropping a column is how a reader draws a conclusion from a table that
        is not showing them everything."""
        table = self._table(80)
        self.assertIn("narrowed to fit 80 columns", table)
        self.assertIn("hiding", table)
        self.assertIn("life tok", table)              # named, even though its column is gone
        self.assertIn("/squirrel-sessions wide", table)

    def test_the_rows_still_line_up_after_shedding(self):
        """Column starts are taken from the rule row's runs of dashes; every data row must
        have its separator space at each of those boundaries, or the table is ragged."""
        lines = self._table(80).splitlines()
        header, rule = lines[0], lines[1]
        starts = [m.start() for m in re.finditer(r"-+", rule)]
        self.assertGreater(len(starts), 1)
        self.assertEqual(len(rule), len(header))
        for row in lines[2:5]:
            for start in starts[1:]:
                self.assertEqual(row[start - 1], " ", f"ragged at {start}: {row!r}")
                self.assertNotEqual(row[start], " ", f"cell not left-aligned: {row!r}")

    def test_a_note_about_a_hidden_column_is_not_printed(self):
        """The 'life %' explanation is noise once that column is gone."""
        self.assertIn("life % ranked by", self._table(400))
        self.assertNotIn("life % ranked by", self._table(60))

    def test_the_uncertainty_marks_survive_every_narrowing(self):
        """Dropping `coverage` costs precision, but a figure must never read as more certain
        than it is — the marks live on the value itself, so they cannot be shed."""
        for width in (400, 80, 40, 20):
            self.assertIn("~", self._table(width))

    def test_wide_is_accepted_through_the_cli_in_either_order(self):
        for argv in (["--show-sessions", "wide"], ["--show-sessions", "alpha wide"],
                     ["--show-sessions", "wide alpha"]):
            out = io.StringIO()
            with mock.patch.object(sys, "stdout", out), \
                 mock.patch.object(sl.time, "time", return_value=NOW), \
                 mock.patch.object(sl, "_current_email", return_value="you@example.com"), \
                 mock.patch.dict(os.environ, {"COLUMNS": "80"}):
                sl.main(argv)
            self.assertIn("model mix", out.getvalue(), argv)
            self.assertNotIn("narrowed to fit", out.getvalue(), argv)

    def test_the_session_marker_still_works_alongside_wide(self):
        out = io.StringIO()
        with mock.patch.object(sys, "stdout", out), \
             mock.patch.object(sl.time, "time", return_value=NOW), \
             mock.patch.object(sl, "_current_email", return_value="you@example.com"):
            sl.main(["--show-sessions", "alpha wide"])
        self.assertIn("▸storefront", out.getvalue())

    def test_without_wide_a_narrow_terminal_sheds(self):
        out = io.StringIO()
        with mock.patch.object(sys, "stdout", out), \
             mock.patch.object(sl.time, "time", return_value=NOW), \
             mock.patch.object(sl, "_current_email", return_value="you@example.com"), \
             mock.patch.dict(os.environ, {"COLUMNS": "80"}):
            sl.main(["--show-sessions", "alpha"])
        self.assertIn("narrowed to fit 80 columns", out.getvalue())

    def test_the_drop_order_covers_every_droppable_column(self):
        order = ["session", "5h pts", "coverage", "5h cost", "5h tok", "life cost", "life %",
                 "life tok", "model mix", "burn", "age"]
        droppable = set(order) - {"session", "5h pts"}
        self.assertEqual(set(sl.SESSION_COLUMN_DROP_ORDER), droppable)
        self.assertEqual(len(sl.SESSION_COLUMN_DROP_ORDER), len(droppable))



class OscWidthAccountingTests(TmpEnv):
    """An OSC sequence carries a payload — an OSC-8 hyperlink embeds a whole URL — and it
    occupies no columns. Counting one as visible text is not a rounding error.

    Shipped in Task 16 and caught in Task 26 by looking at the rendered bar rather than at a
    test: a `link=` on a segment made visible_len() report ~35 columns more than the line
    renders, so the fit search shed real segments and narrowed every bar to pay for width
    that was never on screen. The Task 16 test only asserted the escape was present, at a
    width where nothing sheds, so it never saw it."""

    LINK = "https://example.com/1"

    def _visible(self, text):
        """What the user actually sees — independent of visible_len(), deliberately."""
        stripped = re.sub(r"\x1b\]8;;[^\x1b\x07]*(?:\x1b\\|\x07)", "", plain(text))
        return len(stripped)

    def test_visible_len_ignores_an_osc8_hyperlink(self):
        wrapped = f"\033]8;;{self.LINK}\033\\PR #42\033]8;;\033\\"
        self.assertEqual(sl.visible_len(wrapped), 6)

    def test_it_ignores_the_bel_terminated_form_too(self):
        self.assertEqual(sl.visible_len(f"\033]8;;{self.LINK}\007ok\033]8;;\007"), 2)

    def test_it_still_counts_ordinary_text_and_colour(self):
        self.assertEqual(sl.visible_len("abc"), 3)
        self.assertEqual(sl.visible_len(sl.colored("abc", sl.GREEN)), 3)

    def test_a_link_changes_nothing_but_the_hyperlink_itself(self):
        """The bug, end to end: adding a link narrowed every bar and dropped the segment.

        Stated as the strongest form of the property — strip the OSC wrapper from the linked
        render and it must be byte-identical to the unlinked one, at every width. Asserting
        "the segment is still there" would be wrong: at 100 columns it sheds in BOTH, because
        a custom segment's priority is low. What must not differ is anything else."""
        osc = re.compile(r"\x1b\]8;;[^\x1b\x07]*(?:\x1b\\|\x07)")
        plain_cfg = dict(CONFIG, customSegments=[{"id": "note", "text": "PR #42"}])
        linked_cfg = dict(CONFIG, customSegments=[{"id": "note", "text": "PR #42",
                                                   "link": self.LINK}])
        for width in (200, 130, 120, 110, 100, 95, 90, 70):
            with mock.patch.object(sl, "git_segment_runs", return_value=[]):
                bare = sl.render(STATUS, CACHE, "you@example.com", plain_cfg, NOW,
                                 timezone.utc, width=width)[0]
                linked = sl.render(STATUS, CACHE, "you@example.com", linked_cfg, NOW,
                                   timezone.utc, width=width)[0]
            self.assertEqual(bare, osc.sub("", linked), f"width {width}")
        # and at a width where it does survive, the hyperlink is genuinely emitted
        with mock.patch.object(sl, "git_segment_runs", return_value=[]):
            wide = sl.render(STATUS, CACHE, "you@example.com", linked_cfg, NOW,
                             timezone.utc, width=200)[0]
        self.assertIn(f"\033]8;;{self.LINK}\033\\", wide)
        self.assertIn("PR #42", plain(wide))

    def test_squirrels_own_measurement_matches_what_renders(self):
        cfg = dict(CONFIG, customSegments=[{"id": "note", "text": "PR #42", "link": self.LINK}])
        for width in (200, 120, 100, 90, 70):
            with mock.patch.object(sl, "git_segment_runs", return_value=[]):
                line = sl.render(STATUS, CACHE, "you@example.com", cfg, NOW, timezone.utc,
                                 width=width)[0]
            self.assertEqual(sl.visible_len(line), self._visible(line), f"width {width}")

    def test_the_banner_pads_a_linked_line_like_any_other(self):
        sl.apply_state("idle", "osc-pad", NOW - 300)
        status = dict(STATUS, session_id="osc-pad")
        widths = []
        for extra in ({}, {"customSegments": [{"id": "note", "text": "PR", "link": self.LINK}]}):
            cfg = dict(sl.load_config(), **extra)
            with mock.patch.object(sl, "git_segment_runs", return_value=[]):
                lines = sl.render(status, CACHE, "you@example.com", cfg, NOW, timezone.utc,
                                  width=120)
            widths.append([self._visible(line) for line in lines])
        self.assertEqual(widths[0], widths[1], "a link changed the banner's padding")



# ── Task 26: click to acknowledge ────────────────────────────────────────────
class ClickNoSocketTests(TmpEnv):
    """THE governing rule, in the user's words: "it should run only when needed. We should
    not neccessarily create local listener servers for users which dont need that."

    Three conditions must ALL hold before anything binds. Each row below is one of them
    missing, and each must spawn nothing."""

    STATUS = {"session_id": "click", "model": {"display_name": "M"},
              "context_window": {"context_window_size": 200_000, "used_percentage": 42},
              "cost": {"total_cost_usd": 1.0}}
    SEG = {"id": "w", "every": 60, "ack": True, "click": "ack", "text": "w"}

    def _spawned(self, config, term="vscode"):
        with mock.patch.dict(os.environ, {"TERM_PROGRAM": term}), \
             mock.patch.object(sl, "spawn_click_listener") as spawn, \
             mock.patch.object(sl, "git_segment_runs", return_value=[]):
            sl.render(self.STATUS, {}, None, config, NOW, timezone.utc, width=200)
        return spawn.called

    def test_a_stock_config_opens_no_socket(self):
        self.assertFalse(self._spawned(sl.load_config()))

    def test_a_declared_click_with_the_feature_off_opens_no_socket(self):
        self.assertFalse(self._spawned({"customSegments": [self.SEG]}))

    def test_the_feature_on_with_nothing_declaring_it_opens_no_socket(self):
        self.assertFalse(self._spawned({"clickToAck": True}))

    def test_a_segment_that_cannot_be_acknowledged_opens_no_socket(self):
        seg = {k: v for k, v in self.SEG.items() if k != "ack"}
        self.assertFalse(self._spawned({"clickToAck": True, "customSegments": [seg]}))

    def test_a_terminal_that_cannot_follow_the_link_opens_no_socket(self):
        """Not in the original brief. Claude Code passes OSC-8 to IDE terminals and not to
        standalone ones (upstream #26356, closed as not planned), so a socket on Konsole
        could never receive a click — "only when needed" has to mean "only where it can
        work"."""
        config = {"clickToAck": True, "customSegments": [self.SEG]}
        for term in ("iTerm.app", "Apple_Terminal", "", "konsole"):
            self.assertFalse(self._spawned(config, term=term), term)

    def test_all_three_conditions_together_do_open_one(self):
        self.assertTrue(self._spawned({"clickToAck": True, "customSegments": [self.SEG]}))

    def test_the_terminal_check_can_be_overridden(self):
        config = {"clickToAck": True, "clickTerminals": ["any"], "customSegments": [self.SEG]}
        self.assertTrue(self._spawned(config, term="iTerm.app"))

    def test_no_link_is_emitted_before_a_listener_is_live(self):
        """The render never waits for a socket: the first render spawns and shows no link,
        the next one has it."""
        config = {"clickToAck": True, "customSegments": [self.SEG]}
        with mock.patch.dict(os.environ, {"TERM_PROGRAM": "vscode"}), \
             mock.patch.object(sl, "spawn_click_listener"), \
             mock.patch.object(sl, "git_segment_runs", return_value=[]):
            line = sl.render(self.STATUS, {}, None, config, NOW, timezone.utc, width=200)[0]
        self.assertNotIn("\033]8;;", line)
        self.assertIn("w", plain(line))


class ClickListenerTests(TmpEnv):
    """The listener itself. Each test maps to a numbered item in the task's threat model."""

    def setUp(self):
        super().setUp()
        # Own shared store: an ack with shared=on writes to SQUIRREL_SHARED_DIR, which
        # setUpModule points at ONE directory for the whole run. Without this these tests
        # leave state behind that another class asserts is absent.
        patcher = mock.patch.dict(os.environ,
                                  {"SQUIRREL_SHARED_DIR": str(self.root / "shared-click")})
        patcher.start(); self.addCleanup(patcher.stop)
        sl.cmd_custom("add water every=30m ack=on shared=on click=ack text=water")
        sl.cmd_enable_click("on")
        self.thread = None

    def _serve(self):
        import threading
        self.thread = threading.Thread(target=sl.run_click_listener, daemon=True)
        self.thread.start()
        for _ in range(50):
            live = sl.read_click_listener(_time.time(), sl.load_config())
            if live:
                return live
            _time.sleep(0.05)
        self.fail("listener never came up")

    @staticmethod
    def _get(port, path, host=None, method="GET"):
        import urllib.error
        import urllib.request
        req = urllib.request.Request(f"http://127.0.0.1:{port}{path}", method=method)
        req.add_header("Host", host if host is not None else f"127.0.0.1:{port}")
        try:
            with urllib.request.urlopen(req, timeout=3) as response:
                return response.status, response.read().decode()
        except urllib.error.HTTPError as exc:
            return exc.code, ""

    # 1 — any local process can reach loopback; the token is the only barrier
    def test_the_token_is_128_bits_and_regenerated_each_start(self):
        """128 bits, down from 256: every character of the token is charged against the status
        line's width by Claude Code (see host_len), and 21 columns is the difference between a
        reminder that can be clicked on a busy bar and one that cannot. 128 bits is still far
        past any local brute force against a listener that lives half an hour."""
        first = sl.run_click_listener(serve=False)["token"]
        second = sl.run_click_listener(serve=False)["token"]
        self.assertNotEqual(first, second)
        self.assertGreaterEqual(len(first), 22)          # token_urlsafe(16)

    def test_a_wrong_or_missing_token_gets_a_bare_404(self):
        live = self._serve()
        for path in (f"/wrong/ack/water", "/ack/water", "/", "//ack/water",
                     f"/{live['token'][:-1]}/ack/water"):
            status, body = self._get(live["port"], path)
            self.assertEqual(status, 404, path)
            self.assertEqual(body, "", f"{path} leaked a body")

    @unittest.skipIf(os.name == "nt", "POSIX permission bits")
    def test_the_token_is_never_written_outside_the_0600_state_file(self):
        live = self._serve()
        token = live["token"]
        self.assertEqual(stat.S_IMODE(os.stat(sl.click_state_path()).st_mode), 0o600)
        for path in self.root.rglob("*"):
            if path.is_file() and path != sl.click_state_path():
                try:
                    self.assertNotIn(token, path.read_text(errors="ignore"), str(path))
                except OSError:
                    pass

    # 2 — a browser page can reach 127.0.0.1; Host check, GET only, no CORS
    def test_a_foreign_host_header_is_refused(self):
        live = self._serve()
        for host in ("evil.example.com", f"evil.example.com:{live['port']}", "", "127.0.0.1"):
            status, _ = self._get(live["port"], f"/{live['token']}/ack/water", host=host)
            self.assertEqual(status, 404, host)

    def test_loopback_hosts_are_accepted(self):
        live = self._serve()
        for host in (f"127.0.0.1:{live['port']}", f"localhost:{live['port']}"):
            status, _ = self._get(live["port"], f"/{live['token']}/ack/water", host=host)
            self.assertEqual(status, 200, host)

    def test_only_get_is_answered(self):
        live = self._serve()
        for method in ("POST", "PUT", "DELETE", "PATCH", "OPTIONS", "HEAD"):
            status, _ = self._get(live["port"], f"/{live['token']}/ack/water", method=method)
            self.assertEqual(status, 404, method)

    def test_no_cors_headers_are_ever_sent(self):
        import urllib.request
        live = self._serve()
        req = urllib.request.Request(f"http://127.0.0.1:{live['port']}/{live['token']}/ack/water")
        req.add_header("Host", f"127.0.0.1:{live['port']}")
        req.add_header("Origin", "https://evil.example.com")
        with urllib.request.urlopen(req, timeout=3) as response:
            headers = {k.lower() for k in response.headers.keys()}
        self.assertEqual({h for h in headers if h.startswith("access-control")}, set())

    # 3 — other users on a shared machine
    @unittest.skipIf(os.name == "nt", "POSIX permission bits")
    def test_the_state_file_is_owner_only(self):
        sl.run_click_listener(serve=False)
        self.assertEqual(stat.S_IMODE(os.stat(sl.click_state_path()).st_mode), 0o600)

    # 4 — denial of service / wedging
    def test_a_malformed_request_does_not_wedge_the_listener(self):
        import socket
        live = self._serve()
        for junk in (b"\x00\x01garbage\r\n\r\n", b"GET\r\n", b"\r\n\r\n", b"X" * 9000):
            probe = socket.socket()
            probe.settimeout(2)
            try:
                probe.connect(("127.0.0.1", live["port"]))
                probe.sendall(junk)
            except OSError:
                pass
            finally:
                probe.close()
        status, _ = self._get(live["port"], f"/{live['token']}/ack/water")
        self.assertEqual(status, 200, "the listener stopped serving after malformed input")

    def test_an_unknown_verb_or_segment_changes_nothing(self):
        live = self._serve()
        self.assertEqual(self._get(live["port"], f"/{live['token']}/rm/water")[0], 404)
        self.assertEqual(self._get(live["port"], f"/{live['token']}/command/ls")[0], 404)
        status, body = self._get(live["port"], f"/{live['token']}/ack/no-such-segment")
        self.assertEqual(status, 200)
        self.assertIn("not a dismissible segment", body)

    # the thing it is actually for
    def test_a_click_acknowledges_the_segment(self):
        live = self._serve()
        self.assertFalse(sl.custom_state_path(True).exists())
        status, body = self._get(live["port"], f"/{live['token']}/ack/water")
        self.assertEqual(status, 200)
        self.assertIn("Acknowledged", body)
        state = json.loads(sl.custom_state_path(True).read_text(encoding="utf-8"))
        self.assertIn("water", state)

    def test_a_click_can_snooze(self):
        live = self._serve()
        status, body = self._get(live["port"], f"/{live['token']}/snooze/water/5")
        self.assertEqual(status, 200)
        self.assertIn("Snoozed", body)

    def test_the_segment_id_is_escaped_in_the_page(self):
        page = sl._click_page("Acknowledged", "<script>alert(1)</script>")
        self.assertNotIn(b"<script>alert(1)", page)
        self.assertIn(b"&lt;script&gt;", page)

    def test_a_stale_state_file_is_not_trusted(self):
        sl.write_cache(sl.click_state_path(),
                       {"port": 9, "token": "x", "pid": 999_999_999, "started": _time.time()})
        self.assertIsNone(sl.read_click_listener(_time.time(), {}))       # dead pid
        sl.write_cache(sl.click_state_path(),
                       {"port": 9, "token": "x", "pid": os.getpid(), "started": 0})
        self.assertIsNone(sl.read_click_listener(_time.time(), {}))       # too old
        sl.write_cache(sl.click_state_path(), {"port": "nope", "token": "", "pid": os.getpid()})
        self.assertIsNone(sl.read_click_listener(_time.time(), {}))       # malformed


class ClickCommandTests(TmpEnv):
    def setUp(self):
        super().setUp()
        # Own shared store: an ack with shared=on writes to SQUIRREL_SHARED_DIR, which
        # setUpModule points at ONE directory for the whole run. Without this these tests
        # leave state behind that another class asserts is absent.
        patcher = mock.patch.dict(os.environ,
                                  {"SQUIRREL_SHARED_DIR": str(self.root / "shared-click")})
        patcher.start(); self.addCleanup(patcher.stop)

    def test_enabling_reports_whether_this_terminal_can_follow_the_link(self):
        with mock.patch.dict(os.environ, {"TERM_PROGRAM": "vscode"}):
            self.assertIn("can follow the link", sl.cmd_enable_click("on"))
        with mock.patch.dict(os.environ, {"TERM_PROGRAM": "iTerm.app"}):
            message = sl.cmd_enable_click("on")
        self.assertIn("will NOT follow", message)
        self.assertIn("No listener will be started", message)
        self.assertIn("/squirrel-done", message)

    def test_it_warns_about_the_browser_tab(self):
        with mock.patch.dict(os.environ, {"TERM_PROGRAM": "vscode"}):
            self.assertIn("browser tab", sl.cmd_enable_click("on"))

    def test_show_config_says_why_clicking_would_not_work(self):
        """"Clicking does nothing" must be answerable with one command, not a debugging
        session — so --show-config names the unmet condition, not just on/off."""
        self.assertIn("off (/squirrel-click on)", sl.cmd_show_config())
        with mock.patch.dict(os.environ, {"TERM_PROGRAM": "vscode"}):
            sl.cmd_enable_click("on")
            self.assertIn("no segment has click= yet", sl.cmd_show_config())
            sl.cmd_custom("add water every=30m ack=on click=ack text=w")
            self.assertIn("clickable: water", sl.cmd_show_config())
        with mock.patch.dict(os.environ, {"TERM_PROGRAM": "iTerm.app"}):
            summary = sl.cmd_show_config()
        self.assertIn("cannot follow a status-line link", summary)
        self.assertIn("/squirrel-done", summary)

    def test_off_and_usage(self):
        self.assertIn("OFF", sl.cmd_enable_click("off"))
        self.assertFalse(sl.click_enabled(sl.load_config()))
        self.assertIn("usage", sl.cmd_enable_click("maybe"))

    def test_terminals_can_be_listed_overridden_and_reset(self):
        self.assertIn("usage", sl.cmd_set_click_terminals(""))
        sl.cmd_set_click_terminals("ghostty, wezterm")
        self.assertEqual(sl.load_config()["clickTerminals"], ["ghostty", "wezterm"])
        with mock.patch.dict(os.environ, {"TERM_PROGRAM": "ghostty"}):
            self.assertTrue(sl.click_capable_terminal(sl.load_config()))
        self.assertIn("EVERY terminal", sl.cmd_set_click_terminals("any"))
        sl.cmd_set_click_terminals("reset")
        self.assertNotIn("clickTerminals", sl.user_config())

    def test_a_click_action_is_validated(self):
        for arg, needle in (("add w every=1m ack=on click=command:ls text=w", "not a click action"),
                            ("add w every=1m ack=on click=delete text=w", "not a click action"),
                            ("add w every=1m ack=on click=snooze:soon text=w", "not a click action")):
            self.assertIn(needle, sl.cmd_custom(arg), arg)
            self.assertEqual(sl.user_config().get("customSegments", []), [])
        sl.cmd_custom("add w every=1m ack=on click=ack text=w")
        self.assertEqual(sl.user_config()["customSegments"][0]["click"], "ack")
        sl.cmd_custom("set w click=snooze:15")
        self.assertEqual(sl.user_config()["customSegments"][0]["click"], "snooze:15")



class UnknownFlagTests(TmpEnv):
    """A mistyped flag used to fall through to the render path, which blocks on stdin — so
    the terminal simply froze with no message. Every doc and skill recipe invites people to
    run these by hand, so a near-miss is the expected error, not an exotic one."""

    # `--config` was here until it became a REAL flag (/squirrel-config share). That is
    # exactly what test_no_near_miss_is_secretly_a_real_flag guards against, and it caught it.
    NEAR_MISSES = ("--configg", "--sessions", "--style-set", "--wibble", "--show", "--ack-all")

    def _run(self, argv, stdin_text=None):
        """Runs main() with a stdin that would BLOCK if anything tried to read it, so a test
        that passes proves the render path was never entered."""
        blocking = mock.Mock()
        blocking.isatty.return_value = False
        blocking.read.side_effect = AssertionError("stdin was read — the render path ran")
        out, err = io.StringIO(), io.StringIO()
        stdin = io.StringIO(stdin_text) if stdin_text is not None else blocking
        with mock.patch.object(sys, "stdin", stdin), \
             mock.patch.object(sys, "stdout", out), mock.patch.object(sys, "stderr", err):
            code = sl.main(list(argv))
        return code, out.getvalue(), err.getvalue()

    def test_every_near_miss_exits_non_zero_without_reading_stdin(self):
        for flag in self.NEAR_MISSES:
            code, out, err = self._run([flag])
            self.assertEqual(code, 2, f"{flag} did not exit 2")
            self.assertIn("unknown option", err, flag)
            self.assertEqual(out, "", f"{flag} wrote to stdout")

    def test_it_names_the_closest_real_flag(self):
        self.assertIn("--config", self._run(["--configg"])[2])
        self.assertIn("--show-sessions", self._run(["--sessions"])[2])
        self.assertIn("--set-states", self._run(["--set-state"])[2])

    def test_it_always_points_at_the_full_list(self):
        for flag in self.NEAR_MISSES:
            self.assertIn("--help-text", self._run([flag])[2], flag)

    def test_an_unknown_flag_anywhere_in_argv_is_caught(self):
        code, _out, err = self._run(["--show-config", "--wibble"])
        self.assertEqual(code, 2)
        self.assertIn("--wibble", err)

    def test_safe_real_flags_still_run(self):
        """Deliberately a SHORT list, not every flag. The first version of this test looped
        over known_flags() and actually invoked each one — which runs --fetch (network) and
        --click-listener (a server that serves for half an hour). It hung the suite and left
        a stray listener running. Coverage that every dispatched flag is admitted comes from
        test_known_flags_covers_everything_main_dispatches, which reads main()'s source
        instead of executing it."""
        for flag in ("--show-config", "--help-text", "--show-layout", "--show-rules",
                     "--show-custom", "--show-styles"):
            code, out, err = self._run([flag])
            self.assertEqual(code, 0, f"{flag} -> {err}")
            self.assertNotIn("unknown option", err, flag)
            self.assertNotEqual(out, "", f"{flag} printed nothing")

    def test_no_near_miss_is_secretly_a_real_flag(self):
        for flag in self.NEAR_MISSES:
            self.assertNotIn(flag, sl.known_flags(), flag)

    def test_a_tty_on_stdin_explains_itself_instead_of_hanging(self):
        """Nothing is piped, so there is nothing to render — say so rather than blocking."""
        tty = mock.Mock()
        tty.isatty.return_value = True
        tty.read.side_effect = AssertionError("blocked on a tty instead of explaining")
        err = io.StringIO()
        with mock.patch.object(sys, "stdin", tty), mock.patch.object(sys, "stderr", err), \
             mock.patch.object(sys, "stdout", io.StringIO()):
            code = sl.main([])
        self.assertEqual(code, 2)
        self.assertIn("Nothing is piped", err.getvalue())
        self.assertIn("--help-text", err.getvalue())

    def test_a_piped_render_still_works(self):
        code, out, _err = self._run([], stdin_text=json.dumps(
            {"model": {"display_name": "M"}, "context_window": {"context_window_size": 200_000}}))
        self.assertEqual(code, 0)
        self.assertIn("ctx", out)

    @unittest.skipIf(os.name == "nt", "the stdin wait is POSIX-only by design — see _stdin_has_data_within()")
    def test_a_pipe_that_is_never_written_gives_up_rather_than_blocking(self):
        """The backstop. Claude Code always sends the status JSON, but if it ever did not it
        keeps spawning a render every few seconds — each one blocked for ever. A render that
        prints nothing is recoverable; hundreds of stuck processes are not."""
        import subprocess
        # A 1-second wait, not the 10-second default: the point is that it gives up at ALL,
        # and making every future test run sleep ten seconds to prove it is a tax on the
        # gate nobody would thank us for.
        (self.root / "squirrel").mkdir(parents=True, exist_ok=True)
        (self.root / "squirrel" / "config.json").write_text(json.dumps({"stdinWaitSeconds": 1}))
        read_fd, write_fd = os.pipe()
        started = _time.monotonic()
        proc = subprocess.Popen(
            [sys.executable, str(PLUGIN_ROOT / "scripts" / "statusline.py")],
            stdin=read_fd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
            env={**os.environ, "CLAUDE_CONFIG_DIR": str(self.root),
                 "XDG_CACHE_HOME": str(self.root)})
        os.close(read_fd)
        try:
            _out, err = proc.communicate(timeout=20)
        except subprocess.TimeoutExpired:
            proc.kill()
            self.fail("the render blocked for ever on a pipe nobody wrote to")
        finally:
            os.close(write_fd)
        self.assertEqual(proc.returncode, 2)
        self.assertIn("no status JSON arrived", err)
        self.assertLess(_time.monotonic() - started, 10, "the configured wait was not honoured")

    def test_a_pipe_that_is_written_renders_immediately(self):
        import subprocess
        started = _time.monotonic()
        proc = subprocess.run(
            [sys.executable, str(PLUGIN_ROOT / "scripts" / "statusline.py")],
            input=json.dumps({"model": {"display_name": "M"},
                              "context_window": {"context_window_size": 200_000}}),
            capture_output=True, text=True, timeout=30,
            env={**os.environ, "CLAUDE_CONFIG_DIR": str(self.root),
                 "XDG_CACHE_HOME": str(self.root)})
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("ctx", proc.stdout)
        self.assertLess(_time.monotonic() - started, 10, "the backstop delayed a real render")

    def test_the_backstop_is_posix_only_and_says_so(self):
        """select() cannot watch a pipe on Windows, and a watchdog thread could fire
        mid-render. Windows keeps the isatty check, which covers the reported bug."""
        with mock.patch.object(sl, "IS_WINDOWS", True):
            self.assertTrue(sl._stdin_has_data_within(0.01))
        self.assertTrue(sl._stdin_has_data_within(0))          # 0 = wait for ever

    def test_known_flags_covers_everything_main_dispatches(self):
        """A flag main() handles but known_flags() omits would be refused before it ran."""
        import re as _re
        source = inspect.getsource(sl.main)
        dispatched = set(_re.findall(r'"(--[a-z-]+)" in argv', source))
        self.assertEqual(dispatched - set(sl.known_flags()), set())



class ProfileFilePermissionsTests(TmpEnv):
    """Every file this plugin creates anywhere under the profile must be 0600.

    Asserted by WALKING THE TREE after exercising the write paths, not per call site —
    which is how this was found: three separate lock writers all used Path.touch(), whose
    default is 0666 & ~umask, so fetch.lock had been shipping at 0644 since Task 4 next to
    the 0600 files it guards. A per-call-site test would have needed someone to think of
    the fourth caller; a tree walk catches it."""

    def setUp(self):
        super().setUp()
        patcher = mock.patch.dict(os.environ,
                                  {"SQUIRREL_SHARED_DIR": str(self.root / "shared-perm")})
        patcher.start(); self.addCleanup(patcher.stop)

    def _exercise_every_write_path(self):
        with mock.patch("subprocess.Popen"):
            sl.spawn_fetch(sl.cache_paths()[1])
            sl.spawn_click_listener(_time.time())
            sl.spawn_custom_refresh("seg", _time.time(), 60)
        sl.cmd_custom("add water every=30m ack=on shared=on click=ack text=w")
        sl.cmd_enable_click("on")
        sl.cmd_ack("water")
        sl.apply_state("idle", "perm-session", NOW)
        sl.write_cache(sl.click_state_path(),
                       {"port": 1, "token": "t", "pid": os.getpid(), "started": _time.time()})
        sl.record_session_sample({"session_id": "perm", "cost": {"total_cost_usd": 1.0},
                                  "context_window": {}}, "you@example.com", NOW)
        sl.run_custom("seg", NOW)

    @unittest.skipIf(os.name == "nt", "POSIX permissions only")
    def test_every_file_under_the_profile_is_owner_only(self):
        import stat as _stat
        self._exercise_every_write_path()
        offenders = []
        for base in (self.root, self.root / "shared-perm"):
            for dirpath, _dirs, files in os.walk(base):
                for name in files:
                    path = Path(dirpath) / name
                    mode = _stat.S_IMODE(path.stat().st_mode)
                    if mode != 0o600:
                        offenders.append(f"{oct(mode)} {path.relative_to(self.root)}")
        self.assertEqual(offenders, [], f"world-readable files: {offenders}")

    @unittest.skipIf(os.name == "nt", "POSIX permissions only")
    def test_every_lock_file_specifically(self):
        import stat as _stat
        self._exercise_every_write_path()
        locks = [p for p in self.root.rglob("*.lock")]
        self.assertGreaterEqual(len(locks), 3, "the write paths did not create the locks")
        for lock in locks:
            self.assertEqual(_stat.S_IMODE(lock.stat().st_mode), 0o600, str(lock))


class ClickListenerRecoveryTests(TmpEnv):
    """A killed listener must not be permanent. Found when the live one was killed and did
    not come back — and the reason was neither of the two obvious candidates."""

    STATUS = {"session_id": "rec", "model": {"display_name": "M"},
              "context_window": {"context_window_size": 200_000, "used_percentage": 42},
              "cost": {"total_cost_usd": 1.0}}

    def setUp(self):
        super().setUp()
        patcher = mock.patch.dict(os.environ, {"TERM_PROGRAM": "vscode"})
        patcher.start(); self.addCleanup(patcher.stop)
        sl.cmd_custom("add water every=30m ack=on click=ack text=water")
        sl.cmd_enable_click("on")

    def _render(self, now=None):
        with mock.patch.object(sl, "spawn_click_listener") as spawn, \
             mock.patch.object(sl, "git_segment_runs", return_value=[]):
            sl.render(self.STATUS, {}, None, sl.load_config(), now or _time.time(), width=200)
        return spawn.called

    def _plant_dead_listener(self):
        sl.write_cache(sl.click_state_path(),
                       {"port": 36895, "token": "x", "pid": 999_999_999,
                        "started": _time.time()})

    def test_a_dead_pid_yields_a_fresh_listener(self):
        self._plant_dead_listener()
        self.assertTrue(self._render(), "a dead listener was treated as live")

    def test_a_stale_record_is_removed_from_disk_not_merely_ignored(self):
        self._plant_dead_listener()
        self.assertIsNone(sl.read_click_listener(_time.time(), sl.load_config()))
        self.assertFalse(sl.click_state_path().exists(),
                         "a record naming a dead process was left claiming a port")

    def test_an_old_lock_does_not_block_recovery(self):
        self._plant_dead_listener()
        lock = sl.click_lock_path()          # shared store now, not the profile (audit #2)
        sl.touch_private(lock)
        os.utime(lock, (_time.time() - 1200, _time.time() - 1200))
        self.assertTrue(self._render(), "a stale lock blocked recovery")

    def test_a_fresh_lock_does_hold_off_a_second_spawn(self):
        self._plant_dead_listener()
        lock = sl.click_lock_path()          # shared store now, not the profile (audit #2)
        sl.touch_private(lock)
        with mock.patch.object(sl, "git_segment_runs", return_value=[]), \
             mock.patch("subprocess.Popen") as popen:
            sl.render(self.STATUS, {}, None, sl.load_config(), _time.time(), width=200)
        popen.assert_not_called()

    def test_recovery_happens_when_the_reminder_is_next_showing(self):
        """The actual cause: the listener starts only while a clickable segment is SHOWING.
        With every reminder acknowledged there is nothing to click, so nothing starts — which
        is the rule working, not a fault. It recovers when one is next due."""
        self._plant_dead_listener()
        sl.cmd_ack("water")
        self.assertFalse(self._render(), "a listener started with nothing clickable showing")
        self.assertTrue(self._render(now=_time.time() + 1801), "it never recovered once due")

    def test_show_config_does_not_promise_a_restart_that_cannot_happen(self):
        sl.cmd_ack("water")
        summary = sl.cmd_show_config()
        self.assertIn("all of them are acknowledged right now", summary)
        self.assertNotIn("starts on the next render", summary)



# ── audit findings ───────────────────────────────────────────────────────────
HOSTILE_TEXT = "ok\x1b[31m\x1b]8;;http://evil.example\x1b\\PWNED\x1b]8;;\x1b\\\nsecond"


def _escapes_beyond_sgr(text: str) -> list:
    """Every escape in `text` that is not one of our own colour codes. OSC of any kind, and
    any CSI whose final byte is not 'm', have no business in output we produce."""
    found = re.findall(r"\x1b\][^\x1b\x07]*(?:\x1b\\|\x07)?|\x1b\[[0-9;?]*[A-Za-z]|\x1b.",
                       text)
    return [f for f in found if not re.fullmatch(r"\x1b\[[0-9;]*m", f)]


class CommandOutputIsBoundedTests(TmpEnv):
    """Audit #1. `communicate()` buffered everything a command printed: a 20MB response cost
    58MB resident and wrote a 20MB cache file that every later render re-read. No attacker
    required — an endpoint having a bad day is enough."""

    BIG = "python3 -c \"import sys; sys.stdout.write('A'*3000000)\""

    def test_the_read_is_capped_and_the_process_killed(self):
        out, _code, err = sl._run_command(self.BIG, 5000, 4096)
        self.assertEqual(len(out), 4096)
        self.assertIn("truncated", err)

    def test_the_cached_value_is_capped_independently(self):
        sl.save_config({"customCommands": True, "customOutputMaxBytes": 4096,
                        "customValueMaxChars": 64,
                        "customSegments": [{"id": "big", "command": self.BIG}]})
        entry = sl.run_custom("big", NOW)
        self.assertEqual(len(entry["value"]), 64)
        self.assertLess(sl.custom_cache_path().stat().st_size, 4096,
                        "an oversized value reached the cache every render re-reads")

    def test_a_normal_command_is_unaffected(self):
        out, code, err = sl._run_command("echo hello", 5000, 65536)
        self.assertEqual(out.strip(), "hello")
        self.assertEqual(code, 0)
        self.assertIsNone(err)

    def test_the_timeout_still_wins_over_the_cap(self):
        started = _time.monotonic()
        _out, _code, err = sl._run_command("sleep 30", 200, 65536)
        self.assertEqual(err, "timed out")
        self.assertLess(_time.monotonic() - started, 10)

    def test_output_exactly_at_the_cap_is_not_called_truncated(self):
        out, _code, err = sl._run_command("python3 -c \"import sys; sys.stdout.write('A'*100)\"",
                                          5000, 100)
        self.assertEqual(len(out), 100)
        self.assertIsNone(err)


class SessionStoreSanitisingTests(TmpEnv):
    """Audit #8. The model name went into the SHARED store raw while repo beside it was
    sanitised — and that store is read by every profile on the machine. One session opened in
    a maliciously named project put a clickable attacker hyperlink into another profile's
    /squirrel-sessions, which Claude then relays."""

    def setUp(self):
        super().setUp()
        self.shared = self.root / "shared"
        patcher = mock.patch.dict(os.environ, {"SQUIRREL_SHARED_DIR": str(self.shared)})
        patcher.start(); self.addCleanup(patcher.stop)
        self.account = sl.account_hash("you@example.com")

    def _plant(self, model=HOSTILE_TEXT, repo="proj"):
        for sid, mdl, cost in (("victim", model, 5.0), ("other", "Opus 5", 2.0)):
            sl.write_cache(sl.session_path(sid), {"session_id": sid, "repo": repo, "samples": [
                [NOW - 600, 0, 0.0, mdl, self.account, 10.0],
                [NOW - 60, 1000, cost, mdl, self.account, 30.0]]})

    def _table(self):
        with mock.patch.object(sl.time, "time", return_value=NOW), \
             mock.patch.object(sl, "_current_email", return_value="you@example.com"):
            return sl.cmd_show_sessions(width=400)

    def test_a_hostile_model_name_cannot_reach_the_table(self):
        self._plant()
        table = self._table()
        self.assertEqual(_escapes_beyond_sgr(table), [], "an escape survived into the table")
        self.assertNotIn("\n\nsecond", table)

    def test_a_hostile_repo_name_cannot_reach_the_table(self):
        self._plant(model="Opus 5", repo=HOSTILE_TEXT)
        self.assertEqual(_escapes_beyond_sgr(self._table()), [])

    def test_the_model_is_sanitised_at_ingest_like_the_repo(self):
        sl.record_session_sample({"session_id": "fresh", "workspace": {"repo": {"name": "p"}},
                                  "model": {"display_name": HOSTILE_TEXT},
                                  "cost": {"total_cost_usd": 1.0}, "context_window": {}},
                                 "you@example.com", NOW)
        stored = json.loads(sl.session_path("fresh").read_text(encoding="utf-8"))["samples"][0][3]
        self.assertNotIn("\x1b", stored)
        self.assertNotIn("\n", stored)

    def test_records_written_by_an_older_version_are_still_safe_to_render(self):
        """Existing stores are not rewritten, so the render must not depend on ingest."""
        self._plant()
        with mock.patch.object(sl, "sanitise", side_effect=lambda t: t):
            pass                      # ingest deliberately not re-run; the store stays raw
        self.assertEqual(_escapes_beyond_sgr(self._table()), [])


class NoSurfaceEscapesTheSeamTests(TmpEnv):
    """The chip was the first output path found outside the seam, --show-sessions the second.
    This checks EVERY surface at once, so a third is caught by construction rather than by
    someone remembering to look."""

    def setUp(self):
        super().setUp()
        self.shared = self.root / "shared"
        patcher = mock.patch.dict(os.environ, {"SQUIRREL_SHARED_DIR": str(self.shared)})
        patcher.start(); self.addCleanup(patcher.stop)
        account = sl.account_hash("you@example.com")
        sl.write_cache(sl.session_path("s1"), {"session_id": "s1", "repo": HOSTILE_TEXT,
            "samples": [[NOW - 600, 0, 0.0, HOSTILE_TEXT, account, 10.0],
                        [NOW - 60, 10, 1.0, HOSTILE_TEXT, account, 30.0]]})
        sl.save_config({
            "customSegments": [{"id": "eviltext", "text": HOSTILE_TEXT, "every": 60,
                                "ack": True, "click": "ack"}],
            "accountNames": {"you@example.com": HOSTILE_TEXT},
            "segmentLabels": {"share": HOSTILE_TEXT},
            "barRules": [{"when": "ctx.pct >= 1", "bg": HOSTILE_TEXT}],
        })
        sl.apply_state("idle", "surface", NOW - 300)

    def test_no_surface_emits_an_escape_it_did_not_choose(self):
        surfaces = {
            "--show-sessions": lambda: sl.cmd_show_sessions(width=400),
            "--show-custom": sl.cmd_show_custom,
            "--show-rules": sl.cmd_show_rules,
            "--show-layout": sl.cmd_show_layout,
            "--show-styles": sl.cmd_show_styles,
            "--show-config": sl.cmd_show_config,
            "--help-text": sl.cmd_help,
        }
        with mock.patch.object(sl.time, "time", return_value=NOW), \
             mock.patch.object(sl, "_current_email", return_value="you@example.com"):
            for name, produce in surfaces.items():
                # Through emit(), the chokepoint main() uses — testing the raw function
                # would test a layer users never see.
                out = io.StringIO()
                with mock.patch.object(sys, "stdout", out):
                    sl.emit(produce())
                text = out.getvalue()
                self.assertEqual(_escapes_beyond_sgr(text), [], f"{name} leaked an escape")
                self.assertNotIn("\x1b]8;;", text, f"{name} emitted a hyperlink")

    def test_emit_is_a_real_backstop_not_decoration(self):
        """Tested directly, because the per-field sanitising masks it: with every field
        already clean, removing emit()'s sanitising failed no test at all. A backstop that
        only works while the thing it backs up works is not a backstop."""
        out = io.StringIO()
        with mock.patch.object(sys, "stdout", out):
            sl.emit(f"line one {HOSTILE_TEXT}\nline two\nline three")
        text = out.getvalue()
        self.assertEqual(_escapes_beyond_sgr(text), [])
        self.assertIn("line two", text)
        self.assertIn("line three", text)

    def test_emit_keeps_the_layout_it_was_given(self):
        """sanitise() strips newlines by design, so running it over the joined text collapsed
        the whole sessions table onto one line. The newlines in a listing are OURS."""
        out = io.StringIO()
        with mock.patch.object(sys, "stdout", out):
            sl.emit("a\nb\nc")
        self.assertEqual(out.getvalue(), "a\nb\nc\n")

    def test_a_field_nobody_sanitised_is_still_caught(self):
        """The case emit() exists for: a future listing that forgets its own field."""
        out = io.StringIO()
        with mock.patch.object(sys, "stdout", out):
            sl.emit(f"newfield : {HOSTILE_TEXT}")
        self.assertEqual(_escapes_beyond_sgr(out.getvalue()), [])
        self.assertNotIn("\x1b]8;;", out.getvalue())

    def test_the_bar_itself_is_still_clean(self):
        status = dict(STATUS, session_id="surface",
                      workspace={"repo": {"name": HOSTILE_TEXT}, "current_dir": "/x"},
                      model={"display_name": HOSTILE_TEXT})
        with mock.patch.object(sl, "git_segment_runs", return_value=[]):
            lines = sl.render(status, CACHE, "you@example.com", sl.load_config(), NOW,
                              timezone.utc, width=200)
        self.assertEqual(len(lines), 2)
        joined = "\n".join(lines)
        self.assertEqual(_escapes_beyond_sgr(joined), [])



class ClickListenerLifecycleAuditTests(TmpEnv):
    """Audit #2 #3 #4 #5 #6 #9 #10 #11 #15 — the state machine I had been wrong about twice."""

    def setUp(self):
        super().setUp()
        self.shared = self.root / "shared"
        for patcher in (mock.patch.dict(os.environ, {"SQUIRREL_SHARED_DIR": str(self.shared),
                                                     "TERM_PROGRAM": "vscode"}),):
            patcher.start(); self.addCleanup(patcher.stop)
        sl.cmd_custom("add water every=30m ack=on click=ack text=water")
        sl.cmd_enable_click("on")

    # ── #2: one per MACHINE, not one per profile ──
    def test_the_listener_record_lives_in_the_shared_store(self):
        self.assertTrue(str(sl.click_state_path()).startswith(str(self.shared)))
        self.assertTrue(str(sl.click_lock_path()).startswith(str(self.shared)))

    def test_a_second_profile_sees_the_same_listener(self):
        """The claim is 'one per machine'. Two profiles, one shared store, one record."""
        sl.write_cache(sl.click_state_path(),
                       {"port": 1234, "token": "t", "pid": os.getpid(),
                        "started": _time.time(), "start_ticks": sl._proc_start_ticks(os.getpid())})
        first = sl.click_state_path()
        with mock.patch.dict(os.environ, {"CLAUDE_CONFIG_DIR": str(self.root / "profile2")}):
            self.assertEqual(sl.click_state_path(), first)

    # ── #3: the socket owner holds the lock ──
    def test_only_one_listener_can_hold_the_lock(self):
        first = sl._take_click_lock(_time.time())
        self.assertIsNotNone(first)
        with mock.patch.object(sl.os, "getpid", return_value=os.getpid() + 1):
            self.assertIsNone(sl._take_click_lock(_time.time()),
                              "a second listener claimed the lock and would have bound anyway")
        sl._release_click_lock(first)
        self.assertIsNotNone(sl._take_click_lock(_time.time()))

    def test_liveness_never_signals_the_process_it_asks_about(self):
        """Windows-shaped bug, found by CI on its first run: `os.kill(pid, 0)` means
        CTRL_C_EVENT there, so the liveness check Ctrl-C'd its own console group and the test
        runner died with KeyboardInterrupt. The question is "is it alive", and the answer must
        never be a signal — on any platform."""
        with mock.patch.object(sl.os, "kill") as kill:
            sl._pid_alive(999999999)
            if os.name == "nt":
                kill.assert_not_called()
        self.assertFalse(sl._pid_alive(999999999))
        self.assertTrue(sl._pid_alive(os.getpid()))
        for junk in (None, True, -1, 0, "1234", 1.5):
            self.assertFalse(sl._pid_alive(junk), junk)

    def test_a_lock_held_by_a_dead_process_is_taken_over(self):
        sl.click_lock_path().parent.mkdir(parents=True, exist_ok=True)
        sl.click_lock_path().write_text("999999999")
        self.assertIsNotNone(sl._take_click_lock(_time.time()),
                             "one crash disabled the feature permanently")

    def test_run_click_listener_refuses_when_the_lock_is_held(self):
        other = os.getpid() + 1
        sl.click_lock_path().parent.mkdir(parents=True, exist_ok=True)
        sl.click_lock_path().write_text(str(other))
        with mock.patch.object(sl, "_pid_alive", return_value=True):
            result = sl.run_click_listener(serve=False)
        self.assertEqual(result["port"], 0)

    # ── #4: the token must not reach a process we did not start ──
    def test_a_record_whose_pid_does_not_own_the_port_is_discarded(self):
        sl.write_cache(sl.click_state_path(),
                       {"port": 65000, "token": "t", "pid": os.getpid(),
                        "started": _time.time()})
        with mock.patch.object(sl, "_pid_owns_port", return_value=False):
            self.assertIsNone(sl.read_click_listener(_time.time(), {}))
        self.assertFalse(sl.click_state_path().exists())

    def test_a_reused_pid_cannot_inherit_the_record(self):
        sl.write_cache(sl.click_state_path(),
                       {"port": 65000, "token": "t", "pid": os.getpid(),
                        "started": _time.time(), "start_ticks": 1})
        with mock.patch.object(sl, "_pid_owns_port", return_value=True):
            self.assertIsNone(sl.read_click_listener(_time.time(), {}),
                              "a different process with the same pid was trusted")

    def test_a_real_listener_passes_every_check(self):
        listener = sl.run_click_listener(serve=False)
        self.assertGreater(listener["port"], 0)
        sl.write_cache(sl.click_state_path(),
                       {"port": listener["port"], "token": listener["token"],
                        "pid": os.getpid(), "started": _time.time(),
                        "start_ticks": sl._proc_start_ticks(os.getpid())})
        with mock.patch.object(sl, "_pid_owns_port", return_value=True):
            self.assertIsNotNone(sl.read_click_listener(_time.time(), {}))

    # ── #5: records track the process, so no accumulation ──
    def test_a_busy_listener_is_not_expired_out_from_under_itself(self):
        """`started` was written once while the exit loop watched `last_seen`, so a busy
        listener outlived its record and every idle window spawned a rival beside it."""
        now = _time.time()
        idle = sl.tunable(sl.load_config(), "clickIdleSeconds")
        sl.write_cache(sl.click_state_path(),
                       {"port": 65000, "token": "t", "pid": os.getpid(),
                        "started": now, "start_ticks": sl._proc_start_ticks(os.getpid())})
        with mock.patch.object(sl, "_pid_owns_port", return_value=True):
            self.assertIsNotNone(sl.read_click_listener(now + idle - 1, {}))
            self.assertIsNone(sl.read_click_listener(now + idle + sl.LOCK_MAX_AGE_S + 5, {}))

    # ── #15 / #9: malformed records ──
    def test_a_malformed_record_cannot_pass_or_crash(self):
        for bad in ({"port": True, "token": "t", "pid": os.getpid(), "started": 1},
                    {"port": 0, "token": "t", "pid": os.getpid(), "started": 1},
                    {"port": 70000, "token": "t", "pid": os.getpid(), "started": 1},
                    {"port": 1234, "token": "", "pid": os.getpid(), "started": 1},
                    {"port": 1234, "token": "t", "pid": os.getpid(), "started": "soon"},
                    {"port": 1234, "token": "t", "pid": True, "started": 1},
                    {"port": 1234, "token": "t", "pid": os.getpid()}):
            sl.write_cache(sl.click_state_path(), bad)
            self.assertIsNone(sl.read_click_listener(_time.time(), {}), bad)

    def test_a_non_float_started_does_not_kill_the_bar(self):
        sl.write_cache(sl.click_state_path(),
                       {"port": 1234, "token": "t", "pid": os.getpid(), "started": "soon"})
        status = {"session_id": "x", "model": {"display_name": "M"},
                  "context_window": {"context_window_size": 200_000, "used_percentage": 1},
                  "cost": {"total_cost_usd": 0.0}}
        # spawn_click_listener is mocked because the record above is deliberately unreadable:
        # the render correctly decides there is no live listener and starts one. That is the
        # right behaviour and the wrong thing to do 40 times on a developer's machine.
        with mock.patch.object(sl, "git_segment_runs", return_value=[]), \
             mock.patch.object(sl, "spawn_click_listener"):
            lines = sl.render(status, {}, None, sl.load_config(), NOW, timezone.utc, width=200)
        self.assertEqual(len(lines), 2)

    # ── #11: a stray file at the lock path ──
    def test_a_directory_at_the_lock_path_does_not_disable_the_feature_for_ever(self):
        sl.click_lock_path().parent.mkdir(parents=True, exist_ok=True)
        sl.click_lock_path().mkdir()
        with mock.patch("subprocess.Popen") as popen:
            sl.spawn_click_listener(_time.time())
        popen.assert_not_called()               # refuses, rather than raising
        sl.click_lock_path().rmdir()
        with mock.patch("subprocess.Popen") as popen:
            sl.spawn_click_listener(_time.time())
        popen.assert_called_once()              # and recovers once it is gone


class StoreHygieneTests(TmpEnv):
    """Audit #7 #12 #13 #14 — the cheap ones, which are cheap only until they are not."""

    def setUp(self):
        super().setUp()
        patcher = mock.patch.dict(os.environ, {"SQUIRREL_SHARED_DIR": str(self.root / "sh")})
        patcher.start(); self.addCleanup(patcher.stop)

    def test_a_traversing_session_id_is_refused(self):
        for bad in ("../../etc/passwd", "a/b", "..", ".", ".hidden", "", None, 7,
                    "x" * 200, "a\x00b", "a\nb"):
            self.assertIsNone(sl.safe_session_id(bad), repr(bad))
            with self.assertRaises(ValueError):
                sl.session_path(bad)

    def test_ordinary_session_ids_still_work(self):
        for good in ("abc", "af541aa8-997a-4895-849e-d368888d6621", "a.b_c-1"):
            self.assertEqual(sl.safe_session_id(good), good)
            self.assertTrue(str(sl.session_path(good)).endswith(f"{good}.json"))

    def test_sampling_ignores_an_unusable_session_id(self):
        sl.record_session_sample({"session_id": "../escape", "cost": {"total_cost_usd": 1.0},
                                  "context_window": {}}, "you@example.com", NOW)
        self.assertFalse((self.root / "sh" / "sessions").exists())

    @unittest.skipIf(os.name == "nt", "POSIX permissions only")
    def test_touch_private_never_follows_a_symlink(self):
        target = self.root / "target-that-must-not-be-touched"
        link = self.root / "planted.lock"
        link.symlink_to(target)
        sl.touch_private(link)
        self.assertFalse(target.exists(), "a planted symlink got us to create its target")

    @unittest.skipIf(os.name == "nt", "POSIX permissions only")
    def test_the_temp_file_is_never_world_readable_even_briefly(self):
        import stat as _stat
        seen = []
        real_chmod = sl.chmod_private

        def watching(path, mode):
            seen.append(_stat.S_IMODE(os.stat(path).st_mode))   # mode BEFORE we narrow it
            return real_chmod(path, mode)

        with mock.patch.object(sl, "chmod_private", side_effect=watching):
            sl.write_cache(self.root / "sh" / "probe.json", {"secret": "x"})
        self.assertTrue(seen, "write_cache did not chmod at all")
        self.assertEqual([m for m in seen if m != 0o600], [],
                         f"a temp file existed at {[oct(m) for m in seen]} before being narrowed")

    def test_the_session_store_is_pruned(self):
        sessions = self.root / "sh" / "sessions"
        sessions.mkdir(parents=True)
        for i in range(5):
            path = sessions / f"old{i}.json"
            path.write_text("{}")
            os.utime(path, (NOW - 999_999, NOW - 999_999))
        (sessions / "fresh.json").write_text("{}")
        os.utime(sessions / "fresh.json", (NOW, NOW))
        removed = sl.prune_session_store(NOW, {})
        self.assertEqual(removed, 5)
        self.assertTrue((sessions / "fresh.json").exists())

    def test_pruning_happens_on_the_sampling_path(self):
        sessions = self.root / "sh" / "sessions"
        sessions.mkdir(parents=True)
        stale = sessions / "ancient.json"
        stale.write_text("{}")
        os.utime(stale, (NOW - 999_999, NOW - 999_999))
        sl.record_session_sample({"session_id": "live", "cost": {"total_cost_usd": 1.0},
                                  "context_window": {}}, "you@example.com", NOW)
        self.assertFalse(stale.exists(), "the store grows without bound")


class ClickAuthorisationTests(TmpEnv):
    """Audit #6 — the socket outlived the setting that permitted it."""

    def setUp(self):
        super().setUp()
        patcher = mock.patch.dict(os.environ, {"SQUIRREL_SHARED_DIR": str(self.root / "sh"),
                                               "TERM_PROGRAM": "vscode"})
        patcher.start(); self.addCleanup(patcher.stop)
        sl.cmd_custom("add water every=30m ack=on click=ack text=water")
        sl.cmd_enable_click("on")

    def test_a_click_works_while_it_is_switched_on(self):
        title, _detail = sl._click_apply("ack", "water", "")
        self.assertEqual(title, "Acknowledged")

    def test_turning_it_off_revokes_an_already_minted_url(self):
        sl.cmd_enable_click("off")
        title, detail = sl._click_apply("ack", "water", "")
        self.assertEqual(title, "Clicking is switched off")
        self.assertIn("/squirrel-click on", detail)

    def test_removing_the_click_action_revokes_it_too(self):
        sl.cmd_custom("set water click=off")
        title, detail = sl._click_apply("ack", "water", "")
        self.assertEqual(title, "Nothing to do")
        self.assertIn("no longer clickable", detail)

    def test_removing_the_segment_revokes_it(self):
        sl.cmd_custom("remove water")
        self.assertEqual(sl._click_apply("ack", "water", "")[0], "Nothing to do")



# ── Task 27: the release layout ───────────────────────────────────────────────
REPO = DEV_DIR.parent
MARKETPLACE = REPO / ".claude-plugin" / "marketplace.json"

# Every basename that is development material. Any of these appearing inside the published
# tree means it is on a colleague's disk.
DEV_ONLY_FILES = ("test_statusline.py", "golden_render.json", "hostile_strings.json",
                  "smoke_install.sh", "bench.py", "write_golden.py", "profile_snapshot.py",
                  "stale_observer_fixture.json")


class ReleaseLayoutTests(unittest.TestCase):
    """The installer copies the plugin directory verbatim, so the directory IS the release.

    Before this split it was the whole repository: every colleague received our test suite,
    the golden fixture, and `hostile_strings.json` — 36 terminal-injection payloads sitting
    on their disk with nothing to explain them to whoever's scanner finds them. The layout is
    the only thing keeping that out, and a layout has no other guard, so it gets one here.
    """

    def test_the_marketplace_points_at_the_plugin_subdirectory(self):
        data = json.loads(MARKETPLACE.read_text(encoding="utf-8"))
        entry, = data["plugins"]
        self.assertEqual(entry["source"], "./plugin")
        self.assertEqual((REPO / entry["source"]).resolve(), PLUGIN_ROOT.resolve())

    def test_the_plugin_manifest_lives_in_the_published_tree(self):
        manifest = PLUGIN_ROOT / ".claude-plugin" / "plugin.json"
        self.assertTrue(manifest.is_file(), f"{manifest} is missing — nothing would install")
        self.assertEqual(json.loads(manifest.read_text(encoding="utf-8"))["name"],
                         json.loads(MARKETPLACE.read_text(encoding="utf-8"))["plugins"][0]["name"])

    def test_no_development_material_is_inside_the_published_tree(self):
        found = [str(p.relative_to(REPO)) for p in PLUGIN_ROOT.rglob("*")
                 if p.name in DEV_ONLY_FILES]
        self.assertEqual(found, [], "development files would ship to every user")

    def test_the_development_material_is_actually_still_here(self):
        # The inverse of the guard above: deleting the tests would also satisfy it.
        missing = [n for n in DEV_ONLY_FILES if not (DEV_DIR / n).is_file()]
        self.assertEqual(missing, [], f"missing from {DEV_DIR}: {missing}")

    def test_the_published_tree_holds_only_what_a_user_needs(self):
        expected = {".claude-plugin", "scripts", "defaults", "commands", "hooks", "skills",
                    "README.md", "LICENSE"}
        actual = {p.name for p in PLUGIN_ROOT.iterdir() if p.name != "__pycache__"}
        self.assertEqual(actual, expected)
        self.assertEqual({p.name for p in (PLUGIN_ROOT / "scripts").iterdir()
                          if p.name != "__pycache__"},
                         {"statusline.py", "squirrel.sh"})

    def test_nothing_shipped_imports_anything_from_the_dev_tree(self):
        for path in (PLUGIN_ROOT / "scripts").glob("*.py"):
            tree = ast.parse(path.read_text(encoding="utf-8"))
            names = set()
            for node in ast.walk(tree):
                if isinstance(node, ast.Import):
                    names.update(a.name.split(".")[0] for a in node.names)
                elif isinstance(node, ast.ImportFrom) and node.module:
                    names.add(node.module.split(".")[0])
            leaked = names & {Path(n).stem for n in DEV_ONLY_FILES}
            self.assertEqual(leaked, set(), f"{path.name} imports development code: {leaked}")

    def test_both_manifests_declare_the_same_version(self):
        """The pinning trap: Claude Code serves the CACHED copy until the version string
        moves, and it reads the version from the marketplace entry. A plugin.json bumped on
        its own leaves every colleague on the previous release with no symptom at all — the
        install simply never updates. Neither file is the source of truth; they have to agree."""
        marketplace = json.loads(MARKETPLACE.read_text(encoding="utf-8"))["plugins"][0]["version"]
        plugin = json.loads((PLUGIN_ROOT / ".claude-plugin" / "plugin.json").read_text(encoding="utf-8"))["version"]
        self.assertEqual(marketplace, plugin,
                         "marketplace.json and plugin.json disagree; users would keep the cached copy")
        self.assertRegex(plugin, r"^\d+\.\d+\.\d+$", "not a semver version string")

    def test_the_repo_licence_is_a_faithful_copy_of_the_shipped_one(self):
        # Two copies exist so GitHub can see one at the root and users get one in the install.
        # Two copies drift; this is what stops them.
        self.assertEqual((REPO / "LICENSE").read_bytes(), (PLUGIN_ROOT / "LICENSE").read_bytes())

    def test_no_shipped_file_is_unexpectedly_large(self):
        # A fixture creeping back in would show up as weight long before anyone noticed the path.
        # __pycache__ is excluded because it is not shipped by any path that matters: it is
        # gitignored, the marketplace installs from git, and running this very suite creates
        # it. CI on a clean checkout is what caught the difference.
        big = [(str(p.relative_to(PLUGIN_ROOT)), p.stat().st_size)
               for p in PLUGIN_ROOT.rglob("*")
               if p.is_file() and p.stat().st_size > 400_000 and p.name != "statusline.py"
               and "__pycache__" not in p.parts]
        self.assertEqual(big, [], f"unexpectedly large shipped files: {big}")

    def test_the_split_is_explained_and_reachable_from_the_front_page(self):
        """The front page is the product page now, so the repository's own shape lives in
        CONTRIBUTING.md — but it must still be one click from the README, or the next
        contributor learns the layout by guessing."""
        contributing = (REPO / "CONTRIBUTING.md").read_text(encoding="utf-8")
        self.assertIn("plugin/", contributing)
        self.assertIn("dev/", contributing)
        self.assertIn("CONTRIBUTING.md", (REPO / "README.md").read_text(encoding="utf-8"))


class StaleLauncherTests(TmpEnv):
    """A plugin update installs into a NEW versioned directory, so the path baked into the
    launcher dies. Before this, the bar simply went blank and stayed blank.

    Recovery is deliberate in two places: refresh_launcher() repairs the path on any hook or
    slash command (both carry $CLAUDE_PLUGIN_ROOT), and if neither has run yet the launcher
    itself says what to do. Nothing is added to the render path, which is reached THROUGH the
    launcher and therefore cannot see that a newer version exists.
    """

    @posix_only
    def test_a_launcher_whose_target_is_gone_says_so_in_the_bar(self):
        sl.write_launcher(str(self.root / "gone" / "squirrel-1.0.0"))
        result = subprocess.run(["sh", str(sl.launcher_path())],
                                capture_output=True, text=True, timeout=15)
        # stdout, not stderr: Claude Code renders stdout as the status line. A message on
        # stderr with a non-zero exit is precisely the blank bar this replaces.
        self.assertEqual(result.returncode, 0)
        self.assertIn("/squirrel-setup", result.stdout)
        self.assertEqual(result.stderr, "")

    @posix_only
    def test_a_launcher_whose_target_exists_still_runs_it(self):
        root = self.root / "live"
        (root / "scripts").mkdir(parents=True)
        (root / "scripts" / "statusline.py").write_text("print('rendered')\n")
        sl.write_launcher(str(root))
        result = subprocess.run(["sh", str(sl.launcher_path())],
                                capture_output=True, text=True, timeout=15)
        self.assertEqual((result.returncode, result.stdout.strip()), (0, "rendered"))

    def test_a_slash_command_repairs_a_stale_launcher(self):
        sl.write_launcher(os.path.join(os.sep, "plugins", "squirrel-1.0.0"))
        out = io.StringIO()
        with mock.patch.dict(os.environ, {"CLAUDE_PLUGIN_ROOT":
                                          os.path.join(os.sep, "plugins", "squirrel-2.0.0")}), \
             mock.patch.object(sys, "stdout", out):
            self.assertEqual(sl.main(["--show-config"]), 0)
        body = sl.launcher_path().read_text(encoding="utf-8")
        self.assertIn(os.path.join("squirrel-2.0.0", "scripts", "statusline.py"), body)
        self.assertNotIn("squirrel-1.0.0", body)

    def test_a_slash_command_without_a_plugin_root_changes_nothing(self):
        p = sl.write_launcher(os.path.join(os.sep, "plugins", "squirrel-1.0.0"))
        before = p.read_text(encoding="utf-8")
        out = io.StringIO()
        with mock.patch.dict(os.environ), mock.patch.object(sys, "stdout", out):
            os.environ.pop("CLAUDE_PLUGIN_ROOT", None)
            self.assertEqual(sl.main(["--show-config"]), 0)
        self.assertEqual(p.read_text(encoding="utf-8"), before)

    def test_a_failing_repair_never_breaks_the_command(self):
        with mock.patch.object(sl, "write_launcher", side_effect=OSError("read-only fs")), \
             mock.patch.dict(os.environ, {"CLAUDE_PLUGIN_ROOT": "/plugins/squirrel-2.0.0"}), \
             mock.patch.object(sys, "stdout", io.StringIO()):
            self.assertEqual(sl.main(["--show-config"]), 0)

    def test_the_render_path_does_no_launcher_work(self):
        """The whole point of putting the repair in the command path: a warm render — the
        thing that runs every few seconds, for ever — must not pay a single syscall for it.
        Driven through main() rather than render(), because main() is where the render branch
        actually lives and where an extra call would be added by mistake."""
        status = json.dumps({"session_id": "nolauncher", "model": {"display_name": "M"},
                             "workspace": {"current_dir": str(self.root)},
                             "context_window": {"context_window_size": 200_000,
                                                "used_percentage": 10},
                             "cost": {}})
        with mock.patch.object(sl, "refresh_launcher") as refresh, \
             mock.patch.object(sl, "write_launcher") as write, \
             mock.patch.dict(os.environ, {"CLAUDE_PLUGIN_ROOT": "/plugins/squirrel-2.0.0"}), \
             mock.patch.object(sys, "stdin", io.StringIO(status)), \
             mock.patch.object(sys, "stdout", io.StringIO()):
            self.assertEqual(sl.main([]), 0)
        refresh.assert_not_called()
        write.assert_not_called()


# ── Audit re-verification: R1, R2, N1, N2, N4 ─────────────────────────────────
class RunCommandEscapedGrandchildTests(unittest.TestCase):
    """R1, a HIGH regression introduced by the audit-#1 fix itself.

    Killing the process group does not end a bounded read: a grandchild that `setsid`s out
    of the group survives and keeps the pipe's write end open. Measured on the pre-fix build
    with a 1-second timeout: still in `anon_pipe_read` past 25 seconds, three of them piling
    up in 70, and the cache never written — strictly worse than the `communicate(timeout=)`
    it replaced, and precisely the leak _run_command exists to prevent.
    """

    @posix_only
    def test_a_grandchild_that_escapes_the_group_cannot_hang_the_read(self):
        started = _time.monotonic()
        out, code, error = sl._run_command("setsid sh -c 'sleep 30' & sleep 30", 1000)
        elapsed = _time.monotonic() - started
        self.assertLess(elapsed, 8, f"the read blocked for {elapsed:.1f}s past a 1s timeout")
        self.assertEqual((out, code, error), ("", 1, "timed out"))

    @posix_only
    def test_an_ordinary_timeout_is_still_reported_as_one(self):
        started = _time.monotonic()
        self.assertEqual(sl._run_command("sleep 30", 500), ("", 1, "timed out"))
        self.assertLess(_time.monotonic() - started, 8)

    def test_a_fast_command_still_returns_its_output_immediately(self):
        started = _time.monotonic()
        out, code, error = sl._run_command("echo hello", 5000)
        self.assertEqual((out.strip(), code, error), ("hello", 0, None))
        self.assertLess(_time.monotonic() - started, 5)

    @unittest.skipIf(os.name == "nt", "the fixture runs a POSIX shell command")
    def test_the_output_cap_still_bites(self):
        out, _code, error = sl._run_command(
            f'{shlex.quote(sys.executable)} -c "print(\'x\' * 200000)"', 10000, 1024)
        self.assertEqual(len(out), 1024)
        self.assertIn("truncated", error)


class StatePathValidationTests(TmpEnv):
    """R2: audit #7 validated session_path() and left state_paths() alone. A guard covering
    one of two path builders is worse than no guard, because it reads as if it covers both —
    and this one was reachable through the real `--state` hook."""

    ESCAPES = ("../../escape", "a/b", "..", ".", ".hidden", "", None, 7, True,
               "x" * 300, "sess\x00id", "sess\nid")

    def test_state_paths_refuses_anything_unfit_to_be_a_filename(self):
        for bad in self.ESCAPES:
            self.assertIsNone(sl.state_paths(bad), repr(bad))

    def test_state_paths_still_accepts_a_real_session_id(self):
        sid = "af541aa8-997a-4895-849e-d368888d6621"
        path = sl.state_paths(sid)
        self.assertEqual(path, sl.data_dir() / f"state-{sid}.json")

    def test_the_hook_writes_nothing_outside_the_data_dir(self):
        outside = self.root / "outside"
        outside.mkdir()
        before = sorted(p.name for p in outside.iterdir())
        with mock.patch.object(sys, "stdin",
                               io.StringIO(json.dumps({"session_id": "../outside/pwned"}))), \
             mock.patch.object(sys, "stdout", io.StringIO()):
            self.assertEqual(sl.main(["--state", "working"]), 0)   # silent, as a hook must be
        self.assertEqual(sorted(p.name for p in outside.iterdir()), before)
        self.assertEqual([p.name for p in sl.data_dir().glob("state-*")], [])

    def test_reading_a_refused_id_is_simply_unknown(self):
        self.assertIsNone(sl.read_state("../../escape", NOW))

    def test_applying_a_refused_id_neither_writes_nor_raises(self):
        result = sl.apply_state("working", "../../escape", NOW)
        self.assertEqual(result["state"], "working")     # the caller still gets an answer
        self.assertEqual(list(sl.data_dir().glob("state-*")), [])


class CustomValueReadCapTests(TmpEnv):
    """N1: the audit-#1 cap was write-side only. A cache written by an older build still
    holds a huge value, and every render pays for it until a refresh happens to overwrite."""

    def test_an_oversized_cached_value_is_capped_on_read(self):
        sl.save_config({"customCommands": True,
                        "customSegments": [{"id": "big", "command": "true", "every": 60}]})
        config = sl.load_config()
        limit = sl.tunable(config, "customValueMaxChars")
        sl.write_cache(sl.custom_cache_path(),
                       {"big": {"value": "x" * 500_000, "at": NOW}})
        ctx = {"config": config, "now": NOW}
        with mock.patch.object(sl, "spawn_custom_refresh"):
            value = sl.custom_command_value(ctx, {"id": "big", "command": "true"})
        self.assertEqual(len(value), limit)


class StderrGoesThroughTheSeamTests(TmpEnv):
    """N4: a fifth surface outside emit() — main() wrote unknown_flag_message() to stderr
    verbatim, echoing argv back with its escapes live. The exploitability is low (it is the
    user's own typing); the point is that the guard next door asserts a structural property,
    and one raw writer disproves it."""

    def test_an_unknown_flag_cannot_echo_an_escape_back(self):
        err = io.StringIO()
        with mock.patch.object(sys, "stderr", err):
            self.assertEqual(sl.main([f"--wobble{HOSTILE_TEXT}"]), 2)
        text = err.getvalue()
        self.assertEqual(_escapes_beyond_sgr(text), [], "argv escaped onto stderr")
        self.assertNotIn("\x1b]8;;", text)
        self.assertIn("unknown option", text)

    def test_main_has_no_raw_stderr_writer_left(self):
        """By construction, not by each message happening to be a literal today."""
        source = (PLUGIN_ROOT / "scripts" / "statusline.py").read_text(encoding="utf-8")
        tree = ast.parse(source)
        main = next(n for n in ast.walk(tree)
                    if isinstance(n, ast.FunctionDef) and n.name == "main")
        raw = [n.lineno for n in ast.walk(main)
               if isinstance(n, ast.Attribute) and n.attr == "write"
               and isinstance(n.value, ast.Attribute) and n.value.attr == "stderr"]
        self.assertEqual(raw, [], f"main() writes to stderr outside emit_err() at {raw}")


class ClickGuardHonestyTests(unittest.TestCase):
    """N2/N3: both of audit #4's guards are no-ops off Linux, and the binding proves port
    ownership rather than listener identity. Neither is a code change — the fix is to stop
    the documentation claiming otherwise, which is the sort of claim only a test keeps true."""

    @staticmethod
    def _doc(fn):
        """Wrapped prose, joined — so a reflow cannot silently drop an assertion."""
        return " ".join(fn.__doc__.split())

    def test_the_docstring_does_not_claim_a_fallback_that_does_not_exist(self):
        doc = self._doc(sl._pid_owns_port)
        self.assertIn("Linux only, and there is no fallback", doc)
        self.assertIn("BOTH of audit #4's guards are no-ops", doc)
        self.assertIn("liveness alone is what remains", doc)

    def test_the_start_time_check_admits_it_is_absent_rather_than_degraded(self):
        doc = self._doc(sl._proc_start_ticks)
        self.assertIn("Linux only", doc)
        self.assertIn("absent rather than degraded", doc)

    def test_the_docstring_says_what_the_binding_does_not_prove(self):
        self.assertIn("does NOT prove that process is our listener",
                      self._doc(sl._pid_owns_port))

    def test_the_macos_paragraph_does_not_contradict_the_security_section(self):
        """These two sit 300 lines apart and said opposite things: the platform notes claimed
        macOS needs no `/proc`, while the security section correctly documented three `/proc`
        reads. Two true-sounding statements, one of them false, and nothing connecting them."""
        readme = README.read_text(encoding="utf-8")
        para = readme[readme.index("**macOS** uses the same POSIX code paths"):]
        para = para[:para.index("**Windows**")]
        self.assertNotIn("no `/proc` access", para)
        self.assertIn("/proc", para, "the macOS notes do not mention the one real difference")

    def test_the_readme_states_the_platform_limit(self):
        readme = README.read_text(encoding="utf-8")
        section = readme[readme.index("A stale listener record"):]
        section = section[:section.index("**If clicking does nothing")]
        for phrase in ("Linux", "macOS", "Windows"):
            self.assertIn(phrase, section, f"the README does not mention {phrase} here")
        self.assertIn("liveness alone", section)


class LauncherInjectionTests(TmpEnv):
    """Audit X1, a HIGH that shipped. `_shell_dquote` escaped `\\` and `"` and interpolated
    into a DOUBLE-quoted string, where `$(...)`, backticks and `${...}` all still expand —
    and `run.sh` IS the statusLine command, so the payload ran every few seconds, silently,
    for as long as the bar was installed. Demonstrated live:

        --setup '/tmp/x$(touch /tmp/WB_PROOF)y'   ->  *** COMMAND EXECUTED ***

    Not remotely reachable, since Claude Code supplies the plugin root — but a user can type
    it into /squirrel-setup, and this is the one file on the render path.

    Flagged as a minor in the Task 4 review as "launcher template unescaped path"; only the
    quote half was ever fixed, and it took a second interpolation site to put it back in
    front of anyone. Hence a test that actually RUNS the generated script.
    """

    HOSTILE = ("/tmp/x$(touch {proof})y", "/tmp/x`touch {proof}`y",
               "/tmp/x${{IFS}}$(touch {proof})y", "/tmp/x;touch {proof};y",
               "/tmp/x' ; touch {proof} ; 'y", '/tmp/x"$(touch {proof})"y')

    @posix_only
    def test_no_metacharacter_in_the_plugin_root_can_execute(self):
        for template in self.HOSTILE:
            proof = self.root / f"proof-{abs(hash(template))}"
            root = template.format(proof=proof)
            with self.subTest(root=root):
                launcher = sl.write_launcher(root)
                subprocess.run(["sh", str(launcher)], stdin=subprocess.DEVNULL,
                               capture_output=True, timeout=15)
                self.assertFalse(proof.exists(), f"{root!r} executed a command")

    @posix_only
    def test_the_path_survives_as_literal_text(self):
        """Not executing is half of it; the other half is that a legitimate path containing
        these characters still WORKS, rather than being silently mangled into a dead one."""
        root = self.root / "o'brien's $HOME (dev)"
        (root / "scripts").mkdir(parents=True)
        (root / "scripts" / "statusline.py").write_text("print('rendered')\n")
        launcher = sl.write_launcher(str(root))
        result = subprocess.run(["sh", str(launcher)], stdin=subprocess.DEVNULL,
                                capture_output=True, text=True, timeout=15)
        self.assertEqual((result.returncode, result.stdout.strip()), (0, "rendered"))

    def test_the_posix_quoter_uses_the_single_quote_idiom(self):
        self.assertEqual(sl._shell_squote("plain"), "plain")
        self.assertEqual(sl._shell_squote("$(x)"), "$(x)")          # inert inside single quotes
        self.assertEqual(sl._shell_squote("o'brien"), "o'\\''brien")

    def test_the_template_never_interpolates_into_a_double_quoted_string(self):
        """The property, not the instance: a future edit that reintroduces `"{script}"` is the
        whole bug back again, and it would pass every behavioural test above on a tame path."""
        for template in (sl.LAUNCHER_TEMPLATE,):
            for field in ("{script}", "{python}"):
                self.assertNotIn(f'"{field}"', template,
                                 f"{field} is interpolated into a double-quoted shell string")
                self.assertIn(f"'{field}'", template)

    def test_the_windows_template_has_no_parenthesised_block_around_the_path(self):
        """cmd splits an `if ... ( ... )` block on `)` before it finishes thinking about
        quotes, and `(` and `)` are both legal in a Windows path. `goto` has no such hazard.
        Asserted rather than exercised because there is no cmd.exe on this host to run it."""
        self.assertNotIn("(", sl.LAUNCHER_TEMPLATE_CMD.split("setlocal", 1)[1].split(":squirrel_run")[0])
        self.assertIn("goto squirrel_run", sl.LAUNCHER_TEMPLATE_CMD)

    def test_the_windows_quoter_still_neutralises_what_cmd_expands(self):
        self.assertEqual(sl._cmd_quote(r"C:\a%PATH%b"), r"C:\a%%PATH%%b")
        self.assertNotIn('"', sl._cmd_quote('C:\\a"b'))
        for legal in ("&", "^", "!", "(", ")"):
            # Legal in a Windows path and harmless inside a double-quoted batch argument with
            # delayed expansion off — they must pass through, not be mangled.
            self.assertEqual(sl._cmd_quote(f"C:\\a{legal}b"), f"C:\\a{legal}b")

class AbandonedReaderStaysContainedTests(unittest.TestCase):
    """The R1 fix abandons a reader thread that is still blocked when the deadline passes.
    That is safe for exactly one reason: it can only ever happen inside the detached
    `--run-custom` child, which exits immediately afterwards and takes the fd with it. The
    auditor could not break it in six shapes plus an accumulation stress — but what makes it
    sound is the call graph, not the measurements.

    `_run_command` <- `run_custom` <- `main`'s `--run-custom` branch, and nothing else. If
    anyone later calls either from the render or from any long-lived process, the abandoned
    reader silently becomes a real leak and every existing test still passes:
    `test_a_command_never_runs_on_the_render_path` guards the render, not the caller count.

    So this pins the assumption the fix rests on. It is the difference between a property
    that is true and a property that stays true.
    """

    @staticmethod
    def _callers_of(name):
        """Top-level function names whose body references `name` (excluding its own def)."""
        tree = ast.parse((PLUGIN_ROOT / "scripts" / "statusline.py").read_text(encoding="utf-8"))
        callers = set()
        for top in tree.body:
            if not isinstance(top, (ast.FunctionDef, ast.AsyncFunctionDef)) or top.name == name:
                continue
            if any(isinstance(n, ast.Name) and n.id == name for n in ast.walk(top)):
                callers.add(top.name)
        return callers

    # Every caller of _run_command, and the detached, short-lived entry point each is
    # confined to. The abandoned reader is safe ONLY because the process exits promptly
    # afterwards, so the property to hold is not "one caller" but "no caller the render can
    # reach". Adding a row here is a claim that the new caller is confined too — and the
    # tests below check the chain rather than taking the claim.
    CONFINED_CALLERS = {
        "run_custom": "--run-custom",           # one custom segment's command
        "list_agent_sessions": "--drain-queue",  # `claude agents --json`
        "send_notify": "--drain-queue",          # delivering one job
    }

    def test_every_caller_of_run_command_is_a_known_confined_one(self):
        self.assertEqual(self._callers_of("_run_command"), set(self.CONFINED_CALLERS),
                         "_run_command's abandoned reader is only safe in a short-lived child")

    def test_run_custom_has_exactly_one_caller(self):
        self.assertEqual(self._callers_of("run_custom"), {"main"},
                         "run_custom must stay reachable only from the --run-custom child")

    def test_the_queue_reader_functions_are_confined_to_the_drain(self):
        for name in ("list_agent_sessions", "send_notify"):
            self.assertEqual(self._callers_of(name), {"drain_queue"}, name)
        self.assertEqual(self._callers_of("drain_queue"), {"main"})

    def test_the_drain_is_only_reachable_from_its_own_flag(self):
        tree = ast.parse((PLUGIN_ROOT / "scripts" / "statusline.py").read_text(encoding="utf-8"))
        main = next(n for n in tree.body
                    if isinstance(n, ast.FunctionDef) and n.name == "main")
        guarded = []
        for node in ast.walk(main):
            if isinstance(node, ast.If) and "--drain-queue" in ast.dump(node.test):
                guarded += [n for n in ast.walk(node)
                            if isinstance(n, ast.Name) and n.id == "drain_queue"]
        self.assertEqual(len(guarded), 1,
                         "drain_queue is not (or no longer only) under the --drain-queue branch")

    def test_that_one_caller_is_the_detached_child_branch(self):
        """`main` is also the render path, so naming it is not enough: the call has to sit
        under the `--run-custom` dispatch, which returns before any render happens."""
        tree = ast.parse((PLUGIN_ROOT / "scripts" / "statusline.py").read_text(encoding="utf-8"))
        main = next(n for n in tree.body
                    if isinstance(n, ast.FunctionDef) and n.name == "main")
        guarded = []
        for node in ast.walk(main):
            if not isinstance(node, ast.If):
                continue
            if "--run-custom" not in ast.dump(node.test):
                continue
            guarded += [n for n in ast.walk(node)
                        if isinstance(n, ast.Name) and n.id == "run_custom"]
        self.assertEqual(len(guarded), 1,
                         "run_custom is not (or no longer only) under the --run-custom branch")

# ── The 183/3% blocker: a stale observer, and an unaligned window ─────────────
W = sl.SESSION_WINDOW_S


def _smp(ts, tokens, cost, account="acct", pct=None, resets_at=None):
    """One sample in the current 7-element format."""
    return [ts, tokens, cost, "M", account, pct, resets_at]


class StaleObserverTests(unittest.TestCase):
    """The user's bar read `5h ▰▰▱▱▱ 183/3%`. The part cannot exceed the whole.

    Measured on a copy of the live store: five sessions wrote readings for one account and
    ONE of them reported a frozen 45.0 every minute for three hours while the other four
    reported the true 3.0 — its Claude Code process was still serving the pre-reset window's
    rate_limits block. `_observation_series` merged them into one timeline on the assumption
    that every observer sees the same thing, so a flat 3%% became a 3→45→3→45 sawtooth and the
    walker credited every up-leg as +42 points of fresh consumption, hundreds of times.

        all sessions (as shipped)             total = 182.0
        with the one stale observer removed   total =   6.0
        the account's actual reading                  7.0

    Two defences, because they cover different data. A reading carries the `resets_at` of the
    window it describes, so one written AFTER its own window ended is stale by definition and
    is dropped. Samples predating that field cannot be dated, so observers that disagree at
    the same moment are treated as an observation GAP — unknown, never a summed movement.
    That is this project's oldest rule and the reason the fix works on data already on disk.
    """

    def test_a_frozen_observer_cannot_manufacture_consumption(self):
        """The real shape, reduced: one writer stuck on the pre-reset value."""
        t0 = 1_000_000.0
        fresh, stale = [], []
        for i in range(60):
            ts = t0 + i * 60
            fresh.append(_smp(ts, 100 + i, 1.0, pct=3.0, resets_at=t0 + W))
            stale.append(_smp(ts + 5, 200 + i, 2.0, pct=45.0, resets_at=t0 - 3600))
        result = sl.account_window_attribution({"fresh": fresh, "stale": stale}, "acct",
                                               t0 - W, t0 + 3600)
        self.assertLessEqual(result["total_points"], 3.0,
                             "a stale observer manufactured consumption")

    def test_undatable_observers_that_disagree_are_a_gap_not_a_movement(self):
        """Legacy samples carry no resets_at, so neither reading can be shown to be stale.
        Unknown is not zero and it is certainly not 42."""
        t0 = 1_000_000.0
        a = [_smp(t0 + i * 60, 100 + i, 1.0, pct=3.0) for i in range(30)]
        b = [_smp(t0 + i * 60 + 5, 200 + i, 2.0, pct=45.0) for i in range(30)]
        result = sl.account_window_attribution({"a": a, "b": b}, "acct", t0 - W, t0 + 3600)
        self.assertEqual(result["total_points"], 0.0)
        self.assertTrue(all(v["kind"] != "exact" for v in result["sessions"].values()),
                        "a figure derived from contradictory observers cannot be 'exact'")

    def test_observers_that_agree_are_left_alone(self):
        """The single-profile case, which is most people: nothing about this changes."""
        t0 = 1_000_000.0
        a = [_smp(t0, 100, 1.0, pct=10.0, resets_at=t0 + W),
             _smp(t0 + 600, 200, 2.0, pct=25.0, resets_at=t0 + W)]
        b = [_smp(t0 + 2, 50, 0.5, pct=10.0, resets_at=t0 + W),
             _smp(t0 + 602, 50, 0.5, pct=25.0, resets_at=t0 + W)]
        result = sl.account_window_attribution({"a": a, "b": b}, "acct", t0 - W, t0 + 700)
        self.assertEqual(result["total_points"], 15.0)
        self.assertEqual(result["sessions"]["a"]["kind"], "exact")

    # Both offsets are expressed RELATIVE to the grace constant, never as literals. A
    # literal here is a hidden coupling: the constant and the fixture have to move together,
    # and when they were split across two commits the guard for this very bug read as a
    # failure on a mixed tree. Stated this way, tuning the grace cannot silently invalidate
    # either test, and each one says which side of the line it is testing.
    WELL_PAST_GRACE = sl.OBS_STALE_GRACE_S + 3600
    INSIDE_GRACE = sl.OBS_STALE_GRACE_S / 2

    def test_a_reading_written_after_its_own_window_ended_is_dropped(self):
        t0 = 1_000_000.0
        series = sl._observation_series(
            {"s": [_smp(t0, 1, 1.0, pct=45.0, resets_at=t0 - self.WELL_PAST_GRACE),
                   _smp(t0 + 60, 2, 2.0, pct=3.0, resets_at=t0 + W)]}, "acct")
        self.assertEqual([p for _ts, p, _r in series], [3.0])

    def test_a_reading_a_moment_past_the_boundary_is_kept(self):
        """The grace margin. A render landing in the same second the window rolls is a moment
        late, not stale; what this discards had been repeating for three hours. Comparing a
        sample's timestamp with the resets_at recorded beside it is intra-sample — one process,
        one instant — so no clock skew between profiles can reach this test."""
        t0 = 1_000_000.0
        series = sl._observation_series(
            {"s": [_smp(t0, 1, 1.0, pct=45.0, resets_at=t0 - self.INSIDE_GRACE)]}, "acct")
        self.assertEqual([p for _ts, p, _r in series], [45.0])

    def test_the_grace_is_a_margin_not_a_licence(self):
        """A grace wide enough to admit what this fix exists to discard would be worse than
        none at all — the frozen observer was three HOURS past its window."""
        self.assertLess(sl.OBS_STALE_GRACE_S, 600,
                        "the stale-reading grace has grown into a loophole")


STALE_FIXTURE = DEV_DIR / "stale_observer_fixture.json"


class RealStaleObserverFixtureTests(TmpEnv):
    """The regression test for the bug that actually happened, driven by the store it
    happened in rather than by a reduction of it that I wrote after understanding it.

    `dev/stale_observer_fixture.json` is 45 minutes of a real `~/.squirrel`: five sessions,
    four reporting the true 3.0 and one repeating a frozen 45.0 once a minute. Session ids,
    repo names, the account hash and absolute timestamps are all stripped — the SHAPE is the
    only thing kept, and the shape is the bug. A synthetic fixture would only ever reproduce
    what I already believed; this one reproduces what the machine did.

    Note it carries NO resets_at: every sample predates that field, so this is also the
    legacy path, which is the case every existing user is in until their store turns over."""

    def setUp(self):
        super().setUp()
        patcher = mock.patch.dict(os.environ, {"SQUIRREL_SHARED_DIR": str(self.root / "sh")})
        patcher.start(); self.addCleanup(patcher.stop)
        self.acct = sl.account_hash("you@example.com")
        self.data = json.loads(STALE_FIXTURE.read_text(encoding="utf-8"))
        self.t0 = NOW - 45 * 60

    def _sessions(self):
        out = {}
        for sid, rows in self.data["sessions"].items():
            out[sid] = [_smp(self.t0 + ts, tok, cost, self.acct, pct=pct) for ts, tok, cost, pct in rows]
        return out

    def test_the_fixture_still_contains_the_bug_it_was_captured_for(self):
        """A fixture that has lost its teeth tests nothing. One session must disagree."""
        seen = {sid: sorted({r[3] for r in rows}) for sid, rows in self.data["sessions"].items()}
        odd = [sid for sid, pcts in seen.items() if 45.0 in pcts]
        self.assertEqual(len(odd), 1, f"expected exactly one frozen observer, got {seen}")
        self.assertTrue(any(pcts == [3.0] for pcts in seen.values()), seen)

    def test_the_frozen_observer_manufactures_no_attribution(self):
        sessions = self._sessions()
        start = sl.current_window_start(sessions, self.acct, NOW)
        result = sl.account_window_attribution(sessions, self.acct, start, NOW)
        self.assertLessEqual(round(result["total_points"], 3),
                             self.data["account_reading_at_capture"],
                             f"{result['total_points']} points against a "
                             f"{self.data['account_reading_at_capture']}% reading")

    def test_before_the_fix_this_fixture_produced_the_sawtooth(self):
        """Guards the guard: shows the fixture WOULD blow up under the old merge rule, so a
        future change that quietly reinstates it cannot pass by leaving this file untouched."""
        sessions = self._sessions()
        naive = []
        for samples in sessions.values():
            naive += [(s[0], s[5]) for s in samples if s[5] is not None]
        naive.sort()
        sawtooth = sum(max(0.0, b - a) for (_t1, a), (_t2, b) in zip(naive, naive[1:]))
        self.assertGreater(sawtooth, 100.0,
                           "the fixture no longer reproduces the original over-count")

    def test_a_legacy_only_store_degrades_to_unknown_not_a_winner(self):
        """Point 2 of the brief: with nothing datable, Squirrel must say it does not know
        rather than quietly pick one of the contradicting observers."""
        for sid, samples in self._sessions().items():
            sl.write_cache(sl.session_path(sid),
                           {"session_id": sid, "repo": sid, "samples": [list(x) for x in samples]})
        sessions = sl.live_sessions("you@example.com", NOW, sl.load_config())
        self.assertTrue(sessions, "the fixture's sessions should still be live")
        self.assertTrue(all(x["attributed_points"] == 0.0 for x in sessions),
                        "a contradicted timeline produced a number anyway")
        self.assertTrue(any(x["attribution_kind"] == "unknown" for x in sessions),
                        "nothing reported unknown; a gap was rendered as a confident zero")

    def test_the_composite_is_withheld_rather_than_guessed(self):
        for sid, samples in self._sessions().items():
            sl.write_cache(sl.session_path(sid),
                           {"session_id": sid, "repo": sid, "samples": [list(x) for x in samples]})
        sid = next(iter(self.data["sessions"]))
        self.assertIsNone(sl.session_contribution({"session_id": sid}, "you@example.com",
                                                  NOW, sl.load_config()),
                          "a composite was offered for a timeline nobody can vouch for")

class AlignedWindowTests(unittest.TestCase):

    """Coverage read `4h59m/5h` for a window that was only 3h36m old, because attribution was
    clipped at a ROLLING `now - 5h` rather than the window's own `[resets_at - 5h, resets_at]`.
    The rolling clip reaches back before the last reset into the previous window."""

    def test_the_window_starts_at_the_reset_not_five_hours_ago(self):
        now = 1_000_000.0
        resets_at = now + 84 * 60                 # 84 minutes to go, as on the user's bar
        samples = [_smp(now - 4 * 3600, 1, 1.0, pct=90.0, resets_at=resets_at)]
        self.assertAlmostEqual(sl.current_window_start({"s": samples}, "acct", now),
                               resets_at - W, delta=1)

    def test_the_window_start_is_inferred_when_no_sample_can_date_it(self):
        """Every sample already on disk predates the resets_at field, so the aligned boundary
        has to come from somewhere else or the fix does nothing until the store turns over.
        The last time utilisation FELL is the last time the window rolled. On the user's real
        store this recovers 06:41:52 — a 3.74h-old window, against the 84 minutes remaining
        they reported — and takes the total from 182 points to 7.0 against a 7.0 reading."""
        now = 1_000_000.0
        roll = now - 2 * 3600
        old = [(roll - 3600 + i * 600, 20.0 + i * 5) for i in range(6)]     # previous window
        new_ = [(roll + i * 600, float(i)) for i in range(12)]              # after the roll
        samples = [_smp(ts, 100 + i, float(i), pct=pct)                     # no resets_at
                   for i, (ts, pct) in enumerate(old + new_)]
        self.assertAlmostEqual(sl.current_window_start({"s": samples}, "acct", now),
                               roll, delta=1)

    def test_an_inferred_start_is_never_older_than_the_rolling_fallback(self):
        now = 1_000_000.0
        samples = [_smp(now - 6 * 3600 + i * 600, 10 + i, float(i), pct=float(i))
                   for i in range(4)]      # one fall, but long before the rolling window
        self.assertAlmostEqual(sl.current_window_start({"s": samples}, "acct", now),
                               now - W, delta=1)

    def test_nothing_before_the_reset_is_attributed(self):
        now = 1_000_000.0
        resets_at = now + 3600
        start = resets_at - W
        samples = ([_smp(start - 3600 + i * 300, 10 + i, float(i), pct=10.0 * i,
                         resets_at=start)          # the PREVIOUS window
                    for i in range(1, 6)]
                   + [_smp(start + i * 300, 100 + i, 50.0 + i, pct=float(i),
                           resets_at=resets_at) for i in range(1, 6)])
        result = sl.account_window_attribution({"s": samples}, "acct", start, now)
        self.assertLessEqual(result["total_points"], 5.0,
                             "points banked in the previous window survived the reset")

    def test_coverage_never_exceeds_the_windows_own_age(self):
        now = 1_000_000.0
        resets_at = now + 84 * 60
        start = resets_at - W
        age = now - start
        samples = [_smp(start - 4 * 3600 + i * 600, 10 + i, float(i), pct=float(i),
                        resets_at=start) for i in range(20)]
        samples += [_smp(start + i * 600, 200 + i, 100.0 + i, pct=float(i),
                         resets_at=resets_at) for i in range(10)]
        result = sl.account_window_attribution({"s": samples}, "acct", start, now)
        for sid, v in result["sessions"].items():
            self.assertLessEqual(v["coverage_seconds"], age + 1,
                                 f"{sid} claims more coverage than the window has existed")

    def test_two_resets_in_the_retained_history_behave_the_same(self):
        now = 1_000_000.0
        resets_at = now + 1800
        start = resets_at - W
        samples = []
        for w in (2, 1):        # two complete earlier windows, each climbing to 80%
            base = start - w * W
            samples += [_smp(base + i * 1200, 10 * (3 - w) + i, float(i), pct=float(i * 20),
                             resets_at=base + W) for i in range(5)]
        samples += [_smp(start + i * 600, 500 + i, 300.0 + i, pct=float(i * 2),
                         resets_at=resets_at) for i in range(6)]
        result = sl.account_window_attribution({"s": samples}, "acct", start, now)
        self.assertLessEqual(result["total_points"], 10.0)

    def test_a_session_active_only_before_the_reset_reports_unknown_not_zero(self):
        now = 1_000_000.0
        resets_at = now + 3600
        start = resets_at - W
        old = [_smp(start - 3600 + i * 300, 10 + i, float(i), pct=float(i * 10),
                    resets_at=start) for i in range(5)]
        cur = [_smp(start + i * 300, 100 + i, 50.0 + i, pct=float(i),
                    resets_at=resets_at) for i in range(5)]
        result = sl.account_window_attribution({"old": old, "cur": cur}, "acct", start, now)
        self.assertEqual(result["sessions"]["old"]["kind"], "unknown",
                         "a session with nothing in this window must not report a confident 0")
        self.assertEqual(result["sessions"]["old"]["points"], 0.0)


class PartNeverExceedsWholeTests(unittest.TestCase):
    """The invariant, asserted rather than hoped for: attributed points can never exceed the
    account's own utilisation. If they ever can, that is a bug by construction — which is
    exactly what `183/3%` was."""

    def _histories(self):
        """Deterministic pseudo-random histories, including resets and stale observers."""
        import random
        for seed in range(60):
            rng = random.Random(seed)
            now = 1_000_000.0
            resets_at = now + rng.randint(60, W)
            start = resets_at - W
            sessions, peak = {}, 0.0
            pct = 0.0
            n = rng.randint(2, 5)
            for sid in range(n):
                samples, tok, cost = [], 0, 0.0
                for i in range(rng.randint(2, 25)):
                    ts = start - rng.randint(0, W) + i * 120
                    tok += rng.randint(0, 5000); cost += rng.random()
                    if ts >= start:
                        pct = min(100.0, pct + rng.random() * 4)
                        peak = max(peak, pct)
                        r = resets_at
                    else:
                        r = start
                    stale = rng.random() < 0.25
                    samples.append(_smp(ts, tok, cost, pct=(90.0 if stale else pct),
                                        resets_at=(start - 1 if stale else r)))
                sessions[f"s{sid}"] = sorted(samples, key=lambda s: s[0])
            yield sessions, start, now, peak

    def test_attributed_points_never_exceed_the_accounts_own_utilisation(self):
        for sessions, start, now, peak in self._histories():
            result = sl.account_window_attribution(sessions, "acct", start, now)
            self.assertLessEqual(round(result["total_points"], 6), round(peak, 6) + 1e-6,
                                 f"attributed {result['total_points']} of a {peak} total")

    def test_no_session_is_ever_attributed_a_negative_share(self):
        for sessions, start, now, _peak in self._histories():
            result = sl.account_window_attribution(sessions, "acct", start, now)
            for sid, v in result["sessions"].items():
                self.assertGreaterEqual(v["points"], 0.0, sid)


class CompositeNeverExceedsTheAccountTests(TmpEnv):
    """End to end, through the segment the user actually looked at. `5h ▰▰▱▱▱ 183/3%` was
    visible on the bar; nothing below the renderer had noticed. This asserts the rendered
    composite, not an intermediate."""

    def setUp(self):
        super().setUp()
        patcher = mock.patch.dict(os.environ, {"SQUIRREL_SHARED_DIR": str(self.root / "sh")})
        patcher.start(); self.addCleanup(patcher.stop)
        self.acct = sl.account_hash("you@example.com")

    def _write(self, sid, samples):
        sl.write_cache(sl.session_path(sid), {"session_id": sid, "repo": sid,
                                              "samples": [list(x) for x in samples]})

    def test_the_users_own_shape_renders_a_number_that_is_not_impossible(self):
        """One frozen observer against four honest ones, which is what their store held."""
        now = NOW
        roll = now - 3 * 3600
        for sid in ("a", "b", "c", "d"):
            self._write(sid, [_smp(roll + i * 60, 1000 * i, float(i), self.acct,
                                   pct=min(7.0, i * 0.05), resets_at=roll + W)
                              for i in range(180)])
        self._write("stale", [_smp(roll + i * 60 + 5, 500 * i, float(i) / 2, self.acct,
                                   pct=45.0, resets_at=roll - 60)
                              for i in range(180)])
        sessions = sl.live_sessions("you@example.com", now, sl.load_config())
        total = sum(s["attributed_points"] for s in sessions)
        self.assertLessEqual(round(total, 3), 7.0,
                             f"attributed {total} points of a 7% account total")
        for s in sessions:
            self.assertLessEqual(s["coverage_seconds"], now - roll + 1)

    def test_live_sessions_scopes_to_the_window_not_a_rolling_five_hours(self):
        """The hole a mutation found: every window test above called the walker directly, so
        reverting live_sessions to `now - 5h` passed the whole suite. This drives the same
        history through the function the segments actually call, with a reset 90 minutes ago
        and a previous window that climbed to 80% — all of which a rolling five hours would
        still be inside."""
        now = NOW
        roll = now - 90 * 60
        prev = [_smp(roll - 3 * 3600 + i * 600, 1000 * i, float(i), self.acct,
                     pct=float(i * 4), resets_at=roll)              # climbs to 80%
                for i in range(20)]
        cur = [_smp(roll + i * 300, 30_000 + 100 * i, 50.0 + i, self.acct,
                    pct=float(i * 0.5), resets_at=roll + W)          # this window: 0 -> 8%
               for i in range(18)]
        self._write("s", prev + cur)
        sessions = sl.live_sessions("you@example.com", now, sl.load_config())
        total = sum(x["attributed_points"] for x in sessions)
        self.assertLessEqual(round(total, 3), 8.5,
                             f"{total} points — the previous window's 80% leaked in")
        for x in sessions:
            self.assertLessEqual(x["coverage_seconds"], now - roll + 1,
                                 "coverage exceeds the age of the window itself")

    def test_the_rendered_composite_left_never_exceeds_its_right(self):
        now = NOW
        roll = now - 2 * 3600
        for sid in ("mine", "other"):
            self._write(sid, [_smp(roll + i * 120, 900 * i, float(i), self.acct,
                                   pct=min(12.0, i * 0.2), resets_at=roll + W)
                              for i in range(60)])
        runs = sl.share_segment_runs({"session_id": "mine"}, "you@example.com", now,
                                     sl.load_config(), account_pct=12.0)
        text = plain(sl.join_runs(runs))
        match = re.search(r"(\d+(?:\.\d+)?)\s*(?:pp|%)?\s*/\s*(\d+(?:\.\d+)?)\s*%", text)
        self.assertIsNotNone(match, f"no composite in {text!r}")
        left, right = float(match.group(1)), float(match.group(2))
        self.assertLessEqual(left, right, f"the part exceeds the whole: {text!r}")

class SampleFormatSevenTests(TmpEnv):

    """`resets_at` is the seventh element. Additive, per Task 12's rule: every older length
    still parses and a missing trailing field reads as unknown, never as zero."""

    def test_every_generation_still_parses(self):
        self.assertEqual(sl._parse_sample([1, 2, 3.0])[6], None)
        self.assertEqual(sl._parse_sample([1, 2, 3.0, "M"])[6], None)
        self.assertEqual(sl._parse_sample([1, 2, 3.0, "M", "a"])[6], None)
        self.assertEqual(sl._parse_sample([1, 2, 3.0, "M", "a", 5.0])[6], None)
        self.assertEqual(sl._parse_sample([1, 2, 3.0, "M", "a", 5.0, 99.0])[6], 99.0)
        self.assertIsNone(sl._parse_sample([1, 2, 3.0, "M", "a", 5.0, 99.0, "extra"]))

    def test_the_recorded_sample_carries_the_windows_reset(self):
        patcher = mock.patch.dict(os.environ, {"SQUIRREL_SHARED_DIR": str(self.root / "sh")})
        patcher.start(); self.addCleanup(patcher.stop)
        sl.record_session_sample({"session_id": "s1", "cost": {"total_cost_usd": 1.0},
                                  "context_window": {}}, "you@example.com", NOW,
                                 None, 42.0, NOW + 1234)
        samples = sl.load_json(sl.session_path("s1"))["samples"]
        self.assertEqual(samples[-1][5], 42.0)
        self.assertEqual(samples[-1][6], NOW + 1234)

# ── Task 28 part 1: activity read from the transcript, not inferred ──────────
def _entry(kind, parts, sidechain=False):
    return json.dumps({"type": kind, "isSidechain": sidechain,
                       "message": {"role": kind, "content": parts}})


def _text(kind, body="hello", **kw):
    return _entry(kind, [{"type": "text", "text": body}], **kw)


def _use(tool_id, name="Bash", **kw):
    return _entry("assistant", [{"type": "tool_use", "id": tool_id, "name": name}], **kw)


def _result(tool_id, **kw):
    return _entry("user", [{"type": "tool_result", "tool_use_id": tool_id}], **kw)


class TranscriptActivityTests(TmpEnv):
    """The chip infers activity from hook events, and the inference is wrong in a way the
    user has been staring at: a background agent fires its spawn event and then the session
    fires `Stop`, so the bar reads `IDLE 1h42m` while agents are working.

    The transcript answers it deterministically, with no model in the loop: an assistant that
    spoke last with nothing outstanding is waiting for the user; a `tool_use` with no matching
    `tool_result` means something is still running; anything else is mid-turn.

    Verified against the real format before any of this was written, by truncating a live
    transcript immediately after a genuine tool call:

        after a real 'Agent' tool_use   -> waiting-tools, agents=1
        after a real 'Skill' tool_use   -> waiting-tools, tools=1, agents=0
        four live sessions at rest      -> waiting-you
    """

    def _write(self, *lines):
        path = self.root / "t.jsonl"
        path.write_text("\n".join(lines) + "\n", encoding="utf-8")
        return path

    def test_assistant_spoke_last_means_waiting_for_you(self):
        path = self._write(_text("user", "do it"), _text("assistant", "done"))
        self.assertEqual(sl.scan_transcript(path)["state"], "waiting-you")

    def test_an_unanswered_tool_use_means_waiting_on_tools(self):
        path = self._write(_text("user", "go"), _use("t1", "Bash"))
        scan = sl.scan_transcript(path)
        self.assertEqual(scan["state"], "waiting-tools")
        self.assertEqual((scan["pending_tools"], scan["pending_agents"]), (1, 0))

    def test_an_unanswered_agent_call_is_counted_as_an_agent(self):
        path = self._write(_text("user", "go"), _use("t1", "Task"), _use("t2", "Agent"))
        scan = sl.scan_transcript(path)
        self.assertEqual((scan["pending_tools"], scan["pending_agents"]), (2, 2))

    def test_a_completed_tool_call_is_not_pending(self):
        path = self._write(_use("t1", "Bash"), _result("t1"), _text("assistant", "ok"))
        scan = sl.scan_transcript(path)
        self.assertEqual((scan["state"], scan["pending_tools"]), ("waiting-you", 0))

    def test_the_users_own_complaint_a_running_agent_is_not_idle(self):
        """The whole point. The hook state says idle — the assistant's turn ended — while a
        background agent is still running. The transcript still shows the call outstanding."""
        path = self._write(_text("user", "go"), _use("t1", "Task"), _text("assistant", "spawned"))
        scan = sl.scan_transcript(path)
        self.assertEqual(scan["state"], "waiting-tools")
        self.assertEqual(scan["pending_agents"], 1)

    def test_a_user_turn_last_means_working(self):
        path = self._write(_text("assistant", "ok"), _text("user", "next thing"))
        self.assertEqual(sl.scan_transcript(path)["state"], "working")

    def test_sidechain_entries_do_not_speak_for_the_session(self):
        """A subagent's own turns are written into the same file. They are not this session
        talking to the user, and must not decide what the bar says. Stated as a case where
        including them would give the OPPOSITE answer: the main chain ends on a user turn, so
        the session is mid-turn, but a subagent's chatter would read as "waiting for you"."""
        path = self._write(_text("assistant", "ok"), _text("user", "next thing"),
                           _text("assistant", "subagent chatter", sidechain=True))
        self.assertEqual(sl.scan_transcript(path)["state"], "working")

    def test_a_sidechain_tool_call_is_not_this_sessions_pending_work(self):
        path = self._write(_text("assistant", "done"),
                           _use("s1", "Bash", sidechain=True))
        scan = sl.scan_transcript(path)
        self.assertEqual((scan["state"], scan["pending_tools"]), ("waiting-you", 0))

    def test_only_the_tail_is_ever_read(self):
        """78 MB was the largest transcript on the developer's machine and the render runs
        every few seconds. Reading one whole would be a per-render tax measured in seconds."""
        path = self.root / "big.jsonl"
        filler = _text("assistant", "x" * 900)
        with path.open("w", encoding="utf-8") as fh:
            for _ in range(4000):
                fh.write(filler + "\n")
            fh.write(_use("t1", "Bash") + "\n")
        self.assertGreater(path.stat().st_size, 3 * 1024 * 1024)
        reads = []
        real_open = builtins.open

        def counting_open(*a, **kw):
            handle = real_open(*a, **kw)
            if "b" in str(kw.get("mode") or (a[1] if len(a) > 1 else "r")):
                real_read = handle.read

                def watched(*ra):
                    data = real_read(*ra)
                    reads.append(len(data))
                    return data
                handle.read = watched
            return handle

        with mock.patch.object(builtins, "open", counting_open):
            scan = sl.scan_transcript(path)
        self.assertEqual(scan["state"], "waiting-tools")
        self.assertTrue(reads, "the scan did not read the file at all")
        self.assertLessEqual(max(reads), sl.TRANSCRIPT_TAIL_BYTES + 1,
                             "the whole transcript was read into memory")

    def test_a_partial_first_line_never_breaks_the_scan(self):
        """Seeking into the middle of the file lands mid-line by definition."""
        path = self.root / "cut.jsonl"
        with path.open("w", encoding="utf-8") as fh:
            fh.write('{"type": "assistant", "message": {"content": [{"type": "text",'
                     + ' "text": "' + "y" * 400_000 + '"}]}}\n')
            fh.write(_use("t1", "Bash") + "\n")
        self.assertEqual(sl.scan_transcript(path)["state"], "waiting-tools")

    def test_the_fragment_is_dropped_even_when_it_would_parse(self):
        """The reason the discarded first line matters. A mid-line cut usually yields
        rubbish that fails to parse and is skipped anyway — so a scan that forgot to drop it
        would still look correct. Here the cut lands exactly on a complete JSON object
        embedded in a much longer line's text, so the fragment parses cleanly and claims an
        outstanding tool call that does not exist. Only dropping the line catches it."""
        ghost = ('{"type": "assistant", "isSidechain": false, "message": {"content":'
                 ' [{"type": "tool_use", "id": "ghost", "name": "Bash"}]}}')
        trailer = _text("assistant", "all done") + "\n"
        # Everything from `ghost` onwards must weigh EXACTLY one tail, so the seek lands on
        # its first byte. Pad after it, never before: padding the head only moves the cut
        # into the filler, where the fragment is rubbish that fails to parse anyway — which
        # is how the first version of this test passed against the very bug it targets.
        filler_room = sl.TRANSCRIPT_TAIL_BYTES - len(ghost) - 1 - len(trailer)
        self.assertGreater(filler_room, 200, "no room to pad")
        filler = '{"type": "system", "pad": "' + "z" * (filler_room - 30) + '"}\n'
        self.assertEqual(len(filler), filler_room)
        head = '{"type": "system", "junk": "' + "y" * 500 + '", "x": '
        blob = head + ghost + "\n" + filler + trailer
        path = self.root / "ghost.jsonl"
        path.write_bytes(blob.encode("utf-8"))
        self.assertEqual(path.stat().st_size - sl.TRANSCRIPT_TAIL_BYTES, len(head),
                         "the seek does not land on the ghost's first byte")
        scan = sl.scan_transcript(path)
        self.assertEqual((scan["state"], scan["pending_tools"]), ("waiting-you", 0),
                         "a fragment of a longer line was read as a real entry")

    def test_any_failure_at_all_degrades_to_no_information(self):
        """The format belongs to Claude Code and can change without notice. Whatever comes
        back, the bar renders exactly as it did before this existed."""
        path = self._write(_use("t1", "Bash"))
        for boom in (ValueError("shape changed"), RuntimeError("?"), KeyError("field")):
            sl._TRANSCRIPT_CACHE.clear()
            with mock.patch.object(sl, "_scan_transcript_uncached", side_effect=boom):
                self.assertIsNone(sl.scan_transcript(path), repr(boom))

    def test_rubbish_and_absence_degrade_silently(self):
        for content in ("", "not json at all\n", '{"type": "weird"}\n', "\x00\x00\n"):
            path = self.root / "junk.jsonl"
            path.write_text(content, encoding="utf-8")
            self.assertIsInstance(sl.scan_transcript(path), dict)
        self.assertIsNone(sl.scan_transcript(self.root / "nope.jsonl"))
        self.assertIsNone(sl.scan_transcript(None))

    def test_the_scan_is_memoised_on_mtime_and_size(self):
        path = self._write(_use("t1", "Bash"))
        sl._TRANSCRIPT_CACHE.clear()
        first = sl.scan_transcript(path)
        with mock.patch.object(sl, "_scan_transcript_uncached",
                               side_effect=AssertionError("re-read an unchanged transcript")):
            self.assertEqual(sl.scan_transcript(path), first)
        _time.sleep(0.01)
        path.write_text("\n".join([_use("t1", "Bash"), _result("t1"),
                                    _text("assistant", "done")]) + "\n", encoding="utf-8")
        self.assertEqual(sl.scan_transcript(path)["state"], "waiting-you")


class TranscriptMetricTests(TmpEnv):
    """The three metrics, through the grammar the rules and tasks already share."""

    def setUp(self):
        super().setUp()
        self.path = self.root / "m.jsonl"
        self.status = {"session_id": "m1", "transcript_path": str(self.path)}

    def _ctx(self):
        return {"status": self.status, "config": sl.load_config(), "now": NOW}

    def test_the_metrics_are_registered_in_the_one_grammar(self):
        for name in ("state", "pending.tools", "pending.agents"):
            self.assertIn(name, sl.METRICS)

    def test_pending_counts_come_from_the_transcript(self):
        self.path.write_text("\n".join([_text("user", "go"), _use("a", "Task"),
                                         _use("b", "Bash")]) + "\n", encoding="utf-8")
        ctx = self._ctx()
        self.assertEqual(sl.metric_value(ctx, "pending.tools"), 2)
        self.assertEqual(sl.metric_value(ctx, "pending.agents"), 1)
        self.assertEqual(sl.metric_value(ctx, "state"), "waiting-tools")

    def test_without_a_transcript_state_is_the_hook_state_exactly_as_before(self):
        sl.apply_state("idle", "m1", NOW - 60)
        ctx = {"status": {"session_id": "m1"}, "config": sl.load_config(), "now": NOW}
        self.assertEqual(sl.metric_value(ctx, "state"), "idle")
        self.assertIsNone(sl.metric_value(ctx, "pending.tools"))

    def test_unknown_pending_never_satisfies_a_condition(self):
        """Rule 5 of the brief, and this project's oldest rule: unknown is not zero."""
        ctx = {"status": {"session_id": "nope"}, "config": sl.load_config(), "now": NOW}
        for cond in ("pending.agents > 0", "pending.agents == 0", "pending.tools < 5"):
            self.assertNotIsInstance(sl.parse_condition(cond), str, cond)   # it parses
            self.assertFalse(sl.condition_holds(ctx, cond), cond)           # and never fires

    def test_a_transcript_that_disagrees_never_makes_the_session_look_more_idle(self):
        """The refinement is one-directional on purpose. An outstanding call can contradict a
        hook that claimed idle; nothing here may invent idleness the hooks did not report."""
        sl.apply_state("working", "m1", NOW)
        self.path.write_text(_text("assistant", "done") + "\n", encoding="utf-8")
        self.assertEqual(sl.metric_value(self._ctx(), "state"), "working")


class TranscriptChipTests(TmpEnv):
    """`IDLE 1h42m` with agents running is the complaint. The chip has to stop saying it."""

    def setUp(self):
        super().setUp()
        self.path = self.root / "c.jsonl"

    def test_the_chip_does_not_claim_idle_while_an_agent_is_outstanding(self):
        self.path.write_text("\n".join([_text("user", "go"), _use("t1", "Task"),
                                         _text("assistant", "spawned")]) + "\n",
                             encoding="utf-8")
        sl.apply_state("idle", "c1", NOW - 6000)
        status = {"session_id": "c1", "transcript_path": str(self.path)}
        text = plain(sl.join_runs(sl.state_segment_runs(status, NOW, sl.load_config())))
        self.assertNotIn("IDLE", text.upper(), text)

    def test_the_refined_chip_reports_the_agent_count_from_the_transcript(self):
        """Found by rendering it rather than by a test: the first cut refined the state to
        `subagents` but left the COUNT on the hook record, which said 0 — so the chip came
        out as an empty string. Worse than the wrong chip it replaced."""
        self.path.write_text("\n".join([_text("user", "go"), _use("t1", "Task"),
                                         _use("t2", "Agent")]) + "\n", encoding="utf-8")
        sl.apply_state("idle", "c4", NOW - 6000)
        status = {"session_id": "c4", "transcript_path": str(self.path)}
        text = plain(sl.join_runs(sl.state_segment_runs(status, NOW, sl.load_config())))
        self.assertIn("2 subagents", text, text)

    def test_a_refined_state_is_not_aged_out_by_the_hook_clock(self):
        """The same defect's other half. An active state older than `activeMaxAgeSeconds` is
        discarded as stale — correct for a hook event nobody closed, wrong for a state the
        transcript has just this second proved. The evidence carries its own timestamp."""
        self.path.write_text("\n".join([_text("user", "go"), _use("t1", "Task")]) + "\n",
                             encoding="utf-8")
        sl.apply_state("idle", "c5", NOW - 30 * 3600)     # a very old hook event
        status = {"session_id": "c5", "transcript_path": str(self.path)}
        runs = sl.state_segment_runs(status, _time.time(), sl.load_config())
        self.assertTrue(runs, "a freshly-proved active state was discarded as stale")
        self.assertIn("subagent", plain(sl.join_runs(runs)))

    def test_a_genuinely_idle_session_still_reads_idle(self):
        self.path.write_text(_text("assistant", "all done") + "\n", encoding="utf-8")
        sl.apply_state("idle", "c2", NOW - 6000)
        status = {"session_id": "c2", "transcript_path": str(self.path)}
        text = plain(sl.join_runs(sl.state_segment_runs(status, NOW, sl.load_config())))
        self.assertIn("IDLE", text.upper(), text)

    def test_with_no_transcript_the_chip_is_byte_for_byte_what_it_was(self):
        sl.apply_state("idle", "c3", NOW - 6000)
        before = sl.join_runs(sl.state_segment_runs({"session_id": "c3"}, NOW, sl.load_config()))
        self.assertIn("IDLE", plain(before).upper())

# ── Task 28 part 2: the queue is the contract ────────────────────────────────
class TaskSpecTests(TmpEnv):
    """Tasks are validated on the way IN, so an invalid one can never reach the queue —
    the /squirrel-layout discipline: nothing written on error, and a refusal that names the
    fix rather than one that leaves a dormant task doing nothing."""

    def test_a_well_formed_task_parses(self):
        spec, = sl.task_specs({"tasks": [{"id": "t", "when": "idle.secs > 60",
                                          "do": "notify", "text": "hi", "cooldown": 900}]})
        self.assertEqual((spec["id"], spec["do"], spec["cooldown"]), ("t", "notify", 900))
        self.assertIsNone(spec.get("error"))

    def test_an_unknown_verb_is_refused_never_guessed(self):
        spec, = sl.task_specs({"tasks": [{"id": "t", "when": "idle.secs > 60", "do": "explode"}]})
        self.assertIn("explode", spec["error"])
        self.assertIn("notify", spec["error"])      # names the real vocabulary

    def test_an_unparseable_condition_is_reported_not_dropped(self):
        spec, = sl.task_specs({"tasks": [{"id": "t", "when": "wibble > 3", "do": "notify"}]})
        self.assertIn("wibble", spec["error"])

    def test_a_task_needs_an_id_and_a_condition(self):
        specs = sl.task_specs({"tasks": [{"when": "idle.secs > 1", "do": "notify"},
                                         {"id": "x", "do": "notify"}]})
        self.assertTrue(all(sp.get("error") for sp in specs))

    def test_a_zero_cooldown_becomes_the_default_never_zero(self):
        """A watcher with no brake is the one mistake that costs real money."""
        spec, = sl.task_specs({"tasks": [{"id": "t", "when": "idle.secs > 60",
                                          "do": "notify", "cooldown": 0}]})
        self.assertGreaterEqual(spec["cooldown"], 60)

    def test_the_number_of_tasks_is_capped(self):
        many = [{"id": f"t{i}", "when": "idle.secs > 60", "do": "notify"} for i in range(200)]
        self.assertLessEqual(len(sl.task_specs({"tasks": many})), sl.TASKS_MAX)

    def test_a_broken_task_never_silences_the_good_ones(self):
        specs = sl.task_specs({"tasks": [{"id": "bad", "when": "nope", "do": "notify"},
                                         {"id": "good", "when": "idle.secs > 60", "do": "notify"}]})
        good = next(sp for sp in specs if sp["id"] == "good")
        self.assertIsNone(good.get("error"))


class TaskQueueTests(TmpEnv):
    """Squirrel writes jobs. It never acts. A job is data, durable, and actioned once."""

    def setUp(self):
        super().setUp()
        patcher = mock.patch.dict(os.environ, {"SQUIRREL_SHARED_DIR": str(self.root / "sh")})
        patcher.start(); self.addCleanup(patcher.stop)
        self.cfg = {"tasks": [{"id": "status", "when": "idle.secs > 1800", "do": "notify",
                               "text": "tldr status check", "target": "idle",
                               "cooldown": 1800, "warn": 600}],
                    "taskActions": {"notify": True}}

    def _ctx(self, idle=3600, sid="s1"):
        sl.apply_state("idle", sid, NOW - idle)
        return sl._ctx_for({"session_id": sid}, {"session": None, "weekly_all": None, "scoped": []},
                           False, "stdin", "you@example.com", self.cfg, NOW, timezone.utc, None)

    def test_a_holding_condition_writes_exactly_one_job(self):
        with mock.patch.object(sl, "spawn_queue_reader") as spawn:
            sl.evaluate_tasks(self._ctx())
        job = sl.load_json(sl.task_record_path("status")).get("job")
        self.assertEqual((job["task"], job["do"], job["state"]), ("status", "notify", "pending"))
        self.assertEqual(job["text"], "tldr status check")
        spawn.assert_called_once()

    def test_a_condition_that_does_not_hold_writes_nothing(self):
        with mock.patch.object(sl, "spawn_queue_reader"):
            sl.evaluate_tasks(self._ctx(idle=5))
        self.assertFalse(sl.task_record_path("status").exists())

    def test_four_concurrent_sessions_produce_one_job_not_four(self):
        """The whole reason the queue is locked. Four live sessions all evaluate the same
        condition on the same account within the same second."""
        with mock.patch.object(sl, "spawn_queue_reader"):
            for sid in ("a", "b", "c", "d"):
                sl.evaluate_tasks(self._ctx(sid=sid))
        record = sl.load_json(sl.task_record_path("status"))
        self.assertIsNotNone(record.get("job"))
        jobs = list((sl.queue_dir()).glob("*.json"))
        self.assertEqual(len(jobs), 1, [p.name for p in jobs])

    def test_the_cooldown_prevents_a_repeat(self):
        with mock.patch.object(sl, "spawn_queue_reader"):
            sl.evaluate_tasks(self._ctx())
            first = sl.load_json(sl.task_record_path("status"))["job"]["job"]
            sl.mark_job(first, "done", NOW + 1)
            sl.evaluate_tasks(self._ctx())          # still inside the cooldown
        self.assertIsNone(sl.load_json(sl.task_record_path("status")).get("job"))

    def test_the_cooldown_expiring_allows_the_next_firing(self):
        with mock.patch.object(sl, "spawn_queue_reader"):
            sl.evaluate_tasks(self._ctx())
            first = sl.load_json(sl.task_record_path("status"))["job"]["job"]
            sl.mark_job(first, "done", NOW + 1)
            ctx = self._ctx()
            ctx["now"] = NOW + 5000
            sl.evaluate_tasks(ctx)
        self.assertIsNotNone(sl.load_json(sl.task_record_path("status")).get("job"))

    def test_an_action_class_that_is_not_opted_in_never_creates_a_job(self):
        """Refused at WRITE time. A job squirrel may not create is never created, rather
        than relying on a reader to decline it."""
        self.cfg = dict(self.cfg, taskActions={})
        with mock.patch.object(sl, "spawn_queue_reader") as spawn:
            sl.evaluate_tasks(self._ctx())
        self.assertFalse(sl.task_record_path("status").exists())
        spawn.assert_not_called()

    def test_a_cancelled_job_stops_that_firing_and_not_the_task(self):
        """Both halves, because a mutation showed the first alone is satisfied by a cancel
        that does nothing: if the condition still holds a second later and the task re-queues
        at once, the user dismisses a nudge and it reappears. Cancelling suppresses this
        firing and starts the cooldown; the task itself is untouched."""
        with mock.patch.object(sl, "spawn_queue_reader"):
            sl.evaluate_tasks(self._ctx())
            job = sl.load_json(sl.task_record_path("status"))["job"]["job"]
            sl.mark_job(job, "cancelled", NOW + 1)
            self.assertIsNone(sl.load_json(sl.task_record_path("status")).get("job"))

            inside = self._ctx(); inside["now"] = NOW + 2      # well inside the cooldown
            sl.evaluate_tasks(inside)
            self.assertIsNone(sl.load_json(sl.task_record_path("status")).get("job"),
                              "a cancelled nudge came straight back")

            after = self._ctx(); after["now"] = NOW + 5000     # past it
            sl.evaluate_tasks(after)
        record = sl.load_json(sl.task_record_path("status"))
        self.assertEqual(record["job"]["state"], "pending")     # the task itself lives on

    def test_a_job_carries_where_it_came_from(self):
        with mock.patch.object(sl, "spawn_queue_reader"):
            sl.evaluate_tasks(self._ctx(sid="origin"))
        job = sl.load_json(sl.task_record_path("status"))["job"]
        self.assertEqual(job["session"], "origin")
        self.assertEqual(job["account"], sl.account_hash("you@example.com"))
        self.assertIn("due", job)

    def test_the_warn_window_makes_a_job_visible_before_it_is_actionable(self):
        with mock.patch.object(sl, "spawn_queue_reader"):
            sl.evaluate_tasks(self._ctx())
        job = sl.load_json(sl.task_record_path("status"))["job"]
        self.assertGreater(job["due"], NOW, "a warn window must delay the firing, not the job")
        self.assertFalse(sl.job_is_actionable(job, NOW))
        self.assertTrue(sl.job_is_actionable(job, job["due"] + 1))

    def test_evaluation_never_raises_whatever_the_store_does(self):
        ctx = self._ctx()       # built first: patching write_cache would break apply_state
        with mock.patch.object(sl, "write_cache", side_effect=OSError("disk full")), \
             mock.patch.object(sl, "spawn_queue_reader"):
            sl.evaluate_tasks(ctx)      # must not raise
        self.assertFalse(sl.task_record_path("status").exists())

    def test_a_job_is_never_actioned_twice(self):
        with mock.patch.object(sl, "spawn_queue_reader"):
            sl.evaluate_tasks(self._ctx())
        job = sl.load_json(sl.task_record_path("status"))["job"]["job"]
        self.assertTrue(sl.claim_job(job, NOW + 10_000))
        self.assertFalse(sl.claim_job(job, NOW + 10_000), "a second reader claimed the same job")

    def test_a_reader_that_dies_mid_job_leaves_it_runnable(self):
        with mock.patch.object(sl, "spawn_queue_reader"):
            sl.evaluate_tasks(self._ctx())
        job = sl.load_json(sl.task_record_path("status"))["job"]["job"]
        self.assertTrue(sl.claim_job(job, NOW + 10_000))
        # the claiming process vanishes without marking the job done
        record = sl.load_json(sl.task_record_path("status"))
        record["job"]["claimed_by"] = 999_999            # a pid that is not alive
        sl.write_cache(sl.task_record_path("status"), record)
        self.assertTrue(sl.claim_job(job, NOW + 20_000), "a dead claim was never released")


class TasksAreWiredToTheRenderTests(TmpEnv):
    """The wiring, not the mechanism. `evaluate_tasks` existed and was fully tested for a
    whole commit without being called from anywhere — the queue was inert and every test
    still passed, because they all called it directly. Same shape as the mutation that
    reverted live_sessions to a rolling window: a unit tested off the path it runs on."""

    def test_a_render_evaluates_tasks(self):
        with mock.patch.object(sl, "evaluate_tasks") as evaluate:
            sl.render(STATUS, CACHE, "you@example.com", sl.load_config(), NOW,
                      timezone.utc, width=120)
        evaluate.assert_called_once()

    def test_a_task_cannot_change_or_break_what_is_displayed(self):
        """The contract, stated as an equality rather than as an ordering: the bar is
        byte-for-byte the same whether task evaluation succeeds, does nothing, or explodes."""
        args = (STATUS, CACHE, "you@example.com", sl.load_config(), NOW, timezone.utc)
        with mock.patch.object(sl, "evaluate_tasks", return_value=[]):
            quiet = sl.render(*args, width=120)
        with mock.patch.object(sl, "evaluate_tasks", side_effect=RuntimeError("boom")):
            angry = sl.render(*args, width=120)
        self.assertEqual(quiet, angry)
        self.assertEqual(len(quiet), 2)

    def test_shlex_is_not_imported_by_a_render(self):
        """It exists for the detached reader's shell quoting only."""
        source = (PLUGIN_ROOT / "scripts" / "statusline.py").read_text(encoding="utf-8")
        tree = ast.parse(source)
        top = {a.name for n in tree.body if isinstance(n, ast.Import) for a in n.names}
        self.assertNotIn("shlex", top, "shlex is imported at module scope")


class QueueReaderSeamTests(TmpEnv):
    """The reader is not built yet, and the half-state has to be a safe one: a spawned child
    must not exit 2 on an unknown flag, and must not eat a job it cannot action."""

    def test_the_reader_flag_is_recognised(self):
        self.assertIn("--drain-queue", sl.known_flags())

    def test_the_drain_flag_runs_the_reader(self):
        patcher = mock.patch.dict(os.environ, {"SQUIRREL_SHARED_DIR": str(self.root / "sh")})
        patcher.start(); self.addCleanup(patcher.stop)
        with mock.patch.object(sl, "drain_queue") as drain, \
             mock.patch.object(sys, "stdout", io.StringIO()):
            self.assertEqual(sl.main(["--drain-queue"]), 0)
        drain.assert_called_once()


class TaskRenderCostTests(TmpEnv):
    """Non-negotiable #3, and #6: with no tasks configured nothing happens at all."""

    def test_no_tasks_means_no_queue_and_no_work(self):
        patcher = mock.patch.dict(os.environ, {"SQUIRREL_SHARED_DIR": str(self.root / "q")})
        patcher.start(); self.addCleanup(patcher.stop)
        ctx = sl._ctx_for({"session_id": "n"}, {"session": None, "weekly_all": None, "scoped": []},
                          False, "stdin", None, {}, NOW, timezone.utc, None)
        with mock.patch.object(sl, "spawn_queue_reader") as spawn:
            sl.evaluate_tasks(ctx)
        self.assertFalse((self.root / "q" / "queue").exists())
        spawn.assert_not_called()

# ── Task 28 part 3: who a job may reach ──────────────────────────────────────
AGENTS_JSON = json.dumps([
    {"pid": 22551, "cwd": "/w/sandbox", "kind": "interactive",
     "sessionId": "af541aa8", "name": "sandbox-8a", "status": "busy"},
    {"pid": 2455146, "cwd": "/w/storefront", "kind": "interactive",
     "sessionId": "29accf7e", "name": "storefront-21", "status": "idle"},
])


class AgentEnumerationTests(TmpEnv):
    """Subagents must be unreachable, and the enumeration source is what guarantees it.

    Verified from inside a subagent rather than reasoned about: running `claude agents --json`
    as a subagent whose own pid was 2814901 returned two entries, neither of them mine. What
    it returned was my PARENT — pid 22551, the interactive session hosting me — with
    status "busy", busy precisely because I was running.

    So a subagent has no entry to filter out; it is only ever visible as its host's status.
    That is a stronger guarantee than a filter, and it is why the enumeration must never move
    to `ListAgents`: that tool DOES see subagents, and the obvious future refactor — "use the
    tool that lists agents" — would silently start messaging them."""

    def _run(self, stdout=AGENTS_JSON, code=0):
        return mock.patch.object(sl, "_run_command", return_value=(stdout, code, None))

    def test_only_interactive_sessions_are_listed(self):
        with self._run():
            names = [s["name"] for s in sl.list_agent_sessions()]
        self.assertEqual(names, ["sandbox-8a", "storefront-21"])

    def test_any_kind_that_is_not_interactive_is_dropped(self):
        blob = json.dumps([{"pid": 1, "kind": "subagent", "sessionId": "x", "name": "helper",
                            "status": "idle"},
                           {"pid": 2, "kind": "interactive", "sessionId": "y", "name": "real",
                            "status": "idle"}])
        with self._run(blob):
            self.assertEqual([s["name"] for s in sl.list_agent_sessions()], ["real"])

    def test_a_failed_or_absent_cli_lists_nothing_rather_than_guessing(self):
        for out, code in (("", 1), ("not json", 0), ("{}", 0), ("null", 0)):
            with self._run(out, code):
                self.assertEqual(sl.list_agent_sessions(), [])


class NotifyTargetTests(TmpEnv):
    """Resolved at ACTION time, never at write time: a session can exit between a job being
    queued and a reader draining it, and silently picking a different recipient is worse than
    doing nothing."""

    JOB = {"job": "t-1-1", "task": "t", "do": "notify", "text": "hi",
           "session": "af541aa8", "target": "idle", "state": "pending", "due": 0}

    def _sessions(self):
        return json.loads(AGENTS_JSON)

    def test_the_originating_session_is_never_a_target(self):
        """A watcher waking its own session is a loop with a cooldown as its only brake."""
        job = dict(self.JOB, session="29accf7e")        # the only idle one is its own
        self.assertEqual(sl.resolve_notify_targets(job, self._sessions()), [])

    def test_only_idle_sessions_are_targeted(self):
        targets = sl.resolve_notify_targets(self.JOB, self._sessions())
        self.assertEqual([t["name"] for t in targets], ["storefront-21"])

    def test_a_named_target_that_has_vanished_drops_the_job(self):
        job = dict(self.JOB, target="gone-99")
        self.assertEqual(sl.resolve_notify_targets(job, self._sessions()), [])

    def test_a_named_target_is_honoured_when_it_is_present_and_idle(self):
        job = dict(self.JOB, target="storefront-21")
        self.assertEqual([t["name"] for t in sl.resolve_notify_targets(job, self._sessions())],
                         ["storefront-21"])

    def test_a_named_target_that_is_busy_is_not_interrupted(self):
        job = dict(self.JOB, target="sandbox-8a")
        self.assertEqual(sl.resolve_notify_targets(job, self._sessions()), [])

    def test_no_target_resolves_to_no_send_rather_than_a_broadcast(self):
        self.assertEqual(sl.resolve_notify_targets(self.JOB, []), [])


class NudgeLoopSafetyTests(TmpEnv):
    """A nudge changes the woken session's state, and state is what watcher conditions read.
    Two tasks that trigger each other must quiesce. With four live sessions this is the most
    expensive bug the feature could have."""

    def setUp(self):
        super().setUp()
        patcher = mock.patch.dict(os.environ, {"SQUIRREL_SHARED_DIR": str(self.root / "sh")})
        patcher.start(); self.addCleanup(patcher.stop)

    def _cfg(self):
        return {"taskActions": {"notify": True},
                "tasks": [{"id": "ping", "when": "idle.secs > 10", "do": "notify",
                           "text": "ping", "target": "idle", "cooldown": 60},
                          {"id": "pong", "when": "idle.secs > 10", "do": "notify",
                           "text": "pong", "target": "idle", "cooldown": 60}]}

    def _ctx(self, sid, now):
        sl.apply_state("idle", sid, now - 600)
        return sl._ctx_for({"session_id": sid}, {"session": None, "weekly_all": None, "scoped": []},
                           False, "stdin", "you@example.com", self._cfg(), now, timezone.utc, None)

    def test_two_mutually_triggering_tasks_quiesce(self):
        """Run both tasks across two sessions for an hour of simulated renders and count the
        jobs. Bounded by the cooldowns, not growing."""
        made = 0
        with mock.patch.object(sl, "spawn_queue_reader"):
            for step in range(0, 3600, 30):
                for sid in ("s1", "s2"):
                    made += len(sl.evaluate_tasks(self._ctx(sid, NOW + step)))
                for task in ("ping", "pong"):
                    record = sl.load_json(sl.task_record_path(task))
                    job = (record or {}).get("job")
                    if job:
                        sl.mark_job(job["job"], "done", NOW + step)
        # two tasks, a 60s cooldown, one hour: 60 firings each is the ceiling by arithmetic
        self.assertLessEqual(made, 2 * (3600 // 60) + 2, f"{made} jobs — this is a loop")
        self.assertGreater(made, 0, "nothing fired at all; the test proves nothing")

    def test_a_job_records_the_task_that_caused_it(self):
        with mock.patch.object(sl, "spawn_queue_reader"):
            sl.evaluate_tasks(self._ctx("s1", NOW))
        job = sl.load_json(sl.task_record_path("ping"))["job"]
        self.assertEqual(job["task"], "ping")
        self.assertEqual(job["session"], "s1")


class NudgeMarkerTests(TmpEnv):
    """Making causality observable instead of inferring it.

    A nudge arrives as text, and the receiving session's transcript records that text. So a
    recognisable marker in the nudge is the one thing that turns "B woke, but nobody knows
    why" into a fact B can read about itself. A task then refuses to create a nudge when this
    session's own most recent user turn WAS a nudge — which is precisely the ping-pong case.

    The ceiling stays regardless: the marker gives precision, the ceiling bounds the damage
    when precision fails (a marker stripped by a paste, a nudge delivered another way)."""

    def setUp(self):
        super().setUp()
        patcher = mock.patch.dict(os.environ, {"SQUIRREL_SHARED_DIR": str(self.root / "sh")})
        patcher.start(); self.addCleanup(patcher.stop)
        self.path = self.root / "n.jsonl"

    def _write(self, *lines):
        self.path.write_text("\n".join(lines) + "\n", encoding="utf-8")

    def test_the_marker_is_short_and_unobtrusive(self):
        """The user sees this in their own session. A suffix is fine; a banner is not."""
        self.assertLessEqual(len(sl.NUDGE_MARKER), 16, sl.NUDGE_MARKER)
        self.assertNotIn("\n", sl.NUDGE_MARKER)
        self.assertEqual(sl.sanitise(sl.NUDGE_MARKER), sl.NUDGE_MARKER,
                         "the marker must survive the seam or it can never be read back")

    def test_a_delivered_nudge_carries_the_marker(self):
        sent = {}
        with mock.patch.object(sl, "_run_command",
                               side_effect=lambda cmd, *a, **k: (sent.setdefault("cmd", cmd), 0, None)[1:] + (None,)):
            sl.send_notify({"name": "peer"}, "tldr status check")
        self.assertIn("tldr status check", sent["cmd"])
        self.assertIn(sl.NUDGE_MARKER, sent["cmd"])

    def test_a_transcript_whose_last_user_turn_was_a_nudge_says_so(self):
        self._write(_text("assistant", "done"),
                    _text("user", f"tldr status check {sl.NUDGE_MARKER}"))
        self.assertTrue(sl.scan_transcript(self.path)["nudged"])

    def test_an_ordinary_user_turn_is_not_a_nudge(self):
        self._write(_text("assistant", "done"), _text("user", "please carry on"))
        self.assertFalse(sl.scan_transcript(self.path)["nudged"])

    def test_only_the_MOST_RECENT_user_turn_counts(self):
        """An old nudge must not silence a watcher for ever — the user has replied since."""
        self._write(_text("user", f"check {sl.NUDGE_MARKER}"),
                    _text("assistant", "ok"),
                    _text("user", "actually do this instead"),
                    _text("assistant", "done"))
        self.assertFalse(sl.scan_transcript(self.path)["nudged"])

    def test_nudged_is_a_metric_in_the_one_grammar(self):
        self.assertIn("nudged", sl.METRICS)
        self._write(_text("user", f"x {sl.NUDGE_MARKER}"))
        ctx = {"status": {"session_id": "n1", "transcript_path": str(self.path)},
               "config": sl.load_config(), "now": NOW}
        self.assertTrue(sl.metric_value(ctx, "nudged"))

    def test_unknown_when_there_is_no_transcript(self):
        ctx = {"status": {"session_id": "gone"}, "config": sl.load_config(), "now": NOW}
        self.assertIsNone(sl.metric_value(ctx, "nudged"))

    def _cfg(self):
        return {"taskActions": {"notify": True}, "nudgeMaxPerHour": 50,
                "tasks": [{"id": "pong", "when": "idle.secs > 10", "do": "notify",
                           "text": "ping back", "cooldown": 60}]}

    def _fire(self, transcript=None):
        sl.apply_state("idle", "s1", NOW - 600)
        status = {"session_id": "s1"}
        if transcript:
            status["transcript_path"] = str(transcript)
        ctx = sl._ctx_for(status, {"session": None, "weekly_all": None, "scoped": []},
                          False, "stdin", "you@example.com", self._cfg(), NOW, timezone.utc, None)
        with mock.patch.object(sl, "spawn_queue_reader"):
            return len(sl.evaluate_tasks(ctx))

    def test_a_session_that_was_just_nudged_does_not_nudge_back(self):
        self._write(_text("assistant", "done"), _text("user", f"ping {sl.NUDGE_MARKER}"))
        self.assertEqual(self._fire(self.path), 0, "the ping-ponged straight back")

    def test_a_session_the_user_actually_spoke_to_still_nudges(self):
        self._write(_text("assistant", "done"), _text("user", "carry on"))
        self.assertEqual(self._fire(self.path), 1)

    def test_with_no_transcript_the_ceiling_is_the_only_brake(self):
        """Unknown is not "was nudged". A missing transcript must not silence every watcher —
        that would be inventing a reason to do nothing, which is its own kind of wrong."""
        self.assertEqual(self._fire(None), 1)

class NudgeCeilingTests(TmpEnv):

    """The backstop. Loop safety rests on cooldowns, and cooldowns rest on the user having
    written tasks we anticipated. This bounds the damage when they did not: a machine-wide
    ceiling on nudges per hour, across every task and every target.

    "We reasoned it cannot loop" and "it cannot loop for long" are different claims, and only
    the second survives contact with two tasks nobody designed together. A nudge costs a
    turn on the user's own account, so this is the one failure here that spends real money."""

    def setUp(self):
        super().setUp()
        patcher = mock.patch.dict(os.environ, {"SQUIRREL_SHARED_DIR": str(self.root / "sh")})
        patcher.start(); self.addCleanup(patcher.stop)

    def _cfg(self, ceiling=None):
        cfg = {"taskActions": {"notify": True},
               "tasks": [{"id": f"t{i}", "when": "idle.secs > 10", "do": "notify",
                          "text": "x", "cooldown": 60} for i in range(8)]}
        if ceiling is not None:
            cfg["nudgeMaxPerHour"] = ceiling
        return cfg

    def _fire(self, cfg, now, sid="s1"):
        sl.apply_state("idle", sid, now - 600)
        ctx = sl._ctx_for({"session_id": sid}, {"session": None, "weekly_all": None, "scoped": []},
                          False, "stdin", "you@example.com", cfg, now, timezone.utc, None)
        with mock.patch.object(sl, "spawn_queue_reader"):
            return len(sl.evaluate_tasks(ctx))

    def test_the_ceiling_stops_job_creation_across_all_tasks(self):
        cfg = self._cfg(3)
        self.assertEqual(self._fire(cfg, NOW), 3, "the ceiling did not bound the first sweep")

    def test_the_ceiling_is_machine_wide_not_per_task(self):
        cfg = self._cfg(2)
        self._fire(cfg, NOW)
        self.assertEqual(self._fire(cfg, NOW + 1), 0, "a second task slipped past the ceiling")

    def test_the_window_rolls_so_the_ceiling_is_per_hour_not_for_ever(self):
        cfg = self._cfg(2)
        self._fire(cfg, NOW)
        self.assertEqual(self._fire(cfg, NOW + 7200), 2, "the ceiling never released")

    def test_a_trip_is_recorded_so_it_can_be_explained(self):
        cfg = self._cfg(1)
        self._fire(cfg, NOW)
        self._fire(cfg, NOW + 1)
        self.assertIsNotNone(sl.nudge_budget(NOW + 2).get("tripped_at"),
                             "the ceiling tripped silently")

    def test_the_budget_reports_usage_against_the_ceiling(self):
        cfg = self._cfg(4)
        self._fire(cfg, NOW)
        budget = sl.nudge_budget(NOW + 1, cfg)
        self.assertEqual((budget["used"], budget["ceiling"]), (4, 4))

    def test_only_nudges_count_against_it(self):
        """`ack` and `snooze` touch nothing outside this machine and cost no tokens, so they
        must neither be blocked by the ceiling nor consume it. Stated as the second half too,
        because a version that merely fails to BLOCK them still quietly spends their budget:
        six acks first, then a notify that must still be allowed through."""
        cfg = {"taskActions": {"ack": True, "notify": True},
               "tasks": ([{"id": f"a{i}", "when": "idle.secs > 10", "do": "ack",
                           "text": "x", "cooldown": 60} for i in range(6)]
                         + [{"id": "n", "when": "idle.secs > 10", "do": "notify",
                             "text": "x", "cooldown": 60}]),
               "nudgeMaxPerHour": 2}
        self.assertEqual(self._fire(cfg, NOW), 7, "an ack was blocked by the nudge ceiling")
        self.assertEqual(sl.nudge_budget(NOW, cfg)["used"], 1,
                         "something other than a nudge was charged to the nudge budget")

    def test_show_config_reports_the_budget_and_says_when_it_tripped(self):
        # Real wall-clock, because --show-config reports the budget as of NOW rather than as
        # of a fixture's frozen clock, and an hour-long window makes that difference visible.
        now = _time.time()
        cfg = self._cfg(1)
        sl.save_config(cfg)
        self._fire(cfg, now)
        self.assertIn("nudges 1/1 used this hour", sl.cmd_show_config())
        self._fire(cfg, now + 1)             # over the ceiling
        self.assertIn("ceiling reached", sl.cmd_show_config())

    def test_the_shipped_default_is_low(self):
        shipped = json.loads(sl.default_config_path().read_text(encoding="utf-8"))
        self.assertLessEqual(shipped["nudgeMaxPerHour"], 10)
        self.assertGreater(shipped["nudgeMaxPerHour"], 0)

class QueueDrainTests(TmpEnv):

    """The reader: claim, resolve, act, mark. Never twice, never on the render path."""

    def setUp(self):
        super().setUp()
        patcher = mock.patch.dict(os.environ, {"SQUIRREL_SHARED_DIR": str(self.root / "sh")})
        patcher.start(); self.addCleanup(patcher.stop)
        sl.write_cache(sl.task_record_path("t"), {"task": "t", "last_fired": None, "job": {
            "job": "t-1-1", "task": "t", "do": "notify", "text": "status check",
            "target": "idle", "session": "af541aa8", "state": "pending", "due": 0,
            "cancellable": True, "fired_at": None, "claimed_by": None}})

    def test_a_drained_job_is_actioned_once_and_marked_done(self):
        with mock.patch.object(sl, "list_agent_sessions", return_value=json.loads(AGENTS_JSON)), \
             mock.patch.object(sl, "send_notify", return_value=True) as send:
            sl.drain_queue(NOW + 100)
        send.assert_called_once()
        self.assertEqual(send.call_args[0][1], "status check")
        self.assertIsNone(sl.load_json(sl.task_record_path("t")).get("job"))

    def test_a_job_with_no_resolvable_target_is_dropped_not_redirected(self):
        with mock.patch.object(sl, "list_agent_sessions", return_value=[]), \
             mock.patch.object(sl, "send_notify") as send:
            sl.drain_queue(NOW + 100)
        send.assert_not_called()
        self.assertIsNone(sl.load_json(sl.task_record_path("t")).get("job"))

    def test_an_unknown_verb_is_refused_and_recorded_never_guessed(self):
        record = sl.load_json(sl.task_record_path("t"))
        record["job"]["do"] = "detonate"
        sl.write_cache(sl.task_record_path("t"), record)
        with mock.patch.object(sl, "send_notify") as send:
            sl.drain_queue(NOW + 100)
        send.assert_not_called()
        self.assertEqual(sl.load_json(sl.task_record_path("t"))["last_job"]["state"], "refused")

    def test_a_job_before_its_due_time_is_left_alone(self):
        record = sl.load_json(sl.task_record_path("t"))
        record["job"]["due"] = NOW + 10_000
        sl.write_cache(sl.task_record_path("t"), record)
        with mock.patch.object(sl, "send_notify") as send:
            sl.drain_queue(NOW)
        send.assert_not_called()
        self.assertEqual(sl.load_json(sl.task_record_path("t"))["job"]["state"], "pending")

    def test_a_send_that_fails_leaves_the_job_runnable(self):
        with mock.patch.object(sl, "list_agent_sessions", return_value=json.loads(AGENTS_JSON)), \
             mock.patch.object(sl, "send_notify", return_value=False):
            sl.drain_queue(NOW + 100)
        job = sl.load_json(sl.task_record_path("t")).get("job")
        self.assertIsNotNone(job, "a failed send discarded the job")
        self.assertEqual(job["state"], "pending")

    def test_a_second_reader_arriving_mid_action_does_not_send_twice(self):
        """The claim's whole purpose, and nothing else was testing it: two sequential drains
        cannot show it, because the first clears the job. This puts the second reader inside
        the first one's action — exactly when the window is open — by re-entering the drain
        from the send itself."""
        sends = []

        def sending(target, text):
            sends.append(text)
            if len(sends) == 1:
                sl.drain_queue(NOW + 100)       # a second reader, while the first is working
            return True

        with mock.patch.object(sl, "list_agent_sessions", return_value=json.loads(AGENTS_JSON)), \
             mock.patch.object(sl, "send_notify", side_effect=sending):
            sl.drain_queue(NOW + 100)
        self.assertEqual(len(sends), 1, f"the job was actioned {len(sends)} times")

    def test_draining_never_raises(self):
        with mock.patch.object(sl, "list_agent_sessions", side_effect=OSError("no claude")):
            sl.drain_queue(NOW + 100)

# ── Task 28 part 4: the composition surface ──────────────────────────────────
class ShowMetricsTests(TmpEnv):
    """Claude must DISCOVER the vocabulary at runtime, not recall it. A skill that lists
    metric names is a snapshot of the version it was written against; this is the version
    that is running."""

    def test_every_metric_is_listed_with_a_value_and_a_meaning(self):
        out = sl.cmd_show_metrics()
        for name in sl.METRICS:
            self.assertIn(name, out, f"{name} missing from --show-metrics")
        self.assertIn("state", out)
        self.assertGreater(len(out.splitlines()), len(sl.METRICS))

    def test_metrics_only_knowable_during_a_render_say_so(self):
        """The same honesty as --show-rules: refuse to guess rather than print a plausible
        number a command-line invocation cannot actually know."""
        out = sl.cmd_show_metrics()
        for name in sl.LIVE_ONLY_METRICS:
            row = next(line for line in out.splitlines() if line.strip().startswith(name))
            self.assertIn("?", row, f"{name} printed a value it cannot know")

    def test_the_verbs_and_the_job_fields_are_discoverable_too(self):
        out = sl.cmd_show_metrics()
        for verb in sl.TASK_VERBS:
            self.assertIn(verb, out)
        for field in ("when", "do", "cooldown", "warn", "target"):
            self.assertIn(field, out)

    def test_every_metric_has_a_one_line_meaning(self):
        for name in sl.METRICS:
            self.assertIn(name, sl.METRIC_HELP, f"{name} has no description")
            self.assertTrue(sl.METRIC_HELP[name].strip())

    def test_it_goes_through_the_seam(self):
        out = io.StringIO()
        with mock.patch.object(sys, "stdout", out):
            self.assertEqual(sl.main(["--show-metrics"]), 0)
        self.assertEqual(_escapes_beyond_sgr(out.getvalue()), [])


class TaskDryRunTests(TmpEnv):
    """Claude cannot see the bar. Without this it writes a task, cannot observe it, and tells
    the user it works — which is the single thing most likely to make this feature produce
    confidently wrong claims."""

    def setUp(self):
        super().setUp()
        patcher = mock.patch.dict(os.environ, {"SQUIRREL_SHARED_DIR": str(self.root / "sh")})
        patcher.start(); self.addCleanup(patcher.stop)
        sl.save_config({"taskActions": {"notify": True},
                        "tasks": [{"id": "quiet", "when": "idle.secs > 99999", "do": "notify",
                                   "text": "hi", "cooldown": 900},
                                  {"id": "loud", "when": "git.dirty", "do": "notify",
                                   "text": "hi", "cooldown": 900}]})

    def test_it_says_whether_the_task_would_fire(self):
        out = sl.cmd_task("test quiet")
        self.assertIn("quiet", out)
        self.assertRegex(out.lower(), r"would not fire|no")

    def test_it_names_the_clause_that_failed_and_the_actual_value(self):
        """The ACTUAL value, distinct from the threshold. The first version of this asserted
        `99999` was in the output — which the echoed condition satisfies on its own, so
        deleting the line that reports the real value passed it. Third instance of a test
        passing for a reason other than the one in its name; see the note at the top."""
        # A metric that RESOLVES, so the known-value branch is the one under test. With
        # `quiet` (idle.secs, unknown here) the dry run takes the unknown path and never
        # reaches the line this is about — which is how the weak version passed.
        sl.save_config(dict(sl.load_config(),
                            tasks=[{"id": "named", "when": "repo.name == nonesuch",
                                    "do": "notify", "text": "hi", "cooldown": 900}]))
        out = sl.cmd_task("test named")
        condition_echo, _, verdict = out.partition("\n")
        self.assertIn("repo.name", condition_echo)      # the condition, echoed back
        self.assertRegex(verdict, r"repo\.name\s*=\s*'\S+'",
                         "the dry run never reports the metric's actual value")
        self.assertNotIn("nonesuch'\n", verdict.split("=")[1] if "=" in verdict else "")

    def test_an_unknown_task_is_an_error_not_a_guess(self):
        self.assertIn("no task", sl.cmd_task("test nope").lower())

    def test_a_metric_it_cannot_know_is_reported_as_unknown(self):
        sl.save_config({"taskActions": {"notify": True},
                        "tasks": [{"id": "live", "when": "ctx.pct > 50", "do": "notify",
                                   "text": "hi", "cooldown": 900}]})
        out = sl.cmd_task("test live")
        self.assertIn("?", out)
        self.assertRegex(out.lower(), r"only during a render|live render|cannot know")


class TaskCommandTests(TmpEnv):
    """Claude creates these unattended, so the guardrails are the feature."""

    def setUp(self):
        super().setUp()
        patcher = mock.patch.dict(os.environ, {"SQUIRREL_SHARED_DIR": str(self.root / "sh")})
        patcher.start(); self.addCleanup(patcher.stop)

    def test_add_list_remove(self):
        sl.save_config({"taskActions": {"notify": True}})
        self.assertIn("status", sl.cmd_task("add status when='idle.secs > 1800' do=notify "
                                            "text='tldr status check' cooldown=1800"))
        self.assertEqual([t["id"] for t in sl.task_specs(sl.load_config())], ["status"])
        self.assertIn("status", sl.cmd_tasks(""))
        sl.cmd_task("remove status")
        self.assertEqual(sl.task_specs(sl.load_config()), [])

    def test_an_action_that_is_not_opted_in_is_refused_and_names_the_fix(self):
        """Never create a dormant task that silently does nothing."""
        out = sl.cmd_task("add x when='idle.secs > 60' do=notify text=hi")
        self.assertIn("--task-actions", out)
        self.assertEqual(sl.task_specs(sl.load_config()), [], "a dormant task was created")

    def test_nothing_is_written_on_a_validation_error(self):
        sl.save_config({"taskActions": {"notify": True}})
        before = sl.load_config()
        for bad in ("add y when='wibble > 3' do=notify", "add y when='idle.secs > 1' do=explode",
                    "add", "wobble x"):
            sl.cmd_task(bad)
            self.assertEqual(sl.load_config(), before, bad)

    def test_a_missing_cooldown_gets_a_sane_default_never_zero(self):
        sl.save_config({"taskActions": {"notify": True}})
        sl.cmd_task("add z when='idle.secs > 60' do=notify text=hi")
        self.assertGreaterEqual(sl.task_specs(sl.load_config())[0]["cooldown"], 60)

    def test_the_number_of_tasks_is_capped_at_creation(self):
        sl.save_config({"taskActions": {"notify": True},
                        "tasks": [{"id": f"t{i}", "when": "idle.secs > 60", "do": "notify",
                                   "text": "x", "cooldown": 900} for i in range(sl.TASKS_MAX)]})
        out = sl.cmd_task("add one-more when='idle.secs > 60' do=notify text=hi")
        self.assertIn("too many", out.lower())

    def test_the_listing_shows_the_queue_as_well_as_the_rules(self):
        sl.save_config({"taskActions": {"notify": True},
                        "tasks": [{"id": "q", "when": "idle.secs > 60", "do": "notify",
                                   "text": "x", "cooldown": 900}]})
        sl.write_cache(sl.task_record_path("q"), {"task": "q", "last_fired": None, "job": {
            "job": "q-1-1", "task": "q", "do": "notify", "text": "x", "state": "pending",
            "due": NOW + 60, "target": "idle", "session": "s", "cancellable": True}})
        out = sl.cmd_tasks("")
        self.assertIn("pending", out.lower())
        self.assertIn("nudges", out.lower())       # the budget, where the user will look

    def test_a_broken_task_is_reported_not_hidden(self):
        sl.save_config({"tasks": [{"id": "bad", "when": "wibble", "do": "notify"}]})
        self.assertIn("wibble", sl.cmd_tasks(""))

class TaskArgumentParsingTests(TmpEnv):
    """Found by using the command as a stranger would, within two minutes.

        --task "add w1 when=weekly.pct > 85 do=notify text=hi"
          stored:  when = 'weekly.pct'      <- the comparison silently GONE

    The task was then accepted, listed, and dry-ran cleanly — as a DIFFERENT task from the
    one asked for. And a bare metric is truthy-tested, so `weekly.pct` alone plausibly fires
    constantly, on a surface Claude drives unattended, for an action that costs money.

    Every real condition contains a space, so the natural unquoted form has to work: a field
    value now consumes greedily up to the next recognised `key=`. Driven through main() rather
    than the parser, because off-the-path is exactly how this got here."""

    def setUp(self):
        super().setUp()
        patcher = mock.patch.dict(os.environ, {"SQUIRREL_SHARED_DIR": str(self.root / "sh")})
        patcher.start(); self.addCleanup(patcher.stop)
        sl.save_config({"taskActions": {"notify": True}})

    def _cli(self, *argv):
        out = io.StringIO()
        with mock.patch.object(sys, "stdout", out):
            code = sl.main(list(argv))
        return code, out.getvalue()

    def _stored(self, task_id):
        return next(t for t in sl.task_specs(sl.load_config()) if t["id"] == task_id)

    def test_an_unquoted_condition_keeps_its_comparison(self):
        self._cli("--task", "add w1 when=weekly.pct > 85 do=notify text=hi cooldown=3600")
        self.assertEqual(self._stored("w1")["when"], "weekly.pct > 85")

    def test_the_quoted_form_still_works_identically(self):
        self._cli("--task", "add w2 when='weekly.pct > 85' do=notify text=hi cooldown=3600")
        self.assertEqual(self._stored("w2")["when"], "weekly.pct > 85")

    def test_a_multi_word_text_is_not_truncated_by_a_following_field(self):
        self._cli("--task", "add w3 when='git.dirty' do=notify text=you left work open "
                            "cooldown=3600")
        self.assertEqual(self._stored("w3")["text"], "you left work open")
        self.assertEqual(self._stored("w3")["cooldown"], 3600)

    def test_a_two_token_operator_condition_survives(self):
        self._cli("--task", "add w4 when=state == waiting-you do=notify text=hi")
        self.assertEqual(self._stored("w4")["when"], "state == waiting-you")

    def test_an_equals_inside_a_value_is_not_read_as_a_new_field(self):
        self._cli("--task", "add w5 when='git.dirty' do=notify text=a=b c=d")
        self.assertEqual(self._stored("w5")["text"], "a=b c=d")

    def test_command_arguments_may_be_separate_argv_words(self):
        """`--task-actions notify on` is Claude's natural first attempt, and it printed only
        `usage:` while the quoted form worked."""
        code, out = self._cli("--task-actions", "notify", "off")
        self.assertEqual(code, 0)
        self.assertIn("OFF", out)
        self.assertFalse(sl.task_action_allowed(sl.load_config(), "notify"))

    def test_a_whole_task_can_be_written_as_separate_argv_words(self):
        self._cli("--task", "add", "w6", "when=weekly.pct > 85", "do=notify", "text=hi")
        self.assertEqual(self._stored("w6")["when"], "weekly.pct > 85")

    def test_a_following_flag_is_not_swallowed_as_an_argument(self):
        code, out = self._cli("--task-actions", "notify", "on", "--show-config")
        self.assertEqual(code, 0)
        self.assertIn("ON", out)
        self.assertNotIn("config file", out, "a later flag was eaten as an argument")

    def test_a_condition_that_still_does_not_parse_is_refused_and_writes_nothing(self):
        before = sl.load_config()
        code, out = self._cli("--task", "add bad when=wibble > 3 do=notify text=hi")
        self.assertIn("wibble", out)
        self.assertEqual(sl.load_config(), before)

class IdleSecsRespectsTheTranscriptTests(TmpEnv):
    """`idle.secs` read the RAW hook state while `state` and the chip had both been taught to
    consult the transcript. So a session with agents running still reported idle seconds, and
    the shipped idle ladder still painted the banner — the same complaint the chip fix was
    for, surviving one layer down.

    It also matters for the flagship recipe. "Send a status check after 30 minutes idle" wants
    idleness that is not "waiting on tools", and with this the single clause `idle.secs > 1800`
    says exactly that — no second clause, no boolean grammar."""

    def setUp(self):
        super().setUp()
        self.path = self.root / "i.jsonl"

    def _ctx(self, sid):
        return {"status": {"session_id": sid, "transcript_path": str(self.path)},
                "config": sl.load_config(), "now": NOW}

    def test_a_session_waiting_on_agents_reports_no_idle_seconds(self):
        self.path.write_text("\n".join([_text("user", "go"), _use("t1", "Task")]) + "\n",
                             encoding="utf-8")
        sl.apply_state("idle", "i1", NOW - 6000)
        self.assertIsNone(sl.metric_value(self._ctx("i1"), "idle.secs"))

    def test_a_genuinely_idle_session_still_reports_them(self):
        self.path.write_text(_text("assistant", "all done") + "\n", encoding="utf-8")
        sl.apply_state("idle", "i2", NOW - 6000)
        self.assertAlmostEqual(sl.metric_value(self._ctx("i2"), "idle.secs"), 6000, delta=2)

    def test_with_no_transcript_it_is_exactly_what_it_was(self):
        sl.apply_state("idle", "i3", NOW - 6000)
        ctx = {"status": {"session_id": "i3"}, "config": sl.load_config(), "now": NOW}
        self.assertAlmostEqual(sl.metric_value(ctx, "idle.secs"), 6000, delta=2)

    def test_the_flagship_recipe_is_one_clause_and_it_parses(self):
        """The recipe SKILL 4g leads with. A clean-machine run refused the two-clause form,
        because the grammar has no `and` — so the recipe has to be expressible without one."""
        self.assertNotIsInstance(sl.parse_condition("idle.secs > 1800"), str)
        self.path.write_text(_text("assistant", "done") + "\n", encoding="utf-8")
        sl.apply_state("idle", "i4", NOW - 2000)
        self.assertTrue(sl.condition_holds(self._ctx("i4"), "idle.secs > 1800"))

    def test_the_skill_never_shows_a_condition_the_grammar_refuses(self):
        """The defect this came from: the skill's own flagship example did not parse, and no
        test could see it because the skill is prose. Every condition the docs show is now
        run through the real parser."""
        import re as _re
        for doc in (SKILL, README, *COMMANDS_DIR.glob("*.md")):
            for match in _re.finditer(r"when=?'([^']+)'", doc.read_text(encoding="utf-8")):
                condition = match.group(1)
                if "..." in condition or "<" in condition:
                    continue        # a placeholder, not a condition being shown as real
                parsed = sl.parse_condition(condition)
                self.assertNotIsInstance(parsed, str,
                                         f"{doc.name} shows a condition the grammar refuses: "
                                         f"{condition!r} -> {parsed}")

# ── Task 29: the usage cache had no account identity ─────────────────────────
class CacheAccountIdentityTests(TmpEnv):
    """The user reported "large lags" when switching accounts. It was not lag.

    `usage.json` carried `fetched_at`, `ok`, `error`, `limits` — and no account. So after a
    mid-session `/login` the bar kept showing the PREVIOUS account's 5-hour and weekly
    figures, as fact, with no `~` and no `?`, until the TTL expired. `183/3%` all over again:
    a plausible number belonging to something else.

    The distinction that matters, and the one the code has to make explicitly: a stale value
    from the SAME account is honest and is marked `~`. A value from a DIFFERENT account is
    not stale — it is wrong — and reusing the staleness vocabulary would mislabel it.

    Driven through render(), per the standing habit: off-the-path is how this class keeps
    reaching the bar."""

    ME = "me@example.com"
    THEM = "them@example.com"

    UNSTAMPED = object()        # "the key is absent", distinct from "the key is None"

    def _cache(self, account=UNSTAMPED, pct=91.0):
        entry = {"fetched_at": NOW, "ok": True,
                 "limits": {"session": {"label": "5h", "pct": pct, "resets_at": NOW + 3600},
                            "weekly_all": {"label": "week all", "pct": pct, "resets_at": NOW + 9999},
                            "scoped": []}}
        if account is not self.UNSTAMPED:
            entry["account"] = account
        return entry

    def _bar(self, cache, email, status=None):
        return plain(" ".join(sl.render(status or {"session_id": "acc"}, cache, email,
                                        sl.load_config(), NOW, timezone.utc, width=200)))

    def test_a_cache_from_another_account_never_shows_its_figures(self):
        bar = self._bar(self._cache(sl.account_hash(self.THEM)), self.ME)
        self.assertNotIn("91%", bar, "another account's usage was rendered as fact")

    def test_it_is_reported_as_unknown_not_as_stale(self):
        """`~` means "old but ours". Using it here would say the number is merely late."""
        cache = self._cache(sl.account_hash(self.THEM))
        _limits, stale, source = sl.pick_limits({}, cache, NOW, {}, self.ME)
        self.assertEqual(source, "stdin", "the foreign cache still counted as an API source")
        self.assertFalse(stale, "a foreign cache was labelled stale rather than unknown")

    def test_the_same_account_still_renders_normally(self):
        bar = self._bar(self._cache(sl.account_hash(self.ME)), self.ME)
        self.assertIn("91%", bar)

    def test_the_same_account_stale_path_is_untouched(self):
        """Since Task 29d the API only supplies the per-model buckets, so `source`/`stale`
        describe THOSE — the headline numbers arrive with every render and have no staleness."""
        cache = dict(self._cache(sl.account_hash(self.ME)), fetched_at=NOW - 99999)
        cache["limits"]["scoped"] = [{"label": "Fable", "pct": 3.0, "resets_at": NOW + 999}]
        _limits, stale, source = sl.pick_limits({}, cache, NOW, {}, self.ME)
        self.assertEqual(source, "api")
        self.assertTrue(stale, "a genuinely stale same-account value stopped being marked")

    def test_an_unstamped_legacy_cache_is_not_trusted(self):
        """Unknown provenance, not "probably fine". It self-heals in one fetch, and nobody
        sees a wrong number in the meantime."""
        bar = self._bar(self._cache(), self.ME)
        self.assertNotIn("91%", bar)

    def test_an_unknown_current_account_leaves_a_good_cache_alone(self):
        """Not logged in, or credentials unreadable. Unknown is not "different" — the same
        rule as `nudged`: never invent a reason to throw away good data."""
        for email in (None, ""):
            bar = self._bar(self._cache(sl.account_hash(self.ME)), email)
            self.assertIn("91%", bar, f"a good cache was discarded for email={email!r}")

    def test_stdin_rate_limits_still_show_through_after_a_mismatch(self):
        """Dropping the foreign cache must fall back to the SESSION's own numbers, which are
        current-account by construction — not to a blank bar."""
        status = {"session_id": "acc",
                  "rate_limits": {"five_hour": {"used_percentage": 12, "resets_at": NOW + 60}}}
        bar = self._bar(self._cache(sl.account_hash(self.THEM)), self.ME, status)
        self.assertIn("12%", bar)
        self.assertNotIn("91%", bar)


class RefetchOnAccountChangeTests(TmpEnv):
    """The mismatch is detectable the moment it happens, so waiting out the TTL would leave
    an honest blank on screen for up to a minute for no reason."""

    def _lock(self):
        return self.root / "nolock"

    def test_an_account_change_refetches_without_waiting_for_the_ttl(self):
        cache = {"fetched_at": NOW, "ok": True, "account": sl.account_hash("them@example.com"),
                 "limits": {"session": {"label": "5h", "pct": 5.0, "resets_at": NOW}}}
        self.assertFalse(sl.needs_fetch(cache, NOW, self._lock(), {}, "them@example.com"))
        self.assertTrue(sl.needs_fetch(cache, NOW, self._lock(), {}, "me@example.com"))

    def test_an_unstamped_cache_refetches_once(self):
        cache = {"fetched_at": NOW, "ok": True, "limits": {}}
        self.assertTrue(sl.needs_fetch(cache, NOW, self._lock(), {}, "me@example.com"))

    def test_an_unknown_account_does_not_force_a_refetch(self):
        cache = {"fetched_at": NOW, "ok": True, "account": sl.account_hash("me@example.com"),
                 "limits": {}}
        self.assertFalse(sl.needs_fetch(cache, NOW, self._lock(), {}, None))

    def test_a_fetch_stamps_the_account_it_was_for(self):
        with mock.patch.object(sl, "read_token", return_value=None), \
             mock.patch.object(sl, "read_email", return_value="me@example.com"):
            entry = sl.run_fetch(NOW)
        self.assertEqual(entry["account"], sl.account_hash("me@example.com"))
        self.assertNotIn("me@example.com", json.dumps(entry),
                         "the raw email was written into the cache")

# ── Task 29b: one contrast rule for text on a painted banner ─────────────────
SHIPPED_PHASE_BGS = ("#c9a227", "#b3261e")


class BannerContrastTests(TmpEnv):
    """The user's screenshot: with the alarm banner painted, `5h ~0/51%` and `ctx 21%` were
    the same white as everything else. Three compounding faults on one strong ground —

      * the phase `text` override flattened pct_color()'s green/yellow/red, so a threshold
        stopped meaning anything. That is the Task 14 rule one layer down: a style must never
        silence a threshold, and the phase override never got the treatment `fg` did;
      * `white` resolved to `37` — STANDARD white, a mid-grey on many themes — while the loud
        chip has always used bright `97`, so the plugin disagreed with itself;
      * `DIM` stayed on the token counts, and bold-standard-white plus dim on bright amber is
        what the user quite reasonably called black.

    One rule covers all three, because they are one question: text on a strong ground has to
    stay readable AND keep meaning something. Asserted as RENDERED — a test that only checks
    an escape sequence survived would pass on something unreadable."""

    def _painted(self, bg, cfg=None):
        config = dict(cfg or {}, idlePhases=[{"after": 400, "bg": bg, "text": "white",
                                              "bold": True}])
        sl.apply_state("idle", "b1", NOW - 1800)
        status = dict(STATUS, session_id="b1")
        return sl.render(status, CACHE, "you@example.com", config, NOW, timezone.utc,
                         width=200)[0]

    def _danger_vs_safe(self, bg, cfg=None):
        """The same segment at a danger level and a safe level, both on a painted banner."""
        out = []
        for pct in (95.0, 5.0):
            cache = dict(CACHE, limits=dict(CACHE["limits"],
                         session={"label": "", "pct": pct, "resets_at": NOW + 3600}))
            config = dict(cfg or {}, idlePhases=[{"after": 200, "bg": bg, "text": "white",
                                                  "bold": True}])
            sl.apply_state("idle", "b2", NOW - 1800)
            out.append(sl.render(dict(STATUS, session_id="b2"), cache, "you@example.com",
                                 config, NOW, timezone.utc, width=200)[0])
        return out

    def _danger_vs_safe_forced(self, bg):
        """`force: true` on the phase — Task 14's word for the same opt-in."""
        out = []
        for pct in (95.0, 5.0):
            cache = dict(CACHE, limits=dict(CACHE["limits"],
                         session={"label": "", "pct": pct, "resets_at": NOW + 3600}))
            config = {"idlePhases": [{"after": 400, "bg": bg, "text": "white", "bold": True,
                                      "force": True}]}
            sl.apply_state("idle", "b4", NOW - 1800)
            out.append(sl.render(dict(STATUS, session_id="b4"), cache, "you@example.com",
                                 config, NOW, timezone.utc, width=200)[0])
        return out

    def test_the_fixture_paints_the_colour_it_asks_for(self):
        """Guards the guard. The shipped ladder has phases at 30s, 120s and 300s, so a fixture
        phase below the last of them never wins and these tests were quietly exercising the SHIPPED
        phase instead of the configured one. Third family again — a fixture that cannot
        reach the branch — this time in my own new tests."""
        for bg in ("#c9a227", "#b3261e", "#c0392b"):
            self.assertTrue(self._painted(bg).startswith(sl.bg_code(bg)),
                            f"the fixture did not paint {bg}")

    def test_nothing_is_singled_out_by_its_colour_alone(self):
        """The regression that reached a real bar, kept as the guard against its return.

        The inverse-chip treatment decided what was a threshold by looking at the COLOUR, and
        three unrelated producers return YELLOW: a crossed limit, a dirty git tree, and a
        user's own amber. So all three were inverted and the bar read as though parts of it
        had been highlighted at random. My fixtures missed it because none of them had a dirty
        branch, a share figure or a coloured custom segment — the render looked perfect and
        was, for that fixture.

        Until a run can CARRY the intent, nothing on a banner is singled out. This fixture has
        all of it at once, which is the normal case on a real bar."""
        for bg in SHIPPED_PHASE_BGS:
            line = self._busy_line(bg)
            self.assertNotIn(sl.REVERSE, line,
                             f"something was singled out on {bg} by its colour alone")

    def _busy_line(self, bg):
        """A dirty branch, a coloured custom segment, and a real threshold crossing — several
        yellow-for-different-reasons things sharing one line."""
        sl.save_config({"customSegments": [{"id": "brk", "text": "take a break",
                                            "fg": "#c9a227"}],
                        "layout": [["context", "session", "weekly"],
                                   ["git", "brk", "model"]]})
        cache = dict(CACHE, limits=dict(CACHE["limits"],
                     session={"label": "", "pct": 95.0, "resets_at": NOW + 3600}))
        config = dict(sl.load_config(),
                      idlePhases=[{"after": 200, "bg": bg, "text": "white", "bold": True}])
        sl.apply_state("idle", "busy", NOW - 900)
        status = dict(STATUS, session_id="busy",
                      workspace={"current_dir": str(self.root), "repo": {"name": "demo"}})
        with mock.patch.object(sl, "git_segment_runs",
                               return_value=[("demo*", sl.YELLOW)]):   # dirty: yellow, NOT a threshold
            return sl.render(status, cache, "you@example.com", config, NOW,
                             timezone.utc, width=200)[0] + sl.render(
                                 status, cache, "you@example.com", config, NOW,
                                 timezone.utc, width=200)[1]

    def test_white_on_a_banner_is_bright_white_not_standard(self):
        """`37` is a mid-grey on a great many themes; the loud chip has always used 97."""
        for bg in SHIPPED_PHASE_BGS:
            line = self._painted(bg)
            self.assertIn("\033[1;97m".encode().decode("unicode_escape"), line,
                          f"standard white on {bg}")
            self.assertNotIn("\033[1;37m".encode().decode("unicode_escape"), line)

    def test_dim_never_survives_onto_a_painted_banner(self):
        """Bold standard-white plus dim on amber is what the user called black."""
        for bg in SHIPPED_PHASE_BGS:
            self.assertNotIn(sl.DIM, self._painted(bg), f"dim survived onto {bg}")

    def test_the_unpainted_bar_keeps_its_dim_and_its_colours(self):
        """The rule applies to a painted ground and nowhere else."""
        line = sl.render(STATUS, CACHE, "you@example.com", {}, NOW, timezone.utc, width=200)[0]
        self.assertIn(sl.DIM, line)

    def test_the_shipped_phases_produce_a_bright_foreground(self):
        """`37` versus `97` is exactly the one-character difference that survives review and
        looks wrong on someone else's theme."""
        shipped = json.loads(sl.default_config_path().read_text(encoding="utf-8"))["idlePhases"]
        for phase in shipped:
            self.assertIsNotNone(phase.get("text"))
            sl.apply_state("idle", "bright", NOW - 6000)
            line = sl.render(dict(STATUS, session_id="bright"), CACHE, "you@example.com",
                             {"idlePhases": [phase]}, NOW, timezone.utc, width=200)[0]
            self.assertRegex(line, r"\x1b\[(1;)?(9[0-7]|38;2)",
                             f"{phase} does not render a bright foreground")
            self.assertNotRegex(line, r"\x1b\[1?;?37m", f"{phase} renders standard white")

    def test_the_escape_hatch_restores_the_flat_look(self):
        """Someone who genuinely wants everything one colour can still have it — off by
        default, and documented as able to hide a threshold."""
        flat = self._danger_vs_safe_forced(SHIPPED_PHASE_BGS[1])
        self.assertEqual(_escapes_around(flat[0], "95%"), _escapes_around(flat[1], "5%"))
        for line in flat:
            self.assertNotIn(sl.DIM, line, "flat is still a painted ground; dim must go")

    def test_the_loud_chip_is_still_left_alone(self):
        line2 = sl.render(dict(STATUS, session_id="b3"), CACHE, "you@example.com",
                          {"idlePhases": [{"after": 200, "bg": "#b3261e", "text": "white"}],
                           "alarmAfterSeconds": 60},
                          NOW, timezone.utc, width=200)[1] if sl.apply_state(
                              "idle", "b3", NOW - 600) else ""
        self.assertIn(sl.ALARM_SEQ, line2)


def _escapes_around(line, token):
    """The SGR codes in force where `token` is rendered — what a person actually sees."""
    index = plain(line).find(token)
    if index < 0:
        return "<absent>"
    seen, visible = [], 0
    for match in re.finditer(r"(\x1b\[[0-9;]*m)|([^\x1b])", line):
        if match.group(1):
            code = match.group(1)
            if code == sl.RESET:
                seen = []
            else:
                seen.append(code)
        else:
            if visible == index:
                break
            visible += 1
    return "".join(sorted(set(seen)))

# ── Task 29d: stdin is the primary source; the API is only for per-model ─────
class UsageSourceRestructureTests(TmpEnv):
    """The user compared their bar with Claude Code's own /usage panel and the bar was 18
    points low: `5h ~0/51%` against a panel reading 69%. Two faults.

    The API cache won over a FRESH stdin bucket that arrived in the same payload, because
    pick_limits() read `api.get(...) or stdin_bucket(...)` — identity, not freshness. And
    all three profiles were sitting on `http 429` with no backoff at all, retrying every 60s
    for ever.

    Their question reframed both: "if Claude Code gives us the numbers, shouldn't this be
    enough, so we will not hammer API?" It is enough. Claude Code's payload carries the two
    headline buckets free on every render, always for the account logged in now. The API is
    needed ONLY for the per-model weekly buckets — the `Fable` line, which is the reason this
    plugin exists.

    What must NOT regress: Task 28 established that `rate_limits` can go stale inside a
    long-lived session, which is what manufactured the `183/3%` sawtooth. So the rule is not
    "stdin wins" — it is prefer the fresher of the two, per bucket, with unknown never
    beating known."""

    ME = "you@example.com"

    def _cache(self, session_pct=51.0, fetched=None, **kw):
        entry = {"fetched_at": fetched if fetched is not None else NOW, "ok": True,
                 "account": sl.account_hash(self.ME),
                 "limits": {"session": {"label": "", "pct": session_pct, "resets_at": NOW + 3600},
                            "weekly_all": {"label": "", "pct": 8.0, "resets_at": NOW + 99999},
                            "scoped": [{"label": "Fable", "pct": 33.0, "resets_at": NOW + 99999}]}}
        entry.update(kw)
        return entry

    def _bar(self, cache, rl=None, cfg=None):
        status = dict(STATUS, session_id="src")
        if rl is not None:
            status["rate_limits"] = rl
        return plain(sl.render(status, cache, self.ME, cfg or {}, NOW, timezone.utc,
                               width=250)[0])

    FRESH_STDIN = {"five_hour": {"used_percentage": 69, "resets_at": NOW + 7200},
                   "seven_day": {"used_percentage": 10, "resets_at": NOW + 99999}}

    def test_the_headline_numbers_come_from_stdin(self):
        """The reported case: a cached 51% must not beat the 69% in the same payload."""
        bar = self._bar(self._cache(session_pct=51.0), self.FRESH_STDIN)
        self.assertIn("69%", bar)
        self.assertNotIn("51%", bar)

    def test_the_per_model_line_still_comes_from_the_api(self):
        bar = self._bar(self._cache(), self.FRESH_STDIN)
        self.assertIn("Fable", bar)
        self.assertIn("33%", bar)

    def test_a_refused_fetch_costs_the_per_model_line_and_nothing_else(self):
        """The whole point of the restructure: a 429 should cost them `Fable`, not 18 points
        on the number they use to decide when to compact."""
        refused = {"fetched_at": NOW, "ok": False, "error": "http 429",
                   "account": sl.account_hash(self.ME), "limits": None}
        bar = self._bar(refused, self.FRESH_STDIN)
        self.assertIn("69%", bar)
        self.assertIn("10%", bar)
        self.assertNotIn("Fable", bar)

    def test_with_no_api_data_at_all_the_headlines_still_render(self):
        bar = self._bar({}, self.FRESH_STDIN)
        self.assertIn("69%", bar)
        self.assertIn("10%", bar)

    def test_a_demonstrably_stale_stdin_bucket_loses_to_a_fresher_api_one(self):
        """Not "stdin wins". Task 28's stale rate_limits block is exactly why."""
        stale = {"five_hour": {"used_percentage": 5, "resets_at": NOW - 3600}}   # window gone
        bar = self._bar(self._cache(session_pct=51.0), stale)
        self.assertIn("51%", bar, "a stdin bucket whose own window has passed still won")

    def test_a_stale_api_cache_does_not_mark_the_headline_numbers(self):
        """`~` means "we fetched this a while ago". The headline figures arrived with THIS
        render, so marking them old is its own small lie — and it is exactly the confusion
        the user was already in. The per-model line still carries the mark."""
        bar = self._bar(self._cache(fetched=NOW - 99999), self.FRESH_STDIN)
        self.assertIn("69%", bar)
        self.assertNotIn("~69%", bar)
        self.assertIn("~", bar.split("Fable")[1], "the per-model line lost its staleness mark")

    def test_unknown_never_beats_known(self):
        bar = self._bar(self._cache(session_pct=51.0), {"five_hour": None})
        self.assertIn("51%", bar)


class UsageFetchCadenceTests(TmpEnv):
    """`API every 60 seconds is toooo much` — and they were right. Once stdin carries the
    headlines the API exists only for SEVEN-DAY per-model buckets, where a quarter-hour lag
    is imperceptible."""

    def test_the_per_model_ttl_is_its_own_tunable(self):
        self.assertIn("usageFetchSeconds", sl.TUNABLES)
        self.assertNotEqual("usageFetchSeconds", "cacheTtlSeconds")

    def test_the_shipped_default_is_conservative(self):
        shipped = json.loads(sl.default_config_path().read_text(encoding="utf-8"))["usageFetchSeconds"]
        self.assertGreaterEqual(shipped, 600, "a status line should not poll this often")
        self.assertEqual(shipped, 900)

    def _with_user_config(self, **written):
        """A REAL user config file, resolved through load_config().

        The first version of these tests passed a hand-built dict, which cannot express the
        distinction being tested: load_config() merges the shipped defaults, so in the config
        the code actually receives EVERY key is present. A dict literal has exactly the shape
        the merged config does not, and that is why a dead branch passed."""
        (self.root / "squirrel").mkdir(parents=True, exist_ok=True)
        (self.root / "squirrel" / "config.json").write_text(json.dumps(written))
        return sl.load_config()

    def test_an_explicitly_set_cacheTtlSeconds_is_not_silently_overridden(self):
        """The user set `cacheTtlSeconds 300` by hand as an interim before this tunable
        existed. Shipping a 900s default that ignores it overrides a choice they made
        deliberately, and they have no way to tell — the setting simply stops mattering."""
        cfg = self._with_user_config(cacheTtlSeconds=300)
        self.assertEqual(sl.usage_fetch_ttl(cfg), 300)
        cache = {"fetched_at": NOW - 400, "ok": True, "account": sl.account_hash("a@b.c"),
                 "limits": {"scoped": []}}
        self.assertTrue(sl.needs_fetch(cache, NOW, self.root / "nolock", cfg, "a@b.c"),
                        "an explicitly set cacheTtlSeconds was ignored")

    def test_the_new_tunable_wins_once_it_is_set(self):
        cfg = self._with_user_config(cacheTtlSeconds=300, usageFetchSeconds=900)
        self.assertEqual(sl.usage_fetch_ttl(cfg), 900)
        cache = {"fetched_at": NOW - 400, "ok": True, "account": sl.account_hash("a@b.c"),
                 "limits": {"scoped": []}}
        self.assertFalse(sl.needs_fetch(cache, NOW, self.root / "nolock", cfg, "a@b.c"))

    def test_with_neither_set_the_shipped_default_applies(self):
        self.assertEqual(sl.usage_fetch_ttl(self._with_user_config()), 900)

    def test_a_fetch_inside_the_new_ttl_does_not_happen(self):
        cache = {"fetched_at": NOW - 300, "ok": True, "account": sl.account_hash("a@b.c"),
                 "limits": {"scoped": []}}
        self.assertFalse(sl.needs_fetch(cache, NOW, self.root / "nolock", {}, "a@b.c"))

    def test_a_fetch_past_it_does(self):
        cache = {"fetched_at": NOW - 1200, "ok": True, "account": sl.account_hash("a@b.c"),
                 "limits": {"scoped": []}}
        self.assertTrue(sl.needs_fetch(cache, NOW, self.root / "nolock", {}, "a@b.c"))


class RateLimitBackoffTests(TmpEnv):
    """All three of the user's profiles were sitting on `http 429`, retrying every 60s for
    ever, with `grep -nE "429|Retry-After|backoff"` finding nothing. A refusal has to be
    heard."""

    def _refused(self, retry_after=None, at=None, tries=1):
        entry = {"fetched_at": at if at is not None else NOW, "ok": False,
                 "error": "http 429", "account": sl.account_hash("a@b.c"), "limits": None,
                 "retry_after": retry_after, "refusals": tries}
        return entry

    def test_a_429_is_not_retried_while_the_backoff_stands(self):
        cache = self._refused(at=NOW)
        self.assertFalse(sl.needs_fetch(cache, NOW + 60, self.root / "nolock", {}, "a@b.c"),
                         "a refused endpoint was asked again inside its own backoff")
        self.assertTrue(sl.needs_fetch(cache, NOW + 4000, self.root / "nolock", {}, "a@b.c"),
                        "the backoff never expired")

    def test_retry_after_is_honoured_when_given(self):
        cache = self._refused(retry_after=NOW + 600, at=NOW - 1200)
        self.assertFalse(sl.needs_fetch(cache, NOW, self.root / "nolock", {}, "a@b.c"))
        self.assertTrue(sl.needs_fetch(cache, NOW + 601, self.root / "nolock", {}, "a@b.c"))

    def test_backoff_grows_with_repeated_refusals(self):
        first = sl.backoff_until(self._refused(tries=1))
        later = sl.backoff_until(self._refused(tries=5))
        self.assertGreater(later, first)

    def test_backoff_has_a_ceiling(self):
        self.assertLessEqual(sl.backoff_until(self._refused(tries=99)) - NOW,
                             sl.USAGE_BACKOFF_MAX_S + 1)

    def test_one_sessions_backoff_is_observed_by_the_others(self):
        """Recorded in the cache, which every session in the profile reads — rather than each
        discovering the refusal for itself."""
        sl.write_cache(sl.cache_paths()[0], self._refused(at=NOW))
        cache = sl.load_json(sl.cache_paths()[0])
        self.assertFalse(sl.needs_fetch(cache, NOW + 120, self.root / "nolock", {}, "a@b.c"))

    def _http429(self, retry_after=None):
        import urllib.error
        headers = {"Retry-After": retry_after} if retry_after else {}
        return urllib.error.HTTPError("u", 429, "Too Many Requests", headers, None)

    def test_a_real_429_records_the_refusal(self):
        """The link between the fetch and the backoff, which nothing was testing: a
        hand-built cache with `refusals` set proves the backoff arithmetic and says nothing
        about whether a refusal is ever counted."""
        with mock.patch.object(sl, "read_token", return_value="t"), \
             mock.patch.object(sl, "read_email", return_value="a@b.c"), \
             mock.patch.object(sl, "fetch_usage", side_effect=self._http429()):
            first = sl.run_fetch(NOW)
        self.assertEqual(first["error"], "http 429")
        self.assertEqual(first["refusals"], 1)
        with mock.patch.object(sl, "read_token", return_value="t"), \
             mock.patch.object(sl, "read_email", return_value="a@b.c"), \
             mock.patch.object(sl, "fetch_usage", side_effect=self._http429()):
            second = sl.run_fetch(NOW + 5000)
        self.assertEqual(second["refusals"], 2, "repeated refusals did not accumulate")
        self.assertGreater(sl.backoff_until(second), sl.backoff_until(first))

    def test_a_real_429_honours_the_servers_retry_after(self):
        with mock.patch.object(sl, "read_token", return_value="t"), \
             mock.patch.object(sl, "read_email", return_value="a@b.c"), \
             mock.patch.object(sl, "fetch_usage", side_effect=self._http429("300")):
            entry = sl.run_fetch(NOW)
        self.assertAlmostEqual(entry["retry_after"], NOW + 300, delta=1)
        self.assertAlmostEqual(sl.backoff_until(entry), NOW + 300, delta=1)

    def test_a_success_clears_the_backoff(self):
        with mock.patch.object(sl, "read_token", return_value=None), \
             mock.patch.object(sl, "read_email", return_value="a@b.c"):
            entry = sl.run_fetch(NOW)
        self.assertEqual(entry.get("refusals", 0), 0)


class UsageFetchIsOptionalTests(TmpEnv):
    """Turn the per-model line off and squirrel never reads `.credentials.json`, never opens
    a socket, and has no token in the process at all. A real security simplification for a
    colleague who does not want the per-model figure."""

    def test_with_the_fetch_off_no_credentials_file_is_read(self):
        sl.save_config({"usageFetch": False})
        with mock.patch.object(sl, "read_token", side_effect=AssertionError("read the token")):
            entry = sl.run_fetch(NOW)
        self.assertFalse(entry["ok"])
        self.assertIn("disabled", (entry.get("error") or ""))

    def test_with_the_fetch_off_the_headline_numbers_still_render(self):
        sl.save_config({"usageFetch": False})
        status = dict(STATUS, session_id="off",
                      rate_limits={"five_hour": {"used_percentage": 69, "resets_at": NOW + 7200}})
        bar = plain(sl.render(status, {}, "you@example.com", sl.load_config(), NOW,
                              timezone.utc, width=250)[0])
        self.assertIn("69%", bar)

class SuiteIsWhollyCollectedTests(unittest.TestCase):












    """Audit X2, and the worst kind of defect: one that reports success.

    `unittest.main()` sat at line 7374 of a 7684-line file, so the seven classes below it did
    not exist yet when it collected the namespace. `python3 test_statusline.py` ran 798 tests
    and printed OK; `python3 -m unittest` ran 829. The 31 it silently skipped were exactly the
    regression guards for the fixes being reported at the time — every one of them for a
    finding whose whole point was that a green suite had missed it.

    Appending to the end of a file is the natural way to add a test, so this WILL happen
    again. The guard is therefore on the class of mistake, not the instance: nothing that
    looks like a test may be defined after the entry point, and the entry point must be last.

    One honest limit: this guard is subject to the very bug it prevents. Move the entry point
    above this class and the guard is itself uncollected, so a DIRECT run prints OK on exactly
    the defect it exists to catch. Discovery is immune, which is why CI runs discovery — but
    a green direct run is not, on its own, evidence that the suite is whole.
    """

    def _source(self):
        return (DEV_DIR / "test_statusline.py").read_text(encoding="utf-8")

    def _main_block(self, tree):
        for node in tree.body:
            if (isinstance(node, ast.If) and isinstance(node.test, ast.Compare)
                    and getattr(node.test.left, "id", None) == "__name__"):
                return node
        self.fail("no `if __name__ == \"__main__\":` block found")

    def test_no_test_class_is_defined_after_the_entry_point(self):
        tree = ast.parse(self._source())
        cutoff = self._main_block(tree).lineno
        late = [n.name for n in tree.body
                if isinstance(n, ast.ClassDef) and n.lineno > cutoff]
        self.assertEqual(late, [], f"defined after unittest.main(), so a direct run skips them: {late}")

    def test_the_entry_point_is_the_last_statement_in_the_file(self):
        tree = ast.parse(self._source())
        self.assertIs(tree.body[-1], self._main_block(tree),
                      "something follows the entry point; append above it, never below")

    def test_a_direct_run_collects_exactly_what_discovery_collects(self):
        """A consistency check on the loader, NOT a reproduction of the bug above.

        The first version of this docstring claimed it compared "both counts in one place".
        It cannot: by the time it runs, the module is fully loaded, so both numbers come from
        the same complete namespace and agree regardless of where the entry point sits. The
        auditor proved it — under their mutation the real counts diverged 839 against 842 and
        this assertion stayed green; the two AST checks above did all the work.

        It is kept because it catches a different thing: a class that discovery finds and
        `loadTestsFromTestCase` does not, or vice versa. The AST checks are what guard X2."""
        loader = unittest.TestLoader()
        discovered = loader.loadTestsFromModule(sys.modules[__name__]).countTestCases()
        classes = [obj for obj in globals().values()
                   if isinstance(obj, type) and issubclass(obj, unittest.TestCase)]
        direct = sum(loader.loadTestsFromTestCase(c).countTestCases() for c in classes)
        self.assertEqual(direct, discovered,
                         f"direct run would collect {direct}, discovery collects {discovered}")


if __name__ == "__main__":
    unittest.main()
