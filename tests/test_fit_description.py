"""Opt-in description estimates; fictional facts and disposable memory only."""

from copy import deepcopy
import unittest
from unittest.mock import patch

import test_fit_review
from test_fit_review import FitRunner
from product_radar.fit_description import (DESCRIPTION_CONTRACT, DESCRIPTION_INFERENCE_KIND,
    DESCRIPTION_STYLE, description_inference_context, validate_description_inference)
from product_radar.fit_policy import ACTIVE_CRITERION_IDS, FIT_POLICY_ID, LEGACY_POLICY_ID, evaluate_fit
from product_radar.fit_review import (_payload, PRACTICAL_CONTRACT, prepare_fit_review,
    prepare_practical_fit_reviews, run_fit_reviews, saved_description_review_attribution,
    prepare_description_fit_from_saved_repair)
from product_radar.models import SafetyError, digest
from product_radar.research_ai import e_contract, validate_results
from product_radar.research_memory import key
from product_radar.storage import load_json


def response_for(payload, *, all_description=True):
    response, _ = FitRunner()("E", payload, None)
    for value, item in zip(response["results"], payload["items"]):
        description = item["description"]
        fields = description_inference_context(description)["allowed_fields"]
        chosen = ["function"] if "function" in fields else [fields[0]]
        for row in value["criteria"]:
            row["description_inference"] = None
            if all_description:
                row.update(state="Meets", basis="inferred", references=[],
                    reason="Estimated: The described function supports this ordinary practical judgment.",
                    description_inference={"kind": DESCRIPTION_INFERENCE_KIND,
                        "description_hash": digest(description), "fields": chosen})
        if 13 in payload["requested_criterion_ids"]:
            value["audience_positioning"]["positioning"] = "women_centric"
    return response


class DescriptionRunner(FitRunner):
    def __call__(self, role, payload, folder):
        self.calls.append((role, deepcopy(payload)))
        return response_for(payload), {"input_tokens": 1, "output_tokens": 1, "cached_input_tokens": 0}


