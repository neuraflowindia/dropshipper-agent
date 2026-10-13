"""Existing accepted-type prerequisites run before their consumers.

Disposable real functional records and synthetic answers only. These checks
never contact a provider, change the installed task or launch live research.
"""

from copy import deepcopy
from datetime import timedelta
import json
import unittest
from unittest.mock import patch

import product_radar.programme as programme
from product_radar.fit_description import FUNCTIONAL_CONTRACT
from product_radar.fit_policy import FIT_POLICY_ID
from product_radar.models import digest, timestamp
from product_radar.product_queries import (current_query_profile, ensure_query_profiles,
    product_query_terms, query_profile_scope_hash)
from product_radar.research_ai import Jobs
from product_radar.research_flow import assessment_item, current_assessment_fit
from product_radar.work_turns import DEFAULT_POLICY
import test_functional_fit as fixtures
import test_functional_queries as query_fixtures
from test_research_flow import FakeRunner, NOW


class ProgrammeDependencyOrderTests(unittest.TestCase):
    def setUp(self):
        self.fixture = fixtures.FunctionalFitTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.memory, self.root, self.cid = self.fixture.memory, self.fixture.root, self.fixture.cid
        self.record = programme.prepare_programme(self.memory, "dependency-order",
            concept_ids=[self.cid], as_of=NOW)

    def plan(self, sequence=4, *, available=True, record=None):
        state = self.memory.snapshot()
        control = self.record if record is None else record
        saved = deepcopy(state); original = deepcopy(control)
        with patch.object(self.memory, "snapshot", side_effect=AssertionError("No extra history replay")):
            result = programme._programme_stage_plan(state, control,
                {"programme_turn_sequence": sequence}, at=NOW,
                inputs=programme._signature(state, control), model_available=available)
        self.assertEqual(state, saved)
        self.assertEqual(control, original)
        return result

    def test_legacy_fallback_is_not_an_accepted_definition(self):
        value = self.plan()
        self.assertEqual(value["stage_order"], list(programme.STAGES[1:] + programme.STAGES[:1]))
        self.assertEqual(value["priority_concept_ids"], {})
        self.assertEqual(value["discovery_position"], "after")

    def test_rotated_consumers_follow_due_wording_and_fit_exactly_once(self):
        self.fixture.accept()
        value = self.plan()
        self.assertEqual(value["stage_order"][:2], ["profiles", "fit"])
        self.assertEqual(value["stage_order"][2:], ["acquire", "match", "suppliers", "photos", "reports", "private_staging"])
        self.assertEqual(value["priority_concept_ids"], {"profiles": [self.cid], "fit": [self.cid]})
        self.assertEqual(len(value["stage_order"]), len(set(value["stage_order"])))

    def test_odd_discovery_follows_only_the_promoted_prerequisites(self):
        self.fixture.accept()
        value = self.plan(sequence=5)
        self.assertEqual(value["discovery_position"], "after_prerequisites")
        self.assertEqual(value["discovery_after_stage"], "fit")

    def test_current_wording_is_not_promoted_again(self):
        self.fixture.accept()
        ensure_query_profiles(self.memory, query_fixtures.Runner(), concept_ids=[self.cid], as_of=NOW)
        value = self.plan()
        self.assertEqual(value["priority_concept_ids"], {"fit": [self.cid]})
        self.assertEqual(value["stage_order"][0], "fit")

    def test_current_functional_fit_is_not_replaced_by_a_fresh_question(self):
        self.fixture.accept(); self.fixture.complete()
        value = self.plan()
        self.assertEqual(value["priority_concept_ids"], {"profiles": [self.cid]})
        self.assertEqual(value["stage_order"][0], "profiles")

    def test_both_current_restore_the_ordinary_fair_order(self):
        self.fixture.accept(); self.fixture.complete()
        ensure_query_profiles(self.memory, query_fixtures.Runner(), concept_ids=[self.cid], as_of=NOW)
        value = self.plan(sequence=5)
        self.assertEqual(value["priority_concept_ids"], {})
        self.assertEqual(value["discovery_position"], "before")
        self.assertEqual(value["stage_order"][0], "acquire")

    def test_actual_future_input_waits_are_not_erased(self):
        self.fixture.accept()
        state = self.memory.snapshot(); record = deepcopy(self.record)
        future = (timestamp(NOW) + timedelta(hours=1)).isoformat()
        for stage, binding in (("profiles", query_profile_scope_hash(state, self.cid)),
                ("fit", digest(assessment_item(state, self.cid, contract_version=FUNCTIONAL_CONTRACT)))):
            record["deferrals"][stage + ":" + self.cid] = {"stage": stage, "target": self.cid,
                "binding": binding, "reason": "AI_RATE_LIMITED", "at": NOW, "next_retry_at": future}
        self.assertEqual(self.plan(record=record)["priority_concept_ids"], {})

    def test_no_live_runner_keeps_the_normal_saved_answer_recovery_order(self):
        self.fixture.accept()
        self.assertEqual(self.plan(available=False)["priority_concept_ids"], {})

    def test_excluded_subject_is_not_priority_work(self):
        self.fixture.accept()
        original = self.memory.snapshot()["concepts"][self.cid]
        self.memory.put("concepts", self.cid, dict(original, status="excluded"), previous=original)
        self.assertEqual(self.plan()["priority_concept_ids"], {})

    def test_order_is_recorded_before_close_in_the_existing_safe_log_fields(self):
        from product_radar.operations import operation_run
        self.fixture.accept()
        noop = {stage: (lambda *_args: {"state": "checked"}) for stage in programme.STAGES}
        with operation_run(self.root) as recorder:
            programme.advance_programme_turn(self.memory, self.record["programme_id"], root=self.root,
                runner=FakeRunner(), as_of=NOW, handlers={}, steps=noop)
        rows = [json.loads(line) for line in (recorder.folder / (recorder.run_id + ".jsonl")).read_text().splitlines()]
        order = next(row for row in rows if row["event"] == "programme_order")
        self.assertEqual(order["state"], "after_prerequisites")
        self.assertEqual(order["next_phase"], "fit")
        self.assertEqual(order["counts"]["profiles_order"], 1)
        self.assertEqual(order["counts"]["fit_order"], 2)
        self.assertEqual(order["counts"]["profiles_priority"], 1)
        self.assertEqual(order["counts"]["fit_priority"], 1)
        self.assertFalse(order.get("useful_result", False))
        self.assertNotIn("priority_concept_ids", order)

    def test_actual_odd_round_finishes_wording_and_fit_before_discovery_spends_slots(self):
        self.fixture.accept()
        noop = {stage: (lambda *_args: {"state": "checked"}) for stage in programme.STAGES}
        for _ in range(4):
            programme.advance_programme_turn(self.memory, self.record["programme_id"], root=self.root,
                as_of=NOW, handlers={}, steps=noop)
        order = []
        class Runner:
            identity = "synthetic-prerequisite-order-v1"
            def __init__(self):
                self.calls = []
            def __call__(self, role, payload, folder):
                self.calls.append((role, deepcopy(payload)))
                if role == "C":
                    result = {"results": [query_fixtures.answer(item) for item in payload["items"]]}
                elif role == "E":
                    result = fixtures.response_for(payload)
                else:
                    return FakeRunner()(role, payload, folder)
                return result, {"input_tokens": 1, "cached_input_tokens": 0, "output_tokens": 1}
        runner = Runner()
        steps = dict(noop)
        for stage, callback in (("profiles", programme._profiles), ("fit", programme._fit)):
            def wrapped(memory, record, context, name=stage, function=callback):
                order.append(name)
                return function(memory, record, context)
            steps[stage] = wrapped
        def acquire(memory, record, context):
            order.append("acquire")
            state = memory.snapshot()
            self.assertIsNotNone(current_query_profile(state, self.cid))
            self.assertIsNotNone(current_assessment_fit(state, self.cid, expected_policy_id=FIT_POLICY_ID))
            self.assertEqual(state["assessments"][self.cid]["contract_version"], FUNCTIONAL_CONTRACT)
            self.assertTrue(product_query_terms(memory, self.cid, source="trends"))
            return {"state": "checked"}
        def discovery(memory, record, context):
            order.append("discovery")
            Jobs(memory, context["runner"]).run("A", {"items": [{"id": "extra-discovery",
                "source_text": {"title": "Blue sink caddy"}}]})
            return {"state": "checked"}
        steps["acquire"] = acquire
        policy = deepcopy(DEFAULT_POLICY); policy.update(model_executions=2, report_slots=0)
        value = programme.advance_programme_turn(self.memory, self.record["programme_id"], root=self.root,
            runner=runner, as_of=NOW, handlers={}, steps=steps, discovery_step=discovery, turn_policy=policy)
        self.assertEqual(order[:4], ["profiles", "fit", "discovery", "acquire"])
        self.assertEqual([role for role, _ in runner.calls], ["C", "E"])
        self.assertEqual(value["work_turn"]["counters"]["model_executions"], 2)
        self.assertEqual(programme.load_programme(self.memory, self.record["programme_id"])["last_results"]["acquire"],
                         {"state": "checked"})
        closed = self.memory.snapshot()["batch_runs"][value["work_turn"]["id"]]
        self.assertEqual(closed["cursors"]["discovery_position"], "after_prerequisites")
        self.assertEqual(closed["cursors"]["stage_order"], value["execution_order"]["stage_order"])
        self.assertEqual(order.count("profiles"), 1)
        self.assertEqual(order.count("fit"), 1)


if __name__ == "__main__":
    unittest.main()
