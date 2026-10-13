"""Disposable real Jobs/supplier-writer checks; no provider or model calls."""

from copy import deepcopy
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from product_radar.models import SafetyError, canonical, digest
from product_radar.research_ai import CodexRunner, Jobs, MODEL, closed_model_retry_hint, current_attempt
from product_radar.research_memory import ResearchMemory
from product_radar.storage import Journal, atomic_json, load_json
from product_radar.supplier_quote_repair import (
    FAILURE, _claim_path, resolve_supplier_question, supplier_model_question, supplier_original_retry_question,
)
from product_radar.suppliers import _save_supplier_aliases, _supplier_aliases, _supplier_payload, extract_supplier_terms
from product_radar.work_turns import DEFAULT_POLICY, bind_work_turn, close_work_turn, open_work_turn
import test_work_turns as turn_fixtures


NOW = "2026-10-12T10:00:00+00:00"
AFTER = "2026-10-12T10:05:00+00:00"
PAGE = "Main Domestic Market / All India. We have a huge consumer base."


def item(item_id="bad", *, bad=True):
    return {"id": item_id, "product": {"label": "Bad" if bad else "Good"},
            "pages": [{"id": "page-" + item_id, "url": "https://example.com/" + item_id,
                       "title": "Synthetic supplier", "text": PAGE}]}


class QuoteRunner:
    identity = "synthetic-supplier-quote-v1"

    def __init__(self, *, identity=None, reject_repair=False, completion=False, alteration=None, repair_error=None):
        if identity is not None:
            self.identity = identity
        self.reject_repair, self.completion = reject_repair, completion
        self.alteration, self.repair_error = alteration, repair_error
        self.calls, self.responses = [], []

    def __call__(self, role, payload, folder):
        self.calls.append(deepcopy(payload))
        if "supplier_repair" in payload:
            if self.repair_error is not None:
                raise self.repair_error
            value = deepcopy(payload["supplier_repair"]["rejected_result"])
            if not self.reject_repair:
                value["terms"][0]["quotes"][1]["excerpt"] = "huge consumer base"
            if self.alteration is not None:
                self.alteration(value)
            values = [value]
        else:
            values = []
            for supplied in payload["items"]:
                page = supplied["pages"][0]["id"]
                terms = []
                if supplied["product"]["label"] == "Bad":
                    terms = [{"field": "india_coverage", "value": "Yes", "basis": "observed",
                              "references": [page], "quotes": [
                                  {"page_id": page, "excerpt": "Main Domestic Market / All India"},
                                  {"page_id": page, "excerpt": "huge customer base"}],
                              "reason": "Synthetic supplied coverage claim."}]
                values.append({"id": supplied["id"], "relevance": {
                    "relationship": "relevant_company", "references": [page], "reason": "Synthetic company."},
                    "terms": terms, "prices": []})
        response, usage = {"results": values}, {"accounting": "synthetic"}
        self.responses.append(deepcopy(response))
        if self.completion:
            atomic_json(folder / "completion.json", {"response": response, "usage": usage, "exit_code": 0})
        return response, usage


class SupplierQuoteRepairTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name)
        self.memory = ResearchMemory(root / "research", Journal(root / "finance"), evidence_type="synthetic")

    def seed(self, runner, items=None):
        originals = items or [item()]
        payload = _supplier_payload(originals)
        _, refs = _supplier_aliases(originals)
        _save_supplier_aliases(self.memory, runner, payload, refs)
        return Jobs(self.memory, runner).run("F", payload, declared_version="research-f-v3")[0]

    def question(self, original=None):
        return supplier_model_question(self.memory, original or item(), state=self.memory.snapshot())

    def fail_closed(self, originals):
        """Run only an immediate local Python failure, never Codex or a model."""
        runner = CodexRunner({"model": MODEL, "reasoning_effort": "high",
                              "authentication": "chatgpt", "paid_fallback": False})
        actual_popen = subprocess.Popen
        processes = []
        script = "import sys;sys.stdin.buffer.read();sys.stderr.write('HTTP 429 Too Many Requests');sys.exit(1)"

        def local_process(command, **kwargs):
            self.assertEqual(command[0], "/fixture/codex")
            process = actual_popen([sys.executable, "-c", script], **kwargs)
            processes.append(process)
            return process

        before = set(self.memory.snapshot()["jobs"])
        login = subprocess.CompletedProcess([], 0, b"Logged in using ChatGPT", b"")
        with patch("product_radar.research_ai.utc_now", return_value=NOW), \
             patch("product_radar.research_ai.shutil.which", return_value="/fixture/codex"), \
             patch("product_radar.research_ai.subprocess.run", return_value=login), \
             patch("product_radar.research_ai.subprocess.Popen", side_effect=local_process):
            result = extract_supplier_terms(self.memory, originals, runner)
        new = set(self.memory.snapshot()["jobs"]) - before
        self.assertEqual(len(new), 1)
        self.assertEqual(len(processes), 1)
        self.assertIsNotNone(processes[0].poll())
        job_id = new.pop()
        return job_id, self.memory.root / "ai_jobs" / job_id, result

    def cooperative_runner(self):
        runner = QuoteRunner(identity="codex-chatgpt-high-v1")
        runner.closed_retry_policy = {"as_of": lambda: AFTER, "retry_seconds": 300,
                                      "retry_not_before": lambda: None}
        return runner

    def test_new_question_is_pure_and_exactly_once_aliased(self):
        before = self.memory.snapshot_with_provenance()
        original = item()
        hint = self.question(original)
        self.assertEqual(hint["state"], "original")
        self.assertEqual(hint["payload"], _supplier_payload([original]))
        self.assertNotEqual(hint["payload"]["items"][0]["id"], _supplier_payload(hint["payload"]["items"])["items"][0]["id"])
        self.assertIsNone(hint["completed_at"])
        self.assertEqual(self.memory.snapshot_with_provenance(), before)

    def test_one_correction_keeps_siblings_raw_reply_and_full_scope_identity(self):
        runner = QuoteRunner()
        result = extract_supplier_terms(self.memory, [item(), item("good", bad=False)], runner)
        self.assertEqual(set(result["accepted"]), {"bad", "good"})
        self.assertEqual(result["failures"], {})
        self.assertEqual(len(runner.calls), 2)
        self.assertEqual(len(runner.calls[0]["items"]), 2)
        self.assertEqual(len(runner.calls[1]["items"]), 1)
        hint = self.question()
        descriptor = hint["payload"]["supplier_repair"]
        self.assertEqual(descriptor["scope_id"], digest([descriptor["original_job_id"],
            descriptor["original_binding_key"], descriptor["original_response_hash"]]))
        self.assertEqual(len(_claim_path(self.memory, hint["scope_id"]).parent.name), 77)
        self.assertTrue(_claim_path(self.memory, hint["scope_id"]).is_file())
        saved = load_json(self.memory.root / "ai_jobs" / descriptor["original_job_id"] / "result.json")
        self.assertEqual(saved["response"], runner.responses[0])
        self.assertIn("huge customer base", canonical(saved["response"]).decode())
        self.assertTrue(hint["local_ready"])
        self.assertIsNotNone(hint["completed_at"])
        self.assertEqual(result["accepted"]["bad"]["prices"], [])

    def test_completed_rejected_correction_is_terminal_and_never_repeated(self):
        runner = QuoteRunner(reject_repair=True)
        first = extract_supplier_terms(self.memory, [item(), item("good", bad=False)], runner)
        self.assertEqual(set(first["accepted"]), {"good"})
        hint = self.question()
        self.assertEqual(hint["state"], "terminal")
        self.assertEqual(hint["failure"], FAILURE)
        self.assertIsNotNone(hint["completed_at"])
        before = self.memory.snapshot_with_provenance()
        other = QuoteRunner(identity="different-runner")
        for current in (runner, other, None):
            result = extract_supplier_terms(self.memory, [item()], current)
            self.assertEqual(result["failures"]["bad"], FAILURE)
            self.assertEqual(result["failure_job_ids"]["bad"], hint["job_id"])
        self.assertEqual(len(runner.calls), 2)
        self.assertEqual(other.calls, [])
        self.assertEqual(self.memory.snapshot_with_provenance(), before)

    def test_changed_runner_and_regrouping_use_actual_original_then_one_repair(self):
        first = QuoteRunner()
        original_id = self.seed(first, [item(), item("good", bad=False)])
        raw = (self.memory.root / "ai_jobs" / original_id / "result.json").read_bytes()
        second = QuoteRunner(identity="second-runner")
        result = extract_supplier_terms(self.memory, [item("good", bad=False), item()], second)
        self.assertEqual(set(result["accepted"]), {"bad", "good"})
        self.assertEqual(len(second.calls), 1)
        self.assertEqual(second.calls[0]["supplier_repair"]["original_job_id"], original_id)
        third = QuoteRunner(identity="third-runner")
        replay = extract_supplier_terms(self.memory, [item()], third)
        self.assertIn("bad", replay["accepted"])
        self.assertEqual(third.calls, [])
        self.assertEqual((self.memory.root / "ai_jobs" / original_id / "result.json").read_bytes(), raw)
        self.assertEqual(len(self.memory.snapshot()["jobs"]), 2)

    def test_no_runner_does_not_claim_or_launch_a_correction(self):
        first = QuoteRunner(); self.seed(first)
        hint = self.question()
        result = extract_supplier_terms(self.memory, [item()])
        self.assertEqual(result["failures"]["bad"], "AI_RUNNER_NOT_ENABLED")
        self.assertFalse(_claim_path(self.memory, hint["scope_id"]).exists())
        self.assertEqual(len(first.calls), 1)

    def test_uncertain_original_is_not_a_new_runner_or_repair_question(self):
        runner = QuoteRunner()
        def fail(role, payload, folder):
            raise OSError("Synthetic local failure")
        runner.__class__ = type("FailedOriginal", (QuoteRunner,), {"__call__": fail})
        with self.assertRaises(SafetyError):
            self.seed(runner)
        self.assertEqual(self.question()["state"], "waiting")
        next_runner = QuoteRunner(identity="next-runner")
        result = extract_supplier_terms(self.memory, [item()], next_runner)
        self.assertEqual(result["failures"]["bad"], "AI_JOB_PENDING_RECONCILIATION")
        self.assertEqual(next_runner.calls, [])
        self.assertEqual(len(self.memory.snapshot()["jobs"]), 1)

    def test_closed_original_retry_uses_existing_full_group_proof_and_turn_allowance(self):
        originals = [item("good", bad=False), item("other", bad=False)]
        job_id, base, first = self.fail_closed(originals)
        self.assertEqual(first["failures"], {"good": "AI_RATE_LIMITED", "other": "AI_RATE_LIMITED"})
        original_bytes = {path.name: path.read_bytes() for path in base.iterdir() if path.is_file()}
        runner = self.cooperative_runner()
        question = self.question(originals[0])
        self.assertEqual(question["state"], "waiting")
        before = self.memory.snapshot_with_provenance()
        with patch.object(self.memory, "snapshot", side_effect=AssertionError("Use the verified advisory state")):
            due = supplier_original_retry_question(self.memory, question, runner,
                current_items=_supplier_payload(originals)["items"], as_of=AFTER, state=before["state"])
        self.assertEqual(due["state"], "original")
        self.assertEqual(due["job_id"], job_id)
        self.assertEqual(self.memory.snapshot_with_provenance(), before)
        self.assertFalse((base / "retries.json").exists())

        turn = open_work_turn(self.memory, "supplier-closed-original", at=AFTER)
        with bind_work_turn(self.memory, turn["id"]):
            result = extract_supplier_terms(self.memory, originals, runner,
                                            current_guard=lambda original, state: None)
        self.assertEqual(set(result["accepted"]), {"good", "other"})
        self.assertEqual(result["failures"], {})
        self.assertEqual(len(runner.calls), 1)
        self.assertEqual(runner.calls[0]["items"], _supplier_payload(originals)["items"])
        self.assertEqual(current_attempt(base), base / "retry-1")
        state = self.memory.snapshot()
        counters = state["batch_runs"][turn["id"]]["counters"]
        self.assertEqual(counters["model_executions"], 1)
        self.assertEqual(counters["purpose_items"]["F"], 2)
        self.assertEqual({row["job_id"] for row in state["supplier_assessments"].values()}, {job_id})
        for name, data in original_bytes.items():
            self.assertEqual((base / name).read_bytes(), data)
        extract_supplier_terms(self.memory, originals, runner)
        self.assertEqual(len(runner.calls), 1)

    def test_closed_original_partial_group_cannot_prepare_or_execute_retry(self):
        originals = [item("good", bad=False), item("other", bad=False)]
        _, base, _ = self.fail_closed(originals)
        runner = self.cooperative_runner()
        question = self.question(originals[0])
        self.assertEqual(supplier_original_retry_question(self.memory, question, runner,
            current_items=_supplier_payload(originals[:1])["items"], as_of=AFTER), question)
        turn = open_work_turn(self.memory, "supplier-partial-original", at=AFTER)
        with bind_work_turn(self.memory, turn["id"]):
            result = extract_supplier_terms(self.memory, originals[:1], runner)
        self.assertEqual(result["failures"], {"good": "AI_JOB_PENDING_RECONCILIATION"})
        self.assertEqual(result["accepted"], {})
        self.assertEqual(runner.calls, [])
        self.assertFalse((base / "retries.json").exists())
        self.assertEqual(self.memory.snapshot()["batch_runs"][turn["id"]]["counters"]["model_executions"], 0)

    def test_closed_original_opt_in_requires_due_policy_exact_runner_and_current_words(self):
        originals = [item("good", bad=False), item("other", bad=False)]
        _, base, _ = self.fail_closed(originals)
        question = self.question(originals[0])
        current_items = _supplier_payload(originals)["items"]
        changed = deepcopy(current_items)
        changed[1]["pages"][0]["text"] += " A changed saved page."
        runner = self.cooperative_runner()
        for current_runner, supplied, kwargs in (
            (None, current_items, {"as_of": AFTER}),
            (QuoteRunner(identity="other-runner"), current_items, {"as_of": AFTER}),
            (runner, current_items, {}),  # Unbound policy does not opt in.
            (runner, current_items, {"as_of": "2026-10-12T10:04:59+00:00"}),
            (runner, current_items, {"as_of": AFTER, "retry_not_before": "2026-10-12T11:00:00+00:00"}),
            (runner, changed, {"as_of": AFTER}),
        ):
            with self.subTest(runner=getattr(current_runner, "identity", None), kwargs=kwargs, changed=supplied == changed):
                self.assertEqual(supplier_original_retry_question(self.memory, question, current_runner,
                    current_items=supplied, **kwargs), question)
        self.assertEqual(runner.calls, [])
        self.assertFalse((base / "retries.json").exists())

    def test_unknown_original_rate_limit_is_not_proved_closed_or_retried(self):
        class Unknown(QuoteRunner):
            def __call__(self, role, payload, folder):
                self.calls.append(deepcopy(payload))
                raise SafetyError("AI_RATE_LIMITED")

        original = item("good", bad=False)
        first_runner = Unknown(identity="codex-chatgpt-high-v1")
        first = extract_supplier_terms(self.memory, [original], first_runner)
        self.assertEqual(first["failures"], {"good": "AI_RATE_LIMITED"})
        question = self.question(original)
        runner = self.cooperative_runner()
        self.assertEqual(supplier_original_retry_question(self.memory, question, runner,
            current_items=_supplier_payload([original])["items"], as_of=AFTER), question)
        turn = open_work_turn(self.memory, "supplier-unknown-original", at=AFTER)
        with bind_work_turn(self.memory, turn["id"]):
            result = extract_supplier_terms(self.memory, [original], runner)
        self.assertEqual(result["failures"], {"good": "AI_JOB_PENDING_RECONCILIATION"})
        self.assertEqual(len(first_runner.calls), 1)
        self.assertEqual(runner.calls, [])
        base = self.memory.root / "ai_jobs" / question["job_id"]
        self.assertFalse((base / "retries.json").exists())

    def test_proved_closed_corrective_failure_never_uses_original_retry_path(self):
        original_runner = QuoteRunner(identity="codex-chatgpt-high-v1")
        self.seed(original_runner)
        job_id, base, first = self.fail_closed([item()])
        self.assertEqual(first["failures"], {"bad": "AI_RATE_LIMITED"})
        question = self.question()
        self.assertEqual(question["state"], "waiting")
        self.assertIsNotNone(question["scope_id"])
        self.assertEqual(question["job_id"], job_id)
        self.assertTrue(closed_model_retry_hint(self.memory, job_id, as_of=AFTER)["ready"])
        runner = self.cooperative_runner()
        before = self.memory.snapshot_with_provenance()
        self.assertEqual(supplier_original_retry_question(self.memory, question, runner,
            current_items=_supplier_payload([item()])["items"], as_of=AFTER), question)
        self.assertEqual(self.memory.snapshot_with_provenance(), before)
        turn = open_work_turn(self.memory, "supplier-corrective-one-shot", at=AFTER)
        with bind_work_turn(self.memory, turn["id"]):
            result = extract_supplier_terms(self.memory, [item()], runner)
        self.assertEqual(result["failures"], {"bad": "AI_JOB_PENDING_RECONCILIATION"})
        self.assertEqual(runner.calls, [])
        self.assertEqual(len(original_runner.calls), 1)
        self.assertFalse((base / "retries.json").exists())

    def test_unsent_claimed_different_runner_waits_without_creating_an_intent(self):
        runner = QuoteRunner(); self.seed(runner)
        real_run = Jobs.run
        def spent(instance, role, payload, **kwargs):
            if "supplier_repair" in payload:
                raise SafetyError("WORK_TURN_MODEL_LIMIT")
            return real_run(instance, role, payload, **kwargs)
        with patch.object(Jobs, "run", new=spent):
            result = extract_supplier_terms(self.memory, [item()], runner)
        self.assertEqual(result["failures"]["bad"], "WORK_TURN_MODEL_LIMIT")
        hint = self.question()
        self.assertEqual(hint["runner_identity"], runner.identity)
        before = self.memory.snapshot_with_provenance()
        other = QuoteRunner(identity="different-runner")
        result = extract_supplier_terms(self.memory, [item()], other)
        self.assertEqual(result["failures"]["bad"], "AI_JOB_PENDING_RECONCILIATION")
        self.assertEqual(other.calls, [])
        self.assertFalse((self.memory.root / "ai_jobs" / hint["job_id"] / "intent.json").exists())
        self.assertEqual(self.memory.snapshot_with_provenance(), before)

    def test_uncertain_correction_never_repeats_even_with_another_runner(self):
        runner = QuoteRunner(repair_error=OSError("Synthetic corrective execution error"))
        first = extract_supplier_terms(self.memory, [item()], runner)
        self.assertEqual(first["failures"]["bad"], "AI_LOCAL_EXECUTION_FAILED")
        self.assertEqual(self.question()["state"], "waiting")
        other = QuoteRunner(identity="another-runner")
        for current in (runner, other):
            result = extract_supplier_terms(self.memory, [item()], current)
            self.assertEqual(result["failures"]["bad"], "AI_JOB_PENDING_RECONCILIATION")
        self.assertEqual(len(runner.calls), 2)
        self.assertEqual(other.calls, [])

    def test_completion_only_repair_is_not_terminal_and_recovers_without_model(self):
        runner = QuoteRunner(completion=True)
        extract_supplier_terms(self.memory, [item()], runner)
        hint = self.question()
        path = self.memory.root / "ai_jobs" / hint["job_id"] / "result.json"
        path.unlink()  # Disposable synthetic reply: simulate lost canonical result save.
        # The original memory already has an assessment; ask the resolver
        # directly so accepted-record reuse cannot hide completion recovery.
        pending = self.question()
        self.assertEqual(pending["state"], "waiting")
        self.assertIsNone(pending["completed_at"])
        recovered = resolve_supplier_question(self.memory, item())
        self.assertTrue(recovered["local_ready"])
        self.assertIsNotNone(recovered["completed_at"])
        self.assertTrue(path.exists())
        self.assertEqual(len(runner.calls), 2)

    def test_bad_original_hash_refuses_any_correction(self):
        runner = QuoteRunner(); jid = self.seed(runner)
        path = self.memory.root / "ai_jobs" / jid / "result.json"
        saved = load_json(path); saved["request_hash"] = "0" * 64; atomic_json(path, saved)
        result = extract_supplier_terms(self.memory, [item()], runner)
        self.assertEqual(result["failures"]["bad"], "SUPPLIER_QUOTE_REPAIR_JOB_CONFLICT")
        self.assertEqual(len(runner.calls), 1)

    def test_original_intent_and_binding_conflicts_are_not_correction_authority(self):
        runner = QuoteRunner(); jid = self.seed(runner)
        state = self.memory.snapshot()
        binding = next(iter(state["jobs"][jid]["binding_keys"]))
        state["jobs"][jid]["binding_keys"][binding] = "0" * 64
        with self.assertRaisesRegex(SafetyError, "^SUPPLIER_QUOTE_REPAIR_JOB_CONFLICT$"):
            supplier_model_question(self.memory, item(), state=state)
        path = self.memory.root / "ai_jobs" / jid / "intent.json"
        intent = load_json(path); intent["request"]["runner"] = "not-the-owner"; atomic_json(path, intent)
        result = extract_supplier_terms(self.memory, [item()], runner)
        self.assertEqual(result["failures"]["bad"], "SUPPLIER_QUOTE_REPAIR_JOB_CONFLICT")
        self.assertEqual(len(runner.calls), 1)

    def test_multiple_actual_original_owners_refuse_instead_of_choosing_or_repairing(self):
        first, second = QuoteRunner(), QuoteRunner(identity="second-original-owner")
        self.seed(first); self.seed(second)
        before = self.memory.snapshot_with_provenance()
        result = extract_supplier_terms(self.memory, [item()], first)
        self.assertEqual(result["failures"]["bad"], "SUPPLIER_QUOTE_REPAIR_ORIGINAL_CONFLICT")
        self.assertEqual(len(first.calls), 1)
        self.assertEqual(len(second.calls), 1)
        self.assertEqual(self.memory.snapshot_with_provenance(), before)

    def test_tampered_claim_is_not_another_runner_or_correction_authority(self):
        runner = QuoteRunner(); self.seed(runner)
        with patch.object(Jobs, "run", side_effect=SafetyError("WORK_TURN_MODEL_LIMIT")):
            extract_supplier_terms(self.memory, [item()], runner)
        hint = self.question()
        path = _claim_path(self.memory, hint["scope_id"])
        claim = load_json(path)
        claim["request"]["payload"]["supplier_repair"]["rules"] = "different"
        atomic_json(path, claim)
        before = self.memory.snapshot_with_provenance()
        result = extract_supplier_terms(self.memory, [item()], runner)
        self.assertEqual(result["failures"]["bad"], "SUPPLIER_QUOTE_REPAIR_OWNER_CONFLICT")
        self.assertEqual(len(runner.calls), 1)
        self.assertEqual(self.memory.snapshot_with_provenance(), before)

    def test_owner_interrupt_propagates_and_saved_original_and_good_sibling_recover(self):
        for error in (KeyboardInterrupt(), SystemExit(130)):
            with self.subTest(error=type(error).__name__):
                with tempfile.TemporaryDirectory() as folder:
                    memory = ResearchMemory(Path(folder) / "research", Journal(Path(folder) / "finance"), evidence_type="synthetic")
                    runner = QuoteRunner(repair_error=error)
                    with self.assertRaises(type(error)):
                        extract_supplier_terms(memory, [item(), item("good", bad=False)], runner)
                    original_job = next(jid for jid, row in memory.snapshot()["jobs"].items()
                        if "supplier_repair" not in row["request"]["payload"])
                    original_path = memory.root / "ai_jobs" / original_job / "result.json"
                    raw = original_path.read_bytes()
                    other = QuoteRunner(identity="after-interruption")
                    resumed = extract_supplier_terms(memory, [item("good", bad=False), item()], other)
                    self.assertIn("good", resumed["accepted"])
                    self.assertEqual(resumed["failures"]["bad"], "AI_JOB_PENDING_RECONCILIATION")
                    self.assertEqual(other.calls, [])
                    self.assertEqual(original_path.read_bytes(), raw)
                    self.assertEqual(len(runner.calls), 2)

    def test_no_scope_or_financial_lock_is_held_during_corrective_runner(self):
        memory = self.memory
        class LockChecking(QuoteRunner):
            def __call__(self, role, payload, folder):
                if "supplier_repair" in payload:
                    scope = payload["supplier_repair"]["scope_id"]
                    with Journal(_claim_path(memory, scope).parent).locked(timeout=0.1):
                        with Journal(memory.journal.root).locked(timeout=0.1):
                            pass
                return super().__call__(role, payload, folder)
        runner = LockChecking()
        result = extract_supplier_terms(self.memory, [item()], runner)
        self.assertIn("bad", result["accepted"])
        self.assertEqual(len(runner.calls), 2)

    def test_corrective_original_is_revalidated_inside_guarded_commit(self):
        runner = QuoteRunner()
        real_commit = self.memory.commit
        def race(event_id, changes, **kwargs):
            if changes[0]["table"] == "supplier_assessments":
                hint = self.question()
                path = self.memory.root / "ai_jobs" / hint["original_job_id"] / "result.json"
                saved = load_json(path)
                saved["response"]["results"][0]["terms"][0]["reason"] = "Changed original after validation"
                atomic_json(path, saved)
            return real_commit(event_id, changes, **kwargs)
        with patch.object(self.memory, "commit", side_effect=race):
            result = extract_supplier_terms(self.memory, [item()], runner, current_guard=lambda original, state: None)
        self.assertEqual(result["failures"]["bad"], "SUPPLIER_QUOTE_REPAIR_ORIGINAL_CHANGED")
        self.assertEqual(self.memory.snapshot()["supplier_assessments"], {})
        self.assertEqual(len(runner.calls), 2)

    def test_malformed_completed_correction_is_terminal_not_an_execution_retry(self):
        class Malformed(QuoteRunner):
            def __call__(self, role, payload, folder):
                response, usage = super().__call__(role, payload, folder)
                return ({"bad_envelope": True} if "supplier_repair" in payload else response), usage
        runner = Malformed()
        result = extract_supplier_terms(self.memory, [item()], runner)
        self.assertEqual(result["failures"]["bad"], "AI_ENVELOPE_INVALID")
        hint = self.question()
        self.assertEqual(hint["state"], "terminal")
        self.assertEqual(hint["failure"], "AI_ENVELOPE_INVALID")
        self.assertIsNotNone(hint["completed_at"])
        replay = extract_supplier_terms(self.memory, [item()], runner)
        self.assertEqual(replay["failures"]["bad"], "AI_ENVELOPE_INVALID")
        self.assertEqual(len(runner.calls), 2)

    def test_prepared_retry_pointer_cannot_repeat_a_completed_rejected_correction(self):
        runner = QuoteRunner(reject_repair=True)
        extract_supplier_terms(self.memory, [item()], runner)
        hint = self.question()
        base = self.memory.root / "ai_jobs" / hint["job_id"]
        original = (base / "result.json").read_bytes()
        atomic_json(base / "retries.json", [{"reason": "Synthetic deliberate retry", "requested_at": NOW}])
        self.assertEqual(self.question()["state"], "terminal")
        extract_supplier_terms(self.memory, [item()], runner)
        self.assertEqual(len(runner.calls), 2)
        self.assertFalse((base / "retry-1" / "intent.json").exists())
        self.assertEqual((base / "result.json").read_bytes(), original)

    def test_runner_pause_prevents_corrective_intent_and_resumes_same_claim(self):
        runner = QuoteRunner(); self.seed(runner)
        runner.check_new_execution = lambda: (_ for _ in ()).throw(SafetyError("AI_EXECUTION_WAIT"))
        result = extract_supplier_terms(self.memory, [item()], runner)
        self.assertEqual(result["failures"]["bad"], "AI_EXECUTION_WAIT")
        hint = self.question()
        self.assertEqual(hint["state"], "repair")
        self.assertFalse((self.memory.root / "ai_jobs" / hint["job_id"] / "intent.json").exists())
        self.assertEqual(len(runner.calls), 1)
        runner.check_new_execution = lambda: None
        result = extract_supplier_terms(self.memory, [item()], runner)
        self.assertIn("bad", result["accepted"])
        self.assertEqual(len(runner.calls), 2)

    def test_scope_violation_cannot_drop_quote_or_change_claim(self):
        for alteration in (lambda row: row["terms"][0]["quotes"].pop(),
                           lambda row: row["relevance"].update(relationship="exact_product"),
                           lambda row: row["terms"][0].update(value="No")):
            with self.subTest(alteration=alteration):
                with tempfile.TemporaryDirectory() as folder:
                    memory = ResearchMemory(Path(folder) / "research", Journal(Path(folder) / "finance"), evidence_type="synthetic")
                    runner = QuoteRunner(alteration=alteration)
                    result = extract_supplier_terms(memory, [item()], runner)
                    self.assertEqual(result["failures"]["bad"], "AI_SUPPLIER_QUOTE_REPAIR_SCOPE_INVALID")
                    hint = supplier_model_question(memory, item(), state=memory.snapshot())
                    self.assertEqual(hint["state"], "terminal")
                    self.assertEqual(memory.snapshot()["supplier_assessments"], {})
                    extract_supplier_terms(memory, [item()], runner)
                    self.assertEqual(len(runner.calls), 2)

    def test_current_guard_blocks_stale_input_before_correction_dispatch(self):
        runner = QuoteRunner(); self.seed(runner)
        def stale(original, state):
            raise SafetyError("SUPPLIER_CAPTURE_ORIGINAL_CHANGED")
        hint = self.question()
        result = extract_supplier_terms(self.memory, [item()], runner, current_guard=stale)
        self.assertEqual(result["failures"]["bad"], "SUPPLIER_CAPTURE_ORIGINAL_CHANGED")
        self.assertEqual(len(runner.calls), 1)
        self.assertFalse(_claim_path(self.memory, hint["scope_id"]).exists())

    def test_original_guard_rechecks_after_alias_save_before_any_intent(self):
        runner = QuoteRunner()
        original = item("good", bad=False)
        actual_save = _save_supplier_aliases
        seen = []

        def guard(supplied, state):
            seen.append(state["batch_runs"].get("changed-input") is not None)
            self.assertEqual(supplied, original)
            if seen[-1]:
                raise SafetyError("SUPPLIER_CAPTURE_ORIGINAL_CHANGED")

        def change_after_aliases(*args, **kwargs):
            result = actual_save(*args, **kwargs)
            self.memory.put("batch_runs", "changed-input", {"changed": True})
            return result

        with patch("product_radar.suppliers._save_supplier_aliases", side_effect=change_after_aliases):
            result = extract_supplier_terms(self.memory, [original], runner, current_guard=guard)
        self.assertEqual(result["accepted"], {})
        self.assertEqual(result["failures"], {"good": "SUPPLIER_CAPTURE_ORIGINAL_CHANGED"})
        self.assertIn(False, seen)
        self.assertTrue(seen[-1])
        self.assertEqual(runner.calls, [])
        state = self.memory.snapshot()
        self.assertEqual(len(state["jobs"]), 1)
        self.assertEqual(state["supplier_assessments"], {})
        for job_id in state["jobs"]:
            self.assertFalse((self.memory.root / "ai_jobs" / job_id / "intent.json").exists())

    def test_original_guard_rechecks_after_cooperative_wrapper_preparation(self):
        from product_radar import model_recovery
        runner = QuoteRunner()
        runner.closed_retry_policy = {"as_of": lambda: NOW, "retry_seconds": 300,
                                      "retry_not_before": lambda: None}
        actual_scope = model_recovery._current_scope
        scope_calls = []

        def guard(original, state):
            if state["batch_runs"].get("changed-input"):
                raise SafetyError("SUPPLIER_CAPTURE_ORIGINAL_CHANGED")

        def change_after_scope(*args, **kwargs):
            scope = actual_scope(*args, **kwargs)
            self.assertIsNotNone(scope)
            scope_calls.append(scope)
            self.memory.put("batch_runs", "changed-input", {"changed": True})
            return scope

        turn = open_work_turn(self.memory, "supplier-original-guard", at=NOW)
        with bind_work_turn(self.memory, turn["id"]):
            with patch.object(model_recovery, "_current_scope", side_effect=change_after_scope):
                result = extract_supplier_terms(self.memory, [item("good", bad=False)], runner,
                                                current_guard=guard)
        self.assertEqual(len(scope_calls), 1)
        self.assertEqual(result["accepted"], {})
        self.assertEqual(result["failures"], {"good": "SUPPLIER_CAPTURE_ORIGINAL_CHANGED"})
        self.assertEqual(runner.calls, [])
        state = self.memory.snapshot()
        self.assertEqual(state["supplier_assessments"], {})
        self.assertEqual(state["batch_runs"][turn["id"]]["counters"]["model_executions"], 0)
        for path in (self.memory.root / "ai_jobs").glob("*/intent.json"):
            self.fail("The changed source must be refused before intent: " + path.name)

    def test_original_guard_refuses_unselected_sibling_in_prepared_original(self):
        runner = QuoteRunner()
        originals = [item("good", bad=False), item("other", bad=False)]
        payload = _supplier_payload(originals)
        _, refs = _supplier_aliases(originals)
        original_id = _save_supplier_aliases(self.memory, runner, payload, refs)

        def refuse_preparation(request, folder):
            self.assertFalse((folder / "intent.json").exists())
            raise SafetyError("SYNTHETIC_PREPARED_ONLY")

        with self.assertRaisesRegex(SafetyError, "^SYNTHETIC_PREPARED_ONLY$"):
            Jobs(self.memory, runner, before_new_execution=refuse_preparation).run(
                "F", payload, declared_version="research-f-v3")
        guarded = []
        result = extract_supplier_terms(self.memory, [originals[0]], runner,
            current_guard=lambda original, state: guarded.append(original["id"]))
        self.assertEqual(result["accepted"], {})
        self.assertEqual(result["failures"], {"good": "AI_JOB_PENDING_RECONCILIATION"})
        self.assertEqual(set(guarded), {"good"})
        self.assertEqual(runner.calls, [])
        self.assertEqual(self.memory.snapshot()["supplier_assessments"], {})
        self.assertFalse((self.memory.root / "ai_jobs" / original_id / "intent.json").exists())
        self.assertEqual(len(self.memory.snapshot()["jobs"]), 1)

    def test_current_guard_rechecks_fresh_state_inside_assessment_commit(self):
        runner = QuoteRunner()
        real_commit = self.memory.commit
        def guard(original, state):
            if state["batch_runs"].get("changed-input"):
                raise SafetyError("SUPPLIER_CAPTURE_ORIGINAL_CHANGED")
        def race(event_id, changes, **kwargs):
            if changes[0]["table"] == "supplier_assessments":
                self.memory.put("batch_runs", "changed-input", {"changed": True})
            return real_commit(event_id, changes, **kwargs)
        with patch.object(self.memory, "commit", side_effect=race):
            result = extract_supplier_terms(self.memory, [item()], runner, current_guard=guard)
        self.assertEqual(result["failures"]["bad"], "SUPPLIER_CAPTURE_ORIGINAL_CHANGED")
        self.assertEqual(self.memory.snapshot()["supplier_assessments"], {})
        self.assertEqual(len(runner.calls), 2)

    def test_existing_accepted_assessment_is_not_reused_when_current_guard_fails(self):
        runner = QuoteRunner()
        extract_supplier_terms(self.memory, [item()], runner)
        before = deepcopy(self.memory.snapshot()["supplier_assessments"])
        def stale(original, state):
            raise SafetyError("SUPPLIER_CAPTURE_ORIGINAL_CHANGED")
        result = extract_supplier_terms(self.memory, [item()], runner, current_guard=stale)
        self.assertEqual(result["accepted"], {})
        self.assertEqual(result["reused"], [])
        self.assertEqual(result["failures"]["bad"], "SUPPLIER_CAPTURE_ORIGINAL_CHANGED")
        self.assertEqual(self.memory.snapshot()["supplier_assessments"], before)
        self.assertEqual(len(runner.calls), 2)

    def test_recaptured_identical_words_keep_dates_but_do_not_repeat_correction(self):
        runner = QuoteRunner()
        first_dates = {"bad": {"page-bad": "2026-10-10T08:00:00+00:00"}}
        new_dates = {"bad": {"page-bad": "2026-10-11T08:00:00+00:00"}}
        extract_supplier_terms(self.memory, [item()], runner, source_dates=first_dates)
        first_scope = self.question()["scope_id"]
        result = extract_supplier_terms(self.memory, [item()], runner, source_dates=new_dates)
        self.assertIn("bad", result["accepted"])
        records = self.memory.snapshot()["supplier_assessments"]
        self.assertEqual({tuple(row["source_dates"].values()) for row in records.values()},
                         {tuple(first_dates["bad"].values()), tuple(new_dates["bad"].values())})
        self.assertEqual(self.question()["scope_id"], first_scope)
        self.assertEqual(len(runner.calls), 2)
        self.assertNotIn("source_dates", runner.calls[1])

    def test_spent_turn_preserves_counter_then_next_turn_services_one_repair(self):
        runner = QuoteRunner(); self.seed(runner)
        policy = deepcopy(DEFAULT_POLICY); policy.update(model_executions=1, report_slots=0)
        turn = open_work_turn(self.memory, "quote-programme", at=NOW, policy=policy)
        with bind_work_turn(self.memory, turn["id"]):
            Jobs(self.memory, turn_fixtures.Runner()).run("A", turn_fixtures.payload("spent"))
            result = extract_supplier_terms(self.memory, [item()], runner)
        self.assertEqual(result["failures"]["bad"], "WORK_TURN_MODEL_LIMIT")
        before = self.memory.snapshot()["batch_runs"][turn["id"]]
        self.assertEqual(before["counters"]["model_executions"], 1)
        self.assertEqual(before["counters"]["purpose_items"]["F"], 0)
        close_work_turn(self.memory, turn["id"], at=NOW, reason="Synthetic completed turn")
        next_turn = open_work_turn(self.memory, "quote-programme", at=NOW, policy=policy)
        with bind_work_turn(self.memory, next_turn["id"]):
            result = extract_supplier_terms(self.memory, [item()], runner)
        self.assertIn("bad", result["accepted"])
        after = self.memory.snapshot()["batch_runs"][next_turn["id"]]
        self.assertEqual(after["counters"]["model_executions"], 1)
        self.assertEqual(after["counters"]["purpose_items"]["F"], 1)
        self.assertEqual(after["counters"]["source_callbacks"], 0)
        self.assertEqual(len(runner.calls), 2)


if __name__ == "__main__":
    unittest.main()
