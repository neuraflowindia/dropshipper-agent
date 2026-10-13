"""Actual cooperative caller links original discovery to later accepted types."""

from copy import deepcopy
import unittest

import test_catalogue_scope_review as fixtures
from product_radar.programme import _match, prepare_programme, programme_turn_readiness
from product_radar.research_scope import current_catalogue_scopes
from product_radar.work_turns import bind_work_turn, close_work_turn, open_work_turn


class ProgrammeCatalogueScopeJoinTests(unittest.TestCase):
    def setUp(self):
        fixture = fixtures.CatalogueScopeTests(); fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        self.fixture, self.memory, self.cid = fixture, fixture.memory, fixture.cid
        self.record = prepare_programme(self.memory, "scope-normal-caller", source_run_id="scope-source",
            concept_ids=[self.cid], as_of=fixtures.waves.NOW)
        self.runner = fixtures.ScopeRunner()
        self.context = {"at": fixtures.waves.NOW, "runner": self.runner, "match_limit": 5,
            "retry_seconds": 3600, "discovery_catalogue": fixture.catalogue}

    def match(self, record=None):
        turn = open_work_turn(self.memory, self.record["programme_id"], at=self.context["at"])
        try:
            with bind_work_turn(self.memory, turn["id"]):
                return _match(self.memory, record or self.record, self.context)
        finally:
            close_work_turn(self.memory, turn["id"], at=self.context["at"], reason="Synthetic normal scope join")

    def test_actual_match_joins_later_definition_to_saved_discovery_once(self):
        before = self.memory.snapshot()
        result = self.match()
        self.assertEqual(result["catalogue_scopes"]["outcomes"][0]["state"], "current")
        scopes = current_catalogue_scopes(self.memory.snapshot(), self.fixture.catalogue, self.cid)
        self.assertEqual(len(scopes), 1)
        self.assertEqual(scopes[0]["relationship"], "narrower")
        for table in before:
            if table not in {"jobs", "source_checks", "batch_runs"}:
                self.assertEqual(before[table], self.memory.snapshot()[table], table)
        self.match()
        self.assertEqual(len(self.runner.calls), 1)

    def test_follow_on_lane_does_not_repeat_global_scope_review(self):
        follow_on = deepcopy(self.record)
        follow_on["parent_programme_id"] = "actual-parent"
        result = self.match(follow_on)
        self.assertIsNone(result["catalogue_scopes"])
        self.assertEqual(self.runner.calls, [])

    def test_pure_wake_sees_original_scope_work_and_does_not_launch_it(self):
        def discovery(*args):
            raise AssertionError("Pure readiness must not run discovery")
        discovery.discovery_catalogue = self.fixture.catalogue
        before = self.memory.snapshot()
        hint = programme_turn_readiness(self.memory, self.record, root=self.fixture.root,
            runner=self.runner, discovery_step=discovery, as_of=fixtures.waves.NOW)
        self.assertTrue(hint["ready"], hint)
        self.assertEqual(self.memory.snapshot(), before)
        self.assertEqual(self.runner.calls, [])


if __name__ == "__main__":
    unittest.main()
