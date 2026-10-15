"""Progress watch: heartbeats, owner warnings and the worker watchdog.

Local files and cost-free local child processes only; no provider, no real
research store and no Windows task.
"""

from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import tempfile
import textwrap
from types import SimpleNamespace
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from product_radar import desktop_control as controls
from product_radar import operations
from product_radar.models import SafetyError, canonical, timestamp, utc_now
from product_radar.operations import (code_version, operation_run, operation_writer, plain_reason,
    progress_watch_settings, read_operation_status, record_event, record_heartbeat, run_outcome)
from product_radar.programme import programme_service_state, set_programme_service_state
from product_radar.storage import atomic_json, load_json

WATCH = {"attention_no_activity_seconds": 1800, "attention_no_result_seconds": 1800,
         "watchdog_no_activity_seconds": 2700, "watchdog_poll_seconds": 60,
         "watchdog_interrupt_grace_seconds": 300, "watchdog_terminate_grace_seconds": 60,
         "scheduled_run_overdue_seconds": 1200, "repeated_failure_runs": 3}


def _ticks(pid=None):
    return Path(f"/proc/{pid or os.getpid()}/stat").read_text().rsplit(")", 1)[1].split()[19]


class Clock:
    """Wall-clock instants for status reads; each call can move forward."""
    def __init__(self, start, step=0):
        self.at, self.step = start, step

    def __call__(self):
        value = self.at
        self.at += timedelta(seconds=self.step)
        return value


class FakeChild:
    """A child process stand-in: times out a set number of polls, then exits."""
    def __init__(self, pid=4242, polls=10**6, exit_on=None, code=0):
        self.pid, self.polls, self.exit_on, self.code = pid, polls, exit_on or {}, code
        self.returncode, self.timeouts = None, []

    def wait(self, timeout=None):
        if self.returncode is not None:
            return self.returncode
        if timeout is None:
            self.returncode = self.code
            return self.code
        self.timeouts.append(timeout)
        if self.polls <= 0:
            self.returncode = self.code
            return self.code
        self.polls -= 1
        raise subprocess.TimeoutExpired("child", timeout)

    def poll(self):
        return self.returncode


