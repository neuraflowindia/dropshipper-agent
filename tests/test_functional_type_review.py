"""Independent offline adversarial review of the new S1 additive contracts.

Every observation is fictional and stored below a temporary directory. These
tests exercise actual saved-record constructors, not semantic model quality or
live research. No provider, browser, production store or cloud call is made.
"""

from copy import deepcopy
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from product_radar.functional_types import (
    accept_functional_definition, current_example_memberships,
    definition_meaning_hash, effective_functional_definition, prepare_example_memberships,
)
from product_radar.identity_corrections import apply_unallocated_identity_merge
from product_radar.models import SafetyError, digest
from product_radar.research_flow import run_flow
from product_radar.research_memory import ResearchMemory, change
from product_radar.research_questions import prepare_research_questions
from product_radar.storage import Journal
import test_identity_corrections as identity_fixtures
from test_research_flow import FakeRunner, capture


NOW = "2026-10-06T10:00:00+00:00"
LATER = "2026-10-06T11:00:00+00:00"


class FunctionalTypeReviewTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="functional-type-review-")
        self.addCleanup(temporary.cleanup)
        root = Path(temporary.name)
        self.memory = ResearchMemory(root / "research", Journal(root / "finance"), evidence_type="synthetic")
        capture(self.memory)
        run_flow(self.memory, FakeRunner(), as_of=NOW, fit_policy_id="fit-13-v1")
        state = self.memory.snapshot()
        self.cid = next(cid for cid, row in state["concepts"].items()
                        if row["description"]["label"] == "Sink caddy")
        semantics = {row["semantic_id"] for row in state["associations"].values()
                     if row.get("concept_id") == self.cid and row.get("status") == "resolved"}
        self.refs = [ref for ref, row in state["observations"].items()
                     if row["semantic_id"] in semantics]
        self.decisions = {child_id: {"decision": "same", "reason": "Fictional explicit membership review."}
                          for child_id, row in state["associations"].items()
                          if row.get("concept_id") == self.cid and row.get("status") == "resolved"}
        # This meaning is deliberately narrower than the old descriptor. Its
        # explicit per-child judgment must not be inferred from a legacy label.
        self.definition = {
            "report_title": "Draining sink caddy", "primary_use": "Store wet dishwashing tools",
            "mechanism": "Passive draining holder", "use_context": ["Beside the kitchen sink"],
            "compatibility_constraints": [], "included_distinctions": ["Draining base"],
            "excluded_distinctions": ["Powered cleaning brush"],
        }

    def accept(self, definition=None, *, reviewed_at=NOW, **kwargs):
        current = effective_functional_definition(self.memory.snapshot(), self.cid)
        return accept_functional_definition(
            self.memory, self.cid, self.definition if definition is None else definition,
            basis_refs=self.refs, reviewer="independent_synthetic_review", reviewed_at=reviewed_at,
            expected_meaning_hash=current["meaning_hash"], **kwargs)

    def prepare_examples(self):
        return prepare_example_memberships(self.memory, self.cid, as_of=NOW)

    def question(self, **kwargs):
        current = effective_functional_definition(self.memory.snapshot(), self.cid)
        return {
            "subject": {"kind": "concept", "concept_id": self.cid,
                        "definition_hash": current["meaning_hash"]},
            "route": "amazon-search", "purpose": "validation",
            "request": {"query": "sink caddy", "country": "IN"}, **kwargs,
        }

    def test_title_only_revision_retains_current_explicit_memberships(self):
        self.accept(example_decisions=self.decisions)
        first = self.prepare_examples()
        self.assertEqual(len(first["membership_ids"]), 2)
        original_meaning = first["meaning_hash"]
        renamed = {**self.definition, "report_title": "A tidier sink"}
        self.accept(renamed, reviewed_at=LATER)
        second = self.prepare_examples()
        self.assertEqual(second["meaning_hash"], original_meaning)
        self.assertEqual(second["pending_child_ids"], [])
        self.assertEqual(len(current_example_memberships(self.memory.snapshot(), self.cid)), 2)

    def test_same_meaning_does_not_reuse_a_corrected_child_binding(self):
        self.accept(example_decisions=self.decisions)
        self.prepare_examples()
        original = self.memory.snapshot()["associations"][next(iter(self.decisions))]
        child_id = next(iter(self.decisions))
        # A different reviewed association reason is a changed exact binding,
        # even though the accepted type itself has not been renamed or revised.
        self.memory.put("associations", child_id,
                        {**original, "reason": "Subsequent actual identity review."}, previous=original)
        second = self.prepare_examples()
        self.assertIn(child_id, second["pending_child_ids"])
        self.assertEqual(len(current_example_memberships(self.memory.snapshot(), self.cid)), 1)

    def test_membership_prepare_replay_refuses_a_corrupted_existing_record(self):
        self.accept(example_decisions=self.decisions)
        prepared = self.prepare_examples()
        membership_id = prepared["membership_ids"][0]
        original = self.memory.snapshot()["source_checks"][membership_id]
        # The generic event store intentionally allows source-check updates;
        # the typed constructor must not call a damaged existing row reusable.
        self.memory.put("source_checks", membership_id,
                        {**original, "record_hash": "0" * 64}, previous=original)
        with self.assertRaisesRegex(SafetyError, "FUNCTIONAL_EXAMPLE_.*(?:HASH|RECORD|BINDING)"):
            self.prepare_examples()

    def test_definition_identity_cannot_hide_self_consistent_different_meaning(self):
        accepted = self.accept(example_decisions=self.decisions)
        definition_id = accepted["definition_id"]
        original = self.memory.snapshot()["source_checks"][definition_id]
        damaged = deepcopy(original)
        damaged["definition"]["mechanism"] = "A powered draining pump"
        damaged["meaning_hash"] = definition_meaning_hash(damaged["definition"])
        damaged["record_hash"] = digest({field: value for field, value in damaged.items()
                                          if field != "record_hash"})
        # A self-consistent row hash is not proof that this is the exact
        # judgment whose immutable deterministic identity the caller selected.
        self.memory.put("source_checks", definition_id, damaged, previous=original)
        with self.assertRaisesRegex(SafetyError, "FUNCTIONAL_DEFINITION_.*(?:BINDING|HASH|RECORD)"):
            effective_functional_definition(self.memory.snapshot(), self.cid)

    def test_saved_question_candidates_cannot_change_under_a_frozen_binding(self):
        question = self.question(wording_candidates=[{
            "text": "draining sink organiser",
            "request": {"query": "draining sink organiser", "country": "IN"},
        }])
        original = prepare_research_questions(self.memory, [question], as_of=NOW)["records"][0]
        damaged = deepcopy(original)
        damaged["wording_candidates"][0]["request"]["query"] = "Unrelated query"
        self.memory.put("query_tasks", original["id"], damaged, previous=original)
        before = self.memory.snapshot()
        with self.assertRaisesRegex(SafetyError, "RESEARCH_QUESTION_BINDING_CONFLICT"):
            prepare_research_questions(self.memory, [question], as_of=LATER)
        self.assertEqual(self.memory.snapshot(), before)

    def test_saved_question_wrapper_cannot_disagree_with_immutable_subject(self):
        question = self.question()
        original = prepare_research_questions(self.memory, [question], as_of=NOW)["records"][0]
        damaged = deepcopy(original)
        damaged["subject"]["definition_hash"] = "0" * 64
        self.memory.put("query_tasks", original["id"], damaged, previous=original)
        with self.assertRaisesRegex(SafetyError, "RESEARCH_QUESTION_BINDING_CONFLICT"):
            prepare_research_questions(self.memory, [question], as_of=LATER)

    def test_corrupted_parent_question_cannot_admit_new_alternative(self):
        original = prepare_research_questions(self.memory, [self.question()], as_of=NOW)["records"][0]
        damaged = deepcopy(original)
        damaged["request"]["query"] = "Changed without a new immutable binding"
        self.memory.put("query_tasks", original["id"], damaged, previous=original)
        before = self.memory.snapshot()
        child = self.question(parent_question_id=original["id"],
                              request={"query": "draining sink organiser", "country": "IN"})
        with self.assertRaisesRegex(SafetyError, "RESEARCH_QUESTION_(?:BINDING_CONFLICT|PARENT_INVALID)"):
            prepare_research_questions(self.memory, [child], as_of=LATER)
        self.assertEqual(self.memory.snapshot(), before)

    def test_parent_immutable_binding_rechecked_before_child_commit(self):
        parent = prepare_research_questions(self.memory, [self.question()], as_of=NOW)["records"][0]
        child = self.question(parent_question_id=parent["id"],
                              request={"query": "draining sink organiser", "country": "IN"})
        original_commit = self.memory.commit
        injected = False

        def race(event_id, changes, *, validate=None):
            nonlocal injected
            if not injected:
                injected = True
                damaged = {**parent, "kind": "future-unknown-question"}
                original_commit("independent-parent-correction",
                                [change("query_tasks", parent["id"], damaged, parent)])
            return original_commit(event_id, changes, validate=validate)

        with patch.object(self.memory, "commit", side_effect=race):
            with self.assertRaisesRegex(SafetyError, "RESEARCH_QUESTION_(?:BINDING_CONFLICT|PARENT_INVALID)"):
                prepare_research_questions(self.memory, [child], as_of=LATER)
        self.assertEqual(set(self.memory.snapshot()["query_tasks"]), {parent["id"]})


