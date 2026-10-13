"""Saved cost/time reports; synthetic receipts cannot become live source proof."""

from copy import deepcopy
from pathlib import Path
import tempfile
import unittest

from product_radar.models import SafetyError, digest
from product_radar.operational_audit import audit_operational_checkpoint, direct_capture_effort, model_effort
from product_radar.research_memory import ResearchMemory
from product_radar.storage import Journal, atomic_json


NOW = "2026-10-08T10:00:00+00:00"
LATER = "2026-10-08T10:00:10+00:00"
USAGE = {"input_tokens": 100, "cached_input_tokens": 20, "output_tokens": 10}


class OperationalAuditTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.memory = ResearchMemory(self.root / "research", Journal(self.root / "finance"), evidence_type="synthetic")
        self.request = {"role": "E", "payload": {"items": [{"id": "fictional-product"}]},
                        "prompt_version": "synthetic-version", "model": "synthetic-model", "runner": "synthetic-only"}
        self.memory.put("jobs", "fictional-job", {"request": self.request, "binding_keys": {}})
        self.folder = self.memory.root / "ai_jobs/fictional-job"

    def write_attempt(self, *, attempt="initial", usage=USAGE, failure=False, result=True, execution=True):
        folder = self.folder if attempt == "initial" else self.folder / attempt
        atomic_json(folder / "intent.json", {"request": self.request, "created_at": NOW})
        if execution:
            atomic_json(folder / "runner_execution.json", {"request_hash": digest(self.request), "job_id": "fictional-job",
                        "attempt": attempt, "started_at": NOW})
        if result:
            atomic_json(folder / "result.json", {"request_hash": digest(self.request), "usage": usage,
                        "response": {"results": []}, "completed_at": LATER})
            atomic_json(folder / "completion.json", {"usage": usage, "response": {"results": []}})
        if failure:
            atomic_json(folder / "failure.json", {"code": "AI_LOCAL_PERMISSION_DENIED", "at": LATER, "usage": None})
        return folder

    def test_prepared_job_is_not_a_model_attempt_or_a_zero_token_invoice(self):
        audit = audit_operational_checkpoint(self.memory, as_of=NOW)
        self.assertEqual(audit["model_effort"]["saved_job_attempts"], 0)
        self.assertIsNone(audit["model_effort"]["incremental_model_cost_usd"])
        self.assertEqual(audit["collection"]["physical_acquisitions"], 0)
        self.assertEqual(audit["evidence_type"], "synthetic")

    def test_result_completion_replay_does_not_double_count_tokens_or_time(self):
        self.write_attempt()
        first = audit_operational_checkpoint(self.memory, as_of=NOW)
        second = audit_operational_checkpoint(self.memory, as_of=NOW)
        self.assertEqual(first, second)
        measured = first["model_effort"]
        self.assertEqual(measured["saved_job_attempts"], 1)
        self.assertEqual(measured["usage_receipts"], 1)
        self.assertEqual(measured["recorded_tokens"], USAGE)
        self.assertEqual(measured["recorded_duration_sum_seconds"], 10)

    def test_genuinely_separate_saved_retry_attempt_is_counted_separately(self):
        self.write_attempt(failure=True, result=False)
        self.write_attempt(attempt="retry-1")
        measured = model_effort(self.memory, self.memory.snapshot())
        self.assertEqual(measured["saved_job_attempts"], 2)
        self.assertEqual(measured["states"], {"failed": 1, "response_saved": 1})
        self.assertEqual(measured["recorded_tokens"], USAGE)
        self.assertEqual(measured["attempts_without_usable_token_counts"], 1)

    def test_pending_and_missing_usage_stay_unknown(self):
        self.write_attempt(result=False)
        self.write_attempt(attempt="retry-1", usage=None, execution=False)
        measured = model_effort(self.memory, self.memory.snapshot())
        self.assertEqual(measured["usage_receipts"], 0)
        self.assertEqual(measured["attempts_without_usable_token_counts"], 2)
        self.assertEqual(measured["duration_receipts"], 0)
        self.assertEqual(measured["states"]["execution_or_reconciliation_pending"], 1)

    def test_wrong_bindings_and_invalid_usage_cannot_inflate_recorded_effort(self):
        folder = self.write_attempt()
        atomic_json(folder / "result.json", {"request_hash": "wrong", "usage": USAGE, "completed_at": LATER})
        measured = model_effort(self.memory, self.memory.snapshot())
        self.assertEqual(measured["usage_receipts"], 0)
        self.assertEqual(measured["states"], {"receipt_needs_review": 1})
        self.assertEqual(measured["duration_receipts"], 0)
        self.assertEqual(measured["recorded_duration_sum_seconds"], 0)
        self.assertIsNone(measured["rows"][0]["duration_seconds"])
        self.write_attempt(usage=dict(USAGE, input_tokens=True))
        self.assertEqual(model_effort(self.memory, self.memory.snapshot())["usage_receipts"], 0)
        self.write_attempt(usage=dict(USAGE, cached_input_tokens=101))
        self.assertEqual(model_effort(self.memory, self.memory.snapshot())["usage_receipts"], 0)

    def test_foreign_intent_or_execution_cannot_donate_elapsed_time(self):
        cases = [("intent.json", {"request": dict(self.request, model="other-model"), "created_at": NOW})]
        execution = {"request_hash": digest(self.request), "job_id": "fictional-job",
                     "attempt": "initial", "started_at": NOW}
        cases.extend(("runner_execution.json", dict(execution, **change)) for change in (
            {"request_hash": "other-request"}, {"job_id": "other-job"}, {"attempt": "retry-1"}))
        for filename, receipt in cases:
            with self.subTest(filename=filename, receipt=receipt):
                folder = self.write_attempt()
                atomic_json(folder / filename, receipt)
                original = {path.name: path.read_bytes() for path in folder.iterdir() if path.is_file()}
                measured = model_effort(self.memory, self.memory.snapshot())
                self.assertEqual(measured["states"], {"receipt_needs_review": 1})
                self.assertEqual(measured["usage_receipts"], 0)
                self.assertEqual(measured["duration_receipts"], 0)
                self.assertEqual(measured["recorded_duration_sum_seconds"], 0)
                self.assertIsNone(measured["rows"][0]["duration_seconds"])
                self.assertEqual(original, {path.name: path.read_bytes() for path in folder.iterdir() if path.is_file()})

    def test_unreadable_result_cannot_donate_failure_elapsed_time(self):
        folder = self.write_attempt(failure=True)
        atomic_json(folder / "result.json", [])
        measured = model_effort(self.memory, self.memory.snapshot())
        self.assertEqual(measured["states"], {"receipt_needs_review": 1})
        self.assertEqual(measured["duration_receipts"], 0)
        self.assertIsNone(measured["rows"][0]["duration_seconds"])

    def test_valid_failed_and_retry_attempts_keep_their_elapsed_time(self):
        self.write_attempt(failure=True, result=False)
        self.write_attempt(attempt="retry-1")
        measured = model_effort(self.memory, self.memory.snapshot())
        self.assertEqual(measured["states"], {"failed": 1, "response_saved": 1})
        self.assertEqual(measured["duration_receipts"], 2)
        self.assertEqual(measured["recorded_duration_sum_seconds"], 20)
        self.assertEqual(measured["usage_receipts"], 1)

    def test_legacy_completion_without_a_result_keeps_usage_but_does_not_invent_time(self):
        folder = self.write_attempt(result=False)
        atomic_json(folder / "completion.json", {"response": {"results": []}, "usage": USAGE, "exit_code": 0})
        measured = model_effort(self.memory, self.memory.snapshot())
        self.assertEqual(measured["states"], {"response_saved": 1})
        self.assertEqual(measured["recorded_tokens"], USAGE)
        self.assertEqual(measured["usage_receipts"], 1)
        self.assertEqual(measured["duration_receipts"], 0)
        self.assertIsNone(measured["rows"][0]["duration_seconds"])

    def test_invalid_usage_does_not_discard_an_independently_valid_duration(self):
        self.write_attempt(usage=dict(USAGE, input_tokens=True))
        measured = model_effort(self.memory, self.memory.snapshot())
        self.assertEqual(measured["states"], {"response_saved": 1})
        self.assertEqual(measured["usage_receipts"], 0)
        self.assertEqual(measured["duration_receipts"], 1)
        self.assertEqual(measured["recorded_duration_sum_seconds"], 10)

    def test_original_financial_events_are_not_changed_or_repolled(self):
        with self.memory.journal.locked(): self.memory.journal.setup_budget("fixture-account:fixture-period", "5")
        original = self.memory.journal.path.read_bytes()
        first = audit_operational_checkpoint(self.memory)
        self.assertEqual(first["collection"]["known_actual_usd"], "0")
        self.assertEqual(first["collection"]["unsettled_hold_usd"], "0")
        self.assertEqual(original, self.memory.journal.path.read_bytes())
        self.assertIn("not the exclusive cost of one report", first["limitations"][0])


