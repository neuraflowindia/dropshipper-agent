"""Crowding level per narrow type from reviewed relevant listings (Plan R10 W6).

Every type, review and listing below is synthetic. The saved pages are trimmed
real first results pages, read by parser v3. No model, page or provider call.
"""

from copy import deepcopy
import json
from pathlib import Path
import shutil
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "tests"))

from product_radar import crowding
from product_radar.crowding import (CALIBRATION_KIND, NOT_CHECKED, assess_crowding, calibration_record, crowding_due,
                                    crowding_for, crowding_known, crowding_level, load_thresholds, refresh_crowding,
                                    validate_thresholds)
from product_radar.functional_types import effective_functional_definition
from product_radar.models import SafetyError, digest
from product_radar.report_examples import RECOMMENDATION_CONTRACT
from product_radar.report_selection import build_report_pool
from product_radar.research_memory import change, key
from product_radar.research_scope import accept_scoped_evidence
from product_radar.source_coverage import CHECK_KIND, TASK_KIND
from test_functional_source_market import functional_pool_fixture
from test_research_flow import NOW

THRESHOLDS = load_thresholds()
FIXTURES = ROOT / "tests/fixtures/crowding"


def listing(site="amazon", *, ratings=None, brand=None, bought=None, bestseller=False, readable=True, badge=False,
            stars=None, title=""):
    card = {"ratings_count": ratings, "star_rating": stars, "brand": brand, "bought_past_month": bought,
            "bought_text": None, "sponsored": False, "list_rank": None, "bestseller_badge": badge, "position": 1}
    return {"site": site, "bestseller": bestseller, "card": card if readable else None, "title": title,
            "evidence_ids": ["obs-" + str(id(card))]}


