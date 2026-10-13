"""Contract activation replays original owners; fictional disposable inputs only."""

from copy import deepcopy
import unittest
from unittest.mock import patch

import test_fit_review
from test_fit_description import DescriptionRunner
from test_fit_review import FitRunner
from test_research_flow import NOW
from product_radar.fit_description import DESCRIPTION_CONTRACT
from product_radar.fit_review import (PRACTICAL_CONTRACT, _payload,
    prepare_practical_fit_reviews, run_fit_reviews)
from product_radar.models import SafetyError
from product_radar.programme import _fit
from product_radar.research_ai import Jobs, prepare_retry
from product_radar.storage import atomic_json, load_json


class NoCalls(DescriptionRunner):
    identity = "a-different-current-runner"

    def __call__(self, *_args):
        raise AssertionError("Saved-only recovery must not call the current runner")


class RejectedRunner(FitRunner):
    def __call__(self, role, payload, folder):
        response, usage = super().__call__(role, payload, folder)
        response["results"][0]["criteria"][0]["references"] = ["invented-observation"]
        return response, usage


class ProgrammeFitRecoveryTests(unittest.TestCase):
    setUp = test_fit_review.FitReviewTests.setUp

    def prepare_v2(self, ids=None):
        return prepare_practical_fit_reviews(self.memory, concept_ids=ids or [self.concept_id], as_of=NOW)

    def stage(self, runner=None, ids=None):
        record = {"concept_ids": ids or [self.concept_id], "deferrals": {}}
        result = _fit(self.memory, record, {"fit_limit": 5, "runner": runner or NoCalls(),
            "at": NOW, "retry_seconds": 60})
        return result, record

    def unfinished_answer(self, ids=None, runner=None):
        rows = self.prepare_v2(ids)
        runner = runner or FitRunner()
        runner.identity = "original-v2-runner"
        with patch("product_radar.fit_review.apply_fit_review", side_effect=SafetyError("SAVED_FILE_UNAVAILABLE")):
            results = run_fit_reviews(self.memory, runner, [row["review_id"] for row in rows])
        self.assertTrue(all(row["status"] == "needs_reconciliation" for row in results))
        return rows, runner

    def v2_job(self, review_id):
        state = self.memory.snapshot()
        return next((jid, value) for jid, value in state["jobs"].items()
            if any(item.get("fit_review_base_id") == review_id
                for item in value["request"]["payload"]["items"]))

    def test_saved_valid_v2_answer_is_applied_before_v3_without_new_calls_or_mutation(self):
        rows, original = self.unfinished_answer()
        job_id, _ = self.v2_job(rows[0]["review_id"])
        saved = self.memory.root / "ai_jobs" / job_id / "result.json"
        raw, jobs = saved.read_bytes(), deepcopy(self.memory.snapshot()["jobs"])
        result, record = self.stage()
        self.assertEqual(result["saved_v2_accepted_recovered"], 1)
        self.assertEqual(result["prepared"], 0)
        self.assertEqual(record["deferrals"], {})
        self.assertEqual(self.memory.snapshot()["assessments"][self.concept_id]["contract_version"], PRACTICAL_CONTRACT)
        self.assertEqual(self.memory.snapshot()["jobs"], jobs)
        self.assertEqual(saved.read_bytes(), raw)
        self.assertEqual(len(original.calls), 1)
        before = self.memory.snapshot()
        self.assertEqual(self.stage()[0]["current_accepted_reused"], 1)
        self.assertEqual(self.memory.snapshot(), before)

    def test_saved_completion_without_result_recovers_original_identity(self):
        rows, _ = self.unfinished_answer()
        job_id, _ = self.v2_job(rows[0]["review_id"])
        folder = self.memory.root / "ai_jobs" / job_id
        saved = load_json(folder / "result.json")
        atomic_json(folder / "completion.json", {"response": saved["response"], "usage": saved["usage"], "exit_code": 0})
        (folder / "result.json").unlink()  # Disposable manufactured interruption only.
        self.assertEqual(self.stage()[0]["saved_v2_accepted_recovered"], 1)
        self.assertTrue((folder / "result.json").exists())
        self.assertEqual(self.memory.snapshot()["assessments"][self.concept_id]["job_id"], job_id)

    def test_uncertain_original_remains_pending_and_preserves_old_intent(self):
        rows = self.prepare_v2()
        class Uncertain(FitRunner):
            def __call__(self, *_args):
                raise SafetyError("AI_NETWORK_UNAVAILABLE")
        self.assertEqual(run_fit_reviews(self.memory, Uncertain(), [rows[0]["review_id"]])[0]["status"], "pending")
        before = deepcopy(self.memory.snapshot()["jobs"])
        result, record = self.stage()
        self.assertEqual(result["prepared"], 0)
        self.assertEqual(result["outcomes"][0]["reason"], "AI_JOB_PENDING_RECONCILIATION")
        self.assertEqual(self.memory.snapshot()["jobs"], before)
        self.assertEqual(record["deferrals"]["fit:" + self.concept_id]["reason"], "AI_JOB_PENDING_RECONCILIATION")
        self.assertFalse(any(row.get("binding", {}).get("contract_version") == DESCRIPTION_CONTRACT
            for row in self.memory.snapshot()["source_checks"].values()))

    def test_uncertain_original_batch_is_not_misread_as_completed_rejection(self):
        ids = list(self.memory.snapshot()["concepts"])
        rows = self.prepare_v2(ids)
        class Uncertain(FitRunner):
            def __call__(self, *_args):
                raise SafetyError("AI_NETWORK_UNAVAILABLE")
        run_fit_reviews(self.memory, Uncertain(), [row["review_id"] for row in rows])
        result, record = self.stage(ids=ids)
        self.assertEqual(result["prepared"], 0)
        self.assertEqual({row["status"] for row in result["outcomes"]}, {"pending"})
        self.assertEqual(len(record["deferrals"]), len(ids))

    def test_empty_prepared_retry_does_not_erase_a_sent_original_or_gain_an_intent(self):
        rows = self.prepare_v2()
        class Uncertain(FitRunner):
            def __call__(self, *_args):
                raise SafetyError("AI_NETWORK_UNAVAILABLE")
        run_fit_reviews(self.memory, Uncertain(), [rows[0]["review_id"]])
        job_id, _ = self.v2_job(rows[0]["review_id"])
        prepare_retry(self.memory, job_id, "Synthetic deliberate retry preparation only")
        retry_intent = self.memory.root / "ai_jobs" / job_id / "retry-1" / "intent.json"
        before = deepcopy(self.memory.snapshot()["jobs"])
        result, record = self.stage()
        self.assertEqual(result["prepared"], 0)
        self.assertEqual(result["outcomes"][0]["reason"], "AI_JOB_PENDING_RECONCILIATION")
        self.assertIn("fit:" + self.concept_id, record["deferrals"])
        self.assertEqual(self.memory.snapshot()["jobs"], before)
        self.assertFalse(retry_intent.exists())

    def test_abandoned_base_does_not_block_new_explicit_contract(self):
        rows = self.prepare_v2(); old = rows[0]["review_id"]
        runner = DescriptionRunner(); result, _ = self.stage(runner)
        self.assertEqual(result["prepared"], 1)
        self.assertEqual(len(runner.calls), 1)
        self.assertEqual(self.memory.snapshot()["assessments"][self.concept_id]["contract_version"], DESCRIPTION_CONTRACT)
        self.assertIn(old, self.memory.snapshot()["source_checks"])

    def test_prepared_job_without_dispatch_does_not_block_or_gain_an_intent(self):
        rows = self.prepare_v2(); base = self.memory.snapshot()["source_checks"][rows[0]["review_id"]]
        with self.assertRaisesRegex(SafetyError, "AI_RUNNER_NOT_ENABLED"):
            Jobs(self.memory, None).run("E", _payload(base), declared_version=PRACTICAL_CONTRACT)
        job_id, _ = self.v2_job(base["reference"])
        folder = self.memory.root / "ai_jobs" / job_id
        self.assertFalse((folder / "intent.json").exists())
        runner = DescriptionRunner(); result, _ = self.stage(runner)
        self.assertEqual(result["prepared"], 1)
        self.assertEqual(len(runner.calls), 1)
        self.assertFalse((folder / "intent.json").exists())

    def test_completed_rejected_v2_without_sent_repair_can_upgrade_once(self):
        rows = self.prepare_v2(); bad = RejectedRunner()
        result = run_fit_reviews(self.memory, bad, [rows[0]["review_id"]], repair_rejected=False)
        self.assertEqual(result[0]["status"], "rejected")
        runner = DescriptionRunner(); result, _ = self.stage(runner)
        self.assertEqual([row["status"] for row in result["outcomes"]], ["rejected", "accepted"])
        self.assertEqual(len(runner.calls), 1)
        self.stage(runner)
        self.assertEqual(len(runner.calls), 1)

    def test_exhausted_saved_v2_repair_replays_before_upgrade_once(self):
        rows = self.prepare_v2(); bad = RejectedRunner()
        self.assertEqual(run_fit_reviews(self.memory, bad, [rows[0]["review_id"]])[0]["status"], "rejected")
        self.assertEqual(len(bad.calls), 2)
        raw = {path: path.read_bytes() for path in (self.memory.root / "ai_jobs").rglob("result.json")}
        runner = DescriptionRunner(); result, _ = self.stage(runner)
        self.assertEqual(result["outcomes"][0]["status"], "rejected")
        self.assertIn("original_job_id", result["outcomes"][0])
        self.assertEqual(len(runner.calls), 1)
        self.assertTrue(all(path.read_bytes() == value for path, value in raw.items()))
        self.stage(runner)
        self.assertEqual(len(runner.calls), 1)

    def test_saved_valid_repair_application_gap_recovers_without_new_v3(self):
        class RepairRunner(FitRunner):
            def __call__(self, role, payload, folder):
                response, usage = super().__call__(role, payload, folder)
                if not payload.get("checklist_repair"):
                    response["results"][0]["criteria"][0]["references"] = ["invented-observation"]
                return response, usage
        rows, original = self.unfinished_answer(runner=RepairRunner())
        self.assertEqual(len(original.calls), 2)
        result, record = self.stage()
        self.assertEqual(result["saved_v2_accepted_recovered"], 1)
        self.assertEqual(result["prepared"], 0)
        self.assertEqual(record["deferrals"], {})
        self.assertIn("original_job_id", result["outcomes"][0])

    def test_uncertain_sent_repair_cannot_be_bypassed_with_v3(self):
        class RepairUncertain(RejectedRunner):
            def __call__(self, role, payload, folder):
                if payload.get("checklist_repair"):
                    raise SafetyError("AI_NETWORK_UNAVAILABLE")
                return super().__call__(role, payload, folder)
        rows = self.prepare_v2()
        run_fit_reviews(self.memory, RepairUncertain(), [rows[0]["review_id"]])
        jobs = deepcopy(self.memory.snapshot()["jobs"])
        result, record = self.stage()
        self.assertEqual(result["prepared"], 0)
        self.assertEqual(result["outcomes"][0]["reason"], "AI_JOB_PENDING_RECONCILIATION")
        self.assertIn("fit:" + self.concept_id, record["deferrals"])
        self.assertEqual(self.memory.snapshot()["jobs"], jobs)

    def test_empty_prepared_correction_retry_remains_pending_without_new_intent(self):
        class RepairUncertain(RejectedRunner):
            def __call__(self, role, payload, folder):
                if payload.get("checklist_repair"):
                    raise SafetyError("AI_NETWORK_UNAVAILABLE")
                return super().__call__(role, payload, folder)
        rows = self.prepare_v2()
        run_fit_reviews(self.memory, RepairUncertain(), [rows[0]["review_id"]])
        repair_id = next(jid for jid, value in self.memory.snapshot()["jobs"].items()
            if value["request"]["payload"].get("checklist_repair"))
        prepare_retry(self.memory, repair_id, "Synthetic deliberately prepared correction retry only")
        retry_intent = self.memory.root / "ai_jobs" / repair_id / "retry-1" / "intent.json"
        result, record = self.stage()
        self.assertEqual(result["prepared"], 0)
        self.assertEqual(result["outcomes"][0]["reason"], "AI_JOB_PENDING_RECONCILIATION")
        self.assertIn("fit:" + self.concept_id, record["deferrals"])
        self.assertFalse(retry_intent.exists())

    def test_unstarted_saved_correction_does_not_gain_intent_or_block_v3(self):
        rows = self.prepare_v2(); original_run = Jobs.run
        def stop_before_dispatch(jobs, role, payload, **options):
            if payload.get("checklist_repair"):
                return original_run(Jobs(jobs.memory, None), role, payload, **options)
            return original_run(jobs, role, payload, **options)
        with patch.object(Jobs, "run", new=stop_before_dispatch):
            run_fit_reviews(self.memory, RejectedRunner(), [rows[0]["review_id"]])
        repair_id = next(jid for jid, value in self.memory.snapshot()["jobs"].items()
            if value["request"]["payload"].get("checklist_repair"))
        intent = self.memory.root / "ai_jobs" / repair_id / "intent.json"
        self.assertFalse(intent.exists())
        runner = DescriptionRunner(); result, _ = self.stage(runner)
        self.assertEqual(result["prepared"], 1)
        self.assertEqual(len(runner.calls), 1)
        self.assertFalse(intent.exists())

    def test_tampered_original_result_stays_pending_without_new_contract(self):
        rows, _ = self.unfinished_answer()
        job_id, _ = self.v2_job(rows[0]["review_id"])
        path = self.memory.root / "ai_jobs" / job_id / "result.json"
        bad = load_json(path); bad["request_hash"] = "a" * 64
        atomic_json(path, bad)
        result, record = self.stage()
        self.assertEqual(result["prepared"], 0)
        self.assertEqual(result["outcomes"][0]["reason"], "AI_JOB_RESULT_CONFLICT")
        self.assertEqual(record["deferrals"]["fit:" + self.concept_id]["reason"], "AI_JOB_RESULT_CONFLICT")

    def test_batch_integrity_failure_is_not_a_genuine_output_rejection(self):
        ids = list(self.memory.snapshot()["concepts"])
        rows, _ = self.unfinished_answer(ids)
        job_id, _ = self.v2_job(rows[0]["review_id"])
        path = self.memory.root / "ai_jobs" / job_id / "result.json"
        bad = load_json(path); bad["request_hash"] = "a" * 64
        atomic_json(path, bad)
        result, record = self.stage(ids=ids)
        self.assertEqual(result["prepared"], 0)
        self.assertEqual({row["reason"] for row in result["outcomes"]}, {"AI_JOB_RESULT_CONFLICT"})
        self.assertEqual(len(record["deferrals"]), len(ids))
        self.assertFalse(any(row.get("binding", {}).get("contract_version") == DESCRIPTION_CONTRACT
            for row in self.memory.snapshot()["source_checks"].values()))

    def test_global_symlink_integrity_failure_is_not_softened(self):
        rows, _ = self.unfinished_answer()
        job_id, _ = self.v2_job(rows[0]["review_id"])
        folder = self.memory.root / "ai_jobs" / job_id
        saved = folder / "result.json"
        target = folder / "original-result.json"
        saved.rename(target)
        saved.symlink_to(target.name)
        # A dispatch intent must not hide a conflicting result-file symlink.
        with self.assertRaisesRegex(SafetyError, "SYMLINK_PATH_REFUSED"):
            self.stage()

    def test_accepted_current_v3_keeps_original_result_without_old_v2_replay(self):
        self.prepare_v2()
        runner = DescriptionRunner(); self.stage(runner)
        before = self.memory.snapshot()
        result, _ = self.stage()
        self.assertEqual(result["current_accepted_reused"], 1)
        self.assertEqual(result["prepared"], 0)
        self.assertEqual(self.memory.snapshot(), before)

    def test_unrelated_source_metadata_does_not_enter_fit_base_validation(self):
        self.memory.put("source_checks", "unrelated-capture-receipt",
                        {"kind": "unrelated_historical_receipt", "binding": "a source-specific binding"})
        runner = DescriptionRunner(); result, _ = self.stage(runner)
        self.assertEqual(result["prepared"], 1)
        self.assertEqual(len(runner.calls), 1)

    def test_model_route_paused_still_allows_original_saved_answer_recovery(self):
        self.unfinished_answer()
        record = {"concept_ids": [self.concept_id], "deferrals": {}}
        result = _fit(self.memory, record, {"fit_limit": 5, "runner": None,
            "at": NOW, "retry_seconds": 60})
        self.assertEqual(result["saved_v2_accepted_recovered"], 1)
        self.assertEqual(result["prepared"], 0)
        self.assertEqual(record["deferrals"], {})

    def test_genuinely_changed_description_does_not_replay_old_answer(self):
        rows, _ = self.unfinished_answer()
        original = self.memory.snapshot()["concepts"][self.concept_id]
        changed = deepcopy(original); changed["description"]["function"] = "A genuinely different fictional function"
        self.memory.put("concepts", self.concept_id, changed, previous=original)
        runner = DescriptionRunner(); result, _ = self.stage(runner)
        self.assertEqual(result["saved_v2_accepted_recovered"], 0)
        self.assertEqual(len(runner.calls), 1)
        self.assertEqual(self.memory.snapshot()["assessments"][self.concept_id]["contract_version"], DESCRIPTION_CONTRACT)

    def test_batch_recovery_preserves_original_job_for_each_product(self):
        ids = list(self.memory.snapshot()["concepts"])
        rows, original = self.unfinished_answer(ids)
        original_jobs = set(self.memory.snapshot()["jobs"])
        result, _ = self.stage(ids=ids)
        self.assertEqual(result["saved_v2_accepted_recovered"], len(ids))
        self.assertEqual(result["prepared"], 0)
        self.assertEqual(set(self.memory.snapshot()["jobs"]), original_jobs)
        self.assertEqual(len(original.calls), 1)
        self.assertEqual(len({self.memory.snapshot()["assessments"][cid]["job_id"] for cid in ids}), 1)

    def test_local_replay_failure_keeps_original_pending_and_healthy_sibling_continues(self):
        self.unfinished_answer()
        ids = list(self.memory.snapshot()["concepts"])
        original_run = run_fit_reviews
        def local_failure(memory, runner, review_ids, **options):
            if runner is None:
                raise OSError("Fictional local read failure")
            return original_run(memory, runner, review_ids, **options)
        runner = DescriptionRunner()
        with patch("product_radar.fit_review.run_fit_reviews", side_effect=local_failure):
            result, record = self.stage(runner, ids)
        self.assertEqual(result["prepared"], 1)
        self.assertEqual(len(runner.calls), 1)
        self.assertEqual(record["deferrals"]["fit:" + self.concept_id]["reason"], "SAVED_FILE_UNAVAILABLE")
        self.assertEqual([row["status"] for row in result["outcomes"]], ["needs_reconciliation", "accepted"])
        self.assertEqual(result["remaining_to_prepare"], 0)


if __name__ == "__main__":
    unittest.main()
