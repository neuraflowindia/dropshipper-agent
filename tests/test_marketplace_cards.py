"""Parser v2 crowding card facts on trimmed real saved first results pages.

The fixtures are cut from actual saved Amazon India and Flipkart pages (card
markup and the page's own result-count line only; links, scripts, images and
page chrome removed). No page is visited and no model is called.
"""

import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from product_radar import marketplace_cards
from product_radar.marketplace_cards import (CARD_PARSER_VERSION, SIDE_FILE, merge_card_facts, parse_saved_cards,
                                             read_capture_cards, total_results)
from product_radar.models import SafetyError

FIXTURES = ROOT / "tests/fixtures/crowding"


def page(name):
    return ((FIXTURES / (name + ".html")).read_text(encoding="utf-8"),
            json.loads((FIXTURES / (name + ".snapshot.json")).read_text(encoding="utf-8"))["text"])


def card(identity, *pieces, labels=(), brand=None):
    """A small Amazon search card in the saved page's own shape."""
    label = "".join(f'<span aria-label="{value}"></span>' for value in labels)
    head = f'<h2 class="a-size-mini s-line-clamp-1"><span>{brand}</span></h2>' if brand else ""
    body = "".join(f"<span>{piece}</span>" for piece in pieces)
    return f'<div data-asin="{identity}" class="s-result-item s-asin">{head}{label}{body}</div>'


class RealPageTests(unittest.TestCase):
    def test_amazon_search_reads_ratings_stars_brand_bought_and_sponsored_per_card(self):
        result = parse_saved_cards("amazon-search", *page("amazon-search-camphor-cones"))
        self.assertEqual((result["parser_version"], result["state"], result["total_results"],
                          result["total_results_lower_bound"]), (2, "read", 337, False))
        cards = result["cards"]
        self.assertEqual(list(cards), ["B0BLLNWWY5", "B0GRMKRBDS", "B0GMDHJDWX", "B07ZJSK969"])
        self.assertEqual(cards["B0BLLNWWY5"], {"ratings_count": 3752, "star_rating": "4.0", "brand": "CamPure",
            "bought_past_month": 900, "bought_text": "900+ bought in past month", "sponsored": True,
            "list_rank": None, "bestseller_badge": False, "position": 1})
        self.assertEqual((cards["B0GRMKRBDS"]["ratings_count"], cards["B0GRMKRBDS"]["brand"],
                          cards["B0GRMKRBDS"]["bought_past_month"]), (14, None, None))
        self.assertEqual((cards["B0GMDHJDWX"]["brand"], cards["B0GMDHJDWX"]["bought_past_month"]), ("Haka", 1000))
        self.assertEqual((cards["B07ZJSK969"]["sponsored"], cards["B07ZJSK969"]["ratings_count"],
                          cards["B07ZJSK969"]["star_rating"]), (False, 5813, "4.1"))

    def test_amazon_best_seller_badge_is_read_from_the_search_card(self):
        result = parse_saved_cards("amazon-search", *page("amazon-search-backpack-rain-covers"))
        self.assertEqual({identity: row["bestseller_badge"] for identity, row in result["cards"].items()},
                         {"B0D1CNN4MG": True, "B0C7ZWNBJZ": False})
        self.assertEqual(result["cards"]["B0D1CNN4MG"]["brand"], "Boldfit")

    def test_flipkart_grid_without_ratings_leaves_the_count_unknown(self):
        result = parse_saved_cards("flipkart-search", *page("flipkart-search-backpack-rain-covers"))
        self.assertEqual((result["total_results"], len(result["cards"])), (14559, 2))
        self.assertTrue(all(row["ratings_count"] is None and row["star_rating"] is None for row in result["cards"].values()))

    def test_amazon_carousel_result_and_sponsored_label_and_lower_bound_total(self):
        result = parse_saved_cards("amazon-search", *page("amazon-search-baby-bath-towels"))
        self.assertEqual((result["total_results"], result["total_results_lower_bound"]), (20000, True))
        self.assertEqual(result["cards"]["B0F27RZYLM"]["sponsored"], True)  # "Sponsored Ad - ..." title label
        self.assertEqual((result["cards"]["B0F27RZYLM"]["ratings_count"],
                          result["cards"]["B0F27RZYLM"]["bought_past_month"]), (60, 50))

    def test_flipkart_grid_cards_read_stars_and_bracketed_rating_counts(self):
        result = parse_saved_cards("flipkart-search", *page("flipkart-search-tealight-candles"))
        self.assertEqual((result["total_results"], result["total_results_lower_bound"]), (11623, False))
        self.assertEqual({identity: (row["ratings_count"], row["star_rating"]) for identity, row in result["cards"].items()},
                         {"DIYHFBTFR27N5ZCS": (41, "3.9"), "CANGTRDVYFKHHRRM": (1401, "3.5"),
                          "CANHQP28RVTG6ZAA": (216, "4.3"), "CANHPF5ZV3HHZWPD": (6, "3.8")})
        self.assertTrue(all(row["brand"] is None and row["bought_past_month"] is None and row["sponsored"] is False
                            for row in result["cards"].values()))

    def test_flipkart_list_layout_reads_indian_grouped_rating_counts(self):
        result = parse_saved_cards("flipkart-search", *page("flipkart-search-juicer-mixer-grinder"))
        self.assertEqual(result["total_results"], 1274)
        self.assertEqual({identity: (row["ratings_count"], row["star_rating"]) for identity, row in result["cards"].items()},
                         {"MIXGRH82YRZPZBZH": (69755, "4.2"), "MIXHMECM5RQF7TNE": (239048, "4.1")})

    def test_amazon_bestseller_list_reads_rank_and_ratings_without_a_total(self):
        result = parse_saved_cards("amazon-bestsellers", *page("amazon-bestsellers-kitchen"))
        self.assertEqual((result["total_results"], result["total_results_lower_bound"]), (None, False))
        self.assertEqual({identity: (row["list_rank"], row["ratings_count"], row["star_rating"])
                          for identity, row in result["cards"].items()},
                         {"B07QP9PTZP": (1, 912879, "4.5"), "B0821PN8L4": (2, 54676, "4.4")})


