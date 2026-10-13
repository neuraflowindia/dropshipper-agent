"""Synthetic receipts prove saved-capture reuse, not actual supplier terms."""

from copy import deepcopy
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from product_radar.models import SafetyError, digest
from product_radar.research_memory import ResearchMemory, key
from product_radar.storage import Journal, atomic_json, load_json
from product_radar.supplier_capture_store import (capture_saved_supplier_bundle, load_saved_supplier_input,
                                                load_saved_supplier_capture)

NOW = "2026-10-06T08:00:00+00:00"
LATER = "2026-11-06T08:00:00+00:00"
BASE = "https://supplier.example.org"
REQUEST = {"concept_id": "concept-caddy", "lead": {"id": "lead-caddy", "urls": [BASE + "/product/caddy"]}}
CONCEPT = {"description": {"label": "Sink caddy", "function": "Store sink sponges", "brand": "generic_or_small",
                           "category": "Home and Kitchen"}, "status": "provisional"}


class Reader:
    def __init__(self, responses=None):
        self.responses, self.calls = responses or {}, []

    def __call__(self, item):
        self.calls.append(deepcopy(item))
        value = {"id": item["id"], "requested_url": item["url"], "final_url": item["url"],
                 "checked_at": NOW, "state": "usable", "http_status": 200, "body_complete": True,
                 "title": "Fictional supplier", "text": "Synthetic source text; no fulfilment promise.",
                 "discovered_links": [], "html": "<p>Synthetic source</p>"}
        value.update(self.responses.get(item["url"], {}))
        return value


class SavedSupplierCaptureTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        root = Path(self.temp.name)
        self.memory = ResearchMemory(root / "research", Journal(root / "finance"), evidence_type="synthetic")
        self.memory.put("concepts", REQUEST["concept_id"], deepcopy(CONCEPT))
        self.reader = Reader()

    def capture(self, request=None, reader=None):
        return capture_saved_supplier_bundle(self.memory, request or deepcopy(REQUEST), fetcher=reader or self.reader)

    def refresh(self, previous, *, reason="Revisit the known supplier's dated terms.", requested_at=LATER):
        return {**deepcopy(REQUEST), "refresh": {"previous_capture_id": previous["capture_id"],
                                                 "reason": reason, "requested_at": requested_at}}

    def test_reuse_keeps_original_bundle_dates_and_only_projects_supplier_effort(self):
        before = self.memory.snapshot()
        first = self.capture()
        self.assertFalse(first["reused"])
        after_first = self.memory.snapshot()
        self.assertEqual(len(after_first["source_checks"]), 1)
        self.assertTrue(first["effort_satisfied"])
        self.assertEqual({table: rows for table, rows in after_first.items() if table != "source_checks"},
                         {table: rows for table, rows in before.items() if table != "source_checks"})
        second = self.capture()
        self.assertTrue(second["reused"])
        self.assertEqual(len(self.reader.calls), 1)
        self.assertEqual(first["bundle"], second["bundle"])
        self.assertEqual(first["bundle"]["receipts"][0]["checked_at"], NOW)
        self.assertEqual(self.memory.snapshot(), after_first)
        saved = load_json(first["path"], 8 * 1024 * 1024)
        self.assertEqual(saved["product"], {"concept_id": REQUEST["concept_id"], "description": CONCEPT["description"]})
        self.assertEqual(saved["bundle"], first["bundle"])
        self.assertFalse(first["bundle"]["client_ready"])

    def test_failed_capture_reuses_original_failure_without_automatic_retry(self):
        reader = Reader({BASE + "/product/caddy": {"state": "unavailable", "reason": "PUBLIC_FETCH_DEADLINE"}})
        first = self.capture(reader=reader)
        second = self.capture(reader=reader)
        self.assertEqual(len(reader.calls), 1)
        self.assertTrue(second["reused"])
        self.assertEqual(first["bundle"], second["bundle"])
        self.assertEqual(second["bundle"]["state"], "no_usable_pages")
        self.assertIsNone(second["bundle"]["item"])

    def test_read_only_failed_bundle_loader_verifies_original_scope_and_hashes(self):
        reader = Reader({BASE + "/product/caddy": {"state": "unavailable", "reason": "PUBLIC_FETCH_DEADLINE"}})
        captured = self.capture(reader=reader)
        before = self.memory.snapshot()
        loaded = load_saved_supplier_capture(self.memory, captured["path"])
        self.assertEqual(loaded["bundle"], captured["bundle"])
        self.assertEqual(loaded["product"]["concept_id"], REQUEST["concept_id"])
        self.assertEqual(len(reader.calls), 1)
        self.assertEqual(self.memory.snapshot(), before)
        with self.assertRaisesRegex(SafetyError, "SUPPLIER_CAPTURE_NO_USABLE_PAGES"):
            load_saved_supplier_input(self.memory, captured["path"])
        raw = load_json(captured["path"], 8 * 1024 * 1024)
        raw["bundle"]["receipts"][0]["reason"] = "Changed original failure"
        atomic_json(captured["path"], raw)
        with self.assertRaisesRegex(SafetyError, "SAVED_SUPPLIER_CAPTURE_INVALID"):
            load_saved_supplier_capture(self.memory, captured["path"])

    def test_changed_trusted_description_uses_new_capture_key(self):
        first = self.capture()
        old = self.memory.snapshot()["concepts"][REQUEST["concept_id"]]
        changed = {**old, "description": {**old["description"], "function": "Store bathroom accessories"}}
        self.memory.put("concepts", REQUEST["concept_id"], changed, previous=old)
        second = self.capture()
        self.assertNotEqual(first["capture_id"], second["capture_id"])
        self.assertFalse(second["reused"])
        self.assertEqual(len(self.reader.calls), 2)
        self.assertEqual(second["bundle"]["item"]["product"]["function"], "Store bathroom accessories")
        self.assertTrue(Path(first["path"]).exists())

    def test_malformed_reader_receipt_is_retained_as_failure_without_repeating_fetch(self):
        calls = []
        def reader(item):
            calls.append(item)
            return {"unexpected": "Original unusable reader output"}
        first = self.capture(reader=reader)
        second = self.capture(reader=reader)
        self.assertEqual(len(calls), 1)
        self.assertEqual(first["bundle"], second["bundle"])
        self.assertEqual(second["bundle"]["state"], "no_usable_pages")
        self.assertEqual(second["bundle"]["receipts"], [{"unexpected": "Original unusable reader output"}])
        self.assertIsNone(second["bundle"]["item"])

    def test_established_retired_and_conflicted_concepts_do_not_fetch(self):
        old = self.memory.snapshot()["concepts"][REQUEST["concept_id"]]
        for changes in ({"description": {**old["description"], "brand": "established"}},
                        {"status": "retired"}, {"identity_conflicted": True}):
            current = self.memory.snapshot()["concepts"][REQUEST["concept_id"]]
            self.memory.put("concepts", REQUEST["concept_id"], {**old, **changes}, previous=current)
            with self.assertRaisesRegex(SafetyError, "SUPPLIER_CAPTURE_CONCEPT_EXCLUDED"):
                self.capture()
        self.assertEqual(self.reader.calls, [])

    def test_corrupt_saved_body_or_identity_is_not_refetched(self):
        first = self.capture()
        original = load_json(first["path"], 8 * 1024 * 1024)
        for field, value in (("schema_version", 2), ("input_hash", "wrong"), ("capture_id", "different")):
            atomic_json(first["path"], {**original, field: value})
            with self.assertRaisesRegex(SafetyError, "SAVED_SUPPLIER_CAPTURE_INVALID"):
                self.capture()
        corrupt = deepcopy(original)
        corrupt["bundle"]["receipts"][0]["text"] = "Changed saved body"
        atomic_json(first["path"], corrupt)
        with self.assertRaisesRegex(SafetyError, "SAVED_SUPPLIER_CAPTURE_INVALID"):
            self.capture()
        self.assertEqual(len(self.reader.calls), 1)

    def test_interruption_after_atomic_save_reuses_completed_capture(self):
        def save_then_interrupt(path, value):
            atomic_json(path, value)
            raise RuntimeError("Synthetic stop after completed save")
        with patch("product_radar.supplier_capture_store.atomic_json", side_effect=save_then_interrupt), \
                self.assertRaises(RuntimeError):
            self.capture()
        second = self.capture()
        self.assertTrue(second["reused"])
        self.assertEqual(len(self.reader.calls), 1)
        self.assertEqual(second["bundle"]["receipts"][0]["checked_at"], NOW)

    def test_partial_linked_terms_capture_is_saved_and_reused(self):
        reader = Reader({BASE + "/product/caddy": {"discovered_links": [BASE + "/pricing"]},
                         BASE + "/pricing": {"state": "unavailable", "reason": "PUBLIC_HTTP_UNAVAILABLE"}})
        first = self.capture(reader=reader)
        second = self.capture(reader=reader)
        self.assertEqual(first["bundle"]["state"], "partial")
        self.assertEqual(len(first["bundle"]["item"]["pages"]), 1)
        self.assertEqual(first["bundle"], second["bundle"])
        self.assertEqual(len(reader.calls), 2)

    def test_financial_lock_is_not_held_during_fetch_and_duplicate_capture_is_blocked(self):
        calls = []
        def reader(item):
            with self.memory.journal.locked():
                calls.append("financial lock available")
            with self.assertRaisesRegex(SafetyError, "RUNNER_ALREADY_ACTIVE"):
                self.capture(reader=Reader())
            return self.reader(item)
        self.capture(reader=reader)
        self.assertEqual(calls, ["financial lock available"])
        self.assertEqual(len(self.reader.calls), 1)

    def test_changed_description_during_fetch_retains_capture_but_refuses_current_input(self):
        def reader(item):
            old = self.memory.snapshot()["concepts"][REQUEST["concept_id"]]
            self.memory.put("concepts", REQUEST["concept_id"],
                            {**old, "description": {**old["description"], "label": "Different caddy"}}, previous=old)
            return self.reader(item)
        with self.assertRaisesRegex(SafetyError, "SUPPLIER_CAPTURE_DESCRIPTION_CHANGED"):
            self.capture(reader=reader)
        self.assertEqual(len(list((self.memory.root / "supplier_pages").glob("*/capture.json"))), 1)

    def test_request_shape_unknown_concept_and_bounds_fail_before_fetch(self):
        for request in ({**REQUEST, "extra": True}, {**REQUEST, "concept_id": "unknown"},
                        {**REQUEST, "lead": {**REQUEST["lead"], "urls": []}},
                        {**REQUEST, "lead": {**REQUEST["lead"], "urls": [BASE] * 4}},
                        {**REQUEST, "lead": {**REQUEST["lead"], "urls": [BASE + "/?token=synthetic-secret"]}},
                        {**REQUEST, "lead": {**REQUEST["lead"], "urls": ["https://user:synthetic-secret@example.org"]}},
                        {**REQUEST, "lead": {**REQUEST["lead"], "extra": "unused"}}):
            with self.subTest(request=request), self.assertRaises(SafetyError):
                self.capture(request=request)
        self.assertEqual(self.reader.calls, [])

    def test_loader_returns_current_model_item_and_original_separate_page_dates(self):
        reader = Reader({BASE + "/product/caddy": {"discovered_links": [BASE + "/pricing"]},
                         BASE + "/pricing": {"checked_at": "2026-10-05T08:00:00+00:00"}})
        captured = self.capture(reader=reader)
        result = load_saved_supplier_input(self.memory, captured["path"])
        self.assertEqual(result["item"], captured["bundle"]["item"])
        self.assertEqual(result["capture_id"], captured["capture_id"])
        self.assertEqual(result["page_dates"], {row["id"]: row["checked_at"] for row in captured["bundle"]["receipts"]})
        self.assertNotIn("checked_at", result["item"])
        self.assertTrue(all("checked_at" not in page for page in result["item"]["pages"]))
        self.assertEqual(load_saved_supplier_input(self.memory, captured["path"]), result)
        self.assertEqual(len(reader.calls), 2)

    def test_loader_rejects_wrong_path_and_another_namespace(self):
        captured = self.capture()
        saved = load_json(captured["path"], 8 * 1024 * 1024)
        wrong = self.memory.root / "supplier_pages" / "different-capture" / "capture.json"
        atomic_json(wrong, saved)
        other = ResearchMemory(Path(self.temp.name) / "another-research", Journal(Path(self.temp.name) / "another-finance"), evidence_type="synthetic")
        for memory, path in ((self.memory, wrong), (other, captured["path"]),
                             (self.memory, Path(captured["path"]).with_name("other.json")), (self.memory, None)):
            with self.subTest(path=path), self.assertRaisesRegex(SafetyError, "SUPPLIER_CAPTURE_PATH_INVALID"):
                load_saved_supplier_input(memory, path)
        self.assertEqual(len(self.reader.calls), 1)

    def test_loader_refuses_corrupt_saved_body_even_when_capture_exists(self):
        captured = self.capture()
        saved = load_json(captured["path"], 8 * 1024 * 1024)
        saved["bundle"]["item"]["pages"][0]["text"] = "Changed page text"
        atomic_json(captured["path"], saved)
        with self.assertRaisesRegex(SafetyError, "SAVED_SUPPLIER_CAPTURE_INVALID"):
            load_saved_supplier_input(self.memory, captured["path"])
        # Even a recomputed bundle hash cannot hide mismatched page/receipt text.
        saved["bundle_hash"] = digest(saved["bundle"])
        atomic_json(captured["path"], saved)
        with self.assertRaisesRegex(SafetyError, "SAVED_SUPPLIER_CAPTURE_INVALID"):
            load_saved_supplier_input(self.memory, captured["path"])
        self.assertEqual(len(self.reader.calls), 1)

    def test_loader_refuses_stale_and_excluded_product_without_refetching(self):
        captured = self.capture()
        old = self.memory.snapshot()["concepts"][REQUEST["concept_id"]]
        changed = {**old, "description": {**old["description"], "function": "Different function"}}
        self.memory.put("concepts", REQUEST["concept_id"], changed, previous=old)
        with self.assertRaisesRegex(SafetyError, "SUPPLIER_CAPTURE_DESCRIPTION_CHANGED"):
            load_saved_supplier_input(self.memory, captured["path"])
        excluded = {**old, "description": {**old["description"], "brand": "established"}}
        self.memory.put("concepts", REQUEST["concept_id"], excluded, previous=changed)
        with self.assertRaisesRegex(SafetyError, "SUPPLIER_CAPTURE_CONCEPT_EXCLUDED"):
            load_saved_supplier_input(self.memory, captured["path"])
        self.assertEqual(len(self.reader.calls), 1)

    def test_loader_refuses_no_usable_pages_but_accepts_useful_partial_capture(self):
        failed = self.capture(reader=Reader({BASE + "/product/caddy": {"state": "unavailable", "reason": "PUBLIC_FETCH_DEADLINE"}}))
        with self.assertRaisesRegex(SafetyError, "SUPPLIER_CAPTURE_NO_USABLE_PAGES"):
            load_saved_supplier_input(self.memory, failed["path"])
        request = {**REQUEST, "lead": {**REQUEST["lead"], "id": "lead-partial"}}
        reader = Reader({BASE + "/product/caddy": {"discovered_links": [BASE + "/pricing"]},
                         BASE + "/pricing": {"state": "unavailable", "reason": "PUBLIC_FETCH_DEADLINE"}})
        partial = self.capture(request=request, reader=reader)
        result = load_saved_supplier_input(self.memory, partial["path"])
        self.assertEqual(result["item"], partial["bundle"]["item"])
        self.assertEqual(result["page_dates"], {result["item"]["pages"][0]["id"]: NOW})

    def test_loader_does_not_need_a_date_from_a_malformed_failed_sibling(self):
        request = {**REQUEST, "lead": {**REQUEST["lead"], "urls": [BASE + "/product/caddy", BASE + "/broken"]}}
        reader = Reader({BASE + "/broken": {"id": [], "state": "unavailable", "checked_at": None,
                                              "reason": "PUBLIC_HTTP_UNAVAILABLE"}})
        captured = self.capture(request=request, reader=reader)
        self.assertEqual(captured["bundle"]["state"], "partial")
        result = load_saved_supplier_input(self.memory, captured["path"])
        self.assertEqual(result["page_dates"], {result["item"]["pages"][0]["id"]: NOW})

    def test_explicit_refresh_has_distinct_v2_key_actual_dates_and_stable_model_item(self):
        first = self.capture()
        original_bytes = Path(first["path"]).read_bytes()
        reader = Reader({BASE + "/product/caddy": {"checked_at": "2026-11-07T08:00:00+00:00"}})
        request = self.refresh(first)
        refreshed = self.capture(request=request, reader=reader)
        repeated = self.capture(request=request, reader=reader)
        self.assertNotEqual(first["capture_id"], refreshed["capture_id"])
        self.assertFalse(refreshed["reused"])
        self.assertTrue(repeated["reused"])
        self.assertEqual(repeated["bundle"], refreshed["bundle"])
        self.assertEqual(len(reader.calls), 1)
        self.assertEqual(Path(first["path"]).read_bytes(), original_bytes)
        saved = load_json(refreshed["path"], 8 * 1024 * 1024)
        self.assertEqual(saved["schema_version"], 2)
        self.assertEqual(saved["bundle"]["schema_version"], 1)
        self.assertEqual(saved["request"], request)
        original_input = load_saved_supplier_input(self.memory, first["path"])
        new_input = load_saved_supplier_input(self.memory, refreshed["path"])
        self.assertEqual(original_input["item"], new_input["item"])
        self.assertEqual(set(new_input["page_dates"].values()), {"2026-11-07T08:00:00+00:00"})
        self.assertNotEqual(new_input["page_dates"], original_input["page_dates"])
        self.assertNotEqual(next(iter(new_input["page_dates"].values())), request["refresh"]["requested_at"])
        self.assertEqual(self.capture()["capture_id"], key("supplier-capture", [saved["product"], REQUEST["lead"]]))
        self.assertEqual(self.capture()["bundle"], first["bundle"])
        self.assertEqual(len(self.reader.calls), 1)

    def test_changed_explicit_intent_creates_a_separate_capture(self):
        first = self.capture()
        request = self.refresh(first)
        refreshed = self.capture(request=request)
        for update in ({"reason": "Recheck a newly announced delivery option."},
                       {"requested_at": "2026-11-08T08:00:00+00:00"}):
            changed = {**request, "refresh": {**request["refresh"], **update}}
            another = self.capture(request=changed)
            self.assertNotEqual(another["capture_id"], refreshed["capture_id"])
            self.assertFalse(another["reused"])
            self.assertTrue(self.capture(request=changed)["reused"])
        self.assertEqual(len(self.reader.calls), 4)

    def test_malformed_refresh_intents_fail_before_fetch(self):
        first = self.capture()
        valid = self.refresh(first)
        bad = [None, {}, {**valid["refresh"], "extra": True},
               {**valid["refresh"], "previous_capture_id": "../capture"},
               {**valid["refresh"], "previous_capture_id": "supplier-capture-" + "A" * 40},
               {**valid["refresh"], "reason": " "}, {**valid["refresh"], "reason": "a" * 501},
               {**valid["refresh"], "reason": True}, {**valid["refresh"], "requested_at": None},
               {**valid["refresh"], "requested_at": "2026-11-06"},
               {**valid["refresh"], "requested_at": "not a time"}]
        for refresh in bad:
            with self.subTest(refresh=refresh), self.assertRaisesRegex(SafetyError, "SUPPLIER_CAPTURE_REFRESH_INVALID"):
                self.capture(request={**REQUEST, "refresh": refresh})
        self.assertEqual(len(self.reader.calls), 1)

    def test_missing_or_other_namespace_prior_fails_before_fetch(self):
        first = self.capture()
        request = self.refresh(first)
        missing = {**request, "refresh": {**request["refresh"], "previous_capture_id": "supplier-capture-" + "0" * 40}}
        with self.assertRaisesRegex(SafetyError, "SUPPLIER_CAPTURE_REFRESH_PREVIOUS_INVALID"):
            self.capture(request=missing)
        other = ResearchMemory(Path(self.temp.name) / "other-research", Journal(Path(self.temp.name) / "other-finance"), evidence_type="synthetic")
        other.put("concepts", REQUEST["concept_id"], deepcopy(CONCEPT))
        with self.assertRaisesRegex(SafetyError, "SUPPLIER_CAPTURE_REFRESH_PREVIOUS_INVALID"):
            capture_saved_supplier_bundle(other, request, fetcher=self.reader)
        self.assertEqual(len(self.reader.calls), 1)

    def test_prior_must_match_current_concept_description_and_exact_lead(self):
        first = self.capture()
        request = self.refresh(first)
        self.memory.put("concepts", "concept-other", deepcopy(CONCEPT))
        wrong_requests = [{**request, "concept_id": "concept-other"},
                          {**request, "lead": {**REQUEST["lead"], "id": "lead-other"}},
                          {**request, "lead": {**REQUEST["lead"], "urls": [BASE + "/product/other"]}}]
        for wrong in wrong_requests:
            with self.subTest(request=wrong), self.assertRaisesRegex(SafetyError, "SUPPLIER_CAPTURE_REFRESH_PREVIOUS_INVALID"):
                self.capture(request=wrong)
        old = self.memory.snapshot()["concepts"][REQUEST["concept_id"]]
        self.memory.put("concepts", REQUEST["concept_id"],
                        {**old, "description": {**old["description"], "function": "A different use"}}, previous=old)
        with self.assertRaisesRegex(SafetyError, "SUPPLIER_CAPTURE_REFRESH_PREVIOUS_INVALID"):
            self.capture(request=request)
        self.assertEqual(len(self.reader.calls), 1)

    def test_corrupt_prior_envelope_key_and_body_fail_without_fetch(self):
        first = self.capture()
        original = load_json(first["path"], 8 * 1024 * 1024)
        request = self.refresh(first)
        bad_records = [{**original, "schema_version": 2}, {**original, "capture_id": "wrong"},
                       {**original, "description_hash": "wrong"}]
        modified = deepcopy(original)
        modified["bundle"]["receipts"][0]["text"] = "Tampered original body"
        modified["bundle_hash"] = digest(modified["bundle"])
        bad_records.append(modified)
        renamed = deepcopy(original)
        renamed["request"]["lead"]["id"] = "lead-different"
        bad_records.append(renamed)
        for saved in bad_records:
            atomic_json(first["path"], saved)
            with self.subTest(saved=saved), self.assertRaisesRegex(SafetyError, "SUPPLIER_CAPTURE_REFRESH_PREVIOUS_INVALID"):
                self.capture(request=request)
        self.assertEqual(len(self.reader.calls), 1)

    def test_failed_prior_is_preserved_and_explicit_refresh_can_read_success(self):
        failed_reader = Reader({BASE + "/product/caddy": {"state": "unavailable", "reason": "PUBLIC_FETCH_DEADLINE"}})
        first = self.capture(reader=failed_reader)
        previous_bytes = Path(first["path"]).read_bytes()
        refreshed = self.capture(request=self.refresh(first))
        self.assertEqual(refreshed["bundle"]["state"], "usable")
        self.assertEqual(Path(first["path"]).read_bytes(), previous_bytes)
        self.assertEqual(self.capture(reader=failed_reader)["bundle"]["state"], "no_usable_pages")
        self.assertEqual(len(failed_reader.calls), 1)
        self.assertEqual(len(self.reader.calls), 1)

    def test_refresh_can_fail_and_replay_keeps_new_original_failure(self):
        first = self.capture()
        original_bytes = Path(first["path"]).read_bytes()
        reader = Reader({BASE + "/product/caddy": {"state": "unavailable", "reason": "PUBLIC_HTTP_UNAVAILABLE", "checked_at": LATER}})
        request = self.refresh(first)
        refreshed = self.capture(request=request, reader=reader)
        replayed = self.capture(request=request, reader=reader)
        self.assertEqual(refreshed["bundle"], replayed["bundle"])
        self.assertEqual(replayed["bundle"]["state"], "no_usable_pages")
        self.assertEqual(replayed["bundle"]["receipts"][0]["checked_at"], LATER)
        self.assertEqual(len(reader.calls), 1)
        self.assertEqual(Path(first["path"]).read_bytes(), original_bytes)

    def test_v2_prior_is_validated_once_without_traversing_its_ancestors(self):
        first = self.capture()
        second = self.capture(request=self.refresh(first))
        Path(first["path"]).unlink()
        request = self.refresh(second, requested_at="2026-12-06T08:00:00+00:00")
        third = self.capture(request=request)
        self.assertFalse(third["reused"])
        self.assertTrue(self.capture(request=request)["reused"])
        self.assertEqual(len(self.reader.calls), 3)
        self.assertEqual(load_saved_supplier_input(self.memory, third["path"])["item"], second["bundle"]["item"])

    def test_refresh_replay_refuses_corrupt_new_envelope_without_http(self):
        first = self.capture()
        request = self.refresh(first)
        refreshed = self.capture(request=request)
        original = load_json(refreshed["path"], 8 * 1024 * 1024)
        for change in ({"schema_version": 1}, {"capture_id": first["capture_id"]},
                       {"request": REQUEST}, {"bundle_hash": "incorrect"}):
            atomic_json(refreshed["path"], {**original, **change})
            with self.assertRaisesRegex(SafetyError, "SAVED_SUPPLIER_CAPTURE_INVALID"):
                self.capture(request=request)
            with self.assertRaises(SafetyError):
                load_saved_supplier_input(self.memory, refreshed["path"])
        self.assertEqual(len(self.reader.calls), 2)

    def test_refresh_interruption_after_atomic_save_reuses_completed_revisit(self):
        first = self.capture()
        request = self.refresh(first)
        def save_then_interrupt(path, value):
            atomic_json(path, value)
            raise RuntimeError("Synthetic stop after completed refresh")
        with patch("product_radar.supplier_capture_store.atomic_json", side_effect=save_then_interrupt), \
                self.assertRaises(RuntimeError):
            self.capture(request=request)
        replayed = self.capture(request=request)
        self.assertTrue(replayed["reused"])
        self.assertEqual(len(self.reader.calls), 2)

    def test_refresh_keeps_same_bounded_seed_and_linked_page_reads(self):
        request = {**REQUEST, "lead": {**REQUEST["lead"], "urls": [BASE + "/product/caddy", BASE + "/catalogue", BASE + "/shipping"]}}
        reader = Reader({BASE + "/product/caddy": {"discovered_links": [BASE + "/pricing", BASE + "/faq"]}})
        first = self.capture(request=request, reader=reader)
        refresh_request = {**request, "refresh": self.refresh(first)["refresh"]}
        refreshed = self.capture(request=refresh_request, reader=reader)
        self.assertEqual(len(reader.calls), 8)
        self.assertEqual(refreshed["bundle"]["retrieval"]["fetched"], 4)
        self.assertEqual(first["bundle"]["item"], refreshed["bundle"]["item"])

    def test_refresh_uses_its_isolated_lock_and_leaves_financial_lock_available(self):
        first = self.capture()
        request = self.refresh(first)
        def reader(item):
            with self.memory.journal.locked():
                pass
            with self.assertRaisesRegex(SafetyError, "RUNNER_ALREADY_ACTIVE"):
                self.capture(request=request, reader=Reader())
            return self.reader(item)
        self.capture(request=request, reader=reader)
        self.assertEqual(len(self.reader.calls), 2)

    def test_changed_product_during_refresh_retains_but_rejects_new_current_input(self):
        first = self.capture()
        original_bytes = Path(first["path"]).read_bytes()
        request = self.refresh(first)
        def reader(item):
            old = self.memory.snapshot()["concepts"][REQUEST["concept_id"]]
            self.memory.put("concepts", REQUEST["concept_id"],
                            {**old, "description": {**old["description"], "label": "Changed caddy"}}, previous=old)
            return self.reader(item)
        with self.assertRaisesRegex(SafetyError, "SUPPLIER_CAPTURE_DESCRIPTION_CHANGED"):
            self.capture(request=request, reader=reader)
        self.assertEqual(Path(first["path"]).read_bytes(), original_bytes)
        paths = list((self.memory.root / "supplier_pages").glob("*/capture.json"))
        self.assertEqual(len(paths), 2)
        refreshed_path = next(path for path in paths if str(path) != first["path"])
        with self.assertRaisesRegex(SafetyError, "SUPPLIER_CAPTURE_DESCRIPTION_CHANGED"):
            load_saved_supplier_input(self.memory, refreshed_path)


if __name__ == "__main__":
    unittest.main()
