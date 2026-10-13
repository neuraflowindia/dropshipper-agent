"""Disposable host mocks for one exact graceful reload; never signals a host PID."""

from contextlib import ExitStack, redirect_stdout
from copy import deepcopy
import importlib.util
import io
from pathlib import Path
import signal
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from product_radar.models import SafetyError
from product_radar.storage import atomic_json, load_json


class FrozenImplementationReloadTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        path = Path(__file__).resolve().parents[1] / "data/operations/implementation-20261011/reload_frozen_implementation.py"
        spec = importlib.util.spec_from_file_location("isolated_frozen_implementation_reload", path)
        cls.helper = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(cls.helper)

    def setUp(self):
        temporary = tempfile.TemporaryDirectory(); self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.folder = self.root / "data/operations/implementation-20261011"
        self.freeze_path = self.folder / "reviewed-source-freeze.json"
        self.receipt_path = self.folder / "frozen-implementation-reload-proof.json"
        self.activity_path = self.root / "data/operations/research-runs/current.json"
        self.fingerprint = {"files": 12, "sha256": "a" * 64}
        self.freeze = {"ready_for_single_reload": True, "fingerprint": self.fingerprint}
        self.row = {"pid": 604774, "start_ticks": "2276004"}
        self.activity = {"pid": 604774, "process_start_ticks": "2276004", "run_id": "isolated-run",
            "writer_active": True, "step": "fit", "phase": "fit"}
        atomic_json(self.freeze_path, self.freeze)
        atomic_json(self.activity_path, self.activity)
        self.signal_mock = Mock(return_value=True)

    def execute(self, *, fingerprints=None, activities=None, scans=None, owned=None, main_options=None):
        activity_rows = iter(activities) if activities is not None else None
        def read(path, limit):
            if activity_rows is not None and path == self.activity_path:
                return deepcopy(next(activity_rows))
            return load_json(path, limit)
        proc = SimpleNamespace(iterdir=Mock(side_effect=scans or [[], []]))
        paths = Mock(side_effect=lambda name: proc if name == "/proc" else Path(name))
        paths.cwd.return_value = self.root
        def process(pid):
            # Only fabricated /proc entries exist in this test. The target
            # disappears after the mocked signal, without a real process call.
            return {"pid": 100, "parent": 604774} if str(pid) == "100" else None
        with ExitStack() as stack:
            stack.enter_context(patch.object(self.helper, "Path", paths))
            stack.enter_context(patch.object(self.helper, "source_fingerprint",
                side_effect=fingerprints or [self.fingerprint, self.fingerprint]))
            stack.enter_context(patch.object(self.helper, "load_json", side_effect=read))
            stack.enter_context(patch.object(self.helper, "owned_programmes",
                side_effect=owned or [[self.row], [self.row], []]))
            stack.enter_context(patch.object(self.helper, "process_info", side_effect=process))
            stack.enter_context(patch.object(self.helper, "signal_identified", self.signal_mock))
            stack.enter_context(patch.object(self.helper, "time", SimpleNamespace(
                monotonic=Mock(side_effect=[100, 100]),
                sleep=Mock(side_effect=AssertionError("No real waiting is permitted.")))))
            forbidden_command = Mock(side_effect=AssertionError("No host command is permitted."))
            stack.enter_context(patch.object(self.helper, "subprocess", SimpleNamespace(
                check_output=forbidden_command, run=forbidden_command, Popen=forbidden_command)))
            stack.enter_context(redirect_stdout(io.StringIO()))
            return self.helper.main(**(main_options or {}))

    def assert_refused_before_claim(self):
        self.signal_mock.assert_not_called()
        self.assertFalse(self.receipt_path.exists())

    def assert_refused_after_claim(self):
        self.signal_mock.assert_not_called()
        receipt = load_json(self.receipt_path)
        self.assertEqual(receipt["state"], "not_signalled_boundary_changed")
        self.assertFalse(receipt["signal_delivered"])

    def test_missing_freeze_cannot_signal(self):
        self.freeze_path.unlink()
        with self.assertRaisesRegex(SafetyError, "SAVED_FILE_UNAVAILABLE"):
            self.execute()
        self.assert_refused_before_claim()

    def test_wrong_initial_fingerprint_cannot_signal(self):
        with self.assertRaisesRegex(SystemExit, "not frozen"):
            self.execute(fingerprints=[{"files": 13, "sha256": "b" * 64}])
        self.assert_refused_before_claim()

    def test_active_initial_child_cannot_signal(self):
        with self.assertRaisesRegex(SystemExit, "child is active"):
            self.execute(scans=[[Path("100")]])
        self.assert_refused_before_claim()

    def test_initial_model_phase_cannot_signal(self):
        atomic_json(self.activity_path, dict(self.activity, phase="model_call"))
        with self.assertRaisesRegex(SystemExit, "boundary is no longer current"):
            self.execute()
        self.assert_refused_before_claim()

    def test_exact_local_matching_boundary_preserves_same_one_shot_handoff(self):
        atomic_json(self.activity_path, dict(self.activity, step="match", phase="match",
                                           phase_deadline_seconds=None))
        self.assertEqual(self.execute(), 0)
        self.signal_mock.assert_called_once_with(self.row, signal.SIGINT)
        receipt = load_json(self.receipt_path)
        self.assertEqual(receipt["rechecked_activity"]["step"], "match")
        self.assertFalse(receipt["allowances_reset"])
        self.assertFalse(receipt["replacement_manually_started"])

    def test_matching_stage_cannot_hide_a_model_or_website_phase(self):
        for phase in ("model_call", "handler", "source_preparation", None):
            with self.subTest(phase=phase):
                atomic_json(self.activity_path, dict(self.activity, step="match", phase=phase))
                with self.assertRaisesRegex(SystemExit, "boundary is no longer current"):
                    self.execute()
                self.assert_refused_before_claim()

    def test_local_matching_label_with_active_website_deadline_is_refused(self):
        atomic_json(self.activity_path, dict(self.activity, step="match", phase="match",
                                           phase_deadline_seconds=45))
        with self.assertRaisesRegex(SystemExit, "boundary is no longer current"):
            self.execute()
        self.assert_refused_before_claim()

    def test_matching_boundary_is_rechecked_after_receipt_claim(self):
        initial = dict(self.activity, step="match", phase="match", phase_deadline_seconds=None)
        self.assertEqual(self.execute(activities=[initial, dict(initial, phase="handler")]), 1)
        self.assert_refused_after_claim()

    def test_acquisition_handoff_requires_explicit_option(self):
        atomic_json(self.activity_path, dict(self.activity, step="acquire", phase="acquire",
                                           phase_deadline_seconds=None))
        with self.assertRaisesRegex(SystemExit, "boundary is no longer current"):
            self.execute()
        self.assert_refused_before_claim()

    def test_reviewed_acquisition_handoff_preserves_original_requests_and_existing_clock(self):
        atomic_json(self.activity_path, dict(self.activity, step="acquire", phase="acquire",
                                           phase_deadline_seconds=None))
        self.assertEqual(self.execute(main_options={"allow_acquire_reload": True}), 0)
        self.signal_mock.assert_called_once_with(self.row, signal.SIGINT)
        receipt = load_json(self.receipt_path)
        self.assertTrue(receipt["acquisition_reload_enabled"])
        self.assertTrue(receipt["uncertain_original_requests_kept"])
        self.assertFalse(receipt["allowances_reset"])
        self.assertFalse(receipt["replacement_manually_started"])

    def test_acquisition_handoff_cannot_hide_a_nested_website_or_model_phase(self):
        for phase, deadline in (("model_call", None), ("handler", None), ("acquire", 45)):
            with self.subTest(phase=phase, deadline=deadline):
                atomic_json(self.activity_path, dict(self.activity, step="acquire", phase=phase,
                                                   phase_deadline_seconds=deadline))
                with self.assertRaisesRegex(SystemExit, "boundary is no longer current"):
                    self.execute(main_options={"allow_acquire_reload": True})
                self.assert_refused_before_claim()

    def test_acquisition_handoff_is_rechecked_after_receipt_claim(self):
        initial = dict(self.activity, step="acquire", phase="acquire", phase_deadline_seconds=None)
        self.assertEqual(self.execute(activities=[initial, dict(initial, phase="handler")],
                                     main_options={"allow_acquire_reload": True}), 1)
        self.assert_refused_after_claim()

    def test_supplier_handoff_requires_explicit_option(self):
        atomic_json(self.activity_path, dict(self.activity, step="suppliers", phase="suppliers",
                                           phase_deadline_seconds=None))
        with self.assertRaisesRegex(SystemExit, "boundary is no longer current"):
            self.execute()
        self.assert_refused_before_claim()

    def test_reviewed_supplier_handoff_keeps_the_originals_and_existing_clock(self):
        activity = dict(self.activity, step="suppliers", phase="suppliers", phase_deadline_seconds=None)
        atomic_json(self.activity_path, activity)
        original_files = {path: path.read_bytes() for path in self.root.rglob("*.json")}
        self.assertEqual(self.execute(main_options={"allow_supplier_reload": True,
                                                   "expected_run_id": activity["run_id"]}), 0)
        self.signal_mock.assert_called_once_with(self.row, signal.SIGINT)
        receipt = load_json(self.receipt_path)
        self.assertEqual(receipt["rechecked_activity"], activity)
        self.assertTrue(receipt["supplier_reload_enabled"])
        self.assertFalse(receipt["acquisition_reload_enabled"])
        self.assertTrue(receipt["uncertain_original_requests_kept"])
        for field in ("owner_intent_changed", "allowances_reset", "requests_or_results_rewritten",
                      "task_reconfigured", "replacement_manually_started", "release_gate_run"):
            self.assertIs(receipt[field], False)
        for path, original in original_files.items():
            self.assertEqual(path.read_bytes(), original)
        self.assertEqual(set(self.root.rglob("*.json")), set(original_files) | {self.receipt_path})

    def test_supplier_handoff_cannot_hide_a_nested_model_website_or_timed_phase(self):
        for phase, deadline in (("model_call", None), ("handler", None), ("preparation", None),
                                (None, None), ("suppliers", 45), ("suppliers", 0)):
            with self.subTest(phase=phase, deadline=deadline):
                atomic_json(self.activity_path, dict(self.activity, step="suppliers", phase=phase,
                                                    phase_deadline_seconds=deadline))
                with self.assertRaisesRegex(SystemExit, "boundary is no longer current"):
                    self.execute(main_options={"allow_supplier_reload": True})
                self.assert_refused_before_claim()

    def test_supplier_option_does_not_authorize_an_acquisition_or_photo_step(self):
        for step in ("acquire", "photos", "reports", "discovery"):
            with self.subTest(step=step):
                atomic_json(self.activity_path, dict(self.activity, step=step, phase=step,
                                                    phase_deadline_seconds=None))
                with self.assertRaisesRegex(SystemExit, "boundary is no longer current"):
                    self.execute(main_options={"allow_supplier_reload": True})
                self.assert_refused_before_claim()

    def test_supplier_boundary_is_rechecked_after_receipt_claim(self):
        initial = dict(self.activity, step="suppliers", phase="suppliers", phase_deadline_seconds=None)
        changed = dict(initial, step="photos", phase="photos")
        self.assertEqual(self.execute(activities=[initial, changed],
                                     main_options={"allow_supplier_reload": True}), 1)
        self.assert_refused_after_claim()
        self.assertTrue(load_json(self.receipt_path)["supplier_reload_enabled"])

    def test_supplier_handoff_refuses_a_new_model_phase_after_receipt_claim(self):
        initial = dict(self.activity, step="suppliers", phase="suppliers", phase_deadline_seconds=None)
        changed = dict(initial, phase="model_call", phase_deadline_seconds=900)
        self.assertEqual(self.execute(activities=[initial, changed],
                                     main_options={"allow_supplier_reload": True}), 1)
        self.assert_refused_after_claim()

    def test_supplier_handoff_refuses_a_new_child_after_receipt_claim(self):
        atomic_json(self.activity_path, dict(self.activity, step="suppliers", phase="suppliers",
                                           phase_deadline_seconds=None))
        self.assertEqual(self.execute(scans=[[], [Path("100")]],
                                     main_options={"allow_supplier_reload": True}), 1)
        self.assert_refused_after_claim()

    def test_supplier_handoff_refuses_a_changed_process_identity_after_receipt_claim(self):
        atomic_json(self.activity_path, dict(self.activity, step="suppliers", phase="suppliers",
                                           phase_deadline_seconds=None))
        changed = dict(self.row, start_ticks="changed")
        self.assertEqual(self.execute(owned=[[self.row], [changed]],
                                     main_options={"allow_supplier_reload": True}), 1)
        self.assert_refused_after_claim()

    def test_supplier_handoff_refuses_a_changed_run_after_receipt_claim(self):
        initial = dict(self.activity, step="suppliers", phase="suppliers", phase_deadline_seconds=None)
        self.assertEqual(self.execute(activities=[initial, dict(initial, run_id="different-run")],
                                     main_options={"allow_supplier_reload": True}), 1)
        self.assert_refused_after_claim()

    def test_supplier_option_does_not_suppress_a_signal_identity_race(self):
        atomic_json(self.activity_path, dict(self.activity, step="suppliers", phase="suppliers",
                                           phase_deadline_seconds=None))
        self.signal_mock.return_value = False
        self.assertEqual(self.execute(main_options={"allow_supplier_reload": True}), 1)
        self.signal_mock.assert_called_once_with(self.row, signal.SIGINT)
        receipt = load_json(self.receipt_path)
        self.assertEqual(receipt["state"], "original_identity_changed")
        self.assertFalse(receipt["signal_delivered"])

    def test_existing_exclusive_receipt_is_unchanged_and_never_signals_again(self):
        atomic_json(self.receipt_path, {"already_claimed": True})
        original = self.receipt_path.read_bytes()
        with self.assertRaisesRegex(SystemExit, "already exists"):
            self.execute()
        self.assertEqual(self.receipt_path.read_bytes(), original)
        self.signal_mock.assert_not_called()

    def test_final_changed_fingerprint_refuses_signal_and_keeps_one_shot_claim(self):
        self.assertEqual(self.execute(fingerprints=[self.fingerprint,
            {"files": 13, "sha256": "b" * 64}]), 1)
        self.assert_refused_after_claim()

    def test_final_changed_stage_refuses_signal(self):
        self.assertEqual(self.execute(activities=[self.activity, dict(self.activity, step="acquire")]), 1)
        self.assert_refused_after_claim()

    def test_final_child_refuses_signal(self):
        self.assertEqual(self.execute(scans=[[], [Path("100")]]), 1)
        self.assert_refused_after_claim()

    def test_final_model_phase_refuses_signal(self):
        self.assertEqual(self.execute(activities=[self.activity, dict(self.activity, phase="model_call")]), 1)
        self.assert_refused_after_claim()

    def test_final_changed_run_refuses_signal(self):
        self.assertEqual(self.execute(activities=[self.activity, dict(self.activity, run_id="another-run")]), 1)
        self.assert_refused_after_claim()

    def test_exact_success_sends_one_sigint_and_only_records_existing_clock_handoff(self):
        initial = {path.relative_to(self.root): path.read_bytes() for path in self.root.rglob("*.json")}
        self.assertEqual(self.execute(), 0)
        self.signal_mock.assert_called_once_with(self.row, signal.SIGINT)
        receipt = load_json(self.receipt_path)
        self.assertEqual(receipt["state"], "original_exited_waiting_for_existing_clock")
        self.assertTrue(receipt["signal_delivered"])
        self.assertFalse(receipt["supplier_reload_enabled"])
        self.assertEqual(receipt["original_pid"], 604774)
        self.assertEqual(receipt["original_start_ticks"], "2276004")
        self.assertEqual(receipt["rechecked_activity"], self.activity)
        for field in ("owner_intent_changed", "allowances_reset", "requests_or_results_rewritten",
                "task_reconfigured", "replacement_manually_started", "release_gate_run"):
            self.assertIs(receipt[field], False)
        for path, original in initial.items():
            self.assertEqual((self.root / path).read_bytes(), original)
        self.assertEqual({path.relative_to(self.root) for path in self.root.rglob("*.json")},
            set(initial) | {self.receipt_path.relative_to(self.root)})
        with self.assertRaisesRegex(SystemExit, "already exists"):
            self.execute()
        self.signal_mock.assert_called_once()

    def test_later_named_review_has_distinct_identity_freeze_and_exclusive_receipt(self):
        original_receipt = {"state": "original_exited_waiting_for_existing_clock", "dated_first_reload": True}
        atomic_json(self.receipt_path, original_receipt)
        original_bytes = self.receipt_path.read_bytes()
        next_freeze = self.folder / "later-reviewed-source.json"
        atomic_json(next_freeze, self.freeze)
        later_row = {"pid": 700001, "start_ticks": "3000000"}
        later_activity = dict(self.activity, pid=later_row["pid"],
            process_start_ticks=later_row["start_ticks"], run_id="later-reviewed-run")
        options = {"freeze_name": next_freeze.name, "receipt_name": "later-reload-proof.json",
            "expected_pid": later_row["pid"], "expected_start": later_row["start_ticks"],
            "expected_run_id": later_activity["run_id"]}
        self.assertEqual(self.execute(activities=[later_activity, later_activity],
            owned=[[later_row], [later_row], []], main_options=options), 0)
        self.signal_mock.assert_called_once_with(later_row, signal.SIGINT)
        proof = load_json(self.folder / options["receipt_name"])
        self.assertEqual(proof["original_pid"], later_row["pid"])
        self.assertEqual(proof["original_start_ticks"], later_row["start_ticks"])
        self.assertEqual(proof["original_activity"]["run_id"], later_activity["run_id"])
        self.assertEqual(self.receipt_path.read_bytes(), original_bytes)
        self.assertEqual(load_json(next_freeze), self.freeze)
        with self.assertRaisesRegex(SystemExit, "already exists"):
            self.execute(activities=[later_activity], owned=[[later_row]], main_options=options)
        self.signal_mock.assert_called_once()

    def test_explicit_wrong_run_is_refused_before_receipt_or_signal(self):
        with self.assertRaisesRegex(SystemExit, "exact reviewed run"):
            self.execute(main_options={"expected_run_id": "different-run"})
        self.assert_refused_before_claim()

    def test_invalid_named_evidence_and_identity_are_refused_before_read_or_signal(self):
        for options in ({"freeze_name": "../other.json"}, {"receipt_name": "/tmp/other.json"},
                        {"receipt_name": self.freeze_path.name}, {"expected_pid": True},
                        {"expected_start": "not-an-identity"}, {"expected_run_id": ""},
                        {"allow_acquire_reload": "yes"}, {"allow_supplier_reload": "yes"},
                        {"allow_supplier_reload": 1}, {"allow_supplier_reload": None}):
            with self.subTest(options=options), self.assertRaisesRegex(SystemExit, "identity is invalid"):
                self.execute(main_options=options)
            self.assert_refused_before_claim()


if __name__ == "__main__":
    unittest.main()
