"""The AI per-site market opinion is retired (W7): the relevance prompts no longer
ask for it, nothing saves it, and an opinion in an older saved review changes
nothing (no tier, no support-only question, no report interpretation row)."""

from copy import deepcopy
from pathlib import Path
import tempfile
import unittest

from product_radar.fit_policy import FIT_POLICY_ID
from product_radar.market_validation import REVIEW_VERSION, source_evidence
from product_radar.models import SafetyError, digest
from product_radar.research_ai import (FUNCTIONAL_SOURCE_INSTRUCTIONS, FUNCTIONAL_SOURCE_PROMPT_VERSION, FUNCTIONAL_SOURCE_SCHEMA,
                                      SOURCE_RELEVANCE_INSTRUCTIONS, SOURCE_RELEVANCE_PROMPT_VERSION, e_contract, validate_results)
from product_radar.research_flow import candidate_summary
from product_radar.research_memory import ResearchMemory, change
from product_radar.report_selection import build_report_pool, selection_scope
from product_radar.report_narration import make_narrative_input
from product_radar.live_outputs import validate_dataset
from product_radar.source_interpretation import _questions, review_source_results
from product_radar.storage import Journal
from product_radar.providers.trends_browser import parse_trends_csv
from test_trends_browser import metadata, weekly
from test_fit_pipeline import current_fixture, selection
from test_opportunity_platforms import context, expire_ad as only_amazon
from test_report_selection import NOW


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


def record(memory, refs, *, source="flipkart", judgment="reasonably_positive", review_id="support-fixture", at=NOW):
    """A relevance review saved by the retired prompt, which also carried a per-site opinion."""
    item = {"id": "c000", "description": memory.state["concepts"]["c000"]["description"],
            "observations": [{"id": ref, "source": source} for ref in refs]}
    value = {"concept_id": "c000", "description_hash": digest(item["description"]), "input_hash": digest(item),
             "input": item, "review_version": REVIEW_VERSION, "support_version": "captured-source-support-v3",
             "prompt_version": "research-e-source-support-v3", "reviewed_at": at,
             "evidence_hashes": {ref: digest(source_evidence(memory.state, ref)[2]) for ref in refs},
             "source_support": [{"source": source, "judgment": judgment, "basis": "inferred", "references": refs,
                 "reason": "Estimated: meaningful captured consumer-review context supports moderate interest."}],
             "relevance": [{"id": ref, "relationship": "relevant_family", "reason": "Synthetic matching function."} for ref in refs]}
    memory.state["market_reviews"][review_id] = value
    return value


