"""Actual shared Journal consumers use the same canonical money after rollover."""

from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from product_radar import financial_segments as segments
from product_radar.models import SafetyError, canonical, digest
from product_radar.storage import Journal, MAX_JOURNAL, atomic_bytes, atomic_json, load_json
from test_production_budget import ProductionBudgetTests


class FinancialSegmentsTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(); self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.journal = Journal(self.root / "finance")

    def budget(self, namespace="test-account:period-one"):
        self.journal.setup_budget(namespace, "5")

    def test_migration_keeps_old_bytes_money_and_exact_event_ids(self):
        with self.journal.locked():
            self.budget()
            old, events, state = self.journal.path.read_bytes(), self.journal.events(), self.journal.state()
            spendable = self.journal.spendable("test-account:period-one")
            first = self.journal.migrate_segments()
            self.assertFalse(first["reused"])
            self.assertEqual(self.journal.events(), events)
            self.assertEqual(self.journal.state(), state)
            self.assertEqual(self.journal.path.read_bytes(), old)
            again = self.journal.migrate_segments(); self.assertTrue(again["reused"])
            event = events[0]
            self.assertEqual(self.journal.append(event["event_id"], event["kind"], event["data"]), event)
            with self.assertRaisesRegex(SafetyError, "EVENT_ID_CONFLICT"):
                self.journal.append(event["event_id"], "budget", {**event["data"], "allowance": "9"})
            self.assertEqual(self.journal.spendable("test-account:period-one"), spendable)
        cold = Journal(self.journal.root)
        with cold.locked():
            self.assertEqual(cold.state(), state)
            self.assertEqual(cold.export_bytes(), b"".join(canonical(row) + b"\n" for row in events))

    def test_warm_empty_migration_notices_previously_absent_legacy_file_appearing(self):
        with self.journal.locked():
            self.journal.migrate_segments()
            self.assertEqual(self.journal.state(), ({}, {}))
        other = Journal(self.root / "old-writer-fixture")
        with other.locked():
            other.setup_budget("test-account:period-one", "5")
        atomic_bytes(self.journal.path, other.path.read_bytes())
        with self.journal.locked():
            with self.assertRaisesRegex(SafetyError, "JOURNAL_LEGACY_PREFIX_CHANGED"):
                self.journal.state()

    def test_new_period_append_is_segmented_and_does_not_touch_legacy_file(self):
        with self.journal.locked():
            self.budget(); self.journal.migrate_segments()
            old = self.journal.path.read_bytes()
            self.budget("test-account:period-two")
            self.assertEqual(self.journal.path.read_bytes(), old)
            self.assertEqual(len(self.journal.events()), 4)
            self.assertEqual(len(self.journal.state()[0]), 2)
            self.assertEqual(self.journal.spendable("test-account:period-one"), self.journal.spendable("test-account:period-two"))
            self.assertNotEqual(self.journal.export_bytes(), old)

    def test_unpublished_orphans_retry_once_but_lost_committed_reply_replays(self):
        with self.journal.locked():
            self.journal.migrate_segments()
            original = segments._publish
            with patch.object(segments, "_publish", side_effect=OSError("Synthetic pre-pointer failure")):
                with self.assertRaises(OSError):
                    self.budget()
            self.assertEqual(self.journal.events(), [])
            self.budget()
            def lost_reply(journal, value):
                original(journal, value)
                raise OSError("Synthetic committed reply lost")
            with patch.object(segments, "_publish", side_effect=lost_reply):
                with self.assertRaises(OSError):
                    self.budget("test-account:period-two")
            self.budget("test-account:period-two")
            self.assertEqual(len(self.journal.state()[0]), 2)
            self.assertEqual(len(self.journal.events()), 4)

    def test_checkpoint_failure_leaves_old_pointer_authoritative(self):
        with self.journal.locked():
            self.budget()
            with patch.object(segments, "_read_pages", side_effect=SafetyError("SYNTHETIC_BAD_CHECKPOINT")):
                with self.assertRaisesRegex(SafetyError, "SYNTHETIC_BAD_CHECKPOINT"):
                    self.journal.migrate_segments()
            self.assertFalse(segments.enabled(self.journal))
            self.assertEqual(len(self.journal.events()), 2)
            self.journal.migrate_segments()

    def test_actual_state_pages_and_canonical_chain_reject_resealed_invented_money(self):
        with self.journal.locked():
            self.budget(); self.journal.migrate_segments()
        pointer_path = self.journal.root / segments.POINTER
        pointer = load_json(pointer_path)
        checkpoint = segments._read(self.journal, pointer["checkpoint"])
        page_ref = checkpoint["tables"]["budgets"]["last"]
        page = segments._read(self.journal, page_ref)
        page["rows"][0][1]["allowance"] = "500"
        checkpoint["tables"]["budgets"]["last"] = segments._save(self.journal, page)
        checkpoint["state_hash"] = digest(({"test-account:period-one": page["rows"][0][1]}, {}))
        pointer["checkpoint"] = segments._save(self.journal, checkpoint)
        segments._publish(self.journal, pointer)
        cold = Journal(self.journal.root)
        with cold.locked(), self.assertRaisesRegex(SafetyError, "CHECKPOINT_CANONICAL_MISMATCH"):
            cold.state()

    def test_old_file_modification_segment_corruption_and_warm_rollback_are_rejected(self):
        with self.journal.locked():
            self.budget(); self.journal.migrate_segments()
            first = load_json(self.journal.root / segments.POINTER)
            self.budget("test-account:period-two")
            latest = load_json(self.journal.root / segments.POINTER)
            atomic_json(self.journal.root / segments.POINTER, first)
            with self.assertRaisesRegex(SafetyError, "ROLLBACK"):
                self.journal.state()
            atomic_json(self.journal.root / segments.POINTER, latest)
            old = self.journal.path.read_bytes(); atomic_bytes(self.journal.path, old + b"\n")
            with self.assertRaisesRegex(SafetyError, "LEGACY_PREFIX_CHANGED"):
                self.journal.state()
            atomic_bytes(self.journal.path, old)
            path = segments._path(self.journal, latest["last_segment"])
            atomic_bytes(path, b"{}\n")
            with self.assertRaisesRegex(SafetyError, "HASH_CHANGED"):
                self.journal.state()

    def test_warm_state_does_not_parse_old_blobs_or_trust_disposable_index(self):
        with self.journal.locked():
            self.budget(); self.journal.migrate_segments()
            expected = self.journal.state()
            atomic_json(self.journal.root / "index.json", {"money": "invented"})
            with patch.object(segments, "_read", side_effect=AssertionError("No unchanged blob reread")):
                self.assertEqual(self.journal.state(), expected)
                self.assertEqual(len(self.journal.events()), 2)

    def test_warm_append_replays_only_new_tail_and_current_checkpoint(self):
        with self.journal.locked():
            self.budget(); self.journal.migrate_segments()
            prefix = {path for path in self.journal._financial_verified["segment_files"]}
            original, reads = segments._read, []
            def recording(journal, reference, files=None):
                reads.append(segments._path(journal, reference))
                return original(journal, reference, files)
            with patch.object(segments, "_read", side_effect=recording):
                self.budget("test-account:period-two")
            self.assertFalse(prefix & set(reads), reads)
            self.assertEqual(len(self.journal.events()), 4)

    def test_old_inactive_checkpoint_loss_does_not_break_the_new_authoritative_checkpoint(self):
        with patch.object(segments, "CHECKPOINT_INTERVAL", 2), self.journal.locked():
            self.budget(); self.journal.migrate_segments()
            old = {path for path in self.journal._financial_verified["checkpoint_files"]}
            self.budget("test-account:period-two")
            current = {path for path in self.journal._financial_verified["checkpoint_files"]}
            for path in old - current:
                path.unlink()
            self.assertEqual(len(self.journal.state()[0]), 2)
        cold = Journal(self.journal.root)
        with cold.locked():
            self.assertEqual(len(cold.state()[0]), 2)

    def test_pages_roll_over_under_small_bound_and_state_survives_cold_restart(self):
        with patch.object(segments, "MAX_PAGE_ROWS", 2), patch.object(segments, "MAX_SEGMENT_EVENTS", 2), \
                patch.object(segments, "CHECKPOINT_INTERVAL", 2), self.journal.locked():
            for i in range(5):
                self.budget(f"test-account:period-{i}")
            before = self.journal.state()
            self.journal.migrate_segments()
            self.budget("test-account:period-six")
            self.assertEqual(len(self.journal.state()[0]), 6)
            cold = Journal(self.journal.root)
        with cold.locked():
            self.assertEqual(len(cold.state()[0]), 6)
            self.assertEqual({key: row for key, row in cold.state()[0].items() if key in before[0]}, before[0])

    def test_empty_migration_still_requires_the_existing_writer_lock(self):
        with self.assertRaisesRegex(SafetyError, "LOCK_REQUIRED"):
            self.journal.migrate_segments()
        with self.journal.locked():
            self.journal.migrate_segments()
            self.assertEqual(self.journal.events(), [])
            self.assertEqual(self.journal.state(), ({}, {}))

    def test_actual_legacy_file_above_eight_megabytes_migrates_without_money_reset(self):
        events, head = [], segments.ZERO
        # Enough real valid budget events to cross the actual old byte limit,
        # not a patched-down stand-in for that boundary.
        for number in range(24000):
            body = {"version": 1, "seq": number + 1, "event_id": f"scale-budget-{number}",
                "at": "2026-10-09T00:00:00+00:00", "kind": "budget", "data": {
                    "namespace": f"synthetic-account-{number:05d}:synthetic-period-{number:05d}",
                    "allowance": "5", "production_provision": "0", "emergency": "0"}, "previous": head}
            event = {**body, "hash": digest(body)}; events.append(event); head = event["hash"]
        raw = b"".join(canonical(event) + b"\n" for event in events)
        self.assertGreater(len(raw), MAX_JOURNAL)
        atomic_bytes(self.journal.path, raw)
        with self.journal.locked():
            with self.assertRaisesRegex(SafetyError, "JOURNAL_CORRUPT_REVIEW_REQUIRED"):
                self.journal.events()
            proof = self.journal.migrate_segments()
            self.assertEqual(proof["events"], 24000)
            self.assertEqual(proof["head"], head)
            self.assertEqual(len(self.journal.state()[0]), 24000)
            self.assertEqual(self.journal.export_bytes(), raw)
            self.assertEqual(self.journal.path.read_bytes(), raw)
            self.assertLess((self.journal.root / segments.POINTER).stat().st_size, 16384)
            self.assertTrue(all(path.stat().st_size <= segments.MAX_OBJECT
                                for path in (self.journal.root / "financial-v2/objects").glob("*.json")))


class SegmentedProductionBudgetTests(ProductionBudgetTests):
    """Run every existing paid-reservation rule through the new shared reader."""
    def setUp(self):
        super().setUp()
        with self.journal.locked():
            self.journal.migrate_segments()


if __name__ == "__main__":
    unittest.main()
