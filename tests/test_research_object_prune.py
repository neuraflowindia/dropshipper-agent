"""Unused research lookup pages go; a cold start replays history only once.

Synthetic research in temporary folders; no website, provider or model.
"""

import json
import os
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import patch

from product_radar import research_object_prune as prune
from product_radar import research_segments as segments
from product_radar import runtime_storage
from product_radar import store_housekeeping as housekeeping
from product_radar.models import SafetyError, timestamp
from product_radar.research_memory import ResearchMemory
from product_radar.storage import Journal, atomic_json, load_json


DURABILITY = {"schema_version": 1, "kind": "long-run-durability-v1", "backup_keep_daily": 14, "backup_keep_weekly": 8,
              "backup_minimum_free_gib": 10, "run_log_keep_days": 90, "research_object_grace_hours": 1}


class _Store:
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="research-object-prune-")
        self.addCleanup(temporary.cleanup)
        self.addCleanup(runtime_storage._configured.cache_clear)
        self.root = Path(temporary.name)
        self.memory = self.fresh()

    def fresh(self):
        return ResearchMemory(self.root / "data/research/captured", Journal(self.root / "data/benchmarks/state/apify"),
                              evidence_type="synthetic")

    def objects(self):
        return self.memory.root / "research-v1/objects"

    def age_all(self, seconds=7200):
        moment = time.time() - seconds
        for path in self.objects().iterdir():
            os.utime(path, (moment, moment))

    def grow(self, count, prefix="concept"):
        for number in range(count):
            self.memory.put("concepts", "%s-%d" % (prefix, number), {"label": "Synthetic %s %d" % (prefix, number)})
        self.memory.put("published_history", prefix + "-shown", {"status": "published", "concept_id": prefix + "-0",
            "issue_id": "synthetic-issue", "client_id": "default"})