class LevelRuleTests(unittest.TestCase):
    """Every boundary of the owner's draft thresholds (config, version v1)."""

    def level(self, rows):
        return crowding_level(rows, THRESHOLDS)[:2]

    def test_bestseller_hit_is_high_even_without_card_facts(self):
        self.assertEqual(self.level([listing(bestseller=True, readable=False)]), ("high", "bestseller_list"))

    def test_three_listings_with_1000_ratings_is_the_well_rated_boundary(self):
        self.assertEqual(self.level([listing(ratings=1000)] * 3), ("high", "well_rated_listings"))
        self.assertEqual(self.level([listing(ratings=1000)] * 2 + [listing(ratings=999)]), ("medium", "default"))

    def test_eight_listings_showing_five_brands_is_the_brand_boundary(self):
        brands = ["A", "B", "C", "D", "E", "A", "B", "C"]
        self.assertEqual(self.level([listing(brand=brand) for brand in brands]), ("high", "many_brands_shown"))
        self.assertEqual(self.level([listing(brand=brand) for brand in brands[:7]]), ("medium", "default"))
        self.assertEqual(self.level([listing(brand=brand) for brand in ["A", "a ", "B", "C", "D", "A", "B", "C"]]),
                         ("medium", "default"))  # Brand text compared without case or spacing.
        # A listing whose card shows no brand never counts towards the brand rule.
        level, rule, counts = crowding_level([listing(brand=brand) for brand in brands[:5]] + [listing()] * 3, THRESHOLDS)
        self.assertEqual((level, counts["listings_with_brand"], counts["brand_not_shown"]), ("medium", 5, 3))

    def test_two_listings_with_1k_bought_is_the_bought_boundary(self):
        self.assertEqual(self.level([listing(bought=1000), listing("flipkart", bought=5000)]), ("high", "bought_past_month"))
        self.assertEqual(self.level([listing(bought=1000, ratings=9), listing(bought=999, ratings=9)]), ("low", "few_listings"))

    def test_amazon_best_seller_badge_on_a_search_card_is_a_bestseller_hit(self):
        level, rule, counts = crowding_level([listing(badge=True, ratings=12)], THRESHOLDS)
        self.assertEqual((level, rule, counts["bestseller_hits"], counts["bestseller_badges"]), ("high", "bestseller_list", 1, 1))

    def test_low_needs_at_most_two_listings_and_none_with_200_ratings(self):
        self.assertEqual(self.level([]), ("low", "few_listings"))
        self.assertEqual(self.level([listing(ratings=199), listing("flipkart", ratings=0)]), ("low", "few_listings"))
        self.assertEqual(self.level([listing(ratings=200)]), ("medium", "default"))
        self.assertEqual(self.level([listing(ratings=1), listing(ratings=1), listing("flipkart", ratings=1)]),
                         ("medium", "default"))

    def test_an_unknown_rating_count_never_supports_low(self):
        self.assertEqual(self.level([listing(ratings=5), listing("flipkart", ratings=None)]), ("medium", "default"))

    def test_colour_and_size_variants_of_one_product_count_once(self):
        variants = [listing(ratings=1500, stars="4.1", title=f"Acme Rain Cover for Backpack, {colour}")
                    for colour in ("Black", "Blue", "Red")]
        collapsed = crowding._collapse_variants(variants)
        self.assertEqual(len(collapsed), 1)
        level, rule, counts = crowding_level(collapsed, THRESHOLDS)
        self.assertEqual((level, counts["listings_rated_high"], counts["variants_collapsed"]), ("medium", 1, 2))
        self.assertEqual(len(collapsed[0]["evidence_ids"]), 3)
        # Different products, another site or an unknown count are never merged.
        other = [listing(ratings=1500, stars="4.1", title="Other Brand Cover"), listing("flipkart", ratings=1500, stars="4.1",
                 title="Acme Rain Cover for Backpack, Black"), listing(ratings=None, title="Acme Rain Cover for Backpack, Grey")]
        self.assertEqual(len(crowding._collapse_variants(variants + other)), 4)
        self.assertEqual(self.level(variants + other[:2]), ("high", "well_rated_listings"))

    def test_unreadable_card_is_never_low(self):
        level, rule, counts = crowding_level([listing(readable=False)], THRESHOLDS)
        self.assertEqual((level, rule, counts["card_facts_missing"]), (None, "saved_page_unreadable", 1))

    def test_counts_behind_the_level(self):
        _, _, counts = crowding_level([listing(ratings=1500, brand="Acme", bought=2000), listing("flipkart"),
                                       listing(bestseller=True, ratings=250)], THRESHOLDS)
        self.assertEqual(counts, {"relevant_listings": {"amazon": 2, "flipkart": 1}, "brands_or_sellers": 1,
            "listings_with_brand": 1, "brand_not_shown": 2, "max_ratings": 1500, "listings_rated_high": 1, "listings_rated_200_plus": 2, "bought_past_month_hits": 1,
            "bestseller_hits": 1, "bestseller_badges": 0, "sponsored_listings": 0, "ratings_not_shown": 1,
            "card_facts_missing": 0, "variants_collapsed": 0})


class SettingsTests(unittest.TestCase):
    def test_saved_settings_are_valid_and_numbers_only(self):
        self.assertEqual(THRESHOLDS["version"], "crowding-thresholds-v1")
        for change_settings in (lambda s: s["high"].pop("brands"), lambda s: s["high"].update(listings="8"),
                                lambda s: s["high"].update(listings=True), lambda s: s.update(kind="other"),
                                lambda s: s["calibration"].update(high_share_below="0.80"),
                                lambda s: s["calibration"].update(high_share_above="1.5"),
                                lambda s: s["calibration"].update(high_share_above=0.7),
                                lambda s: s["calibration"].update(high_share_above="seventy"),
                                lambda s: s.update(version="bad version"), lambda s: s.pop("low")):
            value = deepcopy(THRESHOLDS); change_settings(value)
            with self.assertRaises(SafetyError):
                validate_thresholds(value)
        with self.assertRaises(SafetyError):
            validate_thresholds(None)


