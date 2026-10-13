"""Saved source/meaning scope contracts; no browser, provider or model calls."""

from copy import deepcopy
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from product_radar.functional_types import effective_functional_definition
from product_radar.models import SafetyError, digest
from product_radar.providers.apify import map_meta_rows
from product_radar.research_ai import MODEL, PROMPT_VERSION
from product_radar.research_memory import ResearchMemory, change, import_capture, key
from product_radar.research_questions import prepare_research_questions
from product_radar.research_scope import (accept_catalogue_scope, accept_scoped_evidence,
    current_catalogue_scopes, validated_scoped_evidence)
from product_radar.storage import Journal, atomic_json
from test_catalogue_expansion import fixture
from test_research_flow import FakeRunner, rows
from test_research_records import paid_task


NOW = "2026-10-06T10:00:00+00:00"
OBSERVED = "2026-10-04T10:00:00+00:00"
LATER = "2026-10-07T10:00:00+00:00"


class ScopeTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        root = Path(self.temp.name)
        self.memory = ResearchMemory(root / "research", Journal(root / "finance"), evidence_type="synthetic")
        self.base = fixture()
        self.cid = "existing-caddy-type"
        self.description = {"label": "Draining sink caddy", "category": "Home and Kitchen",
                            "function": "Store wet sink essentials", "mechanism": "Passive draining tray",
                            "features": ["Draining base"], "audience": "Households", "basis": "observed",
                            "brand": "generic_or_small", "reason": "Synthetic source-bound test."}
        self.memory.put("concepts", self.cid, {"description": self.description, "status": "provisional"})
        self.captured = import_capture(self.memory, source="meta", acquisition_id="saved-real-shape", observed_at=OBSERVED,
            mapped=map_meta_rows(rows(), purpose="discovery", query_reference="sink-caddy"), capture_hash=digest(rows()),
            reference={"attempt_id": "attempt-original", "file": "captured_rows.json"})
        state = self.memory.snapshot()
        self.oid = next(oid for oid, row in state["observations"].items() if "caddy" in row["semantic"].get("title", "").lower())
        self.memory.put("associations", "existing-child", {"concept_id": self.cid,
            "semantic_id": state["observations"][self.oid]["semantic_id"], "status": "resolved", "reason": "Existing functional match."})
        self.memory.put("query_tasks", "actual-paid-owner", paid_task())
        self.initial = self.memory.snapshot()

    def references(self, table="observations", record_id=None):
        record_id = record_id or self.oid
        return [{"table": table, "id": record_id, "hash": digest(self.memory.snapshot()[table][record_id])}]

    def fields(self, table="observations", record_id=None, path=None):
        return [{"reference": {"table": table, "id": record_id or self.oid}, "path": path or ["facts", "active"]}]

    def evidence(self, **changes):
        values = dict(route="meta-ads", scope="narrow_type", relationship="relevant_family", support="positive",
            references=self.references(), usable_fields=self.fields(), limitations=["An ad is not a sales count."],
            reason="The saved example shows the reviewed draining function.", reviewer="synthetic_operator", as_of=NOW,
            expected_meaning_hash=effective_functional_definition(self.memory.snapshot(), self.cid)["meaning_hash"])
        values.update(changes)
        return accept_scoped_evidence(self.memory, self.cid, **values)

    def catalogue(self, **changes):
        values = dict(relationship="narrower", references=self.references(), reason="The draining type is narrower than the heading.",
            reviewer="synthetic_operator", as_of=NOW,
            expected_meaning_hash=effective_functional_definition(self.memory.snapshot(), self.cid)["meaning_hash"])
        values.update(changes)
        return accept_catalogue_scope(self.memory, self.base, self.cid, "caddy", **values)

    def test_additive_bindings_preserve_exact_original_dates_refs_and_facts(self):
        self.catalogue()
        result = self.evidence()
        state = self.memory.snapshot()
        for table in state:
            if table != "source_checks":
                self.assertEqual(state[table], self.initial[table])
        saved = validated_scoped_evidence(state, self.cid)[0]
        self.assertEqual(saved["id"], result["id"])
        self.assertEqual(saved["subject"]["concept_id"], self.cid)
        self.assertEqual(saved["meaning_basis"], "legacy_concept")
        self.assertEqual(saved["usable_fields"][0]["value"], state["observations"][self.oid]["facts"]["active"])
        self.assertEqual({row["observed_at"] for row in saved["source_contexts"]}, {OBSERVED})
        self.assertEqual({ref["table"] for ref in saved["references"]}, {"captures", "observations"})
        self.assertEqual(saved["support"], "positive")
        self.assertEqual(saved["admissibility"], "accepted")
        self.assertNotIn("market_votes", saved)
        self.assertFalse(self.memory.journal.path.exists())

    def test_repeat_preparation_keeps_first_review_date_and_writes_nothing(self):
        first = self.evidence()
        heading = self.catalogue()
        before = self.memory.snapshot()
        repeated = self.evidence(as_of=LATER)
        repeat_heading = self.catalogue(as_of=LATER)
        self.assertEqual(first["id"], repeated["id"])
        self.assertEqual(heading["id"], repeat_heading["id"])
        self.assertTrue(repeated["reused"])
        self.assertTrue(repeat_heading["reused"])
        self.assertEqual(self.memory.snapshot(), before)

    def test_corrected_same_binding_supersedes_without_rewriting_or_resurrecting_old_review(self):
        first = self.evidence()
        corrected = self.evidence(support="not_positive", reason="The same evidence was reviewed more cautiously.", as_of=LATER)
        rows = validated_scoped_evidence(self.memory.snapshot(), self.cid)
        self.assertEqual([row["id"] for row in rows], [corrected["id"]])
        self.assertEqual(rows[0]["revision"], 2)
        self.assertEqual(rows[0]["previous_binding_id"], first["id"])
        self.assertEqual(rows[0]["support"], "not_positive")
        old = self.evidence()
        self.assertTrue(old["reused"])
        self.assertFalse(old["current"])
        self.assertEqual([row["id"] for row in validated_scoped_evidence(self.memory.snapshot(), self.cid)], [corrected["id"]])
        historical = validated_scoped_evidence(self.memory.snapshot(), self.cid, as_of=NOW)
        self.assertEqual([row["id"] for row in historical], [first["id"]])
        heading = self.catalogue()
        correction = self.catalogue(relationship="unrelated", reason="An explicit corrected comparison.", as_of=LATER)
        current = current_catalogue_scopes(self.memory.snapshot(), self.base, self.cid)
        self.assertEqual([row["id"] for row in current], [correction["id"]])
        self.assertEqual(current[0]["previous_binding_id"], heading["id"])

    def test_revision_chain_is_contiguous_and_corrected_review_cannot_be_backdated(self):
        first = self.evidence()
        second = self.evidence(support="neutral", as_of=LATER)
        missing = self.memory.snapshot()
        del missing["source_checks"][first["id"]]
        with self.assertRaisesRegex(SafetyError, "REVISION_CHAIN_INVALID"):
            validated_scoped_evidence(missing, self.cid)
        with self.assertRaisesRegex(SafetyError, "REVIEW_ORDER_INVALID"):
            self.evidence(support="not_positive", as_of=NOW)
        state = self.memory.snapshot()
        row = state["source_checks"][second["id"]]
        row["previous_binding_id"] = "invented-previous"
        row["record_hash"] = digest({k: v for k, v in row.items() if k != "record_hash"})
        with self.assertRaisesRegex(SafetyError, "REVISION_CHAIN_INVALID"):
            validated_scoped_evidence(state, self.cid)

    def test_concurrent_corrected_judgment_cannot_fork_same_revision_chain(self):
        self.evidence(support="neutral")
        original = self.memory.commit
        injected = False
        def racing(event_id, changes, *, validate=None):
            nonlocal injected
            if not injected:
                injected = True
                self.evidence(support="not_positive", reason="A concurrent accepted correction.", as_of=LATER)
            return original(event_id, changes, validate=validate)
        with patch.object(self.memory, "commit", racing), self.assertRaisesRegex(SafetyError, "REVISION_CHANGED"):
            self.evidence(support="positive", reason="The competing judgment.", as_of=LATER)
        current = validated_scoped_evidence(self.memory.snapshot(), self.cid)
        self.assertEqual(len(current), 1)
        self.assertEqual((current[0]["revision"], current[0]["support"]), (2, "not_positive"))

    def test_missing_original_request_context_cannot_be_upgraded_to_positive_support(self):
        state = self.memory.snapshot()
        old = state["query_tasks"]["actual-paid-owner"]
        self.memory.put("query_tasks", "actual-paid-owner", {"kind": "unknown-owner-v9"}, previous=old)
        with self.assertRaisesRegex(SafetyError, "REQUEST_CONTEXT_REQUIRED"):
            self.evidence()
        result = self.evidence(support="insufficient", usable_fields=[])
        self.assertEqual(validated_scoped_evidence(self.memory.snapshot(), self.cid)[0]["id"], result["id"])

    def test_broader_example_and_supplier_scope_are_retained_not_promoted(self):
        self.evidence(scope="broader_context", relationship="broader_context")
        self.evidence(scope="example_offer", relationship="example_offer")
        check = {"kind": "supplier_route_search", "source": "supplier", "route": "tradeindia", "at": OBSERVED,
                 "state": "interpretation_pending", "concept_id": self.cid, "description_hash": digest(self.description),
                 "reference": "saved_supplier_capture.json", "acquisition_id": "saved-supplier", "query": "sink caddy manufacturer India",
                 "candidates": [{"company": "Synthetic supplier", "url": "https://example.org/company", "product": "Sink caddy"}],
                 "reason": "Public directory lead, not confirmed dispatch."}
        self.memory.put("source_checks", "actual-supplier-shape", check)
        self.evidence(route="tradeindia", scope="supplier_company", relationship="relevant_family",
            references=self.references("source_checks", "actual-supplier-shape"),
            usable_fields=self.fields("source_checks", "actual-supplier-shape", ["candidates"]))
        result = validated_scoped_evidence(self.memory.snapshot(), self.cid)
        self.assertEqual({row["scope"] for row in result}, {"broader_context", "example_offer", "supplier_company"})
        self.assertTrue(all("market_votes" not in row for row in result))
        with self.assertRaisesRegex(SafetyError, "RELATIONSHIP_SCOPE_MISMATCH"):
            self.evidence(scope="narrow_type", relationship="broader_context")

    def test_unrelated_and_unclear_review_is_audit_not_accepted_positive(self):
        self.evidence(relationship="unrelated")
        self.evidence(relationship="unclear", support="insufficient", usable_fields=[])
        result = validated_scoped_evidence(self.memory.snapshot(), self.cid)
        self.assertEqual({row["admissibility"] for row in result}, {"unrelated", "unclear"})
        self.assertFalse(any(row["admissibility"] == "accepted" and row["support"] == "positive" for row in result))

    def test_invented_source_fields_wrong_source_future_source_and_stale_meaning_refused(self):
        cases = [({"references": [{"table": "observations", "id": "invented"}]}, "REFERENCE_MISSING"),
                 ({"usable_fields": self.fields(path=["facts", "invented"])}, "USABLE_FIELD_MISSING"),
                 ({"route": "amazon-search"}, "SOURCE_MISMATCH"),
                 ({"as_of": "2026-10-01T00:00:00+00:00"}, "REVIEW_BEFORE_SOURCE"),
                 ({"expected_meaning_hash": "0" * 64}, "MEANING_CHANGED"),
                 ({"references": [dict(self.references()[0], hash="0" * 64)]}, "REFERENCE_CHANGED")]
        for changes, message in cases:
            with self.subTest(message=message), self.assertRaisesRegex(SafetyError, message):
                self.evidence(**changes)
        with self.assertRaisesRegex(SafetyError, "FUNCTIONAL_CONCEPT_NOT_FOUND"):
            accept_scoped_evidence(self.memory, "invented-concept", route="meta-ads", scope="narrow_type",
                relationship="exact_product", support="positive", references=self.references(), usable_fields=self.fields(),
                limitations=[], reason="Test", reviewer="test", as_of=NOW, expected_meaning_hash="0" * 64)
        self.assertEqual(self.memory.snapshot(), self.initial)

    def test_changed_meaning_original_reference_or_association_invalidates_only_binding(self):
        self.evidence()
        original = self.memory.snapshot()
        for change_kind in ("meaning", "observation", "association", "capture"):
            changed = deepcopy(original)
            if change_kind == "meaning":
                changed["concepts"][self.cid]["description"]["mechanism"] = "Powered water pump"
            elif change_kind == "observation":
                changed["observations"][self.oid]["facts"]["active"] = False
            elif change_kind == "association":
                changed["associations"]["existing-child"]["status"] = "deferred"
            else:
                changed["captures"][self.captured["capture_id"]]["capture_hash"] = "0" * 64
            self.assertEqual(validated_scoped_evidence(changed, self.cid), [], change_kind)
        changed = deepcopy(original)
        changed["concepts"][self.cid]["description"]["label"] = "Harmless new display title"
        self.assertEqual(len(validated_scoped_evidence(changed, self.cid)), 1)
        self.assertEqual(validated_scoped_evidence(original, self.cid, as_of=OBSERVED), [])

    def test_paid_request_progress_does_not_invalidate_but_request_bounds_do(self):
        owner = paid_task()
        self.memory.put("query_tasks", "actual-paid-owner", owner)
        self.evidence()
        state = self.memory.snapshot()
        self.assertEqual(set(validated_scoped_evidence(state, self.cid)[0]["request_dependencies"]), {"actual-paid-owner"})
        changed = deepcopy(state)
        changed["query_tasks"]["actual-paid-owner"]["status"] = "completed"
        self.assertEqual(len(validated_scoped_evidence(changed, self.cid)), 1)
        changed["query_tasks"]["actual-paid-owner"]["request"]["coverage"]["limit"] += 1
        self.assertEqual(validated_scoped_evidence(changed, self.cid), [])

    def test_real_question_binding_survives_progress_not_substantive_changes(self):
        state = self.memory.snapshot()
        prepared = prepare_research_questions(self.memory, [{"subject": {"kind": "concept", "concept_id": self.cid,
            "definition_hash": effective_functional_definition(state, self.cid)["meaning_hash"]},
            "route": "meta-ads", "purpose": "validation", "request": {"query": "draining sink caddy", "country": "IN"}}], as_of=NOW)
        qid = prepared["question_ids"][0]
        self.evidence(question_id=qid)
        changed = self.memory.snapshot()
        changed["query_tasks"][qid]["progress"]["scheduling"] = "waiting"
        self.assertEqual(len(validated_scoped_evidence(changed, self.cid)), 1)
        changed["query_tasks"][qid]["request"]["query"] = "different function"
        self.assertEqual(validated_scoped_evidence(changed, self.cid), [])

    def test_catalogue_aliases_and_queries_are_not_meaning_but_scope_changes_are(self):
        self.catalogue()
        state = self.memory.snapshot()
        changed = deepcopy(self.base)
        changed["families"][0]["aliases"].append("Caddy for sinks")
        changed["families"][0]["phrases"][0]["text"] = "draining caddy"
        self.assertEqual(len(current_catalogue_scopes(state, changed, self.cid)), 1)
        changed["families"][0]["definition"] = "Powered tools for cleaning sinks"
        self.assertEqual(current_catalogue_scopes(state, changed, self.cid), [])
        changed["families"][0]["disposition"] = "proposed"
        self.assertEqual(current_catalogue_scopes(state, changed, self.cid), [])
        with self.assertRaisesRegex(SafetyError, "FAMILY_NOT_ACCEPTED"):
            accept_catalogue_scope(self.memory, changed, self.cid, "caddy", relationship="same_scope",
                references=self.references(), reason="Test", reviewer="test", as_of=NOW,
                expected_meaning_hash=effective_functional_definition(state, self.cid)["meaning_hash"])

    def test_capture_existence_and_metadata_cannot_create_positive_support(self):
        capture_id = self.captured["capture_id"]
        self.evidence(support="insufficient", references=self.references("captures", capture_id), usable_fields=[])
        with self.assertRaisesRegex(SafetyError, "SUBSTANTIVE_FIELDS_REQUIRED"):
            self.evidence(references=self.references("captures", capture_id),
                          usable_fields=self.fields("captures", capture_id, ["capture_hash"]))
        with self.assertRaisesRegex(SafetyError, "REFERENCE_NOT_SOURCE"):
            self.evidence(references=[{"table": "source_checks", "id": self.catalogue()["id"]}])

    def test_concurrent_association_change_is_refused_under_short_commit(self):
        original = self.memory.commit
        injected = False
        def racing(event_id, changes, *, validate=None):
            nonlocal injected
            if not injected:
                injected = True
                old = self.memory.snapshot()["associations"]["existing-child"]
                original("concurrent-scope-correction", [change("associations", "existing-child", dict(old, status="deferred"), old)])
            return original(event_id, changes, validate=validate)
        with patch.object(self.memory, "commit", racing), self.assertRaisesRegex(SafetyError, "DEPENDENCY_CHANGED"):
            self.evidence()
        self.assertFalse(any(row.get("kind") == "scoped-evidence-v1" for row in self.memory.snapshot()["source_checks"].values()))

    def test_tampered_overlay_hash_is_not_silently_accepted(self):
        result = self.evidence()
        state = self.memory.snapshot()
        state["source_checks"][result["id"]]["scope"] = "broader_context"
        with self.assertRaisesRegex(SafetyError, "RECORD_HASH_MISMATCH"):
            validated_scoped_evidence(state, self.cid)

    def test_quickcommerce_original_location_price_and_offer_paths_are_exact(self):
        from product_radar.quickcommerce import VERSION, map_response, prepare_query, save_capture
        from test_quickcommerce import LOCATION, payload
        request = prepare_query("blinkit", "draining sink caddy", deepcopy(LOCATION))
        raw = payload(request)
        raw["data"]["products"][0]["name"] = "Synthetic draining sink caddy"
        receipt = {"version": VERSION, "request": request, "observed_at": OBSERVED,
                   "dispatch_started": True, "purchase_made": False, "apify_usd": "0",
                   "raw_response": raw, "public_response_hash": digest(raw), "body_complete": True, "http_status": 200,
                   **map_response(raw, request)}
        imported = save_capture(self.memory, "saved-blinkit", receipt)
        oid = imported["observation_ids"][0]
        self.evidence(route="blinkit", scope="example_offer", relationship="example_offer",
            references=self.references("observations", oid), usable_fields=self.fields("observations", oid, ["facts", "price"]))
        saved = next(row for row in validated_scoped_evidence(self.memory.snapshot(), self.cid) if row["route"] == "blinkit")
        self.assertEqual(saved["usable_fields"][0]["value"], self.memory.snapshot()["observations"][oid]["facts"]["price"])
        context = next(row for row in saved["source_contexts"] if row["table"] == "observations")
        self.assertEqual(context["location"], LOCATION)
        self.assertEqual(context["observed_at"], OBSERVED)
        changed = self.memory.snapshot()
        changed["captures"][imported["capture_id"]]["request"]["location"]["name"] = "Different reference location"
        self.assertFalse(any(row["route"] == "blinkit" for row in validated_scoped_evidence(changed, self.cid)))

    def test_actual_trends_series_retains_broad_query_country_period_and_original_date(self):
        from product_radar.research_operations import import_trends_evidence
        from test_trends_browser import metadata, weekly
        imported = import_trends_evidence(self.memory, concept_id=self.cid, acquisition_id="saved-chart", csv_bytes=weekly(),
            metadata=metadata(collected_at=NOW))
        self.evidence(route="google-trends", scope="broader_context", relationship="broader_context",
            references=self.references("trends_evidence", imported["id"]),
            usable_fields=self.fields("trends_evidence", imported["id"], ["parsed", "series", 0, "value"]))
        saved = next(row for row in validated_scoped_evidence(self.memory.snapshot(), self.cid) if row["route"] == "google-trends")
        context = saved["source_contexts"][0]
        self.assertEqual(context["request"]["term"], "sink organiser")
        self.assertEqual(context["request"]["geo"], "IN")
        self.assertEqual(context["request"]["time_range"], "today 12-m")
        self.assertEqual(context["observed_at"], NOW)
        self.assertEqual(saved["scope"], "broader_context")

    def test_concurrent_meaning_change_and_job_result_change_do_not_commit_stale_review(self):
        original = self.memory.commit
        old = self.memory.snapshot()["concepts"][self.cid]
        injected = False
        def racing(event_id, changes, *, validate=None):
            nonlocal injected
            if not injected:
                injected = True
                changed = deepcopy(old)
                changed["description"]["mechanism"] = "Powered pump"
                original("concurrent-meaning-correction", [change("concepts", self.cid, changed, old)])
            return original(event_id, changes, validate=validate)
        with patch.object(self.memory, "commit", racing), self.assertRaisesRegex(SafetyError, "DEPENDENCY_CHANGED"):
            self.evidence()
        job_id = self.saved_job()
        path = self.memory.root / "ai_jobs" / job_id / "result.json"
        def changing_result(event_id, changes, *, validate=None):
            atomic_json(path, {"request_hash": "0" * 64, "response": {}, "usage": {}, "completed_at": NOW})
            return original(event_id, changes, validate=validate)
        with patch.object(self.memory, "commit", changing_result), self.assertRaisesRegex(SafetyError, "JOB_CHANGED"):
            self.evidence(job_id=job_id, job_item_id=self.cid)
        self.assertFalse(any(row.get("kind") == "scoped-evidence-v1" for row in self.memory.snapshot()["source_checks"].values()))

    def saved_job(self, *, completed=True, include_source=True):
        observation = dict(id=self.oid, **self.memory.snapshot()["observations"][self.oid])
        item = {"id": self.cid, "description": self.description, "observations": [observation] if include_source else []}
        payload = {"items": [item]}
        request = {"role": "E", "payload": payload, "model": MODEL, "prompt_version": PROMPT_VERSION, "runner": "synthetic-only-v1"}
        job_id = key("job", request)
        self.memory.put("jobs", job_id, {"request": request,
            "binding_keys": {self.cid: digest(["E", item, {}, MODEL, PROMPT_VERSION, request["runner"]])}})
        if completed:
            response, usage = FakeRunner()("E", payload, None)
            atomic_json(self.memory.root / "ai_jobs" / job_id / "result.json",
                        {"request_hash": digest(request), "response": response, "usage": usage, "completed_at": NOW})
        return job_id

    def test_job_needs_actual_accepted_result_and_source_bound_item_not_status_field(self):
        incomplete = self.saved_job(completed=False)
        with self.assertRaisesRegex(SafetyError, "JOB_INVALID"):
            self.evidence(job_id=incomplete, job_item_id=self.cid)
        complete = self.saved_job(completed=True)
        self.assertNotIn("status", self.memory.snapshot()["jobs"][complete])
        result = self.evidence(job_id=complete, job_item_id=self.cid)
        row = self.memory.snapshot()["source_checks"][result["id"]]
        self.assertEqual(row["job_proof"]["job_id"], complete)
        wrong = self.saved_job(include_source=False)
        with self.assertRaisesRegex(SafetyError, "JOB_SOURCE_MISMATCH"):
            self.evidence(job_id=wrong, job_item_id=self.cid)


if __name__ == "__main__":
    unittest.main()
