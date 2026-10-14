"""Synthetic continuation and native task definitions; no live activation."""

from datetime import timedelta
from io import StringIO
import importlib.util
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch
import xml.etree.ElementTree as ET

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from product_radar.models import SafetyError, digest, timestamp, utc_now
from product_radar.programme import (STAGES, VERSION, _current_profiles, _report_revision, _reporting_ids, _signature,
    _ProgrammeRunner, advance_programme, due_programme, load_programme, prepare_programme,
    programme_progress, programme_service_state, release_slots, research_week, set_programme_service_state)
from product_radar.programme_schedule import NAMESPACE, compare_native_task, native_task_xml
from product_radar.research_memory import ResearchMemory, key
from product_radar.source_coverage import ROUTES
from product_radar.storage import Journal, atomic_json, load_json


class ProgrammeTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.memory = ResearchMemory(self.root / "research", Journal(self.root / "money"), evidence_type="synthetic")
        self.now = utc_now()
        self.product("first"); self.product("second")

    def product(self, cid, *, brand="generic_or_small"):
        self.memory.put("concepts", cid, {"status": "active", "description": {
            "label": "Sink caddy", "category": "Home and Kitchen", "function": "Store sink essentials",
            "mechanism": "manual", "features": ["draining"], "audience": "adult",
            "brand": brand, "basis": "observed", "reason": "Synthetic product."}},
            previous=self.memory.snapshot()["concepts"].get(cid))

    def prepare(self, **options):
        return prepare_programme(self.memory, "whole-programme", source_run_id="existing-source-run", as_of=self.now, **options)

    def noops(self):
        return {stage: (lambda _memory, _record, _context: {"state": "nothing_new"}) for stage in STAGES}

    def advance(self, **options):
        return advance_programme(self.memory, "whole-programme", root=self.root, handlers={},
            steps=options.pop("steps", self.noops()), as_of=self.now, **options)

    def test_monday_is_india_local_not_utc_or_a_future_week(self):
        self.assertEqual(research_week("2026-10-04T18:29:59+00:00"), "2026-09-28")
        self.assertEqual(research_week("2026-10-04T18:30:00+00:00"), "2026-10-05")
        self.assertEqual(research_week("2026-10-11T12:00:00+00:00"), "2026-10-05")

    def test_release_dates_and_time_are_separate_from_research(self):
        slots = release_slots("2026-10-05")
        self.assertEqual([row["date"] for row in slots], ["2026-10-06", "2026-10-08", "2026-10-10"])
        self.assertTrue(all(row["scheduled_at"].endswith("03:30:00+00:00") for row in slots))
        self.assertTrue(all(row["client_id"] == "default" for row in slots))

    def test_non_monday_or_unreviewed_time_is_not_silently_replaced(self):
        with self.assertRaisesRegex(SafetyError, "WEEK_INVALID"):
            release_slots("2026-10-06")
        with self.assertRaisesRegex(SafetyError, "POLICY_CHANGED"):
            release_slots("2026-10-05", policy={"timezone": "Asia/Kolkata", "publication_time": "10:00"})

    def test_release_day_changes_are_not_silently_scheduled(self):
        for weekdays in ([0, 2, 4], [1, 3, 5, 6], [1, 1, 5], None):
            with self.subTest(weekdays=weekdays), self.assertRaisesRegex(SafetyError, "POLICY_CHANGED"):
                release_slots("2026-10-05", policy={"timezone": "Asia/Kolkata", "publication_time": "09:00",
                                                     "publication_weekdays": weekdays})

    def test_prepare_freezes_scope_without_sources_models_or_publication(self):
        before = self.memory.snapshot()
        result = self.prepare()
        self.assertEqual(result["concept_ids"], ["first", "second"])
        self.assertEqual(result["source_run_id"], "existing-source-run")
        self.assertEqual(result["kind"], VERSION)
        after = self.memory.snapshot()
        for table in ("jobs", "query_tasks", "captures", "published_history", "issue_reservations"):
            self.assertEqual(after[table], before[table])

    def test_prepare_156_pool_retains_brands_and_does_not_invent_a_percentage(self):
        for number in range(154):
            self.product("extra-" + str(number), brand="established" if number < 3 else "generic_or_small")
        record = self.prepare()
        progress = programme_progress(self.memory, record["programme_id"], as_of=self.now)
        self.assertEqual(progress["products"], 156)
        self.assertEqual(progress["fit_products"], 153)
        self.assertEqual(progress["excluded_fit_products"], 3)
        self.assertEqual(progress["product_route_questions"], 156 * len(ROUTES))
        self.assertFalse(any("percent" in field for field in progress))

    def test_programme_progress_reuses_one_supplied_verified_snapshot_for_coverage(self):
        record = self.prepare(); state = self.memory.snapshot()
        with patch.object(self.memory, "snapshot", side_effect=AssertionError("Progress reread the whole mounted store")):
            progress = programme_progress(self.memory, record["programme_id"], record=record, state=state,
                                          handlers={}, as_of=self.now)
        self.assertEqual(progress["products"], 2)
        self.assertEqual(progress["product_route_questions"], 2 * len(ROUTES))

    def test_exact_prepare_replay_does_not_append_an_event(self):
        record = self.prepare(); before = self.memory.snapshot()
        self.assertEqual(self.prepare(), record)
        self.assertEqual(self.memory.snapshot(), before)

    def test_new_discovery_does_not_expand_old_pool_by_accident(self):
        record = self.prepare(); self.product("later")
        self.assertEqual(self.prepare()["concept_ids"], record["concept_ids"])
        with self.assertRaisesRegex(SafetyError, "BINDING_CHANGED"):
            self.prepare(concept_ids=["first", "second", "later"])

    def test_source_identity_and_location_are_frozen(self):
        self.prepare(location={"name": "Mumbai", "lat": "19.0760", "lon": "72.8777"})
        with self.assertRaisesRegex(SafetyError, "BINDING_CHANGED"):
            self.prepare()
        with self.assertRaisesRegex(SafetyError, "BINDING_CHANGED"):
            prepare_programme(self.memory, "whole-programme", source_run_id="different", as_of=self.now)

    def test_preparation_uses_the_same_single_programme_guard(self):
        with Journal(self.memory.root / "programme_writer").locked():
            with self.assertRaisesRegex(SafetyError, "RUNNER_ALREADY_ACTIVE"):
                self.prepare()

    def test_one_live_writer_guard_prevents_two_programmes(self):
        self.prepare()
        with Journal(self.memory.root / "programme_writer").locked():
            with self.assertRaisesRegex(SafetyError, "RUNNER_ALREADY_ACTIVE"):
                self.advance()

    def test_a_busy_initial_canonical_save_keeps_a_recoverable_checkpoint(self):
        with patch.object(self.memory, "put", side_effect=SafetyError("RUNNER_ALREADY_ACTIVE")):
            record = self.prepare()
        self.assertNotIn(key("programme", "whole-programme"), self.memory.snapshot()["batch_runs"])
        self.assertEqual(load_programme(self.memory, "whole-programme"), record)
        self.advance()
        self.assertIn(key("programme", "whole-programme"), self.memory.snapshot()["batch_runs"])

    def test_multiple_busy_checkpoints_keep_the_latest_original_programme(self):
        self.prepare()
        original_put = self.memory.put
        def busy(table, record_id, value, **kwargs):
            if value.get("kind") == VERSION:
                raise SafetyError("RUNNER_ALREADY_ACTIVE")
            return original_put(table, record_id, value, **kwargs)
        with patch.object(self.memory, "put", side_effect=busy):
            result = self.advance()
        self.assertEqual(result["control_save"]["saved"], "local_checkpoint")
        recovered = load_programme(self.memory, "whole-programme")
        self.assertEqual(set(recovered["last_results"]), set(STAGES))
        self.assertEqual(recovered["cycles"], 1)
        self.advance()
        self.assertEqual(self.memory.snapshot()["batch_runs"][key("programme", "whole-programme")]["cycles"], 2)

    def test_changed_control_bytes_are_not_accepted(self):
        self.prepare()
        path = next((self.memory.root / "programme_runs").glob("*/pending-control.json"))
        staged = load_json(path); staged["record"]["source_run_id"] = "invented"
        atomic_json(path, staged)
        with self.assertRaisesRegex(SafetyError, "CONTROL_INVALID"):
            load_programme(self.memory, "whole-programme")

    def test_incomplete_control_record_keeps_a_precise_error_instead_of_key_error(self):
        self.prepare()
        path = next((self.memory.root / "programme_runs").glob("*/pending-control.json"))
        staged = load_json(path); del staged["record"]["source_run_id"]
        staged["record_hash"] = digest(staged["record"]); atomic_json(path, staged)
        with self.assertRaisesRegex(SafetyError, "CONTROL_INVALID"):
            load_programme(self.memory, "whole-programme")

    def test_conflicting_canonical_control_is_not_overwritten(self):
        record = self.prepare()
        record_id = key("programme", "whole-programme")
        changed = dict(record, cycles=900)
        self.memory.put("batch_runs", record_id, changed, previous=record)
        with self.assertRaisesRegex(SafetyError, "CONTROL_CONFLICT"):
            load_programme(self.memory, "whole-programme")

    def test_all_lanes_run_in_order_and_no_work_means_a_fixed_point(self):
        self.prepare(); called = []
        steps = {stage: (lambda _m, _r, _c, name=stage: called.append(name) or {"state": "nothing_new"}) for stage in STAGES}
        result = self.advance(steps=steps)
        self.assertEqual(called, list(STAGES))
        self.assertEqual(result["cycles_this_tick"], 1)
        self.assertEqual(result["tick_state"], "fixed_point_with_saved_remaining_work")
        self.assertEqual(result["progress"]["discovery_state"], "not_connected")
        self.assertFalse(result["client_publication_attempted"])

    def test_diagnostic_writes_are_not_useful_progress_or_an_infinite_loop(self):
        self.prepare(); calls = []
        def noisy(memory, _record, context):
            calls.append(1)
            memory.put("source_checks", "diagnostic-" + str(len(calls)), {"kind": "programme_diagnostic", "at": context["at"]})
            return {"state": "waiting"}
        steps = self.noops(); steps["profiles"] = noisy
        self.assertEqual(self.advance(steps=steps)["cycles_this_tick"], 1)
        self.assertEqual(len(calls), 1)

    def test_seed_branch_progress_counts_but_clock_only_changes_do_not(self):
        from copy import deepcopy
        from product_radar.catalogue_discovery import SEED_CYCLE_VERSION
        record = self.prepare(); state = self.memory.snapshot()
        before = _signature(state, record)
        state = deepcopy(state)
        cycle = {"kind": SEED_CYCLE_VERSION, "programme_id": record["programme_id"],
                 "planned_branches": [{"branch_id": "first-branch"}, {"branch_id": "healthy-branch"}],
                 "branches": {}}
        state["batch_runs"]["seed-cycle"] = cycle
        prepared = _signature(state, record)
        self.assertNotEqual(prepared, before)
        cycle["branches"]["first-branch"] = {"state": "input_failed", "addition_ids": [],
            "eligible_material_ids": [], "pending_material_ids": ["original-material"],
            "considered_at": self.now, "next_retry_at": self.now}
        first_visited = _signature(state, record)
        self.assertNotEqual(first_visited, prepared)
        cycle["branches"]["first-branch"].update(considered_at="2026-10-08T20:00:00+00:00",
                                                 next_retry_at="2026-10-08T20:05:00+00:00")
        self.assertEqual(_signature(state, record), first_visited)
        state["batch_runs"]["unrelated-seed"] = dict(cycle, programme_id="different-programme")
        self.assertEqual(_signature(state, record), first_visited)
        state["catalogue_additions"]["local-merge"] = {"material_id": "original-material", "outcome": "merged"}
        self.assertNotEqual(_signature(state, record), first_visited)

    def test_filled_reports_do_not_stop_other_pending_source_acquisitions(self):
        self.prepare(); calls = []
        def handler(context):
            context["record_dispatch"](); calls.append(context["route"])
            return {"schema_version": 1, "route": context["route"], "acquisition_id": context["acquisition_id"],
                "observed_at": context["as_of"], "state": "empty", "dispatch_started": True,
                "reference": context["acquisition_id"] + "/receipt.json", "capture_ids": [],
                "observation_ids": [], "target_results": [], "reason": "Synthetic readable empty result.", "retryable": False}
        def reports(_memory, record, _context):
            record["drafts"]["already-full"] = {"state": "draft_ready_for_review", "complete": True, "approved": False}
            return {"state": "draft_ready_for_review", "complete": True}
        steps = self.noops(); steps.pop("acquire"); steps["reports"] = reports
        result = advance_programme(self.memory, "whole-programme", root=self.root, handlers={route: handler for route in ROUTES},
            request_builder=lambda context: {"query": "sink caddy", "country": "IN"}, steps=steps,
            source_limit=3, max_cycles=20)
        # Every planned route is asked once; retired Google Search results never are.
        self.assertEqual(len(calls), len(ROUTES) - 1)
        self.assertEqual(set(calls), set(ROUTES) - {"google-suppliers"})
        self.assertGreater(result["cycles_this_tick"], 1)
        self.assertTrue(result["report_filled_does_not_stop_work"])
        self.assertEqual(self.memory.snapshot()["published_history"], {})
        self.assertTrue(all(row["run_id"] == "existing-source-run" for row in self.memory.snapshot()["query_tasks"].values()))

    def test_explicit_slice_preserves_pending_work_and_resumes_same_programme(self):
        self.prepare(); calls = []
        def grow(memory, _record, context):
            name = "observed-" + str(len(calls)); calls.append(name)
            memory.put("observations", name, {"fixture": True, "semantic_id": name, "source": "meta",
                "source_item": name, "acquisition_id": "synthetic-slice", "observed_at": context["at"],
                "semantic": {"title": "Unmatched synthetic slice observation"}, "facts": {"limitations": []}})
            return {"state": "advanced"}
        steps = self.noops(); steps["acquire"] = grow
        result = self.advance(steps=steps, max_cycles=2)
        self.assertEqual(len(calls), 2)
        self.assertEqual(result["tick_state"], "work_slice_finished_remaining_work_kept")
        self.advance(steps=steps, max_cycles=1)
        self.assertEqual(len(calls), 3)
        self.assertEqual(load_programme(self.memory, "whole-programme")["cycles"], 3)

    def test_local_or_route_errors_do_not_stop_healthy_lanes(self):
        for failure, expected_reason in (
                (SafetyError("RUNNER_ALREADY_ACTIVE"), "RUNNER_ALREADY_ACTIVE"),
                (OSError("private fixture detail"), "PROGRAMME_LOCAL_FILE_FAILED"),
                (TimeoutError("private fixture detail"), "PROGRAMME_LOCAL_FILE_FAILED"),
                (ValueError("private fixture detail"), "PROGRAMME_STAGE_FAILED"),
                (KeyError("private fixture detail"), "PROGRAMME_STAGE_FAILED"),
                (TypeError("private fixture detail"), "PROGRAMME_STAGE_FAILED")):
            with self.subTest(error=type(failure).__name__):
                record = self.prepare(); called = []
                def broken(_m, _r, _c):
                    raise failure
                steps = {stage: (lambda _m, _r, _c, name=stage: called.append(name) or {"state": "nothing_new"}) for stage in STAGES}
                steps["profiles"] = broken
                self.advance(steps=steps)
                self.assertIn("reports", called)
                saved = load_programme(self.memory, record["programme_id"])
                self.assertIn("lane:profiles", saved["deferrals"])
                self.assertEqual(saved["deferrals"]["lane:profiles"]["reason"], expected_reason)
                self.assertEqual(saved["last_results"]["profiles"], {
                    "state": "waiting", "reason": expected_reason, "error_type": type(failure).__name__})
                self.assertEqual(called, list(STAGES[1:]))
                self.assertNotIn("private fixture detail", str(saved["last_results"]))
                # These are independent synthetic failures, not
                # repeated unchanged recovery of a real production target.
                saved["deferrals"].clear()
                from product_radar.programme import _save
                _save(self.memory, saved)

    def test_unchanged_failed_lane_is_not_retried_each_cycle(self):
        self.prepare(); calls = []
        def fail(_m, _r, _c):
            calls.append(1); raise SafetyError("AI_RATE_LIMITED")
        steps = self.noops(); steps["profiles"] = fail
        self.advance(steps=steps); self.advance(steps=steps)
        self.assertEqual(len(calls), 1)
        later = (timestamp(self.now) + timedelta(seconds=301)).isoformat()
        advance_programme(self.memory, "whole-programme", root=self.root, handlers={}, steps=steps, as_of=later)
        self.assertEqual(len(calls), 2)

    def test_model_pause_allows_saved_only_reviews_and_public_sources(self):
        self.prepare(); model_calls = []; stages = []
        class Runner:
            identity = "synthetic-approved-model"
            def __call__(self, role, payload, folder):
                model_calls.append(role); raise SafetyError("AI_SUBSCRIPTION_LIMIT")
        def profiles(_memory, _record, context):
            context["runner"]("C", {}, self.root)
        def later(_memory, _record, context):
            stages.append(context["runner"])
            return {"state": "saved_only_or_public"}
        steps = {stage: later for stage in STAGES}; steps["profiles"] = profiles
        result = self.advance(steps=steps, runner=Runner())
        self.assertEqual(model_calls, ["C"])
        self.assertEqual(stages, [None] * (len(STAGES) - 1))
        self.assertEqual(result["progress"]["model_pause"]["reason"], "AI_SUBSCRIPTION_LIMIT")

    def test_corrupt_authority_does_not_launch_dependent_work(self):
        self.prepare(); calls = []
        steps = self.noops()
        def invalid(_m, _r, _c):
            raise SafetyError("RESEARCH_EVENTS_INVALID")
        steps["profiles"] = invalid
        steps["acquire"] = lambda _m, _r, _c: calls.append(1) or {}
        with self.assertRaisesRegex(SafetyError, "RESEARCH_EVENTS_INVALID"):
            self.advance(steps=steps)
        self.assertEqual(calls, [])

    def test_bad_observer_cannot_abort_research(self):
        self.prepare()
        def observer(_row):
            raise ValueError("broken console")
        self.assertEqual(self.advance(on_progress=observer)["cycles_this_tick"], 1)

    def test_stage_start_is_observed_before_work_and_finish_after_saved_result(self):
        self.prepare(); events = []
        self.advance(on_progress=events.append)
        self.assertEqual([(row["stage"], row["phase_state"]) for row in events],
                         [(stage, phase) for stage in STAGES for phase in ("started", "finished")])
        self.assertTrue(all(row["frozen_products"] == 2 for row in events if row["phase_state"] == "started"))

    def test_model_observer_has_only_launch_metadata_not_public_inputs_or_answers(self):
        record = self.prepare(); events = []; before = self.memory.snapshot()
        context = {"memory": self.memory, "at": self.now, "retry_seconds": 300,
                   "stage": "profiles", "on_progress": events.append}
        runner = _ProgrammeRunner(lambda *_args: {"private_answer": "not console content"}, record, context)
        self.assertIn("private_answer", runner("C", {"items": [{"title": "raw public product text"}]}, self.root))
        self.assertEqual([row["phase_state"] for row in events], ["runner_call_started", "runner_call_returned"])
        self.assertEqual(events[0]["supplied_items"], 1)
        self.assertNotIn("raw public product text", str(events)); self.assertNotIn("private_answer", str(events))
        self.assertEqual(self.memory.snapshot(), before)

    def test_discovery_failure_is_separate_from_current_pool_work(self):
        self.prepare(); called = []
        def discovery(_m, _r, _c):
            raise SafetyError("DISCOVERY_SOURCE_PAUSED")
        steps = self.noops(); steps["acquire"] = lambda _m, _r, _c: called.append(1) or {}
        result = self.advance(steps=steps, discovery_step=discovery)
        self.assertEqual(called, [1])
        self.assertEqual(result["progress"]["discovery_state"], "waiting")

    def test_new_discovery_gets_every_review_lane_in_its_own_frozen_scope(self):
        original = self.prepare(); self.product("later"); calls = []
        def discovery(_m, _r, _c):
            return {"state": "checked", "new_products_need_full_source_review": ["later"]}
        steps = {stage: (lambda _m, row, _c, lane=stage:
                 calls.append((lane, row["programme_id"], list(row["concept_ids"]))) or {}) for stage in STAGES}
        result = self.advance(steps=steps, discovery_step=discovery, max_cycles=1)
        record = load_programme(self.memory, "whole-programme")
        child = next(iter(record["follow_on_reviews"].values()))
        self.assertEqual(record["concept_ids"], original["concept_ids"])
        self.assertEqual(record["source_run_id"], original["source_run_id"])
        self.assertEqual(child["concept_ids"], ["later"])
        self.assertNotEqual(child["source_run_id"], original["source_run_id"])
        self.assertEqual([lane for lane, scope, _ in calls if scope == child["programme_id"]], list(STAGES[:-2]))
        self.assertEqual([scope for lane, scope, _ in calls if lane == "reports"], [record["programme_id"]])
        self.assertEqual([scope for lane, scope, _ in calls if lane == "private_staging"], [record["programme_id"]])
        self.assertEqual(_reporting_ids(record), ["first", "later", "second"])
        self.assertEqual(result["progress"]["products"], 2)
        self.assertEqual(result["progress"]["follow_on_products"], 1)
        self.assertEqual(result["progress"]["reporting_products"], 3)

    def test_repeated_discovery_does_not_duplicate_review_scopes_or_products(self):
        self.prepare(); self.product("later")
        def discovery(_m, _r, _c):
            return {"state": "checked", "new_concept_ids": ["later", "later", "first"]}
        self.advance(discovery_step=discovery, max_cycles=1)
        first = load_programme(self.memory, "whole-programme")
        self.advance(discovery_step=discovery, max_cycles=1)
        second = load_programme(self.memory, "whole-programme")
        self.assertEqual(set(second["follow_on_reviews"]), set(first["follow_on_reviews"]))
        self.assertEqual(second["last_results"]["discovery"]["queued_follow_on_products"], 0)
        self.assertEqual(self.memory.snapshot()["query_tasks"], {})

    def test_later_admission_appends_a_separate_scope_without_changing_prior_members(self):
        self.prepare(); self.product("later")
        selected = ["later"]
        def discovery(_m, _r, _c):
            return {"state": "checked", "new_concept_ids": list(selected)}
        self.advance(discovery_step=discovery, max_cycles=1)
        previous = next(iter(load_programme(self.memory, "whole-programme")["follow_on_reviews"].values()))
        self.product("newest"); selected.append("newest")
        self.advance(discovery_step=discovery, max_cycles=1)
        record = load_programme(self.memory, "whole-programme")
        self.assertEqual(len(record["follow_on_reviews"]), 2)
        self.assertEqual(record["follow_on_reviews"][previous["programme_id"]]["concept_ids"], ["later"])
        self.assertEqual(record["follow_on_reviews"][previous["programme_id"]]["source_run_id"], previous["source_run_id"])

    def test_follow_on_failure_never_stops_the_original_or_shared_report_lanes(self):
        self.prepare(); self.product("later"); calls = []
        def discovery(_m, _r, _c):
            return {"state": "checked", "new_concept_ids": ["later"]}
        def fit(_m, row, _c):
            calls.append(("fit", row["concept_ids"]))
            if row.get("parent_programme_id"):
                raise SafetyError("AI_MODEL_UNAVAILABLE")
            return {}
        steps = self.noops(); steps["fit"] = fit
        steps["acquire"] = lambda _m, row, _c: calls.append(("acquire", row["concept_ids"])) or {}
        steps["reports"] = lambda _m, row, _c: calls.append(("reports", _reporting_ids(row))) or {}
        self.advance(steps=steps, discovery_step=discovery, max_cycles=1)
        self.assertIn(("acquire", ["first", "second"]), calls)
        self.assertIn(("acquire", ["later"]), calls)
        self.assertIn(("reports", ["first", "later", "second"]), calls)
        child = next(iter(load_programme(self.memory, "whole-programme")["follow_on_reviews"].values()))
        self.assertEqual(child["last_results"]["fit"]["reason"], "AI_MODEL_UNAVAILABLE")

    def test_nonexistent_discovery_identity_is_not_faked_into_follow_on_coverage(self):
        self.prepare(); called = []
        steps = self.noops(); steps["acquire"] = lambda *_args: called.append(1) or {}
        self.advance(steps=steps, discovery_step=lambda *_args: {"state": "checked", "new_concept_ids": ["not-saved"]})
        record = load_programme(self.memory, "whole-programme")
        self.assertEqual(record["follow_on_reviews"], {})
        self.assertEqual(record["discovery_state"], "waiting")
        self.assertEqual(called, [1])

    def test_changed_follow_on_source_binding_is_not_accepted_from_a_checkpoint(self):
        self.prepare(); self.product("later")
        self.advance(discovery_step=lambda *_args: {"state": "checked", "new_concept_ids": ["later"]}, max_cycles=1)
        path = next((self.memory.root / "programme_runs").glob("*/pending-control.json"))
        staged = load_json(path)
        staged["previous_hash"] = digest(staged["record"])
        child = next(iter(staged["record"]["follow_on_reviews"].values()))
        child["source_run_id"] = "invented-new-source"
        staged["record_hash"] = digest(staged["record"]); atomic_json(path, staged)
        with self.assertRaisesRegex(SafetyError, "BINDING_CHANGED"):
            load_programme(self.memory, "whole-programme")

    def test_independent_new_input_reopens_a_deferred_lane(self):
        self.prepare(); called = []
        steps = self.noops()
        def blocked(_m, _r, _c):
            called.append(1); raise SafetyError("SOURCE_PENDING")
        steps["acquire"] = blocked
        self.advance(steps=steps)
        self.product("first", brand="unclear")
        self.advance(steps=steps)
        self.assertEqual(len(called), 2)

    def test_profile_stage_is_real_and_reuses_current_short_wording(self):
        self.prepare(); steps = self.noops(); steps.pop("profiles")
        first = self.advance(steps=steps)
        state = self.memory.snapshot()
        self.assertEqual(_current_profiles(state), {"first", "second"})
        self.assertEqual(first["progress"]["current_query_profiles"], 2)
        before = len([row for row in state["source_checks"].values() if row.get("kind") == "product_query_profile"])
        self.advance(steps=steps)
        after = len([row for row in self.memory.snapshot()["source_checks"].values() if row.get("kind") == "product_query_profile"])
        self.assertEqual(before, after)

    def test_fit_stage_completes_actual_saved_synthetic_products_and_replays_without_model(self):
        from test_fit_description import DescriptionRunner
        from test_research_flow import FakeRunner, capture
        from product_radar.research_flow import run_flow
        self.memory = ResearchMemory(self.root / "fit-research", Journal(self.root / "fit-money"), evidence_type="synthetic")
        capture(self.memory); run_flow(self.memory, FakeRunner())
        self.prepare(); steps = self.noops(); steps.pop("fit"); runner = DescriptionRunner()
        result = self.advance(steps=steps, runner=runner)
        self.assertGreater(result["progress"]["fit_states"].get("accepted", 0), 0)
        calls = len(runner.calls)
        self.advance(steps=steps, runner=runner)
        self.assertEqual(len(runner.calls), calls)
        self.assertTrue(all(row.get("contract_version") == "research-e-fit10-description-v3"
            for row in self.memory.snapshot()["assessments"].values()))

    def test_fit_stage_keeps_unchanged_accepted_v2_without_model_calls(self):
        from copy import deepcopy
        from test_fit_review import FitRunner
        from test_fit_description import DescriptionRunner
        from test_research_flow import FakeRunner, capture
        from product_radar.fit_review import prepare_practical_fit_reviews, run_fit_reviews
        from product_radar.research_flow import run_flow
        self.memory = ResearchMemory(self.root / "v2-research", Journal(self.root / "v2-money"), evidence_type="synthetic")
        capture(self.memory); run_flow(self.memory, FakeRunner())
        bases = prepare_practical_fit_reviews(self.memory)
        run_fit_reviews(self.memory, FitRunner(), [row["review_id"] for row in bases if row["status"] == "prepared"])
        original = deepcopy(self.memory.snapshot()["assessments"])
        self.prepare(); steps = self.noops(); steps.pop("fit"); runner = DescriptionRunner()
        result = self.advance(steps=steps, runner=runner)
        self.assertEqual(result["progress"]["fit_states"], {"accepted": len(original)})
        self.assertEqual(runner.calls, [])
        self.assertEqual(self.memory.snapshot()["assessments"], original)

    def test_supplier_step_passes_only_saved_interpretation_and_preserves_failure_targets(self):
        self.prepare(); steps = self.noops(); steps.pop("suppliers")
        result = {"selected_capture_ids": ["supplier-capture-first"], "selected_inputs": [
            {"capture_id": "supplier-capture-first", "item_id": "first-input"}], "accepted": {},
            "failures": {"first-input": "AI_JOB_PENDING_RECONCILIATION"}, "reused_capture_ids": [],
            "source_bindings": [], "skipped_captures": [], "remaining_inputs": 1}
        with patch("product_radar.source_interpretation.review_supplier_results", return_value=result) as reviewed:
            self.advance(steps=steps)
            self.advance(steps=steps)
        self.assertEqual(reviewed.call_args.kwargs["skip_capture_ids"], ["supplier-capture-first"])
        saved = load_programme(self.memory, "whole-programme")
        self.assertEqual(saved["deferrals"]["suppliers:supplier-capture-first"]["reason"], "AI_JOB_PENDING_RECONCILIATION")

    def test_model_configuration_failure_does_not_remove_public_work(self):
        self.prepare(); called = []
        steps = self.noops(); steps["acquire"] = lambda _m, _r, _c: called.append(1) or {}
        result = self.advance(steps=steps, setup_failures={"model": "AI_CONFIGURATION_CHANGED"})
        self.assertEqual(called, [1])
        self.assertEqual(result["progress"]["model_pause"]["reason"], "AI_CONFIGURATION_CHANGED")
        self.assertEqual(result["progress"]["setup_failures"], {"model": "AI_CONFIGURATION_CHANGED"})

    def test_whole_programme_has_its_own_namespace_not_paid_batch_actions(self):
        record = self.prepare(); self.advance()
        self.assertEqual(record["actions"], {})
        self.assertEqual(self.memory.snapshot()["batch_plans"], {})
        self.assertFalse(self.memory.journal.path.exists())

    def test_due_tick_reuses_unfinished_old_work_instead_of_new_charged_week(self):
        record = self.prepare()
        next_week = "2026-10-19T04:00:00+00:00"
        resumed = due_programme(self.memory, as_of=next_week)
        self.assertEqual(resumed["programme_id"], record["programme_id"])
        self.assertEqual(resumed["source_run_id"], "existing-source-run")
        self.assertEqual(len([row for row in self.memory.snapshot()["batch_runs"].values() if row.get("kind") == VERSION]), 1)

    def test_old_permanently_blocked_fixed_point_admits_only_latest_fresh_week_without_cloning_pool(self):
        old = self.prepare(location={"name": "Mumbai", "lat": "19.0760", "lon": "72.8777"})
        self.advance(steps=self.noops())
        before = self.memory.snapshot()
        fresh = due_programme(self.memory, as_of="2026-11-11T00:00:00+00:00")
        self.assertEqual(fresh["week"], "2026-11-09")
        self.assertEqual(fresh["concept_ids"], [])
        self.assertEqual(fresh["location"], old["location"])
        self.assertEqual(load_programme(self.memory, old["programme_id"])["concept_ids"], old["concept_ids"])
        for table in ("captures", "query_tasks", "jobs", "published_history", "issue_reservations"):
            self.assertEqual(self.memory.snapshot()[table], before[table])
        self.assertFalse(self.memory.journal.path.exists())
        self.assertEqual(len([row for row in self.memory.snapshot()["batch_runs"].values() if row.get("kind") == VERSION]), 2)

    def test_due_programmes_rotate_after_old_retry_becomes_ready(self):
        from product_radar.programme import _save
        self.prepare(); self.advance()
        now = "2026-10-19T04:00:00+00:00"
        fresh = due_programme(self.memory, as_of=now)
        old = load_programme(self.memory, "whole-programme")
        old["next_retry_at"] = "2026-10-19T04:10:00+00:00"; _save(self.memory, old)
        self.assertEqual(due_programme(self.memory, as_of=now)["programme_id"], fresh["programme_id"])
        advance_programme(self.memory, fresh["programme_id"], root=self.root, handlers={}, steps=self.noops(), as_of=now)
        chosen = due_programme(self.memory, as_of="2026-10-19T04:15:00+00:00")
        self.assertEqual(chosen["programme_id"], "whole-programme")
        advance_programme(self.memory, chosen["programme_id"], root=self.root, handlers={}, steps=self.noops(),
                          as_of="2026-10-19T04:15:00+00:00")
        self.assertEqual(due_programme(self.memory, as_of="2026-10-19T04:30:00+00:00")["programme_id"], fresh["programme_id"])

    def test_no_fresh_week_is_admitted_while_old_work_is_moving_or_live(self):
        from product_radar.programme import _save
        self.prepare(); self.advance(max_cycles=1)
        old = load_programme(self.memory, "whole-programme")
        for status, tick in (("running", "fixed_point_with_saved_remaining_work"),
                            ("work_remaining", "work_slice_finished_remaining_work_kept")):
            with self.subTest(status=status):
                old["status"] = status; old["tick_state"] = tick; _save(self.memory, old)
                chosen = due_programme(self.memory, as_of="2026-10-19T04:00:00+00:00")
                self.assertEqual(chosen["programme_id"], old["programme_id"])
                self.assertEqual(len([row for row in self.memory.snapshot()["batch_runs"].values() if row.get("kind") == VERSION]), 1)

    def test_fresh_week_has_one_identity_and_empty_scope_stays_empty_before_discovery(self):
        self.prepare(); self.advance()
        now = "2026-10-19T04:00:00+00:00"
        first = due_programme(self.memory, as_of=now)
        before = len(self.memory.snapshot()["batch_runs"])
        self.assertIn(due_programme(self.memory, as_of=now)["programme_id"], {first["programme_id"], "whole-programme"})
        self.assertEqual(len(self.memory.snapshot()["batch_runs"]), before)
        result = advance_programme(self.memory, first["programme_id"], root=self.root, handlers={},
            steps=self.noops(), as_of=now, discovery_step=lambda *_args: {"state": "work_remaining", "new_concept_ids": []})
        self.assertEqual(result["progress"]["products"], 0)
        self.assertEqual(result["progress"]["reporting_products"], 0)
        self.assertEqual(result["progress"]["research_state"], "discovery_work_remaining")
        self.assertNotEqual(load_programme(self.memory, first["programme_id"])["status"], "research_pool_finished")

    def test_global_owner_pause_prevents_fresh_week_creation_as_well_as_launches(self):
        self.prepare(); self.advance()
        set_programme_service_state(self.memory, enabled=False, as_of=self.now)
        before = self.memory.snapshot()
        with self.assertRaisesRegex(SafetyError, "OWNER_PAUSED"):
            due_programme(self.memory, as_of="2026-10-19T04:00:00+00:00")
        self.assertEqual(self.memory.snapshot(), before)

    def test_saved_in_flight_source_intent_is_resumed_before_new_week_creation(self):
        self.prepare(); self.advance()
        self.memory.put("query_tasks", "real-original-source-task", {"kind": "all_source_task_v1",
            "run_id": "existing-source-run", "status": "dispatching", "acquisition_id": "saved-original-acquisition"})
        chosen = due_programme(self.memory, as_of="2026-10-19T04:00:00+00:00")
        self.assertEqual(chosen["programme_id"], "whole-programme")
        self.assertEqual(len([row for row in self.memory.snapshot()["batch_runs"].values() if row.get("kind") == VERSION]), 1)

    def test_missed_research_weeks_create_only_latest_due_slot(self):
        record = due_programme(self.memory, as_of="2026-11-11T00:00:00+00:00")
        self.assertEqual(record["week"], "2026-11-09")
        self.assertEqual(len(self.memory.snapshot()["batch_runs"]), 1)

    def test_finished_manual_current_week_does_not_spawn_a_second_week_identity(self):
        from product_radar.programme import _save
        record = self.prepare(); record["status"] = "research_pool_finished"; _save(self.memory, record)
        self.assertEqual(due_programme(self.memory, as_of=self.now)["programme_id"], "whole-programme")
        self.assertEqual(len(self.memory.snapshot()["batch_runs"]), 1)

    def test_future_report_dates_do_not_launch_or_publish_a_package(self):
        record = self.prepare()
        self.assertEqual(len(record["release_slots"]), 3)
        self.assertEqual(self.memory.snapshot()["published_history"], {})
        self.assertEqual(self.memory.snapshot()["issue_reservations"], {})
        self.assertFalse((self.root / "data/outputs").exists())

    def test_slice_validation_refuses_bool_and_invalid_bounds(self):
        self.prepare()
        for options in ({"max_cycles": True}, {"source_limit": 0}, {"fit_limit": 51}, {"profile_limit": 201}, {"retry_seconds": 0}):
            with self.subTest(options=options), self.assertRaisesRegex(SafetyError, "SLICE_INVALID"):
                self.advance(**options)

    def test_unknown_step_or_non_callable_is_not_accepted(self):
        self.prepare()
        for steps in ({"publication": lambda *args: {}}, {"profiles": True}):
            with self.assertRaisesRegex(SafetyError, "STEPS_INVALID"):
                self.advance(steps=steps)

    def test_unchanged_package_inputs_reuse_original_revision(self):
        issue = {"issue_id": "issue-one", "client_id": "default", "revision": 1,
                 "date": "2026-10-08", "products": [], "evidence_fingerprint": "fixture", "input_hash": "original"}
        issue["selection_hash"] = digest({field: issue[field] for field in
            ("issue_id", "client_id", "revision", "date", "products", "evidence_fingerprint")})
        path = self.root / "data/outputs/issue-one/revision-1/package-input.json"
        atomic_json(path, {"issue": issue, "product_facts": [], "evidence_type": "synthetic"})
        revised = dict(issue, input_hash="allocator-rechecked")
        self.assertEqual(_report_revision(self.root, revised, [], "synthetic"), issue)
        self.assertEqual(load_json(path)["issue"], issue)

    def test_changed_facts_append_revision_without_overwriting_old_package(self):
        issue = {"issue_id": "issue-one", "client_id": "default", "revision": 1,
                 "date": "2026-10-08", "products": [], "evidence_fingerprint": "fixture", "input_hash": "original"}
        path = self.root / "data/outputs/issue-one/revision-1/package-input.json"
        old = {"issue": issue, "product_facts": [], "evidence_type": "synthetic"}; atomic_json(path, old)
        result = _report_revision(self.root, issue, [{"fact": "new"}], "synthetic")
        self.assertEqual(result["revision"], 2)
        self.assertEqual(load_json(path), old)

    def test_no_fictional_success_for_missing_source_or_discovery_adapter(self):
        self.prepare(); result = self.advance()
        self.assertEqual(result["progress"]["source_states"], {"not_connected": 2 * len(ROUTES)})
        self.assertEqual(result["progress"]["research_state"], "work_remaining")
        self.assertEqual(result["progress"]["actually_attempted_product_routes"], 0)

    def test_malformed_product_does_not_hold_up_healthy_profile_and_source_lanes(self):
        old = self.memory.snapshot()["concepts"]["first"]
        self.memory.put("concepts", "first", dict(old, description=None), previous=old)
        self.prepare(); steps = self.noops(); steps.pop("profiles"); steps.pop("fit")
        result = self.advance(steps=steps)
        self.assertEqual(result["progress"]["fit_states"]["product_information_needs_repair"], 1)
        self.assertEqual(result["progress"]["current_query_profiles"], 1)
        saved = load_programme(self.memory, "whole-programme")
        self.assertEqual(saved["deferrals"]["profiles:first"]["reason"], "SOURCE_PRODUCT_DESCRIPTION_INVALID")
        self.assertEqual(result["progress"]["source_states"]["not_attempted"], len(ROUTES))

    def test_owner_pause_prevents_new_stages_without_resetting_the_source_run(self):
        original = self.prepare(); calls = []
        set_programme_service_state(self.memory, enabled=False, as_of=self.now)
        steps = {stage: (lambda _m, _r, _c, name=stage: calls.append(name) or {}) for stage in STAGES}
        result = self.advance(steps=steps)
        self.assertEqual(result["tick_state"], "owner_paused")
        self.assertEqual(calls, [])
        self.assertFalse(result["progress"]["service_enabled"])
        self.assertEqual(load_programme(self.memory, "whole-programme")["source_run_id"], original["source_run_id"])
        set_programme_service_state(self.memory, enabled=True, as_of=self.now)
        self.advance(steps=steps)
        self.assertEqual(calls, list(STAGES))
        self.assertEqual(self.memory.snapshot()["published_history"], {})

    def test_owner_pause_during_a_stage_keeps_that_result_and_stops_the_next_stage(self):
        self.prepare(); calls = []
        def finish_and_pause(_memory, _record, _context):
            calls.append("profiles")
            set_programme_service_state(self.memory, enabled=False, as_of=self.now)
            return {"state": "saved_current_stage"}
        steps = self.noops(); steps["profiles"] = finish_and_pause
        steps["fit"] = lambda _m, _r, _c: calls.append("fit") or {}
        result = self.advance(steps=steps)
        self.assertEqual(result["tick_state"], "owner_paused")
        self.assertEqual(calls, ["profiles"])
        self.assertEqual(load_programme(self.memory, "whole-programme")["last_results"]["profiles"],
                         {"state": "saved_current_stage"})

    def test_owner_pause_is_checked_immediately_before_a_model_launch(self):
        record = self.prepare(); calls = []
        context = {"memory": self.memory, "at": self.now, "retry_seconds": 300}
        runner = _ProgrammeRunner(lambda *_args: calls.append("model"), record, context)
        set_programme_service_state(self.memory, enabled=False, as_of=self.now)
        with self.assertRaisesRegex(SafetyError, "OWNER_PAUSED"):
            runner("E", {}, self.root)
        self.assertEqual(calls, [])
        self.assertIsNone(record["model_pause"])

    def test_changed_owner_control_bytes_are_not_silently_enabled(self):
        set_programme_service_state(self.memory, enabled=False, as_of=self.now)
        path = self.memory.root / "programme_service/control.json"
        wrapper = load_json(path); wrapper["control"]["enabled"] = True
        atomic_json(path, wrapper)
        with self.assertRaisesRegex(SafetyError, "SERVICE_CONTROL_INVALID"):
            programme_service_state(self.memory)

    def test_repeating_the_same_owner_switch_does_not_renew_work_or_funding(self):
        self.prepare(); before = self.memory.snapshot()
        first = set_programme_service_state(self.memory, enabled=False, as_of=self.now)
        self.assertEqual(set_programme_service_state(self.memory, enabled=False,
            as_of=(timestamp(self.now) + timedelta(days=1)).isoformat()), first)
        self.assertEqual(self.memory.snapshot(), before)


