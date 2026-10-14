"""Per-build reuse of pure derived reads: same answers, never stale across saves."""

from contextlib import nullcontext
from copy import deepcopy
from pathlib import Path
import sys
import tempfile
import threading
import unittest
from types import SimpleNamespace
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from product_radar import read_memo
from product_radar.functional_types import effective_functional_definition
from product_radar.models import SafetyError, canonical, digest
from product_radar.read_memo import pure_read_scope, remember, same_object_digest
from product_radar.report_selection import build_report_pool
from product_radar.research_scope import validated_scoped_evidence
import test_research_scope as scope_fixture
from test_functional_source_market import functional_pool_fixture, NOW


class _Pending(SafetyError):
    pass


class RememberTests(unittest.TestCase):
    def counter(self, value=None, error=None):
        calls = []

        def compute():
            calls.append(1)
            if error is not None:
                raise error
            return deepcopy(value) if value is not None else {"n": len(calls), "rows": [1, 2]}
        return calls, compute

    def test_outside_a_scope_every_call_recomputes(self):
        calls, compute = self.counter()
        state = {"concepts": {}}
        self.assertEqual(remember("r", state, ("a",), compute)["n"], 1)
        self.assertEqual(remember("r", state, ("a",), compute)["n"], 2)
        self.assertIsNone(read_memo._ACTIVE.get())

    def test_same_objects_and_arguments_reuse_one_answer_inside_a_scope(self):
        calls, compute = self.counter()
        state = {"concepts": {}, "source_checks": {}}
        with pure_read_scope():
            first = remember("r", state, ("a",), compute)
            second = remember("r", state, ("a",), compute)
            other_args = remember("r", state, ("b",), compute)
            other_name = remember("q", state, ("a",), compute)
        self.assertEqual(first, second)
        self.assertEqual(len(calls), 3)
        self.assertEqual(other_args["n"], 2)
        self.assertEqual(other_name["n"], 3)

    def test_a_new_snapshot_or_replaced_table_is_a_new_key(self):
        calls, compute = self.counter()
        state = {"concepts": {"c": {"status": "provisional"}}, "source_checks": {}}
        with pure_read_scope():
            remember("r", state, (), compute)
            # A saved change produces a new verified copy: new objects, new key.
            remember("r", deepcopy(state), (), compute)
            replaced = dict(state, concepts={})
            remember("r", replaced, (), compute)
            state["source_checks"] = {}
            remember("r", state, (), compute)
            remember("r", state, (), compute)
        self.assertEqual(len(calls), 4)

    def test_named_tables_bind_only_those_tables(self):
        calls, compute = self.counter()
        checks = {"x": {"kind": "k"}}
        first = {"concepts": {}, "source_checks": checks}
        second = {"concepts": {"other": {}}, "source_checks": checks}
        with pure_read_scope():
            remember("r", first, (), compute, tables=("source_checks",))
            remember("r", second, (), compute, tables=("source_checks",))
            remember("r", dict(first, source_checks=dict(checks)), (), compute, tables=("source_checks",))
        self.assertEqual(len(calls), 2)

    def test_nothing_survives_the_scope(self):
        calls, compute = self.counter()
        state = {"concepts": {}}
        with pure_read_scope():
            remember("r", state, (), compute)
        self.assertIsNone(read_memo._ACTIVE.get())
        with pure_read_scope():
            remember("r", state, (), compute)
        remember("r", state, (), compute)
        self.assertEqual(len(calls), 3)

    def test_scope_closes_after_an_error_and_nested_scopes_share_the_outer_one(self):
        calls, compute = self.counter()
        state = {"concepts": {}}
        with self.assertRaises(RuntimeError), pure_read_scope():
            raise RuntimeError("stop")
        self.assertIsNone(read_memo._ACTIVE.get())
        with pure_read_scope():
            outer = read_memo._ACTIVE.get()
            with pure_read_scope():
                self.assertIs(read_memo._ACTIVE.get(), outer)
                remember("r", state, (), compute)
            self.assertIs(read_memo._ACTIVE.get(), outer)
            remember("r", state, (), compute)
        self.assertEqual(len(calls), 1)

    def test_each_caller_gets_its_own_copy_unless_explicitly_shared(self):
        calls, compute = self.counter()
        state = {"concepts": {}}
        with pure_read_scope():
            first = remember("r", state, (), compute)
            first["rows"].append("edited by a caller")
            self.assertEqual(remember("r", state, (), compute)["rows"], [1, 2])
            shared = remember("s", state, (), compute, copy=False)
            self.assertIs(remember("s", state, (), compute, copy=False), shared)

    def test_plain_safety_error_is_remembered_with_its_exact_code(self):
        calls, compute = self.counter(error=SafetyError("CODE_ONE"))
        state = {"concepts": {}}
        with pure_read_scope():
            for _ in range(2):
                with self.assertRaises(SafetyError) as raised:
                    remember("r", state, (), compute)
                self.assertIs(type(raised.exception), SafetyError)
                self.assertEqual(str(raised.exception), "CODE_ONE")
        self.assertEqual(len(calls), 1)

    def test_other_errors_and_unhashable_arguments_are_never_remembered(self):
        state = {"concepts": {}}
        for error in (_Pending("WAIT"), KeyError("x"), TypeError("y")):
            calls, compute = self.counter(error=error)
            with pure_read_scope():
                for _ in range(2):
                    with self.assertRaises(type(error)):
                        remember("r", state, (), compute)
            self.assertEqual(len(calls), 2, type(error).__name__)
        calls, compute = self.counter()
        with pure_read_scope():
            remember("r", state, (["unhashable"],), compute)
            remember("r", state, (["unhashable"],), compute)
        self.assertEqual(len(calls), 2)

    def test_non_dict_state_is_computed_directly(self):
        calls, compute = self.counter()
        with pure_read_scope():
            remember("r", SimpleNamespace(), (), compute)
            remember("r", SimpleNamespace(), (), compute)
        self.assertEqual(len(calls), 2)

    def test_another_thread_never_sees_this_scope(self):
        seen = []
        with pure_read_scope():
            worker = threading.Thread(target=lambda: seen.append(read_memo._ACTIVE.get()))
            worker.start(); worker.join()
        self.assertEqual(seen, [None])

    def test_same_object_digest_is_the_plain_digest(self):
        first, second = {"a": [1, 2]}, {"a": [1, 3]}
        self.assertEqual(same_object_digest(first), digest(first))
        with pure_read_scope():
            self.assertEqual(same_object_digest(first), digest(first))
            self.assertEqual(same_object_digest(first), digest(first))
            self.assertEqual(same_object_digest(second), digest(second))
            self.assertEqual(same_object_digest(None), digest(None))
            self.assertEqual(same_object_digest("text"), digest("text"))
        # A changed object after the scope closed is hashed afresh.
        first["a"].append(3)
        self.assertEqual(same_object_digest(first), digest(first))


