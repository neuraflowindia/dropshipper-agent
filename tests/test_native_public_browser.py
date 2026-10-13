"""Synthetic native-process receipts; never launch a browser or Windows tool."""

import base64
from contextlib import ExitStack
import json
from pathlib import Path
import re
import subprocess
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from product_radar.models import SafetyError
from product_radar.native_public_browser import SCRIPT, native_snapshot, _cleanup_owned


class NativePublicBrowserTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.folder = Path(self.temp.name)

    def runner(self, result):
        def run(command, **kwargs):
            if command[0] == "wslpath": return SimpleNamespace(stdout=command[-1] + "\n", returncode=0)
            script = base64.b64decode(command[-1]).decode("utf-16le")
            encoded = re.search(r"FromBase64String\('([^']+)'\)", script)[1]
            payload = json.loads(base64.b64decode(encoded))
            Path(payload["output"]).write_text(json.dumps(result), encoding="utf-8")
            return SimpleNamespace(returncode=0, stdout=b"", stderr=b"")
        return run

    def installed(self):
        browser, powershell = self.folder / "browser.exe", self.folder / "powershell.exe"
        browser.write_text("Synthetic executable fixture"); powershell.write_text("Synthetic executable fixture")
        stack = ExitStack()
        stack.enter_context(patch("product_radar.native_public_browser.CHROME", browser))
        stack.enter_context(patch("product_radar.native_public_browser.POWERSHELL", powershell))
        return stack

    def test_actual_navigation_timestamp_and_cleanup_proof_survive_unchanged(self):
        result = {"state": "captured", "snapshot": {"url": "https://www.amazon.in/gp/bestsellers/kitchen"},
                  "navigation_status": 200, "navigation_started": True, "navigation_started_at": "2026-10-08T10:00:00.125Z",
                  "owned_process_exited": True}
        with self.installed():
            actual = native_snapshot(result["snapshot"]["url"], "JSON.stringify({url:location.href})", self.folder,
                                     runner=self.runner(result), timeout=15)
        self.assertEqual(actual["navigation_started_at"], result["navigation_started_at"])
        self.assertTrue(actual["owned_process_exited"])
        self.assertEqual(actual["exit_code"], 0)

    def test_browser_not_installed_does_not_claim_navigation_or_start_runner(self):
        def forbidden(*args, **kwargs): raise AssertionError("Native process must not start")
        with patch("product_radar.native_public_browser.Path.is_file", return_value=False):
            actual = native_snapshot("https://www.amazon.in/gp/bestsellers/kitchen", "'fixture'", self.folder, runner=forbidden)
        self.assertEqual(actual["state"], "unavailable"); self.assertFalse(actual["navigation_started"])

    def test_wsl_process_block_is_not_a_provider_challenge(self):
        def runner(command, **kwargs):
            if command[0] == "wslpath": return SimpleNamespace(stdout=command[-1], returncode=0)
            return SimpleNamespace(returncode=1, stdout=b"", stderr=b"<3>WSL ERROR: UtilBindVsockAnyPort:317: socket failed 1")
        with self.installed(): actual = native_snapshot("https://www.amazon.in/gp/bestsellers/kitchen", "'fixture'", self.folder, runner=runner)
        self.assertEqual(actual["state"], "failed")
        self.assertEqual(actual["reason"], "NATIVE_PUBLIC_WSL_INTEROP_BLOCKED")
        self.assertFalse(actual["navigation_started"])

    def test_deadline_is_bounded_and_does_not_guess_a_success(self):
        def runner(command, **kwargs):
            if command[0] == "wslpath": return SimpleNamespace(stdout=command[-1], returncode=0)
            raise subprocess.TimeoutExpired(command[0], kwargs["timeout"])
        with self.installed(): actual = native_snapshot("https://www.amazon.in/gp/bestsellers/kitchen", "'fixture'", self.folder, runner=runner, timeout=15)
        self.assertEqual(actual["reason"], "NATIVE_PUBLIC_DEADLINE")
        self.assertIsNone(actual["navigation_started"]); self.assertIsNone(actual["snapshot"])

    def test_existing_owned_artifacts_are_never_overwritten(self):
        (self.folder / "native_result.json").write_text("Existing receipt", encoding="utf-8")
        with self.installed(), self.assertRaisesRegex(SafetyError, "ARTIFACT_EXISTS"):
            native_snapshot("https://www.amazon.in/gp/bestsellers/kitchen", "'fixture'", self.folder)
        self.assertEqual((self.folder / "native_result.json").read_text(), "Existing receipt")

    def test_cleanup_never_terminates_a_mismatched_process(self):
        (self.folder / "owned_process.json").write_text(json.dumps({"pid": 123, "browser": "wrong", "profile": "wrong"}))
        def forbidden(*args, **kwargs): raise AssertionError("Mismatched PID must not be used")
        with self.assertRaisesRegex(SafetyError, "OWNERSHIP_INVALID"):
            _cleanup_owned(self.folder, {"browser": "owned", "profile": "owned-profile"}, forbidden)

    def test_unreadable_result_after_owned_launch_does_not_claim_no_navigation(self):
        def runner(command, **kwargs):
            if command[0] == "wslpath": return SimpleNamespace(stdout=command[-1], returncode=0)
            script = base64.b64decode(command[-1]).decode("utf-16le")
            payload = json.loads(base64.b64decode(re.search(r"FromBase64String\('([^']+)'\)", script)[1]))
            Path(payload["output"]).write_text("not valid JSON")
            Path(payload["ownership"]).write_text(json.dumps({"pid": 123, "browser": payload["browser"], "profile": payload["profile"]}))
            return SimpleNamespace(returncode=0, stdout=b"", stderr=b"")
        with self.installed(), patch("product_radar.native_public_browser._cleanup_owned") as cleanup:
            result = native_snapshot("https://www.amazon.in/gp/bestsellers/kitchen", "'fixture'", self.folder, runner=runner)
        self.assertEqual(result["state"], "failed")
        self.assertIsNone(result["navigation_started"])
        self.assertEqual(result["stage"], "result_read")
        cleanup.assert_called_once()

    def test_cleanup_failure_is_explicit_and_does_not_escape_or_claim_process_exited(self):
        result = {"state": "captured", "snapshot": {}, "navigation_started": True, "owned_process_exited": False}
        with self.installed(), patch("product_radar.native_public_browser._cleanup_owned", side_effect=SafetyError("NATIVE_PUBLIC_CLEANUP_UNVERIFIED")):
            actual = native_snapshot("https://www.amazon.in/gp/bestsellers/kitchen", "'fixture'", self.folder, runner=self.runner(result))
        self.assertEqual(actual["state"], "failed")
        self.assertEqual(actual["reason"], "NATIVE_PUBLIC_CLEANUP_UNVERIFIED")
        self.assertFalse(actual["owned_process_exited"])
        self.assertTrue(actual["navigation_started"])

    def test_optional_ready_and_public_export_hooks_are_bounded_to_owned_folder(self):
        with self.installed(), self.assertRaisesRegex(SafetyError, "DOWNLOAD_PATH_INVALID"):
            native_snapshot("https://trends.google.com/trends/explore", "'fixture'", self.folder,
                            download_folder=self.folder / "elsewhere")
        with self.installed(), self.assertRaisesRegex(SafetyError, "INPUT_INVALID"):
            native_snapshot("https://trends.google.com/trends/explore", "'fixture'", self.folder, ready_expression=True)
        self.assertIn("if($request.ready_expression)", SCRIPT)
        self.assertIn("behavior='allow';downloadPath=$request.downloads", SCRIPT)
        self.assertIn("else {$null=Invoke-Cdp 'Browser.setDownloadBehavior' @{behavior='deny'}}", SCRIPT)

    def test_large_dom_instructions_are_saved_not_put_on_windows_command_line(self):
        commands = []
        actual_runner = self.runner({"state": "captured", "snapshot": {}, "owned_process_exited": True})
        def runner(command, **kwargs):
            commands.append(command)
            return actual_runner(command, **kwargs)
        instruction = "'" + "a" * 19000 + "'"
        with self.installed():
            native_snapshot("https://trends.google.com/trends/explore", instruction, self.folder,
                            ready_expression="true", download_folder=self.folder, runner=runner)
        self.assertEqual((self.folder / "native_expression.js").read_text(), instruction)
        self.assertEqual((self.folder / "native_ready.js").read_text(), "true")
        self.assertLess(len(commands[-1][-1]), 30000)

    def test_owned_profile_no_login_and_main_document_status_only(self):
        self.assertIn("--user-data-dir=", SCRIPT)
        self.assertIn("navigation_started_at", SCRIPT)
        self.assertIn("$reply.params.frameId -eq $script:result.primary_frame_id", SCRIPT)
        self.assertIn("taskkill.exe /PID $owned.Id", SCRIPT)
        self.assertNotIn("taskkill.exe /IM", SCRIPT)
        self.assertNotIn("--profile-directory", SCRIPT)


if __name__ == "__main__": unittest.main()
