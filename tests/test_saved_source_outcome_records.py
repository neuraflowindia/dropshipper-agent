"""Saved failure inspection is not a new failure; temporary offline fixtures.

The real source controller and operation recorder are exercised together.
No provider, model, live research store or running process is used.
"""

from copy import deepcopy
import json
from pathlib import Path
import sys
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "tests"))

import test_source_coverage as fixtures
from product_radar import operations
from product_radar import source_coverage as coverage
from product_radar.models import SafetyError
from product_radar.research_memory import key, load_event


SAVED_MESSAGE = "Reviewed an earlier failed result from Google Maps. No new website request was made."


class SavedSourceOutcomeRecordTests(unittest.TestCase):
    def setUp(self):
        self.fixture = fixtures.SourceCoverageTests()
        self.fixture.setUp(); self.addCleanup(self.fixture.doCleanups)
        self.root, self.memory = self.fixture.root, self.fixture.memory
        for target in ("urllib.request.urlopen", "product_radar.research_ai.CodexRunner.__call__"):
            guard = patch(target, side_effect=AssertionError("No provider or model request"))
            guard.start(); self.addCleanup(guard.stop)

    def rows(self, recorder):
        path = recorder.folder / (recorder.run_id + ".jsonl")
        return [json.loads(line) for line in path.read_text().splitlines()]

    def emit_saved_failure(self, **extra):
        operations.record_event("source_outcome", **{
            "stage": "acquire", "route": "google-maps", "task_id": "synthetic-old-task",
            "state": "failed", "actual_attempt": False, "outcome_reused": True,
            "next_action": "continue_to_next_ready_item", **extra,
        })

    def test_real_no_due_retry_branch_keeps_business_result_and_original_evidence_unchanged(self):
        planned = self.fixture.plan([self.fixture.question("google-maps")])
        calls = []

        def old_failure(context):
            calls.append(context["acquisition_id"])
            context["record_dispatch"]()
            return dict(self.fixture.receipt(context, state="failed"),
                        reason="SUPPLIER_ROUTE_RECEIPT_INVALID")

        first = self.fixture.run_sources({"google-maps": old_failure})
        self.assertEqual(first["handler_calls"], 1)
        self.assertEqual(first["counts"], {"failed": 1})
        before = self.memory.snapshot()
        task_id = planned["task_ids"][0]
        original_task = before["query_tasks"][task_id]
        self.assertEqual(original_task["status"], "failed")
        self.assertFalse(original_task["retryable"])
        self.assertEqual(original_task["tries"], 1)
        original_events = set((self.memory.root / "events").glob("*.json"))
        observed = []
        original_event = coverage.record_event

        def observe(event, **values):
            original_event(event, **values)
            if event == "source_outcome":
                observed.append(deepcopy(operations.read_operation_status(self.root)))

        with operations.operation_run(self.root) as recorder, operations.operation_writer(), \
             patch.object(coverage, "record_event", side_effect=observe):
            operations.record_event("stage_started", stage="acquire")
            replay = self.fixture.run_sources({"google-maps": old_failure})
            current = operations.read_operation_status(self.root)
            self.assertEqual(current["counts"]["failures"], 0)
            self.assertEqual(current["counts"]["saved_failed_results"], 1)
            self.assertEqual(current["counts"]["completed_results"], 0)
            self.assertIsNone(current["last_useful_result_at"])

        self.assertEqual(len(calls), 1)
        self.assertEqual(replay["handler_calls"], 0)
        self.assertEqual(replay["counts"], {"failed": 1})  # The business result is not relabelled.
        self.assertEqual(replay["results"], [{"task_id": task_id, "state": "failed", "actual_attempt": False,
            "reason": "Original finite question has no due retry; other questions continue."}])
        self.assertEqual(len(observed), 1)
        self.assertEqual(observed[0]["message"], SAVED_MESSAGE)
        self.assertEqual(observed[0]["next_action"], "continue_to_next_ready_item")
        outcomes = [row for row in self.rows(recorder) if row["event"] == "source_outcome"]
        self.assertEqual(len(outcomes), 1)
        self.assertIs(outcomes[0]["actual_attempt"], False)
        self.assertIs(outcomes[0]["outcome_reused"], True)
        self.assertEqual(outcomes[0]["state"], "failed")
        self.assertNotIn("error_reported", outcomes[0])

        after = self.memory.snapshot()
        for table in before:
            if table != "batch_runs":
                self.assertEqual(after[table], before[table], table)
        self.assertEqual(after["query_tasks"][task_id], original_task)
        saved_run = after["batch_runs"][key("source-run", "week-one")]
        self.assertEqual(saved_run["last_results"], replay["results"])
        self.assertTrue(all("outcome_reused" not in row for row in saved_run["last_results"]))
        # The existing executor saves its normal pass metadata, not a new
        # research receipt, dispatch, attempt, assessment or financial event.
        added_events = set((self.memory.root / "events").glob("*.json")) - original_events
        self.assertTrue(added_events)
        for path in added_events:
            self.assertEqual({change["table"] for change in load_event(path)["changes"]}, {"batch_runs"})
        self.assertFalse(self.memory.journal.path.exists())

    def test_real_fresh_prelaunch_failure_without_reuse_marker_is_still_a_current_failure(self):
        planned = self.fixture.plan([self.fixture.question("google-maps")])
        calls = []

        def prelaunch(context):
            calls.append(context["acquisition_id"])
            return dict(self.fixture.receipt(context, state="failed", actual=False),
                        reason="SOURCE_BROWSER_EXECUTABLE_MISSING")

        with operations.operation_run(self.root) as recorder, operations.operation_writer():
            result = self.fixture.run_sources({"google-maps": prelaunch})
            current = operations.read_operation_status(self.root)
            self.assertEqual(current["counts"]["failures"], 1)
            self.assertEqual(current["counts"]["saved_failed_results"], 0)
            self.assertEqual(current["counts"]["completed_results"], 0)
            self.assertIsNone(current["last_useful_result_at"])
        self.assertEqual(len(calls), 1)
        self.assertEqual(result["handler_calls"], 1)
        self.assertFalse(result["results"][0]["actual_attempt"])
        self.assertNotIn("outcome_reused", result["results"][0])
        outcome = next(row for row in self.rows(recorder) if row["event"] == "source_outcome")
        self.assertIs(outcome["actual_attempt"], False)
        self.assertIs(outcome["outcome_reused"], False)
        saved = self.memory.snapshot()["query_tasks"][planned["task_ids"][0]]
        self.assertEqual(saved["status"], "failed")
        self.assertEqual(saved["generation"], 1)
        self.assertEqual(saved["tries"], 0)
        self.assertEqual(saved["prelaunch_failures"], 1)

    def test_real_fresh_dispatched_failure_still_counts_and_does_not_gain_useful_credit(self):
        self.fixture.plan([self.fixture.question("google-maps")])

        def failure(context):
            context["record_dispatch"]()
            return self.fixture.receipt(context, state="failed")

        with operations.operation_run(self.root) as recorder, operations.operation_writer():
            result = self.fixture.run_sources({"google-maps": failure})
            current = operations.read_operation_status(self.root)
            self.assertEqual(current["counts"]["failures"], 1)
            self.assertEqual(current["counts"]["saved_failed_results"], 0)
            self.assertEqual(current["counts"]["completed_results"], 0)
            self.assertIsNone(current["last_useful_result_at"])
        self.assertTrue(result["results"][0]["actual_attempt"])
        outcome = next(row for row in self.rows(recorder) if row["event"] == "source_outcome")
        self.assertIs(outcome["actual_attempt"], True)
        self.assertIs(outcome["outcome_reused"], False)

    def test_real_prelaunch_exception_keeps_error_location_and_current_failure(self):
        self.fixture.plan([self.fixture.question("google-maps")])
        namespace = {"SafetyError": SafetyError}
        exec(compile("def broken(context):\n    raise SafetyError('SOURCE_BROWSER_EXECUTABLE_MISSING')\n",
                     str(self.root / "src/offline_fixture.py"), "exec"), namespace)
        with operations.operation_run(self.root) as recorder, operations.operation_writer():
            result = self.fixture.run_sources({"google-maps": namespace["broken"]})
            current = operations.read_operation_status(self.root)
            self.assertEqual(current["counts"]["failures"], 1)
            self.assertEqual(current["counts"]["saved_failed_results"], 0)
            self.assertEqual(current["counts"]["completed_results"], 0)
        self.assertFalse(result["results"][0]["actual_attempt"])
        error = next(row for row in self.rows(recorder) if row["event"] == "source_action_failed")
        self.assertEqual(error["error_type"], "SafetyError")
        self.assertIs(error["error_reported"], True)
        self.assertEqual(error["reason"], "SOURCE_BROWSER_EXECUTABLE_MISSING")
        self.assertEqual(error["error_location"], {"file": "src/offline_fixture.py", "function": "broken", "line": 2})

    def test_only_strict_boolean_flags_survive_filtering(self):
        self.assertEqual(operations._fields({"error_reported": True}), {})
        for actual in (False, True):
            for reused in (False, True):
                self.assertEqual(operations._fields({"actual_attempt": actual, "outcome_reused": reused}),
                                 {"actual_attempt": actual, "outcome_reused": reused})
        for malformed in (None, 0, 1, "false", "true", [], {}, object()):
            with self.subTest(value=repr(malformed)):
                self.assertEqual(operations._fields({"actual_attempt": malformed, "outcome_reused": malformed}), {})

    def test_exact_conjunction_is_required_and_old_unmarked_events_still_count(self):
        cases = [
            ("source_outcome", {"state": "failed", "actual_attempt": False}),
            ("source_outcome", {"state": "failed", "outcome_reused": True}),
            ("source_outcome", {"state": "failed", "actual_attempt": True, "outcome_reused": True}),
            ("source_outcome", {"state": "failed", "actual_attempt": False, "outcome_reused": False}),
            ("source_outcome", {"state": "failed", "actual_attempt": 0, "outcome_reused": True}),
            ("source_outcome", {"state": "failed", "actual_attempt": False, "outcome_reused": "true"}),
            ("other_failure", {"state": "failed", "actual_attempt": False, "outcome_reused": True}),
            ("source_outcome", {"state": "failed"}),
        ]
        with operations.operation_run(self.root), operations.operation_writer():
            for number, (event, fields) in enumerate(cases, 1):
                with self.subTest(event=event, fields=fields):
                    operations.record_event(event, **fields)
                    current = operations.read_operation_status(self.root)
                    self.assertEqual(current["counts"]["failures"], number)
                    self.assertEqual(current["counts"]["saved_failed_results"], 0)
                    self.assertEqual(current["counts"]["completed_results"], 0)
            self.emit_saved_failure(state="blocked")
            self.assertEqual(operations.read_operation_status(self.root)["counts"]["failures"], len(cases))
            self.assertEqual(operations.read_operation_status(self.root)["counts"]["saved_failed_results"], 0)

    def test_error_type_overrides_saved_marker_without_private_text_leakage(self):
        secret = "private-cookie-and-account-details"
        with operations.operation_run(self.root) as recorder, operations.operation_writer():
            try:
                exec(compile("raise RuntimeError(secret)", str(self.root / "src/error_fixture.py"), "exec"),
                     {"RuntimeError": RuntimeError, "secret": secret})
            except RuntimeError as error:
                operations.record_error("source_outcome", error, state="failed", actual_attempt=False,
                    outcome_reused=True, error_reported=False, stage="acquire", route="google-maps", prompt=secret,
                    environment={"cookie": secret}, reason=secret, next_action="continue_to_next_ready_item")
            current = operations.read_operation_status(self.root)
            self.assertEqual(current["counts"]["failures"], 1)
            self.assertEqual(current["counts"]["saved_failed_results"], 0)
            self.assertEqual(current["counts"]["completed_results"], 0)
            self.assertNotEqual(current["message"], SAVED_MESSAGE)
        rows = self.rows(recorder)
        self.assertNotIn(secret, json.dumps(rows))
        row = next(row for row in rows if row["event"] == "source_outcome")
        self.assertEqual(row["error_type"], "RuntimeError")
        self.assertIs(row["error_reported"], True)
        self.assertEqual(row["error_location"], {"file": "src/error_fixture.py", "function": "<module>", "line": 1})
        self.assertIs(row["actual_attempt"], False)
        self.assertIs(row["outcome_reused"], True)
        self.assertEqual(row["next_action"], "continue_to_next_ready_item")

    def test_malformed_explicit_error_type_cannot_turn_current_error_into_saved_failure(self):
        private = "private error text cookie=do-not-record"
        too_long = "PrivateError" * 20
        malformed = ("Runtime Error", private, too_long, None, 0, 1, False, True,
                     [], {"private": private}, (private,))
        with operations.operation_run(self.root) as recorder, operations.operation_writer():
            for index, value in enumerate(malformed, 1):
                with self.subTest(index=index, value_type=type(value).__name__):
                    self.emit_saved_failure(error_type=value)
                    current = operations.read_operation_status(self.root)
                    self.assertEqual(current["counts"]["failures"], index)
                    self.assertEqual(current["counts"]["saved_failed_results"], 0)
                    self.assertEqual(current["counts"]["completed_results"], 0)
                    self.assertIsNone(current["last_useful_result_at"])
                    self.assertNotEqual(current["message"], SAVED_MESSAGE)
                    self.assertNotIn("error_type", current["last_event"])
                    self.assertIs(current["last_event"]["error_reported"], True)
                    self.assertIs(current["last_event"]["actual_attempt"], False)
                    self.assertIs(current["last_event"]["outcome_reused"], True)
        rows = self.rows(recorder)
        self.assertEqual(sum(row["event"] == "source_outcome" for row in rows), len(malformed))
        self.assertNotIn(private, json.dumps(rows))
        self.assertNotIn(too_long, json.dumps(rows))
        self.assertNotIn("Runtime Error", json.dumps(rows))

    def test_saved_inspection_keeps_explicit_phase_clock_deadline_and_useful_clock(self):
        phases = (
            ("source_saved_recovery", None, "Checking saved website replies without starting new searches."),
            ("model_call", 120, "Waiting for the AI to finish its answer."),
            ("response_save", None, "Saving the information collected from a website."),
        )
        with operations.operation_run(self.root), operations.operation_writer():
            for index, (phase, deadline, message) in enumerate(phases, 1):
                with self.subTest(phase=phase):
                    operations.record_event("phase_started", stage="acquire", phase=phase, deadline_seconds=deadline)
                    before = operations.read_operation_status(self.root)
                    with patch.object(operations, "utc_now", return_value=fixtures.LATER):
                        self.emit_saved_failure()
                    after = operations.read_operation_status(self.root)
                    self.assertEqual(after["phase"], phase)
                    self.assertEqual(after["phase_started_at"], before["phase_started_at"])
                    self.assertEqual(after["phase_deadline_seconds"], deadline)
                    self.assertEqual(after["message"], message)
                    self.assertEqual(after["counts"]["failures"], 0)
                    self.assertEqual(after["counts"]["saved_failed_results"], index)
                    self.assertEqual(after["counts"]["completed_results"], 0)
                    self.assertIsNone(after["last_useful_result_at"])
                    self.assertEqual(after["last_event"]["at"], fixtures.LATER)
                    self.assertGreaterEqual(after["last_event"]["elapsed_seconds"], 0)


if __name__ == "__main__":
    unittest.main()