class ScopedReaderTests(unittest.TestCase):
    """Real scoped-evidence records; reuse equals a fresh, unscoped read."""

    setUp = scope_fixture.ScopeTests.setUp
    references = scope_fixture.ScopeTests.references
    fields = scope_fixture.ScopeTests.fields
    evidence = scope_fixture.ScopeTests.evidence

    def test_scoped_answers_equal_fresh_answers_and_are_independent_copies(self):
        self.evidence()
        state = self.memory.snapshot()
        fresh_evidence = validated_scoped_evidence(state, self.cid)
        fresh_meaning = effective_functional_definition(state, self.cid)
        self.assertEqual(len(fresh_evidence), 1)
        with pure_read_scope():
            first = validated_scoped_evidence(state, self.cid)
            self.assertEqual(first, fresh_evidence)
            first[0]["support"] = "edited by a caller"
            first[0]["references"].clear()
            self.assertEqual(validated_scoped_evidence(state, self.cid), fresh_evidence)
            meaning = effective_functional_definition(state, self.cid)
            meaning["included_distinctions"].append("edited")
            self.assertEqual(effective_functional_definition(state, self.cid), fresh_meaning)
            self.assertEqual(validated_scoped_evidence(state, self.cid, as_of=scope_fixture.OBSERVED), [])
        self.assertEqual(state, self.memory.snapshot())

    def test_a_saved_correction_is_seen_inside_the_same_scope(self):
        first = self.evidence()
        before = self.memory.snapshot()
        with pure_read_scope():
            self.assertEqual([row["id"] for row in validated_scoped_evidence(before, self.cid)], [first["id"]])
            corrected = self.evidence(support="not_positive", reason="Reviewed again more cautiously.",
                                      as_of=scope_fixture.LATER)
            after = self.memory.snapshot()
            rows = validated_scoped_evidence(after, self.cid)
            self.assertEqual([row["id"] for row in rows], [corrected["id"]])
            self.assertEqual(rows[0]["support"], "not_positive")
            # The older verified copy still answers for its own saved point.
            self.assertEqual([row["id"] for row in validated_scoped_evidence(before, self.cid)], [first["id"]])
        self.assertEqual(rows, validated_scoped_evidence(self.memory.snapshot(), self.cid))

    def test_changed_meaning_in_a_new_copy_is_not_served_from_the_old_one(self):
        self.evidence()
        original = self.memory.snapshot()
        changed = deepcopy(original)
        changed["concepts"][self.cid]["description"]["mechanism"] = "Powered water pump"
        with pure_read_scope():
            self.assertEqual(len(validated_scoped_evidence(original, self.cid)), 1)
            self.assertEqual(validated_scoped_evidence(changed, self.cid), [])
            self.assertNotEqual(effective_functional_definition(original, self.cid)["meaning_hash"],
                                effective_functional_definition(changed, self.cid)["meaning_hash"])

    def test_tampered_record_raises_the_same_code_every_time_inside_a_scope(self):
        result = self.evidence()
        state = self.memory.snapshot()
        state["source_checks"][result["id"]]["scope"] = "broader_context"
        with pure_read_scope():
            for _ in range(2):
                with self.assertRaisesRegex(SafetyError, "RECORD_HASH_MISMATCH"):
                    validated_scoped_evidence(state, self.cid)
        # Another type's reader still meets the same global envelope check.
        with pure_read_scope(), self.assertRaisesRegex(SafetyError, "RECORD_HASH_MISMATCH"):
            validated_scoped_evidence(state, "some-other-type")


class ReportPoolReuseTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(); self.addCleanup(temporary.cleanup)
        self.memory, self.ids = functional_pool_fixture(Path(temporary.name), count=2)

    def pools(self, source):
        built = {}
        for contract in ("functional-type-examples-v1", None):
            built[contract] = canonical(build_report_pool(source, as_of=NOW, recommendation_contract=contract))
        return built

    def uncached(self, source):
        with patch.object(read_memo, "pure_read_scope", nullcontext):
            return self.pools(source)

    def test_pool_equals_the_pool_built_without_reuse(self):
        pools = self.pools(self.memory)
        self.assertEqual(pools, self.uncached(self.memory))
        functional = build_report_pool(self.memory, as_of=NOW, recommendation_contract="functional-type-examples-v1")
        self.assertEqual(len(functional["eligible"]), 2)
        self.assertIsNone(read_memo._ACTIVE.get())

    def test_supplied_state_is_never_edited_by_a_build(self):
        state = self.memory.snapshot()
        before = digest(state)
        source = SimpleNamespace(snapshot=lambda: state, evidence_type=self.memory.evidence_type)
        self.assertEqual(self.pools(source), self.pools(self.memory))
        self.assertEqual(digest(state), before)

    def test_next_build_after_a_save_reflects_the_change(self):
        first = self.pools(self.memory)
        cid = self.ids[0]
        concept = self.memory.snapshot()["concepts"][cid]
        self.memory.put("concepts", cid, dict(concept, status="retired"), previous=concept)
        second = self.pools(self.memory)
        self.assertNotEqual(first, second)
        self.assertEqual(second, self.uncached(self.memory))
        pool = build_report_pool(self.memory, as_of=NOW, recommendation_contract="functional-type-examples-v1")
        self.assertEqual([row["concept_id"] for row in pool["eligible"]], self.ids[1:])

    def test_scope_is_closed_even_when_a_build_fails(self):
        with patch("product_radar.report_selection.candidate_summary", side_effect=SafetyError("STOP")):
            with self.assertRaisesRegex(SafetyError, "STOP"):
                build_report_pool(self.memory, as_of=NOW, recommendation_contract="functional-type-examples-v1")
        self.assertIsNone(read_memo._ACTIVE.get())


if __name__ == "__main__":
    unittest.main()