class TypeFixture(unittest.TestCase):
    """A real accepted type with its own Amazon and Flipkart search reads."""

    def setUp(self):
        temp = tempfile.TemporaryDirectory(); self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)

    def make(self, *, with_market=False, count=1):
        self.memory, ids = functional_pool_fixture(self.root, count=count, with_market=with_market)
        self.cid = ids[0]
        return ids

    def capture(self, name, route):
        folder = self.root / "pages" / name
        folder.mkdir(parents=True)
        shutil.copy(FIXTURES / (name + ".html"), folder / "rendered.html")
        shutil.copy(FIXTURES / (name + ".snapshot.json"), folder / "snapshot.json")
        capture_id = "capture-" + name
        self.memory.commit(key("fixture-capture", capture_id), [change("captures", capture_id, {
            "source": "flipkart" if route == "flipkart-search" else "amazon", "acquisition_id": "acq-" + name,
            "observed_at": NOW, "capture_hash": "0" * 64, "mapping_hash": "0" * 64, "mapping_status": "partial",
            "reference": {"route": route, "folder": str(folder), "file": "receipt.json", "request": {}, "receipt_hash": "0" * 64}})])
        return capture_id

    def observe(self, capture_id, route, identity, *, relationship="relevant_family"):
        state = self.memory.snapshot()
        source = state["captures"][capture_id]["source"]
        observation_id = "obs-" + identity
        semantic = {"title": "Synthetic listing " + identity, "product_url": "https://example.org/" + identity}
        row = {"source": source, "acquisition_id": state["captures"][capture_id]["acquisition_id"],
               "source_item": identity + ":fixture", "capture_id": capture_id, "observed_at": NOW,
               "semantic_id": key("item", semantic), "semantic": semantic,
               "facts": {"asin" if source == "amazon" else "product_id": identity, "source_route": route,
                         "list_type": "product_search", "query": "synthetic", "direct_capture": {"capture_id": capture_id}}}
        self.memory.commit(key("fixture-observation", observation_id), [change("observations", observation_id, row)])
        accept_scoped_evidence(self.memory, self.cid, route=route, scope="narrow_type", relationship=relationship,
            support="insufficient", references=[{"table": "observations", "id": observation_id, "hash": digest(row)}],
            usable_fields=[{"reference": {"table": "observations", "id": observation_id}, "path": ["semantic"]}],
            limitations=["Synthetic listing."], reason="Explicit fixture type comparison.", reviewer="synthetic",
            as_of=NOW, expected_meaning_hash=effective_functional_definition(self.memory.snapshot(), self.cid)["meaning_hash"])
        return observation_id

    def task(self, route, *, status="acquired", capture_ids=(), observation_ids=()):
        state = self.memory.snapshot()
        request = {"route": route, "term": "synthetic " + route}
        task_id = key("fixture-task", [self.cid, route, status])
        envelope = {"route": route, "acquisition_id": "acq-" + task_id, "observed_at": NOW, "capture_ids": list(capture_ids),
                    "observation_ids": list(observation_ids), "evidence_refs": []}
        receipt_id = "receipt-" + task_id
        receipts = [] if status in ("prepared", "failed") else [receipt_id]
        task = {"kind": TASK_KIND, "run_id": "fixture-run", "route": route, "source": "marketplace", "request": request,
                "request_hash": digest(request), "targets": {self.cid: digest(state["concepts"][self.cid]["description"])},
                "target_results": {}, "status": status, "receipt_refs": receipts, "attempt_refs": ["acq-" + task_id],
                "acquisition_id": "acq-" + task_id, "prepared_at": NOW, "tries": 1}
        changes = [change("query_tasks", task_id, task)]
        if receipts:
            changes.append(change("source_checks", receipt_id, {"kind": CHECK_KIND, "task_id": task_id, "route": route,
                "request_hash": task["request_hash"], "envelope": envelope, "envelope_hash": digest(envelope), "at": NOW}))
        self.memory.commit(key("fixture-task-save", task_id), changes)
        return task_id