class Base(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="radar-watch-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.memory = SimpleNamespace(root=self.root / "data/research/captured")
        self.desktop = self.root / "data/operations/desktop"
        self.runs = self.root / "data/operations/research-runs"

    def write_actions(self, *rows):
        self.desktop.mkdir(parents=True, exist_ok=True)
        by_day = {}
        for row in rows:
            by_day.setdefault(row["at"][:10], []).append(row)
        for day, items in by_day.items():
            with (self.desktop / f"{day}.jsonl").open("ab") as stream:
                for row in items:
                    stream.write(canonical(row) + b"\n")

    def actions(self):
        return controls.desktop_actions(self.root)

    def status_file(self, **fields):
        row = {"schema_version": 1, "run_id": "run-" + "a" * 32, "pid": os.getpid(), "process_start_ticks": _ticks(),
               "code_version": "x", "observed_at": "2026-10-15T10:00:00+00:00", "started_at": "2026-10-15T09:00:00+00:00",
               "writer_active": True, "step": "preparation", "phase": "readiness",
               "phase_started_at": "2026-10-15T10:00:00+00:00", "phase_deadline_seconds": 1800,
               "next_action": "continue_to_next_ready_step", "last_reason": None,
               "message": "Research is working through its saved tasks.", "last_useful_result_at": None,
               "counts": {}, "recording_failed": False, "last_event": {}, "log_file": "x"}
        row.update(fields)
        self.runs.mkdir(parents=True, exist_ok=True)
        atomic_json(self.runs / "current.json", row)
        return row


class SettingsAndCodeVersionTests(Base):
    def test_checked_in_settings_are_valid_and_match_the_documented_thresholds(self):
        self.assertEqual(progress_watch_settings(), WATCH)

    def test_invalid_settings_are_refused(self):
        good = load_json(operations.PROGRESS_WATCH_SETTINGS, 16384)
        broken = [dict(good, schema_version=2), dict(good, kind="other"), dict(good, extra=1),
                  {k: v for k, v in good.items() if k != "watchdog_poll_seconds"},
                  dict(good, watchdog_poll_seconds=0), dict(good, watchdog_poll_seconds=True),
                  dict(good, attention_no_activity_seconds=2700), dict(good, watchdog_poll_seconds=2700),
                  dict(good, repeated_failure_runs=1), []]
        for value in broken:
            with self.subTest(value=value):
                path = self.root / "settings.json"
                path.write_text(json.dumps(value))
                with self.assertRaisesRegex(SafetyError, "PROGRESS_WATCH_SETTINGS_INVALID"):
                    progress_watch_settings(path)

    def test_code_version_covers_every_package_file_and_is_cached(self):
        package = self.root / "repo/src/product_radar"
        (package / "sub").mkdir(parents=True)
        (self.root / "repo/scripts").mkdir()
        (package / "a.py").write_text("A = 1\n")
        (package / "sub/b.py").write_text("B = 1\n")
        (self.root / "repo/scripts/run_programme.py").write_text("print(1)\n")
        compute = code_version.__wrapped__
        with patch.object(operations, "PACKAGE", package):
            first, complete = compute()
            self.assertTrue(complete)
            (package / "sub/b.py").write_text("B = 2\n")  # any file, not just a fixed list
            second, _ = compute()
            (self.root / "repo/scripts/run_programme.py").write_text("print(2)\n")
            third, _ = compute()
            (package / "sub/b.py").chmod(0)
            try:
                if os.access(package / "sub/b.py", os.R_OK):
                    self.skipTest("this user can read unreadable files")
                fourth, incomplete = compute()
            finally:
                (package / "sub/b.py").chmod(0o644)
        self.assertEqual(len({first, second, third, fourth}), 4)
        self.assertFalse(incomplete)
        self.assertEqual(code_version(), code_version())
        self.assertRegex(code_version()[0], r"\A[0-9a-f]{64}\Z")

    def test_incomplete_code_version_is_reported_as_a_recording_problem(self):
        with patch.object(operations, "code_version", return_value=("0" * 64, False)), \
                patch("sys.stderr") as notice, operation_run(self.root) as recorder:
            pass
        self.assertTrue(recorder.recording_failed)
        notice.write.assert_called()

    def test_a_broken_settings_file_falls_back_to_the_defaults(self):
        self.assertEqual(operations.PROGRESS_WATCH_DEFAULTS, WATCH)
        self.assertEqual(operations.progress_watch_or_defaults(), (WATCH, True))
        with patch.object(operations, "PROGRESS_WATCH_SETTINGS", self.root / "missing.json"):
            self.assertEqual(operations.progress_watch_or_defaults(), (WATCH, False))
            with operation_run(self.root), operation_writer():
                record_event("active")
                self.assertEqual(read_operation_status(self.root)["state"], "running")  # status still readable

    def test_plain_reason_words(self):
        self.assertEqual(plain_reason("WORK_TURN_SOURCE_LIMIT"), "it used this round's allowance of website checks (normal)")
        self.assertEqual(plain_reason("SOMETHING_NEW"), "code SOMETHING_NEW")
        self.assertIsNone(plain_reason(None))
        self.assertIsNone(plain_reason(""))


class HeartbeatAndAttentionTests(Base):
    def test_heartbeat_is_throttled_and_changes_only_the_activity_time(self):
        record_heartbeat("readiness_progress", checkpoint="ignored")  # no run: nothing happens
        self.assertFalse(self.runs.exists())
        clock = [100.0]
        with patch.object(operations.time, "monotonic", side_effect=lambda: clock[0]), \
                operation_run(self.root) as recorder, operation_writer():
            record_event("phase_started", stage="preparation", phase="readiness", deadline_seconds=1800)
            before = read_operation_status(self.root)
            for moment in (101, 120, 131, 150, 162):
                clock[0] = moment
                record_heartbeat("readiness_progress", checkpoint="checking_review_scopes", stage="fit",
                                 phase="other", state="failed", useful_result=True, reason="NOT_KEPT",
                                 counts={"review_scopes_checked": 1, "review_scopes_total": 3})
            after = read_operation_status(self.root)
        beats = [json.loads(line) for line in (recorder.folder / (recorder.run_id + ".jsonl")).read_text().splitlines()
                 if json.loads(line)["event"] == "readiness_progress"]
        self.assertEqual(len(beats), 3)  # 101, 131 and 162: at most one per 30 seconds
        self.assertTrue(all(set(beat) & {"stage", "phase", "state", "useful_result", "reason"} == set() for beat in beats))
        self.assertEqual(beats[0]["counts"], {"review_scopes_checked": 1, "review_scopes_total": 3})
        for name in ("phase", "phase_started_at", "phase_deadline_seconds", "step", "last_useful_result_at", "last_reason"):
            self.assertEqual(after[name], before[name])
        self.assertEqual(after["counts"]["completed_results"], 0)
        self.assertEqual(after["counts"]["failures"], 0)

    def test_normal_twelve_minute_readiness_is_not_flagged(self):
        start = timestamp(utc_now())
        clock = [start.isoformat()]
        with patch.object(operations, "utc_now", side_effect=lambda: clock[0]), \
                operation_run(self.root), operation_writer():
            record_event("phase_started", stage="preparation", phase="readiness", deadline_seconds=1800)
            for seconds in range(30, 721, 30):
                clock[0] = (start + timedelta(seconds=seconds)).isoformat()
                record_heartbeat("readiness_progress", checkpoint="checking_review_scopes")
            current = read_operation_status(self.root, as_of=(start + timedelta(seconds=725)).isoformat())
            self.assertEqual(current["state"], "running")
            self.assertIsNone(current["attention"])
            self.assertIsNone(current["attention_kind"])

    def test_no_activity_warning_names_the_restart(self):
        start = timestamp(utc_now())
        with patch.object(operations, "utc_now", return_value=start.isoformat()), \
                operation_run(self.root), operation_writer():
            record_event("phase_started", phase="model_call", deadline_seconds=180)
            quiet = read_operation_status(self.root, as_of=(start + timedelta(seconds=1799)).isoformat())
            self.assertEqual(quiet["attention_kind"], "phase_over_time")
            current = read_operation_status(self.root, as_of=(start + timedelta(seconds=1860)).isoformat())
        self.assertEqual(current["attention_kind"], "no_activity")
        self.assertEqual(current["attention"], "Research has not recorded any progress for 31 minutes. If nothing "
                         "changes by 45 minutes, it is stopped and the next scheduled run starts it again.")

    def test_readiness_past_its_local_limit_is_flagged_without_cancelling_it(self):
        start = timestamp(utc_now())
        clock = [start.isoformat()]
        with patch.object(operations, "utc_now", side_effect=lambda: clock[0]), \
                operation_run(self.root), operation_writer():
            record_event("phase_started", stage="preparation", phase="readiness",
                         deadline_seconds=operations.LOCAL_PHASE_DEADLINES["readiness"])
            clock[0] = (start + timedelta(seconds=1790)).isoformat()
            record_heartbeat("readiness_progress", checkpoint="checking_photos")
            current = read_operation_status(self.root, as_of=(start + timedelta(seconds=1810)).isoformat())
        self.assertEqual(current["attention_kind"], "phase_over_time")
        self.assertEqual(current["phase"], "readiness")

    def test_a_finished_run_has_no_attention(self):
        with operation_run(self.root), operation_writer():
            record_event("active")
        current = read_operation_status(self.root, as_of=(timestamp(utc_now()) + timedelta(hours=3)).isoformat())
        self.assertEqual(current["state"], "saved_previous_run")
        self.assertIsNone(current["attention"])


class RunOutcomeTests(Base):
    def log(self, name, rows, *, mtime=None):
        self.runs.mkdir(parents=True, exist_ok=True)
        path = self.runs / name
        path.write_text("".join(json.dumps(row) + "\n" for row in rows))
        if mtime is not None:
            os.utime(path, (mtime, mtime))
        return path

    def test_the_failure_of_this_process_is_read_from_its_own_log(self):
        now = datetime.now(timezone.utc)
        since = (now - timedelta(minutes=1)).isoformat()
        self.assertIsNone(run_outcome(self.root, 77, since=since))  # no folder yet
        self.log("run-other.jsonl", [{"run_id": "run-other", "pid": 78, "at": now.isoformat(), "event": "invocation_started"}])
        self.log("run-old.jsonl", [{"run_id": "run-old", "pid": 77, "at": now.isoformat(), "event": "invocation_started"}],
                 mtime=(now - timedelta(hours=1)).timestamp())
        self.log("run-early.jsonl", [{"run_id": "run-early", "pid": 77, "at": (now - timedelta(hours=1)).isoformat(),
                                      "event": "invocation_started"}])
        self.log("run-broken.jsonl", [])
        (self.runs / "run-broken.jsonl").write_text("{not json\n")
        self.log("run-mine.jsonl", [
            {"run_id": "run-mine", "pid": 77, "at": now.isoformat(), "event": "invocation_started"},
            {"event": "source_outcome", "reason": "SOURCE_ACCESS_WAIT"},
            {"event": "note", "reason": "lower case is not a code"},
            {"event": "programme_stopped_for_recovery", "reason": "RESEARCH_INDEX_SIZE_LIMIT", "error_type": "SafetyError"},
            {"event": "invocation_finished", "exit_code": 1}, ["not", "a", "row"]])
        self.assertEqual(run_outcome(self.root, 77, since=since),
                         {"run_id": "run-mine", "last_reason": "RESEARCH_INDEX_SIZE_LIMIT",
                          "failure_reason": "RESEARCH_INDEX_SIZE_LIMIT", "error_type": "SafetyError"})
        self.assertIsNone(run_outcome(self.root, 79, since=since))
        self.assertIsNone(run_outcome(self.root, "77", since=since))

    def test_error_type_without_a_reason_becomes_a_code_and_a_cut_tail_is_skipped(self):
        now = datetime.now(timezone.utc)
        rows = [{"run_id": "run-x", "pid": 5, "at": now.isoformat(), "event": "invocation_started"}]
        rows += [{"event": "filler", "reason": "SOURCE_ACCESS_WAIT", "pad": "x" * 200} for _ in range(20)]
        rows += [{"event": "invocation_failed", "error_type": "KeyError"}]
        self.log("run-x.jsonl", rows)
        result = run_outcome(self.root, 5, since=(now - timedelta(minutes=1)).isoformat(), tail_bytes=1000)
        self.assertEqual(result["failure_reason"], "PROGRAMME_ERROR_KEYERROR")
        self.assertEqual(result["last_reason"], "SOURCE_ACCESS_WAIT")
        self.assertEqual(operations._error_code("PermissionError"), "PROGRAMME_LOCAL_FILE_FAILED")
        self.assertEqual(operations._error_code("a.b"), "PROGRAMME_ERROR_A_B")
        self.assertEqual(operations._error_code(None), "WORKER_EXIT_FAILED")


class OwnerStatusTests(Base):
    NOW = datetime(2026, 10, 15, 12, 0, tzinfo=timezone.utc)

    def status(self, *, processes=(), now=None):
        with patch.object(controls, "owned_programmes", return_value=list(processes)):
            return controls.status(self.root, now=lambda: now or self.NOW)

    def finished(self, minutes_ago, **fields):
        return dict({"action": "worker_finished", "at": (self.NOW - timedelta(minutes=minutes_ago)).isoformat(),
                     "pid": 1}, **fields)

    def test_nothing_recorded_keeps_the_plain_waiting_message(self):
        result = self.status()
        self.assertEqual(result["message"], "Research is on. Waiting for the next scheduled run.")
        self.assertIsNone(result["attention"]); self.assertIsNone(result["attention_id"])

    def test_previous_run_reason_and_exit_code_are_shown(self):
        cases = [
            (dict(exit_code=0, outcome="finished", last_reason="WORK_TURN_SOURCE_LIMIT"),
             "Last run finished normally (exit code 0). Its last note: it used this round's allowance of website checks (normal)."),
            (dict(exit_code=0, outcome="finished", last_reason="bad code"), "Last run finished normally (exit code 0)."),
            (dict(exit_code=1, outcome="failed", failure_reason="RESEARCH_INDEX_SIZE_LIMIT"),
             "Last run stopped with a problem: the research lookup file reached its size limit (exit code 1)."),
            (dict(exit_code=1, outcome="failed", failure_reason="NEW_CODE"),
             "Last run stopped with a problem: code NEW_CODE (exit code 1)."),
            (dict(exit_code=-9, outcome="failed"), "Last run stopped with a problem: it stopped with an error (exit code -9)."),
            (dict(exit_code=130, outcome="owner_stopped"), "Last run was stopped (exit code 130)."),
            (dict(exit_code=130), "Last run was stopped (exit code 130)."),  # an older receipt
            (dict(exit_code=130, outcome="watchdog_restarted", seconds_since_activity=2700),
             "Last run stopped making progress for 45 minutes, so it was stopped (exit code 130). "
             "The next scheduled run starts research again."),
            (dict(exit_code=130, outcome="watchdog_restarted", seconds_since_activity="x"),
             "Last run stopped making progress, so it was stopped (exit code 130). The next scheduled run starts research again."),
        ]
        for receipt, line in cases:
            with self.subTest(line=line):
                for path in self.desktop.glob("*.jsonl"):
                    path.unlink()
                self.write_actions(self.finished(5, **receipt))
                result = self.status()
                self.assertEqual(result["message"], "Research is on. Waiting for the next scheduled run.\n" + line)
                self.assertIsNone(result["attention"])

    def test_stopped_research_shows_the_last_run_but_no_schedule_warning(self):
        set_programme_service_state(self.memory, enabled=False)
        self.write_actions(self.finished(300, exit_code=130, outcome="owner_stopped"))
        result = self.status()
        self.assertEqual(result["message"], "Research is stopped. Your saved work is kept.\nLast run was stopped (exit code 130).")
        self.assertIsNone(result["attention"])

    def test_saved_activity_note_is_used_when_no_receipt_exists(self):
        self.status_file(pid=999999, last_reason="SOURCE_ACCESS_WAIT")
        result = self.status()
        self.assertTrue(result["message"].endswith("\nLast research note: a website asked us to wait before checking it again."))

    def test_the_same_failure_three_runs_in_a_row_needs_attention_once(self):
        failed = dict(exit_code=1, outcome="failed", failure_reason="RESEARCH_INDEX_SIZE_LIMIT")
        self.write_actions(self.finished(12, exit_code=1, outcome="failed", failure_reason="OTHER_PROBLEM"),
                           self.finished(9, **failed), self.finished(6, **failed))
        self.assertIsNone(self.status()["attention"])  # two in a row is not yet a pattern
        self.write_actions(self.finished(3, **failed))
        result = self.status()
        self.assertEqual(result["attention"], "The last 3 research runs all stopped with the same problem: the research "
                         "lookup file reached its size limit. Research keeps retrying, but this probably needs a fix. "
                         "Open the logs or ask for help.")
        self.assertIn("\nNeeds attention: The last 3 research runs", result["message"])
        first_id = result["attention_id"]
        self.assertEqual(first_id, "repeat:RESEARCH_INDEX_SIZE_LIMIT:" + (self.NOW - timedelta(minutes=9)).isoformat())
        self.write_actions(self.finished(1, **failed))
        later = self.status()
        self.assertEqual(later["attention_id"], first_id)  # the same streak: one notification
        self.assertIn("The last 4 research runs", later["attention"])
        self.write_actions(self.finished(0, exit_code=0, outcome="finished"))
        self.assertIsNone(self.status()["attention"])

    def test_repeated_watchdog_restarts_are_a_pattern_too(self):
        self.write_actions(*[self.finished(minutes, exit_code=130, outcome="watchdog_restarted", seconds_since_activity=2700)
                             for minutes in (50, 30, 10)])
        self.assertIn("it stopped making progress", self.status()["attention"])

    def test_owner_stop_breaks_a_failure_streak(self):
        failed = dict(exit_code=1, outcome="failed", failure_reason="X_FAILED")
        self.write_actions(self.finished(50, **failed), self.finished(40, **failed),
                           self.finished(30, exit_code=130, outcome="owner_stopped"), self.finished(10, **failed))
        self.assertIsNone(self.status()["attention"])

    def test_scheduled_run_overdue_needs_attention(self):
        self.write_actions({"action": "worker_started", "at": (self.NOW - timedelta(minutes=70)).isoformat(), "pid": 1},
                           self.finished(19, exit_code=0, outcome="finished"))
        self.assertIsNone(self.status()["attention"])
        later = self.NOW + timedelta(minutes=5)
        result = self.status(now=later)
        self.assertEqual(result["attention"], "No scheduled research run has started for 24 minutes, although research is on. "
                         "The Windows scheduled task may not be running. Click Stop, then Start, to start research now.")
        self.assertEqual(result["attention_id"], "schedule:" + (self.NOW - timedelta(minutes=19)).isoformat())

    def test_two_warnings_are_both_shown(self):
        failed = dict(exit_code=1, outcome="failed", failure_reason="X_FAILED")
        self.write_actions(*[self.finished(minutes, **failed) for minutes in (80, 70, 60)])
        result = self.status()
        self.assertEqual(result["message"].count("\nNeeds attention: "), 2)
        self.assertEqual(result["attention_id"].count("|"), 1)

    def test_running_attention_reaches_the_message_with_the_run_as_its_identity(self):
        recent = {"started_at": (self.NOW - timedelta(minutes=40)).isoformat(),
                  "phase_started_at": (self.NOW - timedelta(minutes=35)).isoformat(),
                  "last_useful_result_at": (self.NOW - timedelta(minutes=33)).isoformat()}
        self.status_file(observed_at=(self.NOW - timedelta(minutes=31)).isoformat(), **recent)
        result = self.status(processes=[{"pid": os.getpid()}])
        self.assertEqual(result["state"], "running")
        self.assertIn("\nNeeds attention: Research has not recorded any progress for 31 minutes.", result["message"])
        self.assertEqual(result["attention_id"], "run-" + "a" * 32)
        self.status_file(observed_at=(self.NOW - timedelta(minutes=3)).isoformat(),
                         **dict(recent, phase_started_at=(self.NOW - timedelta(minutes=12)).isoformat(),
                                last_useful_result_at=(self.NOW - timedelta(minutes=20)).isoformat()))
        calm = self.status(processes=[{"pid": os.getpid()}])
        self.assertIsNone(calm["attention"])
        self.assertNotIn("Needs attention", calm["message"])

    def test_unreadable_history_or_settings_never_break_status(self):
        self.desktop.mkdir(parents=True)
        (self.desktop / "2026-10-15.jsonl").write_text('{not json\n["list"]\n{"action":"x","at":"not a time"}\n')
        self.assertEqual(self.actions(), [])
        self.assertIsNone(self.status()["attention"])
        with patch.object(controls, "run_health", side_effect=SafetyError("SAVED_FILE_UNAVAILABLE")):
            result = self.status()
        self.assertTrue(result["message"].endswith("\nThe earlier research runs could not be read. See the project log."))
        with patch.object(controls, "desktop_actions", side_effect=OSError("disk")):
            self.assertEqual(controls.run_health(self.root, settings=WATCH), {"line": None, "attention": []})

    def test_an_unreadable_day_file_is_skipped(self):
        self.write_actions(self.finished(3, exit_code=0, outcome="finished"))
        blocked = self.desktop / "2026-10-14.jsonl"
        blocked.write_text("{}\n"); blocked.chmod(0)
        self.addCleanup(blocked.chmod, 0o644)
        if os.access(blocked, os.R_OK):
            self.skipTest("this user can read unreadable files")
        self.assertEqual([row["action"] for row in self.actions()], ["worker_finished"])

    def test_only_recent_days_are_read(self):
        rows = [{"action": "worker_finished", "at": f"2026-10-{day:02d}T10:00:00+00:00", "exit_code": 0} for day in range(1, 11)]
        self.write_actions(*rows)
        self.assertEqual([row["at"][:10] for row in controls.desktop_actions(self.root, days=3)],
                         ["2026-10-08", "2026-10-09", "2026-10-10"])


class WatchdogUnitTests(Base):
    START = datetime(2026, 10, 15, 10, 0, tzinfo=timezone.utc)

    def test_supervisor_returns_the_normal_exit(self):
        child = FakeChild(polls=2, code=0)
        clock = Clock(self.START, step=60)
        self.assertEqual(controls._supervise(self.root, child, self.START, now=clock, watch=WATCH), (0, None))
        self.assertEqual(child.timeouts, [60, 60, 60])

    def test_silent_child_is_interrupted_and_the_switch_stays_on(self):
        child = FakeChild(pid=4242)
        clock = Clock(self.START, step=60)
        sent = []
        def killpg(pid, sig):
            sent.append((pid, sig))
            child.returncode = 130
        with patch.object(controls.os, "killpg", side_effect=killpg):
            code, restarted = controls._supervise(self.root, child, self.START, now=clock, watch=WATCH)
        self.assertEqual((code, restarted), (130, {"seconds_since_activity": 2700, "signals": ["SIGINT"]}))
        self.assertEqual(sent, [(4242, signal.SIGINT)])
        receipt = [row for row in self.actions() if row["action"] == "watchdog_restart"]
        self.assertEqual(len(receipt), 1)
        self.assertEqual((receipt[0]["reason"], receipt[0]["seconds_since_activity"], receipt[0]["pid"]),
                         ("PROGRESS_WATCH_RESTART", 2700, 4242))
        self.assertTrue(programme_service_state(self.memory)["enabled"])

    def test_recorded_activity_of_this_child_keeps_it_running(self):
        child = FakeChild(pid=os.getpid(), polls=60, code=0)
        clock = Clock(self.START, step=60)
        def observed(_root, *, as_of):
            # The child keeps recording activity up to one minute ago.
            return {"pid": os.getpid(), "observed_at": (timestamp(as_of) - timedelta(seconds=60)).isoformat()}
        with patch("product_radar.operations.read_operation_status", side_effect=observed), \
                patch.object(controls.os, "killpg") as killed:
            self.assertEqual(controls._supervise(self.root, child, self.START, now=clock, watch=WATCH), (0, None))
        killed.assert_not_called()

    def test_another_process_activity_does_not_count_for_this_child(self):
        child = FakeChild(pid=4242)
        clock = Clock(self.START, step=60)
        recent = lambda _root, *, as_of: {"pid": 1, "observed_at": as_of}
        with patch("product_radar.operations.read_operation_status", side_effect=recent), \
                patch.object(controls.os, "killpg", side_effect=lambda pid, sig: setattr(child, "returncode", -2)):
            self.assertEqual(controls._supervise(self.root, child, self.START, now=clock, watch=WATCH)[0], -2)

    def test_sleep_gap_restarts_the_quiet_time_count(self):
        child = FakeChild(pid=4242, polls=40, code=0)
        moments = iter([self.START + timedelta(minutes=1), self.START + timedelta(hours=8)]
                       + [self.START + timedelta(hours=8, minutes=minute) for minute in range(1, 60)])
        with patch.object(controls.os, "killpg") as killed:
            result = controls._supervise(self.root, child, self.START, now=lambda: next(moments), watch=WATCH)
        self.assertEqual(result, (0, None))  # 8 hours asleep plus 39 awake minutes is not 45 quiet minutes
        killed.assert_not_called()

    def test_an_unreadable_saved_time_counts_from_launch(self):
        with patch("product_radar.operations.read_operation_status",
                   return_value={"pid": 4242, "observed_at": "not a time"}):
            idle, _ = controls._activity_age(self.root, 4242, self.START, self.START + timedelta(minutes=5))
        self.assertEqual(idle, 300)

    def test_a_status_read_problem_never_restarts_research(self):
        child = FakeChild(pid=4242, polls=100, code=0)
        clock = Clock(self.START, step=60)
        with patch("product_radar.operations.read_operation_status", side_effect=RuntimeError("broken")), \
                patch.object(controls.os, "killpg") as killed:
            self.assertEqual(controls._supervise(self.root, child, self.START, now=clock, watch=WATCH), (0, None))
        killed.assert_not_called()

    def test_escalation_after_each_grace_period(self):
        child = FakeChild(pid=4242)
        sent = []
        def killpg(pid, sig):
            sent.append(sig)
            if sig == signal.SIGKILL:
                child.returncode = -9
        with patch.object(controls.os, "killpg", side_effect=killpg):
            code, signals = controls._interrupt_stuck_child(self.root, child, 2800, {}, WATCH)
        self.assertEqual((code, signals), (-9, ["SIGINT", "SIGTERM", "SIGKILL"]))
        self.assertEqual(child.timeouts, [300, 60])

    def test_child_that_already_left_is_not_signalled(self):
        child = FakeChild(pid=4242)
        with patch.object(controls.os, "killpg", side_effect=ProcessLookupError):
            child.code = 0
            self.assertEqual(controls._interrupt_stuck_child(self.root, child, 2800, {}, WATCH), (0, []))
        finished = FakeChild(pid=4242); finished.returncode = 3
        with patch.object(controls.os, "killpg") as killed:
            self.assertEqual(controls._interrupt_stuck_child(self.root, finished, 2800, {"pid": 4242, "run_id": "run-z",
                                                                                         "phase": "readiness"}, WATCH), (3, []))
        killed.assert_not_called()
        receipt = [row for row in self.actions() if row["action"] == "watchdog_restart"][-1]
        self.assertEqual((receipt["run_id"], receipt["phase"]), ("run-z", "readiness"))


class AdoptedProcessTests(Base):
    def test_an_adopted_silent_process_is_noted_then_signalled_harder(self):
        old = (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat(timespec="seconds")
        self.status_file(observed_at=old)
        row = {"pid": os.getpid(), "start_ticks": _ticks()}
        sent = []
        now = lambda: datetime.now(timezone.utc)
        with patch.object(controls, "signal_identified", side_effect=lambda found, sig: sent.append((found, sig)) or True):
            results = [controls._watch_adopted(self.root, [row], now=now, watch=WATCH) for _ in range(5)]
        self.assertEqual(results, [None, "SIGINT", "SIGTERM", "SIGKILL", "SIGKILL"])
        self.assertEqual([sig for _, sig in sent], [signal.SIGINT, signal.SIGTERM, signal.SIGKILL, signal.SIGKILL])
        self.assertTrue(all(found is row for found, _ in sent))
        actions = [row["action"] for row in self.actions()]
        self.assertEqual(actions, ["watchdog_observed"] + ["watchdog_restart"] * 4)
        self.assertTrue(all(row["adopted"] and row["observed_at"] == old for row in self.actions()))
        self.assertTrue(programme_service_state(self.memory)["enabled"])

    def test_new_activity_starts_the_count_again_and_failed_signals_are_recorded(self):
        row = {"pid": os.getpid(), "start_ticks": _ticks()}
        now = lambda: datetime.now(timezone.utc)
        self.status_file(observed_at=(datetime.now(timezone.utc) - timedelta(hours=1)).isoformat(timespec="seconds"))
        self.assertIsNone(controls._watch_adopted(self.root, [row], now=now, watch=WATCH))
        self.status_file(observed_at=(datetime.now(timezone.utc) - timedelta(minutes=50)).isoformat(timespec="seconds"))
        with patch.object(controls, "signal_identified", return_value=False) as signalled:
            self.assertIsNone(controls._watch_adopted(self.root, [row], now=now, watch=WATCH))  # noted again
            signalled.assert_not_called()
            self.assertIsNone(controls._watch_adopted(self.root, [row], now=now, watch=WATCH))  # signal refused
        self.assertEqual(self.actions()[-1]["signals"], [])

    def test_recent_activity_or_another_process_is_left_alone(self):
        row = {"pid": os.getpid(), "start_ticks": _ticks()}
        now = lambda: datetime.now(timezone.utc)
        self.status_file(observed_at=(datetime.now(timezone.utc) - timedelta(minutes=5)).isoformat(timespec="seconds"))
        with patch.object(controls, "signal_identified") as signalled:
            self.assertIsNone(controls._watch_adopted(self.root, [row], now=now, watch=WATCH))
            self.status_file(observed_at=(datetime.now(timezone.utc) - timedelta(hours=2)).isoformat(timespec="seconds"))
            self.assertIsNone(controls._watch_adopted(self.root, [dict(row, start_ticks="other")], now=now, watch=WATCH))
            self.assertIsNone(controls._watch_adopted(self.root, [{"pid": 1, "start_ticks": "1"}], now=now, watch=WATCH))
        signalled.assert_not_called()
        self.assertEqual(self.actions(), [])

    def test_worker_tick_reports_the_adopted_interrupt(self):
        row = {"pid": os.getpid(), "start_ticks": _ticks()}
        with patch.object(controls, "owned_programmes", return_value=[row]), \
                patch.object(controls, "_watch_adopted", return_value="SIGINT"), \
                patch.object(controls.subprocess, "Popen") as launch:
            self.assertEqual(controls.run_worker(self.root, ["--tick", "--execute"], settings=WATCH),
                             {"state": "watchdog_interrupted", "signal": "SIGINT"})
        launch.assert_not_called()


class RealWorkerTests(Base):
    """Real cost-free local children under the real worker; tiny thresholds."""
    FAST = dict(WATCH, watchdog_no_activity_seconds=2, watchdog_poll_seconds=1,
                watchdog_interrupt_grace_seconds=1, watchdog_terminate_grace_seconds=1)

    def script(self, body):
        path = self.root / "scripts/run_programme.py"
        path.parent.mkdir(exist_ok=True)
        path.write_text(textwrap.dedent(body))

    def test_a_silent_child_that_ignores_ctrl_c_is_stopped_and_research_stays_on(self):
        self.script("""
            import signal, time
            signal.signal(signal.SIGINT, signal.SIG_IGN)
            time.sleep(60)
        """)
        result = controls.run_worker(self.root, ["--tick", "--execute"], settings=self.FAST)
        self.assertEqual(result["exit_code"], -signal.SIGTERM)
        self.assertEqual(result["watchdog"]["signals"], ["SIGINT", "SIGTERM"])
        self.assertTrue(programme_service_state(self.memory)["enabled"])
        actions = self.actions()
        self.assertEqual([row["action"] for row in actions], ["worker_started", "watchdog_restart", "worker_finished"])
        self.assertEqual(actions[-1]["outcome"], "watchdog_restarted")
        self.assertIsNone(actions[-1]["failure_reason"])
        self.assertEqual(result["state"], "ready")
        self.assertIn("Last run stopped making progress", result["message"])

    def test_a_failed_run_records_its_own_logged_reason(self):
        self.script("""
            import json, os, pathlib, sys
            from datetime import datetime, timezone
            folder = pathlib.Path("data/operations/research-runs"); folder.mkdir(parents=True, exist_ok=True)
            now = datetime.now(timezone.utc).isoformat()
            rows = [{"run_id": "run-real", "pid": os.getpid(), "at": now, "event": "invocation_started"},
                    {"event": "programme_stopped_for_recovery", "reason": "RESEARCH_INDEX_SIZE_LIMIT"}]
            (folder / "run-real.jsonl").write_text("".join(json.dumps(row) + "\\n" for row in rows))
            sys.exit(1)
        """)
        result = controls.run_worker(self.root, ["--tick", "--execute"], settings=self.FAST)
        self.assertEqual(result["exit_code"], 1)
        receipt = self.actions()[-1]
        self.assertEqual((receipt["outcome"], receipt["run_id"], receipt["failure_reason"], receipt["last_reason"]),
                         ("failed", "run-real", "RESEARCH_INDEX_SIZE_LIMIT", "RESEARCH_INDEX_SIZE_LIMIT"))
        self.assertIn("Last run stopped with a problem: the research lookup file reached its size limit (exit code 1).",
                      result["message"])

    def test_an_unreadable_run_log_does_not_lose_the_finish_receipt(self):
        self.script("import sys\nsys.exit(2)\n")
        with patch("product_radar.operations.run_outcome", side_effect=SafetyError("SAVED_FILE_UNAVAILABLE")):
            self.assertEqual(controls.run_worker(self.root, ["--tick", "--execute"], settings=self.FAST)["exit_code"], 2)
        receipt = self.actions()[-1]
        self.assertEqual((receipt["outcome"], receipt["run_id"], receipt["failure_reason"]), ("failed", None, None))

    def test_a_broken_settings_file_never_stops_research(self):
        self.script("import sys\nsys.exit(0)\n")
        with patch.object(operations, "PROGRESS_WATCH_SETTINGS", self.root / "missing.json"):
            self.assertEqual(controls.run_worker(self.root, ["--tick", "--execute"])["exit_code"], 0)
        self.assertEqual([row["action"] for row in self.actions()],
                         ["progress_watch_settings_invalid", "worker_started", "worker_finished"])

    def test_a_normal_run_and_an_owner_stop_are_not_failures(self):
        self.script("import time\ntime.sleep(0.2)\n")
        self.assertEqual(controls.run_worker(self.root, ["--tick", "--execute"], settings=self.FAST)["exit_code"], 0)
        self.assertEqual(self.actions()[-1]["outcome"], "finished")
        self.script("""
            import sys, time
            sys.path.insert(0, %r)
            from types import SimpleNamespace
            from product_radar.programme import set_programme_service_state
            set_programme_service_state(SimpleNamespace(root=__import__("pathlib").Path("data/research/captured")), enabled=False)
            sys.exit(130)
        """ % str(Path(__file__).resolve().parents[1] / "src"))
        self.assertEqual(controls.run_worker(self.root, ["--tick", "--execute"], settings=self.FAST)["exit_code"], 130)
        self.assertEqual(self.actions()[-1]["outcome"], "owner_stopped")


if __name__ == "__main__":
    unittest.main()
