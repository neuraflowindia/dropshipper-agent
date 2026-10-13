"""Actual resumable local backups, isolated stores, no provider/model calls."""

from copy import deepcopy
from hashlib import sha256
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor
import tempfile
import threading
import unittest
from unittest.mock import patch

from product_radar import operational_backup_parts as parts
from product_radar.models import SafetyError, canonical, digest
from product_radar.operational_backup import restore_operational_backup, verify_operational_backup
from product_radar.operational_backup_schedule import run_due_operational_backup
from product_radar.operational_backup_selection import select_essential_backup_files
from product_radar.research_memory import ResearchMemory
from product_radar.storage import Journal, MAX_JOURNAL, atomic_bytes, atomic_json, load_json


SETTINGS = {"schema_version": 2, "enabled": True, "destination": "data/backups", "timezone": "Asia/Kolkata",
    "backup_contract": parts.VERSION}


class MultipartBackupTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="multipart-backup-")
        self.addCleanup(temporary.cleanup); self.root = Path(temporary.name) / "project"
        self.memory = ResearchMemory(self.root / "data/research/captured", Journal(self.root / "data/benchmarks/state/apify"),
            evidence_type="synthetic")
        self.memory.put("concepts", "fictional-concept", {"description": {"label": "Fictional product"}})
        with self.memory.journal.locked():
            self.memory.journal.setup_budget("fictional-account:period", "5")
        self.code = self.root / "src/product_radar/fictional.py"
        atomic_bytes(self.code, b"# Synthetic offline code fixture.\n")
        self.destination = self.root / "data/backups"

    def create(self, backup_id="multipart-one", **kwargs):
        return parts.create_multipart_operational_backup(self.memory, self.destination, backup_id=backup_id,
            root=self.root, **kwargs)

    def complete(self, backup_id="multipart-one", **kwargs):
        for _ in range(32):
            value = self.create(backup_id, **kwargs)
            if value["manifest"] is not None:
                return value
        self.fail("The original bounded set did not finish.")

    def legacy_unfrozen_selection(self, *args, **kwargs):
        # Reproduce an existing v3 pending plan written before byte freezing;
        # its descriptor/manifest contract must continue to work unchanged.
        kwargs.pop("freeze_bytes", None)
        return select_essential_backup_files(*args, **kwargs)

    def test_original_heads_code_and_money_restore_through_existing_public_api(self):
        with patch("urllib.request.urlopen", side_effect=AssertionError("No network")):
            saved = self.complete()
            manifest = verify_operational_backup(saved["folder"])
            restored = restore_operational_backup(saved["folder"], self.root.parent / "restored")
        self.assertEqual(manifest["kind"], parts.VERSION)
        self.assertTrue(restored["essential_files_complete"])
        self.assertEqual(restored["research_events"], 1)
        self.assertEqual(restored["financial_events"], 2)
        self.assertEqual(Path(restored["attachment_mapping"][0]["restored_path"]).read_bytes(), self.code.read_bytes())
        self.assertFalse(restored["operational_recovery_proved"])
        self.assertFalse(restored["independent_backup_proved"])

    def test_migrated_research_restores_originals_without_an_orphan_checkpoint_pointer(self):
        self.memory.put("published_history", "old-exposure", {"status": "published", "client_id": "default",
            "issue_id": "old-report", "concept_id": "fictional-concept", "revision": 1})
        # The fictional published row must have its required saved bytes too.
        # These are backup fixtures, not a valid or client-ready report.
        packet = self.root / "data/outputs/old-report/revision-1"
        for name in ("package-input.json", "package-status.json", "delivery-manifest.json", "final_report_data.json"):
            atomic_json(packet / name, {"fixture": "Synthetic backup attachment, not a client report."})
        atomic_bytes(packet / "shortlist.xlsx", b"Synthetic workbook bytes for backup restoration only.")
        expected = self.memory.snapshot()
        originals = {p.name: p.read_bytes() for p in (self.memory.root / "events").glob("*.json")}
        self.memory.migrate_segments()
        saved = self.complete("segmented-research")
        destination = self.root.parent / "segmented-research-restore"
        receipt = restore_operational_backup(saved["folder"], destination)
        restored = ResearchMemory(destination / "research", Journal(destination / "finance"), evidence_type="synthetic")
        self.assertEqual(restored.snapshot(), expected)
        self.assertEqual({p.name: p.read_bytes() for p in (restored.root / "events").glob("*.json")}, originals)
        self.assertFalse((restored.root / "research-checkpoint-v1.json").exists())
        self.assertFalse((restored.root / "research-segments-enabled-v1.json").exists())
        self.assertTrue(receipt["essential_files_complete"])
        restored.migrate_segments()
        self.assertEqual(restored.novelty_page("fictional-concept")["total_rows"], 1)

    def test_exact_pending_set_resumes_after_new_research_without_reselection(self):
        with patch.object(parts, "MAX_PART_FILES", 1):
            first = self.create(); self.assertIsNone(first["manifest"])
            self.memory.put("concepts", "later-product", {"description": {"label": "Must not join old backup"}})
            with patch("product_radar.operational_backup._checkpoint", side_effect=AssertionError("No new checkpoint")), \
                    patch("product_radar.operational_backup_selection.select_essential_backup_files", side_effect=AssertionError("No reselect")):
                result = self.complete()
        plan = load_json(Path(result["folder"]) / "pending.json", 65536)
        self.assertEqual(plan["research_checkpoint"]["sequence"], 1)
        restored = restore_operational_backup(result["folder"], self.root.parent / "frozen-restore")
        self.assertEqual(restored["research_events"], 1)

    def test_committed_part_before_cursor_loss_is_adopted_without_recopy(self):
        original = parts.atomic_json
        def lose_cursor(path, value):
            if Path(path).name == "cursor.json":
                raise OSError("Synthetic cursor write interruption")
            return original(path, value)
        with patch.object(parts, "MAX_PART_FILES", 1), patch.object(parts, "atomic_json", side_effect=lose_cursor):
            with self.assertRaises(OSError):
                self.create()
        with patch.object(parts, "MAX_PART_FILES", 1), patch.object(parts, "_copy_outcome", side_effect=AssertionError("No duplicate copy")):
            resumed = self.create()
        self.assertIsNone(resumed["manifest"])
        self.assertEqual(load_json(Path(resumed["folder"]) / "cursor.json", 16384)["next_index"], 1)
        self.complete()

    def test_unchanged_objects_are_shared_across_two_daily_sets(self):
        first = self.complete("set-one")
        payloads = {path.name for path in (self.destination / "objects/v3").glob("*.bin")}
        self.complete("set-two")
        self.assertEqual(payloads, {path.name for path in (self.destination / "objects/v3").glob("*.bin")})
        self.assertEqual(verify_operational_backup(first["folder"])["backup_id"], "set-one")

    def test_two_distinct_backups_cannot_interleave_in_one_empty_restore_target(self):
        first = self.complete("first-set")
        atomic_bytes(self.code, b"# distinct second saved code\n")
        second = self.complete("second-set")
        entered, release = threading.Event(), threading.Event()
        original = parts.verify_multipart_backup
        def verify(folder):
            if str(folder) == first["folder"]:
                entered.set()
                if not release.wait(5):
                    raise AssertionError("Test did not release original restore")
            return original(folder)
        target = self.root.parent / "one-owned-restore"
        with ThreadPoolExecutor(max_workers=2) as executor, patch.object(parts, "verify_multipart_backup", side_effect=verify):
            owner = executor.submit(parts.restore_multipart_backup, first["folder"], target)
            self.assertTrue(entered.wait(5))
            try:
                competing = executor.submit(parts.restore_multipart_backup, second["folder"], target)
                with self.assertRaisesRegex(SafetyError, "RUNNER_ALREADY_ACTIVE"):
                    competing.result(timeout=5)
            finally:
                release.set()
            proof = owner.result(timeout=5)
        copied = next(row for row in proof["attachment_mapping"] if row["original_path"] == str(self.code))
        self.assertEqual(Path(copied["restored_path"]).read_bytes(), b"# Synthetic offline code fixture.\n")
        with self.assertRaisesRegex(SafetyError, "RESTORE_DESTINATION_NOT_EMPTY"):
            parts.restore_multipart_backup(second["folder"], target)

    def test_missing_canonical_object_is_incomplete_not_limited_ready(self):
        saved = self.complete(); folder = Path(saved["folder"])
        plan = load_json(folder / "pending.json", 65536)
        rows, _ = parts._descriptors(self.destination, plan)
        canonical_row = rows[0]
        parts._object_path(self.destination, {"sha256": canonical_row["sha256"], "bytes": canonical_row["bytes"]}).unlink()
        with self.assertRaisesRegex(SafetyError, "BACKUP_FILE_UNAVAILABLE"):
            verify_operational_backup(folder)

    def test_legacy_unfrozen_attachment_loss_is_still_an_explicit_limited_copy(self):
        with patch.object(parts, "MAX_PART_FILES", 1):
            with patch("product_radar.operational_backup_selection.select_essential_backup_files",
                       side_effect=self.legacy_unfrozen_selection):
                self.create()
            self.code.unlink(); result = self.complete()
        self.assertEqual(result["state"], "records_verified_with_limitations")
        self.assertEqual(result["manifest"]["copy_failures"], 1)
        self.assertFalse(result["essential_files_complete"])
        restored = restore_operational_backup(result["folder"], self.root.parent / "limited")
        self.assertEqual(restored["attachment_mapping"], [])
        self.assertEqual(restored["research_events"], 1)

    def test_legacy_unfrozen_changed_original_cannot_silently_replace_frozen_bytes(self):
        with patch.object(parts, "MAX_PART_FILES", 1):
            with patch("product_radar.operational_backup_selection.select_essential_backup_files",
                       side_effect=self.legacy_unfrozen_selection):
                self.create()
            atomic_bytes(self.code, b"# changed later\n"); result = self.complete()
        self.assertEqual(result["manifest"]["copy_failures"], 1)

    def test_frozen_mutable_code_control_and_deleted_guide_restore_exact_original_bytes(self):
        guide = self.root / "docs/operations/implementation_2026-10-11.md"
        control = self.memory.root / "programme_service/control.json"
        atomic_bytes(guide, b"# Original public operating guide.\n")
        original_control = {"schema_version": 1, "enabled": True, "at": "2026-10-11T08:00:00+00:00"}
        atomic_json(control, {"control": original_control, "hash": digest(original_control)})
        originals = {str(path): path.read_bytes() for path in (self.code, guide, control)}
        with patch.object(parts, "MAX_PART_FILES", 1):
            first = self.create()
            self.assertIsNone(first["manifest"])
            original_plan = (Path(first["folder"]) / "pending.json").read_bytes()
            atomic_bytes(self.code, b"# New code must not replace original frozen bytes.\n")
            guide.unlink()
            changed_control = {**original_control, "enabled": False}
            atomic_json(control, {"control": changed_control, "hash": digest(changed_control)})
            with patch("product_radar.operational_backup._checkpoint", side_effect=AssertionError("No new checkpoint")), \
                    patch("product_radar.operational_backup_selection.select_essential_backup_files", side_effect=AssertionError("No reselect")):
                saved = self.complete()
        self.assertTrue(saved["essential_files_complete"])
        self.assertEqual(saved["manifest"]["copy_failures"], 0)
        self.assertEqual((Path(saved["folder"]) / "pending.json").read_bytes(), original_plan)
        restored = restore_operational_backup(saved["folder"], self.root.parent / "mutable-originals")
        self.assertEqual({row["original_path"]: Path(row["restored_path"]).read_bytes()
                          for row in restored["attachment_mapping"]}, originals)

    def test_freeze_sink_failure_does_not_publish_plan_or_change_research_or_money(self):
        research_before = self.memory.snapshot()
        with self.memory.journal.locked():
            money_before = self.memory.journal.export_bytes()
        folder = self.destination / "daily-v3-2026-10-11"
        for error, reason in ((SafetyError("BACKUP_LOCAL_DEVICE_FULL"), "BACKUP_LOCAL_DEVICE_FULL"),
                              (OSError("Synthetic private filesystem detail"), "LOCAL_BACKUP_FILE_FAILED")):
            with self.subTest(reason=reason), patch.object(parts, "_object_save", side_effect=error):
                failed = run_due_operational_backup(self.memory, root=self.root, settings=SETTINGS,
                                                    now="2026-10-11T08:00:00+00:00")
                self.assertEqual(failed["state"], "waiting")
                self.assertEqual(failed["reason"], reason)
                self.assertFalse((folder / "pending.json").exists() or (folder / "manifest.json").exists())
        self.assertEqual(self.memory.snapshot(), research_before)
        with self.memory.journal.locked():
            self.assertEqual(self.memory.journal.export_bytes(), money_before)
        saved = run_due_operational_backup(self.memory, root=self.root, settings=SETTINGS,
                                          now="2026-10-11T08:15:00+00:00")
        self.assertEqual(saved["state"], "saved")

    def test_freeze_sink_rejects_corrupt_existing_object_without_overwriting_or_ready_claim(self):
        raw = self.code.read_bytes()
        bad = parts._object_path(self.destination, {"sha256": sha256(raw).hexdigest(), "bytes": len(raw)})
        atomic_bytes(bad, b"Corrupt existing object fixture.")
        failed = run_due_operational_backup(self.memory, root=self.root, settings=SETTINGS,
                                            now="2026-10-11T08:00:00+00:00")
        folder = self.destination / "daily-v3-2026-10-11"
        self.assertEqual(failed["state"], "waiting")
        self.assertEqual(failed["reason"], "BACKUP_PART_OBJECT_CHANGED")
        self.assertFalse((folder / "pending.json").exists() or (folder / "manifest.json").exists())
        self.assertEqual(bad.read_bytes(), b"Corrupt existing object fixture.")
        self.assertEqual(self.code.read_bytes(), raw)

    def test_resealed_wrong_head_does_not_pass_canonical_replay(self):
        saved = self.complete(); folder = Path(saved["folder"])
        plan = load_json(folder / "pending.json", 65536)
        plan["finance_checkpoint"]["head"] = "f" * 64
        # This changes the immutable plan identity; the ready root/parts cannot
        # inherit a new financial checkpoint by resealing the small JSON alone.
        plan = parts._sealed({key: value for key, value in plan.items() if key != "hash"})
        atomic_json(folder / "pending.json", plan)
        with self.assertRaisesRegex(SafetyError, "MANIFEST_PLAN_CHANGED"):
            verify_operational_backup(folder)

    def test_daily_hook_continues_original_day_then_saves_next_day(self):
        with patch.object(parts, "MAX_PART_FILES", 1):
            first = run_due_operational_backup(self.memory, root=self.root, settings=SETTINGS, now="2026-10-08T12:00:00+00:00")
            self.assertEqual(first["reason"], "LOCAL_BACKUP_PARTS_IN_PROGRESS")
            second = run_due_operational_backup(self.memory, root=self.root, settings=SETTINGS, now="2026-10-09T12:00:00+00:00")
            third = run_due_operational_backup(self.memory, root=self.root, settings=SETTINGS, now="2026-10-09T12:15:00+00:00")
        self.assertEqual(second["day"], "2026-10-08")
        self.assertEqual(third["state"], "saved")
        self.assertEqual(third["day"], "2026-10-08")
        next_day = run_due_operational_backup(self.memory, root=self.root, settings=SETTINGS, now="2026-10-09T12:30:00+00:00")
        self.assertEqual(next_day["day"], "2026-10-09")
        self.assertEqual(next_day["state"], "saved")

    def test_daily_copy_error_keeps_pending_cursor_and_recovers_next_turn(self):
        with patch.object(parts, "_copy_outcome", side_effect=SafetyError("BACKUP_LOCAL_DEVICE_FULL")):
            failed = run_due_operational_backup(self.memory, root=self.root, settings=SETTINGS, now="2026-10-08T12:00:00+00:00")
        self.assertEqual(failed["state"], "waiting")
        self.assertEqual(failed["reason"], "BACKUP_LOCAL_DEVICE_FULL")
        with patch("product_radar.operational_backup._checkpoint", side_effect=AssertionError("No new checkpoint")):
            saved = run_due_operational_backup(self.memory, root=self.root, settings=SETTINGS, now="2026-10-08T12:15:00+00:00")
        self.assertEqual(saved["state"], "saved")

    def test_daily_completion_time_is_after_actual_work_not_invocation_start(self):
        started, completed = "2026-10-08T12:00:00+00:00", "2026-10-08T12:07:00+00:00"
        with patch("product_radar.operational_backup_schedule.utc_now", side_effect=(started, completed)):
            saved = run_due_operational_backup(self.memory, root=self.root, settings=SETTINGS)
        self.assertEqual(saved["state"], "saved")
        self.assertEqual(saved["completed_at"], completed)
        wrapper = load_json(self.root / "data/operations/backup_controls/multipart-active.json", 16384)
        self.assertEqual(wrapper["control"]["attempted_at"], started)
        self.assertEqual(wrapper["control"]["completed_at"], completed)

    def test_same_day_saved_status_preserves_recovery_limitations_without_recopy(self):
        first = run_due_operational_backup(self.memory, root=self.root, settings=SETTINGS,
            now="2026-10-08T12:00:00+00:00")
        with patch.object(parts, "create_multipart_operational_backup", side_effect=AssertionError("No recopy")):
            reused = run_due_operational_backup(self.memory, root=self.root, settings=SETTINGS,
                now="2026-10-08T12:15:00+00:00")
        self.assertTrue(reused["reused_saved_result"])
        self.assertEqual(reused["limitations"], first["limitations"])
        self.assertIn("drive", " ".join(reused["limitations"]))
        self.assertIn("Sign-in", " ".join(reused["limitations"]))

    def test_final_manifest_write_loss_retries_same_parts(self):
        original = parts.atomic_json
        def lose_manifest(path, value):
            if Path(path).name == "manifest.json":
                raise OSError("Synthetic ready save loss")
            return original(path, value)
        with patch.object(parts, "atomic_json", side_effect=lose_manifest):
            with self.assertRaises(OSError):
                self.create()
        with patch.object(parts, "_copy_outcome", side_effect=AssertionError("No duplicate copy")):
            saved = self.create()
        self.assertEqual(saved["state"], "essential_files_verified")

    def test_segmented_finance_additions_are_not_replaced_by_old_legacy_prefix(self):
        with self.memory.journal.locked():
            self.memory.journal.migrate_segments()
            legacy = self.memory.journal.path.read_bytes()
            self.memory.journal.setup_budget("fictional-account:later-period", "4")
            canonical_export = self.memory.journal.export_bytes()
        saved = self.complete()
        restored = restore_operational_backup(saved["folder"], self.root.parent / "finance-restore")
        self.assertEqual(restored["financial_events"], 4)
        self.assertNotEqual(legacy, canonical_export)
        self.assertEqual((self.root.parent / "finance-restore/finance/events.jsonl").read_bytes(), canonical_export)

    def test_actual_finance_above_eight_mib_chunks_restores_all_original_money_and_new_tail(self):
        events, head = [], "0" * 64
        for number in range(24000):
            body = {"version": 1, "seq": number + 1, "event_id": f"scale-backup-budget-{number}",
                "at": "2026-10-09T00:00:00+00:00", "kind": "budget", "data": {
                    "namespace": f"synthetic-account-{number:05d}:synthetic-period-{number:05d}",
                    "allowance": "5", "production_provision": "0", "emergency": "0"}, "previous": head}
            event = {**body, "hash": digest(body)}; events.append(event); head = event["hash"]
        original = b"".join(canonical(row) + b"\n" for row in events)
        self.assertGreater(len(original), MAX_JOURNAL)
        atomic_bytes(self.memory.journal.path, original)
        with self.memory.journal.locked():
            self.memory.journal.migrate_segments()
            self.memory.journal.setup_budget("synthetic-later:synthetic-period", "4")
            expected = self.memory.journal.export_bytes()
        saved = self.complete()
        plan = load_json(Path(saved["folder"]) / "pending.json", 65536)
        self.assertGreater(plan["finance_chunks"], 1)
        self.assertEqual(plan["finance_checkpoint"]["events"], 24002)
        proof = restore_operational_backup(saved["folder"], self.root.parent / "large-finance-restored")
        self.assertEqual(proof["financial_events"], 24002)
        restored = Journal(self.root.parent / "large-finance-restored/finance")
        with restored.locked():
            self.assertEqual(restored.export_bytes(), expected)
            self.assertEqual(len(restored.state()[0]), 24001)
        self.assertEqual(self.memory.journal.path.read_bytes(), original)

    def test_page_rollover_keeps_small_roots_and_all_actual_files(self):
        for number in range(12):
            atomic_bytes(self.root / f"src/product_radar/extra_{number}.py", b"# offline fixture\n")
        with patch.object(parts, "MAX_PAGE_ROWS", 3), patch.object(parts, "MAX_PART_FILES", 3):
            saved = self.complete()
            manifest = verify_operational_backup(saved["folder"])
        self.assertGreater(manifest["parts"], 3)
        self.assertLess((Path(saved["folder"]) / "pending.json").stat().st_size, 16384)
        self.assertLess((Path(saved["folder"]) / "manifest.json").stat().st_size, 4096)
        self.assertEqual(len(restore_operational_backup(saved["folder"], self.root.parent / "paged")["attachment_mapping"]), 13)

    def test_real_selection_and_backup_exceed_original_512_mib_without_changing_old_limits(self):
        # Actual bytes, not a lowered mock boundary. Distinct public-image
        # fixture bytes exercise the real selector and shared object store.
        for number in range(9):
            raw = bytes([65 + number]) * (60 * 1024 * 1024)
            path = self.root / f"media/live/product-images/large-fixture-{number}.jpg"
            atomic_bytes(path, raw)
            self.memory.put("source_checks", f"large-image-{number}", {"kind": "public_product_image", "concept_id": "fictional-concept",
                "image": {"path": str(path), "sha256": sha256(raw).hexdigest()}})
        with self.memory.journal.locked():
            finances = self.memory.journal.state()
        old = select_essential_backup_files(self.memory.snapshot(), finances, root=self.root)
        self.assertTrue(any(row["reason"] == "BACKUP_SELECTION_TOTAL_SIZE_LIMIT" for row in old["unsafe"]))
        saved = self.complete()
        self.assertGreater(saved["manifest"]["bytes"], 512 * 1024 * 1024)
        self.assertGreater(saved["manifest"]["parts"], 1)
        self.assertTrue(saved["essential_files_complete"])
        verify_operational_backup(saved["folder"])


if __name__ == "__main__":
    unittest.main()
