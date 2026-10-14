"""Free marketplace parser/import proofs use synthetic data, never live calls."""

from copy import deepcopy
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src")); sys.path.insert(0, str(ROOT / "tests"))
from product_radar.discovery import compute_market_signals, latest_observations
from product_radar.market_validation import _listing_pool, review_marketplace
from product_radar.marketplace_browser import (
    SNAPSHOT_JS, build_marketplace_request, capture_marketplace, parse_marketplace_snapshot,
    replay_marketplace, verified_list_observation,
)
from product_radar.models import SafetyError, digest
from product_radar.report_selection import build_report_pool
from product_radar.research_memory import ResearchMemory, import_direct_marketplace
from product_radar.service import policy_for_fit
from product_radar.storage import Journal
from test_fit_pipeline import current_fixture
from test_market_validation import RelevanceRunner
from test_report_selection import NOW


def request(route="amazon-bestsellers", **kwargs):
    return build_marketplace_request(route, **({"department": "kitchen"} if route == "amazon-bestsellers" else {"term": "sink caddy"}), **kwargs)


def snapshot(req, **changes):
    flipkart = req["route"] == "flipkart-search"
    title = "Best Sellers in Home & Kitchen"
    product = "https://www.flipkart.com/sink-caddy/p/itm123456?pid=HOM123456789&lid=tracking" if flipkart else "https://www.amazon.in/Seller-Finish/dp/B012345678?ref_=tracking"
    card = {"card_ref": "card-0", "rank_text": "#1", "title": "Sink caddy pack of 2", "product_url": product,
            "raw_price": "₹1,299.00", "raw_price_truncated": False,
            "image_url": "https://rukminim2.flixcart.com/image/832/832/example.jpeg" if flipkart else "https://m.media-amazon.com/images/I/fixture.jpg"}
    return {"url": req["url"], "title": title, "heading": title, "text": "Public source fixture", "query": req["term"],
            "cards": [card], "card_count": 1, "empty": False, **changes}


def transport(snap, **changes):
    return {"state": "captured", "reason": "Synthetic browser fixture", "navigation_status": 200,
            "navigation_started": True, "navigation_started_at": NOW, "stage": "complete",
            "owned_process_exited": True, "snapshot": snap, **changes}


