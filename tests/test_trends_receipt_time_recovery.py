"""Precision and saved-only dispatch proofs, using mocked native originals only."""

from copy import deepcopy
from hashlib import sha256
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from product_radar.models import SafetyError, digest, timestamp
from product_radar.research_memory import ResearchMemory
from product_radar.source_coverage import execute_source_run, prepare_source_tasks, source_coverage
from product_radar.storage import Journal, atomic_json, load_json
from product_radar.trends_capture import explore_url
from product_radar.trends_controller import (
    _wrapper_utc_now, build_trends_request, capture_direct_trends,
    create_trends_handlers, replay_direct_trends,
)
from test_trends_controller import original, sign_in, snapshot

START = "2026-10-08T14:04:07+00:00"
DISPATCH = "2026-10-08T14:04:08.0413087Z"
OBSERVED = "2026-10-08T14:04:10.0010287Z"
END = "2026-10-08T14:04:10+00:00"
LATER = "2026-10-08T14:10:00+00:00"


class TrendsReceiptTimeRecoveryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.captures = self.root / "captures"
        self.memory = ResearchMemory(self.root / "research", Journal(self.root / "money"), evidence_type="captured")
        self.memory.put("concepts", "chair", {"status": "active", "description": {
            "name": "chair cover", "label": "Chair cover", "category": "Home and Decor",
            "function": "Protect chair upholstery", "mechanism": "fitted cloth cover", "audience": "adult",
            "features": ["washable"], "basis": "observed", "brand": "generic_or_small",
            "reason": "Synthetic controller fixture only."}})
        self.request = build_trends_request("chair cover")
        self.signed_root = self.root / "signed-in"; sign_in(self.signed_root)

    def native_capture(self, name="legacy", *, start=START, end=END,
                       dispatch=DISPATCH, observed=OBSERVED, status=429,
                       write_dispatch=True, write_result=True, actual=None):
        value = actual or original(snapshot(html="<html>Synthetic saved chart</html>"),
            navigation_status=status, navigation_started_at=dispatch, completed_at=observed)
        def native(url, expression, folder, **options):
            self.assertEqual(url, explore_url("chair cover"))
            saved = {key: deepcopy(item) for key, item in value.items() if key != "csv_bytes"}
            if write_dispatch:
                atomic_json(folder / "native_dispatch.json", {"url": url, "navigation_started": True,
                    "navigation_started_at": dispatch})
            if write_result:
                atomic_json(folder / "native_result.json", saved)
            return deepcopy(value)
        with patch("product_radar.trends_controller._wrapper_utc_now", side_effect=[start, end]), \
                patch("product_radar.native_public_browser.native_snapshot", side_effect=native), \
                patch("product_radar.trends_controller._playwright", side_effect=AssertionError("No browser fallback")):
            return capture_direct_trends(self.request, name, data_root=self.captures, transport="signed-in",
                                         signed_in_root=self.signed_root)

    def context(self, name="legacy"):
        return {"route": "google-trends", "request": deepcopy(self.request), "acquisition_id": name,
                "concept_ids": ["chair"], "description_hashes": {"chair": digest(
                    self.memory.snapshot()["concepts"]["chair"]["description"])}}

    def mutate_receipt(self, receipt, **changes):
        value = dict(receipt, **changes)
        atomic_json(Path(receipt["folder"]) / "receipt.json", value)
        return value

    def mutate_artifact(self, receipt, name, changes):
        folder = Path(receipt["folder"])
        value = load_json(folder / name, 12 * 1024 * 1024)
        value.update(changes)
        atomic_json(folder / name, value)
        artifacts = dict(receipt["artifacts"], **{name: sha256((folder / name).read_bytes()).hexdigest()})
        return self.mutate_receipt(receipt, artifacts=artifacts)

    def test_new_wrapper_clock_is_aware_and_retains_microseconds_locally(self):
        value = _wrapper_utc_now()
        self.assertRegex(value, r"\.\d{6}\+00:00$")
        self.assertEqual(timestamp(value).utcoffset().total_seconds(), 0)
        # The compatibility repair is not a change to shared timestamp output.
        from product_radar.models import utc_now
        self.assertNotIn(".", utc_now())

    def test_legacy_fractional_native_result_recovers_without_changing_any_bytes(self):
        receipt = self.native_capture()
        folder = Path(receipt["folder"])
        before = {path.name: path.read_bytes() for path in folder.iterdir() if path.is_file()}
        self.assertEqual(replay_direct_trends(folder), receipt)
        self.assertEqual(receipt["state"], "blocked")
        self.assertEqual(receipt["reason"], "DIRECT_TRENDS_ACCESS_CHALLENGE")
        self.assertEqual(receipt["navigation_status"], 429)
        self.assertFalse(receipt["scope_verified"])
        self.assertEqual(receipt["observed_at"], OBSERVED)
        self.assertEqual(receipt["completed_at"], END)
        self.assertEqual(before, {path.name: path.read_bytes() for path in folder.iterdir() if path.is_file()})

    def test_precise_wrapper_keeps_original_native_time_and_accepts_proper_order(self):
        receipt = self.native_capture("new", start="2026-10-08T14:04:07.000000+00:00",
                                     end="2026-10-08T14:04:10.005000+00:00")
        self.assertEqual(replay_direct_trends(receipt["folder"]), receipt)
        self.assertEqual(receipt["observed_at"], OBSERVED)

    def test_legacy_interval_is_exclusive_and_does_not_allow_a_second_of_drift(self):
        for name, observed, accepted in (
            ("inside", "2026-10-08T14:04:10.9999999Z", True),
            ("boundary", "2026-10-08T14:04:11.0000000Z", False),
            ("outside", "2026-10-08T14:04:11.0010287Z", False),
        ):
            with self.subTest(name=name):
                receipt = self.native_capture(name, observed=observed)
                if accepted:
                    self.assertEqual(replay_direct_trends(receipt["folder"]), receipt)
                else:
                    with self.assertRaisesRegex(SafetyError, "RECEIPT_TIME_INVALID"):
                        replay_direct_trends(receipt["folder"])

    def test_precise_wrapper_does_not_get_legacy_allowance(self):
        receipt = self.native_capture("precise-inversion", end="2026-10-08T14:04:10.000000+00:00")
        with self.assertRaisesRegex(SafetyError, "RECEIPT_TIME_INVALID"):
            replay_direct_trends(receipt["folder"])

    def test_earlier_observation_inverted_wrapper_and_malformed_clocks_stay_rejected(self):
        for index, change in enumerate((
            {"observed": "2026-10-08T14:04:06.9999999Z"},
            {"start": "2026-10-08T14:04:11+00:00"},
            {"start": "2026-10-08T14:04:07"},
            {"end": "invalid"},
            {"observed": "invalid"},
        )):
            with self.subTest(change=change):
                receipt = self.native_capture("invalid-time-" + str(index), **change)
                with self.assertRaisesRegex(SafetyError, "TIME"):
                    replay_direct_trends(receipt["folder"])

    def test_dispatch_fraction_needs_verified_native_boundary_and_exact_interval(self):
        for name, dispatch, observed, accepted in (
            ("dispatch-inside", "2026-10-08T14:04:10.5000000Z", "2026-10-08T14:04:10.9000000Z", True),
            ("dispatch-before", "2026-10-08T14:04:06.9999999Z", OBSERVED, False),
            ("dispatch-after-observation", "2026-10-08T14:04:10.5000000Z", OBSERVED, False),
            ("dispatch-at-limit", "2026-10-08T14:04:11.0000000Z", OBSERVED, False),
        ):
            with self.subTest(name=name):
                receipt = self.native_capture(name, dispatch=dispatch, observed=observed)
                if accepted:
                    self.assertEqual(replay_direct_trends(receipt["folder"]), receipt)
                else:
                    with self.assertRaisesRegex(SafetyError, "DISPATCH_TIME_INVALID"):
                        replay_direct_trends(receipt["folder"])
        receipt = self.native_capture("no-boundary", dispatch="2026-10-08T14:04:10.5000000Z",
                                      observed="2026-10-08T14:04:10.9000000Z", write_dispatch=False)
        with self.assertRaisesRegex(SafetyError, "DISPATCH_TIME_INVALID"):
            replay_direct_trends(receipt["folder"])

    def test_unbound_native_or_other_transport_cannot_use_legacy_allowance(self):
        receipt = self.native_capture("native-mismatch")
        receipt = self.mutate_artifact(receipt, "native_result.json", {"completed_at": END})
        with self.assertRaisesRegex(SafetyError, "RECEIPT_TIME_INVALID"):
            replay_direct_trends(receipt["folder"])
        receipt = self.native_capture("other-transport")
        self.mutate_receipt(receipt, transport_attempts=["owned_playwright_browser"])
        with self.assertRaisesRegex(SafetyError, "RECEIPT_TIME_INVALID"):
            replay_direct_trends(receipt["folder"])

    def test_original_hashes_and_native_scope_are_still_required_with_legacy_time(self):
        for index, name in enumerate(("native_result.json", "native_dispatch.json", "transport.json", "rendered.html")):
            with self.subTest(name=name):
                receipt = self.native_capture("changed-" + str(index))
                path = Path(receipt["folder"]) / name
                path.write_bytes(path.read_bytes() + b" ")
                with self.assertRaisesRegex(SafetyError, "HASH_MISMATCH"):
                    replay_direct_trends(receipt["folder"])
        receipt = self.native_capture("wrong-native-scope")
        self.mutate_artifact(receipt, "native_result.json", {"snapshot": snapshot(text="different public page")})
        with self.assertRaisesRegex(SafetyError, "ORIGINAL_SCOPE_MISMATCH"):
            replay_direct_trends(receipt["folder"])

    def test_legacy_usable_chart_still_needs_its_original_csv_and_comparison(self):
        receipt = self.native_capture("chart", status=200)
        self.assertEqual(receipt["state"], "usable")
        self.assertEqual(replay_direct_trends(receipt["folder"]), receipt)
        csv = Path(receipt["folder"]) / "multiTimeline.csv"
        csv.write_bytes(csv.read_bytes().replace(b",12", b",99"))
        with self.assertRaisesRegex(SafetyError, "HASH_MISMATCH"):
            replay_direct_trends(receipt["folder"])

    def test_reconcile_preserves_real_google_block_and_never_imports_fake_chart(self):
        self.native_capture()
        dispatches = []
        context = self.context()
        context["record_dispatch"] = lambda: dispatches.append(True)
        handlers = create_trends_handlers(self.memory, data_root=self.captures)
        with patch("product_radar.native_public_browser.native_snapshot", side_effect=AssertionError("No native launch")), \
                patch("product_radar.trends_controller._playwright", side_effect=AssertionError("No browser fallback")):
            result = handlers["google-trends"]["reconcile"](context)
            self.assertEqual(handlers["google-trends"]["reconcile"](context), result)
        self.assertEqual(result["state"], "blocked")
        self.assertTrue(result["dispatch_started"])
        self.assertEqual(result["target_results"][0]["state"], "blocked")
        self.assertEqual(result["evidence_refs"], [])
        self.assertEqual(self.memory.snapshot()["trends_evidence"], {})
        self.assertEqual(dispatches, [True, True])
        self.assertFalse(self.memory.journal.path.exists())

    def test_bound_dispatch_is_attributed_before_independent_replay_failure(self):
        for name, failure in (("clock", "clock"), ("chart-hash", "chart")):
            with self.subTest(name=name):
                receipt = self.native_capture(name, status=200)
                if failure == "clock":
                    self.mutate_receipt(receipt, completed_at="2026-10-08T14:04:10.000000+00:00")
                else:
                    path = Path(receipt["folder"]) / "multiTimeline.csv"
                    path.write_bytes(path.read_bytes() + b"changed")
                dispatches = []
                context = self.context(name)
                context["record_dispatch"] = lambda: dispatches.append(True)
                handlers = create_trends_handlers(self.memory, data_root=self.captures,
                    capture=lambda *a, **k: self.fail("Reconcile must be saved-only"))
                with self.assertRaises(SafetyError):
                    handlers["google-trends"]["reconcile"](context)
                self.assertEqual(dispatches, [True])
                self.assertEqual(self.memory.snapshot()["trends_evidence"], {})

    def test_handler_restart_with_existing_capture_verifies_dispatch_before_replay(self):
        receipt = self.native_capture("interrupted-handler")
        self.mutate_receipt(receipt, completed_at="2026-10-08T14:04:10.000000+00:00")
        dispatches = []
        context = self.context("interrupted-handler")
        context["record_dispatch"] = lambda: dispatches.append(True)
        handlers = create_trends_handlers(self.memory, data_root=self.captures,
            capture=lambda *a, **k: self.fail("An existing original must not be recaptured"))
        with self.assertRaisesRegex(SafetyError, "RECEIPT_TIME_INVALID"):
            handlers["google-trends"]["handler"](context)
        self.assertEqual(dispatches, [True])
        self.assertEqual(self.memory.snapshot()["trends_evidence"], {})

    def test_missing_final_native_result_keeps_only_proved_navigation_not_demand(self):
        actual = {"state": "failed", "reason": "NATIVE_PUBLIC_DEADLINE", "snapshot": None,
                  "navigation_started": None, "completed_at": OBSERVED, "owned_process_exited": True}
        receipt = self.native_capture("missing-result", actual=actual, write_result=False)
        self.assertTrue(receipt["dispatch_started"])
        dispatches = []
        context = self.context("missing-result")
        context["record_dispatch"] = lambda: dispatches.append(True)
        handlers = create_trends_handlers(self.memory, data_root=self.captures)
        with self.assertRaisesRegex(SafetyError, "RECEIPT_TIME_INVALID"):
            handlers["google-trends"]["reconcile"](context)
        self.assertEqual(dispatches, [True])
        self.assertEqual(self.memory.snapshot()["trends_evidence"], {})

    def test_wrong_binding_namespace_and_missing_or_corrupt_boundary_do_not_attribute(self):
        for index, kind in enumerate(("query", "acquisition", "namespace", "missing", "corrupt", "wrong-url", "native-time")):
            with self.subTest(kind=kind):
                name = "unproved-" + str(index)
                receipt = self.native_capture(name)
                context = self.context(name)
                if kind == "query":
                    context["request"] = build_trends_request("table mat")
                elif kind == "acquisition":
                    self.mutate_receipt(receipt, acquisition_id="different-acquisition")
                elif kind == "namespace":
                    self.mutate_receipt(receipt, evidence_type="synthetic")
                elif kind == "missing":
                    (Path(receipt["folder"]) / "native_dispatch.json").unlink()
                elif kind == "corrupt":
                    (Path(receipt["folder"]) / "native_dispatch.json").write_bytes(b"{}")
                elif kind == "wrong-url":
                    self.mutate_artifact(receipt, "native_dispatch.json", {"url": explore_url("table mat")})
                elif kind == "native-time":
                    self.mutate_artifact(receipt, "native_result.json", {"navigation_started_at": OBSERVED})
                dispatches = []
                context["record_dispatch"] = lambda: dispatches.append(True)
                handlers = create_trends_handlers(self.memory, data_root=self.captures)
                with self.assertRaises(SafetyError):
                    handlers["google-trends"]["reconcile"](context)
                self.assertEqual(dispatches, [])

    def test_false_or_unknown_navigation_is_not_an_invented_attempt(self):
        for index, navigation in enumerate((False, None)):
            name = "not-proved-" + str(index)
            actual = {"state": "failed", "reason": "Synthetic startup uncertainty", "snapshot": None,
                "navigation_started": navigation, "navigation_started_at": None, "completed_at": END,
                "owned_process_exited": True}
            self.native_capture(name, actual=actual, write_dispatch=False)
            dispatches = []
            context = self.context(name)
            context["record_dispatch"] = lambda: dispatches.append(True)
            handlers = create_trends_handlers(self.memory, data_root=self.captures)
            result = handlers["google-trends"]["reconcile"](context)
            self.assertFalse(result["dispatch_started"])
            self.assertEqual(dispatches, [])
            self.assertEqual(result["evidence_refs"], [])

    def test_controller_keeps_actual_attempt_and_same_acquisition_when_local_replay_fails(self):
        questions = [{"route": "google-trends", "request": self.request, "concept_ids": ["chair"]},
                     {"route": "meta-ads", "request": {"query": "chair cover", "country": "IN"}, "concept_ids": ["chair"]}]
        prepare_source_tasks(self.memory, questions, run_id="original-run", as_of=LATER)
        captures = []
        def capture(request, acquisition_id, **options):
            captures.append(acquisition_id)
            receipt = self.native_capture(acquisition_id)
            return self.mutate_receipt(receipt, completed_at="2026-10-08T14:04:10.000000+00:00")
        healthy_calls = []
        def healthy(context):
            healthy_calls.append(context["acquisition_id"])
            context["record_dispatch"]()
            return {"schema_version": 1, "route": "meta-ads", "acquisition_id": context["acquisition_id"],
                "observed_at": LATER, "state": "empty", "dispatch_started": True, "reference": "synthetic-meta-receipt",
                "capture_ids": [], "observation_ids": [], "target_results": [], "reason": "Readable synthetic empty search."}
        signed_root = self.root / "signed-in"; sign_in(signed_root)
        handlers = dict(create_trends_handlers(self.memory, data_root=self.captures, capture=capture,
                                               signed_in_root=signed_root), **{"meta-ads": healthy})
        def execute():
            with patch("product_radar.source_coverage.utc_now", return_value=LATER):
                return execute_source_run(self.memory, "original-run", handlers=handlers, as_of=LATER,
                    bounded_call=lambda callback, context, timeout: callback(context))
        execute()
        self.assertEqual(len(healthy_calls), 1)
        task = next(row for row in self.memory.snapshot()["query_tasks"].values() if row.get("route") == "google-trends")
        self.assertTrue(task["dispatch_started"])
        self.assertTrue(task["reconcile_required"])
        self.assertEqual(task["tries"], 1)
        acquisition = task["acquisition_id"]
        grid = source_coverage(self.memory, handlers=handlers, as_of=LATER)
        row = next(row for row in grid["rows"] if row["route"] == "google-trends")
        self.assertTrue(row["actual_attempt"], row)
        self.assertEqual(row["reason"], "DIRECT_TRENDS_RECEIPT_TIME_INVALID")
        with patch("product_radar.native_public_browser.native_snapshot", side_effect=AssertionError("No new native request")):
            execute()
        task = next(row for row in self.memory.snapshot()["query_tasks"].values() if row.get("route") == "google-trends")
        self.assertEqual(task["acquisition_id"], acquisition)
        self.assertEqual(task["tries"], 1)
        self.assertEqual(captures, [acquisition])
        self.assertEqual(len(healthy_calls), 1)
        self.assertEqual(self.memory.snapshot()["trends_evidence"], {})


if __name__ == "__main__":
    unittest.main()
