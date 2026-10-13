"""Independent offline review of the extracted programme writer lifecycle."""

from copy import deepcopy
import inspect
from pathlib import Path
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import product_radar.programme as programme
from product_radar.models import SafetyError
from product_radar.research_ai import Jobs
from product_radar.storage import Journal
from product_radar.work_turns import DEFAULT_POLICY, active_work_turn
import test_programme as programme_fixtures
from test_research_flow import FakeRunner


class ProgrammeTurnWriterReviewTests(unittest.TestCase):
    def setUp(self):
        self.fixture = programme_fixtures.ProgrammeTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.memory = self.fixture.memory
        self.fixture.prepare()

    def advance_turn(self, **options):
        return programme.advance_programme_turn(
            self.memory, "whole-programme", root=self.fixture.root,
            handlers={}, as_of=self.fixture.now, **options)

    def test_legacy_wrapper_preserves_keyword_only_signature_and_forwarding(self):
        before = inspect.signature(programme._advance_programme_locked)
        self.assertEqual(inspect.signature(programme.advance_programme), before)
        self.assertEqual(before.parameters["root"].kind, inspect.Parameter.KEYWORD_ONLY)
        sentinel = {"same_return_contract": True}

        def core(memory, programme_id, **options):
            self.assertIs(memory, self.memory)
            self.assertEqual(programme_id, "whole-programme")
            self.assertEqual(options, {"root": self.fixture.root, "max_cycles": 2,
                                       "source_limit": 3, "handlers": {}})
            with self.assertRaisesRegex(SafetyError, "RUNNER_ALREADY_ACTIVE"):
                with Journal(self.memory.root / "programme_writer").locked():
                    self.fail("Legacy wrapper released its writer before the core")
            self.assertIsNone(active_work_turn(memory))
            return sentinel

        with patch.object(programme, "_advance_programme_locked", side_effect=core):
            result = programme.advance_programme(
                self.memory, "whole-programme", root=self.fixture.root,
                max_cycles=2, source_limit=3, handlers={})
        self.assertIs(result, sentinel)

    def test_close_failure_releases_writer_and_resumes_same_spent_turn(self):
        runner = FakeRunner()
        policy = deepcopy(DEFAULT_POLICY)
        policy.update(model_executions=1, report_slots=0)
        steps = self.fixture.noops()

        def real_job(memory, _record, _context):
            job_id, result = Jobs(memory, runner).run("A", {"items": [{
                "id": "writer-close-recovery", "source_text": {"title": "Blue sink caddy"}}]})
            return {"job_id": job_id, "accepted": result["accepted"]}

        steps["profiles"] = real_job
        with patch("product_radar.work_turns.close_work_turn",
                   side_effect=SafetyError("RESEARCH_DEPENDENCY_CHANGED")):
            with self.assertRaisesRegex(SafetyError, "RESEARCH_DEPENDENCY_CHANGED"):
                self.advance_turn(steps=steps, turn_policy=policy)
        turns = [row for row in self.memory.snapshot()["batch_runs"].values()
                 if row.get("kind") == "work-turn-v1"]
        self.assertEqual(len(turns), 1)
        self.assertEqual(turns[0]["state"], "open")
        self.assertEqual(turns[0]["counters"]["model_executions"], 1)
        self.assertEqual(len(runner.calls), 1)
        self.assertIsNone(active_work_turn(self.memory))
        with Journal(self.memory.root / "programme_writer").locked():
            pass

        resumed = self.advance_turn(steps=steps, turn_policy=policy)
        self.assertEqual(resumed["work_turn"]["id"], turns[0]["id"])
        self.assertEqual(resumed["work_turn"]["state"], "closed")
        self.assertEqual(resumed["work_turn"]["counters"]["model_executions"], 1)
        self.assertEqual(len(runner.calls), 1)

    def test_core_exception_restores_context_and_releases_writer_without_closing(self):
        with patch.object(programme, "_advance_programme_locked", side_effect=RuntimeError("local test")):
            with self.assertRaisesRegex(RuntimeError, "local test"):
                self.advance_turn(steps=self.fixture.noops())
        self.assertIsNone(active_work_turn(self.memory))
        turns = [row for row in self.memory.snapshot()["batch_runs"].values()
                 if row.get("kind") == "work-turn-v1"]
        self.assertEqual(len(turns), 1)
        self.assertEqual(turns[0]["state"], "open")
        with Journal(self.memory.root / "programme_writer").locked():
            pass


if __name__ == "__main__":
    unittest.main()
