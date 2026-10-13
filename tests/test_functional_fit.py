"""Joined functional checklist contracts; disposable facts and fake runner only."""

from copy import deepcopy
import unittest
from unittest.mock import patch

from product_radar.fit_description import (FUNCTIONAL_CONTRACT, FUNCTIONAL_INFERENCE_KIND,
    FUNCTIONAL_STYLE, functional_changed_criteria, functional_inference_context,
    FUNCTIONAL_RESPONSE_VERSION, FUNCTIONAL_SELECTION_KIND)
from product_radar.fit_policy import ACTIVE_CRITERION_IDS, FIT_POLICY_ID, evaluate_fit
from product_radar.fit_review import (_payload, apply_fit_review, prepare_fit_review,
    prepare_practical_fit_reviews, run_fit_reviews)
from product_radar.functional_types import functional_role_context
from product_radar.models import SafetyError, digest
from product_radar.research_ai import e_contract, validate_results
from product_radar.research_flow import assessment_item, candidate_summary, current_assessment_fit
from product_radar.research_memory import key
from product_radar.research_scope import accept_scoped_evidence
import test_functional_queries
from test_functional_queries import DEFINITION, LATER
from test_research_flow import NOW, capture


def response_for(payload):
    values = []
    for item in payload["items"]:
        context = item["description_inference_context"]
        fields = {row["field"]: row for row in context["allowed_fields"]}
        criteria = []
        for number in payload["requested_criterion_ids"]:
            chosen = "example_audiences" if number == 13 and "example_audiences" in fields else "primary_use"
            criteria.append({"id": number, "state": "Meets", "basis": "inferred", "references": [],
                "reason": "Estimated: The described use supports this ordinary practical judgment.",
                "description_inference": {"kind": FUNCTIONAL_INFERENCE_KIND,
                    "meaning_hash": item["meaning_hash"], "fields": [deepcopy(fields[chosen])]}})
        values.append({"id": item["id"], "criteria": criteria, "relevance": [],
            "compliance_note": "Client review where relevant.", "missing_questions": [],
            "audience_positioning": {"positioning": "women_centric" if 13 in payload["requested_criterion_ids"] else "unclear",
                                     "reason": "Estimated household buyer positioning."}})
        if payload.get("response_attribution") == FUNCTIONAL_RESPONSE_VERSION:
            for row in criteria:
                row["description_inference"] = {"kind": FUNCTIONAL_SELECTION_KIND,
                    "fields": [entry["field"] for entry in row["description_inference"]["fields"]]}
    return {"results": values}


class FunctionalFitRunner:
    identity = "synthetic-functional-fit-v1"

    def __init__(self, *, action=None, invalid=False):
        self.calls, self.action, self.invalid = [], action, invalid

    def __call__(self, role, payload, folder):
        self.calls.append((role, deepcopy(payload)))
        response = response_for(payload)
        if self.invalid and len(self.calls) == 1:
            response["results"][0]["criteria"].pop()
        if self.action:
            action, self.action = self.action, None
            action()
        return response, {"input_tokens": 1, "output_tokens": 1, "cached_input_tokens": 0}


