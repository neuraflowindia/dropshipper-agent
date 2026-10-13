"""Synthetic Google HTML proves finite routing/replay, not real supplier fit."""

from copy import deepcopy
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch
from urllib.parse import parse_qs, urlsplit

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from product_radar.models import SafetyError, digest
from product_radar.research_memory import ResearchMemory
from product_radar.storage import Journal, atomic_json, load_json
from product_radar.supplier_discovery import (capture_discovered_supplier_pages, discover_saved_supplier_leads,
                                             plan_supplier_search, supplier_search_check)

NOW = "2026-10-06T08:00:00+00:00"
CONCEPT = {"description": {"label": "Food wrap paper", "function": "Wrap food for storage", "brand": "generic_or_small",
                           "category": "Home and Kitchen"}, "status": "provisional"}


class Reader:
    def __init__(self, bodies=None):
        self.calls, self.bodies = [], bodies or {}

    def __call__(self, item):
        self.calls.append(deepcopy(item))
        if urlsplit(item["url"]).hostname == "www.google.com":
            query = parse_qs(urlsplit(item["url"]).query)["q"][0]
            body = self.bodies.get(query, '<a href="/url?q=https%3A%2F%2Fsupplier.example.org%2Ffood-paper"><h3>Food wrap paper manufacturer India</h3></a>')
        else:
            body = "<h1>Food wrap paper</h1><p>Synthetic supplier page; terms are not stated.</p>"
        return {"id": item["id"], "requested_url": item["url"], "final_url": item["url"], "checked_at": NOW,
                "state": "usable", "http_status": 200, "body_complete": True, "title": "Synthetic page",
                "text": body, "html": body, "discovered_links": []}


class SupplierDiscoveryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        root = Path(self.temp.name)
        self.memory = ResearchMemory(root / "research", Journal(root / "finance"), evidence_type="synthetic")
        self.memory.put("concepts", "concept-paper", deepcopy(CONCEPT))
        self.reader = Reader()

    def search(self, reader=None):
        return discover_saved_supplier_leads(self.memory, "concept-paper", fetcher=reader or self.reader)

    def test_two_product_specific_india_queries_and_no_planning_fetch(self):
        plan = plan_supplier_search(self.memory, "concept-paper")
        self.assertEqual(len(plan["queries"]), 2)
        self.assertIn("Food wrap paper", plan["queries"][0])
        self.assertIn("Food wrap paper", plan["queries"][1])
        self.assertNotIn("Wrap food for storage", plan["queries"][1])
        self.assertTrue(all("India" in query for query in plan["queries"]))
        self.assertFalse(plan["client_ready"])
        self.assertEqual(self.reader.calls, [])

    def test_search_is_two_requests_and_deduplicated_urls_remain_unreviewed(self):
        result = self.search()
        self.assertEqual(len(self.reader.calls), 2)
        self.assertEqual(result["result"]["candidate_count"], 1)
        self.assertEqual(result["result"]["state"], "complete")
        lead = result["result"]["selected_leads"][0]
        self.assertEqual(lead["url"], "https://supplier.example.org/food-paper")
        self.assertEqual(lead["query_indices"], [0, 1])
        self.assertEqual(lead["review_state"], "unreviewed")
        check = supplier_search_check(result)
        self.assertEqual(check["supplier_search_status"], "attempted")
        self.assertEqual(check["state"], "insufficient")
        self.assertEqual(check["description_hash"], digest(CONCEPT["description"]))

    def test_replay_never_fetches_and_preserves_original_dates(self):
        first = self.search()
        second = self.search()
        self.assertTrue(second["reused"])
        self.assertEqual(first["result"], second["result"])
        self.assertEqual(second["searched_at"], NOW)
        self.assertEqual(len(self.reader.calls), 2)

    def test_explicit_zero_results_is_complete_not_unsuccessful_http(self):
        queries = plan_supplier_search(self.memory, "concept-paper")["queries"]
        reader = Reader({q: "Your search did not match any documents." for q in queries})
        result = self.search(reader)
        self.assertEqual(result["result"]["candidate_count"], 0)
        self.assertEqual(supplier_search_check(result)["supplier_search_status"], "complete")
        captured = capture_discovered_supplier_pages(self.memory, result, fetcher=reader)
        self.assertTrue(captured["no_results"])
        self.assertEqual(captured["captures"], [])
        self.assertEqual(len(reader.calls), 2)

    def test_unrecognised_layout_and_challenge_never_count_as_zero_results(self):
        queries = plan_supplier_search(self.memory, "concept-paper")["queries"]
        for body in ("Plain page with no detectable results", "Our systems have detected unusual traffic. did not match any documents"):
            # Distinct namespace avoids intentionally reusing the first failure.
            with tempfile.TemporaryDirectory() as temp:
                memory = ResearchMemory(Path(temp) / "research", Journal(Path(temp) / "finance"), evidence_type="synthetic")
                memory.put("concepts", "concept-paper", deepcopy(CONCEPT))
                result = discover_saved_supplier_leads(memory, "concept-paper", fetcher=Reader({q: body for q in queries}))
                self.assertEqual(result["result"]["state"], "blocked")
                self.assertEqual(supplier_search_check(result)["supplier_search_status"], "blocked")
                self.assertIsNone(result["searched_at"])

    def test_one_failed_query_is_partial_and_replayed_without_retry(self):
        calls = []
        def reader(item):
            calls.append(item)
            if len(calls) == 2:
                return {"state": "unavailable", "reason": "PUBLIC_HTTP_UNAVAILABLE", "http_status": 429}
            return self.reader(item)
        first = self.search(reader)
        second = self.search(reader)
        self.assertEqual(first["result"]["state"], "partial")
        self.assertEqual(second["result"], first["result"])
        self.assertEqual(len(calls), 2)

    def test_non_result_navigation_unsafe_urls_and_google_links_are_not_leads(self):
        queries = plan_supplier_search(self.memory, "concept-paper")["queries"]
        html = ('<a href="https://google.com/privacy"><h3>Privacy</h3></a>'
                '<a href="https://supplier.example.org/login"><h3>Food paper</h3></a>'
                '<a href="https://user:password@supplier.example.org/food"><h3>Food paper</h3></a>'
                '<a href="https://supplier.example.org/nav">Navigation without result heading</a>'
                '<script><a href="https://script.example.org/"><h3>Injected</h3></a></script>'
                '<a href="https://supplier.example.org/food"><h3>Food wrap paper</h3></a>')
        result = self.search(Reader({q: html for q in queries}))
        self.assertEqual(result["result"]["candidate_count"], 1)
        self.assertEqual(result["result"]["selected_leads"][0]["url"], "https://supplier.example.org/food")

    def test_three_destination_limit_and_distinct_company_preference(self):
        queries = plan_supplier_search(self.memory, "concept-paper")["queries"]
        html = "".join(f'<a href="https://{host}/food-{i}"><h3>Food wrap paper supplier</h3></a>' for i, host in enumerate(
            ["one.example.org", "one.example.org", "two.example.org", "three.example.org", "four.example.org"]))
        reader = Reader({q: html for q in queries})
        result = self.search(reader)
        self.assertEqual(len(result["result"]["selected_leads"]), 3)
        self.assertEqual(len({urlsplit(lead["url"]).hostname for lead in result["result"]["selected_leads"]}), 3)
        captured = capture_discovered_supplier_pages(self.memory, result, fetcher=reader)
        self.assertEqual(len(captured["pending_interpretation_paths"]), 3)
        self.assertEqual(len(reader.calls), 5)

    def test_destination_captures_are_reused_and_search_headings_are_not_f_terms(self):
        searched = self.search()
        first = capture_discovered_supplier_pages(self.memory, searched, fetcher=self.reader)
        second = capture_discovered_supplier_pages(self.memory, searched, fetcher=self.reader)
        self.assertEqual(len(self.reader.calls), 3)
        self.assertEqual(first["pending_interpretation_paths"], second["pending_interpretation_paths"])
        self.assertTrue(second["captures"][0]["reused"])
        item = first["captures"][0]["bundle"]["item"]
        self.assertNotIn("manufacturer India", item["pages"][0]["text"])
        self.assertIn("terms are not stated", item["pages"][0]["text"])

    def test_failed_destination_remains_failure_not_no_supplier_claim(self):
        searched = self.search()
        def unavailable(item):
            return {"id": item["id"], "state": "unavailable", "reason": "PUBLIC_HTTP_UNAVAILABLE", "checked_at": NOW}
        result = capture_discovered_supplier_pages(self.memory, searched, fetcher=unavailable)
        self.assertFalse(result["no_results"])
        self.assertEqual(result["pending_interpretation_paths"], [])
        self.assertEqual(len(result["failures"]), 1)

    def test_saved_tampering_and_forged_destination_are_not_refetched(self):
        result = self.search()
        record = load_json(result["path"], 4 * 1024 * 1024)
        record["result"]["candidate_count"] = 0
        record["content_hash"] = digest([record["plan"], record["receipts"], record["result"], record["attempted_at"]])
        atomic_json(result["path"], record)
        with self.assertRaisesRegex(SafetyError, "SAVED_SUPPLIER_SEARCH_INVALID"):
            self.search()
        with self.assertRaisesRegex(SafetyError, "SUPPLIER_SEARCH_RESULT_INVALID"):
            capture_discovered_supplier_pages(self.memory, result, fetcher=self.reader)
        self.assertEqual(len(self.reader.calls), 2)

    def test_malformed_capture_request_and_bad_saved_schema_fail_before_fetch(self):
        search = self.search()
        for bad in ({}, {"plan": {}, "result": {}}, {**search, "search_id": "different"}, {**search, "path": None}):
            with self.subTest(bad=bad), self.assertRaisesRegex(SafetyError, "SUPPLIER_SEARCH_RESULT_INVALID"):
                capture_discovered_supplier_pages(self.memory, bad, fetcher=self.reader)
        record = load_json(search["path"], 4 * 1024 * 1024)
        record["schema_version"] = True
        atomic_json(search["path"], record)
        with self.assertRaisesRegex(SafetyError, "SUPPLIER_SEARCH_RESULT_INVALID"):
            capture_discovered_supplier_pages(self.memory, search, fetcher=self.reader)
        self.assertEqual(len(self.reader.calls), 2)

    def test_changed_product_uses_new_search_but_old_result_cannot_capture(self):
        first = self.search()
        old = self.memory.snapshot()["concepts"]["concept-paper"]
        changed = {**old, "description": {**old["description"], "function": "Line baking trays"}}
        self.memory.put("concepts", "concept-paper", changed, previous=old)
        second = self.search()
        self.assertNotEqual(first["search_id"], second["search_id"])
        with self.assertRaisesRegex(SafetyError, "SUPPLIER_SEARCH_RESULT_INVALID"):
            capture_discovered_supplier_pages(self.memory, first, fetcher=self.reader)

    def test_financial_lock_is_free_while_search_waits(self):
        def reader(item):
            with self.memory.journal.locked():
                pass
            with self.assertRaisesRegex(SafetyError, "RUNNER_ALREADY_ACTIVE"):
                self.search()
            return self.reader(item)
        self.search(reader)
        self.assertEqual(len(self.reader.calls), 2)

    def test_save_interruption_reuses_completed_search(self):
        def interrupted(path, record):
            atomic_json(path, record)
            if Path(path).name == "search.json":
                raise RuntimeError("Synthetic interruption")
        with patch("product_radar.supplier_discovery.atomic_json", side_effect=interrupted), self.assertRaises(RuntimeError):
            self.search()
        replay = self.search()
        self.assertTrue(replay["reused"])
        self.assertEqual(len(self.reader.calls), 2)

    def test_exception_is_sanitised_and_does_not_trigger_retries(self):
        calls = []
        def reader(item):
            calls.append(item)
            raise RuntimeError("Do not retain this exception string")
        first = self.search(reader)
        self.search(reader)
        self.assertEqual(first["result"]["state"], "blocked")
        self.assertEqual(len(calls), 2)
        self.assertNotIn("exception string", Path(first["path"]).read_text())

    def test_saved_first_query_progress_resumes_only_second_search(self):
        def interrupted(path, record):
            atomic_json(path, record)
            if Path(path).name == "progress.json" and len(record["receipts"]) == 1:
                raise RuntimeError("Synthetic interruption after first saved search")
        with patch("product_radar.supplier_discovery.atomic_json", side_effect=interrupted), self.assertRaises(RuntimeError):
            self.search()
        self.assertEqual(len(self.reader.calls), 1)
        resumed = self.search()
        self.assertEqual(len(self.reader.calls), 2)
        self.assertEqual(resumed["result"]["state"], "complete")

    def test_corrupt_progress_is_not_discarded_or_refetched(self):
        def interrupted(path, record):
            atomic_json(path, record)
            if Path(path).name == "progress.json" and len(record["receipts"]) == 1:
                raise RuntimeError("Synthetic interruption")
        with patch("product_radar.supplier_discovery.atomic_json", side_effect=interrupted), self.assertRaises(RuntimeError):
            self.search()
        path = next((self.memory.root / "supplier_searches").glob("*/progress.json"))
        progress = load_json(path, 4 * 1024 * 1024)
        progress["receipts"] = []
        atomic_json(path, progress)
        with self.assertRaisesRegex(SafetyError, "SAVED_SUPPLIER_SEARCH_PROGRESS_INVALID"):
            self.search()
        self.assertEqual(len(self.reader.calls), 1)

    def test_blocked_check_retains_original_attempt_date(self):
        queries = plan_supplier_search(self.memory, "concept-paper")["queries"]
        first = self.search(Reader({q: "Unknown layout" for q in queries}))
        check = supplier_search_check(first)
        self.assertEqual(check["at"], first["attempted_at"])
        self.assertEqual(check["state"], "blocked")
        second = self.search()
        self.assertEqual(supplier_search_check(second), check)

    def test_excluded_or_missing_concept_never_searches(self):
        with self.assertRaisesRegex(SafetyError, "KNOWN_CONCEPT_REQUIRED"):
            discover_saved_supplier_leads(self.memory, "unknown", fetcher=self.reader)
        old = self.memory.snapshot()["concepts"]["concept-paper"]
        self.memory.put("concepts", "concept-paper", {**old, "status": "excluded"}, previous=old)
        with self.assertRaisesRegex(SafetyError, "SUPPLIER_CAPTURE_CONCEPT_EXCLUDED"):
            self.search()
        self.assertEqual(self.reader.calls, [])

    def test_browser_route_has_distinct_cache_and_check_without_automatic_fallback(self):
        queries = plan_supplier_search(self.memory, "concept-paper")["queries"]
        native = self.search(Reader({q: "JavaScript landing page" for q in queries}))
        browser = discover_saved_supplier_leads(self.memory, "concept-paper", fetcher=self.reader, route="google-browser-html")
        self.assertNotEqual(native["search_id"], browser["search_id"])
        self.assertEqual(native["result"]["state"], "blocked")
        self.assertEqual(browser["result"]["state"], "complete")
        self.assertEqual(supplier_search_check(browser)["route"], "google-browser-html")
        with self.assertRaisesRegex(SafetyError, "SUPPLIER_BROWSER_READER_REQUIRED"):
            discover_saved_supplier_leads(self.memory, "concept-paper", route="google-browser-html")


if __name__ == "__main__":
    unittest.main()
