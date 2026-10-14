"""Independent finite-source preparation checks; only disposable originals."""

from copy import deepcopy
from datetime import timedelta
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import product_radar.programme as programme
import product_radar.source_coverage as coverage
from product_radar.models import SafetyError, digest, timestamp
from product_radar.operations import operation_run, operation_writer, read_operation_status
from product_radar.work_turns import (DEFAULT_POLICY, bind_work_turn,
    close_work_turn, open_work_turn, reserve_source_callback)
import test_source_coverage as source_fixtures


NOW = source_fixtures.NOW


class SourcePreparationIndependentReviewTests(unittest.TestCase):
    setUp = source_fixtures.SourceCoverageTests.setUp
    product = source_fixtures.SourceCoverageTests.product
    question = source_fixtures.SourceCoverageTests.question
    receipt = source_fixtures.SourceCoverageTests.receipt
    acquired = source_fixtures.SourceCoverageTests.acquired
    direct_runner = source_fixtures.SourceCoverageTests.direct_runner
    query_profile = source_fixtures.SourceCoverageTests.query_profile

    def turn(self, *, spent=True, at=NOW):
        policy = deepcopy(DEFAULT_POLICY)
        policy.update(source_callbacks=1, exploration_slots=0)
        row = open_work_turn(self.memory, "independent-sources", at=at,
                             policy=policy, ready_lanes={"backlog": True, "exploration": False})
        if spent:
            with bind_work_turn(self.memory, row["id"]):
                reserve_source_callback(self.memory, "already-consumed-source", at=at)
        return row

    def continue_sources(self, builder, **options):
        with patch.object(coverage, "utc_now", return_value=NOW):
            return coverage.continue_source_run(self.memory, "week-one", request_builder=builder,
                bounded_call=self.direct_runner, **options)

    def test_spent_turn_preserves_original_targets_after_description_and_status_changes(self):
        prepared = coverage.prepare_source_tasks(self.memory,
            [self.question(ids=["first", "second"])], run_id="week-one", as_of=NOW)
        task_id = prepared["task_ids"][0]
        original = deepcopy(self.memory.snapshot()["query_tasks"][task_id])
        first = deepcopy(self.memory.snapshot()["concepts"]["first"])
        first["description"]["label"] = "Different current sink organiser"
        self.memory.put("concepts", "first", first, previous=self.memory.snapshot()["concepts"]["first"])
        second = deepcopy(self.memory.snapshot()["concepts"]["second"])
        second["status"] = "retired"
        self.memory.put("concepts", "second", second, previous=self.memory.snapshot()["concepts"]["second"])
        turn = self.turn()
        builder = Mock(side_effect=AssertionError("Fresh question planning was not allowed."))
        handler = Mock(side_effect=AssertionError("Stale targets cannot start a source read."))
        with bind_work_turn(self.memory, turn["id"]):
            result = self.continue_sources(builder, concept_ids=["first", "second"],
                                           handlers={"meta-ads": handler})
        builder.assert_not_called()
        handler.assert_not_called()
        saved = self.memory.snapshot()
        self.assertEqual(saved["query_tasks"][task_id]["targets"], original["targets"])
        self.assertEqual(saved["query_tasks"][task_id]["generation"], 0)
        self.assertEqual(saved["batch_runs"][turn["id"]]["counters"]["source_callbacks"], 1)
        self.assertEqual(result["handler_calls"], 0)

    def test_changed_accepted_query_profile_preserves_old_task_until_genuine_next_turn(self):
        from product_radar.product_queries import _from_type, _record, query_profile_input
        self.query_profile("first", alternatives=False)
        initial_builder = coverage.product_source_request_builder(run_id="week-one")
        coverage.prepare_all_source_tasks(self.memory, run_id="week-one", request_builder=initial_builder,
            concept_ids=["first"], as_of=NOW)
        original = next((tid, deepcopy(task)) for tid, task in self.memory.snapshot()["query_tasks"].items()
                        if task.get("run_id") == "week-one" and task["route"] == "meta-ads")
        later = (timestamp(NOW) + timedelta(seconds=1)).isoformat()
        item = query_profile_input(self.memory.snapshot(), "first")
        profile = _from_type(item, "draining sink holder", method="synthetic")
        _record(self.memory, item, profile, "synthetic", later)
        turn = self.turn()
        current_builder = coverage.product_source_request_builder(run_id="week-one")
        with bind_work_turn(self.memory, turn["id"]), patch.object(coverage, "utc_now", return_value=later):
            with patch.object(coverage, "prepare_all_source_tasks", side_effect=AssertionError("Spent turn rebuilt profile questions.")):
                result = coverage.continue_source_run(self.memory, "week-one", request_builder=current_builder,
                    concept_ids=["first"], handlers={}, bounded_call=self.direct_runner)
        self.assertEqual(result["handler_calls"], 0)
        retained = self.memory.snapshot()["query_tasks"][original[0]]
        self.assertEqual(retained["request"], original[1]["request"])
        self.assertEqual(retained["query_plans"], original[1]["query_plans"])
        close_work_turn(self.memory, turn["id"], at=later, reason="Actual old source allowance finished.")
        next_turn = self.turn(spent=False, at=later)
        calls = []
        def actual_handler(context):
            calls.append(context["request"]["query"])
            return self.acquired(context)
        with bind_work_turn(self.memory, next_turn["id"]), patch.object(coverage, "utc_now", return_value=later):
            coverage.continue_source_run(self.memory, "week-one", request_builder=current_builder,
                concept_ids=["first"], handlers={"meta-ads": actual_handler}, action_limit=1,
                bounded_call=self.direct_runner)
        new_tasks = [task for task in self.memory.snapshot()["query_tasks"].values()
                     if task.get("run_id") == "week-one" and task["route"] == "meta-ads"
                     and task["request"]["query"] == "draining sink holder"]
        self.assertEqual(len(new_tasks), 1)
        self.assertNotEqual(new_tasks[0]["query_plans"]["first"]["profile_hash"],
                            original[1]["query_plans"]["first"]["profile_hash"])
        self.assertEqual(len(calls), 1)

    def test_new_target_is_deferred_then_planned_in_genuine_next_turn(self):
        prepared = coverage.prepare_source_tasks(self.memory,
            [self.question(ids=["first"])], run_id="week-one", as_of=NOW)
        self.product("third")
        task_id = prepared["task_ids"][0]
        turn = self.turn()
        builder = Mock(side_effect=AssertionError("New target planning needs fresh allowance."))
        with bind_work_turn(self.memory, turn["id"]):
            result = self.continue_sources(builder, concept_ids=["first", "third"], handlers={})
        builder.assert_not_called()
        self.assertEqual(set(self.memory.snapshot()["query_tasks"][task_id]["targets"]), {"first"})
        self.assertEqual(result["handler_calls"], 0)
        close_work_turn(self.memory, turn["id"], at=NOW, reason="Original source allowance consumed.")
        next_turn = self.turn(spent=False)

        def current_builder(context):
            return ({"query": "sink caddy", "country": "IN"} if context["route"] == "meta-ads"
                    else {"not_applicable_reason": "Outside this synthetic focused route."})

        calls = []
        def actual_handler(context):
            calls.append(deepcopy(context["concept_ids"]))
            return self.acquired(context)
        with bind_work_turn(self.memory, next_turn["id"]):
            result = self.continue_sources(current_builder, concept_ids=["first", "third"],
                handlers={"meta-ads": actual_handler}, action_limit=1)
        self.assertNotEqual(next_turn["id"], turn["id"])
        self.assertEqual(calls, [["first", "third"]])
        self.assertEqual(result["handler_calls"], 1)
        self.assertEqual(set(self.memory.snapshot()["query_tasks"][task_id]["targets"]), {"first", "third"})
        self.assertEqual(self.memory.snapshot()["batch_runs"][turn["id"]]["counters"]["source_callbacks"], 1)

    def test_spent_shortcut_does_not_accept_malformed_planning_inputs_or_changed_run_policy(self):
        coverage.prepare_source_tasks(self.memory, [self.question()], run_id="week-one", as_of=NOW)
        turn = self.turn()
        invalid = [
            {"request_builder": None},
            {"max_attempts": True},
            {"max_attempts": 3},
            {"retry_delay_seconds": -1},
            {"retry_delay_seconds": 301},
            {"reuse_run_ids": "not-a-list"},
            {"reuse_run_ids": ["week-one"]},
            {"reuse_discovery_cycle_ids": ["invalid/id"]},
            {"concept_ids": ["first", "first"]},
            {"concept_ids": ["missing-concept"]},
            {"shared_marketplace": "yes"},
            {"on_progress": True},
            {"timeout_seconds": False},
        ]
        for options in invalid:
            with self.subTest(options=options), bind_work_turn(self.memory, turn["id"]):
                builder = options.get("request_builder", lambda _context: {"query": "sink caddy", "country": "IN"})
                remaining = {name: value for name, value in options.items() if name != "request_builder"}
                with self.assertRaises(SafetyError):
                    self.continue_sources(builder, **remaining)

    def recorded_rows(self, recorder):
        path = recorder.folder / (recorder.run_id + ".jsonl")
        return [json.loads(line) for line in path.read_text().splitlines()]

    def assert_phase_pair(self, rows, phase):
        selected = [row for row in rows if row.get("phase") == phase]
        started = [row for row in selected if row["event"] == "phase_started"]
        finished = [row for row in selected if row["event"] == "phase_finished"]
        self.assertEqual(len(started), 1, selected)
        self.assertEqual(len(finished), 1, selected)
        self.assertFalse(started[0]["useful_result"])
        self.assertFalse(finished[0]["useful_result"])
        duration = finished[0]["timings"][phase]
        self.assertGreaterEqual(duration, 0)
        self.assertGreaterEqual(finished[0]["elapsed_seconds"], duration)
        self.assertNotIn("deadline_seconds", started[0])
        self.assertNotIn("deadline_seconds", finished[0])

    def test_actual_preparation_records_local_duration_without_claiming_a_website_result(self):
        statuses = []
        def builder(_context):
            statuses.append(read_operation_status(self.root))
            return {"not_applicable_reason": "Outside this synthetic phase-only test."}
        with operation_run(self.root) as recorder, operation_writer():
            result = self.continue_sources(builder, concept_ids=["first"], handlers={})
            status = read_operation_status(self.root)
            self.assertEqual(status["counts"]["completed_results"], 0)
            self.assertIsNone(status["last_useful_result_at"])
        self.assertEqual(result["handler_calls"], 0)
        self.assertEqual(len(statuses), len(coverage.PLANNED_ROUTES))
        for status in statuses:
            self.assertEqual(status["phase"], "source_preparation")
            self.assertEqual(status["message"], "Preparing website searches from the saved product information.")
            self.assertIsNone(status["phase_deadline_seconds"])
        self.assert_phase_pair(self.recorded_rows(recorder), "source_preparation")

    def test_actual_spent_sweep_records_saved_recovery_without_fresh_preparation(self):
        coverage.prepare_source_tasks(self.memory, [self.question()], run_id="week-one", as_of=NOW)
        turn = self.turn()
        builder = Mock(side_effect=AssertionError("Spent turn cannot build fresh questions."))
        actual_execute = coverage.execute_source_run
        statuses = []
        def inspect_then_execute(*args, **kwargs):
            statuses.append(read_operation_status(self.root))
            return actual_execute(*args, **kwargs)
        with operation_run(self.root) as recorder, operation_writer(), bind_work_turn(self.memory, turn["id"]):
            with patch.object(coverage, "execute_source_run", side_effect=inspect_then_execute):
                result = self.continue_sources(builder, concept_ids=["first"], handlers={})
            self.assertEqual(read_operation_status(self.root)["counts"]["completed_results"], 0)
        builder.assert_not_called()
        self.assertEqual(result["handler_calls"], 0)
        self.assertEqual(len(statuses), 1)
        self.assertEqual(statuses[0]["phase"], "source_saved_recovery")
        self.assertEqual(statuses[0]["message"], "Checking saved website replies without starting new searches.")
        self.assertIsNone(statuses[0]["phase_deadline_seconds"])
        rows = self.recorded_rows(recorder)
        self.assert_phase_pair(rows, "source_saved_recovery")
        self.assertFalse(any(row.get("phase") == "source_preparation" for row in rows))

    def test_preparation_exception_keeps_its_exact_reason_and_closes_the_local_phase(self):
        builder = Mock()
        with operation_run(self.root) as recorder, operation_writer():
            with patch.object(coverage, "prepare_all_source_tasks", side_effect=SafetyError("SOURCE_SYNTHETIC_PREPARATION_FAILED")):
                with patch.object(coverage, "execute_source_run") as execute:
                    with self.assertRaisesRegex(SafetyError, "^SOURCE_SYNTHETIC_PREPARATION_FAILED$"):
                        self.continue_sources(builder, concept_ids=["first"], handlers={})
            execute.assert_not_called()
            self.assertEqual(read_operation_status(self.root)["counts"]["completed_results"], 0)
        builder.assert_not_called()
        self.assert_phase_pair(self.recorded_rows(recorder), "source_preparation")

    def test_saved_recovery_exception_keeps_its_exact_reason_and_never_prepares_fresh_work(self):
        coverage.prepare_source_tasks(self.memory, [self.question()], run_id="week-one", as_of=NOW)
        turn = self.turn()
        builder = Mock()
        with operation_run(self.root) as recorder, operation_writer(), bind_work_turn(self.memory, turn["id"]):
            with patch.object(coverage, "execute_source_run", side_effect=SafetyError("SOURCE_STAGED_RECEIPT_INVALID")):
                with self.assertRaisesRegex(SafetyError, "^SOURCE_STAGED_RECEIPT_INVALID$"):
                    self.continue_sources(builder, concept_ids=["first"], handlers={})
            self.assertEqual(read_operation_status(self.root)["counts"]["completed_results"], 0)
        builder.assert_not_called()
        rows = self.recorded_rows(recorder)
        self.assert_phase_pair(rows, "source_saved_recovery")
        self.assertFalse(any(row.get("phase") == "source_preparation" for row in rows))


