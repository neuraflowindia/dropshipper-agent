"""Source-local access pauses from exact saved proof; no live requests or AI."""

from copy import deepcopy
from datetime import datetime, timedelta, timezone
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from product_radar.models import digest
from product_radar.research_memory import ResearchMemory
from product_radar.source_coverage import execute_source_run, prepare_source_tasks, source_run_progress
from product_radar.storage import Journal
from product_radar.supplier_route_acquisition import (
    SEARCH_ROUTES, collect_supplier_search, create_supplier_handlers, supplier_route_url,
)
from product_radar.trends_controller import (
    build_trends_request, capture_direct_trends, create_trends_handlers,
)
from test_supplier_discovery import CONCEPT, NOW
from test_supplier_route_acquisition import QUERY, Reader, receipt
from test_trends_controller import sign_in, snapshot

IMMEDIATE = "2026-10-06T08:01:00+00:00"
DUE = "2026-10-06T08:05:01+00:00"


def forbidden(*_args, **_kwargs):
    raise AssertionError("Saved recovery must not launch another reader.")


class _AccessFixtures:
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.memory = ResearchMemory(self.root / "research", Journal(self.root / "money"), evidence_type="synthetic")
        self.memory.put("concepts", "paper", deepcopy(CONCEPT))

    def question(self, route, query=QUERY, ids=None):
        return {"route": route, "request": {"query": query, "country": "IN"}, "concept_ids": ids or ["paper"]}

    def context(self, route, acquisition="original", query=QUERY):
        request = {"query": query, "country": "IN"}
        return {"route": route, "request": request, "request_hash": digest(request),
                "acquisition_id": acquisition, "concept_ids": ["paper"],
                "description_hashes": {"paper": digest(CONCEPT["description"])}, "as_of": IMMEDIATE}

    def healthy(self, context):
        context["record_dispatch"]()
        return {"schema_version": 1, "route": context["route"], "acquisition_id": context["acquisition_id"],
                "observed_at": NOW, "state": "usable", "dispatch_started": True,
                "reference": context["acquisition_id"] + "/receipt.json", "capture_ids": [],
                "observation_ids": [], "target_results": [], "reason": "Synthetic healthy route."}

    def run_sources(self, run_id, handlers, at=NOW):
        return execute_source_run(self.memory, run_id, handlers=handlers, as_of=at,
                                  bounded_call=lambda callback, context, _timeout: callback(context))

    def prove_shared_pause_and_finite_probe(self, route, handler, calls):
        run_id = "health-" + route
        prepare_source_tasks(self.memory, [self.question(route), self.question(route, "Wrapping sheets"),
            self.question("meta-ads")], run_id=run_id, as_of=NOW)
        handlers = {route: handler, "meta-ads": self.healthy}
        first = self.run_sources(run_id, handlers)
        self.assertEqual(first["handler_calls"], 2)
        self.assertEqual(len(calls), 1)
        self.assertTrue(any(row.get("blocked_by") and row["actual_attempt"] is False for row in first["results"]))
        self.assertEqual(set(source_run_progress(self.memory, run_id)["route_pauses"]), {route})
        self.assertEqual(self.run_sources(run_id, handlers, IMMEDIATE)["handler_calls"], 0)
        due = self.run_sources(run_id, handlers, DUE)
        self.assertEqual(due["handler_calls"], 1)
        self.assertEqual(len(calls), 2)
        self.assertEqual(self.run_sources(run_id, handlers, DUE)["handler_calls"], 0)
        tasks = [row for row in self.memory.snapshot()["query_tasks"].values() if row.get("route") == route]
        self.assertEqual(sorted(row["tries"] for row in tasks), [0, 2])
        self.assertEqual(len({ref for row in tasks for ref in row["attempt_refs"]}), 2)
        self.assertIsNone(source_run_progress(self.memory, run_id)["route_pauses"][route]["next_retry_at"])


