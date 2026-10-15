"""Which saved market facts earn opportunity-score points, through the real research joins.

Fictional data, no provider calls. Amazon and Flipkart listings are the
crowding check, never want points; quick-commerce stock counts per city;
Google Trends charts count by their measured nonzero points and rise.
"""

from copy import deepcopy
import unittest

from product_radar.discovery import compute_market_signals
from product_radar.fit_policy import FIT_POLICY_ID, evaluate_fit
from product_radar.issue_allocation import allocate_issues
from product_radar.market_validation import REVIEW_VERSION
from product_radar.models import digest
from product_radar.opportunity_score import (POLICY_ID, evaluate_opportunity, market_evidence)
from product_radar.providers.trends_browser import parse_trends_csv
from product_radar.report_narration import SCORE_RULE, make_narrative_input
from product_radar.report_selection import build_report_pool
from product_radar.research_flow import candidate_summary
from product_radar.research_operations import missing_market_checks, validation_target_result, validation_targets
from product_radar.service import policy_for_fit
from test_fit_pipeline import current_fixture, older_format_scores, selection
from test_fit_policy import criteria
from test_report_selection import NOW
from test_trends_browser import metadata, weekly

LOW = {"level": "low", "rule": "few_listings", "thresholds_version": "crowding-thresholds-v1", "counts": None, "evidence_ids": []}


def expire_ad(memory, n=0):
    # Older observed product features still support fit; they do not keep an
    # expired advert in the current demand sample.
    memory.state["observations"][f"meta{n:03d}"]["observed_at"] = "2026-09-01T10:00:00+00:00"


def context(memory, source, *, concept_id="c000", suffix="one", accepted=True, at=NOW, available=None, city=None):
    obs_id = source + "-context-" + suffix
    description = memory.state["concepts"][concept_id]["description"]
    facts = {"list_type": "product_search" if source in ("amazon", "flipkart") else "quickcommerce_context",
             "url": "https://example.org/fixture/" + source, "limitations": [],
             "price": {"displayed": "₹149", "currency": "INR"}}
    if available is not None:
        facts["available"] = available
    if city is not None:
        facts["location"] = {"name": city}
    row = {"source": source, "source_item": source + "-item-" + suffix,
           "acquisition_id": source + "-capture-" + suffix, "observed_at": at,
           "semantic_id": source + "-semantic-" + suffix, "semantic": {"title": description["label"]}, "facts": facts}
    memory.state["observations"][obs_id] = row
    if accepted:
        memory.state["market_reviews"]["context-review-" + obs_id] = {
            "concept_id": concept_id, "description_hash": digest(description), "review_version": REVIEW_VERSION,
            "reviewed_at": NOW, "input_hash": digest(row), "prompt_version": "research-e-source-relevance-v2",
            "relevance": [{"id": obs_id, "relationship": "relevant_family", "reason": "Fictional accepted family match."}]}
    return obs_id


def want(product, item):
    return product["opportunity"]["opportunity_basis"]["parts"]["want"]["items"][item]


