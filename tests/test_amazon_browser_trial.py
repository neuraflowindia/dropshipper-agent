"""Synthetic browser fixtures only; no public navigation or research writes."""
import asyncio
import base64
import json
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
from types import ModuleType, SimpleNamespace
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from product_radar.amazon_browser_trial import LANDING, SNAPSHOT_JS, capture, list_scope, parse_snapshot, replay, summary, allowed_browser_request, save_browser_capture
from product_radar.models import SafetyError

URL = "https://www.amazon.in/gp/bestsellers/kitchen"


def card(asin="B012345678", rank="#1", title="Synthetic manual chopper", quote="₹130 set of 6"):
    return {"card_ref": "rank-card-0", "rank_text": rank, "title": title,
            "product_url": "https://www.amazon.in/dp/" + asin, "raw_price": quote,
            "image_url": "https://m.media-amazon.com/images/I/synthetic.jpg"}


def snapshot(cards=None):
    values = [card()] if cards is None else cards
    return {"url": URL, "title": "Amazon.in Best Sellers in Home & Kitchen",
            "heading": "Best Sellers\nHome & Kitchen", "text": "Synthetic public list",
            "html": "<html>Synthetic fixture</html>", "html_characters": 36,
            "text_truncated": False, "cards": values, "rank_nodes": len(values),
            "card_count": len(values), "links": []}


class Page:
    def __init__(self, snap=None, status=200, fail=False):
        self.snap, self.status, self.fail, self.navigations = snap or snapshot(), status, fail, 0
    async def goto(self, *_args, **_kwargs):
        self.navigations += 1
        if self.fail:
            raise RuntimeError("sensitive arbitrary browser error")
        return SimpleNamespace(status=self.status)
    async def wait_for_function(self, *_args, **_kwargs):
        pass
    async def evaluate(self, *_args):
        return dict(self.snap)
    async def screenshot(self, **_kwargs):
        return b"synthetic fixture screenshot"


class TrialTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.data, self.media = Path(self.temp.name) / "data", Path(self.temp.name) / "media"
    def save(self, page=None, acquisition="round1"):
        return capture(URL, "fixture", acquisition, data_root=self.data, media_root=self.media,
                       page=page or Page())
    def test_public_scope_is_india_and_page_one_only(self):
        self.assertEqual(list_scope(URL + "/ref=zg_bs_nav_kitchen_0"), "kitchen")
        self.assertEqual(list_scope(URL + "?pg=1"), "kitchen")
        for value in (URL.replace(".in", ".com"), URL + "?pg=2", URL + "?q=test", URL.replace("bestsellers", "movers-and-shakers"), URL.replace("kitchen", "invented"), "https://user@www.amazon.in/gp/bestsellers/kitchen", URL + "#test"):
            with self.subTest(value=value):
                self.assertIsNone(list_scope(value))
    def test_actual_rank_and_raw_price_are_preserved(self):
        value = parse_snapshot(snapshot(), URL)
        self.assertEqual(value["state"], "usable")
        row = value["records"][0]
        self.assertEqual(row["raw_price"], "₹130 set of 6")
        self.assertEqual(row["currency"], "INR")
        self.assertEqual(row["unit_pack_basis"], "unknown")
        self.assertIsNone(value["qualifying_products"])
    def test_optional_price_and_image_are_not_required(self):
        row = card(quote=None)
        row["image_url"] = None
        result = parse_snapshot(snapshot([row]), URL)
        self.assertEqual(result["state"], "usable")
        self.assertIsNone(result["records"][0]["currency"])
    def test_wrong_final_category_or_heading_is_not_verified(self):
        for field, value in (("url", URL.replace("kitchen", "baby")), ("title", "Amazon.in")):
            snap = snapshot()
            snap[field] = value
            if field == "title":
                snap["heading"] = "Amazon home page"
            self.assertFalse(parse_snapshot(snap, URL)["scope_verified"])
    def test_challenge200_is_blocked_not_empty_or_success(self):
        snap = snapshot([])
        snap["text"] = "Enter the characters you see below: robot check"
        result = parse_snapshot(snap, URL)
        self.assertEqual(result["state"], "blocked")
        self.assertFalse(result["scope_verified"])
        self.assertFalse(result["access_success"])
    def test_no_selectors_is_insufficient_not_valid_empty(self):
        result = parse_snapshot(snapshot([]), URL)
        self.assertEqual(result["state"], "insufficient")
        self.assertEqual(result["reason"], "ranked_cards_unrecognised")
    def test_no_dom_order_rank_invention(self):
        result = parse_snapshot(snapshot([card(rank="")]), URL)
        self.assertEqual(result["records"], [])
    def test_duplicate_asin_preserves_references(self):
        first, second = card(), card()
        second["card_ref"] = "rank-card-1"
        result = parse_snapshot(snapshot([first, second]), URL)
        self.assertEqual(len(result["records"]), 1)
        self.assertEqual(result["records"][0]["card_refs"], ["rank-card-0", "rank-card-1"])
    def test_conflicting_asin_is_quarantined(self):
        result = parse_snapshot(snapshot([card(), card(quote="₹999")]), URL)
        self.assertEqual(result["records"], [])
        self.assertEqual(result["rejected"][0]["reason"], "conflicting_duplicate_asin")
    def test_incorrect_country_product_link_rejected(self):
        row = card()
        row["product_url"] = row["product_url"].replace(".in", ".com")
        self.assertEqual(parse_snapshot(snapshot([row]), URL)["records"], [])
    def test_card_ceiling_returns_partial_not_complete(self):
        snap = snapshot()
        snap.update(card_count=101, rank_nodes=101)
        self.assertEqual(parse_snapshot(snap, URL)["state"], "partial")
    def test_landing_retains_only_inspected_list_scopes(self):
        snap = snapshot([])
        snap.update(url=LANDING, heading="Amazon Best Sellers", title="Amazon.in Best Sellers",
                    links=[{"url": URL, "label": "Kitchen"}, {"url": "https://www.amazon.com/gp/bestsellers/baby", "label": "Wrong country"}])
        result = parse_snapshot(snap, LANDING, kind="landing")
        self.assertEqual(result["cases"], [{"url": URL, "label": "Kitchen", "category": "kitchen"}])
    def test_capture_is_immutable_and_replay_is_offline(self):
        page = Page()
        first = self.save(page)
        second = self.save(page)
        self.assertEqual(first, second)
        self.assertEqual(page.navigations, 1)
        self.assertEqual(first["apify_requests"], 0)
        self.assertEqual(first["model_calls"], 0)
        self.assertTrue((self.media / "fixture/round1/page.png").is_file())
    def test_snapshot_tampering_fails_replay(self):
        self.save()
        path = self.data / "fixture/round1/snapshot.json"
        path.write_text("{}")
        with self.assertRaises(SafetyError):
            replay("fixture", "round1", data_root=self.data, media_root=self.media)
    def test_interrupted_folder_never_relaunches(self):
        folder = self.data / "fixture/round1"
        folder.mkdir(parents=True)
        page = Page()
        with self.assertRaises(SafetyError):
            self.save(page)
        self.assertEqual(page.navigations, 0)
    def test_capture_error_is_redacted_and_reusable(self):
        page = Page(fail=True)
        result = self.save(page)
        self.assertEqual(result["reason"], "owned_browser_capture_error")
        self.assertNotIn("sensitive", json.dumps(result))
        self.assertEqual(self.save(page), result)
    def test_summary_counts_source_novelty_not_product_eligibility(self):
        self.save(acquisition="round1")
        self.save(Page(snapshot([card(), card(asin="B098765432", rank="#2")])), "round2")
        result = summary("fixture", data_root=self.data, media_root=self.media)
        self.assertEqual(result["list_attempts"], 2)
        self.assertEqual(result["cases"][1]["previous_overlap"], 1)
        self.assertEqual(result["cases"][1]["new_asins_since_previous"], 1)
        self.assertEqual(result["recommendation_yield"], "Not assessed")
    def test_limits_and_ids_reject_before_directory_or_navigation(self):
        page = Page()
        for value in (0, 61, True):
            with self.assertRaises(SafetyError):
                capture(URL, "fixture", "bad", data_root=self.data, media_root=self.media, timeout=value, page=page)
        self.assertEqual(page.navigations, 0)
        self.assertFalse(self.data.exists())
    def test_source_has_no_production_imports(self):
        source = (Path(__file__).resolve().parents[1] / "src/product_radar/amazon_browser_trial.py").read_text()
        for value in ("from .research_memory", "from .research_ai", "from .benchmark", "from .storage"):
            self.assertNotIn(value, source)
    def test_quote_retains_offers_from_conditions(self):
        row = parse_snapshot(snapshot([card(quote="4 offers from ₹1.00")]), URL)["records"][0]
        self.assertEqual(row["raw_price"], "4 offers from ₹1.00")
        self.assertEqual(row["unit_pack_basis"], "unknown")
    def price_dom_fixture(self, *, modern=None, legacy=None, offers=None, outside=None):
        """Execute the real extractor against a tiny same-card DOM test double.

        The modern node represents the inspected nested a-color-price span, not
        a live visit. Outside-card prices must never supply a missing quote.
        """
        node = shutil.which("node")
        if not node:
            self.skipTest("Existing Node executable is required for the offline JS fixture")
        values = {"modern": modern, "legacy": legacy, "offers": offers, "outside": outside}
        fixture = r"""
const fixture = VALUES;
function price(text, offers) {return text===null?null:{innerText:text,closest(selector){return offers!==null&&selector==='.a-color-secondary'?{innerText:offers}:null;}};}
const card = {querySelector(selector) {
  if(selector==='a[href*="/dp/"],a[href*="/gp/product/"]') return {href:'https://www.amazon.in/dp/B012345678',innerText:'Synthetic chopper'};
  if(selector==='img') return {alt:'Synthetic chopper',currentSrc:'https://m.media-amazon.com/images/I/fixture.jpg'};
  if(selector==='.p13n-sc-price,.a-price:not(.a-text-price) .a-offscreen') return price(fixture.legacy,fixture.offers);
  if(selector==='.a-color-price') return price(fixture.modern,fixture.offers);
  return null;
}};
const rank={innerText:'#7',closest(){return card;}};
globalThis.location={href:'https://www.amazon.in/gp/bestsellers/kitchen'};
globalThis.document={body:{innerText:'Bestsellers in Home & Kitchen'},documentElement:{outerHTML:'<html>synthetic DOM fixture</html>'},title:'Amazon.in Bestsellers in Home & Kitchen',
  querySelector(selector){return fixture.outside===null?null:{innerText:fixture.outside};},
  querySelectorAll(selector){if(selector==='.zg-bdg-text')return [rank];if(selector==='h1,.zg-selected,#zg_banner_text')return [{innerText:'Bestsellers in Home & Kitchen'}];return [];}};
console.log(JSON.stringify((EXTRACTOR)()));
""".replace("VALUES", json.dumps(values)).replace("EXTRACTOR", SNAPSHOT_JS)
        completed = subprocess.run([node, "-e", fixture], capture_output=True, text=True, timeout=5, check=True)
        return json.loads(completed.stdout)
    def test_modern_nested_color_price_is_extracted_from_same_card(self):
        snap = self.price_dom_fixture(modern="₹319.00")
        self.assertEqual(snap["cards"][0]["raw_price"], "₹319.00")
        row = parse_snapshot(snap, URL)["records"][0]
        self.assertEqual(row["raw_price"], "₹319.00")
        self.assertEqual(row["position"], 7)
        self.assertEqual(row["unit_pack_basis"], "unknown")
    def test_legacy_offers_from_quote_remains_complete_before_modern_fallback(self):
        snap = self.price_dom_fixture(modern="₹999.00", legacy="₹1.00", offers="4 offers from ₹1.00")
        self.assertEqual(snap["cards"][0]["raw_price"], "4 offers from ₹1.00")
    def test_modern_price_preserves_pack_context_when_explicitly_displayed(self):
        snap = self.price_dom_fixture(modern="₹319.00", offers="₹319.00 pack of 3")
        self.assertEqual(snap["cards"][0]["raw_price"], "₹319.00 pack of 3")
    def test_outside_card_price_cannot_fill_missing_local_price(self):
        snap = self.price_dom_fixture(outside="₹888.00")
        self.assertIsNone(snap["cards"][0]["raw_price"])
    def test_truncated_quote_is_partial_not_full_price_claim(self):
        row = card()
        row["raw_price_truncated"] = True
        result = parse_snapshot(snapshot([row]), URL)
        self.assertEqual(result["state"], "partial")
        self.assertFalse(result["records"][0]["quote_complete"])
    def test_different_tracking_urls_do_not_create_asin_conflict(self):
        second = card()
        second["product_url"] += "/ref=tracking"
        self.assertEqual(len(parse_snapshot(snapshot([card(), second]), URL)["records"]), 1)
    def test_missing_card_ancestor_is_partial_not_complete(self):
        snap = snapshot()
        snap["rank_nodes"] = 2
        self.assertEqual(parse_snapshot(snap, URL)["state"], "partial")
    def test_landing_kind_cannot_hide_a_list_attempt(self):
        with self.assertRaises(SafetyError):
            parse_snapshot(snapshot(), URL, kind="landing")
    def test_receipt_summary_metadata_cannot_disagree_with_parser(self):
        self.save()
        path = self.data / "fixture/round1/receipt.json"
        value = json.loads(path.read_text())
        value["scope_verified"] = False
        path.write_text(json.dumps(value))
        with self.assertRaises(SafetyError):
            replay("fixture", "round1", data_root=self.data, media_root=self.media)
    def test_wrong_currency_and_from_price_are_not_normalized(self):
        row = parse_snapshot(snapshot([card(quote="$130 from offer")]), URL)["records"][0]
        self.assertIsNone(row["currency"])
        self.assertEqual(row["raw_price"], "$130 from offer")
    def test_request_gate_blocks_login_private_redirects_and_post(self):
        self.assertTrue(allowed_browser_request(URL, navigation=True))
        self.assertTrue(allowed_browser_request("https://images-eu.ssl-images-amazon.com/images/I/x.jpg"))
        for value in ("https://127.0.0.1/a", "http://www.amazon.in/a", "https://www.amazon.in/ap/signin", "https://attacker.test/a", "https://user:pass@www.amazon.in/a"):
            self.assertFalse(allowed_browser_request(value, navigation=True))
        self.assertFalse(allowed_browser_request(URL, "POST"))
    def test_real_ownership_lifecycle_is_fresh_and_closed(self):
        class Context:
            async def route(self, *args):
                self.route_args = args
            async def new_page(self):
                return Page()
        class Browser:
            closed = False
            async def new_context(self, **options):
                self.options = options
                return Context()
            async def close(self):
                self.closed = True
        browser = Browser()
        class Chromium:
            async def launch(self, **options):
                self.options = options
                return browser
        chromium = Chromium()
        class Driver:
            stopped = False
            def __init__(self):
                self.chromium = chromium
            async def stop(self):
                self.stopped = True
        driver = Driver()
        class Starter:
            async def start(self):
                return driver
        module = ModuleType("playwright.async_api")
        module.async_playwright = Starter
        with patch.dict(sys.modules, {"playwright.async_api": module}):
            result = capture(URL, "fixture", "owned", data_root=self.data, media_root=self.media)
        self.assertEqual(chromium.options, {"headless": True})
        self.assertEqual(browser.options["locale"], "en-IN")
        self.assertTrue(browser.closed)
        self.assertTrue(driver.stopped)
        self.assertTrue(result["owned_browser_closed"])
    def test_reused_acquisition_cannot_change_requested_url_or_kind(self):
        self.save()
        page = Page()
        with self.assertRaisesRegex(SafetyError, "ACQUISITION_INTENT_MISMATCH"):
            capture(URL.replace("kitchen", "baby"), "fixture", "round1", data_root=self.data,
                    media_root=self.media, page=page)
        self.assertEqual(page.navigations, 0)
    def test_driver_start_timeout_is_diagnosed_and_owned_manager_stopped(self):
        original = asyncio.wait_for
        class Starter:
            stopped = False
            async def start(self):
                await asyncio.sleep(10)
            async def __aexit__(self, *args):
                self.stopped = True
        starter = Starter()
        module = ModuleType("playwright.async_api")
        module.async_playwright = lambda: starter
        async def bounded(awaitable, timeout):
            return await original(awaitable, timeout=0.01)
        with patch.dict(sys.modules, {"playwright.async_api": module}), patch("product_radar.amazon_browser_trial.asyncio.wait_for", bounded):
            result = capture(URL, "fixture", "startup", data_root=self.data, media_root=self.media)
        self.assertEqual(result["failure_stage"], "driver_start")
        self.assertEqual(result["diagnostic_code"], "driver_start_deadline")
        self.assertEqual(result["public_page_navigations"], 0)
        self.assertFalse(result["driver_started"])
        self.assertTrue(starter.stopped)
        self.assertTrue(result["driver_cleanup_verified"])
    def test_unknown_startup_cleanup_never_claims_browser_closed(self):
        class Starter:
            async def start(self):
                raise RuntimeError("sensitive process path EPIPE")
        module = ModuleType("playwright.async_api")
        module.async_playwright = Starter
        with patch.dict(sys.modules, {"playwright.async_api": module}):
            result = capture(URL, "fixture", "unknown", data_root=self.data, media_root=self.media)
        self.assertFalse(result["owned_browser_closed"])
        self.assertFalse(result["driver_cleanup_verified"])
        self.assertEqual(result["failure_stage"], "driver_start")
        self.assertEqual(result["diagnostic_code"], "browser_driver_pipe_closed")
        self.assertNotIn("sensitive", json.dumps(result))
    def test_failed_startup_cleanup_retains_primary_timeout_reason(self):
        original = asyncio.wait_for
        class Starter:
            async def start(self):
                await asyncio.sleep(10)
            async def __aexit__(self, *args):
                raise RuntimeError("private cleanup error")
        module = ModuleType("playwright.async_api")
        module.async_playwright = Starter
        async def bounded(awaitable, timeout):
            return await original(awaitable, timeout=0.01)
        with patch.dict(sys.modules, {"playwright.async_api": module}), patch("product_radar.amazon_browser_trial.asyncio.wait_for", bounded):
            result = capture(URL, "fixture", "cleanupfailed", data_root=self.data, media_root=self.media)
        self.assertEqual(result["reason"], "whole_case_deadline")
        self.assertEqual(result["failure_stage"], "driver_start")
        self.assertFalse(result["owned_browser_closed"])
        self.assertNotIn("private", json.dumps(result))

    def browser_envelope(self):
        snap = snapshot()
        snap.pop("html")
        return {"mode": "codex_playwright_fresh_owned_context", "kind": "list", "requested_url": URL,
                "started_at": "2026-10-07T10:00:00Z", "collected_at": "2026-10-07T10:00:05Z",
                "completed_at": "2026-10-07T10:00:06Z", "elapsed_seconds": 6.0, "stage": "screenshot",
                "navigation_status": 200, "public_page_navigations": 1, "owned_context_closed": True,
                "snapshot": snap, "html_saved": False,
                "screenshot_base64": base64.b64encode(b"\x89PNG\r\n\x1a\nsyntheticfixture").decode()}
    def test_external_capture_preserves_real_mode_and_replays_offline(self):
        envelope = self.browser_envelope()
        result = save_browser_capture("fixture", "external", envelope, data_root=self.data, media_root=self.media)
        self.assertEqual(result["mode"], "codex_playwright_fresh_owned_context")
        self.assertIsNone(result["owned_browser_closed"])
        self.assertTrue(result["owned_context_closed"])
        self.assertEqual(result["collected_at"], envelope["collected_at"])
        self.assertEqual(result, replay("fixture", "external", data_root=self.data, media_root=self.media))
        self.assertEqual(result, save_browser_capture("fixture", "external", envelope, data_root=self.data, media_root=self.media))
        self.assertEqual(json.loads((self.data / "fixture/external/browser_envelope.json").read_text()), envelope)
        self.assertTrue((self.media / "fixture/external/page.png").is_file())
    def test_external_capture_changed_envelope_cannot_overwrite(self):
        envelope = self.browser_envelope()
        save_browser_capture("fixture", "external", envelope, data_root=self.data, media_root=self.media)
        envelope["stage"] = "complete"
        with self.assertRaisesRegex(SafetyError, "ACQUISITION_INTENT_MISMATCH"):
            save_browser_capture("fixture", "external", envelope, data_root=self.data, media_root=self.media)
    def test_external_capture_invalid_mode_private_inputs_rejected_before_writes(self):
        for field, value in (("mode", "personal_profile"), ("cookies", "secret"), ("requested_url", "https://www.amazon.in/ap/signin"),
                             ("public_page_navigations", 2), ("owned_context_closed", "true"), ("elapsed_seconds", 61)):
            envelope = self.browser_envelope()
            envelope[field] = value
            with self.subTest(field=field), self.assertRaises(SafetyError):
                save_browser_capture("fixture", "external", envelope, data_root=self.data, media_root=self.media)
        self.assertFalse(self.data.exists())
    def test_external_capture_timestamps_cannot_be_estimated_or_out_of_order(self):
        for field, value in (("started_at", "2026-10-07T10:00:00"), ("completed_at", "2026-10-07T09:00:00Z"),
                             ("elapsed_seconds", 50.0)):
            envelope = self.browser_envelope()
            envelope[field] = value
            with self.subTest(field=field), self.assertRaises(SafetyError):
                save_browser_capture("fixture", "external", envelope, data_root=self.data, media_root=self.media)
    def test_external_capture_unclosed_context_is_failed_not_usable(self):
        envelope = self.browser_envelope()
        envelope["owned_context_closed"] = False
        result = save_browser_capture("fixture", "external", envelope, data_root=self.data, media_root=self.media)
        self.assertEqual(result["state"], "failed")
        self.assertEqual(result["reason"], "owned_context_cleanup_unverified")
        self.assertEqual(replay("fixture", "external", data_root=self.data, media_root=self.media), result)
    def test_external_capture_missing_optional_html_and_png_remains_usable(self):
        envelope = self.browser_envelope()
        envelope.pop("screenshot_base64")
        result = save_browser_capture("fixture", "external", envelope, data_root=self.data, media_root=self.media)
        self.assertEqual(result["state"], "usable")
        self.assertNotIn("page.png", result["artifacts"])
        self.assertNotIn("rendered.html", result["artifacts"])
    def test_external_capture_png_invalid_not_written_as_image(self):
        envelope = self.browser_envelope()
        envelope["screenshot_base64"] = base64.b64encode(b"not PNG").decode()
        with self.assertRaises(SafetyError):
            save_browser_capture("fixture", "external", envelope, data_root=self.data, media_root=self.media)
        self.assertFalse(self.media.exists())
    def test_external_capture_only_accepts_fixed_public_snapshot_fields(self):
        for location in ("snapshot", "card", "link"):
            envelope = self.browser_envelope()
            if location == "snapshot":
                envelope["snapshot"]["cookies"] = "private"
            elif location == "card":
                envelope["snapshot"]["cards"][0]["cookies"] = "private"
            else:
                envelope["snapshot"]["links"] = [{"url": URL, "label": "Kitchen", "cookies": "private"}]
            with self.subTest(location=location), self.assertRaises(SafetyError):
                save_browser_capture("fixture", "external", envelope, data_root=self.data, media_root=self.media)
        self.assertFalse(self.data.exists())
    def test_external_capture_receipt_ownership_tampering_fails_replay(self):
        envelope = self.browser_envelope()
        save_browser_capture("fixture", "external", envelope, data_root=self.data, media_root=self.media)
        path = self.data / "fixture/external/receipt.json"
        value = json.loads(path.read_text())
        value["owned_context_closed"] = False
        path.write_text(json.dumps(value))
        with self.assertRaises(SafetyError):
            replay("fixture", "external", data_root=self.data, media_root=self.media)
    def test_bounded_timeout_saves_failed_receipt_no_retry(self):
        original = asyncio.wait_for
        class SlowPage(Page):
            async def goto(self, *args, **kwargs):
                self.navigations += 1
                await asyncio.sleep(20)
        async def quick_deadline(awaitable, timeout):
            return await original(awaitable, timeout=min(timeout, .01))
        page = SlowPage()
        with patch("product_radar.amazon_browser_trial.asyncio.wait_for", side_effect=quick_deadline):
            result = self.save(page)
        self.assertEqual(result["reason"], "whole_case_deadline")
        self.assertEqual(self.save(page), result)
        self.assertEqual(page.navigations, 1)


if __name__ == "__main__":
    unittest.main()
