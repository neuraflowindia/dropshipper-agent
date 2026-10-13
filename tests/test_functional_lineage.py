"""Real saved captures/accepted definitions; fictional operator judgments only."""

from copy import deepcopy
import unittest
from unittest.mock import patch

import test_functional_aliases as alias_fixture
from product_radar.functional_aliases import accept_functional_alias, alias_projection, prepare_alias_input
from product_radar.functional_lineage import (KIND, accept_functional_lineage, history_ids, hold_overlap,
    lineage_binding, lineage_projection, prepare_lineage_input, selected_lineage_binding)
from product_radar.models import SafetyError, digest


AT = "2026-10-06T11:00:00+00:00"


class FunctionalLineageTests(unittest.TestCase):
    def setUp(self):
        self.fixture = alias_fixture.FunctionalAliasTests()
        self.fixture.setUp(); self.addCleanup(self.fixture.doCleanups)
        self.memory = self.fixture.memory
        self.base = self.fixture.definition
        self.fixture.accept_definition("bird", dict(self.base, report_title="Bird bookends",
            compatibility_constraints=["For narrow shelves"]), reviewed_at=AT)
        self.fixture.accept_definition("cat", dict(self.base, report_title="Cat bookends",
            compatibility_constraints=["For narrow shelves", "For metal shelves"]), reviewed_at=AT)

    def prepared(self, parent="bear", child="bird", at=AT):
        return prepare_lineage_input(self.memory.snapshot(), parent, child,
            grounding_refs={cid: [self.fixture.fixture.observations.get(cid, "missing_observation")]
                            for cid in (parent, child)}, reviewed_at=at)

    def accept(self, parent="bear", child="bird", prepared=None, **extra):
        return accept_functional_lineage(self.memory, prepared or self.prepared(parent, child),
            reviewer="synthetic_operator", reason="The child retains the use and mechanism, with the supplied narrower distinction.",
            distinctions=["The supplied shelf compatibility boundary is narrower."], **extra)

    def hold(self, rid, cid, *, client="default", status="New", state="uploaded"):
        self.memory.put("issue_reservations", rid, {"issue_id": rid, "client_id": client,
            "state": state, "products": [{"concept_id": cid, "status": status}]})

    def expose(self, rid, cid, *, client="default"):
        self.memory.put("published_history", rid, {"concept_id": cid, "issue_id": rid,
            "client_id": client, "status": "published"})

    def test_additive_acceptance_and_exact_replay_leave_all_other_tables_untouched(self):
        before = self.memory.snapshot(); prepared = self.prepared()
        first = self.accept(prepared=prepared); after = self.memory.snapshot()
        self.assertTrue(first["applied"])
        for table in before:
            if table != "source_checks": self.assertEqual(before[table], after[table], table)
        count = len(list((self.memory.root / "events").glob("*.json")))
        self.assertTrue(self.accept(prepared=prepared)["reused"])
        self.assertEqual(after, self.memory.snapshot())
        self.assertEqual(count, len(list((self.memory.root / "events").glob("*.json"))))
        row = after["source_checks"][first["record_id"]]
        self.assertEqual(row["kind"], KIND)
        proof = row["input"]["grounding_bindings"]["bird"][self.fixture.fixture.observations["bird"]]
        self.assertEqual(proof["association"], before["associations"][proof["association_id"]])

    def test_history_is_directional_and_siblings_are_not_aliases_or_overlapping_holds(self):
        self.accept(); self.accept("bear", "cat")
        projection = lineage_projection(self.memory.snapshot())
        self.assertEqual(history_ids("bear", projection), {"bear"})
        self.assertEqual(history_ids("bird", projection), {"bear", "bird"})
        self.assertEqual(history_ids("cat", projection), {"bear", "cat"})
        self.assertEqual(projection["bird"]["member_ids"], ["bird"])
        self.assertTrue(hold_overlap("bear", "bird", projection))
        self.assertTrue(hold_overlap("bird", "bear", projection))
        self.assertFalse(hold_overlap("bird", "cat", projection))
        self.assertEqual(projection["bird"]["record_refs"], [])
        self.assertEqual(len(projection["bird"]["lineage_refs"]), 1)

    def test_transitive_history_survives_title_and_actual_meaning_changes(self):
        self.accept(); self.accept("bird", "cat")
        before = lineage_projection(self.memory.snapshot())
        self.assertEqual(history_ids("cat", before), {"bear", "bird", "cat"})
        self.fixture.accept_definition("bear", dict(self.base, report_title="Later sales heading"),
            reviewed_at="2026-10-06T12:00:00+00:00")
        self.fixture.accept_definition("cat", dict(self.base, mechanism="Later explicitly revised mechanism"),
            reviewed_at="2026-10-06T12:00:00+00:00")
        self.assertEqual(before, lineage_projection(self.memory.snapshot()))

    def test_current_definition_change_refuses_new_edge_but_not_exact_original_replay(self):
        prepared = self.prepared(); self.accept(prepared=prepared)
        stale = self.prepared("bear", "cat")
        self.fixture.accept_definition("cat", dict(self.base, included_distinctions=["Revised context"]),
            reviewed_at="2026-10-06T12:00:00+00:00")
        with self.assertRaisesRegex(SafetyError, "INPUT_CHANGED"):
            self.accept("bear", "cat", prepared=stale)
        self.assertTrue(self.accept(prepared=prepared)["reused"])

    def test_differently_worded_use_or_mechanism_requires_explicit_operator_basis(self):
        self.fixture.accept_definition("bird", dict(self.base, primary_use="Support upright books",
            compatibility_constraints=["For narrow shelves"]), reviewed_at=AT)
        with self.assertRaisesRegex(SafetyError, "OPERATOR_REVIEW_REQUIRED"): self.accept()
        self.assertTrue(self.accept(semantic_basis="operator_reviewed")["applied"])

    def test_identical_meanings_or_missing_distinction_are_not_a_split(self):
        self.fixture.accept_definition("bird", self.base, reviewed_at=AT)
        with self.assertRaisesRegex(SafetyError, "MEANINGFUL_NARROWING_REQUIRED"): self.accept()
        self.fixture.accept_definition("bird", dict(self.base, compatibility_constraints=["For narrow shelves"]), reviewed_at=AT)
        with self.assertRaisesRegex(SafetyError, "JUDGMENT_INVALID"):
            accept_functional_lineage(self.memory, self.prepared(), reviewer="operator", reason="Explicit review.", distinctions=[])

    def test_current_grounding_change_refuses_even_if_the_prepared_input_is_rehashed(self):
        prepared = self.prepared(); observation_id = self.fixture.fixture.observations["bird"]
        # Observations are immutable in the actual memory writer. A caller
        # cannot turn a changed alleged original into proof by rehashing it.
        prepared["grounding_bindings"]["bird"][observation_id]["observation_hash"] = "0" * 64
        prepared["input_hash"] = digest({k: v for k, v in prepared.items() if k != "input_hash"})
        with self.assertRaisesRegex(SafetyError, "GROUNDING_CHANGED"): self.accept(prepared=prepared)

    def test_foreign_missing_or_future_grounding_is_refused(self):
        refs = {"bear": [self.fixture.fixture.observations["bear"]], "bird": [self.fixture.fixture.observations["cat"]]}
        with self.assertRaisesRegex(SafetyError, "NOT_ASSOCIATED"):
            prepare_lineage_input(self.memory.snapshot(), "bear", "bird", grounding_refs=refs, reviewed_at=AT)
        refs["bird"] = ["missing_observation"]
        with self.assertRaisesRegex(SafetyError, "GROUNDING_INVALID"):
            prepare_lineage_input(self.memory.snapshot(), "bear", "bird", grounding_refs=refs, reviewed_at=AT)
        refs["bird"] = [self.fixture.fixture.observations["bird"]]
        with self.assertRaises(SafetyError):
            prepare_lineage_input(self.memory.snapshot(), "bear", "bird", grounding_refs=refs,
                                  reviewed_at="2026-10-03T10:00:00+00:00")

    def test_missing_or_legacy_unaccepted_identity_cannot_be_created_by_preparation(self):
        for child in ("missing", "other"):
            before = self.memory.snapshot()
            with self.assertRaises(SafetyError): self.prepared("bear", child)
            self.assertEqual(before, self.memory.snapshot())
        with self.assertRaisesRegex(SafetyError, "SELF_EDGE"): self.prepared("bear", "bear")

    def test_cycles_and_alias_collapsed_edges_are_refused_without_writes(self):
        self.accept(); self.accept("bird", "cat")
        before = self.memory.snapshot()
        with self.assertRaisesRegex(SafetyError, "CYCLE"): self.accept("cat", "bear")
        self.assertEqual(before, self.memory.snapshot())
        self.fixture.accept_definition("other", self.base, reviewed_at=AT)
        prepared = prepare_alias_input(self.memory.snapshot(), ["bear", "other"], reviewed_at=AT)
        accept_functional_alias(self.memory, prepared, method="exact_meaning", reviewer="operator", reason="Exact same meanings.")
        # Existing aliases expand only the ancestor's equivalence component.
        projection = lineage_projection(self.memory.snapshot())
        self.assertEqual(history_ids("bird", projection), {"bear", "bird", "other"})
        self.assertNotIn("bird", history_ids("other", projection))

    def test_held_parent_and_child_return_both_actual_issue_ids_without_append(self):
        self.hold("first", "bear"); self.hold("second", "bird")
        before = self.memory.snapshot(); result = self.accept()
        self.assertEqual(result["affected_issue_ids"], ["first", "second"])
        self.assertEqual(result["decision"], "release_review_required")
        self.assertEqual(before, self.memory.snapshot())

    def test_two_products_in_the_same_hold_return_that_one_issue(self):
        self.memory.put("issue_reservations", "same", {"issue_id": "same", "state": "reserved",
            "products": [{"concept_id": "bear", "status": "New"}, {"concept_id": "bird", "status": "New"}]})
        self.assertEqual(self.accept()["affected_issue_ids"], ["same"])

    def test_published_parent_does_not_block_edge_but_queued_new_child_needs_repair(self):
        self.expose("old", "bear"); self.hold("next", "bird")
        self.assertEqual(self.accept()["affected_issue_ids"], ["next"])
        old = self.memory.snapshot()["issue_reservations"]["next"]
        self.memory.put("issue_reservations", "next", dict(old, state="cancelled"), previous=old)
        self.assertTrue(self.accept()["applied"])

    def test_other_client_and_common_ancestor_siblings_do_not_block_the_new_edge(self):
        self.accept(); self.hold("bird_issue", "bird"); self.hold("cat_issue", "cat")
        self.hold("other_client_parent", "bear", client="another_client")
        self.assertTrue(self.accept("bear", "cat")["applied"])

    def test_published_reservation_already_backed_by_real_exposure_is_not_a_forever_hold(self):
        self.hold("old", "bear", state="published"); self.expose("old", "bear")
        self.assertTrue(self.accept()["applied"])

    def test_current_definition_and_hold_races_are_rechecked_under_commit(self):
        original = self.memory.commit; injected = False
        def racing_commit(event_id, changes, *, validate=None):
            nonlocal injected
            if not injected:
                injected = True; self.hold("racing", "bear")
                self.hold("other_racing", "bird")
            return original(event_id, changes, validate=validate)
        with patch.object(self.memory, "commit", side_effect=racing_commit):
            with self.assertRaisesRegex(SafetyError, "RELEASE_REVIEW_REQUIRED"): self.accept()
        self.assertIsNone(lineage_binding(self.memory.snapshot()))

    def test_actual_association_race_is_rechecked_under_commit(self):
        original = self.memory.commit; injected = False
        def racing_commit(event_id, changes, *, validate=None):
            nonlocal injected
            if not injected:
                injected = True; aid = self.fixture.fixture.children["bird"]
                old = self.memory.snapshot()["associations"][aid]
                self.memory.put("associations", aid, dict(old, reason="A changed real association"), previous=old)
            return original(event_id, changes, validate=validate)
        with patch.object(self.memory, "commit", side_effect=racing_commit):
            with self.assertRaisesRegex(SafetyError, "GROUNDING_CHANGED"): self.accept()
        self.assertIsNone(lineage_binding(self.memory.snapshot()))

    def test_definition_race_is_rechecked_under_commit(self):
        original = self.memory.commit; injected = False
        def racing_commit(event_id, changes, *, validate=None):
            nonlocal injected
            if not injected:
                injected = True
                self.fixture.accept_definition("bird", dict(self.base, compatibility_constraints=["Revised constraint"]),
                    reviewed_at="2026-10-06T12:00:00+00:00")
            return original(event_id, changes, validate=validate)
        with patch.object(self.memory, "commit", side_effect=racing_commit):
            with self.assertRaisesRegex(SafetyError, "INPUT_CHANGED"): self.accept()
        self.assertIsNone(lineage_binding(self.memory.snapshot()))

    def test_reverse_edge_inside_existing_alias_component_is_a_cycle(self):
        self.fixture.accept_definition("other", self.base, reviewed_at=AT)
        accept_functional_alias(self.memory, prepare_alias_input(self.memory.snapshot(), ["bear", "other"], reviewed_at=AT),
            method="exact_meaning", reviewer="operator", reason="Same literal meanings.")
        self.accept()
        before = self.memory.snapshot()
        with self.assertRaisesRegex(SafetyError, "CYCLE"): self.accept("bird", "other")
        self.assertEqual(before, self.memory.snapshot())

    def test_future_review_cannot_append_an_edge(self):
        with self.assertRaisesRegex(SafetyError, "REVIEW_DATE_INVALID"):
            self.accept(prepared=self.prepared(at="2099-01-01T00:00:00+00:00"))

    def test_unrelated_broken_legacy_identity_does_not_block_healthy_lineage(self):
        old = self.memory.snapshot()["concepts"]["other"]
        self.memory.put("concepts", "other", dict(old, status="merged", merged_into="missing_target"), previous=old)
        self.hold("unrelated_broken", "other")
        self.assertTrue(self.accept()["applied"])

    def test_operator_review_needs_real_explanation_not_an_empty_label(self):
        for reviewer, reason, distinctions in (("", "Review.", ["Boundary"]), ("operator", "", ["Boundary"]),
                                               ("operator", "Review.", [""])):
            with self.assertRaisesRegex(SafetyError, "JUDGMENT_INVALID"):
                accept_functional_lineage(self.memory, self.prepared(), reviewer=reviewer, reason=reason,
                    distinctions=distinctions, semantic_basis="operator_reviewed")

    def test_alias_arriving_during_commit_cannot_collapse_the_proposed_lineage(self):
        original = self.memory.commit; injected = False
        def racing_commit(event_id, changes, *, validate=None):
            nonlocal injected
            if not injected:
                injected = True
                accept_functional_alias(self.memory,
                    prepare_alias_input(self.memory.snapshot(), ["bear", "bird"], reviewed_at=AT),
                    method="operator", reviewer="synthetic_operator", reason="Concurrent explicit same-type correction.")
            return original(event_id, changes, validate=validate)
        with patch.object(self.memory, "commit", side_effect=racing_commit):
            with self.assertRaisesRegex(SafetyError, "CYCLE"): self.accept()
        self.assertIsNone(lineage_binding(self.memory.snapshot()))

    def test_historical_original_association_is_retained_after_a_later_correction(self):
        self.accept(); before = lineage_projection(self.memory.snapshot())
        aid = self.fixture.fixture.children["bird"]; old = self.memory.snapshot()["associations"][aid]
        self.memory.put("associations", aid, dict(old, reason="Later explicit association annotation"), previous=old)
        self.assertEqual(before, lineage_projection(self.memory.snapshot()))

    def test_corrupt_routing_field_cannot_hide_the_original_lineage(self):
        result = self.accept(); state = self.memory.snapshot()
        state["source_checks"][result["record_id"]]["input"]["child_id"] = "other"
        with self.assertRaisesRegex(SafetyError, "RECORD_INVALID"): lineage_projection(state)

    def test_selected_binding_changes_only_for_selected_ancestry_and_is_order_independent(self):
        self.accept(); state = self.memory.snapshot()
        before = selected_lineage_binding(state, ["bird"])
        self.assertIsNotNone(before); self.assertIsNone(selected_lineage_binding(state, ["bear"]))
        self.accept("bear", "cat"); state = self.memory.snapshot()
        self.assertEqual(before, selected_lineage_binding(state, ["bird"]))
        reversed_state = {table: dict(reversed(list(rows.items()))) for table, rows in state.items()}
        self.assertEqual(lineage_projection(state), lineage_projection(reversed_state))
        self.assertEqual(lineage_binding(state), lineage_binding(reversed_state))


if __name__ == "__main__": unittest.main()
