"""Actual saved-only functional producers through recommendation/witness joins.

The helper is reusable by packet tests. All pages, receipts and AI replies are
explicitly synthetic; no validator, provider or publication path is mocked.
"""

from copy import deepcopy
from pathlib import Path
import unittest
from unittest.mock import patch

from product_radar.crowding import refresh_crowding
from product_radar.fit_description import FUNCTIONAL_CONTRACT
from product_radar.fit_policy import FIT_POLICY_ID
from product_radar.fit_review import prepare_practical_fit_reviews, run_fit_reviews
from product_radar.frozen_records import freeze_original_json, read_original_json
from product_radar.functional_types import (accept_functional_definition, effective_functional_definition,
    prepare_example_memberships, example_membership_preparation)
from product_radar.market_validation import (functional_market_context, functional_priority_support,
    validate_frozen_functional_market, validate_priority_support)
from product_radar.models import SafetyError, digest
from product_radar.product_queries import ensure_query_profiles
from product_radar.report_examples import RECOMMENDATION_CONTRACT
from product_radar.report_selection import build_report_pool, selection_scope, validate_frozen_functional_fit
from product_radar.research_flow import candidate_summary, run_flow
from product_radar.research_memory import ResearchMemory, import_capture
from product_radar.research_scope import accept_scoped_evidence
from product_radar.research_sources import map_research_rows
from product_radar.storage import Journal
from product_radar.supplier_effort import record_supplier_effort
from test_functional_fit import FunctionalFitRunner
import test_functional_fit as fit_fixtures
from test_functional_queries import DEFINITION, Runner as QueryRunner
from test_research_flow import FakeRunner, NOW, capture
from test_research_sources import amazon_row, request
import test_functional_source_interpretation as source_fixtures
from test_functional_source_interpretation import Runner as SourceRunner


class DiscoveryRunner(FakeRunner):
    """Keep explicitly different fixture functions apart in the real A/B flow."""
    def __call__(self, role, payload, folder):
        response, usage = super().__call__(role, payload, folder)
        if role == "A":
            for item, answer in zip(payload["items"], response["results"]):
                suffix = item["source_text"]["title"].rsplit(" ", 1)[-1]
                for product in answer["products"]:
                    product["label"] = "Synthetic draining caddy " + suffix
                    product["function"] = "Synthetic fixture function " + suffix
        return response, usage


