"""Saved research reads do not consume website time; all fixtures are offline."""

from contextlib import contextmanager
from pathlib import Path
import signal
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from product_radar.models import SafetyError
from product_radar.research_memory import ResearchMemory
from product_radar.source_coverage import execute_source_run, prepare_source_tasks
from product_radar.source_deadline import (SourceActionDeadline, bounded_source_deadline,
    local_source_work, trace_source)
from product_radar.storage import Journal
from product_radar.supplier_route_acquisition import create_supplier_handlers
from test_supplier_route_acquisition import BODIES, NOW, QUERY, receipt


@unittest.skipUnless(hasattr(signal, "setitimer"), "Requires POSIX source deadlines")
class SourceLocalReadDeadlineTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.memory = ResearchMemory(self.root / "research", Journal(self.root / "finance"),
                                     evidence_type="synthetic")
        self.description = {"label": "Food wrap paper", "category": "Home and Kitchen",
            "function": "Wrap food", "mechanism": "manual", "audience": "adult",
            "features": ["wrapping"], "basis": "observed", "brand": "generic_or_small",
            "reason": "Synthetic fixture."}
        self.memory.put("concepts", "paper", {"status": "active", "description": self.description})
        self.handler = signal.getsignal(signal.SIGALRM)
        self.timer = signal.getitimer(signal.ITIMER_REAL)
        signal.setitimer(signal.ITIMER_REAL, 0)
        self.addCleanup(self.restore_alarm)

    def restore_alarm(self):
        signal.setitimer(signal.ITIMER_REAL, 0)
        signal.signal(signal.SIGALRM, self.handler)
        if self.timer[0]:
            signal.setitimer(signal.ITIMER_REAL, *self.timer)

    @contextmanager
    def slow_read(self, seconds=0.08):
        original = self.memory._state
        def read(*args, **kwargs):
            time.sleep(seconds)
            return original(*args, **kwargs)
        with patch.object(self.memory, "_state", side_effect=read):
            yield

    def test_actual_snapshot_outlasts_website_allowance_without_consuming_it(self):
        before = self.memory.snapshot_with_provenance()
        with self.slow_read(), trace_source({}) as trace, bounded_source_deadline(0.05):
            trace.phase = "handler"
            result = self.memory.snapshot()
            remaining = signal.getitimer(signal.ITIMER_REAL)[0]
            self.assertEqual(trace.phase, "handler")
            self.assertGreater(remaining, 0.02)
            self.assertLessEqual(remaining, 0.05)
        self.assertEqual(result, before["state"])
        self.assertGreaterEqual(trace.durations["local_read"], 0.075)
        self.assertIsNone(trace.failure_phase)
        self.assertEqual(self.memory.snapshot_with_provenance(), before)
        self.assertEqual(signal.getitimer(signal.ITIMER_REAL), (0, 0))

    def test_repeated_local_reads_do_not_reset_the_remaining_website_time(self):
        with self.slow_read(), trace_source({}) as trace:
            with bounded_source_deadline(0.15):
                time.sleep(0.025)
                self.memory.snapshot()
                first = signal.getitimer(signal.ITIMER_REAL)[0]
                time.sleep(0.025)
                self.memory.snapshot()
                second = signal.getitimer(signal.ITIMER_REAL)[0]
                self.assertLess(second, first - 0.015)
                self.assertGreater(second, 0.03)
                with self.assertRaises(SourceActionDeadline):
                    time.sleep(0.2)
        self.assertGreaterEqual(trace.durations["local_read"], 0.15)

    def test_snapshot_with_provenance_uses_the_same_local_read_protection(self):
        with self.slow_read(), trace_source({}) as trace, bounded_source_deadline(0.05):
            result = self.memory.snapshot_with_provenance()
        self.assertEqual(result["event_count"], 1)
        self.assertIn("paper", result["state"]["concepts"])
        self.assertGreaterEqual(trace.durations["local_read"], 0.075)

    def test_failed_read_keeps_its_exact_error_and_restores_phase_and_timer(self):
        with trace_source({}) as trace, bounded_source_deadline(0.1):
            trace.phase = "handler"
            with patch.object(self.memory, "_state", side_effect=SafetyError("RESEARCH_EVENTS_INVALID")):
                with self.assertRaisesRegex(SafetyError, "^RESEARCH_EVENTS_INVALID$"):
                    self.memory.snapshot()
            self.assertEqual(trace.failure_phase, "local_read")
            self.assertEqual(trace.phase, "handler")
            self.assertGreater(signal.getitimer(signal.ITIMER_REAL)[0], 0)

    def test_transient_read_retries_are_finite_and_never_return_an_old_prefix(self):
        with trace_source({}) as trace, bounded_source_deadline(0.1):
            with patch.object(self.memory, "_state", side_effect=SafetyError("SAVED_FILE_UNAVAILABLE")) as read:
                with self.assertRaisesRegex(SafetyError, "^SAVED_FILE_UNAVAILABLE$"):
                    self.memory.snapshot()
            self.assertEqual(read.call_count, 3)
            self.assertEqual(trace.failure_phase, "local_read")

    def test_preexisting_alarm_is_not_suspended_or_resurrected(self):
        class ExistingAlarm(BaseException):
            pass
        def alarm(_signum, _frame):
            raise ExistingAlarm()
        signal.signal(signal.SIGALRM, alarm)
        signal.setitimer(signal.ITIMER_REAL, 0.03)
        with self.slow_read(), trace_source({}) as trace:
            with self.assertRaises(ExistingAlarm), bounded_source_deadline(0.3):
                self.memory.snapshot()
        self.assertIs(signal.getsignal(signal.SIGALRM), alarm)
        self.assertEqual(signal.getitimer(signal.ITIMER_REAL), (0, 0))
        self.assertEqual(trace.failure_phase, "local_read")

    def test_read_without_owned_source_deadline_does_not_pause_another_alarm(self):
        class ExistingAlarm(BaseException):
            pass
        def alarm(_signum, _frame):
            raise ExistingAlarm()
        signal.signal(signal.SIGALRM, alarm)
        signal.setitimer(signal.ITIMER_REAL, 0.03)
        with self.slow_read(), self.assertRaises(ExistingAlarm):
            self.memory.snapshot()
        self.assertEqual(signal.getitimer(signal.ITIMER_REAL), (0, 0))

    def test_nested_local_apply_and_read_restore_the_original_remaining_allowance(self):
        @local_source_work("local_apply")
        def apply():
            self.memory.snapshot()
            time.sleep(0.03)
        with self.slow_read(), trace_source({}) as trace, bounded_source_deadline(0.05):
            apply()
            self.assertGreater(signal.getitimer(signal.ITIMER_REAL)[0], 0.025)
            self.assertEqual(trace.phase, "preparation")
        self.assertGreaterEqual(trace.durations["local_read"], 0.075)
        self.assertGreater(trace.durations["local_apply"], trace.durations["local_read"])

    def source_run(self, *, slow_transport):
        questions = [{"route": route, "request": {"query": QUERY, "country": "IN"},
                      "concept_ids": ["paper"]} for route in ("google-maps", "meta-ads")]
        prepare_source_tasks(self.memory, questions, run_id="local-read-run", as_of=NOW)
        calls, browser_calls = [], []
        def http(item):
            calls.append(item)
            if slow_transport:
                time.sleep(0.2)
                self.fail("The real website deadline must still stop a slow transport")
            return receipt(item, body=BODIES["google-maps"])
        def browser(item):
            browser_calls.append(item)
            self.fail("The cancelled website request must not launch a second request")
        handlers = create_supplier_handlers(self.memory, http_reader=http, browser_reader=browser)
        def healthy(context):
            context["record_dispatch"]()
            return {"schema_version": 1, "route": context["route"], "acquisition_id": context["acquisition_id"],
                "observed_at": NOW, "state": "usable", "dispatch_started": True,
                "reference": "synthetic-healthy/receipt.json", "capture_ids": [], "observation_ids": [],
                "target_results": [], "reason": "Synthetic healthy sibling", "retryable": False}
        handlers["meta-ads"] = healthy
        with self.slow_read():
            result = execute_source_run(self.memory, "local-read-run", handlers=handlers,
                                        as_of=NOW, timeout_seconds=0.05)
        self.assertEqual(len(calls), 1)
        self.assertEqual(browser_calls, [])
        return result

    def test_installed_supplier_handler_finishes_despite_slow_saved_file_reads(self):
        result = self.source_run(slow_transport=False)
        self.assertEqual(result["counts"], {"acquired": 2})
        self.assertTrue(all(row.get("reason") != "SOURCE_ACTION_TIMEOUT" for row in result["results"]))

    def test_real_transport_still_times_out_and_the_healthy_next_source_runs(self):
        result = self.source_run(slow_transport=True)
        self.assertEqual(result["counts"], {"failed": 1, "acquired": 1})
        failed = next(row for row in result["results"] if row["state"] == "failed")
        self.assertEqual(failed["reason"], "SOURCE_ACTION_TIMEOUT")
        # This injected reader has no transport-phase wrapper of its own.
        # The controller must still identify handler time, not local reading.
        self.assertEqual(failed["failure_phase"], "handler")


if __name__ == "__main__":
    unittest.main()
