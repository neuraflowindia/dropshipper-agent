"""Saved marketplace fixtures test evidence linkage, not winning products."""

from copy import deepcopy
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
from product_radar.discovery import compute_market_signals
from product_radar.market_validation import import_saved_marketplace, plausible_marketplace_observations, review_marketplace
from product_radar.models import SafetyError, digest
from product_radar.research_flow import exclude_established_brand, run_flow
from product_radar.research_memory import ResearchMemory, import_capture, key
from product_radar.research_sources import map_research_rows
from product_radar.storage import Journal, atomic_json
from product_radar.service import policy_for_fit
from test_research_flow import FakeRunner, capture
from test_research_sources import amazon_row, request

NOW = "2026-10-06T10:00:00+00:00"


def amazon(memory, titles=None, acquisition="amazon-fixture", at=NOW, values=None):
    values = values if values is not None else [dict(amazon_row(), title=title, asin="B01234567" + str(n),
              link="https://www.amazon.in/dp/B01234567" + str(n)) for n, title in enumerate(titles or ["Fictional sink organiser"])]
    mapped = map_research_rows(values, request())
    return import_capture(memory, source="amazon", acquisition_id=acquisition, observed_at=at,
                          mapped=mapped, capture_hash=digest(values), reference={"fixture": "synthetic"})


class RelevanceRunner:
    identity = "marketplace-fixture-v1"
    def __init__(self, relationship="exact_product", fail_first=False):
        self.calls = []
        self.relationship = relationship
        self.fail_first = fail_first
    def __call__(self, role, payload, folder):
        self.calls.append(deepcopy(payload))
        output = []
        for n, item in enumerate(payload["items"]):
            rows = [{"id": o["id"], "relationship": self.relationship, "reason": "Synthetic relevance judgment."}
                    for o in item["observations"]]
            if self.fail_first and n == 0:
                rows[0]["id"] = "invented-observation"
            output.append({"id": item["id"], "criteria": [], "relevance": rows, "compliance_note": "", "missing_questions": []})
        return {"results": output}, {"input_tokens": 1, "output_tokens": 1, "cached_input_tokens": 0}


