"""Focused spent-turn preparation checks using disposable fictional research."""

from copy import deepcopy
import unittest
from unittest.mock import patch

import test_fit_review as fit_fixtures
import test_functional_definition_review as definition_fixtures
import test_programme as programme_fixtures
import test_work_turns as turn_fixtures
from product_radar.models import SafetyError
from product_radar.programme import (_fit, _match, _model_preparation_scope,
    _profiles, _review_material_updates, _suppliers, prepare_programme, programme_turn_readiness)
from product_radar.work_turns import DEFAULT_POLICY, bind_work_turn, open_work_turn
from product_radar.research_ai import Jobs


class ProgrammePreparationScopeTests(unittest.TestCase):
    def setUp(self):
        fixture = turn_fixtures.WorkTurnTests(); fixture.setUp()
        self.addCleanup(fixture.tearDown)
        self.memory = fixture.memory
        policy = deepcopy(DEFAULT_POLICY); policy.update(model_executions=1, report_slots=0)
        self.turn = open_work_turn(self.memory, "preparation-scope", at=turn_fixtures.NOW, policy=policy)
        with bind_work_turn(self.memory, self.turn["id"]):
            Jobs(self.memory, turn_fixtures.Runner()).run("A", turn_fixtures.payload("spent"))
        self.state = self.memory.snapshot()
        self.state["concepts"] = {"one": {}, "two": {}, "three": {}}

    def scope(self, *, state=None, role="B", modes=("functional_definition_review",), purpose="B", **kwargs):
        with bind_work_turn(self.memory, self.turn["id"]):
            return _model_preparation_scope(self.memory, state or self.state,
                ["one", "two", "three"], purpose=purpose, role=role, modes=modes, **kwargs)

    def test_spent_owner_free_scope_is_pure_and_does_not_read_history_again(self):
        before = self.memory.snapshot_with_provenance()
        with bind_work_turn(self.memory, self.turn["id"]), \
                patch.object(self.memory, "snapshot", side_effect=AssertionError("Use the verified state")):
            selected, wait = _model_preparation_scope(self.memory, self.state, ["one", "two"],
                purpose="B", role="B", modes={"functional_definition_review"})
        self.assertEqual(selected, [])
        self.assertEqual(wait["concept_ids"], ["one", "two"])
        self.assertEqual(wait["reason"], "WORK_TURN_MODEL_LIMIT")
        self.assertFalse(wait["model_job_created"])
        self.assertEqual(self.memory.snapshot_with_provenance(), before)

    def test_unbound_scope_does_not_apply_an_old_turn_limit(self):
        selected, wait = _model_preparation_scope(self.memory, self.state, ["one", "two"],
            purpose="B", role="B", modes={"functional_definition_review"})
        self.assertEqual(selected, ["one", "two"])
        self.assertIsNone(wait)

    def test_original_runner_and_batch_siblings_remain_in_scope(self):
        self.state["jobs"]["old-batch"] = {"request": {"role": "B", "runner": "older-real-owner",
            "payload": {"mode": "functional_definition_review", "items": [{"id": "one"}, {"id": "two"}]}}}
        selected, wait = self.scope()
        self.assertEqual(selected, ["one", "two"])
        self.assertEqual(wait["concept_ids"], ["three"])

    def test_unrelated_mode_does_not_own_a_fresh_definition(self):
        self.state["jobs"]["other-b"] = {"request": {"role": "B",
            "payload": {"mode": "functional_example_review", "items": [{"concept_id": "one"}]}}}
        self.assertEqual(self.scope()[0], [])

    def test_alias_members_and_material_comparison_subjects_preserve_originals(self):
        for role, mode, item, purpose in (
                ("B", "functional_alias_review", {"id": "opaque-alias", "members": [{"concept_id": "two"}]}, "B"),
                ("E", "material_update_review", {"id": "opaque-update", "comparison_subjects": ["two"]}, "E")):
            with self.subTest(mode=mode):
                state = deepcopy(self.state)
                state["jobs"]["original"] = {"request": {"role": role, "payload": {"mode": mode, "items": [item]}}}
                selected, wait = self.scope(state=state, role=role, modes=(mode,), purpose=purpose)
                self.assertEqual(selected, ["two"])
                self.assertEqual(wait["concept_ids"], ["one", "three"])

    def test_scoped_supplier_original_uses_saved_meaning_not_its_alias_id(self):
        self.state["source_checks"]["accepted-meaning"] = {"concept_id": "two", "meaning_hash": "a" * 64}
        self.state["jobs"]["original-f"] = {"request": {"role": "F", "payload": {"items": [
            {"id": "candidate-opaque-alias", "product": {"meaning_hash": "a" * 64}}]}}}
        selected, wait = self.scope(role="F", modes=None, purpose="F")
        self.assertEqual(selected, ["two"])
        self.assertEqual(wait["concept_ids"], ["one", "three"])

    def test_unfamiliar_original_shape_is_not_hidden_by_the_hint(self):
        self.state["jobs"]["future-shape"] = {"request": {"role": "B", "payload": {
            "mode": "functional_definition_review", "items": [{"future_subject": "one"}]}}}
        selected, wait = self.scope()
        self.assertEqual(selected, ["one", "two", "three"])
        self.assertIsNone(wait)

    def test_malformed_saved_job_request_or_payload_keeps_normal_validation_in_scope(self):
        for job in (None, [], {}, {"request": None}, {"request": []},
                    {"request": "unexpected"}, {"request": {"role": "B", "payload": None}},
                    {"request": {"role": "B", "payload": []}},
                    {"request": {"role": "B", "payload": "unexpected"}}):
            with self.subTest(job=job):
                state = deepcopy(self.state)
                state["jobs"]["unknown-original"] = job
                selected, wait = self.scope(state=state)
                self.assertEqual(selected, ["one", "two", "three"])
                self.assertIsNone(wait)

    def test_local_work_is_retained_without_fabricating_a_model_owner(self):
        selected, wait = self.scope(locally_reusable=["two"])
        self.assertEqual(selected, ["two"])
        self.assertEqual(wait["concept_ids"], ["one", "three"])


