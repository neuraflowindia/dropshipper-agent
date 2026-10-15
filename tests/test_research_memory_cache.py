"""Verified in-process replay cache tests; event files remain authoritative."""

from copy import deepcopy
import errno
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from product_radar.models import SafetyError, digest
from product_radar.research_memory import ResearchMemory, change
from product_radar.storage import Journal, atomic_bytes, atomic_json, load_json
import product_radar.research_memory as memory_module


class VerifiedReplayCacheTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.memory = self.fresh_memory()

    def fresh_memory(self):
        return ResearchMemory(self.root / "research", Journal(self.root / "finance"), evidence_type="synthetic")

    def seed(self, count):
        for number in range(count):
            self.memory.put("concepts", f"concept-{number}", {"number": number, "reason": "Synthetic cache fixture."})

    def read_count(self, mocked):
        return sum(Path(call.args[0]).parent.name == "events" for call in mocked.call_args_list)

    def event_path(self, sequence):
        return self.memory.root / "events" / f"{sequence:08d}.json"

    def test_thirty_and_eighty_event_histories_replay_once_then_need_no_json_reads(self):
        for count in (30, 80):
            with self.subTest(events=count), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                writer = ResearchMemory(root / "research", Journal(root / "finance"), evidence_type="synthetic")
                for number in range(count):
                    writer.put("concepts", f"concept-{number}", {"number": number})
                reader = ResearchMemory(root / "research", Journal(root / "finance"), evidence_type="synthetic")
                with patch.object(memory_module, "load_event", wraps=memory_module.load_event) as reads:
                    first = reader.snapshot()
                    self.assertEqual(self.read_count(reads), count)
                    reads.reset_mock()
                    second = reader.snapshot()
                    self.assertEqual(self.read_count(reads), 0)
                    self.assertEqual(second, first)
                self.assertEqual(len(first["concepts"]), count)

    def test_external_writer_tail_replays_only_new_events(self):
        self.seed(3)
        baseline = self.memory.snapshot()
        peer = self.fresh_memory()
        peer.put("concepts", "external-one", {"value": 1})
        peer.put("concepts", "external-two", {"value": 2})
        with patch.object(memory_module, "load_event", wraps=memory_module.load_event) as reads:
            current = self.memory.snapshot()
            self.assertEqual(self.read_count(reads), 2)
            reads.reset_mock()
            self.assertEqual(self.memory.snapshot(), current)
            self.assertEqual(self.read_count(reads), 0)
        self.assertEqual(len(current["concepts"]), len(baseline["concepts"]) + 2)

    def test_local_commit_updates_verified_cache_without_loading_its_event_again(self):
        self.seed(3)
        with patch.object(memory_module, "load_event", wraps=memory_module.load_event) as reads:
            self.memory.put("concepts", "local-new", {"value": 4})
            self.assertEqual(self.memory.snapshot()["concepts"]["local-new"], {"value": 4})
            self.assertEqual(self.read_count(reads), 0)

    def test_returned_snapshot_and_event_ids_cannot_mutate_cached_authority(self):
        self.seed(3)
        expected = self.memory.snapshot()
        changed = self.memory.snapshot()
        changed["concepts"]["concept-0"]["number"] = 999
        changed["concepts"].clear()
        with self.memory.journal.locked():
            state, _, _, ids = self.memory._state()
            state["concepts"].clear()
            ids.clear()
        self.assertEqual(self.memory.snapshot(), expected)
        self.assertFalse(self.memory.put("concepts", "concept-0", expected["concepts"]["concept-0"]))

    def test_failed_multi_change_commit_cannot_partially_mutate_the_verified_cache(self):
        self.seed(2)
        before = self.memory.snapshot()
        old = before["concepts"]["concept-0"]
        with self.assertRaisesRegex(SafetyError, "RESEARCH_DEPENDENCY_CHANGED"):
            self.memory.commit("invalid-partial-change", [
                change("concepts", "concept-0", {"number": 99}, previous=old),
                change("concepts", "concept-1", {"number": 100}, previous={"wrong": True}),
            ])
        self.assertEqual(self.memory.snapshot(), before)
        self.assertEqual(len(list((self.memory.root / "events").glob("*.json"))), 2)

    def test_original_event_tamper_forces_full_validation_and_is_rejected(self):
        self.seed(3)
        path = self.event_path(1)
        event = load_json(path)
        event["changes"][0]["value"]["number"] = 8
        # Leave the original event hash: the change must not become trusted.
        atomic_json(path, event)
        with patch.object(memory_module, "load_event", wraps=memory_module.load_event) as reads:
            with self.assertRaisesRegex(SafetyError, "RESEARCH_EVENTS_INVALID"):
                self.memory.snapshot()
            self.assertEqual(self.read_count(reads), 1)

    def test_harmless_old_file_metadata_change_causes_full_revalidation(self):
        self.seed(3)
        expected = self.memory.snapshot()
        path = self.event_path(1)
        original = path.stat()
        os.utime(path, ns=(original.st_atime_ns, original.st_mtime_ns + 1_000_000))
        with patch.object(memory_module, "load_event", wraps=memory_module.load_event) as reads:
            self.assertEqual(self.memory.snapshot(), expected)
            self.assertEqual(self.read_count(reads), 3)
            reads.reset_mock()
            self.assertEqual(self.memory.snapshot(), expected)
            self.assertEqual(self.read_count(reads), 0)

    def test_cached_synthetic_history_cannot_be_relabelled_as_captured(self):
        self.seed(2)
        self.memory.evidence_type = "captured"
        with self.assertRaisesRegex(SafetyError, "RESEARCH_EVENTS_INVALID"):
            self.memory.snapshot()

    def test_same_size_in_place_tamper_with_restored_mtime_is_not_a_cache_hit(self):
        self.seed(3)
        path = self.event_path(1)
        original = path.stat()
        raw = path.read_bytes()
        changed = raw.replace(b'"number":0', b'"number":8', 1)
        self.assertNotEqual(changed, raw)
        self.assertEqual(len(changed), len(raw))
        # Fixture corruption deliberately preserves inode and size, unlike the
        # application's atomic replacement. ctime still reveals the rewrite.
        with path.open("r+b") as handle:
            handle.write(changed)
        os.utime(path, ns=(original.st_atime_ns, original.st_mtime_ns))
        with self.assertRaisesRegex(SafetyError, "RESEARCH_EVENTS_INVALID"):
            self.memory.snapshot()

    def test_truncated_event_does_not_return_previously_cached_state(self):
        self.seed(3)
        atomic_bytes(self.event_path(2), b'{"seq":')
        with self.assertRaisesRegex(SafetyError, "JSON_RESPONSE_INVALID"):
            self.memory.snapshot()
        with self.assertRaisesRegex(SafetyError, "JSON_RESPONSE_INVALID"):
            self.memory.snapshot()

    def test_middle_event_deletion_is_rejected(self):
        self.seed(3)
        self.event_path(2).unlink()
        with self.assertRaisesRegex(SafetyError, "RESEARCH_EVENTS_INVALID"):
            self.memory.snapshot()

    def test_last_event_deletion_cannot_roll_back_this_instances_verified_history(self):
        self.seed(3)
        self.event_path(3).unlink()
        with self.assertRaisesRegex(SafetyError, "RESEARCH_EVENTS_INVALID"):
            self.memory.snapshot()

    def test_entire_verified_history_deletion_is_not_an_empty_new_namespace(self):
        self.seed(1)
        self.event_path(1).unlink()
        with self.assertRaisesRegex(SafetyError, "RESEARCH_EVENTS_INVALID"):
            self.memory.snapshot()

    def test_index_corruption_and_removal_are_ignored_by_cached_and_cold_readers(self):
        self.seed(3)
        expected = self.memory.snapshot()
        index = self.memory.root / "index.json"
        atomic_json(index, {"head": "fake", "state": {"concepts": {"invented": {"value": 999}}}})
        self.assertEqual(self.memory.snapshot(), expected)
        self.assertEqual(self.fresh_memory().snapshot(), expected)
        index.unlink()
        self.assertEqual(self.memory.snapshot(), expected)
        self.assertEqual(self.fresh_memory().snapshot(), expected)

    def test_failed_index_write_preserves_committed_event_and_verified_cache(self):
        self.seed(2)
        original = memory_module.atomic_json
        def fail_index(path, value):
            if Path(path).name == "index.json":
                raise OSError("Synthetic optional index failure")
            return original(path, value)
        with patch.object(memory_module, "atomic_json", side_effect=fail_index):
            self.assertTrue(self.memory.put("concepts", "committed", {"value": 3}))
            self.assertEqual(self.memory.last_commit_diagnostic, "RESEARCH_INDEX_WRITE_UNAVAILABLE")
        with patch.object(memory_module, "load_event", wraps=memory_module.load_event) as reads:
            self.assertEqual(self.memory.snapshot()["concepts"]["committed"], {"value": 3})
            self.assertEqual(self.read_count(reads), 0)
            self.assertFalse(self.memory.put("concepts", "committed", {"value": 3}))
        self.assertEqual(self.fresh_memory().snapshot(), self.memory.snapshot())
        events = len(list((self.memory.root / "events").glob("*.json")))
        self.assertTrue(self.fresh_memory().rebuild_index())
        self.assertEqual(len(list((self.memory.root / "events").glob("*.json"))), events)

    def test_real_event_failure_does_not_claim_success_or_mutate_verified_cache(self):
        self.seed(1)
        original = memory_module.atomic_json
        before = self.memory.snapshot()
        def fail_event(path, value):
            if Path(path).parent.name == "events":
                raise OSError("Synthetic canonical failure")
            return original(path, value)
        with patch.object(memory_module, "atomic_json", side_effect=fail_event):
            with self.assertRaisesRegex(OSError, "canonical failure"):
                self.memory.put("concepts", "not-written", {"value": 2})
        self.assertEqual(self.memory.snapshot(), before)

    def test_lost_event_write_reply_reconciles_only_exact_intended_event(self):
        original = memory_module.atomic_json
        def lose_reply(path, value):
            original(path, value)
            if Path(path).parent.name == "events":
                raise OSError("Synthetic lost rename reply")
        with patch.object(memory_module, "atomic_json", side_effect=lose_reply):
            self.assertTrue(self.memory.put("concepts", "once", {"value": 1}))
            self.assertFalse(self.memory.put("concepts", "once", {"value": 1}))
        self.assertEqual(len(list((self.memory.root / "events").glob("*.json"))), 1)
        self.assertEqual(self.fresh_memory().snapshot()["concepts"]["once"], {"value": 1})

    def test_oversized_index_is_optional_and_new_brand_table_replays(self):
        with patch.object(memory_module, "INDEX_LIMIT", 8):
            self.assertTrue(self.memory.put("brand_reviews", "review-one", {"verdict": "unclear"}))
            self.assertEqual(self.memory.last_commit_diagnostic, "RESEARCH_INDEX_SIZE_LIMIT")
        self.assertEqual(self.fresh_memory().snapshot()["brand_reviews"]["review-one"], {"verdict": "unclear"})

    def test_a_lookup_copy_known_too_large_is_not_rebuilt_only_to_be_refused(self):
        sizes = []
        real = memory_module.canonical

        def measured(value):
            text = real(value)
            if isinstance(value, dict) and set(value) == {"head", "state"}:
                sizes.append(len(text))
            return text
        with patch.object(memory_module, "INDEX_LIMIT", 600), patch.object(memory_module, "canonical", side_effect=measured):
            self.memory.put("concepts", "large", {"text": "x" * 700})
            self.assertEqual((self.memory.last_commit_diagnostic, len(sizes)), ("RESEARCH_INDEX_SIZE_LIMIT", 1))
            self.memory.put("concepts", "small", {"value": 1})
            self.assertEqual((self.memory.last_commit_diagnostic, len(sizes)), ("RESEARCH_INDEX_SIZE_LIMIT", 1))
            # Replacing the large record could make it fit: measured again, and saved.
            self.memory.put("concepts", "large", {"text": "y"}, previous={"text": "x" * 700})
            self.assertEqual((self.memory.last_commit_diagnostic, len(sizes)), (None, 2))
            self.assertTrue((self.memory.root / "index.json").is_file())
            self.memory.put("concepts", "large", {"text": "z" * 700}, previous={"text": "y"})
            self.assertEqual(len(sizes), 3)
            # Another writer's events: the size is measured again.
            other = self.fresh_memory()
            other.put("concepts", "other", {"value": 2})
            self.memory.put("concepts", "after-other", {"value": 3})
            self.assertEqual((self.memory.last_commit_diagnostic, len(sizes)), ("RESEARCH_INDEX_SIZE_LIMIT", 5))
        self.assertEqual(self.fresh_memory().snapshot()["concepts"]["small"], {"value": 1})

    def test_event_symlink_replacement_is_rejected_before_cache_reuse(self):
        self.seed(2)
        path = self.event_path(1)
        duplicate = self.root / "outside-event.json"
        atomic_bytes(duplicate, path.read_bytes())
        path.unlink()
        path.symlink_to(duplicate)
        with self.assertRaisesRegex(SafetyError, "SYMLINK_PATH_REFUSED"):
            self.memory.snapshot()

    def test_commit_guard_is_current_atomic_and_exact_replay_skips_guard(self):
        from product_radar.research_memory import change
        self.memory.put("concepts", "base", {"value": 1})
        proposed = [change("brand_reviews", "review", {"value": 2})]
        self.memory.put("concepts", "base", {"value": 3}, previous={"value": 1})
        def stale_guard(state):
            if state["concepts"]["base"] != {"value": 1}:
                raise SafetyError("SYNTHETIC_BASE_CHANGED")
        with self.assertRaisesRegex(SafetyError, "SYNTHETIC_BASE_CHANGED"):
            self.memory.commit("guarded", proposed, validate=stale_guard)
        self.assertNotIn("review", self.memory.snapshot()["brand_reviews"])
        self.assertTrue(self.memory.commit("guarded", proposed, validate=lambda state: state["concepts"].clear()))
        self.assertIn("base", self.memory.snapshot()["concepts"])
        self.assertFalse(self.memory.commit("guarded", proposed, validate=stale_guard))

    def test_read_only_snapshot_does_not_need_or_hold_the_financial_writer_lock(self):
        self.seed(3)
        reader = self.fresh_memory()
        observed = []
        original = memory_module.load_event
        def check_read(path, limit=32768):
            if Path(path).parent.name == "events":
                observed.append(reader.journal._locked)
                # Another writer can acquire the actual kernel lock while
                # this cold reader verifies immutable original event bytes.
                with Journal(self.root / "finance").locked():
                    pass
            return original(path, limit)
        with patch.object(memory_module, "load_event", side_effect=check_read):
            self.assertEqual(reader.snapshot(), self.memory.snapshot())
        self.assertEqual(observed, [False, False, False])

    def test_a_cold_read_remains_a_valid_prefix_when_another_writer_appends(self):
        self.seed(2)
        reader, peer = self.fresh_memory(), self.fresh_memory()
        before = self.memory.snapshot()
        original, added = memory_module.load_event, []
        def append_during_read(path, limit=32768):
            value = original(path, limit)
            if Path(path).parent.name == "events" and not added:
                added.append(True)
                peer.put("concepts", "appended", {"value": 3})
            return value
        with patch.object(memory_module, "load_event", side_effect=append_during_read):
            # The reader selected the two-event prefix before the append.
            self.assertEqual(reader.snapshot(), before)
        self.assertEqual(reader.snapshot()["concepts"]["appended"], {"value": 3})
        self.assertEqual(reader.snapshot(), peer.snapshot())

    def test_cold_commit_warms_history_before_holding_the_writer_lock(self):
        self.seed(3)
        writer = self.fresh_memory()
        original, lock_states = memory_module.load_event, []
        def inspected(path, limit=32768):
            if Path(path).parent.name == "events":
                lock_states.append(writer.journal._locked)
            return original(path, limit)
        with patch.object(memory_module, "load_event", side_effect=inspected):
            writer.put("concepts", "new", {"value": 4})
        self.assertEqual(lock_states, [False, False, False])
        self.assertEqual(self.memory.snapshot()["concepts"]["new"], {"value": 4})

    def test_disposable_index_copy_does_not_hold_the_canonical_or_financial_lock(self):
        self.seed(2)
        original, index_lock_states = memory_module.atomic_json, []
        def inspect_write(path, value):
            if Path(path).name == "index.json":
                index_lock_states.append(self.memory.journal._locked)
                with Journal(self.root / "finance").locked():
                    pass
            return original(path, value)
        with patch.object(memory_module, "atomic_json", side_effect=inspect_write):
            self.assertTrue(self.memory.put("concepts", "new", {"value": 3}))
            self.assertTrue(self.memory.rebuild_index())
        self.assertEqual(index_lock_states, [False, False])
        self.assertEqual(self.fresh_memory().snapshot(), self.memory.snapshot())

    def test_invalid_new_tail_is_never_saved_as_verified_cache(self):
        self.seed(2)
        last = load_json(self.event_path(2))
        invalid = deepcopy(last)
        invalid.update(seq=3, id="bad-tail", previous="0" * 64)
        invalid["hash"] = digest({k: value for k, value in invalid.items() if k != "hash"})
        atomic_json(self.event_path(3), invalid)
        with self.assertRaisesRegex(SafetyError, "RESEARCH_EVENTS_INVALID"):
            self.memory.snapshot()
        self.event_path(3).unlink()
        self.assertEqual(len(self.memory.snapshot()["concepts"]), 2)

    def test_short_local_admission_conflict_rechecks_the_same_commit(self):
        original = self.memory._commit
        calls = []
        def busy_once(event_id, changes, *, validate=None):
            calls.append(event_id)
            if len(calls) == 1:
                raise SafetyError("RUNNER_ALREADY_ACTIVE")
            return original(event_id, changes, validate=validate)
        with patch.object(self.memory, "_commit", side_effect=busy_once):
            self.assertTrue(self.memory.put("concepts", "same-event", {"value": 1}))
        self.assertEqual(len(calls), 2)
        self.assertEqual(calls[0], calls[1])
        self.assertEqual(len(list((self.memory.root / "events").glob("*.json"))), 1)

    def test_persistent_local_conflict_has_exactly_three_attempts_not_an_endless_loop(self):
        with patch.object(self.memory, "_commit", side_effect=SafetyError("RUNNER_ALREADY_ACTIVE")) as commit:
            with self.assertRaisesRegex(SafetyError, "RUNNER_ALREADY_ACTIVE"):
                self.memory.put("concepts", "waiting", {"value": 1})
        self.assertEqual(commit.call_count, 3)
        self.assertNotIn("waiting", self.memory.snapshot()["concepts"])

    def test_transient_read_failure_rechecks_original_records_without_using_old_cache(self):
        self.seed(2)
        expected = self.memory.snapshot()
        original = self.memory._state
        calls = []
        def unavailable_once(*, readonly=False):
            calls.append(readonly)
            if len(calls) == 1:
                raise SafetyError("SAVED_FILE_UNAVAILABLE")
            return original(readonly=readonly)
        with patch.object(self.memory, "_state", side_effect=unavailable_once):
            self.assertEqual(self.memory.snapshot(), expected)
        self.assertEqual(calls, [True, True])

    def test_read_failure_is_bounded_and_integrity_failure_is_never_retried(self):
        self.seed(1)
        with patch.object(self.memory, "_state", side_effect=SafetyError("SAVED_FILE_UNAVAILABLE")) as read:
            with self.assertRaisesRegex(SafetyError, "SAVED_FILE_UNAVAILABLE"):
                self.memory.snapshot()
        self.assertEqual(read.call_count, 3)
        with patch.object(self.memory, "_state", side_effect=SafetyError("RESEARCH_EVENTS_INVALID")) as read:
            with self.assertRaisesRegex(SafetyError, "RESEARCH_EVENTS_INVALID"):
                self.memory.snapshot()
        self.assertEqual(read.call_count, 1)

    def test_integrity_or_dependency_error_is_never_retried_as_a_local_admission_failure(self):
        for reason in ("RESEARCH_DEPENDENCY_CHANGED", "RESEARCH_EVENTS_INVALID", "RESEARCH_EVENT_CONFLICT"):
            with self.subTest(reason=reason), patch.object(self.memory, "_commit", side_effect=SafetyError(reason)) as commit:
                with self.assertRaisesRegex(SafetyError, reason):
                    self.memory.put("concepts", "invalid", {"value": 1})
                self.assertEqual(commit.call_count, 1)

    def test_event_reader_rejects_final_symlink_without_reading_target(self):
        self.seed(1)
        link = self.root / "event-link.json"
        link.symlink_to(self.event_path(1))
        if hasattr(os, "O_NOFOLLOW"):
            with self.assertRaisesRegex(SafetyError, "SYMLINK_PATH_REFUSED"):
                memory_module.load_event(link)

    def test_event_reader_does_not_wait_on_special_files(self):
        directory = self.root / "not-an-event"
        directory.mkdir()
        with self.assertRaisesRegex(SafetyError, "RESEARCH_EVENTS_INVALID"):
            memory_module.load_event(directory)
        if hasattr(os, "mkfifo") and hasattr(os, "O_NONBLOCK"):
            fifo = self.root / "not-an-event.json"
            os.mkfifo(fifo)
            with self.assertRaisesRegex(SafetyError, "RESEARCH_EVENTS_INVALID"):
                memory_module.load_event(fifo)

    def test_event_reader_checks_original_size_and_descriptor_after_read(self):
        self.seed(1)
        with self.assertRaisesRegex(SafetyError, "SAVED_FILE_SIZE_LIMIT"):
            memory_module.load_event(self.event_path(1), 8)
        original = os.fstat
        calls = []
        def changed_after_read(descriptor):
            value = original(descriptor)
            calls.append(descriptor)
            if len(calls) == 2:
                class Changed:
                    st_dev, st_ino, st_size = value.st_dev, value.st_ino, value.st_size
                    st_mode, st_mtime_ns, st_ctime_ns = value.st_mode, value.st_mtime_ns + 1, value.st_ctime_ns
                return Changed()
            return value
        with patch.object(memory_module.os, "fstat", side_effect=changed_after_read):
            with self.assertRaisesRegex(SafetyError, "RESEARCH_EVENTS_INVALID"):
                memory_module.load_event(self.event_path(1))
        self.assertEqual(len(calls), 2)

    def test_event_fingerprint_read_error_is_a_named_diagnostic_not_a_name_error(self):
        self.seed(1)
        with patch.object(Path, "lstat", side_effect=OSError(errno.EIO, "Synthetic mounted-drive read failure")):
            with self.assertRaisesRegex(SafetyError, "SAVED_FILE_UNAVAILABLE"):
                self.memory._fingerprint(self.event_path(1))
        self.assertEqual(self.memory.last_read_diagnostic, {"operation": "event_manifest", "errno": errno.EIO})

    def test_disappearing_manifest_entry_is_not_silently_an_empty_namespace(self):
        class VanishedEntry:
            name, path = "00000001.json", str(self.root / "research/events/00000001.json")
            def stat(self, **kwargs):
                raise FileNotFoundError(errno.ENOENT, "Synthetic disappearing entry")
        class Directory:
            def __enter__(self):
                return iter([VanishedEntry()])
            def __exit__(self, *args):
                return False
        with patch.object(memory_module.os, "scandir", return_value=Directory()):
            with self.assertRaisesRegex(SafetyError, "SAVED_FILE_UNAVAILABLE"):
                self.memory.snapshot()
        self.assertIsNone(self.memory._verified)


if __name__ == "__main__":
    unittest.main()
