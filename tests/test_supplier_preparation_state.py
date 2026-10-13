"""One verified scan view, fresh selected uses; synthetic pages only, no network."""

from copy import deepcopy
from pathlib import Path
import unittest
from unittest.mock import patch

from product_radar.models import SafetyError, digest
from product_radar.operations import operation_run, operation_writer, record_event
from product_radar.source_interpretation import review_supplier_results
from product_radar.storage import atomic_json, load_json
from product_radar.supplier_capture_store import capture_saved_supplier_bundle, load_saved_supplier_capture
from product_radar.supplier_checks import prepare_supplier_candidates, extract_supplier_candidates
from product_radar.work_turns import DEFAULT_POLICY, bind_work_turn, open_work_turn
import test_source_interpretation as source_fixtures
import test_scoped_supplier_source_join as scoped_fixtures
from test_supplier_capture_store import Reader, BASE
from test_suppliers import FixtureRunner
from test_scoped_supplier_terms import CandidateRunner, Reader as ScopedReader, BASE as SCOPED_BASE, NOW as SCOPED_NOW

NOW = source_fixtures.NOW


class SupplierPreparationStateTests(unittest.TestCase):
    setUp = source_fixtures.SourceInterpretationTests.setUp
    supplier_capture = source_fixtures.SourceInterpretationTests.supplier_capture
    supplier_search_task = source_fixtures.SourceInterpretationTests.supplier_search_task
    supplier_company_task = source_fixtures.SourceInterpretationTests.supplier_company_task
    supplier_failed_capture = source_fixtures.SourceInterpretationTests.supplier_failed_capture

    def capture(self, cid="second", number=0, reader=None):
        return capture_saved_supplier_bundle(self.memory, {"concept_id": cid, "lead": {
            "id": "scan-fixture-lead-" + str(number), "urls": [BASE + "/product/caddy"]}},
            fetcher=reader or Reader())

    def review(self, runner=None, **kwargs):
        with patch("product_radar.suppliers.utc_now", return_value=NOW):
            return review_supplier_results(self.memory, runner or FixtureRunner(), as_of=NOW, **kwargs)

    def test_foreign_capture_count_does_not_multiply_history_snapshots(self):
        own = self.supplier_capture()
        runner = FixtureRunner()
        self.review(runner, concept_ids=["first"])
        original = Path(own["path"]).read_bytes()
        snapshots = []
        for count in (0, 3, 9):
            for number in range(count):
                self.capture(number=number)
            with self.subTest(foreign_captures=count), patch.object(self.memory, "snapshot", wraps=self.memory.snapshot) as read:
                result = self.review(runner, concept_ids=["first"])
            snapshots.append(read.call_count)
            self.assertEqual(result["reused_capture_ids"], [own["capture_id"]])
            self.assertEqual(result["remaining_inputs"], 0)
            self.assertEqual(result["source_calls"], 0)
        self.assertEqual(snapshots, [2, 2, 2])
        self.assertEqual(len(runner.calls), 1)
        self.assertEqual(Path(own["path"]).read_bytes(), original)

    def test_empty_requested_scope_keeps_settlement_but_not_per_capture_snapshots(self):
        for number in range(5):
            self.capture(number=number)
        runner = FixtureRunner()
        with patch.object(self.memory, "snapshot", wraps=self.memory.snapshot) as read:
            result = self.review(runner, concept_ids=[])
        self.assertEqual(read.call_count, 2)
        self.assertEqual(result["selected_capture_ids"], [])
        self.assertEqual(result["remaining_inputs"], 0)
        self.assertEqual(result["source_bindings"], [])
        self.assertEqual(runner.calls, [])

    def test_loader_supplied_view_is_read_only_and_default_still_reads_freshly(self):
        saved = self.supplier_capture()
        state = self.memory.snapshot()
        before = deepcopy(state)
        with patch.object(self.memory, "snapshot", wraps=self.memory.snapshot) as read:
            supplied = load_saved_supplier_capture(self.memory, saved["path"], state=state)
            self.assertEqual(read.call_count, 0)
            ordinary = load_saved_supplier_capture(self.memory, saved["path"])
            self.assertEqual(read.call_count, 1)
        self.assertEqual(supplied, ordinary)
        self.assertEqual(state, before)
        changed = deepcopy(state["concepts"]["first"])
        changed["description"]["label"] = "Different saved product scope"
        self.memory.put("concepts", "first", changed, previous=state["concepts"]["first"])
        with self.assertRaisesRegex(SafetyError, "SUPPLIER_CAPTURE_DESCRIPTION_CHANGED"):
            load_saved_supplier_capture(self.memory, saved["path"])
        # A supplied view is preparation only; it has no execution authority.
        self.assertEqual(load_saved_supplier_capture(self.memory, saved["path"], state=state), supplied)

    def test_supplied_current_view_retains_product_and_brand_exclusions(self):
        saved = self.supplier_capture()
        state = self.memory.snapshot()
        cases = [({"status": value}, "SUPPLIER_CAPTURE_CONCEPT_EXCLUDED")
                 for value in ("excluded", "merged", "retired")]
        cases += [({"identity_conflicted": True}, "SUPPLIER_CAPTURE_CONCEPT_EXCLUDED"),
                  ({"description": {**state["concepts"]["first"]["description"], "brand": "established"}},
                   "SUPPLIER_CAPTURE_CONCEPT_EXCLUDED"),
                  ({"description": {**state["concepts"]["first"]["description"], "label": "Changed"}},
                   "SUPPLIER_CAPTURE_DESCRIPTION_CHANGED")]
        for fields, reason in cases:
            current = deepcopy(state)
            current["concepts"]["first"].update(fields)
            with self.subTest(fields=fields), patch.object(self.memory, "snapshot", side_effect=AssertionError("No scan replay")):
                with self.assertRaisesRegex(SafetyError, reason):
                    load_saved_supplier_capture(self.memory, saved["path"], state=current)

    def test_supplied_view_still_validates_saved_hashes_requests_and_receipts(self):
        saved = self.supplier_capture()
        original = load_json(saved["path"])
        state = self.memory.snapshot()
        bad_records = [dict(original, input_hash="wrong"), dict(original, bundle_hash="wrong")]
        changed = deepcopy(original)
        changed["bundle"]["receipts"][0]["text"] = "Changed original body"
        bad_records.append(changed)
        changed = deepcopy(original)
        changed["request"]["lead"]["urls"] = ["https://supplier.example.org/different"]
        bad_records.append(changed)
        for bad in bad_records:
            atomic_json(saved["path"], bad)
            with self.subTest(bad_hash=bad.get("bundle_hash")), self.assertRaises(SafetyError):
                load_saved_supplier_capture(self.memory, saved["path"], state=state)
        atomic_json(saved["path"], original)
        self.assertEqual(load_saved_supplier_capture(self.memory, saved["path"], state=state)["capture_id"], saved["capture_id"])

    def test_selected_legacy_capture_is_refreshed_and_healthy_sibling_continues(self):
        own = self.supplier_capture()
        healthy = self.capture()
        last = sorted(Path(own["path"]).parent.parent.glob("supplier-capture-*/capture.json"))[-1]
        mutated = []
        loader = load_saved_supplier_capture

        def read(memory, path, **kwargs):
            capture = loader(memory, path, **kwargs)
            if Path(path) == last and not mutated:
                mutated.append(True)
                current = memory.snapshot()["concepts"]["first"]
                changed = deepcopy(current)
                changed["description"]["function"] = "Different product purpose"
                memory.put("concepts", "first", changed, previous=current)
            return capture

        runner = FixtureRunner()
        with patch("product_radar.supplier_capture_store.load_saved_supplier_capture", side_effect=read):
            result = self.review(runner)
        self.assertEqual(len(runner.calls), 1)
        self.assertEqual(len(runner.calls[0]["items"]), 1)
        self.assertEqual(len(result["accepted"]), 1)
        self.assertEqual(set(result["failures"].values()), {"SUPPLIER_CAPTURE_DESCRIPTION_CHANGED"})
        self.assertEqual(result["remaining_inputs"], 1)
        self.assertIn(healthy["capture_id"], result["selected_capture_ids"])

    def test_changed_product_during_f_does_not_settle_current_supplier_tasks(self):
        saved = self.supplier_capture()
        url = BASE + "/product/caddy"
        search, check = self.supplier_search_task([url])
        company = self.supplier_company_task([saved], check, [url])
        fixture = source_fixtures.SupplierRunner("exact_product")

        class ChangingRunner:
            identity = fixture.identity

            def __call__(inner, role, payload, folder):
                response = fixture(role, payload, folder)
                current = self.memory.snapshot()["concepts"]["first"]
                self.memory.put("concepts", "first", dict(current, status="excluded"), previous=current)
                return response

        result = self.review(ChangingRunner())
        self.assertEqual(len(fixture.calls), 1)
        self.assertEqual(result["source_bindings"], [])
        self.assertIn("SUPPLIER_CAPTURE_CONCEPT_EXCLUDED", {r["reason"] for r in result["skipped_captures"]})
        state = self.memory.snapshot()
        for task_id in (search, company):
            self.assertEqual(state["query_tasks"][task_id]["target_results"]["first"]["state"], "interpretation_pending")

    def test_brand_changed_during_f_does_not_settle_current_supplier_tasks(self):
        saved = self.supplier_capture()
        url = BASE + "/product/caddy"
        search, check = self.supplier_search_task([url])
        company = self.supplier_company_task([saved], check, [url])
        fixture = source_fixtures.SupplierRunner("exact_product")

        class ChangingRunner:
            identity = fixture.identity

            def __call__(inner, role, payload, folder):
                response = fixture(role, payload, folder)
                current = self.memory.snapshot()["concepts"]["first"]
                updated = deepcopy(current)
                updated["description"]["brand"] = "established"
                self.memory.put("concepts", "first", updated, previous=current)
                return response

        result = self.review(ChangingRunner())
        self.assertEqual(len(fixture.calls), 1)
        self.assertEqual(result["source_bindings"], [])
        self.assertIn("SUPPLIER_CAPTURE_CONCEPT_EXCLUDED", {r["reason"] for r in result["skipped_captures"]})
        state = self.memory.snapshot()
        self.assertEqual(len(state["supplier_assessments"]), 0)
        # Preserve the exact raw historical answer, but do not apply it to a
        # product excluded before the authoritative assessment save.
        self.assertTrue(any((self.memory.root / "ai_jobs" / job_id / "result.json").is_file()
                            for job_id in state["jobs"]))
        for task_id in (search, company):
            self.assertEqual(state["query_tasks"][task_id]["target_results"]["first"]["state"], "interpretation_pending")

    def test_same_valid_original_changed_after_scan_cannot_start_f(self):
        saved = self.supplier_capture()
        loader = load_saved_supplier_capture
        changed = []
        replacement = "Changed but structurally valid saved supplier text"

        def read(memory, path, **kwargs):
            capture = loader(memory, path, **kwargs)
            if not changed:
                changed.append(True)
                raw = load_json(path)
                raw["bundle"]["item"]["pages"][0]["text"] = replacement
                raw["bundle"]["receipts"][0]["text"] = replacement
                raw["bundle_hash"] = digest(raw["bundle"])
                atomic_json(path, raw)
            return capture

        runner = FixtureRunner()
        with patch("product_radar.supplier_capture_store.load_saved_supplier_capture", side_effect=read):
            result = self.review(runner)
        self.assertEqual(runner.calls, [])
        self.assertEqual(set(result["failures"].values()), {"SUPPLIER_CAPTURE_ORIGINAL_CHANGED"})
        self.assertEqual(result["remaining_inputs"], 1)
        self.assertEqual(result["source_bindings"], [])
        self.assertEqual(load_saved_supplier_capture(self.memory, saved["path"])["bundle"]["item"]["pages"][0]["text"], replacement)

    def test_spent_turn_recovers_same_original_f_answer_without_new_execution(self):
        self.supplier_capture()
        policy = deepcopy(DEFAULT_POLICY)
        policy.update(model_executions=1, report_slots=0)
        turn = open_work_turn(self.memory, "supplier-scan-spent", at=NOW, policy=policy)
        runner = FixtureRunner()
        original_commit = self.memory.commit

        def fail_application(event_id, changes, **kwargs):
            if any(row["table"] == "supplier_assessments" for row in changes):
                raise OSError("Synthetic saved-answer application interruption")
            return original_commit(event_id, changes, **kwargs)

        with bind_work_turn(self.memory, turn["id"]), patch.object(self.memory, "commit", side_effect=fail_application):
            first = self.review(runner)
        self.assertEqual(len(runner.calls), 1)
        self.assertEqual(first["accepted"], {})
        self.assertEqual(set(first["failures"].values()), {"SUPPLIER_REVIEW_LOCAL_FILE_FAILED"})
        original_turn = self.memory.snapshot()["batch_runs"][turn["id"]]
        self.assertEqual(original_turn["counters"]["model_executions"], 1)
        originals = {path: path.read_bytes() for path in (self.memory.root / "ai_jobs").glob("job-*/result.json")}
        self.assertEqual(len(originals), 1)
        with bind_work_turn(self.memory, turn["id"]):
            recovered = self.review(runner)
        self.assertEqual(len(recovered["accepted"]), 1)
        self.assertEqual(recovered["failures"], {})
        self.assertEqual(len(runner.calls), 1)
        self.assertEqual(self.memory.snapshot()["batch_runs"][turn["id"]], original_turn)
        self.assertTrue(all(path.read_bytes() == raw for path, raw in originals.items()))

    def test_scan_progress_is_bounded_private_and_does_not_change_phase_or_result_credit(self):
        import json
        for number in range(18):
            self.capture(number=number)
        with operation_run(self.memory.root.parent) as recorder, operation_writer():
            record_event("stage_started", stage="suppliers")
            phase = recorder.phase, recorder.phase_started, recorder.phase_deadline
            result = self.review(concept_ids=[])
            self.assertEqual((recorder.phase, recorder.phase_started, recorder.phase_deadline), phase)
            self.assertEqual(recorder.counts["completed_results"], 0)
            self.assertIsNone(recorder.last_useful)
        log = self.memory.root.parent / "data/operations/research-runs" / (recorder.run_id + ".jsonl")
        rows = [json.loads(line) for line in log.read_text().splitlines()]
        notices = [row for row in rows if row["event"] == "supplier_capture_scan"]
        self.assertEqual([(row["scanned"], row["remaining"]) for row in notices], [(0, 18), (8, 10), (16, 2), (18, 0)])
        self.assertTrue(all(row["useful_result"] is False for row in notices))
        self.assertTrue(all(not set(row) & {"task_id", "acquisition_id", "job_id", "route", "phase", "deadline_seconds"} for row in notices))
        self.assertFalse(any(BASE in json.dumps(row) for row in notices))
        self.assertEqual(result["source_calls"], 0)

    def test_scan_recorder_failure_does_not_block_saved_original_recovery(self):
        saved = self.supplier_capture()
        runner = FixtureRunner()
        self.review(runner)
        before = self.memory.snapshot_with_provenance()
        with patch("product_radar.operations.record_event", side_effect=OSError("Synthetic private log failure")):
            replay = self.review(runner)
        self.assertEqual(replay["reused_capture_ids"], [saved["capture_id"]])
        self.assertEqual(replay["remaining_inputs"], 0)
        self.assertEqual(len(runner.calls), 1)
        self.assertEqual(self.memory.snapshot_with_provenance(), before)

    def test_failed_original_pages_keep_their_uncertainty_and_do_not_call_f(self):
        saved = self.supplier_failed_capture(BASE + "/unreadable")
        search, check = self.supplier_search_task([BASE + "/unreadable"])
        self.supplier_company_task([saved], check, [BASE + "/unreadable"], outer_state="failed")
        runner = FixtureRunner()
        result = self.review(runner)
        self.assertEqual(runner.calls, [])
        self.assertEqual(result["source_bindings"][0]["state"], "insufficient")
        self.assertEqual(self.memory.snapshot()["query_tasks"][search]["target_results"]["first"]["state"], "insufficient")
        self.assertEqual(result["source_calls"], 0)


