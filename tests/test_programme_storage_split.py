"""A programme record is saved as small rows, written only when they change.

Synthetic programmes in temporary stores; no website, provider or model.
"""

from copy import deepcopy
import json
from pathlib import Path
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import product_radar.programme as programme
from product_radar.inventory_intake import _changed_subjects, _origins
from product_radar.models import SafetyError, digest
from product_radar.programme import (CLOCK_KIND, FOLLOW_ON_KIND, VERSION, _save, load_programme,
                                     programme_from_state, programme_records, programme_rows)
from product_radar.research_memory import key
import test_operational_backup_selection as selection_fixtures
import test_programme as programme_fixtures


class _Programme:
    def setUp(self):
        self.fixture = programme_fixtures.ProgrammeTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.memory = self.fixture.memory
        self.fixture.prepare(); self.fixture.product("later"); self.fixture.product("newest")
        admitted = ["later"]
        discovery = lambda *_args: {"state": "checked", "new_concept_ids": list(admitted)}
        self.fixture.advance(discovery_step=discovery, max_cycles=1)
        admitted.append("newest")
        self.fixture.advance(discovery_step=discovery, max_cycles=1)
        self.record = load_programme(self.memory, "whole-programme")
        self.assertEqual(len(self.record["follow_on_reviews"]), 2)

    def last_event(self):
        sequence = self.memory._verified["sequence"]
        path = self.memory.root / "events" / f"{sequence:08d}.json"
        return json.loads(path.read_bytes()), path.stat().st_size

    def saved(self, record):
        sequence = self.memory._verified["sequence"]
        result = _save(self.memory, record)
        event, size = self.last_event()
        return result, (event if self.memory._verified["sequence"] > sequence else None), size

    def kinds(self, event):
        return sorted(self.memory.snapshot()["batch_runs"][item["id"]]["kind"] for item in event["changes"])


