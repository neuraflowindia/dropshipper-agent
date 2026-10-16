"""The bigger product map (Plan R10 W10): branch inputs from the dropshipping
platforms' categories and items, Google Trends rising queries, relevant
advertisers' Meta ads and IndiaMART/TradeIndia listings; the category gate and
de-duplication before any AI call; admission and merge through the seed C
call; full sub-areas and waiting material; renewal through the expansion call;
the round's AI allowance and the rotation of the AI calls held for discovery;
the wake hint; the new prompt and schema versions; daily backups.

Material is built from synthetic store files; the seed and expansion runners
are offline doubles. No network, browser or model is used.
"""

from copy import deepcopy
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "tests"))
from product_radar import catalogue_discovery as discovery
from product_radar import map_materials as materials
from product_radar import product_map
from product_radar.catalogue import load_catalogue, validate_catalogue
from product_radar.catalogue_expansion import effective_catalogue
from product_radar.catalogue_seed import _input_material, _load_material
from product_radar.dropship_platforms import _empty, save_catalogue
from product_radar.models import SafetyError, canonical, digest
from product_radar.research_memory import ResearchMemory
from product_radar.storage import Journal, atomic_json, load_json
from product_radar.trends_rising import FILE as RISING_FILE, KIND as RISING_KIND, PARSER_VERSION as RISING_PARSER
from product_radar.wider_candidates import READ_KIND
from product_radar.work_turns import DEFAULT_POLICY, bind_work_turn, open_work_turn

AT = "2026-10-15T06:00:00+00:00"
LATER = "2026-10-15T09:00:00+00:00"

sys.path.insert(0, str(Path(__file__).resolve().parent))
import frozen_research_config  # noqa: E402
# Exact expectations use the r10 settings these tests were written against, not the growing live map.
setUpModule, tearDownModule = frozen_research_config.module_fixtures()


def item(item_id, title, *, product_type=None, roles=(), tags=(), at=AT):
    return {"item_id": item_id, "title": title, "url": "https://deodap.in/products/" + item_id, "images": [],
            "price": "99", "mrp": None, "price_basis": "Wholesale price.", "in_stock": True, "vendor": None,
            "product_type": product_type, "tags": list(tags), "created_at": None, "terms": None,
            "first_seen_at": at, "last_seen_at": at, "source_ref": "raw/fixture.json", "collections": {},
            "roles": {role: {"first_seen_at": at, "last_seen_at": at, "via": ["fixture"]} for role in roles}}


def collection(handle, title, roles=()):
    return {"handle": handle, "title": title, "url": "https://deodap.in/collections/" + handle,
            "products_count": 3, "roles": list(roles), "parent": None}


class SeedRunner:
    """Admits every supplied example as its own heading (one dropship phrase too)."""
    identity = "synthetic-map-seed-v1"

    def __init__(self, decide=None):
        self.calls, self.decide = [], decide

    def __call__(self, role, payload, folder):
        self.calls.append(deepcopy(payload))
        rows = []
        for entry in payload["items"]:
            label = entry["material"]["label"]
            row = {"id": entry["id"], "decision": "new_family", "match_id": None, "category": entry["category"],
                   "parent_id": entry["branch"]["id"], "label": label, "problem": "A specific everyday task.",
                   "distinction": "A distinct practical function, not a colour or seller change.", "basis": "observed",
                   "evidence_ids": [entry["evidence"][0]["id"]], "overlap_ids": [],
                   "phrases": [{"text": label.casefold()[:60], "source": "meta", "purpose": "discovery", "language": "en",
                                "country": "IN", "reason": "Find ads showing the function."},
                               {"text": label.casefold()[:60], "source": "dropship", "purpose": "validation",
                                "language": "en", "country": "IN", "reason": "Find it in the platforms' catalogues."}],
                   "action_purpose": "discovery", "reason": "Useful supplied function."}
            if self.decide:
                self.decide(row, entry)
            rows.append(row)
        return {"results": rows}, {"accounting": "synthetic"}


def heading(label, *, meta=None, included=None, season="evergreen", months=()):
    return {"label": label, "purpose": "To keep " + label.casefold() + " ready for daily use.",
            "included_example": included or label.casefold().rstrip("s"), "excluded_neighbour": "storage boxes",
            "meta_phrase": meta or label.casefold(), "season": season, "months": list(months)}


class TopupRunner:
    """Answers the top-up call with the headings ``answer(item)`` gives (one new heading by default)."""
    identity = "synthetic-map-topup-v1"

    def __init__(self, answer=None):
        self.calls, self.answer = [], answer

    def __call__(self, role, payload, folder):
        self.calls.append(deepcopy(payload))
        entry = payload["items"][0]
        rows = self.answer(entry) if self.answer else [heading("Foldable widget stands")]
        return {"results": [{"id": entry["id"], "headings": rows, "reason": "Useful gaps."}]}, {"accounting": "synthetic"}


class ExpansionRunner:
    """Proposes a heading for each supplied type in its category's first sub-area."""
    identity = "synthetic-map-expansion-v1"

    def __init__(self):
        self.calls = []

    def __call__(self, role, payload, folder):
        self.calls.append(deepcopy(payload))
        rows = []
        for entry in payload["items"]:
            parent = next(row["id"] for row in entry["branches"] if row["parent_id"] is not None)
            phrase = {"text": entry["description"]["label"].casefold(), "source": "meta", "purpose": "discovery",
                      "language": "en", "country": "IN", "reason": "Find ads showing the function."}
            rows.append({"id": entry["id"], "decision": "new_family", "match_id": None,
                         "category": entry["description"]["category"], "label": entry["description"]["label"],
                         "problem": "A specific task.", "distinction": "A distinct mechanism.", "parent_id": parent,
                         "basis": "plausible_extension", "evidence_ids": [], "primary_phrase": phrase,
                         "alternative_phrases": [dict(phrase, source="dropship", purpose="validation")],
                         "reason": "A useful sibling direction."})
        return {"results": rows}, {"accounting": "synthetic"}