class MarketplaceParserTests(unittest.TestCase):
    def test_all_three_routes_preserve_actual_identity_quotes_pack_and_photos(self):
        for route in ("amazon-bestsellers", "amazon-search", "flipkart-search"):
            with self.subTest(route=route):
                req = request(route); parsed = parse_marketplace_snapshot(snapshot(req), req)
                self.assertEqual(parsed["state"], "usable")
                row = parsed["records"][0]
                self.assertEqual(row["raw_price"], "₹1,299.00")
                self.assertEqual(row["unit_pack_basis"], "pack of 2")
                self.assertTrue(row["image_url"])
                self.assertNotIn("tracking", row["product_url"])
                self.assertFalse(parsed["sales_proof"])
                if "search" in route:
                    self.assertIsNone(row["position"])
                    self.assertEqual(row["list_type"], "product_search")
                    self.assertEqual(row["limitations"], ["product_search_not_sales_proof"])
                else:
                    self.assertEqual(row["position"], 1)
                    self.assertEqual(row["limitations"], [])

    def test_query_list_and_destination_scope_are_not_guessed(self):
        req = request()
        for change in ({"url": "https://www.amazon.com/gp/bestsellers/kitchen"},
                       {"url": "https://www.amazon.in/gp/bestsellers/baby"},
                       {"url": req["url"] + "?pg=2"}, {"heading": "Other list", "title": "Other list"}):
            self.assertFalse(parse_marketplace_snapshot(snapshot(req, **change), req)["scope_verified"])
        req = request("amazon-search")
        self.assertFalse(parse_marketplace_snapshot(snapshot(req, query="garbage bag"), req)["scope_verified"])
        self.assertFalse(parse_marketplace_snapshot(snapshot(req, url=req["url"] + "&page=2"), req)["scope_verified"])

    def test_scope_handles_only_equivalent_source_owned_list_alias(self):
        req = request()
        for url in ("https://www.amazon.in/zgbs/kitchen", req["url"] + "/ref=zg_bs_nav_1?ref_=fixture"):
            self.assertEqual(parse_marketplace_snapshot(snapshot(req, url=url), req)["state"], "usable")
        self.assertFalse(parse_marketplace_snapshot(snapshot(req, url="https://www.amazon.in/zgbs/baby"), req)["scope_verified"])

    def test_challenge_http_failure_empty_and_unrecognised_cards_are_distinct(self):
        req = request("amazon-search")
        self.assertEqual(parse_marketplace_snapshot(snapshot(req, text="Robot check CAPTCHA"), req)["state"], "blocked")
        self.assertEqual(parse_marketplace_snapshot(snapshot(req), req, navigation_status=429)["state"], "blocked")
        self.assertEqual(parse_marketplace_snapshot(snapshot(req), req, navigation_status=500)["state"], "insufficient")
        self.assertEqual(parse_marketplace_snapshot(snapshot(req, cards=[], card_count=0, empty=True), req)["state"], "empty")
        self.assertEqual(parse_marketplace_snapshot(snapshot(req, cards=[], card_count=0), req)["state"], "insufficient")
        req = request()
        self.assertEqual(parse_marketplace_snapshot(snapshot(req, cards=[], card_count=0, empty=True), req)["state"], "insufficient")
        # Amazon Movers & Shakers is retired (Plan R10 W6): no request, parser or list scope remains.
        with self.assertRaisesRegex(SafetyError, "MARKETPLACE_REQUEST_INVALID"):
            build_marketplace_request("amazon-movers-and-shakers", department="kitchen")
        self.assertFalse(verified_list_observation({"source": "amazon", "facts": {"list_type": "movers-and-shakers"}}))

    def test_missing_fields_or_a_search_rank_do_not_become_list_proof(self):
        req = request(); snap = snapshot(req)
        for change in ({"rank_text": None}, {"product_url": "https://www.amazon.com/dp/B012345678"}, {"title": ""}):
            bad = deepcopy(snap); bad["cards"][0].update(change)
            parsed = parse_marketplace_snapshot(bad, req)
            self.assertEqual(parsed["records"], [])
            self.assertTrue(parsed["rejected"])

    def test_missing_price_invalid_image_and_range_do_not_reject_the_product(self):
        req = request(); snap = snapshot(req)
        snap["cards"][0].update(raw_price=None, image_url="https://127.0.0.1/private.png")
        row = parse_marketplace_snapshot(snap, req)["records"][0]
        self.assertIsNone(row["raw_price"]); self.assertIsNone(row["image_url"])
        snap["cards"][0]["raw_price"] = "₹199–299 / pack"
        self.assertEqual(parse_marketplace_snapshot(snap, req)["records"][0]["raw_price"], "₹199–299 / pack")

    def test_duplicates_conflicts_and_bounded_cards_remain_auditable(self):
        req = request(); snap = snapshot(req)
        snap["cards"].append(dict(snap["cards"][0], card_ref="card-1")); snap["card_count"] = 2
        parsed = parse_marketplace_snapshot(snap, req)
        self.assertEqual(len(parsed["records"]), 1); self.assertEqual(len(parsed["duplicates"]), 1)
        snap["cards"][1]["raw_price"] = "₹300"
        self.assertEqual(parse_marketplace_snapshot(snap, req)["records"], [])
        req = request(limit=1)
        self.assertEqual(parse_marketplace_snapshot(snap, req)["state"], "partial")

    def test_invalid_requests_counts_and_booleans_are_rejected(self):
        for kwargs in ({"limit": True}, {"limit": 0}, {"department": "../account"}):
            with self.assertRaises(SafetyError):
                build_marketplace_request("amazon-bestsellers", department=kwargs.get("department", "kitchen"), limit=kwargs.get("limit", 30))
        for term in (None, "", "a\nprivate", "x" * 161):
            with self.assertRaises(SafetyError): build_marketplace_request("flipkart-search", term=term)
        req = request()
        with self.assertRaises(SafetyError): parse_marketplace_snapshot(snapshot(req, card_count=True), req)

    def test_selectors_keep_card_quote_title_rank_and_image_in_same_root(self):
        for selector in (".zg-bdg-text", ".s-result-item[data-asin]", "div[data-id]", ".a-price:not(.a-text-price) .a-offscreen", ".hZ3P6w", ".Nx9bqj", "image?.currentSrc"):
            self.assertIn(selector, SNAPSHOT_JS)
        # The observed current quote, not the struck-through MRP/discount, is selected.
        self.assertNotIn(".kRYCnD", SNAPSHOT_JS)
        self.assertNotIn(".HQe8jr", SNAPSHOT_JS)

    def test_sponsored_destination_decodes_only_an_observed_amazon_owned_product_link(self):
        from urllib.parse import urlencode
        req = request("amazon-search")
        snap = snapshot(req)
        def parsed_link(destination, **extra):
            snap["cards"][0]["product_url"] = "https://www.amazon.in/sspa/click?" + urlencode({"url": destination, **extra})
            return parse_marketplace_snapshot(snap, req)["records"]
        rows = parsed_link("/fixture/dp/B012345678/ref=sponsored?psc=1")
        self.assertEqual(rows[0]["product_url"], "https://www.amazon.in/dp/B012345678")
        self.assertIsNone(rows[0]["position"])
        self.assertEqual(rows[0]["limitations"], ["product_search_not_sales_proof"])
        for destination in ("https://seller.example/dp/B012345678", "//127.0.0.1/dp/B012345678",
                            "http://www.amazon.in/dp/B012345678", "/account/private", " /dp/B012345678"):
            self.assertEqual(parsed_link(destination), [])
        snap["cards"][0]["product_url"] = "https://www.amazon.in/sspa/click?url=%2Fdp%2FB012345678&url=%2Fdp%2FB098765432"
        self.assertEqual(parse_marketplace_snapshot(snap, req)["records"], [])


class MarketplaceCaptureImportTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.memory = ResearchMemory(self.root / "research", Journal(self.root / "finance"), evidence_type="synthetic")

    def capture(self, req=None, name="fixture-capture", snap=None, **changes):
        req = req or request(); snap = snap or snapshot(req)
        with patch("product_radar.marketplace_browser.utc_now", return_value=NOW):
            return capture_marketplace(req, name, data_root=self.root / "captures", snapshot_reader=lambda request: transport(snap, **changes))

    def test_immutable_capture_replay_and_import_no_paid_ledger(self):
        receipt = self.capture()
        folder = Path(receipt["folder"])
        self.assertEqual(replay_marketplace(folder), receipt)
        with self.memory.journal.locked(): before = self.memory.journal.state()
        first = import_direct_marketplace(self.memory, folder)
        second = import_direct_marketplace(self.memory, folder)
        self.assertEqual(first["new_observations"], 1); self.assertTrue(second["reused_capture"])
        self.assertEqual(first["observation_ids"], second["observation_ids"])
        with self.memory.journal.locked(): self.assertEqual(self.memory.journal.state(), before)
        observation = self.memory.snapshot()["observations"][first["observation_ids"][0]]
        self.assertTrue(verified_list_observation(observation))
        self.assertEqual(observation["facts"]["price"]["displayed"], "₹1,299.00")
        self.assertEqual(observation["facts"]["price"]["pack_basis"], "pack of 2")
        self.assertEqual(observation["facts"]["image_urls"], ["https://m.media-amazon.com/images/I/fixture.jpg"])
        self.assertEqual(receipt["apify_requests"], 0); self.assertEqual(receipt["apify_charge_usd"], "0")

    def test_search_context_never_replaces_or_counts_as_a_list_signal(self):
        ranked = self.capture(name="list")
        listed = import_direct_marketplace(self.memory, ranked["folder"])
        searched = self.capture(request("amazon-search"), name="search")
        generic = import_direct_marketplace(self.memory, searched["folder"])
        state = self.memory.snapshot(); fresh = latest_observations(state["observations"], NOW)
        self.assertEqual(len(fresh), 2)
        relevance = {o["semantic_id"]: "relevant_family" for o in state["observations"].values()}
        self.assertTrue(compute_market_signals(fresh, relevance, NOW)["families"]["marketplace"]["positive"])
        generic_only = {i: state["observations"][i] for i in generic["observation_ids"]}
        self.assertFalse(compute_market_signals(generic_only, relevance, NOW)["families"]["marketplace"]["available"])

    def test_explicit_empty_blocked_failed_and_bad_snapshot_import_without_products(self):
        req = request("flipkart-search")
        for name, snap, changes, state in (
                ("empty", snapshot(req, cards=[], card_count=0, empty=True), {}, "empty"),
                ("blocked", snapshot(req, text="Access denied CAPTCHA"), {}, "blocked"),
                ("failed", None, {"state": "failed", "snapshot": None}, "failed"),
                ("bad-dom", {"cards": None}, {}, "failed")):
            with self.subTest(name=name):
                receipt = self.capture(req, name=name, snap=snap, **changes)
                self.assertEqual(receipt["state"], state)
                self.assertEqual(replay_marketplace(receipt["folder"]), receipt)
                imported = import_direct_marketplace(self.memory, receipt["folder"])
                self.assertEqual(imported["new_observations"], 0)

    def test_synthetic_receipt_cannot_be_imported_into_captured_namespace(self):
        receipt = self.capture()
        captured = ResearchMemory(self.root / "captured", self.memory.journal)
        with self.assertRaisesRegex(SafetyError, "RESEARCH_NAMESPACE_INVALID"):
            import_direct_marketplace(captured, receipt["folder"])

    def test_artifact_changes_and_acquisition_rebinding_fail_replay(self):
        receipt = self.capture(); folder = Path(receipt["folder"])
        (folder / "snapshot.json").write_bytes(b"{}")
        with self.assertRaisesRegex(SafetyError, "HASH_MISMATCH"): replay_marketplace(folder)
        with self.assertRaises(SafetyError): self.capture(request("amazon-search"))

    def test_native_pre_navigation_failure_tries_playwright_once_not_challenge_bypass(self):
        req = request()
        actual = transport(snapshot(req))
        with (patch("product_radar.native_public_browser.native_snapshot", return_value={"state": "failed", "reason": "startup", "navigation_started": False}) as native,
              patch("product_radar.marketplace_browser._playwright_snapshot", return_value=actual) as browser):
            result = capture_marketplace(req, "startup-recovery", data_root=self.root / "captures")
        self.assertEqual(result["state"], "usable"); self.assertEqual(native.call_count, 1); self.assertEqual(browser.call_count, 1)
        actual = transport(snapshot(req, text="Robot check CAPTCHA"))
        with (patch("product_radar.native_public_browser.native_snapshot", return_value=actual),
              patch("product_radar.marketplace_browser._playwright_snapshot") as browser):
            result = capture_marketplace(req, "challenge", data_root=self.root / "captures")
        self.assertEqual(result["state"], "blocked"); browser.assert_not_called()


