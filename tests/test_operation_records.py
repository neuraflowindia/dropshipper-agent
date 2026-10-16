"""Small real log/status files, injected failures, no external services."""

from contextlib import redirect_stderr
from datetime import timedelta
import io
import json
from pathlib import Path
import signal
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from product_radar.models import SafetyError, timestamp, utc_now
from product_radar.operations import (operation_run, operation_writer, record_event,
    record_error, record_progress, read_operation_status, preparation_step)
from product_radar.source_deadline import (SourceActionDeadline, bounded_source_deadline,
    trace_source, source_phase, local_source_work)
from product_radar.storage import atomic_json, load_json


class OperationRecordTests(unittest.TestCase):
    def test_preparation_helper_does_not_steal_public_advance_signature(self):
        import inspect
        from product_radar import programme
        self.assertEqual(inspect.signature(programme.advance_programme),
                         inspect.signature(programme._advance_programme_locked))
        self.assertFalse(hasattr(programme._warm_research_history, "__wrapped__"))

    def setUp(self):
        temporary = tempfile.TemporaryDirectory(); self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)

    def rows(self, recorder):
        return [json.loads(line) for line in (recorder.folder / (recorder.run_id + ".jsonl")).read_text().splitlines()]

    def test_read_only_status_does_not_create_any_files(self):
        self.assertEqual(read_operation_status(self.root)["state"], "not_recorded")
        self.assertEqual(list(self.root.iterdir()), [])

    def test_unique_invocations_and_only_admitted_writer_owns_current_status(self):
        with operation_run(self.root) as first:
            with operation_writer():
                record_event("first_step", stage="fit")
            with operation_run(self.root) as loser:
                record_event("waiting_for_owner", state="waiting")
                self.assertNotEqual(first.run_id, loser.run_id)
                self.assertEqual(read_operation_status(self.root)["run_id"], first.run_id)
            with operation_run(self.root) as second:
                with operation_writer():
                    record_event("next_step", stage="photos")
            # A late finalizer on the previous invocation cannot overwrite
            # the writer which now owns the saved status.
            record_event("invocation_finished", exit_code=0)
            self.assertEqual(read_operation_status(self.root)["run_id"], second.run_id)
        self.assertEqual(read_operation_status(self.root)["state"], "saved_previous_run")
        self.assertEqual(self.rows(loser)[-1]["event"], "waiting_for_owner")

    def test_raw_error_private_text_and_arbitrary_fields_are_not_recorded(self):
        with operation_run(self.root) as recorder, operation_writer():
            try:
                raise RuntimeError("password=secret-cookie-and-account-details")
            except RuntimeError as error:
                record_error("source_failed", error, route="amazon-search", prompt="secret-cookie-and-account-details",
                    reason="private unstructured text", environment={"token": "secret-cookie-and-account-details"})
            record_event("safe_result", reason="SOURCE_ACTION_TIMEOUT", task_id="task-1", timings={"transport": 0.4})
        raw = (recorder.folder / (recorder.run_id + ".jsonl")).read_text()
        self.assertNotIn("secret-cookie", raw)
        self.assertNotIn("private unstructured", raw)
        self.assertEqual(self.rows(recorder)[2]["error_type"], "RuntimeError")
        self.assertEqual(self.rows(recorder)[3]["timings"], {"transport": 0.4})

    def test_error_location_is_relative_to_project(self):
        with operation_run(self.root) as recorder:
            with patch.object(recorder.logger, "info") as log:
                try:
                    exec(compile("raise SafetyError('TEST_FAILURE')", str(self.root / "src/fixture.py"), "exec"),
                         {"SafetyError": SafetyError})
                except SafetyError as error:
                    record_error("test_error", error)
                row = log.call_args.args[0]
                self.assertEqual(row["error_location"]["file"], "src/fixture.py")
                self.assertEqual(row["error_location"]["function"], "<module>")
                self.assertEqual(row["reason"], "TEST_FAILURE")

    def test_preparation_and_model_return_are_not_useful_results(self):
        with operation_run(self.root), operation_writer():
            record_progress({"stage": "profiles", "phase_state": "started"})
            record_progress({"stage": "fit", "phase_state": "runner_call_returned"})
            self.assertIsNone(read_operation_status(self.root)["last_useful_result_at"])
            record_progress({"stage": "fit", "phase_state": "finished", "result": {"completed": 2}})
            current = read_operation_status(self.root)
            self.assertEqual(current["state"], "running")
            self.assertEqual(current["counts"]["completed_results"], 1)
            self.assertIsNotNone(current["last_useful_result_at"])

    def test_finite_preparation_details_are_plain_nested_and_not_useful_results(self):
        with operation_run(self.root) as recorder, operation_writer():
            record_event("stage_started", stage="discovery")
            original = read_operation_status(self.root)
            with preparation_step("discovery_questions"):
                self.assertEqual(read_operation_status(self.root)["message"],
                                 "Choosing the next searches and checking their saved results.")
                with preparation_step("discovery_saved_answers", counts={"saved_searches": 2}):
                    current = read_operation_status(self.root)
                    self.assertEqual(current["message"], "Reviewing products from saved search results.")
                    self.assertEqual(current["last_event"]["counts"], {"saved_searches": 2})
                    self.assertEqual(current["phase_started_at"], original["phase_started_at"])
                self.assertEqual(read_operation_status(self.root)["message"],
                                 "Choosing the next searches and checking their saved results.")
            current = read_operation_status(self.root)
            self.assertEqual(current["message"], "Looking for more kinds of products.")
            self.assertEqual(current["counts"]["completed_results"], 0)
            finished = [row for row in self.rows(recorder) if row["event"] == "preparation_finished"]
            self.assertEqual(len(finished), 2)
            self.assertTrue(all(row["elapsed_seconds"] >= 0 for row in finished))

    def test_preparation_failure_preserves_error_and_filters_private_details(self):
        with operation_run(self.root) as recorder, operation_writer():
            record_event("stage_started", stage="discovery")
            with self.assertRaisesRegex(RuntimeError, "private-cookie"):
                with preparation_step("product_map"):
                    raise RuntimeError("private-cookie")
            record_event("ignored_detail", detail={"secret": "private-cookie"})
            with preparation_step("private-cookie"):
                pass
            self.assertEqual(read_operation_status(self.root)["message"], "Looking for more kinds of products.")
            rows = self.rows(recorder)
            self.assertEqual(sum(row["event"] == "preparation_failed" for row in rows), 1)
            self.assertNotIn("private-cookie", json.dumps(rows))

    def test_nested_model_and_website_phases_keep_the_active_preparation_detail(self):
        with operation_run(self.root), operation_writer():
            record_event("stage_started", stage="discovery")
            with preparation_step("discovery_questions"):
                record_event("phase_started", phase="model_call", deadline_seconds=120)
                current = read_operation_status(self.root)
                self.assertEqual(current["message"], "Waiting for the AI to finish its answer.")
                self.assertEqual(current["phase_deadline_seconds"], 120)
                record_event("phase_finished", phase="model_call")
                self.assertEqual(read_operation_status(self.root)["message"],
                                 "Choosing the next searches and checking their saved results.")
                record_event("phase_started", phase="response_save", route="amazon-search")
                self.assertEqual(read_operation_status(self.root)["message"],
                                 "Saving the information collected from a website.")
                record_event("phase_finished", phase="response_save")
                self.assertEqual(read_operation_status(self.root)["message"],
                                 "Choosing the next searches and checking their saved results.")
            current = read_operation_status(self.root)
            self.assertEqual(current["message"], "Looking for more kinds of products.")
            self.assertEqual(current["counts"]["completed_results"], 0)

    def test_actual_source_result_records_useful_completion(self):
        with operation_run(self.root), operation_writer():
            record_progress({"stage": "acquire", "phase_state": "source_result", "source_progress": {
                "state": "acquired", "task_id": "task-1", "route": "google-trends"}})
            current = read_operation_status(self.root)
            self.assertEqual(current["message"], "Checking Google Trends.")
            self.assertEqual(current["counts"]["completed_results"], 1)

    def test_source_preparation_and_saved_recovery_are_not_new_website_work(self):
        messages = {
            "source_preparation": "Preparing website searches from the saved product information.",
            "source_saved_recovery": "Checking saved website replies without starting new searches.",
        }
        with operation_run(self.root) as recorder, operation_writer():
            record_event("stage_started", stage="acquire")
            for phase, message in messages.items():
                with self.subTest(phase=phase):
                    record_event("phase_started", stage="acquire", phase=phase)
                    record_event("source_inspection", stage="acquire", route="amazon-search", task_id="saved-task")
                    current = read_operation_status(self.root)
                    self.assertEqual(current["message"], message)
                    self.assertIsNone(current["phase_deadline_seconds"])
                    self.assertEqual(current["counts"]["completed_results"], 0)
                    self.assertIsNone(current["last_useful_result_at"])
                    record_event("phase_finished", stage="acquire", phase=phase,
                                 timings={phase: 1.25})
            finished = [row for row in self.rows(recorder) if row["event"] == "phase_finished"]
            self.assertEqual([row["timings"] for row in finished],
                             [{"source_preparation": 1.25}, {"source_saved_recovery": 1.25}])

    def test_saved_source_recovery_keeps_real_handler_phase_deadline_visible(self):
        with operation_run(self.root), operation_writer():
            record_event("phase_started", stage="acquire", phase="source_saved_recovery")
            with trace_source({"stage": "acquire", "route": "google-trends", "deadline_seconds": 45}):
                with source_phase("handler"):
                    current = read_operation_status(self.root)
                    self.assertEqual(current["phase"], "handler")
                    self.assertEqual(current["phase_deadline_seconds"], 45)
                with source_phase("response_save"):
                    current = read_operation_status(self.root)
                    self.assertEqual(current["message"], "Saving the information collected from a website.")
                    self.assertIsNone(current["phase_deadline_seconds"])
            self.assertEqual(read_operation_status(self.root)["counts"]["completed_results"], 0)

    def test_research_lookup_save_is_local_work_not_a_new_research_result(self):
        with operation_run(self.root) as recorder, operation_writer():
            record_event("stage_started", stage="match")
            record_event("phase_started", phase="research_lookup_save", useful_result=False)
            current = read_operation_status(self.root)
            self.assertEqual(current["message"],
                             "Updating the research lookup file after saving the results.")
            self.assertIsNone(current["phase_deadline_seconds"])
            self.assertEqual(current["counts"]["completed_results"], 0)
            self.assertIsNone(current["last_useful_result_at"])
            record_event("phase_finished", phase="research_lookup_save", next_phase=None,
                         useful_result=False, timings={"research_lookup_save": 2.5})
            current = read_operation_status(self.root)
            self.assertIsNone(current["phase"])
            self.assertEqual(current["message"], "Checking whether products repeat earlier findings.")
            self.assertEqual(current["counts"]["completed_results"], 0)
            rows = self.rows(recorder)
            self.assertEqual(rows[-1]["timings"], {"research_lookup_save": 2.5})
            self.assertNotIn("deadline_seconds", rows[-1])

    def test_matching_report_and_private_delivery_results_are_useful(self):
        for stage, result in (("match", {"outcomes": [{"state": "reviewed"}]}),
                              ("reports", {"outcomes": [{"complete": True}]}),
                              ("private_staging", {"outcomes": [{"state": "private_bytes_verified"}]}),
                              ("discovery", {"new_concept_ids": ["concept-1"]})):
            with self.subTest(stage=stage), operation_run(self.root), operation_writer():
                record_progress({"stage": stage, "phase_state": "finished", "result": result})
                self.assertEqual(read_operation_status(self.root)["counts"]["completed_results"], 1)

    def test_reused_reports_and_delivery_are_not_new_useful_results(self):
        with operation_run(self.root), operation_writer():
            record_progress({"stage": "reports", "phase_state": "finished",
                             "result": {"outcomes": [{"complete": True, "reused": True}]}})
            record_progress({"stage": "private_staging", "phase_state": "finished",
                             "result": {"outcomes": [{"state": "reused"}]}})
            self.assertIsNone(read_operation_status(self.root)["last_useful_result_at"])

    def test_replayed_discovery_ids_are_not_new_useful_results(self):
        with operation_run(self.root), operation_writer():
            record_progress({"stage": "discovery", "phase_state": "finished", "result": {
                "new_concept_ids": ["concept-previously-queued"], "queued_follow_on_products": 0,
                "external_handler_calls_this_round": 0}})
            current = read_operation_status(self.root)
            self.assertIsNone(current["last_useful_result_at"])
            self.assertEqual(current["counts"]["completed_results"], 0)

    def test_newly_queued_discovery_is_a_useful_result(self):
        with operation_run(self.root), operation_writer():
            record_progress({"stage": "discovery", "phase_state": "finished", "result": {
                "new_concept_ids": ["concept-previously-queued", "concept-new"],
                "queued_follow_on_products": 1}})
            current = read_operation_status(self.root)
            self.assertIsNotNone(current["last_useful_result_at"])
            self.assertEqual(current["counts"]["completed_results"], 1)

    def test_invalid_discovery_admission_count_does_not_credit_replayed_ids(self):
        for queued in (None, True, "1", -1):
            with self.subTest(queued=queued), operation_run(self.root), operation_writer():
                record_progress({"stage": "discovery", "phase_state": "finished", "result": {
                    "new_concept_ids": ["concept-previously-queued"], "queued_follow_on_products": queued}})
                current = read_operation_status(self.root)
                self.assertIsNone(current["last_useful_result_at"])
                self.assertEqual(current["counts"]["completed_results"], 0)

    def test_partial_helper_context_does_not_make_observation_an_execution_gate(self):
        from product_radar.programme import _observe
        with operation_run(self.root), operation_writer():
            _observe({"model_pause": None}, {}, phase_state="runner_call_started", role="G")
            self.assertEqual(read_operation_status(self.root)["phase"], "model_call")

    def test_review_group_position_shows_in_the_activity_until_that_group_finishes(self):
        from product_radar.programme import _observe
        match = "Checking whether products repeat earlier findings."
        with operation_run(self.root) as recorder, operation_writer():
            context = {"stage": "match", "review_scope_id": "group-5"}
            _observe({"programme_id": "whole", "cycles": 0}, context, phase_state="started", group_position=5, group_count=16)
            current = read_operation_status(self.root)
            self.assertEqual(current["message"], match + " Review group 5 of 16.")
            self.assertEqual(current["group_progress"], {"step": "match", "position": 5, "total": 16})
            # An AI call inside the group keeps its position; its return too.
            _observe({"programme_id": "whole"}, context, phase_state="runner_call_started", role="E")
            self.assertEqual(read_operation_status(self.root)["message"],
                             "Waiting for the AI to finish its answer. Review group 5 of 16.")
            _observe({"programme_id": "whole"}, context, phase_state="runner_call_returned", role="E")
            self.assertEqual(read_operation_status(self.root)["message"], match + " Review group 5 of 16.")
            _observe({"programme_id": "whole"}, context, phase_state="finished", result={})
            current = read_operation_status(self.root)
            self.assertEqual((current["message"], current["group_progress"]), (match, None))
            _observe({"programme_id": "whole"}, context, phase_state="started", group_position=6, group_count=16)
            self.assertEqual(read_operation_status(self.root)["message"], match + " Review group 6 of 16.")
            # A new stage without a position, or another step, ends it.
            record_event("stage_started", stage="discovery")
            self.assertIsNone(read_operation_status(self.root)["group_progress"])
            record_event("stage_started", stage="fit", group_position=2, group_count=3)
            record_event("source_outcome", stage="acquire", state="acquired")
            self.assertIsNone(read_operation_status(self.root)["group_progress"])
        rows = self.rows(recorder)
        self.assertEqual([(row["group_position"], row["group_count"]) for row in rows if "group_position" in row],
                         [(5, 16), (6, 16), (2, 3)])

    def test_readiness_check_of_review_groups_shows_its_position_until_readiness_ends(self):
        from product_radar.operations import record_heartbeat
        with operation_run(self.root), operation_writer():
            record_event("phase_started", stage="preparation", phase="readiness", deadline_seconds=1800)
            record_heartbeat("readiness_progress", checkpoint="checking_review_scopes", group_position=3, group_count=16)
            self.assertEqual(read_operation_status(self.root)["message"],
                             "Preparing the next research step. Review group 3 of 16.")
            # A nested local phase finishing is not the end of readiness.
            record_event("phase_started", phase="research_lookup_save")
            record_event("phase_finished", phase="research_lookup_save", next_phase="readiness")
            self.assertEqual(read_operation_status(self.root)["group_progress"]["position"], 3)
            record_event("phase_finished", stage="preparation", phase="readiness")
            current = read_operation_status(self.root)
            self.assertEqual((current["message"], current["group_progress"]), ("Preparing the next research step.", None))

    def test_malformed_group_positions_are_not_recorded(self):
        with operation_run(self.root) as recorder, operation_writer():
            for position, count in ((0, 16), (17, 16), (True, 2), (1.0, 2), ("1", "2"), (1, None), (2, 20000)):
                record_event("stage_started", stage="match", group_position=position, group_count=count)
                self.assertIsNone(read_operation_status(self.root)["group_progress"])
        self.assertFalse(any("group_position" in row or "group_count" in row for row in self.rows(recorder)))

    def test_status_write_failure_is_bounded_and_does_not_stop_work(self):
        notice = io.StringIO()
        with redirect_stderr(notice), operation_run(self.root) as recorder, operation_writer():
            with patch("product_radar.operations.atomic_json", side_effect=OSError("secret-path")):
                record_event("first_fault")
                record_event("second_fault")
            record_event("healthy_work_finished", useful_result=True)
            self.assertTrue(read_operation_status(self.root)["recording_failed"])
        self.assertEqual(notice.getvalue().count("Research activity could not be saved."), 1)
        self.assertNotIn("secret-path", notice.getvalue())
        self.assertIn("healthy_work_finished", [row["event"] for row in self.rows(recorder)])

    def test_logger_failure_does_not_recurse_or_suppress_status(self):
        notice = io.StringIO()
        with redirect_stderr(notice), operation_run(self.root) as recorder, operation_writer():
            with patch.object(recorder.logger, "info", side_effect=OSError("secret")):
                record_event("logger_fault")
                record_event("logger_fault_again")
            record_event("after_fault")
            self.assertTrue(read_operation_status(self.root)["recording_failed"])
        self.assertEqual(notice.getvalue().count("Research activity could not be saved."), 1)

    def test_process_identity_mismatch_is_not_running(self):
        with operation_run(self.root) as recorder, operation_writer():
            record_event("active")
            row = load_json(recorder.status_path)
            row["process_start_ticks"] = "wrong-generation"
            atomic_json(recorder.status_path, row)
            current = read_operation_status(self.root)
            self.assertEqual(current["state"], "saved_previous_run")
            self.assertTrue(current["stale"])

    def test_phase_age_is_reported_without_inventing_website_failure(self):
        with operation_run(self.root), operation_writer():
            record_event("phase_started", phase="local_apply", stage="acquire")
            current = read_operation_status(self.root, as_of=(timestamp(utc_now()) + timedelta(seconds=60)).isoformat())
            self.assertGreaterEqual(current["seconds_in_current_phase"], 60)
            self.assertEqual(current["state"], "running")
            self.assertEqual(current["phase"], "local_apply")

    def test_long_but_in_time_model_call_is_not_called_stalled(self):
        with operation_run(self.root), operation_writer():
            record_progress({"stage": "fit", "phase_state": "runner_call_started"})
            current = read_operation_status(self.root, as_of=(timestamp(utc_now()) + timedelta(seconds=90)).isoformat())
            self.assertEqual(current["phase"], "model_call")
            self.assertIsNone(current["attention"])

    def test_rising_preparation_events_do_not_reset_the_useful_progress_clock(self):
        start = timestamp(utc_now())
        clock = [start.isoformat()]
        with patch("product_radar.operations.utc_now", side_effect=lambda: clock[0]), \
                operation_run(self.root), operation_writer():
            for seconds in (0, 900, 1790):
                clock[0] = (start + timedelta(seconds=seconds)).isoformat()
                record_progress({"stage": "profiles", "phase_state": "started"})
            # A normal 10-12 minute local check is not flagged.
            calm = read_operation_status(self.root, as_of=(start + timedelta(seconds=720)).isoformat())
            self.assertIsNone(calm["attention"])
            current = read_operation_status(self.root, as_of=(start + timedelta(seconds=1810)).isoformat())
            self.assertEqual(current["counts"]["completed_results"], 0)
            self.assertEqual(current["seconds_since_activity"], 20)
            self.assertEqual(current["attention_kind"], "no_result")
            self.assertIn("No completed result has been recorded for at least 30 minutes", current["attention"])

    def test_phase_limit_warning_keeps_confirmed_activity_separate(self):
        with operation_run(self.root), operation_writer():
            record_event("phase_started", phase="transport", deadline_seconds=45)
            current = read_operation_status(self.root, as_of=(timestamp(utc_now()) + timedelta(seconds=60)).isoformat())
            self.assertEqual(current["state"], "running")
            self.assertIsNotNone(current["attention"])

    def test_completed_website_allowance_does_not_time_local_save_or_application(self):
        with operation_run(self.root), operation_writer(), trace_source({"deadline_seconds": 0.02}):
            with source_phase("handler"), bounded_source_deadline(0.02):
                pass
            for phase in ("response_save", "apply"):
                with self.subTest(phase=phase), source_phase(phase):
                    current = read_operation_status(self.root,
                        as_of=(timestamp(utc_now()) + timedelta(seconds=1)).isoformat())
                    self.assertEqual(current["phase"], phase)
                    self.assertIsNone(current["phase_deadline_seconds"])
                    self.assertIsNone(current["attention"])
            current = read_operation_status(self.root)
            self.assertEqual(current["phase"], "preparation")
            self.assertIsNone(current["phase_deadline_seconds"])

    def test_nested_source_phase_restores_its_parent_deadline_not_the_child_deadline(self):
        with operation_run(self.root), operation_writer(), trace_source({"deadline_seconds": 45}):
            with source_phase("handler"):
                with source_phase("response_save"):
                    self.assertIsNone(read_operation_status(self.root)["phase_deadline_seconds"])
                current = read_operation_status(self.root)
                self.assertEqual(current["phase"], "handler")
                self.assertEqual(current["phase_deadline_seconds"], 45)

    def test_hourly_log_rotation_keeps_both_original_segments(self):
        with operation_run(self.root) as recorder:
            record_event("before_rotation")
            recorder.handler.rolloverAt = 0
            record_event("after_rotation")
        segments = list(recorder.folder.glob(recorder.run_id + ".jsonl*"))
        self.assertEqual(len(segments), 2)
        raw = "\n".join(path.read_text() for path in segments)
        self.assertIn("before_rotation", raw)
        self.assertIn("after_rotation", raw)


