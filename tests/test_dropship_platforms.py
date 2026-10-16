"""The three dropshipping platforms' saved catalogues (Plan R10 W4).

DeoDap and BaapStore feed pages are trimmed real public Shopify feeds saved on
14 October 2026 (tests/fixtures/dropship_platforms); Dropdash's signed-in pages
are trimmed real reads of 16 October 2026 (home page, its "Profitable Products"
list, one product page; the account holder's name and billing prompt removed)
plus invented edge cases (walls, redirects, a changed layout). Nothing here
opens a network connection or a browser: the feed
fetcher, the address resolver and the signed-in browser are offline doubles,
and pacing and sign-ins are local records in a temporary folder.
"""

from copy import deepcopy
from datetime import timedelta
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
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
            # A signed-in platform names every page selector (text, or null for the generic guess); a feed has none.
            lambda v: v["platforms"]["dropdash"].pop("selectors"),
            lambda v: v["platforms"]["dropdash"].update(selectors=[]),
            lambda v: v["platforms"]["dropdash"]["selectors"].pop("card"),
            lambda v: v["platforms"]["dropdash"]["selectors"].update(extra=".x"),
            lambda v: v["platforms"]["dropdash"]["selectors"].update(card=""),
            lambda v: v["platforms"]["dropdash"]["selectors"].update(card=".a\n.b"),
            lambda v: v["platforms"]["dropdash"]["selectors"].update(card="x" * 301),
            lambda v: v["platforms"]["dropdash"]["selectors"].update(card=7),
            lambda v: v["platforms"]["deodap"].update(selectors={}),
            # At most one capturing group in a product address: the platform's own product number.
            lambda v: v["platforms"]["dropdash"].update(product_path=r"^https://app\.dropdash\.co/p/([0-9]+)/([0-9]+)$"),
            lambda v: v["platforms"]["dropdash"].update(product_path="(unclosed"),
            # Sales sections: a list of the platform's own category addresses, each named once.
            lambda v: v["platforms"]["dropdash"].pop("sales_sections"),
            lambda v: v["platforms"]["dropdash"].update(sales_sections=WINNING),
            lambda v: v["platforms"]["dropdash"].update(sales_sections=["https://elsewhere.example/products?categoryId=1"]),
            lambda v: v["platforms"]["dropdash"].update(sales_sections=[WINNING, WINNING]),
            lambda v: v["platforms"]["dropdash"].update(sales_sections=[WINNING + "#top"]),
            lambda v: v["platforms"]["dropdash"].update(sales_sections=[WINNING + " x"]),
            lambda v: v["platforms"]["dropdash"].update(sales_sections=[7]),
            lambda v: v["platforms"]["dropdash"].update(sales_sections=[{"url": WINNING}]),
            lambda v: v["platforms"]["deodap"].update(sales_sections=[]),
        ]
        for change in changes:
            broken = deepcopy(value)
            change(broken)
            with self.assertRaisesRegex(SafetyError, "DROPSHIP_SETTINGS_INVALID"):
                platforms.validate_settings(broken)
        relaxed = deepcopy(value)
        relaxed["platforms"]["dropdash"]["selectors"].update(card=None, product_terms=None)  # the generic guess
        self.assertIsNone(platforms.validate_settings(relaxed)["platforms"]["dropdash"]["selectors"]["card"])
        with self.assertRaisesRegex(SafetyError, "DROPSHIP_ROUTE_INVALID"):
            platforms.platform_settings("amazon-search")


    def test_product_address_patterns_are_anchored_and_read_alike_in_python_and_javascript(self):
        value = platforms.settings()
        live = value["platforms"]["dropdash"]["product_path"]
        self.assertTrue(live.startswith("^" + re.escape("https://app.dropdash.co") + "/"))  # the live settings pass
        rejected = [
            r"^https://app.dropdash.co/productDetails\?id=([0-9]+)$",  # the platform's address not escaped
            r"https://app\.dropdash\.co/productDetails\?id=([0-9]+)$",  # not anchored at the start
            r"^https://app\.dropdash\.co/productDetails\?id=([0-9]+)",  # nor at the end
            r"^https://app\.dropdash\.co/productDetails\?id=([0-9]+)\$",  # an escaped "$" is no anchor
            r"^https://app\.dropdash\.co/x$|^https://elsewhere\.example/x$",  # a top-level "|" escapes the anchors
            r"^https://app\.dropdash\.co/x|.*$",
            r"^https://elsewhere\.example/productDetails\?id=([0-9]+)$",
            r"^https://app\.dropdash\.co/$",  # nothing after the platform's address
            r"^https://app\.dropdash\.co/productDetails\?id=(?P<id>[0-9]+)$",  # Python-only named group
            r"^https://app\.dropdash\.co/(?i)productdetails\?id=([0-9]+)$",  # Python-only inline flag
            r"^https://app\.dropdash\.co/productDetails\?id=([0-9]+)\Z$",
            r"^https://app\.dropdash\.co/\AproductDetails\?id=([0-9]+)$",
            r"^https://app\.dropdash\.co/productDetails\?id=([0-9]++)$",  # possessive (Python 3.11+ only)
            r"^https://app\.dropdash\.co/productDetails\?id=((?>[0-9]+))$",  # atomic group
            r"^https://app\.dropdash\.co/productDetails\?id=(\d+)$",  # \d reads other scripts' digits in Python
            r"^https://app\.dropdash\.co/productDetails\?id=([0-9]+)(?=&)$",  # look-ahead
            r"^https://app\.dropdash\.co/p/([0-9]+)/\1$",  # back-reference
            r"^https://app\.dropdash\.co/productDetails\?id=(*[0-9])$",  # nothing to repeat
            r"^https://app\.dropdash\.co/productDetails\?id=()[0-9]$",  # an empty group
            r"^https://app\.dropdash\.co/productDetails\?id=([0-9]+$",  # unclosed
            r"^https://app\.dropdash\.co/productDetails\?id=[0-9]+)$",
            r"^https://app\.dropdash\.co/product Details$",
            r"^https://app\.dropdash\.co/x{1,2}{3}$",
            r"^https://app\.dropdash\.co/x[]$",
            r"^https://app\.dropdash\.co/x[a&&b]$",  # kept for set operations in a future Python
            "^https://app\\.dropdash\\.co/" + "x" * 500 + "$",
            7, None]
        for pattern in rejected:
            broken = deepcopy(value)
            broken["platforms"]["dropdash"]["product_path"] = pattern
            with self.subTest(pattern=pattern), self.assertRaisesRegex(SafetyError, "DROPSHIP_SETTINGS_INVALID"):
                platforms.validate_settings(broken)
        accepted = [live, r"^https://app\.dropdash\.co/(?:product|products)/[A-Za-z0-9._~%-]+/?$",
                    r"^https://app\.dropdash\.co/p/([0-9]{1,9})(?:/[a-z0-9-]*)?$",
                    r"^https://app\.dropdash\.co/item-[^/&]+?\.html$"]
        for pattern in accepted:
            fine = deepcopy(value)
            fine["platforms"]["dropdash"]["product_path"] = pattern
            self.assertEqual(platforms.validate_settings(fine)["platforms"]["dropdash"]["product_path"], pattern)
        # Each accepted pattern means the same in the page's JavaScript (new RegExp) as in Python.
        node = shutil.which("node")
        if not node:
            self.skipTest("Node is needed to read the patterns as the page's JavaScript does")
        urls = [PRODUCT, PRODUCT + "&x=1", "https://app.dropdash.co/productDetails?id=0&sellerId=1",
                "https://app.dropdash.co/products/sink-caddy", "https://app.dropdash.co/product/", "https://app.dropdash.co/p/12",
                "https://app.dropdash.co/p/12/steel-caddy", "https://app.dropdash.co/item-caddy.html",
                "https://app.dropdash.co/item-a/b.html", "https://elsewhere.example/p/12"]
        script = ("const [patterns, urls] = JSON.parse(process.argv[1]);"
                  "console.log(JSON.stringify(patterns.map(p => urls.map(u => new RegExp(p).test(u)))));")
        reply = subprocess.run([node, "-e", script, json.dumps([accepted, urls])], capture_output=True, text=True,
                               timeout=10, check=True)
        self.assertEqual(json.loads(reply.stdout), [[bool(re.fullmatch(p, u)) for u in urls] for p in accepted])


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

    def test_the_body_is_read_after_http_client_hands_the_socket_to_the_response(self):
        # Real http.client sets connection.sock to None in getresponse() after
        # "Connection: close"; the reader must keep the socket it sent on.
        timeouts = []
        base = self.connection(self.Response(200, b'{"products": [1]}'), [])

        class ClosingConnection(base):
            def __init__(self, *args):
                super().__init__(*args)
                self.sock = type("Sock", (), {"settimeout": lambda _, value: timeouts.append(value)})()

            def getresponse(self):
                found = super().getresponse()
                self.sock = None
                return found
        receipt, body = feeds.fetch_feed("https://deodap.in/products.json", resolver=self.resolver,
                                         connection_factory=ClosingConnection)
        self.assertEqual((receipt["state"], json.loads(body)), ("read", {"products": [1]}))
        self.assertTrue(timeouts)

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


