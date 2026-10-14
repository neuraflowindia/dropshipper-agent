"""Functional v5 checklist: labelled estimates instead of Unclear (Plan R10 W9, P2).

Disposable accepted-type facts and a fake runner only; no model, provider or
live store is touched.
"""

from copy import deepcopy
from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from product_radar.fit_description import FUNCTIONAL_CONTRACT, FUNCTIONAL_RESPONSE_VERSION, FUNCTIONAL_STYLE
from product_radar.fit_policy import ACTIVE_CRITERION_IDS
from product_radar.fit_review import _payload, run_fit_reviews
from product_radar.models import digest
from product_radar.research_ai import (DESCRIPTION_FIT_E_INSTRUCTIONS, ESTIMATE_OVER_UNCLEAR_RULE,
    FUNCTIONAL_FIELDS_FIT_E_INSTRUCTIONS, FUNCTIONAL_FIT_E_INSTRUCTIONS, FIT_PROMPT_VERSIONS, e_contract)
from product_radar.research_flow import candidate_summary, current_assessment_fit
import test_functional_fit as fixtures
from test_research_flow import NOW

EARLIER_CONTRACT = "research-e-fit10-functional-v4"


class CurrentRunner(fixtures.FunctionalFitRunner):
    identity = "synthetic-functional-fit-v5"


