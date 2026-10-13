"""Actual saved source questions compete with B through mocked finite AI turns."""

from contextlib import ExitStack
from copy import deepcopy
from datetime import timedelta
import unittest
from unittest.mock import Mock, patch

from product_radar import programme, source_interpretation
from product_radar.functional_types import effective_functional_definition
from product_radar.marketplace_browser import build_marketplace_request
from product_radar.models import SafetyError, digest, timestamp
from product_radar.research_ai import Jobs
from product_radar.research_memory import import_capture
from product_radar.research_scope import validated_scoped_evidence
from product_radar.research_sources import map_research_rows
from product_radar.source_coverage import execute_source_run, prepare_source_tasks
from product_radar.work_turns import (DEFAULT_POLICY, bind_work_turn, close_work_turn, open_work_turn)
import test_functional_definition_review as definition_fixtures
import test_functional_queries as query_fixtures
import test_functional_source_interpretation as source_fixtures
from test_research_flow import FakeRunner
from test_research_sources import amazon_row, request


class Runner:
    identity = "synthetic-programme-source-priority-v1"

    def __init__(self):
        self.calls = []
        self.source = source_fixtures.Runner()
        self.definition = definition_fixtures.Runner()
        self.other = FakeRunner()

    def __call__(self, role, payload, folder):
        self.calls.append((role, deepcopy(payload)))
        if role == "E":
            return self.source(role, payload, folder)
        if role == "B":
            return self.definition(role, payload, folder)
        return self.other(role, payload, folder)


