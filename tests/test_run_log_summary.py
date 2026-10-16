"""Owner-meter run-log totals stay identical while reading only new log bytes.

Synthetic run logs in a temporary folder only; no research store, website,
provider or model.
"""

import json
import os
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import patch

from product_radar import run_log_summary as logs
from product_radar import runtime_storage
from product_radar.models import SafetyError
from product_radar.operations import operation_log_summary
from product_radar.storage import atomic_json, load_json

START = "2026-10-13T18:00:00+00:00"
NOW = "2026-10-16T12:00:00+00:00"


def row(at, event="source_outcome", run="run-a", **values):
    return {"schema_version": 1, "run_id": run, "at": at, "event": event, **values}


def original_summary(folder, start):
    """The meter's reading before the speed copy, kept here as the reference."""
    from collections import Counter
    from datetime import datetime, timezone
    from product_radar.models import timestamp
    from product_radar.operations import _WAIT_STATES, _access_outcome
    start = timestamp(start)
    totals, failures, waits, blocks, runs = Counter(), Counter(), Counter(), Counter(), set()
    unreadable, first, last = 0, None, None
    for path in sorted(folder.glob("run-*.jsonl*")):
        if datetime.fromtimestamp(path.stat().st_mtime, timezone.utc) < start:
            continue
        with path.open("rb") as stream:
            lines = stream.readlines()
        for line in lines:
            try:
                value = json.loads(line); at = timestamp(value["at"])
                if not isinstance(value, dict) or not isinstance(value.get("event"), str):
                    raise ValueError
            except (ValueError, KeyError, TypeError, SafetyError):
                unreadable += 1; continue
            if at < start:
                continue
            first = at if first is None or at < first else first
            last = at if last is None or at > last else last
            runs.add(value["run_id"]); totals["events"] += 1
            event = value["event"]
            reason = value.get("reason") if isinstance(value.get("reason"), str) else event
            if value.get("useful_result") is True:
                totals["useful"] += 1
            reused = (event == "source_outcome" and value.get("state") == "failed" and value.get("outcome_reused") is True
                      and value.get("actual_attempt") is False and not value.get("error_reported"))
            if value.get("error_type") or value.get("state") == "failed":
                if reused:
                    totals["reused"] += 1
                else:
                    totals["failures"] += 1; failures[reason[:120]] += 1
            access = _access_outcome(event, value)
            if access == "block":
                totals["blocks"] += 1; blocks[value.get("route", "unknown")] += 1
            if value.get("state") in _WAIT_STATES or access == "held":
                totals["waits"] += 1; waits[reason[:120]] += 1
    return {"events": totals["events"], "useful_results": totals["useful"], "failures": totals["failures"],
            "waits": totals["waits"], "access_blocks": totals["blocks"], "runs": len(runs),
            "top_failure_reasons": dict(failures.most_common(8)), "top_wait_reasons": dict(waits.most_common(8)),
            "top_access_block_routes": dict(blocks.most_common(8)), "unreadable_lines": unreadable,
            "first_event_at": first.isoformat(timespec="seconds") if first else None,
            "last_event_at": last.isoformat(timespec="seconds") if last else None}