def functional_pool_fixture(root, *, count=1, at=NOW, with_market=True, with_images=False):
    """Build real records for count synthetic types; return memory and IDs.

    Accepted A/B, explicit type/example judgments, C profiles, v4 fit, scoped
    original-page matches and verified finite supplier effort all use actual
    application writers. This does not construct or mutate allocator rows.
    """
    root = Path(root)
    memory = ResearchMemory(root / "research", Journal(root / "finance"), evidence_type="synthetic")
    values = [{"ad_archive_id": str(100 + n), "page_id": str(200 + n), "is_active": True,
               "start_date": 1788220800, "snapshot": {"title": f"Blue sink caddy {n:03d}",
                   **({"original_image_url": f"https://images.example.org/caddy-{n}.png"} if with_images else {})}}
              for n in range(count)]
    capture(memory, values=values, observed=at)
    run_flow(memory, DiscoveryRunner(), as_of=at)
    ids = sorted(memory.snapshot()["concepts"])
    for n, cid in enumerate(ids):
        state = memory.snapshot()
        children = {child: row for child, row in state["associations"].items() if row["concept_id"] == cid}
        semantics = {row["semantic_id"] for row in children.values()}
        refs = sorted(ref for ref, row in state["observations"].items() if row["semantic_id"] in semantics)
        meaning = accept_functional_definition(memory, cid,
            dict(DEFINITION, primary_use=f"Synthetic fixture function {n:03d}"), basis_refs=refs,
            reviewer="synthetic", reviewed_at=at,
            expected_meaning_hash=effective_functional_definition(state, cid)["meaning_hash"],
            example_decisions={child: {"decision": "same", "reason": "Explicit synthetic example."} for child in children})
        prepare_example_memberships(memory, cid, as_of=at)
        if with_market:
            cards = [dict(amazon_row(), title=f"Synthetic draining caddy {n:03d}", asin=f"B{n:09d}",
                          link=f"https://www.amazon.in/dp/B{n:09d}")]
            imported = import_capture(memory, source="amazon", acquisition_id=f"fixture-amazon-{n}", observed_at=at,
                mapped=map_research_rows(cards, request()), capture_hash=digest(cards), reference={"fixture": "synthetic"})
            state = memory.snapshot()
            refs += [ref for ref, row in state["observations"].items() if row["capture_id"] == imported["capture_id"]]
            for ref in refs:
                row = memory.snapshot()["observations"][ref]
                accept_scoped_evidence(memory, cid, route="meta-ads" if row["source"] == "meta" else "amazon-bestsellers",
                    scope="narrow_type", relationship="relevant_family", support="insufficient",
                    references=[{"table": "observations", "id": ref, "hash": digest(row)}],
                    usable_fields=[{"reference": {"table": "observations", "id": ref}, "path": ["semantic"]}],
                    limitations=["Synthetic public listing, not sales proof."], reason="Explicit fixture type comparison.",
                    reviewer="synthetic", as_of=at,
                    expected_meaning_hash=effective_functional_definition(memory.snapshot(), cid)["meaning_hash"])
        state = memory.snapshot()
        url = f"https://supplier.example.org/fixture/{n}"
        record_supplier_effort(memory, concept_id=cid, description=state["concepts"][cid]["description"],
            pass_id=f"fixture-suppliers-{n}", receipts=[{"schema_version": 2, "id": f"supplier-{n}",
                "requested_url": url, "final_url": url, "checked_at": at, "http_status": 200,
                "execution_stage": "response_received", "attempt_started_at": at,
                "execution_ref": "public-fetch-" + f"{n:032x}", "body_complete": True,
                "state": "no_results", "reason": "Synthetic readable empty supplier result."}],
            receipt_reference=f"synthetic/fixture-{n}/receipt.json", outcome="completed_empty")
    ensure_query_profiles(memory, QueryRunner(), concept_ids=ids, as_of=at)
    reviews = prepare_practical_fit_reviews(memory, concept_ids=ids, as_of=at, contract_version=FUNCTIONAL_CONTRACT)
    results = run_fit_reviews(memory, FunctionalFitRunner(), [row["review_id"] for row in reviews])
    if len(ids) != count or any(row["status"] != "accepted" for row in results):
        raise AssertionError((ids, results))
    # The free crowding check (W6) through its real writer: the fixture's
    # relevant bestseller listing makes each market type High, never assumed.
    refresh_crowding(memory, ids, as_of=at, limit=max(1, min(500, len(ids))))
    return memory, ids


