"""Priority travels through selection, files and delivery without AI choosing it."""

from copy import deepcopy
import unittest

from test_fit_pipeline import current_fixture, selection
from product_radar.delivery_package import _workbook
from product_radar.live_outputs import _workbook_bytes, validate_dataset
from product_radar.models import SafetyError, digest
from product_radar.opportunity_policy import CURRENT_OPPORTUNITY_POLICY_ID, OPPORTUNITY_FIELDS, OPPORTUNITY_POLICY_ID
from product_radar.report_source_facts import FIELDS as SOURCE_ROUTE_FIELDS
from product_radar.service import issue_fit_policy, product_fit


class OpportunityDeliveryTests(unittest.TestCase):
    def test_priority_is_frozen_and_workbook_roundtrips_for_both_fit_levels(self):
        for matches in (5, 6):
            _, issue, dataset = selection(current_fixture(matches=matches))
            self.assertEqual(issue["opportunity_policy_id"], CURRENT_OPPORTUNITY_POLICY_ID)
            self.assertEqual(dataset["opportunity_policy_id"], CURRENT_OPPORTUNITY_POLICY_ID)
            for product, frozen in zip(dataset["products"], issue["products"]):
                self.assertTrue(all(product[field] == frozen[field] for field in OPPORTUNITY_FIELDS))
                self.assertIn(product["opportunity_priority"], (2, 4) if matches == 5 else (1, 3))
            _workbook(_workbook_bytes(issue, dataset), dataset)

    def test_modified_priority_or_source_count_is_not_a_presentation_change(self):
        _, issue, original = selection(current_fixture())
        for field, value in (("opportunity_priority", 4), ("opportunity_basis", {"available_families": 1, "positive_families": 1})):
            changed = deepcopy(original); changed["products"][0][field] = value
            with self.assertRaises(SafetyError):
                validate_dataset(issue, changed)

    def test_header_cannot_hide_a_mixed_policy_or_unmarked_product(self):
        _, issue, _ = selection(current_fixture())
        for field in OPPORTUNITY_FIELDS:
            changed = deepcopy(issue); changed["products"][0].pop(field)
            with self.assertRaises(SafetyError):
                issue_fit_policy(changed)

    def test_boolean_source_counts_are_not_integers(self):
        _, _, dataset = selection(current_fixture())
        changed = deepcopy(dataset["products"][0])
        changed["opportunity_basis"]["available_sources"] = True
        with self.assertRaises(SafetyError):
            product_fit(changed)

    def old_three_group_packet(self, *, keep_current_markers=False):
        """Construct old facts, not an invalid downgrade of current fields."""
        _, issue, dataset = selection(current_fixture())
        issue["opportunity_policy_id"] = dataset["opportunity_policy_id"] = OPPORTUNITY_POLICY_ID
        for records in (issue["products"], dataset["products"]):
            for product in records:
                product["opportunity_policy_id"] = OPPORTUNITY_POLICY_ID
                product["opportunity_priority"] = 1
                product["opportunity_basis"] = {"available_families": 2, "positive_families": 1}
                if not keep_current_markers:
                    for field in (*SOURCE_ROUTE_FIELDS, "supplier_terms_display_version"):
                        product.pop(field, None)
        issue["selection_hash"] = digest({field: issue[field] for field in (
            "issue_id", "client_id", "revision", "date", "products", "evidence_fingerprint")})
        dataset["selection_hash"] = issue["selection_hash"]
        return issue, dataset

    def test_old_three_group_packet_still_uses_its_original_workbook_columns(self):
        issue, dataset = self.old_three_group_packet()
        validate_dataset(issue, dataset)
        _workbook(_workbook_bytes(issue, dataset), dataset)
        self.assertEqual(issue_fit_policy(issue), "fit-10-v1")

    def test_current_supplier_marker_cannot_be_added_to_downgraded_three_group_facts(self):
        issue, dataset = self.old_three_group_packet(keep_current_markers=True)
        with self.assertRaisesRegex(SafetyError, "LIVE_SUPPLIER_TERMS_DISPLAY_VERSION_INVALID"):
            validate_dataset(issue, dataset)


if __name__ == "__main__":
    unittest.main()
