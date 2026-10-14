"""Google Maps and the retired Google Search route through the installed shared supplier leaves.

Google Maps is read straight in the owned browser (owner decision D11, 14
October 2026): no plain read comes first. Old queued plain or throwaway
browser reads of Maps and IndiaMART are closed (superseded when asked again)
and never sent. TradeIndia keeps the plain read first, and the guards on its
browser fallback and its host's refusal back-off are proved here.

Pages are the sanitised saved bytes in tests/fixtures/supplier_routes, read
through the real owned HTTP reader (offline fake socket) and the real owned
browser receipt wrapper (offline fake native child). No website is contacted.
"""

from copy import deepcopy
from datetime import timedelta
from pathlib import Path
import unittest
from unittest.mock import patch

from product_radar.models import SafetyError, digest, timestamp
from product_radar.public_pages import _PageText, fetch_public_page
from product_radar.source_coverage import (acquisition_access_scope, prepare_all_source_tasks, prepare_source_tasks,
    product_source_request_builder, source_access_wait)
from product_radar.storage import atomic_json
from product_radar.supplier_effort import VERSION as EFFORT_VERSION
from product_radar.supplier_route_acquisition import create_supplier_handlers, fetch_supplier_native_page, supplier_route_url
from product_radar.supplier_search import current_supplier_search_checks, prepare_supplier_request, supplier_request_for_state
import test_shared_supplier_search as shared
from test_public_pages import Response, Transport, resolver

FIXTURES = Path(__file__).resolve().parent / "fixtures" / "supplier_routes"
GATE = "google-search-enablejs-gate-noscript-2026-10-14.html"
SHELL = "google-maps-search-http-app-shell-2026-10-14.html"
RESULTS = "google-maps-search-rendered-results-2026-10-08.html"
LAYOUT = "<html><body><main>Readable different layout without any recognisable result card</main></body></html>"
CHALLENGE = "<html><body><h1>Please verify you are human</h1></body></html>"
WAITING = "This route is waiting after an actual shared route failure."
NO_RESULTS = "google-maps-search-rendered-no-results-2026-10-08.html"


def page(name):
    return (FIXTURES / name).read_text(encoding="utf-8")