class Case(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.memory = ResearchMemory(self.root / "research", Journal(self.root / "finance"), evidence_type="synthetic")
        self.base = load_catalogue()
        # The saved map has fewer never-searched headings than the top-up
        # threshold, so the AI top-up would join every round. These cases are
        # about staging, building and renewal: here the map counts as well
        # stocked. ``TopupTests`` use the saved threshold.
        stocked = deepcopy(materials.settings())
        stocked["topup"]["min_unsearched_headings"] = 0
        for module in (materials, product_map):
            patcher = patch.object(module, "settings", return_value=stocked)
            patcher.start()
            self.addCleanup(patcher.stop)

    def platform(self, collections=(), items=(), route="deodap", at=AT):
        value = _empty(route)
        value.update(updated_at=at, full_completed_at=at, collections=list(collections),
                     items={row["item_id"]: row for row in items})
        save_catalogue(self.memory.root, route, value)

    def rising(self, queries, term="kitchen gadget", folder="trends-1", at=AT):
        rows = [{"rank": index + 1, "query": text, "raw_value": "Breakout" if index == 0 else "+250%",
                 "growth_kind": "breakout" if index == 0 else "percentage", "growth_percent": None if index == 0 else 250.0}
                for index, text in enumerate(queries)]
        atomic_json(self.memory.root / "direct_trends" / folder / RISING_FILE,
                    {"kind": RISING_KIND, "parser_version": RISING_PARSER, "state": "read", "queries": rows,
                     "observed_at": at, "term": term, "acquisition_id": "acq-" + folder})

    def wider_read(self, lane, route, rows, acquisition_id, at=AT):
        from product_radar.wider_discovery import NOMINATION_VERSION
        folder = self.memory.root / "wider_discovery" / acquisition_id
        name = "receipt.json" if lane == "supplier-listings" else "nomination.json"
        atomic_json(folder / name, {"version": NOMINATION_VERSION, "acquisition_id": acquisition_id, "lane": lane,
                                    "route": route, "observed_at": at, "baseline_concepts": [], "rows": rows})
        record_id = "read-" + acquisition_id
        self.memory.put("batch_runs", record_id, {"kind": READ_KIND, "id": record_id, "lane": lane, "route": route,
            "key": acquisition_id, "state": "read", "capture_id": "capture-" + acquisition_id,
            "acquisition_id": acquisition_id, "nomination_folder": str(folder), "started_at": at, "updated_at": at})

    def advance(self, runner=None, at=AT, **context):
        return product_map.advance_product_map(self.memory, dict({"at": at, "runner": runner}, **context),
                                               programme_id="programme", catalogue=self.base)

    def examined(self, config=None):
        return materials.load_examined(self.memory, product_map.config_hash(config or materials.settings(), self.base))

    def staged(self):
        return {row["kind"] + ":" + _load_material(self.memory, row["material_id"])["label"]: row["branch_id"]
                for row in self.examined().values() if row.get("outcome") == "staged"}

    def concept(self, concept_id, label, category="Home and Kitchen"):
        self.memory.put("concepts", concept_id, {"status": "provisional", "description": {
            "label": label, "category": category, "function": "Holds a specific item.", "mechanism": "Clip-on frame",
            "audience": "Households", "features": [], "basis": "observed", "brand": "unclear", "reason": "Saved."}})


class SettingsTests(Case):
    def test_twelve_sub_areas_per_category_with_routing_words_and_a_soft_target_near_1900(self):
        config = load_json(materials.SETTINGS, materials.SETTINGS_LIMIT)
        branches = [row for row in self.base["branches"] if row["parent_id"] is not None]
        from product_radar.service import CATEGORIES
        self.assertEqual({category: sum(row["category"] == category for row in branches) for category in CATEGORIES},
                         {category: 12 for category in CATEGORIES})
        self.assertEqual(set(config["branch_words"]), {row["id"] for row in branches})
        self.assertEqual(len(branches) * config["target"]["headings_per_sub_area"], 1920)
        self.assertEqual(config["topup"], {"min_unsearched_headings": 300, "headings_per_call": 12,
                                           "max_calls_per_day": 6, "retry_minutes": 60})
        self.assertEqual(len(self.base["families"]), 224)  # the existing headings stay the base

    def test_bad_settings_are_refused(self):
        good = load_json(materials.SETTINGS, 64 * 1024)
        broken_words = deepcopy(good)
        broken_words["branch_words"].pop("branch-hk-preparation-tools")
        upper = deepcopy(good)
        upper["branch_words"]["branch-hk-preparation-tools"] = ["Chopper"]
        no_topup = {name: value for name, value in good.items() if name != "topup"}
        for value in (dict(good, extra=1), dict(good, kind="x"), broken_words, upper,
                      dict(good, build=dict(good["build"], materials_per_call=11)),
                      dict(good, renewal=dict(good["renewal"], concepts_per_call=6)), no_topup,
                      dict(good, topup=dict(good["topup"], headings_per_call=21)),
                      dict(good, topup=dict(good["topup"], max_calls_per_day=-1)),
                      dict(good, topup=dict(good["topup"], extra=1)), [], None):
            with self.subTest(value=str(value)[:60]):
                with self.assertRaisesRegex(SafetyError, "PRODUCT_MAP_SETTINGS_INVALID"):
                    materials.validate_settings(value)

    def test_the_map_settings_are_a_checked_public_setting_of_the_daily_backup(self):
        from product_radar import operational_backup_selection as backup
        from product_radar.r10_cutover import CONFIG_FILES
        self.assertIn("config/research/product_map.json", CONFIG_FILES)
        empty = {table: {} for table in self.memory.snapshot()}
        name = "config/research/product_map.json"
        good = load_json(materials.SETTINGS, materials.SETTINGS_LIMIT)  # the frozen settings, matching the frozen catalogue
        atomic_json(self.root / "project" / name, good)
        selected = backup.select_essential_backup_files(empty, ({}, {}), root=self.root / "project")
        self.assertEqual([row["kind"] for row in selected["files"]], ["public_setting"])
        atomic_json(self.root / "project" / name, dict(good, extra=1))
        selected = backup.select_essential_backup_files(empty, ({}, {}), root=self.root / "project")
        self.assertEqual((selected["files"], [row["reason"] for row in selected["unsafe"]]),
                         ([], ["PRODUCT_MAP_SETTINGS_INVALID"]))


class MaterialTests(Case):
    def test_platform_categories_product_types_and_items_become_branch_inputs(self):
        self.platform(
            collections=[collection("silicone-cupcake-moulds", "Silicone Cupcake Baking Moulds"),
                         collection("best-selling-products", "Bestselling Products", ["bestseller"]),
                         collection("laptop-stands", "Laptop Stands")],
            items=[item("1", "Electric Egg Boiler 7 Egg", product_type="Egg Boiler"),
                   item("2", "Steam Egg Boiler Compact", product_type="Egg Boiler"),
                   item("3", "Egg Boiler with Tray", product_type="Egg Boiler"),
                   item("4", "Gold Plated Necklace Set", tags=["jewellery"]),
                   item("5", "Laptop Cooling Stand"),
                   item("6", "Garlic Crusher Press"),
                   item("7", "Electric Hand Blender", product_type="Appliance", roles=["new_arrival"])])
        result = self.advance()
        self.assertEqual(result["build"], {"state": "waiting", "reason": "PRODUCT_MAP_MODEL_WAITING",
                                           "branch_id": result["build"]["branch_id"]})
        staged = self.staged()
        self.assertEqual(staged, {"platform-collection:Silicone Cupcake Baking Moulds": "branch-hk-baking-and-dessert-making",
                                  "platform-collection:Egg Boiler": "branch-hk-small-kitchen-appliances",
                                  "platform-item:Electric Hand Blender": "branch-hk-small-kitchen-appliances"})
        # The list collection is not a product area; the egg boilers repeat their product type's
        # words: variants of that staged material until its AI decision is saved.
        examined = self.examined()
        outcomes = {name: row["outcome"] for name, row in examined.items()}
        self.assertEqual([outcomes["platform-item:deodap:item:" + n] for n in "123"], ["variant"] * 3)
        self.assertEqual({examined["platform-item:deodap:item:" + n]["of"] for n in "123"},
                         {examined["platform-collection:deodap:type:egg boiler"]["material_id"]})
        self.assertEqual(outcomes["platform-item:deodap:item:4"], "outside_categories")  # an excluded kind
        self.assertEqual(outcomes["platform-item:deodap:item:5"], "outside_categories")  # no client category
        self.assertEqual(outcomes["platform-item:deodap:item:6"], "known")  # an existing heading's words
        self.assertEqual(outcomes["platform-collection:deodap:collection:laptop-stands"], "outside_categories")
        self.assertEqual(result["staging"]["outcomes"]["staged"], 3)
        # The staged material is ordinary seed material: the AI sees the actual source line.
        material_id = next(row["material_id"] for name, row in self.examined().items() if name.endswith("item:7"))
        material = _load_material(self.memory, material_id)
        self.assertEqual((material["category"], material["basis"], material["source"]["url"]),
                         ("Home and Kitchen", "observed", "https://deodap.in/products/7"))
        self.assertEqual(_input_material(material)["context_excerpt"],
                         "DeoDap product: Electric Hand Blender (product type: Appliance); on its new arrival list")
        self.assertEqual(result["map"]["waiting_material"], 3)
        self.assertEqual(result["map"]["headings"], 224)

    def test_rising_queries_advertisers_ads_and_supplier_listings_become_branch_inputs(self):
        self.rising(["silicone cupcake baking mould", "egg boiler electric", "gold necklace"])
        self.wider_read("meta-advertisers", "meta-ads", [
            {"ad_id": "9001", "library_url": "https://www.facebook.com/ads/library/?id=9001",
             "presentation": {"body": "Wooden Montessori Puzzle Board for toddlers\nShop now and save!"}},
            {"ad_id": "9002", "library_url": "https://www.facebook.com/ads/library/?id=9002", "presentation": {"body": ""}}],
            "acq-meta")
        self.wider_read("supplier-listings", "indiamart", [
            {"semantic": {"title": "Kids Tricycle with Parent Handle", "product_url": "https://www.indiamart.com/proddetail/t-1.html",
                          "platform": "IndiaMART"},
             "facts": {"url": "https://www.indiamart.com/proddetail/t-1.html", "query": "kids toy"}}], "acq-im")
        result = self.advance()
        staged = self.staged()
        self.assertEqual(staged["trends-rising:silicone cupcake baking mould"], "branch-hk-baking-and-dessert-making")
        self.assertEqual(staged["meta-advertiser:Wooden Montessori Puzzle Board for toddlers"], "branch-bb-toys-and-learning-games")
        self.assertEqual(staged["supplier-listing:Kids Tricycle with Parent Handle"], "branch-bb-active-and-outdoor-play")
        self.assertNotIn("trends-rising:gold necklace", staged)
        self.assertEqual(self.examined()["trends-rising:gold necklace"]["outcome"], "outside_categories")
        # The kinds take turns: the first look-ats come from different kinds.
        self.assertEqual(result["staging"]["looked_at"], 5)
        signals = materials.load_signals(self.memory.root)
        self.assertEqual(len(signals), 3)  # two rising queries and the advertiser's ad (not the supplier listing)
        meta = next(row for row in self.examined().values() if row.get("kind") == "meta-advertiser")
        page = _load_material(self.memory, meta["material_id"])
        self.assertEqual(page["source"]["url"], "https://www.facebook.com/ads/library/?id=9001")
        self.assertEqual(_input_material(page)["context_excerpt"],
                         "Active Meta ad of an advertiser of a reviewed product: Wooden Montessori Puzzle Board for toddlers")

    def test_variants_are_staged_once_and_looked_at_once(self):
        self.platform(items=[item("1", "Silicone Cupcake Baking Mould"), item("2", "Cupcake Baking Mould Silicone Pack"),
                             item("3", "Silicone Cupcake Baking Mould with Lid")])
        first = self.advance()
        self.assertEqual(first["staging"]["outcomes"], {"staged": 1, "variant": 2})
        second = self.advance()
        self.assertEqual(second["staging"], {"looked_at": 0, "outcomes": {}, "failures": []})
        # A lost speed-aid file is rebuilt: staged material is never staged twice.
        (self.memory.root / "product_map" / "examined.json").write_text("{broken", encoding="utf-8")
        third = self.advance()
        self.assertEqual(third["staging"]["outcomes"].get("staged", 0), 1)
        self.assertEqual(len(list((self.memory.root / "catalogue_materials" / "items").glob("*/material.json"))), 1)

    def test_a_declined_example_reopens_its_variants_and_an_admitted_one_makes_them_known(self):
        self.platform(items=[item("1", "Silicone Cupcake Baking Mould"), item("2", "Cupcake Baking Mould Silicone Pack"),
                             item("3", "Silicone Cupcake Baking Mould with Lid")])
        self.advance()

        def none(row, entry):
            row.update(decision="no_useful_addition", label="", problem="", distinction="", basis="not_applicable",
                       evidence_ids=[], overlap_ids=[], phrases=[], action_purpose="none", reason="Covered already.")
        declined = self.advance(SeedRunner(none))
        self.assertEqual(declined["build"]["outcomes"], {"no_useful_addition": 1})
        # The next round looks again at the variants: one is staged, the other is its variant.
        again = self.advance()
        self.assertEqual(again["staging"]["outcomes"], {"staged": 1, "variant": 1})
        # A contract failure is not a judgement either: its variants reopen too.
        broken = self.advance(SeedRunner(lambda row, entry: row.update(evidence_ids=["invented"])))
        self.assertEqual(broken["build"]["outcomes"], {"rejected": 1})
        self.assertEqual(self.advance()["staging"]["outcomes"], {"staged": 1})
        accepted = self.advance(SeedRunner())
        self.assertEqual(accepted["build"]["outcomes"], {"accepted": 1})
        label = next(row["family"]["label"] for row in self.memory.snapshot()["catalogue_additions"].values()
                     if row["outcome"] == "accepted")
        self.platform(items=[item("1", "Silicone Cupcake Baking Mould"), item("2", "Cupcake Baking Mould Silicone Pack"),
                             item("3", "Silicone Cupcake Baking Mould with Lid"), item("4", label + " Heart Shape")])
        later = self.advance()
        self.assertEqual(later["staging"]["outcomes"], {"known": 1})  # the admitted heading's words

    def test_one_word_wording_is_never_staged_and_never_hides_anything(self):
        self.platform(collections=[collection("lights", "Lights"), collection("toys", "Toys"), collection("bags", "Bags")],
                      items=[item("1", "Decorative Wind Chime"), item("2", "Musical Toy Phone"), item("3", "Wooden Stacking Toy Rings")])
        result = self.advance()
        examined = self.examined()
        self.assertEqual({examined["platform-collection:deodap:collection:" + name]["outcome"] for name in ("lights", "toys", "bags")},
                         {"single_word"})
        # "Lights" and "Toys" hide nothing: these items are looked at on their own words.
        self.assertEqual(examined["platform-item:deodap:item:2"]["outcome"], "staged")
        self.assertEqual(examined["platform-item:deodap:item:1"]["outcome"], "no_sub_area")
        self.assertNotIn("variant", result["staging"]["outcomes"])
        self.assertEqual(materials.catalogue_sets({"concepts": {}}, {"families": [dict(self.base["families"][0], label="Toys",
                                                                                   aliases=["Lamp"], phrases=[])]}), set())

    def test_routing_stays_in_the_gates_category_unless_another_is_clearly_meant(self):
        profiles = materials.branch_profiles(self.base, materials.settings())
        from product_radar.wider_candidates import gate

        def routed(title):
            return materials.route(materials.word_set(title), profiles, gate(title))
        expected = {"Car Seat Cushion": "branch-to-car-interior-and-in-car-organisation",  # W4: decor ("cushion")
                    "Table Tennis Racket": "branch-to-sports-and-outdoor-games",  # W4: decor ("table")
                    "Piping Bag Nozzle Icing Set": "branch-hk-baking-and-dessert-making",  # W4: travel ("bag")
                    "Baby Car Seat": "branch-bb-baby-travel-and-seating",
                    "Mosquito Racket Rechargeable": "branch-hk-cleaning-and-household-maintenance",
                    "Digital Kitchen Weighing Scale": "branch-hk-preparation-tools",
                    "Body Weight Scale": "branch-to-fitness-yoga-and-home-workout",
                    "Reed Diffuser": "branch-hd-tabletop-decoration-and-fragrance",
                    "Bathroom Shelf Rack": "branch-hd-bathroom-organisation-and-accessories",
                    "Lunch Bag Insulated": "branch-hk-lunch-and-drinks-on-the-go"}
        self.assertEqual({title: routed(title) for title in expected}, expected)
        # Close calls are left for later, never guessed: no routing word, or no clear margin.
        for title in ("Toran Door Hanging", "Tennis Ball", "Hair Dryer"):
            self.assertIsNone(routed(title), title)
        # One routing word against nothing else still needs a hit, not shared vocabulary only.
        self.assertIsNone(materials.route(frozenset({"stainless"}), profiles))

    def test_one_generic_routing_word_never_stages_a_product_outside_the_gate(self):
        # No client category from the gate and only one routing word ("sleep", "led", "mould"): not staged.
        # A multi-word routing match ("egg boiler") or a gate category with its routing words still stages.
        self.platform(items=[item("1", "Melatonin Sleep Gummies"), item("2", "Smart LED TV 43 inch"),
                             item("3", "Silicone Cupcake Mould"), item("4", "Electric Egg Boiler"),
                             item("5", "Self Watering Plant Pots"), item("6", "Baby Feeding Spoons"),
                             item("7", "Car Cup Holders"), item("8", "Sleeping Bag Camping")])
        self.advance()
        outcomes = {name.rsplit(":", 1)[1]: (row["outcome"], row.get("branch_id")) for name, row in self.examined().items()}
        self.assertEqual({key: outcomes[key][0] for key in "123"}, dict.fromkeys("123", "outside_categories"))
        # The in-scope ones pass: staged, or already known to the map (a heading's words).
        self.assertEqual({key: outcomes[key][0] for key in "45678"},
                         {"4": "staged", "5": "staged", "6": "known", "7": "staged", "8": "known"})
        profiles = materials.branch_profiles(self.base, materials.settings())
        from product_radar.wider_candidates import gate
        placed = {title: materials.place(materials.word_set(title), profiles, gate(title)) for title in
                  ("Baby Feeding Spoons", "Sleeping Bag Camping", "Car Cup Holders")}
        self.assertEqual(placed, {"Baby Feeding Spoons": "branch-bb-feeding-and-nursing",
                                  "Sleeping Bag Camping": "branch-to-camping-sleep-and-shelter",
                                  "Car Cup Holders": "branch-to-car-interior-and-in-car-organisation"})
        # Within the gate's category, a close call between two sub-areas goes to the one sharing clearly more words.
        self.assertEqual(materials.place(materials.word_set("Self Watering Plant Pots"), profiles, "Home and Decor"),
                         "branch-hd-plant-care-and-seasonal-decoration")
        self.assertIsNone(materials.place(materials.word_set("Decorative Wind Chime"), profiles, "Home and Decor"))
        # Another category needs two or more of its routing words ("piping", "icing"), never one generic word.
        self.assertEqual(materials.place(materials.word_set("Piping Bag Nozzle Icing Set"), profiles, "Travel and Outdoor"),
                         "branch-hk-baking-and-dessert-making")
        self.assertEqual(materials.place(materials.word_set("Table Tennis Racket"), profiles, "Home and Decor"),
                         "branch-to-sports-and-outdoor-games")  # a multi-word match
        self.assertIsNone(materials.place(materials.word_set("Stainless Bowl Kit"), profiles, "Baby Products"))

    def test_pets_and_personal_fragrance_are_outside_the_client_categories(self):
        self.platform(items=[item(str(n), title) for n, title in enumerate(
            ["Dog Bowl Stainless Steel", "Cat Litter Box", "Aquarium Light", "Pet Grooming Brush",
             "Aroma Fragrance Diffuser", "Reed Diffuser Sticks"])])
        self.advance()
        outcomes = {name: row["outcome"] for name, row in self.examined().items()}
        self.assertEqual([outcomes["platform-item:deodap:item:" + str(n)] for n in range(5)], ["outside_categories"] * 5)
        self.assertEqual(outcomes["platform-item:deodap:item:5"], "staged")  # home fragrance stays home decor
        from product_radar.dropship_platforms import settings as platform_settings
        from product_radar.platform_roles import item_category
        self.assertIsNone(item_category({"title": "Dog Feeding Bowl Kitchen Steel"}, platform_settings()["discovery"]))

    def test_unrouted_candidates_are_looked_at_again_once_the_map_has_grown(self):
        self.platform(items=[item("1", "Decorative Wind Chime")])  # home decor by the gate, no routing word
        self.advance()
        self.assertEqual(self.examined()["platform-item:deodap:item:1"], {"outcome": "no_sub_area", "headings": 224})
        self.assertEqual(self.advance()["staging"]["looked_at"], 0)
        grown = deepcopy(self.base)
        for n in range(25):
            family = deepcopy(grown["families"][0])
            family.update(id="grown-" + str(n), label="Grown heading " + str(n), aliases=[])
            grown["families"].append(family)
        self.base = grown
        self.assertEqual(self.advance()["staging"]["looked_at"], 1)

    def test_an_unreadable_examined_file_makes_the_round_wait_without_overwriting_it(self):
        self.platform(items=[item("1", "Electric Egg Boiler")])
        self.advance()
        path = self.memory.root / "product_map" / "examined.json"
        before = path.read_bytes()
        with patch.object(materials, "load_json", side_effect=SafetyError("SAVED_FILE_UNAVAILABLE")):
            self.assertIsNone(materials.load_examined(self.memory, "x"))
        real = materials.load_examined
        with patch.object(materials, "load_examined", return_value=None), \
                patch.object(product_map, "load_examined", return_value=None):
            result = self.advance(SeedRunner())
            hint = product_map.map_lane_hint(self.memory, self.memory.snapshot(), "programme", self.base, as_of=AT,
                                             runner_available=True)
        self.assertEqual(result["staging"]["waiting"], "PRODUCT_MAP_EXAMINED_UNREADABLE")
        self.assertEqual(result["build"], {"state": "waiting", "reason": "PRODUCT_MAP_EXAMINED_UNREADABLE"})
        self.assertFalse(hint["ready"])
        self.assertEqual(path.read_bytes(), before)
        self.assertEqual(real(self.memory, product_map.config_hash(materials.settings(), self.base))["platform-item:deodap:item:1"]["outcome"],
                         "staged")

    def test_passed_over_candidates_do_not_use_the_rounds_look_limit(self):
        config = deepcopy(materials.settings())
        config["build"].update(examine_per_round=2, unbuilt_per_branch=1)
        self.platform(items=[item("1", "Electric Egg Boiler"), item("2", "Electric Hand Blender"),
                             item("3", "Electric Kettle Steel Jug"), item("4", "Cupcake Baking Mould"),
                             item("5", "Piping Bag Nozzle Icing Set")])
        with patch.object(product_map, "settings", return_value=config):
            result = self.advance()
        # The hand blender is passed over (busy sub-area) and does not use a look: the cupcake mould gets it.
        self.assertEqual(result["staging"]["looked_at"], 2)
        self.assertEqual(result["staging"]["outcomes"].get("sub_area_backlog"), 1)
        self.assertEqual(set(materials.staged_materials(self.examined(config)).values()),
                         {"branch-hk-small-kitchen-appliances", "branch-hk-baking-and-dessert-making"})

    def test_a_busy_sub_area_takes_no_more_material_but_a_full_one_still_does(self):
        config = deepcopy(materials.settings())
        config["target"]["headings_per_sub_area"] = 8  # the preparation tools hold 8 headings, small appliances none
        config["build"]["unbuilt_per_branch"] = 1
        self.platform(items=[item("1", "Bamboo Chopping Board Stand"), item("2", "Electric Egg Boiler"),
                             item("3", "Hand Blender Electric Stick")])
        with patch.object(product_map, "settings", return_value=config):
            result = self.advance()
        outcomes = {name: row["outcome"] for name, row in self.examined(config).items()}
        # The target is a soft cap: a full sub-area still takes material (built after the others).
        self.assertEqual(outcomes["platform-item:deodap:item:1"], "staged")
        self.assertEqual(outcomes["platform-item:deodap:item:2"], "staged")
        self.assertNotIn("platform-item:deodap:item:3", outcomes)  # looked at again once the waiting one is built
        self.assertEqual(result["staging"]["outcomes"]["sub_area_backlog"], 1)

    def test_staging_failure_is_that_candidates_own(self):
        self.platform(items=[item("1", "Electric Egg Boiler"), item("2", "Silicone Cupcake Baking Mould")])
        real = materials.stage_material

        def flaky(memory, branch, row):
            if row["key"].endswith(":1"):
                raise OSError("disk")
            return real(memory, branch, row)
        with patch.object(materials, "stage_material", side_effect=flaky):
            result = self.advance()
        self.assertEqual(result["staging"]["failures"], [{"key": "platform-item:deodap:item:1",
                                                         "reason": "PRODUCT_MAP_STAGING_LOCAL_FAILURE"}])
        self.assertEqual(result["staging"]["outcomes"], {"staged": 1})
        self.assertNotIn("platform-item:deodap:item:1", self.examined())

    def test_unreadable_speed_aid_files_are_rebuilt(self):
        self.platform(items=[item("1", "Electric Egg Boiler")])
        folder = self.memory.root / "product_map"
        folder.mkdir(parents=True)
        for name in ("rows-deodap.json", "rows-meta-advertisers.json", "signals.json"):
            (folder / name).write_text("{broken", encoding="utf-8")
        self.assertEqual(materials.load_signals(self.memory.root), [])
        self.assertEqual(self.advance()["staging"]["outcomes"], {"staged": 1})
        self.assertEqual(load_json(folder / "rows-deodap.json", 1 << 20)["rows"][0]["label"], "Electric Egg Boiler")

    def test_edges_of_saved_material_are_skipped_not_guessed(self):
        # A platform never read yet, a rising query too long to be a search, an
        # unreadable read file, a long ad clipped at a word, a no-category item.
        value = _empty("baapstore")
        save_catalogue(self.memory.root, "baapstore", value)
        self.rising(["x" * 130, "silicone cupcake baking mould"])
        long_body = "Silicone cupcake baking mould " + "with extra words " * 20
        self.wider_read("meta-advertisers", "meta-ads", [{"ad_id": "1", "library_url": "https://www.facebook.com/ads/library/?id=1",
                                                         "presentation": {"body": long_body}}], "acq-a")
        self.wider_read("meta-advertisers", "meta-ads", [], "acq-b")
        (self.memory.root / "wider_discovery" / "acq-b" / "nomination.json").write_text("{", encoding="utf-8")
        self.wider_read("supplier-listings", "tradeindia", [
            {"semantic": {"title": "Mosquito Racket Rechargeable"}, "facts": {"url": "https://www.tradeindia.com/products/m-1.html",
                                                                            "query": "kitchen utensil"}},
            {"semantic": {"title": "Kitchen Tools"}, "facts": {"url": "https://www.tradeindia.com/products/d-1.html",
                                                                              "query": "home decor item"}}], "acq-c")
        rows = materials.collect_rows(self.memory, self.memory.snapshot(), AT, materials.settings())
        labels = {row["label"] for row in rows}
        self.assertNotIn("x" * 130, labels)
        clipped = next(row["label"] for row in rows if row["kind"] == "meta-advertiser")
        self.assertTrue(len(clipped) <= 160 and long_body.startswith(clipped) and not clipped.endswith(" "))
        self.assertEqual(materials._clip("x" * 200), "x" * 160)
        with patch.object(materials, "atomic_json", side_effect=OSError("disk")):
            again = materials.collect_rows(self.memory, self.memory.snapshot(), AT, materials.settings())
        self.assertEqual(again, rows)  # speed-aid files that cannot be written are only rebuilt next time
        result = self.advance()
        outcomes = {name: row["outcome"] for name, row in self.examined().items()}
        self.assertEqual(outcomes["supplier-listing:tradeindia:https://www.tradeindia.com/products/d-1.html"], "single_word")
        self.assertEqual(result["staging"]["failures"], [])
        # A changed configuration keeps staged material and looks at the rest again.
        kept = materials.load_examined(self.memory, "another-configuration")
        self.assertTrue(kept and {row["outcome"] for row in kept.values()} <= {"staged", "variant"})
        (self.memory.root / "product_map" / "examined.json").write_text("[]", encoding="utf-8")
        self.assertEqual(materials.load_examined(self.memory, "x"), {})
        with patch.object(materials, "atomic_json", side_effect=OSError("disk")):
            materials.save_examined(self.memory, "x", {})
            self.assertEqual(materials.write_signals(self.memory, rows, AT, materials.settings()), 2)

    def test_the_round_stops_looking_at_its_limit(self):
        config = deepcopy(materials.settings())
        config["build"]["examine_per_round"] = 2
        self.platform(items=[item(str(n), "Gadget " + str(n)) for n in range(5)])
        with patch.object(product_map, "settings", return_value=config):
            self.assertEqual(self.advance()["staging"]["looked_at"], 2)
            self.assertEqual(self.advance()["staging"]["looked_at"], 2)

    def test_a_map_page_cannot_be_changed_or_name_another_origin(self):
        self.platform(items=[item("1", "Electric Egg Boiler")])
        self.advance()
        material_id = next(iter(materials.staged_materials(self.examined())))
        material = _load_material(self.memory, material_id)
        page = load_json(material["page_reference"], 1 << 20)
        for origin in ({"kind": "product_map", "source": "invented", "key": "x"}, {"kind": "product_map", "source": "platform-item"}):
            with self.subTest(origin=origin):
                receipt = dict(page["receipt"], origin=origin)
                atomic_json(material["page_reference"], dict(page, receipt=receipt, hash=digest(receipt)))
                with self.assertRaisesRegex(SafetyError, "CATALOGUE_SEED_MATERIAL_CHANGED"):
                    _load_material(self.memory, material_id)
        with self.assertRaisesRegex(SafetyError, "CATALOGUE_SEED_EXCERPT_NOT_SUPPLIED"):
            materials.stage_material(self.memory, self.base["branches"][1], dict(
                materials._row("platform-item", "k", "Invented label", "https://deodap.in/x", "t", AT, "Other text", None)))


class BuildTests(Case):
    def test_admitted_headings_are_in_the_map_at_once_and_served_first(self):
        from product_radar.heading_order import heading_facts, heading_key
        from product_radar.service import CATEGORIES
        self.platform(items=[item("1", "Electric Egg Boiler"), item("2", "Silicone Cupcake Baking Mould")])
        runner = SeedRunner()
        result = self.advance(runner)
        self.assertEqual(result["build"]["state"], "built")
        self.assertEqual(len(runner.calls), 1)
        built = result["build"]["branch_id"]
        self.assertEqual(len(runner.calls[0]["items"]), 1)  # one sub-area per call
        view = effective_catalogue(self.base, self.memory.snapshot())
        validate_catalogue(view)
        added = [row for row in view["families"] if row["parent_id"] == built and row["author"] == "bounded-role-c-seed"]
        self.assertEqual(len(added), 1)
        self.assertIn("dropship", {phrase["source"] for phrase in added[0]["phrases"]})
        self.assertEqual(result["map"]["headings"], 225)
        # Never served: the next wave serves it ahead of every served heading.
        facts = heading_facts(self.memory.snapshot(), view, root=self.memory.root)
        facts["served"] = {row["id"]: AT for row in self.base["families"]}
        from product_radar.heading_order import heading_turns
        facts["turns"] = heading_turns(view, facts)
        keys = sorted(view["families"], key=lambda row: heading_key(row, facts, list(CATEGORIES)))
        self.assertEqual(keys[0]["id"], added[0]["id"])
        # The next round builds the other sub-area; nothing is sent twice.
        second = self.advance(runner, at=LATER)
        self.assertNotEqual(second["build"]["branch_id"], built)
        self.assertEqual(len(runner.calls), 2)
        third = self.advance(runner, at=LATER)
        self.assertEqual(third["build"], {"state": "nothing_waiting"})
        self.assertEqual(len(runner.calls), 2)

    def test_admitted_map_material_and_its_page_are_in_the_daily_backup(self):
        from product_radar import operational_backup_selection as backup
        project = self.root / "project"
        self.memory = ResearchMemory(project / "data/research/captured", Journal(self.root / "finance-2"),
                                     evidence_type="synthetic")
        self.rising(["silicone cupcake baking mould"])
        self.advance(SeedRunner())
        state = self.memory.snapshot()
        material_id = next(row["material_id"] for row in state["catalogue_additions"].values())
        material = _load_material(self.memory, material_id)
        selected = backup.select_essential_backup_files(state, ({}, {}), root=project)
        paths = {Path(row["path"]) for row in selected["files"]}
        self.assertIn(self.memory.root / "catalogue_materials/items" / material_id / "material.json", paths)
        self.assertIn(Path(material["page_reference"]), paths)
        self.assertIn(self.memory.root / "direct_trends/trends-1/rising_queries.json", paths)  # the original saved list
        self.assertEqual(selected["unsafe"], [])

    def test_a_same_heading_answer_merges_as_an_alias(self):
        self.platform(items=[item("1", "Electric Egg Boiler")])

        def same(row, entry):
            row.update(decision="same_family", match_id=entry["candidates"][0]["id"])
        runner = SeedRunner(same)
        result = self.advance(runner)
        self.assertEqual(result["build"]["outcomes"], {"merged": 1})
        self.assertEqual(result["map"]["headings"], 224)
        view = effective_catalogue(self.base, self.memory.snapshot())
        self.assertTrue(any("Electric Egg Boiler" in row["aliases"] for row in view["families"]))

    def test_build_waits_for_the_rounds_ai_allowance(self):
        self.platform(items=[item("1", "Electric Egg Boiler")])
        policy = dict(deepcopy(DEFAULT_POLICY), model_executions=2, report_slots=0, review_slots=0)
        turn = open_work_turn(self.memory, "programme", at=AT, policy=policy)
        runner = SeedRunner()
        from product_radar.research_ai import Jobs
        from test_research_flow import FakeRunner
        with bind_work_turn(self.memory, turn["id"]):
            for n in range(2):
                Jobs(self.memory, FakeRunner()).run("A", {"items": [{"id": "spent-" + str(n), "source_text": {"title": "Unclear offer"}}]})
            before = deepcopy(self.memory.snapshot()["catalogue_additions"])
            result = self.advance(runner)
        self.assertEqual((result["build"]["state"], result["build"]["reason"]), ("waiting", "WORK_TURN_MODEL_LIMIT"))
        self.assertEqual(runner.calls, [])
        self.assertEqual(self.memory.snapshot()["catalogue_additions"], before)
        lane = product_map.lane_record(self.memory.snapshot(), "programme")
        self.assertEqual(lane["branch_waits"], {})  # an allowance wait ends with the round

    def test_a_failed_call_waits_and_an_unreadable_material_is_set_aside(self):
        self.platform(items=[item("1", "Electric Egg Boiler"), item("2", "Hand Blender Electric Stick")])
        config = deepcopy(materials.settings())
        config["build"]["unbuilt_per_branch"] = 5
        with patch.object(product_map, "settings", return_value=config):
            self.advance()
            failing = SeedRunner(lambda row, entry: row.update(category="Baby Products"))
            first = self.advance(failing)
        self.assertEqual(first["build"]["state"], "built")  # a contract rejection is a finished answer
        self.assertEqual(first["build"]["outcomes"], {"rejected": 2})
        # A runner failure waits retry_minutes for that sub-area.
        self.platform(items=[item("3", "Electric Juicer Mixer Grinder")])

        class Broken(SeedRunner):
            def __call__(self, role, payload, folder):
                raise SafetyError("AI_RUNNER_UNAVAILABLE")
        with patch.object(product_map, "settings", return_value=config):
            second = self.advance(Broken(), at=LATER)
            self.assertEqual(second["build"]["state"], "waiting")
            lane = product_map.lane_record(self.memory.snapshot(), "programme")
            self.assertEqual(set(lane["branch_waits"]), {second["build"]["branch_id"]})
            self.assertEqual(self.advance(SeedRunner(), at=LATER)["build"], {"state": "nothing_waiting"})
        # A material whose saved page changed is set aside; its sub-area moves on.
        self.platform(items=[item("4", "Silicone Cupcake Baking Mould"), item("5", "Piping Bag Nozzle Icing Set")])
        with patch.object(product_map, "settings", return_value=config):
            self.advance(None, at=LATER)
            staged = [row for row in self.examined(config).values() if row.get("outcome") == "staged"
                      and row["branch_id"] == "branch-hk-baking-and-dessert-making"]
            broken = _load_material(self.memory, staged[0]["material_id"])
            atomic_json(Path(broken["page_reference"]), {"broken": True})
            third = self.advance(SeedRunner(), at=LATER)
            self.assertEqual(third["build"]["branch_id"], "branch-hk-baking-and-dessert-making")
            self.assertEqual(third["build"]["outcomes"], {"accepted": 1})
            lane = product_map.lane_record(self.memory.snapshot(), "programme")
            self.assertEqual(list(lane["set_aside"]), [staged[0]["material_id"]])
            self.assertIsNone(product_map.build_choice(self.memory.snapshot(), self.base, self.examined(config),
                                                       lane, LATER, config))

    def test_a_local_build_failure_holds_its_sub_area(self):
        self.platform(items=[item("1", "Electric Egg Boiler")])
        with patch("product_radar.catalogue_seed.build_catalogue_branch", side_effect=OSError("disk")):
            result = self.advance(SeedRunner())
        self.assertEqual(result["build"]["failures"], [{"material_id": None, "reason": "PRODUCT_MAP_BUILD_LOCAL_FAILURE"}])
        lane = product_map.lane_record(self.memory.snapshot(), "programme")
        self.assertEqual(lane["branch_waits"]["branch-hk-small-kitchen-appliances"]["reason"], "PRODUCT_MAP_BUILD_LOCAL_FAILURE")
        hint = product_map.map_lane_hint(self.memory, self.memory.snapshot(), "programme", self.base, as_of=AT,
                                         runner_available=True)
        self.assertEqual(hint["next_retry_at"], "2026-10-15T06:30:00+00:00")

    def test_the_build_call_goes_before_renewal_when_one_ai_call_is_left(self):
        self.platform(items=[item("1", "Electric Egg Boiler")])
        self.concept("concept-1", "Clip on pan lid holder")
        policy = dict(deepcopy(DEFAULT_POLICY), model_executions=2, report_slots=0, review_slots=0)
        turn = open_work_turn(self.memory, "programme", at=AT, policy=policy)
        from product_radar.research_ai import Jobs
        from test_research_flow import FakeRunner

        class Both(SeedRunner):
            def __call__(self, role, payload, folder):
                return (ExpansionRunner() if "description" in payload["items"][0] else SeedRunner())(role, payload, folder)
        with bind_work_turn(self.memory, turn["id"]):
            Jobs(self.memory, FakeRunner()).run("A", {"items": [{"id": "spent", "source_text": {"title": "Unclear offer"}}]})
            result = self.advance(Both())
        self.assertEqual(result["build"]["outcomes"], {"accepted": 1})
        self.assertEqual((result["renewal"]["state"], result["renewal"]["reason"]), ("waiting", "WORK_TURN_MODEL_LIMIT"))

    def test_fuller_sub_areas_are_built_before_one_or_two_waiting_examples(self):
        config = deepcopy(materials.settings())
        config["build"]["min_batch"] = 2
        self.platform(items=[item("1", "Silicone Cupcake Baking Mould"), item("2", "Electric Egg Boiler"),
                             item("3", "Electric Hand Blender")])
        with patch.object(product_map, "settings", return_value=config):
            self.advance()
            first = self.advance(SeedRunner())
            second = self.advance(SeedRunner())
        # The baking sub-area has fewer... no: its single example waits until no fuller sub-area is ready.
        self.assertEqual((first["build"]["branch_id"], first["build"]["materials"]), ("branch-hk-small-kitchen-appliances", 2))
        self.assertEqual((second["build"]["branch_id"], second["build"]["materials"]), ("branch-hk-baking-and-dessert-making", 1))

    def test_the_target_is_a_soft_cap_and_below_target_sub_areas_build_first(self):
        config = deepcopy(materials.settings())
        config["target"]["headings_per_sub_area"] = 8  # the preparation tools hold 8 headings: at the target
        config["build"]["min_batch"] = 2
        self.platform(items=[item("1", "Bamboo Chopping Board Stand"), item("2", "Vegetable Spiral Slicer Cutter"),
                             item("3", "Electric Egg Boiler")])
        with patch.object(product_map, "settings", return_value=config):
            self.advance()
            self.assertEqual(set(materials.staged_materials(self.examined(config)).values()),
                             {"branch-hk-preparation-tools", "branch-hk-small-kitchen-appliances"})
            # A below-target sub-area on a retry wait holds nobody up.
            lane = product_map.lane_record(self.memory.snapshot(), "programme")
            lane["branch_waits"]["branch-hk-small-kitchen-appliances"] = {"reason": "AI_RUNNER_UNAVAILABLE",
                                                                          "next_retry_at": LATER}
            held = product_map.build_choice(self.memory.snapshot(), self.base, self.examined(config), lane, AT, config)
            self.assertEqual(held[0], "branch-hk-preparation-tools")
            first = self.advance(SeedRunner())
            second = self.advance(SeedRunner())
            third = self.advance(SeedRunner())
        # The single example below the target goes before two examples (a fuller batch) at the target.
        self.assertEqual((first["build"]["branch_id"], first["build"]["materials"]), ("branch-hk-small-kitchen-appliances", 1))
        # Then the full sub-area still grows: nothing below the target waits.
        self.assertEqual((second["build"]["branch_id"], second["build"]["materials"]), ("branch-hk-preparation-tools", 2))
        self.assertEqual(third["build"], {"state": "nothing_waiting"})
        counts = product_map.heading_counts(self.base, self.memory.snapshot())
        self.assertEqual(counts["branch-hk-preparation-tools"], 10)
        self.assertEqual(third["map"]["full_sub_areas"], 28)  # still reported (28 sub-areas hold 8 or more)

    def test_the_lookup_copy_is_written_once_per_map_round(self):
        self.platform(items=[item("1", "Electric Egg Boiler"), item("2", "Silicone Cupcake Baking Mould")])
        self.concept("concept-1", "Clip on pan lid holder")
        self.advance()

        class Both(SeedRunner):
            def __call__(self, role, payload, folder):
                return (ExpansionRunner() if "description" in payload["items"][0] else SeedRunner())(role, payload, folder)
        with patch.object(self.memory, "_write_index", wraps=self.memory._write_index) as written:
            result = self.advance(Both())
        self.assertEqual((result["build"]["outcomes"], result["renewal"]["outcomes"]), ({"accepted": 1}, {"accepted": 1}))
        self.assertEqual(written.call_count, 1)

    def test_lane_record_must_be_its_own_shape(self):
        self.memory.put("batch_runs", product_map._lane_id("programme"), {"kind": "other"})
        with self.assertRaisesRegex(SafetyError, "PRODUCT_MAP_LANE_INVALID"):
            product_map.lane_record(self.memory.snapshot(), "programme")


class RenewalTests(Case):
    def test_new_types_renew_the_map_once_five_at_most(self):
        for index in range(7):
            self.concept("concept-" + str(index), "Clip on pan lid holder " + str(index))
        self.memory.put("concepts", "concept-excluded", {"status": "excluded", "description": {"label": "x"}})
        runner = ExpansionRunner()
        first = self.advance(runner)
        self.assertEqual(first["renewal"]["state"], "renewed")
        self.assertEqual(len(runner.calls[0]["items"]), 5)
        self.assertEqual(first["renewal"]["outcomes"], {"accepted": 5})
        second = self.advance(runner)
        self.assertEqual(second["renewal"]["concept_ids"], ["concept-5", "concept-6"])
        third = self.advance(runner)
        self.assertEqual(third["renewal"], {"state": "nothing_new"})
        self.assertEqual(len(runner.calls), 2)
        view = effective_catalogue(self.base, self.memory.snapshot())
        self.assertEqual(sum(row["author"] == "bounded-role-c" for row in view["families"]), 7)

    def test_renewal_waits_without_the_ai_and_after_a_failure(self):
        self.concept("concept-1", "Clip on pan lid holder")
        self.assertEqual(self.advance(None)["renewal"]["reason"], "PRODUCT_MAP_MODEL_WAITING")

        class Broken(ExpansionRunner):
            def __call__(self, role, payload, folder):
                raise SafetyError("AI_RUNNER_UNAVAILABLE")
        failed = self.advance(Broken())
        self.assertEqual((failed["renewal"]["state"], failed["renewal"]["reason"]), ("waiting", "AI_RUNNER_UNAVAILABLE"))
        held = self.advance(ExpansionRunner())
        self.assertEqual((held["renewal"]["state"], held["renewal"]["reason"]), ("waiting", "AI_RUNNER_UNAVAILABLE"))
        # After retry_minutes the saved original is reconciled, never sent again blind.
        retried = self.advance(ExpansionRunner(), at=LATER)
        self.assertEqual((retried["renewal"]["state"], retried["renewal"]["reason"]),
                         ("waiting", "AI_JOB_PENDING_RECONCILIATION"))
        with patch("product_radar.catalogue_expansion.run_expansion", side_effect=OSError("disk")):
            self.concept("concept-2", "Clip on spoon rest")
            local = self.advance(ExpansionRunner(), at="2026-10-15T12:00:00+00:00")
        self.assertEqual(local["renewal"]["reason"], "PRODUCT_MAP_RENEWAL_LOCAL_FAILURE")


class LaneTests(Case):
    def test_wake_hint_reports_ai_work_only_with_the_ai(self):
        self.platform(items=[item("1", "Electric Egg Boiler")])
        self.advance()
        state = self.memory.snapshot()
        ready = product_map.map_lane_hint(self.memory, state, "programme", self.base, as_of=AT, runner_available=True)
        self.assertTrue(ready["ready"])
        self.assertEqual(ready["signal"]["build"][0], "branch-hk-small-kitchen-appliances")
        self.assertFalse(product_map.map_lane_hint(self.memory, state, "programme", self.base, as_of=AT,
                                                   runner_available=False)["ready"])
        self.advance(SeedRunner())
        done = product_map.map_lane_hint(self.memory, self.memory.snapshot(), "programme", self.base, as_of=AT,
                                         runner_available=True)
        self.assertEqual((done["ready"], done["next_retry_at"]), (False, None))

    def test_programme_readiness_includes_the_map_hint(self):
        import product_radar.programme as programme
        self.platform(items=[item("1", "Electric Egg Boiler")])
        self.advance()
        step = discovery.create_catalogue_discovery_step(self.base, shared_marketplace=True, renewable_waves=True,
                                                         map_step=product_map.map_step)
        record = {"programme_id": "programme"}
        found = programme._discovery_turn_readiness(self.memory, self.memory.snapshot(), record, {}, AT,
                                                    discovery_step=step, runner_available=True)
        self.assertTrue(found["local_ready"])
        self.assertEqual(found["signal"][-1][0], "product_map")
        with patch.object(product_map, "map_lane_hint", side_effect=SafetyError("PRODUCT_MAP_SETTINGS_INVALID")):
            failed = programme._discovery_turn_readiness(self.memory, self.memory.snapshot(), record, {}, AT,
                                                         discovery_step=step, runner_available=True)
        self.assertEqual(failed["signal"][-1], ["product_map", "PRODUCT_MAP_SETTINGS_INVALID"])
        with patch.object(product_map, "map_lane_hint", side_effect=SafetyError("RESEARCH_EVENTS_INVALID")):
            with self.assertRaisesRegex(SafetyError, "RESEARCH_EVENTS_INVALID"):
                programme._discovery_turn_readiness(self.memory, self.memory.snapshot(), record, {}, AT,
                                                    discovery_step=step, runner_available=True)

    def step(self, mapping):
        return discovery.create_catalogue_discovery_step(self.base, shared_marketplace=True, renewable_waves=True,
                                                         map_step=mapping)

    def test_the_map_goes_first_only_in_its_rounds_and_its_failure_is_its_own(self):
        order = []
        searches = lambda *_a, **_o: order.append("headings") or {"state": "checked", "new_concept_ids": ["a"]}

        def mapping(memory, record, context):
            order.append("map")
            self.assertEqual(context["discovery_catalogue"]["revision"], self.base["revision"])
            return {"state": "checked", "new_concept_ids": []}
        step = self.step(mapping)
        self.assertTrue(step.discovery_product_map)
        with patch.object(discovery, "advance_discovery_waves", side_effect=searches):
            for first, expected in ((False, ["headings", "map"]), (True, ["map", "headings"])):
                order.clear()
                result = step(self.memory, {"programme_id": "p"}, {"at": AT, "handlers": {}, "map_first": first})
                self.assertEqual(order, expected)
                self.assertEqual((result["map"]["state"], result["new_concept_ids"]), ("checked", ["a"]))
            for failure, reason in ((SafetyError("PRODUCT_MAP_SETTINGS_INVALID"), "PRODUCT_MAP_SETTINGS_INVALID"),
                                    (OSError("disk"), "PRODUCT_MAP_LOCAL_FAILURE"), (None, "PRODUCT_MAP_RESULT_INVALID")):
                broken = self.step(lambda *_a, error=failure: (_ for _ in ()).throw(error) if error else {"state": 1})
                self.assertEqual(broken(self.memory, {"programme_id": "p"}, {"at": AT, "handlers": {}})["map"]["reason"], reason)
            corrupt = self.step(lambda *_a: (_ for _ in ()).throw(SafetyError("RESEARCH_EVENTS_INVALID")))
            with self.assertRaisesRegex(SafetyError, "RESEARCH_EVENTS_INVALID"):
                corrupt(self.memory, {"programme_id": "p"}, {"at": AT, "handlers": {}})
        ran = []
        step = self.step(lambda *_a: ran.append("map") or {"state": "checked"})
        with patch.object(discovery, "advance_discovery_waves", side_effect=SafetyError("DISCOVERY_SLICE_INVALID")):
            for first in (False, True):
                ran.clear()
                with self.assertRaisesRegex(SafetyError, "DISCOVERY_SLICE_INVALID"):
                    step(self.memory, {"programme_id": "p"}, {"at": AT, "handlers": {}, "map_first": first})
                self.assertEqual(ran, ["map"])

    def test_the_map_lane_needs_the_renewable_waves(self):
        for options in ({"shared_marketplace": True, "renewable_waves": False, "map_step": product_map.map_step},
                        {"shared_marketplace": True, "renewable_waves": True, "map_step": "not callable"}):
            with self.assertRaisesRegex(SafetyError, "DISCOVERY_WAVE_OPTION_INVALID"):
                discovery.create_catalogue_discovery_step(**options)

    def test_map_step_reports_its_progress_and_reads_the_discovery_catalogue(self):
        with patch.object(product_map, "advance_product_map", return_value={"state": "checked"}) as advance:
            product_map.map_step(self.memory, {"programme_id": "p"}, {"at": AT, "discovery_catalogue": {"x": 1}})
        self.assertEqual(advance.call_args.kwargs, {"programme_id": "p", "catalogue": {"x": 1}})
        from product_radar.operations import _DETAILS
        self.assertIn("product_map", _DETAILS)


DAY = "2026-10-15"


class TopupCase(Case):
    """The saved top-up settings: the saved map's 224 headings were never
    searched, fewer than the threshold of 300."""

    def setUp(self):
        super().setUp()
        self.config = self.use()

    def use(self, **topup):
        config = deepcopy(load_json(materials.SETTINGS, materials.SETTINGS_LIMIT))
        config["topup"].update(topup)
        for module in (materials, product_map):
            patcher = patch.object(module, "settings", return_value=config)
            patcher.start()
            self.addCleanup(patcher.stop)
        self.config = config
        return config

    def lane(self):
        return product_map.lane_record(self.memory.snapshot(), "programme")

    def hint(self, at=AT, runner=True):
        return product_map.map_lane_hint(self.memory, self.memory.snapshot(), "programme", self.base, as_of=at,
                                         runner_available=runner)

    def serve(self, family_ids, at):
        """A discovery wave planned these headings at ``at``."""
        from product_radar.catalogue_discovery import WAVE_VERSION
        record_id = "wave-" + digest([family_ids, at])[:16]
        self.memory.put("batch_runs", record_id, {"kind": WAVE_VERSION, "id": record_id, "prepared_at": at,
            "planned_actions": [{"family_id": family_id, "questions": []} for family_id in family_ids], "actions": {}})


class TopupTests(TopupCase):
    """The AI top-up: new headings when the map runs low."""

    def test_a_low_map_asks_the_ai_for_new_headings_for_its_emptiest_sub_area(self):
        from product_radar import map_topup
        from product_radar.heading_order import heading_facts, heading_key, heading_turns
        from product_radar.service import CATEGORIES
        runner = TopupRunner(lambda entry: [heading("Foldable widget stands"), heading("Removable wall decals"),
                                            heading("Pet feeding bowl stands"),
                                            heading("Foldable widget stands", meta="widget stand rack")])
        result = self.advance(runner)
        self.assertEqual(result["build"], {"state": "nothing_waiting"})
        self.assertEqual(len(runner.calls), 1)
        payload = runner.calls[0]
        self.assertEqual((payload["mode"], payload["topup_version"]), (map_topup.MODE, map_topup.VERSION))
        entry = payload["items"][0]
        # Fewest never-searched headings, then fewest headings; Home and Decor first (the categories in turn).
        self.assertEqual(entry["category"], "Home and Decor")
        self.assertEqual(entry["sub_area_headings"], [])  # an empty sub-area
        labels = sorted((row["label"] for row in self.base["families"] if row["category"] == "Home and Decor"),
                        key=lambda text: text.casefold())
        self.assertEqual((entry["category_headings"], entry["category_headings_truncated"]), (labels, False))
        self.assertEqual(entry["headings_wanted"], 12)
        self.assertEqual(result["topup"]["state"], "added")
        # One new heading; an existing label and a repeated label merge; a pet product is refused.
        self.assertEqual(result["topup"]["outcomes"], {"accepted": 1, "merged": 2, "rejected": 1})
        state = self.memory.snapshot()
        rejected = next(row for row in state["catalogue_additions"].values() if row["outcome"] == "rejected")
        self.assertEqual(rejected["reason"], "CATALOGUE_TOPUP_EXCLUDED_KIND")
        view = effective_catalogue(self.base, state)
        added = [row for row in view["families"] if row["author"] == map_topup.AUTHOR]
        self.assertEqual(len(added), 1)
        self.assertEqual(added[0]["parent_id"], entry["branch"]["id"])
        self.assertTrue(added[0]["definition"].startswith(
            "Products whose primary purpose is to keep foldable widget stands ready for daily use. Functional"))
        self.assertEqual([(row["source"], row["purpose"]) for row in added[0]["phrases"]],
                         [("meta", "discovery"), ("marketplace", "validation"), ("trends", "validation")])
        self.assertEqual((added[0]["basis"], added[0]["sources"], added[0]["disposition"]),
                         ("plausible_extension", [], "usable"))
        self.assertIn("An AI top-up research direction", added[0]["rationale"])
        self.assertEqual(result["map"]["headings"], 225)
        self.assertEqual(result["map"]["unsearched"], 225)
        lane = self.lane()
        self.assertEqual((lane["topup_day"], lane["topup_last_category"], lane["topup_wait"]),
                         ({"date": DAY, "calls": 1}, "Home and Decor", None))
        # Served first by the next discovery wave (never served).
        facts = heading_facts(state, view, root=self.memory.root)
        facts["served"] = {row["id"]: AT for row in self.base["families"]}
        facts["turns"] = heading_turns(view, facts)
        ordered = sorted(view["families"], key=lambda row: heading_key(row, facts, list(CATEGORIES)))
        self.assertEqual(ordered[0]["id"], added[0]["id"])
        # The next round tops up the next category.
        second = self.advance(TopupRunner(lambda entry: [heading("Foldable crib widget mats")]), at=LATER)
        self.assertEqual(second["topup"]["state"], "added")
        self.assertEqual(self.lane()["topup_last_category"], "Baby Products")
        self.assertEqual(self.lane()["topup_day"], {"date": DAY, "calls": 2})

    def test_the_top_up_leaves_the_call_to_the_build_and_rests_above_the_threshold(self):
        self.platform(items=[item("1", "Electric Egg Boiler")])
        self.advance(None)

        class Both(SeedRunner):
            def __call__(self, role, payload, folder):
                runner = TopupRunner() if payload.get("mode") == "catalogue_topup" else SeedRunner()
                self.calls.append(payload.get("mode"))
                return runner(role, payload, folder)
        runner = Both()
        first = self.advance(runner)
        self.assertEqual((first["build"]["state"], first["topup"]), ("built", {"state": "build_used_the_call"}))
        second = self.advance(runner)
        self.assertEqual((second["build"]["state"], second["topup"]["state"]), ("nothing_waiting", "added"))
        self.assertEqual(runner.calls, ["catalogue_branch_seed", "catalogue_topup"])
        self.use(min_unsearched_headings=200)
        third = self.advance(runner)
        self.assertEqual(third["topup"], {"state": "not_needed", "unsearched": 226})
        self.assertEqual(len(runner.calls), 2)
        self.assertFalse(self.hint()["ready"])

    def test_served_headings_count_down_the_map_and_wake_the_top_up(self):
        self.use(min_unsearched_headings=100)
        self.assertEqual(self.advance(TopupRunner())["topup"], {"state": "not_needed", "unsearched": 224})
        hint = self.hint()
        self.assertEqual((hint["ready"], hint["signal"]["topup"]), (False, None))
        self.serve([row["id"] for row in self.base["families"][:130]], AT)
        hint = self.hint()
        self.assertEqual((hint["ready"], hint["signal"]["topup"]), (True, ["call", DAY, 0]))
        self.assertFalse(self.hint(runner=False)["ready"])
        self.assertEqual(self.advance(TopupRunner())["topup"]["state"], "added")

    def test_at_most_the_days_calls_then_the_next_day(self):
        self.use(max_calls_per_day=2)
        runner = TopupRunner(lambda entry: [heading("Widget stands " + entry["id"][-6:])])
        for _ in range(2):
            self.assertEqual(self.advance(runner)["topup"]["state"], "added")
        third = self.advance(runner)
        self.assertEqual((third["topup"]["state"], third["topup"]["calls_today"]), ("day_limit", 2))
        self.assertEqual(len(runner.calls), 2)
        hint = self.hint()
        self.assertEqual((hint["ready"], hint["next_retry_at"]), (False, "2026-10-16T00:00:00+00:00"))
        tomorrow = "2026-10-16T01:00:00+00:00"
        self.assertTrue(self.hint(at=tomorrow)["ready"])
        self.assertEqual(self.advance(runner, at=tomorrow)["topup"]["state"], "added")
        self.assertEqual(self.lane()["topup_day"], {"date": "2026-10-16", "calls": 1})
        self.use(max_calls_per_day=0)
        self.assertEqual(self.advance(runner, at=tomorrow)["topup"]["state"], "day_limit")
        self.assertFalse(self.hint(at=tomorrow)["ready"])

    def test_a_failed_call_counts_and_waits_retry_minutes(self):
        class Broken(TopupRunner):
            def __call__(self, role, payload, folder):
                self.calls.append(payload)
                raise SafetyError("AI_RUNNER_UNAVAILABLE")
        runner = Broken()
        first = self.advance(runner)
        self.assertEqual((first["state"], first["topup"]["state"], first["topup"]["reason"]),
                         ("waiting", "waiting", "AI_RUNNER_UNAVAILABLE"))
        lane = self.lane()
        self.assertEqual(lane["topup_wait"], {"reason": "AI_RUNNER_UNAVAILABLE", "next_retry_at": "2026-10-15T07:00:00+00:00"})
        self.assertEqual(lane["topup_day"], {"date": DAY, "calls": 1})
        self.assertEqual(self.hint()["next_retry_at"], "2026-10-15T07:00:00+00:00")
        self.assertFalse(self.hint()["ready"])
        held = self.advance(TopupRunner())
        self.assertEqual((held["topup"]["state"], held["topup"]["reason"]), ("waiting", "AI_RUNNER_UNAVAILABLE"))
        self.assertEqual(len(runner.calls), 1)
        with patch("product_radar.model_recovery.run_current_model_job", side_effect=OSError("disk")):
            local = self.advance(TopupRunner(), at="2026-10-15T07:00:00+00:00")
        self.assertEqual(local["topup"]["reason"], "PRODUCT_MAP_TOPUP_LOCAL_FAILURE")

    def test_the_rounds_ai_allowance_and_the_runner_come_first(self):
        self.assertEqual(self.advance(None)["topup"], {"state": "waiting", "reason": "PRODUCT_MAP_MODEL_WAITING",
                                                       "unsearched": 224})
        policy = dict(deepcopy(DEFAULT_POLICY), model_executions=2, report_slots=0, review_slots=0)
        turn = open_work_turn(self.memory, "programme", at=AT, policy=policy)
        runner = TopupRunner()
        from product_radar.research_ai import Jobs
        from test_research_flow import FakeRunner
        with bind_work_turn(self.memory, turn["id"]):
            for n in range(2):
                Jobs(self.memory, FakeRunner()).run("A", {"items": [{"id": "spent-" + str(n), "source_text": {"title": "Unclear offer"}}]})
            result = self.advance(runner)
        self.assertEqual((result["topup"]["state"], result["topup"]["reason"]), ("waiting", "WORK_TURN_MODEL_LIMIT"))
        self.assertEqual(runner.calls, [])
        lane = self.lane()
        self.assertEqual((lane["topup_wait"], lane["topup_day"]), (None, None))  # an allowance wait ends with the round

    def test_a_saved_answer_is_admitted_after_a_local_failure_and_never_bought_twice(self):
        from product_radar import map_topup
        runner = TopupRunner()
        with patch.object(map_topup, "save_records", side_effect=OSError("disk")):
            first = self.advance(runner)
        self.assertEqual((first["topup"]["state"], first["topup"]["reason"]), ("waiting", "PRODUCT_MAP_TOPUP_LOCAL_FAILURE"))
        self.assertEqual(self.memory.snapshot()["catalogue_additions"], {})
        job_id = first["topup"]["job_id"]
        self.assertFalse(self.hint(runner=False)["ready"])
        later = "2026-10-15T07:00:00+00:00"
        hint = self.hint(at=later, runner=False)  # local work: no AI needed to admit a saved answer
        self.assertEqual((hint["ready"], hint["signal"]["topup"]), (True, ["admit", job_id]))
        second = self.advance(None, at=later)
        self.assertEqual((second["topup"]["state"], second["topup"]["job_id"], second["topup"]["replayed"]),
                         ("added", job_id, True))
        self.assertEqual(second["topup"]["outcomes"], {"accepted": 1})
        self.assertEqual(len(runner.calls), 1)
        self.assertEqual(self.lane()["topup_wait"], None)
        # The admitted job is done: the next call is a new question (another attempt), not the old one again.
        third = self.advance(runner, at=later)
        self.assertEqual(third["topup"]["state"], "added")
        self.assertNotEqual(third["topup"]["job_id"], job_id)
        self.assertEqual(len(runner.calls), 2)
        self.assertNotEqual(runner.calls[0]["items"][0]["id"], runner.calls[1]["items"][0]["id"])
        # A disk failure between the call and its admission replays the same saved job too.
        with patch.object(map_topup, "admission_records", side_effect=OSError("disk")):
            crashed = self.advance(runner, at="2026-10-15T09:00:00+00:00")
        self.assertEqual(crashed["topup"]["reason"], "PRODUCT_MAP_TOPUP_LOCAL_FAILURE")
        replayed = self.advance(None, at="2026-10-15T10:00:00+00:00")
        self.assertEqual((replayed["topup"]["job_id"], replayed["topup"]["replayed"]), (crashed["topup"]["job_id"], True))
        self.assertEqual(len(runner.calls), 3)
        # A saved answer whose admission fails in itself is closed with one rejected record, never replayed again.
        with patch.object(map_topup, "admission_records", side_effect=KeyError("bad answer")):
            closed = self.advance(runner, at="2026-10-15T11:00:00+00:00")
        self.assertEqual((closed["topup"]["state"], closed["topup"]["reason"]), ("closed", "PRODUCT_MAP_TOPUP_ADMISSION_FAILED"))
        records = [row for row in self.memory.snapshot()["catalogue_additions"].values()
                   if row["job_id"] == closed["topup"]["job_id"]]
        self.assertEqual([(row["outcome"], row["reason"]) for row in records], [("rejected", "PRODUCT_MAP_TOPUP_ADMISSION_FAILED")])
        self.assertIsNone(map_topup.saved_unadmitted_job(self.memory, self.memory.snapshot()))
        # Reading the saved jobs failing now waits retry_minutes.
        with patch.object(map_topup, "saved_unadmitted_job", side_effect=SafetyError("AI_RETRY_RECORD_INVALID")):
            broken = self.advance(None, at="2026-10-15T12:00:00+00:00")
        self.assertEqual(broken["topup"], {"state": "waiting", "reason": "AI_RETRY_RECORD_INVALID"})

    def test_admission_rules_for_proposed_headings(self):
        from product_radar import map_topup
        state = self.memory.snapshot()
        branch = next(row for row in self.base["branches"] if row["id"] == "branch-hk-baking-and-dessert-making")
        item = map_topup.topup_item(self.base, state, branch, 12)
        meta = next(row["text"] for row in self.base["families"][0]["phrases"] if row["source"] == "meta")
        proposals = [heading("Electric egg boilers", meta="electric egg boiler"),  # another kitchen sub-area: joins this one
                     heading("Cake decorating turntables"),
                     heading("Cake stand spinners", meta=meta),
                     heading("Lipstick holders"),
                     dict(heading("Cake toppers"), label="https://example.com"),
                     heading("Kids party hats"),
                     heading("Christmas cookie cutter sets", season="seasonal", months=[12, 11]),
                     heading("Cake decorating turntables", meta="rotating cake stand"),
                     heading("Cake-decorating turntable", meta="cake turntable stand"),
                     heading("Electric cake decorating turntables", meta="electric cake turntable"),
                     heading("Camping cooking stoves", meta="camping stove"),
                     heading("Icing bags", season="evergreen", months=[10]),
                     heading("Piping nozzles", season="seasonal", months=[0])]
        result = {"accepted": {item["id"]: {"headings": proposals, "reason": "x"}}, "failures": {}}
        records = map_topup.admission_records(self.base, state, item, result, "job-1", self.config, AT)
        outcomes = [(row["outcome"], row.get("reason") if row["outcome"] == "rejected" else row.get("family_id"))
                    for row in records]
        self.assertEqual(records[0]["outcome"], "accepted")
        self.assertEqual(records[0]["family"]["parent_id"], branch["id"])
        self.assertEqual(records[1]["outcome"], "accepted")
        turntables = records[1]["family_id"]
        self.assertEqual((records[2]["outcome"], records[2]["family_id"], records[2]["aliases"]),
                         ("merged", self.base["families"][0]["id"], ["Cake stand spinners"]))
        self.assertEqual(outcomes[3:6], [("rejected", "CATALOGUE_TOPUP_EXCLUDED_KIND"), ("rejected", "CATALOGUE_TOPUP_LABEL_INVALID"),
                                         ("rejected", "CATALOGUE_TOPUP_OTHER_CATEGORY")])
        self.assertEqual(records[6]["outcome"], "accepted")
        self.assertEqual(records[6]["family"]["season"], {"kind": "seasonal", "months": [11, 12]})
        self.assertIn("Months are a planning judgment", records[6]["family"]["review_note"])
        # The same heading again, in another form (singular, hyphen) or as a variant, merges without change.
        self.assertEqual(outcomes[7:10], [("merged", turntables)] * 3)
        self.assertEqual([records[n]["aliases"] for n in (7, 8, 9)], [[], [], []])
        self.assertIn("another form", records[8]["reason"])
        self.assertIn("variant", records[9]["reason"])
        self.assertEqual(outcomes[10], ("rejected", "CATALOGUE_TOPUP_OTHER_CATEGORY"))  # a camping product
        # A bad season drops only that heading.
        self.assertEqual(outcomes[11:], [("rejected", "CATALOGUE_TOPUP_SEASON_INVALID")] * 2)
        self.assertEqual({row["topup_version"] for row in records}, {map_topup.VERSION})
        self.assertEqual(len({row["id"] for row in records}), len(records))
        map_topup.save_records(self.memory, records)
        view = effective_catalogue(self.base, self.memory.snapshot())
        validate_catalogue(view)
        self.assertIn("Cake stand spinners", next(row for row in view["families"] if row["id"] == records[2]["family_id"])["aliases"])
        # An empty answer or a contract failure is one finished record.
        for answer, outcome in (({"accepted": {item["id"]: {"headings": [], "reason": "Nothing new."}}, "failures": {}},
                                 "no_useful_addition"),
                                ({"accepted": {}, "failures": {item["id"]: "AI_TEXT_INVALID"}}, "rejected")):
            rows = map_topup.admission_records(self.base, state, item, answer, "job-2", self.config, AT)
            self.assertEqual([row["outcome"] for row in rows], [outcome])
        # The sub-area left the map (a later catalogue revision): one rejected record, no crash.
        gone = dict(self.base, branches=[row for row in self.base["branches"] if row["id"] != branch["id"]])
        rows = map_topup.admission_records(gone, state, item, result, "job-3", self.config, AT)
        self.assertEqual([(row["outcome"], row["reason"]) for row in rows], [("rejected", "CATALOGUE_TOPUP_SUB_AREA_GONE")])

    def test_thin_top_ups_rest_a_sub_area_and_full_or_waiting_ones_are_left_out(self):
        from product_radar import map_topup
        def choose(at=AT, target=40, waiting=()):
            return map_topup.topup_choice(self.base, self.memory.snapshot(), {}, None, at, target=target, waiting=waiting)
        chosen = choose()
        item = map_topup.topup_item(self.base, self.memory.snapshot(), chosen, 12)

        def answer(job_id, at, *labels):
            result = {"accepted": {item["id"]: {"headings": [heading(label) for label in labels], "reason": "x"}},
                      "failures": {}}
            map_topup.save_records(self.memory, map_topup.admission_records(
                self.base, self.memory.snapshot(), item, result, job_id, self.config, at))
        answer("job-a", AT, "Pet widget beds")  # nothing added
        self.assertEqual(choose()["id"], chosen["id"])  # one thin top-up is not yet a rest
        answer("job-b", LATER, "Foldable widget stands", "Foldable widget trays")  # two added: thin again
        state = self.memory.snapshot()
        self.assertIn(chosen["id"], map_topup.resting_branches(state, LATER))
        self.assertIn(chosen["id"], map_topup.resting_branches(state, "2026-11-13T09:00:00+00:00"))  # 29 days on
        self.assertNotIn(chosen["id"], map_topup.resting_branches(state, "2026-11-15T09:00:00+00:00"))  # rested 30 days
        self.assertNotEqual(choose(LATER)["id"], chosen["id"])
        answer("job-c", "2026-10-15T10:00:00+00:00", "Widget hooks", "Widget clips", "Widget bins")  # three added
        self.assertNotIn(chosen["id"], map_topup.resting_branches(self.memory.snapshot(), LATER))
        # Its next item is a new attempt (another question), even with the same headings.
        again = map_topup.topup_item(self.base, self.memory.snapshot(), chosen, 12)
        self.assertNotEqual(again["id"], item["id"])
        # A sub-area on a wait is left out; at three times the target headings it is full.
        self.assertNotEqual(choose(waiting={chosen["id"]})["id"], chosen["id"])
        empty = {row["id"] for row in self.base["branches"] if row["parent_id"] and row["id"] != chosen["id"]
                 and not any(family["parent_id"] == row["id"] for family in self.base["families"])}
        self.assertEqual(choose(target=3, waiting=empty | {chosen["id"]})["parent_id"] is not None, True)  # 8 < 9
        self.assertIsNone(choose(target=2, waiting=empty | {chosen["id"]}))  # 8 >= 6: every other sub-area is full
        self.assertIsNone(map_topup.topup_choice(dict(self.base, branches=[]), self.memory.snapshot(), {}, None, AT,
                                                 target=40))

    def test_the_ai_sees_the_most_similar_category_labels(self):
        from product_radar import map_topup
        branch = next(row for row in self.base["branches"] if row["id"] == "branch-hk-baking-and-dessert-making")
        unrelated = [dict(self.base["families"][0], id="hk-extra-" + str(n), label="Aaa unrelated thing " + str(n),
                          parent_id="branch-hk-preparation-tools", category="Home and Kitchen") for n in range(450)]
        baking = [dict(self.base["families"][0], id="hk-bake-" + str(n), label="Zz baking dessert mould " + str(n),
                       parent_id="branch-hk-preparation-tools", category="Home and Kitchen") for n in range(5)]
        base = dict(self.base, families=self.base["families"] + unrelated + baking)
        item = map_topup.topup_item(base, {"catalogue_additions": {}}, branch, 12)
        self.assertEqual(len(item["category_headings"]), map_topup.MAX_CATEGORY_LABELS)
        self.assertTrue(item["category_headings_truncated"])
        # Alphabetically last, but sharing the sub-area's words ("baking", "dessert"): they are sent.
        self.assertTrue({row["label"] for row in baking} <= set(item["category_headings"]))
        self.assertEqual(item["category_headings"], sorted(item["category_headings"], key=lambda text: text.casefold()))

    def test_a_failed_call_puts_its_sub_area_on_a_wait_so_the_rotation_moves_on(self):
        class Broken(TopupRunner):
            def __call__(self, role, payload, folder):
                self.calls.append(payload)
                raise SafetyError("AI_AUTH_REQUIRED")
        failed = self.advance(Broken())
        branch_id = failed["topup"]["branch_id"]
        lane = self.lane()
        self.assertEqual(lane["topup_branch_waits"], {branch_id: {"reason": "AI_AUTH_REQUIRED",
                                                                  "next_retry_at": "2026-10-15T07:00:00+00:00"}})
        self.assertEqual(lane["topup_day"]["calls"], 1)
        good = TopupRunner(lambda entry: [heading("Widget stands " + entry["id"][-6:])])
        rounds = [self.advance(good, at=at)["topup"] for at in ("2026-10-15T07:00:00+00:00", "2026-10-15T08:00:00+00:00",
                                                                 "2026-10-16T09:00:00+00:00")]
        self.assertEqual([row["state"] for row in rounds], ["added"] * 3)
        self.assertNotIn(branch_id, [row["branch_id"] for row in rounds])
        self.assertEqual(self.lane()["topup_branch_waits"], {})  # an ended wait is dropped
        # Back to that sub-area: its saved original still waits for reconciliation. Only that sub-area
        # waits (a day); nothing is sent, nothing is counted, and the next round tops up another sub-area.
        later = "2026-10-30T09:00:00+00:00"
        pending = self.advance(good, at=later)["topup"]
        self.assertEqual((pending["branch_id"], pending["reason"]), (branch_id, "AI_JOB_PENDING_RECONCILIATION"))
        lane = self.lane()
        self.assertEqual((lane["topup_wait"], lane["topup_day"]), (None, {"date": "2026-10-16", "calls": 1}))
        self.assertEqual(lane["topup_branch_waits"][branch_id]["next_retry_at"], "2026-10-31T09:00:00+00:00")
        again = self.advance(good, at=later)["topup"]
        self.assertEqual(again["state"], "added")
        self.assertNotEqual(again["branch_id"], branch_id)
        self.assertEqual(len(good.calls), 4)

    def test_an_answer_that_can_never_be_admitted_is_closed_not_replayed_forever(self):
        from product_radar import map_topup

        class Bad(TopupRunner):
            def __call__(self, role, payload, folder):
                self.calls.append(payload)
                entry = payload["items"][0]
                return {"results": [{"id": entry["id"], "headings": [], "reason": "x"}], "note": "extra"}, {}
        first = self.advance(Bad())
        self.assertEqual(first["topup"]["reason"], "AI_ENVELOPE_INVALID")  # the answer is saved but unreadable
        job_id = map_topup.saved_unadmitted_job(self.memory, self.memory.snapshot())[0]
        closed = self.advance(None, at="2026-10-15T07:00:00+00:00")["topup"]
        self.assertEqual(closed, {"state": "closed", "reason": "AI_ENVELOPE_INVALID", "job_id": job_id, "replayed": True})
        self.assertIsNone(map_topup.saved_unadmitted_job(self.memory, self.memory.snapshot()))
        good = TopupRunner()
        self.assertEqual(self.advance(good, at="2026-10-15T08:00:00+00:00")["topup"]["state"], "added")
        self.assertEqual(len(good.calls), 1)
        # A replay read that fails now (a self-healing failure) waits instead of closing.
        with patch.object(map_topup, "save_records", side_effect=OSError("disk")):
            self.advance(good, at="2026-10-15T09:00:00+00:00")
        with patch.object(map_topup, "replay_saved_job", side_effect=OSError("disk")):
            held = self.advance(None, at="2026-10-15T10:00:00+00:00")["topup"]
        self.assertEqual((held["state"], held["reason"]), ("waiting", "PRODUCT_MAP_TOPUP_LOCAL_FAILURE"))
        with patch.object(map_topup, "admission_records", side_effect=SafetyError("SAVED_FILE_UNAVAILABLE")):
            held = self.advance(None, at="2026-10-15T11:00:00+00:00")["topup"]
        self.assertEqual((held["state"], held["reason"]), ("waiting", "SAVED_FILE_UNAVAILABLE"))
        # Closing it cannot be saved now: it waits too.
        with patch.object(map_topup, "replay_saved_job", side_effect=SafetyError("AI_JOB_RESULT_CONFLICT")), \
                patch.object(map_topup, "save_records", side_effect=OSError("disk")):
            held = self.advance(None, at="2026-10-15T12:00:00+00:00")["topup"]
        self.assertEqual((held["state"], held["reason"]), ("waiting", "PRODUCT_MAP_TOPUP_LOCAL_FAILURE"))
        later = self.advance(None, at="2026-10-15T13:00:00+00:00")["topup"]
        self.assertEqual((later["state"], later["replayed"]), ("added", True))

    def test_a_saved_answer_for_a_sub_area_that_left_the_map_is_closed(self):
        from product_radar import map_topup
        from product_radar.catalogue_discovery import _map_result
        runner = TopupRunner()
        with patch.object(map_topup, "save_records", side_effect=OSError("disk")):
            self.advance(runner)
        branch_id = runner.calls[0]["items"][0]["branch"]["id"]
        self.base = dict(self.base, branches=[row for row in self.base["branches"] if row["id"] != branch_id],
                         families=[row for row in self.base["families"] if row["parent_id"] != branch_id])
        result = _map_result(lambda: self.advance(None, at="2026-10-15T08:00:00+00:00"))
        self.assertEqual((result["topup"]["state"], result["topup"]["outcomes"]), ("added", {"rejected": 1}))
        record = next(iter(self.memory.snapshot()["catalogue_additions"].values()))
        self.assertEqual(record["reason"], "CATALOGUE_TOPUP_SUB_AREA_GONE")

    def test_the_build_and_renewal_state_is_saved_before_the_top_up(self):
        self.platform(items=[item("1", "Electric Egg Boiler")])
        with patch("product_radar.catalogue_seed.build_catalogue_branch", side_effect=OSError("disk")), \
                patch.object(product_map, "_topup", side_effect=KeyError("broken")):
            result = self.advance(SeedRunner())
        self.assertEqual(result["topup"], {"state": "waiting", "reason": "PRODUCT_MAP_TOPUP_LOCAL_FAILURE"})
        self.assertIn("branch-hk-small-kitchen-appliances", self.lane()["branch_waits"])
        with patch("product_radar.catalogue_seed.build_catalogue_branch", side_effect=OSError("disk")), \
                patch.object(product_map, "_topup", side_effect=SafetyError("RESEARCH_EVENTS_INVALID")):
            with self.assertRaisesRegex(SafetyError, "RESEARCH_EVENTS_INVALID"):
                self.advance(SeedRunner(), at="2026-10-15T07:00:00+00:00")
        # The build's new wait was saved before the top-up's integrity failure stopped the round.
        self.assertEqual(self.lane()["branch_waits"]["branch-hk-small-kitchen-appliances"]["next_retry_at"],
                         "2026-10-15T07:30:00+00:00")

    def test_the_wake_hint_survives_a_top_up_defect_and_waits_when_no_sub_area_is_open(self):
        from product_radar import map_topup
        self.platform(items=[item("1", "Electric Egg Boiler")])
        self.advance(None)
        with patch.object(map_topup, "saved_unadmitted_job", side_effect=SafetyError("AI_RETRY_RECORD_INVALID")):
            hint = self.hint()
        self.assertEqual((hint["ready"], hint["signal"]["topup"]), (True, "AI_RETRY_RECORD_INVALID"))  # the build still wakes it
        self.assertEqual(hint["signal"]["build"][0], "branch-hk-small-kitchen-appliances")
        lane = self.lane()
        lane["topup_branch_waits"] = {"branch-hk-preparation-tools": {"reason": "AI_AUTH_REQUIRED",
                                                                      "next_retry_at": "2026-10-15T09:00:00+00:00"}}
        self.memory.put("batch_runs", lane["id"], lane, previous=self.memory.snapshot()["batch_runs"][lane["id"]])
        with patch.object(map_topup, "topup_choice", return_value=None):
            hint = product_map.topup_hint(self.memory, self.memory.snapshot(), self.base, self.lane(), AT, self.config,
                                          runner_available=True)
        self.assertEqual(hint, {"ready": False, "next_retry_at": "2026-10-15T09:00:00+00:00", "signal": "no_sub_area"})
        with patch.object(map_topup, "topup_choice", return_value=None):
            result = self.advance(TopupRunner(), at=LATER)
        self.assertEqual(result["topup"]["state"], "build_used_the_call")
        with patch.object(map_topup, "topup_choice", return_value=None):
            result = self.advance(TopupRunner(), at=LATER)
        self.assertEqual(result["topup"], {"state": "nothing_to_top_up", "unsearched": 224})

    def test_top_up_headings_come_after_fresh_signals_and_before_plain_re_searches(self):
        from product_radar import map_topup
        from product_radar.heading_order import heading_key, heading_turns
        from product_radar.service import CATEGORIES
        families = [row for row in self.base["families"] if row["category"] == "Home and Kitchen"][:3]
        topup = dict(families[0], id="hk-topup-idea", author=map_topup.AUTHOR)
        catalogue = {"families": [*families, topup]}
        facts = {"served": {families[1]["id"]: AT, families[2]["id"]: AT}, "crowded": set(),
                 "fresh": {families[1]["id"]: 2}, "branch_served": {}}
        facts["turns"] = heading_turns(catalogue, facts)
        ordered = sorted(catalogue["families"], key=lambda row: heading_key(row, facts, list(CATEGORIES)))
        # Never served (observed or reviewed), fresh signals, the AI top-up's idea, then a plain re-search.
        self.assertEqual([row["id"] for row in ordered], [families[0]["id"], families[1]["id"], "hk-topup-idea",
                                                         families[2]["id"]])

    def test_the_target_is_not_part_of_what_staging_depends_on(self):
        config = deepcopy(self.config)
        changed = dict(config, target={"headings_per_sub_area": 12})
        self.assertEqual(product_map.config_hash(config, self.base), product_map.config_hash(changed, self.base))
        self.assertNotEqual(product_map.config_hash(config, self.base),
                            product_map.config_hash(config, dict(self.base, revision="other")))

    def test_long_category_lists_are_capped(self):
        from product_radar import map_topup
        branch = next(row for row in self.base["branches"] if row["id"] == "branch-hk-baking-and-dessert-making")
        families = [dict(self.base["families"][0], id="hk-extra-" + str(n), label="Extra heading " + str(n),
                         parent_id="branch-hk-preparation-tools", category="Home and Kitchen") for n in range(450)]
        base = dict(self.base, families=self.base["families"] + families)
        item = map_topup.topup_item(base, {"catalogue_additions": {}}, branch, 12)
        self.assertEqual((len(item["category_headings"]), item["category_headings_truncated"]),
                         (map_topup.MAX_CATEGORY_LABELS, True))
        with patch.object(map_topup, "MAX_ITEM_BYTES", 2000):
            small = map_topup.topup_item(base, {"catalogue_additions": {}}, branch, 12)
        self.assertLessEqual(len(canonical(small)), 2000)
        self.assertTrue(small["category_headings_truncated"])

    def test_an_older_lane_record_loads_with_top_up_defaults(self):
        lane_id = product_map._lane_id("programme")
        old = {"kind": product_map.LANE_KIND, "id": lane_id, "programme_id": "programme", "last_category": None,
               "branch_waits": {}, "set_aside": {}, "renewal_wait": None}
        self.memory.put("batch_runs", lane_id, old)
        lane = self.lane()
        self.assertEqual({name: lane[name] for name in ("topup_wait", "topup_day", "topup_last_category")},
                         {"topup_wait": None, "topup_day": None, "topup_last_category": None})
        self.advance(TopupRunner())
        self.assertEqual(self.memory.snapshot()["batch_runs"][lane_id]["topup_day"], {"date": DAY, "calls": 1})
        current = self.memory.snapshot()["batch_runs"][lane_id]
        for broken in (dict(current, topup_day={"date": DAY}), dict(current, topup_wait={"reason": 1}),
                       dict(current, topup_wait={"reason": "x", "next_retry_at": "not a time"}),
                       dict(current, topup_last_category=3), dict(current, extra=1),
                       dict(current, topup_branch_waits=[]), dict(current, topup_branch_waits={"x": {"reason": 1}}),
                       dict(current, topup_branch_waits={"x": None}),
                       {name: value for name, value in current.items() if name != "topup_day"}):
            self.memory.put("batch_runs", lane_id, broken, previous=self.memory.snapshot()["batch_runs"][lane_id])
            with self.subTest(broken=sorted(broken)):
                with self.assertRaisesRegex(SafetyError, "PRODUCT_MAP_LANE_INVALID"):
                    self.lane()


class RunwayTests(TopupCase):
    """The owner's product-map line and its pace maths."""

    def summary(self, at=AT):
        return product_map.runway_summary(self.base, self.memory.snapshot(), at, self.config)

    def test_wording_for_the_owner(self):
        text = product_map.runway_text
        later = ("headings are searched again for new ads, and the AI adds new headings whenever fewer than 300 are "
                 "left unsearched.")
        self.assertEqual(text({"headings": 2140, "unsearched": 1980, "pace_per_day": 70.0, "topup_threshold": 300}),
                         "Product map: 2,140 headings, 1,980 not searched yet; about 70 searched a day lately, so "
                         "roughly 4 weeks until every heading has had its first search. After that, " + later)
        self.assertEqual(text({"headings": 224, "unsearched": 224, "pace_per_day": None, "topup_threshold": 300}),
                         "Product map: 224 headings, 224 not searched yet; the search pace is not measured yet (no "
                         "heading has been searched). After that, " + later)
        self.assertEqual(text({"headings": 224, "unsearched": 0, "pace_per_day": 3.0, "topup_threshold": 300}),
                         "Product map: 224 headings; every heading has had its first search. Now " + later)
        self.assertIn("; no heading had its first search in the last 7 days. After every heading's first search, ",
                      text({"headings": 224, "unsearched": 10, "pace_per_day": 0.0, "topup_threshold": 300}))
        self.assertIn("; fewer than one searched a day lately, so roughly 5 months until",
                      text({"headings": 224, "unsearched": 100, "pace_per_day": 0.7, "topup_threshold": 300}))
        self.assertIn("so about a day until", text({"headings": 9, "unsearched": 9, "pace_per_day": 10, "topup_threshold": 1}))
        self.assertIn("so about 5 days until", text({"headings": 9, "unsearched": 50, "pace_per_day": 10, "topup_threshold": 1}))
        self.assertEqual(text(None), "Product map: not measured yet (the product-map lane has not run).")

    def test_pace_counts_first_searches_in_the_last_seven_days(self):
        ids = [row["id"] for row in self.base["families"]]
        self.assertEqual(self.summary()["pace_per_day"], None)
        self.serve(ids[:14], "2026-10-05T06:00:00+00:00")  # ten days ago
        self.serve(ids[14:84], "2026-10-12T06:00:00+00:00")
        self.serve(ids[:14], "2026-10-14T06:00:00+00:00")  # served again: not a first search
        summary = self.summary()
        self.assertEqual({name: summary[name] for name in ("headings", "unsearched", "first_searched_last_7_days",
                                                           "pace_per_day")},
                         {"headings": 224, "unsearched": 140, "first_searched_last_7_days": 70, "pace_per_day": 10.0})
        self.assertIn("about 10 searched a day lately, so roughly 2 weeks until", product_map.runway_text(summary))
        # A searched heading's query outcome counts too.
        self.memory.put("query_outcomes", "outcome-1", {"family_id": ids[100], "observed_at": "2026-10-15T05:00:00+00:00"})
        self.assertEqual(self.summary()["first_searched_last_7_days"], 71)

    def test_a_history_shorter_than_a_week_is_measured_over_its_own_days(self):
        ids = [row["id"] for row in self.base["families"]]
        self.serve(ids[:20], "2026-10-13T06:00:00+00:00")  # two days ago
        summary = self.summary()
        self.assertEqual((summary["first_searched_last_7_days"], summary["pace_per_day"]), (20, 10.0))
        self.serve(ids[20:30], "2026-10-15T05:00:00+00:00")
        self.assertEqual(self.summary("2026-10-13T08:00:00+00:00")["pace_per_day"], 20.0)  # at least one day

    def test_the_lane_saves_the_line_the_desktop_reads(self):
        from product_radar.storage import atomic_json
        self.assertEqual(product_map.map_line(self.memory.root), product_map.runway_text(None))
        self.use(min_unsearched_headings=0)
        self.advance(None)
        saved = load_json(self.memory.root / "product_map/runway.json")
        self.assertEqual(saved, self.summary())
        self.assertEqual(product_map.map_line(self.memory.root), product_map.runway_text(saved))
        for broken in ({"version": 2}, dict(saved, headings=-1), dict(saved, pace_per_day="fast"), []):
            atomic_json(self.memory.root / "product_map/runway.json", broken)
            with self.assertRaisesRegex(SafetyError, "PRODUCT_MAP_RUNWAY_INVALID"):
                product_map.map_line(self.memory.root)
        with patch("product_radar.storage.atomic_json", side_effect=OSError("disk")):
            self.assertEqual(self.advance(None)["map"]["unsearched"], 224)  # an unsaved line never stops the round

    def test_the_owner_meter_reads_the_line_from_saved_records(self):
        self.assertEqual(product_map.runway_line(self.base, self.memory.snapshot(), AT),
                         product_map.runway_text(self.summary()))


class RotationTests(unittest.TestCase):
    """Heading searches, the platforms, wider discovery and the product map each
    have the first use of the AI calls held for discovery within four rounds."""

    def test_each_discovery_lane_has_the_first_use_within_four_rounds(self):
        from test_programme_dependency_boundaries import ProgrammeDependencyBoundaryTests
        from test_research_flow import FakeRunner
        harness = ProgrammeDependencyBoundaryTests("test_platform_lane_runs_every_round_and_takes_turns_with_discovery")
        harness.setUp()
        self.addCleanup(harness.doCleanups)
        calls = []

        def lane(name, result):
            return lambda *_args, **_options: calls.append((name, harness.held())) or result
        step = discovery.create_catalogue_discovery_step(shared_marketplace=True, renewable_waves=True,
            wider_step=lane("wider", {"state": "checked", "new_concept_ids": []}),
            map_step=lane("map", {"state": "checked"}))
        firsts = []
        with patch.object(discovery, "advance_discovery_waves",
                          side_effect=lane("headings", {"state": "checked", "new_concept_ids": []})):
            for _ in range(4):
                calls.clear()
                harness.advance(steps=harness.noops(), runner=FakeRunner(), discovery_step=step,
                                platform_step=lane("platforms", {"discovery": {"new_concept_ids": []}}))
                firsts.append(calls[0][0])
                self.assertEqual(sorted(name for name, _ in calls), ["headings", "map", "platforms", "wider"])
                self.assertEqual(calls[0][1], 0)  # the first lane has the whole held share
        self.assertEqual(sorted(firsts), ["headings", "map", "platforms", "wider"])


class VersionTests(unittest.TestCase):
    def test_new_prompt_and_schema_versions_carry_the_dropship_phrase_source(self):
        from product_radar import catalogue_seed as seed
        from product_radar.research_ai import CATALOGUE_PROMPT_VERSION, PHRASE, c_contract
        self.assertEqual((seed.PROMPT_VERSION, CATALOGUE_PROMPT_VERSION), ("research-c-catalogue-seed-v2", "research-c-v2"))
        self.assertIn("dropship", PHRASE["properties"]["source"]["enum"])
        contract = c_contract({"mode": seed.MODE, "seed_version": seed.VERSION, "items": []})
        phrase = contract["schema"]["properties"]["results"]["items"]["properties"]["phrases"]["items"]
        self.assertIn("dropship", phrase["properties"]["source"]["enum"])
        self.assertIn("must include one specific Meta discovery phrase", contract["instructions"])
        with self.assertRaisesRegex(SafetyError, "AI_C_CONTRACT_INVALID"):
            c_contract({"mode": seed.MODE, "seed_version": seed.VERSION, "items": []}, "research-c-catalogue-seed-v1")
        base = load_catalogue()
        family = deepcopy(base["families"][0])
        family["phrases"].append(dict(family["phrases"][0], source="dropship"))
        validate_catalogue(dict(base, families=[family] + base["families"][1:]))
        family["phrases"][-1]["source"] = "amazon"
        with self.assertRaisesRegex(SafetyError, "CATALOGUE_PHRASE_INVALID"):
            validate_catalogue(dict(base, families=[family] + base["families"][1:]))

    def test_the_top_up_has_its_own_strict_prompt_schema_and_checks(self):
        from product_radar import map_topup
        from product_radar.research_ai import (Jobs, c_contract, instructions_for, prompt_version, schema_for,
                                               validate_results)
        base = load_catalogue()
        branch = next(row for row in base["branches"] if row["id"] == "branch-hk-baking-and-dessert-making")
        item = map_topup.topup_item(base, {"catalogue_additions": {}}, branch, 3)
        payload = map_topup.payload_for(item)
        self.assertEqual(prompt_version("C", payload), "research-c-catalogue-topup-v1")
        contract = c_contract(payload)
        self.assertEqual(contract["mode"], "catalogue_topup")
        self.assertEqual(schema_for("C", payload), contract["schema"])
        result = contract["schema"]["properties"]["results"]["items"]
        heading_schema = result["properties"]["headings"]["items"]
        self.assertEqual((set(result["required"]), result["additionalProperties"]), ({"id", "headings", "reason"}, False))
        self.assertEqual(set(heading_schema["required"]), {"label", "purpose", "included_example", "excluded_neighbour",
                                                           "meta_phrase", "season", "months"})
        self.assertFalse(heading_schema["additionalProperties"])
        instructions = instructions_for("C", payload)
        for words in ("India dropshipping", "small, light, generic, unbranded", "jewellery", "cosmetics", "clothing",
                      "phone accessories", "earphones", "stationery", "pets", "food", "medicines", "meta_phrase",
                      "untrusted data"):
            self.assertIn(words, instructions)
        with self.assertRaisesRegex(SafetyError, "AI_C_CONTRACT_INVALID"):
            c_contract(payload, "research-c-v2")

        def answer(*headings, reason="Gaps."):
            return {"results": [{"id": item["id"], "headings": list(headings), "reason": reason}]}
        good = heading("Cake decorating turntables")
        checked = validate_results("C", payload, answer(good, heading("Cookie stamps", season="seasonal", months=[10, 11])))
        self.assertEqual(checked["failures"], {})
        # Headings beyond the number asked for are trimmed, not a reason to lose the answer.
        trimmed = validate_results("C", payload, answer(good, heading("B c"), heading("C d"), heading("D e")))
        self.assertEqual([row["label"] for row in trimmed["accepted"][item["id"]]["headings"]],
                         ["Cake decorating turntables", "B c", "C d"])
        # A bad season is that heading's own problem (checked at admission); the answer stands.
        for bad in (heading("A b", months=[3]), heading("A b", season="seasonal"),
                    heading("A b", season="seasonal", months=[13]), heading("A b", season="seasonal", months=[4, 4])):
            checked = validate_results("C", payload, answer(good, bad))
            self.assertEqual((checked["failures"], len(checked["accepted"][item["id"]]["headings"])), ({}, 2))
            with self.assertRaisesRegex(SafetyError, "CATALOGUE_TOPUP_SEASON_INVALID"):
                map_topup._heading(bad)
        self.assertEqual(validate_results("C", payload, answer(good, reason=""))["failures"], {item["id"]: "AI_TEXT_INVALID"})
        extra = validate_results("C", payload, answer(dict(good, price="99")))["failures"]
        self.assertEqual(list(extra), [item["id"]])  # the strict schema refuses extra fields
        for broken in (dict(payload, topup_version="v0"), dict(payload, items=[item, dict(item, id="other")]),
                       dict(payload, items=[dict(item, headings_wanted=21)]),
                       dict(payload, items=[dict(item, headings_wanted=0)]),
                       dict(payload, items=[dict(item, extra=1)]),
                       dict(payload, items=[dict(item, category="Baby Products")]),
                       dict(payload, items=[dict(item, sub_area_headings="x")]),
                       dict(payload, items=[dict(item, category_headings_truncated="no")]),
                       dict(payload, items=[{"id": "x"}])):
            with self.subTest(broken=str(broken)[:80]):
                with self.assertRaisesRegex(SafetyError, "CATALOGUE_TOPUP_INPUT_INVALID"):
                    map_topup.validate_topup_input(broken)
        with tempfile.TemporaryDirectory() as folder:
            memory = ResearchMemory(Path(folder) / "research", Journal(Path(folder) / "finance"), evidence_type="synthetic")
            runner = TopupRunner()
            with self.assertRaisesRegex(SafetyError, "CATALOGUE_TOPUP_INPUT_INVALID"):
                Jobs(memory, runner).run("C", dict(payload, topup_version="v0"))
            self.assertEqual(runner.calls, [])

    def test_the_amazon_page_seed_lane_is_retired(self):
        import inspect
        from product_radar import catalogue_seed as seed
        for name in ("SEED_CYCLE_VERSION", "prepare_catalogue_seed_cycle", "advance_catalogue_seed_cycle",
                     "catalogue_seed_lane_hint"):
            self.assertFalse(hasattr(discovery, name), name)
        for name in ("stage_page_materials", "stage_marketplace_materials", "load_seed_inventory",
                     "prepare_catalogue_branch_inputs", "load_prepared_catalogue_branch"):
            self.assertFalse(hasattr(seed, name), name)
        options = inspect.signature(discovery.create_catalogue_discovery_step).parameters
        self.assertFalse({"seed_inventory", "seed_manifest", "seed_source_root"} & set(options))
        self.assertFalse((ROOT / "config/research/catalogue_branch_inventory_2026-10-08.json").exists())
        self.assertFalse((ROOT / "scripts/catalogue_research.py").exists())


if __name__ == "__main__":
    unittest.main()