class ProgrammeStorageSplitTests(_Programme, unittest.TestCase):
    def test_a_group_update_writes_only_that_group(self):
        scope_id = sorted(self.record["follow_on_reviews"])[0]
        self.record["follow_on_reviews"][scope_id]["last_results"]["fit"] = {"state": "checked"}
        _, event, size = self.saved(self.record)
        self.assertEqual([item["id"] for item in event["changes"]],
                         [programme._follow_on_row_id("whole-programme", scope_id)])
        self.assertLess(size, 20 * 1024)
        # With its update time as well: the group row plus the small clock row.
        self.record["follow_on_reviews"][scope_id]["updated_at"] = "2026-10-16T12:00:00+00:00"
        self.record["follow_on_reviews"][scope_id]["deferrals"]["fit:x"] = {"next_retry_at": None}
        _, event, _ = self.saved(self.record)
        self.assertEqual(self.kinds(event), [CLOCK_KIND, FOLLOW_ON_KIND])
        self.assertEqual(load_programme(self.memory, "whole-programme"), self.record)

    def test_a_parent_field_change_writes_the_parent_row_only(self):
        self.record["last_results"]["match"] = {"state": "checked", "reason": "SYNTHETIC"}
        _, event, _ = self.saved(self.record)
        self.assertEqual([item["id"] for item in event["changes"]], [key("programme", "whole-programme")])
        parent = self.memory.snapshot()["batch_runs"][key("programme", "whole-programme")]
        self.assertNotIn("follow_on_reviews", parent)
        self.assertEqual(parent["follow_on_review_ids"], sorted(self.record["follow_on_reviews"]))
        self.assertNotIn("updated_at", parent)

    def test_an_update_time_only_save_is_one_small_clock_row(self):
        self.record["updated_at"] = "2026-10-16T12:00:00+00:00"
        self.record["acquisition_cursor"] = {"last": 0, "ceiling": 2}
        _, event, size = self.saved(self.record)
        self.assertEqual(self.kinds(event), [CLOCK_KIND])
        self.assertLess(size, 3 * 1024)
        self.assertEqual(self.saved(self.record)[1], None)  # nothing changed: no event at all
        self.assertEqual(load_programme(self.memory, "whole-programme"), self.record)

    def test_load_gives_back_the_saved_record_with_and_without_the_staged_control(self):
        self.record["status"] = "running"; self.record["round_progress"] = {"starts": 2}
        self.saved(self.record)
        self.assertEqual(load_programme(self.memory, "whole-programme"), self.record)
        programme._control_path(self.memory, "whole-programme").unlink()
        self.assertEqual(load_programme(self.memory, "whole-programme"), self.record)
        state = self.memory.snapshot()
        self.assertEqual(programme_from_state(state, state["batch_runs"][key("programme", "whole-programme")]), self.record)

    def test_a_busy_save_keeps_the_staged_record_and_the_next_save_writes_the_difference(self):
        before = deepcopy(self.record)
        self.record["last_results"]["match"] = {"state": "checked"}
        with patch.object(self.memory, "commit", side_effect=SafetyError("RUNNER_ALREADY_ACTIVE")):
            self.assertEqual(_save(self.memory, self.record)["saved"], "local_checkpoint")
        state = self.memory.snapshot()
        self.assertEqual(programme_from_state(state, state["batch_runs"][key("programme", "whole-programme")]), before)
        self.assertEqual(load_programme(self.memory, "whole-programme"), self.record)
        self.record["follow_on_reviews"][sorted(self.record["follow_on_reviews"])[0]]["status"] = "running"
        cache = programme._save_cache(self.memory, "whole-programme")
        self.assertEqual(_save(self.memory, self.record, cache=cache)["saved"], "canonical")
        self.assertEqual(cache["canonical"], self.record)
        self.assertEqual(load_programme(self.memory, "whole-programme"), self.record)

    def test_a_record_saved_with_its_groups_inside_still_loads_and_converts_on_save(self):
        programme._control_path(self.memory, "whole-programme").unlink()
        embedded = deepcopy(self.record); embedded["kind"] = VERSION
        state = self.memory.snapshot()
        row_id = key("programme", "whole-programme")
        self.memory.put("batch_runs", row_id, embedded, previous=state["batch_runs"][row_id])
        self.assertEqual(load_programme(self.memory, "whole-programme"), self.record)
        self.record["status"] = "running"
        _, event, _ = self.saved(self.record)
        self.assertIn(row_id, [item["id"] for item in event["changes"]])
        self.assertEqual(load_programme(self.memory, "whole-programme"), self.record)
        self.assertNotIn("follow_on_reviews", self.memory.snapshot()["batch_runs"][row_id])

    def test_readers_see_whole_records_and_never_a_group_row_as_a_programme(self):
        state = self.memory.snapshot()
        records = programme_records(state)
        self.assertEqual(records, [self.record])
        self.assertEqual(programme_records(state, client_id="other"), [])
        self.assertEqual(sorted(row["kind"] for row in state["batch_runs"].values()
                                if row.get("kind", "").startswith("research-programme")),
                         [CLOCK_KIND, FOLLOW_ON_KIND, FOLLOW_ON_KIND, VERSION])
        origins = _origins(state)
        self.assertEqual(set(origins), {"first", "second", "later", "newest"})
        self.assertNotEqual(origins["later"]["scope_id"], "whole-programme")
        _, pool, programmes, _ = programme._meter_inputs(self.memory, as_of=self.fixture.now, failures={})
        self.assertEqual(programmes, [self.record])
        for kind in (CLOCK_KIND, FOLLOW_ON_KIND):
            row_id, row = next((rid, row) for rid, row in state["batch_runs"].items() if row["kind"] == kind)
            self.assertEqual(_changed_subjects(state, {"table": "batch_runs", "id": row_id, "value": row,
                                                       "previous_hash": None}), [])

    def test_binding_guard_and_damaged_rows_are_refused(self):
        changed = deepcopy(self.record)
        child = changed["follow_on_reviews"][sorted(changed["follow_on_reviews"])[0]]
        child["source_run_id"] = "invented-new-source"
        with self.assertRaisesRegex(SafetyError, "PROGRAMME_BINDING_CHANGED"):
            _save(self.memory, changed)
        state = self.memory.snapshot()
        parent = state["batch_runs"][key("programme", "whole-programme")]
        scope_id = parent["follow_on_review_ids"][0]
        damaged = [
            lambda rows: rows.pop(programme._follow_on_row_id("whole-programme", scope_id)),
            lambda rows: rows[programme._follow_on_row_id("whole-programme", scope_id)].update(parent_programme_id="other"),
            lambda rows: rows[programme._follow_on_row_id("whole-programme", scope_id)].update(scope_id="other"),
            lambda rows: rows.pop(programme._clock_row_id("whole-programme")),
            lambda rows: rows[programme._clock_row_id("whole-programme")]["fields"].update(unknown=1),
            lambda rows: rows[programme._clock_row_id("whole-programme")]["follow_on_updated_at"].update(unknown="x"),
            lambda rows: rows[key("programme", "whole-programme")].update(follow_on_review_ids=[scope_id, scope_id]),
        ]
        for damage in damaged:
            copy = deepcopy(state); damage(copy["batch_runs"])
            with self.assertRaisesRegex(SafetyError, "PROGRAMME_CONTROL_INVALID"):
                programme_from_state(copy, copy["batch_runs"][key("programme", "whole-programme")])
        with self.assertRaisesRegex(SafetyError, "PROGRAMME_CONTROL_INVALID"):
            programme_rows(dict(self.record, follow_on_reviews=["not", "a", "mapping"]))
        with self.assertRaisesRegex(SafetyError, "PROGRAMME_CONTROL_INVALID"):
            programme_rows(dict(self.record, follow_on_reviews={scope_id: "not a scope"}))
        self.assertIsNone(programme_from_state(state, None))

    def test_a_record_without_groups_keeps_that_shape(self):
        bare = {name: value for name, value in self.record.items() if name != "follow_on_reviews"}
        rows = programme_rows(bare)
        state = {"batch_runs": rows}
        self.assertEqual(programme_from_state(state, rows[key("programme", "whole-programme")]), bare)

    def test_values_seen_before_are_new_events_not_replays(self):
        original = deepcopy(self.record)
        for status in ("running", original["status"], "running"):  # A, B, A, B
            self.record["status"] = status
            _, event, _ = self.saved(self.record)
            self.assertIsNotNone(event)
            self.assertEqual(load_programme(self.memory, "whole-programme")["status"], status)
        state = self.memory.snapshot()
        self.assertEqual(state["batch_runs"][key("programme", "whole-programme")]["status"], "running")

    def test_an_already_saved_event_is_fine_only_when_it_holds_these_rows(self):
        self.record["status"] = "running"
        with patch.object(self.memory, "commit", return_value=False):
            with self.assertRaisesRegex(SafetyError, "PROGRAMME_CONTROL_CONFLICT"):
                _save(self.memory, self.record)
        self.assertEqual(_save(self.memory, self.record)["saved"], "canonical")
        self.record["status"] = "prepared"
        rows = programme_rows(self.record)
        real_commit = self.memory.commit
        def replayed(event_id, changes, **options):
            real_commit(event_id, changes, **options)  # the write happened; its reply says "already saved"
            return False
        with patch.object(self.memory, "commit", side_effect=replayed):
            self.assertEqual(_save(self.memory, self.record)["saved"], "canonical")
        self.assertEqual(self.memory.snapshot()["batch_runs"][key("programme", "whole-programme")],
                         rows[key("programme", "whole-programme")])


