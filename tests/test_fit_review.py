"""Manufactured saved-input migration/replay checks, not benefit evidence."""

from copy import deepcopy
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from test_research_flow import FakeRunner, NOW, capture
from product_radar.fit_policy import FIT_POLICY_ID, evaluate_fit
from product_radar.fit_review import apply_fit_review, prepare_fit_review, project_saved_fit, run_fit_reviews
from product_radar.models import SafetyError, digest
from product_radar.research_flow import assessment_item, candidate_summary, current_assessment_fit, run_flow
from product_radar.research_memory import ResearchMemory, change, key
from product_radar.research_operations import missing_market_checks
from product_radar.service import policy_for_fit
from product_radar.storage import Journal


class FitRunner(FakeRunner):
    def __call__(self, role, payload, folder):
        if role != "E" or payload.get("fit_policy_id") != FIT_POLICY_ID:
            return super().__call__(role, payload, folder)
        self.calls.append((role, deepcopy(payload)))
        values = []
        for item in payload["items"]:
            refs = [row["id"] for row in item["observations"]]
            values.append({"id": item["id"], "criteria": [
                {"id": number, "state": "Meets" if number in (1, 2, 5, 6, 7, 8, 13) else "Unclear",
                 "basis": "inferred" if number in (1, 2, 5, 6, 7, 8, 13) else "not_stated",
                 "references": refs if number in (1, 2, 5, 6, 7, 8, 13) else [],
                 "reason": "Fewer transfers using the described draining feature." if number == 7
                           else "Moderate household buyer positioning from supplied use." if number == 13
                           else "Synthetic supplied product-use judgment."}
                for number in payload["requested_criterion_ids"]],
                "relevance": [{"id": ref, "relationship": "exact_product", "reason": "Synthetic product scope."} for ref in refs],
                "compliance_note": "Client review where relevant.", "missing_questions": [],
                "audience_positioning": {"positioning": "women_centric" if 13 in payload["requested_criterion_ids"] else "unclear",
                                         "reason": "Synthetic household buyer context."}})
        return {"results": values}, {"input_tokens": 1, "output_tokens": 1, "cached_input_tokens": 0}


class FitReviewTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.memory = ResearchMemory(Path(self.temp.name) / "research", Journal(Path(self.temp.name) / "money"), evidence_type="synthetic")
        self.legacy = FakeRunner()
        capture(self.memory)
        run_flow(self.memory, self.legacy, as_of=NOW)
        self.concept_id = next(iter(self.memory.snapshot()["concepts"]))

    def projected(self, concept_id=None):
        return project_saved_fit(self.memory, concept_id or self.concept_id, as_of=NOW)

    def test_projection_uses_original_ids_and_retains_historical_answer(self):
        before = self.memory.snapshot()
        original_events = {path: path.read_bytes() for path in (self.memory.root / "events").glob("*.json")}
        projected = self.projected()
        after = self.memory.snapshot()
        result = after["assessments"][self.concept_id]
        self.assertTrue(projected["applied"])
        self.assertEqual(result["fit_policy_id"], FIT_POLICY_ID)
        self.assertEqual([row["id"] for row in result["result"]["criteria"]], [1, 2, 5, 6, 7, 8, 9, 11, 12, 13])
        self.assertEqual({row["id"]: row["state"] for row in result["result"]["criteria"] if row["id"] in (7, 13)}, {7: "Unclear", 13: "Unclear"})
        self.assertEqual(before["descriptions"], after["descriptions"])
        self.assertEqual(before["observations"], after["observations"])
        self.assertEqual(before["jobs"], after["jobs"])
        self.assertTrue(all(path.read_bytes() == raw for path, raw in original_events.items()))
        count = len(list((self.memory.root / "events").glob("*.json")))
        replay = self.projected()
        self.assertFalse(replay["applied"])
        self.assertEqual(replay["review_id"], projected["review_id"])
        self.assertEqual(count, len(list((self.memory.root / "events").glob("*.json"))))

    def test_projection_and_pending_review_survive_the_same_interruption(self):
        original_commit = self.memory.commit
        def interrupted_after_commit(event_id, changes, *, validate=None):
            result = original_commit(event_id, changes, validate=validate)
            if event_id.startswith("fit-projection-"):
                raise SafetyError("LOCAL_INTERRUPTION")
            return result
        with patch.object(self.memory, "commit", side_effect=interrupted_after_commit), self.assertRaisesRegex(SafetyError, "LOCAL_INTERRUPTION"):
            self.projected()
        state = self.memory.snapshot()
        parent = state["assessments"][self.concept_id]
        self.assertEqual(parent["fit_policy_id"], FIT_POLICY_ID)
        receipt = state["source_checks"][parent["projection"]["id"]]
        self.assertIn(receipt["review_id"], state["source_checks"])
        replay = self.projected()
        self.assertEqual(replay["review_id"], receipt["review_id"])
        runner = FitRunner()
        self.assertTrue(run_fit_reviews(self.memory, runner, [replay["review_id"]])[0]["applied"])
        self.assertEqual(len(runner.calls), 1)

    def test_pre_atomic_projection_gap_prepares_only_its_missing_review_base(self):
        original_commit = self.memory.commit
        def pre_atomic_commit(event_id, changes, *, validate=None):
            if event_id.startswith("fit-projection-"):
                changes = deepcopy([row for row in changes if row["value"].get("kind") != "fit_review_base"])
                for row in changes:
                    if row["value"].get("kind") == "fit_projection_applied":
                        row["value"].pop("review_id")
            return original_commit(event_id, changes, validate=validate)
        with patch.object(self.memory, "commit", side_effect=pre_atomic_commit):
            projected = self.projected()
        before = self.memory.snapshot()
        self.assertNotIn(projected["review_id"], before["source_checks"])
        recovered = self.projected()
        self.assertEqual(recovered["status"], "review_base_recovered")
        self.assertEqual(before["assessments"], self.memory.snapshot()["assessments"])
        self.assertEqual(before["jobs"], self.memory.snapshot()["jobs"])
        self.assertIn(recovered["review_id"], self.memory.snapshot()["source_checks"])
        state = self.memory.snapshot()
        self.assertFalse(self.projected()["applied"])
        self.assertEqual(state, self.memory.snapshot())

    def test_unsupported_legacy_negative_becomes_unknown_not_a_point(self):
        old = self.memory.snapshot()["assessments"][self.concept_id]
        changed = deepcopy(old)
        row = next(row for row in changed["result"]["criteria"] if row["id"] == 9)
        row.update(state="Does not meet", basis="not_stated", references=[], reason="No claim was stated.")
        self.memory.put("assessments", self.concept_id, changed, previous=old)
        projected = self.projected()
        current = self.memory.snapshot()["assessments"][self.concept_id]
        row = next(row for row in current["result"]["criteria"] if row["id"] == 9)
        self.assertEqual(row["state"], "Unclear")
        self.assertEqual(projected["fit"]["kpi_matches"], 5)
        self.assertEqual(projected["fit"]["fit_tier"], "decent")
        self.assertTrue(current["diagnostics"])
        product = next(row for row in candidate_summary(self.memory, as_of=NOW)["products"] if row["id"] == self.concept_id)
        self.assertEqual(product["checklist_total"], 10)
        self.assertIn(self.concept_id, [row["concept_id"] for row in missing_market_checks({"products": [product]}, as_of=NOW)])

    def test_named_review_combines_only_frozen_retained_rows_once(self):
        projected = self.projected()
        frozen = self.memory.snapshot()["source_checks"][projected["review_id"]]["binding"]
        runner = FitRunner()
        result = run_fit_reviews(self.memory, runner, [projected["review_id"]])
        self.assertTrue(result[0]["applied"])
        record = self.memory.snapshot()["assessments"][self.concept_id]
        self.assertEqual(record["fit_review"]["retained_rows_hash"], frozen["retained_rows_hash"])
        retained = [row for row in record["result"]["criteria"] if row["id"] not in (7, 13)]
        self.assertEqual(retained, frozen["retained_rows"])
        self.assertIn("transfers", next(row for row in record["result"]["criteria"] if row["id"] == 7)["reason"])
        self.assertEqual(record["result"]["audience_positioning"]["positioning"], "women_centric")
        before, calls = self.memory.snapshot(), len(runner.calls)
        replay = run_fit_reviews(self.memory, None, [projected["review_id"]])
        self.assertFalse(replay[0]["applied"])
        self.assertEqual(before, self.memory.snapshot())
        self.assertEqual(calls, len(runner.calls))

    def test_partial_fit_review_retains_independent_parent_market_and_compliance(self):
        projected = self.projected()
        parent = deepcopy(self.memory.snapshot()["assessments"][self.concept_id]["result"])
        actual = FitRunner()
        def unrelated_side_answer(role, payload, folder):
            response, usage = actual(role, payload, folder)
            for answer in response["results"]:
                answer["relevance"] = [dict(row, relationship="unrelated") for row in answer["relevance"]]
                answer["compliance_note"] = "A different nonrequested review note."
                answer["missing_questions"] = ["A different nonrequested question."]
            return response, usage
        result = run_fit_reviews(self.memory, unrelated_side_answer, [projected["review_id"]])
        self.assertTrue(result[0]["applied"])
        current = self.memory.snapshot()["assessments"][self.concept_id]["result"]
        for field in ("relevance", "compliance_note", "missing_questions"):
            self.assertEqual(parent[field], current[field])
        self.assertNotEqual(parent["audience_positioning"], current["audience_positioning"])

    def test_replay_after_later_assessment_does_not_reinstate_old_result(self):
        base = self.projected()["review_id"]
        runner = FitRunner()
        run_fit_reviews(self.memory, runner, [base])
        current = self.memory.snapshot()["assessments"][self.concept_id]
        later = dict(current, later_named_review="retained newer result")
        self.memory.put("assessments", self.concept_id, later, previous=current)
        before = self.memory.snapshot()
        self.assertFalse(run_fit_reviews(self.memory, runner, [base])[0]["applied"])
        self.assertEqual(before, self.memory.snapshot())
        self.assertEqual(len(runner.calls), 1)

    def test_stale_base_stays_saved_and_does_not_launch(self):
        base = self.projected()["review_id"]
        old = self.memory.snapshot()["assessments"][self.concept_id]
        changed = deepcopy(old); changed["result"]["criteria"][0]["reason"] = "A later accepted reason."
        self.memory.put("assessments", self.concept_id, changed, previous=old)
        runner = FitRunner()
        result = run_fit_reviews(self.memory, runner, [base])
        self.assertEqual(result[0]["status"], "needs_reconciliation")
        self.assertEqual(result[0]["reason"], "FIT_REVIEW_BASE_CHANGED")
        self.assertEqual(runner.calls, [])
        self.assertEqual(changed, self.memory.snapshot()["assessments"][self.concept_id])

    def test_base_changed_during_model_does_not_overwrite_and_sibling_commits(self):
        concepts = list(self.memory.snapshot()["concepts"])
        bases = [self.projected(concept_id)["review_id"] for concept_id in concepts]
        actual = FitRunner()
        def change_during_model(role, payload, folder):
            response, usage = actual(role, payload, folder)
            old = self.memory.snapshot()["assessments"][concepts[0]]
            changed = deepcopy(old); changed["result"]["criteria"][0]["reason"] = "An independent later result."
            self.memory.put("assessments", concepts[0], changed, previous=old)
            return response, usage
        result = run_fit_reviews(self.memory, change_during_model, bases)
        self.assertEqual([row["status"] for row in result], ["needs_reconciliation", "accepted"])
        self.assertEqual(len(actual.calls), 1)
        old = self.memory.snapshot()["assessments"][concepts[0]]
        replay = run_fit_reviews(self.memory, change_during_model, bases)
        self.assertEqual({row["status"] for row in replay}, {"accepted", "needs_reconciliation"})
        self.assertEqual(old, self.memory.snapshot()["assessments"][concepts[0]])
        self.assertEqual(len(actual.calls), 1)

    def test_saved_answer_after_interrupted_application_resumes_without_model(self):
        base = self.projected()["review_id"]
        runner = FitRunner()
        with patch("product_radar.fit_review.apply_fit_review", side_effect=SafetyError("LOCAL_INTERRUPTION")):
            self.assertEqual(run_fit_reviews(self.memory, runner, [base])[0]["status"], "needs_reconciliation")
        count = len(runner.calls)
        self.assertTrue(run_fit_reviews(self.memory, runner, [base])[0]["applied"])
        self.assertEqual(len(runner.calls), count)

    def test_saved_answer_reuses_original_runner_with_no_or_different_runner(self):
        concepts = list(self.memory.snapshot()["concepts"])
        bases = [self.projected(concept_id)["review_id"] for concept_id in concepts]
        runner = FitRunner(); runner.identity = "saved-review-runner"
        with patch("product_radar.fit_review.apply_fit_review", side_effect=SafetyError("LOCAL_INTERRUPTION")):
            run_fit_reviews(self.memory, runner, bases)
        saved_jobs = set(self.memory.snapshot()["jobs"])
        self.assertTrue(run_fit_reviews(self.memory, None, [bases[1]])[0]["applied"])
        other_runner = FitRunner(); other_runner.identity = "different-runner"
        self.assertTrue(run_fit_reviews(self.memory, other_runner, [bases[0]])[0]["applied"])
        state = self.memory.snapshot()
        self.assertEqual(saved_jobs, set(state["jobs"]))
        self.assertEqual(len(runner.calls), 1)
        self.assertEqual(other_runner.calls, [])
        self.assertTrue(all(row["job_id"] in state["jobs"] for row in state["assessments"].values()))

    def test_no_runner_does_not_claim_or_block_a_new_review_job(self):
        base = self.projected()["review_id"]
        before_jobs = self.memory.snapshot()["jobs"]
        self.assertEqual(run_fit_reviews(self.memory, None, [base])[0]["status"], "pending")
        self.assertEqual(before_jobs, self.memory.snapshot()["jobs"])
        runner = FitRunner()
        self.assertTrue(run_fit_reviews(self.memory, runner, [base])[0]["applied"])
        self.assertEqual(len(runner.calls), 1)

    def test_ordinary_current_flow_without_runner_does_not_claim_new_full_job(self):
        memory = ResearchMemory(Path(self.temp.name) / "not-enabled", self.memory.journal, evidence_type="synthetic")
        seed = self.memory.snapshot()
        memory.commit("synthetic-unassessed-sources", [change(table, record_id, row)
                      for table in ("captures", "observations", "descriptions", "concepts", "associations")
                      for record_id, row in seed[table].items()])
        run_flow(memory, None, fit_policy_id=FIT_POLICY_ID, as_of=NOW)
        self.assertEqual(memory.snapshot()["jobs"], {})
        self.assertEqual(memory.snapshot()["assessments"], {})
        runner = FitRunner(); runner.identity = "enabled-owner"
        run_flow(memory, runner, fit_policy_id=FIT_POLICY_ID, as_of=NOW)
        self.assertEqual(len(memory.snapshot()["assessments"]), len(seed["concepts"]))
        self.assertEqual(len(runner.calls), 1)

    def test_ordinary_current_flow_replays_saved_named_answer_without_runner(self):
        base = self.projected()["review_id"]
        runner = FitRunner(); runner.identity = "original-named-owner"
        with patch("product_radar.fit_review.apply_fit_review", side_effect=SafetyError("LOCAL_INTERRUPTION")):
            run_fit_reviews(self.memory, runner, [base])
        saved_jobs = self.memory.snapshot()["jobs"]
        run_flow(self.memory, None, fit_policy_id=FIT_POLICY_ID, as_of=NOW)
        state = self.memory.snapshot()
        self.assertIn(key("fit-review-apply", base), state["source_checks"])
        self.assertEqual(saved_jobs, state["jobs"])
        self.assertEqual(len(runner.calls), 1)

    def test_changed_saved_review_payload_is_not_a_second_variant(self):
        base = self.projected()["review_id"]
        runner = FitRunner()
        with patch("product_radar.fit_review.apply_fit_review", side_effect=SafetyError("LOCAL_INTERRUPTION")):
            run_fit_reviews(self.memory, runner, [base])
        state = self.memory.snapshot()
        job_id, job = next((i, row) for i, row in state["jobs"].items()
                           if any(item.get("fit_review_base_id") == base for item in row["request"]["payload"]["items"]))
        modified = deepcopy(job)
        modified["request"]["payload"]["checklist_rules"] += " Invented later rule."
        self.memory.put("jobs", job_id, modified, previous=job)
        result = run_fit_reviews(self.memory, runner, [base])
        self.assertEqual(result[0]["reason"], "FIT_REVIEW_JOB_CONFLICT")
        self.assertEqual(len(runner.calls), 1)

    def test_rejected_item_does_not_drop_accepted_sibling_and_replay_is_finite(self):
        concepts = list(self.memory.snapshot()["concepts"])
        bases = [self.projected(concept_id)["review_id"] for concept_id in concepts]
        actual = FitRunner()
        def bad_one(role, payload, folder):
            response, usage = actual(role, payload, folder)
            response["results"][0]["criteria"][0]["references"] = ["invented-reference"]
            return response, usage
        result = run_fit_reviews(self.memory, bad_one, bases)
        self.assertEqual([row["status"] for row in result], ["rejected", "accepted"])
        count = len(actual.calls)
        again = run_fit_reviews(self.memory, bad_one, bases)
        self.assertIn("rejected", [row["status"] for row in again])
        self.assertEqual(len(actual.calls), count)

    def test_explicit_current_flow_uses_ten_and_legacy_model_jobs_do_not_block_it(self):
        runner = FitRunner()
        summary = run_flow(self.memory, runner, fit_policy_id=FIT_POLICY_ID, as_of=NOW)
        self.assertTrue(all(row["checklist_total"] == 10 for row in summary["products"]))
        self.assertTrue(all(row["fit_policy_id"] == FIT_POLICY_ID for row in summary["products"]))
        self.assertEqual(len(runner.calls), 1)
        count = len(runner.calls)
        run_flow(self.memory, runner, fit_policy_id=FIT_POLICY_ID, as_of=NOW)
        self.assertEqual(len(runner.calls), count)

    def test_full_new_assessment_has_no_retired_rows(self):
        memory = ResearchMemory(Path(self.temp.name) / "new", self.memory.journal, evidence_type="synthetic")
        capture(memory)
        summary = run_flow(memory, FitRunner(), fit_policy_id=FIT_POLICY_ID, as_of=NOW)
        self.assertTrue(all(row["fit"]["eligible"] for row in summary["products"]))
        self.assertTrue(all(len(row["criteria"]) == 10 for row in summary["products"]))
        self.assertTrue(all(row["fit"]["does_not_meet"] == 0 for row in summary["products"]))

    def test_full_saved_batch_narrower_resume_keeps_actual_source_job_and_runner(self):
        memory = ResearchMemory(Path(self.temp.name) / "new", self.memory.journal, evidence_type="synthetic")
        capture(memory)
        runner = FitRunner()
        original_commit = memory.commit
        completed = 0
        def interrupt_second_application(event_id, changes, *, validate=None):
            nonlocal completed
            if any(row["table"] == "assessments" for row in changes):
                completed += 1
                if completed == 2:
                    raise SafetyError("LOCAL_INTERRUPTION")
            return original_commit(event_id, changes, validate=validate)
        with patch.object(memory, "commit", side_effect=interrupt_second_application):
            run_flow(memory, runner, fit_policy_id=FIT_POLICY_ID, as_of=NOW)
        self.assertEqual(len(memory.snapshot()["assessments"]), 1)
        before_jobs = memory.snapshot()["jobs"]
        other_runner = FitRunner(); other_runner.identity = "different-runner"
        run_flow(memory, other_runner, limit=1, fit_policy_id=FIT_POLICY_ID, as_of=NOW)
        state = memory.snapshot()
        self.assertEqual(len(state["assessments"]), 2)
        self.assertEqual(before_jobs, state["jobs"])
        self.assertTrue(all(record["job_id"] in state["jobs"] for record in state["assessments"].values()))
        self.assertEqual(other_runner.calls, [])
        self.assertEqual(sum(role == "E" for role, _ in runner.calls), 1)

    def test_pending_original_owner_does_not_consume_healthy_full_assessment_slot(self):
        from product_radar.research_ai import CRITERIA, Jobs
        from product_radar.research_flow import FIT_CHECKLIST_RULES, FIT_CHECKLIST_SCOPE
        memory = ResearchMemory(Path(self.temp.name) / "pending", self.memory.journal, evidence_type="synthetic")
        seed = self.memory.snapshot()
        memory.commit("synthetic-unassessed-sources", [change(table, record_id, row)
                      for table in ("captures", "observations", "descriptions", "concepts", "associations")
                      for record_id, row in seed[table].items()])
        concepts = list(memory.snapshot()["concepts"])
        context = {"mode": "fit_assessment", "fit_policy_id": FIT_POLICY_ID,
                   "requested_criterion_ids": [1, 2, 5, 6, 7, 8, 9, 11, 12, 13],
                   "criteria": [{"id": n, "name": CRITERIA[n - 1]} for n in (1, 2, 5, 6, 7, 8, 9, 11, 12, 13)],
                   "checklist_scope": FIT_CHECKLIST_SCOPE, "checklist_rules": FIT_CHECKLIST_RULES}
        def interrupted(role, payload, folder):
            raise SafetyError("AI_NETWORK_UNAVAILABLE")
        interrupted.identity = "original-owner"
        item = dict(assessment_item(memory.snapshot(), concepts[0]), earlier_judgments=[])
        with self.assertRaisesRegex(SafetyError, "AI_NETWORK_UNAVAILABLE"):
            Jobs(memory, interrupted).run("E", dict(context, items=[item]))
        runner = FitRunner(); runner.identity = "different-owner"
        run_flow(memory, runner, limit=1, fit_policy_id=FIT_POLICY_ID, as_of=NOW)
        state = memory.snapshot()
        self.assertNotIn(concepts[0], state["assessments"])
        self.assertIn(concepts[1], state["assessments"])
        self.assertEqual(len(runner.calls), 1)
        self.assertEqual(runner.calls[0][1]["items"][0]["id"], concepts[1])

    def test_explicit_prepared_full_question_is_not_bypassed_by_ordinary_flow(self):
        memory = ResearchMemory(Path(self.temp.name) / "prepared", self.memory.journal, evidence_type="synthetic")
        seed = self.memory.snapshot()
        memory.commit("synthetic-unassessed-sources", [change(table, record_id, row)
                      for table in ("captures", "observations", "descriptions", "concepts", "associations")
                      for record_id, row in seed[table].items()])
        base = project_saved_fit(memory, self.concept_id, as_of=NOW)
        runner = FitRunner()
        run_flow(memory, runner, fit_policy_id=FIT_POLICY_ID, as_of=NOW)
        parent = memory.snapshot()["assessments"][self.concept_id]
        self.assertEqual(parent["fit_review"]["review_id"], base["review_id"])
        self.assertTrue(all(row["job_id"] in memory.snapshot()["jobs"] for row in memory.snapshot()["assessments"].values()))

    def test_wrong_current_contract_marker_is_pending_not_a_current_score(self):
        self.projected()
        original = self.memory.snapshot()["assessments"][self.concept_id]
        for marker in (None, "research-abe-v1"):
            modified = dict(original, contract_version=marker)
            previous = self.memory.snapshot()["assessments"][self.concept_id]
            self.memory.put("assessments", self.concept_id, modified, previous=previous)
            self.assertIsNone(current_assessment_fit(self.memory.snapshot(), self.concept_id))
            product = next(row for row in candidate_summary(self.memory, as_of=NOW)["products"] if row["id"] == self.concept_id)
            self.assertIsNone(product["checklist_meets"])
            self.assertEqual(product["status"], "assessment_pending")

    def test_current_summary_does_not_count_a_source_reference_outside_its_facts(self):
        self.projected()
        original = self.memory.snapshot()["assessments"][self.concept_id]
        modified = deepcopy(original)
        supported = next(row for row in modified["result"]["criteria"] if row["state"] == "Meets")
        supported["references"] = ["unrelated-observation"]
        self.memory.put("assessments", self.concept_id, modified, previous=original)
        self.assertIsNone(current_assessment_fit(self.memory.snapshot(), self.concept_id))
        product = next(row for row in candidate_summary(self.memory, as_of=NOW)["products"] if row["id"] == self.concept_id)
        self.assertIsNone(product["checklist_meets"])
        self.assertEqual(product["status"], "assessment_pending")

    def test_explicit_project_action_prepares_full_review_for_wrong_current_marker(self):
        self.projected()
        original = self.memory.snapshot()["assessments"][self.concept_id]
        modified = dict(original, contract_version="research-abe-v1")
        self.memory.put("assessments", self.concept_id, modified, previous=original)
        outcome = self.projected()
        self.assertEqual(outcome["status"], "assessment_pending")
        self.assertFalse(outcome["applied"])
        state = self.memory.snapshot()
        self.assertEqual(state["assessments"][self.concept_id], modified)
        base = state["source_checks"][outcome["review_id"]]
        self.assertEqual(base["binding"]["requested_criterion_ids"], [1, 2, 5, 6, 7, 8, 9, 11, 12, 13])
        self.assertEqual(self.projected()["review_id"], outcome["review_id"])
        runner = FitRunner()
        run_flow(self.memory, runner, fit_policy_id=FIT_POLICY_ID, as_of=NOW)
        self.assertEqual(self.memory.snapshot()["assessments"][self.concept_id]["fit_review"]["review_id"], outcome["review_id"])

    def test_summary_of_retired_concepts_keeps_unresolved_fit_problem(self):
        from product_radar.research_flow import problem
        problem(self.memory, "fit_contract", self.concept_id, "FIT_CURRENT_CONTRACT_REVIEW_REQUIRED")
        state = self.memory.snapshot()
        self.memory.commit("synthetic-retire-all", [change("concepts", concept_id, dict(concept, status="retired"), concept)
                                                   for concept_id, concept in state["concepts"].items()])
        summary = candidate_summary(self.memory, as_of=NOW)
        self.assertEqual(summary["products"], [])
        self.assertTrue(any(row["stage"] == "fit_contract" and row["current_status"] == "unresolved" for row in summary["problems"]))

    def test_no_prior_review_is_full_and_stale_facts_do_not_project(self):
        projected = self.projected()
        old = self.memory.snapshot()["concepts"][self.concept_id]
        changed = dict(old, description=dict(old["description"], function="Changed functional context"))
        self.memory.put("concepts", self.concept_id, changed, previous=old)
        before = self.memory.snapshot()
        self.assertEqual(self.projected()["status"], "saved_facts_changed")
        self.assertEqual(before, self.memory.snapshot())
        with self.assertRaisesRegex(SafetyError, "PARENT_FACTS_CHANGED"):
            prepare_fit_review(self.memory, self.concept_id, requested_criterion_ids=[7, 13], reason="specific review", as_of=NOW)

    def test_unknown_criteria_do_not_get_invented_negatives(self):
        self.projected()
        record = self.memory.snapshot()["assessments"][self.concept_id]
        self.assertEqual(evaluate_fit(record["result"]["criteria"], FIT_POLICY_ID)["does_not_meet"], 0)
        self.assertFalse(self.memory.journal.path.exists())

    def test_current_production_market_review_admits_decent_five_without_legacy_recount(self):
        from product_radar.market_validation import review_marketplace
        from test_market_validation import RelevanceRunner, amazon
        concept_id = next(i for i, concept in self.memory.snapshot()["concepts"].items()
                          if concept["description"]["label"] == "Sink caddy")
        old = self.memory.snapshot()["assessments"][concept_id]
        changed = deepcopy(old)
        next(row for row in changed["result"]["criteria"] if row["id"] == 9).update(
            state="Unclear", basis="not_stated", references=[], reason="Synthetic missing construction context.")
        self.memory.put("assessments", concept_id, changed, previous=old)
        projected = self.projected(concept_id)
        self.assertEqual(projected["fit"]["fit_tier"], "decent")
        amazon(self.memory, titles=["Sink sponge organiser caddy"], at=NOW)
        selected = policy_for_fit(FIT_POLICY_ID)
        with patch("product_radar.service.load_policy", return_value=selected):
            result = review_marketplace(self.memory, RelevanceRunner(), as_of=NOW)
        self.assertIn(concept_id, result["accepted"])
        self.assertEqual(len(result["accepted"]), 1)
        self.assertEqual(self.memory.snapshot()["assessments"][concept_id]["fit_policy_id"], FIT_POLICY_ID)

    def test_current_trends_question_binds_fit_and_effective_brand_before_collection(self):
        from product_radar.market_followups import execute_trends_check, prepare_trends_check
        from product_radar.brand_reviews import apply_brand_review, prepare_brand_review
        concept_id = self.concept_id
        self.projected(concept_id)
        selected = policy_for_fit(FIT_POLICY_ID)
        with patch("product_radar.service.load_policy", return_value=selected):
            request = prepare_trends_check(self.memory, concept_id=concept_id, term="Synthetic household tool", as_of=NOW)
        self.assertEqual(request["schema_version"], 2)
        self.assertEqual(request["fit_policy_id"], FIT_POLICY_ID)
        state = self.memory.snapshot()
        semantics = {a["semantic_id"] for a in state["associations"].values() if a.get("concept_id") == concept_id}
        refs = [i for i, observation in state["observations"].items() if observation["semantic_id"] in semantics]
        review = prepare_brand_review(state, "concept", concept_id, verdict="unclear",
            reason="Synthetic named manufacturer context review.", evidence_ids=refs, reviewer="fixture_owner", reviewed_at=NOW)
        apply_brand_review(self.memory, review)
        calls = []
        def must_not_collect(*args, **kwargs):
            calls.append(True)
            raise AssertionError("A changed brand-bound question cannot collect.")
        with self.assertRaisesRegex(SafetyError, "TRENDS_PRODUCT_BRAND_CHANGED"):
            execute_trends_check(self.memory, request, root=Path(self.temp.name), capture=must_not_collect)
        self.assertEqual(calls, [])


if __name__ == "__main__":
    unittest.main()
