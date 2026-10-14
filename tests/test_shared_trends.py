"""Actual CSV/chart verifier, real callers/owners, synthetic browser only."""

from copy import deepcopy
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from product_radar.models import SafetyError
from product_radar.research_memory import ResearchMemory
from product_radar.source_acquisitions import ACQUISITION_KIND
from product_radar.source_coverage import (execute_source_run, prepare_source_tasks, signed_in_access_scope,
    source_access_scope, source_access_wait)
from product_radar.storage import Journal
from product_radar.trends_controller import capture_direct_trends, create_trends_handlers
from product_radar.trends_shared import build_shared_trends_request, validate_trends_term_receipt
from test_shared_marketplace_acquisitions import wall_clock
from test_trends_controller import original, sign_in, snapshot

NOW = "2026-10-06T10:00:00+00:00"


class SharedTrendsTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.memory = ResearchMemory(self.root / "research", Journal(self.root / "finance"), evidence_type="synthetic")
        self.memory.put("concepts", "chair", {"status": "active", "description": {"label": "chair cover"}})
        self.now = NOW; self.mode = "usable"; self.calls = []
        self.signed_root = self.root / "signed-in"; sign_in(self.signed_root)
        clock = patch("product_radar.signed_in_pacing.utc_now", side_effect=lambda: self.now)
        clock.start(); self.addCleanup(clock.stop)

    def capture(self, request, aid, **options):
        self.calls.append((aid, request))
        if self.mode == "unknown": raise TimeoutError("Synthetic interrupted original browser")
        snap = snapshot(request["query"])
        raw = original()["csv_bytes"].replace(b"chair cover", request["query"].encode())
        actual = original(snap, raw=raw, completed_at=self.now, navigation_started_at=self.now)
        if self.mode == "empty":
            actual.update(snapshot=snapshot(request["query"], rows=[], download_found=False, download_clicked=False,
                                            no_data_text="No data available"), csv_bytes=None)
        if self.mode == "wrong_scope":
            actual["snapshot"] = snapshot(request["query"], text=snap["text"].replace("India", "Canada"))
        if self.mode == "blocked": actual.update(navigation_status=429, navigation_retry_after="3600")
        with patch("product_radar.trends_controller._wrapper_utc_now", return_value=self.now):
            return capture_direct_trends(request, aid, snapshot_reader=lambda _: actual, transport="signed-in", **options)

    def handlers(self, shared=True, capture=None):
        return create_trends_handlers(self.memory, capture=capture or self.capture, shared_acquisitions=shared,
                                      signed_in_root=self.signed_root)

    def wait(self, query="chair cover"):
        """The access wait of the owner's signed-in identity for this term's host."""
        return source_access_wait(self.memory.snapshot(), signed_in_access_scope(build_shared_trends_request(query)["url"]),
                                  as_of=self.now)

    def prepare(self, query="chair cover", run="checks"):
        self.run = run
        return prepare_source_tasks(self.memory, [{"route": "google-trends", "request": {"query": query, "country": "IN"},
            "concept_ids": ["chair"]}], run_id=run, as_of=self.now)

    def checks(self, handlers=None):
        return execute_source_run(self.memory, self.run, handlers=handlers or self.handlers(), as_of=self.now,
            bounded_call=lambda callback, context, timeout: callback(context), shared_marketplace=True)

    def owners(self):
        return {rid: row for rid, row in self.memory.snapshot()["batch_runs"].items() if row.get("kind") == ACQUISITION_KIND}

    def test_actual_chart_first_then_new_caller_and_restart_reuse_one_browser(self):
        self.prepare(); first = self.checks()
        self.assertEqual(first["handler_calls"], 1)
        self.assertEqual(first["counts"], {"acquired": 1})
        self.assertEqual(len(self.memory.snapshot()["trends_evidence"]), 1)
        self.prepare(run="second"); self.checks()
        self.memory = ResearchMemory(self.memory.root, Journal(self.root / "finance"), evidence_type="synthetic")
        self.prepare(run="third"); third = self.checks()
        self.assertEqual(third["handler_calls"], 0); self.assertEqual(len(self.calls), 1)
        self.assertEqual(len(self.owners()), 1)
        owner = next(iter(self.owners().values()))
        self.assertEqual(owner["binding"]["request"], build_shared_trends_request("chair cover"))
        response = self.memory.snapshot()["source_checks"][owner["active_response_id"]]
        self.assertTrue(response["complete"]); self.assertEqual(response["observed_at"], NOW)
        self.assertEqual(response["captures"], [])
        self.assertEqual(len(self.memory.snapshot()["concepts"]), 1)

    def test_empty_search_completes_without_fake_product_or_demand(self):
        self.mode = "empty"; self.prepare()
        self.assertEqual(self.checks()["counts"], {"completed_no_results": 1})
        self.assertEqual(next(iter(self.owners().values()))["state"], "completed_empty")
        state = self.memory.snapshot()
        self.assertEqual(state["trends_evidence"], {}); self.assertEqual(state["observations"], {})
        self.assertEqual(state["market_reviews"], {})

    def test_wrong_scope_is_not_a_complete_empty_search(self):
        self.mode = "wrong_scope"; self.prepare()
        self.assertEqual(self.checks()["counts"], {"insufficient": 1})
        owner = next(iter(self.owners().values()))
        self.assertFalse(self.memory.snapshot()["source_checks"][owner["active_response_id"]]["complete"])
        self.assertIsNone(self.wait())

    def test_blocked_request_waits_across_terms_without_a_second_browser(self):
        self.mode = "blocked"; self.prepare(); self.checks()
        wait = self.wait()
        self.assertIsNotNone(wait)
        self.assertEqual(wait["next_retry_at"], "2026-10-06T11:00:00+00:00")
        # The refusal holds the owner's signed-in identity only, never the public browser's.
        self.assertIsNone(source_access_wait(self.memory.snapshot(), source_access_scope(build_shared_trends_request("chair cover")["url"]), as_of=self.now))
        self.now = "2026-10-06T10:05:00+00:00"  # past pacing: only the refusal holds the next term
        self.prepare(query="table mat", run="second"); self.checks()
        self.assertEqual(len(self.calls), 1)

    def test_unknown_original_does_not_start_a_replacement_browser(self):
        self.mode = "unknown"; self.prepare(); self.checks()
        self.mode = "usable"; self.checks(); self.prepare(run="second"); self.checks()
        self.assertEqual(len(self.calls), 1); self.assertEqual(len(self.owners()), 1)

    def test_an_older_unshared_read_is_never_adopted_by_the_signed_in_route(self):
        self.prepare()
        execute_source_run(self.memory, self.run, handlers=self.handlers(False), as_of=self.now,
            bounded_call=lambda callback, context, timeout: callback(context))
        original_id = self.calls[0][0]
        self.now = "2026-10-06T10:05:00+00:00"
        self.prepare(run="new-shared-caller"); result = self.checks()
        self.assertEqual(result["handler_calls"], 1); self.assertEqual(len(self.calls), 2)
        (owner_id, owner), = self.owners().items()
        self.assertNotEqual(owner_id, original_id)
        self.assertEqual(owner["binding"]["request"]["transport"], "signed-in-browser")
        self.assertEqual(owner["binding"]["incremental_collection_fee"], "known_zero")

    def test_due_closed_block_gets_one_linked_retry(self):
        self.mode = "blocked"; self.prepare(); self.checks()
        original_id = next(iter(self.owners()))
        self.now = "2026-10-06T11:00:00+00:00"; self.mode = "usable"
        self.prepare(run="due-caller"); result = self.checks()
        self.assertEqual(result["handler_calls"], 1)
        self.assertEqual(len(self.calls), 2); self.assertEqual(len(self.owners()), 2)
        latest = max(self.owners().values(), key=lambda row: row["binding"]["generation"])
        self.assertEqual(latest["binding"]["previous_acquisition_id"], original_id)
        self.assertIsNone(self.wait())

    def test_future_or_expired_chart_cannot_be_claimed_as_fresh(self):
        self.prepare(); self.checks()
        handler = self.handlers()["google-trends"]
        for at in ("2026-10-06T09:59:59+00:00", "2026-10-13T10:00:00+00:00"):
            with self.subTest(at=at):
                hint = handler["source_due_hint"]({"route": "google-trends", "request": {"query": "chair cover", "country": "IN"}, "as_of": at})
                self.assertFalse(hint["local_saved"])
                self.assertEqual(hint["signal"]["observed_at"], NOW)
        self.assertEqual(len(self.calls), 1)

    def test_reserved_due_probe_remains_ready_after_failed_launch_commit(self):
        self.mode = "blocked"; self.prepare(); self.checks()
        self.now = "2026-10-06T11:00:00+00:00"; self.mode = "usable"; self.prepare(run="due-caller")
        with patch("product_radar.source_acquisitions.mark_source_dispatching", side_effect=OSError("Synthetic failed launch commit")):
            self.checks()
        self.assertEqual(len(self.calls), 1)
        hint = self.handlers()["google-trends"]["source_due_hint"]({"route": "google-trends",
            "request": {"query": "chair cover", "country": "IN"}, "as_of": self.now})
        self.assertTrue(hint["ready"])
        self.checks()
        self.assertEqual(len(self.calls), 2); self.assertEqual(len(self.owners()), 2)

    def test_india_offset_start_accepts_actual_later_utc_completion(self):
        self.now = "2026-10-06T15:30:00+05:30"; self.prepare()
        def delayed(request, aid, **options):
            self.calls.append((aid, request))
            actual = original(completed_at="2026-10-06T10:00:03+00:00")
            with patch("product_radar.trends_controller._wrapper_utc_now",
                       side_effect=[NOW, "2026-10-06T10:00:05+00:00"]):
                return capture_direct_trends(request, aid, snapshot_reader=lambda _: actual, transport="signed-in", **options)
        handlers = self.handlers(capture=delayed)
        with patch("product_radar.trends_shared.utc_now", return_value="2026-10-06T10:00:05+00:00"):
            result = self.checks(handlers)
        self.assertEqual(result["counts"], {"acquired": 1})
        owner = next(iter(self.owners().values()))
        response = self.memory.snapshot()["source_checks"][owner["active_response_id"]]
        self.assertEqual(response["observed_at"], "2026-10-06T10:00:03+00:00")
        self.assertEqual(len(self.calls), 1)

    def run_native_subsecond(self, completed, wrapper_completed, commit_instant):
        self.prepare()
        def native(request, aid, **options):
            self.calls.append((aid, request))
            actual = original(completed_at=completed, navigation_started_at="2026-10-06T10:00:01.1881905Z",
                              navigation_status=429, navigation_retry_after="3600")
            with patch("product_radar.trends_controller._wrapper_utc_now", side_effect=[NOW, wrapper_completed]):
                return capture_direct_trends(request, aid, snapshot_reader=lambda _: actual, transport="signed-in", **options)
        handlers = self.handlers(capture=native)
        with patch("product_radar.trends_shared.utc_now", wall_clock(commit_instant)):
            return self.checks(handlers)

    def test_native_completion_earlier_in_the_commit_second_is_saved(self):
        # 13 Oct first round: observed 19:49:25.1500845Z, wrapper 19:49:25.740331,
        # commit stamped "19:49:25" -> DIRECT_TRENDS_RECEIPT_MISMATCH.
        completed = "2026-10-06T10:00:03.1500845Z"
        result = self.run_native_subsecond(completed, "2026-10-06T10:00:03.740331+00:00",
                                           "2026-10-06T10:00:03.800000+00:00")
        self.assertEqual(result["counts"], {"blocked": 1})
        owner = next(iter(self.owners().values()))
        self.assertEqual(owner["state"], "failed_access")
        response = self.memory.snapshot()["source_checks"][owner["active_response_id"]]
        self.assertEqual(response["observed_at"], completed)
        self.assertEqual(response["recorded_at"], "2026-10-06T10:00:03.800000+00:00")
        wait = source_access_wait(self.memory.snapshot(), signed_in_access_scope(build_shared_trends_request("chair cover")["url"]), as_of=NOW)
        self.assertEqual(wait["next_retry_at"], "2026-10-06T11:00:03+00:00")
        self.assertEqual(len(self.calls), 1)

    def test_receipt_dated_after_the_actual_commit_instant_is_still_refused(self):
        result = self.run_native_subsecond("2026-10-06T10:00:03.9000000Z", "2026-10-06T10:00:03.950000+00:00",
                                           "2026-10-06T10:00:03.800000+00:00")
        self.assertEqual(result["counts"], {"failed": 1})
        task = next(row for row in self.memory.snapshot()["query_tasks"].values() if row.get("kind") == "all_source_task_v1")
        self.assertEqual(task["reason"], "DIRECT_TRENDS_RECEIPT_MISMATCH")
        owner = next(iter(self.owners().values()))
        self.assertEqual(owner["state"], "dispatching"); self.assertIsNone(owner["active_response_id"])
        self.assertEqual(len(self.calls), 1)

    def test_default_still_has_no_shared_owner_and_cannot_bypass_admission(self):
        self.prepare()
        execute_source_run(self.memory, self.run, handlers=self.handlers(False), as_of=self.now,
            bounded_call=lambda callback, context, timeout: callback(context))
        self.assertEqual(self.owners(), {})
        self.now = "2026-10-06T10:05:00+00:00"  # past pacing: admission is what refuses it
        with self.assertRaisesRegex(SafetyError, "SOURCE_SHARED_ADMISSION_REQUIRED"):
            self.handlers()["google-trends"]["handler"]({"route": "google-trends", "request": {"query": "chair cover", "country": "IN"},
                "acquisition_id": "not-admitted", "as_of": self.now})
        self.assertEqual(len(self.calls), 1)

    def test_term_reference_verifies_the_actual_original_and_rejects_forgery(self):
        self.prepare(); self.checks()
        owner_id, owner = next(iter(self.owners().items()))
        context = {"route": "google-trends", "request": owner["binding"]["request"], "acquisition_id": owner_id,
                   "concept_ids": [], "description_hashes": {}, "as_of": self.now}
        receipt = self.handlers()["google-trends"]["reconcile"](context)
        validate_trends_term_receipt(self.memory, context["request"], owner_id, receipt)
        wrong = deepcopy(receipt); wrong["term_capture_ref"]["hash"] = "0" * 64
        with self.assertRaisesRegex(SafetyError, "TERM_PROOF_INVALID"):
            validate_trends_term_receipt(self.memory, context["request"], owner_id, wrong)
        original_path = Path(receipt["reference"]).parent / "multiTimeline.csv"
        original_path.write_bytes(b"Changed synthetic source")
        with self.assertRaises(SafetyError):
            validate_trends_term_receipt(self.memory, context["request"], owner_id, receipt)
        self.assertEqual(len(self.calls), 1)

    def test_nonusable_term_original_is_verified_without_becoming_usable_evidence(self):
        for mode in ("blocked", "wrong_scope"):
            with self.subTest(mode=mode):
                self.memory = ResearchMemory(self.root / mode, Journal(self.root / (mode + "-finance")), evidence_type="synthetic")
                self.memory.put("concepts", "chair", {"status": "active", "description": {"label": "chair cover"}})
                self.signed_root = self.root / (mode + "-signed-in"); sign_in(self.signed_root)
                self.mode = mode; self.prepare(query=mode + " chair cover", run=mode); self.checks()
                owner_id, owner = next((aid, row) for aid, row in self.owners().items()
                    if row["binding"]["request"]["query"] == mode + " chair cover")
                context = {"route": "google-trends", "request": owner["binding"]["request"], "acquisition_id": owner_id,
                           "concept_ids": [], "description_hashes": {}, "as_of": self.now}
                receipt = self.handlers()["google-trends"]["reconcile"](context)
                response = validate_trends_term_receipt(self.memory, context["request"], owner_id, receipt)
                self.assertFalse(response["complete"])
                forged = deepcopy(receipt); forged["state"] = "usable"
                with self.assertRaisesRegex(SafetyError, "TERM_PROOF_INVALID"):
                    validate_trends_term_receipt(self.memory, context["request"], owner_id, forged)


if __name__ == "__main__": unittest.main()
