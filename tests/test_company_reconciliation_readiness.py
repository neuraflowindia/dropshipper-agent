"""Known missing company originals do not consume another callback allowance."""

from copy import deepcopy
import unittest
from unittest.mock import patch

import test_shared_company_pages as fixtures
import test_shared_company_programme as programme_fixtures
import test_source_deadline as deadline_fixtures

from product_radar.company_pages import (CompanyPagePending, ORIGINAL_READINESS,
                                        company_source_hints)
from product_radar.programme import _source_turn_readiness, prepare_programme
from product_radar.source_coverage import (_stage_receipt, execute_source_run, prepare_source_tasks,
                                         source_access_context)
from product_radar.storage import atomic_json, load_json
from product_radar.supplier_route_acquisition import (collect_supplier_search, create_supplier_handlers,
                                                     supplier_route_url)
from product_radar.work_turns import (DEFAULT_POLICY, bind_work_turn, close_work_turn, open_work_turn,
                                    reserve_source_callback)


class CompanyReconciliationReadinessTests(unittest.TestCase):
    def setUp(self):
        fixture = fixtures.SharedCompanyTests()
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        self.fixture, self.memory = fixture, fixture.memory
        self.run = "company-missing-originals"

    def handlers(self, reader=fixtures.forbidden):
        return create_supplier_handlers(self.memory, http_reader=fixtures.forbidden,
            page_reader=reader, browser_reader=fixtures.forbidden, shared_acquisitions=True,
            respect_access_waits=True)

    def execute(self, handlers, **options):
        return execute_source_run(self.memory, self.run, handlers=handlers, as_of=fixtures.LATER,
            bounded_call=lambda callback, context, _timeout: callback(context), **options)

    def pending(self):
        request = self.fixture.nominate("paper", [fixtures.URL])
        prepared = prepare_source_tasks(self.memory, [{"route": "company-pages", "request": request,
            "concept_ids": ["paper"]}], run_id=self.run, as_of=fixtures.NOW)
        def original_interrupted_before_input(_callback, _context, _timeout):
            raise CompanyPagePending("SUPPLIER_COMPANY_ORIGINAL_PENDING")
        initial = execute_source_run(self.memory, self.run, handlers=self.handlers(), as_of=fixtures.NOW,
            bounded_call=original_interrupted_before_input)
        self.assertEqual(initial["handler_calls"], 1)
        task_id = prepared["task_ids"][0]
        original = deepcopy(self.memory.snapshot()["query_tasks"][task_id])
        self.assertTrue(original["reconcile_required"])
        self.assertFalse((self.memory.root / "supplier_routes" / original["acquisition_id"]).exists())
        return task_id, original

    def turn(self):
        return open_work_turn(self.memory, "company-wait-programme", at=fixtures.LATER,
                             policy=dict(DEFAULT_POLICY, source_callbacks=1, exploration_slots=0))

    def pending_saved(self, *, shared=True):
        """The real local callback saved its reply before the outer return was lost."""
        request = self.fixture.nominate("paper", [fixtures.URL])
        prepared = prepare_source_tasks(self.memory, [{"route": "company-pages", "request": request,
            "concept_ids": ["paper"]}], run_id=self.run, as_of=fixtures.NOW)
        reader = fixtures.OwnedReader()
        handlers = self.handlers(reader) if shared else create_supplier_handlers(self.memory,
            page_reader=reader, browser_reader=fixtures.forbidden)
        saved = {}
        def reply_lost(callback, context, _timeout):
            saved["context"] = deepcopy(context)
            saved["envelope"] = callback(context)
            raise CompanyPagePending("SUPPLIER_COMPANY_ORIGINAL_PENDING")
        first = execute_source_run(self.memory, self.run, handlers=handlers,
            as_of=fixtures.NOW, bounded_call=reply_lost)
        self.assertEqual(first["counts"], {"blocked": 1})
        task_id = prepared["task_ids"][0]
        task = deepcopy(self.memory.snapshot()["query_tasks"][task_id])
        folder = self.memory.root / "supplier_routes" / task["acquisition_id"]
        self.assertTrue((folder / "company_pass.json").is_file())
        self.assertEqual(len(reader.calls), 1)
        return task_id, task, folder, reader, saved

    def programme(self):
        return prepare_programme(self.memory, "company-wait-programme", source_run_id=self.run,
                                 concept_ids=["paper"], as_of=fixtures.NOW)

    def test_missing_original_waits_across_real_turns_without_handler_or_allowance(self):
        task_id, original = self.pending()
        handlers = self.handlers()
        state = self.memory.snapshot()
        context = source_access_context(state, state["query_tasks"][task_id], as_of=fixtures.LATER)
        self.assertFalse(handlers["company-pages"]["source_due_hint"](context, state=state)["ready"])
        for _ in range(2):
            turn = self.turn()
            with bind_work_turn(self.memory, turn["id"]), patch.dict(handlers["company-pages"],
                    reconcile=fixtures.forbidden):
                result = self.execute(handlers)
            self.assertEqual(result["handler_calls"], 0)
            self.assertEqual(result["counts"], {"waiting": 1})
            self.assertEqual(result["results"][0]["reason"], "SUPPLIER_COMPANY_ORIGINAL_PENDING")
            self.assertFalse(result["results"][0]["actual_attempt"])
            self.assertEqual(self.memory.snapshot()["query_tasks"][task_id], original)
            self.assertEqual(self.memory.snapshot()["batch_runs"][turn["id"]]["counters"]["source_callbacks"], 0)
            close_work_turn(self.memory, turn["id"], at=fixtures.LATER,
                            reason="Unchanged original remains waiting; no callback was spent.")

    def test_wait_does_not_take_the_healthy_next_company_callback(self):
        task_id, original = self.pending()
        # A distinct healthy URL must have its own actual saved nomination;
        # changing a shared cached search or re-reading the uncertain URL is
        # not a valid healthy-work fixture.
        query = "Wrapping paper sheets"
        search = fixtures.Reader({supplier_route_url("google-suppliers", query):
            '<a href="' + fixtures.OTHER + '"><h3>Wrapping paper supplier</h3></a>'})
        collect_supplier_search(self.memory, route="google-suppliers", concept_ids=["paper-two"],
                                query=query, http_reader=search, as_of=fixtures.NOW)
        checks = [rid for rid, row in self.memory.snapshot()["source_checks"].items()
                  if row.get("kind") == "supplier_route_search" and row.get("concept_id") == "paper-two"]
        request = {"candidate_urls": {"paper-two": [fixtures.OTHER]},
                   "candidate_check_ids": {"paper-two": checks}}
        prepared = prepare_source_tasks(self.memory, [{"route": "company-pages", "request": request,
            "concept_ids": ["paper-two"]}], run_id=self.run, as_of=fixtures.NOW)
        healthy = [tid for tid in prepared["task_ids"] if tid != task_id]
        reader = fixtures.OwnedReader(at=fixtures.LATER)
        turn = self.turn()
        with bind_work_turn(self.memory, turn["id"]):
            result = self.execute(self.handlers(reader), action_limit=1)
        # The healthy page joined the run as an appended cohort. That pass
        # serves only the new suffix; the already-inspected original is not
        # revisited there, so it can neither take nor block the one callback.
        self.assertEqual(result["handler_calls"], 1)
        self.assertEqual(result["counts"], {"acquired": 1})
        self.assertEqual([row["task_id"] for row in result["results"]], healthy)
        self.assertEqual([item["url"] for item in reader.calls], [fixtures.OTHER])
        self.assertEqual(self.memory.snapshot()["query_tasks"][task_id], original)
        self.assertEqual(self.memory.snapshot()["batch_runs"][turn["id"]]["counters"]["source_callbacks"], 1)
        close_work_turn(self.memory, turn["id"], at=fixtures.LATER, reason="Healthy page served; original waits.")
        # The next real turn's full sweep reaches the unchanged original: it is
        # reported as waiting, with no handler, reconciler or callback spent.
        handlers = self.handlers(reader)
        next_turn = self.turn()
        with bind_work_turn(self.memory, next_turn["id"]), patch.dict(handlers["company-pages"],
                reconcile=fixtures.forbidden):
            later = self.execute(handlers, action_limit=1)
        self.assertEqual(later["handler_calls"], 0)
        self.assertEqual(later["counts"], {"waiting": 1, "reused": 1})
        waiting = next(row for row in later["results"] if row["state"] == "waiting")
        self.assertEqual((waiting["task_id"], waiting["reason"], waiting["actual_attempt"]),
                         (task_id, "SUPPLIER_COMPANY_ORIGINAL_PENDING", False))
        self.assertEqual(len(reader.calls), 1)
        self.assertEqual(self.memory.snapshot()["query_tasks"][task_id], original)
        self.assertEqual(self.memory.snapshot()["batch_runs"][next_turn["id"]]["counters"]["source_callbacks"], 0)

    def test_late_exact_outer_files_wake_and_recover_without_new_transport(self):
        task_id, original, folder, reader, _saved = self.pending_saved()
        files = {name: load_json(folder / name) for name in ("company_inputs.json", "company_pass.json")}
        originals = {name: (folder / name).read_bytes() for name in files}
        for name in files: (folder / name).unlink()
        record = self.programme()
        state = self.memory.snapshot()
        context = source_access_context(state, state["query_tasks"][task_id], as_of=fixtures.LATER)
        due_hint, saved_hint = company_source_hints(self.memory)
        head = self.memory.snapshot_with_provenance()["head"]
        with patch.object(self.memory, "snapshot", side_effect=AssertionError("No cold replay in pure hints")):
            before_saved = saved_hint(context, state=state)
            before_due = due_hint(context, state=state)
            # The shared physical reply still exists, but cannot recreate the
            # caller's missing exact refresh/capture intent.
            self.assertFalse(before_due["ready"])
            self.assertIn("company_original_wait", before_due)
            for name, value in files.items(): atomic_json(folder / name, value)
            after_saved = saved_hint(context, state=state)
            after_due = due_hint(context, state=state)
        self.assertNotEqual(before_saved, after_saved)
        self.assertTrue(after_due["local_saved"])
        self.assertNotIn("company_original_wait", after_due)
        self.assertEqual(self.memory.snapshot_with_provenance()["head"], head)
        readiness = _source_turn_readiness(self.memory, state, record, self.handlers(), fixtures.LATER)
        self.assertTrue(readiness["local_ready"])
        self.assertTrue(readiness["callback_ready"])
        resumed = self.execute(self.handlers())
        self.assertEqual(resumed["counts"], {"acquired": 1})
        current = self.memory.snapshot()["query_tasks"][task_id]
        for field in ("acquisition_id", "generation", "tries"):
            self.assertEqual(current[field], original[field])
        self.assertFalse(current["reconcile_required"])
        self.assertEqual(len(reader.calls), 1)
        for name, raw in originals.items(): self.assertEqual((folder / name).read_bytes(), raw)
        self.assertEqual(self.execute(self.handlers())["counts"], {"reused": 1})
        settled = _source_turn_readiness(self.memory, self.memory.snapshot(), record, self.handlers(), fixtures.LATER)
        self.assertFalse(settled["callback_ready"])
        self.assertFalse(settled["local_ready"])

    def test_staged_reply_wins_even_without_outer_files_and_with_spent_allowance(self):
        task_id, original, folder, reader, saved = self.pending_saved()
        _stage_receipt(self.memory, saved["context"], saved["envelope"])
        for name in ("company_inputs.json", "company_pass.json"): (folder / name).unlink()
        turn = self.turn()
        handlers = self.handlers()
        with bind_work_turn(self.memory, turn["id"]):
            reserve_source_callback(self.memory, "already-spent", at=fixtures.LATER)
            with patch.dict(handlers["company-pages"], reconcile=fixtures.forbidden,
                            source_due_hint=fixtures.forbidden):
                resumed = self.execute(handlers)
        self.assertEqual(resumed["handler_calls"], 0)
        self.assertEqual(resumed["counts"], {"acquired": 1})
        self.assertEqual(self.memory.snapshot()["batch_runs"][turn["id"]]["counters"]["source_callbacks"], 1)
        self.assertEqual(self.memory.snapshot()["query_tasks"][task_id]["acquisition_id"], original["acquisition_id"])
        self.assertEqual(len(reader.calls), 1)

    def test_legacy_saved_outer_pass_without_shared_leaves_remains_recoverable(self):
        task_id, original, folder, reader, _saved = self.pending_saved(shared=False)
        self.assertFalse((folder / "company_inputs.json").exists())
        raw = (folder / "company_pass.json").read_bytes()
        state = self.memory.snapshot()
        self.assertFalse(any(row.get("kind") == "source-acquisition-owner-v1"
                             and row.get("binding", {}).get("route") == "company-pages"
                             for row in state["batch_runs"].values()))
        record = self.programme()
        readiness = _source_turn_readiness(self.memory, state, record, self.handlers(), fixtures.LATER)
        self.assertTrue(readiness["local_ready"])
        self.assertTrue(readiness["callback_ready"])
        result = self.execute(self.handlers())
        self.assertEqual(result["counts"], {"acquired": 1})
        self.assertEqual((folder / "company_pass.json").read_bytes(), raw)
        self.assertEqual(len(reader.calls), 1)
        self.assertEqual(self.memory.snapshot()["query_tasks"][task_id]["acquisition_id"], original["acquisition_id"])

    def test_real_saved_outer_deadline_wakes_and_replays_without_transport(self):
        request = self.fixture.nominate("paper", [fixtures.URL])
        prepared = prepare_source_tasks(self.memory, [{"route": "company-pages", "request": request,
            "concept_ids": ["paper"]}], run_id=self.run, as_of=fixtures.NOW)
        native_fixture = deadline_fixtures.SourceDeadlineTests()
        native_fixture.root, native_fixture.memory = self.fixture.root, self.memory
        calls, primary_calls = [], []
        def primary(item):
            primary_calls.append(deepcopy(item))
            return fixtures.receipt(item, status=500, actual=False)
        actual_native = deadline_fixtures.native_snapshot
        def native(url, expression, folder, **options):
            return actual_native(url, expression, folder, runner=native_fixture.native_runner(
                calls, dispatch=True, cancel_after_proof=True), **options)
        old_handlers = create_supplier_handlers(self.memory, page_reader=primary)
        with native_fixture.installed_native(), patch("product_radar.native_public_browser.native_snapshot", native):
            first = execute_source_run(self.memory, self.run, handlers=old_handlers, as_of=fixtures.NOW,
                bounded_call=lambda callback, context, _timeout: callback(context))
        self.assertEqual(first["counts"], {"failed": 1})
        task_id = prepared["task_ids"][0]
        task = self.memory.snapshot()["query_tasks"][task_id]
        folder = self.memory.root / "supplier_routes" / task["acquisition_id"]
        self.assertTrue((folder / "company_deadline.json").is_file())
        self.assertFalse((folder / "company_inputs.json").exists())
        raw = (folder / "company_deadline.json").read_bytes()
        state = self.memory.snapshot()
        hint = self.handlers()["company-pages"]["source_due_hint"](
            source_access_context(state, task, as_of=fixtures.LATER), state=state)
        self.assertTrue(hint["local_saved"])
        result = self.execute(self.handlers())
        self.assertEqual(result["counts"], {"failed": 1})
        self.assertFalse(self.memory.snapshot()["query_tasks"][task_id]["reconcile_required"])
        self.assertEqual((folder / "company_deadline.json").read_bytes(), raw)
        self.assertEqual(calls, ["launch", "cleanup"])
        self.assertEqual(len(primary_calls), 1)

    def test_custom_uncontracted_false_hint_does_not_disable_original_reconciler(self):
        task_id, original = self.pending()
        calls = []
        def reconcile(context):
            calls.append(deepcopy(context))
            raise CompanyPagePending("SUPPLIER_COMPANY_ORIGINAL_PENDING")
        handlers = self.handlers()
        handlers["company-pages"].update(source_due_hint=lambda _context: {"ready": False},
                                          reconcile=reconcile)
        turn = self.turn()
        with bind_work_turn(self.memory, turn["id"]): result = self.execute(handlers)
        self.assertEqual(result["handler_calls"], 1)
        self.assertEqual(len(calls), 1)
        self.assertEqual(self.memory.snapshot()["query_tasks"][task_id]["acquisition_id"], original["acquisition_id"])
        self.assertEqual(self.memory.snapshot()["batch_runs"][turn["id"]]["counters"]["source_callbacks"], 1)

    def test_changed_description_is_not_hidden_by_original_wait_hint(self):
        task_id, original = self.pending()
        previous = self.memory.snapshot()["concepts"]["paper"]
        concept = deepcopy(previous)
        concept["description"]["function"] = "A changed current meaning"
        self.memory.put("concepts", "paper", concept, previous=previous)
        handlers = self.handlers()
        with patch.dict(handlers["company-pages"], source_due_hint=fixtures.forbidden):
            result = self.execute(handlers)
        self.assertEqual(result["handler_calls"], 0)
        self.assertEqual(result["counts"], {"not_attempted": 1})
        self.assertIn("Target descriptions changed", result["results"][0]["reason"])
        self.assertEqual(self.memory.snapshot()["query_tasks"][task_id], original)

    def test_foreign_wait_marker_does_not_disable_original_reconciler(self):
        task_id, original = self.pending()
        calls = []
        def reconcile(context):
            calls.append(deepcopy(context))
            raise CompanyPagePending("SUPPLIER_COMPANY_ORIGINAL_PENDING")
        handlers = self.handlers()
        handlers["company-pages"].update(reconcile=reconcile, source_due_hint=lambda _context: {
            "ready": False, "company_original_wait": {"contract": ORIGINAL_READINESS,
                "task_id": task_id, "acquisition_id": "a-different-original",
                "request_hash": original["request_hash"]}})
        turn = self.turn()
        with bind_work_turn(self.memory, turn["id"]): result = self.execute(handlers)
        self.assertEqual(result["handler_calls"], 1)
        self.assertEqual(len(calls), 1)
        self.assertEqual(self.memory.snapshot()["query_tasks"][task_id]["acquisition_id"], original["acquisition_id"])
        self.assertEqual(self.memory.snapshot()["batch_runs"][turn["id"]]["counters"]["source_callbacks"], 1)

    def test_inexact_original_context_cannot_claim_missing_original_proof(self):
        task_id, original = self.pending()
        state = self.memory.snapshot()
        context = source_access_context(state, original, as_of=fixtures.LATER)
        context["acquisition_id"] = "a-different-original"
        due_hint, saved_hint = company_source_hints(self.memory)
        with patch.object(self.memory, "snapshot", side_effect=AssertionError("No cold replay in pure hints")):
            self.assertNotIn("company_original_wait", due_hint(context, state=state))
            self.assertEqual(saved_hint(context, state=state)["files"], [])
        self.assertEqual(self.memory.snapshot()["query_tasks"][task_id], original)

    def test_missing_outer_files_do_not_suppress_proved_unstarted_continuation(self):
        helper = programme_fixtures.SharedCompanyProgrammeTests()
        helper.fixture, helper.memory, helper.root = self.fixture, self.memory, self.fixture.root
        unseen, healthy, task_id, original, record = helper.wait_company(self.run)
        folder = self.memory.root / "supplier_routes" / original["acquisition_id"]
        self.assertTrue((folder / "company_inputs.json").is_file())
        (folder / "company_inputs.json").unlink()
        self.assertFalse(any((folder / name).exists() for name in
                             ("company_inputs.json", "company_pass.json", "company_deadline.json")))
        state = self.memory.snapshot()
        context = source_access_context(state, original, as_of=fixtures.LATER)
        hint = self.handlers()["company-pages"]["source_due_hint"](context, state=state)
        self.assertTrue(hint["ready"])
        self.assertNotIn("company_original_wait", hint)
        readiness = _source_turn_readiness(self.memory, state, record, self.handlers(), fixtures.LATER)
        self.assertTrue(readiness["callback_ready"])
        reader = fixtures.OwnedReader(at=fixtures.LATER)
        turn = self.turn()
        with bind_work_turn(self.memory, turn["id"]):
            result = self.execute(self.handlers(reader), action_limit=1, shared_marketplace=True)
        self.assertEqual(result["counts"], {"acquired": 1})
        self.assertEqual([row["url"] for row in reader.calls], [unseen])
        self.assertEqual(len(healthy.calls), 1)
        current = self.memory.snapshot()["query_tasks"][task_id]
        for field in ("acquisition_id", "generation", "tries"):
            self.assertEqual(current[field], original[field])
        self.assertFalse(current["reconcile_required"])
        self.assertEqual(self.memory.snapshot()["batch_runs"][turn["id"]]["counters"]["source_callbacks"], 1)


if __name__ == "__main__":
    unittest.main()
