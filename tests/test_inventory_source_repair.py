"""Narrow due-source servicing through the existing controller; offline only."""

from copy import deepcopy
from datetime import timedelta
import unittest
from unittest.mock import patch

from product_radar.inventory_intake import _new_work
from product_radar.marketplace_browser import capture_marketplace
from product_radar.models import SafetyError, digest, timestamp
from product_radar.source_coverage import (create_marketplace_handlers, execute_source_run,
    prepare_inventory_source_repair, prepare_source_tasks, source_access_context)
from test_marketplace_browser import snapshot, transport
import test_shared_marketplace_acquisitions as fixtures


NOW = fixtures.NOW
DUE = (timestamp(NOW) + timedelta(days=7)).isoformat()
LATER = (timestamp(NOW) + timedelta(days=8)).isoformat()


class InventorySourceRepairTests(unittest.TestCase):
    def setUp(self):
        self.fixture = fixtures.SharedMarketplaceTests(); self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.memory = self.fixture.memory
        self.handlers = self.fixture.handlers()
        self.fixture.prepare(self.handlers)
        self.fixture.checks(self.handlers)
        state = self.memory.snapshot()
        self.original_id, self.original = next((ref, row) for ref, row in state["query_tasks"].items()
                                               if row.get("kind") == "all_source_task_v1")

    def work(self, *, source=True, due=DUE, task_id=None):
        state = self.memory.snapshot(); tid = task_id or self.original_id; task = state["query_tasks"][tid]
        refs = [{"table": "observations", "id": ref, "hash": digest(row)} for ref, row in state["observations"].items()]
        binding = {"route": task["route"], "request": deepcopy(task["request"]), "original_task_id": tid,
            "original_acquisition_id": task.get("acquisition_id"), "expired_support_refs": refs if due else [],
            "refresh_due_at": due, "refresh_reason": "Selected original support expires before release." if due else None} if source else None
        work = _new_work(state, "default", "sink", "source", [{"code": "support_expired"}], None, NOW, source=binding)
        self.memory.put("batch_runs", work["id"], work)
        return work

    def late_handlers(self):
        def capture(req, aid, **options):
            self.fixture.calls.append((aid, deepcopy(req)))
            with patch("product_radar.marketplace_browser.utc_now", return_value=LATER):
                return capture_marketplace(req, aid, **options,
                    snapshot_reader=lambda _: transport(snapshot(req), navigation_started_at=LATER))
        return create_marketplace_handlers(self.memory, data_root=self.fixture.source_root,
                                           capture=capture, shared_acquisitions=True)

    def execute(self, result, handlers=None, *, at=LATER):
        return execute_source_run(self.memory, result["run_id"], handlers=handlers or self.late_handlers(),
            as_of=at, task_ids=result["task_ids"], shared_marketplace=True,
            bounded_call=lambda callback, context, timeout: callback(context))

    def test_prepares_one_due_linked_run_idempotently_without_launch(self):
        work = self.work(); before = self.memory.snapshot()
        result = prepare_inventory_source_repair(self.memory, work, as_of=LATER)
        self.assertEqual(len(result["task_ids"]), 1)
        self.assertEqual(len(self.fixture.calls), 1)
        self.assertEqual(self.memory.snapshot()["query_tasks"][self.original_id], before["query_tasks"][self.original_id])
        saved = self.memory.snapshot_with_provenance()
        self.assertEqual(prepare_inventory_source_repair(self.memory, work, as_of=LATER), result)
        self.assertEqual(self.memory.snapshot_with_provenance(), saved)

    def test_due_refresh_reaches_actual_owner_once_and_preserves_original(self):
        before = self.memory.snapshot(); result = prepare_inventory_source_repair(self.memory, self.work(), as_of=LATER)
        outcome = self.execute(result)
        self.assertEqual(outcome["handler_calls"], 1, outcome)
        self.assertEqual(len(self.fixture.calls), 2)
        self.assertEqual(self.memory.snapshot()["query_tasks"][self.original_id], before["query_tasks"][self.original_id])
        owners = self.fixture.owners()
        self.assertEqual(sorted(row["binding"]["generation"] for row in owners.values()), [1, 2])
        self.assertEqual({row["observed_at"] for row in self.memory.snapshot()["captures"].values()}, {NOW, LATER})
        self.execute(result)
        self.assertEqual(len(self.fixture.calls), 2)

    def test_future_expiry_waits_and_missing_request_creates_nothing(self):
        work = self.work(); before = self.memory.snapshot()
        result = prepare_inventory_source_repair(self.memory, work, as_of=NOW)
        self.assertEqual((result["state"], result["next_retry_at"]), ("waiting", DUE))
        self.assertEqual(self.memory.snapshot(), before)
        missing = self.work(source=False)
        result = prepare_inventory_source_repair(self.memory, missing, as_of=LATER)
        self.assertEqual((result["state"], result["task_ids"]), ("waiting_input", []))

    def test_uncertain_original_is_serviced_saved_only_and_subset_excludes_other_tasks(self):
        planned = prepare_source_tasks(self.memory, [{"route": "amazon-search", "request": {"query": "other caddy", "country": "IN"},
            "concept_ids": ["sink"]}, {"route": "flipkart-search", "request": {"query": "other caddy", "country": "IN"},
            "concept_ids": ["sink"]}], run_id="uncertain-run", as_of=NOW)
        target = next(tid for tid in planned["task_ids"] if self.memory.snapshot()["query_tasks"][tid]["route"] == "amazon-search")
        execute_source_run(self.memory, "uncertain-run", handlers=self.fixture.handlers(failure=TimeoutError("synthetic")),
            as_of=NOW, task_ids=[target], shared_marketplace=True,
            bounded_call=lambda callback, context, timeout: callback(context))
        original = self.memory.snapshot()["query_tasks"][target]
        work = self.work(task_id=target, due=None)
        result = prepare_inventory_source_repair(self.memory, work, as_of=LATER)
        calls = len(self.fixture.calls)
        self.assertEqual(result["task_ids"], [target])
        self.execute(result)
        self.assertEqual(len(self.fixture.calls), calls)
        self.assertEqual(self.memory.snapshot()["query_tasks"][target]["acquisition_id"], original["acquisition_id"])
        other = next(tid for tid in planned["task_ids"] if tid != target)
        self.assertEqual(self.memory.snapshot()["query_tasks"][other]["status"], "prepared")

    def test_current_meaning_and_source_proof_are_rechecked_before_dispatch(self):
        work = self.work(); result = prepare_inventory_source_repair(self.memory, work, as_of=LATER)
        state = self.memory.snapshot(); concept = state["concepts"]["sink"]
        self.memory.put("concepts", "sink", dict(concept, description=dict(concept["description"], mechanism="changed")), previous=concept)
        calls = len(self.fixture.calls)
        outcome = self.execute(result)
        self.assertEqual(len(self.fixture.calls), calls)
        self.assertTrue(any(row.get("reason") == "SOURCE_INVENTORY_REPAIR_INVALID" for row in outcome["results"]), outcome)

    def test_repair_refresh_context_uses_canonical_frozen_work_not_caller_fields(self):
        result = prepare_inventory_source_repair(self.memory, self.work(), as_of=LATER)
        task = self.memory.snapshot()["query_tasks"][result["task_ids"][0]]
        context = source_access_context(self.memory.snapshot(), task, as_of=LATER)
        self.assertEqual(context["refresh_due_at"], DUE)
        self.assertEqual(context["refresh_reason"], "Selected original support expires before release.")
        damaged = self.memory.snapshot(); damaged["batch_runs"][task["inventory_repair"]["work_id"]]["binding"]["source"]["request"]["query"] = "different"
        with self.assertRaisesRegex(SafetyError, "INVENTORY_BINDING_INVALID"):
            source_access_context(damaged, task, as_of=LATER)

    def test_dispatcher_refuses_foreign_or_duplicate_task_subset(self):
        for scope in (["foreign-task"], [self.original_id, self.original_id]):
            with self.subTest(scope=scope), self.assertRaisesRegex(SafetyError, "SOURCE_EXECUTION_TASK_SCOPE_INVALID"):
                execute_source_run(self.memory, self.original["run_id"], task_ids=scope, as_of=LATER)

    def test_existing_other_original_cannot_be_used_as_refresh_proof(self):
        other = prepare_source_tasks(self.memory, [{"route": "amazon-search", "request": {"query": "other holder", "country": "IN"},
            "concept_ids": ["sink"]}], run_id="other-run", as_of=NOW)
        execute_source_run(self.memory, "other-run", handlers=self.handlers, as_of=NOW, shared_marketplace=True,
                           bounded_call=lambda callback, context, timeout: callback(context))
        state = self.memory.snapshot(); task = state["query_tasks"][other["task_ids"][0]]
        source = {"route": self.original["route"], "request": deepcopy(self.original["request"]),
            "original_task_id": self.original_id, "original_acquisition_id": self.original["acquisition_id"],
            "expired_support_refs": [{"table": "observations", "id": ref, "hash": digest(row)}
                for ref, row in state["observations"].items() if row["acquisition_id"] == task["acquisition_id"]],
            "refresh_due_at": DUE, "refresh_reason": "Synthetic wrong original proof."}
        self.assertTrue(source["expired_support_refs"])
        work = _new_work(state, "default", "sink", "source", [{"code": "support_expired"}], None, NOW, source=source)
        self.memory.put("batch_runs", work["id"], work)
        with self.assertRaisesRegex(SafetyError, "SOURCE_INVENTORY_SUPPORT_ORIGINAL_MISMATCH"):
            prepare_inventory_source_repair(self.memory, work, as_of=LATER)

    def test_prepared_without_id_follows_actual_original_through_uncertainty(self):
        planned = prepare_source_tasks(self.memory, [{"route": "amazon-search", "request": {"query": "new holder", "country": "IN"},
            "concept_ids": ["sink"]}], run_id="unallocated-run", as_of=NOW)
        tid = planned["task_ids"][0]
        work = self.work(task_id=tid, due=None)
        self.assertIsNone(work["binding"]["source"]["original_acquisition_id"])
        first = prepare_inventory_source_repair(self.memory, work, as_of=NOW)
        self.assertEqual(first["task_ids"], [tid])
        self.execute(first, self.fixture.handlers(failure=TimeoutError("synthetic uncertainty")), at=NOW)
        actual = self.memory.snapshot()["query_tasks"][tid]["acquisition_id"]
        replay = prepare_inventory_source_repair(self.memory, work, as_of=LATER)
        self.assertEqual(replay["task_ids"], [tid])
        before = len(self.fixture.calls)
        self.execute(replay)
        self.assertEqual(len(self.fixture.calls), before)
        self.assertEqual(self.memory.snapshot()["query_tasks"][tid]["acquisition_id"], actual)

    def test_prepared_without_id_reuses_completed_original_after_progress_interruption(self):
        planned = prepare_source_tasks(self.memory, [{"route": "amazon-search", "request": {"query": "new caddy", "country": "IN"},
            "concept_ids": ["sink"]}], run_id="first-capture-run", as_of=NOW)
        tid = planned["task_ids"][0]; work = self.work(task_id=tid, due=None)
        first = prepare_inventory_source_repair(self.memory, work, as_of=NOW)
        self.execute(first, self.handlers, at=NOW)
        before = len(self.fixture.calls)
        replay = prepare_inventory_source_repair(self.memory, work, as_of=LATER)
        self.assertTrue(replay["source_already_collected"])
        self.execute(replay)
        self.assertEqual(len(self.fixture.calls), before)

    def test_unallocated_binding_cannot_adopt_an_arbitrary_changed_acquisition(self):
        from product_radar.source_coverage import validate_inventory_source_original
        planned = prepare_source_tasks(self.memory, [{"route": "amazon-search", "request": {"query": "new basket", "country": "IN"},
            "concept_ids": ["sink"]}], run_id="arbitrary-id-run", as_of=NOW)
        tid = planned["task_ids"][0]; work = self.work(task_id=tid, due=None)
        state = self.memory.snapshot()
        state["query_tasks"][tid].update(acquisition_id="unrelated-original", generation=1)
        with self.assertRaisesRegex(SafetyError, "SOURCE_INVENTORY_ORIGINAL_CHANGED"):
            validate_inventory_source_original(state, work["binding"]["source"], concept_id="sink")

