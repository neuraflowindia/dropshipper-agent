"""Exact bounded supplier-answer settlement using disposable local sources."""

from copy import deepcopy
from pathlib import Path
import unittest
from unittest.mock import patch

from product_radar.source_interpretation import review_supplier_results
from product_radar.supplier_capture_store import capture_saved_supplier_bundle
from product_radar.research_ai import Jobs
from product_radar.research_memory import key
from product_radar.storage import atomic_json
from product_radar.work_turns import bind_work_turn, open_work_turn
import product_radar.programme as programme
import product_radar.source_interpretation as interpretation
import test_source_interpretation as source_fixtures
import test_programme_model_continuation as continuation_fixtures
from test_supplier_capture_store import Reader, BASE
import test_suppliers as supplier_fixtures
import test_supplier_quote_repair as quote_fixtures

NOW = source_fixtures.NOW
TEXT = "Main Domestic Market / All India. We serve our consumer base."


class QuoteRunner:
    identity = "supplier-quote-continuation-fixture-v1"

    def __init__(self, *, repair_valid=True, mutate=None):
        self.calls, self.repair_valid, self.mutate = [], repair_valid, mutate

    def __call__(self, role, payload, folder):
        self.calls.append(deepcopy(payload))
        rows = []
        for item in payload["items"]:
            page = item["pages"][0]["id"]
            value = supplier_fixtures.result(item["id"])
            value["relevance"]["references"] = [page]
            term = supplier_fixtures.term("india_coverage", "Yes", "All India", reference=page)
            corrected = bool(payload.get("supplier_repair")) and self.repair_valid
            term["quotes"].append({"page_id": page,
                "excerpt": "We serve our consumer base." if corrected else "We serve our customer base."})
            value["terms"] = [term]
            rows.append(value)
        if self.mutate and payload.get("supplier_repair"):
            self.mutate()
        return {"results": rows}, {"input_tokens": 1, "cached_input_tokens": 0, "output_tokens": 1}


