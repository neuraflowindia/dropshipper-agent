"""Installed saved-evidence flow retries; only synthetic local child processes."""

from copy import deepcopy
import subprocess
import sys
import unittest
from unittest.mock import patch

import test_research_flow as fixture
from product_radar.models import SafetyError
from product_radar.research_ai import (CodexRunner, MODEL, closed_model_retry_hint,
    current_attempt, prepare_closed_model_retry, prepare_retry)
from product_radar.research_flow import _description_source_job, run_flow
from product_radar.research_memory import key
from product_radar.storage import atomic_json, load_json
from product_radar.work_turns import bind_work_turn, close_work_turn, open_work_turn

NOW = fixture.NOW
BEFORE = "2026-10-05T10:04:59+00:00"
AFTER = "2026-10-05T10:05:00+00:00"


class CooperativeRunner(fixture.FakeRunner):
    identity = "codex-chatgpt-high-v1"
    def __init__(self, at=NOW):
        super().__init__()
        self.closed_retry_policy = {"as_of": at, "retry_seconds": 300, "retry_not_before": None}


class ClosedFlowRetryTests(unittest.TestCase):
    def setUp(self):
        actual = fixture.FlowTests(); actual.setUp(); self.addCleanup(actual.doCleanups)
        self.memory, self.fixture = actual.memory, actual

    def prepare_role(self, role):
        self.role = role
        if role == "B":
            fixture.capture(self.memory, [fixture.rows()[0]], acquisition="base-listing")
            run_flow(self.memory, fixture.FakeRunner(), as_of=NOW)
            self.base_concept = next(iter(self.memory.snapshot()["concepts"]))
            fixture.capture(self.memory, [fixture.rows()[1]], acquisition="new-listing")
        elif role == "E":
            fixture.capture(self.memory, [fixture.rows()[0], fixture.rows()[2]])
        else:
            fixture.capture(self.memory)

    def run_turn(self, runner, *, at=NOW, **kwargs):
        runner.closed_retry_policy["as_of"] = at
        turn = open_work_turn(self.memory, "flow-transient-retry", at=at,
            ready_lanes={"backlog": True, "exploration": False})
        try:
            with bind_work_turn(self.memory, turn["id"]), \
                 patch("product_radar.research_ai.utc_now", return_value=at), \
                 patch("product_radar.research_flow.utc_now", return_value=at):
                result = run_flow(self.memory, runner, as_of=at, **kwargs)
        finally:
            ended = close_work_turn(self.memory, turn["id"], at=at, reason="Synthetic bounded flow retry.")
        return result, ended

    def fail_closed(self, *, invoke=None):
        codex = CodexRunner({"model": MODEL, "reasoning_effort": "high",
            "authentication": "chatgpt", "paid_fallback": False})
        role, calls = self.role, []
        class FailOneRole(CooperativeRunner):
            def __call__(inner, actual_role, payload, folder):
                if actual_role == role:
                    inner.calls.append((actual_role, deepcopy(payload))); calls.append(actual_role)
                    return codex(actual_role, payload, folder)
                return super().__call__(actual_role, payload, folder)
        runner = FailOneRole()
        real_popen = subprocess.Popen
        script = "import sys;sys.stdin.buffer.read();sys.stderr.write('HTTP 429 Too Many Requests');sys.exit(1)"
        def owned_local(command, **kwargs):
            self.assertEqual(command[0], "/fixture/codex")
            return real_popen([sys.executable, "-c", script], **kwargs)
        login = subprocess.CompletedProcess([], 0, b"Logged in using ChatGPT", b"")
        with patch("product_radar.research_ai.shutil.which", return_value="/fixture/codex"), \
             patch("product_radar.research_ai.subprocess.run", return_value=login), \
             patch("product_radar.research_ai.subprocess.Popen", side_effect=owned_local):
            invoke(runner) if invoke else self.run_turn(runner)
        self.assertEqual(calls, [role])
        self.job_id, self.job = next((jid, row) for jid, row in self.memory.snapshot()["jobs"].items()
            if row["request"]["role"] == role and row["request"]["runner"] == runner.identity)
        self.base = self.memory.root / "ai_jobs" / self.job_id
        self.assertTrue((self.base / "execution_failure.json").exists())
        self.originals = {p.name: p.read_bytes() for p in self.base.iterdir() if p.is_file()}
        return runner

    def assert_originals(self):
        for name, raw in self.originals.items(): self.assertEqual((self.base / name).read_bytes(), raw)

    def test_A_waits_then_retries_entire_original_batch_even_when_current_slice_is_one(self):
        self.prepare_role("A"); self.fail_closed()
        self.assertEqual(len(self.job["request"]["payload"]["items"]), 4)
        before = CooperativeRunner(); self.run_turn(before, at=BEFORE, limit=1)
        self.assertEqual(before.calls, []); self.assertFalse((self.base / "retries.json").exists())
        runner = CooperativeRunner(); _, ended = self.run_turn(runner, at=AFTER, limit=1)
        descriptions = [payload for role, payload in runner.calls if role == "A"]
        self.assertEqual(descriptions, [self.job["request"]["payload"]])
        self.assertEqual(current_attempt(self.base), self.base / "retry-1")
        self.assertTrue(all(row["job_id"] == self.job_id for row in self.memory.snapshot()["descriptions"].values()))
        reservations = [row for row in ended["reservations"]["model"].values() if row["job_id"] == self.job_id]
        self.assertEqual(len(reservations), 1); self.assertEqual(reservations[0]["attempt"], "retry-1")
        self.assert_originals()
        self.run_turn(runner, at=AFTER)
        self.assertEqual(len([role for role, _ in runner.calls if role == "A"]), 1)
        self.assertEqual(set(self.memory.snapshot()["descriptions"]),
            {item["id"] for item in self.job["request"]["payload"]["items"]})
        self.assertTrue(all(row["job_id"] == self.job_id for row in self.memory.snapshot()["descriptions"].values()))

    def test_A_source_owner_rejects_corrupt_exact_binding_and_does_not_use_aggregate_fallback(self):
        self.prepare_role("A"); self.fail_closed()
        item = self.job["request"]["payload"]["items"][0]
        state = self.memory.snapshot(); runner = CooperativeRunner()
        self.assertEqual(_description_source_job(state, item, self.job["request"]["payload"], runner), self.job_id)
        state["jobs"][self.job_id]["binding_keys"][item["id"]] = "0" * 64
        with self.assertRaisesRegex(SafetyError, "DESCRIPTION_SOURCE_JOB_BINDING_CHANGED"):
            _description_source_job(state, item, self.job["request"]["payload"], runner)
        with self.assertRaisesRegex(SafetyError, "DESCRIPTION_SOURCE_JOB_REQUIRED"):
            _description_source_job(self.memory.snapshot(), dict(item, source_text={"title": "Changed input"}),
                self.job["request"]["payload"], runner)
        # Even a resealed malformed whole-batch envelope cannot make an
        # unrelated broken sibling escape as a raw error or borrowed owner.
        damaged = deepcopy(self.job); damaged["request"]["payload"]["items"].append(None)
        state = self.memory.snapshot(); state["jobs"] = {key("job", damaged["request"]): damaged}
        with self.assertRaisesRegex(SafetyError, "DESCRIPTION_SOURCE_JOB_BINDING_CHANGED"):
            _description_source_job(state, item, self.job["request"]["payload"], runner)

    def test_new_A_answers_preserve_every_already_saved_description_row(self):
        fixture.capture(self.memory, [fixture.rows()[0]], acquisition="older-listing")
        run_flow(self.memory, fixture.FakeRunner(), as_of=NOW)
        originals = self.memory.snapshot()["descriptions"]
        self.role = "A"
        fixture.capture(self.memory, [fixture.rows()[1], fixture.rows()[2]], acquisition="newer-listings")
        self.fail_closed()
        self.run_turn(CooperativeRunner(), at=AFTER, limit=1)
        state = self.memory.snapshot()
        for sid, row in originals.items(): self.assertEqual(state["descriptions"][sid], row)
        fresh = [row for sid, row in state["descriptions"].items() if sid not in originals]
        self.assertEqual(len(fresh), 1); self.assertEqual(fresh[0]["job_id"], self.job_id)

    def test_B_known_closed_retry_preserves_actual_child_and_original_association_target(self):
        self.prepare_role("B"); self.fail_closed()
        child = self.job["request"]["payload"]["items"][0]["id"]
        self.assertNotIn(child, self.memory.snapshot()["associations"])
        before = CooperativeRunner(); self.run_turn(before, at=BEFORE)
        self.assertFalse(any(role == "B" for role, _ in before.calls))
        runner = CooperativeRunner(); self.run_turn(runner, at=AFTER)
        self.assertEqual([payload for role, payload in runner.calls if role == "B"], [self.job["request"]["payload"]])
        association = self.memory.snapshot()["associations"][child]
        self.assertEqual(association["concept_id"], self.base_concept)
        self.assertEqual(association["job_id"], self.job_id)
        self.assertEqual(len(self.memory.snapshot()["concepts"]), 1)
        self.assertEqual(current_attempt(self.base), self.base / "retry-1"); self.assert_originals()
        self.run_turn(runner, at=AFTER)
        self.assertEqual(len([role for role, _ in runner.calls if role == "B"]), 1)

    def test_reachable_legacy_E_known_closed_retry_keeps_exact_original_assessment_batch(self):
        self.prepare_role("E"); self.fail_closed()
        self.assertEqual(len(self.job["request"]["payload"]["items"]), 2)
        runner = CooperativeRunner(); self.run_turn(runner, at=AFTER)
        self.assertEqual([payload for role, payload in runner.calls if role == "E"], [self.job["request"]["payload"]])
        self.assertEqual(current_attempt(self.base), self.base / "retry-1")
        self.assertEqual(set(self.memory.snapshot()["assessments"]),
            {item["id"] for item in self.job["request"]["payload"]["items"]})
        self.assert_originals(); self.run_turn(runner, at=AFTER)
        self.assertEqual(len([role for role, _ in runner.calls if role == "E"]), 1)

    def assert_prepared_retry_resumes(self, role):
        self.prepare_role(role); self.fail_closed()
        hint = closed_model_retry_hint(self.memory, self.job_id, as_of=AFTER)
        prepare_closed_model_retry(self.memory, self.job_id, as_of=AFTER, expected_signal=hint["signal"])
        self.assertFalse((self.base / "retry-1" / "intent.json").exists())
        runner = CooperativeRunner(); later = "2026-10-05T12:00:00+00:00"
        runner.closed_retry_policy["retry_not_before"] = later
        self.run_turn(runner, at=AFTER)
        self.assertFalse(any(actual == role for actual, _ in runner.calls))
        self.assertEqual(len(load_json(self.base / "retries.json")), 1)
        self.assertFalse((self.base / "retry-1" / "intent.json").exists())
        _, ended = self.run_turn(runner, at=later)
        self.assertEqual([payload for actual, payload in runner.calls if actual == role],
            [self.job["request"]["payload"]])
        self.assertEqual(len(load_json(self.base / "retries.json")), 1)
        reservations = [row for row in ended["reservations"]["model"].values() if row["job_id"] == self.job_id]
        self.assertEqual(len(reservations), 1); self.assertEqual(reservations[0]["attempt"], "retry-1")
        self.assert_originals(); self.run_turn(runner, at=later)
        self.assertEqual(len([actual for actual, _ in runner.calls if actual == role]), 1)

    def test_A_checked_prepared_retry_survives_lost_handoff_and_later_pause(self):
        self.assert_prepared_retry_resumes("A")

    def test_B_checked_prepared_retry_survives_lost_handoff_and_later_pause(self):
        self.assert_prepared_retry_resumes("B")

    def test_legacy_E_checked_prepared_retry_survives_lost_handoff_and_later_pause(self):
        self.assert_prepared_retry_resumes("E")

    def test_manual_empty_retry_does_not_gain_automatic_recovery_authority(self):
        self.prepare_role("A"); self.fail_closed()
        prepare_retry(self.memory, self.job_id, "Synthetic explicit manual pointer; not automatic proof.")
        self.assertNotIn("closed_retry_proof", load_json(self.base / "retries.json")[0])
        runner = CooperativeRunner(); self.run_turn(runner, at=AFTER)
        self.assertEqual(runner.calls, [])
        self.assertFalse((self.base / "retry-1" / "intent.json").exists())
        self.assertEqual(len(load_json(self.base / "retries.json")), 1)
        self.assert_originals()

    def test_no_active_cooperative_turn_keeps_legacy_pending_behavior(self):
        self.prepare_role("A"); self.fail_closed()
        runner = CooperativeRunner(AFTER)
        run_flow(self.memory, runner, as_of=AFTER)
        self.assertEqual(runner.calls, []); self.assertFalse((self.base / "retries.json").exists())
        self.assert_originals()

    def test_A_out_of_scope_original_sibling_prevents_retry_without_blocking_broader_later_scope(self):
        self.role = "A"
        fixture.capture(self.memory, [fixture.rows()[0]], acquisition="first-acquisition")
        fixture.capture(self.memory, [fixture.rows()[1]], acquisition="second-acquisition")
        self.fail_closed()
        runner = CooperativeRunner(); self.run_turn(runner, at=AFTER, acquisition_ids=["first-acquisition"])
        self.assertEqual(runner.calls, []); self.assertFalse((self.base / "retries.json").exists())
        self.run_turn(runner, at=AFTER, acquisition_ids=["first-acquisition", "second-acquisition"])
        self.assertEqual([payload for role, payload in runner.calls if role == "A"], [self.job["request"]["payload"]])

    def test_unknown_allowlisted_A_original_stays_pending_while_disjoint_new_listing_continues(self):
        self.prepare_role("A")
        class Unknown(CooperativeRunner):
            def __call__(inner, role, payload, folder):
                if role == "A": inner.calls.append((role, deepcopy(payload))); raise SafetyError("AI_RATE_LIMITED")
                return super().__call__(role, payload, folder)
        original = Unknown(); self.run_turn(original)
        jid = next(jid for jid, row in self.memory.snapshot()["jobs"].items() if row["request"]["role"] == "A")
        old_items = {row["id"] for row in self.memory.snapshot()["jobs"][jid]["request"]["payload"]["items"]}
        fixture.capture(self.memory, [{"ad_archive_id": "999", "page_id": "999", "is_active": True,
            "start_date": 1788220800, "snapshot": {"title": "Another sink caddy"}}], acquisition="new-disjoint-listing")
        runner = CooperativeRunner(); self.run_turn(runner, at=AFTER)
        new_A = [payload for role, payload in runner.calls if role == "A"]
        self.assertEqual(len(new_A), 1)
        self.assertFalse({row["id"] for row in new_A[0]["items"]} & old_items)
        self.assertEqual(len(original.calls), 1)
        self.assertFalse((self.memory.root / "ai_jobs" / jid / "retries.json").exists())

    def test_changed_B_candidate_meaning_does_not_relaunch_the_obsolete_original(self):
        self.prepare_role("B"); self.fail_closed()
        old = self.memory.snapshot()["concepts"][self.base_concept]
        changed = deepcopy(old); changed["description"]["features"].append("Current reviewed design distinction")
        self.memory.put("concepts", self.base_concept, changed, previous=old)
        runner = CooperativeRunner(); self.run_turn(runner, at=AFTER)
        self.assertFalse((self.base / "retries.json").exists())
        self.assertTrue(all(payload != self.job["request"]["payload"] for role, payload in runner.calls if role == "B"))
        self.assert_originals()

    def test_changed_E_facts_do_not_retry_the_superseded_original_batch(self):
        self.prepare_role("E"); self.fail_closed()
        cid = self.job["request"]["payload"]["items"][0]["id"]
        old = self.memory.snapshot()["concepts"][cid]
        changed = deepcopy(old); changed["description"]["features"].append("Current reviewed design distinction")
        self.memory.put("concepts", cid, changed, previous=old)
        runner = CooperativeRunner(); self.run_turn(runner, at=AFTER)
        self.assertFalse((self.base / "retries.json").exists())
        self.assertTrue(all(payload != self.job["request"]["payload"] for role, payload in runner.calls if role == "E"))
        self.assert_originals()

    def test_completed_original_A_wins_without_retry_or_new_A_execution(self):
        self.prepare_role("A"); self.fail_closed()
        response, usage = CooperativeRunner()("A", self.job["request"]["payload"], None)
        atomic_json(self.base / "completion.json", {"response": response, "usage": usage, "exit_code": 0})
        runner = CooperativeRunner(); self.run_turn(runner, at=AFTER)
        self.assertFalse(any(role == "A" for role, _ in runner.calls))
        self.assertFalse((self.base / "retries.json").exists())
        self.assertEqual(len(self.memory.snapshot()["descriptions"]), 4)

    def test_current_pause_guard_creates_no_new_A_intent_or_synthetic_failure(self):
        self.prepare_role("A"); runner = CooperativeRunner()
        def paused(): raise SafetyError("AI_RATE_LIMITED")
        runner.check_new_execution = paused
        self.run_turn(runner)
        self.assertEqual(runner.calls, [])
        # Managed jobs are saved during actual admission, which this guard
        # prevents; no placeholder owner or synthetic provider failure exists.
        self.assertFalse(any(row["request"]["role"] == "A" for row in self.memory.snapshot()["jobs"].values()))
        self.assertEqual(list((self.memory.root / "ai_jobs").glob("*/intent.json")), [])
        self.assertEqual(list((self.memory.root / "ai_jobs").glob("*/failure.json")), [])
        runner.check_new_execution = lambda: None
        self.run_turn(runner)
        self.assertEqual(len([role for role, _ in runner.calls if role == "A"]), 1)


if __name__ == "__main__": unittest.main()
