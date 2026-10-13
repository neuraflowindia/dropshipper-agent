"""Round-local lookup batching never defers original research, offline only."""

from copy import deepcopy
import json
from pathlib import Path
import signal
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

from product_radar import programme, research_segments as segments
from product_radar.models import SafetyError, digest
from product_radar.operational_backup import (_checkpoint, create_operational_backup,
    verify_operational_backup)
from product_radar.operations import operation_run, operation_writer, record_event
from product_radar.research_memory import ResearchMemory
from product_radar.storage import Journal, atomic_json, load_json
from product_radar.work_turns import DEFAULT_POLICY, reserve_source_callback


NOW = "2026-10-11T12:00:00+00:00"


class ResearchProjectionBatchTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="research-lookup-batch-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.memory = self.fresh()
        self.memory.put("concepts", "old", {"label": "Synthetic old product"})
        self.memory.migrate_segments()

    def fresh(self):
        return ResearchMemory(self.root / "research", Journal(self.root / "finance"), evidence_type="synthetic")

    def pointer(self):
        return load_json(self.memory.root / segments.POINTER)

    def rows(self, recorder):
        return [json.loads(line) for line in (recorder.folder / (recorder.run_id + ".jsonl")).read_text().splitlines()]

    def test_commits_are_immediate_and_immutable_while_only_one_projection_is_published(self):
        old = self.pointer()
        original_event = (self.memory.root / "events/00000001.json").read_bytes()
        with patch.object(self.memory, "_write_index", wraps=self.memory._write_index) as write:
            with self.memory.defer_lookup_projection():
                self.memory.put("concepts", "new", {"label": "Synthetic new product"})
                self.memory.put("observations", "result", {"value": 3})
                self.assertEqual(write.call_count, 0)
                self.assertEqual(self.pointer(), old)
                self.assertEqual(self.fresh().snapshot()["observations"]["result"], {"value": 3})
                self.assertEqual(len(list((self.memory.root / "events").glob("*.json"))), 3)
            self.assertEqual(write.call_count, 1)
        self.assertEqual(self.pointer()["sequence"], 3)
        self.assertEqual(self.pointer()["generation"], old["generation"] + 1)
        self.assertEqual((self.memory.root / "events/00000001.json").read_bytes(), original_event)

    def test_standalone_and_independent_instances_stay_eager(self):
        other = self.fresh()
        with patch.object(self.memory, "_write_index", wraps=self.memory._write_index) as own, \
                patch.object(other, "_write_index", wraps=other._write_index) as independent:
            with self.memory.defer_lookup_projection():
                self.memory.put("concepts", "deferred", {"value": 1})
                other.put("concepts", "independent", {"value": 2})
                self.assertEqual(own.call_count, 0)
                self.assertEqual(independent.call_count, 1)
                self.assertEqual(self.pointer()["sequence"], 3)
            self.assertEqual(own.call_count, 1)
            self.memory.put("concepts", "eager", {"value": 3})
            self.assertEqual(own.call_count, 2)

    def test_outer_flush_rereads_independent_append_not_only_its_last_local_state(self):
        other = self.fresh()
        with self.memory.defer_lookup_projection():
            self.memory.put("concepts", "local", {"value": 1})
            other.put("concepts", "later", {"value": 2})
            self.assertNotIn("later", self.memory._verified["state"]["concepts"])
        self.assertEqual(self.memory.snapshot()["concepts"]["later"], {"value": 2})
        self.assertEqual(self.pointer()["sequence"], 3)
        self.assertEqual(segments._read(self.memory, self.pointer()["checkpoint"])["head"], self.memory._verified["head"])

    def test_independent_append_during_projection_build_refuses_old_publication(self):
        original = segments._verify_checkpoint_values
        other = self.fresh()
        appended = []
        def append_after_candidate_check(memory, *args, **kwargs):
            result = original(memory, *args, **kwargs)
            if memory is self.memory and not appended:
                appended.append(True)
                other.put("concepts", "independent-during-flush", {"value": 2})
            return result
        with patch.object(segments, "_verify_checkpoint_values", side_effect=append_after_candidate_check):
            with self.memory.defer_lookup_projection():
                self.memory.put("concepts", "local-before-flush", {"value": 1})
        self.assertEqual(self.pointer()["sequence"], 3)
        self.assertEqual(self.memory.last_commit_diagnostic, "RESEARCH_CHECKPOINT_CHANGED")
        self.assertTrue(self.memory._projection_dirty)
        self.assertIn("independent-during-flush", self.memory.snapshot()["concepts"])
        self.assertIn("local-before-flush", self.fresh().snapshot()["concepts"])
        self.memory.put("concepts", "next-eager", {"value": 3})
        self.assertEqual(self.pointer()["sequence"], 4)
        self.assertFalse(self.memory._projection_dirty)

    def test_overlapping_instance_scopes_keep_events_immediate_and_flush_only_at_last_exit(self):
        admitted, release, failures = threading.Event(), threading.Event(), []
        def worker():
            try:
                with self.memory.defer_lookup_projection():
                    self.memory.put("concepts", "thread-product", {"value": 2})
                    admitted.set()
                    if not release.wait(5):
                        raise AssertionError("Synthetic worker release timed out")
            except BaseException as error:
                failures.append(error)
                admitted.set()
        thread = threading.Thread(target=worker)
        with patch.object(self.memory, "_write_index", wraps=self.memory._write_index) as write:
            try:
                with self.memory.defer_lookup_projection():
                    self.memory.put("concepts", "main-product", {"value": 1})
                    thread.start()
                    self.assertTrue(admitted.wait(5))
                    self.assertEqual(failures, [])
                    self.assertEqual(write.call_count, 0)
                    self.assertIn("thread-product", self.fresh().snapshot()["concepts"])
                self.assertEqual(write.call_count, 0)
            finally:
                release.set()
                if thread.ident is not None:
                    thread.join(5)
            self.assertFalse(thread.is_alive())
            self.assertEqual(failures, [])
            self.assertEqual(write.call_count, 1)
        self.assertEqual(self.pointer()["sequence"], 3)
        self.assertEqual(self.memory._projection_defer_depth, 0)

    def test_nested_success_flushes_once_and_caught_inner_error_does_not_flush_early(self):
        with patch.object(self.memory, "_write_index", wraps=self.memory._write_index) as write:
            with self.memory.defer_lookup_projection():
                with self.memory.defer_lookup_projection():
                    self.memory.put("concepts", "nested", {"value": 1})
                try:
                    with self.memory.defer_lookup_projection():
                        self.memory.put("concepts", "before-error", {"value": 2})
                        raise ValueError("Synthetic caught item failure")
                except ValueError:
                    pass
                self.assertEqual(write.call_count, 0)
            self.assertEqual(write.call_count, 1)
        self.assertEqual(self.memory._projection_defer_depth, 0)
        self.assertEqual(self.pointer()["sequence"], 3)

    def test_exception_and_cancellation_preserve_original_error_and_immediate_events(self):
        for number, error in enumerate((RuntimeError("Synthetic failure"), KeyboardInterrupt(), SystemExit(130))):
            with self.subTest(error=type(error).__name__), \
                    patch.object(self.memory, "_write_index", side_effect=AssertionError("No optional flush on failure")):
                with self.assertRaises(type(error)) as raised:
                    with self.memory.defer_lookup_projection():
                        self.memory.put("concepts", "saved-" + str(number), {"value": number})
                        raise error
                self.assertIs(raised.exception, error)
            self.assertEqual(self.memory._projection_defer_depth, 0)
            self.assertEqual(self.fresh().snapshot()["concepts"]["saved-" + str(number)], {"value": number})
        self.assertEqual(self.pointer()["sequence"], 1)

    def test_owner_stop_skips_optional_flush_and_later_eager_commit_recovers_current_projection(self):
        with patch.object(self.memory, "_write_index", wraps=self.memory._write_index) as write:
            with self.memory.defer_lookup_projection(flush_if=lambda: False):
                self.memory.put("concepts", "stopped", {"value": 1})
            self.assertEqual(write.call_count, 0)
            self.assertEqual(self.pointer()["sequence"], 1)
            self.assertEqual(self.fresh().snapshot()["concepts"]["stopped"], {"value": 1})
            self.memory.put("concepts", "after-stop", {"value": 2})
            self.assertEqual(write.call_count, 1)
        self.assertEqual(self.pointer()["sequence"], 3)

    def test_empty_scope_never_reads_or_writes_projection(self):
        with patch.object(self.memory, "_state", side_effect=AssertionError("No empty read")), \
                patch.object(self.memory, "_write_index", side_effect=AssertionError("No empty projection")):
            with self.memory.defer_lookup_projection():
                pass

    def test_unsegmented_legacy_index_remains_optional_and_is_coalesced_too(self):
        memory = ResearchMemory(self.root / "legacy", Journal(self.root / "legacy-finance"), evidence_type="synthetic")
        memory.put("concepts", "first", {"value": 1})
        old_index = (memory.root / "index.json").read_bytes()
        with patch.object(memory, "_write_index", wraps=memory._write_index) as write:
            with memory.defer_lookup_projection():
                memory.put("concepts", "second", {"value": 2})
                memory.put("concepts", "third", {"value": 3})
                self.assertEqual((memory.root / "index.json").read_bytes(), old_index)
                self.assertEqual(write.call_count, 0)
            self.assertEqual(write.call_count, 1)
        self.assertEqual(load_json(memory.root / "index.json")["state"]["concepts"]["third"], {"value": 3})
        self.assertFalse((memory.root / segments.POINTER).exists())

    def test_invalid_flush_guard_is_rejected_before_entering_scope(self):
        with self.assertRaisesRegex(SafetyError, "RESEARCH_PROJECTION_GUARD_INVALID"):
            with self.memory.defer_lookup_projection(flush_if=True):
                self.fail("Invalid guard entered")
        self.assertEqual(self.memory._projection_defer_depth, 0)

    def test_all_bounded_readers_fall_back_to_current_originals_during_scope(self):
        old = self.pointer()
        with self.memory.defer_lookup_projection():
            self.memory.put("concepts", "new", {"label": "Synthetic new product"})
            self.memory.put("published_history", "exposure", {"status": "published", "concept_id": "new",
                "issue_id": "synthetic-issue", "client_id": "default"})
            self.assertEqual(self.pointer(), old)
            records = self.memory.read_records("concepts", ["new"])
            self.assertFalse(records["checkpoint_used"])
            self.assertEqual(records["records"]["new"], {"label": "Synthetic new product"})
            self.assertEqual(dict(self.memory.read_table_page("concepts")["records"])["new"], records["records"]["new"])
            event_id = next(reversed(self.memory._verified["ids"]))
            self.assertEqual(self.memory.event_id_record(event_id)["record"]["sequence"], 3)
            self.assertEqual(self.memory.novelty_page("new")["records"][0][0], "exposure")

    def test_backup_selects_exact_new_canonical_state_without_projection_flush(self):
        with patch.object(self.memory, "_write_index", wraps=self.memory._write_index) as write:
            with self.memory.defer_lookup_projection():
                self.memory.put("concepts", "backup-new", {"label": "Synthetic backup product"})
                research, _, _, state, _ = _checkpoint(self.memory, include_state=True)
                self.assertEqual(research["sequence"], 2)
                self.assertEqual(research["state_hash"], digest(state))
                self.assertIn("backup-new", state["concepts"])
                saved = create_operational_backup(self.memory, self.root / "backup", backup_id="batched-copy")
                manifest = verify_operational_backup(saved["folder"])
                self.assertEqual(manifest["research_checkpoint"]["sequence"], 2)
                self.assertEqual(manifest["research_checkpoint"]["state_hash"], research["state_hash"])
                self.assertEqual(write.call_count, 0)
            self.assertEqual(write.call_count, 1)

    def test_dependency_validation_and_event_id_replay_remain_exact(self):
        with self.memory.defer_lookup_projection():
            self.memory.put("concepts", "saved", {"value": 1}, event_id="exact-saved-event")
            self.assertFalse(self.memory.put("concepts", "saved", {"value": 1}, event_id="exact-saved-event"))
            with self.assertRaisesRegex(SafetyError, "RESEARCH_EVENT_CONFLICT"):
                self.memory.put("concepts", "saved", {"value": 2}, event_id="exact-saved-event")
            with self.assertRaisesRegex(SafetyError, "RESEARCH_DEPENDENCY_CHANGED"):
                self.memory.put("concepts", "old", {"label": "Wrong dependency"}, previous={"wrong": "original"})
        self.assertEqual(self.pointer()["sequence"], 2)
        self.assertEqual(self.memory.snapshot()["concepts"]["saved"], {"value": 1})

    def test_changed_original_is_not_hidden_by_deferred_projection(self):
        event_path = self.memory.root / "events/00000001.json"
        with patch.object(self.memory, "_write_index", side_effect=AssertionError("No flush after invalid input")):
            with self.assertRaisesRegex(SafetyError, "RESEARCH_EVENTS_INVALID"):
                with self.memory.defer_lookup_projection():
                    self.memory.put("concepts", "new", {"value": 1})
                    event = load_json(event_path)
                    event["changes"][0]["value"] = {"label": "Corrupted original"}
                    atomic_json(event_path, event)
                    self.memory.snapshot()
        with self.assertRaisesRegex(SafetyError, "RESEARCH_EVENTS_INVALID"):
            self.fresh().snapshot()

    def test_projection_failure_is_diagnostic_and_never_repeats_canonical_commit(self):
        original = segments.atomic_json
        def failed_pointer(path, value):
            if Path(path).name == segments.POINTER:
                raise OSError("Synthetic lookup write failure")
            return original(path, value)
        with patch.object(segments, "atomic_json", side_effect=failed_pointer):
            with self.memory.defer_lookup_projection():
                self.memory.put("concepts", "saved", {"value": 1})
        self.assertEqual(self.memory.last_commit_diagnostic, "RESEARCH_CHECKPOINT_WRITE_UNAVAILABLE")
        self.assertEqual(self.fresh().snapshot()["concepts"]["saved"], {"value": 1})
        self.assertFalse(self.memory.put("concepts", "saved", {"value": 1}))
        self.assertEqual(len(list((self.memory.root / "events").glob("*.json"))), 2)

    def test_lost_projection_reply_never_repeats_originals(self):
        original = segments.atomic_json
        def lost_reply(path, value):
            original(path, value)
            if Path(path).name == segments.POINTER:
                raise OSError("Synthetic lost lookup reply")
        with patch.object(segments, "atomic_json", side_effect=lost_reply):
            with self.memory.defer_lookup_projection():
                self.memory.put("concepts", "saved", {"value": 1})
        self.assertEqual(self.pointer()["sequence"], 2)
        self.assertFalse(self.fresh().put("concepts", "saved", {"value": 1}))
        self.assertEqual(len(list((self.memory.root / "events").glob("*.json"))), 2)

    def test_failed_projection_has_a_durable_safe_reason_in_normal_and_nested_phases(self):
        original = segments.atomic_json
        def failed_pointer(path, value):
            if Path(path).name == segments.POINTER:
                raise OSError("password=never-copy-this-private-text")
            return original(path, value)
        for nested in (False, True):
            with self.subTest(nested=nested), operation_run(self.root) as recorder, operation_writer():
                if nested:
                    record_event("phase_started", stage="acquire", phase="handler", deadline_seconds=45)
                before = recorder.phase, recorder.phase_started, recorder.phase_deadline
                with patch.object(segments, "atomic_json", side_effect=failed_pointer):
                    with self.memory.defer_lookup_projection():
                        self.memory.put("concepts", "saved-" + str(nested), {"value": nested})
                self.assertEqual((recorder.phase, recorder.phase_started, recorder.phase_deadline), before)
                self.assertEqual(recorder.counts["completed_results"], 0)
                rows = self.rows(recorder)
                outcomes = [row for row in rows if row.get("reason") == "RESEARCH_CHECKPOINT_WRITE_UNAVAILABLE"]
                self.assertEqual(len(outcomes), 1)
                self.assertEqual(outcomes[0]["event"], "research_lookup_save_outcome" if nested else "phase_finished")
                self.assertEqual(outcomes[0]["state"], "waiting")
                self.assertFalse(outcomes[0]["useful_result"])
                self.assertIn("research_lookup_save", outcomes[0]["timings"])
                self.assertNotIn("never-copy-this-private-text", json.dumps(rows))
                self.assertEqual(load_json(recorder.status_path)["last_reason"], "RESEARCH_CHECKPOINT_WRITE_UNAVAILABLE")

    def test_exit_read_failure_is_durably_recorded_without_masking_saved_work(self):
        original = self.memory._state
        def unavailable(*args, **kwargs):
            if kwargs.get("copy_state") is False:
                raise SafetyError("SAVED_FILE_UNAVAILABLE")
            return original(*args, **kwargs)
        with operation_run(self.root) as recorder, operation_writer(), patch.object(self.memory, "_state", side_effect=unavailable):
            with self.memory.defer_lookup_projection():
                self.memory.put("concepts", "saved", {"value": 1})
        outcome = next(row for row in self.rows(recorder) if row.get("reason") == "SAVED_FILE_UNAVAILABLE")
        self.assertEqual((outcome["event"], outcome["state"]), ("phase_finished", "waiting"))
        self.assertEqual(self.fresh().snapshot()["concepts"]["saved"], {"value": 1})

    def test_flush_guard_error_is_diagnostic_and_durable_without_starting_lookup_work(self):
        def unavailable():
            raise OSError("password=never-log-owner-control-details")
        with operation_run(self.root) as recorder, operation_writer(), \
                patch.object(self.memory, "_write_index", side_effect=AssertionError("No flush after guard failure")):
            with self.memory.defer_lookup_projection(flush_if=unavailable):
                self.memory.put("concepts", "saved", {"value": 1})
        rows = self.rows(recorder)
        outcome = next(row for row in rows if row["event"] == "research_lookup_save_outcome")
        self.assertEqual(outcome["reason"], "RESEARCH_INDEX_WRITE_UNAVAILABLE")
        self.assertNotIn("never-log-owner-control-details", json.dumps(rows))
        self.assertEqual(self.fresh().snapshot()["concepts"]["saved"], {"value": 1})

    def test_transient_projection_failure_recovers_once_and_clears_pending_flag(self):
        original = segments.atomic_json
        def failed_pointer(path, value):
            if Path(path).name == segments.POINTER:
                raise OSError("Synthetic first projection failure")
            return original(path, value)
        with patch.object(segments, "atomic_json", side_effect=failed_pointer):
            with self.memory.defer_lookup_projection():
                self.memory.put("concepts", "saved", {"value": 1})
        self.assertTrue(self.memory._projection_dirty)
        with patch.object(self.memory, "_write_index", wraps=self.memory._write_index) as write:
            with self.memory.defer_lookup_projection():
                pass
            self.assertEqual(write.call_count, 1)
            self.assertFalse(self.memory._projection_dirty)
            self.assertIsNone(self.memory.last_commit_diagnostic)
            with self.memory.defer_lookup_projection():
                pass
            self.assertEqual(write.call_count, 1)
        self.assertEqual(self.pointer()["sequence"], 2)

    def test_optional_flush_read_error_does_not_reclassify_already_committed_work(self):
        original = self.memory._state
        def unavailable(*args, **kwargs):
            if kwargs.get("copy_state") is False:
                raise SafetyError("SAVED_FILE_UNAVAILABLE")
            return original(*args, **kwargs)
        with patch.object(self.memory, "_state", side_effect=unavailable):
            with self.memory.defer_lookup_projection():
                self.memory.put("concepts", "saved", {"value": 1})
        self.assertEqual(self.memory.last_commit_diagnostic, "SAVED_FILE_UNAVAILABLE")
        self.assertEqual(self.fresh().snapshot()["concepts"]["saved"], {"value": 1})

    def test_exit_rechecks_original_integrity_before_optional_publication(self):
        old = self.pointer()
        with self.memory.defer_lookup_projection():
            self.memory.put("concepts", "saved", {"value": 1})
            path = self.memory.root / "events/00000001.json"
            changed = load_json(path)
            changed["changes"][0]["value"] = {"value": "Corrupt original fixture"}
            atomic_json(path, changed)
        self.assertEqual(self.pointer(), old)
        self.assertEqual(self.memory.last_commit_diagnostic, "RESEARCH_EVENTS_INVALID")
        with self.assertRaisesRegex(SafetyError, "RESEARCH_EVENTS_INVALID"):
            self.memory.snapshot()

    @unittest.skipUnless(hasattr(signal, "setitimer"), "Requires POSIX source deadlines")
    def test_optional_flush_does_not_consume_an_enclosing_website_allowance(self):
        from product_radar.source_deadline import bounded_source_deadline, trace_source
        original = self.memory._write_index
        def slow(*args, **kwargs):
            time.sleep(0.06)
            return original(*args, **kwargs)
        with patch.object(self.memory, "_write_index", side_effect=slow), trace_source({}) as trace:
            trace.phase = "handler"
            with bounded_source_deadline(0.04):
                with self.memory.defer_lookup_projection():
                    self.memory.put("concepts", "saved", {"value": 1})
                self.assertGreater(signal.getitimer(signal.ITIMER_REAL)[0], 0.02)
            self.assertEqual(trace.phase, "handler")
            self.assertGreaterEqual(trace.durations["local_apply"], 0.06)

    def test_optional_flush_logs_phase_duration_without_result_or_website_deadline(self):
        with operation_run(self.root) as recorder, operation_writer():
            with self.memory.defer_lookup_projection():
                self.memory.put("concepts", "new", {"value": 1})
            self.assertIsNone(recorder.phase)
            self.assertIsNone(recorder.phase_deadline)
            self.assertEqual(recorder.counts["completed_results"], 0)
        phases = [row for row in self.rows(recorder) if row.get("phase") == "research_lookup_save"]
        self.assertEqual([row["event"] for row in phases], ["phase_started", "phase_finished"])
        self.assertGreaterEqual(phases[1]["timings"]["research_lookup_save"], 0)
        self.assertGreaterEqual(phases[1]["elapsed_seconds"], phases[1]["timings"]["research_lookup_save"])
        self.assertTrue(all(row.get("useful_result") is False and row.get("deadline_seconds") is None for row in phases))

    def test_nested_live_phase_is_not_relabelled_or_given_a_fresh_clock(self):
        with operation_run(self.root) as recorder, operation_writer():
            record_event("phase_started", stage="acquire", phase="handler", deadline_seconds=45)
            before = recorder.phase, recorder.phase_started, recorder.phase_deadline
            with self.memory.defer_lookup_projection():
                self.memory.put("concepts", "new", {"value": 1})
            self.assertEqual((recorder.phase, recorder.phase_started, recorder.phase_deadline), before)
            status = load_json(recorder.status_path)
            self.assertEqual((status["phase"], status["phase_started_at"], status["phase_deadline_seconds"]), before)
        self.assertFalse(any(row.get("phase") == "research_lookup_save" for row in self.rows(recorder)))


