"""Closed allowance continuation with disposable local research only."""

from copy import deepcopy
from datetime import timedelta
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import product_radar.programme as programme
from product_radar.models import SafetyError, digest, timestamp
from product_radar.product_queries import query_profile_input, query_profile_scope_hash, _payload
from product_radar.research_ai import Jobs
from product_radar.research_memory import ResearchMemory
from product_radar.storage import Journal, atomic_json
from product_radar.work_turns import DEFAULT_POLICY, bind_work_turn, close_work_turn, open_work_turn
import test_programme as programme_fixtures
import test_product_queries as query_fixtures
import test_work_turns as turn_fixtures


class ModelAllowanceContinuationTests(unittest.TestCase):
    def setUp(self):
        fixture = programme_fixtures.ProgrammeTests(); fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        self.fixture, self.memory, self.root, self.now = fixture, fixture.memory, fixture.root, fixture.now
        original = self.memory.snapshot()["concepts"]["first"]
        self.memory.put("concepts", "first", dict(original, description=deepcopy(query_fixtures.DESCRIPTION)), previous=original)
        self.pid = "model-continuation"
        self.record = programme.prepare_programme(self.memory, self.pid, concept_ids=["first"],
            source_run_id="model-only-sources", as_of=self.now)
        self.policy = deepcopy(DEFAULT_POLICY); self.policy.update(model_executions=1, report_slots=0)

    def spent(self, *, close=False):
        turn = open_work_turn(self.memory, self.pid, at=self.now, policy=self.policy)
        original_runner = turn_fixtures.Runner()
        with bind_work_turn(self.memory, turn["id"]):
            Jobs(self.memory, original_runner).run("A", turn_fixtures.payload("already-spent-original"))
        if close:
            close_work_turn(self.memory, turn["id"], at=self.now, reason="Synthetic original round finished.")
        return turn, original_runner

    def advance(self, runner, *, memory=None, profiles=True, policy=None):
        steps = self.fixture.noops()
        if profiles:
            steps["profiles"] = programme._profiles
        return programme.advance_ready_programme_turn(memory or self.memory, root=self.root, programme_id=self.pid,
            handlers={}, steps=steps, runner=runner, turn_policy=policy or self.policy, as_of=self.now)

    def readiness(self, runner, *, record=None, state=None):
        return programme.programme_turn_readiness(self.memory,
            record or programme.load_programme(self.memory, self.pid), root=self.root,
            runner=runner, handlers={}, steps=self.fixture.noops(), as_of=self.now, state=state)

    def test_spent_original_closes_then_fresh_model_only_work_runs_then_empty_round_settles(self):
        original, original_runner = self.spent()
        runner = query_fixtures.Runner()
        first = self.advance(runner)
        self.assertEqual(first["work_turn"]["id"], original["id"])
        self.assertEqual(first["work_turn"]["state"], "closed")
        self.assertEqual(first["work_turn"]["counters"]["model_executions"], 1)
        self.assertFalse(first["meaningful_progress"])
        self.assertEqual(first["next_action"], "continue_ready_work")
        self.assertEqual(runner.calls, [])
        second = self.advance(runner)
        self.assertNotEqual(second["work_turn"]["id"], original["id"])
        self.assertEqual(second["work_turn"]["counters"]["model_executions"], 1)
        self.assertEqual(second["work_turn"]["counters"]["source_callbacks"], 0)
        self.assertTrue(second["meaningful_progress"])
        self.assertEqual(len(runner.calls), 1)
        third = self.advance(runner)
        self.assertFalse(third["meaningful_progress"])
        self.assertEqual(third["work_turn"]["counters"]["model_executions"], 0)
        self.assertEqual(third["next_action"], "wait_for_saved_trigger")
        fourth = self.advance(runner)
        self.assertEqual(fourth["state"], "all_waiting")
        self.assertFalse(fourth["round_completed"])
        self.assertEqual(len(original_runner.calls), 1)
        self.assertEqual(len(runner.calls), 1)
        self.assertEqual(len([r for r in self.memory.snapshot()["batch_runs"].values()
                              if r.get("kind") == "work-turn-v1"]), 3)

    def test_old_false_checkpoint_recovers_in_a_fresh_process_without_migration_write(self):
        original, _ = self.spent()
        runner = query_fixtures.Runner()
        self.advance(runner)
        record = programme.load_programme(self.memory, self.pid)
        record["cooperative_checkpoint"]["continue_ready"] = False  # Exact old-version checkpoint shape.
        programme._save(self.memory, record)
        before = self.memory.snapshot_with_provenance()
        with patch.object(self.memory, "snapshot", side_effect=AssertionError("Use the supplied verified state")):
            ready = self.readiness(runner, record=record, state=before["state"])
        self.assertTrue(ready["ready"])
        self.assertEqual(ready["reason"], "closed_model_allowance_has_ready_work")
        self.assertEqual(ready["model_allowance_continuation"]["turn_id"], original["id"])
        self.assertEqual(ready["model_allowance_continuation"]["purpose"], "C:product_query_profile")
        self.assertEqual(self.memory.snapshot_with_provenance(), before)
        cold = ResearchMemory(self.memory.root, Journal(self.memory.journal.root), evidence_type="synthetic")
        resumed = self.advance(runner, memory=cold)
        self.assertTrue(resumed["round_completed"])
        self.assertNotEqual(resumed["work_turn"]["id"], original["id"])
        self.assertEqual(len(runner.calls), 1)

    def test_open_original_preserves_its_actual_policy_and_counters_before_any_new_turn(self):
        original, _ = self.spent()
        before = deepcopy(self.memory.snapshot()["batch_runs"][original["id"]])
        next_policy = deepcopy(self.policy); next_policy["model_executions"] = 2
        runner = query_fixtures.Runner()
        first = self.advance(runner, policy=next_policy)
        closed = self.memory.snapshot()["batch_runs"][original["id"]]
        self.assertEqual(first["work_turn"]["id"], original["id"])
        self.assertEqual(closed["policy"], before["policy"])
        self.assertEqual(closed["counters"], before["counters"])
        self.assertEqual(closed["reservations"], before["reservations"])
        self.assertEqual(runner.calls, [])
        second = self.advance(runner, policy=next_policy)
        self.assertEqual(self.memory.snapshot()["batch_runs"][second["work_turn"]["id"]]["policy"], next_policy)
        self.assertEqual(len(runner.calls), 1)

    def test_absent_runner_and_future_global_or_exact_profile_wait_do_not_qualify(self):
        self.spent(close=True)
        runner = query_fixtures.Runner()
        record = programme.load_programme(self.memory, self.pid)
        future = (timestamp(self.now) + timedelta(hours=1)).isoformat()
        self.assertIsNone(self.readiness(None, record=record)["model_allowance_continuation"])
        paused = deepcopy(record)
        paused["model_pause"] = {"at": self.now, "reason": "AI_RATE_LIMITED", "next_retry_at": future}
        self.assertIsNone(self.readiness(runner, record=paused)["model_allowance_continuation"])
        held = deepcopy(record)
        held["deferrals"]["profiles:first"] = {"stage": "profiles", "target": "first",
            "binding": query_profile_scope_hash(self.memory.snapshot(), "first"), "reason": "AI_MODEL_RETRY_WAIT",
            "at": self.now, "next_retry_at": future}
        self.assertIsNone(self.readiness(runner, record=held)["model_allowance_continuation"])

    def test_owner_stop_prevents_fresh_turn_even_with_a_spent_original_and_ready_model(self):
        original, _ = self.spent(close=True)
        before = self.memory.snapshot()
        programme.set_programme_service_state(self.memory, enabled=False, as_of=self.now)
        runner = query_fixtures.Runner()
        paused = self.advance(runner)
        self.assertEqual(paused["state"], "owner_paused")
        self.assertFalse(paused["round_completed"])
        self.assertEqual(self.memory.snapshot(), before)
        self.assertEqual(runner.calls, [])
        self.assertEqual(self.memory.snapshot()["batch_runs"][original["id"]]["state"], "closed")

    def test_unknown_original_model_execution_is_not_fresh_ready_work(self):
        runner = query_fixtures.Runner()
        item = query_profile_input(self.memory.snapshot(), "first")
        def unknown(*_args):
            raise SafetyError("AI_RATE_LIMITED")
        unknown.identity = runner.identity
        with self.assertRaisesRegex(SafetyError, "AI_RATE_LIMITED"):
            Jobs(self.memory, unknown).run("C", _payload([item]))
        self.spent(close=True)
        self.assertIsNone(self.readiness(runner)["model_allowance_continuation"])

    def test_execution_failure_without_intent_is_uncertain_not_a_fresh_original(self):
        runner = query_fixtures.Runner()
        payload = _payload([query_profile_input(self.memory.snapshot(), "first")])
        def failed(*_args):
            raise SafetyError("AI_RATE_LIMITED")
        failed.identity = runner.identity
        with self.assertRaisesRegex(SafetyError, "AI_RATE_LIMITED"):
            Jobs(self.memory, failed).run("C", payload)
        job_id = next(job_id for job_id, row in self.memory.snapshot()["jobs"].items()
                      if row["request"]["role"] == "C")
        folder = self.memory.root / "ai_jobs" / job_id
        for path in folder.iterdir():
            if path.is_file():
                path.unlink()  # Disposable isolated fixture: emulate a retained execution-failure-only original.
        atomic_json(folder / "execution_failure.json", {"reason": "Synthetic uncertain execution."})
        self.spent(close=True)
        before = self.memory.snapshot_with_provenance()
        self.assertFalse(programme._model_question_ready(self.memory, before["state"], runner, "C", payload,
            at=self.now, retry_seconds=300))
        self.assertIsNone(self.readiness(runner, state=before["state"])["model_allowance_continuation"])
        self.assertEqual(self.memory.snapshot_with_provenance(), before)

    def test_unused_turn_with_all_model_slots_reserved_does_not_create_another_turn(self):
        policy = deepcopy(self.policy); policy["report_slots"] = policy["model_executions"]
        original = open_work_turn(self.memory, self.pid, at=self.now, policy=policy, report_ready=True)
        close_work_turn(self.memory, original["id"], at=self.now, reason="Synthetic unused reserved turn.")
        ready = self.readiness(query_fixtures.Runner())
        self.assertIsNone(ready["model_allowance_continuation"])
        self.assertEqual(self.memory.snapshot()["batch_runs"][original["id"]]["counters"]["model_executions"], 0)

    def test_unfinished_generic_counts_and_untouched_supplier_scope_are_not_model_readiness(self):
        self.spent(close=True)
        state = self.memory.snapshot()
        state["concepts"]["first"]["description"]["label"] = "Sink caddy"
        record = programme.load_programme(self.memory, self.pid)
        record["progress"] = {"fit_products": 999, "current_query_profiles": 0, "fit_states": {"accepted": 0}}
        record["last_results"]["suppliers"] = {"remaining_inputs": 999,
            "preparation_wait": {"reason": "WORK_TURN_MODEL_LIMIT", "concept_ids": ["first"]}}
        self.assertIsNone(self.readiness(query_fixtures.Runner(), record=record, state=state)["model_allowance_continuation"])


