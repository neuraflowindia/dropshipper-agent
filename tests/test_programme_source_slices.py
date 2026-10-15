"""Programme joins for finite source inspections; disposable records only.

The source-slice mechanics have their own tests. These cases exercise the
real programme, turn lifecycle, effective saved cursor and wake checkpoint.
No source collector, model, process signal or provider is used.
"""

from copy import deepcopy
from datetime import timedelta
import unittest
from unittest.mock import patch

import test_programme as fixtures

import product_radar.programme as programme
from product_radar.models import digest, timestamp
from product_radar.research_memory import ResearchMemory, change, key
from product_radar.source_coverage import (
    _stage_receipt, execute_source_run, prepare_source_tasks, source_access_context,
)
from product_radar.work_turns import (
    DEFAULT_POLICY, active_work_turn, bind_work_turn, open_work_turn,
    reserve_source_callback,
)


class ProgrammeSourceSliceTests(unittest.TestCase):
    def setUp(self):
        self.fixture = fixtures.ProgrammeTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.memory, self.root, self.now = (
            self.fixture.memory, self.fixture.root, self.fixture.now)
        self.fixture.prepare()

    def prepare_sources(self, count=3):
        return prepare_source_tasks(self.memory, [
            {"route": "meta-ads", "request": {
                "query": "synthetic sink caddy " + str(number), "country": "IN"},
             "concept_ids": ["first"]}
            for number in range(count)
        ], run_id="existing-source-run", as_of=self.now)

    def save_inspection(self, next_index):
        """Write the real advisory format, not an invented research result."""
        run_key = key("source-run", "existing-source-run")
        previous = self.memory.snapshot()["batch_runs"][run_key]
        members = previous["task_ids"]
        turn = active_work_turn(self.memory)
        raw = {"contract": "source-inspection-slice-v1",
               "scope_hash": digest(members), "ceiling": len(members),
               "next_index": next_index, "sweep": 0,
               "turn_id": turn["id"] if turn else None}
        self.memory.put("batch_runs", run_key,
            dict(previous, inspection_continuation=raw), previous=previous)
        return {"handler_calls": 0, "fresh_dispatches": 0,
                "inspection_pending": next_index < len(members),
                "inspection_scanned": 1,
                "inspection_remaining": len(members) - next_index}

    def advance_ready(self, **options):
        return programme.advance_ready_programme_turn(
            self.memory, root=self.root, programme_id="whole-programme",
            handlers=options.pop("handlers", {}), as_of=options.pop("as_of", self.now),
            steps=options.pop("steps", self.fixture.noops()), **options)

    def empty_receipt(self, context):
        return {"schema_version": 1, "route": context["route"],
                "acquisition_id": context["acquisition_id"], "observed_at": self.now,
                "state": "empty", "dispatch_started": True,
                "reference": context["acquisition_id"] + "/receipt.json",
                "capture_ids": [], "observation_ids": [], "target_results": [],
                "reason": "Synthetic completed empty reply.", "retryable": False}

    def uncertain_source_fixture(self, *, with_sibling=False):
        """One original loses its reply; an optional later member is healthy."""
        self.prepare_sources(2 if with_sibling else 1)
        run = self.memory.snapshot()["batch_runs"][key("source-run", "existing-source-run")]
        original_id = run["task_ids"][0]
        calls = []

        def callback(context):
            calls.append((context["task_id"], context["acquisition_id"], context["reconciling"]))
            if not context["reconciling"]:
                context["record_dispatch"]()
            if context["task_id"] == original_id:
                raise TimeoutError("Synthetic original reply has not returned.")
            return self.empty_receipt(context)

        handlers = {"meta-ads": {"handler": callback, "reconcile": callback}}
        steps = self.fixture.noops()

        def acquire(memory, _record, context):
            return execute_source_run(memory, "existing-source-run", handlers=handlers,
                as_of=context["fixed_as_of"], action_limit=context["source_limit"],
                bounded_call=lambda callback, supplied, _timeout: callback(supplied))

        steps["acquire"] = acquire
        policy = dict(DEFAULT_POLICY, source_callbacks=1, exploration_slots=0)
        options = {"handlers": handlers, "steps": steps, "source_limit": 1, "turn_policy": policy}
        return original_id, run["task_ids"], calls, options

    def test_quota_filled_pending_slice_does_not_revisit_parent_before_later_stages(self):
        record = programme.load_programme(self.memory, "whole-programme")
        self.fixture.product("later-child")
        programme._register_discovered_products(self.memory, record,
            {"new_concept_ids": ["later-child"]}, self.now)
        programme._save(self.memory, record)
        child = programme._review_scopes(record)[1]["programme_id"]
        calls, later = [], []
        steps = self.fixture.noops()

        def acquire(_memory, scope, context):
            calls.append((scope["programme_id"], context["source_limit"]))
            is_parent = scope["programme_id"] == "whole-programme"
            # Manufactured callback counts isolate the programme's fair
            # scope selection. They are not actual dispatch reservations.
            return {"handler_calls": context["source_limit"] if is_parent else 0,
                    "fresh_dispatches": context["source_limit"] if is_parent else 0,
                    "inspection_pending": is_parent,
                    "inspection_remaining": 5 if is_parent else 0}

        steps["acquire"] = acquire
        for stage in ("match", "suppliers", "photos", "reports", "private_staging"):
            def observed(_memory, scope, _context, stage=stage):
                later.append((stage, scope["programme_id"]))
                return {"state": "nothing_new"}
            steps[stage] = observed
        result = programme.advance_programme(self.memory, "whole-programme",
            root=self.root, handlers={}, steps=steps, source_limit=12,
            max_cycles=1, as_of=self.now)
        self.assertEqual(calls, [("whole-programme", 6), (child, 6)])
        saved = programme.load_programme(self.memory, "whole-programme")
        self.assertEqual(saved["acquisition_budget"]["budget_units_consumed"], 6)
        self.assertEqual(saved["acquisition_budget"]["handler_calls"], 6)
        self.assertTrue(saved["last_results"]["acquire"]["inspection_pending"])
        for stage in ("match", "suppliers", "photos", "reports", "private_staging"):
            self.assertIn((stage, "whole-programme"), later)
        self.assertEqual(result["tick_state"], "work_slice_finished_remaining_work_kept")
        self.assertEqual(self.memory.snapshot()["published_history"], {})

    def test_cursor_only_progress_wakes_after_close_and_cold_restart_without_fake_research(self):
        self.prepare_sources()
        original = programme.load_programme(self.memory, "whole-programme")
        before = self.memory.snapshot()
        steps = self.fixture.noops()
        steps["acquire"] = lambda *_args: self.save_inspection(1)
        result = self.advance_ready(steps=steps)
        self.assertTrue(result["round_completed"])
        self.assertFalse(result["meaningful_progress"])
        self.assertEqual(result["next_action"], "continue_ready_work")
        self.assertEqual(result["work_turn"]["state"], "closed")
        self.assertEqual(result["work_turn"]["counters"]["source_callbacks"], 0)
        latest = programme.load_programme(self.memory, "whole-programme")
        after = self.memory.snapshot()
        self.assertTrue(latest["cooperative_checkpoint"]["continue_ready"])
        self.assertEqual(before["query_tasks"], after["query_tasks"])
        self.assertEqual(programme._signature(before, original),
                         programme._signature(after, latest))
        # A normal turn may also prepare inventory bookkeeping. Isolate the
        # inspection position here so that unrelated preparation cannot hide
        # whether cursor movement is falsely counted as accepted research.
        without_inspection = deepcopy(after)
        without_inspection["batch_runs"][key("source-run", "existing-source-run")].pop(
            "inspection_continuation")
        self.assertEqual(programme._cooperative_signature(without_inspection, latest),
                         programme._cooperative_signature(after, latest))
        # Reopen the disposable store so wake correctness cannot rely on an
        # in-memory counter, an active turn binding or a cached cursor.
        self.memory = ResearchMemory(self.memory.root, self.memory.journal,
                                     evidence_type="synthetic")
        latest = programme.load_programme(self.memory, "whole-programme")
        readiness = programme.programme_turn_readiness(self.memory, latest,
            root=self.root, handlers={}, steps=self.fixture.noops(), as_of=self.now)
        self.assertTrue(readiness["ready"])
        self.assertTrue(readiness["source_recovery_pending"])
        self.assertEqual(readiness["reason"], "independent_ready_work")

    def test_completed_unchanged_cohort_settles_without_more_empty_turns(self):
        self.prepare_sources()
        visits = []
        steps = self.fixture.noops()

        def acquire(*_args):
            visits.append(True)
            return self.save_inspection(2 if len(visits) == 1 else 3)

        steps["acquire"] = acquire
        first = self.advance_ready(steps=steps)
        self.assertEqual(first["next_action"], "continue_ready_work")
        second = self.advance_ready(steps=steps)
        self.assertTrue(second["round_completed"])
        self.assertFalse(second["meaningful_progress"])
        self.assertEqual(second["next_action"], "wait_for_saved_trigger")
        self.assertEqual(second["tick_state"], "fixed_point_with_saved_remaining_work")
        latest = programme.load_programme(self.memory, "whole-programme")
        self.assertFalse(latest["cooperative_checkpoint"]["continue_ready"])
        third = self.advance_ready(steps=steps)
        self.assertEqual(third["state"], "all_waiting")
        self.assertFalse(third["round_completed"])
        self.assertEqual(len(visits), 2)
        turns = [row for row in self.memory.snapshot()["batch_runs"].values()
                 if row.get("kind") == "work-turn-v1"]
        self.assertEqual(len(turns), 2)
        self.assertTrue(all(row["state"] == "closed" for row in turns))
        self.assertTrue(all(row["counters"]["source_callbacks"] == 0 for row in turns))

    def test_original_open_allowance_survives_slice_and_different_caller_policy(self):
        policy = deepcopy(DEFAULT_POLICY)
        policy["exploration_slots"] = 0
        policy["source_callbacks"] = 12  # an original allowance unlike the configured default
        turn = open_work_turn(self.memory, "whole-programme", at=self.now,
            policy=policy, ready_lanes={"backlog": True, "exploration": False},
            report_ready=True)
        with bind_work_turn(self.memory, turn["id"]):
            for number in range(10):
                reserve_source_callback(self.memory, "synthetic-original-" + str(number),
                    at=self.now, ready_lanes={"backlog": True, "exploration": False})
        original = deepcopy(self.memory.snapshot()["batch_runs"][turn["id"]])
        self.assertEqual(original["counters"]["source_callbacks"], 10)
        seen, later = [], []
        steps = self.fixture.noops()

        def acquire(memory, _record, _context):
            seen.append(deepcopy(active_work_turn(memory)))
            return {"handler_calls": 0, "fresh_dispatches": 0,
                    "inspection_pending": True, "inspection_remaining": 10}

        steps["acquire"] = acquire
        for stage in ("match", "suppliers", "photos", "reports", "private_staging"):
            def observed(*_args, stage=stage):
                later.append(stage)
                return {"state": "nothing_new"}
            steps[stage] = observed
        result = programme.advance_programme_turn(self.memory, "whole-programme",
            root=self.root, handlers={}, steps=steps, as_of=self.now,
            turn_policy=dict(policy, source_callbacks=1), report_ready=False)
        self.assertEqual(result["work_turn"]["id"], original["id"])
        self.assertEqual(seen, [original])
        final = self.memory.snapshot()["batch_runs"][original["id"]]
        self.assertEqual(final["state"], "closed")
        for field in ("policy", "policy_hash", "counters", "reservations",
                      "report_ready", "turn_sequence", "programme_turn_sequence"):
            self.assertEqual(final[field], original[field])
        self.assertEqual(final["policy"]["source_callbacks"] -
                         final["counters"]["source_callbacks"], 2)
        for stage in ("match", "suppliers", "photos", "reports", "private_staging"):
            self.assertIn(stage, later)
        self.assertEqual(result["tick_state"], "work_slice_finished_remaining_work_kept")

    def test_acquire_preserves_inspection_fields_without_inventing_dispatch(self):
        record = programme.load_programme(self.memory, "whole-programme")
        returned = {"handler_calls": 0, "passes": [], "inspection_pending": True,
                    "inspection_scanned": 4, "inspection_remaining": 12}
        context = {"handlers": {}, "request_builder": lambda _context: {},
                   "source_limit": 12, "retry_seconds": 300}
        with patch("product_radar.source_coverage.continue_source_run",
                   return_value=returned) as continuation:
            result = programme._acquire(self.memory, record, context)
        self.assertEqual(continuation.call_count, 1)
        self.assertEqual(continuation.call_args.kwargs["action_limit"], 12)
        for field in ("inspection_pending", "inspection_scanned", "inspection_remaining"):
            self.assertEqual(result[field], returned[field])
        self.assertEqual(result["fresh_dispatches"], 0)
        self.assertEqual(result["callbacks_without_new_dispatch"], 0)
        self.assertEqual(self.memory.snapshot()["query_tasks"], {})

    def test_pending_inspection_readiness_reuses_supplied_state_without_mutating(self):
        self.prepare_sources()
        self.save_inspection(1)
        record = programme.load_programme(self.memory, "whole-programme")
        state = self.memory.snapshot()
        with patch.object(self.memory, "snapshot",
                side_effect=AssertionError("Readiness must reuse its supplied state.")):
            result = programme._source_turn_readiness(self.memory, state, record, {}, self.now)
        self.assertFalse(result["callback_ready"])
        self.assertTrue(result["local_ready"])
        self.assertTrue(result["recovery_pending"])
        saved = next(row for row in result["signal"]
                     if row[1] == "inspection_continuation")
        self.assertEqual(saved[2]["remaining"], 2)
        self.assertEqual(self.memory.snapshot(), state)

    def test_uncertain_original_without_due_hint_settles_without_renewing_spent_turns(self):
        original_id, _members, calls, options = self.uncertain_source_fixture()
        first = self.advance_ready(**options)
        self.assertTrue(first["meaningful_progress"])
        second = self.advance_ready(**options)
        self.assertFalse(second["meaningful_progress"])
        self.assertEqual(second["next_action"], "wait_for_saved_trigger")
        before = self.memory.snapshot_with_provenance()
        for _ in range(2):
            waiting = self.advance_ready(**options)
            self.assertEqual(waiting["state"], "all_waiting")
            self.assertFalse(waiting["round_completed"])
        self.assertEqual(self.memory.snapshot_with_provenance(), before)
        self.assertEqual(len(calls), 2)
        self.assertEqual(len({acquisition for _task, acquisition, _reconcile in calls}), 1)
        task = self.memory.snapshot()["query_tasks"][original_id]
        self.assertEqual(task["status"], "blocked")
        self.assertTrue(task["reconcile_required"])
        self.assertEqual(task["tries"], 1)
        self.assertEqual(task["generation"], 1)

    def test_uncertain_original_cannot_starve_a_healthy_later_member(self):
        original_id, members, calls, options = self.uncertain_source_fixture(with_sibling=True)
        healthy_id = next(task_id for task_id in members if task_id != original_id)
        results = [self.advance_ready(**options) for _ in range(4)]
        self.assertEqual(sum(task_id == healthy_id for task_id, *_rest in calls), 1)
        self.assertLessEqual(sum(task_id == original_id for task_id, *_rest in calls), 2)
        self.assertEqual(len({acquisition for task_id, acquisition, _reconcile in calls
                              if task_id == original_id}), 1)
        tasks = self.memory.snapshot()["query_tasks"]
        self.assertEqual(tasks[healthy_id]["status"], "completed_no_results")
        self.assertEqual(tasks[healthy_id]["tries"], 1)
        self.assertEqual(tasks[original_id]["generation"], 1)
        self.assertEqual(results[-1]["state"], "all_waiting")

    def test_returned_original_reply_wakes_after_uncertain_round_settled(self):
        original_id, _members, calls, options = self.uncertain_source_fixture()
        self.advance_ready(**options)
        self.assertEqual(self.advance_ready(**options)["next_action"], "wait_for_saved_trigger")
        state = self.memory.snapshot()
        original = deepcopy(state["query_tasks"][original_id])
        prior_turns = {ref: deepcopy(row) for ref, row in state["batch_runs"].items()
                       if row.get("kind") == "work-turn-v1"}
        context = source_access_context(state, original, as_of=self.now)
        context["request_hash"] = original["request_hash"]
        _stage_receipt(self.memory, context, self.empty_receipt(context))
        recovered = self.advance_ready(**options)
        self.assertTrue(recovered["round_completed"])
        self.assertTrue(recovered["meaningful_progress"])
        self.assertEqual(recovered["work_turn"]["counters"]["source_callbacks"], 0)
        self.assertEqual(len(calls), 2)  # Apply the returned original, never call the collector again.
        after = self.memory.snapshot()
        final = after["query_tasks"][original_id]
        self.assertEqual(final["status"], "completed_no_results")
        self.assertFalse(final["reconcile_required"])
        for field in ("acquisition_id", "generation", "tries", "attempt_refs"):
            self.assertEqual(final[field], original[field])
        for ref, row in prior_turns.items():
            self.assertEqual(after["batch_runs"][ref], row)

    def test_actual_source_due_hint_change_wakes_once_then_settles(self):
        original_id, _members, calls, options = self.uncertain_source_fixture()
        self.advance_ready(**options)
        self.assertEqual(self.advance_ready(**options)["next_action"], "wait_for_saved_trigger")
        future = (timestamp(self.now) + timedelta(hours=1)).isoformat()

        def due(context):
            ready = timestamp(context["as_of"]) >= timestamp(future)
            return {"ready": ready, "local_saved": False, "bypass_public_pause": False,
                    "next_retry_at": None if ready else future,
                    "reason": "Synthetic original recovery deadline."}

        def preflight(context):
            hint = due(context)
            return ({"action": "collect"} if hint["ready"] else
                    {"action": "wait", "reason": hint["reason"], "next_retry_at": future})

        options["handlers"]["meta-ads"]["source_due_hint"] = due
        options["handlers"]["meta-ads"]["source_access_preflight"] = preflight
        waiting = self.advance_ready(**options)
        self.assertEqual(waiting["next_action"], "wait_for_saved_trigger")
        self.assertEqual(waiting["next_retry_at"], future)
        self.assertEqual(len(calls), 2)
        recovered = self.advance_ready(**dict(options, as_of=future))
        self.assertTrue(recovered["round_completed"])
        self.assertFalse(recovered["meaningful_progress"])
        self.assertEqual(recovered["next_action"], "wait_for_saved_trigger")
        self.assertEqual(len(calls), 3)
        self.assertTrue(all(task_id == original_id for task_id, *_rest in calls))
        self.assertEqual(len({acquisition for _task, acquisition, _reconcile in calls}), 1)
        self.assertEqual(self.advance_ready(**dict(options, as_of=future))["state"], "all_waiting")

    def test_same_open_turn_recovers_new_original_once_after_cold_restart_then_settles(self):
        original_id, _members, calls, options = self.uncertain_source_fixture()
        turn = open_work_turn(self.memory, "whole-programme", at=self.now,
            policy=dict(DEFAULT_POLICY, source_callbacks=3, exploration_slots=0))

        def execute():
            return execute_source_run(self.memory, "existing-source-run", handlers=options["handlers"],
                as_of=self.now, bounded_call=lambda callback, supplied, _timeout: callback(supplied))

        with bind_work_turn(self.memory, turn["id"]):
            first = execute()
        self.assertEqual(first["handler_calls"], 1)
        saved_original = deepcopy(self.memory.snapshot()["query_tasks"][original_id])
        self.assertTrue(saved_original["reconcile_required"])
        self.memory = ResearchMemory(self.memory.root, self.memory.journal, evidence_type="synthetic")
        with bind_work_turn(self.memory, turn["id"]):
            recovered = execute()
            after_recovery = deepcopy(self.memory.snapshot()["query_tasks"])
            settled = execute()
            current = active_work_turn(self.memory)
        self.assertEqual(recovered["handler_calls"], 1)
        self.assertEqual(settled["handler_calls"], 0)
        self.assertEqual(current["state"], "open")
        self.assertEqual(current["counters"]["source_callbacks"], 2)
        self.assertEqual(current["policy"]["source_callbacks"], 3)
        self.assertEqual(len(calls), 2)
        self.assertEqual(calls[0], (original_id, saved_original["acquisition_id"], False))
        self.assertEqual(calls[1], (original_id, saved_original["acquisition_id"], True))
        self.assertEqual(self.memory.snapshot()["query_tasks"], after_recovery)
        final = self.memory.snapshot()["query_tasks"][original_id]
        for field in ("acquisition_id", "generation", "tries", "attempt_refs"):
            self.assertEqual(final[field], saved_original[field])

    def test_original_recovery_obligation_survives_a_new_company_page_suffix(self):
        original_id, _members, calls, options = self.uncertain_source_fixture()
        turn = open_work_turn(self.memory, "whole-programme", at=self.now,
            policy=dict(DEFAULT_POLICY, source_callbacks=3, exploration_slots=0))

        def execute():
            return execute_source_run(self.memory, "existing-source-run", handlers=options["handlers"],
                as_of=self.now, bounded_call=lambda callback, supplied, _timeout: callback(supplied))

        with bind_work_turn(self.memory, turn["id"]):
            self.assertEqual(execute()["handler_calls"], 1)
            original = deepcopy(self.memory.snapshot()["query_tasks"][original_id])
            appended = prepare_source_tasks(self.memory, [{"route": "company-pages", "request": {
                "query": "synthetic newly found supplier page", "country": "IN"},
                "concept_ids": ["first"]}], run_id="existing-source-run", as_of=self.now)
            suffix = next(task_id for task_id in appended["task_ids"] if task_id != original_id)
            # This pass finishes only the genuinely new suffix. It must not
            # claim that the older original recovery was visited as well.
            company_pass = execute()
            self.assertEqual(company_pass["handler_calls"], 0)
            self.assertEqual([row["task_id"] for row in company_pass["results"]], [suffix])
            self.assertEqual(company_pass["counts"], {"not_connected": 1})
            recovered = execute()
            settled = execute()
            current = active_work_turn(self.memory)
        self.assertEqual(recovered["handler_calls"], 1)
        self.assertEqual(settled["handler_calls"], 0)
        self.assertEqual(current["counters"]["source_callbacks"], 2)
        self.assertEqual(calls, [(original_id, original["acquisition_id"], False),
                                 (original_id, original["acquisition_id"], True)])
        state = self.memory.snapshot()
        for field in ("acquisition_id", "generation", "tries", "attempt_refs"):
            self.assertEqual(state["query_tasks"][original_id][field], original[field])
        self.assertEqual(state["query_tasks"][suffix]["status"], "prepared")
        self.assertNotIn("acquisition_id", state["query_tasks"][suffix])

    def future_waiting_original_fixture(self):
        original_id, _members, calls, options = self.uncertain_source_fixture()
        future = (timestamp(self.now) + timedelta(hours=1)).isoformat()

        def due(context):
            ready = not context["reconciling"] or timestamp(context["as_of"]) >= timestamp(future)
            return {"ready": ready, "local_saved": False, "bypass_public_pause": False,
                    "next_retry_at": None if ready else future,
                    "reason": "Synthetic original recovery future deadline."}

        def preflight(context):
            hint = due(context)
            return ({"action": "collect"} if hint["ready"] else
                    {"action": "wait", "reason": hint["reason"], "next_retry_at": future})

        options["handlers"]["meta-ads"]["source_due_hint"] = due
        options["handlers"]["meta-ads"]["source_access_preflight"] = preflight
        turn = open_work_turn(self.memory, "whole-programme", at=self.now,
            policy=dict(DEFAULT_POLICY, source_callbacks=2, exploration_slots=0))
        with bind_work_turn(self.memory, turn["id"]):
            first = execute_source_run(self.memory, "existing-source-run", handlers=options["handlers"],
                as_of=self.now, bounded_call=lambda callback, supplied, _timeout: callback(supplied))
        self.assertEqual(first["handler_calls"], 1)
        self.assertEqual(len(calls), 1)
        self.assertTrue(self.memory.snapshot()["query_tasks"][original_id]["reconcile_required"])
        return calls, options["handlers"], future

    def test_future_original_recovery_does_not_wake_a_completed_inspection(self):
        calls, handlers, future = self.future_waiting_original_fixture()
        state = self.memory.snapshot_with_provenance()
        record = programme.load_programme(self.memory, "whole-programme")
        waiting = programme._source_turn_readiness(
            self.memory, state["state"], record, handlers, self.now)
        self.assertFalse(waiting["callback_ready"])
        self.assertFalse(waiting["local_ready"])
        self.assertFalse(waiting["recovery_pending"])
        self.assertIn(future, waiting["waits"])
        due = programme._source_turn_readiness(self.memory, state["state"], record, handlers, future)
        self.assertTrue(due["callback_ready"])
        self.assertEqual(len(calls), 1)
        self.assertEqual(self.memory.snapshot_with_provenance(), state)

    def test_future_original_wait_does_not_hide_an_unfinished_ordinary_suffix(self):
        calls, handlers, future = self.future_waiting_original_fixture()
        prepare_source_tasks(self.memory, [{"route": "company-pages", "request": {
            "query": "synthetic later ordinary supplier page", "country": "IN"},
            "concept_ids": ["first"]}], run_id="existing-source-run", as_of=self.now)
        state = self.memory.snapshot_with_provenance()
        record = programme.load_programme(self.memory, "whole-programme")
        waiting = programme._source_turn_readiness(
            self.memory, state["state"], record, handlers, self.now)
        self.assertFalse(waiting["callback_ready"])
        self.assertTrue(waiting["local_ready"])
        self.assertTrue(waiting["recovery_pending"])
        self.assertIn(future, waiting["waits"])
        inspection = next(row[2] for row in waiting["signal"]
                          if row[1] == "inspection_continuation")
        self.assertEqual(inspection["ordinary_remaining"], 1)
        self.assertEqual(len(calls), 1)
        self.assertEqual(self.memory.snapshot_with_provenance(), state)

    def add_deleted_route_question(self, status, task_id="deleted-route-question"):
        """A question saved before Amazon Movers & Shakers was deleted (R10 W6)."""
        self.prepare_sources(1)
        state = self.memory.snapshot(); run_key = key("source-run", "existing-source-run")
        run = state["batch_runs"][run_key]
        original = state["query_tasks"][run["task_ids"][0]]
        old = dict(original, route="amazon-movers-and-shakers", request={"department": "kitchen"}, status=status)
        if status == "retired":
            old.update(reason="SOURCE_ROUTE_RETIRED", reconcile_required=False, retryable=False,
                       next_retry_at=None, retired_at=self.now)
        self.memory.commit("synthetic-deleted-route-question", [
            change("query_tasks", task_id, old),
            change("batch_runs", run_key, dict(run, task_ids=run["task_ids"] + [task_id]), previous=run)])
        return task_id

    def test_open_question_of_a_deleted_route_wakes_local_work_without_raising(self):
        self.prepare_sources(1)
        record = programme.load_programme(self.memory, "whole-programme")
        baseline = programme._source_turn_readiness(self.memory, self.memory.snapshot(), record, {}, self.now)
        self.assertFalse(baseline["local_ready"])
        self.add_deleted_route_question("prepared")
        state = self.memory.snapshot_with_provenance()
        result = programme._source_turn_readiness(self.memory, state["state"], record, {}, self.now)
        self.assertTrue(result["local_ready"])
        self.assertFalse(result["callback_ready"])
        self.assertFalse(result["fresh_callback_ready"])
        self.assertEqual(result["waits"], [])
        # The whole programme decision reads it too (the live KeyError).
        programme.programme_turn_readiness(self.memory, record, root=self.root, handlers={}, as_of=self.now)
        self.assertEqual(self.memory.snapshot_with_provenance(), state)

    def test_retired_question_of_a_deleted_route_is_neither_ready_work_nor_a_wait(self):
        self.prepare_sources(1)
        record = programme.load_programme(self.memory, "whole-programme")
        baseline = programme._source_turn_readiness(self.memory, self.memory.snapshot(), record, {}, self.now)
        self.add_deleted_route_question("retired")
        state = self.memory.snapshot_with_provenance()
        result = programme._source_turn_readiness(self.memory, state["state"], record, {}, self.now)
        self.assertEqual(result, baseline)
        # A late reply staged for it is never applied, so it must not wake turns.
        with patch("product_radar.source_coverage._has_unapplied_receipt",
                   side_effect=lambda _memory, _state, task: task["route"] == "amazon-movers-and-shakers") as staged:
            late = programme._source_turn_readiness(self.memory, state["state"], record, {}, self.now)
        self.assertFalse(late["local_ready"])
        self.assertNotIn("amazon-movers-and-shakers", [call.args[2]["route"] for call in staged.call_args_list])
        programme.programme_turn_readiness(self.memory, record, root=self.root, handlers={}, as_of=self.now)
        self.assertEqual(self.memory.snapshot_with_provenance(), state)

    def test_source_pass_closes_a_deleted_route_question_and_readiness_then_settles(self):
        task_id = self.add_deleted_route_question("dispatching")
        record = programme.load_programme(self.memory, "whole-programme")
        self.assertTrue(programme._source_turn_readiness(
            self.memory, self.memory.snapshot(), record, {}, self.now)["local_ready"])
        result = execute_source_run(self.memory, "existing-source-run", handlers={}, as_of=self.now)
        self.assertEqual(result["handler_calls"], 0)
        self.assertIn({"task_id": task_id, "state": "retired", "actual_attempt": False,
                       "reason": "SOURCE_ROUTE_RETIRED"}, result["results"])
        state = self.memory.snapshot()
        self.assertEqual(state["query_tasks"][task_id]["status"], "retired")
        settled = programme._source_turn_readiness(self.memory, state, record, {}, self.now)
        self.assertFalse(settled["local_ready"])
        self.assertFalse(settled["recovery_pending"])
        self.assertEqual(settled["waits"], [])
        # Nothing is left to close: a second pass changes nothing.
        before = self.memory.snapshot()
        again = execute_source_run(self.memory, "existing-source-run", handlers={}, as_of=self.now)
        self.assertFalse([row for row in again["results"] if row["task_id"] == task_id])
        self.assertEqual(self.memory.snapshot()["query_tasks"], before["query_tasks"])


if __name__ == "__main__":
    unittest.main()