class ScopedSupplierPreparationStateTests(unittest.TestCase):
    setUp = scoped_fixtures.ScopedSupplierSourceJoinTests.setUp
    review = scoped_fixtures.ScopedSupplierSourceJoinTests.review

    def healthy_capture(self):
        request = {"concept_id": self.cid, "lead": {"id": "healthy-supplier-scan-sibling",
            "urls": [SCOPED_BASE + "/about"]}}
        return capture_saved_supplier_bundle(self.memory, request, fetcher=ScopedReader())

    def company_task(self, saved, run_id):
        from product_radar.source_coverage import prepare_source_tasks, execute_source_run
        capture = load_saved_supplier_capture(self.memory, saved["path"])
        planned = prepare_source_tasks(self.memory, [{"route": "company-pages", "request": {
            "candidate_urls": {self.cid: capture["request"]["lead"]["urls"]}, "country": "IN"},
            "concept_ids": [self.cid]}], run_id=run_id, as_of=SCOPED_NOW)

        def handler(context):
            context["record_dispatch"]()
            return {"schema_version": 1, "route": "company-pages", "acquisition_id": context["acquisition_id"],
                "observed_at": SCOPED_NOW, "state": "usable", "dispatch_started": True,
                "reference": saved["path"], "capture_ids": [], "observation_ids": [],
                "evidence_refs": [saved["path"]], "reason": "Synthetic original supplier capture.",
                "target_results": [{"concept_id": self.cid,
                    "description_hash": context["description_hashes"][self.cid],
                    "state": "interpretation_pending", "evidence_refs": [saved["path"]],
                    "reason": "Saved original awaits scoped interpretation."}], "retryable": False}

        execute_source_run(self.memory, run_id, handlers={"company-pages": handler}, as_of=SCOPED_NOW,
            bounded_call=lambda callback, context, timeout: callback(context))
        return planned["task_ids"][0]

    def test_scoped_reread_corruption_keeps_healthy_sibling(self):
        healthy = self.healthy_capture()
        loader, changed = load_saved_supplier_capture, []

        def read(memory, path, **kwargs):
            capture = loader(memory, path, **kwargs)
            if Path(path) == Path(self.saved["path"]) and not changed:
                changed.append(True)
                record = load_json(path)
                record["bundle_hash"] = "changed-original"
                atomic_json(path, record)
            return capture

        runner = CandidateRunner()
        with patch("product_radar.supplier_capture_store.load_saved_supplier_capture", side_effect=read):
            result = self.review(runner)
        self.assertEqual(len(runner.calls), 1)
        self.assertEqual(len(result["accepted"]), 1)
        self.assertEqual(result["selected_capture_ids"], [healthy["capture_id"]])
        self.assertEqual(result["skipped_captures"], [{"capture_id": self.saved["capture_id"],
            "state": "not_interpreted", "reason": "SAVED_SUPPLIER_CAPTURE_INVALID"}])
        self.assertEqual(result["remaining_inputs"], 0)
        self.assertEqual(result["source_bindings"], [])

    def test_scoped_preparation_file_error_keeps_healthy_sibling(self):
        healthy = self.healthy_capture()
        loader = load_saved_supplier_capture

        def read(memory, path, **kwargs):
            if Path(path) == Path(self.saved["path"]):
                raise OSError("Synthetic private path must not be returned")
            return loader(memory, path, **kwargs)

        runner = CandidateRunner()
        with patch("product_radar.supplier_checks.load_saved_supplier_capture", side_effect=read):
            result = self.review(runner)
        self.assertEqual(len(runner.calls), 1)
        self.assertEqual(len(result["accepted"]), 1)
        self.assertEqual(result["selected_capture_ids"], [healthy["capture_id"]])
        self.assertEqual(result["skipped_captures"], [{"capture_id": self.saved["capture_id"],
            "state": "not_interpreted", "reason": "SUPPLIER_REVIEW_LOCAL_FILE_FAILED"}])
        self.assertEqual(result["remaining_inputs"], 0)

    def test_scoped_currentness_failure_does_not_leave_partial_selected_input(self):
        from product_radar.supplier_checks import current_supplier_capture_review
        healthy = self.healthy_capture()
        checks = []

        def current(state, prepared, **kwargs):
            if any(entry["capture_id"] == self.saved["capture_id"] for entry in prepared):
                checks.append(True)
                if len(checks) == 3:
                    raise SafetyError("SUPPLIER_SCOPED_APPLICATION_CHANGED")
            return current_supplier_capture_review(state, prepared, **kwargs)

        runner = CandidateRunner()
        with patch("product_radar.supplier_checks.current_supplier_capture_review", side_effect=current):
            result = self.review(runner)
        self.assertEqual(len(runner.calls), 1)
        self.assertEqual(len(result["accepted"]), 1)
        self.assertEqual(result["selected_capture_ids"], [healthy["capture_id"]])
        self.assertEqual(result["skipped_captures"], [{"capture_id": self.saved["capture_id"],
            "state": "not_interpreted", "reason": "SUPPLIER_SCOPED_APPLICATION_CHANGED"}])
        self.assertEqual(result["remaining_inputs"], 0)

    def test_scoped_malformed_application_keeps_healthy_sibling_settlement(self):
        from product_radar.source_interpretation import _bind_supplier_tasks
        from product_radar.supplier_terms import APPLICATION_KIND
        healthy = self.healthy_capture()
        runner = CandidateRunner()
        prepared = self.review(runner)
        self.assertEqual(len(prepared["accepted"]), 3)  # Shared page meaning, separate capture applications.
        original_calls = len(runner.calls)
        bad_task = self.company_task(self.saved, "bad-scoped-supplier-application")
        healthy_task = self.company_task(healthy, "healthy-scoped-supplier-application")
        state = self.memory.snapshot()
        captured = {}
        for saved in (self.saved, healthy):
            capture = load_saved_supplier_capture(self.memory, saved["path"], state=state)
            item = capture["bundle"]["item"]
            captured[saved["path"]] = {"item": item, "capture_id": saved["capture_id"],
                "capture_hash": digest(capture), "requested_urls": capture["request"]["lead"]["urls"],
                "scoped_prepared": prepare_supplier_candidates(self.memory, saved["path"], state=state)}
        bad_id, application = next((rid, row) for rid, row in state["source_checks"].items()
            if row.get("kind") == APPLICATION_KIND and row["binding"]["capture_id"] == self.saved["capture_id"])
        self.memory.put("source_checks", bad_id, dict(application, binding_hash="changed-binding"), previous=application)
        skipped = []
        bindings = _bind_supplier_tasks(self.memory, captured, as_of=SCOPED_NOW, skipped_captures=skipped)
        self.assertEqual([(row["task_id"], row["state"]) for row in bindings], [(healthy_task, "completed_relevant")])
        self.assertEqual(skipped, [{"capture_id": self.saved["capture_id"], "state": "not_interpreted",
            "reason": "SAVED_SCOPED_SUPPLIER_INVALID"}])
        self.assertEqual(self.memory.snapshot()["query_tasks"][bad_task]["target_results"][self.cid]["state"],
            "interpretation_pending")
        replay = self.review(runner)
        self.assertEqual(len(runner.calls), original_calls)
        self.assertEqual(replay["selected_capture_ids"], [])
        self.assertEqual(replay["reused_capture_ids"], [healthy["capture_id"]])
        self.assertEqual(replay["skipped_captures"], skipped)

    def test_scoped_preparation_and_binding_retain_shared_integrity_errors(self):
        from product_radar.source_interpretation import _bind_supplier_tasks
        saved = self.saved
        state = self.memory.snapshot()
        capture = load_saved_supplier_capture(self.memory, saved["path"], state=state)
        captured = {saved["path"]: {"item": capture["bundle"]["item"], "capture_id": saved["capture_id"],
            "capture_hash": digest(capture), "requested_urls": capture["request"]["lead"]["urls"],
            "scoped_prepared": prepare_supplier_candidates(self.memory, saved["path"], state=state)}}
        runner = CandidateRunner()
        with patch("product_radar.supplier_checks.prepare_supplier_candidates",
                   side_effect=SafetyError("RESEARCH_EVENTS_INVALID")):
            with self.assertRaisesRegex(SafetyError, "^RESEARCH_EVENTS_INVALID$"):
                self.review(runner)
        with patch("product_radar.supplier_checks.current_supplier_capture_review",
                   side_effect=SafetyError("RESEARCH_EVENTS_INVALID")):
            with self.assertRaisesRegex(SafetyError, "^RESEARCH_EVENTS_INVALID$"):
                _bind_supplier_tasks(self.memory, captured, as_of=SCOPED_NOW, skipped_captures=[])
        self.assertEqual(runner.calls, [])

    def test_candidate_preparation_reuses_supplied_view_and_default_keeps_fresh_reads(self):
        state = self.memory.snapshot()
        before = deepcopy(state)
        with patch.object(self.memory, "snapshot", wraps=self.memory.snapshot) as read:
            supplied = prepare_supplier_candidates(self.memory, self.saved["path"], state=state)
            self.assertEqual(read.call_count, 0)
            ordinary = prepare_supplier_candidates(self.memory, self.saved["path"])
            self.assertEqual(read.call_count, 2)
        self.assertEqual(supplied, ordinary)
        self.assertEqual(state, before)

    def test_scoped_replay_has_constant_scan_reads_and_no_new_f(self):
        runner = CandidateRunner()
        with patch("product_radar.supplier_checks.utc_now", return_value=SCOPED_NOW):
            first = review_supplier_results(self.memory, runner, as_of=SCOPED_NOW,
                concept_ids=[self.cid], scoped_concept_ids=[self.cid])
        self.assertEqual(first["failures"], {})
        self.assertEqual(len(runner.calls), 3)
        with patch.object(self.memory, "snapshot", wraps=self.memory.snapshot) as read:
            replay = review_supplier_results(self.memory, runner, as_of=SCOPED_NOW,
                concept_ids=[self.cid], scoped_concept_ids=[self.cid])
        self.assertEqual(read.call_count, 2)
        self.assertEqual(replay["reused_capture_ids"], [self.saved["capture_id"]])
        self.assertEqual(replay["remaining_inputs"], 0)
        self.assertEqual(len(runner.calls), 3)

    def test_prepared_candidate_cannot_execute_against_a_changed_current_product(self):
        state = self.memory.snapshot()
        prepared = prepare_supplier_candidates(self.memory, self.saved["path"], state=state)
        current = self.memory.snapshot()["concepts"][self.cid]
        self.memory.put("concepts", self.cid, dict(current, status="retired"), previous=current)
        runner = CandidateRunner()
        result = extract_supplier_candidates(self.memory, prepared, runner)
        self.assertEqual(runner.calls, [])
        self.assertEqual(result["accepted"], {})
        self.assertEqual(set(result["failures"].values()), {"SUPPLIER_CAPTURE_CONCEPT_EXCLUDED"})


if __name__ == "__main__":
    unittest.main()
