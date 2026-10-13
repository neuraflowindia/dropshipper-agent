"""Synthetic saved Trends dispatch tests; no browser, model or paid requests."""

from copy import deepcopy
from datetime import date
import hashlib
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from product_radar.market_followups import execute_trends_check, prepare_trends_check
from product_radar.models import SafetyError, digest
from product_radar.research_memory import ResearchMemory, change
from product_radar.research_operations import import_trends_evidence
from product_radar.report_selection import build_report_pool
from product_radar.storage import Journal, atomic_bytes, atomic_json, load_json
from product_radar.service import policy_for_fit
from product_radar.trends_capture import CAPTURE_VERSION, explore_url
from product_radar.providers.trends_browser import parse_trends_csv
from test_report_selection import NOW, fixture
from test_trends_browser import metadata, weekly


class MarketFollowupTests(unittest.TestCase):
    def setUp(self):
        # The unmarked report fixture has thirteen historical criteria.
        policy = policy_for_fit("fit-13-v1")
        historical_policy = patch("product_radar.service.load_policy", return_value=policy)
        historical_policy.start(); self.addCleanup(historical_policy.stop)
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.memory = ResearchMemory(self.root / "research", Journal(self.root / "finance"),
                                     evidence_type="synthetic")
        state = fixture().state
        state["market_reviews"].clear()
        self.memory.commit("synthetic-fixture", [change(table, record_id, row)
                           for table, rows in state.items() for record_id, row in rows.items()])
        self.calls = []
        self.term = "synthetic product"

    def request(self, **changes):
        return prepare_trends_check(self.memory, concept_id="c000", term=self.term,
                                    as_of=NOW, **changes)

    def capture(self, term, acquisition_id, *, root, browser_path):
        self.calls.append((term, acquisition_id, root, browser_path))
        raw = weekly(label=term + ": (India)")
        folder = Path(root) / acquisition_id
        csv_path = folder / "multiTimeline.csv"
        atomic_bytes(csv_path, raw)
        receipt = {"schema_version": 1, "route": "owned_playwright_browser", "capture_version": CAPTURE_VERSION,
                "state": "Usable", "acquisition_id": acquisition_id, "term": term,
                "geo": "IN", "time_range": "today 12-m", "kind": "search_term",
                "property": "web", "scope_verified": True, "collected_at": NOW,
                "started_at": NOW, "completed_at": NOW, "chart_rows_match": True,
                "requested_url": explore_url(term), "receipt_path": str(folder / "receipt.json"),
                "owned_browser_closed": False,
                "source_reference": "https://trends.google.com/trends/explore?geo=IN",
                "csv_path": str(csv_path), "csv_sha256": hashlib.sha256(raw).hexdigest(),
                "reason": "Synthetic scope-checked CSV, not live browser proof."}
        parsed = parse_trends_csv(raw, receipt)
        rows = [["x", "y1"], *[[date.fromisoformat(p["start"]).strftime("%b %d, %Y"), p["raw_value"]]
                               for p in parsed["series"]]]
        atomic_json(folder / "scope.json", {"url": explore_url(term), "text": "\n".join([
            term, "Search term", "India", "Past 12 months", "All categories", "Web Search"]), "rows": rows})
        atomic_json(folder / "receipt.json", receipt)
        return receipt

    def execute(self, request, capture=None):
        return execute_trends_check(self.memory, request, root=self.root,
                                    capture=capture or self.capture, browser_path="fixture-browser")

    def test_prepare_known_passing_candidate_missing_coverage_is_saved_and_repeatable(self):
        before = self.memory.snapshot()
        request = self.request()
        self.assertEqual(request["description_hash"], digest(before["concepts"]["c000"]["description"]))
        self.assertEqual(request["concept_id"], "c000")
        self.assertEqual(request["planned_at"], NOW)
        self.assertEqual(request["term"], self.term)
        self.assertEqual(request["route"], "public_playwright_csv")
        self.assertEqual(load_json(self.memory.root / "trends_checks" / request["id"] / "request.json"), request)
        self.assertEqual(self.request(), request)
        self.assertEqual(self.memory.snapshot(), before)
        self.assertEqual(self.calls, [])

    def test_unknown_concept_is_rejected_without_capture(self):
        with self.assertRaisesRegex(SafetyError, "KNOWN_CONCEPT_REQUIRED"):
            prepare_trends_check(self.memory, concept_id="unknown", term=self.term, as_of=NOW)
        self.assertEqual(self.calls, [])

    def test_below_threshold_candidate_is_not_dispatched(self):
        previous = self.memory.snapshot()["assessments"]["c000"]
        changed = deepcopy(previous)
        for row in changed["result"]["criteria"]:
            if row["state"] == "Meets":
                row["state"] = "Unclear"
        self.memory.put("assessments", "c000", changed, previous=previous)
        with self.assertRaisesRegex(SafetyError, "USEFUL_MARKET_CHECK_REQUIRED"):
            self.request()
        self.assertEqual(self.calls, [])

    def test_already_covered_candidate_does_not_create_a_redundant_check(self):
        row = fixture().state["market_reviews"]["review0"]
        self.memory.put("market_reviews", "review0", row)
        self.assertEqual(build_report_pool(self.memory, as_of=NOW)["needed_market_work"], [])
        with self.assertRaisesRegex(SafetyError, "USEFUL_MARKET_CHECK_REQUIRED"):
            self.request()

    def test_prepared_check_is_not_collected_after_coverage_is_satisfied(self):
        request = self.request()
        self.memory.put("market_reviews", "review0", fixture().state["market_reviews"]["review0"])
        result = self.execute(request)
        self.assertEqual(result["state"], "No longer needed")
        self.assertIsNone(result["evidence_id"])
        self.assertEqual(self.calls, [])

    def test_prepared_check_is_not_collected_after_product_is_excluded(self):
        request = self.request()
        old = self.memory.snapshot()["concepts"]["c000"]
        self.memory.put("concepts", "c000", dict(old, status="excluded"), previous=old)
        with self.assertRaisesRegex(SafetyError, "TRENDS_PRODUCT_INACTIVE"):
            self.execute(request)
        self.assertEqual(self.calls, [])

    def test_two_assessable_but_nonpositive_families_are_not_a_missing_coverage_task(self):
        import_trends_evidence(self.memory, concept_id="c000", acquisition_id="flat-fixture",
                               csv_bytes=weekly([20] * 54, label=self.term + ": (India)"),
                               metadata=metadata(term=self.term, collected_at=NOW))
        pool = build_report_pool(self.memory, as_of=NOW)
        decision = next(row for row in pool["decisions"] if row["concept_id"] == "c000")
        self.assertIn("weak_market_signal", decision["codes"])
        self.assertEqual(pool["needed_market_work"], [])
        with self.assertRaisesRegex(SafetyError, "USEFUL_MARKET_CHECK_REQUIRED"):
            self.request()
        self.assertEqual(self.calls, [])

    def test_invalid_terms_are_rejected_before_request_or_capture(self):
        for term in ("", "   ", "a" * 121, "bad\nterm"):
            with self.subTest(term=term):
                with self.assertRaisesRegex(SafetyError, "TRENDS_TERM_INVALID"):
                    prepare_trends_check(self.memory, concept_id="c000", term=term, as_of=NOW)
        self.assertEqual(self.calls, [])

    def test_omitted_term_uses_shared_compact_query_and_explicit_term_stays_authoritative(self):
        before = self.memory.snapshot()
        with patch("product_radar.product_queries.product_query_terms", return_value=[
                {"text": "doorway pull-up bar", "scope": "subtype", "source": "trends"}]) as profiles:
            request = prepare_trends_check(self.memory, concept_id="c000", as_of=NOW)
            profiles.assert_called_once_with(self.memory, "c000", source="trends", limit=1)
            explicit = self.request()
            self.assertEqual(profiles.call_count, 1)
        self.assertEqual(request["term"], "doorway pull-up bar")
        self.assertEqual(request["phrase_basis"], "shared_product_query_profile")
        self.assertEqual(explicit["term"], self.term)
        self.assertEqual(explicit["phrase_basis"], "operator_selected_product_type")
        self.assertEqual(self.memory.snapshot(), before)
        self.assertEqual(self.calls, [])

    def test_frozen_trends_request_replay_never_asks_for_new_query_wording(self):
        request = self.request()
        with patch("product_radar.product_queries.product_query_terms", side_effect=AssertionError("No profile lookup on frozen replay")):
            first = self.execute(request)
            second = self.execute(request)
        self.assertEqual(first["evidence_id"], second["evidence_id"])
        self.assertEqual(len(self.calls), 1)

    def test_changed_product_before_capture_rejects_saved_request(self):
        request = self.request()
        old = self.memory.snapshot()["concepts"]["c000"]
        changed = deepcopy(old)
        changed["description"]["function"] = "A different synthetic product function"
        self.memory.put("concepts", "c000", changed, previous=old)
        with self.assertRaisesRegex(SafetyError, "TRENDS_PRODUCT_CHANGED"):
            self.execute(request)
        self.assertEqual(self.calls, [])
        self.assertEqual(self.memory.snapshot()["trends_evidence"], {})

    def test_changed_product_during_capture_does_not_attach_evidence(self):
        request = self.request()
        def changed_capture(*args, **kwargs):
            receipt = self.capture(*args, **kwargs)
            old = self.memory.snapshot()["concepts"]["c000"]
            changed = deepcopy(old)
            changed["description"]["mechanism"] = "A different synthetic mechanism"
            self.memory.put("concepts", "c000", changed, previous=old)
            return receipt
        with self.assertRaisesRegex(SafetyError, "TRENDS_PRODUCT_CHANGED"):
            self.execute(request, changed_capture)
        self.assertEqual(len(self.calls), 1)
        self.assertEqual(self.memory.snapshot()["trends_evidence"], {})

    def test_usable_capture_imports_once_and_replay_preserves_facts_and_dates(self):
        request = self.request()
        before = self.memory.snapshot()
        first = self.execute(request)
        after = self.memory.snapshot()
        self.assertEqual(first["state"], "Usable")
        self.assertIsNotNone(first["evidence_id"])
        self.assertEqual(first["model_calls"], 0)
        self.assertEqual(first["provider_requests"], 0)
        self.assertFalse(first["client_ready"])
        evidence = after["trends_evidence"][first["evidence_id"]]
        self.assertEqual(evidence["concept_id"], "c000")
        self.assertEqual(evidence["acquisition_id"], request["acquisition_id"])
        self.assertEqual(evidence["parsed"]["metadata"]["collected_at"], NOW)
        for table in ("concepts", "descriptions", "assessments", "observations",
                      "published_history", "issue_reservations"):
            self.assertEqual(after[table], before[table])
        second = self.execute(request)
        self.assertEqual(first, second)
        self.assertEqual(self.memory.snapshot(), after)
        self.assertEqual(len(self.calls), 1)

    def test_tampered_saved_csv_is_rejected_without_recapture(self):
        request = self.request()
        self.execute(request)
        folder = self.memory.root / "trends_checks" / request["id"]
        receipt = load_json(folder / "capture.json")
        atomic_bytes(receipt["csv_path"], b"tampered synthetic CSV")
        with self.assertRaisesRegex(SafetyError, "TRENDS_SAVED_CAPTURE_HASH_MISMATCH"):
            self.execute(request)
        self.assertEqual(len(self.calls), 1)

    def test_incomplete_original_receipt_cannot_supply_market_proof(self):
        request = self.request()
        def incomplete(*args, **kwargs):
            receipt = self.capture(*args, **kwargs)
            receipt.pop("completed_at")
            atomic_json(receipt["receipt_path"], receipt)
            return receipt
        with self.assertRaisesRegex(SafetyError, "TRENDS_SAVED_CAPTURE_INVALID_OR_INCOMPLETE"):
            self.execute(request, incomplete)
        self.assertEqual(self.memory.snapshot()["trends_evidence"], {})
        self.assertEqual(self.memory.snapshot()["source_checks"], {})
        with self.assertRaisesRegex(SafetyError, "TRENDS_SAVED_CAPTURE_INVALID_OR_INCOMPLETE"):
            self.execute(request, incomplete)
        self.assertEqual(len(self.calls), 1)

    def test_completed_chart_value_mismatch_cannot_supply_market_proof(self):
        request = self.request()
        def wrong_chart(*args, **kwargs):
            receipt = self.capture(*args, **kwargs)
            scope_path = Path(receipt["receipt_path"]).with_name("scope.json")
            scope = load_json(scope_path)
            scope["rows"][10][1] = "99"
            atomic_json(scope_path, scope)
            return receipt
        with self.assertRaisesRegex(SafetyError, "TRENDS_SAVED_CAPTURE_SCOPE_OR_CHART_MISMATCH"):
            self.execute(request, wrong_chart)
        self.assertEqual(self.memory.snapshot()["trends_evidence"], {})
        self.assertEqual(self.memory.snapshot()["source_checks"], {})

    def test_replaced_working_receipt_is_rejected_against_original_acquisition(self):
        request = self.request()
        self.execute(request)
        before = self.memory.snapshot()
        working_path = self.memory.root / "trends_checks" / request["id"] / "capture.json"
        receipt = load_json(working_path)
        receipt["reason"] = "Replacement working receipt must not overwrite acquisition meaning."
        atomic_json(working_path, receipt)
        with self.assertRaisesRegex(SafetyError, "TRENDS_RECEIPT_CONFLICT"):
            self.execute(request)
        self.assertEqual(self.memory.snapshot(), before)
        self.assertEqual(len(self.calls), 1)

    def test_complete_acquisition_recovers_without_a_second_capture(self):
        request = self.request()
        receipt = self.capture(request["term"], request["acquisition_id"],
                               root=self.root / "media/live/trends", browser_path="fixture-browser")
        self.assertFalse((self.memory.root / "trends_checks" / request["id"] / "capture.json").exists())
        result = self.execute(request)
        self.assertEqual(result["state"], "Usable")
        self.assertIsNotNone(result["evidence_id"])
        self.assertEqual(len(self.calls), 1)
        self.assertEqual(load_json(receipt["receipt_path"]), receipt)
        after = self.memory.snapshot()
        self.assertEqual(self.execute(request), result)
        self.assertEqual(self.memory.snapshot(), after)
        self.assertEqual(len(self.calls), 1)

    def test_replayed_old_export_does_not_become_fresh_market_proof(self):
        request = self.request()
        self.execute(request)
        self.execute(request)
        pool = build_report_pool(self.memory, as_of="2026-10-14T10:00:00+00:00")
        checks = pool["facts_by_concept"]["c000"]["market_checks"]
        trend = next(row for row in checks if row["source"] == "trends")
        self.assertEqual(trend["state"], "Stale")
        evidence = next(iter(self.memory.snapshot()["trends_evidence"].values()))
        self.assertEqual(evidence["parsed"]["metadata"]["collected_at"], NOW)
        self.assertEqual(len(self.calls), 1)

    def test_csv_from_another_in_project_acquisition_is_not_substituted(self):
        request = self.request()
        def wrong_file(*args, **kwargs):
            receipt = self.capture(*args, **kwargs)
            unrelated = self.root / "media/live/trends/other-acquisition/multiTimeline.csv"
            atomic_bytes(unrelated, Path(receipt["csv_path"]).read_bytes())
            return dict(receipt, csv_path=str(unrelated))
        with self.assertRaisesRegex(SafetyError, "TRENDS_CAPTURE_PATH_INVALID"):
            self.execute(request, wrong_file)
        self.assertEqual(self.memory.snapshot()["trends_evidence"], {})

    def test_invalid_completion_date_cannot_supply_market_proof(self):
        request = self.request()
        def invalid_date(*args, **kwargs):
            receipt = self.capture(*args, **kwargs)
            receipt["completed_at"] = "not a timestamp"
            atomic_json(receipt["receipt_path"], receipt)
            return receipt
        with self.assertRaisesRegex(SafetyError, "TRENDS_SAVED_CAPTURE_INVALID_OR_INCOMPLETE"):
            self.execute(request, invalid_date)
        self.assertEqual(self.memory.snapshot()["trends_evidence"], {})

    def test_csv_outside_project_root_is_not_imported(self):
        request = self.request()
        with tempfile.TemporaryDirectory() as other:
            outside = Path(other) / "external.csv"
            raw = weekly(label=self.term + ": (India)")
            atomic_bytes(outside, raw)
            def outside_capture(*args, **kwargs):
                return dict(self.capture(*args, **kwargs), csv_path=str(outside),
                            csv_sha256=hashlib.sha256(raw).hexdigest())
            with self.assertRaisesRegex(SafetyError, "TRENDS_CAPTURE_PATH_INVALID"):
                self.execute(request, outside_capture)
        self.assertEqual(self.memory.snapshot()["trends_evidence"], {})

    def test_unknown_receipt_state_is_not_accepted_as_success(self):
        request = self.request()
        def wrong_state(*args, **kwargs):
            return dict(self.capture(*args, **kwargs), state="Succeeded")
        with self.assertRaisesRegex(SafetyError, "TRENDS_RECEIPT_INVALID"):
            self.execute(request, wrong_state)
        self.assertEqual(self.memory.snapshot()["trends_evidence"], {})
        self.assertEqual(self.memory.snapshot()["source_checks"], {})

    def test_receipt_scope_must_be_verified_before_it_can_supply_market_coverage(self):
        request = self.request()
        def unverified(*args, **kwargs):
            return dict(self.capture(*args, **kwargs), scope_verified=False)
        with self.assertRaisesRegex(SafetyError, "TRENDS_RECEIPT_CONFLICT"):
            self.execute(request, unverified)
        pool = build_report_pool(self.memory, as_of=NOW)
        self.assertEqual(pool["eligible"], [])
        self.assertEqual(len(pool["needed_market_work"]), 1)

    def test_receipt_identity_mismatch_never_attaches_to_the_product(self):
        request = self.request()
        for field in ("term", "acquisition_id"):
            with self.subTest(field=field):
                def wrong_capture(*args, **kwargs):
                    return dict(self.capture(*args, **kwargs), **{field: "wrong-synthetic-value"})
                if field == "acquisition_id":
                    request = prepare_trends_check(self.memory, concept_id="c000", term="second synthetic term", as_of=NOW)
                with self.assertRaisesRegex(SafetyError, "TRENDS_RECEIPT_MISMATCH"):
                    self.execute(request, wrong_capture)
                self.assertEqual(self.memory.snapshot()["trends_evidence"], {})

    def test_blocked_failure_is_saved_not_zero_demand_and_is_not_automatically_retried(self):
        request = self.request()
        calls = []
        def blocked(term, acquisition_id, **kwargs):
            calls.append(acquisition_id)
            return {"state": "Blocked", "term": term, "acquisition_id": acquisition_id,
                    "completed_at": NOW, "reason": "Synthetic HTTP 429 route observation."}
        first = self.execute(request, blocked)
        self.assertEqual(first["state"], "Blocked")
        self.assertIsNone(first["evidence_id"])
        self.assertEqual(self.memory.snapshot()["trends_evidence"], {})
        check = next(iter(self.memory.snapshot()["source_checks"].values()))
        self.assertEqual(check["state"], "blocked")
        self.assertEqual(check["at"], NOW)
        self.assertEqual(check["concept_id"], "c000")
        self.assertIn("429", check["reason"])
        self.assertNotIn("positive", check)
        self.assertNotIn("value", check)
        second = self.execute(request, blocked)
        self.assertEqual(first, second)
        self.assertEqual(len(calls), 1)
        fact = build_report_pool(self.memory, as_of=NOW)["facts_by_concept"]["c000"]
        trend = next(row for row in fact["market_checks"] if row["source"] == "trends")
        self.assertEqual(trend["state"], "Insufficient")


if __name__ == "__main__":
    unittest.main()