class FunctionalPoolTests(unittest.TestCase):
    def setUp(self):
        import tempfile
        temp = tempfile.TemporaryDirectory(); self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.memory, (self.cid,) = functional_pool_fixture(self.root)

    def pool(self, **kwargs):
        return build_report_pool(self.memory, as_of=NOW, recommendation_contract=RECOMMENDATION_CONTRACT, **kwargs)

    def test_full_real_producers_join_new_opt_in_pool_without_writes(self):
        before = self.memory.snapshot()
        pool = self.pool()
        self.assertEqual(len(pool["eligible"]), 1, pool["decisions"])
        row, fact = pool["eligible"][0], pool["product_facts"][0]
        self.assertEqual(row["opportunity_priority"], 1)
        self.assertEqual(row["recommendation_contract"], RECOMMENDATION_CONTRACT)
        self.assertEqual(row["opportunity_basis"]["source_ids"], ["amazon", "meta"])
        self.assertEqual(len(row["priority_support"]["sources"]), 2)
        self.assertEqual(row["source_route_contract"], "saved-source-routes-v2")
        self.assertTrue(row["example_products"])
        self.assertTrue(fact["supplier_effort_satisfied"])
        self.assertEqual(fact["supplier_limitation"], "Supplier search did not produce any usable results.")
        validate_frozen_functional_market(fact); validate_frozen_functional_fit(fact)
        self.assertEqual(self.memory.snapshot(), before)

    def test_default_guard_and_unknown_contract_do_not_retag_current_v4(self):
        old = build_report_pool(self.memory, as_of=NOW)
        self.assertFalse(old["eligible"])
        self.assertTrue(any(row["code"] == "functional_report_contract_required" for row in old["needed_fact_work"]), old)
        with self.assertRaises(SafetyError):
            build_report_pool(self.memory, as_of=NOW, recommendation_contract="unrecognised")

    def test_both_proof_readers_refuse_tampered_original_or_input(self):
        original = self.pool()["product_facts"][0]
        changed = deepcopy(original)
        changed["functional_fit_input"]["canonical_json"] += " "
        with self.assertRaises(SafetyError):
            validate_frozen_functional_fit(changed)
        changed = deepcopy(original)
        table = changed["functional_market_proof"]["records"]["observations"]
        oid = next(iter(table)); record = read_original_json(table[oid])
        record["facts"]["synthetic_tamper"] = True
        table[oid] = freeze_original_json(record)
        with self.assertRaisesRegex(SafetyError, "FUNCTIONAL_MARKET_ORIGINAL_CHANGED"):
            validate_frozen_functional_market(changed)

    def test_complete_witness_dates_and_count_cannot_be_relabelled(self):
        fact = self.pool()["product_facts"][0]
        wrong = deepcopy(fact["priority_support"])
        wrong["sources"].pop()
        with self.assertRaises(SafetyError):
            validate_priority_support(wrong, opportunity=fact,
                meaning_hash=fact["functional_definition"]["meaning_hash"], as_of=NOW)
        with self.assertRaisesRegex(SafetyError, "FUNCTIONAL_MARKET_WITNESS_STALE"):
            validate_priority_support(fact["priority_support"], opportunity=fact,
                meaning_hash=fact["functional_definition"]["meaning_hash"], as_of="2026-10-13T10:00:00+00:00")

    def test_meaning_change_drops_previous_fit_and_source_votes_not_unknown_equals_failure(self):
        state = self.memory.snapshot(); meaning = effective_functional_definition(state, self.cid)
        accept_functional_definition(self.memory, self.cid, dict(DEFINITION, mechanism="Different synthetic mechanism"),
            basis_refs=[next(iter(state["observations"]))], reviewer="synthetic", reviewed_at=NOW,
            expected_meaning_hash=meaning["meaning_hash"])
        summary = next(row for row in candidate_summary(self.memory, as_of=NOW)["products"] if row["id"] == self.cid)
        self.assertIsNone(summary["checklist_meets"])
        self.assertFalse(any(row["available"] for row in summary["market"]["sources"].values()))
        self.assertFalse(self.pool()["eligible"])

    def test_float_physical_originals_remain_exact_inside_integer_safe_packet_wrappers(self):
        fit_fixtures.FunctionalFitTests.physical_fact(self, field="weight", value=0.35)
        reviews = prepare_practical_fit_reviews(self.memory, concept_ids=[self.cid], as_of=NOW,
                                               contract_version=FUNCTIONAL_CONTRACT)
        run_fit_reviews(self.memory, FunctionalFitRunner(), [row["review_id"] for row in reviews])
        pool = self.pool(); self.assertEqual(len(pool["eligible"]), 1, pool["decisions"])
        fact = pool["product_facts"][0]
        self.assertEqual(read_original_json(fact["functional_fit_input"])["observations"][0]["source_text"]["value"], 0.35)
        validate_frozen_functional_fit(fact)
        outer = deepcopy(fact["functional_fit_reference_proofs"])
        self.assertIn("0.35", str(outer))

    def test_global_unreviewed_replacement_changes_selection_scope_and_cannot_keep_vote(self):
        before = selection_scope(self.memory.snapshot(), self.cid)
        values = [dict(amazon_row(), title="Different unrelated original", asin="B000000000",
                       link="https://www.amazon.in/dp/B000000000")]
        import_capture(self.memory, source="amazon", acquisition_id="newer-unrelated", observed_at="2026-10-06T10:00:00+00:00",
            mapped=map_research_rows(values, request()), capture_hash=digest(values), reference={"fixture": "synthetic"})
        state = self.memory.snapshot()
        self.assertNotEqual(selection_scope(state, self.cid), before)
        market = functional_market_context(state, self.cid, as_of="2026-10-06T10:00:00+00:00")
        self.assertFalse(market["market"]["sources"]["amazon"]["available"])

    def test_membership_readiness_is_exact_and_writer_clears_without_new_model(self):
        state = self.memory.snapshot()
        self.assertFalse(example_membership_preparation(state, self.cid)["missing_ids"])
        capture(self.memory, values=[{"ad_archive_id": "100", "page_id": "200", "is_active": True,
            "start_date": 1788220800, "snapshot": {"title": "Blue sink caddy 000"}}],
            acquisition="actual-later-fixture", observed="2026-10-06T10:00:00+00:00")
        later = self.memory.snapshot(); jobs = deepcopy(later["jobs"])
        missing = example_membership_preparation(later, self.cid)
        self.assertTrue(missing["missing_ids"])
        prepare_example_memberships(self.memory, self.cid, as_of="2026-10-06T10:00:00+00:00")
        final = self.memory.snapshot()
        self.assertFalse(example_membership_preparation(final, self.cid)["missing_ids"])
        self.assertEqual(final["jobs"], jobs)

    def test_newer_failed_chart_does_not_erase_usable_chart_but_new_usable_chart_replaces_it(self):
        from product_radar.research_operations import import_trends_evidence
        from test_trends_browser import metadata, weekly
        chart = import_trends_evidence(self.memory, concept_id=self.cid, acquisition_id="actual-chart",
            csv_bytes=weekly(), metadata=metadata(collected_at=NOW))
        original = self.memory.snapshot()["trends_evidence"][chart["id"]]
        accept_scoped_evidence(self.memory, self.cid, route="google-trends", scope="narrow_type",
            relationship="relevant_family", support="insufficient",
            references=[{"table": "trends_evidence", "id": chart["id"], "hash": digest(original)}],
            usable_fields=[{"reference": {"table": "trends_evidence", "id": chart["id"]}, "path": ["parsed", "series"]}],
            limitations=["Relative search interest, not product sales."], reason="Explicit fixture narrow chart match.",
            reviewer="synthetic", as_of=NOW,
            expected_meaning_hash=effective_functional_definition(self.memory.snapshot(), self.cid)["meaning_hash"])
        before = selection_scope(self.memory.snapshot(), self.cid)
        later = "2026-10-06T10:00:00+00:00"
        failed = import_trends_evidence(self.memory, concept_id=self.cid, acquisition_id="actual-failed-chart",
            csv_bytes=b"No usable exported series", metadata=metadata(collected_at=later))
        state = self.memory.snapshot()
        market = functional_market_context(state, self.cid, as_of=later)
        self.assertIn(chart["id"], market["market"]["sources"]["google-trends"]["used_ids"])
        self.assertNotIn(failed["id"], market["matches"])
        import_trends_evidence(self.memory, concept_id=self.cid, acquisition_id="actual-new-zero-chart",
            csv_bytes=weekly([0] * 54), metadata=metadata(collected_at=later))
        state = self.memory.snapshot()
        self.assertNotEqual(selection_scope(state, self.cid), before)
        market = functional_market_context(state, self.cid, as_of=later)
        self.assertNotIn(chart["id"], market["matches"])

    def test_display_facts_cannot_change_without_their_actual_accepted_inputs(self):
        fact = self.pool()["product_facts"][0]
        for field, mutate, validator in (
                ("criteria", lambda row: row["criteria"][0].update(reason="Invented accepted reason."), validate_frozen_functional_fit),
                ("writing_facts", lambda row: row["writing_facts"][0].update(text="Invented benefit."), validate_frozen_functional_fit),
                ("prices", lambda row: row["prices"].append({"amount": "1", "kind": "retail"}), validate_frozen_functional_fit),
                ("market_checks", lambda row: row["market_checks"][0].update(detail="Guaranteed sales."), validate_frozen_functional_market),
                ("source_refs", lambda row: row["source_refs"][0].update(url="https://invented.example.org/"), validate_frozen_functional_fit),
                ("suppliers", lambda row: row["suppliers"].append({"name": "Invented company"}), validate_frozen_functional_fit)):
            with self.subTest(field=field):
                changed = deepcopy(fact); mutate(changed)
                with self.assertRaises(SafetyError):
                    validator(changed)

    def test_legacy_supplier_terms_remain_dated_company_leads_not_new_type_promises(self):
        from product_radar.suppliers import extract_supplier_terms, SUPPLIER_FIELDS
        from test_suppliers import item, term
        supplied = item()
        supplied["product"] = dict(self.memory.snapshot()["concepts"][self.cid]["description"], concept_id=self.cid)
        class SupplierRunner:
            identity = "synthetic-observed-legacy-supplier"
            def __call__(self, role, payload, folder):
                results = []
                for row in payload["items"]:
                    page = row["pages"][0]["id"]
                    terms = [term(field, "Yes", "We dispatch directly to your customer.", reference=page)
                             if field in ("single_piece_purchase", "direct_customer_dispatch") else
                             {"field": field, "value": "Not stated", "basis": "not_stated", "references": [],
                              "quotes": [], "reason": "Synthetic unknown term."} for field in SUPPLIER_FIELDS]
                    results.append({"id": row["id"], "relevance": {"relationship": "exact_product", "references": [page],
                        "reason": "Synthetic legacy product-page match."}, "terms": terms, "prices": []})
                return {"results": results}, {"input_tokens": 1, "output_tokens": 1, "cached_input_tokens": 0}
        with patch("product_radar.suppliers.utc_now", return_value=NOW):
            result = extract_supplier_terms(self.memory, [supplied], SupplierRunner())
        self.assertTrue(result["accepted"], result)
        pool = self.pool(); self.assertEqual(len(pool["eligible"]), 1, pool["decisions"])
        fact = pool["product_facts"][0]; self.assertEqual(len(fact["suppliers"]), 1)
        lead = fact["suppliers"][0]
        self.assertEqual(lead["relationship"], "unclear")
        self.assertEqual(lead["single_piece_dispatch"], "not_stated")
        self.assertTrue(all(term["value"] == "Not stated" and term["quotes"] == [] for term in lead["terms"]))
        validate_frozen_functional_fit(fact)
        changed = deepcopy(fact); changed["suppliers"][0]["terms"][0].update(value="Yes", basis="observed")
        with self.assertRaisesRegex(SafetyError, "REPORT_FUNCTIONAL_SUPPLIER_DISPLAY_CHANGED"):
            validate_frozen_functional_fit(changed)