class MarketplaceContextJoinTests(unittest.TestCase):
    def context(self, source="flipkart", quote="₹249.00"):
        memory = current_fixture(1)
        row = {"source": source, "source_item": "context-item", "acquisition_id": "context-acquisition", "observed_at": NOW,
               "semantic_id": "context-semantic", "semantic": {"title": "Synthetic product 0"},
               "facts": {"list_type": "product_search" if source in ("amazon", "flipkart") else "quickcommerce_context", "limitations": [],
                         "url": "https://www.flipkart.com/fixture/p/itm123?pid=HOM123456", "price": {"displayed": quote, "currency": "INR"},
                         "image_urls": ["https://rukminim2.flixcart.com/image/fixture.jpg"], "quantity": "pack of 2"}}
        memory.state["observations"]["context-obs"] = row
        review = deepcopy(memory.state["market_reviews"]["review0"])
        review.update(input_hash=digest(row), relevance=[{"id": "context-obs", "relationship": "relevant_family", "reason": "Synthetic family relevance."}])
        memory.state["market_reviews"]["context-review"] = review
        return memory

    def test_default_list_pool_unchanged_opt_in_context_keeps_scope(self):
        for source in ("amazon", "flipkart", "blinkit", "zepto", "instamart"):
            with self.subTest(source=source):
                memory = self.context(source)
                state = memory.snapshot()
                self.assertNotIn("context-semantic", _listing_pool(state, NOW))
                self.assertIn("context-semantic", _listing_pool(state, NOW, include_context=True))

    def test_prices_refs_photos_and_quickcommerce_context_do_not_change_three_group_counts(self):
        for source in ("amazon", "flipkart", "blinkit", "zepto", "instamart"):
            with self.subTest(source=source):
                memory = self.context(source)
                pool = build_report_pool(memory, NOW, fit_policy_id="fit-10-v1")
                facts = pool["facts_by_concept"]["c000"]
                price = next(p for p in facts["prices"] if p.get("raw_displayed") == "₹249.00")
                self.assertEqual(price["amount"], "249.00"); self.assertIsNone(price["quantity"])
                self.assertIn("pack of 2", price["conditions"])
                self.assertIn("context-obs", [r["id"] for r in facts["source_refs"]])
                self.assertEqual(facts["image_candidates"][0]["observation_id"], "context-obs")
                context = next(c for c in facts["market_checks"] if c.get("context_only"))
                self.assertIn("not sales", context["detail"])
                self.assertEqual(facts["opportunity_basis"]["available_sources"], 2 if source == "amazon" else 3)
                self.assertIn(source, facts["opportunity_basis"]["source_ids"])

    def test_unit_rate_range_and_missing_are_not_presented_as_single_unit_prices(self):
        for quote in ("₹199 / 100g", "₹199–299", None):
            facts = build_report_pool(self.context(quote=quote), NOW, fit_policy_id="fit-10-v1")["facts_by_concept"]["c000"]
            price = next(p for p in facts["prices"] if p.get("raw_displayed") == quote)
            self.assertIsNone(price["amount"]); self.assertIsNone(price["quantity"])


if __name__ == "__main__":
    unittest.main()