class CrowdingFactTests(TypeFixture):
    def test_relevant_bestseller_listing_is_high_before_both_searches_are_read(self):
        self.make(with_market=True)
        fact = crowding_for(self.memory.snapshot(), self.cid)  # The pool fixture runs the real writer.
        self.assertEqual((fact["level"], fact["rule"], fact["reason"]), ("high", "bestseller_list", "CROWDING_CHECKED"))
        self.assertEqual(fact["reads"], {"amazon-search": "not_started", "flipkart-search": "not_started"})
        self.assertEqual(fact["counts"]["bestseller_hits"], 1)
        self.assertEqual(fact["counts"]["card_facts_missing"], 1)  # An old paid list row has no saved page.
        self.assertTrue(crowding_known(fact))

    def test_no_reads_is_not_checked_never_low_and_blocks_report_eligibility(self):
        self.make()  # The pool fixture already ran the real writer once.
        result = refresh_crowding(self.memory, [self.cid], as_of=NOW)
        self.assertEqual((result["state"], result["saved"]), ("nothing_due", 0))
        fact = crowding_for(self.memory.snapshot(), self.cid)
        self.assertEqual((fact["level"], fact["reason"], fact["rule"]), (NOT_CHECKED, "CROWDING_READS_PENDING", None))
        self.assertFalse(crowding_known(fact))
        pool = build_report_pool(self.memory, as_of=NOW, recommendation_contract=RECOMMENDATION_CONTRACT)
        decision = next(row for row in pool["decisions"] if row["concept_id"] == self.cid)
        self.assertIn("crowding_pending", decision["codes"])
        self.assertFalse(pool["eligible"])

    def test_both_searches_read_with_few_quiet_listings_is_low(self):
        self.make()
        amazon = self.capture("amazon-search-baby-bath-towels", "amazon-search")
        flipkart = self.capture("flipkart-search-tealight-candles", "flipkart-search")
        self.observe(amazon, "amazon-search", "B0F27RZYLM")              # 60 ratings
        self.observe(flipkart, "flipkart-search", "CANHPF5ZV3HHZWPD")    # 6 ratings
        self.observe(flipkart, "flipkart-search", "CANGTRDVYFKHHRRM", relationship="unrelated")
        self.task("amazon-search", capture_ids=[amazon]); self.task("flipkart-search", status="completed_no_results",
                                                                    capture_ids=[flipkart])
        self.assertTrue(crowding_due(self.memory.snapshot(), self.cid, as_of=NOW))
        refresh_crowding(self.memory, [self.cid], as_of=NOW)
        fact = crowding_for(self.memory.snapshot(), self.cid)
        self.assertEqual((fact["level"], fact["rule"]), ("low", "few_listings"))
        self.assertEqual(fact["counts"]["relevant_listings"], {"amazon": 1, "flipkart": 1})
        self.assertEqual(fact["counts"]["max_ratings"], 60)
        self.assertEqual(fact["search_results"], {"amazon": {"total": 20000, "lower_bound": True},
                                                  "flipkart": {"total": 11623, "lower_bound": False}})
        self.assertEqual(fact["evidence_ids"], ["obs-B0F27RZYLM", "obs-CANHPF5ZV3HHZWPD"])
        self.assertFalse(crowding_due(self.memory.snapshot(), self.cid, as_of=NOW))
        # The answer is saved once; an unchanged second check writes nothing.
        self.assertEqual(refresh_crowding(self.memory, [self.cid], as_of=NOW)["saved"], 0)
        decision = next(row for row in build_report_pool(self.memory, as_of=NOW,
            recommendation_contract=RECOMMENDATION_CONTRACT)["decisions"] if row["concept_id"] == self.cid)
        self.assertNotIn("crowding_pending", decision["codes"])

    def test_well_rated_listings_make_high_and_a_third_listing_makes_medium(self):
        self.make()
        amazon = self.capture("amazon-search-camphor-cones", "amazon-search")
        flipkart = self.capture("flipkart-search-tealight-candles", "flipkart-search")
        self.task("amazon-search", capture_ids=[amazon]); self.task("flipkart-search", capture_ids=[flipkart])
        self.observe(flipkart, "flipkart-search", "CANGTRDVYFKHHRRM")    # 1,401 ratings
        self.observe(flipkart, "flipkart-search", "CANHQP28RVTG6ZAA")    # 216 ratings
        refresh_crowding(self.memory, [self.cid], as_of=NOW)
        self.assertEqual(crowding_for(self.memory.snapshot(), self.cid)["level"], "medium")
        self.observe(amazon, "amazon-search", "B0BLLNWWY5")              # 3,752 ratings, 900+ bought
        self.observe(amazon, "amazon-search", "B07ZJSK969")              # 5,813 ratings, 1K+ bought
        refresh_crowding(self.memory, [self.cid], as_of=NOW)
        fact = crowding_for(self.memory.snapshot(), self.cid)
        self.assertEqual((fact["level"], fact["rule"]), ("high", "well_rated_listings"))
        self.assertEqual((fact["counts"]["brands_or_sellers"], fact["counts"]["sponsored_listings"]), (1, 1))

    def test_flipkart_cards_without_ratings_make_quiet_reads_medium_not_low(self):
        self.make()
        amazon = self.capture("amazon-search-baby-bath-towels", "amazon-search")
        flipkart = self.capture("flipkart-search-backpack-rain-covers", "flipkart-search")
        self.observe(amazon, "amazon-search", "B0F27RZYLM")              # 60 ratings
        self.observe(flipkart, "flipkart-search", "LGCHFZPHKAWC2EPK")    # Flipkart shows no ratings here
        self.task("amazon-search", capture_ids=[amazon]); self.task("flipkart-search", capture_ids=[flipkart])
        refresh_crowding(self.memory, [self.cid], as_of=NOW)
        fact = crowding_for(self.memory.snapshot(), self.cid)
        self.assertEqual((fact["level"], fact["counts"]["ratings_not_shown"]), ("medium", 1))

    def test_best_seller_badge_on_a_saved_search_card_makes_high(self):
        self.make()
        amazon = self.capture("amazon-search-backpack-rain-covers", "amazon-search")
        self.observe(amazon, "amazon-search", "B0D1CNN4MG")              # "Best seller in Bag & Backpack Covers"
        refresh_crowding(self.memory, [self.cid], as_of=NOW)
        fact = crowding_for(self.memory.snapshot(), self.cid)
        self.assertEqual((fact["level"], fact["rule"], fact["counts"]["bestseller_badges"]), ("high", "bestseller_list", 1))

    def test_pending_review_failed_reads_and_unreadable_pages_stay_not_checked(self):
        self.make()
        amazon = self.capture("amazon-search-baby-bath-towels", "amazon-search")
        observation = self.observe(amazon, "amazon-search", "B0F27RZYLM")
        self.task("amazon-search", capture_ids=[amazon], observation_ids=[observation])
        self.task("flipkart-search", status="failed")
        fact = assess_crowding(self.memory.snapshot(), self.cid, as_of=NOW, thresholds=THRESHOLDS)
        # An operator note is not the type's completed AI review of its own read.
        self.assertEqual(fact["reads"], {"amazon-search": "review_pending", "flipkart-search": "failed"})
        self.assertEqual((fact["level"], fact["reason"]), (NOT_CHECKED, "CROWDING_READS_PENDING"))
        self.task("flipkart-search", status="prepared")
        self.assertEqual(assess_crowding(self.memory.snapshot(), self.cid, as_of=NOW, thresholds=THRESHOLDS)["reads"]
                         ["flipkart-search"], "pending")

    def test_unreadable_saved_page_with_complete_reads_is_not_checked(self):
        self.make()
        amazon = self.capture("amazon-search-baby-bath-towels", "amazon-search")
        flipkart = self.capture("flipkart-search-tealight-candles", "flipkart-search")
        self.observe(amazon, "amazon-search", "B0F27RZYLM")
        self.task("amazon-search", capture_ids=[amazon]); self.task("flipkart-search", capture_ids=[flipkart])
        refresh_crowding(self.memory, [self.cid], as_of=NOW)
        self.assertEqual(crowding_for(self.memory.snapshot(), self.cid)["level"], "low")
        page = self.root / "pages/amazon-search-baby-bath-towels/rendered.html"
        held = page.with_suffix(".held"); page.rename(held)
        # The saved page's file state is part of the check's inputs: it is looked at again.
        self.assertTrue(crowding_due(self.memory.snapshot(), self.cid, as_of=NOW))
        refresh_crowding(self.memory, [self.cid], as_of=NOW)
        fact = crowding_for(self.memory.snapshot(), self.cid)
        self.assertEqual((fact["level"], fact["reason"]), (NOT_CHECKED, "CROWDING_SAVED_PAGE_UNREADABLE"))
        self.assertEqual(fact["unreadable_captures"], [amazon])
        held.rename(page)
        self.assertTrue(crowding_due(self.memory.snapshot(), self.cid, as_of=NOW))
        refresh_crowding(self.memory, [self.cid], as_of=NOW)
        self.assertEqual(crowding_for(self.memory.snapshot(), self.cid)["level"], "low")
        page.unlink()
        failing = assess_crowding(self.memory.snapshot(), self.cid, as_of=NOW, thresholds=THRESHOLDS,
                                  card_reader=lambda folder, route: (_ for _ in ()).throw(OSError("disk")))
        self.assertEqual(failing["level"], NOT_CHECKED)

    def test_reader_returns_not_checked_for_changed_meaning_or_thresholds(self):
        self.make(with_market=True)
        state = self.memory.snapshot()
        newer = deepcopy(THRESHOLDS); newer["version"] = "crowding-thresholds-v2"
        self.assertEqual(crowding_for(state, self.cid, thresholds=newer)["reason"], "CROWDING_NOT_ASSESSED")
        self.assertTrue(crowding_due(state, self.cid, as_of=NOW, thresholds=newer))
        moved = deepcopy(state); moved["batch_runs"][key("crowding-fact", self.cid)]["meaning_hash"] = "f" * 64
        self.assertEqual(crowding_for(moved, self.cid)["level"], NOT_CHECKED)


