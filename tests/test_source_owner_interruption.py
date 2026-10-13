"""Owner interruption keeps original work and releases only the local writer.

Disposable stores and injected collectors only; no real network or model.
"""

from copy import deepcopy
import unittest
from unittest.mock import patch

import test_programme as programme_fixtures
import test_source_coverage as source_fixtures

from product_radar.programme import advance_programme_turn
from product_radar.storage import Journal
from product_radar.work_turns import (
    DEFAULT_POLICY, active_work_turn, bind_work_turn, open_work_turn,
    reserve_source_callback,
)


class SourceOwnerInterruptionTests(unittest.TestCase):
    def source_fixture(self, source_callbacks=1):
        fixture = source_fixtures.SourceCoverageTests()
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        fixture.plan([fixture.question()])
        turn = open_work_turn(fixture.memory, "interrupted-sources",
            at=source_fixtures.NOW, policy=dict(DEFAULT_POLICY, source_callbacks=source_callbacks,
                                               exploration_slots=0))
        return fixture, turn

    def check_unreturned_original(self, interruption, *, dispatch=True, source_callbacks=1):
        fixture, turn = self.source_fixture(source_callbacks)
        calls = []
        error = interruption("Synthetic owner interruption")

        def original(context):
            calls.append(context["acquisition_id"])
            if dispatch:
                context["record_dispatch"]()
            raise error

        with bind_work_turn(fixture.memory, turn["id"]):
            with self.assertRaises(interruption) as raised:
                fixture.run_sources({"meta-ads": original})
            self.assertIs(raised.exception, error)
            state = fixture.memory.snapshot()
            saved_turn = deepcopy(state["batch_runs"][turn["id"]])
            saved_task = deepcopy(next(iter(state["query_tasks"].values())))
            self.assertEqual(saved_turn["state"], "open")
            self.assertEqual(saved_turn["counters"]["source_callbacks"], 1)
            self.assertEqual(saved_task["status"], "dispatching")
            self.assertIs(saved_task["dispatch_started"], dispatch)
            self.assertTrue(saved_task["reconcile_required"])
            self.assertEqual(saved_task["tries"], int(dispatch))
            self.assertEqual(saved_task["acquisition_id"], calls[0])
            resumed = fixture.run_sources({"meta-ads": original})
        self.assertEqual(calls, [saved_task["acquisition_id"]])
        self.assertEqual(resumed["handler_calls"], 0)
        after = fixture.memory.snapshot()
        self.assertEqual(after["batch_runs"][turn["id"]], saved_turn)
        self.assertEqual(next(iter(after["query_tasks"].values())), saved_task)

    def test_keyboard_interruption_keeps_unreturned_original_without_relaunch(self):
        self.check_unreturned_original(KeyboardInterrupt)

    def test_system_exit_keeps_unreturned_original_without_relaunch(self):
        self.check_unreturned_original(SystemExit)

    def test_keyboard_interruption_does_not_relaunch_with_remaining_allowance(self):
        self.check_unreturned_original(KeyboardInterrupt, source_callbacks=2)

    def test_system_exit_does_not_relaunch_with_remaining_allowance(self):
        self.check_unreturned_original(SystemExit, source_callbacks=2)

    def test_keyboard_interruption_before_dispatch_preserves_admitted_intent(self):
        self.check_unreturned_original(KeyboardInterrupt, dispatch=False, source_callbacks=2)

    def test_system_exit_before_dispatch_preserves_admitted_intent(self):
        self.check_unreturned_original(SystemExit, dispatch=False, source_callbacks=2)

    def check_staged_reply(self, interruption):
        fixture, turn = self.source_fixture()
        calls = []
        error = interruption("Synthetic interruption before application")

        def original(context):
            calls.append(context["acquisition_id"])
            return fixture.acquired(context)

        with bind_work_turn(fixture.memory, turn["id"]):
            with patch("product_radar.source_coverage._persist_receipt", side_effect=error):
                with self.assertRaises(interruption) as raised:
                    fixture.run_sources({"meta-ads": original})
            self.assertIs(raised.exception, error)
            before = fixture.memory.snapshot()
            saved_turn = deepcopy(before["batch_runs"][turn["id"]])
            saved_task = deepcopy(next(iter(before["query_tasks"].values())))
            folder = fixture.memory.root / "all_source_dispatch/returned_receipts" / saved_task["acquisition_id"]
            saved_files = {path.name: path.read_bytes() for path in folder.glob("*.json")}
            self.assertEqual(len(saved_files), 2)  # Immutable reply plus its pending-apply pointer.
            self.assertIn("pending.json", saved_files)
            resumed = fixture.run_sources({"meta-ads": original})
        self.assertEqual(resumed["handler_calls"], 0)
        self.assertEqual(resumed["counts"], {"acquired": 1})
        self.assertEqual(calls, [saved_task["acquisition_id"]])
        after = fixture.memory.snapshot()
        final = next(iter(after["query_tasks"].values()))
        self.assertEqual(final["acquisition_id"], saved_task["acquisition_id"])
        self.assertEqual(final["tries"], 1)
        self.assertFalse(final["reconcile_required"])
        self.assertEqual(after["batch_runs"][turn["id"]], saved_turn)
        self.assertEqual({path.name: path.read_bytes() for path in folder.glob("*.json")}, saved_files)

    def test_keyboard_interruption_recovers_staged_reply_with_spent_allowance(self):
        self.check_staged_reply(KeyboardInterrupt)

    def test_system_exit_recovers_staged_reply_with_spent_allowance(self):
        self.check_staged_reply(SystemExit)

    def check_writer_release(self, interruption):
        fixture = programme_fixtures.ProgrammeTests()
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        fixture.prepare()
        steps = fixture.noops()
        seen = []
        error = interruption("Synthetic interrupted acquisition")

        def interrupted(memory, _record, _context):
            reserve_source_callback(memory, "original-owner", at=fixture.now)
            seen.append(deepcopy(active_work_turn(memory)))
            raise error

        steps["acquire"] = interrupted
        options = dict(root=fixture.root, handlers={}, as_of=fixture.now, report_ready=True)
        with self.assertRaises(interruption) as raised:
            advance_programme_turn(fixture.memory, "whole-programme", steps=steps, **options)
        self.assertIs(raised.exception, error)
        self.assertEqual(len(seen), 1)
        original = seen[0]
        self.assertTrue(original["report_ready"])
        self.assertEqual(fixture.memory.snapshot()["batch_runs"][original["id"]], original)
        # The same ordinary lock is obtainable after the interrupted context;
        # no lock-file removal, forced release or replacement writer is used.
        with Journal(fixture.memory.root / "programme_writer").locked():
            pass
        resumed = advance_programme_turn(fixture.memory, "whole-programme",
            steps=fixture.noops(), **dict(options, report_ready=False))
        self.assertEqual(resumed["work_turn"]["id"], original["id"])
        final = fixture.memory.snapshot()["batch_runs"][original["id"]]
        self.assertEqual(final["id"], original["id"])
        self.assertEqual(final["turn_sequence"], original["turn_sequence"])
        self.assertEqual(final["state"], "closed")
        for field in ("policy", "counters", "reservations", "report_ready"):
            self.assertEqual(final[field], original[field])

    def test_keyboard_interruption_releases_writer_and_resumes_same_turn(self):
        self.check_writer_release(KeyboardInterrupt)

    def test_system_exit_releases_writer_and_resumes_same_turn(self):
        self.check_writer_release(SystemExit)


if __name__ == "__main__":
    unittest.main()
