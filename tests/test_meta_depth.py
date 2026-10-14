"""Deeper Meta reads with the free Apify allowance (W11); fictional provider only.

Every Apify reply comes from the offline fake provider of the live-trial tests
(extended with the account usage read): no account, key, network or paid call.
"""

from copy import deepcopy
from datetime import datetime, timedelta, timezone
from decimal import Decimal
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch
from urllib.error import HTTPError
from urllib.parse import urlsplit

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "tests"))

from product_radar import meta_depth, meta_depth_money, spend_watch
from product_radar.accounts import bind_account, bind_registered_account
from product_radar.benchmark import MetaTrial
from product_radar.discovery import compute_market_signals
from product_radar.functional_types import accept_functional_definition, effective_functional_definition
from product_radar.market_validation import functional_market_context
from product_radar.meta_browser import meta_direct_source_handlers, prepare_meta_query
from product_radar.meta_depth import (ROUTE, attempt_id_for, create_meta_depth_handlers, depth_request,
                                      prepared_for, save_preparation)
from product_radar.meta_depth_money import MetaDepthWait, money_check
from product_radar.meta_facts import (advertiser_count, advertiser_identity, depth_facts, meta_state,
                                      relevant_ads)
from product_radar.models import SafetyError, digest
from product_radar.report_source_facts import coverage_rows
from product_radar.research_memory import ResearchMemory
from product_radar.source_coverage import (PLANNED_ROUTES, ROUTES, execute_source_run, installed_source_handlers,
                                           prepare_source_tasks, product_source_request_builder)
from product_radar.source_interpretation import review_source_results
from product_radar.storage import Journal, atomic_json, load_json
from test_functional_queries import DEFINITION
from test_functional_source_interpretation import Runner
from test_meta_live_trial import TOKEN, Provider, Response

FREE_AT = "2026-10-08T10:00:00+00:00"
CLOCK = datetime(2026, 10, 8, 11, 0, tzinfo=timezone.utc)
REVIEW_AT = "2026-10-08T12:00:00+00:00"
DESCRIPTION = {"label": "Sink caddy", "category": "Home and Kitchen", "function": "Store sink essentials",
               "mechanism": "manual", "audience": "adult", "features": ["draining"], "basis": "observed",
               "brand": "generic_or_small", "reason": "Synthetic fixture."}
LEGACY = {"pool_enabled": False, "accounts": [{"id": "legacy", "enabled": True, "priority": 0,
    "credential": "config/local/providers.env", "binding": "config/local/apify_binding.json",
    "profile": "config/local/apify_profile.json", "handover": "config/local/apify_handover.json"}]}


def observation(ad_id, observed_at, *, advertiser=None, url=None, active=None, started=None, route=None):
    facts = {"ad_id": ad_id, "advertiser_id": advertiser, "active": active, "started_at": started}
    if url:
        facts["advertiser_url"] = url
    if route:
        facts["source_route"] = route
    return {"source": "meta", "source_item": ad_id + ":parent", "semantic_id": "sem-" + ad_id + (route or ""),
            "observed_at": observed_at, "facts": facts}


class AccountProvider(Provider):
    """The live-trial fake plus Apify's free account usage read."""

    def __init__(self):
        super().__init__()
        self.limits_denied = False
        self.cycle("2026-10-03T00:00:00.000Z", "2026-11-01T23:59:59.999Z", used=0.5)

    def cycle(self, start, end, *, used, maximum=5):
        self.limits = {"data": {"monthlyUsageCycle": {"startAt": start, "endAt": end},
                                "limits": {"maxMonthlyUsageUsd": maximum}, "current": {"monthlyUsageUsd": used}}}

    def run(self, run_id="runOne"):
        payload = super().run(run_id)
        if getattr(self, "finished_at", None):
            payload["data"]["finishedAt"] = self.finished_at
        return payload

    def open(self, request, timeout):
        if urlsplit(request.full_url).path == "/v2/users/me/limits":
            self.calls.append((request.method, "/v2/users/me/limits", {}))
            if self.limits_denied:
                raise HTTPError(request.full_url, 403, "", {}, None)
            return Response(self.limits)
        return super().open(request, timeout)