class Runner:
    identity = "source-relevance-fixture-v1"
    def __init__(self):
        self.calls = []

    def __call__(self, role, payload, folder):
        self.calls.append(deepcopy(payload)); answers = []
        for item in payload["items"]:
            answers.append({"id": item["id"], "criteria": [], "relevance": [
                {"id": alias, "relationship": "relevant_family", "reason": "Synthetic matched object and function."}
                for alias in item["reference_aliases"]], "compliance_note": "", "missing_questions": []})
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

    def test_relevance_prompts_ask_no_per_site_opinion(self):
        retired = {"mode": "marketplace_relevance_only", "criteria": [], "relevance_style": "short-references-support-v3"}
        with self.assertRaisesRegex(SafetyError, "AI_E_CONTRACT_INVALID"):
            e_contract(retired)
        relevance = dict(retired, relevance_style="short-references-v2")
        contract = e_contract(relevance)
        self.assertEqual(contract["version"], SOURCE_RELEVANCE_PROMPT_VERSION)
        self.assertNotIn("source_support", contract["schema"]["properties"]["results"]["items"]["properties"])
        self.assertEqual(FUNCTIONAL_SOURCE_PROMPT_VERSION, "research-e-source-scope-v5")
        self.assertNotIn("source_support", FUNCTIONAL_SOURCE_SCHEMA["properties"]["results"]["items"]["properties"])
        for text in (FUNCTIONAL_SOURCE_INSTRUCTIONS, SOURCE_RELEVANCE_INSTRUCTIONS):
            self.assertNotIn("Movers", text); self.assertNotIn("reasonably_positive", text); self.assertNotIn("source_support", text)
        with self.assertRaisesRegex(SafetyError, "AI_E_CONTRACT_INVALID"):
            e_contract(dict(retired, relevance_style="functional-source-scope-v4"))

    def test_relevance_answer_is_validated_without_any_opinion(self):
        memory, refs = self.fixture()
        item = {"id": "c000", "description": memory.state["concepts"]["c000"]["description"],
                "observations": [{"id": refs[0], "semantic_id": "s", "source": "flipkart", "observed_at": NOW,
                                  "source_text": {"title": "Listing"}, "captured_facts": {}}],
                "reference_aliases": {"R1": refs[0]}}
        payload = {"mode": "marketplace_relevance_only", "criteria": [], "relevance_style": "short-references-v2", "items": [item]}
        response, _ = Runner()("E", payload, None)
        accepted = validate_results("E", payload, response)["accepted"]["c000"]
        self.assertEqual(accepted["relevance"][0]["id"], refs[0])
        self.assertNotIn("source_support", accepted)

    def test_a_saved_positive_opinion_changes_neither_market_facts_nor_score(self):
        # Retired with the priority tiers (W7): the score is measured from facts.
        plain, _ = self.fixture()
        memory, refs = self.fixture(); record(memory, refs)
        product = candidate_summary(memory, as_of=NOW)["products"][0]
        baseline = candidate_summary(plain, as_of=NOW)["products"][0]
        signal = product["market"]["sources"]["flipkart"]
        self.assertTrue(signal["available"])
        self.assertFalse({"evidence_support", "support_judgment", "support_review_id"} & set(signal))
        self.assertNotIn("source_support", product["market"])
        self.assertEqual(product["opportunity"], baseline["opportunity"])
        self.assertFalse(product["opportunity"]["eligible"])
        pool = build_report_pool(memory, NOW, fit_policy_id=FIT_POLICY_ID)
        fact = pool["facts_by_concept"]["c000"]
        self.assertFalse([row for row in fact["market_checks"] if row.get("interpretation_only")])
        self.assertTrue(set(refs) <= {row["id"] for row in fact["source_refs"]})

    def test_no_support_only_question_or_model_call_for_a_single_site_type(self):
        fixture, _ = self.fixture(trends=True)
        self.assertFalse([row for row in _questions(fixture.state, as_of=NOW) if row.get("kind") == "saved_source_support"])
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory); memory = ResearchMemory(root / "research", Journal(root / "finance"), evidence_type="synthetic")
            memory.commit("seed", [change(table, record_id, value) for table, rows in fixture.state.items() for record_id, value in rows.items()])
            before = memory.snapshot(); runner = Runner()
            result = review_source_results(memory, runner, as_of=NOW)
            self.assertEqual(result["remaining_questions"], 0)
            self.assertEqual(runner.calls, [])
            self.assertEqual(memory.snapshot(), before)

    def test_chart_only_opinion_is_not_part_of_the_selection_scope_any_more(self):
        memory, refs = self.fixture(trends=True); saved = record(memory, refs, source="google-trends")
        saved["relevance"] = []  # an opinion with no listing relevance of its own
        before = selection_scope(memory.state, "c000")
        saved["source_support"][0]["reason"] = "A different saved contextual conclusion."
        self.assertEqual(selection_scope(memory.state, "c000"), before)
        product = candidate_summary(memory, as_of=NOW)["products"][0]
        self.assertFalse({"evidence_support", "support_judgment"} & set(product["market"]["sources"]["google-trends"]))

    def test_all_zero_chart_has_no_opinion_and_earns_no_points(self):
        memory, refs = self.fixture(trends=True)
        for point in memory.state["trends_evidence"][refs[0]]["parsed"]["series"]:
            point.update(value=0, lower=0, upper=0)
        record(memory, refs, source="google-trends")
        product = candidate_summary(memory, as_of=NOW)["products"][0]
        signal = product["market"]["sources"]["google-trends"]
        self.assertFalse({"support_judgment", "support_accepted", "support_reason"} & set(signal))
        searches = product["opportunity"]["opportunity_basis"]["parts"]["want"]["items"]["google_searches"]
        self.assertEqual((searches["facts"]["nonzero_points"], searches["points"]), (0, 0))

    def test_frozen_dataset_and_writer_carry_no_market_interpretation_rows(self):
        from test_fit_pipeline import _older_format_inputs
        from unittest.mock import patch
        import product_radar.opportunity_score as score
        memory = current_fixture(10); only_amazon(memory); memory.state["market_reviews"].pop("review0")
        refs = [context(memory, "flipkart")]; record(memory, refs)
        with patch.object(score, "score_inputs", side_effect=_older_format_inputs(score.score_inputs)):
            _, issue, dataset = selection(memory)
        validate_dataset(issue, dataset)
        for product in dataset["products"]:
            self.assertFalse([row for row in product["market_checks"] if row.get("interpretation_only")])
        writing = make_narrative_input(dataset, "en")
        self.assertFalse([row for item in writing["items"] for row in item["facts"] if "interpretation" in row["text"]])

if __name__ == "__main__":
    unittest.main()
