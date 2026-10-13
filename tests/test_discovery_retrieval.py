"""Offline retrieval hints, not proof of semantic identity or real demand."""

from copy import deepcopy
import unittest

from product_radar.discovery import find_possible_matches, meaningful_tokens, tokens


def product(label, function, mechanism="Not stated", audience="People furnishing a home"):
    return {"label": label, "function": function, "mechanism": mechanism,
            "audience": audience, "category": "Home and Kitchen",
            "basis": "observed", "brand": "generic_or_small", "features": [],
            "reason": "Synthetic text based on the inspected retrieval boundary."}


def profile():
    return product("Pack of 5 black PVD-coated stainless steel kitchen profiles",
                   "Kitchen profile for kitchen fitting or finishing",
                   "A stainless steel profile fitted as part of a kitchen installation; the exact placement is not specified.",
                   "People furnishing or fitting out a kitchen")


def handle():
    return product("5-Inch Stainless Steel D-Curve Handle Set, 12 Pieces",
                   "Provide a grip for opening doors, cabinets, or windows",
                   "The handles attach to a door, cabinet, or window and provide a gripping point.",
                   "People fitting or replacing handles on home doors, cabinets, or windows")


class DiscoveryRetrievalTests(unittest.TestCase):
    def concepts(self, **values):
        return {i: {"description": row, "status": "provisional"} for i, row in values.items()}

    def test_weak_material_pack_grammar_and_category_do_not_admit(self):
        target = profile()
        values = self.concepts(
            tray=product("Stainless steel kitchen tray", "Place kitchen jewellery or ornaments"),
            grinder=product("Stainless steel grinder", "Grind spices or coffee"),
            handle=handle())
        before = deepcopy(values)
        self.assertEqual(find_possible_matches(target, values), {"candidates": [], "omitted": 0})
        self.assertEqual(values, before)

    def test_general_tokens_and_source_description_are_not_rewritten(self):
        value = "Pack of 5 black PVD-coated stainless steel kitchen profiles or handles"
        self.assertIn("or", tokens(value))
        self.assertIn("steel", tokens(value))
        self.assertEqual(meaningful_tokens(value), {"profiles", "handles"})
        self.assertEqual(value, "Pack of 5 black PVD-coated stainless steel kitchen profiles or handles")

    def test_true_handle_variants_remain_across_material_pack_and_category(self):
        candidate = handle()
        candidate["category"] = "Home and Decor"
        target = product("Black cabinet pull, pack of 5", "Grip cabinet doors for opening",
                         "Attach a pull to cabinet doors")
        found = find_possible_matches(target, self.concepts(handle=candidate))
        self.assertEqual([row["id"] for row in found["candidates"]], ["handle"])
        self.assertIn("functional_overlap", found["candidates"][0]["retrieval_reasons"])

    def test_fitting_only_does_not_admit_riding_jacket(self):
        jacket = product("Street mesh riding jacket", "Provide protection with adjustable fitting",
                         "A jacket fitted with mesh panels", "People fitting riding apparel")
        self.assertEqual(find_possible_matches(profile(), self.concepts(jacket=jacket))["candidates"], [])

    def test_audience_only_and_single_general_function_word_are_not_anchors(self):
        target = product("Utensil caddy", "Store utensils", audience="adult")
        other = product("Luggage trunk", "Store clothing", audience="adult")
        self.assertFalse(find_possible_matches(target, self.concepts(other=other))["candidates"])

    def test_generic_exact_label_function_mechanism_do_not_admit(self):
        target = product("Stainless steel", "For kitchen use", "Not stated", "adult")
        self.assertFalse(find_possible_matches(target, self.concepts(other=deepcopy(target)))["candidates"])

    def test_exact_meaningful_fields_keep_existing_weights(self):
        target = product("Caddy", "Store utensils", "Drain holes", "adult")
        row = find_possible_matches(target, self.concepts(other=deepcopy(target)))["candidates"][0]
        self.assertEqual(row["retrieval_score"], 6 + 4 + 3 + 1 + 1 + 2)
        self.assertEqual(row["retrieval_reasons"], ["label", "function", "mechanism", "audience",
                                                  "label_overlap", "functional_overlap"])

    def test_repeated_shared_label_words_do_not_count_as_functional_overlap(self):
        target = product("Napkin holder", "Hold napkin", "Fixed napkin support")
        other = product("Napkin stand", "Display napkin", "Upright napkin frame")
        row = find_possible_matches(target, self.concepts(other=other))["candidates"][0]
        self.assertEqual(row["retrieval_reasons"], ["audience", "label_overlap"])
        self.assertEqual(row["retrieval_score"], 2)

    def test_meaningful_ranking_beats_material_ties(self):
        target = product("Black stainless steel napkin holder", "Hold folded napkins", "Upright holder")
        values = self.concepts(
            a=product("Black stainless steel napkin tray", "Display napkins", "Flat tray"),
            z=product("Wooden napkin holder", "Hold folded napkins", "Upright holder"))
        self.assertEqual(find_possible_matches(target, values)["candidates"][0]["id"], "z")

    def test_weak_attributes_only_break_already_admitted_ties(self):
        target = product("Black steel napkin holder", "Hold napkins")
        values = self.concepts(a=product("Pink wood napkin stand", "Display napkins"),
                               z=product("Black steel napkin stand", "Display napkins"))
        found = find_possible_matches(target, values)
        self.assertEqual([row["id"] for row in found["candidates"]], ["z", "a"])
        self.assertEqual(found["candidates"][0]["retrieval_score"], found["candidates"][1]["retrieval_score"])

    def test_grammar_does_not_break_already_admitted_ties(self):
        target = product("Napkin holder", "Serve napkins or arrange table")
        values = self.concepts(a=product("Napkin stand", "Display napkins dining table"),
                               z=product("Napkin stand", "Display napkins or dining table"))
        found = find_possible_matches(target, values)
        self.assertEqual([row["id"] for row in found["candidates"]], ["a", "z"])
        self.assertEqual(found["candidates"][0]["retrieval_score"], found["candidates"][1]["retrieval_score"])

    def test_large_history_is_stable_bounded_and_reports_exact_omissions(self):
        target = handle()
        values = {"handle-" + str(n).zfill(3): {"description": deepcopy(target)} for n in range(800)}
        values.update(self.concepts(tray=product("Steel tray", "Display kitchen ornaments")))
        found = find_possible_matches(target, values, limit=3)
        self.assertEqual(len(found["candidates"]), 3)
        self.assertEqual(found["omitted"], 797)
        self.assertEqual(found, find_possible_matches(target, dict(reversed(list(values.items()))), limit=3))

    def test_inactive_candidates_still_do_not_crowd_out_current_product(self):
        target = handle()
        values = {status: {"description": deepcopy(target), "status": status}
                  for status in ("merged", "retired", "provisional", "excluded")}
        self.assertEqual({row["id"] for row in find_possible_matches(target, values)["candidates"]},
                         {"provisional", "excluded"})


if __name__ == "__main__":
    unittest.main()