class MetaFactsTests(unittest.TestCase):
    def test_advertiser_identity_uses_page_id_then_official_page_address(self):
        self.assertEqual(advertiser_identity({"advertiser_id": "42"}), "42")
        self.assertEqual(advertiser_identity({"advertiser_id": None, "advertiser_url": "https://facebook.com/Shop/?x=1"}),
                         "page-url:https://www.facebook.com/shop?x=1")
        for url in ("https://www.facebook.com/ads/library/", "http://www.facebook.com/shop", "https://example.com/shop",
                    "https://user:pw@www.facebook.com/shop", "https://[bad", None):
            self.assertIsNone(advertiser_identity({"advertiser_id": None, "advertiser_url": url}))

    def test_deeper_read_fills_what_the_free_read_lacks_and_counts_the_ad_once(self):
        free = observation("1", FREE_AT, active=True)
        deep = observation("1", "2026-10-08T11:00:00+00:00", advertiser="77", active=True,
                           started="2026-09-01T07:30:00+00:00", route=ROUTE)
        deep["source_item"] = ROUTE + ":1:0"
        ads, conflicts = relevant_ads([("free", free), ("deep", deep)])
        self.assertEqual(conflicts, set())
        self.assertEqual(ads["1"], {"id": "deep", "advertiser": "77", "page_id": "77", "page_address": None, "active": True,
                                    "started_at": "2026-09-01T07:30:00+00:00", "observed_at": "2026-10-08T11:00:00+00:00"})
        relevance = {"sem-1": "exact_product", "sem-1" + ROUTE: "relevant_family"}
        market = compute_market_signals({"free": free, "deep": deep}, relevance, REVIEW_AT)
        self.assertEqual(market["sources"]["meta"]["used_ids"], ["deep"])  # one ad, one evidence ID
        self.assertEqual(market["families"]["meta"]["advertisers"], 1)
        self.assertEqual(market["families"]["meta"]["missing_advertiser_ids"], 0)
        self.assertNotIn(ROUTE, market["sources"])  # never a site of its own
        self.assertTrue(market["sources"]["meta"]["depth"]["complete"])

    def test_identity_forms_dates_and_status_merge_or_conflict(self):
        early, late = "2026-10-08T10:00:00+00:00", "2026-10-08T11:00:00+00:00"
        def merged(first, second):
            ads, conflicts = relevant_ads([("a", first), ("b", second)])
            return None if "1" in conflicts else ads["1"]
        page = observation("1", early, url="https://www.facebook.com/Shop/", started="2026-09-01T00:00:00+00:00")
        both = merged(page, observation("1", late, advertiser="9", started="2026-08-31T19:30:00+00:00"))
        self.assertEqual((both["advertiser"], both["page_address"]), ("9", "page-url:https://www.facebook.com/shop"))
        self.assertEqual(both["started_at"], "2026-08-31T19:30:00+00:00")  # within one day: the earlier start
        self.assertEqual(merged(observation("1", early, advertiser="9"), page)["advertiser"], "9")
        self.assertIsNone(merged(observation("1", early, advertiser="1"), observation("1", late, advertiser="2")))
        self.assertIsNone(merged(page, observation("1", late, url="https://www.facebook.com/Other/")))
        self.assertIsNotNone(merged(observation("1", early, advertiser="9", url="https://www.facebook.com/Shop/"),
                                    observation("1", late, url="https://www.facebook.com/Other/")))
        self.assertIsNone(merged(observation("1", early, started="2026-09-01T00:00:00+00:00"),
                                 observation("1", late, started="2026-09-04T00:00:00+00:00")))
        self.assertTrue(merged(observation("1", early, active=False), observation("1", late, active=True))["active"])
        self.assertFalse(merged(observation("1", late, active=False), observation("1", early, active=True))["active"])
        self.assertIsNone(merged(observation("1", early, active=False), observation("1", early, active=True)))
        ads, conflicts = relevant_ads([("a", observation("1", early, advertiser="1")), ("b", observation("1", late, advertiser="2")),
                                       ("c", observation("1", late, advertiser="1"))])
        self.assertEqual(conflicts, {"1"})  # a conflict is never repaired by a later third copy

    def test_two_real_advertisers_never_become_four(self):
        """The reviewer's case: free ads name pages by address, deeper ads by page ID only."""
        early, late = "2026-10-08T10:00:00+00:00", "2026-10-08T11:00:00+00:00"
        rows = [("a1", observation("1", early, url="https://www.facebook.com/ShopA/")),
                ("b1", observation("2", early, url="https://www.facebook.com/ShopB/")),
                ("a2", observation("3", late, advertiser="101", route=ROUTE)),
                ("b2", observation("4", late, advertiser="202", route=ROUTE))]
        ads, _ = relevant_ads(rows)
        self.assertEqual(advertiser_count(list(ads.values())), 2)  # a lower bound, not 4
        relevance = {row["semantic_id"]: "exact_product" for _, row in rows}
        market = compute_market_signals(dict(rows), relevance, REVIEW_AT)
        self.assertEqual(market["families"]["meta"]["advertisers"], 2)
        self.assertNotEqual(market["families"]["meta"]["state"], "Positive")  # never three advertisers from two
        # The deeper read's own page address links each form: still exactly two.
        rows[2][1]["facts"]["advertiser_url"] = "https://www.facebook.com/ShopA/"
        rows[3][1]["facts"]["advertiser_url"] = "https://www.facebook.com/ShopB/"
        ads, _ = relevant_ads(rows)
        self.assertEqual(advertiser_count(list(ads.values())), 2)
        # A link learned from one merged ad serves another ad of that advertiser.
        merged_rows = [("free", observation("1", early, url="https://www.facebook.com/ShopA/")),
                       ("deep", observation("1", late, advertiser="101", route=ROUTE)),
                       ("other", observation("5", late, advertiser="101", route=ROUTE)),
                       ("c", observation("6", late, advertiser="303", route=ROUTE)),
                       ("d", observation("7", late, advertiser="404", route=ROUTE))]
        merged_rows[1][1]["source_item"] = ROUTE + ":1:parent"
        ads, _ = relevant_ads(merged_rows)
        self.assertEqual(advertiser_count(list(ads.values())), 3)
        self.assertEqual(meta_state(list(ads.values())), "Positive")

    def test_deeper_facts_and_the_recent_start_signal(self):
        seen = "2026-10-08T12:00:00+00:00"
        def ad(name, advertiser, active, started, observed=seen):
            return {"id": name, "advertiser": advertiser, "page_id": advertiser, "page_address": None, "active": active,
                    "started_at": started, "observed_at": observed}
        ads = [ad("a", "A", True, "2026-09-20T00:00:00+00:00"), ad("b", "A", True, "2026-10-01T00:00:00+00:00"),
               ad("c", "B", False, "2026-06-01T00:00:00+00:00"), ad("d", "C", True, "2026-10-05T00:00:00+00:00"),
               ad("e", "C", None, None), ad("f", None, True, "2026-10-07T00:00:00+00:00"),
               # Started 40 days before as_of, but only 20 days before it was observed.
               ad("g", "D", True, "2026-08-29T00:00:00+00:00", observed="2026-09-18T00:00:00+00:00")]
        facts = depth_facts(ads)
        self.assertEqual(facts, {"version": "meta-depth-facts-v2", "relevant_ads": 7, "advertisers": 4, "active_ads": 5,
            "oldest_started_at": "2026-06-01T00:00:00+00:00", "newest_started_at": "2026-10-07T00:00:00+00:00",
            "recent_start_advertisers": 2,  # A and D; B started long before it was seen, C has an undated ad
            "ads_without_advertiser": 1, "ads_without_start_date": 1, "complete": False})
        self.assertIsNone(depth_facts([ads[4]])["recent_start_advertisers"])
        self.assertFalse(depth_facts([])["complete"])
        self.assertTrue(depth_facts(ads[:4])["complete"])
        # An unlinked page address that might repeat an unlinked page ID is not counted on its own.
        loose = dict(ad("h", None, True, "2026-10-01T00:00:00+00:00"), page_address="page-url:x", advertiser="page-url:x")
        self.assertEqual(depth_facts([ads[3], loose])["recent_start_advertisers"], 1)


