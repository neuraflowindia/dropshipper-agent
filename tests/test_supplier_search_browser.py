"""Owned browser fixtures prove isolation/bounds/cleanup, not Google availability."""

from copy import deepcopy
from pathlib import Path
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from product_radar.supplier_discovery import _parse_receipt, _search_url
from product_radar.supplier_search_browser import _allow_request, _challenge_url, fetch_google_search_page

QUERY = "food wrap paper India manufacturer supplier"
ITEM = {"id": "supplier-browser-test", "url": _search_url(QUERY)}
HTML = '<html><body><a href="https://supplier.example.org/food"><h3>Food wrap paper supplier</h3></a></body></html>'


class Fixture:
    def __init__(self, fail=None, status=200, snapshot=None):
        self.fail, self.status = fail, status
        self.snapshot = snapshot if snapshot is not None else {"html": HTML, "html_characters": len(HTML), "text": "Food wrap paper supplier", "text_truncated": False, "title": "Google Search"}
        self.calls, self.url = [], ITEM["url"]
        self.chromium = self
        self.closed = []

    def factory(self):
        return self

    def start(self):
        self.calls.append(("start", {}))
        return self

    def launch(self, **kwargs):
        self.calls.append(("launch", kwargs))
        if self.fail == "launch":
            raise RuntimeError("Synthetic exception with sensitive machine path")
        return Browser(self)

    def stop(self):
        self.closed.append("driver")


class Browser:
    def __init__(self, fixture):
        self.fixture = fixture

    def new_context(self, **kwargs):
        self.fixture.calls.append(("new_context", kwargs))
        return Context(self.fixture)

    def close(self):
        self.fixture.closed.append("browser")


class Context:
    def __init__(self, fixture):
        self.fixture = fixture

    def route(self, pattern, handler):
        self.fixture.calls.append(("route", {"pattern": pattern}))
        self.fixture.route_handler = handler

    def new_page(self):
        return Page(self.fixture)

    def close(self):
        self.fixture.closed.append("context")


class Page:
    def __init__(self, fixture):
        self.fixture = fixture

    @property
    def url(self):
        return self.fixture.url

    def set_default_timeout(self, timeout):
        self.fixture.calls.append(("default_timeout", {"timeout": timeout}))

    def goto(self, url, **kwargs):
        self.fixture.calls.append(("goto", {"url": url, **kwargs}))
        if self.fixture.fail == "goto":
            raise RuntimeError("Synthetic secret exception")
        return type("Response", (), {"status": self.fixture.status})()

    def wait_for_function(self, expression, **kwargs):
        self.fixture.calls.append(("wait", kwargs))
        if self.fixture.fail == "wait":
            raise TimeoutError("Synthetic secret timeout")

    def evaluate(self, expression):
        if self.fixture.fail == "evaluate":
            raise RuntimeError("Synthetic secret exception")
        return deepcopy(self.fixture.snapshot)


