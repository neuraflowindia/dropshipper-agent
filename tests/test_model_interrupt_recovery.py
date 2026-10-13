"""Owned local fixture processes only; no Codex/model/provider calls."""

from copy import deepcopy
from pathlib import Path
import json
import os
import signal
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
from product_radar.models import SafetyError, canonical, digest
from product_radar.research_ai import (CodexRunner, Jobs, MODEL, _finish_owned_process,
                                     current_attempt, prepare_retry)
from product_radar.research_memory import ResearchMemory, key
from product_radar.storage import Journal, atomic_json, load_json

CONFIG = {"model": MODEL, "reasoning_effort": "high", "authentication": "chatgpt", "paid_fallback": False}
PAYLOAD = {"items": [{"id": "fixture-observation", "source_text": {"title": "Manufactured fixture."}}]}
RESPONSE = {"results": [{"id": "fixture-observation", "kind": "uninterpretable", "products": [], "reason": "Manufactured response for recovery tests."}]}
EVENTS = b"\n".join(canonical(event) for event in [
    {"type": "turn.started"}, {"type": "item.completed", "item": {"type": "agent_message", "text": canonical(RESPONSE).decode()}},
    {"type": "turn.completed", "usage": {"input_tokens": 10, "cached_input_tokens": 0, "output_tokens": 10}}]) + b"\n"


class InterruptRecoveryTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.memory = ResearchMemory(self.root / "research", Journal(self.root / "finance"), evidence_type="synthetic")
        self.runner = CodexRunner(CONFIG)
        self.processes = []

    def launch(self, script, *, deadline=0.02):
        real_popen = subprocess.Popen
        def fixture_process(command, **kwargs):
            self.assertEqual(command[0], "/fixture/codex")
            self.assertIs(kwargs["start_new_session"], True)
            process = real_popen([sys.executable, "-c", script], **kwargs)
            self.processes.append(process)
            return process
        login = subprocess.CompletedProcess([], 0, b"Logged in using ChatGPT", b"")
        with patch("product_radar.research_ai.shutil.which", return_value="/fixture/codex"), \
             patch("product_radar.research_ai.subprocess.run", return_value=login), \
             patch("product_radar.research_ai.subprocess.Popen", side_effect=fixture_process), \
             patch("product_radar.research_ai.MODEL_DEADLINE_SECONDS", deadline):
            return Jobs(self.memory, self.runner).run("A", PAYLOAD)

    def interrupted(self):
        with self.assertRaisesRegex(SafetyError, "AI_INTERRUPTED_REVIEW_SAVED_JOB"):
            self.launch("import sys,time;sys.stdin.buffer.read();time.sleep(30)")
        job_id = next(iter(self.memory.snapshot()["jobs"]))
        return job_id, self.memory.root / "ai_jobs" / job_id

    def test_timeout_finishes_only_owned_process_before_bound_failure_and_unknown_usage(self):
        real_killpg = os.killpg
        calls = []
        def owned_kill(group, sig):
            self.assertIn(group, [p.pid for p in self.processes])
            calls.append((group, sig))
            return real_killpg(group, sig)
        with patch("product_radar.research_ai.os.killpg", side_effect=owned_kill):
            job_id, folder = self.interrupted()
        process = self.processes[0]
        self.assertIsNotNone(process.poll())
        self.assertTrue(process.stdin.closed)
        self.assertTrue(all(group == process.pid for group, _ in calls))
        self.assertIn(signal.SIGTERM, [sig for _, sig in calls])
        receipt = load_json(folder / "execution_failure.json")
        execution = load_json(folder / "runner_execution.json")
        failure = load_json(folder / "failure.json")
        self.assertIs(receipt["owned_process_finished"], True)
        self.assertIs(receipt["owned_group_cleanup_confirmed"], True)
        self.assertEqual(receipt["exit_code"], process.returncode)
        self.assertEqual(receipt["job_id"], job_id)
        self.assertEqual(receipt["request_hash"], digest(self.memory.snapshot()["jobs"][job_id]["request"]))
        self.assertTrue(all(receipt[k] == value for k, value in execution.items()))
        self.assertIsNone(receipt["usage"]); self.assertIsNone(failure["usage"])
        self.assertEqual(receipt["interruption"], "deadline")
        self.assertFalse((folder / "completion.json").exists())
        self.assertFalse((folder / "result.json").exists())
        self.assertLessEqual(receipt["at"], failure["at"])
        self.assertNotIn("Manufactured fixture", canonical(receipt).decode())
        self.assertNotIn("pid", receipt)

    def test_deliberate_retry_keeps_original_failure_and_never_calls_model_during_preparation(self):
        job_id, folder = self.interrupted()
        originals = {name: (folder / name).read_bytes() for name in ("intent.json", "failure.json", "runner_execution.json", "execution_failure.json")}
        self.assertEqual(prepare_retry(self.memory, job_id, "Retry this fixture after its confirmed local termination."), 1)
        self.assertEqual(current_attempt(folder), folder / "retry-1")
        self.assertEqual(len(self.processes), 1)
        self.assertFalse((folder / "retry-1" / "intent.json").exists())
        for name, raw in originals.items():
            self.assertEqual((folder / name).read_bytes(), raw)
        retries = load_json(folder / "retries.json")
        self.assertEqual(retries[0]["previous_failure"], "AI_INTERRUPTED_REVIEW_SAVED_JOB")
        with self.assertRaisesRegex(SafetyError, "SAVED_FILE_UNAVAILABLE"):
            prepare_retry(self.memory, job_id, "No new failed attempt exists yet.")
        self.assertEqual(len(load_json(folder / "retries.json")), 1)

    def test_next_deliberate_attempt_binds_retry_folder_and_saved_success_reuses_without_new_model(self):
        job_id, folder = self.interrupted()
        prepare_retry(self.memory, job_id, "Deliberate fixture retry.")
        script = "import sys;sys.stdin.buffer.read();sys.stdout.buffer.write(" + repr(EVENTS) + ")"
        recovered_id, result = self.launch(script, deadline=10)
        self.assertEqual(recovered_id, job_id)
        self.assertEqual(result["accepted"]["fixture-observation"]["kind"], "uninterpretable")
        retry = folder / "retry-1"
        self.assertEqual(load_json(retry / "runner_execution.json")["attempt"], "retry-1")
        self.assertFalse((retry / "execution_failure.json").exists())
        with patch.object(CodexRunner, "__call__", side_effect=AssertionError("A saved response must be reused")):
            self.assertEqual(Jobs(self.memory, self.runner).run("A", PAYLOAD), (job_id, result))
        with self.assertRaisesRegex(SafetyError, "AI_COMPLETED_RESULT_REUSE_REQUIRED"):
            prepare_retry(self.memory, job_id, "Do not make a second model call.")
        self.assertEqual(len(self.processes), 2)

    def test_missing_historical_terminal_receipt_never_backfills_or_authorizes_retry(self):
        # This old-style failure intentionally has no runner receipts. Local
        # current process observations cannot prove that historical execution.
        request = {"role": "A", "payload": PAYLOAD, "model": MODEL, "prompt_version": "research-abe-v1", "runner": self.runner.identity}
        job_id = key("job", request)
        self.memory.put("jobs", job_id, {"request": request, "binding_keys": {}})
        folder = self.memory.root / "ai_jobs" / job_id
        atomic_json(folder / "intent.json", {"request": request, "created_at": "2026-10-06T10:00:00+00:00"})
        atomic_json(folder / "failure.json", {"code": "AI_INTERRUPTED_REVIEW_SAVED_JOB", "usage": None})
        original = (folder / "failure.json").read_bytes()
        with self.assertRaisesRegex(SafetyError, "AI_RETRY_RECONCILIATION_REQUIRED"):
            prepare_retry(self.memory, job_id, "Unknown old process cannot be guessed.")
        self.assertEqual((folder / "failure.json").read_bytes(), original)
        self.assertFalse((folder / "execution_failure.json").exists())
        self.assertFalse((folder / "retries.json").exists())

    def test_mismatched_or_nonterminal_receipts_are_refused(self):
        job_id, folder = self.interrupted()
        execution = load_json(folder / "runner_execution.json")
        receipt = load_json(folder / "execution_failure.json")
        changes = [lambda r: r.update(request_hash="f" * 64), lambda r: r.update(intent_hash="f" * 64),
                   lambda r: r.update(execution_id="other-execution"), lambda r: r.update(attempt="retry-9"),
                   lambda r: r.update(runner="other-runner"), lambda r: r.update(owned_process_finished=False),
                   lambda r: r.update(owned_group_cleanup_confirmed=False), lambda r: r.update(exit_code=None),
                   lambda r: r.update(exit_code=True), lambda r: r.update(code="AI_NETWORK_UNAVAILABLE"),
                   lambda r: r.update(at="2000-01-01T00:00:00+00:00"), lambda r: r.update(usage={"input_tokens": 0})]
        for change in changes:
            edited = deepcopy(receipt); change(edited)
            atomic_json(folder / "execution_failure.json", edited)
            with self.assertRaisesRegex(SafetyError, "AI_RETRY_RECONCILIATION_REQUIRED"):
                prepare_retry(self.memory, job_id, "Malformed receipt stays blocked.")
            self.assertFalse((folder / "retries.json").exists())
        atomic_json(folder / "execution_failure.json", receipt)
        edited = deepcopy(execution); edited["request_hash"] = "f" * 64
        atomic_json(folder / "runner_execution.json", edited)
        with self.assertRaisesRegex(SafetyError, "AI_RETRY_RECONCILIATION_REQUIRED"):
            prepare_retry(self.memory, job_id, "A changed launch record is not accepted.")

    def test_saved_completion_wins_over_interrupt_receipt_and_reuses_without_runner(self):
        job_id, folder = self.interrupted()
        atomic_json(folder / "completion.json", {"response": RESPONSE, "usage": None, "exit_code": 0})
        with self.assertRaisesRegex(SafetyError, "AI_COMPLETED_RESULT_REUSE_REQUIRED"):
            prepare_retry(self.memory, job_id, "The answer is already saved.")
        recovered, result = Jobs(self.memory).run("A", PAYLOAD)
        self.assertEqual(recovered, job_id)
        self.assertIn("fixture-observation", result["accepted"])
        self.assertTrue((folder / "result.json").exists())
        self.assertEqual(len(self.processes), 1)

    def test_complete_provider_turn_before_wrapper_timeout_is_reused_not_retried(self):
        script = "import sys,time;sys.stdin.buffer.read();sys.stdout.buffer.write(" + repr(EVENTS) + ");sys.stdout.buffer.flush();time.sleep(30)"
        job_id, result = self.launch(script)
        self.assertIn("fixture-observation", result["accepted"])
        folder = self.memory.root / "ai_jobs" / job_id
        completion = load_json(folder / "completion.json")
        self.assertEqual(completion["response"], RESPONSE)
        self.assertEqual(completion["recovered_after"], "AI_INTERRUPTED_REVIEW_SAVED_JOB")
        self.assertEqual(completion["usage"]["input_tokens"], 10)
        self.assertIsNotNone(self.processes[0].poll())
        self.assertTrue((folder / "execution_failure.json").exists())
        self.assertTrue((folder / "result.json").exists())
        self.assertFalse((folder / "failure.json").exists())
        with self.assertRaisesRegex(SafetyError, "AI_COMPLETED_RESULT_REUSE_REQUIRED"):
            prepare_retry(self.memory, job_id, "The completed turn must not be inferred again.")
        with patch.object(CodexRunner, "__call__", side_effect=AssertionError("saved result reuse")):
            self.assertEqual(Jobs(self.memory, self.runner).run("A", PAYLOAD), (job_id, result))

    def test_partial_turn_is_not_mistaken_for_a_completed_response(self):
        partial = canonical({"type": "turn.started"}) + b"\n"
        script = "import sys,time;sys.stdin.buffer.read();sys.stdout.buffer.write(" + repr(partial) + ");sys.stdout.buffer.flush();time.sleep(30)"
        with self.assertRaisesRegex(SafetyError, "AI_INTERRUPTED_REVIEW_SAVED_JOB"):
            self.launch(script)
        job_id = next(iter(self.memory.snapshot()["jobs"]))
        folder = self.memory.root / "ai_jobs" / job_id
        self.assertFalse((folder / "completion.json").exists())
        self.assertEqual(prepare_retry(self.memory, job_id, "Only an explicit retry after confirmed fixture termination."), 1)

    def test_nonzero_cli_failure_keeps_classified_terminal_receipt_and_existing_retry_policy(self):
        with self.assertRaisesRegex(SafetyError, "AI_NETWORK_UNAVAILABLE"):
            self.launch("import sys;sys.stdin.buffer.read();sys.stderr.write('failed to connect');sys.exit(2)", deadline=10)
        job_id = next(iter(self.memory.snapshot()["jobs"]))
        folder = self.memory.root / "ai_jobs" / job_id
        receipt = load_json(folder / "execution_failure.json")
        self.assertEqual(receipt["exit_code"], 2)
        self.assertEqual(receipt["code"], "AI_NETWORK_UNAVAILABLE")
        self.assertIs(receipt["owned_process_finished"], True)
        self.assertNotIn("failed to connect", canonical(receipt).decode())
        self.assertEqual(prepare_retry(self.memory, job_id, "The saved known failure keeps its existing explicit retry route."), 1)

    def test_keyboard_interruption_is_finished_then_saved_as_interrupted_failure(self):
        class Process:
            pid = 12345
            returncode = None
            def communicate(self, *args, **kwargs): raise KeyboardInterrupt
            def wait(self, timeout): self.returncode = -signal.SIGTERM; return self.returncode
            def poll(self): return self.returncode
        process = Process()
        login = subprocess.CompletedProcess([], 0, b"Logged in using ChatGPT", b"")
        with patch("product_radar.research_ai.shutil.which", return_value="/fixture/codex"), \
             patch("product_radar.research_ai.subprocess.run", return_value=login), \
             patch("product_radar.research_ai.subprocess.Popen", return_value=process), \
             patch("product_radar.research_ai.os.killpg") as kill:
            with self.assertRaisesRegex(SafetyError, "AI_INTERRUPTED_REVIEW_SAVED_JOB"):
                Jobs(self.memory, self.runner).run("A", PAYLOAD)
        self.assertEqual(kill.call_args_list[0].args, (12345, signal.SIGTERM))
        job_id = next(iter(self.memory.snapshot()["jobs"]))
        receipt = load_json(self.memory.root / "ai_jobs" / job_id / "execution_failure.json")
        self.assertEqual(receipt["exit_code"], process.returncode)
        self.assertIs(receipt["owned_process_finished"], True)
        self.assertEqual(receipt["interruption"], "keyboard_interrupt")

    def test_failed_cleanup_never_creates_terminal_receipt_or_allows_retry(self):
        class Process:
            pid = 12345
            returncode = None
            def communicate(self, *args, **kwargs): raise KeyboardInterrupt
            def wait(self, timeout): raise subprocess.TimeoutExpired("fixture-only", timeout)
            def poll(self): return None
        login = subprocess.CompletedProcess([], 0, b"Logged in using ChatGPT", b"")
        with patch("product_radar.research_ai.shutil.which", return_value="/fixture/codex"), \
             patch("product_radar.research_ai.subprocess.run", return_value=login), \
             patch("product_radar.research_ai.subprocess.Popen", return_value=Process()), \
             patch("product_radar.research_ai.os.killpg"):
            with self.assertRaisesRegex(SafetyError, "AI_OWNED_PROCESS_RECONCILIATION_REQUIRED"):
                Jobs(self.memory, self.runner).run("A", PAYLOAD)
        job_id = next(iter(self.memory.snapshot()["jobs"]))
        folder = self.memory.root / "ai_jobs" / job_id
        self.assertFalse((folder / "execution_failure.json").exists())
        with self.assertRaisesRegex(SafetyError, "AI_RETRY_RECONCILIATION_REQUIRED"):
            prepare_retry(self.memory, job_id, "No false terminal claim.")

    def test_owner_stop_propagates_after_cleanup_and_saves_failure(self):
        self.check_owner_stop(completed=False)

    def test_owner_stop_saves_completed_answer_without_continuing_the_batch(self):
        self.check_owner_stop(completed=True)

    def test_owner_exit_propagates_its_original_exit_code(self):
        self.check_owner_stop(completed=False, interruption=SystemExit(7))

    def test_owner_stop_is_not_swallowed_if_execution_receipt_cannot_be_saved(self):
        self.check_owner_stop(completed=False, failed_save="execution_failure.json")

    def test_owner_stop_is_not_swallowed_if_job_failure_cannot_be_saved(self):
        self.check_owner_stop(completed=False, failed_save="failure.json")

    def test_owner_stop_is_not_swallowed_if_child_cleanup_cannot_be_confirmed(self):
        self.check_owner_stop(completed=False, failed_cleanup=True)

    def check_owner_stop(self, *, completed, interruption=None, failed_save=None, failed_cleanup=False):
        interruption = KeyboardInterrupt() if interruption is None else interruption
        class Process:
            pid = 12345
            returncode = None
            def communicate(inner, *args, **kwargs):
                if completed:
                    inner.output.write(EVENTS)
                    inner.output.flush()
                raise interruption
            def wait(inner, timeout):
                if failed_cleanup:
                    raise subprocess.TimeoutExpired("fixture-only", timeout)
                inner.returncode = -signal.SIGTERM
                return inner.returncode
            def poll(inner): return inner.returncode
        process = Process()
        def launch(_command, **kwargs):
            process.output = kwargs["stdout"]
            return process
        def save(path, data):
            if path.name == failed_save:
                raise OSError("Manufactured local save failure")
            return atomic_json(path, data)
        runner = CodexRunner(CONFIG, stop_on_interrupt=True)
        login = subprocess.CompletedProcess([], 0, b"Logged in using ChatGPT", b"")
        with patch("product_radar.research_ai.shutil.which", return_value="/fixture/codex"), \
             patch("product_radar.research_ai.subprocess.run", return_value=login), \
             patch("product_radar.research_ai.subprocess.Popen", side_effect=launch), \
             patch("product_radar.research_ai.atomic_json", side_effect=save), \
             patch("product_radar.research_ai.os.killpg") as kill:
            with self.assertRaises(type(interruption)) as stopped:
                Jobs(self.memory, runner).run("A", PAYLOAD)
        self.assertIs(stopped.exception, interruption)
        self.assertEqual(kill.call_args_list[0].args, (12345, signal.SIGTERM))
        job_id = next(iter(self.memory.snapshot()["jobs"]))
        folder = self.memory.root / "ai_jobs" / job_id
        if failed_save == "failure.json":
            self.assertFalse((folder / "failure.json").exists())
        else:
            self.assertEqual(load_json(folder / "failure.json")["code"], "AI_INTERRUPTED_REVIEW_SAVED_JOB")
        if failed_save == "execution_failure.json" or failed_cleanup:
            self.assertFalse((folder / "execution_failure.json").exists())
        else:
            self.assertIs(load_json(folder / "execution_failure.json")["owned_process_finished"], True)
        self.assertFalse((folder / "result.json").exists())
        if completed:
            self.assertEqual(load_json(folder / "completion.json")["response"], RESPONSE)
            with patch.object(CodexRunner, "__call__", side_effect=AssertionError("No repeated model call")):
                recovered_id, results = Jobs(self.memory, runner).run("A", PAYLOAD)
            self.assertEqual(recovered_id, job_id)
            self.assertIn("fixture-observation", results["accepted"])
        else:
            self.assertFalse((folder / "completion.json").exists())

    def test_cleanup_escalates_term_to_kill_and_waits_for_exact_owned_process(self):
        class Process:
            pid = 54321
            returncode = None
            waits = 0
            def wait(self, timeout):
                self.waits += 1
                if self.waits == 1: raise subprocess.TimeoutExpired("fixture-only", timeout)
                self.returncode = -signal.SIGKILL; return self.returncode
            def poll(self): return self.returncode
        process = Process()
        with patch("product_radar.research_ai.os.killpg") as kill:
            self.assertEqual(_finish_owned_process(process, interrupted=True), -signal.SIGKILL)
        self.assertEqual(process.waits, 2)
        self.assertEqual(kill.call_args_list[0].args, (54321, signal.SIGTERM))
        self.assertEqual(kill.call_args_list[1].args, (54321, signal.SIGKILL))
        self.assertTrue(all(call.args[0] == 54321 for call in kill.call_args_list))