class ProgrammeProjectionBatchTests(unittest.TestCase):
    def setUp(self):
        import test_programme as fixtures
        self.fixture = fixtures.ProgrammeTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.fixture.prepare()
        self.memory = self.fixture.memory
        self.memory.migrate_segments()

    def test_all_three_admitted_entrypoints_publish_one_current_lookup_copy(self):
        entries = (
            lambda: programme.advance_programme(self.memory, "whole-programme", max_cycles=1, **self.options()),
            lambda: programme.advance_programme_turn(self.memory, "whole-programme", **self.options()),
            lambda: programme.advance_ready_programme_turn(self.memory, programme_id="whole-programme", **self.options()),
        )
        for entry in entries:
            with self.subTest(entry=entry), patch.object(self.memory, "_write_index", wraps=self.memory._write_index) as write:
                entry()
                self.assertEqual(write.call_count, 1)
                self.assertEqual(load_json(self.memory.root / segments.POINTER)["sequence"], self.memory._verified["sequence"])

    def options(self):
        return {"root": self.fixture.root, "handlers": {}, "steps": self.fixture.noops(), "as_of": self.fixture.now}

    def test_actual_original_allowances_and_normal_turn_closure_are_unchanged(self):
        from product_radar.research_ai import Jobs
        from test_research_flow import FakeRunner
        runner = FakeRunner()
        def actual_work(memory, record, context):
            reserve_source_callback(memory, "synthetic-source", at=context["at"])
            job_id, result = Jobs(memory, runner).run("A", {"items": [{"id": "synthetic-item",
                "source_text": {"title": "Synthetic blue sink caddy"}}]})
            return {"job_id": job_id, "accepted": result["accepted"]}
        options = self.options()
        options["steps"]["profiles"] = actual_work
        policy = deepcopy(DEFAULT_POLICY)
        policy.update(source_callbacks=1, exploration_slots=0, model_executions=1, report_slots=0)
        with patch.object(self.memory, "_write_index", wraps=self.memory._write_index) as write:
            result = programme.advance_ready_programme_turn(self.memory, programme_id="whole-programme",
                turn_policy=policy, **options)
            self.assertEqual(write.call_count, 1)
        turn = self.memory.snapshot()["batch_runs"][result["work_turn"]["id"]]
        self.assertEqual(turn["state"], "closed")
        self.assertEqual(turn["counters"]["source_callbacks"], 1)
        self.assertEqual(turn["counters"]["model_executions"], 1)
        self.assertEqual(len(runner.calls), 1)
        self.assertTrue(result["round_completed"])
        self.assertEqual(turn["policy"], policy)

    def test_interrupted_round_keeps_original_open_turn_and_skips_lookup_flush(self):
        original_error = KeyboardInterrupt()
        with patch("product_radar.programme._advance_programme_locked", side_effect=original_error), \
                patch.object(self.memory, "_write_index", side_effect=AssertionError("No cancellation flush")):
            with self.assertRaises(KeyboardInterrupt) as raised:
                programme.advance_programme_turn(self.memory, "whole-programme", **self.options())
        self.assertIs(raised.exception, original_error)
        turn = next(row for row in self.memory.snapshot()["batch_runs"].values() if row.get("kind") == "work-turn-v1")
        self.assertEqual(turn["state"], "open")
        result = programme.advance_programme_turn(self.memory, "whole-programme", **self.options())
        self.assertEqual(result["work_turn"]["id"], turn["id"])
        self.assertEqual(result["work_turn"]["state"], "closed")

    def test_real_owner_stop_skips_optional_lookup_work_without_resetting_the_turn(self):
        def stop(memory, record, context):
            programme.set_programme_service_state(memory, enabled=False, as_of=context["at"])
            return {"state": "nothing_new"}
        options = self.options()
        options["steps"]["profiles"] = stop
        with patch.object(self.memory, "_write_index", side_effect=AssertionError("No optional Stop flush")):
            result = programme.advance_programme_turn(self.memory, "whole-programme", **options)
        self.assertEqual(result["tick_state"], "owner_paused")
        self.assertEqual(result["work_turn"]["state"], "closed")
        self.assertFalse(programme.programme_service_state(self.memory)["enabled"])


if __name__ == "__main__":
    unittest.main()
