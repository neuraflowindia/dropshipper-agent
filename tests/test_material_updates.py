"""Synthetic comparison and selection proof, not live supplier/publication proof."""

from copy import deepcopy
from pathlib import Path
import tempfile
import unittest

from test_report_selection import fixture, supplier, stated, NOW, OBSERVED
from product_radar.issue_allocation import allocate_issues, allocation_snapshot_hash, reserve_issue
from product_radar.live_outputs import build_issue_dataset
from product_radar.material_updates import (material_snapshot, find_material_changes,
    latest_published_baseline, review_supplier_update, update_baseline_current)
from product_radar.models import digest, SafetyError
from product_radar.report_selection import build_report_pool, selection_scope
from product_radar.research_memory import ResearchMemory, change
from product_radar.storage import Journal


def publish_fixture(memory, *, issue_id="previous", at="2026-10-06T09:00:00Z", concepts=None):
    """Place allocator-frozen synthetic facts in simulated real-history shape."""
    pool = build_report_pool(memory, as_of=NOW)
    eligible = [row for row in pool["eligible"] if concepts is None or row["concept_id"] in concepts]
    issue = allocate_issues(eligible, [{"issue_id": issue_id, "date": "2026-10-06", "client_id": "default"}], {}, {})["issues"][0]
    memory.state["issue_reservations"][issue_id] = dict(issue, state="cancelled")
    for product in issue["products"]:
        memory.state["published_history"][issue_id + "-" + product["concept_id"]] = {
            "status": "published", "client_id": "default", "concept_id": product["concept_id"],
            "issue_id": issue_id, "revision": issue["revision"], "available_at": at,
            "evidence_fingerprint": issue["evidence_fingerprint"],
            "activation_receipt": {"verified": True, "authority": "release_index", "selection_hash": issue["selection_hash"]}}
    return issue


def enable_dispatch(memory, n=0):
    for term in supplier(memory, n)["result"]["terms"]:
        if term["field"] in ("single_piece_purchase", "direct_customer_dispatch"):
            term.update(value="Yes", basis="observed", references=[f"page{n}"],
                        quotes=[{"page_id": f"page{n}", "excerpt": "Fixture terms only."}])
    supplier(memory, n)["source_dates"] = {f"page{n}": OBSERVED}


