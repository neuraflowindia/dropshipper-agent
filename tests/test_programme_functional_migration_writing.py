"""Saved-only old report writing during migration; disposable real job proofs."""

from copy import deepcopy
from datetime import timedelta
from pathlib import Path
import unittest
from unittest.mock import patch

import product_radar.programme as programme
from product_radar.live_outputs import build_issue_dataset
from product_radar.models import SafetyError, digest, timestamp
from product_radar.report_narration import (LEGACY_MIGRATION_WAIT, generate_report_narratives,
    make_narrative_input, saved_legacy_narratives)
from product_radar.report_workflow import prepare_review_package
from product_radar.research_ai import Jobs, current_attempt
from product_radar.storage import atomic_json, load_json
from product_radar.work_turns import (DEFAULT_POLICY, bind_work_turn, close_work_turn,
    open_work_turn)
import test_programme_reports as legacy
import test_report_narration as narration
import test_work_turns as turns


class CooperativeMigrationWritingTests(unittest.TestCase):
    def setUp(self):
        self.case = legacy.ProgrammeReportsTests(); self.case.setUp()
        self.addCleanup(self.case.doCleanups)
        self.case.seed(10)
        self.record = self.case.record

    def context(self, **changes):
        return dict({"at": self.case.as_of, "root": self.case.root, "runner": self.case.writer,
            "render_pdf": self.case.render, "inspect_pdf": legacy.inspect,
            "retry_seconds": 300, "cooperative_mode": True}, **changes)

    def reports(self, **changes):
        self.older_packet()
        return programme._reports(self.case.memory, self.record, self.context(**changes))

    def older_packet(self):
        """An earlier non-cooperative run froze and reserved this legacy packet.

        Explicit turns no longer allocate older-format stock to any date; they
        only finish an existing older packet from saved writing.
        """
        if (self.case.folder() / "package-input.json").exists():
            return
        from product_radar.issue_allocation import allocate_issues, allocation_snapshot_hash, reserve_issue
        memory = self.case.memory; state = memory.snapshot()
        pool = programme._programme_report_pool(memory, self.record, as_of=self.case.as_of, functional_packets=False)
        eligible = programme._reporting_eligible(self.record, state, pool)
        issue = allocate_issues(eligible, self.record["release_slots"], state["published_history"],
            state["issue_reservations"], identity_state=state)["issues"][0]
        facts = [pool["facts_by_concept"][row["concept_id"]] for row in issue["products"]]
        issue = programme._report_revision(self.case.root, issue, facts, memory.evidence_type)
        reserve_issue(memory, issue, expected_input_hash=issue["input_hash"],
            expected_snapshot_hash=allocation_snapshot_hash(memory.snapshot()), as_of=self.case.as_of)

    def test_explicit_turn_never_freezes_a_new_older_format_packet(self):
        before = self.case.memory.snapshot()
        result = programme._reports(self.case.memory, self.record, self.context())
        self.assertEqual(result["eligible_products"], 10)  # Older-format stock is still visible...
        for slot in self.case.slots:  # ...but never refills a release date in an explicit turn.
            self.assertFalse((self.case.root / "data/outputs" / slot["issue_id"]).exists())
            draft = self.record["drafts"][slot["issue_id"]]
            if timestamp(slot["scheduled_at"]) <= timestamp(self.case.as_of):
                self.assertEqual(draft["state"], "release_passed")  # No new set after its release time.
            else:
                self.assertEqual(draft["products"], 0)
        self.assertEqual(self.case.memory.snapshot()["issue_reservations"], before["issue_reservations"])
        self.assertEqual(self.case.writer.calls, 0)

    def frozen(self):
        ref = self.case.slots[0]["issue_id"]
        source = load_json(self.case.folder() / "package-input.json", 8 * 1024 * 1024)
        return ref, source, build_issue_dataset(source["issue"], source["product_facts"],
            evidence_type=self.case.memory.evidence_type)

    def writing(self, **changes):
        return programme._frozen_writing_ready(self.case.memory, self.case.memory.snapshot(), self.record,
            root=self.case.root, at=self.case.as_of, detailed=True, runner=self.case.writer,
            allow_fresh_legacy_model=False, **changes)

    def ready(self):
        return programme.programme_turn_readiness(self.case.memory, self.record, root=self.case.root,
            handlers={}, runner=self.case.writer, as_of=self.case.as_of, state=self.case.memory.snapshot())

    def cache(self, *, languages=("en", "hi-Latn")):
        ref, source, dataset = self.frozen()
        result = generate_report_narratives(self.case.memory, source["issue"], dataset,
            self.case.writer, languages=languages)
        self.assertEqual(result["complete_languages"], list(languages))
        return result

    def closed_checkpoint(self, *, spent=False):
        policy = deepcopy(DEFAULT_POLICY); policy.update(model_executions=1, report_slots=0)
        turn = open_work_turn(self.case.memory, self.record["programme_id"], at=self.case.as_of, policy=policy)
        if spent:
            with bind_work_turn(self.case.memory, turn["id"]):
                Jobs(self.case.memory, turns.Runner()).run("A", turns.payload("migration-spent-proof"))
        close_work_turn(self.case.memory, turn["id"], at=self.case.as_of, reason="Disposable migration test")
        before = self.ready()
        self.record["cooperative_checkpoint"] = {"version": 1, "continue_ready": False,
            "wake_hash": before["wake_hash"], "work_signature": before["work_signature"],
            "turn_sequence": turn["turn_sequence"]}
        return turn

    def test_cooperative_missing_legacy_writes_no_jobs_intents_problems_or_model_calls(self):
        before = deepcopy(self.case.memory.snapshot())
        with patch("product_radar.report_narration.run_current_model_job") as dispatch:
            first = self.reports()
        dispatch.assert_not_called()
        ref, source, dataset = self.frozen()
        draft = self.record["drafts"][ref]
        self.assertEqual(draft["state"], "narration_pending")
        self.assertEqual(draft["reason"], LEGACY_MIGRATION_WAIT)
        self.assertEqual(self.case.writer.calls, 0)
        after = self.case.memory.snapshot()
        self.assertEqual(after["jobs"], before["jobs"])
        self.assertEqual(after["problems"], before["problems"])
        self.assertFalse(list((self.case.memory.root / "ai_jobs").rglob("intent.json")))
        self.assertNotIn("reports:" + ref, self.record["deferrals"])
        self.assertFalse(self.writing()["ready"])
        self.assertFalse(self.writing()["local_ready"])
        self.assertEqual(self.writing()["model_inputs"], [])
        self.assertFalse(self.ready()["report_ready"])
        self.reports()
        self.assertEqual(self.case.memory.snapshot()["jobs"], before["jobs"])
        self.assertFalse(first["client_publication_attempted"])

    def test_partial_english_is_not_local_ready_or_a_closed_allowance_continuation(self):
        self.reports()
        _ref, source, dataset = self.frozen()
        payload = make_narrative_input(dataset, "en")
        Jobs(self.case.memory, self.case.writer).run("G", dict(payload, items=payload["items"][:5]))
        turn = self.closed_checkpoint(spent=True)
        hint = programme._closed_model_allowance_continuation(self.case.memory, self.case.memory.snapshot(),
            self.record, runner=self.case.writer, at=self.case.as_of, retry_seconds=300, model_wait=False,
            semantic_hints=[], report_inputs=[{"dataset": dataset, "languages": ("en", "hi-Latn")}],
            source_questions={}, update_questions=[], allow_fresh_legacy_model=False)
        self.assertIsNone(hint)
        self.assertFalse(self.writing()["local_ready"])
        current = self.ready()
        self.assertFalse(current["report_ready"])
        # Genuine unfinished definitions may still use the next allowance;
        # missing old prose must not be the witness or steal its G slot.
        self.assertNotEqual((current["model_allowance_continuation"] or {}).get("purpose"), "G")
        self.assertEqual(self.case.memory.snapshot()["batch_runs"][turn["id"]]["state"], "closed")

    def test_complete_cached_english_renders_once_then_missing_hinglish_settles(self):
        self.reports(); self.cache(languages=("en",))
        self.assertTrue(self.writing()["local_ready"])
        calls = self.case.writer.calls
        self.reports()
        self.assertEqual(self.case.writer.calls, calls)
        self.assertEqual(len(self.case.renders), 1)
        self.assertFalse(self.writing()["local_ready"])
        self.assertNotIn("reports:" + self.case.slots[0]["issue_id"], self.record["deferrals"])
        self.closed_checkpoint()
        self.assertFalse(self.ready()["ready"])
        self.reports()
        self.assertEqual(len(self.case.renders), 1)

    def test_exact_cached_bilingual_nondefault_runner_finishes_without_dispatch(self):
        self.reports(); self.cache()
        self.assertNotEqual(self.case.writer.identity, "codex-chatgpt-high-v1")
        before_jobs = deepcopy(self.case.memory.snapshot()["jobs"])
        calls = self.case.writer.calls
        with patch("product_radar.report_narration.run_current_model_job") as dispatch:
            self.reports()
        dispatch.assert_not_called()
        ref, source, _dataset = self.frozen()
        self.assertTrue(self.record["drafts"][ref]["complete"])
        self.assertEqual(self.case.writer.calls, calls)
        self.assertEqual(self.case.memory.snapshot()["jobs"], before_jobs)
        self.assertEqual(load_json(self.case.folder() / "package-input.json", 8 * 1024 * 1024), source)
        self.assertEqual(len(self.case.renders), 2)
        self.assertFalse(self.writing()["local_ready"])
        self.assertFalse(self.writing()["ready"])

    def test_cached_english_failed_renderer_keeps_exact_retry_while_hinglish_missing(self):
        self.reports(); self.cache(languages=("en",))
        calls = self.case.writer.calls
        def failed_renderer(*_args):
            raise SafetyError("SYNTHETIC_CACHED_EN_RENDER_FAILED")
        self.reports(render_pdf=failed_renderer)
        ref = self.case.slots[0]["issue_id"]
        self.assertEqual(self.record["drafts"][ref]["state"], "narration_pending")
        self.assertEqual(self.record["drafts"][ref]["reason"], "SYNTHETIC_CACHED_EN_RENDER_FAILED")
        wait = self.record["deferrals"]["reports:" + ref]
        self.assertEqual(wait["reason"], "SYNTHETIC_CACHED_EN_RENDER_FAILED")
        self.assertGreater(timestamp(wait["next_retry_at"]), timestamp(self.case.as_of))
        self.assertFalse(self.writing()["local_ready"])
        self.closed_checkpoint()
        self.assertFalse(self.ready()["ready"])
        self.reports()
        self.assertEqual(len(self.case.renders), 0)
        self.case.as_of = (timestamp(self.case.as_of) + timedelta(seconds=301)).isoformat()
        self.assertTrue(self.writing()["local_ready"])
        self.reports()
        self.assertEqual(len(self.case.renders), 1)
        self.assertFalse(self.writing()["local_ready"])
        self.assertEqual(self.case.writer.calls, calls)

    def test_damaged_existing_cached_english_is_not_mistaken_for_settled_bytes(self):
        self.reports(); self.cache(languages=("en",)); self.reports()
        self.assertFalse(self.writing()["local_ready"])
        status = load_json(self.case.folder() / "package-status.json", 512 * 1024)
        pdf = Path(status["stage_outcomes"]["editions"]["en"]["pdf"]["path"])
        from product_radar.storage import atomic_bytes
        atomic_bytes(pdf, b"not the originally rendered PDF")
        self.assertTrue(self.writing()["local_ready"])
        calls = self.case.writer.calls
        self.reports()
        ref = self.case.slots[0]["issue_id"]
        self.assertEqual(self.record["deferrals"]["reports:" + ref]["reason"], "REPORT_LOCAL_STAGE_FAILED")
        self.assertFalse(self.writing()["local_ready"])
        self.assertEqual(self.case.writer.calls, calls)

    def test_saved_original_completion_is_read_without_rewriting_its_files(self):
        self.reports(); cached = self.cache()
        job_id = cached["job_ids"][0]
        folder = current_attempt(self.case.memory.root / "ai_jobs" / job_id)
        atomic_json(folder / "completion.json", load_json(folder / "result.json", 128 * 1024))
        (folder / "result.json").unlink()
        before = {p: p.read_bytes() for p in (self.case.memory.root / "ai_jobs").rglob("*") if p.is_file()}
        _ref, source, dataset = self.frozen()
        result = generate_report_narratives(self.case.memory, source["issue"], dataset, self.case.writer,
            allow_fresh_legacy_model=False)
        self.assertEqual(result["complete_languages"], ["en", "hi-Latn"])
        self.assertEqual({p: p.read_bytes() for p in (self.case.memory.root / "ai_jobs").rglob("*") if p.is_file()}, before)

    def test_unknown_original_remains_pending_not_fresh_migration_permission(self):
        self.reports()
        _ref, source, dataset = self.frozen()
        class Interrupted:
            identity = self.case.writer.identity
            def __call__(self, *_args):
                raise SafetyError("AI_JOB_PENDING_RECONCILIATION")
        payload = make_narrative_input(dataset, "en")
        with self.assertRaises(SafetyError):
            Jobs(self.case.memory, Interrupted()).run("G", dict(payload, items=payload["items"][:1]))
        before = deepcopy(self.case.memory.snapshot())
        with patch("product_radar.report_narration.run_current_model_job") as dispatch:
            result = saved_legacy_narratives(self.case.memory, source["issue"], dataset, self.case.writer)
        dispatch.assert_not_called()
        self.assertEqual(result["failures"]["en"][payload["items"][0]["id"]], "AI_JOB_PENDING_RECONCILIATION")
        self.assertEqual(self.case.memory.snapshot(), before)
        self.assertFalse(self.writing()["local_ready"])

    def test_standalone_default_still_writes_the_same_legacy_packet(self):
        result = self.case.advance(max_cycles=1)
        self.assertGreater(self.case.writer.calls, 0)
        self.assertTrue(self.case.saved()["drafts"][self.case.slots[0]["issue_id"]]["complete"])
        self.assertEqual(len(self.case.renders), 2)
        self.assertIsNotNone(result)

    def test_existing_open_turn_flag_policy_and_counters_are_not_rewritten(self):
        self.reports()
        turn = open_work_turn(self.case.memory, self.record["programme_id"], at=self.case.as_of, report_ready=True)
        with bind_work_turn(self.case.memory, turn["id"]):
            Jobs(self.case.memory, turns.Runner()).run("A", turns.payload("already-open-migration-turn"))
        original = deepcopy(self.case.memory.snapshot()["batch_runs"][turn["id"]])
        self.assertTrue(self.ready()["ready"])
        self.assertFalse(self.ready()["report_ready"])
        with bind_work_turn(self.case.memory, turn["id"]):
            self.reports()
        self.assertEqual(self.case.memory.snapshot()["batch_runs"][turn["id"]], original)
        self.assertTrue(original["report_ready"])
        self.assertEqual(original["counters"]["model_executions"], 1)

    def test_malformed_policy_is_rejected_before_saved_or_model_work(self):
        self.reports()
        _ref, source, dataset = self.frozen()
        for value in (None, 0, 1, "no"):
            with self.subTest(value=value), self.assertRaisesRegex(SafetyError, "REPORT_NARRATION_OPTIONS_INVALID"):
                generate_report_narratives(self.case.memory, source["issue"], dataset,
                    self.case.writer, allow_fresh_legacy_model=value)
            with self.subTest(package=value), self.assertRaisesRegex(SafetyError, "REPORT_NARRATION_OPTIONS_INVALID"):
                prepare_review_package(self.case.memory, source["issue"], source["product_facts"],
                    root=self.case.root, runner=self.case.writer, allow_fresh_legacy_model=value)