class ScheduleTests(unittest.TestCase):
    def task(self, **options):
        return ET.fromstring(native_task_xml("/mnt/d/a project", "Ubuntu-24.04", "S-1-5-21-123-456-789-1001",
            as_of="2026-10-08T10:00:00+00:00", **options))

    def test_task_repeats_and_recovers_missed_runs_without_parallel_writer(self):
        ns = {"t": NAMESPACE}; task = self.task()
        self.assertEqual(task.findtext("t:Triggers/t:TimeTrigger/t:Repetition/t:Interval", namespaces=ns), "PT15M")
        self.assertEqual(task.findtext("t:Settings/t:StartWhenAvailable", namespaces=ns), "true")
        self.assertEqual(task.findtext("t:Settings/t:MultipleInstancesPolicy", namespaces=ns), "IgnoreNew")
        self.assertEqual(task.findtext("t:Settings/t:WakeToRun", namespaces=ns), "false")
        self.assertEqual(task.findtext("t:Principals/t:Principal/t:RunLevel", namespaces=ns), "LeastPrivilege")

    def test_native_action_has_no_shell_password_publication_or_billing_upgrade(self):
        ns = {"t": NAMESPACE}; task = self.task()
        self.assertEqual(task.findtext("t:Actions/t:Exec/t:Command", namespaces=ns), "wsl.exe")
        arguments = task.findtext("t:Actions/t:Exec/t:Arguments", namespaces=ns)
        self.assertIn('--cd "/mnt/d/a project" --exec "/mnt/d/a project/.venv/bin/python"', arguments)
        self.assertIn("--tick --execute", arguments)
        self.assertNotIn("--live-model", arguments)
        self.assertNotIn("--funded", arguments)
        self.assertNotIn("bash", arguments)

    def test_approved_existing_model_and_credit_flags_are_explicit(self):
        task = self.task(live_model=True, funded_quickcommerce_fallback=True)
        arguments = task.findtext("{" + NAMESPACE + "}Actions/{" + NAMESPACE + "}Exec/{" + NAMESPACE + "}Arguments")
        self.assertIn("--live-model", arguments)
        self.assertIn("--funded-quickcommerce-fallback", arguments)

    def test_bad_distribution_sid_or_relative_path_cannot_define_a_task(self):
        for path, distro, sid in (("relative", "Ubuntu", "S-1-5-21-1"), ("/mnt/d/app", "Ubuntu;other", "S-1-5-21-1"),
                                 ("/mnt/d/app", "Ubuntu", "owner"), ("/mnt/d/../app", "Ubuntu", "S-1-5-21-1")):
            with self.assertRaisesRegex(SafetyError, "TASK_INPUT_INVALID"):
                native_task_xml(path, distro, sid)

    def test_documented_windows_trigger_sequence_and_principal_context(self):
        task = self.task(); tag = lambda name: "{" + NAMESPACE + "}" + name
        self.assertEqual(task.get("version"), "1.3")
        timer, logon = task.find(tag("Triggers"))
        self.assertEqual([node.tag for node in timer], [tag(name) for name in
                         ("Enabled", "StartBoundary", "Repetition", "ExecutionTimeLimit")])
        self.assertEqual([node.tag for node in logon], [tag(name) for name in ("Enabled", "ExecutionTimeLimit", "UserId")])
        principal = task.find(tag("Principals") + "/" + tag("Principal"))
        self.assertEqual(task.find(tag("Actions")).get("Context"), principal.get("id"))

    def test_task_matching_ignores_namespace_prefix_and_earlier_registration_start(self):
        actual = self.task()
        expected = native_task_xml("/mnt/d/a project", "Ubuntu-24.04", "S-1-5-21-123-456-789-1001",
                                  as_of="2026-10-08T11:00:00+00:00")
        ET.register_namespace("t", NAMESPACE)
        exported = ET.tostring(actual, encoding="unicode")
        self.assertIn("<t:Task", exported)
        self.assertTrue(compare_native_task(exported, expected)["matches"])

    def test_same_action_with_broken_settings_does_not_match(self):
        tag = lambda name: "{" + NAMESPACE + "}" + name
        expected = ET.tostring(self.task(), encoding="unicode")
        for name, value in (("Enabled", "false"), ("MultipleInstancesPolicy", "Parallel"),
                            ("StartWhenAvailable", "false"), ("RunOnlyIfIdle", "true"),
                            ("DisallowStartIfOnBatteries", "true"), ("ExecutionTimeLimit", "PT1M")):
            with self.subTest(setting=name):
                task = self.task(); settings = task.find(tag("Settings")); node = settings.find(tag(name))
                if node is None:
                    node = ET.SubElement(settings, tag(name))
                node.text = value
                self.assertFalse(compare_native_task(ET.tostring(task, encoding="unicode"), expected)["matches"])

    def test_same_action_with_disabled_or_different_trigger_does_not_match(self):
        tag = lambda name: "{" + NAMESPACE + "}" + name
        expected = ET.tostring(self.task(), encoding="unicode")
        for path, value in (("TimeTrigger/Enabled", "false"), ("LogonTrigger/Enabled", "false"),
                            ("TimeTrigger/Repetition/Interval", "PT1H"),
                            ("TimeTrigger/Repetition/StopAtDurationEnd", "true"),
                            ("TimeTrigger/StartBoundary", "2026-10-09T10:00:00+00:00"),
                            ("LogonTrigger/UserId", "S-1-5-21-1-2-3-1009")):
            with self.subTest(trigger=path):
                task = self.task(); task.find("/".join(tag(part) for part in ("Triggers/" + path).split("/"))).text = value
                self.assertFalse(compare_native_task(ET.tostring(task, encoding="unicode"), expected)["matches"])

    def test_unlimited_and_repetition_duration_equivalents_match(self):
        tag = lambda name: "{" + NAMESPACE + "}" + name
        expected = ET.tostring(self.task(), encoding="unicode"); actual = self.task()
        actual.find(tag("Settings") + "/" + tag("ExecutionTimeLimit")).text = "P0D"
        actual.find("/".join(tag(part) for part in "Triggers/TimeTrigger/Repetition/Interval".split("/"))).text = "PT900S"
        self.assertTrue(compare_native_task(ET.tostring(actual, encoding="unicode"), expected)["matches"])

    def test_missing_extra_or_invalid_schema_trigger_is_never_accepted_as_same(self):
        tag = lambda name: "{" + NAMESPACE + "}" + name
        expected = ET.tostring(self.task(), encoding="unicode")
        for mode in ("missing", "extra", "duplicate_field", "unknown_field", "bad_namespace"):
            with self.subTest(mode=mode):
                task = self.task(); triggers = task.find(tag("Triggers"))
                if mode == "missing":
                    triggers.remove(triggers.find(tag("LogonTrigger")))
                elif mode == "extra":
                    ET.SubElement(triggers, tag("BootTrigger"))
                elif mode == "duplicate_field":
                    ET.SubElement(triggers.find(tag("TimeTrigger")), tag("Enabled")).text = "true"
                elif mode == "unknown_field":
                    ET.SubElement(triggers.find(tag("TimeTrigger")), tag("UnrecognisedSetting")).text = "true"
                else:
                    task.tag = "{urn:wrong-task}" + "Task"
                self.assertFalse(compare_native_task(ET.tostring(task, encoding="unicode"), expected)["matches"])

    def test_registered_windows_trigger_order_does_not_change_execution_meaning(self):
        tag = lambda name: "{" + NAMESPACE + "}" + name
        expected = ET.tostring(self.task(), encoding="unicode"); actual = self.task()
        timer = actual.find(tag("Triggers") + "/" + tag("TimeTrigger"))
        children = {node.tag: node for node in timer}
        for node in list(timer):
            timer.remove(node)
        for name in ("StartBoundary", "ExecutionTimeLimit", "Repetition"):
            timer.append(children[tag(name)])
        repeat = timer.find(tag("Repetition")); repeat.remove(repeat.find(tag("StopAtDurationEnd")))
        logon = actual.find(tag("Triggers") + "/" + tag("LogonTrigger"))
        logon.remove(logon.find(tag("Enabled")))
        self.assertTrue(compare_native_task(ET.tostring(actual, encoding="unicode"), expected)["matches"])

    def test_explicit_scheduler_engine_difference_is_not_ignored(self):
        tag = lambda name: "{" + NAMESPACE + "}" + name
        actual = self.task(); expected = ET.tostring(actual, encoding="unicode")
        settings = actual.find(tag("Settings")); node = settings.find(tag("UseUnifiedSchedulingEngine"))
        if node is None:
            node = ET.SubElement(settings, tag("UseUnifiedSchedulingEngine"))
        node.text = "false"
        result = compare_native_task(ET.tostring(actual, encoding="unicode"), expected)
        self.assertFalse(result["matches"])
        self.assertIn("UseUnifiedSchedulingEngine", result["differences"])

    def test_actual_registered_windows_export_matches_generated_intent(self):
        fixture = load_json(ROOT / "tests/fixtures/native_tasks/windows_task_export_2026-10-08.json")
        repo = "/mnt/d/coding/business/ideas and trials and forked and misc/dropshipping-product-and-supplier-finder"
        expected = native_task_xml(repo, "Ubuntu", "S-1-5-21-123-456-789-1001",
            as_of="2026-10-08T11:40:00+00:00", live_model=True, funded_quickcommerce_fallback=True)
        self.assertEqual(compare_native_task(fixture["actual_xml"], expected), {"matches": True, "differences": []})

    def test_actual_export_still_refuses_changed_engine_action_owner_and_interval(self):
        fixture = load_json(ROOT / "tests/fixtures/native_tasks/windows_task_export_2026-10-08.json")
        repo = "/mnt/d/coding/business/ideas and trials and forked and misc/dropshipping-product-and-supplier-finder"
        expected = native_task_xml(repo, "Ubuntu", "S-1-5-21-123-456-789-1001",
            as_of="2026-10-08T11:40:00+00:00", live_model=True, funded_quickcommerce_fallback=True)
        tag = lambda name: "{" + NAMESPACE + "}" + name
        for path, value, difference in (("Settings/UseUnifiedSchedulingEngine", "false", "UseUnifiedSchedulingEngine"),
                ("Actions/Exec/Command", "powershell.exe", "command"),
                ("Principals/Principal/UserId", "S-1-5-21-1-2-3-1009", "owner"),
                ("Triggers/TimeTrigger/Repetition/Interval", "PT1H", "interval_seconds")):
            with self.subTest(setting=path):
                actual = ET.fromstring(fixture["actual_xml"])
                actual.find("/".join(tag(part) for part in path.split("/"))).text = value
                result = compare_native_task(ET.tostring(actual, encoding="unicode"), expected)
                self.assertFalse(result["matches"])
                self.assertIn(difference, result["differences"])

    def test_windows_account_alias_requires_real_resolution_not_a_python_name_guess(self):
        fixture = load_json(ROOT / "tests/fixtures/native_tasks/windows_task_export_2026-10-08.json")
        tag = lambda name: "{" + NAMESPACE + "}" + name
        actual = ET.fromstring(fixture["actual_xml"])
        actual.find("/".join(tag(part) for part in "Triggers/LogonTrigger/UserId".split("/"))).text = fixture["synthetic_raw_logon_user"]
        result = compare_native_task(ET.tostring(actual, encoding="unicode"), fixture["actual_xml"])
        self.assertFalse(result["matches"])
        self.assertEqual(result["differences"], ["logon_owner"])

    def test_install_helper_uses_native_validation_and_create_only_flags(self):
        script = (ROOT / "scripts/setup_programme_task.ps1").read_text(encoding="utf-8")
        self.assertIn("RegisterTaskDefinition($taskName, $definition, 1,", script)
        self.assertIn("RegisterTaskDefinition($taskName, $definition, 2,", script)
        self.assertIn("--compare-task-xml", script)
        self.assertIn("/t:Task/t:Principals/t:Principal/t:UserId | /t:Task/t:Triggers/t:LogonTrigger/t:UserId", script)
        self.assertIn(".Translate([System.Security.Principal.SecurityIdentifier]).Value", script)
        self.assertIn("A task owner identity could not be resolved. The task was not changed.", script)
        self.assertNotIn("Register-ScheduledTask", script)
        self.assertNotIn("Start-ScheduledTask", script)
        self.assertNotIn("Stop-ScheduledTask", script)


