"""Fixture-only proof of one-item contract correction; no live model or data."""

from copy import deepcopy
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from test_research_flow import FakeRunner, capture, NOW
from product_radar.checklist_repair import repair_checklist_item, REPAIR_SCOPE
from product_radar.models import SafetyError, digest
from product_radar.research_flow import run_flow, candidate_summary
from product_radar.research_memory import ResearchMemory
from product_radar.storage import Journal, load_json, atomic_json


class RejectedReferenceRunner(FakeRunner):
    def __call__(self, role, payload, folder):
        response, usage = super().__call__(role, payload, folder)
        if role == "E" and "checklist_repair" not in payload:
            row = next(row for row in response["results"]
                       if next(item for item in payload["items"] if item["id"] == row["id"])
                       ["description"]["label"] == "Sink caddy")
            row["criteria"][0]["references"] = ["invented-observation"]
        return response, usage


class ChecklistRepairTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.memory = ResearchMemory(self.root / "research", Journal(self.root / "finance"), evidence_type="synthetic")
        self.runner = RejectedReferenceRunner()
        capture(self.memory)
        run_flow(self.memory, self.runner, as_of=NOW)
        state = self.memory.snapshot()
        self.failure_id, self.failure = next((record_id, row) for record_id, row in state["problems"].items()
                                            if row["stage"] == "assessment")
        self.concept_id = self.failure["affected_id"]
        self.job_id = self.failure["job_id"]
        self.args = {"concept_id": self.concept_id, "failure_id": self.failure_id, "job_id": self.job_id}

    def repair(self, runner=None, **changes):
        return repair_checklist_item(self.memory, runner or self.runner, **dict(self.args, **changes))

    def assert_refused_without_model(self, code, **changes):
        before, count = self.memory.snapshot(), len(self.runner.calls)
        with self.assertRaisesRegex(SafetyError, code):
            self.repair(**changes)
        self.assertEqual(self.memory.snapshot(), before)
        self.assertEqual(len(self.runner.calls), count)

    def test_success_only_changes_failed_assessment_and_preserves_original_bytes(self):
        before = self.memory.snapshot()
        original_path = self.memory.root / "ai_jobs" / self.job_id / "result.json"
        original_bytes = original_path.read_bytes()
        result = self.repair()
        after = self.memory.snapshot()
        self.assertTrue(result["applied"])
        self.assertEqual(result["status"], "accepted")
        self.assertNotEqual(result["job_id"], self.job_id)
        self.assertEqual(original_path.read_bytes(), original_bytes)
        for table in before:
            if table not in ("assessments", "jobs"):
                self.assertEqual(after[table], before[table], table)
        for concept_id, assessment in before["assessments"].items():
            self.assertEqual(after["assessments"][concept_id], assessment)
        self.assertEqual(after["jobs"][self.job_id], before["jobs"][self.job_id])
        assessment = after["assessments"][self.concept_id]
        self.assertEqual(assessment["input_hash"], self.failure["revision"])
        self.assertEqual(assessment["repair"]["scope"], REPAIR_SCOPE)
        self.assertEqual(assessment["repair"]["failure_id"], self.failure_id)
        role, payload = self.runner.calls[-1]
        self.assertEqual(role, "E")
        self.assertEqual(len(payload["items"]), 1)
        self.assertEqual(payload["items"][0]["id"], self.concept_id)
        self.assertIn("observations[].id", payload["checklist_repair"]["rules"])
        self.assertIn("do not target a higher score", payload["checklist_repair"]["rules"])
        original_payload = before["jobs"][self.job_id]["request"]["payload"]
        for field in ("criteria", "checklist_scope", "checklist_rules"):
            self.assertEqual(payload[field], original_payload[field])
        self.assertFalse(self.memory.journal.path.exists())

    def test_exact_replay_reuses_without_runner_and_no_new_events(self):
        first = self.repair()
        before, count = self.memory.snapshot(), len(self.runner.calls)
        replay = repair_checklist_item(self.memory, None, **self.args)
        self.assertEqual(replay["job_id"], first["job_id"])
        self.assertFalse(replay["applied"])
        self.assertEqual(self.memory.snapshot(), before)
        self.assertEqual(len(self.runner.calls), count)

    def test_different_runner_identity_cannot_start_a_second_attempt(self):
        first = self.repair()
        other = FakeRunner()
        other.identity = "different-fixture-runner"
        replay = self.repair(other)
        self.assertEqual(replay["job_id"], first["job_id"])
        self.assertFalse(replay["applied"])
        self.assertEqual(other.calls, [])

    def test_no_runner_before_first_repair_does_not_register_a_new_job(self):
        before = self.memory.snapshot()
        with self.assertRaisesRegex(SafetyError, "AI_RUNNER_NOT_ENABLED"):
            repair_checklist_item(self.memory, None, **self.args)
        self.assertEqual(self.memory.snapshot(), before)

    def test_new_dated_copy_reuses_original_observation_reference(self):
        capture(self.memory, acquisition="fixture-repeat", observed=NOW)
        original = next(row for row in self.memory.snapshot()["jobs"][self.job_id]["request"]["payload"]["items"]
                        if row["id"] == self.concept_id)
        self.repair()
        repaired_item = self.runner.calls[-1][1]["items"][0]
        self.assertEqual(repaired_item["observations"], original["observations"])

    def test_ordinary_flow_defers_failure_then_recognizes_recovery(self):
        count = len(self.runner.calls)
        run_flow(self.memory, self.runner, as_of=NOW)
        self.assertEqual(len(self.runner.calls), count)
        self.repair()
        count = len(self.runner.calls)
        summary = run_flow(self.memory, self.runner, as_of=NOW)
        self.assertEqual(len(self.runner.calls), count)
        self.assertEqual(summary["unresolved_issues"], 0)
        self.assertEqual(summary["recovered_issues"], 1)
        self.assertIn(self.failure_id, self.memory.snapshot()["problems"])
        self.assertEqual(candidate_summary(self.memory, as_of=NOW)["problems"][0]["current_status"], "recovered")

    def test_rejected_repair_is_one_saved_attempt_and_cannot_be_repaired_again(self):
        def invalid(role, payload, folder):
            response, usage = self.runner(role, payload, folder)
            response["results"][0]["relevance"][0]["id"] = "invented-observation"
            return response, usage
        first = self.repair(invalid)
        self.assertEqual(first["status"], "rejected")
        self.assertEqual(first["reason"], "AI_RELEVANCE_REFERENCE_INVALID")
        before, count = self.memory.snapshot(), len(self.runner.calls)
        self.assertEqual(self.repair(invalid), first)
        self.assertEqual(self.memory.snapshot(), before)
        self.assertEqual(len(self.runner.calls), count)
        failure_id = next(record_id for record_id, row in before["problems"].items()
                          if row["job_id"] == first["job_id"])
        self.assert_refused_without_model("CHECKLIST_REPAIR_FAILURE_BINDING_INVALID",
                                         failure_id=failure_id, job_id=first["job_id"])

    def test_wrong_concept_failure_job_and_revision_are_rejected(self):
        sibling = next(iter(self.memory.snapshot()["assessments"]))
        for changes in ({"concept_id": sibling}, {"failure_id": "missing-problem"}, {"job_id": "missing-job"}):
            with self.subTest(changes=changes):
                self.assert_refused_without_model("CHECKLIST_REPAIR_FAILURE_BINDING_INVALID", **changes)
        old = self.memory.snapshot()["problems"][self.failure_id]
        self.memory.put("problems", self.failure_id, dict(old, revision="different"), previous=old)
        self.assert_refused_without_model("CHECKLIST_REPAIR_INPUT_CHANGED")

    def test_wrong_problem_stage_reason_or_response_failure_is_rejected(self):
        for changes in ({"stage": "description"}, {"reason": "AI_NETWORK_UNAVAILABLE"},
                        {"reason": "AI_RELEVANCE_REFERENCE_INVALID"}):
            with self.subTest(changes=changes):
                old = self.memory.snapshot()["problems"][self.failure_id]
                self.memory.put("problems", self.failure_id, dict(self.failure, **changes), previous=old)
                self.assert_refused_without_model("CHECKLIST_REPAIR_FAILURE_BINDING_INVALID")
                old = self.memory.snapshot()["problems"][self.failure_id]
                self.memory.put("problems", self.failure_id, self.failure, previous=old,
                                event_id="restore-" + digest(changes)[:20])

    def test_changed_description_is_not_score_improvement_permission(self):
        old = self.memory.snapshot()["concepts"][self.concept_id]
        self.memory.put("concepts", self.concept_id,
                        dict(old, description=dict(old["description"], label="Changed product meaning")), previous=old)
        self.assert_refused_without_model("CHECKLIST_REPAIR_INPUT_CHANGED")

    def test_changed_evidence_association_is_rejected(self):
        old = self.memory.snapshot()["associations"]
        sibling_id = next(record_id for record_id, row in old.items() if row["concept_id"] != self.concept_id)
        self.memory.put("associations", sibling_id, dict(old[sibling_id], concept_id=self.concept_id), previous=old[sibling_id])
        self.assert_refused_without_model("CHECKLIST_REPAIR_INPUT_CHANGED")

    def test_excluded_retired_merged_and_established_are_rejected(self):
        original = self.memory.snapshot()["concepts"][self.concept_id]
        for changes in ({"status": "excluded"}, {"status": "retired"}, {"status": "merged"},
                        {"description": dict(original["description"], brand="established")}):
            with self.subTest(changes=changes):
                old = self.memory.snapshot()["concepts"][self.concept_id]
                self.memory.put("concepts", self.concept_id, dict(original, **changes), previous=old)
                self.assert_refused_without_model("CHECKLIST_REPAIR_CONCEPT_EXCLUDED")

    def test_already_accepted_current_assessment_is_not_reassessed(self):
        original = next(iter(self.memory.snapshot()["assessments"].values()))
        self.memory.put("assessments", self.concept_id, dict(original, input_hash=self.failure["revision"]))
        self.assert_refused_without_model("CHECKLIST_REPAIR_ACCEPTED_RESULT_REUSE_REQUIRED")

    def test_published_and_uploaded_concepts_require_release_review(self):
        self.memory.put("published_history", "past", {"status": "published", "concept_id": self.concept_id})
        self.assert_refused_without_model("CHECKLIST_REPAIR_RELEASE_REVIEW_REQUIRED")
        old = self.memory.snapshot()["published_history"]["past"]
        self.memory.put("published_history", "past", dict(old, status="not-published"), previous=old)
        self.memory.put("issue_reservations", "issue1", {"state": "uploaded", "products": [{"concept_id": self.concept_id}]})
        self.assert_refused_without_model("CHECKLIST_REPAIR_RELEASE_REVIEW_REQUIRED")

    def test_unrelated_publication_and_ordinary_draft_do_not_block_repair(self):
        self.memory.put("published_history", "past", {"status": "published", "concept_id": "unrelated"})
        self.memory.put("issue_reservations", "draft", {"state": "reserved", "products": [{"concept_id": self.concept_id}]})
        before = self.memory.snapshot()
        self.assertTrue(self.repair()["applied"])
        after = self.memory.snapshot()
        self.assertEqual(after["published_history"], before["published_history"])
        self.assertEqual(after["issue_reservations"], before["issue_reservations"])

    def test_rechecks_description_and_publication_before_commit(self):
        def changed(role, payload, folder):
            response, usage = self.runner(role, payload, folder)
            old = self.memory.snapshot()["concepts"][self.concept_id]
            self.memory.put("concepts", self.concept_id, dict(old, status="retired"), previous=old)
            return response, usage
        with self.assertRaisesRegex(SafetyError, "CHECKLIST_REPAIR_CONCEPT_EXCLUDED"):
            self.repair(changed)
        self.assertNotIn(self.concept_id, self.memory.snapshot()["assessments"])

    def test_rechecks_published_history_after_model_wait(self):
        def published(role, payload, folder):
            response, usage = self.runner(role, payload, folder)
            self.memory.put("published_history", "new-publication",
                            {"status": "published", "concept_id": self.concept_id})
            return response, usage
        with self.assertRaisesRegex(SafetyError, "CHECKLIST_REPAIR_RELEASE_REVIEW_REQUIRED"):
            self.repair(published)
        self.assertNotIn(self.concept_id, self.memory.snapshot()["assessments"])

    def test_concurrent_accepted_assessment_is_not_overwritten(self):
        def changed(role, payload, folder):
            response, usage = self.runner(role, payload, folder)
            self.memory.put("assessments", self.concept_id, {"input_hash": "separate-review", "result": {}})
            return response, usage
        with self.assertRaisesRegex(SafetyError, "CHECKLIST_REPAIR_DEPENDENCY_CHANGED"):
            self.repair(changed)
        self.assertEqual(self.memory.snapshot()["assessments"][self.concept_id]["input_hash"], "separate-review")

    def test_saved_output_survives_failed_assessment_commit_and_replays(self):
        real_commit = self.memory.commit
        def fail_commit(event_id, changes, *, validate=None):
            if any(row["table"] == "assessments" for row in changes):
                raise SafetyError("FIXTURE_INTERRUPTED_COMMIT")
            return real_commit(event_id, changes, validate=validate)
        with patch.object(self.memory, "commit", side_effect=fail_commit):
            with self.assertRaisesRegex(SafetyError, "FIXTURE_INTERRUPTED_COMMIT"):
                self.repair()
        count = len(self.runner.calls)
        result = repair_checklist_item(self.memory, None, **self.args)
        self.assertTrue(result["applied"])
        self.assertEqual(len(self.runner.calls), count)

    def test_original_receipt_mismatch_prevents_model_call(self):
        path = self.memory.root / "ai_jobs" / self.job_id / "result.json"
        saved = load_json(path)
        atomic_json(path, dict(saved, request_hash="bad"))
        self.assert_refused_without_model("AI_JOB_RESULT_CONFLICT")

    def test_original_job_without_completed_result_is_not_reexecuted(self):
        path = self.memory.root / "ai_jobs" / self.job_id / "result.json"
        path.rename(path.with_name("fixture-hidden-result.json"))
        self.assert_refused_without_model("SAVED_FILE_UNAVAILABLE")

    def test_job_binding_corruption_is_rejected(self):
        old = self.memory.snapshot()["jobs"][self.job_id]
        self.memory.put("jobs", self.job_id, dict(old, binding_keys={self.concept_id: "bad"}), previous=old)
        self.assert_refused_without_model("CHECKLIST_REPAIR_FAILURE_BINDING_INVALID")

    def test_execution_failure_remains_saved_and_is_not_automatically_retried(self):
        calls = []
        def interrupted(role, payload, folder):
            calls.append(role)
            raise SafetyError("AI_NETWORK_UNAVAILABLE")
        with self.assertRaisesRegex(SafetyError, "AI_NETWORK_UNAVAILABLE"):
            self.repair(interrupted)
        with self.assertRaisesRegex(SafetyError, "AI_JOB_PENDING_RECONCILIATION"):
            self.repair(interrupted)
        self.assertEqual(calls, ["E"])
        self.assertNotIn(self.concept_id, self.memory.snapshot()["assessments"])

    def test_unparseable_contract_envelope_is_saved_not_automatically_retried(self):
        calls = []
        def invalid(role, payload, folder):
            calls.append(role)
            return {"not_results": []}, {"input_tokens": 1, "output_tokens": 1}
        for _ in range(2):
            with self.assertRaisesRegex(SafetyError, "AI_ENVELOPE_INVALID"):
                self.repair(invalid)
        self.assertEqual(calls, ["E"])
        self.assertNotIn(self.concept_id, self.memory.snapshot()["assessments"])


if __name__ == "__main__":
    unittest.main()
