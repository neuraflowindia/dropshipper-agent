"""The opportunity score out of 100 (owner's weights, 15 October 2026), synthetic and offline."""

from copy import deepcopy
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from product_radar import opportunity_score as score
from product_radar.discovery import MARKET_SITES
from product_radar.fit_policy import ACTIVE_CRITERION_IDS, FIT_POLICY_ID
from product_radar.issue_allocation import allocate_issues
from product_radar.models import SafetyError
from product_radar.research_memory import ResearchMemory, key
from product_radar.service import CATEGORIES, policy_for_fit
from product_radar.storage import Journal

NOW = "2026-10-15T10:00:00+00:00"
PRESENCE_AT = "2026-10-14T10:00:00+00:00"
SETTINGS = json.loads((ROOT / "config/research/opportunity_score.json").read_text())


def market(*, ads=0, positive=False, advertisers=None, recent=None, trends=None, stock=()):
    sources = {site: {"available": False, "positive": None, "used_ids": [], "reason": "None."} for site in MARKET_SITES}
    if ads:
        sources["meta"] = {"available": True, "positive": positive, "used_ids": [f"ad{i}" for i in range(ads)],
                           "depth": {"advertisers": advertisers, "recent_start_advertisers": recent}}
    if trends is not None:
        sources["google-trends"] = {"available": True, "positive": trends.get("positive", False), "used_ids": ["chart1"],
                                    "nonzero_points": trends.get("nonzero"),
                                    "previous_mean_bounds": trends.get("previous"), "recent_mean_bounds": trends.get("recent"),
                                    "breakout_queries": trends.get("breakout", 0)}
    for app, city, ref in stock:
        row = sources[app]
        row.update(available=True, positive=False, used_ids=sorted(set(row["used_ids"]) | {ref}))
        row.setdefault("stock_locations", {}).setdefault(city, []).append(ref)
    return {"sources": sources}


def presence(*, listed=(), on_list=(), arrivals=(), at=PRESENCE_AT):
    value = {"at": at, "listed": {route: ["item-" + route] for route in listed}, "on_list": {}, "new_arrival": {}}
    for route in on_list:
        value["listed"].setdefault(route, ["item-" + route]); value["on_list"][route] = ["item-" + route]
    for route in arrivals:
        value["listed"].setdefault(route, ["item-" + route]); value["new_arrival"][route] = ["item-" + route]
    return value


def crowding(level="low"):
    known = level != "not_checked"
    return {"level": level, "rule": ({"low": "few_listings", "medium": "default", "high": "bestseller_list"}[level] if known else None),
            "thresholds_version": "crowding-thresholds-v1",
            "counts": {"relevant_listings": {"amazon": 2, "flipkart": 1}, "brands_or_sellers": 2, "listings_with_brand": 2,
                       "brand_not_shown": 1, "max_ratings": 120,
                       "bestseller_hits": 0} if known else None,
            "evidence_ids": ["amz1", "flk1"] if known else []}


def fit(meets=6):
    return {"fit_policy_id": FIT_POLICY_ID, "kpi_matches": meets, "kpi_total": 10}


def evaluate(meets=6, market_value=None, presence_value=None, crowding_value=None):
    return score.evaluate_opportunity(fit(meets), market_value or market(), presence=presence_value,
                                      crowding=crowding_value or crowding())


def points(record, item):
    for part in score.PARTS:
        if item in record["opportunity_basis"]["parts"][part]["items"]:
            return record["opportunity_basis"]["parts"][part]["items"][item]["points"]


def product(record, meets=6, **extra):
    """An allocator / frozen row carrying the record and the fit fields it binds."""
    refs = sorted({ref for refs in score.market_evidence(record).values() for ref in refs})
    return {"concept_id": extra.pop("concept_id", "concept0001"), "category": extra.pop("category", CATEGORIES[0]),
            "identity_reviewed": True, "status": "New", "fit_policy_id": FIT_POLICY_ID,
            "criterion_ids": list(ACTIVE_CRITERION_IDS), "kpi_total": 10,
            "kpi_states": ["Meets"] * meets + ["Unclear"] * (10 - meets), "kpi_matches": meets,
            "fit_tier": "good" if meets >= 6 else "decent", "evidence_refs": refs, **score.opportunity_fields(record), **extra}


WANT5 = market(ads=3, positive=True, advertisers=3)  # Meta positive: 6 want points


