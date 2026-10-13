"""Synthetic opportunity-priority and connected selection tests, no services."""

from copy import deepcopy
from pathlib import Path
import sys
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "tests"))

from product_radar.fit_policy import FIT_POLICY_ID, evaluate_fit
from product_radar.issue_allocation import allocate_issues
from product_radar.models import SafetyError, digest
from product_radar.opportunity_policy import (
    CURRENT_OPPORTUNITY_POLICY_ID, DEMAND_GROUPS, OPPORTUNITY_FIELDS, OPPORTUNITY_POLICY_ID,
    evaluate_opportunity, opportunity_fields, opportunity_sort_key, validate_opportunity,
)
from product_radar.providers.trends_browser import parse_trends_csv
from product_radar.research_flow import candidate_summary, research_status
from product_radar.research_operations import missing_market_checks
from product_radar.report_selection import build_report_pool
from product_radar.service import policy_for_fit
from test_fit_policy import criteria
from test_fit_pipeline import current_fixture
from test_report_selection import fixture, NOW
from test_trends_browser import metadata, weekly


def market(available=2, positive=0):
    families = {}
    for n, name in enumerate(DEMAND_GROUPS):
        families[name] = {"available": n < available,
                          "positive": n < positive if n < available else None}
    return {"families": families, "available_families": available, "positive_families": positive}


def selected(matches=6, available=2, positive=0):
    fit = evaluate_fit(criteria(matches), FIT_POLICY_ID)
    return dict(fit, **opportunity_fields(evaluate_opportunity(fit, market(available, positive))),
                kpi_states=[row["state"] for row in criteria(matches)],
                available_families=available, positive_families=positive)


def only_amazon(memory, n=0):
    # Older observed product features still support fit; they do not keep an
    # expired advert in the current demand sample.
    memory.state["observations"][f"meta{n:03d}"]["observed_at"] = "2026-09-01T10:00:00+00:00"


def flat_trends(memory, n=0):
    concept_id = f"c{n:03d}"
    description = memory.state["concepts"][concept_id]["description"]
    term = description["label"]
    parsed = parse_trends_csv(weekly([20] * 54, label=term + ": (India)"),
                              metadata(term=term, collected_at=NOW,
                                       source_reference="https://trends.google.com/trends/explore?geo=IN&q=fixture"))
    memory.state["trends_evidence"][f"flat-trends{n}"] = {
        "concept_id": concept_id, "acquisition_id": f"flat-capture{n}",
        "description_hash": digest(description), "parsed": parsed,
    }
    memory.state["market_reviews"].pop(f"review{n}")


