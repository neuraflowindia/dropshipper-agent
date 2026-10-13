"""Saved synthetic supplier pages through actual Fv4 and report projection."""

from copy import deepcopy
from pathlib import Path
from types import SimpleNamespace
import json
import tempfile
import unittest
from unittest.mock import patch

from product_radar.models import SafetyError, canonical
from product_radar.report_examples import RECOMMENDATION_CONTRACT
from product_radar.report_selection import build_report_pool, _validate_frozen_functional_suppliers
from product_radar.report_supplier_facts import CONTRACT, validate_frozen_functional_supplier_terms
from product_radar.supplier_capture_store import capture_saved_supplier_bundle
from product_radar.supplier_checks import prepare_supplier_terms, extract_scoped_supplier_terms
from product_radar.supplier_effort import frozen_supplier_effort_valid
from test_functional_source_market import functional_pool_fixture, NOW
from test_scoped_supplier_terms import Reader, ScopedRunner, BASE, CandidateRunner
import test_scoped_supplier_terms as scoped_fixtures


def scoped_supplier_fixture(memory, cid, *, at=NOW, checked_at=None, candidate=False, reader=None, runner=None):
    saved = capture_saved_supplier_bundle(memory, {"concept_id": cid, "lead": {
        "id": "scoped-fictional-lead", "urls": [BASE + "/about", BASE + "/offers/a", BASE + "/offers/b"]}},
        fetcher=reader or Reader(at=at))
    if candidate:
        from product_radar.supplier_checks import prepare_supplier_candidates, extract_supplier_candidates
        prepared = prepare_supplier_candidates(memory, saved["path"])
        with patch("product_radar.supplier_checks.utc_now", return_value=checked_at or at):
            result = extract_supplier_candidates(memory, prepared, runner or CandidateRunner())
    else:
        choices = scoped_fixtures.ScopedSupplierTests.choices_for(SimpleNamespace(memory=memory), saved)
        prepared = prepare_supplier_terms(memory, saved["path"], choices)
        with patch("product_radar.supplier_checks.utc_now", return_value=checked_at or at):
            result = extract_scoped_supplier_terms(memory, prepared, runner or ScopedRunner())
    if result["failures"]:
        raise AssertionError(result)
    return saved, result


def range_supplier_fixture(memory, cid, *, at=NOW):
    """Actual literal range and required fee that conflicts with free membership."""
    from test_scoped_supplier_terms import COMPANY
    text = "Sink caddy, 1 piece. Minimum 10 pieces. INR 100–250 per piece. This offer cannot be dispatched directly."
    company = COMPANY + " Membership costs INR 50 per month."
    def change(item, selection):
        page = next(p for p in item["pages"] if p["id"] == item["primary_page_id"])
        if selection["scope_kind"] == "company_workflow":
            selection["prices"].append({"amount": "50", "currency": "INR", "unit": "month",
                "quantity_condition": "membership", "price_kind": "mandatory_recurring",
                "qualifier": "Required membership fee", "references": [page["id"]],
                "quotes": [{"page_id": page["id"], "excerpt": "Membership costs INR 50 per month."}],
                "reason": "Actual saved fictional fee."})
        elif page["url"].endswith("/a"):
            selection["prices"][0]["amount"] = "100–250"
    return scoped_supplier_fixture(memory, cid, at=at, candidate=True,
        reader=Reader(at=at, company=company, a=text), runner=CandidateRunner(change))


