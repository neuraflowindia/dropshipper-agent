"""Spent original source grants do not rebuild a fresh product/source grid.

Every source body and work round here is a disposable synthetic fixture.
"""

from copy import deepcopy
from pathlib import Path
import sys
import unittest
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import product_radar.source_coverage as coverage
from product_radar.research_memory import ResearchMemory, change, key
from product_radar.models import SafetyError
from product_radar.source_coverage import (ROUTES, SAVED_RECOVERY_SCAN_LIMIT,
    continue_source_run, execute_source_run, prepare_all_source_tasks, prepare_source_tasks)
from product_radar.storage import atomic_json, load_json
from product_radar.work_turns import (DEFAULT_POLICY, bind_work_turn, close_work_turn,
    open_work_turn)
import test_source_coverage as fixtures

NOW = fixtures.NOW


class SourceExhaustedPreparationTests(unittest.TestCase):
    def setUp(self):
        self.fixture = fixtures.SourceCoverageTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.memory = self.fixture.memory
        self.root = self.fixture.root

    def spent_turn(self):
        prepare_source_tasks(self.memory, [self.fixture.question(query="spent one"),
            self.fixture.question(query="spent two")], run_id="spent-source", as_of=NOW)
        policy = deepcopy(DEFAULT_POLICY)
        policy.update(source_callbacks=2, exploration_slots=0)
        turn = open_work_turn(self.memory, "source-programme", at=NOW, policy=policy)
        with bind_work_turn(self.memory, turn["id"]):
            result = execute_source_run(self.memory, "spent-source", as_of=NOW,
                handlers={"meta-ads": self.fixture.acquired}, bounded_call=self.fixture.direct_runner)
        self.assertEqual(result["handler_calls"], 2)
        return turn

    def continue_run(self, builder, **kwargs):
        return continue_source_run(self.memory, "week-one", request_builder=builder,
            bounded_call=self.fixture.direct_runner, **kwargs)

    def staged_original(self):
        prepared = prepare_source_tasks(self.memory, [self.fixture.question()], run_id="week-one", as_of=NOW)
        policy = deepcopy(DEFAULT_POLICY)
        policy.update(source_callbacks=1, exploration_slots=0)
        turn = open_work_turn(self.memory, "source-programme", at=NOW, policy=policy)
        handler = Mock(side_effect=self.fixture.acquired)
        with bind_work_turn(self.memory, turn["id"]), patch.object(coverage, "_persist_receipt",
                side_effect=RuntimeError("Synthetic interrupted receipt application.")):
            result = execute_source_run(self.memory, "week-one", as_of=NOW,
                handlers={"meta-ads": handler}, bounded_call=self.fixture.direct_runner)
        self.assertEqual(result["handler_calls"], 1)
        self.assertEqual(handler.call_count, 1)
        task_id = prepared["task_ids"][0]
        task = self.memory.snapshot()["query_tasks"][task_id]
        self.assertTrue(task["reconcile_required"])
        self.assertEqual(task["receipt_refs"], [])
        return turn, task_id, handler

    def test_existing_spent_grant_skips_all_2184_fresh_builder_calls(self):
        state = self.memory.snapshot()
        prototype = state["concepts"]["first"]
        additions = [change("concepts", "extra-" + str(n), deepcopy(prototype)) for n in range(154)]
        self.memory.commit("synthetic-extra-products", additions)
        prepare_source_tasks(self.memory, [self.fixture.question(query="queued " + str(n))
            for n in range(140)], run_id="week-one", as_of=NOW)
        turn = self.spent_turn()
        builder_calls = []
        def builder(context):
            builder_calls.append((context["concept_id"], context["route"]))
            raise SafetyError("SOURCE_QUESTION_INPUT_PENDING")
        with bind_work_turn(self.memory, turn["id"]):
            result = self.continue_run(builder, action_limit=12,
                handlers={route: self.fixture.acquired for route in ROUTES})
        self.assertEqual(builder_calls, [])
        self.assertEqual(result["products"], 156)
        self.assertEqual(result["product_route_questions"], 2184)
        self.assertEqual(result["handler_calls"], 0)
        self.assertEqual(len(result["passes"]), 1)
        self.assertEqual(result["passes"][0]["counts"], {"not_attempted": SAVED_RECOVERY_SCAN_LIMIT})
        self.assertEqual(result["preparation_wait"]["reason"], "WORK_TURN_SOURCE_LIMIT")
        current = self.memory.snapshot()
        self.assertEqual(current["batch_runs"][turn["id"]]["counters"]["source_callbacks"], 2)
        self.assertEqual(current["batch_runs"][key("source-run", "week-one")]["saved_recovery"]["remaining"], 76)
        self.assertEqual(len([task for task in current["query_tasks"].values()
            if task.get("run_id") == "week-one"]), 140)

    def test_filled_local_limit_keeps_new_company_dependencies_prepared(self):
        calls = []
        def builder(context):
            calls.append((context["concept_id"], context["route"]))
            if context["route"] not in {"google-suppliers", "company-pages"}:
                return {"not_applicable_reason": "Synthetic unrelated source."}
            if context["route"] == "company-pages" and not context["state"]["source_checks"].get("synthetic-destination"):
                raise SafetyError("SOURCE_SUPPLIER_DESTINATIONS_PENDING")
            return {"query": "sink caddy", "country": "IN"}
        def supplier(context):
            self.memory.put("source_checks", "synthetic-destination",
                {"state": "captured", "url": "https://supplier.example.org/sink-caddy"})
            return self.fixture.acquired(context)
        result = self.continue_run(builder, action_limit=1,
            handlers={"google-suppliers": supplier, "company-pages": self.fixture.acquired})
        self.assertEqual(result["handler_calls"], 1)
        self.assertEqual(len(calls), 4 * len(ROUTES))
        self.assertEqual(result["progress"]["pending_preparation"], 0)
        company = [task for task in self.memory.snapshot()["query_tasks"].values()
            if task.get("run_id") == "week-one" and task["route"] == "company-pages"]
        self.assertEqual(len(company), 1)
        self.assertEqual(company[0]["status"], "prepared")

    def test_spent_entry_recovers_staged_original_without_builder_or_new_callback(self):
        turn, task_id, handler = self.staged_original()
        original = deepcopy(self.memory.snapshot()["query_tasks"][task_id])
        builder = Mock(side_effect=AssertionError("Fresh planning must not run."))
        progress = []
        with bind_work_turn(self.memory, turn["id"]):
            result = self.continue_run(builder, handlers={"meta-ads": handler}, on_pass=progress.append)
        builder.assert_not_called()
        self.assertEqual(handler.call_count, 1)
        saved = self.memory.snapshot()
        current = saved["query_tasks"][task_id]
        self.assertEqual(result["handler_calls"], 0)
        self.assertEqual(result["passes"][0]["counts"], {"acquired": 1})
        self.assertEqual(current["acquisition_id"], original["acquisition_id"])
        self.assertEqual(current["request"], original["request"])
        self.assertEqual(current["targets"], original["targets"])
        self.assertEqual(current["generation"], original["generation"])
        self.assertEqual(len(current["receipt_refs"]), 1)
        self.assertEqual(saved["batch_runs"][turn["id"]]["counters"]["source_callbacks"], 1)
        self.assertEqual(len(progress), 1)
        self.assertEqual(progress[0]["task_states"], {"acquired": 1})

    def test_changed_description_does_not_retarget_the_original_saved_reply(self):
        turn, task_id, handler = self.staged_original()
        original = deepcopy(self.memory.snapshot()["query_tasks"][task_id])
        product = self.memory.snapshot()["concepts"]["first"]
        changed = deepcopy(product)
        changed["description"]["mechanism"] = "A different current mechanism."
        self.memory.put("concepts", "first", changed, previous=product)
        builder = Mock(side_effect=AssertionError("Do not rewrite the original request."))
        with bind_work_turn(self.memory, turn["id"]):
            result = self.continue_run(builder, handlers={"meta-ads": handler})
        builder.assert_not_called()
        current = self.memory.snapshot()["query_tasks"][task_id]
        self.assertEqual(current["targets"], original["targets"])
        self.assertEqual(current["receipt_refs"], [])
        self.assertEqual(handler.call_count, 1)
        self.assertEqual(result["passes"][0]["counts"], {"not_attempted": 1})

    def test_corrupt_original_body_is_not_accepted_by_the_shortcut(self):
        turn, task_id, handler = self.staged_original()
        task = self.memory.snapshot()["query_tasks"][task_id]
        folder = self.memory.root / "all_source_dispatch/returned_receipts" / task["acquisition_id"]
        pointer = load_json(folder / "pending.json")
        path = folder / (pointer["receipt_hash"] + ".json")
        atomic_json(path, dict(load_json(path), reason="Synthetic changed original bytes."))
        builder = Mock(side_effect=AssertionError("Do not conceal invalid original bytes with replanning."))
        with bind_work_turn(self.memory, turn["id"]):
            with self.assertRaisesRegex(SafetyError, "SOURCE_STAGED_RECEIPT_INVALID"):
                self.continue_run(builder, handlers={"meta-ads": handler})
        builder.assert_not_called()
        self.assertEqual(handler.call_count, 1)
        self.assertEqual(self.memory.snapshot()["query_tasks"][task_id]["receipt_refs"], [])

    def test_foreign_original_request_pointer_remains_rejected(self):
        turn, task_id, handler = self.staged_original()
        task = self.memory.snapshot()["query_tasks"][task_id]
        path = self.memory.root / "all_source_dispatch/returned_receipts" / task["acquisition_id"] / "pending.json"
        atomic_json(path, dict(load_json(path), request_hash="f" * 64))
        with bind_work_turn(self.memory, turn["id"]):
            with self.assertRaisesRegex(SafetyError, "SOURCE_STAGED_RECEIPT_INVALID"):
                self.continue_run(Mock(), handlers={"meta-ads": handler})
        self.assertEqual(handler.call_count, 1)
        self.assertEqual(self.memory.snapshot()["query_tasks"][task_id]["receipt_refs"], [])

    def test_spent_entry_preserves_actual_pending_preparation_counts(self):
        def builder(context):
            if context["route"] == "company-pages":
                raise SafetyError("SOURCE_SUPPLIER_DESTINATIONS_PENDING")
            return ({"query": "sink caddy", "country": "IN"} if context["route"] == "meta-ads"
                else {"not_applicable_reason": "Outside this synthetic route."})
        prepared = prepare_all_source_tasks(self.memory, run_id="week-one", request_builder=builder, as_of=NOW)
        turn = self.spent_turn()
        forbidden = Mock(side_effect=AssertionError("Existing pending gaps are not fresh attempts."))
        with bind_work_turn(self.memory, turn["id"]):
            result = self.continue_run(forbidden, handlers={"meta-ads": self.fixture.acquired})
        forbidden.assert_not_called()
        self.assertEqual(len(prepared["pending_preparation"]), 2)
        self.assertEqual(result["passes"][0]["pending_preparation"], 2)
        self.assertEqual(result["progress"]["pending_preparation"], 2)
        self.assertEqual(result["progress"]["pending_preparation_reasons"],
            {"SOURCE_SUPPLIER_DESTINATIONS_PENDING": 2})
        self.assertEqual(result["handler_calls"], 0)

    def test_missing_saved_run_still_uses_existing_planning_path(self):
        turn = self.spent_turn()
        builder = Mock(return_value={"query": "sink caddy", "country": "IN"})
        with bind_work_turn(self.memory, turn["id"]):
            result = self.continue_run(builder, handlers={route: self.fixture.acquired for route in ROUTES})
        self.assertEqual(builder.call_count, 2 * len(ROUTES))
        self.assertNotIn("preparation_wait", result)
        self.assertEqual(result["handler_calls"], 0)
        self.assertEqual(result["progress"]["physical_questions"], 14)

    def test_existing_run_with_no_turn_still_plans_normally(self):
        prepare_source_tasks(self.memory, [self.fixture.question()], run_id="week-one", as_of=NOW)
        builder = Mock(return_value={"query": "sink caddy", "country": "IN"})
        result = self.continue_run(builder, action_limit=1,
            handlers={route: self.fixture.acquired for route in ROUTES})
        self.assertEqual(builder.call_count, 4 * len(ROUTES))
        self.assertNotIn("preparation_wait", result)
        self.assertEqual(result["handler_calls"], 1)

    def test_closed_bound_turn_is_saved_only_without_reopening_it(self):
        prepare_source_tasks(self.memory, [self.fixture.question()], run_id="week-one", as_of=NOW)
        turn = self.spent_turn()
        builder = Mock(side_effect=AssertionError("A closed grant is not new planning authority."))
        with bind_work_turn(self.memory, turn["id"]):
            close_work_turn(self.memory, turn["id"], at=NOW, reason="Synthetic original round completed.")
            result = self.continue_run(builder, handlers={"meta-ads": self.fixture.acquired})
        builder.assert_not_called()
        self.assertEqual(result["preparation_wait"]["reason"], "WORK_TURN_CLOSED")
        self.assertEqual(result["handler_calls"], 0)
        self.assertEqual(self.memory.snapshot()["batch_runs"][turn["id"]]["state"], "closed")

    def test_shortcut_retains_exact_caller_and_run_policy_errors(self):
        prepare_source_tasks(self.memory, [self.fixture.question()], run_id="week-one", as_of=NOW)
        turn = self.spent_turn()
        cases = [
            ({"request_builder": None}, "SOURCE_REQUEST_BUILDER_REQUIRED"),
            ({"max_attempts": True}, "SOURCE_PLAN_INVALID"),
            ({"max_attempts": 0}, "SOURCE_PLAN_INVALID"),
            ({"retry_delay_seconds": True}, "SOURCE_PLAN_INVALID"),
            ({"retry_delay_seconds": -1}, "SOURCE_PLAN_INVALID"),
            ({"max_attempts": 3}, "SOURCE_RUN_POLICY_CHANGED"),
            ({"retry_delay_seconds": 301}, "SOURCE_RUN_POLICY_CHANGED"),
            ({"reuse_run_ids": ()}, "SOURCE_REUSE_SCOPE_INVALID"),
            ({"reuse_run_ids": ["week-one"]}, "SOURCE_REUSE_SCOPE_INVALID"),
            ({"reuse_discovery_cycle_ids": ()}, "SOURCE_REUSE_SCOPE_INVALID"),
            ({"concept_ids": ["first", "first"]}, "SOURCE_CONCEPTS_INVALID"),
            ({"concept_ids": ["missing"]}, "KNOWN_CONCEPT_REQUIRED"),
        ]
        for options, reason in cases:
            with self.subTest(options=options), bind_work_turn(self.memory, turn["id"]):
                parameters = {"request_builder": Mock(), **options}
                with self.assertRaisesRegex(SafetyError, reason):
                    continue_source_run(self.memory, "week-one", **parameters)

    def test_malformed_non_string_run_id_keeps_existing_validation(self):
        before = self.memory.snapshot()
        builder = Mock(return_value={"query": "sink caddy", "country": "IN"})
        with self.assertRaisesRegex(SafetyError, "INVALID_IDENTIFIER"):
            continue_source_run(self.memory, {"invalid": set()}, request_builder=builder)
        # The normal planner still owns validation; the early lookup must not
        # replace its fixed error with a JSON serialization exception.
        self.assertEqual(builder.call_count, 2 * len(ROUTES))
        self.assertEqual(self.memory.snapshot(), before)

    def test_source_phase_records_do_not_claim_results_or_website_deadlines(self):
        builder = Mock(return_value={"query": "sink caddy", "country": "IN"})
        events = []
        with patch.object(coverage, "record_event", side_effect=lambda name, **values: events.append((name, values))):
            self.continue_run(builder, action_limit=1, handlers={route: self.fixture.acquired for route in ROUTES})
        phases = [(name, values) for name, values in events if values.get("phase") == "source_preparation"]
        self.assertEqual([name for name, _ in phases], ["phase_started", "phase_finished"] * 2)
        turn = self.spent_turn()
        events.clear()
        with bind_work_turn(self.memory, turn["id"]), patch.object(coverage, "record_event",
                side_effect=lambda name, **values: events.append((name, values))):
            self.continue_run(Mock(side_effect=AssertionError("No fresh preparation.")), handlers={})
        recovery = [(name, values) for name, values in events if values.get("phase") == "source_saved_recovery"]
        self.assertEqual([name for name, _ in recovery], ["phase_started", "phase_finished"])
        self.assertFalse(any(values.get("phase") == "source_preparation" for _, values in events))
        for name, values in phases + recovery:
            self.assertIs(values["useful_result"], False)
            self.assertIsNone(values["deadline_seconds"])
            if name == "phase_finished":
                self.assertNotIn("elapsed_seconds", values)
                self.assertGreaterEqual(values["timings"][values["phase"]], 0)

    def test_source_phase_records_finish_without_hiding_planning_errors(self):
        events = []
        with patch.object(coverage, "record_event", side_effect=lambda name, **values: events.append((name, values))), \
                patch.object(coverage, "prepare_all_source_tasks", side_effect=OSError("Synthetic local failure.")):
            with self.assertRaisesRegex(OSError, "Synthetic local failure"):
                self.continue_run(Mock(), handlers={})
        self.assertEqual([name for name, values in events if values.get("phase") == "source_preparation"],
            ["phase_started", "phase_finished"])

    def test_saved_recovery_phase_finishes_without_hiding_original_errors(self):
        prepare_source_tasks(self.memory, [self.fixture.question()], run_id="week-one", as_of=NOW)
        turn = self.spent_turn()
        events = []
        builder = Mock(side_effect=AssertionError("Fresh planning is not a recovery route."))
        with bind_work_turn(self.memory, turn["id"]), \
                patch.object(coverage, "record_event", side_effect=lambda name, **values: events.append((name, values))), \
                patch.object(coverage, "execute_source_run", side_effect=SafetyError("SOURCE_STAGED_RECEIPT_INVALID")):
            with self.assertRaisesRegex(SafetyError, "SOURCE_STAGED_RECEIPT_INVALID"):
                self.continue_run(builder, handlers={})
        builder.assert_not_called()
        self.assertEqual([name for name, values in events if values.get("phase") == "source_saved_recovery"],
            ["phase_started", "phase_finished"])
        self.assertFalse(any(values.get("useful_result") for _, values in events))
        self.assertIn("source_saved_recovery", events[-1][1]["timings"])

    def test_saved_recovery_cursor_is_bounded_across_cold_restarts_and_settles(self):
        prepare_source_tasks(self.memory, [self.fixture.question(query="queued " + str(n))
            for n in range(140)], run_id="week-one", as_of=NOW)
        turn = self.spent_turn()
        builder = Mock(side_effect=AssertionError("Cold restart does not replenish the grant."))
        remaining = []
        scanned = []
        cursors = []
        for _ in range(4):
            self.memory = ResearchMemory(self.memory.root, self.memory.journal, evidence_type="synthetic")
            with bind_work_turn(self.memory, turn["id"]):
                result = self.continue_run(builder, handlers={"meta-ads": self.fixture.acquired})
            self.assertEqual(result["handler_calls"], 0)
            state = self.memory.snapshot()
            recovery = state["batch_runs"][key("source-run", "week-one")]["saved_recovery"]
            remaining.append(recovery["remaining"])
            scanned.append(sum(result["passes"][0]["counts"].values()))
            cursors.append(recovery["cursor"])
            self.assertEqual(state["batch_runs"][turn["id"]]["counters"]["source_callbacks"], 2)
        builder.assert_not_called()
        self.assertEqual(remaining, [76, 12, 0, 0])
        self.assertEqual(scanned, [64, 64, 12, 0])
        self.assertEqual(len(set(cursors[:3])), 3)

    def test_normal_programme_closes_spent_turn_after_reaching_later_stages(self):
        from product_radar.programme import STAGES, advance_programme_turn, prepare_programme
        prepare_programme(self.memory, "source-programme", source_run_id="week-one", as_of=NOW)
        prepare_source_tasks(self.memory, [self.fixture.question(query="queued " + str(n))
            for n in range(140)], run_id="week-one", as_of=NOW)
        turn = self.spent_turn()
        visited = []
        builder = Mock(side_effect=AssertionError("Do not prepare before the spent source pass."))
        def completed(stage):
            def step(*_args):
                visited.append(stage)
                return {"state": "nothing_new"}
            return step
        steps = {stage: completed(stage) for stage in STAGES}
        def acquire(memory, record, _context):
            visited.append("acquire")
            return continue_source_run(memory, record["source_run_id"], request_builder=builder,
                handlers={"meta-ads": self.fixture.acquired}, concept_ids=record["concept_ids"],
                action_limit=12, bounded_call=self.fixture.direct_runner)
        steps["acquire"] = acquire
        result = advance_programme_turn(self.memory, "source-programme", root=self.root,
            handlers={}, steps=steps, as_of=NOW)
        builder.assert_not_called()
        self.assertEqual(result["work_turn"]["id"], turn["id"])
        self.assertEqual(result["work_turn"]["state"], "closed")
        self.assertEqual(result["work_turn"]["counters"]["source_callbacks"], 2)
        for stage in ("match", "suppliers", "photos", "reports", "private_staging"):
            self.assertIn(stage, visited)


if __name__ == "__main__":
    unittest.main()