class ResearchObjectPruneTests(_Store, unittest.TestCase):
    def test_unreachable_old_pages_go_and_a_cold_reader_still_accepts_the_checkpoint(self):
        self.grow(30); self.memory.migrate_segments()
        self.grow(30, "second"); self.grow(5, "third")
        expected = self.memory.snapshot()
        self.age_all()
        before = len(list(self.objects().iterdir()))
        result = prune.prune_research_objects(self.memory, grace_seconds=3600)
        self.assertEqual(result["state"], "removed")
        self.assertEqual(len(list(self.objects().iterdir())), before - result["removed_objects"])
        self.assertEqual(result["live_objects"], len(list(self.objects().iterdir())))
        cold = self.fresh()
        self.assertEqual(cold.snapshot(), expected)
        self.assertIsNone(cold.last_checkpoint_diagnostic)
        self.assertIsNotNone(cold._research_pages_verified)
        self.assertEqual(cold.read_records("concepts", ["second-3"])["records"]["second-3"]["label"], "Synthetic second 3")
        self.assertTrue(cold.read_records("concepts", ["second-3"])["checkpoint_used"])
        self.assertEqual(cold.novelty_page("third-0")["total_rows"], 1)
        # The writer keeps publishing after the pruning.
        self.memory.put("concepts", "after-prune", {"label": "Synthetic after"})
        self.assertIsNone(self.memory.last_commit_diagnostic)
        self.assertEqual(self.fresh().read_records("concepts", ["after-prune"])["records"]["after-prune"],
                         {"label": "Synthetic after"})
        # Its replaced (aged) pages go next time; the new checkpoint still reads.
        self.assertEqual(prune.prune_research_objects(self.memory, grace_seconds=3600)["state"], "removed")
        again = self.fresh()
        self.assertEqual(again.read_records("concepts", ["after-prune"])["records"]["after-prune"],
                         {"label": "Synthetic after"})
        self.assertIsNone(again.last_checkpoint_diagnostic)
        self.assertEqual(prune.prune_research_objects(self.memory, grace_seconds=3600)["removed_objects"], 0)

    def test_large_tables_with_branch_pages_keep_every_reachable_page(self):
        self.grow(150); self.memory.migrate_segments()
        pointer = load_json(self.memory.root / segments.POINTER)
        checkpoint = segments._read(self.memory, pointer["checkpoint"])
        root = segments._read(self.memory, checkpoint["tables"]["concepts"]["root"])
        self.assertEqual(root["contract"], "research-branch-v1")
        self.grow(150, "second"); self.age_all()
        expected = self.memory.snapshot()
        self.assertEqual(prune.prune_research_objects(self.memory, grace_seconds=3600)["state"], "removed")
        cold = self.fresh()
        self.assertEqual(cold.snapshot(), expected)
        self.assertIsNone(cold.last_checkpoint_diagnostic)
        self.assertEqual(cold.read_table_page("concepts", limit=128)["total_rows"], 300)

    def test_files_that_vanish_or_change_before_removal_are_left_alone(self):
        self.grow(10); self.memory.migrate_segments(); self.grow(10, "second"); self.age_all()
        live = prune.live_object_names(self.memory, load_json(self.memory.root / segments.POINTER))
        unreachable = sorted(path for path in self.objects().iterdir() if path.name not in live)
        self.assertGreaterEqual(len(unreachable), 2)
        gone, touched = unreachable[0], unreachable[1]
        original = self.memory.journal.locked
        from contextlib import contextmanager
        @contextmanager
        def meddling(timeout=0):
            gone.unlink(); os.utime(touched, None)
            with original(timeout=timeout):
                yield
        with patch.object(self.memory.journal, "locked", side_effect=meddling):
            result = prune.prune_research_objects(self.memory, grace_seconds=3600)
        self.assertEqual(result["removed_objects"], len(unreachable) - 2)
        self.assertTrue(touched.exists())

    def test_a_new_segment_removed_before_publication_is_never_published(self):
        self.grow(5); self.memory.migrate_segments()
        before = load_json(self.memory.root / segments.POINTER)
        written, original_segments = [], segments._segments
        def remember(memory, start, stop, previous):
            last = original_segments(memory, start, stop, previous)
            written.append(last)
            return last
        original_lock = self.memory.journal.locked
        from contextlib import contextmanager
        @contextmanager
        def pruned_meanwhile(timeout=0):
            if written:  # the next lock after new segments: the pointer publication
                (self.objects() / (written.pop()["sha256"] + ".json")).unlink()
            with original_lock(timeout=timeout):
                yield
        with patch.object(segments, "_segments", side_effect=remember), \
                patch.object(self.memory.journal, "locked", side_effect=pruned_meanwhile):
            self.memory.put("concepts", "during-prune", {"label": "Synthetic during"})
        self.assertEqual(self.memory.last_commit_diagnostic, "SAVED_FILE_UNAVAILABLE")
        self.assertEqual(load_json(self.memory.root / segments.POINTER), before)
        # Removed before the read-back: refused too, nothing published.
        def removed_at_once(memory, start, stop, previous):
            last = original_segments(memory, start, stop, previous)
            (self.objects() / (last["sha256"] + ".json")).unlink()
            return last
        with patch.object(segments, "_segments", side_effect=removed_at_once):
            self.memory.put("concepts", "during-prune-2", {"label": "Synthetic during 2"})
        self.assertIsNotNone(self.memory.last_commit_diagnostic)
        self.assertEqual(load_json(self.memory.root / segments.POINTER), before)
        # The next save writes the segment again and publishes a readable pointer.
        self.memory.put("concepts", "after-prune", {"label": "Synthetic after"})
        self.assertIsNone(self.memory.last_commit_diagnostic)
        cold = self.fresh()
        self.assertEqual(cold.read_records("concepts", ["during-prune"])["records"]["during-prune"]["label"],
                         "Synthetic during")
        self.assertIsNone(cold.last_checkpoint_diagnostic)
        self.assertIsNotNone(cold._research_pages_verified)

    def test_recent_unreachable_pages_wait_for_the_grace_period(self):
        self.grow(10); self.memory.migrate_segments(); self.grow(10, "second")
        result = prune.prune_research_objects(self.memory, grace_seconds=3600)
        self.assertEqual(result["removed_objects"], 0)
        self.assertGreater(result["kept_unreachable_recent"], 0)
        self.age_all()
        later = prune.prune_research_objects(self.memory, grace_seconds=3600, now=time.time())
        self.assertEqual(later["removed_objects"], result["kept_unreachable_recent"])

    def test_pointer_changed_after_the_walk_keeps_everything(self):
        self.grow(10); self.memory.migrate_segments(); self.grow(10, "second"); self.age_all()
        before = sorted(path.name for path in self.objects().iterdir())
        original = prune._pointer_and_fingerprint
        calls = []
        def moved(memory):
            calls.append(True)
            pointer, fingerprint = original(memory)
            return (pointer, fingerprint) if len(calls) == 1 else (dict(pointer, generation=0), fingerprint)
        with patch.object(prune, "_pointer_and_fingerprint", side_effect=moved):
            result = prune.prune_research_objects(self.memory, grace_seconds=3600)
        self.assertEqual((result["state"], result["reason"]), ("kept", "RESEARCH_CHECKPOINT_CHANGED"))
        self.assertEqual(sorted(path.name for path in self.objects().iterdir()), before)

    def test_disabled_damaged_and_invalid_settings_remove_nothing(self):
        self.grow(3)
        self.assertEqual(prune.prune_research_objects(self.memory, grace_seconds=3600)["state"], "not_enabled")
        with self.assertRaisesRegex(SafetyError, "RESEARCH_PRUNE_SETTINGS_INVALID"):
            prune.prune_research_objects(self.memory, grace_seconds=60)
        self.memory.migrate_segments(); self.age_all()
        pointer = load_json(self.memory.root / segments.POINTER)
        (self.objects() / (pointer["checkpoint"]["sha256"] + ".json")).unlink()
        before = sorted(path.name for path in self.objects().iterdir())
        with self.assertRaises((SafetyError, OSError)):
            prune.prune_research_objects(self.memory, grace_seconds=3600)
        self.assertEqual(sorted(path.name for path in self.objects().iterdir()), before)

    def test_unknown_page_shapes_are_refused(self):
        self.grow(3); self.memory.migrate_segments()
        pointer = load_json(self.memory.root / segments.POINTER)
        checkpoint = segments._read(self.memory, pointer["checkpoint"])
        bad = segments._save(self.memory, {"contract": "other", "table": "concepts"})
        forged = dict(checkpoint, tables=dict(checkpoint["tables"], concepts={"root": bad, "rows": 1}))
        with self.assertRaisesRegex(SafetyError, "RESEARCH_PRUNE_PAGE_UNKNOWN"):
            prune.live_object_names(self.memory, dict(pointer, checkpoint=segments._save(self.memory, forged)))
        with self.assertRaisesRegex(SafetyError, "RESEARCH_PRUNE_CHECKPOINT_UNKNOWN"):
            prune.live_object_names(self.memory, dict(pointer, checkpoint=bad))
        with self.assertRaisesRegex(SafetyError, "RESEARCH_PRUNE_SEGMENT_UNKNOWN"):
            prune.live_object_names(self.memory, dict(pointer, last_segment=bad))
        groups = segments._save(self.memory, {"contract": "research-page-v1", "table": "novelty-groups", "prefix": "",
                                              "rows": [["x", {"aliases": "not a page"}]]})
        forged = dict(checkpoint, novelty=dict(checkpoint["novelty"], groups={"root": groups, "rows": 1}))
        with self.assertRaisesRegex(SafetyError, "RESEARCH_PRUNE_PAGE_UNKNOWN"):
            prune.live_object_names(self.memory, dict(pointer, checkpoint=segments._save(self.memory, forged)))

    def test_folder_size_diagnostic(self):
        self.assertEqual(prune.object_folder_bytes(self.memory)["objects"], 0)
        self.grow(3); self.memory.migrate_segments()
        self.assertGreater(prune.object_folder_bytes(self.memory)["bytes"], 0)