class ProgrammeSpentPreparationTests(unittest.TestCase):
    def setUp(self):
        fixture = definition_fixtures.FunctionalDefinitionReviewTests(); fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        self.fixture, self.memory = fixture, fixture.memory
        self.cid = fixture.cids[0]
        self.record = prepare_programme(self.memory, "spent-programme-preparation", source_run_id="synthetic-sources",
            concept_ids=[self.cid], as_of=definition_fixtures.NOW)
        self.context = {"at": definition_fixtures.NOW, "runner": definition_fixtures.Runner(),
            "match_limit": 5, "fit_limit": 5, "profile_limit": 20, "supplier_limit": 5, "retry_seconds": 60}

    def test_spent_match_does_not_prepare_owner_free_definition_example_alias_or_catalogue_work(self):
        turn = self.fixture.spent_turn(); before = self.memory.snapshot_with_provenance()
        with bind_work_turn(self.memory, turn["id"]), \
                patch("product_radar.functional_definition_review.review_functional_definitions",
                      side_effect=AssertionError("No possible original definition owner")), \
                patch("product_radar.catalogue_scope_review.review_discovery_catalogue_scopes",
                      side_effect=AssertionError("No possible original catalogue owner")), \
                patch("product_radar.functional_alias_review.match_functional_aliases",
                      side_effect=AssertionError("No possible original or exact local alias")):
            result = _match(self.memory, self.record, self.context)
        wait = result["functional_definitions"]["preparation_wait"]
        self.assertEqual(wait["concept_ids"], [self.cid])
        self.assertFalse(wait["model_job_created"])
        self.assertEqual(self.memory.snapshot_with_provenance(), before)
        self.assertEqual(self.context["runner"].calls, [])

    def test_saved_definition_answer_is_recovered_at_capacity_without_another_execution(self):
        from product_radar.functional_definition_review import review_functional_definitions
        original = definition_fixtures.Runner()
        with patch("product_radar.functional_definition_review.accept_functional_definition",
                   side_effect=SafetyError("SAVED_FILE_UNAVAILABLE")):
            outcome = review_functional_definitions(self.memory, [self.cid], original, as_of=definition_fixtures.NOW)
        self.assertIn(self.cid, outcome["failures"])
        originals = {path: path.read_bytes() for path in (self.memory.root / "ai_jobs").glob("*/result.json")}
        turn = self.fixture.spent_turn()
        with bind_work_turn(self.memory, turn["id"]):
            result = _match(self.memory, self.record, self.context)
        self.assertIn(self.cid, result["functional_definitions"]["accepted"])
        self.assertEqual(self.context["runner"].calls, [])
        self.assertTrue(all(path.read_bytes() == raw for path, raw in originals.items()))
        self.assertEqual(self.memory.snapshot()["batch_runs"][turn["id"]]["counters"]["model_executions"], 1)

    def test_spent_functional_profile_does_not_prepare_unowned_membership_or_question(self):
        # Accept a genuine synthetic definition first; a functional C question
        # requires a model, unlike compact legacy/catalogue wording.
        definition_fixtures.review_functional_definitions(self.memory, [self.cid],
            definition_fixtures.Runner(), as_of=definition_fixtures.NOW)
        turn = self.fixture.spent_turn(); before = self.memory.snapshot_with_provenance()
        with bind_work_turn(self.memory, turn["id"]), \
                patch("product_radar.functional_types.prepare_example_memberships",
                      side_effect=AssertionError("Fresh functional C cannot use this turn")), \
                patch("product_radar.product_queries.ensure_query_profiles",
                      side_effect=AssertionError("No original C owner")):
            result = _profiles(self.memory, self.record, self.context)
        self.assertEqual(result["preparation_wait"]["concept_ids"], [self.cid])
        self.assertEqual(result["pending"], [])
        self.assertEqual(self.memory.snapshot_with_provenance(), before)

    def test_spent_unowned_supplier_does_not_open_captures_or_claim_it_finished(self):
        turn = self.fixture.spent_turn(); before = self.memory.snapshot_with_provenance()
        with bind_work_turn(self.memory, turn["id"]), \
                patch("product_radar.source_interpretation.review_supplier_results",
                      side_effect=AssertionError("Do not reread all unowned supplier captures")):
            result = _suppliers(self.memory, self.record, self.context)
        self.assertIsNone(result["remaining_inputs"])
        self.assertEqual(result["completed_results"], 0)
        self.assertEqual(result["preparation_wait"]["concept_ids"], [self.cid])
        self.assertEqual(self.memory.snapshot_with_provenance(), before)

    def test_spent_unowned_update_does_not_rebuild_the_report_pool(self):
        turn = self.fixture.spent_turn(); before = self.memory.snapshot_with_provenance()
        with bind_work_turn(self.memory, turn["id"]), \
                patch("product_radar.programme._material_update_work",
                      side_effect=AssertionError("No original comparable-update owner")):
            result = _review_material_updates(self.memory, self.record, self.context)
        self.assertIsNone(result["remaining"])
        self.assertEqual(result["outcomes"], [])
        self.assertEqual(self.memory.snapshot_with_provenance(), before)


