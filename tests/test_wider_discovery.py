"""Wider discovery (Plan R10 W8): Meta advertisers' other active ads, Google
Trends rising queries as Meta searches, and IndiaMART/TradeIndia listings for
mapped category terms. The category gate, de-duplication and limits before any
AI call; each read as a shared source acquisition whose refusal holds every
lane's reads of that site; the round's source-read allowance; read-only,
paced reads that yield IndiaMART to the type supplier searches; recovery
without a repeated read; per-read failure containment; its place after the
heading searches; the round limits config; daily backups; the portal's route
list; and supplier listings as report examples.

Types and reviews are built with the project's synthetic fixtures. Browsers,
IndiaMART and TradeIndia are offline doubles; no network, browser or model is
used (the A/B runner is the synthetic fixture runner).
"""

from copy import deepcopy
from datetime import timedelta
from io import StringIO
import importlib.util
import json
from pathlib import Path
import re
import shutil
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "tests"))
from product_radar import catalogue_discovery as discovery
from product_radar import meta_advertisers as advertisers
from product_radar import report_examples as examples
from product_radar import round_limits
from product_radar import wider_access as access
from product_radar import wider_candidates as candidates
from product_radar import wider_discovery as wider
from product_radar.catalogue import load_catalogue
from product_radar.functional_types import effective_functional_definition
from product_radar.meta_browser import EXTRACT, parse_meta_snapshot, prepare_meta_query
from product_radar.models import SafetyError, digest, timestamp
from product_radar.research_memory import ResearchMemory
from product_radar.research_scope import accept_scoped_evidence
from product_radar.signed_in_attempts import SignedInWait
from product_radar.source_acquisitions import _record
from product_radar.source_coverage import source_access_scope, source_access_wait
from product_radar.storage import Journal, atomic_json, load_json
from product_radar.supplier_route_acquisition import supplier_route_url
from product_radar.work_turns import DEFAULT_POLICY, bind_work_turn, open_work_turn
from test_functional_source_market import functional_pool_fixture
from test_research_flow import FakeRunner
from test_supplier_route_acquisition import receipt as page_receipt

AT = "2026-10-15T00:00:00+00:00"
AFTER = {"discovery_position": "after", "priority_concept_ids": {}}
META_SCOPE = source_access_scope(prepare_meta_query("anything")["url"])

sys.path.insert(0, str(Path(__file__).resolve().parent))
import frozen_research_config  # noqa: E402
# Exact expectations use the r10 settings these tests were written against, not the growing live map.
setUpModule, tearDownModule = frozen_research_config.module_fixtures()


def later(at, seconds):
    return (timestamp(at) + timedelta(seconds=seconds)).isoformat()


def card(ad_id, body, *, active=True, advertiser="200"):
    status = "Active" if active else "Inactive"
    return {"ad_id": ad_id, "text": "Library ID: " + ad_id + "\n" + status + "\nStarted running on 1 Oct 2026\nSponsored\n" + body,
            "links": [{"url": "https://www.facebook.com/profile.php?id=" + advertiser, "text": "Shop"}], "images": []}


class MetaBrowser:
    """An owned hidden-browser double: the Ad Library page with these cards."""

    def __init__(self, cards, *, status=200, at=AT):
        self.cards, self.status, self.calls, self.at = cards, status, [], at
        self.text = "Ad Library" if cards else "No ads match your search."

    def __call__(self, url, expression, folder, *, timeout=45, headed=False):
        self.calls.append({"url": url, "expression": expression, "headed": headed})
        result = {"state": "captured", "navigation_started": True, "navigation_status": self.status,
                  "owned_process_exited": True, "completed_at": self.at,
                  "snapshot": {"url": url, "title": "Ad Library", "text": self.text, "cards": deepcopy(self.cards)}}
        atomic_json(Path(folder) / "native_result.json", result)
        return result


def supplier_receipt(item, body="", status=200, at=AT):
    """A supplier page receipt as the route's own readers return it (closed reader)."""
    return dict(page_receipt(item, body=body, status=status), text=re.sub(r"<[^>]+>", " ", body), checked_at=at,
                attempt_started_at=at, transport_contract="public-page-owned-http-v1", owned_worker_exited=True,
                owned_process_exited=True)


def indiamart_html(rows):
    data = {"props": {"pageProps": {"dummyOtpPage": False, "searchResponse": {"results": [
        {"title": title, "url": url, "companyname": "Synthetic Works", "city": "Rajkot", "price": "120", "unit": "Piece"}
        for title, url in rows]}}}}
    return ('<main class="listing"></main><script id="__NEXT_DATA__" type="application/json">' + json.dumps(data)
            + '</script>')


