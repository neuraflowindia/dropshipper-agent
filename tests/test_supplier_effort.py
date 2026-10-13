"""Actual attempts versus prelaunch failures, with wholly offline receipts."""

from copy import deepcopy
from pathlib import Path
import socket
import sys
import tempfile
import threading
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from product_radar.models import SafetyError, digest
from product_radar.public_pages import fetch_public_page
from product_radar.research_memory import ResearchMemory
from product_radar.storage import Journal
from product_radar.supplier_capture_store import capture_saved_supplier_bundle
from product_radar.supplier_discovery import discover_saved_supplier_leads
from product_radar.supplier_effort import (CLIENT_NO_RESULTS, current_supplier_effort, failed_reader_receipt,
                                           frozen_supplier_effort_valid, record_supplier_effort, supplier_scope)

NOW = "2026-10-07T09:00:00+00:00"
LATER = "2026-10-08T09:00:00+00:00"
DESCRIPTION = {"label": "Sink caddy", "category": "Home and Kitchen", "function": "Store sink sponges",
               "mechanism": "Drainage holes", "features": ["Compact"], "brand": "generic_or_small"}
CONCEPT = "concept-caddy"
URL = "https://supplier.example.org/caddy"
REQUEST = {"concept_id": CONCEPT, "lead": {"id": "lead-caddy", "urls": [URL]}}


def receipt(item, *, stage="attempt_started", reason="PUBLIC_FETCH_DEADLINE", at=NOW):
    return {"schema_version": 2, "id": item["id"], "requested_url": item["url"], "final_url": None,
            "state": "unavailable", "reason": reason, "checked_at": at, "http_status": None,
            "execution_stage": stage, "attempt_started_at": None if stage == "not_dispatched" else at,
            "execution_ref": None if stage == "not_dispatched" else "public-fetch-" + "a" * 32}


class SupplierEffortTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        root = Path(self.temp.name)
        self.memory = ResearchMemory(root / "research", Journal(root / "finance"), evidence_type="synthetic")
        self.memory.put("concepts", CONCEPT, {"description": deepcopy(DESCRIPTION), "status": "provisional"})

    def record(self, rows, *, description=None, pass_id="pass-one", outcome="failed"):
        return record_supplier_effort(self.memory, concept_id=CONCEPT, description=description or DESCRIPTION,
               pass_id=pass_id, receipts=rows, receipt_reference="saved-public-capture", outcome=outcome)

    def current(self, *, description=None, as_of=LATER, accepted=()):
        return current_supplier_effort(self.memory.snapshot(), CONCEPT, description or DESCRIPTION,
                                        as_of=as_of, accepted_lead_refs=accepted)

    def test_genuine_dns_failure_is_actual_effort_but_worker_busy_is_not(self):
        def broken(*_args, **_kwargs):
            raise socket.gaierror("Do not save this private message")
        actual = fetch_public_page({"id": "page-one", "url": URL}, resolver=broken)
        self.assertEqual(actual["execution_stage"], "attempt_started")
        self.record([actual])
        self.assertTrue(self.current(as_of=None)["effort_satisfied"])
        with patch("product_radar.public_pages._SLOTS", threading.BoundedSemaphore(0)):
            busy = fetch_public_page({"id": "page-two", "url": URL})
        self.assertEqual(busy["execution_stage"], "not_dispatched")
        self.assertIsNone(busy["attempt_started_at"])
        self.assertIsNone(busy["execution_ref"])
        result = self.record([busy], pass_id="pass-busy")
        self.assertFalse(result["record"]["effort_satisfied"])
        self.assertNotIn("private message", repr(actual))

    def test_invalid_url_and_bad_local_reader_capability_do_not_start(self):
        for item, kwargs in (({"id": "page-one", "url": "file:///private"}, {}),
                             ({"id": "page-one", "url": URL}, {"resolver": "not callable"})):
            value = fetch_public_page(item, **kwargs)
            self.assertEqual(value["execution_stage"], "not_dispatched")
            self.assertIsNone(value["execution_ref"])

    def test_timeout_snapshot_retains_admitted_dns_execution(self):
        released = threading.Event()
        def stuck(*_args, **_kwargs):
            released.wait(1)
            raise socket.gaierror("Synthetic ended DNS")
        try:
            value = fetch_public_page({"id": "page-one", "url": URL}, resolver=stuck, timeout=0.03)
            self.assertEqual(value["reason"], "PUBLIC_FETCH_DEADLINE")
            self.assertEqual(value["execution_stage"], "attempt_started")
            self.record([value])
            self.assertTrue(self.current(as_of=None)["effort_satisfied"])
        finally:
            released.set()

    def test_url_date_and_failure_wording_alone_are_not_dispatch_proof(self):
        legacy = {"id": "page-one", "requested_url": URL, "checked_at": NOW,
                  "state": "unavailable", "reason": "PUBLIC_FETCH_DEADLINE"}
        result = self.record([legacy])
        self.assertFalse(result["record"]["effort_satisfied"])
        self.assertEqual(self.current()["internal_outcome"], "prelaunch_failed")

    def test_real_legacy_http_response_counts_without_rewriting_receipt(self):
        legacy = {"schema_version": 1, "id": "page-one", "requested_url": URL, "final_url": URL,
                  "checked_at": NOW, "state": "unavailable", "http_status": 429, "reason": "PUBLIC_HTTP_UNAVAILABLE"}
        original = deepcopy(legacy)
        self.record([legacy], outcome="blocked")
        current = self.current()
        self.assertTrue(current["effort_satisfied"])
        self.assertEqual(current["actual_attempt_refs"][0]["proof"], "legacy_http_response")
        self.assertEqual(current["internal_outcome"], "blocked")
        self.assertEqual(legacy, original)

    def test_bogus_v2_stage_or_prelaunch_code_cannot_claim_actual_effort(self):
        base = receipt({"id": "page-one", "url": URL})
        for index, overrides in enumerate(({"execution_ref": "model-says-attempted"},
                 {"reason": "PUBLIC_FETCH_WORKERS_BUSY"}, {"execution_stage": "response_received", "http_status": None},
                 {"attempt_started_at": LATER}, {"schema_version": True}, {"no_request": True})):
            with self.subTest(overrides=overrides):
                result = self.record([{**base, **overrides}], pass_id="invalid-" + str(index))
                self.assertFalse(result["record"]["effort_satisfied"])

    def test_current_scope_ignores_brand_judgment_but_not_function_change(self):
        self.record([receipt({"id": "page-one", "url": URL})])
        branded = {**DESCRIPTION, "brand": "unclear", "audience_reason": "A revised buyer positioning."}
        self.assertEqual(supplier_scope(CONCEPT, branded), supplier_scope(CONCEPT, DESCRIPTION))
        self.assertTrue(self.current(description=branded)["effort_satisfied"])
        self.assertFalse(self.current(description={**DESCRIPTION, "function": "Store bathroom toothbrushes"})["effort_satisfied"])
        other = current_supplier_effort(self.memory.snapshot(), "concept-other", DESCRIPTION, as_of=LATER)
        self.assertFalse(other["effort_satisfied"])

    def test_current_as_of_filter_excludes_future_attempts(self):
        self.record([receipt({"id": "page-one", "url": URL}, at=LATER)])
        self.assertFalse(self.current(as_of=NOW)["effort_satisfied"])
        self.assertTrue(self.current(as_of=LATER)["effort_satisfied"])

    def test_changed_current_product_cannot_receive_old_scope_projection(self):
        old = self.memory.snapshot()["concepts"][CONCEPT]
        self.memory.put("concepts", CONCEPT, {**old, "description": {**DESCRIPTION, "function": "Another function"}}, previous=old)
        with self.assertRaisesRegex(SafetyError, "SUPPLIER_EFFORT_SCOPE_CHANGED"):
            self.record([receipt({"id": "page-one", "url": URL})])
        self.assertEqual(self.memory.snapshot()["source_checks"], {})

    def test_replay_has_no_new_canonical_event(self):
        rows = [receipt({"id": "page-one", "url": URL})]
        first = self.record(rows)
        before = self.memory.snapshot()
        files = len(list((self.memory.root / "events").glob("*.json")))
        second = self.record(rows)
        self.assertTrue(second["reused"])
        self.assertEqual(first["check_id"], second["check_id"])
        self.assertEqual(self.memory.snapshot(), before)
        self.assertEqual(len(list((self.memory.root / "events").glob("*.json"))), files)

    def test_canonical_reader_does_not_scan_capture_folders(self):
        # A caller-provided source URL or an arbitrary local folder cannot
        # declare supplier effort without a verified canonical projection.
        self.assertFalse(self.current()["effort_satisfied"])
        self.assertEqual(self.current()["actual_attempt_refs"], [])
        self.assertEqual(self.current()["usable_lead_count"], 0)

    def test_known_page_actual_failure_without_f_is_sufficient(self):
        calls = []
        def reader(item):
            calls.append(item)
            return receipt(item)
        captured = capture_saved_supplier_bundle(self.memory, REQUEST, fetcher=reader)
        self.assertIsNone(captured["bundle"]["item"])
        self.assertEqual(self.memory.snapshot()["supplier_assessments"], {})
        self.assertTrue(captured["effort_satisfied"])
        current = self.current()
        self.assertTrue(current["effort_satisfied"])
        self.assertEqual(current["internal_outcome"], "failed")
        self.assertEqual(current["usable_lead_count"], 0)
        captured_again = capture_saved_supplier_bundle(self.memory, REQUEST, fetcher=reader)
        self.assertTrue(captured_again["reused"])
        self.assertEqual(len(calls), 1)
        self.assertEqual(CLIENT_NO_RESULTS, "Supplier search did not produce any usable results.")

    def test_known_page_busy_prelaunch_does_not_satisfy_effort(self):
        captured = capture_saved_supplier_bundle(self.memory, REQUEST, fetcher=lambda item: receipt(item,
                    stage="not_dispatched", reason="PUBLIC_FETCH_WORKERS_BUSY"))
        self.assertFalse(captured["effort_satisfied"])
        self.assertFalse(self.current()["effort_satisfied"])

    def test_wrong_page_id_receipt_cannot_be_reused_for_another_page(self):
        captured = capture_saved_supplier_bundle(self.memory, REQUEST, fetcher=lambda item:
                    receipt({"id": "another-products-page", "url": item["url"]}))
        self.assertFalse(captured["effort_satisfied"])

    def test_interrupted_projection_reuses_saved_capture_without_research(self):
        calls = []
        def reader(item):
            calls.append(item)
            return receipt(item)
        with patch("product_radar.supplier_capture_store.record_supplier_effort", side_effect=RuntimeError("Synthetic stop")), \
                self.assertRaises(RuntimeError):
            capture_saved_supplier_bundle(self.memory, REQUEST, fetcher=reader)
        saved = next((self.memory.root / "supplier_pages").glob("*/capture.json"))
        original = saved.read_bytes()
        recovered = capture_saved_supplier_bundle(self.memory, REQUEST, fetcher=reader)
        self.assertTrue(recovered["reused"])
        self.assertTrue(recovered["effort_satisfied"])
        self.assertEqual(len(calls), 1)
        self.assertEqual(saved.read_bytes(), original)

    def test_interrupted_search_projection_reuses_both_saved_searches(self):
        calls = []
        def reader(item):
            calls.append(item)
            return receipt(item)
        with patch("product_radar.supplier_discovery.record_supplier_effort", side_effect=RuntimeError("Synthetic stop")), \
                self.assertRaises(RuntimeError):
            discover_saved_supplier_leads(self.memory, CONCEPT, fetcher=reader)
        saved = next((self.memory.root / "supplier_searches").glob("*/search.json"))
        original = saved.read_bytes()
        recovered = discover_saved_supplier_leads(self.memory, CONCEPT, fetcher=reader)
        self.assertTrue(recovered["reused"])
        self.assertTrue(recovered["effort_satisfied"])
        self.assertEqual(len(calls), 2)
        self.assertEqual(saved.read_bytes(), original)

    def test_projection_hash_binds_all_saved_receipts_including_bad_siblings(self):
        request = {**REQUEST, "lead": {**REQUEST["lead"], "urls": [URL, URL + "-two"]}}
        def reader(item):
            if item["url"].endswith("-two"):
                return {"unexpected": "Original malformed reader result"}
            return receipt(item)
        captured = capture_saved_supplier_bundle(self.memory, request, fetcher=reader)
        projection = self.memory.snapshot()["source_checks"][captured["effort_check_id"]]
        self.assertEqual(projection["receipts_hash"], digest(captured["bundle"]["receipts"]))
        self.assertEqual(len(projection["actual_attempt_refs"]), 1)

    def test_unrelated_requested_url_cannot_inherit_admitted_page_execution(self):
        def reader(item):
            return receipt({"id": item["id"], "url": "https://different.example.org/another-product"})
        captured = capture_saved_supplier_bundle(self.memory, REQUEST, fetcher=reader)
        self.assertFalse(captured["effort_satisfied"])

    def test_real_failed_sibling_keeps_useful_page_and_interpretation_pending(self):
        request = {**REQUEST, "lead": {**REQUEST["lead"], "urls": [URL, URL + "-two"]}}
        def reader(item):
            value = receipt(item)
            if item["url"].endswith("-two"):
                value.update(execution_stage="response_received", http_status=200, state="usable", final_url=item["url"],
                             body_complete=True, title="Source page", text="Public synthetic source", discovered_links=[])
            return value
        captured = capture_saved_supplier_bundle(self.memory, request, fetcher=reader)
        self.assertEqual(captured["bundle"]["state"], "partial")
        self.assertEqual(len(captured["bundle"]["item"]["pages"]), 1)
        current = self.current()
        self.assertTrue(current["effort_satisfied"])
        self.assertEqual(current["internal_outcome"], "interpretation_pending")
        self.assertEqual(len(current["actual_attempt_refs"]), 2)

    def test_search_failed_after_actual_navigation_counts_but_unknown_exception_does_not(self):
        searched = discover_saved_supplier_leads(self.memory, CONCEPT, fetcher=lambda item: receipt(item))
        self.assertEqual(searched["result"]["state"], "blocked")
        self.assertTrue(searched["effort_satisfied"])
        self.assertEqual(len(self.current()["actual_attempt_refs"]), 2)
        with tempfile.TemporaryDirectory() as temp:
            memory = ResearchMemory(Path(temp) / "research", Journal(Path(temp) / "finance"), evidence_type="synthetic")
            memory.put("concepts", CONCEPT, {"description": DESCRIPTION, "status": "provisional"})
            def unknown(item):
                raise RuntimeError("Unknown dispatch must not become an attempted search")
            result = discover_saved_supplier_leads(memory, CONCEPT, fetcher=unknown)
            self.assertFalse(result["effort_satisfied"])

    def test_complete_empty_search_is_different_from_blocked_search(self):
        def no_results(item):
            value = receipt(item)
            value.update(state="usable", execution_stage="response_received", final_url=item["url"], http_status=200,
                         body_complete=True, html="Your search did not match any documents.",
                         text="Your search did not match any documents.")
            return value
        result = discover_saved_supplier_leads(self.memory, CONCEPT, fetcher=no_results)
        self.assertTrue(result["effort_satisfied"])
        self.assertEqual(self.current()["internal_outcome"], "completed_empty")
        self.assertEqual(self.current()["usable_lead_count"], 0)

    def test_wrapper_preserves_valid_owned_stage_but_not_secret_attribute(self):
        item = {"id": "page-one", "url": URL}
        error = RuntimeError("Secret raw message")
        error.execution_receipt = receipt(item)
        kept = failed_reader_receipt(item, "SUPPLIER_PUBLIC_FETCH_FAILED", error)
        self.assertEqual(kept["execution_stage"], "attempt_started")
        self.assertNotIn("Secret raw message", repr(kept))
        error.execution_receipt["final_url"] = "https://user:secret@supplier.example.org/"
        rejected = failed_reader_receipt(item, "SUPPLIER_PUBLIC_FETCH_FAILED", error)
        self.assertFalse(rejected["dispatch_known"])
        self.assertNotIn("secret", repr(rejected))

    def test_canonical_tampering_cannot_invent_effort(self):
        saved = self.record([receipt({"id": "page-one", "url": URL})])
        state = self.memory.snapshot()
        state["source_checks"][saved["check_id"]]["actual_attempt_refs"] = []
        with self.assertRaisesRegex(SafetyError, "SUPPLIER_EFFORT_RECORD_INVALID"):
            current_supplier_effort(state, CONCEPT, DESCRIPTION, as_of=LATER)

    def test_duplicate_receipt_identity_and_leads_without_actual_attempt_are_rejected(self):
        value = receipt({"id": "page-one", "url": URL})
        with self.assertRaisesRegex(SafetyError, "SUPPLIER_EFFORT_RECEIPT_CONFLICT"):
            self.record([value, value])
        with self.assertRaisesRegex(SafetyError, "SUPPLIER_EFFORT_LEADS_WITHOUT_ATTEMPT"):
            record_supplier_effort(self.memory, concept_id=CONCEPT, description=DESCRIPTION, pass_id="pass-two", receipts=[],
                   receipt_reference="saved-empty", outcome="usable_leads", accepted_lead_refs=["lead-one"])

    def test_current_lead_count_is_separate_from_failed_effort(self):
        self.record([receipt({"id": "page-one", "url": URL})])
        current = self.current(accepted=["lead-one", "lead-two"])
        self.assertEqual(current["usable_lead_count"], 2)
        self.assertEqual(current["internal_outcome"], "usable_leads")
        self.assertTrue(current["effort_satisfied"])


