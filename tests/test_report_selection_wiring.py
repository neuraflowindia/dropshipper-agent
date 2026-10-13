"""Saved accepted facts survive selection, dataset construction and writing.

These are synthetic joins, not proof of live model prose or translation.
"""

from copy import deepcopy
from pathlib import Path
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parent))
from test_report_selection import NOW, OBSERVED, fixture, supplier, supplier_price
from product_radar.issue_allocation import allocate_issues, allocation_snapshot_hash, reserve_issue
from product_radar.live_outputs import build_issue_dataset, price_display
from product_radar.models import SafetyError, digest
from product_radar.providers.trends_browser import parse_trends_csv
from product_radar.report_narration import make_narrative_input, validate_narration_result
from product_radar.report_selection import build_report_pool, selection_scope
from product_radar.research_memory import ResearchMemory
from product_radar.storage import Journal
from test_trends_browser import metadata, weekly


def dataset(memory):
    pool = build_report_pool(memory, NOW)
    issue = allocate_issues(pool["eligible"], [{"date": "2026-10-07", "issue_id": "fixture-wiring", "revision": 1, "client_id": "default"}], [], [])["issues"][0]
    facts = [pool["facts_by_concept"][row["concept_id"]] for row in issue["products"]]
    return issue, build_issue_dataset(issue, facts, evidence_type="synthetic")