class _Logs:
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="radar-run-log-summary-")
        self.addCleanup(temporary.cleanup)
        self.addCleanup(runtime_storage._configured.cache_clear)
        self.root = Path(temporary.name)
        self.folder = self.root / "data/operations/research-runs"
        self.folder.mkdir(parents=True)

    def write(self, name, rows, *, mtime=None, append=False):
        path = self.folder / name
        with path.open("a" if append else "w") as stream:
            stream.write("".join((value if isinstance(value, str) else json.dumps(value)) + "\n" for value in rows))
        if mtime is not None:
            os.utime(path, (mtime, mtime))
        return path

    def seed(self):
        # Ties across files: the first-seen order decides the top-8 lists.
        reasons = ["R%02d" % number for number in range(12)]
        self.write("run-b.jsonl.2026-10-14_09", [row("2026-10-14T09:%02d:00+00:00" % minute, state="failed",
            reason=reasons[minute % 12], run="run-b") for minute in range(24)] + ["{broken"])
        self.write("run-a.jsonl", [row("2026-10-13T17:00:00+00:00", event="invocation_started"),
            row("2026-10-13T18:00:00+05:30", event="invocation_started"),
            row("2026-10-13T18:30:00+00:00", state="waiting", reason="MODEL_WAIT"),
            row("2026-10-13T18:31:00+00:00", state="blocked", actual_attempt=True, route="google-maps"),
            row("2026-10-13T18:32:00+00:00", state="blocked", actual_attempt=False, reason="SOURCE_ACCESS_WAIT"),
            row("2026-10-13T18:33:00+00:00", state="failed", outcome_reused=True, actual_attempt=False),
            row("2026-10-13T18:34:00+00:00", event="saved_result", useful_result=True),
            row("2026-10-13T18:35:00+00:00", event="invocation_failed", state="failed")])
        self.write("run-c.jsonl", [row("2026-10-15T01:00:00+00:00", state="failed", reason="R11", run="run-c"),
            row("2026-10-15T01:00:00+00:00", state="failed", reason="R10", run="run-c")])
        self.write("run-old.jsonl", [row("2026-10-14T00:00:00+00:00", run="run-old")], mtime=0)

    def reference(self):
        return original_summary(self.folder, START)


