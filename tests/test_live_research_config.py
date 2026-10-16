"""The LIVE research settings are valid and consistent with each other.

The map-lane tests run against frozen r10 copies (``frozen_research_config``)
so their exact expectations do not move as the map grows. These checks read
the live files themselves: ``config/research/catalogue.json``,
``config/research/product_map.json`` and
``config/research/dropship_platforms.json``. Nothing is written.
"""

from collections import Counter
from pathlib import Path
import sys
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
from product_radar.catalogue import CATALOGUE_LIMIT, catalogue_summary, validate_catalogue
from product_radar.catalogue_rows import CATEGORY_CODES, encode_settings
from product_radar.dropship_platforms import _settings_value
from product_radar.map_materials import SETTINGS_LIMIT, validate_settings
from product_radar.service import CATEGORIES
from product_radar.storage import load_json

CONFIG = ROOT / "config" / "research"


class LiveResearchConfigTests(unittest.TestCase):
    def setUp(self):
        self.catalogue = validate_catalogue(load_json(CONFIG / "catalogue.json", CATALOGUE_LIMIT))
        self.settings = load_json(CONFIG / "product_map.json", SETTINGS_LIMIT)
        self.platforms = _settings_value(load_json(CONFIG / "dropship_platforms.json", 64 * 1024))
        self.sub_areas = [row for row in self.catalogue["branches"] if row["parent_id"] is not None]
        self.usable = [row for row in self.catalogue["families"] if row["disposition"] == "usable"]

    def test_the_catalogue_is_the_big_map(self):
        self.assertGreaterEqual(len(self.sub_areas), 48)
        self.assertGreaterEqual(len(self.catalogue["families"]), 1600)
        summary = catalogue_summary(self.catalogue)
        self.assertEqual(summary["thin_categories"], [])
        roots = {row["id"]: row for row in self.catalogue["branches"] if row["parent_id"] is None}
        self.assertEqual({row["category"] for row in roots.values()}, set(CATEGORIES))
        for row in self.sub_areas:  # each sub-area sits under its category's root, named by its code
            code = row["id"].split("-")[1]
            self.assertEqual((row["parent_id"], CATEGORY_CODES[code]), ("branch-" + code, row["category"]))
        counts = Counter(row["parent_id"] for row in self.usable)
        self.assertEqual([row["id"] for row in self.sub_areas if not counts[row["id"]]], [])  # no empty sub-area
        for family in self.usable:  # every usable heading can be searched
            self.assertTrue(any(phrase["source"] == "meta" and phrase["purpose"] == "discovery"
                                for phrase in family["phrases"]), family["id"])

    def test_every_sub_area_has_routing_words_and_the_settings_match_the_catalogue(self):
        validate_settings(self.settings, catalogue=self.catalogue)
        self.assertEqual(set(self.settings["branch_words"]), {row["id"] for row in self.sub_areas})
        self.assertEqual(self.settings["target"]["headings_per_sub_area"], 40)
        # A fresh store must not call the AI top-up at once: the threshold is below the live map's size.
        self.assertLess(self.settings["topup"]["min_unsearched_headings"], len(self.usable))
        # The file keeps the expander's layout, so a rewrite changes nothing.
        self.assertEqual(encode_settings(self.settings), (CONFIG / "product_map.json").read_bytes())

    def test_every_category_word_gates_to_its_own_category(self):
        from product_radar.wider_candidates import gate
        categories = self.platforms["discovery"]["categories"]
        self.assertEqual(set(categories), set(CATEGORIES))
        wrong = [(category, word, gate(word)) for category, words in categories.items() for word in words
                 if gate(word) != category]
        self.assertEqual(wrong, [])
        excluded = set(self.platforms["discovery"]["exclude"])
        self.assertEqual([word for words in categories.values() for word in words if word in excluded], [])

    def test_known_products_gate_to_the_right_category_or_none(self):
        from product_radar.wider_candidates import gate
        table = {
            # Outside the client's categories (food, supplements, large electronics, excluded kinds).
            "India Gate basmati rice 5 kg": None, "Melatonin sleep gummies": None, "Smart LED TV 43 inch": None,
            "Gold plated necklace set": None, "Pet dog chew toy": None, "Mobile phone back cover": None,
            # In scope, including the newer sub-areas (party, home office, crafts, pregnancy, car care, swimming, repair).
            "Self watering plant pots": "Home and Decor", "Baby feeding spoons": "Baby Products",
            "Car cup holders": "Travel and Outdoor", "Swaddle blankets": "Baby Products",
            "Rice string lights": "Home and Decor", "sleeping bag": "Travel and Outdoor",
            "neck pillow": "Travel and Outdoor", "Birthday party balloons": "Home and Decor",
            "Laptop stand for desk": "Home and Decor", "Kids colouring kit": "Baby Products",
            "Pregnancy pillow": "Baby Products", "Maternity belt": "Baby Products",
            "Car wash shampoo": "Travel and Outdoor", "Swimming goggles": "Travel and Outdoor",
            "Screwdriver set": "Home and Kitchen", "Home repair tool kit": "Home and Kitchen",
            "Bicycle tool kit": "Travel and Outdoor", "LED bulb": "Home and Decor"}
        self.assertEqual({text: gate(text) for text in table}, table)
        # Staging: the in-scope products reach a sub-area of their own category; the others never stage.
        from product_radar.map_materials import branch_profiles, place, word_set
        profiles = branch_profiles(self.catalogue, self.settings)
        sub_areas = {row["id"]: row["category"] for row in self.sub_areas}
        placed = {text: place(word_set(text), profiles, gate(text)) for text in table}
        self.assertEqual([text for text, category in table.items() if category is None and placed[text]], [])
        missing = [text for text, category in table.items() if category is not None and placed[text] is None]
        self.assertEqual(missing, [])
        exceptions = {"Bicycle tool kit"}  # "tool kit" is a home-repair routing phrase: a multi-word match wins
        self.assertEqual([text for text, category in table.items() if category and text not in exceptions
                          and sub_areas[placed[text]] != category], [])


class GateRuleTests(unittest.TestCase):
    def test_the_longest_matching_category_phrase_decides(self):
        from product_radar.platform_roles import item_category
        config = {"categories": {"Home and Decor": ["pillow", "home"], "Travel and Outdoor": ["neck pillow", "car"]},
                  "exclude": ["pet"]}
        self.assertEqual(item_category({"title": "Memory foam neck pillow"}, config), "Travel and Outdoor")
        self.assertEqual(item_category({"title": "Sofa pillow"}, config), "Home and Decor")
        self.assertEqual(item_category({"title": "Home car organiser"}, config), "Home and Decor")  # a tie: file order
        self.assertIsNone(item_category({"title": "Pet car seat"}, config))
        self.assertIsNone(item_category({"title": "Steel bottle"}, config))

if __name__ == "__main__":
    unittest.main()
