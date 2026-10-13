"""Synthetic controller contracts; no provider, browser, paid work or model."""

from copy import deepcopy
from pathlib import Path
import importlib.util
from io import StringIO
import sys
import tempfile
import time
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from product_radar.models import SafetyError, digest
from product_radar.research_memory import ResearchMemory, key
from product_radar.source_coverage import (CHECK_KIND, ROUTES, TASK_KIND, bind_source_relevance,
    continue_source_run, create_marketplace_handlers, execute_source_run, installed_source_handlers, prepare_all_source_tasks,
    prepare_source_tasks, product_source_request_builder, resume_source_route, retry_source_prelaunch_task,
    route_registry, source_coverage, source_run_progress)
from product_radar.storage import Journal

NOW = "2026-10-08T10:00:00+00:00"
LATER = "2026-10-08T10:05:01+00:00"


class SourceCoverageTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.memory = ResearchMemory(self.root / "research", Journal(self.root / "finance"), evidence_type="synthetic")
        for concept_id in ("first", "second"):
            self.product(concept_id)

    def product(self, concept_id, *, brand="generic_or_small", status="active"):
        row = {"status": status, "description": {"label": "Sink caddy", "category": "Home and Kitchen",
            "function": "Store sink essentials", "mechanism": "manual", "audience": "adult",
            "features": ["draining"], "basis": "observed", "brand": brand, "reason": "Synthetic fixture."}}
        self.memory.put("concepts", concept_id, row)

    def question(self, route="meta-ads", ids=None, query="sink caddy"):
        return {"route": route, "request": {"query": query, "country": "IN"}, "concept_ids": ids or ["first"]}

    def plan(self, questions, **kwargs):
        return prepare_source_tasks(self.memory, questions, run_id="week-one", as_of=NOW, **kwargs)

    def receipt(self, context, *, state="usable", actual=True, retryable=False, targets=None):
        return {"schema_version": 1, "route": context["route"], "acquisition_id": context["acquisition_id"],
                "observed_at": NOW, "state": state, "dispatch_started": actual,
                "reference": context["acquisition_id"] + "/receipt.json", "capture_ids": [],
                "observation_ids": [], "target_results": targets or [], "reason": "Synthetic source receipt.",
                "retryable": retryable}

    def acquired(self, context):
        context["record_dispatch"]()
        return self.receipt(context)

    def direct_runner(self, callback, context, _timeout):
        return callback(context)

    def run_sources(self, handlers, **kwargs):
        return execute_source_run(self.memory, "week-one", handlers=handlers, as_of=NOW,
                                  bounded_call=self.direct_runner, **kwargs)

    def query_profile(self, concept_id="first", *, alternatives=True, aliases=None):
        from product_radar.product_queries import SOURCES, _from_type, _record, query_profile_input
        item = query_profile_input(self.memory.snapshot(), concept_id)
        profile = _from_type(item, "sink caddy", method="synthetic")
        if alternatives:
            profile["core_type"] = "sink organizer"
            profile["subtype"] = "draining sink caddy"
            profile["aliases"] = ["sink holder"]
            profile["queries"] = [{"source": source, "text": text, "scope": scope,
                                    "reason": "Reviewed synthetic product-type meaning."}
                for source in SOURCES for text, scope in
                (("draining sink caddy", "subtype"), ("sink organizer", "family"), ("sink holder", "family"))]
        elif aliases is not None:
            profile["aliases"] = aliases
        return _record(self.memory, item, profile, "synthetic", NOW)

    def query_context(self, route="meta-ads", concept_id="first", *, run_id="week-one"):
        state = self.memory.snapshot()
        return {"route": route, "concept_id": concept_id, "state": state, "run_id": run_id,
                "description": state["concepts"][concept_id]["description"],
                "description_hash": digest(state["concepts"][concept_id]["description"])}

    def plan_query(self, builder, *, route="meta-ads", concept_id="first", run_id="week-one"):
        context = self.query_context(route, concept_id, run_id=run_id)
        request = builder(context)
        question = {"route": route, "request": request, "concept_ids": [concept_id]}
        if "query_plan" in context:
            question["query_plan"] = context["query_plan"]
        return prepare_source_tasks(self.memory, [question], run_id=run_id, as_of=NOW)

    def test_registry_has_fourteen_agreed_routes_and_only_three_demand_groups(self):
        registry = route_registry()
        self.assertEqual(len(registry), 14)
        self.assertTrue(all(not row["connected"] for row in registry.values()))
        self.assertEqual({row["demand_group"] for row in registry.values()} - {None}, {"meta", "marketplace", "trends"})
        self.assertEqual({registry[r]["demand_group"] for r in ("blinkit", "zepto", "instamart", "google-suppliers")}, {None})
        connected = route_registry({"meta-ads": self.acquired})
        self.assertTrue(connected["meta-ads"]["connected"])
        self.assertFalse(connected["google-trends"]["connected"])

    def test_registry_refuses_unknown_or_noncallable_handlers(self):
        with self.assertRaisesRegex(SafetyError, "REGISTRY"):
            route_registry({"made-up-source": self.acquired})
        with self.assertRaisesRegex(SafetyError, "HANDLER"):
            route_registry({"meta-ads": True})

    def test_one_malformed_product_leaves_all_healthy_source_questions_running(self):
        state = self.memory.snapshot(); old = state["concepts"]["first"]
        self.memory.put("concepts", "first", dict(old, description=None), previous=old)
        prepared = prepare_all_source_tasks(self.memory, run_id="week-one", as_of=NOW,
            request_builder=lambda context: {"query": "sink caddy", "country": "IN"})
        self.assertEqual(prepared["physical_questions"], 14)
        self.assertEqual(len(prepared["pending_preparation"]), 14)
        result = self.run_sources({route: self.acquired for route in ROUTES})
        self.assertEqual(result["handler_calls"], 14)
        grid = source_coverage(self.memory, handlers={route: self.acquired for route in ROUTES}, as_of=NOW)
        broken = [row for row in grid["rows"] if row["concept_id"] == "first"]
        self.assertEqual(len(grid["rows"]), 28)
        self.assertTrue(all(row["state"] == "not_attempted" and not row["actual_attempt"] for row in broken))
        self.assertTrue(all(row["reason"] == "SOURCE_PRODUCT_DESCRIPTION_INVALID" for row in broken))
        self.assertEqual(grid["actually_attempted_product_routes"], 14)

    def test_product_brand_binding_conflict_is_not_a_global_source_exception(self):
        from unittest.mock import patch
        def brand(state, kind, cid, description):
            if cid == "first":
                raise SafetyError("BRAND_ORIGIN_BINDING_CHANGED")
            return "generic_or_small"
        with patch("product_radar.source_coverage.current_brand", side_effect=brand):
            prepared = prepare_all_source_tasks(self.memory, run_id="week-one", as_of=NOW,
                request_builder=lambda context: {"query": "sink caddy", "country": "IN"})
            grid = source_coverage(self.memory, as_of=NOW)
        self.assertEqual(prepared["physical_questions"], 14)
        self.assertEqual(len(prepared["pending_preparation"]), 14)
        self.assertTrue(all(row["reason"] == "BRAND_ORIGIN_BINDING_CHANGED"
            for row in grid["rows"] if row["concept_id"] == "first"))

    def test_route_connection_and_global_source_checks_do_not_fake_product_attempts(self):
        self.memory.put("source_checks", "global-test", {"source": "meta", "route": "apify", "at": NOW,
                        "state": "usable", "reason": "Global route check only.", "reference": "global-test"})
        grid = source_coverage(self.memory, handlers={"meta-ads": self.acquired}, as_of=NOW)
        self.assertEqual(grid["product_route_questions"], 28)
        self.assertEqual(grid["actually_attempted_product_routes"], 0)
        self.assertEqual(grid["states"], {"not_attempted": 2, "not_connected": 26})

    def test_full_156_grid_preserves_three_known_brands_without_excluding_categories(self):
        for number in range(154):
            self.product("extra-" + str(number), brand="established" if number < 3 else "generic_or_small")
        grid = source_coverage(self.memory, as_of=NOW)
        self.assertEqual(grid["products"], 156)
        self.assertEqual(grid["product_route_questions"], 2184)
        self.assertEqual(grid["states"]["not_applicable"], 42)
        self.assertEqual(grid["states"]["not_connected"], 2142)

    def test_same_family_question_shares_acquisition_but_keeps_each_target(self):
        planned = self.plan([self.question(ids=["first"]), self.question(ids=["second"])])
        self.assertEqual(planned["physical_questions"], 1)
        self.assertEqual(planned["product_questions"], 2)
        self.assertEqual(planned["source_attempts_created"], 0)
        task = self.memory.snapshot()["query_tasks"][planned["task_ids"][0]]
        self.assertEqual(set(task["targets"]), {"first", "second"})
        self.assertFalse(task["collector_dispatched"])
        before = self.memory.snapshot()
        again = self.plan([self.question(ids=["first"]), self.question(ids=["second"])])
        self.assertEqual(again, planned)
        self.assertEqual(before, self.memory.snapshot())
        self.assertFalse(self.memory.journal.path.exists())

    def test_actual_location_department_or_scope_changes_are_different_physical_questions(self):
        questions = [self.question(), self.question(query="door pull-up bar")]
        question = self.question("blinkit")
        question["request"]["pincode"] = "400001"
        questions.append(question)
        other = deepcopy(question)
        other["request"]["pincode"] = "110001"
        questions.append(other)
        self.assertEqual(self.plan(questions)["physical_questions"], 4)

    def test_planning_refuses_secret_scope_and_does_not_call_sources(self):
        question = self.question()
        question["request"]["api_key"] = "private-fixture"
        with self.assertRaisesRegex(SafetyError, "PRIVATE_REQUEST"):
            self.plan([question])
        self.assertEqual(self.memory.snapshot()["query_tasks"], {})

    def test_all_routes_builder_records_exact_preparation_gap_without_attempt(self):
        def builder(context):
            if context["route"] == "company-pages":
                raise SafetyError("SUPPLIER_DESTINATIONS_PENDING")
            if context["route"] == "blinkit":
                return {"not_applicable_reason": "Explicitly outside selected location scope."}
            return {"query": "sink caddy", "country": "IN"}
        prepared = prepare_all_source_tasks(self.memory, run_id="week-one", request_builder=builder, as_of=NOW)
        self.assertEqual(prepared["physical_questions"], 12)
        self.assertEqual(len(prepared["pending_preparation"]), 2)
        self.assertEqual(prepared["product_questions"], 24)
        grid = source_coverage(self.memory, handlers={r: self.acquired for r in ROUTES}, as_of=NOW)
        self.assertEqual(grid["actually_attempted_product_routes"], 0)
        self.assertTrue(all(r["reason"] == "SUPPLIER_DESTINATIONS_PENDING" for r in grid["rows"] if r["route"] == "company-pages"))

    def test_raw_rows_need_per_target_interpretation_and_replay_never_recollects(self):
        self.plan([self.question(ids=["first", "second"])])
        calls = []
        def handler(context):
            calls.append(context["acquisition_id"])
            return self.acquired(context)
        first = self.run_sources({"meta-ads": handler})
        self.assertEqual(first["handler_calls"], 1)
        grid = source_coverage(self.memory, handlers={"meta-ads": handler}, as_of=NOW)
        self.assertEqual(grid["states"]["interpretation_pending"], 2)
        self.assertEqual(grid["actually_attempted_product_routes"], 2)
        self.assertEqual(grid["physical_attempts"], 1)
        second = self.run_sources({"meta-ads": handler})
        self.assertEqual(second["handler_calls"], 0)
        self.assertEqual(len(calls), 1)

    def test_new_target_can_reuse_capture_but_never_another_targets_verdict(self):
        planned = self.plan([self.question()])
        self.run_sources({"meta-ads": self.acquired})
        self.plan([self.question(ids=["second"])])
        replay = self.run_sources({"meta-ads": self.acquired})
        self.assertEqual(replay["handler_calls"], 0)
        row = next(r for r in source_coverage(self.memory, as_of=NOW)["rows"] if r["concept_id"] == "second" and r["route"] == "meta-ads")
        self.assertEqual(row["state"], "interpretation_pending")
        self.assertNotEqual(row["state"], "completed_relevant")
        task = self.memory.snapshot()["query_tasks"][planned["task_ids"][0]]
        self.assertNotIn("second", task["target_results"])

    def test_empty_after_real_read_is_completed_check_not_missing_research(self):
        self.plan([self.question()])
        def empty(context):
            context["record_dispatch"]()
            return self.receipt(context, state="empty")
        self.run_sources({"meta-ads": empty})
        row = next(r for r in source_coverage(self.memory, as_of=NOW)["rows"] if r["concept_id"] == "first" and r["route"] == "meta-ads")
        self.assertEqual(row["state"], "completed_no_results")
        self.assertTrue(row["actual_attempt"])

    def test_no_dispatch_empty_claim_is_rejected_and_healthy_sibling_continues(self):
        self.plan([self.question(), self.question("google-trends")])
        def invalid(context):
            return self.receipt(context, state="empty", actual=False)
        result = self.run_sources({"meta-ads": invalid, "google-trends": self.acquired})
        self.assertEqual(result["counts"], {"blocked": 1, "acquired": 1})
        grid = source_coverage(self.memory, as_of=NOW)
        row = next(r for r in grid["rows"] if r["concept_id"] == "first" and r["route"] == "meta-ads")
        self.assertEqual(row["state"], "blocked")
        self.assertFalse(row["actual_attempt"])
        self.assertIn("DISPATCH_REQUIRED", row["reason"])

    def test_timeout_keeps_actual_intent_and_siblings_without_detached_late_writer(self):
        self.plan([self.question(), self.question("google-trends")])
        def hangs(context):
            context["record_dispatch"]()
            time.sleep(20)
            self.fail("Outer deadline did not interrupt the handler.")
        result = execute_source_run(self.memory, "week-one", handlers={"meta-ads": hangs, "google-trends": self.acquired},
                                    as_of=NOW, timeout_seconds=0.1)
        self.assertEqual(result["handler_calls"], 2)
        self.assertEqual(result["counts"], {"failed": 1, "acquired": 1})
        blocked = next(row for row in result["results"] if row["state"] == "failed")
        self.assertEqual(blocked["reason"], "SOURCE_ACTION_TIMEOUT")
        self.assertTrue(blocked["actual_attempt"])
        replay = self.run_sources({"meta-ads": hangs, "google-trends": self.acquired})
        self.assertEqual(replay["handler_calls"], 0)

    def test_prelaunch_exception_is_not_an_actual_attempt(self):
        self.plan([self.question(), self.question("google-trends")])
        def local_failure(_context):
            raise SafetyError("SOURCE_BROWSER_EXECUTABLE_MISSING")
        result = self.run_sources({"meta-ads": local_failure, "google-trends": self.acquired})
        blocked = next(row for row in result["results"] if row["state"] == "blocked")
        self.assertFalse(blocked["actual_attempt"])
        self.assertEqual(source_coverage(self.memory, as_of=NOW)["physical_attempts"], 1)

    def test_local_receipt_failure_is_not_google_block_and_sibling_continues(self):
        planned = self.plan([self.question("google-trends"), self.question()])
        def damaged_proof(context):
            context["record_dispatch"]()
            raise SafetyError("DIRECT_TRENDS_RECEIPT_TIME_INVALID")
        result = self.run_sources({"google-trends": damaged_proof, "meta-ads": self.acquired})
        self.assertEqual(result["counts"], {"failed": 1, "acquired": 1})
        failed = next(row for row in result["results"] if row["state"] == "failed")
        task = self.memory.snapshot()["query_tasks"][failed["task_id"]]
        self.assertIn(failed["task_id"], planned["task_ids"])
        self.assertTrue(task["reconcile_required"])
        self.assertFalse(task["retryable"])
        self.assertEqual(task["tries"], 1)
        grid = source_coverage(self.memory, as_of=NOW)
        row = next(row for row in grid["rows"] if row["concept_id"] == "first" and row["route"] == "google-trends")
        self.assertEqual(row["state"], "failed")
        self.assertEqual(row["reason"], "DIRECT_TRENDS_RECEIPT_TIME_INVALID")
        self.assertTrue(row["actual_attempt"])

    def test_saved_reconciliation_retains_actual_google_block_without_recollection(self):
        planned = self.plan([self.question("google-trends")])
        calls = []; recovered = []
        def original(context):
            calls.append(context["acquisition_id"])
            context["record_dispatch"]()
            raise SafetyError("DIRECT_TRENDS_RECEIPT_TIME_INVALID")
        def reconcile(context):
            recovered.append(context["acquisition_id"])
            self.assertTrue(context["reconciling"])
            context["record_dispatch"]()
            return dict(self.receipt(context, state="blocked"), reason="DIRECT_TRENDS_ACCESS_CHALLENGE")
        handlers = {"google-trends": {"handler": original, "reconcile": reconcile}}
        self.run_sources(handlers)
        task_id = planned["task_ids"][0]
        original_acquisition = self.memory.snapshot()["query_tasks"][task_id]["acquisition_id"]
        result = self.run_sources(handlers)
        self.assertEqual(result["counts"], {"blocked": 1})
        task = self.memory.snapshot()["query_tasks"][task_id]
        self.assertEqual(task["acquisition_id"], original_acquisition)
        self.assertEqual(task["tries"], 1)
        self.assertFalse(task["reconcile_required"])
        self.assertEqual(calls, [original_acquisition])
        self.assertEqual(recovered, [original_acquisition])
        row = next(row for row in source_coverage(self.memory, as_of=NOW)["rows"]
                   if row["concept_id"] == "first" and row["route"] == "google-trends")
        self.assertEqual(row["state"], "blocked")
        self.assertEqual(row["reason"], "DIRECT_TRENDS_ACCESS_CHALLENGE")

    def test_unproved_local_receipt_failure_does_not_invent_dispatch(self):
        self.plan([self.question("google-trends")])
        def invalid_receipt(_context):
            raise SafetyError("DIRECT_TRENDS_ARTIFACT_HASH_MISMATCH")
        result = self.run_sources({"google-trends": invalid_receipt})
        self.assertEqual(result["counts"], {"failed": 1})
        self.assertFalse(result["results"][0]["actual_attempt"])
        self.assertEqual(source_coverage(self.memory, as_of=NOW)["physical_attempts"], 0)

    def test_saved_reconciliation_has_a_turn_before_new_reads(self):
        prepared = self.plan([self.question("google-trends"), self.question()])
        def incomplete(context):
            context["record_dispatch"]()
            raise SafetyError("DIRECT_TRENDS_RECEIPT_TIME_INVALID")
        self.run_sources({"google-trends": incomplete})
        repaired = []; new_calls = []
        def reconcile(context):
            repaired.append(context["task_id"])
            return self.receipt(context, state="blocked")
        def new(context):
            new_calls.append(context["task_id"])
            return self.acquired(context)
        result = self.run_sources({"google-trends": {"handler": incomplete, "reconcile": reconcile},
                                   "meta-ads": new}, action_limit=1)
        self.assertEqual(result["handler_calls"], 1)
        self.assertEqual(len(repaired), 1)
        self.assertIn(repaired[0], prepared["task_ids"])
        self.assertEqual(new_calls, [])

    def test_route_pause_does_not_prevent_saved_only_reconciliation(self):
        from product_radar.research_memory import change
        prepared = self.plan([self.question("google-trends", query="first query"),
                              self.question("google-trends", query="second query")])
        first_id, second_id = sorted(prepared["task_ids"])
        state = self.memory.snapshot(); first = state["query_tasks"][first_id]
        # Fictional interruption proof is an original intent, not permission
        # to recollect. A different actual receipt pauses the same route.
        interrupted = dict(first, status="dispatching", acquisition_id="original-interrupted-acquisition",
            reconcile_required=True, dispatched_at=NOW, reason="Original receipt needs recovery.")
        self.memory.commit("synthetic-interrupted-intent", [change("query_tasks", first_id, interrupted, first)])
        def reconcile(context):
            return self.receipt(context, state="blocked", actual=False)
        def challenge(context):
            self.assertEqual(context["task_id"], second_id)
            return dict(self.receipt(context, state="blocked"), failure_scope="route", retryable=False)
        # The first pass settles the original interruption, then obtains the
        # second failure. Mark its saved local application as pending again.
        handlers = {"google-trends": {"handler": challenge, "reconcile": reconcile}}
        self.run_sources(handlers)
        state = self.memory.snapshot(); saved = state["query_tasks"][first_id]
        pending = dict(saved, status="failed", reconcile_required=True)
        self.memory.commit("synthetic-unapplied-original-reply", [change("query_tasks", first_id, pending, saved)])
        recovered = []
        def final_reconcile(context):
            recovered.append(context["task_id"])
            return self.receipt(context, state="blocked", actual=False)
        result = self.run_sources({"google-trends": {"handler": challenge, "reconcile": final_reconcile}})
        self.assertEqual(recovered, [first_id])
        self.assertEqual(result["handler_calls"], 1)

    def test_saved_local_prelaunch_failure_consumes_no_actual_attempt_and_can_recover(self):
        planned = self.plan([self.question()], max_attempts=1)
        calls = []
        def busy(context):
            calls.append(context["acquisition_id"])
            return dict(self.receipt(context, state="failed", actual=False, retryable=True), reason="RUNNER_ALREADY_ACTIVE")
        self.run_sources({"meta-ads": busy})
        task_id = planned["task_ids"][0]
        task = self.memory.snapshot()["query_tasks"][task_id]
        self.assertEqual(task["tries"], 0)
        self.assertEqual(task["generation"], 1)
        self.assertEqual(task["prelaunch_failures"], 1)
        original_receipt = task["receipt_refs"][0]
        original_acquisition = task["acquisition_id"]
        row = next(r for r in source_coverage(self.memory, as_of=NOW)["rows"] if r["concept_id"] == "first" and r["route"] == "meta-ads")
        self.assertEqual(row["state"], "not_connected")  # No actual request or new handler in this view.
        self.assertFalse(row["actual_attempt"])
        self.assertEqual(self.run_sources({"meta-ads": busy})["handler_calls"], 0)
        recovery = retry_source_prelaunch_task(self.memory, task_id, reason="The local event-write contention was repaired.", as_of=LATER)
        self.assertTrue(recovery)
        recovered = execute_source_run(self.memory, "week-one", handlers={"meta-ads": self.acquired},
                                       as_of=LATER, bounded_call=self.direct_runner)
        self.assertEqual(recovered["counts"], {"acquired": 1})
        task = self.memory.snapshot()["query_tasks"][task_id]
        self.assertEqual(task["tries"], 1)
        self.assertEqual(task["generation"], 2)
        self.assertNotEqual(task["acquisition_id"], original_acquisition)
        self.assertIn(original_receipt, task["receipt_refs"])
        self.assertFalse(self.memory.snapshot()["source_checks"][original_receipt]["envelope"]["dispatch_started"])
        self.assertEqual(len(task["attempt_refs"]), 1)

    def test_unchanged_prelaunch_failures_are_bounded_without_phantom_physical_tries(self):
        planned = self.plan([self.question()], max_attempts=2)
        calls = []
        def busy(context):
            calls.append(context["acquisition_id"])
            return dict(self.receipt(context, state="failed", actual=False, retryable=True), reason="RUNNER_ALREADY_ACTIVE")
        self.run_sources({"meta-ads": busy})
        execute_source_run(self.memory, "week-one", handlers={"meta-ads": busy}, as_of=LATER, bounded_call=self.direct_runner)
        result = execute_source_run(self.memory, "week-one", handlers={"meta-ads": busy}, as_of=LATER, bounded_call=self.direct_runner)
        self.assertEqual(result["handler_calls"], 0)
        self.assertEqual(len(set(calls)), 2)
        task = self.memory.snapshot()["query_tasks"][planned["task_ids"][0]]
        self.assertEqual(task["tries"], 0)
        self.assertEqual(task["prelaunch_failures"], 2)
        self.assertEqual(source_coverage(self.memory, as_of=LATER)["physical_attempts"], 0)

    def test_prelaunch_recovery_refuses_uncertain_or_actual_requests(self):
        planned = self.plan([self.question()])
        def uncertain(_context):
            raise SafetyError("RUNNER_ALREADY_ACTIVE")
        self.run_sources({"meta-ads": uncertain})
        with self.assertRaisesRegex(SafetyError, "PRELAUNCH_RECEIPT_REQUIRED"):
            retry_source_prelaunch_task(self.memory, planned["task_ids"][0], reason="Cannot prove that this adapter did not launch.")
        def proved_actual(context):
            context["record_dispatch"]()
            return dict(self.receipt(context, state="failed"), reason="RUNNER_ALREADY_ACTIVE")
        self.run_sources({"meta-ads": {"handler": uncertain, "reconcile": proved_actual}})
        with self.assertRaisesRegex(SafetyError, "PRELAUNCH_RECOVERY_NOT_PROVEN"):
            retry_source_prelaunch_task(self.memory, planned["task_ids"][0], reason="Actual request cannot be reset as local startup.")

    def test_unknown_native_launch_is_saved_as_uncertain_not_false_or_complete(self):
        planned = self.plan([self.question(), self.question("google-trends")])
        def unknown(context):
            return dict(self.receipt(context, state="unavailable", actual=False),
                        dispatch_started=None, reason="NATIVE_CHILD_RESULT_UNCERTAIN")
        result = self.run_sources({"meta-ads": unknown, "google-trends": self.acquired})
        self.assertEqual(result["counts"], {"blocked": 1, "acquired": 1})
        blocked = next(row for row in result["results"] if row["state"] == "blocked")
        self.assertTrue(blocked["dispatch_uncertain"])
        task = self.memory.snapshot()["query_tasks"][blocked["task_id"]]
        self.assertEqual(task["tries"], 0)
        self.assertEqual(task["prelaunch_failures"], 0)
        self.assertTrue(task["reconcile_required"])
        envelope = self.memory.snapshot()["source_checks"][task["receipt_refs"][0]]["envelope"]
        self.assertIsNone(envelope["dispatch_started"])
        with self.assertRaisesRegex(SafetyError, "PRELAUNCH_RECOVERY_NOT_PROVEN"):
            retry_source_prelaunch_task(self.memory, blocked["task_id"], reason="Cannot assume the browser did not launch.")
        replay = self.run_sources({"meta-ads": unknown, "google-trends": self.acquired})
        self.assertEqual(replay["handler_calls"], 0)

    def test_later_verified_stage_retains_original_uncertain_receipt_without_new_request(self):
        planned = self.plan([self.question()])
        def unknown(context):
            return dict(self.receipt(context, state="unavailable", actual=False),
                        dispatch_started=None, reason="NATIVE_CHILD_RESULT_UNCERTAIN")
        self.run_sources({"meta-ads": unknown})
        task_id = planned["task_ids"][0]
        original = deepcopy(self.memory.snapshot()["query_tasks"][task_id])
        prior_receipt = deepcopy(self.memory.snapshot()["source_checks"][original["receipt_refs"][0]])
        def verified_stage(context):
            self.assertEqual(context["acquisition_id"], original["acquisition_id"])
            return self.receipt(context)
        result = self.run_sources({"meta-ads": {"handler": unknown, "reconcile": verified_stage}})
        self.assertEqual(result["counts"], {"acquired": 1})
        current = self.memory.snapshot()["query_tasks"][task_id]
        self.assertEqual(current["generation"], original["generation"])
        self.assertEqual(current["tries"], 1)
        self.assertEqual(len(current["receipt_refs"]), 2)
        self.assertEqual(self.memory.snapshot()["source_checks"][original["receipt_refs"][0]], prior_receipt)

    def test_progress_observer_failure_cannot_mark_saved_success_as_source_failure(self):
        self.plan([self.question(), self.question("google-trends")])
        updates = []
        def observer(row):
            updates.append(row["state"])
            raise RuntimeError("Synthetic console output failure.")
        result = self.run_sources({"meta-ads": self.acquired, "google-trends": self.acquired}, on_progress=observer)
        self.assertEqual(result["counts"], {"acquired": 2})
        self.assertEqual(updates, ["acquired", "acquired"])
        self.assertEqual(len(result["progress_errors"]), 2)

    def test_quickcommerce_reconciliation_imports_original_success_without_second_search(self):
        from product_radar.quickcommerce import QuickCommerceClient, prepare_query
        from test_quickcommerce import LOCATION, Opener, Response, SECRET, payload
        from unittest.mock import patch
        import json
        request = prepare_query("blinkit", "sink caddy", LOCATION)
        self.plan([{"route": "blinkit", "request": {"query": "sink caddy", "location": LOCATION}, "concept_ids": ["first"]}])
        opener = Opener(Response(json.dumps(payload(request)).encode()))
        native = lambda *_args, **_kwargs: {"state": "unavailable", "navigation_started": False,
                   "completed_at": NOW, "reason": "Synthetic native reader did not launch."}
        handlers = installed_source_handlers(self.memory, quickcommerce_client=QuickCommerceClient(SECRET, opener=opener),
                                              quickcommerce_options={"native_capture": native})
        commit = self.memory.commit
        once = []
        def local_write_busy(event_id, changes, **options):
            if any(row.get("table") == "captures" for row in changes) and not once:
                once.append(True)
                raise SafetyError("RUNNER_ALREADY_ACTIVE")
            return commit(event_id, changes, **options)
        with patch.object(self.memory, "commit", side_effect=local_write_busy), patch("product_radar.quickcommerce.utc_now", return_value=NOW):
            first = self.run_sources(handlers)
        self.assertEqual(first["counts"], {"blocked": 1})
        self.assertEqual(len(opener.calls), 1)
        task_id = first["results"][0]["task_id"]
        acquisition_id = self.memory.snapshot()["query_tasks"][task_id]["acquisition_id"]
        self.assertTrue((self.memory.root / "quickcommerce_captures" / acquisition_id / "capture.json").is_file())
        self.assertEqual(len(self.memory.snapshot()["captures"]), 0)
        recovered = self.run_sources(handlers)
        self.assertEqual(recovered["counts"], {"acquired": 1})
        self.assertEqual(len(opener.calls), 1)
        self.assertEqual(len(self.memory.snapshot()["observations"]), 1)
        self.assertEqual(self.memory.snapshot()["query_tasks"][task_id]["tries"], 1)
        self.assertEqual(self.memory.snapshot()["query_tasks"][task_id]["acquisition_id"], acquisition_id)

    def test_quickcommerce_saved_only_reconciliation_cannot_collect_when_file_missing(self):
        from product_radar.quickcommerce import QuickCommerceClient
        from test_quickcommerce import LOCATION, Opener, SECRET
        self.plan([{"route": "blinkit", "request": {"query": "sink caddy", "location": LOCATION}, "concept_ids": ["first"]}])
        opener = Opener()
        native = lambda *_args, **_kwargs: {"state": "unavailable", "navigation_started": False,
                   "completed_at": NOW, "reason": "Synthetic native reader did not launch."}
        handlers = installed_source_handlers(self.memory, quickcommerce_client=QuickCommerceClient(SECRET, opener=opener),
                                              quickcommerce_options={"native_capture": native})
        def interrupted(_context):
            raise TimeoutError()
        handlers["blinkit"]["handler"] = interrupted
        self.run_sources(handlers)
        result = self.run_sources(handlers)
        self.assertEqual(result["handler_calls"], 1)
        self.assertEqual(result["results"][0]["reason"], "SOURCE_ORIGINAL_RECEIPT_PENDING")
        self.assertEqual(opener.calls, [])

    def test_continue_run_prepares_real_new_company_dependencies_without_recollecting_searches(self):
        calls = []
        def builder(context):
            if context["route"] not in {"google-suppliers", "company-pages"}:
                return {"not_applicable_reason": "Synthetic route outside this focused contract."}
            if context["route"] == "company-pages" and not context["state"]["source_checks"].get("synthetic-destination"):
                raise SafetyError("SOURCE_SUPPLIER_DESTINATIONS_PENDING")
            return {"query": "sink caddy", "country": "IN"}
        def search(context):
            calls.append(context["route"])
            self.memory.put("source_checks", "synthetic-destination", {"state": "captured", "url": "https://supplier.example.org/sink-caddy"})
            return self.acquired(context)
        def company(context):
            calls.append(context["route"])
            return self.acquired(context)
        result = continue_source_run(self.memory, "week-one", request_builder=builder,
            handlers={"google-suppliers": search, "company-pages": company}, bounded_call=self.direct_runner)
        self.assertEqual(calls, ["google-suppliers", "company-pages"])
        self.assertEqual(result["handler_calls"], 2)
        self.assertEqual(result["product_route_questions"], 28)
        self.assertEqual(result["progress"]["pending_preparation"], 0)
        replay = continue_source_run(self.memory, "week-one", request_builder=builder,
            handlers={"google-suppliers": search, "company-pages": company}, bounded_call=self.direct_runner)
        self.assertEqual(replay["handler_calls"], 0)
        self.assertEqual(len(calls), 2)

    def test_continue_run_reconciliation_has_fixed_point_instead_of_unchanged_loop(self):
        def builder(context):
            return ({"query": "sink caddy", "country": "IN"} if context["route"] == "meta-ads" else
                    {"not_applicable_reason": "Synthetic unaffected route."})
        calls = []
        def uncertain(context):
            calls.append(context["acquisition_id"])
            raise TimeoutError()
        result = continue_source_run(self.memory, "week-one", request_builder=builder,
            handlers={"meta-ads": {"handler": uncertain, "reconcile": uncertain}}, bounded_call=self.direct_runner)
        self.assertEqual(result["handler_calls"], 2)
        self.assertEqual(len(set(calls)), 1)
        self.assertEqual(result["progress"]["controller_acquisition_attempts"], 0)
        self.assertEqual(result["progress"]["task_states"], {"blocked": 1})

    def test_cheap_run_progress_can_reuse_one_verified_snapshot_without_read_or_mutation(self):
        self.plan([self.question(ids=["first", "second"])])
        state = self.memory.snapshot()
        from unittest.mock import patch
        with patch.object(self.memory, "snapshot", side_effect=AssertionError("Second snapshot was unnecessary.")):
            progress = source_run_progress(self.memory, "week-one", state=state)
        self.assertEqual(progress["physical_questions"], 1)
        self.assertEqual(progress["planned_product_route_questions"], 2)
        self.assertEqual(progress["controller_acquisition_attempts"], 0)
        self.assertEqual(progress["captured_target_states"], {})

    def test_continue_run_global_call_limit_leaves_same_programme_resumable(self):
        def builder(_context):
            return {"query": "sink caddy", "country": "IN"}
        handlers = {route: self.acquired for route in ROUTES}
        first = continue_source_run(self.memory, "week-one", request_builder=builder, handlers=handlers,
                                    action_limit=1, bounded_call=self.direct_runner)
        self.assertEqual(first["handler_calls"], 1)
        self.assertEqual(first["progress"]["task_states"]["prepared"], 13)
        second = continue_source_run(self.memory, "week-one", request_builder=builder, handlers=handlers,
                                     action_limit=1, bounded_call=self.direct_runner)
        self.assertEqual(second["handler_calls"], 1)
        self.assertEqual(second["progress"]["task_states"]["prepared"], 12)

    def test_known_failed_read_waits_and_retries_once_without_resetting_run(self):
        self.plan([self.question()], max_attempts=2)
        calls = []
        def fail(context):
            calls.append(context["acquisition_id"])
            context["record_dispatch"]()
            return self.receipt(context, state="failed", retryable=True)
        first = self.run_sources({"meta-ads": fail})
        self.assertEqual(first["handler_calls"], 1)
        waiting = self.run_sources({"meta-ads": fail})
        self.assertEqual(waiting["handler_calls"], 0)
        self.assertEqual(waiting["counts"], {"waiting": 1})
        due = execute_source_run(self.memory, "week-one", handlers={"meta-ads": fail}, as_of=LATER, bounded_call=self.direct_runner)
        self.assertEqual(due["handler_calls"], 1)
        exhausted = execute_source_run(self.memory, "week-one", handlers={"meta-ads": fail}, as_of=LATER, bounded_call=self.direct_runner)
        self.assertEqual(exhausted["handler_calls"], 0)
        self.assertEqual(len(set(calls)), 2)
        self.assertEqual(source_coverage(self.memory, as_of=LATER)["physical_attempts"], 2)

    def test_same_pass_route_outage_pauses_only_untried_siblings_not_all_work(self):
        self.plan([self.question(), self.question(ids=["second"], query="different product"), self.question("google-trends")])
        calls = []
        def route_blocked(context):
            calls.append(context["acquisition_id"])
            context["record_dispatch"]()
            return dict(self.receipt(context, state="blocked"), failure_scope="route")
        result = self.run_sources({"meta-ads": route_blocked, "google-trends": self.acquired})
        self.assertEqual(len(calls), 1)
        self.assertEqual(result["handler_calls"], 2)
        actual = [row for row in result["results"] if row.get("actual_attempt")]
        self.assertEqual(len(actual), 2)
        self.assertTrue(any(row.get("blocked_by") and not row["actual_attempt"] for row in result["results"]))

    def test_shared_outage_persists_across_passes_without_sending_each_product(self):
        self.plan([self.question(), self.question(ids=["second"], query="another type")])
        calls = []
        def block(context):
            calls.append(context["acquisition_id"])
            context["record_dispatch"]()
            return dict(self.receipt(context, state="blocked", retryable=True), failure_scope="route")
        self.run_sources({"meta-ads": block})
        self.run_sources({"meta-ads": block})
        self.assertEqual(len(calls), 1)
        due = execute_source_run(self.memory, "week-one", handlers={"meta-ads": block}, as_of=LATER, bounded_call=self.direct_runner)
        self.assertEqual(due["handler_calls"], 1)
        self.assertEqual(len(calls), 2)
        later = execute_source_run(self.memory, "week-one", handlers={"meta-ads": block}, as_of=LATER, bounded_call=self.direct_runner)
        self.assertEqual(later["handler_calls"], 0)
        self.assertEqual(len(calls), 2)

    def test_recovered_due_route_probe_releases_healthy_untried_siblings(self):
        self.plan([self.question(), self.question(ids=["second"], query="another type")])
        def block(context):
            context["record_dispatch"]()
            return dict(self.receipt(context, state="blocked", retryable=True), failure_scope="route")
        self.run_sources({"meta-ads": block})
        result = execute_source_run(self.memory, "week-one", handlers={"meta-ads": self.acquired}, as_of=LATER, bounded_call=self.direct_runner)
        self.assertEqual(result["handler_calls"], 2)
        self.assertEqual(result["counts"], {"acquired": 2})
        run = self.memory.snapshot()["batch_runs"][key("source-run", "week-one")]
        self.assertEqual(run["route_pauses"], {})

    def test_explicit_recovery_resumes_only_untouched_route_work_without_changing_money(self):
        self.plan([self.question(), self.question(ids=["second"], query="another type")])
        def block(context):
            context["record_dispatch"]()
            return dict(self.receipt(context, state="blocked"), failure_scope="route")
        self.run_sources({"meta-ads": block})
        self.assertEqual(self.run_sources({"meta-ads": self.acquired})["handler_calls"], 0)
        record = resume_source_route(self.memory, "week-one", "meta-ads", reason="Synthetic independently checked route recovery.", as_of=LATER)
        self.assertTrue(record)
        self.assertEqual(self.run_sources({"meta-ads": self.acquired})["handler_calls"], 1)
        self.assertFalse(self.memory.journal.path.exists())

    def test_action_limit_is_not_a_permanent_or_report_filled_stop(self):
        self.plan([self.question(), self.question("google-trends"), self.question("amazon-search")])
        handlers = {route: self.acquired for route in ROUTES}
        first = self.run_sources(handlers, action_limit=1)
        self.assertEqual(first["handler_calls"], 1)
        second = self.run_sources(handlers, action_limit=1)
        self.assertEqual(second["handler_calls"], 1)
        third = self.run_sources(handlers, action_limit=1)
        self.assertEqual(third["handler_calls"], 1)
        final = self.run_sources(handlers, action_limit=1)
        self.assertEqual(final["handler_calls"], 0)
        self.assertTrue(final["report_filled_does_not_stop_work"])

    def test_changed_target_does_not_collect_using_old_description_scope(self):
        self.plan([self.question()])
        old = self.memory.snapshot()["concepts"]["first"]
        updated = deepcopy(old)
        updated["description"]["mechanism"] = "powered"
        self.memory.put("concepts", "first", updated, previous=old)
        result = self.run_sources({"meta-ads": self.acquired})
        self.assertEqual(result["handler_calls"], 0)
        self.assertIn("descriptions changed", result["results"][0]["reason"])

    def test_completed_relevance_needs_saved_import_and_bound_evidence(self):
        self.plan([self.question()])
        def invalid(context):
            return dict(self.receipt(context), capture_ids=["not-imported"])
        result = self.run_sources({"meta-ads": invalid})
        self.assertIn("IMPORT_REQUIRED", result["results"][0]["reason"])

    def test_existing_unrelated_capture_cannot_be_claimed_as_this_route_attempt(self):
        self.plan([self.question()])
        self.memory.put("captures", "other-source-capture", {"source": "amazon", "acquisition_id": "old-question"})
        def invalid(context):
            return dict(self.receipt(context), capture_ids=["other-source-capture"])
        result = self.run_sources({"meta-ads": invalid})
        self.assertEqual(result["results"][0]["reason"], "SOURCE_RECEIPT_CAPTURE_SCOPE_INVALID")

    def test_trends_current_evidence_ids_are_bound_without_faking_marketplace_captures(self):
        self.plan([self.question("google-trends")])
        def chart(context):
            context["record_dispatch"]()
            row = {"concept_id": "first", "description_hash": context["description_hashes"]["first"],
                   "acquisition_id": context["acquisition_id"], "parsed": {"state": "Usable"}}
            self.memory.put("trends_evidence", "chart-evidence", row)
            targets = [{"concept_id": "first", "description_hash": context["description_hashes"]["first"],
                        "state": "completed_relevant", "evidence_refs": ["chart-evidence"], "reason": "Synthetic current chart binding."}]
            return dict(self.receipt(context, targets=targets), evidence_refs=["chart-evidence"])
        result = self.run_sources({"google-trends": chart})
        self.assertEqual(result["counts"], {"acquired": 1})
        self.assertEqual(self.memory.snapshot()["captures"], {})
        row = next(r for r in source_coverage(self.memory, as_of=NOW)["rows"] if r["concept_id"] == "first" and r["route"] == "google-trends")
        self.assertEqual(row["state"], "completed_relevant")
        self.assertEqual(row["evidence_refs"], ["chart-evidence"])

    def test_accepted_relevance_is_bound_idempotent_and_no_new_source_work(self):
        planned = self.plan([self.question()])
        self.run_sources({"meta-ads": self.acquired})
        task_id = planned["task_ids"][0]
        task = self.memory.snapshot()["query_tasks"][task_id]
        result = {"concept_id": "first", "description_hash": task["targets"]["first"],
                  "state": "completed_relevant", "evidence_refs": task["receipt_refs"],
                  "reason": "Synthetic accepted interpretation of the exact saved receipt."}
        check = bind_source_relevance(self.memory, task_id, [result], as_of=NOW)
        self.assertEqual(check, bind_source_relevance(self.memory, task_id, [result], as_of=LATER))
        row = next(r for r in source_coverage(self.memory, as_of=LATER)["rows"] if r["concept_id"] == "first" and r["route"] == "meta-ads")
        self.assertEqual(row["state"], "completed_relevant")
        self.assertEqual(row["evidence_refs"], task["receipt_refs"])
        self.assertEqual(self.run_sources({"meta-ads": self.acquired})["handler_calls"], 0)

    def test_explicit_follow_on_replays_exact_acquisition_but_not_another_target_verdict(self):
        original = self.plan([self.question()]); calls = []
        def collect(context):
            calls.append("live"); return self.acquired(context)
        self.run_sources({"meta-ads": collect})
        old_task = self.memory.snapshot()["query_tasks"][original["task_ids"][0]]
        bind_source_relevance(self.memory, original["task_ids"][0], [{"concept_id": "first",
            "description_hash": old_task["targets"]["first"], "state": "completed_relevant",
            "evidence_refs": old_task["receipt_refs"], "reason": "Synthetic original accepted target."}], as_of=NOW)
        child = prepare_source_tasks(self.memory, [self.question(ids=["second"])], run_id="follow-on",
            as_of=NOW, reuse_run_ids=["week-one"])
        def replay(context):
            calls.append("saved-only"); context["record_dispatch"](); return self.receipt(context)
        result = execute_source_run(self.memory, "follow-on", handlers={"meta-ads": {"handler": collect, "reconcile": replay}},
            as_of=NOW, bounded_call=self.direct_runner)
        task = self.memory.snapshot()["query_tasks"][child["task_ids"][0]]
        self.assertEqual(calls, ["live", "saved-only"])
        self.assertEqual(task["acquisition_id"], old_task["acquisition_id"])
        self.assertEqual(task["reused_acquisition_ref"], original["task_ids"][0])
        self.assertEqual(task["target_results"]["second"]["state"], "interpretation_pending")
        self.assertEqual(source_run_progress(self.memory, "follow-on")["reused_acquisitions"], 1)
        self.assertEqual(sum(row.get("kind") == "all_source_dispatch_v1" for row in self.memory.snapshot()["source_checks"].values()), 1)
        self.assertEqual(result["handler_calls"], 1)

    def test_other_runs_are_not_reused_without_explicit_scope_or_with_different_query(self):
        self.plan([self.question()]); self.run_sources({"meta-ads": self.acquired})
        for run_id, query, reuse in (("fresh-week", "sink caddy", None), ("different-term", "soap dispenser", ["week-one"])):
            row = prepare_source_tasks(self.memory, [self.question(ids=["second"], query=query)], run_id=run_id,
                                       as_of=NOW, reuse_run_ids=reuse)
            task = self.memory.snapshot()["query_tasks"][row["task_ids"][0]]
            self.assertEqual(task["status"], "prepared")
            self.assertNotIn("reused_acquisition_ref", task)

    def test_unproved_or_uncertain_source_attempt_is_not_a_reusable_acquisition(self):
        self.plan([self.question()])
        self.run_sources({"meta-ads": lambda context: self.receipt(context, state="failed", actual=False)})
        child = prepare_source_tasks(self.memory, [self.question(ids=["second"])], run_id="follow-on",
                                     as_of=NOW, reuse_run_ids=["week-one"])
        self.assertEqual(self.memory.snapshot()["query_tasks"][child["task_ids"][0]]["status"], "prepared")

    def test_discovery_acquisition_is_replayed_from_its_exact_saved_question(self):
        request = self.question()["request"]; cycle_id = "synthetic-discovery-cycle"; question_id = "first-question"
        self.memory.put("batch_runs", cycle_id, {"kind": "catalogue-discovery-cycle-v1",
            "planned_actions": [{"questions": [{"id": question_id, "route": "meta-ads", "request": request}]}],
            "actions": {question_id: {"state": "interpretation_pending", "dispatch_started": True,
                                       "acquisition_id": "original-discovery-acquisition"}}})
        child = prepare_source_tasks(self.memory, [self.question(ids=["second"])], run_id="follow-on",
            as_of=NOW, reuse_discovery_cycle_ids=[cycle_id])
        task = self.memory.snapshot()["query_tasks"][child["task_ids"][0]]
        self.assertEqual(task["acquisition_id"], "original-discovery-acquisition")
        self.assertTrue(task["reconcile_required"])
        self.assertEqual(task["reused_acquisition_ref"], {"cycle_id": cycle_id, "question_id": question_id})

    def test_missing_saved_only_reconciler_never_falls_back_to_recollecting_shared_acquisition(self):
        self.plan([self.question()]); self.run_sources({"meta-ads": self.acquired})
        prepare_source_tasks(self.memory, [self.question(ids=["second"])], run_id="follow-on", as_of=NOW,
                             reuse_run_ids=["week-one"])
        def forbidden(_context):
            raise AssertionError("A saved acquisition must not be collected again")
        result = execute_source_run(self.memory, "follow-on", handlers={"meta-ads": forbidden},
                                    as_of=NOW, bounded_call=self.direct_runner)
        self.assertEqual(result["handler_calls"], 0)
        self.assertEqual(result["counts"], {"blocked": 1})

    def test_hundreds_of_unchanged_queued_rows_use_one_finite_verified_pass_not_one_read_each(self):
        from unittest.mock import patch
        self.plan([self.question(query="synthetic family " + str(number)) for number in range(300)])
        with patch.object(self.memory, "snapshot", wraps=self.memory.snapshot) as observed:
            result = self.run_sources({})
        self.assertEqual(result["handler_calls"], 0)
        self.assertEqual(result["counts"], {"not_connected": 64})
        self.assertEqual(result["inspection_scanned"], 64)
        self.assertTrue(result["inspection_pending"])
        self.assertEqual(result["inspection_remaining"], 236)
        self.assertLessEqual(observed.call_count, 3)

    def test_supplied_verified_coverage_state_requires_no_second_full_read(self):
        from unittest.mock import patch
        state = self.memory.snapshot()
        with patch.object(self.memory, "snapshot", side_effect=AssertionError("Unnecessary repeated verified read")):
            result = source_coverage(self.memory, handlers={}, as_of=NOW, state=state)
        self.assertEqual(result["product_route_questions"], 28)
        self.assertEqual(result["actually_attempted_product_routes"], 0)

    def test_relevance_rejects_unrelated_reference_or_changed_product(self):
        planned = self.plan([self.question()])
        self.run_sources({"meta-ads": self.acquired})
        task_id = planned["task_ids"][0]
        task = self.memory.snapshot()["query_tasks"][task_id]
        result = {"concept_id": "first", "description_hash": "0" * 64,
                  "state": "completed_relevant", "evidence_refs": task["receipt_refs"], "reason": "Fixture."}
        with self.assertRaisesRegex(SafetyError, "BINDING_INVALID"):
            bind_source_relevance(self.memory, task_id, [result], as_of=NOW)
        result["description_hash"] = task["targets"]["first"]
        result["evidence_refs"] = ["another-source"]
        with self.assertRaisesRegex(SafetyError, "BINDING_INVALID"):
            bind_source_relevance(self.memory, task_id, [result], as_of=NOW)

    def test_no_handler_is_not_attempted_or_completed(self):
        self.plan([self.question()])
        result = self.run_sources({})
        self.assertEqual(result["handler_calls"], 0)
        self.assertEqual(result["counts"], {"not_connected": 1})
        self.assertEqual(source_coverage(self.memory, as_of=NOW)["actually_attempted_product_routes"], 0)

    def test_product_request_builder_uses_family_not_full_seller_title_and_shares_lists(self):
        builder = product_source_request_builder()
        context = {"route": "amazon-bestsellers", "concept_id": "first", "state": self.memory.snapshot(),
                   "description": self.memory.snapshot()["concepts"]["first"]["description"],
                   "description_hash": digest(self.memory.snapshot()["concepts"]["first"]["description"])}
        listed = builder(context)
        self.assertEqual(listed["department"], "kitchen")
        self.assertIsNone(listed["term"])
        context["route"] = "amazon-search"
        self.assertEqual(builder(context)["term"].casefold(), "sink caddy")
        context["route"] = "company-pages"
        with self.assertRaisesRegex(SafetyError, "DESTINATIONS_PENDING"):
            builder(context)

    def test_home_and_decor_list_uses_verified_india_node_not_undefined_home_department(self):
        builder = product_source_request_builder()
        state = self.memory.snapshot()
        description = dict(state["concepts"]["first"]["description"], category="Home and Decor")
        request = builder({"route": "amazon-bestsellers", "concept_id": "first", "state": state,
                           "description": description, "description_hash": digest(description)})
        self.assertEqual(request["department"], "kitchen/1380374031")
        self.assertEqual(request["url"], "https://www.amazon.in/gp/bestsellers/kitchen/1380374031")

    def test_undefined_old_decor_request_is_superseded_without_rewriting_original_receipt(self):
        from product_radar.marketplace_browser import build_marketplace_request
        state = self.memory.snapshot(); old_concept = state["concepts"]["first"]
        self.memory.put("concepts", "first", dict(old_concept,
            description=dict(old_concept["description"], category="Home and Decor")), previous=old_concept)
        original = build_marketplace_request("amazon-bestsellers", department="home")
        prepared = self.plan([{"route": "amazon-bestsellers", "request": original, "concept_ids": ["first"]}])
        old_id = prepared["task_ids"][0]
        self.run_sources({"amazon-bestsellers": lambda context: self.receipt(context, state="empty")})
        before = self.memory.snapshot()["query_tasks"][old_id]
        builder = product_source_request_builder(run_id="week-one")
        prepare_all_source_tasks(self.memory, run_id="week-one", request_builder=builder, concept_ids=["first"], as_of=NOW)
        current = self.memory.snapshot()["query_tasks"][old_id]
        self.assertEqual(current["request"], before["request"])
        self.assertEqual(current["receipt_refs"], before["receipt_refs"])
        self.assertEqual(current["attempt_refs"], before["attempt_refs"])
        self.assertIn("first", current["superseded_targets"])
        grid = source_coverage(self.memory, handlers={"amazon-bestsellers": self.acquired}, concept_ids=["first"], as_of=NOW)
        row = next(row for row in grid["rows"] if row["route"] == "amazon-bestsellers")
        self.assertEqual(row["state"], "not_attempted")
        self.assertNotIn(old_id, row["task_ids"])
        calls = []
        def correct(context):
            calls.append(context["request"]["department"]); return self.acquired(context)
        self.run_sources({"amazon-bestsellers": correct})
        self.assertEqual(calls, ["kitchen/1380374031"])

    def test_undefined_prepared_decor_request_never_dispatches_after_verified_replacement(self):
        from product_radar.marketplace_browser import build_marketplace_request
        state = self.memory.snapshot(); old_concept = state["concepts"]["first"]
        self.memory.put("concepts", "first", dict(old_concept,
            description=dict(old_concept["description"], category="Home and Decor")), previous=old_concept)
        self.plan([{"route": "amazon-bestsellers", "request": build_marketplace_request("amazon-bestsellers", department="home"),
                    "concept_ids": ["first"]}])
        prepare_all_source_tasks(self.memory, run_id="week-one", request_builder=product_source_request_builder(run_id="week-one"),
                                 concept_ids=["first"], as_of=NOW)
        called = []
        def collector(context):
            called.append(context["request"]["department"]); return self.acquired(context)
        results = self.run_sources({"amazon-bestsellers": collector})
        self.assertEqual(called, ["kitchen/1380374031"])
        self.assertEqual(results["counts"].get("superseded"), 1)

    def test_wrong_or_uncertain_supersession_does_not_remove_original_recovery(self):
        from product_radar.marketplace_browser import build_marketplace_request
        original = build_marketplace_request("amazon-bestsellers", department="home")
        old_id = self.plan([{"route": "amazon-bestsellers", "request": original, "concept_ids": ["first"]}])["task_ids"][0]
        with self.assertRaisesRegex(SafetyError, "SUPERSEDED_BINDING_INVALID"):
            self.plan([{"route": "amazon-bestsellers", "request": build_marketplace_request("amazon-bestsellers", department="kitchen/1380374031"),
                        "concept_ids": ["first"], "supersedes_task_ids": [old_id]}])
        self.assertNotIn("superseded_targets", self.memory.snapshot()["query_tasks"][old_id])

    def test_shared_undefined_decor_scope_is_superseded_for_each_target_in_one_commit(self):
        from product_radar.marketplace_browser import build_marketplace_request
        for cid in ("first", "second"):
            old = self.memory.snapshot()["concepts"][cid]
            self.memory.put("concepts", cid, dict(old, description=dict(old["description"], category="Home and Decor")), previous=old)
        old_id = self.plan([{"route": "amazon-bestsellers", "request": build_marketplace_request("amazon-bestsellers", department="home"),
                             "concept_ids": ["first", "second"]}])["task_ids"][0]
        prepare_all_source_tasks(self.memory, run_id="week-one", request_builder=product_source_request_builder(run_id="week-one"),
                                 concept_ids=["first", "second"], as_of=NOW)
        state = self.memory.snapshot()
        self.assertEqual(set(state["query_tasks"][old_id]["superseded_targets"]), {"first", "second"})
        called = []
        def collector(context):
            called.append(context["concept_ids"]); return self.acquired(context)
        self.run_sources({"amazon-bestsellers": collector})
        self.assertEqual(called, [["first", "second"]])

    def test_sparse_source_questions_advance_at_most_three_distinct_reviewed_phrases(self):
        self.query_profile()
        builder = product_source_request_builder(run_id="week-one")
        calls = []
        def empty(context):
            calls.append(context["request"]["query"])
            context["record_dispatch"]()
            return self.receipt(context, state="empty")
        for _ in range(5):
            self.plan_query(builder)
            self.run_sources({"meta-ads": empty})
        self.assertEqual(calls, ["draining sink caddy", "sink organizer", "sink holder"])
        state = self.memory.snapshot()
        tasks = list(state["query_tasks"].values())
        self.assertEqual(len(tasks), 3)
        last = next(task for task in tasks if task["request"]["query"] == "sink holder")
        plan = last["query_plans"]["first"]
        self.assertEqual((plan["term_index"], plan["term_count"]), (3, 3))
        self.assertEqual(len(plan["previous_task_ids"]), 2)
        self.assertTrue(plan["previous_receipt_refs"])
        self.assertEqual(source_run_progress(self.memory, "week-one")["controller_acquisition_attempts"], 3)

    def test_raw_pending_result_does_not_launch_a_second_phrase_before_relevance(self):
        self.query_profile()
        builder = product_source_request_builder(run_id="week-one")
        first = self.plan_query(builder)
        self.run_sources({"meta-ads": self.acquired})
        again = self.plan_query(builder)
        self.assertEqual(first["task_ids"], again["task_ids"])
        self.assertEqual(self.run_sources({"meta-ads": self.acquired})["handler_calls"], 0)

    def test_accepted_unrelated_result_advances_only_that_target_not_shared_siblings(self):
        self.query_profile("first")
        self.query_profile("second")
        builder = product_source_request_builder(run_id="week-one")
        first = self.plan_query(builder)
        self.plan_query(builder, concept_id="second")
        self.run_sources({"meta-ads": self.acquired})
        task_id = first["task_ids"][0]
        task = self.memory.snapshot()["query_tasks"][task_id]
        bind_source_relevance(self.memory, task_id, [{"concept_id": "first",
            "description_hash": task["targets"]["first"], "state": "completed_no_results",
            "evidence_refs": task["receipt_refs"], "reason": "Saved returned rows concern unrelated products."}], as_of=NOW)
        self.assertEqual(builder(self.query_context())["query"], "sink organizer")
        self.assertEqual(builder(self.query_context(concept_id="second"))["query"], "draining sink caddy")
        self.plan_query(builder)
        self.assertEqual(self.run_sources({"meta-ads": self.acquired})["handler_calls"], 1)

    def test_readable_insufficient_scope_allows_an_alternative_but_access_failures_do_not(self):
        self.query_profile()
        builder = product_source_request_builder(run_id="week-one")
        for route, state in (("meta-ads", "insufficient"), ("google-trends", "blocked"), ("google-suppliers", "failed")):
            self.plan_query(builder, route=route)
            def outcome(context, state=state):
                context["record_dispatch"]()
                return self.receipt(context, state=state)
            self.run_sources({route: outcome})
        self.assertEqual(builder(self.query_context("meta-ads"))["query"], "sink organizer")
        for route in ("google-trends", "google-suppliers"):
            self.assertEqual(builder(self.query_context(route))["query"], "draining sink caddy")

    def test_old_run_empty_or_different_location_does_not_consume_current_phrase_sequence(self):
        self.query_profile()
        builder = product_source_request_builder()
        self.plan_query(builder)
        self.run_sources({"meta-ads": lambda context: self.receipt(context, state="empty")})
        self.assertEqual(builder(self.query_context(run_id="week-two"))["query"], "draining sink caddy")
        mumbai = {"name": "Mumbai", "lat": "19.0760", "lon": "72.8777"}
        first = product_source_request_builder(location=mumbai, run_id="week-one")
        self.plan_query(first, route="blinkit")
        self.run_sources({"blinkit": lambda context: self.receipt(context, state="empty")})
        self.assertEqual(first(self.query_context("blinkit"))["query"], "sink organizer")
        delhi = product_source_request_builder(location={"name": "Delhi", "lat": "28.6139", "lon": "77.2090"}, run_id="week-one")
        self.assertEqual(delhi(self.query_context("blinkit"))["query"], "draining sink caddy")

    def test_alias_reuse_skips_normalised_duplicate_phrase_without_inventing_synonyms(self):
        self.query_profile(alternatives=False, aliases=["SINK CADDY", "draining sink holder"])
        builder = product_source_request_builder(run_id="week-one")
        self.plan_query(builder)
        self.run_sources({"meta-ads": lambda context: self.receipt(context, state="empty")})
        context = self.query_context()
        self.assertEqual(builder(context)["query"], "draining sink holder")
        self.assertEqual(context["query_plan"]["term_count"], 2)

    def test_local_intent_admission_failure_launches_nothing_and_keeps_healthy_sibling(self):
        from unittest.mock import patch
        self.plan([self.question(), self.question("google-trends")])
        original_commit = self.memory.commit
        failures = []
        def contended(event_id, changes):
            if (not failures and any(row["table"] == "query_tasks" and row["value"].get("route") == "meta-ads"
                                     and row["value"].get("status") == "dispatching" for row in changes)):
                failures.append(event_id)
                raise SafetyError("RUNNER_ALREADY_ACTIVE")
            return original_commit(event_id, changes)
        with patch.object(self.memory, "commit", side_effect=contended):
            result = self.run_sources({"meta-ads": self.acquired, "google-trends": self.acquired})
        self.assertEqual(result["counts"], {"not_attempted": 1, "acquired": 1})
        self.assertEqual(result["handler_calls"], 1)
        blocked = next(task for task in self.memory.snapshot()["query_tasks"].values() if task["route"] == "meta-ads")
        self.assertEqual((blocked["status"], blocked["generation"], blocked["tries"]), ("prepared", 0, 0))
        self.assertEqual(self.run_sources({"meta-ads": self.acquired, "google-trends": self.acquired})["handler_calls"], 1)

    def test_local_outcome_double_save_conflict_reuses_staged_reply_without_source_call(self):
        from unittest.mock import patch
        planned = self.plan([self.question(), self.question("google-trends")])
        original_commit = self.memory.commit
        calls = []
        def contended(event_id, changes):
            if any(row["table"] == "source_checks" and row["value"].get("kind") == CHECK_KIND
                   and row["value"].get("route") == "meta-ads" for row in changes):
                raise SafetyError("RUNNER_ALREADY_ACTIVE")
            if any(row["table"] == "query_tasks" and row["value"].get("route") == "meta-ads"
                   and row["value"].get("status") == "blocked" for row in changes):
                raise SafetyError("RUNNER_ALREADY_ACTIVE")
            return original_commit(event_id, changes)
        def source(context):
            calls.append(context["route"])
            return self.acquired(context)
        with patch.object(self.memory, "commit", side_effect=contended):
            result = self.run_sources({"meta-ads": source, "google-trends": source})
        self.assertEqual(result["counts"], {"blocked": 1, "acquired": 1})
        meta = next(task for task in self.memory.snapshot()["query_tasks"].values() if task["route"] == "meta-ads")
        self.assertEqual(meta["status"], "dispatching")
        self.assertEqual(meta["tries"], 1)
        self.assertTrue(result["local_save_errors"])
        replay = self.run_sources({"meta-ads": source, "google-trends": source})
        self.assertEqual(replay["counts"], {"acquired": 1, "reused": 1})
        self.assertEqual(calls.count("meta-ads"), 1)
        self.assertEqual(source_run_progress(self.memory, "week-one")["controller_acquisition_attempts"], 2)

    def test_unsaved_pass_summary_preserves_actual_shared_route_pause(self):
        from unittest.mock import patch
        self.plan([self.question(query="first family"), self.question(query="second family"), self.question("google-trends")])
        original_commit = self.memory.commit
        failures, calls = [], []
        def contended(event_id, changes):
            if (not failures and any(row["table"] == "batch_runs" and row["value"].get("status") == "pass_finished" for row in changes)):
                failures.append(event_id)
                raise SafetyError("RUNNER_ALREADY_ACTIVE")
            return original_commit(event_id, changes)
        def blocked(context):
            calls.append(context["acquisition_id"])
            context["record_dispatch"]()
            return dict(self.receipt(context, state="blocked"), failure_scope="route")
        with patch.object(self.memory, "commit", side_effect=contended):
            result = self.run_sources({"meta-ads": blocked, "google-trends": self.acquired})
        self.assertTrue(any(row["stage"] == "pass_progress" for row in result["local_save_errors"]))
        self.assertIn("meta-ads", source_run_progress(self.memory, "week-one")["route_pauses"])
        self.plan([self.question(query="third new family")])
        again = self.run_sources({"meta-ads": blocked, "google-trends": self.acquired})
        self.assertEqual(again["handler_calls"], 0)
        self.assertEqual(len(calls), 1)

    def test_pending_pass_pause_can_be_resumed_without_forgetting_the_canonical_failure(self):
        from unittest.mock import patch
        self.plan([self.question(query="first family"), self.question(query="second family")])
        original_commit = self.memory.commit
        def contended(event_id, changes):
            if any(row["table"] == "batch_runs" and row["value"].get("status") == "pass_finished" for row in changes):
                raise SafetyError("RUNNER_ALREADY_ACTIVE")
            return original_commit(event_id, changes)
        def blocked(context):
            context["record_dispatch"]()
            return dict(self.receipt(context, state="blocked"), failure_scope="route")
        with patch.object(self.memory, "commit", side_effect=contended):
            self.run_sources({"meta-ads": blocked})
        before = deepcopy(self.memory.snapshot()["source_checks"])
        resume_source_route(self.memory, "week-one", "meta-ads", reason="Ordinary access recovered after the original shared outage.", as_of=LATER)
        self.assertEqual(source_run_progress(self.memory, "week-one")["route_pauses"], {})
        result = self.run_sources({"meta-ads": self.acquired})
        self.assertEqual(result["handler_calls"], 1)
        for ref, check in before.items():
            self.assertEqual(self.memory.snapshot()["source_checks"][ref], check)

    def test_company_builder_freezes_real_candidate_urls_and_check_ids(self):
        scope = digest(self.memory.snapshot()["concepts"]["first"]["description"])
        self.memory.put("source_checks", "actual-search-check", {"kind": "supplier_route_search", "concept_id": "first",
                        "description_hash": scope, "candidates": [{"url": "https://supplier.example.org/product", "title": "Synthetic supplier"}]})
        context = {"route": "company-pages", "concept_id": "first", "state": self.memory.snapshot(),
                   "description": self.memory.snapshot()["concepts"]["first"]["description"], "description_hash": scope}
        request = product_source_request_builder()(context)
        self.assertEqual(request["candidate_urls"], {"first": ["https://supplier.example.org/product"]})
        self.assertEqual(request["candidate_check_ids"], {"first": ["actual-search-check"]})

    def test_no_discovered_company_after_all_actual_supplier_checks_is_explicitly_conditional(self):
        routes = ("google-suppliers", "google-maps", "indiamart", "tradeindia")
        self.plan([self.question(route) for route in routes])
        def empty(context):
            context["record_dispatch"]()
            return self.receipt(context, state="empty")
        self.run_sources({route: empty for route in routes})
        state = self.memory.snapshot()
        context = {"route": "company-pages", "concept_id": "first", "state": state,
                   "description": state["concepts"]["first"]["description"],
                   "description_hash": digest(state["concepts"]["first"]["description"])}
        self.assertEqual(product_source_request_builder()(context),
                         {"not_applicable_reason": "Actual supplier checks did not identify a company page to read."})
        context["concept_id"] = "second"
        with self.assertRaisesRegex(SafetyError, "DESTINATIONS_PENDING"):
            product_source_request_builder()(context)

    def test_later_real_company_destination_supersedes_earlier_conditional_not_applicable(self):
        self.plan([{"route": "company-pages", "concept_ids": ["first"],
                    "not_applicable_reason": "Actual supplier checks did not identify a company page to read."}])
        request = {"candidate_urls": {"first": ["https://supplier.example.org/product"]},
                   "candidate_check_ids": {"first": ["later-real-search"]}, "country": "IN"}
        prepare_source_tasks(self.memory, [{"route": "company-pages", "request": request, "concept_ids": ["first"]}],
                             run_id="week-one", as_of=LATER)
        grid = source_coverage(self.memory, handlers={"company-pages": self.acquired}, as_of=LATER)
        row = next(r for r in grid["rows"] if r["concept_id"] == "first" and r["route"] == "company-pages")
        self.assertEqual(row["state"], "not_attempted")
        self.assertTrue(row["task_ids"])

    def test_installed_factory_is_fourteen_real_handlers_without_hidden_calls_or_optional_keys(self):
        handlers = installed_source_handlers(self.memory)
        self.assertEqual(len(handlers), 14)
        self.assertIn("meta-ads", handlers)
        self.assertIn("google-trends", handlers)
        self.assertIn("blinkit", handlers)
        self.assertEqual(self.memory.snapshot()["captures"], {})

    def test_concrete_free_marketplace_capture_import_controller_replay(self):
        from product_radar.marketplace_browser import capture_marketplace, build_marketplace_request
        from unittest.mock import patch
        calls = []
        def captured_snapshot(request):
            card = {"card_ref": "card-0", "rank_text": "#1", "title": "Sink caddy pack of 2",
                    "product_url": "https://www.amazon.in/dp/B012345678", "raw_price": "₹129.00",
                    "raw_price_truncated": False, "image_url": "https://m.media-amazon.com/images/I/fixture.jpg"}
            snapshot = {"url": request["url"], "title": "Best Sellers in Home & Kitchen",
                        "heading": "Best Sellers in Home & Kitchen", "text": "Synthetic public list fixture",
                        "query": None, "cards": [card], "card_count": 1, "empty": False}
            return {"state": "captured", "reason": "Synthetic owned reader fixture", "navigation_status": 200,
                    "navigation_started": True, "navigation_started_at": NOW, "stage": "complete",
                    "owned_process_exited": True, "snapshot": snapshot}
        def capture(request, acquisition_id, **options):
            calls.append(acquisition_id)
            return capture_marketplace(request, acquisition_id, **options, snapshot_reader=captured_snapshot)
        req = build_marketplace_request("amazon-bestsellers", department="kitchen")
        self.plan([{"route": "amazon-bestsellers", "request": req, "concept_ids": ["first", "second"]}])
        handlers = create_marketplace_handlers(self.memory, capture=capture)
        with patch("product_radar.marketplace_browser.utc_now", return_value=NOW):
            result = self.run_sources(handlers)
        self.assertEqual(result["counts"], {"acquired": 1})
        self.assertEqual(len(self.memory.snapshot()["observations"]), 1)
        grid = source_coverage(self.memory, handlers=handlers, as_of=NOW)
        self.assertEqual(grid["states"]["interpretation_pending"], 2)
        self.assertEqual(grid["physical_attempts"], 1)
        self.assertEqual(self.run_sources(handlers)["handler_calls"], 0)
        self.assertEqual(len(calls), 1)
        self.assertFalse(self.memory.journal.path.exists())

    def test_concrete_supplier_search_and_company_paths_use_same_controller(self):
        from test_supplier_route_acquisition import BODIES, Reader, QUERY
        from product_radar.supplier_route_acquisition import create_supplier_handlers, supplier_route_url
        reader = Reader({supplier_route_url("google-suppliers", "sink caddy"): BODIES["google-suppliers"]})
        handlers = create_supplier_handlers(self.memory, http_reader=reader, browser_reader=reader, page_reader=reader)
        self.plan([self.question("google-suppliers")])
        first = self.run_sources(handlers)
        self.assertEqual(first["counts"], {"acquired": 1})
        self.assertEqual(len(reader.calls), 1)
        check_refs = [i for i, row in self.memory.snapshot()["source_checks"].items() if row.get("kind") == CHECK_KIND]
        envelope = self.memory.snapshot()["source_checks"][check_refs[0]]["envelope"]
        self.assertTrue(envelope["evidence_refs"])
        self.assertFalse(envelope["capture_ids"])
        context = {"route": "company-pages", "concept_id": "first", "state": self.memory.snapshot(),
                   "description": self.memory.snapshot()["concepts"]["first"]["description"],
                   "description_hash": digest(self.memory.snapshot()["concepts"]["first"]["description"])}
        company_request = product_source_request_builder()(context)
        self.plan([{"route": "company-pages", "request": company_request, "concept_ids": ["first"]}])
        second = self.run_sources(handlers)
        self.assertEqual(second["counts"].get("acquired"), 1)
        row = next(r for r in source_coverage(self.memory, handlers=handlers)["rows"] if r["concept_id"] == "first" and r["route"] == "company-pages")
        self.assertEqual(row["state"], "interpretation_pending")
        self.assertTrue(any(Path(ref).is_file() for ref in row["evidence_refs"]))
        calls = len(reader.calls)
        self.run_sources(handlers)
        self.assertEqual(len(reader.calls), calls)

    def test_existing_verified_supplier_effort_is_coverage_without_fake_new_attempts(self):
        from test_supplier_route_acquisition import Reader
        from product_radar.supplier_route_acquisition import collect_supplier_search
        collect_supplier_search(self.memory, route="indiamart", concept_ids=["first"], query="Food wrap paper", http_reader=Reader(), as_of=NOW)
        grid = source_coverage(self.memory, as_of=NOW)
        row = next(r for r in grid["rows"] if r["concept_id"] == "first" and r["route"] == "indiamart")
        self.assertEqual(row["state"], "interpretation_pending")
        self.assertTrue(row["actual_attempt"])
        self.assertEqual(grid["physical_attempts"], 0)  # Controller attempts are separate from legacy receipts.

    def test_no_company_destinations_is_no_actual_read_not_completion(self):
        self.plan([self.question("company-pages")])
        def no_input(context):
            return self.receipt(context, state="insufficient", actual=False)
        result = self.run_sources({"company-pages": no_input})
        self.assertEqual(result["counts"], {"waiting_input": 1})
        self.assertEqual(source_coverage(self.memory, as_of=NOW)["physical_attempts"], 0)
        self.assertEqual(self.run_sources({"company-pages": no_input})["handler_calls"], 0)

    def test_cli_default_shows_complete_denominator_without_planning_or_source_calls(self):
        spec = importlib.util.spec_from_file_location("coverage_script", ROOT / "scripts/run_source_coverage.py")
        module = importlib.util.module_from_spec(spec); spec.loader.exec_module(module)
        before = self.memory.snapshot()
        from contextlib import redirect_stdout
        stream = StringIO()
        with redirect_stdout(stream):
            self.assertEqual(module.main([], memory=self.memory, handlers={}), 0)
        self.assertIn('"product_route_questions": 28', stream.getvalue())
        self.assertEqual(before, self.memory.snapshot())

    def test_resume_uses_original_acquisition_and_only_registered_reconciliation(self):
        self.plan([self.question()])
        acquisitions = []
        def interrupted(context):
            acquisitions.append(context["acquisition_id"])
            context["record_dispatch"]()
            raise TimeoutError()
        self.run_sources({"meta-ads": interrupted})
        def reconcile(context):
            self.assertTrue(context["reconciling"])
            acquisitions.append(context["acquisition_id"])
            return self.receipt(context)
        result = self.run_sources({"meta-ads": {"handler": interrupted, "reconcile": reconcile}})
        self.assertEqual(result["counts"], {"acquired": 1})
        self.assertEqual(len(set(acquisitions)), 1)
        self.assertEqual(source_coverage(self.memory, as_of=NOW)["physical_attempts"], 1)


if __name__ == "__main__":
    unittest.main()