class MarketValidationTests(unittest.TestCase):
    def setUp(self):
        # These thirteen-row fixtures exercise the historical policy only.
        policy = policy_for_fit("fit-13-v1")
        historical_policy = patch("product_radar.service.load_policy", return_value=policy)
        historical_policy.start(); self.addCleanup(historical_policy.stop)
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        root = Path(self.temp.name)
        self.memory = ResearchMemory(root / "research", Journal(root / "finance"), evidence_type="synthetic")
        capture(self.memory)
        self.summary = run_flow(self.memory, FakeRunner(), as_of=NOW)
        self.caddy = next(p["id"] for p in self.summary["products"] if p["label"] == "Sink caddy")

    def test_positive_marketplace_review_preserves_checklist_and_source_dates_prices(self):
        amazon(self.memory)
        before = self.memory.snapshot()
        result = review_marketplace(self.memory, RelevanceRunner(), concept_ids=[self.caddy], as_of=NOW)
        after = self.memory.snapshot()
        self.assertEqual(before["assessments"], after["assessments"])
        self.assertEqual(before["associations"], after["associations"])
        saved = result["accepted"][self.caddy]
        self.assertTrue(saved["assessment_unchanged"])
        obs_id = saved["observation_ids"][0]
        observation = after["observations"][obs_id]
        self.assertEqual(saved["observed_at"][obs_id], NOW)
        self.assertEqual(observation["facts"]["price"]["displayed"], "₹249.00")
        relevance = {observation["semantic_id"]: saved["relevance"][0]["relationship"]}
        market = compute_market_signals({obs_id: observation}, relevance, NOW)
        self.assertTrue(market["families"]["marketplace"]["positive"])

    def test_repeat_reviews_skip_model_and_recollection(self):
        amazon(self.memory)
        runner = RelevanceRunner()
        first = review_marketplace(self.memory, runner, concept_ids=[self.caddy], as_of=NOW)
        second = review_marketplace(self.memory, runner, concept_ids=[self.caddy], as_of=NOW)
        self.assertIn(self.caddy, first["accepted"])
        self.assertEqual(second["reused"], [self.caddy])
        self.assertEqual(len(runner.calls), 1)
        self.assertEqual(second["collector_requests"], 0)

    def test_operator_brand_exclusion_skips_default_and_explicit_market_review(self):
        amazon(self.memory)
        description = self.memory.snapshot()["concepts"][self.caddy]["description"]
        exclude_established_brand(self.memory, {
            "concept_id": self.caddy, "expected_description_hash": digest(description),
            "reason": "Synthetic established-brand finding.", "source_url": "https://example.com/brand",
            "source_excerpt": "Synthetic brand evidence.", "reviewer": "test_operator", "reviewed_at": NOW})
        runner = RelevanceRunner()
        for concept_ids in (None, [self.caddy]):
            result = review_marketplace(self.memory, runner, concept_ids=concept_ids, as_of=NOW)
            self.assertNotIn(self.caddy, result["accepted"])
            self.assertTrue(any(row["concept_id"] == self.caddy and "Excluded" in row["reason"] for row in result["deferred"]))
        self.assertEqual(runner.calls, [])

    def test_market_rubric_distinguishes_same_product_family_from_shared_benefit(self):
        amazon(self.memory)
        runner = RelevanceRunner()
        review_marketplace(self.memory, runner, concept_ids=[self.caddy], as_of=NOW)
        payload = runner.calls[0]
        self.assertIn("same primary product use, function and basic mechanism", payload["task"])
        self.assertEqual(payload["comparison_examples"][0]["relationship"], "unrelated")
        self.assertIn("Mattress", payload["comparison_examples"][0]["listing"])
        self.assertEqual(payload["comparison_examples"][1]["relationship"], "relevant_family")

    def test_obsolete_market_rubric_does_not_reuse_old_positive_review(self):
        amazon(self.memory)
        runner = RelevanceRunner()
        state = self.memory.snapshot()
        obs_id, observation = next((i, o) for i, o in state["observations"].items() if o["source"] == "amazon")
        legacy = {"concept_id": self.caddy, "description_hash": digest(state["concepts"][self.caddy]["description"]),
                  "semantic_ids": [observation["semantic_id"]], "review_version": "marketplace-relevance-v1",
                  "model": "gpt-6-luna", "prompt_version": "research-abe-v1",
                  "relevance": [{"id": obs_id, "relationship": "relevant_family", "reason": "Obsolete broad-benefit fixture."}]}
        legacy_id = key("market-review", legacy)
        self.memory.put("market_reviews", legacy_id, legacy)
        current = review_marketplace(self.memory, runner, concept_ids=[self.caddy], as_of=NOW)
        self.assertEqual(current["reused"], [])
        self.assertEqual(len(runner.calls), 1)
        self.assertEqual(current["accepted"][self.caddy]["review_version"], "marketplace-relevance-v2")
        self.assertIn(legacy_id, self.memory.snapshot()["market_reviews"])

    def test_negative_and_unclear_reviews_are_cached_not_positive(self):
        for n, relationship in enumerate(("unrelated", "unclear")):
            with self.subTest(relationship=relationship):
                amazon(self.memory, ["Fictional sink organiser " + relationship], acquisition=relationship,
                       at="2026-10-06T09:" + ("00" if n == 0 else "30") + ":00+00:00")
                runner = RelevanceRunner(relationship)
                result = review_marketplace(self.memory, runner, concept_ids=[self.caddy], as_of=NOW)
                self.assertTrue(all(r["relationship"] == relationship for r in result["accepted"][self.caddy]["relevance"]))
                review_marketplace(self.memory, runner, concept_ids=[self.caddy], as_of=NOW)
                self.assertEqual(len(runner.calls), 1)

    def test_failure_source_is_retained_without_listing_or_demand_claim(self):
        amazon(self.memory, values=[], acquisition="empty-source")
        result = review_marketplace(self.memory, RelevanceRunner(), concept_ids=[self.caddy], as_of=NOW)
        self.assertEqual(result["accepted"], {})
        self.assertEqual(result["deferred"][0]["saved_listings"], 0)

    def test_wrong_scope_listing_is_not_used(self):
        row = dict(amazon_row(), categoryUrl="https://www.amazon.com/gp/bestsellers/kitchen")
        amazon(self.memory, values=[row])
        runner = RelevanceRunner()
        result = review_marketplace(self.memory, runner, concept_ids=[self.caddy], as_of=NOW)
        self.assertEqual(len(runner.calls), 0)
        self.assertEqual(result["accepted"], {})

    def test_stale_capture_does_not_become_fresh_by_review(self):
        amazon(self.memory, at="2026-09-20T10:00:00+00:00")
        runner = RelevanceRunner()
        review_marketplace(self.memory, runner, concept_ids=[self.caddy], as_of=NOW)
        self.assertEqual(len(runner.calls), 0)

    def test_new_date_same_listing_meaning_reuses_existing_relevance(self):
        amazon(self.memory, at="2026-10-04T10:00:00+00:00")
        runner = RelevanceRunner()
        review_marketplace(self.memory, runner, concept_ids=[self.caddy], as_of=NOW)
        amazon(self.memory, acquisition="new-capture", at=NOW)
        result = review_marketplace(self.memory, runner, concept_ids=[self.caddy], as_of=NOW)
        self.assertEqual(result["reused"], [self.caddy])
        self.assertEqual(len(runner.calls), 1)

    def test_retrieval_limits_are_visible_and_next_batch_advances(self):
        amazon(self.memory, ["Sink caddy " + str(n) for n in range(8)])
        runner = RelevanceRunner()
        first = review_marketplace(self.memory, runner, concept_ids=[self.caddy], observations_per_concept=3, as_of=NOW)
        self.assertEqual(first["accepted"][self.caddy]["retrieval"]["omitted"], 5)
        second = review_marketplace(self.memory, runner, concept_ids=[self.caddy], observations_per_concept=3, as_of=NOW)
        self.assertEqual(second["accepted"][self.caddy]["retrieval"]["omitted"], 2)
        self.assertTrue(set(first["accepted"][self.caddy]["semantic_ids"]).isdisjoint(second["accepted"][self.caddy]["semantic_ids"]))

    def test_all_unrelated_reviews_advance_thirteen_saved_candidates(self):
        values = [dict(amazon_row(), title="Sink caddy candidate " + str(n), asin=f"B{n:09d}",
                       link="https://www.amazon.in/dp/" + f"B{n:09d}") for n in range(13)]
        amazon(self.memory, values=values)
        runner = RelevanceRunner("unrelated")
        reviewed = set()
        for expected_count, remaining in ((5, 8), (5, 3), (3, 0)):
            result = review_marketplace(self.memory, runner, limit=1, as_of=NOW)
            record = result["accepted"][self.caddy]
            self.assertEqual(len(record["semantic_ids"]), expected_count)
            self.assertEqual(record["retrieval"]["omitted"], remaining)
            self.assertTrue(reviewed.isdisjoint(record["semantic_ids"]))
            self.assertTrue(all(row["relationship"] == "unrelated" for row in record["relevance"]))
            reviewed.update(record["semantic_ids"])
            self.assertEqual(result["collector_requests"], 0)
        finished = review_marketplace(self.memory, runner, limit=1, as_of=NOW)
        self.assertEqual(len(runner.calls), 3)
        self.assertEqual(len(reviewed), 13)
        self.assertEqual(finished["accepted"], {})
        self.assertIn(self.caddy, finished["reused"])
        self.assertIn("No unreviewed", finished["deferred"][0]["reason"])

    def test_title_plural_normalization_is_only_retrieval(self):
        state = self.memory.snapshot()
        desc = {**state["concepts"][self.caddy]["description"], "label": "Chair cover", "function": "Protect chair"}
        amazon(self.memory, ["Dining chair covers"])
        pool = {o["semantic_id"]: (i, o) for i, o in self.memory.snapshot()["observations"].items() if o["source"] == "amazon"}
        result = plausible_marketplace_observations(desc, pool)
        self.assertEqual(len(result["observations"]), 1)
        self.assertIn("not_identity", result["method"])

    def test_generic_category_words_do_not_trigger_automatic_match(self):
        amazon(self.memory, ["Premium kitchen essentials"])
        runner = RelevanceRunner()
        result = review_marketplace(self.memory, runner, concept_ids=[self.caddy], as_of=NOW)
        self.assertEqual(len(runner.calls), 0)
        self.assertIn("different wording", result["deferred"][0]["limitation"])

    def shortlist(self, description, titles):
        pool = {str(n): ("observation-" + str(n), {"semantic": {"title": title}})
                for n, title in enumerate(titles)}
        result = plausible_marketplace_observations(description, pool, limit=20)
        return [row["source_text"]["title"] for row in result["observations"]]

    def wash_bag(self):
        return {"label": "Blue PVC Travel Wash Bag Set",
                "function": "Carry and organize toiletries or washing items while travelling.",
                "mechanism": "Two bags provide storage for wash items during travel.",
                "features": ["Pack of 2", "Blue", "PVC"]}

    def lunch_bag(self):
        return {"label": "Pink and Blue Printed Lunch Bag Set",
                "function": "Carry and organize lunch.",
                "mechanism": "A set of two bags for carrying lunch.",
                "features": ["Pack of 2", "Pink and blue printed design"]}

    def test_wash_bag_requires_carrying_use_not_body_wash_or_generic_bag(self):
        unrelated = ["Ezee Premium Garbage Bags for Dustbin Trash Bag",
                     "Shalimar Premium Scented Garbage Bags with Lavender Fragrance",
                     "Mother Sparsh Milky Soft Head to Toe Baby Wash and Shampoo",
                     "Aveeno Baby Daily Moisture Wash and Shampoo",
                     "Sebamed Baby Gentle Wash Body Wash Soap Free Tear Free",
                     "Himalaya Gentle Baby Wash No Tears Formula",
                     "Body wash gift kit", "Travel body wash bottle", "Baby body wash refill bag",
                     "Blue PVC printed wardrobe storage bags", "Hanging shoe bag",
                     "Laundry wash bags for clothes", "Mesh lingerie washbag",
                     "Disposable travel wash bags for body cleansing", "Makeup brush", "Toiletry bottles",
                     "Insulated lunch bag", "Tiffin carrying pouch"]
        self.assertEqual(self.shortlist(self.wash_bag(), unrelated), [])

    def test_wash_bag_retains_toiletry_cosmetic_pouch_and_common_washbag_aliases(self):
        candidates = ["Hanging travel wash bag", "Blue PVC travel wash bags set of two",
                      "Toiletry bag", "Toiletries carrying case", "Travel cosmetic pouches",
                      "Makeup travel pouch", "Dopp kit", "Dopp kits for travel",
                      "Portable washbag", "Wash bags", "Travel washing-items pouch",
                      "Travel wash pouch"]
        self.assertCountEqual(self.shortlist(self.wash_bag(), candidates), candidates)
        for label in ("Travel toiletry pouches", "Cosmetic carrying case", "Dopp kit", "Dopp kits"):
            target = {**self.wash_bag(), "label": label}
            self.assertEqual(self.shortlist(target, ["Toiletry bag", "Travel garbage bags"]), ["Toiletry bag"])

    def test_travel_word_cannot_turn_wash_refill_packaging_into_carrying_bag(self):
        packaging = ["Travel baby body wash refill bag", "Travel body wash refill bags",
                     "Travel shampoo wash refill pouch", "Travel face wash sachet bag",
                     "Hanging body wash refill bag", "Travel wash refill case",
                     "Travel washing liquid sachets in a bag"]
        self.assertEqual(self.shortlist(self.wash_bag(), packaging), [])

    def test_explicit_carrying_pouches_can_describe_the_refills_they_hold(self):
        containers = ["Travel cosmetic pouch for shampoo bottles",
                      "Travel cosmetic carrying pouch for shampoo refill sachets",
                      "Toiletry bag holds body wash refill sachets",
                      "Makeup travel pouch with a compartment for refills",
                      "Dopp kit for carrying shampoo bottles and refill sachets"]
        self.assertCountEqual(self.shortlist(self.wash_bag(), containers), containers)
        laundry = {"label": "Travel laundry wash bag", "function": "Protect laundry clothes during washing",
                   "features": []}
        self.assertEqual(self.shortlist(laundry, ["Mesh travel laundry wash bag"]), ["Mesh travel laundry wash bag"])

    def test_lunch_bag_does_not_admit_colour_print_storage_or_packaging_only(self):
        unrelated = ["Ezee Premium Garbage Bags for Dustbin Trash Bag",
                     "Shalimar Premium Scented Garbage Bags",
                     "BSB HOME Cotton Bedsheet Light Pink with Pillow Covers",
                     "Homestrap Non-Woven Printed Saree Cover Cloth Storage Wardrobe Organizer",
                     "Pink printed storage bags for wardrobe", "Blue lunch box",
                     "Steel tiffin food container", "Lunch plate", "Reusable grocery bags",
                     "Food packaging ziplock bags", "Disposable sandwich bags", "Pink tote bag",
                     "Toiletry bag", "Travel cosmetic pouch", "Laundry washbag"]
        self.assertEqual(self.shortlist(self.lunch_bag(), unrelated), [])

    def test_lunch_bag_keeps_lunch_tiffin_and_contextual_food_carriers(self):
        candidates = ["Pink and blue printed lunch bag set", "Insulated lunch bags",
                      "Tiffin carrying bag", "Lunch carrier", "Meal carrying pouch",
                      "Office food carrier", "Tiffin carrying case", "Cooler bag for food",
                      "Sandwich carrying pouch", "Lunchbox carrier bag"]
        self.assertCountEqual(self.shortlist(self.lunch_bag(), candidates), candidates)
        for label in ("Lunch bags", "Tiffin carrying pouch"):
            self.assertEqual(self.shortlist({**self.lunch_bag(), "label": label},
                                           ["Tiffin carrier", "Pink printed bedding"]), ["Tiffin carrier"])

    def test_other_bag_functions_keep_their_existing_retrieval(self):
        for target in ({"label": "Garbage bags", "function": "Collect rubbish", "features": []},
                       {"label": "Laundry wash bag", "function": "Protect laundry clothes during washing", "features": []},
                       {"label": "Travel laundry wash bag", "function": "Protect laundry clothes while washing", "features": []},
                       {"label": "Shoe carrying bag", "function": "Carry shoes", "features": []}):
            title = target["label"]
            self.assertEqual(self.shortlist(target, [title]), [title])

    def test_carrying_bag_alias_still_requires_model_relevance(self):
        concept = self.memory.snapshot()["concepts"][self.caddy]
        self.memory.put("concepts", self.caddy,
                        dict(concept, description={**concept["description"], **self.wash_bag()}), previous=concept)
        amazon(self.memory, ["Toiletry carrying case", "Baby body wash", "Garbage bags"])
        runner = RelevanceRunner("unclear")
        reviewed = review_marketplace(self.memory, runner, concept_ids=[self.caddy], as_of=NOW)
        self.assertEqual(len(runner.calls), 1)
        supplied = runner.calls[0]["items"][0]["observations"]
        self.assertEqual([o["source_text"]["title"] for o in supplied], ["Toiletry carrying case"])
        self.assertEqual(reviewed["accepted"][self.caddy]["relevance"][0]["relationship"], "unclear")

    def test_actual_chair_feature_words_do_not_admit_bedding_bags_or_fan(self):
        description = {"label": "HOKIPO Dining Chair Protector Covers", "function": "Cover and protect dining chairs.",
                       "mechanism": "Elastic slipcovers stretch over dining chairs.",
                       "features": ["Set of 4", "Elastic slipcovers", "Beige", "Model Ar-4097-D23*4"]}
        irrelevant = [
            "Homestrap Set of 6 Non-Woven Printed Saree Cover/Cloth Storage/Wardrobe Organizer",
            "BSB HOME 200 TC Cotton Feel Bedsheet for Double Bed with Two Pillow Covers",
            "GO WELL Solid Satin Silk Pillow Covers Pack of 2 with Satin Scrunchies",
            "Shalimar Premium Garbage Bags 120 Bags 4 Rolls",
            "atomberg Renesa 1200mm Ceiling Fan Set Model Ar LED Power Saving",
            "Waterproof elastic beige mattress protector stretch cover set of 4",
            "Dining table waterproof protector cover",
        ]
        relevant_candidates = ["Stretch dining chair slipcovers", "Unbranded dining seat protector",
                               "Seat cushion for dining chair"]
        self.assertCountEqual(self.shortlist(description, irrelevant + relevant_candidates), relevant_candidates)
        # Retrieval deliberately admits a chair cushion for final E judgment;
        # it does not certify that cushion and slipcover mechanisms are equal.

    def hanging_shelf(self):
        return {"label": "MONVO No-Drill Handwoven Wall Hanging Shelf",
                "function": "Provides a wall-mounted display shelf for plants and home decoration",
                "mechanism": "Hangs on the wall without drilling; suitable for plants and home decor",
                "features": ["No-drill wall hanging", "Handwoven", "Boho style"]}

    def test_hanging_shelf_requires_shelf_object_not_shared_wall_or_no_claims(self):
        unrelated = ["DALUCI Self Adhesive Wall Hooks for Hanging Without Drilling",
                     "Zulaxy Photo Frame Hooks for Wall Without Drilling",
                     "JIALTO Wall Hooks for Hanging Wall hangings Kitchen Accessories",
                     "Spoo No-Tear Formula Baby Shampoo By Torrent Pharma",
                     "Pampers Baby Diapers No Marks Design",
                     "Himalaya Baby Lotion No.1 Doctor-Prescribed Brand",
                     "Digital Clock for Home Decor", "Decorative plant wall painting"]
        self.assertEqual(self.shortlist(self.hanging_shelf(), unrelated), [])

    def test_hanging_shelf_keeps_shelves_and_contextual_display_rack_aliases(self):
        candidates = ["Handwoven hanging shelf for plants", "Floating wall shelves",
                      "Wooden shelf with hooks", "Floating shelves with metal mounting brackets",
                      "Hanging plant display rack", "Wall-mounted display rack", "Macrame wall rack"]
        self.assertCountEqual(self.shortlist(self.hanging_shelf(), candidates), candidates)
        ranked = self.shortlist(self.hanging_shelf(), ["Hanging plant display rack", "Floating wall shelves"])
        self.assertEqual(ranked[0], "Floating wall shelves")

    def test_shelf_accessories_are_not_the_actual_shelf(self):
        unrelated = ["Washable mat for drawers fridge and shelf liners",
                     "Non-slip shelf liner paper", "Shelf bracket", "Shelf hooks",
                     "Adhesive hooks for hanging wall shelves", "Metal brackets for wooden shelves",
                     "Laundry drying rack", "Handwoven hanging planter without shelf"]
        self.assertEqual(self.shortlist(self.hanging_shelf(), unrelated), [])

    def test_grammar_alone_cannot_be_a_primary_object_anchor(self):
        description = {"label": "No-drill picture frame", "function": "Mount without drilling by hand on a wall",
                       "features": []}
        unrelated = ["No-tear baby shampoo", "Diapers without marks", "Baby powder prescribed by doctors",
                     "Clothing worn on a sunny day", "Cream is soft and can be used by people"]
        self.assertEqual(self.shortlist(description, unrelated), [])
        self.assertEqual(self.shortlist(description, ["No-drill picture frame"]), ["No-drill picture frame"])

    def test_hanging_shelf_alias_is_still_a_model_question_not_accepted_relevance(self):
        state = self.memory.snapshot()
        concept = state["concepts"][self.caddy]
        self.memory.put("concepts", self.caddy, dict(concept, description={**concept["description"], **self.hanging_shelf()}), previous=concept)
        amazon(self.memory, ["Hanging plant display rack", "No-tear baby shampoo", "Wall hooks"])
        runner = RelevanceRunner("unclear")
        reviewed = review_marketplace(self.memory, runner, concept_ids=[self.caddy], as_of=NOW)
        self.assertEqual(len(runner.calls), 1)
        self.assertEqual(len(runner.calls[0]["items"][0]["observations"]), 1)
        self.assertEqual(reviewed["accepted"][self.caddy]["relevance"][0]["relationship"], "unclear")

    def oil_sprayer(self):
        return {"label": "Misamo Enterprise 2-in-1 Oil Sprayer & Dispenser",
                "function": "Stores and dispenses cooking oil by spraying or pouring",
                "mechanism": "A combined sprayer and pour dispenser",
                "features": ["500 ml capacity", "Black", "Leakproof design"]}

    def test_actual_oil_sprayer_retrieval_does_not_admit_oil_ingredients(self):
        target = "Misamo Enterprise 2-in-1 Oil Sprayer & Dispenser for Kitchen, 500ml, Black | Enjoy fine mist spraying and smooth pouring with a leakproof design for healthier, mess-free everyday cooking"
        unrelated = [
            "Himalaya Gentle Baby Powder with Olive Oil, Almond Oil and Yashad Bhasma",
            "Cetaphil Baby Mild Cleansing Bar with Shea Butter, Avocado Oil and Olive Oil",
            "Mother Sparsh Plant Powered Baby Laundry Liquid Detergent with Eucalyptus Oil",
            "Himalaya Gentle Baby Lotion with Almond Oil and Olive Oil",
            "Baby soap dispenser for olive oil cleansing liquid",
        ]
        self.assertEqual(self.shortlist(self.oil_sprayer(), unrelated + [target]), [target])

    def test_oil_sprayer_retrieval_keeps_practical_food_dispensing_aliases(self):
        candidates = ["Cooking oil spray bottle", "Oil sprayer and pour dispenser", "Olive oil pourer",
                      "Oil pouring bottle with spout", "Glass oil cruet for salad", "Olive misting bottle for cooking",
                      "Cooking oil atomizer", "Oil dispensing bottle"]
        self.assertCountEqual(self.shortlist(self.oil_sprayer(), candidates), candidates)

    def test_cooking_oil_sprayer_does_not_admit_explicit_other_oil_uses(self):
        unrelated = ["Engine oil dispenser for automotive lubrication", "Motor oil spray bottle",
                     "Essential oil misting spray bottle for aromatherapy", "Perfume oil atomizer",
                     "Hair oil sprayer", "Skin massage oil dispenser", "Engine oil can with pouring spout",
                     "Olive oil shampoo dispenser", "Olive oil cosmetic spray"]
        self.assertEqual(self.shortlist(self.oil_sprayer(), unrelated), [])
        # A dual-use listing explicitly supporting cooking remains a useful
        # candidate; retrieval does not decide its actual product relevance.
        mixed = "Essential oil sprayer also labelled for cooking oil and salad dressing"
        self.assertEqual(self.shortlist(self.oil_sprayer(), [mixed]), [mixed])

    def test_oil_mentions_in_other_product_labels_do_not_activate_dispenser_rule(self):
        description = {"label": "Oil-free air fryer", "function": "Cook food using hot air", "features": []}
        candidates = ["Air fryer oil-free cooking appliance", "Airfryer basket"]
        self.assertCountEqual(self.shortlist(description, candidates), candidates)
        engine = {"label": "Engine oil sprayer", "function": "Sprays engine oil for lubrication", "features": []}
        self.assertEqual(self.shortlist(engine, ["Engine oil sprayer"]), ["Engine oil sprayer"])
        fragrance = {"label": "Essential oil sprayer", "function": "Sprays stored oil", "features": []}
        self.assertEqual(self.shortlist(fragrance, ["Essential oil sprayer"]), ["Essential oil sprayer"])

    def test_oil_primary_anchor_is_candidate_retrieval_not_accepted_evidence(self):
        description = self.oil_sprayer()
        state = self.memory.snapshot()
        concept = state["concepts"][self.caddy]
        self.memory.put("concepts", self.caddy, dict(concept, description={**concept["description"], **description}), previous=concept)
        amazon(self.memory, ["Cooking oil pourer", "Baby powder with olive oil"])
        runner = RelevanceRunner("unrelated")
        reviewed = review_marketplace(self.memory, runner, concept_ids=[self.caddy], as_of=NOW)
        self.assertEqual(len(runner.calls), 1)
        self.assertEqual(len(runner.calls[0]["items"][0]["observations"]), 1)
        verdict = reviewed["accepted"][self.caddy]["relevance"]
        self.assertEqual(verdict[0]["relationship"], "unrelated")

    def test_renamed_sink_storage_candidates_use_object_and_storage_context(self):
        description = {"label": "Sink organiser", "function": "Store cleaning sponges beside the sink", "features": ["Steel"]}
        candidates = ["Dish sponge holder", "Soap storage basket", "Basin caddy", "Sink organiser", "Kitchen sponge tray"]
        unrelated = ["Steel wardrobe organizer", "Cleaning sponge", "Sink drain-cleaning chemical", "Cotton storage rack"]
        self.assertCountEqual(self.shortlist(description, candidates + unrelated), candidates)

    def test_air_fryer_spelling_and_named_appliance_aliases_remain_plausible(self):
        description = {"label": "Airfryer paper liners", "function": "Line an air fryer basket", "features": ["Silicone", "Waterproof"]}
        candidates = ["Air-fryer basket paper liner", "Disposable liners for hot air cooker", "Airfryer liner"]
        unrelated = ["Waterproof baking paper", "Silicone sink basket", "Plastic laundry basket liner"]
        self.assertCountEqual(self.shortlist(description, candidates + unrelated), candidates)

    def test_primary_head_outweighs_material_count_and_function_renaming_is_allowed(self):
        description = {"label": "Mini whisk", "function": "Mix and froth milk", "features": ["Silicone", "Black", "Model 123"]}
        candidates = ["Silicone black whisk model 123", "Milk froth wand", "Black silicone spoon model 123"]
        selected = self.shortlist(description, candidates)
        self.assertEqual(selected, candidates[:2])

    def test_feature_only_numeric_matches_cannot_admit_candidates(self):
        description = {"label": "Unbranded phone stand", "function": "Hold phone", "features": ["Set of 4", "Model Ar-4097", "Beige"]}
        self.assertEqual(self.shortlist(description, ["Set of 4 beige pillow cases Model Ar-4097"]), [])

    def test_cleaning_target_is_not_mistaken_for_primary_product(self):
        description = {"label": "Powered cleaning brush", "function": "Clean chairs", "features": ["Waterproof"]}
        self.assertEqual(self.shortlist(description, ["Powered cleaning brush", "Dining chair", "Air fryer"]),
                         ["Powered cleaning brush"])

    def test_planter_stand_shape_material_and_support_are_not_product_anchors(self):
        description = {"label": "Metal Z-shape/X-shape planter stand",
                       "function": "Raises and displays a plant pot.",
                       "mechanism": "A freestanding metal stand supports a planter above the floor or ground.",
                       "features": ["Black metal Z-shape/X-shape design", "Strong legs", "40 cm"]}
        unrelated = ["CRAE Digital Weighing Scale 10Kg x 1g LCD Display",
                     "Presto Garbage Bags 30 Bags X 6 Rolls 17 X 19 Inches",
                     "Ganesh Metal Gas Stove Lighter",
                     "Hipkoo Washing Machine Stand with Heavy Duty Steel Trolley",
                     "Flower pot without a support", "Black metal stand 40 cm"]
        candidates = ["Metal planter stand", "Flower pot stand", "Plant support rack"]
        self.assertCountEqual(self.shortlist(description, unrelated + candidates), candidates)

    def test_swing_chain_ignores_s_units_and_ordinary_wall_hooks(self):
        description = {"label": "Q1 Beads 3 Feet Metal Chain with S Hook",
                       "function": "Suspends a garden swing or jhula",
                       "mechanism": "A heavy-duty metal chain and included S hook provide a hanging connection",
                       "features": ["3 feet long", "7 mm chain", "Rated capacity of 300 kg", "Chrome finish"]}
        unrelated = ["Little's Premium Baby Diapers XL Size Pants Style",
                     "LuvLap Laundry Detergent Safe for mommy's hands & baby's skin",
                     "JIALTO Adhesive Hooks for Wall Heavy Duty Hanging",
                     "DALUCI Self Adhesive Wall Hooks 20 Pcs", "Metal heavy duty hanging accessory"]
        candidates = ["Metal swing chain with S hook", "Jhula hanging hooks", "Garden swing hooks"]
        self.assertCountEqual(self.shortlist(description, unrelated + candidates), candidates)

    def test_rice_drainer_admits_straining_objects_not_generic_liquid_or_stands(self):
        description = {"label": "URBANMONK Stainless Steel Rice Pot Drainer Stand",
                       "function": "Drains liquid from rice and other foods",
                       "mechanism": "A tabletop stand supports the drainer so food can drain hands-free",
                       "features": ["Stainless steel", "Tabletop design", "Hands-free draining"]}
        unrelated = ["AGARO Royal Milk Mixer Stand-up Design Stainless Steel",
                     "Hipkoo Washing Machine Stand with Wheels Steel Trolley",
                     "Mee Mee Baby Liquid Detergent Free from Harsh Chemicals Safe for Mother's Hands",
                     "URBANMONK Stainless Steel Bottle"]
        candidates = ["Rice pot drainer stand", "Stainless steel colander", "Food strainer for cooked rice"]
        self.assertCountEqual(self.shortlist(description, unrelated + candidates), candidates)

    def test_table_mats_keep_real_mat_objects_not_table_clock_surface(self):
        description = {"label": "SARAL Home Jute and Cotton Crochet Antiskid Round Table Mats",
                       "function": "Serve as decorative placemats or surface mats for tables and shelves.",
                       "mechanism": "Crocheted jute and cotton mats cover a surface; the title describes them as antiskid.",
                       "features": ["Round", "Set of 4", "38 x 38 cm", "Grey"]}
        candidates = ["Round crochet table mats", "Cotton mats for dining table", "Round placemats"]
        unrelated = ["AERYS Digital Alarm Clock Desk Table Clock for Students",
                     "Round table clock grey set of 4", "Kitchen tabletop stand 38 x 38 cm",
                     "SARAL Home Jute Round Clock", "Anti-slip mat for drawer and shelf liner"]
        self.assertCountEqual(self.shortlist(description, unrelated + candidates), candidates)

    def test_anti_slip_feature_does_not_admit_anti_rash_or_antibacterial_goods(self):
        description = {"label": "HomeCloud TPE Anti-Slip Shower Mat",
                       "function": "Provides a grippy surface for standing in the shower",
                       "mechanism": "Suction cups attach the mat to the shower surface",
                       "features": ["TPE", "Anti-slip", "Suction cups", "Grey"]}
        candidates = ["Anti-slip shower mat with suction cups", "Shower mat", "Non-slip bath rug"]
        unrelated = ["Pampers Diapers with Anti Rash Shield", "Anti-Bacterial Baby Liquid Detergent",
                     "Hipkoo Washing Machine Stand Anti-Vibration Pads", "Anti-slip mat for drawer and shelf liner"]
        self.assertCountEqual(self.shortlist(description, unrelated + candidates), candidates)

    def test_empty_shortlist_retains_absence_limitation_without_model_padding(self):
        amazon(self.memory, ["Metal kitchen stand 40 cm", "Anti bacterial detergent set of 4"])
        runner = RelevanceRunner()
        result = review_marketplace(self.memory, runner, concept_ids=[self.caddy], as_of=NOW)
        self.assertEqual(runner.calls, [])
        self.assertEqual(result["accepted"], {})
        self.assertIn("absence is not proof", result["deferred"][0]["limitation"])

    def test_invalid_reference_does_not_discard_valid_sibling(self):
        amazon(self.memory, ["Sink caddy", "Powered cleaning brush"])
        result = review_marketplace(self.memory, RelevanceRunner(fail_first=True), as_of=NOW)
        self.assertEqual(len(result["failures"]), 1)
        self.assertEqual(len(result["accepted"]), 1)

    def test_finished_first_concept_does_not_starve_next_bounded_batch(self):
        amazon(self.memory, ["Sink caddy", "Powered cleaning brush"])
        runner = RelevanceRunner()
        first = review_marketplace(self.memory, runner, limit=1, as_of=NOW)
        second = review_marketplace(self.memory, runner, limit=1, as_of=NOW)
        self.assertEqual(len(first["accepted"]), 1)
        self.assertEqual(len(second["accepted"]), 1)
        self.assertTrue(set(first["accepted"]).isdisjoint(second["accepted"]))
        third = review_marketplace(self.memory, runner, limit=1, as_of=NOW)
        self.assertEqual(len(runner.calls), 2)
        self.assertEqual(len(third["reused"]), 2)

    def test_description_revision_rechecks_relevance_with_new_input(self):
        amazon(self.memory)
        runner = RelevanceRunner()
        review_marketplace(self.memory, runner, concept_ids=[self.caddy], as_of=NOW)
        old = self.memory.snapshot()["concepts"][self.caddy]
        changed = {**old, "description": {**old["description"], "reason": "Revised interpretation."}}
        self.memory.put("concepts", self.caddy, changed, previous=old)
        review_marketplace(self.memory, runner, concept_ids=[self.caddy], as_of=NOW)
        self.assertEqual(len(runner.calls), 2)

    def test_import_helper_rejects_non_amazon_before_import(self):
        fake_journal = self.memory.journal
        prepared = request("trends", None, "fake-source")
        atomic_json(fake_journal.root / "fake-source" / "prepared.json", prepared.to_dict())
        with patch.object(fake_journal, "state", return_value=({}, {"fake-source": {"capture": "synthetic"}})):
            with self.assertRaisesRegex(SafetyError, "SAVED_AMAZON"):
                import_saved_marketplace(self.memory, "fake-source")

    def test_saved_amazon_import_reuses_committed_bytes_and_preserves_prices(self):
        prepared = request(attempt="amazon-import")
        values = [amazon_row()]
        mapped = map_research_rows(values, prepared)
        journal = self.memory.journal
        folder = journal.root / "amazon-import"
        atomic_json(folder / "prepared.json", prepared.to_dict())
        atomic_json(folder / "captured_rows.json", values)
        atomic_json(folder / "mapped_rows.json", mapped)
        atomic_json(folder / "capture_manifest.json", {"captured_hash": digest(values), "dataset_id": "saved-dataset", "observed_at": NOW})
        attempt = {"capture": digest(values), "dataset_id": "saved-dataset", "config_hash": prepared.config_hash}
        captured = ResearchMemory(Path(self.temp.name) / "capture-contract-fixture", journal)
        with patch.object(journal, "state", return_value=({}, {"amazon-import": attempt})):
            first = import_saved_marketplace(captured, "amazon-import")
            second = import_saved_marketplace(captured, "amazon-import")
        self.assertEqual(first["new_observations"], 1)
        self.assertTrue(second["reused_capture"])
        observation = next(iter(captured.snapshot()["observations"].values()))
        self.assertEqual(observation["observed_at"], NOW)
        self.assertEqual(observation["facts"]["price"]["displayed"], "₹249.00")

    def test_unknown_concept_and_invalid_limits_fail_without_model(self):
        for kwargs in ({"concept_ids": ["unknown"]}, {"limit": 0}, {"observations_per_concept": 11}, {"concept_ids": [{}]}):
            with self.subTest(kwargs=kwargs), self.assertRaises(SafetyError):
                review_marketplace(self.memory, RelevanceRunner(), as_of=NOW, **kwargs)


if __name__ == "__main__":
    unittest.main()