def direct(acquisition="one", actual=True, state="partial", **changes):
    return {"evidence_type": "captured", "acquisition_id": acquisition, "dispatch_started": actual,
            "route": "amazon-search", "state": state, "elapsed_seconds": 3.5,
            "apify_requests": 0, "provider_cost_usd": "0", **changes}


class DirectEffortTests(unittest.TestCase):
    def test_actual_empty_blocked_and_prelaunch_failure_are_separate(self):
        receipts = [direct("good"), direct("empty", state="empty"), direct("blocked", state="blocked"),
                    direct("local", actual=False, state="failed"), direct("uncertain", actual=None, state="failed")]
        value = direct_capture_effort(receipts + [deepcopy(receipts[0])])
        self.assertEqual(value["recognised_acquisitions"], 5)
        self.assertEqual(value["actual_dispatches"], 3)
        self.assertEqual(value["proved_pre_dispatch_failures"], 1)
        self.assertEqual(value["uncertain_dispatches"], 1)
        self.assertEqual(value["recorded_duration_sum_seconds"], 17.5)
        self.assertEqual(value["states"], {"partial": 1, "empty": 1, "blocked": 1, "failed": 2})

    def test_different_receipt_for_same_id_is_not_silently_double_counted(self):
        with self.assertRaisesRegex(SafetyError, "CONFLICT"):
            direct_capture_effort([direct(), direct(state="blocked")])

    def test_synthetic_or_unknown_format_does_not_become_actual_trial(self):
        value = direct_capture_effort([direct(evidence_type="synthetic"), {"url": "https://example.org"}])
        self.assertEqual(value["recognised_acquisitions"], 0)
        self.assertEqual(value["unrecognised_receipts"], 2)

    def test_missing_invalid_or_boolean_durations_are_unknown_not_invented_zero(self):
        for elapsed in (None, True, -1):
            with self.subTest(elapsed=elapsed):
                value = direct_capture_effort([direct(elapsed_seconds=elapsed)])
                self.assertEqual(value["duration_receipts"], 0)
                self.assertIsNone(value["rows"][0]["duration_seconds"])
        for elapsed in (float("nan"), float("inf")):
            value = direct_capture_effort([direct(elapsed_seconds=elapsed)])
            self.assertEqual(value["recognised_acquisitions"], 0)
            self.assertEqual(value["invalid_receipts"][0]["reason"], "DIRECT_AUDIT_RECEIPT_INVALID")
        valid = direct_capture_effort([direct(elapsed_seconds=None, started_at=NOW, completed_at=LATER)])
        self.assertEqual(valid["recorded_duration_sum_seconds"], 10)


if __name__ == "__main__":
    unittest.main()
