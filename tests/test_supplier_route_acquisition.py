"""Public supplier route/receipt fixtures. No live provider or model calls."""

from copy import deepcopy
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch
from urllib.parse import urlsplit

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from product_radar.models import SafetyError, digest
from product_radar.research_memory import ResearchMemory, key
from product_radar.storage import Journal, atomic_json, load_json
from product_radar.supplier_capture_store import load_saved_supplier_input
from product_radar.supplier_effort import current_supplier_effort
from product_radar.supplier_route_acquisition import (SEARCH_ROUTES, acquire_supplier_search, collect_supplier_company_pages,
    collect_supplier_search, create_supplier_handlers, fetch_supplier_native_page,
    parse_supplier_route_receipt, supplier_route_url)
from test_supplier_discovery import CONCEPT, NOW

QUERY = "Food wrap paper"
BODIES = {
    "google-suppliers": '<a href="https://packaging.example.org/food-paper"><h3>Food wrap paper supplier India</h3></a>',
    "google-maps": '<a class="hfpxzc" aria-label="Paper Pack Manufacturing" href="https://www.google.com/maps/place/Paper+Pack/">Open</a>',
    "indiamart": '<a href="https://www.indiamart.com/proddetail/food-wrap-paper-123456.html"><h3>Food wrap paper manufacturer</h3></a>',
    "tradeindia": '<a href="https://www.tradeindia.com/products/food-wrap-paper-123456.html"><h3>Food wrapping paper supplier</h3></a>'}


def receipt(item, *, body=None, status=200, reason=None, actual=True):
    return {"schema_version": 2, "id": item["id"], "requested_url": item["url"], "final_url": item["url"],
            "checked_at": NOW, "state": "usable" if status == 200 else "unavailable",
            "reason": reason or ("Accessible fixture HTML" if status == 200 else "PUBLIC_HTTP_UNAVAILABLE"),
            "http_status": status, "body_complete": status == 200, "title": "Synthetic public page", "text": body or "",
            "html": body if status == 200 else None, "discovered_links": [],
            "execution_stage": "response_received" if actual else "not_dispatched",
            "attempt_started_at": NOW if actual else None,
            "execution_ref": "public-fetch-" + "a" * 32 if actual else None}


class Reader:
    def __init__(self, bodies=None, status=200, actual=True):
        self.bodies, self.status, self.actual, self.calls = bodies or {}, status, actual, []

    def __call__(self, item):
        self.calls.append(deepcopy(item))
        body = self.bodies.get(item["url"])
        if body is None:
            body = next((BODIES[route] for route in SEARCH_ROUTES if item["url"] == supplier_route_url(route, QUERY)),
                        "<h1>Food wrap paper</h1><p>Synthetic factory page. Price, dropshipping and subscription terms are not stated.</p>")
        return receipt(item, body=body, status=self.status, actual=self.actual)


