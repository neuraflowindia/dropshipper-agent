"""Disposable preparation diagnostics; no providers or canonical research.

Real operation logs and the real planner use a RAM-only memory fixture and
unique temporary paths. Preparing questions is not collecting website data.
"""

from contextlib import contextmanager, redirect_stderr
from copy import deepcopy
from datetime import timedelta
import io
import json
from pathlib import Path
import sys
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "tests"))

import test_source_planning_owner_scope as fixtures
from product_radar.models import SafetyError, timestamp
from product_radar.research_memory import key
import product_radar.operations as operations
import product_radar.source_acquisitions as acquisitions
import product_radar.source_coverage as coverage

NOW = fixtures.NOW
EVENT = "source_preparation_progress"
# The retired Google Search supplier route is never planned (13 of 14 routes).
ROUTES = coverage.PLANNED_ROUTES
R = len(ROUTES)


class SourcePreparationProgressTests(unittest.TestCase):
    def setUp(self):
        self.fixture = fixtures.SourcePlanningOwnerScopeTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.root, self.memory = self.fixture.root, self.fixture.memory

    @contextmanager
    def recording(self):
        with operations.operation_run(self.root) as recorder, operations.operation_writer():
            operations.record_event("phase_started", stage="acquire", phase="source_preparation",
                                    useful_result=False)
            yield recorder

    def rows(self, recorder):
        path = recorder.folder / (recorder.run_id + ".jsonl")
        return [json.loads(line) for line in path.read_text().splitlines()]

    def progress_rows(self, recorder):
        return [row for row in self.rows(recorder) if row["event"] == EVENT]

    def current(self):
        return operations.read_operation_status(self.root)

    def prepare(self, builder=None, *, concept_ids=None):
        return coverage.prepare_all_source_tasks(self.memory, run_id="synthetic-week",
            request_builder=self.fixture.builder() if builder is None else builder,
            concept_ids=["first", "second"] if concept_ids is None else concept_ids, as_of=NOW)

    def generic_builder(self, context):
        return {"query": "Synthetic custom question", "country": "IN"}

    def assert_no_complete(self, recorder):
        self.assertNotIn("complete", [row["phase_state"] for row in self.progress_rows(recorder)])

    def test_normal_walk_has_bounded_exact_order_and_preparation_only_counts(self):
        with self.recording() as recorder:
            result = self.prepare()
            current = self.current()
        rows = self.progress_rows(recorder)
        expected = ["read_saved_products"]
        for _ in range(2):
            expected += ["product_context"] + ["question"] * R
        expected += ["prepare_tasks", "read_prepared_run", "save_pending_questions", "complete"]
        self.assertEqual([row["phase_state"] for row in rows], expected)
        self.assertEqual(len(rows), (R + 1) * 2 + 5)
        questions = [row for row in rows if row["phase_state"] == "question"]
        self.assertEqual([(row["concept_id"], row["route"]) for row in questions],
            [(cid, route) for cid in ("first", "second") for route in ROUTES])
        self.assertEqual(R, len(coverage.ROUTES) - 1)
        self.assertNotIn("google-suppliers", {row["route"] for row in questions})
        self.assertNotIn("google-suppliers", {task["route"] for task in self.memory.state["query_tasks"].values()})
        self.assertEqual([row["scanned"] for row in questions], list(range(2 * R)))
        self.assertEqual([row["remaining"] for row in questions], list(range(2 * R, 0, -1)))
        self.assertTrue(all(row["stage"] == "acquire" and row["useful_result"] is False for row in rows))
        # Pending: each product's company pages (no destination yet), its three
        # dropshipping-platform questions (no platform catalogue has been read yet),
        # the Meta depth follow-up (no free read yet) and its three paid
        # quick-commerce checks (reserved for later: no passed checklist yet, W5).
        platforms = [route for route in ROUTES if coverage.ROUTES[route]["source"] == "dropship-platforms"]
        commerce = [route for route in ROUTES if coverage.ROUTES[route]["source"] == "quickcommerce"]
        waiting = {"company-pages", *platforms, *commerce} | ({"meta-ads-depth"} & set(ROUTES))
        pending = 2 * len(waiting)
        self.assertEqual((len(platforms), len(commerce)), (3, 3))
        self.assertEqual({row["route"] for row in result["pending_preparation"]}, waiting)
        self.assertEqual(rows[-1]["counts"], {"questions_total": 2 * R,
            "questions_prepared": 2 * R - pending, "questions_pending": pending})
        self.assertEqual(rows[-1]["scanned"], 2 * R)
        self.assertEqual(rows[-1]["remaining"], 0)
        self.assertEqual(len(result["pending_preparation"]), pending)
        self.assertEqual({row["reason"] for row in result["pending_preparation"] if row["route"] in platforms},
                         {"DROPSHIP_CATALOGUE_PENDING"})
        self.assertEqual({row["reason"] for row in result["pending_preparation"] if row["route"] in commerce},
                         {"QUICKCOMMERCE_RESERVED_CHECKLIST"})
        self.assertEqual(result["source_attempts_created"], 0)
        self.assertEqual(current["counts"]["completed_results"], 0)
        self.assertEqual(current["counts"]["failures"], 0)
        self.assertEqual(current["counts"]["deferred"], 0)
        self.assertIsNone(current["last_useful_result_at"])
        self.assertTrue(all(not task["attempt_refs"] and task["generation"] == 0
                            for task in self.memory.state["query_tasks"].values()))

    def test_current_question_is_visible_inside_builder_before_it_returns(self):
        visited = []
        def builder(context):
            current = self.current()
            row = current["last_event"]
            self.assertEqual(row["event"], EVENT)
            self.assertEqual(row["phase_state"], "question")
            self.assertEqual((row["concept_id"], row["route"]),
                             (context["concept_id"], context["route"]))
            self.assertEqual(row["scanned"], len(visited))
            self.assertEqual(row["remaining"], 2 * R - len(visited))
            self.assertEqual(row["counts"], {"questions_total": 2 * R,
                "questions_prepared": len(visited), "questions_pending": 0})
            self.assertEqual(current["phase"], "source_preparation")
            self.assertEqual(current["counts"]["completed_results"], 0)
            visited.append((context["concept_id"], context["route"]))
            return self.generic_builder(context)
        with self.recording():
            self.prepare(builder)
        self.assertEqual(len(visited), 2 * R)

    def test_read_context_and_save_boundaries_are_visible_before_each_operation(self):
        real_snapshot = self.memory.snapshot
        real_context = coverage._product_context
        real_prepare, real_upsert = coverage.prepare_source_tasks, coverage._upsert
        snapshots, contexts, saves = [], [], []
        def snapshot():
            snapshots.append(self.current()["last_event"]["phase_state"])
            return real_snapshot()
        def context(state, cid):
            row = self.current()["last_event"]
            self.assertEqual((row["phase_state"], row["concept_id"]), ("product_context", cid))
            contexts.append(cid)
            return real_context(state, cid)
        def prepare(memory, questions, **options):
            self.assertEqual(self.current()["last_event"]["phase_state"], "prepare_tasks")
            self.assertIsNone(acquisitions._READINESS_OWNERS.get())
            before = len(self.progress_rows(recorder))
            result = real_prepare(memory, questions, **options)
            self.assertEqual(len(self.progress_rows(recorder)), before)
            saves.append("prepare_tasks")
            return result
        def upsert(memory, table, rid, value, old):
            self.assertEqual(self.current()["last_event"]["phase_state"], "save_pending_questions")
            self.assertEqual((table, rid), ("batch_runs", key("source-run", "synthetic-week")))
            self.assertIn("pending_preparation", value)
            self.assertIsNone(acquisitions._READINESS_OWNERS.get())
            saves.append("save_pending_questions")
            return real_upsert(memory, table, rid, value, old)
        with self.recording() as recorder, patch.object(self.memory, "snapshot", side_effect=snapshot), \
                patch.object(coverage, "_product_context", side_effect=context), \
                patch.object(coverage, "prepare_source_tasks", side_effect=prepare), \
                patch.object(coverage, "_upsert", side_effect=upsert):
            self.prepare()
        self.assertEqual(snapshots[0], "read_saved_products")
        self.assertIn("read_prepared_run", snapshots)
        self.assertIn("save_pending_questions", snapshots)
        self.assertEqual(contexts, ["first", "second"])
        self.assertEqual(saves, ["prepare_tasks", "save_pending_questions"])

    def test_progress_does_not_reset_parent_deadline_or_hide_no_result_warning(self):
        at = [NOW]
        # Past the no-result warning (30 minutes) but inside the phase limit.
        later = (timestamp(NOW) + timedelta(seconds=1860)).isoformat()
        with patch.object(operations, "utc_now", side_effect=lambda: at[0]), \
                operations.operation_run(self.root) as recorder, operations.operation_writer():
            operations.record_event("prior_result", useful_result=True)
            operations.record_event("phase_started", stage="acquire", phase="source_preparation",
                                    deadline_seconds=4000, useful_result=False)
            original = operations.read_operation_status(self.root, as_of=NOW)
            at[0] = later
            self.prepare()
            current = operations.read_operation_status(self.root, as_of=later)
            self.assertEqual(current["phase"], "source_preparation")
            self.assertEqual(current["phase_started_at"], original["phase_started_at"])
            self.assertEqual(current["phase_deadline_seconds"], 4000)
            self.assertEqual(current["last_useful_result_at"], original["last_useful_result_at"])
            self.assertEqual(current["counts"]["completed_results"], 1)
            self.assertEqual(current["seconds_in_current_phase"], 1860)
            self.assertEqual(current["seconds_since_useful_result"], 1860)
            self.assertIn("No completed result", current["attention"])
            self.assertEqual(current["message"],
                             "Preparing website searches from the saved product information.")
        phase_events = [row for row in self.rows(recorder)
                        if row["event"] in {"phase_started", "phase_finished", "stage_started"}]
        self.assertEqual([row["event"] for row in phase_events], ["phase_started"])

    def test_recording_and_no_recorder_produce_identical_plans_and_research_state(self):
        before = deepcopy(self.memory.state)
        control = fixtures._Memory(self.root / "control-memory", deepcopy(before))
        actual = fixtures._Memory(self.root / "recorded-memory", deepcopy(before))
        def prepare(memory):
            return coverage.prepare_all_source_tasks(memory, run_id="synthetic-week",
                request_builder=self.fixture.builder(), concept_ids=["first", "second"], as_of=NOW)
        self.assertIsNone(operations._ACTIVE.get())
        expected = prepare(control)
        with self.recording():
            observed = prepare(actual)
        self.assertEqual(observed, expected)
        self.assertEqual(actual.state, control.state)
        self.assertEqual(actual.commits, control.commits)
        self.assertEqual(self.memory.state, before)

    def test_missing_description_is_logged_before_validation_and_next_product_continues(self):
        self.memory.state["concepts"]["first"]["description"] = None
        real_context, entered = coverage._product_context, []
        def context(state, cid):
            row = self.current()["last_event"]
            self.assertEqual((row["phase_state"], row["concept_id"]), ("product_context", cid))
            entered.append(cid)
            return real_context(state, cid)
        with self.recording() as recorder, patch.object(coverage, "_product_context", side_effect=context):
            result = self.prepare(self.generic_builder)
        rows = self.progress_rows(recorder)
        questions = [row for row in rows if row["phase_state"] == "question"]
        self.assertEqual(entered, ["first", "second"])
        self.assertEqual(len(questions), R)
        self.assertEqual({row["concept_id"] for row in questions}, {"second"})
        self.assertEqual(questions[0]["scanned"], R)
        self.assertEqual(questions[0]["remaining"], R)
        self.assertEqual(questions[0]["counts"]["questions_pending"], R)
        self.assertEqual(len(result["pending_preparation"]), R)
        self.assertEqual({row["reason"] for row in result["pending_preparation"]},
                         {"SOURCE_PRODUCT_DESCRIPTION_INVALID"})
        self.assertEqual({row["route"] for row in result["pending_preparation"]}, set(ROUTES))
        self.assertEqual(rows[-1]["counts"], {"questions_total": 2 * R,
            "questions_prepared": R, "questions_pending": R})
        self.assertLessEqual(len(rows), (R + 1) * 2 + 5)

    def test_excluded_product_and_recoverable_question_gap_do_not_stop_other_questions(self):
        self.memory.state["concepts"]["first"]["status"] = "excluded"
        visited = []
        def builder(context):
            visited.append((context["concept_id"], context["route"]))
            if context["route"] == "company-pages":
                raise SafetyError("SOURCE_SUPPLIER_DESTINATIONS_PENDING")
            return self.generic_builder(context)
        with self.recording() as recorder:
            result = self.prepare(builder)
            current = self.current()
        rows = self.progress_rows(recorder)
        self.assertEqual(len(visited), R)
        self.assertEqual({cid for cid, _ in visited}, {"second"})
        self.assertEqual(len([row for row in rows if row["phase_state"] == "question"]), 2 * R)
        # Prepared includes the R explicit not-applicable decisions, not 2R-1
        # saved tasks, source attempts or newly collected results.
        self.assertEqual(rows[-1]["counts"], {"questions_total": 2 * R,
            "questions_prepared": 2 * R - 1, "questions_pending": 1})
        self.assertEqual(len(result["pending_preparation"]), 1)
        applicability = [row for row in self.memory.state["source_checks"].values()
                         if row.get("kind") == "all_source_applicability_v1"]
        self.assertEqual(len(applicability), R)
        self.assertEqual(current["counts"]["completed_results"], 0)
        self.assertEqual(current["counts"]["deferred"], 0)
        self.assertEqual(result["source_attempts_created"], 0)

    def test_zero_products_still_records_each_read_and_save_stage_and_zero_counts(self):
        with self.recording() as recorder:
            result = self.prepare(self.generic_builder, concept_ids=[])
        rows = self.progress_rows(recorder)
        self.assertEqual([row["phase_state"] for row in rows], ["read_saved_products",
            "prepare_tasks", "read_prepared_run", "save_pending_questions", "complete"])
        self.assertEqual(rows[-1]["counts"], {"questions_total": 0,
            "questions_prepared": 0, "questions_pending": 0})
        self.assertEqual(result["products"], 0)
        self.assertEqual(result["routes"], R)
        self.assertEqual(result["source_attempts_created"], 0)

    def test_product_labels_queries_and_links_are_not_copied_to_diagnostics(self):
        private_label = "private-product-name-which-must-not-appear"
        private_query = "private-search-wording-which-must-not-appear"
        private_url = "https://example.com/private-product-path-not-for-diagnostics"
        self.memory.state["concepts"]["first"]["description"]["label"] = private_label
        def builder(context):
            return {"query": private_query, "country": "IN", "url": private_url}
        with self.recording() as recorder:
            self.prepare(builder)
            status_raw = recorder.status_path.read_text()
        log_raw = (recorder.folder / (recorder.run_id + ".jsonl")).read_text()
        for value in (private_label, private_query, private_url):
            self.assertNotIn(value, log_raw)
            self.assertNotIn(value, status_raw)
        self.assertIn('"concept_id":"first"', log_raw)
        self.assertIn('"route":"meta-ads"', log_raw)
        self.assertEqual(self.memory.state["concepts"]["first"]["description"]["label"], private_label)
        self.assertTrue(all(row["request"]["query"] == private_query
                            and row["request"]["url"] == private_url
                            for row in self.memory.state["query_tasks"].values()))

    def test_unsafe_identifier_text_is_filtered_out_of_progress_records(self):
        unsafe = "private product name and spaces"
        self.memory.state["concepts"][unsafe] = self.memory.state["concepts"].pop("first")
        # Selection normally rejects unsafe IDs. This disposable override
        # exercises the diagnostic filter, not a relaxation of selection.
        with self.recording() as recorder, patch.object(coverage, "_active_concepts", return_value=[unsafe]), \
                patch.object(coverage, "prepare_source_tasks", return_value={"source_attempts_created": 0}), \
                patch.object(coverage, "_upsert"):
            self.memory.state["batch_runs"][key("source-run", "synthetic-week")] = {}
            self.prepare(self.generic_builder)
            status_raw = recorder.status_path.read_text()
        self.assertNotIn(unsafe, (recorder.folder / (recorder.run_id + ".jsonl")).read_text())
        self.assertNotIn(unsafe, status_raw)
        self.assertTrue(all("concept_id" not in row for row in self.progress_rows(recorder)))

    def test_status_write_failure_does_not_abort_or_change_preparation(self):
        stderr = io.StringIO()
        with self.recording() as recorder, redirect_stderr(stderr), \
                patch.object(operations, "atomic_json", side_effect=OSError("private disk failure")):
            result = self.prepare(self.generic_builder)
            self.assertTrue(recorder.recording_failed)
            self.assertEqual(recorder.counts["completed_results"], 0)
        rows = self.progress_rows(recorder)
        self.assertEqual(rows[-1]["phase_state"], "complete")
        self.assertEqual(result["source_attempts_created"], 0)
        self.assertEqual(len(self.memory.state["query_tasks"]), R)
        self.assertEqual(stderr.getvalue().count("Research activity could not be saved."), 1)
        self.assertNotIn("private disk failure", stderr.getvalue())

    def test_log_write_failure_does_not_abort_preparation_or_break_current_status(self):
        stderr = io.StringIO()
        with self.recording() as recorder, redirect_stderr(stderr), \
                patch.object(recorder.logger, "info", side_effect=OSError("private log failure")):
            result = self.prepare(self.generic_builder)
            current = self.current()
            self.assertEqual(current["last_event"]["phase_state"], "complete")
            self.assertTrue(current["recording_failed"])
            self.assertEqual(current["counts"]["completed_results"], 0)
        self.assertEqual(result["source_attempts_created"], 0)
        self.assertEqual(stderr.getvalue().count("Research activity could not be saved."), 1)
        self.assertNotIn("private log failure", stderr.getvalue())

    def assert_builder_error_propagates(self, error):
        original = deepcopy(self.memory.state)
        def builder(context):
            row = self.current()["last_event"]
            self.assertEqual((row["phase_state"], row["concept_id"], row["route"]),
                             ("question", "first", "meta-ads"))
            raise error
        with self.recording() as recorder:
            with self.assertRaises(type(error)) as caught:
                self.prepare(builder)
            self.assertIs(caught.exception, error)
            self.assert_no_complete(recorder)
            self.assertEqual(self.current()["counts"]["completed_results"], 0)
        self.assertEqual(self.memory.state, original)
        self.assertEqual(self.memory.commits, [])
        self.assertIsNone(acquisitions._READINESS_OWNERS.get())
        self.assertFalse(any(row["event"] == "phase_finished" for row in self.rows(recorder)))

    def test_integrity_error_propagates_without_false_completion(self):
        self.assert_builder_error_propagates(SafetyError("SYMLINK_PATH_REFUSED"))

    def test_unexpected_error_propagates_without_false_completion(self):
        self.assert_builder_error_propagates(ValueError("private builder failure"))

    def test_owner_interruptions_propagate_without_false_completion(self):
        for error in (KeyboardInterrupt(), SystemExit(130)):
            with self.subTest(error_type=type(error).__name__):
                self.assert_builder_error_propagates(error)

    def test_first_snapshot_failure_has_started_read_record_but_no_false_completion(self):
        error = SafetyError("RESEARCH_EVENTS_INVALID")
        with self.recording() as recorder, patch.object(self.memory, "snapshot", side_effect=error):
            with self.assertRaises(SafetyError) as caught:
                self.prepare()
            self.assertIs(caught.exception, error)
        self.assertEqual([row["phase_state"] for row in self.progress_rows(recorder)], ["read_saved_products"])
        self.assertEqual(self.memory.commits, [])

    def test_pending_save_failure_does_not_claim_complete_or_undo_prepared_tasks(self):
        error = OSError("private pending-save failure")
        real_upsert = coverage._upsert
        def upsert(memory, table, rid, value, old):
            self.assertEqual(self.current()["last_event"]["phase_state"], "save_pending_questions")
            self.assertTrue(memory.state["query_tasks"])
            raise error
        with self.recording() as recorder, patch.object(coverage, "_upsert", side_effect=upsert):
            with self.assertRaises(OSError) as caught:
                self.prepare()
            self.assertIs(caught.exception, error)
            self.assert_no_complete(recorder)
        self.assertEqual(self.progress_rows(recorder)[-1]["phase_state"], "save_pending_questions")
        self.assertTrue(self.memory.state["query_tasks"])
        self.assertTrue(self.memory.commits)
        self.assertIs(coverage._upsert, real_upsert)


if __name__ == "__main__":
    unittest.main()