class DepthFixture(unittest.TestCase):
    """A captured store with one free Meta read, an account and the fake provider."""

    card = {"ad_id": "123456", "text": "Active\nLibrary ID: 123456\nDraining sink caddy", "links": [], "images": []}

    def setUp(self):
        temp = tempfile.TemporaryDirectory(); self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        meta_depth._MEMO.clear()
        self.journal = Journal(self.root / "data/benchmarks/state/apify")
        self.memory = ResearchMemory(self.root / "data/research/captured", self.journal)
        self.memory.put("concepts", "first", {"status": "active", "description": deepcopy(DESCRIPTION)})
        self.clock_at, self.ticks = CLOCK, 0
        self.provider = AccountProvider()
        self.provider.rows = [{"ad_archive_id": "123456", "page_id": "998877", "page_name": "Sink shop", "is_active": True,
                               "page_profile_uri": "https://www.facebook.com/SinkShop/",
                               "start_date": int(datetime(2026, 9, 1, tzinfo=timezone.utc).timestamp()),
                               "snapshot": {"title": "Draining sink caddy", "body": {"text": "Draining sink caddy for kitchens"}}}]
        self.handover = {"schema_version": 2, "account_key": "testing-owner-label", "period": "period-one",
            "period_ends_at": "2026-11-02T00:00:00+00:00", "plan": "Free", "available_usd": "5",
            "balance_checked_at": FREE_AT, "independently_funded": True, "owner_account_confirmed": True,
            "owner_credential_confirmed": True, "confirmed_at": FREE_AT, "provider_user_id": "userOne",
            "output_route_reviewed": True, "production_provision_usd": "3.07", "emergency_usd": "0"}
        credential = self.root / "config/local/providers.env"; credential.parent.mkdir(parents=True)
        credential.write_text("APIFY_TOKEN=" + TOKEN + "\n")
        saved_at = datetime(2026, 10, 1, tzinfo=timezone.utc).timestamp()  # confirmed after the key was saved
        os.utime(credential, (saved_at, saved_at))
        self.controllers = []

    def now(self):
        return self.clock_at

    def sleep(self, seconds):
        self.ticks += seconds

    def controller(self):
        if not self.controllers:
            self.controllers.append(MetaTrial(self.root, self.journal, transport_factory=self.provider.factory,
                clock=lambda: self.ticks, sleep=self.sleep, now=self.now, short_locks=True))
        return self.controllers[0]

    def handlers(self):
        return create_meta_depth_handlers(self.memory, root=self.root, controller_factory=self.controller, now=self.now)

    def bind(self, allocation="3.07", *, pool=False):
        atomic_json(self.root / "config/local/apify_handover.json", self.handover)
        if pool:
            binding = bind_registered_account(self.root, self.journal, "main", self.provider.factory, now=self.clock_at)
        with self.journal.locked():
            if not pool:
                binding = bind_account(self.root, self.journal, self.provider.factory, self.clock_at)
            self.journal.adopt_production_scope(binding.namespace, {"meta": allocation}, "Fictional earmark.", "fictional-meta-" + binding.period)
        return binding

    def pool(self):
        """The cutover's account list: the one funded account only."""
        atomic_json(self.root / "config/local/apify_accounts.json", {"schema_version": 1, "pool_enabled": True,
            "accounts": [dict(LEGACY["accounts"][0], id="main")]})

    def free_read(self, card=None, *, cards=None):
        request = {"query": "draining sink caddy", "country": "IN"}
        snapshot = {"url": prepare_meta_query(request["query"])["url"], "title": "Meta Ad Library", "text": "Ad Library",
                    "cards": cards or [card or self.card]}
        def browser(url, expression, folder, **kwargs):
            return {"snapshot": snapshot, "navigation_status": 200, "navigation_started": True,
                    "owned_process_exited": True, "completed_at": FREE_AT}
        prepare_source_tasks(self.memory, [{"route": "meta-ads", "request": request, "concept_ids": ["first"]}],
                             run_id="free-run", as_of=FREE_AT)
        execute_source_run(self.memory, "free-run", handlers={"meta-ads": meta_direct_source_handlers(self.memory, browser=browser)},
                           as_of=FREE_AT, bounded_call=lambda callback, context, timeout: callback(context))
        state = self.memory.snapshot()
        self.free_observation = sorted(state["observations"])[0]
        for ref in state["observations"]:
            self.memory.put("associations", "meta-child-" + ref[-12:], {"concept_id": "first", "status": "resolved",
                "semantic_id": state["observations"][ref]["semantic_id"]})
        accept_functional_definition(self.memory, "first", DEFINITION, basis_refs=[self.free_observation],
            reviewer="synthetic", reviewed_at=FREE_AT,
            expected_meaning_hash=effective_functional_definition(self.memory.snapshot(), "first")["meaning_hash"])

    def review(self, runner=None, as_of=REVIEW_AT):
        return review_source_results(self.memory, runner or Runner(), as_of=as_of)

    def plan(self, run_id="depth-run", as_of=REVIEW_AT):
        request = depth_request(self.memory.snapshot(), "first", {"as_of": as_of, "run_id": run_id})
        if set(request) == {"not_applicable_reason"}:
            return request
        prepare_source_tasks(self.memory, [{"route": ROUTE, "request": request, "concept_ids": ["first"]}],
                             run_id=run_id, as_of=as_of)
        return request

    def execute(self, run_id="depth-run", as_of=REVIEW_AT, handlers=None):
        return execute_source_run(self.memory, run_id, handlers={ROUTE: handlers or self.handlers()}, as_of=as_of,
                                  bounded_call=lambda callback, context, timeout: callback(context))

    def task(self, run_id="depth-run"):
        return next(row for row in self.memory.snapshot()["query_tasks"].values()
                    if row.get("route") == ROUTE and row["run_id"] == run_id)

    def attempts(self):
        with self.journal.locked():
            return self.journal.state()[1]

    def posts(self):
        return sum(method == "POST" for method, *_ in self.provider.calls)

    def calls_to(self, prefix):
        return sum(path.startswith(prefix) for _, path, *_ in self.provider.calls)

    def spend_state(self):
        return spend_watch.load_state(self.root)

    def context(self, acquisition_id, request, **fields):
        return {"task_id": "t", "acquisition_id": acquisition_id, "route": ROUTE, "request": request,
                "concept_ids": ["first"], "description_hashes": {"first": digest(DESCRIPTION)}, "reconciling": False, **fields}


