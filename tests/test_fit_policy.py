"""Reviewed ten-point arithmetic and strict legacy boundary fixtures."""

from copy import deepcopy
from pathlib import Path
import sys
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from product_radar.fit_policy import (ACTIVE_CRITERION_IDS, FIT_POLICY_ID, LEGACY_POLICY_ID,
                                      evaluate_fit, ordered_criteria, policy_for, policy_id_for)
from product_radar.models import SafetyError


def criteria(meets=5, negative=0, *, policy_id=FIT_POLICY_ID):
    rows = []
    for index, number in enumerate(policy_for(policy_id)["active_ids"]):
        state = "Meets" if index < meets else "Does not meet" if index < meets + negative else "Unclear"
        rows.append({"id": number, "state": state, "basis": "inferred" if state in ("Meets", "Does not meet") else "not_stated",
                     "references": ["public-feature-observation"] if state in ("Meets", "Does not meet") else [],
                     "reason": "A supplied functional feature supports this judgment." if state in ("Meets", "Does not meet") else
                               "The supplied facts do not settle this point."})
    if policy_id == LEGACY_POLICY_ID:
        rows[2].update(state="Not assessed", basis="not_stated", references=[], reason="Margin is outside scope.")
    return rows


class FitPolicyTests(unittest.TestCase):
    def test_original_ids_and_reviewed_thresholds_cannot_be_reconfigured(self):
        policy = policy_for(FIT_POLICY_ID)
        self.assertEqual(policy["active_ids"], (1, 2, 5, 6, 7, 8, 9, 11, 12, 13))
        self.assertEqual(policy["retired_ids"], (3, 4, 10))
        self.assertEqual((policy["total"], policy["minimum_matches"], policy["good_matches"]), (10, 5, 6))
        policy["minimum_matches"] = 0
        self.assertEqual(policy_for(FIT_POLICY_ID)["minimum_matches"], 5)
        with self.assertRaisesRegex(SafetyError, "FIT_POLICY_UNKNOWN"):
            policy_for("caller-chosen-threshold")

    def test_four_five_six_and_ten_boundaries_use_unknown_not_failure(self):
        for count, tier, eligible in ((4, "below", False), (5, "decent", True), (6, "good", True), (10, "good", True)):
            with self.subTest(count=count):
                outcome = evaluate_fit(criteria(count), FIT_POLICY_ID)
                self.assertEqual((outcome["kpi_matches"], outcome["fit_tier"], outcome["eligible"]), (count, tier, eligible))
                self.assertEqual(outcome["kpi_total"], 10)
                self.assertEqual(outcome["does_not_meet"], 0)
                self.assertEqual(outcome["unknown"], 10 - count)
                self.assertEqual(outcome["reason"], "fit_passes" if eligible else "fit_information_pending")

    def test_supported_negative_can_make_a_below_threshold_result_known(self):
        outcome = evaluate_fit(criteria(4, 6), FIT_POLICY_ID)
        self.assertEqual(outcome["reason"], "below_fit_threshold")
        self.assertEqual(outcome["kpi_matches"] + outcome["does_not_meet"] + outcome["unknown"], 10)

    def test_input_order_is_not_display_order_and_rows_are_not_mutated(self):
        rows = list(reversed(criteria()))
        original = deepcopy(rows)
        self.assertEqual([row["id"] for row in ordered_criteria(rows, FIT_POLICY_ID)], list(ACTIVE_CRITERION_IDS))
        evaluate_fit(rows, FIT_POLICY_ID)
        self.assertEqual(rows, original)

    def test_duplicate_renumbered_missing_and_retired_rows_are_rejected(self):
        cases = [criteria()[:-1], criteria() + [dict(criteria()[0], id=3)],
                 [dict(row, id=index + 1) for index, row in enumerate(criteria())],
                 [criteria()[0], *criteria()[1:-1], criteria()[0]]]
        for rows in cases:
            with self.subTest(ids=[row["id"] for row in rows]):
                with self.assertRaises(SafetyError):
                    evaluate_fit(rows, FIT_POLICY_ID)

    def test_unsupported_negative_must_be_normalised_before_evaluation(self):
        rows = criteria()
        rows[-1].update(state="Does not meet", basis="not_stated", references=[])
        with self.assertRaisesRegex(SafetyError, "FIT_UNSUPPORTED_NEGATIVE"):
            evaluate_fit(rows, FIT_POLICY_ID)
        rows[-1]["state"] = "Unclear"
        self.assertEqual(evaluate_fit(rows, FIT_POLICY_ID)["unknown"], 5)

    def test_positive_requires_source_basis_and_references_not_model_score(self):
        for change in ({"basis": "not_stated"}, {"references": []}, {"references": ["same", "same"]},
                       {"reason": ""}, {"state": "Invented"}, {"id": True}):
            rows = criteria(); rows[0].update(change)
            with self.subTest(change=change):
                with self.assertRaises(SafetyError):
                    evaluate_fit(rows, FIT_POLICY_ID)

    def test_explicit_frozen_state_arithmetic_does_not_weaken_full_assessments(self):
        rows = [{"id": row["id"], "state": row["state"]} for row in criteria()]
        self.assertEqual(evaluate_fit(rows, FIT_POLICY_ID, states_only=True)["fit_tier"], "decent")
        with self.assertRaises(SafetyError):
            evaluate_fit(rows, FIT_POLICY_ID)
        rows[0]["model_total"] = 100
        with self.assertRaises(SafetyError):
            evaluate_fit(rows, FIT_POLICY_ID, states_only=True)

    def test_legacy_reader_requires_a_real_legacy_boundary(self):
        self.assertEqual(policy_id_for({"schema_version": 1}), LEGACY_POLICY_ID)
        self.assertEqual(policy_id_for({}, schema_version=1), LEGACY_POLICY_ID)
        self.assertEqual(policy_id_for({"schema_version": 2, "fit_policy_id": FIT_POLICY_ID}), FIT_POLICY_ID)
        for record in ({}, {"schema_version": 2}, {"schema_version": True}, {"fit_policy_id": "unreviewed"},
                       {"schema_version": 1, "fit_policy_id": None}):
            with self.subTest(record=record):
                with self.assertRaises(SafetyError):
                    policy_id_for(record)

    def test_legacy_seven_point_policy_and_margin_remain_distinct(self):
        rows = criteria(8, policy_id=LEGACY_POLICY_ID)
        outcome = evaluate_fit(rows, LEGACY_POLICY_ID)
        self.assertEqual((outcome["kpi_matches"], outcome["kpi_total"], outcome["fit_tier"]), (7, 13, "good"))
        rows[2].update(state="Meets", basis="inferred", references=["invented-margin"])
        with self.assertRaisesRegex(SafetyError, "FIT_LEGACY_MARGIN_INVALID"):
            evaluate_fit(rows, LEGACY_POLICY_ID)


if __name__ == "__main__":
    unittest.main()
