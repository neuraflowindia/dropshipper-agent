"""Owned narration-file failures preserve completed work; no real AI calls."""

from copy import deepcopy
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from product_radar.models import SafetyError
from product_radar.report_narration import generate_report_narratives
import product_radar.report_narration as narration
from product_radar.research_ai import Jobs, MODEL, _item_bindings, prompt_version
from product_radar.research_memory import ResearchMemory, key
from product_radar.storage import Journal, load_json
from product_radar.work_turns import (DEFAULT_POLICY, admit_model_job, bind_work_turn,
    close_work_turn, open_work_turn)
from test_live_outputs import fixture
from test_report_narration import NarrationRunner, RomanCorrectionRunner
from test_functional_report_packet import RecordingWriter, functional_packet_fixture
from test_fit_pipeline import WritingFixture
from test_work_turns import Runner as AllowanceFiller
import test_report_workflow as workflow_fixtures


LOCAL_FAILURE = "REPORT_NARRATION_LOCAL_FILE_FAILED"


class NarrationFileRecoveryTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        root = Path(temporary.name)
        self.memory = ResearchMemory(root / "research", Journal(root / "finance"), evidence_type="synthetic")
        self.issue, self.dataset = fixture()
        self.dataset["narratives"] = {}
        self.runner = NarrationRunner(memory=self.memory)

    def narrate(self, **kwargs):
        return generate_report_narratives(self.memory, self.issue, self.dataset, self.runner, **kwargs)

    def completed_then_unreadable(self):
        original = Jobs.run
        returned = []
        def run(jobs, role, payload):
            result = original(jobs, role, payload)
            if payload["language"] == "hi-Latn" and not returned:
                returned.append(result[0])
                raise OSError("Sensitive local path/account details must not appear in output")
            return result
        return run, returned

    def test_late_hinglish_file_failure_keeps_english_and_other_saved_chunks(self):
        original = deepcopy(self.dataset)
        run, returned = self.completed_then_unreadable()
        with patch.object(Jobs, "run", new=run):
            result = self.narrate()
        self.assertEqual(result["complete_languages"], ["en"])
        self.assertEqual(set(result["dataset"]["narratives"]), {"en"})
        self.assertEqual(set(result["failures"]["hi-Latn"].values()), {LOCAL_FAILURE})
        self.assertEqual(len(self.runner.calls), 6)
        self.assertEqual(len(returned), 1)
        self.assertTrue((self.memory.root / "ai_jobs" / returned[0] / "result.json").is_file())
        self.assertNotIn("Sensitive local", str(result))
        self.assertEqual(self.dataset, original)
        self.assertFalse(result["client_ready"])

    def test_saved_success_after_local_failure_replays_without_another_model_request(self):
        run, _ = self.completed_then_unreadable()
        with patch.object(Jobs, "run", new=run):
            first = self.narrate()
        calls = len(self.runner.calls)
        second = self.narrate(chunk_size=3)
        self.assertEqual(first["complete_languages"], ["en"])
        self.assertEqual(second["complete_languages"], ["en", "hi-Latn"])
        self.assertEqual(second["failures"], {})
        self.assertEqual(len(self.runner.calls), calls)
        self.assertEqual(second["dataset"]["narratives"]["en"], first["dataset"]["narratives"]["en"])

    def test_typed_chunk_file_failure_remains_local_and_later_chunks_continue(self):
        original = Jobs.run
        failed = []
        def run(jobs, role, payload):
            if payload["language"] == "hi-Latn" and not failed:
                failed.append(True)
                raise SafetyError("SAVED_FILE_UNAVAILABLE")
            return original(jobs, role, payload)
        with patch.object(Jobs, "run", new=run):
            result = self.narrate()
        self.assertEqual(result["complete_languages"], ["en"])
        self.assertEqual(set(result["failures"]["hi-Latn"].values()), {"SAVED_FILE_UNAVAILABLE"})
        self.assertEqual(len([p for p in self.runner.calls if p["language"] == "hi-Latn"]), 2)

    def test_hinglish_saved_slot_read_failure_does_not_discard_english(self):
        original = narration._completed_items
        def completed(memory, context, items, **kwargs):
            if context["language"] == "hi-Latn":
                raise PermissionError("Private file path must not be exposed")
            return original(memory, context, items, **kwargs)
        with patch.object(narration, "_completed_items", side_effect=completed):
            first = self.narrate()
        self.assertEqual(first["complete_languages"], ["en"])
        self.assertEqual(first["failures"], {"hi-Latn": {"edition": LOCAL_FAILURE}})
        self.assertTrue(all(payload["language"] == "en" for payload in self.runner.calls))
        english_calls = len(self.runner.calls)
        second = self.narrate()
        self.assertEqual(second["complete_languages"], ["en", "hi-Latn"])
        self.assertEqual(len([p for p in self.runner.calls if p["language"] == "en"]), english_calls)

    def test_problem_record_file_failure_is_saved_in_status_not_thrown_over_english(self):
        run, _ = self.completed_then_unreadable()
        with patch.object(Jobs, "run", new=run), \
             patch.object(narration, "problem", side_effect=OSError("Hidden diagnostic path")):
            result = self.narrate()
        self.assertEqual(result["complete_languages"], ["en"])
        self.assertEqual(set(result["problem_save_failures"]), {"hi-Latn"})
        self.assertEqual(set(result["problem_save_failures"]["hi-Latn"]), set(result["failures"]["hi-Latn"]))
        self.assertEqual(set(result["problem_save_failures"]["hi-Latn"].values()), {LOCAL_FAILURE})
        self.assertNotIn("Hidden diagnostic", str(result))

    def test_local_correction_file_failure_preserves_other_slots_then_one_repair_resumes(self):
        self.runner = RomanCorrectionRunner(self.dataset["products"][0]["concept_id"])
        with patch.object(narration, "_correct_roman_item", side_effect=OSError("Hidden correction path")):
            first = self.narrate()
        calls = len(self.runner.calls)
        self.assertEqual(first["complete_languages"], ["en"])
        self.assertEqual(first["failures"]["hi-Latn"], {self.dataset["products"][0]["concept_id"]: LOCAL_FAILURE})
        second = self.narrate(chunk_size=3)
        self.assertEqual(second["complete_languages"], ["en", "hi-Latn"])
        self.assertEqual(len(self.runner.calls), calls + 1)
        self.assertIn("narration_repair", self.runner.calls[-1])

    def test_store_integrity_error_in_chunk_propagates_instead_of_becoming_file_failure(self):
        with patch.object(Jobs, "run", side_effect=SafetyError("RESEARCH_EVENTS_INVALID")):
            with self.assertRaisesRegex(SafetyError, "RESEARCH_EVENTS_INVALID"):
                self.narrate()
        self.assertEqual(self.runner.calls, [])

    def test_context_integrity_error_keeps_original_code(self):
        with patch.object(narration, "_has_contract_context", side_effect=SafetyError("AI_JOB_RESULT_CONFLICT")):
            with self.assertRaisesRegex(SafetyError, "AI_JOB_RESULT_CONFLICT"):
                self.narrate()
        self.assertEqual(self.runner.calls, [])

    def test_programming_error_is_not_mislabelled_as_a_file_problem(self):
        with patch.object(Jobs, "run", side_effect=KeyError("unexpected-contract-field")):
            with self.assertRaises(KeyError):
                self.narrate()

    def test_unreadable_context_keeps_previously_checked_edition_without_new_intent(self):
        first = self.narrate(languages=["en"])
        self.dataset = first["dataset"]
        calls = len(self.runner.calls)
        jobs = deepcopy(self.memory.snapshot()["jobs"])
        with patch.object(narration, "_has_contract_context", side_effect=OSError("Hidden context path")):
            result = self.narrate()
        self.assertEqual(result["complete_languages"], ["en"])
        self.assertEqual(result["failures"], {"hi-Latn": {"edition": LOCAL_FAILURE}})
        self.assertEqual(len(self.runner.calls), calls)
        self.assertEqual(self.memory.snapshot()["jobs"], jobs)


