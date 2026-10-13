"""New seller examples reach real matching, memberships and frozen report facts."""

from copy import deepcopy
import unittest
from unittest.mock import patch

import test_functional_example_review as fixtures
from product_radar.frozen_records import read_original_json
from product_radar.functional_types import current_example_memberships
from product_radar.models import SafetyError, canonical
from product_radar.programme import _match, _profiles, prepare_programme
from product_radar.report_examples import (example_block_fields, freeze_functional_examples,
    validate_functional_examples)
from product_radar.work_turns import bind_work_turn, close_work_turn, open_work_turn


class ProgrammeExampleReviewJoinTests(unittest.TestCase):
    def setUp(self):
        fixture = fixtures.FunctionalExampleReviewTests(); fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        self.fixture, self.memory, self.cid = fixture, fixture.memory, fixture.cid
        self.record = prepare_programme(self.memory, "example-normal-caller", source_run_id="example-source",
            concept_ids=[self.cid], as_of=fixtures.NOW)
        self.runner = fixtures.Runner()
        self.context = {"at": fixtures.NOW, "runner": self.runner, "match_limit": 5,
            "profile_limit": 5, "retry_seconds": 3600}

    def review_and_prepare(self):
        turn = open_work_turn(self.memory, self.record["programme_id"], at=fixtures.NOW)
        try:
            with bind_work_turn(self.memory, turn["id"]):
                result = _match(self.memory, self.record, self.context)
                _profiles(self.memory, self.record, dict(self.context, runner=None))
        finally:
            close_work_turn(self.memory, turn["id"], at=fixtures.NOW, reason="Synthetic real example caller completed")
        return result

    def block(self):
        return freeze_functional_examples(self.memory.snapshot(), self.cid, as_of=fixtures.NOW)

    def test_actual_match_and_profile_stage_review_new_examples_without_redefining(self):
        original = deepcopy(self.memory.snapshot()["source_checks"][self.fixture.meaning["definition_id"]])
        result = self.review_and_prepare()
        self.assertIn(self.cid, result["functional_examples"]["accepted"])
        members = current_example_memberships(self.memory.snapshot(), self.cid)
        self.assertEqual({m["child_id"] for m in members}, set(self.fixture.children))
        self.assertEqual(self.memory.snapshot()["source_checks"][self.fixture.meaning["definition_id"]], original)
        self.assertEqual(len(self.runner.calls), 1)
        self.review_and_prepare()
        self.assertEqual(len(self.runner.calls), 1)

    def test_one_original_review_is_frozen_once_for_multiple_seller_examples(self):
        self.review_and_prepare(); block = self.block()
        self.assertGreater(len(block["example_bindings"]), 1)
        self.assertEqual(len(block["example_review_proofs"]), 1)
        for rid, wrapper in block["example_review_proofs"].items():
            self.assertEqual(read_original_json(wrapper), self.memory.snapshot()["source_checks"][rid])
        validate_functional_examples(block, concept_id=self.cid)
        bad = deepcopy(block); bad.pop("example_review_proofs")
        with self.assertRaises(SafetyError): validate_functional_examples(bad, concept_id=self.cid)
        bad = deepcopy(block)
        bad["example_review_proofs"]["unreferenced-review"] = next(iter(bad["example_review_proofs"].values()))
        with self.assertRaises(SafetyError): validate_functional_examples(bad, concept_id=self.cid)

    def test_small_byte_budget_keeps_type_and_explains_omitted_examples(self):
        self.review_and_prepare()
        empty = freeze_functional_examples(self.memory.snapshot(), self.cid, as_of=fixtures.NOW, max_examples=0)
        budget = len(canonical({name: empty[name] for name in example_block_fields(empty)})) + 256
        with patch("product_radar.report_examples.MAX_BLOCK_BYTES", budget):
            block = self.block()
            self.assertEqual(block["example_products"], [])
            self.assertNotIn("example_review_proofs", block)
            self.assertEqual(block["example_selection"]["byte_limit"], budget)
            self.assertGreater(block["example_selection"]["omitted_for_byte_limit"], 0)
            validate_functional_examples(block, concept_id=self.cid)
        self.assertEqual(block["functional_definition"]["concept_id"], self.cid)


if __name__ == "__main__":
    unittest.main()
