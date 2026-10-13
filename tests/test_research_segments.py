"""Bounded derived research pages retain the exact canonical event history."""

from copy import deepcopy
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from product_radar.models import SafetyError, canonical, digest
from product_radar.research_memory import ResearchMemory, change
from product_radar.storage import Journal, atomic_json, load_json
import product_radar.research_segments as segments


class ResearchSegmentsTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.memory = self.fresh()

    def fresh(self, namespace="synthetic"):
        return ResearchMemory(self.root / "research", Journal(self.root / "finance"), evidence_type=namespace)

    def seed(self, count=20):
        for number in range(count):
            self.memory.put("concepts", "concept-" + str(number), {"label": "Synthetic type " + str(number)})

    def root_record(self):
        return load_json(self.memory.root / segments.POINTER)

    def checkpoint(self):
        return segments._read(self.memory, self.root_record()["checkpoint"])

    def test_explicit_migration_retains_originals_and_restart_exact_replay(self):
        self.seed()
        original = {path.name: path.read_bytes() for path in (self.memory.root / "events").glob("*.json")}
        state = self.memory.snapshot()
        result = self.memory.migrate_segments()
        self.assertTrue(result["original_events_unchanged"])
        self.assertEqual(self.fresh().snapshot(), state)
        self.assertEqual(original, {path.name: path.read_bytes() for path in (self.memory.root / "events").glob("*.json")})
        self.assertTrue(self.memory.migrate_segments()["reused"])
        self.assertEqual(self.memory.read_records("concepts", ["concept-0", "absent"])["records"],
            {"concept-0": state["concepts"]["concept-0"], "absent": None})
        eid = next(iter(self.memory._verified["ids"]))
        self.assertEqual(self.memory.event_id_record(eid)["record"]["sequence"], 1)
        self.assertFalse(self.fresh().put("concepts", "concept-0", state["concepts"]["concept-0"]))

    def test_reviewed_alias_joins_all_history_without_rewriting_original_ids(self):
        import test_functional_aliases as fixture
        saved = fixture.FunctionalAliasTests(); saved.setUp()
        self.addCleanup(saved.doCleanups)
        memory = saved.memory
        memory.put("published_history", "old-bird", {"status": "published", "concept_id": "bird",
            "issue_id": "old-issue", "client_id": "default"})
        memory.migrate_segments()
        self.assertEqual(memory.novelty_page("bear")["total_rows"], 0)
        saved.accept()
        page = memory.novelty_page("bear")
        self.assertEqual(page["total_rows"], 1)
        self.assertEqual(page["records"][0][1]["concept_id"], "bird")
        self.assertEqual(memory.novelty_page("bird")["total_rows"], 1)
        self.assertEqual(memory.snapshot()["published_history"]["old-bird"]["concept_id"], "bird")

    def test_migrated_append_updates_same_loader_and_never_rewrites_old_index(self):
        self.seed(3); index = (self.memory.root / "index.json").read_bytes()
        self.memory.migrate_segments(); original = self.root_record()
        self.memory.put("concepts", "new-type", {"label": "Synthetic new type"})
        self.assertEqual((self.memory.root / "index.json").read_bytes(), index)
        self.assertEqual(self.root_record()["generation"], original["generation"] + 1)
        self.assertEqual(self.fresh().read_records("concepts", ["new-type"])["records"]["new-type"],
            {"label": "Synthetic new type"})
        eid = next(reversed(self.memory._verified["ids"]))
        self.assertEqual(self.memory.event_id_record(eid)["record"]["sequence"], 4)

    def test_projection_write_failure_does_not_repeat_or_erase_canonical_commit(self):
        self.seed(2); self.memory.migrate_segments()
        oldroot = self.root_record(); original = segments.atomic_json
        def fail_pointer(path, value):
            if Path(path).name == segments.POINTER: raise OSError("Synthetic projection unavailable")
            return original(path, value)
        with patch.object(segments, "atomic_json", side_effect=fail_pointer):
            self.assertTrue(self.memory.put("concepts", "saved", {"value": 3}))
        self.assertEqual(self.root_record(), oldroot)
        self.assertEqual(self.memory.last_commit_diagnostic, "RESEARCH_CHECKPOINT_WRITE_UNAVAILABLE")
        self.assertEqual(self.fresh().snapshot()["concepts"]["saved"], {"value": 3})
        self.assertFalse(self.memory.put("concepts", "saved", {"value": 3}))
        self.assertTrue(self.memory.rebuild_index())
        self.assertEqual(self.fresh().read_records("concepts", ["saved"])["records"]["saved"], {"value": 3})

    def test_lost_pointer_reply_is_not_another_event_or_migration(self):
        self.seed(2); self.memory.migrate_segments(); original = segments.atomic_json
        def lose_reply(path, value):
            original(path, value)
            if Path(path).name == segments.POINTER: raise OSError("Synthetic lost pointer reply")
        with patch.object(segments, "atomic_json", side_effect=lose_reply):
            self.assertTrue(self.memory.put("concepts", "saved", {"value": 3}))
        self.assertEqual(self.root_record()["sequence"], 3)
        self.assertFalse(self.fresh().put("concepts", "saved", {"value": 3}))
        self.assertEqual(len(list((self.memory.root / "events").glob("*.json"))), 3)

    def test_missing_or_corrupt_checkpoint_falls_back_not_empty_history(self):
        self.seed(3); expected = self.memory.snapshot(); self.memory.migrate_segments()
        object_path = segments._path(self.memory, self.root_record()["checkpoint"])
        object_path.unlink()
        current = self.fresh()
        self.assertEqual(current.snapshot(), expected)
        self.assertIsNotNone(current.last_checkpoint_diagnostic)
        self.assertFalse(current.read_records("concepts", ["concept-0"])["checkpoint_used"])
        self.assertTrue(current.rebuild_index())
        atomic_json(self.memory.root / segments.POINTER, {"invented": "root"})
        current = self.fresh()
        self.assertEqual(current.snapshot(), expected)
        self.assertTrue(current.rebuild_index())
        self.assertTrue(current.read_records("concepts", ["concept-0"])["checkpoint_used"])

    def test_self_hashed_forged_page_cannot_replace_canonical_state(self):
        self.seed(3); self.memory.migrate_segments()
        root = self.root_record(); checkpoint = self.checkpoint()
        fake = segments._save(self.memory, {"contract": "research-page-v1", "table": "concepts", "prefix": "",
            "rows": [["invented", {"label": "Not a saved original"}]]})
        checkpoint["tables"]["concepts"] = {"root": fake, "rows": 1}
        root["checkpoint"] = segments._save(self.memory, checkpoint)
        root["hash"] = digest({k: v for k, v in root.items() if k != "hash"})
        atomic_json(self.memory.root / segments.POINTER, root)
        reader = self.fresh()
        self.assertNotIn("invented", reader.snapshot()["concepts"])
        self.assertEqual(reader.last_checkpoint_diagnostic, "RESEARCH_CHECKPOINT_CANONICAL_MISMATCH")

    def test_original_tamper_or_missing_history_is_not_checkpoint_fallback(self):
        self.seed(3); self.memory.migrate_segments()
        path = self.memory.root / "events" / "00000001.json"
        event = load_json(path); event["changes"][0]["value"]["label"] = "Changed"
        atomic_json(path, event)
        with self.assertRaisesRegex(SafetyError, "RESEARCH_EVENTS_INVALID"): self.memory.snapshot()
        path.unlink()
        with self.assertRaisesRegex(SafetyError, "RESEARCH_EVENTS_INVALID"): self.fresh().snapshot()

    def test_pages_have_no_skips_duplicates_or_cross_head_cursor_reuse(self):
        self.seed(260); self.memory.migrate_segments()
        rows, cursor = {}, None
        while True:
            page = self.memory.read_table_page("concepts", limit=17, cursor=cursor)
            for rid, row in page["records"]:
                self.assertNotIn(rid, rows); rows[rid] = row
            cursor = page["next_cursor"]
            if cursor is None: break
        self.assertEqual(rows, self.memory.snapshot()["concepts"])
        cursor = self.memory.read_table_page("concepts", limit=17)["next_cursor"]
        self.memory.put("concepts", "after-page", {"value": 1})
        with self.assertRaisesRegex(SafetyError, "RESEARCH_PAGE_CURSOR_STALE"):
            self.memory.read_table_page("concepts", limit=17, cursor=cursor)

    def test_selected_key_read_uses_bounded_pages_without_copying_whole_cached_state(self):
        self.seed(260); self.memory.migrate_segments(); self.memory.snapshot()
        original = segments._read
        with patch.object(segments, "_read", wraps=original) as reads, \
             patch.object(self.memory, "snapshot", side_effect=AssertionError("No full snapshot read")):
            result = self.memory.read_records("concepts", ["concept-123"])
        self.assertTrue(result["checkpoint_used"])
        self.assertLessEqual(reads.call_count, 4)

    def test_late_derived_page_failure_uses_verified_original_rows_without_new_event(self):
        self.seed(3); self.memory.migrate_segments()
        before = self.memory.snapshot_with_provenance()
        with patch.object(segments, "_lookup", side_effect=OSError("Synthetic read failure after verified view")):
            result = self.memory.read_records("concepts", ["concept-0"])
        self.assertFalse(result["checkpoint_used"])
        self.assertEqual(result["records"]["concept-0"], before["state"]["concepts"]["concept-0"])
        self.assertEqual(self.memory.snapshot_with_provenance()["head"], before["head"])

    def test_novelty_and_id_lookup_late_projection_failure_do_not_forget_history(self):
        self.memory.commit("saved-history", [change("concepts", "known", {"label": "Known"}),
            change("published_history", "publication", {"status": "published", "concept_id": "known", "issue_id": "issue"})])
        self.memory.migrate_segments()
        with patch.object(segments, "_lookup", side_effect=OSError("Synthetic unavailable derived page")):
            self.assertEqual(self.memory.novelty_page("known")["total_rows"], 1)
        self.memory.snapshot()
        with patch.object(segments, "_lookup", side_effect=OSError("Synthetic unavailable ID page")):
            self.assertEqual(self.memory.event_id_record("saved-history")["record"]["sequence"], 1)

    def test_actual_page_deletion_between_verified_view_and_lookup_retains_originals(self):
        self.memory.commit("saved-history", [change("concepts", "known", {"label": "Known"}),
            change("published_history", "publication", {"status": "published", "concept_id": "known", "issue_id": "issue"})])
        self.memory.migrate_segments(); original = segments._lookup
        calls = [lambda: self.memory.read_records("concepts", ["known"]),
                 lambda: self.memory.event_id_record("saved-history"), lambda: self.memory.novelty_page("known")]
        for index, call in enumerate(calls):
            with self.subTest(api=index):
                self.memory.snapshot(); deleted = []
                def disappear(memory, table, ref, rid):
                    if not deleted:
                        path = segments._path(memory, ref); value = segments._read(memory, ref)
                        deleted.append((path, value)); path.unlink()
                    return original(memory, table, ref, rid)
                with patch.object(segments, "_lookup", side_effect=disappear): result = call()
                self.assertEqual(len(deleted), 1)
                if index == 0: self.assertEqual(result["records"]["known"], {"label": "Known"})
                elif index == 1: self.assertEqual(result["record"]["sequence"], 1)
                else: self.assertEqual(result["total_rows"], 1)
                atomic_json(*deleted[0])
        self.assertEqual(self.memory.snapshot_with_provenance()["event_count"], 1)

    def test_actual_page_deletion_between_verified_view_and_iteration_retains_originals(self):
        self.seed(3); self.memory.migrate_segments(); self.memory.snapshot()
        original, deleted = segments._rows, []
        def disappear(memory, table, ref, **kwargs):
            if not deleted:
                path = segments._path(memory, ref); deleted.append(path); path.unlink()
            yield from original(memory, table, ref, **kwargs)
        with patch.object(segments, "_rows", side_effect=disappear):
            page = self.memory.read_table_page("concepts", limit=2)
        self.assertEqual(page["total_rows"], 3)
        self.assertEqual(len(page["records"]), 2)
        self.assertIsNotNone(page["next_cursor"])

    def test_self_hashed_bool_substitution_is_not_equal_to_original_numeric_data(self):
        self.memory.put("concepts", "known", {"count": 1}); self.memory.migrate_segments()
        root = self.root_record(); checkpoint = self.checkpoint()
        checkpoint["tables"]["concepts"] = {"root": segments._save(self.memory, {
            "contract": "research-page-v1", "table": "concepts", "prefix": "", "rows": [["known", {"count": True}]]}), "rows": 1}
        root["checkpoint"] = segments._save(self.memory, checkpoint)
        root["hash"] = digest({k: v for k, v in root.items() if k != "hash"})
        atomic_json(self.memory.root / segments.POINTER, root)
        reader = self.fresh(); result = reader.read_records("concepts", ["known"])
        self.assertFalse(result["checkpoint_used"])
        self.assertIs(type(result["records"]["known"]["count"]), int)

    def test_novelty_retains_all_clients_history_aliases_and_active_holds(self):
        self.memory.commit("synthetic-history", [
            change("concepts", "old", {"status": "merged", "merged_into": "current"}),
            change("concepts", "current", {"description": {"label": "Current narrow type"}}),
            change("published_history", "old-publication", {"status": "published", "client_id": "one", "concept_id": "old", "issue_id": "issue-old"}),
            change("published_history", "other-publication", {"status": "published", "client_id": "two", "concept_id": "current", "issue_id": "issue-two"}),
            change("issue_reservations", "held", {"state": "reserved", "client_id": "one", "products": [{"concept_id": "current"}]}),
            change("issue_reservations", "withdrawn", {"state": "withdrawn", "client_id": "one", "products": [{"concept_id": "old"}]}),
        ])
        self.memory.migrate_segments()
        page = self.memory.novelty_page("current", client_id="one")
        self.assertEqual([rid for rid, _ in page["records"]], ["old-publication"])
        self.assertEqual(self.memory.novelty_page("old", client_id="two")["records"][0][0], "other-publication")
        self.assertEqual(self.memory.novelty_page("old", client_id="one", section="holds")["records"][0][0], "held")
        self.assertEqual({rid for rid, _ in self.memory.novelty_page("current", section="aliases")["records"]}, {"old", "current"})

    def test_alias_cycle_is_not_an_empty_new_type(self):
        self.memory.commit("synthetic-cycle", [change("concepts", "one", {"status": "merged", "merged_into": "two"}),
            change("concepts", "two", {"status": "merged", "merged_into": "one"})])
        self.memory.migrate_segments()
        with self.assertRaisesRegex(SafetyError, "RESEARCH_NOVELTY_ALIAS_UNCLEAR"):
            self.memory.novelty_page("one")

    def test_namespace_change_is_not_checkpoint_relabel(self):
        self.seed(2); self.memory.migrate_segments()
        with self.assertRaisesRegex(SafetyError, "RESEARCH_EVENTS_INVALID"): self.fresh("captured").snapshot()

    def test_failed_candidate_readback_never_publishes_projection_authority(self):
        self.seed(3); original = segments._tree
        def wrong(memory, table, rows, **kwargs):
            if table == "concepts": return original(memory, table, {"invented": {"label": "Not an original"}}, **kwargs)
            return original(memory, table, rows, **kwargs)
        with patch.object(segments, "_tree", side_effect=wrong), \
             self.assertRaisesRegex(SafetyError, "RESEARCH_CHECKPOINT_CANONICAL_MISMATCH"):
            self.memory.migrate_segments()
        self.assertFalse((self.memory.root / segments.POINTER).exists())
        self.assertFalse((self.memory.root / segments.MARKER).exists())
        self.assertEqual(len(self.fresh().snapshot()["concepts"]), 3)

    def test_migration_from_empty_then_first_commit_preserves_nonempty_marker(self):
        self.memory.migrate_segments()
        self.memory.put("concepts", "first", {"label": "First actual saved type"})
        self.assertEqual(load_json(self.memory.root / segments.MARKER)["initial_sequence"], 1)
        (self.memory.root / segments.POINTER).unlink()
        (self.memory.root / "events" / "00000001.json").unlink()
        with self.assertRaisesRegex(SafetyError, "RESEARCH_EVENTS_INVALID"): self.fresh().snapshot()

    def test_fresh_reader_cannot_reset_novelty_after_valid_prefix_truncation(self):
        self.seed(3); self.memory.migrate_segments()
        (self.memory.root / "events" / "00000003.json").unlink()
        with self.assertRaisesRegex(SafetyError, "RESEARCH_EVENTS_INVALID"): self.fresh().snapshot()
        (self.memory.root / "events" / "00000001.json").unlink()
        (self.memory.root / "events" / "00000002.json").unlink()
        with self.assertRaisesRegex(SafetyError, "RESEARCH_EVENTS_INVALID"): self.fresh().snapshot()

    def test_missing_pointer_and_originals_cannot_revert_to_new_legacy_namespace(self):
        self.seed(3); self.memory.migrate_segments()
        (self.memory.root / segments.POINTER).unlink()
        for path in (self.memory.root / "events").glob("*.json"): path.unlink()
        with self.assertRaisesRegex(SafetyError, "RESEARCH_EVENTS_INVALID"): self.fresh().snapshot()

    def test_real_large_state_and_two_thousand_logical_issues_roll_over_restart_and_lookup(self):
        blob = "x" * 50000
        for batch in range(40):
            self.memory.commit("large-batch-" + str(batch), [change("observations", "obs-" + str(batch * 10 + item),
                {"source": "synthetic", "body": blob}) for item in range(10)])
        history = [change("published_history", "history-" + str(number) + "-" + str(product), {
            "status": "published", "client_id": "default", "concept_id": "retained-type-" + str(product),
            "issue_id": "issue-" + str(number), "revision": 1, "ordinal": number + 1})
            for number in range(2000) for product in range(10)]
        self.memory.commit("two-thousand-logical-issues", [change("concepts", "retained-type-" + str(product),
            {"label": "Retained type " + str(product)}) for product in range(10)] + history)
        self.assertGreater(len(canonical(self.memory.snapshot())), 16 * 1024 * 1024)
        self.memory.migrate_segments(); reader = self.fresh()
        saved_history = reader.snapshot()["published_history"]
        self.assertEqual(len(saved_history), 20000)
        self.assertEqual(len({row["issue_id"] for row in saved_history.values()}), 2000)
        page = reader.novelty_page("retained-type-0", limit=10)
        self.assertEqual(page["total_rows"], 2000); self.assertEqual(len(page["records"]), 10)
        self.assertIsNotNone(page["next_cursor"])
        self.assertEqual(reader.event_id_record("two-thousand-logical-issues")["record"]["sequence"], 41)
        checkpoint = self.checkpoint()
        self.assertLess(len(canonical(checkpoint)), 16384)
        self.assertLess(len(canonical(self.root_record())), 16384)
        objects = list((self.memory.root / "research-v1" / "objects").glob("*.json"))
        self.assertTrue(all(path.stat().st_size <= segments.MAX_OBJECT for path in objects))
        with patch.object(segments, "_read", wraps=segments._read) as reads:
            reader.read_records("observations", ["obs-399"])
        self.assertLessEqual(reads.call_count, 6)


if __name__ == "__main__":
    unittest.main()
