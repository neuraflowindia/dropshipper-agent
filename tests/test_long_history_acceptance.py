"""Eighty synthetic issues keep lifetime history and bounded local retrieval."""

from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from product_radar.discovery import find_possible_matches
from product_radar.issue_allocation import allocate_issues, draft_ordinal
from product_radar.operational_backup import create_operational_backup, restore_operational_backup
from product_radar.research_memory import ResearchMemory, change
from product_radar.service import policy_for_fit
from product_radar.storage import Journal, load_json
from test_issue_allocation import products
import product_radar.research_memory as memory_module


class LongHistoryAcceptanceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.memory = ResearchMemory(self.root / "research", Journal(self.root / "finance"), evidence_type="synthetic")
        self.history, self.descriptions = {}, {}
        for report in range(80):
            changes = []
            for offset in range(10):
                number = report * 10 + offset; concept_id = f"concept{number:04d}"
                description = {"label": f"Fictional draining rack {number}", "function": "Drain and store kitchen utensils",
                               "mechanism": "Manual draining tray", "audience": "adult", "features": ["draining"],
                               "basis": "observed", "category": "Home and Kitchen", "brand": "generic_or_small"}
                concept = {"description": description, "status": "provisional"}
                row = {"concept_id": concept_id, "status": "published", "issue_id": f"past-issue-{report:03d}",
                       "client_id": "default", "ordinal": report + 1}
                self.history[concept_id] = row; self.descriptions[concept_id] = concept
                changes.extend([change("concepts", concept_id, concept), change("published_history", concept_id, row)])
            self.memory.commit(f"past-report-{report:03d}", changes)
        self.hold = {"issue_id": "future-reserved", "state": "reserved", "client_id": "default", "date": "2026-10-13",
                     "products": products(10, offset=800)}
        self.memory.put("issue_reservations", "future-reserved", self.hold)

    def test_cold_restart_replays_once_warm_checks_need_no_historical_json_reads(self):
        reader = ResearchMemory(self.memory.root, self.memory.journal, evidence_type="synthetic")
        with patch.object(memory_module, "load_event", wraps=memory_module.load_event) as reads:
            first = reader.snapshot()
            event_reads = [call for call in reads.call_args_list if Path(call.args[0]).parent.name == "events"]
            self.assertEqual(len(event_reads), 81)
            self.assertEqual(len(first["published_history"]), 800)
            reads.reset_mock()
            second = reader.snapshot()
            self.assertFalse(any(Path(call.args[0]).parent.name == "events" for call in reads.call_args_list))
            self.assertEqual(first, second)

    def test_oldest_and_newest_published_products_and_queued_hold_cannot_be_new_again(self):
        state = self.memory.snapshot()
        result = allocate_issues(products(820), ["2026-10-15"], state["published_history"], state["issue_reservations"],
                                 policy_for_fit("fit-13-v1"))
        selected = {row["concept_id"] for row in result["issues"][0]["products"]}
        self.assertEqual(selected, {f"concept{number:04d}" for number in range(810, 820)})
        self.assertEqual(result["issues"][0]["counts"], {"products": 10, "new": 10, "updates": 0})
        self.assertEqual(draft_ordinal("next-issue", "2026-10-15", state["published_history"], state["issue_reservations"]), 82)

    def test_ai_shortlist_stays_bounded_with_eight_hundred_known_products(self):
        description = dict(self.descriptions["concept0799"]["description"], label="Blue draining utensil rack")
        result = find_possible_matches(description, self.descriptions)
        self.assertEqual(len(result["candidates"]), 10)
        self.assertEqual(result["omitted"], 790)
        self.assertTrue(all(row["id"] in self.descriptions for row in result["candidates"]))

    def test_restore_and_same_event_replay_preserve_all_eighty_issues_and_hold(self):
        state = self.memory.snapshot()
        backup = create_operational_backup(self.memory, self.root / "backups", backup_id="eighty-issues")
        receipt = restore_operational_backup(backup["folder"], self.root / "restored")
        restored = ResearchMemory(self.root / "restored/research", Journal(self.root / "restored/finance"), evidence_type="synthetic")
        self.assertEqual(restored.snapshot(), state)
        self.assertEqual(receipt["table_counts"]["published_history"], 800)
        self.assertEqual(receipt["table_counts"]["issue_reservations"], 1)
        before = len(list((restored.root / "events").glob("*.json")))
        original = load_json(self.memory.root / "events/00000001.json")
        self.assertFalse(restored.commit(original["id"], original["changes"]))
        self.assertEqual(len(list((restored.root / "events").glob("*.json"))), before)


if __name__ == "__main__":
    unittest.main()