class LoginPreflightTests(unittest.TestCase):
    """Manufactured login responses; never inspect auth or launch Codex."""
    PRIVATE = b"fixture-private-account@example.org fixture-private-token"

    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.runner = CodexRunner(CONFIG)

    def memory(self, suffix):
        return ResearchMemory(self.root / suffix / "research", Journal(self.root / suffix / "finance"), evidence_type="synthetic")

    def failed(self, memory, *, status=None, error=None, expected):
        with patch("product_radar.research_ai.shutil.which", return_value="/fixture/codex"), \
             patch("product_radar.research_ai.subprocess.run", return_value=status, side_effect=error) as login, \
             patch("product_radar.research_ai.subprocess.Popen", side_effect=AssertionError("No model dispatch on a failed preflight")) as model:
            with self.assertRaisesRegex(SafetyError, "^" + expected + "$"):
                Jobs(memory, self.runner).run("A", PAYLOAD)
        self.assertEqual(login.call_count, 1)
        self.assertEqual(login.call_args.args[0], ["/fixture/codex", "login", "status"])
        self.assertEqual(login.call_args.kwargs["timeout"], 10)
        model.assert_not_called()
        job_id = next(iter(memory.snapshot()["jobs"]))
        folder = memory.root / "ai_jobs" / job_id
        receipt = load_json(folder / "runner_preflight.json")
        failure = load_json(folder / "failure.json")
        self.assertEqual(receipt["job_id"], job_id)
        self.assertEqual(receipt["request_hash"], digest(memory.snapshot()["jobs"][job_id]["request"]))
        self.assertEqual(receipt["intent_hash"], digest(load_json(folder / "intent.json")))
        self.assertEqual(receipt["stage"], "login_status")
        self.assertEqual(receipt["code"], expected)
        self.assertEqual(failure["code"], expected)
        self.assertIs(receipt["model_started"], False)
        self.assertIsNone(receipt["usage"])
        self.assertIsNone(failure["usage"])
        self.assertEqual(failure["execution_certainty"], "unknown")
        self.assertFalse((folder / "runner_execution.json").exists())
        self.assertFalse((folder / "execution_failure.json").exists())
        self.assertFalse((folder / "completion.json").exists())
        self.assertFalse((folder / "result.json").exists())
        self.assertNotIn(self.PRIVATE, canonical(receipt))
        self.assertNotIn(self.PRIVATE, canonical(failure))
        self.assertFalse({"stdout", "stderr", "output", "env", "account", "auth_path"} & set(receipt))
        return job_id, folder, receipt

    def test_only_explicit_signed_out_or_api_auth_requires_chatgpt_sign_in(self):
        for n, (returncode, output, expected) in enumerate((
            (1, b"Not logged in\n", "AI_CHATGPT_SIGN_IN_REQUIRED"),
            (0, b"Logged in using API key\n", "AI_CHATGPT_SIGN_IN_REQUIRED"),
            (0, b"Logged in using an API key\n", "AI_CHATGPT_SIGN_IN_REQUIRED"),
            (2, b"unrecognised fixture failure\n", "AI_LOCAL_EXECUTION_FAILED"),
            (0, b"", "AI_LOCAL_EXECUTION_FAILED"),
            (2, b"Logged in using ChatGPT\n", "AI_LOCAL_EXECUTION_FAILED"),
            (1, b"Could not determine whether not logged in\n", "AI_LOCAL_EXECUTION_FAILED"),
        )):
            with self.subTest(returncode=returncode, output=output):
                _, _, receipt = self.failed(self.memory("status-" + str(n)),
                    status=subprocess.CompletedProcess([], returncode, output, self.PRIVATE), expected=expected)
                self.assertEqual(receipt["exit_code"], returncode)
                self.assertIsNone(receipt["exception_type"])

    def test_known_permission_configuration_and_network_failures_are_not_sign_out(self):
        for n, (output, expected) in enumerate((
            (b"permission denied", "AI_LOCAL_PERMISSION_DENIED"),
            (b"read-only file system", "AI_LOCAL_PERMISSION_DENIED"),
            (b"unexpected argument", "AI_CLI_CONFIGURATION_REJECTED"),
            (b"unknown feature", "AI_CLI_CONFIGURATION_REJECTED"),
            (b"failed to connect", "AI_NETWORK_UNAVAILABLE"),
            (b"network is unreachable", "AI_NETWORK_UNAVAILABLE"),
        )):
            with self.subTest(output=output):
                _, _, receipt = self.failed(self.memory("known-" + str(n)),
                    status=subprocess.CompletedProcess([], 3, b"", output + b"\n" + self.PRIVATE), expected=expected)
                self.assertEqual(receipt["exit_code"], 3)

    def test_local_exceptions_keep_stage_and_class_without_private_exception_text(self):
        for n, (error, expected) in enumerate((
            (PermissionError(self.PRIVATE.decode()), "AI_LOCAL_PERMISSION_DENIED"),
            (OSError(self.PRIVATE.decode()), "AI_LOCAL_EXECUTION_FAILED"),
            (subprocess.TimeoutExpired(self.PRIVATE.decode(), 10, output=self.PRIVATE, stderr=self.PRIVATE), "AI_LOCAL_EXECUTION_FAILED"),
        )):
            with self.subTest(kind=type(error).__name__):
                memory = self.memory("exception-" + str(n))
                job_id, _, receipt = self.failed(memory, error=error, expected=expected)
                self.assertIsNone(receipt["exit_code"])
                self.assertEqual(receipt["exception_type"], type(error).__name__)
                if expected == "AI_LOCAL_EXECUTION_FAILED":
                    self.assertEqual(prepare_retry(memory, job_id, "Inspect this bound unstarted local-check failure first."), 1)

    def test_bound_neutral_preflight_allows_one_deliberate_retry_without_changing_originals(self):
        memory = self.memory("neutral-retry")
        job_id, folder, _ = self.failed(memory, status=subprocess.CompletedProcess([], 4, b"", self.PRIVATE), expected="AI_LOCAL_EXECUTION_FAILED")
        originals = {name: (folder / name).read_bytes() for name in ("intent.json", "failure.json", "runner_preflight.json")}
        before = memory.snapshot()["jobs"]
        self.assertEqual(prepare_retry(memory, job_id, "Deliberately retry the exact unstarted fixture."), 1)
        self.assertEqual(memory.snapshot()["jobs"], before)
        self.assertEqual(current_attempt(folder), folder / "retry-1")
        self.assertFalse((folder / "retry-1" / "intent.json").exists())
        for name, raw in originals.items():
            self.assertEqual((folder / name).read_bytes(), raw)
        with self.assertRaises(SafetyError):
            prepare_retry(memory, job_id, "An unexecuted retry is not another failed attempt.")
        self.assertEqual(len(load_json(folder / "retries.json")), 1)
        real_popen = subprocess.Popen
        def fixture_process(command, **kwargs):
            script = "import sys;sys.stdin.buffer.read();sys.stdout.buffer.write(" + repr(EVENTS) + ")"
            return real_popen([sys.executable, "-c", script], **kwargs)
        with patch("product_radar.research_ai.shutil.which", return_value="/fixture/codex"), \
             patch("product_radar.research_ai.subprocess.run", return_value=subprocess.CompletedProcess([], 0, b"Logged in using ChatGPT", b"")) as login, \
             patch("product_radar.research_ai.subprocess.Popen", side_effect=fixture_process) as model:
            recovered_id, result = Jobs(memory, self.runner).run("A", PAYLOAD)
        self.assertEqual(recovered_id, job_id)
        self.assertEqual(login.call_count, 1)
        self.assertEqual(model.call_count, 1)
        self.assertEqual(load_json(folder / "retry-1" / "runner_execution.json")["attempt"], "retry-1")
        self.assertFalse((folder / "retry-1" / "runner_preflight.json").exists())
        for name, raw in originals.items():
            self.assertEqual((folder / name).read_bytes(), raw)
        with patch.object(CodexRunner, "__call__", side_effect=AssertionError("A completed retry is reused")):
            self.assertEqual(Jobs(memory, self.runner).run("A", PAYLOAD), (job_id, result))
        with self.assertRaisesRegex(SafetyError, "AI_COMPLETED_RESULT_REUSE_REQUIRED"):
            prepare_retry(memory, job_id, "A completed retry is not another request.")

    def test_changed_or_contradictory_neutral_preflight_never_authorises_retry(self):
        memory = self.memory("tampered-neutral")
        job_id, folder, receipt = self.failed(memory, status=subprocess.CompletedProcess([], 4, b"", self.PRIVATE), expected="AI_LOCAL_EXECUTION_FAILED")
        changes = [lambda r: r.update(job_id="other-job"), lambda r: r.update(request_hash="f" * 64),
                   lambda r: r.update(intent_hash="f" * 64), lambda r: r.update(execution_id="other-execution"),
                   lambda r: r.update(attempt="retry-9"), lambda r: r.update(runner="other-runner"),
                   lambda r: r.update(stage="model_execution"), lambda r: r.update(code="AI_CHATGPT_SIGN_IN_REQUIRED"),
                   lambda r: r.update(model_started=True), lambda r: r.update(model_started=0),
                   lambda r: r.update(exit_code=True), lambda r: r.update(exit_code=None),
                   lambda r: r.update(exception_type=self.PRIVATE.decode()), lambda r: r.update(usage={"input_tokens": 0}),
                   lambda r: r.update(at="2000-01-01T00:00:00+00:00"), lambda r: r.update(stdout="Do not retain raw login output")]
        for change in changes:
            edited = deepcopy(receipt); change(edited)
            atomic_json(folder / "runner_preflight.json", edited)
            with self.assertRaisesRegex(SafetyError, "AI_RETRY_RECONCILIATION_REQUIRED"):
                prepare_retry(memory, job_id, "An edited fixture receipt is not proof.")
            self.assertFalse((folder / "retries.json").exists())
        atomic_json(folder / "runner_preflight.json", receipt)
        for name in ("runner_execution.json", "execution_failure.json"):
            atomic_json(folder / name, {"synthetic": "contradicts an unstarted model"})
            with self.assertRaisesRegex(SafetyError, "AI_RETRY_RECONCILIATION_REQUIRED"):
                prepare_retry(memory, job_id, "Model execution cannot be waved away.")
            (folder / name).unlink()
        (folder / "runner_preflight.json").unlink()
        with self.assertRaisesRegex(SafetyError, "AI_RETRY_RECONCILIATION_REQUIRED"):
            prepare_retry(memory, job_id, "Old generic execution failures do not acquire a new retry route.")
        self.assertFalse((folder / "runner_preflight.json").exists())

    def test_successful_stdout_or_stderr_preserves_run_and_saved_replay_skips_login(self):
        real_popen = subprocess.Popen
        for n, (stdout, stderr) in enumerate(((b"Logged in using ChatGPT", b""), (b"", b"Logged in using ChatGPT"))):
            memory = self.memory("success-" + str(n))
            commands = []
            def fixture_process(command, **kwargs):
                commands.append(command)
                self.assertTrue(kwargs["start_new_session"])
                script = "import sys;sys.stdin.buffer.read();sys.stdout.buffer.write(" + repr(EVENTS) + ")"
                return real_popen([sys.executable, "-c", script], **kwargs)
            with patch("product_radar.research_ai.shutil.which", return_value="/fixture/codex"), \
                 patch("product_radar.research_ai.subprocess.run", return_value=subprocess.CompletedProcess([], 0, stdout, stderr)) as login, \
                 patch("product_radar.research_ai.subprocess.Popen", side_effect=fixture_process):
                job_id, result = Jobs(memory, self.runner).run("A", PAYLOAD)
            self.assertEqual(login.call_count, 1)
            self.assertEqual(len(commands), 1)
            self.assertEqual(commands[0][commands[0].index("--model") + 1], MODEL)
            self.assertEqual(commands[0][commands[0].index("--sandbox") + 1], "read-only")
            self.assertIn("--ignore-user-config", commands[0])
            self.assertIn("shell_tool", commands[0])
            self.assertIn("approval_policy=\"never\"", commands[0])
            folder = memory.root / "ai_jobs" / job_id
            self.assertFalse((folder / "runner_preflight.json").exists())
            self.assertTrue((folder / "runner_execution.json").exists())
            with patch("product_radar.research_ai.subprocess.run", side_effect=AssertionError("Saved results do not recheck login")), \
                 patch("product_radar.research_ai.subprocess.Popen", side_effect=AssertionError("Saved results do not redispatch")):
                self.assertEqual(Jobs(memory, self.runner).run("A", PAYLOAD), (job_id, result))


if __name__ == "__main__":
    unittest.main()
