"""Bounded entry point joins the real dispatcher without activating a task."""

from copy import deepcopy
from pathlib import Path
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from product_radar.models import SafetyError
from product_radar.programme import (_photos, advance_programme_turn, load_programme,
                                    set_programme_service_state)
from product_radar.research_ai import Jobs
from product_radar.storage import Journal
from product_radar.work_turns import DEFAULT_POLICY, active_work_turn, bind_work_turn, open_work_turn, reserve_stage_items
import test_programme as programme_fixtures
from test_research_flow import FakeRunner


class ProgrammeTurnTests(unittest.TestCase):
    def setUp(self):
        self.fixture = programme_fixtures.ProgrammeTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.memory = self.fixture.memory
        self.fixture.prepare()

    def advance(self, **options):
        return advance_programme_turn(self.memory, "whole-programme", root=self.fixture.root,
            handlers={}, as_of=self.fixture.now, **options)

    def test_actual_jobs_and_stage_waits_resume_in_next_turn_not_fake_outage(self):
        runner = FakeRunner()
        def model_item(item_id):
            def step(memory, _record, _context):
                job_id, result = Jobs(memory, runner).run("A", {"items": [{"id": item_id,
                    "source_text": {"title": "Blue sink caddy"}}]})
                return {"job_id": job_id, "accepted": result["accepted"]}
            return step
        steps = self.fixture.noops()
        steps.update(profiles=model_item("first-question"), fit=model_item("second-question"))
        policy = deepcopy(DEFAULT_POLICY)
        policy.update(model_executions=1, report_slots=0)
        first = self.advance(steps=steps, turn_policy=policy)
        record = load_programme(self.memory, "whole-programme")
        self.assertEqual(len(runner.calls), 1)
        self.assertEqual(first["work_turn"]["counters"]["model_executions"], 1)
        self.assertEqual(record["last_results"]["fit"]["reason"], "WORK_TURN_MODEL_LIMIT")
        self.assertNotIn("lane:fit", record["deferrals"])
        self.assertIsNone(record["model_pause"])
        self.assertEqual(first["next_action"], "continue_ready_work")
        second = self.advance(steps=steps, turn_policy=policy)
        self.assertEqual(len(runner.calls), 2)
        self.assertEqual(second["work_turn"]["turn_sequence"], first["work_turn"]["turn_sequence"] + 1)
        self.assertEqual(second["work_turn"]["counters"]["model_executions"], 1)
        third = self.advance(steps=steps, turn_policy=policy)
        self.assertEqual(len(runner.calls), 2)
        self.assertEqual(third["work_turn"]["counters"]["model_executions"], 0)
        self.assertEqual(third["next_action"], "wait_for_saved_trigger")

    def test_interrupted_before_dispatch_keeps_same_open_turn(self):
        with patch("product_radar.programme._advance_programme_locked", side_effect=SafetyError("RUNNER_ALREADY_ACTIVE")):
            with self.assertRaisesRegex(SafetyError, "RUNNER_ALREADY_ACTIVE"):
                self.advance(steps=self.fixture.noops())
        original = next(row for row in self.memory.snapshot()["batch_runs"].values() if row.get("kind") == "work-turn-v1")
        self.assertEqual(original["state"], "open")
        result = self.advance(steps=self.fixture.noops())
        self.assertEqual(result["work_turn"]["id"], original["id"])
        self.assertEqual(result["work_turn"]["state"], "closed")

    def test_busy_writer_cannot_prepare_or_close_another_workers_turn(self):
        with Journal(self.memory.root / "programme_writer").locked():
            with self.assertRaisesRegex(SafetyError, "RUNNER_ALREADY_ACTIVE"):
                self.advance(steps=self.fixture.noops())
        self.assertFalse(any(row.get("kind") == "work-turn-v1"
                             for row in self.memory.snapshot()["batch_runs"].values()))

    def test_turn_lifecycle_retains_the_same_single_writer_through_close(self):
        from product_radar.work_turns import close_work_turn
        ownership_checked = []
        def competing_writer(*args, **kwargs):
            with self.assertRaisesRegex(SafetyError, "RUNNER_ALREADY_ACTIVE"):
                with Journal(self.memory.root / "programme_writer").locked():
                    self.fail("A competing writer acquired the turn closure window")
            ownership_checked.append(True)
            return close_work_turn(*args, **kwargs)
        with patch("product_radar.work_turns.close_work_turn", side_effect=competing_writer):
            result = self.advance(steps=self.fixture.noops())
        self.assertEqual(ownership_checked, [True])
        self.assertEqual(result["work_turn"]["state"], "closed")

    def test_owner_pause_prevents_new_turn(self):
        set_programme_service_state(self.memory, enabled=False, as_of=self.fixture.now)
        with self.assertRaisesRegex(SafetyError, "PROGRAMME_OWNER_PAUSED"):
            self.advance(steps=self.fixture.noops())
        self.assertFalse(any(row.get("kind") == "work-turn-v1" for row in self.memory.snapshot()["batch_runs"].values()))

    def test_photo_item_bound_is_shared_by_actual_original_and_follow_on_stages(self):
        policy = deepcopy(DEFAULT_POLICY)
        policy["purpose_items"]["photos"] = 1
        turn = open_work_turn(self.memory, "whole-programme", at=self.fixture.now, policy=policy)
        original = load_programme(self.memory, "whole-programme")
        follow_on = deepcopy(original)
        original["concept_ids"] = ["first"]
        follow_on["concept_ids"] = ["second"]
        context = {"at": self.fixture.now, "photo_limit": 10, "retry_seconds": 300,
                   "media_root": None, "photo_fetcher": None}
        pool = {"facts_by_concept": {cid: {"image_candidates": [{"url": "https://example.com/image.jpg",
                 "relationship": "exact_product"}]} for cid in ("first", "second")}}
        fetched = []
        def physical_leaf(memory, concept_id, _candidate, **options):
            self.assertIs(options.get("shared_acquisitions"), True)
            reserve_stage_items(memory, "photos", "synthetic-physical-" + concept_id, 1, at=self.fixture.now)
            fetched.append(concept_id)
            return {"state": "usable", "reason": "Synthetic image", "reference": "synthetic-image"}
        with bind_work_turn(self.memory, turn["id"]), \
                patch("product_radar.report_selection.build_report_pool", return_value=pool), \
                patch("product_radar.product_images.current_public_image", return_value=None), \
                patch("product_radar.product_images.product_image_readiness",
                      return_value={"ready": True, "local_saved": False, "next_retry_at": None}), \
                patch("product_radar.product_images.capture_product_image", side_effect=physical_leaf):
            self.assertEqual(len(_photos(self.memory, original, context)["outcomes"]), 1)
            waiting = _photos(self.memory, follow_on, context)["outcomes"]
            self.assertEqual(waiting[0]["state"], "waiting")
            self.assertEqual(waiting[0]["reason"], "WORK_TURN_PURPOSE_LIMIT")
            self.assertEqual(active_work_turn(self.memory)["counters"]["purpose_items"]["photos"], 1)
            self.assertEqual(fetched, ["first"])


if __name__ == "__main__":
    unittest.main()