class SavedLegacyCorrectionMigrationTests(unittest.TestCase):
    def setUp(self):
        self.case = narration.ReportNarrationTests(); self.case.setUp()
        self.addCleanup(self.case.doCleanups)

    def files(self):
        return {p: p.read_bytes() for p in (self.case.memory.root / "ai_jobs").rglob("*") if p.is_file()}

    def test_valid_saved_correction_reuses_exact_owner_without_runner_or_problem_writes(self):
        runner = narration.RomanCorrectionRunner(self.case.dataset["products"][0]["concept_id"])
        first = self.case.narrate(runner)
        self.assertEqual(first["failures"], {})
        before, files, calls = deepcopy(self.case.memory.snapshot()), self.files(), len(runner.calls)
        with patch("product_radar.report_narration.run_current_model_job") as dispatch:
            again = generate_report_narratives(self.case.memory, self.case.issue, self.case.dataset, runner,
                allow_fresh_legacy_model=False)
        dispatch.assert_not_called()
        self.assertEqual(again["dataset"], first["dataset"])
        self.assertEqual(self.case.memory.snapshot(), before)
        self.assertEqual(self.files(), files)
        self.assertEqual(len(runner.calls), calls)

    def test_missing_correction_keeps_real_rejection_without_new_owner(self):
        runner = narration.RomanCorrectionRunner(self.case.dataset["products"][0]["concept_id"])
        with patch("product_radar.report_narration._run_item_correction", return_value=(None, None)):
            first = self.case.narrate(runner)
        before, files = deepcopy(self.case.memory.snapshot()), self.files()
        again = generate_report_narratives(self.case.memory, self.case.issue, self.case.dataset, runner,
            allow_fresh_legacy_model=False)
        cid = self.case.dataset["products"][0]["concept_id"]
        self.assertEqual(again["failures"]["hi-Latn"][cid], narration.ROMAN_REPAIR_ERROR)
        self.assertEqual(first["failures"]["hi-Latn"][cid], again["failures"]["hi-Latn"][cid])
        self.assertEqual(self.case.memory.snapshot(), before)
        self.assertEqual(self.files(), files)

    def test_completed_failed_correction_is_final_and_not_dispatched_again(self):
        def reject(_item, value):
            value["references"] = ["invented-fact"]
        runner = narration.RomanCorrectionRunner(self.case.dataset["products"][0]["concept_id"],
            correction_mutation=reject)
        first = self.case.narrate(runner)
        self.assertTrue(first["failures"])
        before, calls, files = deepcopy(self.case.memory.snapshot()), len(runner.calls), self.files()
        again = generate_report_narratives(self.case.memory, self.case.issue, self.case.dataset, runner,
            allow_fresh_legacy_model=False)
        self.assertEqual(again["failures"], first["failures"])
        self.assertEqual(self.case.memory.snapshot(), before)
        self.assertEqual(self.files(), files)
        self.assertEqual(len(runner.calls), calls)

    def test_same_owner_sent_uncertain_correction_cannot_retry_inside_active_turn(self):
        class UncertainCorrection(narration.RomanCorrectionRunner):
            def __call__(self, role, payload, folder):
                if "narration_repair" in payload:
                    self.calls.append(deepcopy(payload))
                    raise SafetyError("AI_JOB_PENDING_RECONCILIATION")
                return super().__call__(role, payload, folder)
        cid = self.case.dataset["products"][0]["concept_id"]
        runner = UncertainCorrection(cid)
        first = self.case.narrate(runner)
        self.assertEqual(first["failures"]["hi-Latn"][cid], "AI_JOB_PENDING_RECONCILIATION")
        owners = [(ref, row) for ref, row in self.case.memory.snapshot()["jobs"].items()
            if row["request"]["payload"].get("narration_repair")]
        self.assertEqual(len(owners), 1)
        job_id, owner = owners[0]
        self.assertEqual(owner["request"]["runner"], runner.identity)
        folder = current_attempt(self.case.memory.root / "ai_jobs" / job_id)
        self.assertTrue((folder / "intent.json").exists())
        self.assertFalse((folder / "result.json").exists())
        self.assertFalse((folder / "completion.json").exists())
        # This makes the existing normal helper's same_live_owner condition
        # true. Saved-only policy must stop before that retry decision.
        runner.closed_retry_policy = {"retry_seconds": 300}
        turn = open_work_turn(self.case.memory, "sent-correction-migration", at=turns.NOW, report_ready=True)
        before, files, calls = deepcopy(self.case.memory.snapshot()), self.files(), len(runner.calls)
        with bind_work_turn(self.case.memory, turn["id"]), \
                patch("product_radar.report_narration.run_current_model_job") as dispatch:
            again = generate_report_narratives(self.case.memory, self.case.issue, self.case.dataset, runner,
                allow_fresh_legacy_model=False)
        dispatch.assert_not_called()
        self.assertEqual(again["failures"]["hi-Latn"][cid], "AI_JOB_PENDING_RECONCILIATION")
        self.assertEqual(again["complete_languages"], ["en"])
        self.assertEqual(self.case.memory.snapshot(), before)
        self.assertEqual(self.files(), files)
        self.assertEqual(len(runner.calls), calls)
        self.assertEqual(before["batch_runs"][turn["id"]]["counters"]["model_executions"], 0)
        close_work_turn(self.case.memory, turn["id"], at=turns.NOW, reason="Disposable saved-only correction proof")