class ProgrammeSupplierWaitReadinessTests(unittest.TestCase):
    def test_finished_supplier_subgroup_does_not_claim_untouched_subjects_are_finished(self):
        fixture = programme_fixtures.ProgrammeTests(); fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        record = fixture.prepare()
        wait = {"reason": "WORK_TURN_MODEL_LIMIT", "concept_ids": ["second"],
            "preparation_skipped": True, "model_job_created": False}
        inspected = {"selected_capture_ids": [], "remaining_inputs": 0, "reused_capture_ids": [],
            "failures": {}, "source_bindings": [], "skipped_captures": [], "selected_inputs": [], "accepted": {}}
        with patch("product_radar.programme._model_preparation_scope", return_value=(["first"], wait)), \
                patch("product_radar.source_interpretation.review_supplier_results", return_value=inspected) as review:
            result = _suppliers(fixture.memory, record,
                {"at": fixture.now, "supplier_limit": 5, "runner": None})
        self.assertEqual(review.call_args.kwargs["concept_ids"], ["first"])
        self.assertIsNone(result["remaining_inputs"])
        self.assertEqual(result["preparation_wait"]["concept_ids"], ["second"])
        self.assertEqual(result["completed_results"], 0)
        self.assertEqual(inspected["remaining_inputs"], 0)

    def test_uninspected_supplier_wait_remains_pending_in_an_otherwise_finished_pool(self):
        fixture = programme_fixtures.ProgrammeTests(); fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        record = fixture.prepare(concept_ids=["first"])
        record["status"] = "research_pool_finished"
        record["progress"] = {"current_query_profiles": 1, "fit_products": 1,
            "fit_states": {"accepted": 1}}
        record["last_results"]["suppliers"] = {"remaining_inputs": None}
        state = fixture.memory.snapshot(); before = fixture.memory.snapshot_with_provenance()
        initial = programme_turn_readiness(fixture.memory, record, root=fixture.root,
            as_of=fixture.now, state=state)
        self.assertFalse(initial["available"])
        record["last_results"]["suppliers"]["preparation_wait"] = {
            "reason": "WORK_TURN_MODEL_LIMIT", "concept_ids": ["first"],
            "preparation_skipped": True, "model_job_created": False}
        pending = programme_turn_readiness(fixture.memory, record, root=fixture.root,
            as_of=fixture.now, state=state)
        self.assertTrue(pending["available"])
        self.assertEqual(fixture.memory.snapshot_with_provenance(), before)


class ProgrammeSpentFitTests(unittest.TestCase):
    def setUp(self):
        fixture = fit_fixtures.FitReviewTests(); fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        self.memory, self.fixture = fixture.memory, fixture

    def test_fresh_fit_wait_happens_before_canonical_base_preparation(self):
        clone = deepcopy(self.memory.snapshot()["concepts"][self.fixture.concept_id])
        self.memory.put("concepts", "fresh-fit-subject", clone)
        policy = deepcopy(DEFAULT_POLICY); policy.update(model_executions=1, report_slots=0)
        turn = open_work_turn(self.memory, "fit-preparation-limit", at=fit_fixtures.NOW, policy=policy)
        with bind_work_turn(self.memory, turn["id"]):
            Jobs(self.memory, turn_fixtures.Runner()).run("A", turn_fixtures.payload("spent"))
        before = self.memory.snapshot_with_provenance()
        with bind_work_turn(self.memory, turn["id"]), \
                patch("product_radar.fit_review.prepare_practical_fit_reviews",
                      side_effect=AssertionError("Do not create a fresh fit base without room")):
            result = _fit(self.memory, {"concept_ids": ["fresh-fit-subject"], "deferrals": {}},
                {"at": fit_fixtures.NOW, "runner": None, "fit_limit": 5, "retry_seconds": 60})
        self.assertEqual(result["prepared"], 0)
        self.assertEqual(result["preparation_wait"]["concept_ids"], ["fresh-fit-subject"])
        self.assertEqual(result["remaining_to_prepare"], 1)
        self.assertEqual(self.memory.snapshot_with_provenance(), before)


if __name__ == "__main__":
    unittest.main()
