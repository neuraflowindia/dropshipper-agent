"""Opt-in existing-command continuation with synthetic local work only."""

from copy import deepcopy
from datetime import timedelta
from io import StringIO
import json
import importlib.util
from pathlib import Path
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
import product_radar.programme as programme
from product_radar.models import SafetyError, timestamp
from product_radar.research_ai import Jobs
from product_radar.research_memory import key
from product_radar.source_coverage import execute_source_run, prepare_source_tasks
from product_radar.storage import Journal, atomic_json
from product_radar.work_turns import DEFAULT_POLICY
import test_programme as fixtures
from test_research_flow import FakeRunner


class CooperativeProgrammeTests(unittest.TestCase):
    def setUp(self):
        self.fixture = fixtures.ProgrammeTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.memory, self.root, self.now = self.fixture.memory, self.fixture.root, self.fixture.now
        self.fixture.prepare()

    def advance(self, **options):
        return programme.advance_ready_programme_turn(self.memory, root=self.root,
            programme_id=options.pop("programme_id", "whole-programme"), as_of=options.pop("as_of", self.now),
            handlers=options.pop("handlers", {}), steps=options.pop("steps", self.fixture.noops()), **options)

    def turns(self):
        return [row for row in self.memory.snapshot()["batch_runs"].values() if row.get("kind") == "work-turn-v1"]

    def cli(self):
        path = Path(__file__).resolve().parents[1] / "scripts/run_programme.py"
        specification = importlib.util.spec_from_file_location("cooperative_programme_cli", path)
        module = importlib.util.module_from_spec(specification)
        specification.loader.exec_module(module)
        return module

    def test_latest_due_week_is_admitted_without_waiting_for_old_fixed_point(self):
        later = (timestamp(self.now) + timedelta(days=8)).isoformat()
        first = self.advance(programme_id=None, as_of=later)
        current_id = "programme-default-" + programme.research_week(later)
        current = programme.load_programme(self.memory, current_id)
        self.assertEqual(current["concept_ids"], [])
        self.assertEqual(first["programme_id"], "whole-programme")
        second = self.advance(programme_id=None, as_of=later)
        self.assertEqual(second["programme_id"], current_id)
        self.assertGreater(second["work_turn"]["turn_sequence"], first["work_turn"]["turn_sequence"])
        self.assertEqual(programme.load_programme(self.memory, "whole-programme")["concept_ids"], ["first", "second"])

    def test_unchanged_wait_does_not_create_empty_turns_or_a_default_delay(self):
        first = self.advance()
        self.assertTrue(first["round_completed"])
        self.assertFalse(first["meaningful_progress"])
        self.assertIsNone(first["next_retry_at"])
        for at in (self.now, (timestamp(self.now) + timedelta(seconds=60)).isoformat()):
            waiting = self.advance(as_of=at)
            self.assertEqual(waiting["state"], "all_waiting")
            self.assertFalse(waiting["round_completed"])
            self.assertEqual(waiting["next_trigger"], "saved_input_reply_or_connection_change")
        self.assertEqual(len(self.turns()), 1)

    def test_finished_round_and_all_waiting_exit_keep_the_actual_next_action(self):
        from product_radar.operations import operation_run
        with operation_run(self.root) as recorder:
            first = self.advance()
            waiting = self.advance()
        self.assertTrue(first["round_completed"])
        self.assertEqual(waiting["state"], "all_waiting")
        rows = [json.loads(line) for line in (recorder.folder / (recorder.run_id + ".jsonl")).read_text().splitlines()]
        finished = next(row for row in rows if row["event"] == "round_finished")
        deferred = next(row for row in rows if row["event"] == "programme_waiting")
        self.assertEqual(finished["next_action"], first["next_action"])
        self.assertEqual(deferred["next_action"], "wait_for_saved_trigger")
        self.assertEqual(finished["turn_id"], first["work_turn"]["id"])

    def test_future_model_wait_does_not_hide_ready_original_source(self):
        record = programme.load_programme(self.memory, "whole-programme")
        future = (timestamp(self.now) + timedelta(hours=2)).isoformat()
        record["model_pause"] = {"reason": "AI_RATE_LIMITED", "at": self.now, "next_retry_at": future}
        record["next_retry_at"] = future
        programme._save(self.memory, record)
        self.advance()
        calls = []
        def handler(context):
            calls.append(context["request"])
            context["record_dispatch"]()
            return {"schema_version": 1, "route": context["route"], "acquisition_id": context["acquisition_id"],
                "observed_at": self.now, "state": "empty", "dispatch_started": True,
                "reference": context["acquisition_id"] + "/receipt.json", "capture_ids": [], "observation_ids": [],
                "target_results": [], "reason": "Synthetic complete empty response.", "retryable": False}
        prepare_source_tasks(self.memory, [{"route": "meta-ads", "request": {"query": "sink caddy", "country": "IN"},
            "concept_ids": ["first"]}], run_id="existing-source-run", as_of=self.now)
        steps = self.fixture.noops()
        steps["acquire"] = lambda memory, _record, _context: execute_source_run(memory, "existing-source-run",
            handlers={"meta-ads": handler}, as_of=self.now, bounded_call=lambda callback, context, _timeout: callback(context))
        result = self.advance(steps=steps, handlers={"meta-ads": handler})
        self.assertEqual(len(calls), 1)
        self.assertEqual(result["work_turn"]["counters"]["source_callbacks"], 1)
        self.assertEqual(result["work_turn"]["counters"]["model_executions"], 0)
        self.assertEqual(programme.load_programme(self.memory, "whole-programme")["model_pause"]["next_retry_at"], future)

    def test_source_readiness_observes_neutral_hook_without_launching_or_writing(self):
        prepare_source_tasks(self.memory, [{"route": "meta-ads", "request": {"query": "sink caddy", "country": "IN"},
            "concept_ids": ["first"]}], run_id="existing-source-run", as_of=self.now)
        record = programme.load_programme(self.memory, "whole-programme")
        record["source_run_id"] = "existing-source-run"
        before = self.memory.snapshot()
        for neutral in (False, True):
            calls = []
            def hint(context):
                calls.append(context)
                return {"reason": "Synthetic original file hint", "present": True}
            def forbidden(*_args):
                self.fail("A pure readiness check must not launch a source or use a superseded hook.")
            installed = {"handler": forbidden, "marketplace_saved_hint": hint}
            if neutral:
                installed.update(source_saved_hint=hint, marketplace_saved_hint=forbidden)
            result = programme._source_turn_readiness(self.memory, before, record, {"meta-ads": installed}, self.now)
            self.assertEqual(len(calls), 1)
            self.assertEqual(calls[0]["request"]["query"], "sink caddy")
            self.assertEqual(result["signal"][0][4]["present"], True)
            self.assertEqual(self.memory.snapshot(), before)

    def test_ordinary_source_readiness_prefers_neutral_due_hook_and_preserves_legacy_fallback(self):
        prepared = prepare_source_tasks(self.memory, [{"route": "meta-ads",
            "request": {"query": "sink caddy", "country": "IN"}, "concept_ids": ["first"]}],
            run_id="existing-source-run", as_of=self.now)
        task_id = prepared["task_ids"][0]
        task = self.memory.snapshot()["query_tasks"][task_id]
        self.memory.put("query_tasks", task_id,
            dict(task, status="blocked", reconcile_required=True), previous=task)
        record = programme.load_programme(self.memory, "whole-programme")
        record["source_run_id"] = "existing-source-run"
        before = self.memory.snapshot()
        future = (timestamp(self.now) + timedelta(minutes=30)).isoformat()
        for neutral in (False, True):
            calls = []
            def hint(context):
                calls.append(context)
                return {"ready": False, "local_saved": False, "next_retry_at": future,
                        "reason": "Synthetic original is still waiting."}
            def forbidden(*_args):
                self.fail("Readiness must not launch or invoke a superseded access hook.")
            installed = {"handler": forbidden, "reconcile": forbidden, "source_access_hint": hint}
            if neutral:
                installed.update(source_due_hint=hint, source_access_hint=forbidden)
            result = programme._source_turn_readiness(self.memory, before, record,
                {"meta-ads": installed}, self.now)
            self.assertEqual(len(calls), 1)
            self.assertEqual(calls[0]["task_id"], task_id)
            self.assertTrue(calls[0]["reconciling"])
            self.assertFalse(result["callback_ready"])
            self.assertIn(future, result["waits"])
            self.assertEqual(self.memory.snapshot(), before)

    def test_direct_cooperative_entry_composes_one_shared_factory_for_readiness_and_work(self):
        with patch("product_radar.source_coverage.installed_source_handlers", return_value={}) as installed:
            self.advance(handlers=None)
        self.assertEqual(installed.call_count, 1)
        self.assertEqual(installed.call_args.kwargs, {
            "marketplace_options": {"shared_acquisitions": True},
            "meta_options": {"shared_acquisitions": True},
            "trends_options": {"shared_acquisitions": True},
            "supplier_options": {"respect_access_waits": True, "shared_acquisitions": True}})

    def test_cooperative_original_and_follow_on_builders_share_supplier_mode(self):
        self.fixture.product("supplier-builder-later")
        record = programme.load_programme(self.memory, "whole-programme")
        programme._register_discovered_products(self.memory, record,
            {"new_concept_ids": ["supplier-builder-later"]}, self.now)
        scope = next(iter(record["follow_on_reviews"].values()))
        programme._save(self.memory, record)
        with patch("product_radar.source_coverage.product_source_request_builder",
                   side_effect=lambda **_kwargs: lambda *_args: None) as builder:
            self.advance()
        self.assertEqual({call.kwargs["run_id"] for call in builder.call_args_list},
            {record["source_run_id"], scope["source_run_id"]})
        self.assertTrue(all(call.kwargs.get("shared_suppliers") is True for call in builder.call_args_list))

    def test_legacy_builder_keeps_its_original_supplier_mode(self):
        with patch("product_radar.source_coverage.product_source_request_builder",
                   side_effect=lambda **_kwargs: lambda *_args: None) as builder:
            programme.advance_programme(self.memory, "whole-programme", root=self.root,
                handlers={}, steps=self.fixture.noops(), as_of=self.now, max_cycles=1)
        self.assertEqual(builder.call_count, 1)
        self.assertNotIn("shared_suppliers", builder.call_args.kwargs)

    def test_model_wait_settles_without_spinning_and_wakes_at_actual_deadline(self):
        record = programme.load_programme(self.memory, "whole-programme")
        future = (timestamp(self.now) + timedelta(hours=2)).isoformat()
        record["model_pause"] = {"reason": "AI_RATE_LIMITED", "at": self.now, "next_retry_at": future}
        programme._save(self.memory, record)
        self.advance(runner=FakeRunner())
        waiting = self.advance(runner=FakeRunner())
        self.assertEqual(waiting["state"], "all_waiting")
        self.assertEqual(waiting["next_retry_at"], future)
        self.assertEqual(len(self.turns()), 1)
        due = self.advance(as_of=future, runner=FakeRunner())
        self.assertTrue(due["round_completed"])
        self.assertEqual(len(self.turns()), 2)

    def test_source_deadline_wakes_even_without_a_committed_change(self):
        planned = prepare_source_tasks(self.memory, [{"route": "meta-ads", "request": {"query": "sink caddy", "country": "IN"},
            "concept_ids": ["first"]}], run_id="existing-source-run", as_of=self.now)
        task_id = planned["task_ids"][0]
        task = self.memory.snapshot()["query_tasks"][task_id]
        future = (timestamp(self.now) + timedelta(minutes=30)).isoformat()
        self.memory.put("query_tasks", task_id, dict(task, next_retry_at=future), previous=task)
        handler = lambda _context: self.fail("Readiness must never collect")
        self.advance(handlers={"meta-ads": handler})
        before = self.memory.snapshot()
        record = programme.load_programme(self.memory, "whole-programme")
        waiting = programme.programme_turn_readiness(self.memory, record, root=self.root,
            handlers={"meta-ads": handler}, steps=self.fixture.noops(), as_of=self.now)
        due = programme.programme_turn_readiness(self.memory, record, root=self.root,
            handlers={"meta-ads": handler}, steps=self.fixture.noops(), as_of=future)
        self.assertFalse(waiting["ready"])
        self.assertEqual(waiting["next_retry_at"], future)
        self.assertTrue(due["ready"])
        self.assertTrue(due["ready_lanes"]["backlog"])
        self.assertEqual(self.memory.snapshot(), before)

    def test_original_only_retry_wait_is_not_progress_and_wakes_when_due(self):
        prepared = prepare_source_tasks(self.memory, [{"route": "meta-ads",
            "request": {"query": "sink caddy", "country": "IN"}, "concept_ids": ["first"]}],
            run_id="existing-source-run", as_of=self.now)
        task_id = prepared["task_ids"][0]
        old = self.memory.snapshot()["query_tasks"][task_id]
        future = (timestamp(self.now) + timedelta(minutes=30)).isoformat()
        current = dict(old, status="blocked", reconcile_required=True,
                       reason="SOURCE_ORIGINAL_RECEIPT_PENDING", next_retry_at=future)
        self.memory.put("query_tasks", task_id, current, previous=old)
        callbacks = []
        def reconcile(_context):
            callbacks.append(True)
            raise SafetyError("SOURCE_ORIGINAL_RECEIPT_PENDING")
        handlers = {"meta-ads": {"handler": reconcile, "reconcile": reconcile}}
        steps = self.fixture.noops()
        def schedule_retry(memory, _record, _context):
            task = memory.snapshot()["query_tasks"][task_id]
            later = (timestamp(task["next_retry_at"]) + timedelta(minutes=1)).isoformat()
            memory.put("query_tasks", task_id, dict(task, next_retry_at=later), previous=task)
            return {"state": "waiting", "reason": "SOURCE_ORIGINAL_RECEIPT_PENDING"}
        steps["acquire"] = schedule_retry
        result = self.advance(steps=steps, handlers=handlers)
        self.assertFalse(result["meaningful_progress"])
        waiting = self.advance(steps=steps, handlers=handlers)
        self.assertFalse(waiting["round_completed"])
        self.assertEqual(callbacks, [])
        self.assertEqual(len(self.turns()), 1)
        record = programme.load_programme(self.memory, "whole-programme")
        due = programme.programme_turn_readiness(self.memory, record, root=self.root,
            handlers=handlers, steps=steps, as_of=waiting["next_retry_at"])
        self.assertTrue(due["ready"])
        self.assertTrue(due["ready_lanes"]["backlog"])

    def test_original_missing_receipt_settles_after_its_one_recovery_probe(self):
        def original(context):
            context["record_dispatch"]()
            raise TimeoutError("Synthetic original result has not arrived")
        def reconcile(_context):
            raise SafetyError("SOURCE_ORIGINAL_RECEIPT_PENDING")
        handlers = {"meta-ads": {"handler": original, "reconcile": reconcile}}
        prepare_source_tasks(self.memory, [{"route": "meta-ads",
            "request": {"query": "sink caddy", "country": "IN"}, "concept_ids": ["first"]}],
            run_id="existing-source-run", as_of=self.now)
        steps = self.fixture.noops()
        steps["acquire"] = lambda memory, _record, _context: execute_source_run(memory, "existing-source-run",
            handlers=handlers, as_of=self.now, bounded_call=lambda callback, context, _timeout: callback(context))
        first = self.advance(steps=steps, handlers=handlers)
        self.assertTrue(first["meaningful_progress"])
        recovered = self.advance(steps=steps, handlers=handlers)
        self.assertFalse(recovered["meaningful_progress"])
        waiting = self.advance(steps=steps, handlers=handlers)
        self.assertEqual(waiting["state"], "all_waiting")
        self.assertEqual(len(self.turns()), 2)

    def test_purpose_order_and_discovery_position_rotate_only_when_opted_in(self):
        order = []
        steps = {stage: (lambda _memory, _record, _context, name=stage:
                         (order.append(name) or {"state": "checked"})) for stage in programme.STAGES}
        discovery = lambda *_args: (order.append("discovery") or {"state": "checked"})
        for expected_start, expected_discovery in (("profiles", "before"), ("profiles", "after"), ("fit", "before")):
            order.clear()
            result = programme.advance_programme_turn(self.memory, "whole-programme", root=self.root,
                handlers={}, steps=steps, discovery_step=discovery, as_of=self.now)
            stages = [name for name in order if name != "discovery"]
            self.assertEqual(stages[0], expected_start)
            self.assertEqual(order[0] == "discovery", expected_discovery == "before")
            turn = self.memory.snapshot()["batch_runs"][result["work_turn"]["id"]]
            self.assertEqual(turn["cursors"]["discovery_position"], expected_discovery)
        order.clear()
        programme.advance_programme(self.memory, "whole-programme", root=self.root, handlers={},
            steps=steps, discovery_step=discovery, as_of=self.now, max_cycles=1)
        self.assertEqual(order[0], "discovery")
        self.assertEqual(order[1], "profiles")

    def test_continually_ready_discovery_cannot_take_every_model_slot(self):
        from product_radar.work_turns import active_work_turn
        runner = FakeRunner()
        policy = deepcopy(DEFAULT_POLICY)
        policy.update(model_executions=1, report_slots=0)
        profile_applied = []
        def profile(memory, _record, _context):
            job_id, result = Jobs(memory, runner).run("A", {"items": [{"id": "pending-profile",
                "source_text": {"title": "Blue sink caddy"}}]})
            profile_applied.append(job_id)
            return {"state": "applied", "job_id": job_id, "accepted": result["accepted"]}
        def discovery(memory, _record, _context):
            sequence = active_work_turn(memory)["programme_turn_sequence"]
            job_id, _ = Jobs(memory, runner).run("A", {"items": [{"id": "fresh-discovery-" + str(sequence),
                "source_text": {"title": "Blue sink caddy"}}]})
            return {"state": "checked", "job_id": job_id}
        steps = self.fixture.noops(); steps["profiles"] = profile
        self.advance(steps=steps, runner=runner, discovery_step=discovery, turn_policy=policy)
        self.assertEqual(profile_applied, [])
        second = self.advance(steps=steps, runner=runner, discovery_step=discovery, turn_policy=policy)
        self.assertEqual(len(profile_applied), 1)
        self.assertEqual(len(runner.calls), 2)
        self.assertEqual(second["work_turn"]["counters"]["model_executions"], 1)
        self.assertIsNone(programme.load_programme(self.memory, "whole-programme")["model_pause"])

    def test_photo_readiness_uses_actual_configured_media_folder(self):
        record = programme.load_programme(self.memory, "whole-programme")
        media_root = self.root / "configured-photos"
        pool = {"facts_by_concept": {"first": {"image_candidates": [{"url": "https://example.com/image.jpg",
            "relationship": "exact_product"}]}}}
        with patch("product_radar.report_selection.build_report_pool", return_value=pool), \
                patch("product_radar.product_images.current_public_image", return_value=None) as current, \
                patch("product_radar.product_images.product_image_readiness", return_value={"ready": True}):
            self.assertTrue(programme._photo_turn_ready(self.memory, self.memory.snapshot(), record, self.now,
                                                       media_root=media_root))
        self.assertEqual(current.call_args.kwargs["media_root"], media_root)

    def test_owner_pause_and_busy_writer_create_no_turn_or_due_week(self):
        later = (timestamp(self.now) + timedelta(days=8)).isoformat()
        before = self.memory.snapshot()
        with Journal(self.memory.root / "programme_writer").locked():
            with self.assertRaisesRegex(SafetyError, "RUNNER_ALREADY_ACTIVE"):
                self.advance(programme_id=None, as_of=later)
        self.assertEqual(self.memory.snapshot(), before)
        programme.set_programme_service_state(self.memory, enabled=False, as_of=self.now)
        paused = self.advance(programme_id=None, as_of=later)
        self.assertEqual(paused["state"], "owner_paused")
        self.assertEqual(self.memory.snapshot(), before)

    def test_open_turn_restarts_with_original_counters_not_another_allowance(self):
        from product_radar.work_turns import open_work_turn, bind_work_turn, reserve_source_callback
        policy = deepcopy(DEFAULT_POLICY)
        policy.update(source_callbacks=1, exploration_slots=0)
        turn = open_work_turn(self.memory, "whole-programme", at=self.now, policy=policy)
        with bind_work_turn(self.memory, turn["id"]):
            reserve_source_callback(self.memory, "already-spent", at=self.now)
        result = self.advance(turn_policy=policy)
        self.assertEqual(result["work_turn"]["id"], turn["id"])
        self.assertEqual(result["work_turn"]["counters"]["source_callbacks"], 1)
        self.assertEqual(len(self.turns()), 1)

    def test_cold_programme_restart_preserves_open_policy_then_uses_new_limits(self):
        from product_radar.research_memory import ResearchMemory
        from product_radar.work_turns import active_work_turn, open_work_turn, bind_work_turn, reserve_source_callback
        for previous_limit, next_limit in ((2, 12), (12, 2)):
            with self.subTest(previous=previous_limit, following=next_limit):
                prior_policy = deepcopy(DEFAULT_POLICY)
                prior_policy.update(source_callbacks=previous_limit,
                                    exploration_slots=min(2, previous_limit))
                next_policy = deepcopy(DEFAULT_POLICY)
                next_policy.update(source_callbacks=next_limit,
                                   exploration_slots=min(2, next_limit))
                original = open_work_turn(self.memory, "whole-programme", at=self.now, policy=prior_policy)
                with bind_work_turn(self.memory, original["id"]):
                    reserve_source_callback(self.memory, "already-spent", at=self.now)
                original_row = deepcopy(self.memory.snapshot()["batch_runs"][original["id"]])
                cold = ResearchMemory(self.memory.root, Journal(self.memory.journal.root), evidence_type="synthetic")
                seen = []
                steps = self.fixture.noops()
                def inspect_original(memory, _record, _context):
                    seen.append(deepcopy(active_work_turn(memory)))
                    return {"state": "original_allowance_verified"}
                steps["profiles"] = inspect_original
                result = programme.advance_programme_turn(cold, "whole-programme", root=self.root,
                    handlers={}, steps=steps, as_of=self.now, turn_policy=next_policy)
                self.assertEqual(result["work_turn"]["id"], original["id"])
                self.assertEqual(seen, [original_row])
                self.assertEqual(result["work_turn"]["counters"]["source_callbacks"], 1)
                following = programme.advance_programme_turn(cold, "whole-programme", root=self.root,
                    handlers={}, steps=self.fixture.noops(), as_of=self.now, turn_policy=next_policy)
                next_row = cold.snapshot()["batch_runs"][following["work_turn"]["id"]]
                self.assertNotEqual(next_row["id"], original["id"])
                self.assertEqual(next_row["policy"], next_policy)
                self.assertEqual(next_row["counters"]["source_callbacks"], 0)

    def test_filesystem_reply_wakes_and_reuses_while_model_wait_is_active(self):
        runner = FakeRunner()
        payload = {"items": [{"id": "first", "source_text": {"title": "Blue sink caddy"}}]}
        job_id, _ = Jobs(self.memory, runner).run("A", payload)
        folder = self.memory.root / "ai_jobs" / job_id
        result = json.loads((folder / "result.json").read_text())
        # Keep the fixture's saved original intent, but make its reply arrive
        # only after the settled checkpoint. Moving a temporary fixture file
        # changes no production artifact and does not launch a model.
        held = folder / "held-result.json"
        (folder / "result.json").rename(held)
        record = programme.load_programme(self.memory, "whole-programme")
        record["model_pause"] = {"reason": "AI_RATE_LIMITED", "at": self.now,
            "next_retry_at": (timestamp(self.now) + timedelta(hours=2)).isoformat()}
        programme._save(self.memory, record)
        self.advance()
        atomic_json(folder / "result.json", result)
        calls = []
        steps = self.fixture.noops()
        def saved_only(memory, _record, _context):
            calls.append(Jobs(memory, runner).run("A", payload)[0])
            return {"state": "saved_reply_applied"}
        steps["profiles"] = saved_only
        resumed = self.advance(steps=steps)
        self.assertTrue(resumed["round_completed"])
        self.assertEqual(calls, [job_id])
        self.assertEqual(resumed["work_turn"]["counters"]["model_executions"], 0)
        self.assertEqual(len(runner.calls), 1)

    def test_cli_opt_in_counts_completed_rounds_and_keeps_manual_programme_pinned(self):
        cli = self.cli()
        calls = []
        steps = self.fixture.noops()
        def progress(memory, record, _context):
            calls.append(record["programme_id"])
            old = memory.snapshot()["concepts"]["first"]
            updated = deepcopy(old); updated["description"]["reason"] = "Synthetic revision " + str(len(calls))
            memory.put("concepts", "first", updated, previous=old)
            return {"state": "updated"}
        steps["profiles"] = progress
        output = StringIO()
        with patch("sys.stdout", new=output):
            result = cli.main(["--programme-id", "whole-programme", "--execute", "--cooperative-turns", "--max-cycles", "2"],
                memory=self.memory, root=self.root, handlers={}, steps=steps,
                discovery_step=lambda *_args: {"state": "checked"}, private_staging=lambda *_args: {}, backup_step=lambda *_args: {})
        self.assertEqual(result, 0)
        self.assertEqual(calls, ["whole-programme", "whole-programme"])
        self.assertEqual(len(self.turns()), 2)
        self.assertIn('"completed_rounds_this_invocation": 2', output.getvalue())

    def test_cli_opt_in_finishes_at_saved_wait_without_empty_loop(self):
        cli = self.cli(); output = StringIO()
        with patch("sys.stdout", new=output):
            result = cli.main(["--programme-id", "whole-programme", "--execute", "--cooperative-turns"],
                memory=self.memory, root=self.root, handlers={}, steps=self.fixture.noops(),
                discovery_step=lambda *_args: {"state": "checked"}, private_staging=lambda *_args: {}, backup_step=lambda *_args: {})
        self.assertEqual(result, 0)
        self.assertEqual(len(self.turns()), 1)
        self.assertIn('"state": "all_waiting"', output.getvalue())
        self.assertIn('"completed_rounds_this_invocation": 1', output.getvalue())

    def test_existing_cli_factories_enable_shared_waves_and_supplier_inner_wait_only_on_opt_in(self):
        cli = self.cli()
        for cooperative in (False, True):
            with self.subTest(cooperative=cooperative), patch("sys.stdout", new=StringIO()), \
                    patch.object(cli, "installed_source_handlers", return_value={}) as installed, \
                    patch("product_radar.catalogue_discovery.create_catalogue_discovery_step",
                          return_value=lambda *_args: {"state": "checked"}) as discovery:
                arguments = ["--programme-id", "whole-programme", "--execute", "--max-cycles", "1"]
                if cooperative:
                    arguments.append("--cooperative-turns")
                self.assertEqual(cli.main(arguments, memory=self.memory, root=self.root,
                    steps=self.fixture.noops(), private_staging=lambda *_args: {}, backup_step=lambda *_args: {}), 0)
                # Paid quick-commerce checks keep their credit records with this project (W5).
                expected = {"quickcommerce_client": None, "quickcommerce_options": {"spend_root": Path(self.root)}}
                if cooperative:
                    expected.update(marketplace_options={"shared_acquisitions": True},
                                    meta_options={"shared_acquisitions": True},
                                    trends_options={"shared_acquisitions": True},
                                    supplier_options={"respect_access_waits": True, "shared_acquisitions": True})
                self.assertEqual(installed.call_args.kwargs, expected)
                kwargs = discovery.call_args.kwargs
                self.assertEqual(kwargs.get("renewable_waves", False), cooperative)
                self.assertEqual(kwargs.get("shared_marketplace", False), cooperative)


    def test_paid_quickcommerce_client_is_installed_only_for_cooperative_turns(self):
        from product_radar.quickcommerce import QuickCommerceClient
        cli = self.cli()
        for cooperative in (False, True):
            with self.subTest(cooperative=cooperative), patch("sys.stdout", new=StringIO()) as output, \
                    patch.object(cli, "installed_source_handlers", return_value={}) as installed, \
                    patch("product_radar.quickcommerce.load_key", return_value="fictional_test_key_1234567890") as key, \
                    patch("product_radar.catalogue_discovery.create_catalogue_discovery_step",
                          return_value=lambda *_args: {"state": "checked"}):
                arguments = ["--programme-id", "whole-programme", "--execute", "--max-cycles", "1",
                             "--funded-quickcommerce-fallback"] + (["--cooperative-turns"] if cooperative else [])
                self.assertEqual(cli.main(arguments, memory=self.memory, root=self.root,
                    steps=self.fixture.noops(), private_staging=lambda *_args: {}, backup_step=lambda *_args: {}), 0)
                client = installed.call_args.kwargs["quickcommerce_client"]
                if cooperative:
                    self.assertIsInstance(client, QuickCommerceClient)
                else:
                    # Outside the cooperative turns no paid route can be reached at all.
                    self.assertIsNone(client); key.assert_not_called()
                    self.assertIn("QUICKCOMMERCE_COOPERATIVE_TURNS_REQUIRED", output.getvalue())


