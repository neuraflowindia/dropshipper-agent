"""Bounded saved-source opinions use real fixture refs, never sales inventions."""

from copy import deepcopy
from datetime import timedelta
from pathlib import Path
import tempfile
import unittest

from product_radar.discovery import compute_market_signals
from product_radar.fit_policy import FIT_POLICY_ID
from product_radar.market_validation import REVIEW_VERSION, SOURCE_SUPPORT_VERSION, current_source_support, source_evidence
from product_radar.models import SafetyError, digest, timestamp
from product_radar.research_ai import SOURCE_RELEVANCE_PROMPT_VERSION, SOURCE_SUPPORT_PROMPT_VERSION, e_contract, validate_results
from product_radar.research_flow import candidate_summary
from product_radar.research_memory import ResearchMemory, change
from product_radar.report_selection import build_report_pool, selection_scope
from product_radar.report_narration import make_narrative_input, generate_report_narratives
from product_radar.live_outputs import validate_dataset, render_html
from product_radar.source_interpretation import _item, _questions, review_source_results
from product_radar.storage import Journal
from product_radar.source_coverage import prepare_all_source_tasks
from test_fit_pipeline import current_fixture, selection, WritingFixture
from test_opportunity_platforms import context
from test_opportunity_policy import flat_trends, only_amazon
from test_report_selection import NOW


def question(memory, refs, source="flipkart"):
    return {"kind": "saved_source_support", "task_id": "saved-support-fixture", "source": source,
            "concept_id": "c000", "description_hash": digest(memory.state["concepts"]["c000"]["description"]),
            "observation_ids": refs, "unknown_ids": refs, "relevant_ids": refs, "uncertain_ids": []}


def record(memory, refs, *, source="flipkart", judgment="reasonably_positive", review_id="support-fixture", at=NOW):
    item = _item(memory.state, question(memory, refs, source), 30)
    value = {"concept_id": "c000", "description_hash": digest(item["description"]), "input_hash": digest(item),
             "input": item, "review_version": REVIEW_VERSION, "support_version": SOURCE_SUPPORT_VERSION,
             "prompt_version": SOURCE_SUPPORT_PROMPT_VERSION, "reviewed_at": at,
             "evidence_hashes": {ref: digest(source_evidence(memory.state, ref)[2]) for ref in refs},
             "source_support": [{"source": source, "judgment": judgment, "basis": "inferred", "references": refs,
                 "reason": "Estimated: meaningful captured consumer-review context supports moderate interest."}],
             "relevance": [{"id": ref, "relationship": "relevant_family", "reason": "Synthetic matching function."} for ref in refs]}
    memory.state["market_reviews"][review_id] = value
    return value


class Runner:
    identity = "source-support-fixture-v1"
    def __init__(self, judgment="neutral"):
        self.calls, self.judgment = [], judgment

    def __call__(self, role, payload, folder):
        self.calls.append(deepcopy(payload)); answers = []
        for item in payload["items"]:
            aliases = list(item["reference_aliases"])
            sources = sorted({row["source"] for row in item["observations"]})
            answers.append({"id": item["id"], "criteria": [], "relevance": [
                {"id": alias, "relationship": "relevant_family", "reason": "Synthetic matched object and function."} for alias in aliases],
                "source_support": [{"source": source, "judgment": self.judgment, "basis": "inferred",
                    "references": [alias for alias, ref in item["reference_aliases"].items()
                                   if next(row for row in item["observations"] if row["id"] == ref)["source"] == source],
                    "reason": "Estimated: the supplied current recurring-interest context is moderately useful."} for source in sources],
                "compliance_note": "", "missing_questions": []})
        return {"results": answers}, {"input_tokens": 1, "output_tokens": 1, "cached_input_tokens": 0}


