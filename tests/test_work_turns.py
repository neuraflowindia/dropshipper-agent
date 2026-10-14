"""Isolated canonical turn/admission proofs; no real model or provider calls."""

from copy import deepcopy
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from product_radar.models import SafetyError, digest
from product_radar.fit_policy import ACTIVE_CRITERION_IDS, FIT_POLICY_ID
from product_radar.research_ai import Jobs, MODEL, _item_bindings, prompt_version
from product_radar.research_memory import ResearchMemory, change, key
from product_radar.storage import Journal, atomic_json, load_json
from product_radar.work_turns import (
    DEFAULT_POLICY, TURN_KIND, active_work_turn, admit_model_job, bind_work_turn, close_work_turn,
    new_model_work_wait, open_work_turn, reserve_source_callback, reserve_stage_items,
)

NOW = "2026-10-09T10:00:00+00:00"
LATER = "2026-10-09T10:15:00+00:00"


def payload(*ids):
    return {"items": [{"id": item_id, "source_text": {"title": "Synthetic " + item_id}} for item_id in ids]}


class Runner:
    identity = "synthetic-work-turn-v1"

    def __init__(self, failure=None):
        self.calls = []
        self.failure = failure

    def __call__(self, role, supplied, folder):
        self.calls.append((role, deepcopy(supplied), folder))
        if self.failure is not None:
            raise SafetyError(self.failure)
        return {"results": [{"id": item["id"], "kind": "uninterpretable", "products": [],
                             "reason": "Synthetic response only."} for item in supplied["items"]]}, {"accounting": "synthetic"}


class WorkTurnTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.root = root
        self.memory = ResearchMemory(root / "research", Journal(root / "finance"), evidence_type="synthetic")

    def tearDown(self):
        self.tmp.cleanup()

    def open(self, programme="programme1", **kwargs):
        return open_work_turn(self.memory, programme, at=NOW, **kwargs)

    def turn(self, turn_id):
        return self.memory.snapshot()["batch_runs"][turn_id]

    def events(self):
        return list((self.memory.root / "events").glob("*.json"))

    def request(self, runner, supplied, role="A"):
        version = prompt_version(role, supplied)
        request = {"role": role, "payload": supplied, "model": MODEL, "prompt_version": version, "runner": runner.identity}
        return key("job", request), request, _item_bindings(role, supplied, MODEL, version, runner.identity)

    def admit(self, runner, supplied, role="A", attempt="original"):
        job_id, request, bindings = self.request(runner, supplied, role)
        return admit_model_job(self.memory, job_id, request, bindings, attempt=attempt, at=NOW)

    def test_one_open_turn_restart_preserves_policy_counters_and_sequence(self):
        first = self.open()
        with bind_work_turn(self.memory, first["id"]):
            reserve_source_callback(self.memory, "callback1", at=NOW)
        restarted = ResearchMemory(self.memory.root, Journal(self.root / "finance"), evidence_type="synthetic")
        resumed = open_work_turn(restarted, "programme1", at=LATER)
        self.assertEqual(resumed["id"], first["id"])
        self.assertEqual(resumed["counters"]["source_callbacks"], 1)
        peer = open_work_turn(restarted, "programme2", at=LATER)
        self.assertEqual(peer["turn_sequence"], first["turn_sequence"] + 1)
        close_work_turn(restarted, first["id"], at=LATER, reason="Completed finite round.")
        following = open_work_turn(restarted, "programme1", at=LATER)
        self.assertEqual(following["turn_sequence"], peer["turn_sequence"] + 1)
        self.assertEqual(following["counters"]["source_callbacks"], 0)

    def test_changed_policy_cannot_reset_open_turn(self):
        first = self.open()
        different = deepcopy(DEFAULT_POLICY)
        different["source_callbacks"] = 20
        with self.assertRaisesRegex(SafetyError, "WORK_TURN_POLICY_CHANGED"):
            self.open(policy=different)
        self.assertEqual(self.turn(first["id"])["policy"], DEFAULT_POLICY)

    def test_explicit_preservation_keeps_original_policy_and_all_counters(self):
        first = self.open()
        with bind_work_turn(self.memory, first["id"]):
            reserve_source_callback(self.memory, "already-started", at=NOW)
        original = deepcopy(self.turn(first["id"]))
        different = deepcopy(DEFAULT_POLICY)
        different.update(source_callbacks=2, exploration_slots=2)
        before = self.memory.snapshot_with_provenance()
        resumed = self.open(policy=different, preserve_open_policy=True)
        self.assertEqual(resumed, original)
        self.assertEqual(self.memory.snapshot_with_provenance(), before)
        close_work_turn(self.memory, first["id"], at=LATER, reason="Original finite turn completed.")
        following = self.open(policy=different, preserve_open_policy=True)
        self.assertEqual(following["policy"], different)
        self.assertEqual(following["counters"]["source_callbacks"], 0)

    def test_policy_preservation_does_not_accept_invalid_flag_or_configuration(self):
        self.open()
        before = self.memory.snapshot_with_provenance()
        for flag in (None, 1, "yes"):
            with self.subTest(flag=flag), self.assertRaisesRegex(SafetyError, "WORK_TURN_POLICY_INVALID"):
                self.open(preserve_open_policy=flag)
        with self.assertRaisesRegex(SafetyError, "WORK_TURN_POLICY_INVALID"):
            self.open(policy={}, preserve_open_policy=True)
        self.assertEqual(self.memory.snapshot_with_provenance(), before)

    def test_context_binding_is_nested_and_memory_specific(self):
        first, second = self.open(), self.open("programme2")
        other = ResearchMemory(self.root / "other", Journal(self.root / "other-finance"), evidence_type="synthetic")
        self.assertIsNone(active_work_turn(self.memory))
        with bind_work_turn(self.memory, first["id"]):
            self.assertEqual(active_work_turn(self.memory)["id"], first["id"])
            self.assertIsNone(active_work_turn(other))
            with bind_work_turn(self.memory, second["id"]):
                self.assertEqual(active_work_turn(self.memory)["id"], second["id"])
            self.assertEqual(active_work_turn(self.memory)["id"], first["id"])
        self.assertIsNone(active_work_turn(self.memory))

    def test_source_slot_and_actual_task_intent_commit_together(self):
        turn = self.open()
        with bind_work_turn(self.memory, turn["id"]):
            joined = [change("query_tasks", "legacy-task", {"kind": "all_source_task_v1", "intent": "prepared"})]
            first = reserve_source_callback(self.memory, "callback1", at=NOW, changes=joined)
            before = len(self.events())
            self.assertEqual(reserve_source_callback(self.memory, "callback1", at=NOW, changes=joined), first)
            self.assertEqual(len(self.events()), before)
        state = self.memory.snapshot()
        self.assertEqual(state["batch_runs"][turn["id"]]["counters"]["source_callbacks"], 1)
        event = load_json(self.events()[-1])
        self.assertEqual({item["table"] for item in event["changes"]}, {"batch_runs", "query_tasks"})

    def test_source_limit_is_shared_and_missing_callback_return_consumes_slot(self):
        turn = self.open()
        limit = DEFAULT_POLICY["source_callbacks"]  # the configured round allowance (round_limits)
        with bind_work_turn(self.memory, turn["id"]):
            for number in range(limit):
                reserve_source_callback(self.memory, "call" + str(number), at=NOW)
            with self.assertRaisesRegex(SafetyError, "WORK_TURN_SOURCE_LIMIT"):
                reserve_source_callback(self.memory, "saved-only-new-callback", at=NOW)
        self.assertEqual(self.turn(turn["id"])["counters"]["source_callbacks"], limit)

    def test_source_fairness_and_unused_exploration_donation(self):
        turn = self.open(ready_lanes={"exploration": True, "backlog": True})
        limit = DEFAULT_POLICY["source_callbacks"]  # the configured round allowance (round_limits)
        with bind_work_turn(self.memory, turn["id"]):
            for number in range(limit - DEFAULT_POLICY["exploration_slots"]):
                reserve_source_callback(self.memory, "backlog" + str(number), at=NOW)
            with self.assertRaisesRegex(SafetyError, "WORK_TURN_SOURCE_LIMIT"):
                reserve_source_callback(self.memory, "protected", at=NOW)
            reserve_source_callback(self.memory, "explore1", at=NOW, lane="exploration")
            reserve_source_callback(self.memory, "donated", at=NOW, ready_lanes={"exploration": False, "backlog": True})
        self.assertEqual(self.turn(turn["id"])["counters"]["source_callbacks"], limit)

    def test_non_model_photos_share_item_bound_across_followons(self):
        turn = self.open()
        with bind_work_turn(self.memory, turn["id"]):
            reserve_stage_items(self.memory, "photos", "original", 6, at=NOW)
            reserve_stage_items(self.memory, "photos", "followon", 4, at=NOW)
            with self.assertRaisesRegex(SafetyError, "WORK_TURN_PURPOSE_LIMIT"):
                reserve_stage_items(self.memory, "photos", "another-followon", 1, at=NOW)
        self.assertEqual(self.turn(turn["id"])["counters"]["purpose_items"]["photos"], 10)
        self.assertEqual(self.turn(turn["id"])["counters"]["source_callbacks"], 0)

    def test_origin_and_counter_are_same_commit_and_replay_consumes_once(self):
        turn = self.open()
        runner = Runner()
        with bind_work_turn(self.memory, turn["id"]):
            first = self.admit(runner, payload("p1", "p2"))
            before = len(self.events())
            second = self.admit(runner, payload("p1", "p2"))
            self.assertEqual(first, second)
            self.assertEqual(len(self.events()), before)
        row = self.turn(turn["id"])
        self.assertEqual((row["counters"]["model_executions"], row["counters"]["origin_admissions"]), (1, 1))
        event = load_json(self.events()[-1])
        self.assertEqual({item["table"] for item in event["changes"]}, {"batch_runs", "jobs"})

    def test_jobs_leaf_reserves_once_and_saved_result_costs_no_slot(self):
        turn = self.open()
        runner = Runner()
        with bind_work_turn(self.memory, turn["id"]):
            job_id, original = Jobs(self.memory, runner).run("A", payload("p1"))
            _, reused = Jobs(self.memory, runner).run("A", payload("p1"))
        self.assertEqual(original, reused)
        self.assertEqual(len(runner.calls), 1)
        self.assertEqual(self.turn(turn["id"])["counters"]["model_executions"], 1)
        intent = load_json(self.memory.root / "ai_jobs" / job_id / "intent.json")
        self.assertIn("turn_admission", intent)
        self.assertEqual(intent["turn_admission"]["job_id"], job_id)

    def test_crash_after_admission_before_intent_recovers_same_origin(self):
        turn = self.open()
        runner = Runner()
        real_atomic = atomic_json
        crashed = False
        def fail_intent(path, value):
            nonlocal crashed
            if Path(path).name == "intent.json" and not crashed:
                crashed = True
                raise OSError("Synthetic crash before intent write")
            return real_atomic(path, value)
        with bind_work_turn(self.memory, turn["id"]):
            with patch("product_radar.research_ai.atomic_json", side_effect=fail_intent), self.assertRaises(OSError):
                Jobs(self.memory, runner).run("A", payload("p1"))
            self.assertEqual(len(runner.calls), 0)
            self.assertEqual(self.turn(turn["id"])["counters"]["model_executions"], 1)
            Jobs(self.memory, runner).run("A", payload("p1"))
        self.assertEqual(len(runner.calls), 1)
        self.assertEqual(self.turn(turn["id"])["counters"]["model_executions"], 1)

    def test_crash_after_intent_is_uncertain_and_does_not_launch_again(self):
        turn = self.open()
        runner = Runner("AI_PROVIDER_REQUEST_FAILED")
        with bind_work_turn(self.memory, turn["id"]):
            with self.assertRaisesRegex(SafetyError, "AI_PROVIDER_REQUEST_FAILED"):
                Jobs(self.memory, runner).run("A", payload("p1"))
            with self.assertRaisesRegex(SafetyError, "AI_JOB_PENDING_RECONCILIATION"):
                Jobs(self.memory, runner).run("A", payload("p1"))
        self.assertEqual(len(runner.calls), 1)
        self.assertEqual(self.turn(turn["id"])["counters"]["model_executions"], 1)

    def test_carried_never_intented_job_needs_later_service_full_original_batch(self):
        first = self.open()
        runner = Runner()
        original_payload = payload(*["p" + str(number) for number in range(10)])
        with bind_work_turn(self.memory, first["id"]):
            admission = self.admit(runner, original_payload)
        close_work_turn(self.memory, first["id"], at=LATER, reason="Safe finite yield before launch.")
        second = open_work_turn(self.memory, "programme1", at=LATER)
        with bind_work_turn(self.memory, second["id"]):
            _, result = Jobs(self.memory, runner).run("A", payload("p0"))
        self.assertIn("p0", result["accepted"])
        self.assertEqual(len(runner.calls[0][1]["items"]), 10)
        self.assertEqual(self.turn(second["id"])["counters"]["model_executions"], 1)
        self.assertEqual(self.turn(second["id"])["counters"]["origin_admissions"], 0)
        current = next(iter(self.turn(second["id"])["reservations"]["model"].values()))
        self.assertEqual(current["origin_token"], admission["origin_token"])
        self.assertEqual(current["item_count"], 10)

    def test_old_uncertain_job_reconciliation_does_not_reserve_new_service(self):
        first = self.open()
        runner = Runner("AI_PROVIDER_REQUEST_FAILED")
        with bind_work_turn(self.memory, first["id"]), self.assertRaises(SafetyError):
            Jobs(self.memory, runner).run("A", payload("p1"))
        close_work_turn(self.memory, first["id"], at=LATER, reason="Original attempt waits for reconciliation.")
        second = open_work_turn(self.memory, "programme1", at=LATER)
        with bind_work_turn(self.memory, second["id"]), self.assertRaisesRegex(SafetyError, "AI_JOB_PENDING_RECONCILIATION"):
            Jobs(self.memory, runner).run("A", payload("p1"))
        self.assertEqual(self.turn(second["id"])["counters"]["model_executions"], 0)

    def test_one_old_reserved_f_batch_can_exceed_new_fair_size_without_new_origin(self):
        old_policy = deepcopy(DEFAULT_POLICY)
        old_policy["purpose_items"]["F"] = 10
        first = self.open(policy=old_policy)
        runner = Runner()
        supplied = payload(*["supplier" + str(number) for number in range(10)])
        with bind_work_turn(self.memory, first["id"]):
            original = self.admit(runner, supplied, role="F")
        close_work_turn(self.memory, first["id"], at=LATER, reason="Proved not yet intended.")
        second = open_work_turn(self.memory, "programme1", at=LATER)
        with bind_work_turn(self.memory, second["id"]):
            Jobs(self.memory, runner).run("F", payload("supplier0"))
            with self.assertRaisesRegex(SafetyError, "WORK_TURN_PURPOSE_LIMIT"):
                Jobs(self.memory, runner).run("F", payload("extra-supplier"))
        row = self.turn(second["id"])
        service = next(iter(row["reservations"]["model"].values()))
        self.assertTrue(service["carried_purpose_overage"])
        self.assertEqual(service["origin_token"], original["origin_token"])
        self.assertEqual(row["counters"]["purpose_items"]["F"], 10)
        self.assertEqual((row["counters"]["model_executions"], row["counters"]["origin_admissions"]), (1, 0))
        self.assertEqual(len(runner.calls[0][1]["items"]), 10)

    def test_new_large_f_job_cannot_take_carried_exception(self):
        turn = self.open()
        runner = Runner()
        with bind_work_turn(self.memory, turn["id"]), self.assertRaisesRegex(SafetyError, "WORK_TURN_PURPOSE_LIMIT"):
            Jobs(self.memory, runner).run("F", payload(*["supplier" + str(number) for number in range(10)]))
        self.assertEqual(self.turn(turn["id"])["counters"]["model_executions"], 0)
        self.assertEqual(runner.calls, [])

    def test_legacy_metadata_only_overlarge_payload_has_named_policy_wait(self):
        runner = Runner()
        supplied = payload(*["supplier" + str(number) for number in range(10)])
        job_id, request, bindings = self.request(runner, supplied, role="F")
        self.memory.put("jobs", job_id, {"request": request, "binding_keys": bindings})
        turn = self.open()
        with bind_work_turn(self.memory, turn["id"]), self.assertRaisesRegex(SafetyError, "WORK_TURN_LEGACY_PAYLOAD_POLICY_WAIT"):
            Jobs(self.memory, runner).run("F", supplied)
        self.assertEqual(self.turn(turn["id"])["counters"]["model_executions"], 0)

    def test_fit_purpose_items_shared_across_full_partial_and_followon_scopes(self):
        turn = self.open()
        runner = Runner()
        with bind_work_turn(self.memory, turn["id"]):
            full = {**payload(*["fit" + str(number) for number in range(8)]), "mode": "fit_assessment", "fit_policy_id": FIT_POLICY_ID,
                    "requested_criterion_ids": list(ACTIVE_CRITERION_IDS)}
            partial = {**payload("followon1", "followon2"), "mode": "fit_partial_review", "fit_policy_id": FIT_POLICY_ID,
                       "requested_criterion_ids": [1]}
            self.admit(runner, full, role="E")
            self.admit(runner, partial, role="E")
            with self.assertRaisesRegex(SafetyError, "WORK_TURN_PURPOSE_LIMIT"):
                self.admit(runner, {**partial, **payload("extra-followon")}, role="E")
        row = self.turn(turn["id"])
        self.assertEqual(row["counters"]["purpose_items"]["E:fit"], 10)
        self.assertEqual(row["counters"]["model_executions"], 2)

    def test_ready_frozen_report_has_one_reserved_model_slot_without_role_starvation(self):
        turn = self.open(report_ready=True)
        runner = Runner()
        with bind_work_turn(self.memory, turn["id"]):
            for number in range(9):
                self.admit(runner, payload("a" + str(number)))
            with self.assertRaisesRegex(SafetyError, "WORK_TURN_MODEL_LIMIT"):
                self.admit(runner, payload("blocked-b"), role="B")
            self.admit(runner, payload("ready-report"), role="G")
        self.assertEqual(self.turn(turn["id"])["counters"]["model_executions"], 10)

    def test_distinct_roles_share_ceiling_and_exact_binding_values_are_verified(self):
        policy = deepcopy(DEFAULT_POLICY)
        policy["model_executions"] = 2
        policy["report_slots"] = 0
        turn = self.open(policy=policy)
        runner = Runner()
        with bind_work_turn(self.memory, turn["id"]):
            job_id, request, bindings = self.request(runner, payload("p1"))
            with self.assertRaisesRegex(SafetyError, "WORK_TURN_MODEL_BINDING_INVALID"):
                admit_model_job(self.memory, job_id, request, {"p1": "a" * 64}, attempt="original", at=NOW)
            self.admit(runner, payload("p1"), role="A")
            self.admit(runner, payload("p2"), role="B")
            with self.assertRaisesRegex(SafetyError, "WORK_TURN_MODEL_LIMIT"):
                self.admit(runner, payload("p3"), role="G")
        self.assertEqual(self.turn(turn["id"])["counters"]["model_executions"], 2)

    def test_recursive_old_batch_and_remaining_items_share_one_allowance(self):
        policy = deepcopy(DEFAULT_POLICY)
        policy["model_executions"] = 1
        policy["report_slots"] = 0
        first = self.open(policy=policy)
        runner = Runner()
        with bind_work_turn(self.memory, first["id"]):
            self.admit(runner, payload("old1", "old2"))
        close_work_turn(self.memory, first["id"], at=LATER, reason="Safe yield.")
        second = open_work_turn(self.memory, "programme1", at=LATER, policy=policy)
        with bind_work_turn(self.memory, second["id"]):
            _, result = Jobs(self.memory, runner).run("A", payload("old1", "new1"))
        self.assertIn("old1", result["accepted"])
        self.assertEqual(result["failures"], {"new1": "WORK_TURN_MODEL_LIMIT"})
        self.assertEqual(len(runner.calls), 1)
        self.assertEqual([item["id"] for item in runner.calls[0][1]["items"]], ["old1", "old2"])
        self.assertEqual(self.turn(second["id"])["counters"]["model_executions"], 1)

    def test_saved_result_applies_despite_full_or_closed_turn_and_guard_wait(self):
        first = self.open()
        runner = Runner()
        with bind_work_turn(self.memory, first["id"]):
            job_id, expected = Jobs(self.memory, runner).run("A", payload("p1"))
            close_work_turn(self.memory, first["id"], at=LATER, reason="Completed finite round.")
            self.assertEqual(Jobs(self.memory, runner).run("A", payload("p1")), (job_id, expected))
        second = open_work_turn(self.memory, "programme1", at=LATER)
        def stopped():
            raise SafetyError("PROGRAMME_OWNER_PAUSED")
        with bind_work_turn(self.memory, second["id"], before_model=stopped):
            self.assertEqual(Jobs(self.memory, runner).run("A", payload("p1")), (job_id, expected))
            with self.assertRaisesRegex(SafetyError, "PROGRAMME_OWNER_PAUSED"):
                Jobs(self.memory, runner).run("A", payload("p2"))
        self.assertEqual(self.turn(second["id"])["counters"]["model_executions"], 0)

    def test_fresh_job_spent_allowance_does_not_run_its_preparation_guard(self):
        policy = deepcopy(DEFAULT_POLICY); policy.update(model_executions=1, report_slots=0)
        turn = self.open(policy=policy); runner = Runner(); preparations = []
        with bind_work_turn(self.memory, turn["id"]):
            Jobs(self.memory, runner).run("A", payload("spent"))
            before = self.memory.snapshot_with_provenance()
            with self.assertRaisesRegex(SafetyError, "WORK_TURN_MODEL_LIMIT"):
                Jobs(self.memory, runner, before_new_execution=lambda *_: preparations.append("fresh")).run(
                    "A", payload("new"))
            self.assertEqual(self.memory.snapshot_with_provenance(), before)
        self.assertEqual(preparations, [])
        self.assertEqual(len(runner.calls), 1)

    def test_new_model_hint_is_read_only_and_respects_report_and_purpose_slots(self):
        policy = deepcopy(DEFAULT_POLICY); policy.update(model_executions=3, report_slots=1)
        turn = self.open(policy=policy, report_ready=True); runner = Runner()
        with bind_work_turn(self.memory, turn["id"]):
            self.admit(runner, payload("a")); self.admit(runner, payload("b"))
            state = self.memory.snapshot(); before = self.memory.snapshot_with_provenance()
            with patch.object(self.memory, "snapshot", side_effect=AssertionError("Use the supplied verified state.")):
                self.assertEqual(new_model_work_wait(self.memory, "A", state=state), "WORK_TURN_MODEL_LIMIT")
                self.assertIsNone(new_model_work_wait(self.memory, "G", state=state))
            self.assertEqual(self.memory.snapshot_with_provenance(), before)
            close_work_turn(self.memory, turn["id"], at=LATER, reason="Finished original allowance.")
            self.assertEqual(new_model_work_wait(self.memory, "G"), "WORK_TURN_CLOSED")
        next_turn = self.open(programme="purpose")
        with bind_work_turn(self.memory, next_turn["id"]):
            self.admit(runner, payload("f1", "f2", "f3", "f4", "f5"), role="F")
            self.assertEqual(new_model_work_wait(self.memory, "F"), "WORK_TURN_PURPOSE_LIMIT")
            self.assertIsNone(new_model_work_wait(self.memory, "B"))

    def test_new_model_hint_without_bound_turn_does_not_read_history(self):
        with patch.object(self.memory, "snapshot", side_effect=AssertionError("No bound turn needs no history read.")):
            self.assertIsNone(new_model_work_wait(self.memory, "B"))

    def test_new_model_hint_refuses_invalid_purpose_or_item_count(self):
        for purpose, count in (([], 1), ("unknown", 1), ("B", True), ("B", 0), ("B", 11)):
            with self.subTest(purpose=purpose, count=count), self.assertRaisesRegex(
                    SafetyError, "WORK_TURN_MODEL_BINDING_INVALID"):
                new_model_work_wait(self.memory, purpose, item_count=count)

    def test_spent_turn_recovers_saved_derived_sibling_without_fresh_guard(self):
        policy = deepcopy(DEFAULT_POLICY); policy.update(model_executions=1, report_slots=0)
        first = self.open(policy=policy); runner = Runner(); preparations = []
        with bind_work_turn(self.memory, first["id"]):
            Jobs(self.memory, runner).run("A", payload("old1", "old2"))
        close_work_turn(self.memory, first["id"], at=LATER, reason="Saved original answer.")
        second = self.open(policy=policy)
        with bind_work_turn(self.memory, second["id"]):
            Jobs(self.memory, runner).run("A", payload("spent"))
            _, result = Jobs(self.memory, runner,
                before_new_execution=lambda *_: preparations.append("fresh")).run("A", payload("old1", "new1"))
        self.assertIn("old1", result["accepted"])
        self.assertEqual(result["failures"], {"new1": "WORK_TURN_MODEL_LIMIT"})
        self.assertEqual(preparations, [])
        self.assertEqual(len(runner.calls), 2)

    def test_spent_turn_resumes_its_existing_never_intented_service(self):
        policy = deepcopy(DEFAULT_POLICY); policy.update(model_executions=1, report_slots=0)
        turn = self.open(policy=policy); runner = Runner(); preparations = []
        with bind_work_turn(self.memory, turn["id"]):
            self.admit(runner, payload("reserved"))
            _, result = Jobs(self.memory, runner,
                before_new_execution=lambda *_: preparations.append("original")).run("A", payload("reserved"))
        self.assertIn("reserved", result["accepted"])
        self.assertEqual(preparations, ["original"])
        self.assertEqual(len(runner.calls), 1)
        self.assertEqual(self.turn(turn["id"])["counters"]["model_executions"], 1)

    def test_leaf_no_turn_retains_legacy_compatibility(self):
        runner = Runner()
        job_id, _ = Jobs(self.memory, runner).run("A", payload("p1"))
        state = self.memory.snapshot()
        self.assertNotIn("turn_admissions", state["jobs"][job_id])
        self.assertFalse(any(row.get("kind") == TURN_KIND for row in state["batch_runs"].values()))

    def test_managed_carried_job_cannot_escape_turn_service_through_legacy_entry(self):
        first = self.open()
        runner = Runner()
        with bind_work_turn(self.memory, first["id"]):
            self.admit(runner, payload("managed"))
        close_work_turn(self.memory, first["id"], at=LATER, reason="Safe yield before intent.")
        with self.assertRaisesRegex(SafetyError, "WORK_TURN_REQUIRED_FOR_CARRIED_JOB"):
            Jobs(self.memory, runner).run("A", payload("managed"))
        self.assertEqual(runner.calls, [])
        self.assertEqual(self.turn(first["id"])["counters"]["model_executions"], 1)

    def test_custom_policy_resumes_without_explicit_reconfiguration(self):
        policy = deepcopy(DEFAULT_POLICY)
        policy["source_callbacks"] = 2
        policy["exploration_slots"] = 2
        first = self.open(policy=policy)
        resumed = open_work_turn(self.memory, "programme1", at=LATER)
        self.assertEqual(resumed["id"], first["id"])
        self.assertEqual(resumed["policy"], policy)

    def test_local_counter_race_rechecks_without_losing_either_reservation(self):
        turn = self.open()
        original_commit = self.memory.commit
        injected = False
        def competing(event_id, changes, *, validate=None):
            nonlocal injected
            if not injected:
                injected = True
                reserve_source_callback(self.memory, "competing-callback", at=NOW)
            return original_commit(event_id, changes, validate=validate)
        with bind_work_turn(self.memory, turn["id"]), patch.object(self.memory, "commit", side_effect=competing):
            reserve_source_callback(self.memory, "original-callback", at=NOW)
        row = self.turn(turn["id"])
        self.assertEqual(row["counters"]["source_callbacks"], 2)
        self.assertEqual({item["owner_key"] for item in row["reservations"]["source"].values()},
                         {"competing-callback", "original-callback"})

    def test_malformed_saved_lifecycle_or_joined_hash_cannot_admit_work(self):
        for field, value in (("opened_at", None), ("closed_at", NOW), ("close_reason", "Unexpected close reason"), ("cursors", [])):
            with self.subTest(field=field):
                turn = self.open("malformed-" + field)
                broken = deepcopy(turn)
                broken[field] = value
                self.memory.put("batch_runs", turn["id"], broken, previous=turn)
                with self.assertRaises(SafetyError):
                    with bind_work_turn(self.memory, turn["id"]):
                        reserve_source_callback(self.memory, "never-launch", at=NOW)
                self.memory.put("batch_runs", turn["id"], turn, previous=broken)
        turn = self.open("malformed-hash")
        with bind_work_turn(self.memory, turn["id"]):
            reserve_source_callback(self.memory, "original", at=NOW)
        saved = self.turn(turn["id"])
        broken = deepcopy(saved)
        next(iter(broken["reservations"]["source"].values()))["joined_changes_hash"] = "bad-hash"
        self.memory.put("batch_runs", turn["id"], broken, previous=saved)
        with self.assertRaises(SafetyError):
            with bind_work_turn(self.memory, turn["id"]):
                reserve_source_callback(self.memory, "never-launch", at=NOW)

    def test_closed_turn_cannot_admit_new_callback_or_model(self):
        turn = self.open()
        with bind_work_turn(self.memory, turn["id"]):
            close_work_turn(self.memory, turn["id"], at=LATER, reason="Safe turn close.")
            with self.assertRaisesRegex(SafetyError, "WORK_TURN_CLOSED"):
                reserve_source_callback(self.memory, "new-call", at=LATER)
            with self.assertRaisesRegex(SafetyError, "WORK_TURN_CLOSED"):
                Jobs(self.memory, Runner()).run("A", payload("p1"))


