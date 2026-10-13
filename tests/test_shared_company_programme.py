"""Independent installed programme/controller regressions, using local pages only."""

from copy import deepcopy
import unittest

import test_shared_company_pages as fixtures
from test_supplier_route_acquisition import receipt

from product_radar.company_pages import CompanyPagePending, company_unstarted_continuation
from product_radar.programme import _source_turn_readiness, load_programme, prepare_programme
from product_radar.research_memory import ResearchMemory, key
from product_radar.source_coverage import execute_source_run, prepare_source_tasks, source_access_context
from product_radar.storage import atomic_json
from product_radar.supplier_capture_store import load_saved_supplier_input
from product_radar.supplier_route_acquisition import create_supplier_handlers
from product_radar.work_turns import (DEFAULT_POLICY, bind_work_turn, close_work_turn,
                                      open_work_turn, reserve_source_callback)


class SharedCompanyProgrammeTests(unittest.TestCase):
    def setUp(self):
        fixture = fixtures.SharedCompanyTests()
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        self.fixture = fixture
        self.memory = fixture.memory
        self.root = fixture.root

    def handlers(self, pages=fixtures.forbidden, browser=fixtures.forbidden):
        return create_supplier_handlers(self.memory, http_reader=fixtures.forbidden,
            page_reader=pages, browser_reader=browser, shared_acquisitions=True,
            respect_access_waits=True)

    def execute(self, run, handlers, *, at=fixtures.NOW, **options):
        return execute_source_run(self.memory, run, handlers=handlers, as_of=at,
            bounded_call=lambda callback, context, _timeout: callback(context), **options)

    def task(self, run):
        return next((rid, row) for rid, row in self.memory.snapshot()["query_tasks"].items()
                    if row.get("run_id") == run and row.get("route") == "company-pages")

    def captures(self):
        return list((self.memory.root / "supplier_pages").glob("*/capture.json"))

    def restart(self):
        self.memory = ResearchMemory(self.memory.root, self.memory.journal, evidence_type="synthetic")

    def wait_company(self, run="company-programme-wait"):
        blocked = fixtures.OwnedReader(status=429)
        self.fixture.fetcher(blocked)({"id": "prior-host-refusal", "url": fixtures.URL})
        unseen = fixtures.URL + "?second-company-page=1"
        request = self.fixture.nominate("paper", [unseen, fixtures.OTHER])
        prepare_source_tasks(self.memory, [{"route": "company-pages", "request": request,
            "concept_ids": ["paper"]}], run_id=run, as_of=fixtures.NOW)
        healthy = fixtures.OwnedReader()
        first = self.execute(run, self.handlers(healthy))
        self.assertEqual(first["counts"], {"blocked": 1})
        self.assertEqual([item["url"] for item in healthy.calls], [fixtures.OTHER])
        self.assertEqual(len(self.captures()), 1)
        task_id, original = self.task(run)
        self.assertTrue(original["reconcile_required"])
        record = prepare_programme(self.memory, "company-programme", source_run_id=run,
                                   concept_ids=["paper"], as_of=fixtures.NOW)
        return unseen, healthy, task_id, original, record

    def test_late_native_original_wakes_real_programme_and_recovers_without_new_read(self):
        run = "company-programme-late"
        request = self.fixture.nominate("paper", [fixtures.URL, fixtures.OTHER])
        prepare_source_tasks(self.memory, [{"route": "company-pages", "request": request,
            "concept_ids": ["paper"]}], run_id=run, as_of=fixtures.NOW)
        http = fixtures.OwnedReader()
        def pages(item):
            value = http(item)
            if item["url"] == fixtures.URL:
                value.update(state="unavailable", text="", body_complete=True)
            return value
        browser_calls = []
        def pending_browser(item):
            browser_calls.append(deepcopy(item))
            value = receipt(item, body="", status=200)
            value.update(state="unavailable", owned_process_exited=False)
            return value
        first = self.execute(run, self.handlers(pages, pending_browser))
        self.assertEqual(first["counts"], {"blocked": 1})
        task_id, original = self.task(run)
        self.assertEqual(len(self.captures()), 1)
        self.assertEqual(load_saved_supplier_input(self.memory, self.captures()[0])["item"]["pages"][0]["url"], fixtures.OTHER)
        record = prepare_programme(self.memory, "company-programme", source_run_id=run,
                                   concept_ids=["paper"], as_of=fixtures.NOW)
        self.restart()
        record = load_programme(self.memory, record["programme_id"])
        replay_handlers = self.handlers()
        before = _source_turn_readiness(self.memory, self.memory.snapshot(), record,
                                        replay_handlers, fixtures.LATER)
        self.assertFalse(before["local_ready"])
        self.assertFalse(before["callback_ready"])
        leaf_id = next(rid for rid, row in self.memory.snapshot()["batch_runs"].items()
            if row.get("kind") == "source-acquisition-owner-v1"
            and key("supplier-company-read", rid) == browser_calls[0]["id"])
        native = self.memory.root / "supplier_routes" / "company_shared" / leaf_id / "native"
        original_page = native.parent / "page.json"
        original_bytes = original_page.read_bytes()
        head = self.memory.snapshot_with_provenance()["head"]
        atomic_json(native / "native_result.json", {
            "navigation_started": True, "navigation_started_at": fixtures.NOW,
            "completed_at": fixtures.NOW, "owned_process_exited": True,
            "state": "captured", "navigation_status": 200,
            "snapshot": {"url": fixtures.URL, "html": "<p>Factory wrapping paper.</p>",
                         "text": "Factory wrapping paper.", "links": []}})
        atomic_json(native / "native_dispatch.json", {"url": fixtures.URL,
            "navigation_started": True, "navigation_started_at": fixtures.NOW})
        after = _source_turn_readiness(self.memory, self.memory.snapshot(), record,
                                       replay_handlers, fixtures.LATER)
        self.assertTrue(after["local_ready"])
        self.assertTrue(after["callback_ready"])
        self.assertNotEqual(before["signal"], after["signal"])
        self.assertEqual(self.memory.snapshot_with_provenance()["head"], head)
        resumed = self.execute(run, replay_handlers, at=fixtures.LATER)
        self.assertEqual(resumed["counts"], {"acquired": 1})
        current = self.memory.snapshot()["query_tasks"][task_id]
        self.assertFalse(current["reconcile_required"])
        self.assertEqual(current["acquisition_id"], original["acquisition_id"])
        self.assertEqual(len(self.captures()), 2)
        self.assertEqual(original_page.read_bytes(), original_bytes)
        self.assertEqual((len(http.calls), len(browser_calls)), (2, 1))
        self.assertEqual(self.memory.snapshot()["observations"], {})

    def test_due_unstarted_sibling_resumes_after_restart_under_one_callback(self):
        unseen, healthy, task_id, original, record = self.wait_company()
        healthy_path = self.captures()[0]
        healthy_bytes = healthy_path.read_bytes()
        before = _source_turn_readiness(self.memory, self.memory.snapshot(), record,
                                        self.handlers(), fixtures.NOW)
        self.assertFalse(before["callback_ready"])
        self.assertTrue(before["waits"])
        self.restart()
        record = load_programme(self.memory, record["programme_id"])
        later_reader = fixtures.OwnedReader(at=fixtures.LATER)
        later_handlers = self.handlers(later_reader)
        context = source_access_context(self.memory.snapshot(), self.memory.snapshot()["query_tasks"][task_id],
                                        as_of=fixtures.LATER)
        # An ordinary reconciliation is original-only, even after the host wait.
        with self.assertRaises(CompanyPagePending):
            later_handlers["company-pages"]["reconcile"](context)
        self.assertEqual(later_reader.calls, [])
        after = _source_turn_readiness(self.memory, self.memory.snapshot(), record,
                                       later_handlers, fixtures.LATER)
        self.assertTrue(after["callback_ready"])
        policy = dict(DEFAULT_POLICY, source_callbacks=1, exploration_slots=0)
        turn = open_work_turn(self.memory, "company-programme", at=fixtures.LATER, policy=policy)
        with bind_work_turn(self.memory, turn["id"]):
            resumed = self.execute(original["run_id"], later_handlers, at=fixtures.LATER,
                                   action_limit=1, shared_marketplace=True)
        self.assertEqual(resumed["counts"], {"acquired": 1})
        self.assertEqual([item["url"] for item in later_reader.calls], [unseen])
        current = self.memory.snapshot()["query_tasks"][task_id]
        self.assertFalse(current["reconcile_required"])
        for field in ("acquisition_id", "generation", "tries"):
            self.assertEqual(current[field], original[field], field)
        self.assertEqual(self.memory.snapshot()["batch_runs"][turn["id"]]["counters"]["source_callbacks"], 1)
        self.assertEqual(len(self.captures()), 2)
        self.assertEqual(healthy_path.read_bytes(), healthy_bytes)
        self.assertEqual(len(healthy.calls), 1)
        replay = self.execute(original["run_id"], self.handlers(), at=fixtures.LATER)
        self.assertEqual(replay["counts"], {"reused": 1})
        self.assertEqual(self.memory.snapshot()["observations"], {})

    def test_due_continuation_does_not_mint_extra_callback_capacity_on_restart(self):
        unseen, _healthy, task_id, original, _record = self.wait_company()
        policy = dict(DEFAULT_POLICY, source_callbacks=1, exploration_slots=0)
        turn = open_work_turn(self.memory, "company-programme", at=fixtures.NOW, policy=policy)
        with bind_work_turn(self.memory, turn["id"]):
            reserve_source_callback(self.memory, "already-used-source-slot", at=fixtures.NOW)
        self.restart()
        resumed_turn = open_work_turn(self.memory, "company-programme", at=fixtures.LATER, policy=policy)
        self.assertEqual(resumed_turn["id"], turn["id"])
        reader = fixtures.OwnedReader(at=fixtures.LATER)
        with bind_work_turn(self.memory, resumed_turn["id"]):
            blocked = self.execute(original["run_id"], self.handlers(reader), at=fixtures.LATER,
                                   action_limit=1, shared_marketplace=True)
        self.assertEqual(reader.calls, [])
        self.assertEqual(blocked["counts"], {"not_attempted": 1})
        self.assertEqual(blocked["results"][0]["reason"], "WORK_TURN_SOURCE_LIMIT")
        self.assertEqual(self.memory.snapshot()["query_tasks"][task_id]["acquisition_id"], original["acquisition_id"])
        self.assertEqual(self.memory.snapshot()["batch_runs"][turn["id"]]["counters"]["source_callbacks"], 1)
        close_work_turn(self.memory, turn["id"], at=fixtures.LATER, reason="Original finite callback allowance is used.")
        following = open_work_turn(self.memory, "company-programme", at=fixtures.LATER, policy=policy)
        with bind_work_turn(self.memory, following["id"]):
            complete = self.execute(original["run_id"], self.handlers(reader), at=fixtures.LATER,
                                    action_limit=1, shared_marketplace=True)
        self.assertEqual(complete["counts"], {"acquired": 1})
        self.assertEqual([item["url"] for item in reader.calls], [unseen])

    def test_unstarted_proof_excludes_same_task_orphan_not_currently_nominated(self):
        unseen, _healthy, task_id, _original, _record = self.wait_company()
        orphan = fixtures.URL + "?orphaned-company-page=1"
        with self.assertRaisesRegex(CompanyPagePending, "SOURCE_ACCESS_WAIT"):
            self.fixture.fetcher(fixtures.forbidden, owner=task_id)(
                {"id": "old-orphaned-url", "url": orphan})
        state = self.memory.snapshot()
        context = source_access_context(state, state["query_tasks"][task_id], as_of=fixtures.LATER)
        proof = company_unstarted_continuation(self.memory, context)
        self.assertIsNotNone(proof)
        urls = {state["batch_runs"][aid]["binding"]["request"]["url"] for aid in proof["leaf_owners"]}
        self.assertEqual(urls, {unseen})

    def test_continuation_rechecks_exact_unstarted_files_before_reading(self):
        _unseen, _healthy, task_id, original, _record = self.wait_company()
        state = self.memory.snapshot()
        context = source_access_context(state, state["query_tasks"][task_id], as_of=fixtures.LATER)
        proof = company_unstarted_continuation(self.memory, context)
        leaf_id = next(iter(proof["leaf_owners"]))
        # The file appears after controller selection but before callback dispatch.
        path = self.memory.root / "supplier_routes" / "company_shared" / leaf_id / "intent.json"
        reader = fixtures.OwnedReader(at=fixtures.LATER)
        def change_before_callback(callback, current_context, _timeout):
            atomic_json(path, {"changed_after_selection": True})
            return callback(current_context)
        result = execute_source_run(self.memory, original["run_id"], handlers=self.handlers(reader),
            as_of=fixtures.LATER, action_limit=1, bounded_call=change_before_callback)
        self.assertEqual(result["counts"], {"blocked": 1})
        self.assertEqual(reader.calls, [])
        self.assertEqual(len(self.captures()), 1)
        self.assertTrue(self.memory.snapshot()["query_tasks"][task_id]["reconcile_required"])


if __name__ == "__main__":
    unittest.main()
