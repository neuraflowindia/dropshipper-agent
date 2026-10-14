"""Supplier directory pages as actually served on 13 October 2026.

Fixtures are sanitised copies of saved public receipts (scripts, styles,
images, session tokens and tracking ids removed; the searched phrase replaced
by the shared test phrase). No live website, provider or model is contacted.
"""

from pathlib import Path
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from product_radar.public_pages import _PageText
from product_radar.supplier_route_acquisition import (PARSER_VERSION, fetch_supplier_native_page,
    parse_supplier_route_receipt, supplier_route_url)
from test_supplier_discovery import NOW
from test_supplier_route_acquisition import BODIES, QUERY, receipt

FIXTURES = Path(__file__).resolve().parent / "fixtures" / "supplier_routes"


def saved_page(name):
    return (FIXTURES / name).read_text(encoding="utf-8")


def saved_receipt(route, html):
    """A receipt whose text is extracted the same way the HTTP reader does."""
    item = {"id": "saved-layout-" + route, "url": supplier_route_url(route, QUERY)}
    page = _PageText(item["url"])
    page.feed(html)
    page.close()
    value = receipt(item, body=html)
    value["text"] = page.extracted()["text"]
    return item, value


class SavedSupplierLayoutTests(unittest.TestCase):
    def test_tradeindia_page_still_loading_is_not_a_featured_products_result(self):
        # The owned browser snapshot captured TradeIndia's loading skeletons.
        # Its only matching link was the footer "Featured Products" index.
        item, value = saved_receipt("tradeindia", saved_page("tradeindia-search-rendered-loading-2026-10-13.html"))
        self.assertIn("react-loading-skeleton", value["html"])
        current = parse_supplier_route_receipt("tradeindia", QUERY, item, value)
        self.assertEqual(current, {"state": "extraction_pending", "reason": "SUPPLIER_ROUTE_LAYOUT_UNRECOGNISED",
                                   "results": []})
        # The saved version 3 decision stays replayable; it is why version 4 exists.
        older = parse_supplier_route_receipt("tradeindia", QUERY, item, value, _parser_version=3)
        self.assertEqual(older["state"], "complete")
        self.assertEqual(older["results"], [{"url": "https://www.tradeindia.com/products/", "title": "Featured Products"}])

    def test_tradeindia_product_cards_remain_results_beside_the_footer_index(self):
        page = saved_page("tradeindia-search-rendered-loading-2026-10-13.html")
        item, value = saved_receipt("tradeindia", page.replace("</h3>", "</h3>" + BODIES["tradeindia"], 1))
        parsed = parse_supplier_route_receipt("tradeindia", QUERY, item, value)
        self.assertEqual(parsed["state"], "complete")
        self.assertEqual([row["url"] for row in parsed["results"]],
                         ["https://www.tradeindia.com/products/food-wrap-paper-123456.html"])

    def test_tradeindia_script_only_http_shell_is_an_unrecognised_layout_not_no_suppliers(self):
        # Since about 9 October the HTTP page is a static script shell; only
        # an unrecognised layout (never "no results") permits the browser read.
        item, value = saved_receipt("tradeindia", saved_page("tradeindia-search-http-shell-2026-10-13.html"))
        self.assertNotIn("Results found", value["text"])
        self.assertEqual(parse_supplier_route_receipt("tradeindia", QUERY, item, value),
                         {"state": "extraction_pending", "reason": "SUPPLIER_ROUTE_LAYOUT_UNRECOGNISED", "results": []})

    def test_indiamart_static_preview_placeholder_cards_are_not_supplier_leads(self):
        item, value = saved_receipt("indiamart", saved_page("indiamart-search-static-preview-2026-10-13.html"))
        self.assertIn("Verified cotton t-shirts supplier", value["text"])
        for version in (3, PARSER_VERSION):
            with self.subTest(parser_version=version):
                self.assertEqual(parse_supplier_route_receipt("indiamart", QUERY, item, value, _parser_version=version),
                                 {"state": "extraction_pending", "reason": "SUPPLIER_ROUTE_LAYOUT_UNRECOGNISED",
                                  "results": []})

    def test_google_javascript_gate_is_never_a_lead_or_a_no_result_search(self):
        item, value = saved_receipt("google-suppliers", saved_page("google-search-enablejs-gate-2026-10-13.html"))
        self.assertIn("having trouble accessing Google Search", value["text"])
        parsed = parse_supplier_route_receipt("google-suppliers", QUERY, item, value)
        self.assertNotEqual(parsed["state"], "complete")
        self.assertEqual(parsed["results"], [])

    def test_google_javascript_gate_is_an_access_challenge_from_version_5(self):
        # Both the minimal saved gate and the fuller one (with Google's own
        # <noscript> refresh to /httpservice/retry/enablejs) are Google refusing
        # to serve results to this reader, not an unreadable layout.
        for name in ("google-search-enablejs-gate-2026-10-13.html", "google-search-enablejs-gate-noscript-2026-10-14.html"):
            with self.subTest(fixture=name):
                item, value = saved_receipt("google-suppliers", saved_page(name))
                self.assertEqual(parse_supplier_route_receipt("google-suppliers", QUERY, item, value),
                                 {"state": "blocked", "reason": "SUPPLIER_ROUTE_CHALLENGE", "results": []})
                # Saved version 4 decisions replay unchanged; that is why version 5 exists.
                self.assertEqual(parse_supplier_route_receipt("google-suppliers", QUERY, item, value, _parser_version=4),
                                 {"state": "extraction_pending", "reason": "SUPPLIER_SEARCH_LAYOUT_UNRECOGNISED", "results": []})

    def test_google_gate_needs_its_sentence_and_google_retry_link_and_never_hides_results(self):
        gate = saved_page("google-search-enablejs-gate-noscript-2026-10-14.html")
        # A rendered results page also carries the <noscript> enablejs refresh.
        item, value = saved_receipt("google-suppliers", gate.replace("</body>", BODIES["google-suppliers"] + "</body>"))
        parsed = parse_supplier_route_receipt("google-suppliers", QUERY, item, value)
        self.assertEqual(parsed["state"], "complete")
        self.assertEqual([row["url"] for row in parsed["results"]], ["https://packaging.example.org/food-paper"])
        # The refresh alone (no gate sentence) is an unrecognised layout.
        item, value = saved_receipt("google-suppliers", gate.replace("If you're having trouble accessing Google Search", "Results"))
        self.assertEqual(parse_supplier_route_receipt("google-suppliers", QUERY, item, value)["reason"],
                         "SUPPLIER_SEARCH_LAYOUT_UNRECOGNISED")
        # The sentence with only non-Google or non-retry links is not the gate either.
        page = ('<html><body><div>If you\'re having trouble accessing Google Search, please '
                '<a href="https://example.org/httpservice/retry/enablejs">click</a> or '
                '<a href="/search?q=other&amp;emsg=OTHER">retry</a>.</div></body></html>')
        item, value = saved_receipt("google-suppliers", page)
        self.assertEqual(parse_supplier_route_receipt("google-suppliers", QUERY, item, value)["state"], "extraction_pending")

    def test_google_consent_and_sorry_pages_are_blocked_never_an_invalid_receipt(self):
        for route, final in (("google-maps", "https://consent.google.com/ml?continue=https://www.google.com/maps"),
                             ("google-suppliers", "https://consent.google.co.in/?continue=https://www.google.co.in/search"),
                             ("google-maps", "https://www.google.co.in/sorry/index?continue=https://www.google.com/maps")):
            with self.subTest(route=route, final=final):
                item, value = saved_receipt(route, "<html><body><h1>Before you continue to Google</h1>"
                                                   "<button>Reject all</button><button>Accept all</button></body></html>")
                value["final_url"] = final
                self.assertEqual(parse_supplier_route_receipt(route, QUERY, item, value),
                                 {"state": "blocked", "reason": "SUPPLIER_ROUTE_CHALLENGE", "results": []})
                self.assertEqual(parse_supplier_route_receipt(route, QUERY, item, value, _parser_version=4)["reason"],
                                 "SUPPLIER_ROUTE_RECEIPT_INVALID")
        # A directory route is not given Google's interstitial meaning.
        item, value = saved_receipt("indiamart", "<html><body>Before you continue</body></html>")
        value["final_url"] = "https://consent.google.com/ml"
        self.assertEqual(parse_supplier_route_receipt("indiamart", QUERY, item, value)["reason"], "SUPPLIER_ROUTE_RECEIPT_INVALID")

    def test_google_sign_in_interstitial_is_blocked_from_version_5(self):
        # A Google route that lands on Google's sign-in page was refused, not
        # served an invalid receipt to retry. No account is ever used.
        for route, final in (("google-maps", "https://accounts.google.com/ServiceLogin?continue=https://www.google.com/maps/search/"),
                             ("google-suppliers", "https://accounts.google.com/v3/signin/identifier?continue=https://www.google.com/search"),
                             ("google-maps", "https://accounts.google.co.in/ServiceLogin"),
                             ("google-maps", "https://www.google.com/ServiceLogin?continue=https://www.google.com/maps")):
            with self.subTest(route=route, final=final):
                item, value = saved_receipt(route, "<html><body><h1>Sign in</h1><p>Use your Google Account</p></body></html>")
                value["final_url"] = final
                self.assertEqual(parse_supplier_route_receipt(route, QUERY, item, value),
                                 {"state": "blocked", "reason": "SUPPLIER_ROUTE_CHALLENGE", "results": []})
                # Saved version 4 decisions replay unchanged.
                self.assertEqual(parse_supplier_route_receipt(route, QUERY, item, value, _parser_version=4)["reason"],
                                 "SUPPLIER_ROUTE_RECEIPT_INVALID")
        # A directory route is not given Google's sign-in meaning.
        item, value = saved_receipt("tradeindia", "<html><body>Sign in</body></html>")
        value["final_url"] = "https://accounts.google.com/ServiceLogin"
        self.assertEqual(parse_supplier_route_receipt("tradeindia", QUERY, item, value)["reason"], "SUPPLIER_ROUTE_RECEIPT_INVALID")

    def test_indiamart_login_wall_is_an_access_challenge_from_version_5(self):
        # Saved 14 October 2026: plain and browser reads both returned IndiaMART's
        # sign-in (OTP) wall with the same placeholder cards for every query.
        for name in ("indiamart-search-login-wall-http-2026-10-14.html",
                     "indiamart-search-login-wall-rendered-2026-10-14.html"):
            with self.subTest(fixture=name):
                item, value = saved_receipt("indiamart", saved_page(name))
                self.assertIn("Verified cotton t-shirts supplier", value["text"])
                self.assertEqual(parse_supplier_route_receipt("indiamart", QUERY, item, value),
                                 {"state": "blocked", "reason": "SUPPLIER_ROUTE_CHALLENGE", "results": []})
                # Saved version 4 decisions replay unchanged.
                self.assertEqual(parse_supplier_route_receipt("indiamart", QUERY, item, value, _parser_version=4),
                                 {"state": "extraction_pending", "reason": "SUPPLIER_ROUTE_LAYOUT_UNRECOGNISED", "results": []})

    def test_indiamart_wall_needs_its_flag_empty_search_noindex_and_login_marker(self):
        wall = saved_page("indiamart-search-login-wall-http-2026-10-14.html")
        def parsed(html):
            item, value = saved_receipt("indiamart", html)
            return parse_supplier_route_receipt("indiamart", QUERY, item, value)
        self.assertEqual(parsed(wall)["state"], "blocked")
        # A real result card is never hidden by the wall markers.
        page = parsed(wall.replace("</body>", BODIES["indiamart"] + "</body>"))
        self.assertEqual(page["state"], "complete")
        self.assertEqual([row["url"] for row in page["results"]],
                         ["https://www.indiamart.com/proddetail/food-wrap-paper-123456.html"])
        # Each required part on its own is not the wall.
        for index, changed in enumerate((wall.replace('"dummyOtpPage":true', '"dummyOtpPage":false'),
                        wall.replace('"dummyOtpPage":true', '"dummyOtpPage":"true"'),
                        wall.replace('"searchResponse":null', '"searchResponse":{"results":[]}'),
                        wall.replace("noindex, nofollow", "index, follow"),
                        wall.replace('id="loginstrip"', 'id="topstrip"'),
                        wall.replace('id="__NEXT_DATA__"', 'id="other-data"'))):
            with self.subTest(variant=index):
                self.assertEqual(parsed(changed)["reason"], "SUPPLIER_ROUTE_LAYOUT_UNRECOGNISED")
        # The rendered prompt alone also identifies the login marker.
        rendered = saved_page("indiamart-search-login-wall-rendered-2026-10-14.html").replace('id="loginstrip"', 'id="topstrip"')
        self.assertEqual(parsed(rendered)["state"], "blocked")

    def test_rendered_maps_results_page_nominates_place_cards(self):
        # Owned-browser Maps snapshot of 8 October 2026 (old flow, 18 of 30 reads).
        item, value = saved_receipt("google-maps", saved_page("google-maps-search-rendered-results-2026-10-08.html"))
        for version in (4, PARSER_VERSION):
            with self.subTest(parser_version=version):
                parsed = parse_supplier_route_receipt("google-maps", QUERY, item, value, _parser_version=version)
                self.assertEqual(parsed["state"], "complete")
                self.assertEqual(len(parsed["results"]), 10)
                self.assertTrue(all(row["url"].startswith("https://www.google.com/maps/place/") for row in parsed["results"]))
                self.assertEqual(parsed["results"][0]["title"], "Kumar Plasto India Pvt. Ltd.")

    def test_rendered_maps_rewritten_address_with_no_results_is_a_genuine_empty_search(self):
        # Rendered Maps rewrote the address to /maps/search/<query with +>/@<view>.
        # Version 4 mis-read the "+" and called a real "can't find" page invalid.
        page = saved_page("google-maps-search-rendered-no-results-2026-10-08.html")
        self.assertIn("Google Maps can't find", page)
        rewritten = ("https://www.google.com/maps/search/" + "+".join((QUERY + " manufacturer supplier India").split())
                     + "/@28.5,77.1,11z?entry=fixture")
        item, value = saved_receipt("google-maps", page)
        value["final_url"] = rewritten
        self.assertEqual(parse_supplier_route_receipt("google-maps", QUERY, item, value),
                         {"state": "complete", "reason": "The readable bounded search explicitly reported no results.",
                          "results": []})
        self.assertEqual(parse_supplier_route_receipt("google-maps", QUERY, item, value, _parser_version=4)["reason"],
                         "SUPPLIER_ROUTE_RECEIPT_INVALID")
        # A different or extended query segment is never this search's empty result.
        for changed in (rewritten.replace("Food+wrap+paper", "Food+wrap+foil"),
                        rewritten.replace("+India/", "+India+near+me/")):
            with self.subTest(final=changed):
                value["final_url"] = changed
                self.assertEqual(parse_supplier_route_receipt("google-maps", QUERY, item, value)["reason"],
                                 "SUPPLIER_ROUTE_RECEIPT_INVALID")

    def test_maps_http_page_is_a_script_only_application_shell(self):
        # The plain HTTP read of Maps returns HTTP 200 HTML with no readable
        # text (30 of 30 saved reads); the real reader records that exactly.
        from product_radar.public_pages import fetch_public_page
        from test_public_pages import Response, Transport, resolver
        html = saved_page("google-maps-search-http-app-shell-2026-10-14.html")
        self.assertNotIn("<script", html)
        item = {"id": "saved-maps-shell", "url": supplier_route_url("google-maps", QUERY)}
        value = fetch_public_page(item, resolver=resolver, connection_factory=Transport([Response(body=html.encode())]),
                                  retain_transport=True)
        self.assertEqual((value["state"], value["reason"], value["http_status"], value["body_complete"]),
                         ("unavailable", "PUBLIC_TEXT_UNAVAILABLE", 200, True))
        self.assertEqual(parse_supplier_route_receipt("google-maps", QUERY, item, value),
                         {"state": "failed", "reason": "PUBLIC_TEXT_UNAVAILABLE", "results": []})

    def test_native_snapshot_waits_only_for_each_directory_own_placeholders(self):
        pages = {"tradeindia": ("tradeindia-search-rendered-loading-2026-10-13.html", "react-loading-skeleton"),
                 "indiamart": ("indiamart-search-static-preview-2026-10-13.html", "staticSearchPreview")}
        with tempfile.TemporaryDirectory() as folder:
            for route in ("tradeindia", "indiamart", "google-maps"):
                calls = []
                def native(url, expression, target, **options):
                    calls.append(options)
                    return {"state": "failed", "navigation_started": False, "stage": "browser_launch",
                            "reason": "NATIVE_PUBLIC_BROWSER_NOT_INSTALLED"}
                item = {"id": "native-wait-" + route, "url": supplier_route_url(route, QUERY)}
                value = fetch_supplier_native_page(item, route=route, folder=Path(folder) / route, native_capture=native)
                self.assertEqual(value["execution_stage"], "not_dispatched")
                with self.subTest(route=route):
                    if route not in pages:
                        self.assertEqual(calls, [{}])
                        continue
                    name, placeholder = pages[route]
                    self.assertEqual(set(calls[0]), {"ready_expression"})
                    ready = calls[0]["ready_expression"]
                    # The waited-for selector is the one the saved page really shows,
                    # and the wait is capped well inside the 45-second source deadline.
                    self.assertIn("'." + placeholder + "'", ready)
                    self.assertIn('class="' + placeholder, saved_page(name))
                    self.assertIn("performance.now() > 15000", ready)
                    self.assertIn("document.readyState === 'loading'", ready)


if __name__ == "__main__":
    unittest.main()
