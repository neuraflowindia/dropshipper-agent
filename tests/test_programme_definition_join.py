"""The actual cooperative matching step creates reusable functional meanings."""

from copy import deepcopy
import unittest
from unittest.mock import patch

import test_functional_definition_review as fixtures
from product_radar.functional_types import effective_functional_definition, prepare_example_memberships
from product_radar.functional_definition_review import review_functional_definitions
from product_radar.frozen_records import read_original_json
from product_radar.models import SafetyError
from product_radar.programme import _match, prepare_programme
from product_radar.report_examples import freeze_functional_examples, validate_functional_examples
from product_radar.work_turns import bind_work_turn, open_work_turn


class ProgrammeDefinitionJoinTests(unittest.TestCase):
    def setUp(self):
        fixture = fixtures.FunctionalDefinitionReviewTests(); fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        self.fixture, self.memory = fixture, fixture.memory
        self.cid = fixture.cids[0]
        self.record = prepare_programme(self.memory, "definition-join", source_run_id="definition-source",
            concept_ids=[self.cid], as_of=fixtures.NOW)
        self.runner = fixtures.Runner()
        self.context = {"at": fixtures.NOW, "runner": self.runner, "match_limit": 5, "retry_seconds": 3600}

    def match(self):
        turn = open_work_turn(self.memory, self.record["programme_id"], at=fixtures.NOW)
        with bind_work_turn(self.memory, turn["id"]):
            return _match(self.memory, self.record, self.context)

    def test_actual_matching_step_accepts_definition_and_reuses_original_without_second_call(self):
        before = self.memory.snapshot()
        result = self.match()
        self.assertIn(self.cid, result["functional_definitions"]["accepted"])
        self.assertEqual(result["functional_definitions"]["failures"], {})
        self.assertEqual(len(self.runner.calls), 1)
        meaning = effective_functional_definition(self.memory.snapshot(), self.cid)
        self.assertTrue(meaning["accepted"])
        for table in ("concepts", "associations", "assessments", "observations", "captures", "published_history"):
            self.assertEqual(before[table], self.memory.snapshot()[table], table)
        repeated = self.match()
        self.assertIn(self.cid, repeated["functional_definitions"]["accepted"])
        self.assertEqual(len(self.runner.calls), 1)

    def test_matching_pass_shares_one_state_copy_until_a_save_with_identical_results(self):
        import product_radar.programme as programme
        from product_radar.research_memory import UnchangedSnapshot

        class EveryRead(UnchangedSnapshot):
            def __call__(self):  # the earlier behaviour: a fresh full copy at every read
                return self.memory.snapshot()

        self.match()  # accepts the definition: the pass below has nothing new to save
        results, copies = {}, {}
        for name, reader in (("every_read", EveryRead), ("shared", UnchangedSnapshot)):
            before = self.memory.snapshot_with_provenance()
            with patch.object(programme, "UnchangedSnapshot", reader), \
                    patch.object(self.memory, "snapshot", wraps=self.memory.snapshot) as snapshot:
                results[name] = self.match()
            copies[name] = snapshot.call_count
            # Opening the already open round saves nothing either.
            self.assertEqual(self.memory.snapshot_with_provenance()["head"], before["head"])
        self.assertEqual(results["shared"], results["every_read"])
        self.assertLess(copies["shared"], copies["every_read"])
        self.assertEqual(len(self.runner.calls), 1)

    def test_a_save_inside_the_matching_pass_is_read_by_the_reviews_after_it(self):
        read = []
        original = review_functional_definitions
        def definitions(memory, *args, read_state, **options):
            result = original(memory, *args, read_state=read_state, **options)
            read.append(read_state())
            return result
        with patch("product_radar.functional_definition_review.review_functional_definitions", side_effect=definitions):
            self.match()
        # The definition saved inside the pass is in the copy the later reviews read.
        self.assertTrue(effective_functional_definition(read[0], self.cid)["accepted"])

    def test_legacy_matching_without_explicit_turn_does_not_activate_new_definition_writer(self):
        _match(self.memory, self.record, self.context)
        self.assertEqual(self.runner.calls, [])
        self.assertFalse(effective_functional_definition(self.memory.snapshot(), self.cid)["accepted"])

    def test_matching_passes_the_actual_retry_interval_and_model_deadline(self):
        deadline = "2026-10-05T10:30:00+00:00"
        self.record["model_pause"] = {"reason": "AI_RATE_LIMITED", "at": fixtures.NOW,
                                     "next_retry_at": deadline}
        # The normal dispatcher supplies no live runner during this pause.
        # Check the join itself while retaining real saved-only preparation.
        self.context["runner"] = None
        with patch("product_radar.functional_definition_review.review_functional_definitions",
                   wraps=review_functional_definitions) as review:
            self.match()
        self.assertEqual(review.call_args.kwargs["retry_seconds"], 3600)
        self.assertEqual(review.call_args.kwargs["retry_not_before"], deadline)
        self.assertEqual(self.runner.calls, [])

    def test_real_model_definition_packet_preserves_exact_original_large_integers(self):
        self.match()
        prepare_example_memberships(self.memory, self.cid, as_of=fixtures.NOW)
        state = self.memory.snapshot()
        block = freeze_functional_examples(state, self.cid, as_of=fixtures.NOW)
        wrapper = block["functional_definition_proof"]["record"]
        self.assertEqual(wrapper["contract"], "frozen-original-json-v1")
        record = read_original_json(wrapper)
        meaning = effective_functional_definition(state, self.cid)
        self.assertEqual(record, state["source_checks"][meaning["definition_id"]])
        fingerprint = record["job_proof"]["definition_acceptance"]["result_fingerprint"]
        self.assertTrue(any(number > 2 ** 53 for number in fingerprint))
        validate_functional_examples(block, concept_id=self.cid)
        damaged = deepcopy(block)
        damaged["functional_definition_proof"]["record"] = record
        with self.assertRaisesRegex(SafetyError, "DEFINITION_INVALID"):
            validate_functional_examples(damaged, concept_id=self.cid)

    def test_unchanged_unclear_wait_does_not_move_the_saved_retry_time(self):
        self.runner = fixtures.Runner(decision="unclear")
        self.context["runner"] = self.runner
        first = self.match()
        self.assertEqual(first["functional_definitions"]["failures"][self.cid]["reason"],
                         "FUNCTIONAL_DEFINITION_UNCLEAR")
        # The first follow-up records the newly saved original's stable input.
        # Later identical ticks must not push its existing deadline forward.
        self.context["at"] = "2026-10-05T10:00:10+00:00"
        self.match()
        saved = deepcopy(self.record["deferrals"]["functional_definition:" + self.cid])
        self.context["at"] = "2026-10-05T10:00:20+00:00"
        self.match()
        self.assertEqual(self.record["deferrals"]["functional_definition:" + self.cid], saved)
        self.assertEqual(len(self.runner.calls), 1)


if __name__ == "__main__":
    unittest.main()
