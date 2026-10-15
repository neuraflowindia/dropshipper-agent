"""One readiness decision reuses its pure per-type reads: same answer, once per type."""

from collections import Counter
from contextlib import nullcontext
import unittest
from unittest.mock import patch

import test_catalogue_scope_review as fixtures
from product_radar import functional_alias_review, functional_types, read_memo, research_scope
from product_radar.models import canonical
from product_radar.programme import prepare_programme, programme_turn_readiness


class ProgrammeReadinessReuseTests(unittest.TestCase):
    def setUp(self):
        fixture = fixtures.CatalogueScopeTests(); fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        self.fixture, self.memory, self.cid = fixture, fixture.memory, fixture.cid
        self.record = prepare_programme(self.memory, "readiness-reuse", source_run_id="reuse-source",
            concept_ids=[self.cid], as_of=fixtures.waves.NOW)
        self.runner = fixtures.ScopeRunner()

    def readiness(self, state=None):
        def discovery(*args):
            raise AssertionError("Pure readiness must not run discovery")
        discovery.discovery_catalogue = self.fixture.catalogue
        return programme_turn_readiness(self.memory, self.record, root=self.fixture.root, runner=self.runner,
            discovery_step=discovery, as_of=fixtures.waves.NOW, state=state)

    def counted(self, module, name):
        calls, original = Counter(), getattr(module, name)
        def wrapper(*args, **kwargs):
            # Keyed by the exact state object: a derived view is a new key.
            calls[(id(args[0]), canonical([list(args[1:]), kwargs]))] += 1
            return original(*args, **kwargs)
        return calls, patch.object(module, name, wrapper)

    def test_reused_answer_equals_the_answer_computed_without_reuse(self):
        state = self.memory.snapshot()
        reused = self.readiness(state)
        with patch.object(read_memo, "pure_read_scope", nullcontext):
            fresh = self.readiness(state)
        self.assertEqual(canonical(reused), canonical(fresh))
        self.assertEqual(state, self.memory.snapshot())  # the supplied state is never edited
        self.assertIsNone(read_memo._ACTIVE.get())       # nothing is kept after the decision
        self.assertEqual(self.runner.calls, [])

    def test_each_type_is_derived_once_per_decision(self):
        evidence, evidence_patch = self.counted(research_scope, "_validated_scoped_evidence")
        roles, roles_patch = self.counted(functional_types, "_functional_role_context")
        members, members_patch = self.counted(functional_types, "_membership_records_uncached")
        with evidence_patch, roles_patch, members_patch:
            self.readiness()
        self.assertTrue(evidence and roles and members, (evidence, roles, members))
        for calls in (evidence, roles, members):
            self.assertEqual(max(calls.values()), 1, calls)

    def test_role_context_callers_still_get_independent_copies(self):
        state = self.memory.snapshot()
        fresh = functional_types.functional_role_context(state, self.cid, role="E")
        with read_memo.pure_read_scope():
            first = functional_types.functional_role_context(state, self.cid, role="E")
            first["definition"].clear(); first["examples"].clear()
            self.assertEqual(functional_types.functional_role_context(state, self.cid, role="E"), fresh)

    def test_alias_review_rows_not_saved_in_the_state_are_validated_afresh(self):
        state = self.memory.snapshot()
        rid, row = "functional-alias-semantic-review-unsaved", {"kind": "not-a-review"}
        with read_memo.pure_read_scope():
            for _ in range(2):
                with self.assertRaisesRegex(Exception, "FUNCTIONAL_ALIAS_REVIEW_RECORD_INVALID"):
                    functional_alias_review._review_record(state, rid, row)
            self.assertFalse(any(name == "functional_alias_review_record" for name, *_ in read_memo._ACTIVE.get()))


if __name__ == "__main__":
    unittest.main()