class OpportunityPolicyTests(unittest.TestCase):
    def test_all_four_priorities_and_three_source_boundary(self):
        for matches, available, positive, priority in (
                (6, 2, 0, 1), (10, 3, 0, 1), (5, 2, 0, 2), (5, 3, 2, 2),
                (6, 1, 1, 3), (10, 1, 1, 3), (5, 1, 1, 4)):
            with self.subTest(matches=matches, available=available, positive=positive):
                product = selected(matches, available, positive)
                self.assertEqual(product["opportunity_priority"], priority)
                self.assertEqual(validate_opportunity(product), priority)

    def test_two_source_zero_growth_is_not_rejected(self):
        fit = evaluate_fit(criteria(6), FIT_POLICY_ID)
        result = evaluate_opportunity(fit, market(2, 0))
        self.assertTrue(result["eligible"])
        self.assertEqual(result["opportunity_basis"], {"available_families": 2, "positive_families": 0})
        self.assertEqual(research_status(6, market(2, 0), fit=fit), "candidate_for_supplier_review")

    def test_one_nonpositive_zero_sources_and_under_five_are_pending(self):
        for matches, available, positive, reason in (
                (6, 1, 0, "market_support_pending"), (5, 0, 0, "market_coverage_required"),
                (4, 2, 1, "fit_information_pending")):
            with self.subTest(matches=matches, available=available):
                result = evaluate_opportunity(evaluate_fit(criteria(matches), FIT_POLICY_ID), market(available, positive))
                self.assertFalse(result["eligible"])
                self.assertIsNone(result["opportunity_priority"])
                self.assertEqual(result["reason"], reason)

    def test_supported_reasonably_positive_marker_enables_one_source(self):
        source = market(1, 0)
        source["families"]["meta"]["evidence_support"] = "reasonably_positive"
        result = evaluate_opportunity(evaluate_fit(criteria(5), FIT_POLICY_ID), source)
        self.assertEqual(result["opportunity_priority"], 4)
        self.assertEqual(result["opportunity_basis"]["positive_families"], 1)
        source["families"]["meta"]["available"] = False
        self.assertFalse(evaluate_opportunity(evaluate_fit(criteria(5), FIT_POLICY_ID), source)["eligible"])

    def test_aggregate_counts_and_same_platform_subchecks_do_not_create_sources(self):
        source = market(1, 0)
        source.update(available_families=3, positive_families=3)
        source["families"]["meta"]["subchecks"] = ["first", "second", "third"]
        source["families"]["supplier"] = {"available": True, "positive": True}
        before = deepcopy(source)
        result = evaluate_opportunity(evaluate_fit(criteria(6), FIT_POLICY_ID), source)
        self.assertEqual(result["opportunity_basis"], {"available_families": 1, "positive_families": 0})
        self.assertFalse(result["eligible"])
        self.assertEqual(source, before)

    def test_invalid_numeric_or_boolean_fit_and_source_values_are_rejected(self):
        fit = evaluate_fit(criteria(6), FIT_POLICY_ID)
        for value in (True, 6.0, -1, 11):
            with self.subTest(matches=value), self.assertRaises(SafetyError):
                evaluate_opportunity(dict(fit, kpi_matches=value), market())
        for value in (1, "true", None):
            source = market(); source["families"]["meta"]["available"] = value
            with self.subTest(available=value), self.assertRaises(SafetyError):
                evaluate_opportunity(fit, source)
        source = market(); source["families"]["meta"]["positive"] = 1
        with self.assertRaises(SafetyError):
            evaluate_opportunity(fit, source)

    def test_marked_records_require_exact_policy_priority_and_basis(self):
        base = selected()
        for change in (
                {"opportunity_policy_id": "invented"}, {"opportunity_priority": True},
                {"opportunity_priority": 1.0}, {"opportunity_priority": 3},
                {"opportunity_basis": {"available_families": True, "positive_families": 0}},
                {"opportunity_basis": {"available_families": 1, "positive_families": 2}},
                {"opportunity_basis": {"available_families": 4, "positive_families": 0}}):
            with self.subTest(change=change), self.assertRaises(SafetyError):
                validate_opportunity(dict(base, **change))
        partial = deepcopy(base); partial.pop("opportunity_basis")
        with self.assertRaises(SafetyError):
            validate_opportunity(partial)

    def test_shared_sort_obeys_source_priority_before_fit(self):
        records = [selected(6, 1, 1), selected(5, 2, 0), selected(5, 1, 1), selected(6, 2, 0)]
        self.assertEqual([row["opportunity_priority"] for row in sorted(records, key=opportunity_sort_key)], [1, 2, 3, 4])

    def test_unmarked_historical_records_are_not_reinterpreted(self):
        product = {"kpi_states": ["Meets"] * 7 + ["Unclear"] * 6}
        before = deepcopy(product)
        self.assertIsNone(validate_opportunity(product))
        opportunity_sort_key(product)
        self.assertEqual(product, before)
        self.assertEqual(research_status(8, market(2, 0)), "weak_market_signal")
        self.assertEqual(research_status(8, market(1, 1)), "needs_market_evidence")