class AdmissionTests(DepthFixture):
    def test_free_read_must_exist_and_be_reviewed_first(self):
        with self.assertRaisesRegex(SafetyError, "META_DEPTH_FREE_READ_PENDING"):
            depth_request(self.memory.snapshot(), "first", {"as_of": REVIEW_AT, "run_id": "r"})
        self.free_read()
        with self.assertRaisesRegex(SafetyError, "META_DEPTH_FREE_REVIEW_PENDING"):
            depth_request(self.memory.snapshot(), "first", {"as_of": REVIEW_AT, "run_id": "r"})
        self.review()
        request = depth_request(self.memory.snapshot(), "first", {"as_of": REVIEW_AT, "run_id": "r"})
        self.assertEqual(request, {"query": "draining sink caddy", "country": "IN", "limit": 10,
                                   "basis": digest(["123456"]), "version": "meta-depth-v2"})

    def test_answers_are_remembered_until_the_free_read_or_its_reviews_change(self):
        self.free_read()
        with patch("product_radar.market_validation.functional_market_context", wraps=functional_market_context) as reader:
            for _ in range(2):
                with self.assertRaisesRegex(SafetyError, "META_DEPTH_FREE_REVIEW_PENDING"):
                    depth_request(self.memory.snapshot(), "first", {"as_of": REVIEW_AT, "run_id": "r"})
            self.review()  # new saved reviews of the type: computed again
            for _ in range(2):
                self.assertIn("query", depth_request(self.memory.snapshot(), "first", {"as_of": REVIEW_AT, "run_id": "r"}))
        self.assertEqual(reader.call_count, 1)
        with patch("product_radar.source_interpretation.functional_source_questions", side_effect=SafetyError("SOURCE_REVIEW_TASK_SCOPE_INVALID")):
            meta_depth._MEMO.clear()
            with self.assertRaisesRegex(SafetyError, "SOURCE_REVIEW_TASK_SCOPE_INVALID"):  # an integrity error is never remembered
                depth_request(self.memory.snapshot(), "first", {"as_of": REVIEW_AT, "run_id": "r"})
        self.assertEqual(meta_depth._MEMO, {})

    def test_meaning_must_be_accepted(self):
        self.free_read(); self.review()
        with patch("product_radar.market_validation.functional_market_context", return_value=None):
            with self.assertRaisesRegex(SafetyError, "META_DEPTH_TYPE_MEANING_PENDING"):
                depth_request(self.memory.snapshot(), "first", {"as_of": REVIEW_AT, "run_id": "r"})

    def test_complete_settled_or_irrelevant_free_reads_are_not_applicable(self):
        complete = dict(self.card, text="Active\nLibrary ID: 123456\nStarted running on Oct 1, 2026\nDraining sink caddy",
                        links=[{"url": "https://www.facebook.com/profile.php?id=998877", "text": "Shop"}])
        self.free_read(complete); self.review()
        self.assertIn("already settles", self.plan()["not_applicable_reason"])  # complete: Not-positive
        unrelated = DepthFixture("run"); unrelated.setUp(); self.addCleanup(unrelated.doCleanups)
        unrelated.free_read(); unrelated.review(Runner(relationship="unrelated"))
        self.assertIn("no relevant ads", unrelated.plan()["not_applicable_reason"])

    def test_positive_meta_is_not_deepened_even_with_gaps(self):
        cards = [dict(self.card, ad_id=str(n), text=f"Active\nLibrary ID: {n}\nDraining sink caddy",
                      links=[{"url": f"https://www.facebook.com/profile.php?id=7{n}", "text": "Shop"}]) for n in (1, 2, 3)]
        self.free_read(cards=cards); self.review()
        gaps = meta_depth.depth_gaps(self.memory.snapshot(), "first", as_of=REVIEW_AT)
        self.assertEqual((gaps["meta_state"], gaps["lacking_ads"]), ("Positive", ["1", "2", "3"]))
        self.assertIn("already settles", self.plan()["not_applicable_reason"])

    def test_lacking_start_date_alone_admits_and_long_words_do_not_fit(self):
        card = dict(self.card, links=[{"url": "https://www.facebook.com/profile.php?id=998877", "text": "Shop"}])
        self.free_read(card); self.review()
        self.assertEqual(self.plan()["query"], "draining sink caddy")
        gaps = meta_depth.depth_gaps(self.memory.snapshot(), "first", as_of=REVIEW_AT)
        self.assertEqual((gaps["relevant_ads"], gaps["lacking_ads"], gaps["meta_state"]), (1, ["123456"], "Insufficient"))
        with patch("product_radar.meta_depth.depth_gaps", return_value=dict(gaps, query="x" * 130)):
            self.assertIn("do not fit", depth_request(self.memory.snapshot(), "first",
                                                      {"as_of": REVIEW_AT, "run_id": "other"})["not_applicable_reason"])

    def test_nearly_ready_types_see_their_deeper_read_as_finishing_work(self):
        from product_radar.nearly_ready import finishing_work
        self.free_read(); self.review(); self.plan()
        work = finishing_work(self.memory.snapshot(), {"source_run_id": "depth-run"}, ["first"], as_of=REVIEW_AT)
        self.assertIn("first", work["acquire"])  # the source stage serves it before new discovery

    def test_planner_builder_and_route_tables_register_the_route(self):
        self.assertEqual(ROUTES[ROUTE], {"source": "meta", "demand_group": "meta"})
        self.assertIn(ROUTE, PLANNED_ROUTES)
        from product_radar import operations, report_source_facts, research_cohort, source_acquisitions
        from product_radar.research_scope import _route_source
        self.assertEqual(_route_source(ROUTE), "meta")
        self.assertEqual(source_acquisitions.CAPTURE_SOURCES[ROUTE], "meta")
        self.assertEqual(research_cohort._SOURCES[ROUTE], "meta")
        self.assertIn(ROUTE, operations._SITES)
        self.assertIn(ROUTE, report_source_facts.ROUTE_LABELS)
        self.free_read(); self.review()
        build = product_source_request_builder(run_id="builder-run")
        context = {"route": ROUTE, **ROUTES[ROUTE], "concept_id": "first", "description": DESCRIPTION,
                   "description_hash": digest(DESCRIPTION), "state": self.memory.snapshot(), "run_id": "builder-run",
                   "as_of": REVIEW_AT}
        self.assertEqual(build(context)["query"], "draining sink caddy")
        installed = installed_source_handlers(self.memory, meta_depth_options={"root": self.root})
        self.assertTrue(callable(installed[ROUTE]["source_access_preflight"]))


