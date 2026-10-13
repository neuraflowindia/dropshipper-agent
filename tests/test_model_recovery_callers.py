"""Real application callers recover owned local failures, with no external AI."""

from contextlib import contextmanager
from datetime import timedelta
import subprocess
import sys
import unittest
from unittest.mock import patch

import test_fit_review as fit
from test_fit_description import DescriptionRunner
import test_closed_model_retry as closed
import test_functional_source_interpretation as source
import test_report_narration as writing
import test_scoped_supplier_terms as supplier
import test_programme_named_fit as named_fit
from product_radar.fit_review import prepare_practical_fit_reviews, run_fit_reviews
from product_radar.models import timestamp
from product_radar.programme import (_ProgrammeRunner, _fit, _saved_job_signals, prepare_programme,
                                    programme_turn_readiness)
from product_radar.report_narration import generate_report_narratives
from product_radar.research_ai import (CodexRunner, MODEL, current_attempt,
    closed_model_retry_hint, prepare_closed_model_retry)
from product_radar.source_interpretation import review_source_results
from product_radar.supplier_checks import extract_scoped_supplier_terms
from product_radar.work_turns import bind_work_turn, open_work_turn


class ModelRecoveryCallerTests(unittest.TestCase):
    def fixture(self, cls):
        fixture = cls(); fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        return fixture

    @contextmanager
    def closed_failure(self, at):
        """The actual CodexRunner owns a harmless Python process, not Codex."""
        real_popen = subprocess.Popen
        script = "import sys;sys.stdin.buffer.read();sys.stderr.write('HTTP 429 Too Many Requests');sys.exit(1)"
        def local_process(command, **kwargs):
            self.assertEqual(command[0], "/fixture/codex")
            return real_popen([sys.executable, "-c", script], **kwargs)
        runner = CodexRunner({"model": MODEL, "reasoning_effort": "high",
                              "authentication": "chatgpt", "paid_fallback": False})
        login = subprocess.CompletedProcess([], 0, b"Logged in using ChatGPT", b"")
        with patch("product_radar.research_ai.utc_now", return_value=at), \
             patch("product_radar.research_ai.shutil.which", return_value="/fixture/codex"), \
             patch("product_radar.research_ai.subprocess.run", return_value=login), \
             patch("product_radar.research_ai.subprocess.Popen", side_effect=local_process):
            yield runner

    def exercise(self, memory, at, action, successful, accepted):
        previous = set(memory.snapshot()["jobs"])
        with self.closed_failure(at) as runner:
            action(runner)
        owners = sorted(set(memory.snapshot()["jobs"]) - previous)
        self.assertTrue(owners)
        originals = {jid: {path.name: path.read_bytes() for path in
            (memory.root / "ai_jobs" / jid).iterdir() if path.is_file()} for jid in owners}
        self.assertTrue(all("execution_failure.json" in rows for rows in originals.values()))
        later = (timestamp(at) + timedelta(minutes=5)).isoformat()
        successful.identity = "codex-chatgpt-high-v1"
        successful.closed_retry_policy = {"as_of": later, "retry_seconds": 300, "retry_not_before": None}
        turn = open_work_turn(memory, "caller-retry", at=later)
        with bind_work_turn(memory, turn["id"]), patch("product_radar.research_ai.utc_now", return_value=later):
            result = action(successful)
            accepted(result)
            calls = len(successful.calls)
            action(successful)
        self.assertEqual(len(successful.calls), calls)
        counters = memory.snapshot()["batch_runs"][turn["id"]]["counters"]
        self.assertEqual(counters["model_executions"], len(owners))
        for jid, rows in originals.items():
            base = memory.root / "ai_jobs" / jid
            self.assertEqual(current_attempt(base), base / "retry-1")
            for name, raw in rows.items():
                self.assertEqual((base / name).read_bytes(), raw, (jid, name))

    def test_current_fit_review_retries_exact_original_and_applies_it_once(self):
        fixture = self.fixture(fit.FitReviewTests)
        reviews = prepare_practical_fit_reviews(fixture.memory, concept_ids=[fixture.concept_id], as_of=fit.NOW)
        action = lambda runner: run_fit_reviews(fixture.memory, runner, [row["review_id"] for row in reviews])
        self.exercise(fixture.memory, fit.NOW, action, fit.FitRunner(),
            lambda result: self.assertEqual([row["status"] for row in result], ["accepted"]))

    def test_due_closed_obsolete_fit_question_does_not_force_an_old_contract_rerun(self):
        self.obsolete_fit_transition(prepared=False)

    def test_checked_empty_retry_of_obsolete_fit_does_not_hold_the_current_contract(self):
        self.obsolete_fit_transition(prepared=True)

    def obsolete_fit_transition(self, *, prepared):
        fixture = self.fixture(fit.FitReviewTests)
        reviews = prepare_practical_fit_reviews(fixture.memory, concept_ids=[fixture.concept_id], as_of=fit.NOW)
        with self.closed_failure(fit.NOW) as runner:
            first = run_fit_reviews(fixture.memory, runner, [reviews[0]["review_id"]])
        self.assertEqual(first[0]["reason"], "AI_RATE_LIMITED")
        original_id = next(jid for jid, job in fixture.memory.snapshot()["jobs"].items()
            if job["request"]["payload"].get("items", [{}])[0].get("fit_review_base_id") == reviews[0]["review_id"])
        original = fixture.memory.root / "ai_jobs" / original_id
        old_files = {p.name: p.read_bytes() for p in original.iterdir() if p.is_file()}
        later = (timestamp(fit.NOW) + timedelta(minutes=5)).isoformat()
        if prepared:
            hint = closed_model_retry_hint(fixture.memory, original_id, as_of=later)
            prepare_closed_model_retry(fixture.memory, original_id, as_of=later,
                expected_signal=hint["signal"])
        good = DescriptionRunner(); good.identity = "codex-chatgpt-high-v1"
        good.closed_retry_policy = {"as_of": later, "retry_seconds": 300, "retry_not_before": None}
        turn = open_work_turn(fixture.memory, "fit-contract-transition", at=later)
        record = {"concept_ids": [fixture.concept_id], "deferrals": {}}
        with bind_work_turn(fixture.memory, turn["id"]), patch("product_radar.research_ai.utc_now", return_value=later):
            result = _fit(fixture.memory, record,
                {"fit_limit": 5, "runner": good, "at": later, "retry_seconds": 300})
        self.assertTrue(any(row["status"] == "obsolete_closed_failure" for row in result["outcomes"]), result)
        self.assertTrue(any(row["status"] == "accepted" for row in result["outcomes"]), result)
        self.assertEqual(len(good.calls), 1)
        self.assertEqual((original / "retries.json").exists(), prepared)
        if prepared:
            self.assertEqual(current_attempt(original), original / "retry-1")
            self.assertFalse((original / "retry-1" / "intent.json").exists())
        for name, raw in old_files.items(): self.assertEqual((original / name).read_bytes(), raw)

    def test_checked_empty_partial_retry_resumes_the_original_question_once(self):
        fixture = self.fixture(named_fit.ProgrammeNamedFitTests)
        fixture.accepted(); base = fixture.partial()
        with self.closed_failure(fit.NOW) as runner:
            first = run_fit_reviews(fixture.memory, runner, [base["reference"]])
        self.assertEqual(first[0]["reason"], "AI_RATE_LIMITED")
        job_id = next(jid for jid, job in fixture.memory.snapshot()["jobs"].items()
            if any(item.get("fit_review_base_id") == base["reference"]
                   for item in job["request"]["payload"]["items"]))
        later = (timestamp(fit.NOW) + timedelta(minutes=5)).isoformat()
        hint = closed_model_retry_hint(fixture.memory, job_id, as_of=later)
        prepare_closed_model_retry(fixture.memory, job_id, as_of=later,
            expected_signal=hint["signal"])
        good = fit.FitRunner(); good.identity = "codex-chatgpt-high-v1"
        good.closed_retry_policy = {"as_of": later, "retry_seconds": 300, "retry_not_before": None}
        turn = open_work_turn(fixture.memory, "named-fit-lost-handoff", at=later)
        with bind_work_turn(fixture.memory, turn["id"]), patch("product_radar.research_ai.utc_now", return_value=later):
            result, _ = fixture.stage(good, as_of=later)
        self.assertEqual(result["outcomes"][0]["status"], "accepted", result)
        self.assertEqual(len(good.calls), 1)
        self.assertEqual(current_attempt(fixture.memory.root / "ai_jobs" / job_id).name, "retry-1")

    def test_current_functional_source_review_retries_without_recollecting(self):
        fixture = self.fixture(source.FunctionalSourceInterpretationTests)
        before = fixture.memory.snapshot()
        action = lambda runner: review_source_results(fixture.memory, runner, run_id="full-list", as_of=source.NOW)
        self.exercise(fixture.memory, source.NOW, action, source.Runner(),
            lambda result: self.assertEqual(result["remaining_questions"], 0, result))
        for table in ("captures", "observations", "query_tasks"):
            self.assertEqual(before[table], fixture.memory.snapshot()[table])

    def test_scoped_supplier_retries_only_its_current_grounded_company_input(self):
        fixture = self.fixture(supplier.ScopedSupplierTests)
        prepared = fixture.prepare()[:1]
        action = lambda runner: extract_scoped_supplier_terms(fixture.memory, prepared, runner)
        self.exercise(fixture.memory, supplier.NOW, action, supplier.ScopedRunner(),
            lambda result: self.assertEqual(len(result["accepted"]), 1, result))

    def test_report_writing_retries_full_original_chunks_and_reuses_the_answers(self):
        fixture = self.fixture(writing.ReportNarrationTests)
        at = "2026-10-09T10:00:00+00:00"
        action = lambda runner: generate_report_narratives(fixture.memory, fixture.issue, fixture.dataset,
            runner, languages=("en",))
        self.exercise(fixture.memory, at, action, writing.NarrationRunner(),
            lambda result: self.assertEqual(result["failures"], {}, result))

    def test_programme_wakes_at_the_real_configured_retry_deadline_without_writes(self):
        fixture = self.fixture(closed.ClosedModelRetryTests)
        job_id, _, _ = fixture.fail_closed()
        record = prepare_programme(fixture.memory, "retry-wake", source_run_id="retry-wake-sources",
            concept_ids=[fixture.cid], as_of=closed.NOW)
        before = fixture.memory.snapshot_with_provenance()
        early = _saved_job_signals(fixture.memory, before["state"], record,
            as_of=closed.BEFORE, retry_seconds=3600)
        deadline = "2026-10-05T11:00:00+00:00"
        due = _saved_job_signals(fixture.memory, before["state"], record,
            as_of=deadline, retry_seconds=3600)
        self.assertEqual(early[job_id]["closed_retry"]["next_retry_at"], deadline)
        self.assertFalse(early[job_id]["closed_retry"]["ready"])
        self.assertTrue(due[job_id]["closed_retry"]["ready"])
        self.assertNotEqual(early, due)
        hint = programme_turn_readiness(fixture.memory, record, root=fixture.memory.root.parent,
            handlers={}, as_of=closed.BEFORE, retry_seconds=3600)
        self.assertEqual(hint["next_retry_at"], deadline)
        self.assertEqual(fixture.memory.snapshot_with_provenance(), before)

    def test_programme_sees_checked_empty_retry_as_due_without_new_preparation(self):
        fixture = self.fixture(closed.ClosedModelRetryTests)
        job_id, folder, _ = fixture.fail_closed()
        record = prepare_programme(fixture.memory, "prepared-wake", source_run_id="prepared-wake-sources",
            concept_ids=[fixture.cid], as_of=closed.NOW)
        hint = closed_model_retry_hint(fixture.memory, job_id, as_of=closed.AFTER)
        prepare_closed_model_retry(fixture.memory, job_id, as_of=closed.AFTER,
            expected_signal=hint["signal"])
        before = fixture.memory.snapshot_with_provenance()
        files = {path: path.read_bytes() for path in folder.rglob("*.json")}
        signals = _saved_job_signals(fixture.memory, before["state"], record,
            as_of=closed.AFTER, retry_seconds=300)
        self.assertTrue(signals[job_id]["closed_retry"]["ready"], signals)
        self.assertEqual(signals[job_id]["closed_retry"]["reason"], "AI_CHECKED_MODEL_RETRY_PREPARED")
        self.assertEqual(fixture.memory.snapshot_with_provenance(), before)
        self.assertTrue(all(path.read_bytes() == raw for path, raw in files.items()))

    def test_programme_pause_after_first_writing_failure_does_not_create_more_intents(self):
        fixture = self.fixture(writing.ReportNarrationTests)
        at = "2026-10-09T10:00:00+00:00"
        record = {"model_pause": None}
        context = {"memory": fixture.memory, "at": at, "fixed_as_of": at,
                   "retry_seconds": 300, "cooperative_mode": True}
        turn = open_work_turn(fixture.memory, "writing-pause", at=at)
        with bind_work_turn(fixture.memory, turn["id"]), self.closed_failure(at) as original:
            guarded = _ProgrammeRunner(original, record, context)
            result = generate_report_narratives(fixture.memory, fixture.issue, fixture.dataset,
                guarded, languages=("en",))
        self.assertTrue(result["failures"])
        self.assertEqual(record["model_pause"]["reason"], "AI_RATE_LIMITED")
        jobs = fixture.memory.snapshot()["jobs"]
        intents = [jid for jid in jobs if (fixture.memory.root / "ai_jobs" / jid / "intent.json").exists()]
        self.assertEqual(len(intents), 1)
        counters = fixture.memory.snapshot()["batch_runs"][turn["id"]]["counters"]
        self.assertEqual(counters["model_executions"], 1)


if __name__ == "__main__":
    unittest.main()