class SelectionWritingTests(unittest.TestCase):
    def test_accepted_benefit_reason_basis_and_citations_reach_the_writer_unchanged(self):
        memory = fixture(10)
        criterion = memory.state["assessments"]["c000"]["result"]["criteria"][7]
        criterion.update(reason="The accepted use may save preparation time.", basis="inferred")
        _, report = dataset(memory)
        product = next(row for row in report["products"] if row["concept_id"] == "c000")
        benefit = next(row for row in product["writing_facts"] if row["text"] == criterion["reason"])
        self.assertEqual(benefit, {"text": criterion["reason"], "basis": "inferred", "references": ["meta000"]})
        payload = make_narrative_input(report, "en")
        item = next(row for row in payload["items"] if row["id"] == "c000")
        passed = next(row for row in item["facts"] if row["text"] == criterion["reason"])
        self.assertEqual(passed["basis"], "inferred")
        self.assertEqual(passed["references"], ["meta000"])
        self.assertLessEqual(len(product["writing_facts"]), 6)

    def test_margin_and_unclear_or_unreferenced_criteria_are_not_new_writing_facts(self):
        memory = fixture(10)
        criteria = memory.state["assessments"]["c000"]["result"]["criteria"]
        criteria[9].update(reason="No supported advantage is known.", state="Unclear", basis="not_stated", references=[])
        _, report = dataset(memory)
        writing = next(row for row in report["products"] if row["concept_id"] == "c000")["writing_facts"]
        self.assertFalse(any(row["text"] == criteria[9]["reason"] for row in writing))
        self.assertTrue(all(row["references"] and row["basis"] in ("observed", "inferred") for row in writing))
        self.assertEqual(len(report["products"][0]["criteria"]), 13)
        self.assertEqual(report["products"][0]["kpi_states"][2], "Not assessed")

    def test_completed_supplier_search_with_no_relevant_lead_has_consistent_wording(self):
        memory = fixture(10)
        supplier(memory)["result"]["relevance"].update(relationship="unrelated", reason="Different use.")
        _, report = dataset(memory)
        payload = make_narrative_input(report, "en")
        item = next(row for row in payload["items"] if row["id"] == "c000")
        self.assertEqual(item["required_limitation"], "No supplier lead was found in the saved research.")
        product = next(row for row in report["products"] if row["concept_id"] == "c000")
        self.assertEqual(product["supplier_search_status"], "complete")
        self.assertEqual(product["supplier_leads_checked"], 1)
        self.assertEqual(product["supplier_leads_found"], 0)
        self.assertEqual(product["supplier_decisions"][0]["reason"], "Different use.")

    def test_unknown_currency_range_keeps_original_quote_and_supplier_review_date(self):
        memory = fixture(10)
        quote = supplier_price(amount=None, currency=None, quoted_amount="2,500 - 5,500",
            amount_range={"minimum": "2500", "maximum": "5500"}, quantity_condition="25 sets")
        supplier(memory)["result"]["prices"] = [quote]
        _, report = dataset(memory)
        product = next(row for row in report["products"] if row["concept_id"] == "c000")
        self.assertFalse(any(row["kind"] == "supplier" for row in product["prices"]))
        self.assertIn("2,500 - 5,500", product["suppliers"][0]["reason"])
        self.assertIn("currency not stated", product["suppliers"][0]["reason"])
        raw = product["supplier_price_observations"][0]
        self.assertEqual(raw["amount_range"], quote["amount_range"])
        self.assertEqual(raw["quotes"], quote["quotes"])
        self.assertEqual(raw["reviewed_at"], NOW)
        self.assertIsNone(product["suppliers"][0]["source_capture_at"])

    def test_known_currency_range_and_dated_family_retail_price_survive_without_unit_guessing(self):
        memory = fixture(10)
        memory.state["market_reviews"]["review0"]["relevance"][0]["relationship"] = "relevant_family"
        supplier(memory)["result"]["prices"] = [supplier_price(amount=None, quoted_amount="100-200", amount_range={"minimum": "100", "maximum": "200"})]
        _, report = dataset(memory)
        product = next(row for row in report["products"] if row["concept_id"] == "c000")
        retail = next(row for row in product["prices"] if row["kind"] == "retail")
        self.assertEqual(retail["observed_at"], OBSERVED)
        self.assertEqual(retail["relationship"], "relevant_family")
        self.assertEqual(retail["relevance_reason"], "Fixture matching use.")
        self.assertEqual(retail["unit"], "not_stated")
        quoted = next(row for row in product["prices"] if row["kind"] == "supplier")
        self.assertIsNone(quoted["amount"])
        self.assertIn("100-200", price_display(quoted))
        self.assertEqual(quoted["date_basis"], "supplier_interpretation_review")

    def test_roman_hinglish_trial_receives_same_facts_and_source_english_with_no_research(self):
        _, report = dataset(fixture(10))
        before = deepcopy(report)
        english = {"title": "Useful product picks", "products": [{"concept_id": row["concept_id"],
            "reason": "{{title}} may save preparation time.", "points": [], "limitation": "", "update_sentence": ""} for row in report["products"]]}
        payload = make_narrative_input(report, "hi-Latn", english=english)
        item = next(row for row in payload["items"] if row["id"] == "c000")
        self.assertEqual(item["language"], "hi-Latn")
        self.assertEqual(item["source_english"], english["products"][0])
        self.assertEqual(report, before)
        response = {"id": item["id"], "issue_id": item["issue_id"], "language": "hi-Latn", "title": "",
            "reason": "{{title}} se taiyari mein waqt bach sakta hai.", "points": [],
            "limitation": item["required_limitation"], "update_sentence": "", "references": ["benefit-0"]}
        self.assertEqual(validate_narration_result(item, response), response)
        with self.assertRaisesRegex(SafetyError, "ROMAN_HINGLISH"):
            validate_narration_result(item, dict(response, reason="{{title}} \u092f\u0939 \u0905\u091a\u094d\u091b\u093e \u0939\u0948"))

    def test_new_items_do_not_receive_a_made_up_update_sentence_or_image(self):
        memory = fixture(10)
        memory.state["concepts"]["c000"].update(update_reason="A different report date.", material_update_reviewed=True)
        _, report = dataset(memory)
        product = next(row for row in report["products"] if row["concept_id"] == "c000")
        self.assertEqual(product["status"], "New")
        self.assertIsNone(product["image_path"])
        self.assertNotIn("update_reason", product)
        item = next(row for row in make_narrative_input(report, "en")["items"] if row["id"] == "c000")
        self.assertEqual(item["update_fact"], "")

    def test_unavailable_stale_third_source_does_not_expire_two_fresh_sources(self):
        memory = fixture()
        memory.state["trends_evidence"]["old-trend"] = {"concept_id": "c000",
            "parsed": parse_trends_csv(weekly(), metadata(collected_at="2026-09-20T10:00:00+00:00"))}
        pool = build_report_pool(memory, NOW)
        self.assertEqual(len(pool["eligible"]), 1)
        self.assertEqual(pool["eligible"][0]["usable_until"], "2026-10-12T10:00:00+00:00")
        fact = pool["product_facts"][0]
        self.assertEqual(next(check for check in fact["market_checks"] if check["source"] == "trends")["state"], "Stale")
        self.assertIn("old-trend", pool["eligible"][0]["source_revisions"]["trends_evidence"])

    def test_changed_used_record_blocks_real_issue_reservation(self):
        saved = fixture(10).state
        association_id = next(i for i, row in saved["associations"].items() if row["concept_id"] == "c000")
        mutations = [("concepts", "c000"), ("assessments", "c000"), ("associations", association_id),
                     ("market_reviews", "review0"),
                     ("supplier_assessments", "supplier-record0"), ("descriptions", "semantic000")]
        for table, record_id in mutations:
            with self.subTest(table=table), tempfile.TemporaryDirectory() as directory:
                memory = ResearchMemory(Path(directory) / "research", Journal(Path(directory) / "journal"), evidence_type="synthetic")
                changes = [{"table": name, "id": key, "value": value, "previous_hash": None}
                           for name, rows in saved.items() for key, value in rows.items()]
                memory.commit("fixture-load", changes)
                pool = build_report_pool(memory, NOW)
                issue = allocate_issues(pool["eligible"], [{"date": "2026-10-07", "issue_id": "fixture-hold", "revision": 1, "client_id": "default"}], [], [])["issues"][0]
                snapshot_hash = allocation_snapshot_hash(memory.snapshot())
                old = memory.snapshot()[table][record_id]
                memory.commit("fixture-change", [{"table": table, "id": record_id, "value": dict(old, changed_fixture_fact=True), "previous_hash": digest(old)}])
                with self.assertRaisesRegex(SafetyError, "ISSUE_SOURCE_FACTS_CHANGED"):
                    reserve_issue(memory, issue, expected_input_hash=issue["input_hash"], expected_snapshot_hash=snapshot_hash)

    def test_unrelated_records_are_not_folded_into_global_source_hashes(self):
        memory = fixture(2)
        pool = build_report_pool(memory, NOW)
        product = next(row for row in pool["eligible"] if row["concept_id"] == "c000")
        revisions = product["source_revisions"]
        self.assertEqual(set(revisions["concepts"]), {"c000"})
        self.assertEqual(set(revisions["assessments"]), {"c000"})
        self.assertEqual(set(revisions["market_reviews"]), {"review0"})
        self.assertEqual(set(revisions["supplier_assessments"]), {"supplier-record0"})
        self.assertNotIn("meta001", revisions["observations"])
        self.assertNotIn("amazon001", revisions["observations"])

    def test_inserted_market_review_changes_scoped_hash_without_changing_old_record(self):
        memory = fixture()
        original = deepcopy(memory.state["market_reviews"]["review0"])
        before = digest(selection_scope(memory.state, "c000"))
        # Same input/time, but an accepted later judgment with a lexically
        # smaller ID must still be recognised through authoritative event order.
        later = deepcopy(original)
        later["relevance"][0].update(relationship="unrelated", reason="Accepted correction.")
        memory.state["market_reviews"]["a-new-review"] = later
        self.assertNotEqual(digest(selection_scope(memory.state, "c000")), before)
        self.assertEqual(memory.state["market_reviews"]["review0"], original)
        self.assertEqual(set(selection_scope(memory.state, "c000")["market_reviews"]), {"a-new-review"})

    def test_inserted_current_supplier_review_changes_scoped_hash_but_unrelated_insert_does_not(self):
        memory = fixture(2)
        before = digest(selection_scope(memory.state, "c000"))
        unrelated = deepcopy(memory.state["supplier_assessments"]["supplier-record1"])
        unrelated["checked_at"] = "2026-10-06T11:00:00+00:00"
        memory.state["supplier_assessments"]["unrelated-later"] = unrelated
        self.assertEqual(digest(selection_scope(memory.state, "c000")), before)
        later = deepcopy(memory.state["supplier_assessments"]["supplier-record0"])
        later["checked_at"] = "2026-10-06T11:00:00+00:00"
        later["result"]["terms"][0]["value"] = "Accepted changed term"
        memory.state["supplier_assessments"]["new-supplier-review"] = later
        self.assertNotEqual(digest(selection_scope(memory.state, "c000")), before)

    def test_job_progress_and_completion_metadata_are_not_part_of_scoped_facts(self):
        memory = fixture()
        before = digest(selection_scope(memory.state, "c000"))
        memory.state["jobs"]["other-job"] = {"state": "completed", "completed_at": NOW}
        memory.state["assessments"]["c000"]["job_id"] = "other-job"
        memory.state["market_reviews"]["review0"]["job_id"] = "other-job"
        memory.state["supplier_assessments"]["supplier-record0"]["job_id"] = "other-job"
        self.assertEqual(digest(selection_scope(memory.state, "c000")), before)

    def test_new_observation_for_same_source_replaces_old_support_in_scoped_facts(self):
        memory = fixture()
        before = digest(selection_scope(memory.state, "c000"))
        replacement = deepcopy(memory.state["observations"]["meta000"])
        replacement.update(observed_at="2026-10-06T11:00:00+00:00", semantic_id="another-product")
        memory.state["observations"]["new-observation"] = replacement
        scope = selection_scope(memory.state, "c000")
        self.assertNotEqual(digest(scope), before)
        self.assertNotIn("meta000", scope["observations"])
        self.assertNotIn("new-observation", scope["observations"])

    def test_ready_pool_exposes_exact_scoped_hash_for_reservation_check(self):
        memory = fixture()
        pool = build_report_pool(memory, NOW)
        self.assertEqual(pool["eligible"][0]["source_scope_hash"], digest(selection_scope(memory.state, "c000")))


if __name__ == "__main__":
    unittest.main()
