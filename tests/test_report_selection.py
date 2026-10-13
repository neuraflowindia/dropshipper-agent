"""Synthetic join tests, not product demand, supplier or publication proof."""

from copy import deepcopy
from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from product_radar.issue_allocation import allocate_issues
from product_radar.live_outputs import build_issue_dataset
from product_radar.market_validation import REVIEW_VERSION
from product_radar.models import digest
from product_radar.report_selection import build_report_pool
from product_radar.research_memory import TABLES, key
from product_radar.suppliers import SUPPLIER_FIELDS, VERSION

NOW = "2026-10-06T10:00:00+00:00"
OBSERVED = "2026-10-05T10:00:00+00:00"


class MemoryFixture:
    evidence_type = "synthetic"
    def __init__(self, state):
        self.state = state
        self.reads = 0
    def snapshot(self):
        self.reads += 1
        return deepcopy(self.state)


def fixture(count=1):
    state = {table: {} for table in TABLES}
    for n in range(count):
        concept_id, semantic_id, obs_id, amazon_id = f"c{n:03d}", f"semantic{n:03d}", f"meta{n:03d}", f"amazon{n:03d}"
        description = {"label": f"Synthetic product {n}", "category": "Home and Kitchen", "function": f"Fixture use {n}",
                       "mechanism": "Manual fixture mechanism", "audience": "Adult", "features": ["Fixture feature"],
                       "basis": "observed", "reason": "Fixture only.", "brand": "generic_or_small"}
        state["concepts"][concept_id] = {"description": description, "status": "provisional"}
        semantic = {"title": description["label"]}
        state["observations"][obs_id] = {"source": "meta", "source_item": f"ad{n}", "acquisition_id": "fixture-meta", "observed_at": OBSERVED,
            "semantic_id": semantic_id, "semantic": semantic, "facts": {"ad_id": str(n), "advertiser_id": str(n + 100), "active": True,
            "started_at": "2026-09-01T10:00:00+00:00", "url": f"https://example.org/fixture-ad/{n}", "limitations": []}}
        answer = {"id": semantic_id, "kind": "product", "products": [deepcopy(description)], "reason": "Synthetic description."}
        state["descriptions"][semantic_id] = {"result": answer, "job_id": f"job-a{n}"}
        child_id = key("child", [semantic_id, 0, digest(answer)])
        state["associations"][child_id] = {"semantic_id": semantic_id, "concept_id": concept_id, "status": "resolved", "reason": "no_known_match", "method": "no_known_match", "job_id": None}
        item = {"id": concept_id, "description": description, "observations": [{"id": obs_id, "semantic_id": semantic_id, "source_text": semantic}]}
        criteria = [{"id": i, "state": "Not assessed" if i == 3 else "Meets" if i <= 9 else "Unclear",
                     "basis": "not_stated" if i == 3 else "inferred", "references": [] if i == 3 else [obs_id], "reason": "Fixture checklist reason."}
                    for i in range(1, 14)]
        state["assessments"][concept_id] = {"input_hash": digest(item), "job_id": f"job-e{n}", "used_record_ids": [obs_id],
            "result": {"id": concept_id, "criteria": criteria, "relevance": [{"id": obs_id, "relationship": "exact_product", "reason": "Fixture relevance."}],
                       "compliance_note": "Client review where relevant.", "missing_questions": []}, "diagnostics": []}
        state["observations"][amazon_id] = {"source": "amazon", "source_item": f"ASIN{n}", "acquisition_id": "fixture-amazon", "observed_at": OBSERVED,
            "semantic_id": f"amazonsemantic{n}", "semantic": {"title": description["label"], "product_url": f"https://example.org/fixture-listing/{n}"},
            "facts": {"list_type": "bestsellers", "position": n + 1, "limitations": [], "url": f"https://example.org/fixture-listing/{n}",
                      "price": {"displayed": 179, "alternate_displayed": 299, "currency": "INR", "pack_basis": "not_verified",
                                "basis": "retail_listing", "source_url": f"https://example.org/fixture-listing/{n}"}}}
        state["market_reviews"][f"review{n}"] = {"concept_id": concept_id, "review_version": REVIEW_VERSION,
            "description_hash": digest(description), "reviewed_at": NOW, "input_hash": digest(amazon_id),
            "relevance": [{"id": amazon_id, "relationship": "exact_product", "reason": "Fixture matching use."}]}
        supplier_id, page_id = f"supplier{n}", f"page{n}"
        terms = [{"field": field, "value": "Not stated", "basis": "not_stated", "references": [], "quotes": [], "reason": "No fixture term."}
                 for field in SUPPLIER_FIELDS]
        input_item = {"id": supplier_id, "product": dict(description, concept_id=concept_id), "pages": [
            {"id": page_id, "url": f"https://supplier.example.org/fixture/{n}", "title": "Synthetic supplier page", "text": "Fixture terms only."}]}
        state["supplier_assessments"][f"supplier-record{n}"] = {"input_hash": digest(input_item), "input": input_item, "version": VERSION,
            "job_id": f"job-f{n}", "checked_at": NOW, "result": {"id": supplier_id,
            "relevance": {"relationship": "exact_product", "references": [page_id], "reason": "Synthetic product lead."}, "terms": terms, "prices": [],
            "supply_arrangement": "Supply arrangement not stated", "client_ready": False}}
    return MemoryFixture(state)