class ColdStartReplayTests(_Store, unittest.TestCase):
    def behind(self):
        """A published checkpoint behind the newest events, as after a stop mid-round."""
        self.grow(12); self.memory.migrate_segments()
        with self.memory.defer_lookup_projection(flush_if=lambda: False):
            self.grow(4, "later")
        pointer = load_json(self.memory.root / segments.POINTER)
        self.assertLess(pointer["sequence"], self.memory._verified["sequence"])
        return self.memory.snapshot()

    def test_cold_check_uses_the_one_verified_replay_not_a_second_one(self):
        expected = self.behind()
        cold = self.fresh()
        with patch.object(segments, "_prefix", side_effect=AssertionError("Second replay")):
            self.assertEqual(cold.snapshot(), expected)
        self.assertIsNone(cold.last_checkpoint_diagnostic)
        self.assertIsNotNone(cold._research_pages_verified)
        self.assertIsNone(cold._replay_capture)
        # Warm reads neither copy nor replay again.
        with patch.object(segments, "_prefix", side_effect=AssertionError("Second replay")):
            self.assertEqual(cold.snapshot(), expected)

    def test_a_capture_from_another_read_is_refused_and_replayed_canonically(self):
        self.behind()
        cold = self.fresh()
        original = segments._captured_prefix
        def stale(memory, root):
            memory._replay_capture = dict(memory._replay_capture, manifest=())
            return original(memory, root)
        with patch.object(segments, "_captured_prefix", side_effect=stale), \
                patch.object(segments, "_prefix", wraps=segments._prefix) as replay:
            cold.snapshot()
        self.assertEqual(replay.call_count, 1)
        self.assertIsNone(cold.last_checkpoint_diagnostic)

    def test_tampered_checkpoint_pages_still_fail_with_the_capture(self):
        self.behind()
        pointer = load_json(self.memory.root / segments.POINTER)
        checkpoint = segments._read(self.memory, pointer["checkpoint"])
        empty = dict(checkpoint, tables=dict(checkpoint["tables"], concepts={"root": None, "rows": 0}))
        forged = dict(pointer, checkpoint=segments._save(self.memory, empty))
        forged.pop("hash"); forged["hash"] = segments.digest(forged)
        atomic_json(self.memory.root / segments.POINTER, forged)
        cold = self.fresh()
        state = cold.snapshot()
        self.assertEqual(len(state["concepts"]), 16)
        self.assertEqual(cold.last_checkpoint_diagnostic, "RESEARCH_CHECKPOINT_CANONICAL_MISMATCH")

    def test_target_is_only_given_when_a_check_needs_it(self):
        self.assertIsNone(segments.prefix_target(self.memory))  # not enabled
        self.behind()
        self.assertIsNone(segments.prefix_target(self.memory))  # already verified here
        cold = self.fresh()
        self.assertEqual(segments.prefix_target(cold), load_json(cold.root / segments.POINTER)["sequence"])
        (cold.root / segments.POINTER).write_text("{}")
        self.assertIsNone(segments.prefix_target(cold))


