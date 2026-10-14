"""The four dropshipping platforms' saved catalogues (Plan R10 W4).

DeoDap and BaapStore feed pages are trimmed real public Shopify feeds saved on
14 October 2026 (tests/fixtures/dropship_platforms); the signed-in pages are
synthetic. Nothing here opens a network connection or a browser: the feed
fetcher, the address resolver and the signed-in browser are offline doubles,
and pacing and sign-ins are local records in a temporary folder.
"""

from copy import deepcopy
from datetime import timedelta
import json
import os
from pathlib import Path
import re
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "tests"))
from product_radar import dropship_platforms as platforms
from product_radar import platform_feeds as feeds
from product_radar import platform_signed_in as signed
from product_radar import signed_in_pacing as pacing
from product_radar.models import SafetyError, timestamp
from product_radar.signed_in_browser import note_sign_in_window, site_states
from product_radar.storage import load_json

FIXTURES = ROOT / "tests/fixtures/dropship_platforms"
START = "2026-10-14T06:00:00+00:00"


def fixture(name):
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


def later(at, seconds):
    return (timestamp(at) + timedelta(seconds=seconds)).isoformat()


class Clock:
    """Each call is six seconds after the last: past the five-second feed spacing.

    ``last`` is the time last handed out: pacing counts a read at "now", so the
    tests' pacing records use this same clock (``paced``)."""

    def __init__(self, at=START, step=6, times=None):
        self.at, self.step, self.times, self.last = at, step, iter(times) if times else None, at

    def __call__(self):
        if self.times is not None:
            self.last = next(self.times)
            return self.last
        value, self.at = self.at, later(self.at, self.step)
        self.last = value
        return value


def paced(clock):
    """Pacing's own "now" follows the test clock (the shared helpers count at now)."""
    return patch("product_radar.signed_in_pacing.utc_now", side_effect=lambda *args, **kwargs: clock.last)


class FeedDouble:
    """Serve saved feed bodies by address; record every request."""

    def __init__(self, pages=None, status=200):
        self.pages, self.status, self.calls = dict(pages or {}), status, []

    def __call__(self, url):
        self.calls.append(url)
        n = len(self.calls)
        receipt = {"schema_version": 2, "id": "dropship-feed-read-" + str(n).zfill(8), "requested_url": url,
                   "final_url": url, "checked_at": START, "http_status": self.status, "execution_stage": "response_received",
                   "attempt_started_at": START, "execution_ref": "public-fetch-" + format(n, "032x"),
                   "state": "read", "reason": None, "body_sha256": "0" * 64}
        if self.status != 200:
            return dict(receipt, state="failed", reason="DROPSHIP_FEED_REFUSED"), None
        body = self.pages.get(url)
        if body is None:
            body = {"products": []} if "products.json" in url else {"collections": []}
        return receipt, json.dumps(body)


def deodap_pages(products=None, lists=None):
    base = "https://deodap.in"
    pages = {base + "/collections.json?limit=250&page=1": fixture("deodap-collections-2026-10-14.json"),
             base + "/products.json?limit=250&page=1": products or fixture("deodap-products-2026-10-14.json")}
    for handle, body in (lists or {"best-selling-products": fixture("deodap-best-selling-products-2026-10-14.json")}).items():
        pages[base + "/collections/" + handle + "/products.json?limit=250&page=1"] = body
    return pages


def caddy(product_id=777, title="Steel Draining Sink Caddy with Drip Tray", price="149.00", mrp="499.00"):
    return {"id": product_id, "title": title, "handle": "steel-draining-sink-caddy-" + str(product_id),
            "created_at": "2026-10-10T10:00:00+05:30", "vendor": "Fixture", "product_type": "Kitchen",
            "tags": ["Sink Organiser", "Kitchen"], "images": [{"src": "https://cdn.shopify.com/fixture/caddy.jpg"}],
            "variants": [{"price": price, "compare_at_price": mrp, "available": True}]}


