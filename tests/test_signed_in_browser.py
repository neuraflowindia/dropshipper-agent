"""Signed-in research profile records, locks and owner sign-in window.

Every Windows, PowerShell and Chrome call is a synthetic double: nothing here
launches a browser, opens a window or visits a site.
"""

import base64
from contextlib import ExitStack
import io
import json
from pathlib import Path
import re
import subprocess
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "scripts"))
from product_radar import signed_in_browser as signed
from product_radar.models import SafetyError
from product_radar.source_coverage import SIGNED_IN_ACCESS_IDENTITY, signed_in_access_scope, source_access_scope

PROFILE = "C:\\Users\\owner\\AppData\\Local\\NeuraFlowDropshipper\\research-browser-profile"
REQUIRED = ["google", "google-trends", "indiamart", "dropship-india", "dropdash"]


def decoded(command):
    """The PowerShell text and its embedded request, from an -EncodedCommand call."""
    script = base64.b64decode(command[-1]).decode("utf-16le")
    found = re.search(r"FromBase64String\('([^']+)'\)", script)
    return script, json.loads(base64.b64decode(found[1])) if found else None


class SignedInBrowserTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="radar-signed-in-"); self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        signed._PROFILE_CACHE.clear(); self.addCleanup(signed._PROFILE_CACHE.clear)
        self.commands = []

    def installed(self):
        tools = self.root / "tools"; tools.mkdir(exist_ok=True)
        chrome, powershell = tools / "chrome.exe", tools / "powershell.exe"
        chrome.write_text("Synthetic executable fixture"); powershell.write_text("Synthetic executable fixture")
        stack = ExitStack()
        stack.enter_context(patch("product_radar.native_public_browser.CHROME", chrome))
        stack.enter_context(patch("product_radar.native_public_browser.POWERSHELL", powershell))
        return stack

    def runner(self, *, window=False, opened="opened", profile=PROFILE):
        def run(command, **kwargs):
            self.commands.append(command)
            if command[0] == "wslpath":
                return SimpleNamespace(stdout="C:\\Program Files\\Google\\Chrome\\Application\\chrome.exe\n", returncode=0)
            script, request = decoded(command)
            if "GetFolderPath('LocalApplicationData')" in script:
                return SimpleNamespace(returncode=0, stdout=profile, stderr="")
            if "already_open" in script:
                return SimpleNamespace(returncode=0, stdout=json.dumps({"state": opened}), stderr="")
            return SimpleNamespace(returncode=0, stdout=json.dumps({"window": window}), stderr="")
        return run

    # Profile path -----------------------------------------------------------------------------------------------
    def test_profile_is_resolved_through_powershell_once_and_never_hard_coded(self):
        run = self.runner()
        with self.installed():
            self.assertEqual(signed.research_profile(run), PROFILE)
            self.assertEqual(signed.research_profile(run), PROFILE)
        self.assertEqual(len(self.commands), 1)
        script, _ = decoded(self.commands[0])
        self.assertIn("[Environment]::GetFolderPath('LocalApplicationData')", script)
        self.assertIn("NeuraFlowDropshipper\\research-browser-profile", script)
        self.assertNotIn("C:\\Users", script)

    def test_profile_path_outside_the_program_folder_or_malformed_is_refused(self):
        for value in ("C:\\Users\\owner\\AppData\\Local\\Google\\Chrome\\User Data",
                      "/home/owner/research-browser-profile", PROFILE + "\nextra", "C:\\a\\Other\\research-browser-profile"):
            with self.subTest(value=value), self.installed(), self.assertRaisesRegex(SafetyError, "PROFILE_PATH_INVALID"):
                signed._PROFILE_CACHE.clear()
                signed.research_profile(self.runner(profile=value))

    def test_missing_powershell_or_failed_command_fails_closed(self):
        with patch("product_radar.native_public_browser.POWERSHELL", self.root / "missing.exe"), \
                self.assertRaisesRegex(SafetyError, "NOT_INSTALLED"):
            signed.research_profile(self.runner())
        def failing(command, **kwargs): return SimpleNamespace(returncode=1, stdout="", stderr="denied")
        def raising(command, **kwargs): raise subprocess.TimeoutExpired(command[0], kwargs["timeout"])
        for runner in (failing, raising):
            with self.subTest(runner=runner.__name__), self.installed(), self.assertRaisesRegex(SafetyError, "COMMAND_FAILED"):
                signed.research_profile(runner)

    # Lock --------------------------------------------------------------------------------------------------------
    def test_one_signed_in_chrome_at_a_time(self):
        with signed.browser_lock(self.root):
            with self.assertRaisesRegex(SafetyError, "SIGNED_IN_BROWSER_BUSY"):
                with signed.browser_lock(self.root, wait=0):
                    pass
        with signed.browser_lock(self.root, wait=0):
            pass

    def test_sign_in_window_launch_waits_for_a_running_read(self):
        with self.installed(), signed.browser_lock(self.root), self.assertRaisesRegex(SafetyError, "SIGNED_IN_BROWSER_BUSY"):
            signed.open_sign_in_window(self.root, runner=self.runner(), wait=0)
        self.assertFalse(any(command[0] != "wslpath" for command in self.commands))
        self.assertFalse(signed.sign_in_window(self.root)["open"])

    # Sign-in window ----------------------------------------------------------------------------------------------
    def test_open_starts_plain_chrome_without_remote_control_and_with_every_site(self):
        with self.installed():
            self.assertEqual(signed.open_sign_in_window(self.root, runner=self.runner()), "opened")
        script, request = decoded(self.commands[-1])
        flags = script[script.index("$flags="):script.index("$quoted=")]
        self.assertNotIn("--remote-debugging-port", flags)
        self.assertIn("'--disable-sync'", flags)
        self.assertIn("--user-data-dir=", flags)
        self.assertNotIn("--headless", script)
        self.assertIn("-WindowStyle Normal", script)
        self.assertEqual(request["profile"], PROFILE)
        self.assertEqual(request["urls"], [row["url"] for row in signed.SITES.values()])
        hosts = " ".join(request["urls"])
        for host in ("accounts.google.com", "trends.google.com", "indiamart.com", "deodap.in", "baapstore.com",
                     "dropshipindia.live", "app.dropdash.co"):
            self.assertIn(host, hosts)
        window = signed.sign_in_window(self.root)
        self.assertTrue(window["open"]); self.assertEqual(window["last_opened_at"], window["checked_at"])
        # Program Chrome left behind is closed before deciding the owner's window is already open.
        self.assertLess(script.index("Close-Leftovers $request.profile"), script.index("(Get-SignInWindow $request.profile).Count"))
        self.assertLess(script.index("(Get-SignInWindow $request.profile).Count"), script.index("already_open"))

    def test_already_open_window_is_not_opened_twice_but_the_owner_click_rechecks_sites(self):
        signed.note_sign_in_window(self.root, True, opened=True, at="2026-10-14T10:00:00+00:00")
        signed.note_sign_in_window(self.root, False, at="2026-10-14T10:01:00+00:00")
        signed.record_site_observation(self.root, "indiamart", False, observed_at="2026-10-14T11:00:00+00:00")
        self.assertEqual(signed.site_states(self.root)["indiamart"]["state"], "sign_in_needed")
        with self.installed():
            self.assertEqual(signed.open_sign_in_window(self.root, runner=self.runner(opened="already_open")), "already_open")
        self.assertTrue(signed.sign_in_window(self.root)["open"])
        self.assertEqual(signed.site_states(self.root)["indiamart"]["state"], "not_checked")

    def test_unknown_open_reply_fails_closed(self):
        for reply in ("not json", json.dumps({"state": "maybe"}), json.dumps([])):
            def run(command, **kwargs):
                if command[0] == "wslpath": return SimpleNamespace(stdout="C:\\chrome.exe", returncode=0)
                script, _ = decoded(command)
                return SimpleNamespace(returncode=0, stdout=PROFILE if "GetFolderPath" in script else reply, stderr="")
            with self.subTest(reply=reply), self.installed(), self.assertRaisesRegex(SafetyError, "COMMAND_FAILED"):
                signed.open_sign_in_window(self.root, runner=run)
        with patch("product_radar.native_public_browser.CHROME", self.root / "missing.exe"), \
                self.assertRaisesRegex(SafetyError, "NOT_INSTALLED"):
            signed.open_sign_in_window(self.root, runner=self.runner())

    def test_window_check_looks_at_local_processes_only_and_saves_closed(self):
        signed.note_sign_in_window(self.root, True, opened=True, at="2026-10-14T10:00:00+00:00")
        with self.installed():
            closed = signed.check_sign_in_window(self.root, runner=self.runner(window=False))
        self.assertFalse(closed["open"]); self.assertEqual(closed["last_opened_at"], "2026-10-14T10:00:00+00:00")
        script, request = decoded(self.commands[-1])
        self.assertIn("Get-CimInstance Win32_Process", script)
        self.assertIn("window=((Get-SignInWindow $request.profile).Count -gt 0)", script)
        self.assertLess(script.index("if($request.close_leftovers){Close-Leftovers"), script.index("window=((Get-SignInWindow"))
        self.assertNotIn("Start-Process", script); self.assertNotIn("http", script.lower().replace("https://", ""))
        self.assertEqual(request, {"profile": PROFILE, "close_leftovers": True})
        for bad in ("[]", json.dumps({"window": "yes"})):
            def run(command, **kwargs):
                script, _ = decoded(command)
                return SimpleNamespace(returncode=0, stdout=PROFILE if "GetFolderPath" in script else bad, stderr="")
            with self.subTest(bad=bad), self.installed(), self.assertRaisesRegex(SafetyError, "COMMAND_FAILED"):
                signed._PROFILE_CACHE.clear()
                signed.check_sign_in_window(self.root, runner=run)

    def test_window_check_during_a_read_never_closes_the_reads_chrome(self):
        signed.note_sign_in_window(self.root, True, opened=True)
        with self.installed(), signed.browser_lock(self.root):
            signed.check_sign_in_window(self.root, runner=self.runner(window=False))
        self.assertFalse(decoded(self.commands[-1])[1]["close_leftovers"])
        with patch.object(signed, "browser_lock", side_effect=SafetyError("SYMLINK_PATH_REFUSED")), \
                self.assertRaisesRegex(SafetyError, "SYMLINK"):
            signed.check_sign_in_window(self.root, runner=self.runner())

    def test_a_probe_finding_a_window_never_counts_as_the_owners_sign_in(self):
        with self.installed():
            found = signed.check_sign_in_window(self.root, runner=self.runner(window=False))
            self.assertFalse(found["open"]); self.assertFalse((self.root / "data/operations/signed-in-browser/sign-in-window.json").exists())
            signed.note_sign_in_window(self.root, True)  # a read or status saw a window it did not open
        window = signed.sign_in_window(self.root)
        self.assertTrue(window["open"]); self.assertIsNone(window["last_opened_at"])
        self.assertEqual([row["site"] for row in signed.sign_in_needs(self.root)], REQUIRED)

    def test_status_verifies_the_window_only_when_it_was_open(self):
        with self.installed():
            status = signed.sign_in_status(self.root, runner=self.runner())
            self.assertEqual(self.commands, [])
            self.assertFalse(status["window_open"])
            self.assertEqual([row["site"] for row in status["needs"]], REQUIRED)
            signed.note_sign_in_window(self.root, True, opened=True)
            status = signed.sign_in_status(self.root, runner=self.runner(window=True))
        self.assertTrue(status["window_open"]); self.assertTrue(self.commands)
        self.assertIn("The sign-in window is open", status["line"])

    # Site states and waits -------------------------------------------------------------------------------------
    def test_never_signed_in_routes_wait_calmly_and_optional_sites_may_be_read(self):
        for site in REQUIRED:
            self.assertEqual(signed.signed_in_wait(self.root, site), "OWNER_SIGN_IN_REQUIRED")
        for site in ("deodap", "baapstore"):
            self.assertIsNone(signed.signed_in_wait(self.root, site))
        self.assertEqual(signed.site_states(self.root)["deodap"]["state"], "not_checked")
        self.assertIn("Sign-in needed: Google account, Google Trends, IndiaMART, Dropship India, Dropdash.",
                      signed.sign_in_line(self.root))

    def test_open_window_holds_reads_until_rechecked(self):
        signed.note_sign_in_window(self.root, True, opened=True, at="2026-10-14T10:00:00+00:00")
        self.assertEqual(signed.signed_in_wait(self.root, "indiamart", as_of="2026-10-14T10:01:59+00:00"), "OWNER_SIGN_IN_WINDOW_OPEN")
        # After two minutes a read may start; the read itself re-verifies the window first.
        self.assertIsNone(signed.signed_in_wait(self.root, "indiamart", as_of="2026-10-14T10:02:00+00:00"))
        self.assertEqual(signed.sign_in_needs(self.root), [])
        self.assertEqual(signed.site_states(self.root)["indiamart"]["state"], "not_checked")

    def test_signed_in_then_expired_then_rechecked_after_new_sign_in(self):
        signed.note_sign_in_window(self.root, True, opened=True, at="2026-10-14T10:00:00+00:00")
        signed.note_sign_in_window(self.root, False, at="2026-10-14T10:05:00+00:00")
        self.assertEqual(signed.sign_in_line(self.root), "Sign-ins will be confirmed on the next research reads.")
        row = signed.record_site_observation(self.root, "indiamart", True, route="indiamart", observed_at="2026-10-14T10:10:00+00:00")
        self.assertEqual(row["state"], "signed_in")
        signed.record_site_observation(self.root, "indiamart", True, observed_at="2026-10-14T11:00:00+00:00")
        self.assertEqual(signed.site_states(self.root)["indiamart"]["since"], "2026-10-14T10:10:00+00:00")
        row = signed.record_site_observation(self.root, "indiamart", False, route="indiamart", observed_at="2026-10-15T09:00:00+00:00")
        self.assertEqual((row["state"], row["since"]), ("expired", "2026-10-15T09:00:00+00:00"))
        self.assertEqual(signed.signed_in_wait(self.root, "indiamart", as_of="2026-10-15T09:01:00+00:00"), "OWNER_SIGN_IN_REQUIRED")
        self.assertEqual(signed.sign_in_needs(self.root), [{"id": "indiamart:expired:2026-10-15T09:00:00+00:00",
                                                            "site": "indiamart", "label": "IndiaMART", "state": "expired"}])
        self.assertIn("Sign-in expired: IndiaMART.", signed.sign_in_line(self.root))
        signed.note_sign_in_window(self.root, True, opened=True, at="2026-10-15T10:00:00+00:00")
        signed.note_sign_in_window(self.root, False, at="2026-10-15T10:03:00+00:00")
        self.assertEqual(signed.site_states(self.root)["indiamart"]["state"], "not_checked")
        self.assertIsNone(signed.signed_in_wait(self.root, "indiamart", as_of="2026-10-15T10:04:00+00:00"))

    def test_wall_before_any_sign_in_is_sign_in_needed_not_expired(self):
        signed.note_sign_in_window(self.root, True, opened=True, at="2026-10-14T10:00:00+00:00")
        signed.note_sign_in_window(self.root, False, at="2026-10-14T10:01:00+00:00")
        signed.record_site_observation(self.root, "dropdash", False, observed_at="2026-10-14T10:30:00+00:00")
        self.assertEqual(signed.site_states(self.root)["dropdash"]["state"], "sign_in_needed")
        signed.record_site_observation(self.root, "google", True, observed_at="2026-10-14T10:30:00+00:00")
        for site in ("google", "google-trends", "indiamart", "dropship-india"):
            signed.record_site_observation(self.root, site, True, observed_at="2026-10-14T10:31:00+00:00")
        signed.record_site_observation(self.root, "dropdash", True, observed_at="2026-10-14T10:32:00+00:00")
        self.assertEqual(signed.sign_in_line(self.root), "Research sites are signed in.")

    def test_optional_sites_never_need_the_owner_until_they_were_signed_in(self):
        signed.record_site_observation(self.root, "deodap", False, observed_at="2026-10-14T10:00:00+00:00")
        self.assertEqual(signed.site_states(self.root)["deodap"]["state"], "not_signed_in")
        self.assertIsNone(signed.signed_in_wait(self.root, "deodap"))
        self.assertNotIn("deodap", [row["site"] for row in signed.sign_in_needs(self.root)])
        signed.record_site_observation(self.root, "baapstore", True, observed_at="2026-10-14T10:00:00+00:00")
        signed.record_site_observation(self.root, "baapstore", False, observed_at="2026-10-15T10:00:00+00:00")
        self.assertEqual(signed.signed_in_wait(self.root, "baapstore"), "OWNER_SIGN_IN_REQUIRED")
        self.assertIn("baapstore", [row["site"] for row in signed.sign_in_needs(self.root)])

    def test_locked_desktop_is_one_calm_wait_for_two_minutes(self):
        self.assertFalse(signed.desktop_unavailable(self.root))
        signed.note_desktop_unavailable(self.root, at="2026-10-14T22:00:00+00:00")
        for site in ("deodap", "indiamart"):
            self.assertEqual(signed.signed_in_wait(self.root, site, as_of="2026-10-14T22:01:59+00:00"), "OWNER_DESKTOP_UNAVAILABLE")
        self.assertFalse(signed.desktop_unavailable(self.root, as_of="2026-10-14T22:02:00+00:00"))
        self.assertIsNone(signed.signed_in_wait(self.root, "deodap", as_of="2026-10-14T22:02:00+00:00"))
        (self.root / "data/operations/signed-in-browser/desktop.json").write_text(json.dumps({"schema_version": 1, "at": "x"}))
        with self.assertRaisesRegex(SafetyError, "RECORD_INVALID"):
            signed.desktop_unavailable(self.root)

    def test_every_calm_wait_carries_its_recheck_time(self):
        # The desktop and window waits end with their saved rechecks; others are looked at again in two minutes.
        signed.note_desktop_unavailable(self.root, at="2026-10-14T22:00:00+00:00")
        self.assertEqual(signed.signed_in_recheck_at(self.root, "OWNER_DESKTOP_UNAVAILABLE", as_of="2026-10-14T22:01:00+00:00"),
                         "2026-10-14T22:02:00+00:00")
        signed.note_sign_in_window(self.root, True, opened=True, at="2026-10-14T22:00:30+00:00")
        self.assertEqual(signed.signed_in_recheck_at(self.root, "OWNER_SIGN_IN_WINDOW_OPEN", as_of="2026-10-14T22:01:00+00:00"),
                         "2026-10-14T22:02:30+00:00")
        for reason in ("OWNER_SIGN_IN_REQUIRED", "SIGNED_IN_BROWSER_BUSY", "NATIVE_PUBLIC_ARTIFACT_EXISTS"):
            self.assertEqual(signed.signed_in_recheck_at(self.root, reason, as_of="2026-10-14T22:01:00+00:00"),
                             "2026-10-14T22:03:00+00:00")
        # A recheck that has already passed (the desktop came back) is looked at again in two minutes, never in the past.
        self.assertEqual(signed.signed_in_recheck_at(self.root, "OWNER_DESKTOP_UNAVAILABLE", as_of="2026-10-14T23:00:00+00:00"),
                         "2026-10-14T23:02:00+00:00")
        signed.note_sign_in_window(self.root, False, at="2026-10-14T22:05:00+00:00")
        self.assertEqual(signed.signed_in_recheck_at(self.root, "OWNER_SIGN_IN_WINDOW_OPEN", as_of="2026-10-14T22:06:00+00:00"),
                         "2026-10-14T22:08:00+00:00")
        with patch.object(signed, "utc_now", return_value="2026-10-14T22:06:00+00:00"):
            self.assertEqual(signed.signed_in_recheck_at(self.root, "OWNER_SIGN_IN_REQUIRED"), "2026-10-14T22:08:00+00:00")

    def test_redaction_removes_account_identity_but_keeps_pages_usable(self):
        html = ('<a aria-label="Google Account: Research Owner&#10;(owner.research@example.com)" href="https://accounts.google.com/'
                'SignOutOptions?hl=en&amp;authuser=0&amp;continue=x"><img srcset="/logo@2x.png 2x"></a>'
                '<script id="__NEXT_DATA__">{"props":{"email":"owner.research@example.com","glid":"123",'
                '"next":"https:\\/\\/x.in\\/?email=owner%40example.com\\u0026page=2"}}</script>')
        value = {"html": html, "rows": [{"text": "owner.research@example.com", "count": 3}], "flag": True,
                 "url": "https://accounts.google.com/o?login_hint=owner.research%40example.com&username=owner&x=1#email=a"}
        clean = signed.redact_signed_in(value)
        flat = json.dumps(clean)
        for secret in ("owner.research", "Research Owner", "owner%40", "username=owner"):
            self.assertNotIn(secret, flat)
        self.assertIn("Google Account: [account redacted]", clean["html"])
        self.assertIn("/logo@2x.png 2x", clean["html"]); self.assertIn("&amp;continue=x", clean["html"])
        self.assertIn('"glid":"123"', clean["html"]); self.assertIn("\\u0026page=2", clean["html"])
        data = clean["html"][clean["html"].index('{"props"'):clean["html"].index("</script>")]
        self.assertEqual(json.loads(data)["props"]["email"], "[email redacted]")
        self.assertEqual(clean["url"], "https://accounts.google.com/o?login_hint=redacted&username=redacted&x=1#email=redacted")
        self.assertEqual(clean["rows"], [{"text": "[email redacted]", "count": 3}]); self.assertIs(clean["flag"], True)
        self.assertEqual(signed.redact_signed_in(None), None)

    def test_observations_and_saved_records_are_validated(self):
        for args in (("unknown", True), ("google", "yes"), ("google", True, "bad route!")):
            with self.subTest(args=args), self.assertRaisesRegex(SafetyError, "OBSERVATION_INVALID"):
                signed.record_site_observation(self.root, *args[:2], route=args[2] if len(args) > 2 else None)
        with self.assertRaisesRegex(SafetyError, "OBSERVATION_INVALID"):
            signed.signed_in_wait(self.root, "unknown")
        folder = self.root / "data/operations/signed-in-browser"; folder.mkdir(parents=True)
        bad_sites = ({"schema_version": 2, "sites": {}}, {"schema_version": 1, "sites": {"other": {}}},
                     {"schema_version": 1, "sites": {"google": {"signed_in": True}}},
                     {"schema_version": 1, "sites": {"google": {"signed_in": True, "observed_at": "yesterday",
                      "since": "2026-10-14T10:00:00+00:00", "last_signed_in_at": None, "route": None}}}, ["not", "a", "record"])
        for value in bad_sites:
            (folder / "site-states.json").write_text(json.dumps(value))
            with self.subTest(value=value), self.assertRaises(SafetyError):
                signed.site_states(self.root)
        (folder / "site-states.json").unlink()
        bad_windows = ({"schema_version": 1, "open": True, "checked_at": None, "last_opened_at": "2026-10-14T10:00:00+00:00"},
                       {"schema_version": 1, "open": "no", "checked_at": None, "last_opened_at": None},
                       {"schema_version": 1, "open": False, "checked_at": "later", "last_opened_at": None},
                       {"schema_version": 1, "open": False, "checked_at": None, "last_opened_at": None, "pid": 1})
        for value in bad_windows:
            (folder / "sign-in-window.json").write_text(json.dumps(value))
            with self.subTest(value=value), self.assertRaises(SafetyError):
                signed.sign_in_window(self.root)

    def test_closed_window_note_without_change_writes_nothing(self):
        signed.note_sign_in_window(self.root, False)
        self.assertFalse((self.root / "data").exists())

    def test_records_hold_no_page_content_passwords_or_cookies(self):
        signed.note_sign_in_window(self.root, True, opened=True)
        signed.record_site_observation(self.root, "google-trends", True, route="google-trends")
        saved = " ".join(path.read_text() for path in (self.root / "data/operations/signed-in-browser").glob("*.json"))
        for word in ("password", "cookie", "token", "@"):
            self.assertNotIn(word, saved.lower())

    # Reads and access identity ---------------------------------------------------------------------------------
    def test_signed_in_read_waits_without_launching_and_records_markers(self):
        calls = []
        def browser(url, expression, folder, **options):
            calls.append(options)
            return {"state": "captured", "snapshot": {"marker": url}}
        self.assertEqual(signed.signed_in_read("indiamart", "https://www.indiamart.com/", "x", self.root, marker=bool,
                                               root=self.root, browser=browser)["reason"], "OWNER_SIGN_IN_REQUIRED")
        self.assertEqual(calls, [])
        signed.note_sign_in_window(self.root, True, opened=True, at="2026-10-14T10:00:00+00:00")
        signed.note_sign_in_window(self.root, False, at="2026-10-14T10:05:00+00:00")
        seen = signed.signed_in_read("indiamart", "https://www.indiamart.com/", "x", self.root, root=self.root,
                                     route="indiamart", browser=browser, marker=lambda snapshot: True, timeout=30)
        self.assertTrue(seen["signed_in"])
        self.assertEqual(calls, [{"profile": "signed-in", "root": self.root, "timeout": 30}])
        self.assertEqual(signed.site_states(self.root)["indiamart"]["route"], "indiamart")
        walled = signed.signed_in_read("indiamart", "https://www.indiamart.com/", "x", self.root, root=self.root,
                                       browser=browser, marker=lambda snapshot: False)
        self.assertEqual(walled["sign_in_reason"], "OWNER_SIGN_IN_REQUIRED")
        self.assertEqual(signed.site_states(self.root)["indiamart"]["state"], "expired")
        unknown = signed.signed_in_read("deodap", "https://deodap.in/", "x", self.root, root=self.root,
                                        browser=browser, marker=lambda snapshot: None)
        self.assertIsNone(unknown["signed_in"]); self.assertIsNone(signed.site_states(self.root)["deodap"]["observed_at"])
        failed = signed.signed_in_read("deodap", "https://deodap.in/", "x", self.root, root=self.root,
                                       browser=lambda *a, **k: {"state": "waiting", "reason": "OWNER_DESKTOP_UNAVAILABLE"},
                                       marker=lambda snapshot: self.fail("No marker without a captured page"))
        self.assertEqual(failed["reason"], "OWNER_DESKTOP_UNAVAILABLE")

    def test_signed_in_read_defaults_to_the_native_signed_in_browser(self):
        with patch("product_radar.native_public_browser.native_snapshot", return_value={"state": "waiting"}) as native:
            signed.signed_in_read("deodap", "https://deodap.in/", "x", self.root, root=self.root, marker=bool)
        self.assertEqual(native.call_args.kwargs["profile"], "signed-in")

    def test_signed_in_access_identity_is_separate_from_the_hidden_browser(self):
        public = source_access_scope("https://trends.google.com/trends/explore")
        own = signed_in_access_scope("https://trends.google.com/trends/explore")
        self.assertEqual(own["access_identity"], SIGNED_IN_ACCESS_IDENTITY)
        self.assertEqual(own["access_identity"], "owner-signed-in")
        self.assertEqual(own["host"], public["host"])
        self.assertNotEqual(own, public)
        self.assertEqual(set(signed.WAIT_CODES), {"OWNER_SIGN_IN_REQUIRED", "OWNER_SIGN_IN_WINDOW_OPEN",
                                                  "OWNER_DESKTOP_UNAVAILABLE", "SIGNED_IN_BROWSER_BUSY"})

    # Command-line entry ----------------------------------------------------------------------------------------
    def test_sign_in_command_open_status_and_busy(self):
        import research_sign_in as command
        output = io.StringIO()
        with patch.object(command, "open_sign_in_window", return_value="opened") as opened, \
                patch.object(command, "sign_in_status", return_value={"window_open": True, "line": "open"}), \
                patch("sys.stdout", output):
            self.assertEqual(command.main(["open"]), 0)
        opened.assert_called_once_with(command.ROOT)
        self.assertEqual(json.loads(output.getvalue()), {"state": "opened", "window_open": True, "line": "open"})
        output = io.StringIO()
        with patch.object(command, "sign_in_status", return_value={"window_open": False}), patch("sys.stdout", output):
            self.assertEqual(command.main(["status"]), 0)
        self.assertEqual(json.loads(output.getvalue())["state"], "status")
        for error, message in ((SafetyError("SIGNED_IN_BROWSER_BUSY"), "Try again in a minute"),
                               (SafetyError("SIGNED_IN_BROWSER_NOT_INSTALLED"), "could not be handled"),
                               (OSError("disk"), "could not be handled")):
            output = io.StringIO()
            with self.subTest(error=error), patch.object(command, "open_sign_in_window", side_effect=error), \
                    patch("sys.stdout", output):
                self.assertEqual(command.main(["open"]), 1)
            self.assertIn(message, json.loads(output.getvalue())["message"])
        with self.assertRaises(SystemExit), patch("sys.stderr", io.StringIO()):
            command.main(["close"])


if __name__ == "__main__":
    unittest.main()
