"""Daily backup copies stay within the configured numbers; nothing unsafe goes.

Real resumable backups of a synthetic store in a temporary project folder; no
website, provider or model, and never the owner's real backups folder.
"""

from datetime import date, timedelta
import json
import os
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import patch

from product_radar import backup_retention as retention
from product_radar import operational_backup_parts as parts
from product_radar.models import SafetyError
from product_radar.operational_backup_schedule import run_due_operational_backup
from product_radar.research_memory import ResearchMemory
from product_radar.models import digest
from product_radar.storage import Journal, atomic_bytes, atomic_json, load_json

SETTINGS = {"schema_version": 2, "enabled": True, "destination": "data/backups", "timezone": "Asia/Kolkata",
            "backup_contract": parts.VERSION}
DURABILITY = {"schema_version": 1, "kind": retention.SETTINGS_VERSION, "backup_keep_daily": 2, "backup_keep_weekly": 0,
              "backup_minimum_free_gib": 1, "run_log_keep_days": 90, "research_object_grace_hours": 24}


class KeepDaysTests(unittest.TestCase):
    def test_newest_days_plus_newest_of_each_recent_week(self):
        days = [date(2026, 10, 16) - timedelta(days=number) for number in range(80)]
        kept = retention.keep_days(days, keep_daily=14, keep_weekly=8)
        self.assertEqual(sorted(kept)[-14:], sorted(days[:14]))
        sundays = {day for day in kept if day not in days[:14]}
        self.assertTrue(all(day.isoweekday() == 7 for day in sundays))
        self.assertEqual(len(kept), 14 + len(sundays))
        self.assertLessEqual(len(kept), 22)
        # Eight calendar weeks counting the current one: its newest day plus seven Sundays.
        self.assertEqual(min(kept), date(2026, 8, 30))

    def test_edges_year_boundary_sparse_and_empty(self):
        self.assertEqual(retention.keep_days([], keep_daily=14, keep_weekly=8), set())
        newest = date(2027, 1, 2)
        sparse = [newest, date(2026, 12, 30), date(2026, 11, 1), date(2025, 6, 1)]
        self.assertEqual(retention.keep_days(sparse, keep_daily=1, keep_weekly=0), {newest})
        self.assertEqual(retention.keep_days(sparse, keep_daily=1, keep_weekly=2), {newest, date(2026, 11, 1)})
        self.assertEqual(retention.keep_days(sparse, keep_daily=0, keep_weekly=0), {newest})  # the newest always stays


class SettingsTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="durability-settings-")
        self.addCleanup(temporary.cleanup); self.root = Path(temporary.name)

    def test_checked_in_settings_are_valid(self):
        checked_in = Path(__file__).resolve().parents[1]
        value = retention.load_settings(checked_in)
        self.assertEqual((value["backup_keep_daily"], value["backup_keep_weekly"], value["backup_minimum_free_gib"]),
                         (14, 8, 10))

    def test_missing_invalid_and_floor_fallback(self):
        with self.assertRaisesRegex(SafetyError, "DURABILITY_SETTINGS_MISSING"):
            retention.load_settings(self.root)
        self.assertEqual(retention.minimum_free_bytes(self.root), 10 * 1024 ** 3)
        path = self.root / retention.SETTINGS
        path.parent.mkdir(parents=True)
        for broken in (dict(DURABILITY, backup_keep_daily=0), dict(DURABILITY, extra=1), dict(DURABILITY, kind="x"),
                       dict(DURABILITY, run_log_keep_days=True)):
            path.write_text(json.dumps(broken))
            with self.assertRaisesRegex(SafetyError, "DURABILITY_SETTINGS_INVALID"):
                retention.load_settings(self.root)
        path.write_text("not json")
        with self.assertRaisesRegex(SafetyError, "DURABILITY_SETTINGS_INVALID"):
            retention.load_settings(self.root)
        path.write_text(json.dumps(dict(DURABILITY, backup_minimum_free_gib=3)))
        self.assertEqual(retention.minimum_free_bytes(self.root), 3 * 1024 ** 3)

    def test_disk_space_reason(self):
        with patch.object(retention, "free_bytes", return_value=5 * 1024 ** 3):
            self.assertEqual(retention.disk_space_reason(self.root / "absent/backups", self.root),
                             "LOCAL_BACKUP_DISK_SPACE_LOW")
        with patch.object(retention, "free_bytes", return_value=50 * 1024 ** 3):
            self.assertIsNone(retention.disk_space_reason(self.root, self.root))
        with patch.object(retention, "free_bytes", side_effect=OSError("Synthetic unmeasurable drive")):
            self.assertIsNone(retention.disk_space_reason(self.root, self.root))
        self.assertGreater(retention.free_bytes(self.root / "not/yet/created"), 0)


class BackupPruneTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="backup-retention-")
        self.addCleanup(temporary.cleanup); self.root = Path(temporary.name) / "project"
        self.memory = ResearchMemory(self.root / "data/research/captured", Journal(self.root / "data/benchmarks/state/apify"),
            evidence_type="synthetic")
        self.memory.put("concepts", "fictional-concept", {"description": {"label": "Fictional product"}})
        with self.memory.journal.locked():
            self.memory.journal.setup_budget("fictional-account:period", "5")
        self.code = self.root / "src/product_radar/fictional.py"
        self.destination = self.root / "data/backups"
        self.objects = self.destination / "objects/v3"

    def daily(self, day, *, change=True):
        """One finished daily copy; the code file changes so each copy has its own bytes."""
        if change:
            atomic_bytes(self.code, ("# Synthetic offline code fixture for %s.\n" % day).encode())
            self.memory.put("concepts", "concept-" + day, {"description": {"label": "Fictional " + day}})
        for _ in range(32):
            value = parts.create_multipart_operational_backup(self.memory, self.destination, backup_id="daily-v3-" + day,
                root=self.root)
            if value["manifest"] is not None:
                return value
        self.fail("The synthetic backup did not finish.")

    def prune(self, newest, **changes):
        return retention.prune_backups(self.destination, dict(DURABILITY, **changes), verified_backup_id=newest,
                                       now="2026-10-16T12:00:00+00:00")

    def names(self):
        return sorted(path.name for path in self.destination.iterdir())

    def test_old_copies_and_their_own_files_go_and_kept_copies_still_verify(self):
        for day in ("2026-10-12", "2026-10-13", "2026-10-14", "2026-10-15"):
            self.daily(day)
        before = {path.name for path in self.objects.iterdir()}
        record = self.prune("daily-v3-2026-10-15")
        self.assertEqual(record["state"], "removed")
        self.assertEqual(record["kept"], ["daily-v3-2026-10-15", "daily-v3-2026-10-14"])
        self.assertEqual(record["removed"], ["daily-v3-2026-10-12", "daily-v3-2026-10-13"])
        self.assertEqual(self.names(), ["daily-v3-2026-10-14", "daily-v3-2026-10-15", "multipart-control", "objects"])
        after = {path.name for path in self.objects.iterdir()}
        self.assertTrue(after < before)
        self.assertEqual(record["removed_objects"], len(before - after))
        self.assertGreater(record["removed_bytes"], 0)
        for day in ("2026-10-14", "2026-10-15"):
            self.assertEqual(parts.verify_multipart_backup(self.destination / ("daily-v3-" + day))["backup_id"],
                             "daily-v3-" + day)
        again = self.prune("daily-v3-2026-10-15")
        self.assertEqual((again["state"], again["removed"], again["removed_objects"]), ("nothing_to_remove", [], 0))

    def test_unfinished_set_and_other_named_sets_keep_their_files(self):
        self.daily("2026-10-13"); self.daily("2026-10-14"); self.daily("2026-10-15")
        manual = parts.create_multipart_operational_backup(self.memory, self.destination, backup_id="manual-copy",
                                                           root=self.root)
        self.assertIsNotNone(manual["manifest"])
        atomic_bytes(self.code, b"# Synthetic changed bytes for an unfinished copy.\n")
        with patch.object(parts, "MAX_PART_FILES", 1):
            unfinished = parts.create_multipart_operational_backup(self.memory, self.destination,
                backup_id="daily-v3-2026-10-16", root=self.root)
        self.assertIsNone(unfinished["manifest"])
        record = self.prune("daily-v3-2026-10-15", backup_keep_daily=1)
        self.assertEqual(record["removed"], ["daily-v3-2026-10-13", "daily-v3-2026-10-14"])
        self.assertTrue((self.destination / "daily-v3-2026-10-16/pending.json").exists())
        self.assertEqual(parts.verify_multipart_backup(self.destination / "manual-copy")["backup_id"], "manual-copy")
        finished = parts.create_multipart_operational_backup(self.memory, self.destination,
            backup_id="daily-v3-2026-10-16", root=self.root)
        while finished["manifest"] is None:
            finished = parts.create_multipart_operational_backup(self.memory, self.destination,
                backup_id="daily-v3-2026-10-16", root=self.root)
        self.assertEqual(parts.verify_multipart_backup(self.destination / "daily-v3-2026-10-16")["backup_id"],
                         "daily-v3-2026-10-16")

    def test_nothing_goes_while_a_backup_holds_the_lock_or_newest_is_not_the_verified_one(self):
        self.daily("2026-10-13"); self.daily("2026-10-14"); self.daily("2026-10-15")
        with Journal(self.destination / "multipart-control").locked():
            busy = self.prune("daily-v3-2026-10-15", backup_keep_daily=1)
        self.assertEqual((busy["state"], busy["reason"]), ("kept", "BACKUP_PRUNE_BACKUP_RUNNING"))
        older = self.prune("daily-v3-2026-10-14", backup_keep_daily=1)
        self.assertEqual((older["state"], older["reason"]), ("kept", "BACKUP_PRUNE_NEWEST_NOT_VERIFIED"))
        self.assertEqual(len([name for name in self.names() if name.startswith("daily-v3-")]), 3)
        empty = retention.prune_backups(self.root / "data/empty-backups", DURABILITY,
                                        verified_backup_id="daily-v3-2026-10-15")
        self.assertEqual(empty["reason"], "BACKUP_PRUNE_NEWEST_NOT_VERIFIED")
        self.assertFalse((self.root / "data/empty-backups").exists())

    def test_damaged_kept_copy_stops_all_removal(self):
        self.daily("2026-10-13"); self.daily("2026-10-14"); self.daily("2026-10-15")
        before = {path.name for path in self.objects.iterdir()}
        plan = load_json(self.destination / "daily-v3-2026-10-14/pending.json", 65536)
        rows = list(parts._pages(self.destination, plan["descriptors"], contract="backup-descriptor-page-v3"))
        (self.objects / (rows[0]["sha256"] + ".bin")).unlink()  # a research event file of a kept copy
        lost = before - {rows[0]["sha256"] + ".bin"}
        record = self.prune("daily-v3-2026-10-15")
        self.assertEqual((record["state"], record["reason"]), ("kept", "BACKUP_PRUNE_KEPT_SET_DAMAGED"))
        self.assertEqual({path.name for path in self.objects.iterdir()}, lost)
        self.assertIn("daily-v3-2026-10-13", self.names())
        (self.destination / "daily-v3-2026-10-14/manifest.json").write_text("{}")
        record = self.prune("daily-v3-2026-10-15")
        self.assertEqual(record["state"], "kept")
        self.assertIn("daily-v3-2026-10-13", self.names())

    def test_tampered_sealed_records_stop_removal(self):
        self.daily("2026-10-13"); self.daily("2026-10-14"); self.daily("2026-10-15")
        (self.destination / "daily-v3-2026-02-30").mkdir()  # not a real day: left alone
        (self.destination / "notes").mkdir()
        kept = self.destination / "daily-v3-2026-10-14"
        cursor = load_json(kept / "cursor.json", 16384)
        original_cursor = (kept / "cursor.json").read_bytes()
        changed = {key: value for key, value in cursor.items() if key != "hash"}
        changed["plan_hash"] = "0" * 64
        atomic_json(kept / "cursor.json", dict(changed, hash=digest(changed)))
        self.assertEqual(self.prune("daily-v3-2026-10-15")["reason"], "BACKUP_PART_CURSOR_INVALID")
        (kept / "cursor.json").write_bytes(original_cursor)
        manifest = load_json(kept / "manifest.json", 65536)
        original_manifest = (kept / "manifest.json").read_bytes()
        changed = {key: value for key, value in manifest.items() if key != "hash"}
        changed["parts"] += 1
        atomic_json(kept / "manifest.json", dict(changed, hash=digest(changed)))
        self.assertEqual(self.prune("daily-v3-2026-10-15")["reason"], "BACKUP_PART_MANIFEST_INVALID")
        (kept / "manifest.json").write_bytes(original_manifest)
        with patch.object(retention, "free_bytes", side_effect=OSError("Synthetic unmeasurable drive")):
            record = self.prune("daily-v3-2026-10-15")
        self.assertEqual((record["state"], record["free_bytes_before"], record["free_bytes_after"]), ("removed", None, None))
        self.assertTrue((self.destination / "daily-v3-2026-02-30").is_dir())
        self.assertTrue((self.destination / "notes").is_dir())

    def test_unknown_files_are_never_removed_and_an_interrupted_removal_finishes(self):
        self.daily("2026-10-13"); self.daily("2026-10-14"); self.daily("2026-10-15")
        (self.destination / "daily-v3-2026-10-13/owner-note.txt").write_text("Synthetic owner file.")
        stray = self.objects / "not-a-backup-object.txt"; stray.write_text("Synthetic stray file.")
        record = self.prune("daily-v3-2026-10-15")
        self.assertEqual(record["reason"], "BACKUP_PRUNE_UNKNOWN_FILES_KEPT")
        self.assertTrue((self.destination / "pruning-daily-v3-2026-10-13/owner-note.txt").exists())
        self.assertTrue(stray.exists())
        (self.destination / "pruning-daily-v3-2026-10-13/owner-note.txt").unlink()
        record = self.prune("daily-v3-2026-10-15")
        self.assertIsNone(record["reason"])
        self.assertNotIn("pruning-daily-v3-2026-10-13", self.names())

    def test_stale_temporary_files_and_orphans_are_swept(self):
        self.daily("2026-10-14"); self.daily("2026-10-15")
        orphan = self.objects / ("ab" * 32 + ".bin"); orphan.write_bytes(b"Synthetic orphan bytes.")
        stale = self.objects / ".save-abc123.tmp"; stale.write_bytes(b"x")
        fresh = self.objects / ".save-def456.tmp"; fresh.write_bytes(b"y")
        old = time.time() - 3 * 86400
        os.utime(stale, (old, old))
        record = self.prune("daily-v3-2026-10-15")
        self.assertEqual(record["removed_objects"], 2)
        self.assertFalse(orphan.exists() or stale.exists())
        self.assertTrue(fresh.exists())

    def test_daily_flow_skips_on_low_disk_and_tidies_after_a_saved_copy(self):
        (self.root / retention.SETTINGS).parent.mkdir(parents=True, exist_ok=True)
        (self.root / retention.SETTINGS).write_text(json.dumps(dict(DURABILITY, backup_keep_daily=1)))
        with patch.object(retention, "free_bytes", return_value=0):
            low = run_due_operational_backup(self.memory, root=self.root, settings=SETTINGS, now="2026-10-08T12:00:00+00:00")
        self.assertEqual((low["state"], low["reason"]), ("waiting", "LOCAL_BACKUP_DISK_SPACE_LOW"))
        self.assertFalse((self.root / "data/operations/backup_controls/multipart-active.json").exists())
        self.assertFalse(self.destination.exists())
        first = run_due_operational_backup(self.memory, root=self.root, settings=SETTINGS, now="2026-10-08T12:00:00+00:00")
        self.assertEqual(first["state"], "saved")
        atomic_bytes(self.code, b"# Synthetic next-day bytes.\n")
        second = run_due_operational_backup(self.memory, root=self.root, settings=SETTINGS, now="2026-10-09T12:00:00+00:00")
        self.assertEqual(second["state"], "saved")
        self.assertNotIn("daily-v3-2026-10-08", self.names())
        history = load_json(self.root / "data/operations/backup_controls" / retention.HISTORY_NAME, 1 << 20)
        records = history["value"]["records"]
        # The full drive's clean-up ran first, with nothing saved yet to keep.
        self.assertEqual((records[0]["trigger"], records[0]["reason"], records[0]["disk_low"]),
                         ("low_disk", "BACKUP_PRUNE_NEWEST_NOT_VERIFIED", True))
        self.assertEqual([row["removed"] for row in records[1:]], [[], ["daily-v3-2026-10-08"]])
        # The first copy in a brand-new backups folder: kept, nothing to remove.
        self.assertEqual((records[1]["state"], records[1]["kept"], records[1]["removed_objects"]),
                         ("nothing_to_remove", ["daily-v3-2026-10-08"], 0))
        self.assertEqual(records[2]["kept"], ["daily-v3-2026-10-09"])
        with patch.object(retention, "free_bytes", return_value=0):
            reused = run_due_operational_backup(self.memory, root=self.root, settings=SETTINGS,
                                                now="2026-10-09T13:00:00+00:00")
        self.assertEqual(reused["state"], "saved")  # a finished day is reported, not re-checked

    def test_a_full_drive_still_tidies_against_the_newest_finished_copy_once_a_day(self):
        (self.root / retention.SETTINGS).parent.mkdir(parents=True, exist_ok=True)
        (self.root / retention.SETTINGS).write_text(json.dumps(dict(DURABILITY, backup_keep_daily=1)))
        self.daily("2026-10-08"); self.daily("2026-10-09")
        control = self.root / "data/operations/backup_controls"
        with patch.object(retention, "free_bytes", return_value=0):
            low = run_due_operational_backup(self.memory, root=self.root, settings=SETTINGS, now="2026-10-10T12:00:00+00:00")
            again = run_due_operational_backup(self.memory, root=self.root, settings=SETTINGS, now="2026-10-10T13:00:00+00:00")
        self.assertEqual((low["reason"], again["reason"]), ("LOCAL_BACKUP_DISK_SPACE_LOW",) * 2)
        self.assertNotIn("daily-v3-2026-10-08", self.names())
        self.assertNotIn("daily-v3-2026-10-10", self.names())
        records = load_json(control / retention.HISTORY_NAME, 1 << 20)["value"]["records"]
        self.assertEqual(len(records), 1)  # once a day
        self.assertEqual((records[0]["trigger"], records[0]["removed"], records[0]["verified_backup_id"],
                          records[0]["minimum_free_bytes"]),
                         ("low_disk", ["daily-v3-2026-10-08"], "daily-v3-2026-10-09", 1024 ** 3))
        self.assertEqual(retention.latest_record(control)["trigger"], "low_disk")
        self.assertIsNone(retention.tidy_when_disk_low(self.destination, root=self.root, control_root=control,
                                                       now="2026-10-10T14:00:00+00:00"))

    def test_the_newest_complete_copy_is_kept_beyond_the_numbers(self):
        self.daily("2026-10-13"); self.daily("2026-10-14"); self.daily("2026-10-15")
        self.assertTrue(retention._complete(self.destination / "daily-v3-2026-10-14"))
        with patch.object(retention, "_complete", side_effect=lambda folder: folder.name == "daily-v3-2026-10-13"):
            record = self.prune("daily-v3-2026-10-15", backup_keep_daily=1)
        self.assertEqual(record["kept"], ["daily-v3-2026-10-15", "daily-v3-2026-10-13"])
        self.assertEqual(record["removed"], ["daily-v3-2026-10-14"])
        (self.destination / "daily-v3-2026-10-13/manifest.json").write_text("{}")
        self.assertFalse(retention._complete(self.destination / "daily-v3-2026-10-13"))

    def test_a_damaged_copy_past_retention_is_removed_while_kept_copies_verify(self):
        self.daily("2026-10-13"); self.daily("2026-10-14"); self.daily("2026-10-15")
        (self.destination / "daily-v3-2026-10-13/pending.json").write_text("{}")
        record = self.prune("daily-v3-2026-10-15")
        self.assertEqual((record["state"], record["removed"], record["removed_damaged"]),
                         ("removed", [], ["daily-v3-2026-10-13"]))
        self.assertNotIn("daily-v3-2026-10-13", self.names())
        for day in ("2026-10-14", "2026-10-15"):
            self.assertEqual(parts.verify_multipart_backup(self.destination / ("daily-v3-" + day))["backup_id"],
                             "daily-v3-" + day)

    def test_unfinished_copy_waits_on_low_disk_with_its_own_day(self):
        with patch.object(parts, "MAX_PART_FILES", 1):
            started = run_due_operational_backup(self.memory, root=self.root, settings=SETTINGS,
                                                 now="2026-10-08T12:00:00+00:00")
        self.assertEqual(started["reason"], "LOCAL_BACKUP_PARTS_IN_PROGRESS")
        with patch.object(retention, "free_bytes", return_value=0):
            low = run_due_operational_backup(self.memory, root=self.root, settings=SETTINGS,
                                             now="2026-10-09T12:00:00+00:00")
        self.assertEqual((low["state"], low["reason"], low["day"], low["backup_id"]),
                         ("waiting", "LOCAL_BACKUP_DISK_SPACE_LOW", "2026-10-08", "daily-v3-2026-10-08"))

    def test_tidy_never_raises_and_records_history(self):
        control = self.root / "data/operations/backup_controls"
        missing = retention.tidy_after_backup(self.destination, root=self.root, control_root=control,
                                              verified_backup_id="daily-v3-2026-10-15")
        self.assertEqual(missing["reason"], "DURABILITY_SETTINGS_MISSING")
        (self.root / retention.SETTINGS).parent.mkdir(parents=True, exist_ok=True)
        (self.root / retention.SETTINGS).write_text(json.dumps(DURABILITY))
        with patch.object(retention, "prune_backups", side_effect=OSError("Synthetic drive error")):
            failed = retention.tidy_after_backup(self.destination, root=self.root, control_root=control,
                                                 verified_backup_id="daily-v3-2026-10-15")
        self.assertEqual((failed["state"], failed["reason"]), ("failed", "BACKUP_PRUNE_FAILED"))
        (control / retention.HISTORY_NAME).write_text("damaged")
        for _ in range(retention.MAX_HISTORY + 3):
            value = retention.save_history(control, {"state": "kept"})
        self.assertEqual(len(value["records"]), retention.MAX_HISTORY)
        with patch.object(retention, "save_history", side_effect=OSError("Synthetic full drive")):
            self.assertEqual(retention.tidy_after_backup(self.destination, root=self.root, control_root=control,
                verified_backup_id="daily-v3-2026-10-15")["reason"], "BACKUP_PRUNE_NEWEST_NOT_VERIFIED")

    def test_reference_walk_refuses_cycles_and_count_changes(self):
        self.daily("2026-10-15")
        plan = load_json(self.destination / "daily-v3-2026-10-15/pending.json", 65536)
        refs = retention._References(self.destination)
        with self.assertRaisesRegex(SafetyError, "BACKUP_PART_PAGE_COUNT_CHANGED"):
            refs.chain(dict(plan["descriptors"], rows=plan["descriptors"]["rows"] + 1), "backup-descriptor-page-v3", [])
        with self.assertRaisesRegex(SafetyError, "BACKUP_PART_PAGE_ROOT_INVALID"):
            refs.chain({"last": None}, "backup-descriptor-page-v3", [])
        with self.assertRaisesRegex(SafetyError, "BACKUP_PART_PAGE_INVALID"):
            retention._References(self.destination).chain(plan["descriptors"], "backup-outcome-page-v3", [])
        with self.assertRaisesRegex(SafetyError, "BACKUP_PART_REFERENCE_INVALID"):
            refs.binary("not-a-hash", 1)


if __name__ == "__main__":
    unittest.main()
