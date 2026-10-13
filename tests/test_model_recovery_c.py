"""Actual C producers after an owned local429; no model/provider calls."""

from copy import deepcopy
from datetime import timedelta
import subprocess
import sys
import unittest
from unittest.mock import patch

import test_product_queries as query_fixture
import test_catalogue_seed as seed_fixture
import test_catalogue_expansion as expansion_fixture
from product_radar.catalogue_expansion import run_expansion
from product_radar.catalogue_seed import build_catalogue_branch
from product_radar.models import SafetyError, timestamp
from product_radar.product_queries import current_query_profile, ensure_query_profiles
from product_radar.research_ai import CodexRunner, MODEL, closed_model_retry_hint, current_attempt, prepare_closed_model_retry
from product_radar.storage import atomic_json, load_json
from product_radar.work_turns import bind_work_turn, open_work_turn


class ModelRecoveryCProducersTests(unittest.TestCase):
    def setup_fixture(self, fixture):
        fixture.setUp(); self.addCleanup(fixture.doCleanups)
        if isinstance(fixture, expansion_fixture.ExpansionTests): self.addCleanup(fixture.tearDown)
        self.memory = fixture.memory
        return fixture

    def fail_closed(self, invoke, at):
        runner = CodexRunner({"model": MODEL, "reasoning_effort": "high",
            "authentication": "chatgpt", "paid_fallback": False})
        real_popen = subprocess.Popen
        script = "import sys;sys.stdin.buffer.read();sys.stderr.write('HTTP 429 Too Many Requests');sys.exit(1)"
        processes = []
        def local_process(command, **kwargs):
            self.assertEqual(command[0], "/fixture/codex")
            process = real_popen([sys.executable, "-c", script], **kwargs)
            processes.append(process); return process
        with patch("product_radar.research_ai.utc_now", return_value=at), \
             patch("product_radar.research_ai.shutil.which", return_value="/fixture/codex"), \
             patch("product_radar.research_ai.subprocess.run", return_value=subprocess.CompletedProcess([], 0, b"Logged in using ChatGPT", b"")), \
             patch("product_radar.research_ai.subprocess.Popen", side_effect=local_process):
            result = invoke(runner, at)
        self.assertEqual(len(processes), 1)
        jobs = self.memory.snapshot()["jobs"]; self.assertEqual(len(jobs), 1)
        jid = next(iter(jobs)); base = self.memory.root / "ai_jobs" / jid
        self.assertEqual(load_json(base / "failure.json")["code"], "AI_RATE_LIMITED")
        return jid, base, result

    def retry(self, runner, invoke, at):
        later = (timestamp(at) + timedelta(seconds=300)).isoformat()
        runner.closed_retry_policy = {"as_of": later, "retry_seconds": 300, "retry_not_before": None}
        turn = open_work_turn(self.memory, "actual-c-producer", at=later)
        with patch("product_radar.research_ai.utc_now", return_value=later), bind_work_turn(self.memory, turn["id"]):
            result = invoke(runner, later)
        self.assertEqual(self.memory.snapshot()["batch_runs"][turn["id"]]["counters"]["model_executions"], 1)
        self.assertEqual(len(runner.calls), 1)
        with bind_work_turn(self.memory, turn["id"]): invoke(runner, later)
        self.assertEqual(len(runner.calls), 1)
        return result

    def test_query_profile_producer_retries_exact_original_and_reuses_answer(self):
        fixture = self.setup_fixture(query_fixture.ProductQueryTests())
        def invoke(runner, at): return ensure_query_profiles(self.memory, runner,
            concept_ids=["bar"], catalogue=fixture.catalogue, as_of=at)
        jid, base, first = self.fail_closed(invoke, query_fixture.NOW)
        self.assertTrue(first["pending"])
        class Success(query_fixture.Runner): identity = "codex-chatgpt-high-v1"
        result = self.retry(Success(), invoke, query_fixture.NOW)
        self.assertFalse(result["pending"])
        self.assertIsNotNone(current_query_profile(self.memory.snapshot(), "bar"))
        self.assertEqual(current_attempt(base), base / "retry-1")
        self.assertEqual(len(self.memory.snapshot()["jobs"]), 1)
        self.assertIn(jid, result["recovered_job_ids"])

    def test_seed_producer_retries_exact_original_under_shared_turn(self):
        fixture = self.setup_fixture(seed_fixture.CatalogueSeedTests()); fixture.stage()
        def invoke(runner, at): return build_catalogue_branch(self.memory, fixture.base, runner,
            branch_id="storage", as_of=at)
        _, base, first = self.fail_closed(invoke, seed_fixture.NOW)
        self.assertTrue(first["pending"])
        class Success(seed_fixture.SeedRunner): identity = "codex-chatgpt-high-v1"
        result = self.retry(Success(), invoke, seed_fixture.NOW)
        self.assertFalse(result["pending"]); self.assertEqual(result["records"][0]["outcome"], "accepted")
        self.assertEqual(current_attempt(base), base / "retry-1")
        self.assertEqual(len(self.memory.snapshot()["jobs"]), 1)

    def test_expansion_producer_retries_using_its_actual_held_root_leader(self):
        fixture = self.setup_fixture(expansion_fixture.ExpansionTests()); fixture.concept()
        def invoke(runner, at): return run_expansion(self.memory, fixture.base, runner,
            concept_ids=["concept1"], as_of=at)
        _, base, first = self.fail_closed(invoke, expansion_fixture.NOW)
        self.assertEqual(first["problem"], "AI_RATE_LIMITED")
        class Success(expansion_fixture.Runner): identity = "codex-chatgpt-high-v1"
        result = self.retry(Success(), invoke, expansion_fixture.NOW)
        self.assertIsNone(result["problem"]); self.assertEqual(result["records"][0]["outcome"], "accepted")
        self.assertEqual(current_attempt(base), base / "retry-1")
        self.assertEqual(len(self.memory.snapshot()["jobs"]), 1)

    def test_unknown_query_attempt_has_no_retry_or_new_model_slot(self):
        fixture = self.setup_fixture(query_fixture.ProductQueryTests())
        class Unknown(query_fixture.Runner):
            identity = "codex-chatgpt-high-v1"
            def __call__(self, *args): self.calls.append(args); raise SafetyError("AI_RATE_LIMITED")
        first = Unknown()
        ensure_query_profiles(self.memory, first, concept_ids=["bar"], as_of=query_fixture.NOW)
        later = (timestamp(query_fixture.NOW) + timedelta(seconds=300)).isoformat()
        class Success(query_fixture.Runner): identity = "codex-chatgpt-high-v1"
        runner = Success(); runner.closed_retry_policy = {"as_of": later, "retry_seconds": 300, "retry_not_before": None}
        turn = open_work_turn(self.memory, "uncertain-c-producer", at=later)
        with bind_work_turn(self.memory, turn["id"]):
            result = ensure_query_profiles(self.memory, runner, concept_ids=["bar"], as_of=later)
        self.assertTrue(result["pending"]); self.assertFalse(runner.calls)
        self.assertEqual(len(self.memory.snapshot()["jobs"]), 1)
        self.assertEqual(self.memory.snapshot()["batch_runs"][turn["id"]]["counters"]["model_executions"], 0)

    def test_query_partial_application_retains_full_original_current_batch(self):
        fixture = self.setup_fixture(query_fixture.ProductQueryTests())
        self.memory.put("concepts", "bar2", deepcopy(self.memory.snapshot()["concepts"]["bar"]))
        def first(runner, at): return ensure_query_profiles(self.memory, runner,
            concept_ids=["bar", "bar2"], as_of=at)
        _, base, _ = self.fail_closed(first, query_fixture.NOW)
        class Success(query_fixture.Runner): identity = "codex-chatgpt-high-v1"
        def partial(runner, at): return ensure_query_profiles(self.memory, runner,
            concept_ids=["bar", "bar2"], limit=1, as_of=at)
        runner = Success(); result = self.retry(runner, partial, query_fixture.NOW)
        self.assertFalse(result["pending"]); self.assertEqual(len(runner.calls[0][1]["items"]), 2)
        self.assertEqual(current_attempt(base), base / "retry-1")

    def test_seed_partial_application_retains_full_original_current_batch(self):
        fixture = self.setup_fixture(seed_fixture.CatalogueSeedTests())
        fixture.stage(["Magnetic pan lid rack", "Collapsible drying basket"])
        def first(runner, at): return build_catalogue_branch(self.memory, fixture.base, runner,
            branch_id="storage", as_of=at)
        _, base, _ = self.fail_closed(first, seed_fixture.NOW)
        class Success(seed_fixture.SeedRunner): identity = "codex-chatgpt-high-v1"
        def partial(runner, at): return build_catalogue_branch(self.memory, fixture.base, runner,
            branch_id="storage", limit=1, as_of=at)
        runner = Success(); result = self.retry(runner, partial, seed_fixture.NOW)
        self.assertFalse(result["pending"]); self.assertEqual(len(runner.calls[0]["items"]), 2)
        self.assertEqual(current_attempt(base), base / "retry-1")

    def test_expansion_partial_application_retains_full_original_current_batch(self):
        fixture = self.setup_fixture(expansion_fixture.ExpansionTests()); fixture.concept()
        fixture.concept("Collapsible drying basket", concept_id="concept2")
        def first(runner, at): return run_expansion(self.memory, fixture.base, runner,
            concept_ids=["concept1", "concept2"], as_of=at)
        _, base, _ = self.fail_closed(first, expansion_fixture.NOW)
        class Success(expansion_fixture.Runner): identity = "codex-chatgpt-high-v1"
        def partial(runner, at): return run_expansion(self.memory, fixture.base, runner,
            concept_ids=["concept1", "concept2"], limit=1, as_of=at)
        runner = Success(); result = self.retry(runner, partial, expansion_fixture.NOW)
        self.assertIsNone(result["problem"]); self.assertEqual(len(runner.calls[0][1]["items"]), 2)
        self.assertEqual(current_attempt(base), base / "retry-1")

    def test_query_legacy_proof_reader_applies_late_original_after_checked_pointer_with_zero_calls(self):
        fixture = self.setup_fixture(query_fixture.ProductQueryTests())
        def invoke(runner, at): return ensure_query_profiles(self.memory, runner,
            concept_ids=["bar"], catalogue=fixture.catalogue, as_of=at)
        jid, base, _ = self.fail_closed(invoke, query_fixture.NOW)
        later = (timestamp(query_fixture.NOW) + timedelta(seconds=300)).isoformat()
        hint = closed_model_retry_hint(self.memory, jid, as_of=later)
        prepare_closed_model_retry(self.memory, jid, as_of=later, expected_signal=hint["signal"])
        class Success(query_fixture.Runner): identity = "codex-chatgpt-high-v1"
        request = self.memory.snapshot()["jobs"][jid]["request"]
        response, usage = Success()("C", request["payload"], None)
        atomic_json(base / "completion.json", {"response": response, "usage": usage, "exit_code": 0})
        runner = Success(); runner.closed_retry_policy = {"as_of": later, "retry_seconds": 300, "retry_not_before": None}
        turn = open_work_turn(self.memory, "late-query-answer", at=later)
        with bind_work_turn(self.memory, turn["id"]): result = invoke(runner, later)
        self.assertFalse(result["pending"]); self.assertFalse(runner.calls)
        self.assertIsNotNone(current_query_profile(self.memory.snapshot(), "bar"))
        self.assertEqual(current_attempt(base), base)
        self.assertEqual(len(load_json(base / "retries.json")), 1)
        self.assertEqual(self.memory.snapshot()["batch_runs"][turn["id"]]["counters"]["model_executions"], 0)

    def test_never_started_query_full_batch_resumes_during_partial_application_without_retry(self):
        import product_radar.research_ai as module
        fixture = self.setup_fixture(query_fixture.ProductQueryTests())
        self.memory.put("concepts", "bar2", deepcopy(self.memory.snapshot()["concepts"]["bar"]))
        class Success(query_fixture.Runner): identity = "codex-chatgpt-high-v1"
        original = module.atomic_json
        def stop_before_intent(path, value):
            if path.name == "intent.json": raise OSError("Synthetic crash before any original intent")
            return original(path, value)
        initial = Success()
        with patch.object(module, "atomic_json", side_effect=stop_before_intent):
            result = ensure_query_profiles(self.memory, initial, concept_ids=["bar", "bar2"], as_of=query_fixture.NOW)
        self.assertTrue(result["pending"]); self.assertFalse(initial.calls)
        jid = next(iter(self.memory.snapshot()["jobs"])); base = self.memory.root / "ai_jobs" / jid
        def partial(runner, at): return ensure_query_profiles(self.memory, runner,
            concept_ids=["bar", "bar2"], limit=1, as_of=at)
        runner = Success(); result = self.retry(runner, partial, query_fixture.NOW)
        self.assertFalse(result["pending"]); self.assertEqual(len(runner.calls[0][1]["items"]), 2)
        self.assertFalse((base / "retries.json").exists()); self.assertEqual(len(self.memory.snapshot()["jobs"]), 1)


if __name__ == "__main__": unittest.main()