class SupplierAccessHealthTests(_AccessFixtures, unittest.TestCase):
    def search(self, route="google-suppliers", name="original", reader=None, browser=None):
        return collect_supplier_search(self.memory, route=route, concept_ids=["paper"], query=QUERY,
            acquisition_id=name, as_of=NOW, http_reader=reader or Reader(), browser_reader=browser)

    def test_each_directory_429_has_verified_shared_scope_and_delayed_retry(self):
        for route in SEARCH_ROUTES:
            with self.subTest(route=route):
                reader = Reader(status=429)
                result = self.search(route, name=route, reader=reader)
                self.assertEqual(result["state"], "blocked")
                self.assertTrue(result["dispatch_started"])
                self.assertEqual(result["failure_scope"], "route")
                self.assertTrue(result["retryable"])
                self.assertEqual(len(reader.calls), 1)

    def test_directory_access_refusals_without_429_pause_without_automatic_probe(self):
        for status in (401, 403):
            for route in SEARCH_ROUTES:
                with self.subTest(route=route, status=status):
                    result = self.search(route, name=route + "-" + str(status), reader=Reader(status=status))
                    self.assertEqual(result["state"], "blocked")
                    self.assertEqual(result["failure_scope"], "route")
                    self.assertFalse(result["retryable"])

    def test_rendered_challenge_200_is_route_failure_not_empty_supplier_results(self):
        for route in SEARCH_ROUTES:
            reader = Reader({supplier_route_url(route, QUERY): "Verify you are human. No products found."})
            result = self.search(route, name=route, reader=reader)
            self.assertEqual(result["state"], "blocked")
            self.assertEqual(result["reason"], "SUPPLIER_ROUTE_CHALLENGE")
            self.assertEqual(result["failure_scope"], "route")
            self.assertFalse(result["retryable"])

    def test_unrecognised_layout_empty_result_and_local_fetch_error_do_not_pause_route(self):
        variants = (("layout", Reader({supplier_route_url("indiamart", QUERY): "Readable but unrecognised page."}), "insufficient"),
                    ("empty", Reader({supplier_route_url("indiamart", QUERY): "No products found."}), "completed_no_results"),
                    ("error", Reader(status=500), "failed"))
        for name, reader, state in variants:
            result = self.search("indiamart", name, reader)
            self.assertEqual(result["state"], state)
            self.assertEqual(result["failure_scope"], "question")
            self.assertFalse(result["retryable"])

    def test_successful_fallback_prevents_shared_pause_from_earlier_429(self):
        result = self.search("indiamart", reader=Reader(status=429), browser=Reader())
        self.assertEqual(result["state"], "interpretation_pending")
        self.assertEqual(result["failure_scope"], "question")
        self.assertFalse(result["retryable"])

    def test_http_layout_failure_then_proved_browser_429_pauses_route(self):
        result = self.search(reader=Reader({supplier_route_url("google-suppliers", QUERY): "Unreadable layout."}),
                             browser=Reader(status=429))
        self.assertEqual(result["state"], "blocked")
        self.assertEqual(result["failure_scope"], "route")
        self.assertTrue(result["retryable"])

    def test_earlier_http_dispatch_cannot_donate_proof_to_browser_prelaunch_block(self):
        result = self.search(reader=Reader({supplier_route_url("google-suppliers", QUERY): "Unreadable layout."}),
                             browser=Reader(status=429, actual=False))
        self.assertEqual(result["state"], "blocked")
        self.assertTrue(result["dispatch_started"])
        self.assertEqual(result["failure_scope"], "question")
        self.assertFalse(result["retryable"])

    def test_uncertain_or_prelaunch_429_does_not_claim_shared_platform_failure(self):
        for name, stage in (("prelaunch", "not_dispatched"), ("uncertain", "unknown")):
            def reader(item, selected_stage=stage):
                return dict(receipt(item, status=429, actual=False), execution_stage=selected_stage)
            result = self.search(name=name, reader=reader)
            self.assertEqual(result["failure_scope"], "question")
            self.assertFalse(result["retryable"])
            self.assertIs(result["dispatch_started"], None if stage == "unknown" else False)

    def test_wrong_request_receiver_and_started_only_status_cannot_donate_shared_proof(self):
        changes = {"wrong-request": {"requested_url": supplier_route_url("google-maps", QUERY)},
                   "foreign-response": {"final_url": "https://unrelated.example.org/challenge"},
                   "started-only": {"execution_stage": "attempt_started"}}
        for name, values in changes.items():
            result = self.search(name=name, reader=lambda item, values=values: dict(receipt(item, status=429), **values))
            self.assertEqual(result["state"], "blocked")
            self.assertEqual(result["failure_scope"], "question")
            self.assertFalse(result["retryable"])

    def test_legacy_actual_http_429_retains_dispatch_proof_and_shared_scope(self):
        def legacy(item):
            result = receipt(item, status=429)
            for name in ("execution_stage", "attempt_started_at", "execution_ref"):
                result.pop(name)
            result["schema_version"] = 1
            return result
        result = self.search(reader=legacy)
        self.assertTrue(result["dispatch_started"])
        self.assertEqual(result["failure_scope"], "route")
        self.assertTrue(result["retryable"])

    def test_saved_only_scope_projection_preserves_original_bytes_dates_and_zero_requests(self):
        result = self.search(reader=Reader(status=429))
        original = Path(result["reference"]).read_bytes()
        handlers = create_supplier_handlers(self.memory, http_reader=forbidden, browser_reader=forbidden, page_reader=forbidden)
        state = self.memory.snapshot()
        replay = handlers["google-suppliers"]["reconcile"](self.context("google-suppliers"))
        self.assertEqual(replay, result)
        self.assertEqual(Path(result["reference"]).read_bytes(), original)
        self.assertEqual(replay["observed_at"], NOW)
        self.assertEqual(self.memory.snapshot(), state)

    def test_installed_supplier_handler_pauses_only_shared_route_and_probes_once(self):
        reader = Reader(status=429)
        handlers = create_supplier_handlers(self.memory, http_reader=reader, browser_reader=reader)
        # Each acquisition has one HTTP and one browser reply, not two controller attempts.
        acquisitions = []
        original_handler = handlers["google-maps"]["handler"]
        def tracked(context):
            acquisitions.append(context["acquisition_id"])
            return original_handler(context)
        selected = dict(handlers["google-maps"], handler=tracked)
        self.prove_shared_pause_and_finite_probe("google-maps", selected, acquisitions)
        self.assertEqual(len(reader.calls), 4)

    def test_one_company_429_does_not_pause_other_company_domains(self):
        self.memory.put("concepts", "second", deepcopy(CONCEPT))
        alpha, beta = "https://alpha.example.org/food-paper", "https://beta.example.org/food-paper"
        search_reader = Reader({supplier_route_url("google-suppliers", QUERY):
            '<a href="' + alpha + '"><h3>Food wrap paper manufacturer</h3></a>'
            '<a href="' + beta + '"><h3>Food wrap paper factory</h3></a>'})
        collect_supplier_search(self.memory, route="google-suppliers", concept_ids=["paper", "second"], query=QUERY,
                                http_reader=search_reader, as_of=NOW)
        checks = self.memory.snapshot()["source_checks"]
        questions = []
        for cid, url in (("paper", alpha), ("second", beta)):
            refs = [ref for ref, check in checks.items()
                    if check.get("kind") == "supplier_route_search" and check.get("concept_id") == cid]
            questions.append({"route": "company-pages", "concept_ids": [cid],
                              "request": {"candidate_urls": {cid: [url]}, "candidate_check_ids": {cid: refs}}})
        prepare_source_tasks(self.memory, questions, run_id="company-health", as_of=NOW)
        calls = []
        def page(item):
            calls.append(item["url"])
            return receipt(item, status=429 if item["url"] == alpha else 200,
                           body="Food wrap paper factory. Commercial terms not stated.")
        handlers = create_supplier_handlers(self.memory, http_reader=forbidden, browser_reader=page, page_reader=page)
        result = self.run_sources("company-health", handlers)
        self.assertEqual(result["handler_calls"], 2)
        self.assertIn(beta, calls)
        self.assertEqual(source_run_progress(self.memory, "company-health")["route_pauses"], {})
        self.assertEqual(result["counts"], {"failed": 1, "acquired": 1})