class ProgrammeSourcePriorityTests(unittest.TestCase):
    def setUp(self):
        fixture = query_fixtures.FunctionalQueryTests()
        fixture.setUp(); self.addCleanup(fixture.doCleanups)
        self.fixture, self.memory, self.root = fixture, fixture.memory, fixture.root
        self.now, self.cid = query_fixtures.NOW, fixture.cid
        fixture.accept()
        self.other_cid = next(cid for cid in self.memory.snapshot()["concepts"] if cid != self.cid)
        self.runner = Runner()
        clock = patch("product_radar.research_ai.utc_now", return_value=self.now)
        clock.start(); self.addCleanup(clock.stop)
        actual_request = build_marketplace_request("amazon-bestsellers", department="kitchen")
        planned = prepare_source_tasks(self.memory, [{"route": "amazon-bestsellers", "request": actual_request,
            "concept_ids": [self.cid]}], run_id="source-priority-original", as_of=self.now)

        def saved_cards(context):
            context["record_dispatch"]()
            values = [dict(amazon_row(), title="Draining sink caddy holder", asin="B012345670",
                           link="https://www.amazon.in/dp/B012345670")]
            imported = import_capture(self.memory, source="amazon", acquisition_id=context["acquisition_id"],
                observed_at=self.now, mapped=map_research_rows(values, request()), capture_hash=digest(values),
                reference={"fixture": "synthetic", "route": "amazon-bestsellers", "request": actual_request})
            ids = sorted(ref for ref, row in self.memory.snapshot()["observations"].items()
                         if row["capture_id"] == imported["capture_id"])
            return {"schema_version": 1, "route": "amazon-bestsellers", "acquisition_id": context["acquisition_id"],
                "observed_at": self.now, "state": "usable", "dispatch_started": True,
                "reference": context["acquisition_id"] + "/receipt.json", "capture_ids": [imported["capture_id"]],
                "observation_ids": ids, "target_results": [], "reason": "Original synthetic marketplace cards.",
                "retryable": False}

        execute_source_run(self.memory, "source-priority-original", handlers={"amazon-bestsellers": saved_cards},
            as_of=self.now, bounded_call=lambda callback, context, _timeout: callback(context))
        self.task_id = planned["task_ids"][0]
        self.record = programme.prepare_programme(self.memory, "source-priority-programme",
            source_run_id="source-priority-original", concept_ids=[self.cid, self.other_cid], as_of=self.now)
        self.context = {"memory": self.memory, "root": self.root, "at": self.now, "fixed_as_of": self.now,
            "runner": self.runner, "match_limit": 5, "retry_seconds": 60, "stage": "match",
            "cooperative_mode": True}
        questions = source_interpretation._questions(self.memory.snapshot(), run_id=self.record["source_run_id"],
                                                     as_of=self.now)
        self.assertEqual(len(questions), 1)
        self.assertEqual(questions[0]["kind"], "functional_source_scope")
        self.assertTrue(questions[0]["unknown_ids"])

    def turn(self, *, even, spent=0):
        first = open_work_turn(self.memory, self.record["programme_id"], at=self.now, policy=DEFAULT_POLICY)
        if even:
            close_work_turn(self.memory, first["id"], at=self.now, reason="Synthetic preceding round finished.")
            turn = open_work_turn(self.memory, self.record["programme_id"], at=self.now, policy=DEFAULT_POLICY)
        else:
            turn = first
        with bind_work_turn(self.memory, turn["id"]):
            for number in range(spent):
                Jobs(self.memory, self.runner).run("A", {"items": [{"id": "already-served-" + str(number),
                    "source_text": {"title": "Unclear offer"}}]})
        self.runner.calls.clear()
        return turn

    def match(self, turn=None, *, examples=None, catalogue=None, aliases=None):
        # Leave the competing grounded B-definition and saved-page E producers
        # real. These unrelated subreviews have no work in the ordering test.
        examples = examples if examples is not None else {"accepted": {}, "failures": {}, "bindings": {}}
        catalogue = catalogue if catalogue is not None else {"outcomes": [], "waiting": {}, "bindings": {}}
        aliases = aliases if aliases is not None else {"outcomes": [], "failures": {}}
        with ExitStack() as stack:
            stack.enter_context(patch("product_radar.functional_example_review.review_functional_examples", return_value=examples))
            stack.enter_context(patch("product_radar.catalogue_scope_review.review_discovery_catalogue_scopes", return_value=catalogue))
            stack.enter_context(patch("product_radar.functional_alias_review.match_functional_aliases", return_value=aliases))
            review = stack.enter_context(patch.object(source_interpretation, "review_source_results",
                                                      wraps=source_interpretation.review_source_results))
            if turn is not None:
                stack.enter_context(bind_work_turn(self.memory, turn["id"]))
            result = programme._match(self.memory, self.record, self.context)
            self.assertEqual(review.call_count, 1)
            self.assertEqual(review.call_args.kwargs["limit"], self.context["match_limit"])
            return result

    def test_even_round_spends_last_original_slot_on_actual_collected_functional_source(self):
        turn = self.turn(even=True, spent=9)
        before = self.memory.snapshot()
        result = self.match(turn)
        self.assertEqual([role for role, _ in self.runner.calls], ["E"])
        after = self.memory.snapshot()
        self.assertEqual(after["batch_runs"][turn["id"]]["counters"]["model_executions"], 10)
        self.assertEqual(result["remaining_questions"], 0)
        self.assertTrue(validated_scoped_evidence(after, self.cid, as_of=self.now))
        wait = result["functional_definitions"]["preparation_wait"]
        self.assertEqual(wait["reason"], "WORK_TURN_MODEL_LIMIT")
        self.assertEqual(wait["concept_ids"], [self.other_cid])
        for table in ("captures", "observations", "query_tasks", "assessments", "issue_reservations", "published_history"):
            self.assertEqual(after[table], before[table], table)
        self.assertEqual(after["batch_runs"][turn["id"]]["policy"], before["batch_runs"][turn["id"]]["policy"])
        self.assertEqual(len(after["jobs"]), len(before["jobs"]) + 1)

    def test_odd_round_preserves_first_claim_for_actual_grounded_B_definition(self):
        turn = self.turn(even=False, spent=9)
        result = self.match(turn)
        self.assertEqual([role for role, _ in self.runner.calls], ["B"])
        self.assertTrue(effective_functional_definition(self.memory.snapshot(), self.other_cid)["accepted"])
        self.assertEqual(result["remaining_questions"], 1)
        self.assertIn("WORK_TURN_MODEL_LIMIT", {row.get("reason") for row in result["outcomes"]})
        self.assertEqual(self.memory.snapshot()["batch_runs"][turn["id"]]["counters"]["model_executions"], 10)

    def test_B_preparation_sees_the_slot_already_used_by_source_first(self):
        turn = self.turn(even=True, spent=9)
        original = programme._model_preparation_scope
        observed = []
        def checked(memory, state, concept_ids, **options):
            if options["purpose"] == "B":
                observed.append(state["batch_runs"][turn["id"]]["counters"]["model_executions"])
                self.assertEqual(observed[-1], 10, "B preparation must not see the pre-E allowance.")
            return original(memory, state, concept_ids, **options)
        with patch.object(programme, "_model_preparation_scope", side_effect=checked):
            self.match(turn)
        self.assertTrue(observed)

    def test_even_round_with_deferred_functional_question_keeps_B_first_and_the_saved_wait(self):
        question = source_interpretation._questions(self.memory.snapshot(), run_id=self.record["source_run_id"],
                                                    as_of=self.now)[0]
        target = question["task_id"] + ":" + self.cid
        programme._defer(self.record, "match", target, digest(question), "SYNTHETIC_ORIGINAL_SOURCE_WAIT", self.context)
        original = deepcopy(self.record["deferrals"]["match:" + target])
        turn = self.turn(even=True, spent=9)
        self.match(turn)
        self.assertEqual([role for role, _ in self.runner.calls], ["B"])
        self.assertEqual(self.record["deferrals"]["match:" + target], original)
        self.assertFalse(validated_scoped_evidence(self.memory.snapshot(), self.cid, as_of=self.now))

    def test_even_round_without_unfinished_functional_source_work_keeps_B_first(self):
        source_interpretation.review_source_results(self.memory, self.runner, run_id=self.record["source_run_id"],
                                                    as_of=self.now)
        turn = self.turn(even=True, spent=9)
        result = self.match(turn)
        self.assertEqual([role for role, _ in self.runner.calls], ["B"])
        self.assertEqual(result["remaining_questions"], 0)

    def test_future_recapture_does_not_bypass_the_saved_wait_for_the_actual_cutoff(self):
        state = self.memory.snapshot()
        question = source_interpretation._questions(state, run_id=self.record["source_run_id"], as_of=self.now)[0]
        target = question["task_id"] + ":" + self.cid
        programme._defer(self.record, "match", target, digest(question), "SYNTHETIC_ORIGINAL_SOURCE_WAIT", self.context)
        original_wait = deepcopy(self.record["deferrals"]["match:" + target])
        values = [dict(amazon_row(), title="Draining sink caddy holder", asin="B012345670",
                       link="https://www.amazon.in/dp/B012345670")]
        import_capture(self.memory, source="amazon", acquisition_id="future-original-recapture",
            observed_at=(timestamp(self.now) + timedelta(days=1)).isoformat(),
            mapped=map_research_rows(values, request()), capture_hash=digest(values),
            reference={"fixture": "synthetic", "route": "amazon-bestsellers", "request": state["query_tasks"][self.task_id]["request"]})
        turn = self.turn(even=True)
        result = self.match(turn)
        self.assertEqual([role for role, _ in self.runner.calls], ["B"])
        self.assertEqual(result["remaining_questions"], 1)
        self.assertEqual(self.record["deferrals"]["match:" + target], original_wait)
        self.assertFalse(validated_scoped_evidence(self.memory.snapshot(), self.cid, as_of=self.now))

    def test_legacy_entry_without_work_turn_still_runs_its_existing_source_review_once(self):
        before = self.memory.snapshot()
        result = self.match()
        self.assertEqual([role for role, _ in self.runner.calls], ["E"])
        self.assertEqual(result["remaining_questions"], 0)
        self.assertNotIn("functional_definitions", result)
        self.assertEqual(self.memory.snapshot()["query_tasks"], before["query_tasks"])

    def test_spent_even_turn_creates_no_fresh_execution_or_quota_reset(self):
        turn = self.turn(even=True, spent=10)
        before = self.memory.snapshot()
        result = self.match(turn)
        after = self.memory.snapshot()
        self.assertEqual(self.runner.calls, [])
        self.assertEqual(result["remaining_questions"], 1)
        self.assertEqual(after["jobs"], before["jobs"])
        for field in ("counters", "reservations", "policy", "policy_hash", "state", "programme_turn_sequence"):
            self.assertEqual(after["batch_runs"][turn["id"]][field], before["batch_runs"][turn["id"]][field], field)

    def test_spent_even_turn_applies_the_exact_saved_source_answer_without_another_call(self):
        from product_radar import research_scope
        original_accept = research_scope.accept_scoped_evidence
        def failed_local_support(*args, **kwargs):
            if kwargs["method"] == source_interpretation.FUNCTIONAL_SUPPORT_METHOD:
                raise OSError("Synthetic saved answer awaits local support application")
            return original_accept(*args, **kwargs)
        with patch.object(research_scope, "accept_scoped_evidence", side_effect=failed_local_support):
            first = source_interpretation.review_source_results(self.memory, self.runner,
                run_id=self.record["source_run_id"], as_of=self.now)
        self.assertEqual(first["remaining_questions"], 1)
        turn = self.turn(even=True, spent=10)
        before = self.memory.snapshot()
        result = self.match(turn)
        after = self.memory.snapshot()
        self.assertEqual(result["remaining_questions"], 0)
        self.assertEqual(self.runner.calls, [])
        self.assertTrue(validated_scoped_evidence(after, self.cid, as_of=self.now))
        self.assertEqual(after["jobs"], before["jobs"])
        self.assertEqual(after["batch_runs"][turn["id"]]["counters"], before["batch_runs"][turn["id"]]["counters"])

    def test_source_first_retains_all_B_subreview_outputs_and_deferrals(self):
        turn = self.turn(even=True)
        examples = {"accepted": {}, "failures": {self.cid: {"reason": "FUNCTIONAL_EXAMPLE_AMBIGUOUS"}},
                    "bindings": {self.cid: "a" * 64}}
        catalogue = {"outcomes": [], "waiting": {"catalogue-original": {"reason": "CATALOGUE_ORIGINAL_WAIT"}},
                     "bindings": {"catalogue-original": "b" * 64}}
        aliases = {"outcomes": [{"decision": "waiting", "group_id": "alias-original", "input_hash": "c" * 64,
                                 "reason": "FUNCTIONAL_ALIAS_REVIEW_PENDING"}], "failures": {}}
        result = self.match(turn, examples=examples, catalogue=catalogue, aliases=aliases)
        self.assertEqual([role for role, _ in self.runner.calls], ["E", "B"])
        self.assertEqual(result["functional_examples"], examples)
        self.assertEqual(result["catalogue_scopes"], catalogue)
        self.assertEqual(result["functional_aliases"], aliases)
        for target, binding in (("functional_example:" + self.cid, "a" * 64),
                                ("catalogue_scope:catalogue-original", "b" * 64),
                                ("functional_alias:alias-original", "c" * 64)):
            self.assertEqual(self.record["deferrals"][target]["binding"], binding)

    def test_source_first_provider_wait_retains_pause_and_does_not_launch_B(self):
        turn = self.turn(even=True)
        self.runner.source = Mock(side_effect=SafetyError("AI_RATE_LIMITED"))
        self.context["runner"] = programme._ProgrammeRunner(self.runner, self.record, self.context)
        result = self.match(turn)
        self.assertEqual([role for role, _ in self.runner.calls], ["E"])
        self.assertEqual(self.record["model_pause"]["reason"], "AI_RATE_LIMITED")
        self.assertEqual(result["remaining_questions"], 1)
        self.assertIn("AI_RATE_LIMITED", {row.get("reason") for row in result["outcomes"]})
        self.assertFalse(effective_functional_definition(self.memory.snapshot(), self.other_cid)["accepted"])

    def test_source_first_preserves_owner_interruption_and_does_not_proceed_to_B(self):
        turn = self.turn(even=True)
        self.runner.source = Mock(side_effect=KeyboardInterrupt("Synthetic owner interruption"))
        with self.assertRaises(KeyboardInterrupt):
            self.match(turn)
        self.assertEqual([role for role, _ in self.runner.calls], ["E"])
        self.assertEqual(self.memory.snapshot()["batch_runs"][turn["id"]]["state"], "open")


if __name__ == "__main__":
    unittest.main()