class FunctionalFitTests(unittest.TestCase):
    setUp = test_functional_queries.FunctionalQueryTests.setUp
    accept = test_functional_queries.FunctionalQueryTests.accept

    def prepare(self):
        return prepare_practical_fit_reviews(self.memory, concept_ids=[self.cid], as_of=NOW,
                                             contract_version=FUNCTIONAL_CONTRACT)[0]

    def complete(self, runner=None):
        row = self.prepare()
        runner = runner or FunctionalFitRunner()
        result = run_fit_reviews(self.memory, runner, [row["review_id"]])
        self.assertEqual(result[0]["status"], "accepted", result)
        return runner, row

    def physical_fact(self, *, field="weight", value="about a small household item"):
        state = self.memory.snapshot()
        source_id = key("synthetic-supplier-physical", [field, value])
        saved = {"kind": "supplier_route_search", "source": "supplier", "route": "tradeindia", "at": NOW,
            "state": "interpretation_pending", "concept_id": self.cid,
            "description_hash": digest(state["concepts"][self.cid]["description"]),
            "reference": "synthetic_supplier_capture.json", "acquisition_id": source_id,
            "query": "draining sink caddy manufacturer India",
            "candidates": [{"company": "Synthetic supplier", "url": "https://example.org/company",
                            "product": "Draining sink caddy", field: value}],
            "reason": "Public directory lead, not confirmed dispatch."}
        self.memory.put("source_checks", source_id, saved)
        # This is an explicit operator review of a real saved source field.
        return accept_scoped_evidence(self.memory, self.cid, route="tradeindia", scope="supplier_offer",
            relationship="exact_product", support="insufficient",
            references=[{"table": "source_checks", "id": source_id, "hash": digest(saved)}],
            usable_fields=[{"reference": {"table": "source_checks", "id": source_id}, "path": ["candidates", 0, field]}],
            limitations=["This physical detail describes only the reviewed example."],
            reason="Synthetic reviewed physical fact, not demand proof.", reviewer="synthetic", as_of=NOW,
            expected_meaning_hash=functional_role_context(self.memory.snapshot(), self.cid)["meaning_hash"])

    def test_legacy_input_stays_exact_and_v4_requires_an_accepted_definition(self):
        state = self.memory.snapshot()
        old = assessment_item(state, self.cid)
        self.assertEqual(set(old), {"id", "description", "observations"})
        with self.assertRaisesRegex(SafetyError, "FIT_FUNCTIONAL_DEFINITION_REQUIRED"):
            assessment_item(state, self.cid, contract_version=FUNCTIONAL_CONTRACT)
        self.accept()
        self.assertEqual(assessment_item(self.memory.snapshot(), self.cid), old)

    def test_new_semantic_question_excludes_titles_brand_proof_dates_and_base_ids(self):
        self.accept(); row = self.prepare(); base = self.memory.snapshot()["source_checks"][row["review_id"]]
        payload = _payload(base); item = payload["items"][0]
        self.assertEqual(e_contract(payload)["version"], FUNCTIONAL_CONTRACT)
        self.assertEqual(payload["judgment_style"], FUNCTIONAL_STYLE)
        self.assertNotIn("label", item["description"])
        self.assertNotIn("report_title", item["description"])
        self.assertNotIn("brand", item["description"])
        self.assertNotIn("proof", item)
        self.assertNotIn("fit_review_base_id", item)
        self.assertNotIn("named_review_reason", item)
        self.assertIn("functional_proof", base["binding"])
        with self.assertRaisesRegex(SafetyError, "AI_E_CONTRACT_INVALID"):
            e_contract(payload, "research-e-fit10-description-v3")

    def test_full_ten_estimates_are_current_without_any_market_votes(self):
        self.accept(); before = self.memory.snapshot()
        runner, row = self.complete()
        state = self.memory.snapshot(); saved = state["assessments"][self.cid]
        self.assertEqual(current_assessment_fit(state, self.cid)["kpi_matches"], 10)
        self.assertEqual(saved["result"]["relevance"], [])
        self.assertEqual(saved["input"], assessment_item(state, self.cid, contract_version=FUNCTIONAL_CONTRACT))
        self.assertEqual(saved["record_hash"], digest({k: v for k, v in saved.items() if k != "record_hash"}))
        summary = next(item for item in candidate_summary(self.memory, as_of=NOW)["products"] if item["id"] == self.cid)
        self.assertEqual(summary["checklist_meets"], 10)
        self.assertEqual(state["market_reviews"], before["market_reviews"])
        self.assertEqual(state["observations"], before["observations"])
        self.assertEqual(self.prepare()["status"], "accepted")
        self.assertEqual(len(runner.calls), 1)

    def test_title_citation_and_date_revisions_reuse_all_checklist_answers(self):
        self.accept(refs=[self.refs[0]])
        runner, _ = self.complete(); before = self.memory.snapshot()
        self.accept(dict(DEFINITION, report_title="A cleaner looking sink"), at=LATER)
        state = self.memory.snapshot()
        self.assertEqual(current_assessment_fit(state, self.cid)["kpi_matches"], 10)
        self.assertEqual(self.prepare()["status"], "accepted")
        self.assertEqual(len(runner.calls), 1)
        self.assertEqual(state["assessments"], before["assessments"])
        self.assertEqual(state["jobs"], before["jobs"])

    def test_definition_only_is_reviewable_and_has_no_minimum_example_gate(self):
        self.accept(decisions={}); item = assessment_item(self.memory.snapshot(), self.cid, contract_version=FUNCTIONAL_CONTRACT)
        self.assertEqual(item["description"]["example_features"], [])
        self.assertEqual(item["observations"], [])
        self.complete()
        self.assertEqual(current_assessment_fit(self.memory.snapshot(), self.cid)["kpi_matches"], 10)

    def test_exact_estimate_measurements_and_invented_scope_fields_are_refused(self):
        self.accept(); row = self.prepare(); payload = _payload(self.memory.snapshot()["source_checks"][row["review_id"]])
        for change in ("Saves 10 minutes.", "Measured weight is 2 kg."):
            response = response_for(payload); response["results"][0]["criteria"][0]["reason"] = change
            self.assertEqual(validate_results("E", payload, response)["failures"][self.cid], "FIT_DESCRIPTION_INFERENCE_INVALID")
        response = response_for(payload)
        response["results"][0]["criteria"][0]["description_inference"]["fields"][0]["hash"] = "f" * 64
        self.assertEqual(validate_results("E", payload, response)["failures"][self.cid], "FIT_DESCRIPTION_FIELDS_INVALID")

    def test_new_response_selects_fields_and_code_owns_links_without_changing_raw_answer(self):
        self.accept(); row = self.prepare(); payload = _payload(self.memory.snapshot()["source_checks"][row["review_id"]])
        payload["response_attribution"] = FUNCTIONAL_RESPONSE_VERSION
        response = response_for(payload); before = deepcopy(response)
        response["results"][0]["criteria"][0]["reason"] = "Its ordinary construction is compact."
        before = deepcopy(response)
        checked = validate_results("E", payload, response)
        self.assertEqual(checked["failures"], {})
        self.assertEqual(response, before)
        marker = checked["accepted"][self.cid]["criteria"][0]["description_inference"]
        self.assertEqual(marker["meaning_hash"], payload["items"][0]["meaning_hash"])
        self.assertEqual(marker["fields"], [payload["items"][0]["description_inference_context"]["allowed_fields"][0]])

    def test_new_response_refuses_wrong_or_duplicate_fields_and_hash_bearing_markers(self):
        self.accept(); row = self.prepare(); payload = _payload(self.memory.snapshot()["source_checks"][row["review_id"]])
        payload["response_attribution"] = FUNCTIONAL_RESPONSE_VERSION
        for fields in ([], ["primary_use", "primary_use"], ["foreign_field"], ["example_audiences"]):
            response = response_for(payload)
            if fields == ["example_audiences"]:
                payload["items"][0]["description"]["example_audiences"] = []
                payload["items"][0]["description_inference_context"] = functional_inference_context(payload["items"][0]["description"])
            response["results"][0]["criteria"][0]["description_inference"]["fields"] = fields
            self.assertTrue(validate_results("E", payload, response)["failures"], fields)
        response = response_for(payload)
        response["results"][0]["criteria"][0]["description_inference"].update(meaning_hash="f" * 64)
        self.assertTrue(validate_results("E", payload, response)["failures"])

    def test_actual_new_job_uses_field_selection_and_reuses_on_restart(self):
        self.accept(); runner, row = self.complete()
        self.assertEqual(runner.calls[0][1]["response_attribution"], FUNCTIONAL_RESPONSE_VERSION)
        from product_radar.research_memory import ResearchMemory
        cold = ResearchMemory(self.memory.root, self.memory.journal, evidence_type="synthetic")
        again = run_fit_reviews(cold, runner, [row["review_id"]])
        self.assertFalse(again[0]["applied"])
        self.assertEqual(len(runner.calls), 1)

    def test_existing_legacy_hash_question_is_applied_without_new_question(self):
        from product_radar.research_ai import Jobs
        self.accept(); row = self.prepare(); payload = _payload(self.memory.snapshot()["source_checks"][row["review_id"]])
        runner = FunctionalFitRunner()
        Jobs(self.memory, runner).run("E", payload, declared_version=FUNCTIONAL_CONTRACT)
        outcomes = run_fit_reviews(self.memory, runner, [row["review_id"]])
        self.assertEqual(outcomes[0]["status"], "accepted")
        self.assertEqual(len(runner.calls), 1)
        self.assertNotIn("response_attribution", runner.calls[0][1])

    def test_fit_cannot_manufacture_market_relevance_from_projected_physical_fact(self):
        self.accept(); self.physical_fact()
        row = self.prepare(); base = self.memory.snapshot()["source_checks"][row["review_id"]]; payload = _payload(base)
        self.assertTrue(payload["items"][0]["observations"])
        response = response_for(payload)
        response["results"][0]["relevance"] = [{"id": "R1", "relationship": "exact_product", "reason": "Not market proof."}]
        self.assertEqual(validate_results("E", payload, response)["failures"][self.cid], "AI_FUNCTIONAL_FIT_MARKET_CLAIM_REFUSED")

    def test_weight_change_reopens_only_small_size_and_shipping_answers(self):
        self.accept(); runner, _ = self.complete(); old = self.memory.snapshot()["assessments"][self.cid]
        self.physical_fact()
        item = assessment_item(self.memory.snapshot(), self.cid, contract_version=FUNCTIONAL_CONTRACT)
        self.assertEqual(functional_changed_criteria(old, item), [1, 2])
        with self.assertRaisesRegex(SafetyError, "FIT_REVIEW_AFFECTED_CRITERIA_REQUIRED"):
            prepare_fit_review(self.memory, self.cid, requested_criterion_ids=[1], reason="incomplete request",
                               contract_version=FUNCTIONAL_CONTRACT)
        row = self.prepare(); base = self.memory.snapshot()["source_checks"][row["review_id"]]
        self.assertEqual(base["binding"]["requested_criterion_ids"], [1, 2])
        self.assertEqual(run_fit_reviews(self.memory, runner, [row["review_id"]])[0]["status"], "accepted")
        new = self.memory.snapshot()["assessments"][self.cid]
        self.assertEqual(new["result"]["criteria"][2:], old["result"]["criteria"][2:])
        self.assertEqual(current_assessment_fit(self.memory.snapshot(), self.cid)["kpi_matches"], 10)

    def test_audience_physical_fact_reopens_only_audience_answer(self):
        self.accept(); self.complete(); old = self.memory.snapshot()["assessments"][self.cid]
        self.physical_fact(field="audience", value="Shared household use")
        row = self.prepare(); base = self.memory.snapshot()["source_checks"][row["review_id"]]
        self.assertEqual(base["binding"]["requested_criterion_ids"], [13])
        self.assertEqual(len(base["binding"]["retained_rows"]), 9)
        self.assertEqual(functional_changed_criteria(old, base["binding"]["input"]), [13])

    def test_changed_meaning_requires_full_ten_and_retains_original_job(self):
        self.accept(); self.complete(); before = self.memory.snapshot()
        self.accept(dict(DEFINITION, mechanism="Wall mounted draining tray"), at=LATER)
        self.assertIsNone(current_assessment_fit(self.memory.snapshot(), self.cid))
        row = self.prepare(); base = self.memory.snapshot()["source_checks"][row["review_id"]]
        self.assertEqual(base["binding"]["requested_criterion_ids"], list(ACTIVE_CRITERION_IDS))
        for job_id, job in before["jobs"].items():
            self.assertEqual(self.memory.snapshot()["jobs"][job_id], job)

    def test_concurrent_meaning_change_keeps_old_answer_unapplied(self):
        self.accept(); row = self.prepare(); old = self.memory.snapshot()["assessments"][self.cid]
        runner = FunctionalFitRunner(action=lambda: self.accept(dict(DEFINITION, mechanism="Wall mounted draining tray"), at=LATER))
        result = run_fit_reviews(self.memory, runner, [row["review_id"]])
        self.assertEqual(result[0]["status"], "needs_reconciliation")
        self.assertEqual(self.memory.snapshot()["assessments"][self.cid], old)

    def test_saved_answer_after_lost_apply_recovers_without_another_model_call(self):
        self.accept(); row = self.prepare(); runner = FunctionalFitRunner()
        with patch("product_radar.fit_review.apply_fit_review", side_effect=SafetyError("LOCAL_INTERRUPTION")):
            result = run_fit_reviews(self.memory, runner, [row["review_id"]])
        self.assertEqual(result[0]["status"], "needs_reconciliation")
        result = run_fit_reviews(self.memory, runner, [row["review_id"]])
        self.assertEqual(result[0]["status"], "accepted")
        self.assertEqual(len(runner.calls), 1)

    def test_one_finite_shape_repair_preserves_original_answer(self):
        self.accept(); runner, _ = self.complete(FunctionalFitRunner(invalid=True))
        self.assertEqual(len(runner.calls), 2)
        self.assertIn("checklist_repair", runner.calls[1][1])
        self.assertEqual(self.prepare()["status"], "accepted")

    def test_title_only_new_base_reuses_the_same_original_shape_repair(self):
        self.accept(); row = self.prepare(); runner = FunctionalFitRunner(invalid=True)
        with patch("product_radar.fit_review.apply_fit_review", side_effect=SafetyError("LOCAL_INTERRUPTION")):
            first = run_fit_reviews(self.memory, runner, [row["review_id"]])
        self.assertEqual(first[0]["status"], "needs_reconciliation")
        self.assertEqual(len(runner.calls), 2)
        self.assertNotIn("review_id", runner.calls[1][1]["checklist_repair"])
        self.accept(dict(DEFINITION, report_title="Changed presentation only"), at=LATER)
        replacement = self.prepare()
        self.assertNotEqual(replacement["review_id"], row["review_id"])
        self.assertEqual(run_fit_reviews(self.memory, runner, [replacement["review_id"]])[0]["status"], "accepted")
        self.assertEqual(len(runner.calls), 2)

    def test_uncertain_shape_repair_cannot_be_relaunched_by_title_change(self):
        self.accept(); row = self.prepare()
        class UncertainRepair(FunctionalFitRunner):
            def __call__(self, role, payload, folder):
                if self.calls:
                    self.calls.append((role, deepcopy(payload)))
                    raise SafetyError("AI_NETWORK_UNAVAILABLE")
                return super().__call__(role, payload, folder)
        runner = UncertainRepair(invalid=True)
        run_fit_reviews(self.memory, runner, [row["review_id"]])
        self.assertEqual(len(runner.calls), 2)
        self.accept(dict(DEFINITION, report_title="Changed presentation only"), at=LATER)
        replacement = self.prepare()
        result = run_fit_reviews(self.memory, runner, [replacement["review_id"]])
        self.assertEqual(result[0]["reason"], "AI_JOB_PENDING_RECONCILIATION")
        self.assertEqual(len(runner.calls), 2)

    def test_large_healthy_items_are_packed_into_separate_model_jobs(self):
        from product_radar.functional_types import accept_functional_definition, effective_functional_definition
        from product_radar.models import canonical
        state = self.memory.snapshot()
        ids = sorted(state["concepts"])
        for cid in ids:
            children = [child for child, row in state["associations"].items() if row["concept_id"] == cid]
            semantics = {state["associations"][child]["semantic_id"] for child in children}
            refs = [oid for oid, row in state["observations"].items() if row["semantic_id"] in semantics]
            accept_functional_definition(self.memory, cid, dict(DEFINITION, primary_use="A synthetic useful function. " * 60),
                basis_refs=refs, reviewer="synthetic", reviewed_at=NOW,
                expected_meaning_hash=effective_functional_definition(self.memory.snapshot(), cid)["meaning_hash"],
                example_decisions={child: {"decision": "same", "reason": "Synthetic membership."} for child in children})
        rows = prepare_practical_fit_reviews(self.memory, concept_ids=ids, as_of=NOW, contract_version=FUNCTIONAL_CONTRACT)
        payloads = [_payload(self.memory.snapshot()["source_checks"][row["review_id"]]) for row in rows]
        combined = dict(payloads[0], items=[item for payload in payloads for item in payload["items"]])
        ceiling = (max(len(canonical(payload)) for payload in payloads) + len(canonical(combined))) // 2
        runner = FunctionalFitRunner()
        with patch("product_radar.research_ai.MAX_JOB_BYTES", ceiling):
            results = run_fit_reviews(self.memory, runner, [row["review_id"] for row in rows])
        self.assertEqual({row["status"] for row in results}, {"accepted"}, results)
        self.assertEqual(len(runner.calls), len(ids))

    def test_legacy_report_writer_cannot_silently_use_v4(self):
        self.accept(); self.complete()
        from product_radar.report_selection import _assessment
        from product_radar.research_flow import evidence_indexes
        state = self.memory.snapshot(); indexes = evidence_indexes(state)
        record, code, _ = _assessment(state, self.cid, state["concepts"][self.cid], NOW, *indexes,
                                     fit_policy_id=FIT_POLICY_ID)
        self.assertIsNone(record)
        self.assertEqual(code, "functional_report_contract_required")

    def test_new_legacy_question_cannot_downgrade_an_accepted_functional_type(self):
        from product_radar.fit_description import DESCRIPTION_CONTRACT
        self.accept(); self.complete(); before = self.memory.snapshot()
        outcome = prepare_practical_fit_reviews(self.memory, concept_ids=[self.cid], as_of=NOW,
                                                 contract_version=DESCRIPTION_CONTRACT)[0]
        self.assertEqual(outcome["status"], "not_reviewable")
        self.assertEqual(outcome["reason"], "FIT_FUNCTIONAL_CONTRACT_REQUIRED")
        with self.assertRaisesRegex(SafetyError, "FIT_FUNCTIONAL_CONTRACT_REQUIRED"):
            prepare_fit_review(self.memory, self.cid, requested_criterion_ids=list(ACTIVE_CRITERION_IDS),
                               reason="Wrong legacy contract", as_of=NOW, contract_version=DESCRIPTION_CONTRACT)
        self.assertEqual(self.memory.snapshot(), before)

    def test_corrupted_saved_semantic_input_or_result_is_not_current(self):
        self.accept(); self.complete()
        state = self.memory.snapshot(); altered = deepcopy(state)
        altered["assessments"][self.cid]["result"]["criteria"][0]["reason"] = "Changed without an owner."
        self.assertIsNone(current_assessment_fit(altered, self.cid))
        altered = deepcopy(state); altered["assessments"][self.cid]["input"]["meaning_hash"] = "f" * 64
        self.assertIsNone(current_assessment_fit(altered, self.cid))

    def test_corrupt_parent_cannot_retain_nine_rows_on_audience_change(self):
        self.accept(); self.complete(); state = self.memory.snapshot()
        parent = deepcopy(state["assessments"][self.cid])
        item = assessment_item(state, self.cid, contract_version=FUNCTIONAL_CONTRACT)
        item["description"]["example_audiences"] = ["Different current audience"]
        parent["result"]["criteria"][0]["state"] = "Does not meet"
        self.assertEqual(functional_changed_criteria(parent, item), list(ACTIVE_CRITERION_IDS))

    def test_price_photo_rank_and_unchanged_capture_date_do_not_reopen_fit(self):
        self.accept(); self.complete(); before = self.memory.snapshot()
        capture(self.memory, acquisition="synthetic-later-date", observed=LATER)
        state = self.memory.snapshot()
        self.assertEqual(assessment_item(before, self.cid, contract_version=FUNCTIONAL_CONTRACT),
                         assessment_item(state, self.cid, contract_version=FUNCTIONAL_CONTRACT))
        self.assertIsNotNone(current_assessment_fit(state, self.cid))
        changed = deepcopy(state)
        for oid in self.refs:
            changed["observations"][oid]["facts"].update(price="999", rank=1, image_url="https://example.org/image.jpg")
        self.assertEqual(assessment_item(changed, self.cid, contract_version=FUNCTIONAL_CONTRACT),
                         assessment_item(state, self.cid, contract_version=FUNCTIONAL_CONTRACT))
        self.assertIsNotNone(current_assessment_fit(changed, self.cid))

    def test_raw_established_brand_is_not_hidden_by_the_functional_definition(self):
        self.accept(); self.complete(); state = self.memory.snapshot()
        changed = deepcopy(state)
        changed["concepts"][self.cid]["description"]["brand"] = "established"
        self.assertIsNone(current_assessment_fit(changed, self.cid))

    def test_programme_recovers_saved_v3_before_preparing_new_functional_contract(self):
        from product_radar.fit_description import DESCRIPTION_CONTRACT
        from product_radar.programme import _fit
        from test_fit_description import DescriptionRunner
        row = prepare_practical_fit_reviews(self.memory, concept_ids=[self.cid], as_of=NOW,
                                             contract_version=DESCRIPTION_CONTRACT)[0]
        original = DescriptionRunner()
        with patch("product_radar.fit_review.apply_fit_review", side_effect=SafetyError("LOCAL_INTERRUPTION")):
            self.assertEqual(run_fit_reviews(self.memory, original, [row["review_id"]])[0]["status"], "needs_reconciliation")
        before = self.memory.snapshot(); original_jobs = deepcopy(before["jobs"])
        self.accept(); runner = FunctionalFitRunner()
        record = {"concept_ids": [self.cid], "deferrals": {}}
        result = _fit(self.memory, record, {"fit_limit": 5, "runner": runner, "at": NOW, "retry_seconds": 60})
        self.assertEqual(result["saved_v2_accepted_recovered"], 1)
        self.assertEqual(self.memory.snapshot()["assessments"][self.cid]["contract_version"], FUNCTIONAL_CONTRACT)
        self.assertEqual(len(original.calls), 1)
        self.assertEqual(len(runner.calls), 1)
        for job_id, job in original_jobs.items():
            self.assertEqual(self.memory.snapshot()["jobs"][job_id], job)

    def test_closed_spent_turn_continues_exhausted_v3_through_bound_definition_to_one_v4(self):
        import product_radar.programme as programme
        from product_radar.fit_description import DESCRIPTION_CONTRACT
        from product_radar.functional_definition_review import MODE, PROOF_KEY
        from product_radar.research_ai import Jobs
        from product_radar.storage import load_json
        from product_radar.work_turns import DEFAULT_POLICY, bind_work_turn, open_work_turn
        from test_fit_description import DescriptionRunner
        from test_functional_definition_review import Runner as DefinitionRunner
        from test_research_flow import FakeRunner

        class MalformedDescriptionRunner(DescriptionRunner):
            def __call__(inner, role, payload, folder):
                response, usage = super().__call__(role, payload, folder)
                marker = response["results"][0]["criteria"][0]["description_inference"]
                marker["description_hash"] = marker["description_hash"][:61]
                return response, usage

        class ContinuationRunner:
            identity = "synthetic-definition-fit-continuation"
            def __init__(inner):
                inner.calls, inner.definition, inner.fit = [], DefinitionRunner(), FunctionalFitRunner()
            def __call__(inner, role, payload, folder):
                inner.calls.append((role, deepcopy(payload)))
                if role == "B" and payload.get("mode") == MODE:
                    return inner.definition(role, payload, folder)
                if role == "E" and payload.get("judgment_style") == FUNCTIONAL_STYLE:
                    return inner.fit(role, payload, folder)
                raise AssertionError("Only the real definition and current fit questions should run")

        # Every record and job below belongs to this test's disposable memory.
        # Retire the unrelated fixture concept so inventory cannot widen scope.
        for cid, concept in self.memory.snapshot()["concepts"].items():
            if cid != self.cid:
                self.memory.put("concepts", cid, dict(concept, status="retired"), previous=concept)
        with patch("product_radar.research_ai.utc_now", return_value=NOW):
            row = prepare_practical_fit_reviews(self.memory, concept_ids=[self.cid], as_of=NOW,
                                                 contract_version=DESCRIPTION_CONTRACT)[0]
            old_runner = MalformedDescriptionRunner()
            failed = run_fit_reviews(self.memory, old_runner, [row["review_id"]])[0]
            self.assertEqual((failed["status"], failed["reason"]),
                             ("rejected", "FIT_DESCRIPTION_BINDING_INVALID"))
            self.assertEqual(len(old_runner.calls), 2)
            old_state = self.memory.snapshot()
            old_jobs = {jid: deepcopy(job) for jid, job in old_state["jobs"].items()
                        if job["request"]["prompt_version"] == DESCRIPTION_CONTRACT}
            self.assertEqual(len(old_jobs), 2)
            old_results = {self.memory.root / "ai_jobs" / jid / "result.json":
                           (self.memory.root / "ai_jobs" / jid / "result.json").read_bytes()
                           for jid in old_jobs}

            record = programme.prepare_programme(self.memory, "exhausted-v3-continuation",
                concept_ids=[self.cid], source_run_id="isolated-definition-fit-source", as_of=NOW)
            policy = deepcopy(DEFAULT_POLICY); policy.update(model_executions=1, report_slots=0)
            spent = open_work_turn(self.memory, record["programme_id"], at=NOW, policy=policy)
            with bind_work_turn(self.memory, spent["id"]):
                Jobs(self.memory, FakeRunner()).run("A", {"items": [
                    {"id": "spent-fixture-slot", "source_text": {"title": "Unclear offer"}}]})
            original_turn = deepcopy(self.memory.snapshot()["batch_runs"][spent["id"]])
            runner = ContinuationRunner()
            steps = {stage: (lambda *_args: {"state": "nothing_new"}) for stage in programme.STAGES}
            steps.update(fit=programme._fit, match=programme._match)
            options = dict(root=self.root, programme_id=record["programme_id"], handlers={},
                           runner=runner, steps=steps, as_of=NOW, turn_policy=policy)

            # Resume and normally close the actual spent original, without a
            # reset, another old correction, or an operator-accepted definition.
            first = programme.advance_ready_programme_turn(self.memory, **options)
            self.assertEqual(first["work_turn"]["id"], spent["id"])
            self.assertEqual(first["work_turn"]["state"], "closed")
            self.assertEqual(first["work_turn"]["counters"], original_turn["counters"])
            self.assertEqual(first["next_action"], "continue_ready_work")
            self.assertEqual(runner.calls, [])
            closed = programme.load_programme(self.memory, record["programme_id"])
            self.assertEqual(closed["last_results"]["match"]["functional_definitions"]
                             ["preparation_wait"]["concept_ids"], [self.cid])
            self.assertIsNone(functional_role_context(self.memory.snapshot(), self.cid))

            # The next ordinary round admits B and accepts its actual bound
            # example. Fit precedes matching in this round, so E runs later.
            second = programme.advance_ready_programme_turn(self.memory, **options)
            self.assertNotEqual(second["work_turn"]["id"], spent["id"])
            self.assertEqual(second["work_turn"]["counters"]["model_executions"], 1)
            self.assertEqual([role for role, _ in runner.calls], ["B"])
            state = self.memory.snapshot(); context = functional_role_context(state, self.cid)
            self.assertIsNotNone(context)
            definition = state["source_checks"][context["proof"]["definition_id"]]
            proof = definition["job_proof"][PROOF_KEY]
            self.assertEqual(proof["job_id"], definition["job_id"])
            self.assertEqual(state["jobs"][proof["job_id"]]["request"]["payload"]["mode"], MODE)
            self.assertEqual(set(proof["binding"]["children"]), set(self.children))
            self.assertTrue(context["examples"])
            for binding in proof["binding"]["children"].values():
                self.assertEqual(binding["observation_hash"], digest(state["observations"][binding["observation_id"]]))

            third = programme.advance_ready_programme_turn(self.memory, **options)
            self.assertEqual(third["work_turn"]["counters"]["model_executions"], 1)
            self.assertEqual([role for role, _ in runner.calls], ["B", "E"])
            state = self.memory.snapshot(); assessment = state["assessments"][self.cid]
            self.assertEqual(assessment["contract_version"], FUNCTIONAL_CONTRACT)
            self.assertEqual(current_assessment_fit(state, self.cid)["kpi_matches"], 10)
            new_jobs = {jid: job for jid, job in state["jobs"].items()
                        if job["request"]["prompt_version"] == FUNCTIONAL_CONTRACT}
            self.assertEqual(len(new_jobs), 1)
            new_id, new_job = next(iter(new_jobs.items()))
            self.assertNotIn(new_id, old_jobs)
            self.assertEqual(assessment["job_id"], new_id)
            self.assertEqual(new_job["request"]["payload"]["response_attribution"], FUNCTIONAL_RESPONSE_VERSION)
            raw = load_json(self.memory.root / "ai_jobs" / new_id / "result.json")["response"]["results"][0]
            self.assertTrue(all(criterion["description_inference"]["kind"] == FUNCTIONAL_SELECTION_KIND
                                for criterion in raw["criteria"]))
            self.assertTrue(all(set(criterion["description_inference"]) == {"kind", "fields"}
                                for criterion in raw["criteria"]))
            self.assertTrue(all(criterion["description_inference"]["meaning_hash"] == context["meaning_hash"]
                                for criterion in assessment["result"]["criteria"]))

            # A further normal round may settle local work; it cannot dispatch
            # either completed v3 answer or a second current assessment.
            programme.advance_ready_programme_turn(self.memory, **options)
            state = self.memory.snapshot()
            self.assertEqual([role for role, _ in runner.calls], ["B", "E"])
            self.assertEqual(len(old_runner.calls), 2)
            for jid, job in old_jobs.items():
                self.assertEqual(state["jobs"][jid], job)
            self.assertTrue(all(path.read_bytes() == raw for path, raw in old_results.items()))
            preserved = state["batch_runs"][spent["id"]]
            self.assertEqual(preserved["state"], "closed")
            for field in ("policy", "counters", "reservations"):
                self.assertEqual(preserved[field], original_turn[field])
            for table in ("concepts", "associations", "descriptions", "observations", "captures"):
                self.assertEqual(state[table], old_state[table], table)

    def test_programme_does_not_bypass_uncertain_old_v3_partial_question(self):
        from product_radar.fit_description import DESCRIPTION_CONTRACT
        from product_radar.programme import _fit
        from test_fit_description import DescriptionRunner
        row = prepare_practical_fit_reviews(self.memory, concept_ids=[self.cid], as_of=NOW,
                                             contract_version=DESCRIPTION_CONTRACT)[0]
        run_fit_reviews(self.memory, DescriptionRunner(), [row["review_id"]])
        partial = prepare_fit_review(self.memory, self.cid, requested_criterion_ids=[13],
            reason="Synthetic partial audience review", as_of=NOW, contract_version=DESCRIPTION_CONTRACT)
        class Uncertain(DescriptionRunner):
            def __call__(self, *_args):
                raise SafetyError("AI_NETWORK_UNAVAILABLE")
        self.assertEqual(run_fit_reviews(self.memory, Uncertain(), [partial["reference"]])[0]["status"], "pending")
        self.accept(); before = self.memory.snapshot(); runner = FunctionalFitRunner()
        record = {"concept_ids": [self.cid], "deferrals": {}}
        result = _fit(self.memory, record, {"fit_limit": 5, "runner": runner, "at": NOW, "retry_seconds": 60})
        self.assertEqual(result["prepared"], 0)
        self.assertEqual(runner.calls, [])
        self.assertEqual(self.memory.snapshot()["assessments"], before["assessments"])
        self.assertEqual(self.memory.snapshot()["jobs"], before["jobs"])
        self.assertEqual(record["deferrals"]["fit:" + self.cid]["reason"], "AI_JOB_PENDING_RECONCILIATION")

    def test_narrowed_type_cannot_inherit_old_positive_market_votes(self):
        from product_radar.market_validation import REVIEW_VERSION
        before = self.memory.snapshot()
        peer = next(cid for cid in before["concepts"] if cid != self.cid)
        legacy_peer = next(row for row in candidate_summary(self.memory, as_of=NOW)["products"] if row["id"] == peer)
        raw_description = before["concepts"][self.cid]["description"]
        review = {"concept_id": self.cid, "review_version": REVIEW_VERSION,
            "description_hash": digest(raw_description), "reviewed_at": NOW, "input_hash": digest("synthetic old market"),
            "relevance": [{"id": ref, "relationship": "exact_product", "reason": "Old raw-title product match."} for ref in self.refs]}
        self.memory.put("market_reviews", "synthetic-old-positive-review", review)
        self.accept(dict(DEFINITION, mechanism="Wall mounted draining tray"))
        self.complete()
        summary = candidate_summary(self.memory, as_of=NOW)["products"]
        current = next(row for row in summary if row["id"] == self.cid)
        self.assertEqual(current["checklist_meets"], 10)
        self.assertEqual(current["market_scope_status"], "needs_review")
        self.assertFalse(current["opportunity"]["eligible"])
        self.assertTrue(all(not value["available"] for value in current["market"]["families"].values()))
        self.assertGreater(current["observations"], 0)
        self.assertEqual(next(row for row in summary if row["id"] == peer), legacy_peer)


if __name__ == "__main__":
    unittest.main()
