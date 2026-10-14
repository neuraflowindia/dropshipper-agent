"""Google Maps and the retired Google Search route through the installed shared supplier leaves.

Pages are the sanitised saved bytes in tests/fixtures/supplier_routes, read
through the real owned HTTP reader (offline fake socket) and the real owned
browser receipt wrapper (offline fake native child). No website is contacted.
"""

from copy import deepcopy
from datetime import timedelta
from pathlib import Path
import unittest
from unittest.mock import patch

from product_radar.models import digest, timestamp
from product_radar.public_pages import _PageText, fetch_public_page
from product_radar.source_coverage import acquisition_access_scope, prepare_source_tasks, source_access_wait
from product_radar.storage import atomic_json
from product_radar.supplier_effort import VERSION as EFFORT_VERSION
from product_radar.supplier_route_acquisition import create_supplier_handlers, fetch_supplier_native_page, supplier_route_url
from product_radar.supplier_search import current_supplier_search_checks, prepare_supplier_request, supplier_request_for_state
import test_shared_supplier_search as shared
from test_public_pages import Response, Transport, resolver

FIXTURES = Path(__file__).resolve().parent / "fixtures" / "supplier_routes"
GATE = "google-search-enablejs-gate-noscript-2026-10-14.html"
SHELL = "google-maps-search-http-app-shell-2026-10-14.html"
WALL = "indiamart-search-login-wall-http-2026-10-14.html"
WAITING = "This route is waiting after an actual shared route failure."
RESULTS = "google-maps-search-rendered-results-2026-10-08.html"
NO_RESULTS = "google-maps-search-rendered-no-results-2026-10-08.html"


def page(name):
    return (FIXTURES / name).read_text(encoding="utf-8")


