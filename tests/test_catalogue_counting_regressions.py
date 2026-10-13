"""Pure query-counting regressions; no memory, files, models or providers.

An attempted search need not produce usable rows. These tests distinguish the
physical attempt from successful extraction and keep genuinely untried choices
separate. Family/phrase/source/purpose/language/country defines one choice;
another explanation of that same choice does not create more search capacity.
"""

from copy import deepcopy
import unittest

from test_catalogue_expansion import NOW, fixture, phrase
from product_radar.catalogue import catalogue_summary
from product_radar.catalogue_coverage import catalogue_matrix


class CatalogueAttemptCountingRegressions(unittest.TestCase):
    def setUp(self):
        self.catalogue = fixture()

    def paid_task(self):
        # Real legacy preparation shape; submitted/uncertain belongs to the
        # financial attempt, not to an invented typed or incomplete task.
        return {"family_id": "caddy", "attempt_id": "paid-attempt", "plan_id": "paid-plan",
                "action_id": "paid-action", "config_hash": "a" * 64, "status": "prepared",
                "baseline_concepts": [], "collector_dispatched": False,
                "request": {"term": "sink caddy", "source": "meta", "purpose": "discovery",
                    "language": "en", "country": "IN", "route": "apify", "build": "fixture-build",
                    "schema_version": 1, "date_scope": {}, "coverage": {}, "options": {}}}

    def public_state(self, *, state, dispatch_started, with_capture=False):
        question = {"id": "question", "route": "meta-ads",
                    "request": {"query": "sink caddy", "country": "IN"},
                    "choice_source": "meta", "choice_purpose": "discovery", "language": "en"}
        action = {"state": state, "dispatch_started": dispatch_started,
                  "acquisition_id": "actual-public-acquisition", "observed_at": NOW,
                  "capture_ids": ["capture"] if with_capture else []}
        return {"captures": {"capture": {"acquisition_id": "actual-public-acquisition", "observed_at": NOW}}
                if with_capture else {},
                "batch_runs": {"cycle": {"kind": "catalogue-discovery-cycle-v1",
                    "planned_actions": [{"family_id": "caddy", "questions": [question]}],
                    "actions": {"question": action}}}}

    def assert_attempted_not_unused(self, action_state, *, with_capture=False):
        result = catalogue_matrix(self.catalogue, self.public_state(
            state=action_state, dispatch_started=True, with_capture=with_capture))
        self.assertEqual(result["totals"]["actual_public_discovery_queries"], 1)
        self.assertEqual(result["totals"]["meta_discovery_tried"], 1)
        self.assertEqual(result["totals"]["meta_discovery_not_dispatched"], 0)
        self.assertEqual(result["totals"]["meta_discovery_execution_uncertain"], 0)
        self.assertEqual(result["rows"][0]["state"], "tried")
        self.assertEqual(result["rows"][0]["acquisition_ids"], ["actual-public-acquisition"])

    def test_actual_blocked_navigation_is_tried_even_without_captured_rows(self):
        self.assert_attempted_not_unused("blocked")

    def test_actual_failed_read_is_tried_even_without_captured_rows(self):
        self.assert_attempted_not_unused("failed")

    def test_recognised_no_results_is_tried_even_without_product_rows(self):
        self.assert_attempted_not_unused("empty")

    def test_successful_capture_is_tried_before_target_interpretation(self):
        self.assert_attempted_not_unused("interpretation_pending", with_capture=True)

    def test_retained_proved_navigation_before_result_is_an_attempt_not_unused(self):
        self.assert_attempted_not_unused("intent_saved")

    def test_pre_dispatch_failure_remains_untried_and_has_no_physical_request(self):
        for outcome in ("not_connected", "local_retry_pending", "failed"):
            with self.subTest(outcome=outcome):
                result = catalogue_matrix(self.catalogue, self.public_state(
                    state=outcome, dispatch_started=False))
                self.assertEqual(result["totals"]["actual_public_discovery_queries"], 0)
                self.assertEqual(result["totals"]["meta_discovery_tried"], 0)
                self.assertEqual(result["totals"]["meta_discovery_not_dispatched"], 1)
                self.assertEqual(result["rows"][0]["state"], "prepared_not_dispatched")

    def test_unknown_execution_is_neither_a_proved_attempt_nor_unused_capacity(self):
        result = catalogue_matrix(self.catalogue, self.public_state(
            state="execution_uncertain", dispatch_started=None))
        self.assertEqual(result["totals"]["actual_public_discovery_queries"], 0)
        self.assertEqual(result["totals"]["meta_discovery_tried"], 0)
        self.assertEqual(result["totals"]["meta_discovery_not_dispatched"], 0)
        self.assertEqual(result["totals"]["meta_discovery_execution_uncertain"], 1)

    def test_one_failed_phrase_does_not_consume_unvisited_sibling_phrase(self):
        self.catalogue["families"][0]["phrases"].append(phrase("dishwashing tool organiser"))
        result = catalogue_matrix(self.catalogue, self.public_state(state="blocked", dispatch_started=True))
        self.assertEqual(result["totals"]["meta_discovery_options"], 2)
        self.assertEqual(result["totals"]["meta_discovery_tried"], 1)
        self.assertEqual(result["totals"]["meta_discovery_not_dispatched"], 1)

    def test_paid_submitted_attempt_remains_tried_without_interpreted_capture(self):
        state = {"query_tasks": {"paid-question": self.paid_task()}}
        result = catalogue_matrix(self.catalogue, state, financial_attempts={"paid-attempt": {
            "submission": "submitted", "run_id": "actual-paid-run", "dataset_id": "actual-paid-dataset"}})
        self.assertEqual(result["totals"]["meta_discovery_tried"], 1)
        self.assertEqual(result["totals"]["meta_discovery_not_dispatched"], 0)

    def test_paid_uncertain_submission_is_not_reported_as_unused_or_proved_tried(self):
        state = {"query_tasks": {"paid-question": self.paid_task()}}
        result = catalogue_matrix(self.catalogue, state,
                                  financial_attempts={"paid-attempt": {"submission": "uncertain"}})
        self.assertEqual(result["totals"]["meta_discovery_tried"], 0)
        self.assertEqual(result["totals"]["meta_discovery_not_dispatched"], 0)
        self.assertEqual(result["totals"]["meta_discovery_execution_uncertain"], 1)

    def test_repeat_outcome_does_not_add_another_same_acquisition_to_exact_choice(self):
        state = self.public_state(state="interpretation_pending", dispatch_started=True, with_capture=True)
        outcome = {"family_id": "caddy", "acquisition_id": "actual-public-acquisition", "observed_at": NOW,
                   "request": {"term": "sink caddy", "source": "meta", "purpose": "discovery", "language": "en", "country": "IN"}}
        state["query_outcomes"] = {"first": deepcopy(outcome), "replayed": deepcopy(outcome)}
        result = catalogue_matrix(self.catalogue, state)
        self.assertEqual(result["totals"]["meta_discovery_tried"], 1)
        self.assertEqual(result["rows"][0]["acquisition_ids"], ["actual-public-acquisition"])


