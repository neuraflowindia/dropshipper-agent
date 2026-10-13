"""Startup selection reuses verified state without lending execution authority.

All records and operation logs are disposable synthetic fixtures. No real
history, collector, provider, model, scheduled task or process signal is used.
"""

from copy import deepcopy
from datetime import timedelta
import json
import unittest
from unittest.mock import patch

import test_programme as fixtures
import product_radar.programme as programme
from product_radar.models import SafetyError, timestamp
from product_radar.operations import operation_run, read_operation_status
from product_radar.research_memory import key
from product_radar.storage import atomic_json, load_json
from product_radar.work_turns import (
    DEFAULT_POLICY, bind_work_turn, open_work_turn, reserve_source_callback,
)


class ProgrammeStartupReadTests(unittest.TestCase):
    def setUp(self):
        self.fixture = fixtures.ProgrammeTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.memory, self.root, self.now = (
            self.fixture.memory, self.fixture.root, self.fixture.now)
        self.record = self.fixture.prepare()

    def open_original(self, programme_id="whole-programme", *, spend=False):
        turn = open_work_turn(self.memory, programme_id, at=self.now,
            policy=dict(DEFAULT_POLICY, source_callbacks=3, exploration_slots=0))
        if spend:
            with bind_work_turn(self.memory, turn["id"]):
                reserve_source_callback(self.memory, "synthetic-already-reserved", at=self.now)
        return deepcopy(self.memory.snapshot()["batch_runs"][turn["id"]])

    def select(self, **options):
        return programme._due_programme_locked(self.memory, as_of=options.pop("as_of", self.now),
            cooperative=True, **options)

    def advance(self, **options):
        return programme.advance_ready_programme_turn(self.memory, root=self.root,
            as_of=self.now, handlers={}, steps=self.fixture.noops(), **options)

    def rows(self, recorder):
        path = recorder.folder / (recorder.run_id + ".jsonl")
        return [json.loads(line) for line in path.read_text().splitlines()]

    def assert_selection_phase(self, rows):
        selected = [row for row in rows if row.get("phase") == "programme_selection"]
        self.assertEqual([row["event"] for row in selected], ["phase_started", "phase_finished"])
        self.assertTrue(all(row["stage"] == "preparation" for row in selected))
        self.assertTrue(all(row.get("deadline_seconds") is None for row in selected))
        self.assertTrue(all(row.get("useful_result") is False for row in selected))
        self.assertGreaterEqual(selected[1]["timings"]["programme_selection"], 0)
        return selected

    def test_supplied_state_open_selector_uses_no_snapshot_and_preserves_allowance(self):
        original = self.open_original(spend=True)
        state = self.memory.snapshot_with_provenance()
        frozen = deepcopy(state)
        with patch.object(self.memory, "snapshot", side_effect=AssertionError("Redundant selector read")):
            chosen = self.select(state=state["state"],
                readiness=lambda _record: self.fail("Open original does not need selection readiness."),
                inventory_refresh=lambda _record: self.fail("Do not refresh an unfinished original turn."))
        self.assertEqual(chosen, self.record)
        self.assertEqual(state, frozen)
        self.assertEqual(self.memory.snapshot_with_provenance(), frozen)
        self.assertEqual(frozen["state"]["batch_runs"][original["id"]], original)

    def test_default_selector_and_checkpoint_loader_still_read_fresh_state(self):
        self.open_original()
        with patch.object(self.memory, "snapshot", wraps=self.memory.snapshot) as snapshots:
            self.assertEqual(self.select(), self.record)
        self.assertEqual(snapshots.call_count, 1)
        with patch.object(self.memory, "snapshot", wraps=self.memory.snapshot) as snapshots:
            self.assertEqual(programme.load_programme(self.memory, "whole-programme"), self.record)
        self.assertEqual(snapshots.call_count, 1)

    def test_supplied_state_recovers_a_staged_only_busy_checkpoint(self):
        with patch.object(self.memory, "put", side_effect=SafetyError("RUNNER_ALREADY_ACTIVE")):
            staged = programme.prepare_programme(self.memory, "staged-only", as_of=self.now)
        self.open_original("staged-only")
        state = self.memory.snapshot_with_provenance()
        self.assertNotIn(key("programme", "staged-only"), state["state"]["batch_runs"])
        with patch.object(self.memory, "snapshot", side_effect=AssertionError("Staged checkpoint nested read")):
            chosen = self.select(state=state["state"])
        self.assertEqual(chosen, staged)
        self.assertEqual(self.memory.snapshot_with_provenance(), state)

    def test_supplied_state_does_not_accept_changed_staged_control_bytes(self):
        self.open_original()
        state = self.memory.snapshot()
        path = programme._control_path(self.memory, "whole-programme")
        staged = load_json(path)
        staged["record"]["source_run_id"] = "invented-source"
        atomic_json(path, staged)
        with patch.object(self.memory, "snapshot", side_effect=AssertionError("Control validation nested read")):
            with self.assertRaisesRegex(SafetyError, "PROGRAMME_CONTROL_INVALID"):
                self.select(state=state)

    def test_supplied_state_retains_conflicting_canonical_control_refusal(self):
        record_id = key("programme", "whole-programme")
        changed = dict(self.record, cycles=900)
        self.memory.put("batch_runs", record_id, changed, previous=self.record)
        state = self.memory.snapshot_with_provenance()
        with patch.object(self.memory, "snapshot", side_effect=AssertionError("Conflict check nested read")):
            with self.assertRaisesRegex(SafetyError, "PROGRAMME_CONTROL_CONFLICT"):
                self.select(state=state["state"])
        self.assertEqual(self.memory.snapshot_with_provenance(), state)

    def test_automatic_startup_reuses_warm_state_but_readiness_and_real_advance_are_fresh(self):
        original = self.open_original(spend=True)
        warm, inspected, stages = [], [], []
        real_warm = programme._warm_research_history
        real_select = programme._due_programme_locked
        real_readiness = programme._programme_turn_readiness

        def warm_then_append(memory):
            state = real_warm(memory)
            warm.append(state)
            # This is a separate synthetic append after warm verification.
            # Selection may use its earlier read, but real work must see it.
            self.fixture.product("later-canonical-tail")
            return state

        def select_without_nested_read(memory, **options):
            self.assertIs(options["state"], warm[0])
            with patch.object(memory, "snapshot", side_effect=AssertionError("Automatic selector nested read")):
                return real_select(memory, **options)

        def inspect_fresh_readiness(memory, record, **options):
            inspected.append(options["state"])
            self.assertIsNot(options["state"], warm[0])
            self.assertIn("later-canonical-tail", options["state"]["concepts"])
            return real_readiness(memory, record, **options)

        steps = self.fixture.noops()
        def actual_stage(memory, _record, _context):
            stages.append(memory.snapshot())
            self.assertIn("later-canonical-tail", stages[-1]["concepts"])
            return {"state": "nothing_new"}
        steps["profiles"] = actual_stage
        with patch.object(programme, "_warm_research_history", side_effect=warm_then_append), \
             patch.object(programme, "_due_programme_locked", side_effect=select_without_nested_read), \
             patch.object(programme, "_programme_turn_readiness", side_effect=inspect_fresh_readiness):
            result = programme.advance_ready_programme_turn(self.memory, root=self.root,
                as_of=self.now, handlers={}, steps=steps)
        self.assertTrue(result["round_completed"])
        self.assertEqual(result["work_turn"]["id"], original["id"])
        self.assertEqual(len(warm), 1)
        self.assertNotIn("later-canonical-tail", warm[0]["concepts"])
        self.assertEqual(len(inspected), 2)
        self.assertTrue(stages)
        final = self.memory.snapshot()["batch_runs"][original["id"]]
        for field in ("policy", "policy_hash", "counters", "reservations", "turn_sequence"):
            self.assertEqual(final[field], original[field])

    def test_later_canonical_control_conflict_is_refused_before_any_stage(self):
        self.open_original()
        real_warm = programme._warm_research_history
        record_id = key("programme", "whole-programme")
        def warm_then_conflict(memory):
            state = real_warm(memory)
            memory.put("batch_runs", record_id, dict(self.record, cycles=900), previous=self.record)
            return state
        reached = []
        steps = {stage: (lambda *_args, stage=stage: reached.append(stage)) for stage in programme.STAGES}
        with patch.object(programme, "_warm_research_history", side_effect=warm_then_conflict):
            with self.assertRaisesRegex(SafetyError, "PROGRAMME_CONTROL_CONFLICT"):
                programme.advance_ready_programme_turn(self.memory, root=self.root,
                    as_of=self.now, handlers={}, steps=steps)
        self.assertEqual(reached, [])
        self.assertEqual(self.memory.snapshot()["batch_runs"][record_id]["cycles"], 900)

    def test_new_week_preparation_refreshes_the_selection_state(self):
        old = self.memory.snapshot()
        later = (timestamp(self.now) + timedelta(days=8)).isoformat()
        current_id = "programme-default-" + programme.research_week(later)
        real_load = programme.load_programme
        loaded = []
        def observe_load(memory, programme_id, **options):
            loaded.append((programme_id, options.get("state")))
            return real_load(memory, programme_id, **options)
        with patch.object(programme, "load_programme", side_effect=observe_load), \
             patch.object(self.memory, "snapshot", wraps=self.memory.snapshot) as snapshots:
            chosen = self.select(state=old, as_of=later,
                readiness=lambda row: {"ready": row["programme_id"] == current_id})
        self.assertEqual(chosen["programme_id"], current_id)
        self.assertEqual(chosen["concept_ids"], [])
        final_state = loaded[-1][1]
        self.assertIsNotNone(final_state)
        self.assertIsNot(final_state, old)
        self.assertIn(key("programme", current_id), final_state["batch_runs"])
        self.assertNotIn(key("programme", current_id), old["batch_runs"])
        self.assertGreater(snapshots.call_count, 0)

    def test_inventory_boundary_mutation_refreshes_the_selection_state(self):
        old = self.memory.snapshot()
        real_load = programme.load_programme
        loaded = []
        def observe_load(memory, programme_id, **options):
            loaded.append(options.get("state"))
            return real_load(memory, programme_id, **options)
        def inventory_refresh(record):
            self.memory.put("source_checks", "synthetic-intake-tail", {"kind": "synthetic-selector-tail"})
            return record
        with patch.object(programme, "load_programme", side_effect=observe_load):
            chosen = self.select(state=old, readiness=lambda _record: {"ready": True},
                inventory_refresh=inventory_refresh)
        self.assertEqual(chosen["programme_id"], "whole-programme")
        self.assertIsNotNone(loaded[-1])
        self.assertIsNot(loaded[-1], old)
        self.assertIn("synthetic-intake-tail", loaded[-1]["source_checks"])
        self.assertNotIn("synthetic-intake-tail", old["source_checks"])

    def test_manual_programme_stays_pinned_and_its_first_load_reuses_warm_state(self):
        warm, loaded = [], []
        real_warm, real_load = programme._warm_research_history, programme.load_programme
        def observe_warm(memory):
            warm.append(real_warm(memory))
            return warm[-1]
        def observe_load(memory, programme_id, **options):
            loaded.append((programme_id, options.get("state")))
            return real_load(memory, programme_id, **options)
        with patch.object(programme, "_warm_research_history", side_effect=observe_warm), \
             patch.object(programme, "load_programme", side_effect=observe_load), \
             patch.object(programme, "_due_programme_locked", side_effect=AssertionError("Manual programme switched selector")):
            result = self.advance(programme_id="whole-programme")
        self.assertEqual(result["programme_id"], "whole-programme")
        self.assertEqual(loaded[0][0], "whole-programme")
        self.assertIs(loaded[0][1], warm[0])

    def test_successful_selection_has_its_own_phase_without_a_deadline_or_result_credit(self):
        self.open_original()
        with operation_run(self.root) as recorder:
            result = self.advance()
        self.assertTrue(result["round_completed"])
        rows = self.rows(recorder)
        selected = self.assert_selection_phase(rows)
        first_readiness = next(row for row in rows if row["event"] == "phase_started"
                               and row.get("phase") == "readiness")
        self.assertLess(rows.index(selected[1]), rows.index(first_readiness))
        self.assertIsNone(recorder.last_useful)
        self.assertEqual(recorder.counts["completed_results"], 0)

    def test_selection_failure_or_owner_interruption_closes_phase_and_propagates_original(self):
        for error in (SafetyError("TEST_SELECTION_REFUSED"), KeyboardInterrupt(), SystemExit(41)):
            with self.subTest(error=type(error).__name__):
                with operation_run(self.root) as recorder, \
                     patch.object(programme, "_due_programme_locked", side_effect=error):
                    with self.assertRaises(type(error)) as raised:
                        self.advance()
                self.assertIs(raised.exception, error)
                self.assert_selection_phase(self.rows(recorder))
                self.assertIsNone(recorder.last_useful)
                self.assertEqual(recorder.counts["completed_results"], 0)
                self.assertIsNone(read_operation_status(self.root).get("phase_deadline_seconds"))

    def test_history_failure_is_not_mislabelled_as_selection_work(self):
        failure = SafetyError("RESEARCH_EVENTS_INVALID")
        with operation_run(self.root) as recorder, \
             patch.object(programme, "_warm_research_history", side_effect=failure):
            with self.assertRaises(SafetyError) as raised:
                self.advance()
        self.assertIs(raised.exception, failure)
        self.assertFalse(any(row.get("phase") == "programme_selection" for row in self.rows(recorder)))
        self.assertEqual(recorder.counts["completed_results"], 0)

    def test_successful_warm_returns_verified_read_and_records_only_filtered_diagnostic(self):
        verified = self.memory.snapshot()
        with operation_run(self.root) as recorder, \
             patch.object(self.memory, "snapshot", return_value=verified) as snapshots, \
             patch.object(self.memory, "last_checkpoint_diagnostic", "RESEARCH_CHECKPOINT_READ_UNAVAILABLE"):
            returned = programme._warm_research_history(self.memory)
        self.assertIs(returned, verified)
        self.assertEqual(snapshots.call_count, 1)
        rows = self.rows(recorder)
        diagnostic = [row for row in rows if row["event"] == "research_checkpoint_diagnostic"]
        self.assertEqual(len(diagnostic), 1)
        self.assertEqual(diagnostic[0]["reason"], "RESEARCH_CHECKPOINT_READ_UNAVAILABLE")
        self.assertEqual(diagnostic[0]["stage"], "saved_history")
        self.assertIs(diagnostic[0]["useful_result"], False)
        self.assertEqual([row["event"] for row in rows if row.get("phase") == "history_verification"],
                         ["phase_started", "phase_finished"])
        self.assertEqual(recorder.counts["completed_results"], 0)
        self.assertIsNone(recorder.last_useful)

    def test_successful_warm_without_diagnostic_does_not_invent_one(self):
        verified = self.memory.snapshot()
        with operation_run(self.root) as recorder, \
             patch.object(self.memory, "snapshot", return_value=verified), \
             patch.object(self.memory, "last_checkpoint_diagnostic", None):
            returned = programme._warm_research_history(self.memory)
        self.assertIs(returned, verified)
        self.assertFalse(any(row["event"] == "research_checkpoint_diagnostic" for row in self.rows(recorder)))
        self.assertEqual(recorder.counts["completed_results"], 0)
        self.assertIsNone(recorder.last_useful)

    def test_successful_warm_does_not_leak_an_unsafe_checkpoint_reason(self):
        verified = self.memory.snapshot()
        unsafe = "Checkpoint failed at https://private.invalid/?token=DO_NOT_LOG_THIS_VALUE"
        with operation_run(self.root) as recorder, \
             patch.object(self.memory, "snapshot", return_value=verified), \
             patch.object(self.memory, "last_checkpoint_diagnostic", unsafe):
            returned = programme._warm_research_history(self.memory)
        self.assertIs(returned, verified)
        rows = self.rows(recorder)
        diagnostic = [row for row in rows if row["event"] == "research_checkpoint_diagnostic"]
        self.assertEqual(len(diagnostic), 1)
        self.assertNotIn("reason", diagnostic[0])
        self.assertNotIn("DO_NOT_LOG_THIS_VALUE", json.dumps(rows))
        self.assertEqual(recorder.counts["completed_results"], 0)
        self.assertIsNone(recorder.last_useful)

    def test_selection_can_contain_readiness_and_finishes_without_result_credit(self):
        # No open turn: the fair selector consults readiness before choosing.
        with operation_run(self.root) as recorder, \
             patch.object(programme, "_refresh_inventory_boundary", side_effect=lambda _memory, row, **_options: row):
            result = self.advance()
        self.assertTrue(result["round_completed"])
        rows = self.rows(recorder)
        selected = self.assert_selection_phase(rows)
        readiness = [row for row in rows if row.get("phase") == "readiness"]
        self.assertGreaterEqual(len(readiness), 4)
        self.assertEqual([row["event"] for row in readiness[:2]], ["phase_started", "phase_finished"])
        self.assertLess(rows.index(selected[0]), rows.index(readiness[0]))
        self.assertLess(rows.index(readiness[0]), rows.index(readiness[1]))
        self.assertLess(rows.index(readiness[1]), rows.index(selected[1]))
        self.assertIsNone(recorder.last_useful)
        self.assertEqual(recorder.counts["completed_results"], 0)

    def test_nested_readiness_failure_closes_both_phases_without_masking_original(self):
        for error in (SafetyError("TEST_READINESS_REFUSED"), KeyboardInterrupt(), SystemExit(42)):
            with self.subTest(error=type(error).__name__):
                with operation_run(self.root) as recorder, \
                     patch.object(programme, "_refresh_inventory_boundary", side_effect=lambda _memory, row, **_options: row), \
                     patch.object(programme, "programme_turn_readiness", side_effect=error):
                    with self.assertRaises(type(error)) as raised:
                        self.advance()
                self.assertIs(raised.exception, error)
                rows = self.rows(recorder)
                selected = self.assert_selection_phase(rows)
                readiness = [row for row in rows if row.get("phase") == "readiness"]
                self.assertEqual([row["event"] for row in readiness], ["phase_started", "phase_finished"])
                self.assertLess(rows.index(selected[0]), rows.index(readiness[0]))
                self.assertLess(rows.index(readiness[0]), rows.index(readiness[1]))
                self.assertLess(rows.index(readiness[1]), rows.index(selected[1]))
                status = read_operation_status(self.root)
                self.assertIsNone(status.get("phase_deadline_seconds"))
                self.assertIsNone(status.get("phase"))
                self.assertIsNone(recorder.last_useful)
                self.assertEqual(recorder.counts["completed_results"], 0)


if __name__ == "__main__":
    unittest.main()
