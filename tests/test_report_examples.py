"""Actual synthetic source records prove projection, not product quality."""

from copy import deepcopy
from pathlib import Path
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from product_radar.functional_types import prepare_example_memberships
from product_radar.models import SafetyError, digest
from product_radar.frozen_records import read_original_json
from product_radar.report_examples import (FIELDS, freeze_functional_examples,
    validate_functional_examples, example_workbook_rows)
import test_functional_types as functional_fixtures
from test_research_flow import NOW


class ReportExampleTests(unittest.TestCase):
    def setUp(self):
        functional_fixtures.FunctionalTypeTests.setUp(self)
        functional_fixtures.FunctionalTypeTests.accept(self)
        prepare_example_memberships(self.memory, self.cid, as_of=NOW)
        self.state = self.memory.snapshot()

    def block(self, state=None, **kwargs):
        return freeze_functional_examples(state or self.state, self.cid, as_of=NOW, **kwargs)

    def test_frozen_actual_records_are_pure_and_read_without_research(self):
        before = deepcopy(self.state)
        with patch.object(self.memory, "commit", side_effect=AssertionError("read only")):
            value = self.block()
        self.assertEqual(before, self.state)
        self.assertTrue(value["example_products"])
        self.assertEqual(set(value), set(FIELDS))
        for row in value["example_products"]:
            self.assertEqual(read_original_json(row["observation"]), self.state["observations"][row["observation_id"]])
            self.assertEqual(row["observation_hash"], row["observation"]["sha256"])
            self.assertIsNone(row["seller"])
        self.assertIs(validate_functional_examples(value, concept_id=self.cid), value)

    def test_no_membership_is_honest_zero_not_a_minimum_gate(self):
        state = deepcopy(self.state)
        state["source_checks"] = {ref: row for ref, row in state["source_checks"].items()
                                  if row.get("kind") != "concept-example-v1"}
        value = self.block(state)
        self.assertEqual(value["example_products"], [])
        self.assertEqual(value["example_bindings"], {})
        self.assertEqual(value["example_coverage"]["selected_offers"], 0)

    def test_latest_global_same_item_blocks_old_matching_title(self):
        state = deepcopy(self.state)
        old_id = self.refs[0]
        old = state["observations"][old_id]
        state["observations"]["obs-new-unreviewed"] = dict(deepcopy(old),
            semantic_id="item-new-meaning", observed_at="2026-10-05T09:00:00+00:00",
            semantic={"title": "Changed unreviewed product"})
        value = self.block(state)
        self.assertNotIn(old_id, [row["observation_id"] for row in value["example_products"]])
        self.assertNotIn("obs-new-unreviewed", [row["observation_id"] for row in value["example_products"]])

    def test_future_recapture_does_not_supply_or_erase_earlier_example(self):
        state = deepcopy(self.state)
        old = state["observations"][self.refs[0]]
        state["observations"]["obs-future"] = dict(deepcopy(old), semantic_id="item-new-meaning",
            observed_at="2026-10-06T09:00:00+00:00", semantic={"title": "Future product"})
        self.assertEqual(self.block(state), self.block())

    def test_future_definition_cannot_be_backdated(self):
        with self.assertRaisesRegex(SafetyError, "DEFINITION_AFTER_CUTOFF"):
            freeze_functional_examples(self.state, self.cid, as_of="2026-10-04T10:00:00+00:00")

    def test_exact_frozen_title_seller_pack_and_price_are_not_independent_claims(self):
        for field, value in (("title", "Invented title"), ("seller", "Invented seller"),
                             ("pack", "Pack of 4"), ("price", {"displayed": "₹1"})):
            with self.subTest(field=field):
                block = self.block()
                block["example_products"][0][field] = value
                with self.assertRaisesRegex(SafetyError, "DISPLAY_CHANGED"):
                    validate_functional_examples(block, concept_id=self.cid)

    def test_foreign_meaning_or_membership_or_original_hash_refused(self):
        for field, value in (("meaning_hash", "0" * 64), ("membership_hash", "0" * 64),
                             ("child_id", "foreign-child"), ("observation_hash", "0" * 64)):
            with self.subTest(field=field):
                block = self.block()
                block["example_products"][0][field] = value
                with self.assertRaises(SafetyError):
                    validate_functional_examples(block, concept_id=self.cid)

    def test_damaged_original_membership_refused(self):
        block = self.block()
        next(iter(block["example_bindings"].values()))["record"]["reason"] = "Changed after freeze"
        with self.assertRaisesRegex(SafetyError, "HASH_MISMATCH"):
            validate_functional_examples(block, concept_id=self.cid)

    def test_changed_original_observation_refused(self):
        block = self.block()
        block["example_products"][0]["observation"]["canonical_json"] += " "
        with self.assertRaisesRegex(SafetyError, "HASH_MISMATCH"):
            validate_functional_examples(block, concept_id=self.cid)

    def test_zero_and_one_examples_allowed_with_exact_coverage(self):
        for limit in (0, 1):
            with self.subTest(limit=limit):
                value = self.block(max_examples=limit)
                self.assertEqual(len(value["example_products"]), limit)
                self.assertEqual(value["example_coverage"]["retained_current_offers"],
                    value["example_coverage"]["selected_offers"] + value["example_coverage"]["omitted_offers"])
                validate_functional_examples(value, concept_id=self.cid)

    def test_bool_limit_and_counter_tamper_refused(self):
        with self.assertRaisesRegex(SafetyError, "LIMIT_INVALID"):
            self.block(max_examples=True)
        block = self.block()
        block["example_coverage"]["selected_offers"] = True
        with self.assertRaisesRegex(SafetyError, "COVERAGE_INVALID"):
            validate_functional_examples(block, concept_id=self.cid)

    def test_replayed_frozen_block_does_not_require_mutable_source_store(self):
        block = self.block()
        self.state.clear()
        validate_functional_examples(block, concept_id=self.cid)

    def test_workbook_rows_keep_exact_source_and_missing_seller(self):
        block = dict(self.block(), concept_id=self.cid, rank=1, title="Draining sink caddy")
        rows = example_workbook_rows(block)
        self.assertEqual(len(rows), len(block["example_products"]))
        self.assertEqual(rows[0][4], "Not stated")
        self.assertEqual(rows[0][-1], block["example_products"][0]["observation_id"])

    def test_unaccepted_legacy_type_cannot_receive_new_packet_marker(self):
        state = deepcopy(self.state)
        state["source_checks"] = {ref: row for ref, row in state["source_checks"].items()
                                  if row.get("kind") != "functional-definition-v1"}
        with self.assertRaisesRegex(SafetyError, "DEFINITION_REQUIRED"):
            self.block(state)

    def test_optional_bad_example_is_explicit_not_entire_product_failure(self):
        with patch("product_radar.report_examples._display", side_effect=SafetyError("REPORT_EXAMPLE_URL_INVALID")):
            value = self.block()
        self.assertEqual(value["example_products"], [])
        self.assertTrue(value["example_coverage"]["rejected_observations"])
        validate_functional_examples(value, concept_id=self.cid)

    def test_unknown_foreign_binding_cannot_hide_outside_displayed_rows(self):
        block = self.block()
        block["example_bindings"]["unselected-member"] = deepcopy(next(iter(block["example_bindings"].values())))
        with self.assertRaisesRegex(SafetyError, "MEMBERSHIP_INVALID"):
            validate_functional_examples(block, concept_id=self.cid)

    def test_damaged_unrelated_observation_does_not_block_this_type(self):
        state = deepcopy(self.state)
        state["observations"]["unrelated-bad"] = {"source": "amazon", "source_item": "unrelated",
                                                   "facts": None, "observed_at": "bad"}
        self.assertEqual(self.block(state), self.block())


if __name__ == "__main__":
    unittest.main()