class SpecificModelLaneReadinessTests(unittest.TestCase):
    def spend(self, memory, pid, now, *, policy=None):
        policy = deepcopy(DEFAULT_POLICY) if policy is None else policy
        policy.update(model_executions=1, report_slots=0)
        turn = open_work_turn(memory, pid, at=now, policy=policy)
        with bind_work_turn(memory, turn["id"]):
            Jobs(memory, turn_fixtures.Runner()).run("A", turn_fixtures.payload("spent-original"))
        close_work_turn(memory, turn["id"], at=now, reason="Synthetic finite round complete.")
        return turn

    def hint(self, memory, record, now, runner, **options):
        return programme._closed_model_allowance_continuation(memory, memory.snapshot(), record,
            runner=runner, at=now, retry_seconds=300, model_wait=False,
            semantic_hints=options.pop("semantic_hints", []), report_inputs=options.pop("report_inputs", []),
            source_questions=options.pop("source_questions", {}), update_questions=options.pop("update_questions", []), **options)

    def test_all_existing_semantic_hints_use_their_real_purpose(self):
        fixture = programme_fixtures.ProgrammeTests(); fixture.setUp(); self.addCleanup(fixture.doCleanups)
        record = fixture.prepare(); self.spend(fixture.memory, record["programme_id"], fixture.now)
        for lane in ("definition", "alias", "example", "catalogue_scope"):
            with self.subTest(lane=lane):
                self.assertEqual(self.hint(fixture.memory, record, fixture.now, object(),
                    semantic_hints=[{"model_ready": True, "signal": lane}])["purpose"], "B")

    def test_purpose_only_fit_ceiling_closes_and_permits_the_next_real_fit_turn(self):
        import test_functional_fit as fit_fixtures
        from product_radar.fit_review import _payload as fit_payload
        fixture = fit_fixtures.FunctionalFitTests(); fixture.setUp(); self.addCleanup(fixture.doCleanups)
        fixture.accept(); prepared = fixture.prepare()
        memory, now, cid = fixture.memory, fit_fixtures.NOW, fixture.cid
        for other_id, other in memory.snapshot()["concepts"].items():
            if other_id != cid:
                memory.put("concepts", other_id, dict(other, status="retired"), previous=other)
        record = programme.prepare_programme(memory, "fit-purpose-continuation", concept_ids=[cid], as_of=now)
        policy = deepcopy(DEFAULT_POLICY); policy["report_slots"] = 0
        turn = open_work_turn(memory, record["programme_id"], at=now, policy=policy)
        payload = fit_payload(memory.snapshot()["source_checks"][prepared["review_id"]])
        payload["items"] = [dict(deepcopy(payload["items"][0]), id="already-spent-fit-" + str(n)) for n in range(10)]
        with bind_work_turn(memory, turn["id"]):
            Jobs(memory, fit_fixtures.FunctionalFitRunner()).run("E", payload)
        original = deepcopy(memory.snapshot()["batch_runs"][turn["id"]])
        self.assertEqual(original["counters"]["purpose_items"]["E:fit"], 10)
        self.assertEqual(original["counters"]["model_executions"], 1)
        self.assertLess(original["counters"]["model_executions"], original["policy"]["model_executions"])
        steps = {stage: (lambda *_args: {"state": "nothing_new"}) for stage in programme.STAGES}
        steps["fit"] = programme._fit
        runner = fit_fixtures.FunctionalFitRunner()
        options = dict(root=memory.root.parent, programme_id=record["programme_id"], handlers={}, runner=runner,
                       steps=steps, as_of=now, turn_policy=policy)
        first = programme.advance_ready_programme_turn(memory, **options)
        self.assertEqual(first["work_turn"]["id"], turn["id"])
        self.assertEqual(first["work_turn"]["counters"], original["counters"])
        self.assertEqual(runner.calls, [])
        closed = programme.load_programme(memory, record["programme_id"])
        hint = self.hint(memory, closed, now, runner)
        self.assertEqual(hint["purpose"], "E:fit")
        self.assertEqual(hint["reason"], "WORK_TURN_PURPOSE_LIMIT")
        second = programme.advance_ready_programme_turn(memory, **options)
        self.assertNotEqual(second["work_turn"]["id"], turn["id"])
        self.assertEqual(len(runner.calls), 1)
        self.assertEqual(second["work_turn"]["counters"]["purpose_items"]["E:fit"], 1)
        self.assertTrue(second["meaningful_progress"])

    def test_a_spent_fit_purpose_does_not_justify_a_ready_unrelated_definition(self):
        import test_functional_fit as fit_fixtures
        from product_radar.fit_review import _payload as fit_payload
        fixture = fit_fixtures.FunctionalFitTests(); fixture.setUp(); self.addCleanup(fixture.doCleanups)
        fixture.accept(); prepared = fixture.prepare()
        memory, now = fixture.memory, fit_fixtures.NOW
        record = programme.prepare_programme(memory, "unrelated-purpose", concept_ids=[], as_of=now)
        turn = open_work_turn(memory, record["programme_id"], at=now)
        payload = fit_payload(memory.snapshot()["source_checks"][prepared["review_id"]])
        payload["items"] = [dict(deepcopy(payload["items"][0]), id="already-spent-fit-" + str(n)) for n in range(10)]
        with bind_work_turn(memory, turn["id"]):
            Jobs(memory, fit_fixtures.FunctionalFitRunner()).run("E", payload)
        close_work_turn(memory, turn["id"], at=now, reason="Synthetic unrelated fit purpose consumed.")
        self.assertIsNone(self.hint(memory, record, now, object(), semantic_hints=[{"model_ready": True}]))

    def test_actual_three_item_fit_batch_cannot_fit_two_remaining_purpose_items(self):
        from product_radar.fit_description import FUNCTIONAL_CONTRACT
        from product_radar.fit_review import prepare_practical_fit_reviews, _payload as fit_payload
        from product_radar.functional_types import accept_functional_definition, effective_functional_definition, DEFINITION_FIELDS
        from test_functional_fit import FunctionalFitRunner
        from test_functional_source_market import functional_pool_fixture, NOW
        temporary = tempfile.TemporaryDirectory(); self.addCleanup(temporary.cleanup)
        memory, ids = functional_pool_fixture(Path(temporary.name), count=3, with_market=False)
        for cid in ids:
            state = memory.snapshot(); current = effective_functional_definition(state, cid)
            definition = {field: current[field] for field in DEFINITION_FIELDS}
            definition["mechanism"] += " with added support"
            basis = state["source_checks"][current["definition_id"]]["basis_refs"]
            accept_functional_definition(memory, cid, definition, basis_refs=list(basis),
                reviewer="synthetic", reviewed_at=NOW, expected_meaning_hash=current["meaning_hash"], example_decisions={})
        prepared = prepare_practical_fit_reviews(memory, concept_ids=ids, as_of=NOW, contract_version=FUNCTIONAL_CONTRACT)
        record = programme.prepare_programme(memory, "partial-fit-purpose", concept_ids=ids, as_of=NOW)
        policy = deepcopy(DEFAULT_POLICY); policy["report_slots"] = 0
        turn = open_work_turn(memory, record["programme_id"], at=NOW, policy=policy)
        payload = fit_payload(memory.snapshot()["source_checks"][prepared[0]["review_id"]])
        payload["items"] = [dict(deepcopy(payload["items"][0]), id="spent-fit-" + str(n)) for n in range(8)]
        with bind_work_turn(memory, turn["id"]):
            Jobs(memory, FunctionalFitRunner()).run("E", payload)
        steps = {stage: (lambda *_args: {"state": "nothing_new"}) for stage in programme.STAGES}
        steps["fit"] = programme._fit
        runner = FunctionalFitRunner()
        options = dict(root=memory.root.parent, programme_id=record["programme_id"], handlers={}, runner=runner,
                       steps=steps, as_of=NOW, turn_policy=policy)
        first = programme.advance_ready_programme_turn(memory, **options)
        self.assertEqual(first["work_turn"]["counters"]["purpose_items"]["E:fit"], 8)
        self.assertEqual(runner.calls, [])
        hint = self.hint(memory, programme.load_programme(memory, record["programme_id"]), NOW, runner)
        self.assertEqual((hint["purpose"], hint["reason"]), ("E:fit", "WORK_TURN_PURPOSE_LIMIT"))
        second = programme.advance_ready_programme_turn(memory, **options)
        self.assertNotEqual(second["work_turn"]["id"], turn["id"])
        self.assertEqual(second["work_turn"]["counters"]["purpose_items"]["E:fit"], 3)
        self.assertEqual(len(runner.calls), 1)
        self.assertEqual(len(runner.calls[0][1]["items"]), 3)

    def test_functional_writing_waits_for_real_questions_not_finished_prose(self):
        from test_functional_report_packet import functional_packet_fixture, RecordingWriter
        from test_functional_source_market import NOW
        from product_radar.report_narration import generate_report_narratives
        temporary = tempfile.TemporaryDirectory(); self.addCleanup(temporary.cleanup)
        memory, _, issue, dataset = functional_packet_fixture(Path(temporary.name))
        record = programme.prepare_programme(memory, "writing-continuation", concept_ids=[], as_of=NOW)
        self.spend(memory, record["programme_id"], NOW)
        runner = RecordingWriter()
        options = {"report_inputs": [{"dataset": dataset, "languages": ("en", "hi-Latn")} ]}
        self.assertEqual(self.hint(memory, record, NOW, runner, **options)["purpose"], "G")
        english = generate_report_narratives(memory, issue, dataset, runner, languages=("en",))
        self.assertEqual(english["failures"], {})
        self.assertEqual(self.hint(memory, record, NOW, runner, **options)["purpose"], "G")
        complete = generate_report_narratives(memory, issue, dataset, runner)
        self.assertEqual(complete["failures"], {})
        calls, before = len(runner.payloads), memory.snapshot_with_provenance()
        self.assertIsNone(self.hint(memory, record, NOW, runner, **options))
        different = RecordingWriter(); different.identity = "different-synthetic-report-runner"
        self.assertIsNone(self.hint(memory, record, NOW, different, **options))
        self.assertEqual(memory.snapshot_with_provenance(), before)
        self.assertEqual(len(runner.payloads), calls)
        self.assertEqual(different.payloads, [])

    def test_actual_saved_discovery_description_and_original_seed_questions_continue(self):
        import test_catalogue_discovery as discovery_fixtures
        import test_catalogue_seed_discovery as seed_fixtures
        from product_radar.catalogue_discovery import create_catalogue_discovery_step, prepare_catalogue_seed_cycle
        from test_catalogue_seed import SeedRunner
        fixture = discovery_fixtures.DiscoveryTests(); fixture.setUp(); self.addCleanup(fixture.doCleanups)
        handlers = {"meta-ads": fixture.handler()}
        cycle = fixture.prepare(handlers=handlers); fixture.advance(cycle, handlers)
        record = programme.prepare_programme(fixture.memory, "programme-one", concept_ids=[], as_of=discovery_fixtures.NOW)
        self.spend(fixture.memory, record["programme_id"], discovery_fixtures.NOW)
        step = create_catalogue_discovery_step(fixture.catalogue)
        self.assertEqual(self.hint(fixture.memory, record, discovery_fixtures.NOW, object(), discovery_step=step)["purpose"], "A")
        seed = seed_fixtures.SeedDiscoveryTests(); seed.setUp(); self.addCleanup(seed.doCleanups)
        manifest = seed.manifest(seed.prepared())
        record = programme.prepare_programme(seed.memory, seed.owner["programme_id"], concept_ids=[], as_of=seed_fixtures.NOW)
        prepare_catalogue_seed_cycle(seed.memory, record["programme_id"], manifest, as_of=seed_fixtures.NOW)
        self.spend(seed.memory, record["programme_id"], seed_fixtures.NOW)
        self.assertEqual(self.hint(seed.memory, record, seed_fixtures.NOW, SeedRunner(),
            discovery_step=seed.step(manifest, renewable_waves=True, shared_marketplace=True))["purpose"], "C")

    def test_malformed_saved_correction_receipt_is_not_fresh_model_authority(self):
        from test_functional_report_packet import functional_packet_fixture, RecordingWriter
        from test_functional_source_market import NOW
        from product_radar.report_narration import generate_report_narratives
        from product_radar.research_ai import current_attempt
        temporary = tempfile.TemporaryDirectory(); self.addCleanup(temporary.cleanup)
        memory, _, issue, dataset = functional_packet_fixture(Path(temporary.name))
        record = programme.prepare_programme(memory, "malformed-writing-continuation", concept_ids=[], as_of=NOW)
        self.spend(memory, record["programme_id"], NOW)
        bad_id = dataset["products"][0]["concept_id"]
        runner = RecordingWriter(bad_id=bad_id)
        output = generate_report_narratives(memory, issue, dataset, runner)
        self.assertTrue(output["failures"])
        correction_id, correction = next((job_id, job) for job_id, job in memory.snapshot()["jobs"].items()
            if job["request"]["role"] == "G" and "narration_repair" in job["request"]["payload"])
        folder = current_attempt(memory.root / "ai_jobs" / correction_id)
        before, calls = memory.snapshot_with_provenance(), len(runner.payloads)
        options = {"report_inputs": [{"dataset": dataset, "languages": ("en", "hi-Latn")} ]}
        for malformed in ([], {"request_hash": digest(correction["request"])}):
            with self.subTest(malformed=malformed):
                atomic_json(folder / "result.json", malformed)
                self.assertIsNone(self.hint(memory, record, NOW, runner, **options))
                self.assertEqual(memory.snapshot_with_provenance(), before)
                self.assertEqual(len(runner.payloads), calls)

    def test_actual_saved_source_questions_are_ready_even_without_any_healthy_source_handler(self):
        import test_source_interpretation as source_fixtures
        from product_radar.source_interpretation import _questions
        fixture = source_fixtures.SourceInterpretationTests(); fixture.setUp(); self.addCleanup(fixture.doCleanups)
        fixture.acquired()
        record = programme.prepare_programme(fixture.memory, "source-only-continuation", source_run_id="full-list",
            concept_ids=["first"], as_of=source_fixtures.NOW)
        self.spend(fixture.memory, record["programme_id"], source_fixtures.NOW)
        questions = _questions(fixture.memory.snapshot(), run_id="full-list", as_of=source_fixtures.NOW)
        before = fixture.memory.snapshot_with_provenance()
        hint = self.hint(fixture.memory, record, source_fixtures.NOW, source_fixtures.Runner(),
                         source_questions={record["programme_id"]: questions})
        self.assertEqual(hint["purpose"], "E:source_support")
        self.assertEqual(fixture.memory.snapshot_with_provenance(), before)
        future = (timestamp(source_fixtures.NOW) + timedelta(hours=1)).isoformat()
        for question in questions:
            target = question["task_id"] + ":" + question["concept_id"]
            record["deferrals"]["match:" + target] = {"stage": "match", "target": target, "binding": digest(question),
                "reason": "AI_MODEL_RETRY_WAIT", "at": source_fixtures.NOW, "next_retry_at": future}
        self.assertIsNone(self.hint(fixture.memory, record, source_fixtures.NOW, source_fixtures.Runner(),
                                  source_questions={record["programme_id"]: questions}))

    def test_actual_usable_supplier_capture_qualifies_but_its_future_retry_does_not(self):
        import test_supplier_capture_store as supplier_fixtures
        fixture = supplier_fixtures.SavedSupplierCaptureTests(); fixture.setUp(); self.addCleanup(fixture.doCleanups)
        previous = fixture.memory.snapshot()["concepts"][supplier_fixtures.REQUEST["concept_id"]]
        concept = deepcopy(previous)
        concept["description"].update(mechanism="A draining basket holds sponges beside the sink.", features=["Drain slots"])
        fixture.memory.put("concepts", supplier_fixtures.REQUEST["concept_id"], concept, previous=previous)
        captured = fixture.capture()
        record = programme.prepare_programme(fixture.memory, "supplier-only-continuation",
            concept_ids=[supplier_fixtures.REQUEST["concept_id"]], as_of=supplier_fixtures.NOW)
        self.spend(fixture.memory, record["programme_id"], supplier_fixtures.NOW)
        hint = self.hint(fixture.memory, record, supplier_fixtures.NOW, object())
        self.assertEqual(hint["purpose"], "F")
        future = (timestamp(supplier_fixtures.NOW) + timedelta(hours=1)).isoformat()
        capture_id = captured["capture_id"]
        record["deferrals"]["suppliers:" + capture_id] = {"stage": "suppliers", "target": capture_id,
            "binding": capture_id, "reason": "AI_MODEL_RETRY_WAIT", "at": supplier_fixtures.NOW, "next_retry_at": future}
        self.assertIsNone(self.hint(fixture.memory, record, supplier_fixtures.NOW, object()))

    def test_exact_material_update_question_uses_e_and_respects_its_saved_retry(self):
        fixture = programme_fixtures.ProgrammeTests(); fixture.setUp(); self.addCleanup(fixture.doCleanups)
        record = fixture.prepare(); self.spend(fixture.memory, record["programme_id"], fixture.now)
        entry = {"binding": {"concept_id": "first", "input_hash": "a" * 64},
                 "item": {"id": "synthetic-prepared-update"}}
        # The existing update-work producer validates the full real comparison;
        # this test isolates continuation's use of that producer's exact output.
        with patch("product_radar.material_updates.update_payload", return_value={"items": [entry["item"]]}) as payload, \
                patch.object(programme, "_model_question_ready", return_value={"ready": True,
                    "fresh_items": [entry["item"]], "original_item_counts": []}):
            hint = self.hint(fixture.memory, record, fixture.now, object(), update_questions=[entry])
            self.assertEqual(hint["purpose"], "E")
            self.assertEqual(payload.call_count, 1)
            future = (timestamp(fixture.now) + timedelta(hours=1)).isoformat()
            record["deferrals"]["material_updates:first"] = {"stage": "material_updates", "target": "first",
                "binding": digest(entry["binding"]), "reason": "AI_MODEL_RETRY_WAIT", "at": fixture.now, "next_retry_at": future}
            self.assertIsNone(self.hint(fixture.memory, record, fixture.now, object(), update_questions=[entry]))


if __name__ == "__main__":
    unittest.main()
