"""Actual existing source collectors share a durable turn's callback budget."""

from copy import deepcopy
from pathlib import Path
import json
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from product_radar.catalogue_discovery import advance_discovery_cycle
from product_radar.research_memory import ResearchMemory
from product_radar.source_coverage import execute_source_run, prepare_source_tasks
from product_radar.work_turns import DEFAULT_POLICY, active_work_turn, bind_work_turn, open_work_turn
import test_catalogue_discovery as discovery_fixtures
import test_source_coverage as source_fixtures

NOW = source_fixtures.NOW


class SharedSourceTurnTests(unittest.TestCase):
    def setUp(self):
        self.fixture = source_fixtures.SourceCoverageTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.memory = self.fixture.memory

    def turn(self, budget=2):
        policy = deepcopy(DEFAULT_POLICY)
        policy.update(source_callbacks=budget, exploration_slots=0)
        return open_work_turn(self.memory, "programme", at=NOW, policy=policy)

    def test_controller_limit_is_shared_across_runs_and_intent_saved_atomically(self):
        self.fixture.plan([self.fixture.question(query="sink caddy")])
        prepare_source_tasks(self.memory, [self.fixture.question(query="draining tray")],
                             run_id="week-two", as_of=NOW)
        turn = self.turn(budget=1)
        with bind_work_turn(self.memory, turn["id"]):
            first = self.fixture.run_sources({"meta-ads": self.fixture.acquired})
            second = execute_source_run(self.memory, "week-two", as_of=NOW,
                handlers={"meta-ads": self.fixture.acquired}, bounded_call=self.fixture.direct_runner)
            self.assertEqual(active_work_turn(self.memory)["counters"]["source_callbacks"], 1)
        self.assertEqual(first["handler_calls"], 1)
        self.assertEqual(second["handler_calls"], 0)
        task = next(row for row in self.memory.snapshot()["query_tasks"].values() if row.get("run_id") == "week-two")
        self.assertEqual(task["status"], "prepared")
        self.assertNotIn("acquisition_id", task)
        events = [json.loads(path.read_text()) for path in (self.memory.root / "events").glob("*.json")]
        joined = [event for event in events if any(row["table"] == "query_tasks" and row["value"].get("status") == "dispatching"
                  for row in event["changes"]) and any(row["table"] == "batch_runs" and row["value"].get("kind") == "work-turn-v1"
                  for row in event["changes"])]
        self.assertEqual(len(joined), 1)

    def test_reconciliation_callback_uses_another_slot_but_final_saved_result_does_not(self):
        self.fixture.plan([self.fixture.question()])
        calls = []
        def interrupted(context):
            calls.append("initial")
            context["record_dispatch"]()
            raise RuntimeError("Synthetic lost local return")
        def recover(context):
            calls.append("saved_only")
            self.assertTrue(context["reconciling"])
            return self.fixture.receipt(context)
        handlers = {"meta-ads": {"handler": interrupted, "reconcile": recover}}
        turn = self.turn()
        with bind_work_turn(self.memory, turn["id"]):
            self.fixture.run_sources(handlers)
        restarted = ResearchMemory(self.memory.root, self.memory.journal, evidence_type="synthetic")
        with bind_work_turn(restarted, turn["id"]):
            execute_source_run(restarted, "week-one", handlers=handlers, as_of=NOW,
                               bounded_call=self.fixture.direct_runner)
            execute_source_run(restarted, "week-one", handlers=handlers, as_of=NOW,
                               bounded_call=self.fixture.direct_runner)
            self.assertEqual(active_work_turn(restarted)["counters"]["source_callbacks"], 2)
        self.assertEqual(calls, ["initial", "saved_only"])

    def test_discovery_and_validation_use_same_limit_not_two_allowances(self):
        discovery = discovery_fixtures.DiscoveryTests()
        discovery.setUp()
        self.addCleanup(discovery.doCleanups)
        discovery.memory = self.memory
        handler = discovery.handler()
        cycle = discovery.prepare(handlers={"meta-ads": handler})
        self.fixture.plan([self.fixture.question(query="draining caddy")])
        turn = self.turn(budget=1)
        with bind_work_turn(self.memory, turn["id"]):
            advance_discovery_cycle(self.memory, cycle["id"], handlers={"meta-ads": handler},
                as_of=NOW, max_actions=1, admission=discovery.no_admission)
            self.fixture.run_sources({"meta-ads": self.fixture.acquired})
            self.assertEqual(active_work_turn(self.memory)["counters"]["source_callbacks"], 1)
        self.assertEqual(len(discovery.calls), 1)
        task = next(row for row in self.memory.snapshot()["query_tasks"].values() if row.get("run_id") == "week-one")
        self.assertEqual(task["status"], "prepared")


if __name__ == "__main__":
    unittest.main()
