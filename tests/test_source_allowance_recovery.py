"""Installed shared resolver and cold restart; all source bytes are fixtures."""

from copy import deepcopy
from pathlib import Path
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from product_radar.models import SafetyError
from product_radar.programme import advance_programme, advance_programme_turn, advance_ready_programme_turn
from product_radar.research_memory import ResearchMemory, key
from product_radar.source_coverage import SAVED_RECOVERY_SCAN_LIMIT, execute_source_run, prepare_source_tasks
from product_radar.storage import Journal
from product_radar.work_turns import DEFAULT_POLICY, bind_work_turn, open_work_turn
import test_shared_marketplace_acquisitions as fixtures

NOW = fixtures.NOW


class SourceAllowanceRecoveryTests(unittest.TestCase):
    setUp = fixtures.SharedMarketplaceTests.setUp
    handlers = fixtures.SharedMarketplaceTests.handlers
    owners = fixtures.SharedMarketplaceTests.owners

    def run_sources(self, handlers, **kwargs):
        return execute_source_run(self.memory, "tail", handlers=handlers, as_of=NOW,
            shared_marketplace=True, action_limit=1,
            bounded_call=lambda callback, context, timeout: callback(context), **kwargs)

    def turn(self):
        policy = deepcopy(DEFAULT_POLICY)
        policy.update(source_callbacks=1, exploration_slots=0)
        return open_work_turn(self.memory, "allowance-programme", at=NOW, policy=policy)

    def questions(self, count):
        return [{"route": "amazon-search", "request": {"query": "sink caddy " + str(n), "country": "IN"},
                 "concept_ids": ["sink"]} for n in range(count)]

    def test_exhausted_real_resolver_does_not_reserve_the_queued_tail(self):
        prepare_source_tasks(self.memory, self.questions(90), run_id="tail", as_of=NOW)
        handlers = self.handlers()
        result = self.run_sources(handlers)
        self.assertEqual(result["handler_calls"], 1)
        self.assertEqual(len(self.calls), 1)
        self.assertEqual(len(self.owners()), 1)
        self.assertEqual(result["saved_recovery_scanned"], SAVED_RECOVERY_SCAN_LIMIT)
        self.assertEqual(sum(row["generation"] > 0 for row in self.memory.snapshot()["query_tasks"].values()), 1)

    def test_later_saved_reply_is_recovered_with_no_allowance_across_cold_restarts(self):
        prepared = prepare_source_tasks(self.memory, self.questions(140), run_id="tail", as_of=NOW)
        latest = sorted(prepared["task_ids"])[-1]
        request = self.memory.snapshot()["query_tasks"][latest]["request"]
        prepare_source_tasks(self.memory, [{"route": "amazon-search", "request": request,
            "concept_ids": ["sink"]}], run_id="prior", as_of=NOW)
        handlers = self.handlers()
        execute_source_run(self.memory, "prior", handlers=handlers, as_of=NOW,
            shared_marketplace=True, bounded_call=lambda callback, context, timeout: callback(context))
        turn = self.turn()
        with bind_work_turn(self.memory, turn["id"]):
            first = self.run_sources(handlers)
        cursor = first["saved_recovery_cursor"]
        self.assertIsNotNone(cursor)
        self.assertEqual(len(self.owners()), 2)
        for _ in range(3):
            self.memory = ResearchMemory(self.memory.root, self.memory.journal, evidence_type="synthetic")
            handlers = self.handlers()
            with bind_work_turn(self.memory, turn["id"]):
                result = self.run_sources(handlers)
            self.assertEqual(result["handler_calls"], 0)
            self.assertLessEqual(result["saved_recovery_scanned"], SAVED_RECOVERY_SCAN_LIMIT)
            if self.memory.snapshot()["query_tasks"][latest]["status"] == "acquired":
                break
        self.assertEqual(self.memory.snapshot()["query_tasks"][latest]["status"], "acquired")
        self.assertEqual(len(self.calls), 2)
        self.assertEqual(len(self.owners()), 2)
        self.assertEqual(self.memory.snapshot()["batch_runs"][turn["id"]]["counters"]["source_callbacks"], 1)
        self.assertEqual(self.memory.snapshot()["batch_runs"][key("source-run", "tail")]["saved_recovery_cursor"],
                         result["saved_recovery_cursor"])

    def test_losing_programme_wake_does_not_read_research_history(self):
        with Journal(self.memory.root / "programme_writer").locked():
            with patch.object(self.memory, "snapshot", side_effect=AssertionError("Must admit the writer first.")):
                for operation in (
                    lambda: advance_programme(self.memory, "another", root=self.root),
                    lambda: advance_programme_turn(self.memory, "another", root=self.root),
                    lambda: advance_ready_programme_turn(self.memory, root=self.root, handlers={}),
                ):
                    with self.assertRaisesRegex(SafetyError, "RUNNER_ALREADY_ACTIVE"):
                        operation()

    def test_saved_scan_settles_after_one_finite_sweep_of_the_same_spent_turn(self):
        prepare_source_tasks(self.memory, self.questions(140), run_id="tail", as_of=NOW)
        handlers = self.handlers(); turn = self.turn()
        scanned = []
        with bind_work_turn(self.memory, turn["id"]):
            for _ in range(5):
                result = self.run_sources(handlers)
                scanned.append(result["saved_recovery_scanned"])
                self.assertEqual(result["handler_calls"], 1 if len(scanned) == 1 else 0)
        self.assertEqual(scanned, [64, 64, 11, 0, 0])
        self.assertFalse(result["saved_recovery_pending"])
        self.assertEqual(len(self.calls), 1)
        self.assertEqual(len(self.owners()), 1)

    def test_scan_cursor_survives_busy_canonical_progress_save(self):
        import product_radar.source_coverage as coverage
        prepare_source_tasks(self.memory, self.questions(140), run_id="tail", as_of=NOW)
        handlers = self.handlers(); turn = self.turn(); original = coverage._upsert
        def busy(memory, table, *args, **kwargs):
            if table == "batch_runs":
                raise SafetyError("RUNNER_ALREADY_ACTIVE")
            return original(memory, table, *args, **kwargs)
        with bind_work_turn(self.memory, turn["id"]):
            with patch.object(coverage, "_upsert", side_effect=busy):
                first = self.run_sources(handlers)
            second = self.run_sources(handlers)
        self.assertEqual(first["saved_recovery_scanned"], 64)
        self.assertEqual(second["saved_recovery_scanned"], 64)
        self.assertNotEqual(first["saved_recovery_cursor"], second["saved_recovery_cursor"])
        self.assertEqual(len(self.calls), 1)

    def test_normal_programme_reaches_later_stages_after_real_resolver_allowance_is_spent(self):
        from product_radar.programme import prepare_programme, STAGES
        prepare_programme(self.memory, "allowance-programme", source_run_id="tail", as_of=NOW)
        prepare_source_tasks(self.memory, self.questions(140), run_id="tail", as_of=NOW)
        handlers = self.handlers(); visited = []
        def completed(stage):
            def step(*_args):
                visited.append(stage)
                return {"state": "nothing_new"}
            return step
        steps = {stage: completed(stage) for stage in STAGES}
        steps["acquire"] = lambda *_args: self.run_sources(handlers)
        policy = deepcopy(DEFAULT_POLICY); policy.update(source_callbacks=1, exploration_slots=0)
        result = advance_ready_programme_turn(self.memory, root=self.root, programme_id="allowance-programme",
            handlers=handlers, steps=steps, turn_policy=policy, as_of=NOW)
        self.assertTrue(result["round_completed"])
        self.assertEqual(result["work_turn"]["counters"]["source_callbacks"], 1)
        self.assertEqual(len(self.calls), 1)
        self.assertEqual(len(self.owners()), 1)
        for stage in ("match", "suppliers", "photos", "reports", "private_staging"):
            self.assertIn(stage, visited)
        self.assertEqual(result["next_action"], "continue_ready_work")


if __name__ == "__main__":
    unittest.main()