class OpportunityPipelineTests(unittest.TestCase):
    def test_two_usable_nonpositive_sources_are_selected_without_changing_measurements(self):
        memory = current_fixture(1)
        flat_trends(memory)
        before = deepcopy(memory.state)
        pool = build_report_pool(memory, NOW, fit_policy_id=FIT_POLICY_ID)
        self.assertEqual(len(pool["eligible"]), 1)
        row = pool["eligible"][0]
        self.assertEqual(row["opportunity_priority"], 1)
        self.assertEqual(row["opportunity_basis"], {"available_sources": 2, "positive_sources": 0,
            "source_ids": ["google-trends", "meta"], "positive_source_ids": [],
            "evidence_by_source": {"google-trends": ["flat-trends0"], "meta": ["meta000"]}})
        self.assertEqual(row["positive_families"], 0)
        self.assertEqual(pool["facts_by_concept"]["c000"]["opportunity_priority"], 1)
        self.assertEqual(pool["decisions"][0]["decision"], "included")
        self.assertTrue(all(not work["blocking"] for work in pool["needed_market_work"]))
        optional = missing_market_checks(candidate_summary(memory, as_of=NOW), as_of=NOW)
        self.assertEqual(len(optional), 1)
        self.assertFalse(optional[0]["blocking"])
        self.assertEqual([choice["source"] for choice in optional[0]["choices"]], ["amazon", "flipkart", "blinkit", "zepto", "instamart"])
        self.assertEqual(memory.state, before)

    def test_one_positive_source_is_selected_and_retains_nonblocking_followup(self):
        for matches, priority in ((6, 3), (5, 4)):
            with self.subTest(matches=matches):
                memory = current_fixture(1, matches)
                only_amazon(memory)
                pool = build_report_pool(memory, NOW, fit_policy_id=FIT_POLICY_ID)
                self.assertEqual(pool["eligible"][0]["opportunity_priority"], priority)
                self.assertEqual(len(pool["needed_market_work"]), 1)
                self.assertFalse(pool["needed_market_work"][0]["blocking"])
                summary = candidate_summary(memory, as_of=NOW)
                self.assertEqual(summary["products"][0]["status"], "candidate_for_supplier_review")
                work = missing_market_checks(summary, as_of=NOW)
                self.assertFalse(work[0]["blocking"])
                self.assertEqual(work[0]["minimum_additional_sources"], 0)

    def test_one_nonpositive_source_is_deferred_not_rejected(self):
        memory = current_fixture(1)
        memory.state["market_reviews"].clear()
        pool = build_report_pool(memory, NOW, fit_policy_id=FIT_POLICY_ID)
        self.assertEqual(pool["eligible"], [])
        self.assertEqual(pool["decisions"][0]["decision"], "deferred")
        self.assertIn("market_support_pending", pool["decisions"][0]["codes"])
        self.assertTrue(pool["needed_market_work"][0]["blocking"])

    def test_pool_and_allocator_put_five_two_sources_before_six_one_source(self):
        memory = current_fixture(10, matches=5)
        memory.state["assessments"]["c000"]["result"]["criteria"][5]["state"] = "Meets"
        only_amazon(memory)
        pool = build_report_pool(memory, NOW, fit_policy_id=FIT_POLICY_ID)
        self.assertEqual(pool["eligible"][0]["opportunity_priority"], 2)
        self.assertEqual(pool["eligible"][-1]["concept_id"], "c000")
        result = allocate_issues(list(reversed(pool["eligible"])), ["2026-10-07"], [], [],
                                 policy=policy_for_fit(FIT_POLICY_ID))
        issue = result["issues"][0]
        self.assertTrue(issue["ready_for_review"])
        self.assertEqual(issue["opportunity_policy_id"], CURRENT_OPPORTUNITY_POLICY_ID)
        self.assertEqual(issue["products"][-1]["concept_id"], "c000")
        self.assertEqual([row["opportunity_priority"] for row in issue["products"]], [2] * 9 + [3])

    def test_invalid_priority_does_not_stop_healthy_allocator_rows(self):
        pool = build_report_pool(current_fixture(11), NOW, fit_policy_id=FIT_POLICY_ID)
        broken = pool["eligible"][0]
        broken["opportunity_priority"] = True
        result = allocate_issues(pool["eligible"], ["2026-10-07"], [], [], policy=policy_for_fit(FIT_POLICY_ID))
        self.assertTrue(result["issues"][0]["ready_for_review"])
        self.assertNotIn(broken["concept_id"], [row["concept_id"] for row in result["issues"][0]["products"]])

    def test_legacy_pool_and_allocation_have_no_new_marker(self):
        pool = build_report_pool(fixture(10), NOW)
        issue = allocate_issues(pool["eligible"], ["2026-10-07"], [], [],
                               policy=policy_for_fit("fit-13-v1"))["issues"][0]
        self.assertNotIn("opportunity_policy_id", issue)
        self.assertTrue(all(not any(field in row for field in OPPORTUNITY_FIELDS) for row in issue["products"]))

    def test_old_unmarked_ten_point_frozen_records_still_allocate(self):
        pool = build_report_pool(current_fixture(10), NOW, fit_policy_id=FIT_POLICY_ID)
        for row in pool["eligible"]:
            for field in OPPORTUNITY_FIELDS:
                row.pop(field)
        before = deepcopy(pool["eligible"])
        issue = allocate_issues(pool["eligible"], ["2026-10-07"], [], [],
                               policy=policy_for_fit(FIT_POLICY_ID))["issues"][0]
        self.assertTrue(issue["ready_for_review"])
        self.assertNotIn("opportunity_policy_id", issue)
        self.assertEqual(pool["eligible"], before)


if __name__ == "__main__":
    unittest.main()
