"""Finite local source visits, using disposable records and saved fixtures only.

These checks do not call providers, browsers, models or real research tasks.
"""

from copy import deepcopy
import unittest
from unittest.mock import patch

import test_source_coverage as fixtures

import product_radar.source_coverage as coverage
from product_radar.models import SafetyError, digest
from product_radar.research_memory import ResearchMemory, key
from product_radar.source_coverage import (
    SOURCE_INSPECTION_SLICE_LIMIT, SOURCE_INSPECTION_PRIORITY_LIMIT,
    _effective_source_inspection, _inspection_record, _source_inspection_position,
    _stage_receipt, continue_source_run, prepare_source_tasks,
)
from product_radar.work_turns import (
    DEFAULT_POLICY, bind_work_turn, close_work_turn, open_work_turn, reserve_source_callback,
)

NOW = fixtures.NOW


class SourcePassSliceTests(unittest.TestCase):
    setUp = fixtures.SourceCoverageTests.setUp
    product = fixtures.SourceCoverageTests.product
    question = fixtures.SourceCoverageTests.question
    plan = fixtures.SourceCoverageTests.plan
    receipt = fixtures.SourceCoverageTests.receipt
    acquired = fixtures.SourceCoverageTests.acquired
    direct_runner = fixtures.SourceCoverageTests.direct_runner
    run_sources = fixtures.SourceCoverageTests.run_sources

    def questions(self, count, *, start=0, route="meta-ads"):
        return [self.question(route, query="sink caddy " + str(n))
                for n in range(start, start + count)]

    def turn(self):
        policy = deepcopy(DEFAULT_POLICY)
        policy.update(exploration_slots=0)
        return open_work_turn(self.memory, "slice-programme", at=NOW, policy=policy)

    def waiting(self, visits, *, clock=None):
        def preflight(context):
            visits.append(context["task_id"])
            if clock is not None:
                clock[0] += 35
            return {"action": "wait", "reason": "Synthetic saved answer has not arrived."}
        def forbidden(_context):
            raise AssertionError("A waiting local visit must not launch a collector.")
        return {"meta-ads": {"handler": forbidden, "source_access_preflight": preflight}}

    def continuation(self):
        run_key = key("source-run", "week-one")
        run = self.memory.snapshot()["batch_runs"][run_key]
        return _effective_source_inspection(self.memory, run_key, run)

    def save_position(self, next_index, **extra):
        run_key = key("source-run", "week-one")
        state = self.memory.snapshot()
        old = state["batch_runs"][run_key]
        raw = {"contract": "source-inspection-slice-v1", "scope_hash": digest(old["task_ids"]),
               "ceiling": len(old["task_ids"]), "next_index": next_index, "sweep": 0,
               "turn_id": None, **extra}
        self.memory.put("batch_runs", run_key, dict(old, inspection_continuation=raw), previous=old)

    def staged(self, task_id):
        state = self.memory.snapshot()
        task = state["query_tasks"][task_id]
        acquisition_id = key("source-acquisition", [task_id, 1])
        updated = dict(task, status="dispatching", acquisition_id=acquisition_id,
            generation=1, tries=1, dispatch_started=True, reconcile_required=True,
            attempt_refs=[acquisition_id], dispatched_at=NOW)
        self.memory.put("query_tasks", task_id, updated, previous=task)
        context = {"task_id": task_id, "acquisition_id": acquisition_id, "route": task["route"],
            "request_hash": task["request_hash"], "description_hashes": dict(task["targets"]),
            "concept_ids": list(task["targets"]), "as_of": NOW}
        _stage_receipt(self.memory, context, self.receipt(context))

    def test_open_allowance_visits_at_most_64_normal_items_and_resumes_after_restart(self):
        prepared = self.plan(self.questions(140))
        before = self.memory.snapshot()["query_tasks"]
        turn, visits = self.turn(), []
        with bind_work_turn(self.memory, turn["id"]):
            first = self.run_sources(self.waiting(visits))
        self.assertEqual(first["handler_calls"], 0)
        self.assertEqual(first["inspection_scanned"], SOURCE_INSPECTION_SLICE_LIMIT)
        self.assertEqual(first["inspection_remaining"], 76)
        self.assertTrue(first["inspection_pending"])
        self.assertEqual(visits, prepared["task_ids"][:64])
        self.memory = ResearchMemory(self.memory.root, self.memory.journal, evidence_type="synthetic")
        with bind_work_turn(self.memory, turn["id"]):
            second = self.run_sources(self.waiting(visits))
            third = self.run_sources(self.waiting(visits))
            settled = self.run_sources(self.waiting(visits))
        self.assertEqual([second["inspection_scanned"], third["inspection_scanned"], settled["inspection_scanned"]],
                         [64, 12, 0])
        self.assertEqual(visits, prepared["task_ids"])
        self.assertFalse(settled["inspection_pending"])
        after = self.memory.snapshot()
        self.assertEqual(before, after["query_tasks"])
        self.assertEqual(after["batch_runs"][turn["id"]]["counters"]["source_callbacks"], 0)

    def test_elapsed_ceiling_yields_between_finished_items_without_interrupting_one(self):
        prepared = self.plan(self.questions(8))
        clock, visits = [0.0], []
        with patch.object(coverage.time, "monotonic", side_effect=lambda: clock[0]):
            result = self.run_sources(self.waiting(visits, clock=clock))
        self.assertEqual(visits, prepared["task_ids"][:2])
        self.assertEqual(clock[0], 70)
        self.assertEqual(result["inspection_remaining"], 6)
        self.assertTrue(result["inspection_pending"])
        self.assertEqual(result["handler_calls"], 0)

    def test_new_arrivals_do_not_reset_the_frozen_prefix(self):
        prepared = self.plan(self.questions(80))
        turn, visits = self.turn(), []
        with bind_work_turn(self.memory, turn["id"]):
            self.run_sources(self.waiting(visits))
            appended = self.plan(self.questions(3, start=80))
            second = self.run_sources(self.waiting(visits))
            self.assertEqual(second["inspection_scanned"], 16)
            self.assertEqual(second["inspection_remaining"], 3)
            self.assertEqual(self.continuation()["ceiling"], 80)
            third = self.run_sources(self.waiting(visits))
        self.assertEqual(visits, appended["task_ids"])
        self.assertEqual(visits[:80], prepared["task_ids"])
        self.assertEqual(third["inspection_scanned"], 3)
        self.assertFalse(third["inspection_pending"])

    def test_subsets_never_move_or_overwrite_the_full_run_position(self):
        prepared = self.plan(self.questions(80))
        turn, visits = self.turn(), []
        with bind_work_turn(self.memory, turn["id"]):
            self.run_sources(self.waiting(visits))
            full = self.continuation()
            subset = self.run_sources(self.waiting(visits), task_ids=[prepared["task_ids"][-1]])
            self.assertEqual(self.continuation(), full)
            tail = self.run_sources(self.waiting(visits))
        self.assertEqual(subset["inspection_scanned"], 1)
        self.assertEqual(tail["inspection_scanned"], 16)
        self.assertEqual(visits[:64], prepared["task_ids"][:64])
        self.assertEqual(visits[65:], prepared["task_ids"][64:])

    def test_subset_preserves_full_position_saved_only_in_pass_return(self):
        prepared = self.plan(self.questions(80))
        turn, visits = self.turn(), []
        upsert = coverage._upsert
        def busy(memory, table, *args, **kwargs):
            if table == "batch_runs":
                raise SafetyError("RUNNER_ALREADY_ACTIVE")
            return upsert(memory, table, *args, **kwargs)
        with bind_work_turn(self.memory, turn["id"]):
            with patch.object(coverage, "_upsert", side_effect=busy):
                first = self.run_sources(self.waiting(visits))
            self.assertEqual(first["local_save_errors"], [{"stage": "pass_progress", "reason": "RUNNER_ALREADY_ACTIVE"}])
            self.assertEqual(self.continuation()["next_index"], 64)
            self.run_sources(self.waiting(visits), task_ids=[prepared["task_ids"][-1]])
            self.assertEqual(self.continuation()["next_index"], 64)
            result = self.run_sources(self.waiting(visits))
        self.assertEqual(result["inspection_scanned"], 16)
        self.assertFalse(result["inspection_pending"])

    def test_staged_reply_has_priority_without_skipping_untouched_normal_items(self):
        prepared = self.plan(self.questions(75))
        last = prepared["task_ids"][-1]
        self.staged(last)
        visits, events = [], []
        with patch.object(coverage, "record_event", side_effect=lambda kind, **row: events.append((kind, row))):
            result = self.run_sources(self.waiting(visits))
        inspected = [row["task_id"] for kind, row in events if kind == "source_inspection"]
        self.assertEqual(inspected[0], last)
        self.assertEqual(visits, prepared["task_ids"][:64])
        self.assertEqual(result["inspection_normal_scanned"], 64)
        self.assertEqual(result["inspection_scanned"], 65)
        self.assertEqual(self.continuation()["next_index"], 64)
        self.assertEqual(self.memory.snapshot()["query_tasks"][last]["status"], "acquired")
        self.assertEqual(result["handler_calls"], 0)

    def test_priority_only_elapsed_yield_is_persisted_and_visible_to_readiness(self):
        prepared = self.plan(self.questions(3))
        for task_id in prepared["task_ids"]:
            self.staged(task_id)
        turn = self.turn()
        self.save_position(3, turn_id=turn["id"])
        clock = [0.0]
        persist = coverage._persist_receipt
        def slow_saved(*args, **kwargs):
            result = persist(*args, **kwargs)
            clock[0] += 65
            return result
        with bind_work_turn(self.memory, turn["id"]), \
                patch.object(coverage.time, "monotonic", side_effect=lambda: clock[0]), \
                patch.object(coverage, "_persist_receipt", side_effect=slow_saved):
            first = self.run_sources(self.waiting([]))
        effective = self.continuation()
        self.assertEqual(first["inspection_scanned"], 1)
        self.assertTrue(first["inspection_pending"])
        self.assertEqual(effective["ordinary_remaining"], 0)
        self.assertEqual(effective["priority_remaining"], 2)
        self.assertEqual(effective["remaining"], first["inspection_remaining"])
        self.assertEqual(len(effective["priority_pending_ids"]), 2)
        # A new work turn must not replay the already completed ordinary
        # cohort while its bounded priority-only continuation is unfinished.
        close_work_turn(self.memory, turn["id"], at=NOW, reason="Synthetic finite slice completed.")
        next_turn = self.turn()
        with bind_work_turn(self.memory, next_turn["id"]):
            second = self.run_sources(self.waiting([]))
        self.assertEqual(second["inspection_normal_scanned"], 0)
        self.assertFalse(second["inspection_pending"])
        self.assertEqual(second["handler_calls"], 0)
        self.assertTrue(all(self.memory.snapshot()["query_tasks"][tid]["status"] == "acquired"
                            for tid in prepared["task_ids"]))

    def test_more_staged_replies_than_priority_batch_keep_a_finite_later_wake(self):
        prepared = self.plan(self.questions(SOURCE_INSPECTION_PRIORITY_LIMIT + 3))
        for task_id in prepared["task_ids"]:
            self.staged(task_id)
        turn = self.turn()
        self.save_position(len(prepared["task_ids"]), turn_id=turn["id"])
        with bind_work_turn(self.memory, turn["id"]):
            first = self.run_sources(self.waiting([]))
            self.assertEqual(first["inspection_scanned"], SOURCE_INSPECTION_PRIORITY_LIMIT)
            self.assertTrue(first["inspection_pending"])
            self.assertEqual(self.continuation()["priority_scan_remaining"], 3)
            second = self.run_sources(self.waiting([]))
            third = self.run_sources(self.waiting([]))
        self.assertEqual(second["inspection_scanned"], 3)
        self.assertFalse(second["inspection_pending"])
        self.assertEqual(third["inspection_scanned"], 0)

    def test_a_slow_priority_reply_still_allows_one_ordinary_step(self):
        prepared = self.plan(self.questions(4))
        self.staged(prepared["task_ids"][-1])
        clock, visits = [0.0], []
        persist = coverage._persist_receipt
        def slow_saved(*args, **kwargs):
            result = persist(*args, **kwargs)
            clock[0] += 65
            return result
        with patch.object(coverage.time, "monotonic", side_effect=lambda: clock[0]), \
                patch.object(coverage, "_persist_receipt", side_effect=slow_saved):
            result = self.run_sources(self.waiting(visits))
        self.assertEqual(visits, prepared["task_ids"][:1])
        self.assertEqual(result["inspection_scanned"], 2)
        self.assertEqual(self.continuation()["next_index"], 1)

    def test_wait_outcome_is_recorded_before_next_visit_without_expensive_progress_callback(self):
        self.plan(self.questions(3))
        events, observer = [], []
        handlers = self.waiting([])
        preflight = handlers["meta-ads"]["source_access_preflight"]
        def inspect(context):
            prior = [row for kind, row in events if kind == "source_outcome"]
            self.assertEqual(len(prior), len([kind for kind, _ in events if kind == "source_inspection"]) - 1)
            return preflight(context)
        handlers["meta-ads"]["source_access_preflight"] = inspect
        with patch.object(coverage, "record_event", side_effect=lambda kind, **row: events.append((kind, row))):
            result = self.run_sources(handlers, on_progress=observer.append)
            deepcopy(result)
        self.assertEqual(len([kind for kind, _ in events if kind == "source_outcome"]), 3)
        self.assertEqual(observer, [])
        self.assertEqual(self.memory.snapshot()["batch_runs"][key("source-run", "week-one")]["last_results"], result["results"])

    def test_wrapper_yields_pending_slice_even_after_a_real_callback(self):
        self.plan(self.questions(80))
        calls = []
        def preflight(context):
            return ({"action": "collect"} if not calls else
                    {"action": "wait", "reason": "Synthetic pending saved answer."})
        def acquired(context):
            calls.append(context["task_id"])
            return self.acquired(context)
        result = continue_source_run(self.memory, "week-one", concept_ids=["first"],
            request_builder=lambda _context: {"not_applicable_reason": "Synthetic original queue only."},
            handlers={"meta-ads": {"handler": acquired, "source_access_preflight": preflight}},
            bounded_call=self.direct_runner)
        self.assertEqual(len(calls), 1)
        self.assertEqual(result["handler_calls"], 1)
        self.assertEqual(len(result["passes"]), 1)
        self.assertEqual(result["inspection_scanned"], 64)
        self.assertTrue(result["inspection_pending"])
        self.assertEqual(result["inspection_remaining"], 16)

    def test_saved_supplier_reply_appended_company_dependency_keeps_wrapper_pending(self):
        prepared = self.plan(self.questions(1, route="google-suppliers"))
        self.staged(prepared["task_ids"][0])
        policy = deepcopy(DEFAULT_POLICY)
        policy.update(source_callbacks=1, exploration_slots=0)
        turn = open_work_turn(self.memory, "slice-programme", at=NOW, policy=policy)
        def builder(context):
            return ({"query": "synthetic supplier company", "country": "IN"}
                    if context["route"] == "company-pages" else
                    {"not_applicable_reason": "Synthetic saved reply only."})
        with bind_work_turn(self.memory, turn["id"]):
            reserve_source_callback(self.memory, "already-used", at=NOW)
            result = continue_source_run(self.memory, "week-one", request_builder=builder,
                concept_ids=["first"], handlers={}, bounded_call=self.direct_runner)
        self.assertEqual(result["handler_calls"], 0)
        self.assertEqual(len(result["passes"]), 1)
        self.assertTrue(result["inspection_pending"])
        self.assertEqual(result["inspection_remaining"], 1)
        self.assertEqual(self.continuation()["ordinary_remaining"], 1)

    def test_wrapper_action_limit_keeps_dependency_prepared_after_previous_completed_pass(self):
        calls = []
        def builder(context):
            if context["route"] == "google-suppliers":
                return {"query": "synthetic supplier", "country": "IN"}
            if context["route"] == "company-pages":
                if not context["state"]["source_checks"].get("synthetic-destination"):
                    raise SafetyError("SOURCE_SUPPLIER_DESTINATIONS_PENDING")
                return {"query": "synthetic company", "country": "IN"}
            return {"not_applicable_reason": "Synthetic focused supplier work only."}
        def supplier(context):
            calls.append(context["route"])
            self.memory.put("source_checks", "synthetic-destination", {"state": "captured"})
            return self.acquired(context)
        result = continue_source_run(self.memory, "week-one", request_builder=builder,
            concept_ids=["first"], action_limit=1, handlers={"google-suppliers": supplier},
            bounded_call=self.direct_runner)
        self.assertEqual(calls, ["google-suppliers"])
        self.assertEqual(result["handler_calls"], 1)
        self.assertEqual(len(result["passes"]), 1)
        self.assertTrue(result["inspection_pending"])
        self.assertEqual(result["inspection_remaining"], 1)
        self.assertEqual(self.continuation()["ordinary_remaining"], 1)

    def test_invalid_or_reordered_frozen_prefix_is_not_accepted_as_progress(self):
        prepared = self.plan(self.questions(3))
        self.save_position(1)
        original = self.continuation()
        raw = {name: value for name, value in original.items()
               if name not in {"remaining", "ordinary_remaining", "priority_remaining", "cohort_remaining"}}
        with self.assertRaisesRegex(SafetyError, "SOURCE_PROGRESS_STAGE_INVALID"):
            _inspection_record(raw, list(reversed(prepared["task_ids"])))
        for extra in ({"next_index": True}, {"priority_pending_ids": ["not-a-member"]},
                      {"priority_scan_remaining": 4}, {"ceiling": 2}):
            with self.subTest(extra=extra), self.assertRaisesRegex(SafetyError, "SOURCE_PROGRESS_STAGE_INVALID"):
                _inspection_record(dict(raw, **extra), prepared["task_ids"])

    def test_new_turn_does_not_reset_any_unfinished_priority_dimension(self):
        members = ["first", "second", "third"]
        raw = {"contract": "source-inspection-slice-v1", "scope_hash": digest(members),
            "ceiling": 3, "next_index": 3, "sweep": 7, "turn_id": "old-turn"}
        for extra in ({"priority_pending_ids": ["second"]}, {"priority_scan_remaining": 2}):
            with self.subTest(extra=extra):
                position = _source_inspection_position(dict(raw, **extra), members,
                    {"id": "new-turn"}, exhausted=False)
                self.assertEqual(position["next_index"], 3)
                self.assertEqual(position["sweep"], 7)

    def test_new_bound_turn_services_a_healthy_sibling_before_repeating_uncertain_original(self):
        prepared = self.plan(self.questions(2))
        unknown, healthy = prepared["task_ids"]
        policy = deepcopy(DEFAULT_POLICY)
        policy.update(source_callbacks=1, exploration_slots=0)
        calls = []
        def handler(context):
            calls.append((context["task_id"], context["acquisition_id"], context["reconciling"]))
            if context["task_id"] == unknown:
                raise TimeoutError("Synthetic original has not returned.")
            return self.acquired(context)
        handlers = {"meta-ads": {"handler": handler, "reconcile": handler}}
        for number in range(3):
            turn = open_work_turn(self.memory, "slice-programme", at=NOW, policy=policy)
            with bind_work_turn(self.memory, turn["id"]):
                result = self.run_sources(handlers)
            self.assertLessEqual(result["handler_calls"], 1)
            close_work_turn(self.memory, turn["id"], at=NOW, reason="Synthetic work turn finished.")
        self.assertEqual([row[0] for row in calls[:2]], [unknown, healthy])
        original_calls = [row for row in calls if row[0] == unknown]
        self.assertEqual(len({row[1] for row in original_calls}), 1)
        self.assertFalse(original_calls[0][2])
        self.assertTrue(all(row[2] for row in original_calls[1:]))
        state = self.memory.snapshot()
        self.assertEqual(state["query_tasks"][unknown]["generation"], 1)
        self.assertEqual(state["query_tasks"][unknown]["tries"], 0)
        self.assertEqual(state["query_tasks"][healthy]["status"], "acquired")
        self.assertEqual(state["published_history"], {})


if __name__ == "__main__":
    unittest.main()
