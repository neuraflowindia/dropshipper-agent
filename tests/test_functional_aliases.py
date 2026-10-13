"""Actual saved-definition/alias events; fictional products, no external calls."""

from copy import deepcopy
import unittest
from unittest.mock import patch

import test_identity_corrections as fixture
import test_issue_allocation as allocation_fixture
from product_radar.functional_aliases import (accept_functional_alias, alias_binding,
    alias_projection, expand_identities, prepare_alias_input)
from product_radar.functional_types import accept_functional_definition, effective_functional_definition
from product_radar.models import SafetyError, digest
from product_radar.issue_allocation import allocate_issues, _held, _published


class FunctionalAliasTests(unittest.TestCase):
    def setUp(self):
        self.fixture = fixture.IdentityCorrectionTests()
        # The named fixture method is not run; this uses only its real saved
        # capture/description/association setup, with its own cleanup lifecycle.
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.memory = self.fixture.memory
        self.definition = {"report_title": "Decorative bookends", "primary_use": "Hold books upright",
            "mechanism": "Fixed side supports", "use_context": ["Book shelf"],
            "compatibility_constraints": [], "included_distinctions": ["Decorative themes"],
            "excluded_distinctions": ["Powered mechanism"]}
        for cid in ("bear", "bird", "cat"):
            self.accept_definition(cid)

    def accept_definition(self, cid, definition=None, reviewed_at=fixture.NOW):
        return accept_functional_definition(self.memory, cid, definition or self.definition,
            basis_refs=[self.fixture.observations[cid]], reviewer="synthetic_operator", reviewed_at=reviewed_at,
            expected_meaning_hash=effective_functional_definition(self.memory.snapshot(), cid)["meaning_hash"])

    def prepared(self, members=("bear", "bird")):
        return prepare_alias_input(self.memory.snapshot(), list(members), reviewed_at=fixture.NOW)

    def accept(self, prepared=None, **kwargs):
        return accept_functional_alias(self.memory, prepared or self.prepared(), method="exact_meaning",
            reviewer="exact_saved_meanings", reason="The accepted functional meaning fields match exactly.", **kwargs)

    def test_actual_alias_is_additive_and_replay_does_not_write(self):
        before = self.memory.snapshot()
        prepared = self.prepared()
        first = self.accept(prepared)
        after = self.memory.snapshot()
        for table in before:
            if table != "source_checks": self.assertEqual(after[table], before[table])
        self.assertTrue(first["applied"])
        self.assertTrue(self.accept(prepared)["reused"])
        self.assertEqual(self.memory.snapshot(), after)
        self.assertIsNotNone(alias_binding(after))
        projection = alias_projection(after)
        self.assertEqual(projection["bird"]["member_ids"], ["bear", "bird"])
        self.assertEqual(expand_identities({"bird"}, projection), {"bear", "bird"})

    def test_transitive_components_are_deterministic_and_keep_original_ids(self):
        self.accept()
        self.accept(self.prepared(("bird", "cat")))
        state = self.memory.snapshot()
        for cid in ("bear", "bird", "cat"):
            self.assertEqual(alias_projection(state)[cid]["member_ids"], ["bear", "bird", "cat"])
        reversed_state = {table: dict(reversed(list(rows.items()))) for table, rows in state.items()}
        self.assertEqual(alias_projection(state), alias_projection(reversed_state))
        self.assertEqual(set(state["concepts"]), {"bear", "bird", "cat", "other"})

    def test_later_definition_changes_do_not_reset_prior_exposure_identity(self):
        prepared = self.prepared(); self.accept(prepared)
        changed = dict(self.definition, report_title="Another sales title", mechanism="Revised fixed support")
        self.accept_definition("bird", changed, reviewed_at="2026-10-06T10:00:00+00:00")
        self.assertEqual(alias_projection(self.memory.snapshot())["bird"]["member_ids"], ["bear", "bird"])
        self.assertTrue(self.accept(prepared)["reused"])

    def test_changed_definition_cannot_accept_stale_review_as_new_edge(self):
        prepared = self.prepared()
        self.accept_definition("bird", dict(self.definition, mechanism="Different manual mechanism"),
            reviewed_at="2026-10-06T10:00:00+00:00")
        with self.assertRaisesRegex(SafetyError, "INPUT_CHANGED"): self.accept(prepared)
        with self.assertRaisesRegex(SafetyError, "EXACT_MEANING_REQUIRED"):
            self.accept(prepare_alias_input(self.memory.snapshot(), ["bear", "bird"],
                reviewed_at="2026-10-06T10:00:00+00:00"))

    def test_new_alias_returns_exact_affected_held_reports_without_mutating_them(self):
        for rid, cid in (("tuesday", "bear"), ("thursday", "bird")):
            self.memory.put("issue_reservations", rid, {"issue_id": rid, "client_id": "default",
                "state": "uploaded", "products": [{"concept_id": cid, "status": "New"}]})
        before = self.memory.snapshot(); result = self.accept()
        self.assertEqual(result["decision"], "release_review_required")
        self.assertEqual(result["affected_issue_ids"], ["thursday", "tuesday"])
        self.assertEqual(before, self.memory.snapshot())

    def test_publication_and_future_new_hold_require_replacement_but_other_client_does_not(self):
        self.memory.put("published_history", "old", {"concept_id": "bear", "client_id": "default",
            "issue_id": "old_issue", "status": "published"})
        self.memory.put("issue_reservations", "next", {"issue_id": "next", "client_id": "default",
            "state": "reserved", "products": [{"concept_id": "bird", "status": "New"}]})
        self.assertEqual(self.accept()["affected_issue_ids"], ["next"])
        self.memory.put("issue_reservations", "next", {"issue_id": "next", "client_id": "other_client",
            "state": "reserved", "products": [{"concept_id": "bird", "status": "New"}]},
            previous=self.memory.snapshot()["issue_reservations"]["next"])
        self.assertTrue(self.accept()["applied"])

    def test_hold_arriving_during_commit_prevents_inconsistent_alias(self):
        original = self.memory.commit; injected = False
        def racing_commit(event_id, changes, *, validate=None):
            nonlocal injected
            if not injected:
                injected = True
                self.memory.put("issue_reservations", "newhold", {"issue_id": "newhold", "state": "reserved",
                    "products": [{"concept_id": "bear", "status": "New"}, {"concept_id": "bird", "status": "New"}]})
            return original(event_id, changes, validate=validate)
        with patch.object(self.memory, "commit", side_effect=racing_commit):
            with self.assertRaisesRegex(SafetyError, "RELEASE_REVIEW_REQUIRED"): self.accept()
        self.assertIsNone(alias_binding(self.memory.snapshot()))

    def test_forged_definition_reference_and_alias_hash_are_refused(self):
        prepared = self.prepared(); prepared["definition_bindings"]["bird"]["record_hash"] = "0" * 64
        prepared["input_hash"] = digest({k: v for k, v in prepared.items() if k != "input_hash"})
        with self.assertRaisesRegex(SafetyError, "DEFINITION_CHANGED"): self.accept(prepared)
        self.accept(); state = self.memory.snapshot()
        row = next(row for row in state["source_checks"].values() if row.get("kind") == "functional-alias-v1")
        row["reason"] = "Rewritten decision"
        with self.assertRaisesRegex(SafetyError, "RECORD_INVALID"): alias_projection(state)

    def test_different_or_unclear_never_append_an_equivalence(self):
        before = self.memory.snapshot()
        for decision in ("different", "unclear"):
            self.assertFalse(self.accept(decision=decision)["applied"])
        self.assertEqual(self.memory.snapshot(), before)

    def pool(self):
        values = allocation_fixture.products(10, 100)
        for cid in ("bear", "bird"):
            values.append(dict(deepcopy(values[0]), concept_id=cid, category="Home and Decor"))
        return values

    def test_allocator_does_not_call_an_old_alias_new_or_mix_its_market_facts(self):
        self.accept(); state = self.memory.snapshot()
        old = [{"concept_id": "bear", "client_id": "default", "issue_id": "old", "status": "published"}]
        result = allocate_issues(self.pool(), [allocation_fixture.DATES[0]], old, [], identity_state=state)
        self.assertEqual(result["issues"][0]["counts"], {"products": 10, "new": 10, "updates": 0})
        self.assertTrue(all(p["concept_id"] not in {"bear", "bird"} for p in result["issues"][0]["products"]))
        aliases = alias_projection(state)
        self.assertEqual(_published(old, aliases=aliases), {"bear", "bird"})
        self.assertEqual(_published(old, "other", aliases=aliases), set())

    def test_weaker_member_cannot_hide_usable_alias_and_only_one_is_selected(self):
        self.accept(); values = self.pool()
        next(p for p in values if p["concept_id"] == "bear")["kpi_states"] = ["Does not meet"] * 13
        result = allocate_issues(values, [allocation_fixture.DATES[0]], [], [], identity_state=self.memory.snapshot())
        ids = {p["concept_id"] for p in result["issues"][0]["products"]}
        self.assertIn("bird", ids); self.assertNotIn("bear", ids)
        self.assertIn("identity_context_hash", result["issues"][0])

    def test_a_future_alias_hold_blocks_other_members_without_blocking_unrelated_products(self):
        self.accept(); state = self.memory.snapshot()
        holds = [{"issue_id": "other_issue", "client_id": "default", "state": "reserved", "date": allocation_fixture.DATES[1],
                  "products": [{"concept_id": "bird", "status": "New"}]}]
        result = allocate_issues(self.pool(), [allocation_fixture.DATES[0]], [], holds, identity_state=state)
        self.assertEqual(result["issues"][0]["counts"]["products"], 10)
        self.assertTrue({"bear", "bird"}.isdisjoint(p["concept_id"] for p in result["issues"][0]["products"]))

    def test_published_report_is_exposure_not_a_permanent_future_hold(self):
        new, updated = allocation_fixture.products(8), allocation_fixture.products(2, 100)
        for p in updated:
            p.update(material_update_reviewed=True, update_reason="New supported fulfilment option.")
        history = allocation_fixture.history(updated)
        holds = [{"issue_id": "oldissue", "client_id": "default", "state": "published", "products": updated}]
        self.assertEqual(_held(holds, history=history), set())
        self.assertEqual(_published(history), {p["concept_id"] for p in updated})
        issue = allocate_issues(new + updated, [allocation_fixture.DATES[0]], history, holds)["issues"][0]
        self.assertEqual(issue["counts"], {"products": 10, "new": 8, "updates": 2})


if __name__ == "__main__": unittest.main()
