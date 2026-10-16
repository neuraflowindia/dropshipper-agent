"""Per-AI-use accounting (R18a): tokens, model time and allowance per call.

Owned local fixture processes and scripted allowance readings only; no Codex
program, account, model or network is used.
"""

import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "tests"))
from product_radar import ai_accounting, operations
from product_radar.models import SafetyError, canonical
from product_radar.research_ai import CodexRunner, Jobs, MODEL
from product_radar.research_memory import ResearchMemory
from product_radar.runtime_storage import runtime_path
from product_radar.spend_watch import AREA, load_settings
from product_radar.spend_watch_ai import (HISTORY_FILE, HISTORY_PREVIOUS, AllowanceWatchedRunner,
                                          append_allowance_history, read_allowance_history)
from product_radar.storage import Journal, load_json
from test_spend_watch_ai import NOW, Clock, Reader, reply

CONFIG = {"model": MODEL, "reasoning_effort": "high", "authentication": "chatgpt", "paid_fallback": False}
CONCEPT = "concept-" + "a" * 40
OTHER = "concept-" + "b" * 40
PAYLOAD = {"items": [{"id": "fixture-observation", "source_text": {"title": "Manufactured fixture."}}]}
RESPONSE = {"results": [{"id": "fixture-observation", "kind": "uninterpretable", "products": [],
                         "reason": "Manufactured response for accounting tests."}]}
USAGE = {"input_tokens": 15000, "cached_input_tokens": 500, "output_tokens": 300}


def events(*rows):
    return b"\n".join(canonical(row) for row in rows) + b"\n"


COMPLETE = events({"type": "turn.started"},
                  {"type": "item.completed", "item": {"type": "agent_message", "text": canonical(RESPONSE).decode()}},
                  {"type": "turn.completed", "usage": USAGE})


