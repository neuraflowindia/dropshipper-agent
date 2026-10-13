"""Distinct-platform prioritisation with fictional data and no provider calls."""

from copy import deepcopy
from datetime import timedelta
import unittest

from product_radar.discovery import compute_market_signals
from product_radar.fit_policy import FIT_POLICY_ID, evaluate_fit
from product_radar.issue_allocation import allocate_issues
from product_radar.live_outputs import render_html
from product_radar.market_validation import REVIEW_VERSION
from product_radar.models import SafetyError, digest, timestamp
from product_radar.opportunity_policy import (
    CURRENT_OPPORTUNITY_POLICY_ID, OPPORTUNITY_POLICY_ID, SOURCE_PLATFORMS,
    evaluate_opportunity, opportunity_fields, opportunity_sort_key, validate_opportunity,
)
from product_radar.providers.trends_browser import parse_trends_csv
from product_radar.report_narration import make_narrative_input
from product_radar.report_selection import build_report_pool
from product_radar.research_flow import candidate_summary
from product_radar.research_operations import missing_market_checks, validation_targets, validation_target_result
from product_radar.service import policy_for_fit
from test_fit_pipeline import current_fixture, selection
from test_fit_policy import criteria
from test_opportunity_policy import flat_trends, market, only_amazon
from test_report_selection import NOW
from test_trends_browser import metadata, weekly


def platform_market(names=("amazon", "flipkart"), positive=()):
    sources = {source: {"available": source in names, "positive": source in positive,
                        "used_ids": ["observed-" + source] if source in names else []}
               for source in SOURCE_PLATFORMS}
    return {"source_policy_id": CURRENT_OPPORTUNITY_POLICY_ID, "sources": sources,
            "available_sources": 900, "positive_sources": 900}


def selected(names=("amazon", "flipkart"), positive=(), matches=6):
    fit = evaluate_fit(criteria(matches), FIT_POLICY_ID)
    result = evaluate_opportunity(fit, platform_market(names, positive))
    return dict(fit, **opportunity_fields(result),
                kpi_states=[row["state"] for row in criteria(matches)])


def context(memory, source, *, concept_id="c000", suffix="one", accepted=True, at=NOW):
    obs_id = source + "-context-" + suffix
    description = memory.state["concepts"][concept_id]["description"]
    row = {"source": source, "source_item": source + "-item-" + suffix,
           "acquisition_id": source + "-capture-" + suffix, "observed_at": at,
           "semantic_id": source + "-semantic-" + suffix, "semantic": {"title": description["label"]},
           "facts": {"list_type": "product_search" if source in ("amazon", "flipkart") else "quickcommerce_context",
                     "url": "https://example.org/fixture/" + source, "limitations": [],
                     "price": {"displayed": "₹149", "currency": "INR"}}}
    memory.state["observations"][obs_id] = row
    if accepted:
        memory.state["market_reviews"]["context-review-" + obs_id] = {
            "concept_id": concept_id, "description_hash": digest(description), "review_version": REVIEW_VERSION,
            "reviewed_at": NOW, "input_hash": digest(row), "prompt_version": "research-e-source-relevance-v2",
            "relevance": [{"id": obs_id, "relationship": "relevant_family", "reason": "Fictional accepted family match."}]}
    return obs_id


