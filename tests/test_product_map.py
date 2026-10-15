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
from product_radar.models import SafetyError, digest
from product_radar.research_memory import ResearchMemory
from product_radar.storage import Journal, atomic_json, load_json
from product_radar.trends_rising import FILE as RISING_FILE, KIND as RISING_KIND, PARSER_VERSION as RISING_PARSER
from product_radar.wider_candidates import READ_KIND
from product_radar.work_turns import DEFAULT_POLICY, bind_work_turn, open_work_turn

AT = "2026-10-15T06:00:00+00:00"
LATER = "2026-10-15T09:00:00+00:00"


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
    def test_twelve_sub_areas_per_category_with_routing_words_and_a_target_near_1200(self):
        config = materials.settings()
        branches = [row for row in self.base["branches"] if row["parent_id"] is not None]
        from product_radar.service import CATEGORIES
        self.assertEqual({category: sum(row["category"] == category for row in branches) for category in CATEGORIES},
                         {category: 12 for category in CATEGORIES})
        self.assertEqual(set(config["branch_words"]), {row["id"] for row in branches})
        self.assertEqual(len(branches) * config["target"]["headings_per_sub_area"], 1200)
        self.assertEqual(len(self.base["families"]), 224)  # the existing headings stay the base

    def test_bad_settings_are_refused(self):
        good = load_json(materials.SETTINGS, 64 * 1024)
        broken_words = deepcopy(good)
        broken_words["branch_words"].pop("branch-hk-preparation-tools")
        upper = deepcopy(good)
        upper["branch_words"]["branch-hk-preparation-tools"] = ["Chopper"]
        for value in (dict(good, extra=1), dict(good, kind="x"), broken_words, upper,
                      dict(good, build=dict(good["build"], materials_per_call=11)),
                      dict(good, renewal=dict(good["renewal"], concepts_per_call=6)), [], None):
            with self.subTest(value=str(value)[:60]):
                with self.assertRaisesRegex(SafetyError, "PRODUCT_MAP_SETTINGS_INVALID"):
                    materials.validate_settings(value)

    def test_the_map_settings_are_a_checked_public_setting_of_the_daily_backup(self):
        from product_radar import operational_backup_selection as backup
        from product_radar.r10_cutover import CONFIG_FILES
        self.assertIn("config/research/product_map.json", CONFIG_FILES)
        empty = {table: {} for table in self.memory.snapshot()}
        name = "config/research/product_map.json"
        good = load_json(ROOT / name, 64 * 1024)
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
            collections=[collection("silicone-cupcake-moulds", "Silicone Cupcake Moulds"),
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
        self.assertEqual(staged, {"platform-collection:Silicone Cupcake Moulds": "branch-hk-baking-and-dessert-making",
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
        self.rising(["silicone cupcake mould", "egg boiler electric", "gold necklace"])
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
        self.assertEqual(staged["trends-rising:silicone cupcake mould"], "branch-hk-baking-and-dessert-making")
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
        self.platform(items=[item("1", "Silicone Cupcake Mould"), item("2", "Cupcake Mould Silicone Pack"),
                             item("3", "Silicone Cupcake Mould with Lid")])
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
        self.platform(items=[item("1", "Silicone Cupcake Mould"), item("2", "Cupcake Mould Silicone Pack"),
                             item("3", "Silicone Cupcake Mould with Lid")])
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
        self.platform(items=[item("1", "Silicone Cupcake Mould"), item("2", "Cupcake Mould Silicone Pack"),
                             item("3", "Silicone Cupcake Mould with Lid"), item("4", label + " Heart Shape")])
        later = self.advance()
        self.assertEqual(later["staging"]["outcomes"], {"known": 1})  # the admitted heading's words

    def test_one_word_wording_is_never_staged_and_never_hides_anything(self):
        self.platform(collections=[collection("lights", "Lights"), collection("toys", "Toys"), collection("bags", "Bags")],
                      items=[item("1", "Solar Garden Light Stake"), item("2", "Musical Toy Phone"), item("3", "Wooden Stacking Toy Rings")])
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
        self.platform(items=[item("1", "Solar Garden Light Stake")])
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
                             item("3", "Electric Kettle Steel Jug"), item("4", "Silicone Cupcake Mould"),
                             item("5", "Piping Bag Nozzle Icing Set")])
        with patch.object(product_map, "settings", return_value=config):
            result = self.advance()
        # The hand blender is passed over (busy sub-area) and does not use a look: the cupcake mould gets it.
        self.assertEqual(result["staging"]["looked_at"], 2)
        self.assertEqual(result["staging"]["outcomes"].get("sub_area_backlog"), 1)
        self.assertEqual(set(materials.staged_materials(self.examined(config)).values()),
                         {"branch-hk-small-kitchen-appliances", "branch-hk-baking-and-dessert-making"})

    def test_a_full_or_busy_sub_area_takes_no_more_material(self):
        config = deepcopy(materials.settings())
        config["target"]["headings_per_sub_area"] = 8  # every existing sub-area holds 8 headings
        config["build"]["unbuilt_per_branch"] = 1
        self.platform(items=[item("1", "Bamboo Chopping Board Stand"), item("2", "Electric Egg Boiler"),
                             item("3", "Hand Blender Electric Stick")])
        with patch.object(product_map, "settings", return_value=config):
            result = self.advance()
        outcomes = {name: row["outcome"] for name, row in self.examined(config).items()}
        self.assertEqual(outcomes["platform-item:deodap:item:1"], "sub_area_full")
        self.assertEqual(outcomes["platform-item:deodap:item:2"], "staged")
        self.assertNotIn("platform-item:deodap:item:3", outcomes)  # looked at again once the waiting one is built
        self.assertEqual(result["staging"]["outcomes"]["sub_area_backlog"], 1)

    def test_staging_failure_is_that_candidates_own(self):
        self.platform(items=[item("1", "Electric Egg Boiler"), item("2", "Silicone Cupcake Mould")])
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
        self.rising(["x" * 130, "silicone cupcake mould"])
        long_body = "Silicone cupcake mould " + "with extra words " * 20
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
        self.platform(items=[item("1", "Electric Egg Boiler"), item("2", "Silicone Cupcake Mould")])
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
        self.rising(["silicone cupcake mould"])
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
        self.platform(items=[item("4", "Silicone Cupcake Mould"), item("5", "Piping Bag Nozzle Icing Set")])
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
        self.platform(items=[item("1", "Silicone Cupcake Mould"), item("2", "Electric Egg Boiler"),
                             item("3", "Electric Hand Blender")])
        with patch.object(product_map, "settings", return_value=config):
            self.advance()
            first = self.advance(SeedRunner())
            second = self.advance(SeedRunner())
        # The baking sub-area has fewer... no: its single example waits until no fuller sub-area is ready.
        self.assertEqual((first["build"]["branch_id"], first["build"]["materials"]), ("branch-hk-small-kitchen-appliances", 2))
        self.assertEqual((second["build"]["branch_id"], second["build"]["materials"]), ("branch-hk-baking-and-dessert-making", 1))

    def test_the_lookup_copy_is_written_once_per_map_round(self):
        self.platform(items=[item("1", "Electric Egg Boiler"), item("2", "Silicone Cupcake Mould")])
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
