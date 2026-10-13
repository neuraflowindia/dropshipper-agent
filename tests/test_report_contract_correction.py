"""One bounded saved report-format repair; fixtures make no provider requests."""

from copy import deepcopy
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from product_radar.models import SafetyError, digest
from product_radar.report_narration import (CONTRACT_REPAIR_RULES, CONTRACT_REPAIR_SCOPE, _correct_contract_item,
                                          generate_report_narratives, make_narrative_input, recovered_narration_problems)
from product_radar.research_ai import Jobs, MAX_JOB_BYTES, prepare_retry
from product_radar.research_flow import problem
from product_radar.research_memory import ResearchMemory
from product_radar.storage import Journal, atomic_json, load_json
from test_live_outputs import fixture
from test_report_narration import NarrationRunner


class ContractCorrectionRunner(NarrationRunner):
    identity = "synthetic-report-contract-correction-v1"

    def __init__(self, bad_id, original=None, *, correction=None, envelope=None, transport=None):
        super().__init__()
        self.bad_id, self.original, self.correction, self.envelope, self.transport = bad_id, original, correction, envelope, transport

    def __call__(self, role, payload, folder):
        repair = "narration_repair" in payload
        def mutate(item, value):
            if "criteria" in item:
                value["checklist_phrases"] = [{"id": row["id"], "text": "Synthetic saved judgment"} for row in item["criteria"]]
            if item["id"] == self.bad_id and item["language"] == "en":
                change = self.correction if repair else self.original
                if change is not None:
                    change(item, value)
        self.mutate = mutate
        response, usage = super().__call__(role, payload, folder)
        if repair and self.transport:
            raise SafetyError(self.transport)
        if not repair and self.envelope is not None:
            response = self.envelope(payload, response)
        return response, usage


