"""Same-read checkpoint metadata reuse, using only temporary research stores.

The optimization does not remove fresh directory scans or grant authority to a
caller-supplied map. These tests distinguish a consistent read prefix from the
fresh, locked checks required by a later canonical mutation. No live research,
provider, model, installed task, process, or release gate is accessed.
"""

import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from product_radar.models import SafetyError
from product_radar.research_memory import ResearchMemory, change
from product_radar.storage import Journal, atomic_bytes, atomic_json, load_json
import product_radar.research_memory as memory_module
import product_radar.research_segments as segments


class ResearchSameReadManifestTests(unittest.TestCase):
    def memory(self, count=4, *, namespace="synthetic"):
        temporary = tempfile.TemporaryDirectory(prefix="same-read-manifest-")
        self.addCleanup(temporary.cleanup)
        root = Path(temporary.name)
        memory = ResearchMemory(root / "research", Journal(root / "finance"), evidence_type=namespace)
        for number in range(count):
            memory.put("concepts", f"concept-{number}", {"label": f"Disposable type {number}"})
        memory.migrate_segments()
        expected = memory.snapshot()
        self.assertIsNotNone(memory._research_pages_verified)
        return memory, expected

    def originals(self, memory):
        return {path.name: path.read_bytes() for path in (memory.root / "events").iterdir()
                if path.name.endswith(".json")}

    def original_fingerprints(self, memory, mocked):
        return [Path(call.args[0]) for call in mocked.call_args_list
                if Path(call.args[0]).parent == memory.root / "events"]

    def metadata(self, memory):
        verified = memory._verified
        return verified["state"], verified["head"], verified["sequence"], verified["manifest"]

    def corrupt_first_original(self, memory):
        path = memory.root / "events" / "00000001.json"
        event = load_json(path)
        event["changes"][0]["value"]["label"] = "An unaccepted changed original"
        atomic_json(path, event)  # Deliberately retain the old event hash.

    def test_warm_thirty_and_eighty_event_reads_rescan_all_originals_but_only_fingerprint_anchor(self):
        for namespace in ("synthetic", "captured"):
            for count in (30, 80):
                with self.subTest(namespace=namespace, events=count):
                    memory, expected = self.memory(count, namespace=namespace)
                    original_bytes = self.originals(memory)
                    stat_fingerprint = ResearchMemory._stat_fingerprint
                    with patch.object(memory, "_fingerprint", wraps=memory._fingerprint) as fingerprints, \
                            patch.object(ResearchMemory, "_stat_fingerprint", wraps=stat_fingerprint) as stats, \
                            patch.object(memory_module, "load_event", wraps=memory_module.load_event) as reads:
                        self.assertEqual(memory.snapshot(), expected)
                    self.assertEqual(self.original_fingerprints(memory, fingerprints),
                                     [memory.root / "events" / f"{count:08d}.json"])
                    # Every directory entry still gets its fresh no-follow
                    # stat. Other calls are the anchor and derived objects.
                    self.assertEqual(stats.call_count, count + fingerprints.call_count)
                    self.assertEqual(reads.call_count, 0)
                    self.assertEqual(self.originals(memory), original_bytes)

    def test_internal_read_passes_exact_verified_tuple_and_matching_prefix(self):
        memory, expected = self.memory()
        original_check = segments.check
        seen = []
        def inspect(owner, state, head, sequence, **options):
            seen.append(options)
            self.assertIs(owner, memory)
            self.assertIs(options.get("_verified_manifest"), owner._verified["manifest"])
            self.assertIsInstance(options["_verified_manifest"], tuple)
            self.assertEqual((head, sequence, owner.evidence_type),
                             (owner._verified["head"], owner._verified["sequence"],
                              owner._verified["evidence_type"]))
            return original_check(owner, state, head, sequence, **options)
        with patch.object(segments, "check", side_effect=inspect):
            self.assertEqual(memory.snapshot(), expected)
        self.assertEqual(len(seen), 1)

    def test_cold_migrated_reader_still_reads_every_original_body_in_both_namespaces(self):
        for namespace in ("synthetic", "captured"):
            with self.subTest(namespace=namespace):
                memory, expected = self.memory(6, namespace=namespace)
                reader = ResearchMemory(memory.root, Journal(memory.journal.root), evidence_type=namespace)
                self.assertIsNone(reader._verified)
                with patch.object(memory_module, "load_event", wraps=memory_module.load_event) as reads:
                    self.assertEqual(reader.snapshot(), expected)
                expected_names = {f"{number:08d}.json" for number in range(1, 7)}
                self.assertEqual({Path(call.args[0]).name for call in reads.call_args_list}, expected_names)
                self.assertGreaterEqual(reads.call_count, 6)
                self.assertIsNotNone(reader._research_pages_verified)
                self.assertEqual(reader._verified["evidence_type"], namespace)

    def test_standalone_check_after_optimized_read_still_fingerprints_every_original(self):
        memory, expected = self.memory(8)
        self.assertEqual(memory.snapshot(), expected)
        state, head, sequence, _ = self.metadata(memory)
        with patch.object(memory, "_fingerprint", wraps=memory._fingerprint) as fingerprints:
            self.assertIsNotNone(segments.check(memory, state, head, sequence))
        paths = self.original_fingerprints(memory, fingerprints)
        self.assertEqual(len(paths), sequence + 1)  # Original set plus the activation anchor.
        self.assertEqual(set(paths), {path for path, _ in memory._verified["manifest"]})

    def test_copied_tuple_arbitrary_map_and_wrong_identity_fall_back_to_fresh_checks(self):
        memory, _ = self.memory(6)
        state, head, sequence, manifest = self.metadata(memory)
        copied = tuple(list(manifest))
        self.assertIsNot(copied, manifest)
        candidates = (copied, dict(manifest), (), {path: (0, 0, 0, 0, 0) for path, _ in manifest})
        for candidate in candidates:
            with self.subTest(kind=type(candidate).__name__, empty=not candidate), \
                    patch.object(memory, "_fingerprint", wraps=memory._fingerprint) as fingerprints:
                self.assertIsNotNone(segments.check(memory, state, head, sequence,
                                                    _verified_manifest=candidate))
            self.assertEqual(len(self.original_fingerprints(memory, fingerprints)), sequence + 1)

    def test_head_sequence_and_namespace_mismatch_cannot_reuse_manifest(self):
        memory, _ = self.memory(6)
        state, head, sequence, manifest = self.metadata(memory)
        for field, value in (("head", "0" * 64), ("sequence", sequence + 1),
                             ("evidence_type", "captured")):
            with self.subTest(field=field), patch.dict(memory._verified, {field: value}), \
                    patch.object(memory, "_fingerprint", wraps=memory._fingerprint) as fingerprints:
                self.assertIsNotNone(segments.check(memory, state, head, sequence,
                                                    _verified_manifest=manifest))
            self.assertEqual(len(self.original_fingerprints(memory, fingerprints)), sequence + 1)

    def test_old_manifest_after_new_original_commit_cannot_be_reused(self):
        memory, _ = self.memory(5)
        old_manifest = memory._verified["manifest"]
        memory.put("concepts", "new-original", {"label": "A new accepted original"})
        state, head, sequence, _ = self.metadata(memory)
        self.assertIsNot(old_manifest, memory._verified["manifest"])
        with patch.object(memory, "_fingerprint", wraps=memory._fingerprint) as fingerprints:
            self.assertIsNotNone(segments.check(memory, state, head, sequence,
                                                _verified_manifest=old_manifest))
        self.assertEqual(len(self.original_fingerprints(memory, fingerprints)), sequence + 1)

    def test_standalone_update_keeps_fresh_original_checks_and_exact_original_bytes(self):
        memory, _ = self.memory(5)
        before = self.originals(memory)
        state, head, sequence, _ = self.metadata(memory)
        with patch.object(memory, "_fingerprint", wraps=memory._fingerprint) as fingerprints, \
                patch.object(segments, "check", wraps=segments.check) as checks:
            self.assertTrue(segments.update(memory, state, head, sequence))
        self.assertGreaterEqual(len(self.original_fingerprints(memory, fingerprints)), sequence + 1)
        self.assertTrue(checks.call_args_list)
        self.assertTrue(all("_verified_manifest" not in call.kwargs for call in checks.call_args_list))
        self.assertEqual(self.originals(memory), before)

    def test_peer_append_is_freshly_replayed_and_exact_local_commit_stays_idempotent(self):
        memory, before = self.memory(5)
        original_bytes = self.originals(memory)
        peer = ResearchMemory(memory.root, Journal(memory.journal.root), evidence_type=memory.evidence_type)
        peer.put("concepts", "peer-added", {"label": "A peer's accepted type"})
        with patch.object(memory_module, "load_event", wraps=memory_module.load_event) as reads:
            latest = memory.snapshot()
        self.assertEqual(latest["concepts"]["peer-added"], {"label": "A peer's accepted type"})
        self.assertEqual(len(latest["concepts"]), len(before["concepts"]) + 1)
        # Canonical tail replay and the changed checkpoint's segment proof can
        # both inspect the new event. Neither may reload old event bodies.
        self.assertGreaterEqual(reads.call_count, 1)
        self.assertEqual({Path(call.args[0]).name for call in reads.call_args_list}, {"00000006.json"})
        self.assertTrue(memory.put("concepts", "local-added", {"label": "A local accepted type"}))
        self.assertFalse(memory.put("concepts", "local-added", {"label": "A local accepted type"}))
        self.assertEqual(len(self.originals(memory)), 7)
        self.assertTrue(all(self.originals(memory)[name] == raw for name, raw in original_bytes.items()))

    def test_locked_commit_rechecks_peer_changes_after_prior_advisory_snapshot(self):
        memory, before = self.memory(4)
        peer = ResearchMemory(memory.root, Journal(memory.journal.root), evidence_type=memory.evidence_type)
        old = before["concepts"]["concept-0"]
        peer.put("concepts", "concept-0", {"label": "A fresh peer correction"}, previous=old)
        proposed = [change("brand_reviews", "must-not-save", {"state": "unclear"})]
        def guard(state):
            if state["concepts"]["concept-0"] != old:
                raise SafetyError("SYNTHETIC_CURRENT_DEPENDENCY_CHANGED")
        with self.assertRaisesRegex(SafetyError, "CURRENT_DEPENDENCY_CHANGED"):
            memory.commit("stale-advisory-save", proposed, validate=guard)
        self.assertNotIn("must-not-save", memory.snapshot()["brand_reviews"])
        self.assertEqual(len(self.originals(memory)), 5)

    def test_original_edit_before_next_read_still_forces_canonical_rejection(self):
        memory, _ = self.memory(4)
        self.corrupt_first_original(memory)
        with self.assertRaisesRegex(SafetyError, "RESEARCH_EVENTS_INVALID"):
            memory.snapshot()

    def test_harmless_original_metadata_change_revalidates_bytes_and_checkpoint_originals(self):
        memory, expected = self.memory(4)
        path = memory.root / "events" / "00000001.json"
        raw = path.read_bytes()
        before = path.stat()
        os.utime(path, ns=(before.st_atime_ns, before.st_mtime_ns + 1_000_000))
        with patch.object(memory_module, "load_event", wraps=memory_module.load_event) as reads, \
                patch.object(memory, "_fingerprint", wraps=memory._fingerprint) as fingerprints:
            self.assertEqual(memory.snapshot(), expected)
        self.assertEqual({Path(call.args[0]).name for call in reads.call_args_list},
                         {f"{number:08d}.json" for number in range(1, 5)})
        self.assertGreaterEqual(reads.call_count, 4)
        self.assertIn(path, self.original_fingerprints(memory, fingerprints))
        self.assertEqual(path.read_bytes(), raw)
        self.assertIsNone(memory.last_checkpoint_diagnostic)
        self.assertIsNotNone(memory._research_pages_verified)

    def test_missing_symlink_and_same_size_restored_mtime_originals_do_not_borrow_old_manifest(self):
        for damage in ("missing", "symlink", "ctime"):
            with self.subTest(damage=damage):
                memory, _ = self.memory(4)
                path = memory.root / "events" / "00000001.json"
                if damage == "missing":
                    path.unlink()
                    message = "RESEARCH_EVENTS_INVALID"
                elif damage == "symlink":
                    outside = memory.root.parent / "outside-original.json"
                    atomic_bytes(outside, path.read_bytes())
                    path.unlink()
                    path.symlink_to(outside)
                    message = "SYMLINK_PATH_REFUSED"
                else:
                    before = path.stat()
                    raw = path.read_bytes()
                    changed = raw.replace(b'Disposable type 0', b'Disposable type 9', 1)
                    self.assertNotEqual(raw, changed)
                    self.assertEqual(len(raw), len(changed))
                    # An intentionally corrupted fixture retains inode, size,
                    # and mtime. ctime still exposes the changed original.
                    with path.open("r+b") as stream:
                        stream.write(changed)
                    os.utime(path, ns=(before.st_atime_ns, before.st_mtime_ns))
                    message = "RESEARCH_EVENTS_INVALID"
                with self.assertRaisesRegex(SafetyError, message):
                    memory.snapshot()

    def test_derived_checkpoint_tamper_is_freshly_checked_not_supplied_by_manifest(self):
        memory, expected = self.memory(4)
        checkpoint = segments._path(memory, memory._research_pages_verified["root"]["checkpoint"])
        atomic_json(checkpoint, {"unaccepted": "A changed disposable checkpoint"})
        with patch.object(memory, "_fingerprint", wraps=memory._fingerprint) as fingerprints:
            self.assertEqual(memory.snapshot(), expected)
        self.assertIn(checkpoint, [Path(call.args[0]) for call in fingerprints.call_args_list])
        self.assertEqual(memory.last_checkpoint_diagnostic, "RESEARCH_SEGMENT_HASH_CHANGED")
        self.assertIsNone(memory._research_pages_verified)

    def test_original_edit_after_scan_keeps_selected_prefix_but_next_locked_save_rejects(self):
        memory, expected = self.memory(4)
        original_replay = memory._replay_state
        injected = []
        def edit_after_scan(*args, **options):
            result = original_replay(*args, **options)
            if not injected:
                injected.append(True)
                self.corrupt_first_original(memory)
            return result
        with patch.object(memory, "_replay_state", side_effect=edit_after_scan):
            self.assertEqual(memory.snapshot(), expected)
        self.assertEqual(injected, [True])
        self.assertIsNone(memory.last_checkpoint_diagnostic)
        # No atomic-filesystem claim: the snapshot selected the valid prefix
        # before the uncooperative edit. The later locked write starts fresh.
        with self.assertRaisesRegex(SafetyError, "RESEARCH_EVENTS_INVALID"):
            memory.put("concepts", "must-not-write", {"label": "Not accepted"})
        self.assertEqual(len(self.originals(memory)), 4)
        self.assertNotIn("must-not-write", memory._verified["state"]["concepts"])
        with self.assertRaisesRegex(SafetyError, "RESEARCH_EVENTS_INVALID"):
            memory.snapshot()

    def test_derived_edit_in_same_post_scan_window_is_detected_immediately(self):
        memory, expected = self.memory(4)
        original_replay = memory._replay_state
        checkpoint = segments._path(memory, memory._research_pages_verified["root"]["checkpoint"])
        injected = []
        def edit_after_scan(*args, **options):
            result = original_replay(*args, **options)
            if not injected:
                injected.append(True)
                atomic_json(checkpoint, {"unaccepted": "Changed after the original scan"})
            return result
        with patch.object(memory, "_replay_state", side_effect=edit_after_scan):
            self.assertEqual(memory.snapshot(), expected)
        self.assertEqual(injected, [True])
        self.assertEqual(memory.last_checkpoint_diagnostic, "RESEARCH_SEGMENT_HASH_CHANGED")
        self.assertIsNone(memory._research_pages_verified)

    def test_missing_cached_path_not_in_manifest_still_gets_fresh_file_check(self):
        memory, expected = self.memory(4)
        absent = memory.root / "events" / "00000099.json"
        memory._research_pages_verified["files"][absent] = (0, 0, 0, 0, 0)
        with patch.object(memory, "_fingerprint", wraps=memory._fingerprint) as fingerprints:
            self.assertEqual(memory.snapshot(), expected)
        self.assertIn(absent, [Path(call.args[0]) for call in fingerprints.call_args_list])
        self.assertEqual(memory.last_checkpoint_diagnostic, "SAVED_FILE_UNAVAILABLE")
        self.assertIsNone(memory._research_pages_verified)

    def test_exception_and_owner_interruption_do_not_leave_a_cross_call_manifest_context(self):
        for error in (RuntimeError("Synthetic check failure"), KeyboardInterrupt(), SystemExit(130)):
            with self.subTest(error=type(error).__name__):
                memory, expected = self.memory(4)
                original_keys = set(vars(memory))
                with patch.object(segments, "check", side_effect=error):
                    with self.assertRaises(type(error)) as raised:
                        memory.snapshot()
                self.assertIs(raised.exception, error)
                self.assertEqual(set(vars(memory)), original_keys)
                state, head, sequence, _ = self.metadata(memory)
                with patch.object(memory, "_fingerprint", wraps=memory._fingerprint) as fingerprints:
                    self.assertIsNotNone(segments.check(memory, state, head, sequence))
                self.assertEqual(len(self.original_fingerprints(memory, fingerprints)), sequence + 1)
                self.assertEqual(memory.snapshot(), expected)


if __name__ == "__main__":
    unittest.main()