class RunLogSummaryTests(_Logs, unittest.TestCase):

    def check_same(self, result, expected):
        for name, value in expected.items():
            self.assertEqual(result[name], value, name)
            if isinstance(value, dict):
                self.assertEqual(list(result[name]), list(value), name + " order")

    def test_cold_and_warm_readings_match_the_original_reading_exactly(self):
        self.seed()
        expected = self.reference()
        cold = operation_log_summary(self.root, since=START)
        self.check_same(cold, expected)
        self.assertEqual(cold["state"], "partial")  # one unreadable line, as before
        self.assertTrue((self.folder / logs.CACHE_NAME).exists())
        with patch.object(logs, "summarise_lines", side_effect=AssertionError("No log reread")):
            warm = operation_log_summary(self.root, since=START)
        self.assertEqual(warm, cold)
        self.assertEqual(warm["pruned_logs"], {"files": 0, "state": "none"})

    def test_only_a_new_or_growing_log_is_read_again(self):
        self.seed()
        operation_log_summary(self.root, since=START)
        self.write("run-c.jsonl", [row("2026-10-15T02:00:00+00:00", state="waiting", reason="NEW", run="run-c")],
                   append=True)
        read = []
        original = logs.summarise_lines
        def spy(lines, start, budget=None):
            read.append(Path(lines.name).name)
            return original(lines, start, budget)
        with patch.object(logs, "summarise_lines", side_effect=spy):
            result = operation_log_summary(self.root, since=START)
        self.assertEqual(read, ["run-c.jsonl"])
        self.check_same(result, self.reference())

    def test_damaged_or_other_start_speed_copy_is_ignored_with_same_numbers(self):
        self.seed()
        cold = operation_log_summary(self.root, since=START)
        wrapper = load_json(self.folder / logs.CACHE_NAME, 1 << 20)
        wrapper["value"]["files"]["run-a.jsonl"]["summary"]["events"] = 999
        atomic_json(self.folder / logs.CACHE_NAME, wrapper)  # hash no longer matches
        self.assertEqual(operation_log_summary(self.root, since=START), cold)
        later = operation_log_summary(self.root, since="2026-10-14T00:00:00+00:00")
        self.assertEqual(later["events"], 26)  # run-b (24 records) and run-c (2), not the cached start
        self.assertEqual(operation_log_summary(self.root, since=START), cold)

    def test_unusable_cached_entries_and_unreadable_cache_fall_back(self):
        self.seed()
        cold = operation_log_summary(self.root, since=START)
        wrapper = load_json(self.folder / logs.CACHE_NAME, 1 << 20)
        value = wrapper["value"]; value["files"]["run-a.jsonl"]["identity"] = "not a list"
        atomic_json(self.folder / logs.CACHE_NAME, {"value": value, "hash": logs.digest(value)})
        self.assertEqual(operation_log_summary(self.root, since=START), cold)
        value = dict(value, contract="other")
        atomic_json(self.folder / logs.CACHE_NAME, {"value": value, "hash": logs.digest(value)})
        self.assertEqual(operation_log_summary(self.root, since=START), cold)
        with patch.object(logs, "atomic_json", side_effect=OSError("Synthetic read-only folder")):
            self.assertEqual(operation_log_summary(self.root, since=START), cold)

    def test_byte_bound_reading_is_partial_then_completed_by_later_readings(self):
        self.seed()
        expected = self.reference()
        # The bound covers the largest single log but not all of them.
        bound = max(path.stat().st_size for path in self.folder.glob("run-*")) + 10
        first = operation_log_summary(self.root, since=START, max_bytes=bound)
        self.assertTrue(first["truncated"])
        self.assertEqual(first["state"], "partial")
        self.assertLess(first["events"], expected["events"])
        for _ in range(5):
            result = operation_log_summary(self.root, since=START, max_bytes=bound)
            if not result["truncated"]:
                break
        self.assertFalse(result["truncated"])
        self.check_same(result, expected)

    def test_a_log_written_while_read_is_counted_but_not_cached(self):
        self.seed()
        original = logs.summarise_lines
        def grow(lines, start, budget=None):
            value = original(lines, start, budget)
            if Path(lines.name).name == "run-c.jsonl":
                self.write("run-c.jsonl", [row("2026-10-15T03:00:00+00:00", run="run-c")], append=True)
            return value
        with patch.object(logs, "summarise_lines", side_effect=grow):
            operation_log_summary(self.root, since=START)
        cached = load_json(self.folder / logs.CACHE_NAME, 1 << 20)["value"]["files"]
        self.assertNotIn("run-c.jsonl", cached)
        self.check_same(operation_log_summary(self.root, since=START), self.reference())

    def test_empty_or_missing_folder(self):
        self.assertEqual(operation_log_summary(self.root, since=START)["events"], 0)
        self.folder.rmdir()
        missing = operation_log_summary(self.root, since=START)
        self.assertEqual((missing["events"], missing["state"]), (0, "known"))
        self.assertFalse(self.folder.exists())

    def test_merge_and_validation_edges(self):
        self.assertEqual(logs.merge([]), logs.empty_summary())
        with self.assertRaises(SafetyError):
            logs._validate_summary(dict(logs.empty_summary(), failure_reasons=[["x", 0]]))
        with self.assertRaises(SafetyError):
            logs._validate_summary({"events": 1})
        self.assertIsNotNone(logs._validate_summary(dict(logs.empty_summary(), first=START)))


