"""Independent restart, shared-limit and fairness checks with fictional jobs.

All state is temporary. A local fake runner supplies fixed JSON; no provider,
browser, external model, cloud system or production record is used.
"""

from copy import deepcopy
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from hashlib import sha256

from product_radar.models import SafetyError, canonical
from product_radar.research_ai import Jobs, MODEL, _item_bindings, prompt_version
from product_radar.research_memory import ResearchMemory, change, key
from product_radar.storage import Journal, atomic_json, load_json
from product_radar.work_turns import (
    DEFAULT_POLICY, active_work_turn, admit_model_job, bind_work_turn,
    close_work_turn, open_work_turn, reserve_source_callback,
)
import test_fit_ai_contracts as fit_fixtures
import test_report_narration as report_fixtures
import test_catalogue_discovery as discovery_fixtures
from product_radar.source_acquisitions import (
    claim_source_acquisition, mark_source_dispatching,
    mark_source_execution_unknown, recover_source_prelaunch, record_source_response,
    record_source_parse,
)


NOW = "2026-10-09T10:00:00+00:00"
LATER = "2026-10-09T10:15:00+00:00"


def payload(*ids):
    return {"items": [{"id": item_id, "source_text": {"title": "Fictional " + item_id}}
                      for item_id in ids]}


class Runner:
    identity = "independent-work-turn-review-v1"

    def __init__(self, failure=None):
        self.calls = []
        self.failure = failure

    def __call__(self, role, supplied, folder):
        self.calls.append((role, deepcopy(supplied)))
        if self.failure:
            raise SafetyError(self.failure)
        if role == "B":
            rows = [{"id": item["id"], "decision": "different", "match_id": None,
                     "reason": "Fictional fixed result only."} for item in supplied["items"]]
        else:
            rows = [{"id": item["id"], "kind": "uninterpretable", "products": [],
                     "reason": "Fictional fixed result only."} for item in supplied["items"]]
        return {"results": rows}, {"accounting": "synthetic"}


class WorkTurnReviewTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="work-turn-review-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.memory = ResearchMemory(self.root / "research", Journal(self.root / "finance"),
                                     evidence_type="synthetic")

    def open(self, programme="programme1", **kwargs):
        return open_work_turn(self.memory, programme, at=NOW, **kwargs)

    def configured(self, **changes):
        return {**deepcopy(DEFAULT_POLICY), **changes}

    def current(self, turn):
        return self.memory.snapshot()["batch_runs"][turn["id"]]

    def admit(self, role, supplied, runner, *, attempt="original"):
        version = prompt_version(role, supplied)
        request = {"role": role, "payload": supplied, "model": MODEL,
                   "prompt_version": version, "runner": runner.identity}
        bindings = _item_bindings(role, supplied, MODEL, version, runner.identity)
        return admit_model_job(self.memory, key("job", request), request, bindings,
                               attempt=attempt, at=NOW)

    def test_two_callback_turn_serves_both_ready_lanes_without_dead_slot(self):
        policy = self.configured(source_callbacks=2, exploration_slots=2)
        turn = self.open(policy=policy, ready_lanes={"exploration": True, "backlog": True})
        with bind_work_turn(self.memory, turn["id"]):
            reserve_source_callback(self.memory, "backlog1", at=NOW, lane="backlog")
            reserve_source_callback(self.memory, "exploration1", at=NOW, lane="exploration")
            with self.assertRaisesRegex(SafetyError, "WORK_TURN_SOURCE_LIMIT"):
                reserve_source_callback(self.memory, "extra", at=NOW)
        self.assertEqual(self.current(turn)["counters"]["source_callbacks"], 2)

    def test_one_callback_turn_alternates_per_programme_despite_interleaved_peer(self):
        policy = self.configured(source_callbacks=1, exploration_slots=1)
        served = []
        for number in range(4):
            turn = self.open(policy=policy, ready_lanes={"exploration": True, "backlog": True})
            preferred = "exploration" if number % 2 == 0 else "backlog"
            other = "backlog" if preferred == "exploration" else "exploration"
            with bind_work_turn(self.memory, turn["id"]):
                with self.assertRaisesRegex(SafetyError, "WORK_TURN_SOURCE_LIMIT"):
                    reserve_source_callback(self.memory, "wrong-lane", at=NOW, lane=other)
                reserve_source_callback(self.memory, "ready-lane", at=NOW, lane=preferred)
                served.append(preferred)
            close_work_turn(self.memory, turn["id"], at=LATER, reason="Safe finite yield.")
            peer = self.open("programme2", policy=policy)
            close_work_turn(self.memory, peer["id"], at=LATER, reason="Safe peer yield.")
        self.assertEqual(served, ["exploration", "backlog", "exploration", "backlog"])

    def test_reconciliation_invocations_use_new_ordinal_not_old_reservation_voucher(self):
        turn = self.open(policy=self.configured(source_callbacks=2, exploration_slots=0))
        callbacks = []

        def saved_only_callback():
            current = active_work_turn(self.memory)
            owner = key("source-service", [current["id"], current["counters"]["source_callbacks"],
                                           "same-task", "same-acquisition", "saved_reconciliation"])
            reservation = reserve_source_callback(self.memory, owner, at=NOW)
            callbacks.append(reservation["token"])

        with bind_work_turn(self.memory, turn["id"]):
            saved_only_callback()
            saved_only_callback()
            with self.assertRaisesRegex(SafetyError, "WORK_TURN_SOURCE_LIMIT"):
                saved_only_callback()
        self.assertEqual(len(callbacks), 2)
        self.assertEqual(len(set(callbacks)), 2)
        self.assertEqual(self.current(turn)["counters"]["source_callbacks"], 2)

    def test_failed_joined_source_intent_does_not_consume_or_save_half_reservation(self):
        self.memory.put("query_tasks", "task", {"kind": "all_source_task_v1", "status": "prepared"})
        turn = self.open()
        before = self.memory.snapshot()
        with bind_work_turn(self.memory, turn["id"]):
            # Missing the actual prior value gives a genuine conditional-write
            # conflict after the turn change has entered the tentative batch.
            with self.assertRaisesRegex(SafetyError, "RESEARCH_DEPENDENCY_CHANGED"):
                reserve_source_callback(self.memory, "callback", at=NOW, changes=[
                    change("query_tasks", "task", {"kind": "all_source_task_v1", "status": "dispatching"})])
        self.assertEqual(self.memory.snapshot(), before)

    def test_restart_retains_full_bound_and_does_not_turn_resume_into_reset(self):
        policy = self.configured(source_callbacks=1, exploration_slots=0,
                                 model_executions=1, report_slots=0)
        turn = self.open(policy=policy)
        runner = Runner()
        with bind_work_turn(self.memory, turn["id"]):
            reserve_source_callback(self.memory, "first", at=NOW)
            Jobs(self.memory, runner).run("A", payload("first"))
        restarted = ResearchMemory(self.memory.root, Journal(self.root / "finance"), evidence_type="synthetic")
        resumed = open_work_turn(restarted, "programme1", at=LATER, policy=policy)
        self.assertEqual(resumed["id"], turn["id"])
        with bind_work_turn(restarted, resumed["id"]):
            with self.assertRaisesRegex(SafetyError, "WORK_TURN_SOURCE_LIMIT"):
                reserve_source_callback(restarted, "second", at=LATER)
            with self.assertRaisesRegex(SafetyError, "WORK_TURN_MODEL_LIMIT"):
                Jobs(restarted, runner).run("A", payload("second"))
            _, reused = Jobs(restarted, runner).run("A", payload("first"))
        self.assertIn("first", reused["accepted"])
        self.assertEqual(len(runner.calls), 1)

    def test_actual_a_and_b_jobs_share_one_model_ceiling(self):
        policy = self.configured(model_executions=2, report_slots=0)
        turn = self.open(policy=policy)
        runner = Runner()
        with bind_work_turn(self.memory, turn["id"]):
            _, first = Jobs(self.memory, runner).run("A", payload("a1"))
            _, second = Jobs(self.memory, runner).run("B", {
                "items": [{"id": "b1", "description": {"label": "Fictional holder"}, "candidates": []}]})
            with self.assertRaisesRegex(SafetyError, "WORK_TURN_MODEL_LIMIT"):
                Jobs(self.memory, runner).run("A", payload("a2"))
        self.assertIn("a1", first["accepted"])
        self.assertIn("b1", second["accepted"])
        self.assertEqual([role for role, _ in runner.calls], ["A", "B"])
        self.assertEqual(self.current(turn)["counters"]["model_executions"], 2)

    def test_ready_report_gets_shared_slot_not_extra_allowance(self):
        turn = self.open(policy=self.configured(model_executions=2), report_ready=True)
        runner = Runner()
        _, dataset = report_fixtures.fixture()
        supplied = report_fixtures.make_narrative_input(dataset, "en")
        supplied["items"] = supplied["items"][:1]
        narration = report_fixtures.NarrationRunner(memory=self.memory)
        with bind_work_turn(self.memory, turn["id"]):
            Jobs(self.memory, runner).run("A", payload("a1"))
            with self.assertRaisesRegex(SafetyError, "WORK_TURN_MODEL_LIMIT"):
                Jobs(self.memory, runner).run("A", payload("a2"))
            _, result = Jobs(self.memory, narration).run("G", supplied)
        self.assertEqual(len(narration.calls), 1)
        self.assertEqual(len(result["accepted"]), 1)
        self.assertEqual(self.current(turn)["counters"]["model_executions"], 2)

    def test_absent_report_donates_reserved_capacity_to_real_research(self):
        turn = self.open(policy=self.configured(model_executions=2), report_ready=False)
        runner = Runner()
        with bind_work_turn(self.memory, turn["id"]):
            Jobs(self.memory, runner).run("A", payload("first"))
            Jobs(self.memory, runner).run("A", payload("second"))
        self.assertEqual(len(runner.calls), 2)
        self.assertEqual(self.current(turn)["counters"]["model_executions"], 2)

    def test_many_carried_jobs_cannot_bypass_current_turn_ceiling(self):
        origin = self.open()
        runner = Runner()
        with bind_work_turn(self.memory, origin["id"]):
            admissions = [self.admit("A", payload("carry" + str(number)), runner) for number in range(3)]
        close_work_turn(self.memory, origin["id"], at=LATER, reason="Safe yield before any intent.")
        current = self.open(policy=self.configured(model_executions=1, report_slots=0))
        with bind_work_turn(self.memory, current["id"]):
            Jobs(self.memory, runner).run("A", payload("carry0"))
            with self.assertRaisesRegex(SafetyError, "WORK_TURN_MODEL_LIMIT"):
                Jobs(self.memory, runner).run("A", payload("carry1"))
        row = self.current(current)
        self.assertEqual((row["counters"]["model_executions"], row["counters"]["origin_admissions"]), (1, 0))
        service = next(iter(row["reservations"]["model"].values()))
        self.assertEqual(service["origin_token"], admissions[0]["origin_token"])
        self.assertEqual(len(runner.calls), 1)

    def test_carried_fit_batch_reserves_original_item_count_not_requested_subset(self):
        origin = self.open()
        runner = fit_fixtures.FitRunner()
        original = fit_fixtures.fit_payload(item_ids=tuple("fit" + str(number) for number in range(10)))
        with bind_work_turn(self.memory, origin["id"]):
            self.admit("E", original, runner)
        close_work_turn(self.memory, origin["id"], at=LATER, reason="Safe yield before intent.")
        policy = self.configured()
        policy["purpose_items"]["E:fit"] = 5
        current = self.open(policy=policy)
        with bind_work_turn(self.memory, current["id"]):
            _, result = Jobs(self.memory, runner).run("E", fit_fixtures.fit_payload(item_ids=("fit0",)))
            with self.assertRaisesRegex(SafetyError, "WORK_TURN_PURPOSE_LIMIT"):
                Jobs(self.memory, runner).run("E", fit_fixtures.fit_payload(item_ids=("new-fit",)))
        self.assertIn("fit0", result["accepted"])
        self.assertEqual(result["failures"], {})
        self.assertEqual(len(runner.calls), 1)
        self.assertEqual(len(runner.calls[0]["items"]), 10)
        row = self.current(current)
        self.assertEqual(row["counters"]["purpose_items"]["E:fit"], 10)
        self.assertEqual((row["counters"]["model_executions"], row["counters"]["origin_admissions"]), (1, 0))
        service = next(iter(row["reservations"]["model"].values()))
        self.assertTrue(service["carried_purpose_overage"])
        self.assertEqual(service["origin_turn_id"], origin["id"])

    def test_new_fit_batch_cannot_use_original_owner_exception(self):
        policy = self.configured()
        policy["purpose_items"]["E:fit"] = 5
        current = self.open(policy=policy)
        runner = fit_fixtures.FitRunner()
        supplied = fit_fixtures.fit_payload(item_ids=tuple("fit" + str(number) for number in range(10)))
        with bind_work_turn(self.memory, current["id"]), self.assertRaisesRegex(SafetyError, "WORK_TURN_PURPOSE_LIMIT"):
            Jobs(self.memory, runner).run("E", supplied)
        self.assertEqual(runner.calls, [])
        self.assertEqual(self.memory.snapshot()["jobs"], {})
        self.assertEqual(self.current(current)["counters"]["model_executions"], 0)

    def test_execution_artifact_without_intent_is_not_proof_of_never_started(self):
        origin = self.open()
        runner = Runner()
        with bind_work_turn(self.memory, origin["id"]):
            admission = self.admit("A", payload("uncertain"), runner)
        # Losing an intent while an original execution receipt survives must
        # never donate a new-run permission. Its incomplete provenance remains
        # for reconciliation; this fake document does not claim a real call.
        atomic_json(self.memory.root / "ai_jobs" / admission["job_id"] / "runner_execution.json",
                    {"fixture": "Original execution needs reconciliation."})
        close_work_turn(self.memory, origin["id"], at=LATER, reason="Original receipt remains unresolved.")
        current = self.open()
        with bind_work_turn(self.memory, current["id"]), self.assertRaisesRegex(SafetyError, "AI_JOB_PENDING_RECONCILIATION"):
            Jobs(self.memory, runner).run("A", payload("uncertain"))
        self.assertEqual(runner.calls, [])
        self.assertEqual(self.current(current)["counters"]["model_executions"], 0)

    def test_missing_discovery_reconciler_does_not_make_uncertain_dispatch_unstarted(self):
        fixture = discovery_fixtures.DiscoveryTests()
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        fixture.memory = self.memory
        calls = []

        def lost_return(context):
            calls.append(context["acquisition_id"])
            context["record_dispatch"]()
            raise RuntimeError("Fictional lost collector return after dispatch.")

        # A real connected collector can temporarily have no saved-reply
        # reconciliation function. Lack of that function is not proof that
        # its already recorded original dispatch never happened.
        handlers = {"amazon-search": {"handler": lost_return},
                    "flipkart-search": fixture.handler(state="empty")}
        cycle = fixture.prepare(handlers=handlers)
        turn = self.open()
        with bind_work_turn(self.memory, turn["id"]):
            fixture.advance(cycle, handlers)
            fixture.advance(cycle, handlers)
            fixture.advance(cycle, handlers)
        self.assertEqual(len(calls), 1)
        saved = self.memory.snapshot()["batch_runs"][cycle["id"]]
        original = next(row for row in saved["actions"].values() if row.get("acquisition_id") == calls[0])
        self.assertEqual(original["state"], "execution_uncertain")
        self.assertIs(original["dispatch_started"], True)
        self.assertEqual(fixture.calls, [], "Uncertain is not a confirmed failure that permits the old fallback.")
        self.assertEqual(self.current(turn)["counters"]["source_callbacks"], 1)
        with bind_work_turn(self.memory, turn["id"]):
            fixture.advance(cycle, {"amazon-search": fixture.handler(recovered=True)})
        self.assertEqual(len(calls), 1)
        self.assertEqual(len(fixture.replays), 1)
        self.assertEqual(fixture.replays[0]["acquisition_id"], calls[0])
        self.assertTrue(fixture.replays[0]["reconciling"])
        self.assertEqual(self.current(turn)["counters"]["source_callbacks"], 2)

    def test_uncertain_original_does_not_relaunch_or_consume_later_service(self):
        origin = self.open()
        runner = Runner("AI_PROVIDER_REQUEST_FAILED")
        with bind_work_turn(self.memory, origin["id"]), self.assertRaisesRegex(SafetyError, "AI_PROVIDER_REQUEST_FAILED"):
            Jobs(self.memory, runner).run("A", payload("uncertain"))
        close_work_turn(self.memory, origin["id"], at=LATER, reason="Wait for original receipt.")
        current = self.open(policy=self.configured(model_executions=1, report_slots=0))
        runner.failure = None
        with bind_work_turn(self.memory, current["id"]):
            with self.assertRaisesRegex(SafetyError, "AI_JOB_PENDING_RECONCILIATION"):
                Jobs(self.memory, runner).run("A", payload("uncertain"))
            Jobs(self.memory, runner).run("A", payload("healthy"))
        self.assertEqual([row[1]["items"][0]["id"] for row in runner.calls], ["uncertain", "healthy"])
        self.assertEqual(self.current(current)["counters"]["model_executions"], 1)

    def test_saved_mixed_sibling_still_applies_when_new_sibling_hits_limit(self):
        runner = Runner()
        Jobs(self.memory, runner).run("A", payload("saved"))
        turn = self.open(policy=self.configured(model_executions=1, report_slots=0))
        with bind_work_turn(self.memory, turn["id"]):
            Jobs(self.memory, runner).run("A", payload("used"))
            _, result = Jobs(self.memory, runner).run("A", payload("saved", "new"))
        self.assertIn("saved", result["accepted"])
        self.assertEqual(result["failures"], {"new": "WORK_TURN_MODEL_LIMIT"})
        self.assertEqual(len(runner.calls), 2)

    def test_bad_saved_result_is_not_downgraded_to_an_ordinary_turn_wait(self):
        runner = Runner()
        job_id, _ = Jobs(self.memory, runner).run("A", payload("saved"))
        result_path = self.memory.root / "ai_jobs" / job_id / "result.json"
        damaged = load_json(result_path)
        damaged["request_hash"] = "0" * 64
        atomic_json(result_path, damaged)
        current = self.open()
        with bind_work_turn(self.memory, current["id"]), self.assertRaisesRegex(SafetyError, "AI_JOB_RESULT_CONFLICT"):
            Jobs(self.memory, runner).run("A", payload("saved", "new"))
        self.assertEqual(len(runner.calls), 1)
        self.assertEqual(self.current(current)["counters"]["model_executions"], 0)

    def test_same_reservation_recovers_after_proved_pre_intent_save_failure(self):
        turn = self.open(policy=self.configured(model_executions=1, report_slots=0))
        runner = Runner()
        original_atomic = atomic_json
        injected = False

        def fail_before_intent(path, value):
            nonlocal injected
            if Path(path).name == "intent.json" and not injected:
                injected = True
                raise OSError("Fictional failed local save before model entry")
            return original_atomic(path, value)

        with bind_work_turn(self.memory, turn["id"]):
            with patch("product_radar.research_ai.atomic_json", side_effect=fail_before_intent), self.assertRaises(OSError):
                Jobs(self.memory, runner).run("A", payload("original"))
            reservation = deepcopy(self.current(turn)["reservations"]["model"])
            self.assertEqual(len(runner.calls), 0)
            job_id, _ = Jobs(self.memory, runner).run("A", payload("original"))
        self.assertEqual(self.current(turn)["reservations"]["model"], reservation)
        intent = load_json(self.memory.root / "ai_jobs" / job_id / "intent.json")
        self.assertEqual(intent["turn_admission"], next(iter(reservation.values())))
        self.assertEqual(len(runner.calls), 1)

    def test_unbound_legacy_call_does_not_create_any_turn_or_admission(self):
        runner = Runner()
        job_id, result = Jobs(self.memory, runner).run("A", payload("legacy"))
        state = self.memory.snapshot()
        self.assertEqual(len(runner.calls), 1)
        self.assertIn("legacy", result["accepted"])
        self.assertEqual(state["batch_runs"], {})
        self.assertNotIn("turn_admissions", state["jobs"][job_id])