class FunctionalEstimateContractTests(unittest.TestCase):
    def setUp(self):
        self.fixture = fixtures.FunctionalFitTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.memory, self.cid = self.fixture.memory, self.fixture.cid

    def question(self):
        row = self.fixture.prepare()
        return row, _payload(self.memory.snapshot()["source_checks"][row["review_id"]])

    def test_new_prompt_version_identifies_every_functional_question(self):
        self.fixture.accept()
        _, payload = self.question()
        self.assertEqual(FUNCTIONAL_CONTRACT, "research-e-fit10-functional-v5")
        self.assertEqual(payload["judgment_style"], FUNCTIONAL_STYLE)
        self.assertEqual(e_contract(payload)["version"], FUNCTIONAL_CONTRACT)
        self.assertIn(FUNCTIONAL_CONTRACT, FIT_PROMPT_VERSIONS)
        self.assertNotIn(EARLIER_CONTRACT, FIT_PROMPT_VERSIONS)
        self.assertEqual(payload["items"][0]["functional_contract"], FUNCTIONAL_CONTRACT)

    def test_sent_question_carries_the_estimate_rule(self):
        self.fixture.accept()
        _, payload = self.question()
        payload["response_attribution"] = FUNCTIONAL_RESPONSE_VERSION
        instructions = e_contract(payload)["instructions"]
        self.assertEqual(instructions, FUNCTIONAL_FIELDS_FIT_E_INSTRUCTIONS)
        self.assertTrue(instructions.endswith(ESTIMATE_OVER_UNCLEAR_RULE))
        self.assertTrue(FUNCTIONAL_FIT_E_INSTRUCTIONS.endswith(ESTIMATE_OVER_UNCLEAR_RULE))
        for phrase in ("labelled practical estimate", "instead of Unclear",
                       "Use Unclear only when the form or the use itself is not known",
                       "'Estimated:'", "never absent proof alone",
                       "never invents minutes, hours, percentages", "moderate product-specific Indian buyer judgment",
                       "do not mark a product women_centric for its category alone"):
            self.assertIn(phrase, instructions)
        # Contract section 4: no invented time amounts in a practical estimate.
        self.assertNotIn("Rough size, weight or time estimates", instructions)
        # One rule on uncertainty: never Does not meet; Unclear only for an unknown form or use.
        self.assertNotIn("Keep genuinely uncertain answers Unclear", instructions)
        self.assertIn("Genuine uncertainty is never Does not meet", instructions)
        self.assertNotIn(ESTIMATE_OVER_UNCLEAR_RULE, DESCRIPTION_FIT_E_INSTRUCTIONS)

    def test_checker_refuses_invented_times_and_percentages_but_keeps_rough_sizes(self):
        from product_radar.fit_description import FIT_ESTIMATE_VALIDATION_VERSION
        from product_radar.research_ai import validate_results
        self.assertEqual(FIT_ESTIMATE_VALIDATION_VERSION, "practical-estimate-validation-v3")
        self.fixture.accept()
        _, payload = self.question()
        for reason in ("Saves about 10 minutes per wash.", "Roughly 20% less handling.",
                       "About 30 seconds faster each time.", "Likely 1 hour saved weekly.",
                       "Around 5 percent less waste."):
            response = fixtures.response_for(payload); response["results"][0]["criteria"][0]["reason"] = reason
            self.assertEqual(validate_results("E", payload, response)["failures"].get(self.cid),
                             "FIT_DESCRIPTION_INFERENCE_INVALID", reason)
        for reason in ("Likely about 300 grams when packed.", "Roughly 20 cm long, so compact.",
                       "Fewer transfers while draining."):
            response = fixtures.response_for(payload); response["results"][0]["criteria"][0]["reason"] = reason
            self.assertEqual(validate_results("E", payload, response)["failures"], {}, reason)

    def test_a_time_stated_in_the_supplied_fields_may_be_repeated(self):
        from product_radar.fit_description import _valid_estimate_numbers
        description = {"label": "Timer lid", "function": "Rings after 10 minutes of steeping"}
        self.assertTrue(_valid_estimate_numbers("Rings after 10 minutes, so less watching.", description,
                                                ["function"], False))
        self.assertFalse(_valid_estimate_numbers("Saves about 5 minutes.", description, ["function"], False))

    def test_earlier_contract_answer_is_reassessed_once_then_reused(self):
        self.fixture.accept()
        old_runner, _ = self.fixture.complete()
        state = self.memory.snapshot(); saved = state["assessments"][self.cid]
        # The live store holds answers and jobs saved under the earlier v4
        # identity; their saved request differs from any v5 question.
        earlier = dict(deepcopy(saved), contract_version=EARLIER_CONTRACT)
        earlier["record_hash"] = digest({field: value for field, value in earlier.items() if field != "record_hash"})
        self.memory.put("assessments", self.cid, earlier, previous=saved)
        (job_id, job), = [(ref, row) for ref, row in state["jobs"].items()
                          if row["request"].get("prompt_version") == FUNCTIONAL_CONTRACT]
        old_job = deepcopy(job)
        old_job["request"].update(prompt_version=EARLIER_CONTRACT)
        old_job["request"]["payload"]["judgment_style"] = "practical-functional-estimates-v4"
        for item in old_job["request"]["payload"]["items"]:
            item["functional_contract"] = EARLIER_CONTRACT
        self.memory.put("jobs", job_id, old_job, previous=job)
        state = self.memory.snapshot()
        self.assertIsNone(current_assessment_fit(state, self.cid))
        summary = next(row for row in candidate_summary(self.memory, as_of=NOW)["products"] if row["id"] == self.cid)
        self.assertIsNone(summary["checklist_meets"])
        row = self.fixture.prepare()
        self.assertEqual(row["status"], "prepared")
        base = self.memory.snapshot()["source_checks"][row["review_id"]]
        self.assertEqual(base["binding"]["contract_version"], FUNCTIONAL_CONTRACT)
        self.assertEqual(base["binding"]["requested_criterion_ids"], list(ACTIVE_CRITERION_IDS))
        self.assertEqual(base["binding"]["review_reason"], "functional_type_estimates_v5_review")
        runner = CurrentRunner()
        outcome = run_fit_reviews(self.memory, runner, [row["review_id"]])
        self.assertEqual(outcome[0]["status"], "accepted", outcome)
        self.assertEqual(len(old_runner.calls), 1)
        self.assertEqual(len(runner.calls), 1)
        self.assertEqual(runner.calls[0][1]["response_attribution"], FUNCTIONAL_RESPONSE_VERSION)
        state = self.memory.snapshot()
        self.assertEqual(state["assessments"][self.cid]["contract_version"], FUNCTIONAL_CONTRACT)
        self.assertEqual(current_assessment_fit(state, self.cid)["kpi_matches"], 10)
        # Unchanged inputs reuse the v5 answer: no further question.
        again = self.fixture.prepare()
        self.assertEqual((again["status"], again["reused"]), ("accepted", True))
        run_fit_reviews(self.memory, runner, [row["review_id"]])
        self.assertEqual(len(runner.calls), 1)


if __name__ == "__main__":
    unittest.main()