class SupplierQuoteRepairContinuationTests(unittest.TestCase):
    setUp = source_fixtures.SourceInterpretationTests.setUp
    supplier_search_task = source_fixtures.SourceInterpretationTests.supplier_search_task
    supplier_company_task = source_fixtures.SourceInterpretationTests.supplier_company_task

    def capture(self, cid="first", suffix=""):
        url = BASE + "/product/caddy" + suffix
        return capture_saved_supplier_bundle(self.memory, {"concept_id": cid, "lead": {
            "id": "quote-fixture-lead" + suffix, "urls": [url]}},
            fetcher=Reader({url: {"text": TEXT}}))

    def review(self, runner, *, as_of=NOW, completed_at=NOW, **options):
        with patch("product_radar.suppliers.utc_now", return_value=completed_at), \
             patch("product_radar.research_ai.utc_now", return_value=completed_at), \
             patch("product_radar.source_interpretation.utc_now", return_value=completed_at):
            return review_supplier_results(self.memory, runner, as_of=as_of, **options)

    def tasks(self, saved):
        url = BASE + "/product/caddy"
        search, check = self.supplier_search_task([url])
        company = self.supplier_company_task([saved], check, [url])
        return search, company

    def test_one_valid_correction_finishes_exact_supplier_tasks_and_keeps_original_dates(self):
        saved = self.capture()
        tasks = self.tasks(saved)
        raw = Path(saved["path"]).read_bytes()
        runner = QuoteRunner()
        result = self.review(runner)
        self.assertEqual(len(runner.calls), 2)
        self.assertEqual(len(result["accepted"]), 1, result)
        self.assertEqual(result["remaining_inputs"], 0)
        self.assertEqual(result["terminal_inputs"], {})
        state = self.memory.snapshot()
        for task in tasks:
            self.assertEqual(state["query_tasks"][task]["target_results"]["first"]["state"], "completed_relevant")
        assessment = next(iter(state["supplier_assessments"].values()))
        self.assertEqual(set(assessment["source_dates"].values()), {"2026-10-06T08:00:00+00:00"})
        self.assertEqual(Path(saved["path"]).read_bytes(), raw)
        self.review(runner)
        self.assertEqual(len(runner.calls), 2)

    def test_rejected_correction_settles_with_limitation_without_retry_or_false_absence(self):
        saved = self.capture()
        tasks = self.tasks(saved)
        runner = QuoteRunner(repair_valid=False)
        result = self.review(runner)
        self.assertEqual(len(runner.calls), 2)
        self.assertEqual(result["accepted"], {})
        self.assertEqual(result["remaining_inputs"], 0, result)
        self.assertEqual(len(result["terminal_inputs"]), 1)
        state = self.memory.snapshot()
        self.assertEqual(state["supplier_assessments"], {})
        for task in tasks:
            target = state["query_tasks"][task]["target_results"]["first"]
            self.assertEqual(target["state"], "insufficient")
            self.assertNotIn("could not be read", target["reason"])
        originals = {path: path.read_bytes() for path in (self.memory.root / "ai_jobs").glob("job-*/result.json")}
        before = self.memory.snapshot_with_provenance()
        replay = self.review(runner)
        self.assertEqual(replay["remaining_inputs"], 0)
        self.assertEqual(replay["selected_inputs"], [])
        self.assertEqual(len(replay["terminal_inputs"]), 1)
        self.assertEqual(len(runner.calls), 2)
        self.assertEqual(self.memory.snapshot_with_provenance(), before)
        self.assertTrue(all(path.read_bytes() == raw for path, raw in originals.items()))

    def test_terminal_capture_clears_old_programme_deferral_without_claiming_accepted_supplier(self):
        saved = self.capture()
        runner = QuoteRunner(repair_valid=False)
        self.review(runner)
        record = {"concept_ids": ["first"], "deferrals": {"suppliers:" + saved["capture_id"]: {
            "stage": "suppliers", "target": saved["capture_id"], "binding": saved["capture_id"],
            "at": "2026-10-07T10:00:00+00:00", "next_retry_at": "2026-10-07T10:05:00+00:00",
            "reason": "AI_SUPPLIER_QUOTE_INVALID"}}}
        with patch("product_radar.research_ai.utc_now", return_value=NOW):
            result = programme._suppliers(self.memory, record,
                {"at": NOW, "runner": runner, "supplier_limit": 5, "retry_seconds": 300})
        self.assertEqual(result["remaining_inputs"], 0)
        self.assertEqual(result["completed_results"], 0)
        self.assertEqual(len(result["terminal_inputs"]), 1)
        self.assertEqual(record["deferrals"], {})
        self.assertEqual(len(runner.calls), 2)

    def test_changed_product_during_correction_cannot_receive_assessment_or_settle_tasks(self):
        saved = self.capture()
        tasks = self.tasks(saved)
        def change_product():
            old = self.memory.snapshot()["concepts"]["first"]
            self.memory.put("concepts", "first", dict(old, status="excluded"), previous=old)
        runner = QuoteRunner(mutate=change_product)
        result = self.review(runner)
        self.assertEqual(len(runner.calls), 2)
        self.assertEqual(result["accepted"], {})
        self.assertEqual(self.memory.snapshot()["supplier_assessments"], {})
        for task in tasks:
            self.assertEqual(self.memory.snapshot()["query_tasks"][task]["target_results"]["first"]["state"],
                             "interpretation_pending")
        self.assertIn("SUPPLIER_CAPTURE_CONCEPT_EXCLUDED", set(result["failures"].values()))
        self.assertTrue((self.memory.root / "ai_jobs").is_dir())

    def test_product_changed_after_outer_guard_is_rechecked_inside_assessment_commit(self):
        self.capture()
        runner = QuoteRunner()
        original_commit = self.memory.commit
        changed = []

        def change_before_commit(event_id, changes, **options):
            if any(row.get("table") == "supplier_assessments" for row in changes) and not changed:
                changed.append(True)
                old = self.memory.snapshot()["concepts"]["first"]
                self.memory.put("concepts", "first", dict(old, status="excluded"), previous=old)
            return original_commit(event_id, changes, **options)

        with patch.object(self.memory, "commit", side_effect=change_before_commit):
            result = self.review(runner)
        self.assertEqual(changed, [True])
        self.assertEqual(len(runner.calls), 2)
        self.assertEqual(self.memory.snapshot()["supplier_assessments"], {})
        self.assertIn("SUPPLIER_CAPTURE_CONCEPT_EXCLUDED", set(result["failures"].values()))

    def test_terminal_correction_after_historical_cutoff_cannot_settle_earlier_checks(self):
        saved = self.capture()
        tasks = self.tasks(saved)
        later = "2026-10-08T11:00:00+00:00"
        runner = QuoteRunner(repair_valid=False)
        earlier = self.review(runner, completed_at=later)
        self.assertEqual(earlier["terminal_inputs"], {})
        self.assertEqual(earlier["remaining_inputs"], 1)
        for task in tasks:
            self.assertEqual(self.memory.snapshot()["query_tasks"][task]["target_results"]["first"]["state"],
                             "interpretation_pending")
        current = self.review(runner, as_of=later, completed_at=later)
        self.assertEqual(current["remaining_inputs"], 0)
        self.assertEqual(len(current["terminal_inputs"]), 1)
        self.assertEqual(len(runner.calls), 2)
        for task in tasks:
            self.assertEqual(self.memory.snapshot()["query_tasks"][task]["target_results"]["first"]["state"],
                             "insufficient")

    def test_terminal_capture_changed_before_binding_remains_unfinished_not_falsely_settled(self):
        saved = self.capture()
        tasks = self.tasks(saved)
        runner = QuoteRunner(repair_valid=False)
        with patch.object(interpretation, "_bind_supplier_tasks", return_value=[]):
            self.assertEqual(len(self.review(runner)["terminal_inputs"]), 1)
        original_bind = interpretation._bind_supplier_tasks

        def change_before_binding(*args, **options):
            old = self.memory.snapshot()["concepts"]["first"]
            self.memory.put("concepts", "first", dict(old, status="excluded"), previous=old)
            return original_bind(*args, **options)

        with patch.object(interpretation, "_bind_supplier_tasks", side_effect=change_before_binding):
            result = self.review(runner)
        self.assertEqual(result["terminal_inputs"], {})
        self.assertEqual(result["remaining_inputs"], 1, result)
        self.assertIn("SUPPLIER_CAPTURE_CONCEPT_EXCLUDED", set(result["failures"].values()))
        self.assertEqual(len(runner.calls), 2)
        for task in tasks:
            self.assertEqual(self.memory.snapshot()["query_tasks"][task]["target_results"]["first"]["state"],
                             "interpretation_pending")

    def pending_correction(self):
        from product_radar.suppliers import _supplier_payload
        from product_radar.supplier_quote_repair import supplier_model_question
        saved = self.capture()
        runner = QuoteRunner()
        with patch("product_radar.research_ai.utc_now", return_value=NOW):
            Jobs(self.memory, runner).run("F", _supplier_payload([saved["bundle"]["item"]]))
        question = supplier_model_question(self.memory, saved["bundle"]["item"], state=self.memory.snapshot())
        self.assertEqual(question["state"], "repair")
        record = programme.prepare_programme(self.memory, "supplier-quote-normal-continuation",
                                             concept_ids=["first"], as_of=NOW)
        helper = continuation_fixtures.SpecificModelLaneReadinessTests()
        turn = helper.spend(self.memory, record["programme_id"], NOW)
        return saved, runner, record, helper, turn

    def test_closed_spent_turn_wakes_exact_correction_then_uses_one_normal_next_turn(self):
        saved, runner, record, helper, turn = self.pending_correction()
        old = deepcopy(self.memory.snapshot()["batch_runs"][turn["id"]])
        real_ready = programme._model_question_ready
        questions = []

        def supplier_only(*args, **options):
            if args[3] != "F":
                return {"ready": False, "fresh_items": [], "original_item_counts": []}
            questions.append(deepcopy(args[4]))
            return real_ready(*args, **options)

        before = self.memory.snapshot_with_provenance()
        with patch.object(programme, "_model_question_ready", side_effect=supplier_only):
            hint = helper.hint(self.memory, record, NOW, runner)
        self.assertEqual(hint["purpose"], "F")
        self.assertEqual(self.memory.snapshot_with_provenance(), before)
        self.assertEqual(len(questions), 1)
        self.assertIn("supplier_repair", questions[0])
        self.assertEqual(len(questions[0]["items"]), 1)
        next_turn = open_work_turn(self.memory, record["programme_id"], at=NOW, policy=turn["policy"])
        with bind_work_turn(self.memory, next_turn["id"]):
            result = self.review(runner)
        self.assertEqual(len(result["accepted"]), 1, result)
        self.assertEqual(len(runner.calls), 2)
        self.assertEqual(self.memory.snapshot()["batch_runs"][next_turn["id"]]["counters"]["model_executions"], 1)
        self.assertEqual(self.memory.snapshot()["batch_runs"][turn["id"]], old)

    def test_unsent_correction_owned_by_different_runner_does_not_open_empty_turn(self):
        from product_radar.supplier_quote_repair import _claim_path, _request, supplier_model_question
        saved, runner, record, helper, turn = self.pending_correction()
        question = supplier_model_question(self.memory, saved["bundle"]["item"], state=self.memory.snapshot())
        request = _request(question["payload"], "different-saved-owner-v1")
        atomic_json(_claim_path(self.memory, question["scope_id"]), {"schema_version": 1,
            "scope_id": question["scope_id"], "job_id": key("job", request), "request": request})
        real_ready = programme._model_question_ready

        def supplier_only(*args, **options):
            return (real_ready(*args, **options) if args[3] == "F" else
                    {"ready": False, "fresh_items": [], "original_item_counts": []})

        before = self.memory.snapshot_with_provenance()
        with patch.object(programme, "_model_question_ready", side_effect=supplier_only):
            self.assertIsNone(helper.hint(self.memory, record, NOW, runner))
        self.assertEqual(self.memory.snapshot_with_provenance(), before)
        self.assertEqual(len(runner.calls), 1)

    def test_closed_original_full_current_group_wakes_without_reset_or_preparing_retry(self):
        saved = [self.capture(), self.capture(cid="second", suffix="-second")]
        originals = [row["bundle"]["item"] for row in saved]
        job_id, base, failed = quote_fixtures.SupplierQuoteRepairTests.fail_closed(self, originals)
        self.assertEqual(set(failed["failures"].values()), {"AI_RATE_LIMITED"})
        runner = quote_fixtures.SupplierQuoteRepairTests.cooperative_runner(self)
        record = programme.prepare_programme(self.memory, "supplier-original-retry-continuation",
            concept_ids=["first", "second"], as_of=quote_fixtures.AFTER)
        helper = continuation_fixtures.SpecificModelLaneReadinessTests()
        turn = helper.spend(self.memory, record["programme_id"], quote_fixtures.AFTER)
        old = deepcopy(self.memory.snapshot()["batch_runs"][turn["id"]])
        real_ready, questions = programme._model_question_ready, []

        def supplier_only(*args, **options):
            if args[3] != "F":
                return {"ready": False, "fresh_items": [], "original_item_counts": []}
            questions.append(deepcopy(args[4]))
            return real_ready(*args, **options)

        before = self.memory.snapshot_with_provenance()
        with patch.object(programme, "_model_question_ready", side_effect=supplier_only):
            hint = helper.hint(self.memory, record, quote_fixtures.AFTER, runner)
        self.assertEqual(hint["purpose"], "F")
        self.assertEqual(questions, [before["state"]["jobs"][job_id]["request"]["payload"]])
        self.assertEqual(self.memory.snapshot_with_provenance(), before)
        self.assertEqual(self.memory.snapshot()["batch_runs"][turn["id"]], old)
        self.assertFalse((base / "retries.json").exists())
        self.assertEqual(runner.calls, [])

        # Leaving out a current sibling, or its own retry still being due
        # later, cannot use the full original group as fresh-turn authority.
        partial = deepcopy(record)
        partial["concept_ids"] = ["first"]
        held = deepcopy(record)
        capture_id = saved[1]["capture_id"]
        held["deferrals"]["suppliers:" + capture_id] = {
            "stage": "suppliers", "target": capture_id, "binding": capture_id,
            "at": quote_fixtures.AFTER, "next_retry_at": "2026-10-12T11:00:00+00:00",
            "reason": "AI_MODEL_RETRY_WAIT"}
        for restricted in (partial, held):
            with self.subTest(partial=restricted is partial), \
                 patch.object(programme, "_model_question_ready", side_effect=supplier_only):
                self.assertIsNone(helper.hint(self.memory, restricted, quote_fixtures.AFTER, runner))
        self.assertEqual(self.memory.snapshot_with_provenance(), before)
        self.assertFalse((base / "retries.json").exists())
        self.assertEqual(runner.calls, [])

    def inventory_stage(self, runner, tasks, *, as_of=NOW):
        from product_radar.inventory_intake import _new_work
        record = programme.prepare_programme(self.memory, "supplier-inventory-exact-outcomes",
                                             concept_ids=[], as_of=as_of)
        work = _new_work(self.memory.snapshot(), record["client_id"], "first", "supplier",
                         [{"code": "supplier_review_required"}], None, as_of)
        work["progress"].update(source_collected=True, task_ids=list(tasks), linked_run_id="full-list")
        self.memory.put("batch_runs", work["id"], work)
        record["inventory_work_ids"] = [work["id"]]
        context = {"at": as_of, "fixed_as_of": as_of, "runner": runner,
                   "supplier_limit": 5, "retry_seconds": 300}
        turn = open_work_turn(self.memory, record["programme_id"], at=as_of)
        with bind_work_turn(self.memory, turn["id"]), \
             patch("product_radar.suppliers.utc_now", return_value=as_of), \
             patch("product_radar.research_ai.utc_now", return_value=as_of):
            programme._run_inventory_stage(self.memory, record, context, "suppliers")
        return self.memory.snapshot()["batch_runs"][work["id"]]["progress"]

    def test_inventory_own_unreadable_capture_is_not_complete_from_zero_pending_count(self):
        saved = self.capture()
        tasks = self.tasks(saved)
        atomic_json(Path(saved["path"]), {"schema_version": 999, "synthetic_unreadable": True})
        runner = QuoteRunner()
        progress = self.inventory_stage(runner, tasks)
        self.assertEqual(progress["last_result"]["remaining_inputs"], 0)
        self.assertEqual(progress["state"], "waiting")
        self.assertTrue(progress["last_result"]["skipped_captures"])
        self.assertEqual(runner.calls, [])

    def test_inventory_unrelated_unreadable_capture_does_not_block_own_settled_result(self):
        saved = self.capture()
        tasks = self.tasks(saved)
        unrelated = self.capture(cid="second", suffix="-unrelated")
        atomic_json(Path(unrelated["path"]), {"schema_version": 999, "synthetic_unreadable": True})
        progress = self.inventory_stage(QuoteRunner(), tasks)
        self.assertEqual(progress["state"], "complete", progress)
        self.assertTrue(progress["last_result"]["skipped_captures"])

    def test_inventory_rejected_correction_finishes_as_limitation_not_positive_supplier(self):
        saved = self.capture()
        progress = self.inventory_stage(QuoteRunner(repair_valid=False), self.tasks(saved))
        self.assertEqual(progress["state"], "complete", progress)
        self.assertEqual(progress["last_result"]["completed_results"], 0)
        self.assertEqual(len(progress["last_result"]["terminal_inputs"]), 1)
        self.assertIn("limitation", progress["reason"])

    def test_inventory_future_valid_supplier_answer_does_not_finish_historical_obligation(self):
        saved = self.capture()
        tasks = self.tasks(saved)
        runner = QuoteRunner()
        later = "2026-10-08T11:00:00+00:00"
        self.assertEqual(len(self.review(runner, completed_at=later)["accepted"]), 1)
        progress = self.inventory_stage(runner, tasks)
        self.assertEqual(progress["state"], "waiting", progress)
        self.assertEqual(len(runner.calls), 2)

    def test_inventory_already_bound_later_answer_still_cannot_finish_historical_obligation(self):
        saved = self.capture()
        tasks = self.tasks(saved)
        runner = QuoteRunner()
        later = "2026-10-08T11:00:00+00:00"
        self.assertEqual(len(self.review(runner, as_of=later, completed_at=later)["accepted"]), 1)
        self.assertTrue(all(self.memory.snapshot()["query_tasks"][task]["target_results"]["first"]["state"]
                            == "completed_relevant" for task in tasks))
        progress = self.inventory_stage(runner, tasks)
        self.assertEqual(progress["state"], "waiting", progress)
        self.assertEqual(len(runner.calls), 2)


if __name__ == "__main__":
    unittest.main()
