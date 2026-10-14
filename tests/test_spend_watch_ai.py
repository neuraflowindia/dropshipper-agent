"""AI usage windows (flag only) and the Codex paid-credits money guards.

A small stand-in program plays the app-server protocol; the real Codex program,
account and model are never used. Readings carry a fictional account id and
Codex folder to prove neither is kept.
"""

from contextlib import redirect_stdout
from datetime import datetime, timedelta, timezone
import io
import json
import os
from pathlib import Path
import stat
import subprocess
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
from product_radar.models import SafetyError
from product_radar.runtime_storage import runtime_path
from product_radar.spend_watch import AREA, allowance_summary, load_settings, load_state
from product_radar.spend_watch_ai import (AllowanceWatchedRunner, _stop, ai_paid_credit_wait, credits_drawable,
                                          limit_reached, parse_rate_limits, read_rate_limits, research_codex_runner,
                                          watched_codex_runner)

PRIVATE_ACCOUNT = "account-private-fixture-0000"
PRIVATE_HOME = "/home/private-fixture/.codex"
NOW = datetime(2026, 10, 14, 10, 0, tzinfo=timezone.utc)
WEEK_RESET = datetime(2026, 10, 20, 5, 4, 55, tzinfo=timezone.utc)


def reply(percent=43, balance="59915.0366570000", *, reset=WEEK_RESET, secondary=None, by_limit=True,
          allowed=True, reached=None, has_credits=True):
    bucket = {"limitId": "codex", "limitName": None, "normalModelSlug": None,
              "primary": {"usedPercent": percent, "windowDurationMins": 10080,
                          "resetsAt": int(reset.timestamp()) if reset else None},
              "secondary": secondary, "individualLimit": None, "spendControlReached": False, "planType": "plus",
              "rateLimitReachedType": reached,
              "credits": None if balance is False else {"hasCredits": has_credits, "unlimited": False, "balance": balance}}
    result = {"ordinaryUsageAllowed": allowed, "rateLimits": bucket,
              "rateLimitResetCredits": {"availableCount": 2, "credits": None},
              "accountId": PRIVATE_ACCOUNT, "rateLimitUpsell": None}
    if by_limit:
        result["rateLimitsByLimitId"] = {"codex": bucket}
    return {"id": 2, "result": result}


FAKE_SERVER = r'''
import json, os, sys, time
mode = os.environ.get("FAKE_MODE", "ok")
if mode == "exit":
    sys.exit(0)
for line in sys.stdin:
    message = json.loads(line)
    if message.get("id") == 1:
        if mode == "silent":
            time.sleep(30)
        print(json.dumps({"method": "configWarning", "params": {}}))
        print(json.dumps({"id": 1, "result": {"codexHome": %(home)r, "userAgent": "fixture"}}))
    elif message.get("id") == 2:
        assert message["method"] == "account/rateLimits/read", message
        if mode == "error":
            print(json.dumps({"id": 2, "error": {"code": -32600, "message": "not signed in"}}))
        elif mode == "garbage":
            print("not json")
        elif mode == "huge":
            sys.stdout.write("x" * (2 * 1024 * 1024))
        else:
            if mode == "request":  # a server request reusing the id must not be taken as the reply
                print(json.dumps({"id": 2, "method": "item/tool/requestUserInput", "params": {}}))
            print(json.dumps(%(reply)s))
    sys.stdout.flush()
'''


class Clock:
    def __init__(self, at=NOW):
        self.at = at

    def __call__(self):
        return self.at.isoformat()


class Reader:
    """Scripted readings: each item is a reply dict or an exception; the last one repeats."""
    def __init__(self, *items):
        self.items, self.calls = list(items), 0

    def __call__(self):
        self.calls += 1
        item = self.items.pop(0) if len(self.items) > 1 else self.items[0]
        if isinstance(item, Exception):
            raise item
        return parse_rate_limits(item)


class Inner:
    identity = "codex-chatgpt-high-v1"

    def __init__(self, error=None):
        self.calls, self.error, self.checks = 0, error, 0

    def check_new_execution(self):
        self.checks += 1

    def __call__(self, role, payload, folder):
        self.calls += 1
        if self.error is not None:
            raise self.error
        return {"items": []}, None


