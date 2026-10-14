"""IndiaMART parser v6, the signed-in marker, account-detail removal and pacing.

Pages are the synthetic signed-in results fixture (no real signed-in IndiaMART
page has been saved yet), the saved login-wall pages and small inline pages.
Nothing here launches a browser or visits a site.
"""

from datetime import timedelta
import json
from pathlib import Path
import re
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "tests"))
from product_radar import signed_in_pacing as pacing
from product_radar.indiamart_redaction import QUARANTINE, protect_saved, redact_output, redact_snapshot
from product_radar.indiamart_results import FIELDS
from product_radar.indiamart_signed_in import signed_in_marker
from product_radar.models import SafetyError, timestamp
from product_radar.storage import atomic_json, load_json
from product_radar.supplier_route_acquisition import (_DOM, _READY, MAX_RESULTS, PARSER_VERSION, _ready_expression,
    indiamart_login_wall, parse_supplier_route_receipt, supplier_route_url)
from test_supplier_route_acquisition import receipt

FIXTURES = ROOT / "tests" / "fixtures" / "supplier_routes"
RESULTS = "indiamart-search-signed-in-results-synthetic-2026-10-14.html"
WALL = "indiamart-search-login-wall-rendered-2026-10-14.html"
QUERY = "draining sink caddy"
BASE = supplier_route_url("indiamart", QUERY)
# The research account as its own page shows it. "Shreeji" is also the start of
# a supplier's name on the page, so word-guessing removal would damage listings.
VISITOR = {"fn": "Shreeji", "ln": "Verma", "em": "asha.research@example.com", "mb1": "919876501234", "iso": "IN",
           "glid": "90000001", "sessionKey": "s3ss10nK3y"}
SECRETS = ("Shreeji Verma", "9876501234", "98765 01234", "98765-01234", "asha.research@example.com",
           "90000001", "s3ss10nK3y")


def page(name):
    return (FIXTURES / name).read_text(encoding="utf-8")


def visitor_page(visitor=VISITOR):
    """The results fixture as the signed-in account sees it: its details in the
    page data, a greeting in the header, a filled enquiry form and its number."""
    html = page(RESULTS).replace('<a href="https://my.indiamart.com/" class="im-header-top-link">Hi</a>',
                                 '<a href="https://my.indiamart.com/" class="im-header-top-link">Hi Shreeji Verma</a>')
    html = html.replace('<main class="listing">', '<main class="listing"><p class="note">Call me on +91 98765 01234 '
                        'or 098765-01234</p><form class="enquiry"><input name="mobile" value="9876501234">'
                        '<input name="name" value="Shreeji Verma"><textarea>From Shreeji Verma</textarea></form>')
    return re.sub(r'"imeshVObject":\{[^{}]*\}', '"imeshVObject":' + json.dumps(visitor, separators=(",", ":")), html)


def signed_receipt(html, *, profile="signed-in", query=QUERY):
    item = {"id": "supplier-route-request-signed", "url": supplier_route_url("indiamart", query)}
    value = dict(receipt(item, body=html), text=re.sub(r"<[^>]+>", " ", html))
    if profile:
        value["profile"] = profile
    return item, value


def parsed(html, **options):
    item, value = signed_receipt(html, **options)
    return parse_supplier_route_receipt("indiamart", options.get("query", QUERY), item, value)