class TrendsAccessHealthTests(_AccessFixtures, unittest.TestCase):
    def setUp(self):
        super().setUp()
        self.signed_root = self.root / "signed-in"; sign_in(self.signed_root)
        # Pacing is proved in test_trends_signed_in. Here each counted read is
        # dated a day apart and long before the rounds, so only access health
        # decides when Google is read.
        days = iter(range(1, 400))
        clock = patch("product_radar.signed_in_pacing.utc_now",
                      side_effect=lambda: (datetime(2025, 1, 1, tzinfo=timezone.utc) + timedelta(days=next(days))).isoformat())
        clock.start(); self.addCleanup(clock.stop)

    def handlers(self, capture):
        return create_trends_handlers(self.memory, data_root=self.root / "trends", capture=capture,
                                      signed_in_root=self.signed_root)

    def capture(self, name="original", **changes):
        term = changes.pop("term", QUERY)
        actual = {"state": "captured", "reason": "Synthetic original transport.", "navigation_status": 200,
                  "navigation_started": True, "navigation_started_at": NOW, "completed_at": NOW,
                  "owned_process_exited": True, "snapshot": snapshot(term=term),
                  "csv_bytes": ("Week," + term + ": (India)\n2026-09-13,12\n2026-09-20,<1\n2026-09-27,20\n").encode(), **changes}
        with patch("product_radar.trends_controller._wrapper_utc_now", return_value=NOW):
            return capture_direct_trends(build_trends_request(term), name, data_root=self.root / "trends",
                                         snapshot_reader=lambda _request: actual, transport="signed-in")

    def reconcile(self, name="original"):
        handlers = create_trends_handlers(self.memory, data_root=self.root / "trends", capture=forbidden)
        return handlers["google-trends"]["reconcile"](self.context("google-trends", name))

    def test_proved_429_and_403_have_route_scope_but_only_429_is_due_for_retry(self):
        for status in (429, 403):
            self.capture(str(status), navigation_status=status)
            result = self.reconcile(str(status))
            self.assertEqual(result["state"], "blocked")
            self.assertEqual(result["failure_scope"], "route")
            self.assertEqual(result["retryable"], status == 429)
            self.assertTrue(result["dispatch_started"])

    def test_rendered_google_challenge_200_pauses_route_without_fake_chart_evidence(self):
        self.capture(snapshot=snapshot(term=QUERY, text="Unusual traffic CAPTCHA"), csv_bytes=None)
        result = self.reconcile()
        self.assertEqual(result["state"], "blocked")
        self.assertEqual(result["failure_scope"], "route")
        self.assertFalse(result["retryable"])
        self.assertEqual(result["evidence_refs"], [])
        self.assertEqual(self.memory.snapshot()["trends_evidence"], {})

    def test_503_without_rendered_access_challenge_is_not_shared_provider_block(self):
        self.capture(navigation_status=503)
        result = self.reconcile()
        self.assertEqual(result["state"], "blocked")
        self.assertEqual(result["failure_scope"], "question")

    def test_scope_and_chart_parsing_failures_do_not_pause_route(self):
        variants = {"scope": snapshot(term=QUERY, text="Unknown country scope"),
                    "chart": snapshot(term=QUERY, rows=[["x", "y1"], ["Sep 13, 2026", "99"]]),
                    "layout": {"text": None, "rows": []}}
        for name, snap in variants.items():
            self.capture(name, snapshot=snap)
            result = self.reconcile(name)
            self.assertIn(result["state"], ("insufficient", "failed"))
            self.assertEqual(result["failure_scope"], "question")
            self.assertFalse(result["retryable"])

    def test_status_without_actual_dispatch_does_not_pause_route(self):
        self.capture(navigation_status=429, navigation_started=False, navigation_started_at=None)
        result = self.reconcile()
        self.assertFalse(result["dispatch_started"])
        self.assertEqual(result["failure_scope"], "question")

    def test_failed_cleanup_or_missing_observation_cannot_become_shared_block(self):
        self.capture(state="failed", snapshot=None, csv_bytes=None, navigation_status=429,
                     reason="OWNED_BROWSER_CLEANUP_FAILED", owned_process_exited=False)
        result = self.reconcile()
        self.assertEqual(result["state"], "failed")
        self.assertEqual(result["failure_scope"], "question")

    def test_saved_route_projection_leaves_original_packet_bytes_and_date_unchanged(self):
        receipt = self.capture(navigation_status=429)
        folder = Path(receipt["folder"])
        originals = {path.name: path.read_bytes() for path in folder.iterdir() if path.is_file()}
        result = self.reconcile()
        state = self.memory.snapshot()
        self.assertEqual(self.reconcile(), result)
        self.assertEqual(self.memory.snapshot(), state)
        self.assertEqual(result["observed_at"], NOW)
        self.assertEqual({path.name: path.read_bytes() for path in folder.iterdir() if path.is_file()}, originals)

    def test_installed_trends_handler_pauses_only_google_and_probes_once(self):
        calls = []
        def capture(request, acquisition_id, **_options):
            calls.append(acquisition_id)
            return self.capture(acquisition_id, term=request["query"], navigation_status=429)
        handlers = self.handlers(capture)
        self.prove_shared_pause_and_finite_probe("google-trends", handlers["google-trends"], calls)

    def test_saved_reconciliation_is_allowed_while_same_route_is_paused(self):
        calls = []
        def capture(request, acquisition_id, **_options):
            calls.append(acquisition_id)
            return self.capture(acquisition_id, navigation_status=403)
        handlers = self.handlers(capture)
        prepare_source_tasks(self.memory, [self.question("google-trends")], run_id="paused-replay", as_of=NOW)
        self.run_sources("paused-replay", handlers)
        task_id, task = next(iter(self.memory.snapshot()["query_tasks"].items()))
        raw = self.root / "trends" / task["acquisition_id"] / "receipt.json"
        original = raw.read_bytes()
        self.memory.put("query_tasks", task_id, dict(task, reconcile_required=True), previous=task)
        saved_handlers = create_trends_handlers(self.memory, data_root=self.root / "trends", capture=forbidden)
        recovered = self.run_sources("paused-replay", saved_handlers, IMMEDIATE)
        after = self.memory.snapshot()["query_tasks"][task_id]
        self.assertEqual(recovered["handler_calls"], 1)
        self.assertEqual(after["attempt_refs"], task["attempt_refs"])
        self.assertEqual(after["tries"], 1)
        self.assertEqual(after["receipt_refs"], task["receipt_refs"])
        self.assertFalse(after["reconcile_required"])
        self.assertEqual(raw.read_bytes(), original)
        self.assertEqual(len(calls), 1)

    def test_local_hash_error_keeps_healthy_lane_running_without_shared_pause(self):
        def damaged_capture(request, acquisition_id, **_options):
            saved = self.capture(acquisition_id, term=request["query"])
            (Path(saved["folder"]) / "multiTimeline.csv").write_bytes(b"Synthetic corruption")
            return saved
        handlers = self.handlers(damaged_capture)
        handlers["meta-ads"] = self.healthy
        prepare_source_tasks(self.memory, [self.question("google-trends"), self.question("meta-ads")],
                             run_id="local-proof-error", as_of=NOW)
        result = self.run_sources("local-proof-error", handlers)
        self.assertEqual(result["counts"], {"failed": 1, "acquired": 1})
        task = next(row for row in self.memory.snapshot()["query_tasks"].values() if row.get("route") == "google-trends")
        self.assertEqual(task["reason"], "DIRECT_TRENDS_ARTIFACT_HASH_MISMATCH")
        self.assertTrue(task["dispatch_started"])
        self.assertEqual(task["tries"], 1)
        self.assertEqual(source_run_progress(self.memory, "local-proof-error")["route_pauses"], {})


if __name__ == "__main__":
    unittest.main()
