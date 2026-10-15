"""Actual accepted functional records reach photo and pre-turn intake readers."""

from pathlib import Path
import tempfile
import unittest

import product_radar.programme as programme
from product_radar.inventory_intake import reporting_intake_concept_ids
from product_radar.issue_allocation import allocate_issues
from product_radar.product_images import inspect_image
from product_radar.report_examples import RECOMMENDATION_CONTRACT
from product_radar.report_selection import build_report_pool
from product_radar.report_workflow import freeze_review_input
from product_radar.service import policy_for_fit
from product_radar.work_turns import active_work_turn, bind_work_turn, close_work_turn, open_work_turn
from test_functional_source_market import functional_pool_fixture, NOW
from test_photo_operation_progress import report_image_bytes


class ProgrammeFunctionalPoolCallerTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(); self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.memory, (self.cid,) = functional_pool_fixture(self.root / "inputs", with_images=True)
        self.record = programme.prepare_programme(self.memory, "functional-pool-callers",
            concept_ids=[self.cid], as_of=NOW)

    def test_pre_turn_photo_readiness_sees_actual_accepted_example_without_pool_mock(self):
        self.assertIsNone(active_work_turn(self.memory)); before = self.memory.snapshot()
        ready = programme._photo_turn_ready(self.memory, before, self.record, NOW,
            media_root=self.root / "media", detailed=True)
        self.assertTrue(ready["ready"], ready)
        self.assertEqual(len(ready["signal"]), 1)
        self.assertEqual(self.memory.snapshot(), before)

    def test_photo_stage_fetches_actual_accepted_example_once_without_pool_mock(self):
        calls = []
        def fetch(url):
            # The stage credits only a report-sized photograph (short edge >= 128).
            calls.append(url); raw = report_image_bytes()
            return {"state": "usable", "reason": "Synthetic original photo.", "checked_at": NOW,
                "requested_url": url, "final_url": url, "http_status": 200, "redirects": [],
                "body_complete": True, "dispatch_started": True, "metadata": inspect_image(raw),
                "response_received": True, "owned_worker_exited": True, "execution_stage": "response_received"}, raw
        context = {"at": NOW, "fixed_as_of": NOW, "photo_limit": 3, "retry_seconds": 300,
            "media_root": self.root / "media", "photo_fetcher": fetch}
        turn = open_work_turn(self.memory, self.record["programme_id"], at=NOW)
        try:
            with bind_work_turn(self.memory, turn["id"]):
                result = programme._photos(self.memory, self.record, context)
                again = programme._photos(self.memory, self.record, context)
        finally:
            close_work_turn(self.memory, turn["id"], at=NOW, reason="Synthetic caller review completed")
        self.assertEqual([row["state"] for row in result["outcomes"]], ["usable"], result)
        self.assertEqual(again["outcomes"], [])
        self.assertEqual(len(calls), 1)

    def test_cooperative_inventory_boundary_admits_actual_type_before_turn_is_open(self):
        self.assertIsNone(active_work_turn(self.memory))
        result = programme._refresh_inventory_boundary(self.memory, self.record, as_of=NOW)
        self.assertNotIn("inventory_preparation_wait", result)
        self.assertIn(self.cid, reporting_intake_concept_ids(self.memory.snapshot(), result))
        self.assertFalse(any(row.get("kind") == "inventory-work-v1"
            and row.get("binding", {}).get("purpose") == "report_contract"
            for row in self.memory.snapshot()["batch_runs"].values()))

    def test_pre_turn_frozen_writing_readiness_accepts_real_new_packet_selection(self):
        memory, ids = functional_pool_fixture(self.root / "ten-inputs", count=10)
        record = programme.prepare_programme(memory, "functional-writing-readiness", concept_ids=ids, as_of=NOW)
        pool = build_report_pool(memory, as_of=NOW, recommendation_contract=RECOMMENDATION_CONTRACT)
        state = memory.snapshot()
        issue = allocate_issues(pool["eligible"], record["release_slots"], state["published_history"],
            state["issue_reservations"], policy=policy_for_fit("fit-10-v1"))["issues"][0]
        self.assertTrue(issue["ready_for_review"])
        facts = [pool["facts_by_concept"][row["concept_id"]] for row in issue["products"]]
        issue, facts = programme._functional_report_revision(self.root, issue, facts, "synthetic")
        freeze_review_input(memory, issue, facts, root=self.root)
        self.assertIsNone(active_work_turn(memory)); before = memory.snapshot()
        self.assertTrue(programme._frozen_writing_ready(memory, before, record, root=self.root, at=NOW))
        self.assertEqual(memory.snapshot(), before)


if __name__ == "__main__":
    unittest.main()
