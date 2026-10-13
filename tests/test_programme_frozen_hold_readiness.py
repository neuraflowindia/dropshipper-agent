"""Exact newer checked report holds wake once, without live research or release."""

from copy import deepcopy
from datetime import timedelta
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import product_radar.programme as programme
from product_radar.models import SafetyError, digest, timestamp
from product_radar.research_ai import Jobs
from product_radar.service import load_policy
from product_radar.work_turns import DEFAULT_POLICY, bind_work_turn, close_work_turn, open_work_turn
import test_functional_source_market as functional
import test_programme_legacy_upgrade as upgrades
import test_programme_reports as legacy
import test_work_turns as turns


class FrozenHoldReadinessTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        temporary = tempfile.TemporaryDirectory(); cls.addClassCleanup(temporary.cleanup)
        memory, cls.functional_ids = functional.functional_pool_fixture(Path(temporary.name), count=10)
        cls.functional_state = memory.snapshot()

    def setUp(self):
        self.upgrade = upgrades.ProgrammeLegacyUpgradeTests()
        self.upgrade.functional_ids = self.functional_ids
        self.upgrade.functional_state = self.functional_state
        self.upgrade.setUp(); self.addCleanup(self.upgrade.doCleanups)
        self.upgrade.join(10)
        self.case, self.record = self.upgrade.case, self.upgrade.record
        self.ref = self.upgrade.slots[0]["issue_id"]

    def newer_hold(self, *, complete=False):
        old = deepcopy(self.record)
        if complete:
            self.upgrade.run_reports()
            self.assertTrue(self.record["drafts"][self.ref]["complete"])
        else:
            with patch("product_radar.report_workflow.prepare_review_package",
                       side_effect=SafetyError("SYNTHETIC_FAILURE_AFTER_CHECKED_HOLD")):
                self.upgrade.run_reports()
        self.assertEqual(self.case.memory.snapshot()["issue_reservations"][self.ref]["revision"], 2)
        self.record.clear(); self.record.update(old)
        programme._save(self.case.memory, self.record)
        self.assertTrue(self.record["drafts"][self.ref]["complete"])
        self.assertEqual(self.record["drafts"][self.ref]["revision"], 1)

    def close_latest(self, *, spent=False):
        policy = deepcopy(DEFAULT_POLICY); policy.update(model_executions=1, report_slots=0)
        turn = open_work_turn(self.case.memory, self.record["programme_id"], at=self.case.as_of, policy=policy)
        if spent:
            with bind_work_turn(self.case.memory, turn["id"]):
                Jobs(self.case.memory, turns.Runner()).run("A", turns.payload("spent-newer-hold-check"))
        close_work_turn(self.case.memory, turn["id"], at=self.case.as_of, reason="Disposable readiness proof")
        return turn

    def writing(self):
        return programme._frozen_writing_ready(self.case.memory, self.case.memory.snapshot(), self.record,
            root=self.case.root, at=self.case.as_of, detailed=True)

    def ready(self, *, runner=True):
        return programme.programme_turn_readiness(self.case.memory, self.record, root=self.case.root,
            runner=self.case.writer if runner else None, handlers={}, as_of=self.case.as_of,
            state=self.case.memory.snapshot())

    def closed_unchanged(self, turn, *, runner=True):
        before = self.ready(runner=runner)
        self.record["cooperative_checkpoint"] = {"version": 1, "continue_ready": False,
            "wake_hash": before["wake_hash"], "work_signature": before["work_signature"],
            "turn_sequence": turn["turn_sequence"]}
        return self.ready(runner=runner)

    def test_newer_incomplete_checked_hold_wakes_closed_unused_checkpoint_with_exact_g_input(self):
        self.newer_hold(); turn = self.close_latest()
        before = self.case.memory.snapshot_with_provenance()
        writing = self.writing()
        self.assertTrue(writing["ready"], writing)
        self.assertTrue(writing["local_ready"], writing)
        self.assertEqual(len(writing["model_inputs"]), 1)
        self.assertEqual(writing["model_inputs"][0]["dataset"]["revision"], 2)
        ready = self.closed_unchanged(turn)
        self.assertTrue(ready["ready"], ready)
        self.assertTrue(ready["report_local_recovery_ready"])
        self.assertIsNone(ready["model_allowance_continuation"])
        self.assertEqual(self.case.memory.snapshot_with_provenance(), before)

    def test_newer_incomplete_hold_keeps_exact_g_continuation_for_spent_turn(self):
        self.newer_hold(); turn = self.close_latest(spent=True)
        ready = self.closed_unchanged(turn)
        self.assertTrue(ready["ready"], ready)
        hint = programme._closed_model_allowance_continuation(self.case.memory, self.case.memory.snapshot(),
            self.record, runner=self.case.writer, at=self.case.as_of, retry_seconds=300, model_wait=False,
            semantic_hints=[], report_inputs=self.writing()["model_inputs"], source_questions={}, update_questions=[])
        self.assertEqual(hint["purpose"], "G")
        self.assertEqual(hint["turn_id"], turn["id"])

    def test_same_frozen_binding_wait_blocks_but_older_revision_wait_does_not(self):
        self.newer_hold(); self.close_latest()
        state = self.case.memory.snapshot()
        new = programme._frozen_report_input(self.case.root, self.upgrade.slots[0], None,
            state["issue_reservations"][self.ref], "captured")
        old = programme._frozen_report_input(self.case.root, self.upgrade.slots[0], self.record["drafts"][self.ref],
            self.upgrade.original_holds[self.ref], "captured")
        language = load_policy()["ongoing_pdf_language"]
        wait = {"stage": "reports", "target": self.ref, "reason": "SYNTHETIC_REPORT_RETRY_WAIT",
            "at": self.case.as_of, "next_retry_at": (timestamp(self.case.as_of) + timedelta(hours=1)).isoformat()}
        self.record["deferrals"]["reports:" + self.ref] = dict(wait,
            binding=digest([new["issue"], new["product_facts"], language]))
        blocked = self.writing()
        self.assertFalse(blocked["ready"])
        self.assertFalse(blocked["local_ready"])
        self.assertEqual(blocked["model_inputs"], [])
        self.record["deferrals"]["reports:" + self.ref]["binding"] = digest([old["issue"], old["product_facts"], language])
        changed = self.writing()
        self.assertTrue(changed["ready"])
        self.assertTrue(changed["local_ready"])
        self.assertEqual(changed["model_inputs"][0]["dataset"]["revision"], 2)

    def test_complete_newer_packet_wakes_local_only_without_runner_then_settles_after_normal_recovery(self):
        self.newer_hold(complete=True); turn = self.close_latest()
        writing = self.writing()
        self.assertFalse(writing["ready"])
        self.assertTrue(writing["local_ready"])
        self.assertEqual(writing["model_inputs"], [])
        ready = self.closed_unchanged(turn, runner=False)
        self.assertTrue(ready["ready"], ready)
        self.assertFalse(ready["report_ready"])
        self.assertTrue(ready["report_local_recovery_ready"])
        self.assertIsNone(ready["model_allowance_continuation"])
        calls, renders = self.case.writer.calls, len(self.case.renders)
        programme._save(self.case.memory, self.record)
        recovered = programme.advance_ready_programme_turn(self.case.memory, root=self.case.root,
            programme_id=self.record["programme_id"], runner=None, handlers={}, steps=self.case.steps(),
            render_pdf=self.case.render, inspect_pdf=upgrades.legacy.inspect, as_of=self.case.as_of)
        self.assertTrue(recovered["round_completed"])
        self.record.clear(); self.record.update(self.case.saved())
        self.assertTrue(self.record["drafts"][self.ref]["complete"])
        self.assertEqual(self.record["drafts"][self.ref]["revision"], 2)
        self.assertEqual((self.case.writer.calls, len(self.case.renders)), (calls, renders))
        self.assertFalse(self.writing()["local_ready"])
        turn = self.close_latest()
        settled = self.closed_unchanged(turn, runner=False)
        self.assertFalse(settled["ready"], settled)
        self.upgrade.assert_originals_unchanged()

    def test_current_stage_failure_wait_prevents_repeated_local_only_recovery_rounds(self):
        self.newer_hold(complete=True); turn = self.close_latest()
        binding = digest({"stage": "reports", "inputs": programme._signature(self.case.memory.snapshot(), self.record)})
        self.record["deferrals"]["lane:reports"] = {"stage": "lane", "target": "reports", "binding": binding,
            "reason": "SYNTHETIC_LOCAL_REPORT_FILE_WAIT", "at": self.case.as_of,
            "next_retry_at": (timestamp(self.case.as_of) + timedelta(seconds=300)).isoformat()}
        blocked = self.closed_unchanged(turn, runner=False)
        self.assertFalse(blocked["report_local_recovery_ready"])
        self.assertFalse(blocked["ready"], blocked)
        self.case.as_of = (timestamp(self.case.as_of) + timedelta(seconds=301)).isoformat()
        self.assertTrue(self.ready(runner=False)["ready"])


