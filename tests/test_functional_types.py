"""Source-bound functional meaning; fixtures are not real product judgments."""

from copy import deepcopy
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from product_radar.functional_types import (accept_functional_definition, current_example_memberships,
    definition_meaning_hash, effective_functional_definition, functional_type_inventory,
    prepare_example_memberships, validate_definition)
from product_radar.models import SafetyError, digest
from product_radar.research_memory import ResearchMemory, change
from product_radar.storage import Journal
from test_research_flow import FakeRunner, NOW, capture, run_flow


class FunctionalTypeTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        root = Path(self.temp.name)
        self.memory = ResearchMemory(root / "research", Journal(root / "finance"), evidence_type="synthetic")
        capture(self.memory)
        run_flow(self.memory, FakeRunner(), as_of=NOW)
        self.state = self.memory.snapshot()
        self.cid = next(iter(self.state["concepts"]))
        semantic_ids = {row["semantic_id"] for row in self.state["associations"].values()
                        if row["concept_id"] == self.cid}
        self.refs = [obs_id for obs_id, row in self.state["observations"].items() if row["semantic_id"] in semantic_ids]
        self.definition = {"report_title": "Draining sink caddy", "primary_use": "Store wet sink essentials",
                           "mechanism": "Passive draining tray", "use_context": ["Kitchen sink"],
                           "compatibility_constraints": [], "included_distinctions": ["Draining base"],
                           "excluded_distinctions": ["Powered cleaning brush"]}

    def accept(self, definition=None, **kwargs):
        decisions = {child_id: {"decision": "same", "reason": "Explicit synthetic functional membership review."}
                     for child_id, row in self.state["associations"].items() if row["concept_id"] == self.cid}
        return accept_functional_definition(self.memory, self.cid, definition or self.definition,
            basis_refs=self.refs, reviewer="synthetic_operator", reviewed_at=NOW,
            expected_meaning_hash=effective_functional_definition(self.memory.snapshot(), self.cid)["meaning_hash"],
            example_decisions=decisions,
            **kwargs)

    def test_existing_concept_is_explicit_legacy_not_new_identity(self):
        before = deepcopy(self.state)
        view = effective_functional_definition(self.state, self.cid)
        self.assertFalse(view["accepted"])
        self.assertEqual(view["basis"], "legacy_concept")
        self.assertEqual(self.state, before)
        with self.assertRaisesRegex(SafetyError, "FUNCTIONAL_CONCEPT_NOT_FOUND"):
            effective_functional_definition(self.state, "invented")

    def test_accept_is_additive_and_actual_examples_have_original_facts(self):
        accepted = self.accept()
        result = prepare_example_memberships(self.memory, self.cid, as_of=NOW)
        state = self.memory.snapshot()
        view = effective_functional_definition(state, self.cid)
        self.assertTrue(view["accepted"])
        self.assertEqual(view["definition_id"], accepted["definition_id"])
        self.assertEqual(view["meaning_hash"], result["meaning_hash"])
        for table in ("concepts", "associations", "observations", "captures", "assessments", "jobs"):
            self.assertEqual(state[table], self.state[table])
        examples = current_example_memberships(state, self.cid)
        self.assertTrue(examples)
        for example in examples:
            for row in example["observations"]:
                obs_id = row["observation_id"]
                self.assertEqual({k: v for k, v in row.items() if k != "observation_id"}, self.state["observations"][obs_id])
        self.assertEqual(functional_type_inventory(state)["accepted_definitions"], 1)

    def test_exact_retry_no_new_record_or_revision(self):
        expected = effective_functional_definition(self.state, self.cid)["meaning_hash"]
        first = self.accept()
        count = len(self.memory.snapshot()["source_checks"])
        replay = accept_functional_definition(self.memory, self.cid, self.definition, basis_refs=self.refs,
            reviewer="synthetic_operator", reviewed_at=NOW, expected_meaning_hash=expected,
            example_decisions={child_id: {"decision": "same", "reason": "Explicit synthetic functional membership review."}
                for child_id, row in self.state["associations"].items() if row["concept_id"] == self.cid})
        self.assertTrue(replay["reused"])
        self.assertEqual(first["definition_id"], replay["definition_id"])
        prepare_example_memberships(self.memory, self.cid, as_of=NOW)
        count = len(self.memory.snapshot()["source_checks"])
        prepare_example_memberships(self.memory, self.cid, as_of="2026-10-06T10:00:00+00:00")
        self.assertEqual(len(self.memory.snapshot()["source_checks"]), count)
        self.assertEqual(effective_functional_definition(self.memory.snapshot(), self.cid)["revision"], 1)

    def test_title_and_list_order_not_meaning_but_mechanism_is(self):
        changed = deepcopy(self.definition)
        changed["report_title"] = "A tidier sink"
        self.assertEqual(definition_meaning_hash(changed), definition_meaning_hash(self.definition))
        changed["included_distinctions"] += ["Adjustable drain"]
        reversed_value = deepcopy(changed)
        reversed_value["included_distinctions"].reverse()
        self.assertEqual(definition_meaning_hash(changed), definition_meaning_hash(reversed_value))
        changed["mechanism"] = "Powered pump"
        self.assertNotEqual(definition_meaning_hash(changed), definition_meaning_hash(self.definition))
        changed["excluded_distinctions"].append("Draining base")
        with self.assertRaisesRegex(SafetyError, "DISTINCTIONS_CONFLICT"):
            validate_definition(changed)

    def test_newly_narrowed_definition_does_not_automatically_accept_old_examples(self):
        accept_functional_definition(self.memory, self.cid, self.definition, basis_refs=self.refs,
            reviewer="test", reviewed_at=NOW,
            expected_meaning_hash=effective_functional_definition(self.state, self.cid)["meaning_hash"])
        result = prepare_example_memberships(self.memory, self.cid, as_of=NOW)
        self.assertEqual(result["membership_ids"], [])
        self.assertTrue(result["pending_child_ids"])
        self.assertEqual(current_example_memberships(self.memory.snapshot(), self.cid), [])

    def test_accepted_definition_stable_on_price_and_label_edits(self):
        self.accept()
        state = self.memory.snapshot()
        expected = effective_functional_definition(state, self.cid)
        changed = deepcopy(state)
        changed["concepts"][self.cid]["description"]["label"] = "New sales title"
        changed["observations"][self.refs[0]]["facts"]["price"] = "999"
        self.assertEqual(effective_functional_definition(changed, self.cid), expected)

    def test_stale_expected_meaning_foreign_evidence_and_missing_job_refused(self):
        with self.assertRaisesRegex(SafetyError, "MEANING_CHANGED"):
            accept_functional_definition(self.memory, self.cid, self.definition, basis_refs=self.refs,
                reviewer="test", reviewed_at=NOW, expected_meaning_hash="0" * 64)
        with self.assertRaisesRegex(SafetyError, "REFERENCE_NOT_ASSOCIATED"):
            accept_functional_definition(self.memory, self.cid, self.definition, basis_refs=["invented"],
                reviewer="test", reviewed_at=NOW,
                expected_meaning_hash=effective_functional_definition(self.state, self.cid)["meaning_hash"])
        with self.assertRaisesRegex(SafetyError, "JOB_NOT_COMPLETED"):
            self.accept(job_id="missing")

    def test_concurrent_source_change_refused_inside_commit(self):
        original = self.memory.commit
        injected = False
        def racing_commit(event_id, changes, *, validate=None):
            nonlocal injected
            if not injected:
                injected = True
                state = self.memory.snapshot()
                child_id, previous = next((child_id, row) for child_id, row in state["associations"].items()
                    if row.get("semantic_id") == state["observations"][self.refs[0]]["semantic_id"])
                # Original captures are immutable. The realistic concurrent
                # correction is the interpretation binding, not their bytes.
                original("synthetic-evidence-correction", [change("associations", child_id,
                    dict(previous, status="deferred"), previous)])
            return original(event_id, changes, validate=validate)
        with patch.object(self.memory, "commit", racing_commit):
            with self.assertRaisesRegex(SafetyError, "EVIDENCE_CHANGED"):
                self.accept()
        self.assertFalse(any(row.get("kind") == "functional-definition-v1"
            for row in self.memory.snapshot()["source_checks"].values()))

    def test_changed_association_or_observation_does_not_resurrect_old_example(self):
        self.accept()
        prepare_example_memberships(self.memory, self.cid, as_of=NOW)
        state = self.memory.snapshot()
        changed = deepcopy(state)
        for row in changed["associations"].values():
            if row["concept_id"] == self.cid:
                row["status"] = "deferred"
        self.assertEqual(current_example_memberships(changed, self.cid), [])
        changed = deepcopy(state)
        for ref in self.refs:
            changed["observations"][ref]["semantic"]["title"] = "Corrected source title"
        self.assertEqual(current_example_memberships(changed, self.cid), [])

    def test_malformed_or_conflicting_definition_records_cannot_be_selected(self):
        accepted = self.accept()
        state = self.memory.snapshot()
        state["source_checks"][accepted["definition_id"]]["meaning_hash"] = "0" * 64
        with self.assertRaisesRegex(SafetyError, "HASH_MISMATCH"):
            effective_functional_definition(state, self.cid)
        unresolved = functional_type_inventory(state)["unresolved"]
        # A damaged opaque review ID cannot tell us whose definition it hid.
        # Dependent semantic readers defer instead of inventing a legacy view;
        # this does not mean that every deferred product itself is defective.
        self.assertEqual(len(unresolved), len(state["concepts"]))
        self.assertIn(self.cid, [row["concept_id"] for row in unresolved])
        state = self.memory.snapshot()
        state["source_checks"]["duplicate-revision"] = deepcopy(state["source_checks"][accepted["definition_id"]])
        with self.assertRaisesRegex(SafetyError, "(?:REVISION_CONFLICT|BINDING_MISMATCH)"):
            effective_functional_definition(state, self.cid)


if __name__ == "__main__":
    unittest.main()
