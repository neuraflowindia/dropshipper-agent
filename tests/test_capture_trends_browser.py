"""Native capture boundary fixtures; these never launch a real browser."""

import base64
import importlib.util
import json
from pathlib import Path
import re
import subprocess
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch
from urllib.parse import parse_qs, urlsplit

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("capture_trends_browser", ROOT / "scripts/capture_trends_browser.py")
capture_module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(capture_module)
from product_radar.models import SafetyError
from product_radar.storage import atomic_json


def scope_text(term="chair cover"):
    return "\n".join([term, "Search term", "India", "Past 12 months", "All categories", "Web Search", "Interest over time"])


class NativeFixture:
    def __init__(self, state="captured", text=None, csv=None):
        self.state, self.text, self.csv, self.commands = state, text, csv, []
    def __call__(self, command, **options):
        self.commands.append(command)
        if command[0] == "wslpath":
            return SimpleNamespace(stdout=command[-1] + "\n", returncode=0)
        script = base64.b64decode(command[-1]).decode("utf-16le")
        payload = json.loads(base64.b64decode(re.search(r"FromBase64String\('([^']+)'\)", script).group(1)))
        snapshot = {"url": payload["url"], "title": "Synthetic Trends fixture",
                    "text": self.text if self.text is not None else scope_text(payload["term"]), "buttons": []}
        atomic_json(Path(payload["snapshot"]), snapshot)
        atomic_json(Path(payload["native_result"]), {"state": self.state, "reason": "Synthetic native boundary fixture",
                    "snapshot": snapshot, "completed_at": "2026-10-06T10:00:00+00:00",
                    "owned_process_id": 1234, "owned_process_exited": True,
                    "requests": [{"url": payload["url"], "status": 429 if self.state == "blocked" else 200}]})
        if self.csv is not None:
            Path(payload["downloads"], "multiTimeline.csv").write_bytes(self.csv)
        return SimpleNamespace(returncode=0, stdout=b"", stderr=b"")


class NativeTrendsCaptureTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.patcher = patch.object(capture_module, "ROOT", self.root)
        self.patcher.start()
        self.addCleanup(self.patcher.stop)

    def test_navigation_url_is_fixed_public_google_scope_with_encoded_term(self):
        url = urlsplit(capture_module.explore_url("sink organiser & travel"))
        self.assertEqual(url.hostname, "trends.google.com")
        self.assertEqual(url.path, "/trends/explore")
        values = parse_qs(url.query)
        self.assertEqual(values["q"], ["sink organiser & travel"])
        self.assertEqual(values["geo"], ["IN"])
        self.assertEqual(values["date"], ["today 12-m"])
        for term in ("", " ", "x" * 121, "term\nmalformed"):
            with self.subTest(term=term), self.assertRaises(SafetyError):
                capture_module.explore_url(term)

    def test_requested_url_does_not_count_as_verified_rendered_scope(self):
        self.assertFalse(capture_module.verified_scope({"url": capture_module.explore_url("chair cover"), "text": "429. Too many requests"}, "chair cover"))
        self.assertTrue(capture_module.verified_scope({"text": scope_text()}, "chair cover"))
        for old, new in (("India", "United States"), ("Past 12 months", "Past 5 years"),
                         ("Web Search", "YouTube Search"), ("Search term", "Topic"), ("chair cover", "mattress protector")):
            with self.subTest(old=old):
                self.assertFalse(capture_module.verified_scope({"text": scope_text().replace(old, new)}, "chair cover"))

    def test_rate_limit_remains_blocked_without_csv_or_demand_zero(self):
        fixture = NativeFixture("blocked", "429. Too many requests")
        result = capture_module.capture("chair cover", "blocked", runner=fixture)
        self.assertEqual(result["state"], "Blocked")
        self.assertFalse(result["scope_verified"])
        self.assertIsNone(result["csv_path"])
        self.assertIsNone(result["provider_cost_usd"])
        self.assertEqual(result["provider_requests"], 0)
        self.assertTrue((self.root / "media/live/trends/blocked/receipt.json").exists())
        self.assertNotIn("series", result)
        self.assertEqual(list((self.root / "media/live/trends/blocked").glob(".owned-profile-*")), [])

    def test_verified_csv_preserves_original_bytes_bounded_values_and_scope(self):
        raw = b'Week,chair cover: (India)\n2026-09-13,12\n2026-09-20,<1\n2026-09-27,20\n'
        result = capture_module.capture("chair cover", "csv", runner=NativeFixture(csv=raw))
        self.assertEqual(result["state"], "Usable")
        self.assertEqual(Path(result["csv_path"]).read_bytes(), raw)
        parsed = json.loads((self.root / "media/live/trends/csv/parsed.json").read_text())
        self.assertIsNone(parsed["series"][1]["value"])
        self.assertEqual(parsed["series"][1]["upper"], 1)
        self.assertTrue(parsed["series"][1]["upper_exclusive"])
        self.assertEqual(result["intervals"], 3)
        self.assertTrue(result["scope_verified"])

    def test_download_without_verified_ui_or_missing_csv_is_not_accepted(self):
        raw = b'Week,chair cover: (India)\n2026-09-13,12\n'
        for name, fixture in (("wrong-scope", NativeFixture(text=scope_text().replace("India", "Canada"), csv=raw)),
                              ("missing-csv", NativeFixture())):
            with self.subTest(name=name):
                result = capture_module.capture("chair cover", name, runner=fixture)
                self.assertEqual(result["state"], "Insufficient")
                self.assertIsNone(result["csv_path"])

    def test_html_and_oversized_downloads_are_failures_not_empty_success(self):
        for name, raw in (("html", b"<html>Denied</html>"), ("oversized", b"x" * (capture_module.MAX_BYTES + 1))):
            with self.subTest(name=name):
                result = capture_module.capture("chair cover", name, runner=NativeFixture(csv=raw))
                self.assertEqual(result["state"], "Failed")
                self.assertNotIn("positive", result)

    def test_existing_attempt_and_timeout_limits_do_not_launch_again(self):
        fixture = NativeFixture("blocked", "429")
        capture_module.capture("chair cover", "same-attempt", runner=fixture)
        count = len(fixture.commands)
        with self.assertRaisesRegex(SafetyError, "ALREADY_EXISTS"):
            capture_module.capture("chair cover", "same-attempt", runner=fixture)
        self.assertEqual(len(fixture.commands), count)
        with self.assertRaisesRegex(SafetyError, "TIMEOUT_INVALID"):
            capture_module.capture("chair cover", "bad-timeout", timeout=91, runner=fixture)

    def test_native_command_owns_profile_and_terminates_only_owned_pid(self):
        fixture = NativeFixture("blocked", "429")
        capture_module.capture("chair cover", "owned", runner=fixture)
        script = base64.b64decode(fixture.commands[-1][-1]).decode("utf-16le")
        self.assertIn("--user-data-dir=", script)
        self.assertIn("/PID $owned.Id /T /F", script)
        self.assertNotIn("/IM chrome.exe", script)
        self.assertNotIn("Default\\", script)
        self.assertNotIn("Network.getAllCookies", script)
        self.assertIn("--remote-debugging-port=0", script)

    def test_timeout_cleanup_requires_exact_saved_browser_and_profile(self):
        folder = self.root / "ownership"
        folder.mkdir()
        payload = {"browser": "C:\\Program Files\\Google\\Chrome\\Application\\chrome.exe", "profile": "D:\\owned-profile"}
        atomic_json(folder / "owned_process.json", {"pid": 42, **payload})
        commands = []
        def recorder(command, **kwargs):
            commands.append(command)
            return SimpleNamespace(returncode=0)
        capture_module._cleanup_owned(folder, payload, recorder)
        script = base64.b64decode(commands[0][-1]).decode("utf-16le")
        self.assertIn("ExecutablePath -ine", script)
        self.assertIn("CommandLine.IndexOf", script)
        self.assertIn("/PID $owned.pid", script)
        atomic_json(folder / "owned_process.json", {"pid": 42, **payload, "profile": "C:\\personal-profile"})
        with self.assertRaisesRegex(SafetyError, "OWNED_PROCESS_MISMATCH"):
            capture_module._cleanup_owned(folder, payload, recorder)
        self.assertEqual(len(commands), 1)


if __name__ == "__main__":
    unittest.main()