class RunLogRetirementTests(_Logs, unittest.TestCase):
    def age(self, *names, days=120):
        moment = time.time() - days * 86400
        for name in names:
            os.utime(self.folder / name, (moment, moment))

    def test_retired_logs_keep_identical_meter_totals_and_are_removed(self):
        self.seed()
        self.age("run-b.jsonl.2026-10-14_09", "run-old.jsonl")
        # The meter start is before the aged logs (as for a long-running meter).
        since = "2026-01-01T00:00:00+00:00"
        before = operation_log_summary(self.root, since=since)
        result = logs.retire_old_run_logs(self.root, since=since, keep_days=90, now=NOW)
        self.assertEqual(result["state"], "removed")
        self.assertEqual(result["removed_files"], 2)
        self.assertFalse((self.folder / "run-b.jsonl.2026-10-14_09").exists())
        after = operation_log_summary(self.root, since=since)
        self.assertEqual(after["pruned_logs"], {"files": 2, "state": "counted"})
        for name in ("events", "failures", "waits", "access_blocks", "runs", "unreadable_lines",
                     "first_event_at", "last_event_at", "useful_results", "top_wait_reasons", "top_access_block_routes"):
            self.assertEqual(after[name], before[name], name)
        self.assertEqual(sorted(after["top_failure_reasons"].items()), sorted(before["top_failure_reasons"].items()))
        self.assertEqual(logs.retire_old_run_logs(self.root, since=since, keep_days=90, now=NOW)["state"],
                         "nothing_to_remove")

    def test_interrupted_removal_never_counts_a_log_twice_and_finishes_later(self):
        self.seed(); self.age("run-b.jsonl.2026-10-14_09")
        since = "2026-01-01T00:00:00+00:00"
        before = operation_log_summary(self.root, since=since)
        with patch.object(logs.os, "unlink", side_effect=OSError("Synthetic interruption")):
            with self.assertRaises(OSError):
                logs.retire_old_run_logs(self.root, since=since, keep_days=90, now=NOW)
        self.assertTrue((self.folder / "run-b.jsonl.2026-10-14_09").exists())
        middle = operation_log_summary(self.root, since=since)
        self.assertEqual(middle["events"], before["events"])
        # run-old (unchanged since 1970, before the start) goes too, uncounted.
        self.assertEqual(logs.retire_old_run_logs(self.root, since=since, keep_days=90, now=NOW)["removed_files"], 2)
        self.assertFalse((self.folder / "run-b.jsonl.2026-10-14_09").exists())
        self.assertEqual(operation_log_summary(self.root, since=since)["events"], before["events"])

    def test_damaged_retired_summary_is_reported_never_replaced(self):
        self.seed(); self.age("run-b.jsonl.2026-10-14_09")
        since = "2026-01-01T00:00:00+00:00"
        logs.retire_old_run_logs(self.root, since=since, keep_days=90, now=NOW)
        (self.folder / logs.RETIRED_NAME).write_text('{"value": {}, "hash": "0"}')
        reading = operation_log_summary(self.root, since=since)
        self.assertEqual((reading["state"], reading["pruned_logs"]["state"]), ("partial", "summary_unreadable"))
        with self.assertRaises(SafetyError):
            logs.retire_old_run_logs(self.root, since=since, keep_days=90, now=NOW)
        self.assertEqual((self.folder / logs.RETIRED_NAME).read_text(), '{"value": {}, "hash": "0"}')

    def test_other_meter_start_keeps_raw_logs_or_marks_the_reading(self):
        self.seed(); self.age("run-b.jsonl.2026-10-14_09")
        since = "2026-01-01T00:00:00+00:00"
        logs.retire_old_run_logs(self.root, since=since, keep_days=90, now=NOW)
        self.age("run-c.jsonl")
        # An earlier-than-retired start cannot be re-counted: raw logs stay.
        kept = logs.retire_old_run_logs(self.root, since="2026-02-01T00:00:00+00:00", keep_days=90, now=NOW)
        self.assertEqual((kept["state"], kept["reason"]), ("kept", "RUN_LOG_RETIRED_SUMMARY_START_CHANGED"))
        self.assertTrue((self.folder / "run-c.jsonl").exists())
        # A start after every retired log simply excludes them.
        later = operation_log_summary(self.root, since=NOW)
        self.assertEqual((later["state"], later["pruned_logs"]["state"]), ("known", "counted"))
        # An earlier start cannot be answered from the kept totals.
        earlier = operation_log_summary(self.root, since="2025-01-01T00:00:00+00:00")
        self.assertEqual((earlier["state"], earlier["pruned_logs"]["state"]), ("partial", "not_counted"))

    def test_a_later_meter_start_reseals_the_retired_summary_and_retiring_goes_on(self):
        self.seed(); self.age("run-b.jsonl.2026-10-14_09", days=200)
        logs.retire_old_run_logs(self.root, since="2026-01-01T00:00:00+00:00", keep_days=90, now=NOW)
        fresh_start = "2026-10-01T00:00:00+00:00"  # after every retired log
        nothing = logs.retire_old_run_logs(self.root, since=fresh_start, keep_days=90, now=NOW)
        self.assertEqual((nothing["state"], nothing["reason"]), ("nothing_to_remove", "RUN_LOG_RETIRED_SUMMARY_RESEALED"))
        retired = logs.load_retired(self.folder)
        self.assertEqual((retired["since"], retired["files"], retired["summary"]), (fresh_start, [], logs.empty_summary()))
        reading = operation_log_summary(self.root, since=fresh_start)
        self.assertEqual((reading["pruned_logs"], reading["state"]), ({"files": 0, "state": "counted"}, "known"))
        self.assertEqual(reading["events"], original_summary(self.folder, fresh_start)["events"])
        # Later retirements count for the new start.
        self.age("run-c.jsonl", days=100)
        later = logs.retire_old_run_logs(self.root, since=fresh_start, keep_days=90, now=NOW)
        self.assertEqual((later["state"], later["reason"]), ("removed", None))
        # A start change with old logs to retire re-seals and retires at once.
        self.age("run-a.jsonl", days=100)
        moved = logs.retire_old_run_logs(self.root, since="2026-10-05T00:00:00+00:00", keep_days=90, now=NOW)
        self.assertEqual((moved["state"], moved["reason"]), ("removed", "RUN_LOG_RETIRED_SUMMARY_RESEALED"))

    def test_retirement_guards(self):
        with self.assertRaises(SafetyError):
            logs.retire_old_run_logs(self.root, since=START, keep_days=3, now=NOW)
        self.assertEqual(logs.retire_old_run_logs(self.root, since=START, keep_days=90, now=NOW)["state"],
                         "nothing_to_remove")
        self.folder.rmdir()
        self.assertEqual(logs.retire_old_run_logs(self.root, since=START, keep_days=90, now=NOW)["state"],
                         "nothing_to_remove")

    def test_a_log_changed_while_retiring_is_refused(self):
        self.seed(); self.age("run-b.jsonl.2026-10-14_09")
        original = logs.summarise_lines
        def touch(lines, start, budget=None):
            value = original(lines, start, budget)
            self.write("run-b.jsonl.2026-10-14_09", ["{late"], append=True)
            return value
        with patch.object(logs, "summarise_lines", side_effect=touch):
            with self.assertRaisesRegex(SafetyError, "RUN_LOG_CHANGED_WHILE_RETIRING"):
                logs.retire_old_run_logs(self.root, since="2026-01-01T00:00:00+00:00", keep_days=90, now=NOW)
        self.assertFalse((self.folder / logs.RETIRED_NAME).exists())

    def test_reading_restarts_when_logs_are_retired_meanwhile(self):
        self.seed(); self.age("run-b.jsonl.2026-10-14_09")
        since = "2026-01-01T00:00:00+00:00"
        before = operation_log_summary(self.root, since=since)
        (self.folder / logs.CACHE_NAME).unlink()
        original, calls = logs.summarise_lines, []
        def retire_once(lines, start, budget=None):
            value = original(lines, start, budget)
            if not calls:
                calls.append(True)
                logs.retire_old_run_logs(self.root, since=since, keep_days=90, now=NOW)
            return value
        with patch.object(logs, "summarise_lines", side_effect=retire_once):
            during = operation_log_summary(self.root, since=since)
        self.assertEqual(during["events"], before["events"])
        self.assertEqual(during["pruned_logs"]["files"], 1)
        with patch.object(logs, "_summarise_folder", side_effect=logs._RetiredChanged()):
            with self.assertRaisesRegex(SafetyError, "RUN_LOG_SUMMARY_CHANGING"):
                logs.summarise_folder(self.root, since=since)


if __name__ == "__main__":
    unittest.main()
