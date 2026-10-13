"""Practical estimates and exact short references; all evidence is synthetic."""

from copy import deepcopy
import unittest

import test_fit_review
from test_fit_review import FitRunner
from product_radar.fit_policy import ACTIVE_CRITERION_IDS, FIT_POLICY_ID
from product_radar.fit_review import (PRACTICAL_CONTRACT, _payload, prepare_fit_review,
                                     prepare_practical_fit_reviews, repair_practical_fit_review, run_fit_reviews)
from product_radar.models import SafetyError
from product_radar.research_ai import (FIT_PROMPT_VERSION, PRACTICAL_FIT_E_INSTRUCTIONS,
                                     PRACTICAL_FIT_PROMPT_VERSION, e_contract, validate_results)
from product_radar.research_flow import current_assessment_fit
from product_radar.research_memory import change


class PracticalReviewTests(unittest.TestCase):
    setUp = test_fit_review.FitReviewTests.setUp

    def base(self):
        return prepare_fit_review(self.memory, self.concept_id,
            requested_criterion_ids=list(ACTIVE_CRITERION_IDS), reason="full_practical_estimates_v2_review",
            contract_version=PRACTICAL_CONTRACT)

    def alias_response(self, payload):
        response, _ = FitRunner()("E", payload, None)
        aliases = {value: name for name, value in payload["items"][0]["reference_aliases"].items()}
        for value in response["results"]:
            for row in value["criteria"]:
                row["references"] = [aliases[reference] for reference in row["references"]]
            for row in value["relevance"]:
                row["id"] = aliases[row["id"]]
        return response

    def test_versions_are_explicit_and_old_payload_stays_old(self):
        payload = _payload(self.base())
        self.assertEqual(e_contract(payload)["version"], PRACTICAL_FIT_PROMPT_VERSION)
        with self.assertRaisesRegex(SafetyError, "AI_E_CONTRACT_INVALID"):
            e_contract(payload, FIT_PROMPT_VERSION)
        old = dict(payload); old.pop("judgment_style")
        self.assertEqual(e_contract(old)["version"], FIT_PROMPT_VERSION)
        self.assertNotIn("earlier_judgments", payload["items"][0])
        self.assertIn("Estimated:", PRACTICAL_FIT_E_INSTRUCTIONS)

    def test_short_aliases_resolve_exactly_to_saved_observations(self):
        base = self.base(); payload = _payload(base)
        checked = validate_results("E", payload, self.alias_response(payload))
        self.assertFalse(checked["failures"])
        value = checked["accepted"][self.concept_id]
        expected = {row["id"] for row in payload["items"][0]["observations"]}
        self.assertEqual(set(value["criteria"][0]["references"]), expected)
        self.assertEqual({row["id"] for row in value["relevance"]}, expected)

    def test_unlisted_alias_and_truncated_reference_never_get_guessed(self):
        payload = _payload(self.base())
        for reference in ("R999", payload["items"][0]["observations"][0]["id"][:-1]):
            response = self.alias_response(payload)
            response["results"][0]["criteria"][0]["references"] = [reference]
            checked = validate_results("E", payload, response)
            self.assertEqual(checked["failures"][self.concept_id], "AI_EVIDENCE_REFERENCE_INVALID")

    def test_changed_alias_map_does_not_manufacture_evidence(self):
        payload = _payload(self.base()); response = self.alias_response(payload)
        payload["items"][0]["reference_aliases"]["R1"] = "made-up-observation"
        checked = validate_results("E", payload, response)
        self.assertEqual(checked["failures"][self.concept_id], "AI_FIT_REFERENCE_ALIASES_INVALID")

    def test_fresh_full_review_can_replace_a_stale_parent_not_a_partial_review(self):
        state = self.memory.snapshot(); parent = deepcopy(state["assessments"][self.concept_id])
        parent["input_hash"] = "old-facts"
        self.memory.commit("synthetic-stale-parent", [change("assessments", self.concept_id, parent,
                                                            state["assessments"][self.concept_id])])
        with self.assertRaisesRegex(SafetyError, "FIT_REVIEW_PARENT_FACTS_CHANGED"):
            prepare_fit_review(self.memory, self.concept_id, requested_criterion_ids=[7, 13],
                               reason="partial", contract_version=PRACTICAL_CONTRACT)
        base = self.base(); runner = FitRunner()
        result = run_fit_reviews(self.memory, runner, [base["reference"]])
        self.assertTrue(result[0]["applied"])
        self.assertTrue(current_assessment_fit(self.memory.snapshot(), self.concept_id,
                                               expected_policy_id=FIT_POLICY_ID))

    def test_full_list_completion_reuses_current_reviews_without_new_model_calls(self):
        prepared = prepare_practical_fit_reviews(self.memory)
        ids = [row["review_id"] for row in prepared if row["review_id"]]
        runner = FitRunner()
        self.assertTrue(run_fit_reviews(self.memory, runner, ids)[0]["applied"])
        calls = len(runner.calls); state = self.memory.snapshot()
        replay = prepare_practical_fit_reviews(self.memory)
        self.assertTrue(all(row["reused"] for row in replay))
        self.assertEqual(len(runner.calls), calls)
        self.assertEqual(self.memory.snapshot(), state)

    def test_rejected_reference_is_repaired_once_and_original_response_is_preserved(self):
        base = self.base()
        class Runner(FitRunner):
            def __call__(inner, role, payload, folder):
                response, usage = super().__call__(role, payload, folder)
                if not payload.get("checklist_repair"):
                    response["results"][0]["criteria"][0]["references"] = ["invented-reference"]
                return response, usage
        runner = Runner()
        original = run_fit_reviews(self.memory, runner, [base["reference"]], repair_rejected=False)
        self.assertEqual(original[0]["status"], "rejected")
        original_files = {path: path.read_bytes() for path in (self.memory.root / "ai_jobs").rglob("result.json")}
        result = repair_practical_fit_review(self.memory, runner, base["reference"])
        self.assertEqual(result["status"], "accepted")
        self.assertEqual(len(runner.calls), 2)
        self.assertTrue(all(path.read_bytes() == raw for path, raw in original_files.items()))
        replay = run_fit_reviews(self.memory, runner, [base["reference"]])
        self.assertEqual(replay[0]["status"], "accepted")
        self.assertEqual(len(runner.calls), 2)

    def test_default_flow_repairs_a_shape_error_without_requesting_a_better_score(self):
        base = self.base()
        class Runner(FitRunner):
            def __call__(inner, role, payload, folder):
                response, usage = super().__call__(role, payload, folder)
                if not payload.get("checklist_repair"):
                    response["results"][0]["criteria"] = response["results"][0]["criteria"][:-1]
                return response, usage
        runner = Runner()
        results = run_fit_reviews(self.memory, runner, [base["reference"]])
        self.assertEqual(results[0]["status"], "accepted")
        self.assertEqual(len(runner.calls), 2)
        self.assertIn("do not target a higher score", runner.calls[1][1]["checklist_repair"]["rules"])
        self.assertNotIn("fit_review_base_id", runner.calls[1][1]["items"][0])

    def test_rejected_repair_replays_without_another_model_call(self):
        base = self.base()
        class Runner(FitRunner):
            def __call__(inner, role, payload, folder):
                response, usage = super().__call__(role, payload, folder)
                response["results"][0]["criteria"][0]["references"] = ["wrong-reference"]
                return response, usage
        runner = Runner()
        result = run_fit_reviews(self.memory, runner, [base["reference"]])
        self.assertEqual(result[0]["status"], "rejected")
        self.assertEqual(len(runner.calls), 2)
        replay = run_fit_reviews(self.memory, runner, [base["reference"]])
        self.assertEqual(replay[0]["status"], "rejected")
        self.assertEqual(len(runner.calls), 2)

    def test_accepted_or_unstarted_review_does_not_create_a_repair_job(self):
        base = self.base(); runner = FitRunner()
        with self.assertRaisesRegex(SafetyError, "PRACTICAL_FIT_REPAIR_ORIGINAL_REQUIRED"):
            repair_practical_fit_review(self.memory, runner, base["reference"])
        self.assertFalse(runner.calls)
        run_fit_reviews(self.memory, runner, [base["reference"]])
        replay = repair_practical_fit_review(self.memory, runner, base["reference"])
        self.assertEqual(replay["status"], "accepted")
        self.assertEqual(len(runner.calls), 1)


if __name__ == "__main__":
    unittest.main()
