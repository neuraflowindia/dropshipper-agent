"""Offline opt-in and immutable source binding checks for the visual probe."""

from copy import deepcopy
import contextlib
from hashlib import sha256
import importlib.util
import io
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

from PIL import Image

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("visual_luna_check", ROOT / "scripts/check_visual_luna_connection.py")
check = importlib.util.module_from_spec(spec)
spec.loader.exec_module(check)
from product_radar.models import SafetyError
from product_radar.storage import atomic_json, load_json
from test_visual_identity import description

NOW = "2026-10-06T20:00:00+00:00"


class VisualLunaProbeTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.photos = self.root / "product-images"
        folder = self.photos / "visual-probe-20261006"
        folder.mkdir(parents=True)
        self.state = {"observations": {}, "descriptions": {}}
        self.receipts = []
        for n, observation_id in enumerate(check.OBSERVATIONS, 1):
            image = folder / f"image-{n}.jpg"
            Image.new("RGB", (12, 8), "red" if n == 1 else "blue").save(image)
            raw = image.read_bytes()
            semantic_id = f"fixture-semantic-{n}"
            url = f"https://supplier.example/photo-{n}.jpg"
            self.state["observations"][observation_id] = {"semantic_id": semantic_id, "observed_at": NOW,
                                                        "semantic": {"nested": [{"image_url": url}]}, "facts": {}}
            self.state["descriptions"][semantic_id] = {"result": {"id": semantic_id, "products": [description()]}}
            self.receipts.append({"observation_id": observation_id, "source_kind": "public_product_image",
                                  "source_url": url, "source_observed_at": NOW, "collected_at": NOW,
                                  "status": 200, "state": "Captured", "path": f"visual-probe-20261006/image-{n}.jpg",
                                  "bytes": len(raw), "sha256": sha256(raw).hexdigest()})

    def test_default_does_not_read_memory_run_model_or_write_report(self):
        with patch.object(check, "run_probe") as run, patch.object(check, "ResearchMemory") as memory, \
             patch.object(check, "atomic_json") as write, contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(check.main([]), 2)
        run.assert_not_called(); memory.assert_not_called(); write.assert_not_called()

    def test_live_option_calls_only_named_probe_once(self):
        with patch.object(check, "run_probe", return_value={"status": "passed"}) as run, \
             contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(check.main(["--live"]), 0)
        run.assert_called_once_with()

    def test_preparation_retains_exact_saved_description_source_ids_and_dates(self):
        prepared = check.prepare_probe(self.state, self.receipts, media_root=self.photos)
        item = prepared["request"]["payload"]["items"][0]
        self.assertEqual(item["semantic_id"], "fixture-semantic-1")
        self.assertEqual(item["description"], self.state["descriptions"]["fixture-semantic-1"]["result"]["products"][0])
        for row, receipt in zip(prepared["request"]["payload"]["images"], self.receipts):
            self.assertEqual(row["source_ref"], receipt["observation_id"])
            self.assertEqual(row["observed_at"], receipt["source_observed_at"])
            self.assertEqual(row["source_url"], receipt["source_url"])

    def test_missing_second_description_is_source_gap_not_invented_product(self):
        del self.state["descriptions"]["fixture-semantic-2"]
        with self.assertRaisesRegex(SafetyError, "VISUAL_PROBE_DESCRIPTION_MISSING"):
            check.prepare_probe(self.state, self.receipts, media_root=self.photos)

    def test_exact_url_date_and_description_semantic_bindings_required(self):
        for change in ("url", "date", "semantic"):
            state, receipts = deepcopy(self.state), deepcopy(self.receipts)
            if change == "url":
                receipts[0]["source_url"] += "?changed=true"
            elif change == "date":
                receipts[0]["source_observed_at"] = "2026-10-05T20:00:00+00:00"
            else:
                state["descriptions"]["fixture-semantic-2"]["result"]["id"] = "unrelated-semantic"
            with self.assertRaisesRegex(SafetyError, "VISUAL_PROBE_SOURCE_BINDING_INVALID"):
                check.prepare_probe(state, receipts, media_root=self.photos)

    def test_fixed_observation_pair_successful_receipt_bytes_and_hash_required(self):
        for changes in ({"observation_id": "another-observation"}, {"status": 403}, {"state": "Blocked"},
                        {"bytes": 1}, {"sha256": "0" * 64}, {"source_kind": "generated_preview"}):
            receipts = deepcopy(self.receipts)
            receipts[0].update(changes)
            with self.assertRaises(SafetyError):
                check.prepare_probe(self.state, receipts, media_root=self.photos)

    def test_missing_description_report_precedes_runner_and_job_creation(self):
        del self.state["descriptions"]["fixture-semantic-2"]
        paths = [self.root / f"receipt-{n}.json" for n in (1, 2)]
        for path, receipt in zip(paths, self.receipts):
            atomic_json(path, receipt)
        output = self.root / "probe-report.json"
        with patch.object(check, "ROOT", self.root), patch.object(check, "PHOTOS", self.photos), \
             patch.object(check, "RECEIPTS", paths), patch.object(check, "OUTPUT", output), \
             patch.object(check, "ResearchMemory", return_value=Mock(snapshot=Mock(return_value=self.state))), \
             patch.object(check, "research_codex_runner") as runner, patch.object(check, "Jobs") as jobs:
            report = check.run_probe()
        runner.assert_not_called(); jobs.assert_not_called()
        self.assertEqual(report["error_code"], "VISUAL_PROBE_DESCRIPTION_MISSING")
        self.assertIsNone(report["job_id"]); self.assertIsNone(report["usage"])
        self.assertEqual(report["applied_associations"], 0)
        self.assertIs(report["probe_only"], True)
        self.assertEqual(load_json(output), report)

    def test_completed_probe_report_retains_saved_usage_and_never_applies_association(self):
        paths = [self.root / f"receipt-{n}.json" for n in (1, 2)]
        for path, receipt in zip(paths, self.receipts):
            atomic_json(path, receipt)
        configuration = self.root / "config.json"
        atomic_json(configuration, {"synthetic": True})
        memory = Mock(root=self.root / "research", snapshot=Mock(return_value=self.state))
        saved_result = memory.root / "ai_jobs/fixture-job/result.json"
        usage = {"input_tokens": 123, "output_tokens": 45, "cached_input_tokens": 0}
        atomic_json(saved_result, {"usage": usage})
        output = self.root / "probe-report.json"
        for decision, expected_status in (("different", "passed"), ("unclear", "needs_review")):
            result = {"accepted": {"visual-probe-chair-cover": {"decision": decision}}, "failures": {}, "diagnostics": []}
            jobs = Mock(run=Mock(return_value=("fixture-job", result)))
            with patch.object(check, "ROOT", self.root), patch.object(check, "PHOTOS", self.photos), \
                 patch.object(check, "RECEIPTS", paths), patch.object(check, "OUTPUT", output), \
                 patch.object(check, "CONFIG", configuration), patch.object(check, "ResearchMemory", return_value=memory), \
                 patch.object(check, "research_codex_runner", return_value=Mock(identity="synthetic-runner")), \
                 patch.object(check, "Jobs", return_value=jobs):
                report = check.run_probe()
            self.assertEqual(report["status"], expected_status)
            self.assertEqual(report["usage"], usage)
            self.assertEqual(report["image_input_contract"], "accepted")
            self.assertEqual(report["result"], result)
            self.assertEqual(report["job_id"], "fixture-job")
            self.assertEqual(report["applied_associations"], 0)
            self.assertEqual(jobs.run.call_args.args[0], "D")
            memory.put.assert_not_called()
            self.assertEqual(load_json(output), report)

    def test_unexpected_diagnostics_are_never_printed_or_saved_as_error_codes(self):
        self.assertEqual(check.safe_code(RuntimeError("private-diagnostic")), "VISUAL_PROBE_LOCAL_FAILURE")
        output = io.StringIO()
        with patch.object(check, "run_probe", side_effect=RuntimeError("private-diagnostic")), contextlib.redirect_stdout(output):
            self.assertEqual(check.main(["--live"]), 1)
        self.assertNotIn("private-diagnostic", output.getvalue())


if __name__ == "__main__":
    unittest.main()
