"""Exact query-pool arithmetic and honest unmapped/uncertain distinctions."""

from copy import deepcopy
import unittest

from test_catalogue_expansion import NOW, fixture, phrase
from product_radar.catalogue_coverage import catalogue_matrix, workload_scenarios
from product_radar.models import SafetyError


class CoverageTests(unittest.TestCase):
    def setUp(self): self.catalogue = fixture()
    def request(self, **change):
        return dict({"term": "sink caddy", "source": "meta", "purpose": "discovery", "language": "en", "country": "IN"}, **change)
    def paid_task(self, attempt_id):
        return {"plan_id": "fixture-plan", "action_id": "fixture-action", "family_id": "caddy",
                "attempt_id": attempt_id, "config_hash": "a" * 64, "status": "prepared",
                "baseline_concepts": [], "collector_dispatched": False,
                "request": self.request(route="apify", build="2.7.26", schema_version=1,
                                        date_scope={"kind": "active_ads_at_capture"}, coverage={"limit": 5}, options={})}
    def state(self):
        return {"captures": {"capture": {"acquisition_id": "saved-data", "observed_at": NOW}},
            "query_outcomes": {"outcome": {"family_id": "caddy", "request": self.request(), "acquisition_id": "saved-data", "observed_at": NOW}}}

    def test_counting_is_sum_of_actual_family_choices_not_category_times_arbitrary_phrases(self):
        family = self.catalogue["families"][0]
        family["phrases"].append(dict(phrase("dishwashing tool organiser")))
        family["phrases"].append(dict(phrase("sink caddy"), source="marketplace", purpose="validation"))
        result = catalogue_matrix(self.catalogue)
        self.assertEqual(result["totals"]["family_phrase_pairs"], 2)
        self.assertEqual(result["totals"]["source_purpose_options"], 3)
        self.assertEqual(result["totals"]["meta_discovery_options"], 2)

    def test_same_semantic_choice_with_different_reason_does_not_inflate_pool(self):
        self.catalogue["families"][0]["phrases"].append(dict(phrase("SINK CADDY"), reason="Different explanation."))
        self.assertEqual(catalogue_matrix(self.catalogue)["totals"]["source_purpose_options"], 1)

    def test_one_bound_captured_query_is_tried_once_not_every_variant_in_family(self):
        self.catalogue["families"][0]["phrases"].append(phrase("dishwashing tool organiser"))
        state = self.state(); state["query_outcomes"]["duplicate"] = deepcopy(state["query_outcomes"]["outcome"])
        result = catalogue_matrix(self.catalogue, state)
        self.assertEqual(result["totals"]["meta_discovery_tried"], 1)
        self.assertEqual(result["totals"]["meta_discovery_not_dispatched"], 1)
        self.assertEqual(sum(len(row["acquisition_ids"]) for row in result["rows"]), 1)

    def test_planned_and_uncertain_jobs_are_not_reported_as_successful_or_fresh_unused_searches(self):
        state = {"query_tasks": {"task": self.paid_task("lost")}}
        prepared = catalogue_matrix(self.catalogue, state)
        self.assertEqual(prepared["rows"][0]["state"], "prepared_not_dispatched")
        uncertain = catalogue_matrix(self.catalogue, state, financial_attempts={"lost": {"submission": "uncertain"}})
        self.assertEqual(uncertain["totals"]["meta_discovery_tried"], 0)
        self.assertEqual(uncertain["totals"]["meta_discovery_not_dispatched"], 0)
        self.assertEqual(uncertain["totals"]["meta_discovery_execution_uncertain"], 1)

    def test_real_submitted_receipt_establishes_try_even_before_rows_are_interpreted(self):
        state = {"query_tasks": {"task": self.paid_task("sent")}}
        result = catalogue_matrix(self.catalogue, state, financial_attempts={"sent": {"submission": "submitted", "run_id": "real-run", "dataset_id": "saved-data"}})
        self.assertEqual(result["rows"][0]["state"], "tried")

    def test_unmapped_checks_and_validation_choices_do_not_claim_untouched_coverage(self):
        self.catalogue["families"][0]["phrases"].append(dict(phrase("sink caddy"), source="trends", purpose="validation"))
        state = {"query_tasks": {"public-task": {"kind": "all_source_task_v1", "request": {"query": "sink caddy", "term": None}}}}
        result = catalogue_matrix(self.catalogue, state)
        trends = next(row for row in result["rows"] if row["source"] == "trends")
        self.assertEqual(trends["state"], "coverage_not_mapped")
        self.assertEqual(len(result["unmapped"]), 1)

    def test_invalid_capture_link_does_not_count_as_completed_search(self):
        state = self.state(); state["captures"] = {}
        result = catalogue_matrix(self.catalogue, state)
        self.assertEqual(result["totals"]["meta_discovery_tried"], 0)
        self.assertEqual(len(result["unmapped"]), 1)

    def test_direct_meta_attempt_consumes_its_exact_choice_but_marketplace_fallback_does_not_consume_meta(self):
        question = {"id": "actual-question", "route": "meta-ads", "request": {"query": "sink caddy", "country": "IN"},
            "choice_source": "meta", "choice_purpose": "discovery", "language": "en"}
        state = {"captures": {"capture": {"acquisition_id": "actual-data", "observed_at": NOW}},
            "batch_runs": {"cycle": {"kind": "catalogue-discovery-cycle-v1", "planned_actions": [{"family_id": "caddy", "questions": [question]}],
                "actions": {"actual-question": {"state": "interpretation_pending", "dispatch_started": True,
                    "capture_ids": ["capture"], "acquisition_id": "actual-data", "observed_at": NOW}}}}}
        result = catalogue_matrix(self.catalogue, state)
        self.assertEqual(result["totals"]["meta_discovery_tried"], 1)
        self.assertEqual(result["totals"]["actual_public_discovery_queries"], 1)
        question["route"] = "amazon-search"
        alternative = catalogue_matrix(self.catalogue, state)
        self.assertEqual(alternative["totals"]["meta_discovery_tried"], 0)
        self.assertEqual(alternative["totals"]["actual_public_discovery_queries"], 1)
        self.assertEqual(alternative["unmapped"][0]["kind"], "public_discovery_question")

    def test_six_month_scenario_includes_yield_and_per_issue_floor_for_updates(self):
        matrix = catalogue_matrix(self.catalogue)
        result = workload_scenarios(matrix, new_candidates_per_query=5)
        self.assertEqual((result["issues"], result["entries"], result["new_entries_if_maximum_useful_updates_exist"]), (78, 780, 624))
        first = result["yield_cases"][0]
        self.assertEqual((first["candidates_per_issue"], first["queries_per_issue"], first["horizon_queries"]), (160, 32, 2496))
        worse = next(row for row in result["yield_cases"] if row["useful_yield"] == "0.03" and row["new_products_per_issue"] == 8)
        self.assertGreater(worse["queries_per_issue"], first["queries_per_issue"])
        self.assertEqual(result["query_cases"][-1]["horizon_queries"], 2340)

    def test_no_candidate_yield_is_invented_when_it_was_not_measured_or_supplied(self):
        result = workload_scenarios(catalogue_matrix(self.catalogue))
        self.assertEqual(result["yield_cases"], [])
        for rate in ("0", "-1", "NaN", "Infinity", "bad"):
            with self.assertRaisesRegex(SafetyError, "YIELDS_INVALID"):
                workload_scenarios(catalogue_matrix(self.catalogue), new_candidates_per_query=5, useful_yields=[rate])


if __name__ == "__main__": unittest.main()