class MaterialUpdateTests(unittest.TestCase):
    def test_first_new_selection_contains_compact_baseline_but_client_data_does_not(self):
        memory = fixture(10)
        pool = build_report_pool(memory, as_of=NOW)
        issue = allocate_issues(pool["eligible"], ["2026-10-06"], {}, {})["issues"][0]
        self.assertTrue(issue["ready_for_review"])
        dataset = build_issue_dataset(issue, pool["product_facts"], evidence_type="synthetic")
        for frozen, public in zip(issue["products"], dataset["products"]):
            self.assertEqual(frozen["material_facts_hash"], digest(frozen["material_facts"]))
            self.assertNotIn("material_facts", public)
            self.assertNotIn("material_update_review", public)
            self.assertNotIn("source_revisions", public)

    def test_dates_seller_names_source_ids_colour_display_and_order_do_not_make_changes(self):
        memory = fixture()
        pool = build_report_pool(memory, as_of=NOW)
        fact = deepcopy(pool["product_facts"][0])
        market = {"label": "sufficient", "families": {}}
        before = material_snapshot(fact, market)
        fact["source_refs"] = [{"id": "different"}]
        fact["suppliers"][0].update(name="Another seller", url="https://different.example.org", reviewed_at=NOW)
        fact["criteria"].reverse()
        fact["prices"].reverse()
        fact.update(image_path="blue-version.png", title="NEW NAME", language="hi-Latn")
        fact["suppliers"].append(deepcopy(fact["suppliers"][0]))
        self.assertEqual(find_material_changes(before, material_snapshot(fact, market)), [])

    def test_new_substantiated_customer_dispatch_is_useful_not_verified_fulfilment(self):
        memory = fixture()
        publish_fixture(memory)
        enable_dispatch(memory)
        before = deepcopy(memory.state)
        result = build_report_pool(memory, as_of=NOW)
        self.assertEqual(memory.state, before)
        row = result["eligible"][0]
        self.assertEqual(row["status"], "Updated")
        review = row["material_update_review"]
        self.assertTrue(review["accepted"])
        self.assertEqual(review["decision"], "useful_update")
        self.assertIn("without assuming verified fulfilment", review["practical_consequence"])
        self.assertTrue(review["old_evidence_ids"])
        self.assertTrue(review["new_evidence_ids"])
        self.assertEqual([change["field_id"] for change in review["changes"]], ["suppliers"])
        self.assertNotIn("snapshot", review["baseline"])
        self.assertNotIn("current", review)
        self.assertTrue(update_baseline_current(row, memory.state["published_history"], memory.state["issue_reservations"], "default"))

    def test_repeat_existing_dispatch_capability_is_not_a_useful_update(self):
        memory = fixture(); enable_dispatch(memory); publish_fixture(memory)
        supplier(memory)["checked_at"] = NOW
        result = build_report_pool(memory, as_of=NOW)
        self.assertEqual(result["eligible"], [])
        self.assertEqual(result["decisions"][0]["useful_update_decision"], "not_material")

    def test_purchase_only_bulk_requirement_unrelated_or_stale_supply_is_not_admitted(self):
        for mutation in (lambda mem: stated(mem, "direct_customer_dispatch", "Not stated"),
                         lambda mem: stated(mem, "bulk_stock_prerequisite", "Yes"),
                         lambda mem: supplier(mem)["result"]["relevance"].update(relationship="unrelated"),
                         lambda mem: supplier(mem).update(source_dates={"page0": "2026-09-01T10:00:00Z"})):
            with self.subTest(mutation=mutation):
                memory = fixture(); publish_fixture(memory); enable_dispatch(memory); mutation(memory)
                result = build_report_pool(memory, as_of=NOW)
                self.assertEqual(result["eligible"], [])
                self.assertNotEqual(result["decisions"][0]["useful_update_decision"], "useful_update")

    def test_material_price_change_requires_focused_review_not_automatic_usefulness(self):
        memory = fixture(); publish_fixture(memory)
        memory.state["observations"]["amazon000"]["facts"]["price"]["displayed"] = 149
        result = build_report_pool(memory, as_of=NOW)
        self.assertEqual(result["eligible"], [])
        self.assertEqual(result["decisions"][0]["decision"], "deferred")
        self.assertEqual(result["decisions"][0]["useful_update_decision"], "review_needed")
        self.assertIn("prices", result["needed_fact_work"][0]["changed_fields"])

    def test_legacy_missing_comparison_defers_only_that_product(self):
        memory = fixture(2)
        memory.state["published_history"]["legacy"] = {"status": "published", "client_id": "default", "concept_id": "c000"}
        enable_dispatch(memory)
        result = build_report_pool(memory, as_of=NOW)
        self.assertEqual([row["concept_id"] for row in result["eligible"]], ["c001"])
        self.assertEqual(result["decisions"][0]["decision"], "deferred")
        self.assertEqual(result["decisions"][0]["useful_update_decision"], "baseline_unavailable")

    def test_current_checklist_and_market_still_apply_to_supplier_update(self):
        for field in ("checklist", "market"):
            memory = fixture(); publish_fixture(memory); enable_dispatch(memory)
            if field == "checklist":
                for criterion in memory.state["assessments"]["c000"]["result"]["criteria"]:
                    if criterion["id"] in (1, 2, 4):
                        criterion["state"] = "Unclear"
            else:
                memory.state["market_reviews"].clear()
            result = build_report_pool(memory, as_of=NOW)
            self.assertEqual(result["eligible"], [])
            self.assertEqual(result["decisions"][0]["useful_update_decision"], "useful_update")

    def test_real_latest_publication_controls_comparison_even_when_cancelled(self):
        memory = fixture(); publish_fixture(memory, issue_id="older"); enable_dispatch(memory)
        updated = build_report_pool(memory, as_of=NOW)["eligible"][0]
        issue = allocate_issues([updated], ["2026-10-06"], memory.state["published_history"], memory.state["issue_reservations"])["issues"][0]
        memory.state["issue_reservations"][issue["issue_id"]] = dict(issue, state="cancelled")
        memory.state["published_history"]["newer"] = dict(memory.state["published_history"]["older-c000"],
            issue_id=issue["issue_id"], revision=issue["revision"], available_at="2026-10-06T09:30:00Z",
            evidence_fingerprint=issue["evidence_fingerprint"],
            activation_receipt={"verified": True, "authority": "release_index", "selection_hash": issue["selection_hash"]})
        baseline = latest_published_baseline(memory.state, "c000", "default", NOW)
        self.assertEqual(baseline["record_id"], "newer")
        self.assertEqual(build_report_pool(memory, as_of=NOW)["eligible"], [])
        self.assertFalse(update_baseline_current(updated, memory.state["published_history"], memory.state["issue_reservations"], "default"))

    def test_saved_update_is_rejected_after_newer_publication_or_baseline_change(self):
        for mutation in ("new_history", "changed_reservation", "tampered_current"):
            memory = fixture(); publish_fixture(memory); enable_dispatch(memory)
            row = build_report_pool(memory, as_of=NOW)["eligible"][0]
            if mutation == "new_history":
                memory.state["published_history"]["newer"] = dict(memory.state["published_history"]["previous-c000"], available_at=NOW)
            elif mutation == "changed_reservation":
                memory.state["issue_reservations"]["previous"]["state"] = "published"
            else:
                row["material_facts"]["suppliers"] = []
            result = allocate_issues([row], ["2026-10-06"], memory.state["published_history"], memory.state["issue_reservations"])
            self.assertEqual(result["issues"][0]["products"], [])
            self.assertIn("latest published facts", result["decisions"][0]["reason"])

    def test_corrupt_old_facts_are_reported_without_disrupting_unrelated_products(self):
        memory = fixture(2); publish_fixture(memory, concepts={"c000"}); enable_dispatch(memory)
        memory.state["issue_reservations"]["previous"]["products"][0]["material_facts"]["suppliers"] = []
        result = build_report_pool(memory, as_of=NOW)
        self.assertEqual([row["concept_id"] for row in result["eligible"]], ["c001"])
        self.assertEqual(result["decisions"][0]["useful_update_decision"], "baseline_invalid")

    def test_real_join_fills_complete_ten_with_eight_new_and_two_updates(self):
        memory = fixture(18)
        original = publish_fixture(memory, concepts={f"c{n:03d}" for n in range(10)})
        self.assertTrue(original["ready_for_review"])
        enable_dispatch(memory); enable_dispatch(memory, 1)
        pool = build_report_pool(memory, as_of=NOW)
        selected = allocate_issues(pool["eligible"], ["2026-10-06"], memory.state["published_history"], memory.state["issue_reservations"])["issues"][0]
        self.assertEqual(selected["counts"], {"products": 10, "new": 8, "updates": 2})
        self.assertTrue(selected["ready_for_review"])
        selected_ids = {row["concept_id"] for row in selected["products"]}
        dataset = build_issue_dataset(selected, [fact for fact in pool["product_facts"] if fact["concept_id"] in selected_ids], evidence_type="synthetic")
        for public in dataset["products"]:
            self.assertNotIn("material_facts", public)
            self.assertNotIn("material_update_review", public)
            if public["status"] == "Updated":
                self.assertTrue(public["material_update_reviewed"])
                self.assertIn("single-piece purchase", public["update_reason"])

    def test_reservation_rechecks_baseline_even_when_caller_supplies_current_snapshot(self):
        memory = fixture(18); publish_fixture(memory, concepts={f"c{n:03d}" for n in range(10)})
        enable_dispatch(memory); enable_dispatch(memory, 1)
        pool = build_report_pool(memory, as_of=NOW)
        issue = allocate_issues(pool["eligible"], ["2026-10-06"], memory.state["published_history"], memory.state["issue_reservations"])["issues"][0]
        memory.state["published_history"]["newer"] = dict(memory.state["published_history"]["previous-c000"], available_at=NOW)
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            saved = ResearchMemory(root / "research", Journal(root / "finance"), evidence_type="synthetic")
            saved.commit("fixture", [change(table, key, value) for table, rows in memory.state.items() for key, value in rows.items()])
            before = saved.snapshot()
            with self.assertRaisesRegex(SafetyError, "ISSUE_USEFUL_UPDATE_BASELINE_CHANGED"):
                reserve_issue(saved, issue, expected_input_hash=issue["input_hash"],
                              expected_snapshot_hash=allocation_snapshot_hash(before), as_of=NOW)
            self.assertEqual(saved.snapshot(), before)

    def test_changed_description_trends_no_longer_changes_selection_scope(self):
        memory = fixture()
        original = deepcopy(memory.state["concepts"]["c000"]["description"])
        memory.state["trends_evidence"]["trend0"] = {"concept_id": "c000", "description_hash": digest(original),
            "parsed": {"state": "Usable", "metadata": {"collected_at": OBSERVED}}}
        self.assertIn("trend0", selection_scope(memory.state, "c000")["trends_evidence"])
        memory.state["concepts"]["c000"]["description"]["function"] = "Changed underlying product use"
        self.assertEqual(selection_scope(memory.state, "c000")["trends_evidence"], {})

    def test_supplier_update_freshness_applies_at_target_release_not_just_today(self):
        memory = fixture(); publish_fixture(memory); enable_dispatch(memory)
        supplier(memory)["source_dates"] = {"page0": "2026-09-07T10:00:00Z"}
        row = build_report_pool(memory, as_of=NOW)["eligible"][0]
        self.assertEqual(row["usable_until"], "2026-10-07T10:00:00+00:00")
        result = allocate_issues([row], ["2026-10-08"], memory.state["published_history"], memory.state["issue_reservations"])
        self.assertEqual(result["issues"][0]["products"], [])

    def test_numeric_trends_change_is_detected_without_automatic_usefulness(self):
        fact = {"concept_id": "product0"}
        market = {"families": {"trends": {"state": "Positive", "available": True, "positive": True,
                                         "previous_mean_bounds": [10.0, 10.0], "recent_mean_bounds": [20.0, 20.0]}}}
        previous = material_snapshot(fact, market)
        market["families"]["trends"]["recent_mean_bounds"] = [25.0, 25.0]
        self.assertEqual([change["field_id"] for change in find_material_changes(previous, material_snapshot(fact, market))], ["market"])


if __name__ == "__main__":
    unittest.main()