class CatalogueSummaryChoiceDeduplicationRegressions(unittest.TestCase):
    def setUp(self):
        self.catalogue = fixture()

    def assert_summary_matches_exact_matrix(self, expected):
        original = deepcopy(self.catalogue)
        summary = catalogue_summary(self.catalogue)
        matrix = catalogue_matrix(self.catalogue)
        self.assertEqual(matrix["totals"]["source_purpose_options"], expected)
        self.assertEqual(summary["source_purpose_options"], expected)
        self.assertEqual(self.catalogue, original)

    def test_case_spacing_and_reason_only_duplicates_are_one_choice_in_both_views(self):
        self.catalogue["families"][0]["phrases"].append(dict(
            phrase("  SINK   CADDY  "), reason="Another explanation is not another query."))
        self.assert_summary_matches_exact_matrix(1)
        self.assertEqual(catalogue_summary(self.catalogue)["phrase_variants"], 1)

    def test_source_purpose_and_language_are_separate_choices_not_duplicate_reasons(self):
        self.catalogue["families"][0]["phrases"].extend([
            dict(phrase("sink caddy"), source="trends", purpose="validation"),
            dict(phrase("sink caddy"), purpose="validation"),
            dict(phrase("sink caddy"), language="hi-Latn"),
            dict(phrase("sink caddy"), source="trends", purpose="validation", reason="Same Trends choice.")])
        self.assert_summary_matches_exact_matrix(4)
        self.assertEqual(catalogue_summary(self.catalogue)["phrase_variants"], 1)

    def test_same_text_in_different_family_does_not_collapse_family_choices(self):
        second = deepcopy(self.catalogue["families"][0])
        second.update(id="other-family", label="Pan holder", aliases=[],
                      phrases=[phrase("sink caddy")])
        self.catalogue["families"].append(second)
        self.assert_summary_matches_exact_matrix(2)
        self.assertEqual(catalogue_matrix(self.catalogue)["totals"]["family_phrase_pairs"], 2)

    def test_proposed_and_retired_rows_do_not_add_usable_search_options(self):
        for index, disposition in enumerate(("proposed", "retired")):
            other = deepcopy(self.catalogue["families"][0])
            other.update(id="excluded-family-" + str(index), label="Other function " + str(index),
                         aliases=[], disposition=disposition,
                         phrases=[phrase("other function " + str(index))])
            self.catalogue["families"].append(other)
        self.assert_summary_matches_exact_matrix(1)


if __name__ == "__main__":
    unittest.main()
