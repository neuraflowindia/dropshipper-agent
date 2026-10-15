"""Whole-batch capture/replay tests use isolated providers and model fixtures."""

from copy import deepcopy
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
from product_radar.models import digest
from product_radar.models import SafetyError
from product_radar.providers.apify import map_prepared_rows
from product_radar.research_batch import execute_plan
from product_radar.research_memory import ResearchMemory
from product_radar.research_operations import plan_research
from product_radar.storage import Journal, atomic_json
from test_benchmark_foundation import CONFIG, binding, cli
from test_research_flow import FakeRunner, rows
from test_research_planner import catalogue, NOW
from product_radar.models import prepared_request
from product_radar.research_planner import semantic_key
from product_radar.fit_policy import ACTIVE_CRITERION_IDS, FIT_POLICY_ID


class FixtureController:
    def __init__(self, suite):
        self.suite = suite
    def preflight(self, attempt_id):
        self.suite.preflights.append(attempt_id)
        self.suite.assertTrue(self.suite.memory.journal._locked)
        return {"ready_for_live": True}
    def run(self, attempt_id, resume_only=False):
        suite, journal = self.suite, self.suite.memory.journal
        suite.assertTrue(journal._locked)
        suite.runs.append((attempt_id, resume_only))
        prepared = suite.prepared[attempt_id]
        if not resume_only:
            suite.launches.append(attempt_id)
            journal.reserve(prepared, suite.account, read_reserve="0.02")
            journal.append("submit-" + attempt_id, "submitting", {"attempt": attempt_id})
        behavior = suite.behaviors.get(attempt_id, "success")
        if behavior == "unknown":
            return {"verdict": "Blocked", "code": "UNRESOLVED_PROVIDER_ATTEMPT"}
        values = rows()[:1] if suite.launches.index(attempt_id) % 2 == 0 else rows()[2:3]
        if behavior in ("failed", "no_output"):
            values = []
        dataset = None if behavior == "no_output" else "dataset-" + attempt_id
        state = journal.state()[1][attempt_id]
        if state["submission"] == "uncertain":
            journal.append("receipt-" + attempt_id, "receipt", {"attempt": attempt_id, "run_id": "run-" + attempt_id, "dataset_id": dataset})
        if state["controls_ok"] is None:
            journal.append("controls-" + attempt_id, "controls", {"attempt": attempt_id, "ok": True, "evidence": "fixture-controls"})
        if state["remote_status"] is None:
            journal.append("terminal-" + attempt_id, "terminal", {"attempt": attempt_id, "status": "FAILED" if behavior in ("failed", "no_output") else "SUCCEEDED"})
            journal.append("charge-" + attempt_id, "charge", {"attempt": attempt_id, "usd": "0.001", "evidence": "fixture-cost"})
        if behavior == "no_output":
            journal.append("no-output-" + attempt_id, "no_output", {"attempt": attempt_id, "evidence": "fixture-no-output"})
            return {"verdict": "Partial", "code": "SOURCE_NO_OUTPUT"}
        folder = journal.root / attempt_id
        atomic_json(folder / "prepared.json", prepared.to_dict())
        atomic_json(folder / "captured_rows.json", values)
        mapped = map_prepared_rows(values, prepared)
        atomic_json(folder / "mapped_rows.json", mapped)
        atomic_json(folder / "capture_manifest.json", {"captured_hash": digest(values), "dataset_id": dataset,
                    "observed_at": NOW, "bounded_capture_complete": True, "truncated": False})
        journal.append("capture-" + attempt_id, "capture", {"attempt": attempt_id, "hash": digest(values)})
        journal.append("mapping-" + attempt_id, "mapping", {"attempt": attempt_id, "status": mapped["status"]})
        return {"verdict": "Partial", "code": "CAPTURED_REVIEW_PENDING"}