class ParserRuleTests(unittest.TestCase):
    def test_same_product_twice_is_one_listing_with_the_larger_counts(self):
        html = (card("B000000001", "Sponsored", "(1.2K)", "500+ bought in past month")
                + card("B000000001", "4.1 out of 5 stars", "2K+ bought in past month", labels=("1,150 ratings",), brand="Acme"))
        row = parse_saved_cards("amazon-search", html, "")["cards"]["B000000001"]
        self.assertEqual((row["ratings_count"], row["bought_past_month"], row["sponsored"], row["brand"],
                          row["star_rating"], row["position"]), (1200, 2000, True, "Acme", "4.1", 1))
        self.assertEqual(merge_card_facts({"ratings_count": None, "sponsored": False}, {"ratings_count": None})["ratings_count"], None)

    def test_compact_counts_hidden_text_and_unrelated_markup(self):
        html = ('<div data-asin="" class="s-result-item"><span>ignored</span></div>'
                '<div data-asin="B000000002" class="s-result-item"><script>var x="(9K)"</script><span>(3M)</span>'
                '<br><img alt="x"><span>1.5K+ bought in past month</span></div>'
                '<div data-asin="nope" class="s-result-item"><span>(5)</span></div>')
        result = parse_saved_cards("amazon-search", html, "")
        self.assertEqual(list(result["cards"]), ["B000000002"])
        self.assertEqual((result["cards"]["B000000002"]["ratings_count"], result["cards"]["B000000002"]["bought_past_month"]),
                         (3000000, 1500))

    def test_label_on_the_card_root_and_stray_closing_tags(self):
        html = ('</div></span><div data-asin="B000000003" class="s-result-item" aria-label="2,400 ratings">'
                '<span>4.4 out of 5 stars</span></div>')
        row = parse_saved_cards("amazon-search", html, "")["cards"]["B000000003"]
        self.assertEqual((row["ratings_count"], row["star_rating"]), (2400, "4.4"))

    def test_no_cards_is_not_an_empty_market(self):
        result = parse_saved_cards("flipkart-search", "<html><body><p>Sorry</p></body></html>", "No results")
        self.assertEqual((result["state"], result["reason"], result["cards"]), ("no_cards", "CROWDING_CARDS_NOT_FOUND", {}))

    def test_total_result_lines(self):
        self.assertEqual(total_results("16 results for \"x\""), (16, False))
        self.assertEqual(total_results("Showing 1 – 40 of 2,39,048 results for \"x\""), (239048, False))
        self.assertEqual(total_results("1-48 of over 1,000 results for"), (1000, True))
        self.assertEqual(total_results("nothing here"), (None, False))
        self.assertEqual(total_results(None), (None, False))

    def test_invalid_input_fails_closed(self):
        for args in (("amazon-movers-and-shakers", "", ""), ("amazon-search", None, ""), ("amazon-search", "", None),
                     ("amazon-search", "x" * (marketplace_cards.MAX_HTML + 1), "")):
            with self.assertRaisesRegex(SafetyError, "CROWDING_CARD_INPUT_INVALID"):
                parse_saved_cards(*args)


class SavedCaptureTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory(); self.addCleanup(temp.cleanup)
        self.folder = Path(temp.name) / "capture"
        self.folder.mkdir()
        html, text = page("flipkart-search-tealight-candles")
        (self.folder / "rendered.html").write_text(html, encoding="utf-8")
        (self.folder / "snapshot.json").write_text(json.dumps({"text": text}), encoding="utf-8")

    def test_side_file_is_written_once_and_reused_until_the_saved_bytes_change(self):
        first = read_capture_cards(self.folder, "flipkart-search")
        side = json.loads((self.folder / SIDE_FILE).read_text())
        self.assertEqual((side["parser_version"], set(side["source_hashes"])), (CARD_PARSER_VERSION, {"rendered.html", "snapshot.json"}))
        with patch.object(marketplace_cards, "parse_saved_cards", side_effect=AssertionError("re-read")):
            self.assertEqual(read_capture_cards(self.folder, "flipkart-search"), first)
        (self.folder / "rendered.html").write_text("<div data-id=\"ABCDEF1234\"><span>(7)</span></div>", encoding="utf-8")
        changed = read_capture_cards(self.folder, "flipkart-search")
        self.assertEqual(changed["cards"]["ABCDEF1234"]["ratings_count"], 7)
        (self.folder / SIDE_FILE).write_text("{broken", encoding="utf-8")
        self.assertEqual(read_capture_cards(self.folder, "flipkart-search"), changed)

    def test_missing_saved_page_is_unreadable_never_empty(self):
        (self.folder / "rendered.html").unlink()
        result = read_capture_cards(self.folder, "flipkart-search")
        self.assertEqual((result["state"], result["reason"]), ("unreadable", "CROWDING_SAVED_PAGE_UNAVAILABLE"))
        self.assertFalse((self.folder / SIDE_FILE).exists())

    def test_snapshot_without_text_and_unknown_route(self):
        (self.folder / "snapshot.json").write_text(json.dumps({"cards": []}), encoding="utf-8")
        self.assertIsNone(read_capture_cards(self.folder, "flipkart-search")["total_results"])
        with self.assertRaisesRegex(SafetyError, "CROWDING_CARD_ROUTE_INVALID"):
            read_capture_cards(self.folder, "meta-ads")


if __name__ == "__main__":
    unittest.main()
