"""What the dropshipping platforms contribute (Plan R10 W4): the per-type local
match through the real source dispatcher, the hand-over to the existing
relevance review, the presence fact, supplier leads and effort, discovery
through the existing admission flow, and the daily backup of what they save.

Types, profiles and reviews are built with the project's synthetic fixtures;
platform feeds are offline doubles serving saved pages. No network, browser
or model is used (the A/B runner is the synthetic fixture runner).
"""

from copy import deepcopy
from pathlib import Path
import shutil
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "tests"))
from product_radar import dropship_platforms as platforms
from product_radar import platform_feeds as feeds
from product_radar import platform_matching as matching
from product_radar import platform_roles as roles
from product_radar import platform_signed_in as signed
from product_radar.functional_types import effective_functional_definition
from product_radar.models import SafetyError, digest, timestamp, utc_now
from product_radar.product_queries import resolve_query_profile
from product_radar.research_memory import ResearchMemory
from product_radar.research_scope import _route_source, accept_scoped_evidence
from product_radar.signed_in_attempts import SignedInWait
from product_radar.source_coverage import (ROUTES, execute_source_run, prepare_source_tasks, product_source_request_builder,
                                           route_registry)
from product_radar.source_interpretation import functional_source_questions
from product_radar.storage import Journal, atomic_json, load_json
from product_radar.supplier_effort import current_supplier_effort
from test_dropship_platforms import Clock, FeedDouble, START, caddy, deodap_pages, fixture, later, paced
from test_functional_source_market import functional_pool_fixture
from test_research_flow import FakeRunner

AT = "2026-10-15T00:00:00+00:00"


def pages(price="149.00"):
    products = {"products": fixture("deodap-products-2026-10-14.json")["products"] + [
        caddy(price=price), caddy(888, title="Wall Sink Organiser Rack", price="99.00", mrp=None),
        caddy(889, title="Sink Caddy Tray", price="89.00", mrp=None)]}
    return deodap_pages(products=products, lists={"best-selling-products": {"products": [caddy(price=price)]},
                                                  "just-arrived": fixture("deodap-best-selling-products-2026-10-14.json")})


def item(item_id, title, *, roles=(), state_lists=0, in_stock=True, product_type="Kitchen", tags=(), seen=START):
    """A saved catalogue item for discovery checks."""
    held = {role: {"first_seen_at": seen, "last_seen_at": seen, "via": ["list-" + role]} for role in roles}
    if state_lists:
        held["state_bestseller"] = {"first_seen_at": seen, "last_seen_at": seen,
                                    "via": ["state-" + str(n) + "-best-selling" for n in range(state_lists)]}
    return {"item_id": item_id, "title": title, "url": "https://deodap.in/products/" + item_id, "images": [],
            "price": "99", "mrp": None, "price_basis": "b", "in_stock": in_stock, "vendor": None,
            "product_type": product_type, "tags": list(tags), "created_at": None, "terms": None, "first_seen_at": seen,
            "last_seen_at": seen, "source_ref": "raw/2026-10-14/000001.json.gz", "collections": {}, "roles": held}