class PlatformOpportunityTests(unittest.TestCase):
    def test_all_four_tiers_count_actual_distinct_platforms(self):
        for matches, names, positive, priority in (
                (6, ("amazon", "flipkart"), (), 1), (5, ("blinkit", "zepto"), (), 2),
                (6, ("amazon",), ("amazon",), 3), (5, ("google-trends",), ("google-trends",), 4)):
            with self.subTest(priority=priority):
                value = selected(names, positive, matches)
                self.assertEqual(validate_opportunity(value), priority)
                self.assertEqual(value["opportunity_basis"]["source_ids"], sorted(names))
                self.assertEqual(value["opportunity_basis"]["positive_source_ids"], sorted(positive))
                self.assertEqual(value["opportunity_basis"]["available_sources"], len(names))
                self.assertEqual(value["opportunity_policy_id"], CURRENT_OPPORTUNITY_POLICY_ID)

    def test_all_seven_sources_are_supported_but_historical_three_groups_stay_three(self):
        value = selected(SOURCE_PLATFORMS)
        self.assertEqual(validate_opportunity(value), 1)
        self.assertEqual(value["opportunity_basis"]["available_sources"], 7)
        old_fit = evaluate_fit(criteria(6), FIT_POLICY_ID)
        old = dict(old_fit, **opportunity_fields(evaluate_opportunity(old_fit, market(3, 0))))
        self.assertEqual(old["opportunity_policy_id"], OPPORTUNITY_POLICY_ID)
        self.assertEqual(validate_opportunity(old), 1)
        old["opportunity_basis"]["available_families"] = 4
        with self.assertRaises(SafetyError): validate_opportunity(old)

    def test_one_plain_listing_is_not_positive_and_missing_information_is_pending(self):
        fit = evaluate_fit(criteria(6), FIT_POLICY_ID)
        result = evaluate_opportunity(fit, platform_market(("flipkart",)))
        self.assertFalse(result["eligible"])
        self.assertEqual(result["reason"], "market_support_pending")
        self.assertEqual(result["opportunity_basis"]["positive_sources"], 0)
        empty = evaluate_opportunity(fit, platform_market(()))
        self.assertEqual(empty["reason"], "market_coverage_required")
        self.assertIsNone(empty["opportunity_priority"])

    def test_validated_reasonably_positive_interpretation_can_support_one_source(self):
        observed = platform_market(("flipkart",))
        observed["sources"]["flipkart"]["evidence_support"] = "reasonably_positive"
        fit = evaluate_fit(criteria(5), FIT_POLICY_ID)
        result = evaluate_opportunity(fit, observed)
        self.assertEqual(result["opportunity_priority"], 4)
        observed["sources"]["flipkart"]["available"] = False
        self.assertFalse(evaluate_opportunity(fit, observed)["eligible"])

    def test_aggregate_or_supplier_flags_do_not_invent_platforms(self):
        observed = platform_market(("amazon",))
        observed["sources"]["amazon"]["subchecks"] = ["bestsellers", "movers", "generic-search"]
        observed["sources"]["supplier"] = {"available": True, "positive": True, "used_ids": ["supplier-one"]}
        before = deepcopy(observed)
        result = evaluate_opportunity(evaluate_fit(criteria(6), FIT_POLICY_ID), observed)
        self.assertEqual(result["opportunity_basis"]["source_ids"], ["amazon"])
        self.assertFalse(result["eligible"])
        self.assertEqual(observed, before)

    def test_missing_or_duplicate_evidence_cannot_create_confirmations(self):
        for mutate in (
                lambda value: value["sources"]["amazon"].update(used_ids=[]),
                lambda value: value["sources"]["amazon"].update(used_ids=["same", "same"]),
                lambda value: value["sources"]["flipkart"].update(used_ids=["observed-amazon"]),
                lambda value: value["sources"]["amazon"].update(available=1),
                lambda value: value["sources"]["amazon"].update(positive=1)):
            observed = platform_market(); mutate(observed)
            with self.assertRaises(SafetyError):
                evaluate_opportunity(evaluate_fit(criteria(6), FIT_POLICY_ID), observed)

    def test_protected_ids_counts_and_reference_bindings_are_checked(self):
        for mutate in (
                lambda p: p["opportunity_basis"].update(available_sources=True),
                lambda p: p["opportunity_basis"].update(positive_sources=1),
                lambda p: p["opportunity_basis"].update(source_ids=["amazon", "amazon"]),
                lambda p: p["opportunity_basis"].update(source_ids=["amazon", "supplier"]),
                lambda p: p["opportunity_basis"].update(positive_source_ids=["meta"]),
                lambda p: p["opportunity_basis"]["evidence_by_source"].update(flipkart=["observed-amazon"]),
                lambda p: p.update(source_refs=[{"id": "observed-amazon"}]),
                lambda p: p.update(source_refs=[{"id": ["observed-amazon"]}]),
                lambda p: p.update(source_refs=[{"id": {"unexpected": "object"}}]),
                lambda p: p.update(evidence_refs=["observed-amazon"]),
                lambda p: p.update(opportunity_priority=True)):
            value = selected(); mutate(value)
            with self.assertRaises(SafetyError): validate_opportunity(value)

    def test_priority_sort_is_not_reversed_by_a_higher_checklist_total(self):
        rows = [selected(("amazon",), ("amazon",), 10), selected(("blinkit", "zepto"), (), 5)]
        self.assertEqual([row["opportunity_priority"] for row in sorted(rows, key=opportunity_sort_key)], [2, 3])