class FunctionalProgrammeReaderTests(unittest.TestCase):
    def setUp(self):
        import test_functional_fit as functional_fixtures
        self.fixture = functional_fixtures.FunctionalFitTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.memory, self.cid = self.fixture.memory, self.fixture.cid
        self.now = fixtures.utc_now()
        self.record = programme.prepare_programme(self.memory, "functional-reader",
            concept_ids=[self.cid], as_of=self.now)

    def test_progress_requires_functional_fit_after_accepted_meaning(self):
        self.fixture.accept()
        pending = programme.programme_progress(self.memory, self.record["programme_id"],
            handlers={}, as_of=self.now)
        self.assertEqual(pending["fit_states"], {"needs_review": 1})
        self.fixture.complete()
        complete = programme.programme_progress(self.memory, self.record["programme_id"],
            handlers={}, as_of=self.now)
        self.assertEqual(complete["fit_states"], {"accepted": 1})

    def test_reviewed_physical_fact_wakes_before_functional_fit_base_exists(self):
        from product_radar.fit_description import FUNCTIONAL_CONTRACT
        from product_radar.product_queries import query_profile_scope_hash
        self.fixture.accept()
        before = self.memory.snapshot()
        self.assertFalse(any(row.get("binding", {}).get("contract_version") == FUNCTIONAL_CONTRACT
                             for row in before["source_checks"].values()))
        signature = programme._cooperative_signature(before, self.record)
        query_meaning = query_profile_scope_hash(before, self.cid)
        self.fixture.physical_fact(field="weight", value="Estimated 250 grams")
        after = self.memory.snapshot()
        self.assertEqual(query_profile_scope_hash(after, self.cid), query_meaning)
        self.assertNotEqual(programme._cooperative_signature(after, self.record), signature)
        self.assertEqual(programme._cooperative_signature(after, self.record),
                         programme._cooperative_signature(self.memory.snapshot(), self.record))


if __name__ == "__main__":
    unittest.main()
