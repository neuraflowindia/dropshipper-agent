"""Real-sized synthetic saved pairs, never provider traffic or journal writes."""

from copy import deepcopy
from decimal import Decimal
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from product_radar.models import SafetyError
from product_radar.programme_cost_snapshot import _ledger
from product_radar.programme_summary import _money
from product_radar.research_costs import collection_cost_totals, collection_costs
from test_programme_summary import financial


class ResearchCostTotalsTests(unittest.TestCase):
    def test_existing_selected_cost_rows_and_total_only_reduction_match_exact_money(self):
        pair = financial()
        pair[1]["shared-attempt"]["renewed_reads"] = {
            "read-one": {"read_id": "read-one", "namespace": "original:later", "generation": "later",
                         "charge": "0.003", "cap": "0.02"},
            "read-two": {"read_id": "read-two", "namespace": "original:later", "generation": "later",
                         "charge": None, "cap": "0.01"}}
        for ids in (None, [], ["shared-attempt"], ["failed-attempt"],
                    ["shared-attempt", "failed-attempt", "shared-attempt"]):
            with self.subTest(ids=ids):
                before = deepcopy(pair)
                old = collection_costs(SimpleNamespace(state=lambda: pair), ids)
                totals = collection_cost_totals(pair, ids)
                for field in ("physical_acquisitions", "known_actual_usd", "unsettled_hold_usd", "accounting_complete"):
                    self.assertEqual(totals[field], old[field])
                self.assertNotIn("rows", totals)
                self.assertEqual(pair, before)

    def test_one_hundred_thousand_and_one_original_attempts_do_not_erase_owner_totals(self):
        budgets, original = financial()
        # All entries are genuine mapping rows, not a fabricated length or a
        # mocked reducer result. These are synthetic settled FAILED attempts.
        attempts = {f"synthetic-failed-{number}": dict(original["failed-attempt"], charge="0.00000001")
                    for number in range(100001)}
        pair = budgets, attempts
        with patch("product_radar.programme_summary.collection_costs", side_effect=AssertionError("All-history row expansion")), \
             patch("product_radar.programme_cost_snapshot.collection_costs", side_effect=AssertionError("All-history row expansion")):
            cost = _ledger(pair)
            money = _money(pair, None)
        self.assertEqual(cost["state"], "known")
        self.assertEqual(cost["physical_acquisitions"], 100001)
        self.assertEqual(cost["acquisitions_by_source"], {"meta": 100001})
        self.assertEqual(Decimal(cost["known_incurred_usd"]), Decimal("0.00100001"))
        self.assertEqual(cost["unsettled_hold_usd"], "0")
        self.assertTrue(cost["accounting_complete"])
        self.assertEqual(money["ledger_physical_acquisitions"], 100001)
        self.assertEqual(Decimal(money["ledger_known_incurred_usd"]), Decimal("0.00100001"))
        self.assertTrue(money["ledger_accounting_complete"])
        selected = collection_costs(SimpleNamespace(state=lambda: pair), ["synthetic-failed-100000"])
        self.assertEqual(selected["physical_acquisitions"], 1)
        self.assertEqual(Decimal(selected["known_actual_usd"]), Decimal("0.00000001"))

    def test_one_hundred_thousand_and_one_distributed_renewals_preserve_actuals_and_holds(self):
        budgets, original = financial()
        attempts, remaining, offset = {}, 100001, 0
        namespace = next(iter(budgets))
        while remaining:
            size = min(101, remaining)
            row = dict(original["failed-attempt"], charge="0.00000001")
            row["renewed_reads"] = {}
            for number in range(offset, offset + size):
                rid = "synthetic-read-" + str(number)
                row["renewed_reads"][rid] = {"read_id": rid, "namespace": namespace,
                    "generation": "synthetic-later-generation", "charge": None if number == 100000 else "0.00000001",
                    "cap": "0.02"}
            attempts["synthetic-original-" + str(offset)] = row
            offset += size; remaining -= size
        pair = budgets, attempts
        totals = collection_cost_totals(pair)
        self.assertEqual(totals["renewed_read_operations_count"], 100001)
        self.assertEqual(totals["physical_acquisitions"], 991)
        self.assertEqual(Decimal(totals["known_actual_usd"]), Decimal("0.00100991"))
        self.assertEqual(Decimal(totals["unsettled_hold_usd"]), Decimal("0.02"))
        self.assertFalse(totals["accounting_complete"])
        cost = _ledger(pair); money = _money(pair, None)
        self.assertEqual(cost["state"], "known")
        self.assertEqual(Decimal(cost["known_incurred_usd"]), Decimal("0.00100991"))
        self.assertEqual(Decimal(money["ledger_unsettled_hold_usd"]), Decimal("0.02"))
        self.assertFalse(money["ledger_accounting_complete"])

    def test_unknown_actor_read_and_renewal_charges_never_become_zero_or_double_count(self):
        pair = financial(); row = pair[1]["failed-attempt"]
        row.update(charge=None, read_charge=None, read_hold="0.004")
        row["renewed_reads"] = {"later-read": {"read_id": "later-read", "namespace": "saved:later",
            "generation": "later", "charge": None, "cap": "0.02"}}
        totals = collection_cost_totals(pair, ["failed-attempt", "failed-attempt"])
        self.assertEqual(totals["physical_acquisitions"], 1)
        self.assertEqual(totals["known_actual_usd"], "0")
        self.assertEqual(Decimal(totals["unsettled_hold_usd"]), Decimal("0.224"))
        self.assertFalse(totals["accounting_complete"])

    def test_source_mapping_and_individual_money_validation_are_not_relaxed(self):
        pair = financial(); pair[1]["failed-attempt"]["stage"] = "correction"
        self.assertEqual(_ledger(pair)["acquisitions_by_source"], {"meta": 1, "other": 1})
        pair[1]["failed-attempt"]["charge"] = "-0.01"
        with self.assertRaisesRegex(SafetyError, "INVALID_USD"):
            collection_cost_totals(pair)
        with self.assertRaisesRegex(SafetyError, "COST_ATTEMPT_NOT_SAVED"):
            collection_cost_totals(financial(), ["missing-original"])


if __name__ == "__main__":
    unittest.main()
