"""Explicit source-bound brand corrections, with no live model or collection."""

from copy import deepcopy
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from test_research_flow import FakeRunner, NOW, capture
from product_radar.brand_reviews import apply_brand_review, brand_binding, current_brand, prepare_brand_review
from product_radar.fit_policy import FIT_POLICY_ID
from product_radar.models import SafetyError, digest
from product_radar.research_flow import candidate_summary, run_flow
from product_radar.research_memory import ResearchMemory, change, key
from product_radar.storage import Journal
from test_fit_review import FitRunner


class EstablishedRunner(FitRunner):
    def __call__(self, role, payload, folder):
        response, usage = super().__call__(role, payload, folder)
        if role == "A":
            for result in response["results"]:
                for product in result["products"]:
                    if product["label"] == "Sink caddy":
                        product["brand"] = "established"
        return response, usage


class BrandReviewTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.memory = ResearchMemory(Path(self.temp.name) / "research", Journal(Path(self.temp.name) / "money"), evidence_type="synthetic")
        self.runner = EstablishedRunner()
        capture(self.memory)
        run_flow(self.memory, self.runner, fit_policy_id=FIT_POLICY_ID, as_of=NOW)
        self.child_id = next(i for i, row in self.memory.snapshot()["associations"].items() if row.get("reason") == "established_brand")

    def request(self, kind="child", target_id=None, verdict="generic_or_small", **changes):
        state = self.memory.snapshot()
        target_id = target_id or self.child_id
        semantics = ({state["associations"][target_id]["semantic_id"]} if kind == "child"
                     else {a["semantic_id"] for a in state["associations"].values() if a.get("concept_id") == target_id})
        refs = [i for i, row in state["observations"].items() if row["semantic_id"] in semantics]
        return prepare_brand_review(state, kind, target_id, verdict=verdict,
            reason="The named store is the seller; this is a generic manufactured tool.",
            evidence_ids=refs, reviewer="fixture_owner", reviewed_at=NOW, **changes)

    def test_child_correction_reopens_only_brand_exclusion_and_reaches_new_concept(self):
        before = self.memory.snapshot()
        request = self.request()
        applied = apply_brand_review(self.memory, request)
        pending = self.memory.snapshot()
        self.assertEqual(pending["associations"][self.child_id]["status"], "brand_review_pending")
        self.assertEqual(pending["descriptions"], before["descriptions"])
        summary = run_flow(self.memory, self.runner, fit_policy_id=FIT_POLICY_ID, as_of=NOW)
        state = self.memory.snapshot()
        link = state["associations"][self.child_id]
        self.assertEqual(link["status"], "resolved")
        concept_id = link["concept_id"]
        concept = state["concepts"][concept_id]
        self.assertEqual(concept["description"]["brand"], "established")
        self.assertEqual(concept["origin_child_id"], self.child_id)
        self.assertEqual(concept["origin_brand_review_id"], applied["review_id"])
        self.assertEqual(current_brand(state, "concept", concept_id, concept["description"]), "generic_or_small")
        self.assertIn(concept_id, state["assessments"])
        row = next(row for row in summary["products"] if row["id"] == concept_id)
        self.assertNotEqual(row["status"], "excluded_established_brand")
        self.assertEqual(state["descriptions"], before["descriptions"])
        for job_id, job in before["jobs"].items():
            self.assertEqual(job, state["jobs"][job_id])
        count = len(self.runner.calls)
        run_flow(self.memory, self.runner, fit_policy_id=FIT_POLICY_ID, as_of=NOW)
        self.assertEqual(len(self.runner.calls), count)

    def test_exact_replay_after_resolution_never_reopens_or_calls_models(self):
        request = self.request()
        apply_brand_review(self.memory, request)
        run_flow(self.memory, self.runner, fit_policy_id=FIT_POLICY_ID, as_of=NOW)
        before, calls = self.memory.snapshot(), len(self.runner.calls)
        self.assertFalse(apply_brand_review(self.memory, request)["applied"])
        self.assertEqual(before, self.memory.snapshot())
        self.assertEqual(calls, len(self.runner.calls))

    def test_unclear_brand_is_admitted_not_failed(self):
        apply_brand_review(self.memory, self.request(verdict="unclear"))
        run_flow(self.memory, self.runner, fit_policy_id=FIT_POLICY_ID, as_of=NOW)
        link = self.memory.snapshot()["associations"][self.child_id]
        self.assertEqual(link["status"], "resolved")
        state = self.memory.snapshot()
        self.assertEqual(current_brand(state, "concept", link["concept_id"], state["concepts"][link["concept_id"]]["description"]), "unclear")

    def test_later_concept_review_outranks_creation_review_without_identity_rewrite(self):
        apply_brand_review(self.memory, self.request())
        run_flow(self.memory, self.runner, fit_policy_id=FIT_POLICY_ID, as_of=NOW)
        state = self.memory.snapshot(); concept_id = state["associations"][self.child_id]["concept_id"]
        before = deepcopy(state["concepts"][concept_id])
        request = self.request(kind="concept", target_id=concept_id, verdict="established")
        applied = apply_brand_review(self.memory, request)
        state = self.memory.snapshot()
        self.assertEqual(state["concepts"][concept_id], before)
        self.assertEqual(brand_binding(state, "concept", concept_id, before["description"])["review_id"], applied["review_id"])
        self.assertEqual(current_brand(state, "concept", concept_id, before["description"]), "established")

    def test_b_same_does_not_transfer_child_overlay_to_existing_concept(self):
        state = self.memory.snapshot()
        saved = state["descriptions"][state["associations"][self.child_id]["semantic_id"]]["result"]
        product = deepcopy(saved["products"][0]); product["brand"] = "unclear"
        self.memory.put("concepts", "existing-caddy", {"description": product, "status": "provisional"})
        # A known canonical concept's brand verdict stays its own, even when
        # the admitted child refers to that same useful product type.
        apply_brand_review(self.memory, self.request())
        run_flow(self.memory, self.runner, fit_policy_id=FIT_POLICY_ID, as_of=NOW)
        state = self.memory.snapshot()
        self.assertEqual(state["associations"][self.child_id]["concept_id"], "existing-caddy")
        self.assertNotIn("origin_brand_review_id", state["concepts"]["existing-caddy"])
        self.assertEqual(current_brand(state, "concept", "existing-caddy", state["concepts"]["existing-caddy"]["description"]), "unclear")

    def test_changed_origin_link_is_only_affected_binding_problem(self):
        apply_brand_review(self.memory, self.request())
        run_flow(self.memory, self.runner, fit_policy_id=FIT_POLICY_ID, as_of=NOW)
        state = self.memory.snapshot(); concept_id = state["associations"][self.child_id]["concept_id"]
        link = state["associations"][self.child_id]
        self.memory.put("associations", self.child_id, dict(link, origin_description_hash="0" * 64), previous=link)
        summary = run_flow(self.memory, self.runner, fit_policy_id=FIT_POLICY_ID, as_of=NOW)
        affected = next(row for row in summary["products"] if row["id"] == concept_id)
        self.assertEqual(affected["status"], "decision_binding_pending")
        self.assertEqual(affected["brand_binding_problem"], "BRAND_ORIGIN_BINDING_CHANGED")
        self.assertTrue(any(row["id"] != concept_id and row["status"] != "decision_binding_pending" for row in summary["products"]))

    def test_stale_target_and_unrelated_sources_cannot_apply(self):
        request = self.request()
        state = self.memory.snapshot(); association = state["associations"][self.child_id]
        self.memory.put("associations", self.child_id, dict(association, reason="out_of_scope"), previous=association)
        before = self.memory.snapshot()
        with self.assertRaisesRegex(SafetyError, "DEPENDENCY_CHANGED"):
            apply_brand_review(self.memory, request)
        self.assertEqual(before, self.memory.snapshot())
        unrelated = next(i for i, row in state["observations"].items() if row["semantic_id"] != association["semantic_id"])
        with self.assertRaisesRegex(SafetyError, "SOURCE_INVALID"):
            prepare_brand_review(self.memory.snapshot(), "child", self.child_id, verdict="generic_or_small",
                reason="A source review.", evidence_ids=[unrelated], reviewer="fixture_owner", reviewed_at=NOW)

    def test_other_exclusion_is_not_cleared(self):
        state = self.memory.snapshot(); old = state["associations"][self.child_id]
        self.memory.put("associations", self.child_id, dict(old, reason="out_of_scope"), previous=old)
        request = self.request()
        before = self.memory.snapshot()
        with self.assertRaisesRegex(SafetyError, "OTHER_EXCLUSION"):
            apply_brand_review(self.memory, request)
        self.assertEqual(before, self.memory.snapshot())

    def test_superseded_review_replay_does_not_restore_old_verdict(self):
        first = self.request(verdict="unclear")
        apply_brand_review(self.memory, first)
        second = self.request(verdict="generic_or_small")
        apply_brand_review(self.memory, second)
        before = self.memory.snapshot()
        self.assertFalse(apply_brand_review(self.memory, first)["applied"])
        self.assertEqual(before, self.memory.snapshot())
        saved = before["descriptions"][before["associations"][self.child_id]["semantic_id"]]["result"]["products"][0]
        self.assertEqual(current_brand(before, "child", self.child_id, saved), "generic_or_small")

    def test_target_retired_between_prepare_and_writer_check_cannot_apply(self):
        apply_brand_review(self.memory, self.request())
        run_flow(self.memory, self.runner, fit_policy_id=FIT_POLICY_ID, as_of=NOW)
        concept_id = self.memory.snapshot()["associations"][self.child_id]["concept_id"]
        review = self.request(kind="concept", target_id=concept_id)
        original_commit = self.memory.commit
        def retire_before_writer(event_id, changes, *, validate=None):
            current = self.memory.snapshot()["concepts"][concept_id]
            original_commit("fixture-retire-before-brand", [change("concepts", concept_id, dict(current, status="retired"), current)])
            return original_commit(event_id, changes, validate=validate)
        with patch.object(self.memory, "commit", side_effect=retire_before_writer), self.assertRaisesRegex(SafetyError, "TARGET_INACTIVE"):
            apply_brand_review(self.memory, review)
        self.assertNotIn(key("brand-overlay", review), self.memory.snapshot()["brand_reviews"])


if __name__ == "__main__":
    unittest.main()
