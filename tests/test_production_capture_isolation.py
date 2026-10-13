"""Offline production source failures must not erase financial constraints."""

from copy import deepcopy
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
from product_radar.benchmark import MetaTrial
from product_radar.models import SafetyError, digest
from product_radar.storage import Journal, atomic_json
from test_production_budget import binding, request


class ProductionCaptureIsolationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.journal = Journal(self.root / "data/benchmarks/state/apify")
        self.account = binding()
        self.controller = MetaTrial(self.root, self.journal)

    def reconcile(self, scope="production", *, charge="0.01", read="0.02", controls=True):
        prepared = request("previous", scope)
        self.prepared = prepared
        self.preparation = self.root / "data/benchmarks/2026-10-03/previous/prepared.json"
        atomic_json(self.preparation, {"request": prepared.to_dict(), "config_hash": prepared.config_hash,
                                      "execution_mode": "offline_preparation"})
        with self.journal.locked():
            self.journal.setup_budget(self.account.namespace, "5", production_provision="4")
            self.journal.adopt_production_scope(self.account.namespace, {"meta": "3"}, "Fictional offline allocation.", "test-production")
            self.journal.reserve(prepared, self.account, read_reserve=read)
            self.journal.append("submit", "submitting", {"attempt": "previous"})
            self.journal.append("receipt", "receipt", {"attempt": "previous", "run_id": "run-previous", "dataset_id": "dataset-previous"})
            self.journal.append("terminal", "terminal", {"attempt": "previous", "status": "SUCCEEDED"})
            self.journal.append("controls", "controls", {"attempt": "previous", "ok": controls, "evidence": "fictional-control-proof"})
            if charge is not None:
                self.journal.append("charge", "charge", {"attempt": "previous", "usd": charge, "evidence": "fictional-charge-proof"})
            return self.journal.state()[1]

    def state(self):
        with self.journal.locked():
            return self.journal.state()[1]

    def check(self, attempts):
        with self.journal.locked():
            return self.controller.check_previous_reviews(attempts)

    def test_failed_source_is_isolated_without_changing_ledger_or_holds(self):
        attempts = self.reconcile()
        before = self.journal.path.read_bytes()
        with patch("product_radar.benchmark.load_json", side_effect=AssertionError("No capture files should be opened")), \
                patch("product_radar.benchmark.load_preparation", return_value=self.prepared) as loader:
            self.check(attempts)
        loader.assert_called_once_with(self.root, "previous")
        self.assertEqual(self.journal.path.read_bytes(), before)
        saved = self.state()["previous"]
        self.assertEqual(saved["capture_status"], "not_captured")
        self.assertIsNone(saved["capture"])
        self.assertIsNone(saved["read_charge"])
        self.assertEqual(saved["read_hold"], "0.02")
        # An independent funded request can proceed, but the failed attempt's
        # charge plus held read allowance remain in account arithmetic.
        with self.journal.locked():
            self.journal.check_reservation(request("next"), self.account, read_reserve="0.02")

    def test_real_saved_preparation_scope_and_hash_are_checked(self):
        attempts = self.reconcile()
        self.check(attempts)
        changed = request("previous", "trial")
        atomic_json(self.preparation, {"request": changed.to_dict(), "config_hash": changed.config_hash,
                                      "execution_mode": "offline_preparation"})
        with self.assertRaisesRegex(SafetyError, "PRIOR_PRODUCTION_CAPTURE_INVALID"):
            self.check(attempts)

    def test_unknown_charge_or_bad_controls_do_not_gain_exception(self):
        for changes in ({"charge": None}, {"controls_ok": False}, {"controls_ok": None},
                        {"remote_status": None}, {"submission": "uncertain"},
                        {"charge": "0.04"}, {"read_charge": "0.021"},
                        {"read_charge": None, "read_hold": "0"}):
            attempts = self.reconcile() if not hasattr(self, "prepared") else self.state()
            changed = deepcopy(attempts)
            changed["previous"].update(changes)
            with self.subTest(changes=changes), self.assertRaisesRegex(SafetyError, "PRIOR_PRODUCTION_CONTROLS_UNRESOLVED"):
                self.check(changed)

    def test_known_read_cost_within_hold_is_also_isolated(self):
        attempts = self.reconcile()
        with self.journal.locked():
            self.journal.append("reads", "read_cost", {"attempt": "previous", "usd": "0.005", "evidence": "fictional-read-proof"})
        self.check(self.state())

    def test_trial_with_no_capture_still_needs_review(self):
        with self.assertRaisesRegex(SafetyError, "PRIOR_SAMPLE_REVIEW_REQUIRED"):
            self.check(self.reconcile(scope="trial"))

    def test_committed_capture_without_files_is_not_silently_skipped(self):
        self.reconcile()
        with self.journal.locked():
            self.journal.append("captured", "capture", {"attempt": "previous", "hash": digest([])})
        with self.assertRaisesRegex(SafetyError, "SAVED_FILE_UNAVAILABLE"):
            self.check(self.state())

    def test_inconsistent_not_captured_label_cannot_hide_committed_hash(self):
        attempts = self.reconcile()
        attempts["previous"]["capture"] = digest([])
        with self.assertRaisesRegex(SafetyError, "SAVED_FILE_UNAVAILABLE"):
            self.check(attempts)

    def test_missing_preparation_is_not_assumed_production(self):
        attempts = self.reconcile()
        self.preparation.unlink()
        with self.assertRaisesRegex(SafetyError, "PREPARATION_MISSING_OR_AMBIGUOUS"):
            self.check(attempts)


if __name__ == "__main__":
    unittest.main()