class MarketFactJoinTests(unittest.TestCase):
    def summary(self, memory):
        return candidate_summary(memory, as_of=NOW)["products"][0]

    def test_amazon_and_flipkart_listings_earn_no_want_points(self):
        memory = current_fixture(1); expire_ad(memory); memory.state["market_reviews"].clear()
        amazon_id = context(memory, "amazon"); flipkart_id = context(memory, "flipkart")
        before = deepcopy(memory.state)
        product = self.summary(memory)
        self.assertTrue(product["market"]["sources"]["amazon"]["available"])
        self.assertTrue(product["market"]["sources"]["flipkart"]["available"])
        self.assertEqual(product["opportunity"]["opportunity_basis"]["parts"]["want"]["points"], 0)
        self.assertEqual(product["opportunity"]["opportunity_label"], "too_quiet")
        facts = build_report_pool(memory, NOW, fit_policy_id=FIT_POLICY_ID)["facts_by_concept"]["c000"]
        self.assertTrue({amazon_id, flipkart_id} <= {ref["id"] for ref in facts["source_refs"]})
        self.assertIn("2 sources", facts["market_label"])
        self.assertTrue(all("not sales" in check["detail"] for check in facts["market_checks"] if check.get("context_only")))
        self.assertEqual(memory.state, before)

    def test_quickcommerce_stock_counts_cities_not_apps_or_listings(self):
        memory = current_fixture(1); expire_ad(memory); memory.state["market_reviews"].clear()
        context(memory, "blinkit", suffix="a", available=True, city="Mumbai")
        context(memory, "zepto", suffix="b", available=True, city="Mumbai")
        context(memory, "instamart", suffix="c", available=False, city="Bengaluru")
        context(memory, "blinkit", suffix="d", available=None, city="Delhi NCR")
        product = self.summary(memory)
        stock = want(product, "quick_commerce")
        self.assertEqual(stock["facts"], {"stock_cities": {"Mumbai": ["blinkit-context-a", "zepto-context-b"]}})
        self.assertEqual(stock["points"], 1)
        context(memory, "instamart", suffix="e", available=True, city="Bengaluru")
        self.assertEqual(want(self.summary(memory), "quick_commerce")["points"], 2)

    def test_blank_location_names_are_ignored_not_fatal(self):
        memory = current_fixture(1); expire_ad(memory); memory.state["market_reviews"].clear()
        context(memory, "blinkit", suffix="a", available=True, city="")
        context(memory, "zepto", suffix="b", available=True, city=" \t")
        context(memory, "instamart", suffix="c", available=True, city="Pune")
        product = self.summary(memory)  # an empty name once crashed the whole summary
        self.assertEqual(product["market"]["sources"]["blinkit"]["stock_locations"], {})
        self.assertEqual(want(product, "quick_commerce")["facts"], {"stock_cities": {"Pune": ["instamart-context-c"]}})
        self.assertEqual(want(product, "quick_commerce")["points"], 1)

    def test_unreviewed_stale_unrelated_and_changed_scope_contexts_do_not_count(self):
        for change in ("unreviewed", "stale", "unrelated", "changed-description"):
            with self.subTest(change=change):
                memory = current_fixture(1); expire_ad(memory); memory.state["market_reviews"].clear()
                obs_id = context(memory, "blinkit", accepted=change != "unreviewed", available=True, city="Mumbai",
                                 at="2026-09-01T10:00:00+00:00" if change == "stale" else NOW)
                record = memory.state["market_reviews"].get("context-review-" + obs_id)
                if change == "unrelated": record["relevance"][0]["relationship"] = "unrelated"
                if change == "changed-description": record["description_hash"] = "0" * 64
                product = self.summary(memory)
                self.assertEqual(want(product, "quick_commerce")["facts"], {"stock_cities": {}})
                self.assertFalse(product["market"]["sources"]["blinkit"]["available"])

    def test_zero_flat_and_rising_full_charts_are_measured_not_judged(self):
        fit = evaluate_fit(criteria(6), FIT_POLICY_ID)
        for values, searches, rising in (([0] * 54, 0, 0), ([20] * 54, 5, 0),
                                         ([10] * 46 + [40] * 8, 5, 8),     # rises by the contract's rule (40 vs 32.5)
                                         ([10] * 49 + [40] * 5, 5, 12)):   # lower ratio bound 4.0: rising by 50%+
            parsed = parse_trends_csv(weekly(values), metadata(collected_at=NOW))
            market = compute_market_signals({}, {}, NOW, [{"id": "timeline-one", "parsed": parsed}])
            source = market["sources"]["google-trends"]
            with self.subTest(values=values[-1]):
                self.assertTrue(source["available"])
                self.assertEqual(source["used_ids"], ["timeline-one"])
                record = evaluate_opportunity(fit, market, presence=None, crowding=LOW)
                parts = record["opportunity_basis"]["parts"]
                self.assertEqual(parts["want"]["items"]["google_searches"]["points"], searches)
                self.assertEqual(parts["early"]["items"]["rising_searches"]["points"], rising)
                # A usable chart with 4+ nonzero points alone gives the 5 want points needed.
                self.assertEqual(record["reason"], "opportunity_scored" if searches else "demand_too_quiet")

    def test_missing_market_checks_name_only_sites_that_can_add_points(self):
        memory = current_fixture(1)
        product = self.summary(memory)
        self.assertEqual(market_evidence(product["opportunity"]), {"meta": ["meta000"]})
        task = missing_market_checks({"products": [product]}, as_of=NOW)[0]
        # Amazon and Flipkart are the free crowding check, not a market question.
        self.assertEqual([row["source"] for row in task["choices"]], ["google-trends", "blinkit", "zepto", "instamart"])
        self.assertEqual((task["needed_sources"], task["positive_signal_needed"]), (0, False))
        self.assertTrue(task["blocking"])  # waiting for the crowding check (no accepted meaning here)
        self.assertEqual(task["opportunity_policy_id"], POLICY_ID)
        self.assertEqual(task["minimum_additional_sources"], 0)
        with self.assertRaisesRegex(Exception, "OPPORTUNITY_SCORE_REQUIRED"):
            missing_market_checks({"products": [dict(product, opportunity=None)]}, as_of=NOW)

    def test_meta_followup_targets_types_whose_facebook_ads_earn_nothing(self):
        memory = current_fixture(1); expire_ad(memory); memory.state["market_reviews"].clear()
        context(memory, "flipkart")
        targets, pool = validation_targets(memory, as_of=NOW)
        self.assertEqual(list(targets), ["c000"])
        target = targets["c000"]
        self.assertEqual((target["before_market"]["meta_points"], target["opportunity_policy_id"]), (0, POLICY_ID))
        result = validation_target_result({"target": target}, candidate_summary(memory, as_of=NOW))
        self.assertEqual(result["status"], "coverage_still_insufficient")
        self.assertFalse(result["useful_progress"])
        memory.state["observations"]["meta000"]["observed_at"] = NOW  # the ad is read again
        result = validation_target_result({"target": target}, candidate_summary(memory, as_of=NOW))
        self.assertTrue(result["useful_progress"])
        self.assertEqual(result["after_market"]["meta_points"], 2)
        self.assertEqual(pool["eligible"], [])
        current = current_fixture(1)  # Facebook ads already earn points: no Meta follow-up
        self.assertEqual(validation_targets(current, as_of=NOW)[0], {})


