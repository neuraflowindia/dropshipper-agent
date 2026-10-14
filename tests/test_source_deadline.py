"""Outer signal deadlines use offline readers/process fixtures, not providers."""

import base64
from contextlib import ExitStack
from copy import deepcopy
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import re
import signal
import sys
import tempfile
import threading
import time
from types import SimpleNamespace
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from product_radar.marketplace_browser import build_marketplace_request, capture_marketplace, reconcile_marketplace, replay_marketplace
from product_radar.meta_browser import capture_meta_search, meta_direct_source_handlers, prepare_meta_query
from product_radar.models import SafetyError, digest
from product_radar.native_public_browser import native_snapshot
from product_radar.public_pages import _Control, fetch_public_page
from product_radar.quickcommerce_browser import create_quickcommerce_handlers
from product_radar.research_memory import ResearchMemory, import_direct_marketplace, key
from product_radar.source_coverage import (_bounded_call, create_marketplace_handlers, execute_source_run,
    installed_source_handlers, prepare_source_tasks)
from product_radar.source_deadline import SourceActionDeadline
from product_radar.source_deadline import saved_completed_transport, validate_interrupted_transport
from product_radar.storage import Journal, atomic_json, load_json
from product_radar.supplier_capture import capture_supplier_bundle
from product_radar.supplier_route_acquisition import (acquire_supplier_search, collect_supplier_search, create_supplier_handlers,
    fetch_supplier_native_page)
from product_radar.trends_controller import (build_trends_request, capture_direct_trends, create_trends_handlers,
    reconcile_direct_trends, replay_direct_trends)
from test_marketplace_browser import snapshot as marketplace_snapshot
from test_trends_controller import snapshot as trends_snapshot
from test_public_pages import resolver
from test_quickcommerce import LOCATION
from test_supplier_route_acquisition import BODIES, QUERY, Reader, receipt as supplier_receipt


NOW = "2026-10-08T10:00:00+00:00"


