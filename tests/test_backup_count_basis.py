"""Backup count labels on disposable local copies, never live project data.

Multipart ``files`` is the immutable selection count, not successful copies.
These response-only labels must not change the old sealed backup contracts.
Imported test modules supply fixtures without rediscovering their test classes.
"""

from copy import deepcopy
from pathlib import Path
import sys
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "tests"))

import test_operational_backup_parts as multipart_fixtures
import test_operational_backup_schedule as legacy_fixtures
import test_programme_summary_connection as programme_fixtures
import test_programme_summary as summary_fixtures
import test_catalogue_expansion as catalogue_fixtures
from product_radar import operational_backup_parts as parts
from product_radar import operational_backup_schedule as schedule
from product_radar import operations
from product_radar.models import SafetyError, digest
from product_radar.programme import (
    _refresh_owner_backup, _signature, programme_owner_summary,
)
from product_radar.service import load_policy
from product_radar.storage import load_json


NOW = "2026-10-08T12:00:00+00:00"


class MultipartCountBasisTests(unittest.TestCase):
    def setUp(self):
        self.fixture = multipart_fixtures.MultipartBackupTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.root, self.memory = self.fixture.root, self.fixture.memory
        self.control = self.root / "data/operations/backup_controls/multipart-active.json"
        self.folder = self.root / "data/backups/daily-v3-2026-10-08"
        network = patch("urllib.request.urlopen", side_effect=AssertionError("No provider request"))
        model = patch("product_radar.research_ai.CodexRunner.__call__",
                      side_effect=AssertionError("No model request"))
        network.start(); self.addCleanup(network.stop)
        model.start(); self.addCleanup(model.stop)

    def run_backup(self, **options):
        return schedule.run_due_operational_backup(self.memory, root=self.root,
            settings=multipart_fixtures.SETTINGS, now=NOW, **options)

    def finish_backup(self):
        for _ in range(8):
            result = self.run_backup()
            if result["state"] in {"saved", "saved_with_limitations"}:
                return result
        self.fail("The small, frozen synthetic backup did not complete.")

    def assert_passive(self, result):
        for field in ("provider_calls", "model_calls", "financial_writes", "research_writes"):
            self.assertEqual(result[field], 0, field)
        self.assertIs(result["independent_backup_proved"], False)
        self.assertIs(result["original_layout_resume_proved"], False)

    def test_actual_limited_copy_labels_planned_files_not_successful_copies(self):
        before = self.memory.snapshot()
        with self.memory.journal.locked():
            financial_before = self.memory.journal.export_bytes()
        # An existing, unfrozen v3 plan can lose an attachment before copying.
        # Only the disposable fixture file is removed; no real project is read.
        with patch.object(parts, "MAX_PART_FILES", 1):
            with patch("product_radar.operational_backup_selection.select_essential_backup_files",
                       side_effect=self.fixture.legacy_unfrozen_selection):
                first = self.run_backup()
            self.assertEqual(first["state"], "waiting")
            self.fixture.code.unlink()
            with patch.object(self.memory, "put", side_effect=AssertionError("Research write")), \
                 patch.object(self.memory.journal, "append", side_effect=AssertionError("Financial write")):
                result = self.finish_backup()

        manifest = load_json(self.folder / "manifest.json")
        _plan, selected, outcomes = parts._verify(self.folder, manifest)
        copied = sum(outcome["stored"] for outcome in outcomes)
        self.assertEqual(manifest["copy_failures"], 1)
        self.assertEqual(len(selected), copied + 1)
        self.assertEqual(result["state"], "saved_with_limitations")
        self.assertEqual(result["file_count_basis"], "planned_files")
        self.assertEqual(result["planned_files"], len(selected))
        self.assertEqual(result["files"], manifest["files"])
        self.assertNotEqual(result["planned_files"], copied)
        self.assertEqual(result["bytes"], manifest["bytes"])
        self.assertIn("Some files could not be included", " ".join(result["limitations"]))

        wrapper = load_json(self.control)
        self.assertEqual(wrapper["hash"], digest(wrapper["control"]))
        self.assertEqual(set(wrapper["control"]), {
            "contract", "root_hash", "day", "backup_id", "state", "attempted_at",
            "completed_at", "manifest_hash", "files", "bytes",
        })
        self.assertEqual(set(manifest), parts._MANIFEST_FIELDS | {"hash"})
        for original in (wrapper["control"], manifest):
            self.assertNotIn("file_count_basis", original)
            self.assertNotIn("planned_files", original)
        self.assertEqual(self.memory.snapshot(), before)
        with self.memory.journal.locked():
            self.assertEqual(self.memory.journal.export_bytes(), financial_before)
        self.assert_passive(result)

    def test_same_day_reuse_reads_only_control_and_preserves_all_saved_bytes(self):
        first = self.run_backup()
        self.assertEqual(first["state"], "saved")
        original_control = self.control.read_bytes()
        originals = {path.relative_to(self.fixture.destination): path.read_bytes()
                     for path in self.fixture.destination.rglob("*") if path.is_file()}
        reads = []
        actual_load = schedule.load_json

        def control_only(path, *args, **kwargs):
            self.assertEqual(Path(path), self.control, "Cached status read a manifest or payload")
            reads.append(Path(path))
            return actual_load(path, *args, **kwargs)

        with patch.object(schedule, "load_json", side_effect=control_only), \
             patch.object(parts, "create_multipart_operational_backup", side_effect=AssertionError("Recopy")), \
             patch.object(parts, "_object_read", side_effect=AssertionError("Payload read")), \
             patch.object(parts, "load_json", side_effect=AssertionError("Manifest/plan read")), \
             patch.object(parts, "_verify", side_effect=AssertionError("Payload verification")), \
             patch.object(schedule, "atomic_json", side_effect=AssertionError("Control rewrite")):
            reused = self.run_backup(creator=lambda *_args, **_kwargs: self.fail("Cached creator called"))

        self.assertEqual(reads, [self.control])
        self.assertTrue(reused["reused_saved_result"])
        self.assertFalse(reused["backup_action_started"])
        self.assertEqual(reused["file_count_basis"], "planned_files")
        self.assertEqual(reused["planned_files"], first["files"])
        self.assertEqual(reused["manifest_hash"], first["manifest_hash"])
        self.assertEqual(reused["limitations"], first["limitations"])
        self.assertEqual(self.control.read_bytes(), original_control)
        self.assertEqual({path.relative_to(self.fixture.destination): path.read_bytes()
                          for path in self.fixture.destination.rglob("*") if path.is_file()}, originals)
        self.assert_passive(reused)

    def test_in_progress_count_stays_none_instead_of_claiming_zero_files(self):
        with patch.object(parts, "MAX_PART_FILES", 1):
            result = self.run_backup()
        plan = load_json(self.folder / "pending.json")
        self.assertGreater(plan["files"], 1)
        self.assertEqual(result["state"], "waiting")
        self.assertEqual(result["reason"], "LOCAL_BACKUP_PARTS_IN_PROGRESS")
        self.assertEqual(result["file_count_basis"], "planned_files")
        self.assertIsNone(result["files"])
        self.assertIsNone(result["planned_files"])
        self.assertIsNone(load_json(self.control)["control"]["files"])
        self.assertFalse((self.folder / "manifest.json").exists())
        self.assert_passive(result)

    def test_recoverable_copy_error_keeps_unknown_count_then_labels_completed_plan(self):
        with patch.object(parts, "_copy_outcome", side_effect=SafetyError("BACKUP_LOCAL_DEVICE_FULL")):
            waiting = self.run_backup()
        self.assertEqual(waiting["state"], "waiting")
        self.assertEqual(waiting["reason"], "BACKUP_LOCAL_DEVICE_FULL")
        self.assertEqual(waiting["file_count_basis"], "planned_files")
        self.assertIsNone(waiting["files"])
        self.assertIsNone(waiting["planned_files"])
        saved = self.finish_backup()
        self.assertEqual(saved["state"], "saved")
        self.assertEqual(saved["file_count_basis"], "planned_files")
        self.assertGreater(saved["planned_files"], 0)
        self.assertEqual(saved["planned_files"], saved["files"])
        self.assert_passive(waiting)
        self.assert_passive(saved)

    def test_legacy_v1_saved_and_cached_results_keep_their_original_count_contract(self):
        legacy = legacy_fixtures.DailyBackupTests()
        legacy.setUp(); self.addCleanup(legacy.doCleanups)
        first = legacy.run_backup()
        self.assertEqual(first["state"], "saved")
        original = legacy.control_path().read_bytes()
        manifest_path = legacy.root / "data/backups/daily-2026-10-08/manifest.json"
        manifest_bytes = manifest_path.read_bytes()
        manifest = load_json(manifest_path)
        self.assertEqual(first["files"], len(manifest["files"]))
        with patch("product_radar.operational_backup._verify_manifest_payload",
                   side_effect=AssertionError("Legacy repeated verification")):
            reused = legacy.run_backup(creator=lambda *_args, **_kwargs: self.fail("Legacy recopy"))
        self.assertTrue(reused["reused_saved_result"])
        self.assertFalse(reused["backup_action_started"])
        self.assertEqual(reused["files"], first["files"])
        for value in (first, reused, load_json(legacy.control_path())["control"], manifest):
            self.assertNotIn("file_count_basis", value)
            self.assertNotIn("planned_files", value)
        self.assertEqual(legacy.control_path().read_bytes(), original)
        self.assertEqual(manifest_path.read_bytes(), manifest_bytes)
        self.assertEqual(legacy.calls, ["daily-2026-10-08"])


