"""Isolated saved-job fixtures: failures remain pending without starving peers."""

from copy import deepcopy
from pathlib import Path
import tempfile
import unittest

from test_research_flow import FakeRunner, capture, NOW
from product_radar.models import SafetyError, digest
from product_radar.research_ai import Jobs, prepare_retry
from product_radar.research_flow import run_flow
from product_radar.research_memory import ResearchMemory
from product_radar.storage import Journal, atomic_json


class SelectiveFailure(FakeRunner):
    def __init__(self, role):
        super().__init__()
        self.role = role
        self.failed_calls = []
        self.fail = True

    def __call__(self, role, payload, folder):
        first = payload["items"][0]
        selected = (first.get("source_text", {}).get("title") == "Blue sink caddy"
                    if role == "A" else first.get("description", {}).get("label") == "Sink caddy")
        if self.fail and role == self.role and selected:
            self.failed_calls.append(deepcopy(payload))
            raise SafetyError("AI_PROVIDER_REQUEST_FAILED")
        return super().__call__(role, payload, folder)


class ModelQueueProgressTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.memory = ResearchMemory(self.root / "research", Journal(self.root / "finance"), evidence_type="synthetic")
        capture(self.memory)

    def flow(self, runner, count=1, limit=1):
        for _ in range(count):
            summary = run_flow(self.memory, runner, limit=limit, as_of=NOW)
        return summary

    def failed_job(self, role):
        for job_id, job in self.memory.snapshot()["jobs"].items():
            if job["request"]["role"] == role and (self.memory.root / "ai_jobs" / job_id / "failure.json").exists():
                return job_id, job
        self.fail("Expected saved failure")

    def test_pending_a_does_not_consume_independent_description_slots(self):
        runner = SelectiveFailure("A")
        self.flow(runner, count=5)
        state = self.memory.snapshot()
        self.assertEqual(len(state["descriptions"]), 3)
        self.assertEqual(len(runner.failed_calls), 1)
        self.assertEqual({c["description"]["label"] for c in state["concepts"].values()},
                         {"Sink caddy", "Powered cleaning brush"})
        self.assertTrue(any(p["stage"] == "description" for p in state["problems"].values()))

    def test_pending_b_does_not_consume_independent_identity_slots(self):
        runner = SelectiveFailure("B")
        self.flow(runner, count=6)
        state = self.memory.snapshot()
        self.assertEqual(len(state["descriptions"]), 4)
        self.assertEqual(len(runner.failed_calls), 1)
        self.assertEqual({c["description"]["label"] for c in state["concepts"].values()},
                         {"Sink caddy", "Powered cleaning brush"})
        self.assertTrue(any(p["stage"] == "identity" for p in state["problems"].values()))

    def test_pending_e_does_not_consume_independent_assessment_slots(self):
        runner = SelectiveFailure("E")
        self.flow(runner, count=6)
        state = self.memory.snapshot()
        brush = next(i for i, c in state["concepts"].items() if c["description"]["label"] == "Powered cleaning brush")
        self.assertIn(brush, state["assessments"])
        # The second caddy observation is genuinely different checklist input,
        # so it remains eligible rather than inheriting the first failure.
        self.assertEqual(len(runner.failed_calls), 2)
        self.assertNotEqual(digest(runner.failed_calls[0]), digest(runner.failed_calls[1]))
        self.assertTrue(any(p["stage"] == "assessment" for p in state["problems"].values()))

    def test_saved_a_result_or_completion_recovers_without_another_call(self):
        for artifact in ("result.json", "completion.json"):
            with self.subTest(artifact=artifact):
                isolated = self.root / artifact.replace(".", "-")
                memory = ResearchMemory(isolated / "research", Journal(isolated / "finance"), evidence_type="synthetic")
                capture(memory)
                runner = SelectiveFailure("A")
                run_flow(memory, runner, limit=1, as_of=NOW)
                job_id, job = next(iter(memory.snapshot()["jobs"].items()))
                response, usage = FakeRunner()("A", job["request"]["payload"], None)
                saved = ({"request_hash": digest(job["request"]), "response": response, "usage": usage}
                         if artifact == "result.json" else {"response": response, "usage": usage, "exit_code": 0})
                atomic_json(memory.root / "ai_jobs" / job_id / artifact, saved)
                self.assertEqual(Jobs(memory, runner).pending_bindings(memory.snapshot()), set())
                run_flow(memory, runner, limit=1, as_of=NOW)
                item_id = job["request"]["payload"]["items"][0]["id"]
                self.assertIn(item_id, memory.snapshot()["descriptions"])
                self.assertEqual(len(runner.failed_calls), 1)

    def test_explicit_retry_becomes_ready_preserving_original_failure(self):
        runner = SelectiveFailure("A")
        self.flow(runner)
        job_id, job = self.failed_job("A")
        original = (self.memory.root / "ai_jobs" / job_id / "failure.json").read_bytes()
        self.assertEqual(prepare_retry(self.memory, job_id, "Retry this saved execution after inspecting failure."), 1)
        self.assertEqual(Jobs(self.memory, runner).pending_bindings(self.memory.snapshot()), set())
        runner.fail = False
        self.flow(runner)
        self.assertIn(job["request"]["payload"]["items"][0]["id"], self.memory.snapshot()["descriptions"])
        self.assertEqual((self.memory.root / "ai_jobs" / job_id / "failure.json").read_bytes(), original)
        self.assertTrue((self.memory.root / "ai_jobs" / job_id / "retry-1" / "result.json").exists())

    def test_b_and_e_saved_results_and_completions_remain_recoverable(self):
        for role in ("B", "E"):
            for artifact in ("result.json", "completion.json"):
                with self.subTest(role=role, artifact=artifact):
                    isolated = self.root / (role + artifact.replace(".", "-"))
                    memory = ResearchMemory(isolated / "research", Journal(isolated / "finance"), evidence_type="synthetic")
                    capture(memory)
                    runner = SelectiveFailure(role)
                    if role == "B":
                        for _ in range(2):
                            run_flow(memory, runner, limit=1, as_of=NOW)
                    else:
                        run_flow(memory, runner, limit=10, as_of=NOW)
                    job_id, job = next((i, j) for i, j in memory.snapshot()["jobs"].items()
                                       if j["request"]["role"] == role)
                    response, usage = FakeRunner()(role, job["request"]["payload"], None)
                    saved = ({"request_hash": digest(job["request"]), "response": response, "usage": usage}
                             if artifact == "result.json" else {"response": response, "usage": usage, "exit_code": 0})
                    atomic_json(memory.root / "ai_jobs" / job_id / artifact, saved)
                    self.assertFalse(Jobs(memory, runner).pending_bindings(memory.snapshot()))
                    for _ in range(2):
                        run_flow(memory, runner, limit=1, as_of=NOW)
                    state = memory.snapshot()
                    table = "associations" if role == "B" else "assessments"
                    for item in job["request"]["payload"]["items"]:
                        self.assertIn(item["id"], state[table])
                    self.assertEqual(len(runner.failed_calls), 1)

    def test_b_and_e_explicit_retry_are_ready_without_erasing_failure(self):
        for role in ("B", "E"):
            with self.subTest(role=role):
                isolated = self.root / (role + "-retry")
                memory = ResearchMemory(isolated / "research", Journal(isolated / "finance"), evidence_type="synthetic")
                capture(memory)
                runner = SelectiveFailure(role)
                if role == "B":
                    for _ in range(2):
                        run_flow(memory, runner, limit=1, as_of=NOW)
                else:
                    run_flow(memory, runner, limit=10, as_of=NOW)
                job_id, job = next((i, j) for i, j in memory.snapshot()["jobs"].items()
                                   if j["request"]["role"] == role)
                failure = memory.root / "ai_jobs" / job_id / "failure.json"
                original = failure.read_bytes()
                prepare_retry(memory, job_id, "Deliberate fixture retry after execution inspection.")
                self.assertFalse(Jobs(memory, runner).pending_bindings(memory.snapshot()))
                runner.fail = False
                for _ in range(2):
                    run_flow(memory, runner, limit=1, as_of=NOW)
                table = "associations" if role == "B" else "assessments"
                for item in job["request"]["payload"]["items"]:
                    self.assertIn(item["id"], memory.snapshot()[table])
                self.assertEqual(failure.read_bytes(), original)
                self.assertTrue((failure.parent / "retry-1" / "result.json").exists())

    def test_changed_b_candidates_are_not_the_same_unfinished_input(self):
        runner = SelectiveFailure("B")
        self.flow(runner, count=2)
        _, job = self.failed_job("B")
        jobs = Jobs(self.memory, runner)
        pending = jobs.pending_bindings(self.memory.snapshot())
        payload = deepcopy(job["request"]["payload"])
        self.assertEqual(jobs.blocked_items("B", payload, pending), {payload["items"][0]["id"]})
        payload["items"][0]["candidates"][0]["description"]["features"].append("Newly supplied feature")
        self.assertFalse(jobs.blocked_items("B", payload, pending))

    def test_exact_binding_only_and_no_helper_writes(self):
        runner = SelectiveFailure("A")
        self.flow(runner)
        _, job = self.failed_job("A")
        before = self.memory.snapshot()
        jobs = Jobs(self.memory, runner)
        pending = jobs.pending_bindings(before)
        payload = deepcopy(job["request"]["payload"])
        self.assertEqual(jobs.blocked_items("A", payload, pending), {payload["items"][0]["id"]})
        payload["items"][0]["source_text"]["title"] = "A genuinely changed product description"
        self.assertEqual(jobs.blocked_items("A", payload, pending), set())
        payload = deepcopy(job["request"]["payload"])
        payload["description_scope"] = "different-current-input-scope"
        self.assertEqual(jobs.blocked_items("A", payload, pending), set())
        self.assertEqual(self.memory.snapshot(), before)

    def test_independent_progress_keeps_per_run_item_bound(self):
        runner = SelectiveFailure("A")
        self.flow(runner)
        for _ in range(3):
            before = self.memory.snapshot()
            self.flow(runner)
            after = self.memory.snapshot()
            for table in ("descriptions", "associations", "assessments"):
                self.assertLessEqual(len(set(after[table]) - set(before[table])), 1, table)


if __name__ == "__main__":
    unittest.main()
