"""Real synthetic capture/meaning/source/programme supplier joins, no network."""

from copy import deepcopy
import unittest
from unittest.mock import patch

from product_radar.functional_types import (accept_functional_definition, effective_functional_definition)
from product_radar.models import SafetyError
from product_radar.programme import _suppliers
from product_radar.source_coverage import prepare_source_tasks, execute_source_run
from product_radar.source_interpretation import review_supplier_results
from product_radar.supplier_capture_store import capture_saved_supplier_bundle
from product_radar.supplier_checks import prepare_supplier_candidates, current_supplier_capture_review
from product_radar.supplier_terms import current_supplier_terms, VERSION
import test_functional_type_review as meaning_fixtures
from test_scoped_supplier_terms import Reader, CandidateRunner, BASE, NOW
from test_suppliers import FixtureRunner


class ScopedSupplierSourceJoinTests(unittest.TestCase):
    def setUp(self):
        meaning_fixtures.FunctionalTypeReviewTests.setUp(self)
        meaning_fixtures.FunctionalTypeReviewTests.accept(self, example_decisions=self.decisions)
        self.request = {"concept_id": self.cid, "lead": {"id": "fictional-joined-lead",
            "urls": [BASE + "/about", BASE + "/offers/a", BASE + "/offers/b"]}}
        self.saved = capture_saved_supplier_bundle(self.memory, self.request, fetcher=Reader())

    def review(self, runner=None, **kwargs):
        with patch("product_radar.supplier_checks.utc_now", return_value=NOW), \
             patch("product_radar.suppliers.utc_now", return_value=NOW):
            return review_supplier_results(self.memory, runner or CandidateRunner(), as_of=NOW,
                concept_ids=[self.cid], scoped_concept_ids=[self.cid], **kwargs)

    def test_actual_source_entrypoint_scopes_plain_text_and_replays_without_f(self):
        runner = CandidateRunner(); result = self.review(runner)
        self.assertEqual(result["failures"], {}, result)
        self.assertEqual(result["remaining_inputs"], 0)
        self.assertEqual(len(result["accepted"]), 3)
        self.assertEqual(result["source_calls"], 0)
        records = self.memory.snapshot()["supplier_assessments"]
        self.assertEqual(sum(r.get("version") == VERSION for r in records.values()), 3)
        before = self.memory.snapshot(); replay = self.review(runner)
        self.assertEqual(replay["remaining_inputs"], 0)
        self.assertEqual(replay["reused_capture_ids"], [self.saved["capture_id"]])
        self.assertEqual(self.memory.snapshot(), before)
        self.assertEqual(len(runner.calls), 3)

    def test_default_source_entrypoint_still_uses_original_f3_contract(self):
        runner = FixtureRunner()
        with patch("product_radar.suppliers.utc_now", return_value=NOW):
            result = review_supplier_results(self.memory, runner, as_of=NOW, concept_ids=[self.cid])
        self.assertEqual(result["failures"], {}, result)
        self.assertEqual(len(runner.calls), 1)
        self.assertNotIn("supplier_contract", runner.calls[0])
        self.assertTrue(all(row.get("version") == "supplier-terms-v1"
                            for row in self.memory.snapshot()["supplier_assessments"].values()))

    def test_actual_programme_supplier_stage_selects_new_contract_for_accepted_meaning(self):
        runner = CandidateRunner()
        record = {"concept_ids": [self.cid], "deferrals": {}}
        context = {"runner": runner, "supplier_limit": 5, "at": NOW,
                   "retry_seconds": 300, "clock": None, "fixed_as_of": NOW}
        with patch("product_radar.supplier_checks.utc_now", return_value=NOW):
            result = _suppliers(self.memory, record, context)
        self.assertEqual(result["remaining_inputs"], 0)
        self.assertEqual(result["failures"], {}, result)
        self.assertEqual(len(runner.calls), 3)
        self.assertTrue(all(call["mode"] == "supplier_scope_candidates" for call in runner.calls))
        self.assertEqual(len(current_supplier_terms(self.memory.snapshot(), concept_id=self.cid, as_of=NOW)["all"]), 3)

    def test_actual_original_company_task_settles_only_after_every_page_application(self):
        planned = prepare_source_tasks(self.memory, [{"route": "company-pages", "request": {
            "candidate_urls": {self.cid: self.request["lead"]["urls"]}, "country": "IN"},
            "concept_ids": [self.cid]}], run_id="supplier-joined-synthetic", as_of=NOW)
        def handler(context):
            context["record_dispatch"]()
            return {"schema_version": 1, "route": "company-pages", "acquisition_id": context["acquisition_id"],
                "observed_at": NOW, "state": "usable", "dispatch_started": True, "reference": self.saved["path"],
                "capture_ids": [], "observation_ids": [], "evidence_refs": [self.saved["path"]],
                "reason": "Synthetic actual saved supplier-page pass.", "target_results": [{"concept_id": self.cid,
                    "description_hash": context["description_hashes"][self.cid], "state": "interpretation_pending",
                    "evidence_refs": [self.saved["path"]], "reason": "Actual pages await scoped review."}], "retryable": False}
        execute_source_run(self.memory, "supplier-joined-synthetic", handlers={"company-pages": handler}, as_of=NOW,
            bounded_call=lambda callback, context, timeout: callback(context))
        task_id = planned["task_ids"][0]; runner = CandidateRunner()
        first = self.review(runner, limit=1)
        self.assertEqual(first["remaining_inputs"], 2)
        self.assertEqual(first["source_bindings"], [])
        self.assertEqual(self.memory.snapshot()["query_tasks"][task_id]["target_results"][self.cid]["state"], "interpretation_pending")
        second = self.review(runner, limit=5)
        self.assertEqual(second["remaining_inputs"], 0)
        self.assertEqual([(r["task_id"], r["state"]) for r in second["source_bindings"]], [(task_id, "completed_relevant")])
        self.assertEqual(len(runner.calls), 3)

    def test_scoped_source_opt_in_refuses_an_unaccepted_legacy_subject(self):
        # A different real saved concept remains legacy; a new contract is
        # never activated simply by passing a misleading concept ID.
        other = next(cid for cid in self.memory.snapshot()["concepts"] if cid != self.cid)
        with self.assertRaisesRegex(SafetyError, "SUPPLIER_FUNCTIONAL_DEFINITION_REQUIRED"):
            review_supplier_results(self.memory, CandidateRunner(), as_of=NOW,
                concept_ids=[other], scoped_concept_ids=[other])

    def test_old_date_application_not_renewed_by_selection_or_definition_title(self):
        self.review()
        before = self.memory.snapshot()
        meaning = effective_functional_definition(before, self.cid)
        definition = {name: deepcopy(meaning[name]) for name in self.definition}
        definition["report_title"] = "Tidier sink tools"
        accept_functional_definition(self.memory, self.cid, definition, basis_refs=self.refs,
            reviewer="synthetic_title_only", reviewed_at=NOW, expected_meaning_hash=meaning["meaning_hash"])
        runner = CandidateRunner(); replay = self.review(runner)
        self.assertEqual(replay["remaining_inputs"], 0)
        self.assertEqual(runner.calls, [])
        prepared = prepare_supplier_candidates(self.memory, self.saved["path"])
        self.assertTrue(current_supplier_capture_review(self.memory.snapshot(), prepared, as_of=NOW)["complete"])
        terms = current_supplier_terms(self.memory.snapshot(), concept_id=self.cid, as_of=NOW)
        self.assertEqual({row["source_capture_at"] for row in terms["all"]}, {NOW})
        self.assertEqual(self.memory.snapshot()["supplier_assessments"], before["supplier_assessments"])


if __name__ == "__main__":
    unittest.main()