class UnlockedModel(FakeRunner):
    def __init__(self, suite):
        super().__init__()
        self.suite = suite
    def __call__(self, *args):
        self.suite.assertFalse(self.suite.memory.journal._locked)
        response, usage = super().__call__(*args)
        role, payload, _ = args
        if role == "E" and payload.get("fit_policy_id") == FIT_POLICY_ID:
            for result in response["results"]:
                result["criteria"] = [row for row in result["criteria"] if row["id"] in ACTIVE_CRITERION_IDS]
                result["audience_positioning"] = {"positioning": "unisex",
                    "reason": "Fictional general household use; no demographic claim."}
        return response, usage


class ResearchBatchTests(unittest.TestCase):
    def setUp(self):
        # Capture freshness (seven days) is judged at execution time and the
        # fixture captures are observed at NOW, so execution runs at NOW, not
        # at today's date.
        clock = patch("product_radar.research_batch.utc_now", return_value=NOW)
        clock.start(); self.addCleanup(clock.stop)
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        root = Path(self.temp.name)
        self.memory = ResearchMemory(root / "research", Journal(root / "finance"))
        self.account = binding()
        with self.memory.journal.locked():
            self.memory.journal.setup_budget(self.account.namespace, "5")
        self.plan = plan_research(self.memory, catalogue(), as_of=NOW, slots=3)
        self.prepared, self.launches, self.runs, self.preflights, self.behaviors = {}, [], [], [], {}
        self.runner = UnlockedModel(self)

    def prepare(self, plan, action):
        existing = action.get("existing_task")
        if existing:
            return self.prepared[existing["attempt_id"]]
        config = deepcopy(CONFIG)
        config.update(term=action["request"]["term"], purpose=action["request"]["purpose"])
        attempt_id = "attempt-" + action["action_id"][-8:]
        prepared = prepared_request(cli.prepare(config, attempt_id)["request"])
        self.prepared[attempt_id] = prepared
        return prepared

    def execute(self, **changes):
        return execute_plan(self.memory, self.plan["id"], prepare_action=self.prepare,
                            controller_factory=lambda: FixtureController(self), runner=self.runner,
                            action_limit=changes.pop("action_limit", 2), **changes)

    def test_connected_two_capture_then_replay_uses_existing_work(self):
        first = self.execute()
        self.assertEqual(len(self.launches), 2)
        self.assertEqual(len(first["acquisition_ids"]), 2)
        self.assertEqual(len(first["summary"]["products"]), 2)
        self.assertEqual(len(first["feedback_ids"]), 2)
        self.assertTrue(all(a["status"] == "captured" for a in first["actions"]))
        calls = len(self.runner.calls)
        second = self.execute()
        self.assertTrue(second["replay"])
        self.assertEqual(len(self.launches), 2)
        self.assertEqual(len(self.runner.calls), calls)
        self.assertFalse(second["client_ready"])
        self.assertTrue(all(outcome["cost_usd"] is None for outcome in self.memory.snapshot()["query_outcomes"].values()))

    def test_production_batch_declares_current_fit_and_preserves_unknowns(self):
        first = self.execute()
        self.assertEqual(len(first["summary"]["products"]), 2)
        assessments = self.memory.snapshot()["assessments"]
        self.assertEqual(len(assessments), 2)
        for assessment in assessments.values():
            self.assertEqual(assessment["fit_policy_id"], FIT_POLICY_ID)
            self.assertEqual([row["id"] for row in assessment["result"]["criteria"]], list(ACTIVE_CRITERION_IDS))
            self.assertEqual(assessment["result"]["audience_positioning"]["positioning"], "unisex")
        calls = len(self.runner.calls)
        self.assertTrue(self.execute()["replay"])
        self.assertEqual(len(self.runner.calls), calls)

    def test_exact_query_across_two_families_in_one_batch_launches_once(self):
        original = deepcopy(self.plan)
        revised = deepcopy(self.plan)
        revised["actions"][1]["request"] = deepcopy(revised["actions"][0]["request"])
        revised["actions"][1]["phrase"] = revised["actions"][0]["phrase"]
        revised["actions"][1]["semantic_key"] = semantic_key(revised["actions"][1]["request"])
        self.memory.put("batch_plans", revised["id"], revised, previous=original)
        self.plan = revised
        first = self.execute(action_limit=2)
        self.assertEqual(len(self.launches), 1)
        self.assertEqual([entry["status"] for entry in first["actions"]], ["captured", "reused"])
        self.assertEqual(len(first["acquisition_ids"]), 1)
        self.assertEqual(first["actions"][0]["task_id"], first["actions"][1]["task_id"])
        self.assertNotEqual(first["actions"][0]["family_id"], first["actions"][1]["family_id"])
        self.assertEqual(len(self.memory.snapshot()["query_tasks"]), 1)
        self.assertEqual(len(self.memory.snapshot()["query_outcomes"]), 1)
        self.assertEqual(next(iter(self.memory.snapshot()["query_outcomes"].values()))["family_id"],
                         first["actions"][0]["family_id"])
        self.assertTrue(self.execute(action_limit=2)["replay"])
        self.assertEqual(len(self.launches), 1)

    def test_failed_source_continues_independent_safe_action(self):
        first_action = self.plan["actions"][0]
        prepared = self.prepare(self.plan, first_action)
        self.behaviors[prepared.attempt_id] = "failed"
        result = self.execute()
        self.assertEqual(len(self.launches), 2)
        self.assertEqual(len(result["summary"]["products"]), 1)
        outcomes = self.memory.snapshot()["query_outcomes"].values()
        self.assertEqual(sum(outcome["status"] == "failed" for outcome in outcomes), 1)

    def test_unknown_submission_stops_later_launches_not_saved_analysis(self):
        second = self.plan["actions"][1]
        prepared = self.prepare(self.plan, second)
        self.behaviors[prepared.attempt_id] = "unknown"
        result = self.execute(action_limit=3)
        self.assertEqual(len(self.launches), 2)
        self.assertTrue(result["paid_dispatch_stopped"])
        self.assertEqual(len(result["summary"]["products"]), 1)
        self.assertEqual(result["actions"][2]["status"], "deferred")
        replay = self.execute(action_limit=3)
        self.assertEqual(len(self.launches), 2)
        self.assertIn((prepared.attempt_id, True), self.runs)
        self.assertTrue(replay["paid_dispatch_stopped"])
        # Repeated recovery visits the same state transitions, but each saved
        # revision must still agree with the returned final status.
        third = self.execute(action_limit=3)
        stored = self.memory.snapshot()["batch_runs"][third["id"]]
        self.assertEqual(list(stored["actions"].values()), third["actions"])
        self.assertEqual(stored["actions"][second["action_id"]]["status"], "blocked")

    def test_terminal_failure_without_output_is_not_reposted(self):
        prepared = self.prepare(self.plan, self.plan["actions"][0])
        self.behaviors[prepared.attempt_id] = "no_output"
        first = self.execute(action_limit=1)
        self.assertEqual(first["actions"][0]["status"], "blocked")
        self.execute(action_limit=1)
        self.assertEqual(len(self.launches), 1)
        self.assertEqual(len(self.runs), 1)

    def test_wrong_preparation_scope_fails_before_controller_calls(self):
        def wrong(plan, action):
            config = deepcopy(CONFIG)
            config["term"] = "An unrelated request"
            return prepared_request(cli.prepare(config, "wrong-preparation")["request"])
        result = execute_plan(self.memory, self.plan["id"], prepare_action=wrong,
                              controller_factory=lambda: FixtureController(self), runner=self.runner, action_limit=1)
        self.assertEqual(result["actions"][0]["status"], "blocked")
        self.assertEqual(self.launches, [])
        self.assertEqual(self.preflights, [])
        self.assertEqual(self.memory.snapshot()["query_tasks"], {})

    def test_recreated_plan_reuses_equivalent_existing_capture(self):
        self.execute()
        old_plan = self.plan
        changed = deepcopy(old_plan)
        changed["id"] = "plan-recreated"
        changed["note"] = "Different presentation; identical search tasks."
        self.memory.put("batch_plans", changed["id"], changed)
        self.plan = changed
        result = self.execute()
        self.assertEqual(len(self.launches), 2)
        self.assertTrue(all(a["status"] == "reused" for a in result["actions"]))

    def test_recreated_plan_recovers_active_attempt_without_reposting(self):
        prepared = self.prepare(self.plan, self.plan["actions"][0])
        self.behaviors[prepared.attempt_id] = "unknown"
        first = self.execute(action_limit=1)
        replacement = deepcopy(self.plan)
        replacement["id"] = "plan-uncertain-recreated"
        self.memory.put("batch_plans", replacement["id"], replacement)
        self.plan = replacement
        second = self.execute(action_limit=1)
        self.assertEqual(len(self.launches), 1)
        self.assertEqual(second["actions"][0]["task_id"], first["actions"][0]["task_id"])
        self.assertEqual(self.runs[-1], (prepared.attempt_id, True))
        self.assertEqual(len(self.memory.snapshot()["query_tasks"]), 1)

    def test_committed_capture_import_failure_recovers_without_provider_call(self):
        with patch("product_radar.research_batch.import_saved_attempt", side_effect=SafetyError("FIXTURE_LOCAL_CAPTURE_READ")):
            first = self.execute(action_limit=1)
        self.assertEqual(first["actions"][0]["status"], "blocked")
        self.assertEqual(len(self.launches), 1)
        calls = len(self.runs)
        recovered = self.execute(action_limit=1)
        self.assertEqual(recovered["actions"][0]["status"], "captured")
        self.assertEqual(len(recovered["summary"]["products"]), 1)
        self.assertEqual(len(self.runs), calls)
        self.assertEqual(len(self.launches), 1)

    def test_partial_replay_does_not_expand_paid_action_limit(self):
        self.execute(action_limit=1)
        second = self.execute(action_limit=3)
        self.assertEqual(len(self.launches), 1)
        self.assertEqual(len(second["actions"]), 1)
        self.assertEqual(second["unselected_actions"], 2)

    def test_capture_freshness_uses_execution_time_not_frozen_plan_time(self):
        # The query choices were made before collection began. Newly captured
        # observations must not be hidden as if they were future evidence.
        changed = deepcopy(self.plan)
        changed["id"] = "plan-earlier"
        changed["as_of"] = "2026-10-05T10:00:00+00:00"
        self.memory.put("batch_plans", changed["id"], changed)
        self.plan = changed
        with patch("product_radar.research_batch.utc_now", return_value=NOW):
            first = self.execute(action_limit=1)
            self.assertEqual(first["as_of"], NOW)
            product = first["summary"]["products"][0]
            self.assertGreater(product["market"]["families"]["meta"]["active_ads"], 0)
            replacement = deepcopy(changed)
            replacement["id"] = "plan-earlier-recreated"
            self.memory.put("batch_plans", replacement["id"], replacement)
            self.plan = replacement
            second = self.execute(action_limit=1)
            self.assertEqual(second["actions"][0]["status"], "reused")
            self.assertEqual(len(self.launches), 1)

    def test_model_failure_does_not_repeat_collector_work(self):
        class Unavailable:
            identity = "model-unavailable-fixture"
            def __call__(self, *args):
                from product_radar.models import SafetyError
                raise SafetyError("AI_SUBSCRIPTION_LIMIT")
        first = execute_plan(
            self.memory, self.plan["id"], prepare_action=self.prepare, controller_factory=lambda: FixtureController(self),
            runner=Unavailable(), action_limit=1)
        self.assertEqual(len(self.launches), 1)
        self.assertGreater(first["summary"]["unresolved_issues"], 0)
        execute_plan(self.memory, self.plan["id"], prepare_action=self.prepare, controller_factory=lambda: FixtureController(self),
                     runner=Unavailable(), action_limit=1)
        self.assertEqual(len(self.launches), 1)


if __name__ == "__main__":
    unittest.main()