class GoogleSupplierRouteTests(unittest.TestCase):
    setUp_shared = shared.SharedSupplierTests.setUp
    checks = shared.SharedSupplierTests.checks
    signed_in_page = shared.SharedSupplierTests.signed_in_page
    owners = shared.SharedSupplierTests.owners

    def setUp(self):
        self.setUp_shared()
        self.http_pages = {"google-maps": (page(SHELL), 200, {}), "google-suppliers": (page(GATE), 200, {}),
                           "tradeindia": (page(SHELL), 200, {})}
        self.browser_page = (page(RESULTS), None, 200)
        self.native_options, self.unclosed, self.other_urls = [], False, {}

    def http(self, item):
        """The real owned HTTP reader, served the saved page without a network."""
        self.http_calls.append(deepcopy(item))
        route = next(r for r in ("google-suppliers", "google-maps", "tradeindia") if supplier_route_url(r, self.query) == item["url"]
                     or item["url"] in self.other_urls.get(r, ()))
        body, status, headers = self.http_pages[route]
        page_response = Response(body=body.encode(), status=status, headers={"Content-Type": "text/html; charset=utf-8", **headers})
        value = fetch_public_page(item, resolver=resolver, retain_transport=True, connection_factory=Transport([page_response]))
        value.update(checked_at=self.at, attempt_started_at=self.at)
        if self.unclosed:
            value["owned_worker_exited"] = False
        return value

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
                                             shared_acquisitions=True, signed_in_root=self.signed_root)
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

    def maps_browser_request(self):
        """Planning asks Maps straight in the browser; nothing has been read yet."""
        handlers = self.handlers()
        request = supplier_request_for_state(self.memory.snapshot(), "google-maps", self.query)
        self.assertEqual(request, prepare_supplier_request({"route": "google-maps",
                                                            "request": {"query": self.query, "country": "IN"}}))
        self.assertEqual((self.http_calls, self.browser_calls), ([], []))
        return handlers, request

    # Owner decision D11: Maps is read straight in the owned browser.

    def test_maps_is_read_straight_in_the_browser_without_a_plain_read(self):
        handlers, request = self.maps_browser_request()
        self.assertEqual(request["transport"], "public-browser")
        self.assertEqual(request["url"], supplier_route_url("google-maps", self.query))
        self.prepare("google-maps", run="maps-browser")
        self.assertEqual(self.run_checks(handlers)["counts"], {"acquired": 1})
        self.assertEqual((len(self.http_calls), len(self.browser_calls)), (0, 1))
        self.assertEqual(self.owner("google-maps", "public-browser")[1]["binding"]["request"], request)
        # The ordinary bounded snapshot: no Maps-specific wait, scroll or click.
        self.assertEqual(self.native_options, [{}])
        state = self.memory.snapshot()
        checks = current_supplier_search_checks(state, self.cid, description_hash=digest(state["concepts"][self.cid]["description"]))
        candidates = [row for check in checks.values() for row in check["candidates"]]
        self.assertEqual(len(candidates), 10)
        self.assertTrue(all(row["url"].startswith("https://www.google.com/maps/place/") for row in candidates))
        # Saved originals answer later passes; no second read of either transport.
        self.assertEqual(self.run_checks(handlers)["handler_calls"], 0)
        self.prepare("google-maps", run="maps-browser-again", request=request)
        self.assertEqual(self.run_checks(handlers)["handler_calls"], 0)
        self.assertEqual((len(self.http_calls), len(self.browser_calls)), (0, 1))

    def test_rewritten_maps_address_with_no_results_is_a_completed_empty_search(self):
        handlers, request = self.maps_browser_request()
        final = ("https://www.google.com/maps/search/" + "+".join((self.query + " manufacturer supplier India").split())
                 + "/@28.5,77.1,11z?entry=fixture")
        self.browser_page = (page(NO_RESULTS), final, 200)
        self.prepare("google-maps", run="maps-browser", request=request)
        self.assertEqual(self.run_checks(handlers)["counts"], {"completed_no_results": 1})
        _, owner = self.owner("google-maps", "public-browser")
        self.assertTrue(self.response(owner)["complete"])
        self.assertEqual(self.response(owner)["source_state"], "empty")

    def test_maps_browser_429_is_a_recorded_access_wait_not_a_retry(self):
        handlers, request = self.maps_browser_request()
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
        handlers, request = self.maps_browser_request()
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

    def test_a_plain_reads_script_only_shell_never_authorises_a_browser_read(self):
        # The Maps application-shell permission is gone with the Maps plain read.
        handlers = self.handlers(("tradeindia",))
        self.prepare("tradeindia", run="directory-shell")
        self.assertEqual(self.run_checks(handlers)["counts"], {"failed": 1})
        self.assertEqual(supplier_request_for_state(self.memory.snapshot(), "tradeindia", self.query)["transport"],
                         "public-http")
        self.assertEqual(self.browser_calls, [])

    def queue_old_reads(self, run):
        """Questions queued under the old rules: a plain read first, a browser read after proof."""
        base = {"query": self.query, "country": "IN"}
        old = [("google-maps", "public-http"), ("indiamart", "public-http"), ("indiamart", "public-browser")]
        for route, transport in old:
            self.prepare(route, run=run, request=dict(prepare_supplier_request({"route": route, "request": base}),
                                                     transport=transport))
        return [task_id for task_id, task in self.memory.snapshot()["query_tasks"].items()
                if task.get("kind") == "all_source_task_v1" and task.get("run_id") == run]

    def test_old_queued_plain_and_throwaway_reads_of_maps_and_indiamart_are_closed_unsent(self):
        handlers = self.handlers(("google-maps", "indiamart"))
        task_ids = self.queue_old_reads("old-queue")
        result = self.run_checks(handlers)
        self.assertEqual((result["handler_calls"], result["counts"]), (0, {"not_connected": 3}))
        self.assertEqual({row["reason"] for row in result["results"]}, {"SUPPLIER_ROUTE_TRANSPORT_REPLACED"})
        for task_id in task_ids:
            task = self.memory.snapshot()["query_tasks"][task_id]
            self.assertEqual((task["status"], task["reconcile_required"], task["reason"]),
                             ("blocked", False, "SUPPLIER_ROUTE_TRANSPORT_REPLACED"))
        self.assertEqual((self.http_calls, self.browser_calls, self.owners()), ([], [], {}))
        # Closed: a later pass changes nothing and still sends nothing.
        before = self.memory.snapshot()["query_tasks"]
        self.assertEqual(self.run_checks(handlers)["counts"], {"not_connected": 3})
        self.assertEqual(self.memory.snapshot()["query_tasks"], before)
        self.assertEqual({route: supplier_request_for_state(self.memory.snapshot(), route, self.query)["transport"]
                          for route in ("google-maps", "indiamart")},
                         {"google-maps": "public-browser", "indiamart": "signed-in-browser"})
        # The installed collector refuses them too, before any intent or read.
        base = {"query": self.query, "country": "IN"}
        context = {"task_id": "direct", "acquisition_id": "direct-old-read", "as_of": self.at, "concept_ids": [self.cid],
                   "request": dict(prepare_supplier_request({"route": "indiamart", "request": base}), transport="public-browser")}
        with self.assertRaisesRegex(SafetyError, "SUPPLIER_ROUTE_TRANSPORT_REPLACED"):
            handlers["indiamart"]["handler"](context)
        self.assertFalse((self.memory.root / "supplier_routes" / "shared" / "direct-old-read").exists())
        # A signed-in read is IndiaMART's alone.
        with self.assertRaisesRegex(SafetyError, "SUPPLIER_SHARED_TRANSPORT_INVALID"):
            prepare_supplier_request({"route": "google-maps", "request": dict(base, transport="signed-in-browser")})

    def test_asking_again_on_the_routes_own_first_read_supersedes_the_old_queued_reads(self):
        run = "same-run"
        old_ids = self.queue_old_reads(run)
        prepare_all_source_tasks(self.memory, run_id=run, concept_ids=[self.cid], as_of=self.at,
                                 request_builder=product_source_request_builder(shared_suppliers=True))
        state = self.memory.snapshot()
        new = {task["route"]: (task_id, task) for task_id, task in state["query_tasks"].items()
               if task.get("run_id") == run and task.get("route") in ("google-maps", "indiamart")
               and task["request"].get("transport") in ("public-browser", "signed-in-browser")
               and task_id not in old_ids}
        self.assertEqual({route: task["request"]["transport"] for route, (_, task) in new.items()},
                         {"google-maps": "public-browser", "indiamart": "signed-in-browser"})
        for task_id in old_ids:
            old = state["query_tasks"][task_id]
            marker = old["superseded_targets"][self.cid]
            self.assertEqual(marker["task_id"], new[old["route"]][0])
            self.assertEqual(marker["reason"], "This route now starts with its own browser read; the old queued read is saved history.")
        handlers = self.handlers(("google-maps", "indiamart"))
        self.run = run
        result = self.run_checks(handlers)
        superseded = [row for row in result["results"] if row["task_id"] in old_ids]
        self.assertEqual({row["state"] for row in superseded}, {"superseded"})
        self.assertEqual(self.http_calls, [])
        # A different-transport question that is not a replacement is refused as a supersession.
        with self.assertRaisesRegex(SafetyError, "SOURCE_SUPERSEDED_BINDING_INVALID"):
            prepare_source_tasks(self.memory, [{"route": "tradeindia", "concept_ids": [self.cid],
                "request": dict(prepare_supplier_request({"route": "tradeindia", "request": {"query": self.query, "country": "IN"}}),
                                transport="public-browser"), "supersedes_task_ids": [old_ids[0]]}], run_id=run, as_of=self.at)

    def test_a_retired_route_question_left_mid_send_is_closed_not_left_waiting(self):
        handlers = self.handlers(("google-suppliers",))
        task_id = self.prepare("google-suppliers", run="retired-mid-send")[0]
        task = self.memory.snapshot()["query_tasks"][task_id]
        self.memory.put("query_tasks", task_id, dict(task, status="dispatching", reconcile_required=True), previous=task)
        result = self.run_checks(handlers)
        self.assertEqual((result["handler_calls"], result["results"][0]["state"], result["results"][0]["reason"]),
                         (0, "not_connected", "SUPPLIER_ROUTE_RETIRED"))
        closed = self.memory.snapshot()["query_tasks"][task_id]
        self.assertEqual((closed["status"], closed["reconcile_required"]), ("blocked", False))
        self.assertEqual(self.run_checks(handlers)["counts"], {"not_connected": 1})
        self.assertEqual(self.http_calls, [])
        # Asked directly for a new read, the retired route answers the same way.
        resolution = handlers["google-suppliers"]["resolve_source"]({"task_id": "direct", "acquisition_id": "direct",
            "request": {"query": self.query, "country": "IN"}, "as_of": self.at})
        self.assertEqual(resolution, {"action": "not_connected", "reason": "SUPPLIER_ROUTE_RETIRED"})

    # TradeIndia: the browser fallback needs a closed, complete, unreadable plain page.

    def tradeindia_layout_then_request(self, run="tradeindia-plain"):
        self.http_pages["tradeindia"] = (LAYOUT, 200, {})
        handlers = self.handlers(("tradeindia",))
        self.prepare("tradeindia", run=run)
        self.run_checks(handlers)
        return handlers, supplier_request_for_state(self.memory.snapshot(), "tradeindia", self.query)

    def test_a_closed_unreadable_plain_page_permits_one_browser_read(self):
        _, request = self.tradeindia_layout_then_request()
        self.assertEqual(request["transport"], "public-browser")

    def test_an_unclosed_plain_read_cannot_authorise_a_browser_read(self):
        self.unclosed = True
        _, request = self.tradeindia_layout_then_request()
        self.assertEqual((request["transport"], self.browser_calls), ("public-http", []))

    def test_a_plain_read_refusal_cannot_authorise_a_browser_read(self):
        self.http_pages["tradeindia"] = ("Too many requests", 429, {"Retry-After": "3600"})
        handlers = self.handlers(("tradeindia",))
        self.prepare("tradeindia", run="tradeindia-refused")
        self.assertEqual(self.run_checks(handlers)["counts"], {"blocked": 1})
        self.assertEqual(supplier_request_for_state(self.memory.snapshot(), "tradeindia", self.query)["transport"], "public-http")
        self.assertEqual(self.browser_calls, [])

    def test_a_page_the_current_parser_reads_differently_no_longer_authorises_its_queued_browser_read(self):
        handlers, request = self.tradeindia_layout_then_request()
        self.assertEqual(request["transport"], "public-browser")
        self.prepare("tradeindia", run="queued-browser", request=request)
        # The saved plain page, read again by the current parser, is no longer
        # an unreadable layout: the queued browser read is never sent.
        changed = {"state": "complete", "reason": "The readable bounded search explicitly reported no results.", "results": []}
        with patch("product_radar.supplier_search.parse_supplier_route_receipt", return_value=changed):
            self.assertEqual(supplier_request_for_state(self.memory.snapshot(), "tradeindia", self.query)["transport"],
                             "public-http")
            result = self.run_checks(handlers)
        self.assertEqual((result["handler_calls"], result["results"][0]["reason"]),
                         (0, "SUPPLIER_BROWSER_FALLBACK_PROOF_REQUIRED"))
        self.assertEqual((len(self.http_calls), self.browser_calls), (1, []))

    def test_a_refusing_supplier_host_holds_its_other_questions_and_escalates_its_probe(self):
        second, third = "sink caddy organiser", "sink caddy rack"
        self.http_pages["tradeindia"] = (CHALLENGE, 200, {})
        self.other_urls = {"tradeindia": {supplier_route_url("tradeindia", second), supplier_route_url("tradeindia", third)}}
        handlers = self.handlers(("tradeindia",))
        earlier = (timestamp(self.at) - timedelta(seconds=1)).isoformat(timespec="seconds")
        self.prepare("tradeindia", run="refusing-host", as_of=earlier)
        task_ids = self.prepare("tradeindia", run="refusing-host", queries=[second])
        result = self.run_checks(handlers)
        # One real read; the second product question is not sent to the refusing host.
        self.assertEqual((result["handler_calls"], len(self.http_calls), result["counts"]), (1, 1, {"blocked": 2}))
        held = next(row for row in result["results"] if row.get("actual_attempt") is False)
        self.assertEqual(held["reason"], WAITING)
        aid, owner = next(iter(self.owners().items()))
        response = self.response(owner)
        self.assertEqual((response["source_state"], response["parse_reason"], response["access"]["http_status"]),
                         ("blocked", "SUPPLIER_ROUTE_CHALLENGE", 200))
        state = self.memory.snapshot()
        wait = source_access_wait(state, acquisition_access_scope(owner), as_of=self.at)
        self.assertEqual((wait["phase"], wait["acquisition_id"], wait["scope"]["host"]), ("waiting", aid, "www.tradeindia.com"))
        self.assertEqual(wait["next_retry_at"], (timestamp(self.at) + timedelta(seconds=300)).isoformat(timespec="seconds"))
        self.assertIs(state["source_checks"][wait["failure_id"]]["binding"]["challenge"], True)
        sent = next(state["query_tasks"][tid] for tid in task_ids if state["query_tasks"][tid].get("attempt_refs"))
        self.assertEqual((sent["status"], sent["reason"], sent["retryable"], sent["next_retry_at"]),
                         ("blocked", "SUPPLIER_ROUTE_CHALLENGE", False, None))
        effort = self.effort("tradeindia")
        self.assertEqual((effort["internal_outcome"], effort["supplier_search_status"], effort["effort_satisfied"]),
                         ("blocked", "blocked", True))
        # Never a browser read of a refusing host, and no repeat while waiting.
        self.assertEqual(supplier_request_for_state(state, "tradeindia", self.query)["transport"], "public-http")
        self.assertEqual(self.run_checks(handlers)["handler_calls"], 0)
        # After the wait, exactly one new caller probes; a second refusal waits an hour.
        self.at = (timestamp(self.at) + timedelta(seconds=301)).isoformat(timespec="seconds")
        self.prepare("tradeindia", run="after-wait", queries=[third])
        result = self.run_checks(handlers)
        self.assertEqual((result["handler_calls"], result["counts"]), (1, {"blocked": 1}))
        wait = source_access_wait(self.memory.snapshot(), acquisition_access_scope(owner), as_of=self.at)
        self.assertEqual(wait["phase"], "waiting")
        self.assertEqual(wait["next_retry_at"], (timestamp(self.at) + timedelta(hours=1)).isoformat(timespec="seconds"))
        self.assertEqual(self.memory.snapshot()["source_checks"][wait["failure_id"]]["binding"]["attempt"], 2)
        self.assertEqual((len(self.http_calls), self.browser_calls), (2, []))

    # Retired: Google Search supplier results are never sent and never hold Maps.

    def test_retired_google_search_is_never_sent_and_never_holds_the_google_host(self):
        handlers = self.handlers(("google-suppliers", "google-maps"))
        self.assertNotIn("handler", handlers["google-suppliers"])
        # An old saved Google Search question and a current Maps question share a run.
        self.prepare("google-suppliers", run="google-host")
        self.prepare("google-maps", run="google-host")
        result = self.run_checks(handlers)
        self.assertEqual(result["handler_calls"], 1)
        self.assertEqual(result["counts"], {"not_connected": 1, "acquired": 1})
        self.assertEqual(self.http_calls, [])
        self.assertEqual([item["url"] for item in self.browser_calls], [supplier_route_url("google-maps", self.query)])
        # No Google Search refusal exists, so the Google host has no wait.
        _, owner = self.owner("google-maps", "public-browser")
        self.assertIsNone(source_access_wait(self.memory.snapshot(), acquisition_access_scope(owner), as_of=self.at))
        self.assertFalse(any(row["binding"]["route"] == "google-suppliers" for row in self.owners().values()))
        self.assertFalse(any(row.get("kind") == EFFORT_VERSION and row.get("route") == "google-suppliers"
                             for row in self.memory.snapshot()["source_checks"].values()))

    def test_maps_browser_sign_in_page_is_blocked_and_never_signed_into(self):
        # Repair 5: Google's sign-in interstitial is an access challenge.
        handlers, request = self.maps_browser_request()
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