@unittest.skipUnless(hasattr(signal, "setitimer"), "Requires the installed POSIX outer deadline seam")
class SourceDeadlineTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(); self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.memory = ResearchMemory(self.root / "research", Journal(self.root / "finance"), evidence_type="synthetic")
        self.description = {"label": "Sink caddy", "category": "Home and Kitchen", "function": "Store sink essentials",
                            "mechanism": "manual", "audience": "adult", "features": ["draining"],
                            "basis": "observed", "brand": "generic_or_small", "reason": "Synthetic fixture."}
        self.memory.put("concepts", "first", {"status": "active", "description": self.description})
        self.handler, self.timer = signal.getsignal(signal.SIGALRM), signal.getitimer(signal.ITIMER_REAL)
        self.addCleanup(self.restore_alarm)

    def restore_alarm(self):
        signal.setitimer(signal.ITIMER_REAL, 0)
        signal.signal(signal.SIGALRM, self.handler)
        if self.timer[0]:
            signal.setitimer(signal.ITIMER_REAL, *self.timer)

    def blocked_reader(self, calls):
        def read(*args, **kwargs):
            calls.append((args, kwargs))
            time.sleep(0.1)
            raise AssertionError("The outer deadline did not interrupt the offline reader")
        return read

    def installed_native(self):
        browser, powershell = self.root / "browser.exe", self.root / "powershell.exe"
        browser.write_text("Offline executable fixture"); powershell.write_text("Offline executable fixture")
        stack = ExitStack()
        stack.enter_context(patch("product_radar.native_public_browser.CHROME", browser))
        stack.enter_context(patch("product_radar.native_public_browser.POWERSHELL", powershell))
        return stack

    def native_runner(self, calls, *, dispatch=False, cleanup_ok=True, preparation_delay=False, cancel_after_proof=False):
        def run(command, **kwargs):
            if command[0] == "wslpath":
                if preparation_delay:
                    time.sleep(0.1)
                return SimpleNamespace(stdout=command[-1] + "\n", returncode=0)
            script = base64.b64decode(command[-1]).decode("utf-16le")
            match = re.search(r"FromBase64String\('([^']+)'\)", script)
            payload = json.loads(base64.b64decode(match[1]))
            if "output" in payload:
                calls.append("launch")
                atomic_json(Path(payload["ownership"]), {"pid": 123, "browser": payload["browser"], "profile": payload["profile"]})
                if dispatch:
                    atomic_json(Path(payload["dispatch"]), {"url": payload["url"], "navigation_started": True,
                        "navigation_started_at": datetime.now(timezone.utc).isoformat()})
                if cancel_after_proof:
                    raise SourceActionDeadline()
                time.sleep(0.1)
                raise AssertionError("The outer deadline did not interrupt the offline native launch")
            calls.append("cleanup")
            self.assertEqual(payload["pid"], 123)
            self.assertIn("taskkill.exe /PID $owned.pid /T /F", script)
            return SimpleNamespace(returncode=0 if cleanup_ok else 2, stdout=b"", stderr=b"")
        return run

    def test_twenty_millisecond_outer_deadline_cannot_be_swallowed_as_provider_timeout(self):
        catches, cleanup = [], []
        def callback(_context):
            try:
                time.sleep(0.1)
            except (TimeoutError, OSError, Exception):
                catches.append("fallback")
                time.sleep(0.1)
            finally:
                cleanup.append("owned cleanup")
        started = time.monotonic()
        with self.assertRaises(SourceActionDeadline):
            _bounded_call(callback, {}, 0.02)
        self.assertLess(time.monotonic() - started, 0.2)
        self.assertEqual(catches, [])
        self.assertEqual(cleanup, ["owned cleanup"])
        self.assertNotIsInstance(SourceActionDeadline(), Exception)
        self.assertNotIsInstance(SourceActionDeadline(), OSError)

    def test_prior_alarm_handler_remaining_time_and_interval_restore_on_return_and_deadline(self):
        def original(_signum, _frame):
            self.fail("The existing long alarm should not fire in this short fixture")
        for cancelled in (False, True):
            with self.subTest(cancelled=cancelled):
                signal.signal(signal.SIGALRM, original)
                signal.setitimer(signal.ITIMER_REAL, 1, 0.8)
                if cancelled:
                    with self.assertRaises(SourceActionDeadline):
                        _bounded_call(lambda _: time.sleep(0.1), {}, 0.02)
                else:
                    _bounded_call(lambda _: time.sleep(0.005), {}, 0.02)
                remaining, interval = signal.getitimer(signal.ITIMER_REAL)
                self.assertIs(signal.getsignal(signal.SIGALRM), original)
                self.assertGreater(remaining, 0.5); self.assertLess(remaining, 1)
                self.assertAlmostEqual(interval, 0.8, places=3)
                signal.setitimer(signal.ITIMER_REAL, 0)

    def test_nested_deadline_unwinds_without_leaving_an_outer_signal_handler(self):
        prior = signal.getsignal(signal.SIGALRM)
        with self.assertRaises(SourceActionDeadline):
            _bounded_call(lambda _: _bounded_call(lambda __: time.sleep(0.1), {}, 0.02), {}, 0.5)
        self.assertIs(signal.getsignal(signal.SIGALRM), prior)
        self.assertEqual(signal.getitimer(signal.ITIMER_REAL), (0, 0))

    def test_supplier_search_deadline_stops_browser_fallback_and_healthy_sibling_runs(self):
        questions = [{"route": route, "request": {"query": QUERY, "country": "IN"}, "concept_ids": ["first"]}
                     for route in ("google-maps", "meta-ads")]
        plan = prepare_source_tasks(self.memory, questions, run_id="deadline-run", as_of=NOW)
        calls, browser_calls = [], []
        handlers = create_supplier_handlers(self.memory, http_reader=self.blocked_reader(calls),
                                           browser_reader=self.blocked_reader(browser_calls))
        def healthy(context):
            context["record_dispatch"]()
            return {"schema_version": 1, "route": context["route"], "acquisition_id": context["acquisition_id"],
                    "observed_at": NOW, "state": "usable", "dispatch_started": True,
                    "reference": "synthetic-healthy/receipt.json", "capture_ids": [], "observation_ids": [],
                    "target_results": [], "reason": "Synthetic healthy sibling", "retryable": False}
        handlers["meta-ads"] = healthy
        result = execute_source_run(self.memory, "deadline-run", handlers=handlers, as_of=NOW, timeout_seconds=0.02)
        self.assertEqual(result["counts"], {"failed": 1, "acquired": 1})
        blocked = next(row for row in result["results"] if row["state"] == "failed")
        self.assertEqual(blocked["reason"], "SOURCE_ACTION_TIMEOUT")
        self.assertTrue(blocked["dispatch_uncertain"])
        self.assertEqual(len(calls), 1); self.assertEqual(browser_calls, [])
        original = deepcopy(self.memory.snapshot()["query_tasks"][blocked["task_id"]])
        self.assertEqual(original["generation"], 1)
        self.assertEqual(original["tries"], 0)
        self.assertTrue(original["reconcile_required"])
        self.assertIn(blocked["task_id"], plan["task_ids"])
        # The short first timer tests interruption. This second pass tests
        # saved-only proof replay; do not randomly cancel local file work.
        replay = execute_source_run(self.memory, "deadline-run", handlers=handlers, as_of=NOW, timeout_seconds=2)
        self.assertEqual(len(calls), 1); self.assertEqual(browser_calls, [])
        self.assertEqual(replay["counts"], {"blocked": 1, "reused": 1})
        self.assertTrue(next(row for row in replay["results"] if row["state"] == "blocked")["dispatch_uncertain"])
        self.assertEqual(self.memory.snapshot()["query_tasks"][blocked["task_id"]]["acquisition_id"], original["acquisition_id"])

    def test_supplier_bundle_deadline_stops_other_seed_and_linked_page_reads(self):
        calls = []
        product = {"concept_id": "first", "description": self.description}
        lead = {"id": "supplier-lead", "urls": ["https://supplier.example.org/one", "https://supplier.example.org/two"]}
        with self.assertRaises(SourceActionDeadline):
            _bounded_call(lambda _: capture_supplier_bundle(product, lead, fetcher=self.blocked_reader(calls)), {}, 0.02)
        self.assertEqual(len(calls), 1)

    def test_ordinary_supplier_transport_timeout_still_allows_one_bounded_fallback(self):
        calls, browser_calls = [], []
        def ordinary(item):
            calls.append(item)
            raise TimeoutError("Synthetic provider timeout")
        def browser(item):
            browser_calls.append(item)
            return supplier_receipt(item, body=BODIES["google-suppliers"])
        result = _bounded_call(lambda _: acquire_supplier_search(self.root, route="google-suppliers", query=QUERY,
            acquisition_id="ordinary-timeout", http_reader=ordinary, browser_reader=browser, as_of=NOW), {}, 0.5)
        self.assertEqual(len(calls), 1); self.assertEqual(len(browser_calls), 1)
        self.assertEqual(result["record"]["result"]["state"], "complete")

    def test_public_worker_is_aborted_before_it_can_connect_after_outer_deadline(self):
        release, finished = threading.Event(), threading.Event()
        requests, controls = [], []
        def slow_resolver(*args, **kwargs):
            release.wait(0.5)
            return resolver(*args, **kwargs)
        def no_connection(*args):
            requests.append(args)
            raise AssertionError("Cancelled worker must not connect")
        original_abort = _Control.abort
        def abort(control):
            controls.append(control)
            original_abort(control)
            release.set()
        with patch("product_radar.public_pages._Control.abort", abort):
            with self.assertRaises(SourceActionDeadline):
                _bounded_call(lambda _: fetch_public_page({"id": "public-fixture", "url": "https://supplier.example.org/one"},
                    resolver=slow_resolver, connection_factory=no_connection), {}, 0.02)
        self.assertEqual(len(controls), 1)
        self.assertTrue(controls[0].cancelled.is_set())
        # Only the pre-existing bounded reader thread is allowed to finish;
        # its cancellation prevents connection or redirect work.
        for thread in threading.enumerate():
            if thread.name == "neuraflow-public-page":
                thread.join(timeout=0.5)
                self.assertFalse(thread.is_alive())
        self.assertEqual(requests, [])

    def test_native_outer_deadline_cleans_exact_owned_process_and_retains_unknown_navigation(self):
        calls = []; folder = self.root / "native"; folder.mkdir()
        with self.installed_native(), self.assertRaises(SourceActionDeadline) as raised:
            _bounded_call(lambda _: native_snapshot("https://www.amazon.in/s?k=sink+caddy", "'fixture'", folder,
                runner=self.native_runner(calls), timeout=15), {}, 0.02)
        self.assertEqual(calls, ["launch", "cleanup"])
        proof = load_json(folder / "native_outer_deadline.json", 16384)
        self.assertIsNone(proof["navigation_started"])
        self.assertTrue(proof["owned_process_exited"])
        self.assertEqual(raised.exception.native_result["reason"], "SOURCE_ACTION_TIMEOUT")
        self.assertIsNone(proof["snapshot"])

    def test_native_cleanup_failure_still_propagates_deadline_and_never_starts_fallback(self):
        calls = []; folder = self.root / "native"; folder.mkdir()
        with self.installed_native(), self.assertRaises(SourceActionDeadline):
            _bounded_call(lambda _: native_snapshot("https://www.amazon.in/s?k=sink+caddy", "'fixture'", folder,
                runner=self.native_runner(calls, cleanup_ok=False), timeout=15), {}, 0.02)
        self.assertEqual(calls, ["launch", "cleanup"])
        proof = load_json(folder / "native_outer_deadline.json", 16384)
        self.assertFalse(proof["owned_process_exited"])
        self.assertEqual(proof["cleanup_reason"], "NATIVE_PUBLIC_CLEANUP_UNVERIFIED")

    def test_native_path_preparation_deadline_is_proved_not_launched(self):
        calls = []; folder = self.root / "native"; folder.mkdir()
        with self.installed_native(), self.assertRaises(SourceActionDeadline):
            _bounded_call(lambda _: native_snapshot("https://www.amazon.in/s?k=sink+caddy", "'fixture'", folder,
                runner=self.native_runner(calls, preparation_delay=True), timeout=15), {}, 0.02)
        self.assertEqual(calls, [])
        proof = load_json(folder / "native_outer_deadline.json", 16384)
        self.assertIs(proof["navigation_started"], False)
        self.assertIs(proof["launch_invoked"], False)
        self.assertIs(proof["owned_process_exited"], True)

    def test_marketplace_deadline_retains_unknown_receipt_and_replay_creates_no_observations(self):
        calls = []; request = build_marketplace_request("amazon-search", term="sink caddy")
        with self.assertRaises(SourceActionDeadline):
            _bounded_call(lambda _: capture_marketplace(request, "market-deadline", data_root=self.root / "market",
                snapshot_reader=self.blocked_reader(calls)), {}, 0.02)
        folder = self.root / "market" / "market-deadline"
        saved = replay_marketplace(folder); raw = (folder / "receipt.json").read_bytes()
        self.assertEqual(saved["reason"], "SOURCE_ACTION_TIMEOUT")
        self.assertIsNone(saved["dispatch_started"])
        self.assertEqual(saved["state"], "failed")
        imported = import_direct_marketplace(self.memory, folder)
        self.assertEqual(imported["observation_ids"], [])
        self.assertEqual(replay_marketplace(folder), saved)
        self.assertEqual((folder / "receipt.json").read_bytes(), raw)
        self.assertEqual(len(calls), 1)

    def test_marketplace_native_deadline_preserves_exact_dispatch_and_cleanup_proof(self):
        calls = []; request = build_marketplace_request("amazon-search", term="sink caddy")
        actual_native = native_snapshot
        def native(url, expression, folder, **kwargs):
            return actual_native(url, expression, folder, runner=self.native_runner(calls, dispatch=True), **kwargs)
        with self.installed_native(), patch("product_radar.native_public_browser.native_snapshot", native), \
                self.assertRaises(SourceActionDeadline):
            _bounded_call(lambda _: capture_marketplace(request, "native-market-deadline", data_root=self.root / "market"), {}, 0.02)
        folder = self.root / "market" / "native-market-deadline"
        saved = replay_marketplace(folder)
        dispatch = load_json(folder / "native_dispatch.json", 16384)
        self.assertIs(saved["dispatch_started"], True)
        self.assertEqual(saved["dispatch_started_at"], dispatch["navigation_started_at"])
        self.assertIs(saved["owned_process_exited"], True)
        self.assertEqual(saved["state"], "failed")
        self.assertEqual(calls, ["launch", "cleanup"])

    def test_trends_deadline_is_not_no_data_or_provider_block_and_replays_without_requests(self):
        calls = []; request = build_trends_request("sink caddy")
        with self.assertRaises(SourceActionDeadline):
            _bounded_call(lambda _: capture_direct_trends(request, "trends-deadline", data_root=self.root / "trends",
                snapshot_reader=self.blocked_reader(calls)), {}, 0.02)
        folder = self.root / "trends" / "trends-deadline"
        saved = replay_direct_trends(folder); raw = (folder / "receipt.json").read_bytes()
        self.assertEqual(saved["reason"], "SOURCE_ACTION_TIMEOUT")
        self.assertEqual(saved["state"], "failed")
        self.assertIsNone(saved["dispatch_started"])
        self.assertNotIn("interpretation.json", saved["artifacts"])
        self.assertEqual(replay_direct_trends(folder), saved)
        self.assertEqual((folder / "receipt.json").read_bytes(), raw)
        self.assertEqual(len(calls), 1)

    def test_quickcommerce_deadline_does_not_trigger_funded_fallback(self):
        calls = []
        class Funded:
            def search(self, *args, **kwargs):
                raise AssertionError("Outer public deadline must not trigger funded fallback")
        handlers = create_quickcommerce_handlers(self.memory, Funded(), native_capture=self.blocked_reader(calls))
        context = {"route": "blinkit", "acquisition_id": "commerce-deadline", "request": {"query": "sink caddy", "location": LOCATION},
                   "concept_ids": ["first"], "description_hashes": {"first": digest(self.description)}}
        with self.assertRaises(SourceActionDeadline):
            _bounded_call(handlers["blinkit"], context, 0.02)
        self.assertEqual(len(calls), 1)

    def test_quickcommerce_native_cancellation_reconciles_exact_proof_without_funded_or_browser_repeat(self):
        calls = []
        class Funded:
            def search(self, *args, **kwargs):
                raise AssertionError("Saved outer cancellation must not trigger funded fallback")
        def native(url, expression, folder):
            return native_snapshot(url, expression, folder, runner=self.native_runner(calls, dispatch=True, cancel_after_proof=True))
        handler = create_quickcommerce_handlers(self.memory, Funded(), native_capture=native)["blinkit"]
        context = {"route": "blinkit", "acquisition_id": "commerce-native-cancel", "request": {"query": "sink caddy", "location": LOCATION},
                   "concept_ids": ["first"], "description_hashes": {"first": digest(self.description)}}
        with self.installed_native(), self.assertRaises(SourceActionDeadline):
            _bounded_call(handler, context, 0.5)
        proof_path = self.memory.root / "quickcommerce_free/acquisitions/commerce-native-cancel/native/native_outer_deadline.json"
        original = proof_path.read_bytes()
        saved = handler(dict(context, reconciling=True))
        self.assertEqual(saved["reason"], "SOURCE_ACTION_TIMEOUT")
        self.assertEqual(saved["state"], "failed")
        self.assertIs(saved["dispatch_started"], True)
        self.assertEqual(saved["observation_ids"], [])
        self.assertEqual(handler(dict(context, reconciling=True)), saved)
        self.assertEqual(proof_path.read_bytes(), original)
        self.assertEqual(calls, ["launch", "cleanup"])
        changed = dict(context, reconciling=True, request={"query": "sink caddy", "location": dict(LOCATION, name="Different location")})
        with self.assertRaises(SafetyError):
            handler(changed)

    def test_supplier_native_cancellation_retains_failed_attempt_and_reconciles_without_search_repeat(self):
        calls = []
        actual_native = native_snapshot
        def native(url, expression, folder, **kwargs):
            return actual_native(url, expression, folder, runner=self.native_runner(calls, dispatch=True, cancel_after_proof=True), **kwargs)
        # Primary reader returned no proved request; the browser then starts
        # the original request and is cancelled. No result cards are invented.
        def primary(item):
            return supplier_receipt(item, status=500, actual=False)
        handlers = create_supplier_handlers(self.memory, http_reader=primary)
        context = {"route": "google-maps", "acquisition_id": "supplier-native-cancel", "request": {"query": QUERY},
                   "concept_ids": ["first"], "description_hashes": {"first": digest(self.description)}, "as_of": NOW}
        with self.installed_native(), patch("product_radar.native_public_browser.native_snapshot", native), \
                self.assertRaises(SourceActionDeadline):
            _bounded_call(handlers["google-maps"]["handler"], context, 0.5)
        saved = handlers["google-maps"]["reconcile"](context)
        self.assertEqual(saved["state"], "failed")
        self.assertEqual(saved["reason"], "SOURCE_ACTION_TIMEOUT")
        self.assertIs(saved["dispatch_started"], True)
        capture = Path(saved["reference"]); original = capture.read_bytes()
        self.assertEqual(load_json(capture)["result"]["results"], [])
        self.assertEqual(handlers["google-maps"]["reconcile"](context), saved)
        self.assertEqual(capture.read_bytes(), original)
        self.assertEqual(calls, ["launch", "cleanup"])

    def test_old_supplier_false_no_dispatch_is_corrected_only_from_original_bound_navigation(self):
        acquisition = "old-supplier-dispatch"
        query = "Soft Snooze adjustable baby crib"
        calls = []
        def primary(item):
            calls.append("http")
            return supplier_receipt(item, status=500, reason="PUBLIC_FETCH_DEADLINE", actual=False)
        def browser(item):
            calls.append("browser")
            folder = self.memory.root / "supplier_routes/native" / item["id"]
            folder.mkdir(parents=True)
            atomic_json(folder / "native_dispatch.json", {"url": item["url"], "navigation_started": True,
                "navigation_started_at": NOW})
            return dict(supplier_receipt(item, status=500, reason="SUPPLIER_NATIVE_READ_FAILED", actual=False),
                native_reference=str(folder / "native_result.json"), native_reason="NATIVE_PUBLIC_EXECUTION_FAILED",
                native_stage="result_read", checked_at=NOW)
        initial = acquire_supplier_search(self.memory.root, route="google-suppliers", query=query,
            acquisition_id=acquisition, http_reader=primary, browser_reader=browser, as_of=NOW)
        original_path = Path(initial["reference"]); original = original_path.read_bytes()
        handlers = create_supplier_handlers(self.memory, http_reader=self.blocked_reader(calls), browser_reader=self.blocked_reader(calls))
        context = {"route": "google-suppliers", "acquisition_id": acquisition, "request": {"query": query},
            "concept_ids": ["first"], "description_hashes": {"first": digest(self.description)}, "as_of": NOW}
        saved = handlers["google-suppliers"]["reconcile"](context)
        self.assertEqual(saved["state"], "failed"); self.assertEqual(saved["reason"], "SUPPLIER_NATIVE_READ_FAILED")
        self.assertIs(saved["dispatch_started"], True)
        self.assertEqual(saved["observation_ids"], []); self.assertEqual(calls, ["http", "browser"])
        recovered = load_json(Path(saved["reference"])); receipt = recovered["records"][-1]["receipt"]
        self.assertEqual(receipt["execution_stage"], "attempt_started")
        self.assertIsNone(receipt["owned_process_exited"])
        self.assertEqual(receipt["attempt_started_at"], NOW)
        self.assertEqual(recovered["result"], initial["record"]["result"])
        self.assertEqual(original_path.read_bytes(), original)
        self.assertEqual(handlers["google-suppliers"]["reconcile"](context), saved)
        dispatch = self.memory.root / "supplier_routes/native" / key("supplier-route-request", [acquisition, "browser"]) / "native_dispatch.json"
        value = load_json(dispatch); value["url"] = "https://www.google.com/search?q=changed"
        atomic_json(dispatch, value)
        with self.assertRaisesRegex(SafetyError, "SAVED_SUPPLIER_ROUTE_INVALID"):
            handlers["google-suppliers"]["reconcile"](context)
        self.assertEqual(calls, ["http", "browser"])

    def test_installed_controller_automatically_recovers_terminal_false_supplier_dispatch_once(self):
        calls, requests = [], []
        def primary(item):
            calls.append("http")
            return supplier_receipt(item, status=500, reason="PUBLIC_FETCH_DEADLINE", actual=False)
        def browser(item):
            calls.append("browser"); requests.append(deepcopy(item))
            folder = self.memory.root / "supplier_routes/native" / item["id"]
            folder.mkdir(parents=True)
            return dict(supplier_receipt(item, status=500, reason="SUPPLIER_NATIVE_READ_FAILED", actual=False),
                native_reference=str(folder / "native_result.json"), native_reason="NATIVE_PUBLIC_EXECUTION_FAILED",
                native_stage="result_read", checked_at=NOW)
        handlers = create_supplier_handlers(self.memory, http_reader=primary, browser_reader=browser)
        plan = prepare_source_tasks(self.memory, [{"route": "google-maps", "request": {"query": QUERY},
            "concept_ids": ["first"]}], run_id="old-terminal-auto", as_of=NOW)
        first = execute_source_run(self.memory, "old-terminal-auto", handlers=handlers, as_of=NOW)
        task_id = plan["task_ids"][0]; old = deepcopy(self.memory.snapshot()["query_tasks"][task_id])
        self.assertEqual(old["status"], "failed"); self.assertFalse(old["reconcile_required"])
        self.assertFalse(old["retryable"]); self.assertEqual(old["tries"], 0)
        self.assertEqual(old["prelaunch_failures"], 1)
        capture_path = self.memory.root / "supplier_routes" / old["acquisition_id"] / "capture.json"
        original = capture_path.read_bytes()
        checks = {cid: deepcopy(self.memory.snapshot()["source_checks"][cid]) for cid in old["receipt_refs"]}
        item = requests[0]
        atomic_json(self.memory.root / "supplier_routes/native" / item["id"] / "native_dispatch.json",
            {"url": item["url"], "navigation_started": True, "navigation_started_at": NOW})
        def interrupted_admission(_callback, context, _timeout):
            self.assertTrue(context["reconciling"])
            self.assertEqual(context["terminal_recovery"], "supplier-original-dispatch-recovery-v1")
            raise SourceActionDeadline()
        interrupted = execute_source_run(self.memory, "old-terminal-auto", handlers=handlers, as_of=NOW,
            bounded_call=interrupted_admission)
        self.assertEqual(interrupted["counts"], {"failed": 1})
        pending = self.memory.snapshot()["query_tasks"][task_id]
        self.assertTrue(pending["reconcile_required"])
        self.assertEqual(pending["acquisition_id"], old["acquisition_id"])
        self.assertEqual(pending["generation"], old["generation"])
        self.assertEqual(calls, ["http", "browser"])
        second = execute_source_run(self.memory, "old-terminal-auto", handlers=handlers, as_of=NOW)
        self.assertEqual(second["handler_calls"], 1); self.assertEqual(second["counts"], {"failed": 1})
        self.assertTrue(second["results"][0]["actual_attempt"])
        recovered = self.memory.snapshot()["query_tasks"][task_id]
        self.assertEqual(recovered["acquisition_id"], old["acquisition_id"])
        self.assertEqual(recovered["generation"], old["generation"])
        self.assertIs(recovered["dispatch_started"], True)
        self.assertEqual(recovered["tries"], 1); self.assertEqual(recovered["prelaunch_failures"], 0)
        self.assertFalse(recovered["retryable"]); self.assertFalse(recovered["reconcile_required"])
        self.assertEqual(recovered["reason"], "SUPPLIER_NATIVE_READ_FAILED")
        self.assertEqual(capture_path.read_bytes(), original)
        self.assertTrue(set(old["receipt_refs"]) < set(recovered["receipt_refs"]))
        self.assertEqual({cid: self.memory.snapshot()["source_checks"][cid] for cid in checks}, checks)
        again = execute_source_run(self.memory, "old-terminal-auto", handlers=handlers, as_of=NOW)
        self.assertEqual(again["handler_calls"], 0); self.assertEqual(calls, ["http", "browser"])
        self.assertEqual(self.memory.snapshot()["batch_runs"][key("source-run", "old-terminal-auto")]["route_pauses"], {})

    def test_terminal_supplier_recovery_without_proof_is_checked_once_and_never_relaunched(self):
        calls = []
        def fail(item):
            calls.append(item["url"])
            return dict(supplier_receipt(item, status=500, reason="SUPPLIER_NATIVE_READ_FAILED", actual=False),
                native_reference=str(self.memory.root / "supplier_routes/native" / item["id"] / "native_result.json"),
                checked_at=NOW)
        handlers = create_supplier_handlers(self.memory, http_reader=fail, browser_reader=fail)
        plan = prepare_source_tasks(self.memory, [{"route": "google-maps", "request": {"query": QUERY},
            "concept_ids": ["first"]}], run_id="old-terminal-no-proof", as_of=NOW)
        execute_source_run(self.memory, "old-terminal-no-proof", handlers=handlers, as_of=NOW)
        old = deepcopy(self.memory.snapshot()["query_tasks"][plan["task_ids"][0]])
        recovery = execute_source_run(self.memory, "old-terminal-no-proof", handlers=handlers, as_of=NOW)
        self.assertEqual(recovery["handler_calls"], 1)
        current = self.memory.snapshot()["query_tasks"][plan["task_ids"][0]]
        self.assertEqual(current["tries"], 0); self.assertEqual(current["generation"], old["generation"])
        self.assertEqual(current["prelaunch_failures"], old["prelaunch_failures"])
        self.assertIs(current["dispatch_started"], False)
        again = execute_source_run(self.memory, "old-terminal-no-proof", handlers=handlers, as_of=NOW)
        self.assertEqual(again["handler_calls"], 0); self.assertEqual(len(calls), 2)

    def test_supplier_complete_native_data_survives_wrapper_deadline_without_a_second_search(self):
        calls = []
        def primary(item):
            calls.append("http")
            return supplier_receipt(item, status=500, actual=False)
        def native(url, expression, folder, **kwargs):
            calls.append("native")
            at = datetime.now(timezone.utc).isoformat()
            result = {"state": "captured", "reason": "Original saved fixture", "stage": "complete",
                "navigation_status": 200, "navigation_started": True, "navigation_started_at": at,
                "owned_process_exited": True, "completed_at": at, "snapshot": {"url": url, "title": "Suppliers",
                    "html": BODIES["google-maps"], "text": "Food wrap paper supplier India", "links": []}}
            atomic_json(folder / "native_result.json", result)
            atomic_json(folder / "native_dispatch.json", {"url": url, "navigation_started": True,
                "navigation_started_at": at})
            return result
        handlers = create_supplier_handlers(self.memory, http_reader=primary)
        context = {"route": "google-maps", "acquisition_id": "supplier-finished-cancel", "request": {"query": QUERY},
            "concept_ids": ["first"], "description_hashes": {"first": digest(self.description)}, "as_of": NOW}
        with patch("product_radar.native_public_browser.native_snapshot", native), \
                patch("product_radar.supplier_route_acquisition._supplier_native_output_receipt", side_effect=SourceActionDeadline()), \
                self.assertRaises(SourceActionDeadline):
            handlers["google-maps"]["handler"](context)
        folder = self.memory.root / "supplier_routes" / context["acquisition_id"]
        progress = (folder / "progress.json").read_bytes()
        native_folder = self.memory.root / "supplier_routes/native" / key("supplier-route-request", [context["acquisition_id"], "browser"])
        original_native = (native_folder / "native_result.json").read_bytes()
        saved = handlers["google-maps"]["reconcile"](context)
        self.assertEqual(saved["state"], "interpretation_pending"); self.assertIs(saved["dispatch_started"], True)
        self.assertEqual(saved["observation_ids"], [])  # Public cards are only leads; F still judges supplier terms.
        captured = load_json(Path(saved["reference"]))
        self.assertEqual(len(captured["result"]["results"]), 1)
        self.assertEqual(handlers["google-maps"]["reconcile"](context), saved)
        self.assertEqual((folder / "progress.json").read_bytes(), progress)
        self.assertEqual((native_folder / "native_result.json").read_bytes(), original_native)
        self.assertEqual(calls, ["http", "native"])

    def test_meta_native_cancellation_reconciles_exact_failure_and_no_second_browser(self):
        calls = []
        def native(url, expression, folder, **kwargs):
            return native_snapshot(url, expression, folder, runner=self.native_runner(calls, dispatch=True, cancel_after_proof=True), **kwargs)
        handlers = meta_direct_source_handlers(self.memory, browser=native)
        context = {"route": "meta-ads", "acquisition_id": "meta-native-cancel", "request": {"query": "sink caddy"},
            "concept_ids": ["first"], "description_hashes": {"first": digest(self.description)}}
        with self.installed_native(), self.assertRaises(SourceActionDeadline):
            _bounded_call(handlers["handler"], context, .5)
        folder = self.memory.root / "direct_meta" / context["acquisition_id"]
        receipt_bytes = (folder / "receipt.json").read_bytes()
        native_bytes = (folder / "native_outer_deadline.json").read_bytes()
        saved = handlers["reconcile"](context)
        self.assertEqual(saved["state"], "failed"); self.assertEqual(saved["reason"], "SOURCE_ACTION_TIMEOUT")
        self.assertIs(saved["dispatch_started"], True); self.assertEqual(saved["observation_ids"], [])
        self.assertEqual(saved["failure_scope"], "question")
        self.assertEqual(handlers["reconcile"](context), saved)
        self.assertEqual(calls, ["launch", "cleanup"])
        self.assertEqual((folder / "receipt.json").read_bytes(), receipt_bytes)
        self.assertEqual((folder / "native_outer_deadline.json").read_bytes(), native_bytes)

    def test_meta_complete_native_data_recovery_is_additive_and_keeps_original_observation_date(self):
        request = prepare_meta_query("sink caddy"); calls = []
        def native(url, expression, folder, **kwargs):
            calls.append(url); now = datetime.now(timezone.utc).isoformat()
            result = {"state": "captured", "stage": "complete", "reason": "Original Meta fixture",
                "navigation_started": True, "navigation_started_at": now, "completed_at": now,
                "navigation_status": 200, "owned_process_exited": True,
                "snapshot": {"url": url, "title": "Meta Ad Library", "text": "Ad Library",
                    "cards": [{"ad_id": "123456", "text": "Active\nLibrary ID: 123456\nSink caddy", "links": [], "images": []}]}}
            atomic_json(folder / "native_result.json", result)
            atomic_json(folder / "native_dispatch.json", {"url": url, "navigation_started": True, "navigation_started_at": now})
            return result
        acquisition = "finished-meta"
        with patch("product_radar.meta_browser.parse_meta_snapshot", side_effect=SourceActionDeadline()), self.assertRaises(SourceActionDeadline):
            capture_meta_search(self.memory.root / "direct_meta", acquisition, "sink caddy", browser=native)
        folder = self.memory.root / "direct_meta" / acquisition
        original = (folder / "receipt.json").read_bytes(); native_bytes = (folder / "native_result.json").read_bytes()
        snapshot_bytes = (folder / "snapshot.json").read_bytes()
        observed = load_json(folder / "native_result.json")["completed_at"]
        handlers = meta_direct_source_handlers(self.memory, browser=self.blocked_reader(calls))
        context = {"route": "meta-ads", "acquisition_id": acquisition, "request": {"query": "sink caddy"},
            "concept_ids": ["first"], "description_hashes": {"first": digest(self.description)}}
        # Preserve an already imported original failure as well as its bytes.
        failed = handlers["handler"](context)
        original_capture = deepcopy(self.memory.snapshot()["captures"][failed["capture_ids"][0]])
        saved = handlers["reconcile"](context)
        self.assertEqual(saved["state"], "usable"); self.assertEqual(len(saved["observation_ids"]), 1)
        self.assertNotEqual(saved["capture_ids"], failed["capture_ids"])
        self.assertEqual(saved["observed_at"], observed)
        state = self.memory.snapshot()
        self.assertEqual(state["captures"][failed["capture_ids"][0]], original_capture)
        self.assertEqual(state["observations"][saved["observation_ids"][0]]["observed_at"], observed)
        self.assertEqual(state["observations"][saved["observation_ids"][0]]["acquisition_id"], acquisition)
        self.assertEqual(handlers["reconcile"](context), saved)
        self.assertEqual(calls, [request["url"]])
        self.assertEqual((folder / "receipt.json").read_bytes(), original)
        self.assertEqual((folder / "native_result.json").read_bytes(), native_bytes)
        self.assertEqual((folder / "snapshot.json").read_bytes(), snapshot_bytes)
        self.assertIsInstance(load_json(folder / "snapshot.json"), dict)
        changed = load_json(folder / "native_result.json"); changed["snapshot"]["title"] = "Changed"
        atomic_json(folder / "native_result.json", changed)
        with self.assertRaisesRegex(SafetyError, "META_PUBLIC_CAPTURE_CHANGED"):
            handlers["reconcile"](context)

    def test_meta_saved_native_cancel_cannot_rebind_another_query(self):
        calls = []
        def native(url, expression, folder, **kwargs):
            return native_snapshot(url, expression, folder, runner=self.native_runner(calls, dispatch=True, cancel_after_proof=True), **kwargs)
        with self.installed_native(), self.assertRaises(SourceActionDeadline):
            capture_meta_search(self.root / "meta", "changed-meta", "sink caddy", browser=native)
        with self.assertRaisesRegex(SafetyError, "META_PUBLIC_RECEIPT_CONFLICT"):
            capture_meta_search(self.root / "meta", "changed-meta", "different query", browser=native, reconcile=True)
        self.assertEqual(calls, ["launch", "cleanup"])

    def test_meta_deadline_after_successful_receipt_save_does_not_overwrite_original_bytes(self):
        calls = []
        request = prepare_meta_query("sink caddy")
        def native(url, expression, folder, **kwargs):
            calls.append(url)
            now = datetime.now(timezone.utc).isoformat()
            result = {"state": "captured", "stage": "complete", "reason": "Complete fixture",
                "navigation_started": True, "navigation_started_at": now, "completed_at": now,
                "navigation_status": 200, "owned_process_exited": True,
                "snapshot": {"url": url, "title": "Meta", "text": "No ads match this search", "cards": []}}
            atomic_json(folder / "native_result.json", result)
            atomic_json(folder / "native_dispatch.json", {"url": url, "navigation_started": True,
                "navigation_started_at": now})
            return result
        original_receipts = []
        writer = atomic_json
        def save(path, value):
            writer(path, value)
            if path.name == "receipt.json":
                original_receipts.append(path.read_bytes())
                raise SourceActionDeadline()
        with patch("product_radar.meta_browser.atomic_json", side_effect=save), self.assertRaises(SourceActionDeadline):
            capture_meta_search(self.root / "meta", "saved-before-deadline", "sink caddy", browser=native)
        receipt, folder = capture_meta_search(self.root / "meta", "saved-before-deadline", "sink caddy",
            browser=self.blocked_reader(calls), reconcile=True)
        self.assertEqual(receipt["state"], "empty")
        self.assertEqual((folder / "receipt.json").read_bytes(), original_receipts[0])
        self.assertFalse((folder / "outer_deadline.json").exists())
        self.assertEqual(calls, [request["url"]])

    def test_supplier_company_deadline_preserves_unvisited_sibling_and_no_second_company_request(self):
        self.memory.put("concepts", "second", {"status": "active", "description": self.description})
        search = collect_supplier_search(self.memory, route="google-suppliers", concept_ids=["first", "second"],
            query=QUERY, acquisition_id="company-input", http_reader=Reader(), as_of=NOW)
        calls, primary_calls = [], []
        def primary(item):
            primary_calls.append(item)
            return supplier_receipt(item, status=500, actual=False)
        actual_native = native_snapshot
        def native(url, expression, folder, **kwargs):
            return actual_native(url, expression, folder, runner=self.native_runner(calls, dispatch=True, cancel_after_proof=True), **kwargs)
        handlers = create_supplier_handlers(self.memory, page_reader=primary)
        context = {"route": "company-pages", "acquisition_id": "company-cancel", "request": {},
                   "concept_ids": ["first", "second"], "description_hashes": {cid: digest(self.description) for cid in ("first", "second")},
                   "as_of": NOW}
        with self.installed_native(), patch("product_radar.native_public_browser.native_snapshot", native), \
                self.assertRaises(SourceActionDeadline):
            _bounded_call(handlers["company-pages"]["handler"], context, 0.5)
        saved = handlers["company-pages"]["reconcile"](context)
        self.assertEqual(saved["state"], "failed")
        self.assertEqual(saved["reason"], "SOURCE_ACTION_TIMEOUT")
        self.assertIs(saved["dispatch_started"], True)
        self.assertEqual({row["concept_id"]: row["state"] for row in saved["target_results"]},
                         {"first": "failed", "second": "not_attempted"})
        self.assertEqual(handlers["company-pages"]["reconcile"](context), saved)
        self.assertEqual(calls, ["launch", "cleanup"])
        self.assertEqual(len(primary_calls), 1)

    def test_missing_dispatch_field_is_a_classified_invalid_proof_not_key_error(self):
        with self.assertRaisesRegex(SafetyError, "SOURCE_DEADLINE_PROOF_INVALID"):
            validate_interrupted_transport({"outer_deadline": True, "requested_url": "https://example.org/",
                "snapshot": None, "state": "failed", "reason": "SOURCE_ACTION_TIMEOUT", "stage": "outer_deadline",
                "owned_process_exited": True, "completed_at": NOW}, requested_url="https://example.org/")

    def test_installed_controller_keeps_reparse_proof_failures_local_and_healthy_source_runs(self):
        reasons = ("SOURCE_DEADLINE_PROOF_INVALID", "SOURCE_DEADLINE_ORIGINAL_HASH_MISMATCH",
                   "SOURCE_DEADLINE_ORIGINAL_INVALID", "SOURCE_DEADLINE_ORIGINAL_TIME_INVALID",
                   "MARKETPLACE_DEADLINE_REPARSE_CONFLICT", "DIRECT_TRENDS_DEADLINE_REPARSE_CONFLICT")
        for index, reason in enumerate(reasons):
            with self.subTest(reason=reason):
                run_id = "bad-proof-" + str(index)
                request = build_marketplace_request("amazon-search", term="sink caddy")
                plan = prepare_source_tasks(self.memory, [{"route": "amazon-search", "request": request,
                    "concept_ids": ["first"]}, {"route": "meta-ads", "request": {"query": "sink caddy"},
                    "concept_ids": ["first"]}], run_id=run_id, as_of=NOW)
                def untouched(*_args, **_kwargs):
                    raise SourceActionDeadline()
                with patch("product_radar.marketplace_browser.reconcile_marketplace", side_effect=SafetyError(reason)):
                    handlers = create_marketplace_handlers(self.memory, data_root=self.root / run_id, capture=untouched)
                first = execute_source_run(self.memory, run_id, handlers=handlers, as_of=NOW)
                failed = next(row for row in first["results"] if row.get("reason") == "SOURCE_ACTION_TIMEOUT")
                original = deepcopy(self.memory.snapshot()["query_tasks"][failed["task_id"]])
                attempts = []
                def healthy(context):
                    attempts.append(context["acquisition_id"])
                    context["record_dispatch"]()
                    return {"schema_version": 1, "route": context["route"], "acquisition_id": context["acquisition_id"],
                        "observed_at": NOW, "state": "usable", "dispatch_started": True,
                        "reference": "synthetic-healthy/receipt.json", "capture_ids": [], "observation_ids": [],
                        "target_results": [], "reason": "Healthy independent source", "retryable": False}
                handlers["meta-ads"] = healthy
                result = execute_source_run(self.memory, run_id, handlers=handlers, as_of=NOW)
                self.assertEqual(result["counts"], {"failed": 1, "acquired": 1})
                failed = next(row for row in result["results"] if row["state"] == "failed")
                self.assertEqual(failed["reason"], reason); self.assertTrue(failed["reconcile_required"])
                current = self.memory.snapshot()["query_tasks"][failed["task_id"]]
                self.assertEqual(current["acquisition_id"], original["acquisition_id"])
                self.assertEqual(current["generation"], original["generation"])
                self.assertEqual(current["tries"], 0); self.assertIsNone(current["next_retry_at"])
                self.assertEqual(len(attempts), 1)
                self.assertEqual(self.memory.snapshot()["batch_runs"][key("source-run", run_id)]["route_pauses"], {})

    def successful_saved_reader(self, kind, request, folder, *, status=200, wrong_scope=False):
        def read(_request):
            now = datetime.now(timezone.utc).isoformat()
            if kind == "marketplace":
                snapshot = marketplace_snapshot(request)
                if wrong_scope:
                    snapshot["query"] = "different product"
            else:
                snapshot = trends_snapshot(request["query"])
                if wrong_scope:
                    snapshot["text"] = snapshot["text"].replace("India", "Canada")
                raw = ("Week," + request["query"] + ": (India)\n2026-09-13,12\n2026-09-20,<1\n2026-09-27,20\n").encode()
                (folder / "multiTimeline.csv").write_bytes(raw)
            actual = {"state": "captured", "reason": "Offline original native fixture", "navigation_status": status,
                "navigation_started": True, "navigation_started_at": now, "completed_at": now,
                "owned_process_exited": True, "snapshot": snapshot, "stage": "complete"}
            atomic_json(folder / "native_result.json", actual)
            atomic_json(folder / "native_dispatch.json", {"url": request["url"] if kind == "marketplace" else snapshot["url"],
                "navigation_started": True, "navigation_started_at": now})
            return actual
        return read

    def completed_capture_then_deadline(self, kind, name, *, status=200, wrong_scope=False):
        request = build_marketplace_request("amazon-search", term="sink caddy") if kind == "marketplace" else build_trends_request("sink caddy")
        collector = capture_marketplace if kind == "marketplace" else capture_direct_trends
        folder = self.root / kind / name
        reader = self.successful_saved_reader(kind, request, folder, status=status, wrong_scope=wrong_scope)
        module = "product_radar.marketplace_browser.parse_marketplace_snapshot" if kind == "marketplace" else "product_radar.trends_controller._interpret"
        with patch(module, side_effect=SourceActionDeadline()), self.assertRaises(SourceActionDeadline):
            collector(request, name, data_root=self.root / kind, snapshot_reader=reader)
        return request, folder

    def test_post_native_marketplace_extraction_recovery_is_additive_and_installed_reconciler_is_zero_call(self):
        request, folder = self.completed_capture_then_deadline("marketplace", "finished-market")
        original = (folder / "receipt.json").read_bytes(); native = (folder / "native_result.json").read_bytes()
        failed = import_direct_marketplace(self.memory, folder)
        failed_capture = deepcopy(self.memory.snapshot()["captures"][failed["capture_id"]])
        handler = create_marketplace_handlers(self.memory, data_root=self.root / "marketplace")["amazon-search"]["reconcile"]
        context = {"route": "amazon-search", "request": request, "acquisition_id": "finished-market",
            "concept_ids": ["first"], "description_hashes": {"first": digest(self.description)}}
        with patch("product_radar.native_public_browser.native_snapshot", side_effect=AssertionError("No new native read")):
            result = handler(context)
            again = handler(context)
        self.assertEqual(result["state"], "usable"); self.assertEqual(len(result["observation_ids"]), 1)
        self.assertEqual(result, again)
        self.assertNotEqual(result["capture_ids"], [failed["capture_id"]])
        self.assertEqual(self.memory.snapshot()["captures"][failed["capture_id"]], failed_capture)
        self.assertEqual((folder / "receipt.json").read_bytes(), original)
        self.assertEqual((folder / "native_result.json").read_bytes(), native)
        self.assertEqual(replay_marketplace(folder)["state"], "failed")
        self.assertEqual(Path(result["reference"]).name, "deadline_reparse.json")

    def test_post_native_trends_extraction_recovery_uses_original_chart_export_and_zero_calls(self):
        request, folder = self.completed_capture_then_deadline("trends", "finished-trends")
        original, native, csv = [(folder / name).read_bytes() for name in ("receipt.json", "native_result.json", "multiTimeline.csv")]
        handler = create_trends_handlers(self.memory, data_root=self.root / "trends")["google-trends"]["reconcile"]
        context = {"route": "google-trends", "request": request, "acquisition_id": "finished-trends",
            "concept_ids": ["first"], "description_hashes": {"first": digest(self.description)}}
        with patch("product_radar.native_public_browser.native_snapshot", side_effect=AssertionError("No new native read")):
            result = handler(context); again = handler(context)
        self.assertEqual(result["state"], "usable")
        self.assertEqual(result["target_results"][0]["state"], "completed_relevant")
        self.assertEqual(result, again)
        self.assertEqual(len(self.memory.snapshot()["trends_evidence"]), 1)
        self.assertEqual([(folder / name).read_bytes() for name in ("receipt.json", "native_result.json", "multiTimeline.csv")],
                         [original, native, csv])
        self.assertEqual(replay_direct_trends(folder)["state"], "failed")

    def test_post_native_wrong_scope_or_changed_original_hash_cannot_donate_success(self):
        for kind, replay in (("marketplace", reconcile_marketplace), ("trends", reconcile_direct_trends)):
            with self.subTest(kind=kind):
                request, folder = self.completed_capture_then_deadline(kind, "wrong-scope", wrong_scope=True)
                self.assertEqual(replay(folder)["state"], "failed")
                _, changed = self.completed_capture_then_deadline(kind, "changed-hash")
                value = load_json(changed / "native_result.json")
                value["snapshot"]["text"] += " changed original"
                atomic_json(changed / "native_result.json", value)
                with self.assertRaises(SafetyError):
                    replay(changed)

    def test_completed_original_http_block_projects_block_not_timeout_or_no_results(self):
        for kind, replay in (("marketplace", reconcile_marketplace), ("trends", reconcile_direct_trends)):
            with self.subTest(kind=kind):
                request, folder = self.completed_capture_then_deadline(kind, "proved-block", status=429)
                first = (folder / "receipt.json").read_bytes()
                saved = replay(folder)
                self.assertEqual(saved["state"], "blocked")
                self.assertNotEqual(saved["reason"], "SOURCE_ACTION_TIMEOUT")
                self.assertEqual((folder / "receipt.json").read_bytes(), first)

    def test_precise_original_clock_rejects_pre_wrapper_dispatch_and_inverted_completion(self):
        request, folder = self.completed_capture_then_deadline("trends", "precise-clock")
        receipt = replay_direct_trends(folder)
        original_native = load_json(folder / "native_result.json")
        for invalid in ("pre-wrapper", "inverted"):
            native = deepcopy(original_native)
            if invalid == "pre-wrapper":
                native["navigation_started_at"] = (datetime.fromisoformat(receipt["started_at"]) - timedelta(seconds=1)).isoformat()
            else:
                native["completed_at"] = (datetime.fromisoformat(native["navigation_started_at"]) - timedelta(seconds=1)).isoformat()
            raw = json.dumps(native, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()
            (folder / "native_result.json").write_bytes(raw)
            # Isolate clock validation from the independently checked
            # artifact/transport consistency. No production receipt is edited.
            isolated = dict(receipt, artifacts={"native_result.json": __import__("hashlib").sha256(raw).hexdigest()},
                            transport_attempts=["synthetic_fixture"])
            with self.assertRaisesRegex(SafetyError, "SOURCE_DEADLINE_ORIGINAL_TIME_INVALID"):
                saved_completed_transport(folder, isolated, snapshot_file="scope.json", requested_url=original_native["snapshot"]["url"],
                                          snapshot_limit=2097152)

    def test_connected_commerce_timeout_recovery_does_not_pause_healthy_same_platform_question(self):
        calls = []
        def native(url, expression, folder):
            calls.append(url)
            now = datetime.now(timezone.utc).isoformat()
            if "sink+caddy" in url:
                atomic_json(folder / "native_dispatch.json", {"url": url, "navigation_started": True, "navigation_started_at": now})
                atomic_json(folder / "native_outer_deadline.json", {"state": "failed", "reason": "SOURCE_ACTION_TIMEOUT",
                    "stage": "outer_deadline", "outer_deadline": True, "requested_url": url, "snapshot": None,
                    "navigation_started": True, "navigation_started_at": now, "navigation_status": None,
                    "owned_process_exited": True, "completed_at": now, "launch_invoked": True})
                raise SourceActionDeadline()
            return {"state": "captured", "navigation_started": True, "navigation_started_at": now,
                    "completed_at": now, "navigation_status": 200, "snapshot": {}, "owned_process_exited": True}
        handlers = installed_source_handlers(self.memory, quickcommerce_options={"native_capture": native})
        questions = [{"route": "blinkit", "request": {"query": term, "location": LOCATION}, "concept_ids": ["first"]}
                     for term in ("sink caddy", "food wrap")]
        prepare_source_tasks(self.memory, questions, run_id="same-platform", as_of=NOW)
        first = execute_source_run(self.memory, "same-platform", handlers=handlers, timeout_seconds=0.5, as_of=NOW)
        second = execute_source_run(self.memory, "same-platform", handlers=handlers, timeout_seconds=0.5, as_of=NOW)
        self.assertEqual(len(calls), 2)
        self.assertIn("insufficient", first["counts"])
        self.assertIn("failed", second["counts"])
        run = next(row for row in self.memory.snapshot()["batch_runs"].values() if row.get("run_id") == "same-platform")
        self.assertEqual(run["route_pauses"], {})

    def test_wrapper_proof_save_failure_keeps_deadline_and_does_not_save_default_false(self):
        for kind in ("marketplace", "trends"):
            with self.subTest(kind=kind):
                module = "product_radar.marketplace_browser" if kind == "marketplace" else "product_radar.trends_controller"
                original_write = __import__(module, fromlist=["_"]).__dict__["_write_new" if kind == "marketplace" else "_write"]
                def failing_write(path, raw):
                    if Path(path).name == "outer_deadline.json":
                        raise OSError("Synthetic local save failure")
                    return original_write(path, raw)
                request = build_marketplace_request("amazon-search", term="sink caddy") if kind == "marketplace" else build_trends_request("sink caddy")
                collector = capture_marketplace if kind == "marketplace" else capture_direct_trends
                with patch(module + ("._write_new" if kind == "marketplace" else "._write"), side_effect=failing_write), \
                        self.assertRaises(SourceActionDeadline):
                    _bounded_call(lambda _: collector(request, "proof-save-failed", data_root=self.root / kind,
                        snapshot_reader=self.blocked_reader([])), {}, 0.02)
                saved = load_json(self.root / kind / "proof-save-failed/receipt.json")
                self.assertEqual(saved["reason"], "SOURCE_ACTION_TIMEOUT")
                self.assertIsNone(saved["dispatch_started"])
                self.assertTrue(saved["outer_deadline"])

    def test_saved_public_probe_or_unsubmitted_funded_intent_cannot_start_new_request_on_reconcile(self):
        native_calls, funded_calls = [], []
        def native(*args, **kwargs):
            native_calls.append(args)
            return {"state": "failed", "navigation_started": False, "completed_at": NOW, "reason": "Offline native fixture"}
        class Funded:
            def search(self, *args, **kwargs):
                funded_calls.append(args)
                raise SourceActionDeadline()
        handler = create_quickcommerce_handlers(self.memory, Funded(), native_capture=native)["blinkit"]
        context = {"route": "blinkit", "acquisition_id": "commerce-recovery", "request": {"query": "sink caddy", "location": LOCATION},
                   "concept_ids": ["first"], "description_hashes": {"first": digest(self.description)}}
        with self.assertRaises(SourceActionDeadline):
            handler(context)
        with self.assertRaisesRegex(SafetyError, "SOURCE_ORIGINAL_RECEIPT_PENDING"):
            handler(dict(context, reconciling=True))
        self.assertEqual(len(native_calls), 1); self.assertEqual(len(funded_calls), 1)


if __name__ == "__main__":
    unittest.main()