class LegacyHoldReadinessTests(unittest.TestCase):
    def newer_legacy(self):
        case = legacy.ProgrammeReportsTests(); case.setUp()
        self.addCleanup(case.doCleanups)
        case.seed(20, ready=10); case.advance(max_cycles=1)
        old = case.saved(); ref = case.slots[0]["issue_id"]
        case.refresh_selected(); case.accept_pending(); case.advance(max_cycles=1)
        state = case.memory.snapshot()
        hold = state["issue_reservations"][ref]
        self.assertEqual(hold["revision"], 2)
        self.assertIsNone(hold.get("recommendation_contract"))
        self.assertTrue((case.folder(0, 2) / "package-input.json").exists())
        self.assertTrue(old["drafts"][ref]["complete"])
        return case, old, state, hold

    def test_genuine_newer_legacy_hold_recovers_without_new_writing_then_settles(self):
        case, old, state, hold = self.newer_legacy()
        ref = case.slots[0]["issue_id"]
        before = case.memory.snapshot_with_provenance()
        calls, renders = case.writer.calls, len(case.renders)
        writing = programme._frozen_writing_ready(case.memory, state, old,
            root=case.root, at=case.as_of, detailed=True)
        self.assertTrue(writing["local_ready"], writing)
        self.assertFalse(writing["ready"], writing)
        self.assertEqual(writing["model_inputs"], [])
        self.assertTrue(programme._newer_report_hold(old["drafts"][ref], hold))
        self.assertEqual(case.memory.snapshot_with_provenance(), before)
        self.assertEqual((case.writer.calls, len(case.renders)), (calls, renders))
        original = case.packet_bytes(0, revision=2)
        stale = case.saved()
        stale["drafts"][ref] = deepcopy(old["drafts"][ref])
        programme._save(case.memory, stale)
        case.advance(max_cycles=1)
        recovered = case.saved()
        self.assertEqual(recovered["drafts"][ref]["revision"], 2)
        self.assertTrue(recovered["drafts"][ref]["complete"])
        self.assertEqual((case.writer.calls, len(case.renders)), (calls, renders))
        self.assertEqual(case.packet_bytes(0, revision=2), original)
        settled = programme._frozen_writing_ready(case.memory, case.memory.snapshot(), recovered,
            root=case.root, at=case.as_of, detailed=True)
        self.assertEqual(settled, {"ready": False, "local_ready": False, "model_inputs": []})

    def test_divergent_or_malformed_newer_hold_never_authorizes_recovery_hint(self):
        case, old, state, hold = self.newer_legacy()
        draft = old["drafts"][case.slots[0]["issue_id"]]
        self.assertTrue(programme._newer_report_hold(draft, hold))
        missing = deepcopy(hold); missing.pop("products")
        non_serializable = deepcopy(hold); non_serializable["products"] = {object()}
        for candidate in (None, [], "not a hold", missing, non_serializable,
                dict(hold, selection_hash="f" * 64), dict(hold, revision=3),
                dict(hold, recommendation_contract="unsupported"),
                dict(hold, identity_context_hash="not-a-hash"),
                dict(hold, revision=True), dict(hold, state="uploaded")):
            with self.subTest(candidate=type(candidate).__name__):
                self.assertFalse(programme._newer_report_hold(draft, candidate))
        self.assertFalse(programme._newer_report_hold([], hold))


if __name__ == "__main__":
    unittest.main()