class SplitBackupSelectionTests(unittest.TestCase):
    setUp = selection_fixtures.EssentialBackupSelectionTests.setUp
    select = selection_fixtures.EssentialBackupSelectionTests.select
    json = selection_fixtures.EssentialBackupSelectionTests.json
    files = selection_fixtures.EssentialBackupSelectionTests.files

    def control(self, record, previous=None):
        return self.json("data/research/captured/programme_runs/" + key("programme", record["programme_id"])
                         + "/pending-control.json",
                         {"record": record, "record_hash": digest(record), "previous_hash": previous})

    def split_programme(self):
        child = {"kind": VERSION, "programme_id": "programme-followon-one", "parent_programme_id": "programme-one",
                 "source_run_id": "source-two", "concept_ids": ["concept-two"], "week": "2026-10-05",
                 "client_id": "client-one", "release_slots": [], "updated_at": "2026-10-16T10:00:00+00:00"}
        record = {"kind": VERSION, "programme_id": "programme-one", "source_run_id": "source-one",
                  "concept_ids": [], "week": "2026-10-05", "client_id": "client-one", "release_slots": [],
                  "location": None, "drafts": {}, "updated_at": "2026-10-16T10:00:00+00:00",
                  "follow_on_reviews": {"programme-followon-one": child}}
        self.state["batch_runs"].update(programme_rows(record))
        return record

    def test_split_record_control_is_accepted_and_a_changed_group_is_not(self):
        record = self.split_programme()
        path = self.control(record)
        selected = self.select(pending_controls=[{"path": str(path), "record_hash": digest(record)}])
        self.assertEqual(selected["unsafe"], [])
        self.assertIn(path.relative_to(self.root).as_posix(), self.files(selected))
        changed = deepcopy(record)
        changed["follow_on_reviews"]["programme-followon-one"]["concept_ids"] = ["future-product"]
        changed["concept_ids"] = ["future-product"]
        self.control(changed, digest(record))
        selected = self.select()
        self.assertIn("BACKUP_SELECTION_PROGRAMME_CONTROL_INVALID", {row["reason"] for row in selected["unsafe"]})


if __name__ == "__main__":
    unittest.main()
