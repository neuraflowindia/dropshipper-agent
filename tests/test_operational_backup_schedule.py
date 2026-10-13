"""Project-local daily housekeeping; no provider, model or native task actions."""

from copy import deepcopy
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from product_radar.models import SafetyError, digest
from product_radar.operational_backup import create_essential_operational_backup
from product_radar.operational_backup_schedule import (
    VERSION, create_backup_housekeeping_step, run_due_operational_backup)
from product_radar.programme import prepare_programme, set_programme_service_state
from product_radar.research_memory import ResearchMemory
from product_radar.storage import Journal, atomic_json, load_json


SETTINGS = {"schema_version": 1, "enabled": True, "destination": "data/backups", "timezone": "Asia/Kolkata"}


class DailyBackupTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.memory = ResearchMemory(self.root / "data/research/captured",
            Journal(self.root / "data/benchmarks/state/apify"), evidence_type="synthetic")
        self.now = "2026-10-08T12:00:00+00:00"
        self.calls = []

    def create(self, memory, destination, **kwargs):
        self.calls.append(kwargs["backup_id"])
        return create_essential_operational_backup(memory, destination, **kwargs)

    def run_backup(self, **kwargs):
        return run_due_operational_backup(self.memory, root=self.root, now=kwargs.pop("now", self.now),
            settings=kwargs.pop("settings", SETTINGS), creator=kwargs.pop("creator", self.create), **kwargs)

    def control_path(self, day="2026-10-08"):
        return self.root / "data/operations/backup_controls/days" / (day + ".json")

    def started_control(self):
        value = {"schema_version": 1, "kind": VERSION, "root_hash": digest(str(self.root)),
            "day": "2026-10-08", "backup_id": "daily-2026-10-08", "attempted_at": self.now,
            "completed_at": None, "manifest_created_at": None, "state": "attempt_started", "manifest_hash": None,
            "files": None, "bytes": None, "limitations": [], "reason": None}
        atomic_json(self.control_path(), {"control": value, "hash": digest(value)})
        return value

    def test_96_ticks_in_same_day_make_one_copy_and_do_not_repeat_payload_verification(self):
        first = self.run_backup()
        self.assertEqual(first["state"], "saved")
        self.assertTrue(first["backup_action_started"])
        self.assertFalse(first["independent_backup_proved"])
        with patch("product_radar.operational_backup._verify_manifest_payload", side_effect=AssertionError("Repeated file scan")):
            for _ in range(95):
                result = self.run_backup()
                self.assertTrue(result["reused_saved_result"])
                self.assertFalse(result["backup_action_started"])
        self.assertEqual(self.calls, ["daily-2026-10-08"])
        self.assertIn("drive", " ".join(first["limitations"]))

    def test_next_india_day_keeps_old_copy_and_makes_one_new_copy(self):
        self.run_backup()
        same = self.run_backup(now="2026-10-08T18:29:59+00:00")
        next_day = self.run_backup(now="2026-10-08T18:30:00+00:00")
        self.assertEqual(same["day"], "2026-10-08")
        self.assertEqual(next_day["day"], "2026-10-09")
        self.assertEqual(self.calls, ["daily-2026-10-08", "daily-2026-10-09"])
        self.assertTrue((self.root / "data/backups/daily-2026-10-08/manifest.json").is_file())

    def test_missing_or_disabled_settings_create_no_backup_or_control_folders(self):
        value = run_due_operational_backup(self.memory, root=self.root, now=self.now, creator=self.create)
        self.assertEqual(value["state"], "not_configured")
        disabled = dict(SETTINGS, enabled=False)
        self.assertEqual(self.run_backup(settings=disabled)["state"], "disabled")
        self.assertEqual(self.calls, [])
        self.assertFalse((self.root / "data/backups").exists())
        self.assertFalse((self.root / "data/operations/backup_controls").exists())

    def test_failed_copy_is_attempted_once_today_and_can_run_next_day(self):
        def broken(*args, **kwargs):
            self.calls.append(kwargs["backup_id"])
            raise SafetyError("BACKUP_LOCAL_DEVICE_FULL")
        first = self.run_backup(creator=broken)
        second = self.run_backup(creator=broken)
        self.assertEqual(first["state"], "failed")
        self.assertEqual(second["reason"], "BACKUP_LOCAL_DEVICE_FULL")
        self.assertFalse(second["backup_action_started"])
        self.assertEqual(self.calls, ["daily-2026-10-08"])
        self.assertEqual(self.run_backup(now="2026-10-09T12:00:00+00:00")["state"], "saved")

    def test_unfinished_original_intent_does_not_start_a_second_copy(self):
        self.started_control()
        first = self.run_backup()
        self.assertEqual(first["state"], "waiting")
        self.assertEqual(first["reason"], "LOCAL_BACKUP_ATTEMPT_INCOMPLETE")
        self.assertEqual(self.calls, [])

    def test_ready_original_manifest_recovers_metadata_without_rereading_payload(self):
        self.started_control()
        saved = self.create(self.memory, self.root / "data/backups", backup_id="daily-2026-10-08", root=self.root)
        with patch("product_radar.operational_backup._verify_manifest_payload", side_effect=AssertionError("Repeated verify")):
            value = self.run_backup()
        self.assertEqual(value["state"], "saved")
        self.assertEqual(value["manifest_hash"], saved["manifest"]["hash"])
        self.assertEqual(value["manifest_created_at"], saved["manifest"]["created_at"])
        self.assertTrue(value["reused_saved_result"])
        self.assertFalse(value["backup_action_started"])
        self.assertIn("did not reread", " ".join(value["limitations"]))
        self.assertEqual(len(self.calls), 1)

    def test_records_only_v1_receipt_cannot_be_reported_as_essential_backup(self):
        from product_radar.operational_backup import create_operational_backup
        self.started_control()
        create_operational_backup(self.memory, self.root / "data/backups", backup_id="daily-2026-10-08")
        value = self.run_backup()
        self.assertEqual(value["state"], "waiting")
        self.assertEqual(value["reason"], "LOCAL_BACKUP_ESSENTIAL_MANIFEST_REQUIRED")
        self.assertEqual(self.calls, [])

    def test_foreign_project_receipt_cannot_be_used_for_this_day(self):
        other = self.root / "other-project"; other.mkdir()
        foreign = ResearchMemory(other / "data/research/captured",
            Journal(other / "data/benchmarks/state/apify"), evidence_type="synthetic")
        self.started_control()
        self.create(foreign, self.root / "data/backups", backup_id="daily-2026-10-08", root=other)
        value = self.run_backup()
        self.assertEqual(value["state"], "waiting")
        self.assertEqual(value["reason"], "LOCAL_BACKUP_PROJECT_BINDING_CHANGED")
        self.assertEqual(len(self.calls), 1)

    def test_final_status_save_failure_keeps_original_ready_copy_for_reconciliation(self):
        from product_radar.operational_backup_schedule import _save_control
        calls = []
        def one_failure(path, control):
            calls.append(control["state"])
            if control["state"] == "saved" and calls.count("saved") == 1:
                raise OSError("Temporary status save failure")
            _save_control(path, control)
        with patch("product_radar.operational_backup_schedule._save_control", side_effect=one_failure):
            first = self.run_backup()
            second = self.run_backup()
        self.assertEqual(first["state"], "waiting")
        self.assertEqual(first["reason"], "LOCAL_BACKUP_CONTROL_SAVE_FAILED")
        self.assertEqual(second["state"], "saved")
        self.assertEqual(first["manifest_hash"], second["manifest_hash"])
        self.assertEqual(self.calls, ["daily-2026-10-08"])

    def test_foreign_research_or_financial_store_is_rejected_before_copy(self):
        other = self.root / "foreign"
        foreign = ResearchMemory(other / "data/research/captured",
            Journal(other / "data/benchmarks/state/apify"), evidence_type="synthetic")
        for memory in (foreign, ResearchMemory(self.memory.root, foreign.journal, evidence_type="synthetic")):
            value = run_due_operational_backup(memory, root=self.root, now=self.now,
                settings=SETTINGS, creator=self.create)
            self.assertEqual(value["state"], "waiting")
            self.assertEqual(value["reason"], "LOCAL_BACKUP_STORAGE_BINDING_CHANGED")
        self.assertEqual(self.calls, [])
        self.assertFalse(self.control_path().exists())

    def test_corrupt_daily_control_stops_copy_not_research_records(self):
        self.started_control(); value = load_json(self.control_path())
        value["control"]["backup_id"] = "other-day"
        atomic_json(self.control_path(), value)
        before = self.memory.snapshot()
        result = self.run_backup()
        self.assertEqual(result["state"], "waiting")
        self.assertEqual(result["reason"], "LOCAL_BACKUP_CONTROL_INVALID")
        self.assertEqual(self.memory.snapshot(), before)
        self.assertEqual(self.calls, [])

    def test_busy_housekeeping_guard_creates_no_second_copy(self):
        with Journal(self.root / "data/operations/backup_controls").locked():
            result = self.run_backup()
        self.assertEqual(result["state"], "waiting")
        self.assertEqual(result["reason"], "RUNNER_ALREADY_ACTIVE")
        self.assertEqual(self.calls, [])

    def test_invalid_destination_is_not_a_permission_to_copy_elsewhere(self):
        for destination in ("/tmp/unrelated-backups", "config/local", "data/backups/child", "../backups"):
            with self.subTest(destination=destination):
                result = self.run_backup(settings=dict(SETTINGS, destination=destination))
                self.assertEqual(result["reason"], "BACKUP_SETTINGS_INVALID")
        self.assertEqual(self.calls, [])

    def test_symlink_control_path_is_ignored_without_writes_outside_project(self):
        self.started_control()
        path = self.control_path(); original = load_json(path); path.unlink()
        other = self.root / "untouched.json"; atomic_json(other, original); path.symlink_to(other)
        result = self.run_backup()
        self.assertEqual(result["state"], "waiting")
        self.assertEqual(result["reason"], "SYMLINK_PATH_REFUSED")
        self.assertEqual(load_json(other), original)
        self.assertEqual(self.calls, [])

    def test_no_provider_model_financial_or_research_write_is_used(self):
        before = self.memory.snapshot()
        with self.memory.journal.locked():
            money = self.memory.journal.state()
        with patch("urllib.request.urlopen", side_effect=AssertionError("Provider")), \
             patch("product_radar.research_ai.CodexRunner.__call__", side_effect=AssertionError("Model")), \
             patch.object(self.memory, "put", side_effect=AssertionError("Research write")), \
             patch.object(self.memory.journal, "append", side_effect=AssertionError("Money write")):
            value = self.run_backup()
        self.assertEqual(value["state"], "saved")
        self.assertEqual(self.memory.snapshot(), before)
        with self.memory.journal.locked():
            self.assertEqual(self.memory.journal.state(), money)
        for field in ("provider_calls", "model_calls", "financial_writes", "research_writes"):
            self.assertEqual(value[field], 0)

    def test_configuring_callback_does_not_read_credentials_or_copy(self):
        with patch("product_radar.operational_backup_schedule.run_due_operational_backup",
                   side_effect=AssertionError("Copy during factory")), \
             patch("product_radar.operational_backup_schedule.load_json", side_effect=AssertionError("Config read")):
            self.assertTrue(callable(create_backup_housekeeping_step(self.root)))

    def test_owner_paused_callback_performs_no_housekeeping(self):
        record = prepare_programme(self.memory, "example", as_of=self.now)
        set_programme_service_state(self.memory, enabled=False, as_of=self.now)
        with patch("product_radar.operational_backup_schedule.run_due_operational_backup",
                   side_effect=AssertionError("Copy while paused")):
            result = create_backup_housekeeping_step(self.root)(self.memory, record, {"at": self.now})
        self.assertEqual(result["state"], "owner_paused")

    def test_callback_uses_current_time_not_stale_source_stage_time(self):
        record = prepare_programme(self.memory, "example", as_of=self.now)
        with patch("product_radar.operational_backup_schedule.run_due_operational_backup", return_value={}) as run:
            create_backup_housekeeping_step(self.root)(self.memory, record, {"at": self.now})
        self.assertNotIn("now", run.call_args.kwargs)
        self.assertEqual(len(run.call_args.kwargs["pending_controls"]), 1)


if __name__ == "__main__":
    unittest.main()