class SourceSupportTests(unittest.TestCase):
    def fixture(self, *, trends=False):
        memory = current_fixture(1); only_amazon(memory); memory.state["market_reviews"].clear()
        if trends:
            # The helper removes this legacy review itself.
            memory.state["market_reviews"]["review0"] = {}
            flat_trends(memory); refs = ["flat-trends0"]
        else:
            refs = [context(memory, "flipkart")]
            memory.state["observations"][refs[0]]["facts"].update(rating=4.4, review_count=78)
        return memory, refs

    def test_source_support_is_a_new_explicit_contract_not_an_old_schema_change(self):
        payload = {"mode": "marketplace_relevance_only", "criteria": [], "relevance_style": "short-references-support-v3"}
        self.assertEqual(e_contract(payload)["version"], SOURCE_SUPPORT_PROMPT_VERSION)
        self.assertIn("source_support", e_contract(payload)["schema"]["properties"]["results"]["items"]["properties"])
        old = dict(payload, relevance_style="short-references-v2")
        self.assertEqual(e_contract(old)["version"], SOURCE_RELEVANCE_PROMPT_VERSION)
        self.assertNotIn("source_support", e_contract(old)["schema"]["properties"]["results"]["items"]["properties"])
        with self.assertRaisesRegex(SafetyError, "AI_E_CONTRACT_INVALID"):
            e_contract(payload, SOURCE_RELEVANCE_PROMPT_VERSION)

    def answer(self):
        memory, refs = self.fixture(); item = _item(memory.state, question(memory, refs), 30)
        payload = {"mode": "marketplace_relevance_only", "criteria": [], "relevance_style": "short-references-support-v3", "items": [item]}
        response, _ = Runner("reasonably_positive")("E", payload, None)
        return memory, refs, payload, response

    def test_short_aliases_expand_support_and_relevance_to_the_same_real_source(self):
        _, refs, payload, response = self.answer()
        accepted = validate_results("E", payload, response)["accepted"]["c000"]
        self.assertEqual(accepted["source_support"][0]["references"], refs)
        self.assertEqual(accepted["relevance"][0]["id"], refs[0])

    def test_unrelated_rows_cannot_be_borrowed_as_positive_evidence(self):
        _, _, payload, response = self.answer()
        response["results"][0]["relevance"][0]["relationship"] = "unrelated"
        failure = validate_results("E", payload, response)["failures"]["c000"]
        self.assertEqual(failure, "AI_SOURCE_SUPPORT_REFERENCE_INVALID")

    def test_missing_support_unknown_sources_and_unsupported_positive_fail_only_that_result(self):
        for mutate, failure in (
            (lambda row: row.update(source_support=[]), "AI_SOURCE_SUPPORT_RESULT_MISSING"),
            (lambda row: row["source_support"][0].update(source="amazon"), "AI_SOURCE_SUPPORT_SOURCES_INVALID"),
            (lambda row: row["source_support"][0].update(references=[]), "AI_SOURCE_SUPPORT_UNSUPPORTED"),
            (lambda row: row["source_support"][0].update(basis="not_stated"), "AI_SOURCE_SUPPORT_UNSUPPORTED")):
            _, _, payload, response = self.answer(); mutate(response["results"][0])
            self.assertEqual(validate_results("E", payload, response)["failures"]["c000"], failure)

    def test_actual_captured_reviews_dates_price_and_scope_are_supplied_without_score_targets(self):
        memory, refs, payload, _ = self.answer(); supplied = payload["items"][0]
        self.assertEqual(supplied["observations"][0]["captured_facts"]["review_count"], 78)
        self.assertEqual(supplied["observations"][0]["observed_at"], NOW)
        self.assertEqual(supplied["observations"][0]["source"], "flipkart")
        self.assertNotIn("fit", supplied); self.assertNotIn("opportunity_priority", supplied)
        self.assertEqual(memory.state["observations"][refs[0]]["facts"]["price"]["displayed"], "₹149")

    def test_one_supported_listing_context_enters_tier_three_and_references_its_saved_opinion(self):
        memory, refs = self.fixture(); record(memory, refs)
        pool = build_report_pool(memory, NOW, fit_policy_id=FIT_POLICY_ID)
        self.assertEqual(pool["eligible"][0]["opportunity_priority"], 3)
        self.assertEqual(pool["eligible"][0]["opportunity_basis"]["source_ids"], ["flipkart"])
        fact = pool["facts_by_concept"]["c000"]
        self.assertIn("support-fixture", fact["market_review_ids"])
        self.assertTrue(set(refs) <= {row["id"] for row in fact["source_refs"]})
        self.assertEqual(candidate_summary(memory, as_of=NOW)["products"][0]["market"]["positive_families"], 0)

    def test_plain_inventory_or_unsupported_old_relevance_remains_one_nonpositive_source(self):
        memory, _ = self.fixture()
        product = candidate_summary(memory, as_of=NOW)["products"][0]
        self.assertEqual(product["market"]["positive_sources"], 0)
        self.assertFalse(product["opportunity"]["eligible"])

    def test_changed_source_description_stale_and_future_support_cannot_certify_current_source(self):
        for mutation in (
            lambda memory, refs, saved: saved.update(description_hash="wrong"),
            lambda memory, refs, saved: saved.update(reviewed_at=(timestamp(NOW) + timedelta(seconds=1)).isoformat()),
            lambda memory, refs, saved: memory.state["observations"][refs[0]]["facts"].update(review_count=0),
            lambda memory, refs, saved: memory.state["observations"][refs[0]].update(observed_at="2026-09-01T10:00:00+00:00")):
            memory, refs = self.fixture(); saved = record(memory, refs); mutation(memory, refs, saved)
            self.assertEqual(current_source_support(memory.state, "c000", as_of=NOW), [])

    def test_newer_neutral_support_does_not_cherry_pick_older_positive(self):
        memory, refs = self.fixture(); record(memory, refs, at="2026-10-06T09:59:59+00:00")
        record(memory, refs, review_id="new-neutral", judgment="neutral")
        product = candidate_summary(memory, as_of=NOW)["products"][0]
        self.assertFalse(product["opportunity"]["eligible"])
        self.assertEqual(product["market"]["source_support"][0]["review_id"], "new-neutral")

    def test_flat_nonzero_chart_can_support_one_source_without_rewriting_old_growth_arithmetic(self):
        memory, refs = self.fixture(trends=True); record(memory, refs, source="google-trends")
        product = candidate_summary(memory, as_of=NOW)["products"][0]
        self.assertFalse(product["market"]["families"]["trends"]["positive"])
        self.assertEqual(product["market"]["sources"]["google-trends"]["evidence_support"], "reasonably_positive")
        self.assertEqual(product["opportunity"]["opportunity_priority"], 3)
        pool = build_report_pool(memory, NOW, fit_policy_id=FIT_POLICY_ID)
        self.assertIn("support-fixture", pool["facts_by_concept"]["c000"]["market_review_ids"])

    def test_all_zero_chart_is_not_promoted_to_positive_by_an_opinion(self):
        memory, refs = self.fixture(trends=True)
        for point in memory.state["trends_evidence"][refs[0]]["parsed"]["series"]:
            point.update(value=0, lower=0, upper=0)
        record(memory, refs, source="google-trends")
        product = candidate_summary(memory, as_of=NOW)["products"][0]
        self.assertEqual(product["market"]["positive_sources"], 0)
        self.assertFalse(product["opportunity"]["eligible"])
        signal = product["market"]["sources"]["google-trends"]
        self.assertEqual(signal["support_judgment"], "unclear")
        self.assertFalse(signal["support_accepted"])
        self.assertIn("not proof of absent demand", signal["support_reason"])

    def test_support_opinion_and_chart_only_support_are_revision_bound(self):
        for trends, source in ((False, "flipkart"), (True, "google-trends")):
            memory, refs = self.fixture(trends=trends); saved = record(memory, refs, source=source)
            before = selection_scope(memory.state, "c000")
            saved["source_support"][0]["reason"] = "A different saved contextual conclusion."
            self.assertNotEqual(selection_scope(memory.state, "c000"), before)

    def test_frozen_dataset_and_writer_preserve_inferred_market_support(self):
        memory = current_fixture(10); only_amazon(memory); memory.state["market_reviews"].pop("review0")
        refs = [context(memory, "flipkart")]; record(memory, refs)
        _, issue, dataset = selection(memory)
        validate_dataset(issue, dataset)
        product = next(row for row in dataset["products"] if row["concept_id"] == "c000")
        interpretation = next(row for row in product["market_checks"] if row.get("interpretation_only"))
        self.assertEqual(interpretation["basis"], "inferred")
        self.assertTrue(interpretation["detail"].startswith("Estimated:"))
        writing = make_narrative_input(dataset, "en")
        item = next(row for row in writing["items"] if row["id"] == "c000")
        source_fact = next(row for row in item["facts"] if "flipkart interpretation" in row["text"])
        self.assertEqual(source_fact["basis"], "inferred")
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            writing_memory = ResearchMemory(root / "research", Journal(root / "finance"), evidence_type="synthetic")
            rendered = generate_report_narratives(writing_memory, issue, dataset, WritingFixture())
            self.assertEqual(rendered["failures"], {})
            self.assertIn("Estimated:", render_html(issue, rendered["dataset"], language="en"))

    def test_saved_single_site_chart_is_reviewed_once_without_collection_or_checklist_changes(self):
        fixture, _ = self.fixture(trends=True)
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory); memory = ResearchMemory(root / "research", Journal(root / "finance"), evidence_type="synthetic")
            memory.commit("seed", [change(table, record_id, value) for table, rows in fixture.state.items() for record_id, value in rows.items()])
            before = memory.snapshot(); runner = Runner("reasonably_positive")
            self.assertEqual(_questions(before, as_of=NOW)[0]["kind"], "saved_source_support")
            result = review_source_results(memory, runner, as_of=NOW)
            self.assertEqual(result["remaining_questions"], 0)
            after = memory.snapshot()
            self.assertEqual(before["captures"], after["captures"])
            self.assertEqual(before["trends_evidence"], after["trends_evidence"])
            self.assertEqual(before["assessments"], after["assessments"])
            self.assertEqual(before["query_tasks"], after["query_tasks"])
            review_source_results(memory, runner, as_of=NOW)
            self.assertEqual(len(runner.calls), 1)
            self.assertEqual(candidate_summary(memory, as_of=NOW)["products"][0]["opportunity"]["opportunity_priority"], 3)

    def test_run_scoped_support_uses_real_saved_targets_not_invocation_concept_ids(self):
        fixture, _ = self.fixture()
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory); memory = ResearchMemory(root / "research", Journal(root / "finance"), evidence_type="synthetic")
            memory.commit("seed", [change(table, record_id, value) for table, rows in fixture.state.items() for record_id, value in rows.items()])
            prepare_all_source_tasks(memory, run_id="support-run", concept_ids=["c000"], as_of=NOW,
                request_builder=lambda context: {"query": "sink caddy", "country": "IN"})
            before = memory.snapshot()
            for task in before["query_tasks"].values():
                self.assertNotIn("concept_ids", task)
                self.assertEqual(task["targets"], {"c000": digest(before["concepts"]["c000"]["description"])})
            self.assertEqual(len(_questions(before, run_id="support-run", as_of=NOW)), 1)
            self.assertEqual(_questions(before, run_id="other-run", as_of=NOW), [])
            runner = Runner("neutral")
            completed = review_source_results(memory, runner, run_id="support-run", as_of=NOW)
            self.assertEqual(completed["remaining_questions"], 0)
            self.assertEqual(len(runner.calls), 1)
            self.assertEqual(memory.snapshot()["query_tasks"], before["query_tasks"])
            review_source_results(memory, runner, run_id="support-run", as_of=NOW)
            self.assertEqual(len(runner.calls), 1)

    def test_run_scoped_support_never_inherits_an_old_description_target(self):
        fixture, _ = self.fixture()
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory); memory = ResearchMemory(root / "research", Journal(root / "finance"), evidence_type="synthetic")
            memory.commit("seed", [change(table, record_id, value) for table, rows in fixture.state.items() for record_id, value in rows.items()])
            prepare_all_source_tasks(memory, run_id="support-run", concept_ids=["c000"], as_of=NOW,
                request_builder=lambda context: {"query": "sink caddy", "country": "IN"})
            original = memory.snapshot()["concepts"]["c000"]
            changed = deepcopy(original); changed["description"]["features"].append("changed product feature")
            memory.put("concepts", "c000", changed, previous=original)
            runner = Runner()
            result = review_source_results(memory, runner, run_id="support-run", as_of=NOW)
            self.assertEqual(result["remaining_questions"], 0)
            self.assertEqual(runner.calls, [])
            self.assertEqual(memory.snapshot()["market_reviews"], fixture.state["market_reviews"])

    def test_malformed_persisted_target_scope_is_not_silently_an_empty_run(self):
        fixture, _ = self.fixture()
        fixture.state["query_tasks"]["malformed"] = {"kind": "all_source_task_v1", "run_id": "support-run", "status": "not_attempted"}
        with self.assertRaisesRegex(SafetyError, "SOURCE_REVIEW_TASK_BINDING_INVALID"):
            _questions(fixture.state, run_id="support-run", as_of=NOW)

    def test_bounded_neutral_support_finishes_even_when_current_context_has_more_than_one_slice(self):
        fixture, _ = self.fixture()
        for number in range(3): context(fixture, "flipkart", suffix=str(number))
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory); memory = ResearchMemory(root / "research", Journal(root / "finance"), evidence_type="synthetic")
            memory.commit("seed", [change(table, record_id, value) for table, rows in fixture.state.items() for record_id, value in rows.items()])
            runner = Runner("neutral")
            result = review_source_results(memory, runner, as_of=NOW, per_product=1)
            self.assertEqual(result["remaining_questions"], 0)
            self.assertEqual(runner.calls[0]["items"][0]["source_scope"]["omitted_context_count"], 3)
            self.assertFalse(candidate_summary(memory, as_of=NOW)["products"][0]["opportunity"]["eligible"])
            review_source_results(memory, runner, as_of=NOW, per_product=1)
            self.assertEqual(len(runner.calls), 1)


if __name__ == "__main__":
    unittest.main()