class PlatformResearchJoinTests(unittest.TestCase):
    def test_two_generic_platforms_qualify_without_inventing_sales(self):
        memory = current_fixture(1); only_amazon(memory); memory.state["market_reviews"].clear()
        amazon_id = context(memory, "amazon"); flipkart_id = context(memory, "flipkart")
        before = deepcopy(memory.state)
        pool = build_report_pool(memory, NOW, fit_policy_id=FIT_POLICY_ID)
        value = pool["eligible"][0]
        self.assertEqual(value["opportunity_priority"], 1)
        self.assertEqual(value["opportunity_basis"]["source_ids"], ["amazon", "flipkart"])
        self.assertEqual(value["opportunity_basis"]["positive_sources"], 0)
        self.assertEqual(value["positive_families"], 0)
        self.assertEqual(value["available_families"], 0)
        facts = pool["facts_by_concept"]["c000"]
        self.assertTrue({amazon_id, flipkart_id} <= {ref["id"] for ref in facts["source_refs"]})
        self.assertEqual(facts["usable_until"], (timestamp(NOW) + timedelta(days=7)).isoformat())
        self.assertIn("2 sources", facts["market_label"])
        self.assertTrue(all("not sales" in check["detail"] for check in facts["market_checks"] if check.get("context_only")))
        self.assertEqual(memory.state, before)

    def test_multiple_amazon_contexts_still_count_one_nonpositive_source(self):
        memory = current_fixture(1); only_amazon(memory); memory.state["market_reviews"].clear()
        context(memory, "amazon", suffix="search-a"); context(memory, "amazon", suffix="search-b")
        pool = build_report_pool(memory, NOW, fit_policy_id=FIT_POLICY_ID)
        self.assertEqual(pool["eligible"], [])
        basis = pool["facts_by_concept"]["c000"]["opportunity_basis"]
        self.assertEqual(basis["available_sources"], 1)
        self.assertEqual(len(basis["evidence_by_source"]["amazon"]), 2)
        self.assertEqual(pool["decisions"][0]["decision"], "deferred")

    def test_unreviewed_stale_unrelated_and_changed_scope_contexts_do_not_count(self):
        for change in ("unreviewed", "stale", "unrelated", "changed-description"):
            with self.subTest(change=change):
                memory = current_fixture(1); only_amazon(memory); memory.state["market_reviews"].clear()
                obs_id = context(memory, "flipkart", accepted=change != "unreviewed",
                                 at="2026-09-01T10:00:00+00:00" if change == "stale" else NOW)
                record = memory.state["market_reviews"].get("context-review-" + obs_id)
                if change == "unrelated": record["relevance"][0]["relationship"] = "unrelated"
                if change == "changed-description": record["description_hash"] = "0" * 64
                product = candidate_summary(memory, as_of=NOW)["products"][0]
                self.assertEqual(product["opportunity"]["opportunity_basis"]["available_sources"], 0)

    def test_quickcommerce_sites_count_separately_and_still_are_catalogue_context(self):
        memory = current_fixture(1); only_amazon(memory); memory.state["market_reviews"].clear()
        for source in ("blinkit", "zepto", "instamart"): context(memory, source)
        pool = build_report_pool(memory, NOW, fit_policy_id=FIT_POLICY_ID)
        basis = pool["eligible"][0]["opportunity_basis"]
        self.assertEqual(basis["available_sources"], 3)
        self.assertEqual(basis["positive_sources"], 0)
        self.assertEqual(pool["eligible"][0]["positive_families"], 0)
        tasks = missing_market_checks(candidate_summary(memory, as_of=NOW), as_of=NOW)
        self.assertFalse(tasks[0]["blocking"])
        self.assertEqual([row["source"] for row in tasks[0]["choices"]], ["amazon", "flipkart", "meta", "google-trends"])

    def test_zero_and_flat_full_charts_are_information_not_positive_demand(self):
        for values in ([0] * 54, [20] * 54):
            parsed = parse_trends_csv(weekly(values), metadata(collected_at=NOW))
            market_result = compute_market_signals({}, {}, NOW, [{"id": "timeline-one", "parsed": parsed}])
            source = market_result["sources"]["google-trends"]
            self.assertTrue(source["available"])
            self.assertIsNot(source["positive"], True)
            self.assertEqual(source["used_ids"], ["timeline-one"])
            decision = evaluate_opportunity(evaluate_fit(criteria(6), FIT_POLICY_ID), market_result)
            self.assertFalse(decision["eligible"])

    def test_two_platforms_do_not_hold_up_the_other_nonblocking_checks(self):
        memory = current_fixture(1)
        product = candidate_summary(memory, as_of=NOW)["products"][0]
        self.assertEqual(product["opportunity"]["opportunity_basis"]["source_ids"], ["amazon", "meta"])
        task = missing_market_checks({"products": [product]}, as_of=NOW)[0]
        self.assertEqual(task["needed_sources"], 0)
        self.assertEqual(task["minimum_additional_sources"], 0)
        self.assertFalse(task["blocking"])
        self.assertEqual([row["source"] for row in task["choices"]], ["flipkart", "google-trends", "blinkit", "zepto", "instamart"])

    def test_meta_followup_can_start_from_a_useful_generic_platform_without_bestseller_proof(self):
        memory = current_fixture(1); only_amazon(memory); memory.state["market_reviews"].clear()
        context(memory, "flipkart")
        targets, pool = validation_targets(memory, as_of=NOW)
        self.assertEqual(list(targets), ["c000"])
        target = targets["c000"]
        self.assertEqual(target["before_market"]["source_ids"], ["flipkart"])
        self.assertEqual(target["before_market"]["positive_sources"], 0)
        result = validation_target_result({"target": target}, candidate_summary(memory, as_of=NOW))
        self.assertEqual(result["status"], "coverage_still_insufficient")
        self.assertFalse(result["useful_progress"])
        context(memory, "zepto")
        result = validation_target_result({"target": target}, candidate_summary(memory, as_of=NOW))
        self.assertEqual(result["status"], "coverage_satisfied")
        self.assertEqual(result["after_market"]["source_ids"], ["flipkart", "zepto"])
        self.assertFalse(result["useful_progress"])
        self.assertEqual(pool["eligible"], [])

    def test_allocator_preserves_exact_evidence_when_free_sources_enable_selection(self):
        memory = current_fixture(10, matches=5)
        for n in range(10):
            only_amazon(memory, n); memory.state["market_reviews"].pop("review" + str(n))
            context(memory, "flipkart", concept_id=f"c{n:03d}", suffix=str(n))
            context(memory, "blinkit", concept_id=f"c{n:03d}", suffix=str(n))
        pool = build_report_pool(memory, NOW, fit_policy_id=FIT_POLICY_ID)
        issue = allocate_issues(pool["eligible"], ["2026-10-07"], [], [], policy_for_fit(FIT_POLICY_ID))["issues"][0]
        self.assertTrue(issue["ready_for_review"])
        self.assertEqual(issue["opportunity_policy_id"], CURRENT_OPPORTUNITY_POLICY_ID)
        self.assertTrue(all(row["opportunity_priority"] == 2 for row in issue["products"]))
        for row in issue["products"]:
            self.assertTrue(set(sum(row["opportunity_basis"]["evidence_by_source"].values(), [])) <= set(row["evidence_refs"]))

    def test_writing_input_keeps_platform_names_and_does_not_assign_priority(self):
        _, _, dataset = selection(current_fixture())
        payload = make_narrative_input(dataset, "en")
        self.assertEqual(payload["opportunity_policy_id"], CURRENT_OPPORTUNITY_POLICY_ID)
        self.assertIn("code-owned", payload["source_priority_rule"])
        facts = payload["items"][1]["facts"]
        confirmed = next(row for row in facts if row["id"] == "confirmed-sources")
        self.assertEqual(confirmed["text"], "Amazon India, Facebook ads")
        self.assertTrue(confirmed["references"])


if __name__ == "__main__":
    unittest.main()