class IndiaMartParserTests(unittest.TestCase):
    def test_parser_v6_reads_each_listing_card_with_supplier_location_price_and_moq(self):
        result = parsed(page(RESULTS))
        self.assertEqual((PARSER_VERSION, result["state"], len(result["results"])), (6, "complete", MAX_RESULTS))
        self.assertTrue(all(tuple(row) == FIELDS for row in result["results"]))
        first, second, third = result["results"][:3]
        self.assertEqual(first, {"url": "https://www.indiamart.com/proddetail/sink-caddy-organiser-2851239901.html",
            "title": "Plastic Sink Caddy Organiser", "supplier_name": "Kitchen King Industries",
            "supplier_url": "https://kitchenking.indiamart.com/products.html", "location": "Andheri East, Maharashtra",
            "price_text": "₹ 120 - 180 / Piece", "moq_text": "Min. Order Qty: 100 Pieces"})
        # A picture-only title link, a "View Details" link and a "Contact
        # Supplier" link: labels are never a title or a supplier name.
        self.assertEqual((second["title"], second["supplier_name"], second["price_text"], second["location"]),
                         ("Steel Sink Caddy", None, "Rs. 450 / Piece", "Sector 63, Noida, Uttar Pradesh"))
        self.assertEqual(third, {"url": "https://www.indiamart.com/proddetail/draining-sink-caddy-2851234500.html",
            "title": "Stainless Steel Draining Sink Caddy", "supplier_name": "Shreeji Steel Works",
            "supplier_url": "https://www.indiamart.com/shreeji-steel-works/", "location": "Rajkot",
            "price_text": "₹ 249 / Piece", "moq_text": "MOQ: 50 Piece"})
        by_title = {row["title"]: row for row in result["results"]}
        self.assertIsNone(by_title["Wire Sink Caddy Basket"]["price_text"])  # "Get Latest Price" is not a price
        self.assertIsNone(by_title["SS Sink Organiser Caddy"]["moq_text"])
        urls = [row["url"] for row in result["results"]]
        self.assertEqual(len(set(urls)), MAX_RESULTS)
        # Header and footer listings (recently viewed, promoted) are never results.
        self.assertFalse(any("recently-viewed" in url or "footer-promoted" in url for url in urls))
        labels = {"View Mobile Number", "Contact Supplier", "Get Best Price", "Send Enquiry", "View Details"}
        self.assertFalse(labels & {value for row in result["results"] for value in row.values()})

    def test_tracking_query_strings_do_not_split_a_listing_and_personal_blocks_are_never_results(self):
        listing = "https://www.indiamart.com/proddetail/caddy-2851231111.html"
        html = ('<main><div class="c"><a href="' + listing + '?pos=1&amp;kwd=caddy"><img alt="Caddy picture"></a>'
                '<a href="' + listing + '?pos=1&amp;src=title">Wall Caddy</a><p>&#8377; 80 / Piece</p></div>'
                '<div class="c"><a href="https://www.indiamart.com/proddetail/rack-2851232222.html">Sink Rack</a></div>'
                '<section class="recentlyViewed"><a href="https://www.indiamart.com/proddetail/seen-2851233333.html">'
                'Seen before</a><a href="https://www.indiamart.com/proddetail/seen-2851233334.html">Seen too</a></section>'
                '<div><h2>Recommended for you</h2><a href="https://www.indiamart.com/proddetail/pick-2851234444.html">'
                'Pick</a></div></main>')
        rows = parsed(html)["results"]
        self.assertEqual([(row["url"], row["title"], row["price_text"]) for row in rows],
                         [(listing, "Wall Caddy", "₹ 80 / Piece"),
                          ("https://www.indiamart.com/proddetail/rack-2851232222.html", "Sink Rack", None)])

    def test_structured_search_data_is_read_first(self):
        data = {"props": {"pageProps": {"dummyOtpPage": False, "searchResponse": {"total": 2, "results": [
            {"fields": {"title": "Draining Sink Caddy", "desktop_title_url":
                        "https://www.indiamart.com/proddetail/data-caddy-2851235555.html?pos=3&kwd=caddy",
                        "companyname": "Shreeji Steel Works", "catalogUrl": "https://www.indiamart.com/shreeji-steel-works/",
                        "city": "Rajkot", "price": "₹ 249", "unit": "Piece", "moq": 50, "moq_type": "Piece"}},
            {"title": "No listing link here", "companyname": "Other"},
            {"name": "Sink Rack", "url": "https://www.indiamart.com/proddetail/data-rack-2851236666.html",
             "company_name": "Rack House", "cityname": "Pune"}]}}}}
        html = ('<main><a href="https://www.indiamart.com/proddetail/card-only-2851237777.html">Card only</a></main>'
                '<script id="__NEXT_DATA__" type="application/json">' + json.dumps(data) + '</script>')
        self.assertEqual(parsed(html)["results"], [
            {"url": "https://www.indiamart.com/proddetail/data-caddy-2851235555.html", "title": "Draining Sink Caddy",
             "supplier_name": "Shreeji Steel Works", "supplier_url": "https://www.indiamart.com/shreeji-steel-works/",
             "location": "Rajkot", "price_text": "₹ 249 / Piece", "moq_text": "MOQ: 50 Piece"},
            {"url": "https://www.indiamart.com/proddetail/data-rack-2851236666.html", "title": "Sink Rack",
             "supplier_name": "Rack House", "supplier_url": None, "location": "Pune", "price_text": None, "moq_text": None}])
        # Without usable structured results the visible cards are read.
        for broken in ("{broken", json.dumps({"props": {"pageProps": {"searchResponse": {"results": []}}}})):
            fallback = html.replace(html[html.index('<script'):], '<script id="__NEXT_DATA__">' + broken + '</script>')
            self.assertEqual([row["title"] for row in parsed(fallback)["results"]], ["Card only"])

    def test_older_parsers_keep_their_own_reading_of_the_same_page(self):
        item, value = signed_receipt(page(RESULTS))
        older = parse_supplier_route_receipt("indiamart", QUERY, item, value, _parser_version=5)
        self.assertEqual(older["state"], "complete")
        self.assertTrue(all(set(row) == {"url", "title"} for row in older["results"]))

    def test_sign_in_wall_in_the_signed_in_profile_means_the_sign_in_expired(self):
        for profile, version, reason in (("signed-in", 6, "OWNER_SIGN_IN_REQUIRED"),
                                         (None, 6, "SUPPLIER_ROUTE_CHALLENGE"),
                                         ("throwaway", 6, "SUPPLIER_ROUTE_CHALLENGE"),
                                         ("signed-in", 5, "SUPPLIER_ROUTE_CHALLENGE")):
            with self.subTest(profile=profile, version=version):
                item, value = signed_receipt(page(WALL), profile=profile, query="Food wrap paper")
                self.assertEqual(parse_supplier_route_receipt("indiamart", "Food wrap paper", item, value,
                                                              _parser_version=version),
                                 {"state": "blocked", "reason": reason, "results": []})

    def test_a_page_without_listing_cards_keeps_supplier_page_links_in_the_same_row_shape(self):
        rows = parsed('<main><a href="https://kitchenking.indiamart.com/products.html">Kitchen King Industries</a></main>')["results"]
        self.assertEqual(rows, [dict(dict.fromkeys(FIELDS), url="https://kitchenking.indiamart.com/products.html",
                                     title="Kitchen King Industries")])

    def test_odd_markup_neither_invents_results_nor_fails_the_page(self):
        html = ('<main><svg><svg><a href="https://www.indiamart.com/proddetail/icon-2851230000.html">Icon</a></svg></svg>'
                '<a href="https://[broken/x">Broken link</a><a href="https://www.indiamart.com:99999/bad-port/">Bad port</a>'
                '<div class="c"><a href="https://www.indiamart.com/proddetail/heading-2851230001.html"><img src="x.jpg"></a>'
                '<h3>Heading Titled Caddy</h3><p>&#8377; 99</p></div>'
                '<div class="c"><a href="https://www.indiamart.com/proddetail/untitled-2851230002.html"><img src="y.jpg"></a>'
                '<p>&#8377; 50</p></div></main>')
        self.assertEqual(parsed(html)["results"], [dict(dict.fromkeys(FIELDS),
            url="https://www.indiamart.com/proddetail/heading-2851230001.html", title="Heading Titled Caddy", price_text="₹ 99")])

    def test_marker_reads_only_the_pages_own_signed_in_signals(self):
        self.assertIs(signed_in_marker({"html": page(WALL), "text": "Login to connect with suppliers GET OTP"}), False)
        self.assertIs(signed_in_marker({"html": page(RESULTS), "text": "Draining sink caddy"}), True)
        anonymous = page(RESULTS).replace('"glid":"90000001"', '"glid":""')
        self.assertIs(signed_in_marker({"html": anonymous, "text": ""}), True)  # results served to this profile
        self.assertIsNone(signed_in_marker({"html": anonymous.replace('"searchResponse":{"total_results":12,"results_shown":12}',
                                                                     '"searchResponse":null'), "text": ""}))
        self.assertIsNone(signed_in_marker({"html": "<html><body>Plain page</body></html>", "text": "Plain page"}))
        self.assertIsNone(signed_in_marker({"html": '<script id="__NEXT_DATA__">{broken</script>', "text": ""}))
        self.assertIsNone(signed_in_marker({"html": '<script id="__NEXT_DATA__">{"props":{"pageProps":{"dummyOtpPage":true}}}</script>',
                                            "text": ""}))
        self.assertIsNone(signed_in_marker(None))
        self.assertIsNone(signed_in_marker({"html": None, "text": ""}))

    def test_the_read_expressions_only_read(self):
        # No click, typing, form, focus, scroll, navigation, request or page change.
        forbidden = re.compile(r"\.click|dispatchEvent|submit|\.focus|scroll|\.value\s*=|innerHTML\s*=|setAttribute|"
                               r"location\s*\.(?:assign|replace|href\s*=)|location\s*=|window\.open|fetch\(|XMLHttpRequest|"
                               r"sendBeacon|keydown|keypress|\.remove\(|append|insert|history\.", re.I)
        ready = _ready_expression("indiamart")
        for expression in (_DOM, _READY, ready):
            self.assertIsNone(forbidden.search(expression), expression)
        self.assertIn("staticSearchPreview", ready)


