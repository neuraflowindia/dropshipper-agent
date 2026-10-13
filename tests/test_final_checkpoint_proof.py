"""The final checkpoint evidence helper is offline, bounded and isolated."""

from contextlib import redirect_stdout, redirect_stderr
import importlib.util
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from product_radar import operational_backup_parts as parts
from product_radar.models import SafetyError
from product_radar.research_memory import ResearchMemory
from product_radar.storage import Journal, atomic_bytes, atomic_json, load_json


HELPER_PATH = Path(__file__).resolve().parents[1] / (
    "data/operations/implementation-20261011/final_checkpoint_proof.py")
SPEC = importlib.util.spec_from_file_location("final_checkpoint_evidence_helper", HELPER_PATH)
helper = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(helper)


class FinalCheckpointHelperTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="final-checkpoint-helper-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name) / "project"
        self.memory = ResearchMemory(self.root / "data/research/captured",
            Journal(self.root / "data/benchmarks/state/apify"), evidence_type="synthetic")
        self.memory.put("concepts", "fictional-concept", {"description": {"label": "Fictional product"}})
        with self.memory.journal.locked():
            self.memory.journal.setup_budget("fictional-account:period", "5")
            self.money = self.memory.journal.export_bytes()
        self.state = self.memory.snapshot()
        self.code = self.root / "src/product_radar/fictional.py"
        atomic_bytes(self.code, b"# Synthetic offline public code fixture.\n")
        self.proof_root = self.root / helper.PROOF_RELATIVE
        self.old = {
            self.root / "data/backups/daily-v3-2026-10-11/manifest.json": b"Old daily evidence, never replaced.\n",
            self.proof_root / "backup-current-result.json": b"Old backup evidence, never replaced.\n",
            self.proof_root / "backup-restore-proof.json": b"Old restore evidence, never replaced.\n",
            self.proof_root / "cost-current-proof.json": b"Old cost evidence, never replaced.\n"}
        for path, raw in self.old.items():
            atomic_bytes(path, raw)
        self.constructor = patch.object(helper, "ResearchMemory", return_value=self.memory)
        self.constructor.start()
        self.addCleanup(self.constructor.stop)
        network = patch("urllib.request.urlopen", side_effect=AssertionError("No provider calls in this helper"))
        network.start()
        self.addCleanup(network.stop)

    def execute(self, action="create"):
        return helper.execute(action, root=self.root)

    def assert_unchanged(self):
        self.assertEqual(self.memory.snapshot(), self.state)
        with self.memory.journal.locked():
            self.assertEqual(self.memory.journal.export_bytes(), self.money)
        for path, raw in self.old.items():
            self.assertEqual(path.read_bytes(), raw)

    def assert_saved_receipt(self, result):
        self.assertTrue(result["receipt_saved"])
        receipt = self.root / result["receipt_file"]
        self.assertEqual(load_json(receipt, 65536), result)
        self.assertNotIn("attachment_mapping", json.dumps(result))
        self.assertNotIn("original_path", json.dumps(result))
        self.assertEqual(result["provider_calls"], 0)
        self.assertEqual(result["model_calls"], 0)
        self.assertFalse(result["independent_backup_proved"])
        self.assertFalse(result["operational_recovery_proved"])
        self.assertFalse(result["automatic_daily_copy_proved"])
        self.assertFalse(result["working_project_replaced"])

    def test_create_has_an_immutable_separate_id_one_part_and_the_existing_writer(self):
        calls = []
        original = helper.create_multipart_operational_backup
        def create(memory, destination, **options):
            with self.assertRaisesRegex(SafetyError, "^RUNNER_ALREADY_ACTIVE$"):
                with Journal(memory.root / "programme_writer").locked():
                    self.fail("The checkpoint must own the existing writer")
            calls.append((destination, options))
            return original(memory, destination, **options)
        with patch.object(helper, "create_multipart_operational_backup", side_effect=create), \
                patch.object(helper, "monotonic", side_effect=[10, 12.75]):
            result = self.execute()
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0][0], self.root / "data/backups")
        self.assertEqual(calls[0][1], {"backup_id": "implementation-v3-2026-10-11-final", "root": self.root,
            "pending_controls": (self.root / helper.CONTROL_RELATIVE,), "max_parts": 1})
        self.assertEqual(result["elapsed_seconds"], 2.75)
        self.assertEqual(result["selected_files"], 3)
        self.assertEqual(result["stored_files"], 3)
        self.assertGreater(result["stored_bytes"], 0)
        # The synthetic fixture deliberately has no real programme control;
        # its missing reference is honestly retained, not called complete.
        self.assertEqual(result["state"], "records_verified_with_limitations")
        self.assertFalse(result["essential_files_complete"])
        self.assertEqual(set(result["phase_timings_seconds"]), {
            "backup_preparation", "backup_copy", "backup_final_verification"})
        self.assertIsNotNone(result["recorded_writer_scope_seconds"])
        self.assertTrue(result["operation_recording_complete"])
        self.assert_saved_receipt(result)
        self.assert_unchanged()

    def test_pending_copy_resumes_exact_original_selection_without_looping(self):
        with patch.object(parts, "MAX_PART_FILES", 1), \
                patch.object(helper, "create_multipart_operational_backup", wraps=helper.create_multipart_operational_backup) as create:
            first = self.execute()
            self.assertEqual(create.call_count, 1)
            self.assertEqual(first["state"], "in_progress")
            self.assertIsNone(first["stored_files"])
            self.assertIsNone(first["stored_bytes"])
            self.assertIsNone(first["manifest_hash"])
            self.assertEqual(first["next_action"], "resume_same_checkpoint")
            pending = self.root / first["backup_folder"] / "pending.json"
            original = pending.read_bytes()
            atomic_bytes(self.code, b"# Later source bytes must not replace the frozen selection.\n")
            with patch.object(parts, "_prepare", side_effect=AssertionError("No new checkpoint")):
                second = self.execute("resume")
                self.assertEqual(create.call_count, 2)
                self.assertEqual(second["state"], "in_progress")
                third = self.execute("resume")
                self.assertEqual(create.call_count, 3)
        self.assertEqual(third["state"], "records_verified_with_limitations")
        self.assertEqual(first["plan_hash"], second["plan_hash"])
        self.assertEqual(first["plan_hash"], third["plan_hash"])
        self.assertEqual(pending.read_bytes(), original)
        self.assertEqual(len({row["receipt_file"] for row in (first, second, third)}), 3)
        for row in (first, second, third):
            self.assert_saved_receipt(row)
        self.assert_unchanged()

    def test_ready_reuse_verifies_the_same_manifest_without_reselection_or_overwrite(self):
        first = self.execute()
        folder = self.root / first["backup_folder"]
        original = (folder / "manifest.json").read_bytes()
        with patch.object(parts, "_prepare", side_effect=AssertionError("Do not select again")), \
                patch.object(parts, "_copy_outcome", side_effect=AssertionError("Do not copy again")):
            second = self.execute()
        self.assertTrue(second["replayed"])
        self.assertEqual(second["manifest_hash"], first["manifest_hash"])
        self.assertEqual((folder / "manifest.json").read_bytes(), original)
        self.assertNotEqual(first["receipt_file"], second["receipt_file"])
        self.assertEqual(set(second["phase_timings_seconds"]), {"backup_saved_verification"})
        self.assert_saved_receipt(second)
        self.assert_unchanged()

    def test_busy_existing_research_records_one_failure_without_copy_or_retry(self):
        status = self.root / "data/operations/research-runs/current.json"
        atomic_json(status, {"run_id": "synthetic-active-research", "writer_active": True,
            "message": "Keep the actual research visible."})
        original_status = status.read_bytes()
        with Journal(self.memory.root / "programme_writer").locked(), \
                patch.object(helper, "create_multipart_operational_backup", side_effect=AssertionError("Do not compete")) as create:
            result = self.execute()
        self.assertEqual(result["state"], "waiting_for_existing_writer")
        self.assertEqual(result["reason"], "RUNNER_ALREADY_ACTIVE")
        self.assertIsNone(result["recorded_writer_scope_seconds"])
        create.assert_not_called()
        self.assertFalse((self.root / result["backup_folder"]).exists())
        self.assertEqual(status.read_bytes(), original_status)
        log = self.root / result["operation_log"]
        rows = [json.loads(line) for line in log.read_bytes().splitlines()]
        self.assertEqual([row["event"] for row in rows], ["invocation_started"])
        self.assertTrue(result["operation_recording_complete"])
        self.assert_saved_receipt(result)
        self.assert_unchanged()

    def test_resume_without_an_original_plan_does_not_create_a_checkpoint(self):
        with patch.object(helper, "create_multipart_operational_backup", side_effect=AssertionError("No new checkpoint")) as create:
            result = self.execute("resume")
        self.assertEqual(result["reason"], "FINAL_CHECKPOINT_NOT_STARTED")
        create.assert_not_called()
        self.assert_saved_receipt(result)
        self.assert_unchanged()

    def test_restore_keeps_fresh_targets_and_never_enters_programme_operation_or_writer(self):
        saved = self.execute()
        with Journal(self.memory.root / "programme_writer").locked(), \
                patch.object(helper, "operation_run", side_effect=AssertionError("No programme recorder for restore")), \
                patch.object(helper, "operation_writer", side_effect=AssertionError("No programme writer for restore")):
            first = self.execute("restore")
            second = self.execute("restore")
        self.assertNotEqual(first["restore_destination"], second["restore_destination"])
        self.assertNotEqual(first["receipt_file"], second["receipt_file"])
        for result in (first, second):
            self.assertEqual(result["state"], "isolated_records_verified")
            self.assertEqual(result["backup_hash"], saved["manifest_hash"])
            self.assertEqual(result["research_events"], 1)
            self.assertEqual(result["financial_events"], 2)
            self.assertEqual(result["files_verified"], 3)
            self.assertIsNone(result["operation_run_id"])
            self.assertIsNone(result["phase_timings_seconds"])
            self.assertTrue((self.root / result["restore_destination"] / "restore-proof.json").is_file())
            self.assert_saved_receipt(result)
        self.assert_unchanged()

    def test_restore_of_an_unfinished_checkpoint_does_not_create_a_target(self):
        with patch.object(helper, "restore_multipart_backup", side_effect=AssertionError("Not ready")) as restore:
            result = self.execute("restore")
        self.assertEqual(result["reason"], "FINAL_CHECKPOINT_NOT_READY")
        restore.assert_not_called()
        self.assertNotIn("restore_destination", result)
        self.assert_saved_receipt(result)
        self.assert_unchanged()

    def test_corrupt_ready_manifest_is_not_replaced_or_reselected(self):
        saved = self.execute()
        folder = self.root / saved["backup_folder"]
        pending = (folder / "pending.json").read_bytes()
        atomic_bytes(folder / "manifest.json", b"{}\n")
        with patch.object(parts, "_prepare", side_effect=AssertionError("No reselect")):
            result = self.execute("resume")
        self.assertEqual(result["reason"], "BACKUP_PART_CONTROL_INVALID")
        self.assertEqual((folder / "manifest.json").read_bytes(), b"{}\n")
        self.assertEqual((folder / "pending.json").read_bytes(), pending)
        self.assert_saved_receipt(result)
        self.assert_unchanged()

    def test_ordinary_failure_has_a_safe_receipt_without_private_exception_text(self):
        with patch.object(helper, "create_multipart_operational_backup", side_effect=OSError("Private path, token, account")) as create:
            result = self.execute()
        self.assertEqual(create.call_count, 1)
        self.assertEqual(result["state"], "needs_follow_up")
        self.assertEqual(result["error_type"], "OSError")
        self.assertNotIn("Private path", json.dumps(result))
        self.assert_saved_receipt(result)
        self.assert_unchanged()

    def test_interruption_propagates_after_its_unique_safe_receipt_and_guard_release(self):
        for error in (KeyboardInterrupt(), SystemExit(17), GeneratorExit()):
            with self.subTest(error=type(error).__name__):
                with patch.object(helper, "create_multipart_operational_backup", side_effect=error):
                    with self.assertRaises(type(error)) as caught:
                        self.execute()
                self.assertIs(caught.exception, error)
                receipt = max(self.proof_root.glob("final-checkpoint-create-*.json"), key=lambda path: path.stat().st_mtime_ns)
                result = load_json(receipt, 65536)
                self.assertEqual(result["state"], "interrupted")
                self.assert_saved_receipt(result)
                with Journal(self.memory.root / "programme_writer").locked():
                    pass
        self.assert_unchanged()

    def test_receipt_write_failure_never_repeats_or_mislabels_completed_copy(self):
        with patch.object(helper, "atomic_json", side_effect=OSError("Private receipt path")), \
                patch.object(helper, "create_multipart_operational_backup", wraps=helper.create_multipart_operational_backup) as create:
            result = self.execute()
        self.assertEqual(create.call_count, 1)
        self.assertFalse(result["receipt_saved"])
        self.assertNotIn("Private receipt", json.dumps(result))
        self.assertTrue((self.root / result["backup_folder"] / "manifest.json").is_file())
        self.assertEqual((self.root / result["receipt_file"]).read_bytes(), b"")
        self.assert_unchanged()

    def test_bad_or_missing_operation_log_is_unknown_not_invented_zero_timing(self):
        class Recorder:
            folder = self.proof_root
            run_id = "synthetic-run"
            recording_failed = False
        rotated_fragment = (json.dumps({"run_id": "synthetic-run", "event": "phase_finished",
            "phase": "backup_copy", "timings": {"backup_copy": 1}}) + "\n").encode()
        for raw in (None, b"Broken JSON\n", b"x" * 65537, rotated_fragment):
            with self.subTest(raw=None if raw is None else len(raw)):
                if raw is not None:
                    atomic_bytes(self.proof_root / "synthetic-run.jsonl", raw)
                result = helper._timing_summary(Recorder())
                self.assertFalse(result["operation_recording_complete"])
                self.assertIsNone(result["phase_timings_seconds"])
                self.assertIsNone(result["recorded_writer_scope_seconds"])
        self.assert_unchanged()

    def test_cli_cannot_select_a_different_id_or_bypass_waiting_or_missing_receipt(self):
        with patch.object(helper, "execute") as execute, redirect_stderr(io.StringIO()):
            with self.assertRaises(SystemExit):
                helper.main(["create", "--backup-id", "daily-v3-2026-10-11"])
            execute.assert_not_called()
        for result in ({"state": "in_progress", "receipt_saved": True},
                       {"state": "waiting_for_existing_writer", "receipt_saved": True},
                       {"state": "records_verified_with_limitations", "receipt_saved": False}):
            with self.subTest(result=result), patch.object(helper, "execute", return_value=result), \
                    redirect_stdout(io.StringIO()) as output:
                code = helper.main(["create"])
            self.assertEqual(code, 0 if result["state"] == "in_progress" else 1)
            self.assertEqual(json.loads(output.getvalue()), result)
        self.assert_unchanged()


if __name__ == "__main__":
    unittest.main()