class FunctionalMergeReviewTests(unittest.TestCase):
    def test_new_accepted_meaning_cannot_hide_behind_old_raw_merge_hashes(self):
        # Reuse the real four-concept synthetic source fixture, without
        # inheriting or rerunning that class's unrelated tests.
        fixture = identity_fixtures.IdentityCorrectionTests()
        fixture.setUp()
        self.addCleanup(fixture.temp.cleanup)
        memory = fixture.memory
        original_request = deepcopy(fixture.request)
        state = memory.snapshot()
        current = effective_functional_definition(state, "bird")
        definition = {field: deepcopy(current[field]) for field in (
            "report_title", "primary_use", "mechanism", "use_context",
            "compatibility_constraints", "included_distinctions", "excluded_distinctions")}
        definition["compatibility_constraints"] = ["Only narrow shelf edges"]
        accept_functional_definition(
            memory, "bird", definition, basis_refs=[fixture.observations["bird"]],
            reviewer="independent_synthetic_review", reviewed_at=NOW,
            expected_meaning_hash=current["meaning_hash"], example_decisions={
                fixture.children["bird"]: {"decision": "same", "reason": "Source-bound synthetic review."}})
        # Existing raw concept and child rows are deliberately unchanged by
        # the additive writer; the old merge review therefore lacks the new
        # accepted functional boundary, not merely a changed display title.
        state = memory.snapshot()
        self.assertEqual({cid: digest(state["concepts"][cid])
                          for cid in original_request["concept_hashes"]}, original_request["concept_hashes"])
        with self.assertRaisesRegex(SafetyError, "IDENTITY_MERGE_.*(?:DEFINITION|MEANING|INPUT|REVIEW)"):
            apply_unallocated_identity_merge(memory, original_request)
        self.assertNotEqual(memory.snapshot()["concepts"]["bird"]["status"], "merged")

    def test_definition_accepted_during_merge_commit_recheck_is_not_lost(self):
        fixture = identity_fixtures.IdentityCorrectionTests()
        fixture.setUp()
        self.addCleanup(fixture.temp.cleanup)
        memory = fixture.memory
        original_commit = memory.commit
        injected = False

        def race(event_id, changes, *, validate=None):
            nonlocal injected
            if not injected:
                injected = True
                state = memory.snapshot()
                current = effective_functional_definition(state, "bird")
                definition = {field: deepcopy(current[field]) for field in (
                    "report_title", "primary_use", "mechanism", "use_context",
                    "compatibility_constraints", "included_distinctions", "excluded_distinctions")}
                definition["compatibility_constraints"] = ["Only narrow shelf edges"]
                accept_functional_definition(
                    memory, "bird", definition, basis_refs=[fixture.observations["bird"]],
                    reviewer="independent_synthetic_review", reviewed_at=NOW,
                    expected_meaning_hash=current["meaning_hash"])
            return original_commit(event_id, changes, validate=validate)

        with patch.object(memory, "commit", side_effect=race):
            with self.assertRaisesRegex(SafetyError, "IDENTITY_MERGE_FUNCTIONAL_REVIEW_REQUIRED"):
                apply_unallocated_identity_merge(memory, fixture.request)
        state = memory.snapshot()
        self.assertTrue(effective_functional_definition(state, "bird")["accepted"])
        self.assertEqual(state["concepts"]["bird"]["status"], "provisional")
        self.assertEqual(state["concepts"]["cat"]["status"], "provisional")


if __name__ == "__main__":
    unittest.main()