class FunctionalAggregateTests(unittest.TestCase):
    setUp = source_fixtures.FunctionalSourceInterpretationTests.setUp
    accept = source_fixtures.FunctionalSourceInterpretationTests.accept
    review = source_fixtures.FunctionalSourceInterpretationTests.review

    def test_one_positive_source_uses_all_indivisible_judgment_refs(self):
        self.review(SourceRunner(judgment="reasonably_positive"))
        state = self.memory.snapshot(); context = functional_market_context(state, "first", as_of="2026-10-08T10:00:00+00:00")
        # Exercise the aggregate-opinion witness branch separately from this
        # fixture's independent observed bestseller-rank branch. No original
        # or accepted opinion is changed; packet validators recompute both.
        context["market"]["sources"]["amazon"]["positive"] = False
        support = functional_priority_support(state, context, {"fit_policy_id": FIT_POLICY_ID, "kpi_total": 10, "kpi_matches": 6}, as_of="2026-10-08T10:00:00+00:00")
        self.assertEqual(support["opportunity"]["opportunity_priority"], 3)
        self.assertEqual(len(support["priority_support"]["sources"]), 1)
        self.assertEqual(set(support["priority_support_refs"]), set(self.refs))

    def test_neutral_opinion_does_not_erase_actual_verified_bestseller_context(self):
        self.review(SourceRunner())
        state = self.memory.snapshot(); context = functional_market_context(state, "first", as_of="2026-10-08T10:00:00+00:00")
        support = functional_priority_support(state, context, {"fit_policy_id": FIT_POLICY_ID, "kpi_total": 10, "kpi_matches": 6}, as_of="2026-10-08T10:00:00+00:00")
        self.assertTrue(context["market"]["sources"]["amazon"]["available"])
        self.assertEqual(context["market"]["sources"]["amazon"]["support_judgment"], "neutral")
        self.assertEqual(support["opportunity"]["opportunity_priority"], 3)
        self.assertTrue(support["priority_support"])

