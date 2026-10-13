"""Exact installed supplier-original replay, with synthetic local pages only."""

from copy import deepcopy
from pathlib import Path
import unittest
from unittest.mock import patch

from product_radar.models import SafetyError, digest
from product_radar.source_coverage import (_has_unapplied_receipt, _stage_receipt,
    bind_source_relevance, execute_source_run, prepare_source_tasks, source_access_context)
from product_radar.storage import atomic_json, load_json
from product_radar.supplier_route_acquisition import create_supplier_handlers
from product_radar.supplier_search import APPLICATION_KIND, current_supplier_search_checks
from product_radar.work_turns import bind_work_turn, open_work_turn
import test_shared_supplier_search as fixtures


class SupplierUnchangedOriginalTests(unittest.TestCase):
    setUp = fixtures.SharedSupplierTests.setUp
    reader = fixtures.SharedSupplierTests.reader
    browser = fixtures.SharedSupplierTests.browser
    checks = fixtures.SharedSupplierTests.checks
    owners = fixtures.SharedSupplierTests.owners

    def handlers(self, route="indiamart", *, unclosed=True):
        def reader(item):
            result = self.reader(item)
            return dict(result, owned_worker_exited=False) if unclosed else result
        installed = create_supplier_handlers(self.memory, http_reader=reader,
            browser_reader=self.browser, respect_access_waits=True, shared_acquisitions=True)
        return {route: installed[route]}

    def prepare(self, route="indiamart", *, run="supplier-unchanged", concept_ids=None):
        self.run = run
        result = prepare_source_tasks(self.memory, [{"route": route,
            "request": {"query": self.query, "country": "IN"},
            "concept_ids": concept_ids or [self.cid]}], run_id=run, as_of=self.at)
        return result["task_ids"][0]

    def assert_unchanged_replay(self, route):
        handlers = self.handlers(route); task_id = self.prepare(route)
        self.checks(handlers)
        before = self.memory.snapshot()
        original_task = deepcopy(before["query_tasks"][task_id])
        self.assertTrue(original_task["supplier_original_pending"])
        self.assertTrue(original_task["reconcile_required"])
        with patch.object(self.memory, "commit", wraps=self.memory.commit) as commit:
            result = self.checks(handlers)
        caller_changes = [entry for call in commit.call_args_list for entry in call.args[1]
            if entry.get("table") == "query_tasks" and entry.get("id") == task_id]
        self.assertEqual(len(caller_changes), 0,
            "The unchanged saved original must not write dispatching and the same final result again.")
        after = self.memory.snapshot()
        self.assertEqual(after["query_tasks"][task_id], original_task)
        self.assertEqual(after["source_checks"], before["source_checks"])
        self.assertEqual(result["counts"], {"waiting": 1})
        self.assertEqual(result["handler_calls"], 0)
        self.assertEqual(result["results"][0]["reason"], "SUPPLIER_ORIGINAL_SEARCH_PENDING")
        self.assertEqual(len(self.http_calls), 1)
        self.assertEqual(self.browser_calls, [])

    def test_indiamart_unchanged_unclosed_original_does_not_reapply_caller(self):
        self.assert_unchanged_replay("indiamart")

    def test_tradeindia_unchanged_unclosed_original_does_not_reapply_caller(self):
        self.assert_unchanged_replay("tradeindia")

    def test_maps_unchanged_unclosed_original_does_not_reapply_caller(self):
        self.assert_unchanged_replay("google-maps")

    def test_google_unchanged_unclosed_original_does_not_reapply_caller(self):
        self.assert_unchanged_replay("google-suppliers")

    def test_unchanged_original_retains_exact_validated_supplier_refinement(self):
        handlers = self.handlers(); task_id = self.prepare(); self.checks(handlers)
        task = self.memory.snapshot()["query_tasks"][task_id]
        target = task["target_results"][self.cid]
        bind_source_relevance(self.memory, task_id, [{"concept_id": self.cid,
            "description_hash": target["description_hash"], "state": "completed_relevant",
            "evidence_refs": target["evidence_refs"],
            "reason": "Synthetic accepted matching supplier-page interpretation."}], as_of=self.at)
        before = deepcopy(self.memory.snapshot()["query_tasks"][task_id])
        result = self.checks(handlers)
        self.assertEqual(self.memory.snapshot()["query_tasks"][task_id], before)
        self.assertEqual(result["counts"], {"waiting": 1})
        self.assertEqual(len(self.http_calls), 1)

    def test_unproved_refinement_does_not_qualify_as_unchanged_applied_work(self):
        handlers = self.handlers(); task_id = self.prepare(); self.checks(handlers)
        original = self.memory.snapshot()["query_tasks"][task_id]
        changed = deepcopy(original)
        changed["target_results"][self.cid].update(state="completed_relevant", reason="Unproved synthetic edit.")
        self.memory.put("query_tasks", task_id, changed, previous=original)
        result = self.checks(handlers)
        self.assertEqual(result["counts"], {"acquired": 1})
        self.assertEqual(self.memory.snapshot()["query_tasks"][task_id]["target_results"][self.cid]["state"],
            "interpretation_pending")
        self.assertEqual(len(self.http_calls), 1)

    def test_normal_turn_unchanged_replay_spends_no_callback_and_reports_no_new_result(self):
        handlers = self.handlers(); task_id = self.prepare(); self.checks(handlers)
        turn = open_work_turn(self.memory, "supplier-no-op-programme", at=self.at)
        before = deepcopy(self.memory.snapshot()["batch_runs"][turn["id"]]["counters"])
        progress = []
        with bind_work_turn(self.memory, turn["id"]):
            result = execute_source_run(self.memory, self.run, handlers=handlers, as_of=self.at,
                shared_marketplace=True, on_progress=progress.append,
                bounded_call=lambda callback, context, _timeout: callback(context))
        self.assertEqual(result["handler_calls"], 0)
        self.assertEqual(result["results"], [{"task_id": task_id, "state": "waiting", "actual_attempt": False,
            "reason": "SUPPLIER_ORIGINAL_SEARCH_PENDING", "next_retry_at": None}])
        self.assertEqual(progress, [])
        self.assertEqual(self.memory.snapshot()["batch_runs"][turn["id"]]["counters"], before)
        self.assertEqual(len(self.http_calls), 1)

    def test_other_caller_applies_same_original_once_then_waits(self):
        handlers = self.handlers(); first = self.prepare(); self.checks(handlers)
        other = self.prepare(run="supplier-other-caller")
        self.assertNotEqual(other, first)
        result = self.checks(handlers)
        self.assertEqual(result["counts"], {"acquired": 1})
        before = deepcopy(self.memory.snapshot()["query_tasks"][other])
        self.assertTrue(before["target_results"][self.cid]["evidence_refs"])
        self.assertEqual(self.checks(handlers)["counts"], {"waiting": 1})
        self.assertEqual(self.memory.snapshot()["query_tasks"][other], before)
        self.assertEqual(len(self.http_calls), 1)

    def test_added_current_product_target_gets_its_own_application_without_new_read(self):
        handlers = self.handlers(); task_id = self.prepare(); self.checks(handlers)
        state = self.memory.snapshot()
        other = deepcopy(state["concepts"][self.cid]); other["description"]["label"] = self.query
        self.memory.put("concepts", "another-caddy", other)
        self.assertEqual(self.prepare(concept_ids=[self.cid, "another-caddy"]), task_id)
        self.assertEqual(self.checks(handlers)["counts"], {"acquired": 1})
        state = self.memory.snapshot()
        self.assertEqual(set(state["query_tasks"][task_id]["target_results"]), {self.cid, "another-caddy"})
        applications = {row["binding"]["concept_id"] for row in state["source_checks"].values()
            if row.get("kind") == APPLICATION_KIND}
        self.assertEqual(applications, {self.cid, "another-caddy"})
        before = deepcopy(state["query_tasks"][task_id])
        self.assertEqual(self.checks(handlers)["counts"], {"waiting": 1})
        self.assertEqual(self.memory.snapshot()["query_tasks"][task_id], before)
        self.assertEqual(len(self.http_calls), 1)

    def test_changed_definition_and_current_profile_reapply_saved_original_then_settle(self):
        handlers = self.handlers(); task_id = self.prepare(); self.checks(handlers)
        before = deepcopy(self.memory.snapshot()["query_tasks"][task_id])
        definition = deepcopy(fixtures.functional_fixture.DEFINITION)
        definition["primary_use"] = "Store wet cleaning tools by the kitchen sink"
        self.fixture.accept(definition, at=self.at); self.fixture.prepare(fixtures.Runner())
        self.assertEqual(self.checks(handlers)["counts"], {"acquired": 1})
        changed = self.memory.snapshot()["query_tasks"][task_id]
        self.assertNotEqual(changed["target_results"][self.cid]["evidence_refs"],
            before["target_results"][self.cid]["evidence_refs"])
        self.assertEqual(self.checks(handlers)["counts"], {"waiting": 1})
        self.assertEqual(self.memory.snapshot()["query_tasks"][task_id], changed)
        self.assertEqual(len(self.http_calls), 1)

    def test_changed_description_prepares_current_target_and_reuses_only_the_original_read(self):
        handlers = self.handlers(); task_id = self.prepare(); self.checks(handlers)
        concept = self.memory.snapshot()["concepts"][self.cid]
        changed = deepcopy(concept); changed["description"]["label"] = "Draining sink caddy with tray"
        self.memory.put("concepts", self.cid, changed, previous=concept)
        self.assertEqual(self.prepare(), task_id)
        self.assertEqual(self.checks(handlers)["counts"], {"acquired": 1})
        task = self.memory.snapshot()["query_tasks"][task_id]
        self.assertEqual(task["target_results"][self.cid]["description_hash"], digest(changed["description"]))
        self.assertEqual(self.checks(handlers)["counts"], {"waiting": 1})
        self.assertEqual(self.memory.snapshot()["query_tasks"][task_id], task)
        self.assertEqual(len(self.http_calls), 1)

    def test_changed_parser_reapplies_current_proof_without_another_read(self):
        handlers = self.handlers(); task_id = self.prepare(); self.checks(handlers)
        state = self.memory.snapshot(); before = deepcopy(state["query_tasks"][task_id])
        old_refs = before["target_results"][self.cid]["evidence_refs"]
        old_records = {rid: deepcopy(state["source_checks"][rid]) for rid in old_refs}
        old_path = Path(state["source_checks"][old_refs[0]]["reference"])
        old_bytes = old_path.read_bytes()
        with patch("product_radar.supplier_search.PARSER_VERSION", 4):
            self.assertEqual(self.checks(handlers)["counts"], {"acquired": 1})
            state = self.memory.snapshot(); after = state["query_tasks"][task_id]
            self.assertNotEqual(after["receipt_refs"], before["receipt_refs"])
            new_refs = after["target_results"][self.cid]["evidence_refs"]
            self.assertNotEqual(new_refs[0], old_refs[0])
            self.assertEqual({rid: state["source_checks"][rid] for rid in old_refs}, old_records)
            self.assertEqual(old_path.read_bytes(), old_bytes)
            current = current_supplier_search_checks(state, self.cid,
                description_hash=after["target_results"][self.cid]["description_hash"])
            self.assertEqual(set(current), {new_refs[0]})
            self.assertEqual(current[new_refs[0]], state["source_checks"][new_refs[0]])
            self.assertEqual(self.checks(handlers)["counts"], {"waiting": 1})
            self.assertEqual(self.memory.snapshot()["query_tasks"][task_id], after)
        self.assertEqual(len(self.http_calls), 1)

    def test_changed_immutable_original_is_refused_not_hidden_by_wait(self):
        handlers = self.handlers(); task_id = self.prepare(); self.checks(handlers)
        task = self.memory.snapshot()["query_tasks"][task_id]
        path = self.memory.root / "supplier_routes/shared" / task["acquisition_id"] / "page.json"
        original = load_json(path); original["receipt"]["text"] += " Changed immutable source."
        original["hash"] = digest(original["receipt"]); atomic_json(path, original)
        result = self.checks(handlers)
        self.assertEqual(result["counts"], {"blocked": 1})
        self.assertEqual(result["results"][0]["reason"], "SOURCE_ACQUISITION_ARTIFACT_CHANGED")
        self.assertEqual(self.memory.snapshot()["query_tasks"][task_id], task)
        self.assertEqual(len(self.http_calls), 1)

    def test_changed_old_check_is_not_a_parser_upgrade_authority(self):
        handlers = self.handlers(); task_id = self.prepare(); self.checks(handlers)
        state = self.memory.snapshot(); task = state["query_tasks"][task_id]
        check_id = task["target_results"][self.cid]["evidence_refs"][0]
        prior = state["source_checks"][check_id]
        self.memory.put("source_checks", check_id, dict(prior, reason="Unproved changed old check."), previous=prior)
        for version in (3, 4):
            with self.subTest(parser_version=version), patch("product_radar.supplier_search.PARSER_VERSION", version):
                result = self.checks(handlers)
                self.assertEqual(result["counts"], {"blocked": 1})
                self.assertEqual(result["results"][0]["reason"], "SUPPLIER_SHARED_APPLICATION_INVALID")
                checks = [row for row in self.memory.snapshot()["source_checks"].values()
                    if row.get("kind") == "supplier_route_search"]
                self.assertEqual(len(checks), 1)
        self.assertEqual(len(self.http_calls), 1)

    def test_saved_version_two_parser_recovers_under_actual_current_parser(self):
        handlers = self.handlers(); task_id = self.prepare()
        with patch("product_radar.supplier_search.PARSER_VERSION", 2), \
                patch("product_radar.supplier_route_acquisition.PARSER_VERSION", 2):
            self.checks(handlers)
        state = self.memory.snapshot(); task = state["query_tasks"][task_id]
        old_id = task["target_results"][self.cid]["evidence_refs"][0]
        old = deepcopy(state["source_checks"][old_id])
        self.assertEqual(Path(old["reference"]).name, "capture-parser-2.json")
        self.assertEqual(self.checks(handlers)["counts"], {"acquired": 1})
        state = self.memory.snapshot(); task = state["query_tasks"][task_id]
        new_id = task["target_results"][self.cid]["evidence_refs"][0]
        self.assertEqual(Path(state["source_checks"][new_id]["reference"]).name, "capture-parser-3.json")
        self.assertEqual(state["source_checks"][old_id], old)
        self.assertNotEqual(new_id, old_id)
        self.assertEqual(set(current_supplier_search_checks(state, self.cid,
            description_hash=task["target_results"][self.cid]["description_hash"])), {new_id})
        self.assertEqual(self.checks(handlers)["counts"], {"waiting": 1})
        self.assertEqual(len(self.http_calls), 1)

    def test_changed_old_parser_bytes_are_refused_not_given_an_alternate_identity(self):
        handlers = self.handlers(); task_id = self.prepare(); self.checks(handlers)
        state = self.memory.snapshot(); task = state["query_tasks"][task_id]
        check_id = task["target_results"][self.cid]["evidence_refs"][0]
        path = Path(state["source_checks"][check_id]["reference"])
        proof = load_json(path); proof["parsed"]["reason"] = "Unproved changed old parser proof."
        proof["hash"] = digest({field: value for field, value in proof.items() if field != "hash"})
        atomic_json(path, proof)
        with patch("product_radar.supplier_search.PARSER_VERSION", 4):
            result = self.checks(handlers)
        self.assertEqual(result["counts"], {"blocked": 1})
        self.assertEqual(result["results"][0]["reason"], "SOURCE_ACQUISITION_ARTIFACT_CHANGED")
        self.assertEqual(len([row for row in self.memory.snapshot()["source_checks"].values()
            if row.get("kind") == "supplier_route_search"]), 1)
        self.assertEqual(len(self.http_calls), 1)

    def test_malformed_latest_caller_envelope_is_refused_not_treated_as_already_applied(self):
        handlers = self.handlers(); task_id = self.prepare(); self.checks(handlers)
        state = self.memory.snapshot(); task = state["query_tasks"][task_id]
        check_id = task["receipt_refs"][-1]; check = state["source_checks"][check_id]
        altered = dict(check, envelope_hash="f" * 64)
        self.memory.put("source_checks", check_id, altered, previous=check)
        with self.assertRaisesRegex(SafetyError, "^SOURCE_RECEIPT_CONFLICT$"):
            self.checks(handlers)
        self.assertTrue(self.memory.snapshot()["query_tasks"][task_id]["supplier_original_pending"])
        self.assertEqual(len(self.http_calls), 1)

    def test_genuine_interrupted_staged_reply_is_recovered_without_another_callback(self):
        handlers = self.handlers(); task_id = self.prepare()
        import product_radar.source_coverage as coverage
        with patch.object(coverage, "_persist_receipt", side_effect=SafetyError("RUNNER_ALREADY_ACTIVE")):
            first = self.checks(handlers)
        self.assertEqual(first["counts"], {"blocked": 1})
        state = self.memory.snapshot()
        self.assertTrue(_has_unapplied_receipt(self.memory, state, state["query_tasks"][task_id]))
        self.assertEqual(self.checks(handlers)["counts"], {"acquired": 1})
        state = self.memory.snapshot()
        self.assertFalse(_has_unapplied_receipt(self.memory, state, state["query_tasks"][task_id]))
        before = deepcopy(state["query_tasks"][task_id])
        self.assertEqual(self.checks(handlers)["counts"], {"waiting": 1})
        self.assertEqual(self.memory.snapshot()["query_tasks"][task_id], before)
        self.assertEqual(len(self.http_calls), 1)

    def test_final_pending_caller_does_not_swallow_an_unapplied_staged_target_scope(self):
        handlers = self.handlers(); task_id = self.prepare(); self.checks(handlers)
        state = self.memory.snapshot()
        another = deepcopy(state["concepts"][self.cid]); another["description"]["label"] = self.query
        self.memory.put("concepts", "staged-caddy", another)
        self.prepare(concept_ids=[self.cid, "staged-caddy"])
        state = self.memory.snapshot(); task = state["query_tasks"][task_id]
        self.assertEqual(set(task["target_results"]), {self.cid})
        context = source_access_context(state, task, as_of=self.at)
        context["request_hash"] = task["request_hash"]
        with patch("product_radar.supplier_search.utc_now", return_value=self.at):
            returned = handlers["indiamart"]["reconcile"](context)
        self.assertEqual({row["concept_id"] for row in returned["target_results"]}, {self.cid, "staged-caddy"})
        _stage_receipt(self.memory, context, returned)
        staged_path = self.memory.root / "all_source_dispatch/returned_receipts" / task["acquisition_id"] / (digest(returned) + ".json")
        staged_bytes = staged_path.read_bytes()
        self.memory.put("concepts", "staged-caddy", dict(another, status="retired"), previous=another)
        state = self.memory.snapshot()
        self.assertTrue(_has_unapplied_receipt(self.memory, state, state["query_tasks"][task_id]))
        resolver = handlers["indiamart"]["resolve_source"]; resolutions = []
        def recorded(context):
            value = resolver(context); resolutions.append(value); return value
        installed = {"indiamart": dict(handlers["indiamart"], resolve_source=recorded)}
        result = self.checks(installed)
        self.assertEqual(resolutions[0]["action"], "saved")
        self.assertEqual(result["counts"], {"acquired": 1})
        self.assertEqual(result["handler_calls"], 0)
        state = self.memory.snapshot(); task = state["query_tasks"][task_id]
        self.assertFalse(_has_unapplied_receipt(self.memory, state, task))
        self.assertEqual(set(task["target_results"]), {self.cid})
        self.assertEqual(staged_path.read_bytes(), staged_bytes)
        self.assertEqual(self.checks(handlers)["counts"], {"waiting": 1})
        self.assertEqual(len(self.http_calls), 1)

    def test_late_closed_native_original_still_recovers_and_settles(self):
        case = fixtures.SharedSupplierTests(); case.setUp(); self.addCleanup(case.doCleanups)
        case.test_late_native_recovery_keeps_earlier_actual_date_and_clears_pending_original()
        handlers = case.handlers()
        before = deepcopy(case.memory.snapshot()["query_tasks"])
        self.assertEqual(case.checks(handlers)["counts"], {"reused": 1})
        self.assertEqual(case.memory.snapshot()["query_tasks"], before)

    def test_due_closed_access_failure_still_allows_normal_next_generation(self):
        case = fixtures.SharedSupplierTests(); case.setUp(); self.addCleanup(case.doCleanups)
        case.test_closed_server_failure_allows_one_due_new_caller_probe_not_browser_bypass()

    def test_waiting_original_does_not_prevent_next_distinct_healthy_supplier_task(self):
        handlers = self.handlers(); task_id = self.prepare(); self.checks(handlers)
        other = self.handlers("tradeindia", unclosed=False)
        handlers.update(other)
        prepare_source_tasks(self.memory, [{"route": "tradeindia",
            "request": {"query": self.query, "country": "IN"}, "concept_ids": [self.cid]}],
            run_id=self.run, as_of=self.at)
        before = deepcopy(self.memory.snapshot()["query_tasks"][task_id])
        result = self.checks(handlers)
        self.assertEqual(result["counts"], {"waiting": 1, "acquired": 1})
        self.assertEqual(result["handler_calls"], 1)
        self.assertEqual(self.memory.snapshot()["query_tasks"][task_id], before)
        self.assertEqual(len(self.http_calls), 2)


if __name__ == "__main__":
    unittest.main()