class PlatformCase(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.store = Path(temp.name) / "research"
        self.pacing_root = Path(temp.name) / "project"
        self.store.mkdir()
        self.pacing_root.mkdir()

    def feed(self, route="deodap", fetcher=None):
        return feeds.FeedReader(route, pacing_root=self.pacing_root, fetcher=fetcher or FeedDouble(deodap_pages()))

    def advance(self, reader, route="deodap", clock=None, **options):
        clock = clock or Clock(options.get("as_of") or START)
        with paced(clock):
            return platforms.advance_platform(self.store, route, reader, clock=clock, sleep=lambda _: None, **options)

    def build(self, route="deodap", pages=None, limit=20):
        """A finished full cycle and lists cycle from saved pages."""
        double = FeedDouble(pages or deodap_pages())
        reader = self.feed(route, double)
        clock = Clock()
        while True:
            outcome = self.advance(reader, route, clock=clock, limit=limit)
            if outcome["state"] in ("current", "waiting"):
                return outcome, double, clock


class SettingsTests(unittest.TestCase):
    def test_saved_settings_are_valid_and_complete(self):
        value = platforms.settings()
        self.assertEqual(set(value["platforms"]), set(platforms.ROUTES))
        self.assertEqual(platforms.platform_settings("deodap")["transport"], "shopify-feed")
        self.assertEqual(platforms.platform_settings("dropdash")["transport"], "signed-in-browser")
        for route in platforms.ROUTES:
            rules = pacing.pacing_rules(route)
            expected = (5, 1500) if route in platforms.FEED_ROUTES else (30, 300)
            self.assertEqual((rules["min_interval_seconds"], rules["daily_ceiling"]), expected)

    def test_invalid_settings_fail_closed(self):
        value = platforms.settings()
        changes = [
            lambda v: v["platforms"].pop("deodap"),
            lambda v: v.update(kind="other"),
            lambda v: v["platforms"]["deodap"].update(transport="signed-in-browser"),
            lambda v: v["platforms"]["deodap"].update(requests_per_round=0),
            lambda v: v["platforms"]["deodap"]["lists"][0].update(role="invented"),
            lambda v: v["platforms"]["deodap"]["lists"][0].update(pattern="("),
            lambda v: v["platforms"]["deodap"]["lists"][0].update(handles=[], pattern=None),
            lambda v: v["platforms"]["dropdash"]["pages"][0].update(url="https://elsewhere.example/x"),
            lambda v: v["platforms"]["dropdash"].update(pages=[{"role": "trending", "url": "https://app.dropdash.co/t", "max_pages": 1}]),
            lambda v: v["platforms"]["dropdash"].update(terms_url="https://elsewhere.example/terms"),
            lambda v: v["matching"].update(min_coverage="0.1"),
            lambda v: v["discovery"].update(items_per_round=0),
            lambda v: v["discovery"]["categories"].pop("Baby Products"),
            lambda v: v["discovery"].update(exclude=[]),
            lambda v: v["refresh"].update(seconds_per_round=5),
            # Saved reads must outlive a whole catalogue refresh.
            lambda v: v["platforms"]["deodap"].update(raw_retention_days=7),
        ]
        for change in changes:
            broken = deepcopy(value)
            change(broken)
            with self.assertRaisesRegex(SafetyError, "DROPSHIP_SETTINGS_INVALID"):
                platforms.validate_settings(broken)
        with self.assertRaisesRegex(SafetyError, "DROPSHIP_ROUTE_INVALID"):
            platforms.platform_settings("amazon-search")


class FeedParsingTests(unittest.TestCase):
    def test_products_keep_price_mrp_stock_vendor_tags_and_dates(self):
        items = feeds.parse_products(fixture("deodap-best-selling-products-2026-10-14.json")["products"], "https://deodap.in")
        self.assertEqual(len(items), 4)
        first = items[0]
        self.assertEqual((first["item_id"], first["price"], first["mrp"], first["in_stock"]), ("10736207266102", "17", "20", True))
        self.assertTrue(first["url"].startswith("https://deodap.in/products/"))
        self.assertTrue(first["images"][0].startswith("https://cdn.shopify.com/"))
        self.assertTrue(first["created_at"])
        cleaned = platforms.clean_item(first, price_basis="basis")
        self.assertEqual(cleaned["price"], "17")
        self.assertEqual(platforms.clean_item(dict(first, mrp="10.00"), price_basis="b")["mrp"], None)
        # Canonical decimal text: no trailing zeros, no exponent; a zero variant price is no price.
        self.assertEqual([platforms.amount(value) for value in ("149.00", "12.50", "1e3", "1,299.90", "0.00", 7)],
                         ["149", "12.5", "1000", "1299.9", None, "7"])
        zero = feeds.parse_products([{"id": 9, "handle": "z", "title": "Z", "variants": [
            {"price": "0.00", "compare_at_price": "0.00"}, {"price": "35.00", "compare_at_price": "99.00"}]}], "https://deodap.in")
        self.assertEqual((zero[0]["price"], zero[0]["mrp"]), ("35", "99"))
        self.assertIsNone(platforms.clean_item(dict(first, url="http://deodap.in/x"), price_basis="b"))
        self.assertEqual(platforms.clean_item(dict(first, created_at="not a date", in_stock="yes"), price_basis="b")["created_at"], None)

    def test_unusable_rows_are_skipped_and_tags_text_is_split(self):
        rows = [{"id": "1", "handle": "x"}, {"id": 2, "handle": "bad handle"}, "text",
                {"id": 3, "handle": "ok", "title": "T", "tags": "a, b ,", "variants": [{"price": True}]}]
        items = feeds.parse_products(rows, "https://deodap.in")
        self.assertEqual([(row["item_id"], row["tags"], row["price"], row["in_stock"]) for row in items], [("3", ["a", "b"], None, None)])
        self.assertEqual(feeds.parse_products("nothing", "https://deodap.in"), [])
        self.assertEqual(platforms.amount("1,299"), "1299")
        self.assertIsNone(platforms.amount("0"))
        self.assertIsNone(platforms.amount(True))
        self.assertIsNone(platforms.amount("free"))

    def test_collections_carry_their_list_roles_for_the_product_map(self):
        config = platforms.platform_settings("deodap")
        rows = feeds.parse_collections(fixture("deodap-collections-2026-10-14.json")["collections"] + [{"handle": "Bad Handle"}],
                                       "https://deodap.in", config)
        roles = {row["handle"]: row["roles"] for row in rows}
        self.assertEqual(roles["best-selling-products"], ["bestseller"])
        self.assertEqual(roles["most-bought-this-month"], ["bestseller"])
        self.assertEqual(roles["delhi-best-selling"], ["state_bestseller"])
        self.assertEqual(roles["trending-tech-vault"], [])  # "Internet made me buy it": not a trending list
        self.assertEqual(roles["just-arrived"], ["new_arrival"])
        self.assertEqual(roles["hot-water-bag"], [])
        self.assertNotIn("Bad Handle", roles)
        self.assertTrue(all(set(row) == {"handle", "title", "url", "products_count", "roles", "parent"} for row in rows))


class FetchTests(unittest.TestCase):
    class Response:
        def __init__(self, status, body, content_type="application/json; charset=utf-8"):
            self.status, self.body, self.content_type, self.closed = status, body, content_type, False

        def getheader(self, name, default=None):
            return {"Content-Type": self.content_type}.get(name, default)

        def isclosed(self):
            return self.closed

        def read1(self, size):
            chunk, self.body = self.body[:size], self.body[size:]
            if not chunk:
                self.closed = True
            return chunk

        def close(self):
            self.closed = True

    def connection(self, response, requests):
        test = self

        class Connection:
            def __init__(self, host, port, scheme, address, control):
                self.sock = type("Sock", (), {"settimeout": lambda self, value: None})()
                test.assertEqual(address[2][0], "93.184.216.34")

            def request(self, method, target, headers):
                requests.append((method, target, headers))

            def getresponse(self):
                return response

            def close(self):
                pass
        return Connection

    @staticmethod
    def resolver(host, port, type=None):
        return [(2, 1, 6, "", ("93.184.216.34", port))]

    def test_json_feed_is_read_with_a_plain_get_and_an_owned_receipt(self):
        requests = []
        receipt, body = feeds.fetch_feed("https://deodap.in/products.json?limit=250&page=1", resolver=self.resolver,
                                         connection_factory=self.connection(self.Response(200, b'{"products": []}'), requests))
        self.assertEqual(json.loads(body), {"products": []})
        self.assertEqual((receipt["state"], receipt["http_status"], receipt["execution_stage"]), ("read", 200, "response_received"))
        self.assertRegex(receipt["execution_ref"], r"^public-fetch-[0-9a-f]{32}$")
        self.assertEqual(requests[0][:2], ("GET", "/products.json?limit=250&page=1"))
        self.assertNotIn("Cookie", requests[0][2])

    def test_refusals_html_and_bad_json_are_failures_without_a_body(self):
        cases = [(self.Response(429, b""), "DROPSHIP_FEED_REFUSED"),
                 (self.Response(200, b"<html></html>", "text/html"), "DROPSHIP_FEED_UNAVAILABLE"),
                 (self.Response(200, b"{not json"), "DROPSHIP_FEED_UNAVAILABLE"),
                 (self.Response(500, b""), "DROPSHIP_FEED_UNAVAILABLE")]
        for response, reason in cases:
            receipt, body = feeds.fetch_feed("https://deodap.in/collections.json", resolver=self.resolver,
                                             connection_factory=self.connection(response, []))
            self.assertEqual((receipt["state"], receipt["reason"], body, receipt["body_sha256"]), ("failed", reason, None, None))

    def test_private_addresses_are_refused(self):
        receipt, body = feeds.fetch_feed("https://deodap.in/collections.json",
                                         resolver=lambda host, port, type=None: [(2, 1, 6, "", ("10.0.0.1", port))])
        self.assertEqual((receipt["state"], receipt["reason"], body), ("failed", "DROPSHIP_FEED_UNAVAILABLE", None))
        with self.assertRaisesRegex(SafetyError, "PUBLIC_URL_INVALID"):
            feeds.fetch_feed("file:///etc/passwd")


class FeedRefreshTests(PlatformCase):
    def test_full_then_lists_cycle_builds_the_catalogue_with_roles_and_raw_files(self):
        outcome, double, _ = self.build()
        self.assertEqual(outcome["state"], "current")
        self.assertEqual(double.calls[:2], ["https://deodap.in/collections.json?limit=250&page=1",
                                            "https://deodap.in/products.json?limit=250&page=1"])
        lists = double.calls[2:]
        self.assertIn("https://deodap.in/collections/best-selling-products/products.json?limit=250&page=1", lists)
        self.assertIn("https://deodap.in/collections/delhi-best-selling/products.json?limit=250&page=1", lists)
        self.assertIn("https://deodap.in/collections/just-arrived/products.json?limit=250&page=1", lists)
        catalogue = platforms.load_catalogue(self.store, "deodap")
        self.assertEqual(len(catalogue["items"]), 7)
        bestseller = catalogue["items"]["10736207266102"]
        self.assertEqual(sorted(bestseller["roles"]), ["bestseller"])
        self.assertEqual(bestseller["roles"]["bestseller"]["via"], ["best-selling-products"])
        self.assertEqual(catalogue["items"]["15419393081654"]["roles"], {})
        self.assertTrue(catalogue["full_completed_at"] and catalogue["lists_completed_at"])
        self.assertEqual({row["handle"] for row in catalogue["collections"]}, {
            row["handle"] for row in fixture("deodap-collections-2026-10-14.json")["collections"]})
        self.assertIsNone(platforms.catalogue_wait(self.store, "deodap"))
        raws = platforms.raw_files(self.store, "deodap")
        self.assertEqual(len(raws), len(double.calls))
        saved = platforms.load_raw(self.store, "deodap", bestseller["source_ref"])
        self.assertEqual(saved["receipt"]["request"]["handle"], "best-selling-products")
        self.assertEqual(platforms.item_receipt(self.store, "deodap", bestseller)["id"], saved["receipt"]["id"])
        state = platforms.platform_state(self.store, "deodap")
        self.assertEqual((state["items"], state["ready"], state["label"]), (7, True, "DeoDap"))
        reads = load_json(pacing._path(self.pacing_root), 1024 * 1024)["reads"]["deodap"]
        self.assertEqual(len(reads), len(double.calls))

    def test_catalogue_waits_before_its_first_full_cycle(self):
        self.assertEqual(platforms.catalogue_wait(self.store, "deodap"), "DROPSHIP_CATALOGUE_PENDING")
        self.advance(self.feed(), limit=1)
        self.assertEqual(platforms.catalogue_wait(self.store, "deodap"), "DROPSHIP_CATALOGUE_PENDING")
        self.assertEqual(platforms.load_refresh(self.store, "deodap")["cycle"]["kind"], "full")

    def test_spacing_is_waited_out_inside_a_round_but_not_for_a_fixed_time(self):
        reader = self.feed()
        pauses = []
        clock = Clock(times=[START, START, later(START, 5), later(START, 5), later(START, 10)])
        with paced(clock):
            outcome = platforms.advance_platform(self.store, "deodap", reader, clock=clock, sleep=pauses.append, limit=2)
        self.assertEqual((outcome["reads"], pauses), (2, [5.0]))
        fixed = self.advance(self.feed(), as_of=later(START, 11), limit=3)
        self.assertEqual((fixed["state"], fixed["reason"], fixed["reads"]), ("waiting", "SIGNED_IN_READ_SPACING", 1))

    def test_daily_ceiling_stops_reads_with_a_calm_wait(self):
        with patch.object(pacing, "pacing_rules", return_value={"min_interval_seconds": 1, "daily_ceiling": 2}):
            outcome = self.advance(self.feed(), limit=5)
        self.assertEqual((outcome["state"], outcome["reason"], outcome["reads"]), ("waiting", "SIGNED_IN_DAILY_CEILING", 2))
        self.assertIsNotNone(outcome["next_retry_at"])
        self.assertEqual(platforms.load_refresh(self.store, "deodap")["wait"]["reason"], "SIGNED_IN_DAILY_CEILING")

    def test_refusals_back_off_longer_each_time_and_count_as_reads(self):
        double = FeedDouble(status=429)
        clock = Clock()
        first = self.advance(self.feed(fetcher=double), clock=clock)
        self.assertEqual((first["state"], first["reason"]), ("waiting", "DROPSHIP_FEED_REFUSED"))
        refresh = platforms.load_refresh(self.store, "deodap")
        self.assertEqual(timestamp(refresh["blocked_until"]) - timestamp(START), timedelta(seconds=300))
        held = self.advance(self.feed(fetcher=double), clock=Clock(later(START, 60)))
        self.assertEqual((held["state"], held["reads"], len(double.calls)), ("waiting", 0, 1))
        self.advance(self.feed(fetcher=double), clock=Clock(later(START, 400)))
        refresh = platforms.load_refresh(self.store, "deodap")
        self.assertEqual((refresh["failures"], timestamp(refresh["blocked_until"]) - timestamp(later(START, 400))),
                         (2, timedelta(seconds=3600)))
        self.assertEqual(len(platforms.raw_files(self.store, "deodap")), 2)

    def test_a_reader_that_could_not_start_uncounts_its_read(self):
        def never(url):
            return ({"schema_version": 2, "id": "x", "requested_url": url, "execution_stage": "not_dispatched",
                     "state": "failed", "reason": "DROPSHIP_FEED_UNAVAILABLE"}, None)
        outcome = self.advance(self.feed(fetcher=never))
        self.assertEqual((outcome["state"], outcome["reads"]), ("waiting", 0))
        self.assertEqual(load_json(pacing._path(self.pacing_root), 1024 * 1024)["reads"].get("deodap"), [])

    def test_next_full_cycle_drops_delisted_items_and_next_lists_cycle_drops_old_roles(self):
        _, _, clock = self.build()
        catalogue = platforms.load_catalogue(self.store, "deodap")
        self.assertIn("10736207266102", catalogue["items"])
        later_pages = deodap_pages(lists={"best-selling-products": {"products": [caddy()]}})
        reader = self.feed(fetcher=FeedDouble(later_pages))
        week = Clock(later(START, 8 * 86400))
        while self.advance(reader, clock=week, limit=50)["state"] not in ("current", "waiting"):
            pass
        catalogue = platforms.load_catalogue(self.store, "deodap")
        self.assertNotIn("10736207266102", catalogue["items"])  # only in the old bestseller list: delisted
        self.assertEqual(sorted(catalogue["items"]["777"]["roles"]), ["bestseller"])
        lists_only = deodap_pages(lists={"best-selling-products": {"products": []}})
        day = Clock(later(START, 9 * 86400 + 100))
        reader = self.feed(fetcher=FeedDouble(lists_only))
        while self.advance(reader, clock=day, limit=50)["state"] not in ("current", "waiting"):
            pass
        catalogue = platforms.load_catalogue(self.store, "deodap")
        self.assertEqual(catalogue["items"]["777"]["roles"], {})
        self.assertNotIn("best-selling-products", catalogue["items"]["777"]["collections"])

    def test_paged_lists_follow_up_to_their_page_limit(self):
        many = {"products": [caddy(1000 + n, title="Item " + str(n)) for n in range(250)]}
        pages = deodap_pages(lists={"best-selling-products": many})
        pages["https://deodap.in/collections/best-selling-products/products.json?limit=250&page=2"] = many
        pages["https://deodap.in/products.json?limit=250&page=1"] = many
        _, double, _ = self.build(pages=pages, limit=60)
        best = [url for url in double.calls if "best-selling-products" in url]
        self.assertEqual(len(best), 2)  # max_pages 2, even though page 2 is full again
        self.assertIn("https://deodap.in/products.json?limit=250&page=2", double.calls)

    def test_empty_full_cycle_keeps_waiting_instead_of_an_empty_platform(self):
        pages = {"https://deodap.in/collections.json?limit=250&page=1": {"collections": []}}
        outcome, _, _ = self.build(pages=pages)
        self.assertEqual((outcome["state"], outcome["reason"]), ("waiting", "DROPSHIP_CATALOGUE_EMPTY"))
        self.assertEqual(platforms.catalogue_wait(self.store, "deodap"), "DROPSHIP_CATALOGUE_EMPTY")

    def test_unparsable_feed_backs_off_instead_of_reading_every_round(self):
        double = FeedDouble()
        reader = self.feed(fetcher=lambda url: double(url)[:1] + ("[]",))
        outcome = self.advance(reader)
        self.assertEqual((outcome["state"], outcome["reason"], outcome["reads"]), ("waiting", "DROPSHIP_FEED_BODY_INVALID", 1))
        refresh = platforms.load_refresh(self.store, "deodap")
        self.assertEqual(timestamp(refresh["blocked_until"]) - timestamp(START), timedelta(seconds=300))
        self.assertEqual(platforms.load_catalogue(self.store, "deodap")["items"], {})
        again = self.advance(reader, clock=Clock(later(START, 60)))
        self.assertEqual((again["reads"], len(double.calls)), (0, 1))  # not re-read within the wait
        with self.assertRaisesRegex(SafetyError, "DROPSHIP_FEED_BODY_INVALID"):
            reader.parse({"kind": "collections", "page": 1}, {"receipt": {}, "body": "{}"})
        with self.assertRaisesRegex(SafetyError, "DROPSHIP_FEED_BODY_INVALID"):
            reader.parse({"kind": "invented", "page": 1}, {"receipt": {}, "body": '{"products": []}'})
        with self.assertRaisesRegex(SafetyError, "DROPSHIP_FEED_BODY_INVALID"):
            platforms.body_json({"body": None})

    def test_round_time_budget_and_owner_pause_stop_reads(self):
        ticks = iter([0, 100])
        outcome = self.advance(self.feed(), deadline=50, monotonic=lambda: next(ticks), limit=5)
        self.assertEqual((outcome["state"], outcome["reads"]), ("round_time_spent", 1))
        def paused():
            raise SafetyError("PROGRAMME_OWNER_PAUSED")
        outcome = self.advance(self.feed(), clock=Clock(later(START, 60)), before_read=paused)
        self.assertEqual((outcome["state"], outcome["reason"], outcome["reads"]), ("waiting", "PROGRAMME_OWNER_PAUSED", 0))

    def test_invalid_pacing_settings_are_raised_not_waited_out(self):
        with patch.object(pacing, "pacing_rules", side_effect=SafetyError("SIGNED_IN_PACING_SETTINGS_INVALID")):
            with self.assertRaisesRegex(SafetyError, "SIGNED_IN_PACING_SETTINGS_INVALID"):
                self.feed().read({"url": "https://deodap.in/collections.json"}, None, 1, START)

    def test_new_parser_version_replays_the_saved_feeds_without_reading(self):
        _, double, _ = self.build()
        before = platforms.load_catalogue(self.store, "deodap")
        reader = self.feed(fetcher=FeedDouble({}))
        with patch.object(feeds.FeedReader, "parser_version", "shopify-feed-v2"):
            outcome = self.advance(reader, clock=Clock(later(START, 3600)))
            replayed = platforms.load_catalogue(self.store, "deodap")
        self.assertEqual(outcome["state"], "current")
        self.assertEqual(reader.fetcher.calls, [])
        self.assertEqual(replayed["parser_version"], "shopify-feed-v2")
        self.assertEqual(set(replayed["items"]), set(before["items"]))
        self.assertEqual(replayed["items"]["10736207266102"]["roles"].keys(), before["items"]["10736207266102"]["roles"].keys())
        self.assertEqual(replayed["collections"], before["collections"])

    def test_replay_without_finished_cycles_is_empty_and_retention_prunes_old_days(self):
        catalogue = platforms.replay_platform(self.store, "deodap", self.feed())
        self.assertEqual((catalogue["items"], catalogue["parser_version"]), ({}, feeds.PARSER_VERSION))
        relative = platforms.save_raw(self.store, "deodap", "2026-09-01T00:00:00+00:00", 1, {"id": "x"}, "{}")
        folder = platforms.platform_folder(self.store, "deodap")
        old_read, new_read = folder / "reads/r000001", folder / "reads/r000002"
        old_read.mkdir(parents=True)
        new_read.mkdir()
        os.utime(old_read, (timestamp("2026-09-01T00:00:00+00:00").timestamp(),) * 2)
        self.assertEqual(platforms.prune_raw(self.store, "deodap", START, 21), 2)
        self.assertFalse((folder / relative).exists())
        self.assertEqual((old_read.exists(), new_read.exists()), (False, True))
        with self.assertRaisesRegex(SafetyError, "DROPSHIP_RAW_REFERENCE_INVALID"):
            platforms.load_raw(self.store, "deodap", "../catalogue.json")
        with self.assertRaisesRegex(SafetyError, "DROPSHIP_RAW_UNAVAILABLE"):
            platforms.load_raw(self.store, "deodap", "raw/2026-01-01/000001.json.gz")

    def test_saved_records_are_validated(self):
        folder = platforms.platform_folder(self.store, "deodap")
        folder.mkdir(parents=True)
        (folder / "catalogue.json").write_text('{"version": "other"}')
        with self.assertRaisesRegex(SafetyError, "DROPSHIP_CATALOGUE_INVALID"):
            platforms.load_catalogue(self.store, "deodap")
        (folder / "refresh.json").write_text('{"version": "other"}')
        with self.assertRaisesRegex(SafetyError, "DROPSHIP_REFRESH_RECORD_INVALID"):
            platforms.load_refresh(self.store, "deodap")

    def test_a_running_refresh_holds_the_platform_lock(self):
        from product_radar.storage import file_lock
        folder = platforms.platform_folder(self.store, "deodap")
        folder.mkdir(parents=True)
        with file_lock(folder / "refresh.lock", 0, "BUSY"):
            with self.assertRaisesRegex(SafetyError, "DROPSHIP_REFRESH_BUSY"):
                self.advance(self.feed())

    def test_baapstore_reads_its_trending_list(self):
        pages = {"https://baapstore.com/collections.json?limit=250&page=1": {"collections": [
                     {"id": 1, "handle": "trending-products", "title": "Trending Products", "products_count": 4}]},
                 "https://baapstore.com/products.json?limit=250&page=1": fixture("baapstore-trending-products-2026-10-14.json"),
                 "https://baapstore.com/collections/trending-products/products.json?limit=250&page=1":
                     fixture("baapstore-trending-products-2026-10-14.json")}
        outcome, _, _ = self.build(route="baapstore", pages=pages)
        self.assertEqual(outcome["state"], "current")
        catalogue = platforms.load_catalogue(self.store, "baapstore")
        self.assertEqual(len(catalogue["items"]), 4)
        self.assertTrue(all(sorted(item["roles"]) == ["trending"] for item in catalogue["items"].values()))
        with self.assertRaisesRegex(SafetyError, "DROPSHIP_ROUTE_INVALID"):
            feeds.FeedReader("dropdash", pacing_root=self.pacing_root)


class SignedInTests(PlatformCase):
    route = "dropship-india"

    def setUp(self):
        super().setUp()
        self.pages = fixture("signed-in-pages-synthetic-2026-10-14.json")
        self.calls, self.serve = [], {}

    def browser(self, url, expression, folder, **options):
        self.calls.append({"url": url, "expression": expression, "options": options})
        page = self.serve.get(url, "unrecognised")
        if page == "busy":
            return {"state": "waiting", "reason": "SIGNED_IN_BROWSER_BUSY", "navigation_started": False, "snapshot": None}
        if page == "broken":
            return {"state": "failed", "reason": "NATIVE_PUBLIC_DEADLINE", "navigation_started": None, "snapshot": None}
        if page == "garbled":
            (folder / "native_result.json").write_text("{not json")
            page = "catalogue"
        else:
            (folder / "native_result.json").write_text(json.dumps({"text": "Signed in as research.owner@example.com"}))
        snapshot = deepcopy(self.pages[page])
        snapshot["text"] += "\nAccount: research.owner@example.com"
        return {"state": "captured", "navigation_started": True, "snapshot": snapshot}

    def reader(self):
        return signed.SignedInReader(self.route, pacing_root=self.pacing_root, browser=self.browser)

    def sign_in(self):
        note_sign_in_window(self.pacing_root, True, opened=True, at=START)
        note_sign_in_window(self.pacing_root, False, at=START)

    def run_reads(self, count, start=START, step=31):
        clock = Clock(start, step)
        outcomes = [self.advance(self.reader(), self.route, clock=clock, limit=1) for _ in range(count)]
        return outcomes, clock

    def test_without_sign_in_the_platform_waits_calmly_and_nothing_is_opened(self):
        outcome = self.advance(self.reader(), self.route)
        self.assertEqual((outcome["state"], outcome["reason"]), ("waiting", "OWNER_SIGN_IN_REQUIRED"))
        self.assertIsNotNone(outcome["next_retry_at"])
        self.assertEqual(self.calls, [])
        self.assertEqual(platforms.catalogue_wait(self.store, self.route), "OWNER_SIGN_IN_REQUIRED")

    def test_signed_in_cycle_reads_catalogue_pages_lists_and_product_pages(self):
        self.sign_in()
        self.serve = {"https://dropshipindia.live/products": "catalogue",
                      "https://dropshipindia.live/products?page=2": "catalogue_page_2",
                      "https://dropshipindia.live/trending-products": "trending",
                      "https://dropshipindia.live/high-demand-products": "trending",
                      "https://dropshipindia.live/products/steel-draining-sink-caddy": "product",
                      "https://dropshipindia.live/products/kids-bath-toy-set": "product_sparse"}
        outcomes, clock = self.run_reads(5)
        self.assertEqual([row["state"] for row in outcomes], ["reading"] * 4 + ["current"])
        self.assertEqual(outcomes[-1]["reads"], 0)
        self.assertEqual([call["url"] for call in self.calls], [
            "https://dropshipindia.live/products", "https://dropshipindia.live/products?page=2",
            "https://dropshipindia.live/trending-products", "https://dropshipindia.live/high-demand-products"])
        self.assertTrue(all(call["options"] == {"profile": "signed-in", "root": self.pacing_root} for call in self.calls))
        catalogue = platforms.load_catalogue(self.store, self.route)
        self.assertEqual(catalogue["scope"], "sample")
        self.assertEqual(set(catalogue["items"]), {"steel-draining-sink-caddy", "kids-bath-toy-set", "folding-travel-pillow",
                                                   "best-selling-trending-lunch-box"})
        caddy_item = catalogue["items"]["steel-draining-sink-caddy"]
        self.assertEqual((caddy_item["title"], caddy_item["price"], caddy_item["mrp"], caddy_item["in_stock"]),
                         ("Steel Draining Sink Caddy", "149", "499", True))
        self.assertEqual(sorted(caddy_item["roles"]), ["high_demand", "trending"])
        self.assertEqual(sorted(catalogue["items"]["kids-bath-toy-set"]["roles"]), ["high_demand"])
        self.assertIs(catalogue["items"]["kids-bath-toy-set"]["in_stock"], False)
        # Words in a title are not list badges; a "/month" plan figure is never a price.
        lunch = catalogue["items"]["best-selling-trending-lunch-box"]
        self.assertEqual((lunch["roles"], lunch["price"]), ({}, "120"))
        self.assertIn("kitchen-tools", {row["handle"] for row in catalogue["collections"]})
        self.assertEqual(site_states(self.pacing_root)[self.route]["state"], "signed_in")
        # The next day's lists cycle also reads the listed items' product pages for their own stated terms.
        outcomes, _ = self.run_reads(6, start=later(START, 86400 + 300))
        catalogue = platforms.load_catalogue(self.store, self.route)
        self.assertIn("https://dropshipindia.live/products/steel-draining-sink-caddy", [call["url"] for call in self.calls])
        caddy_item = catalogue["items"]["steel-draining-sink-caddy"]
        self.assertEqual(caddy_item["terms"], "Free shipping across India; dispatch within 24 hours; COD available; "
                                              "Returns accepted within 7 days")
        self.assertEqual((caddy_item["title"], caddy_item["price"], caddy_item["mrp"]), ("Steel Draining Sink Caddy", "149", "499"))
        # A sparse product page leaves the card's price, images and stock in place; footer text is never terms.
        kids = catalogue["items"]["kids-bath-toy-set"]
        self.assertEqual((kids["price"], kids["in_stock"], kids["terms"]), ("99", False, None))
        self.assertIn("product-page", kids["collections"])
        self.assertEqual(sorted(kids["roles"]), ["high_demand"])
        # A sampled catalogue never drops an item it did not see again.
        self.serve = {"https://dropshipindia.live/products": "catalogue_page_2",
                      "https://dropshipindia.live/trending-products": "trending",
                      "https://dropshipindia.live/high-demand-products": "trending"}
        self.run_reads(6, start=later(START, 8 * 86400))
        self.assertIn("kids-bath-toy-set", platforms.load_catalogue(self.store, self.route)["items"])

    def test_plan_walls_and_redirects_wait_a_day_and_never_click(self):
        self.sign_in()
        self.serve = {"https://dropshipindia.live/products": "plan_wall"}
        outcomes, _ = self.run_reads(1)
        self.assertEqual((outcomes[0]["state"], outcomes[0]["reason"]), ("waiting", "DROPSHIP_PLAN_REQUIRED"))
        refresh = platforms.load_refresh(self.store, self.route)
        self.assertEqual(timestamp(refresh["blocked_until"]) - timestamp(START), timedelta(days=1))
        self.assertEqual(platforms.platform_lines(self.store), [
            "Dropship India shows a paid plan or membership page instead of products. The program never pays or accepts a "
            "plan; it looks again in a day. Nothing to do unless you choose to upgrade."])
        self.serve = {"https://dropshipindia.live/products": "redirected_to_plans"}
        outcomes, _ = self.run_reads(1, start=later(START, 86400 + 10))
        self.assertEqual(outcomes[0]["reason"], "DROPSHIP_PLAN_REQUIRED")
        self.assertEqual(len(self.calls), 2)
        self.assertEqual(platforms.catalogue_wait(self.store, self.route), "DROPSHIP_PLAN_REQUIRED")

    def test_a_redirect_elsewhere_is_refused(self):
        self.sign_in()
        self.serve = {"https://dropshipindia.live/products": "catalogue"}
        self.pages["catalogue"]["url"] = "https://dropshipindia.live/products?sort=new"
        outcomes, _ = self.run_reads(1)
        self.assertEqual(outcomes[0]["reason"], "DROPSHIP_PAGE_REDIRECTED")
        self.assertEqual(platforms.load_catalogue(self.store, self.route)["items"], {})

    def test_account_details_are_removed_and_unreadable_saved_results_quarantined(self):
        self.sign_in()
        self.serve = {"https://dropshipindia.live/products": "catalogue"}
        self.run_reads(1)
        reads = platforms.platform_folder(self.store, self.route) / "reads"
        saved = (reads / "r000001" / "native_result.json").read_text()
        self.assertNotIn("research.owner@example.com", saved)
        self.assertIn("[email redacted]", saved)
        raw = platforms.load_raw(self.store, self.route, platforms.raw_files(self.store, self.route)[0])
        self.assertNotIn("research.owner@example.com", raw["body"])
        self.serve = {"https://dropshipindia.live/products?page=2": "garbled"}
        self.run_reads(1, start=later(START, 40))
        self.assertFalse((reads / "r000002" / "native_result.json").exists())
        self.assertEqual(len(list((reads / "signed-in-quarantine").iterdir())), 1)

    def test_sign_in_wall_records_an_expired_sign_in(self):
        self.sign_in()
        self.serve = {"https://dropshipindia.live/products": "catalogue"}
        self.run_reads(1)
        self.serve = {"https://dropshipindia.live/products?page=2": "wall"}
        outcomes, _ = self.run_reads(1, start=later(START, 40))
        self.assertEqual((outcomes[0]["state"], outcomes[0]["reason"]), ("waiting", "OWNER_SIGN_IN_REQUIRED"))
        self.assertEqual(site_states(self.pacing_root)[self.route]["state"], "expired")
        self.assertIsNotNone(platforms.load_refresh(self.store, self.route)["cycle"])  # resumes after the owner signs in

    def test_unrecognised_layout_drops_the_cycle_instead_of_an_empty_catalogue(self):
        self.sign_in()
        outcomes, _ = self.run_reads(1)
        self.assertEqual((outcomes[0]["state"], outcomes[0]["reason"]), ("waiting", "DROPSHIP_PAGE_LAYOUT_UNRECOGNISED"))
        refresh = platforms.load_refresh(self.store, self.route)
        self.assertIsNone(refresh["cycle"])
        self.assertEqual(platforms.catalogue_wait(self.store, self.route), "DROPSHIP_PAGE_LAYOUT_UNRECOGNISED")

    def test_a_busy_browser_uncounts_the_read_and_a_broken_one_is_a_failure(self):
        self.sign_in()
        self.serve = {"https://dropshipindia.live/products": "busy"}
        outcomes, _ = self.run_reads(1)
        self.assertEqual((outcomes[0]["state"], outcomes[0]["reason"], outcomes[0]["reads"]), ("waiting", "SIGNED_IN_BROWSER_BUSY", 0))
        self.assertEqual(load_json(pacing._path(self.pacing_root), 1024 * 1024)["reads"][self.route], [])
        self.serve = {"https://dropshipindia.live/products": "broken"}
        outcomes, _ = self.run_reads(1, start=later(START, 600))
        self.assertEqual((outcomes[0]["state"], outcomes[0]["reason"]), ("waiting", "DROPSHIP_PAGE_READ_FAILED"))
        self.assertEqual(len(load_json(pacing._path(self.pacing_root), 1024 * 1024)["reads"][self.route]), 1)

    def test_a_signed_in_page_starts_only_if_its_worst_case_fits_the_round(self):
        from product_radar.native_public_browser import signed_in_budget
        self.sign_in()
        reader = self.reader()
        self.assertEqual(reader.worst_case_seconds, signed_in_budget())
        # 100 seconds left: less than one worst-case signed-in read (feeds keep the plain deadline).
        outcome = self.advance(reader, self.route, deadline=100, monotonic=lambda: 0, limit=1)
        self.assertEqual((outcome["state"], outcome["reads"], self.calls), ("round_time_spent", 0, []))
        outcome = self.advance(reader, self.route, deadline=signed_in_budget() + 1, monotonic=lambda: 0, limit=1)
        self.assertEqual((outcome["reads"], len(self.calls)), (1, 1))
        self.assertGreaterEqual(platforms.settings()["refresh"]["seconds_per_round"], signed_in_budget())

    def test_only_signed_in_waits_are_calm_other_refusals_are_failures(self):
        self.sign_in()
        def refuse(*args, **options):
            raise SafetyError("SIGNED_IN_BROWSER_NOT_INSTALLED")
        reader = signed.SignedInReader(self.route, pacing_root=self.pacing_root, browser=refuse)
        outcome = self.advance(reader, self.route)
        self.assertEqual((outcome["state"], outcome["reason"], outcome["reads"]), ("waiting", "SIGNED_IN_BROWSER_NOT_INSTALLED", 1))
        self.assertIsNotNone(platforms.load_refresh(self.store, self.route)["blocked_until"])

        def busy(*args, **options):
            raise SafetyError("SIGNED_IN_BROWSER_BUSY")
        outcome = self.advance(signed.SignedInReader(self.route, pacing_root=self.pacing_root, browser=busy), self.route,
                               clock=Clock(later(START, 3600)))
        self.assertEqual((outcome["reason"], outcome["reads"]), ("SIGNED_IN_BROWSER_BUSY", 0))

    def test_browser_start_and_read_errors_are_handled_like_indiamart(self):
        self.sign_in()
        def unstartable(*args, **options):
            raise OSError("no powershell")
        outcome = self.advance(signed.SignedInReader(self.route, pacing_root=self.pacing_root, browser=unstartable), self.route)
        # Not a signed-in wait (CALM_CODES): a failure that backs off, like Trends and IndiaMART.
        self.assertEqual((outcome["state"], outcome["reason"], outcome["reads"]), ("waiting", "SIGNED_IN_BROWSER_START_FAILED", 1))
        self.assertIsNotNone(platforms.load_refresh(self.store, self.route)["blocked_until"])
        self.assertEqual(platforms.load_refresh(self.store, self.route)["failures"], 1)

        def interrupted(url, expression, folder, **options):
            (folder / "owned_process.json").write_text("{}")
            raise OSError("pipe closed")
        outcome = self.advance(signed.SignedInReader(self.route, pacing_root=self.pacing_root, browser=interrupted), self.route,
                               clock=Clock(later(START, 3600)))
        self.assertEqual((outcome["reason"], outcome["reads"]), ("SIGNED_IN_BROWSER_READ_FAILED", 1))

    def test_reading_only_no_click_no_paid_or_contact_page(self):
        script = signed.expression(self.route)
        for forbidden in (".click(", "submit", "dispatchEvent", ".focus(", "scroll", "location.assign", "location.href =",
                          "fetch(", "XMLHttpRequest", ".value =", "localStorage", "cookie"):
            self.assertNotIn(forbidden, script)
        for url in ("https://dropshipindia.live/checkout", "https://dropshipindia.live/cart/add",
                    "https://dropshipindia.live/plans", "https://dropshipindia.live/pricing",
                    "https://dropshipindia.live/subscribe?plan=gold", "https://dropshipindia.live/products/x?add_to_store=1",
                    "https://dropshipindia.live/contact-us", "https://dropshipindia.live/wallet/recharge",
                    "https://dropshipindia.live/account/settings", "https://dropshipindia.live/orders/1",
                    "https://dropshipindia.live/upgrade", "https://dropshipindia.live/premium/products",
                    "https://dropshipindia.live/products/x/buy-now", "https://dropshipindia.live/free-trial",
                    "https://dropshipindia.live/membership", "https://dropshipindia.live/place-order",
                    "https://dropshipindia.live/winning-products",  # not a configured page: never opened
                    "https://dropshipindia.live/products?page=2&plan=gold", "https://dropshipindia.live/products?sort=new",
                    "https://elsewhere.example/products", "https://dropshipindia.live/products#top"):
            with self.assertRaisesRegex(SafetyError, "DROPSHIP_PAGE_NOT_READ_ONLY"):
                signed.read_only_url(self.route, url)
        for url in ("https://dropshipindia.live/products/kids-pay-pretend-set", "https://dropshipindia.live/products",
                    "https://dropshipindia.live/products?page=3", "https://dropshipindia.live/trending-products?page=2"):
            self.assertEqual(signed.read_only_url(self.route, url), url)
        # A next-page link to a forbidden step is simply not followed.
        parsed = signed.parse_snapshot(self.route, {"kind": "page", "handle": "page-catalogue", "role": "catalogue", "page": 1,
                                                    "max_pages": 5, "url": "https://dropshipindia.live/products"},
                                       self.pages["catalogue_page_2"])
        self.assertEqual(parsed["follow"], [])

    def test_markers_and_card_parsing(self):
        self.assertIs(signed.signed_in_marker(self.pages["wall"]), False)
        self.assertIs(signed.signed_in_marker(dict(self.pages["unrecognised"], url="https://dropshipindia.live/signin")), False)
        self.assertIs(signed.signed_in_marker(self.pages["catalogue"]), True)
        self.assertIsNone(signed.signed_in_marker(self.pages["unrecognised"]))
        self.assertIsNone(signed.signed_in_marker(None))
        parsed = signed.parse_snapshot(self.route, {"kind": "page", "handle": "page-catalogue", "role": "catalogue", "page": 1,
                                                    "max_pages": 5, "url": "https://dropshipindia.live/products"},
                                       self.pages["catalogue"])
        self.assertEqual([row["item_id"] for row in parsed["items"]], ["steel-draining-sink-caddy", "kids-bath-toy-set",
                                                                       "best-selling-trending-lunch-box"])
        self.assertEqual([row["badges"] for row in parsed["items"]], [["trending"], ["high_demand"], []])
        self.assertEqual([row["title"] for row in parsed["items"]], ["Steel Draining Sink Caddy", "Kids Bath Toy Set",
                                                                     "Best Selling Trending Lunch Box"])
        self.assertEqual(signed.prices("Plans from ₹ 499/month\nOnly ₹ 1,299 per year\nPrice ₹ 99"), ("99", None))
        self.assertIs(signed.plan_wall({"kind": "page"}, self.pages["catalogue"]), False)  # a banner beside products
        self.assertIs(signed.plan_wall({"kind": "page"}, self.pages["plan_wall"]), True)
        self.assertEqual(parsed["follow"][0]["url"], "https://dropshipindia.live/products?page=2")
        self.assertEqual([row["handle"] for row in parsed["collections"]], ["page-catalogue", "kitchen-tools"])
        product = signed.parse_snapshot(self.route, {"kind": "product", "item_id": "steel-draining-sink-caddy", "role": None,
                                                     "page": 1, "max_pages": 1, "title": "Card title",
                                                     "url": "https://dropshipindia.live/products/steel-draining-sink-caddy"},
                                        dict(self.pages["product"], product={"title": " ", "text": "₹ 50", "terms": []}))
        # The card's title, never the browser tab's document title.
        self.assertEqual((product["items"][0]["title"], product["items"][0]["price"]), ("Card title", "50"))
        with self.assertRaisesRegex(SafetyError, "DROPSHIP_FEED_BODY_INVALID"):
            self.reader().parse({"kind": "page"}, {"receipt": {}, "body": "[]"})
        with self.assertRaisesRegex(SafetyError, "DROPSHIP_ROUTE_INVALID"):
            signed.SignedInReader("deodap", pacing_root=self.pacing_root)

    def test_dropdash_uses_its_own_pages_and_product_addresses(self):
        reader = signed.SignedInReader("dropdash", pacing_root=self.pacing_root, browser=self.browser)
        plan = reader.plan_full(platforms.platform_settings("dropdash"), platforms.load_catalogue(self.store, "dropdash"))
        self.assertEqual([row["role"] for row in plan], ["catalogue", "winning", "trending"])
        self.assertTrue(re.fullmatch(platforms.platform_settings("dropdash")["product_path"],
                                     "https://app.dropdash.co/products/sink-caddy"))


if __name__ == "__main__":
    unittest.main()