class ProgrammeBackupCountBasisTests(unittest.TestCase):
    def setUp(self):
        self.fixture = programme_fixtures.ProgrammeSummaryConnectionTests()
        self.fixture.setUp(); self.addCleanup(self.fixture.doCleanups)
        self.root, self.memory, self.now = self.fixture.root, self.fixture.memory, self.fixture.now
        self.record = self.fixture.prepare()

    def result(self, **extra):
        return {"state": "saved_with_limitations", "destination": "data/backups", "files": 7,
            "file_count_basis": "planned_files", "planned_files": 7, "bytes": 300,
            "limitations": ["Some files could not be included."],
            "provider_calls": 0, "model_calls": 0, "financial_writes": 0, "research_writes": 0,
            "independent_backup_proved": False, "original_layout_resume_proved": False, **extra}

    def test_owner_status_and_real_summary_retain_only_labels_without_progress_or_authority(self):
        state = self.memory.snapshot()
        original_record = deepcopy(self.record)
        signature = _signature(state, self.record)
        raw = self.result(copied_files=7, files_verified=7, approved=True,
                          client_publication_attempted=True, private_payload="Do not expose")
        with self.memory.journal.locked():
            financial_before = self.memory.journal.export_bytes()
        with operations.operation_run(self.root) as recorder, operations.operation_writer():
            operations.record_event("phase_started", stage="reports", phase="report_preparation",
                                    useful_result=False)
            events_before = deepcopy(recorder.counts)
            with patch.object(self.memory, "put", side_effect=AssertionError("Research write")), \
                 patch.object(self.memory.journal, "append", side_effect=AssertionError("Financial write")), \
                 patch("urllib.request.urlopen", side_effect=AssertionError("Provider request")), \
                 patch("product_radar.research_ai.CodexRunner.__call__", side_effect=AssertionError("Model request")):
                status = _refresh_owner_backup(self.memory, self.record,
                    {"at": self.now, "backup_step": lambda *_args: raw})
                with patch.object(self.memory, "snapshot", side_effect=AssertionError("Owner summary replay")):
                    summary = programme_owner_summary(self.memory, self.record, root=self.root,
                        state=state, as_of=self.now, financial_state=summary_fixtures.financial(),
                        account_status=summary_fixtures.account_status(), catalogue=catalogue_fixtures.fixture(),
                        report_pool={"eligible": [], "decisions": []}, policy=load_policy(), allocations={"issues": []},
                        effort_rows={}, effort_checkpoint={"model": None}, cost_baseline=self.fixture.cost_baseline)
            self.assertEqual(recorder.counts, events_before)
            self.assertEqual(recorder.counts["completed_results"], 0)
            self.assertIsNone(recorder.last_useful)
            self.assertEqual(recorder.phase, "report_preparation")

        self.assertEqual(summary["local_backup"], status)
        self.assertEqual(status["file_count_basis"], "planned_files")
        self.assertEqual(status["planned_files"], status["files"])
        for field in ("copied_files", "files_verified", "approved", "client_publication_attempted", "private_payload"):
            self.assertNotIn(field, status)
            self.assertNotIn(field, summary["local_backup"])
        self.assertTrue(summary["owner_only"])
        self.assertFalse(summary["client_publication_attempted"])
        self.assertFalse(status["independent_backup_proved"])
        self.assertFalse(status["original_layout_resume_proved"])
        self.assertEqual(_signature(state, self.record), signature)
        self.assertEqual({name: value for name, value in self.record.items() if name != "owner_backup_status"}, original_record)
        self.assertEqual(self.memory.snapshot(), state)
        with self.memory.journal.locked():
            self.assertEqual(self.memory.journal.export_bytes(), financial_before)
        summary["local_backup"]["limitations"].append("Changed only in returned summary")
        self.assertEqual(status["limitations"], raw["limitations"])
        raw["limitations"].append("Changed only in callback result")
        self.assertEqual(status["limitations"], ["Some files could not be included."])

    def test_count_labels_do_not_override_existing_action_and_recovery_claim_checks(self):
        before = self.memory.snapshot()
        for extra in ({"provider_calls": 1}, {"model_calls": 1}, {"financial_writes": 1},
                      {"research_writes": 1}, {"independent_backup_proved": True},
                      {"original_layout_resume_proved": True}, {"destination": "other-storage"}):
            with self.subTest(extra=extra):
                status = _refresh_owner_backup(self.memory, self.record,
                    {"at": self.now, "backup_step": lambda *_args: self.result(**extra)})
                self.assertEqual(status["state"], "waiting")
                self.assertEqual(status["reason"], "PROGRAMME_BACKUP_RESULT_INVALID")
                self.assertNotIn("file_count_basis", status)
                self.assertNotIn("planned_files", status)
        self.assertEqual(self.memory.snapshot(), before)

    def test_waiting_owner_summary_preserves_none_and_does_not_relabel_legacy_status(self):
        waiting = _refresh_owner_backup(self.memory, self.record,
            {"at": self.now, "backup_step": lambda *_args: self.result(state="waiting", files=None,
                                                                         planned_files=None, bytes=None)})
        self.assertEqual(waiting["file_count_basis"], "planned_files")
        self.assertIsNone(waiting["files"])
        self.assertIsNone(waiting["planned_files"])
        legacy = _refresh_owner_backup(self.memory, self.record,
            {"at": self.now, "backup_step": lambda *_args: {"state": "saved", "files": 3, "bytes": 200}})
        self.assertEqual(legacy["files"], 3)
        self.assertNotIn("file_count_basis", legacy)
        self.assertNotIn("planned_files", legacy)


if __name__ == "__main__":
    unittest.main()