class DescriptionFitTests(unittest.TestCase):
    setUp = test_fit_review.FitReviewTests.setUp

    def test_mixed_saved_batch_retains_real_admission_wait_for_unsent_sibling(self):
        from product_radar.work_turns import bind_work_turn, open_work_turn
        from test_research_flow import NOW
        ids = list(self.memory.snapshot()["concepts"])[:2]
        self.assertEqual(len(ids), 2)
        rows = prepare_practical_fit_reviews(self.memory, concept_ids=ids, as_of=NOW,
                                             contract_version=DESCRIPTION_CONTRACT)
        runner = DescriptionRunner()
        with patch("product_radar.fit_review.apply_fit_review", side_effect=SafetyError("SAVED_FILE_UNAVAILABLE")):
            first = run_fit_reviews(self.memory, runner, [rows[0]["review_id"]])
        self.assertEqual(first[0]["status"], "needs_reconciliation")
        jobs = deepcopy(self.memory.snapshot()["jobs"])
        originals = {path: path.read_bytes() for path in (self.memory.root / "ai_jobs").rglob("result.json")}
        turn = open_work_turn(self.memory, "synthetic-fit-admission", at=NOW)
        with bind_work_turn(self.memory, turn["id"]), patch("product_radar.work_turns.admit_model_job",
                side_effect=SafetyError("WORK_TURN_MODEL_LIMIT")):
            result = run_fit_reviews(self.memory, runner, [row["review_id"] for row in rows])
        by_review = {row["review_id"]: row for row in result}
        self.assertEqual(by_review[rows[0]["review_id"]]["status"], "accepted")
        remaining = by_review[rows[1]["review_id"]]
        self.assertEqual(remaining["status"], "pending")
        self.assertEqual(remaining["reason"], "WORK_TURN_MODEL_LIMIT")
        self.assertNotIn("job_id", remaining)
        self.assertEqual(len(runner.calls), 1)
        self.assertEqual(self.memory.snapshot()["jobs"], jobs)
        self.assertTrue(all(path.read_bytes() == raw for path, raw in originals.items()))

    def base(self, *, contract=DESCRIPTION_CONTRACT):
        return prepare_fit_review(self.memory, self.concept_id,
            requested_criterion_ids=list(ACTIVE_CRITERION_IDS), reason="synthetic-description-contract",
            contract_version=contract)

    def checked(self, payload, response):
        return validate_results("E", payload, response, declared_version=DESCRIPTION_CONTRACT)

    def test_v3_is_explicit_and_v2_schema_replay_remains_strict(self):
        payload = _payload(self.base())
        self.assertEqual(e_contract(payload)["version"], DESCRIPTION_CONTRACT)
        self.assertEqual(payload["judgment_style"], DESCRIPTION_STYLE)
        old = _payload(self.base(contract=PRACTICAL_CONTRACT))
        self.assertEqual(e_contract(old)["version"], PRACTICAL_CONTRACT)
        with self.assertRaisesRegex(SafetyError, "AI_E_CONTRACT_INVALID"):
            e_contract(old, DESCRIPTION_CONTRACT)
        response = response_for(payload)
        checked = validate_results("E", old, response, declared_version=PRACTICAL_CONTRACT)
        self.assertEqual(checked["failures"][self.concept_id], "AI_FIELDS_INVALID")

    def test_all_ten_original_ids_can_use_explicit_estimates_without_observation_votes(self):
        payload = _payload(self.base()); response = response_for(payload)
        for relation in response["results"][0]["relevance"]:
            relation["relationship"] = "unrelated"
        checked = self.checked(payload, response)
        self.assertEqual(checked["failures"], {})
        value = checked["accepted"][self.concept_id]
        fit = evaluate_fit(value["criteria"], FIT_POLICY_ID, description=payload["items"][0]["description"],
                           description_contract=DESCRIPTION_INFERENCE_KIND)
        self.assertEqual(fit["kpi_matches"], 10)
        self.assertEqual([row["id"] for row in value["criteria"]], list(ACTIVE_CRITERION_IDS))
        self.assertTrue(all(row["references"] == [] for row in value["criteria"]))
        self.assertTrue(all(row["relationship"] == "unrelated" for row in value["relevance"]))

    def test_scoring_requires_explicit_current_description_and_contract(self):
        payload = _payload(self.base()); rows = response_for(payload)["results"][0]["criteria"]
        with self.assertRaisesRegex(SafetyError, "FIT_DESCRIPTION_CONTRACT_REQUIRED"):
            evaluate_fit(rows, FIT_POLICY_ID)
        with self.assertRaisesRegex(SafetyError, "FIT_DESCRIPTION_CONTEXT_REQUIRED"):
            evaluate_fit(rows, FIT_POLICY_ID, description_contract=DESCRIPTION_INFERENCE_KIND)
        changed = dict(payload["items"][0]["description"], function="A different fictional mechanism")
        with self.assertRaisesRegex(SafetyError, "FIT_DESCRIPTION_BINDING_INVALID"):
            evaluate_fit(rows, FIT_POLICY_ID, description=changed, description_contract=DESCRIPTION_INFERENCE_KIND)

    def test_description_context_and_each_row_hash_are_checked(self):
        payload = _payload(self.base())
        for change in (lambda p, v: p["items"][0]["description_inference_context"].update(description_hash="a" * 64),
                       lambda p, v: v["criteria"][0]["description_inference"].update(description_hash="b" * 64)):
            p, response = deepcopy(payload), response_for(payload)
            change(p, response["results"][0])
            checked = self.checked(p, response)
            self.assertTrue(checked["failures"])

    def test_only_model_selected_populated_description_fields_are_admitted(self):
        payload = _payload(self.base())
        for fields in ([], ["category"], ["brand"], ["price"], ["source_ids"], ["function", "function"], [None]):
            response = response_for(payload)
            response["results"][0]["criteria"][0]["description_inference"]["fields"] = fields
            self.assertTrue(self.checked(payload, response)["failures"], fields)
        description = {"label": "Fictional compact aid", "function": "An ordinary useful task", "features": []}
        row = {"id": 1, "state": "Meets", "basis": "inferred", "references": [], "reason": "Estimated: Compact aid.",
               "description_inference": {"kind": DESCRIPTION_INFERENCE_KIND, "description_hash": digest(description), "fields": ["features"]}}
        with self.assertRaisesRegex(SafetyError, "FIT_DESCRIPTION_FIELDS_INVALID"):
            validate_description_inference(row, description, description_contract=DESCRIPTION_INFERENCE_KIND)

    def test_estimates_cannot_be_observed_measured_or_mixed_with_source_references(self):
        payload = _payload(self.base())
        changes = [{"basis": "observed"}, {"reason": "Measured weight is 2 kg."},
                   {"reason": "Saves 10 minutes."}, {"reason": "Weighs 2 kg."},
                   {"reason": "Measures 20 cm."}, {"reason": "Estimated: Costs ₹100."},
                   {"reason": "Estimated: Produces 50% growth."}, {"reason": "Estimated: Has 100 sales."},
                   {"references": ["R1"]}]
        for change in changes:
            response = response_for(payload); response["results"][0]["criteria"][0].update(change)
            self.assertEqual(self.checked(payload, response)["failures"][self.concept_id], "FIT_DESCRIPTION_INFERENCE_INVALID", change)

    def test_missing_prose_prefix_and_practical_estimates_are_accepted(self):
        from product_radar.fit_description import display_fit_reason
        payload = _payload(self.base())
        for reason in ("Ordinary compact household construction.", "Roughly 2 kg for an ordinary example.",
                       "Estimated: Saves about 10 minutes of repeated setup.", "Likely around 20 cm for a typical example."):
            response = response_for(payload); response["results"][0]["criteria"][0]["reason"] = reason
            checked = self.checked(payload, response)
            self.assertEqual(checked["failures"], {}, reason)
            row = checked["accepted"][self.concept_id]["criteria"][0]
            self.assertEqual(row["reason"], reason)
            self.assertTrue(display_fit_reason(row).startswith("Estimated:"))

    def test_supplied_advertised_capacity_is_not_a_blanket_numerical_rejection(self):
        from product_radar.fit_description import validate_description_inference
        description = {"label": "Kitchen scale", "function": "Weigh ingredients", "features": ["Advertised 10 kg capacity"]}
        row = {"id": 1, "state": "Meets", "basis": "inferred", "references": [],
               "reason": "Its advertised 10kg capacity does not imply that the scale is heavy.",
               "description_inference": {"kind": DESCRIPTION_INFERENCE_KIND, "description_hash": digest(description), "fields": ["features"]}}
        self.assertTrue(validate_description_inference(row, description, description_contract=DESCRIPTION_INFERENCE_KIND))

    def test_estimate_label_never_allows_invented_sales_profit_or_money_amounts(self):
        payload = _payload(self.base())
        for reason in ("Estimated: About 100 orders each week.", "Estimated: 50% profit.",
                       "Estimated: Saves ₹100.", "Estimated: Demand rose 40%."):
            response = response_for(payload); response["results"][0]["criteria"][0]["reason"] = reason
            self.assertTrue(self.checked(payload, response)["failures"], reason)

    def test_description_hash_never_becomes_an_observation_reference_or_relevance_id(self):
        payload = _payload(self.base())
        response = response_for(payload)
        response["results"][0]["criteria"][0]["references"] = [digest(payload["items"][0]["description"])]
        self.assertEqual(self.checked(payload, response)["failures"][self.concept_id], "AI_EVIDENCE_REFERENCE_INVALID")
        response = response_for(payload)
        response["results"][0]["relevance"][0]["id"] = digest(payload["items"][0]["description"])
        self.assertEqual(self.checked(payload, response)["failures"][self.concept_id], "AI_RELEVANCE_REFERENCE_INVALID")

    def test_supported_negative_is_separate_from_unknown(self):
        payload = _payload(self.base()); response = response_for(payload)
        response["results"][0]["criteria"][-1].update(state="Does not meet", reason="Estimated: Ordinary shared household use is unisex.")
        response["results"][0]["audience_positioning"]["positioning"] = "unisex"
        checked = self.checked(payload, response)
        value = checked["accepted"][self.concept_id]
        fit = evaluate_fit(value["criteria"], FIT_POLICY_ID, description=payload["items"][0]["description"],
                           description_contract=DESCRIPTION_INFERENCE_KIND)
        self.assertEqual((fit["kpi_matches"], fit["does_not_meet"]), (9, 1))
        response["results"][0]["criteria"][-1].update(state="Unclear", basis="not_stated", description_inference=None)
        value = self.checked(payload, response)["accepted"][self.concept_id]
        self.assertEqual(evaluate_fit(value["criteria"], FIT_POLICY_ID, description=payload["items"][0]["description"],
                                     description_contract=DESCRIPTION_INFERENCE_KIND)["unknown"], 1)

    def test_null_marker_keeps_original_observation_backed_validation(self):
        payload = _payload(self.base()); response = response_for(payload, all_description=False)
        checked = self.checked(payload, response)
        self.assertEqual(checked["failures"], {})
        self.assertTrue(all(row["description_inference"] is None for row in checked["accepted"][self.concept_id]["criteria"]))

    def test_live_description_inference_is_not_accepted_as_legacy_thirteen_point_arithmetic(self):
        payload = _payload(self.base()); row = response_for(payload)["results"][0]["criteria"][0]
        from test_fit_policy import criteria
        rows = criteria(policy_id=LEGACY_POLICY_ID); rows[0] = row
        with self.assertRaisesRegex(SafetyError, "FIT_DESCRIPTION_POLICY_INVALID"):
            evaluate_fit(rows, LEGACY_POLICY_ID, description=payload["items"][0]["description"],
                         description_contract=DESCRIPTION_INFERENCE_KIND)

    def test_normal_preparation_application_and_saved_replay_are_connected(self):
        rows = prepare_practical_fit_reviews(self.memory, concept_ids=[self.concept_id], contract_version=DESCRIPTION_CONTRACT)
        self.assertEqual(rows[0]["status"], "prepared")
        runner = DescriptionRunner(); result = run_fit_reviews(self.memory, runner, [rows[0]["review_id"]])
        self.assertEqual(result[0]["status"], "accepted")
        self.assertEqual(len(runner.calls), 1)
        state = self.memory.snapshot(); assessment = state["assessments"][self.concept_id]
        self.assertEqual(assessment["contract_version"], DESCRIPTION_CONTRACT)
        self.assertEqual(assessment["fit_description_contract"], DESCRIPTION_INFERENCE_KIND)
        receipt = state["source_checks"][key("fit-review-apply", rows[0]["review_id"])]
        self.assertEqual(receipt["fit_description_contract"], DESCRIPTION_INFERENCE_KIND)
        self.assertEqual(receipt["assessment_hash"], digest(assessment))
        self.assertEqual(run_fit_reviews(self.memory, runner, [rows[0]["review_id"]])[0]["status"], "accepted")
        self.assertEqual(len(runner.calls), 1)

    def test_existing_valid_v2_is_reused_without_rescoring_or_new_jobs(self):
        base = self.base(contract=PRACTICAL_CONTRACT); runner = FitRunner()
        self.assertEqual(run_fit_reviews(self.memory, runner, [base["reference"]])[0]["status"], "accepted")
        before = self.memory.snapshot()
        rows = prepare_practical_fit_reviews(self.memory, concept_ids=[self.concept_id], contract_version=DESCRIPTION_CONTRACT)
        self.assertEqual(rows[0]["status"], "accepted")
        self.assertEqual(self.memory.snapshot(), before)
        self.assertEqual(len(runner.calls), 1)

    def test_pending_default_stays_v2_until_caller_opts_in(self):
        rows = prepare_practical_fit_reviews(self.memory, concept_ids=[self.concept_id])
        base = self.memory.snapshot()["source_checks"][rows[0]["review_id"]]
        self.assertEqual(base["binding"]["contract_version"], PRACTICAL_CONTRACT)
        self.assertNotIn("description_inference_context", _payload(base)["items"][0])

    def test_one_shape_repair_preserves_original_and_stays_finite(self):
        base = self.base()
        class Runner(DescriptionRunner):
            def __call__(inner, role, payload, folder):
                response, usage = super().__call__(role, payload, folder)
                if not payload.get("checklist_repair"):
                    response["results"][0]["criteria"].pop()
                return response, usage
        runner = Runner()
        self.assertEqual(run_fit_reviews(self.memory, runner, [base["reference"]])[0]["status"], "accepted")
        self.assertEqual(len(runner.calls), 2)
        raw = {path: path.read_bytes() for path in (self.memory.root / "ai_jobs").rglob("result.json")}
        run_fit_reviews(self.memory, runner, [base["reference"]])
        self.assertEqual(len(runner.calls), 2)
        self.assertTrue(all(path.read_bytes() == original for path, original in raw.items()))

    def test_failed_v3_repair_does_not_repeat_until_score_passes(self):
        base = self.base()
        class Runner(DescriptionRunner):
            def __call__(inner, role, payload, folder):
                response, usage = super().__call__(role, payload, folder)
                response["results"][0]["criteria"][0]["references"] = ["invented-observation"]
                return response, usage
        runner = Runner()
        self.assertEqual(run_fit_reviews(self.memory, runner, [base["reference"]])[0]["status"], "rejected")
        self.assertEqual(run_fit_reviews(self.memory, runner, [base["reference"]])[0]["status"], "rejected")
        self.assertEqual(len(runner.calls), 2)

    def test_description_output_binding_can_receive_one_finite_contract_repair(self):
        base = self.base()
        class Runner(DescriptionRunner):
            def __call__(inner, role, payload, folder):
                response, usage = super().__call__(role, payload, folder)
                if not payload.get("checklist_repair"):
                    response["results"][0]["criteria"][0]["description_inference"]["description_hash"] = "a" * 64
                return response, usage
        runner = Runner()
        outcome = run_fit_reviews(self.memory, runner, [base["reference"]])[0]
        self.assertEqual(outcome["status"], "accepted")
        self.assertEqual(len(runner.calls), 2)
        self.assertEqual(runner.calls[1][1]["checklist_repair"]["failure_code"], "FIT_DESCRIPTION_BINDING_INVALID")
        run_fit_reviews(self.memory, runner, [base["reference"]])
        self.assertEqual(len(runner.calls), 2)

    def test_description_output_inference_repair_exhaustion_replays_without_more_calls(self):
        base = self.base()
        class Runner(DescriptionRunner):
            def __call__(inner, role, payload, folder):
                response, usage = super().__call__(role, payload, folder)
                response["results"][0]["criteria"][0]["reason"] = "Confirmed: measured weight is 2 kg."
                return response, usage
        runner = Runner()
        for _ in range(2):
            outcome = run_fit_reviews(self.memory, runner, [base["reference"]])[0]
            self.assertEqual((outcome["status"], outcome["reason"]), ("rejected", "FIT_DESCRIPTION_INFERENCE_INVALID"))
        self.assertEqual(len(runner.calls), 2)

    def test_current_accepted_v3_is_reused_without_new_jobs_or_bases(self):
        prepared = prepare_practical_fit_reviews(self.memory, concept_ids=[self.concept_id],
                                                 contract_version=DESCRIPTION_CONTRACT)
        runner = DescriptionRunner()
        run_fit_reviews(self.memory, runner, [prepared[0]["review_id"]])
        before = self.memory.snapshot()
        repeated = prepare_practical_fit_reviews(self.memory, concept_ids=[self.concept_id],
                                                 contract_version=DESCRIPTION_CONTRACT)
        self.assertEqual((repeated[0]["status"], repeated[0]["review_id"], repeated[0]["reused"]),
                         ("accepted", None, True))
        self.assertEqual(self.memory.snapshot(), before)
        self.assertEqual(len(runner.calls), 1)

    def test_partial_review_retains_other_description_estimates_and_their_fields(self):
        base = self.base(); runner = DescriptionRunner()
        run_fit_reviews(self.memory, runner, [base["reference"]])
        before = deepcopy(self.memory.snapshot()["assessments"][self.concept_id]["result"]["criteria"])
        partial = prepare_fit_review(self.memory, self.concept_id, requested_criterion_ids=[7],
                                    reason="synthetic-one-question", contract_version=DESCRIPTION_CONTRACT)
        self.assertEqual(run_fit_reviews(self.memory, runner, [partial["reference"]])[0]["status"], "accepted")
        after = self.memory.snapshot()["assessments"][self.concept_id]["result"]["criteria"]
        self.assertEqual([row for row in after if row["id"] != 7], [row for row in before if row["id"] != 7])

    def test_new_v3_partial_base_explicitly_projects_v2_rows_without_changing_old_answer(self):
        old_base = self.base(contract=PRACTICAL_CONTRACT)
        run_fit_reviews(self.memory, FitRunner(), [old_base["reference"]])
        old = deepcopy(self.memory.snapshot()["assessments"][self.concept_id])
        saved = {path: path.read_bytes() for path in (self.memory.root / "ai_jobs").rglob("result.json")}
        partial = prepare_fit_review(self.memory, self.concept_id, requested_criterion_ids=[7],
                                    reason="synthetic-v2-to-v3-one-question", contract_version=DESCRIPTION_CONTRACT)
        self.assertEqual(self.memory.snapshot()["assessments"][self.concept_id], old)
        self.assertTrue(all(row["description_inference"] is None for row in partial["binding"]["retained_rows"]))
        self.assertEqual(partial["binding"]["parent_assessment"], old)
        self.assertEqual(run_fit_reviews(self.memory, DescriptionRunner(), [partial["reference"]])[0]["status"], "accepted")
        rows = self.memory.snapshot()["assessments"][self.concept_id]["result"]["criteria"]
        self.assertTrue(all("description_inference" in row for row in rows))
        self.assertEqual([{key: value for key, value in row.items() if key != "description_inference"}
                          for row in rows if row["id"] != 7],
                         [row for row in old["result"]["criteria"] if row["id"] != 7])
        self.assertTrue(all(path.read_bytes() == raw for path, raw in saved.items()))

    def test_saved_v2_repair_is_attributed_unknown_then_one_new_contract_selects_fields(self):
        base = self.base(contract=PRACTICAL_CONTRACT)
        class LegacyRunner(FitRunner):
            def __call__(inner, role, payload, folder):
                response, usage = super().__call__(role, payload, folder)
                response["results"][0]["criteria"][0]["references"] = []
                return response, usage
        original = LegacyRunner()
        outcome = run_fit_reviews(self.memory, original, [base["reference"]])[0]
        self.assertEqual(outcome["status"], "rejected")
        self.assertEqual(len(original.calls), 2)
        state = self.memory.snapshot(); request = state["jobs"][outcome["job_id"]]["request"]
        result_path = self.memory.root / "ai_jobs" / outcome["job_id"] / "result.json"
        saved = load_json(result_path); original_bytes = result_path.read_bytes()
        attribution = saved_description_review_attribution(request, saved)
        self.assertEqual(attribution["criterion_ids"], [1])
        row = attribution["unknown_result"]["criteria"][0]
        self.assertEqual((row["state"], row["references"]), ("Unclear", []))
        self.assertNotIn("description_inference", row)
        prepared = prepare_description_fit_from_saved_repair(self.memory, request, saved)
        repeat = prepare_description_fit_from_saved_repair(self.memory, request, saved)
        self.assertEqual(prepared["review_id"], repeat["review_id"])
        automatic = prepare_practical_fit_reviews(self.memory, concept_ids=[self.concept_id], contract_version=DESCRIPTION_CONTRACT)
        self.assertEqual(automatic[0]["review_id"], prepared["review_id"])
        runner = DescriptionRunner()
        self.assertEqual(run_fit_reviews(self.memory, runner, [prepared["review_id"]])[0]["status"], "accepted")
        self.assertEqual(len(runner.calls), 1)
        assessment = self.memory.snapshot()["assessments"][self.concept_id]
        self.assertEqual(assessment["fit_review"]["description_review_attribution"], attribution)
        self.assertEqual(result_path.read_bytes(), original_bytes)

    def test_unprepared_or_changed_source_repair_attribution_cannot_be_applied(self):
        payload = _payload(self.base(contract=PRACTICAL_CONTRACT)); response, usage = FitRunner()("E", payload, None)
        request = {"role": "E", "prompt_version": PRACTICAL_CONTRACT, "model": "synthetic", "runner": "synthetic", "payload": payload}
        saved = {"request_hash": digest(request), "response": response, "usage": usage}
        with self.assertRaisesRegex(SafetyError, "FIT_DESCRIPTION_ATTRIBUTION_INVALID"):
            saved_description_review_attribution(request, saved)
        saved["request_hash"] = "a" * 64
        with self.assertRaisesRegex(SafetyError, "FIT_DESCRIPTION_ATTRIBUTION_INVALID"):
            saved_description_review_attribution(request, saved)


if __name__ == "__main__":
    unittest.main()
