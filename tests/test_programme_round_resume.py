"""A restarted round goes on from its first unfinished stage (synthetic, offline)."""

from contextlib import nullcontext
from pathlib import Path
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import product_radar.programme as programme
from product_radar.programme import STAGES, load_programme
import test_programme as programme_fixtures


class Crash(BaseException):
    """Stands in for the process dying (not an ordinary stage failure)."""


class RoundResumeTests(unittest.TestCase):
    def setUp(self):
        self.fixture = programme_fixtures.ProgrammeTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.memory = self.fixture.memory
        self.fixture.prepare()
        self.calls = []

    def steps(self, crash_at=None):
        def step(_memory, _record, _context, stage):
            self.calls.append(stage)
            if stage == crash_at:
                raise Crash()
            return {"state": "checked"}
        return {stage: (lambda memory, record, context, name=stage: step(memory, record, context, name))
                for stage in STAGES}

    def discovery(self, _memory, _record, _context):
        self.calls.append("discovery")
        return {"state": "checked", "new_products_need_full_source_review": []}

    def advance(self, crash_at=None):
        return programme.advance_programme_turn(self.memory, "whole-programme", root=self.fixture.root,
            handlers={}, as_of=self.fixture.now, steps=self.steps(crash_at), discovery_step=self.discovery)

    def turns(self):
        return sorted((row for row in self.memory.snapshot()["batch_runs"].values() if row.get("kind") == "work-turn-v1"),
                      key=lambda row: row["turn_sequence"])

    def test_restart_skips_finished_stages_and_reruns_the_one_that_crashed(self):
        with self.assertRaises(Crash):
            self.advance(crash_at="match")
        first = self.calls[:]; self.calls.clear()
        crashed = first.index("match")
        self.assertEqual([turn["state"] for turn in self.turns()], ["open"])
        saved = load_programme(self.memory, "whole-programme")["round_progress"]
        self.assertEqual(saved["finished_stages"], [stage for stage in first[:crashed] if stage != "discovery"])
        self.assertEqual(saved["turn_id"], self.turns()[0]["id"])
        result = self.advance()
        # Only the crashed stage and the ones after it run; discovery ran once.
        self.assertEqual([stage for stage in self.calls if stage != "discovery"],
                         [stage for stage in result["execution_order"]["stage_order"]
                          if stage not in saved["finished_stages"]])
        self.assertEqual((first + self.calls).count("discovery"), 1)
        self.assertEqual(result["work_turn"]["id"], saved["turn_id"])
        self.assertEqual(result["work_turn"]["state"], "closed")
        # Every stage is still recorded as visited by the round.
        self.assertEqual(result["execution_order"]["visited_stages"], result["execution_order"]["stage_order"])
        self.assertTrue(result["execution_order"]["discovery_attempted"])

    def test_a_stage_crashing_inside_a_later_review_group_runs_again_whole(self):
        groups = []
        def discovery(_memory, _record, _context):
            return {"state": "checked", "new_products_need_full_source_review": ["later"]}
        self.fixture.product("later")
        programme.advance_programme_turn(self.memory, "whole-programme", root=self.fixture.root,
            handlers={}, as_of=self.fixture.now, steps=self.steps(), discovery_step=discovery)
        def fit(_memory, record, _context):
            groups.append(record["programme_id"])
            if len(groups) == 2:
                raise Crash()
            return {"state": "checked"}
        steps = self.steps(); steps["fit"] = fit
        options = {"root": self.fixture.root, "handlers": {}, "as_of": self.fixture.now, "steps": steps}
        with self.assertRaises(Crash):
            programme.advance_programme_turn(self.memory, "whole-programme", **options)
        self.assertNotIn("fit", load_programme(self.memory, "whole-programme")["round_progress"]["finished_stages"])
        self.calls.clear()
        programme.advance_programme_turn(self.memory, "whole-programme", **options)
        self.assertEqual(len(groups), 4)  # both groups again, not only the one that crashed
        self.assertNotIn("profiles", self.calls)

    def test_discovery_that_crashed_after_a_finished_stage_runs_again_after_it_is_skipped(self):
        planned = programme._programme_stage_plan
        def plan(*args, **options):
            order = planned(*args, **options)
            order.update(discovery_position="after_prerequisites", discovery_after_stage="fit")
            return order
        crashes = [True]
        def discovery(_memory, _record, _context):
            self.calls.append("discovery")
            if crashes.pop() if crashes else False:
                raise Crash()
            return {"state": "checked", "new_products_need_full_source_review": []}
        options = {"root": self.fixture.root, "handlers": {}, "as_of": self.fixture.now, "discovery_step": discovery}
        with patch.object(programme, "_programme_stage_plan", side_effect=plan):
            with self.assertRaises(Crash):
                programme.advance_programme_turn(self.memory, "whole-programme", steps=self.steps(), **options)
            first = self.calls[:]; self.calls.clear()
            self.assertEqual(first[-1], "discovery")
            self.assertIn("fit", load_programme(self.memory, "whole-programme")["round_progress"]["finished_stages"])
            result = programme.advance_programme_turn(self.memory, "whole-programme", steps=self.steps(), **options)
        # Fit is not repeated; discovery, placed after it, runs again first.
        self.assertEqual(self.calls[0], "discovery")
        self.assertNotIn("fit", self.calls)
        self.assertEqual(self.calls.count("discovery"), 1)
        self.assertTrue(result["execution_order"]["discovery_attempted"])

    def test_a_new_round_runs_every_stage_again(self):
        self.advance(); self.calls.clear()
        self.advance()
        self.assertEqual(sorted(stage for stage in self.calls if stage != "discovery"), sorted(STAGES))
        self.assertEqual([turn["state"] for turn in self.turns()], ["closed", "closed"])

    def test_changed_code_or_unreadable_progress_runs_every_stage_again(self):
        for case in ("code", "malformed", "foreign_stage"):
            with self.subTest(case=case):
                with self.assertRaises(Crash):
                    self.advance(crash_at="photos")
                record = load_programme(self.memory, "whole-programme")
                self.assertTrue(record["round_progress"]["finished_stages"])
                if case == "malformed":
                    record["round_progress"]["finished_stages"] = "profiles"
                elif case == "foreign_stage":
                    record["round_progress"]["finished_stages"].append("not-a-stage")
                if case != "code":
                    programme._save(self.memory, record)
                self.calls.clear()
                with (patch("product_radar.operations.code_version", return_value=("f" * 64, True))
                      if case == "code" else nullcontext()):
                    self.advance()
                self.assertEqual(sorted(stage for stage in self.calls if stage != "discovery"), sorted(STAGES))
                self.calls.clear()

    def test_incomplete_code_fingerprint_is_not_proof_of_the_same_code(self):
        from product_radar.operations import code_version
        with self.assertRaises(Crash):
            self.advance(crash_at="photos")
        self.calls.clear()
        with patch("product_radar.operations.code_version", return_value=(code_version()[0], False)):
            self.advance()
        self.assertEqual(sorted(stage for stage in self.calls if stage != "discovery"), sorted(STAGES))

    def test_older_whole_programme_advance_keeps_no_round_progress(self):
        programme.advance_programme(self.memory, "whole-programme", root=self.fixture.root, handlers={},
            as_of=self.fixture.now, steps=self.steps(), max_cycles=1)
        self.assertNotIn("round_progress", load_programme(self.memory, "whole-programme"))
        self.assertEqual(programme._round_progress({}, None), None)


if __name__ == "__main__":
    unittest.main()