class ReportScopedSuppliers(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory(); self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.memory, (self.cid,) = functional_pool_fixture(self.root)

    def fact(self):
        pool = build_report_pool(self.memory, as_of=NOW, recommendation_contract=RECOMMENDATION_CONTRACT)
        self.assertEqual(len(pool["eligible"]), 1, pool["decisions"])
        return dict(pool["product_facts"][0], source_revisions=pool["eligible"][0]["source_revisions"])

    def test_actual_fv4_company_and_two_offer_terms_remain_local(self):
        scoped_supplier_fixture(self.memory, self.cid)
        before = self.memory.snapshot()
        fact = self.fact()
        self.assertEqual(fact["functional_supplier_proof"]["contract"], CONTRACT)
        self.assertEqual(fact["supplier_leads_found"], 1)
        group, = fact["suppliers"]
        self.assertEqual(len(group["company_workflows"]), 1)
        offers = {row["purchase_unit"]: row for row in group["product_offers"]}
        self.assertEqual(offers["1 piece"]["min_order"], 10)
        self.assertEqual(offers["pack of 3"]["min_order"], 100)
        self.assertEqual(offers["1 piece"]["prices"][0]["amount"], "100")
        self.assertEqual(offers["pack of 3"]["prices"][0]["amount"], "250")
        self.assertEqual({t["field"]: t["value"] for t in offers["1 piece"]["effective_terms"]}["direct_customer_dispatch"], "No")
        self.assertEqual(group["single_piece_dispatch"], "not_stated")
        self.assertIsNone(group["min_order"])
        self.assertEqual(fact["supplier_price_observations"], [])
        self.assertEqual(fact["supplier_scope_coverage"]["selected_groups"], 1)
        self.assertTrue(frozen_supplier_effort_valid(fact))
        _validate_frozen_functional_suppliers(fact)
        self.assertEqual(self.memory.snapshot(), before)

    def test_canonical_roundtrip_reconstructs_same_scoped_display(self):
        scoped_supplier_fixture(self.memory, self.cid)
        fact = json.loads(canonical(self.fact()))
        validate_frozen_functional_supplier_terms(fact)
        _validate_frozen_functional_suppliers(fact)
        self.assertTrue(frozen_supplier_effort_valid(fact))

    def test_display_terms_prices_dates_and_origin_cannot_drift(self):
        scoped_supplier_fixture(self.memory, self.cid)
        fact = self.fact()
        for change in (
            lambda f: f["suppliers"][0]["product_offers"][0].update(min_order=1),
            lambda f: f["suppliers"][0]["product_offers"][0]["prices"][0].update(amount="1"),
            lambda f: f["suppliers"][0]["product_offers"][0].update(current=False),
            lambda f: f["supplier_scoped_terms"]["all"][0].update(source_capture_at="2026-10-05T00:00:00+00:00"),
            lambda f: f["supplier_scoped_terms"]["all"][0]["terms"][0].update(support_application_ids=[]),
            lambda f: f.update(supplier_leads_found=3),
        ):
            changed = deepcopy(fact); change(changed)
            with self.assertRaises(SafetyError):
                _validate_frozen_functional_suppliers(changed)
            self.assertFalse(frozen_supplier_effort_valid(changed))

    def test_hybrid_candidate_original_owner_is_frozen_and_validated(self):
        scoped_supplier_fixture(self.memory, self.cid, candidate=True)
        fact = self.fact()
        records = fact["functional_supplier_proof"]["records"]
        from product_radar.frozen_records import read_original_json
        candidates = [read_original_json(r) for r in records["supplier_assessments"].values()
                      if read_original_json(r).get("kind") == "supplier-candidate-review-v1"]
        self.assertEqual(len(candidates), 3)
        self.assertTrue(all(r["job_id"] in records["jobs"] for r in candidates))
        _validate_frozen_functional_suppliers(fact)

    def test_legacy_functional_rows_keep_original_contract(self):
        fact = self.fact()
        self.assertEqual(fact["functional_supplier_proof"]["contract"], "functional-supplier-context-v1")
        self.assertIsNone(fact["supplier_scoped_terms"])
        self.assertIsNone(fact["supplier_scope_coverage"])
        self.assertTrue(frozen_supplier_effort_valid(fact))

    def test_actual_quoted_range_and_conflicting_membership_stay_scoped(self):
        range_supplier_fixture(self.memory, self.cid)
        fact = self.fact(); _validate_frozen_functional_suppliers(fact)
        group, = fact["suppliers"]
        company, = group["company_workflows"]
        self.assertEqual(company["fees"][0]["amount"], "50")
        subscription = next(t for t in company["terms"] if t["field"] == "subscription_requirement")
        self.assertEqual(subscription["value"], "Conflicting")
        offer = next(o for o in group["product_offers"] if o["purchase_unit"] == "1 piece")
        self.assertIsNone(offer["prices"][0]["amount"])
        self.assertEqual(offer["prices"][0]["quoted_amount"], "100–250")
        self.assertEqual(offer["prices"][0]["amount_range"], {"minimum": "100", "maximum": "250"})

    def test_new_contract_cannot_enter_legacy_effort_validator(self):
        scoped_supplier_fixture(self.memory, self.cid)
        fact = self.fact()
        for value in (None, "fit-13-v1"):
            changed = dict(fact, fit_policy_id=value)
            self.assertFalse(frozen_supplier_effort_valid(changed))
        self.assertFalse(frozen_supplier_effort_valid({"fit_policy_id": "fit-10-v1", "functional_supplier_proof": None}))

    def test_older_usable_supplier_context_is_labelled_not_a_product_gate(self):
        scoped_supplier_fixture(self.memory, self.cid, at="2026-09-01T10:00:00+00:00", checked_at=NOW)
        fact = self.fact(); _validate_frozen_functional_suppliers(fact)
        self.assertEqual(fact["supplier_leads_found"], 1)
        group, = fact["suppliers"]
        self.assertFalse(group["current"])
        self.assertFalse(group["scoped_usable"])
        self.assertTrue(all(not row["current"] for row in fact["supplier_scoped_terms"]["all"]))
        self.assertTrue(frozen_supplier_effort_valid(fact))


if __name__ == "__main__":
    unittest.main()
