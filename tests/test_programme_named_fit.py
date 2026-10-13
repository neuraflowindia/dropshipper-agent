"""Explicit current partial reviews, never automatic preferred-score retries."""

from copy import deepcopy
from datetime import timedelta
import unittest
from unittest.mock import patch

import test_fit_review
from test_fit_description import DescriptionRunner
from test_fit_review import FitRunner
from test_programme_fit_recovery import NoCalls
from test_research_flow import NOW
from product_radar.fit_description import DESCRIPTION_CONTRACT
from product_radar.fit_review import (PRACTICAL_CONTRACT, prepare_fit_review,
    prepare_practical_fit_reviews, run_fit_reviews)
from product_radar.models import SafetyError, digest, timestamp
from product_radar.programme import _fit
from product_radar.research_ai import prepare_retry
from product_radar.research_memory import key


class ProgrammeNamedFitTests(unittest.TestCase):
    setUp = test_fit_review.FitReviewTests.setUp

    def accepted(self, ids=None, *, version=PRACTICAL_CONTRACT):
        ids = ids or [self.concept_id]
        rows = prepare_practical_fit_reviews(self.memory, concept_ids=ids, as_of=NOW,
            contract_version=version)
        runner = DescriptionRunner() if version == DESCRIPTION_CONTRACT else FitRunner()
        self.assertTrue(all(row["status"] == "accepted" for row in run_fit_reviews(
            self.memory, runner, [row["review_id"] for row in rows])))
        return runner

    def partial(self, cid=None, *, version=PRACTICAL_CONTRACT, as_of=NOW):
        return prepare_fit_review(self.memory, cid or self.concept_id,
            requested_criterion_ids=[9], reason="Review only this explicitly named practical question from the saved facts.",
            as_of=as_of, contract_version=version)

    def stage(self, runner=None, *, ids=None, fit_limit=5, record=None, as_of=NOW):
        record = record or {"concept_ids": ids or [self.concept_id], "deferrals": {}}
        result = _fit(self.memory, record, {"fit_limit": fit_limit, "runner": runner,
            "at": as_of, "retry_seconds": 60})
        return result, record

    def test_explicit_v2_partial_runs_once_preserving_other_nine_and_market_meaning(self):
        self.accepted(); parent = deepcopy(self.memory.snapshot()["assessments"][self.concept_id])
        base = self.partial(); runner = FitRunner()
        result, _ = self.stage(runner)
        self.assertEqual(result["named_partial_reviews"], 1)
        self.assertEqual(result["current_accepted_reused"], 0)
        self.assertEqual(result["prepared"], 1)
        self.assertEqual(len(runner.calls), 1)
        self.assertEqual(runner.calls[0][1]["mode"], "fit_partial_review")
        self.assertEqual(runner.calls[0][1]["requested_criterion_ids"], [9])
        assessment = self.memory.snapshot()["assessments"][self.concept_id]
        self.assertEqual(assessment["fit_review"]["review_id"], base["reference"])
        for field in ("relevance", "compliance_note", "missing_questions", "audience_positioning"):
            self.assertEqual(assessment["result"][field], parent["result"][field])
        self.assertEqual([row for row in assessment["result"]["criteria"] if row["id"] != 9],
                         [row for row in parent["result"]["criteria"] if row["id"] != 9])
        before = self.memory.snapshot()
        replay, _ = self.stage(NoCalls())
        self.assertEqual(replay["current_accepted_reused"], 1)
        self.assertEqual(replay["named_partial_reviews"], 0)
        self.assertEqual(self.memory.snapshot(), before)

    def test_v3_partial_retains_current_v3_description_provenance(self):
        self.accepted(version=DESCRIPTION_CONTRACT)
        parent = deepcopy(self.memory.snapshot()["assessments"][self.concept_id])
        self.partial(version=DESCRIPTION_CONTRACT); runner = DescriptionRunner()
        result, _ = self.stage(runner)
        self.assertEqual(result["outcomes"][0]["status"], "accepted")
        updated = self.memory.snapshot()["assessments"][self.concept_id]
        self.assertEqual(updated["contract_version"], DESCRIPTION_CONTRACT)
        self.assertEqual([row for row in updated["result"]["criteria"] if row["id"] != 9],
                         [row for row in parent["result"]["criteria"] if row["id"] != 9])
        self.assertEqual(len(runner.calls), 1)

    def test_valid_negative_is_accepted_without_retrying_until_a_positive(self):
        self.accepted(); self.partial()
        class Negative(FitRunner):
            def __call__(self, role, payload, folder):
                response, usage = super().__call__(role, payload, folder)
                row = response["results"][0]["criteria"][0]
                row.update(state="Does not meet", basis="inferred",
                    references=[payload["items"][0]["observations"][0]["id"]],
                    reason="Synthetic supplied function supports this practical negative judgment.")
                return response, usage
        runner = Negative(); result, _ = self.stage(runner)
        self.assertEqual(result["outcomes"][0]["status"], "accepted")
        row = next(row for row in self.memory.snapshot()["assessments"][self.concept_id]["result"]["criteria"] if row["id"] == 9)
        self.assertEqual(row["state"], "Does not meet")
        self.stage(runner)
        self.assertEqual(len(runner.calls), 1)

    def test_saved_valid_partial_application_gap_recovers_exact_old_job_without_new_call(self):
        self.accepted(); base = self.partial(); original = FitRunner(); original.identity = "original-named-runner"
        with patch("product_radar.fit_review.apply_fit_review", side_effect=SafetyError("SAVED_FILE_UNAVAILABLE")):
            results = run_fit_reviews(self.memory, original, [base["reference"]])
        self.assertEqual(results[0]["status"], "needs_reconciliation")
        jobs = deepcopy(self.memory.snapshot()["jobs"])
        raw = {path: path.read_bytes() for path in (self.memory.root / "ai_jobs").rglob("result.json")}
        result, _ = self.stage(NoCalls())
        self.assertTrue(result["outcomes"][0]["applied"])
        self.assertEqual(self.memory.snapshot()["assessments"][self.concept_id]["job_id"], results[0]["job_id"])
        self.assertEqual(self.memory.snapshot()["jobs"], jobs)
        self.assertTrue(all(path.read_bytes() == value for path, value in raw.items()))
        self.assertEqual(len(original.calls), 1)

    def test_uncertain_original_partial_is_not_bypassed_by_full_or_another_runner(self):
        self.accepted(); base = self.partial()
        class Uncertain(FitRunner):
            def __call__(self, *_args):
                raise SafetyError("AI_NETWORK_UNAVAILABLE")
        self.assertEqual(run_fit_reviews(self.memory, Uncertain(), [base["reference"]])[0]["status"], "pending")
        before = self.memory.snapshot()
        result, record = self.stage(NoCalls())
        self.assertEqual(result["outcomes"][0]["reason"], "AI_JOB_PENDING_RECONCILIATION")
        self.assertEqual(self.memory.snapshot()["jobs"], before["jobs"])
        self.assertEqual(self.memory.snapshot()["assessments"], before["assessments"])
        self.assertEqual(record["deferrals"]["fit:" + self.concept_id]["binding"], digest(base["reference"]))

    def test_empty_prepared_partial_retry_keeps_original_owner_without_new_intent(self):
        self.accepted(); base = self.partial()
        class Uncertain(FitRunner):
            def __call__(self, *_args):
                raise SafetyError("AI_NETWORK_UNAVAILABLE")
        result = run_fit_reviews(self.memory, Uncertain(), [base["reference"]])[0]
        job_id = next(jid for jid, job in self.memory.snapshot()["jobs"].items()
            if any(item.get("fit_review_base_id") == base["reference"] for item in job["request"]["payload"]["items"]))
        prepare_retry(self.memory, job_id, "Synthetic named retry prepared but not dispatched")
        retry_intent = self.memory.root / "ai_jobs" / job_id / "retry-1" / "intent.json"
        result, _ = self.stage(FitRunner())
        self.assertEqual(result["outcomes"][0]["reason"], "AI_JOB_PENDING_RECONCILIATION")
        self.assertFalse(retry_intent.exists())

    def test_stale_partial_parent_is_skipped_without_dispatch_or_new_base(self):
        self.accepted(); base = self.partial()
        parent = self.memory.snapshot()["assessments"][self.concept_id]
        changed = deepcopy(parent); changed["result"]["compliance_note"] = "Different current saved review."
        self.memory.put("assessments", self.concept_id, changed, previous=parent)
        before = self.memory.snapshot()
        result, _ = self.stage(NoCalls())
        self.assertEqual(result["current_accepted_reused"], 1)
        self.assertEqual(result["named_partial_reviews"], 0)
        self.assertEqual(self.memory.snapshot(), before)
        self.assertNotIn(key("fit-review-apply", base["reference"]), before["source_checks"])

    def test_changed_description_never_applies_the_old_partial_answer(self):
        self.accepted(); base = self.partial()
        previous = self.memory.snapshot()["concepts"][self.concept_id]
        changed = deepcopy(previous); changed["description"]["function"] = "A genuinely changed fictional function"
        self.memory.put("concepts", self.concept_id, changed, previous=previous)
        runner = DescriptionRunner(); result, _ = self.stage(runner)
        self.assertEqual(result["named_partial_reviews"], 0)
        self.assertEqual(len(runner.calls), 1)
        self.assertEqual(runner.calls[0][1]["mode"], "fit_assessment")
        self.assertNotIn(key("fit-review-apply", base["reference"]), self.memory.snapshot()["source_checks"])
        self.assertEqual(self.memory.snapshot()["assessments"][self.concept_id]["contract_version"], DESCRIPTION_CONTRACT)

    def test_named_saved_result_symlink_integrity_error_is_not_hidden(self):
        self.accepted(); base = self.partial()
        with patch("product_radar.fit_review.apply_fit_review", side_effect=SafetyError("SAVED_FILE_UNAVAILABLE")):
            result = run_fit_reviews(self.memory, FitRunner(), [base["reference"]])[0]
        folder = self.memory.root / "ai_jobs" / result["job_id"]
        saved = folder / "result.json"; original = folder / "original-result.json"
        saved.rename(original); saved.symlink_to(original.name)
        with self.assertRaisesRegex(SafetyError, "SYMLINK_PATH_REFUSED"):
            self.stage(NoCalls())

    def test_future_preparation_is_not_applied_to_an_earlier_fixed_view(self):
        self.accepted(); future = (timestamp(NOW) + timedelta(hours=1)).isoformat()
        self.partial(as_of=future); before = self.memory.snapshot()
        result, _ = self.stage(NoCalls())
        self.assertEqual(result["current_accepted_reused"], 1)
        self.assertEqual(self.memory.snapshot(), before)

    def test_named_partial_limit_is_shared_with_new_full_reviews(self):
        self.accepted(); self.partial(); runner = FitRunner()
        ids = list(self.memory.snapshot()["concepts"])
        result, _ = self.stage(runner, ids=ids, fit_limit=1)
        self.assertEqual(result["prepared"], 1)
        self.assertEqual(result["remaining_to_prepare"], len(ids) - 1)
        self.assertEqual(len(runner.calls), 1)
        self.assertFalse(any(row.get("binding", {}).get("contract_version") == DESCRIPTION_CONTRACT
            for row in self.memory.snapshot()["source_checks"].values()))

    def test_named_local_failure_preserves_parent_and_healthy_sibling_continues(self):
        ids = list(self.memory.snapshot()["concepts"]); self.accepted(ids)
        bases = [self.partial(cid) for cid in ids]; original_run = run_fit_reviews
        def local_failure(memory, runner, review_ids, **options):
            if review_ids == [bases[0]["reference"]]:
                raise OSError("Synthetic local saved file failure")
            return original_run(memory, runner, review_ids, **options)
        parent = deepcopy(self.memory.snapshot()["assessments"][ids[0]])
        runner = FitRunner()
        with patch("product_radar.fit_review.run_fit_reviews", side_effect=local_failure):
            result, record = self.stage(runner, ids=ids)
        self.assertEqual([row["status"] for row in result["outcomes"]], ["needs_reconciliation", "accepted"])
        self.assertEqual(record["deferrals"]["fit:" + ids[0]]["reason"], "SAVED_FILE_UNAVAILABLE")
        self.assertEqual(self.memory.snapshot()["assessments"][ids[0]], parent)
        self.assertEqual(len(runner.calls), 1)


if __name__ == "__main__":
    unittest.main()
