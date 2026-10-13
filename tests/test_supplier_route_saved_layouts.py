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