@older_format_scores
class ScoredSelectionTests(unittest.TestCase):
    def test_allocator_keeps_the_exact_cited_evidence(self):
        memory = current_fixture(10, matches=5)
        for n in range(10):
            context(memory, "blinkit", concept_id=f"c{n:03d}", suffix=str(n), available=True, city="Mumbai")
        pool = build_report_pool(memory, NOW, fit_policy_id=FIT_POLICY_ID)
        issue = allocate_issues(pool["eligible"], ["2026-10-07"], [], [], policy_for_fit(FIT_POLICY_ID))["issues"][0]
        self.assertTrue(issue["ready_for_review"])
        self.assertEqual(issue["opportunity_policy_id"], POLICY_ID)
        for row in issue["products"]:
            cited = {ref for refs in market_evidence(row).values() for ref in refs}
            self.assertEqual(len(cited), 2)  # the relevant ad and the in-stock Mumbai listing
            self.assertTrue(cited <= set(row["evidence_refs"]))
            self.assertEqual(row["opportunity_basis"]["parts"]["want"]["points"], 8)  # 2 ads + 5 lists + 1 city

    def test_writing_input_carries_code_owned_score_facts_not_a_priority(self):
        _, _, dataset = selection(current_fixture())
        payload = make_narrative_input(dataset, "en")
        self.assertEqual((payload["opportunity_policy_id"], payload["score_rule"]), (POLICY_ID, SCORE_RULE))
        self.assertNotIn("source_priority_rule", payload)
        facts = {row["id"]: row for row in payload["items"][1]["facts"]}
        self.assertTrue(facts["opportunity-score"]["text"].startswith("Opportunity score 45/100 · Want 7/20"))
        self.assertEqual(facts["opportunity-score"]["references"], ["meta000"])
        self.assertTrue(facts["crowding"]["text"].startswith("Amazon/Flipkart crowding: Low"))
        self.assertNotIn("confirmed-sources", facts)
        hindi = {row["id"]: row for row in make_narrative_input(dataset, "hi-Latn", english={"title": "t", "products": [
            {"concept_id": p["concept_id"]} for p in dataset["products"]]})["items"][1]["facts"]}
        self.assertIn("Maang 7/20", hindi["opportunity-score"]["text"])
        self.assertTrue(hindi["crowding"]["text"].startswith("Amazon/Flipkart par bheed: Kam"))


if __name__ == "__main__":
    unittest.main()