def supplier(memory, n=0):
    return memory.state["supplier_assessments"][f"supplier-record{n}"]


def stated(memory, field, value):
    term = next(row for row in supplier(memory)["result"]["terms"] if row["field"] == field)
    term.update(value=value, basis="observed", references=["page0"], quotes=[{"page_id": "page0", "excerpt": "Fixture terms only."}])


def supplier_price(**changes):
    return {"amount": "45", "currency": "INR", "unit": "piece", "quantity_condition": "Minimum 100 pieces", "price_kind": "product",
            "qualifier": "Fixture stated rate", "references": ["page0"], "quotes": [{"page_id": "page0", "excerpt": "Fixture terms only."}],
            "reason": "Fixture quote, not margin.", **changes}


class ReportSelectionTests(unittest.TestCase):
    def test_latest_matching_supplier_review_preserves_construction_caveat(self):
        memory = fixture()
        original = deepcopy(memory.state['supplier_assessments']['supplier-record0'])
        original['checked_at'] = '2026-10-05T10:00:00+00:00'
        memory.state['supplier_assessments']['supplier-record0'] = original
        old_pool = build_report_pool(memory, as_of=NOW)
        self.assertEqual(old_pool['facts_by_concept']['c000']['supplier_assessment_ids'], ['supplier-record0'])
        latest = deepcopy(original)
        latest['checked_at'] = NOW
        latest['job_id'] = 'job-f-material-review'
        caveat = 'Construction statements conflict; confirm the actual material with the supplier.'
        latest['result']['relevance']['reason'] = 'Same squeeze mechanism. ' + caveat
        latest['result']['prices'] = [{
            'amount': '110', 'currency': 'INR', 'unit': 'Piece', 'quantity_condition': 'Minimum not stated',
            'price_kind': 'product', 'qualifier': 'Price on request. ' + caveat,
            'references': ['page0'], 'quotes': [{'page_id': 'page0', 'excerpt': 'Rs 110 / Piece'}],
            'reason': caveat}]
        memory.state['supplier_assessments']['supplier-record-new'] = latest
        before = deepcopy(memory.state)
        pool = build_report_pool(memory, as_of=NOW)
        facts = pool['facts_by_concept']['c000']
        self.assertEqual(facts['supplier_assessment_ids'], ['supplier-record-new'])
        self.assertEqual(len(facts['suppliers']), 1)
        self.assertIn(caveat, facts['suppliers'][0]['reason'])
        price = next(row for row in facts['prices'] if row['kind'] == 'supplier')
        self.assertIn(caveat, price['conditions'])
        self.assertEqual(memory.state, before)
        self.assertEqual(memory.state['supplier_assessments']['supplier-record0'], original)

    def test_recorded_zero_lead_search_completes_without_inventing_a_supplier(self):
        memory = fixture()
        memory.state["supplier_assessments"].clear()
        memory.state["source_checks"]["supplier-search0"] = {
            "source": "supplier", "concept_id": "c000", "description_hash": digest(memory.state["concepts"]["c000"]["description"]),
            "state": "usable", "supplier_search_status": "complete", "candidate_count": 0,
            "at": NOW, "reason": "Two readable searches explicitly returned no results.", "reference": "saved-search"}
        result = build_report_pool(memory, as_of=NOW)
        self.assertEqual(len(result["eligible"]), 1)
        fact = result["facts_by_concept"]["c000"]
        self.assertEqual(fact["supplier_search_status"], "complete")
        self.assertEqual(fact["suppliers"], [])
        self.assertEqual(fact["supplier_leads_checked"], 0)
        self.assertEqual(fact["supplier_search_check_ids"], ["supplier-search0"])
        self.assertIn("supplier-search0", result["eligible"][0]["source_revisions"]["source_checks"])

    def test_blocked_unreviewed_or_old_scope_search_does_not_claim_completion(self):
        memory = fixture(); memory.state["supplier_assessments"].clear()
        original = {"source": "supplier", "concept_id": "c000", "description_hash": digest(memory.state["concepts"]["c000"]["description"]),
                    "state": "usable", "supplier_search_status": "complete", "candidate_count": 0,
                    "at": NOW, "reason": "Synthetic search result.", "reference": "saved-search"}
        for changes in ({"state": "blocked"}, {"candidate_count": 3}, {"candidate_count": False},
                        {"description_hash": "changed"}, {"at": "2026-10-07T10:00:00+00:00"}):
            with self.subTest(changes=changes):
                memory.state["source_checks"]["search0"] = dict(original, **changes)
                self.assertEqual(build_report_pool(memory, as_of=NOW)["eligible"], [])

    def test_current_accepted_inputs_join_without_model_or_memory_writes(self):
        memory = fixture()
        before = deepcopy(memory.state)
        result = build_report_pool(memory, as_of=NOW)
        self.assertEqual([row["concept_id"] for row in result["eligible"]], ["c000"])
        self.assertEqual(result["eligible"][0]["kpi_states"][2], "Not assessed")
        self.assertEqual(result["decisions"][0]["decision"], "included")
        self.assertEqual(memory.reads, 1)
        self.assertEqual(memory.state, before)
        self.assertFalse(result["client_ready"])

    def test_unknown_supplier_capabilities_and_compliance_note_do_not_exclude(self):
        result = build_report_pool(fixture(), as_of=NOW)
        fact = result["product_facts"][0]
        self.assertEqual(fact["suppliers"][0]["single_piece_dispatch"], "not_stated")
        self.assertEqual(fact["suppliers"][0]["subscription"], "not_stated")
        self.assertEqual(fact["supplier_search_status"], "complete")
        self.assertEqual(fact["compliance_note"], "Client review where relevant.")

    def test_missing_supplier_work_is_named_not_falsely_completed(self):
        memory = fixture(); memory.state["supplier_assessments"].clear()
        result = build_report_pool(memory, as_of=NOW)
        self.assertEqual(result["eligible"], [])
        self.assertEqual(result["facts_by_concept"]["c000"]["supplier_search_status"], "not_started")
        self.assertEqual(result["needed_supplier_work"][0]["concept_id"], "c000")

    def test_a_search_with_no_relevant_lead_still_satisfies_cursory_search(self):
        memory = fixture(); supplier(memory)["result"]["relevance"].update(relationship="unrelated", reason="Different product.")
        result = build_report_pool(memory, as_of=NOW)
        self.assertEqual(len(result["eligible"]), 1)
        fact = result["product_facts"][0]
        self.assertEqual(fact["suppliers"], [])
        self.assertEqual(fact["supplier_leads_checked"], 1)
        self.assertEqual(fact["supplier_decisions"][0]["reason"], "Different product.")

    def test_supplier_results_require_canonical_id_not_label_guessing(self):
        memory = fixture(); supplier(memory)["input"]["product"].pop("concept_id")
        result = build_report_pool(memory, as_of=NOW)
        self.assertEqual(result["eligible"], [])
        self.assertEqual(result["facts_by_concept"]["c000"]["supplier_leads_checked"], 0)

    def test_future_or_changed_supplier_context_does_not_attach(self):
        for change in (lambda row: row.update(checked_at="2026-10-07T10:00:00Z"),
                       lambda row: row["input"]["product"].update(function="Different purchase unit")):
            memory = fixture(); change(supplier(memory))
            result = build_report_pool(memory, as_of=NOW)
            self.assertEqual(result["eligible"], [])
            self.assertEqual(result["facts_by_concept"]["c000"]["supplier_search_status"], "not_started")

    def test_single_piece_purchase_alone_never_becomes_customer_dispatch(self):
        memory = fixture(); stated(memory, "purchase_moq", "1 piece")
        lead = build_report_pool(memory, as_of=NOW)["product_facts"][0]["suppliers"][0]
        self.assertEqual(lead["min_order"], 1)
        self.assertEqual(lead["single_piece_dispatch"], "not_stated")
        stated(memory, "direct_customer_dispatch", "Yes")
        self.assertEqual(build_report_pool(memory, as_of=NOW)["product_facts"][0]["suppliers"][0]["single_piece_dispatch"], "supported")

    def test_bulk_stock_and_subscription_fields_stay_separate_and_nonblocking(self):
        memory = fixture(); stated(memory, "single_piece_purchase", "Yes"); stated(memory, "direct_customer_dispatch", "Yes")
        stated(memory, "bulk_stock_prerequisite", "Yes"); stated(memory, "subscription_requirement", "Required")
        result = build_report_pool(memory, as_of=NOW)
        self.assertEqual(len(result["eligible"]), 1)
        lead = result["product_facts"][0]["suppliers"][0]
        self.assertEqual(lead["single_piece_dispatch"], "not_supported")
        self.assertEqual(lead["subscription"], "subscription_stated")
        self.assertIn("Bulk stock", lead["supply_arrangement"])

    def test_range_and_unknown_currency_are_not_a_fabricated_single_inr_cost(self):
        memory = fixture()
        supplier(memory)["result"]["prices"] = [supplier_price(amount=None, currency=None, quoted_amount="2,500 - 5,500",
            amount_range={"minimum": "2500", "maximum": "5500"}, quantity_condition="25 sets")]
        result = build_report_pool(memory, as_of=NOW)
        fact = result["product_facts"][0]
        self.assertFalse(any(row["kind"] == "supplier" for row in fact["prices"]))
        self.assertIsNone(fact["supplier_price_observations"][0]["amount"])
        self.assertIn("currency not stated", fact["suppliers"][0]["reason"])
        self.assertFalse(result["needed_fact_work"][0]["blocking"])

    def test_known_inr_range_keeps_bounds_and_does_not_pick_the_cheapest_endpoint(self):
        memory = fixture()
        supplier(memory)["result"]["prices"] = [supplier_price(amount=None, quoted_amount="100-200", amount_range={"minimum": "100", "maximum": "200"})]
        row = next(row for row in build_report_pool(memory, as_of=NOW)["product_facts"][0]["prices"] if row["kind"] == "supplier")
        self.assertIsNone(row["amount"])
        self.assertEqual(row["amount_range"], {"minimum": "100", "maximum": "200"})
        self.assertIn("not a single sourcing cost", row["conditions"])

    def test_membership_fee_is_not_product_cost_and_pack_moq_is_not_item_count(self):
        memory = fixture(); stated(memory, "purchase_moq", "25 sets")
        supplier(memory)["result"]["prices"] = [supplier_price(amount="1499", unit="year", price_kind="mandatory_recurring")]
        fact = build_report_pool(memory, as_of=NOW)["product_facts"][0]
        self.assertIsNone(fact["suppliers"][0]["min_order"])
        self.assertIn("25 sets", fact["suppliers"][0]["reason"])
        self.assertFalse(any(row["kind"] == "supplier" for row in fact["prices"]))
        self.assertEqual(fact["supplier_price_observations"][0]["amount"], "1499")

    def test_family_price_keeps_its_actual_link_date_and_unverified_pack(self):
        memory = fixture(); memory.state["market_reviews"]["review0"]["relevance"][0]["relationship"] = "relevant_family"
        fact = build_report_pool(memory, as_of=NOW)["product_facts"][0]
        price = next(row for row in fact["prices"] if row["kind"] == "retail")
        self.assertEqual(price["amount"], "179")
        self.assertEqual(price["unit"], "not_stated")
        self.assertIsNone(price["quantity"])
        self.assertEqual(price["observed_at"], OBSERVED)
        self.assertEqual(price["source_url"], "https://example.org/fixture-listing/0")
        self.assertIn("not an exact product", price["conditions"])

    def test_missing_or_ambiguous_price_stays_unknown_not_zero(self):
        for displayed in (None, "100-200", "₹149 (sale)", True):
            memory = fixture(); memory.state["observations"]["amazon000"]["facts"]["price"]["displayed"] = displayed
            fact = build_report_pool(memory, as_of=NOW)["product_facts"][0]
            self.assertIsNone(fact["prices"][0]["amount"])
            self.assertEqual(fact["retail_price_observations"][0]["displayed"], displayed)

    def test_supplier_review_time_is_not_claimed_as_original_capture_time(self):
        fact = build_report_pool(fixture(), as_of=NOW)["product_facts"][0]
        self.assertIsNone(fact["suppliers"][0]["source_capture_at"])
        source = next(row for row in fact["source_refs"] if row["source"].startswith("Supplier"))
        self.assertEqual(source["date_basis"], "supplier_interpretation_review")
        self.assertEqual(source["observed_at"], NOW)

    def test_supplier_capture_dates_reach_leads_citations_and_prices(self):
        memory = fixture()
        supplier(memory)["source_dates"] = {"page0": OBSERVED}
        supplier(memory)["result"]["prices"] = [supplier_price()]
        fact = build_report_pool(memory, as_of=NOW)["product_facts"][0]
        self.assertEqual(fact["suppliers"][0]["source_capture_at"], OBSERVED)
        self.assertEqual(fact["suppliers"][0]["reviewed_at"], NOW)
        source = next(row for row in fact["source_refs"] if row["source"].startswith("Supplier"))
        self.assertEqual(source["observed_at"], OBSERVED)
        self.assertEqual(source["date_basis"], "supplier_page_capture")
        price = next(row for row in fact["prices"] if row["kind"] == "supplier")
        self.assertEqual(price["observed_at"], OBSERVED)
        self.assertEqual(price["date_basis"], "supplier_page_capture")

    def test_future_supplier_capture_does_not_enter_an_earlier_report(self):
        memory = fixture()
        supplier(memory)["source_dates"] = {"page0": "2026-10-07T10:00:00Z"}
        pool = build_report_pool(memory, as_of=NOW)
        self.assertEqual(pool["eligible"], [])
        self.assertIn("supplier_search_required", pool["decisions"][0]["codes"])

    def test_supplier_source_date_only_change_invalidates_draft_scope(self):
        from product_radar.report_selection import selection_scope
        memory = fixture()
        supplier(memory)["source_dates"] = {"page0": OBSERVED}
        before = digest(selection_scope(memory.state, "c000"))
        supplier(memory)["source_dates"]["page0"] = "2026-10-05T10:00:00Z"
        self.assertNotEqual(digest(selection_scope(memory.state, "c000")), before)

    def test_published_product_needs_actual_useful_update_not_timestamp_or_boolean(self):
        memory = fixture()
        memory.state["published_history"]["published0"] = {"status": "published", "client_id": "default", "concept_id": "c000"}
        memory.state["concepts"]["c000"].update(material_update_reviewed=True, update_reason="A new report date")
        result = build_report_pool(memory, as_of=NOW)
        self.assertEqual(result["eligible"], [])
        self.assertIn("useful_update_review_required", result["decisions"][0]["codes"])
        self.assertEqual(len(build_report_pool(memory, as_of=NOW, client_id="other-client")["eligible"]), 1)

    def test_held_products_remain_pool_rows_for_allocator_to_handle(self):
        memory = fixture(); memory.state["issue_reservations"]["held"] = {"state": "reserved", "concept_ids": ["c000"]}
        self.assertEqual(len(build_report_pool(memory, as_of=NOW)["eligible"]), 1)

    def test_missing_or_stale_checklist_returns_exact_fact_work(self):
        for change in (lambda state: state["assessments"].clear(),
                       lambda state: state["concepts"]["c000"]["description"].update(features=["Changed feature"]),
                       lambda state: state["assessments"]["c000"]["result"]["criteria"][2].update(state="Meets")):
            memory = fixture(); change(memory.state)
            result = build_report_pool(memory, as_of=NOW)
            self.assertEqual(result["eligible"], [])
            self.assertEqual(result["needed_fact_work"][0]["code"], "checklist_review_required")

    def test_description_replacement_requires_identity_rebinding(self):
        memory = fixture(); memory.state["descriptions"]["semantic000"]["result"]["reason"] = "Reinterpreted purchase unit"
        result = build_report_pool(memory, as_of=NOW)
        self.assertEqual(result["eligible"], [])
        self.assertEqual(result["decisions"][0]["codes"], ["identity_pending"])

    def test_modest_brand_gate_only_excludes_accepted_established_brand(self):
        for brand, expected in [("unclear", True), ("generic_or_small", True), ("established", False)]:
            memory = fixture(); memory.state["concepts"]["c000"]["description"]["brand"] = brand
            # Changing canonical description also requires the accepted E input hash.
            obs = memory.state["observations"]["meta000"]
            item = {"id": "c000", "description": memory.state["concepts"]["c000"]["description"], "observations": [{"id": "meta000", "semantic_id": "semantic000", "source_text": obs["semantic"]}]}
            memory.state["assessments"]["c000"]["input_hash"] = digest(item)
            memory.state["market_reviews"]["review0"]["description_hash"] = digest(item["description"])
            result = build_report_pool(memory, as_of=NOW)
            self.assertEqual(bool(result["eligible"]), expected)

    def test_low_checklist_does_not_queue_paid_market_or_supplier_work(self):
        memory = fixture(); memory.state["assessments"]["c000"]["result"]["criteria"][0]["state"] = "Unclear"
        memory.state["assessments"]["c000"]["result"]["criteria"][1]["state"] = "Unclear"
        memory.state["market_reviews"].clear(); memory.state["supplier_assessments"].clear()
        result = build_report_pool(memory, as_of=NOW)
        self.assertEqual(result["decisions"][0]["decision"], "excluded")
        self.assertEqual(result["needed_market_work"], [])
        self.assertEqual(result["needed_supplier_work"], [])

    def test_missing_market_and_expired_captures_are_not_negative_demand(self):
        memory = fixture(); memory.state["market_reviews"].clear()
        result = build_report_pool(memory, as_of=NOW)
        self.assertEqual(result["eligible"], [])
        self.assertEqual(result["needed_market_work"][0]["available_families"], 1)
        old = build_report_pool(fixture(), as_of="2026-10-20T10:00:00Z")
        self.assertEqual(old["eligible"], [])
        self.assertEqual(old["facts_by_concept"]["c000"]["market_label"], "Insufficient evidence")

    def test_market_queue_explains_signal_gap_and_prioritises_existing_positive_stably(self):
        memory = fixture(3)
        memory.state["market_reviews"].clear()
        # Identical product semantics can occur in different advertisers' ads;
        # two more independent advertisers make c002's retained Meta positive.
        for n in (1, 2):
            row = deepcopy(memory.state["observations"]["meta002"])
            row["source_item"] = f"extra-ad-{n}"
            row["facts"].update(ad_id=f"extra-ad-{n}", advertiser_id=f"extra-advertiser-{n}")
            memory.state["observations"][f"extra-meta-{n}"] = row
        before = deepcopy(memory.state)
        result = build_report_pool(memory, as_of=NOW)
        work = result["needed_market_work"]
        self.assertEqual([row["concept_id"] for row in work], ["c002", "c000", "c001"])
        self.assertEqual([row["positive_families"] for row in work], [1, 0, 0])
        self.assertEqual([row["positive_signal_needed"] for row in work], [False, True, True])
        self.assertIn("does not have to be positive", work[0]["reason"])
        self.assertIn("Another usable check alone does not qualify", work[1]["reason"])
        self.assertEqual(result["eligible"], [])
        self.assertEqual(memory.state, before)
        self.assertEqual(build_report_pool(memory, as_of=NOW)["needed_market_work"], work)

    def test_ten_joined_rows_allocate_and_build_one_matching_dataset(self):
        memory = fixture(10)
        pool = build_report_pool(memory, as_of=NOW, client_id="fixture-client")
        issue = allocate_issues(pool["eligible"], [{"date": "2026-10-07", "issue_id": "fixture-issue", "revision": 1, "client_id": "fixture-client"}], [], [])["issues"][0]
        selected = [pool["facts_by_concept"][row["concept_id"]] for row in issue["products"]]
        dataset = build_issue_dataset(issue, selected, evidence_type="synthetic")
        self.assertEqual(len(dataset["products"]), 10)
        self.assertTrue(all(row["status"] == "New" and row["kpi_states"][2] == "Not assessed" for row in dataset["products"]))
        self.assertEqual(len({row["concept_id"] for row in dataset["products"]}), 10)

    def test_one_invalid_supplier_record_does_not_stop_other_products(self):
        memory = fixture(2)
        supplier(memory)["input_hash"] = "outdated-input"
        result = build_report_pool(memory, NOW)
        self.assertEqual([row["concept_id"] for row in result["eligible"]], ["c001"])
        problem = next(row for row in result["decisions"] if row["concept_id"] == "c000")
        self.assertEqual(problem["codes"], ["saved_supplier_or_source_facts_invalid"])
        self.assertTrue(result["needed_fact_work"][0]["blocking"])

    def test_original_source_filter_decisions_are_retained_without_new_judgments(self):
        memory = fixture()
        memory.state["associations"]["excluded-fixture"] = {"semantic_id": "other-fixture", "concept_id": None,
            "status": "excluded", "reason": "Accepted established brand", "method": "brand_filter", "job_id": "fixture-a"}
        result = build_report_pool(memory, NOW)
        self.assertEqual(len(result["source_decisions"]), 2)
        excluded = next(row for row in result["source_decisions"] if row["child_id"] == "excluded-fixture")
        self.assertEqual(excluded["reason"], "Accepted established brand")
        self.assertEqual(excluded["decision"], "excluded")


if __name__ == "__main__":
    unittest.main()
