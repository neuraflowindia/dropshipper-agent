"""Real owned local-process failures through three automatic B callers.

All saved products come from existing synthetic producers. The fake Codex
executable is a local Python child which only reads stdin and exits; no model,
provider, browser or network request is made.
"""

from copy import deepcopy
from contextlib import nullcontext
from datetime import datetime, timedelta
import subprocess
import sys
import unittest
from unittest.mock import patch

import test_functional_alias_review as alias_fixture
import test_functional_example_review as example_fixture
import test_catalogue_scope_review as scope_fixture
import product_radar.functional_alias_review as aliases
import product_radar.functional_example_review as examples
import product_radar.catalogue_scope_review as scopes
from product_radar.models import SafetyError, digest
from product_radar.research_ai import (CodexRunner, MODEL, closed_model_retry_hint,
    current_attempt, prepare_closed_model_retry, prepare_retry)
from product_radar.storage import atomic_json, load_json
from product_radar.work_turns import DEFAULT_POLICY, bind_work_turn, close_work_turn, open_work_turn


class ClosedReviewRetryTests(unittest.TestCase):
    MODES = ("examples", "scope", "aliases")

    def setup_mode(self, mode):
        self.mode = mode
        if mode == "examples":
            fixture = example_fixture.FunctionalExampleReviewTests(); fixture.setUp()
            self.module, self.now, self.runner_type = examples, example_fixture.NOW, example_fixture.Runner
        elif mode == "scope":
            fixture = scope_fixture.CatalogueScopeTests(); fixture.setUp()
            self.module, self.now, self.runner_type = scopes, scope_fixture.waves.NOW, scope_fixture.ScopeRunner
        else:
            fixture = alias_fixture.FunctionalAliasReviewTests(); fixture.setUp(); fixture.different_wording()
            # Keep this fixture focused on one nonexact pair, not the separate
            # exact/candidate group involving its unrelated third product.
            fixture.different_wording("cat", use="Bake food in an oven")
            fixture.fixture.accept_definition("cat", dict(fixture.fixture.definition,
                primary_use="Bake food in an oven", mechanism="Electric heating element"),
                reviewed_at="2026-10-06T10:00:00+00:00")
            self.module, self.now, self.runner_type = aliases, alias_fixture.REVIEWED, alias_fixture.Runner
        self.addCleanup(fixture.doCleanups)
        self.fixture, self.memory = fixture, fixture.memory
        at = datetime.fromisoformat(self.now)
        self.before = (at + timedelta(seconds=299)).isoformat()
        self.after = (at + timedelta(seconds=300)).isoformat()

    def success(self):
        runner = self.runner_type(); runner.identity = "codex-chatgpt-high-v1"
        return runner

    def review(self, runner, *, at=None, **kwargs):
        at = at or self.now
        with patch("product_radar.research_ai.utc_now", return_value=at), \
             (patch.object(self.module, "utc_now", return_value=at) if self.mode != "examples" else nullcontext()):
            if self.mode == "examples":
                return examples.review_functional_examples(self.memory, [self.fixture.cid], runner, as_of=at, **kwargs)
            if self.mode == "scope":
                return scopes.review_discovery_catalogue_scopes(self.memory, runner, catalogue=self.fixture.catalogue,
                    concept_ids=[self.fixture.cid], as_of=at, **kwargs)
            return aliases.review_functional_aliases(self.memory, [["bear", "bird"]], runner, reviewed_at=at, **kwargs)

    def hint(self, *, at=None, skip=None, **kwargs):
        at = at or self.now
        if self.mode == "examples":
            return examples.functional_example_readiness(self.memory, [self.fixture.cid], as_of=at,
                skip_concept_bindings=skip, **kwargs)
        if self.mode == "scope":
            return scopes.catalogue_scope_readiness(self.memory, catalogue=self.fixture.catalogue,
                concept_ids=[self.fixture.cid], as_of=at, runner_available=True, skip_bindings=skip, **kwargs)
        return aliases.functional_alias_readiness(self.memory, self.memory.snapshot(), ["bird"], as_of=at,
            runner_available=True, limit=1, skip_group_bindings=skip, **kwargs)

    def bindings(self, hint):
        return ({row["group_id"]: row["input_hash"] for row in hint["groups"]}
            if self.mode == "aliases" else hint["bindings"])

    def accepted(self, result):
        return result["current_count"] if self.mode == "scope" else len(result["accepted"])

    def fail_closed(self, diagnostic="HTTP 429 Too Many Requests", *, invoke=None):
        runner = CodexRunner({"model": MODEL, "reasoning_effort": "high",
            "authentication": "chatgpt", "paid_fallback": False})
        real_popen = subprocess.Popen
        script = "import sys;sys.stdin.buffer.read();sys.stderr.write(" + repr(diagnostic) + ");sys.exit(1)"
        def owned_local(command, **kwargs):
            self.assertEqual(command[0], "/fixture/codex")
            return real_popen([sys.executable, "-c", script], **kwargs)
        login = subprocess.CompletedProcess([], 0, b"Logged in using ChatGPT", b"")
        with patch("product_radar.research_ai.shutil.which", return_value="/fixture/codex"), \
             patch("product_radar.research_ai.subprocess.run", return_value=login), \
             patch("product_radar.research_ai.subprocess.Popen", side_effect=owned_local):
            result = invoke(runner) if invoke else self.review(runner)
        jid, job = next((jid, row) for jid, row in self.memory.snapshot()["jobs"].items()
            if row.get("request", {}).get("prompt_version") == self.module.PROMPT_VERSION)
        self.job_id, self.job, self.base = jid, job, self.memory.root / "ai_jobs" / jid
        self.assertTrue((self.base / "execution_failure.json").exists())
        return result

    def test_closed_rate_limit_wait_then_same_original_retry_and_zero_call_reuse(self):
        for mode in self.MODES:
            with self.subTest(mode=mode):
                self.setup_mode(mode); self.fail_closed()
                originals = {p.name: p.read_bytes() for p in self.base.iterdir()}
                before_state = self.memory.snapshot_with_provenance()
                waiting = self.hint(at=self.before)
                self.assertFalse(waiting["ready"]); self.assertEqual(waiting["waits"], [self.after])
                self.assertEqual(before_state, self.memory.snapshot_with_provenance())
                self.assertFalse(self.hint(at=self.before, skip=self.bindings(waiting))["ready"])
                self.assertTrue(self.hint(at=self.after, skip=self.bindings(waiting))["model_ready"])
                runner = self.success(); result = self.review(runner, at=self.after)
                self.assertGreater(self.accepted(result), 0); self.assertEqual(len(runner.calls), 1)
                self.assertEqual(current_attempt(self.base), self.base / "retry-1")
                self.assertEqual(load_json(self.base / "retries.json")[0]["previous_failure"], "AI_RATE_LIMITED")
                for name, raw in originals.items(): self.assertEqual((self.base / name).read_bytes(), raw)
                self.review(runner, at=self.after); self.assertEqual(len(runner.calls), 1)
                self.assertEqual(len(load_json(self.base / "retries.json")), 1)

    def test_network_retry_respects_configured_and_programme_deadline(self):
        for mode in self.MODES:
            with self.subTest(mode=mode):
                self.setup_mode(mode); self.fail_closed("Failed to connect: network is unreachable")
                later = (datetime.fromisoformat(self.now) + timedelta(hours=2)).isoformat()
                waiting = self.hint(at=self.after, retry_seconds=3600, retry_not_before=later)
                self.assertFalse(waiting["ready"]); self.assertEqual(waiting["waits"], [later])
                self.assertEqual(waiting["retry_hints"][self.job_id]["failure_code"], "AI_NETWORK_UNAVAILABLE")
                runner = self.success(); self.review(runner, at=self.after, retry_seconds=3600, retry_not_before=later)
                self.assertEqual(runner.calls, []); self.assertFalse((self.base / "retries.json").exists())
                self.assertTrue(self.hint(at=later, retry_seconds=3600, retry_not_before=later)["model_ready"])
                self.assertGreater(self.accepted(self.review(runner, at=later, retry_seconds=3600,
                    retry_not_before=later)), 0)
                self.assertEqual(len(runner.calls), 1)

    def test_prepared_empty_retry_resumes_original_without_preparing_twice(self):
        for mode in self.MODES:
            with self.subTest(mode=mode):
                self.setup_mode(mode); self.fail_closed()
                hint = closed_model_retry_hint(self.memory, self.job_id, as_of=self.after)
                self.assertEqual(prepare_closed_model_retry(self.memory, self.job_id,
                    as_of=self.after, expected_signal=hint["signal"]), 1)
                self.assertFalse((self.base / "retry-1" / "intent.json").exists())
                self.assertTrue(self.hint(at=self.after)["model_ready"])
                runner = self.success(); result = self.review(runner, at=self.after)
                self.assertGreater(self.accepted(result), 0)
                self.assertEqual(runner.calls, [("B", self.job["request"]["payload"])])
                self.assertEqual(len(load_json(self.base / "retries.json")), 1)
                self.review(runner, at=self.after); self.assertEqual(len(runner.calls), 1)

    def test_prepared_empty_retry_waits_when_policy_or_global_deadline_lengthens(self):
        for mode in self.MODES:
            for policy in ("retry_seconds", "retry_not_before"):
                with self.subTest(mode=mode, policy=policy):
                    self.setup_mode(mode); self.fail_closed()
                    hint = closed_model_retry_hint(self.memory, self.job_id, as_of=self.after)
                    prepare_closed_model_retry(self.memory, self.job_id, as_of=self.after,
                        expected_signal=hint["signal"])
                    later = (datetime.fromisoformat(self.now) + timedelta(hours=2)).isoformat()
                    options = {"retry_seconds": 7200} if policy == "retry_seconds" else {"retry_not_before": later}
                    waiting = self.hint(at=self.after, **options)
                    self.assertFalse(waiting["ready"])
                    self.assertEqual(waiting["waits"], [later])
                    self.assertEqual(waiting["retry_hints"][self.job_id]["previous_attempt"], "initial")
                    self.assertFalse(self.hint(at=self.after, skip=self.bindings(waiting), **options)["ready"])
                    runner = self.success(); self.review(runner, at=self.after, **options)
                    self.assertEqual(runner.calls, [])
                    self.assertEqual(len(load_json(self.base / "retries.json")), 1)
                    self.assertFalse((self.base / "retry-1" / "intent.json").exists())
                    self.assertTrue(self.hint(at=later, skip=self.bindings(waiting), **options)["model_ready"])
                    self.assertGreater(self.accepted(self.review(runner, at=later, **options)), 0)
                    self.assertEqual(len(runner.calls), 1)
                    self.assertEqual(len(load_json(self.base / "retries.json")), 1)

    def test_manual_empty_retry_and_damaged_checked_proof_do_not_wake_automatic_review(self):
        for mode in self.MODES:
            for proof in ("manual", "damaged_checked"):
                with self.subTest(mode=mode, proof=proof):
                    self.setup_mode(mode); self.fail_closed()
                    if proof == "manual":
                        prepare_retry(self.memory, self.job_id, "Synthetic explicit manual retry.")
                    else:
                        hint = closed_model_retry_hint(self.memory, self.job_id, as_of=self.after)
                        prepare_closed_model_retry(self.memory, self.job_id, as_of=self.after,
                            expected_signal=hint["signal"])
                        pointer = load_json(self.base / "retries.json")
                        pointer[0]["closed_retry_proof"]["failure_code"] = "AI_MODEL_UNAVAILABLE"
                        atomic_json(self.base / "retries.json", pointer)
                    waiting = self.hint(at=self.after)
                    self.assertFalse(waiting["ready"])
                    runner = self.success(); self.review(runner, at=self.after)
                    self.assertEqual(runner.calls, [])
                    self.assertFalse((self.base / "retry-1" / "intent.json").exists())
                    self.assertEqual(len(load_json(self.base / "retries.json")), 1)

    def test_unknown_rate_limit_never_relaunches_without_actual_terminal_proof(self):
        for mode in self.MODES:
            with self.subTest(mode=mode):
                self.setup_mode(mode); runner = self.success()
                def unknown(*args): runner.calls.append(args); raise SafetyError("AI_RATE_LIMITED")
                class Unknown:
                    identity = runner.identity
                    __call__ = staticmethod(unknown)
                self.review(Unknown()); again = self.review(Unknown(), at=self.after)
                self.assertEqual(len(runner.calls), 1); self.assertEqual(self.accepted(again), 0)
                self.assertFalse(self.hint(at=self.after)["ready"])
                jobs = [jid for jid, row in self.memory.snapshot()["jobs"].items()
                    if row.get("request", {}).get("prompt_version") == self.module.PROMPT_VERSION]
                self.assertEqual(len(jobs), 1)
                self.assertFalse((self.memory.root / "ai_jobs" / jobs[0] / "retries.json").exists())

    def test_late_original_completion_wins_over_due_retry_and_uses_no_runner(self):
        for mode in self.MODES:
            with self.subTest(mode=mode):
                self.setup_mode(mode); self.fail_closed()
                waiting = self.hint(at=self.before)
                response, usage = self.success()("B", self.job["request"]["payload"], None)
                atomic_json(self.base / "completion.json", {"response": response, "usage": usage, "exit_code": 0})
                self.assertTrue(self.hint(at=self.after, skip=self.bindings(waiting))["local_ready"])
                runner = self.success(); result = self.review(runner, at=self.after)
                self.assertGreater(self.accepted(result), 0); self.assertEqual(runner.calls, [])
                self.assertFalse((self.base / "retries.json").exists())

    def test_wrong_runner_never_prepares_an_otherwise_due_retry(self):
        for mode in self.MODES:
            with self.subTest(mode=mode):
                self.setup_mode(mode); self.fail_closed()
                wrong = self.runner_type(); self.review(wrong, at=self.after)
                self.assertEqual(wrong.calls, []); self.assertFalse((self.base / "retries.json").exists())

    def test_live_pause_guard_precedes_any_new_intent_or_synthetic_failure(self):
        for mode in self.MODES:
            with self.subTest(mode=mode):
                self.setup_mode(mode); runner = self.success()
                def paused(): raise SafetyError("AI_RATE_LIMITED")
                runner.check_new_execution = paused
                self.review(runner)
                self.assertEqual(runner.calls, [])
                jobs = [(jid, row) for jid, row in self.memory.snapshot()["jobs"].items()
                    if row.get("request", {}).get("prompt_version") == self.module.PROMPT_VERSION]
                self.assertEqual(len(jobs), 1)
                folder = self.memory.root / "ai_jobs" / jobs[0][0]
                for name in ("intent.json", "failure.json", "runner_execution.json", "execution_failure.json"):
                    self.assertFalse((folder / name).exists(), name)
                runner.check_new_execution = lambda: None
                self.assertGreater(self.accepted(self.review(runner)), 0)
                self.assertEqual(len(runner.calls), 1)

    def test_corrupt_terminal_proof_is_an_isolated_named_wait_not_retry_authority(self):
        for mode in self.MODES:
            with self.subTest(mode=mode):
                self.setup_mode(mode); self.fail_closed()
                receipt = load_json(self.base / "execution_failure.json")
                receipt["owned_group_cleanup_confirmed"] = False
                atomic_json(self.base / "execution_failure.json", receipt)
                hint = self.hint(at=self.after); self.assertFalse(hint["ready"])
                self.assertIn("AI_RETRY_RECONCILIATION_REQUIRED", str(hint))
                runner = self.success(); self.review(runner, at=self.after)
                self.assertEqual(runner.calls, []); self.assertFalse((self.base / "retries.json").exists())

    def test_setup_failure_remains_waiting_and_is_not_an_automatic_retry(self):
        for mode in self.MODES:
            with self.subTest(mode=mode):
                self.setup_mode(mode); self.fail_closed("Model not found")
                hint = self.hint(at=self.after); self.assertFalse(hint["ready"])
                self.assertEqual(hint["waits"], [])
                runner = self.success(); self.review(runner, at=self.after)
                self.assertEqual(runner.calls, []); self.assertFalse((self.base / "retries.json").exists())

    def test_retry_uses_current_turn_allowance_and_cannot_repeat_in_the_same_turn(self):
        for mode in self.MODES:
            with self.subTest(mode=mode):
                self.setup_mode(mode); self.fail_closed()
                turn = open_work_turn(self.memory, "bounded-retry-review", at=self.after,
                    policy=dict(deepcopy(DEFAULT_POLICY), source_callbacks=1, model_executions=1,
                        exploration_slots=0, report_slots=0),
                    ready_lanes={"backlog": True, "exploration": False})
                try:
                    with bind_work_turn(self.memory, turn["id"]):
                        runner = self.success(); self.review(runner, at=self.after); self.review(runner, at=self.after)
                        self.assertEqual(len(runner.calls), 1)
                finally:
                    ended = close_work_turn(self.memory, turn["id"], at=self.after, reason="Synthetic finite retry proof.")
                self.assertEqual(ended["counters"]["model_executions"], 1)
                self.assertEqual(len(ended["reservations"]["model"]), 1)
                reservation = next(iter(ended["reservations"]["model"].values()))
                self.assertEqual(reservation["job_id"], self.job_id)
                self.assertEqual(reservation["attempt"], "retry-1")

    def test_examples_changed_child_blocks_retry_of_the_whole_original_item(self):
        self.setup_mode("examples"); self.fail_closed()
        state = self.memory.snapshot(); child = self.fixture.children[0]; old = state["associations"][child]
        self.memory.put("associations", child, dict(old, status="deferred"), previous=old)
        hint = self.hint(at=self.after)
        self.assertFalse(hint["ready"])
        self.assertEqual(hint["waiting"][self.fixture.cid]["reason"], "FUNCTIONAL_EXAMPLE_ORIGINAL_BATCH_INPUT_CHANGED")
        runner = self.success(); self.review(runner, at=self.after)
        self.assertEqual(runner.calls, []); self.assertFalse((self.base / "retries.json").exists())

    def test_unknown_allowlisted_original_does_not_block_disjoint_new_examples(self):
        from test_research_flow import FakeRunner, capture, run_flow
        from product_radar.functional_types import example_membership_preparation
        self.setup_mode("examples"); unknown_calls = []
        class Unknown:
            identity = "codex-chatgpt-high-v1"
            def __call__(self, *args): unknown_calls.append(args); raise SafetyError("AI_RATE_LIMITED")
        self.review(Unknown())
        capture(self.memory, [{"ad_archive_id": "1101", "page_id": "1201", "is_active": True,
            "start_date": 1788220800, "snapshot": {"title": "Another sink caddy"}}], acquisition="disjoint-new-example")
        run_flow(self.memory, FakeRunner(), as_of=self.now)
        added = set(example_membership_preparation(self.memory.snapshot(), self.fixture.cid)["pending_child_ids"]) - set(self.fixture.children)
        self.assertEqual(len(added), 1)
        hint = self.hint(at=self.after)
        self.assertTrue(hint["model_ready"])
        self.assertEqual({child["child_id"] for child in hint["prepared"][0]["item"]["examples"]}, added)
        runner = self.success(); result = self.review(runner, at=self.after)
        self.assertGreater(self.accepted(result), 0); self.assertEqual(len(runner.calls), 1)
        self.assertEqual(len(unknown_calls), 1)
        self.assertFalse(self.hint(at=self.after)["ready"])

    def test_scope_changed_sibling_family_blocks_whole_original_batch_retry(self):
        self.setup_mode("scope")
        second = deepcopy(self.fixture.catalogue["families"][0]); second.update(id="caddy-second", label="Second heading")
        self.fixture.catalogue["families"].append(second)
        scope_fixture.waves.DiscoveryWaveTests.advance(self.fixture, max_actions=3,
            runner=scope_fixture.FakeRunner(), admission=None)
        self.fail_closed(); self.assertEqual(len(self.job["request"]["payload"]["items"]), 2)
        self.fixture.catalogue["families"][1]["definition"] += " Only powered mechanisms."
        runner = self.success(); self.review(runner, at=self.after, limit=1)
        # A changed heading is a genuinely new independent question. It may
        # continue, but cannot prepare or relaunch the now-stale original batch.
        self.assertTrue(all(payload != self.job["request"]["payload"] for _, payload in runner.calls))
        self.assertFalse((self.base / "retries.json").exists())

    def test_alias_changed_unselected_sibling_blocks_whole_original_batch_retry(self):
        self.setup_mode("aliases")
        self.fixture.different_wording("cat", use="Support books in a standing position")
        def original(runner):
            with patch("product_radar.research_ai.utc_now", return_value=self.now):
                return aliases.review_functional_aliases(self.memory, [["bear", "bird"], ["bear", "cat"]], runner,
                    reviewed_at=self.now)
        self.fail_closed(invoke=original); self.assertEqual(len(self.job["request"]["payload"]["items"]), 2)
        self.fixture.fixture.accept_definition("cat", dict(self.fixture.fixture.definition,
            primary_use="Cook food in an oven", mechanism="Powered heating element"), reviewed_at=self.now)
        runner = self.success(); result = self.review(runner, at=self.after)
        self.assertEqual(set(result["failures"].values()), {"FUNCTIONAL_ALIAS_ORIGINAL_BATCH_INPUT_CHANGED"})
        self.assertEqual(runner.calls, []); self.assertFalse((self.base / "retries.json").exists())


if __name__ == "__main__": unittest.main()