class ProgrammeInventoryAndStagingTests(unittest.TestCase):
    setUp = ProgrammeTests.setUp
    product = ProgrammeTests.product
    prepare = ProgrammeTests.prepare
    noops = ProgrammeTests.noops
    advance = ProgrammeTests.advance

    def save_draft(self, issue_id="private-test-issue", revision=1, *, complete=True):
        from product_radar.programme import _save
        record = load_programme(self.memory, "whole-programme")
        draft = {"issue_id": issue_id, "revision": revision, "complete": complete, "approved": False,
                 "state": "draft_ready_for_review", "package_hash": digest([issue_id, revision]),
                 "binding": digest([issue_id, revision, "input"])}
        record["drafts"][issue_id] = draft; _save(self.memory, record)
        return draft

    def packet(self, _root, issue_id, revision):
        manifest = {"client_id": "default", "package_hash": digest([issue_id, revision])}
        files = [{"name": name, "size": 12, "sha256": digest([name, revision]), "raw": b"not retained"}
                 for name in ("shortlist.xlsx", "report-en.pdf", "report-hi-Latn.pdf", "final_report_data.json", "delivery-manifest.json")]
        return manifest, files

    def receipt(self, issue_id, revision, *, state="private_bytes_verified"):
        manifest, files = self.packet(self.root, issue_id, revision)
        return {"issue_id": issue_id, "revision": revision, "client_id": manifest["client_id"],
                "package_hash": manifest["package_hash"], "state": state, "index_unchanged": True,
                "approved": False, "release_ready": False, "client_available": False,
                "index_write_attempted": False, "publication_attempted": False,
                "outcomes": [{"name": row["name"], "size": row["size"], "sha256": row["sha256"], "state": "verified"} for row in files]}

    def private_steps(self):
        steps = self.noops(); steps.pop("private_staging"); return steps

    def test_frozen_inventory_is_available_only_to_reporting_not_source_or_kpi_lanes(self):
        record = self.prepare(concept_ids=[], reporting_inventory_concept_ids=["first", "second"])
        calls = []
        steps = {stage: (lambda _m, row, _c, lane=stage:
                 calls.append((lane, list(row["concept_ids"]), _reporting_ids(row))) or {}) for stage in STAGES}
        result = self.advance(steps=steps)
        self.assertTrue(all(ids == [] for _, ids, _ in calls))
        self.assertEqual(record["concept_ids"], [])
        self.assertEqual(result["progress"]["products"], 0)
        self.assertEqual(result["progress"]["reporting_inventory_products"], 2)
        self.assertEqual(result["progress"]["reporting_products"], 2)
        self.assertEqual(self.memory.snapshot()["query_tasks"], {})
        self.assertEqual(self.memory.snapshot()["jobs"], {})

    def test_reporting_inventory_membership_is_frozen_and_legacy_default_remains_empty(self):
        self.prepare(concept_ids=[], reporting_inventory_concept_ids=["first"])
        self.assertEqual(self.prepare(concept_ids=[])["reporting_inventory_concept_ids"], ["first"])
        with self.assertRaisesRegex(SafetyError, "BINDING_CHANGED"):
            self.prepare(concept_ids=[], reporting_inventory_concept_ids=["second"])
        record = load_programme(self.memory, "whole-programme")
        from product_radar.programme import _binding
        self.assertEqual(_binding(dict(record, reporting_inventory_concept_ids=[]))["reporting_inventory_concept_ids"], [])

    def test_next_week_freezes_unused_eligible_inventory_without_cloning_old_source_work(self):
        self.prepare(); self.advance()
        before = self.memory.snapshot()
        with patch("product_radar.report_selection.build_report_pool", return_value={"eligible": [{"concept_id": "first"}, {"concept_id": "second"}]}):
            fresh = due_programme(self.memory, as_of="2026-10-19T04:00:00+00:00")
        self.assertEqual(fresh["concept_ids"], [])
        self.assertEqual(fresh["reporting_inventory_concept_ids"], ["first", "second"])
        for table in ("query_tasks", "jobs", "captures", "published_history", "issue_reservations"):
            self.assertEqual(self.memory.snapshot()[table], before[table])

    def test_published_held_and_unrelated_products_are_not_unused_inventory(self):
        self.prepare(); self.advance(); self.product("unrelated")
        self.memory.put("published_history", "published-proof", {"status": "published", "concept_id": "first", "client_id": "default", "issue_id": "earlier-published-issue"})
        self.memory.put("issue_reservations", "held-proof", {"state": "reserved", "client_id": "default", "issue_id": "earlier-held-issue", "products": [{"concept_id": "second"}]})
        with patch("product_radar.report_selection.build_report_pool", return_value={"eligible": [{"concept_id": "first"}, {"concept_id": "second"}, {"concept_id": "unrelated"}]}):
            fresh = due_programme(self.memory, as_of="2026-10-19T04:00:00+00:00")
        self.assertEqual(fresh["reporting_inventory_concept_ids"], [])

    def test_untrusted_history_excludes_old_inventory_but_allows_new_week_research(self):
        self.prepare(); self.advance()
        self.memory.put("published_history", "broken-history", {"status": "published"})
        fresh = due_programme(self.memory, as_of="2026-10-19T04:00:00+00:00")
        self.assertEqual(fresh["reporting_inventory_concept_ids"], [])
        self.assertIn("could not be verified", fresh["reporting_inventory_reason"])
        self.assertEqual(fresh["concept_ids"], [])

    def test_reporting_rechecks_later_publication_but_preserves_own_issue_hold_on_resume(self):
        record = self.prepare(concept_ids=[], reporting_inventory_concept_ids=["first", "second"])
        self.memory.put("published_history", "later-published", {"status": "published", "concept_id": "first", "client_id": "default", "issue_id": "other-issue"})
        self.memory.put("issue_reservations", "own-current-hold", {"state": "reserved", "client_id": "default",
            "issue_id": record["release_slots"][0]["issue_id"], "products": [{"concept_id": "second"}]})
        steps = self.noops(); steps.pop("reports"); selected = []
        def allocate(eligible, *_args, **_kwargs):
            selected.extend(row["concept_id"] for row in eligible); return {"issues": []}
        with patch("product_radar.report_selection.build_report_pool", return_value={"eligible": [{"concept_id": "first"}, {"concept_id": "second"}]}), \
             patch("product_radar.issue_allocation.allocate_issues", side_effect=allocate):
            self.advance(steps=steps)
        self.assertEqual(selected, ["second"])

    def test_missing_private_callback_is_independent_setup_wait_and_not_success(self):
        self.prepare(); self.save_draft(); calls = []
        steps = self.private_steps(); steps["acquire"] = lambda *_args: calls.append("research") or {}
        with patch("product_radar.private_drafts.prepare_private_draft", side_effect=self.packet):
            result = self.advance(steps=steps)
        self.assertEqual(calls, ["research"])
        self.assertEqual(result["progress"]["private_staging_states"], {"setup_waiting": 1})
        record = load_programme(self.memory, "whole-programme")
        self.assertTrue(record["drafts"]["private-test-issue"]["complete"])
        self.assertFalse(result["client_publication_attempted"])

    def test_same_verified_private_packet_is_not_uploaded_again_on_resume(self):
        self.prepare(); self.save_draft(); calls = []
        def callback(issue_id, revision):
            calls.append((issue_id, revision)); return self.receipt(issue_id, revision)
        with patch("product_radar.private_drafts.prepare_private_draft", side_effect=self.packet):
            self.advance(steps=self.private_steps(), private_staging=callback)
            result = self.advance(steps=self.private_steps(), private_staging=callback)
        self.assertEqual(calls, [("private-test-issue", 1)])
        self.assertEqual(result["progress"]["private_staging_states"], {"private_bytes_verified": 1})
        self.assertEqual(load_programme(self.memory, "whole-programme")["last_results"]["private_staging"]["outcomes"][0]["state"], "reused")

    def test_new_private_revision_reopens_only_that_packet_and_keeps_old_proof(self):
        self.prepare(); self.save_draft(); calls = []
        def callback(issue_id, revision):
            calls.append(revision); return self.receipt(issue_id, revision)
        with patch("product_radar.private_drafts.prepare_private_draft", side_effect=self.packet):
            self.advance(steps=self.private_steps(), private_staging=callback)
            self.save_draft(revision=2)
            self.advance(steps=self.private_steps(), private_staging=callback)
        self.assertEqual(calls, [1, 2])
        self.assertEqual(len(load_programme(self.memory, "whole-programme")["private_staging_receipts"]), 2)

    def test_failed_or_unreadable_private_packet_does_not_block_another_good_packet(self):
        self.prepare(); self.save_draft("a-broken"); self.save_draft("b-good"); calls = []
        def prepare(root, issue_id, revision):
            if issue_id == "a-broken":
                raise SafetyError("PRIVATE_DRAFT_FILE_CHANGED")
            return self.packet(root, issue_id, revision)
        def callback(issue_id, revision):
            calls.append(issue_id); return self.receipt(issue_id, revision)
        with patch("product_radar.private_drafts.prepare_private_draft", side_effect=prepare):
            result = self.advance(steps=self.private_steps(), private_staging=callback)
        self.assertEqual(calls, ["b-good"])
        self.assertEqual(result["progress"]["private_staging_states"], {"waiting": 1, "private_bytes_verified": 1})

    def test_private_prepared_or_four_verified_files_are_not_false_upload_success(self):
        for state in ("prepared", "private_bytes_verified"):
            with self.subTest(state=state):
                self.prepare(); issue_id = "private-test-issue-" + state.replace("_", "-")
                self.save_draft(issue_id); receipt = self.receipt(issue_id, 1, state=state)
                receipt["outcomes"] = receipt["outcomes"][:4]
                with patch("product_radar.private_drafts.prepare_private_draft", side_effect=self.packet):
                    result = self.advance(steps=self.private_steps(), private_staging=lambda *_args: receipt)
                self.assertNotIn("private_bytes_verified", result["progress"]["private_staging_states"])

    def test_private_callback_cannot_claim_publication_or_index_write(self):
        self.prepare(); self.save_draft()
        receipt = self.receipt("private-test-issue", 1); receipt["index_write_attempted"] = True
        with patch("product_radar.private_drafts.prepare_private_draft", side_effect=self.packet):
            result = self.advance(steps=self.private_steps(), private_staging=lambda *_args: receipt)
        self.assertEqual(result["progress"]["private_staging_states"], {"waiting": 1})
        self.assertEqual(self.memory.snapshot()["published_history"], {})

    def test_private_result_requires_canonical_revision_identity_sizes_and_booleans(self):
        from product_radar.programme import _private_result
        manifest, files = self.packet(self.root, "private-test-issue", 1)
        packet = {"issue_id": "private-test-issue", "revision": 1, "package_hash": manifest["package_hash"],
                  "client_id": "default", "files": files}
        for changes in ({"revision": True}, {"revision": 1.0}, {"index_unchanged": 1}, {"approved": 0},
                        {"release_ready": 0}, {"client_available": 0}, {"issue_id": "../different"},
                        {"verified_file_count": True}):
            receipt = dict(self.receipt("private-test-issue", 1), **changes)
            with self.subTest(changes=changes), self.assertRaises(SafetyError):
                _private_result(receipt, packet)
        receipt = self.receipt("private-test-issue", 1)
        receipt["outcomes"][0]["size"] = 12.0
        with self.assertRaisesRegex(SafetyError, "RESULT_INVALID"):
            _private_result(receipt, packet)

    def test_valid_later_single_language_packet_derives_exact_four_file_proof(self):
        from product_radar.programme import _private_result
        manifest, files = self.packet(self.root, "later-language-issue", 3)
        files = [row for row in files if row["name"] != "report-hi-Latn.pdf"]
        packet = {"issue_id": "later-language-issue", "revision": 3, "package_hash": manifest["package_hash"],
                  "client_id": "default", "files": files}
        receipt = self.receipt("later-language-issue", 3)
        receipt["outcomes"] = [row for row in receipt["outcomes"] if row["name"] != "report-hi-Latn.pdf"]
        receipt["verified_file_count"] = 4
        result = _private_result(receipt, packet)
        self.assertEqual(result["state"], "private_bytes_verified")
        self.assertEqual(result["verified_file_count"], 4)
        for missing in ("shortlist.xlsx", "report-en.pdf", "final_report_data.json", "delivery-manifest.json"):
            incomplete = dict(receipt, outcomes=[row for row in receipt["outcomes"] if row["name"] != missing], verified_file_count=3)
            with self.subTest(missing=missing), self.assertRaisesRegex(SafetyError, "RESULT_INVALID"):
                _private_result(incomplete, packet)

    def test_private_packet_cannot_omit_every_pdf_or_a_core_file_from_expected_set(self):
        from product_radar.programme import _private_result
        manifest, files = self.packet(self.root, "later-language-issue", 3)
        for missing in ({"report-en.pdf", "report-hi-Latn.pdf"}, {"shortlist.xlsx"}, {"final_report_data.json"}, {"delivery-manifest.json"}):
            packet = {"issue_id": "later-language-issue", "revision": 3, "package_hash": manifest["package_hash"], "client_id": "default",
                      "files": [row for row in files if row["name"] not in missing]}
            receipt = self.receipt("later-language-issue", 3)
            receipt["outcomes"] = [row for row in receipt["outcomes"] if row["name"] not in missing]
            with self.subTest(missing=missing), self.assertRaisesRegex(SafetyError, "RESULT_INVALID"):
                _private_result(receipt, packet)

    def test_private_records_retain_only_metadata_not_callback_raw_files_or_credentials(self):
        self.prepare(); self.save_draft()
        receipt = self.receipt("private-test-issue", 1); receipt.update(raw=b"sensitive raw packet", service_client_secret="never retained")
        with patch("product_radar.private_drafts.prepare_private_draft", side_effect=self.packet):
            self.advance(steps=self.private_steps(), private_staging=lambda *_args: receipt)
        stored = str(load_programme(self.memory, "whole-programme")["private_staging_receipts"])
        self.assertNotIn("sensitive raw packet", stored); self.assertNotIn("never retained", stored)

    def test_finished_research_with_private_wait_is_retried_without_blocking_fresh_week(self):
        from product_radar.programme import _save
        self.prepare(); self.save_draft(); self.advance()
        old = load_programme(self.memory, "whole-programme")
        old.update(status="research_pool_finished", next_retry_at="2026-10-19T04:10:00+00:00")
        _save(self.memory, old)
        fresh = due_programme(self.memory, as_of="2026-10-19T04:00:00+00:00")
        self.assertNotEqual(fresh["programme_id"], old["programme_id"])
        advance_programme(self.memory, fresh["programme_id"], root=self.root, handlers={}, steps=self.noops(), as_of="2026-10-19T04:00:00+00:00")
        chosen = due_programme(self.memory, as_of="2026-10-19T04:15:00+00:00")
        self.assertEqual(chosen["programme_id"], old["programme_id"])

    def test_real_private_packet_and_machine_callback_are_connected_without_client_publication(self):
        from test_private_drafts import PrivateDraftTests, Store
        from product_radar.draft_automation import private_staging_callback
        fixture = PrivateDraftTests("test_prepare_rechecks_real_files_without_storage_or_publication")
        fixture.setUp(); self.addCleanup(fixture.doCleanups)
        self.prepare()
        from product_radar.programme import _save
        record = load_programme(self.memory, "whole-programme")
        record["drafts"][fixture.issue["issue_id"]] = {"revision": fixture.issue["revision"], "complete": True,
            "package_hash": fixture.manifest["package_hash"], "approved": False}
        _save(self.memory, record); store = Store()
        result = advance_programme(self.memory, "whole-programme", root=fixture.root, handlers={}, steps=self.private_steps(),
            as_of=self.now, private_staging=private_staging_callback(fixture.root, store))
        self.assertEqual(result["progress"]["private_staging_states"], {"private_bytes_verified": 1})
        self.assertEqual(len(store.objects), 5)
        self.assertEqual(self.memory.snapshot()["published_history"], {})
        self.assertFalse(result["client_publication_attempted"])