class SourceAcquisitionReviewTests(unittest.TestCase):
    """Original-process proofs must not authorise a later dispatch intent."""

    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="acquisition-review-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.memory = ResearchMemory(self.root / "research", Journal(self.root / "finance"),
                                     evidence_type="synthetic")
        self.memory.root.mkdir()

    def claim(self, *, owner="question1", at=NOW, **options):
        return claim_source_acquisition(self.memory, "google-trends",
            {"query": "fictional sink caddy", "country": "IN"}, owner_id=owner, as_of=at,
            collection_channel="direct_local", incremental_collection_fee="known_zero", **options)

    def response_receipt(self, claimed, *, truncated=False, empty=False):
        binding = self.memory.snapshot()["batch_runs"][claimed["acquisition_id"]]["binding"]
        folder = self.memory.root / "original-response"
        folder.mkdir(parents=True, exist_ok=True)
        body = {"records": [] if empty else [{"label": "fictional sink caddy"}]}
        atomic_json(folder / "snapshot.json", body)
        receipt = {"route": binding["route"], "request": binding["request"],
            "acquisition_id": claimed["acquisition_id"], "attempt_id": claimed["original_attempt_id"],
            "dispatch_started": True, "observed_at": NOW,
            "state": "empty" if empty else "usable",
            "coverage": {"body_complete": not truncated, "byte_truncated": truncated,
                "scope_complete": True, "recognized": True, "delivered_count": 0 if empty else 1,
                "intentional_limit": True},
            "artifacts": {"snapshot.json": sha256(canonical(body) + b"\n").hexdigest()}}
        atomic_json(folder / "receipt.json", receipt)
        return folder / "receipt.json"

    def parse_receipt(self, claimed, response_id, *, parser="fixture-parser-v1", state="usable"):
        source = self.memory.snapshot()["source_checks"][response_id]
        parsed = {"contract": "saved-source-parse-v1", "source_response_hash": source["response_hash"],
            "parser_contract": {"contract": parser}, "state": state,
            "records": [] if state in {"empty", "failed"} else [{"label": "fictional sink caddy"}]}
        path = self.memory.root / (parser + ".json")
        atomic_json(path, parsed)
        return path, parsed["parser_contract"]

    def zero_receipt(self, claimed, *, name="original", intent_id=None):
        """Produce fixed local proof files, not an actual process or source call."""
        row = self.memory.snapshot()["batch_runs"][claimed["acquisition_id"]]
        binding = row["binding"]
        intent_id = intent_id or row["active_dispatch_intent_id"]
        intent = self.memory.snapshot()["source_checks"][intent_id]
        folder = self.memory.root / name
        folder.mkdir(parents=True, exist_ok=True)
        proof = {"contract": "owned-source-process-exit-v1",
            "acquisition_id": claimed["acquisition_id"], "attempt_id": claimed["original_attempt_id"],
            "physical_request_fingerprint": binding["physical_request_fingerprint"],
            "owned_process_exited": True, "request_sent": False, "process_id": 123,
            "dispatch_intent_id": intent_id, "intent_sequence": intent["intent_sequence"],
            "ownership_token_hash": intent["process_ownership_token_hash"],
            "started_at": NOW, "exited_at": NOW}
        atomic_json(folder / "process-exit.json", proof)
        receipt = {"route": binding["route"], "request": binding["request"],
            "acquisition_id": claimed["acquisition_id"], "attempt_id": claimed["original_attempt_id"],
            "dispatch_started": False, "observed_at": NOW, "state": "failed",
            "artifacts": {"process-exit.json": sha256(canonical(proof) + b"\n").hexdigest()}}
        atomic_json(folder / "receipt.json", receipt)
        return folder / "receipt.json"

    def test_zero_proof_for_earlier_intent_does_not_reopen_a_later_uncertain_intent(self):
        claimed = self.claim()
        acquisition_id = claimed["acquisition_id"]
        first = mark_source_dispatching(self.memory, acquisition_id, as_of=NOW)
        receipt = self.zero_receipt(claimed)
        recovered = recover_source_prelaunch(self.memory, acquisition_id, receipt_path=receipt, as_of=NOW)
        self.assertEqual(recovered["state"], "reserved")
        second = mark_source_dispatching(self.memory, acquisition_id, as_of=LATER)
        self.assertNotEqual(first["stage_id"], second["stage_id"])
        mark_source_execution_unknown(self.memory, acquisition_id,
                                      reason="Fictional second request has no reply.", as_of=LATER)
        # The bytes describe only the first, exited process. Copying those bytes
        # to a second saved path must not make them proof about the second one.
        copied = self.zero_receipt(claimed, name="copied-old-proof", intent_id=first["stage_id"])
        before = self.memory.snapshot()
        with self.assertRaisesRegex(SafetyError, "SOURCE_ACQUISITION_.*"):
            recover_source_prelaunch(self.memory, acquisition_id, receipt_path=copied, as_of=LATER)
        self.assertEqual(self.memory.snapshot(), before)

    def test_questions_join_one_origin_and_fresh_saved_bytes_reuse_without_launch(self):
        first = self.claim()
        joined = self.claim(owner="question2")
        self.assertEqual(joined["action"], "join")
        self.assertEqual(joined["acquisition_id"], first["acquisition_id"])
        self.assertEqual(joined["owner_id"], "question1")
        mark_source_dispatching(self.memory, first["acquisition_id"], as_of=NOW)
        record_source_response(self.memory, first["acquisition_id"],
                               receipt_path=self.response_receipt(first), as_of=NOW)
        before = self.memory.snapshot()
        reused = self.claim(owner="question3", at=LATER)
        self.assertEqual(reused["action"], "reuse")
        self.assertEqual(reused["acquisition_id"], first["acquisition_id"])
        self.assertEqual(self.memory.snapshot(), before)
        self.assertTrue(all(item["launch_authority"] is False for item in (first, joined, reused)))

    def test_due_refresh_makes_one_new_generation_and_second_consumer_joins_it(self):
        original = self.claim()
        record_source_response(self.memory, original["acquisition_id"],
                               receipt_path=self.response_receipt(original), as_of=NOW)
        at = "2026-10-17T10:00:00+00:00"
        before = self.memory.snapshot()
        due = self.claim(owner="question2", at=at)
        self.assertEqual(due["action"], "refresh_due")
        self.assertEqual(self.memory.snapshot(), before)
        refresh = self.claim(owner="question2", at=at, refresh_due_at=at,
                             refresh_reason="The original scoped read is now old.")
        joined = self.claim(owner="question3", at=at, refresh_due_at=at,
                            refresh_reason="Another consumer needs the same due read.")
        self.assertEqual(refresh["generation"], 2)
        self.assertEqual(joined["action"], "join")
        self.assertEqual(joined["acquisition_id"], refresh["acquisition_id"])
        self.assertNotEqual(refresh["acquisition_id"], original["acquisition_id"])
        self.assertEqual(self.memory.snapshot()["batch_runs"][original["acquisition_id"]],
                         before["batch_runs"][original["acquisition_id"]])

    def test_truncated_bytes_do_not_become_complete_empty_or_fresh_reuse(self):
        claimed = self.claim()
        response = record_source_response(self.memory, claimed["acquisition_id"],
            receipt_path=self.response_receipt(claimed, truncated=True, empty=True), as_of=NOW)
        path, parser = self.parse_receipt(claimed, response["stage_id"], state="empty")
        parsed = record_source_parse(self.memory, claimed["acquisition_id"],
                                     parsed_path=path, parser_contract=parser, as_of=NOW)
        self.assertEqual(parsed["state"], "parsed")
        self.assertFalse(self.memory.snapshot()["source_checks"][response["stage_id"]]["complete"])
        self.assertEqual(self.claim(owner="question2", at=LATER)["action"], "join")

    def test_parser_repair_is_additive_and_keeps_original_capture_date(self):
        claimed = self.claim()
        response = record_source_response(self.memory, claimed["acquisition_id"],
                               receipt_path=self.response_receipt(claimed), as_of=NOW)
        source_before = deepcopy(self.memory.snapshot()["source_checks"][response["stage_id"]])
        path, parser = self.parse_receipt(claimed, response["stage_id"], state="failed")
        record_source_parse(self.memory, claimed["acquisition_id"],
                            parsed_path=path, parser_contract=parser, as_of=NOW)
        path, parser = self.parse_receipt(claimed, response["stage_id"], parser="fixed-parser-v2")
        accepted = record_source_parse(self.memory, claimed["acquisition_id"],
                                      parsed_path=path, parser_contract=parser, as_of=LATER)
        state = self.memory.snapshot()
        self.assertEqual(state["source_checks"][response["stage_id"]], source_before)
        self.assertEqual(source_before["observed_at"], NOW)
        self.assertEqual(len(state["batch_runs"][claimed["acquisition_id"]]["parse_ids"]), 2)
        self.assertEqual(accepted["state"], "parsed")
        before = self.memory.snapshot()
        replay = record_source_parse(self.memory, claimed["acquisition_id"],
                                     parsed_path=path, parser_contract=parser, as_of=LATER)
        self.assertTrue(replay["reused"])
        self.assertEqual(self.memory.snapshot(), before)

    def test_changed_original_bytes_refuse_reuse_without_creating_replacement(self):
        claimed = self.claim()
        receipt = self.response_receipt(claimed)
        record_source_response(self.memory, claimed["acquisition_id"], receipt_path=receipt, as_of=NOW)
        before = self.memory.snapshot()
        atomic_json(receipt.parent / "snapshot.json", {"records": [{"label": "different bytes"}]})
        with self.assertRaisesRegex(SafetyError, "SOURCE_ACQUISITION_ARTIFACT_CHANGED"):
            self.claim(owner="question2", at=LATER)
        self.assertEqual(self.memory.snapshot(), before)

    def test_parse_commit_rechecks_original_response_dependencies(self):
        claimed = self.claim()
        receipt = self.response_receipt(claimed)
        response = record_source_response(self.memory, claimed["acquisition_id"],
                                          receipt_path=receipt, as_of=NOW)
        parsed, contract = self.parse_receipt(claimed, response["stage_id"])
        before = self.memory.snapshot()
        original_commit = self.memory.commit

        def change_original_before_commit(*args, **kwargs):
            atomic_json(receipt.parent / "snapshot.json", {"records": [{"label": "changed source"}]})
            return original_commit(*args, **kwargs)

        with patch.object(self.memory, "commit", side_effect=change_original_before_commit):
            with self.assertRaisesRegex(SafetyError, "SOURCE_ACQUISITION_.*"):
                record_source_parse(self.memory, claimed["acquisition_id"],
                                    parsed_path=parsed, parser_contract=contract, as_of=LATER)
        self.assertEqual(self.memory.snapshot(), before)

    def test_refresh_commit_rechecks_original_response_dependencies(self):
        claimed = self.claim()
        receipt = self.response_receipt(claimed)
        record_source_response(self.memory, claimed["acquisition_id"], receipt_path=receipt, as_of=NOW)
        before = self.memory.snapshot()
        original_commit = self.memory.commit

        def change_original_before_commit(*args, **kwargs):
            atomic_json(receipt.parent / "snapshot.json", {"records": [{"label": "changed source"}]})
            return original_commit(*args, **kwargs)

        at = "2026-10-17T10:00:00+00:00"
        with patch.object(self.memory, "commit", side_effect=change_original_before_commit):
            with self.assertRaisesRegex(SafetyError, "SOURCE_ACQUISITION_.*"):
                self.claim(owner="question2", at=at, refresh_due_at=at,
                           refresh_reason="A due read cannot ignore changed prior proof.")
        self.assertEqual(self.memory.snapshot(), before)


if __name__ == "__main__":
    unittest.main()
