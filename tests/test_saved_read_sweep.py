"""The daily sweep that sets aside saved reads a power cut emptied (R15).

Synthetic saved reads only (the acquisition tests' harness); nothing reads a
website, provider or model.
"""

from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "tests"))

from product_radar import saved_read_sweep as sweep
from product_radar.models import SafetyError
from product_radar.research_memory import ResearchMemory
from product_radar.runtime_storage import runtime_path
from product_radar.source_acquisitions import mark_source_dispatching
from product_radar.storage import Journal, atomic_json, load_json
from product_radar.store_housekeeping import housekeeping_line
import test_source_acquisitions as acquisitions  # imported as a module: its own tests are not run again here
from test_source_acquisitions import AFTER, LATER, NOW

TODAY = "2026-10-16T08:00:00+00:00"  # 13:30 in India
TOMORROW = "2026-10-17T08:00:00+00:00"


class SavedReadSweepTests(unittest.TestCase):
    # The acquisition tests' own helpers (claim, a saved response, a lost file).
    _helpers = acquisitions.SourceAcquisitionTests
    claim, row, owners, save_receipt, response, lose, saved = (
        _helpers.claim, _helpers.row, _helpers.owners, _helpers.save_receipt, _helpers.response, _helpers.lose,
        _helpers.saved)

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        (self.root / "data").mkdir()
        self.memory = ResearchMemory(self.root / "captured", Journal(self.root / "finance"), evidence_type="synthetic")
        self.route = "amazon-search"
        self.request = {"query": "draining sink caddy", "country": "IN", "limit": 30}
        sweep._swept_in_this_process = False
        self.addCleanup(setattr, sweep, "_swept_in_this_process", False)
        events = patch("product_radar.operations.record_event")
        self.events = events.start(); self.addCleanup(events.stop)

    def test_a_lost_read_nobody_asks_for_is_set_aside_once_and_nothing_is_deleted(self):
        first, folder = self.saved()
        self.lose(folder / "snapshot.json", b"\x00" * 10)  # zero-filled by a cut-short write
        outcome = sweep.sweep_damaged_reads(self.memory, as_of=AFTER)
        self.assertEqual({name: outcome[name] for name in ("state", "set_aside", "repeated", "changed")},
                         {"state": "done", "set_aside": 1, "repeated": 0, "changed": 0})
        self.assertEqual(outcome["set_aside_ids"], [first["acquisition_id"]])
        self.assertEqual(self.row(first)["state"], "set_aside")
        self.assertTrue((folder / "snapshot.json").exists() and (folder / "receipt.json").exists())
        self.assertEqual(len(self.owners()), 1)  # no replacement is reserved until a question asks again
        before = self.memory.snapshot()
        again = sweep.sweep_damaged_reads(self.memory, as_of=LATER)  # idempotent
        self.assertEqual((again["set_aside"], again["checked"]), (0, 0))
        self.assertEqual(self.memory.snapshot(), before)
        # The next question that asks gets its one fresh read, as when it is found by a claim.
        self.assertEqual(self.claim(owner_id="question-two", as_of=LATER)["generation"], 2)

    def test_intact_changed_and_missing_files_are_never_set_aside(self):
        first, folder = self.saved()
        outcome = sweep.sweep_damaged_reads(self.memory, as_of=AFTER)
        self.assertEqual((outcome["checked"], outcome["set_aside"]), (1, 0))
        self.lose(folder / "snapshot.json", b"\x00" * 8 + b'{"edited": true}')  # not all zeros: no hint at all
        self.assertEqual(sweep.sweep_damaged_reads(self.memory, as_of=AFTER)["changed"], 0)
        self.lose(folder / "snapshot.json")  # emptied, but beside it the receipt was edited: a real change
        self.lose(folder / "receipt.json", b'{"edited": true}')
        outcome = sweep.sweep_damaged_reads(self.memory, as_of=AFTER)
        self.assertEqual((outcome["set_aside"], outcome["changed"], outcome["changed_ids"]), (0, 1, [first["acquisition_id"]]))
        self.assertEqual(self.row(first)["state"], "response_saved")  # still blocking, as before
        (folder / "snapshot.json").unlink()  # a missing file proves nothing (a drive may be away)
        outcome = sweep.sweep_damaged_reads(self.memory, as_of=AFTER)
        self.assertEqual((outcome["set_aside"], outcome["changed"]), (0, 0))
        self.assertFalse(sweep._suspect(folder / "snapshot.json"))
        self.lose(folder / "snapshot.json")
        with patch("product_radar.source_acquisitions.set_aside_damaged_acquisition",
                   side_effect=SafetyError("RESEARCH_EVENTS_INVALID")):
            with self.assertRaisesRegex(SafetyError, "RESEARCH_EVENTS_INVALID"):  # damaged records stop the sweep
                sweep.sweep_damaged_reads(self.memory, as_of=AFTER)
        # One odd read (a busy store, an unreadable file) never stops the others: counted, looked at again.
        for error in (SafetyError("RUNNER_ALREADY_ACTIVE"), OSError("drive away")):
            with patch("product_radar.source_acquisitions.set_aside_damaged_acquisition", side_effect=error):
                outcome = sweep.sweep_damaged_reads(self.memory, as_of=AFTER)
            self.assertEqual((outcome["state"], outcome["unproven"], outcome["set_aside"]), ("done", 1, 0))

    def test_a_replacement_lost_again_is_counted_and_shown_to_the_owner(self):
        first, folder = self.saved()
        self.lose(folder / "snapshot.json")
        second = self.claim(owner_id="question-two", as_of=AFTER)
        mark_source_dispatching(self.memory, second["acquisition_id"], as_of=AFTER)
        self.response(second)
        self.lose(self.memory.root / second["acquisition_id"] / "snapshot.json")
        with patch("product_radar.saved_read_sweep.utc_now", return_value=TODAY):
            outcome = sweep.run_due_saved_read_sweep(self.memory, root=self.root)
        self.assertEqual((outcome["set_aside"], outcome["repeated"], outcome["repeated_ids"]),
                         (1, 1, [second["acquisition_id"]]))
        line = housekeeping_line(self.root)
        self.assertIn("1 saved research page(s) were lost to a power cut a second time", line)
        self.assertIn("check the computer's power supply", line)
        # The record is sealed and the outcome is in the run log.
        wrapper = load_json(runtime_path(self.root, sweep.CONTROL))
        self.assertEqual((wrapper["value"]["day"], wrapper["value"]["outcome"]["repeated"]), ("2026-10-16", 1))
        self.assertEqual(self.events.call_args.args, ("saved_read_sweep_outcome",))
        self.assertEqual(self.events.call_args.kwargs["counts"]["repeated"], 1)
        # A replacement found set aside by a later sweep is still counted (it never comes back).
        self.assertEqual(sweep.sweep_damaged_reads(self.memory, as_of=LATER)["repeated"], 1)

    def test_once_a_day_and_once_after_each_research_start(self):
        self.saved()
        with patch("product_radar.saved_read_sweep.sweep_damaged_reads", wraps=sweep.sweep_damaged_reads) as runs, \
                patch("product_radar.saved_read_sweep.utc_now", return_value=TODAY):
            self.assertEqual(sweep.run_due_saved_read_sweep(self.memory, root=self.root)["state"], "done")
            self.assertIsNone(sweep.run_due_saved_read_sweep(self.memory, root=self.root))  # done today
            sweep._swept_in_this_process = False  # research restarted (after a power cut)
            self.assertEqual(sweep.run_due_saved_read_sweep(self.memory, root=self.root)["state"], "done")
            self.assertEqual(runs.call_count, 2)
            sweep.run_due_saved_read_sweep(self.memory, root=self.root, now=TOMORROW)  # a new India day
            self.assertEqual(runs.call_count, 3)
        self.assertIsNone(housekeeping_line(self.root))  # nothing lost twice, nothing changed: no line

    def test_a_failed_sweep_never_stops_research_and_is_shown(self):
        with patch("product_radar.saved_read_sweep.sweep_damaged_reads", side_effect=OSError("disk away")):
            outcome = sweep.run_due_saved_read_sweep(self.memory, root=self.root, now=TODAY)
        self.assertEqual(outcome, {"state": "failed", "reason": "SAVED_READ_SWEEP_FAILED"})
        self.assertIn("stopped with an error", housekeeping_line(self.root))
        self.assertFalse(sweep._swept_in_this_process)  # tried again at the next step, the same day
        self.assertEqual(sweep.run_due_saved_read_sweep(self.memory, root=self.root, now=TODAY)["state"], "done")
        self.assertIsNone(housekeeping_line(self.root))
        with patch("product_radar.saved_read_sweep.atomic_json", side_effect=SafetyError("SAVED_FILE_UNAVAILABLE")):
            sweep._swept_in_this_process = False
            self.assertEqual(sweep.run_due_saved_read_sweep(self.memory, root=self.root, now=TOMORROW),
                             {"state": "failed", "reason": "SAVED_FILE_UNAVAILABLE"})
        atomic_json(runtime_path(self.root, sweep.CONTROL), {"value": {"contract": sweep.CONTRACT}, "hash": "x"})
        self.assertIsNone(sweep.sweep_line(self.root))  # an unsealed record shows nothing
        self.assertEqual(sweep._code(SafetyError("not a code")), "SAVED_READ_SWEEP_FAILED")


if __name__ == "__main__":
    unittest.main()