class ReviewShareTests(unittest.TestCase):
    """Definition and functional market-check reviews keep a protected share."""

    setUp, tearDown = WorkTurnTests.setUp, WorkTurnTests.tearDown
    open, turn, request, admit = WorkTurnTests.open, WorkTurnTests.turn, WorkTurnTests.request, WorkTurnTests.admit

    DEFINITION = {"role": "B", "payload": {"mode": "functional_definition_review", "items": []}}
    FUNCTIONAL_SOURCE = {"role": "E", "payload": {"mode": "marketplace_relevance_only",
                                                  "relevance_style": "functional-source-scope-v4", "items": []}}

    def synthetic_turn(self, *, report_ready=False, review_ready=True, jobs=()):
        reservations = {"token-" + job_id: {"purpose": purpose, "job_id": job_id} for job_id, purpose in jobs}
        return {"policy": deepcopy(DEFAULT_POLICY), "report_ready": report_ready, "review_ready": review_ready,
                "reservations": {"model": reservations}}

    def test_only_definition_and_functional_market_check_requests_are_reviews(self):
        from product_radar.work_turns import review_model_request
        self.assertTrue(review_model_request(self.DEFINITION))
        self.assertTrue(review_model_request(self.FUNCTIONAL_SOURCE))
        legacy_source = deepcopy(self.FUNCTIONAL_SOURCE)
        legacy_source["payload"]["relevance_style"] = "short-references-support-v3"
        for request in (legacy_source, {"role": "B", "payload": {"mode": "functional_alias_review"}},
                        {"role": "B", "payload": {"items": []}}, {"role": "A", "payload": {"mode": "functional_definition_review"}},
                        {"role": "B", "payload": None}, None):
            with self.subTest(request=request):
                self.assertFalse(review_model_request(request))

    def test_review_share_is_clamped_and_never_grows_the_total(self):
        from product_radar.work_turns import review_share
        cases = (((10, 1, 2), 2), ((1, 0, 2), 0), ((2, 0, 2), 1), ((3, 1, 2), 1), ((10, 1, 0), 0))
        for (total, report, review), expected in cases:
            with self.subTest(total=total, report=report, review=review):
                self.assertEqual(review_share({"model_executions": total, "report_slots": report,
                                               "review_slots": review}), expected)
        self.assertEqual(review_share({"model_executions": 10, "report_slots": 1}), 0)

    def test_protected_slots_combine_writing_and_unused_review_shares(self):
        from product_radar.work_turns import protected_model_slots
        jobs = {"review-job": {"request": self.DEFINITION}, "other-job": {"request": {"role": "A", "payload": {}}}}
        fresh = self.synthetic_turn()
        self.assertEqual(protected_model_slots(fresh, "A", jobs=jobs), 2)
        self.assertEqual(protected_model_slots(fresh, "B", review=True, jobs=jobs), 0)
        self.assertEqual(protected_model_slots(fresh, "G", jobs=jobs), 2)
        used = self.synthetic_turn(jobs=[("review-job", "B"), ("other-job", "A")])
        self.assertEqual(protected_model_slots(used, "A", jobs=jobs), 1)
        both = self.synthetic_turn(report_ready=True)
        self.assertEqual(protected_model_slots(both, "A", jobs=jobs), 3)
        self.assertEqual(protected_model_slots(both, "G", jobs=jobs), 2)
        self.assertEqual(protected_model_slots(both, "E:source_support", review=True, jobs=jobs), 1)
        self.assertEqual(protected_model_slots(self.synthetic_turn(review_ready=False), "A", jobs=jobs), 0)

    def test_hint_reserves_review_share_only_while_review_work_is_due(self):
        policy = deepcopy(DEFAULT_POLICY); policy.update(model_executions=3, report_slots=0)
        runner = Runner()
        due = self.open(programme="review-due", policy=policy, review_ready=True)
        with bind_work_turn(self.memory, due["id"]):
            self.admit(runner, payload("ordinary"))
            state = self.memory.snapshot()
            self.assertEqual(new_model_work_wait(self.memory, "A", state=state), "WORK_TURN_MODEL_LIMIT")
            self.assertIsNone(new_model_work_wait(self.memory, "B", state=state, review=True))
            self.assertIsNone(new_model_work_wait(self.memory, "E:source_support", state=state, review=True))
            with self.assertRaisesRegex(SafetyError, "WORK_TURN_MODEL_LIMIT"):
                self.admit(runner, payload("second-ordinary"))
            with self.assertRaisesRegex(SafetyError, "WORK_TURN_MODEL_BINDING_INVALID"):
                new_model_work_wait(self.memory, "B", review="yes")
        self.assertIs(self.turn(due["id"])["review_ready"], True)
        idle = self.open(programme="review-idle", policy=policy)
        with bind_work_turn(self.memory, idle["id"]):
            self.admit(runner, payload("first")); self.admit(runner, payload("second"))
            self.assertIsNone(new_model_work_wait(self.memory, "A"))
        self.assertIs(self.turn(idle["id"])["review_ready"], False)

    def test_saved_policy_without_review_share_stays_valid_and_byte_exact(self):
        policy = deepcopy(DEFAULT_POLICY); del policy["review_slots"]
        turn = self.open(programme="older-policy", policy=policy, review_ready=True)
        saved = self.turn(turn["id"])
        self.assertNotIn("review_slots", saved["policy"])
        self.assertEqual(saved["policy_hash"], digest(policy))
        with bind_work_turn(self.memory, turn["id"]):
            self.admit(Runner(), payload("ordinary"))
            self.assertIsNone(new_model_work_wait(self.memory, "A"))
        current = self.turn(turn["id"]); legacy_row = deepcopy(current); del legacy_row["review_ready"]
        self.memory.put("batch_runs", turn["id"], legacy_row, previous=current)
        self.assertEqual(open_work_turn(self.memory, "older-policy", at=LATER)["id"], turn["id"])

    def test_malformed_review_share_or_flag_is_refused(self):
        for value in (-1, 1001, True, "2"):
            policy = deepcopy(DEFAULT_POLICY); policy["review_slots"] = value
            with self.subTest(value=value), self.assertRaisesRegex(SafetyError, "WORK_TURN_POLICY_INVALID"):
                self.open(programme="bad-share", policy=policy)
        extra = deepcopy(DEFAULT_POLICY); extra["unknown_slots"] = 1
        with self.assertRaisesRegex(SafetyError, "WORK_TURN_POLICY_INVALID"):
            self.open(programme="bad-share", policy=extra)
        with self.assertRaisesRegex(SafetyError, "WORK_TURN_READINESS_INVALID"):
            self.open(programme="bad-flag", review_ready="yes")
        turn = self.open(programme="bad-saved-flag")
        saved = self.turn(turn["id"]); broken = deepcopy(saved); broken["review_ready"] = "yes"
        self.memory.put("batch_runs", turn["id"], broken, previous=saved)
        with self.assertRaisesRegex(SafetyError, "WORK_TURN_RECORD_INVALID"):
            open_work_turn(self.memory, "bad-saved-flag", at=LATER)

    def test_stage_item_hint_reports_spent_photo_allowance_without_writing(self):
        from product_radar.work_turns import stage_item_wait
        self.assertIsNone(stage_item_wait(self.memory, "photos"))
        policy = deepcopy(DEFAULT_POLICY); policy["purpose_items"]["photos"] = 1
        turn = self.open(policy=policy)
        with bind_work_turn(self.memory, turn["id"]):
            self.assertIsNone(stage_item_wait(self.memory, "photos"))
            reserve_stage_items(self.memory, "photos", "photo-owner", 1, at=NOW)
            before = self.memory.snapshot_with_provenance()
            self.assertEqual(stage_item_wait(self.memory, "photos", state=self.memory.snapshot()), "WORK_TURN_PURPOSE_LIMIT")
            self.assertEqual(self.memory.snapshot_with_provenance(), before)
            close_work_turn(self.memory, turn["id"], at=LATER, reason="Synthetic photo allowance closed.")
            self.assertEqual(stage_item_wait(self.memory, "photos"), "WORK_TURN_CLOSED")
        for purpose, count in (("model", 1), ("photos", 0), ("photos", True)):
            with self.subTest(purpose=purpose, count=count), self.assertRaisesRegex(SafetyError, "WORK_TURN_PURPOSE_INVALID"):
                stage_item_wait(self.memory, purpose, item_count=count)


if __name__ == "__main__":
    unittest.main()
