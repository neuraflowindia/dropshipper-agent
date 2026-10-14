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
from product_radar.native_public_browser import (SCRIPT, SIGNED_IN_OVERHEAD_SECONDS, SIGNED_IN_SCRIPT_MARGIN, SIGNED_IN_START,
                                                native_snapshot, signed_in_budget, _cleanup_owned)
from product_radar import signed_in_browser


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


PROFILE = "C:\\Users\\owner\\AppData\\Local\\NeuraFlowDropshipper\\research-browser-profile"


class SignedInNativeBrowserTests(unittest.TestCase):
    """The lasting signed-in profile; every Windows call is a synthetic double."""

    installed = NativePublicBrowserTests.installed

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.folder = Path(self.temp.name)
        self.store = self.folder / "store"; self.store.mkdir()
        self.capture = self.folder / "capture"; self.capture.mkdir()
        signed_in_browser._PROFILE_CACHE.clear(); self.addCleanup(signed_in_browser._PROFILE_CACHE.clear)
        self.calls = []

    def signed_runner(self, result):
        def run(command, **kwargs):
            self.calls.append((command, kwargs))
            if command[0] == "wslpath": return SimpleNamespace(stdout=command[-1] + "\n", returncode=0)
            script = base64.b64decode(command[-1]).decode("utf-16le")
            if "GetFolderPath('LocalApplicationData')" in script:
                return SimpleNamespace(returncode=0, stdout=PROFILE, stderr="")
            self.payload = json.loads(base64.b64decode(re.search(r"FromBase64String\('([^']+)'\)", script)[1]))
            Path(self.payload["output"]).write_text(json.dumps(result), encoding="utf-8")
            return SimpleNamespace(returncode=0, stdout=b"", stderr=b"")
        return run

    def read(self, result):
        with self.installed():
            return native_snapshot("https://trends.google.com/trends/explore?geo=IN&q=desk", "'fixture'", self.capture,
                                   profile="signed-in", root=self.store, runner=self.signed_runner(result), timeout=30)

    def clear(self):
        for name in ("native_expression.js", "native_script.ps1", "native_result.json"):
            (self.capture / name).unlink(missing_ok=True)

    def test_signed_in_read_uses_the_lasting_profile_minimised_without_focus(self):
        actual = self.read({"state": "captured", "snapshot": {"ok": True}, "navigation_started": True,
                            "owned_process_exited": True, "sign_in_window_open": False, "close_method": "browser_close",
                            "window_state": "minimized", "page_visibility": "hidden", "animation_frame": True})
        self.assertEqual(actual["state"], "captured"); self.assertEqual(actual["close_method"], "browser_close")
        self.assertEqual((actual["window_state"], actual["page_visibility"], actual["animation_frame"]), ("minimized", "hidden", True))
        self.assertTrue(self.payload["signed_in"]); self.assertEqual(self.payload["profile"], PROFILE)
        self.assertFalse(self.payload["headed"])
        self.assertFalse(any(path.name.startswith("owned-native-profile-") for path in self.capture.iterdir()))
        script = (self.capture / "native_script.ps1").read_text()
        self.assertEqual(self.payload["script_file"], str(self.capture / "native_script.ps1"))
        for flag in ("--disable-renderer-backgrounding", "--disable-backgrounding-occluded-windows",
                     "--disable-background-timer-throttling", "--remote-debugging-port=0", "--disable-sync"):
            self.assertIn(flag, script)
        self.assertNotIn("SIGNED_IN_START", script); self.assertNotIn("CLOSE_LEFTOVERS", script)
        self.assertIn("function Get-SignInWindow", script)
        self.assertIn("startup.flags=1;startup.show=7", script)  # STARTF_USESHOWWINDOW + SW_SHOWMINNOACTIVE
        self.assertNotIn("SetForegroundWindow", script); self.assertNotIn("user-agent", script.lower())
        self.assertNotIn("'--headless=new','--disable-gpu')+$flags", script)
        command, options = self.calls[-1]
        self.assertEqual(options["timeout"], 30 + SIGNED_IN_SCRIPT_MARGIN)
        self.assertIn("$request.script_file", base64.b64decode(command[-1]).decode("utf-16le"))
        self.assertFalse(signed_in_browser.sign_in_window(self.store)["open"])

    def test_realistic_long_windows_paths_keep_the_command_far_below_the_windows_limit(self):
        long_path = "\\\\wsl.localhost\\Ubuntu-24.04\\home\\owner\\" + "neuraflow-dropshipper-data\\" * 5 + "x" * 40
        def run(command, **kwargs):
            self.calls.append(command)
            if command[0] == "wslpath": return SimpleNamespace(stdout=long_path + "\\" + Path(command[-1]).name, returncode=0)
            if "GetFolderPath" in base64.b64decode(command[-1]).decode("utf-16le"):
                return SimpleNamespace(returncode=0, stdout=PROFILE, stderr="")
            return SimpleNamespace(returncode=0, stdout=b"", stderr=b"")
        with self.installed():
            native_snapshot("https://trends.google.com/", "'" + "a" * 19000 + "'", self.capture, profile="signed-in",
                            root=self.store, runner=run, ready_expression="true" * 4000, download_folder=self.capture)
        self.assertGreater(len(long_path), 190)
        launch = self.calls[-1]
        self.assertLess(len(" ".join(launch)), 12000)
        payload = json.loads(base64.b64decode(re.search(r"FromBase64String\('([^']+)'\)", base64.b64decode(launch[-1]).decode("utf-16le"))[1]))
        self.assertTrue(payload["output"].startswith(long_path))

    def test_throwaway_reads_run_the_same_saved_script_without_the_signed_in_part(self):
        result = {"state": "captured", "snapshot": {}, "owned_process_exited": True}
        with self.installed():
            native_snapshot("https://www.amazon.in/s?k=desk", "'fixture'", self.capture, runner=self.signed_runner(result))
        self.assertFalse(self.payload["signed_in"])
        script = (self.capture / "native_script.ps1").read_text()
        self.assertNotIn("NeuraFlowMinimisedStart", script); self.assertNotIn("SIGNED_IN_START", script)
        self.assertIn("if($owned -and $request.signed_in){", script)
        self.assertEqual(self.calls[-1][1]["timeout"], 52)

    def test_leftovers_close_first_then_window_check_then_stale_port_removal_then_launch(self):
        start = SIGNED_IN_START
        removal = start.index("Remove-Item -LiteralPath $portFile")
        self.assertLess(start.index("OWNER_DESKTOP_UNAVAILABLE"), start.index("Close-Leftovers $request.profile $deadline"))
        self.assertLess(start.index("Close-Leftovers $request.profile $deadline"), start.index("(Get-SignInWindow $request.profile).Count"))
        self.assertLess(start.index("OWNER_SIGN_IN_WINDOW_OPEN"), removal)
        self.assertLess(start.index("$portFile=Join-Path $request.profile 'DevToolsActivePort'"), removal)
        self.assertLess(removal, start.index("if(Test-Path -LiteralPath $portFile){throw"))
        self.assertLess(removal, start.index("[NeuraFlowMinimisedStart]::Start("))
        # The Process object is taken before our own process handle is closed.
        self.assertIn("try { return Process.GetProcessById(started.id); } finally { CloseHandle(started.thread);CloseHandle(started.process); }", start)
        self.assertLess(SCRIPT.index("SIGNED_IN_START"), SCRIPT.index("if(-not $owned){"))
        self.assertLess(SCRIPT.index("if(-not $owned){"), SCRIPT.index("Start-Process -FilePath $request.browser"))

    def test_window_detection_ignores_chrome_child_processes(self):
        from product_radar.signed_in_browser import CLOSE_LEFTOVERS
        browsers = CLOSE_LEFTOVERS[CLOSE_LEFTOVERS.index("function Get-ProfileBrowsers"):CLOSE_LEFTOVERS.index("function Get-SignInWindow")]
        self.assertIn("$_.CommandLine -notmatch '--type='", browsers)
        window = CLOSE_LEFTOVERS[CLOSE_LEFTOVERS.index("function Get-SignInWindow"):CLOSE_LEFTOVERS.index("function Close-Leftovers")]
        self.assertIn("Get-ProfileBrowsers $profile|Where-Object {$_.CommandLine -notmatch '--remote-debugging-port'}", window)
        leftovers = CLOSE_LEFTOVERS[CLOSE_LEFTOVERS.index("function Close-Leftovers"):]
        self.assertIn("Get-ProfileBrowsers $profile|Where-Object {$_.CommandLine -match '--remote-debugging-port'}", leftovers)
        self.assertIn("if($by -lt $until){$until=$by}", leftovers)
        self.assertNotIn("Get-ProfileUsers", CLOSE_LEFTOVERS)

    def test_signed_in_window_is_forced_minimised_and_rendering_is_recorded(self):
        minimise = SCRIPT.index("Browser.setWindowBounds")
        self.assertLess(SCRIPT.index("Browser.getWindowForTarget"), minimise)
        self.assertIn("bounds=@{windowState='minimized'}", SCRIPT)
        self.assertLess(minimise, SCRIPT.index("$result.window_state=(Invoke-Cdp 'Browser.getWindowBounds'"))
        self.assertLess(minimise, SCRIPT.index("Page.navigate"))
        self.assertIn("visibility:document.visibilityState", SCRIPT); self.assertIn("requestAnimationFrame(", SCRIPT)
        self.assertLess(SCRIPT.index("$result.page_visibility="), SCRIPT.index("$reply=Invoke-Cdp 'Runtime.evaluate' @{expression=$request.expression"))

    def test_close_order_browser_command_then_window_close_then_forced(self):
        close = SCRIPT.index("""'{"id":1,"method":"Browser.close"}'""")
        self.assertLess(SCRIPT.index("/json/version"), close)
        self.assertLess(close, SCRIPT.index("$sent=$true"))
        self.assertIn("if($sent){$null=$owned.WaitForExit(10000)}", SCRIPT)  # no 10 s wait after a failed send
        window_close = SCRIPT.index("      & taskkill.exe /PID $owned.Id /T | Out-Null")
        self.assertLess(SCRIPT.index("if($sent){$null=$owned.WaitForExit(10000)}"), window_close)
        self.assertLess(window_close, SCRIPT.index("taskkill.exe /PID $owned.Id /T /F"))
        self.assertIn("$result.close_method=if($owned.WaitForExit(5000)){'window_close'}else{'forced'}", SCRIPT)
        self.assertIn("-TimeoutSec $rest", SCRIPT)

    def test_budget_constant_covers_the_whole_signed_in_call(self):
        self.assertEqual(SIGNED_IN_OVERHEAD_SECONDS, 5 + 20 + SIGNED_IN_SCRIPT_MARGIN + 18)
        self.assertEqual(signed_in_budget(45), 45 + SIGNED_IN_OVERHEAD_SECONDS)
        self.assertEqual(signed_in_budget(), signed_in_budget(45))

    def test_outer_cleanup_of_a_signed_in_profile_asks_windows_to_close_first(self):
        (self.folder / "owned_process.json").write_text(json.dumps({"pid": 77, "browser": "C:\\chrome.exe", "profile": PROFILE}))
        seen = []
        def run(command, **kwargs):
            seen.append((base64.b64decode(command[-1]).decode("utf-16le"), kwargs["timeout"]))
            return SimpleNamespace(returncode=0)
        _cleanup_owned(self.folder, {"browser": "C:\\chrome.exe", "profile": PROFILE, "signed_in": True}, run)
        _cleanup_owned(self.folder, {"browser": "C:\\chrome.exe", "profile": PROFILE}, run)
        (graceful_script, graceful_timeout), (forced_script, forced_timeout) = seen
        payload = lambda text: json.loads(base64.b64decode(re.search(r"FromBase64String\('([^']+)'\)", text)[1]))
        self.assertTrue(payload(graceful_script)["graceful"]); self.assertFalse(payload(forced_script)["graceful"])
        self.assertEqual((graceful_timeout, forced_timeout), (18, 8))
        self.assertLess(graceful_script.index("& taskkill.exe /PID $owned.pid /T | Out-Null"), graceful_script.index("/T /F"))
        # Never the owner's sign-in window: only a process started with remote control.
        self.assertIn("$process.CommandLine -notmatch '--remote-debugging-port'){exit 2}", graceful_script)

    def test_failed_graceful_cleanup_is_reported(self):
        (self.folder / "owned_process.json").write_text(json.dumps({"pid": 77, "browser": "C:\\chrome.exe", "profile": PROFILE}))
        with self.assertRaisesRegex(SafetyError, "CLEANUP_UNVERIFIED"):
            _cleanup_owned(self.folder, {"browser": "C:\\chrome.exe", "profile": PROFILE, "signed_in": True},
                           lambda command, **kwargs: SimpleNamespace(returncode=3))

    def test_sign_in_window_open_waits_without_navigation_or_counting_as_a_sign_in(self):
        actual = self.read({"state": "waiting", "reason": "OWNER_SIGN_IN_WINDOW_OPEN", "navigation_started": False,
                            "snapshot": None, "owned_process_exited": True, "stage": "browser_launch", "sign_in_window_open": True})
        self.assertEqual((actual["state"], actual["reason"]), ("waiting", "OWNER_SIGN_IN_WINDOW_OPEN"))
        self.assertFalse(actual["navigation_started"])
        window = signed_in_browser.sign_in_window(self.store)
        self.assertTrue(window["open"]); self.assertIsNone(window["last_opened_at"])
        self.assertEqual(signed_in_browser.signed_in_wait(self.store, "deodap"), "OWNER_SIGN_IN_WINDOW_OPEN")
        self.assertEqual(signed_in_browser.site_states(self.store)["indiamart"]["state"], "sign_in_needed")
        self.assertIn("$wait='OWNER_SIGN_IN_WINDOW_OPEN';throw 'wait'", SIGNED_IN_START)
        self.assertIn("if($wait){$result.state='waiting';$result.reason=$wait}", SCRIPT)

    def test_locked_desktop_is_remembered_so_the_next_reads_do_not_probe(self):
        actual = self.read({"state": "waiting", "reason": "OWNER_DESKTOP_UNAVAILABLE", "navigation_started": False,
                            "snapshot": None, "owned_process_exited": True, "stage": "browser_launch"})
        self.assertEqual(actual["reason"], "OWNER_DESKTOP_UNAVAILABLE")
        self.assertIn("Get-Process -Name LogonUI", SIGNED_IN_START)
        self.clear(); self.calls.clear()
        again = self.read({"state": "captured"})
        self.assertEqual(again, {"state": "waiting", "reason": "OWNER_DESKTOP_UNAVAILABLE", "navigation_started": False,
                                 "stage": "browser_launch", "snapshot": None})
        self.assertEqual(self.calls, []); self.assertEqual(list(self.capture.iterdir()), [])
        self.assertEqual(signed_in_browser.signed_in_wait(self.store, "deodap"), "OWNER_DESKTOP_UNAVAILABLE")

    def test_closed_sign_in_window_found_by_a_read_clears_the_saved_open_state(self):
        signed_in_browser.note_sign_in_window(self.store, True, opened=True)
        self.read({"state": "captured", "snapshot": {}, "owned_process_exited": True, "sign_in_window_open": False})
        self.assertFalse(signed_in_browser.sign_in_window(self.store)["open"])

    def test_signed_in_results_and_saved_receipt_carry_no_account_identity(self):
        snapshot = {"html": '<a aria-label="Google Account: Research Owner (owner.research@gmail.com)" href="/x">',
                    "text": "Signed in as owner.research@gmail.com", "url": "https://trends.google.com/?authuser=0&hl=en"}
        actual = self.read({"state": "captured", "snapshot": snapshot, "owned_process_exited": True,
                            "requests": [{"url": "https://accounts.google.com/o?login_hint=owner.research%40gmail.com&x=1"}]})
        saved = (self.capture / "native_result.json").read_text()
        for text in (json.dumps(actual), saved):
            self.assertNotIn("owner.research", text); self.assertNotIn("Research Owner", text)
        self.assertEqual(actual["snapshot"]["url"], "https://trends.google.com/?authuser=redacted&hl=en")
        self.assertIn("&x=1", actual["requests"][0]["url"])
        self.assertEqual(json.loads(saved)["state"], "captured")

    def test_a_running_signed_in_read_holds_the_next_one_without_any_windows_call(self):
        def immediate(root, wait=5):
            return signed_in_browser._file_lock(signed_in_browser._folder(root) / "browser.lock", 0, "SIGNED_IN_BROWSER_BUSY")
        with signed_in_browser.browser_lock(self.store), patch.object(signed_in_browser, "browser_lock", immediate):
            actual = self.read({"state": "captured"})
        self.assertEqual(actual, {"state": "waiting", "reason": "SIGNED_IN_BROWSER_BUSY", "navigation_started": False,
                                  "stage": "browser_launch", "snapshot": None})
        self.assertEqual(self.calls, []); self.assertEqual(list(self.capture.iterdir()), [])

    def test_lock_failures_other_than_busy_are_not_hidden(self):
        with patch.object(signed_in_browser, "browser_lock", side_effect=SafetyError("SYMLINK_PATH_REFUSED")), \
                self.assertRaisesRegex(SafetyError, "SYMLINK_PATH_REFUSED"):
            self.read({"state": "captured"})

    def test_unresolvable_profile_is_unavailable_not_a_throwaway_fallback(self):
        def run(command, **kwargs):
            self.calls.append(command)
            return SimpleNamespace(returncode=0, stdout="C:\\Users\\owner\\AppData\\Local\\Google\\Chrome\\User Data", stderr="")
        with self.installed():
            actual = native_snapshot("https://trends.google.com/", "'fixture'", self.capture, profile="signed-in",
                                     root=self.store, runner=run)
        self.assertEqual((actual["state"], actual["reason"]), ("unavailable", "SIGNED_IN_PROFILE_PATH_INVALID"))
        self.assertEqual(len(self.calls), 1); self.assertEqual(list(self.capture.iterdir()), [])

    def test_profile_choice_is_validated(self):
        for options in ({"profile": "owner"}, {"profile": "signed-in", "headed": True}, {"root": self.store}):
            with self.subTest(options=options), self.installed(), self.assertRaisesRegex(SafetyError, "INPUT_INVALID"):
                native_snapshot("https://trends.google.com/", "'fixture'", self.capture, **options)

    def test_signed_in_timeout_cleanup_is_graceful(self):
        def run(command, **kwargs):
            if command[0] == "wslpath": return SimpleNamespace(stdout=command[-1], returncode=0)
            if "GetFolderPath" in base64.b64decode(command[-1]).decode("utf-16le"):
                return SimpleNamespace(returncode=0, stdout=PROFILE, stderr="")
            raise subprocess.TimeoutExpired(command[0], kwargs["timeout"])
        with self.installed(), patch("product_radar.native_public_browser._cleanup_owned") as cleanup:
            actual = native_snapshot("https://trends.google.com/", "'fixture'", self.capture, profile="signed-in",
                                     root=self.store, runner=run)
        self.assertEqual(actual["reason"], "NATIVE_PUBLIC_DEADLINE")
        self.assertTrue(cleanup.call_args.args[1]["signed_in"])

if __name__ == "__main__": unittest.main()
