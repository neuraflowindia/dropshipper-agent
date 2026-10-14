"""Independent ordering boundaries in disposable synthetic research stores."""

from copy import deepcopy
from datetime import timedelta
import unittest
from unittest.mock import patch

import product_radar.programme as programme
from product_radar.fit_description import DESCRIPTION_CONTRACT, FUNCTIONAL_CONTRACT
from product_radar.fit_policy import FIT_POLICY_ID
from product_radar.fit_review import prepare_practical_fit_reviews, run_fit_reviews
from product_radar.models import SafetyError, digest, timestamp
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
            inputs=programme._signature(state, row), model_available=True,
            report_pool=lambda: programme._programme_report_pool(self.memory, row, as_of=NOW, state=state,
                                                                 functional_packets=True))

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

    def held(self):
        from product_radar.work_turns import _discovery_var
        holder = _discovery_var(self.memory).get()
        return holder["count"] if holder is not None else None

    def test_platform_lane_runs_every_round_and_takes_turns_with_discovery(self):
        calls = []
        def platforms(memory, record, context):
            calls.append(("platforms", self.held()))
            return {"discovery": {"new_concept_ids": []}}
        def discovery(memory, record, context):
            calls.append(("discovery", self.held()))
            return {"state": "checked"}
        steps = self.noops()
        steps["acquire"] = lambda *_args: calls.append(("acquire", self.held())) or {"state": "checked"}
        orders = []
        for _ in range(3):
            calls.clear()
            result = self.advance(steps=steps, runner=FakeRunner(), discovery_step=discovery, platform_step=platforms)
            orders.append([name for name, _ in calls if name != "acquire"])
            self.assertTrue(result["execution_order"]["platforms_attempted"])
            # Whichever lane goes first has the round's held share; nothing is held once it runs.
            self.assertTrue(all(count == 0 for name, count in calls if name != "acquire"))
        firsts = [order[0] for order in orders if order]
        self.assertIn("platforms", firsts)
        self.assertTrue(all(order.count("platforms") == 1 for order in orders))
        self.assertTrue(any(order[:2] == ["discovery", "platforms"] for order in orders))
        self.assertTrue(any(order[:2] == ["platforms", "discovery"] for order in orders))
        record = programme.load_programme(self.memory, self.record["programme_id"])
        self.assertEqual(record["last_results"]["platforms"]["queued_follow_on_products"], 0)
        # A failing discovery (then waiting in its lane) never stops the platforms.
        def failing(memory, record, context):
            calls.append(("discovery", None))
            raise SafetyError("DISCOVERY_SLICE_INVALID")
        for _ in range(2):
            calls.clear()
            self.advance(steps=steps, runner=FakeRunner(), discovery_step=failing, platform_step=platforms)
            self.assertIn(("platforms", 0), calls)
        record = programme.load_programme(self.memory, self.record["programme_id"])
        self.assertEqual(record["last_results"]["discovery"]["reason"], "DISCOVERY_SLICE_INVALID")

    def test_platform_lane_runs_without_discovery_and_holds_the_share_for_it(self):
        calls = []
        def platforms(memory, record, context):
            calls.append(("platforms", self.held()))
            return {"discovery": {"new_concept_ids": [self.cid]}}
        steps = {stage: (lambda memory, record, context, name=stage: calls.append((name, self.held())) or {"state": "checked"})
                 for stage in programme.STAGES}
        from product_radar.work_turns import DISCOVERY_RESERVED_EXECUTIONS
        rounds = []
        for _ in range(2):
            calls.clear()
            result = self.advance(steps=steps, runner=FakeRunner(), platform_step=platforms)
            self.assertTrue(result["execution_order"]["platforms_attempted"])
            self.assertTrue(result["execution_order"]["report_supply_short"])
            rounds.append((result["execution_order"]["discovery_position"], list(calls)))
        # The lane runs at discovery's place (before or after the stages); while
        # short of report types the stages before it leave it the held share.
        self.assertEqual([position for position, _ in rounds], ["before", "after"])
        self.assertEqual(rounds[0][1][0], ("platforms", 0))
        self.assertEqual(rounds[1][1][-1], ("platforms", 0))
        self.assertTrue(all(count == DISCOVERY_RESERVED_EXECUTIONS for _, count in rounds[1][1][:-1]))
        record = programme.load_programme(self.memory, self.record["programme_id"])
        self.assertEqual(record["last_results"]["platforms"]["queued_follow_on_products"], 0)  # already in this group

    def test_platform_lane_failure_is_recorded_without_a_lane_wait_but_integrity_stops(self):
        def broken(memory, record, context):
            raise SafetyError("DROPSHIP_REFRESH_BUSY")
        self.advance(platform_step=broken)
        record = programme.load_programme(self.memory, self.record["programme_id"])
        self.assertEqual(record["last_results"]["platforms"]["reason"], "DROPSHIP_REFRESH_BUSY")
        self.assertFalse(any(row["stage"] == "lane" and row["target"] == "platforms" for row in record["deferrals"].values()))
        for bad in ({"state": "checked"}, None):
            self.advance(platform_step=lambda *_args, bad=bad: bad)
            record = programme.load_programme(self.memory, self.record["programme_id"])
            self.assertEqual(record["last_results"]["platforms"]["reason"], "PROGRAMME_PLATFORM_RESULT_INVALID")
        self.advance(platform_step=lambda *_args: (_ for _ in ()).throw(OSError("disk")))
        record = programme.load_programme(self.memory, self.record["programme_id"])
        self.assertEqual(record["last_results"]["platforms"]["reason"], "PROGRAMME_PLATFORMS_FAILED")
        failure = next(iter(programme.INTEGRITY_FAILURES))
        with self.assertRaisesRegex(SafetyError, failure):
            self.advance(platform_step=lambda *_args: (_ for _ in ()).throw(SafetyError(failure)))
        with self.assertRaisesRegex(SafetyError, "PROGRAMME_STEPS_INVALID"):
            self.advance(platform_step="not callable")

    def test_each_round_has_its_own_signed_in_browser_share(self):
        from product_radar import signed_in_round
        shares = []
        steps = self.noops()
        steps["acquire"] = lambda *_args: shares.append(signed_in_round._ROUND.get()) or {"state": "checked"}
        self.advance(steps=steps); self.advance(steps=steps)
        self.assertEqual(len(shares), 2)
        self.assertTrue(all(share is not None and share["seconds"] == 0 for share in shares))
        self.assertIsNot(shares[0], shares[1])
        self.assertIsNone(signed_in_round._ROUND.get())

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