class PaidReadTests(DepthFixture):
    def setUp(self):
        super().setUp()
        self.free_read(); self.review()
        self.binding = self.bind()

    def test_one_reserved_read_settles_imports_and_counts_meta_once(self):
        self.plan()
        result = self.execute()
        self.assertEqual(result["results"][0]["state"], "acquired", result)
        self.assertEqual((self.posts(), self.calls_to("/v2/users/me/limits")), (1, 1))
        task = self.task()
        self.assertEqual(len(task["attempt_refs"]), 1)
        attempt = self.attempts()[attempt_id_for(task["acquisition_id"])]
        self.assertEqual((attempt["work_scope"], attempt["stage"], attempt["cap"], attempt["actor_cap"]),
                         ("production", "meta", "0.05", "0.03"))
        self.assertEqual(attempt["charge"], "0.00380000")  # the provider's own terminal charge
        self.assertIsNone(attempt["read_charge"])  # the read cost stays an unknown hold until settled
        state = self.memory.snapshot()
        deep = [ref for ref, row in state["observations"].items() if row["facts"].get("source_route") == ROUTE]
        self.assertEqual(len(deep), 1)
        self.assertEqual(state["observations"][deep[0]]["facts"]["advertiser_url"], "https://www.facebook.com/SinkShop/")
        capture = state["captures"][state["observations"][deep[0]]["capture_id"]]
        self.assertEqual((capture["source"], capture["reference"]["route"], capture["reference"]["mapper"]),
                         ("meta", ROUTE, "meta-depth-profile-v1"))
        self.assertEqual(capture["reference"]["request"], task["request"])
        # Reviewed like any Meta capture, then counted once with the free read.
        self.review(as_of="2026-10-08T13:00:00+00:00")
        context = functional_market_context(self.memory.snapshot(), "first", as_of="2026-10-08T13:00:00+00:00")
        self.assertEqual(set(context["matches"]), {self.free_observation, *deep})  # both reads stay current evidence
        meta = context["market"]["sources"]["meta"]
        self.assertEqual(len(meta["used_ids"]), 1)  # one ad, counted once
        self.assertEqual((meta["depth"]["relevant_ads"], meta["depth"]["advertisers"], meta["depth"]["oldest_started_at"],
                          meta["depth"]["complete"]), (1, 1, "2026-09-01T00:00:00+00:00", True))
        self.assertNotIn(ROUTE, context["market"]["sources"])
        rows = {row["route"]: row for row in coverage_rows(None, self.memory.snapshot(), ["first"], "2026-10-08T13:00:00+00:00")["first"]}
        self.assertEqual((rows["meta-ads"]["state"], rows[ROUTE]["state"]), ("completed_relevant", "completed_relevant"))
        # The client sees one Meta row (it notes the deeper read), never a row of its own.
        from product_radar.report_source_facts import DEEPER_META_NOTE, source_route_facts
        state = self.memory.snapshot()
        relevant = {ref: state["observations"][ref] for ref in context["matches"] if ref in state["observations"]}
        product = {"market": context["market"], "functional_market_contract": context["contract"],
                   "functional_market_context": context}
        client = {row["route"]: row for row in source_route_facts(state, "first", list(rows.values()), product, relevant,
                                                                   "2026-10-08T13:00:00+00:00")}
        self.assertNotIn(ROUTE, client)
        self.assertIn(DEEPER_META_NOTE.strip(), client["meta-ads"]["detail"])
        free_only = {ref: row for ref, row in relevant.items() if row["facts"].get("source_route") != ROUTE}
        plain = source_route_facts(state, "first", list(rows.values()), product, free_only, "2026-10-08T13:00:00+00:00")
        self.assertNotIn(DEEPER_META_NOTE.strip(), next(row for row in plain if row["route"] == "meta-ads")["detail"])
        # The free read's gaps are the same, so they are not paid for again.
        self.assertIn("already read more deeply", self.plan("next-run", as_of="2026-10-08T13:00:00+00:00")["not_applicable_reason"])
        line = next(item for key, item in self.spend_state()["items"].items() if key.startswith("apify-"))
        self.assertEqual(Decimal(line["used"]), Decimal("0.0238"))
        # A saved receipt is replayed without another provider call.
        calls = len(self.provider.calls)
        handlers = self.handlers()
        envelope = handlers["reconcile"](self.context(task["acquisition_id"], task["request"], reconciling=True))
        self.assertEqual((envelope["state"], len(self.provider.calls)), ("usable", calls))
        with self.assertRaisesRegex(SafetyError, "META_DEPTH_RECEIPT_CONFLICT"):
            handlers["handler"](self.context(task["acquisition_id"], dict(task["request"], query="other words")))

    def test_unknown_bill_keeps_the_question_open_until_apify_reports_it(self):
        self.provider.charge = None
        self.plan()
        self.assertEqual(self.execute()["results"][0]["state"], "blocked")
        task = self.task()
        attempt_id = attempt_id_for(task["acquisition_id"])
        self.assertEqual((task["reconcile_required"], task["reason"]), (True, "META_DEPTH_CHARGE_UNKNOWN"))
        self.assertIsNone(self.attempts()[attempt_id]["charge"])
        self.assertFalse((self.memory.root / "meta_depth" / task["acquisition_id"] / "receipt.json").exists())
        line = next(item for key, item in self.spend_state()["items"].items() if key.startswith("apify-"))
        self.assertEqual(Decimal(line["used"]), Decimal("0.05"))  # an unknown bill is never a zero bill
        # Still unknown: a calm recheck, never another launch.
        self.clock_at = datetime(2026, 10, 8, 12, 5, tzinfo=timezone.utc)
        waiting = self.execute(as_of="2026-10-08T12:05:00+00:00")
        self.assertEqual((waiting["results"][0]["state"], waiting["results"][0]["reason"]), ("waiting", "META_DEPTH_CHARGE_UNKNOWN"))
        # Another question is not blocked for good: the only blocker is a finished run's
        # unknown bill, so the controller's bounded bill refresh is allowed to run.
        prepared, _ = prepared_for("meta-depth-other", "draining sink caddy")
        self.assertTrue(money_check(self.root, self.journal, LEGACY, prepared, now=self.clock_at)["refresh"])
        self.provider.charge = Decimal("0.0042")
        self.clock_at = datetime(2026, 10, 8, 12, 30, tzinfo=timezone.utc)
        done = self.execute(as_of="2026-10-08T12:30:00+00:00")
        self.assertEqual(done["results"][0]["state"], "acquired", done)
        self.assertEqual(self.attempts()[attempt_id]["charge"], "0.00420000")
        self.assertEqual(self.posts(), 1)
        self.assertFalse(money_check(self.root, self.journal, LEGACY, prepared, now=self.clock_at)["refresh"])

    def test_bounded_refresh_clears_an_earlier_unknown_bill_before_the_next_read(self):
        self.provider.charge = None
        self.plan(); self.execute()
        first = attempt_id_for(self.task()["acquisition_id"])
        self.provider.charge = Decimal("0.0042")  # Apify has now posted the bill
        save_preparation(self.root, "meta-depth-second", "draining sink caddy", now=self.clock_at)
        self.controller().preflight("meta-depth-second")  # refreshes earlier bills, then reserves nothing yet
        self.assertEqual(self.attempts()[first]["charge"], "0.00420000")

    def test_a_bill_confirmed_an_hour_later_is_not_read_again(self):
        self.plan(); self.execute()
        attempt_id = attempt_id_for(self.task()["acquisition_id"])
        def statuses():
            return sum(path == "/v2/actor-runs/runOne" for _, path, *_ in self.provider.calls)
        controller = self.controller()
        self.clock_at += timedelta(minutes=30)
        controller.refresh_costs()
        self.assertFalse(self.attempts()[attempt_id].get("charge_final"))  # too soon after the run ended
        self.provider.finished_at = (CLOCK - timedelta(hours=1)).isoformat()
        self.clock_at += timedelta(hours=2)
        controller._billing_observations.clear()
        controller.refresh_costs()
        self.assertTrue(self.attempts()[attempt_id]["charge_final"])
        before = statuses()
        controller._billing_observations.clear()
        self.assertEqual(controller.refresh_costs()["checked_attempts"], 0)
        self.assertEqual(statuses(), before)
        with self.journal.locked(), self.assertRaisesRegex(SafetyError, "JOURNAL_STATE_INVALID"):
            self.journal.append("late-update", "charge_update", {"attempt": attempt_id, "previous_usd": "0.00380000",
                                                                  "usd": "0.01", "evidence": "fictional-late"})

    def test_uncertain_paid_call_is_recovered_never_repeated(self):
        self.provider.lost = True
        self.plan()
        self.assertEqual(self.execute()["results"][0]["state"], "blocked")
        task = self.task()
        self.assertTrue(task["reconcile_required"])
        attempt_id = attempt_id_for(task["acquisition_id"])
        self.assertEqual(self.attempts()[attempt_id]["submission"], "uncertain")
        self.assertEqual(task["attempt_refs"], [task["acquisition_id"]])
        self.provider.lost = False
        self.clock_at = datetime(2026, 10, 8, 12, 5, tzinfo=timezone.utc)
        self.assertEqual(self.execute(as_of="2026-10-08T12:05:00+00:00")["results"][0]["state"], "waiting")
        self.assertEqual(self.posts(), 1)
        self.assertEqual(self.task()["reason"], "RECOVERY_UNIQUE_MATCH_NOT_ESTABLISHED")
        waiting = self.execute(as_of="2026-10-08T12:06:00+00:00")
        self.assertEqual((waiting["results"][0]["state"], waiting["handler_calls"]), ("waiting", 0))
        run = self.provider.run()["data"]
        run["startedAt"] = self.clock_at.isoformat()
        self.provider.recovery_items = [run]
        self.clock_at = datetime(2026, 10, 8, 12, 30, tzinfo=timezone.utc)
        third = self.execute(as_of="2026-10-08T12:30:00+00:00")
        self.assertEqual(third["results"][0]["state"], "acquired", third)
        self.assertEqual(self.posts(), 1)
        self.assertEqual(self.attempts()[attempt_id]["submission"], "submitted")

    def test_reservation_without_send_intent_is_cancelled_not_sent(self):
        self.plan()
        task = self.task()
        from product_radar.source_coverage import key
        aid = key("source-acquisition", [key("source-task", [task["run_id"], ROUTE, task["request"]]), 1])
        attempt_id = attempt_id_for(aid)
        prepared = save_preparation(self.root, attempt_id, task["request"]["query"], now=self.clock_at)
        with self.journal.locked():
            self.journal.reserve(prepared, self.binding, read_reserve="0.02")
        self.assertEqual(self.execute()["results"][0]["state"], "failed")
        self.assertEqual(self.attempts()[attempt_id]["submission"], "not_submitted")
        self.assertEqual(self.posts(), 0)
        saved = self.task()
        self.assertEqual((saved["retryable"], saved["reason"]), (True, "META_DEPTH_RESERVATION_CANCELLED"))
        handlers = self.handlers()
        self.assertEqual(handlers["handler"](self.context(aid, task["request"]))["reason"], "META_DEPTH_RESERVATION_CANCELLED")
        self.assertEqual(handlers["reconcile"](self.context("never-reserved", task["request"]))["reason"], "META_DEPTH_NOT_STARTED")

    def test_terminal_run_without_output_ends_without_a_second_paid_call(self):
        self.provider.dataset_absent = True
        self.plan()
        self.assertEqual(self.execute()["results"][0]["state"], "failed")
        self.assertEqual((self.task()["reason"], self.task()["retryable"], self.posts()), ("TERMINAL_DATASET_ABSENT", False, 1))

    def test_terminal_run_whose_output_read_fails_twice_ends_on_reconcile(self):
        self.provider.headerless = True  # dataset pages lack their pagination proof
        self.plan()
        self.assertEqual(self.execute()["results"][0]["state"], "blocked")
        self.assertEqual(self.execute(as_of="2026-10-08T12:05:00+00:00")["results"][0]["state"], "failed")
        self.assertEqual((self.task()["reason"], self.posts()), ("DATASET_PAGINATION_UNVERIFIED", 1))

    def test_default_controller_is_one_short_lock_controller_per_process(self):
        created = []
        class Controller:
            def __init__(self, root, journal, **options):
                created.append((root, options)); self.cancelled = []
            def resolve_non_submission(self, attempt_id):
                self.cancelled.append(attempt_id)
        handlers = create_meta_depth_handlers(self.memory, root=self.root, now=self.now)
        self.plan()
        task = self.task()
        with patch("product_radar.benchmark.MetaTrial", Controller):
            for name in ("acq-one", "acq-two"):
                prepared = save_preparation(self.root, attempt_id_for(name), task["request"]["query"], now=self.clock_at)
                with self.journal.locked():
                    self.journal.reserve(prepared, self.binding, read_reserve="0.02")
                self.assertEqual(handlers["handler"](self.context(name, task["request"]))["reason"],
                                 "META_DEPTH_RESERVATION_CANCELLED")
                with self.journal.locked():  # the fake controller sent nothing; cancel as the real one would
                    self.journal.append("cancel-" + attempt_id_for(name), "cancelled", {"attempt": attempt_id_for(name)})
        self.assertEqual(created, [(self.root, {"short_locks": True})])