class ReportContractCorrectionTests(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.addCleanup(self.folder.cleanup)
        root = Path(self.folder.name)
        self.memory = ResearchMemory(root / "research", Journal(root / "finance"), evidence_type="synthetic")
        self.issue, self.dataset = fixture()
        self.dataset["narratives"] = {}
        self.bad_id = self.dataset["products"][2]["concept_id"]

    def narrate(self, runner, **options):
        return generate_report_narratives(self.memory, self.issue, self.dataset, runner, languages=["en"], **options)

    def reference_runner(self, **options):
        return ContractCorrectionRunner(self.bad_id, lambda item, value: value.update(references=["invented-reference"]), **options)

    def save_originals(self, runner, *, mark_problems=False):
        payload = make_narrative_input(self.dataset, "en")
        context = {key: value for key, value in payload.items() if key != "items"}
        jobs, failures = {}, {}
        for start in range(0, len(payload["items"]), 5):
            with patch("product_radar.research_ai.utc_now", return_value="2026-10-07T10:00:00+00:00"):
                job_id, result = Jobs(self.memory, runner).run("G", dict(context, items=payload["items"][start:start + 5]))
            jobs[job_id] = (self.memory.root / "ai_jobs" / job_id / "result.json").read_bytes()
            failures.update(result["failures"])
        if mark_problems:
            with patch("product_radar.research_flow.utc_now", return_value="2026-10-07T10:01:00+00:00"):
                for item_id, code in failures.items():
                    problem(self.memory, "report_narration", item_id, code, revision=digest([context, item_id]))
        return payload, context, jobs

    def test_one_reference_correction_preserves_originals_siblings_and_facts(self):
        runner = self.reference_runner()
        payload, context, original = self.save_originals(runner)
        before = deepcopy(self.dataset)
        calls = len(runner.calls)
        output = self.narrate(runner, chunk_size=2)
        self.assertEqual(output["failures"], {})
        self.assertEqual(len(runner.calls), calls + 1)
        repair = runner.calls[-1]
        self.assertEqual([row["id"] for row in repair["items"]], [self.bad_id])
        descriptor = repair["narration_repair"]
        self.assertEqual(descriptor["scope"], CONTRACT_REPAIR_SCOPE)
        self.assertEqual(descriptor["context_hash"], digest(context))
        self.assertEqual(descriptor["failure_code"], "AI_REPORT_FACT_REFERENCE_INVALID")
        self.assertEqual(descriptor["rejected_output"]["references"], ["invented-reference"])
        self.assertEqual(descriptor["rules"], CONTRACT_REPAIR_RULES)
        self.assertEqual(repair["items"][0], next(row for row in payload["items"] if row["id"] == self.bad_id))
        self.assertEqual(self.dataset, before)
        self.assertEqual(output["dataset"]["products"], self.dataset["products"])
        for job_id, raw in original.items():
            self.assertEqual((self.memory.root / "ai_jobs" / job_id / "result.json").read_bytes(), raw)
        current = next(row for row in output["dataset"]["narratives"]["en"]["products"] if row["concept_id"] == self.bad_id)
        self.assertIn("saved market checks", current["reason"])

    def test_accepted_repair_replays_with_new_batch_caller_and_no_runner(self):
        runner = self.reference_runner()
        first = self.narrate(runner)
        calls, state = len(runner.calls), deepcopy(self.memory.snapshot()["jobs"])
        second = self.narrate(runner, chunk_size=2)
        self.assertEqual(len(runner.calls), calls)
        changed = self.reference_runner()
        changed.identity = "different-fixture-caller"
        third = self.narrate(changed, chunk_size=7)
        offline = self.narrate(None)
        self.assertEqual(changed.calls, [])
        self.assertEqual(first["dataset"], second["dataset"])
        self.assertEqual(first["dataset"], third["dataset"])
        self.assertEqual(first["dataset"], offline["dataset"])
        self.assertEqual(self.memory.snapshot()["jobs"], state)

    def test_empty_current_title_is_repaired_in_only_its_own_heading_slot(self):
        self.bad_id = make_narrative_input(self.dataset, "en")["items"][0]["id"]
        runner = ContractCorrectionRunner(self.bad_id, lambda item, value: value.update(title=""))
        output = self.narrate(runner)
        self.assertEqual(output["failures"], {})
        self.assertEqual(output["dataset"]["narratives"]["en"]["title"], "Product picks")
        repairs = [row for row in runner.calls if "narration_repair" in row]
        self.assertEqual(len(repairs), 1)
        self.assertEqual(repairs[0]["items"][0]["kind"], "report_title")
        self.assertEqual(repairs[0]["narration_repair"]["failure_code"], "AI_REPORT_TITLE_SLOTS_INVALID")
        self.assertEqual(repairs[0]["items"][0]["facts"], [])
        self.assertEqual(output["dataset"]["products"], self.dataset["products"])

    def test_failed_correction_is_once_and_keeps_original_reason_or_new_specific_failure(self):
        runner = self.reference_runner(correction=lambda item, value: value.update(references=["still-invented"]))
        first = self.narrate(runner)
        self.assertEqual(first["failures"]["en"][self.bad_id], "AI_REPORT_FACT_REFERENCE_INVALID")
        repairs = [row for row in runner.calls if "narration_repair" in row]
        self.assertEqual(len(repairs), 1)
        count = len(runner.calls)
        self.narrate(runner, chunk_size=2)
        self.narrate(None)
        changed = self.reference_runner()
        changed.identity = "different-failed-correction-caller"
        output = self.narrate(changed)
        self.assertEqual(output["failures"], first["failures"])
        self.assertEqual(changed.calls, [])
        self.assertEqual(len(runner.calls), count)

    def test_missing_duplicate_and_wrong_id_results_are_one_bound_item_not_new_facts(self):
        for case in ("missing", "duplicated", "wrong_id"):
            with self.subTest(case=case), tempfile.TemporaryDirectory() as folder:
                memory = ResearchMemory(Path(folder) / "research", Journal(Path(folder) / "finance"), evidence_type="synthetic")
                def envelope(payload, response):
                    rows = response["results"]
                    matches = [row for row in rows if row["id"] == self.bad_id]
                    if not matches:
                        return response
                    if case == "missing":
                        rows.remove(matches[0])
                    elif case == "duplicated":
                        rows.append(deepcopy(matches[0]))
                    else:
                        matches[0]["id"] = "not-a-supplied-item"
                    return response
                runner = ContractCorrectionRunner(self.bad_id, envelope=envelope)
                output = generate_report_narratives(memory, self.issue, self.dataset, runner, languages=["en"])
                self.assertEqual(output["failures"], {})
                repairs = [row for row in runner.calls if "narration_repair" in row]
                self.assertEqual(len(repairs), 1)
                self.assertEqual(repairs[0]["narration_repair"]["failure_code"], "AI_RESULT_MISSING_OR_DUPLICATED")
                self.assertIsNone(repairs[0]["narration_repair"]["rejected_output"])
                self.assertEqual(len(repairs[0]["narration_repair"]["rejected_rows"]), 2 if case == "duplicated" else 0)
                self.assertEqual(output["dataset"]["products"], self.dataset["products"])

    def test_actual_saved_malformed_envelope_has_bounded_per_item_repairs_and_replay(self):
        def malformed(payload, response):
            return {"results": "not a result list"} if any(row["id"] == self.bad_id for row in payload["items"]) else response
        runner = ContractCorrectionRunner(self.bad_id, envelope=malformed)
        first = self.narrate(runner)
        self.assertEqual(first["failures"], {})
        repairs = [row for row in runner.calls if "narration_repair" in row]
        self.assertEqual(len(repairs), 5)
        self.assertTrue(all(len(row["items"]) == 1 for row in repairs))
        self.assertTrue(all(row["narration_repair"]["failure_code"] == "AI_ENVELOPE_INVALID" for row in repairs))
        calls = len(runner.calls)
        self.assertEqual(self.narrate(None)["dataset"], first["dataset"])
        self.assertEqual(len(runner.calls), calls)

    def test_shape_protected_placeholder_plain_text_and_slot_failures_use_same_frozen_item(self):
        changes = {
            "missing_field": lambda item, value: value.pop("points"),
            "wrong_type": lambda item, value: value.update(points="not a list"),
            "empty_reference": lambda item, value: value.update(references=[]),
            "placeholder": lambda item, value: value.update(reason="{{title}} costs {{invented_amount}}."),
            "plain_text": lambda item, value: value.update(reason="{{title}} <b>has saved evidence</b>."),
            "long_text": lambda item, value: value.update(reason="{{title}} " + "x" * 500),
            "missing_title_placeholder": lambda item, value: value.update(reason="The saved market checks are worth investigating."),
            "changed_limitation": lambda item, value: value.update(limitation="Single-piece dispatch is confirmed."),
            "wrong_scope": lambda item, value: value.update(issue_id="another-issue"),
        }
        for case, mutation in changes.items():
            with self.subTest(case=case), tempfile.TemporaryDirectory() as folder:
                memory = ResearchMemory(Path(folder) / "research", Journal(Path(folder) / "finance"), evidence_type="synthetic")
                runner = ContractCorrectionRunner(self.bad_id, mutation)
                output = generate_report_narratives(memory, self.issue, self.dataset, runner, languages=["en"])
                self.assertEqual(output["failures"], {})
                repairs = [row for row in runner.calls if "narration_repair" in row]
                self.assertEqual(len(repairs), 1)
                self.assertEqual(repairs[0]["facts_hash"], output["facts_hash"])
                self.assertEqual(output["dataset"]["products"], self.dataset["products"])

    def test_protected_number_correction_removes_unsupported_claim_not_invents_a_benefit(self):
        runner = ContractCorrectionRunner(self.bad_id, lambda item, value: value.update(reason="{{title}} will make 100% profit."))
        output = self.narrate(runner)
        self.assertEqual(output["failures"], {})
        repair = next(row for row in runner.calls if "narration_repair" in row)
        self.assertEqual(repair["narration_repair"]["failure_code"], "AI_REPORT_UNPROTECTED_NUMBER_reason")
        self.assertIn("remove an unsupported claim", repair["narration_repair"]["rules"])
        current = next(row for row in output["dataset"]["narratives"]["en"]["products"] if row["concept_id"] == self.bad_id)
        self.assertNotIn("profit", current["reason"])
        self.assertNotIn("100", current["reason"])
        self.assertEqual(output["dataset"]["products"], self.dataset["products"])

    def test_direct_forecast_and_commercial_claim_failures_do_not_seek_a_better_verdict(self):
        for reason in ("{{title}} is a guaranteed winner.", "{{title}} includes confirmed supplier dispatch."):
            with self.subTest(reason=reason), tempfile.TemporaryDirectory() as folder:
                memory = ResearchMemory(Path(folder) / "research", Journal(Path(folder) / "finance"), evidence_type="synthetic")
                runner = ContractCorrectionRunner(self.bad_id, lambda item, value: value.update(reason=reason))
                output = generate_report_narratives(memory, self.issue, self.dataset, runner, languages=["en"])
                self.assertIn(self.bad_id, output["failures"]["en"])
                self.assertFalse(any("narration_repair" in row for row in runner.calls))

    def test_no_runner_keeps_saved_failure_without_new_intent_then_one_enabled_repair(self):
        runner = self.reference_runner()
        self.save_originals(runner)
        jobs = deepcopy(self.memory.snapshot()["jobs"])
        offline = self.narrate(None)
        self.assertEqual(offline["failures"]["en"][self.bad_id], "AI_REPORT_FACT_REFERENCE_INVALID")
        self.assertEqual(self.memory.snapshot()["jobs"], jobs)
        calls = len(runner.calls)
        enabled = self.narrate(runner)
        self.assertEqual(enabled["failures"], {})
        self.assertEqual(len(runner.calls), calls + 1)

    def test_correction_transport_failure_and_prepared_retry_do_not_relaunch(self):
        runner = self.reference_runner(transport="AI_NETWORK_UNAVAILABLE")
        first = self.narrate(runner)
        self.assertEqual(first["failures"]["en"][self.bad_id], "AI_NETWORK_UNAVAILABLE")
        jobs = self.memory.snapshot()["jobs"]
        repair_id = next(job_id for job_id, row in jobs.items() if "narration_repair" in row["request"]["payload"])
        calls = len(runner.calls)
        self.narrate(None)
        changed = self.reference_runner()
        changed.identity = "changed-transport-correction-fixture"
        self.assertEqual(self.narrate(changed)["failures"], first["failures"])
        self.assertEqual(changed.calls, [])
        self.assertEqual(len(runner.calls), calls)
        prepare_retry(self.memory, repair_id, "Synthetic explicit transport retry preparation.")
        pending = self.narrate(runner)
        self.assertEqual(pending["failures"]["en"][self.bad_id], "AI_JOB_PENDING_RECONCILIATION")
        self.assertEqual(len(runner.calls), calls)
        self.assertFalse((self.memory.root / "ai_jobs" / repair_id / "retry-1/intent.json").exists())

    def test_changed_original_response_cannot_create_a_second_correction(self):
        runner = self.reference_runner()
        self.narrate(runner)
        repair = next(row for row in runner.calls if "narration_repair" in row)
        source_id = repair["narration_repair"]["original_job_id"]
        path = self.memory.root / "ai_jobs" / source_id / "result.json"
        saved = load_json(path, MAX_JOB_BYTES)
        next(row for row in saved["response"]["results"] if row["id"] == self.bad_id)["references"] = ["a-different-invented-reference"]
        atomic_json(path, saved)
        calls = len(runner.calls)
        # A changed original answer is not an ordinary rejected writing slot.
        # Preserve the protected conflict instead of accepting its replacement
        # or launching another correction; the programme owns issue-local waits.
        with self.assertRaisesRegex(SafetyError, "REPORT_NARRATION_CORRECTION_JOB_CONFLICT"):
            self.narrate(runner)
        self.assertEqual(len(runner.calls), calls)
        self.assertEqual(sum("narration_repair" in row["request"]["payload"] for row in self.memory.snapshot()["jobs"].values()), 1)

    def test_current_ten_point_missing_checklist_is_repaired_without_changing_judgments(self):
        from test_fit_pipeline import current_fixture, selection
        _, self.issue, self.dataset = selection(current_fixture())
        self.bad_id = self.dataset["products"][2]["concept_id"]
        runner = ContractCorrectionRunner(self.bad_id, lambda item, value: value.update(checklist_phrases=[]))
        before = deepcopy(self.dataset["products"])
        output = self.narrate(runner)
        self.assertEqual(output["failures"], {})
        self.assertEqual(output["dataset"]["products"], before)
        repairs = [row for row in runner.calls if "narration_repair" in row]
        self.assertEqual(len(repairs), 1)
        self.assertEqual(repairs[0]["narration_repair"]["failure_code"], "AI_REPORT_CHECKLIST_IDS_INVALID")
        written = next(row for row in output["dataset"]["narratives"]["en"]["products"] if row["concept_id"] == self.bad_id)
        self.assertEqual(len(written["checklist_phrases"]), 10)

    def test_completion_only_accepted_repair_reconciles_without_another_call(self):
        runner = self.reference_runner()
        first = self.narrate(runner)
        job_id = next(job_id for job_id, row in self.memory.snapshot()["jobs"].items() if "narration_repair" in row["request"]["payload"])
        folder = self.memory.root / "ai_jobs" / job_id
        saved = load_json(folder / "result.json", MAX_JOB_BYTES)
        atomic_json(folder / "completion.json", {"response": saved["response"], "usage": saved["usage"]})
        (folder / "result.json").unlink()
        calls = len(runner.calls)
        second = self.narrate(None)
        self.assertEqual(first["dataset"], second["dataset"])
        self.assertEqual(len(runner.calls), calls)
        self.assertTrue((folder / "result.json").exists())

    def test_saved_general_repair_recovers_only_exact_original_problem(self):
        runner = self.reference_runner()
        payload, context, jobs = self.save_originals(runner, mark_problems=True)
        source_id = next(job_id for job_id in jobs if self.bad_id in self.memory.snapshot()["jobs"][job_id]["binding_keys"])
        item = next(row for row in payload["items"] if row["id"] == self.bad_id)
        with patch("product_radar.research_ai.utc_now", return_value="2026-10-07T10:02:00+00:00"):
            repair_id, checked = _correct_contract_item(self.memory, runner, context, item, source_id)
        self.assertIn(self.bad_id, checked["accepted"])
        state = self.memory.snapshot()
        recovered = recovered_narration_problems(state, self.memory.root, as_of="2026-10-07T10:03:00+00:00")
        self.assertEqual(len(recovered), 1)
        self.assertEqual(recovered_narration_problems(state, self.memory.root, as_of="2026-10-07T10:01:30+00:00"), set())
        state["jobs"][repair_id]["request"]["payload"]["narration_repair"]["original_response_hash"] = "f" * 64
        self.assertEqual(recovered_narration_problems(state, self.memory.root, as_of="2026-10-07T10:03:00+00:00"), set())


if __name__ == "__main__":
    unittest.main()