HOME = "https://app.dropdash.co/home"
WINNING = "https://app.dropdash.co/products?categoryId=288"
PRODUCT = "https://app.dropdash.co/productDetails?id=13227&sellerId=1405"


def page_request(role="catalogue", url=HOME, max_pages=1):
    return {"kind": "page", "handle": "page-" + role, "role": role, "page": 1, "max_pages": max_pages, "url": url}


def product_request(item_id="13227", url=PRODUCT, title="Card title"):
    return {"kind": "product", "item_id": item_id, "handle": "product-page", "role": None, "page": 1, "max_pages": 1,
            "title": title, "url": url}


class SignedInTests(PlatformCase):
    """Dropdash: trimmed real signed-in pages (16 October 2026) and invented edge cases."""

    route = "dropdash"

    def setUp(self):
        super().setUp()
        self.pages = dict(fixture("dropdash-signed-in-pages-2026-10-16.json"),
                          **fixture("dropdash-signed-in-edge-cases-synthetic-2026-10-16.json"))
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
            page = "category_288"
        else:
            (folder / "native_result.json").write_text(json.dumps({"text": "Signed in as research.owner@example.com"}))
        snapshot = deepcopy(self.pages[page])
        snapshot["url"] = snapshot["url"] or url
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

    def test_signed_in_cycle_reads_the_home_page_the_winning_list_and_its_product_pages(self):
        self.sign_in()
        self.serve = {HOME: "home", WINNING: "category_288"}
        outcomes, _ = self.run_reads(3)
        self.assertEqual([row["state"] for row in outcomes], ["reading", "reading", "current"])
        self.assertEqual(outcomes[-1]["reads"], 0)
        self.assertEqual([call["url"] for call in self.calls], [HOME, WINNING])
        self.assertTrue(all(call["options"] == {"profile": "signed-in", "root": self.pacing_root} for call in self.calls))
        self.assertTrue(all(call["expression"] == signed.expression(self.route) for call in self.calls))
        catalogue = platforms.load_catalogue(self.store, self.route)
        self.assertEqual(catalogue["scope"], "sample")
        # 30 home cards and 8 "Profitable Products" cards, 6 of which are also on the home page.
        self.assertEqual(len(catalogue["items"]), 32)
        band = catalogue["items"]["12344"]
        self.assertEqual((band["title"], band["url"], band["price"], band["mrp"], band["in_stock"], band["tags"], band["roles"]),
                         ("Figure 8 Workout Resistance Band for Yoga (Pack of 1)",
                          "https://app.dropdash.co/productDetails?id=12344&sellerId=1627", "220", "249", True,
                          ["Testing Products"], {}))
        self.assertEqual(band["images"], ["https://sp-api.dropdash.co/upload/ProductImages/72dd30bc-443a-4ec5-bae0-485016c418c6.jpg"])
        self.assertEqual(band["price_basis"], platforms.platform_settings(self.route)["price_basis"])
        self.assertIs(catalogue["items"]["2644"]["in_stock"], False)  # its stock count is 0
        self.assertEqual((catalogue["items"]["2301"]["price"], catalogue["items"]["2301"]["mrp"]), ("385", None))  # no MRP shown
        # The winning list ("Profitable Products"); its cards on the home page keep their section.
        glue = catalogue["items"]["9240"]
        self.assertEqual((glue["price"], glue["mrp"], sorted(glue["roles"]), glue["tags"]),
                         ("101", "201", ["winning"], ["Profitable Products"]))
        self.assertEqual(sorted(glue["collections"]), ["page-catalogue", "page-winning"])
        resin = catalogue["items"]["12607"]  # only on the list page
        self.assertEqual((sorted(resin["roles"]), resin["tags"], resin["in_stock"]), (["winning"], [], True))
        listed = sorted(item_id for item_id, item in catalogue["items"].items() if item["roles"])
        self.assertEqual(listed, sorted(["9240", "9210", "9212", "9280", "9251", "9274", "12607", "9576"]))
        # The home page's 21 category links less its "Testing Products" sales section, and the winning
        # list's own page. "Profitable Products" is that list (its role); the home page is no category.
        collections = {row["handle"]: row for row in catalogue["collections"]}
        self.assertEqual(len(collections), 20 + 1)
        self.assertEqual((collections["products-288"]["title"], collections["products-288"]["url"], collections["products-288"]["roles"]),
                         ("Profitable Products", WINNING, ["winning"]))
        self.assertEqual(collections["page-winning"]["roles"], ["winning"])
        self.assertNotIn("page-catalogue", collections)
        self.assertNotIn("products-282", collections)
        self.assertEqual(collections["products-63"], {"handle": "products-63", "title": "Home Decor & Lighting",
                                                      "url": "https://app.dropdash.co/products?categoryId=63",
                                                      "products_count": None, "roles": [], "parent": None})
        full_collections = deepcopy(catalogue["collections"])
        # Product-map material: real categories only, never the home page, a sales section or a list.
        from product_radar import map_materials
        titles = [row["label"] for row in map_materials._platform_rows(self.store, self.route, 1)
                  if row["key"].startswith("dropdash:collection:")]
        self.assertEqual(len(titles), 19)
        self.assertIn("Home Decor & Lighting", titles)
        for title in ("Catalogue page", "Testing Products", "Profitable Products", "Winning page"):
            self.assertNotIn(title, titles)
        self.assertEqual(site_states(self.pacing_root)[self.route]["state"], "signed_in")
        # The next day's lists cycle reads the list again, then each listed item's own product page.
        product_pages = {catalogue["items"][item_id]["url"] for item_id in listed}
        self.serve.update({url: "product_sparse" for url in product_pages})
        outcomes, _ = self.run_reads(10, start=later(START, 86400 + 300))
        self.assertEqual([row["state"] for row in outcomes], ["reading"] * 9 + ["current"])
        self.assertEqual(self.calls[2]["url"], WINNING)
        self.assertEqual({call["url"] for call in self.calls[3:]}, product_pages)
        catalogue = platforms.load_catalogue(self.store, self.route)
        glue = catalogue["items"]["9240"]
        # A product page that shows almost nothing leaves the card's title, price, stock and images in place.
        self.assertEqual((glue["title"], glue["price"], glue["mrp"], glue["in_stock"], glue["terms"]),
                         ("Invisible Waterproof Glue", "101", "201", True, None))
        self.assertEqual(len(glue["images"]), 1)
        self.assertIn("product-page", glue["collections"])
        self.assertEqual(sorted(glue["roles"]), ["winning"])
        # A lists cycle reads only the list pages: the full cycle's category list stays whole,
        # also when the catalogue is rebuilt from the saved reads.
        self.assertEqual(catalogue["collections"], full_collections)
        self.assertEqual(platforms.replay_platform(self.store, self.route, self.reader())["collections"], full_collections)
        # A sampled catalogue never drops an item it did not see again.
        self.pages["home_short"] = dict(self.pages["home"], cards=self.pages["home"]["cards"][:6])
        self.serve = {HOME: "home_short", WINNING: "category_288"}
        self.run_reads(3, start=later(START, 8 * 86400))
        self.assertEqual([call["url"] for call in self.calls[-2:]], [HOME, WINNING])
        self.assertIn("2301", platforms.load_catalogue(self.store, self.route)["items"])

    def test_a_product_page_gives_its_own_price_mrp_stock_and_stated_terms(self):
        page = self.pages["product_13227"]
        item = signed.parse_snapshot(self.route, product_request(), page)["items"][0]
        self.assertEqual({name: item[name] for name in ("item_id", "title", "url", "price", "mrp", "in_stock", "terms")},
                         {"item_id": "13227", "title": "Posture Corrector Back Straight And Shoulder Support Belt",
                          "url": PRODUCT, "price": "240", "mrp": "299", "in_stock": True,
                          "terms": "Including GST and Shipping Charges; RTO Cost 70 per return; Delivery Rate Low; "
                                   "Dispatch in 24 hrs"})
        # The same product's home card shows the same figures.
        card = next(row for row in signed.parse_snapshot(self.route, page_request(), self.pages["home"])["items"]
                    if row["item_id"] == "13227")
        self.assertEqual((card["price"], card["mrp"], card["in_stock"], card["tags"]), ("240", "299", True, ["Fitness & Wellness"]))
        sold_out = dict(page, product=dict(page["product"], stock="0 units"))
        self.assertIs(signed.parse_snapshot(self.route, product_request(), sold_out)["items"][0]["in_stock"], False)
        # Without its own price element, the block's text; without a title, the card's.
        textual = dict(page, product={"title": " ", "text": "Dropship price ₹ 50\nMRP ₹ 80\nIn stock", "terms": []})
        item = signed.parse_snapshot(self.route, product_request(), textual)["items"][0]
        self.assertEqual((item["title"], item["price"], item["mrp"], item["in_stock"], item["terms"]), ("Card title", "50", "80", True, None))
        # Read through the signed-in reader, the saved body gives the same item.
        self.sign_in()
        self.serve = {PRODUCT: "product_13227"}
        reader = self.reader()
        with paced(Clock(START)):
            result = reader.read(product_request(), self.store, 1, START)
        self.assertEqual((result["state"], result["reason"], result["receipt"]["final_url"]), ("read", None, PRODUCT))
        self.assertNotIn("research.owner@example.com", result["body"])
        self.assertEqual(reader.parse(product_request(), {"body": result["body"]})["items"][0]["price"], "240")

    def test_plan_walls_and_redirects_wait_a_day_and_never_click(self):
        self.sign_in()
        self.serve = {HOME: "plan_wall"}
        outcomes, _ = self.run_reads(1)
        self.assertEqual((outcomes[0]["state"], outcomes[0]["reason"]), ("waiting", "DROPSHIP_PLAN_REQUIRED"))
        refresh = platforms.load_refresh(self.store, self.route)
        self.assertEqual(timestamp(refresh["blocked_until"]) - timestamp(START), timedelta(days=1))
        self.assertEqual(platforms.platform_lines(self.store), [
            "Dropdash shows a paid plan or membership page instead of products. The program never pays or accepts a "
            "plan; it looks again in a day. Nothing to do unless you choose to upgrade."])
        self.serve = {HOME: "redirected_to_plans"}
        outcomes, _ = self.run_reads(1, start=later(START, 86400 + 10))
        self.assertEqual(outcomes[0]["reason"], "DROPSHIP_PLAN_REQUIRED")
        self.assertEqual(len(self.calls), 2)
        self.assertEqual(platforms.catalogue_wait(self.store, self.route), "DROPSHIP_PLAN_REQUIRED")

    def test_a_redirect_elsewhere_is_refused(self):
        self.sign_in()
        self.serve = {HOME: "home"}
        self.pages["home"]["url"] = "https://app.dropdash.co/home?sort=new"
        outcomes, _ = self.run_reads(1)
        self.assertEqual(outcomes[0]["reason"], "DROPSHIP_PAGE_REDIRECTED")
        self.assertEqual(platforms.load_catalogue(self.store, self.route)["items"], {})
        self.assertEqual(platforms.platform_lines(self.store), [
            "Dropdash sent one of its product lists to a different page; the program waits and tries again "
            "(at most a day). If this keeps showing, the list may have moved."])

    def test_account_details_are_removed_and_unreadable_saved_results_quarantined(self):
        self.sign_in()
        self.serve = {HOME: "home"}
        self.run_reads(1)
        reads = platforms.platform_folder(self.store, self.route) / "reads"
        saved = (reads / "r000001" / "native_result.json").read_text()
        self.assertNotIn("research.owner@example.com", saved)
        self.assertIn("[email redacted]", saved)
        raw = platforms.load_raw(self.store, self.route, platforms.raw_files(self.store, self.route)[0])
        self.assertNotIn("research.owner@example.com", raw["body"])
        self.serve = {WINNING: "garbled"}
        self.run_reads(1, start=later(START, 40))
        self.assertFalse((reads / "r000002" / "native_result.json").exists())
        self.assertEqual(len(list((reads / "signed-in-quarantine").iterdir())), 1)

    def test_sign_in_wall_records_an_expired_sign_in(self):
        self.sign_in()
        self.serve = {HOME: "home"}
        self.run_reads(1)
        self.serve = {WINNING: "wall"}
        outcomes, _ = self.run_reads(1, start=later(START, 40))
        self.assertEqual((outcomes[0]["state"], outcomes[0]["reason"]), ("waiting", "OWNER_SIGN_IN_REQUIRED"))
        self.assertEqual(site_states(self.pacing_root)[self.route]["state"], "expired")
        self.assertIsNotNone(platforms.load_refresh(self.store, self.route)["cycle"])  # resumes after the owner signs in

    def test_unrecognised_layout_drops_the_cycle_instead_of_an_empty_catalogue(self):
        self.sign_in()
        outcomes, _ = self.run_reads(1)
        self.assertEqual(self.calls[0]["url"], HOME)
        self.assertEqual((outcomes[0]["state"], outcomes[0]["reason"]), ("waiting", "DROPSHIP_PAGE_LAYOUT_UNRECOGNISED"))
        refresh = platforms.load_refresh(self.store, self.route)
        self.assertIsNone(refresh["cycle"])
        self.assertEqual(platforms.catalogue_wait(self.store, self.route), "DROPSHIP_PAGE_LAYOUT_UNRECOGNISED")
        # A changed layout: the product cards are still found (the generic guess) but none of the
        # page's own price elements is, and the card text has no price (the rupee sign is a picture).
        self.pages["unpriced"] = deepcopy(self.pages["home"])
        for card in self.pages["unpriced"]["cards"]:
            card.update(price=None, mrp=None, stock=None, sold=None, section=None)
        self.assertEqual(len(signed.parse_snapshot(self.route, page_request(), self.pages["unpriced"])["items"]), 30)
        self.assertFalse(signed.shows_priced_products(self.pages["unpriced"], platforms.platform_settings(self.route)["product_path"]))
        self.serve = {HOME: "unpriced"}
        outcomes, _ = self.run_reads(1, start=later(START, 600))
        self.assertEqual((len(self.calls), outcomes[0]["reason"]), (2, "DROPSHIP_PAGE_LAYOUT_UNRECOGNISED"))
        self.assertEqual(platforms.load_catalogue(self.store, self.route)["items"], {})
        self.assertIn("Dropdash's product pages were not recognised", " ".join(platforms.platform_lines(self.store)))

    def test_a_busy_browser_uncounts_the_read_and_a_broken_one_is_a_failure(self):
        self.sign_in()
        self.serve = {HOME: "busy"}
        outcomes, _ = self.run_reads(1)
        self.assertEqual((outcomes[0]["state"], outcomes[0]["reason"], outcomes[0]["reads"]), ("waiting", "SIGNED_IN_BROWSER_BUSY", 0))
        self.assertEqual(load_json(pacing._path(self.pacing_root), 1024 * 1024)["reads"][self.route], [])
        self.serve = {HOME: "broken"}
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
                          "fetch(", "XMLHttpRequest", ".value =", "localStorage", "cookie", "__PAGE_SELECTORS__",
                          "__PRODUCT_PATTERN__"):
            self.assertNotIn(forbidden, script)
        # The platform's own selectors and product address go in as data (JSON text).
        config = platforms.platform_settings(self.route)
        self.assertIn("pick = " + json.dumps(config["selectors"], sort_keys=True) + ";", script)
        self.assertIn("new RegExp(" + json.dumps(config["product_path"]) + ")", script)
        for url in ("https://app.dropdash.co/checkout", "https://app.dropdash.co/cart/add",
                    "https://app.dropdash.co/plans", "https://app.dropdash.co/pricing",
                    "https://app.dropdash.co/subscribe?plan=gold", "https://app.dropdash.co/contact-us",
                    "https://app.dropdash.co/wallet/recharge", "https://app.dropdash.co/account/settings",
                    "https://app.dropdash.co/orders/1", "https://app.dropdash.co/upgrade",
                    "https://app.dropdash.co/premium/products", "https://app.dropdash.co/free-trial",
                    "https://app.dropdash.co/membership", "https://app.dropdash.co/place-order",
                    "https://app.dropdash.co/push/13227",
                    PRODUCT + "&add_to_store=1", PRODUCT + "#variants", PRODUCT + " ",
                    "https://app.dropdash.co/productDetails?id=13227",  # not the platform's product address
                    "https://app.dropdash.co/productDetails?sellerId=1405&id=13227",
                    "https://app.dropdash.co/productDetails?id=0&sellerId=1405",
                    "https://app.dropdash.co/products",  # the guessed catalogue address: not a configured page
                    "https://app.dropdash.co/winning-products",
                    "https://app.dropdash.co/products?categoryId=63",  # a category seen on the home page, not configured
                    "https://app.dropdash.co/products?categoryId=288&page=2",
                    "https://app.dropdash.co/home?page=2&plan=gold", "https://app.dropdash.co/home?sort=new",
                    "https://app.dropdash.co/home?page=0", "https://elsewhere.example/home",
                    "https://app.dropdash.co/home#top"):
            with self.subTest(url=url), self.assertRaisesRegex(SafetyError, "DROPSHIP_PAGE_NOT_READ_ONLY"):
                signed.read_only_url(self.route, url)
        for url in (HOME, WINNING, PRODUCT, "https://app.dropdash.co/home?page=2",
                    "https://app.dropdash.co/productDetails?id=1&sellerId=999999999"):
            self.assertEqual(signed.read_only_url(self.route, url), url)
        # A next-page link to a forbidden step is simply not followed.
        parsed = signed.parse_snapshot(self.route, page_request(max_pages=5), self.pages["text_only_page_2"])
        self.assertEqual(parsed["follow"], [])

    def test_markers_and_real_card_parsing(self):
        self.assertIs(signed.signed_in_marker(self.pages["wall"]), False)
        self.assertIs(signed.signed_in_marker(dict(self.pages["unrecognised"], url="https://app.dropdash.co/signin")), False)
        for name in ("home", "category_288", "product_13227"):
            self.assertIs(signed.signed_in_marker(self.pages[name]), True)
        self.assertIsNone(signed.signed_in_marker(self.pages["unrecognised"]))
        self.assertIsNone(signed.signed_in_marker(None))
        # Every real card: its own title, price, MRP, stock and section elements (the rupee sign is a picture).
        home = signed.parse_snapshot(self.route, page_request(), self.pages["home"])
        cards = self.pages["home"]["cards"]
        self.assertEqual(len(home["items"]), len(cards))
        for card, row in zip(cards, home["items"]):
            with self.subTest(item=row["item_id"]):
                self.assertEqual((row["url"], row["title"], row["price"], row["mrp"], row["in_stock"], row["tags"]),
                                 (card["href"], card["title"], card["price"], card["mrp"], int(card["stock"]) > 0,
                                  [card["section"]]))
                self.assertEqual(row["item_id"], re.search(r"\?id=([0-9]+)&", card["href"])[1])
                # Its own badge ("₹ 29 OFF", "25% OFF") is MRP less price: never taken for a price.
                off = next((badge for badge in card["badges"] if badge.endswith(" OFF")), None)
                if off and off.startswith("₹"):
                    self.assertEqual(int(row["mrp"]) - int(row["price"]), int(off[1:-4].replace(",", "")))
                elif off:
                    self.assertAlmostEqual((int(row["mrp"]) - int(row["price"])) / int(row["mrp"]) * 100, int(off[:-5]), delta=1)
                else:
                    self.assertIsNone(row["mrp"])
                self.assertEqual(row["badges"], [])  # discounts, units sold and stock are not list badges
        self.assertEqual(sorted({row["tags"][0] for row in home["items"]}),
                         ["Fitness & Wellness", "Men", "Profitable Products", "Religious & Spirituals", "Testing Products"])
        # The home page is no category; its "Testing Products" sales section is left out (its cards keep
        # it as a tag) and "Profitable Products", the configured winning list, is that list.
        categories = home["collections"]
        self.assertEqual(len(categories), 20)
        self.assertEqual(categories[0], {"handle": "products-288", "title": "Profitable Products", "url": WINNING,
                                         "products_count": None, "roles": ["winning"], "parent": None})
        self.assertEqual(categories[1], {"handle": "products-63", "title": "Home Decor & Lighting",
                                         "url": "https://app.dropdash.co/products?categoryId=63", "products_count": None,
                                         "roles": [], "parent": None})
        self.assertNotIn("Testing Products", [row["title"] for row in categories])
        self.assertEqual(home["follow"], [])
        listed = signed.parse_snapshot(self.route, page_request("winning", WINNING), self.pages["category_288"])
        self.assertEqual([(row["price"], row["mrp"], row["in_stock"], row["tags"]) for row in listed["items"]],
                         [("101", "201", True, [])] * 8)
        self.assertEqual((listed["collections"][0]["roles"], len(listed["collections"])), (["winning"], 1))
        # One product shown in two sections is one item with both.
        twice = deepcopy(self.pages["home"])
        twice["cards"].append(dict(twice["cards"][0], section="Profitable Products"))
        items = signed.parse_snapshot(self.route, page_request(), twice)["items"]
        self.assertEqual((len(items), items[0]["tags"]), (30, ["Testing Products", "Profitable Products"]))
        path = platforms.platform_settings(self.route)["product_path"]
        self.assertIs(signed.plan_wall({"kind": "page"}, self.pages["home"], path), False)
        self.assertIs(signed.plan_wall({"kind": "page"}, self.pages["plan_wall"], path), True)
        walled = dict(self.pages["product_13227"], text="Upgrade your plan to view products.")
        self.assertIs(signed.plan_wall({"kind": "product"}, walled, path), False)  # the page still shows its own price
        self.assertIs(signed.plan_wall({"kind": "product"}, dict(walled, product=self.pages["product_sparse"]["product"]), path), True)
        with self.assertRaisesRegex(SafetyError, "DROPSHIP_FEED_BODY_INVALID"):
            self.reader().parse({"kind": "page"}, {"receipt": {}, "body": "[]"})
        with self.assertRaisesRegex(SafetyError, "DROPSHIP_ROUTE_INVALID"):
            signed.SignedInReader("deodap", pacing_root=self.pacing_root)

    def test_a_layout_without_its_own_price_elements_falls_back_to_the_card_text(self):
        parsed = signed.parse_snapshot(self.route, page_request(max_pages=5), self.pages["text_only"])
        rows = {row["item_id"]: row for row in parsed["items"]}
        self.assertEqual(list(rows), ["501", "502", "503"])  # the checkout link is not a product
        caddy = rows["501"]
        self.assertEqual((caddy["title"], caddy["price"], caddy["mrp"], caddy["in_stock"]), ("Steel Draining Sink Caddy", "149", "499", True))
        # Shown again in another section: one item with both sections and both badges.
        self.assertEqual((caddy["tags"], caddy["badges"]), (["Kitchen Tools", "Testing Products"], ["new_arrival", "trending"]))
        kids = rows["502"]
        self.assertEqual((kids["title"], kids["price"], kids["in_stock"], kids["badges"]), ("Kids Bath Toy Set", "99", False, ["high_demand"]))
        # Words in a title are not list badges; a discount or a "/month" plan figure is never a price.
        self.assertEqual((rows["503"]["badges"], rows["503"]["price"], rows["503"]["mrp"]), ([], "120", None))
        self.assertEqual(parsed["follow"][0]["url"], "https://app.dropdash.co/home?page=2")
        self.assertEqual(signed.parse_snapshot(self.route, page_request(), self.pages["text_only"])["follow"], [])  # one page only
        # A category chosen by a query keeps only that query; another site's category is left out.
        self.assertEqual([(row["handle"], row["title"], row["url"]) for row in parsed["collections"]],
                         [("products-77", "Kitchen Tools", "https://app.dropdash.co/products?categoryId=77")])
        self.assertIs(signed.plan_wall({"kind": "page"}, self.pages["text_only"],  # a plan banner beside priced products
                                       platforms.platform_settings(self.route)["product_path"]), False)
        self.assertEqual(signed.prices("Plans from ₹ 499/month\nOnly ₹ 1,299 per year\nPrice ₹ 99"), ("99", None))
        self.assertEqual(signed.prices("₹ 29 OFF\n₹ 220\nMRP ₹ 249"), ("220", "249"))
        self.assertEqual(signed.prices("₹ 220\nOff-white"), ("220", None))
        self.assertEqual(signed.prices("₹ 99\n₹ 120"), ("99", None))  # the first figure is the price
        # Cards without an address and categories without a name of their own are left out.
        odd = dict(self.pages["text_only"], cards=[None, {"href": None}, {"href": 5}],
                   categories=[None, {"href": "https://app.dropdash.co/", "text": "Home"}])
        parsed = signed.parse_snapshot(self.route, page_request(), odd)
        self.assertEqual((parsed["items"], parsed["collections"]), ([], []))
        self.assertFalse(signed.shows_priced_products(odd, platforms.platform_settings(self.route)["product_path"]))
        for value, figure in (("₹ 1,299", "1299"), ("240", "240"), ("97 units", "97"), ("₹ 2,499.50", "2499.50"),
                              ("₹ 499/month", None), ("Plans monthly 99", None), ("Low", None), ("", None), (None, None),
                              ("9" * 41, None)):
            self.assertEqual(signed._figure(value), figure)

    def test_dropdash_uses_its_own_pages_and_product_addresses(self):
        reader = signed.SignedInReader("dropdash", pacing_root=self.pacing_root, browser=self.browser)
        plan = reader.plan_full(platforms.platform_settings("dropdash"), platforms.load_catalogue(self.store, "dropdash"))
        self.assertEqual([(row["role"], row["url"], row["max_pages"]) for row in plan], [("catalogue", HOME, 1), ("winning", WINNING, 1)])
        config = platforms.platform_settings("dropdash")
        self.assertEqual(re.fullmatch(config["product_path"], PRODUCT)[1], "13227")
        for url in ("https://app.dropdash.co/products/sink-caddy", PRODUCT + "&x=1", "https://app.dropdash.co/productDetails?id=13227"):
            self.assertIsNone(re.fullmatch(config["product_path"], url))
        # Every element the read uses is named from the pages seen signed in on 16 October 2026.
        self.assertEqual(sorted(config["selectors"]), sorted(platforms.SELECTOR_KEYS))
        self.assertTrue(all(config["selectors"].values()))

    def test_a_cycle_saved_under_earlier_addresses_is_dropped_never_opened(self):
        # Not signed in yet: the cycle is planned and saved while it waits for the owner.
        self.assertEqual(self.advance(self.reader(), self.route)["reason"], "OWNER_SIGN_IN_REQUIRED")
        refresh = platforms.load_refresh(self.store, self.route)
        refresh["cycle"]["queue"][0]["url"] = "https://app.dropdash.co/products"  # an earlier settings' address
        platforms._save_refresh(self.store, self.route, refresh)
        self.sign_in()
        self.serve = {HOME: "home", WINNING: "category_288"}
        outcomes, _ = self.run_reads(1)
        self.assertEqual((outcomes[0]["state"], outcomes[0]["reason"]), ("waiting", "DROPSHIP_PAGE_NOT_READ_ONLY"))
        self.assertEqual(self.calls, [])  # never opened
        self.assertFalse(pacing._path(self.pacing_root).exists())  # nothing counted against the platform's pacing
        self.assertIsNone(platforms.load_refresh(self.store, self.route)["cycle"])
        receipt = platforms.load_raw(self.store, self.route, platforms.raw_files(self.store, self.route)[0])["receipt"]
        self.assertEqual((receipt["state"], receipt["execution_stage"], receipt["requested_url"]),
                         ("failed", "not_dispatched", "https://app.dropdash.co/products"))
        # After the short wait, a new cycle from the current settings reads the configured pages.
        outcomes, _ = self.run_reads(3, start=later(START, 400))
        self.assertEqual([call["url"] for call in self.calls], [HOME, WINNING])
        self.assertEqual(outcomes[-1]["state"], "current")
        # An item whose saved address the settings no longer allow is never planned for a product page.
        catalogue = platforms.load_catalogue(self.store, self.route)
        catalogue["items"]["9240"]["url"] = "https://app.dropdash.co/products/invisible-glue"
        plan = self.reader().plan_lists(platforms.platform_settings(self.route), catalogue)
        products = [row["item_id"] for row in plan if row["kind"] == "product"]
        self.assertNotIn("9240", products)
        self.assertIn("9210", products)

    def test_odd_figures_and_placeholder_prices_never_break_or_pass_a_read(self):
        page = deepcopy(self.pages["home"])
        page["cards"][0]["stock"] = "1.2K"  # a shortened count is a figure, never an error
        page["cards"][1]["stock"] = "0.0"
        items = {row["item_id"]: row for row in signed.parse_snapshot(self.route, page_request(), page)["items"]}
        self.assertIs(items["12344"]["in_stock"], True)
        self.assertIs(items["12094"]["in_stock"], False)
        # Every card showing a zero price (a placeholder) shows no priced product.
        zero = dict(page, cards=[dict(card, price="0", mrp=None, text="") for card in page["cards"]])
        self.assertFalse(signed.shows_priced_products(zero, platforms.platform_settings(self.route)["product_path"]))

    def test_selectors_and_product_pattern_are_put_in_once_as_data(self):
        config = deepcopy(platforms.platform_settings(self.route))
        config["selectors"]["card"] = ".x__PRODUCT_PATTERN__"  # the other placeholder's text, inside a selector
        with patch.object(signed, "platform_settings", return_value=config):
            script = signed.expression(self.route)
        self.assertIn("pick = " + json.dumps(config["selectors"], sort_keys=True) + ";", script)
        self.assertEqual(script.count(json.dumps(config["product_path"])), 1)

    def test_a_list_page_that_lands_on_another_allowed_page_is_a_redirect(self):
        self.sign_in()
        # The winning list now opens the home page (an allowed page): its cards are not the list's.
        self.serve = {HOME: "home", WINNING: "home"}
        outcomes, _ = self.run_reads(2)
        self.assertEqual([(row["state"], row["reason"]) for row in outcomes],
                         [("reading", None), ("waiting", "DROPSHIP_PAGE_REDIRECTED")])
        catalogue = platforms.load_catalogue(self.store, self.route)
        self.assertEqual(len(catalogue["items"]), 30)  # the home page's own read stays
        self.assertFalse(any(item["roles"] for item in catalogue["items"].values()))
        receipt = platforms.load_raw(self.store, self.route, platforms.raw_files(self.store, self.route)[-1])["receipt"]
        self.assertEqual((receipt["state"], receipt["requested_url"], receipt["final_url"]), ("failed", WINNING, None))
        # Like a redirect off the allowed pages: the cycle keeps what it read; only that page is tried again.
        refresh = platforms.load_refresh(self.store, self.route)
        self.assertEqual(([row["url"] for row in refresh["cycle"]["queue"]], refresh["failures"]), ([WINNING], 1))
        self.assertIsNotNone(refresh["blocked_until"])
        self.serve = {WINNING: "category_288"}
        outcomes, _ = self.run_reads(2, start=later(START, 400))
        self.assertEqual([row["state"] for row in outcomes], ["reading", "current"])
        self.assertEqual([call["url"] for call in self.calls], [HOME, WINNING, WINNING])
        listed = [item for item in platforms.load_catalogue(self.store, self.route)["items"].values() if item["roles"]]
        self.assertEqual(len(listed), 8)

    def test_harmless_differences_in_the_landed_address_are_the_same_page(self):
        for landed, requested in ((HOME + "/", HOME), ("https://APP.Dropdash.co:443/home#top", HOME),
                                  ("https://app.dropdash.co/productDetails?sellerId=1405&id=13227", PRODUCT),
                                  ("https://app.dropdash.co/products?categoryId=%32%38%38", WINNING), (WINNING, WINNING)):
            self.assertTrue(signed.same_page(landed, requested), landed)
        for landed, requested in ((HOME + "?page=2", HOME), ("http://app.dropdash.co/home", HOME),
                                  ("https://app.dropdash.co:8443/home", HOME), ("https://app.dropdash.co/Home", HOME),
                                  ("https://app.dropdash.co/products?categoryId=282", WINNING), (None, HOME), ("", HOME),
                                  ("https://app.dropdash.co:99999/home", HOME), (HOME, None)):
            self.assertFalse(signed.same_page(landed, requested), landed)
        self.sign_in()
        self.pages["home_spelled"] = dict(self.pages["home"], url="https://APP.dropdash.co:443/home/#top")
        self.serve = {HOME: "home_spelled", WINNING: "category_288"}
        outcomes, _ = self.run_reads(3)
        self.assertEqual([row["state"] for row in outcomes], ["reading", "reading", "current"])
        receipt = platforms.load_raw(self.store, self.route, platforms.raw_files(self.store, self.route)[0])["receipt"]
        self.assertEqual((receipt["state"], receipt["final_url"]), ("read", "https://APP.dropdash.co:443/home/"))
        # A product page Chrome reports with its parameters in another order is still that product's.
        reordered = dict(self.pages["product_13227"], url="https://app.dropdash.co/productDetails?sellerId=1405&id=13227")
        self.assertEqual(signed.parse_snapshot(self.route, product_request(), reordered)["items"][0]["price"], "240")

    def test_a_product_page_that_opens_another_product_records_no_item(self):
        page = self.pages["product_13227"]  # product 13227's own page
        other = product_request(item_id="99", url="https://app.dropdash.co/productDetails?id=99&sellerId=1")
        self.assertEqual(signed.parse_snapshot(self.route, other, page)["items"], [])
        for landed in (HOME, WINNING, None, 7):  # no product's page, or no address at all
            self.assertEqual(signed.parse_snapshot(self.route, product_request(), dict(page, url=landed))["items"], [])
        moved = dict(page, url="https://app.dropdash.co/productDetails?id=13227&sellerId=77#reviews")  # the same product number
        item = signed.parse_snapshot(self.route, product_request(), moved)["items"][0]
        self.assertEqual((item["item_id"], item["url"], item["price"]), ("13227", PRODUCT, "240"))
        # In a lists cycle, one listed product (a deleted one) now opens another product's page.
        self.sign_in()
        self.serve = {HOME: "home", WINNING: "category_288"}
        self.run_reads(3)
        catalogue = platforms.load_catalogue(self.store, self.route)
        listed = sorted(item_id for item_id, item in catalogue["items"].items() if item["roles"])
        self.serve.update({catalogue["items"][item_id]["url"]: "product_sparse" for item_id in listed})
        glue_url = catalogue["items"]["9240"]["url"]
        self.serve[glue_url] = "product_13227"
        outcomes, _ = self.run_reads(12, start=later(START, 86400 + 300))
        # The read is kept and the cycle finishes; later rounds read nothing more (no retry every round).
        self.assertEqual([row["state"] for row in outcomes], ["reading"] * 9 + ["current"] * 3)
        self.assertEqual(sorted(call["url"] for call in self.calls[2:]),
                         sorted([WINNING] + [catalogue["items"][item_id]["url"] for item_id in listed]))
        refs = platforms.raw_files(self.store, self.route)
        receipts = [platforms.load_raw(self.store, self.route, ref)["receipt"] for ref in refs]
        redirected = next(row for row in receipts if row["requested_url"] == glue_url)
        self.assertEqual((redirected["state"], redirected["final_url"]), ("read", PRODUCT))
        after = platforms.load_catalogue(self.store, self.route)
        glue, posture = after["items"]["9240"], after["items"]["13227"]
        # Nothing from product 13227's page was recorded for product 9240, or for 13227 itself.
        self.assertNotIn("product-page", glue["collections"])
        self.assertEqual((glue["title"], glue["price"], glue["mrp"], glue["terms"]), ("Invisible Waterproof Glue", "101", "201", None))
        self.assertNotEqual(glue["source_ref"], refs[receipts.index(redirected)])
        self.assertEqual(posture, catalogue["items"]["13227"])
        self.assertTrue(all("product-page" in after["items"][item_id]["collections"] for item_id in listed if item_id != "9240"))

    def test_a_plan_wall_over_unpriced_cards_waits_for_the_plan_not_a_new_layout(self):
        walled = deepcopy(self.pages["home"])
        walled["text"] = "Upgrade your plan to view products. Choose a plan: Basic ₹ 999/month.\n" + walled["text"]
        for card in walled["cards"]:
            card.update(price=None, mrp=None, text=card["title"])  # the cards show, their prices do not
        self.pages["walled_cards"] = walled
        path = platforms.platform_settings(self.route)["product_path"]
        self.assertIs(signed.plan_wall({"kind": "page"}, walled, path), True)
        self.assertIs(signed.plan_wall({"kind": "page"}, dict(walled, text="Welcome back"), path), False)  # no plan words
        self.sign_in()
        self.serve = {HOME: "walled_cards"}
        outcomes, _ = self.run_reads(1)
        self.assertEqual((outcomes[0]["state"], outcomes[0]["reason"]), ("waiting", "DROPSHIP_PLAN_REQUIRED"))
        refresh = platforms.load_refresh(self.store, self.route)
        self.assertEqual(timestamp(refresh["blocked_until"]) - timestamp(START), timedelta(days=1))
        self.assertIsNotNone(refresh["cycle"])  # a plan wall is looked at again, never a dropped cycle
        self.assertEqual(platforms.load_catalogue(self.store, self.route)["items"], {})

    def test_a_category_link_around_the_category_element_is_read(self):
        node = shutil.which("node")
        if not node:
            self.skipTest("Node is needed to run the page read on a small page double")
        selectors = platforms.platform_settings(self.route)["selectors"]
        page = r"""
const selectors = SELECTORS;
const linked = (text, inner, outer) => ({innerText:text, matches(){return false;},
  querySelector(s){return s === 'a[href]' ? inner : null;}, closest(s){return s === 'a[href]' ? outer : null;}});
const rows = [linked('Home Decor & Lighting', null, {href:'https://app.dropdash.co/products?categoryId=63'}),
              linked('Pet Care', {href:'https://app.dropdash.co/products?categoryId=292'}, null),
              linked('No link', null, null)];
globalThis.location = {href:'https://app.dropdash.co/home'};
globalThis.document = {title:'Dropdash', body:{innerText:'Home'}, querySelector(){return null;},
  querySelectorAll(s){return s === selectors.category ? rows : [];}};
console.log(READ);
""".replace("SELECTORS", json.dumps(selectors)).replace("READ", signed.expression(self.route))
        reply = subprocess.run([node, "-e", page], capture_output=True, text=True, timeout=10, check=True)
        self.assertEqual(json.loads(reply.stdout)["categories"],
                         [{"href": "https://app.dropdash.co/products?categoryId=63", "text": "Home Decor & Lighting"},
                          {"href": "https://app.dropdash.co/products?categoryId=292", "text": "Pet Care"}])


if __name__ == "__main__":
    unittest.main()
