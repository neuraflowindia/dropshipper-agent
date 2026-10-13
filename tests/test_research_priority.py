"""Fixture-only priority connection; no operational plan or provider calls."""

from pathlib import Path
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from product_radar.catalogue import load_catalogue
from product_radar.models import SafetyError
from product_radar.research_memory import ResearchMemory
from product_radar.research_operations import plan_research
from product_radar.storage import Journal, load_json
from test_research_planner import catalogue, outcome, NOW


class PriorityPlanTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        root = Path(self.temp.name)
        self.memory = ResearchMemory(root / "research", Journal(root / "finance"), evidence_type="synthetic")

    def test_known_three_families_save_exact_priority_choices_and_repeat_reuses_plan(self):
        data = load_catalogue()
        priorities = ("bb-baby-food-portion-trays", "hk-crevice-cleaning-tools", "to-dry-storage-bags")
        plan = plan_research(self.memory, data, as_of=NOW, slots=3, budget_usd="0.21",
                             urgent_family_ids=priorities)
        self.assertEqual([row["family_id"] for row in plan["actions"]], list(priorities))
        self.assertEqual([row["phrase"] for row in plan["actions"]],
                         ["covered baby food freezer tray", "window groove cleaning brush", "roll-top dry bag"])
        self.assertTrue(all(row["kind"] == "collect" and row["request"]["coverage"] == {"limit": 5}
                            for row in plan["actions"]))
        self.assertEqual(plan["budget"]["discovery_estimate_usd"], "0.15")
        self.assertEqual(plan["budget"]["reserved_for_validation_and_suppliers_usd"], "0.06")
        self.assertFalse(plan["collector_dispatched"])
        self.assertEqual(load_json(self.memory.root / "next_searches.json", 2 * 1024 * 1024), plan)
        self.assertEqual(self.memory.snapshot()["batch_plans"][plan["id"]], plan)
        before = list((self.memory.root / "events").glob("*.json"))
        replay = plan_research(self.memory, data, as_of=NOW, slots=3, budget_usd="0.21",
                               urgent_family_ids=priorities)
        self.assertEqual(replay, plan)
        self.assertEqual(list((self.memory.root / "events").glob("*.json")), before)
        self.assertFalse(self.memory.journal.path.exists())

    def test_priority_recent_exact_capture_still_reuses_original_dates_without_charge(self):
        data = catalogue()
        first = plan_research(self.memory, data, as_of=NOW, slots=12)
        action = next(row for row in first["actions"] if row["family_id"] == "f2-2")
        saved = outcome(action)
        self.memory.put("query_outcomes", saved["id"], saved)
        plan = plan_research(self.memory, data, as_of=NOW, slots=1, budget_usd="0.06",
                             urgent_family_ids=[action["family_id"]])
        self.assertEqual(len(plan["actions"]), 1)
        chosen = plan["actions"][0]
        self.assertEqual(chosen["family_id"], action["family_id"])
        self.assertEqual(chosen["kind"], "reuse")
        self.assertEqual(chosen["capture_id"], saved["id"])
        self.assertEqual(chosen["captured_observed_at"], saved["observed_at"])
        self.assertEqual(chosen["cost_estimate_usd"], "0")

    def test_three_fresh_priorities_reach_collection_instead_of_unrelated_cached_slots(self):
        data = catalogue()
        original = plan_research(self.memory, data, as_of=NOW, slots=3)
        for index, action in enumerate(original["actions"]):
            saved = outcome(action, n=index + 1)
            self.memory.put("query_outcomes", saved["id"], saved)
        ordinary = plan_research(self.memory, data, as_of=NOW, slots=3, budget_usd="0.21")
        self.assertTrue(all(row["kind"] == "collect" for row in ordinary["actions"]))
        original_ids = {row["family_id"] for row in original["actions"]}
        self.assertTrue(all(row["family_id"] not in original_ids for row in ordinary["actions"]))
        priorities = ("f0-2", "f1-2", "f3-2")
        targeted = plan_research(self.memory, data, as_of=NOW, slots=3, budget_usd="0.21",
                                 urgent_family_ids=priorities)
        self.assertEqual({row["family_id"] for row in targeted["actions"]}, set(priorities))
        self.assertTrue(all(row["kind"] == "collect" for row in targeted["actions"]))
        self.assertEqual(targeted["budget"]["discovery_estimate_usd"], "0.15")

    def test_priorities_cannot_spend_reserved_validation_money(self):
        priorities = ("f0-2", "f1-2", "f2-2")
        plan = plan_research(self.memory, catalogue(), as_of=NOW, slots=3, budget_usd="0.11",
                             urgent_family_ids=priorities)
        self.assertEqual(len(plan["actions"]), 1)
        self.assertIn(plan["actions"][0]["family_id"], priorities)
        self.assertEqual(plan["budget"]["discovery_estimate_usd"], "0.05")

    def test_priority_pending_acquisition_is_not_replaced(self):
        data = catalogue()
        first = plan_research(self.memory, data, as_of=NOW, slots=12)
        action = next(row for row in first["actions"] if row["family_id"] == "f2-2")
        saved = outcome(action, status="pending", complete=False, reviewed=False)
        self.memory.put("query_outcomes", saved["id"], saved)
        plan = plan_research(self.memory, data, as_of=NOW, slots=1,
                             urgent_family_ids=(action["family_id"],))
        self.assertNotIn(action["family_id"], [row["family_id"] for row in plan["actions"]])
        decision = next(row for row in plan["decisions"] if row["family_id"] == action["family_id"])
        self.assertIn("pending", decision["reason"])

    def test_invalid_or_unknown_priority_is_refused_before_saving(self):
        data = catalogue()
        before = self.memory.snapshot()
        for priorities, code in (("f0-2", "PLANNER_PRIORITY_FAMILIES_INVALID"),
                                 (None, "PLANNER_PRIORITY_FAMILIES_INVALID"),
                                 (("unknown",), "PLANNER_PRIORITY_FAMILY_UNKNOWN"),
                                 (("bad/id",), "INVALID_IDENTIFIER"),
                                 ((False,), "INVALID_IDENTIFIER")):
            with self.subTest(priorities=priorities):
                with self.assertRaisesRegex(SafetyError, code):
                    plan_research(self.memory, data, as_of=NOW, urgent_family_ids=priorities)
                self.assertEqual(self.memory.snapshot(), before)
        self.assertFalse((self.memory.root / "next_searches.json").exists())
        self.assertFalse(self.memory.journal.path.exists())


if __name__ == "__main__":
    unittest.main()
