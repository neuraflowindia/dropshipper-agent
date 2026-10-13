"""Playwright timing/replay proofs with simulated pages; no browser or network."""

from copy import deepcopy
from pathlib import Path
import sys
import tempfile
from types import ModuleType, SimpleNamespace
import unittest
from unittest.mock import patch

from product_radar.models import SafetyError, timestamp
from product_radar.storage import atomic_json, load_json
from product_radar.trends_capture import (
    _capture_utc_now, capture, validate_saved_capture,
)
from product_radar.trends_controller import (
    build_trends_request, capture_direct_trends, replay_direct_trends,
)
from test_trends_capture import Download, DownloadEvent, FixturePage

BASE = "2026-10-08T14:04:10"
START = BASE + ".100000+00:00"
NAVIGATION = BASE + ".200000+00:00"
PAGE = BASE + ".300000+00:00"
CSV = BASE + ".400000+00:00"
END = BASE + ".500000+00:00"
WHOLE = BASE + "+00:00"


class TrendsPlaywrightTimeRecoveryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.page = FixturePage(self.root)

    def run_capture(self, *, clocks=None, name="precision"):
        with patch("product_radar.trends_capture._capture_utc_now", side_effect=clocks or [START, NAVIGATION, PAGE, CSV, END]):
            return capture("chair cover", name, self.root / "captures", page=self.page)

    def original_bytes(self, folder):
        return {str(path.relative_to(folder)): path.read_bytes() for path in folder.rglob("*") if path.is_file()}

    def fake_playwright(self, *, missing=False):
        """Provide owned-browser protocol objects, never run an executable."""
        closed = []
        executable = self.root / "not-executable-fixture"
        if not missing:
            executable.write_bytes(b"Test fixture; never executed.")
        async def new_page():
            return self.page
        async def new_context(**options):
            return SimpleNamespace(new_page=new_page)
        async def close():
            closed.append("browser")
        async def launch(**options):
            return SimpleNamespace(new_context=new_context, close=close)
        async def stop():
            closed.append("driver")
        driver = SimpleNamespace(chromium=SimpleNamespace(executable_path=str(executable), launch=launch), stop=stop)
        async def start():
            return driver
        package, module = ModuleType("playwright"), ModuleType("playwright.async_api")
        module.async_playwright = lambda: SimpleNamespace(start=start)
        return {"playwright": package, "playwright.async_api": module}, closed

    def wrapper_capture(self, *, clocks, name="wrapper", native_fallback=False, missing=False):
        modules, closed = self.fake_playwright(missing=missing)
        with patch.dict(sys.modules, modules), \
                patch("product_radar.trends_capture._capture_utc_now", side_effect=clocks), \
                patch("product_radar.trends_controller._wrapper_utc_now", side_effect=[BASE + ".000000+00:00", BASE + ".900000+00:00"]), \
                patch("product_radar.native_public_browser.native_snapshot", return_value={"state": "unavailable",
                    "reason": "Synthetic native startup failure", "navigation_started": False, "snapshot": None}) as native:
            receipt = capture_direct_trends(build_trends_request("chair cover"), name,
                data_root=self.root / "direct", transport="auto" if native_fallback else "playwright")
        self.assertEqual(native.call_count, int(native_fallback))
        return receipt, closed

    def test_clock_is_local_aware_and_precise(self):
        value = _capture_utc_now()
        self.assertRegex(value, r"\.\d{6}\+00:00$")
        self.assertEqual(timestamp(value).utcoffset().total_seconds(), 0)
        from product_radar.models import utc_now
        self.assertNotIn(".", utc_now())

    def test_same_second_success_stamps_actual_csv_read_and_keeps_exact_chart(self):
        result = self.run_capture()
        self.assertEqual(result["state"], "Usable")
        self.assertEqual(result["started_at"], START)
        self.assertEqual(result["navigation_started_at"], NAVIGATION)
        self.assertEqual(result["collected_at"], CSV)
        self.assertEqual(result["completed_at"], END)
        self.assertTrue(result["chart_rows_match"])
        self.assertEqual(result["intervals"], 3)
        self.assertEqual(Path(result["csv_path"]).read_bytes(), self.page.download_path.read_bytes())
        parsed = load_json(Path(result["receipt_path"]).with_name("parsed.json"), 262144)
        self.assertEqual(parsed["metadata"]["collected_at"], CSV)
        self.assertEqual(validate_saved_capture(self.root / "captures", "precision", "chair cover"), result)

    def test_every_readable_block_uses_actual_snapshot_time_without_download(self):
        for index, status, text in ((0, 429, "Too many requests"), (1, 403, "Denied"), (2, 200, "Unusual traffic CAPTCHA")):
            with self.subTest(status=status, text=text):
                self.page = FixturePage(self.root, status=status)
                self.page.snapshot["text"] = text
                result = self.run_capture(name="blocked-" + str(index), clocks=[START, NAVIGATION, PAGE, END])
                self.assertEqual(result["state"], "Blocked")
                self.assertEqual(result["collected_at"], PAGE)
                self.assertLess(timestamp(result["navigation_started_at"]), timestamp(result["collected_at"]))
                self.assertIsNone(result["csv_path"])
                self.assertFalse(any(action[0] == "download" for action in self.page.actions))
                self.assertEqual(validate_saved_capture(self.root / "captures", "blocked-" + str(index), "chair cover"), result)

    def test_readable_insufficient_page_keeps_snapshot_observation_not_initial_time(self):
        self.page.snapshot["download_found"] = False
        result = self.run_capture(clocks=[START, NAVIGATION, PAGE, END])
        self.assertEqual(result["state"], "Insufficient")
        self.assertEqual(result["collected_at"], PAGE)
        self.assertIsNone(result["csv_path"])

    def test_navigation_and_snapshot_failures_use_actual_failed_completion_without_data(self):
        for index, method in enumerate(("goto", "evaluate")):
            with self.subTest(method=method):
                self.page = FixturePage(self.root)
                async def fail(*args, **kwargs):
                    raise RuntimeError("Private fixture error must not leak")
                setattr(self.page, method, fail)
                name = "before-observation-" + str(index)
                result = self.run_capture(name=name, clocks=[START, NAVIGATION, END])
                self.assertEqual(result["state"], "Failed")
                self.assertEqual(result["collected_at"], END)
                self.assertEqual(result["completed_at"], END)
                self.assertTrue(result["navigation_started"])
                self.assertFalse((Path(result["receipt_path"]).parent / "scope.json").exists())
                self.assertIsNone(result["csv_path"])
                self.assertFalse(result["scope_verified"])
                self.assertNotIn("Private", result["reason"])
                self.assertEqual(validate_saved_capture(self.root / "captures", name, "chair cover"), result)

    def test_failure_after_snapshot_preserves_that_real_observation_date(self):
        self.page.expect_download = lambda **options: DownloadEvent(Download(None))
        result = self.run_capture(clocks=[START, NAVIGATION, PAGE, END])
        self.assertEqual(result["state"], "Failed")
        self.assertEqual(result["collected_at"], PAGE)
        self.assertTrue((Path(result["receipt_path"]).parent / "scope.json").is_file())
        self.assertIsNone(result["csv_path"])

    def test_new_replay_does_not_read_clock_rewrite_files_or_navigate(self):
        result = self.run_capture()
        folder = Path(result["receipt_path"]).parent
        before = self.original_bytes(folder)
        actions = deepcopy(self.page.actions)
        with patch("product_radar.trends_capture._capture_utc_now", side_effect=AssertionError("Saved dates are immutable")):
            self.assertEqual(capture("chair cover", "precision", self.root / "captures", page=self.page), result)
            self.assertEqual(validate_saved_capture(self.root / "captures", "precision", "chair cover"), result)
        self.assertEqual(self.original_bytes(folder), before)
        self.assertEqual(self.page.actions, actions)

    def test_legacy_whole_second_packet_replays_with_original_bytes_and_dates(self):
        with patch("product_radar.trends_capture._capture_utc_now", return_value=WHOLE):
            result = capture("chair cover", "legacy", self.root / "captures", page=self.page)
        folder = Path(result["receipt_path"]).parent
        before = self.original_bytes(folder)
        actions = deepcopy(self.page.actions)
        with patch("product_radar.trends_capture._capture_utc_now", side_effect=AssertionError("No redating")):
            self.assertEqual(capture("chair cover", "legacy", self.root / "captures", page=self.page), result)
        self.assertEqual(result["collected_at"], WHOLE)
        self.assertEqual(self.original_bytes(folder), before)
        self.assertEqual(self.page.actions, actions)

    def test_old_blocked_packet_keeps_its_pre_navigation_date_on_saved_replay(self):
        self.page.status = 429
        self.page.snapshot["text"] = "Too many requests"
        result = self.run_capture(name="old-block", clocks=[START, NAVIGATION, PAGE, END])
        old = dict(result, started_at=WHOLE, collected_at=WHOLE)
        receipt_path = Path(result["receipt_path"])
        atomic_json(receipt_path, old)
        before = self.original_bytes(receipt_path.parent)
        actions = deepcopy(self.page.actions)
        with patch("product_radar.trends_capture._capture_utc_now", side_effect=AssertionError("Do not redate saved legacy results")):
            self.assertEqual(capture("chair cover", "old-block", self.root / "captures", page=self.page), old)
        self.assertEqual(self.original_bytes(receipt_path.parent), before)
        self.assertEqual(self.page.actions, actions)

    def test_replay_still_rejects_inverted_and_malformed_completion_without_redating(self):
        result = self.run_capture()
        receipt_path = Path(result["receipt_path"])
        actions = deepcopy(self.page.actions)
        for end in (BASE + ".399999+00:00", BASE + ".500000", "invalid"):
            with self.subTest(end=end):
                atomic_json(receipt_path, dict(result, completed_at=end))
                original = receipt_path.read_bytes()
                with patch("product_radar.trends_capture._capture_utc_now", side_effect=AssertionError("No clock allowance")), \
                        self.assertRaisesRegex(SafetyError, "INVALID_OR_INCOMPLETE"):
                    capture("chair cover", "precision", self.root / "captures", page=self.page)
                self.assertEqual(receipt_path.read_bytes(), original)
        self.assertEqual(self.page.actions, actions)

    def test_wrapper_replays_precise_same_second_playwright_chart_without_loose_clocks(self):
        result, closed = self.wrapper_capture(clocks=[START, NAVIGATION, PAGE, CSV, END])
        self.assertEqual(result["state"], "usable")
        self.assertEqual(result["dispatch_started_at"], NAVIGATION)
        self.assertEqual(result["observed_at"], CSV)
        self.assertEqual(result["completed_at"], BASE + ".900000+00:00")
        self.assertEqual(closed, ["browser", "driver"])
        self.assertEqual(replay_direct_trends(result["folder"]), result)
        self.assertEqual(result["apify_requests"], 0)
        self.assertEqual(result["model_calls"], 0)

    def test_native_startup_fallback_keeps_google_block_as_block_not_clock_failure(self):
        self.page.status = 429
        self.page.snapshot["text"] = "Too many requests"
        result, closed = self.wrapper_capture(clocks=[START, NAVIGATION, PAGE, END], native_fallback=True)
        self.assertEqual(result["transport_attempts"], ["owned_native_windows_chrome", "owned_playwright_browser"])
        self.assertEqual(result["state"], "blocked")
        self.assertEqual(result["reason"], "DIRECT_TRENDS_ACCESS_CHALLENGE")
        self.assertEqual(result["observed_at"], PAGE)
        self.assertEqual(result["dispatch_started_at"], NAVIGATION)
        self.assertEqual(replay_direct_trends(result["folder"]), result)
        self.assertEqual(closed, ["browser", "driver"])
        self.assertNotIn("multiTimeline.csv", result["artifacts"])
        original = load_json(Path(result["folder"]) / "transport.json", 262144)
        self.assertIn("Google returned a rate limit or access challenge", original["reason"])
        self.assertEqual(original["fallback_receipt"]["reason"], original["reason"])
        self.assertEqual(original["fallback_receipt"]["collected_at"], PAGE)
        child = load_json(Path(result["folder"]) / "fallback/playwright/receipt.json", 32768)
        self.assertEqual(child, original["fallback_receipt"])

    def test_new_bridge_does_not_turn_failed_snapshot_or_insufficient_chart_into_block(self):
        for index, mode in enumerate(("failed", "insufficient")):
            with self.subTest(mode=mode):
                self.page = FixturePage(self.root)
                if mode == "failed":
                    self.page.expect_download = lambda **options: DownloadEvent(Download(None))
                else:
                    self.page.snapshot["download_found"] = False
                result, closed = self.wrapper_capture(name="non-block-" + str(index), clocks=[START, NAVIGATION, PAGE, END])
                self.assertEqual(result["state"], mode)
                self.assertEqual(result["observed_at"], PAGE)
                self.assertEqual(closed, ["browser", "driver"])
                self.assertEqual(replay_direct_trends(result["folder"]), result)
                if mode == "failed":
                    original = load_json(Path(result["folder"]) / "transport.json", 262144)
                    self.assertEqual(result["reason"], original["reason"])
                self.assertNotEqual(result["reason"], "DIRECT_TRENDS_ACCESS_CHALLENGE")

    def test_redirect_transport_is_not_relabelled_as_independently_confirmed_google_block(self):
        self.page.snapshot["url"] = "https://accounts.google.com/signin"
        result, _ = self.wrapper_capture(name="redirect", clocks=[START, NAVIGATION, PAGE, END])
        original = load_json(Path(result["folder"]) / "transport.json", 262144)
        interpreted = load_json(Path(result["folder"]) / "interpretation.json", 262144)
        self.assertEqual(original["fallback_receipt"]["reason_code"], "redirect_outside_public_explore")
        self.assertEqual(result["state"], "blocked")
        self.assertEqual(result["reason"], original["reason"])
        self.assertEqual(interpreted["state"], "insufficient")
        self.assertNotEqual(result["reason"], "DIRECT_TRENDS_ACCESS_CHALLENGE")
        self.assertFalse(result["scope_verified"])
        self.assertNotIn("multiTimeline.csv", result["artifacts"])
        # Existing independent replay acceptance remains strict. The narrow
        # paired-block correction cannot donate verification to this redirect.
        with self.assertRaisesRegex(SafetyError, "RECEIPT_INVALID"):
            replay_direct_trends(result["folder"])

    def test_wrapper_keeps_pre_snapshot_browser_failure_and_its_proved_attempt(self):
        async def fail(*args, **kwargs):
            raise RuntimeError("Synthetic navigation failed")
        self.page.goto = fail
        result, closed = self.wrapper_capture(clocks=[START, NAVIGATION, END])
        self.assertEqual(result["state"], "failed")
        self.assertTrue(result["dispatch_started"])
        self.assertEqual(result["observed_at"], END)
        self.assertEqual(closed, ["browser", "driver"])
        self.assertEqual(replay_direct_trends(result["folder"]), result)
        self.assertNotIn("scope.json", result["artifacts"])
        self.assertNotIn("multiTimeline.csv", result["artifacts"])

    def test_wrapper_keeps_unavailable_browser_without_inventing_navigation(self):
        result, closed = self.wrapper_capture(clocks=[START, END], missing=True)
        self.assertEqual(result["state"], "unavailable")
        self.assertFalse(result["dispatch_started"])
        self.assertEqual(result["observed_at"], END)
        self.assertEqual(closed, ["driver"])
        self.assertEqual(self.page.actions, [])
        self.assertEqual(replay_direct_trends(result["folder"]), result)


if __name__ == "__main__":
    unittest.main()
