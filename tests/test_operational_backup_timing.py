"""Finite backup phase records using synthetic local files, never providers."""

from contextlib import redirect_stderr
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from product_radar import operational_backup_parts as parts
from product_radar.models import SafetyError
from product_radar.operations import operation_run, operation_writer
from product_radar.research_memory import ResearchMemory
from product_radar.storage import Journal, atomic_bytes, load_json


class BackupTimingTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="backup-timing-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name) / "project"
        self.memory = ResearchMemory(self.root / "data/research/captured",
            Journal(self.root / "data/benchmarks/state/apify"), evidence_type="synthetic")
        self.memory.put("concepts", "fictional-concept", {"description": {"label": "Fictional product"}})
        with self.memory.journal.locked():
            self.memory.journal.setup_budget("fictional-account:period", "5")
            self.money_before = self.memory.journal.export_bytes()
        self.research_before = self.memory.snapshot()
        self.code = self.root / "src/product_radar/fictional.py"
        atomic_bytes(self.code, b"# Synthetic offline backup fixture.\n")
        self.destination = self.root / "data/backups"

    def create(self, backup_id="timed-backup", **options):
        return parts.create_multipart_operational_backup(self.memory, self.destination,
            backup_id=backup_id, root=self.root, **options)

    def rows(self, recorder):
        return [json.loads(line) for line in
            (recorder.folder / (recorder.run_id + ".jsonl")).read_text().splitlines()]

    def phase_rows(self, recorder):
        return [row for row in self.rows(recorder)
                if row["event"] in {"phase_started", "phase_finished"}]

    def assert_history_unchanged(self):
        self.assertEqual(self.memory.snapshot(), self.research_before)
        with self.memory.journal.locked():
            self.assertEqual(self.memory.journal.export_bytes(), self.money_before)

    def assert_no_timing_in_contract(self, saved):
        manifest = saved["manifest"]
        self.assertEqual(set(manifest), parts._MANIFEST_FIELDS | {"hash"})
        plan = load_json(Path(saved["folder"]) / "pending.json", 65536)
        self.assertFalse({"timings", "elapsed_seconds", "phase", "phase_state"} & set(plan))

    def test_preparation_copy_and_final_verification_have_separate_monotonic_timings(self):
        with operation_run(self.root) as recorder, operation_writer(), \
                patch.object(parts, "monotonic", side_effect=[10, 12.5, 20, 23.5, 30, 34.5]):
            saved = self.create()
        rows = self.phase_rows(recorder)
        phases = ("backup_preparation", "backup_copy", "backup_final_verification")
        self.assertEqual([(row["event"], row["phase"]) for row in rows],
            [(event, phase) for phase in phases for event in ("phase_started", "phase_finished")])
        self.assertEqual([row["timings"] for row in rows if row["event"] == "phase_finished"],
            [{phase: duration} for phase, duration in zip(phases, (2.5, 3.5, 4.5))])
        self.assertTrue(all(row.get("phase_state") == "completed" for row in rows
                            if row["event"] == "phase_finished"))
        self.assertEqual(recorder.counts["completed_results"], 0)
        self.assert_no_timing_in_contract(saved)
        self.assert_history_unchanged()

    def test_one_bounded_part_records_no_final_verification_until_the_original_set_finishes(self):
        with patch.object(parts, "MAX_PART_FILES", 1):
            with operation_run(self.root) as first:
                pending = self.create()
            self.assertIsNone(pending["manifest"])
            original_plan = (Path(pending["folder"]) / "pending.json").read_bytes()
            self.assertEqual([row["phase"] for row in self.phase_rows(first) if row["event"] == "phase_finished"],
                             ["backup_preparation", "backup_copy"])
            with patch.object(parts, "_prepare", side_effect=AssertionError("Do not select another checkpoint")):
                with operation_run(self.root) as second:
                    resumed = self.create()
                self.assertIsNone(resumed["manifest"])
                with operation_run(self.root) as final:
                    saved = self.create()
        self.assertEqual([row["phase"] for row in self.phase_rows(second) if row["event"] == "phase_finished"],
                         ["backup_preparation", "backup_copy"])
        self.assertEqual([row["phase"] for row in self.phase_rows(final) if row["event"] == "phase_finished"],
                         ["backup_preparation", "backup_copy", "backup_final_verification"])
        self.assertEqual((Path(saved["folder"]) / "pending.json").read_bytes(), original_plan)
        self.assertEqual(saved["manifest"]["parts"], 3)
        self.assert_no_timing_in_contract(saved)
        self.assert_history_unchanged()

    def test_ready_reuse_records_saved_verification_without_selecting_or_copying(self):
        original = self.create()
        manifest_before = (Path(original["folder"]) / "manifest.json").read_bytes()
        with operation_run(self.root) as recorder, \
                patch.object(parts, "_prepare", side_effect=AssertionError("Do not reselect")), \
                patch.object(parts, "_copy_outcome", side_effect=AssertionError("Do not recopy")), \
                patch.object(parts, "monotonic", side_effect=[1, 4]):
            saved = self.create()
        self.assertTrue(saved["replayed"])
        rows = self.phase_rows(recorder)
        self.assertEqual([(row["event"], row["phase"]) for row in rows],
                         [(event, "backup_saved_verification") for event in ("phase_started", "phase_finished")])
        self.assertEqual(rows[-1]["timings"], {"backup_saved_verification": 3})
        self.assertEqual((Path(saved["folder"]) / "manifest.json").read_bytes(), manifest_before)
        self.assertEqual(saved["manifest"], original["manifest"])

    def test_isolated_restore_records_saved_check_and_restored_byte_verification_separately(self):
        saved = self.create()
        destination = self.root.parent / "isolated-restore"
        with operation_run(self.root) as recorder, \
                patch.object(parts, "monotonic", side_effect=[1, 3, 5, 8]):
            restored = parts.restore_multipart_backup(saved["folder"], destination)
        self.assertEqual([row["timings"] for row in self.phase_rows(recorder) if row["event"] == "phase_finished"],
            [{"backup_saved_verification": 2}, {"backup_restore_verification": 3}])
        self.assertEqual(restored["research_events"], 1)
        self.assertEqual(restored["files_verified"], 3)
        self.assertFalse(restored["operational_recovery_proved"])
        self.assertFalse(restored["independent_backup_proved"])
        self.assert_history_unchanged()

    def test_saved_verification_failure_propagates_without_reselection_or_manifest_replacement(self):
        saved = self.create()
        manifest = Path(saved["folder"]) / "manifest.json"
        original_manifest = manifest.read_bytes()
        failure = SafetyError("BACKUP_RESEARCH_CHECKPOINT_MISMATCH")
        with operation_run(self.root) as recorder, \
                patch.object(parts, "_prepare", side_effect=AssertionError("Do not reselect")), \
                patch.object(parts, "_copy_outcome", side_effect=AssertionError("Do not recopy")), \
                patch.object(parts, "_verify", side_effect=failure):
            with self.assertRaises(SafetyError) as caught:
                self.create()
        self.assertIs(caught.exception, failure)
        failed = next(row for row in self.rows(recorder) if row["event"] == "backup_phase_failed")
        self.assertEqual(failed["phase"], "backup_saved_verification")
        self.assertEqual(failed["reason"], "BACKUP_RESEARCH_CHECKPOINT_MISMATCH")
        self.assertEqual(self.phase_rows(recorder)[-1]["phase_state"], "failed")
        self.assertEqual(manifest.read_bytes(), original_manifest)
        self.assert_history_unchanged()

    def test_restore_failure_is_recorded_without_a_success_receipt_or_working_project_changes(self):
        saved = self.create()
        original_verify = parts._verify
        destination = self.root.parent / "failed-isolated-restore"
        failure = OSError("Private isolated output path")
        def verify(folder, manifest, *, restored=None):
            if restored is not None:
                raise failure
            return original_verify(folder, manifest)
        with operation_run(self.root) as recorder, patch.object(parts, "_verify", side_effect=verify):
            with self.assertRaises(OSError) as caught:
                parts.restore_multipart_backup(saved["folder"], destination)
        self.assertIs(caught.exception, failure)
        failed = next(row for row in self.rows(recorder) if row["event"] == "backup_phase_failed")
        self.assertEqual(failed["phase"], "backup_restore_verification")
        self.assertNotIn("reason", failed)
        self.assertNotIn("Private isolated", json.dumps(self.rows(recorder)))
        self.assertFalse((destination / "restore-proof.json").exists())
        self.assert_history_unchanged()

    def test_restored_final_state_mismatch_is_inside_the_verification_failure_boundary(self):
        saved = self.create()
        destination = self.root.parent / "mismatched-isolated-restore"
        original_state = ResearchMemory._state
        def state(memory, **options):
            result = original_state(memory, **options)
            if memory.root == destination / "research":
                return result[0], "f" * 64, result[2], result[3]
            return result
        with operation_run(self.root) as recorder, patch.object(ResearchMemory, "_state", new=state):
            with self.assertRaisesRegex(SafetyError, "RESTORE_CHECKPOINT_MISMATCH"):
                parts.restore_multipart_backup(saved["folder"], destination)
        failure = next(row for row in self.rows(recorder) if row["event"] == "backup_phase_failed")
        self.assertEqual(failure["phase"], "backup_restore_verification")
        self.assertEqual(failure["reason"], "RESTORE_CHECKPOINT_MISMATCH")
        self.assertFalse((destination / "restore-proof.json").exists())
        self.assert_history_unchanged()

    def test_restore_receipt_save_failure_is_inside_the_verification_failure_boundary(self):
        saved = self.create()
        destination = self.root.parent / "unsaved-isolated-restore"
        original_save = parts.atomic_json
        def save(path, value):
            if Path(path).name == "restore-proof.json":
                raise OSError("Private isolated restore receipt path")
            return original_save(path, value)
        with operation_run(self.root) as recorder, patch.object(parts, "atomic_json", side_effect=save):
            with self.assertRaises(OSError):
                parts.restore_multipart_backup(saved["folder"], destination)
        failure = next(row for row in self.rows(recorder) if row["event"] == "backup_phase_failed")
        self.assertEqual(failure["phase"], "backup_restore_verification")
        self.assertNotIn("Private isolated", json.dumps(self.rows(recorder)))
        self.assertFalse((destination / "restore-proof.json").exists())
        self.assert_history_unchanged()

    def test_phase_failures_keep_exact_safe_reason_origin_and_exception_without_private_text(self):
        for method, phase in (("_prepare", "backup_preparation"), ("_copy_outcome", "backup_copy"),
                              ("_verify", "backup_final_verification")):
            for error in (SafetyError("BACKUP_TIMING_TEST_FAILURE"), OSError("Private path and account secret")):
                with self.subTest(method=method, error=type(error).__name__):
                    def fail(*_args, **_kwargs):
                        exec(compile("raise original_error", str(self.root / "src/fixture.py"), "exec"),
                             {"original_error": error})
                    backup_id = method.strip("_") + "-" + type(error).__name__
                    with operation_run(self.root) as recorder, patch.object(parts, method, side_effect=fail):
                        with self.assertRaises(type(error)) as caught:
                            self.create(backup_id)
                    self.assertIs(caught.exception, error)
                    rows = self.rows(recorder)
                    failure = next(row for row in rows if row["event"] == "backup_phase_failed")
                    self.assertEqual(failure["phase"], phase)
                    self.assertEqual(failure["state"], "failed")
                    self.assertEqual(failure["error_type"], type(error).__name__)
                    self.assertEqual(failure["error_location"], {"file": "src/fixture.py", "function": "<module>", "line": 1})
                    if isinstance(error, SafetyError):
                        self.assertEqual(failure["reason"], "BACKUP_TIMING_TEST_FAILURE")
                    else:
                        self.assertNotIn("reason", failure)
                    self.assertEqual(self.phase_rows(recorder)[-1]["phase_state"], "failed")
                    self.assertNotIn("Private path", json.dumps(rows))
                    self.assertNotIn(str(self.root), json.dumps(rows))
                    self.assertFalse((self.destination / backup_id / "manifest.json").exists())
        self.assert_history_unchanged()

    def test_interruptions_propagate_and_original_pending_plan_can_resume(self):
        for error in (KeyboardInterrupt(), SystemExit(17), GeneratorExit()):
            with self.subTest(error=type(error).__name__):
                backup_id = "interrupted-" + type(error).__name__
                with operation_run(self.root) as recorder, \
                        patch.object(parts, "_copy_outcome", side_effect=error):
                    with self.assertRaises(type(error)) as caught:
                        self.create(backup_id)
                self.assertIs(caught.exception, error)
                folder = self.destination / backup_id
                original_plan = (folder / "pending.json").read_bytes()
                self.assertFalse((folder / "manifest.json").exists())
                interrupted = next(row for row in self.rows(recorder) if row["event"] == "backup_phase_interrupted")
                self.assertEqual(interrupted["state"], "interrupted")
                self.assertEqual(interrupted["phase"], "backup_copy")
                self.assertEqual(self.phase_rows(recorder)[-1]["phase_state"], "interrupted")
                with patch.object(parts, "_prepare", side_effect=AssertionError("Keep original frozen plan")):
                    saved = self.create(backup_id)
                self.assertTrue(saved["essential_files_complete"])
                self.assertEqual((folder / "pending.json").read_bytes(), original_plan)
        self.assert_history_unchanged()

    def test_final_manifest_save_failure_is_recorded_and_reuses_the_original_completed_parts(self):
        original_save = parts.atomic_json
        failure = OSError("Private ready-manifest path")
        def save(path, value):
            if Path(path).name == "manifest.json":
                raise failure
            return original_save(path, value)
        with operation_run(self.root) as recorder, patch.object(parts, "atomic_json", side_effect=save):
            with self.assertRaises(OSError) as caught:
                self.create()
        self.assertIs(caught.exception, failure)
        folder = self.destination / "timed-backup"
        self.assertFalse((folder / "manifest.json").exists())
        original_plan = (folder / "pending.json").read_bytes()
        original_cursor = (folder / "cursor.json").read_bytes()
        failed = next(row for row in self.rows(recorder) if row["event"] == "backup_phase_failed")
        self.assertEqual(failed["phase"], "backup_final_verification")
        self.assertEqual(self.phase_rows(recorder)[-1]["phase_state"], "failed")
        self.assertNotIn("Private ready-manifest", json.dumps(self.rows(recorder)))
        with patch.object(parts, "_prepare", side_effect=AssertionError("No new selection")), \
                patch.object(parts, "_copy_outcome", side_effect=AssertionError("No duplicate copying")):
            saved = self.create()
        self.assertTrue(saved["essential_files_complete"])
        self.assertEqual((folder / "pending.json").read_bytes(), original_plan)
        self.assertEqual((folder / "cursor.json").read_bytes(), original_cursor)
        self.assert_history_unchanged()

    def test_no_recording_context_creates_no_log_or_status_or_other_timing_file(self):
        with parts._backup_phase("backup_preparation"):
            pass
        saved = self.create()
        self.assertFalse((self.root / "data/operations/research-runs").exists())
        self.assert_no_timing_in_contract(saved)
        self.assert_history_unchanged()

    def test_records_are_per_phase_not_per_file_and_do_not_include_source_paths(self):
        for number in range(40):
            atomic_bytes(self.code.with_name(f"fixture_{number}.py"), b"# Another synthetic public fixture.\n")
        with operation_run(self.root) as recorder:
            saved = self.create()
        self.assertEqual(saved["files"], 43)
        self.assertEqual(len(self.phase_rows(recorder)), 6)
        self.assertNotIn(str(self.code), json.dumps(self.rows(recorder)))
        self.assertNotIn("original_path", json.dumps(self.rows(recorder)))

    def test_logging_failure_does_not_repeat_copy_stop_work_or_change_history(self):
        with redirect_stderr(io.StringIO()), operation_run(self.root) as recorder, \
                patch.object(recorder.logger, "info", side_effect=OSError("Private log failure")), \
                patch.object(parts, "_copy_outcome", wraps=parts._copy_outcome) as copy:
            saved = self.create()
        self.assertEqual(copy.call_count, 3)
        self.assertTrue(saved["essential_files_complete"])
        self.assertTrue(recorder.recording_failed)
        self.assert_no_timing_in_contract(saved)
        self.assert_history_unchanged()

    def test_invalid_copy_bound_is_rejected_before_phase_work(self):
        with operation_run(self.root) as recorder:
            with self.assertRaisesRegex(SafetyError, "BACKUP_PART_WORK_BOUND_INVALID"):
                self.create(max_parts=True)
        self.assertEqual(self.phase_rows(recorder), [])
        self.assertFalse(self.destination.exists())
        self.assert_history_unchanged()


if __name__ == "__main__":
    unittest.main()