class GoogleSupplierRouteTests(unittest.TestCase):
    setUp_shared = shared.SharedSupplierTests.setUp
    checks = shared.SharedSupplierTests.checks
    owners = shared.SharedSupplierTests.owners

    def setUp(self):
        self.setUp_shared()
        self.http_pages = {"google-maps": (page(SHELL), 200, {}), "google-suppliers": (page(GATE), 200, {}),
                           "indiamart": (page(SHELL), 200, {})}
        self.browser_page = (page(RESULTS), None, 200)
        self.native_options, self.unclosed, self.redirect_to = [], False, None

    def http(self, item):
        """The real owned HTTP reader, served the saved page without a network."""
        self.http_calls.append(deepcopy(item))
        route = next(r for r in ("google-suppliers", "google-maps", "indiamart") if supplier_route_url(r, self.query) == item["url"]
                     or item["url"] in self.other_urls.get(r, ()))
        body, status, headers = self.http_pages[route]
        page_response = Response(body=body.encode(), status=status, headers={"Content-Type": "text/html; charset=utf-8", **headers})
        # A real HTTP redirect, followed by the owned reader, when a test asks for one.
        responses = ([Response(status=302, headers={"Location": self.redirect_to}), page_response]
                     if self.redirect_to else [page_response])
        value = fetch_public_page(item, resolver=resolver, retain_transport=True, connection_factory=Transport(responses))
        value.update(checked_at=self.at, attempt_started_at=self.at)
        if self.unclosed:
            value["owned_worker_exited"] = False
        return value

    other_urls = {}

    def native(self, item, *, route, folder):
        """The real owned-browser wrapper around an offline native child."""
        self.browser_calls.append(deepcopy(item))
        html, final, status = self.browser_page

        def child(url, expression, target, **options):
            self.native_options.append(options)
            snapshot = None
            if status == 200:
                text = _PageText(url); text.feed(html); text.close()
                snapshot = {"url": final or url, "html": html, "text": text.extracted()["text"],
                            "title": "Google Maps", "links": []}
            output = {"state": "captured" if snapshot else "failed", "navigation_started": True,
                      "navigation_started_at": self.at, "navigation_status": status, "completed_at": self.at,
                      "owned_process_exited": True, "snapshot": snapshot,
                      **({"retry_after": "3600"} if status == 429 else {})}
            target.mkdir(parents=True, exist_ok=True)
            atomic_json(target / "native_result.json", output)
            atomic_json(target / "native_dispatch.json", {"url": url, "navigation_started": True,
                                                          "navigation_started_at": self.at})
            return output
        return fetch_supplier_native_page(item, route=route, folder=folder, native_capture=child)

    def handlers(self, routes=("google-maps",)):
        installed = create_supplier_handlers(self.memory, http_reader=self.http, respect_access_waits=True,
                                             shared_acquisitions=True)
        return {route: installed[route] for route in routes}

    def prepare(self, route, *, run, request=None, queries=None, as_of=None):
        self.run = run
        questions = [{"route": route, "request": request or {"query": query, "country": "IN"}, "concept_ids": [self.cid]}
                     for query in (queries or [self.query])]
        return prepare_source_tasks(self.memory, questions, run_id=run, as_of=as_of or self.at)["task_ids"]

    def run_checks(self, handlers):
        with patch("product_radar.supplier_search.fetch_supplier_native_page", side_effect=self.native):
            return self.checks(handlers)

    def owner(self, route, transport):
        return next((aid, row) for aid, row in self.owners().items() if row["binding"]["route"] == route
                    and row["binding"]["request"]["transport"] == transport)

    def response(self, owner):
        return self.memory.snapshot()["source_checks"][owner["active_response_id"]]

    def effort(self, route):
        rows = [row for row in self.memory.snapshot()["source_checks"].values()
                if row.get("kind") == EFFORT_VERSION and row.get("route") == route and row.get("concept_id") == self.cid]
        self.assertEqual(len(rows), 1)
        return rows[0]

    def maps_http_then_browser_request(self):
        handlers = self.handlers()
        self.prepare("google-maps", run="maps-http")
        self.assertEqual(self.run_checks(handlers)["counts"], {"failed": 1})
        _, owner = self.owner("google-maps", "public-http")
        response = self.response(owner)
        self.assertEqual((response["source_state"], response["parse_reason"], response["access"]["http_status"]),
                         ("failed", "PUBLIC_TEXT_UNAVAILABLE", 200))
        return handlers, supplier_request_for_state(self.memory.snapshot(), "google-maps", self.query)

    # Repair 1: the owner-permitted Maps browser read after the HTTP app shell.

    def test_maps_application_shell_permits_one_bounded_browser_read_of_the_same_search(self):
        handlers, request = self.maps_http_then_browser_request()
        self.assertEqual(request["transport"], "public-browser")
        self.assertEqual(request["url"], supplier_route_url("google-maps", self.query))
        self.prepare("google-maps", run="maps-browser", request=request)
        self.assertEqual(self.run_checks(handlers)["counts"], {"acquired": 1})
        self.assertEqual((len(self.http_calls), len(self.browser_calls)), (1, 1))
        # The ordinary bounded snapshot: no Maps-specific wait, scroll or click.
        self.assertEqual(self.native_options, [{}])
        state = self.memory.snapshot()
        checks = current_supplier_search_checks(state, self.cid, description_hash=digest(state["concepts"][self.cid]["description"]))
        candidates = [row for check in checks.values() for row in check["candidates"]]
        self.assertEqual(len(candidates), 10)
        self.assertTrue(all(row["url"].startswith("https://www.google.com/maps/place/") for row in candidates))
        # Saved originals answer later passes; no third read of either transport.
        self.assertEqual(self.run_checks(handlers)["handler_calls"], 0)
        self.prepare("google-maps", run="maps-browser-again", request=request)
        self.assertEqual(self.run_checks(handlers)["handler_calls"], 0)
        self.assertEqual((len(self.http_calls), len(self.browser_calls)), (1, 1))

    def test_rewritten_maps_address_with_no_results_is_a_completed_empty_search(self):
        handlers, request = self.maps_http_then_browser_request()
        final = ("https://www.google.com/maps/search/" + "+".join((self.query + " manufacturer supplier India").split())
                 + "/@28.5,77.1,11z?entry=fixture")
        self.browser_page = (page(NO_RESULTS), final, 200)
        self.prepare("google-maps", run="maps-browser", request=request)
        self.assertEqual(self.run_checks(handlers)["counts"], {"completed_no_results": 1})
        _, owner = self.owner("google-maps", "public-browser")
        self.assertTrue(self.response(owner)["complete"])
        self.assertEqual(self.response(owner)["source_state"], "empty")

    def test_maps_browser_429_is_a_recorded_access_wait_not_a_retry(self):
        handlers, request = self.maps_http_then_browser_request()
        self.browser_page = (None, None, 429)
        self.prepare("google-maps", run="maps-browser", request=request)
        self.assertEqual(self.run_checks(handlers)["counts"], {"blocked": 1})
        _, owner = self.owner("google-maps", "public-browser")
        self.assertEqual(self.response(owner)["source_state"], "blocked")
        wait = source_access_wait(self.memory.snapshot(), acquisition_access_scope(owner), as_of=self.at)
        self.assertEqual(wait["phase"], "waiting")
        self.assertEqual(wait["next_retry_at"], (timestamp(self.at) + timedelta(hours=1)).isoformat(timespec="seconds"))
        self.assertEqual(self.run_checks(handlers)["handler_calls"], 0)
        self.assertEqual(len(self.browser_calls), 1)

    def test_maps_browser_consent_page_is_blocked_and_never_clicked_through(self):
        handlers, request = self.maps_http_then_browser_request()
        self.browser_page = ("<html><body><h1>Before you continue to Google</h1><button>Reject all</button>"
                             "<button>Accept all</button></body></html>",
                             "https://consent.google.com/ml?continue=https://www.google.com/maps/search/", 200)
        self.prepare("google-maps", run="maps-browser", request=request)
        self.assertEqual(self.run_checks(handlers)["counts"], {"blocked": 1})
        _, owner = self.owner("google-maps", "public-browser")
        response = self.response(owner)
        self.assertEqual((response["source_state"], response["parse_reason"]), ("blocked", "SUPPLIER_ROUTE_CHALLENGE"))
        wait = source_access_wait(self.memory.snapshot(), acquisition_access_scope(owner), as_of=self.at)
        self.assertEqual(wait["phase"], "waiting")
        failure = self.memory.snapshot()["source_checks"][wait["failure_id"]]["binding"]
        self.assertIs(failure["challenge"], True)
        self.assertEqual(self.native_options, [{}])
        self.assertEqual(self.run_checks(handlers)["handler_calls"], 0)

    def test_application_shell_permission_is_maps_only(self):
        # A directory's script-only shell is not Maps: no browser read follows.
        handlers = self.handlers(("indiamart",))
        self.prepare("indiamart", run="directory-shell")
        self.assertEqual(self.run_checks(handlers)["counts"], {"failed": 1})
        self.assertEqual(supplier_request_for_state(self.memory.snapshot(), "indiamart", self.query)["transport"],
                         "public-http")
        self.assertEqual(self.browser_calls, [])

    def test_indiamart_login_wall_is_a_shared_access_wait_not_a_browser_fallback(self):
        # Repair 6: the plain read's sign-in wall used to be an "unreadable
        # layout" that queued a browser read of the same wall.
        self.http_pages["indiamart"] = (page(WALL), 200, {})
        handlers = self.handlers(("indiamart",))
        self.prepare("indiamart", run="directory-wall")
        self.assertEqual(self.run_checks(handlers)["counts"], {"blocked": 1})
        _, owner = self.owner("indiamart", "public-http")
        response = self.response(owner)
        self.assertEqual((response["source_state"], response["parse_reason"]), ("blocked", "SUPPLIER_ROUTE_CHALLENGE"))
        wait = source_access_wait(self.memory.snapshot(), acquisition_access_scope(owner), as_of=self.at)
        self.assertEqual((wait["phase"], wait["scope"]["host"]), ("waiting", "dir.indiamart.com"))
        self.assertIs(self.memory.snapshot()["source_checks"][wait["failure_id"]]["binding"]["challenge"], True)
        self.assertEqual(supplier_request_for_state(self.memory.snapshot(), "indiamart", self.query)["transport"],
                         "public-http")
        self.assertEqual(self.run_checks(handlers)["handler_calls"], 0)
        self.assertEqual((len(self.http_calls), self.browser_calls), (1, []))

    def test_indiamart_wall_pauses_the_route_holds_other_questions_and_escalates_its_probe(self):
        # The full challenge flow formerly proved on Google Search, on IndiaMART's wall.
        second = "sink caddy organiser"
        self.http_pages["indiamart"] = (page(WALL), 200, {})
        self.other_urls = {"indiamart": {supplier_route_url("indiamart", second),
                                         supplier_route_url("indiamart", "sink caddy rack")}}
        handlers = self.handlers(("indiamart",))
        earlier = (timestamp(self.at) - timedelta(seconds=1)).isoformat(timespec="seconds")
        self.prepare("indiamart", run="directory-wall", as_of=earlier)
        task_ids = self.prepare("indiamart", run="directory-wall", queries=[second])
        result = self.run_checks(handlers)
        # One real read; the second product question is not sent to the walled route.
        self.assertEqual((result["handler_calls"], len(self.http_calls), result["counts"]), (1, 1, {"blocked": 2}))
        held = next(row for row in result["results"] if row.get("actual_attempt") is False)
        self.assertEqual(held["reason"], WAITING)
        aid, owner = next(iter(self.owners().items()))
        response = self.response(owner)
        self.assertEqual((response["source_state"], response["parse_reason"], response["access"]["http_status"]),
                         ("blocked", "SUPPLIER_ROUTE_CHALLENGE", 200))
        state = self.memory.snapshot()
        wait = source_access_wait(state, acquisition_access_scope(owner), as_of=self.at)
        self.assertEqual((wait["phase"], wait["acquisition_id"]), ("waiting", aid))
        self.assertEqual(wait["next_retry_at"], (timestamp(self.at) + timedelta(seconds=300)).isoformat(timespec="seconds"))
        self.assertIs(state["source_checks"][wait["failure_id"]]["binding"]["challenge"], True)
        sent = next(state["query_tasks"][tid] for tid in task_ids if state["query_tasks"][tid].get("attempt_refs"))
        self.assertEqual((sent["status"], sent["reason"], sent["retryable"], sent["next_retry_at"]),
                         ("blocked", "SUPPLIER_ROUTE_CHALLENGE", False, None))
        effort = self.effort("indiamart")
        self.assertEqual((effort["internal_outcome"], effort["supplier_search_status"], effort["effort_satisfied"]),
                         ("blocked", "blocked", True))
        # Never a browser read of a walled search, and no repeat while waiting.
        self.assertEqual(supplier_request_for_state(state, "indiamart", self.query)["transport"], "public-http")
        self.assertEqual(self.run_checks(handlers)["handler_calls"], 0)
        # After the wait, exactly one new caller probes; a second wall waits an hour.
        self.at = (timestamp(self.at) + timedelta(seconds=301)).isoformat(timespec="seconds")
        self.prepare("indiamart", run="directory-after-wait", queries=["sink caddy rack"])
        result = self.run_checks(handlers)
        self.assertEqual((result["handler_calls"], result["counts"]), (1, {"blocked": 1}))
        wait = source_access_wait(self.memory.snapshot(), acquisition_access_scope(owner), as_of=self.at)
        self.assertEqual(wait["phase"], "waiting")
        self.assertEqual(wait["next_retry_at"], (timestamp(self.at) + timedelta(hours=1)).isoformat(timespec="seconds"))
        self.assertEqual(self.memory.snapshot()["source_checks"][wait["failure_id"]]["binding"]["attempt"], 2)
        self.assertEqual((len(self.http_calls), self.browser_calls), (2, []))

    def test_wall_saved_by_an_older_parser_no_longer_authorises_its_queued_browser_read(self):
        # Reads saved under parser 4 called the wall an unreadable layout and
        # queued a browser read. The current parser's view of the same saved
        # bytes is now required, so the queued browser read is never sent.
        self.http_pages["indiamart"] = (page(WALL), 200, {})
        handlers = self.handlers(("indiamart",))
        with patch("product_radar.supplier_route_acquisition.PARSER_VERSION", 4), \
                patch("product_radar.supplier_search.PARSER_VERSION", 4):
            self.prepare("indiamart", run="old-parser")
            self.assertEqual(self.run_checks(handlers)["counts"], {"insufficient": 1})
            self.assertEqual(supplier_request_for_state(self.memory.snapshot(), "indiamart", self.query)["transport"],
                             "public-browser")
        self.assertEqual(supplier_request_for_state(self.memory.snapshot(), "indiamart", self.query)["transport"],
                         "public-http")
        queued = dict(prepare_supplier_request({"route": "indiamart", "request": {"query": self.query, "country": "IN"}}),
                      transport="public-browser")
        self.prepare("indiamart", run="queued-browser", request=queued)
        result = self.run_checks(handlers)
        self.assertEqual(result["handler_calls"], 0)
        self.assertEqual(result["results"][0]["reason"], "SUPPLIER_BROWSER_FALLBACK_PROOF_REQUIRED")
        self.assertEqual((len(self.http_calls), self.browser_calls), (1, []))

    def test_redirected_maps_shell_cannot_authorise_a_browser_read(self):
        # Repair 4: the plain read must have ended at exactly the requested
        # Maps address. A redirected shell answered a different page.
        self.redirect_to = "/maps/search/?api=1&query=something+else+entirely"
        handlers = self.handlers()
        self.prepare("google-maps", run="maps-redirected")
        self.assertEqual(self.run_checks(handlers)["counts"], {"failed": 1})
        _, owner = self.owner("google-maps", "public-http")
        response = self.response(owner)
        self.assertEqual((response["source_state"], response["parse_reason"], response["access"]["http_status"]),
                         ("failed", "PUBLIC_TEXT_UNAVAILABLE", 200))
        self.assertEqual(supplier_request_for_state(self.memory.snapshot(), "google-maps", self.query)["transport"],
                         "public-http")
        self.assertEqual(self.browser_calls, [])

    def test_unclosed_maps_shell_cannot_authorise_a_browser_read(self):
        self.unclosed = True
        handlers = self.handlers()
        self.prepare("google-maps", run="maps-unclosed")
        self.run_checks(handlers)
        self.assertEqual(supplier_request_for_state(self.memory.snapshot(), "google-maps", self.query)["transport"],
                         "public-http")
        self.assertEqual(self.browser_calls, [])

    def test_maps_http_refusal_cannot_authorise_a_browser_read(self):
        self.http_pages["google-maps"] = ("Too many requests", 429, {"Retry-After": "3600"})
        handlers = self.handlers()
        self.prepare("google-maps", run="maps-refused")
        self.assertEqual(self.run_checks(handlers)["counts"], {"blocked": 1})
        self.assertEqual(supplier_request_for_state(self.memory.snapshot(), "google-maps", self.query)["transport"],
                         "public-http")
        self.assertEqual(self.browser_calls, [])

    # Retired: Google Search supplier results are never sent and never hold Maps.

    def test_retired_google_search_is_never_sent_and_never_holds_the_google_host(self):
        handlers = self.handlers(("google-suppliers", "google-maps"))
        self.assertNotIn("handler", handlers["google-suppliers"])
        # An old saved Google Search question and a current Maps question share a run.
        self.prepare("google-suppliers", run="google-host")
        self.prepare("google-maps", run="google-host")
        result = self.run_checks(handlers)
        self.assertEqual(result["handler_calls"], 1)
        self.assertEqual(result["counts"], {"not_connected": 1, "failed": 1})
        self.assertEqual([item["url"] for item in self.http_calls], [supplier_route_url("google-maps", self.query)])
        # No Google Search refusal exists, so the Google host has no wait and
        # the owner-permitted Maps browser read proceeds immediately.
        _, owner = self.owner("google-maps", "public-http")
        self.assertIsNone(source_access_wait(self.memory.snapshot(), acquisition_access_scope(owner), as_of=self.at))
        request = supplier_request_for_state(self.memory.snapshot(), "google-maps", self.query)
        self.assertEqual(request["transport"], "public-browser")
        self.prepare("google-maps", run="maps-browser", request=request)
        self.assertEqual(self.run_checks(handlers)["counts"], {"acquired": 1})
        self.assertEqual(len(self.browser_calls), 1)
        self.assertFalse(any(row["binding"]["route"] == "google-suppliers" for row in self.owners().values()))
        self.assertFalse(any(row.get("kind") == EFFORT_VERSION and row.get("route") == "google-suppliers"
                             for row in self.memory.snapshot()["source_checks"].values()))

    def test_maps_browser_sign_in_page_is_blocked_and_never_signed_into(self):
        # Repair 5: Google's sign-in interstitial is an access challenge.
        handlers, request = self.maps_http_then_browser_request()
        self.browser_page = ("<html><body><h1>Sign in</h1><p>Use your Google Account</p></body></html>",
                             "https://accounts.google.com/ServiceLogin?continue=https://www.google.com/maps/search/", 200)
        self.prepare("google-maps", run="maps-browser", request=request)
        self.assertEqual(self.run_checks(handlers)["counts"], {"blocked": 1})
        _, owner = self.owner("google-maps", "public-browser")
        response = self.response(owner)
        self.assertEqual((response["source_state"], response["parse_reason"]), ("blocked", "SUPPLIER_ROUTE_CHALLENGE"))
        wait = source_access_wait(self.memory.snapshot(), acquisition_access_scope(owner), as_of=self.at)
        self.assertEqual(wait["phase"], "waiting")
        self.assertIs(self.memory.snapshot()["source_checks"][wait["failure_id"]]["binding"]["challenge"], True)
        self.assertEqual(self.run_checks(handlers)["handler_calls"], 0)
        self.assertEqual(len(self.browser_calls), 1)

if __name__ == "__main__":
    unittest.main()
