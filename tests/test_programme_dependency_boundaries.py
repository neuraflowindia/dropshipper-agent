"""Independent ordering boundaries in disposable synthetic research stores."""

from copy import deepcopy
from datetime import timedelta
import unittest
from unittest.mock import patch

import product_radar.programme as programme
from product_radar.fit_description import DESCRIPTION_CONTRACT, FUNCTIONAL_CONTRACT
from product_radar.fit_policy import FIT_POLICY_ID
from product_radar.fit_review import prepare_practical_fit_reviews, run_fit_reviews
from product_radar.models import digest, timestamp
from product_radar.product_queries import query_profile_scope_hash
from product_radar.research_flow import assessment_item, current_assessment_fit
from product_radar.work_turns import bind_work_turn, close_work_turn, open_work_turn
import test_fit_description as description_fixtures
import test_functional_fit as fit_fixtures
import test_functional_queries as query_fixtures
from test_research_flow import FakeRunner, NOW


class ProgrammeDependencyBoundaryTests(unittest.TestCase):
    def setUp(self):
        self.fixture = fit_fixtures.FunctionalFitTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.memory, self.root, self.cid = self.fixture.memory, self.fixture.root, self.fixture.cid
        self.record = programme.prepare_programme(self.memory, "dependency-boundary",
            concept_ids=[self.cid], as_of=NOW)

    def noops(self):
        return {stage: (lambda *_args: {"state": "checked"}) for stage in programme.STAGES}

    def advance(self, **options):
        return programme.advance_programme_turn(self.memory, self.record["programme_id"],
            root=self.root, as_of=NOW, handlers={}, steps=options.pop("steps", self.noops()), **options)

    def plan(self, record=None, sequence=4):
        state = self.memory.snapshot()
        row = self.record if record is None else record
        return programme._programme_stage_plan(state, row,
            {"programme_turn_sequence": sequence}, at=NOW,
            inputs=programme._signature(state, row), model_available=True)

    def context(self, stage):
        return {"at": NOW, "fixed_as_of": NOW, "runner": None, "profile_limit": 1,
            "fit_limit": 1, "retry_seconds": 300,
            "priority_concept_ids": {stage: [self.cid]}}

    def legacy_record(self, programme_id="dependency-priority"):
        state = self.memory.snapshot()
        legacy = "aaa-legacy"
        self.memory.put("concepts", legacy, deepcopy(state["concepts"][self.cid]))
        return programme.prepare_programme(self.memory, programme_id,
            concept_ids=[legacy, self.cid], as_of=NOW), legacy

    def test_current_legacy_v3_fit_still_requires_the_functional_review(self):
        prepared = prepare_practical_fit_reviews(self.memory, concept_ids=[self.cid],
            as_of=NOW, contract_version=DESCRIPTION_CONTRACT)
        result = run_fit_reviews(self.memory, description_fixtures.DescriptionRunner(),
            [prepared[0]["review_id"]])
        self.assertEqual(result[0]["status"], "accepted")
        self.fixture.accept()
        state = self.memory.snapshot()
        self.assertIsNotNone(current_assessment_fit(state, self.cid, expected_policy_id=FIT_POLICY_ID))
        self.assertEqual(state["assessments"][self.cid]["contract_version"], DESCRIPTION_CONTRACT)
        self.assertEqual(self.plan()["priority_concept_ids"]["fit"], [self.cid])

    def test_exact_future_wait_survives_title_change_but_not_changed_meaning(self):
        self.fixture.accept()
        state = self.memory.snapshot(); record = deepcopy(self.record)
        future = (timestamp(NOW) + timedelta(hours=1)).isoformat()
        for stage, binding in (("profiles", query_profile_scope_hash(state, self.cid)),
                ("fit", digest(assessment_item(state, self.cid, contract_version=FUNCTIONAL_CONTRACT)))):
            record["deferrals"][stage + ":" + self.cid] = {"stage": stage, "target": self.cid,
                "binding": binding, "reason": "AI_RATE_LIMITED", "at": NOW, "next_retry_at": future}
        self.assertEqual(self.plan(record)["priority_concept_ids"], {})
        self.fixture.accept(dict(query_fixtures.DEFINITION, report_title="A better looking caddy"))
        self.assertEqual(self.plan(record)["priority_concept_ids"], {})
        self.fixture.accept(dict(query_fixtures.DEFINITION, primary_use="Keep wet utensils draining"))
        self.assertEqual(self.plan(record)["priority_concept_ids"], {"profiles": [self.cid], "fit": [self.cid]})

    def test_exact_future_lane_wait_suppresses_only_its_stage(self):
        self.fixture.accept()
        state = self.memory.snapshot(); record = deepcopy(self.record)
        inputs = programme._signature(state, record)
        record["deferrals"]["lane:profiles"] = {"stage": "lane", "target": "profiles",
            "binding": digest({"stage": "profiles", "inputs": inputs}), "reason": "PROGRAMME_LOCAL_FILE_FAILED",
            "at": NOW, "next_retry_at": (timestamp(NOW) + timedelta(hours=1)).isoformat()}
        self.assertEqual(self.plan(record)["priority_concept_ids"], {"fit": [self.cid]})

    def test_profile_priority_changes_only_local_selection_not_frozen_ids(self):
        self.fixture.accept(); record, legacy = self.legacy_record()
        before = deepcopy(programme._binding(record)); calls = []
        def ensure(memory, runner, **options):
            calls.append(deepcopy(options))
            return {"records": [], "reused": [], "pending": [], "job_ids": []}
        with patch("product_radar.product_queries.ensure_query_profiles", side_effect=ensure):
            programme._profiles(self.memory, record, self.context("profiles"))
        self.assertEqual(calls[0]["concept_ids"], [self.cid, legacy])
        self.assertEqual(calls[0]["limit"], 1)
        self.assertEqual(programme._binding(record), before)
        self.assertEqual(record["concept_ids"], [legacy, self.cid])

    def test_fit_priority_changes_only_local_selection_not_frozen_ids(self):
        self.fixture.accept(); record, legacy = self.legacy_record()
        before = deepcopy(programme._binding(record)); calls = []
        def prepare(memory, **options):
            calls.append(deepcopy(options))
            return []
        with patch("product_radar.fit_review.prepare_practical_fit_reviews", side_effect=prepare):
            programme._fit(self.memory, record, self.context("fit"))
        self.assertEqual(calls, [{"concept_ids": [self.cid], "as_of": NOW, "contract_version": FUNCTIONAL_CONTRACT}])
        self.assertEqual(programme._binding(record), before)
        self.assertEqual(record["concept_ids"], [legacy, self.cid])

    def test_promoted_scopes_move_locally_and_nonpromoted_order_stays_unchanged(self):
        self.fixture.accept(); _, legacy = self.legacy_record()
        record = programme.prepare_programme(self.memory, "dependency-scope-priority",
            concept_ids=[legacy], as_of=NOW)
        def discovery(*_args):
            return {"state": "checked", "new_concept_ids": [self.cid]}
        programme.advance_programme_turn(self.memory, record["programme_id"], root=self.root,
            as_of=NOW, handlers={}, steps=self.noops(), discovery_step=discovery)
        current = programme.load_programme(self.memory, record["programme_id"])
        scopes = programme._review_scopes(current)
        self.assertEqual(len(scopes), 2)
        before = {scope["programme_id"]: programme._binding(scope) for scope in scopes}
        calls = []
        steps = {}
        for stage in programme.STAGES:
            def step(memory, scope, context, name=stage):
                calls.append((name, scope["programme_id"]))
                return {"state": "checked"}
            steps[stage] = step
        programme.advance_programme_turn(self.memory, record["programme_id"], root=self.root,
            runner=FakeRunner(), as_of=NOW, handlers={}, steps=steps)
        parent, child = [scope["programme_id"] for scope in scopes]
        for stage in ("profiles", "fit"):
            self.assertEqual([scope for name, scope in calls if name == stage], [child, parent])
        self.assertEqual([scope for name, scope in calls if name == "match"], [parent, child])
        after = programme._review_scopes(programme.load_programme(self.memory, record["programme_id"]))
        self.assertEqual({scope["programme_id"]: programme._binding(scope) for scope in after}, before)

    def test_even_turn_inventory_keeps_first_claim_in_promoted_stages(self):
        self.fixture.accept(); self.advance()
        calls = []
        steps = {stage: (lambda memory, record, context, name=stage:
            calls.append(("ordinary", name)) or {"state": "checked"}) for stage in programme.STAGES}
        def inventory(memory, record, context, stage):
            calls.append(("inventory", stage))
        with patch.object(programme, "_run_inventory_stage", side_effect=inventory):
            result = self.advance(steps=steps, runner=FakeRunner())
        self.assertEqual(result["execution_order"]["stage_order"][:2], ["profiles", "fit"])
        for stage in programme.STAGES:
            self.assertLess(calls.index(("inventory", stage)), calls.index(("ordinary", stage)))

    def test_even_turn_material_updates_keep_first_claim_before_promoted_fit(self):
        self.fixture.accept()
        first = open_work_turn(self.memory, self.record["programme_id"], at=NOW)
        close_work_turn(self.memory, first["id"], at=NOW, reason="synthetic_first_turn")
        second = open_work_turn(self.memory, self.record["programme_id"], at=NOW)
        calls = []
        def updates(*_args):
            calls.append("updates")
            return {"selected": 0, "outcomes": []}
        def prepare(*_args, **_options):
            calls.append("fit")
            return []
        with bind_work_turn(self.memory, second["id"]), \
                patch.object(programme, "_review_material_updates", side_effect=updates), \
                patch("product_radar.fit_review.prepare_practical_fit_reviews", side_effect=prepare):
            programme._fit(self.memory, self.record, self.context("fit"))
        self.assertEqual(calls, ["updates", "fit"])
        self.assertEqual(self.memory.snapshot()["batch_runs"][second["id"]]["counters"]["model_executions"], 0)

    def test_owner_stop_in_first_promoted_stage_records_only_that_visit(self):
        self.fixture.accept(); self.advance(); self.advance()
        calls = []
        steps = self.noops()
        def stop(memory, record, context):
            calls.append("profiles")
            programme.set_programme_service_state(memory, enabled=False, as_of=NOW)
            return {"state": "checked"}
        steps["profiles"] = stop
        discovery = lambda *_args: calls.append("discovery") or {"state": "checked"}
        result = self.advance(steps=steps, runner=FakeRunner(), discovery_step=discovery)
        self.assertEqual(calls, ["profiles"])
        self.assertEqual(result["execution_order"]["visited_stages"], ["profiles"])
        self.assertFalse(result["execution_order"]["discovery_attempted"])
        closed = self.memory.snapshot()["batch_runs"][result["work_turn"]["id"]]
        self.assertEqual(closed["cursors"]["visited_stages"], ["profiles"])
        self.assertFalse(closed["cursors"]["discovery_attempted"])

    def test_owner_stop_in_last_promoted_stage_prevents_discovery_callback(self):
        self.fixture.accept()
        self.advance(); self.advance()
        observed = []
        steps = self.noops()
        steps["profiles"] = lambda *_args: observed.append("profiles") or {"state": "checked"}
        def stop(memory, record, context):
            observed.append("fit")
            programme.set_programme_service_state(memory, enabled=False, as_of=NOW)
            return {"state": "checked"}
        def discovery(*_args):
            observed.append("discovery")
            return {"state": "checked"}
        steps["fit"] = stop
        result = self.advance(steps=steps, runner=FakeRunner(), discovery_step=discovery)
        self.assertEqual(observed, ["profiles", "fit"])
        self.assertEqual(result["tick_state"], "owner_paused")
        self.assertEqual(result["execution_order"]["visited_stages"], ["profiles", "fit"])
        self.assertFalse(result["execution_order"]["discovery_attempted"])
        closed = self.memory.snapshot()["batch_runs"][result["work_turn"]["id"]]
        self.assertEqual(closed["cursors"]["visited_stages"], ["profiles", "fit"])
        self.assertEqual(closed["cursors"]["discovery_position"], "after_prerequisites")
        self.assertFalse(closed["cursors"]["discovery_attempted"])


if __name__ == "__main__":
    unittest.main()