class RefreshTests(TypeFixture):
    def test_only_due_types_are_checked_priority_first_and_bounded(self):
        ids = self.make(with_market=True, count=3)  # The fixture saved all three already.
        self.assertEqual(refresh_crowding(self.memory, ids, as_of=NOW)["state"], "nothing_due")
        newer = deepcopy(THRESHOLDS); newer["version"] = "crowding-thresholds-v2"
        first = refresh_crowding(self.memory, ids, as_of=NOW, priority=[ids[2]], limit=1, thresholds=newer)
        self.assertEqual((first["assessed"], first["saved"], first["more_due"]), (1, 1, True))
        state = self.memory.snapshot()
        self.assertEqual([crowding_known(crowding_for(state, cid, thresholds=newer)) for cid in ids], [False, False, True])
        second = refresh_crowding(self.memory, ids, as_of=NOW, limit=1, thresholds=newer)
        third = refresh_crowding(self.memory, ids, as_of=NOW, limit=1, thresholds=newer)
        self.assertEqual((second["assessed"], third["assessed"], third["more_due"]), (1, 1, False))
        self.assertTrue(all(crowding_known(crowding_for(self.memory.snapshot(), cid, thresholds=newer)) for cid in ids))
        self.assertEqual(refresh_crowding(self.memory, ids + ["missing-type"], as_of=NOW, thresholds=newer)["state"],
                         "nothing_due")
        for value in ([1], (), "x"):
            with self.assertRaisesRegex(SafetyError, "CROWDING_REFRESH_INVALID"):
                refresh_crowding(self.memory, value if value != () else ids, as_of=NOW, limit=0 if value == () else 5)
        self.assertEqual(refresh_crowding(self.memory, [], as_of=NOW)["state"], "nothing_due")

    def test_one_refresh_reads_the_latest_observations_once_and_saves_once(self):
        from unittest.mock import patch
        from product_radar import discovery
        ids = self.make(with_market=True, count=3)
        newer = deepcopy(THRESHOLDS); newer["version"] = "crowding-thresholds-v3"
        events = len(list((self.memory.root / "events").glob("*.json")))
        with patch.object(discovery, "latest_observations", wraps=discovery.latest_observations) as latest:
            result = refresh_crowding(self.memory, ids, as_of=NOW, thresholds=newer)
        self.assertEqual((result["assessed"], result["saved"]), (3, 3))
        self.assertEqual(latest.call_count, 1)  # shared by the three types, not once per type
        self.assertEqual(len(list((self.memory.root / "events").glob("*.json"))), events + 1)  # one save
        self.assertTrue(all(crowding_known(crowding_for(self.memory.snapshot(), cid, thresholds=newer)) for cid in ids))
        # A saved answer that changed after this refresh read it refuses the whole save.
        newest = deepcopy(THRESHOLDS); newest["version"] = "crowding-thresholds-v4"
        original = self.memory.snapshot
        def stale():
            state = original()
            record_id = crowding.fact_id(ids[0])
            state["batch_runs"][record_id] = dict(state["batch_runs"][record_id], level="stale")
            return state
        with patch.object(self.memory, "snapshot", side_effect=[stale()] + [original() for _ in range(5)]):
            with self.assertRaises(SafetyError):
                refresh_crowding(self.memory, ids, as_of=NOW, thresholds=newest)
        self.assertFalse(any(crowding_known(crowding_for(self.memory.snapshot(), cid, thresholds=newest)) for cid in ids))

    def test_programme_crowding_stage_checks_its_scope_after_market_review(self):
        import product_radar.programme as programme
        from unittest.mock import patch
        ids = self.make(with_market=True, count=2)
        # A new weekly programme reviews no new types and carries earlier ones
        # for reports. After a thresholds change every carried type is checked again.
        record = programme.prepare_programme(self.memory, "next-week", concept_ids=[], as_of=NOW,
                                             reporting_inventory_concept_ids=ids)
        newer = deepcopy(THRESHOLDS); newer["version"] = "crowding-thresholds-v2"
        with patch("product_radar.crowding.load_thresholds", return_value=newer):
            result = programme._crowding(self.memory, record, {"at": NOW, "priority_concept_ids": {"crowding": [ids[1]]}})
            self.assertEqual((result["state"], result["assessed"], result["levels"]), ("checked", 2, {"high": 2}))
            self.assertTrue(all(crowding_known(crowding_for(self.memory.snapshot(), cid)) for cid in ids))
            child = dict(record, parent_programme_id=record["programme_id"], concept_ids=[ids[0]])
            self.assertEqual(programme._crowding(self.memory, child, {"at": NOW})["state"], "nothing_due")
        self.assertIs(programme.DEFAULT_STEPS["crowding"], programme._crowding)
        self.assertEqual(programme.STAGES.index("crowding"), programme.STAGES.index("match") + 1)
        self.assertEqual(programme.STAGES.index("suppliers"), programme.STAGES.index("crowding") + 1)

    def test_type_without_accepted_meaning_is_not_saved(self):
        self.make(with_market=True)
        state = self.memory.snapshot()
        fact = assess_crowding(dict(state, source_checks={}), self.cid, as_of=NOW, thresholds=THRESHOLDS)
        self.assertEqual(fact["unreadable_captures"], [])
        self.assertEqual((fact["level"], fact["reason"], fact["meaning_hash"]), (NOT_CHECKED, "CROWDING_MEANING_PENDING", None))
        self.assertFalse(crowding_due(dict(state, source_checks={}), self.cid, as_of=NOW))