@unittest.skipUnless(hasattr(signal, "setitimer"), "Requires POSIX timer")
class PhaseTimingTests(unittest.TestCase):
    def test_fast_response_slow_atomic_save_is_not_a_website_timeout(self):
        with tempfile.TemporaryDirectory() as directory:
            original_fsync = __import__("os").fsync
            def slow_sync(descriptor):
                time.sleep(0.035)
                original_fsync(descriptor)
            with trace_source({"task_id": "task-1"}) as trace:
                with bounded_source_deadline(0.025):
                    with source_phase("transport"):
                        time.sleep(0.001)
                    with patch("product_radar.storage.os.fsync", side_effect=slow_sync):
                        with source_phase("response_save"):
                            atomic_json(Path(directory) / "returned.json", {"answer": "saved"})
            self.assertGreater(trace.durations["response_save"], 0.06)
            self.assertLess(trace.durations["transport"], 0.025)
            self.assertEqual(load_json(Path(directory) / "returned.json")["answer"], "saved")

    def test_stalled_transport_is_attributed_to_transport(self):
        with trace_source({}) as trace:
            with self.assertRaises(SourceActionDeadline) as raised:
                with bounded_source_deadline(0.015), source_phase("transport"):
                    time.sleep(0.1)
        self.assertEqual(raised.exception.phase, "transport")
        self.assertEqual(trace.failure_phase, "transport")

    def test_parser_failure_and_local_commit_have_separate_timings(self):
        @local_source_work("local_apply")
        def commit():
            time.sleep(0.03)
        with trace_source({}) as trace:
            with bounded_source_deadline(0.02):
                commit()
            with self.assertRaises(ValueError):
                with source_phase("parse"):
                    raise ValueError("bad data")
        self.assertGreater(trace.durations["local_apply"], 0.025)
        self.assertEqual(trace.failure_phase, "parse")

    def test_diagnostics_do_not_appear_as_source_response_save_time(self):
        with tempfile.TemporaryDirectory() as directory:
            with operation_run(Path(directory)), operation_writer():
                with trace_source({}) as trace, source_phase("transport"):
                    time.sleep(0.001)
            self.assertEqual(set(trace.durations), {"transport"})

    def test_existing_alarm_is_not_suspended_by_local_work(self):
        handler, timer = signal.getsignal(signal.SIGALRM), signal.getitimer(signal.ITIMER_REAL)
        try:
            class EarlierAlarm(Exception):
                pass
            def earlier(_signum, _frame):
                raise EarlierAlarm()
            signal.signal(signal.SIGALRM, earlier)
            signal.setitimer(signal.ITIMER_REAL, 0.03)
            @local_source_work("local_apply")
            def slow():
                time.sleep(0.1)
            with self.assertRaises(EarlierAlarm):
                with bounded_source_deadline(1):
                    slow()
        finally:
            signal.setitimer(signal.ITIMER_REAL, 0)
            signal.signal(signal.SIGALRM, handler)
            if timer[0]:
                signal.setitimer(signal.ITIMER_REAL, *timer)


if __name__ == "__main__":
    unittest.main()