class StoreHousekeepingTests(_Store, unittest.TestCase):
    def configure(self, value=DURABILITY):
        path = self.root / "config/operations/durability.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(value))

    def test_once_a_day_with_settings_and_nothing_without(self):
        # The pages are two hours older than this test's own "now" (16 October, 12:00 UTC), whatever
        # the real clock says: aged from the real time, they were too young once that day passed 14:00.
        aged = int(time.time() - timestamp("2026-10-16T12:00:00+00:00").timestamp()) + 7200
        self.grow(10); self.memory.migrate_segments(); self.grow(10, "second"); self.age_all(aged)
        self.assertIsNone(housekeeping.run_due_store_housekeeping(self.memory, root=self.root))
        self.configure()
        first = housekeeping.run_due_store_housekeeping(self.memory, root=self.root, now="2026-10-16T12:00:00+00:00")
        self.assertEqual(first["research_objects"]["state"], "removed")
        self.assertEqual(first["run_logs"]["reason"], "RUN_LOG_RETENTION_NO_METER_START")
        self.assertIsNone(housekeeping.run_due_store_housekeeping(self.memory, root=self.root,
                                                                  now="2026-10-16T13:00:00+00:00"))
        saved = load_json(self.root / housekeeping.CONTROL, 65536)
        self.assertEqual(saved["value"]["day"], "2026-10-16")
        nextday = housekeeping.run_due_store_housekeeping(self.memory, root=self.root, now="2026-10-17T12:00:00+00:00")
        self.assertEqual(nextday["research_objects"]["removed_objects"], 0)

    def test_run_logs_use_the_meter_start_and_failures_are_recorded(self):
        self.configure()
        with patch("product_radar.programme.load_meter_baseline", return_value={"started_at": "2026-10-13T18:00:00+00:00"}):
            value = housekeeping.run_due_store_housekeeping(self.memory, root=self.root, now="2026-10-16T12:00:00+00:00")
        self.assertEqual(value["run_logs"]["state"], "nothing_to_remove")
        self.assertEqual(value["research_objects"]["state"], "not_enabled")
        with patch("product_radar.programme.load_meter_baseline", side_effect=SafetyError("METER_BASELINE_INVALID")), \
                patch.object(prune, "prune_research_objects", side_effect=OSError("Synthetic drive error")):
            failed = housekeeping.run_due_store_housekeeping(self.memory, root=self.root, now="2026-10-17T12:00:00+00:00")
        self.assertEqual(failed["run_logs"], {"state": "failed", "reason": "METER_BASELINE_INVALID"})
        self.assertEqual(failed["research_objects"], {"state": "failed", "reason": "RESEARCH_PRUNE_FAILED"})
        self.configure(dict(DURABILITY, kind="other"))
        broken = housekeeping.run_due_store_housekeeping(self.memory, root=self.root, now="2026-10-18T12:00:00+00:00")
        self.assertEqual(broken, {"state": "failed", "reason": "DURABILITY_SETTINGS_INVALID"})
        self.assertEqual(housekeeping._code(ValueError("x"), "FALLBACK_CODE"), "FALLBACK_CODE")
        self.assertEqual(housekeeping._code(SafetyError("lower case"), "FALLBACK_CODE"), "FALLBACK_CODE")