class SupplierSearchBrowserTests(unittest.TestCase):
    def fetch(self, fixture=None, item=None, **kwargs):
        fixture = fixture or Fixture()
        return fetch_google_search_page(item or deepcopy(ITEM), playwright_factory=fixture.factory, **kwargs), fixture

    def test_dispatch_callback_is_after_successful_launch_before_navigation_only(self):
        fixture = Fixture()
        checkpoints = []
        receipt, fixture = self.fetch(fixture, on_dispatch=lambda: checkpoints.append(
            [name for name, _kwargs in fixture.calls]))
        self.assertEqual(receipt["state"], "usable")
        self.assertEqual(len(checkpoints), 1)
        self.assertIn("new_context", checkpoints[0])
        self.assertNotIn("goto", checkpoints[0])
        failed, fixture = self.fetch(Fixture(fail="launch"), on_dispatch=lambda: checkpoints.append([]))
        self.assertEqual(failed["execution_stage"], "not_dispatched")
        self.assertEqual(len(checkpoints), 1)

    def test_owned_context_no_profile_cookies_proxy_downloads_or_service_worker(self):
        receipt, fixture = self.fetch()
        self.assertEqual(receipt["state"], "usable")
        context = next(kwargs for action, kwargs in fixture.calls if action == "new_context")
        self.assertEqual(context, {"locale": "en-IN", "accept_downloads": False, "service_workers": "block"})
        launch = next(kwargs for action, kwargs in fixture.calls if action == "launch")
        self.assertEqual(set(launch), {"headless", "timeout"})
        self.assertTrue(launch["headless"])
        self.assertEqual(fixture.closed, ["context", "browser", "driver"])

    def test_receipt_is_parser_compatible_and_preserves_rendered_html(self):
        receipt, _ = self.fetch()
        self.assertEqual(receipt["html"], HTML)
        self.assertEqual(receipt["html_basis"], "rendered_browser_dom")
        self.assertIsNone(receipt["wire_sha256"])
        self.assertEqual(len(receipt["html_sha256"]), 64)
        parsed = _parse_receipt(receipt, QUERY, ITEM["id"])
        self.assertEqual(parsed["state"], "complete")
        self.assertEqual(parsed["results"][0]["url"], "https://supplier.example.org/food")

    def test_get_google_resources_allowed_other_hosts_forms_and_supplier_links_blocked(self):
        for url, method, resource, navigation, expected in (
            (ITEM["url"], "GET", "document", True, True),
            ("https://www.gstatic.com/script.js", "GET", "script", False, True),
            ("https://www.google.com/async?x=1", "GET", "xhr", False, True),
            ("https://supplier.example.org/food", "GET", "document", True, False),
            ("https://accounts.google.com/login", "GET", "document", True, False),
            ("https://www.google.com/sorry/index", "GET", "document", True, False),
            ("https://www.google.com/search?q=other", "GET", "document", True, False),
            (ITEM["url"], "POST", "document", True, False),
            ("https://www.gstatic.com/a.jpg", "GET", "image", False, False),
            ("http://www.google.com/search?q=test", "GET", "document", True, False),
            ("https://www.google.com:8443/x.js", "GET", "script", False, False),
            ("https://www.google.com.example.org/x.js", "GET", "script", False, False),
        ):
            with self.subTest(url=url):
                self.assertEqual(_allow_request(url, method, resource, navigation, QUERY), expected)

    def test_invalid_query_urls_and_non_search_inputs_do_not_launch(self):
        for item in ({**ITEM, "url": "https://supplier.example.org/food"},
                     {**ITEM, "url": "https://www.google.com/search?q=food&gl=us"},
                     {**ITEM, "url": ITEM["url"] + "&token=not-a-real-secret"},
                     {**ITEM, "extra": True}, {**ITEM, "id": "../bad"}):
            fixture = Fixture()
            receipt, fixture = self.fetch(fixture, item=item)
            self.assertEqual(receipt["state"], "rejected")
            self.assertEqual(fixture.calls, [])

    def test_failure_at_each_stage_closes_owned_resources(self):
        for stage in ("launch", "goto", "wait", "evaluate"):
            fixture = Fixture(fail=stage)
            receipt, fixture = self.fetch(fixture)
            self.assertEqual(receipt["state"], "unavailable")
            self.assertEqual(fixture.closed, ["driver"] if stage == "launch" else ["context", "browser", "driver"])
            self.assertNotIn("secret", str(receipt))

    def test_browser_launch_failure_is_prelaunch_not_supplier_effort(self):
        receipt, _ = self.fetch(Fixture(fail="launch"))
        self.assertEqual(receipt["schema_version"], 2)
        self.assertEqual(receipt["execution_stage"], "not_dispatched")
        self.assertIsNone(receipt["attempt_started_at"])
        self.assertIsNone(receipt["execution_ref"])

    def test_real_navigation_failure_keeps_owned_execution_proof(self):
        receipt, fixture = self.fetch(Fixture(fail="goto"))
        self.assertEqual(receipt["execution_stage"], "attempt_started")
        self.assertIsNotNone(receipt["attempt_started_at"])
        self.assertRegex(receipt["execution_ref"], r"^supplier-browser-[0-9a-f]{32}$")
        self.assertEqual(sum(action == "goto" for action, _ in fixture.calls), 1)

    def test_http_failure_retains_received_stage_without_claiming_a_supplier(self):
        receipt, _ = self.fetch(Fixture(status=429))
        self.assertEqual(receipt["execution_stage"], "response_received")
        self.assertEqual(receipt["http_status"], 429)
        self.assertEqual(receipt["state"], "unavailable")

    def test_missing_explicit_browser_executable_never_marks_an_attempt(self):
        fixture = Fixture()
        receipt, _ = self.fetch(fixture, browser_path="/nonexistent-supplier-browser")
        self.assertEqual(receipt["reason"], "SUPPLIER_BROWSER_EXECUTABLE_INVALID")
        self.assertEqual(receipt["execution_stage"], "not_dispatched")
        self.assertEqual(fixture.calls, [])

    def test_timeout_is_named_and_all_browser_timeouts_are_bounded(self):
        receipt, fixture = self.fetch(Fixture(fail="wait"), timeout=2)
        self.assertEqual(receipt["reason"], "SUPPLIER_BROWSER_DEADLINE")
        self.assertTrue(all(0 < kwargs["timeout"] <= 2000 for _, kwargs in fixture.calls if "timeout" in kwargs))

    def test_challenge_is_retained_as_failure_not_solved(self):
        snapshot = {"html": "<html>Our systems have detected unusual traffic</html>", "text": "Our systems have detected unusual traffic", "title": "Google Search", "text_truncated": False}
        receipt, fixture = self.fetch(Fixture(snapshot=snapshot))
        self.assertEqual(receipt["state"], "unavailable")
        self.assertEqual(receipt["reason"], "SUPPLIER_BROWSER_CHALLENGE")
        self.assertIn("unusual traffic", receipt["html"])
        self.assertEqual(fixture.closed, ["context", "browser", "driver"])

    def test_non_200_stops_without_reading_or_waiting_for_results(self):
        receipt, fixture = self.fetch(Fixture(status=429))
        self.assertEqual(receipt["http_status"], 429)
        self.assertEqual(receipt["state"], "unavailable")
        self.assertNotIn("wait", [action for action, _ in fixture.calls])

    def test_changed_final_query_is_not_a_successful_receipt(self):
        fixture = Fixture()
        fixture.url = _search_url("other query")
        receipt, fixture = self.fetch(fixture)
        self.assertEqual(receipt["reason"], "SUPPLIER_BROWSER_SCOPE_CHANGED")
        self.assertFalse(receipt["body_complete"])

    def test_google_sorry_destination_is_challenge_not_harmless_scope_rewrite(self):
        # This URL shape reproduces the two actual saved 6 October receipts.
        # The response and DOM here are fixtures, not another Google request.
        fixture = Fixture(snapshot={"html": "<html>Google verification required</html>", "text": "Google verification required", "title": "Google", "text_truncated": False})
        fixture.url = "https://www.google.com/sorry/index?continue=https%3A%2F%2Fwww.google.com%2Fsearch%3Fq%3Dfood&hl=en&q=synthetic-challenge-id"
        receipt, fixture = self.fetch(fixture)
        self.assertEqual(receipt["reason"], "SUPPLIER_BROWSER_CHALLENGE")
        self.assertEqual(receipt["state"], "unavailable")
        self.assertEqual(receipt["http_status"], 200)
        self.assertTrue(receipt["body_complete"])
        self.assertEqual(receipt["html"], "<html>Google verification required</html>")
        self.assertNotIn("wait", [action for action, _ in fixture.calls])
        self.assertEqual(fixture.closed, ["context", "browser", "driver"])
        self.assertFalse(_allow_request(fixture.url, "GET", "document", True, QUERY))

    def test_only_google_anti_abuse_urls_are_classified_as_challenge(self):
        for url, expected in (("https://www.google.com/sorry/index", True), ("https://google.co.in/sorry", True),
                              ("https://www.google.com/sorry-not-a-challenge", False),
                              ("https://example.org/sorry/index", False), ("https://www.google.com/search?q=other", False)):
            with self.subTest(url=url):
                self.assertEqual(_challenge_url(url), expected)

    def test_large_utf8_rendered_html_is_not_saved(self):
        fixture = Fixture(snapshot={"html": "₹" * 400000, "text": "Food paper", "title": "Search", "text_truncated": False})
        receipt, _ = self.fetch(fixture)
        self.assertEqual(receipt["reason"], "SUPPLIER_BROWSER_HTML_TOO_LARGE")
        self.assertIsNone(receipt["html"])

    def test_explicit_installed_browser_path_not_profile_is_used(self):
        with tempfile.NamedTemporaryFile() as installed:
            receipt, fixture = self.fetch(browser_path=installed.name)
            self.assertEqual(receipt["state"], "usable")
            launch = next(kwargs for action, kwargs in fixture.calls if action == "launch")
            self.assertEqual(launch["executable_path"], installed.name)
            self.assertNotIn("user_data_dir", launch)
        rejected, fixture = self.fetch(browser_path="relative/browser")
        self.assertEqual(rejected["state"], "rejected")
        self.assertEqual(fixture.calls, [])

    def test_invalid_time_and_headless_configuration_never_launches(self):
        for options in ({"timeout": 46}, {"timeout": True}, {"timeout": 0}, {"headless": "yes"}):
            receipt, fixture = self.fetch(**options)
            self.assertEqual(receipt["state"], "rejected")
            self.assertEqual(fixture.calls, [])


if __name__ == "__main__":
    unittest.main()