class Case(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp = tempfile.TemporaryDirectory()
        cls.pristine = Path(cls.temp.name) / "pool"
        _, cls.ids = functional_pool_fixture(cls.pristine, count=2, with_market=False)

    @classmethod
    def tearDownClass(cls):
        cls.temp.cleanup()

    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.project = Path(temp.name) / "project"
        shutil.copytree(self.pristine / "research", self.project / "data/research/captured")
        shutil.copytree(self.pristine / "finance", Path(temp.name) / "finance")
        self.memory = ResearchMemory(self.project / "data/research/captured", Journal(Path(temp.name) / "finance"),
                                     evidence_type="synthetic")
        enabled = patch("product_radar.programme.programme_service_state", return_value={"enabled": True})
        enabled.start()
        self.addCleanup(enabled.stop)

    def config(self, *, meta=0, rising=0, indiamart=0, tradeindia=0, **changes):
        value = deepcopy(candidates.settings())
        value["meta_advertisers"]["reads_per_round"] = meta
        value["trends_rising"]["reads_per_round"] = rising
        value["supplier_listings"]["reads_per_round"] = {"indiamart": indiamart, "tradeindia": tradeindia}
        for name, row in changes.items():
            value[name] = dict(value[name], **row) if isinstance(row, dict) else row
        return candidates.validate_settings(value)

    def review(self, advertiser, relationship="exact_product", cid=None):
        """A review of the fixture ad of this advertiser (accepted unless unrelated)."""
        state = self.memory.snapshot()
        oid, row = next((oid, row) for oid, row in state["observations"].items()
                        if row["source"] == "meta" and row["facts"]["advertiser_id"] == advertiser)
        cid = cid or self.ids[0]
        accept_scoped_evidence(self.memory, cid, route="meta-ads", scope="narrow_type", relationship=relationship,
            support="insufficient", references=[{"table": "observations", "id": oid, "hash": digest(row)}],
            usable_fields=[{"reference": {"table": "observations", "id": oid}, "path": ["semantic"]}],
            limitations=["Synthetic ad."], reason="Explicit fixture review.", reviewer="synthetic", as_of=AT,
            expected_meaning_hash=effective_functional_definition(state, cid)["meaning_hash"])

    def run_lane(self, config, readers=None, *, at=AT, runner="default", catalogue=None, context=None):
        runner = FakeRunner() if runner == "default" else runner
        with patch.object(wider, "settings", return_value=config):
            return wider.advance_wider_discovery(self.memory, {"at": at, "runner": runner, **AFTER, **(context or {})},
                                                 programme_id="programme", catalogue=catalogue, readers=readers or {},
                                                 signed_in_root=self.project)

    def reads(self):
        return candidates.reads(self.memory.snapshot())

    def owner(self, record):
        return _record(self.memory.snapshot(), record["acquisition_id"])

    def response(self, record):
        state = self.memory.snapshot()
        return state["source_checks"][_record(state, record["acquisition_id"])["active_response_id"]]

    def save_rising(self, queries, *, observed=AT, name="trends-read-1", state="read"):
        folder = self.memory.root / "direct_trends" / name
        folder.mkdir(parents=True, exist_ok=True)
        from product_radar.trends_rising import FILE, KIND, PARSER_VERSION
        record = {"kind": KIND, "parser_version": PARSER_VERSION, "acquisition_id": name, "term": "spice rack",
                  "observed_at": observed, "state": state, "queries": [
                      {"rank": n + 1, "query": text, "raw_value": value, "growth_kind": "breakout" if value == "Breakout" else "percentage",
                       "growth_percent": None if value == "Breakout" else float(value.strip("+%"))}
                      for n, (text, value) in enumerate(queries)]}
        atomic_json(folder / FILE, record)
        return folder, record

    def official(self):
        """The official reader of a saved list, without a whole saved Trends read."""
        def read(folder):
            return load_json(Path(folder) / "rising_queries.json")
        return patch("product_radar.trends_rising.rising_queries", side_effect=read)


class SettingsTests(Case):
    def test_settings_and_mapped_terms_pass_the_category_gate(self):
        value = candidates.settings()
        for category, terms in value["supplier_listings"]["terms"].items():
            self.assertTrue(all(candidates.gate(term) == category for term in terms))
        self.assertEqual(candidates.gate("Gold plated kitchen earring set"), None)
        self.assertEqual(candidates.gate("   "), None)
        self.assertEqual(candidates.gate("Folding travel bag"), "Travel and Outdoor")
        broken = [dict(value, kind="other"), dict(value, retry_after_hours=0),
                  dict(value, meta_advertisers=dict(value["meta_advertisers"], reads_per_round=9)),
                  dict(value, supplier_listings=dict(value["supplier_listings"], terms=dict(
                      value["supplier_listings"]["terms"], **{"Baby Products": ["kitchen utensil"]}))),
                  dict(value, supplier_listings=dict(value["supplier_listings"], terms=dict(
                      value["supplier_listings"]["terms"], **{"Baby Products": ["baby  toy"]}))),
                  {name: item for name, item in value.items() if name != "admission"}, None]
        for item in broken:
            with self.assertRaisesRegex(SafetyError, "WIDER_DISCOVERY_SETTINGS_INVALID"):
                candidates.validate_settings(item)

    def test_category_words_are_read_once_until_their_file_changes(self):
        from product_radar import dropship_platforms
        candidates._CATEGORY_WORDS.clear()
        with patch.object(dropship_platforms, "settings", wraps=dropship_platforms.settings) as read:
            for _ in range(5):
                candidates.gate("Kitchen spice rack")
        self.assertEqual(read.call_count, 1)
        candidates._CATEGORY_WORDS["stamp"] = (0, 0)  # as if the file changed
        with patch.object(dropship_platforms, "settings", wraps=dropship_platforms.settings) as read:
            candidates.gate("Kitchen spice rack")
        self.assertEqual(read.call_count, 1)

    def test_round_limits_are_the_single_config_with_sixteen_reads_and_ten_ai_calls(self):
        limits = round_limits.round_limits()
        self.assertEqual((limits["source_reads"], limits["ai_calls"]), (16, 10))
        # The turn policy's static defaults equal the config (no file is read at import).
        self.assertEqual((DEFAULT_POLICY["source_callbacks"], DEFAULT_POLICY["model_executions"]),
                         (limits["source_reads"], limits["ai_calls"]))
        from product_radar.r10_cutover import CONFIG_FILES
        self.assertTrue({"config/research/round_limits.json", "config/research/wider_discovery.json"} <= set(CONFIG_FILES))
        policy = round_limits.round_policy()
        self.assertEqual((policy["source_callbacks"], policy["model_executions"], policy["exploration_slots"]), (16, 10, 2))
        self.assertEqual(set(policy), set(DEFAULT_POLICY))
        self.assertEqual(round_limits.round_policy(1)["exploration_slots"], 1)
        for broken in (dict(limits, ai_calls=1), dict(limits, source_reads=0), dict(limits, extra=1), dict(limits, kind="x"), []):
            with self.assertRaisesRegex(SafetyError, "ROUND_LIMITS_INVALID"):
                round_limits.validate_round_limits(broken)

    def test_cli_uses_the_configured_round_limits_and_installs_the_wider_lane(self):
        path = ROOT / "scripts/run_programme.py"
        specification = importlib.util.spec_from_file_location("wider_programme_cli", path)
        cli = importlib.util.module_from_spec(specification)
        specification.loader.exec_module(cli)
        for arguments, reads in (([], 16), (["--source-actions", "7"], 7)):
            with self.subTest(arguments=arguments), patch("sys.stdout", new=StringIO()), \
                    patch.object(cli, "installed_source_handlers", return_value={}), \
                    patch.object(cli, "load_programme", return_value={"programme_id": "whole-programme"}), \
                    patch("product_radar.catalogue_discovery.create_catalogue_discovery_step",
                          return_value=lambda *_args: {"state": "checked"}) as factory, \
                    patch.object(cli, "advance_ready_programme_turn",
                                 return_value={"round_completed": False, "programme_id": "whole-programme"}) as turn:
                self.assertEqual(cli.main(["--programme-id", "whole-programme", "--execute", "--cooperative-turns", *arguments],
                    memory=self.memory, root=self.project, private_staging=lambda *_args: {}, backup_step=lambda *_args: {}), 0)
                self.assertIs(factory.call_args.kwargs["wider_step"], wider.wider_step)
                from product_radar.product_map import map_step
                self.assertIs(factory.call_args.kwargs["map_step"], map_step)  # W10: the product-map lane
                policy = turn.call_args.kwargs["turn_policy"]
                self.assertEqual((turn.call_args.kwargs["source_limit"], policy["source_callbacks"], policy["model_executions"]),
                                 (reads, reads, 10))
        with patch("sys.stdout", new=StringIO()), patch("sys.stderr", new=StringIO()), \
                patch.object(round_limits, "round_limits", side_effect=SafetyError("ROUND_LIMITS_INVALID")):
            with self.assertRaises(SystemExit):
                cli.main(["--programme-id", "whole-programme", "--execute"], memory=self.memory, root=self.project)

    def test_new_settings_are_checked_public_settings_of_the_daily_backup(self):
        from product_radar import operational_backup_selection as backup
        self.assertIn("wider_discovery", backup._SOURCE_AREAS)
        empty = {table: {} for table in self.memory.snapshot()}
        for name, reason in (("config/research/wider_discovery.json", "WIDER_DISCOVERY_SETTINGS_INVALID"),
                             ("config/research/round_limits.json", "ROUND_LIMITS_INVALID")):
            with self.subTest(name=name):
                temp = tempfile.TemporaryDirectory()
                self.addCleanup(temp.cleanup)
                root = Path(temp.name)
                good = load_json(ROOT / name)
                atomic_json(root / name, good)
                selected = backup.select_essential_backup_files(empty, ({}, {}), root=root)
                self.assertEqual([row["kind"] for row in selected["files"]], ["public_setting"])
                self.assertEqual(selected["unsafe"], [])
                atomic_json(root / name, dict(good, extra=1))
                selected = backup.select_essential_backup_files(empty, ({}, {}), root=root)
                self.assertEqual((selected["files"], [row["reason"] for row in selected["unsafe"]]), ([], [reason]))

    def test_portal_route_list_already_knows_every_route_the_lane_reads(self):
        from product_radar.source_coverage import ROUTES
        # The lane adds no route: it reads through routes the packets and the portal already know.
        portal = (ROOT / "portal/functional-packet.mjs").read_text(encoding="utf-8")
        for route in ("meta-ads", "indiamart", "tradeindia"):
            self.assertIn(route, ROUTES)
            self.assertIn("'" + route + "'", portal)


class AdvertiserTests(Case):
    def test_only_advertisers_of_relevant_reviewed_ads_are_chosen_and_rechecked_after_a_month(self):
        config = self.config(meta=2)
        state = self.memory.snapshot()
        self.assertEqual(candidates.advertiser_targets(state, AT, config, {}), [])
        self.review("200")
        self.review("201", relationship="unrelated")
        targets = candidates.advertiser_targets(self.memory.snapshot(), AT, config, {})
        self.assertEqual([(row["key"], row["route"], row["category"]) for row in targets],
                         [("200", "meta-ads", "Home and Kitchen")])
        done = {("meta-advertisers", "200"): {"state": "read", "updated_at": AT}}
        self.assertEqual(candidates.advertiser_targets(self.memory.snapshot(), later(AT, 86400), config, done), [])
        self.assertEqual(len(candidates.advertiser_targets(self.memory.snapshot(), later(AT, 31 * 86400), config, done)), 1)
        # A retired type's reviews no longer nominate its advertisers.
        self.review("201", cid=self.ids[1])
        state = self.memory.snapshot()
        retired = deepcopy(state["concepts"][self.ids[1]])
        retired["status"] = "retired"
        self.memory.put("concepts", self.ids[1], retired, previous=state["concepts"][self.ids[1]])
        self.assertEqual([row["key"] for row in candidates.advertiser_targets(self.memory.snapshot(), AT, config, {})], ["200"])
        failed = {("meta-advertisers", "200"): {"state": "refused", "updated_at": AT}}
        self.assertEqual(candidates.advertiser_targets(self.memory.snapshot(), later(AT, 3600), config, failed), [])
        self.assertEqual(len(candidates.advertiser_targets(self.memory.snapshot(), later(AT, 25 * 3600), config, failed)), 1)
        parked = {("meta-advertisers", "200"): {"state": "uncertain", "updated_at": AT}}
        self.assertEqual(candidates.advertiser_targets(self.memory.snapshot(), later(AT, 25 * 3600), config, parked), [])
        self.assertEqual(len(candidates.advertiser_targets(self.memory.snapshot(), later(AT, 31 * 86400), config, parked)), 1)
        # A type's unreadable reviews never hide the other advertisers; a broken store is never ignored.
        with patch("product_radar.research_scope.validated_scoped_evidence", side_effect=SafetyError("RESEARCH_SCOPE_EVIDENCE_INVALID")):
            self.assertEqual(candidates.advertiser_targets(self.memory.snapshot(), AT, config, {}), [])
        with patch("product_radar.research_scope.validated_scoped_evidence", side_effect=SafetyError("RESEARCH_EVENTS_INVALID")):
            with self.assertRaisesRegex(SafetyError, "RESEARCH_EVENTS_INVALID"):
                candidates.advertiser_targets(self.memory.snapshot(), AT, config, {})

    def test_advertiser_page_read_admits_only_new_active_in_category_ads_and_is_a_shared_read(self):
        self.review("200")
        browser = MetaBrowser([card("100", "Blue sink caddy 000"),  # already seen
                               card("9001", "Stainless steel kitchen spice rack with six jars"),
                               card("9002", "Gold plated kitchen earring set"),  # excluded kind
                               card("9003", "Kitchen knife sharpener", active=False),
                               card("9004", "Big festive sale on everything today")])  # no client category
        runner = FakeRunner()
        summary = self.run_lane(self.config(meta=1), {"meta": browser}, runner=runner)
        self.assertEqual([call["url"] for call in browser.calls], [advertisers.prepare_advertiser_request("200")["url"]])
        self.assertIn("view_all_page_id=200", browser.calls[0]["url"])
        self.assertIn("active_status=active", browser.calls[0]["url"])
        self.assertEqual((browser.calls[0]["expression"], browser.calls[0]["headed"]), (EXTRACT, False))
        self.assertEqual([(row["key"], row["state"], row["candidates"]) for row in summary["reads"]], [("200", "read", 1)])
        record = self.reads()[0]
        state = self.memory.snapshot()
        observations = [row for row in state["observations"].values() if row["acquisition_id"] == record["acquisition_id"]]
        self.assertEqual([(row["source"], row["facts"]["ad_id"]) for row in observations], [("meta", "9001")])
        # The read is a source acquisition with a closed, complete response that feeds the shared access wait.
        response = self.response(record)
        self.assertEqual((response["bridge_contract"], response["complete"], response["access"]["owned_process_exited"]),
                         (advertisers.BRIDGE, True, True))
        self.assertEqual(self.owner(record)["binding"]["capture_options"], access.META_PURPOSE)
        self.assertEqual(summary["admission"]["pending_reads"], 1)
        self.assertTrue(summary["new_concept_ids"])
        self.assertIn("A", {role for role, _ in runner.calls})
        # The raw page and the nomination are in the daily backup.
        from product_radar.operational_backup_selection import select_essential_backup_files
        with self.memory.journal.locked():
            financial = self.memory.journal.state()
        selected = select_essential_backup_files(self.memory.snapshot(), financial, root=self.project)
        chosen = {row["relative_path"] for row in selected["files"]}
        folder = "data/research/captured/wider_discovery/" + record["acquisition_id"]
        self.assertTrue({folder + "/" + name for name in ("snapshot.json", "receipt.json", "nomination.json")} <= chosen)
        self.assertFalse([row for row in selected["unsafe"] + selected["unresolved_required"] if "wider" in json.dumps(row)])
        # An advertiser whose ads were all seen before nominates nothing.
        self.review("201")
        seen = self.run_lane(self.config(meta=1), {"meta": MetaBrowser([card("9001", "Stainless steel kitchen spice rack")])},
                             at=later(AT, 60), runner=runner)
        self.assertEqual([(row["key"], row["state"], row["candidates"]) for row in seen["reads"]], [("201", "read", 0)])
        self.assertIsNone(self.reads()[-1]["capture_id"])
        # Neither is read again within its recheck window.
        self.assertEqual(self.run_lane(self.config(meta=1), {"meta": MetaBrowser([])}, at=later(AT, 3600))["reads"], [])

    def test_a_refusal_holds_every_meta_read_of_the_round_and_the_heading_searches(self):
        catalogue = load_catalogue()
        self.review("200")
        self.save_rising([("wooden spice rack for kitchen", "Breakout")])
        browser = MetaBrowser([], status=429)
        with self.official():
            summary = self.run_lane(self.config(meta=1, rising=1), {"meta": browser}, catalogue=catalogue)
        self.assertEqual(len(browser.calls), 1)
        self.assertEqual([row["state"] for row in summary["reads"]], ["refused"])
        self.assertEqual([row["reason"] for row in summary["waits"]], ["SOURCE_ACCESS_WAIT"])
        # The heading searches use the same site and reader identity: they wait too.
        wait = source_access_wait(self.memory.snapshot(), META_SCOPE, as_of=later(AT, 60))
        self.assertEqual((wait["phase"], wait["acquisition_id"]), ("waiting", self.reads()[0]["acquisition_id"]))
        with self.official():
            again = self.run_lane(self.config(meta=1, rising=1), {"meta": browser}, at=later(AT, 120), catalogue=catalogue)
        self.assertEqual((again["reads"], len(browser.calls)), ([], 1))

    def test_a_wait_set_by_another_lane_holds_these_reads(self):
        self.review("200")
        with patch("product_radar.source_coverage.source_access_wait",
                   return_value={"phase": "waiting", "reason": "SOURCE_ACCESS_WAIT", "next_retry_at": later(AT, 900)}):
            waits = self.run_lane(self.config(meta=1), {"meta": MetaBrowser([])})
        self.assertEqual(waits["waits"], [{"lane": "meta-advertisers", "route": "meta-ads", "reason": "SOURCE_ACCESS_WAIT",
                                           "next_retry_at": later(AT, 900)}])
        with patch("product_radar.source_coverage.source_access_wait", side_effect=SafetyError("SOURCE_ACCESS_RECORD_INVALID")):
            self.assertEqual(self.run_lane(self.config(meta=1), {})["waits"][0]["reason"], "SOURCE_ACCESS_RECORD_INVALID")

    def test_advertiser_page_scope_and_status(self):
        request = advertisers.prepare_advertiser_request("200")
        snapshot = {"url": request["url"], "cards": [card("9001", "Kitchen spice rack")]}
        self.assertEqual(advertisers.parse_advertiser_snapshot(snapshot, request, 200)["state"], "usable")
        other = dict(snapshot, url=advertisers.prepare_advertiser_request("201")["url"])
        self.assertEqual(advertisers.parse_advertiser_snapshot(other, request, 200)["state"], "insufficient")
        self.assertEqual(advertisers.parse_advertiser_snapshot(snapshot, request, 429)["state"], "blocked")
        self.assertEqual(advertisers.parse_advertiser_snapshot(snapshot, request, 500)["state"], "failed")
        for bad in ("abc", "1" * 41, 200):
            with self.assertRaisesRegex(SafetyError, "META_ADVERTISER_REQUEST_INVALID"):
                advertisers.prepare_advertiser_request(bad)
        with self.assertRaisesRegex(SafetyError, "META_PUBLIC_SNAPSHOT_INVALID"):
            advertisers.parse_advertiser_snapshot({"url": request["url"]}, request, 200)

    def test_a_saved_page_is_reused_and_a_read_is_never_repeated_on_reconciliation(self):
        root = self.project / "pages"
        browser = MetaBrowser([card("9001", "Kitchen spice rack")])
        first, folder = advertisers.capture_advertiser_ads(root, "read-1", "200", browser=browser)
        second, _ = advertisers.capture_advertiser_ads(root, "read-1", "200", browser=browser, reconcile=True)
        self.assertEqual((first, len(browser.calls)), (second, 1))
        with self.assertRaisesRegex(SafetyError, "SOURCE_ORIGINAL_RECEIPT_PENDING"):
            advertisers.capture_advertiser_ads(root, "read-2", "200", browser=browser, reconcile=True)
        # A crash after the browser returned: its saved result is parsed, never a new read.
        (folder / "receipt.json").unlink()
        recovered, _ = advertisers.capture_advertiser_ads(root, "read-1", "200", browser=None, reconcile=True)
        self.assertEqual(recovered["records"], first["records"])
        (folder / "snapshot.json").write_text("{}")
        with self.assertRaisesRegex(SafetyError, "META_ADVERTISER_RECEIPT_CONFLICT"):
            advertisers.capture_advertiser_ads(root, "read-1", "200", browser=browser)
        with self.assertRaisesRegex(SafetyError, "META_ADVERTISER_RECEIPT_CONFLICT"):
            advertisers.capture_advertiser_ads(root, "read-1", "201", browser=browser)
        # A browser result without its saved intent is not this read's.
        atomic_json(root / "read-3" / "native_result.json", {"snapshot": None})
        with self.assertRaisesRegex(SafetyError, "META_ADVERTISER_RECEIPT_CONFLICT"):
            advertisers.capture_advertiser_ads(root, "read-3", "200", browser=None)
        # A browser that never started is saved as such.
        failed, _ = advertisers.capture_advertiser_ads(
            root, "read-4", "200", browser=lambda *_args, **_options: {"navigation_started": False, "reason": "NATIVE_PUBLIC_BROWSER_UNAVAILABLE"})
        self.assertEqual((failed["state"], failed["reason"]), ("unavailable", "NATIVE_PUBLIC_BROWSER_UNAVAILABLE"))
        self.assertEqual(advertisers.capture_advertiser_ads(root, "read-4", "200", browser=None)[0], failed)
        saved = load_json(root / "read-4" / "receipt.json")
        atomic_json(root / "read-4" / "receipt.json", dict(saved, reason="changed"))
        with self.assertRaisesRegex(SafetyError, "META_ADVERTISER_MAPPING_CHANGED"):
            advertisers.capture_advertiser_ads(root, "read-4", "200", browser=None)
        large = MetaBrowser([card(str(9000 + n), "x" * 11000) for n in range(50)])
        with patch.object(advertisers, "MAX_BYTES", 1000):
            with self.assertRaisesRegex(SafetyError, "META_PUBLIC_CAPTURE_LIMIT"):
                advertisers.capture_advertiser_ads(root, "read-5", "200", browser=large)

    def test_the_advertiser_response_binds_only_its_own_saved_page(self):
        self.review("200")
        self.run_lane(self.config(meta=1), {"meta": MetaBrowser([card("9001", "Kitchen spice rack")])})
        record = self.reads()[0]
        folder = Path(record["folder"])
        with self.assertRaisesRegex(SafetyError, "META_ADVERTISER_RECEIPT_SCOPE_INVALID"):
            advertisers.record_advertiser_response(self.memory, record["acquisition_id"], folder=folder.parent / "other",
                                                   as_of=later(AT, 60))
        with self.assertRaisesRegex(SafetyError, "SOURCE_ACQUISITION_SOURCE_DATE_INVALID"):
            advertisers.record_advertiser_response(self.memory, record["acquisition_id"], folder=folder, as_of=later(AT, -60))
        # Replaying the same saved page is the same response.
        replay = advertisers.record_advertiser_response(self.memory, record["acquisition_id"], folder=folder, as_of=later(AT, 60))
        self.assertTrue(replay["reused"])
        # A page saved for a different request, or older than the response already bound, is refused.
        from product_radar.source_acquisitions import claim_source_acquisition
        other = dict(self.owner(record)["binding"]["request"], limit=20)
        claim = claim_source_acquisition(self.memory, "meta-ads", other, owner_id="other", as_of=AT,
                                         capture_options=access.META_PURPOSE)
        shutil.copytree(folder, folder.parent / claim["acquisition_id"])
        for name in ("intent.json", "receipt.json"):
            saved = load_json(folder.parent / claim["acquisition_id"] / name)
            atomic_json(folder.parent / claim["acquisition_id"] / name, dict(saved, acquisition_id=claim["acquisition_id"]))
        with self.assertRaisesRegex(SafetyError, "META_ADVERTISER_RECEIPT_SCOPE_INVALID"):
            advertisers.record_advertiser_response(self.memory, claim["acquisition_id"],
                                                   folder=folder.parent / claim["acquisition_id"], as_of=later(AT, 60))
        saved = load_json(folder / "receipt.json")
        atomic_json(folder / "receipt.json", dict(saved, observed_at=later(AT, -3600)))
        with self.assertRaisesRegex(SafetyError, "SOURCE_ACQUISITION_RECEIPT_CONFLICT"):
            advertisers.record_advertiser_response(self.memory, record["acquisition_id"], folder=folder, as_of=later(AT, 60))

    def test_the_search_parser_is_unchanged_by_the_shared_card_rules(self):
        request = prepare_meta_query("spice rack")
        snapshot = {"url": request["url"], "cards": [card("9001", "Kitchen spice rack")] * 2}
        parsed = parse_meta_snapshot(snapshot, request, 200)
        self.assertEqual((parsed["state"], [row["ad_id"] for row in parsed["records"]]), ("usable", ["9001"]))
        self.assertEqual(parse_meta_snapshot(dict(snapshot, url=prepare_meta_query("x")["url"]), request, 200)["state"],
                         "blocked")
        # Older saved pages keep their first-image rule.
        from product_radar.meta_browser import card_outcome
        pictured = dict(card("9002", "Kitchen spice rack"), images=[{"url": "https://images.example.org/a.png"}, {"url": "http://x"}])
        legacy = card_outcome({"cards": [pictured]}, 30, legacy_images=True)
        self.assertEqual(legacy["records"][0]["presentation"]["original_image_url"], "https://images.example.org/a.png")
        self.assertNotIn("original_image_url", card_outcome({"cards": [dict(pictured, images=[])]}, 30,
                                                            legacy_images=True)["records"][0]["presentation"])


class SharedReadTests(Case):
    """The claim rules of a shared source acquisition, as the lane uses them."""

    def claim(self, owner, at=AT):
        route, request, options = access.physical_request(self.memory.snapshot(), {
            "lane": "meta-advertisers", "route": "meta-ads", "request": {"advertiser_id": "200"}})
        return access.open_read(self.memory, route, request, options, owner_id=owner, as_of=at,
                                root=self.memory.root / "wider_discovery")

    def test_another_owners_unfinished_read_is_left_alone(self):
        first = self.claim("someone-else")  # another owner's reservation, never sent
        self.assertEqual(first["action"], "read")
        self.assertEqual(self.claim("wider-owner"), {"action": "skip", "acquisition_id": first["acquisition_id"],
                                                     "reason": "SOURCE_ORIGINAL_INCOMPLETE"})

    def test_reuse_and_refresh_after_a_complete_read(self):
        self.review("200")
        self.run_lane(self.config(meta=1, meta_advertisers={"recheck_days": 1}),
                      {"meta": MetaBrowser([card("9001", "Kitchen spice rack")])})
        first = self.reads()[0]
        # Read again after one recheck day: the saved complete read is under a week old.
        reused = self.run_lane(self.config(meta=1, meta_advertisers={"recheck_days": 1}), {"meta": None}, at=later(AT, 2 * 86400))
        self.assertEqual([(row["state"], row["reason"]) for row in reused["reads"]], [("empty", "SOURCE_RECENTLY_READ")])
        browser = MetaBrowser([card("9005", "Kitchen spice rack two")])
        refreshed = self.run_lane(self.config(meta=1, meta_advertisers={"recheck_days": 1}), {"meta": browser},
                                  at=later(AT, 9 * 86400))
        self.assertEqual((len(browser.calls), refreshed["reads"][0]["state"]), (1, "read"))
        second = self.reads()[-1]
        self.assertNotEqual(second["acquisition_id"], first["acquisition_id"])
        self.assertEqual(self.owner(second)["binding"]["previous_acquisition_id"], first["acquisition_id"])


class SiteWaitTests(Case):
    """A due site wait is probed by these reads too; refused keys are tried again in limited generations."""

    def wait(self, at):
        return source_access_wait(self.memory.snapshot(), META_SCOPE, as_of=at)

    def test_a_due_wait_on_its_own_refused_read_is_probed_by_a_new_generation(self):
        self.review("200")
        self.run_lane(self.config(meta=1), {"meta": MetaBrowser([], status=429)})
        first = self.reads()[0]
        self.assertEqual(self.wait(later(AT, 60))["phase"], "waiting")
        # After the wait and the retry hours: the same request again, as the site's probe; the wait clears.
        browser = MetaBrowser([card("9001", "Kitchen spice rack")], at=later(AT, 25 * 3600))
        summary = self.run_lane(self.config(meta=1), {"meta": browser}, at=later(AT, 25 * 3600))
        self.assertEqual((len(browser.calls), summary["reads"][0]["state"]), (1, "read"))
        second = self.reads()[-1]
        self.assertEqual(self.owner(second)["binding"]["previous_acquisition_id"], first["acquisition_id"])
        self.assertEqual(self.owner(second)["binding"]["capture_options"], access.META_PURPOSE)
        self.assertIsNone(self.wait(later(AT, 25 * 3600 + 60)))

    def test_a_refusal_cleared_by_another_read_allows_a_limited_new_generation(self):
        self.review("200")
        self.review("201")
        self.run_lane(self.config(meta=1), {"meta": MetaBrowser([], status=429)})
        refused = self.reads()[0]
        # Another key's read, once the wait is due, is the probe; the site answers and the wait clears.
        probe = self.run_lane(self.config(meta=1), {"meta": MetaBrowser([card("9001", "Kitchen spice rack")], at=later(AT, 600))},
                              at=later(AT, 600))
        self.assertEqual((probe["reads"][0]["key"], probe["reads"][0]["state"]), ("201", "read"))
        self.assertIsNone(self.wait(later(AT, 660)))
        # The refused key's original stays refused: a new generation of its request is read.
        browser = MetaBrowser([card("9002", "Kitchen knife holder")], at=later(AT, 25 * 3600))
        again = self.run_lane(self.config(meta=1), {"meta": browser}, at=later(AT, 25 * 3600))
        self.assertEqual((again["reads"][0]["key"], again["reads"][0]["state"], len(browser.calls)), ("200", "read", 1))
        renewed = self.reads()[-1]
        self.assertEqual((renewed["retry"], renewed["retry_of"]), (1, refused["acquisition_id"]))
        self.assertEqual(self.owner(renewed)["binding"]["capture_options"], dict(access.META_PURPOSE, retry=1))
        # At most two new generations per key and recheck window.
        target = {"lane": "meta-advertisers", "key": "200"}
        self.assertEqual(wider._generation(self.memory, target, later(AT, 26 * 3600), self.config()), (1, True))
        for n in (2, 3):
            row = dict(renewed, id="wider-discovery-read-renewed-" + str(n), retry=n)
            self.memory.put("batch_runs", row["id"], row)
        self.assertEqual(wider._generation(self.memory, target, later(AT, 26 * 3600), self.config()), (3, False))
        self.assertEqual(wider._generation(self.memory, target, later(AT, 32 * 86400), self.config())[1], True)

    def test_a_cleared_refusal_beyond_the_limit_is_left_alone(self):
        self.review("200")
        self.review("201")
        self.run_lane(self.config(meta=1), {"meta": MetaBrowser([], status=429)})
        self.run_lane(self.config(meta=1), {"meta": MetaBrowser([card("9001", "Kitchen spice rack")], at=later(AT, 600))},
                      at=later(AT, 600))
        with patch.object(wider, "MAX_NEW_GENERATIONS", 0):
            held = self.run_lane(self.config(meta=1), {"meta": MetaBrowser([])}, at=later(AT, 25 * 3600))
        self.assertEqual([(row["state"], row["reason"]) for row in held["reads"]], [("failed", "SOURCE_REFUSAL_CLEARED")])

    def test_unknown_or_unfinished_originals_park_the_key_until_its_recheck_days(self):
        config = self.config()
        for row in ({"state": "uncertain", "updated_at": AT, "reason": "SOURCE_ORIGINAL_RECEIPT_PENDING"},
                    {"state": "failed", "updated_at": AT, "reason": "SOURCE_ORIGINAL_INCOMPLETE"}):
            self.assertFalse(candidates.due(row, later(AT, 2 * 86400), config, 7))
            self.assertTrue(candidates.due(row, later(AT, 8 * 86400), config, 7))
            self.assertFalse(candidates.due(row, later(AT, 800 * 86400), config, None))
        refused = {"state": "refused", "updated_at": AT, "reason": "x"}
        self.assertTrue(candidates.due(refused, later(AT, 25 * 3600), config, 7))
        # A probe already admitted by another read is a wait.
        with patch("product_radar.source_coverage.source_access_wait",
                   return_value={"phase": "probe_pending", "reason": "SOURCE_ACCESS_PROBE_PENDING", "next_retry_at": None}):
            self.review("200")
            waits = self.run_lane(self.config(meta=1), {"meta": MetaBrowser([])})
        self.assertEqual(waits["waits"][0]["reason"], "SOURCE_ACCESS_PROBE_PENDING")


class RisingTests(Case):
    def test_rising_queries_become_one_meta_search_each_in_category_and_not_already_known(self):
        catalogue = load_catalogue()
        known = next(phrase["text"] for family in catalogue["families"] for phrase in family["phrases"])
        self.save_rising([("gold earring set", "Breakout"), (known, "Breakout"), ("steel spice rack organiser", "+250%"),
                          ("wooden spice rack for kitchen", "Breakout"), ("Synthetic draining caddy 000", "+900%"),
                          (known + " 1 litre", "Breakout")])  # covers a known phrase: a variant of it
        self.save_rising([("travel neck pillow", "Breakout")], observed=later(AT, -30 * 86400), name="old-read")
        self.save_rising([], name="empty-read", state="none")
        (self.memory.root / "direct_trends" / "broken-read").mkdir()
        (self.memory.root / "direct_trends" / "broken-read" / "rising_queries.json").write_text("{broken")
        config = self.config(rising=1)
        with self.official():
            targets = candidates.rising_targets(self.memory.root, self.memory.snapshot(), AT, config, {}, catalogue)
        self.assertEqual([row["request"]["query"] for row in targets], ["wooden spice rack for kitchen"])
        with self.official():
            two = candidates.rising_targets(self.memory.root, self.memory.snapshot(), AT, self.config(rising=2), {}, catalogue)
        self.assertEqual([row["request"]["query"] for row in two], ["wooden spice rack for kitchen", "steel spice rack organiser"])
        # A list its own read no longer holds is never used.
        with patch("product_radar.trends_rising.rising_queries", return_value=None):
            self.assertEqual(candidates.rising_targets(self.memory.root, self.memory.snapshot(), AT, config, {}, catalogue), [])
        with patch("product_radar.trends_rising.rising_queries", side_effect=SafetyError("DIRECT_TRENDS_RECEIPT_INVALID")):
            self.assertEqual(candidates.rising_targets(self.memory.root, self.memory.snapshot(), AT, config, {}, catalogue), [])
        self.assertEqual(candidates.rising_targets(self.memory.root, self.memory.snapshot(), AT, self.config(), {}, catalogue), [])
        browser = MetaBrowser([card(str(9100 + n), "Wooden spice rack " + str(n)) for n in range(12)])
        with self.official():
            summary = self.run_lane(config, {"meta": browser}, catalogue=catalogue)
        self.assertEqual([call["url"] for call in browser.calls], [prepare_meta_query("wooden spice rack for kitchen")["url"]])
        self.assertEqual(summary["reads"][0]["candidates"], 10)  # cards_per_read
        record = self.reads()[0]
        self.assertEqual((self.response(record)["bridge_contract"], self.response(record)["complete"]),
                         ("verified-meta-response-v1", True))
        with self.official():
            next_targets = candidates.rising_targets(self.memory.root, self.memory.snapshot(), later(AT, 60), config,
                                                     candidates.latest_reads(self.reads()), catalogue)
        self.assertEqual([row["request"]["query"] for row in next_targets], ["steel spice rack organiser"])

    def test_saved_lists_are_read_again_only_when_they_change(self):
        self.save_rising([("wooden spice rack for kitchen", "Breakout")])
        rows = candidates.rising_rows(self.memory.root, AT, 14)
        self.assertEqual([row[1]["queries"][0]["query"] for row in rows], ["wooden spice rack for kitchen"])
        self.assertTrue((self.memory.root / "wider_discovery" / candidates.RISING_INDEX).is_file())
        with patch.object(candidates, "_rising_record", side_effect=AssertionError("not read again")):
            self.assertEqual(len(candidates.rising_rows(self.memory.root, AT, 14)), 1)
        self.save_rising([("steel spice rack organiser", "Breakout"), ("wooden spice rack for kitchen", "+50%")])
        with patch.object(candidates, "_rising_record", wraps=candidates._rising_record) as reread:
            rows = candidates.rising_rows(self.memory.root, AT, 14)
        self.assertEqual(reread.call_count, 1)
        self.assertEqual(rows[0][1]["queries"][0]["query"], "steel spice rack organiser")
        # An unreadable or unwritable index is only slower.
        (self.memory.root / "wider_discovery" / candidates.RISING_INDEX).write_text("{broken")
        self.assertEqual(len(candidates.rising_rows(self.memory.root, AT, 14)), 1)
        (self.memory.root / "wider_discovery" / candidates.RISING_INDEX).unlink()
        with patch.object(candidates, "atomic_json", side_effect=OSError("disk")):
            self.assertEqual(len(candidates.rising_rows(self.memory.root, AT, 14)), 1)
        self.assertEqual(candidates.rising_rows(self.project / "nowhere", AT, 14), [])

    def test_without_the_ai_no_new_read_starts(self):
        self.save_rising([("wooden spice rack for kitchen", "Breakout")])
        browser = MetaBrowser([card("9100", "Wooden spice rack")])
        with self.official():
            summary = self.run_lane(self.config(rising=1), {"meta": browser}, runner=None)
        self.assertEqual((summary["reads"], browser.calls), ([], []))
        self.assertEqual(summary["waits"], [{"reason": "WIDER_DISCOVERY_MODEL_WAITING", "next_retry_at": later(AT, 300),
                                             "lane": None, "route": None}])


class SupplierTests(Case):
    def test_terms_rotate_across_categories_and_wait_for_their_recheck(self):
        config = self.config(indiamart=2, tradeindia=1)
        first = candidates.supplier_targets(self.memory.snapshot(), AT, config, {})
        self.assertEqual([(row["route"], row["request"]["term"]) for row in first],
                         [("indiamart", "home decor item"), ("indiamart", "baby care product"), ("tradeindia", "home decor item")])
        latest = {("supplier-listings", row["key"]): {"state": "read", "updated_at": AT} for row in first}
        second = candidates.supplier_targets(self.memory.snapshot(), later(AT, 60), config, latest)
        self.assertEqual([(row["route"], row["request"]["term"]) for row in second],
                         [("indiamart", "kitchen organiser"), ("indiamart", "travel accessory"), ("tradeindia", "baby care product")])
        # Terms never read come first; once all are read, the one read longest ago, after its recheck days.
        latest.update({("supplier-listings", row["key"]): {"state": "read", "updated_at": later(AT, 60)} for row in second})
        third = candidates.supplier_targets(self.memory.snapshot(), later(AT, 120), self.config(indiamart=2), latest)
        self.assertEqual([row["request"]["term"] for row in third], ["wall hanging", "baby feeding bottle"])
        terms = [term for rows in candidates.settings()["supplier_listings"]["terms"].values() for term in rows]
        everything = {("supplier-listings", "indiamart:" + term): {"state": "read", "updated_at": later(AT, n)}
                      for n, term in enumerate(reversed(terms))}
        only = self.config(indiamart=2)
        self.assertEqual(candidates.supplier_targets(self.memory.snapshot(), later(AT, 86400), only, everything), [])
        again = candidates.supplier_targets(self.memory.snapshot(), later(AT, 8 * 86400), only, everything)
        self.assertEqual([row["request"]["term"] for row in again], [terms[-1], terms[-2]])

    def signed_in(self):
        return patch("product_radar.indiamart_signed_in.signed_in_hold", return_value=None)

    def indiamart(self, html, *, calls=None):
        def read(item, *, folder, root):
            (calls if calls is not None else []).append({"item": item, "root": root})
            return dict(supplier_receipt(item, html), profile="signed-in")
        return read

    def test_indiamart_listings_are_read_signed_in_paced_and_only_new_category_products_become_candidates(self):
        catalogue = load_catalogue()
        known = next(phrase["text"] for family in catalogue["families"] for phrase in family["phrases"]
                     if candidates.gate(phrase["text"]))
        html = indiamart_html([(known.title() + " Premium", "https://www.indiamart.com/proddetail/known-2.html"),
                               ("Gold Earring Set", "https://www.indiamart.com/proddetail/earring-3.html"),
                               ("Magnetic Kitchen Knife Strip", "https://www.indiamart.com/proddetail/knife-strip-1.html"),
                               ("Magnetic Kitchen Knife Strip", "https://www.indiamart.com/proddetail/knife-strip-4.html"),
                               ("Collapsible Silicone Kitchen Funnel", "https://www.indiamart.com/proddetail/funnel-0.html"),
                               ("Bamboo Kitchen Spoon Stand", "https://www.indiamart.com/proddetail/spoon-5.html")])
        calls = []
        with self.signed_in():
            summary = self.run_lane(self.config(indiamart=1, supplier_listings={"listings_per_read": 2}),
                                    {"indiamart": self.indiamart(html, calls=calls)}, catalogue=catalogue)
        self.assertEqual([call["item"]["url"] for call in calls], [supplier_route_url("indiamart", "home decor item")])
        self.assertEqual(calls[0]["root"], self.project)
        self.assertEqual(summary["reads"][0]["candidates"], 2)  # listings_per_read
        # Admitted end to end through the existing candidate flow (the reviewer's probe).
        self.assertEqual(summary["admission"]["pending_reads"], 1)
        self.assertTrue(summary["new_concept_ids"])
        state = self.memory.snapshot()
        rows = sorted((row for row in state["observations"].values() if row["source"] == "indiamart"),
                      key=lambda row: row["facts"]["url"], reverse=True)
        self.assertEqual([row["facts"]["url"] for row in rows], ["https://www.indiamart.com/proddetail/knife-strip-1.html",
                                                                 "https://www.indiamart.com/proddetail/funnel-0.html"])
        self.assertEqual((rows[0]["facts"]["list_type"], rows[0]["facts"]["supplier_name"], rows[0]["semantic"]["platform"]),
                         ("supplier_listing_discovery", "Synthetic Works", "IndiaMART"))
        self.assertEqual(rows[0]["facts"]["limitations"], [wider.LISTING_LIMITATION])
        # The supplier lane's own request, page and response bridge: one read for both lanes.
        record = self.reads()[0]
        self.assertEqual(self.owner(record)["binding"]["request"]["transport"], "signed-in-browser")
        self.assertEqual(self.response(record)["bridge_contract"], "verified-supplier-search-response-v1")
        from product_radar.signed_in_pacing import _records
        self.assertEqual(len(_records(self.project)["reads"]["indiamart"]), 1)
        # The next term waits for the 30-second pacing: a calm wait, never sent, its claim kept for the next read.
        with self.signed_in():
            paced = self.run_lane(self.config(indiamart=1), {"indiamart": self.indiamart(html)}, at=later(AT, 1))
        self.assertEqual((paced["reads"][0]["state"], paced["reads"][0]["reason"]), ("not_started", "SIGNED_IN_READ_SPACING"))
        self.assertEqual(self.owner(self.reads()[-1])["state"], "reserved")

    def test_indiamart_yields_to_the_type_supplier_searches(self):
        with self.signed_in():
            for context in ({"discovery_position": "before"}, {"discovery_position": "after_prerequisites"},
                            {"priority_concept_ids": {"suppliers": [self.ids[0]]}}, {"discovery_position": None}):
                with self.subTest(context=context):
                    summary = self.run_lane(self.config(indiamart=1), {"indiamart": self.indiamart("")}, context=context)
                    self.assertEqual((summary["reads"], summary["waits"][0]["reason"]),
                                     ([], "WIDER_DISCOVERY_SUPPLIERS_FIRST"))
        # TradeIndia is not part of that rule.
        empty = lambda item: supplier_receipt(item, "<p>No search results.</p>")
        summary = self.run_lane(self.config(tradeindia=1), {"tradeindia": empty}, context={"discovery_position": "before"})
        self.assertEqual(summary["reads"][0]["state"], "empty")

    def test_indiamart_waits_for_the_owner_and_a_read_that_never_started_is_given_back(self):
        summary = self.run_lane(self.config(indiamart=1), {"indiamart": self.indiamart("")})
        self.assertEqual((summary["reads"], summary["waits"][0]["reason"]), ([], "OWNER_SIGN_IN_REQUIRED"))

        def never(item, *, folder, root):
            raise SignedInWait("OWNER_DESKTOP_UNAVAILABLE", next_retry_at=later(AT, 600))
        with self.signed_in():
            summary = self.run_lane(self.config(indiamart=1), {"indiamart": never})
        self.assertEqual((summary["reads"][0]["state"], summary["reads"][0]["reason"]), ("not_started", "OWNER_DESKTOP_UNAVAILABLE"))
        from product_radar.signed_in_pacing import _records
        self.assertEqual(_records(self.project)["reads"].get("indiamart", []), [])
        first = self.reads()[0]
        self.assertEqual((first["next_retry_at"], self.owner(first)["state"]), (later(AT, 600), "reserved"))
        # The next read uses the same reserved acquisition.
        html = indiamart_html([("Magnetic Kitchen Knife Strip", "https://www.indiamart.com/proddetail/knife-strip-1.html")])
        with self.signed_in():
            self.run_lane(self.config(indiamart=1), {"indiamart": self.indiamart(html)}, at=later(AT, 60))
        self.assertEqual((self.reads()[-1]["acquisition_id"], self.reads()[-1]["state"]), (first["acquisition_id"], "read"))
        # A sign-in wall in the signed-in profile is an expired sign-in, not a site refusal.
        from product_radar.signed_in_pacing import _path
        _path(self.project).unlink()  # the pacing record keeps real times; this read is a later one
        wall = (ROOT / "tests/fixtures/supplier_routes/indiamart-search-login-wall-rendered-2026-10-14.html").read_text(encoding="utf-8")
        with self.signed_in():
            walled = self.run_lane(self.config(indiamart=1), {"indiamart": self.indiamart(wall)}, at=later(AT, 120))
        self.assertEqual((walled["reads"][0]["state"], walled["reads"][0]["reason"]), ("failed", "OWNER_SIGN_IN_REQUIRED"))

    def test_an_interrupted_reopening_is_finished_by_the_next_claim(self):
        def never(item, *, folder, root):
            raise SignedInWait("OWNER_DESKTOP_UNAVAILABLE", next_retry_at=later(AT, 600))
        with self.signed_in(), patch("product_radar.supplier_search._reopen_not_started", side_effect=OSError("disk")):
            summary = self.run_lane(self.config(indiamart=1), {"indiamart": never})
        self.assertEqual(summary["reads"][0]["state"], "not_started")
        first = self.reads()[0]
        self.assertEqual(self.owner(first)["state"], "dispatching")  # never sent, its reopening still to finish
        html = indiamart_html([("Magnetic Kitchen Knife Strip", "https://www.indiamart.com/proddetail/knife-strip-1.html")])
        with self.signed_in():
            again = self.run_lane(self.config(indiamart=1), {"indiamart": self.indiamart(html)}, at=later(AT, 60))
        self.assertEqual((again["reads"][0]["state"], self.reads()[-1]["acquisition_id"]), ("read", first["acquisition_id"]))

    def test_tradeindia_reads_plainly_then_in_the_browser_next_round_reusing_the_plain_read(self):
        plain, browser = [], []

        def http(item):
            plain.append(item)
            return supplier_receipt(item, "<main><div class='react-loading-skeleton'>Loading products for you here and there.</div></main>")

        def native(item, *, folder):
            browser.append(item)
            return supplier_receipt(item, '<a href="https://www.tradeindia.com/products/baby-bath-seat-1.html">'
                                          '<h3>Wooden Baby Bath Seat Ring</h3></a>'
                                          '<a href="https://www.tradeindia.com/seller-x/">Seller X</a>')
        readers = {"tradeindia": http, "tradeindia-browser": native}
        turn = open_work_turn(self.memory, "programme", at=AT, policy=dict(deepcopy(DEFAULT_POLICY), exploration_slots=0))
        with bind_work_turn(self.memory, turn["id"]):
            first = self.run_lane(self.config(tradeindia=1), readers)
        self.assertEqual((len(plain), len(browser)), (1, 0))
        self.assertEqual((first["reads"][0]["state"], first["reads"][0]["reason"]),
                         ("not_started", "WIDER_DISCOVERY_BROWSER_READ_NEXT"))
        self.assertEqual(self.memory.snapshot()["batch_runs"][turn["id"]]["counters"]["source_callbacks"], 1)
        second = self.run_lane(self.config(tradeindia=1), readers, at=later(AT, 60))
        self.assertEqual((len(plain), len(browser)), (1, 1))
        self.assertEqual(second["reads"][0]["candidates"], 1)
        self.assertEqual(self.owner(self.reads()[-1])["binding"]["request"]["transport"], "public-browser")
        rows = [row for row in self.memory.snapshot()["observations"].values() if row["source"] == "tradeindia"]
        self.assertEqual([row["semantic"]["title"] for row in rows], ["Wooden Baby Bath Seat Ring"])

    def test_a_type_supplier_search_replays_a_wider_read_of_the_same_words(self):
        """One read serves both lanes: the page is saved where the supplier lane replays its own (reviewer probe)."""
        from product_radar.supplier_search import create_shared_supplier_search_handlers
        empty = lambda item: supplier_receipt(item, "<p>No search results.</p>")
        read = self.run_lane(self.config(tradeindia=1), {"tradeindia": empty})["reads"][0]
        record = self.reads()[0]
        self.assertEqual(Path(record["folder"]).parent, self.memory.root / "supplier_routes" / "shared")
        self.assertTrue((Path(record["folder"]) / "page.json").is_file())
        self.assertEqual(Path(record["nomination_folder"]).parent, self.memory.root / "wider_discovery")
        handlers = create_shared_supplier_search_handlers(self.memory, http_reader=empty, signed_in_root=self.project)
        observed = self.response(record)["observed_at"]
        resolution = handlers["tradeindia"]["resolve_source"]({
            "route": "tradeindia", "request": {"query": read["key"].split(":", 1)[1], "country": "IN"},
            "task_id": "probe-task", "acquisition_id": "probe-acquisition", "as_of": later(observed, 3600)})
        self.assertEqual((resolution["action"], resolution["acquisition_id"]), ("saved", record["acquisition_id"]))

    def test_a_tradeindia_refusal_holds_the_supplier_site_for_every_lane(self):
        refusing = lambda item: supplier_receipt(item, status=403)
        result = self.run_lane(self.config(tradeindia=1), {"tradeindia": refusing})
        self.assertEqual(result["reads"][0]["state"], "refused")
        scope = access.access_scope("tradeindia", self.owner(self.reads()[0])["binding"]["request"])
        self.assertEqual(scope["access_identity"], "owned-supplier-public")
        self.assertIsNotNone(source_access_wait(self.memory.snapshot(), scope, as_of=later(AT, 60)))
        held = self.run_lane(self.config(tradeindia=1, retry_after_hours=1), {"tradeindia": refusing}, at=later(AT, 120))
        self.assertEqual((held["reads"], held["waits"][0]["reason"]), ([], "SOURCE_ACCESS_WAIT"))


class RoundTests(Case):
    def test_reads_reserve_the_rounds_source_reads_and_stop_at_the_limit(self):
        self.review("200")
        self.review("201")
        turn = open_work_turn(self.memory, "programme", at=AT,
                              policy=dict(deepcopy(DEFAULT_POLICY), source_callbacks=1, exploration_slots=0))
        browser = MetaBrowser([])
        with bind_work_turn(self.memory, turn["id"]):
            summary = self.run_lane(self.config(meta=2), {"meta": browser})
        self.assertEqual((len(browser.calls), len(summary["reads"])), (1, 1))
        self.assertEqual(summary["waits"][-1]["reason"], "WORK_TURN_SOURCE_LIMIT")
        self.assertEqual(self.memory.snapshot()["batch_runs"][turn["id"]]["counters"]["source_callbacks"], 1)
        self.assertEqual(self.reads()[0]["state"], "empty")
        # The advertiser left over keeps its claim and is read in the next round.
        second = self.run_lane(self.config(meta=2), {"meta": browser}, at=later(AT, 60))
        self.assertEqual((len(browser.calls), [row["key"] for row in second["reads"]]), (2, ["201"]))
        with patch("product_radar.programme.programme_service_state", return_value={"enabled": False}):
            paused = self.run_lane(self.config(meta=2, meta_advertisers={"recheck_days": 1}), {"meta": browser},
                                   at=later(AT, 9 * 86400))
        self.assertEqual(paused["waits"][-1]["reason"], "PROGRAMME_OWNER_PAUSED")

    def test_lanes_take_turns_going_first_with_the_turns(self):
        self.review("200")
        self.save_rising([("wooden spice rack for kitchen", "Breakout")])
        orders = []
        for sequence in (1, 2):
            with patch("product_radar.work_turns.active_work_turn", return_value={"programme_turn_sequence": sequence}), \
                    patch.object(wider, "_read", side_effect=lambda memory, target, **_: orders.append(target["lane"]) or {}), \
                    patch.object(wider, "_target_wait", return_value=None), self.official():
                self.run_lane(self.config(meta=1, rising=1))
        self.assertEqual(orders, ["trends-rising", "meta-advertisers", "meta-advertisers", "trends-rising"])

    def test_holds_for_pending_admission_and_the_daily_ceiling(self):
        self.review("200")
        self.review("201")
        browser = MetaBrowser([card("9001", "Kitchen spice rack")])
        with patch("product_radar.research_flow.run_flow"):
            self.run_lane(self.config(meta=1), {"meta": browser})
            config = self.config(meta=1, admission={"max_pending_reads": 1})
            held = self.run_lane(config, {"meta": browser}, at=later(AT, 60))
        self.assertEqual(held["waits"], [{"lane": None, "route": None, "reason": "WIDER_DISCOVERY_ADMISSION_PENDING",
                                          "next_retry_at": None}])
        ceiling = self.config(meta=1, admission={"daily_reads": 1})
        state = self.memory.snapshot()
        hold = wider._round_hold(state, self.reads(), later(AT, 60), ceiling, FakeRunner())
        self.assertEqual(hold, {"reason": "WIDER_DISCOVERY_DAILY_READS_USED", "next_retry_at": later(AT, 86400)})
        # Old candidates the AI never described stay pending (never given up on unjudged).
        self.assertEqual(len(wider._pending(state, later(AT, 4 * 86400), config)), 1)
        self.assertEqual(wider._admit(self.memory, None, AT, config, 10)["reason"], "WIDER_DISCOVERY_MODEL_WAITING")

    def test_unfinished_reads_are_completed_from_saved_pages_or_recorded_never_sent_or_uncertain(self):
        self.review("200")
        browser = MetaBrowser([card("9001", "Kitchen spice rack")])
        with patch.object(wider, "_finish", side_effect=RuntimeError("process ended")):
            with self.assertRaises(RuntimeError):
                self.run_lane(self.config(meta=1), {"meta": browser})
        self.assertEqual(self.reads()[0]["state"], "intent_saved")
        recovered = self.run_lane(self.config(), {"meta": None}, at=later(AT, 60))
        self.assertEqual((recovered["recovered"][0]["state"], recovered["recovered"][0]["candidates"]), ("read", 1))
        self.assertEqual(len(browser.calls), 1)
        # No saved result: the acquisition's execution is unknown and it is not sent again soon.
        self.review("201")
        with patch.object(wider, "read", side_effect=RuntimeError("process ended")):
            with self.assertRaises(RuntimeError):
                self.run_lane(self.config(meta=1), {"meta": browser}, at=later(AT, 120))
        lost = self.run_lane(self.config(), {"meta": None}, at=later(AT, 180))
        self.assertEqual((lost["recovered"][0]["state"], lost["recovered"][0]["reason"]),
                         ("uncertain", "SOURCE_ORIGINAL_RECEIPT_PENDING"))
        self.assertEqual(self.owner(self.reads()[-1])["state"], "execution_unknown")
        self.assertFalse(candidates.due(candidates.latest_reads(self.reads())[("meta-advertisers", "201")],
                                        later(AT, 3600), self.config(), 30))
        # A crash before the dispatch mark: the claim was never sent.
        with patch("product_radar.source_acquisitions.mark_source_dispatching", side_effect=RuntimeError("process ended")):
            with self.assertRaises(RuntimeError):
                self.run_lane(self.config(meta=1, meta_advertisers={"recheck_days": 1}), {"meta": browser},
                              at=later(AT, 9 * 86400))
        unsent = self.run_lane(self.config(), {"meta": None}, at=later(AT, 9 * 86400 + 60))
        self.assertEqual([row["state"] for row in unsent["recovered"]], ["not_started"])

    def test_one_reads_failure_is_its_own_and_the_lane_moves_on(self):
        self.review("200")
        self.save_rising([("wooden spice rack for kitchen", "Breakout")])
        # A read whose page cannot be closed fails alone; the other lane's read still runs.
        with self.official(), patch.object(wider, "close", side_effect=ValueError("broken page")):
            summary = self.run_lane(self.config(meta=1, rising=1), {"meta": MetaBrowser([card("9001", "Kitchen rack")])})
        self.assertEqual([(row["state"], row["reason"]) for row in summary["reads"]],
                         [("failed", "WIDER_DISCOVERY_LOCAL_FAILURE")] * 2)
        # A target that cannot be claimed is a wait.
        with patch.object(wider, "open_read", side_effect=SafetyError("SOURCE_ACQUISITION_CONFLICT")), \
                patch.object(wider, "_target_wait", return_value=None):
            waits = self.run_lane(self.config(meta=1), {}, at=later(AT, 2 * 86400))
        self.assertEqual(waits["waits"][0]["reason"], "SOURCE_ACQUISITION_CONFLICT")
        # A lane whose planning fails is skipped; integrity failures stop the round.
        with patch.object(wider, "advertiser_targets", side_effect=KeyError("x")):
            self.assertEqual(self.run_lane(self.config(meta=1), {})["waits"][0]["reason"], "WIDER_DISCOVERY_LOCAL_FAILURE")
        with patch.object(wider, "advertiser_targets", side_effect=SafetyError("RESEARCH_EVENTS_INVALID")):
            with self.assertRaisesRegex(SafetyError, "RESEARCH_EVENTS_INVALID"):
                self.run_lane(self.config(meta=1), {})
        with patch.object(wider, "open_read", side_effect=SafetyError("RESEARCH_EVENTS_INVALID")), \
                patch.object(wider, "_target_wait", return_value=None):
            with self.assertRaisesRegex(SafetyError, "RESEARCH_EVENTS_INVALID"):
                self.run_lane(self.config(meta=1), {}, at=later(AT, 2 * 86400))
        # A record that cannot be recovered is reported, and the round goes on.
        record = dict(self.reads()[0], id="wider-discovery-read-broken", state="intent_saved")
        self.memory.put("batch_runs", record["id"], record)
        with patch.object(wider, "_outcome", side_effect=TypeError("bad")):
            broken = self.run_lane(self.config(), {}, at=later(AT, 3 * 86400))
        self.assertEqual(broken["waits"][0]["reason"], "WIDER_DISCOVERY_LOCAL_FAILURE")
        # Admission failures are the admission's own.
        pending = [{"acquisition_id": "x", "lane": "trends-rising", "nomination_folder": str(self.project)}]
        with patch("product_radar.research_flow.run_flow", side_effect=SafetyError("RUNNER_ALREADY_ACTIVE")), \
                patch.object(wider, "_pending", return_value=pending):
            failing = self.run_lane(self.config(), {}, at=later(AT, 4 * 86400))
        self.assertEqual(failing["admission"]["reason"], "RUNNER_ALREADY_ACTIVE")
        with patch("product_radar.research_flow.run_flow", side_effect=SafetyError("RESEARCH_EVENTS_INVALID")), \
                patch.object(wider, "_pending", return_value=pending):
            with self.assertRaisesRegex(SafetyError, "RESEARCH_EVENTS_INVALID"):
                self.run_lane(self.config(), {}, at=later(AT, 4 * 86400))

    def test_a_changed_saved_nomination_fails_that_read_only(self):
        self.review("200")
        browser = MetaBrowser([card("9001", "Kitchen spice rack")])
        original = wider._nominate

        def changed(memory, record, outcome, facts, config):
            atomic_json(Path(record["nomination_folder"]) / "nomination.json", {"version": "other"})
            return original(memory, record, outcome, facts, config)
        with patch.object(wider, "_nominate", side_effect=changed):
            summary = self.run_lane(self.config(meta=1), {"meta": browser})
        self.assertEqual((summary["reads"][0]["state"], summary["reads"][0]["reason"]),
                         ("failed", "WIDER_DISCOVERY_NOMINATION_CONFLICT"))

    def test_a_paused_programme_reserves_no_read(self):
        with patch("product_radar.programme.programme_service_state", return_value={"enabled": False}):
            with self.assertRaisesRegex(SafetyError, "PROGRAMME_OWNER_PAUSED"):
                wider._reserve(self.memory, {"id": "wider-discovery-read-x"}, AT)

    def test_a_described_item_with_an_undecided_product_is_still_being_admitted(self):
        from product_radar.discovery_admission import unsettled
        state = {"observations": {"o": {"acquisition_id": "a", "semantic_id": "s"}},
                 "descriptions": {"s": {"result": {"products": [{"label": "x"}]}}}, "associations": {}, "problems": {}}
        row = {"acquisition_id": "a", "imported_at": AT}
        self.assertEqual(unsettled(state, [row], AT, 3), [row])
        self.assertEqual(unsettled(state, [row], later(AT, 4 * 86400), 3), [])

    def test_the_installed_step_reads_the_programmes_catalogue(self):
        with patch.object(wider, "advance_wider_discovery", return_value={"new_concept_ids": []}) as lane:
            wider.wider_step(self.memory, {"programme_id": "p"}, {"at": AT, "discovery_catalogue": {"x": 1}})
        lane.assert_called_once_with(self.memory, {"at": AT, "discovery_catalogue": {"x": 1}}, programme_id="p",
                                     catalogue={"x": 1})
        self.assertEqual(set(wider._readers()), {"meta", "indiamart", "tradeindia", "tradeindia-browser"})


class DiscoveryStepTests(Case):
    """The wider lane follows the heading searches, so they keep the round's
    first reads and the AI share held for discovery; its failure is its own."""

    def step(self, wider_step):
        return discovery.create_catalogue_discovery_step(shared_marketplace=True, renewable_waves=True, wider_step=wider_step)

    def test_heading_searches_run_first_and_their_results_are_kept(self):
        order = []

        def waves(*_args, **_options):
            order.append("headings")
            return {"state": "work_remaining", "new_concept_ids": ["a"], "new_products_need_full_source_review": ["a"]}

        def lane(memory, record, context):
            order.append("wider")
            self.assertEqual(context["discovery_catalogue"]["families"], step.discovery_catalogue["families"])
            return {"state": "checked", "new_concept_ids": ["b", "a"]}
        step = self.step(lane)
        with patch.object(discovery, "advance_discovery_waves", side_effect=waves):
            result = step(self.memory, {"programme_id": "p"}, {"at": AT, "handlers": {}})
        self.assertEqual(order, ["headings", "wider"])
        self.assertEqual((result["new_concept_ids"], result["new_products_need_full_source_review"]), (["a", "b"], ["a", "b"]))
        self.assertEqual(result["wider"]["state"], "checked")
        for failure, reason in ((SafetyError("WIDER_DISCOVERY_SETTINGS_INVALID"), "WIDER_DISCOVERY_SETTINGS_INVALID"),
                                (OSError("disk"), "WIDER_DISCOVERY_LOCAL_FAILURE"),
                                (KeyError("x"), "WIDER_DISCOVERY_LOCAL_FAILURE")):
            failing = self.step(lambda *_args, error=failure: (_ for _ in ()).throw(error))
            with patch.object(discovery, "advance_discovery_waves", side_effect=waves):
                result = failing(self.memory, {"programme_id": "p"}, {"at": AT, "handlers": {}})
            self.assertEqual((result["state"], result["new_concept_ids"], result["wider"]["reason"]),
                             ("work_remaining", ["a"], reason))
        invalid = self.step(lambda *_args: {"new_concept_ids": "b"})
        with patch.object(discovery, "advance_discovery_waves", side_effect=waves):
            self.assertEqual(invalid(self.memory, {"programme_id": "p"}, {"at": AT, "handlers": {}})["wider"]["reason"],
                             "WIDER_DISCOVERY_RESULT_INVALID")
        broken = self.step(lambda *_args: (_ for _ in ()).throw(SafetyError("RESEARCH_EVENTS_INVALID")))
        with patch.object(discovery, "advance_discovery_waves", side_effect=waves):
            with self.assertRaisesRegex(SafetyError, "RESEARCH_EVENTS_INVALID"):
                broken(self.memory, {"programme_id": "p"}, {"at": AT, "handlers": {}})

    def test_wider_discovery_goes_first_when_the_programme_gives_it_the_held_share(self):
        order = []
        step = self.step(lambda *_args: order.append("wider") or {"state": "checked", "new_concept_ids": []})
        with patch.object(discovery, "advance_discovery_waves",
                          side_effect=lambda *_a, **_o: order.append("headings") or {"state": "checked", "new_concept_ids": []}):
            step(self.memory, {"programme_id": "p"}, {"at": AT, "handlers": {}, "wider_first": True})
        self.assertEqual(order, ["wider", "headings"])

    def test_a_heading_failure_still_runs_the_wider_lane(self):
        ran = []
        step = self.step(lambda *_args: ran.append("wider") or {"state": "checked", "new_concept_ids": []})
        for first in (False, True):
            ran.clear()
            with patch.object(discovery, "advance_discovery_waves", side_effect=SafetyError("DISCOVERY_SLICE_INVALID")):
                with self.assertRaisesRegex(SafetyError, "DISCOVERY_SLICE_INVALID"):
                    step(self.memory, {"programme_id": "p"}, {"at": AT, "handlers": {}, "wider_first": first})
            self.assertEqual(ran, ["wider"])
        with patch.object(discovery, "advance_discovery_waves", side_effect=SafetyError("RESEARCH_EVENTS_INVALID")):
            ran.clear()
            with self.assertRaisesRegex(SafetyError, "RESEARCH_EVENTS_INVALID"):
                step(self.memory, {"programme_id": "p"}, {"at": AT, "handlers": {}})
        self.assertEqual(ran, [])

    def test_the_wider_lane_needs_the_renewable_waves(self):
        for options in ({"shared_marketplace": True, "renewable_waves": False, "wider_step": wider.wider_step},
                        {"shared_marketplace": True, "renewable_waves": True, "wider_step": "not callable"}):
            with self.assertRaisesRegex(SafetyError, "DISCOVERY_WAVE_OPTION_INVALID"):
                discovery.create_catalogue_discovery_step(**options)

    def test_wider_admission_respects_the_rounds_ai_allowance(self):
        """With the round's AI calls used up, the lane's candidates wait unjudged
        (no AI call, no failure written) and hold further reads."""
        self.review("200")
        policy = dict(deepcopy(DEFAULT_POLICY), model_executions=2, report_slots=0, review_slots=0)
        turn = open_work_turn(self.memory, "programme", at=AT, policy=policy)
        runner = FakeRunner()
        from product_radar.research_ai import Jobs
        with bind_work_turn(self.memory, turn["id"]):
            for n in range(2):
                Jobs(self.memory, runner).run("A", {"items": [{"id": "spent-" + str(n), "source_text": {"title": "Unclear offer"}}]})
            runner.calls.clear()
            summary = self.run_lane(self.config(meta=1, admission={"max_pending_reads": 1}),
                                    {"meta": MetaBrowser([card("9001", "Kitchen spice rack")])}, runner=runner)
        self.assertEqual(runner.calls, [])
        self.assertEqual(summary["reads"][0]["candidates"], 1)
        self.assertEqual(summary["new_concept_ids"], [])
        self.assertFalse([row for row in self.memory.snapshot()["problems"].values() if row["stage"] == "description"])
        self.assertEqual(len(wider._pending(self.memory.snapshot(), AT, self.config())), 1)


class HeldShareRotationTests(unittest.TestCase):
    """Heading searches, the platforms' admission and wider admission each get the
    first use of the AI calls held for discovery within three rounds."""

    def test_each_discovery_lane_has_the_first_use_within_three_rounds(self):
        from test_programme_dependency_boundaries import ProgrammeDependencyBoundaryTests
        harness = ProgrammeDependencyBoundaryTests("test_platform_lane_runs_every_round_and_takes_turns_with_discovery")
        harness.setUp()
        self.addCleanup(harness.doCleanups)
        calls = []

        def platforms(memory, record, context):
            calls.append(("platforms", harness.held()))
            return {"discovery": {"new_concept_ids": []}}

        def lane(memory, record, context):
            calls.append(("wider", harness.held()))
            return {"state": "checked", "new_concept_ids": []}

        def headings(*_args, **_options):
            calls.append(("headings", harness.held()))
            return {"state": "checked", "new_concept_ids": []}
        step = discovery.create_catalogue_discovery_step(shared_marketplace=True, renewable_waves=True, wider_step=lane)
        firsts = []
        with patch.object(discovery, "advance_discovery_waves", side_effect=headings):
            for _ in range(3):
                calls.clear()
                harness.advance(steps=harness.noops(), runner=FakeRunner(), discovery_step=step, platform_step=platforms)
                firsts.append(calls[0][0])
                self.assertEqual(sorted(name for name, _ in calls), ["headings", "platforms", "wider"])
                self.assertEqual(calls[0][1], 0)  # the first lane has the whole held share
        self.assertEqual(sorted(firsts), ["headings", "platforms", "wider"])


class ExampleTests(unittest.TestCase):
    """A supplier directory listing is a lead: other examples come first and it says so."""

    def test_supplier_listings_come_after_other_examples(self):
        rows = {("indiamart", "a", "x"): ((1,), {"source": "indiamart", "id": "lead"}),
                ("meta", "z", "x"): ((2,), {"source": "meta", "id": "ad"}),
                ("deodap", "b", "x"): ((3,), {"source": "deodap", "id": "platform"}),
                ("tradeindia", "c", "x"): ((4,), {"source": "tradeindia", "id": "lead-2"})}
        self.assertEqual([row["id"] for row in examples._ordered(rows)], ["platform", "ad", "lead", "lead-2"])

    def test_reports_and_workbooks_label_a_supplier_listing_as_a_lead(self):
        self.assertEqual(examples.example_source_label({"source": "indiamart"}), "IndiaMART · Supplier listing (lead)")
        self.assertEqual(examples.example_source_label({"source": "tradeindia"}), "TradeIndia · Supplier listing (lead)")
        self.assertEqual(examples.example_source_label({"source": "meta"}), "meta")
        row = {"source": "indiamart", "title": "Magnetic Kitchen Knife Strip", "seller": None, "pack": None, "price": None,
               "observed_at": AT, "url": "https://www.indiamart.com/proddetail/knife-strip-1.html", "child_id": "c",
               "membership_id": "m", "observation_id": "o"}
        product = {"concept_id": "concept-1", "rank": 1, "title": "Knife strip", "example_products": [row],
                   "example_coverage": {"omitted_offers": 0}}
        with patch.object(examples, "validate_functional_examples"):
            self.assertEqual(examples.example_workbook_rows(product)[0][3], "IndiaMART · Supplier listing (lead)")
        from product_radar.report_view import examples_html
        html = examples_html(product, "en")
        self.assertIn("IndiaMART · Supplier listing (lead)", html)
        self.assertIn("Supplier listings (leads)", html)


if __name__ == "__main__":
    unittest.main()
