"""Opt-in original model recovery with real saved local-process receipts."""

from copy import deepcopy
import unittest
from unittest.mock import patch

import test_closed_model_retry as fixtures
from product_radar.functional_definition_review import definition_review_input, MODE, CONTRACT, PROMPT_VERSION
from product_radar.model_recovery import current_model_retry_bindings, run_current_model_job
from product_radar.models import SafetyError, digest
from product_radar.research_ai import closed_model_retry_hint, current_attempt, prepare_closed_model_retry, prepare_retry
from product_radar.research_memory import key
from product_radar.storage import atomic_json, load_json
from product_radar.work_turns import bind_work_turn, open_work_turn


class ModelRecoveryTests(unittest.TestCase):
    def setUp(self):
        fixture = fixtures.ClosedModelRetryTests(); fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        self.fixture, self.memory = fixture, fixture.memory

    def payload(self, ids=None):
        return {"mode": MODE, "definition_contract": CONTRACT, "items": [
            definition_review_input(self.memory.snapshot(), cid, as_of=fixtures.NOW)["item"]
            for cid in (ids or self.fixture.fixture.cids)]}

    def run_job(self, runner, payload, **kwargs):
        return run_current_model_job(self.memory, runner, "B", payload, declared_version=PROMPT_VERSION, **kwargs)

    def test_default_unbound_call_remains_saved_only_without_policy(self):
        job_id, base, _ = self.fixture.fail_closed()
        request = self.memory.snapshot()["jobs"][job_id]["request"]
        runner = fixtures.SuccessRunner()
        with self.assertRaisesRegex(SafetyError, "AI_JOB_PENDING_RECONCILIATION"):
            self.run_job(runner, request["payload"])
        self.assertEqual(runner.calls, []); self.assertFalse((base / "retries.json").exists())

    def test_external_current_source_guard_precedes_unbound_intent(self):
        payload = self.payload([self.fixture.cid]); runner = fixtures.SuccessRunner()
        seen = []

        def refuse(request, folder):
            seen.append(request)
            self.assertFalse((folder / "intent.json").exists())
            self.assertFalse(self.memory.journal._locked)
            raise SafetyError("SYNTHETIC_SOURCE_CHANGED")

        with self.assertRaisesRegex(SafetyError, "SYNTHETIC_SOURCE_CHANGED"):
            self.run_job(runner, payload, before_new_execution=refuse)
        self.assertEqual(len(seen), 1)
        self.assertEqual(runner.calls, [])
        self.assertFalse((self.memory.root / "ai_jobs" / key("job", seen[0]) / "intent.json").exists())

    def test_external_guard_composes_with_cooperative_runner_check_and_preserves_allowance(self):
        payload = self.payload([self.fixture.cid]); seen = []

        class Guarded(fixtures.SuccessRunner):
            closed_retry_policy = {"as_of": lambda: fixtures.AFTER, "retry_seconds": 300,
                                   "retry_not_before": lambda: None}

            def check_new_execution(self):
                seen.append("runner")

        def refuse(request, folder):
            seen.append("source")
            self.assertFalse((folder / "intent.json").exists())
            raise SafetyError("SYNTHETIC_SOURCE_CHANGED")

        runner = Guarded(); turn = open_work_turn(self.memory, "guard-composition", at=fixtures.AFTER)
        with bind_work_turn(self.memory, turn["id"]):
            with self.assertRaisesRegex(SafetyError, "SYNTHETIC_SOURCE_CHANGED"):
                self.run_job(runner, payload, before_new_execution=refuse)
        self.assertEqual(seen, ["runner", "source"])
        self.assertEqual(runner.calls, [])
        self.assertEqual(self.memory.snapshot()["batch_runs"][turn["id"]]["counters"]["model_executions"], 0)

    def test_saved_result_recovery_does_not_call_external_new_execution_guard(self):
        payload = self.payload([self.fixture.cid]); runner = fixtures.SuccessRunner()
        self.run_job(runner, payload)
        def refuse(*_args):
            raise AssertionError("A saved original is not a new execution")
        _, result = self.run_job(runner, payload, as_of=fixtures.AFTER, before_new_execution=refuse)
        self.assertEqual(len(result["accepted"]), 1)
        self.assertEqual(len(runner.calls), 1)

    def test_invalid_external_guard_is_refused_before_preparation(self):
        before = self.memory.snapshot_with_provenance()
        with self.assertRaisesRegex(SafetyError, "AI_EXECUTION_GUARD_INVALID"):
            self.run_job(fixtures.SuccessRunner(), self.payload([self.fixture.cid]),
                         before_new_execution="not-callable")
        self.assertEqual(self.memory.snapshot_with_provenance(), before)

    def test_original_retry_advisory_can_reuse_one_verified_state_without_replaying_history(self):
        job_id, base, _ = self.fixture.fail_closed()
        payload = self.memory.snapshot()["jobs"][job_id]["request"]["payload"]
        before = self.memory.snapshot_with_provenance()
        with patch.object(self.memory, "snapshot", side_effect=AssertionError("Use supplied verified state")):
            bindings = current_model_retry_bindings(self.memory, fixtures.SuccessRunner(), "B", payload,
                declared_version=PROMPT_VERSION, as_of=fixtures.AFTER, state=before["state"])
        self.assertTrue(bindings)
        self.assertFalse((base / "retries.json").exists())
        self.assertEqual(self.memory.snapshot_with_provenance(), before)

    def test_partial_batch_cannot_authorize_original_launch_but_full_current_group_can(self):
        job_id, base, _ = self.fixture.fail_closed(ids=self.fixture.fixture.cids)
        whole = self.memory.snapshot()["jobs"][job_id]["request"]["payload"]
        partial = dict(whole, items=whole["items"][:1]); runner = fixtures.SuccessRunner()
        _, result = self.run_job(runner, partial, as_of=fixtures.AFTER)
        self.assertEqual(set(result["failures"]), {partial["items"][0]["id"]})
        self.assertEqual(runner.calls, []); self.assertFalse((base / "retries.json").exists())
        _, result = self.run_job(runner, partial, current_items=whole["items"], as_of=fixtures.AFTER)
        self.assertEqual(set(result["accepted"]), {partial["items"][0]["id"]})
        self.assertEqual(len(runner.calls), 1); self.assertEqual(len(runner.calls[0][1]["items"]), 2)
        self.assertEqual(current_attempt(base), base / "retry-1")

    def test_prepared_empty_retry_still_cannot_run_a_partial_or_stale_batch(self):
        job_id, base, _ = self.fixture.fail_closed(ids=self.fixture.fixture.cids)
        whole = self.memory.snapshot()["jobs"][job_id]["request"]["payload"]
        prepare_retry(self.memory, job_id, "Synthetic deliberate prepared retry without execution.")
        partial = dict(whole, items=whole["items"][:1]); runner = fixtures.SuccessRunner()
        _, result = self.run_job(runner, partial, as_of=fixtures.AFTER)
        self.assertEqual(result["failures"][partial["items"][0]["id"]], "AI_JOB_PENDING_RECONCILIATION")
        self.assertEqual(runner.calls, []); self.assertFalse((base / "retry-1" / "intent.json").exists())
        changed = deepcopy(whole["items"]); changed[1]["examples"][0]["description"]["label"] += " changed"
        _, result = self.run_job(runner, partial, current_items=changed, as_of=fixtures.AFTER)
        self.assertFalse(result["accepted"]); self.assertEqual(runner.calls, [])

    def test_completed_original_recovers_before_guard_even_for_partial_current_group(self):
        job_id, base, _ = self.fixture.fail_closed(ids=self.fixture.fixture.cids)
        whole = self.memory.snapshot()["jobs"][job_id]["request"]["payload"]
        response, usage = fixtures.SuccessRunner()("B", whole, None)
        atomic_json(base / "result.json", {"request_hash": digest(self.memory.snapshot()["jobs"][job_id]["request"]),
            "response": response, "usage": usage, "completed_at": fixtures.NOW})
        class Guarded(fixtures.SuccessRunner):
            def check_new_execution(self): raise AssertionError("Saved response must not need new-launch authority")
        runner = Guarded(); partial = dict(whole, items=whole["items"][:1])
        _, result = self.run_job(runner, partial, as_of=fixtures.AFTER)
        self.assertEqual(set(result["accepted"]), {partial["items"][0]["id"]})
        self.assertEqual(runner.calls, []); self.assertFalse((base / "retries.json").exists())

    def test_policy_requires_actual_active_turn_and_guard_runs_before_intent(self):
        payload = self.payload([self.fixture.cid]); memory = self.memory
        class Guarded(fixtures.SuccessRunner):
            closed_retry_policy = {"as_of": lambda: fixtures.AFTER, "retry_seconds": 300,
                                   "retry_not_before": lambda: None}
            def check_new_execution(self):
                assert not memory.journal._locked
                raise SafetyError("AI_RATE_LIMITED")
        runner = Guarded(); turn = open_work_turn(self.memory, "guarded-model-recovery", at=fixtures.AFTER)
        with bind_work_turn(self.memory, turn["id"]):
            with self.assertRaisesRegex(SafetyError, "AI_RATE_LIMITED"):
                self.run_job(runner, payload)
        self.assertEqual(runner.calls, [])
        self.assertEqual(self.memory.snapshot()["batch_runs"][turn["id"]]["counters"]["model_executions"], 0)
        self.assertFalse(any((self.memory.root / "ai_jobs" / jid / "intent.json").exists()
            for jid, row in self.memory.snapshot()["jobs"].items() if row["request"]["payload"].get("mode") == MODE))

    def test_same_stage_pause_does_not_create_a_new_unknown_intent_for_remaining_item(self):
        job_id, base, _ = self.fixture.fail_closed()
        payload = self.payload(); memory = self.memory
        class Pausing(fixtures.SuccessRunner):
            paused = False
            closed_retry_policy = {"as_of": lambda: fixtures.AFTER, "retry_seconds": 300,
                                   "retry_not_before": lambda: None}
            def check_new_execution(self):
                assert not memory.journal._locked
                if self.paused: raise SafetyError("AI_RATE_LIMITED")
            def __call__(self, *args):
                self.calls.append(args); self.paused = True
                raise SafetyError("AI_RATE_LIMITED")
        runner = Pausing(); turn = open_work_turn(self.memory, "same-stage-model-recovery", at=fixtures.AFTER)
        with bind_work_turn(self.memory, turn["id"]):
            _, result = self.run_job(runner, payload, current_items=payload["items"])
        self.assertEqual(len(runner.calls), 1)
        self.assertEqual(set(result["failures"]), {row["id"] for row in payload["items"]})
        self.assertEqual(self.memory.snapshot()["batch_runs"][turn["id"]]["counters"]["model_executions"], 1)
        other = next(row for row in payload["items"] if row["id"] != self.fixture.cid)
        request = deepcopy(self.memory.snapshot()["jobs"][job_id]["request"])
        request["payload"] = dict(payload, items=[other])
        self.assertFalse((self.memory.root / "ai_jobs" / key("job", request) / "intent.json").exists())
        self.assertTrue((base / "retry-1" / "intent.json").exists())

    def test_cooperative_policy_applies_actual_due_retry_and_shared_turn_counters(self):
        job_id, base, _ = self.fixture.fail_closed()
        payload = self.memory.snapshot()["jobs"][job_id]["request"]["payload"]
        runner = fixtures.SuccessRunner()
        runner.closed_retry_policy = {"as_of": lambda: fixtures.AFTER, "retry_seconds": 300,
                                     "retry_not_before": lambda: None}
        turn = open_work_turn(self.memory, "cooperative-model-recovery", at=fixtures.AFTER)
        with bind_work_turn(self.memory, turn["id"]): _, result = self.run_job(runner, payload)
        self.assertIn(self.fixture.cid, result["accepted"])
        self.assertEqual(len(runner.calls), 1)
        self.assertEqual(self.memory.snapshot()["batch_runs"][turn["id"]]["counters"]["model_executions"], 1)
        self.assertEqual(current_attempt(base), base / "retry-1")

    def test_guard_wait_preserves_a_recovered_sibling_without_new_intent_or_execution(self):
        job_id, base, _ = self.fixture.fail_closed()
        request = self.memory.snapshot()["jobs"][job_id]["request"]
        response, usage = fixtures.SuccessRunner()("B", request["payload"], None)
        atomic_json(base / "result.json", {"request_hash": digest(request), "response": response,
                                           "usage": usage, "completed_at": fixtures.NOW})
        payload = self.payload()
        class Guarded(fixtures.SuccessRunner):
            def check_new_execution(self): raise SafetyError("AI_RATE_LIMITED")
        runner = Guarded()
        _, result = self.run_job(runner, payload, current_items=payload["items"], as_of=fixtures.AFTER)
        self.assertIn(self.fixture.cid, result["accepted"])
        other = next(row for row in payload["items"] if row["id"] != self.fixture.cid)
        self.assertEqual(result["failures"], {other["id"]: "AI_RATE_LIMITED"})
        self.assertEqual(runner.calls, [])
        other_request = deepcopy(request); other_request["payload"] = dict(payload, items=[other])
        self.assertFalse((self.memory.root / "ai_jobs" / key("job", other_request) / "intent.json").exists())

    def test_pure_due_binding_selector_requires_full_current_owner_and_spends_nothing(self):
        job_id, base, _ = self.fixture.fail_closed(ids=self.fixture.fixture.cids)
        request = self.memory.snapshot()["jobs"][job_id]["request"]
        whole = request["payload"]; partial = dict(whole, items=whole["items"][:1])
        before = self.memory.snapshot_with_provenance()
        runner = fixtures.SuccessRunner()
        options = {"declared_version": PROMPT_VERSION}
        self.assertEqual(current_model_retry_bindings(self.memory, runner, "B", whole, **options), set())
        self.assertEqual(current_model_retry_bindings(self.memory, runner, "B", whole,
            as_of=fixtures.BEFORE, **options), set())
        self.assertEqual(current_model_retry_bindings(self.memory, runner, "B", partial,
            as_of=fixtures.AFTER, **options), set())
        due = current_model_retry_bindings(self.memory, runner, "B", partial, current_items=whole["items"],
                                           as_of=fixtures.AFTER, **options)
        self.assertEqual(due, {self.memory.snapshot()["jobs"][job_id]["binding_keys"][partial["items"][0]["id"]]})
        self.assertEqual(self.memory.snapshot_with_provenance(), before)
        self.assertFalse((base / "retries.json").exists()); self.assertEqual(runner.calls, [])

    def test_pure_due_selector_resumes_genuine_prepared_pointer_without_preparing_twice(self):
        job_id, base, _ = self.fixture.fail_closed()
        request = self.memory.snapshot()["jobs"][job_id]["request"]
        hint = closed_model_retry_hint(self.memory, job_id, as_of=fixtures.AFTER)
        prepare_closed_model_retry(self.memory, job_id, as_of=fixtures.AFTER, expected_signal=hint["signal"])
        runner = fixtures.SuccessRunner(); options = {"declared_version": PROMPT_VERSION, "as_of": fixtures.AFTER}
        before = self.memory.snapshot_with_provenance(); ledger = (base / "retries.json").read_bytes()
        self.assertEqual(current_model_retry_bindings(self.memory, runner, "B", request["payload"], **options),
                         set(self.memory.snapshot()["jobs"][job_id]["binding_keys"].values()))
        self.assertEqual(self.memory.snapshot_with_provenance(), before)
        self.run_job(runner, request["payload"], as_of=fixtures.AFTER)
        self.assertEqual(len(runner.calls), 1); self.assertEqual((base / "retries.json").read_bytes(), ledger)

    def test_due_selector_uses_one_verified_history_for_all_original_owners(self):
        for cid in self.fixture.fixture.cids:
            self.fixture.fail_closed(ids=[cid])
        payload = self.payload()
        before = self.memory.snapshot_with_provenance()
        runner = fixtures.SuccessRunner()
        with patch.object(self.memory, "snapshot", wraps=self.memory.snapshot) as snapshots:
            due = current_model_retry_bindings(self.memory, runner, "B", payload,
                declared_version=PROMPT_VERSION, as_of=fixtures.AFTER)
        self.assertEqual(snapshots.call_count, 1)
        self.assertEqual(len(due), len(payload["items"]))
        self.assertEqual(self.memory.snapshot_with_provenance(), before)
        self.assertEqual(runner.calls, [])

    def test_unbound_retry_policy_keeps_zero_history_selection_fast_path(self):
        payload = self.payload()
        runner = fixtures.SuccessRunner()
        runner.closed_retry_policy = {"as_of": lambda: fixtures.AFTER, "retry_seconds": 300,
                                      "retry_not_before": lambda: None}
        with patch.object(self.memory, "snapshot", wraps=self.memory.snapshot) as snapshots:
            due = current_model_retry_bindings(self.memory, runner, "B", payload,
                declared_version=PROMPT_VERSION)
        self.assertEqual(snapshots.call_count, 0)
        self.assertEqual(due, set())
        self.assertEqual(runner.calls, [])

    def test_due_selector_prepared_pointer_checks_original_files_with_one_history(self):
        job_id, base, _ = self.fixture.fail_closed()
        request = self.memory.snapshot()["jobs"][job_id]["request"]
        hint = closed_model_retry_hint(self.memory, job_id, as_of=fixtures.AFTER)
        prepare_closed_model_retry(self.memory, job_id, as_of=fixtures.AFTER, expected_signal=hint["signal"])
        runner = fixtures.SuccessRunner()
        with patch.object(self.memory, "snapshot", wraps=self.memory.snapshot) as snapshots:
            due = current_model_retry_bindings(self.memory, runner, "B", request["payload"],
                declared_version=PROMPT_VERSION, as_of=fixtures.AFTER)
        self.assertEqual(snapshots.call_count, 1)
        self.assertEqual(due, set(self.memory.snapshot()["jobs"][job_id]["binding_keys"].values()))

        response, usage = runner("B", request["payload"], None)
        atomic_json(base / "completion.json", {"response": response, "usage": usage, "exit_code": 0})
        runner.calls.clear()
        with patch.object(self.memory, "snapshot", wraps=self.memory.snapshot) as snapshots:
            late = current_model_retry_bindings(self.memory, runner, "B", request["payload"],
                declared_version=PROMPT_VERSION, as_of=fixtures.AFTER)
        self.assertEqual(snapshots.call_count, 1)
        self.assertEqual(late, set())
        self.assertEqual(runner.calls, [])

    def test_cooperative_due_selector_checks_turn_and_job_in_same_verified_history(self):
        job_id, base, _ = self.fixture.fail_closed()
        request = self.memory.snapshot()["jobs"][job_id]["request"]
        runner = fixtures.SuccessRunner()
        runner.closed_retry_policy = {"as_of": lambda: fixtures.AFTER, "retry_seconds": 300,
                                      "retry_not_before": lambda: None}
        turn = open_work_turn(self.memory, "single-cooperative-history", at=fixtures.AFTER)
        with bind_work_turn(self.memory, turn["id"]), \
                patch.object(self.memory, "snapshot", wraps=self.memory.snapshot) as snapshots:
            due = current_model_retry_bindings(self.memory, runner, "B", request["payload"],
                declared_version=PROMPT_VERSION)
        self.assertEqual(snapshots.call_count, 1)
        self.assertEqual(due, set(self.memory.snapshot()["jobs"][job_id]["binding_keys"].values()))
        self.assertEqual(runner.calls, [])
        self.assertFalse((base / "retries.json").exists())


if __name__ == "__main__": unittest.main()
