"""Full route display replay from a bounded private packet; no collectors."""

from copy import deepcopy
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from product_radar.market_validation import functional_market_context
from product_radar.models import SafetyError, canonical, digest
from product_radar.report_selection import build_report_pool, _url
from product_radar.report_source_facts import (coverage_rows, freeze_functional_route_proof,
    functional_route_proof_state, source_route_facts, validate_functional_route_proof)
from product_radar.source_coverage import execute_source_run, prepare_source_tasks
from test_functional_source_market import functional_pool_fixture
from test_source_interpretation import NOW


class FunctionalRouteProofTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(); self.addCleanup(temporary.cleanup)
        self.memory, ids = functional_pool_fixture(Path(temporary.name), count=1)
        self.cid = ids[0]

    def product(self):
        pool = build_report_pool(self.memory, as_of=NOW, recommendation_contract="functional-type-examples-v1")
        product = deepcopy(pool["facts_by_concept"][self.cid])
        product["functional_route_proof"] = freeze_functional_route_proof(self.memory.snapshot(), self.cid, as_of=NOW)
        return product

    def derived(self, state, *, as_of=NOW):
        context = functional_market_context(state, self.cid, as_of=as_of)
        relevant = {ref: state["observations"][ref] for ref in context["matches"] if ref in state["observations"]}
        route_product = {"market": context["market"], "functional_market_contract": context["contract"],
                         "functional_market_context": context}
        rows = source_route_facts(state, self.cid, coverage_rows(None, state, [self.cid], as_of)[self.cid],
                                  route_product, relevant, as_of)
        for row in rows:
            row["url"] = _url(row["url"])
        return rows

    def test_replays_every_route_detail_and_measurement_without_memory_or_files(self):
        product = self.product()
        with patch.object(self.memory, "snapshot", side_effect=AssertionError("No live read permitted")):
            self.assertEqual(validate_functional_route_proof(product), product["source_route_checks"])
        state = functional_route_proof_state(product["functional_route_proof"])
        self.assertEqual(self.derived(state), product["source_route_checks"])
        self.assertEqual(len(product["source_route_checks"]), 14)

    def test_changing_valid_display_numbers_or_words_is_rejected(self):
        product = self.product()
        for change in ("detail", "outcome", "measurement", "observed_at"):
            with self.subTest(change=change):
                changed = deepcopy(product)
                row = next(row for row in changed["source_route_checks"] if row["route"] == "amazon-bestsellers")
                if change == "detail":
                    row["detail"] = "A different plausible sentence."
                elif change == "outcome":
                    row.update(outcome="blocked", state="Check blocked")
                elif change == "measurement":
                    row["measurements"][0]["value"] += 1
                else:
                    row["observed_at"] = "2026-10-08T11:00:00+00:00"
                with self.assertRaisesRegex(SafetyError, "FUNCTIONAL_ROUTE_DISPLAY_CHANGED"):
                    validate_functional_route_proof(changed)

    def test_original_task_and_receipt_are_selected_and_latest_failed_attempt_replayed(self):
        import test_meta_source_context as meta_fixtures
        from product_radar.functional_types import effective_functional_definition
        meta_fixtures.MetaSourceContextTests.setUp(self)
        self.cid = "first"
        meta_fixtures.MetaSourceContextTests.review(self)
        later = "2026-10-08T11:00:00+00:00"
        prepare_source_tasks(self.memory, [{"route": "meta-ads", "request": {"query": "draining caddy holder", "country": "IN"},
            "concept_ids": [self.cid]}], run_id="latest-real-failure", as_of=later)
        def blocked(context):
            context["record_dispatch"]()
            return {"schema_version": 1, "route": context["route"], "acquisition_id": context["acquisition_id"],
                "observed_at": later, "state": "blocked", "dispatch_started": True, "reference": "actual/receipt.json",
                "capture_ids": [], "observation_ids": [], "target_results": [], "reason": "Synthetic actual access refusal.",
                "retryable": True, "failure_scope": "route"}
        execute_source_run(self.memory, "latest-real-failure", handlers={"meta-ads": blocked}, as_of=later,
            bounded_call=lambda callback, context, timeout: callback(context))
        state = self.memory.snapshot()
        product = {"concept_id": self.cid, "description": deepcopy(state["concepts"][self.cid]["description"]),
            "example_as_of": later, "functional_definition": effective_functional_definition(state, self.cid),
            "source_route_contract": "saved-source-routes-v2", "source_route_checks": self.derived(state, as_of=later),
            "functional_route_proof": freeze_functional_route_proof(state, self.cid, as_of=later)}
        self.assertEqual(validate_functional_route_proof(product), product["source_route_checks"])
        row = next(row for row in product["source_route_checks"] if row["route"] == "meta-ads")
        self.assertEqual(row["outcome"], "completed_relevant")
        self.assertEqual(row["last_attempt"]["outcome"], "blocked")
        self.assertIn("earlier saved relevant results are retained", row["detail"])
        proof = product["functional_route_proof"]
        self.assertTrue(set(row["task_ids"]) <= set(proof["records"]["query_tasks"]))
        self.assertTrue(set(row["receipt_ids"]) <= set(proof["records"]["source_checks"]))

    def test_unrelated_history_is_not_frozen(self):
        state = self.memory.snapshot()
        state["concepts"]["unrelated"] = {"status": "active", "description": {"label": "Unrelated"}}
        state["source_checks"]["unrelated-check"] = {"kind": "some-unknown-kind", "concept_id": "unrelated"}
        state["jobs"]["unrelated-job"] = {"request": {"role": "G", "payload": {"secret": "not selected"}}}
        proof = freeze_functional_route_proof(state, self.cid, as_of=NOW)
        self.assertNotIn("unrelated", proof["records"]["concepts"])
        self.assertNotIn("unrelated-check", proof["records"]["source_checks"])
        self.assertNotIn("unrelated-job", proof["records"]["jobs"])

    def test_nested_meta_context_dependencies_are_portable(self):
        import test_meta_source_context as meta_fixtures
        meta_fixtures.MetaSourceContextTests.setUp(self)
        self.cid = "first"
        meta_fixtures.MetaSourceContextTests.review(self)
        state = self.memory.snapshot()
        proof = freeze_functional_route_proof(state, self.cid, as_of=NOW)
        replay = functional_route_proof_state(proof)
        self.assertIn(self.task_id, replay["query_tasks"])
        self.assertIn(self.receipt_id, replay["source_checks"])
        self.assertEqual(self.derived(replay), self.derived(state))

    def test_capsule_hash_and_missing_dependency_are_not_silently_trusted(self):
        product = self.product()
        broken = deepcopy(product)
        proof = broken["functional_route_proof"]
        proof["records"]["observations"].clear()
        proof["proof_hash"] = digest({name: value for name, value in proof.items() if name != "proof_hash"})
        with self.assertRaises(SafetyError):
            validate_functional_route_proof(broken)
        broken = deepcopy(product)
        broken["functional_route_proof"]["proof_hash"] = "0" * 64
        with self.assertRaisesRegex(SafetyError, "FUNCTIONAL_ROUTE_PROOF_INVALID"):
            validate_functional_route_proof(broken)

    def test_explicit_question_dependency_on_actual_batch_run_is_not_dropped(self):
        from product_radar.functional_types import effective_functional_definition
        from product_radar.research_questions import prepare_research_questions
        from product_radar.research_scope import accept_scoped_evidence
        from product_radar.research_memory import key
        prepare_source_tasks(self.memory, [{"route": "meta-ads", "request": {"query": "caddy", "country": "IN"},
            "concept_ids": [self.cid]}], run_id="actual-local-plan", as_of=NOW)
        state = self.memory.snapshot()
        run_id = key("source-run", "actual-local-plan")
        meaning = effective_functional_definition(state, self.cid)
        prepared = prepare_research_questions(self.memory, [{"subject": {"kind": "concept", "concept_id": self.cid,
            "definition_hash": meaning["meaning_hash"]}, "route": "amazon-bestsellers", "purpose": "validation",
            "request": {"query": "caddy", "country": "IN"},
            "dependencies": [{"table": "batch_runs", "id": run_id, "hash": digest(state["batch_runs"][run_id])}]}], as_of=NOW)
        question_id = prepared["question_ids"][0]
        context = functional_market_context(self.memory.snapshot(), self.cid, as_of=NOW)
        row = next(row for row in context["scoped_evidence"] if row["route"] == "amazon-bestsellers")
        accept_scoped_evidence(self.memory, self.cid, route=row["route"], scope=row["scope"],
            relationship=row["relationship"], support=row["support"], references=row["references"],
            usable_fields=[{"reference": field["reference"], "path": field["path"]} for field in row["usable_fields"]],
            limitations=row["limitations"], reason=row["reason"], reviewer="synthetic", as_of=NOW,
            expected_meaning_hash=meaning["meaning_hash"], question_id=question_id)
        state = self.memory.snapshot()
        proof = freeze_functional_route_proof(state, self.cid, as_of=NOW)
        self.assertIn(run_id, proof["records"]["batch_runs"])
        replay = functional_route_proof_state(proof)
        self.assertEqual(replay["batch_runs"][run_id], state["batch_runs"][run_id])
        self.assertEqual(self.derived(replay), self.derived(state))

    def test_canonical_roundtrip_preserves_original_equal_date_and_task_order(self):
        from product_radar.discovery import latest_observations
        from product_radar.research_memory import import_capture
        from product_radar.research_sources import map_research_rows
        from test_research_sources import amazon_row, request
        questions = [{"route": "meta-ads", "request": {"query": query, "country": "IN"},
            "concept_ids": [self.cid]} for query in ("draining sink caddy", "draining caddy holder")]
        prepared = prepare_source_tasks(self.memory, questions, run_id="same-date-originals", as_of=NOW)
        def blocked(context):
            context["record_dispatch"]()
            return {"schema_version": 1, "route": context["route"], "acquisition_id": context["acquisition_id"],
                "observed_at": NOW, "state": "blocked", "dispatch_started": True, "reference": "synthetic/receipt.json",
                "capture_ids": [], "observation_ids": [], "target_results": [], "reason": "Synthetic actual refusal.",
                "retryable": True, "failure_scope": "query"}
        execute_source_run(self.memory, "same-date-originals", handlers={"meta-ads": blocked}, as_of=NOW,
            bounded_call=lambda callback, context, timeout: callback(context))
        cards = [dict(amazon_row(), title="Different synthetic equal-date capture", asin="B000000000",
                      link="https://www.amazon.in/dp/B000000000")]
        import_capture(self.memory, source="amazon", acquisition_id="equal-date-replacement", observed_at=NOW,
            mapped=map_research_rows(cards, request()), capture_hash=digest(cards), reference={"fixture": "synthetic"})
        state = self.memory.snapshot()
        # Both orders are genuine possible saved-state orders. Canonical JSON
        # would silently reverse these without the explicit selected order.
        for table in ("query_tasks", "observations"):
            state[table] = dict(sorted(state[table].items(), reverse=True))
        proof = freeze_functional_route_proof(state, self.cid, as_of=NOW)
        replay = functional_route_proof_state(json.loads(canonical(proof)))
        self.assertEqual(list(replay["query_tasks"]), sorted(prepared["task_ids"], reverse=True))
        self.assertEqual(list(latest_observations(replay["observations"], NOW)),
                         list(latest_observations(state["observations"], NOW)))
        self.assertEqual(self.derived(replay), self.derived(state))
        meta = next(row for row in self.derived(replay) if row["route"] == "meta-ads")
        self.assertEqual(meta["task_ids"], sorted(prepared["task_ids"], reverse=True))
        self.assertEqual(meta["last_attempt"]["receipt_id"], max(meta["receipt_ids"]))

    def test_record_order_must_be_exact_unique_selected_permutation(self):
        original = self.product()["functional_route_proof"]
        for mode in ("missing", "duplicate", "unknown", "table_missing"):
            with self.subTest(mode=mode):
                proof = deepcopy(original)
                order = proof["record_order"]["observations"]
                if mode == "missing":
                    order.pop()
                elif mode == "duplicate":
                    order.append(order[0])
                elif mode == "unknown":
                    order[0] = "not-an-original"
                else:
                    proof["record_order"].pop("observations")
                proof["proof_hash"] = digest({name: value for name, value in proof.items() if name != "proof_hash"})
                with self.assertRaisesRegex(SafetyError, "FUNCTIONAL_ROUTE_PROOF_INVALID"):
                    functional_route_proof_state(proof)


if __name__ == "__main__":
    unittest.main()
