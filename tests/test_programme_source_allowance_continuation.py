"""Spent source turns must yield to ready work; disposable local records only."""

from copy import deepcopy
from datetime import timedelta
import unittest

import product_radar.programme as programme
from product_radar.models import SafetyError, timestamp
from product_radar.research_memory import ResearchMemory
from product_radar.source_coverage import execute_source_run, prepare_source_tasks
from product_radar.storage import Journal
from product_radar.work_turns import (DEFAULT_POLICY, bind_work_turn, close_work_turn,
                                      open_work_turn, reserve_source_callback)
import test_programme as fixtures


class SourceAllowanceContinuationTests(unittest.TestCase):
    def setUp(self):
        fixture = fixtures.ProgrammeTests()
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        self.fixture, self.memory, self.root, self.now = fixture, fixture.memory, fixture.root, fixture.now
        self.record = fixture.prepare()
        self.pid = self.record["programme_id"]
        self.run_id = self.record["source_run_id"]
        self.policy = deepcopy(DEFAULT_POLICY)
        self.policy.update(source_callbacks=1, exploration_slots=0)
        self.calls = []
        prepared = prepare_source_tasks(self.memory, [
            {"route": "meta-ads", "request": {"query": query, "country": "IN"}, "concept_ids": ["first"]}
            for query in ("sink caddy", "draining sink caddy")
        ], run_id=self.run_id, as_of=self.now)
        self.task_ids = prepared["task_ids"]
        self.handlers = {"meta-ads": self.handler}

    def handler(self, context):
        self.calls.append(context["task_id"])
        context["record_dispatch"]()
        return {"schema_version": 1, "route": context["route"], "acquisition_id": context["acquisition_id"],
                "observed_at": self.now, "state": "empty", "dispatch_started": True,
                "reference": context["acquisition_id"] + "/receipt.json", "capture_ids": [],
                "observation_ids": [], "target_results": [],
                "reason": "Synthetic complete empty response.", "retryable": False}

    def source_step(self, memory, _record, context):
        return execute_source_run(memory, self.run_id, handlers=self.handlers,
            as_of=self.now, action_limit=context["source_limit"],
            bounded_call=lambda callback, supplied, _timeout: callback(supplied))

    def spend_original(self, *, ready_lanes=None):
        turn = open_work_turn(self.memory, self.pid, at=self.now, policy=self.policy, ready_lanes=ready_lanes)
        with bind_work_turn(self.memory, turn["id"]):
            result = self.source_step(self.memory, self.record, {"source_limit": 1})
        self.assertEqual(result["handler_calls"], 1)
        state = self.memory.snapshot()
        self.assertEqual(len(self.calls), 1)
        untouched = [tid for tid in self.task_ids if tid not in self.calls]
        self.assertEqual(len(untouched), 1)
        self.untouched = untouched[0]
        task = state["query_tasks"][self.untouched]
        self.assertEqual(task["status"], "prepared")
        self.assertEqual(task["tries"], 0)
        self.assertFalse(task["attempt_refs"])
        self.assertFalse(task.get("reconcile_required"))
        original = deepcopy(state["batch_runs"][turn["id"]])
        self.assertEqual(original["counters"]["source_callbacks"], 1)
        return original

    def advance(self, *, memory=None, acquire=True, policy=None):
        steps = self.fixture.noops()
        if acquire:
            steps["acquire"] = self.source_step
        return programme.advance_ready_programme_turn(memory or self.memory, root=self.root,
            programme_id=self.pid, handlers=self.handlers, steps=steps,
            source_limit=1, turn_policy=policy or self.policy, as_of=self.now)

    def readiness(self, *, memory=None):
        memory = memory or self.memory
        return programme.programme_turn_readiness(memory, programme.load_programme(memory, self.pid),
            root=self.root, handlers=self.handlers, steps=self.fixture.noops(), as_of=self.now)

    def assert_original_preserved(self, original):
        saved = self.memory.snapshot()["batch_runs"][original["id"]]
        self.assertEqual(saved["state"], "closed")
        for field in ("policy", "policy_hash", "counters", "reservations"):
            self.assertEqual(saved[field], original[field])

    def test_spent_original_closes_then_untouched_ready_source_runs_without_approval(self):
        original = self.spend_original()
        first = self.advance()
        self.assertEqual(first["work_turn"]["id"], original["id"])
        self.assertFalse(first["meaningful_progress"])
        self.assertEqual(len(self.calls), 1)
        self.assert_original_preserved(original)
        self.assertEqual(first["next_action"], "continue_ready_work")
        second = self.advance()
        self.assertTrue(second["round_completed"])
        self.assertNotEqual(second["work_turn"]["id"], original["id"])
        self.assertEqual(second["work_turn"]["counters"]["source_callbacks"], 1)
        self.assertEqual(self.calls.count(self.untouched), 1)
        self.assertEqual(len(self.calls), 2)
        third = self.advance()
        self.assertFalse(third["meaningful_progress"])
        self.assertEqual(third["next_action"], "wait_for_saved_trigger")
        fourth = self.advance()
        self.assertEqual(fourth["state"], "all_waiting")
        self.assertFalse(fourth["round_completed"])
        self.assertEqual(len(self.calls), 2)
        self.assert_original_preserved(original)

    def test_older_false_checkpoint_recovers_after_a_fresh_memory_restart(self):
        original = self.spend_original()
        self.advance()
        record = programme.load_programme(self.memory, self.pid)
        record["cooperative_checkpoint"]["continue_ready"] = False
        programme._save(self.memory, record)
        cold = ResearchMemory(self.memory.root, Journal(self.memory.journal.root), evidence_type="synthetic")
        before = cold.snapshot_with_provenance()
        ready = self.readiness(memory=cold)
        self.assertEqual(cold.snapshot_with_provenance(), before)
        self.assertTrue(ready["available"])
        self.assertTrue(ready["ready"])
        result = self.advance(memory=cold)
        self.assertTrue(result["round_completed"])
        self.assertNotEqual(result["work_turn"]["id"], original["id"])
        self.assertEqual(self.calls.count(self.untouched), 1)
        self.assert_original_preserved(original)

    def test_one_later_empty_turn_supersedes_the_spent_original_witness(self):
        original = self.spend_original()
        self.advance(acquire=False)
        first = self.advance(acquire=False)
        self.assertTrue(first["round_completed"])
        self.assertNotEqual(first["work_turn"]["id"], original["id"])
        self.assertEqual(first["work_turn"]["counters"]["source_callbacks"], 0)
        self.assertEqual(first["next_action"], "wait_for_saved_trigger")
        before = self.memory.snapshot_with_provenance()
        for _ in range(2):
            waiting = self.advance(acquire=False)
            self.assertEqual(waiting["state"], "all_waiting")
            self.assertFalse(waiting["round_completed"])
        self.assertEqual(self.memory.snapshot_with_provenance(), before)
        self.assertEqual(len(self.calls), 1)
        self.assert_original_preserved(original)

    def test_backlog_lane_withheld_capacity_yields_without_rewriting_original_policy(self):
        self.policy.update(source_callbacks=3, exploration_slots=2)
        original = self.spend_original(ready_lanes={"backlog": True, "exploration": True})
        self.assertLess(original["counters"]["source_callbacks"], original["policy"]["source_callbacks"])
        first = self.advance()
        self.assertFalse(first["meaningful_progress"])
        self.assertEqual(first["next_action"], "continue_ready_work")
        self.assertEqual(len(self.calls), 1)
        self.assert_original_preserved(original)
        second = self.advance()
        self.assertTrue(second["round_completed"])
        self.assertNotEqual(second["work_turn"]["id"], original["id"])
        self.assertEqual(self.calls.count(self.untouched), 1)
        self.assertEqual(len(self.calls), 2)
        self.assert_original_preserved(original)

    def test_unused_closed_turn_does_not_authorize_more_empty_rounds(self):
        original = open_work_turn(self.memory, self.pid, at=self.now, policy=self.policy)
        first = self.advance(acquire=False)
        self.assertEqual(first["work_turn"]["id"], original["id"])
        self.assertEqual(first["work_turn"]["counters"]["source_callbacks"], 0)
        self.assertEqual(first["next_action"], "wait_for_saved_trigger")
        before = self.memory.snapshot_with_provenance()
        self.assertTrue(self.readiness()["available"])
        self.assertFalse(self.readiness()["ready"])
        self.assertEqual(self.advance(acquire=False)["state"], "all_waiting")
        self.assertEqual(self.memory.snapshot_with_provenance(), before)
        self.assertFalse(self.calls)

    def test_single_slot_next_turn_still_honours_real_lane_alternation(self):
        self.policy.update(source_callbacks=1, exploration_slots=1)
        both = {"backlog": True, "exploration": True}
        first = open_work_turn(self.memory, self.pid, at=self.now, policy=self.policy, ready_lanes=both)
        with bind_work_turn(self.memory, first["id"]):
            reserve_source_callback(self.memory, "synthetic-first-exploration", at=self.now, lane="exploration")
        close_work_turn(self.memory, first["id"], at=self.now, reason="Synthetic finite exploration turn completed.")
        saved = deepcopy(self.memory.snapshot()["batch_runs"][first["id"]])
        before = self.memory.snapshot_with_provenance()
        hint = programme._closed_source_allowance_continuation(before["state"], self.record, ready_lanes=both)
        self.assertEqual(hint["turn_id"], first["id"])
        self.assertEqual(self.memory.snapshot_with_provenance(), before)
        second = open_work_turn(self.memory, self.pid, at=self.now, policy=self.policy, ready_lanes=both)
        self.assertEqual(second["programme_turn_sequence"], 2)
        with bind_work_turn(self.memory, second["id"]):
            with self.assertRaisesRegex(SafetyError, "WORK_TURN_SOURCE_LIMIT"):
                reserve_source_callback(self.memory, "wrong-second-exploration", at=self.now, lane="exploration")
            result = self.source_step(self.memory, self.record, {"source_limit": 1})
        self.assertEqual(result["handler_calls"], 1)
        self.assertEqual(len(self.calls), 1)
        self.assertEqual(self.memory.snapshot()["batch_runs"][first["id"]], saved)

    def test_future_source_wait_does_not_borrow_an_exhausted_allowance_witness(self):
        original = self.spend_original()
        task = self.memory.snapshot()["query_tasks"][self.untouched]
        future = (timestamp(self.now) + timedelta(hours=1)).isoformat()
        self.memory.put("query_tasks", self.untouched, dict(task, next_retry_at=future), previous=task)
        first = self.advance()
        self.assertEqual(first["next_action"], "wait_for_saved_trigger")
        self.assertFalse(self.readiness()["ready"])
        waiting = self.advance()
        self.assertEqual(waiting["state"], "all_waiting")
        self.assertEqual(waiting["next_retry_at"], future)
        self.assertEqual(len(self.calls), 1)
        self.assert_original_preserved(original)

    def test_uncertain_original_without_ready_saved_reply_does_not_wake(self):
        original = self.spend_original()
        task = self.memory.snapshot()["query_tasks"][self.untouched]
        self.memory.put("query_tasks", self.untouched,
            dict(task, status="dispatching", reconcile_required=True), previous=task)
        def unresolved(_context):
            return {"ready": False, "local_saved": False, "bypass_public_pause": False,
                    "reason": "Synthetic original has no returned reply."}
        def forbidden(_context):
            self.fail("Uncertain original with no ready reply must not call a fresh or reconciliation leaf.")
        self.handlers = {"meta-ads": {"handler": forbidden, "reconcile": forbidden, "source_due_hint": unresolved}}
        first = self.advance()
        self.assertEqual(first["next_action"], "wait_for_saved_trigger")
        self.assertFalse(self.readiness()["ready"])
        self.assertEqual(self.advance()["state"], "all_waiting")
        self.assertEqual(len(self.calls), 1)
        self.assert_original_preserved(original)


if __name__ == "__main__":
    unittest.main()