class WorkflowNarrationFileRecoveryTests(unittest.TestCase):
    setUp = workflow_fixtures.WorkflowTests.setUp
    render = workflow_fixtures.WorkflowTests.render
    run_package = workflow_fixtures.WorkflowTests.run_package
    data = workflow_fixtures.WorkflowTests.data

    def test_healthy_english_render_is_retained_while_hinglish_file_work_waits(self):
        original = Jobs.run
        returned = []
        def run(jobs, role, payload):
            result = original(jobs, role, payload)
            if payload["language"] == "hi-Latn" and not returned:
                returned.append(result[0])
                raise OSError("Hidden late Hinglish local-file failure")
            return result
        with patch.object(Jobs, "run", new=run), \
             patch.object(narration, "problem", side_effect=OSError("Hidden optional problem save")):
            first = self.run_package()
        stages = first["stage_outcomes"]
        self.assertEqual(first["state"], "narration_pending")
        self.assertEqual(stages["writing"]["complete_languages"], ["en"])
        self.assertEqual(stages["editions"]["en"]["state"], "completed")
        self.assertEqual(stages["editions"]["hi-Latn"]["state"], "waiting")
        self.assertTrue(stages["writing"]["problem_save_failures"]["hi-Latn"])
        self.assertEqual(len(self.renders), 1)
        saved = load_json(self.data / "package-status.json")
        self.assertEqual(saved["stage_outcomes"]["writing"], stages["writing"])
        calls = len(self.runner.calls)
        second = self.run_package()
        self.assertEqual(second["state"], "draft_ready_for_review")
        self.assertEqual(len(self.runner.calls), calls)
        self.assertEqual(len(self.renders), 2)
        self.assertFalse(second["client_ready"])