class FunctionalMigrationWritingCompatibilityTests(unittest.TestCase):
    def test_functional_g_runs_and_its_exact_spent_continuation_remains_ready(self):
        import test_functional_source_market as functional
        case = legacy.ProgrammeReportsTests(); case.setUp(); self.addCleanup(case.doCleanups)
        memory, ids = functional.functional_pool_fixture(case.root, count=10)
        case.memory = memory; case.as_of = functional.NOW
        record = programme.prepare_programme(memory, "functional-migration", concept_ids=ids, as_of=case.as_of)
        policy = deepcopy(DEFAULT_POLICY); policy.update(model_executions=1, report_slots=0)
        turn = open_work_turn(memory, record["programme_id"], at=case.as_of, policy=policy)
        context = {"at": case.as_of, "root": case.root, "runner": case.writer,
            "render_pdf": case.render, "inspect_pdf": legacy.inspect, "retry_seconds": 300,
            "cooperative_mode": True}
        with bind_work_turn(memory, turn["id"]):
            result = programme._reports(memory, record, context)
        self.assertEqual(case.writer.calls, 1)
        source = load_json(case.root / "data/outputs" / record["release_slots"][0]["issue_id"] /
            "revision-1/package-input.json", 8 * 1024 * 1024)
        self.assertEqual(source["issue"]["recommendation_contract"], "functional-type-examples-v1")
        close_work_turn(memory, turn["id"], at=case.as_of, reason="Disposable functional writing continuation")
        # Keep the actual report retry boundary; the next due turn, not an
        # immediate bypass of its saved deadline, must retain functional G.
        case.as_of = (timestamp(case.as_of) + timedelta(seconds=301)).isoformat()
        writing = programme._frozen_writing_ready(memory, memory.snapshot(), record, root=case.root,
            at=case.as_of, detailed=True, runner=case.writer, allow_fresh_legacy_model=False)
        self.assertTrue(writing["ready"])
        self.assertEqual(len(writing["model_inputs"]), 1)
        self.assertEqual(writing["model_inputs"][0]["dataset"]["schema_version"], 3)
        continuation = programme._closed_model_allowance_continuation(memory, memory.snapshot(), record,
            runner=case.writer, at=case.as_of, retry_seconds=300, model_wait=False, semantic_hints=[],
            report_inputs=writing["model_inputs"], source_questions={}, update_questions=[],
            allow_fresh_legacy_model=False)
        self.assertEqual(continuation["purpose"], "G")
        self.assertEqual(continuation["turn_id"], turn["id"])
        self.assertFalse(result["client_publication_attempted"])

    def test_genuine_newer_cached_legacy_hold_recovers_once_without_model_work(self):
        import test_programme_frozen_hold_readiness as holds
        fixture = holds.LegacyHoldReadinessTests()
        self.addCleanup(fixture.doCleanups)
        case, old, state, _hold = fixture.newer_legacy()
        calls, renders = case.writer.calls, len(case.renders)
        before = deepcopy(case.packet_bytes(0, revision=2))
        writing = programme._frozen_writing_ready(case.memory, state, old, root=case.root,
            at=case.as_of, detailed=True, runner=case.writer, allow_fresh_legacy_model=False)
        self.assertTrue(writing["local_ready"])
        self.assertFalse(writing["ready"])
        self.assertEqual(writing["model_inputs"], [])
        programme._reports(case.memory, old, {"at": case.as_of, "root": case.root,
            "runner": case.writer, "render_pdf": case.render, "inspect_pdf": legacy.inspect,
            "retry_seconds": 300, "cooperative_mode": True})
        ref = old["release_slots"][0]["issue_id"]
        self.assertTrue(old["drafts"][ref]["complete"])
        self.assertEqual(old["drafts"][ref]["revision"], 2)
        self.assertEqual((case.writer.calls, len(case.renders)), (calls, renders))
        self.assertEqual(case.packet_bytes(0, revision=2), before)
        self.assertEqual(programme._frozen_writing_ready(case.memory, case.memory.snapshot(), old,
            root=case.root, at=case.as_of, detailed=True, runner=case.writer,
            allow_fresh_legacy_model=False), {"ready": False, "local_ready": False, "model_inputs": []})
if __name__ == "__main__":
    unittest.main()