class SettingsTests(unittest.TestCase):
    def test_project_settings_are_valid_and_weights_total_100(self):
        settings = score.load_settings()
        self.assertEqual(settings["parts"], {"want": 20, "early": 30, "room": 25, "product": 25})
        self.assertEqual(sum(settings["parts"].values()), 100)
        self.assertEqual(settings["version"], "opportunity-score-settings-v1")

    def test_every_number_and_part_total_is_checked(self):
        def changed(edit):
            value = deepcopy(SETTINGS); edit(value); return value
        for edit in (lambda v: v["parts"].update(want=21),                     # weights no longer total 100
                     lambda v: v["parts"].update(want=21, early=29),           # inner want maxima no longer 21
                     lambda v: v["want"]["facebook_ads"].update(positive=9),   # not below the many-advertisers points
                     lambda v: v["want"]["google_searches"].update(nonzero_points=[[1, 2], [4, 5]]),
                     lambda v: v["want"]["quick_commerce"].update(stock_cities=[[2, 2.0], [1, 1]]),
                     lambda v: v["want"]["quick_commerce"].update(stock_cities=[]),
                     lambda v: v["want"]["platform_lists"].update(on_list_platforms=[[2, 5, 1]]),
                     lambda v: v["early"]["rising_searches"].update(strong=8),
                     lambda v: v["early"]["rising_searches"].update(strong_rise_percent=True),
                     lambda v: v["early"]["new_arrivals"].update(new_arrival_platforms=[[2, 10], [1, 6]]),
                     lambda v: v["room"].update(low=24),
                     lambda v: v["room"].update(medium=26),
                     lambda v: v["product"].update(per_extra_meet=4),
                     lambda v: v["eligibility"].update(min_want=21),
                     lambda v: v["labels"].update(early_bet_min_early=0),
                     lambda v: v["selection"].update(high_crowding_only_below=0),
                     lambda v: v["calibration"].update(too_quiet_share_below="0.80"),
                     lambda v: v["calibration"].update(too_quiet_share_above=0.7),
                     lambda v: v.update(version="bad version"),
                     lambda v: v.update(extra=1),
                     lambda v: v.pop("note"),
                     lambda v: v.update(kind="crowding-thresholds"),
                     lambda v: v["want"].pop("quick_commerce")):
            with self.subTest(edit=edit), self.assertRaisesRegex(SafetyError, "OPPORTUNITY_SETTINGS_INVALID"):
                score.validate_settings(changed(edit))

    def test_malformed_values_are_settings_errors(self):
        for value in (None, [], {"schema_version": 1}, dict(SETTINGS, calibration=dict(SETTINGS["calibration"], too_quiet_share_above="seventy"))):
            with self.subTest(value=str(value)[:40]), self.assertRaisesRegex(SafetyError, "OPPORTUNITY_SETTINGS_INVALID"):
                score.validate_settings(value)

    def test_settings_file_is_read_again_only_when_it_changes(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "score.json"
            path.write_text(json.dumps(SETTINGS))
            self.assertEqual(score.load_settings(path)["version"], SETTINGS["version"])
            path.write_text(json.dumps(dict(SETTINGS, version="opportunity-score-settings-v2")) + "\n")
            self.assertEqual(score.load_settings(path)["version"], "opportunity-score-settings-v2")
            with self.assertRaisesRegex(SafetyError, "OPPORTUNITY_SETTINGS_INVALID"):
                score.load_settings(Path(folder) / "missing.json")
        score.load_settings()  # the project file again


@unittest.skipUnless(__import__("shutil").which("node"), "Website runtime is not installed")
class PortalMirrorTests(unittest.TestCase):
    def test_portal_mirrors_the_saved_fact_kinds_the_score_reads(self):
        import shutil
        import subprocess
        from product_radar import crowding, platform_roles
        from product_radar.dropship_platforms import ROUTES
        from product_radar.marketplace_cards import CARD_PARSER_VERSION
        script = ("import('" + (ROOT / "portal/opportunity-score.mjs").as_uri() + "').then(m => "
                  "console.log(JSON.stringify({presence: m.PRESENCE, crowding: m.CROWDING})))")
        completed = subprocess.run([shutil.which("node"), "--input-type=module", "-e", script],
                                   capture_output=True, text=True, timeout=60, check=True)
        mirror = json.loads(completed.stdout)
        self.assertEqual(mirror["presence"], {"kind": platform_roles.PRESENCE_KIND, "version": platform_roles.PRESENCE_VERSION,
                                              "days": score.PRESENCE_DAYS, "routes": list(ROUTES),
                                              "states": list(platform_roles._STATES)})
        thresholds = crowding.load_thresholds()
        self.assertEqual(mirror["crowding"], {"kind": crowding.FACT_KIND, "version": crowding.VERSION,
                                              "parser_version": CARD_PARSER_VERSION,
                                              "thresholds": {key: thresholds[key] for key in ("version", "high", "low")}})


    def node(self, expression):
        import shutil
        import subprocess
        script = ("import('" + (ROOT / "portal/opportunity-score.mjs").as_uri() + "').then(m => console.log(JSON.stringify("
                  + expression + ")))")
        completed = subprocess.run([shutil.which("node"), "--input-type=module", "-e", script],
                                   capture_output=True, text=True, timeout=60, check=True)
        return json.loads(completed.stdout)

    def test_recent_start_ties_are_broken_identically(self):
        from product_radar.meta_facts import depth_facts
        def ad(n, page, started, observed):
            return {"id": f"obs{n}", "advertiser": page, "page_id": page, "page_address": None, "active": True,
                    "started_at": started, "observed_at": observed}
        tie = [ad(1, "11", "2026-09-01T00:00:00+00:00", "2026-10-05T00:00:00+00:00"),   # 34 days: not recent
               ad(2, "11", "2026-09-01T00:00:00+00:00", "2026-09-20T00:00:00+00:00")]   # same start, 19 days: recent
        cases = [tie, tie[::-1],
                 [ad(3, "12", "2026-09-10T00:00:00+00:00", "2026-10-15T00:00:00+00:00"),
                  ad(1, "12", "2026-09-10T00:00:00+00:00", "2026-10-15T00:00:00+00:00"),
                  ad(2, "13", "2026-09-30T00:00:00+00:00", "2026-10-01T00:00:00+00:00")]]
        python = [depth_facts(rows)["recent_start_advertisers"] for rows in cases]
        self.assertEqual(python, [1, 1, 1])
        rows = [[{"id": a["id"], "page_id": a["page_id"], "page_address": None, "started": a["started_at"],
                  "observed": a["observed_at"]} for a in case] for case in cases]
        self.assertEqual(self.node(json.dumps(rows) + ".map(rows => m.metaRecentStarts(rows, Date.parse))"), python)

    def test_crowding_threshold_ladder_matches_python(self):
        from product_radar.crowding import load_thresholds, threshold_ladder
        base = {"bestseller_hits": 0, "listings_rated_high": 0, "listings_with_brand": 0, "brands_or_sellers": 0,
                "bought_past_month_hits": 0, "card_facts_missing": 0, "listings_rated_200_plus": 0, "ratings_not_shown": 0,
                "relevant_listings": {"amazon": 1, "flipkart": 1}}
        cases = [dict(base), dict(base, bestseller_hits=5, relevant_listings={"amazon": 12, "flipkart": 0}),
                 dict(base, listings_rated_high=3), dict(base, listings_with_brand=8, brands_or_sellers=5),
                 dict(base, listings_with_brand=8, brands_or_sellers=4), dict(base, bought_past_month_hits=2),
                 dict(base, card_facts_missing=1), dict(base, relevant_listings={"amazon": 2, "flipkart": 1}),
                 dict(base, ratings_not_shown=1), dict(base, listings_rated_200_plus=1)]
        python = [list(threshold_ladder(counts, load_thresholds())) for counts in cases]
        self.assertEqual(python[1], ["high", "bestseller_list"])
        self.assertEqual(self.node(json.dumps(cases) + ".map(c => m.thresholdLadder(c))"), python)
        self.assertIsNone(self.node("m.thresholdLadder({relevant_listings: {amazon: 1}})"))


class SubPointTests(unittest.TestCase):
    def test_facebook_ads_boundaries(self):
        for value, expected in ((market(), 0), (market(ads=2), 2), (market(ads=1, positive=False, advertisers=1), 2),
                                (market(ads=3, positive=True, advertisers=4), 6),
                                (market(ads=5, positive=True, advertisers=5), 8),
                                (market(ads=6, positive=True, advertisers=None), 6)):
            with self.subTest(value=value["sources"]["meta"]):
                record = evaluate(market_value=value)
                self.assertEqual(points(record, "facebook_ads"), expected)
                evidence = record["opportunity_basis"]["parts"]["want"]["items"]["facebook_ads"]["evidence_ids"]
                self.assertEqual(evidence, value["sources"]["meta"]["used_ids"] if expected else [])

    def test_google_searches_boundaries(self):
        for nonzero, expected in ((None, 0), (0, 0), (1, 2), (3, 2), (4, 5), (52, 5)):
            with self.subTest(nonzero=nonzero):
                self.assertEqual(points(evaluate(market_value=market(trends={"nonzero": nonzero})), "google_searches"), expected)
        unusable = market(); unusable["sources"]["google-trends"].update(nonzero_points=30)  # not available: never counted
        self.assertEqual(points(evaluate(market_value=unusable), "google_searches"), 0)

    def test_platform_list_boundaries(self):
        for value, expected in ((None, 0), (presence(listed=["deodap"]), 1), (presence(listed=["deodap", "baapstore"]), 2),
                                (presence(on_list=["deodap"]), 4), (presence(on_list=["deodap", "dropdash"]), 5),
                                (presence(on_list=["deodap"], listed=["baapstore", "dropdash"]), 4)):
            with self.subTest(value=value):
                record = evaluate(presence_value=value)
                self.assertEqual(points(record, "platform_lists"), expected)
                item = record["opportunity_basis"]["parts"]["want"]["items"]["platform_lists"]
                self.assertEqual(bool(item["evidence_ids"]), bool(expected))

    def test_quick_commerce_stock_boundaries(self):
        for stock, expected in (((), 0), ((("blinkit", "Mumbai", "b1"),), 1),
                                ((("blinkit", "Mumbai", "b1"), ("zepto", "Mumbai", "z1")), 1),
                                ((("blinkit", "Mumbai", "b1"), ("instamart", "Bengaluru", "i1")), 2),
                                ((("blinkit", "Mumbai", "b1"), ("zepto", "Delhi NCR", "z1"), ("instamart", "Bengaluru", "i1")), 2)):
            with self.subTest(stock=stock):
                record = evaluate(market_value=market(stock=stock))
                self.assertEqual(points(record, "quick_commerce"), expected)
        record = evaluate(market_value=market(stock=(("blinkit", "Mumbai", "b1"), ("zepto", "Mumbai", "z1"))))
        self.assertEqual(record["opportunity_basis"]["parts"]["want"]["items"]["quick_commerce"]["facts"],
                         {"stock_cities": {"Mumbai": ["b1", "z1"]}})

    def test_rising_search_boundaries_use_the_lower_ratio_bound(self):
        for trends, expected in (({"positive": False, "nonzero": 10}, 0),
                                 ({"positive": True, "nonzero": 10, "previous": [9.75, 10.0], "recent": [15.0, 15.5]}, 12),
                                 ({"positive": True, "nonzero": 10, "previous": [9.75, 10.0], "recent": [14.75, 16.0]}, 8),
                                 ({"positive": True, "nonzero": 10, "previous": [0, 0], "recent": [2.0, 2.0]}, 8),
                                 ({"positive": True, "nonzero": 10, "previous": [0, 0], "recent": [2.0, 2.0], "breakout": 1}, 12),
                                 ({"positive": True, "nonzero": 10}, 8)):
            with self.subTest(trends=trends):
                record = evaluate(market_value=market(trends=trends))
                self.assertEqual(points(record, "rising_searches"), expected)
        facts = evaluate(market_value=market(trends={"positive": True, "nonzero": 10, "previous": [9.75, 10.0],
                                                     "recent": [15.0, 15.5]}))["opportunity_basis"]["parts"]["early"]["items"]["rising_searches"]["facts"]
        self.assertEqual((facts["previous_mean_upper"], facts["recent_mean_lower"]), ("10", "15"))
        for bad in ([9.75, 10.1], [9.75, "10"], [9.75, True], [-0.25, 10.0], [9.75], "10", [None, 10.0]):
            with self.subTest(bad=bad), self.assertRaisesRegex(SafetyError, "OPPORTUNITY_SOURCE_INVALID"):
                evaluate(market_value=market(trends={"positive": True, "nonzero": 10, "previous": bad, "recent": [1.0, 2.0]}))

    def test_new_advertiser_and_new_arrival_boundaries(self):
        for recent, expected in ((None, 0), (0, 0), (1, 3), (2, 6), (3, 9), (7, 9)):
            with self.subTest(recent=recent):
                self.assertEqual(points(evaluate(market_value=market(ads=3, recent=recent)), "new_advertisers"), expected)
        for value, expected in ((None, 0), (presence(listed=["deodap"]), 0), (presence(arrivals=["deodap"]), 6),
                                (presence(arrivals=["deodap", "baapstore"]), 9),
                                (presence(arrivals=["deodap", "baapstore", "dropdash"]), 9)):
            with self.subTest(value=value):
                self.assertEqual(points(evaluate(presence_value=value), "new_arrivals"), expected)

    def test_room_and_product_boundaries(self):
        for level, expected in (("low", 25), ("medium", 12), ("high", 0), ("not_checked", 0)):
            with self.subTest(level=level):
                self.assertEqual(points(evaluate(crowding_value=crowding(level)), "crowding"), expected)
        for meets, expected in ((0, 0), (4, 0), (5, 10), (6, 13), (9, 22), (10, 25)):
            with self.subTest(meets=meets):
                self.assertEqual(points(evaluate(meets), "checklist"), expected)

    def test_amazon_and_flipkart_give_no_want_points(self):
        value = market()
        value["sources"]["amazon"] = {"available": True, "positive": True, "used_ids": ["amz1"]}
        value["sources"]["flipkart"] = {"available": True, "positive": False, "used_ids": ["flk1"]}
        self.assertEqual(evaluate(market_value=value)["opportunity_basis"]["parts"]["want"]["points"], 0)

    def test_full_record_arithmetic(self):
        record = evaluate(7, market(ads=5, positive=True, advertisers=6, recent=2,
                                    trends={"positive": True, "nonzero": 30, "previous": [10.0, 10.25], "recent": [16.0, 16.0]},
                                    stock=(("blinkit", "Mumbai", "b1"),)),
                          presence(on_list=["deodap"], arrivals=["baapstore"]), crowding("medium"))
        parts = record["opportunity_basis"]["parts"]
        self.assertEqual({part: parts[part]["points"] for part in parts}, {"want": 18, "early": 24, "room": 12, "product": 16})
        self.assertEqual((record["opportunity_score"], record["opportunity_label"], record["reason"]), (70, "sweet_spot", "opportunity_scored"))
        self.assertEqual({part: parts[part]["max"] for part in parts}, {"want": 20, "early": 30, "room": 25, "product": 25})
        self.assertEqual(score.basis_inputs(record["opportunity_basis"]), (presence(on_list=["deodap"], arrivals=["baapstore"]), crowding("medium")))


class LabelAndEligibilityTests(unittest.TestCase):
    def test_labels(self):
        early = presence(arrivals=["deodap", "baapstore"])  # 9 early points
        cases = (
            (market(ads=1), None, "low", "too_quiet", "demand_too_quiet"),                  # 2 want
            (market(ads=1), None, "not_checked", "too_quiet", "crowding_pending"),
            (WANT5, None, "not_checked", None, "crowding_pending"),
            (WANT5, None, "high", "proven_but_crowded", "opportunity_scored"),
            (market(ads=3, positive=True, advertisers=3, recent=2), early, "low", "early_bet", "opportunity_scored"),  # early 15, want 6
            (market(ads=3, positive=True, advertisers=3, recent=1), early, "medium", "sweet_spot", "opportunity_scored"),  # early 12
            (market(ads=5, positive=True, advertisers=5, recent=2, trends={"nonzero": 4}), early, "low", "sweet_spot",
             "opportunity_scored"),  # early 15, want 13
            (market(ads=1, trends={"nonzero": 2}), None, "low", "too_quiet", "demand_too_quiet"),  # want 4
            (market(ads=1, trends={"nonzero": 2}, stock=(("zepto", "Mumbai", "z1"),)), None, "low", "sweet_spot", "opportunity_scored"),  # want 5
        )
        for value, presence_value, level, label, reason in cases:
            with self.subTest(label=label, reason=reason, level=level):
                record = evaluate(6, value, presence_value, crowding(level))
                self.assertEqual((record["opportunity_label"], record["reason"]), (label, reason))
                self.assertEqual(record["eligible"], reason == "opportunity_scored")
                self.assertEqual(record["opportunity_basis"]["eligible"], record["eligible"])

    def test_fit_comes_first_then_crowding_then_want(self):
        self.assertEqual(evaluate(4, WANT5, None, crowding("not_checked"))["reason"], "fit_information_pending")
        self.assertEqual(evaluate(5, market(), None, crowding("not_checked"))["reason"], "crowding_pending")
        self.assertEqual(evaluate(5, market(), None, crowding("low"))["reason"], "demand_too_quiet")
        self.assertTrue(evaluate(5, WANT5, None, crowding("high"))["eligible"])

    def test_invalid_inputs_are_refused(self):
        for value in (True, 6.0, -1, 11):
            with self.subTest(meets=value), self.assertRaisesRegex(SafetyError, "OPPORTUNITY_FIT_INVALID"):
                score.evaluate_opportunity(dict(fit(), kpi_matches=value), WANT5, presence=None, crowding=crowding())
        with self.assertRaisesRegex(SafetyError, "OPPORTUNITY_FIT_INVALID"):
            score.evaluate_opportunity(dict(fit(), fit_policy_id="fit-13-v1"), WANT5, presence=None, crowding=crowding())
        for value in (None, {}, {"sources": []}):
            with self.subTest(market=value), self.assertRaisesRegex(SafetyError, "OPPORTUNITY_MARKET_INVALID"):
                score.evaluate_opportunity(fit(), value, presence=None, crowding=crowding())
        for edit, code in ((lambda m: m["sources"]["meta"].update(available=1), "OPPORTUNITY_SOURCE_INVALID"),
                           (lambda m: m["sources"]["meta"].update(positive="yes"), "OPPORTUNITY_SOURCE_INVALID"),
                           (lambda m: m["sources"]["meta"].update(used_ids=[]), "OPPORTUNITY_SOURCE_REFERENCE_INVALID"),
                           (lambda m: m["sources"]["meta"].update(used_ids=["ad0", "ad0", "ad1"]), "OPPORTUNITY_SOURCE_REFERENCE_INVALID"),
                           (lambda m: m["sources"]["meta"]["depth"].update(advertisers=-1), "OPPORTUNITY_SOURCE_INVALID"),
                           (lambda m: m["sources"]["meta"]["depth"].update(recent_start_advertisers=True), "OPPORTUNITY_SOURCE_INVALID"),
                           (lambda m: m["sources"].pop("zepto"), "OPPORTUNITY_SOURCE_INVALID"),
                           (lambda m: m["sources"]["blinkit"].update(available=True, used_ids=["b1"], stock_locations={"Mumbai": ["b2"]}),
                            "OPPORTUNITY_SOURCE_REFERENCE_INVALID"),
                           (lambda m: m["sources"]["blinkit"].update(available=True, used_ids=["b1"], stock_locations={"Mumbai": []}),
                            "OPPORTUNITY_SOURCE_INVALID")):
            value = deepcopy(WANT5); edit(value)
            with self.subTest(code=code), self.assertRaisesRegex(SafetyError, code):
                score.evaluate_opportunity(fit(), value, presence=None, crowding=crowding())
        for value in ({"level": "unknown"}, dict(crowding(), rule=None), dict(crowding("not_checked"), rule="x"),
                      dict(crowding(), counts=[]), dict(crowding(), thresholds_version=None)):
            with self.subTest(crowding=value), self.assertRaisesRegex(SafetyError, "OPPORTUNITY_CROWDING_INVALID"):
                score.evaluate_opportunity(fit(), WANT5, presence=None, crowding=value)
        with self.assertRaisesRegex(SafetyError, "OPPORTUNITY_SOURCE_REFERENCE_INVALID"):
            score.evaluate_opportunity(fit(), WANT5, presence=None, crowding=dict(crowding(), evidence_ids=["b", "a"]))
        bad = presence(listed=["deodap"]); bad["on_list"]["dropdash"] = ["item-dropdash"]
        with self.assertRaisesRegex(SafetyError, "OPPORTUNITY_BASIS_INVALID"):
            score.evaluate_opportunity(fit(), WANT5, presence=bad, crowding=crowding())

    def test_presence_input_uses_listed_items_and_a_week_of_freshness(self):
        record = {"at": PRESENCE_AT, "platforms_carrying": ["deodap"], "platforms": {"deodap": {"items": [
            {"observation_id": "o1", "listed": True, "on_list": True, "new_arrival": False},
            {"observation_id": "o2", "listed": True, "on_list": False, "new_arrival": True},
            {"observation_id": "o3", "listed": False, "on_list": True, "new_arrival": True}]}}}
        self.assertEqual(score.presence_input(record, as_of=NOW), {"at": PRESENCE_AT, "listed": {"deodap": ["o1", "o2"]},
                                                                   "on_list": {"deodap": ["o1"]}, "new_arrival": {"deodap": ["o2"]}})
        self.assertIsNone(score.presence_input(None, as_of=NOW))
        self.assertIsNone(score.presence_input(record, as_of="2026-10-21T10:00:01+00:00"))  # older than a week
        self.assertIsNotNone(score.presence_input(record, as_of="2026-10-21T10:00:00+00:00"))
        self.assertIsNone(score.presence_input(record, as_of="2026-10-13T10:00:00+00:00"))  # after the score time

    def test_score_inputs_read_saved_presence_and_crowding(self):
        saved = {"at": PRESENCE_AT, "platforms_carrying": [], "platforms": {}}
        with patch("product_radar.platform_roles.current_platform_presence", return_value=saved) as presence_read, \
                patch("product_radar.crowding.crowding_for", return_value=dict(crowding("high"), kind="x")) as crowding_read:
            self.assertEqual(score.score_inputs({"state": True}, "concept", as_of=NOW),
                             ({"at": PRESENCE_AT, "listed": {}, "on_list": {}, "new_arrival": {}}, crowding("high")))
            record = score.score_type({"state": True}, "concept", fit(), WANT5, as_of=NOW)
        self.assertEqual(record["opportunity_label"], "proven_but_crowded")
        self.assertEqual(presence_read.call_count, 2); self.assertEqual(crowding_read.call_count, 2)


class ValidatorTests(unittest.TestCase):
    def setUp(self):
        self.record = evaluate(6, market(ads=3, positive=True, advertisers=3, recent=1, trends={"positive": True, "nonzero": 8},
                                         stock=(("blinkit", "Mumbai", "b1"),)),
                               presence(on_list=["deodap"], arrivals=["deodap"]), crowding("low"))
        self.product = product(self.record)

    def test_valid_record_returns_its_score(self):
        self.assertEqual(score.validate_opportunity(self.product), self.record["opportunity_score"])
        self.assertEqual(score.validate_opportunity(self.product, matches=6), self.record["opportunity_score"])
        self.assertIsNone(score.validate_opportunity({"kpi_states": ["Meets"] * 7}))

    def test_presence_time_must_be_written_in_utc(self):
        for at in ("2026-10-14T15:30:00+05:30", "2026-10-14T10:00:00", "2026-10-14 10:00:00+00:00", "２０２６-10-14T10:00:00Z",
                   "2026-02-30T10:00:00Z", "2026-10-14T24:00:00+00:00", "2026-10-14T23:59:60Z", "2026-10-14T10:00:00.1234567Z"):
            with self.subTest(at=at), self.assertRaisesRegex(SafetyError, "OPPORTUNITY_BASIS_INVALID"):
                evaluate(6, None, presence(on_list=["deodap"], at=at))
        for at in ("2026-10-14T10:00:00Z", "2026-10-14T10:00:00.123456+00:00", "2026-10-14T10:00:00.1Z", "2024-02-29T10:00:00+00:00"):
            self.assertEqual(points(evaluate(6, None, presence(on_list=["deodap"], at=at)), "platform_lists"), 4)

    def test_tampering_is_refused(self):
        def items(row, part):
            return row["opportunity_basis"]["parts"][part]["items"]
        cases = (
            (lambda r: r.update(opportunity_score=r["opportunity_score"] + 1), "OPPORTUNITY_SCORE_INVALID"),
            (lambda r: r.update(opportunity_score=float(r["opportunity_score"])), "OPPORTUNITY_SCORE_INVALID"),
            (lambda r: r.update(opportunity_label="early_bet"), "OPPORTUNITY_LABEL_INVALID"),
            (lambda r: r["opportunity_basis"]["parts"]["want"].update(points=20), "OPPORTUNITY_SCORE_INVALID"),
            (lambda r: r["opportunity_basis"]["parts"]["want"].update(max=19), "OPPORTUNITY_SCORE_INVALID"),
            (lambda r: items(r, "want")["facebook_ads"].update(points=8), "OPPORTUNITY_SCORE_INVALID"),
            (lambda r: items(r, "want")["quick_commerce"].update(points=True), "OPPORTUNITY_SCORE_INVALID"),
            (lambda r: r["opportunity_basis"]["parts"]["room"].update(points=25.0), "OPPORTUNITY_SCORE_INVALID"),
            (lambda r: items(r, "want")["facebook_ads"]["facts"].update(advertisers=9), "OPPORTUNITY_SCORE_INVALID"),
            (lambda r: items(r, "want")["facebook_ads"]["facts"].update(positive=1), "OPPORTUNITY_BASIS_INVALID"),
            (lambda r: items(r, "want")["facebook_ads"]["facts"].update(relevant_ads=None), "OPPORTUNITY_BASIS_INVALID"),
            (lambda r: items(r, "want")["facebook_ads"].update(evidence_ids=["ad0", "ad1"]), "OPPORTUNITY_SOURCE_REFERENCE_INVALID"),
            (lambda r: items(r, "want")["facebook_ads"].update(evidence_ids=["ad1", "ad0", "ad2"]), "OPPORTUNITY_SOURCE_REFERENCE_INVALID"),
            (lambda r: items(r, "early")["new_advertisers"].update(evidence_ids=["ad9"]), "OPPORTUNITY_SOURCE_REFERENCE_INVALID"),
            (lambda r: items(r, "early")["rising_searches"].update(evidence_ids=["chart9"]), "OPPORTUNITY_SOURCE_REFERENCE_INVALID"),
            (lambda r: items(r, "early")["rising_searches"]["facts"].update(previous_mean_upper="10.1"), "OPPORTUNITY_BASIS_INVALID"),
            (lambda r: items(r, "early")["rising_searches"]["facts"].update(previous_mean_upper=10), "OPPORTUNITY_BASIS_INVALID"),
            (lambda r: items(r, "want")["google_searches"]["facts"].update(nonzero_points=2), "OPPORTUNITY_SCORE_INVALID"),
            (lambda r: items(r, "want")["platform_lists"]["facts"].update(presence_at=NOW), "OPPORTUNITY_BASIS_INVALID"),
            # The same instant written with an India offset, consistently in both places: still refused (UTC only).
            (lambda r: [items(r, part)[name]["facts"].update(presence_at="2026-10-14T15:30:00+05:30")
                        for part, name in (("want", "platform_lists"), ("early", "new_arrivals"))], "OPPORTUNITY_BASIS_INVALID"),
            (lambda r: [items(r, part)[name]["facts"].update(presence_at="2026-10-14T10:00:00")
                        for part, name in (("want", "platform_lists"), ("early", "new_arrivals"))], "OPPORTUNITY_BASIS_INVALID"),
            (lambda r: items(r, "want")["platform_lists"]["facts"]["on_list"].update(dropdash=["x"]), "OPPORTUNITY_BASIS_INVALID"),
            (lambda r: items(r, "want")["quick_commerce"]["facts"]["stock_cities"].update(Delhi=["d1"]), "OPPORTUNITY_SCORE_INVALID"),
            (lambda r: items(r, "want")["quick_commerce"]["facts"].update(stock_cities={"Mumbai": []}), "OPPORTUNITY_BASIS_INVALID"),
            (lambda r: items(r, "room")["crowding"]["facts"].update(level="medium"), "OPPORTUNITY_SCORE_INVALID"),
            (lambda r: items(r, "room")["crowding"]["facts"].update(level="none"), "OPPORTUNITY_CROWDING_INVALID"),
            (lambda r: items(r, "room")["crowding"].update(evidence_ids=["zz", "aa"]), "OPPORTUNITY_SOURCE_REFERENCE_INVALID"),
            (lambda r: items(r, "product")["checklist"]["facts"].update(meets=7), "OPPORTUNITY_FIT_INVALID"),
            (lambda r: items(r, "product")["checklist"]["facts"].update(extra=1), "OPPORTUNITY_BASIS_INVALID"),
            (lambda r: r["opportunity_basis"].update(eligible=False), "OPPORTUNITY_SCORE_INVALID"),
            (lambda r: r["opportunity_basis"].update(reason="demand_too_quiet"), "OPPORTUNITY_SCORE_INVALID"),
            (lambda r: r["opportunity_basis"].update(settings_version="opportunity-score-settings-v0"), "OPPORTUNITY_SETTINGS_VERSION_MISMATCH"),
            (lambda r: r["opportunity_basis"].update(extra=True), "OPPORTUNITY_BASIS_INVALID"),
            (lambda r: r["opportunity_basis"]["parts"].pop("room"), "OPPORTUNITY_BASIS_INVALID"),
            (lambda r: items(r, "want").pop("quick_commerce"), "OPPORTUNITY_BASIS_INVALID"),
            (lambda r: r.update(opportunity_policy_id="opportunity-priority-v2"), "OPPORTUNITY_POLICY_INVALID"),
            (lambda r: r.pop("opportunity_label"), "OPPORTUNITY_POLICY_INVALID"),
            (lambda r: r.update(fit_policy_id="fit-13-v1"), "OPPORTUNITY_POLICY_INVALID"),
            (lambda r: r.update(kpi_matches=True), "OPPORTUNITY_FIT_INVALID"),
            (lambda r: r.update(evidence_refs=["ad0"]), "OPPORTUNITY_SOURCE_REFERENCE_INVALID"),
            (lambda r: r.update(source_refs=[{"id": "ad0"}]), "OPPORTUNITY_SOURCE_REFERENCE_INVALID"),
            (lambda r: r.update(source_refs="ad0"), "OPPORTUNITY_SOURCE_REFERENCE_INVALID"),
        )
        for edit, code in cases:
            changed = deepcopy(self.product); edit(changed)
            with self.subTest(code=code, edit=edit), self.assertRaisesRegex(SafetyError, code):
                score.validate_opportunity(changed)
        with self.assertRaisesRegex(SafetyError, "OPPORTUNITY_RECORD_INVALID"):
            score.validate_opportunity([])
        for value in ([], {"opportunity_policy_id": score.POLICY_ID}):
            with self.assertRaisesRegex(SafetyError, "OPPORTUNITY_POLICY_INVALID"):
                score.validate_record(value)
        for edit in (lambda r: r["opportunity_basis"]["parts"].update(want=None),
                     lambda r: r["opportunity_basis"]["parts"]["want"]["items"].update(facebook_ads=[]),
                     lambda r: r["opportunity_basis"]["parts"]["want"]["items"]["facebook_ads"].update(evidence_ids=["bad id!"])):
            changed = deepcopy(self.product); edit(changed)
            with self.assertRaisesRegex(SafetyError, "OPPORTUNITY_(BASIS|SOURCE_REFERENCE)_INVALID"):
                score.validate_opportunity(changed)
        for value in ({}, {"parts": {"want": {}}}):
            with self.assertRaisesRegex(SafetyError, "OPPORTUNITY_BASIS_INVALID"):
                score.basis_inputs(value)

    def test_ineligible_records_are_not_report_rows(self):
        quiet = product(evaluate(6, market(ads=1), None, crowding("low")))
        score.validate_record(quiet)  # the arithmetic itself is consistent
        with self.assertRaisesRegex(SafetyError, "OPPORTUNITY_NOT_ELIGIBLE"):
            score.validate_opportunity(quiet)


class PlatformOnlySupportTests(unittest.TestCase):
    def test_platform_lists_alone_are_dated_by_the_presence_fact(self):
        from product_radar.market_validation import functional_score_support, validate_score_support
        record = evaluate(6, market(), presence(on_list=["deodap", "dropdash"]), crowding("low"))
        self.assertEqual((record["eligible"], points(record, "platform_lists")), (True, 5))
        records = {"observations": {}, "trends_evidence": {}}
        context = {"scoped_evidence": [], "matches": {}, "meaning_hash": "m" * 64}
        support = functional_score_support(records, context, record, as_of=NOW)
        witness = support["score_support"]
        self.assertEqual((witness["sources"], witness["presence"]),
                         ([], {"at": PRESENCE_AT, "valid_until": "2026-10-21T10:00:00+00:00"}))
        self.assertEqual((support["support_valid_until"], support["score_support_refs"]), ("2026-10-21T10:00:00+00:00", []))
        for change in (lambda w: w.update(presence=None), lambda w: w["presence"].update(valid_until=NOW),
                       lambda w: w.update(valid_until=NOW)):
            wrong = deepcopy(witness); change(wrong)
            with self.subTest(wrong=wrong), self.assertRaisesRegex(SafetyError, "FUNCTIONAL_MARKET_WITNESS_INVALID"):
                validate_score_support(wrong, opportunity=record, meaning_hash="m" * 64, as_of=NOW)
        with self.assertRaisesRegex(SafetyError, "FUNCTIONAL_MARKET_WITNESS_STALE"):
            validate_score_support(witness, opportunity=record, meaning_hash="m" * 64, as_of="2026-10-21T10:00:01+00:00")
        with self.assertRaisesRegex(SafetyError, "FUNCTIONAL_MARKET_WITNESS_STALE"):
            validate_score_support(witness, opportunity=record, meaning_hash="m" * 64, as_of="2026-10-13T10:00:00+00:00")
        quiet = evaluate(6, market(), presence(listed=["deodap"]), crowding("low"))
        self.assertEqual(functional_score_support(records, context, quiet, as_of=NOW),
                         {"score_support": None, "score_support_refs": [], "support_valid_until": None})

    def test_quick_commerce_sites_are_cited_per_app(self):
        from product_radar.market_validation import functional_score_support
        record = evaluate(6, market(ads=3, positive=True, advertisers=3, stock=(("blinkit", "Mumbai", "b1"), ("zepto", "Delhi NCR", "z1"))),
                          None, crowding("low"))
        def row(source):
            return {"source": source, "observed_at": "2026-10-14T09:00:00+00:00", "facts": {}}
        records = {"observations": {"b1": row("blinkit"), "z1": row("zepto"), **{f"ad{i}": row("meta") for i in range(3)}},
                   "trends_evidence": {}}
        context = {"scoped_evidence": [{"id": "review1", "record_hash": "a" * 64}],
                   "matches": {ref: {"scope_review_id": "review1"} for ref in records["observations"]}, "meaning_hash": "m" * 64}
        witness = functional_score_support(records, context, record, as_of=NOW)["score_support"]
        self.assertEqual([site["source"] for site in witness["sources"]], ["blinkit", "meta", "zepto"])
        self.assertEqual(witness["valid_until"], "2026-10-21T09:00:00+00:00")
        unmatched = deepcopy(context); unmatched["matches"].pop("b1")
        with self.assertRaisesRegex(SafetyError, "FUNCTIONAL_MARKET_WITNESS_REFERENCE_INVALID"):
            functional_score_support(records, unmatched, record, as_of=NOW)
        records["observations"]["z1"]["source"] = "amazon"
        with self.assertRaisesRegex(SafetyError, "FUNCTIONAL_MARKET_WITNESS_REFERENCE_INVALID"):
            functional_score_support(records, context, record, as_of=NOW)

    def test_status_helpers_need_the_score_for_ten_point_types(self):
        from product_radar.research_flow import market_follow_up, research_status
        fit10 = {"fit_policy_id": FIT_POLICY_ID, "eligible": True, "kpi_matches": 6, "kpi_total": 10}
        for helper in (lambda: research_status(6, market(), fit=fit10), lambda: market_follow_up(market(), fit=fit10)):
            with self.assertRaisesRegex(SafetyError, "OPPORTUNITY_SCORE_REQUIRED"):
                helper()
        quiet = evaluate(6, market(), None, crowding("low"))
        self.assertTrue(market_follow_up(market(), fit=fit10, opportunity=quiet).startswith("Too quiet so far: 0 of the 20"))
        self.assertIn("crowding check", market_follow_up(market(), fit=fit10, opportunity=evaluate(6, WANT5, None, crowding("not_checked"))))
        self.assertIn("supports inclusion", market_follow_up(market(), fit=fit10, opportunity=evaluate(6, WANT5, None, crowding("low"))))


class OrderTests(unittest.TestCase):
    def test_low_and_medium_first_then_score_early_product_and_id(self):
        def row(cid, meets, value, presence_value, level):
            return product(evaluate(meets, value, presence_value, crowding(level)), meets, concept_id=cid)
        rows = [row("a-high-best", 10, market(ads=5, positive=True, advertisers=5), None, "high"),
                row("b-medium", 6, WANT5, None, "medium"),
                row("c-low", 6, WANT5, None, "low"),
                row("d-low-early", 6, WANT5, presence(arrivals=["deodap"]), "low"),
                row("e-low-product", 10, WANT5, None, "low"),
                row("f-low-early-tie", 6, market(ads=3, positive=True, advertisers=3, recent=2), None, "low"),
                row("g-low", 6, WANT5, None, "low"),
                # Three types at 47: more early points first, then more product points.
                row("h-tie-early", 6, market(ads=3, positive=True, advertisers=3, recent=1), None, "low"),
                row("i-tie", 7, WANT5, None, "low"),
                row("k-tie-product", 10, market(ads=5, positive=True, advertisers=5, stock=(("zepto", "Mumbai", "z1"),)),
                    presence(listed=["deodap"]), "medium")]
        self.assertEqual([r["opportunity_score"] for r in rows], [33, 31, 44, 51, 56, 50, 44, 47, 47, 47])
        ordered = [r["concept_id"] for r in sorted(rows, key=lambda r: (*score.opportunity_sort_key(r), r["concept_id"]))]
        self.assertEqual(ordered, ["e-low-product", "d-low-early", "f-low-early-tie", "h-tie-early", "k-tie-product", "i-tie",
                                   "c-low", "g-low", "b-medium", "a-high-best"])
        legacy = {"concept_id": "z", "kpi_states": ["Meets"] * 9, "fit_tier": "good"}
        self.assertGreater(score.opportunity_sort_key(legacy), score.opportunity_sort_key(rows[0]))
        self.assertEqual(score.work_rank({}), (1, False, 0))
        self.assertEqual(score.work_rank(rows[0])[:2], (0, True))

    def test_issue_high_crowding_rule(self):
        low = [product(evaluate(6, WANT5, None, crowding("low")), concept_id=f"low{n}") for n in range(10)]
        high = product(evaluate(6, WANT5, None, crowding("high")), concept_id="high")
        score.validate_issue_crowding(low[:9] + [high])
        score.validate_issue_crowding(low)
        with self.assertRaisesRegex(SafetyError, "ISSUE_HIGH_CROWDING_RULE"):
            score.validate_issue_crowding(low + [high])
        score.validate_issue_crowding([high] + low)  # Low/Medium extras may follow a High that filled the issue
        score.validate_issue_crowding(low + [{"concept_id": "older", "kpi_states": ["Meets"] * 7}])  # unscored rows are skipped

    def test_updates_beyond_the_cap_do_not_count_as_ready_low_or_medium(self):
        # 7 New Low + 3 Updated Low + 1 New High: only two updates fit while
        # filling ten, so nine Low/Medium are takeable and the High completes the issue.
        policy = policy_for_fit(FIT_POLICY_ID)
        rows = [product(evaluate(6, WANT5, None, crowding("low")), concept_id=f"new{n}", category=CATEGORIES[n % 4]) for n in range(7)]
        updated = [product(evaluate(6, WANT5, None, crowding("low")), concept_id=f"upd{n}", category=CATEGORIES[n % 4],
                           material_update_reviewed=True, update_reason="Relevant verified supplier terms changed.") for n in range(3)]
        high = product(evaluate(6, WANT5, None, crowding("high")), concept_id="high0")
        history = [{"concept_id": row["concept_id"], "status": "published", "issue_id": "older", "client_id": "default",
                    "ordinal": 1} for row in updated]
        issue = allocate_issues(rows + updated + [high], ["2026-10-20"], history, [], policy=policy)["issues"][0]
        chosen = [p["concept_id"] for p in issue["products"]]
        self.assertTrue(issue["ready_for_review"], issue["counts"])
        self.assertEqual(issue["counts"], {"products": 10, "new": 8, "updates": 2})
        self.assertIn("high0", chosen)
        score.validate_issue_crowding(issue["products"])

    def test_allocator_ties_score_then_early_then_product_then_balance_then_id(self):
        def row(cid, category, meets, value, presence_value=None):
            return product(evaluate(meets, value, presence_value, crowding("low")), meets, concept_id=cid,
                           category=CATEGORIES[category])
        early = market(ads=3, positive=True, advertisers=3, recent=1)                    # want 6, early 3
        wide = market(ads=3, positive=True, advertisers=3, trends={"nonzero": 2},
                      stock=(("zepto", "Mumbai", "z1"),))                                    # want 9, early 0
        rows = [row("a", 0, 10, WANT5), row("b", 0, 10, WANT5),                             # 56
                row("c", 0, 6, early), row("d", 1, 7, WANT5), row("e", 1, 6, wide),        # 47: early 3; product 16; product 13
                row("f", 0, 6, WANT5), row("g", 1, 6, WANT5), row("h", 2, 6, WANT5), row("i", 2, 6, WANT5),  # 44
                row("x0", 3, 5, WANT5), row("x1", 3, 5, WANT5), row("x2", 3, 5, WANT5)]     # 41
        self.assertEqual([r["opportunity_score"] for r in rows], [56, 56, 47, 47, 47, 44, 44, 44, 44, 41, 41, 41])
        issue = allocate_issues(rows, ["2026-10-20"], [], [], policy=policy_for_fit(FIT_POLICY_ID))["issues"][0]
        # Score, early and product come before category balance: c joins a category that already has
        # two types, and d (more product points) precedes e (more want points). Among the equal 44s,
        # balance decides (a category with fewer than two types first, then fewer of it) and the type
        # id last: h and i (category 2 had none), then g (category 1 has two) before f (category 0 has three).
        self.assertEqual([p["concept_id"] for p in issue["products"]], ["a", "b", "c", "d", "e", "h", "i", "g", "f", "x0", "x1", "x2"])

    def test_allocator_uses_high_crowding_only_below_ten_low_or_medium(self):
        def rows(levels):
            return [product(evaluate(6 if level != "high" else 10, WANT5, None, crowding(level)),
                            6 if level != "high" else 10, concept_id=f"c{n:02d}-{level}", category=CATEGORIES[n % 4])
                    for n, level in enumerate(levels)]
        policy = policy_for_fit(FIT_POLICY_ID)
        enough = rows(["high"] * 3 + ["low"] * 6 + ["medium"] * 6)
        issue = allocate_issues(enough, ["2026-10-20"], [], [], policy=policy)["issues"][0]
        chosen = [p["concept_id"] for p in issue["products"]]
        self.assertTrue(issue["ready_for_review"])
        self.assertFalse([cid for cid in chosen if cid.endswith("high")], chosen)  # 12 Low/Medium ready: no High at all
        self.assertEqual(len(chosen), 12)
        self.assertTrue(all(cid.endswith("low") for cid in chosen[:6]), chosen)  # Low (25 room) outscores Medium
        short = rows(["high"] * 4 + ["low"] * 4 + ["medium"] * 4)
        issue = allocate_issues(short, ["2026-10-20"], [], [], policy=policy)["issues"][0]
        chosen = [p["concept_id"] for p in issue["products"]]
        self.assertEqual(sum(cid.endswith("high") for cid in chosen), 4)  # only 8 Low/Medium ready: High fills the issue
        self.assertTrue(all(not cid.endswith("high") for cid in chosen[:8]), chosen)
        self.assertEqual(issue["opportunity_policy_id"], score.POLICY_ID)


class WordingTests(unittest.TestCase):
    def test_score_and_crowding_lines_in_both_languages(self):
        record = product(evaluate(7, market(ads=5, positive=True, advertisers=6, recent=2,
                                            trends={"positive": True, "nonzero": 30, "previous": [10.0, 10.25], "recent": [16.0, 16.0]},
                                            stock=(("blinkit", "Mumbai", "b1"),)),
                                  presence(on_list=["deodap"], arrivals=["baapstore"]), crowding("medium")), 7)
        self.assertEqual(score.score_text(record), "Opportunity score 70/100 · Want 18/20 · Early 24/30 · Room 12/25 · Product 16/25 · Sweet spot")
        self.assertEqual(score.score_text(record, "hi-Latn"),
                         "Mauke ka score 70/100 · Maang 18/20 · Jaldi 24/30 · Jagah 12/25 · Product 16/25 · Sahi mauka")
        self.assertEqual(score.crowding_text(record), "Amazon/Flipkart crowding: Medium (3 relevant listings: Amazon 2, Flipkart 1; "
                                                      "2 brands or sellers (1 show no brand); most ratings 120; 0 on a bestseller list)")
        self.assertEqual(score.crowding_text(record, "hi-Latn"), "Amazon/Flipkart par bheed: Madhyam (3 milti-julti listing: Amazon 2, "
                                                                 "Flipkart 1; 2 brand ya seller (1 listing par brand nahi dikha); sabse zyada ratings 120; "
                                                                 "0 bestseller list par)")
        counts = record["opportunity_basis"]["parts"]["room"]["items"]["crowding"]["facts"]["counts"]
        counts.update(brands_or_sellers=0, listings_with_brand=0, brand_not_shown=3)  # the pages showed no brand at all
        self.assertIn("; brands not shown;", score.crowding_text(record))
        self.assertNotIn("0 brands", score.crowding_text(record))
        self.assertIn("; brand nahi dikhe;", score.crowding_text(record, "hi-Latn"))
        counts.update(brand_not_shown=0, listings_with_brand=2, brands_or_sellers=2)
        self.assertIn("; 2 brands or sellers;", score.crowding_text(record))
        record["opportunity_basis"]["parts"]["room"]["items"]["crowding"]["facts"]["counts"]["max_ratings"] = None
        self.assertIn("most ratings not shown", score.crowding_text(record))
        self.assertIn("sabse zyada ratings nahi dikhi", score.crowding_text(record, "hi-Latn"))
        pending = evaluate(6, WANT5, None, crowding("not_checked"))
        self.assertEqual(score.crowding_text(pending), "Amazon/Flipkart crowding: Not checked")
        self.assertEqual(score.score_text(pending), "Opportunity score 19/100 · Want 6/20 · Early 0/30 · Room 0/25 · Product 13/25")
        self.assertEqual(score.workbook_headers(), ("Score", "Want /20", "Early /30", "Room /25", "Product /25", "Label", "Crowding"))
        self.assertEqual(score.workbook_values(record), [70, 18, 24, 12, 16, "Sweet spot", "Medium"])
        self.assertEqual(score.workbook_values(pending)[-2:], ["Not scored", "Not checked"])


class TallyTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(); self.addCleanup(temporary.cleanup)
        self.memory = ResearchMemory(Path(temporary.name) / "captured", Journal(Path(temporary.name) / "finance"),
                                     evidence_type="synthetic")

    def decisions(self, quiet, sweet, waiting=0, fit_pending=0, prefix="t"):
        def rows(count, record, name):
            return [dict(score.opportunity_fields(record), concept_id=f"{prefix}-{name}{n:03d}") for n in range(count)]
        values = (rows(quiet, evaluate(6, market(ads=1), None, crowding("low")), "quiet")
                  + rows(sweet, evaluate(6, WANT5, None, crowding("low")), "sweet")
                  + rows(waiting, evaluate(6, WANT5, None, crowding("not_checked")), "waiting")
                  + rows(fit_pending, evaluate(3, WANT5, None, crowding("low")), "fit"))
        return {"decisions": values + [{"concept_id": "legacy", "decision": "included"}]}

    def test_tally_counts_scored_types_per_label(self):
        tally = score.score_tally(self.decisions(2, 3, waiting=4, fit_pending=1)["decisions"])
        self.assertEqual(tally, {"labels": {"too_quiet": 2, "proven_but_crowded": 0, "early_bet": 0, "sweet_spot": 3},
                                 "scored": 5, "waiting_for_crowding": 4})

    def test_calibration_sample_is_the_first_fifty_types_in_scoring_order_and_runs_once(self):
        root = self.memory.root
        self.assertEqual(score.score_line(root), "Opportunity score: no types scored yet.")
        first = score.record_score_summary(self.memory, self.decisions(3, 4, waiting=1), at=NOW)
        self.assertEqual(first["calibration"], "waiting_for_sample")
        self.assertIsNone(first["alert"])
        self.assertEqual(first["text"], "Opportunity score: 7 types scored (4 sweet spot, 0 early bet, 0 proven but crowded, "
                                        "3 too quiet), 1 waiting for the crowding check; calibration waiting for sample (7 of 50).")
        self.assertEqual(score.score_line(root), first["text"])
        saved = score.calibration_record(self.memory.snapshot())
        self.assertEqual((saved["state"], len(saved["sample"])), ("collecting", 7))
        self.assertEqual(saved["sample"][0], {"concept_id": "t-quiet000", "label": "too_quiet", "first_scored_at": NOW})
        path = root / "opportunity_score" / "summary.json"
        stamp = path.stat().st_mtime_ns
        score.record_score_summary(self.memory, self.decisions(3, 4, waiting=1), at="2026-10-15T11:00:00+00:00")
        self.assertEqual(path.stat().st_mtime_ns, stamp)  # unchanged counts: not rewritten
        self.assertEqual(score.calibration_record(self.memory.snapshot()), saved)  # the same types are not sampled twice
        # The next pool scores 43 more Too quiet types and many Sweet spot ones: only the
        # first 50 types in scoring order are the sample (7 earlier + the next 43 by id).
        later = "2026-10-16T10:00:00+00:00"
        pool = self.decisions(3, 4)["decisions"][:-1] + self.decisions(45, 60, prefix="u")["decisions"]
        skewed = score.record_score_summary(self.memory, {"decisions": pool}, at=later)
        record = score.calibration_record(self.memory.snapshot())
        self.assertEqual((skewed["calibration"], record["scored_types"], record["too_quiet_types"]), ("calibration_needed", 50, 46))
        self.assertEqual([row["first_scored_at"] for row in record["sample"]], [NOW] * 7 + [later] * 43)
        self.assertEqual(record["sample"][7]["concept_id"], "u-quiet000")
        self.assertEqual((record["too_quiet_share"], record["settings_version"]), ("0.9200", "opportunity-score-settings-v1"))
        self.assertTrue(skewed["alert"].startswith("SCORE CALIBRATION NEEDED: 0.9200 of the first 50 scored types are Too quiet"))
        self.assertIn("OPPORTUNITY_SETTINGS_VERSION_MISMATCH", skewed["alert"])
        self.assertIn("portal redeployed", skewed["alert"])
        self.assertEqual(score.score_line(root), skewed["text"] + "\n" + skewed["alert"])
        # Decided once in total: a later pool, or a new settings version, never re-judges it.
        newer = dict(score.load_settings(), version="opportunity-score-settings-v2")
        self.assertEqual(score.record_score_summary(self.memory, self.decisions(1, 60, prefix="v"), at=later,
                                                    settings=newer)["calibration"], "calibration_needed")
        self.assertEqual(score.calibration_record(self.memory.snapshot()), record)
        meter = score.score_summary(self.memory.snapshot(), self.decisions(1, 60))
        self.assertEqual((meter["scored"], meter["calibration"]), (61, "calibration_needed"))
        path.write_text("{}")
        with self.assertRaisesRegex(SafetyError, "OPPORTUNITY_SUMMARY_INVALID"):
            score.score_line(root)

    def test_in_range_sample_and_damaged_summary_file(self):
        result = score.record_score_summary(self.memory, self.decisions(20, 30), at=NOW)
        self.assertEqual(result["calibration"], "in_range")
        self.assertIsNone(result["alert"])
        state = self.memory.snapshot()
        self.assertEqual(state["batch_runs"][score.CALIBRATION_ID]["state"], "in_range")
        low = ResearchMemory(self.memory.root.parent / "other", Journal(self.memory.root.parent / "finance2"), evidence_type="synthetic")
        self.assertEqual(score.record_score_summary(low, self.decisions(2, 48), at=NOW)["calibration"], "calibration_needed")
        (self.memory.root / "opportunity_score" / "summary.json").write_text("not json")
        rewritten = score.record_score_summary(self.memory, self.decisions(20, 30), at=NOW)  # a damaged file is replaced
        self.assertEqual(score.score_line(self.memory.root), rewritten["text"])
        nothing = ResearchMemory(self.memory.root.parent / "empty", Journal(self.memory.root.parent / "finance3"), evidence_type="synthetic")
        self.assertEqual(score.record_score_summary(nothing, {"decisions": []}, at=NOW)["calibration"], "waiting_for_sample")
        self.assertIsNone(score.calibration_record(nothing.snapshot()))  # nothing scored: nothing saved


if __name__ == "__main__":
    unittest.main()