class FunctionalNarrationAllowanceTests(unittest.TestCase):
    """Real isolated storage/admission paths with synthetic writing, no providers."""

    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.memory, _, self.issue, self.dataset = functional_packet_fixture(self.root)
        self.payload = narration.make_narrative_input(self.dataset, "en")
        self.runner = RecordingWriter()

    def open(self, *, limit=1, at="2026-10-11T10:00:00+00:00"):
        policy = deepcopy(DEFAULT_POLICY)
        policy.update(model_executions=limit, report_slots=0)
        return open_work_turn(self.memory, "synthetic-report-allowance", at=at, policy=policy)

    def consume(self):
        Jobs(self.memory, AllowanceFiller()).run("A", {"items": [{
            "id": "synthetic-allowance-filler", "source_text": {"title": "Synthetic allowance filler"}}]})

    def narrate(self, runner=None):
        return generate_report_narratives(self.memory, self.issue, self.dataset,
            self.runner if runner is None else runner, languages=["en"])

    def request(self, supplied, runner=None):
        runner = self.runner if runner is None else runner
        version = prompt_version("G", supplied)
        request = {"role": "G", "payload": supplied, "model": MODEL,
            "prompt_version": version, "runner": runner.identity}
        return key("job", request), request, _item_bindings("G", supplied, MODEL, version, runner.identity)

    def test_all_fresh_spent_has_zero_wrapper_or_problem_writes(self):
        turn = self.open()
        with bind_work_turn(self.memory, turn["id"]):
            self.consume()
            before = self.memory.snapshot_with_provenance()
            with patch.object(narration, "run_current_model_job") as wrapper, \
                 patch.object(narration, "problem") as problem, \
                 patch.object(self.memory, "snapshot", wraps=self.memory.snapshot) as snapshot:
                result = self.narrate()
            wrapper.assert_not_called()
            problem.assert_not_called()
            self.assertEqual(snapshot.call_count, 1)
            self.assertEqual(self.memory.snapshot_with_provenance(), before)
        self.assertEqual(self.runner.calls, 0)
        self.assertEqual(result["writing_item_refs"], {"en": {}})
        self.assertEqual(len(result["failures"]["en"]), 11)
        self.assertEqual(set(result["failures"]["en"].values()), {"WORK_TURN_MODEL_LIMIT"})
        self.assertEqual(self.memory.snapshot()["batch_runs"][turn["id"]]["counters"]["model_executions"], 1)

    def test_mixed_saved_foreign_runner_rows_keep_provenance_and_wait_only_fresh(self):
        original = RecordingWriter()
        original.identity = "synthetic-original-report-owner"
        supplied = dict(self.payload, items=self.payload["items"][:5])
        job_id, _ = Jobs(self.memory, original).run("G", supplied)
        turn = self.open()
        with bind_work_turn(self.memory, turn["id"]):
            self.consume()
            before_jobs = deepcopy(self.memory.snapshot()["jobs"])
            with patch.object(narration, "run_current_model_job") as wrapper, \
                 patch.object(narration, "problem") as problem:
                result = self.narrate()
            wrapper.assert_not_called()
            problem.assert_not_called()
        refs = result["writing_item_refs"]["en"]
        self.assertEqual(set(refs), {item["id"] for item in supplied["items"]})
        self.assertEqual({ref["job_id"] for ref in refs.values()}, {job_id})
        self.assertEqual(self.memory.snapshot()["jobs"], before_jobs)
        self.assertEqual(self.memory.snapshot()["jobs"][job_id]["request"]["runner"], original.identity)
        self.assertEqual(len(result["failures"]["en"]), 6)
        self.assertEqual(set(result["failures"]["en"].values()), {"WORK_TURN_MODEL_LIMIT"})
        self.assertEqual((original.calls, self.runner.calls), (1, 0))

    def test_next_genuine_turn_finishes_without_resetting_or_rewriting_saved_rows(self):
        supplied = dict(self.payload, items=self.payload["items"][:5])
        old_job_id, _ = Jobs(self.memory, self.runner).run("G", supplied)
        first = self.open()
        with bind_work_turn(self.memory, first["id"]):
            self.consume()
            waiting = self.narrate()
        self.assertEqual(len(waiting["failures"]["en"]), 6)
        first_state = deepcopy(self.memory.snapshot()["batch_runs"][first["id"]])
        close_work_turn(self.memory, first["id"], at="2026-10-11T10:15:00+00:00",
            reason="Synthetic finite turn complete.")
        following = self.open(limit=10, at="2026-10-11T10:15:00+00:00")
        with bind_work_turn(self.memory, following["id"]):
            result = self.narrate()
        self.assertEqual(result["failures"], {})
        self.assertEqual(result["complete_languages"], ["en"])
        self.assertEqual(self.runner.calls, 3)
        state = self.memory.snapshot()
        self.assertEqual(state["batch_runs"][first["id"]]["counters"], first_state["counters"])
        self.assertEqual(state["batch_runs"][following["id"]]["counters"]["model_executions"], 2)
        self.assertEqual(following["turn_sequence"], first["turn_sequence"] + 1)
        self.assertTrue(all(result["writing_item_refs"]["en"][item["id"]]["job_id"] == old_job_id
            for item in supplied["items"]))

    def test_current_unstarted_original_at_ceiling_survives_resized_chunks(self):
        existing_jobs = set(self.memory.snapshot()["jobs"])
        turn = self.open()
        supplied = dict(self.payload, items=self.payload["items"][:8])
        job_id, request, bindings = self.request(supplied)
        with bind_work_turn(self.memory, turn["id"]):
            admit_model_job(self.memory, job_id, request, bindings, attempt="original", at=turn["opened_at"])
            self.assertFalse((self.memory.root / "ai_jobs" / job_id / "intent.json").exists())
            with patch.object(narration, "problem") as problem:
                result = self.narrate()
            problem.assert_not_called()
        self.assertEqual(self.runner.calls, 1)
        self.assertEqual(len(self.runner.payloads[0]["items"]), 8)
        self.assertEqual(len(result["writing_item_refs"]["en"]), 8)
        self.assertEqual({ref["job_id"] for ref in result["writing_item_refs"]["en"].values()}, {job_id})
        self.assertEqual(len(result["failures"]["en"]), 3)
        self.assertEqual(set(result["failures"]["en"].values()), {"WORK_TURN_MODEL_LIMIT"})
        state = self.memory.snapshot()
        self.assertEqual(set(state["jobs"]) - existing_jobs, {job_id})
        self.assertEqual(state["batch_runs"][turn["id"]]["counters"]["model_executions"], 1)

    def test_real_invalid_answer_keeps_actual_reason_when_correction_waits(self):
        bad_id = self.dataset["products"][0]["concept_id"]
        self.runner = RecordingWriter(bad_id)
        turn = self.open()
        real_problem = narration.problem
        with bind_work_turn(self.memory, turn["id"]), \
             patch.object(narration, "problem", wraps=real_problem) as problem:
            result = self.narrate()
        self.assertEqual(self.runner.calls, 1)
        self.assertEqual(result["failures"]["en"][bad_id], "WORK_TURN_MODEL_LIMIT")
        self.assertNotIn(bad_id, result["writing_item_refs"]["en"])
        self.assertEqual(len(result["writing_item_refs"]["en"]), 4)
        self.assertEqual(problem.call_count, 1)
        self.assertEqual(problem.call_args.args[1:4],
            ("report_narration", bad_id, "AI_REPORT_FACT_REFERENCE_INVALID"))
        self.assertTrue(any((self.memory.root / "ai_jobs" / job_id / "result.json").exists()
            for job_id in self.memory.snapshot()["jobs"]))

    def test_exact_original_binding_conflict_is_not_hidden_by_fresh_wait(self):
        supplied = dict(self.payload, items=self.payload["items"][:1])
        job_id, request, bindings = self.request(supplied)
        bindings[supplied["items"][0]["id"]] = "0" * 64
        self.memory.put("jobs", job_id, {"request": request, "binding_keys": bindings})
        turn = self.open()
        with bind_work_turn(self.memory, turn["id"]):
            self.consume()
            before = self.memory.snapshot_with_provenance()
            with patch.object(narration, "run_current_model_job") as wrapper, \
                 patch.object(narration, "problem") as problem:
                with self.assertRaisesRegex(SafetyError, "REPORT_NARRATION_SOURCE_JOB_CONFLICT"):
                    self.narrate()
            wrapper.assert_not_called()
            problem.assert_not_called()
            self.assertEqual(self.memory.snapshot_with_provenance(), before)

    def test_saved_finite_correction_reuses_original_foreign_owner(self):
        bad_id = self.dataset["products"][0]["concept_id"]
        class RepairOnceWriter(WritingFixture):
            identity = "synthetic-finite-original-owner"
            def __call__(self, role, payload, folder):
                response, usage = super().__call__(role, payload, folder)
                if "narration_repair" not in payload:
                    for row in response["results"]:
                        if row["id"] == bad_id:
                            row["references"] = ["invented-slot"]
                return response, usage
        original = RepairOnceWriter()
        first = self.narrate(original)
        self.assertEqual(first["failures"], {})
        original_ref = first["writing_item_refs"]["en"][bad_id]
        self.assertIn("narration_repair", self.memory.snapshot()["jobs"][original_ref["job_id"]]["request"]["payload"])
        turn = self.open()
        with bind_work_turn(self.memory, turn["id"]):
            self.consume()
            before_jobs = deepcopy(self.memory.snapshot()["jobs"])
            second = self.narrate()
        self.assertEqual(second["failures"], {})
        self.assertEqual(second["writing_item_refs"]["en"][bad_id], original_ref)
        self.assertEqual(self.memory.snapshot()["jobs"], before_jobs)
        self.assertEqual(self.memory.snapshot()["jobs"][original_ref["job_id"]]["request"]["runner"], original.identity)
        self.assertEqual((original.calls, self.runner.calls), (4, 0))

    def test_legacy_runner_identity_behavior_is_unchanged(self):
        issue, dataset = fixture()
        dataset["narratives"] = {}
        original, incoming = NarrationRunner(), NarrationRunner()
        original.identity, incoming.identity = "synthetic-legacy-owner", "synthetic-legacy-new-owner"
        first = generate_report_narratives(self.memory, issue, dataset, original, languages=["en"])
        original_jobs = deepcopy(self.memory.snapshot()["jobs"])
        second = generate_report_narratives(self.memory, issue, dataset, incoming, languages=["en"])
        self.assertEqual((first["failures"], second["failures"]), ({}, {}))
        self.assertEqual((len(original.calls), len(incoming.calls)), (3, 3))
        state = self.memory.snapshot()
        self.assertTrue(all(state["jobs"][job_id] == row for job_id, row in original_jobs.items()))
        self.assertEqual({job["request"]["runner"] for job in state["jobs"].values() if job["request"]["role"] == "G"},
            {original.identity, incoming.identity})

    def test_saved_row_proofs_share_one_finite_verified_history_view(self):
        first = self.narrate()
        self.assertEqual(first["failures"], {})
        with patch.object(self.memory, "snapshot", wraps=self.memory.snapshot) as snapshot, \
             patch.object(narration, "run_current_model_job") as wrapper, \
             patch.object(narration, "problem") as problem:
            accepted, failed, refs = narration._functional_writing_rows(self.memory, self.issue, self.dataset,
                self.payload, self.runner, 5, [], {})
        self.assertEqual((len(accepted), failed, len(refs)), (11, {}, 11))
        self.assertEqual(snapshot.call_count, 1)
        wrapper.assert_not_called()
        problem.assert_not_called()
        self.assertEqual(refs, first["writing_item_refs"]["en"])

    def test_foreign_unstarted_original_is_not_new_execution_authority(self):
        original = RecordingWriter()
        original.identity = "synthetic-foreign-unstarted-owner"
        supplied = dict(self.payload, items=self.payload["items"][:1])
        job_id, request, bindings = self.request(supplied, original)
        self.memory.put("jobs", job_id, {"request": request, "binding_keys": bindings})
        turn = self.open()
        with bind_work_turn(self.memory, turn["id"]):
            self.consume()
            before = self.memory.snapshot_with_provenance()
            with patch.object(narration, "problem") as problem:
                result = self.narrate()
            problem.assert_not_called()
            self.assertEqual(self.memory.snapshot_with_provenance(), before)
        self.assertEqual((original.calls, self.runner.calls), (0, 0))
        self.assertEqual(len(result["failures"]["en"]), 11)
        self.assertEqual(set(result["failures"]["en"].values()), {"WORK_TURN_MODEL_LIMIT"})
        self.assertFalse((self.memory.root / "ai_jobs" / job_id / "intent.json").exists())


if __name__ == "__main__":
    unittest.main()
