"""Real local migrations and normal backup continuation, synthetic stores only."""

from contextlib import redirect_stdout
import importlib.util
import io
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from product_radar import financial_segments, research_segments
from product_radar import local_storage_adoption as adoption
from product_radar.models import SafetyError, digest
from product_radar.operational_backup import restore_operational_backup, verify_operational_backup
from product_radar.operational_backup_schedule import run_due_operational_backup, _save_control, VERSION as DAY_VERSION
from product_radar.research_memory import ResearchMemory
from product_radar.storage import Journal, atomic_bytes, atomic_json, load_json


NOW = "2026-10-09T12:00:00Z"
SETTINGS = {"schema_version": 1, "enabled": False, "destination": "data/backups", "timezone": "Asia/Kolkata"}


class LocalStorageAdoptionTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory(prefix="local-storage-adoption-")
        self.addCleanup(temp.cleanup); self.root = Path(temp.name) / "project"
        self.memory = ResearchMemory(self.root / adoption.RESEARCH, Journal(self.root / adoption.FINANCE),
            evidence_type="synthetic")
        for index in range(3):
            self.memory.put("concepts", "fictional-" + str(index), {"label": "Synthetic product " + str(index)})
        with self.memory.journal.locked():
            self.memory.journal.setup_budget("fictional-account:period", "5")
            self.finance_state = self.memory.journal.state()
            self.finance_events = self.memory.journal.events()
            self.spendable = self.memory.journal.spendable("fictional-account:period")
        self.research = self.memory.snapshot_with_provenance()
        self.original_research = {path.name: path.read_bytes() for path in (self.memory.root / "events").glob("*.json")}
        self.original_finance = self.memory.journal.path.read_bytes()
        atomic_json(self.root / adoption.SETTINGS, SETTINGS)

    def execute(self, **kwargs):
        with patch("urllib.request.urlopen", side_effect=AssertionError("No external calls in storage adoption")):
            return adoption.adopt_local_storage(self.root, execute=True, memory=self.memory, at=NOW, **kwargs)

    def assert_originals(self):
        self.assertEqual(self.original_research,
            {path.name: path.read_bytes() for path in (self.memory.root / "events").glob("*.json")})
        self.assertEqual(self.original_finance, self.memory.journal.path.read_bytes())

    def test_preview_is_truly_read_only_in_existing_and_nonexistent_project(self):
        before = {str(path.relative_to(self.root)): path.read_bytes() for path in self.root.rglob("*") if path.is_file()}
        with patch.object(adoption, "Journal", side_effect=AssertionError("Preview must not construct a lock/store")):
            result = adoption.adopt_local_storage(self.root)
            absent = self.root.parent / "absent"
            missing = adoption.adopt_local_storage(absent)
        self.assertFalse(absent.exists()); self.assertEqual(missing["state"], "preview")
        self.assertEqual(result["state"], "preview"); self.assertFalse(result["files_written"])
        self.assertFalse(result["plan"]["stores"]["research"]["canonical_verified"])
        self.assertEqual(before, {str(path.relative_to(self.root)): path.read_bytes() for path in self.root.rglob("*") if path.is_file()})

    def test_real_migrations_cold_readback_and_original_bytes_preserved(self):
        result = self.execute()
        self.assertEqual(result["state"], "complete"); self.assertTrue(result["receipt_saved"])
        for name in ("research", "finance"):
            self.assertTrue(result["stores"][name]["cold_readback_verified"])
            self.assertTrue(result["stores"][name]["originals_preserved"])
        fresh = ResearchMemory(self.memory.root, Journal(self.memory.journal.root), evidence_type="synthetic")
        self.assertEqual(fresh.snapshot_with_provenance(), self.research)
        with fresh.journal.locked():
            self.assertEqual(fresh.journal.events(), self.finance_events)
            self.assertEqual(fresh.journal.state(), self.finance_state)
            self.assertEqual(fresh.journal.spendable("fictional-account:period"), self.spendable)
        receipt = load_json(self.root / adoption.RECEIPT)
        self.assertEqual(receipt["hash"], digest({key: value for key, value in receipt.items() if key != "hash"}))
        self.assertFalse(receipt["independent_backup_proved"])
        self.assert_originals(); self.assertEqual(load_json(self.root / adoption.SETTINGS), SETTINGS)

    def test_exact_replay_does_not_append_or_republish_migration_pointers(self):
        self.execute()
        pointers = [(self.memory.root / research_segments.POINTER), (self.memory.journal.root / financial_segments.POINTER)]
        before = [path.read_bytes() for path in pointers]
        result = self.execute()
        self.assertEqual([row["state"] for row in result["stores"].values()], ["already_adopted", "already_adopted"])
        self.assertEqual(before, [path.read_bytes() for path in pointers]); self.assert_originals()

    def test_backup_upgrade_requires_explicit_option_and_preserves_disabled_state(self):
        self.execute(); self.assertEqual(load_json(self.root / adoption.SETTINGS), SETTINGS)
        result = self.execute(adopt_backup_settings=True)
        self.assertEqual(result["backup"]["state"], "adopted")
        settings = load_json(self.root / adoption.SETTINGS)
        self.assertEqual(settings["schema_version"], 2); self.assertFalse(settings["enabled"])
        self.assertFalse(result["backup"]["backup_copy_verified"])
        self.assertFalse((self.root / "data/backups").exists())
        self.assertEqual(run_due_operational_backup(self.memory, root=self.root, now=NOW)["state"], "disabled")
        self.assertEqual(self.execute(adopt_backup_settings=True)["backup"]["state"], "already_adopted")

    def test_missing_settings_are_not_invented_and_malformed_settings_do_not_block_stores(self):
        (self.root / adoption.SETTINGS).unlink()
        result = self.execute(adopt_backup_settings=True)
        self.assertEqual(result["backup"]["state"], "not_configured")
        self.assertFalse((self.root / adoption.SETTINGS).exists())
        atomic_json(self.root / adoption.SETTINGS, {"schema_version": 7})
        result = self.execute(adopt_backup_settings=True)
        self.assertEqual(result["state"], "partial"); self.assertEqual(result["backup"]["state"], "waiting")
        self.assertTrue(all(row["verified"] for row in result["stores"].values()))
        self.assertEqual(load_json(self.root / adoption.SETTINGS), {"schema_version": 7})

    def test_corrupt_research_does_not_stop_actual_finance_migration_or_upgrade_backup(self):
        path = self.memory.root / "events/00000001.json"
        event = load_json(path); event["changes"][0]["value"]["label"] = "Not an original"
        atomic_json(path, event)
        result = self.execute(adopt_backup_settings=True)
        self.assertEqual(result["state"], "partial"); self.assertFalse(result["stores"]["research"]["verified"])
        self.assertTrue(result["stores"]["finance"]["verified"])
        self.assertTrue(financial_segments.enabled(self.memory.journal))
        self.assertEqual(result["backup"]["reason"], "LOCAL_STORAGE_BACKUP_REQUIRES_VERIFIED_STORES")
        self.assertEqual(load_json(self.root / adoption.SETTINGS), SETTINGS)

    def test_corrupt_finance_does_not_stop_actual_research_migration(self):
        atomic_bytes(self.memory.journal.path, self.original_finance + b"not JSON\n")
        result = self.execute(adopt_backup_settings=True)
        self.assertTrue(result["stores"]["research"]["verified"])
        self.assertFalse(result["stores"]["finance"]["verified"])
        self.assertEqual(result["state"], "partial"); self.assertEqual(load_json(self.root / adoption.SETTINGS), SETTINGS)
        self.assertEqual(self.memory.journal.path.read_bytes(), self.original_finance + b"not JSON\n")

    def test_prepublication_failure_is_named_other_store_runs_originals_remain(self):
        original = research_segments.atomic_json
        def fail_pointer(path, value):
            if Path(path).name == research_segments.POINTER: raise OSError("Synthetic prepublish failure")
            return original(path, value)
        with patch.object(research_segments, "atomic_json", side_effect=fail_pointer): result = self.execute()
        self.assertFalse(result["stores"]["research"]["verified"])
        self.assertTrue(result["stores"]["finance"]["verified"]); self.assert_originals()
        self.assertEqual(self.memory.snapshot_with_provenance(), self.research)
        self.assertEqual(self.execute()["state"], "complete")

    def test_lost_actual_research_and_finance_publish_replies_reconcile_cold(self):
        real_research, real_finance = research_segments.atomic_json, financial_segments._publish
        def lose_research(path, value):
            real_research(path, value)
            if Path(path).name == research_segments.POINTER: raise OSError("Synthetic committed reply lost")
        def lose_finance(journal, value):
            real_finance(journal, value); raise OSError("Synthetic committed reply lost")
        with patch.object(research_segments, "atomic_json", side_effect=lose_research), \
                patch.object(financial_segments, "_publish", side_effect=lose_finance): result = self.execute()
        self.assertEqual(result["state"], "complete")
        self.assertTrue(all(row["recovered_committed_reply"] for row in result["stores"].values()))
        self.assert_originals(); self.assertEqual(self.execute()["state"], "complete")

    def test_changed_preview_or_busy_existing_programme_guard_never_adopts(self):
        preview = adoption.preview_local_storage_adoption(self.root)
        self.memory.put("concepts", "later", {"label": "Synthetic later item"})
        with self.assertRaisesRegex(SafetyError, "LOCAL_STORAGE_ADOPTION_INPUT_CHANGED"):
            self.execute(expected_plan_hash=preview["plan_hash"])
        with Journal(self.memory.root / "programme_writer").locked():
            with self.assertRaisesRegex(SafetyError, "RUNNER_ALREADY_ACTIVE"): self.execute()
        self.assertFalse(research_segments.enabled(self.memory)); self.assertFalse(financial_segments.enabled(self.memory.journal))

    def test_migration_uses_existing_large_legacy_reader_not_old_total_file_cap(self):
        # This is a dispatch regression, not a claim to simulate a large file:
        # lower the old reader cap under the genuinely saved valid legacy bytes.
        with patch("product_radar.storage.MAX_JOURNAL", 1): result = self.execute()
        self.assertEqual(result["state"], "complete"); self.assert_originals()

    def test_original_pending_legacy_backup_is_not_abandoned_by_schema_switch(self):
        control = {"schema_version": 1, "kind": DAY_VERSION, "root_hash": digest(str(self.root)), "day": "2026-10-08",
            "backup_id": "daily-2026-10-08", "attempted_at": "2026-10-08T12:00:00Z", "completed_at": None,
            "manifest_created_at": None, "state": "attempt_started", "manifest_hash": None, "files": None, "bytes": None,
            "limitations": [], "reason": None}
        path = self.root / "data/operations/backup_controls/days/2026-10-08.json"; _save_control(path, control)
        result = self.execute(adopt_backup_settings=True)
        self.assertTrue(all(row["verified"] for row in result["stores"].values()))
        self.assertEqual(result["backup"]["pending_attempt_ids"], ["daily-2026-10-08"])
        self.assertEqual(result["backup"]["reason"], "LOCAL_STORAGE_BACKUP_LEGACY_ATTEMPT_PENDING")
        self.assertEqual(load_json(self.root / adoption.SETTINGS), SETTINGS)

    def test_invalid_existing_multipart_control_prevents_switch_not_store_readback(self):
        value = {"contract": "essential-operational-record-backup-v3", "root_hash": digest(str(self.root))}
        atomic_json(self.root / "data/operations/backup_controls/multipart-active.json", {"control": value, "hash": digest(value)})
        result = self.execute(adopt_backup_settings=True)
        self.assertEqual(result["backup"]["reason"], "LOCAL_BACKUP_CONTROL_INVALID")
        self.assertTrue(all(row["verified"] for row in result["stores"].values()))
        self.assertEqual(load_json(self.root / adoption.SETTINGS), SETTINGS)

    def test_busy_ordinary_backup_guard_cannot_race_schema_switch(self):
        with Journal(self.root / "data/operations/backup_controls").locked():
            result = self.execute(adopt_backup_settings=True)
        self.assertEqual(result["backup"]["state"], "waiting")
        self.assertEqual(result["backup"]["reason"], "RUNNER_ALREADY_ACTIVE")
        self.assertTrue(all(row["verified"] for row in result["stores"].values()))
        self.assertEqual(load_json(self.root / adoption.SETTINGS), SETTINGS)

    def test_hash_valid_multipart_with_bad_date_is_named_wait_not_exception(self):
        value = {"contract": "essential-operational-record-backup-v3", "root_hash": digest(str(self.root)),
            "day": "not-a-date", "backup_id": "daily-v3-not-a-date", "state": "attempt_started",
            "attempted_at": NOW, "completed_at": None, "manifest_hash": None, "files": None, "bytes": None}
        atomic_json(self.root / "data/operations/backup_controls/multipart-active.json", {"control": value, "hash": digest(value)})
        result = self.execute(adopt_backup_settings=True)
        self.assertEqual(result["state"], "partial"); self.assertEqual(result["backup"]["reason"], "LOCAL_BACKUP_CONTROL_INVALID")
        self.assertTrue(all(row["verified"] for row in result["stores"].values()))
        self.assertEqual(load_json(self.root / adoption.SETTINGS), SETTINGS)

    def test_diagnostic_receipt_failure_does_not_falsely_undo_verified_migrations(self):
        original = adoption.atomic_json
        def fail_receipt(path, value):
            if Path(path).name == "storage-adoption.json": raise OSError("Synthetic receipt failure")
            return original(path, value)
        with patch.object(adoption, "atomic_json", side_effect=fail_receipt): result = self.execute()
        self.assertEqual(result["state"], "complete"); self.assertFalse(result["receipt_saved"])
        self.assertTrue(all(row["verified"] for row in result["stores"].values())); self.assert_originals()

    def test_actual_backup_setting_and_receipt_lost_replies_are_reconciled(self):
        original = adoption.atomic_json
        def lose_reply(path, value):
            original(path, value)
            if Path(path).name in {"backup.json", "storage-adoption.json"}: raise OSError("Synthetic lost committed reply")
        with patch.object(adoption, "atomic_json", side_effect=lose_reply):
            result = self.execute(adopt_backup_settings=True)
        self.assertEqual(result["state"], "complete"); self.assertTrue(result["receipt_saved"])
        self.assertTrue(result["receipt_recovered_committed_reply"])
        self.assertTrue(result["backup"]["recovered_committed_reply"])
        self.assertEqual(load_json(self.root / adoption.SETTINGS)["schema_version"], 2); self.assert_originals()

    def test_actual_new_event_after_migration_cannot_fake_unchanged_cold_head(self):
        original = self.memory.migrate_segments
        def append_after():
            result = original()
            self.memory.put("concepts", "later-real-event", {"label": "Actual new synthetic event"})
            return result
        with patch.object(self.memory, "migrate_segments", side_effect=append_after): result = self.execute()
        self.assertEqual(result["stores"]["research"]["reason"], "LOCAL_STORAGE_RESEARCH_READBACK_FAILED")
        self.assertTrue(result["stores"]["finance"]["verified"])
        self.assertEqual(result["state"], "partial")
        self.assertIn("later-real-event", self.memory.snapshot()["concepts"])
        self.assertEqual(self.execute()["state"], "complete")

    def test_actual_normal_multipart_schedule_continues_and_isolated_restore_keeps_money(self):
        from product_radar import operational_backup_parts as parts
        atomic_bytes(self.root / "src/product_radar/fictional.py", b"# Synthetic application attachment.\n")
        atomic_json(self.root / adoption.SETTINGS, dict(SETTINGS, enabled=True))
        self.assertEqual(self.execute(adopt_backup_settings=True)["state"], "complete")
        self.assertFalse((self.root / "data/backups").exists())
        with patch.object(parts, "MAX_PART_FILES", 1):
            result = run_due_operational_backup(self.memory, root=self.root, now=NOW)
            self.assertEqual(result["reason"], "LOCAL_BACKUP_PARTS_IN_PROGRESS"); original_id = result["backup_id"]
            for _ in range(50):
                result = run_due_operational_backup(self.memory, root=self.root, now="2026-10-10T12:00:00Z")
                self.assertEqual(result["backup_id"], original_id)
                if result["state"] in {"saved", "saved_with_limitations"}: break
            else: self.fail("The actual original multipart copy did not finish")
        folder = self.root / "data/backups" / original_id
        verified = verify_operational_backup(folder)
        target = self.root.parent / "isolated-restore"
        restored = restore_operational_backup(folder, target)
        self.assertEqual(verified["hash"], result["manifest_hash"])
        self.assertEqual(restored["research_events"], self.research["event_count"])
        self.assertEqual(restored["financial_events"], len(self.finance_events))
        cold = ResearchMemory(target / "research", Journal(target / "finance"), evidence_type="synthetic")
        self.assertEqual(cold.snapshot_with_provenance(), self.research)
        with cold.journal.locked(): self.assertEqual(cold.journal.state(), self.finance_state)
        self.assertFalse(restored["independent_backup_proved"]); self.assert_originals()

    def test_script_default_preview_and_explicit_failure_exit(self):
        script = Path(__file__).resolve().parents[1] / "scripts/adopt_local_storage.py"
        spec = importlib.util.spec_from_file_location("adoption_test_script", script)
        module = importlib.util.module_from_spec(spec); spec.loader.exec_module(module)
        with redirect_stdout(io.StringIO()) as output:
            self.assertEqual(module.main([], root=self.root, memory=self.memory), 0)
        self.assertIn('"state": "preview"', output.getvalue()); self.assertFalse(research_segments.enabled(self.memory))
        with redirect_stdout(io.StringIO()) as output:
            self.assertEqual(module.main(["--execute", "--expected-plan-hash", "0" * 64], root=self.root, memory=self.memory), 1)
        self.assertIn("LOCAL_STORAGE_ADOPTION_INPUT_CHANGED", output.getvalue())


if __name__ == "__main__": unittest.main()