class CalibrationTests(TypeFixture):
    def test_first_checked_types_record_one_calibration_outcome_per_threshold_version(self):
        ids = self.make(with_market=True, count=3)
        state = self.memory.snapshot()
        self.assertIsNone(calibration_record(state))
        low = deepcopy(THRESHOLDS); low["calibration"]["after_checked_types"] = 3
        result = refresh_crowding(self.memory, ids, as_of=NOW, thresholds=low)
        self.assertEqual(result["calibration"], "calibration_needed")  # 3 of 3 High is above 70%.
        record = calibration_record(self.memory.snapshot(), low)
        self.assertEqual((record["kind"], record["checked_types"], record["high_types"], record["high_share"],
                          record["levels"]), (CALIBRATION_KIND, 3, 3, "1.0000", {"low": 0, "medium": 0, "high": 3}))
        summary = crowding.crowding_summary(self.memory.snapshot(), low)
        self.assertEqual((summary["calibration"], summary["levels"]["high"]), ("calibration_needed", 3))
        self.assertTrue(summary["alert"].startswith("CROWDING CALIBRATION NEEDED: 1.0000 of the first 3"))
        self.assertIn("calibration calibration needed", summary["text"])
        again = refresh_crowding(self.memory, ids, as_of="2026-10-15T00:00:00+00:00", thresholds=low)
        self.assertEqual((again["calibration"], calibration_record(self.memory.snapshot(), low)["at"]),
                         ("calibration_needed", record["at"]))

    def test_owner_meter_shows_the_crowding_line_and_calibration_alert(self):
        from unittest.mock import patch
        import product_radar.programme as programme
        ids = self.make(with_market=True, count=3)
        summary = crowding.crowding_summary(self.memory.snapshot())
        self.assertEqual((summary["calibration"], summary["alert"]), ("waiting_for_sample", None))
        self.assertEqual(summary["text"], "Amazon/Flipkart crowding: 3 types checked (3 high, 0 medium, 0 low), "
                                          "0 not checked yet; calibration waiting for sample (3 of 50).")
        low = deepcopy(THRESHOLDS); low["calibration"]["after_checked_types"] = 3; low["version"] = "crowding-thresholds-v3"
        with patch("product_radar.crowding.load_thresholds", return_value=low):
            refresh_crowding(self.memory, ids, as_of=NOW)
            meter = programme.programme_meter(self.memory, root=self.root, as_of=NOW)
        self.assertNotIn("crowding_check", meter["optional_input_failures"])
        self.assertEqual(meter["crowding_check"]["calibration"], "calibration_needed")
        self.assertTrue(meter["crowding_check"]["alert"].startswith("CROWDING CALIBRATION NEEDED"))

    def test_owner_meter_shows_the_opportunity_score_tally(self):
        import product_radar.programme as programme
        self.make(with_market=True, count=3)
        meter = programme.programme_meter(self.memory, root=self.root, as_of=NOW)
        self.assertNotIn("opportunity_score", meter["optional_input_failures"])
        score = meter["opportunity_score"]
        self.assertEqual((score["scored"], score["labels"]["proven_but_crowded"], score["calibration"], score["alert"]),
                         (3, 3, "waiting_for_sample", None))
        self.assertEqual(score["text"], "Opportunity score: 3 types scored (0 sweet spot, 0 early bet, 3 proven but crowded, "
                                        "0 too quiet), 0 waiting for the crowding check; calibration waiting for sample (3 of 50).")

    def test_in_range_share_and_too_few_checked_types(self):
        ids = self.make(with_market=True, count=2)
        wide = deepcopy(THRESHOLDS)
        wide["calibration"].update(after_checked_types=2, high_share_above="1", high_share_below="0")
        self.assertEqual(refresh_crowding(self.memory, ids, as_of=NOW, thresholds=wide)["calibration"], "in_range")
        many = deepcopy(THRESHOLDS); many["version"] = "crowding-thresholds-v9"
        self.assertIsNone(refresh_crowding(self.memory, ids, as_of=NOW, thresholds=many)["calibration"])


if __name__ == "__main__":
    unittest.main()