class ProgrammeCliTests(unittest.TestCase):
    setUp = ProgrammeTests.setUp
    product = ProgrammeTests.product
    prepare = ProgrammeTests.prepare
    noops = ProgrammeTests.noops

    def cli(self):
        spec = importlib.util.spec_from_file_location("programme_cli", ROOT / "scripts/run_programme.py")
        module = importlib.util.module_from_spec(spec); spec.loader.exec_module(module)
        return module

    def test_cli_read_only_default_cannot_call_models_or_sources(self):
        self.prepare(); cli = self.cli(); before = self.memory.snapshot()
        with patch("sys.stdout", new=StringIO()), patch.object(cli, "advance_programme", side_effect=AssertionError("external action")), \
             patch("product_radar.catalogue_discovery.create_catalogue_discovery_step", side_effect=AssertionError("execution setup")):
            self.assertEqual(cli.main(["--programme-id", "whole-programme"], memory=self.memory, root=self.root), 0)
        self.assertEqual(self.memory.snapshot(), before)

    def test_cli_injects_repository_seed_inventory_without_calling_seed_or_source(self):
        self.prepare(); cli = self.cli(); callback = lambda *_args: {"state": "nothing_new"}
        with patch("sys.stdout", new=StringIO()), \
             patch("product_radar.catalogue_discovery.create_catalogue_discovery_step", return_value=callback) as factory, \
             patch.object(cli, "advance_programme", return_value={}) as advance:
            self.assertEqual(cli.main(["--programme-id", "whole-programme", "--execute"], memory=self.memory,
                handlers={}, steps=self.noops(), private_staging=lambda *_args: {}, root=self.root), 0)
        # The repository inventory and this store's root; the store prepares
        # its own seed manifest later, inside the ordinary seed lane.
        from product_radar.catalogue_seed import load_seed_inventory
        factory.assert_called_once_with(seed_inventory=load_seed_inventory(), seed_source_root=self.root)
        self.assertIs(advance.call_args.kwargs["discovery_step"], callback)
        state = self.memory.snapshot()
        self.assertEqual(state["jobs"], {}); self.assertEqual(state["captures"], {})

    def test_cli_installed_sources_advance_the_dropshipping_platforms_in_their_own_lane(self):
        from product_radar import platform_roles
        from test_research_flow import FakeRunner
        self.prepare(); cli = self.cli(); callback = lambda *_args: {"state": "nothing_new"}
        for discovery in ({"return_value": callback}, {"side_effect": SafetyError("DISCOVERY_CATALOGUE_INVALID")}):
            with self.subTest(discovery=discovery), patch("sys.stdout", new=StringIO()), \
                    patch.object(cli, "installed_source_handlers", return_value={}) as installed, \
                    patch("product_radar.catalogue_discovery.create_catalogue_discovery_step", **discovery), \
                    patch.object(cli, "advance_programme", return_value={}) as advance:
                self.assertEqual(cli.main(["--programme-id", "whole-programme", "--execute"], memory=self.memory,
                    steps=self.noops(), private_staging=lambda *_args: {}, root=self.root, runner=FakeRunner()), 0)
                installed.assert_called_once()
                # Discovery is unchanged (or missing after its setup failed); the platforms still run.
                self.assertIs(advance.call_args.kwargs["discovery_step"], callback if "return_value" in discovery else None)
                self.assertIs(advance.call_args.kwargs["platform_step"], platform_roles.platform_step)
        with patch("product_radar.platform_roles.advance_dropship_platforms",
                   return_value={"discovery": {"new_concept_ids": []}}) as platforms:
            platform_roles.platform_step(self.memory, {}, {"at": "2026-10-14T00:00:00+00:00"})
        platforms.assert_called_once_with(self.memory, {"at": "2026-10-14T00:00:00+00:00"})
        # Supplied sources (tests, saved-only runs) have no platform lane.
        with patch("sys.stdout", new=StringIO()), patch.object(cli, "advance_programme", return_value={}) as advance:
            cli.main(["--programme-id", "whole-programme", "--execute"], memory=self.memory, handlers={},
                     steps=self.noops(), private_staging=lambda *_args: {}, root=self.root, discovery_step=callback)
        self.assertIsNone(advance.call_args.kwargs["platform_step"])

    def test_cli_missing_grounded_sources_keeps_healthy_stages_running_with_named_seed_wait(self):
        self.prepare(); cli = self.cli()
        with patch("sys.stdout", new=StringIO()):
            self.assertEqual(cli.main(["--programme-id", "whole-programme", "--execute"], memory=self.memory,
                handlers={}, steps=self.noops(), private_staging=lambda *_args: {}, root=self.root), 0)
        record = load_programme(self.memory, "whole-programme")
        self.assertTrue(set(STAGES) <= set(record["last_results"]))
        seed = record["last_results"]["discovery"]["seed"]
        self.assertEqual(seed["state"], "waiting")
        self.assertEqual(seed["failures"][0]["reason"], "CATALOGUE_SEED_SOURCES_UNAVAILABLE")
        self.assertTrue(seed["missing_sources"])
        self.assertNotIn("catalogue_seed", record["setup_failures"])
        state = self.memory.snapshot()
        self.assertEqual(state["jobs"], {}); self.assertEqual(state["captures"], {})
        self.assertFalse(any(row.get("kind") == "catalogue-discovery-seed-cycle-v1" for row in state["batch_runs"].values()))
        self.assertEqual(list((self.memory.root / "catalogue_materials/preparations").glob("seed-manifest-*.json")), [])

    def test_cli_prepare_uses_an_existing_source_identity_without_execution(self):
        cli = self.cli()
        with patch("sys.stdout", new=StringIO()), patch.object(cli, "advance_programme", side_effect=AssertionError("external action")):
            self.assertEqual(cli.main(["--programme-id", "whole-programme", "--source-run-id", "current-156", "--prepare"], memory=self.memory, root=self.root), 0)
        self.assertEqual(load_programme(self.memory, "whole-programme")["source_run_id"], "current-156")

    def test_cli_execution_routes_to_real_programme_not_unapproved_publication(self):
        self.prepare(); cli = self.cli()
        with patch("sys.stdout", new=StringIO()):
            self.assertEqual(cli.main(["--programme-id", "whole-programme", "--execute"], memory=self.memory,
                handlers={}, steps=self.noops(), root=self.root), 0)
        self.assertEqual(self.memory.snapshot()["published_history"], {})
        self.assertEqual(self.memory.snapshot()["issue_reservations"], {})

    def test_cli_loads_only_local_machine_draft_access_and_injects_callback_into_programme(self):
        self.prepare(); cli = self.cli(); calls = []
        callback = lambda *_args: calls.append("would-stage")
        with patch("sys.stdout", new=StringIO()), \
             patch("product_radar.draft_automation.load_draft_automation_storage", return_value=object()) as factory, \
             patch("product_radar.draft_automation.private_staging_callback", return_value=callback), \
             patch.object(cli, "advance_programme", return_value={}) as advance:
            self.assertEqual(cli.main(["--programme-id", "whole-programme", "--execute"], memory=self.memory,
                handlers={}, steps=self.noops(), root=self.root), 0)
        factory.assert_called_once_with(self.root)
        self.assertIs(advance.call_args.kwargs["private_staging"], callback)
        self.assertEqual(calls, [])

    def test_cli_missing_machine_access_defers_that_lane_without_skipping_execution(self):
        self.prepare(); cli = self.cli()
        with patch("sys.stdout", new=StringIO()), \
             patch("product_radar.draft_automation.load_draft_automation_storage", side_effect=SafetyError("PORTAL_DRAFT_SERVICE_CREDENTIALS_UNAVAILABLE")), \
             patch.object(cli, "advance_programme", return_value={}) as advance:
            self.assertEqual(cli.main(["--programme-id", "whole-programme", "--execute"], memory=self.memory,
                handlers={}, steps=self.noops(), root=self.root), 0)
        self.assertIsNone(advance.call_args.kwargs["private_staging"])
        self.assertEqual(advance.call_args.kwargs["setup_failures"]["private_staging"], "PORTAL_DRAFT_SERVICE_CREDENTIALS_UNAVAILABLE")

    def test_cli_owner_switch_needs_no_programme_and_paused_tick_creates_no_new_run(self):
        cli = self.cli(); before = self.memory.snapshot()
        with patch("sys.stdout", new=StringIO()), patch.object(cli, "due_programme", side_effect=AssertionError("new run")):
            self.assertEqual(cli.main(["--pause"], memory=self.memory, root=self.root), 0)
            self.assertEqual(cli.main(["--tick", "--execute"], memory=self.memory, root=self.root), 0)
            self.assertEqual(cli.main(["--resume"], memory=self.memory, root=self.root), 0)
        self.assertEqual(self.memory.snapshot(), before)
        self.assertTrue(programme_service_state(self.memory)["enabled"])

    def test_cli_progress_replaces_large_profile_and_capture_bodies_with_counts(self):
        cli = self.cli()
        row = {"programme_id": "whole-programme", "stage": "profiles", "cycle": 1, "phase_state": "finished",
               "result": {"records": [{"input": "raw public profile body" * 1000}] * 20,
                          "pending": [], "reused": [{"receipt": "raw saved receipt"}], "job_ids": ["job-a", "job-b"],
                          "outcomes": [{"state": "accepted"}], "current_accepted_reused": 10},
               "control_save": {"saved": "canonical", "reason": None}}
        compact = cli.compact_progress(row)
        self.assertEqual(compact["counts"], {"records": 20, "pending": 0, "reused": 1, "job_ids": 2,
                                            "outcomes": 1, "current_accepted_reused": 10})
        self.assertEqual(compact["outcome_states"], {"accepted": 1})
        self.assertNotIn("raw public", str(compact)); self.assertNotIn("raw saved", str(compact))
        self.assertLess(len(str(compact)), 700)

    def test_cli_task_comparison_is_read_only_and_prints_only_match_metadata(self):
        import json
        cli = self.cli(); task = native_task_xml("/mnt/d/a project", "Ubuntu", "S-1-5-21-1-2-3-1001")
        before = self.memory.snapshot(); output = StringIO()
        with patch("sys.stdin", new=StringIO(json.dumps({"actual_xml": task, "expected_xml": task}))), patch("sys.stdout", new=output):
            self.assertEqual(cli.main(["--compare-task-xml"], memory=self.memory, root=self.root), 0)
        self.assertEqual(json.loads(output.getvalue()), {"matches": True, "differences": []})
        self.assertEqual(self.memory.snapshot(), before)


if __name__ == "__main__":
    unittest.main()