class RolesCase(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp = tempfile.TemporaryDirectory()
        cls.memory, (cls.cid,) = functional_pool_fixture(Path(cls.temp.name) / "pool", count=1, with_market=False)
        cls.pristine = Path(cls.temp.name) / "pool"

    @classmethod
    def tearDownClass(cls):
        cls.temp.cleanup()

    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        # The store sits where the project keeps it, so the daily backup selection can be checked as it really runs.
        self.project = Path(temp.name) / "project"
        shutil.copytree(self.pristine / "research", self.project / "data/research/captured")
        shutil.copytree(self.pristine / "finance", Path(temp.name) / "finance")
        self.memory = ResearchMemory(self.project / "data/research/captured", Journal(Path(temp.name) / "finance"),
                                     evidence_type="synthetic")
        self.pacing_root = self.project

    def build_catalogue(self, route="deodap", served=None, start=START):
        reader = feeds.FeedReader(route, pacing_root=self.pacing_root, fetcher=FeedDouble(served or pages()))
        clock = Clock(start)
        with paced(clock):
            while platforms.advance_platform(self.memory.root, route, reader, clock=clock, sleep=lambda _: None,
                                             limit=50)["state"] not in ("current", "waiting"):
                pass
        return platforms.record_catalogue_states(self.memory, self.memory.root)

    def request(self, route="deodap"):
        state = self.memory.snapshot()
        return product_source_request_builder()({"concept_id": self.cid, "state": state, "route": route, "as_of": AT,
            "description": state["concepts"][self.cid]["description"],
            "description_hash": digest(state["concepts"][self.cid]["description"])})

    def profile(self):
        return resolve_query_profile(self.memory.snapshot(), self.cid)

    def dispatch(self, route="deodap", run_id="platform-run", request=None):
        prepare_source_tasks(self.memory, [{"route": route, "request": request or self.request(route), "concept_ids": [self.cid]}],
                             run_id=run_id, as_of=AT)
        return execute_source_run(self.memory, run_id, handlers=matching.create_dropship_handlers(self.memory), as_of=AT)

    def task(self, route="deodap"):
        return next(row for row in self.memory.snapshot()["query_tasks"].values()
                    if row.get("kind") == "all_source_task_v1" and row["route"] == route)

    def review(self, observation_id, relationship="exact_product"):
        state = self.memory.snapshot()
        row = state["observations"][observation_id]
        accept_scoped_evidence(self.memory, self.cid, route=row["source"], scope="narrow_type", relationship=relationship,
            support="insufficient", references=[{"table": "observations", "id": observation_id, "hash": digest(row)}],
            usable_fields=[{"reference": {"table": "observations", "id": observation_id}, "path": ["semantic"]}],
            limitations=["Synthetic platform listing."], reason="Explicit fixture review.", reviewer="synthetic", as_of=AT,
            expected_meaning_hash=effective_functional_definition(state, self.cid)["meaning_hash"])

    def matched(self, item_id, route="deodap"):
        state = self.memory.snapshot()
        return next(oid for oid, row in sorted(state["observations"].items(), key=lambda pair: pair[1]["observed_at"], reverse=True)
                    if row["source"] == route and row["facts"]["item_id"] == item_id
                    and row["facts"]["list_type"] == "dropship_catalogue_match")

    def catalogues(self):
        return {route: platforms.load_catalogue(self.memory.root, route) for route in platforms.ROUTES}


class MatchingTests(RolesCase):
    def test_route_tables_register_the_four_platform_routes(self):
        for route in platforms.ROUTES:
            self.assertEqual(ROUTES[route], {"source": "dropship-platforms", "demand_group": None})
            self.assertEqual(_route_source(route), route)
        registry = route_registry(matching.create_dropship_handlers(self.memory))
        self.assertTrue(all(registry[route]["connected"] for route in platforms.ROUTES))
        from product_radar.operations import _SITES
        from product_radar.report_source_facts import CLIENT_ROUTES, ROUTE_LABELS
        from product_radar.research_cohort import _SOURCES
        from product_radar.source_acquisitions import CAPTURE_SOURCES
        for route in platforms.ROUTES:
            self.assertEqual((ROUTE_LABELS[route], _SITES[route], CAPTURE_SOURCES[route], _SOURCES[route]),
                             (platforms.LABELS[route], platforms.LABELS[route], route, "dropship-platforms"))
            self.assertIn(route, CLIENT_ROUTES)
        from product_radar.discovery import MARKET_SITES
        # Not market sites: their presence fact feeds the opportunity score directly.
        self.assertFalse(set(platforms.ROUTES) & set(MARKET_SITES))
        from product_radar.operational_backup_selection import _PUBLIC_SETTINGS
        self.assertIn("config/research/dropship_platforms.json", _PUBLIC_SETTINGS)

    def test_request_carries_the_types_words_and_the_catalogue_date(self):
        self.build_catalogue()
        at = platforms.catalogue_date(self.memory.snapshot(), "deodap")
        self.assertEqual(self.request(), {"catalogue": "deodap", "catalogue_at": at, "terms": [
            "draining sink caddy", "sink organiser", "draining sink holder"], "match_version": matching.MATCH_VERSION,
            "country": "IN"})
        with self.assertRaisesRegex(SafetyError, "PRODUCT_QUERY_PROFILE_REQUIRED"):
            matching.platform_request("deodap", {"queries": [], "core_type": "the", "aliases": []}, at)
        with self.assertRaisesRegex(SafetyError, "DROPSHIP_ROUTE_INVALID"):
            matching.platform_request("amazon-search", {"queries": []}, at)
        # The next weekly catalogue is a new question, so new arrivals are matched again.
        self.build_catalogue(start=later(START, 8 * 86400))
        self.assertGreater(self.request()["catalogue_at"], at)

    def test_local_match_is_judged_on_the_title_and_capped(self):
        catalogue = platforms.load_catalogue(self.memory.root, "deodap")

        def row(product_id, title, tags=()):
            parsed = feeds.parse_products([dict(caddy(product_id, title=title), tags=list(tags))], "https://deodap.in")[0]
            return dict(platforms.clean_item(parsed, price_basis="b"), first_seen_at=START, last_seen_at=START, source_ref="r",
                        collections={}, roles={})
        catalogue["items"] = {"1": row(1, "Steel Draining Sink Caddy"), "2": row(2, "Caddy for cars"),
                              "3": row(3, "Plastic box", tags=["draining sink caddy"]),
                              "4": row(4, "Draining Sink Caddy", tags=["sink caddy", "draining"]),
                              **{str(n): row(n, "Draining Sink Caddy " + str(n)) for n in range(10, 16)}}
        found = matching.local_matches(catalogue, ["draining sink caddies"], limit=5, min_coverage="0.67")
        self.assertEqual(len(found), 5)  # the per-type cap
        self.assertNotIn("3", [row["item"]["item_id"] for row in found])  # tags alone never qualify an item
        self.assertEqual((found[0]["coverage"], found[0]["shared"]), ("1.00", ["caddy", "draining", "sink"]))
        found = matching.local_matches(dict(catalogue, items={key: catalogue["items"][key] for key in ("1", "4")}),
                                       ["draining sink caddies"], limit=5, min_coverage="0.67")
        self.assertEqual([row["item"]["item_id"] for row in found], ["4", "1"])  # equal title coverage: tags break the tie
        self.assertEqual(matching.words("boxes brushes glass caddies glasses"), {"box", "brush", "caddy", "glass"})
        self.assertEqual(platforms.settings()["matching"]["max_matches_per_type"], 5)


class DispatchTests(RolesCase):
    def test_questions_are_planned_only_after_the_platform_has_a_catalogue(self):
        with self.assertRaisesRegex(SafetyError, "DROPSHIP_CATALOGUE_PENDING"):
            self.request()
        self.assertEqual(self.build_catalogue(), ["deodap"])
        self.assertEqual(self.request()["catalogue"], "deodap")
        self.assertEqual(platforms.record_catalogue_states(self.memory, self.memory.root), [])  # once per finished cycle

    def test_question_waits_calmly_until_the_platform_has_a_catalogue(self):
        request = matching.platform_request("deodap", self.profile(), START)
        result = self.dispatch(request=request)
        row = result["results"][0]
        self.assertEqual((row["state"], row["reason"], row["actual_attempt"]), ("waiting", "DROPSHIP_CATALOGUE_PENDING", False))
        self.assertIsNotNone(row["next_retry_at"])
        self.assertEqual(result["handler_calls"], 0)  # the wait never uses the round's source allowance
        self.assertEqual(self.task()["status"], "prepared")
        hint = matching.create_dropship_handlers(self.memory)["deodap"]["source_access_hint"]({})
        self.assertEqual((hint["ready"], hint["reason"]), (False, "DROPSHIP_CATALOGUE_PENDING"))
        # The wait is judged at the caller's time, never the wall clock, so the
        # same saved state and time always give the same readiness answer.
        timed = matching.create_dropship_handlers(self.memory)["deodap"]["source_access_hint"]({"as_of": START})
        self.assertEqual(timed["next_retry_at"], later(START, 600))
        with self.assertRaises(SignedInWait) as waited:
            matching.collect_type_matches(self.memory, "deodap", {"request": request, "acquisition_id": "aid-3",
                                                                  "as_of": START})
        self.assertEqual(waited.exception.next_retry_at, later(START, 600))
        # A finished refresh whose catalogue file is missing still waits rather than answering "none".
        refresh = platforms.load_refresh(self.memory.root, "deodap")
        refresh["completed"]["full"] = {"started_at": START, "completed_at": START, "items": 3}
        platforms._save_refresh(self.memory.root, "deodap", refresh)
        self.assertIs(matching.create_dropship_handlers(self.memory)["deodap"]["source_access_hint"]({})["ready"], True)
        with self.assertRaisesRegex(SignedInWait, "DROPSHIP_CATALOGUE_PENDING"):
            matching.collect_type_matches(self.memory, "deodap", {"request": request, "acquisition_id": "aid-2"})

    def test_signed_in_platform_question_waits_for_the_owner(self):
        reader = signed.SignedInReader("dropdash", pacing_root=self.pacing_root, browser=lambda *a, **k: self.fail("opened"))
        platforms.advance_platform(self.memory.root, "dropdash", reader, as_of=AT)
        with self.assertRaises(SignedInWait) as raised:
            matching.collect_type_matches(self.memory, "dropdash", {"request": matching.platform_request(
                "dropdash", self.profile(), START), "acquisition_id": "aid-1"}, signed_in_root=self.pacing_root)
        self.assertEqual(str(raised.exception), "OWNER_SIGN_IN_REQUIRED")
        self.assertIs(raised.exception.calm_wait, True)
        self.assertGreater(timestamp(raised.exception.next_retry_at), timestamp(utc_now()))

    def test_matches_are_answered_locally_without_a_source_slot_and_reach_the_review(self):
        self.build_catalogue()
        result = self.dispatch()
        self.assertEqual((result["results"][0]["state"], result["handler_calls"]), ("acquired", 0))
        task = self.task()
        state = self.memory.snapshot()
        envelope = state["source_checks"][task["receipt_refs"][0]]["envelope"]
        self.assertIs(envelope["dispatch_started"], True)
        observations = [state["observations"][oid] for oid in envelope["observation_ids"]]
        # "Sink Caddy Tray" covers only two of three words of "draining sink caddy" (below 0.67).
        self.assertEqual({row["facts"]["item_id"] for row in observations}, {"777", "888"})
        first = next(row for row in observations if row["facts"]["item_id"] == "777")
        self.assertEqual(first["source"], "deodap")
        self.assertEqual((first["facts"]["price"]["amount"], first["facts"]["mrp"], first["facts"]["roles"]),
                         ("149", "499", ["bestseller"]))
        self.assertEqual(first["semantic"]["title"], "Steel Draining Sink Caddy with Drip Tray")
        self.assertEqual(task["target_results"][self.cid]["state"], "interpretation_pending")
        questions = [row for row in functional_source_questions(self.memory.snapshot(), as_of=AT) if row["route"] == "deodap"]
        self.assertEqual(len(questions), 1)
        self.assertEqual(set(questions[0]["unknown_ids"]), set(envelope["observation_ids"]))
        capture = state["captures"][first["capture_id"]]
        self.assertEqual(set(capture["reference"]), {"route", "request", "folder", "file"})
        # The saved receipt answers a reconciliation without matching again.
        context = {"request": task["request"], "acquisition_id": task["acquisition_id"]}
        with patch.object(matching, "local_matches", side_effect=AssertionError("matched again")):
            again = matching.collect_type_matches(self.memory, "deodap", context)
        self.assertEqual(again["observation_ids"], envelope["observation_ids"])
        with self.assertRaisesRegex(SafetyError, "DROPSHIP_MATCH_RECEIPT_CONFLICT"):
            matching.collect_type_matches(self.memory, "deodap", dict(context, request=dict(task["request"], terms=["other words"])))
        with self.assertRaisesRegex(SafetyError, "DROPSHIP_REQUEST_INVALID"):
            matching.collect_type_matches(self.memory, "deodap", dict(context, request={"catalogue": "deodap"}))

    def test_no_matching_item_completes_without_results(self):
        self.build_catalogue(served=deodap_pages())
        result = self.dispatch()
        self.assertEqual(result["results"][0]["state"], "completed_no_results")
        self.assertEqual(self.task()["target_results"][self.cid]["state"], "completed_no_results")

    def test_daily_backup_resolves_every_platform_capture_and_lead_receipt(self):
        from product_radar.operational_backup_selection import select_essential_backup_files
        self.build_catalogue()
        self.dispatch()
        self.review(self.matched("777"))
        catalogues = self.catalogues()
        catalogues["deodap"]["items"]["k1"] = item("k1", "Kitchen Spice Rack Model", roles=["bestseller"])
        roles.advance_platform_discovery(self.memory, root=self.memory.root, runner=FakeRunner(), as_of=AT, catalogues=catalogues)
        presence = roles.record_platform_presence(self.memory, self.cid, root=self.memory.root, as_of=AT)
        roles.record_platform_leads(self.memory, self.cid, presence, root=self.memory.root, catalogues=self.catalogues())
        state = self.memory.snapshot()
        platform = {"captures/" + cid for cid, row in state["captures"].items() if row["source"] in platforms.ROUTES}
        self.assertEqual(len(platform), 2)  # one per-type match, one discovery capture
        efforts = {"source_checks/" + rid for rid, row in state["source_checks"].items()
                   if row.get("kind") == "supplier-effort-v1" and row.get("route") in platforms.ROUTES}
        self.assertEqual(len(efforts), 1)
        with self.memory.journal.locked():
            financial = self.memory.journal.state()
        selection = select_essential_backup_files(state, financial, root=self.project)
        problems = [row for row in selection["unresolved_required"] + selection["unsafe"]
                    if any(row["origin"].startswith(origin) for origin in platform | efforts)]
        self.assertEqual(problems, [])
        selected = {row["relative_path"] for row in selection["files"]}
        for area in ("matches", "discovery", "leads"):
            self.assertTrue(any(path.startswith("data/research/captured/dropship_platforms/" + area + "/") for path in selected))


class PresenceTests(RolesCase):
    def setUp(self):
        super().setUp()
        self.build_catalogue()
        self.dispatch()

    def test_presence_fact_reports_platforms_lists_prices_dates_and_catalogue_scope(self):
        self.review(self.matched("777"))
        value = roles.platform_presence(self.memory.snapshot(), self.cid, root=self.memory.root, as_of=AT)
        deodap = value["platforms"]["deodap"]
        self.assertEqual((value["platform_count"], value["platforms_carrying"], value["on_list_platforms"]),
                         (1, ["deodap"], ["deodap"]))
        self.assertEqual((deodap["state"], deodap["relevant_count"], deodap["pending_review"]), ("relevant", 1, 1))
        self.assertEqual((deodap["lowest_price"], deodap["highest_mrp"], deodap["in_stock_items"]), ("149", "499", 1))
        self.assertEqual((deodap["catalogue_scope"], deodap["catalogue_items"]), ("full", 10))
        self.assertEqual(deodap["list_roles"], ["bestseller"])
        self.assertFalse(deodap["on_new_arrivals"])
        self.assertEqual(deodap["links"], [deodap["items"][0]["url"]])
        self.assertEqual(deodap["items"][0]["terms"], roles.NOT_STATED)
        self.assertEqual(deodap["items"][0]["first_seen_at"], deodap["first_seen_at"])
        self.assertGreaterEqual(deodap["items"][0]["roles"]["bestseller"]["first_seen_at"], deodap["first_seen_at"])
        self.assertEqual(value["platforms"]["baapstore"]["state"], "not_checked")
        self.review(self.matched("888"), relationship="unrelated")
        value = roles.platform_presence(self.memory.snapshot(), self.cid, root=self.memory.root, as_of=AT)
        self.assertEqual(value["platforms"]["deodap"]["pending_review"], 0)
        # Relevant but no longer listed is not "relevant".
        catalogues = self.catalogues()
        del catalogues["deodap"]["items"]["777"]
        value = roles.platform_presence(self.memory.snapshot(), self.cid, root=self.memory.root, as_of=AT, catalogues=catalogues)
        self.assertEqual((value["platforms"]["deodap"]["state"], value["platform_count"]), ("relevant_delisted", 0))

    def test_no_relevant_items_says_whether_the_whole_platform_or_a_sample_was_checked(self):
        self.review(self.matched("777"), relationship="unrelated")
        self.review(self.matched("888"), relationship="unrelated")
        value = roles.platform_presence(self.memory.snapshot(), self.cid, root=self.memory.root, as_of=AT)
        self.assertEqual((value["platforms"]["deodap"]["state"], value["platform_count"]), ("no_relevant_items", 0))
        # A signed-in platform's catalogue is a sample of a few pages.
        sample = platforms.load_catalogue(self.memory.root, "dropdash")
        sample.update(full_completed_at=START, updated_at=START, items={"x": item("x", "Folding Travel Pillow")})
        platforms.save_catalogue(self.memory.root, "dropdash", sample)
        refresh = platforms.load_refresh(self.memory.root, "dropdash")
        refresh["completed"]["full"] = {"started_at": START, "completed_at": START, "items": 1}
        platforms._save_refresh(self.memory.root, "dropdash", refresh)
        platforms.record_catalogue_states(self.memory, self.memory.root)
        self.dispatch("dropdash")
        value = roles.platform_presence(self.memory.snapshot(), self.cid, root=self.memory.root, as_of=AT)
        dropdash = value["platforms"]["dropdash"]
        self.assertEqual((dropdash["state"], dropdash["catalogue_scope"], dropdash["catalogue_items"]),
                         ("no_relevant_items_in_sample", "sample", 1))

    def test_saved_presence_is_versioned_reused_and_renewed_weekly(self):
        self.assertIsNone(roles.current_platform_presence(self.memory.snapshot(), self.cid))
        self.review(self.matched("777"))
        saved = roles.record_platform_presence(self.memory, self.cid, root=self.memory.root, as_of=AT)
        state = self.memory.snapshot()
        self.assertEqual(roles.current_platform_presence(state, self.cid), saved)
        self.assertEqual((saved["version"], saved["at"]), (roles.PRESENCE_VERSION, AT))
        count = len(state["source_checks"])
        self.assertEqual(roles.record_platform_presence(self.memory, self.cid, root=self.memory.root, as_of=later(AT, 3600)), saved)
        self.assertEqual(len(self.memory.snapshot()["source_checks"]), count)
        # Renewed at five days, while the score still counts it until seven: no gap in the points.
        self.assertEqual(roles.record_platform_presence(self.memory, self.cid, root=self.memory.root,
                                                        as_of=later(AT, 5 * 86400 - 1)), saved)
        renewed = roles.record_platform_presence(self.memory, self.cid, root=self.memory.root, as_of=later(AT, 5 * 86400))
        self.assertEqual(renewed["at"], later(AT, 5 * 86400))
        from product_radar.opportunity_score import PRESENCE_DAYS
        self.assertLess(roles.PRESENCE_RENEW_DAYS, PRESENCE_DAYS)
        self.assertEqual(roles.current_platform_presence(self.memory.snapshot(), self.cid)["at"], renewed["at"])
        broken = dict(saved, platforms={})
        with self.assertRaisesRegex(SafetyError, "DROPSHIP_PRESENCE_RECORD_INVALID"):
            roles._valid_presence(broken)

    def test_saved_presence_earns_score_points_for_a_week(self):
        from product_radar.opportunity_score import score_type
        from product_radar.discovery import MARKET_SITES
        self.review(self.matched("777"))
        saved = roles.record_platform_presence(self.memory, self.cid, root=self.memory.root, as_of=AT)
        item = saved["platforms"]["deodap"]["items"][0]
        market = {"sources": {site: {"available": False, "positive": None, "used_ids": []} for site in MARKET_SITES}}
        fit = {"fit_policy_id": "fit-10-v1", "kpi_matches": 6, "kpi_total": 10}
        state = self.memory.snapshot()
        record = score_type(state, self.cid, fit, market, as_of=later(AT, 3600))
        lists = record["opportunity_basis"]["parts"]["want"]["items"]["platform_lists"]
        self.assertEqual((lists["points"], lists["evidence_ids"]), (4, [item["observation_id"]]))  # one platform's bestseller list
        self.assertEqual(lists["facts"]["presence_at"], AT)
        stale = score_type(state, self.cid, fit, market, as_of=later(AT, 8 * 86400))
        self.assertEqual(stale["opportunity_basis"]["parts"]["want"]["items"]["platform_lists"]["points"], 0)

    def test_relevant_listing_is_one_stable_lead_with_effort_from_a_kept_copy_of_its_read(self):
        self.review(self.matched("777"))
        presence = roles.record_platform_presence(self.memory, self.cid, root=self.memory.root, as_of=AT)
        recorded = roles.record_platform_leads(self.memory, self.cid, presence, root=self.memory.root, catalogues=self.catalogues())
        self.assertEqual([row["route"] for row in recorded], ["deodap"])
        state = self.memory.snapshot()
        leads = roles.platform_leads(state, self.cid)
        self.assertEqual(len(leads), 1)
        lead = leads[0]
        self.assertEqual(lead["lead_id"], roles.lead_id(self.cid, "deodap", "777"))
        self.assertNotIn("roles", lead)
        self.assertEqual((lead["price"]["amount"], lead["mrp"], lead["in_stock"], lead["item_terms"]),
                         ("149", "499", True, roles.NOT_STATED))
        self.assertIn("No MOQ", lead["stated_terms"])
        effort = current_supplier_effort(state, self.cid, state["concepts"][self.cid]["description"],
                                         accepted_lead_refs=[lead["lead_id"]], as_of=later(AT, 86400 * 400))
        self.assertTrue(effort["effort_satisfied"])
        record = state["source_checks"][recorded[0]["effort_id"]]
        self.assertEqual((record["route"], record["internal_outcome"], record["receipt_refs"][0]["proof"]),
                         ("deodap", "usable_leads", "owned_reader_execution"))
        self.assertEqual(record["reference"], lead["evidence"])
        kept = Path(lead["evidence"])
        self.assertTrue(kept.is_file())
        # The platform's own raw reads may now be pruned: the lead keeps its copy.
        shutil.rmtree(platforms.platform_folder(self.memory.root, "deodap") / "raw")
        self.assertTrue(kept.is_file())
        count = len(state["source_checks"])
        self.assertEqual(roles.record_platform_leads(self.memory, self.cid, presence, root=self.memory.root,
                                                     catalogues=self.catalogues()), [])
        self.assertEqual(len(self.memory.snapshot()["source_checks"]), count)

    def test_a_price_change_updates_the_same_lead_and_records_effort_again(self):
        self.review(self.matched("777"))
        presence = roles.record_platform_presence(self.memory, self.cid, root=self.memory.root, as_of=AT)
        roles.record_platform_leads(self.memory, self.cid, presence, root=self.memory.root, catalogues=self.catalogues())
        self.build_catalogue(served=pages(price="139.00"), start=later(START, 8 * 86400))
        presence = roles.record_platform_presence(self.memory, self.cid, root=self.memory.root, as_of=later(AT, 8 * 86400))
        recorded = roles.record_platform_leads(self.memory, self.cid, presence, root=self.memory.root, catalogues=self.catalogues())
        self.assertEqual(recorded[0]["lead_ids"], [roles.lead_id(self.cid, "deodap", "777")])
        leads = roles.platform_leads(self.memory.snapshot(), self.cid)
        self.assertEqual([row["price"]["amount"] for row in leads], ["139"])


class DiscoveryTests(RolesCase):
    def config(self, **changes):
        return dict(platforms.settings()["discovery"], **changes)

    def test_only_in_stock_in_category_list_items_in_priority_order_reach_admission(self):
        catalogues = self.catalogues()
        catalogues["deodap"]["items"] = {row["item_id"]: row for row in [
            item("n1", "Silicone Baby Feeding Bottle", roles=["new_arrival"], product_type="Baby", seen=later(START, 50)),
            item("n2", "Gold Plated Kitchen Jewellery Set", roles=["new_arrival"], product_type="Jewellery"),
            item("n3", "Travel Neck Pillow", roles=["new_arrival"], in_stock=False, product_type="Travel"),
            item("n4", "Wireless Earbuds Case", roles=["new_arrival"], product_type="Mobile Accessories"),
            item("b1", "Steel Spice Rack", roles=["bestseller"], seen=START),
            item("s1", "Stainless Steel Water Bottle", state_lists=2),
            item("s2", "Stainless Steel Lunch Box", state_lists=1),
            item("x1", "Mystery Gadget", roles=["bestseller"], product_type="Gadgets"),
        ]}
        catalogues["baapstore"]["items"] = {row["item_id"]: row for row in [
            item("t1", "Hanging Planter Pot", roles=["trending"], product_type="Garden"),
            item("t2", "Steel Spice Rack", roles=["trending"])]}  # the same title words as DeoDap's b1
        order = roles._discovery_candidates(self.memory.snapshot(), catalogues, self.config())
        self.assertEqual([(route, row["item_id"]) for route, row in order],
                         [("deodap", "b1"), ("deodap", "s1"), ("baapstore", "t1"), ("deodap", "n1")])
        self.assertEqual(roles.item_category(item("y", "Kids Silicone Plate", product_type="Baby"), self.config()), "Baby Products")
        self.assertEqual(roles.item_category(item("z", "Folding Travel Bag", product_type="Luggage"), self.config()),
                         "Travel and Outdoor")

    def test_items_already_captured_or_covering_an_existing_type_are_presence_not_candidates(self):
        self.build_catalogue()
        order = roles._discovery_candidates(self.memory.snapshot(), self.catalogues(), self.config())
        self.assertIn("777", [row["item_id"] for _, row in order])
        self.dispatch()  # the type's platform question: its words are now a known type
        order = roles._discovery_candidates(self.memory.snapshot(), self.catalogues(), self.config())
        self.assertNotIn("777", [row["item_id"] for _, row in order])

    def test_admission_is_bounded_per_round_per_day_and_by_pending_captures(self):
        catalogues = self.catalogues()
        names = ("alpha bravo charlie delta echo foxtrot golf hotel india juliet kilo lima mike november oscar papa "
                 "quebec romeo sierra tango").split()
        catalogues["deodap"]["items"] = {"k" + str(n): item("k" + str(n), "Kitchen Caddy " + name.title(), roles=["bestseller"])
                                         for n, name in enumerate(names)}
        before = set(self.memory.snapshot()["concepts"])
        first = roles.advance_platform_discovery(self.memory, root=self.memory.root, runner=FakeRunner(), as_of=AT,
                                                 catalogues=catalogues)
        self.assertEqual((first["nominated"], first["pending_acquisitions"]), (5, 1))
        self.assertTrue(first["new_concept_ids"])
        self.assertTrue(set(first["new_concept_ids"]) <= set(self.memory.snapshot()["concepts"]) - before)
        with patch.object(roles, "settings", return_value=dict(platforms.settings(), discovery=self.config(daily_cap=7))):
            second = roles.advance_platform_discovery(self.memory, root=self.memory.root, runner=FakeRunner(),
                                                      as_of=later(AT, 60), catalogues=catalogues)
        self.assertEqual(second["nominated"], 2)  # the daily cap
        # Captures still being admitted hold further nominations (no model work here).
        with patch("product_radar.research_flow.run_flow"):
            rounds = [roles.advance_platform_discovery(self.memory, root=self.memory.root, runner=FakeRunner(),
                                                       as_of=later(AT, 86400 + 60 * n), catalogues=catalogues)["nominated"]
                      for n in range(3)]
        self.assertEqual(rounds, [5, 5, 0])

    def test_failed_or_old_described_items_count_as_settled_so_admission_never_stalls(self):
        catalogues = self.catalogues()
        catalogues["deodap"]["items"] = {"k1": item("k1", "Kitchen Caddy Model A", roles=["bestseller"])}
        with patch("product_radar.research_flow.run_flow"):
            roles.advance_platform_discovery(self.memory, root=self.memory.root, runner=FakeRunner(), as_of=AT, catalogues=catalogues)
        state = self.memory.snapshot()
        capture = next(row for row in state["captures"].values() if row.get("mapper_version") == matching.DISCOVERY_MAPPER)
        self.assertFalse(roles._settled(state, capture["acquisition_id"], capture["imported_at"], AT, 3))
        # An item the AI never described is never given up on, however old (W8 review).
        self.assertFalse(roles._settled(state, capture["acquisition_id"], capture["imported_at"],
                                        later(capture["imported_at"], 4 * 86400), 3))
        semantic = next(row["semantic_id"] for row in state["observations"].values()
                        if row["acquisition_id"] == capture["acquisition_id"])
        # Once described, an old capture whose products are still undecided counts as settled.
        described = deepcopy(state)
        described["descriptions"][semantic] = {"result": {"products": [{"label": "Kitchen caddy"}]}, "job_id": "j"}
        self.assertFalse(roles._settled(described, capture["acquisition_id"], capture["imported_at"], AT, 3))
        self.assertTrue(roles._settled(described, capture["acquisition_id"], capture["imported_at"],
                                       later(capture["imported_at"], 4 * 86400), 3))
        failed = deepcopy(state)
        failed["problems"]["p1"] = {"stage": "description", "affected_id": semantic, "reason": "AI_RESULT_INVALID"}
        self.assertTrue(roles._settled(failed, capture["acquisition_id"], capture["imported_at"], AT, 3))

    def readers(self):
        return {"deodap": feeds.FeedReader("deodap", pacing_root=self.pacing_root, fetcher=FeedDouble(pages())),
                "baapstore": feeds.FeedReader("baapstore", pacing_root=self.pacing_root, fetcher=FeedDouble({})),
                "dropship-india": signed.SignedInReader("dropship-india", pacing_root=self.pacing_root, browser=None),
                "dropdash": signed.SignedInReader("dropdash", pacing_root=self.pacing_root, browser=None)}

    def test_platform_lane_step_refreshes_then_records_presence_leads_and_admission(self):
        readers = self.readers()
        self.build_catalogue()
        self.dispatch()
        self.review(self.matched("777"))
        with patch.object(roles, "_readers", return_value=readers), \
                patch("product_radar.programme.programme_service_state", return_value={"enabled": True}):
            summary = roles.platform_step(self.memory, {}, {"at": later(AT, 3600), "runner": FakeRunner()})
        self.assertEqual(list(summary["refresh"]), ["dropship-india", "dropdash", "deodap", "baapstore"])
        self.assertEqual(summary["refresh"]["dropship-india"]["reason"], "OWNER_SIGN_IN_REQUIRED")
        self.assertEqual((summary["presence"]["checked"], summary["presence"]["unchanged"], summary["leads"]), (1, 0, 1))
        self.assertIsInstance(summary["discovery"]["new_concept_ids"], list)
        self.assertIsNotNone(roles.current_platform_presence(self.memory.snapshot(), self.cid))

    def test_presence_and_leads_are_skipped_while_their_inputs_are_unchanged(self):
        self.build_catalogue()
        self.dispatch()
        self.review(self.matched("777"))
        idle = {route: None for route in platforms.ROUTES}
        def advance(at):
            with patch.object(roles, "_readers", return_value=idle), \
                    patch.object(roles, "advance_platform", return_value={"state": "current"}), \
                    patch.object(roles, "advance_platform_discovery", return_value={"new_concept_ids": []}):
                return roles.advance_dropship_platforms(self.memory, {"at": at})
        first = advance(later(AT, 60))
        self.assertEqual((first["presence"]["checked"], first["presence"]["unchanged"]), (1, 0))
        with patch.object(roles, "record_platform_presence", side_effect=AssertionError("not worked out again")), \
                patch.object(roles, "record_platform_leads", side_effect=AssertionError("not worked out again")):
            second = advance(later(AT, 120))
        self.assertEqual((second["presence"]["checked"], second["presence"]["unchanged"]), (0, 1))
        saved = load_json(self.memory.root / "dropship_platforms" / roles.PRESENCE_INPUTS)
        self.assertEqual((saved["version"], list(saved["types"])), (roles.PRESENCE_VERSION, [self.cid]))
        # A finished catalogue cycle is a changed input: worked out again.
        catalogue = platforms.load_catalogue(self.memory.root, "deodap")
        platforms.save_catalogue(self.memory.root, "deodap", dict(catalogue, lists_completed_at=later(AT, 90)))
        self.assertEqual(advance(later(AT, 180))["presence"]["checked"], 1)
        # So is a week-old saved fact, and an unreadable or foreign speed-aid file.
        self.assertEqual(advance(later(AT, 8 * 86400))["presence"]["checked"], 1)
        (self.memory.root / "dropship_platforms" / roles.PRESENCE_INPUTS).write_text("{corrupt")
        self.assertEqual(advance(later(AT, 8 * 86400 + 60))["presence"]["checked"], 1)
        atomic_json(self.memory.root / "dropship_platforms" / roles.PRESENCE_INPUTS, {"version": "old", "types": {}})
        self.assertEqual(advance(later(AT, 8 * 86400 + 120))["presence"]["checked"], 1)
        self.assertEqual(advance(later(AT, 8 * 86400 + 180))["presence"]["unchanged"], 1)

    def test_presence_inputs_cover_reviews_checks_meaning_and_catalogues(self):
        self.build_catalogue()
        self.dispatch()
        state = self.memory.snapshot()
        from product_radar.functional_types import effective_functional_definition
        meaning = effective_functional_definition(state, self.cid)
        catalogues = self.catalogues()
        base = roles.presence_input_hash(state, self.cid, meaning, roles.catalogue_inputs(catalogues),
                                         roles._presence_input_index(state))
        self.review(self.matched("777"))
        state = self.memory.snapshot()
        reviewed = roles.presence_input_hash(state, self.cid, meaning, roles.catalogue_inputs(catalogues),
                                             roles._presence_input_index(state))
        self.assertNotEqual(base, reviewed)
        other = dict(catalogues, deodap=dict(catalogues["deodap"], full_completed_at=later(AT, 30)))
        self.assertNotEqual(reviewed, roles.presence_input_hash(state, self.cid, meaning, roles.catalogue_inputs(other),
                                                                roles._presence_input_index(state)))
        self.assertNotEqual(reviewed, roles.presence_input_hash(state, self.cid, dict(meaning, meaning_hash="0" * 64),
                                                                roles.catalogue_inputs(catalogues), roles._presence_input_index(state)))

    def test_owner_pause_stops_platform_reads(self):
        with patch("product_radar.programme.programme_service_state", return_value={"enabled": False}):
            with self.assertRaisesRegex(SafetyError, "PROGRAMME_OWNER_PAUSED"):
                roles._before_read(self.memory)()

    def test_round_hook_reports_local_failures_without_stopping(self):
        with patch.object(roles, "_readers", return_value={route: None for route in platforms.ROUTES}), \
                patch.object(roles, "advance_platform", side_effect=SafetyError("DROPSHIP_REFRESH_BUSY")), \
                patch.object(roles, "record_catalogue_states", side_effect=OSError("disk")), \
                patch.object(roles, "advance_platform_discovery", side_effect=OSError("disk")):
            summary = roles.advance_dropship_platforms(self.memory, {"at": AT})
        self.assertEqual(summary["refresh"]["deodap"], {"state": "waiting", "reason": "DROPSHIP_REFRESH_BUSY"})
        self.assertEqual(summary["catalogues_noted"]["reason"], "DROPSHIP_LOCAL_IO_FAILED")
        self.assertEqual(summary["discovery"]["reason"], "DROPSHIP_LOCAL_IO_FAILED")
        with patch.object(roles, "_readers", return_value={route: None for route in platforms.ROUTES}), \
                patch.object(roles, "advance_platform", side_effect=SafetyError("RESEARCH_EVENTS_INVALID")):
            with self.assertRaisesRegex(SafetyError, "RESEARCH_EVENTS_INVALID"):
                roles.advance_dropship_platforms(self.memory, {"at": AT})


if __name__ == "__main__":
    unittest.main()
