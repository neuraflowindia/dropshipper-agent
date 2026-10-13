"""A rejected marketplace match must not leak into product prices or dates."""

import unittest

import test_market_validation as helpers
from product_radar.research_flow import candidate_summary
from product_radar.market_validation import review_marketplace


class SummaryRelevanceTests(unittest.TestCase):
    setUp = helpers.MarketValidationTests.setUp

    def test_unrelated_listing_does_not_supply_price_or_first_seen(self):
        before = next(p for p in self.summary["products"] if p["id"] == self.caddy)
        helpers.amazon(self.memory, at="2026-10-05T10:00:00+00:00")
        review_marketplace(self.memory, helpers.RelevanceRunner("unrelated"), concept_ids=[self.caddy], as_of=helpers.NOW)
        after = next(p for p in candidate_summary(self.memory, as_of=helpers.NOW)["products"] if p["id"] == self.caddy)
        self.assertEqual(after["prices"], [])
        self.assertEqual(after["first_seen"], before["first_seen"])
        self.assertEqual(after["observations"], before["observations"])
        self.assertFalse(after["market"]["families"]["marketplace"]["available"])

    def test_relevant_listing_supplies_observed_price_with_original_date(self):
        helpers.amazon(self.memory, at="2026-10-05T10:00:00+00:00")
        review_marketplace(self.memory, helpers.RelevanceRunner("relevant_family"), concept_ids=[self.caddy], as_of=helpers.NOW)
        product = next(p for p in candidate_summary(self.memory, as_of=helpers.NOW)["products"] if p["id"] == self.caddy)
        self.assertEqual(len(product["prices"]), 1)
        self.assertEqual(product["prices"][0]["displayed"], "₹249.00")
        self.assertEqual(product["prices"][0]["observed_at"], "2026-10-05T10:00:00+00:00")
        self.assertTrue(product["market"]["families"]["marketplace"]["available"])