class RequestContractTests(unittest.TestCase):
    def test_request_and_preparation_contracts(self):
        good = {"query": "q", "country": "IN", "limit": 10, "basis": "a" * 64, "version": "meta-depth-v2"}
        self.assertEqual(meta_depth._validated(good), good)
        for change in ({"limit": 30}, {"country": "US"}, {"version": "meta-depth-v1"}, {"basis": "short"}, {"query": ""}, {"extra": 1}):
            with self.subTest(change=change), self.assertRaisesRegex(SafetyError, "META_DEPTH_REQUEST_INVALID"):
                meta_depth._validated(dict(good, **change))
        request, envelope = prepared_for(attempt_id_for("acq-1"), "draining sink caddy")
        self.assertEqual((request.work_scope, request.purpose, request.reserved_usd, request.input["limitPerSource"]),
                         ("production", "validation", "0.03", 10))
        self.assertEqual(envelope["config_hash"], request.config_hash)
        with tempfile.TemporaryDirectory() as directory:
            at = datetime(2026, 10, 8, tzinfo=timezone.utc)
            first = save_preparation(directory, request.attempt_id, "draining sink caddy", now=at)
            self.assertEqual(save_preparation(directory, request.attempt_id, "draining sink caddy", now=at + timedelta(days=1)), first)
            with self.assertRaisesRegex(SafetyError, "SAVED_PREPARATION_CONFLICT"):
                save_preparation(directory, request.attempt_id, "other words", now=at)

    def test_daily_backup_keeps_the_question_receipt(self):
        from product_radar.operational_backup_selection import _Selection
        with tempfile.TemporaryDirectory() as directory, patch.object(_Selection, "add") as add:
            selection = _Selection({}, {}, Path(directory))
            selection.acquisition("acq-1", ROUTE, "query_tasks/t")
        paths = {str(call.args[0]) for call in add.call_args_list}
        self.assertIn(str(Path(directory) / "data/research/captured/meta_depth/acq-1/receipt.json"), paths)


if __name__ == "__main__":
    unittest.main()