def run_rows(root, recorder):
    path = runtime_path(root, "data/operations/research-runs") / (recorder.run_id + ".jsonl")
    return [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []


class Case(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="ai-accounting-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.memory = ResearchMemory(self.root / "research", Journal(self.root / "finance"), evidence_type="synthetic")
        self.processes = []

    def folder(self, job_id):
        return self.memory.root / "ai_jobs" / job_id

    def accounting(self, job_id=None):
        job_id = job_id or next(iter(self.memory.snapshot()["jobs"]))
        return load_json(self.folder(job_id) / ai_accounting.ACCOUNTING_FILE)

    def launch(self, script, *, runner=None, deadline=5, payload=PAYLOAD, role="A"):
        """Run one job through a real owned fixture process standing in for Codex."""
        real_popen = subprocess.Popen

        def fixture_process(command, **kwargs):
            process = real_popen([sys.executable, "-c", script], **kwargs)
            self.processes.append(process)
            return process
        login = subprocess.CompletedProcess([], 0, b"Logged in using ChatGPT", b"")
        with patch("product_radar.research_ai.shutil.which", return_value="/fixture/codex"), \
             patch("product_radar.research_ai.subprocess.run", return_value=login), \
             patch("product_radar.research_ai.subprocess.Popen", side_effect=fixture_process), \
             patch("product_radar.research_ai.MODEL_DEADLINE_SECONDS", deadline):
            return Jobs(self.memory, runner or CodexRunner(CONFIG)).run(role, payload)


class CodexAccountingTests(Case):
    def test_completed_call_saves_model_time_tokens_purpose_and_a_run_log_row(self):
        script = "import sys;sys.stdin.buffer.read();sys.stdout.buffer.write(" + repr(COMPLETE) + ")"
        with operations.operation_run(self.root) as recorder:
            job_id, result = self.launch(script)
        self.assertIn("fixture-observation", result["accepted"])
        saved = self.accounting(job_id)
        self.assertEqual((saved["kind"], saved["job_id"], saved["attempt"], saved["role"], saved["purpose"]),
                         (ai_accounting.KIND, job_id, "original", "A", "A"))
        self.assertEqual((saved["item_count"], saved["item_ids"], saved["concept_ids"]), (1, ["fixture-observation"], []))
        self.assertEqual(saved["tokens"], {"input": 15000, "cached_input": 500, "output": 300})
        self.assertIsNone(saved["tokens_missing_reason"])
        self.assertEqual(saved["outcome"], "completed")
        model = saved["model"]
        self.assertEqual((model["exit_code"], model["usage_source"]), (0, "turn_completed"))
        self.assertLessEqual(saved["started_at"], model["started_at"])
        self.assertLessEqual(model["started_at"], model["finished_at"])
        self.assertLessEqual(model["finished_at"], saved["finished_at"])
        self.assertGreaterEqual(saved["call_seconds"], model["seconds"])
        # Without the allowance watch nothing was read: said so, never invented.
        self.assertEqual(saved["allowance"]["before_missing_reason"], "not_read_by_runner")
        self.assertNotIn("Manufactured", json.dumps(saved))  # no prompt or answer text
        rows = [row for row in run_rows(self.root, recorder) if row["event"] == "ai_use_finished"]
        self.assertEqual(len(rows), 1)
        row = rows[0]
        self.assertEqual((row["job_id"], row["role"], row["purpose"], row["state"], row["item_count"]),
                         (job_id, "A", "A", "completed", 1))
        self.assertEqual((row["input_tokens"], row["cached_input_tokens"], row["output_tokens"]), (15000, 500, 300))
        self.assertIs(row["accounting_saved"], True)
        self.assertNotIn("reason", row)
        self.assertEqual(operations.read_operation_status(self.root)["state"], "not_recorded")  # no writer, no status

    def test_failed_run_keeps_the_tokens_its_stream_already_reported(self):
        partial = events({"type": "turn.started"}, {"type": "turn.completed", "usage": USAGE}) + b'{"type": "tur'
        script = ("import sys;sys.stdin.buffer.read();sys.stdout.buffer.write(" + repr(partial)
                  + ");sys.stderr.write('failed to connect');sys.exit(3)")
        with operations.operation_run(self.root) as recorder:
            with self.assertRaisesRegex(SafetyError, "AI_NETWORK_UNAVAILABLE"):
                self.launch(script)
        saved = self.accounting()
        self.assertEqual((saved["outcome"], saved["model"]["exit_code"]), ("AI_NETWORK_UNAVAILABLE", 3))
        self.assertEqual((saved["tokens"]["input"], saved["model"]["usage_source"]), (15000, "partial_stream"))
        row = next(row for row in run_rows(self.root, recorder) if row["event"] == "ai_use_finished")
        self.assertEqual((row["state"], row["reason"], row["input_tokens"]), ("failed", "AI_NETWORK_UNAVAILABLE", 15000))

    def test_timed_out_run_without_usage_says_why_its_tokens_are_unknown(self):
        with self.assertRaisesRegex(SafetyError, "AI_INTERRUPTED_REVIEW_SAVED_JOB"):
            self.launch("import sys,time;sys.stdin.buffer.read();time.sleep(30)", deadline=0.02)
        saved = self.accounting()
        self.assertIsNone(saved["tokens"])
        self.assertEqual(saved["tokens_missing_reason"], "stream_reported_no_usage")
        self.assertEqual(saved["outcome"], "AI_INTERRUPTED_REVIEW_SAVED_JOB")
        self.assertIsNotNone(saved["model"]["finished_at"])
        self.assertIsNotNone(self.processes[0].poll())

    def test_a_call_that_never_started_the_model_is_accounted_without_model_time(self):
        login = subprocess.CompletedProcess([], 1, b"Not logged in", b"")
        with patch("product_radar.research_ai.shutil.which", return_value="/fixture/codex"), \
             patch("product_radar.research_ai.subprocess.run", return_value=login), \
             patch("product_radar.research_ai.subprocess.Popen", side_effect=AssertionError("no model")):
            with self.assertRaises(SafetyError):
                Jobs(self.memory, CodexRunner(CONFIG)).run("A", PAYLOAD)
        saved = self.accounting()
        self.assertIsNone(saved["model"])
        self.assertEqual((saved["model_missing_reason"], saved["tokens_missing_reason"]),
                         ("model_not_started", "model_not_started"))
        self.assertNotEqual(saved["outcome"], "completed")

    def test_saved_answers_are_reused_without_a_second_accounting(self):
        script = "import sys;sys.stdin.buffer.read();sys.stdout.buffer.write(" + repr(COMPLETE) + ")"
        job_id, _ = self.launch(script)
        before = (self.folder(job_id) / ai_accounting.ACCOUNTING_FILE).read_bytes()
        with patch.object(CodexRunner, "__call__", side_effect=AssertionError("saved result reuse")), \
             operations.operation_run(self.root) as recorder:
            Jobs(self.memory, CodexRunner(CONFIG)).run("A", PAYLOAD)
        self.assertEqual((self.folder(job_id) / ai_accounting.ACCOUNTING_FILE).read_bytes(), before)
        self.assertFalse([row for row in run_rows(self.root, recorder) if row["event"] == "ai_use_finished"])

    def test_an_unsaveable_record_never_changes_the_research_outcome(self):
        script = "import sys;sys.stdin.buffer.read();sys.stdout.buffer.write(" + repr(COMPLETE) + ")"
        with patch("product_radar.ai_accounting.build_record", side_effect=RuntimeError("broken")):
            _, result = self.launch(script)
        self.assertIn("fixture-observation", result["accepted"])
        with patch("product_radar.storage.atomic_json", side_effect=OSError("disk")), \
             operations.operation_run(self.root) as recorder:
            record = ai_accounting.build_record(job_id="job-x", attempt="original", request={"role": "A", "payload": PAYLOAD},
                                                admission=None, notes={}, outcome="completed", started_at=NOW.isoformat(),
                                                finished_at=NOW.isoformat(), call_seconds=0.5)
            self.assertFalse(ai_accounting.save_and_log(self.root, record))
        row = next(row for row in run_rows(self.root, recorder) if row["event"] == "ai_use_finished")
        self.assertIs(row["accounting_saved"], False)


class AllowanceAccountingTests(Case):
    def setUp(self):
        super().setUp()
        self.clock, self.settings = Clock(), load_settings()

    def watched(self, reader, inner):
        return AllowanceWatchedRunner(inner, root=self.root, reader=reader, clock=self.clock, settings=self.settings,
                                      sleep=lambda seconds: None)

    def test_allowance_before_and_after_each_call_are_the_readings_the_watch_already_takes(self):
        five = {"usedPercent": 12, "windowDurationMins": 300, "resetsAt": int(NOW.timestamp()) + 3600}
        reader = Reader(reply(43, secondary=five), reply(44, secondary=dict(five, usedPercent=14)))

        def inner(role, payload, folder):
            return RESPONSE, USAGE
        watched = self.watched(reader, inner)
        watched.check_new_execution()  # admission check: fresh reading, then cached for the call
        job_id, _ = Jobs(self.memory, watched).run("A", PAYLOAD)
        self.assertEqual(reader.calls, 2)  # no extra reading for accounting
        saved = self.accounting(job_id)
        before, after = saved["allowance"]["before"], saved["allowance"]["after"]
        self.assertEqual((before["source"], before["from_cache"]), ("before_call", True))
        self.assertGreaterEqual(before["age_seconds"], 0)
        self.assertEqual({w["window"]: w["used_percent"] for w in before["windows"]}, {"weekly": 43, "5-hour": 12})
        self.assertEqual((after["source"], after["from_cache"], after["age_seconds"]), ("after_call", False, 0.0))
        self.assertEqual({w["window"]: w["used_percent"] for w in after["windows"]}, {"weekly": 44, "5-hour": 14})
        self.assertEqual(saved["tokens"]["output"], 300)
        self.assertNotIn("account-private", json.dumps(saved))
        history = read_allowance_history(self.root)
        self.assertEqual([row["source"] for row in history], ["admission_check", "after_call"])
        self.assertEqual(history[-1]["windows"][0]["used_percent"], 44)

    def test_failed_after_reading_and_refused_calls_say_why_a_reading_is_missing(self):
        from product_radar.models import SafetyError as Error
        reader = Reader(reply(10), Error("AI_USAGE_READ_TIMEOUT"))

        def inner(role, payload, folder):
            return RESPONSE, USAGE
        Jobs(self.memory, self.watched(reader, inner)).run("A", PAYLOAD)
        saved = self.accounting()
        self.assertIsNotNone(saved["allowance"]["before"])
        self.assertEqual(saved["allowance"]["after_missing_reason"], "AI_USAGE_READ_FAILED")
        self.assertEqual(read_allowance_history(self.root)[-1], {"at": NOW.isoformat(), "source": "after_call",
                                                                 "failed": "AI_USAGE_READ_TIMEOUT"})
        # A call refused at its own admission: nothing ran, nothing was read after.
        refused = AllowanceWatchedRunner(lambda *args: (RESPONSE, USAGE), root=self.root / "other", reader=Reader(reply(100)),
                                         clock=self.clock, settings=self.settings, sleep=lambda seconds: None)
        with patch("product_radar.ai_accounting._NOTES") as notes:
            notes.get.return_value = captured = {}
            with self.assertRaisesRegex(SafetyError, "AI_PLAN_LIMIT_CREDITS_PROTECTED"):
                refused("A", PAYLOAD, self.root)
        self.assertEqual(captured, {"allowance_before_missing": "AI_PLAN_LIMIT_CREDITS_PROTECTED",
                                    "allowance_after_missing": "call_not_admitted"})

    def test_an_owner_stop_is_not_delayed_by_an_after_call_reading(self):
        def stopped(role, payload, folder):
            raise KeyboardInterrupt
        with self.assertRaises(KeyboardInterrupt):
            Jobs(self.memory, self.watched(Reader(reply(10)), stopped)).run("A", PAYLOAD)
        saved = self.accounting()
        self.assertEqual((saved["outcome"], saved["allowance"]["after_missing_reason"]),
                         ("AI_INTERRUPTED_REVIEW_SAVED_JOB", "owner_stop_not_read"))

    def test_held_ai_steps_are_logged_once_per_round_per_reason(self):
        watched = self.watched(Reader(reply(100)), lambda *args: (RESPONSE, USAGE))
        with operations.operation_run(self.root) as recorder:
            for _ in range(3):
                with self.assertRaisesRegex(SafetyError, "AI_PLAN_LIMIT_CREDITS_PROTECTED"):
                    watched.check_new_execution()
            operations.record_event("round_finished", state="finished")
            with self.assertRaisesRegex(SafetyError, "AI_PLAN_LIMIT_CREDITS_PROTECTED"):
                watched.check_new_execution()
        held = [row for row in run_rows(self.root, recorder) if row["event"] == "ai_use_held"]
        self.assertEqual(len(held), 2)
        self.assertEqual((held[0]["reason"], held[0]["state"]), ("AI_PLAN_LIMIT_CREDITS_PROTECTED", "waiting"))
        self.assertEqual(recorder.counts["deferred"], 2)
        self.assertEqual(recorder.last_reason, "AI_PLAN_LIMIT_CREDITS_PROTECTED")
        self.assertIn("paid Codex credits", operations.plain_reason("AI_PLAN_LIMIT_CREDITS_PROTECTED"))

    def test_history_is_bounded_and_unreadable_lines_are_skipped(self):
        row = {"at": NOW.isoformat(), "source": "after_call", "failed": "AI_USAGE_READ_TIMEOUT"}
        for _ in range(4):
            self.assertTrue(append_allowance_history(self.root, row, max_bytes=200))
        folder = runtime_path(self.root, AREA)
        self.assertTrue((folder / HISTORY_PREVIOUS).exists())
        self.assertLessEqual((folder / HISTORY_FILE).stat().st_size, 200)
        with (folder / HISTORY_FILE).open("ab") as stream:
            stream.write(b'{"cut in ha')
        self.assertEqual(read_allowance_history(self.root, limit=2), [row, row])
        self.assertEqual(len(read_allowance_history(self.root)), 4)
        (folder / HISTORY_FILE).unlink()
        (folder / HISTORY_FILE).symlink_to(folder / HISTORY_PREVIOUS)
        self.assertFalse(append_allowance_history(self.root, row))  # never follows a link, never stops AI
        self.assertEqual(len(read_allowance_history(self.root)), 2)


class AccountingPartsTests(unittest.TestCase):
    def test_stream_usage_takes_the_last_complete_turn_and_skips_cut_lines(self):
        later = dict(USAGE, output_tokens=301)
        raw = events({"type": "turn.completed", "usage": USAGE}, {"type": "turn.completed", "usage": later},
                     {"type": "turn.completed", "usage": {"input_tokens": "x"}}, ["not", "an", "event"]) + b"{cut"
        self.assertEqual(ai_accounting.stream_usage(raw), later)
        self.assertIsNone(ai_accounting.stream_usage(b""))
        self.assertIsNone(ai_accounting.stream_usage(None))

    def test_ids_come_from_item_ids_concept_ids_and_supplier_products(self):
        payload = {"items": [{"id": CONCEPT}, {"id": "lead_1", "product": {"concept_id": OTHER}},
                             {"id": "question-1", "concept_id": CONCEPT}, {"id": "bad id"}, "not an item"]}
        self.assertEqual(ai_accounting.job_ids(payload), {"item_ids": [CONCEPT, "lead_1", "question-1"],
                                                          "concept_ids": [CONCEPT, OTHER]})
        self.assertEqual(ai_accounting.job_ids(None), {"item_ids": [], "concept_ids": []})

    def test_turn_admission_purpose_mode_and_one_concept_reach_the_run_log_row(self):
        request = {"role": "E", "payload": {"mode": "fit_assessment", "items": [{"id": CONCEPT}]},
                   "prompt_version": "research-e-fit10-v2", "runner": "codex-chatgpt-high-v1", "model": MODEL}
        notes = {"model_started_at": NOW.isoformat(), "model_finished_at": NOW.isoformat(), "model_seconds": 41.5,
                 "exit_code": 0}
        record = ai_accounting.build_record(job_id="job-1", attempt="retry-1", request=request,
                                            admission={"purpose": "E:fit", "origin_turn_id": "turn-1"}, notes=notes,
                                            outcome="completed", started_at=NOW.isoformat(), finished_at=NOW.isoformat(),
                                            call_seconds=44.25, returned_usage=None)
        self.assertEqual((record["purpose"], record["mode"], record["turn_id"]), ("E:fit", "fit_assessment", "turn-1"))
        self.assertEqual(record["tokens_missing_reason"], "stream_reported_no_usage")
        unfinished = dict(record, **ai_accounting.build_record(
            job_id="job-1", attempt="retry-1", request=request, admission=None, notes={"model_started_at": NOW.isoformat()},
            outcome="AI_LOCAL_EXECUTION_FAILED", started_at=NOW.isoformat(), finished_at=NOW.isoformat(), call_seconds=1))
        self.assertEqual(unfinished["tokens_missing_reason"], "not_reported_by_runner")
        self.assertEqual(ai_accounting.log_values(unfinished)["reason"], "AI_LOCAL_EXECUTION_FAILED")
        values = ai_accounting.log_values(record)
        self.assertEqual((values["concept_id"], values["model_seconds"], values["attempt"]), (CONCEPT, 41.5, "retry-1"))
        with patch("product_radar.work_turns.model_purpose", side_effect=KeyError):
            self.assertIsNone(ai_accounting._purpose(request, None))
        self.assertEqual(ai_accounting._purpose(request, None), "E:fit")
        self.assertEqual(ai_accounting.window_name({"duration_minutes": 60}), "60-minute")
        self.assertEqual(ai_accounting.window_name({"duration_minutes": None, "slot": "secondary"}), "secondary")
        self.assertIsNone(ai_accounting.usage_tokens({"input_tokens": 1, "cached_input_tokens": 2, "output_tokens": 0}))

    def test_a_note_outside_an_accounted_call_is_ignored(self):
        ai_accounting.note(usage=USAGE)  # no error, nothing kept
        call = ai_accounting.CallAccounting()
        ai_accounting.note(exit_code=0)
        self.assertEqual(call.notes, {"exit_code": 0})
        with tempfile.TemporaryDirectory() as folder:
            # No intent file: still saved (purpose from the request), never raised.
            self.assertTrue(call.finish(job_id="job-1", base=Path(folder), folder=Path(folder),
                                        request={"role": "A", "payload": PAYLOAD}))
        ai_accounting.note(exit_code=1)
        self.assertEqual(call.notes, {"exit_code": 0})


if __name__ == "__main__":
    unittest.main()
