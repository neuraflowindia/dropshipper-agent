"""Research trace, one product type's journey in time order (synthetic data)."""

from pathlib import Path
import tempfile
import unittest

import trace_fixture as fx
from product_radar.trace_product import find_types, journey, type_name
from product_radar.trace_records import load_store
from product_radar.trace_text import product_text


class TraceProductTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp = tempfile.TemporaryDirectory()
        root = Path(cls.temp.name) / "store"
        fx.build(root)
        cls.store = load_store(root)
        cls.judged = fx.judged()

    @classmethod
    def tearDownClass(cls):
        cls.temp.cleanup()

    def test_find_by_id_name_or_part_of_id(self):
        self.assertEqual(find_types(self.store, fx.A), [fx.A])
        self.assertEqual(find_types(self.store, "RACK"), [fx.A])
        self.assertEqual(find_types(self.store, "test"), [fx.A, fx.B, fx.C, "concept-old"])
        self.assertEqual(find_types(self.store, "hook"), [fx.D])
        self.assertEqual(find_types(self.store, "concept-a"), [fx.A])
        self.assertEqual(find_types(self.store, "no such thing"), [])
        self.assertEqual(type_name(self.store.state, fx.B), "Quiet test lamp")

    def test_report_ready_type_journey(self):
        report = journey(self.store, self.judged, fx.A)
        self.assertEqual((report["name"], report["first_seen"], report["status"]), ("Folding test rack", fx.T1, "provisional"))
        origin = report["origin"]
        self.assertEqual(origin["created_by_listing"], "item-2")
        self.assertIsNone(origin["created_by_heading_search"])
        listing = origin["listings"][0]
        self.assertEqual((listing["source"], listing["created_type"], listing["search_words"], listing["lane"]),
                         ("deodap", True, "rack", "lists"))
        self.assertEqual([row["job"] for row in report["ai_jobs"]], ["job-a", "job-c"])
        self.assertEqual([row["point"] for row in report["checklist"]], ["Small in size", "Easy to ship"])
        self.assertEqual(report["checklist_meets"], 7)
        questions = {row["task_id"]: row for row in report["questions"]}
        self.assertEqual(sorted(questions), ["task-1", "task-3", "task-4"])
        self.assertEqual([change["status"] for change in questions["task-1"]["changes"]],
                         ["prepared", "dispatching", "acquired"])
        self.assertEqual(questions["task-1"]["answer_for_this_type"], "interpretation_pending")
        self.assertEqual(report["crowding"][0]["level"], "high")
        self.assertEqual(report["supplier"]["supplier_leads_found"], 1)
        self.assertEqual((report["score"]["total"], report["score"]["label"]), (52, "sweet_spot"))
        self.assertEqual(report["score"]["parts"]["room"]["points"], 25)
        self.assertTrue(report["report_ready"])
        self.assertEqual(report["allocations"], ["reservation-1"])
        self.assertEqual([row["stage"] for row in report["problems"]], ["fit_review"])
        kinds = [row["kind"] for row in report["timeline"]]
        self.assertNotIn("research-programme-v1", kinds)
        self.assertEqual([row["at"] for row in report["timeline"]], sorted(row["at"] for row in report["timeline"]))
        self.assertEqual(report["timeline_lines"], len(report["timeline"]))
        text = product_text(report)
        self.assertIn("Report-ready: YES", text)
        self.assertIn("Small in size: Meets", text)
        self.assertIn("Sweet spot [sweet_spot]", text)

    def test_type_created_by_a_heading_search(self):
        report = journey(self.store, self.judged, fx.C)
        wave = report["origin"]["created_by_heading_search"]
        self.assertEqual((wave["wave"], wave["routes"], wave["headings"]),
                         ("discovery-wave-1", ["google-trends", "meta-ads"], ["fam-a", "fam-b", "fam-c"]))
        self.assertFalse(report["report_ready"])
        self.assertEqual(report["codes"], ["crowding_pending", "supplier_search_required"])
        text = product_text(report)
        self.assertIn("Created by a heading search", text)
        self.assertIn("Not ready because: Waiting for the Amazon/Flipkart crowding check", text)

    def test_linked_listing_keeps_its_heading_and_search_words(self):
        report = journey(self.store, self.judged, fx.B)
        listing = report["origin"]["listings"][0]
        self.assertEqual((listing["heading"], listing["search_words"], listing["created_type"]),
                         ("fam-a", "folding rack", False))
        self.assertEqual(listing["title"], "Invented folding rack advert")
        self.assertEqual(report["score"]["label"], "too_quiet")

    def test_type_the_ai_judged_new_is_credited_to_its_listing(self):
        origin = journey(self.store, self.judged, fx.D)["origin"]
        self.assertEqual(origin["created_by_listing"], "item-3")
        self.assertEqual([(row["source"], row["created_type"]) for row in origin["listings"]], [("amazon", True)])

    def test_journey_without_a_pool(self):
        report = journey(self.store, {"pool": None, "meter": None, "errors": {}}, fx.A)
        self.assertEqual((report["checklist"], report["score"]["total"], report["report_ready"]), ([], None, False))
        self.assertIn("Score: - of 100", product_text(report))


if __name__ == "__main__":
    unittest.main()
