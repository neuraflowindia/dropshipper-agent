"""Actual owned local-process failures; no Codex, model or provider requests."""

from copy import deepcopy
import subprocess
import sys
import unittest
from unittest.mock import patch

import test_functional_definition_review as fixtures
from product_radar.functional_definition_review import (
    MODE, functional_definition_readiness, review_functional_definitions)
from product_radar.models import SafetyError
from product_radar.research_ai import (
    CodexRunner, Jobs, MODEL, bind_model_retry_leader, closed_model_retry_hint, current_attempt, prepare_closed_model_retry,
    prepared_closed_model_retry_hint)
from product_radar.storage import Journal, atomic_json, load_json
from product_radar.work_turns import bind_work_turn, close_work_turn, open_work_turn

AFTER = "2026-10-05T10:05:00+00:00"
BEFORE = "2026-10-05T10:04:59+00:00"
NOW = fixtures.NOW


class SuccessRunner(fixtures.Runner):
    identity = "codex-chatgpt-high-v1"


class ClosedModelRetryTests(unittest.TestCase):
    def setUp(self):
        fixture = fixtures.FunctionalDefinitionReviewTests(); fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        self.fixture, self.memory, self.cid = fixture, fixture.memory, fixture.cids[0]
        self.processes = []

    def fail_closed(self, diagnostic="HTTP 429 Too Many Requests", *, ids=None):
        runner = CodexRunner({"model": MODEL, "reasoning_effort": "high",
                              "authentication": "chatgpt", "paid_fallback": False})
        real_popen = subprocess.Popen
        script = "import sys;sys.stdin.buffer.read();sys.stderr.write(" + repr(diagnostic) + ");sys.exit(1)"
        def local_process(command, **kwargs):
            self.assertEqual(command[0], "/fixture/codex")
            process = real_popen([sys.executable, "-c", script], **kwargs)
            self.processes.append(process)
            return process
        login = subprocess.CompletedProcess([], 0, b"Logged in using ChatGPT", b"")
        with patch("product_radar.research_ai.shutil.which", return_value="/fixture/codex"), \
             patch("product_radar.research_ai.subprocess.run", return_value=login), \
             patch("product_radar.research_ai.subprocess.Popen", side_effect=local_process):
            result = review_functional_definitions(self.memory, ids or [self.cid], runner, as_of=NOW)
        job_id = next(jid for jid, row in self.memory.snapshot()["jobs"].items()
                      if row["request"]["payload"].get("mode") == MODE)
        return job_id, self.memory.root / "ai_jobs" / job_id, result

    def hint(self, job_id, **kwargs):
        return closed_model_retry_hint(self.memory, job_id, as_of=kwargs.pop("as_of", AFTER), **kwargs)

    def test_supplied_verified_state_avoids_nested_history_reads_in_retry_hints(self):
        job_id, _, _ = self.fail_closed()
        expected = self.hint(job_id)
        state = self.memory.snapshot()
        with patch.object(self.memory, "snapshot", side_effect=AssertionError("No nested history replay")):
            self.assertEqual(closed_model_retry_hint(self.memory, job_id, as_of=AFTER, state=state), expected)
            ready = functional_definition_readiness(self.memory, [self.cid], as_of=AFTER, state=state)
        self.assertEqual(ready["model_ready_concept_ids"], [self.cid])

    def test_checked_pointer_hint_reuses_state_but_still_checks_late_original_files(self):
        job_id, base, _ = self.fail_closed()
        hint = self.hint(job_id)
        prepare_closed_model_retry(self.memory, job_id, as_of=AFTER, expected_signal=hint["signal"])
        state = self.memory.snapshot()
        with patch.object(self.memory, "snapshot", side_effect=AssertionError("No nested history replay")):
            ready = prepared_closed_model_retry_hint(self.memory, job_id, as_of=AFTER, state=state)
        self.assertTrue(ready["ready"])
        response, usage = SuccessRunner()("B", state["jobs"][job_id]["request"]["payload"], None)
        atomic_json(base / "completion.json", {"response": response, "usage": usage, "exit_code": 0})
        with patch.object(self.memory, "snapshot", side_effect=AssertionError("No nested history replay")):
            late = prepared_closed_model_retry_hint(self.memory, job_id, as_of=AFTER, state=state)
        self.assertFalse(late["ready"])
        self.assertEqual(late["reason"], "AI_COMPLETED_RESULT_REUSE_REQUIRED")

    def test_whole_programme_readiness_uses_one_supplied_verified_history(self):
        from product_radar.programme import prepare_programme, programme_turn_readiness
        job_id, _, _ = self.fail_closed()
        record = prepare_programme(self.memory, "single-history-readiness", concept_ids=[self.cid], as_of=NOW)
        before = self.memory.snapshot_with_provenance()
        with patch.object(self.memory, "snapshot", side_effect=AssertionError("No nested history replay")):
            ready = programme_turn_readiness(self.memory, record, root=self.memory.root.parent,
                runner=SuccessRunner(), as_of=AFTER, state=before["state"])
        self.assertTrue(ready["ready"])
        after = self.memory.snapshot_with_provenance()
        self.assertEqual((after["head"], after["event_count"]), (before["head"], before["event_count"]))
        self.assertIn(job_id, after["state"]["jobs"])

    def test_actual_closed_rate_limit_waits_then_retries_same_job_once_and_reuses_answer(self):
        job_id, base, first = self.fail_closed()
        self.assertEqual(first["failures"][self.cid]["reason"], "AI_RATE_LIMITED")
        originals = {p.name: p.read_bytes() for p in base.iterdir()}
        hint = self.hint(job_id, as_of=BEFORE)
        self.assertFalse(hint["ready"]); self.assertEqual(hint["next_retry_at"], AFTER)
        waiting = functional_definition_readiness(self.memory, [self.cid], as_of=BEFORE)
        self.assertFalse(waiting["ready"]); self.assertEqual(waiting["waits"], [AFTER])
        ready = functional_definition_readiness(self.memory, [self.cid], as_of=AFTER,
            skip_concept_bindings=waiting["bindings"])
        self.assertEqual(ready["model_ready_concept_ids"], [self.cid])
        runner = SuccessRunner()
        result = review_functional_definitions(self.memory, [self.cid], runner, as_of=AFTER)
        self.assertIn(self.cid, result["accepted"]); self.assertEqual(len(runner.calls), 1)
        self.assertEqual(result["accepted"][self.cid]["job_id"], job_id)
        self.assertEqual(current_attempt(base), base / "retry-1")
        self.assertEqual(load_json(base / "retries.json")[0]["previous_failure"], "AI_RATE_LIMITED")
        for name, raw in originals.items(): self.assertEqual((base / name).read_bytes(), raw)
        review_functional_definitions(self.memory, [self.cid], runner, as_of=AFTER)
        self.assertEqual(len(runner.calls), 1)

    def test_actual_closed_network_failure_respects_configured_and_programme_deadlines(self):
        job_id, _, _ = self.fail_closed("Failed to connect: network is unreachable")
        later = "2026-10-05T12:00:00+00:00"
        hint = self.hint(job_id, retry_seconds=3600, not_before=later)
        self.assertEqual(hint["failure_code"], "AI_NETWORK_UNAVAILABLE")
        self.assertFalse(hint["ready"]); self.assertEqual(hint["next_retry_at"], later)
        self.assertTrue(self.hint(job_id, as_of=later, retry_seconds=3600, not_before=later)["ready"])

    def test_generic_jobs_defaults_still_do_not_retry_a_closed_original(self):
        job_id, _, _ = self.fail_closed()
        request = self.memory.snapshot()["jobs"][job_id]["request"]
        self.assertTrue(self.hint(job_id)["ready"])
        runner = SuccessRunner()
        with self.assertRaisesRegex(SafetyError, "AI_JOB_PENDING_RECONCILIATION"):
            Jobs(self.memory, runner).run("B", request["payload"], declared_version=request["prompt_version"])
        self.assertEqual(runner.calls, [])

    def test_unknown_rate_limit_without_terminal_proof_does_not_retry(self):
        class Unknown(SuccessRunner):
            def __call__(self, *args):
                self.calls.append(args)
                raise SafetyError("AI_RATE_LIMITED")
        runner = Unknown()
        review_functional_definitions(self.memory, [self.cid], runner, as_of=NOW)
        result = review_functional_definitions(self.memory, [self.cid], runner, as_of=AFTER)
        self.assertFalse(result["accepted"]); self.assertEqual(len(runner.calls), 1)
        self.assertEqual(result["failures"][self.cid]["reason"], "AI_RETRY_RECONCILIATION_REQUIRED")

    def assert_not_automatic(self, diagnostic):
        job_id, base, _ = self.fail_closed(diagnostic)
        self.assertFalse(self.hint(job_id)["ready"])
        self.assertFalse((base / "retries.json").exists())

    def test_setup_failures_are_not_automatic(self): self.assert_not_automatic("Model not found")
    def test_subscription_failures_are_not_automatic(self): self.assert_not_automatic("Usage limit reached")
    def test_generic_provider_failures_are_not_automatic(self): self.assert_not_automatic("Unclassified local wrapper failure")

    # Money-guard refusals after the intent (the watch changed between the
    # pre-intent check and the call): proven never started, retried when due.

    def refused_by_guard(self, code="AI_PAID_CREDITS_USED"):
        class Refused(SuccessRunner):
            def check_new_execution(self):
                pass  # the pre-intent check still passed

            def __call__(self, *args):
                self.calls.append(args)
                raise SafetyError(code)  # the call's own fresh admission refuses before the model
        result = review_functional_definitions(self.memory, [self.cid], Refused(), as_of=NOW)
        job_id = next(jid for jid, row in self.memory.snapshot()["jobs"].items()
                      if row["request"]["payload"].get("mode") == MODE)
        return job_id, self.memory.root / "ai_jobs" / job_id, result

    def test_money_guard_refusal_after_intent_retries_the_same_job_once_due(self):
        for code in ("AI_PLAN_LIMIT_CREDITS_PROTECTED", "AI_USAGE_READ_FAILED", "AI_SPEND_WATCH_UNAVAILABLE"):
            with self.subTest(code=code):
                fresh = ClosedModelRetryTests(self._testMethodName); fresh.setUp(); self.addCleanup(fresh.doCleanups)
                job_id, _, _ = fresh.refused_by_guard(code)
                self.assertEqual((fresh.hint(job_id)["ready"], fresh.hint(job_id)["failure_code"]), (True, code))
        job_id, base, first = self.refused_by_guard()
        self.assertEqual(first["failures"][self.cid]["reason"], "AI_PAID_CREDITS_USED")
        # The refused call's own accounting (R18a) says the model never started.
        self.assertEqual(sorted(path.name for path in base.iterdir()), ["accounting.json", "failure.json", "intent.json"])
        accounting = load_json(base / "accounting.json")
        self.assertEqual((accounting["outcome"], accounting["model"], accounting["tokens"]),
                         ("AI_PAID_CREDITS_USED", None, None))
        originals = {path.name: path.read_bytes() for path in base.iterdir()}
        hint = self.hint(job_id, as_of=BEFORE)
        self.assertEqual((hint["ready"], hint["next_retry_at"], hint["failure_code"]), (False, AFTER, "AI_PAID_CREDITS_USED"))
        self.assertEqual(set(self.hint(job_id)["original_proof"]["original_hashes"]), {"intent.json", "failure.json"})
        runner = SuccessRunner()
        result = review_functional_definitions(self.memory, [self.cid], runner, as_of=AFTER)
        self.assertIn(self.cid, result["accepted"]); self.assertEqual(len(runner.calls), 1)
        self.assertEqual(result["accepted"][self.cid]["job_id"], job_id)
        self.assertEqual(current_attempt(base), base / "retry-1")
        retries = load_json(base / "retries.json")
        self.assertEqual((len(retries), retries[0]["closed_retry_proof"]["failure_code"]), (1, "AI_PAID_CREDITS_USED"))
        for name, raw in originals.items(): self.assertEqual((base / name).read_bytes(), raw)

    def test_a_money_wait_still_on_at_the_retry_starts_nothing_and_spends_no_retry(self):
        job_id, base, _ = self.refused_by_guard()

        class Waiting(SuccessRunner):
            def check_new_execution(self):
                raise SafetyError("AI_PAID_CREDITS_USED")
        waiting = Waiting()
        for _ in range(2):
            result = review_functional_definitions(self.memory, [self.cid], waiting, as_of=AFTER)
            self.assertEqual(result["failures"][self.cid]["reason"], "AI_PAID_CREDITS_USED")
        self.assertEqual(waiting.calls, [])
        self.assertEqual(len(load_json(base / "retries.json")), 1)
        self.assertEqual(list((base / "retry-1").iterdir()) if (base / "retry-1").exists() else [], [])
        runner = SuccessRunner()
        result = review_functional_definitions(self.memory, [self.cid], runner, as_of=AFTER)
        self.assertIn(self.cid, result["accepted"]); self.assertEqual(len(runner.calls), 1)
        self.assertEqual(len(load_json(base / "retries.json")), 1)

    def test_a_guard_code_beside_any_runner_file_is_not_proof_of_an_unstarted_model(self):
        job_id, base, _ = self.refused_by_guard()
        for name in ("runner_preflight.json", "runner_execution.json", "execution_failure.json"):
            with self.subTest(name=name):
                atomic_json(base / name, {"fixture": True})
                with self.assertRaisesRegex(SafetyError, "AI_RETRY_RECONCILIATION_REQUIRED"):
                    self.hint(job_id)
                (base / name).unlink()
        failure = load_json(base / "failure.json")
        for changed in (dict(failure, execution_certainty="runner_returned"), dict(failure, at="2026-10-05T09:00:00+00:00")):
            with self.subTest(changed=changed):
                atomic_json(base / "failure.json", changed)
                with self.assertRaisesRegex(SafetyError, "AI_RETRY_RECONCILIATION_REQUIRED"):
                    self.hint(job_id)
        atomic_json(base / "failure.json", failure)
        self.assertTrue(self.hint(job_id)["ready"])

    def test_jobs_runs_the_runners_own_guard_before_any_new_intent(self):
        from product_radar.functional_definition_review import CONTRACT, PROMPT_VERSION, definition_review_input
        payload = {"mode": MODE, "definition_contract": CONTRACT,
                   "items": [definition_review_input(self.memory.snapshot(), self.cid, as_of=NOW)["item"]]}
        seen = []

        class Guarded(SuccessRunner):
            def check_new_execution(self):
                seen.append("runner")
                raise SafetyError("AI_PAID_CREDITS_USED")

        def caller(request, folder):
            seen.append("caller")
            self.assertFalse((folder / "intent.json").exists())
        runner = Guarded(); intents = set(self.memory.root.glob("ai_jobs/*/intent.json"))
        for supplied, expected in ((None, ["runner"]), (caller, ["runner", "caller", "runner"])):
            with self.assertRaisesRegex(SafetyError, "AI_PAID_CREDITS_USED"):
                Jobs(self.memory, runner, before_new_execution=supplied).run("B", payload, declared_version=PROMPT_VERSION)
            self.assertEqual(seen, expected)  # the caller's check first, then the runner's own
        self.assertEqual(runner.calls, [])
        self.assertEqual(set(self.memory.root.glob("ai_jobs/*/intent.json")), intents)  # no new intent saved
        with self.assertRaisesRegex(SafetyError, "AI_EXECUTION_GUARD_INVALID"):
            Jobs(self.memory, type("Broken", (), {"check_new_execution": "not-callable"})())

    def test_forged_terminal_proof_and_changed_files_cannot_prepare_retry(self):
        job_id, base, _ = self.fail_closed()
        original = load_json(base / "execution_failure.json")
        for field, value in (("owned_group_cleanup_confirmed", False), ("owned_process_finished", False),
                             ("job_id", "wrong-job"), ("request_hash", "0" * 64), ("code", "AI_NETWORK_UNAVAILABLE"),
                             ("exit_code", 0)):
            with self.subTest(field=field):
                bad = deepcopy(original); bad[field] = value
                atomic_json(base / "execution_failure.json", bad)
                with self.assertRaisesRegex(SafetyError, "AI_RETRY_RECONCILIATION_REQUIRED"):
                    self.hint(job_id)
        atomic_json(base / "execution_failure.json", original)
        hint = self.hint(job_id)
        atomic_json(base / "execution_failure.json", original)  # Same content, different exact original file.
        with self.assertRaisesRegex(SafetyError, "AI_RETRY_ORIGINAL_CHANGED"):
            prepare_closed_model_retry(self.memory, job_id, as_of=AFTER, expected_signal=hint["signal"])
        self.assertFalse((base / "retries.json").exists())

    def test_late_completed_answer_prevents_preparing_even_a_previously_due_retry(self):
        job_id, base, _ = self.fail_closed(); hint = self.hint(job_id)
        response, usage = SuccessRunner()("B", self.memory.snapshot()["jobs"][job_id]["request"]["payload"], None)
        atomic_json(base / "completion.json", {"response": response, "usage": usage, "exit_code": 0})
        self.assertEqual(self.hint(job_id)["reason"], "AI_COMPLETED_RESULT_REUSE_REQUIRED")
        with self.assertRaisesRegex(SafetyError, "AI_RETRY_ORIGINAL_CHANGED"):
            prepare_closed_model_retry(self.memory, job_id, as_of=AFTER, expected_signal=hint["signal"])
        runner = SuccessRunner()
        result = review_functional_definitions(self.memory, [self.cid], runner, as_of=AFTER)
        self.assertIn(self.cid, result["accepted"]); self.assertEqual(runner.calls, [])
        self.assertFalse((base / "retries.json").exists())

    def test_wrong_runner_and_changed_original_batch_do_not_even_prepare_retry(self):
        job_id, base, _ = self.fail_closed(ids=self.fixture.cids)
        wrong = fixtures.Runner()
        result = review_functional_definitions(self.memory, [self.cid], wrong, as_of=AFTER)
        self.assertEqual(result["failures"][self.cid]["reason"], "AI_RUNNER_NOT_ENABLED")
        self.assertFalse((base / "retries.json").exists())
        other = self.fixture.cids[1]
        original = self.memory.snapshot()["concepts"][other]
        self.memory.put("concepts", other, dict(original, status="retired"), previous=original)
        result = review_functional_definitions(self.memory, [self.cid], SuccessRunner(), as_of=AFTER)
        self.assertEqual(result["failures"][self.cid]["reason"], "FUNCTIONAL_DEFINITION_ORIGINAL_BATCH_INPUT_CHANGED")
        self.assertFalse((base / "retries.json").exists())

    def test_actual_retry_execution_consumes_the_current_turn_not_saved_application(self):
        first_turn = open_work_turn(self.memory, "closed-retry-programme", at=NOW)
        with bind_work_turn(self.memory, first_turn["id"]):
            job_id, _, _ = self.fail_closed()
        first = self.memory.snapshot()["batch_runs"][first_turn["id"]]
        self.assertEqual(first["counters"]["model_executions"], 1)
        close_work_turn(self.memory, first_turn["id"], at=AFTER, reason="Synthetic bounded first pass ended.")
        next_turn = open_work_turn(self.memory, "closed-retry-programme", at=AFTER)
        runner = SuccessRunner()
        with bind_work_turn(self.memory, next_turn["id"]):
            result = review_functional_definitions(self.memory, [self.cid], runner, as_of=AFTER)
        self.assertIn(self.cid, result["accepted"])
        second = self.memory.snapshot()["batch_runs"][next_turn["id"]]
        self.assertEqual(second["counters"]["model_executions"], 1)
        self.assertEqual(second["counters"]["origin_admissions"], 1)
        self.assertEqual({row["attempt"] for row in second["reservations"]["model"].values()}, {"retry-1"})
        self.assertEqual({row["job_id"] for row in second["reservations"]["model"].values()}, {job_id})
        close_work_turn(self.memory, next_turn["id"], at=AFTER, reason="Synthetic retry applied.")
        reuse_turn = open_work_turn(self.memory, "closed-retry-programme", at=AFTER)
        with bind_work_turn(self.memory, reuse_turn["id"]):
            review_functional_definitions(self.memory, [self.cid], runner, as_of=AFTER)
        self.assertEqual(self.memory.snapshot()["batch_runs"][reuse_turn["id"]]["counters"]["model_executions"], 0)
        self.assertEqual(len(runner.calls), 1)

    def test_existing_twenty_retry_limit_is_preserved(self):
        from product_radar.research_ai import prepare_retry
        job_id, base, _ = self.fail_closed()
        for attempt in range(1, 21):
            self.assertEqual(prepare_retry(self.memory, job_id, "Synthetic explicit closed-process retry."), attempt)
            self.fail_closed()
        hint = self.hint(job_id)
        self.assertFalse(hint["ready"]); self.assertEqual(hint["reason"], "AI_RETRY_RECORD_LIMIT")
        with self.assertRaisesRegex(SafetyError, "AI_RETRY_RECORD_LIMIT"):
            prepare_closed_model_retry(self.memory, job_id, as_of=AFTER, expected_signal=hint["signal"])
        self.assertEqual(len(load_json(base / "retries.json")), 20)
        self.assertEqual(len(self.processes), 21)

    def test_exact_already_held_root_leader_allows_checked_pointer_without_reacquiring_it(self):
        job_id, base, _ = self.fail_closed(); hint = self.hint(job_id)
        with Journal(self.memory.root).locked() as leader, bind_model_retry_leader(self.memory, leader):
            self.assertEqual(prepare_closed_model_retry(self.memory, job_id, as_of=AFTER,
                expected_signal=hint["signal"]), 1)
            self.assertTrue(leader._locked)
            with self.assertRaisesRegex(SafetyError, "RUNNER_ALREADY_ACTIVE"):
                with Journal(self.memory.root).locked(): pass
        self.assertEqual(current_attempt(base), base / "retry-1")

    def test_unlocked_foreign_or_expired_root_leader_cannot_bypass_exclusion(self):
        job_id, base, _ = self.fail_closed(); hint = self.hint(job_id)
        with self.assertRaisesRegex(SafetyError, "AI_RETRY_LEADER_INVALID"):
            with bind_model_retry_leader(self.memory, Journal(self.memory.root)): pass
        with Journal(self.memory.root / "unrelated").locked() as foreign:
            with self.assertRaisesRegex(SafetyError, "AI_RETRY_LEADER_INVALID"):
                with bind_model_retry_leader(self.memory, foreign): pass
        with Journal(self.memory.root).locked() as leader:
            binding = bind_model_retry_leader(self.memory, leader); binding.__enter__()
        try:
            with self.assertRaisesRegex(SafetyError, "AI_RETRY_LEADER_INVALID"):
                prepare_closed_model_retry(self.memory, job_id, as_of=AFTER, expected_signal=hint["signal"])
        finally: binding.__exit__(None, None, None)
        self.assertFalse((base / "retries.json").exists())

    def test_copied_context_cannot_lend_another_threads_held_leader(self):
        from contextvars import copy_context
        from threading import Thread
        job_id, base, _ = self.fail_closed(); hint = self.hint(job_id); failures = []
        def other_thread():
            try: prepare_closed_model_retry(self.memory, job_id, as_of=AFTER, expected_signal=hint["signal"])
            except SafetyError as error: failures.append(str(error))
        with Journal(self.memory.root).locked() as leader, bind_model_retry_leader(self.memory, leader):
            copied = copy_context(); thread = Thread(target=lambda: copied.run(other_thread))
            thread.start(); thread.join(timeout=2)
            self.assertFalse(thread.is_alive())
        self.assertEqual(failures, ["AI_RETRY_LEADER_INVALID"])
        self.assertFalse((base / "retries.json").exists())

    def test_checked_empty_pointer_survives_restart_without_preparing_a_second_attempt(self):
        from product_radar.research_memory import ResearchMemory
        job_id, base, _ = self.fail_closed(); hint = self.hint(job_id)
        prepare_closed_model_retry(self.memory, job_id, as_of=AFTER, expected_signal=hint["signal"])
        restarted = ResearchMemory(self.memory.root, self.memory.journal, evidence_type="synthetic")
        before = (base / "retries.json").read_bytes()
        prepared = prepared_closed_model_retry_hint(restarted, job_id, as_of=AFTER)
        self.assertTrue(prepared["ready"]); self.assertEqual(prepared["attempt"], "retry-1")
        self.assertEqual(prepared["previous_attempt"], "initial")
        self.assertEqual((base / "retries.json").read_bytes(), before)
        self.assertFalse(self.hint(job_id)["ready"])
        waiting = prepared_closed_model_retry_hint(restarted, job_id, as_of=AFTER,
            not_before="2026-10-05T11:00:00+00:00")
        self.assertFalse(waiting["ready"]); self.assertEqual(waiting["next_retry_at"], "2026-10-05T11:00:00+00:00")

    def test_pointer_reason_or_resealed_borrowed_hash_cannot_replace_actual_previous_closure(self):
        from product_radar.models import digest
        job_id, base, _ = self.fail_closed(); hint = self.hint(job_id)
        prepare_closed_model_retry(self.memory, job_id, as_of=AFTER, expected_signal=hint["signal"])
        retries = load_json(base / "retries.json"); original = deepcopy(retries)
        retries[-1].pop("closed_retry_proof")
        atomic_json(base / "retries.json", retries)
        self.assertFalse(prepared_closed_model_retry_hint(self.memory, job_id, as_of=AFTER)["ready"])
        bad = deepcopy(original)
        proof = bad[-1]["closed_retry_proof"]; proof["original_hashes"]["execution_failure.json"] = "0" * 64
        proof["record_hash"] = digest({name: value for name, value in proof.items() if name != "record_hash"})
        atomic_json(base / "retries.json", bad)
        with self.assertRaisesRegex(SafetyError, "AI_RETRY_ORIGINAL_CHANGED"):
            prepared_closed_model_retry_hint(self.memory, job_id, as_of=AFTER)

    def test_late_previous_answer_prevents_running_an_already_prepared_pointer(self):
        job_id, base, _ = self.fail_closed(); hint = self.hint(job_id)
        prepare_closed_model_retry(self.memory, job_id, as_of=AFTER, expected_signal=hint["signal"])
        response, usage = SuccessRunner()("B", self.memory.snapshot()["jobs"][job_id]["request"]["payload"], None)
        atomic_json(base / "completion.json", {"response": response, "usage": usage, "exit_code": 0})
        prepared = prepared_closed_model_retry_hint(self.memory, job_id, as_of=AFTER)
        self.assertFalse(prepared["ready"]); self.assertEqual(prepared["reason"], "AI_COMPLETED_RESULT_REUSE_REQUIRED")
        retries = (base / "retries.json").read_bytes()
        runner = SuccessRunner(); turn = open_work_turn(self.memory, "late-original-answer", at=AFTER)
        with bind_work_turn(self.memory, turn["id"]):
            result = review_functional_definitions(self.memory, [self.cid], runner, as_of=AFTER)
        self.assertIn(self.cid, result["accepted"]); self.assertFalse(runner.calls)
        self.assertEqual(current_attempt(base), base)
        accepted_definition = self.memory.snapshot()["source_checks"][result["accepted"][self.cid]["definition_id"]]
        self.assertEqual(accepted_definition["job_proof"]["definition_acceptance"]["attempt"], "initial")
        self.assertEqual(self.memory.snapshot()["batch_runs"][turn["id"]]["counters"]["model_executions"], 0)
        self.assertEqual((base / "retries.json").read_bytes(), retries)
        self.assertFalse((base / "retry-1" / "intent.json").exists())

    def test_late_answer_cannot_bypass_manual_pointer_or_started_checked_retry(self):
        from product_radar.models import digest
        from product_radar.research_ai import prepare_retry
        job_id, base, _ = self.fail_closed(); hint = self.hint(job_id)
        prepare_closed_model_retry(self.memory, job_id, as_of=AFTER, expected_signal=hint["signal"])
        request = self.memory.snapshot()["jobs"][job_id]["request"]
        response, usage = SuccessRunner()("B", request["payload"], None)
        atomic_json(base / "result.json", {"request_hash": digest(request), "response": response,
            "usage": usage, "completed_at": AFTER})
        self.assertEqual(current_attempt(base), base)
        atomic_json(base / "retry-1" / "intent.json", {"request": request, "created_at": AFTER})
        self.assertEqual(current_attempt(base), base / "retry-1")
        # A manual pointer carries no automatic original-recovery authority.
        old = load_json(base / "retries.json"); manual = deepcopy(old)
        manual[-1].pop("closed_retry_proof"); atomic_json(base / "retries.json", manual)
        (base / "retry-1" / "intent.json").unlink()
        self.assertEqual(current_attempt(base), base / "retry-1")

    def test_late_answer_changed_original_or_binding_cannot_yield_to_previous_attempt(self):
        from product_radar.models import digest
        job_id, base, _ = self.fail_closed(); hint = self.hint(job_id)
        prepare_closed_model_retry(self.memory, job_id, as_of=AFTER, expected_signal=hint["signal"])
        request = self.memory.snapshot()["jobs"][job_id]["request"]
        response, usage = SuccessRunner()("B", request["payload"], None)
        atomic_json(base / "result.json", {"request_hash": digest(request), "response": response,
            "usage": usage, "completed_at": AFTER})
        retries = load_json(base / "retries.json"); bad = deepcopy(retries)
        proof = bad[-1]["closed_retry_proof"]; proof["binding_hash"] = "0" * 64
        proof["record_hash"] = digest({k: v for k, v in proof.items() if k != "record_hash"})
        atomic_json(base / "retries.json", bad)
        with self.assertRaisesRegex(SafetyError, "AI_RETRY_RECONCILIATION_REQUIRED"): current_attempt(base)
        atomic_json(base / "retries.json", retries)
        original = load_json(base / "execution_failure.json")
        atomic_json(base / "execution_failure.json", dict(original, owned_process_finished=False))
        with self.assertRaisesRegex(SafetyError, "AI_RETRY_ORIGINAL_CHANGED"): current_attempt(base)

    def test_definition_checked_pointer_obeys_changed_deadline_and_resumes_without_second_prepare(self):
        job_id, base, _ = self.fail_closed(); hint = self.hint(job_id)
        prepare_closed_model_retry(self.memory, job_id, as_of=AFTER, expected_signal=hint["signal"])
        deadline = "2026-10-05T11:00:00+00:00"
        waiting = functional_definition_readiness(self.memory, [self.cid], as_of=AFTER,
            retry_seconds=3600, retry_not_before=deadline)
        self.assertFalse(waiting["ready"]); self.assertEqual(waiting["waits"], [deadline])
        self.assertFalse(waiting["retry_hints"][job_id]["requires_prepare"])
        runner = SuccessRunner()
        result = review_functional_definitions(self.memory, [self.cid], runner, as_of=deadline,
            retry_seconds=3600, retry_not_before=deadline, skip_concept_bindings=waiting["bindings"])
        self.assertIn(self.cid, result["accepted"]); self.assertEqual(len(runner.calls), 1)
        self.assertEqual(len(load_json(base / "retries.json")), 1)

    def test_late_retry_one_answer_preserves_that_exact_original_attempt(self):
        from product_radar.research_ai import prepare_retry
        job_id, base, _ = self.fail_closed()
        prepare_retry(self.memory, job_id, "Synthetic deliberate same-original retry.")
        self.fail_closed(); hint = self.hint(job_id)
        self.assertEqual(hint["attempt"], "retry-1")
        prepare_closed_model_retry(self.memory, job_id, as_of=AFTER, expected_signal=hint["signal"])
        response, usage = SuccessRunner()("B", self.memory.snapshot()["jobs"][job_id]["request"]["payload"], None)
        atomic_json(base / "retry-1" / "completion.json", {"response": response, "usage": usage, "exit_code": 0})
        self.assertEqual(current_attempt(base), base / "retry-1")
        runner = SuccessRunner(); result = review_functional_definitions(self.memory, [self.cid], runner, as_of=AFTER)
        self.assertIn(self.cid, result["accepted"]); self.assertFalse(runner.calls)
        record = self.memory.snapshot()["source_checks"][result["accepted"][self.cid]["definition_id"]]
        self.assertEqual(record["job_proof"]["definition_acceptance"]["attempt"], "retry-1")
        self.assertEqual(len(load_json(base / "retries.json")), 2)
        self.assertFalse((base / "retry-2" / "intent.json").exists())

    def test_current_retry_start_during_final_original_check_refuses_old_folder_selection(self):
        import product_radar.research_ai as module
        job_id, base, _ = self.fail_closed(); hint = self.hint(job_id)
        prepare_closed_model_retry(self.memory, job_id, as_of=AFTER, expected_signal=hint["signal"])
        request = self.memory.snapshot()["jobs"][job_id]["request"]
        response, usage = SuccessRunner()("B", request["payload"], None)
        atomic_json(base / "completion.json", {"response": response, "usage": usage, "exit_code": 0})
        original = module._checked_retry_original; checks = []
        def started_after_check(*args, **kwargs):
            result = original(*args, **kwargs); checks.append(result)
            if len(checks) == 2:
                atomic_json(base / "retry-1" / "intent.json", {"request": request, "created_at": AFTER})
            return result
        with patch.object(module, "_checked_retry_original", side_effect=started_after_check):
            with self.assertRaisesRegex(SafetyError, "AI_RETRY_ORIGINAL_CHANGED"): current_attempt(base)
        self.assertEqual(current_attempt(base), base / "retry-1")
        (base / "retry-1" / "intent.json").unlink(); checks.clear()
        def changed_after_check(*args, **kwargs):
            result = original(*args, **kwargs); checks.append(result)
            if len(checks) == 2:
                receipt = load_json(base / "execution_failure.json")
                atomic_json(base / "execution_failure.json", dict(receipt, owned_process_finished=False))
            return result
        with patch.object(module, "_checked_retry_original", side_effect=changed_after_check):
            with self.assertRaisesRegex(SafetyError, "AI_RETRY_ORIGINAL_CHANGED"): current_attempt(base)


if __name__ == "__main__": unittest.main()