class SupplierRouteTests(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.root = Path(folder.name)
        self.memory = ResearchMemory(self.root / "research", Journal(self.root / "finance"), evidence_type="synthetic")
        self.memory.put("concepts", "paper", deepcopy(CONCEPT))
        self.reader = Reader()

    def search(self, route="google-suppliers", **changes):
        return collect_supplier_search(self.memory, route=route, concept_ids=["paper"], query=QUERY,
                                       http_reader=self.reader, as_of=NOW, **changes)

    def test_all_four_real_routes_have_distinct_finite_public_queries(self):
        self.assertEqual({urlsplit(supplier_route_url(route, QUERY)).hostname for route in SEARCH_ROUTES},
                         {"www.google.com", "dir.indiamart.com", "www.tradeindia.com"})
        results = [self.search(route) for route in SEARCH_ROUTES]
        self.assertEqual(len(self.reader.calls), 4)
        for result in results:
            self.assertEqual(result["state"], "interpretation_pending")
            self.assertTrue(result["dispatch_started"])
            self.assertEqual(result["target_results"][0]["description_hash"], digest(CONCEPT["description"]))
            self.assertEqual(result["target_results"][0]["state"], "interpretation_pending")
        self.assertEqual(self.memory.snapshot()["supplier_assessments"], {})

    def test_google_redirects_portal_cards_and_maps_company_labels_are_parsed_not_navigation(self):
        for route in SEARCH_ROUTES:
            item = {"id": "test-public", "url": supplier_route_url(route, QUERY)}
            body = ('<a href="https://example.org/login">Login</a><script>' + BODIES[route] + '</script>' + BODIES[route])
            parsed = parse_supplier_route_receipt(route, QUERY, item, receipt(item, body=body))
            self.assertEqual(parsed["state"], "complete")
            self.assertEqual(len(parsed["results"]), 1)

    def test_indiamart_service_navigation_is_not_a_supplier_result(self):
        item = {"id": "navigation-only", "url": supplier_route_url("indiamart", QUERY)}
        body = "".join('<a href="https://' + host + '.indiamart.com/">' + label + '</a>'
            for host, label in (("lens", "Search by Image"), ("export", "Exporters"), ("seller", "Sell"), ("help", "Help"),
                                ("shipwith", "Ship with IndiaMART"), ("hindi", "Hindi"), ("flips", "Flips")))
        parsed = parse_supplier_route_receipt("indiamart", QUERY, item, receipt(item, body=body))
        self.assertEqual(parsed["state"], "extraction_pending")
        self.assertEqual(parsed["results"], [])

    def test_parser_repair_keeps_original_receipt_and_uses_only_missing_browser_fallback(self):
        acquisition = "legacy-navigation"
        folder = self.memory.root / "supplier_routes" / acquisition
        request = {"version": "supplier-public-routes-v1", "route": "indiamart", "query": QUERY,
                   "url": supplier_route_url("indiamart", QUERY)}
        item = {"id": key("supplier-route-request", [acquisition, "http"]), "url": request["url"]}
        value = receipt(item, body='<a href="https://help.indiamart.com/">Help</a>')
        parsed = parse_supplier_route_receipt("indiamart", QUERY, item, value, _legacy_services=True)
        records = [{"transport": "http", "receipt": value, "parsed": parsed}]
        original = {"schema_version": 1, "acquisition_id": acquisition, "request": request, "records": records,
                    "result": parsed, "content_hash": digest([request, records, parsed])}
        atomic_json(folder / "capture.json", original)
        browser = Reader()
        current = acquire_supplier_search(self.memory.root, route="indiamart", query=QUERY,
            acquisition_id=acquisition, as_of=NOW, http_reader=self.reader, browser_reader=browser)
        self.assertEqual(self.reader.calls, [])
        self.assertEqual(len(browser.calls), 1)
        self.assertEqual(load_json(folder / "capture.json"), original)
        self.assertNotEqual(current["reference"], str(folder / "capture.json"))
        self.assertEqual(current["record"]["records"][0]["parsed"]["state"], "extraction_pending")
        self.assertEqual(len(current["record"]["result"]["results"]), 1)
        replay = acquire_supplier_search(self.memory.root, route="indiamart", query=QUERY,
            acquisition_id=acquisition, as_of=NOW, http_reader=self.reader, browser_reader=browser)
        self.assertEqual(current["record"], replay["record"])
        self.assertEqual(len(browser.calls), 1)

    def test_parser_repair_reuses_previous_rendered_response_instead_of_repeating_browser(self):
        acquisition = "previous-rendered-repair"
        folder = self.memory.root / "supplier_routes" / acquisition
        request = {"version": "supplier-public-routes-v1", "route": "indiamart", "query": QUERY,
                   "url": supplier_route_url("indiamart", QUERY), "parser_version": 2}
        records = []
        for transport in ("http", "browser"):
            item = {"id": key("supplier-route-request", [acquisition, transport]), "url": request["url"]}
            value = receipt(item, body='<a href="https://hindi.indiamart.com/">Hindi</a>')
            records.append({"transport": transport, "receipt": value,
                "parsed": parse_supplier_route_receipt("indiamart", QUERY, item, value, _parser_version=2)})
        old = {"schema_version": 1, "acquisition_id": acquisition, "request": request,
               "records": records, "result": records[0]["parsed"],
               "content_hash": digest([request, records, records[0]["parsed"]])}
        atomic_json(folder / "capture.json", old)
        current = acquire_supplier_search(self.memory.root, route="indiamart", query=QUERY,
            acquisition_id=acquisition, as_of=NOW, http_reader=self.reader, browser_reader=self.reader)
        self.assertEqual(self.reader.calls, [])
        self.assertEqual(current["record"]["result"]["state"], "extraction_pending")
        self.assertEqual(current["record"]["result"]["results"], [])
        self.assertEqual(load_json(folder / "capture.json"), old)
        replay = acquire_supplier_search(self.memory.root, route="indiamart", query=QUERY,
            acquisition_id=acquisition, as_of=NOW, http_reader=self.reader, browser_reader=self.reader)
        self.assertEqual(current["record"], replay["record"])
        self.assertEqual(self.reader.calls, [])

    def test_family_search_is_shared_across_products_and_keeps_original_dates(self):
        first = self.search()
        second_concept = deepcopy(CONCEPT)
        second_concept["description"]["label"] = "Food wrapping sheets"
        self.memory.put("concepts", "another-paper", second_concept)
        calls = []
        second = collect_supplier_search(self.memory, route="google-suppliers", concept_ids=["another-paper"],
            query=QUERY, http_reader=self.reader, as_of=NOW, record_dispatch=lambda: calls.append(True))
        self.assertEqual(len(self.reader.calls), 1)
        self.assertEqual(calls, [])
        self.assertEqual(second["reference"], first["reference"])
        self.assertEqual(second["observed_at"], first["observed_at"])
        self.assertNotEqual(first["target_results"][0]["evidence_refs"], second["target_results"][0]["evidence_refs"])

    def test_public_acquisition_finishes_without_product_memory_and_reconciliation_reuses_it(self):
        original = self.memory.snapshot()
        with patch.object(self.memory, "snapshot", side_effect=SafetyError("SYNTHETIC_WRITER_BUSY")):
            raw = acquire_supplier_search(self.memory.root, route="indiamart", query=QUERY,
                acquisition_id="independent-read", http_reader=self.reader, as_of=NOW)
        self.assertEqual(raw["record"]["result"]["state"], "complete")
        self.assertEqual(self.memory.snapshot(), original)
        result = collect_supplier_search(self.memory, route="indiamart", concept_ids=["paper"], query=QUERY,
            acquisition_id="independent-read", http_reader=self.reader, as_of=NOW)
        self.assertEqual(result["reference"], raw["reference"])
        self.assertEqual(result["state"], "interpretation_pending")
        self.assertEqual(len(self.reader.calls), 1)

    def test_explicit_empty_search_is_completed_but_unknown_layout_is_not(self):
        for route, empty in (("google-suppliers", "Your search did not match any documents."),
                             ("google-maps", "Google Maps can't find that location."),
                             ("indiamart", "No products found."), ("tradeindia", "No search results.")):
            self.reader.bodies[supplier_route_url(route, QUERY)] = empty
            result = self.search(route)
            self.assertEqual(result["state"], "completed_no_results")
            self.assertTrue(result["dispatch_started"])
        unknown = Reader({supplier_route_url("indiamart", QUERY): "Readable page with no recognised search cards."})
        result = collect_supplier_search(self.memory, route="indiamart", concept_ids=["paper"], query=QUERY,
            acquisition_id="unrecognised-layout", http_reader=unknown, as_of=NOW)
        self.assertEqual(result["state"], "insufficient")
        self.assertNotEqual(result["target_results"][0]["state"], "completed_no_results")

    def test_one_primary_failure_uses_one_browser_fallback_and_then_replays_both(self):
        blocked = Reader(status=429)
        browser = Reader()
        first = collect_supplier_search(self.memory, route="indiamart", concept_ids=["paper"], query=QUERY,
            http_reader=blocked, browser_reader=browser, as_of=NOW)
        second = collect_supplier_search(self.memory, route="indiamart", concept_ids=["paper"], query=QUERY,
            http_reader=blocked, browser_reader=browser, as_of=NOW)
        self.assertEqual(first, second)
        self.assertEqual(len(blocked.calls), 1)
        self.assertEqual(len(browser.calls), 1)
        saved = load_json(first["reference"], 4 * 1024 * 1024)
        self.assertEqual([row["parsed"]["state"] for row in saved["records"]], ["blocked", "complete"])

    def test_opted_in_real_refusal_does_not_immediately_repeat_through_browser(self):
        for status in (401, 403, 429, 500, 502, 503, 504):
            with self.subTest(status=status):
                blocked, browser = Reader(status=status), Reader()
                acquired = acquire_supplier_search(self.memory.root, route="indiamart", query=QUERY,
                    acquisition_id="respect-refusal-" + str(status), as_of=NOW,
                    http_reader=blocked, browser_reader=browser, respect_access_waits=True)
                again = acquire_supplier_search(self.memory.root, route="indiamart", query=QUERY,
                    acquisition_id="respect-refusal-" + str(status), as_of=NOW,
                    http_reader=blocked, browser_reader=browser, respect_access_waits=True)
                self.assertEqual(acquired["record"], again["record"])
                self.assertEqual(len(blocked.calls), 1)
                self.assertEqual(browser.calls, [])
                self.assertEqual(len(acquired["record"]["records"]), 1)

    def test_opted_in_parser_failure_can_still_use_browser(self):
        unknown = Reader({supplier_route_url("indiamart", QUERY): "Readable changed layout."})
        browser = Reader()
        result = collect_supplier_search(self.memory, route="indiamart", concept_ids=["paper"], query=QUERY,
            acquisition_id="parser-alternative", as_of=NOW, http_reader=unknown, browser_reader=browser,
            respect_access_waits=True)
        self.assertEqual(result["state"], "interpretation_pending")
        self.assertEqual(len(unknown.calls), 1)
        self.assertEqual(len(browser.calls), 1)

    def test_opted_in_unproved_status_does_not_suppress_browser(self):
        unstarted, browser = Reader(status=429, actual=False), Reader()
        result = collect_supplier_search(self.memory, route="indiamart", concept_ids=["paper"], query=QUERY,
            acquisition_id="unproved-status", as_of=NOW, http_reader=unstarted, browser_reader=browser,
            respect_access_waits=True)
        self.assertEqual(result["state"], "interpretation_pending")
        self.assertEqual(len(browser.calls), 1)

    def test_opted_in_actual_challenge_does_not_trigger_same_host_browser(self):
        challenge = Reader({supplier_route_url("indiamart", QUERY): "Verify you are human"})
        browser = Reader()
        result = collect_supplier_search(self.memory, route="indiamart", concept_ids=["paper"], query=QUERY,
            acquisition_id="challenge", as_of=NOW, http_reader=challenge, browser_reader=browser,
            respect_access_waits=True)
        self.assertEqual(result["state"], "blocked")
        self.assertEqual(browser.calls, [])

    def test_opted_in_company_refusal_does_not_block_other_company(self):
        from product_radar.supplier_route_acquisition import _cached_page_reader
        calls, browser_calls = [], []
        def primary(item):
            calls.append(item["url"])
            return receipt(item, status=429) if "refusing.example.org" in item["url"] else receipt(item,
                body="A readable independent company page.")
        def browser(item):
            browser_calls.append(item["url"])
            return receipt(item, body="A browser-rendered company page.")
        read = _cached_page_reader(self.memory, primary, NOW, browser_reader=browser, respect_access_waits=True)
        refusal = read({"id": "supplier-page-refusing", "url": "https://refusing.example.org/item"})
        healthy = read({"id": "supplier-page-healthy", "url": "https://healthy.example.org/item"})
        self.assertEqual(refusal["http_status"], 429)
        self.assertEqual(healthy["state"], "usable")
        self.assertEqual(len(calls), 2)
        self.assertEqual(browser_calls, [])

    def test_installed_opted_in_supplier_handler_passes_inner_wait_policy(self):
        primary, browser = Reader(status=429), Reader()
        handlers = create_supplier_handlers(self.memory, http_reader=primary, browser_reader=browser,
            respect_access_waits=True)
        request = {"query": QUERY}
        context = {"route": "indiamart", "acquisition_id": "installed-respect-refusal", "concept_ids": ["paper"],
                   "request": request, "description_hashes": {"paper": digest(CONCEPT["description"])}, "as_of": NOW}
        first = handlers["indiamart"]["handler"](context)
        second = handlers["indiamart"]["reconcile"](context)
        self.assertEqual(first, second)
        self.assertEqual(len(primary.calls), 1)
        self.assertEqual(browser.calls, [])

    def test_native_wrapper_keeps_real_retry_after_and_exit_proof(self):
        item = {"id": "supplier-page-native", "url": supplier_route_url("indiamart", QUERY)}
        output = {"state": "captured", "navigation_started": True, "navigation_started_at": NOW,
                  "navigation_status": 429, "completed_at": NOW, "owned_process_exited": True,
                  "retry_after": "7200", "snapshot": None}
        result = fetch_supplier_native_page(item, route="indiamart", folder=self.root / "native-refusal",
            native_capture=lambda *_args, **_kwargs: output)
        self.assertEqual(result["http_status"], 429)
        self.assertEqual(result["retry_after"], "7200")
        self.assertIs(result["owned_process_exited"], True)

    def test_readable_success_does_not_trigger_unneeded_browser_or_extra_queries(self):
        browser = Reader()
        result = self.search(browser_reader=browser)
        self.assertEqual(result["state"], "interpretation_pending")
        self.assertEqual(len(self.reader.calls), 1)
        self.assertEqual(browser.calls, [])

    def test_a_sign_in_wall_in_the_signed_in_profile_is_a_retryable_route_block(self):
        # As Trends does, and as the shared reader does (signed_in_wall_response).
        import re
        from product_radar.supplier_route_acquisition import _result, _search_access_failure
        from test_indiamart_results import WALL, page
        aid = "signed-in-wall-search"
        url = supplier_route_url("indiamart", QUERY)
        item = {"id": key("supplier-route-request", [aid, "browser"]), "url": url}
        for name, profile, status, actual, expected in (("sign-in wall", "signed-in", 200, True, (True, True)),
                                                        ("challenge", None, 200, True, (True, False)),
                                                        ("429", None, 429, True, (True, True)),
                                                        ("never sent", "signed-in", 200, False, (False, False))):
            with self.subTest(name):
                value = dict(receipt(item, body=page(WALL), status=status, actual=actual), text=re.sub(r"<[^>]+>", " ", page(WALL)))
                if profile:
                    value["profile"] = profile
                parsed = parse_supplier_route_receipt("indiamart", QUERY, item, value)
                if status == 429:
                    parsed = {"state": "blocked", "reason": "PUBLIC_HTTP_UNAVAILABLE", "results": []}
                self.assertEqual(parsed["state"], "blocked")
                records = [{"transport": "browser", "receipt": value, "parsed": parsed}]
                record = {"acquisition_id": aid, "request": {"route": "indiamart", "url": url, "query": QUERY},
                          "records": records, "result": _result(records)}
                self.assertEqual(_search_access_failure(record, "saved-search.json"), expected)

    def test_challenge_including_empty_marker_is_blocked_not_empty(self):
        item = {"id": "fixture", "url": supplier_route_url("indiamart", QUERY)}
        parsed = parse_supplier_route_receipt("indiamart", QUERY, item,
            receipt(item, body="Verify you are human. No products found."))
        self.assertEqual(parsed["state"], "blocked")

    def test_a_bad_route_does_not_prevent_the_next_supplier_route(self):
        bad = Reader(status=403)
        failed = collect_supplier_search(self.memory, route="tradeindia", concept_ids=["paper"], query=QUERY,
                                        http_reader=bad, as_of=NOW)
        next_result = self.search("google-maps")
        self.assertEqual(failed["state"], "blocked")
        self.assertEqual(next_result["state"], "interpretation_pending")

    def test_save_interrupt_after_first_result_resumes_without_repeat_query(self):
        original = atomic_json
        def interrupt(path, value):
            original(path, value)
            if Path(path).name == "progress.json":
                raise RuntimeError("Synthetic interruption")
        with patch("product_radar.supplier_route_acquisition.atomic_json", side_effect=interrupt):
            with self.assertRaises(RuntimeError):
                self.search()
        result = self.search()
        self.assertEqual(result["state"], "interpretation_pending")
        self.assertEqual(len(self.reader.calls), 1)

    def test_tampered_progress_is_rejected_without_refetching(self):
        original = atomic_json
        def interrupt(path, value):
            original(path, value)
            if Path(path).name == "progress.json":
                raise RuntimeError("Synthetic interruption")
        with patch("product_radar.supplier_route_acquisition.atomic_json", side_effect=interrupt):
            with self.assertRaises(RuntimeError):
                self.search()
        path = next((self.memory.root / "supplier_routes").glob("*/progress.json"))
        saved = load_json(path, 4 * 1024 * 1024)
        saved["records"][0]["parsed"]["results"] = []
        saved["content_hash"] = digest([saved["request"], saved["records"]])
        atomic_json(path, saved)
        with self.assertRaisesRegex(SafetyError, "SAVED_SUPPLIER_ROUTE_INVALID"):
            self.search()
        self.assertEqual(len(self.reader.calls), 1)

    def test_changed_final_portal_query_cannot_be_no_result_for_our_product(self):
        for route, wrong in (("indiamart", "https://dir.indiamart.com/search.mp?ss=unrelated"),
                             ("tradeindia", "https://www.tradeindia.com/search.html?keyword=unrelated")):
            item = {"id": "fixture", "url": supplier_route_url(route, QUERY)}
            value = receipt(item, body="No products found.")
            value["final_url"] = wrong
            self.assertEqual(parse_supplier_route_receipt(route, QUERY, item, value)["state"], "failed")

    def test_browser_launch_failure_does_not_invent_a_supplier_attempt(self):
        def failed(item):
            return {**receipt(item, status=429, actual=False), "http_status": None,
                    "reason": "SUPPLIER_BROWSER_DEPENDENCY_UNAVAILABLE"}
        result = collect_supplier_search(self.memory, route="google-maps", concept_ids=["paper"], query=QUERY,
                                        http_reader=failed, as_of=NOW)
        self.assertFalse(result["dispatch_started"])
        effort = current_supplier_effort(self.memory.snapshot(), "paper", CONCEPT["description"], as_of=NOW)
        self.assertFalse(effort["effort_satisfied"])

    def test_receiver_changed_during_collection_retains_capture_but_not_current_binding(self):
        def changing(item):
            value = self.reader(item)
            old = self.memory.snapshot()["concepts"]["paper"]
            changed = deepcopy(old)
            changed["description"]["function"] = "A different product use"
            self.memory.put("concepts", "paper", changed, previous=old)
            return value
        result = collect_supplier_search(self.memory, route="indiamart", concept_ids=["paper"], query=QUERY,
                                        http_reader=changing, as_of=NOW)
        self.assertEqual(result["target_results"][0]["state"], "insufficient")
        self.assertEqual(result["target_results"][0]["evidence_refs"], [])
        self.assertTrue(Path(result["reference"]).exists())

    def test_company_reads_use_discovered_destinations_not_search_snippet_promises(self):
        self.search()
        result = collect_supplier_company_pages(self.memory, concept_ids=["paper"], page_reader=self.reader, as_of=NOW)
        self.assertEqual(result["state"], "interpretation_pending")
        self.assertTrue(result["dispatch_started"])
        self.assertEqual(result["capture_ids"], [])
        self.assertEqual(len(result["target_results"][0]["evidence_refs"]), 1)
        item = load_saved_supplier_input(self.memory, result["target_results"][0]["evidence_refs"][0])["item"]
        self.assertIn("terms are not stated", item["pages"][0]["text"])
        self.assertNotIn("supplier India", item["pages"][0]["text"])
        self.assertEqual(self.memory.snapshot()["supplier_assessments"], {})

    def test_company_replay_and_shared_pages_make_no_new_calls_or_dispatch_notifications(self):
        self.search()
        first = collect_supplier_company_pages(self.memory, concept_ids=["paper"], page_reader=self.reader, as_of=NOW)
        notifications = []
        second = collect_supplier_company_pages(self.memory, concept_ids=["paper"], page_reader=self.reader,
                                                as_of=NOW, record_dispatch=lambda: notifications.append(True))
        self.assertEqual(first, second)
        self.assertEqual(len(self.reader.calls), 2)
        self.assertEqual(notifications, [])

    def test_company_lane_with_no_destinations_is_pending_not_a_fake_finished_search(self):
        result = collect_supplier_company_pages(self.memory, concept_ids=["paper"], page_reader=self.reader, as_of=NOW)
        self.assertEqual(result["state"], "insufficient")
        self.assertFalse(result["dispatch_started"])
        self.assertEqual(result["capture_ids"], [])
        self.assertEqual(self.reader.calls, [])

    def test_company_capture_error_retains_exact_code_and_url_not_no_destination_message(self):
        self.search()
        with patch("product_radar.supplier_route_acquisition.capture_saved_supplier_bundle",
                   side_effect=SafetyError("SYNTHETIC_CAPTURE_WRITE_FAILED")):
            result = collect_supplier_company_pages(self.memory, concept_ids=["paper"],
                page_reader=self.reader, as_of=NOW)
        saved = load_json(result["reference"])
        self.assertEqual(saved["target_failures"]["paper"], [{
            "url": "https://packaging.example.org/food-paper", "reason": "SYNTHETIC_CAPTURE_WRITE_FAILED"}])
        self.assertIn("could not be captured", result["target_results"][0]["reason"])
        self.assertFalse(result["dispatch_started"])

    def test_company_frozen_urls_do_not_silently_expand_after_new_search_evidence(self):
        self.search()
        state = self.memory.snapshot()
        check_ids = [record_id for record_id, row in state["source_checks"].items() if row.get("kind") == "supplier_route_search"]
        frozen_urls = {"paper": ["https://packaging.example.org/food-paper"]}
        frozen_checks = {"paper": check_ids}
        self.search("tradeindia")
        result = collect_supplier_company_pages(self.memory, concept_ids=["paper"], page_reader=self.reader, as_of=NOW,
                                                candidate_urls=frozen_urls, candidate_check_ids=frozen_checks)
        self.assertEqual(result["state"], "interpretation_pending")
        self.assertEqual(len(result["target_results"][0]["evidence_refs"]), 1)
        self.assertEqual(len(self.reader.calls), 3)
        self.assertEqual(self.reader.calls[-1]["url"], frozen_urls["paper"][0])

    def test_a_mismatched_frozen_supplier_target_does_not_block_healthy_company_reads(self):
        self.memory.put("concepts", "other-paper", deepcopy(CONCEPT))
        collect_supplier_search(self.memory, route="google-suppliers", concept_ids=["paper", "other-paper"], query=QUERY,
                                http_reader=self.reader, as_of=NOW)
        state = self.memory.snapshot()
        checks = [record_id for record_id, row in state["source_checks"].items()
                  if row.get("kind") == "supplier_route_search" and row.get("concept_id") == "other-paper"]
        result = collect_supplier_company_pages(self.memory, concept_ids=["paper", "other-paper"], page_reader=self.reader,
            as_of=NOW, candidate_check_ids={"paper": ["not-a-saved-check"], "other-paper": checks},
            candidate_urls={"paper": ["https://packaging.example.org/food-paper"], "other-paper": ["https://packaging.example.org/food-paper"]})
        targets = {row["concept_id"]: row for row in result["target_results"]}
        self.assertEqual(targets["paper"]["state"], "insufficient")
        self.assertEqual(targets["other-paper"]["state"], "interpretation_pending")
        self.assertEqual(len(self.reader.calls), 2)

    def test_no_destination_replay_retains_original_status_date_and_new_destination_is_new_work(self):
        empty = collect_supplier_company_pages(self.memory, concept_ids=["paper"], page_reader=self.reader, as_of=NOW)
        replay = collect_supplier_company_pages(self.memory, concept_ids=["paper"], page_reader=self.reader,
                                                as_of="2026-10-07T08:00:00+00:00")
        self.assertEqual(empty, replay)
        self.search()
        actual = collect_supplier_company_pages(self.memory, concept_ids=["paper"], page_reader=self.reader, as_of=NOW)
        self.assertNotEqual(empty["acquisition_id"], actual["acquisition_id"])
        self.assertEqual(actual["state"], "interpretation_pending")

    def test_controller_registry_covers_all_five_routes_without_a_paid_journal_entry(self):
        handlers = create_supplier_handlers(self.memory, http_reader=self.reader, browser_reader=self.reader, page_reader=self.reader)
        self.assertEqual(set(handlers), {*SEARCH_ROUTES, "company-pages"})
        result = handlers["indiamart"]["handler"]({"route": "indiamart", "acquisition_id": "controller-example", "concept_ids": ["paper"],
                                        "request": {"query": QUERY, "as_of": NOW}})
        self.assertEqual(set(result), {"schema_version", "route", "acquisition_id", "observed_at", "state", "dispatch_started",
                                       "reference", "capture_ids", "observation_ids", "target_results", "reason",
                                       "failure_scope", "retryable"})
        self.assertEqual(result["failure_scope"], "question")
        self.assertFalse(result["retryable"])
        with self.memory.journal.locked():
            self.assertEqual(self.memory.journal.state()[1], {})

    def test_native_browser_receipt_preserves_actual_navigation_not_wrapper_start(self):
        item = {"id": "native-page", "url": supplier_route_url("google-maps", QUERY)}
        def native(url, expression, folder):
            value = {"state": "captured", "navigation_started": True, "navigation_started_at": NOW,
                     "navigation_status": 200, "completed_at": NOW, "owned_process_exited": True,
                     "snapshot": {"url": url, "html": BODIES["google-maps"], "text": "Paper Pack Manufacturing", "title": "Maps", "links": []}}
            atomic_json(Path(folder) / "native_result.json", value)
            return value
        value = fetch_supplier_native_page(item, route="google-maps", folder=self.root / "native", native_capture=native)
        self.assertEqual(value["state"], "usable")
        self.assertEqual(value["attempt_started_at"], NOW)
        self.assertEqual(value["execution_stage"], "response_received")
        self.assertTrue(Path(value["native_reference"]).exists())
        self.assertTrue(value["native_result_saved"])
        self.assertEqual(parse_supplier_route_receipt("google-maps", QUERY, item, value)["state"], "complete")

    def test_native_startup_failure_and_oversized_dom_are_not_a_usable_source(self):
        item = {"id": "native-page", "url": supplier_route_url("google-maps", QUERY)}
        value = fetch_supplier_native_page(item, route="google-maps", folder=self.root / "native-startup", native_capture=lambda *_args: {
            "state": "failed", "navigation_started": False, "stage": "browser_launch",
            "reason": "NATIVE_PUBLIC_WSL_INTEROP_BLOCKED"})
        self.assertEqual(value["execution_stage"], "not_dispatched")
        self.assertIsNone(value["attempt_started_at"])
        self.assertEqual(value["native_reason"], "NATIVE_PUBLIC_WSL_INTEROP_BLOCKED")
        self.assertEqual(value["native_stage"], "browser_launch")
        self.assertFalse(value["native_result_saved"])
        self.assertFalse(Path(value["native_reference"]).exists())
        oversized = fetch_supplier_native_page(item, route="google-maps", folder=self.root / "native-large", native_capture=lambda *_args: {
            "state": "captured", "navigation_started": True, "navigation_started_at": NOW, "navigation_status": 200,
            "completed_at": NOW, "owned_process_exited": True,
            "snapshot": {"url": item["url"], "html": "₹" * 400000, "text": "Paper", "links": []}})
        self.assertEqual(oversized["state"], "unavailable")
        self.assertFalse(oversized["body_complete"])

    def test_uncertain_native_navigation_is_not_falsely_reported_as_not_started(self):
        item = {"id": "native-uncertain", "url": supplier_route_url("google-maps", QUERY)}
        value = fetch_supplier_native_page(item, route="google-maps", folder=self.root / "native-uncertain", native_capture=lambda *_args: {
            "state": "failed", "navigation_started": None, "stage": "browser_result", "reason": "NATIVE_CHILD_RESULT_UNCERTAIN"})
        self.assertEqual(value["execution_stage"], "unknown")
        self.assertIsNone(value["attempt_started_at"])
        self.assertIsNone(value["execution_ref"])
        def browser(request): return dict(value, id=request["id"], requested_url=request["url"])
        result = collect_supplier_search(self.memory, route="google-maps", concept_ids=["paper"], query=QUERY,
            acquisition_id="uncertain-with-no-proved-http", http_reader=Reader(status=403, actual=False), browser_reader=browser, as_of=NOW)
        self.assertIsNone(result["dispatch_started"])
        self.assertEqual(result["target_results"][0]["state"], "failed")
        actual_http = collect_supplier_search(self.memory, route="google-maps", concept_ids=["paper"], query=QUERY,
            acquisition_id="uncertain-after-real-http", http_reader=Reader(status=403), browser_reader=browser, as_of=NOW)
        self.assertTrue(actual_http["dispatch_started"])

    def test_invalid_query_and_unknown_route_are_rejected_before_a_provider_read(self):
        for query in ("", "http://example.org/", "a" * 121, "bad\nquery"):
            with self.assertRaisesRegex(SafetyError, "SUPPLIER_ROUTE_QUERY_INVALID"):
                collect_supplier_search(self.memory, route="indiamart", concept_ids=["paper"], query=query,
                                        http_reader=self.reader, as_of=NOW)
        with self.assertRaisesRegex(SafetyError, "SUPPLIER_ROUTE_INVALID"):
            supplier_route_url("unknown", QUERY)
        self.assertEqual(self.reader.calls, [])


if __name__ == "__main__":
    unittest.main()