class FrozenSupplierEffortTests(unittest.TestCase):
    def canonical(self, *, outcome="failed"):
        # Construct via the same projection producer rather than inventing a
        # renderer-only proof shape. Everything stays in temporary storage.
        with tempfile.TemporaryDirectory() as temp:
            memory = ResearchMemory(Path(temp) / "research", Journal(Path(temp) / "finance"), evidence_type="synthetic")
            memory.put("concepts", CONCEPT, {"description": DESCRIPTION, "status": "provisional"})
            captured = record_supplier_effort(memory, concept_id=CONCEPT, description=DESCRIPTION, pass_id="pass-one",
                        receipts=[receipt({"id": "page-one", "url": URL})], receipt_reference="saved-public-capture", outcome=outcome)
            effort = current_supplier_effort(memory.snapshot(), CONCEPT, DESCRIPTION, as_of=LATER)
        return {"fit_policy_id": "fit-10-v1", "concept_id": CONCEPT, "supplier_effort_satisfied": True,
                "supplier_effort": {**effort, "legacy_interpretation_ids": [], "legacy_search_check_ids": []},
                "supplier_search_status": "blocked", "supplier_assessment_ids": [], "supplier_search_check_ids": effort["check_ids"],
                "supplier_leads_found": 0, "suppliers": [],
                "source_revisions": {"source_checks": {captured["check_id"]: digest(captured["record"])}}}

    def legacy(self, *, lead_count=0, search=False):
        assessment_ids = [] if search else ["assessment-" + str(index) for index in range(max(1, lead_count))]
        checks = ["legacy-search-check"] if search else []
        effort = {"check_ids": [], "actual_attempt_refs": [], "last_attempt_at": None, "effort_satisfied": False,
                  "usable_lead_count": lead_count, "internal_outcome": "usable_leads" if lead_count else "not_attempted",
                  "legacy_interpretation_ids": assessment_ids, "legacy_search_check_ids": checks}
        return {"fit_policy_id": "fit-10-v1", "concept_id": CONCEPT, "supplier_effort_satisfied": True,
                "supplier_effort": effort, "supplier_search_status": "complete", "supplier_assessment_ids": assessment_ids,
                "supplier_search_check_ids": checks, "supplier_leads_found": lead_count,
                "suppliers": [{"lead_id": "lead-" + str(index)} for index in range(lead_count)],
                "source_revisions": {"supplier_assessments": {item: "a" * 64 for item in assessment_ids},
                                     "source_checks": {item: "b" * 64 for item in checks}}}

    def test_failed_blocked_pending_and_completed_empty_are_all_valid_effort(self):
        for outcome in ("failed", "blocked", "interpretation_pending", "completed_empty", "partial"):
            with self.subTest(outcome=outcome):
                self.assertTrue(frozen_supplier_effort_valid(self.canonical(outcome=outcome)))

    def test_historical_interpretation_or_empty_search_does_not_need_fake_http(self):
        for product in (self.legacy(), self.legacy(search=True)):
            with self.subTest(product=product):
                self.assertFalse(product["supplier_effort"]["effort_satisfied"])
                self.assertEqual(product["supplier_effort"]["actual_attempt_refs"], [])
                self.assertTrue(frozen_supplier_effort_valid(product))

    def test_all_five_usable_leads_survive_new_facts(self):
        product = self.legacy(lead_count=5)
        self.assertTrue(frozen_supplier_effort_valid(product))
        product["suppliers"] = product["suppliers"][:3]
        self.assertFalse(frozen_supplier_effort_valid(product))

    def test_boolean_only_or_missing_projection_cannot_claim_research(self):
        product = self.canonical()
        product.pop("supplier_effort")
        self.assertFalse(frozen_supplier_effort_valid(product))
        product = self.legacy()
        product["supplier_effort"]["legacy_interpretation_ids"] = []
        product["supplier_assessment_ids"] = []
        self.assertFalse(frozen_supplier_effort_valid(product))

    def test_never_started_status_is_not_satisfied_by_other_fields(self):
        product = self.canonical()
        product["supplier_search_status"] = "not_started"
        self.assertFalse(frozen_supplier_effort_valid(product))

    def test_canonical_flag_must_agree_with_actual_attempt_refs(self):
        product = self.canonical()
        product["supplier_effort"]["effort_satisfied"] = False
        self.assertFalse(frozen_supplier_effort_valid(product))
        product = self.canonical()
        product["supplier_effort"]["actual_attempt_refs"] = []
        self.assertFalse(frozen_supplier_effort_valid(product))

    def test_legacy_bindings_must_match_frozen_assessment_and_check_ids(self):
        for product, field in ((self.legacy(), "legacy_interpretation_ids"),
                               (self.legacy(search=True), "legacy_search_check_ids")):
            product["supplier_effort"][field] = ["different-reference"]
            self.assertFalse(frozen_supplier_effort_valid(product))

    def test_source_revisions_are_required_and_hashes_must_be_valid(self):
        product = self.legacy()
        product.pop("source_revisions")
        self.assertFalse(frozen_supplier_effort_valid(product))
        product = self.canonical()
        item = product["supplier_search_check_ids"][0]
        product["source_revisions"]["source_checks"][item] = "not-a-sha256"
        self.assertFalse(frozen_supplier_effort_valid(product))
        product = self.canonical()
        product["source_revisions"]["source_checks"].clear()
        self.assertFalse(frozen_supplier_effort_valid(product))

    def test_foreign_or_prelaunch_stage_cannot_be_an_actual_attempt(self):
        for overrides in ({"check_id": "foreign-check"}, {"execution_stage": "not_dispatched"},
                          {"proof": "none"}, {"execution_ref": "invented-by-model"}, {"hash": "bad"}):
            product = self.canonical()
            product["supplier_effort"]["actual_attempt_refs"][0].update(overrides)
            self.assertFalse(frozen_supplier_effort_valid(product))

    def test_dates_and_last_attempt_are_checked_without_using_wall_clock(self):
        for overrides in ({"attempt_started_at": "not-a-time"}, {"attempt_started_at": LATER}, {"checked_at": None}):
            product = self.canonical()
            product["supplier_effort"]["actual_attempt_refs"][0].update(overrides)
            self.assertFalse(frozen_supplier_effort_valid(product))
        product = self.canonical()
        product["supplier_effort"]["last_attempt_at"] = LATER
        self.assertFalse(frozen_supplier_effort_valid(product))

    def test_duplicate_physical_receipt_or_supplier_is_rejected(self):
        product = self.canonical()
        product["supplier_effort"]["actual_attempt_refs"].append(deepcopy(product["supplier_effort"]["actual_attempt_refs"][0]))
        self.assertFalse(frozen_supplier_effort_valid(product))
        product = self.legacy(lead_count=2)
        product["suppliers"][1]["lead_id"] = product["suppliers"][0]["lead_id"]
        self.assertFalse(frozen_supplier_effort_valid(product))

    def test_useful_count_and_outcome_cannot_include_rejected_suppliers(self):
        product = self.legacy()
        product["supplier_effort"]["usable_lead_count"] = 1
        self.assertFalse(frozen_supplier_effort_valid(product))
        product = self.legacy()
        product["supplier_effort"]["internal_outcome"] = "usable_leads"
        self.assertFalse(frozen_supplier_effort_valid(product))
        product = self.legacy(lead_count=1)
        product["supplier_effort"]["internal_outcome"] = "not_attempted"
        self.assertFalse(frozen_supplier_effort_valid(product))

    def test_usable_lead_needs_its_bound_interpretation_not_only_an_attempt(self):
        product = self.canonical()
        product["suppliers"] = [{"lead_id": "invented-lead"}]
        product["supplier_leads_found"] = 1
        product["supplier_effort"].update(usable_lead_count=1, internal_outcome="usable_leads")
        self.assertFalse(frozen_supplier_effort_valid(product))

    def test_existing_thirteen_criterion_schema_is_unchanged(self):
        self.assertTrue(frozen_supplier_effort_valid({"title": "Historical product"}))
        self.assertTrue(frozen_supplier_effort_valid({"fit_policy_id": "fit-13-v1"}))
        self.assertFalse(frozen_supplier_effort_valid({"fit_policy_id": "unknown-policy"}))


if __name__ == "__main__":
    unittest.main()