class AccountRedactionTests(unittest.TestCase):
    def snapshot(self, html):
        return {"url": BASE, "html": html, "text": "Hi Shreeji Verma 98765 01234", "title": "Sink caddy 9876501234",
                "links": ["https://my.indiamart.com/?glid=90000001"], "text_truncated": False}

    def test_account_details_are_removed_by_position_and_supplier_data_is_untouched(self):
        clean = redact_snapshot(self.snapshot(visitor_page()))
        saved = json.dumps(clean, ensure_ascii=False)
        for secret in SECRETS:
            self.assertNotIn(secret, saved)
        data = json.loads(re.search(r'"imeshVObject":(\{[^{}]*\})', clean["html"])[1])
        self.assertEqual(data, {"fn": "redacted", "ln": "redacted", "em": "redacted", "mb1": "redacted", "iso": "redacted",
                                "glid": "redacted", "sessionKey": "redacted"})
        self.assertIn('<input name="mobile" value="">', clean["html"])
        self.assertIn("<textarea></textarea>", clean["html"])
        # The supplier sharing the account's first name keeps its own name and links.
        self.assertIn("Shreeji Steel Works", clean["text"])
        self.assertIn("https://www.indiamart.com/shreeji-steel-works/", clean["links"])
        self.assertEqual(clean["title"], "Sink caddy redacted")
        item, value = signed_receipt(clean["html"])
        rows = parse_supplier_route_receipt("indiamart", QUERY, item, value)["results"]
        self.assertEqual(rows, parsed(page(RESULTS))["results"])
        self.assertIs(signed_in_marker(clean), True)
        self.assertEqual(redact_snapshot(clean), clean)  # cleaning twice changes nothing

    def test_header_greeting_goes_even_when_the_page_data_names_no_one(self):
        empty = dict.fromkeys(VISITOR, "")
        clean = redact_snapshot(self.snapshot(visitor_page(empty)))
        self.assertNotIn("Hi Shreeji Verma", clean["html"] + clean["text"])
        self.assertIn("Shreeji Steel Works", clean["text"])
        # Without a number in the page data, a shown number cannot be told from data.
        self.assertIn("98765 01234", clean["text"])

    def test_a_number_inside_a_listing_link_or_longer_number_is_never_rewritten(self):
        visitor = dict(VISITOR, mb1="2851234500")  # the same digits as a listing id
        clean = redact_snapshot(self.snapshot(visitor_page(visitor)))
        self.assertIn("https://www.indiamart.com/proddetail/draining-sink-caddy-2851234500.html", clean["html"])
        self.assertIn("draining-sink-caddy-2851234500", " ".join(clean["links"]))

    def test_a_wall_stays_recognisable_after_cleaning(self):
        clean = redact_snapshot(self.snapshot(page(WALL)))
        self.assertTrue(indiamart_login_wall(clean))
        self.assertIs(signed_in_marker(clean), False)

    def test_unreadable_page_data_is_dropped_rather_than_kept(self):
        html = '<script id="__NEXT_DATA__" type="application/json">{"imeshVObject":{"fn":"Shreeji"</script><p>x</p>'
        clean = redact_snapshot(self.snapshot(html))
        self.assertNotIn("Shreeji", clean["html"])
        # A visitor object elsewhere on the page, readable or not, and a malformed link.
        html = ('<script>window.v={"imeshVObject":{"fn":"Shreeji","mb1":"9876501234"}};'
                'window.w={"imeshVObject":{"fn":"Shreeji" broken}};</script><a href="https://[broken/x">x</a>')
        clean = redact_snapshot(self.snapshot(html))
        self.assertNotIn("Shreeji", clean["html"])
        self.assertIn('"imeshVObject":{}', clean["html"])
        self.assertEqual(clean["links"], [])
        self.assertEqual(redact_output({"state": "failed", "snapshot": None}), {"state": "failed", "snapshot": None})
        self.assertEqual(redact_snapshot(None), None)

    def test_saved_results_are_cleaned_in_place_or_quarantined_unread(self):
        folder = tempfile.TemporaryDirectory(); self.addCleanup(folder.cleanup)
        native = Path(folder.name) / "acquisition" / "native"
        path = native / "native_result.json"
        self.assertEqual(protect_saved(path), "absent")
        atomic_json(path, {"state": "captured", "snapshot": self.snapshot(visitor_page())})
        self.assertEqual(protect_saved(path), "redacted")
        text = path.read_text(encoding="utf-8")
        self.assertFalse(any(secret in text for secret in SECRETS))
        self.assertEqual(protect_saved(path), "redacted")  # already clean: unchanged
        for broken in (b'{"snapshot": {"html": "Hi Shreeji Verma"', b"[1, 2]"):
            path.write_bytes(broken)
            self.assertEqual(protect_saved(path), "quarantined")
            self.assertFalse(path.exists())
        held = list((native.parent / QUARANTINE).iterdir())
        self.assertEqual(len(held), 2)
        self.assertTrue(all(row.name.endswith("-native-native_result.json.unread") for row in held))
        path.write_bytes(b"{}")
        with patch("product_radar.signed_in_saved.MAX_SAVED", 1):
            self.assertEqual(protect_saved(path), "quarantined")

    def test_a_result_that_can_be_neither_cleaned_nor_moved_fails_closed(self):
        folder = tempfile.TemporaryDirectory(); self.addCleanup(folder.cleanup)
        path = Path(folder.name) / "acquisition" / "native" / "native_result.json"
        path.parent.mkdir(parents=True)
        path.write_bytes(b"{broken")
        with patch.object(Path, "rename", side_effect=OSError("held")), patch.object(Path, "unlink", side_effect=OSError("held")):
            with self.assertRaisesRegex(SafetyError, "SIGNED_IN_REDACTION_FAILED"):
                protect_saved(path)
        with patch.object(Path, "rename", side_effect=OSError("held")):
            self.assertEqual(protect_saved(path), "quarantined")
        self.assertFalse(path.exists())


