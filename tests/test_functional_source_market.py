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
from product_radar.market_validation import (functional_market_context, functional_score_support,
    validate_frozen_functional_market, validate_score_support)
from product_radar.opportunity_score import score_type
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


def functional_pool_fixture(root, *, count=1, at=NOW, with_market=True, with_images=False, advertisers=3):
    """Build real records for count synthetic types; return memory and IDs.

    Accepted A/B, explicit type/example judgments, C profiles, v4 fit, scoped
    original-page matches and verified finite supplier effort all use actual
    application writers. This does not construct or mutate allocator rows.
    With a market, each type has ``advertisers`` relevant Meta advertisers
    (three make Meta positive: 6 of the score's 20 want points) and one
    relevant Amazon bestseller listing (High crowding).
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
            if advertisers > 1:
                # More relevant advertisers' ads for the same type (one ad each).
                extra = [{"ad_archive_id": str(5000 + 10 * n + k), "page_id": str(7000 + 10 * n + k), "is_active": True,
                          "start_date": 1788220800, "snapshot": {"title": f"Draining sink caddy {n:03d} advert {k}"}}
                         for k in range(1, advertisers)]
                imported = capture(memory, values=extra, acquisition=f"fixture-meta-{n}", observed=at)
                refs += sorted(ref for ref, row in memory.snapshot()["observations"].items()
                               if row["capture_id"] == imported["capture_id"])
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


def save_presence(memory, concept_id, *, at=NOW):
    """A saved platform presence fact (a relevant listed item on one platform's
    bestseller list) for a type's current meaning: the shape the weekly writer
    saves, built directly so the frozen-fact checks can be exercised."""
    from product_radar.dropship_platforms import ROUTES
    from product_radar.platform_roles import PRESENCE_KIND, PRESENCE_VERSION, _change_hash, presence_record_id
    meaning = effective_functional_definition(memory.snapshot(), concept_id)["meaning_hash"]
    item = {"item_id": "deodap-item-1", "title": "Synthetic draining caddy", "url": "https://deodap.example.org/item-1",
            "listed": True, "observation_id": "deodap-observation-1", "relationship": "relevant_family",
            "review_id": "deodap-review-1", "roles": {"bestseller": {"first_seen_at": at, "last_seen_at": at}},
            "on_list": True, "new_arrival": False, "price": "149", "mrp": None, "price_basis": "dropship_price",
            "in_stock": True, "terms": "Not stated", "first_seen_at": at, "last_seen_at": at}
    platforms = {route: {"label": route, "state": "relevant" if route == "deodap" else "not_checked",
                         "items": [item] if route == "deodap" else []} for route in ROUTES}
    value = {"kind": PRESENCE_KIND, "version": PRESENCE_VERSION, "concept_id": concept_id, "meaning_hash": meaning,
             "as_of": at, "platform_count": 1, "platforms_carrying": ["deodap"], "on_list_platforms": ["deodap"],
             "new_arrival_platforms": [], "platforms": platforms}
    value["change_hash"] = _change_hash(value)
    record = dict(value, at=at)
    memory.put("source_checks", presence_record_id(record), record)
    return record


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
        # Three relevant advertisers make Meta positive (6 want points); the
        # relevant Amazon bestseller listing gives no want points, only High crowding.
        parts = row["opportunity_basis"]["parts"]
        self.assertEqual((row["opportunity_policy_id"], row["opportunity_label"]), ("opportunity-score-v3", "proven_but_crowded"))
        self.assertEqual({part: parts[part]["points"] for part in parts}, {"want": 6, "early": 0, "room": 0, "product": 25})
        self.assertEqual(row["opportunity_score"], 31)
        self.assertEqual(row["recommendation_contract"], RECOMMENDATION_CONTRACT)
        self.assertEqual([site["source"] for site in row["score_support"]["sources"]], ["meta"])
        self.assertEqual(len(row["score_support"]["sources"][0]["references"]), 3)
        self.assertIsNone(row["score_support"]["presence"])
        self.assertEqual(row["support_valid_until"], fact["usable_until"])
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
        meaning = fact["functional_definition"]["meaning_hash"]
        validate_score_support(fact["score_support"], opportunity=fact, meaning_hash=meaning, as_of=NOW)
        for change in (lambda w: w["sources"].pop(), lambda w: w["sources"][0]["references"].pop(),
                       lambda w: w.update(score=w["score"] + 1), lambda w: w.update(presence={"at": NOW, "valid_until": NOW}),
                       lambda w: w["sources"][0].update(valid_until=NOW), lambda w: w.update(valid_until=NOW),
                       lambda w: w.update(contract="functional-priority-support-v1")):
            wrong = deepcopy(fact["score_support"]); change(wrong)
            with self.subTest(wrong=wrong), self.assertRaises(SafetyError):
                validate_score_support(wrong, opportunity=fact, meaning_hash=meaning, as_of=NOW)
        with self.assertRaisesRegex(SafetyError, "FUNCTIONAL_MARKET_WITNESS_STALE"):
            validate_score_support(fact["score_support"], opportunity=fact, meaning_hash=meaning,
                                   as_of="2026-10-13T10:00:00+00:00")
        changed = deepcopy(fact); changed["opportunity_basis"]["eligible"] = False
        with self.assertRaisesRegex(SafetyError, "FUNCTIONAL_MARKET_WITNESS_INVALID"):
            validate_score_support(fact["score_support"], opportunity=changed, meaning_hash=meaning, as_of=NOW)
        with self.assertRaisesRegex(SafetyError, "FUNCTIONAL_MARKET_WITNESS_INVALID"):
            validate_score_support(None, opportunity=fact, meaning_hash=meaning, as_of=NOW)

    def test_frozen_score_cannot_drift_from_recomputed_market_or_recorded_facts(self):
        fact = self.pool()["product_facts"][0]
        validate_frozen_functional_market(fact)
        for mutate in (lambda row: row.update(opportunity_score=row["opportunity_score"] + 1),
                       lambda row: row["opportunity_basis"]["parts"]["want"]["items"]["facebook_ads"]["facts"].update(advertisers=9),
                       lambda row: row["opportunity_basis"]["parts"]["room"]["items"]["crowding"]["facts"].update(level="low"),
                       lambda row: row.update(opportunity_label="sweet_spot")):
            changed = deepcopy(fact); mutate(changed)
            with self.subTest(changed=changed["opportunity_score"]), self.assertRaises(SafetyError):
                validate_frozen_functional_market(changed)
        # A Low crowding level recorded consistently is accepted by the score
        # arithmetic but still must agree with the support it froze.
        changed = deepcopy(fact)
        changed["score_support"]["score"] += 1
        with self.assertRaisesRegex(SafetyError, "FUNCTIONAL_MARKET_WITNESS"):
            validate_frozen_functional_market(changed)

    def test_frozen_presence_and_crowding_facts_are_checked_not_trusted(self):
        from product_radar.opportunity_score import _assemble, _stored_inputs, load_settings, opportunity_fields
        save_presence(self.memory, self.cid)
        fact = self.pool()["product_facts"][0]
        facts = fact["functional_market_proof"]["score_facts"]
        self.assertEqual(read_original_json(facts["crowding"]["record"])["level"], "high")
        self.assertEqual(read_original_json(facts["presence"]["record"])["platforms_carrying"], ["deodap"])
        parts = fact["opportunity_basis"]["parts"]
        self.assertEqual((parts["want"]["items"]["platform_lists"]["points"], fact["opportunity_score"]), (4, 35))
        validate_frozen_functional_market(fact)

        def rescored(edit):
            """Change a recorded sub-point and re-add the score consistently."""
            def mutate(row):
                inputs = _stored_inputs(row["opportunity_basis"]); edit(inputs)
                row.update(opportunity_fields(_assemble(inputs, load_settings())))
            return mutate

        def refrozen(name, edit):
            """Change a frozen saved fact and re-hash its wrapper consistently."""
            def mutate(row):
                entry = row["functional_market_proof"]["score_facts"][name]
                record = read_original_json(entry["record"]); edit(record)
                entry["record"] = freeze_original_json(record)
            return mutate

        def missing_platform_row(row):
            """A carrying platform with no saved row, its change hash and id recomputed: only the item read fails."""
            from product_radar.platform_roles import _change_hash, presence_record_id
            entry = row["functional_market_proof"]["score_facts"]["presence"]
            record = read_original_json(entry["record"]); record["platforms_carrying"] = ["deodap", "missing-route"]
            record["change_hash"] = _change_hash({key: value for key, value in record.items() if key not in ("change_hash", "at")})
            entry.update(id=presence_record_id(record), record=freeze_original_json(record))

        def forged_low_fact_and_score(row):
            """The fact and the score edited together: Low with 12 listings and 5 bestseller hits."""
            entry = row["functional_market_proof"]["score_facts"]["crowding"]
            record = read_original_json(entry["record"])
            record.update(level="low", rule="few_listings", evidence_ids=[f"listing-{n:02d}" for n in range(12)],
                          counts=dict(record["counts"], relevant_listings={"amazon": 12, "flipkart": 0}, bestseller_hits=5))
            entry["record"] = freeze_original_json(record)
            inputs = _stored_inputs(row["opportunity_basis"])
            inputs["crowding"] = ({field: record[field] for field in ("level", "rule", "thresholds_version", "counts")},
                                  record["evidence_ids"])
            row.update(opportunity_fields(_assemble(inputs, load_settings())))

        def low(inputs):
            inputs["crowding"] = (dict(inputs["crowding"][0], level="low", rule="few_listings"), inputs["crowding"][1])

        def invented_listing(inputs):
            facts = inputs["platform_lists"][0]
            facts["listed"]["dropdash"] = ["invented-item"]

        def invented_crowding_evidence(inputs):
            inputs["crowding"] = (inputs["crowding"][0], sorted(inputs["crowding"][1] + ["invented-listing"]))

        cases = (
            (rescored(low), "FUNCTIONAL_MARKET_SCORE_CHANGED"),                          # level changed in the score only
            (rescored(invented_listing), "FUNCTIONAL_MARKET_SCORE_CHANGED"),             # invented platform item
            (rescored(invented_crowding_evidence), "FUNCTIONAL_MARKET_SCORE_CHANGED"),   # invented crowding listing
            (refrozen("crowding", lambda r: r.update(level="low", rule="few_listings")), "FUNCTIONAL_MARKET_SCORE_FACTS_INVALID"),
            (refrozen("crowding", lambda r: r.update(rule="few_listings")), "FUNCTIONAL_MARKET_SCORE_FACTS_INVALID"),
            (refrozen("crowding", lambda r: r.update(concept_id="another-type")), "FUNCTIONAL_MARKET_SCORE_FACTS_INVALID"),
            (refrozen("crowding", lambda r: r.update(evidence_ids=["zz-listing", "aa-listing"])), "FUNCTIONAL_MARKET_SCORE_FACTS_INVALID"),
            (refrozen("presence", lambda r: r["platforms"]["deodap"]["items"][0].update(observation_id="invented-item")),
             "FUNCTIONAL_MARKET_SCORE_FACTS_INVALID"),                                    # its saved change hash no longer holds
            (refrozen("presence", lambda r: r.update(meaning_hash="0" * 64)), "FUNCTIONAL_MARKET_SCORE_FACTS_INVALID"),
            (missing_platform_row, "FUNCTIONAL_MARKET_SCORE_FACTS_INVALID"),
            (forged_low_fact_and_score, "FUNCTIONAL_MARKET_SCORE_FACTS_INVALID"),       # the ladder on its own counts says High
            (refrozen("crowding", lambda r: r.update(thresholds_version="crowding-thresholds-v0")), "FUNCTIONAL_MARKET_SCORE_FACTS_INVALID"),
            (refrozen("presence", lambda r: r.update(at="2026-10-06T10:00:00+00:00")), "FUNCTIONAL_MARKET_SCORE_FACTS_INVALID"),
            (lambda row: row["functional_market_proof"]["score_facts"]["crowding"].update(id="crowding-fact-other"),
             "FUNCTIONAL_MARKET_SCORE_FACTS_INVALID"),
            (lambda row: row["functional_market_proof"]["score_facts"]["presence"].update(id="dropship-presence-other"),
             "FUNCTIONAL_MARKET_SCORE_FACTS_INVALID"),
            (lambda row: row["functional_market_proof"]["score_facts"].update(presence=None), "FUNCTIONAL_MARKET_SCORE_CHANGED"),
            (lambda row: row["functional_market_proof"]["score_facts"]["crowding"]["record"].update(sha256="0" * 64),
             "FROZEN_ORIGINAL_JSON_HASH_MISMATCH"),
            (lambda row: row["functional_market_proof"]["score_facts"].update(extra=None), "FUNCTIONAL_MARKET_SCORE_FACTS_INVALID"),
            (lambda row: row["functional_market_proof"]["score_facts"].update(crowding=None), "FUNCTIONAL_MARKET_SCORE_FACTS_INVALID"),
            (lambda row: row["functional_market_proof"].pop("score_facts"), "FUNCTIONAL_MARKET_PROOF_INVALID"),
        )
        for mutate, code in cases:
            changed = deepcopy(fact); mutate(changed)
            with self.subTest(code=code, mutate=mutate), self.assertRaisesRegex(SafetyError, code):
                validate_frozen_functional_market(changed)
        # The record validators themselves, as a report copy uses them offline.
        from product_radar.crowding import validate_crowding_fact
        from product_radar.market_validation import score_fact_inputs
        from product_radar.platform_roles import validate_presence_fact
        meaning = fact["functional_definition"]["meaning_hash"]
        presence = read_original_json(facts["presence"]["record"])
        crowding = read_original_json(facts["crowding"]["record"])
        self.assertEqual(validate_presence_fact(presence, self.cid, meaning), presence)
        self.assertEqual(validate_crowding_fact(crowding, self.cid, meaning), crowding)
        for record in (dict(presence, concept_id="another-type"), dict(presence, platforms=[1]),
                       {key: value for key, value in presence.items() if key != "at"}):
            with self.assertRaisesRegex(SafetyError, "DROPSHIP_PRESENCE_RECORD_INVALID"):
                validate_presence_fact(record, "another-type" if record.get("concept_id") == "another-type" else self.cid, meaning)
        pending = dict(crowding, level="not_checked", rule=None, reason="CROWDING_READS_PENDING",
                       counts=dict(crowding["counts"], bestseller_hits=0))
        self.assertEqual(validate_crowding_fact(pending, self.cid, meaning), pending)
        for record in (dict(pending, rule="few_listings"), dict(pending, reason="CROWDING_CHECKED"),
                       dict(pending, counts=crowding["counts"]),  # its own counts already show High
                       dict(crowding, counts=dict(crowding["counts"], card_facts_missing="1")),
                       dict(crowding, evidence_ids=["bad id!"]), {key: value for key, value in crowding.items() if key != "reason"},
                       dict(crowding, counts=None), dict(crowding, meaning_hash=None)):
            with self.assertRaisesRegex(SafetyError, "CROWDING_FACT_INVALID"):
                validate_crowding_fact(record, self.cid, meaning if record.get("meaning_hash") else None)
        for proof in ({}, {"score_facts": []}):
            with self.assertRaisesRegex(SafetyError, "FUNCTIONAL_MARKET_SCORE_FACTS_INVALID"):
                score_fact_inputs(proof, self.cid, meaning)

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

    def test_retired_ai_market_opinion_changes_neither_market_facts_nor_score(self):
        # The relevance review still returns a per-site opinion with its answer
        # (prompt unchanged); the score is measured from facts and ignores it.
        at, fit = "2026-10-08T10:00:00+00:00", {"fit_policy_id": FIT_POLICY_ID, "kpi_total": 10, "kpi_matches": 6}
        results = []
        for judgment in ("reasonably_positive", "neutral"):
            self.setUp()
            self.review(SourceRunner(judgment=judgment) if judgment != "neutral" else SourceRunner())
            state = self.memory.snapshot(); context = functional_market_context(state, "first", as_of=at)
            amazon = context["market"]["sources"]["amazon"]
            self.assertTrue(amazon["available"])
            self.assertFalse({"evidence_support", "support_judgment", "support_review_id"} & set(amazon))
            self.assertNotIn("source_support", context["market"])
            self.assertTrue(set(self.refs) <= set(context["matches"]))
            score = score_type(state, "first", fit, context["market"], as_of=at)
            support = functional_score_support(state, context, score, as_of=at)
            results.append((context["market"]["sources"], score["opportunity_basis"]["parts"]["want"]["points"],
                            support["score_support"]))
        self.assertEqual(results[0][0], results[1][0])
        self.assertEqual(results[0][1:], results[1][1:])