class OwnerLineTests(_Store, unittest.TestCase):
    def test_a_line_only_when_tidying_is_stuck_in_the_meter_and_the_window(self):
        from product_radar import desktop_control as controls
        from product_radar.backup_retention import save_history
        control = self.root / "data/operations/backup_controls"
        line = housekeeping.housekeeping_line
        self.assertIsNone(line(self.root))
        for routine in ({"state": "removed", "reason": None}, {"state": "nothing_to_remove", "reason": None},
                        {"state": "kept", "reason": "BACKUP_PRUNE_BACKUP_RUNNING"}):
            save_history(control, routine)
            self.assertIsNone(line(self.root))
        save_history(control, {"state": "kept", "reason": "BACKUP_PRUNE_KEPT_SET_DAMAGED"})
        self.assertEqual(line(self.root), "Backup clean-up is paused: a kept backup copy is damaged.")
        save_history(control, {"state": "failed", "reason": "SOMETHING_NEW"})
        self.assertEqual(line(self.root), "Backup clean-up is paused: code SOMETHING_NEW.")
        save_history(control, {"state": "kept", "reason": "BACKUP_PRUNE_NEWEST_NOT_VERIFIED", "disk_low": True,
                               "free_bytes_after": 5 * 1024 ** 3, "minimum_free_bytes": 10 * 1024 ** 3})
        full = ("Daily backups are paused: the project drive has 5.0 GB free and needs at least 10 GB. "
                "Research continues.")
        self.assertEqual(line(self.root), full)
        save_history(control, {"state": "failed", "reason": "BACKUP_PRUNE_FAILED", "disk_low": True})
        self.assertEqual(line(self.root), "Daily backups are paused: the project drive is nearly full. Research "
                         "continues. Backup clean-up is paused: it stopped with an error.")
        save_history(control, {"state": "removed", "reason": None})
        # A stuck daily store tidying shows too (here: the page pruner fails).
        path = self.root / "config/operations/durability.json"
        path.parent.mkdir(parents=True, exist_ok=True); path.write_text(json.dumps(DURABILITY))
        with patch.object(prune, "prune_research_objects", side_effect=OSError("Synthetic drive error")):
            housekeeping.run_due_store_housekeeping(self.memory, root=self.root, now="2026-10-16T12:00:00+00:00")
        expected = "Research page clean-up is paused: it stopped with an error."
        self.assertEqual(line(self.root), expected)
        with patch.object(controls, "owned_programmes", return_value=[]):
            status = controls.status(self.root)
        self.assertEqual(status["housekeeping_line"], expected)
        self.assertIn("\n" + expected, status["message"])
        with patch("product_radar.store_housekeeping.housekeeping_line", side_effect=OSError("Synthetic")):
            self.assertIn("could not be read", controls._housekeeping_line(self.root))
        # A day whose tidying failed entirely is recorded and shown as well.
        path.write_text(json.dumps(dict(DURABILITY, kind="other")))
        housekeeping.run_due_store_housekeeping(self.memory, root=self.root, now="2026-10-17T12:00:00+00:00")
        self.assertEqual(line(self.root), "Research page clean-up is paused: its settings file "
                         "(config/operations/durability.json) is not valid. Old run-log clean-up is paused: its "
                         "settings file (config/operations/durability.json) is not valid.")


if __name__ == "__main__":
    unittest.main()