class SignedInPacingTests(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory(); self.addCleanup(folder.cleanup)
        self.root, self.at = Path(folder.name), "2026-10-14T10:00:00+00:00"

    def later(self, seconds):
        return (timestamp(self.at) + timedelta(seconds=seconds)).isoformat()

    def test_indiamart_rules_are_one_read_per_30_seconds_and_150_a_day(self):
        self.assertEqual(pacing.pacing_rules("indiamart"), {"min_interval_seconds": 30, "daily_ceiling": 150})
        with self.assertRaisesRegex(SafetyError, "SIGNED_IN_PACING_RULES_MISSING"):
            pacing.pacing_rules("google")  # the plain Google account page is never read

    def test_spacing_holds_a_second_read_for_30_seconds_to_the_exact_instant(self):
        self.assertIsNone(pacing.signed_in_pace(self.root, "indiamart", as_of=self.at))
        start = "2026-10-14T10:00:00.750000+00:00"
        self.assertEqual(pacing.reserve_signed_in_read(self.root, "indiamart", at=start), start)
        hold = {"reason": "SIGNED_IN_READ_SPACING", "next_retry_at": "2026-10-14T10:00:30.750000+00:00"}
        self.assertEqual(pacing.signed_in_pace(self.root, "indiamart", as_of=self.later(30)), hold)
        with self.assertRaisesRegex(SafetyError, "SIGNED_IN_READ_SPACING") as refused:
            pacing.reserve_signed_in_read(self.root, "indiamart", at=self.later(10))
        self.assertEqual(refused.exception.next_retry_at, hold["next_retry_at"])  # the hold keeps its time
        self.assertIsNone(pacing.signed_in_pace(self.root, "indiamart", as_of=hold["next_retry_at"]))

    def test_daily_ceiling_holds_until_the_oldest_counted_read_is_24_hours_old(self):
        for index in range(150):
            pacing.reserve_signed_in_read(self.root, "indiamart", at=self.later(index * 60))
        last = self.later(149 * 60 + 30)
        self.assertEqual(pacing.signed_in_pace(self.root, "indiamart", as_of=last),
                         {"reason": "SIGNED_IN_DAILY_CEILING", "next_retry_at": self.later(24 * 3600)})
        self.assertIsNone(pacing.signed_in_pace(self.root, "indiamart", as_of=self.later(24 * 3600)))
        # Reads older than 24 hours are dropped when the next read is counted.
        pacing.reserve_signed_in_read(self.root, "indiamart", at=self.later(24 * 3600 + 30))
        saved = load_json(pacing._path(self.root))["reads"]["indiamart"]
        self.assertEqual((len(saved), saved[0]), (150, self.later(60)))

    def test_a_start_that_never_navigated_is_uncounted(self):
        pacing.reserve_signed_in_read(self.root, "indiamart", at=self.at)
        pacing.release_signed_in_read(self.root, "indiamart", self.at)
        self.assertEqual(load_json(pacing._path(self.root))["reads"]["indiamart"], [])
        pacing.release_signed_in_read(self.root, "indiamart", self.at)  # nothing left: unchanged
        self.assertIsNone(pacing.signed_in_pace(self.root, "indiamart", as_of=self.at))

    def test_invalid_settings_or_records_fail_closed(self):
        settings = self.root / "settings.json"
        good = load_json(pacing.SETTINGS)
        share = good["round"]
        for value in (dict(good, sites={"indiamart": {"min_interval_seconds": 0, "daily_ceiling": 150}}),
                      dict(good, sites={"unknown-site": {"min_interval_seconds": 30, "daily_ceiling": 1}}),
                      dict(good, schema_version=2), dict(good, kind="signed-in-read-pacing-v1"),
                      {name: value for name, value in good.items() if name != "round"},
                      dict(good, round=dict(share, signed_in_seconds=30)),
                      dict(good, round=dict(share, signed_in_seconds="300")),
                      dict(good, round=dict(share, site_reads={"indiamart": 0})),
                      dict(good, round=dict(share, site_reads={"unknown-site": 4})),
                      dict(good, round=dict(share, extra=1)), []):
            atomic_json(settings, value)
            with patch.object(pacing, "SETTINGS", settings), self.assertRaisesRegex(SafetyError, "SIGNED_IN_PACING_SETTINGS_INVALID"):
                pacing.pacing_rules("indiamart")
            with patch.object(pacing, "SETTINGS", settings), self.assertRaisesRegex(SafetyError, "SIGNED_IN_PACING_SETTINGS_INVALID"):
                pacing.round_settings()
        self.assertEqual(pacing.round_settings(), {"signed_in_seconds": 300, "site_reads": {"indiamart": 4}})
        for value in ({"schema_version": 1, "reads": {"indiamart": ["not a time"]}},
                      {"schema_version": 1, "reads": {"unknown-site": []}}, {"schema_version": 1}):
            pacing._path(self.root).parent.mkdir(parents=True, exist_ok=True)
            atomic_json(pacing._path(self.root), value)
            with self.assertRaisesRegex(SafetyError, "SIGNED_IN_PACING_RECORD_INVALID"):
                pacing.signed_in_pace(self.root, "indiamart", as_of=self.at)


if __name__ == "__main__":
    unittest.main()