class CompanyPreparationReadinessIndependentTests(unittest.TestCase):
    def test_real_completed_supplier_search_keeps_new_company_work_ready_during_model_wait(self):
        from test_functional_source_market import functional_pool_fixture, NOW as at
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        root = Path(temporary.name)
        memory, (cid,) = functional_pool_fixture(root, count=1, with_market=False)
        record = programme.prepare_programme(memory, "company-continuation",
            source_run_id="company-run", concept_ids=[cid], as_of=at)

        def builder(context):
            if context["route"] == "tradeindia":
                return {"query": "sink caddy", "country": "IN"}
            if context["route"] == "company-pages":
                if "synthetic-company-destination" not in context["state"]["source_checks"]:
                    raise SafetyError("SOURCE_SUPPLIER_DESTINATIONS_PENDING")
                return {"candidate_urls": {cid: ["https://supplier.example.org/caddy"]},
                        "candidate_check_ids": {cid: ["synthetic-company-destination"]}, "country": "IN"}
            return {"not_applicable_reason": "Outside this synthetic supplier follow-up."}

        coverage.prepare_all_source_tasks(memory, run_id="company-run", request_builder=builder,
                                          concept_ids=[cid], as_of=at)
        policy = deepcopy(DEFAULT_POLICY)
        policy.update(source_callbacks=1, exploration_slots=0)
        turn = open_work_turn(memory, record["programme_id"], at=at, policy=policy,
                             ready_lanes={"backlog": True, "exploration": False})
        calls = []
        def search(context):
            calls.append(context["route"])
            context["record_dispatch"]()
            memory.put("source_checks", "synthetic-company-destination", {
                "kind": "synthetic-supplier-destination", "concept_id": cid,
                "description_hash": digest(context["state"]["concepts"][cid]["description"])
                if "state" in context else context["description_hashes"][cid],
                "url": "https://supplier.example.org/caddy", "at": at})
            return {"schema_version": 1, "route": context["route"], "acquisition_id": context["acquisition_id"],
                "observed_at": at, "state": "empty", "dispatch_started": True,
                "reference": context["acquisition_id"] + "/receipt.json", "capture_ids": [],
                "observation_ids": [], "target_results": [], "reason": "Synthetic saved supplier destination.",
                "retryable": False}
        company = Mock(side_effect=AssertionError("Company read needs a later actual source grant."))
        handlers = {"tradeindia": search, "company-pages": company}
        with bind_work_turn(memory, turn["id"]), patch.object(coverage, "utc_now", return_value=at):
            deferred = coverage.continue_source_run(memory, "company-run", request_builder=builder,
                concept_ids=[cid], handlers=handlers, action_limit=1,
                bounded_call=lambda callback, context, _timeout: callback(context))
        close_work_turn(memory, turn["id"], at=at, reason="Finite supplier search used its original allowance.")
        self.assertEqual(calls, ["tradeindia"])
        company.assert_not_called()
        self.assertEqual(deferred["handler_calls"], 1)
        state = memory.snapshot()
        record["progress"] = programme.programme_progress(memory, record["programme_id"],
            record=record, state=state, handlers=handlers, as_of=at)
        record["model_pause"] = {"reason": "AI_RATE_LIMITED", "at": at,
            "next_retry_at": (timestamp(at) + timedelta(hours=2)).isoformat()}
        decision = programme.programme_turn_readiness(memory, record, root=root,
            handlers=handlers, runner=None, state=state, as_of=at)
        self.assertTrue(decision["ready_lanes"]["backlog"], decision)
        self.assertTrue(decision["available"], decision)

    def test_saved_supplier_original_prepares_company_once_then_keeps_normal_readiness(self):
        from test_functional_source_market import functional_pool_fixture, NOW as at
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        root = Path(temporary.name)
        memory, (cid,) = functional_pool_fixture(root, count=1, with_market=False)
        record = programme.prepare_programme(memory, "saved-company-continuation",
            source_run_id="company-run", concept_ids=[cid], as_of=at)
        builder_calls = []

        def builder(context):
            builder_calls.append(context["route"])
            if context["route"] == "tradeindia":
                return {"query": "sink caddy", "country": "IN"}
            if context["route"] == "company-pages":
                imported = [row for row in context["state"]["source_checks"].values()
                    if row.get("kind") == coverage.CHECK_KIND and row.get("route") == "tradeindia"
                    and row.get("envelope", {}).get("target_results", {}).get(cid, {}).get("description_hash")
                        == context["description_hash"]]
                if not imported:
                    raise SafetyError("SOURCE_SUPPLIER_DESTINATIONS_PENDING")
                destination = context["state"]["source_checks"]["synthetic-company-destination"]
                return {"candidate_urls": {cid: [destination["url"]]},
                        "candidate_check_ids": {cid: ["synthetic-company-destination"]}, "country": "IN"}
            return {"not_applicable_reason": "Outside this synthetic original supplier recovery."}

        coverage.prepare_all_source_tasks(memory, run_id="company-run", request_builder=builder,
                                          concept_ids=[cid], as_of=at)
        policy = deepcopy(DEFAULT_POLICY)
        policy.update(source_callbacks=1, exploration_slots=0)
        turn = open_work_turn(memory, record["programme_id"], at=at, policy=policy,
                             ready_lanes={"backlog": True, "exploration": False})
        search_calls = []

        def search(context):
            search_calls.append(context["acquisition_id"])
            context["record_dispatch"]()
            memory.put("source_checks", "synthetic-company-destination", {
                "kind": "synthetic-supplier-destination", "concept_id": cid,
                "description_hash": context["description_hashes"][cid],
                "url": "https://supplier.example.org/caddy", "at": at})
            return {"schema_version": 1, "route": context["route"], "acquisition_id": context["acquisition_id"],
                "observed_at": at, "state": "usable", "dispatch_started": True,
                "reference": context["acquisition_id"] + "/receipt.json", "capture_ids": [],
                "observation_ids": [], "target_results": [], "reason": "Synthetic original supplier reply.",
                "retryable": False}

        company = Mock(side_effect=AssertionError("Company website needs a later real source grant."))
        handlers = {"tradeindia": search, "company-pages": company}
        direct = lambda callback, context, _timeout: callback(context)
        with bind_work_turn(memory, turn["id"]), patch.object(coverage, "utc_now", return_value=at):
            with patch.object(coverage, "_persist_receipt", side_effect=RuntimeError("Synthetic interrupted original application.")):
                initial = coverage.execute_source_run(memory, "company-run", handlers=handlers,
                    as_of=at, bounded_call=direct)
        self.assertEqual(initial["handler_calls"], 1)
        original_id, original = next((tid, deepcopy(task)) for tid, task in memory.snapshot()["query_tasks"].items()
                                    if task.get("run_id") == "company-run" and task["route"] == "tradeindia")
        self.assertEqual(original["receipt_refs"], [])
        self.assertTrue(original["reconcile_required"])
        self.assertFalse(any(task.get("route") == "company-pages" for task in memory.snapshot()["query_tasks"].values()))
        builder_calls.clear()
        actual_prepare = coverage.prepare_all_source_tasks
        with bind_work_turn(memory, turn["id"]), patch.object(coverage, "utc_now", return_value=at):
            with patch.object(coverage, "prepare_all_source_tasks", wraps=actual_prepare) as prepare:
                recovered = coverage.continue_source_run(memory, "company-run", request_builder=builder,
                    concept_ids=[cid], handlers=handlers, action_limit=1, bounded_call=direct)
        self.assertEqual(prepare.call_count, 1)
        self.assertEqual(builder_calls, list(coverage.PLANNED_ROUTES))
        self.assertEqual(recovered["handler_calls"], 0)
        self.assertEqual(recovered["progress"]["pending_preparation"], 0)
        self.assertEqual(search_calls, [original["acquisition_id"]])
        company.assert_not_called()
        state = memory.snapshot()
        saved = state["query_tasks"][original_id]
        self.assertEqual(len(saved["receipt_refs"]), 1)
        for field in ("acquisition_id", "request", "targets", "generation"):
            self.assertEqual(saved[field], original[field])
        company_tasks = [task for task in state["query_tasks"].values()
                         if task.get("run_id") == "company-run" and task["route"] == "company-pages"]
        self.assertEqual(len(company_tasks), 1)
        self.assertEqual(company_tasks[0]["status"], "prepared")
        self.assertEqual(state["batch_runs"][turn["id"]]["counters"]["source_callbacks"], 1)

        forbidden = Mock(side_effect=AssertionError("Reused original cannot repeatedly rebuild company dependencies."))
        with bind_work_turn(memory, turn["id"]), patch.object(coverage, "utc_now", return_value=at):
            with patch.object(coverage, "prepare_all_source_tasks", wraps=actual_prepare) as prepare_again:
                unchanged = coverage.continue_source_run(memory, "company-run", request_builder=forbidden,
                    concept_ids=[cid], handlers=handlers, action_limit=1, bounded_call=direct)
        forbidden.assert_not_called()
        prepare_again.assert_not_called()
        self.assertEqual(unchanged["handler_calls"], 0)
        self.assertEqual(len(search_calls), 1)
        close_work_turn(memory, turn["id"], at=at, reason="Original supplier allowance finished after saved recovery.")
        state = memory.snapshot()
        record["progress"] = programme.programme_progress(memory, record["programme_id"],
            record=record, state=state, handlers=handlers, as_of=at)
        record["model_pause"] = {"reason": "AI_RATE_LIMITED", "at": at,
            "next_retry_at": (timestamp(at) + timedelta(hours=2)).isoformat()}
        decision = programme.programme_turn_readiness(memory, record, root=root,
            handlers=handlers, runner=None, state=state, as_of=at)
        self.assertTrue(decision["ready_lanes"]["backlog"], decision)
        self.assertTrue(decision["available"], decision)


if __name__ == "__main__":
    unittest.main()
