"""Reviewed seed admission and saved-query behavior; isolated fixtures only."""

from copy import deepcopy
import unittest

import test_empty_query_refinement as empty_fixtures
from test_research_planner import NOW, outcome
from product_radar.catalogue import alias_groups, catalogue_summary, load_catalogue
from product_radar.models import digest
from product_radar.query_dispatch import find_existing_task
from product_radar.research_operations import bind_prepared_action, plan_research
from product_radar.research_planner import plan_searches


CABINET = "bb-childproof-cabinet-locks"
BAG = "to-travel-toiletry-bags"


class CabinetCatalogueAdmissionTests(unittest.TestCase):
    def setUp(self):
        self.catalogue = load_catalogue()

    def family(self, family_id):
        return next(f for f in self.catalogue["families"] if f["id"] == family_id)

    def primary(self, family_id):
        return plan_searches(self.catalogue, [], as_of=NOW, slots=1,
                             coverage={"limit": 5}, urgent_family_ids=[family_id])["actions"][0]

    def test_one_same_family_hypothesis_has_checked_retailer_wording(self):
        summary = catalogue_summary(self.catalogue)
        self.assertEqual(summary["families"], 224)
        self.assertEqual(summary["distinct_usable_families"], 224)
        self.assertEqual(summary["alias_overlap_groups"], [])
        self.assertEqual(summary["categories"], {c: 56 for c in summary["categories"]})
        self.assertEqual(summary["observed"], 103)
        self.assertEqual(summary["plausible_extensions"], 121)
        groups = alias_groups(self.catalogue)
        self.assertEqual(len(set(groups.values())), 224)
        family = self.family(CABINET)
        meta = [p for p in family["phrases"] if p["source"] == "meta"]
        self.assertEqual([p["text"] for p in meta], ["childproof drawer latch", "cabinet lock"])
        self.assertTrue(all(p["purpose"] == "discovery" and p["language"] == "en"
                            and p["country"] == "IN" for p in meta))
        self.assertIn("Reviewed natural common-name search hypothesis", meta[1]["reason"])
        self.assertIn("verified-empty", meta[1]["reason"])
        self.assertEqual(family["included"], ["childproof drawer latch"])
        self.assertEqual(family["aliases"], [])
        self.assertEqual(family["basis"], "plausible_extension")
        self.assertEqual(family["examples"], [{"label": "childproof drawer latch",
                         "basis": "plausible_extension", "sources": []}])
        source = next(s for s in self.catalogue["sources"] if s["id"] == "firstcry-child-safety-locks")
        self.assertIn(source["id"], family["sources"])
        self.assertEqual(source["excerpts"], ["Child Safety Locks", "Cabinet lock"])
        self.assertEqual(source["checked_at"], "2026-10-06T23:48:57Z")
        self.assertTrue(source["url"].startswith("https://www.firstcry.com/childproofing-and-safety/"))

    def test_cold_start_preserves_primary_not_alternative(self):
        self.assertEqual(self.primary(CABINET)["phrase"], "childproof drawer latch")
        self.assertEqual(self.primary(BAG)["phrase"], "hanging travel wash bag")

    def test_verified_empty_uses_one_cabinet_alternative_and_preserves_frozen_request(self):
        # Reuse only the existing isolated provider-diagnostic fixture seam.
        fixture = empty_fixtures.EmptyRefinementTests()
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        fixture.data = deepcopy(self.catalogue)
        fixture.original = plan_research(fixture.memory, fixture.data, as_of=NOW, slots=1,
                                        urgent_family_ids=[CABINET])
        fixture.action = fixture.original["actions"][0]
        fixture.prepared = fixture.prepare(fixture.action, "fixture-cabinet-primary")
        fixture.task = bind_prepared_action(fixture.memory, fixture.original["id"],
                                            fixture.action["action_id"], fixture.prepared)
        frozen_plan = deepcopy(fixture.original)
        frozen_task = deepcopy(fixture.task)
        fixture.commit()
        with fixture.financial():
            empty_fixtures.sync_query_feedback(fixture.memory, as_of=NOW)
        base = next(o for o in fixture.memory.snapshot()["query_outcomes"].values()
                    if o["acquisition_id"] == "dataset-fixture-cabinet-primary")
        plan = fixture.refine()
        action = plan["actions"][0]
        self.assertEqual(action["family_id"], CABINET)
        self.assertEqual(action["phrase"], "cabinet lock")
        self.assertEqual(action["refinement"]["base_capture_hash"], base["empty_result"]["capture_hash"])
        self.assertEqual(action["refinement"]["base_request_key"], base["empty_result"]["request_key"])
        self.assertEqual(fixture.memory.snapshot()["batch_plans"][frozen_plan["id"]], frozen_plan)
        self.assertEqual(fixture.memory.snapshot()["query_tasks"][frozen_task["id"]]["request"], frozen_task["request"])
        prepared = fixture.prepare(action, "fixture-cabinet-alternative")
        saved = bind_prepared_action(fixture.memory, plan["id"], action["action_id"], prepared)
        with fixture.financial():
            replay = find_existing_task(fixture.memory, action, as_of=NOW)
        self.assertEqual(replay["attempt_id"], prepared.attempt_id)
        self.assertEqual(replay["reuse_kind"], "active_task")
        self.assertEqual(saved, bind_prepared_action(fixture.memory, plan["id"], action["action_id"], prepared))
        self.assertTrue(all(a["family_id"] != CABINET for a in fixture.refine()["actions"]))
        self.assertEqual(digest(frozen_task["request"]), digest(fixture.memory.snapshot()["query_tasks"][frozen_task["id"]]["request"]))

    def test_nonempty_bag_keeps_existing_single_discovery_phrase(self):
        meta = [p["text"] for p in self.family(BAG)["phrases"] if p["source"] == "meta"]
        self.assertEqual(meta, ["hanging travel wash bag"])
        action = self.primary(BAG)
        for reviewed in (False, True):
            previous = outcome(action, reviewed=reviewed, resolved_count=5, known_count=0)
            plan = plan_searches(self.catalogue, [previous], as_of=NOW, slots=1,
                                 coverage={"limit": 5}, urgent_family_ids=[BAG])
            self.assertEqual(plan["actions"][0]["phrase"], "hanging travel wash bag")
            self.assertNotIn("refinement", plan["actions"][0])


if __name__ == "__main__":
    unittest.main()