class RateLimitReadingTests(unittest.TestCase):
    def test_live_shaped_reply_keeps_windows_signals_plan_and_balance_only(self):
        parsed = parse_rate_limits(reply())
        self.assertEqual(parsed, {"windows": [{"key": "codex-10080", "limit_id": "codex", "slot": "primary",
            "duration_minutes": 10080, "used_percent": 43, "resets_at": WEEK_RESET.isoformat()}],
            "credits": {"balance": "59915.036657", "has_credits": True, "unlimited": False},
            "plan_type": "plus", "reached": [], "ordinary_usage_allowed": True})
        self.assertNotIn(PRIVATE_ACCOUNT, json.dumps(parsed))
        self.assertFalse(limit_reached(parsed))

    def test_every_reported_window_is_read_without_assuming_a_five_hour_limit(self):
        five = {"usedPercent": 80.0, "windowDurationMins": 300, "resetsAt": int((NOW + timedelta(hours=2)).timestamp())}
        parsed = parse_rate_limits(reply(24.0, secondary=five, by_limit=False))
        self.assertEqual([(w["key"], w["used_percent"]) for w in parsed["windows"]], [("codex-10080", 24), ("codex-300", 80)])
        other = reply()
        other["result"]["rateLimitsByLimitId"]["other_model"] = dict(other["result"]["rateLimits"], limitId="other_model",
            credits=None, rateLimitReachedType="rate_limit_reached",
            primary={"usedPercent": 5, "windowDurationMins": None, "resetsAt": None})
        parsed = parse_rate_limits(other)
        self.assertEqual([w["key"] for w in parsed["windows"]], ["codex-10080", "other_model-primary"])
        self.assertEqual(parsed["reached"], ["other_model:rate_limit_reached"])
        self.assertTrue(limit_reached(parsed))
        self.assertEqual(parse_rate_limits(reply(balance="0.000"))["credits"]["balance"], "0")
        self.assertIsNone(parse_rate_limits(reply(balance=False))["credits"])

    def test_limit_and_credit_signals(self):
        self.assertTrue(limit_reached(parse_rate_limits(reply(allowed=False))))
        self.assertTrue(limit_reached(parse_rate_limits(reply(100))))
        self.assertFalse(limit_reached(parse_rate_limits(reply(99, allowed=None))))
        for value, drawable in ((reply(), True), (reply(balance=False), True), (reply(balance=None), True),
                                (reply(balance="0", has_credits=False), False), (reply(balance=None, has_credits=False), False),
                                (reply(balance="3", has_credits=False), True)):
            self.assertEqual(credits_drawable(parse_rate_limits(value)), drawable)
        unlimited = reply(has_credits=False, balance=None)
        unlimited["result"]["rateLimitsByLimitId"]["codex"]["credits"]["unlimited"] = True
        self.assertTrue(credits_drawable(parse_rate_limits(unlimited)))

    def test_malformed_replies_fail_closed(self):
        broken = []
        for change in (lambda r: r.pop("result"), lambda r: r["result"].pop("rateLimits"),
                       lambda r: r["result"]["rateLimitsByLimitId"].update({"bad id!": {}}),
                       lambda r: r["result"]["rateLimitsByLimitId"]["codex"].update(primary={"usedPercent": "43"}),
                       lambda r: r["result"]["rateLimitsByLimitId"]["codex"].update(primary={"usedPercent": 24.5}),
                       lambda r: r["result"]["rateLimitsByLimitId"]["codex"].update(primary={"usedPercent": -1}),
                       lambda r: r["result"]["rateLimitsByLimitId"]["codex"].update(primary={"usedPercent": 1, "windowDurationMins": 0}),
                       lambda r: r["result"]["rateLimitsByLimitId"]["codex"].update(primary={"usedPercent": 1, "resetsAt": "soon"}),
                       lambda r: r["result"]["rateLimitsByLimitId"]["codex"].update(rateLimitReachedType="Reached!"),
                       lambda r: r["result"]["rateLimitsByLimitId"]["codex"].update(credits={"hasCredits": "yes", "unlimited": False}),
                       lambda r: r["result"]["rateLimitsByLimitId"]["codex"].update(credits={"hasCredits": True, "unlimited": False, "balance": "-1"}),
                       lambda r: r["result"]["rateLimitsByLimitId"]["codex"].update(credits={"hasCredits": True, "unlimited": False, "balance": "lots"}),
                       lambda r: r["result"]["rateLimitsByLimitId"]["codex"].update(credits={"hasCredits": True, "unlimited": False, "balance": 5}),
                       lambda r: r["result"].update(rateLimitsByLimitId={str(i): {} for i in range(21)})):
            value = reply(); change(value); broken.append(value)
        for value in broken + [None, []]:
            with self.subTest(value=str(value)[:60]), self.assertRaisesRegex(SafetyError, "AI_USAGE_READ_INVALID"):
                parse_rate_limits(value)


class AppServerReaderTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="fake-codex-")
        self.addCleanup(temporary.cleanup)
        server = Path(temporary.name) / "server.py"
        server.write_text(FAKE_SERVER % {"home": PRIVATE_HOME, "reply": repr(reply())})
        self.binary = Path(temporary.name) / "codex"  # a launcher, since the interpreter path may contain spaces
        self.binary.write_text('#!/bin/sh\nexec "%s" "%s" "$@"\n' % (sys.executable, server))
        self.binary.chmod(self.binary.stat().st_mode | stat.S_IXUSR)

    def read(self, mode, **options):
        return read_rate_limits(str(self.binary), env={"FAKE_MODE": mode}, **options)

    def test_initialize_then_one_usage_read(self):
        for mode in ("ok", "request"):
            parsed = self.read(mode)
            self.assertEqual(parsed["windows"][0]["used_percent"], 43)
            self.assertNotIn(PRIVATE_HOME, json.dumps(parsed))

    def test_failures_are_fixed_codes(self):
        for mode, code, options in (("error", "AI_USAGE_READ_REJECTED", {}), ("exit", "AI_USAGE_READ_FAILED", {}),
                                    ("garbage", "JSON_RESPONSE_INVALID", {}), ("huge", "AI_USAGE_READ_INVALID", {}),
                                    ("silent", "AI_USAGE_READ_TIMEOUT", {"seconds": 1})):
            with self.subTest(mode=mode), self.assertRaisesRegex(SafetyError, code):
                self.read(mode, **options)
        with self.assertRaisesRegex(SafetyError, "AI_CODEX_NOT_INSTALLED"):
            read_rate_limits("codex")
        with self.assertRaisesRegex(SafetyError, "AI_USAGE_READ_FAILED"):
            read_rate_limits(str(self.binary.parent / "missing"))

    def test_stop_escalates_and_an_unreaped_process_is_a_failed_read(self):
        signals = []
        with patch("product_radar.spend_watch_ai.os.killpg", side_effect=lambda pid, sent: signals.append(sent)):
            stubborn = Mock(pid=4242)
            stubborn.wait.side_effect = subprocess.TimeoutExpired("codex", 5)
            with self.assertRaisesRegex(SafetyError, "AI_USAGE_READ_FAILED"):
                _stop(stubborn, wait_seconds=0)
            self.assertEqual(len(signals), 2)
            stubborn.stdout.close.assert_called_once_with()
            late = Mock(pid=4243)
            late.wait.side_effect = [subprocess.TimeoutExpired("codex", 5), 0]
            late.stdin.close.side_effect = OSError()
            _stop(late, wait_seconds=0)
            self.assertEqual(len(signals), 4)
        with patch("product_radar.spend_watch_ai.os.killpg", side_effect=ProcessLookupError()):
            gone = Mock(pid=4244)
            _stop(gone)
            gone.wait.assert_called_once()

    def test_research_runner_reader_uses_the_configured_program_and_shared_environment(self):
        from product_radar.research_ai import CODEX_ENVIRONMENT, codex_environment
        with tempfile.TemporaryDirectory() as root, patch.dict(os.environ, {"SECRET_TOKEN": "x"}):
            self.assertNotIn("SECRET_TOKEN", codex_environment())
            self.assertIn("CODEX_HOME", CODEX_ENVIRONMENT)
            watched = watched_codex_runner(Inner(), {"executable": str(self.binary)}, root=root)
            self.assertEqual(watched.reader()["plan_type"], "plus")
            self.assertEqual(watched.identity, "codex-chatgpt-high-v1")

    def test_one_factory_builds_every_live_runner_behind_the_guard(self):
        with tempfile.TemporaryDirectory() as root, patch("product_radar.research_ai.CodexRunner", return_value=Inner()) as codex:
            runner = research_codex_runner({"fixture": True}, root=root)
        codex.assert_called_once_with({"fixture": True}, stop_on_interrupt=False)
        self.assertIsInstance(runner, AllowanceWatchedRunner)
        for script in ("benchmark_providers", "build_review_package", "check_visual_luna_connection",
                       "complete_practical_reviews", "complete_source_interpretations", "run_programme",
                       "check_codex_luna_connection"):
            text = (Path(__file__).resolve().parents[1] / "scripts" / (script + ".py")).read_text()
            self.assertNotIn("CodexRunner(", text, script)
            # The probe keeps its own pinned --ephemeral run, wrapped by the same guard.
            self.assertIn("watched_codex_runner(" if script == "check_codex_luna_connection" else "research_codex_runner(",
                          text, script)


class MoneyGuardTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="spend-watch-ai-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.clock = Clock()
        self.settings = load_settings()
        self.sleeps = []

    def runner(self, reader, inner=None):
        return AllowanceWatchedRunner(inner or Inner(), root=self.root, reader=reader, clock=self.clock,
                                      settings=self.settings, sleep=self.sleeps.append)

    def alerts(self, kind=None):
        rows = allowance_summary(self.root, settings=self.settings, alert_limit=200)["alerts"]
        return [row for row in rows if kind is None or row["kind"] == kind]

    def ai(self):
        return load_state(self.root)["ai"]

    def test_windows_below_the_limit_only_flag(self):
        reader, inner = Reader(reply(49), reply(50), reply(76), reply(91)), Inner()
        watched = self.runner(reader, inner)
        for _ in range(4):
            watched._cached = None
            self.assertEqual(watched("E", {"items": []}, self.root), ({"items": []}, None))
        self.assertEqual(inner.calls, 4)
        item = load_state(self.root)["items"]["ai-codex-10080"]
        self.assertEqual((item["flags"], item["percent"], item["label"]), ([50, 75, 90], 91, "AI weekly allowance"))
        self.assertEqual([a["threshold"] for a in self.alerts("threshold")], [50, 75, 90])
        self.assertIsNone(ai_paid_credit_wait(self.root))
        self.assertFalse(allowance_summary(self.root, settings=self.settings)["ai_paused"])

    def test_plan_limit_with_credits_refuses_before_the_call_until_ordinary_use_returns(self):
        for limited in (reply(100), reply(60, allowed=False), reply(60, reached="rate_limit_reached")):
            with self.subTest(limited=str(limited)[:40]):
                for path in runtime_path(self.root, AREA).glob("**/*"):
                    if path.is_file():
                        path.unlink()
                self.clock.at = NOW
                inner = Inner()
                watched = self.runner(Reader(limited), inner)
                with self.assertRaisesRegex(SafetyError, "AI_PLAN_LIMIT_CREDITS_PROTECTED"):
                    watched.check_new_execution()
                with self.assertRaisesRegex(SafetyError, "AI_PLAN_LIMIT_CREDITS_PROTECTED"):
                    watched("E", {"items": []}, self.root)
                self.assertEqual(inner.calls, 0)  # nothing ran, so no credits could be drawn
                wait = ai_paid_credit_wait(self.root)
                self.assertEqual((wait["reason"], wait["until"]), ("AI_PLAN_LIMIT_CREDITS_PROTECTED", WEEK_RESET.isoformat()))
        line = next(l["text"] for l in allowance_summary(self.root, settings=self.settings)["lines"] if l["item"] == "codex-credits")
        self.assertIn("AI waiting so it doesn't spend paid Codex credits (plan limit reached; resumes at 20 Oct 2026, 05:04 UTC)", line)
        self.assertIn("AI waiting so it doesn't spend paid Codex credits", self.alerts("paused")[-1]["message"])
        reader = Reader(reply(0, allowed=None, reset=WEEK_RESET + timedelta(days=7)))
        watched = self.runner(reader)
        self.clock.at = WEEK_RESET + timedelta(seconds=1)
        with self.assertRaisesRegex(SafetyError, "AI_PLAN_LIMIT_CREDITS_PROTECTED"):
            watched.check_new_execution()  # percentages alone never prove recovery
        self.assertEqual(ai_paid_credit_wait(self.root)["until"], (WEEK_RESET + timedelta(days=7)).isoformat())
        self.clock.at = WEEK_RESET + timedelta(days=7, seconds=1)
        reader.items = [reply(0, reset=WEEK_RESET + timedelta(days=14))]
        watched.check_new_execution()
        self.assertIsNone(ai_paid_credit_wait(self.root))
        self.assertEqual(self.alerts()[-1]["kind"], "resumed")

    def test_plan_limit_without_any_credits_lets_research_run(self):
        inner = Inner()
        self.runner(Reader(reply(100, "0", has_credits=False)), inner)("E", {"items": []}, self.root)
        self.assertEqual(inner.calls, 1)
        self.assertIsNone(ai_paid_credit_wait(self.root))

    def test_full_window_reset_is_the_latest_full_window_or_an_hour_recheck(self):
        five = {"usedPercent": 100, "windowDurationMins": 300, "resetsAt": int((NOW + timedelta(hours=2)).timestamp())}
        with self.assertRaisesRegex(SafetyError, "AI_PLAN_LIMIT_CREDITS_PROTECTED"):
            self.runner(Reader(reply(100, secondary=five))).check_new_execution()
        self.assertEqual(ai_paid_credit_wait(self.root)["until"], WEEK_RESET.isoformat())
        self.clock.at = WEEK_RESET + timedelta(seconds=1)
        with self.assertRaisesRegex(SafetyError, "AI_PLAN_LIMIT_CREDITS_PROTECTED"):
            self.runner(Reader(reply(60, allowed=False, reset=None))).check_new_execution()
        self.assertEqual(ai_paid_credit_wait(self.root)["until"], (self.clock.at + timedelta(hours=1)).isoformat())

    def test_credit_drop_with_a_limit_reached_pauses_until_reset_then_resumes_if_unchanged(self):
        limited = {"usedPercent": 100, "windowDurationMins": 300, "resetsAt": int((NOW + timedelta(hours=3)).timestamp())}
        reader = Reader(reply(60, "100"), reply(60, "99.5", secondary=limited, has_credits=False))
        inner = Inner()
        watched = self.runner(reader, inner)
        self.assertEqual(watched("E", {"items": []}, self.root)[1], None)  # the finished call keeps its answer
        wait = ai_paid_credit_wait(self.root)
        five_reset = (NOW + timedelta(hours=3)).isoformat()
        self.assertEqual((wait["reason"], wait["until"], wait["balance"]), ("AI_PAID_CREDITS_USED", five_reset, "99.5"))
        calls = reader.calls
        for attempt in (watched.check_new_execution, lambda: watched("E", {"items": []}, self.root)):
            with self.assertRaisesRegex(SafetyError, "AI_PAID_CREDITS_USED"):
                attempt()
        self.assertEqual((inner.calls, reader.calls), (1, calls))  # no reading and no call while waiting
        summary = allowance_summary(self.root, settings=self.settings)
        self.assertTrue(summary["ai_paused"])
        self.assertIn("AI waiting because research used paid credits", next(
            line["text"] for line in summary["lines"] if line["item"] == "codex-credits"))
        self.clock.at = NOW + timedelta(hours=3, minutes=1)
        reader.items = [reply(60, "99.5")]
        watched.check_new_execution()
        self.assertIsNone(ai_paid_credit_wait(self.root))
        self.assertEqual(self.alerts()[-1]["kind"], "resumed")

    def test_credit_drop_while_ordinary_use_was_allowed_is_only_a_notice(self):
        inner = Inner()
        watched = self.runner(Reader(reply(40, "100"), reply(41, "97")), inner)
        watched("E", {"items": []}, self.root)
        self.assertIsNone(ai_paid_credit_wait(self.root))
        notice = self.alerts("notice")[0]["message"]
        self.assertIn("fell by 3", notice)
        self.assertIn("most likely your own Codex use", notice)
        watched("E", {"items": []}, self.root)
        self.assertEqual(inner.calls, 2)

    def test_balance_changed_while_paused_rearms_for_the_next_reset(self):
        reader = Reader(reply(90, "10"), reply(100, "9", allowed=False))
        watched = self.runner(reader)
        watched("E", {"items": []}, self.root)
        self.assertEqual(ai_paid_credit_wait(self.root)["reason"], "AI_PAID_CREDITS_USED")
        self.assertEqual(ai_paid_credit_wait(self.root)["until"], WEEK_RESET.isoformat())
        self.clock.at = WEEK_RESET + timedelta(seconds=5)
        next_reset = WEEK_RESET + timedelta(days=7)
        reader.items = [reply(1, "8", reset=next_reset)]
        with self.assertRaisesRegex(SafetyError, "AI_PAID_CREDITS_USED"):
            watched.check_new_execution()
        wait = ai_paid_credit_wait(self.root)
        self.assertEqual((wait["until"], wait["balance"]), (next_reset.isoformat(), "8"))
        self.assertEqual(len(self.alerts("paused")), 2)
        self.clock.at = next_reset + timedelta(seconds=5)
        reader.items = [reply(0, None, reset=None)]
        with self.assertRaisesRegex(SafetyError, "AI_PAID_CREDITS_USED"):
            watched.check_new_execution()  # no balance to prove it unchanged: still waiting
        self.assertEqual(ai_paid_credit_wait(self.root)["until"], (self.clock.at + timedelta(days=1)).isoformat())

    def test_a_missing_balance_uses_the_last_known_one_or_fails_closed(self):
        inner = Inner()
        with self.assertRaisesRegex(SafetyError, "AI_USAGE_READ_FAILED"):
            self.runner(Reader(reply(10, None)), inner)("E", {"items": []}, self.root)
        self.assertEqual(inner.calls, 0)  # credits may exist and no balance was ever seen
        watched = self.runner(Reader(reply(10, "50"), reply(10, "50"), reply(10, None), reply(10, None),
                                     reply(30, "49", allowed=False)), inner)
        watched("E", {"items": []}, self.root)
        self.assertEqual(self.ai()["last_known_balance"], "50")
        watched._cached = None
        watched("E", {"items": []}, self.root)  # no balance before or after: the last known one (50) is the mark
        self.assertEqual(self.ai()["pending_call"]["balance"], "50")
        with self.assertRaisesRegex(SafetyError, "AI_PAID_CREDITS_USED"):
            watched.check_new_execution()
        self.assertEqual(ai_paid_credit_wait(self.root)["reason"], "AI_PAID_CREDITS_USED")

    def test_explicitly_no_credits_means_nothing_to_protect(self):
        watched = self.runner(Reader(reply(10, None, has_credits=False)))
        watched("E", {"items": []}, self.root)
        self.assertNotIn("pending_call", self.ai())
        self.assertIsNone(ai_paid_credit_wait(self.root))
        self.assertEqual(load_state(self.root)["items"]["codex-credits"]["text"], "Codex paid credits: not used by research.")
        watched = self.runner(Reader(reply(10, "5"), reply(10, None, has_credits=False)))
        watched("E", {"items": []}, self.root)
        self.assertNotIn("pending_call", self.ai())

    def test_reads_retry_once_then_back_off_and_tell_the_owner_after_three(self):
        inner = Inner()
        reader = Reader(SafetyError("AI_USAGE_READ_TIMEOUT"), reply(10))
        self.runner(reader, inner)("E", {"items": []}, self.root)
        self.assertEqual((reader.calls, self.sleeps, inner.calls), (3, [2], 1))  # one quick retry, then the after-call read
        failing = Reader(SafetyError("AI_USAGE_READ_TIMEOUT"))
        watched = self.runner(failing, inner)
        delays = []
        for minutes in (5, 10, 20, 60, 60):
            with self.assertRaisesRegex(SafetyError, "AI_USAGE_READ_FAILED"):
                watched.check_new_execution()
            failures = self.ai()["read_failures"]
            delays.append(int((datetime.fromisoformat(failures["next_attempt_at"]) - self.clock.at).total_seconds() // 60))
            calls = failing.calls
            with self.assertRaisesRegex(SafetyError, "AI_USAGE_READ_FAILED"):
                watched.check_new_execution()  # inside the back-off: no new read
            self.assertEqual(failing.calls, calls)
            self.clock.at += timedelta(minutes=minutes)
        self.assertEqual(delays, [5, 10, 20, 60, 60])
        self.assertEqual(self.ai()["read_failures"]["reason"], "AI_USAGE_READ_TIMEOUT")
        notices = [a for a in self.alerts("notice") if a["item"] == "ai-usage-read"]
        self.assertEqual(len(notices), 1)
        self.assertIn("could not be read 3 times in a row", notices[0]["message"])
        self.assertIn("could not be read 5 times", load_state(self.root)["items"]["ai-usage-read"]["text"])
        self.runner(Reader(SafetyError("JSON_RESPONSE_INVALID")), inner)
        recovered = self.runner(Reader(reply(10)), inner)
        recovered.check_new_execution()
        self.assertNotIn("read_failures", self.ai())
        self.assertNotIn("ai-usage-read", load_state(self.root)["items"])
        odd = self.runner(Reader(SafetyError("JSON_RESPONSE_INVALID")), inner)
        with self.assertRaisesRegex(SafetyError, "AI_USAGE_READ_FAILED"):
            odd.check_new_execution()
        self.assertEqual(self.ai()["read_failures"]["reason"], "AI_USAGE_READ_FAILED")

    def test_failed_after_call_reading_is_compared_at_the_next_admission(self):
        reader = Reader(reply(10, "50"), SafetyError("AI_USAGE_READ_TIMEOUT"), SafetyError("AI_USAGE_READ_TIMEOUT"),
                        reply(10, "49", allowed=False))
        inner = Inner()
        watched = self.runner(reader, inner)
        watched("E", {"items": []}, self.root)
        self.assertEqual(self.ai()["pending_call"]["balance"], "50")
        self.clock.at += timedelta(minutes=6)
        with self.assertRaisesRegex(SafetyError, "AI_PAID_CREDITS_USED"):
            watched("E", {"items": []}, self.root)
        self.assertEqual(inner.calls, 1)

    def test_readings_are_cached_briefly_and_inner_guard_runs(self):
        reader, inner = Reader(reply(10)), Inner()
        watched = self.runner(reader, inner)
        watched.check_new_execution()
        watched("E", {"items": []}, self.root)
        self.assertEqual((reader.calls, inner.checks, inner.calls), (2, 1, 1))  # before (cached) and after
        watched.check_new_execution()
        self.assertEqual(reader.calls, 2)

    def test_owner_stop_is_not_delayed_and_errors_still_settle(self):
        reader = Reader(reply(10, "50"))
        watched = self.runner(reader, Inner(KeyboardInterrupt()))
        with self.assertRaises(KeyboardInterrupt):
            watched("E", {"items": []}, self.root)
        self.assertEqual(reader.calls, 1)
        self.assertIn("pending_call", self.ai())
        reader = Reader(reply(10, "50"))
        watched = self.runner(reader, Inner(SafetyError("AI_RATE_LIMITED")))
        with self.assertRaisesRegex(SafetyError, "AI_RATE_LIMITED"):
            watched("E", {"items": []}, self.root)
        self.assertNotIn("pending_call", self.ai())

    def test_unreadable_spend_watch_record_stops_ai_with_its_own_code(self):
        path = runtime_path(self.root, AREA, "state.json")
        path.parent.mkdir(parents=True); path.write_text("{}")
        inner = Inner()
        with self.assertRaisesRegex(SafetyError, "AI_SPEND_WATCH_UNAVAILABLE"):
            self.runner(Reader(reply()), inner)("E", {"items": []}, self.root)
        self.assertEqual(inner.calls, 0)
        with patch("product_radar.spend_watch_ai.load_state", return_value=None), \
             self.assertRaisesRegex(SafetyError, "AI_SPEND_WATCH_UNAVAILABLE"):
            self.runner(Reader(reply()), inner).check_new_execution()
        with self.assertRaisesRegex(SafetyError, "AI_CONFIGURATION_CHANGED"):
            AllowanceWatchedRunner(inner, root=self.root, reader=None)

    def test_window_period_survives_small_reset_shifts_and_rolls_over(self):
        reader = Reader(reply(55), reply(80, reset=WEEK_RESET + timedelta(minutes=3)))
        watched = self.runner(reader)
        watched.check_new_execution()
        watched._cached = None
        watched.check_new_execution()
        self.assertEqual(load_state(self.root)["items"]["ai-codex-10080"]["flags"], [50, 75])
        self.clock.at = WEEK_RESET + timedelta(minutes=10)
        reader.items = [reply(60, reset=WEEK_RESET + timedelta(days=7))]
        watched._cached = None
        watched.check_new_execution()
        self.assertEqual(load_state(self.root)["items"]["ai-codex-10080"]["flags"], [50])
        reader.items = [reply(70, reset=None, secondary={"usedPercent": 1})]
        watched._cached = None
        watched.check_new_execution()
        items = load_state(self.root)["items"]
        self.assertEqual(items["ai-codex-10080"]["period"], "reset-time-not-reported")
        self.assertEqual(items["ai-codex-secondary"]["label"], "AI secondary allowance")
        watched._cached = None
        watched.check_new_execution()
        self.assertEqual(load_state(self.root)["items"]["ai-codex-10080"]["period"], "reset-time-not-reported")

    def test_saved_records_hold_no_account_identifiers(self):
        watched = self.runner(Reader(reply(95, "1"), reply(95, "0.5", allowed=False)))
        watched("E", {"items": []}, self.root)
        saved = json.dumps(load_state(self.root)) + json.dumps(allowance_summary(self.root)["alerts"])
        for private in (PRIVATE_ACCOUNT, PRIVATE_HOME, "accountId"):
            self.assertNotIn(private, saved)

    def test_programme_runner_chains_the_guard_and_treats_its_codes_as_model_waits(self):
        from product_radar.programme import MODEL_ROUTE_FAILURES, _ProgrammeRunner
        from product_radar import research_ai
        codes = {"AI_PAID_CREDITS_USED", "AI_PLAN_LIMIT_CREDITS_PROTECTED", "AI_USAGE_READ_FAILED", "AI_SPEND_WATCH_UNAVAILABLE"}
        self.assertTrue(codes <= MODEL_ROUTE_FAILURES)
        # A job refused after its intent is proven unstarted and retried automatically.
        self.assertEqual(research_ai._AUTOMATIC_GUARD_FAILURES, codes)
        watched = self.runner(Reader(reply(100)))
        record, context = {}, {"memory": SimpleNamespace(root=self.root), "at": NOW.isoformat(),
                               "fixed_as_of": NOW.isoformat(), "clock": None, "retry_seconds": 300}
        outer = _ProgrammeRunner(watched, record, context)
        with patch("product_radar.programme.record_progress"):
            with self.assertRaisesRegex(SafetyError, "AI_PLAN_LIMIT_CREDITS_PROTECTED"):
                outer.check_new_execution()
            with self.assertRaisesRegex(SafetyError, "AI_PLAN_LIMIT_CREDITS_PROTECTED"):
                outer("E", {"items": []}, self.root)
        self.assertEqual(record["model_pause"]["reason"], "AI_PLAN_LIMIT_CREDITS_PROTECTED")

    def test_programme_command_wraps_the_live_model_runner(self):
        import run_programme
        config = {"executable": "/fixture/codex"}
        (self.root / "config/ai").mkdir(parents=True)
        (self.root / "config/ai/codex_luna.json").write_text(json.dumps(config))
        inner, captured = Inner(), {}

        def advance(memory, programme_id, **options):
            captured.update(options)
            return {"programme_id": programme_id}
        with patch("product_radar.research_ai.CodexRunner", return_value=inner) as codex, \
             patch.object(run_programme, "load_programme", return_value={"programme_id": "fixture"}), \
             patch.object(run_programme, "advance_programme", side_effect=advance), redirect_stdout(io.StringIO()):
            code = run_programme.main(["--programme-id", "fixture", "--execute", "--live-model"], memory=SimpleNamespace(),
                handlers={}, discovery_step=lambda *_: None, backup_step=lambda *_: None,
                private_staging=lambda *_: None, root=self.root)
        self.assertEqual(code, 0)
        codex.assert_called_once_with(config, stop_on_interrupt=True)
        self.assertIsInstance(captured["runner"], AllowanceWatchedRunner)
        self.assertIs(captured["runner"].runner, inner)
        self.assertEqual(captured["runner"].root, self.root)


if __name__ == "__main__":
    unittest.main()
