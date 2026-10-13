"""One semantic chart correction cannot erase sources or block other work."""

from copy import deepcopy
from pathlib import Path
import tempfile
import unittest

from test_research_flow import FakeRunner, capture
from test_trends_browser import metadata, weekly
from product_radar.market_followups import review_trends_scope
from product_radar.models import SafetyError, digest
from product_radar.research_flow import candidate_summary, run_flow, trends_description_current
from product_radar.research_memory import ResearchMemory
from product_radar.research_operations import import_trends_evidence, missing_market_checks
from product_radar.storage import Journal

NOW = "2026-10-06T12:00:00+00:00"


class DecisionScopeTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.memory = ResearchMemory(Path(self.temp.name) / "research", Journal(Path(self.temp.name) / "money"), evidence_type="synthetic")
        capture(self.memory)
        run_flow(self.memory, FakeRunner(), as_of=NOW)
        self.concept = next(iter(self.memory.snapshot()["concepts"]))
        self.evidence_id = import_trends_evidence(self.memory, concept_id=self.concept,
            acquisition_id="synthetic-chart", csv_bytes=weekly(), metadata=metadata())["id"]

    def review(self, **changes):
        state = self.memory.snapshot()
        return {"evidence_id": self.evidence_id, "expected_evidence_hash": digest(state["trends_evidence"][self.evidence_id]),
                "expected_description_hash": digest(state["concepts"][self.concept]["description"]),
                "relationship": "unrelated", "reason": "The chart describes a different practical product use.",
                "reviewer": "fixture_owner", "reviewed_at": NOW, **changes}

    def test_scope_overlay_removes_only_wrong_support_not_the_record(self):
        before = self.memory.snapshot()
        prior = next(row for row in candidate_summary(self.memory, as_of=NOW)["products"] if row["id"] == self.concept)
        self.assertTrue(prior["market"]["families"]["trends"]["available"])
        review_trends_scope(self.memory, self.review())
        after = self.memory.snapshot()
        product = next(row for row in candidate_summary(self.memory, as_of=NOW)["products"] if row["id"] == self.concept)
        self.assertFalse(product["market"]["families"]["trends"]["available"])
        self.assertEqual(before["trends_evidence"], after["trends_evidence"])
        self.assertEqual(before["observations"], after["observations"])
        self.assertEqual(before["assessments"], after["assessments"])
        self.assertTrue(any(row["id"] != self.concept for row in candidate_summary(self.memory, as_of=NOW)["products"]))

    def test_exact_scope_review_replay_is_no_new_event(self):
        review = self.review()
        self.assertTrue(review_trends_scope(self.memory, review)["applied"])
        state = self.memory.snapshot()
        self.assertFalse(review_trends_scope(self.memory, review)["applied"])
        self.assertEqual(state, self.memory.snapshot())

    def test_later_scope_review_does_not_rewrite_an_earlier_view(self):
        review_trends_scope(self.memory, self.review())
        before_review = "2026-10-06T11:00:00+00:00"
        product = next(row for row in candidate_summary(self.memory, as_of=before_review)["products"] if row["id"] == self.concept)
        self.assertTrue(product["market"]["families"]["trends"]["available"])
        current = next(row for row in candidate_summary(self.memory, as_of=NOW)["products"] if row["id"] == self.concept)
        self.assertFalse(current["market"]["families"]["trends"]["available"])

    def test_scope_correction_is_not_a_failed_provider_route(self):
        review_trends_scope(self.memory, self.review())
        state = self.memory.snapshot()
        tasks = missing_market_checks(candidate_summary(self.memory, as_of=NOW), as_of=NOW,
                                      source_checks=state["source_checks"].values())
        trends = [choice for task in tasks for choice in task["choices"] if choice["source"] == "trends"]
        self.assertTrue(trends)
        self.assertTrue(all(choice["route_state"] == "not_tested_for_this_question" for choice in trends))

    def test_changed_evidence_and_pre_capture_review_are_refused(self):
        before = self.memory.snapshot()
        for updates, code in (({"expected_evidence_hash": "0" * 64}, "DEPENDENCY_CHANGED"),
                              ({"expected_description_hash": "0" * 64}, "DEPENDENCY_CHANGED"),
                              ({"reviewed_at": "2026-10-05T12:00:00+00:00"}, "PREDATES_EVIDENCE")):
            with self.subTest(updates=updates), self.assertRaisesRegex(SafetyError, code):
                review_trends_scope(self.memory, self.review(**updates))
        self.assertEqual(before, self.memory.snapshot())


if __name__ == "__main__":
    unittest.main()
